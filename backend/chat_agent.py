"""Text demo agent: the same booking flow as the voice agent, over text.

This is the fallback path for testing without API keys and for the
dashboard's "type instead of speaking" box. It is a small deterministic
state machine over the SAME tools (tools.py) the Pipecat LLM calls, so a
booking made here is identical to one made by voice. It is not the
Pipecat pipeline — that lives in agent.py and needs Gemini + ElevenLabs.
"""
from __future__ import annotations

import re

from . import store, tools
from .hub import hub

NUMBER_WORDS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3",
                "four": "4", "five": "5", "six": "6", "seven": "7",
                "eight": "8", "nine": "9"}

SPECIALITIES = {
    "general": "General Physician", "physician": "General Physician",
    "fever": "General Physician", "cardio": "Cardiologist", "heart": "Cardiologist",
    "skin": "Dermatologist", "derma": "Dermatologist",
    "ortho": "Orthopaedic", "bone": "Orthopaedic", "knee": "Orthopaedic",
    "child": "Paediatrician", "paediatric": "Paediatrician", "pediatric": "Paediatrician",
    "dentist": "Dentist", "teeth": "Dentist", "tooth": "Dentist",
}

_sessions: dict[str, dict] = {}


def _digits_from_words(text: str) -> str:
    words = re.findall(r"[a-z]+|\d+", text.lower())
    out, i = "", 0
    while i < len(words):
        w = words[i]
        if w in ("double", "triple") and i + 1 < len(words) and words[i + 1] in NUMBER_WORDS:
            out += NUMBER_WORDS[words[i + 1]] * (2 if w == "double" else 3)
            i += 2
        elif w in NUMBER_WORDS:
            out += NUMBER_WORDS[w]; i += 1
        elif w.isdigit():
            out += w; i += 1
        else:
            i += 1
    return out


def _find_phone(text: str) -> str | None:
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 10:
        return store.normalize_phone(digits[-10:] if len(digits) > 10 else digits)
    return store.normalize_phone(_digits_from_words(text))


def _find_doctor(text: str) -> str | None:
    low = text.lower()
    for name in ("sharma", "mehta", "nair", "patel", "reddy", "singh"):
        if name in low:
            return name
    for key, spec in SPECIALITIES.items():
        if key in low:
            return spec
    return None


def _find_time(text: str) -> str | None:
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text.lower())
    if m:
        return tools.parse_time(m.group(0))
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    return tools.parse_time(m.group(0)) if m else None


def reply(session_id: str, message: str) -> dict:
    """One turn of the text agent. Returns {reply, appointment?, cancelled?}."""
    s = _sessions.setdefault(session_id, {"state": "greet"})
    text = (message or "").strip()
    low = text.lower()
    hub.add(session_id, "caller", text)

    def say(reply_text: str, **extra):
        hub.add(session_id, "agent", reply_text)
        return {"reply": reply_text, **extra}

    if low in ("hi", "hello", "hey", "") and s["state"] == "greet":
        s["state"] = "need_phone"
        return say("Hello, this is CareVoice Clinic. May I have your 10-digit phone number, please?")

    if "cancel" in low and s["state"] in ("greet", "need_phone"):
        s["state"] = "cancel_phone"
        return say("Sure, I can cancel it. Please say your 10-digit phone number.")

    if s["state"] == "cancel_phone":
        phone = _find_phone(text)
        if not phone:
            return say("I didn't catch all 10 digits. Could you say your phone number again, digit by digit?")
        s["phone"] = phone
        result = tools.cancel_appointment(phone)
        if result.get("ok"):
            hub.event(session_id, "cancelled", result)
            s["state"] = "done"
            return say(f"Done. Your appointment with {result['doctor']} on {result['date']} at {result['time']} is cancelled. Appointment ID was {result['appointment_id']}. Is there anything else I can help with?",
                       cancelled=result)
        if result.get("appointments"):
            opts = "; ".join(f"{a['doctor']} on {a['date']} at {a['time']}" for a in result["appointments"])
            s["state"] = "cancel_pick"
            return say(f"I found more than one appointment for {phone}: {opts}. Which one should I cancel — please say the doctor or the date.")
        return say("I couldn't find a booked appointment for that number. Please check the number and say it again.")

    if s["state"] == "cancel_pick":
        result = tools.cancel_appointment(s["phone"], doctor=_find_doctor(text), date_text=text)
        if result.get("ok"):
            hub.event(session_id, "cancelled", result)
            s["state"] = "done"
            return say(f"Done. Your appointment with {result['doctor']} on {result['date']} at {result['time']} is cancelled.",
                       cancelled=result)
        return say("I still couldn't pin down which one. Please say the doctor's name or the date.")

    if s["state"] in ("greet", "need_phone"):
        phone = _find_phone(text)
        if not phone:
            s["state"] = "need_phone"
            return say("I didn't catch all 10 digits. Could you say your phone number again, digit by digit?")
        s["phone"] = phone
        s["state"] = "need_doctor"
        return say(f"Thanks. Just to confirm, your number is {phone[:5]} {phone[5:]}, is that right? And which doctor or speciality do you need — for example, General Physician, Cardiologist, or Dermatologist?")

    if s["state"] == "need_doctor":
        if low.strip() in ("yes", "yeah", "correct", "that's right", "right"):
            return say("Great. Which doctor or speciality do you need?")
        doctor = _find_doctor(text)
        if doctor:
            s["doctor"] = doctor
            s["state"] = "need_slot"
            avail = tools.check_doctor_availability(doctor)
            if not avail.get("ok"):
                return say(avail.get("error", "I couldn't find that doctor."))
            d = avail["doctors"][0]
            slots = ", ".join(d["free_slots"][:8])
            s["date"] = avail["date"]
            return say(f"{d['name']}, {d['speciality']}, fee ₹{d['fee_inr']}. Free slots on {avail['date']}: {slots}. Which time works for you?")
        return say("Which doctor or speciality do you need — for example, General Physician, Cardiologist, or Dermatologist?")

    if s["state"] == "need_slot":
        day = tools.parse_date(text)
        if day:
            s["date"] = day
        slot = _find_time(text)
        if slot:
            s["time"] = slot
            s["state"] = "confirm"
            doc = store.find_doctor(s["doctor"])
            return say(f"Just to confirm: {doc['name']} on {s['date']} at {slot}, for phone number {s['phone'][:5]} {s['phone'][5:]}. Shall I book it?")
        return say("Which time slot would you like? For example, 10 AM or 2:30 PM.")

    if s["state"] == "confirm":
        if low.strip() in ("yes", "yeah", "confirm", "book it", "please book", "ok", "okay"):
            result = tools.book_appointment(s["phone"], doctor=s["doctor"],
                                            date_text=s["date"], time_text=s["time"])
            if result.get("ok"):
                hub.event(session_id, "booked", result)
                s["state"] = "done"
                return say(f"Booked. {result['doctor']} on {result['date']} at {result['time']}. Your appointment ID is {result['appointment_id']}. Please arrive 10 minutes early. Is there anything else I can help with?",
                           appointment=result)
            s["state"] = "need_slot"
            free = ", ".join(result.get("free_slots", [])[:8])
            return say(f"{result.get('error', 'That slot is not available.')} Free slots: {free}. Which one would you like?")
        if "no" in low:
            s["state"] = "need_slot"
            return say("No problem. Which time would you prefer instead?")
        return say("Shall I book it — yes or no?")

    # done / anything else
    if "cancel" in low:
        s["state"] = "cancel_phone"
        return say("Sure. Please say your 10-digit phone number for the appointment to cancel.")
    if "book" in low or "appointment" in low:
        s["state"] = "need_doctor"
        return say("Of course. Which doctor or speciality do you need?")
    return say("Thanks for calling CareVoice Clinic. Would you like to book a new appointment or cancel one?")
