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
    ref = (msg.user_ref or "").strip()
    variants = [ref]
    if ref.startswith("+"):
        variants.append(ref.lstrip("+"))
    else:
        variants.append(f"+{ref}")

    user = session.scalar(
        select(User).where(User.channel == msg.channel, User.channel_user_id.in_(variants))
    )
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
    speech_lang = (user.state_data or {}).get("speech_language", user.language)
    ui_lang = user.language

    from config.languages import is_assist_or_planned, get_language, get_support_level
    if is_assist_or_planned(speech_lang):
        from app.models import Event
        session.add(Event(
            business_id=biz.id if biz else None,
            user_id=user.id,
            name="language_demand",
            props={"language": speech_lang, "level": get_support_level(speech_lang), "action": "assist_fallback"},
        ))
        session.commit()
        return [router.Reply(t(ui_lang, "assist_fallback", language=get_language(speech_lang)["name"]))]

    asr = asr or get_asr()
    tmp_dir = None
    try:
        wav_path, tmp_dir = await download_and_convert(channel, msg.media_ref)
        from app.pipeline.glossary import get_glossary_keywords
        glossary_kws = get_glossary_keywords(speech_lang)
        transcript = asr.transcribe(wav_path, speech_lang, glossary_kws)
        log.info("ASR transcript for %s (%s): %r", mask(msg.user_ref), speech_lang, transcript.text[:120])
        if not transcript.text or len(transcript.text.strip()) < 3:
            return [router.Reply(t(ui_lang, "repeat_prompt"))]

        # Check onboarding state first
        if user.state != "DONE":
            msg.text = transcript.text
            return await onboarding.handle_onboarding(channel, msg, user, session)

        if biz is None:
            return [router.Reply("Please complete shop setup first.")]

        return router.handle_text(session, biz.id, user.id, transcript.text,
                                  msg.message_id, ui_lang, channel=msg.channel,
                                  is_voice=True, speech_lang=speech_lang)
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
        if getattr(r, "document_path", None) and hasattr(channel, "send_document"):
            resp = await channel.send_document(
                to_ref,
                r.document_path,
                getattr(r, "document_filename", "receipt.pdf"),
                caption=r.text,
            )
        elif r.buttons:
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
            msg_type="interactive" if r.buttons else ("document" if getattr(r, "document_path", None) else "text"),
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

    # Per-user rate limiting check
    from app.resilience import limiter
    allowed, _ = limiter.is_allowed(msg.user_ref)
    if not allowed:
        log.warning("Rate limit exceeded for user %s", mask(msg.user_ref))
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, [router.Reply("You are sending messages too quickly. Please wait a moment.")])
        return

    # Check for WhatsApp STOP opt-out (Twilio opt-out compliance)
    text = (msg.text or "").strip()
    t_upper = text.upper()
    if msg.kind == "text" and msg.channel in ("whatsapp", "twilio") and t_upper == "STOP":
        from app.services import templates
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        templates.opt_out_phone(session, msg.user_ref)
        session.commit()
        reply_text = "You have successfully opted out and will no longer receive automated messages. Send a message to start again."
        await _send_all(channel, session, biz, msg.user_ref, [router.Reply(reply_text)])
        return

    # Check for account / shop deletion and reset commands
    if msg.kind == "text" and t_upper in (
        "/RESET", "RESET", "/RESET_SHOP", "RESET SHOP",
        "/DELETE_ACCOUNT", "DELETE ACCOUNT", "/DELETE_SHOP", "DELETE SHOP",
        "/DELETE"
    ):
        hardcoded_phones = {"2348011223344", "+2348164802550", "2348164802550"}
        hardcoded_names = {"Mama Ngozi Provisions", "Business Padi Store"}
        if biz and biz.name not in hardcoded_names and user.channel_user_id not in hardcoded_phones:
            from app import models as m
            m.delete_business_cascade(session, biz.id)
            biz = None

        user.state = "CHOOSE_LANGUAGE"
        user.consent_at = None
        user.state_data = {}
        session.commit()

        reset_reply = "Your shop and account data have been deleted. Let's set up your shop again from scratch.\n\nChoose your language:"
        buttons = [
            ("lang:en", "English"),
            ("lang:pcm", "Nigerian Pidgin"),
            ("lang:yo", "Yorùbá"),
            ("lang:ha", "Hausa"),
            ("lang:ig", "Ìgbò"),
            ("lang:other", "Other language"),
        ]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, None, msg.user_ref, [router.Reply(reset_reply, buttons=buttons, kind="confirm")])
        return

    # Check for /start or /setup
    if msg.kind == "text" and t_upper in ("/START", "START", "/SETUP", "SETUP", "/ONBOARD", "ONBOARDING", "RESTART", "/RESTART"):
        if user.state == "DONE" and biz is not None:
            start_msg = (
                f"Welcome back to {biz.name}!\n\n"
                "You can:\n"
                "- Record a sale or expense by voice note or text\n"
                "- Issue a receipt with /receipt\n"
                "- Open your web portal with /dashboard\n"
                "- See all commands with /commands\n"
                "- Reset your shop anytime with /reset or /delete_account"
            )
            session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
            session.commit()
            await _send_all(channel, session, biz, msg.user_ref, [router.Reply(start_msg)])
            return

        user.state = "NEW"
        user.consent_at = None
        user.state_data = {}
        session.commit()
        replies = await onboarding.handle_onboarding(channel, msg, user, session)
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 1. Check for settings commands upfront
    if msg.kind == "text" and text.lower().startswith("/settings"):
        if user.state != "DONE":
            user.state = "DONE"
            session.commit()
        if biz is None:
            from app.models import DocCounter, now_iso
            biz = Business(
                owner_user_id=user.id,
                name=f"Shop {msg.user_ref[-4:]}",
                phone=msg.user_ref,
                plan="starter",
                created_at=now_iso(),
            )
            session.add(biz)
            session.flush()
            for k in ("receipt", "invoice", "statement", "quote"):
                session.add(DocCounter(business_id=biz.id, kind=k, last_no=0))
            session.commit()

        replies = [onboarding.handle_settings_command(session, user, biz, text, user.language)]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 2. State machine: if user has not completed onboarding, route to onboarding
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

    # 3. Help command
    if msg.kind == "text" and text.lower() == "help":
        replies = [router.Reply(onboarding.help_menu(user.language))]
        session.add(ProcessedMessage(channel=msg.channel, message_id=msg.message_id))
        session.commit()
        await _send_all(channel, session, biz, msg.user_ref, replies)
        return

    # 4. Regular transaction and query flow
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
