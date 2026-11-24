"""The three tools the voice agent can call.

These are plain Python functions over the SQLite store. Pipecat
registers them for the LLM (agent.py); the REST API and the text demo
agent call the same functions, so every path books the same way.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from . import store

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"])}


def parse_date(text: str | None) -> str | None:
    """'tomorrow' / '25 November' / '2026-11-25' / 'Friday' -> ISO date."""
    if not text:
        return None
    t = text.strip().lower()
    today = date.today()
    if t in ("today",):
        return today.isoformat()
    if t in ("tomorrow", "tmrw"):
        return (today + timedelta(days=1)).isoformat()
    if t in ("day after tomorrow",):
        return (today + timedelta(days=2)).isoformat()
    if t in WEEKDAYS:
        delta = (WEEKDAYS.index(t) - today.weekday()) % 7 or 7
        return (today + timedelta(days=delta)).isoformat()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{4}))?", t)
    if m and m.group(2) in MONTHS:
        year = int(m.group(3)) if m.group(3) else today.year
        return date(year, MONTHS[m.group(2)], int(m.group(1))).isoformat()
    return None


def parse_time(text: str | None) -> str | None:
    """'10 am' / '10:30' / '2 pm' -> '10:00' / '14:00' (24h, :00/:30)."""
    if not text:
        return None
    t = text.strip().lower().replace(".", "")
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m:
        return None
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if meridiem == "pm" and hour < 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    if minute not in (0, 30):
        minute = 0 if minute < 15 else 30
    return f"{hour:02d}:{minute:02d}"


def check_doctor_availability(doctor: str | None = None, date_text: str | None = None) -> dict:
    """Tool 1 — available slots for a doctor (and/or date)."""
    day = parse_date(date_text) or (date.today() + timedelta(days=1)).isoformat()
    doctors = store.doctors_with_slots(day)
    if doctor:
        match = store.find_doctor(doctor)
        if not match:
            return {"ok": False, "error": f"No doctor matches '{doctor}'.",
                    "doctors": [d["name"] + " — " + d["speciality"] for d in doctors]}
        doctors = [d for d in doctors if d["id"] == match["id"]]
    return {"ok": True, "date": day,
            "doctors": [{"name": d["name"], "speciality": d["speciality"],
                         "fee_inr": d["fee_inr"], "free_slots": d["free_slots"]}
                        for d in doctors]}


def book_appointment(phone: str, doctor: str | None = None, date_text: str | None = None,
                     time_text: str | None = None, patient_name: str | None = None) -> dict:
    """Tool 2 — book a slot. Caller is identified only by spoken phone."""
    phone10 = store.normalize_phone(phone)
    if not phone10:
        return {"ok": False, "error": "That phone number doesn't look like 10 digits. Please say it again, digit by digit."}
    match = store.find_doctor(doctor)
    if not match:
        return {"ok": False, "error": "Which doctor? Please say the doctor's name or speciality."}
    day = parse_date(date_text) or (date.today() + timedelta(days=1)).isoformat()
    slot = parse_time(time_text)
    if not slot:
        return {"ok": False, "error": "Which time slot? Please say a time, for example 10 AM."}
    if not store.free_slot(match["id"], day, slot):
        avail = check_doctor_availability(match["name"], day)
        return {"ok": False, "error": f"{slot} on {day} with {match['name']} is already taken.",
                "free_slots": avail["doctors"][0]["free_slots"] if avail["doctors"] else []}
    apt_id = store.book_slot(match["id"], day, slot, phone10, patient_name)
    return {"ok": True, "appointment_id": apt_id, "doctor": match["name"],
            "speciality": match["speciality"], "date": day, "time": slot,
            "phone": phone10, "fee_inr": match["fee_inr"],
            "message": f"Booked: {match['name']} on {day} at {slot}. Appointment ID {apt_id}."}


def cancel_appointment(phone: str, doctor: str | None = None,
                       date_text: str | None = None) -> dict:
    """Tool 3 — cancel the caller's appointment, found by spoken phone."""
    phone10 = store.normalize_phone(phone)
    if not phone10:
        return {"ok": False, "error": "That phone number doesn't look like 10 digits. Please say it again, digit by digit."}
    day = parse_date(date_text) if date_text else None
    appts = [a for a in store.list_appointments(phone10) if a["status"] == "booked"]
    if doctor:
        match = store.find_doctor(doctor)
        if match:
            appts = [a for a in appts if a["doctor"] == match["name"]]
    if day:
        appts = [a for a in appts if a["date"] == day]
    if not appts:
        return {"ok": False, "error": "No booked appointment found for that phone number."}
    if len(appts) > 1:
        return {"ok": False, "error": "More than one appointment found. Please say the doctor or date.",
                "appointments": [{"id": a["id"], "doctor": a["doctor"], "date": a["date"], "time": a["time"]}
                                 for a in appts]}
    a = appts[0]
    store.cancel_appointment(a["id"])
    return {"ok": True, "appointment_id": a["id"], "doctor": a["doctor"],
            "date": a["date"], "time": a["time"], "phone": phone10,
            "message": f"Cancelled: {a['doctor']} on {a['date']} at {a['time']}."}
