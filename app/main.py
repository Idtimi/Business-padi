from fastapi import FastAPI

from app.whatsapp_routes import router as whatsapp_router

app = FastAPI(title="Voice-to-Ledger")
app.include_router(whatsapp_router)


@app.get("/health")
def health():
    return {"status": "ok"}
