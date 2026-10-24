"""Multi-language UI strings and translation resolution for BusinessPadi.

Rules:
- Unreviewed languages (ui_reviewed=False) fall back to English/Pidgin.
- Missing keys fall back to English.
- Formats interpolated kwargs cleanly.
"""

from typing import Any

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "sale": "Sale", "expense": "Expense", "payment_received": "Payment received",
        "restock": "Restock", "customer": "Customer", "supplier": "Supplier",
        "paid": "Paid", "owes": "Owes", "due": "due", "total": "Total",
        "save": "Save", "edit": "Edit", "cancel": "Cancel",
        "saved": "Saved.", "cancelled": "Cancelled.",
        "edit_prompt": "Okay. Please send the correct one.",
        "not_yet": "I understood, but I cannot do that one yet.",
        "try_again": "Sorry, something went wrong. Please try again.",
        "balance_now": "{customer} now owes {amount}.",
        "reminder_gentle": "Hello {customer}, gentle reminder about your balance of {amount} with {business}. Thank you!",
        "reminder_firm": "Hello {customer}, this is a reminder that your payment of {amount} to {business} is overdue. Please settle as soon as possible. Thank you.",
        "reminder_final": "Hello {customer}, urgent notice regarding overdue balance of {amount} to {business}. Please pay today to keep your credit open.",
        "reminder_digest_header": "🔔 *Debt Reminders for Today* ({count}):\n",
        "reminder_digest_item": "• {customer} owes {amount} (Due: {due})\nForward to customer: {link}\n",
        "assist_fallback": "We are working on adding full voice support for {language}! For now, please type your entry or send voice in English/Pidgin.",
        "repeat_prompt": "I could not hear that clearly. Please repeat or type it.",
        "strict_confirm_amount": "Amount: {amount}\nPlease confirm this amount is exact before saving.",
        "other_language": "Other language",
        "language_recorded": "Thank you! We have recorded your interest in {language}.",
    },
    "pcm": {
        "sale": "Sale", "expense": "Expense", "payment_received": "Money wey dem pay",
        "restock": "New stock", "customer": "Customer", "supplier": "Supplier",
        "paid": "Dem pay", "owes": "Dem owe", "due": "pay on", "total": "Total",
        "save": "Save", "edit": "Change am", "cancel": "Cancel",
        "saved": "I don save am.", "cancelled": "I don cancel am.",
        "edit_prompt": "Ok. Abeg talk the correct one again.",
        "not_yet": "I hear you, but I never fit do that one yet.",
        "try_again": "Sorry, wahala dey. Abeg try again.",
        "balance_now": "{customer} dey owe you {amount} now.",
        "reminder_gentle": "Hello {customer}, abeg remember your balance of {amount} for {business}. Thank you!",
        "reminder_firm": "Hello {customer}, your payment of {amount} for {business} don pass due date. Abeg try pay am quick. Thank you.",
        "reminder_final": "Hello {customer}, urgent payment notice for your balance of {amount} for {business}. Abeg pay today make your credit still dey open.",
        "reminder_digest_header": "🔔 *Debt Reminders for Today* ({count}):\n",
        "reminder_digest_item": "• {customer} dey owe {amount} (Due: {due})\nSend give customer: {link}\n",
        "assist_fallback": "We dey work to support {language} well-well! For now, abeg type or speak English/Pidgin.",
        "repeat_prompt": "I no hear wetin you talk clearly. Abeg repeat or type am.",
        "strict_confirm_amount": "Amount: {amount}\nAbeg check this amount well-well before you save am.",
        "other_language": "Other language",
        "language_recorded": "Thank you! We don record say you want {language}.",
    },
    "yo": {
        "sale": "Tita", "expense": "Inawo", "payment_received": "Owo ti a gba",
        "restock": "Eru titun", "customer": "Onibara", "supplier": "Olupese",
        "paid": "San", "owes": "Je gbese", "due": "ojo sisan", "total": "Lapapo",
        "save": "Fi pamo", "edit": "Tun se", "cancel": "Fagile",
        "saved": "A ti fi pamo.", "cancelled": "A ti fagile e.",
        "edit_prompt": "O da. E jowo so eyi to to leekan si.",
        "not_yet": "Mo gbo yin, sugbon nko le se eleyi lowolowo.",
        "try_again": "E ma binu, wahala kan sele. E jowo gbiyanju leekan si.",
        "balance_now": "{customer} je yin ni {amount} lowolowo.",
        "reminder_gentle": "Bawo {customer}, e ranti owo {amount} to ku fun {business}. E se!",
        "reminder_firm": "Bawo {customer}, owo {amount} ti e je {business} ti koja ojo sisan. E jowo san an kiakia. E se.",
        "reminder_final": "Bawo {customer}, ifitonile pataki nipa owo {amount} ti e je {business}. E jowo san leni. E se.",
        "reminder_digest_header": "🔔 *Awon Gbese Oni* ({count}):\n",
        "reminder_digest_item": "• {customer} je {amount} (Ojo: {due})\nFi ranse si onibara: {link}\n",
        "assist_fallback": "A n sise lati fi ede {language} kun eto yi! Fun bayi, e ko oro yin sile tabi ki e so Pidgin/Gehesi.",
        "repeat_prompt": "Nko gbo ohun ti e so daadaa. E jowo tun so tabi ki e ko sile.",
        "strict_confirm_amount": "Iye owo: {amount}\nE jowo ri daju pe iye owo yi pe dada ki e to fipamo.",
        "other_language": "Ede miran",
        "language_recorded": "E se! A ti ko akosile wipe e fe ede {language}.",
    },
    "ha": {
        "sale": "Sayarwa", "expense": "Kudi da aka kashe", "payment_received": "Kudi da aka biya",
        "restock": "Sabuwar kaya", "customer": "Abokin ciniki", "supplier": "Mai kawo kaya",
        "paid": "An biya", "owes": "Bashi", "due": "ranar biya", "total": "Jimilla",
        "save": "Ajiye", "edit": "Gyara", "cancel": "Soke",
        "saved": "An ajiye.", "cancelled": "An soke.",
        "edit_prompt": "To. Da fatan zaka sake tura wanda yake daidai.",
        "not_yet": "Na gane, amma ban iya yin wannan ba tukuna.",
        "try_again": "Yi hakuri, an samu matsala. Da fatan za a sake gwadawa.",
        "balance_now": "{customer} yana bin {amount} a yanzu.",
        "reminder_gentle": "Sannu {customer}, tunatarwa kan bashin {amount} na {business}. Mun gode!",
        "reminder_firm": "Sannu {customer}, bashin {amount} na {business} ya wuce lokacin biya. Da fatan za a biya da wuri. Mun gode.",
        "reminder_final": "Sannu {customer}, muhimmiyar sanarwa game da bashin {amount} na {business}. Da fatan za a biya yau.",
        "reminder_digest_header": "🔔 *Tunatarwar Bashi na Yau* ({count}):\n",
        "reminder_digest_item": "• {customer} yana bin {amount} (Kwanan wata: {due})\nTura wa abokin ciniki: {link}\n",
        "assist_fallback": "Muna kokarin kawo cikakken tallafi ga yaren {language}! A yanzu, da fatan za ka rubuta ko ka yi magana da Pidgin/Turanci.",
        "repeat_prompt": "Ban ji abin da ka ce sosai ba. Da fatan za ka sake fada ko ka rubuta shi.",
        "strict_confirm_amount": "Adadin: {amount}\nDa fatan za a tabbatar da wannan adadin kafin a ajiye.",
        "other_language": "Wani yare",
        "language_recorded": "Mun gode! Mun rubuta sha'awar ku ga yaren {language}.",
    },
    "ig": {
        "sale": "Ahia", "expense": "Ego mefuru", "payment_received": "Ego anataara",
        "restock": "Ngwa ahia ohuru", "customer": "Onye ahia", "supplier": "Onye na-ebunye ngwa ahia",
        "paid": "A kwuola", "owes": "Ji ugwo", "due": "ubochi iji kwuo", "total": "Ngụkọta",
        "save": "Chekwaa", "edit": "Gbanwee", "cancel": "Kagbuo",
        "saved": "E chekwaala ya.", "cancelled": "A kagbuola ya.",
        "edit_prompt": "Ndo, ziga nke ziri ezi ozo.",
        "not_yet": "A nurum ihe i kwuru, mana enwebeghim ike ime ya ugbu a.",
        "try_again": "Ndo, enwere nsogbu. Biko nwaa ozo.",
        "balance_now": "{customer} ji ugwo {amount} ugbu a.",
        "reminder_gentle": "Nno {customer}, ncheta maka ugwo {amount} i ji {business}. Daalu!",
        "reminder_firm": "Nno {customer}, ugwo {amount} i ji {business} agafewo ubochi ya. Biko kwuo ya ngwa ngwa. Daalu.",
        "reminder_final": "Nno {customer}, ozi di mkpa maka ugwo {amount} i ji {business}. Biko kwuo ya taa.",
        "reminder_digest_header": "🔔 *Ncheta Ugwo Maka Taa* ({count}):\n",
        "reminder_digest_item": "• {customer} ji ugwo {amount} (Ubochi: {due})\nZiga onye ahia: {link}\n",
        "assist_fallback": "Anyi na-aru oru iji nweta nkwado zuru oke maka asusu {language}! Ugbu a, biko dee ya ede ma obu kwuo Pidgin/Bekee.",
        "repeat_prompt": "Anughim ihe ikwuru nke oma. Biko kwuo ya ozo ma obu dee ya.",
        "strict_confirm_amount": "Onu ego: {amount}\nBiko hu na onu ego a ziri ezi tupu i chekwaa ya.",
        "other_language": "Asusu ozo",
        "language_recorded": "Daalu! Anyi edekoolo na i choro asusu {language}.",
    },
}


def t(lang: str, key: str, **kwargs: Any) -> str:
    """Translates key to target language.

    Falls back to English if the language is unreviewed or if the key is missing.
    """
    try:
        from config.languages import LANGUAGES
        lang_meta = LANGUAGES.get(lang, {})
        # If language is not reviewed by a native speaker, fall back to English/Pidgin
        if not lang_meta.get("ui_reviewed", False) and lang not in ("en", "pcm"):
            effective_lang = "en"
        else:
            effective_lang = lang
    except Exception:
        effective_lang = lang if lang in ("en", "pcm") else "en"

    text = STRINGS.get(effective_lang, {}).get(key)
    if not text:
        text = STRINGS.get("en", {}).get(key, key)

    return text.format(**kwargs) if kwargs else text
