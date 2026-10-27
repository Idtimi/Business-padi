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
    if lang == "yo":
        return (
            "Iranlowo Business Padi:\n"
            "- Se akosile tita: 'Mo ta 3 bags of rice fun Mama Ngozi, 40k each, cash'\n"
            "- Se akosile inawo: 'Mo ra fuel generator 5000 naira'\n"
            "- Owo ti a gba: 'Mama Ngozi ti san 20,000'\n"
            "- Awon to je gbese: 'Tani o je mi ni gbese julo?' tabi 'Elo ni gbogbo gbese mi?'\n"
            "- Wo tita: 'Elo ni mo ta lose yi?'\n"
            "- Yi eto pada: ko /settings\n"
            "- E le fi ohun ranse tabi ko o sile."
        )
    elif lang == "ha":
        return (
            "Taimakon Business Padi:\n"
            "- Rubuta sayarwa: 'Na sayar da buhu 3 na shinkafa ga Musa, 40k kowanne, kudi hannu'\n"
            "- Rubuta kudin da aka kashe: 'Na sayi man janareta na naira 5000'\n"
            "- Biyan bashi: 'Musa ya biya 20,000'\n"
            "- Masu bashi: 'Waye ya fi bina bashi?' ko 'Nawa ne jimillar bashi?'\n"
            "- Duba sayarwa: 'Nawa na sayar a wannan makon?'\n"
            "- Canza saiti: rubuta /settings\n"
            "- Za ka iya tura muryar murya ko ka rubuta."
        )
    elif lang == "ig":
        return (
            "Enyemaka Business Padi:\n"
            "- Dekoo ahia: 'E rerem akpa osikapa 3 nye Chidi, 40k nke obula, ego aka'\n"
            "- Dekoo ego mefuru: 'A zurum mmanu generator 5000 naira taa'\n"
            "- Ego anataara: 'Chidi akwula 20,000'\n"
            "- Ndi ji ugwo: 'Onye kacha ji m ugwo?' ma obu 'Ego ole ka ndi mmadu ji m?'\n"
            "- Lee ahia e rere: 'Ego ole ka m rere na izu a?'\n"
            "- Gbanwee ntọala: dee /settings\n"
            "- I nwere ike ikwu okwu n'olu ma obu dee ya ede."
        )
    elif lang == "pcm":
        return (
            "Business Padi Help:\n"
            "- Record sale: 'I sell 3 bags rice to Mama Ngozi, 40k each, cash'\n"
            "- Record expense: 'Today I buy fuel 5000 naira'\n"
            "- Customer payment: 'Mama Ngozi don pay 20,000'\n"
            "- Who owes: 'Who owe me pass?' or 'Wetin people owe me?'\n"
            "- Check sales: 'How I sell this week?'\n"
            "- Change details: type /settings\n"
            "- You fit talk voice note or type am."
        )
    return (
        "Business Padi Help:\n"
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
            f"- Language: {user.language or 'en'}\n"
            f"- Name: {biz.name or 'Not set'}\n"
            f"- Color: {biz.brand_color or 'Not set'}\n"
            f"- Address: {biz.address or 'Not set'}\n"
            f"- Phone: {biz.phone or 'Not set'}\n"
            f"- Bank: {biz.bank_details or 'Not set'}\n"
            f"- Footer: {biz.footer_note or 'Not set'}\n"
            f"- Logo: {logo_status}\n\n"
            f"To update, type e.g.:\n"
            f"/settings lang <en/pcm>\n"
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

    if subcmd in ("lang", "language") and val:
        raw_l = val.strip("<> ").lower()
        lang_map = {
            "en": "en", "english": "en",
            "pcm": "pcm", "pidgin": "pcm",
            "yo": "yo", "yoruba": "yo",
            "ha": "ha", "hausa": "ha",
            "ig": "ig", "igbo": "ig",
        }
        chosen = lang_map.get(raw_l, raw_l)
        user.language = chosen
        session.commit()
        return Reply(f"Language updated to {chosen}.")
    elif subcmd == "name" and val:
        clean_name = val.strip("<> ")
        biz.name = clean_name
        session.commit()
        return Reply(f"Shop name updated to {clean_name}.")
    elif subcmd == "color" and val:
        c_clean = val.strip("<> ").lower()
        PALETTE = {
            "orange": "#FF6A13", "navy": "#0D1F45", "blue": "#0D1F45",
            "green": "#16A34A", "gold": "#D97706", "amber": "#D97706",
            "teal": "#0D9488", "royal blue": "#2563EB", "purple": "#7C3AED",
            "red": "#DC2626", "pink": "#E11D48", "black": "#1E293B",
            "charcoal": "#334155", "slate": "#334155", "maroon": "#831843",
        }
        effective_color = PALETTE.get(c_clean, val.strip("<> "))
        biz.brand_color = effective_color
        session.commit()
        return Reply(f"Brand color updated to {effective_color}.")
    elif subcmd == "address" and val:
        clean_addr = val.strip("<> ")
        biz.address = clean_addr
        session.commit()
        return Reply(f"Shop address updated to {clean_addr}.")
    elif subcmd == "phone" and val:
        clean_ph = val.strip("<> ")
        biz.phone = clean_ph
        session.commit()
        return Reply(f"Shop phone updated to {clean_ph}.")
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

    # Check for staff join code at any point in onboarding
    clean_upper = text.strip().upper().replace("_", "-")
    candidate_code = None
    if clean_upper.startswith("JOIN-"):
        candidate_code = clean_upper.split()[0]
    elif clean_upper.startswith("/START JOIN-"):
        candidate_code = clean_upper.split()[1]
    elif clean_upper.startswith("JOIN ") and len(clean_upper.split()) == 2:
        candidate_code = f"JOIN-{clean_upper.split()[1]}"

    if candidate_code:
        from app.services import staff
        ok, reply_msg, st_rec = staff.join_business_with_code(session, user.id, candidate_code)
        if ok and st_rec:
            user.state = "DONE"
            session.commit()
            biz = session.get(Business, st_rec.business_id)
            welcome_text = (
                f"{reply_msg}\n\n"
                f"You are now registered as an attendant for {biz.name if biz else 'the shop'}.\n\n"
                f"You can record transactions for the shop anytime by voice note or text (e.g. 'Sold 2 bags rice for 90,000').\n\n"
                f"Type /commands to see available staff actions."
            )
            return [Reply(welcome_text)]
        else:
            return [Reply(reply_msg)]

    # State: NEW
    if user.state == "NEW":
        user.state = "CHOOSE_LANGUAGE"
        session.commit()
        welcome = (
            "Welcome to Business Padi!\n\n"
            "Choose your language to set up your shop, or if you were invited by your shop owner, enter your invite code (e.g. JOIN-123456):"
        )
        buttons = [
            ("lang:en", "English"),
            ("lang:pcm", "Nigerian Pidgin"),
            ("lang:yo", "Yorùbá"),
            ("lang:ha", "Hausa"),
            ("lang:ig", "Ìgbò"),
            ("lang:other", "Other language"),
        ]
        return [Reply(welcome, buttons=buttons, kind="confirm")]

    # State: CHOOSE_LANGUAGE
    if user.state == "CHOOSE_LANGUAGE":
        t_clean = text.lower().strip()
        if btn == "lang:other" or t_clean in ("6", "other", "other language"):
            from app.models import Event
            session.add(Event(
                user_id=user.id,
                name="language_demand",
                props={"source": "onboarding_other_button", "text": text},
            ))
            session.commit()
            prompt = "Thank you! We have recorded your interest in another language. In the meantime, please select one of the supported languages to continue:"
            buttons = [
                ("lang:en", "English"),
                ("lang:pcm", "Nigerian Pidgin"),
                ("lang:yo", "Yorùbá"),
                ("lang:ha", "Hausa"),
                ("lang:ig", "Ìgbò"),
            ]
            return [Reply(prompt, buttons=buttons, kind="confirm")]

        chosen_lang = "en"
        if btn == "lang:en" or t_clean in ("1", "english", "en"):
            chosen_lang = "en"
        elif btn == "lang:pcm" or t_clean in ("2", "pidgin", "pcm", "nigerian pidgin"):
            chosen_lang = "pcm"
        elif btn == "lang:yo" or t_clean in ("3", "yoruba", "yo", "yorùbá"):
            chosen_lang = "yo"
        elif btn == "lang:ha" or t_clean in ("4", "hausa", "ha"):
            chosen_lang = "ha"
        elif btn == "lang:ig" or t_clean in ("5", "igbo", "ig", "ìgbò"):
            chosen_lang = "ig"
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
        elif chosen_lang == "yo":
            prompt = (
                "Mo ma se akosile tita ati gbese ti e ba so, lati toju iwe isiro yin. "
                "A n pa ohun re lehin ti a ba ti gbo tan. "
                "E le mu jade tabi pa data yin re nigbakugba. Se e gba?"
            )
            buttons = [("consent:yes", "Mo gba"), ("consent:no", "Rara")]
        elif chosen_lang == "ha":
            prompt = (
                "Zan rubuta sayarwa da bashin da kake fada don kiyaye littafin kasuwancin ku. "
                "Ana share sautin bayan an gama ganewa. "
                "Kuna iya fitarwa ko share bayanan ku a kowane lokaci. Ka amince?"
            )
            buttons = [("consent:yes", "Na amince"), ("consent:no", "A'a")]
        elif chosen_lang == "ig":
            prompt = (
                "M ga-edekota ahia na ugwo ndi i kwuru iji debe akwukwo ego gi. "
                "A na-ehichapu olu gi ma aghotachaa ya. "
                "I nwere ike ibupura ma obu hichapu data gi oge obula. I kwenyere?"
            )
            buttons = [("consent:yes", "Ekwenyere m"), ("consent:no", "Mba")]
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
        t_clean = text.lower().strip()
        agreed = (btn == "consent:yes") or t_clean in {
            "1", "yes", "i agree", "agree", "ok", "yes i agree",
            "mo gba", "na amince", "ekwenyere m", "ekwenyere", "gba"
        }
        if agreed:
            user.consent_at = now_iso()
            user.state = "BUSINESS_NAME"
            session.commit()
            if user.language == "yo":
                prompt = "Kini oruko soobu yin?"
            elif user.language == "ha":
                prompt = "Mene ne sunan shagon ku?"
            elif user.language == "ig":
                prompt = "Kedu aha ulo ahia gi?"
            elif user.language == "pcm":
                prompt = "Wetin be your shop name?"
            else:
                prompt = "What is your shop name?"
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

        # Localized LOGO prompt
        if user.language == "yo":
            prompt = "Fi aami soobu yin (logo) ranse gege bi aworan, tabi ki e te [Fo o kọja]:"
            buttons = [("logo:skip", "Fo o kọja")]
        elif user.language == "ha":
            prompt = "Tura tambarin shagon ku a matsayin hoto, ko danna [Tsallake]:"
            buttons = [("logo:skip", "Tsallake")]
        elif user.language == "ig":
            prompt = "Ziga akara ulo ahia gi (logo) dika foto, ma obu pia [Mfegharịa]:"
            buttons = [("logo:skip", "Mfegharịa")]
        elif user.language == "pcm":
            prompt = "Send your logo as photo, or tap [Skip]:"
            buttons = [("logo:skip", "Skip")]
        else:
            prompt = "Send your logo as a photo, or tap [Skip]:"
            buttons = [("logo:skip", "Skip")]

        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: LOGO
    if user.state == "LOGO":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        skipped = (btn == "logo:skip") or text.lower().strip() in {"1", "skip", "no", "later", "fo o koja", "tsallake", "mfegharia"}

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

        # Localized BRAND COLOR prompt
        if user.language == "yo":
            prompt = (
                "Yan awo (colour) ti e fe ki o han lori iwe-owo (receipt) yin:\n\n"
                "[1] Osan / Orange (#FF6A13)\n"
                "[2] Bulu Dudu / Navy (#0D1F45)\n"
                "[3] Eweko / Forest Green (#16A34A)\n"
                "[4] Wura / Gold (#D97706)\n"
                "[5] Omi Okun / Teal (#0D9488)\n"
                "[6] Bulu / Royal Blue (#2563EB)\n"
                "[7] Pọpu / Royal Purple (#7C3AED)\n"
                "[8] Pupa / Crimson Red (#DC2626)\n"
                "[9] Pinki / Rose Pink (#E11D48)\n"
                "[10] Eedu / Slate Charcoal (#334155)\n\n"
                "Te nomba (1-10) tabi oruko awo naa."
            )
        elif user.language == "ha":
            prompt = (
                "Zabi launi don rasitocin ku:\n\n"
                "[1] Orange (#FF6A13)\n"
                "[2] Navy Blue (#0D1F45)\n"
                "[3] Kore / Forest Green (#16A34A)\n"
                "[4] Zinariya / Gold (#D97706)\n"
                "[5] Teal (#0D9488)\n"
                "[6] Shu'udi / Royal Blue (#2563EB)\n"
                "[7] Royal Purple (#7C3AED)\n"
                "[8] Ja / Crimson Red (#DC2626)\n"
                "[9] Rose Pink (#E11D48)\n"
                "[10] Slate Charcoal (#334155)\n\n"
                "Tura lamba (1-10) ko sunan launi."
            )
        elif user.language == "ig":
            prompt = (
                "Họrọ agba (colour) maka risiti gi:\n\n"
                "[1] Orange (#FF6A13)\n"
                "[2] Navy Blue (#0D1F45)\n"
                "[3] Akwukwo Nri / Forest Green (#16A34A)\n"
                "[4] Olaedo / Gold (#D97706)\n"
                "[5] Teal (#0D9488)\n"
                "[6] Anunu / Royal Blue (#2563EB)\n"
                "[7] Royal Purple (#7C3AED)\n"
                "[8] Uhie / Crimson Red (#DC2626)\n"
                "[9] Rose Pink (#E11D48)\n"
                "[10] Slate Charcoal (#334155)\n\n"
                "Ziga onuogugu (1-10) ma obu aha agba."
            )
        elif user.language == "pcm":
            prompt = (
                "Pick a color for your receipts:\n\n"
                "[1] Orange (Default #FF6A13)\n"
                "[2] Navy Blue (#0D1F45)\n"
                "[3] Forest Green (#16A34A)\n"
                "[4] Gold / Amber (#D97706)\n"
                "[5] Teal (#0D9488)\n"
                "[6] Royal Blue (#2563EB)\n"
                "[7] Royal Purple (#7C3AED)\n"
                "[8] Crimson Red (#DC2626)\n"
                "[9] Rose Pink (#E11D48)\n"
                "[10] Slate Charcoal (#334155)\n\n"
                "Reply with number (1-10), color name (e.g. 'Purple', 'Gold', 'Teal'), or any hex code (e.g. '#7C3AED')."
            )
        else:
            prompt = (
                "Pick a brand colour for your receipts:\n\n"
                "[1] Orange (Default #FF6A13)\n"
                "[2] Navy Blue (#0D1F45)\n"
                "[3] Forest Green (#16A34A)\n"
                "[4] Gold / Amber (#D97706)\n"
                "[5] Teal (#0D9488)\n"
                "[6] Royal Blue (#2563EB)\n"
                "[7] Royal Purple (#7C3AED)\n"
                "[8] Crimson Red (#DC2626)\n"
                "[9] Rose Pink (#E11D48)\n"
                "[10] Slate Charcoal (#334155)\n\n"
                "Reply with a number (1-10), a color name (e.g. 'Purple', 'Gold', 'Teal'), or any custom hex code (e.g. '#7C3AED')."
            )
        return [Reply(prompt)]

    # State: BRAND_COLOR
    if user.state == "BRAND_COLOR":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        t_clean = text.lower().strip()

        PALETTE = {
            "1": "#FF6A13", "orange": "#FF6A13", "osan": "#FF6A13",
            "2": "#0D1F45", "navy": "#0D1F45", "navy blue": "#0D1F45",
            "3": "#16A34A", "green": "#16A34A", "forest green": "#16A34A", "kore": "#16A34A", "eweko": "#16A34A",
            "4": "#D97706", "gold": "#D97706", "amber": "#D97706", "yellow": "#D97706", "wura": "#D97706", "zinariya": "#D97706", "olaedo": "#D97706",
            "5": "#0D9488", "teal": "#0D9488", "cyan": "#0D9488",
            "6": "#2563EB", "blue": "#2563EB", "royal blue": "#2563EB", "bulu": "#2563EB", "shuudi": "#2563EB", "anunu": "#2563EB",
            "7": "#7C3AED", "purple": "#7C3AED", "violet": "#7C3AED", "popu": "#7C3AED",
            "8": "#DC2626", "red": "#DC2626", "crimson": "#DC2626", "pupa": "#DC2626", "ja": "#DC2626", "uhie": "#DC2626",
            "9": "#E11D48", "pink": "#E11D48", "rose": "#E11D48", "pinki": "#E11D48",
            "10": "#334155", "black": "#334155", "charcoal": "#334155", "slate": "#334155", "eedu": "#334155",
            "brown": "#78350F", "maroon": "#831843", "wine": "#831843",
        }

        if btn.startswith("color:"):
            cname = btn.split(":", 1)[1].lower()
            color = PALETTE.get(cname, "#FF6A13")
        elif t_clean in PALETTE:
            color = PALETTE[t_clean]
        elif t_clean.startswith("#") and len(t_clean) in (4, 7):
            color = t_clean.upper()
        elif len(t_clean) == 6 and all(c in "0123456789abcdefABCDEF" for c in t_clean):
            color = f"#{t_clean.upper()}"
        elif text:
            color = text.strip()
        else:
            color = "#FF6A13"

        if biz:
            biz.brand_color = color

        user.state = "CONTACT"
        session.commit()

        # Localized CONTACT prompt
        if user.language == "yo":
            prompt = f"A ti yan awo {color}. E jowo ko adiresi ati nomba fon soobu yin fun iwe-owo (receipt) [Fo o kọja]:"
            buttons = [("contact:skip", "Fo o kọja")]
        elif user.language == "ha":
            prompt = f"An saita launi zuwa {color}. Adireshin shago da lambar waya don rasitocin ku? [Tsallake]:"
            buttons = [("contact:skip", "Tsallake")]
        elif user.language == "ig":
            prompt = f"Edobere agba dika {color}. Adreesi ulo ahia gi na akara ekwentị maka risiti gi? [Mfegharịa]:"
            buttons = [("contact:skip", "Mfegharịa")]
        elif user.language == "pcm":
            prompt = f"Color set to {color}. Shop address and phone for your receipts? [Skip]:"
            buttons = [("contact:skip", "Skip")]
        else:
            prompt = f"Brand color set to {color}. Shop address and phone for your receipts? [Skip]:"
            buttons = [("contact:skip", "Skip")]

        return [Reply(prompt, buttons=buttons, kind="confirm")]

    # State: CONTACT
    if user.state == "CONTACT":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        skipped = (btn == "contact:skip") or text.lower().strip() in {"1", "skip", "no", "later", "fo o koja", "tsallake", "mfegharia"}

        if not skipped and text and biz:
            biz.address = text

        user.state = "PRODUCTS"
        session.commit()

        if user.language == "yo":
            p_prompt = "Awon oja wo ni e n ta ni soobu yin? (apeere: Rice, Cement, Indomie, Ose) - ko won pelu aami koma, tabi ki e te [Fo o kọja]:"
            p_buttons = [("products:skip", "Fo o kọja")]
        elif user.language == "ha":
            p_prompt = "Wadanne kayayyaki kuke sayarwa a shagon ku? (misali: Shinkafa, Siminti, Indomie, Sabulu) - rubuta su da alamar koma, ko [Tsallake]:"
            p_buttons = [("products:skip", "Tsallake")]
        elif user.language == "ig":
            p_prompt = "Kedu ngwa ahia i na-ere na ulo ahia gi? (dika: Osikapa, Siment, Indomie, Ncha) - dee ha ma tinye koma, ma obu pia [Mfegharịa]:"
            p_buttons = [("products:skip", "Mfegharịa")]
        elif user.language == "pcm":
            p_prompt = "Wetin you dey sell for your shop? (e.g. Rice, Cement, Indomie, Sugar, Soap) - type dem with comma, or tap [Skip]:"
            p_buttons = [("products:skip", "Skip")]
        else:
            p_prompt = "What products or items do you sell in your shop? (e.g. Rice, Cement, Indomie, Sugar, Soap) - type items separated by commas, or tap [Skip]:"
            p_buttons = [("products:skip", "Skip")]

        return [Reply(p_prompt, buttons=p_buttons, kind="confirm")]

    # State: PRODUCTS
    if user.state == "PRODUCTS":
        biz = session.scalar(select(Business).where(Business.owner_user_id == user.id))
        skipped = (btn == "products:skip") or text.lower().strip() in {"1", "skip", "no", "later", "fo o koja", "tsallake", "mfegharia"}

        added_names = []
        if not skipped and text and biz:
            from app.services import stock
            raw_items = [x.strip() for x in text.replace("\n", ",").split(",") if x.strip() and len(x.strip()) > 1]
            for iname in raw_items:
                stock.get_or_create_item(session, biz.id, iname)
                added_names.append(iname.title())

        # Final transition to DONE
        user.state = "DONE"
        session.commit()

        shop_title = (biz.name if biz and biz.name else "Your shop")
        items_msg = f" Added {len(added_names)} products to your catalog ({', '.join(added_names)})." if added_names else ""
        if user.language == "yo":
            done_text = f"Gbogbo e ti se tan! Soobu {shop_title} ti wa ni soki.{items_msg}\n\nGbiyanju: so tita kan fun mi lori ohun (voice note) tabi restock pelu /restock."
        elif user.language == "ha":
            done_text = f"Komai ya shirya! Shagon {shop_title} ya shirya yanzu.{items_msg}\n\nGwada: gaya mini sayarwa daya ta hanyar sautin murya (voice note) ko /restock."
        elif user.language == "ig":
            done_text = f"Ihe niile adịla njikere! Ulo ahia {shop_title} adịla njikere ugbu a.{items_msg}\n\nNwaa ya: gwa m otu ahia site na ozi olu (voice note) ma obu /restock."
        elif user.language == "pcm":
            done_text = f"All set! {shop_title} don ready.{items_msg}\n\nTry am: tell me one sale by voice note, or restock with /restock."
        else:
            done_text = f"All set! {shop_title} is ready.{items_msg}\n\nTry: record a sale by voice note or text, restock with /restock, or type /dashboard."

        return [Reply(done_text)]

    return [Reply("Welcome back! How can I help you today?")]
