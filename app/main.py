import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import text

from app.config import settings
from app.dashboard_routes import router as dashboard_router
from app.db import Base, engine, SessionLocal
from app.logging_conf import setup_logging
from app.profile_routes import router as profile_router
from app.telegram_routes import router as telegram_router
from app.twilio_routes import router as twilio_router
from app.whatsapp_routes import router as whatsapp_router
import app.models as m

setup_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Ensure database schema is created
    Base.metadata.create_all(bind=engine)
    # Automatically seed starter demo shop if database has no businesses
    with SessionLocal() as db:
        try:
            biz_count = db.query(m.Business).count()
            if biz_count == 0:
                from scripts.seed_demo import seed_demo_data
                seed_demo_data(reset=False)
                log.info("Initialized schema and seeded Mama Ngozi Provisions demo shop.")

            # Ensure primary merchant user (+2348164802550) is permanently provisioned
            primary_phones = ["+2348164802550", "2348164802550"]
            for phone in primary_phones:
                existing_user = db.query(m.User).filter_by(channel_user_id=phone).first()
                if not existing_user:
                    u = m.User(
                        channel="whatsapp",
                        channel_user_id=phone,
                        display_name="Merchant",
                        language="en",
                        state="DONE",
                        created_at=m.now_iso(),
                    )
                    db.add(u)
                    db.flush()
                    b = m.Business(
                        owner_user_id=u.id,
                        name="Business Padi Store",
                        phone=phone,
                        brand_color="#1E293B",
                        plan="trader",
                        created_at=m.now_iso(),
                    )
                    db.add(b)
                    db.flush()
                    for k in ("receipt", "invoice", "statement", "quote"):
                        db.add(m.DocCounter(business_id=b.id, kind=k, last_no=0))
                    db.commit()
                    log.info("Seeded primary user %s with trader business.", phone)
                else:
                    updated = False
                    if existing_user.state != "DONE":
                        existing_user.state = "DONE"
                        updated = True
                    if existing_user.language != "en":
                        existing_user.language = "en"
                        updated = True
                    existing_biz = db.query(m.Business).filter_by(owner_user_id=existing_user.id).first()
                    if not existing_biz:
                        b = m.Business(
                            owner_user_id=existing_user.id,
                            name="Business Padi Store",
                            phone=phone,
                            brand_color="#1E293B",
                            plan="trader",
                            created_at=m.now_iso(),
                        )
                        db.add(b)
                        db.flush()
                        for k in ("receipt", "invoice", "statement", "quote"):
                            db.add(m.DocCounter(business_id=b.id, kind=k, last_no=0))
                        updated = True
                    if updated:
                        db.commit()

            # Clean up temporary businesses created during earlier testing phases
            hardcoded_phones = {"2348011223344", "+2348164802550", "2348164802550"}
            hardcoded_names = {"Mama Ngozi Provisions", "Business Padi Store"}
            test_bizs = db.query(m.Business).filter(~m.Business.name.in_(hardcoded_names)).all()
            for tb in test_bizs:
                owner = db.get(m.User, tb.owner_user_id) if tb.owner_user_id else None
                if owner and owner.channel_user_id in hardcoded_phones:
                    continue
                m.delete_business_cascade(db, tb.id)
                if owner and owner.channel_user_id not in hardcoded_phones:
                    owner.state = "NEW"
                    owner.consent_at = None
                    owner.state_data = {}
                    db.commit()

            # Automatically register Telegram webhook if configured
            try:
                tg_token = getattr(settings, "telegram_bot_token", None)
                if tg_token and settings.base_url:
                    import httpx
                    target_url = f"{settings.base_url.rstrip('/')}/webhook/telegram"
                    reg_url = f"https://api.telegram.org/bot{tg_token}/setWebhook?url={target_url}"
                    with httpx.Client(timeout=10.0) as client:
                        reg_res = client.get(reg_url)
                        log.info("Telegram setWebhook (%s): %s", target_url, reg_res.text)
            except Exception as e:
                log.warning("Could not auto-register Telegram webhook: %s", e)
        except Exception as e:
            log.exception("Automatic schema / demo initialization error: %s", str(e))
    yield


app = FastAPI(title="Business Padi", lifespan=lifespan)
app.include_router(whatsapp_router)
app.include_router(twilio_router)
app.include_router(telegram_router)
app.include_router(dashboard_router, prefix="/d")
app.include_router(profile_router)


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 95" fill="none">
    <path d="M22 10C56 10 78 22 78 45C78 65 58 74 36 74L36 84C36 89.5 31.5 94 26 94C20.5 94 16 89.5 16 84L16 24C16 16.3 22 10 26 10H22Z" fill="#FF6A13"/>
    <rect x="34" y="28" width="26" height="32" rx="13" fill="#0D1F45"/>
    <rect x="39" y="38" width="3.2" height="12" rx="1.6" fill="#FF6A13"/>
    <rect x="45.4" y="33" width="3.2" height="22" rx="1.6" fill="#FF6A13"/>
    <rect x="51.8" y="38" width="3.2" height="12" rx="1.6" fill="#FF6A13"/>
    <circle cx="82" cy="18" r="8" fill="#FFC233"/>
</svg>"""


@app.get("/favicon.ico")
@app.get("/favicon.svg")
def favicon():
    return Response(content=FAVICON_SVG, media_type="image/svg+xml")


@app.get("/")
def root():
    return RedirectResponse(url="/d")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def readiness():
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
        return {"status": "ready", "database": "connected"}
    except Exception as e:
        log.exception("Readiness check failed: %s", str(e))
        return JSONResponse(
            {"status": "unhealthy", "database": "disconnected", "error": str(e)},
            status_code=503,
        )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    accept = request.headers.get("accept", "")
    if exc.status_code == 401 and request.url.path.startswith("/d") and "text/html" in accept:
        return RedirectResponse(url="/d/login", status_code=303)
    return JSONResponse(
        {"detail": exc.detail},
        status_code=exc.status_code,
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error processing %s: %s", request.url.path, str(exc))
    return JSONResponse(
        {"error": "internal_server_error", "message": "Sorry, something went wrong. Please try again."},
        status_code=500,
    )
