import asyncio
import logging
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import admin, appointments, auth, doctors, favorites, guests, notifications
from .api import prescriptions as prescription_routes
from .contact_verification import email_configured
from .db import MongoStore, get_store
from .notifications import email_enabled, process_notification_email
from .prescriptions import process_prescriptions, settings as prescription_settings
from .refunds import process_refunds
from .reminders import send_due_reminders


def reconcile_payments():
    store = db.store()
    appointments.reconcile_due_orders(store)
    process_refunds(store)


async def payment_loop():
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.to_thread(reconcile_payments)
        except Exception as exc:
            logging.getLogger(__name__).warning("Payment reconciliation could not run (%s)", type(exc).__name__)


async def prescription_loop():
    while True:
        await asyncio.sleep(15)
        try:
            await asyncio.to_thread(lambda: process_prescriptions(db.store()))
        except Exception as exc:
            logging.getLogger(__name__).warning("Prescription worker could not run (%s)", type(exc).__name__)


async def reminder_loop():
    while True:
        try:
            await asyncio.to_thread(lambda: send_due_reminders(db.store()))
        except Exception as exc:
            logging.getLogger(__name__).warning("Reminder worker could not run (%s)", type(exc).__name__)
        await asyncio.sleep(60)


async def notification_email_loop():
    while True:
        try:
            await asyncio.to_thread(lambda: process_notification_email(db.store()))
        except Exception as exc:
            logging.getLogger(__name__).warning("Notification email worker could not run (%s)", type(exc).__name__)
        await asyncio.sleep(15)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = []
    if get_store not in _app.dependency_overrides:
        tasks.append(asyncio.create_task(reminder_loop()))
        if os.getenv("RAZORPAY_KEY_ID", "").startswith("rzp_test_") and os.getenv("RAZORPAY_KEY_SECRET"):
            tasks.append(asyncio.create_task(payment_loop()))
        if (os.getenv("PRESCRIPTION_GOOGLE_PROJECT") or os.getenv("GEMINI_API_KEY")
                or os.getenv("PRESCRIPTIONS_ENABLED") == "1"):
            tasks.append(asyncio.create_task(prescription_loop()))
        if os.getenv("PRESCRIPTIONS_ENABLED") == "1" and not prescription_settings():
            logging.getLogger(__name__).warning("Prescription uploads are disabled because configuration is incomplete.")
        if email_enabled():
            if email_configured():
                tasks.append(asyncio.create_task(notification_email_loop()))
            else:
                logging.getLogger(__name__).warning("Notification email is enabled but the email provider is not configured.")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Permitted-Cross-Domain-Policies": "none",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # The Jitsi video room is an iframe on meet.jit.si and must be allowed to ask for camera and microphone.
    "Permissions-Policy": 'camera=(self "https://meet.jit.si"), microphone=(self "https://meet.jit.si"), '
                          'display-capture=(self "https://meet.jit.si"), geolocation=()',
    # Origins here are the third parties the app actually loads: Razorpay checkout, the Jitsi external API and Google Fonts.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' https://checkout.razorpay.com https://meet.jit.si; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "img-src 'self' data: blob: https://*.razorpay.com; "
        "connect-src 'self' https://*.razorpay.com https://meet.jit.si; "
        "frame-src https://*.razorpay.com https://meet.jit.si; "
        "object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    ),
}

app = FastAPI(title="MediConnect API", version="1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, error: RequestValidationError):
    issues = [{key: issue[key] for key in ("loc", "msg", "type")} for issue in error.errors()]
    return JSONResponse({"detail": issues}, status_code=422)


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        allowed = {item.strip() for item in os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",")}
        allowed.add(str(request.base_url).rstrip("/"))
        host = request.headers.get("host", "")
        if host:
            allowed.update({f"https://{host}", f"http://{host}"})
        if origin and origin not in allowed:
            return JSONResponse({"detail": "Origin not allowed"}, status_code=403)
    response = await call_next(request)
    # Swagger UI loads its assets from a CDN, so the strict policy is skipped for the API docs pages only.
    docs_page = request.url.path.startswith(("/docs", "/redoc"))
    for header, value in SECURITY_HEADERS.items():
        if not (docs_page and header == "Content-Security-Policy"):
            response.headers.setdefault(header, value)
    # HSTS is only safe to emit when the app is deployed behind HTTPS.
    if os.getenv("APP_ENV") == "production":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.url.path.startswith(("/api/v1/auth/", "/api/v1/guest/", "/api/v1/appointments", "/api/v1/admin/",
                                    "/api/v1/notifications", "/api/v1/favorites", "/api/v1/waitlist")):
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(auth.router, prefix="/api/v1")
app.include_router(doctors.router, prefix="/api/v1")
app.include_router(appointments.router, prefix="/api/v1")
app.include_router(notifications.router, prefix="/api/v1")
app.include_router(prescription_routes.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")
app.include_router(guests.router, prefix="/api/v1")
app.include_router(favorites.router, prefix="/api/v1")


@app.get("/api/v1/health")
def health(db: MongoStore = Depends(get_store)):
    db.database.command("ping")
    return {"status": "ok"}


static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            return JSONResponse({"detail": "Not found"}, status_code=404)
        target = (static_dir / path).resolve()
        if target.is_relative_to(static_dir) and target.is_file():
            # The worker file must always be revalidated or browsers could keep running an old version.
            return FileResponse(target, headers={"Cache-Control": "no-cache"} if path == "sw.js" else None)
        return FileResponse(static_dir / "index.html")
