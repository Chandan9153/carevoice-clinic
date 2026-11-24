"""FastAPI server: serves the dashboard, the REST API, the text demo
agent, and the Pipecat SmallWebRTC signalling endpoint (/api/offer).

Run:  python -m backend.server        (from the repo root)
Then open http://localhost:8000
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from backend import chat_agent, store, tools  # noqa: E402
from backend.hub import hub  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"

app = FastAPI(title="CareVoice Clinic — Voice Appointment Assistant")
store.init_db()

VOICE_READY = bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")) \
    and bool(os.getenv("ELEVENLABS_API_KEY"))


# ---------------------------------------------------------------- REST API
@app.get("/api/status")
def status():
    return {"voice_ready": VOICE_READY,
            "llm": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            "stt_tts": "ElevenLabs",
            "vad": "Silero",
            "mode": "voice" if VOICE_READY else "demo (text) — add GEMINI_API_KEY and ELEVENLABS_API_KEY to .env for voice"}


@app.get("/api/doctors")
def doctors():
    return {"doctors": store.doctors_with_slots()}


@app.get("/api/appointments")
def appointments(phone: str | None = None):
    return {"appointments": store.list_appointments(store.normalize_phone(phone) if phone else None)}


@app.get("/api/availability")
def availability(doctor: str | None = None, date: str | None = None):
    return tools.check_doctor_availability(doctor, date)


@app.post("/api/chat")
async def chat(body: dict):
    session_id = body.get("session_id") or str(uuid.uuid4())
    result = chat_agent.reply(session_id, body.get("message", ""))
    result["session_id"] = session_id
    return result


@app.get("/api/feed")
def feed(session_id: str):
    return hub.feed(session_id)


@app.post("/api/reset")
def reset():
    store.init_db(reset=True)
    return {"ok": True}


# ------------------------------------------------------- Pipecat WebRTC
_webrtc_handler = None


def _get_webrtc_handler():
    global _webrtc_handler
    if _webrtc_handler is None:
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
        _webrtc_handler = SmallWebRTCRequestHandler()
    return _webrtc_handler


@app.post("/api/offer")
async def offer(request: Request, background_tasks: BackgroundTasks):
    """Browser WebRTC offer -> Pipecat answer. Starts one voice pipeline."""
    if not VOICE_READY:
        return JSONResponse(
            {"error": "Voice not configured. Set GEMINI_API_KEY and ELEVENLABS_API_KEY in .env, "
                      "or use the demo (text) mode in the phone."}, status_code=503)
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest
    body = await request.json()
    session_id = str(uuid.uuid4())
    hub.clear(session_id)

    async def connection_callback(connection):
        from backend.agent import run_bot
        background_tasks.add_task(run_bot, connection, session_id)

    handler = _get_webrtc_handler()
    answer = await handler.handle_web_request(
        request=SmallWebRTCRequest.from_dict(body),
        webrtc_connection_callback=connection_callback)
    if answer is None:
        return JSONResponse({"error": "WebRTC offer failed"}, status_code=500)
    return {**answer, "session_id": session_id}


@app.patch("/api/offer")
async def ice_candidate(request: Request):
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCPatchRequest
    body = await request.json()
    await _get_webrtc_handler().handle_patch_request(
        SmallWebRTCPatchRequest(**body))
    return {"status": "success"}


@app.get("/api/health")
def health():
    return {"ok": True}


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    print("CareVoice Clinic — http://localhost:8000")
    print("Voice mode:", "ready" if VOICE_READY else "demo (text) — set GEMINI_API_KEY + ELEVENLABS_API_KEY in .env")
    uvicorn.run(app, host="0.0.0.0", port=8000)
