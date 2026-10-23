from dataclasses import asdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Payment, Transaction
from app.pipeline.validate import Validated, clean_name
from app.services import pending

SAVEABLE = {"sale", "expense", "payment_received", "restock"}


def get_or_create_customer(session: Session, business_id: int, name: str | None) -> Customer | None:
    name = clean_name(name)
    if not name:
        return None
    c = session.scalar(select(Customer).where(
        Customer.business_id == business_id, Customer.name == name))
    if c is None:
        c = Customer(business_id=business_id, name=name)
        session.add(c)
        session.flush()
    return c


def propose(session: Session, business_id: int, user_id: int, v: Validated,
            transcript: str, source_msg_id: str, ttl_minutes: int = pending.TTL_MINUTES):
    """Step 1: park the transaction as a pending action. Nothing hits the ledger yet."""
    if v.intent not in SAVEABLE:
        raise ValueError(f"Cannot save intent '{v.intent}'")
    if v.problems:
        raise ValueError(v.problems[0])
    if v.intent == "payment_received" and not v.customer:
        raise ValueError("Who pay you? Abeg tell me the name.")
    if v.intent == "sale" and v.balance_kobo > 0 and not v.customer:
        raise ValueError("Who owe you this money? Abeg tell me the name.")

    payload = asdict(v)
    payload.pop("problems")
    payload["transcript"] = transcript
    payload["source_msg_id"] = source_msg_id
    p = pending.create(session, business_id, user_id, v.intent, payload, ttl_minutes)
    session.commit()
    return p


def _existing(session: Session, business_id: int, source_msg_id: str) -> Transaction | None:
    return session.scalar(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.source_msg_id == source_msg_id))


def confirm(session: Session, business_id: int, pending_id: int) -> Transaction:
    """Step 2: the trader tapped Save. Safe to call twice: the second call returns the same record."""
    p = pending.get_for_business(session, business_id, pending_id)
    data = p.payload

    existing = _existing(session, business_id, data["source_msg_id"])
    if existing is not None:
        return existing
    if p.status != "open":
        raise ValueError("This one don cancel or expire.")
    if pending.is_expired(p):
        p.status = "expired"
        session.commit()
        raise ValueError("This one don expire. Abeg send am again.")

    customer = get_or_create_customer(session, business_id, data.get("customer"))
    tx = Transaction(
        business_id=business_id,
        customer_id=customer.id if customer else None,
        supplier=data.get("supplier"),
        kind=data["intent"],
        items_json=data["lines"],
        total_kobo=data["total_kobo"],
        paid_kobo=data["paid_kobo"],
        due_date=data.get("due_date"),
        category=data.get("category"),
        status="confirmed",
        transcript=data.get("transcript"),
        created_by_user_id=p.user_id,
        source_msg_id=data["source_msg_id"],
    )
    session.add(tx)
    session.flush()
    if data["intent"] == "payment_received":
        session.add(Payment(business_id=business_id, customer_id=customer.id,
                            amount_kobo=data["total_kobo"], source_msg_id=data["source_msg_id"]))
    p.status = "applied"
    session.commit()
    return tx


def customer_balance_kobo(session: Session, business_id: int, customer_id: int) -> int:
    """Derived in code, never stored: what they still owe us."""
    owed = session.scalar(select(func.coalesce(func.sum(Transaction.total_kobo - Transaction.paid_kobo), 0)).where(
        Transaction.business_id == business_id, Transaction.customer_id == customer_id,
        Transaction.kind == "sale", Transaction.status == "confirmed"))
    repaid = session.scalar(select(func.coalesce(func.sum(Payment.amount_kobo), 0)).where(
        Payment.business_id == business_id, Payment.customer_id == customer_id,
        Payment.transaction_id.is_(None)))
    return owed - repaid