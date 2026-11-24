"""Clinic data store: SQLite, seeded with mock doctors and slots.

One file, stdlib only. The same store backs the Pipecat tools, the
REST API and the text demo agent, so a booking made by voice shows up
on the dashboard immediately.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic.db"

DOCTORS = [
    # (name, speciality, qualification, experience_years, fee_inr)
    ("Dr. Ananya Sharma", "General Physician", "MBBS, MD (Internal Medicine)", 12, 500),
    ("Dr. Rohan Mehta", "Cardiologist", "MBBS, DM (Cardiology)", 15, 900),
    ("Dr. Priya Nair", "Dermatologist", "MBBS, MD (Dermatology)", 10, 700),
    ("Dr. Arjun Patel", "Orthopaedic", "MBBS, MS (Orthopaedics)", 14, 800),
    ("Dr. Kavya Reddy", "Paediatrician", "MBBS, MD (Paediatrics)", 9, 600),
    ("Dr. Vikram Singh", "Dentist", "BDS, MDS", 11, 650),
]

SLOT_TIMES = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30",
              "14:00", "14:30", "15:00", "15:30", "16:00", "16:30"]

# A few slots start out booked so the dashboard looks like a real clinic.
PRE_BOOKED = {  # (doctor_id, day_offset, time): (phone, patient)
    (1, 1, "10:00"): ("9876543210", "Aarav Gupta"),
    (2, 1, "09:30"): ("9812345678", "Meera Iyer"),
    (3, 2, "11:00"): ("9900112233", "Kabir Shah"),
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(reset: bool = False) -> None:
    """Create tables and seed doctors/slots. Dates are relative to today
    (tomorrow, +2, +3 days) so the demo always has future slots."""
    if reset and DB_PATH.exists():
        DB_PATH.unlink()
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS doctors (
            id INTEGER PRIMARY KEY, name TEXT UNIQUE, speciality TEXT,
            qualification TEXT, experience_years INTEGER, fee_inr INTEGER);
        CREATE TABLE IF NOT EXISTS slots (
            id INTEGER PRIMARY KEY, doctor_id INTEGER, date TEXT, time TEXT,
            status TEXT DEFAULT 'free',
            UNIQUE(doctor_id, date, time));
        CREATE TABLE IF NOT EXISTS appointments (
            id TEXT PRIMARY KEY, doctor_id INTEGER, date TEXT, time TEXT,
            phone TEXT, patient_name TEXT, status TEXT DEFAULT 'booked',
            created_at TEXT DEFAULT (datetime('now')));
    """)
    if conn.execute("SELECT COUNT(*) FROM doctors").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO doctors (name, speciality, qualification, experience_years, fee_inr)"
            " VALUES (?,?,?,?,?)", DOCTORS)
        days = [date.today() + timedelta(days=i) for i in (1, 2, 3)]
        for idx, _doc in enumerate(DOCTORS, start=1):
            for offset, day in enumerate(days, start=1):
                for t in SLOT_TIMES:
                    conn.execute(
                        "INSERT OR IGNORE INTO slots (doctor_id, date, time) VALUES (?,?,?)",
                        (idx, day.isoformat(), t))
        for (didx, offset, t), (phone, patient) in PRE_BOOKED.items():
            day = date.today() + timedelta(days=offset)
            conn.execute("UPDATE slots SET status='booked' WHERE doctor_id=? AND date=? AND time=?",
                         (didx, day.isoformat(), t))
            conn.execute(
                "INSERT OR IGNORE INTO appointments (id, doctor_id, date, time, phone, patient_name)"
                " VALUES (?,?,?,?,?,?)",
                (f"APT-{didx}{offset}{t.replace(':', '')}", didx, day.isoformat(), t, phone, patient))
    conn.commit()
    conn.close()


def doctors_with_slots(day: str | None = None) -> list[dict]:
    conn = _connect()
    rows = conn.execute("SELECT * FROM doctors ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Free slots grouped by date; free_slots = tomorrow (the default day
        # the agent offers), so the dashboard never shows duplicate times.
        by_date: dict[str, list[str]] = {}
        for s in conn.execute(
                "SELECT date, time FROM slots WHERE doctor_id=? AND status='free'"
                " ORDER BY date, time", (r["id"],)).fetchall():
            by_date.setdefault(s["date"], []).append(s["time"])
        d["slots_by_date"] = by_date
        default_day = day or (date.today() + timedelta(days=1)).isoformat()
        d["free_slots"] = by_date.get(default_day, [])
        d["next_free"] = d["free_slots"][:6]
        out.append(d)
    conn.close()
    return out


def list_appointments(phone: str | None = None) -> list[dict]:
    conn = _connect()
    q = ("SELECT a.*, d.name AS doctor, d.speciality FROM appointments a"
         " JOIN doctors d ON d.id=a.doctor_id")
    args: list = []
    if phone:
        q += " WHERE a.phone=?"; args.append(phone)
    q += " ORDER BY a.date, a.time"
    rows = [dict(r) for r in conn.execute(q, args).fetchall()]
    conn.close()
    return rows


def find_doctor(query: str | None) -> dict | None:
    """Match by name fragment or speciality, e.g. 'Sharma', 'cardio'."""
    if not query:
        return None
    q = query.strip().lower()
    conn = _connect()
    rows = [dict(r) for r in conn.execute("SELECT * FROM doctors").fetchall()]
    conn.close()
    for r in rows:
        if q in r["name"].lower() or q in r["speciality"].lower():
            return r
    # last-name only, e.g. "doctor Sharma"
    last = q.split()[-1]
    for r in rows:
        if last in r["name"].lower():
            return r
    return None


def normalize_phone(raw: str | None) -> str | None:
    """'91 91538 96500' / '+91-9153896500' / spoken digits -> 10-digit string."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def free_slot(doctor_id: int, day: str, time: str) -> bool:
    conn = _connect()
    row = conn.execute(
        "SELECT status FROM slots WHERE doctor_id=? AND date=? AND time=?",
        (doctor_id, day, time)).fetchone()
    conn.close()
    return bool(row and row["status"] == "free")


def book_slot(doctor_id: int, day: str, time: str, phone: str, patient: str | None) -> str:
    apt_id = f"APT-{doctor_id}-{day.replace('-', '')}-{time.replace(':', '')}-{phone[-4:]}"
    conn = _connect()
    conn.execute("UPDATE slots SET status='booked' WHERE doctor_id=? AND date=? AND time=?",
                 (doctor_id, day, time))
    conn.execute(
        "INSERT INTO appointments (id, doctor_id, date, time, phone, patient_name)"
        " VALUES (?,?,?,?,?,?)", (apt_id, doctor_id, day, time, phone, patient or "Caller"))
    conn.commit(); conn.close()
    return apt_id


def cancel_appointment(apt_id: str) -> bool:
    conn = _connect()
    row = conn.execute("SELECT doctor_id, date, time FROM appointments WHERE id=? AND status='booked'",
                       (apt_id,)).fetchone()
    if not row:
        conn.close(); return False
    conn.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
    conn.execute("UPDATE slots SET status='free' WHERE doctor_id=? AND date=? AND time=?",
                 (row["doctor_id"], row["date"], row["time"]))
    conn.commit(); conn.close()
    return True
