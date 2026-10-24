"""WhatsApp Cloud API message template management, window checks, and delivery tracking."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.channels.base import Channel
from app.config import settings
from app.models import Customer, User, WaMessageLog, get_tz
from app.money import fmt_naira
from app.services.costs import check_daily_budget, message_cost_kobo

log = logging.getLogger(__name__)

# Approved Meta utility template names
TEMPLATE_DOCUMENT_DELIVERY = "receipt_invoice_delivery"
TEMPLATE_PAYMENT_REMINDER = "payment_reminder"
APPROVED_UTILITY_TEMPLATES = {TEMPLATE_DOCUMENT_DELIVERY, TEMPLATE_PAYMENT_REMINDER}


class MessagingError(Exception):
    """Base class for messaging errors."""
    pass


class WindowClosedError(MessagingError):
    """Raised when attempting to send a free-form message outside the 24-hour service window."""
    pass


class OptedOutError(MessagingError):
    """Raised when attempting to send a message to a customer who has opted out."""
    pass


class NotOptedInError(MessagingError):
    """Raised when attempting to send an unsolicited message to a non-opted-in customer."""
    pass


def mask_phone(phone: str) -> str:
    """Mask phone number for logging: 2348012345678 -> *********5678"""
    return "*" * max(len(phone) - 4, 0) + phone[-4:]


def is_within_24h_window(session: Session, phone: str, now: datetime | None = None) -> bool:
    """Checks whether the given phone number sent a message in the last 24 hours.

    Inside 24 hours: customer service window is open, free-form text/buttons allowed.
    Outside 24 hours: window is closed, only approved Meta templates can be sent.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    user = session.scalar(select(User).where(User.channel_user_id == phone))
    if user is None or not user.last_user_message_at:
        return False

    try:
        last_dt = datetime.fromisoformat(user.last_user_message_at)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        elapsed_seconds = (now - last_dt).total_seconds()
        return 0 <= elapsed_seconds <= 86400  # 24 hours
    except Exception:
        log.exception("Error parsing last_user_message_at for %s", mask_phone(phone))
        return False


def is_customer_opted_out(session: Session, phone: str) -> bool:
    """Returns True if any customer associated with this phone number has opted out."""
    customers = session.scalars(select(Customer).where(Customer.phone == phone)).all()
    if not customers:
        return False
    return any(c.opted_out == 1 for c in customers)


def is_customer_opted_in(session: Session, phone: str) -> bool:
    """Returns True if at least one customer record with this phone has opted in and not opted out."""
    customers = session.scalars(select(Customer).where(Customer.phone == phone)).all()
    if not customers:
        return False
    return any(c.opted_in == 1 and c.opted_out == 0 for c in customers)


def opt_out_phone(session: Session, phone: str) -> int:
    """Marks all customer records with this phone as opted out."""
    customers = session.scalars(select(Customer).where(Customer.phone == phone)).all()
    count = 0
    for c in customers:
        c.opted_out = 1
        count += 1
    session.commit()
    return count


def opt_in_phone(session: Session, phone: str, business_id: int | None = None, name: str | None = None) -> Customer | None:
    """Marks customer as opted in (and clears opted_out). Creates one if business_id & name given."""
    customers = session.scalars(select(Customer).where(Customer.phone == phone)).all()
    if customers:
        for c in customers:
            c.opted_in = 1
            c.opted_out = 0
        session.commit()
        return customers[0]

    if business_id is not None and name:
        cust = Customer(business_id=business_id, name=name, phone=phone, opted_in=1, opted_out=0)
        session.add(cust)
        session.commit()
        return cust
    return None


def build_document_delivery_components(doc_type: str, doc_number: str, amount_kobo: int,
                                       customer_name: str, business_name: str,
                                       media_id: str | None = None,
                                       filename: str | None = None) -> list[dict]:
    """Builds template components for receipt/invoice delivery with a document header."""
    components = []
    if media_id:
        components.append({
            "type": "header",
            "parameters": [
                {
                    "type": "document",
                    "document": {
                        "id": media_id,
                        "filename": filename or f"{doc_type}_{doc_number}.pdf",
                    },
                }
            ],
        })

    components.append({
        "type": "body",
        "parameters": [
            {"type": "text", "text": customer_name},
            {"type": "text", "text": f"{doc_type.title()} {doc_number}"},
            {"type": "text", "text": fmt_naira(amount_kobo)},
            {"type": "text", "text": business_name},
        ],
    })
    return components


def build_payment_reminder_components(customer_name: str, amount_kobo: int, due_date_str: str,
                                      business_name: str) -> list[dict]:
    """Builds template components for a payment reminder utility template."""
    return [
        {
            "type": "body",
            "parameters": [
                {"type": "text", "text": customer_name},
                {"type": "text", "text": fmt_naira(amount_kobo)},
                {"type": "text", "text": due_date_str},
                {"type": "text", "text": business_name},
            ],
        }
    ]


async def send_utility_template(channel: Channel, session: Session, business_id: int | None,
                                to_phone: str, template_name: str, language: str = "en",
                                components: list[dict] | None = None,
                                require_opt_in: bool = False) -> WaMessageLog:
    """Sends an approved utility template to a recipient.

    - Permitted outside the 24-hour service window.
    - Blocked if the recipient has opted out (STOP).
    - If require_opt_in is True, blocked if the customer is not opted in.
    - Logs to wa_message_log and checks daily cost alert budget.
    """
    if template_name not in APPROVED_UTILITY_TEMPLATES:
        raise ValueError(f"Template '{template_name}' is not in approved utility templates: {APPROVED_UTILITY_TEMPLATES}")

    if is_customer_opted_out(session, to_phone):
        raise OptedOutError(f"Customer {mask_phone(to_phone)} has opted out of messages.")

    if require_opt_in and not is_customer_opted_in(session, to_phone):
        raise NotOptedInError(f"Customer {mask_phone(to_phone)} is not opted in.")

    # Send template via channel
    resp = await channel.send_template(to_phone, template_name, language_code=language, components=components)

    wa_mid = None
    if isinstance(resp, dict) and resp.get("messages"):
        wa_mid = resp["messages"][0].get("id")

    cost = message_cost_kobo("utility")
    log_entry = WaMessageLog(
        business_id=business_id,
        to_ref=mask_phone(to_phone),
        category="utility",
        msg_type="template",
        template_name=template_name,
        wa_message_id=wa_mid,
        status="sent",
        est_cost_kobo=cost,
    )
    session.add(log_entry)
    session.commit()

    # Budget alert check
    check_daily_budget(session)
    return log_entry


async def send_freeform_with_window_check(channel: Channel, session: Session, business_id: int | None,
                                          to_phone: str, text: str,
                                          buttons: list[tuple[str, str]] | None = None,
                                          now: datetime | None = None) -> WaMessageLog:
    """Sends a free-form message (text or buttons), enforcing the 24-hour customer service window.

    - Outside the 24h window: blocked with WindowClosedError.
    - Customer opted out: blocked with OptedOutError.
    - Inside 24h window: sent and logged to wa_message_log.
    """
    if not is_within_24h_window(session, to_phone, now=now):
        raise WindowClosedError(
            f"Cannot send free-form message to {mask_phone(to_phone)} outside the 24-hour service window. "
            "Only approved templates are permitted."
        )

    if is_customer_opted_out(session, to_phone):
        raise OptedOutError(f"Customer {mask_phone(to_phone)} has opted out of messages.")

    resp = None
    if buttons:
        resp = await channel.send_buttons(to_phone, text, buttons)
    else:
        resp = await channel.send_text(to_phone, text)

    wa_mid = None
    if isinstance(resp, dict) and resp.get("messages"):
        wa_mid = resp["messages"][0].get("id")

    cost = message_cost_kobo("service")
    log_entry = WaMessageLog(
        business_id=business_id,
        to_ref=mask_phone(to_phone),
        category="service",
        msg_type="interactive" if buttons else "text",
        wa_message_id=wa_mid,
        status="sent",
        est_cost_kobo=cost,
    )
    session.add(log_entry)
    session.commit()

    check_daily_budget(session)
    return log_entry


def update_delivery_status(session: Session, status_dict: dict) -> WaMessageLog | None:
    """Updates the delivery status of a message from a Meta webhook callback.

    Status can be: sent, delivered, read, failed.
    """
    wa_mid = status_dict.get("id")
    status_str = status_dict.get("status")

    if not wa_mid or not status_str:
        return None

    log_entry = session.scalar(select(WaMessageLog).where(WaMessageLog.wa_message_id == wa_mid))
    if log_entry is not None:
        log_entry.status = status_str
        session.commit()
        errors = status_dict.get("errors")
        if errors:
            log.warning("Message %s failed delivery. Error details: %s", wa_mid, errors)
        else:
            log.info("Updated message %s delivery status to %s", wa_mid, status_str)
        return log_entry

    return None
