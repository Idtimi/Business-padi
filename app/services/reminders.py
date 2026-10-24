"""Debt reminders service: scheduling, dispatch, trader-forwarding links, and controls.

Per Phase 7 of the Voice-to-Ledger specification:
- Generates reminder schedules for credit sales with a due date:
  * 1 day before (gentle)
  * on the day (gentle)
  * 3 days after (firm)
  * 7 days after (firm)
  * then weekly (+14d, +21d, +28d) up to 7 reminders total (final).
- APScheduler integration (timezone Africa/Lagos, send window 08:00 to 20:00 only).
  NOTE: APScheduler runs in-process and requires a single worker to avoid duplicate
  job executions across multiple server instances.
- Part F5 Route B: sends the trader a ready message plus a wa.me chat link with
  prefilled text in the customer's language/tone.
- Sends at most ONE reminder digest message per trader per day to control WhatsApp costs.
- Stops reminders when debt is paid, paused, customer opted out, or limit reached.
- Idempotent across server restarts.
- Commands: remind now, pause, stop, resume for customer.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import logging
from typing import Any
from urllib.parse import quote_plus

try:
    from zoneinfo import ZoneInfo
    LAGOS_TZ = ZoneInfo("Africa/Lagos")
except Exception:
    LAGOS_TZ = timezone(timedelta(hours=1))

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.i18n import t
from app.models import Business, Customer, Reminder, Transaction, User, WaMessageLog, now_iso
from app.money import fmt_naira
from app.services import documents, ledger, plans

log = logging.getLogger(__name__)

# Max scheduled reminders per credit transaction
MAX_REMINDERS_PER_TX = 7

# Daily send window in Lagos local time: 08:00 to 20:00
SEND_WINDOW_START_HOUR = 8
SEND_WINDOW_END_HOUR = 20


def get_lagos_now(dt: datetime | None = None) -> datetime:
    """Return the given datetime or current time localized to Africa/Lagos (WAT, UTC+1)."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LAGOS_TZ)


def is_in_send_window(dt: datetime | None = None) -> bool:
    """Return True if the current Lagos time is strictly within 08:00 to 20:00."""
    lagos_dt = get_lagos_now(dt)
    return SEND_WINDOW_START_HOUR <= lagos_dt.hour < SEND_WINDOW_END_HOUR


def schedule_reminders_for_sale(
    session: Session,
    business_id: int,
    customer_id: int,
    transaction_id: int,
    due_date_val: str | date,
) -> list[Reminder]:
    """Create the scheduled reminder rows for a credit sale with a due date.

    Schedule offsets:
    - 1 day before (gentle)
    - on the day (gentle)
    - 3 days after (firm)
    - 7 days after (firm)
    - 14 days after (final)
    - 21 days after (final)
    - 28 days after (final)
    """
    if isinstance(due_date_val, str):
        try:
            due_d = date.fromisoformat(due_date_val)
        except ValueError:
            return []
    else:
        due_d = due_date_val

    # Check if customer opted out
    cust = session.get(Customer, customer_id)
    if cust and cust.opted_out:
        log.info("Customer %s opted out; skipping reminder schedule", customer_id)
        return []

    # Check if reminders already exist for this transaction (idempotent)
    existing = list(
        session.scalars(
            select(Reminder).where(
                Reminder.business_id == business_id,
                Reminder.transaction_id == transaction_id,
            )
        )
    )
    if existing:
        return existing

    schedule_offsets = [
        (-1, "gentle"),
        (0, "gentle"),
        (3, "firm"),
        (7, "firm"),
        (14, "final"),
        (21, "final"),
        (28, "final"),
    ]

    created: list[Reminder] = []
    for offset_days, tone in schedule_offsets[:MAX_REMINDERS_PER_TX]:
        rem_date = due_d + timedelta(days=offset_days)
        # Schedule to send at 09:00 Lagos time (well inside 08:00 - 20:00 window)
        send_at = f"{rem_date.isoformat()}T09:00:00"
        r = Reminder(
            business_id=business_id,
            customer_id=customer_id,
            transaction_id=transaction_id,
            send_at=send_at,
            tone=tone,
            status="scheduled",
        )
        session.add(r)
        created.append(r)

    session.flush()
    return created


def sync_reminders_after_payment(session: Session, business_id: int, customer_id: int) -> int:
    """Cancel remaining scheduled reminders if the customer has fully settled their debts."""
    balance = ledger.customer_balance_kobo(session, business_id, customer_id)
    if balance <= 0:
        reminders = list(
            session.scalars(
                select(Reminder).where(
                    Reminder.business_id == business_id,
                    Reminder.customer_id == customer_id,
                    Reminder.status == "scheduled",
                )
            )
        )
        for r in reminders:
            r.status = "cancelled"
        session.flush()
        return len(reminders)
    return 0


def build_prefilled_customer_message(
    business_name: str,
    customer_name: str,
    balance_kobo: int,
    due_date_str: str | None,
    tone: str,
    lang: str = "en",
) -> str:
    """Render localized reminder message intended for the customer."""
    amount_str = fmt_naira(balance_kobo)
    key = f"reminder_{tone}"
    return t(
        lang,
        key,
        customer=customer_name,
        amount=amount_str,
        business=business_name,
        due=due_date_str or "soon",
    )


def build_customer_chat_link(
    customer_phone: str | None,
    business_name: str,
    customer_name: str,
    balance_kobo: int,
    due_date_str: str | None,
    tone: str,
    lang: str = "en",
) -> str:
    """Generate https://wa.me/<number>?text=<urlencoded> link (Part F5 Route B)."""
    text = build_prefilled_customer_message(
        business_name, customer_name, balance_kobo, due_date_str, tone, lang
    )
    return documents.wa_me_link(customer_phone, text)


def get_due_reminders_for_business(
    session: Session,
    business_id: int,
    as_of: datetime | None = None,
) -> list[Reminder]:
    """Retrieve all open reminders due on or before as_of for a business."""
    lagos_dt = get_lagos_now(as_of)
    as_of_iso = lagos_dt.strftime("%Y-%m-%dT%H:%M:%S")

    reminders = list(
        session.scalars(
            select(Reminder)
            .where(
                Reminder.business_id == business_id,
                Reminder.status == "scheduled",
                Reminder.send_at <= as_of_iso,
            )
            .order_by(Reminder.send_at.asc())
        )
    )

    valid: list[Reminder] = []
    for r in reminders:
        # Check customer opt out
        cust = session.get(Customer, r.customer_id)
        if cust and cust.opted_out:
            r.status = "cancelled"
            continue

        # Check debt balance
        bal = ledger.customer_balance_kobo(session, business_id, r.customer_id)
        if bal <= 0:
            r.status = "cancelled"
            continue

        valid.append(r)

    session.flush()
    return valid


def has_sent_digest_today(session: Session, business_id: int, as_of: datetime | None = None) -> bool:
    """Enforce Part M Rule 3: at most ONE reminder digest per trader per day."""
    lagos_dt = get_lagos_now(as_of)
    today_str = lagos_dt.strftime("%Y-%m-%d")

    # Check WaMessageLog for today
    count = session.scalar(
        select(func.count())
        .select_from(WaMessageLog)
        .where(
            WaMessageLog.business_id == business_id,
            WaMessageLog.category == "reminder_digest",
            WaMessageLog.created_at.like(f"{today_str}%"),
        )
    )
    return (count or 0) > 0


def format_daily_digest(
    session: Session,
    business: Business,
    reminders: list[Reminder],
    lang: str = "en",
) -> str:
    """Compose the daily reminder digest message containing all due reminders."""
    header = t(lang, "reminder_digest_header", count=len(reminders))
    items_text = []

    for r in reminders:
        cust = session.get(Customer, r.customer_id)
        cust_name = cust.name if cust else "Customer"
        bal = ledger.customer_balance_kobo(session, business.id, r.customer_id)
        tx = session.get(Transaction, r.transaction_id)
        due_str = tx.due_date if tx and tx.due_date else "soon"

        link = build_customer_chat_link(
            customer_phone=cust.phone if cust else None,
            business_name=business.name or "Shop",
            customer_name=cust_name,
            balance_kobo=bal,
            due_date_str=due_str,
            tone=r.tone,
            lang=lang,
        )

        item = t(
            lang,
            "reminder_digest_item",
            customer=cust_name,
            amount=fmt_naira(bal),
            due=due_str,
            link=link,
        )
        items_text.append(item)

    return header + "\n" + "\n".join(items_text)


async def dispatch_due_reminders(
    session: Session,
    channel: Any,
    as_of: datetime | None = None,
) -> int:
    """Dispatch due reminders to traders in one daily digest per trader.

    Returns the number of reminders successfully processed and marked sent.
    """
    if not is_in_send_window(as_of):
        log.info("Outside reminder send window (08:00 - 20:00 Lagos time). Skipping dispatch.")
        return 0

    businesses = list(session.scalars(select(Business)))
    total_processed = 0

    for biz in businesses:
        # Check plan gating: Starter does not get automatic scheduled reminders (manual only)
        if not plans.allowed(biz, "scheduled_reminders"):
            continue

        # Check at most one digest per day
        if has_sent_digest_today(session, biz.id, as_of):
            continue

        due = get_due_reminders_for_business(session, biz.id, as_of)
        if not due:
            continue

        owner = session.get(User, biz.owner_user_id)
        if not owner or not owner.channel_user_id:
            continue

        # Deduplicate to latest due reminder per customer for the digest
        cust_map: dict[int, Reminder] = {}
        for r in due:
            cust_map[r.customer_id] = r  # due is ordered by send_at asc, so latest overwrites

        display_reminders = list(cust_map.values())
        digest_text = format_daily_digest(session, biz, display_reminders, owner.language or "en")

        # Send to trader
        try:
            if hasattr(channel, "send_text"):
                res = channel.send_text(owner.channel_user_id, digest_text)
                if asyncio.iscoroutine(res):
                    await res

            # Log to wa_message_log
            session.add(
                WaMessageLog(
                    business_id=biz.id,
                    to_ref=owner.channel_user_id,
                    category="reminder_digest",
                    msg_type="text",
                    status="sent",
                    est_cost_kobo=1080,  # ~10.8 NGN (Part M)
                    created_at=get_lagos_now(as_of).isoformat(),
                )
            )

            # Mark all due reminders as sent
            sent_time_iso = get_lagos_now(as_of).isoformat()
            for r in due:
                r.status = "sent"
                r.sent_at = sent_time_iso

            total_processed += len(display_reminders)
            session.commit()
        except Exception:
            log.exception("Failed to send reminder digest to business %s", biz.id)
            session.rollback()

    return total_processed


# ---------- Command Handlers ----------

def handle_remind_now(session: Session, business_id: int, customer_name: str, lang: str = "en") -> str:
    """Trader manual command: 'remind now Mama Ngozi'."""
    cust = session.scalar(
        select(Customer).where(
            Customer.business_id == business_id,
            func.lower(Customer.name) == customer_name.strip().lower(),
        )
    )
    if cust is None:
        return f"Customer '{customer_name}' not found."

    bal = ledger.customer_balance_kobo(session, business_id, cust.id)
    if bal <= 0:
        return f"{cust.name} does not owe any balance."

    biz = session.get(Business, business_id)
    link = build_customer_chat_link(
        customer_phone=cust.phone,
        business_name=biz.name if biz else "Shop",
        customer_name=cust.name,
        balance_kobo=bal,
        due_date_str="today",
        tone="gentle",
        lang=lang,
    )
    return f"Reminder for {cust.name} ({fmt_naira(bal)}):\nForward to customer: {link}"


def handle_pause_reminders(session: Session, business_id: int, customer_name: str) -> str:
    """Trader command: 'pause reminders Mama Ngozi'."""
    cust = session.scalar(
        select(Customer).where(
            Customer.business_id == business_id,
            func.lower(Customer.name) == customer_name.strip().lower(),
        )
    )
    if cust is None:
        return f"Customer '{customer_name}' not found."

    reminders = list(
        session.scalars(
            select(Reminder).where(
                Reminder.business_id == business_id,
                Reminder.customer_id == cust.id,
                Reminder.status == "scheduled",
            )
        )
    )
    for r in reminders:
        r.status = "paused"
    session.commit()
    return f"Reminders for {cust.name} have been paused ({len(reminders)} paused)."


def handle_stop_reminders(session: Session, business_id: int, customer_name: str) -> str:
    """Trader command: 'stop reminders Mama Ngozi'."""
    cust = session.scalar(
        select(Customer).where(
            Customer.business_id == business_id,
            func.lower(Customer.name) == customer_name.strip().lower(),
        )
    )
    if cust is None:
        return f"Customer '{customer_name}' not found."

    cust.opted_out = True
    reminders = list(
        session.scalars(
            select(Reminder).where(
                Reminder.business_id == business_id,
                Reminder.customer_id == cust.id,
                Reminder.status.in_(["scheduled", "paused"]),
            )
        )
    )
    for r in reminders:
        r.status = "cancelled"
    session.commit()
    return f"Reminders for {cust.name} stopped and customer marked opted-out."


def handle_resume_reminders(session: Session, business_id: int, customer_name: str) -> str:
    """Trader command: 'resume reminders Mama Ngozi'."""
    cust = session.scalar(
        select(Customer).where(
            Customer.business_id == business_id,
            func.lower(Customer.name) == customer_name.strip().lower(),
        )
    )
    if cust is None:
        return f"Customer '{customer_name}' not found."

    cust.opted_out = False
    reminders = list(
        session.scalars(
            select(Reminder).where(
                Reminder.business_id == business_id,
                Reminder.customer_id == cust.id,
                Reminder.status == "paused",
            )
        )
    )
    for r in reminders:
        r.status = "scheduled"
    session.commit()
    return f"Reminders for {cust.name} resumed ({len(reminders)} scheduled)."


def handle_reminder_text_command(session: Session, business_id: int, text: str, lang: str = "en") -> str | None:
    """Check if incoming text matches a reminder command."""
    t_clean = text.strip()
    low = t_clean.lower()

    if low.startswith("remind now ") or low.startswith("/remind now "):
        target = t_clean.split("now", 1)[1].strip()
        return handle_remind_now(session, business_id, target, lang)

    if low.startswith("pause reminders ") or low.startswith("/pause reminders "):
        target = t_clean.split("reminders", 1)[1].strip()
        return handle_pause_reminders(session, business_id, target)
    if low.startswith("pause ") or low.startswith("/pause "):
        target = t_clean.split("pause", 1)[1].strip()
        return handle_pause_reminders(session, business_id, target)

    if low.startswith("stop reminders ") or low.startswith("/stop reminders "):
        target = t_clean.split("reminders", 1)[1].strip()
        return handle_stop_reminders(session, business_id, target)
    if low.startswith("stop ") or low.startswith("/stop "):
        target = t_clean.split("stop", 1)[1].strip()
        return handle_stop_reminders(session, business_id, target)

    if low.startswith("resume reminders ") or low.startswith("/resume reminders "):
        target = t_clean.split("reminders", 1)[1].strip()
        return handle_resume_reminders(session, business_id, target)
    if low.startswith("resume ") or low.startswith("/resume "):
        target = t_clean.split("resume", 1)[1].strip()
        return handle_resume_reminders(session, business_id, target)

    return None
