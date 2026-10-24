"""Web dashboard authentication, session tokens, and business metrics."""

import csv
import hashlib
import hmac
import io
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Business,
    Customer,
    Document,
    PendingAction,
    StockItem,
    Transaction,
    User,
    get_tz,
)
from app.money import fmt_naira
from app.services import ledger, plans

log = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "vl_session"
DEFAULT_EXPIRY_MINUTES = 15
DEFAULT_SESSION_HOURS = 24


def _get_secret() -> str:
    return settings.whatsapp_app_secret or "business-padi-secret-key-12345"


def generate_magic_link(session: Session, user_id: int, business_id: int,
                        base_url: str | None = None) -> tuple[str, str]:
    """Generates a secure one-time login token, saves its hash with 15m expiry, and returns (token, url)."""
    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=DEFAULT_EXPIRY_MINUTES)).isoformat()

    action = PendingAction(
        business_id=business_id,
        user_id=user_id,
        kind="dashboard_login",
        payload={"token_hash": token_hash},
        status="open",
        expires_at=expires_at,
    )
    session.add(action)
    session.commit()

    root_url = (base_url or settings.base_url).rstrip("/")
    login_url = f"{root_url}/d/login?token={raw_token}"
    return raw_token, login_url


def verify_magic_token(session: Session, raw_token: str) -> tuple[bool, str, PendingAction | None]:
    """Validates the raw magic token against open PendingAction entries.

    Returns (is_valid, message, action).
    """
    if not raw_token or not raw_token.strip():
        return False, "Missing or invalid login token.", None

    token_hash = hashlib.sha256(raw_token.strip().encode()).hexdigest()

    candidates = session.scalars(
        select(PendingAction).where(
            PendingAction.kind == "dashboard_login",
            PendingAction.status == "open",
        )
    ).all()

    matched_action = None
    for cand in candidates:
        if cand.payload and cand.payload.get("token_hash") == token_hash:
            matched_action = cand
            break

    if matched_action is None:
        return False, "Invalid or already used login token.", None

    # Check expiry
    if matched_action.expires_at:
        try:
            exp_dt = datetime.fromisoformat(matched_action.expires_at)
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            now = datetime.now(timezone.utc)
            if now > exp_dt:
                matched_action.status = "expired"
                session.commit()
                return False, "This login link has expired. Please request a new one via WhatsApp.", None
        except Exception:
            log.exception("Error checking token expiration")

    # Mark as applied
    matched_action.status = "applied"
    session.commit()
    return True, "OK", matched_action


def create_session_cookie(user_id: int, business_id: int, hours: int = DEFAULT_SESSION_HOURS) -> str:
    """Creates a signed HMAC session cookie containing user_id, business_id, and expiration."""
    exp_ts = int(time.time()) + (hours * 3600)
    data = f"{user_id}:{business_id}:{exp_ts}"
    secret = _get_secret()
    sig = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
    return f"{data}.{sig}"


def verify_session_cookie(cookie_str: str | None) -> tuple[int, int] | None:
    """Verifies HMAC signature and expiration of session cookie. Returns (user_id, business_id) or None."""
    if not cookie_str or "." not in cookie_str:
        return None

    try:
        data, sig = cookie_str.rsplit(".", 1)
        secret = _get_secret()
        expected = hmac.new(secret.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None

        user_id_s, biz_id_s, exp_s = data.split(":")
        exp_ts = int(exp_s)
        if time.time() > exp_ts:
            return None

        return int(user_id_s), int(biz_id_s)
    except Exception:
        log.exception("Invalid session cookie")
        return None


def get_overview_metrics(session: Session, business_id: int, today_date=None) -> dict:
    """Calculates overview numbers for the dashboard: today's sales, money owed, low stock."""
    if today_date is None:
        today_date = datetime.now(get_tz()).date()
    today_prefix = today_date.isoformat()

    biz = session.get(Business, business_id)

    # 1. Today's sales
    sales_today = session.scalars(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.kind == "sale",
            Transaction.status == "confirmed",
            Transaction.created_at.like(f"{today_prefix}%"),
        )
    ).all()
    today_sales_count = len(sales_today)
    today_sales_kobo = sum(tx.total_kobo for tx in sales_today)

    # 2. Money owed across all customers
    customers = session.scalars(
        select(Customer).where(Customer.business_id == business_id)
    ).all()
    debtor_count = 0
    total_owed_kobo = 0
    for c in customers:
        bal = ledger.customer_balance_kobo(session, business_id, c.id)
        if bal > 0:
            debtor_count += 1
            total_owed_kobo += bal

    # 3. Stock items (if plan allows)
    low_stock_count = 0
    low_stock_items = []
    has_stock_plan = plans.allowed(biz, "stock_tracking") if biz else False
    if has_stock_plan:
        items = session.scalars(
            select(StockItem).where(StockItem.business_id == business_id)
        ).all()
        for item in items:
            if item.low_at is not None and item.qty <= item.low_at:
                low_stock_count += 1
                low_stock_items.append(item)

    return {
        "today_sales_count": today_sales_count,
        "today_sales_kobo": today_sales_kobo,
        "debtor_count": debtor_count,
        "total_owed_kobo": total_owed_kobo,
        "low_stock_count": low_stock_count,
        "low_stock_items": low_stock_items,
        "has_stock_plan": has_stock_plan,
        "date_str": today_prefix,
    }


def generate_ledger_csv(session: Session, business_id: int) -> str:
    """Exports all transactions for this business as a clean CSV."""
    txs = session.scalars(
        select(Transaction)
        .where(Transaction.business_id == business_id)
        .order_by(Transaction.id.desc())
    ).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Transaction ID",
        "Date",
        "Kind",
        "Status",
        "Customer / Supplier",
        "Items / Description",
        "Total (Kobo)",
        "Total (Naira)",
        "Paid (Kobo)",
        "Paid (Naira)",
        "Due Date",
        "Payment Type",
    ])

    for tx in txs:
        cust_name = ""
        if tx.customer_id:
            c = session.get(Customer, tx.customer_id)
            if c:
                cust_name = c.name

        items_str = ""
        if tx.items_json and isinstance(tx.items_json, list):
            items_str = "; ".join(
                f"{i.get('item', '')} x{i.get('qty', 1)} @ {fmt_naira(i.get('unit_price_kobo', 0))}"
                for i in tx.items_json
            )
        elif tx.transcript:
            items_str = tx.transcript

        writer.writerow([
            tx.id,
            tx.created_at[:19] if tx.created_at else "",
            tx.kind,
            tx.status,
            cust_name,
            items_str,
            tx.total_kobo,
            f"{tx.total_kobo / 100:.2f}",
            tx.paid_kobo,
            f"{tx.paid_kobo / 100:.2f}",
            tx.due_date or "",
            tx.category or "",
        ])

    return output.getvalue()
