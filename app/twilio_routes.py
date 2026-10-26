"""Twilio WhatsApp webhook routes with direct TwiML instant response."""

from datetime import datetime, timezone
import logging
import xml.sax.saxutils as saxutils
from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import PlainTextResponse

from app.channels.base import IncomingMessage
from app.channels.twilio import TwilioChannel
from app.config import settings
from app.db import SessionLocal
from app.i18n import t
from app.models import Business, DocCounter, User, now_iso
from app.pipeline.handler import handle_incoming

log = logging.getLogger(__name__)
router = APIRouter()


class TwilioReplyCollector(TwilioChannel):
    """Channel adapter that collects outbound replies for inline TwiML delivery."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies: list[str] = []

    async def send_text(self, user_ref: str, text: str) -> dict[str, Any]:
        if text and text.strip():
            self.replies.append(text.strip())
        return {"status": "collected"}

    async def send_buttons(
        self,
        user_ref: str,
        text: str,
        buttons: list[tuple[str, str]],
    ) -> dict[str, Any]:
        options = "\n".join(f"[{idx + 1}] {title}" for idx, (_, title) in enumerate(buttons))
        body = f"{text}\n\n{options}\n(Reply with the button name or number)"
        self.replies.append(body.strip())
        return {"status": "collected"}

    async def send_document(
        self,
        user_ref: str,
        media_id: str,
        filename: str,
        caption: str = "",
    ) -> dict[str, Any]:
        body_text = caption or f"Document: {filename}"
        if media_id.startswith("http://") or media_id.startswith("https://"):
            body_text += f"\nDownload link: {media_id}"
        self.replies.append(body_text.strip())
        return {"status": "collected"}


@router.get("/webhook/twilio")
def twilio_health():
    """Verify endpoint availability."""
    return PlainTextResponse("Twilio WhatsApp Webhook Ready", status_code=200)


@router.post("/webhook/twilio")
async def receive_twilio_webhook(
    request: Request,
    From: str = Form(""),
    To: str = Form(""),
    Body: str = Form(""),
    MessageSid: str = Form(""),
    NumMedia: str = Form("0"),
    MediaUrl0: str = Form(""),
    MediaContentType0: str = Form(""),
):
    """Receive incoming WhatsApp message or voice note from Twilio Sandbox and respond with TwiML."""
    clean_sender = From.replace("whatsapp:", "").strip()
    if not clean_sender:
        return Response(
            content="<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>",
            media_type="text/xml",
            status_code=200,
        )

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

    channel = TwilioReplyCollector()
    with SessionLocal() as session:
        try:
            # Look up or create user
            user = session.query(User).filter(User.channel_user_id == clean_sender).first()
            if user is None:
                # Brand new user begins with company onboarding & language choice
                user = User(
                    channel="whatsapp",
                    channel_user_id=clean_sender,
                    display_name=f"Shop Owner {clean_sender[-4:]}",
                    language=settings.default_language,
                    state="NEW",
                )
                session.add(user)
                session.commit()

            # Allow user to restart/run onboarding anytime
            if msg.kind == "text" and msg.text and msg.text.strip().lower() in ("/setup", "setup", "/onboard", "onboarding", "/reset_shop", "start setup"):
                user.state = "NEW"
                session.commit()

            biz = session.query(Business).filter(Business.owner_user_id == user.id).first()
            if user.state == "DONE" and biz is None:
                biz = Business(
                    owner_user_id=user.id,
                    name=f"Shop {clean_sender[-4:]}",
                    phone=clean_sender,
                    plan="starter",
                    created_at=now_iso(),
                )
                session.add(biz)
                session.flush()
                for k in ("receipt", "invoice", "statement", "quote"):
                    session.add(DocCounter(business_id=biz.id, kind=k, last_no=0))
                session.commit()

            # Handle numbered option replies for pending actions
            if user.state == "DONE" and biz is not None and msg.kind == "text" and msg.text:
                from app.models import PendingAction
                t_lower = msg.text.strip().lower()
                open_pending = session.query(PendingAction).filter(
                    PendingAction.business_id == biz.id,
                    PendingAction.user_id == user.id,
                    PendingAction.status == "open",
                ).order_by(PendingAction.id.desc()).first()

                if open_pending is not None:
                    if t_lower in ("1", "save", "yes", "ok", "confirm"):
                        msg.kind = "button"
                        msg.button_id = f"save:{open_pending.id}"
                    elif t_lower in ("2", "edit", "change", "change am"):
                        msg.kind = "button"
                        msg.button_id = f"edit:{open_pending.id}"
                    elif t_lower in ("3", "cancel", "no", "cancel am"):
                        msg.kind = "button"
                        msg.button_id = f"cancel:{open_pending.id}"

            await handle_incoming(channel, msg, session)
        except Exception:
            log.exception("Error processing Twilio message %s", msg.message_id)
            channel.replies.append(t(settings.default_language, "try_again"))
        finally:
            await channel.aclose()

    # Build TwiML response
    if channel.replies:
        twiml_messages = "".join(f"<Message><Body>{saxutils.escape(r)}</Body></Message>" for r in channel.replies)
        content = f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response>{twiml_messages}</Response>"
    else:
        content = "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response></Response>"

    return Response(content=content, media_type="text/xml", status_code=200)
