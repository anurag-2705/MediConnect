"""Tell waiting patients when a time with their doctor opens up."""

from .db import MongoStore
from .models import User, WaitlistEntry
from .notifications import add_notice

NOTIFY_LIMIT = 5  # Only the longest-waiting patients are told, so one cancellation does not trigger a stampede.


def notify_waitlist(db: MongoStore, doctor_id: int, mode: str) -> int:
    """Notify the oldest waiting patients and remove them from the list. Returns how many were told."""
    entries = db.find(WaitlistEntry, {"doctor_id": doctor_id, "mode": mode}, sort=[("created_at", 1)], limit=NOTIFY_LIMIT)
    if not entries:
        return 0
    doctor = db.get(User, doctor_id)
    name = doctor.full_name if doctor else "your doctor"
    for entry in entries:
        add_notice(db, entry.patient_id, "waitlist_slot_open",
                   f"A {mode} time just opened with {name}. Open their profile to book it before it is taken.")
        db.delete(entry)
    return len(entries)
