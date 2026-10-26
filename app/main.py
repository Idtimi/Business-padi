import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from app.config import settings
from app.dashboard_routes import router as dashboard_router
from app.db import Base, engine, SessionLocal
from app.logging_conf import setup_logging
from app.profile_routes import router as profile_router
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
        except Exception as e:
            log.exception("Automatic schema / demo initialization error: %s", str(e))
    yield


app = FastAPI(title="Business Padi", lifespan=lifespan)
app.include_router(whatsapp_router)
app.include_router(twilio_router)
app.include_router(dashboard_router, prefix="/d")
app.include_router(profile_router)


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


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.exception("Unhandled error processing %s: %s", request.url.path, str(exc))
    return JSONResponse(
        {"error": "internal_server_error", "message": "Sorry, something went wrong. Please try again."},
        status_code=500,
    )
