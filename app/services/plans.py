"""Plans and entitlements service.

Centralizes all plan feature gating, monthly voice quotas, usage counters,
and upgrade prompt generation.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, UsageCounter
from config.plans import (
    DEFAULT_PLAN,
    ENTRY_PACK_COUNT,
    ENTRY_PACK_PRICE_KOBO,
    PLANS,
    PlanDefinition,
    get_plan,
)


def get_month_key(dt: datetime | None = None) -> str:
    """Return the current month formatted as YYYY-MM."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def allowed(business: Business | str | None, feature: str) -> bool:
    """Check if the given business or plan name is entitled to access a feature.

    Supported features:
    - invoices / invoice
    - quotations / quote
    - receipts / receipt
    - branded_receipts
    - custom_receipts
    - statements / statement
    - scheduled_reminders / reminders
    - smart_reminders
    - stock_tracking / stock
    - full_stock
    - daily_summary
    - pnl
    - expense_categories
    - staff_accounts / staff
    - financial_profile
    """
    if isinstance(business, Business):
        plan_key = business.plan
    elif isinstance(business, str):
        plan_key = business
    else:
        plan_key = DEFAULT_PLAN

    plan = get_plan(plan_key)
    f = feature.lower().strip()

    if f in ("invoices", "invoice"):
        return plan.invoices
    if f in ("quotations", "quotation", "quote"):
        return plan.quotations
    if f in ("receipts", "receipt"):
        return True  # all plans can issue receipts (starter has basic footer)
    if f in ("branded_receipts", "branded_receipt"):
        return plan.receipts in ("branded", "custom")
    if f in ("custom_receipts", "custom_receipt"):
        return plan.receipts == "custom"
    if f in ("statements", "statement"):
        return plan.statements
    if f in ("scheduled_reminders", "reminders", "scheduled_reminder"):
        return plan.debt_reminders in ("scheduled", "smart")
    if f in ("smart_reminders", "smart_reminder"):
        return plan.debt_reminders == "smart"
    if f in ("stock_tracking", "stock"):
        return bool(plan.stock_tracking)
    if f in ("full_stock",):
        return plan.stock_tracking == "full"
    if f in ("daily_summary",):
        return "daily" in plan.summaries
    if f in ("pnl", "profit_and_loss"):
        return "pnl" in plan.reports
    if f in ("expense_categories",):
        return "expense_categories" in plan.reports
    if f in ("staff_accounts", "staff"):
        return plan.staff_accounts > 0
    if f in ("financial_profile",):
        return bool(plan.financial_profile)
    if f in ("extra_packs",):
        return plan.extra_packs_allowed

    return False


def get_or_create_usage_counter(session: Session, business_id: int, month: str | None = None) -> UsageCounter:
    """Fetch or insert the UsageCounter row for a business and month."""
    if month is None:
        month = get_month_key()

    counter = session.scalar(
        select(UsageCounter).where(
            UsageCounter.business_id == business_id,
            UsageCounter.month == month,
        )
    )
    if counter is None:
        counter = UsageCounter(
            business_id=business_id,
            month=month,
            voice_entries=0,
            extra_entries=0,
        )
        session.add(counter)
        session.flush()
    return counter


def get_voice_usage(session: Session, business_id: int, month: str | None = None) -> tuple[int, int]:
    """Return (voice_entries_used, extra_entries_available) for the month."""
    counter = get_or_create_usage_counter(session, business_id, month)
    return counter.voice_entries, counter.extra_entries


def can_use_voice_entry(session: Session, business: Business) -> tuple[bool, int, int]:
    """Check if the business can record another voice entry this month.

    Returns:
        (is_allowed, current_usage, total_capacity)
    """
    month = get_month_key()
    counter = get_or_create_usage_counter(session, business.id, month)
    plan = get_plan(business.plan)
    total_cap = plan.monthly_voice_entries + counter.extra_entries

    is_allowed = counter.voice_entries < total_cap
    return is_allowed, counter.voice_entries, total_cap


def increment_voice_usage(session: Session, business_id: int) -> int:
    """Atomically increment the monthly voice entry counter."""
    month = get_month_key()
    counter = get_or_create_usage_counter(session, business_id, month)
    counter.voice_entries += 1
    session.flush()
    return counter.voice_entries


def upgrade_prompt_for_feature(feature: str, lang: str = "en") -> tuple[str, list[tuple[str, str]]]:
    """Generate localized upgrade prompt text and buttons when an entitlement is blocked."""
    f = feature.lower().strip()
    if lang == "pcm":
        if f in ("invoices", "invoice", "quotations", "quote"):
            text = (
                "Invoice and quotation dey for Trader plan (₦5,000/month).\n\n"
                "You fit still send normal receipt for any sale, or upgrade make you get invoice!"
            )
            buttons = [
                ("upgrade_trader", "Upgrade to Trader"),
                ("send_receipt", "Send Receipt"),
            ]
        else:
            text = (
                f"Dis feature ({feature}) need upgrade to Trader or Business plan.\n\n"
                "Upgrade your account make you fit use am!"
            )
            buttons = [
                ("upgrade_trader", "Upgrade to Trader"),
                ("view_plans", "View Plans"),
            ]
    else:
        if f in ("invoices", "invoice", "quotations", "quote"):
            text = (
                "Invoices and quotations are available on the Trader plan (₦5,000/month).\n\n"
                "You can still send receipts for any sale, or upgrade to issue invoices!"
            )
            buttons = [
                ("upgrade_trader", "Upgrade to Trader"),
                ("send_receipt", "Send Receipt"),
            ]
        else:
            text = (
                f"This feature ({feature}) requires the Trader or Business plan.\n\n"
                "Upgrade your account to unlock full access!"
            )
            buttons = [
                ("upgrade_trader", "Upgrade to Trader"),
                ("view_plans", "View Plans"),
            ]
    return text, buttons


def upgrade_prompt_for_voice_quota(
    current: int,
    cap: int,
    plan_key: str = "starter",
    lang: str = "en",
) -> tuple[str, list[tuple[str, str]]]:
    """Generate localized quota refusal message with upgrade and pack options."""
    if lang == "pcm":
        text = (
            f"You don reach your {cap} voice note limit for dis month.\n\n"
            "Buy extra pack (+25 notes for ₦2,500) or upgrade to Trader to get 100 notes every month!"
        )
        buttons = [
            ("upgrade_trader", "Upgrade to Trader"),
            ("buy_pack", "Buy 25 Entries"),
        ]
    else:
        text = (
            f"You have reached your limit of {cap} voice entries for this month.\n\n"
            "Buy an extra pack (+25 entries for ₦2,500) or upgrade to Trader for 100 entries/month!"
        )
        buttons = [
            ("upgrade_trader", "Upgrade to Trader"),
            ("buy_pack", "Buy 25 Entries"),
        ]
    return text, buttons
