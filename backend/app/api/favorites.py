from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import DuplicateKeyError

from ..db import MongoStore, as_utc, get_store, utcnow
from ..holds import expire_doctor_holds
from ..models import DoctorProfile, Favorite, Slot, User, WaitlistEntry
from ..schemas import WaitlistInput
from ..security import require_role
from .common import doctor_dict, visible_doctor_user

router = APIRouter(tags=["favorites and waitlist"])
MAX_FAVORITES = 100
MAX_WAITLIST = 20


def bookable_profile(db: MongoStore, doctor_id: int) -> tuple[DoctorProfile, User]:
    profile = db.get(DoctorProfile, doctor_id)
    user = visible_doctor_user(db, profile) if profile and profile.verification_status == "published" else None
    if not user:
        raise HTTPException(404, "Doctor not found")
    return profile, user


@router.get("/favorites")
def list_favorites(user: User = Depends(require_role("patient")), db: MongoStore = Depends(get_store)):
    result = []
    for row in db.find(Favorite, {"patient_id": user.id}, sort=[("created_at", -1), ("_id", -1)]):
        profile = db.get(DoctorProfile, row.doctor_id)
        doctor = visible_doctor_user(db, profile) if profile and profile.verification_status == "published" else None
        if doctor:  # Doctors who were suspended or unpublished simply drop out of the list.
            result.append(doctor_dict(db, profile, doctor))
    return result


@router.get("/favorites/ids")
def favorite_ids(user: User = Depends(require_role("patient")), db: MongoStore = Depends(get_store)):
    return [row.doctor_id for row in db.find(Favorite, {"patient_id": user.id})]


@router.put("/favorites/{doctor_id}")
def add_favorite(doctor_id: int, user: User = Depends(require_role("patient")), db: MongoStore = Depends(get_store)):
    bookable_profile(db, doctor_id)
    if db.first(Favorite, {"patient_id": user.id, "doctor_id": doctor_id}):
        return {"favorite": True}
    if len(db.find(Favorite, {"patient_id": user.id}, limit=MAX_FAVORITES)) >= MAX_FAVORITES:
        raise HTTPException(409, f"You can save up to {MAX_FAVORITES} doctors")
    try:
        db.insert(Favorite(patient_id=user.id, doctor_id=doctor_id))
    except DuplicateKeyError:
        pass
    return {"favorite": True}


@router.delete("/favorites/{doctor_id}")
def remove_favorite(doctor_id: int, user: User = Depends(require_role("patient")), db: MongoStore = Depends(get_store)):
    db.delete_one(Favorite, {"patient_id": user.id, "doctor_id": doctor_id})
    return {"favorite": False}


@router.get("/waitlist")
def my_waitlist(user: User = Depends(require_role("patient")), db: MongoStore = Depends(get_store)):
    result = []
    for entry in db.find(WaitlistEntry, {"patient_id": user.id}, sort=[("created_at", 1)]):
        doctor = db.get(User, entry.doctor_id)
        if doctor:
            result.append({"id": entry.id, "doctor_id": entry.doctor_id, "doctor_name": doctor.full_name,
                           "mode": entry.mode, "created_at": entry.created_at})
    return result


@router.post("/doctors/{doctor_id}/waitlist", status_code=201)
def join_waitlist(doctor_id: int, data: WaitlistInput, user: User = Depends(require_role("patient")),
                  db: MongoStore = Depends(get_store)):
    bookable_profile(db, doctor_id)
    expire_doctor_holds(db, doctor_id)
    now = utcnow()
    open_slots = db.find(Slot, {"doctor_id": doctor_id, "status": "available", "mode": data.mode, "start_at": {"$gt": now}}, limit=1)
    if open_slots:
        raise HTTPException(409, "Times are available for this doctor right now. Book one instead of waiting.")
    existing = db.first(WaitlistEntry, {"patient_id": user.id, "doctor_id": doctor_id, "mode": data.mode})
    if existing:
        return {"id": existing.id, "doctor_id": doctor_id, "mode": data.mode}
    if len(db.find(WaitlistEntry, {"patient_id": user.id}, limit=MAX_WAITLIST)) >= MAX_WAITLIST:
        raise HTTPException(409, f"You can wait on up to {MAX_WAITLIST} doctors at once")
    try:
        entry = db.insert(WaitlistEntry(patient_id=user.id, doctor_id=doctor_id, mode=data.mode))
    except DuplicateKeyError:
        entry = db.first(WaitlistEntry, {"patient_id": user.id, "doctor_id": doctor_id, "mode": data.mode})
    return {"id": entry.id, "doctor_id": doctor_id, "mode": data.mode}


@router.delete("/doctors/{doctor_id}/waitlist")
def leave_waitlist(doctor_id: int, mode: Literal["clinic", "online"] = Query(), user: User = Depends(require_role("patient")),
                   db: MongoStore = Depends(get_store)):
    db.delete_one(WaitlistEntry, {"patient_id": user.id, "doctor_id": doctor_id, "mode": mode})
    return {"ok": True}
