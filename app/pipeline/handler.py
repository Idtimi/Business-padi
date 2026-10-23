import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import Channel, IncomingMessage
from app.config import settings
from app.i18n import t
from app.models import Business, ProcessedMessage, User, WaMessageLog
from app.pipeline import router

log = logging.getLogger(__name__)


def mask(ref: str) -> str:
    """Never keep full phone numbers in logs: 2348012345678 -> *********5678"""
    return "*" * max(len(ref) - 4, 0) + ref[-4:]


def message_cost_kobo() -> int:
    return round(settings.wa_usd_per_message * settings.usd_ngn * 100)


def resolve_identity(session: Session, msg: IncomingMessage) -> tuple[User, Business]:
    """TEMPORARY: auto-creates a user and shop on first message.
    Phase 4 replaces this with real onboarding (consent, shop name, logo)."""
    user = session.scalar(select(User).where(
        User.channel == msg.channel, User.channel_user_id == msg.user_ref))
    if user is None:
        user = User(channel=msg.channel, channel_user_id=msg.user_ref,
                    language=settings.default_language)
        session.add(user)
        session.flush()
        session.add(Business(owner_user_id=user.id, name="My Shop"))
        session.flush()
    biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
    return user, biz


def _non_text_replies(session, biz, user, msg) -> list[router.Reply]:
    if msg.kind == "button":
        try:
            action, pid = router.parse_button(msg.button_id or "")
        except ValueError:
            return [router.Reply(t(user.language, "try_again"))]
        return router.handle_button(session, biz.id, user.id, action, pid, user.language)
    if msg.kind == "voice":  # TEMPORARY until the next chunk
        return [router.Reply("Voice notes are coming soon. Please type for now.")]
    return [router.Reply("I can only read text and voice notes for now.")]


async def _send_all(channel: Channel, session: Session, biz, to_ref, replies):
    for r in replies:
        if r.buttons:
            await channel.send_buttons(to_ref, r.text, r.buttons)
        else:
            await channel.send_text(to_ref, r.text)
        session.add(WaMessageLog(
            business_id=biz.id, to_ref=mask(to_ref), category="service",
            msg_type="interactive" if r.buttons else "text",
            status="sent", est_cost_kobo=message_cost_kobo()))
    session.commit()


async def handle_incoming(channel: Channel, msg: IncomingMessage, session: Session) -> None:
    user, biz = resolve_identity(session, msg)
    user.last_user_message_at = msg.timestamp.isoformat()
    session.commit()

    if msg.kind == "text":
        # router.handle_text ignores repeats of the same message id by itself
        replies = router.handle_text(session, biz.id, user.id, msg.text or "",
                                     msg.message_id, user.language, channel=msg.channel)
    else:
        if session.get(ProcessedMessage, (msg.channel, msg.message_id)) is not None:
            return
        replies = _non_text_replies(session, biz, user, msg)
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()

    await _send_all(channel, session, biz, msg.user_ref, replies)