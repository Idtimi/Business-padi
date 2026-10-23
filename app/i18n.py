STRINGS = {
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
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(lang, {}).get(key) or STRINGS["en"][key]
    return text.format(**kwargs) if kwargs else text