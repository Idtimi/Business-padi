"""Onboarding state machine and business settings management.

State flow:
NEW -> CHOOSE_LANGUAGE -> CONSENT -> BUSINESS_NAME -> LOGO (optional)
    -> BRAND_COLOR -> CONTACT (optional) -> SAMPLE_RECEIPT -> DONE
"""

from io import BytesIO
import logging
from pathlib import Path
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import IncomingMessage
from app.config import settings
from app.models import Business, User, now_iso
from app.pipeline.router import Reply

log = logging.getLogger(__name__)

MEDIA_DIR = Path("media/logos")


def save_and_resize_logo(image_bytes: bytes, business_id: int, max_size=(512, 512)) -> str:
    """Resize image with Pillow to fit within max_size and save to disk."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MEDIA_DIR / f"{business_id}.png"

    with Image.open(BytesIO(image_bytes)) as img:
        img = img.convert("RGBA")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        img.save(out_path, format="PNG")

    return str(out_path)


def help_menu(lang: str = "en") -> str:
    if lang == "pcm":
        return (
            "Voice-to-Ledger Help:\n"
            "- Record sale: 'I sell 3 bags rice to Mama Ngozi, 40k each, cash'\n"
            "- Record expense: 'Today I buy fuel 5000 naira'\n"
            "- Customer payment: 'Mama Ngozi don pay 20,000'\n"
            "- Who owes: 'Who owe me pass?' or 'Wetin people owe me?'\n"
            "- Check sales: 'How I sell this week?'\n"
            "- Change details: type /settings\n"
            "- You fit talk voice note or type am."
        )
    return (
        "Voice-to-Ledger Help:\n"
        "- Record sale: 'I sold 3 bags of rice to John, 40k each, cash'\n"
        "- Record expense: 'Bought generator fuel for 5,000 naira'\n"
        "- Customer repayment: 'John paid 20,000'\n"
        "- Who owes: 'Who owes me the most?' or 'What do people owe me?'\n"
        "- Check sales: 'How much did I sell this week?'\n"
        "- Change settings: type /settings\n"
        "- You can send voice notes or text."
    )


def handle_settings_command(session: Session, user: User, biz: Business | None,
                            text: str, lang: str = "en") -> Reply:
    """Handle /settings commands to view or update shop profile."""
    if biz is None:
        return Reply("Please complete shop setup first.")

    parts = text.strip().split(maxsplit=2)
    if len(parts) == 1:
        # Just "/settings" -> show current settings
        is_pidgin = (lang == "pcm")
        logo_status = "Uploaded" if biz.logo_path else "None"
        msg = (
            f"Shop Settings:\n"
            f"- Name: {biz.name or 'Not set'}\n"
            f"- Color: {biz.brand_color or 'Not set'}\n"
            f"- Address: {biz.address or 'Not set'}\n"
            f"- Phone: {biz.phone or 'Not set'}\n"
            f"- Bank: {biz.bank_details or 'Not set'}\n"
            f"- Footer: {biz.footer_note or 'Not set'}\n"
            f"- Logo: {logo_status}\n\n"
            f"To update, type e.g.:\n"
            f"/settings name <new name>\n"
            f"/settings color <color>\n"
            f"/settings address <address>\n"
            f"/settings phone <phone>\n"
            f"/settings bank <bank details>\n"
            f"/settings footer <footer note>"
        )
        return Reply(msg)

    subcmd = parts[1].lower()
    val = parts[2].strip() if len(parts) > 2 else ""

    if subcmd == "name" and val:
        biz.name = val
        session.commit()
        return Reply(f"Shop name updated to {val}.")
    elif subcmd == "color" and val:
        biz.brand_color = val
        session.commit()
        return Reply(f"Brand color updated to {val}.")
    elif subcmd == "address" and val:
        biz.address = val
        session.commit()
        return Reply(f"Shop address updated to {val}.")
    elif subcmd == "phone" and val:
        biz.phone = val
        session.commit()
        return Reply(f"Shop phone updated to {val}.")
    elif subcmd == "bank" and val:
        biz.bank_details = val
        session.commit()
        return Reply(f"Bank details updated.")
    elif subcmd == "footer" and val:
        biz.footer_note = val
        session.commit()
        return Reply(f"Receipt footer note updated.")

    return Reply("Unknown setting. Use e.g. /settings name My Shop")


async def handle_onboarding(channel, msg: IncomingMessage, user: User,
                            session: Session) -> list[Reply]:
    """Process an incoming message through the onboarding state machine."""
    text = (msg.text or "").strip()
    btn = (msg.button_id or "").strip()
    is_pidgin = (user.language == "pcm")

    # Global "help" command anytime
    if text.lower() == "help":
        return [Reply(help_menu(user.language))]

    # State: NEW
    if user.state == "NEW":
        user.state = "CHOOSE_LANGUAGE"
        session.commit()
        welcome = (
            "Welcome to Voice-to-Ledger. Abeg choose your language:"
            if is_pidgin else
            "Welcome to Voice-to-Ledger. Choose your language:"
        )
        buttons = [
            ("lang:pcm", "Nigerian Pidgin"),
            ("lang:en", "English"),
            ("lang:other", "Other language"),
        ]
        return [Reply(welcome, buttons=buttons, kind="confirm")]

    # State: CHOOSE_LANGUAGE
    if user.state == "CHOOSE_LANGUAGE":
        if btn == "lang:other" or "other" in text.lower():
            from app.models import Event
            session.add(Event(
                user_id=user.id,
                name="language_demand",
                props={"source": "onboarding_other_button", "text": text},
            ))
            session.commit()
            prompt = (
                "Thank you! We don note say you want another language. While we dey work on am, which one you wan use now?"
                if is_pidgin else
                "Thank you! We have recorded your interest in additional languages. In the meantime, which would you prefer?"
            )
            buttons = [
                ("lang:pcm", "Nigerian Pidgin"),
                ("lang:en", "English"),
            ]
            return [Reply(prompt, buttons=buttons, kind="confirm")]

        chosen_lang = "pcm"
        if btn == "lang:en" or "english" in text.lower() or text.lower() == "en":
            chosen_lang = "en"
        elif btn == "lang:pcm" or "pidgin" in text.lower() or text.lower() == "pcm":
            chosen_lang = "pcm"
        elif btn.startswith("lang:"):
            chosen_lang = btn.replace("lang:", "")
            from config.languages import resolve_ui_language
            chosen_lang = resolve_ui_language(chosen_lang)

        user.language = chosen_lang
        # Default speech_language matches chosen language
        state_data = user.state_data or {}
        state_data["speech_language"] = chosen_lang
        user.state_data = state_data
        user.state = "CONSENT"
        session.commit()

        if chosen_lang == "pcm":
            prompt = (
                "I go record the sales and debts wey you tell me, to keep your books. "
                "Audio is deleted after I understand it. "
                "You fit export or delete your data anytime. You agree?"
            )
            buttons = [("consent:yes", "I agree"), ("consent:no", "No")]
        else:
            prompt = (
                "I will record the sales and debts you tell me, to keep your books. "
                "Audio is deleted after I understand it. "
                "You can export or delete your data any time. Do you agree?"
            )
            buttons = [("consent:yes", "Yes"), ("consent:no", "No")]

        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: CONSENT
    if user.state == "CONSENT":
        agreed = (btn == "consent:yes") or text.lower() in {"yes", "i agree", "agree", "ok", "yes i agree"}
        if agreed:
            user.consent_at = now_iso()
            user.state = "BUSINESS_NAME"
            session.commit()
            prompt = "Wetin be your shop name?" if is_pidgin else "What is your shop name?"
            return [Reply(prompt)]
        else:
            prompt = (
                "Without your agreement, I cannot record your books. Whenever you are ready, just say hello."
                if not is_pidgin else
                "Without your agreement, I no fit record your books. Anytime you ready, just say hello."
            )
            return [Reply(prompt)]

    # State: BUSINESS_NAME
    if user.state == "BUSINESS_NAME":
        shop_name = text or "My Shop"
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        if biz is None:
            biz = Business(owner_user_id=user.id, name=shop_name)
            session.add(biz)
        else:
            biz.name = shop_name

        user.state = "LOGO"
        session.commit()

        prompt = (
            "Send your logo as a photo, or tap [Skip]"
            if not is_pidgin else
            "Send your logo as photo, or tap [Skip]"
        )
        buttons = [("logo:skip", "Skip")]
        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: LOGO
    if user.state == "LOGO":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        skipped = (btn == "logo:skip") or text.lower() in {"skip", "no", "later"}

        if msg.kind == "image" and msg.media_ref:
            try:
                img_data, _ = await channel.download_media(msg.media_ref)
                logo_path = save_and_resize_logo(img_data, biz.id if biz else user.id)
                if biz:
                    biz.logo_path = logo_path
            except Exception:
                log.exception("Failed to process logo image")

        user.state = "BRAND_COLOR"
        session.commit()

        prompt = (
            "Pick a brand colour for your receipts:"
            if not is_pidgin else
            "Pick a color for your receipts:"
        )
        buttons = [("color:green", "Green"), ("color:blue", "Blue"), ("color:purple", "Purple")]
        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: BRAND_COLOR
    if user.state == "BRAND_COLOR":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        color = "green"
        if btn.startswith("color:"):
            color = btn.split(":", 1)[1]
        elif text:
            color = text.lower()

        if biz:
            biz.brand_color = color

        user.state = "CONTACT"
        session.commit()

        prompt = (
            "Shop address and phone for your receipts? [Skip]"
            if not is_pidgin else
            "Shop address and phone for your receipts? [Skip]"
        )
        buttons = [("contact:skip", "Skip")]
        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: CONTACT
    if user.state == "CONTACT":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        skipped = (btn == "contact:skip") or text.lower() in {"skip", "no", "later"}

        if not skipped and text and biz:
            biz.address = text

        # Final transition to DONE
        user.state = "DONE"
        session.commit()

        shop_title = (biz.name if biz and biz.name else "Your shop")
        done_text = (
            f"All set! {shop_title} is ready.\n\nTry: tell me one sale by voice note."
            if not is_pidgin else
            f"All set! {shop_title} don ready.\n\nTry am: tell me one sale by voice note."
        )
        return [Reply(done_text)]

    return [Reply("Welcome back! How can I help you today?")]
