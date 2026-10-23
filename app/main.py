from fastapi import FastAPI

app = FastAPI(title="Voice-to-Ledger")


@app.get("/health")
def health():
    return {"status": "ok"}