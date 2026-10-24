from fastapi import FastAPI

from app.dashboard_routes import router as dashboard_router
from app.profile_routes import router as profile_router
from app.whatsapp_routes import router as whatsapp_router

app = FastAPI(title="Voice-to-Ledger")
app.include_router(whatsapp_router)
app.include_router(dashboard_router, prefix="/d")
app.include_router(profile_router)


@app.get("/health")
def health():
    return {"status": "ok"}
