import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.config import settings
from app.dashboard_routes import router as dashboard_router
from app.db import SessionLocal
from app.logging_conf import setup_logging
from app.profile_routes import router as profile_router
from app.whatsapp_routes import router as whatsapp_router

setup_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="Voice-to-Ledger")
app.include_router(whatsapp_router)
app.include_router(dashboard_router, prefix="/d")
app.include_router(profile_router)


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
