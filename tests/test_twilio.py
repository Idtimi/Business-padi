"""Tests for Twilio WhatsApp Sandbox integration."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models as m
from app.db import Base
from app.main import app

def test_twilio_webhook_health():
    client = TestClient(app)
    resp = client.get("/webhook/twilio")
    assert resp.status_code == 200
    assert "Twilio" in resp.text


def test_twilio_webhook_text_message(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tw_test.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.twilio_routes as tw_routes
    monkeypatch.setattr(tw_routes, "SessionLocal", session_factory)

    client = TestClient(app)
    resp = client.post(
        "/webhook/twilio",
        data={
            "From": "whatsapp:+2348033445566",
            "To": "whatsapp:+14155238886",
            "Body": "Hello Business Padi",
            "MessageSid": "SM1234567890",
            "NumMedia": "0",
        },
    )
    assert resp.status_code == 200
    assert "<Response>" in resp.text


def test_twilio_webhook_voice_note(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'tw_test2.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.twilio_routes as tw_routes
    monkeypatch.setattr(tw_routes, "SessionLocal", session_factory)

    client = TestClient(app)
    resp = client.post(
        "/webhook/twilio",
        data={
            "From": "whatsapp:+2348033445566",
            "To": "whatsapp:+14155238886",
            "Body": "",
            "MessageSid": "SM9876543210",
            "NumMedia": "1",
            "MediaUrl0": "https://api.twilio.com/mock_audio.ogg",
            "MediaContentType0": "audio/ogg",
        },
    )
    assert resp.status_code == 200
    assert "<Response>" in resp.text
