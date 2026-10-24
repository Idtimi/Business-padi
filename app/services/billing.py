"""Billing service: plan activation, test-mode billing, packs, and status.

Per Phase 6 of the Voice-to-Ledger specification:
- Manual "activate plan" admin command
- Test-mode flag (no live payment provider yet)
- Entry pack purchases (+25 voice entries)
- Plan downgrades that keep historical data and documents fully readable.
"""

from datetime import datetime, timedelta, timezone
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Business, UsageCounter, UsageLog, now_iso
from app.money import fmt_naira
from config.plans import (
    DEFAULT_PLAN,
    ENTRY_PACK_COUNT,
    ENTRY_PACK_PRICE_KOBO,
    PLANS,
    get_plan,
)
from app.services.plans import get_month_key, get_or_create_usage_counter

# Test-mode flag: allows simulated instant upgrades and pack purchases
BILLING_TEST_MODE: bool = os.getenv("BILLING_TEST_MODE", "true").lower() in ("true", "1", "yes")


def activate_plan(session: Session, business_id: int, plan_key: str, months: int = 1) -> Business:
    """Manually activate or upgrade a business's plan."""
    plan_key = plan_key.lower().strip()
    if plan_key not in PLANS:
        raise ValueError(f"Unknown plan: '{plan_key}'. Valid plans: {list(PLANS.keys())}")

    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    old_plan = biz.plan
    biz.plan = plan_key

    # Calculate renewal date
    now = datetime.now(timezone.utc)
    renews_at = now + timedelta(days=30 * months)
    biz.plan_renews_at = renews_at.isoformat()

    # Log audit event
    session.add(
        AuditLog(
            business_id=biz.id,
            actor_user_id=biz.owner_user_id,
            action="activate_plan",
            ref=f"{old_plan}->{plan_key} for {months}m",
        )
    )
    session.commit()
    return biz


def downgrade_plan(session: Session, business_id: int, plan_key: str = "starter") -> Business:
    """Downgrade a business plan. Preserves all past transactions and documents."""
    plan_key = plan_key.lower().strip()
    if plan_key not in PLANS:
        raise ValueError(f"Unknown plan: '{plan_key}'")

    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    old_plan = biz.plan
    biz.plan = plan_key
    if plan_key == "starter":
        biz.plan_renews_at = None

    session.add(
        AuditLog(
            business_id=biz.id,
            actor_user_id=biz.owner_user_id,
            action="downgrade_plan",
            ref=f"{old_plan}->{plan_key}",
        )
    )
    session.commit()
    return biz


def buy_entry_pack(session: Session, business_id: int, pack_count: int = ENTRY_PACK_COUNT) -> int:
    """Purchase an extra pack of voice entries for the current month."""
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    month = get_month_key()
    counter = get_or_create_usage_counter(session, business_id, month)
    counter.extra_entries += pack_count

    # Log usage record
    session.add(
        UsageLog(
            business_id=business_id,
            kind="pack",
            units=float(pack_count),
            cost_estimate_kobo=ENTRY_PACK_PRICE_KOBO,
        )
    )
    session.add(
        AuditLog(
            business_id=business_id,
            actor_user_id=biz.owner_user_id,
            action="buy_entry_pack",
            ref=f"+{pack_count} entries for {month}",
        )
    )
    session.commit()
    return counter.extra_entries


def get_billing_status(session: Session, business: Business) -> dict:
    """Return dictionary with current plan, usage, and cap."""
    plan = get_plan(business.plan)
    month = get_month_key()
    counter = get_or_create_usage_counter(session, business.id, month)
    total_cap = plan.monthly_voice_entries + counter.extra_entries
    remaining = max(0, total_cap - counter.voice_entries)

    return {
        "plan_key": plan.key,
        "plan_name": plan.name,
        "price_kobo": plan.price_kobo,
        "renews_at": business.plan_renews_at,
        "month": month,
        "voice_used": counter.voice_entries,
        "voice_base_cap": plan.monthly_voice_entries,
        "extra_entries": counter.extra_entries,
        "total_cap": total_cap,
        "remaining": remaining,
    }


def format_plan_status_message(status: dict, lang: str = "en") -> tuple[str, list[tuple[str, str]]]:
    """Format human-readable billing status and action buttons."""
    plan_name = status["plan_name"]
    used = status["voice_used"]
    cap = status["total_cap"]
    rem = status["remaining"]
    price_str = fmt_naira(status["price_kobo"])

    if lang == "pcm":
        msg = (
            f"*Your Plan: {plan_name}*\n"
            f"Monthly Price: {price_str}\n"
            f"Voice Notes Used: {used} of {cap}\n"
            f"Remaining dis month: {rem}\n"
        )
        if status["extra_entries"] > 0:
            msg += f"(Includes +{status['extra_entries']} extra pack notes)\n"

        buttons = []
        if status["plan_key"] == "starter":
            buttons.append(("upgrade_trader", "Upgrade to Trader"))
            buttons.append(("buy_pack", "Buy 25 Entries"))
        elif status["plan_key"] == "trader":
            buttons.append(("upgrade_business", "Upgrade Business"))
            buttons.append(("buy_pack", "Buy 25 Entries"))
    else:
        msg = (
            f"*Your Plan: {plan_name}*\n"
            f"Monthly Fee: {price_str}\n"
            f"Voice Entries Used: {used} of {cap}\n"
            f"Remaining this month: {rem}\n"
        )
        if status["extra_entries"] > 0:
            msg += f"(Includes +{status['extra_entries']} extra pack entries)\n"

        buttons = []
        if status["plan_key"] == "starter":
            buttons.append(("upgrade_trader", "Upgrade to Trader"))
            buttons.append(("buy_pack", "Buy 25 Entries"))
        elif status["plan_key"] == "trader":
            buttons.append(("upgrade_business", "Upgrade Business"))
            buttons.append(("buy_pack", "Buy 25 Entries"))

    return msg, buttons
