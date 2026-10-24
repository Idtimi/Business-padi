import asyncio
import hashlib
import hmac
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models as m
import app.whatsapp_routes as routes
from app.channels.mock import MockChannel
from app.channels.whatsapp import WhatsAppChannel, parse_webhook, verify_signature
from app.config import settings
from app.db import Base
from app.main import app

SECRET = "s3cret"
URL = "/whatsapp/webhook"
SENDER = "2348012345678"
RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


def payload(*messages, statuses=None):
    value = {"messaging_product": "whatsapp", "metadata": {"phone_number_id": "1"}}
    if messages:
        value["messages"] = list(messages)
    if statuses:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "1", "changes": [{"field": "messages", "value": value}]}]}


def text_msg(mid, body):
    return {"from": SENDER, "id": mid, "timestamp": "1761219600", "type": "text", "text": {"body": body}}


def button_msg(mid, button_id):
    return {"from": SENDER, "id": mid, "timestamp": "1761219600", "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "Save"}}}


def voice_msg(mid):
    return {"from": SENDER, "id": mid, "timestamp": "1761219600", "type": "audio",
            "audio": {"id": "MEDIA1", "mime_type": "audio/ogg; codecs=opus", "voice": True}}


def sign(raw: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def post(client, body: dict, secret=SECRET):
    raw = json.dumps(body).encode()
    return client.post(URL, content=raw, headers={
        "X-Hub-Signature-256": sign(raw, secret), "Content-Type": "application/json"})


@pytest.fixture()
def wa(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    channel = MockChannel()
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setattr(routes, "get_channel", lambda: channel)
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)
    monkeypatch.setattr(settings, "whatsapp_verify_token", "verify-me")
    return TestClient(app), channel, factory


def count(factory, model):
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model))


def seed_onboarded_user(factory, sender=SENDER):
    with factory() as s:
        u = m.User(channel="whatsapp", channel_user_id=sender, language="en",
                   state="DONE", consent_at=m.now_iso())
        s.add(u)
        s.flush()
        s.add(m.Business(owner_user_id=u.id, name="Shop"))
        s.commit()


def test_signature_valid():
    raw = b'{"a":1}'
    assert verify_signature(raw, sign(raw), SECRET)


def test_signature_wrong_secret_or_body():
    raw = b'{"a":1}'
    assert not verify_signature(raw, sign(raw, "other"), SECRET)
    assert not verify_signature(b'{"a":2}', sign(raw), SECRET)


def test_signature_missing_header_or_secret():
    assert not verify_signature(b"x", None, SECRET)
    assert not verify_signature(b"x", sign(b"x", ""), "")


def test_parse_text_voice_and_button():
    msgs, _ = parse_webhook(payload(text_msg("a", "hello"), voice_msg("b"), button_msg("c", "save:7")))
    assert [x.kind for x in msgs] == ["text", "voice", "button"]
    assert msgs[0].text == "hello" and msgs[0].user_ref == SENDER
    assert msgs[1].media_ref == "MEDIA1"
    assert msgs[2].button_id == "save:7"


def test_parse_delivery_status():
    msgs, statuses = parse_webhook(payload(statuses=[{"id": "wamid.1", "status": "delivered"}]))
    assert msgs == [] and statuses[0]["status"] == "delivered"


def test_verify_handshake_ok(wa):
    client, _, _ = wa
    r = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "verify-me",
                                "hub.challenge": "12345"})
    assert r.status_code == 200 and r.text == "12345"


def test_verify_handshake_wrong_token(wa):
    client, _, _ = wa
    r = client.get(URL, params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                "hub.challenge": "12345"})
    assert r.status_code == 403


def test_forged_request_is_rejected(wa):
    client, channel, factory = wa
    r = post(client, payload(text_msg("w1", RICE)), secret="wrong")
    assert r.status_code == 403
    assert channel.sent == [] and count(factory, m.User) == 0


def test_text_then_save_flow(wa):
    client, channel, factory = wa
    seed_onboarded_user(factory)
    assert post(client, payload(text_msg("w1", RICE))).status_code == 200
    assert len(channel.sent) == 1
    first = channel.sent[0]
    assert first["type"] == "buttons" and len(first["buttons"]) == 3
    assert "N127,500" in first["text"]
    assert count(factory, m.Transaction) == 0  # not saved until Save is tapped

    save_id = first["buttons"][0][0]
    post(client, payload(button_msg("w2", save_id)))
    assert "N77,500" in channel.sent[-1]["text"]
    assert count(factory, m.Transaction) == 1
    assert count(factory, m.WaMessageLog) == 2  # every outbound message is logged


def test_duplicate_delivery_is_ignored(wa):
    client, channel, factory = wa
    seed_onboarded_user(factory)
    post(client, payload(text_msg("w1", RICE)))
    post(client, payload(text_msg("w1", RICE)))  # Meta sometimes delivers twice
    assert len(channel.sent) == 1
    assert count(factory, m.PendingAction) == 1


def test_buttons_limited_to_three_and_titles_truncated():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"messages": [{"id": "wamid.x"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ch = WhatsAppChannel("tok", "123", "v20.0", client=client)
    asyncio.run(ch.send_buttons("234", "hi", [("a", "A" * 30), ("b", "B"), ("c", "C"), ("d", "D")]))
    rows = seen[0]["interactive"]["action"]["buttons"]
    assert len(rows) == 3 and len(rows[0]["reply"]["title"]) == 20
