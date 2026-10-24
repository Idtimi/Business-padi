import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from app.channels.whatsapp import WhatsAppChannel, parse_webhook, verify_signature
from app.config import settings
from app.db import SessionLocal
from app.i18n import t
from app.pipeline.handler import handle_incoming
from app.services.templates import update_delivery_status

log = logging.getLogger(__name__)
router = APIRouter()
MAX_BODY_BYTES = 1_000_000


def get_channel():
    return WhatsAppChannel(settings.whatsapp_token, settings.whatsapp_phone_number_id,
                           settings.graph_api_version)


@router.get("/whatsapp/webhook")
def verify(mode: str = Query("", alias="hub.mode"),
           token: str = Query("", alias="hub.verify_token"),
           challenge: str = Query("", alias="hub.challenge")):
    """Meta calls this once, when you register the webhook."""
    expected = settings.whatsapp_verify_token
    if mode == "subscribe" and expected and hmac.compare_digest(token.encode(), expected.encode()):
        return PlainTextResponse(challenge)
    return PlainTextResponse("forbidden", status_code=403)


async def process_messages(messages) -> None:
    channel = get_channel()
    try:
        for msg in messages:
            with SessionLocal() as session:
                try:
                    await handle_incoming(channel, msg, session)
                except Exception:
                    log.exception("failed to handle message %s", msg.message_id)
                    try:
                        await channel.send_text(msg.user_ref, t(settings.default_language, "try_again"))
                    except Exception:
                        log.exception("could not send the error message")
    finally:
        if hasattr(channel, "aclose"):
            await channel.aclose()


@router.post("/whatsapp/webhook")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        return JSONResponse({"error": "too large"}, status_code=413)
    if not verify_signature(raw, request.headers.get("X-Hub-Signature-256"), settings.whatsapp_app_secret):
        return JSONResponse({"error": "bad signature"}, status_code=403)
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    messages, statuses = parse_webhook(payload)
    if statuses:
        with SessionLocal() as session:
            for s in statuses:
                update_delivery_status(session, s)
    if messages:
        background.add_task(process_messages, messages)
    return {"status": "ok"}
