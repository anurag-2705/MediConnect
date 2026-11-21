"""Tests for the bug fixes and features added on top of the original booking flow."""

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import seed
from app import contact_verification as otp, payments
from app.db import utcnow
from app.main import app
from app.models import Appointment, DoctorProfile, Favorite, Notification, Review, Slot, User, WaitlistEntry
from app.reminders import send_due_reminders
from test_flows import doctor, login, patient, setup, verify_contacts  # noqa: F401  (setup is a fixture)

SIGNED = {"razorpay_order_id": "order_test_123", "razorpay_payment_id": "pay_1", "razorpay_signature": "valid"}


def published_doctor(doc, admin):
    doctor_id = doctor(doc)
    login(admin, "admin@example.com")
    approved = admin.post(f"/api/v1/admin/doctors/{doctor_id}/decision",
                          json={"decision": "approved", "note": "Official register checked"})
    assert approved.status_code == 200, approved.text
    return doctor_id


def add_slot(doc, start, mode="online", minutes=30):
    created = doc.post("/api/v1/doctor/slots", json={"mode": mode, "start_at": start.isoformat(),
                                                     "end_at": (start + timedelta(minutes=minutes)).isoformat()})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def confirmed_booking(pt, slot_id):
    booked = pt.post("/api/v1/appointments", json={"slot_id": slot_id})
    assert booked.status_code == 201, booked.text
    appointment_id = booked.json()["appointment"]["id"]
    paid = pt.post(f"/api/v1/appointments/{appointment_id}/verify-payment", json=SIGNED)
    assert paid.status_code == 200 and paid.json()["status"] == "confirmed", paid.text
    return appointment_id


def second_patient(client):
    response = client.post("/api/v1/auth/patient-signup", json={
        "email": "second@example.com", "password": "testpass123", "full_name": "Second Patient", "phone": "9999999997"})
    assert response.status_code == 201, response.text
    verify_contacts(client)


def add_published_doctor(db, number, *, clinic, online, experience):
    user = db.insert(User(email=f"dr{number}@example.com", full_name=f"Dr {number}", phone="9876543210", password_hash="x",
                          role="doctor", email_verified_at=utcnow(), phone_verified_at=utcnow()))
    db.insert(DoctorProfile(user_id=user.id, council="Demo", registration_number=f"R{number}",
                            registration_date=date(2015, 1, 1), qualifications="MBBS", specialty="General Medicine",
                            experience_years=experience, clinic_name="Clinic", clinic_address="Somewhere 1", city="Kolkata",
                            clinic_fee_paise=clinic * 100, online_fee_paise=online * 100, verification_status="published",
                            council_key="demo", registration_key=f"r{number}"))
    return user.id


# ---------------------------------------------------------------- bug fixes

def test_seeded_patient_can_use_the_app_without_verification_codes(setup, monkeypatch):
    monkeypatch.setenv("SEED_PATIENT_EMAIL", "seeded@example.com")
    monkeypatch.setenv("SEED_PATIENT_PASSWORD", "seedpass123")
    seed.add_user(setup, "patient", "SEED_PATIENT")
    with TestClient(app) as client:
        response = client.post("/api/v1/auth/login", json={"email": "seeded@example.com", "password": "seedpass123"})
        assert response.status_code == 200 and response.json()["verification_required"] is False
        assert client.get("/api/v1/appointments").status_code == 200


def test_security_headers_are_sent_and_api_docs_keep_working(setup):
    with TestClient(app) as client:
        headers = client.get("/api/v1/doctors").headers
        assert headers["x-content-type-options"] == "nosniff" and headers["x-frame-options"] == "DENY"
        policy = headers["content-security-policy"]
        # The third parties the app really loads must stay allowed, or checkout, fonts and video break.
        for origin in ("checkout.razorpay.com", "meet.jit.si", "fonts.googleapis.com", "fonts.gstatic.com"):
            assert origin in policy
        assert "frame-ancestors 'none'" in policy
        docs = client.get("/docs")
        assert docs.status_code == 200 and "content-security-policy" not in docs.headers
        assert docs.headers["x-frame-options"] == "DENY"


def test_patient_booking_rejects_a_fee_changed_after_the_page_loaded(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        doctor_id = published_doctor(doc, admin)
        slot_id = add_slot(doc, utcnow() + timedelta(days=2))
        patient(pt)
        setup.update(DoctorProfile, {"_id": doctor_id}, {"online_fee_paise": 70000})
        stale = pt.post("/api/v1/appointments", json={"slot_id": slot_id, "expected_amount_paise": 50000})
        assert stale.status_code == 409 and "fee changed" in stale.json()["detail"]
        assert setup.get(Slot, slot_id).status == "available"
        current = pt.post("/api/v1/appointments", json={"slot_id": slot_id, "expected_amount_paise": 70000})
        assert current.status_code == 201 and current.json()["appointment"]["amount_paise"] == 70000


def test_dockerfile_trusts_proxy_headers_so_rate_limits_are_per_user():
    dockerfile = (Path(__file__).resolve().parents[2] / "Dockerfile").read_text()
    assert "--proxy-headers" in dockerfile and "--forwarded-allow-ips" in dockerfile


# ---------------------------------------------------------------- dev console codes

def test_console_delivery_needs_development_mode_and_the_explicit_flag(monkeypatch, caplog):
    for name in ("OTP_SECRET", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_MESSAGING_SERVICE_SID",
                 "RESEND_API_KEY", "OTP_EMAIL_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DEV_CONSOLE_OTP", "1")
    assert not otp.dev_console_delivery()
    with pytest.raises(HTTPException) as refused:
        otp.deliver_code("phone", "+919999999999", "123456")
    assert refused.value.status_code == 503
    with pytest.raises(HTTPException):
        otp._secret()
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DEV_CONSOLE_OTP")
    assert not otp.dev_console_delivery() and not otp.email_configured()

    monkeypatch.setenv("DEV_CONSOLE_OTP", "1")
    assert otp.dev_console_delivery() and otp.email_configured()
    assert len(otp._secret()) >= 32
    with caplog.at_level(logging.WARNING, logger="app.contact_verification"):
        otp.deliver_code("phone", "+919999999999", "654321")
        otp.deliver_email("a@example.com", "Subject", "Body text")
    assert "654321" in caplog.text and "Body text" in caplog.text


def test_console_delivery_lets_a_new_patient_verify_without_providers(monkeypatch, caplog):
    import mongomock
    from app.db import MongoStore, ensure_indexes, get_store
    for name in ("OTP_SECRET", "TWILIO_ACCOUNT_SID", "RESEND_API_KEY", "SMTP_USERNAME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DEV_CONSOLE_OTP", "1")
    database = mongomock.MongoClient(tz_aware=True)["console_test"]
    ensure_indexes(database)
    mongo = MongoStore(database)
    app.dependency_overrides[get_store] = lambda: mongo
    try:
        with TestClient(app) as client, caplog.at_level(logging.WARNING, logger="app.contact_verification"):
            signup = client.post("/api/v1/auth/patient-signup", json={
                "email": "dev@example.com", "password": "testpass123", "full_name": "Dev Patient", "phone": "9999999996"})
            assert signup.status_code == 201, signup.text
            for channel in ("email", "phone"):
                assert client.post(f"/api/v1/auth/contacts/{channel}/send").status_code == 200
                code = [line.split("code is ")[1][:6] for line in caplog.text.splitlines() if "code is" in line][-1]
                verified = client.post(f"/api/v1/auth/contacts/{channel}/verify", json={"code": code})
                assert verified.status_code == 200, verified.text
            assert verified.json()["verification_required"] is False
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- directory sort and pagination

def test_directory_sorts_and_paginates_without_changing_the_list_shape(setup):
    cheap = add_published_doctor(setup, 1, clinic=200, online=900, experience=3)
    mid = add_published_doctor(setup, 2, clinic=500, online=600, experience=20)
    dear = add_published_doctor(setup, 3, clinic=800, online=700, experience=10)
    for rating, doctor_id in ((5, mid), (4, dear), (3, dear)):
        setup.insert(Review(appointment_id=len(setup.find(Review)) + 1, doctor_id=doctor_id, patient_id=1, rating=rating))
    with TestClient(app) as client:
        ids = lambda **params: [item["id"] for item in client.get("/api/v1/doctors", params=params).json()]
        assert sorted(ids()) == sorted([cheap, mid, dear])
        assert ids(sort="fee_low") == [cheap, mid, dear]
        assert ids(sort="fee_high") == [dear, mid, cheap]
        assert ids(sort="experience") == [mid, dear, cheap]
        assert ids(sort="rating") == [mid, dear, cheap]  # 5.0, 3.5, then unrated last
        page = client.get("/api/v1/doctors", params={"sort": "fee_low", "limit": 2, "offset": 1})
        assert [item["id"] for item in page.json()] == [mid, dear]
        assert page.headers["x-total-count"] == "3"
        assert client.get("/api/v1/doctors", params={"sort": "nonsense"}).status_code == 422
        assert client.get("/api/v1/doctors", params={"limit": 0}).status_code == 422
        assert client.get("/api/v1/doctors", params={"available_on": "2000-01-01"}).headers["x-total-count"] == "0"


# ---------------------------------------------------------------- favourites

def test_favourites_are_private_idempotent_and_hide_unavailable_doctors(setup):
    doctor_id = add_published_doctor(setup, 1, clinic=300, online=300, experience=5)
    with TestClient(app) as pt, TestClient(app) as anonymous, TestClient(app) as doc:
        patient(pt)
        assert anonymous.put(f"/api/v1/favorites/{doctor_id}").status_code == 401
        assert pt.put("/api/v1/favorites/9999").status_code == 404
        assert pt.put(f"/api/v1/favorites/{doctor_id}").json() == {"favorite": True}
        assert pt.put(f"/api/v1/favorites/{doctor_id}").status_code == 200  # saving twice is harmless
        assert pt.get("/api/v1/favorites/ids").json() == [doctor_id]
        assert [item["id"] for item in pt.get("/api/v1/favorites").json()] == [doctor_id]
        assert len(setup.find(Favorite)) == 1
        setup.update(User, {"_id": doctor_id}, {"suspended_at": utcnow()})
        assert pt.get("/api/v1/favorites").json() == []
        setup.update(User, {"_id": doctor_id}, {"suspended_at": None})
        assert pt.delete(f"/api/v1/favorites/{doctor_id}").json() == {"favorite": False}
        assert pt.delete(f"/api/v1/favorites/{doctor_id}").status_code == 200
        assert pt.get("/api/v1/favorites/ids").json() == []
        doctor(doc)
        assert doc.get("/api/v1/favorites/ids").status_code == 403


# ---------------------------------------------------------------- waitlist

def test_waitlist_is_notified_when_a_time_opens_and_only_when_none_is_free(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as first, TestClient(app) as waiting:
        doctor_id = published_doctor(doc, admin)
        patient(first)
        second_patient(waiting)
        join = f"/api/v1/doctors/{doctor_id}/waitlist"
        slot_id = add_slot(doc, utcnow() + timedelta(days=2))
        # While a time is free the patient should book it rather than wait.
        assert waiting.post(join, json={"mode": "online"}).status_code == 409
        booked = first.post("/api/v1/appointments", json={"slot_id": slot_id})
        appointment_id = booked.json()["appointment"]["id"]
        assert waiting.post(join, json={"mode": "clinic"}).status_code == 201  # a different, empty visit type is fine
        entry = waiting.post(join, json={"mode": "online"})
        assert entry.status_code == 201, entry.text
        assert waiting.post(join, json={"mode": "online"}).json()["id"] == entry.json()["id"]  # joining twice is idempotent
        assert [(row["doctor_id"], row["mode"]) for row in waiting.get("/api/v1/waitlist").json()] == [(doctor_id, "clinic"), (doctor_id, "online")]
        assert doc.post(join, json={"mode": "online"}).status_code == 403
        assert waiting.delete(join, params={"mode": "clinic"}).status_code == 200

        assert first.post(f"/api/v1/appointments/{appointment_id}/cancel").status_code == 200
        notices = setup.find(Notification, {"kind": "waitlist_slot_open"})
        assert [item.user_id for item in notices] == [waiting.get("/api/v1/auth/me").json()["id"]]
        assert setup.find(WaitlistEntry) == []  # told once, then removed from the list
        assert "opened" in waiting.get("/api/v1/notifications/inbox").json()["items"][0]["body"]


def test_waitlist_notifies_only_the_longest_waiting_patients():
    import mongomock
    from app.db import MongoStore, ensure_indexes
    from app.waitlist import NOTIFY_LIMIT, notify_waitlist
    database = mongomock.MongoClient(tz_aware=True)["waitlist_unit"]
    ensure_indexes(database)
    db = MongoStore(database)
    for patient_id in range(1, NOTIFY_LIMIT + 4):
        db.insert(WaitlistEntry(patient_id=patient_id, doctor_id=7, mode="online"))
    assert notify_waitlist(db, 7, "online") == NOTIFY_LIMIT
    assert sorted(entry.patient_id for entry in db.find(WaitlistEntry)) == [NOTIFY_LIMIT + 1, NOTIFY_LIMIT + 2, NOTIFY_LIMIT + 3]
    assert notify_waitlist(db, 7, "clinic") == 0


# ---------------------------------------------------------------- reschedule

def test_reschedule_moves_a_confirmed_booking_and_frees_the_old_time(setup, monkeypatch):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt, TestClient(app) as other:
        published_doctor(doc, admin)
        old_slot = add_slot(doc, utcnow() + timedelta(days=3))
        new_slot = add_slot(doc, utcnow() + timedelta(days=4))
        clinic_slot = add_slot(doc, utcnow() + timedelta(days=5), mode="clinic")
        short_notice = add_slot(doc, utcnow() + timedelta(hours=5))
        patient(pt)
        second_patient(other)
        appointment_id = confirmed_booking(pt, old_slot)
        # Later bookings in this test need their own order ids (the shared mock reuses one).
        monkeypatch.setattr(payments, "create_order", lambda amount, receipt: {"id": f"order_{receipt}"})
        url = f"/api/v1/appointments/{appointment_id}/reschedule"

        assert other.post(url, json={"new_slot_id": new_slot}).status_code == 404  # not their appointment
        assert doc.post(url, json={"new_slot_id": new_slot}).status_code == 403
        assert pt.post(url, json={"new_slot_id": old_slot}).status_code == 409  # same time
        assert pt.post(url, json={"new_slot_id": clinic_slot}).status_code == 409  # different visit type
        assert pt.post(url, json={"new_slot_id": 99999}).status_code == 409
        assert pt.post(url, json={}).status_code == 422

        moved = pt.post(url, json={"new_slot_id": new_slot})
        assert moved.status_code == 200, moved.text
        assert moved.json()["slot_id"] == new_slot and moved.json()["status"] == "confirmed"
        freed, taken = setup.get(Slot, old_slot), setup.get(Slot, new_slot)
        assert (freed.status, freed.appointment_id) == ("available", None)
        assert (taken.status, taken.appointment_id) == ("booked", appointment_id)
        kinds = [item.kind for item in setup.find(Notification, {"kind": {"$in": ["booking_rescheduled", "doctor_booking_rescheduled"]}})]
        assert kinds == ["booking_rescheduled", "doctor_booking_rescheduled"]
        assert other.post("/api/v1/appointments", json={"slot_id": old_slot}).status_code == 201  # the freed time can be booked

        # A booking that now starts in under 24 hours cannot be moved, and a time someone holds cannot be taken.
        setup.update(Slot, {"_id": new_slot}, {"start_at": utcnow() + timedelta(hours=5), "end_at": utcnow() + timedelta(hours=6)})
        assert "24 hours" in pt.post(url, json={"new_slot_id": short_notice}).json()["detail"]
        setup.update(Slot, {"_id": new_slot}, {"start_at": utcnow() + timedelta(days=4), "end_at": utcnow() + timedelta(days=4, minutes=30)})
        assert pt.post(url, json={"new_slot_id": old_slot}).status_code == 409  # now held by the other patient


def test_reschedule_refuses_unpaid_repriced_and_inactive_bookings(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        doctor_id = published_doctor(doc, admin)
        slots = [add_slot(doc, utcnow() + timedelta(days=day)) for day in (3, 4, 5)]
        patient(pt)
        appointment_id = pt.post("/api/v1/appointments", json={"slot_id": slots[0]}).json()["appointment"]["id"]
        url = f"/api/v1/appointments/{appointment_id}/reschedule"
        assert pt.post(url, json={"new_slot_id": slots[1]}).status_code == 409  # still awaiting payment
        pt.post(f"/api/v1/appointments/{appointment_id}/verify-payment", json=SIGNED)
        setup.update(DoctorProfile, {"_id": doctor_id}, {"online_fee_paise": 60000})
        repriced = pt.post(url, json={"new_slot_id": slots[1]})
        assert repriced.status_code == 409 and "fee" in repriced.json()["detail"]
        assert setup.get(Slot, slots[1]).status == "available" and setup.get(Slot, slots[0]).status == "booked"
        setup.update(DoctorProfile, {"_id": doctor_id}, {"online_fee_paise": 50000})
        setup.update(User, {"_id": doctor_id}, {"suspended_at": utcnow()})
        assert pt.post(url, json={"new_slot_id": slots[1]}).status_code == 409


# ---------------------------------------------------------------- calendar export

def test_calendar_file_is_valid_escaped_and_limited_to_participants(setup, monkeypatch):
    monkeypatch.setattr(payments, "fetch_payment", lambda payment: {"order_id": "order_test_123", "amount": 40000, "currency": "INR", "status": "captured"})
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt, TestClient(app) as other:
        published_doctor(doc, admin)
        start = (utcnow() + timedelta(days=2)).replace(microsecond=0)
        slot_id = add_slot(doc, start, mode="clinic")
        patient(pt)
        second_patient(other)
        appointment_id = pt.post("/api/v1/appointments", json={"slot_id": slot_id}).json()["appointment"]["id"]
        calendar = f"/api/v1/appointments/{appointment_id}/calendar.ics"
        assert pt.get(calendar).status_code == 409  # not confirmed yet
        assert pt.post(f"/api/v1/appointments/{appointment_id}/verify-payment", json=SIGNED).json()["status"] == "confirmed"
        response = pt.get(calendar)
        assert response.status_code == 200 and response.headers["content-type"].startswith("text/calendar")
        assert "attachment" in response.headers["content-disposition"]
        body = response.text
        assert body.startswith("BEGIN:VCALENDAR\r\n") and body.endswith("END:VCALENDAR\r\n")
        assert f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}" in body
        assert "SUMMARY:Appointment with Demo Doctor" in body
        assert "LOCATION:Health Clinic\\, 123 Station Road" in body  # the comma must be escaped
        assert doc.get(calendar).status_code == 200
        assert other.get(calendar).status_code == 404
        assert TestClient(app).get(calendar).status_code == 401


# ---------------------------------------------------------------- reminders

def test_reminders_are_sent_once_and_skipped_when_the_booking_is_too_recent(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        doctor_id = published_doctor(doc, admin)
        patient(pt)
        patient_id = pt.get("/api/v1/auth/me").json()["id"]
        early = add_slot(doc, utcnow() + timedelta(hours=20))
        recent = add_slot(doc, utcnow() + timedelta(days=3))
        early_id = confirmed_booking(pt, early)
        recent_id = insert_confirmed(setup, doctor_id, patient_id, recent, utcnow() - timedelta(days=3))
        setup.update(Appointment, {"_id": early_id}, {"paid_at": utcnow() - timedelta(days=3)})

        def reminders():
            rows = setup.find(Notification, {"kind": {"$in": ["appointment_reminder", "doctor_appointment_reminder"]}})
            return sorted((item.user_id, item.kind, item.appointment_id) for item in rows)

        assert send_due_reminders(setup) == 1
        assert reminders() == sorted([(patient_id, "appointment_reminder", early_id), (doctor_id, "doctor_appointment_reminder", early_id)])
        assert send_due_reminders(setup) == 0 and len(reminders()) == 2  # running again does not repeat it

        # Within the hour, a second and separate reminder goes out.
        setup.update(Slot, {"_id": early}, {"start_at": utcnow() + timedelta(minutes=30), "end_at": utcnow() + timedelta(hours=1)})
        assert send_due_reminders(setup) == 1 and len(reminders()) == 4

        # A booking made inside the 24-hour window never gets a pointless "tomorrow" reminder.
        setup.update(Slot, {"_id": recent}, {"start_at": utcnow() + timedelta(hours=20), "end_at": utcnow() + timedelta(hours=21)})
        setup.update(Appointment, {"_id": recent_id}, {"paid_at": utcnow() - timedelta(minutes=5)})
        assert send_due_reminders(setup) == 0 and len(reminders()) == 4
        assert setup.get(Appointment, recent_id).reminder_24h_sent_at is not None


def insert_confirmed(db, doctor_id, patient_id, slot_id, paid_at):
    """A confirmed booking written directly, for tests that need several without repeating the payment mocks."""
    slot = db.get(Slot, slot_id)
    item = db.insert(Appointment(slot_id=slot_id, doctor_id=doctor_id, patient_id=patient_id, mode=slot.mode, amount_paise=50000,
                                 hold_expires_at=utcnow(), status="confirmed", paid_at=paid_at, room_name="room"))
    db.update(Slot, {"_id": slot_id}, {"status": "booked", "appointment_id": item.id})
    return item.id


def test_cancelled_appointments_get_no_reminder(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        published_doctor(doc, admin)
        patient(pt)
        slot = add_slot(doc, utcnow() + timedelta(hours=20))
        appointment_id = confirmed_booking(pt, slot)
        setup.update(Appointment, {"_id": appointment_id}, {"paid_at": utcnow() - timedelta(days=3)})
        cancelled = pt.post(f"/api/v1/appointments/{appointment_id}/cancel", json={"paid_paise": 50000, "refund_paise": 37500})
        assert cancelled.status_code == 200, cancelled.text
        assert send_due_reminders(setup) == 0


# ---------------------------------------------------------------- recurring availability

def test_bulk_slots_follow_india_time_skip_duplicates_and_enforce_limits(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        published_doctor(doc, admin)
        patient(pt)
        first_day = (utcnow() + timedelta(days=2)).date()
        plan = {"mode": "clinic", "start_date": first_day.isoformat(), "end_date": (first_day + timedelta(days=2)).isoformat(),
                "weekdays": [0, 1, 2, 3, 4, 5, 6], "start_time": "10:00", "end_time": "12:00", "slot_minutes": 30}
        result = doc.post("/api/v1/doctor/slots/bulk", json=plan)
        assert result.status_code == 201, result.text
        assert result.json()["created"] == 12 and result.json()["skipped"] == 0  # 3 days x 4 half-hours
        first_start = min(row["start_at"] for row in result.json()["slots"])
        assert "T04:30:00" in first_start  # 10:00 India time is 04:30 UTC
        again = doc.post("/api/v1/doctor/slots/bulk", json=plan)
        assert again.json()["created"] == 0 and again.json()["skipped"] == 12

        later = first_day + timedelta(days=4)
        one_day = {**plan, "start_date": later.isoformat(), "end_date": later.isoformat(), "weekdays": [later.weekday()]}
        assert doc.post("/api/v1/doctor/slots/bulk", json={**one_day, "start_time": "09:00", "end_time": "11:00"}).json()["created"] == 4
        # Hour-long slots overlapping what exists are skipped rather than failing; the free hour is created.
        overlap = doc.post("/api/v1/doctor/slots/bulk", json={**one_day, "start_time": "10:00", "end_time": "12:00", "slot_minutes": 60})
        assert overlap.json()["created"] == 1 and overlap.json()["skipped"] == 1

        assert doc.post("/api/v1/doctor/slots/bulk", json={**plan, "end_time": "09:00"}).status_code == 422
        assert doc.post("/api/v1/doctor/slots/bulk", json={**plan, "end_date": (first_day + timedelta(days=90)).isoformat()}).status_code == 422
        assert doc.post("/api/v1/doctor/slots/bulk", json={**plan, "slot_minutes": 5}).status_code == 422
        assert doc.post("/api/v1/doctor/slots/bulk", json={**plan, "weekdays": []}).status_code == 422
        assert doc.post("/api/v1/doctor/slots/bulk", json={**plan, "weekdays": [9]}).status_code == 422
        too_many = {**plan, "end_date": (first_day + timedelta(days=60)).isoformat(), "start_time": "00:00", "end_time": "23:59", "slot_minutes": 15}
        assert doc.post("/api/v1/doctor/slots/bulk", json=too_many).status_code == 422
        assert pt.post("/api/v1/doctor/slots/bulk", json=plan).status_code == 403


def test_bulk_slots_that_have_already_passed_are_skipped(setup):
    with TestClient(app) as doc, TestClient(app) as admin:
        published_doctor(doc, admin)
        yesterday = (utcnow() - timedelta(days=1)).date()
        plan = {"mode": "online", "start_date": yesterday.isoformat(), "end_date": yesterday.isoformat(),
                "weekdays": [yesterday.weekday()], "start_time": "10:00", "end_time": "11:00", "slot_minutes": 30}
        result = doc.post("/api/v1/doctor/slots/bulk", json=plan)
        assert result.status_code == 201 and result.json()["created"] == 0 and result.json()["skipped"] == 2


# ---------------------------------------------------------------- review replies

def test_doctor_replies_to_a_review_once_notified_and_can_edit(setup):
    with TestClient(app) as doc, TestClient(app) as admin, TestClient(app) as pt:
        doctor_id = published_doctor(doc, admin)
        slot_id = add_slot(doc, utcnow() + timedelta(minutes=30))
        patient(pt)
        appointment_id = confirmed_booking(pt, slot_id)
        setup.update(Slot, {"_id": slot_id}, {"start_at": utcnow() - timedelta(minutes=10), "end_at": utcnow() + timedelta(minutes=20)})
        assert doc.post(f"/api/v1/appointments/{appointment_id}/complete").status_code == 200
        assert pt.post(f"/api/v1/appointments/{appointment_id}/review", json={"rating": 4, "comment": "Good"}).status_code == 201
        review_id = pt.get(f"/api/v1/doctors/{doctor_id}").json()["reviews"][0]["id"]
        url = f"/api/v1/reviews/{review_id}/reply"

        assert pt.get(f"/api/v1/doctors/{doctor_id}").json()["reviews"][0]["doctor_reply"] == ""
        assert pt.post(url, json={"reply": "Thank you"}).status_code == 403
        assert doc.post(url, json={"reply": "   "}).status_code == 422
        assert doc.post(url, json={"reply": "x" * 501}).status_code == 422
        assert doc.post("/api/v1/reviews/9999/reply", json={"reply": "Hello there"}).status_code == 404
        assert doc.post(url, json={"reply": "Thank you for visiting"}).status_code == 200
        assert doc.post(url, json={"reply": "Thank you, take care"}).json()["doctor_reply"] == "Thank you, take care"
        shown = pt.get(f"/api/v1/doctors/{doctor_id}").json()["reviews"][0]
        assert shown["doctor_reply"] == "Thank you, take care" and shown["replied_at"]
        assert len(setup.find(Notification, {"kind": "review_reply"})) == 1  # editing does not notify again


def test_a_doctor_cannot_reply_to_another_doctors_review(setup):
    other = add_published_doctor(setup, 5, clinic=100, online=100, experience=1)
    review = setup.insert(Review(appointment_id=1, doctor_id=other, patient_id=1, rating=5))
    with TestClient(app) as doc, TestClient(app) as admin:
        published_doctor(doc, admin)
        assert doc.post(f"/api/v1/reviews/{review.id}/reply", json={"reply": "Not mine"}).status_code == 404
