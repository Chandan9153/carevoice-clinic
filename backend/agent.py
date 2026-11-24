"""Pipecat voice pipeline: caller audio -> ElevenLabs STT -> Gemini LLM
-> ElevenLabs TTS -> caller audio, with Silero VAD.

Transport is SmallWebRTC (peer-to-peer WebRTC, no Daily/Twilio needed):
the browser posts its SDP offer to /api/offer and gets an answer back.

The LLM gets three tools (FunctionSchema, handler attached) — the same
functions in tools.py that the REST API and text demo use.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from loguru import logger

from . import store, tools
from .hub import hub

load_dotenv()

SYSTEM_PROMPT = """You are Maya, the receptionist for CareVoice Clinic, a doctor
appointment booking assistant. Speak naturally, briefly, in simple English.
Never invent a doctor, slot, price or appointment.

Flow:
1. Greet the caller and ask for their 10-digit phone number. The caller is
   identified ONLY by the phone number they say out loud — no login, no OTP.
2. Ask which doctor or speciality they need, and which day (tomorrow is the
   default). Call check_doctor_availability and read out the free slots.
3. When they pick a slot, read back the phone number, doctor, date and time
   and ask them to confirm. Only after they say yes, call book_appointment.
4. To cancel, ask for the phone number, read it back, confirm, then call
   cancel_appointment. If several appointments exist, ask which one.
5. After booking or cancelling, read the appointment ID and the details once,
   clearly, and ask if they need anything else.

If a slot is taken, offer the nearest free slots instead. If you do not
understand the phone number, ask them to say it again, digit by digit.
Keep every reply to one or two short sentences.
"""


def _tool_schemas(session_id: str):
    from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
    from pipecat.services.llm_service import FunctionCallParams

    async def _availability(params: FunctionCallParams):
        result = tools.check_doctor_availability(
            doctor=params.arguments.get("doctor"),
            date_text=params.arguments.get("date"))
        await params.result_callback(result)

    async def _book(params: FunctionCallParams):
        result = tools.book_appointment(
            phone=params.arguments.get("phone", ""),
            doctor=params.arguments.get("doctor"),
            date_text=params.arguments.get("date"),
            time_text=params.arguments.get("time"),
            patient_name=params.arguments.get("patient_name"))
        if result.get("ok"):
            hub.event(session_id, "booked", result)
        await params.result_callback(result)

    async def _cancel(params: FunctionCallParams):
        result = tools.cancel_appointment(
            phone=params.arguments.get("phone", ""),
            doctor=params.arguments.get("doctor"),
            date_text=params.arguments.get("date"))
        if result.get("ok"):
            hub.event(session_id, "cancelled", result)
        await params.result_callback(result)

    return [
        FunctionSchema(
            name="check_doctor_availability",
            description="Return the free appointment slots for a doctor (by name or speciality) and/or date.",
            properties={
                "doctor": {"type": "string", "description": "Doctor name or speciality, e.g. 'Dr. Ananya Sharma' or 'Cardiologist'"},
                "date": {"type": "string", "description": "Day, e.g. 'tomorrow', '25 November', '2026-11-25'"},
            },
            required=[],
            handler=_availability,
        ),
        FunctionSchema(
            name="book_appointment",
            description="Book a free slot. Call ONLY after reading the phone number, doctor, date and time back to the caller and getting a yes.",
            properties={
                "phone": {"type": "string", "description": "Caller's 10-digit phone number, digits only"},
                "doctor": {"type": "string", "description": "Doctor name or speciality"},
                "date": {"type": "string", "description": "Day, e.g. 'tomorrow', '25 November'"},
                "time": {"type": "string", "description": "Slot time, e.g. '10 AM', '10:30'"},
                "patient_name": {"type": "string", "description": "Patient name if the caller gives one"},
            },
            required=["phone", "doctor", "date", "time"],
            handler=_book,
        ),
        FunctionSchema(
            name="cancel_appointment",
            description="Cancel the caller's booked appointment, found by the phone number they spoke. Call ONLY after reading the phone number back and getting a yes.",
            properties={
                "phone": {"type": "string", "description": "Caller's 10-digit phone number, digits only"},
                "doctor": {"type": "string", "description": "Doctor name or speciality, if the caller gives one"},
                "date": {"type": "string", "description": "Day of the appointment, if the caller gives one"},
            },
            required=["phone"],
            handler=_cancel,
        ),
    ]


async def run_bot(webrtc_connection, session_id: str):
    """Build and run one Pipecat pipeline for one WebRTC call."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import (
        BotStoppedSpeakingFrame, EndFrame, LLMTextFrame,
        TranscriptionFrame, UserStartedSpeakingFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair, LLMUserAggregatorParams,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.elevenlabs.stt import ElevenLabsSTTService
    from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
    from pipecat.services.google.llm import GoogleLLMService
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.workers.runner import WorkerRunner

    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    eleven_key = os.getenv("ELEVENLABS_API_KEY")
    if not gemini_key or not eleven_key:
        raise RuntimeError("GEMINI_API_KEY and ELEVENLABS_API_KEY are required for voice calls.")

    store.init_db()

    import aiohttp

    # One shared buffer: the logger after STT sees caller transcripts, the
    # logger after the LLM sees agent text; either can flush the agent line.
    shared = {"assistant": ""}

    def flush_assistant():
        text = shared["assistant"].strip()
        shared["assistant"] = ""
        if text:
            hub.add(session_id, "agent", text)

    class TranscriptLogger(FrameProcessor):
        """Copies user transcripts and agent replies into the dashboard hub."""
        async def process_frame(self, frame, direction):
            await super().process_frame(frame, direction)
            if isinstance(frame, TranscriptionFrame):
                flush_assistant()
                hub.add(session_id, "caller", frame.text)
            elif isinstance(frame, LLMTextFrame):
                shared["assistant"] += frame.text
            elif isinstance(frame, (BotStoppedSpeakingFrame, UserStartedSpeakingFrame, EndFrame)):
                flush_assistant()
            await self.push_frame(frame, direction)

    transport = SmallWebRTCTransport(
        webrtc_connection=webrtc_connection,
        params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    )
    http_session = aiohttp.ClientSession()
    stt = ElevenLabsSTTService(api_key=eleven_key, aiohttp_session=http_session)
    llm = GoogleLLMService(
        api_key=gemini_key,
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        system_instruction=SYSTEM_PROMPT,
    )
    tts = ElevenLabsTTSService(
        api_key=eleven_key,
        voice_id=os.getenv("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM"),
        model=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
    )

    messages = [{"role": "user", "content": "The caller has just joined. Greet them briefly and ask for their phone number."}]
    context = LLMContext(messages, tools=_tool_schemas(session_id))
    aggregators = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),  # bonus: Silero VAD
        ),
    )
    pipeline = Pipeline([
        transport.input(),
        stt,
        TranscriptLogger(),      # sees caller TranscriptionFrames
        aggregators.user(),
        llm,
        TranscriptLogger(),      # sees agent LLMTextFrames
        tts,
        transport.output(),
        aggregators.assistant(),
    ])
    worker = PipelineWorker(pipeline, params=PipelineParams(
        audio_in_sample_rate=16000, audio_out_sample_rate=16000))
    runner = WorkerRunner()
    await runner.add_workers(worker)
    hub.event(session_id, "call_started", {"session_id": session_id})
    try:
        await runner.run()
    finally:
        flush_assistant()
        await http_session.close()
        hub.event(session_id, "call_ended", {"session_id": session_id})
