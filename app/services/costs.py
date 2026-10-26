"""WhatsApp and AI message cost modeling, tracking, and reporting."""

import logging
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Business, WaMessageLog, get_tz
from app.money import fmt_naira

log = logging.getLogger(__name__)


def monthly_cost_kobo(entries: int, queries: int, docs: int, reminder_digests: int, summaries: int, approvals: int,
                      msgs_per_entry: float = 2.0, usd_per_msg: float = 0.0077, usd_ngn: float = 1400,
                      ai_entry_kobo: int = 1500, ai_query_kobo: int = 500, overhead: float = 1.15) -> int:
    """Calculates total projected monthly cost in kobo as specified in Part M."""
    msgs = entries * msgs_per_entry + queries + docs + reminder_digests + summaries + approvals
    whatsapp = msgs * usd_per_msg * usd_ngn * 100
    ai = entries * ai_entry_kobo + queries * ai_query_kobo
    return round((whatsapp + ai) * overhead)


def message_cost_kobo(category: str = "service") -> int:
    """Returns the estimated cost in kobo for sending a WhatsApp message."""
    usd = settings.wa_usd_per_message
    if category == "marketing":
        usd = 0.055  # Higher Meta rate for marketing templates
    return round(usd * settings.usd_ngn * 100)


def get_cost_report_data(session: Session, target_date: date | None = None) -> dict:
    """Aggregates wa_message_log for today and this month, broken down by category and business."""
    if target_date is None:
        target_date = datetime.now(get_tz()).date()

    today_prefix = target_date.isoformat()
    month_prefix = target_date.strftime("%Y-%m")

    # Fetch logs
    all_logs = session.scalars(select(WaMessageLog)).all()

    today_logs = [log_item for log_item in all_logs if (log_item.created_at or "").startswith(today_prefix)]
    month_logs = [log_item for log_item in all_logs if (log_item.created_at or "").startswith(month_prefix)]

    def _aggregate(logs: list[WaMessageLog]):
        total_count = len(logs)
        total_kobo = sum(l.est_cost_kobo or 0 for l in logs)

        by_category: dict[str, dict] = {}
        for l in logs:
            cat = l.category or "other"
            if cat not in by_category:
                by_category[cat] = {"count": 0, "cost_kobo": 0}
            by_category[cat]["count"] += 1
            by_category[cat]["cost_kobo"] += (l.est_cost_kobo or 0)

        by_business: dict[int | None, dict] = {}
        for l in logs:
            bid = l.business_id
            if bid not in by_business:
                biz_name = "System / Unassigned"
                if bid is not None:
                    biz = session.get(Business, bid)
                    if biz:
                        biz_name = biz.name
                by_business[bid] = {"name": biz_name, "count": 0, "cost_kobo": 0}
            by_business[bid]["count"] += 1
            by_business[bid]["cost_kobo"] += (l.est_cost_kobo or 0)

        return {
            "total_count": total_count,
            "total_kobo": total_kobo,
            "by_category": by_category,
            "by_business": by_business,
        }

    return {
        "date": today_prefix,
        "month": month_prefix,
        "today": _aggregate(today_logs),
        "month": _aggregate(month_logs),
    }


def generate_cost_report_text(session: Session, today: date | None = None) -> str:
    """Generates a formatted text message for the admin 'cost report' command."""
    data = get_cost_report_data(session, target_date=today)
    today_data = data["today"]
    month_data = data["month"]

    lines = [
        " *WhatsApp Message Cost Report*",
        "-----------------------------",
        f" *Today ({data['date']}):*",
        f"Total Sent: {today_data['total_count']} messages",
        f"Estimated Cost: {fmt_naira(today_data['total_kobo'])} ({today_data['total_kobo']:,} kobo)",
        "",
        "*By Category:*",
    ]

    if not today_data["by_category"]:
        lines.append("  (No messages sent today)")
    else:
        for cat, stats in sorted(today_data["by_category"].items()):
            lines.append(f"  • {cat}: {stats['count']} msgs, {fmt_naira(stats['cost_kobo'])} ({stats['cost_kobo']:,} kobo)")

    lines.append("")
    lines.append("*By Business:*")
    if not today_data["by_business"]:
        lines.append("  (None)")
    else:
        for bid, stats in sorted(today_data["by_business"].items(), key=lambda x: str(x[0])):
            b_label = f"{stats['name']} [ID {bid}]" if bid is not None else stats['name']
            lines.append(f"  • {b_label}: {stats['count']} msgs, {fmt_naira(stats['cost_kobo'])} ({stats['cost_kobo']:,} kobo)")

    lines.append("")
    lines.append("-----------------------------")
    lines.append(f"️ *This Month ({data['date'][:7]}):*")
    lines.append(f"Total Sent: {month_data['total_count']} messages")
    lines.append(f"Estimated Cost: {fmt_naira(month_data['total_kobo'])} ({month_data['total_kobo']:,} kobo)")
    lines.append("")
    lines.append("*By Category:*")
    if not month_data["by_category"]:
        lines.append("  (No messages sent this month)")
    else:
        for cat, stats in sorted(month_data["by_category"].items()):
            lines.append(f"  • {cat}: {stats['count']} msgs, {fmt_naira(stats['cost_kobo'])} ({stats['cost_kobo']:,} kobo)")

    lines.append("")
    lines.append("*By Business:*")
    if not month_data["by_business"]:
        lines.append("  (None)")
    else:
        for bid, stats in sorted(month_data["by_business"].items(), key=lambda x: str(x[0])):
            b_label = f"{stats['name']} [ID {bid}]" if bid is not None else stats['name']
            lines.append(f"  • {b_label}: {stats['count']} msgs, {fmt_naira(stats['cost_kobo'])} ({stats['cost_kobo']:,} kobo)")

    return "\n".join(lines)


def check_daily_budget(session: Session, today: date | None = None) -> tuple[int, bool, int]:
    """Checks if today's WhatsApp message cost exceeds the configured alert limit.

    Returns (current_daily_cost_kobo, alert_triggered, alert_threshold_kobo).
    """
    threshold = settings.daily_message_cost_alert_kobo
    if threshold is None or threshold <= 0:
        threshold = 500_000  # Default 5,000 NGN

    data = get_cost_report_data(session, target_date=today)
    today_cost = data["today"]["total_kobo"]

    alert = today_cost >= threshold
    if alert:
        log.warning(
            "DAILY_MESSAGE_COST_ALERT: Today's message cost %d kobo passes alert threshold %d kobo (%s)",
            today_cost,
            threshold,
            fmt_naira(today_cost),
        )

    return today_cost, alert, threshold
