"""Send appointment reminders through the existing notice and email pipeline."""

from datetime import timedelta

from .db import MongoStore, as_utc, utcnow
from .models import Appointment, Slot, User
from .notifications import add_notice

# (flag stored on the appointment, how long before the start, patient wording, doctor wording)
WINDOWS = (
    ("reminder_24h_sent_at", timedelta(hours=24), "Reminder: your appointment is within the next 24 hours.",
     "Reminder: you have an appointment within the next 24 hours."),
    ("reminder_1h_sent_at", timedelta(hours=1), "Reminder: your appointment starts within the hour.",
     "Reminder: an appointment starts within the hour."),
)


def send_due_reminders(db: MongoStore, *, limit: int = 200) -> int:
    """Create each due reminder exactly once. Safe to call repeatedly and from several servers."""
    now = utcnow()
    sent = 0
    for flag, lead, patient_text, doctor_text in WINDOWS:
        slots = db.find(Slot, {"status": "booked", "start_at": {"$gt": now, "$lte": now + lead}},
                        sort=[("start_at", 1)], limit=limit)
        for slot in slots:
            appointment = db.first(Appointment, {"slot_id": slot.id, "status": "confirmed", flag: None})
            if not appointment:
                continue
            # Claiming the flag first means a crash can skip one reminder but never send two.
            claimed = db.update(Appointment, {"_id": appointment.id, "status": "confirmed", flag: None, "slot_id": slot.id},
                                {flag: now})
            if not claimed:
                continue
            # Booked too close to the start for this reminder to be useful (for example, paid an hour ago).
            if as_utc(slot.start_at) - as_utc(claimed.paid_at or claimed.created_at) <= lead:
                continue
            add_notice(db, claimed.patient_id, "appointment_reminder", patient_text, appointment_id=claimed.id)
            add_notice(db, claimed.doctor_id, "doctor_appointment_reminder", doctor_text, appointment_id=claimed.id)
            sent += 1
    return sent
