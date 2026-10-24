import logging
import shutil

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import Channel, IncomingMessage
from app.config import settings
from app.i18n import t
from app.models import Business, ProcessedMessage, User, WaMessageLog
from app.pipeline import router
from app.pipeline.asr import ASRClient, get_asr
from app.pipeline.audio import AudioConversionError, AudioTooLong, download_and_convert
from app.services import onboarding
from app.services.costs import check_daily_budget, message_cost_kobo

log = logging.getLogger(__name__)


def mask(ref: str) -> str:
    """Never keep full phone numbers in logs: 2348012345678 -> *********5678"""
    return "*" * max(len(ref) - 4, 0) + ref[-4:]


def resolve_identity(session: Session, msg: IncomingMessage) -> tuple[User, Business | None]:
    """Resolve user and their business. If user doesn't exist, create in NEW state."""
    user = session.scalar(select(User).where(
        User.channel == msg.channel, User.channel_user_id == msg.user_ref))
    if user is None:
        user = User(channel=msg.channel, channel_user_id=msg.user_ref,
                    language=settings.default_language, state="NEW")
        session.add(user)
        session.flush()

    biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
    if biz is None:
        from app.models import Staff
        staff_row = session.scalar(
            select(Staff).where(
                Staff.user_id == user.id,
                Staff.active == 1,
            )
        )
        if staff_row is not None:
            biz = session.get(Business, staff_row.business_id)
    return user, biz


async def _handle_voice(channel: Channel, session: Session, biz: Business | None, user: User, msg: IncomingMessage,
                        asr: ASRClient | None = None) -> list[router.Reply]:
    """Download voice note -> convert to WAV -> ASR -> route as text."""
    asr = asr or get_asr()
    tmp_dir = None
    try:
        wav_path, tmp_dir = await download_and_convert(channel, msg.media_ref)
        transcript = asr.transcribe(wav_path, user.language, [])
        log.info("ASR transcript for %s: %r", mask(msg.user_ref), transcript.text[:120])
        if not transcript.text.strip():
            return [router.Reply(t(user.language, "voice_empty"))]

        # Check onboarding state first
        if user.state != "DONE":
            msg.text = transcript.text
            return await onboarding.handle_onboarding(channel, msg, user, session)

        if biz is None:
            return [router.Reply("Please complete shop setup first.")]

        return router.handle_text(session, biz.id, user.id, transcript.text,
                                  msg.message_id, user.language, channel=msg.channel,
                                  is_voice=True)
    except AudioTooLong:
        max_s = settings.max_voice_seconds
        return [router.Reply(f"Voice note too long. Please keep it under {max_s} seconds.")]
    except AudioConversionError:
        log.exception("audio conversion failed for message %s", msg.message_id)
        return [router.Reply(t(user.language, "try_again"))]
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _non_text_replies(session: Session, biz: Business | None, user: User, msg: IncomingMessage) -> list[router.Reply]:
    if msg.kind == "button":
        try:
            action, pid = router.parse_button(msg.button_id or "")
        except ValueError:
            return [router.Reply(t(user.language, "try_again"))]
        if biz is None:
            return [router.Reply("Please complete shop setup first.")]
        return router.handle_button(session, biz.id, user.id, action, pid, user.language)
    return [router.Reply("I can only read text and voice notes for now.")]


async def _send_all(channel: Channel, session: Session, biz: Business | None, to_ref: str, replies: list[router.Reply]):
    for r in replies:
        resp = None
        if r.buttons:
            resp = await channel.send_buttons(to_ref, r.text, r.buttons)
        else:
            resp = await channel.send_text(to_ref, r.text)

        wa_mid = None
        if isinstance(resp, dict) and resp.get("messages"):
            wa_mid = resp["messages"][0].get("id")

        session.add(WaMessageLog(
            business_id=biz.id if biz else None,
            to_ref=mask(to_ref),
            category="service",
            msg_type="interactive" if r.buttons else "text",
            wa_message_id=wa_mid,
            status="sent",
            est_cost_kobo=message_cost_kobo("service")
        ))
    session.commit()
    check_daily_budget(session)


async def handle_incoming(channel: Channel, msg: IncomingMessage, session: Session,
                          asr: ASRClient | None = None) -> None:
    user, biz = resolve_identity(session, msg)
    user.last_user_message_at = msg.timestamp.isoformat()
    session.commit()

    # Deduplication check up front
    if session.get(ProcessedMessage, (msg.channel, msg.message_id)) is not None:
        return

    # Check for STOP / START keywords
    text = (msg.text or "").strip()
    t_upper = text.upper()
    if msg.kind == "text" and t_upper in ("STOP", "START"):
        from app.services import templates
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        if t_upper == "STOP":
            templates.opt_out_phone(session, msg.user_ref)
            session.commit()
            reply_text = "You have successfully opted out and will no longer receive automated messages. Reply START to opt back in."
        else:
            templates.opt_in_phone(session, msg.user_ref)
            session.commit()
            reply_text = "You have re-subscribed to messages. Text HELP to see available options."
        await _send_all(channel, session, biz, msg.user_ref, [router.Reply(reply_text)])
        return

    # Check for staff invite join codes
    if msg.kind == "text" and (t_upper.startswith("JOIN-") or t_upper.startswith("/JOIN ")):
        code = t_upper.replace("/JOIN ", "JOIN-")
        from app.services import staff
        ok, reply_msg, st = staff.join_business_with_code(session, user.id, code)
        if ok:
            user.state = "DONE"
            session.commit()
            biz = session.get(Business, st.business_id) if st else biz
        replies = [router.Reply(reply_msg)]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 1. State machine: if user has not completed onboarding, route to onboarding
    if user.state != "DONE":
        if msg.kind == "voice":
            replies = await _handle_voice(channel, session, biz, user, msg, asr=asr)
        else:
            replies = await onboarding.handle_onboarding(channel, msg, user, session)
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        # Refresh biz if created during onboarding
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 2. Onboarded users: check for settings and help commands
    if msg.kind == "text" and text.lower().startswith("/settings"):
        replies = [onboarding.handle_settings_command(session, user, biz, text, user.language)]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    if msg.kind == "text" and text.lower() == "help":
        replies = [router.Reply(onboarding.help_menu(user.language))]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 3. Regular transaction and query flow
    if msg.kind == "text":
        replies = router.handle_text(session, biz.id, user.id, text,
                                     msg.message_id, user.language, channel=msg.channel)
    elif msg.kind == "voice":
        replies = await _handle_voice(channel, session, biz, user, msg, asr=asr)
    else:
        replies = _non_text_replies(session, biz, user, msg)
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()

    await _send_all(channel, session, biz, msg.user_ref, replies)
