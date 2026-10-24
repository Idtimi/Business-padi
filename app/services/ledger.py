from dataclasses import asdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Customer, Payment, Transaction
from app.money import fmt_naira, to_kobo
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
    payload.pop("problems", None)
    payload.pop("query", None)
    payload.pop("correction", None)
    payload["transcript"] = transcript
    payload["source_msg_id"] = source_msg_id
    p = pending.create(session, business_id, user_id, v.intent, payload, ttl_minutes)
    session.commit()
    return p


def _existing(session: Session, business_id: int, source_msg_id: str) -> Transaction | None:
    return session.scalar(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.source_msg_id == source_msg_id))


def _allocate_payment(session: Session, business_id: int, customer_id: int,
                      amount_kobo: int, source_msg_id: str | None) -> None:
    """Allocate repayment to customer's oldest unpaid credit sale first."""
    remaining = amount_kobo
    unpaid_sales = list(session.scalars(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.customer_id == customer_id,
            Transaction.kind == "sale",
            Transaction.status == "confirmed",
            Transaction.total_kobo > Transaction.paid_kobo,
        ).order_by(Transaction.created_at.asc(), Transaction.id.asc())
    ))

    for sale in unpaid_sales:
        if remaining <= 0:
            break
        due_on_sale = sale.total_kobo - sale.paid_kobo
        allocated = min(remaining, due_on_sale)
        sale.paid_kobo += allocated
        session.add(Payment(
            business_id=business_id,
            customer_id=customer_id,
            transaction_id=sale.id,
            amount_kobo=allocated,
            source_msg_id=source_msg_id,
        ))
        remaining -= allocated

    if remaining > 0 or not unpaid_sales:
        session.add(Payment(
            business_id=business_id,
            customer_id=customer_id,
            transaction_id=None,
            amount_kobo=remaining,
            source_msg_id=source_msg_id,
        ))


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
        items_json=data.get("lines"),
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

    if data["intent"] == "payment_received" and customer is not None:
        _allocate_payment(session, business_id, customer.id, data["total_kobo"], data["source_msg_id"])
        from app.services import reminders
        reminders.sync_reminders_after_payment(session, business_id, customer.id)
    elif tx.kind == "sale" and tx.due_date and tx.paid_kobo < tx.total_kobo and customer is not None:
        from app.services import reminders
        reminders.schedule_reminders_for_sale(session, business_id, customer.id, tx.id, tx.due_date)

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


# ---------- Phase 3: Corrections ----------

def get_last_transaction(session: Session, business_id: int) -> Transaction | None:
    return session.scalar(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.status == "confirmed",
        ).order_by(Transaction.id.desc())
    )


def format_tx_summary(session: Session, tx: Transaction, lang: str = "en") -> str:
    from app.i18n import t
    label = t(lang, tx.kind) if tx.kind in SAVEABLE else tx.kind.title()
    cust = ""
    if tx.customer_id:
        c = session.get(Customer, tx.customer_id)
        if c:
            cust = f" ({c.name})"
    return f"{label}{cust}: {fmt_naira(tx.total_kobo)}"


def propose_correction(session: Session, business_id: int, user_id: int,
                       correction: dict, transcript: str, source_msg_id: str,
                       lang: str = "en", ttl_minutes: int = pending.TTL_MINUTES) -> tuple[pending.PendingAction, str]:
    tx = get_last_transaction(session, business_id)
    if tx is None:
        raise ValueError("No transaction found to correct.")

    field = correction.get("field", "amount")
    new_val = correction.get("new_value")
    before_text = format_tx_summary(session, tx, lang)

    new_kobo = tx.total_kobo
    new_cust_name = None
    if field in {"amount", "unit_price", "total"} and new_val is not None:
        new_kobo = to_kobo(float(new_val))
    elif field == "customer" and new_val is not None:
        new_cust_name = clean_name(str(new_val))

    from app.i18n import t
    label = t(lang, tx.kind) if tx.kind in SAVEABLE else tx.kind.title()
    cust_str = f" ({new_cust_name})" if new_cust_name else (f" ({session.get(Customer, tx.customer_id).name})" if tx.customer_id else "")
    after_text = f"{label}{cust_str}: {fmt_naira(new_kobo)}"

    payload = {
        "target_id": tx.id,
        "field": field,
        "old_value": tx.total_kobo if field in {"amount", "unit_price", "total"} else str(tx.customer_id),
        "new_value": new_val,
        "before_text": before_text,
        "after_text": after_text,
        "transcript": transcript,
        "source_msg_id": source_msg_id,
    }
    p = pending.create(session, business_id, user_id, "correction", payload, ttl_minutes)
    session.commit()

    if lang == "pcm":
        confirm_msg = f"Change am?\nBefore: {before_text}\nAfter: {after_text}"
    else:
        confirm_msg = f"Correction:\nBefore: {before_text}\nAfter: {after_text}"
    return p, confirm_msg


def apply_correction(session: Session, business_id: int, transaction_id: int,
                     field: str, new_value) -> Transaction:
    """Apply correction directly to the existing transaction row in-place."""
    tx = session.get(Transaction, transaction_id)
    if tx is None or tx.business_id != business_id:
        raise LookupError("Transaction not found")

    if field in {"amount", "unit_price", "total"}:
        new_kobo = to_kobo(float(new_value))
        diff = new_kobo - tx.total_kobo
        tx.total_kobo = new_kobo
        if tx.kind == "sale":
            if tx.paid_kobo == (new_kobo - diff):  # was paid in full
                tx.paid_kobo = new_kobo
            elif tx.paid_kobo > new_kobo:
                tx.paid_kobo = new_kobo
        elif tx.kind in {"expense", "restock"}:
            tx.paid_kobo = new_kobo
        elif tx.kind == "payment_received":
            tx.paid_kobo = new_kobo
            # Re-allocate payments for this customer
            if tx.customer_id:
                # Remove payments linked to this source_msg_id
                session.query(Payment).filter(
                    Payment.business_id == business_id,
                    Payment.source_msg_id == tx.source_msg_id,
                ).delete()
                # Re-allocate
                _allocate_payment(session, business_id, tx.customer_id, new_kobo, tx.source_msg_id)
    elif field == "customer":
        cust = get_or_create_customer(session, business_id, str(new_value))
        tx.customer_id = cust.id if cust else None
    elif field == "cancel":
        tx.status = "cancelled"

    session.flush()
    session.commit()
    return tx


def confirm_correction(session: Session, business_id: int, pending_id: int) -> Transaction:
    p = pending.get_for_business(session, business_id, pending_id)
    if p.status != "open":
        raise ValueError("This correction is no longer open.")
    data = p.payload
    tx = apply_correction(session, business_id, data["target_id"], data["field"], data["new_value"])
    p.status = "applied"
    session.commit()
    return tx
