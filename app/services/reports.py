"""Query and reporting services for Business Padi.

Handles business queries computed directly in Python:
- total_owed: total debt across all customers
- top_debtor: customer who owes the most
- customer_balance: balance for a specific customer
- sales_period: sales totals for a given period
- expenses_period: expenses totals for a given period
- expenses_by_category: itemized breakdown of expenses
- profit_estimate: estimated profit (sales - COGS - expenses)
- best_sellers: top selling items by quantity/sales
- rule-based nudges: debts >30d, items unsold >30d, week-on-week sales drop
- evening summaries: daily (opt-in) & weekly (default)
- profit and loss PDF: ReportLab document generation reusing branding
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
import json
import os
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Business, Customer, Payment, StockItem, StockMove, Transaction, User, WaMessageLog, now_iso
from app.money import fmt_naira, to_kobo
from app.services import documents, ledger, plans

REPORT_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "report_wording.txt"


def get_tz():
    try:
        from zoneinfo import ZoneInfo
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


def expenses_by_category(session: Session, business_id: int, period: str = "this_month",
                         today: date | None = None) -> dict:
    """Breakdown of expenses by category for a period."""
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

    categories: dict[str, int] = {}
    total_kobo = 0
    for tx in txs:
        cat = (tx.category or "general").strip().lower()
        categories[cat] = categories.get(cat, 0) + tx.total_kobo
        total_kobo += tx.total_kobo

    sorted_cats = [
        {"category": cat, "total_kobo": amt, "total_naira": fmt_naira(amt)}
        for cat, amt in sorted(categories.items(), key=lambda x: x[1], reverse=True)
    ]
    return {
        "type": "expenses_by_category",
        "period": period,
        "total_kobo": total_kobo,
        "total_naira": fmt_naira(total_kobo),
        "categories": sorted_cats,
    }


def profit_period(session: Session, business_id: int, period: str = "today",
                  today: date | None = None) -> dict:
    """Calculate estimated profit (sales - expenses)."""
    return profit_estimate(session, business_id, period, today)


def profit_estimate(session: Session, business_id: int, period: str = "today",
                    today: date | None = None) -> dict:
    """Calculate estimated profit (sales - COGS - expenses). Labelled 'estimate'."""
    sales = sales_period(session, business_id, period, today)
    expenses = expenses_period(session, business_id, period, today)

    # Cost of Goods Sold from stock moves in this period
    start_utc, end_utc = period_date_range(period, today)
    start_str = start_utc.isoformat()
    end_str = end_utc.isoformat()

    sale_moves = list(session.scalars(
        select(StockMove)
        .join(StockItem, StockItem.id == StockMove.item_id)
        .where(
            StockItem.business_id == business_id,
            StockMove.kind == "sale",
            StockMove.created_at >= start_str,
            StockMove.created_at <= end_str,
        )
    ))
    cogs_kobo = sum(round(abs(m.qty) * (m.unit_cost_kobo or 0)) for m in sale_moves)

    gross_profit_kobo = sales["total_kobo"] - cogs_kobo
    net_profit_kobo = gross_profit_kobo - expenses["total_kobo"]

    return {
        "type": "profit_estimate",
        "period": period,
        "sales_kobo": sales["total_kobo"],
        "sales_naira": sales["total_naira"],
        "cogs_kobo": cogs_kobo,
        "cogs_naira": fmt_naira(cogs_kobo),
        "gross_profit_kobo": gross_profit_kobo,
        "gross_profit_naira": fmt_naira(gross_profit_kobo),
        "expenses_kobo": expenses["total_kobo"],
        "expenses_naira": expenses["total_naira"],
        "profit_kobo": net_profit_kobo,
        "profit_naira": fmt_naira(net_profit_kobo),
        "is_estimate": True,
        "label": "Profit (estimate)",
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


def get_rule_based_nudges(session: Session, business_id: int, today: date | None = None,
                          lang: str = "en") -> list[str]:
    """Evaluate business health rules and produce actionable nudges:
    1. Customers owing over 30 days
    2. Items unsold in last 30 days
    3. Week-on-week sales drop >= 20%
    """
    if today is None:
        today = datetime.now(get_tz()).date()

    nudges: list[str] = []
    is_pidgin = (lang == "pcm")

    # 1. Customers owing over 30 days
    cutoff_30d = (today - timedelta(days=30)).isoformat()
    old_credit_sales = list(session.scalars(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.kind == "sale",
            Transaction.status == "confirmed",
            Transaction.paid_kobo < Transaction.total_kobo,
            Transaction.created_at <= cutoff_30d,
        )
    ))
    seen_debtors = set()
    for s in old_credit_sales:
        if s.customer_id and s.customer_id not in seen_debtors:
            bal = ledger.customer_balance_kobo(session, business_id, s.customer_id)
            if bal > 0:
                cust = session.get(Customer, s.customer_id)
                cust_name = cust.name if cust else "A customer"
                seen_debtors.add(s.customer_id)
                if is_pidgin:
                    nudges.append(f"Nudge: {cust_name} dey owe {fmt_naira(bal)} pass 30 days now.")
                else:
                    nudges.append(f"Nudge: {cust_name} has owed {fmt_naira(bal)} for over 30 days.")

    # 2. Items not sold in 30 days
    items = list(session.scalars(
        select(StockItem).where(
            StockItem.business_id == business_id,
            StockItem.qty > 0,
        )
    ))
    for item in items:
        recent_sale = session.scalar(
            select(StockMove).where(
                StockMove.item_id == item.id,
                StockMove.kind == "sale",
                StockMove.created_at >= cutoff_30d,
            )
        )
        if recent_sale is None:
            unit_str = item.base_unit or "units"
            if is_pidgin:
                nudges.append(f"Nudge: {item.name} never sell for 30 days ({item.qty:g} {unit_str} dey shop).")
            else:
                nudges.append(f"Nudge: {item.name} has not sold in the last 30 days ({item.qty:g} {unit_str} in stock).")

    # 3. Week-on-week drop >= 20%
    this_week = sales_period(session, business_id, "this_week", today)
    last_week = sales_period(session, business_id, "last_week", today)
    if last_week["total_kobo"] > 0 and this_week["total_kobo"] < last_week["total_kobo"]:
        drop_pct = round((last_week["total_kobo"] - this_week["total_kobo"]) / last_week["total_kobo"] * 100)
        if drop_pct >= 20:
            if is_pidgin:
                nudges.append(f"Nudge: Sales dis week drop by {drop_pct}% compare to last week ({this_week['total_naira']} vs {last_week['total_naira']}).")
            else:
                nudges.append(f"Nudge: Sales this week are down {drop_pct}% compared to last week ({this_week['total_naira']} vs {last_week['total_naira']}).")

    return nudges


def generate_summary(session: Session, business_id: int, period: str = "today",
                     today: date | None = None, lang: str = "en") -> str:
    """Generate evening summary message with verified calculations."""
    sales = sales_period(session, business_id, period, today)
    exp = expenses_by_category(session, business_id, period, today)
    prof = profit_estimate(session, business_id, period, today)
    top_items = best_sellers(session, business_id, period, limit=3, today=today)
    debtor = top_debtor(session, business_id)
    nudges = get_rule_based_nudges(session, business_id, today, lang)

    from app.services import stock
    low_stock = stock.get_low_stock_items(session, business_id)

    period_title = "Today" if period == "today" else ("This Week" if period in ("week", "this_week") else period.title())
    lines = [f" *Summary for {period_title}*"]
    lines.append(f"• Sales: {sales['total_naira']} ({sales['count']} sales)")
    if exp["total_kobo"] > 0:
        cat_str = ", ".join(f"{c['category']}: {c['total_naira']}" for c in exp["categories"][:3])
        lines.append(f"• Expenses: {exp['total_naira']} ({cat_str})")
    else:
        lines.append(f"• Expenses: {fmt_naira(0)}")

    lines.append(f"• Profit (estimate): {prof['profit_naira']}")

    if top_items["items"]:
        top_str = ", ".join(f"{it['name']} ({it['qty']:g})" for it in top_items["items"])
        lines.append(f"• Top items: {top_str}")

    if debtor["customer"] and debtor["balance_kobo"] > 0:
        lines.append(f"• Top debtor: {debtor['customer']} ({debtor['balance_naira']})")

    if low_stock:
        low_names = ", ".join(f"{it.name} ({it.qty:g})" for it in low_stock[:3])
        lines.append(f"️ *Low stock*: {low_names}")

    if nudges:
        lines.append("\n *Nudges*:")
        for n in nudges[:3]:
            lines.append(f"• {n}")

    return "\n".join(lines)


def generate_pnl_pdf(session: Session, business_id: int, period: str = "this_month",
                     today: date | None = None) -> str:
    """Generate a branded Profit and Loss PDF statement reusing documents styling."""
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    pnl = profit_estimate(session, business_id, period, today)
    exp_cats = expenses_by_category(session, business_id, period, today)

    # Output directory under media/documents/{biz.id}
    out_dir = documents.MEDIA_DIR / str(biz.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    file_path = out_dir / f"pnl_{period}_{datetime.now().strftime('%Y%m%d%H%M%S')}.pdf"

    doc = SimpleDocTemplate(
        str(file_path),
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    primary_color = HexColor(biz.brand_color or "#1A365D")
    font = documents.FONT_NAME
    bold_font = "DejaVuSans-Bold" if font == "DejaVuSans" else "Helvetica-Bold"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PnlTitle", parent=styles["Heading1"], fontName=bold_font, fontSize=18, textColor=primary_color
    )
    sub_style = ParagraphStyle(
        "PnlSub", parent=styles["Normal"], fontName=font, fontSize=10, textColor=HexColor("#4A5568")
    )
    cell_style = ParagraphStyle(
        "PnlCell", parent=styles["Normal"], fontName=font, fontSize=10, textColor=HexColor("#2D3748")
    )
    bold_cell_style = ParagraphStyle(
        "PnlBoldCell", parent=styles["Normal"], fontName=bold_font, fontSize=10, textColor=HexColor("#1A202C")
    )

    elements = []
    # Title & Business Header
    elements.append(Paragraph(f"<b>{biz.name or 'Business'}</b>", title_style))
    period_label = period.replace("_", " ").title()
    elements.append(Paragraph(f"Profit & Loss Statement (Estimate) — {period_label}", sub_style))
    elements.append(Paragraph(f"Generated: {datetime.now().strftime('%d %b %Y, %H:%M')}", sub_style))
    elements.append(Spacer(1, 14))

    # P&L Breakdown Table
    table_data = [
        [Paragraph("<b>Item</b>", bold_cell_style), Paragraph("<b>Amount</b>", bold_cell_style)],
        [Paragraph("Total Revenue (Sales)", cell_style), Paragraph(pnl["sales_naira"], cell_style)],
        [Paragraph("Cost of Goods Sold (COGS)", cell_style), Paragraph(f"- {pnl['cogs_naira']}", cell_style)],
        [Paragraph("<b>Gross Profit</b>", bold_cell_style), Paragraph(f"<b>{pnl['gross_profit_naira']}</b>", bold_cell_style)],
    ]

    # Itemize operating expenses
    if exp_cats["categories"]:
        table_data.append([Paragraph("<i>Operating Expenses:</i>", cell_style), Paragraph("", cell_style)])
        for c in exp_cats["categories"]:
            cat_name = c["category"].title()
            table_data.append([Paragraph(f"&nbsp;&nbsp;• {cat_name}", cell_style), Paragraph(f"- {c['total_naira']}", cell_style)])

    table_data.append([Paragraph("<b>Total Expenses</b>", bold_cell_style), Paragraph(f"<b>- {pnl['expenses_naira']}</b>", bold_cell_style)])
    table_data.append([Paragraph("<b>Net Profit (Estimate)</b>", bold_cell_style), Paragraph(f"<b>{pnl['profit_naira']}</b>", bold_cell_style)])

    table = Table(table_data, colWidths=[360, 180])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), HexColor("#EDF2F7")),
        ("TEXTCOLOR", (0, 0), (-1, 0), primary_color),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 1, HexColor("#CBD5E0")),
        ("LINEBELOW", (0, -1), (-1, -1), 1.5, primary_color),
        ("LINEABOVE", (0, -1), (-1, -1), 1, HexColor("#CBD5E0")),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 20))
    elements.append(Paragraph("<i>Note: Profit figures are calculated estimates based on recorded transactions and inventory cost memory.</i>", sub_style))

    doc.build(elements)
    return str(file_path)


async def dispatch_evening_summaries(session: Session, channel, as_of: datetime | None = None, is_weekly_day: bool = False) -> int:
    """Send evening summaries to eligible traders.
    - Weekly summary by default
    - Daily summary only if the trader opted in
    - Gated by plan (Starter gets weekly summary only)
    """
    businesses = list(session.scalars(select(Business)))
    sent_count = 0

    for biz in businesses:
        owner = session.get(User, biz.owner_user_id)
        if not owner or not owner.channel_user_id:
            continue

        custom = biz.custom_fields or {}
        opted_in_daily = bool(custom.get("daily_summary", False))
        can_daily = plans.allowed(biz, "daily_summary")

        # Determine whether to send summary
        should_send = False
        summary_period = "today"

        if opted_in_daily and can_daily:
            should_send = True
            summary_period = "today"
        elif is_weekly_day:
            should_send = True
            summary_period = "this_week"

        if not should_send:
            continue

        text = generate_summary(session, biz.id, period=summary_period, today=as_of.date() if as_of else None, lang=owner.language or "en")

        try:
            if hasattr(channel, "send_text"):
                res = channel.send_text(owner.channel_user_id, text)
                if asyncio.iscoroutine(res):
                    await res

            session.add(WaMessageLog(
                business_id=biz.id,
                to_ref=owner.channel_user_id,
                category="summary",
                msg_type="text",
                status="sent",
                est_cost_kobo=1080,
                created_at=(as_of or datetime.now(timezone.utc)).isoformat(),
            ))
            session.commit()
            sent_count += 1
        except Exception:
            session.rollback()

    return sent_count


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

    if qtype in ("profit_period", "profit_estimate"):
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
    elif qtype in ("profit_period", "profit_estimate"):
        facts = profit_estimate(session, business_id, params.get("period", "today"), today)
    elif qtype == "best_sellers":
        facts = best_sellers(session, business_id, params.get("period", "this_week"), today=today)
    elif qtype == "expenses_by_category":
        facts = expenses_by_category(session, business_id, params.get("period", "this_month"), today)
    else:
        return fallback_wording({"type": "unknown"}, lang)

    return fallback_wording(facts, lang)
