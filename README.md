# CareVoice Clinic — Voice AI Doctor Appointment Assistant

SDE Intern Round 1 assignment, NexovAI. A caller speaks to an AI receptionist
("Maya"), who checks a doctor's availability, books an appointment, or cancels
one — identifying the caller **only by the phone number they say out loud**.
A dashboard runs the call and shows doctors, slots, and appointments live.

## Pipeline (short version)

Browser mic → WebRTC (SmallWebRTC, peer-to-peer, no Daily/Twilio) →
Pipecat pipeline: ElevenLabs STT → Gemini LLM → ElevenLabs TTS → caller.
Silero VAD detects when the caller starts/stops speaking. The LLM calls three
tools — `check_doctor_availability`, `book_appointment`, `cancel_appointment` —
backed by a SQLite store. A longer, in-my-own-words write-up is in
[`PIPELINE.md`](PIPELINE.md).

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add GEMINI_API_KEY and ELEVENLABS_API_KEY
python -m backend.server
# open http://localhost:8000, click "Try Call"
```

**No keys yet?** The app still runs in demo mode: the phone uses your
browser's speech recognition (or the type box) and the same booking tools
over `/api/chat`, so the full booking flow can be tested and recorded.
Voice mode needs both keys; the dashboard pill shows which mode is live.

Gemini free tier: get a key at https://aistudio.google.com/apikey.
ElevenLabs: https://elevenlabs.io — the default voice is Rachel
(`21m00Tcm4TlvDq8ikWAM`); change `ELEVENLABS_VOICE_ID` in `.env` to use another.

## What the agent does

1. Asks for the caller's 10-digit phone number, reads it back to confirm.
2. Asks for doctor/speciality and day, calls `check_doctor_availability`,
   reads the free slots out loud.
3. On the caller's pick, reads back phone + doctor + date + time, and only
   after a "yes" calls `book_appointment`.
4. To cancel, asks for the phone number, reads it back, confirms, then calls
   `cancel_appointment` (if several appointments exist, it asks which one).
5. Reads the appointment ID back once, clearly.

## Project layout

```
backend/
  server.py      FastAPI: dashboard, REST API, /api/chat, /api/offer (WebRTC)
  agent.py       Pipecat pipeline: STT -> LLM -> TTS, Silero VAD, 3 tools
  tools.py       check_doctor_availability / book_appointment / cancel_appointment
  store.py       SQLite store: 6 mock doctors, slots for the next 3 days
  chat_agent.py  Text demo agent (same tools, no keys needed)
  hub.py         Live transcript + booking events per call
frontend/
  index.html · styles.css · app.js   Dashboard + phone-style call screen
```

## API

| Endpoint | What it does |
|---|---|
| `GET /api/status` | Voice ready? LLM, STT/TTS, VAD |
| `GET /api/doctors` | Doctors with free slots |
| `GET /api/appointments` | Appointments (optional `?phone=`) |
| `GET /api/availability?doctor=&date=` | Free slots |
| `POST /api/chat` | Text turn with the demo agent |
| `GET /api/feed?session_id=` | Live transcript + booking events |
| `POST /api/offer` | WebRTC offer → Pipecat answer (voice call) |
| `POST /api/reset` | Re-seed the demo data |

## Test the booking flow without a call

```bash
python -m backend.server &
curl -s localhost:8000/api/doctors | head -c 400
curl -s -X POST localhost:8000/api/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo1","message":"Hello"}'
# then: phone number -> doctor -> time -> "yes"
curl -s localhost:8000/api/appointments
```

## Notes / limits

- Mock data only: 6 doctors, slots 09:00–16:30 for the next 3 days, SQLite
  file at `data/clinic.db` (re-seed with `POST /api/reset`).
- Phone numbers are stored as 10 digits; the agent never asks for OTP/login.
- Pipecat version pinned to 1.17.0 (SmallWebRTC + `PipelineWorker` API);
  if you upgrade Pipecat, check the transport/LLM-context API first.
