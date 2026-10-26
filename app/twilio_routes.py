"""Twilio WhatsApp webhook routes."""

from datetime import datetime, timezone
import logging

from fastapi import APIRouter, BackgroundTasks, Form, Request, Response
from fastapi.responses import PlainTextResponse

from app.channels.base import IncomingMessage
from app.channels.twilio import TwilioChannel
from app.config import settings
from app.db import SessionLocal
from app.i18n import t
from app.pipeline.handler import handle_incoming

log = logging.getLogger(__name__)
router = APIRouter()


async def process_twilio_message(msg: IncomingMessage) -> None:
    channel = TwilioChannel()
    try:
        with SessionLocal() as session:
            try:
                await handle_incoming(channel, msg, session)
            except Exception:
                log.exception("Failed to handle Twilio message %s", msg.message_id)
                try:
                    await channel.send_text(msg.user_ref, t(settings.default_language, "try_again"))
                except Exception:
                    log.exception("Could not send error message to user")
    finally:
        await channel.aclose()


@router.get("/webhook/twilio")
def twilio_health():
    """Verify endpoint availability."""
    return PlainTextResponse("Twilio WhatsApp Webhook Ready", status_code=200)


@router.post("/webhook/twilio")
async def receive_twilio_webhook(
    request: Request,
    background: BackgroundTasks,
    From: str = Form(""),
    To: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: str = Form(""),
    MediaContentType0: str = Form(""),
):
    """Receive incoming WhatsApp message or voice note from Twilio Sandbox."""
    clean_sender = From.replace("whatsapp:", "").strip()
    if not clean_sender:
        return PlainTextResponse("<Response></Response>", media_type="text/xml", status_code=200)

    try:
        media_count = int(NumMedia)
    except (ValueError, TypeError):
        media_count = 0

    if media_count > 0 and MediaUrl0:
        kind = "voice"
        media_ref = MediaUrl0
        text = None
    else:
        kind = "text"
        media_ref = None
        text = Body.strip()

    msg = IncomingMessage(
        channel="whatsapp",
        user_ref=clean_sender,
        message_id=MessageSid or f"tw_{datetime.now(timezone.utc).timestamp()}",
        kind=kind,
        text=text,
        media_ref=media_ref,
        button_id=None,
        timestamp=datetime.now(timezone.utc),
    )

    log.info("Received Twilio WhatsApp message (%s) from %s: %s", kind, clean_sender, text or media_ref)
    background.add_task(process_twilio_message, msg)

    # Return valid TwiML XML
    return Response(
        content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
        media_type="text/xml",
        status_code=200,
    )
