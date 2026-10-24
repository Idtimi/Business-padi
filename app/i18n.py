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
        "reminder_gentle": "Hello {customer}, gentle reminder about your balance of {amount} with {business}. Thank you!",
        "reminder_firm": "Hello {customer}, this is a reminder that your payment of {amount} to {business} is overdue. Please settle as soon as possible. Thank you.",
        "reminder_final": "Hello {customer}, urgent notice regarding overdue balance of {amount} to {business}. Please pay today to keep your credit open.",
        "reminder_digest_header": "🔔 *Debt Reminders for Today* ({count}):\n",
        "reminder_digest_item": "• {customer} owes {amount} (Due: {due})\nForward to customer: {link}\n",
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
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = STRINGS.get(lang, {}).get(key) or STRINGS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text
