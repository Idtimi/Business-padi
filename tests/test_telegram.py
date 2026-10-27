"""Tests for Telegram webhook and channel adapter."""

import pytest
from fastapi.testclient import TestClient

from app.channels.telegram import TelegramChannel
from app.db import SessionLocal
from app.main import app
import app.models as m


def test_telegram_health():
    client = TestClient(app)
    res = client.get("/webhook/telegram")
    assert res.status_code == 200
    assert "Telegram Webhook Ready" in res.text


def test_telegram_webhook_start_command(monkeypatch):
    import uuid
    sent_messages = []

    async def mock_send_buttons(self, user_ref, text, buttons):
        sent_messages.append({"to": user_ref, "text": text, "buttons": buttons})
        return {"ok": True}

    async def mock_send_text(self, user_ref, text):
        sent_messages.append({"to": user_ref, "text": text})
        return {"ok": True}

    monkeypatch.setattr(TelegramChannel, "send_buttons", mock_send_buttons)
    monkeypatch.setattr(TelegramChannel, "send_text", mock_send_text)

    uid = uuid.uuid4().hex[:8]
    client = TestClient(app)
    # Simulate user sending /start
    test_user_id = int(uuid.uuid4().int % 100000000 + 100000)
    payload = {
        "update_id": int(uuid.uuid4().int % 10000000),
        "message": {
            "message_id": f"m_{uid}",
            "from": {"id": test_user_id, "first_name": "Timi"},
            "chat": {"id": test_user_id, "type": "private"},
            "date": 1730000000,
            "text": "/start",
        },
    }
    res = client.post("/webhook/telegram", json=payload)
    assert res.status_code == 200
    assert len(sent_messages) > 0


def test_telegram_webhook_photo_upload(monkeypatch):
    import uuid
    import io
    from PIL import Image

    sent_messages = []

    async def mock_send_buttons(self, user_ref, text, buttons):
        sent_messages.append({"to": user_ref, "text": text, "buttons": buttons})
        return {"ok": True}

    async def mock_send_text(self, user_ref, text):
        sent_messages.append({"to": user_ref, "text": text})
        return {"ok": True}

    async def mock_download_media(self, media_ref):
        buf = io.BytesIO()
        img = Image.new("RGB", (100, 100), color="blue")
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"

    monkeypatch.setattr(TelegramChannel, "send_buttons", mock_send_buttons)
    monkeypatch.setattr(TelegramChannel, "send_text", mock_send_text)
    monkeypatch.setattr(TelegramChannel, "download_media", mock_download_media)

    client = TestClient(app)
    chat_id = "55443322"

    with SessionLocal() as db:
        u = db.query(m.User).filter_by(channel="telegram", channel_user_id=chat_id).first()
        if not u:
            u = m.User(channel="telegram", channel_user_id=chat_id, language="en", state="LOGO")
            db.add(u)
            db.flush()
        else:
            u.state = "LOGO"
        b = db.query(m.Business).filter_by(owner_user_id=u.id).first()
        if not b:
            b = m.Business(owner_user_id=u.id, name="Logo Shop", plan="starter")
            db.add(b)
        db.commit()

    uid = uuid.uuid4().hex[:8]
    payload = {
        "update_id": int(uuid.uuid4().int % 10000000),
        "message": {
            "message_id": f"m_{uid}",
            "from": {"id": int(chat_id), "first_name": "Timi"},
            "chat": {"id": int(chat_id), "type": "private"},
            "date": 1730000002,
            "photo": [
                {"file_id": "small_id", "width": 90, "height": 90},
                {"file_id": "large_id", "width": 800, "height": 800},
            ],
        },
    }
    res = client.post("/webhook/telegram", json=payload)
    assert res.status_code == 200
    assert len(sent_messages) > 0

    with SessionLocal() as db:
        u = db.query(m.User).filter_by(channel="telegram", channel_user_id=chat_id).first()
        assert u.state == "BRAND_COLOR"



def test_telegram_webhook_text_sale_entry(monkeypatch):
    import uuid
    sent_messages = []

    async def mock_send_buttons(self, user_ref, text, buttons):
        sent_messages.append({"to": user_ref, "text": text, "buttons": buttons})
        return {"ok": True}

    async def mock_send_text(self, user_ref, text):
        sent_messages.append({"to": user_ref, "text": text})
        return {"ok": True}

    monkeypatch.setattr(TelegramChannel, "send_buttons", mock_send_buttons)
    monkeypatch.setattr(TelegramChannel, "send_text", mock_send_text)

    client = TestClient(app)
    chat_id = "98765432"

    # First ensure user has DONE state
    with SessionLocal() as db:
        u = db.query(m.User).filter_by(channel="telegram", channel_user_id=chat_id).first()
        if not u:
            u = m.User(channel="telegram", channel_user_id=chat_id, language="pcm", state="DONE")
            db.add(u)
            db.flush()
        else:
            u.state = "DONE"
        b = db.query(m.Business).filter_by(owner_user_id=u.id).first()
        if not b:
            b = m.Business(owner_user_id=u.id, name="Test TG Store", plan="starter")
            db.add(b)
        db.commit()

    uid = uuid.uuid4().hex[:8]
    payload = {
        "update_id": int(uuid.uuid4().int % 10000000),
        "message": {
            "message_id": f"m_{uid}",
            "from": {"id": int(chat_id), "first_name": "Timi"},
            "chat": {"id": int(chat_id), "type": "private"},
            "date": 1730000001,
            "text": "I sell 2 bag of rice for 80,000 to Emeka and now e dey owe 40k",
        },
    }
    res = client.post("/webhook/telegram", json=payload)
    assert res.status_code == 200
    assert len(sent_messages) > 0


def test_telegram_webhook_photo_upload(monkeypatch):
    import uuid
    import io
    from PIL import Image

    sent_messages = []

    async def mock_send_buttons(self, user_ref, text, buttons):
        sent_messages.append({"to": user_ref, "text": text, "buttons": buttons})
        return {"ok": True}

    async def mock_send_text(self, user_ref, text):
        sent_messages.append({"to": user_ref, "text": text})
        return {"ok": True}

    async def mock_download_media(self, media_ref):
        buf = io.BytesIO()
        img = Image.new("RGB", (100, 100), color="blue")
        img.save(buf, format="PNG")
        return buf.getvalue(), "image/png"

    monkeypatch.setattr(TelegramChannel, "send_buttons", mock_send_buttons)
    monkeypatch.setattr(TelegramChannel, "send_text", mock_send_text)
    monkeypatch.setattr(TelegramChannel, "download_media", mock_download_media)

    client = TestClient(app)
    chat_id = "55443322"

    with SessionLocal() as db:
        u = db.query(m.User).filter_by(channel="telegram", channel_user_id=chat_id).first()
        if not u:
            u = m.User(channel="telegram", channel_user_id=chat_id, language="en", state="LOGO")
            db.add(u)
            db.flush()
        else:
            u.state = "LOGO"
        b = db.query(m.Business).filter_by(owner_user_id=u.id).first()
        if not b:
            b = m.Business(owner_user_id=u.id, name="Logo Shop", plan="starter")
            db.add(b)
        db.commit()

    uid = uuid.uuid4().hex[:8]
    payload = {
        "update_id": int(uuid.uuid4().int % 10000000),
        "message": {
            "message_id": f"m_{uid}",
            "from": {"id": int(chat_id), "first_name": "Timi"},
            "chat": {"id": int(chat_id), "type": "private"},
            "date": 1730000002,
            "photo": [
                {"file_id": "small_id", "width": 90, "height": 90},
                {"file_id": "large_id", "width": 800, "height": 800},
            ],
        },
    }
    res = client.post("/webhook/telegram", json=payload)
    assert res.status_code == 200
    assert len(sent_messages) > 0

    with SessionLocal() as db:
        u = db.query(m.User).filter_by(channel="telegram", channel_user_id=chat_id).first()
        assert u.state == "BRAND_COLOR"

