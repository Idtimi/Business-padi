"""Query and reporting services for Voice-to-Ledger.

Handles business queries computed directly in Python:
- total_owed: total debt across all customers
- top_debtor: customer who owes the most
- customer_balance: balance for a specific customer
- sales_period: sales totals for a given period
- expenses_period: expenses totals for a given period
- profit_period: estimated profit (sales - expenses)
- best_sellers: top selling items by quantity/sales
"""

from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Customer, Payment, Transaction
from app.money import fmt_naira
from app.services import ledger

REPORT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "report_wording.txt"


def get_tz():
    try:
        return ZoneInfo(settings.tz)
    except Exception:
        # Africa/Lagos is UTC+1 year-round (West Africa Time)
        return timezone(timedelta(hours=1))


def period_date_range(period: str, today: date | None = None) -> tuple[datetime, datetime]:
    """Compute (start_dt_utc, end_dt_utc) for a given named period."""
    tz = get_tz()
    if today is None:
        today = datetime.now(tz).date()

    period = (period or "today").strip().lower()

    if period == "today":
        start_d = today
        end_d = today
    elif period == "yesterday":
        start_d = today - timedelta(days=1)
        end_d = start_d
    elif period in {"this_week", "week"}:
        start_d = today - timedelta(days=today.weekday())  # Monday
        end_d = start_d + timedelta(days=6)
    elif period == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        start_d = this_monday - timedelta(days=7)
        end_d = start_d + timedelta(days=6)
    elif period in {"this_month", "month"}:
        start_d = today.replace(day=1)
        next_month = (start_d.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_d = next_month - timedelta(days=1)
    elif period == "last_month":
        this_month_first = today.replace(day=1)
        end_d = this_month_first - timedelta(days=1)
        start_d = end_d.replace(day=1)
    else:
        # Default fallback to today
        start_d = today
        end_d = today

    start_local = datetime.combine(start_d, time.min, tzinfo=tz)
    end_local = datetime.combine(end_d, time.max, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def total_owed(session: Session, business_id: int) -> dict:
    """Sum of all outstanding customer balances."""
    customers = list(session.scalars(
        select(Customer).where(Customer.business_id == business_id)
    ))
    total_kobo = 0
    debtor_count = 0
    for c in customers:
        bal = ledger.customer_balance_kobo(session, business_id, c.id)
        if bal > 0:
            total_kobo += bal
            debtor_count += 1

    return {
        "type": "total_owed",
        "total_kobo": total_kobo,
        "total_naira": fmt_naira(total_kobo),
        "debtor_count": debtor_count,
    }


def top_debtor(session: Session, business_id: int) -> dict:
    """Find the customer with the highest outstanding balance."""
    customers = list(session.scalars(
        select(Customer).where(Customer.business_id == business_id)
    ))
    max_balance = 0
    top_customer = None

    for c in customers:
        bal = ledger.customer_balance_kobo(session, business_id, c.id)
        if bal > max_balance:
            max_balance = bal
            top_customer = c

    if top_customer is None or max_balance <= 0:
        return {
            "type": "top_debtor",
            "customer": None,
            "balance_kobo": 0,
            "balance_naira": fmt_naira(0),
        }

    return {
        "type": "top_debtor",
        "customer": top_customer.name,
        "balance_kobo": max_balance,
        "balance_naira": fmt_naira(max_balance),
    }


def customer_balance(session: Session, business_id: int, customer_name: str | None) -> dict:
    """Check balance for a single named customer."""
    name = (customer_name or "").strip().title()
    customer = session.scalar(select(Customer).where(
        Customer.business_id == business_id, Customer.name == name
    ))
    if customer is None:
        return {
            "type": "customer_balance",
            "customer": name,
            "balance_kobo": 0,
            "balance_naira": fmt_naira(0),
            "found": False,
        }

    bal = ledger.customer_balance_kobo(session, business_id, customer.id)
    return {
        "type": "customer_balance",
        "customer": customer.name,
        "balance_kobo": bal,
        "balance_naira": fmt_naira(bal),
        "found": True,
    }


def sales_period(session: Session, business_id: int, period: str = "today",
                 today: date | None = None) -> dict:
    """Sum confirmed sales within a given period."""
    start_utc, end_utc = period_date_range(period, today)
    start_str = start_utc.isoformat()
    end_str = end_utc.isoformat()

    txs = list(session.scalars(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.kind == "sale",
        Transaction.status == "confirmed",
        Transaction.created_at >= start_str,
        Transaction.created_at <= end_str,
    )))

    total_kobo = sum(t.total_kobo for t in txs)
    count = len(txs)
    return {
        "type": "sales_period",
        "period": period,
        "total_kobo": total_kobo,
        "total_naira": fmt_naira(total_kobo),
        "count": count,
    }


def expenses_period(session: Session, business_id: int, period: str = "today",
                    today: date | None = None) -> dict:
    """Sum confirmed expenses within a given period."""
    start_utc, end_utc = period_date_range(period, today)
    start_str = start_utc.isoformat()
    end_str = end_utc.isoformat()

    txs = list(session.scalars(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.kind == "expense",
        Transaction.status == "confirmed",
        Transaction.created_at >= start_str,
        Transaction.created_at <= end_str,
    )))

    total_kobo = sum(t.total_kobo for t in txs)
    count = len(txs)
    return {
        "type": "expenses_period",
        "period": period,
        "total_kobo": total_kobo,
        "total_naira": fmt_naira(total_kobo),
        "count": count,
    }


def profit_period(session: Session, business_id: int, period: str = "today",
                  today: date | None = None) -> dict:
    """Calculate estimated profit (sales - expenses)."""
    sales = sales_period(session, business_id, period, today)
    expenses = expenses_period(session, business_id, period, today)
    profit_kobo = sales["total_kobo"] - expenses["total_kobo"]

    return {
        "type": "profit_period",
        "period": period,
        "sales_kobo": sales["total_kobo"],
        "sales_naira": sales["total_naira"],
        "expenses_kobo": expenses["total_kobo"],
        "expenses_naira": expenses["total_naira"],
        "profit_kobo": profit_kobo,
        "profit_naira": fmt_naira(profit_kobo),
        "is_estimate": True,
    }


def best_sellers(session: Session, business_id: int, period: str = "this_week",
                 limit: int = 5, today: date | None = None) -> dict:
    """Rank top-selling items in a period by quantity."""
    start_utc, end_utc = period_date_range(period, today)
    start_str = start_utc.isoformat()
    end_str = end_utc.isoformat()

    txs = list(session.scalars(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.kind == "sale",
        Transaction.status == "confirmed",
        Transaction.created_at >= start_str,
        Transaction.created_at <= end_str,
    )))

    aggregated: dict[str, dict] = {}
    for tx in txs:
        for item in (tx.items_json or []):
            name = item.get("name") or "Item"
            qty = float(item.get("qty") or 1)
            total = int(item.get("line_total_kobo") or 0)
            if name not in aggregated:
                aggregated[name] = {"name": name, "qty": 0.0, "total_kobo": 0}
            aggregated[name]["qty"] += qty
            aggregated[name]["total_kobo"] += total

    ranked = sorted(aggregated.values(), key=lambda x: (x["qty"], x["total_kobo"]), reverse=True)[:limit]
    return {
        "type": "best_sellers",
        "period": period,
        "items": ranked,
    }


# ---------- Fallback formatting (pure template, no hallucination) ----------

def fallback_wording(facts: dict, lang: str = "en") -> str:
    """Deterministic plain text formatting of query facts."""
    qtype = facts.get("type")
    is_pidgin = (lang == "pcm")

    if qtype == "total_owed":
        amount = facts.get("total_naira", "N0")
        count = facts.get("debtor_count", 0)
        if count == 0 or facts.get("total_kobo", 0) == 0:
            return "Nobody dey owe you right now." if is_pidgin else "Nobody owes you right now."
        if is_pidgin:
            return f"Total money wey people owe you na {amount} ({count} customer)."
        return f"Total owed by customers: {amount} across {count} customer(s)."

    if qtype == "top_debtor":
        customer = facts.get("customer")
        amount = facts.get("balance_naira", "N0")
        if not customer:
            return "Nobody dey owe you right now." if is_pidgin else "Nobody owes you right now."
        if is_pidgin:
            return f"Na {customer} owe you pass: {amount}."
        return f"{customer} owes you the most: {amount}."

    if qtype == "customer_balance":
        customer = facts.get("customer") or "Customer"
        amount = facts.get("balance_naira", "N0")
        if facts.get("balance_kobo", 0) <= 0:
            return f"{customer} no dey owe you any money." if is_pidgin else f"{customer} does not owe any money."
        if is_pidgin:
            return f"{customer} dey owe you {amount}."
        return f"{customer} owes {amount}."

    if qtype == "sales_period":
        period = facts.get("period", "period").replace("_", " ")
        amount = facts.get("total_naira", "N0")
        count = facts.get("count", 0)
        if is_pidgin:
            return f"Sales for {period}: {amount} ({count} sales)."
        return f"Sales ({period}): {amount} ({count} sales)."

    if qtype == "expenses_period":
        period = facts.get("period", "period").replace("_", " ")
        amount = facts.get("total_naira", "N0")
        if is_pidgin:
            return f"Expenses for {period}: {amount}."
        return f"Expenses ({period}): {amount}."

    if qtype == "profit_period":
        period = facts.get("period", "period").replace("_", " ")
        profit = facts.get("profit_naira", "N0")
        sales = facts.get("sales_naira", "N0")
        expenses = facts.get("expenses_naira", "N0")
        if is_pidgin:
            return f"Estimated profit for {period}: {profit} (Sales: {sales}, Expenses: {expenses})."
        return f"Profit estimate ({period}): {profit} (Sales: {sales}, Expenses: {expenses})."

    if qtype == "best_sellers":
        items = facts.get("items", [])
        period = facts.get("period", "period").replace("_", " ")
        if not items:
            return "No items sold in this period."
        header = f"Items wey sell pass ({period}):" if is_pidgin else f"Top sellers ({period}):"
        lines = [f"- {it['name']}: {it['qty']:g}" for it in items]
        return "\n".join([header] + lines)

    return "No records found."


def run_query(session: Session, business_id: int, query_type: str, params: dict,
              lang: str = "en", today: date | None = None, llm = None) -> str:
    """Execute query in code and format the response."""
    params = params or {}
    qtype = (query_type or "").strip().lower()

    if qtype == "total_owed":
        facts = total_owed(session, business_id)
    elif qtype == "top_debtor":
        facts = top_debtor(session, business_id)
    elif qtype == "customer_balance":
        facts = customer_balance(session, business_id, params.get("customer"))
    elif qtype == "sales_period":
        facts = sales_period(session, business_id, params.get("period", "today"), today)
    elif qtype == "expenses_period":
        facts = expenses_period(session, business_id, params.get("period", "today"), today)
    elif qtype == "profit_period":
        facts = profit_period(session, business_id, params.get("period", "today"), today)
    elif qtype == "best_sellers":
        facts = best_sellers(session, business_id, params.get("period", "this_week"), today=today)
    else:
        return fallback_wording({"type": "unknown"}, lang)

    # Fallback template string guarantees exact numbers copied from code output
    return fallback_wording(facts, lang)
