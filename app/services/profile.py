"""Financial profile calculation, consent management, and partner sharing."""

import hashlib
import logging
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.platypus import HRFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AuditLog,
    Business,
    Consent,
    Customer,
    Payment,
    ShareLink,
    Transaction,
    User,
    get_tz,
    now_iso,
)
from app.money import fmt_naira, to_kobo
from app.services import documents, ledger, plans

log = logging.getLogger(__name__)


class ConsentAccessDenied(Exception):
    """Raised when access to a shared profile is denied (invalid, expired, or revoked)."""
    pass


# ---------------------------------------------------------------------------
# Metric Computations
# ---------------------------------------------------------------------------

def compute_financial_profile(session: Session, business_id: int, today: date | None = None) -> dict:
    """Computes trade profile metrics for the business using deterministic code.

    Includes:
    1. monthly sales trend
    2. active-days ratio
    3. collection rate (credit sales collected by due date)
    4. average days to collect
    5. debt concentration (share of top debtors)
    6. record maturity (weeks of history)

    STRICT PRIVACY: Returns ONLY aggregated metrics. Never exposes customer names or phones.
    """
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    tz = get_tz()
    if today is None:
        today = datetime.now(tz).date()

    # Query all confirmed transactions
    all_txs = session.scalars(
        select(Transaction)
        .where(
            Transaction.business_id == business_id,
            Transaction.status == "confirmed",
        )
        .order_by(Transaction.created_at.asc())
    ).all()

    # 1. Record Maturity (weeks of history)
    earliest_date = today
    if all_txs and all_txs[0].created_at:
        try:
            earliest_dt = datetime.fromisoformat(all_txs[0].created_at).date()
            if earliest_dt <= today:
                earliest_date = earliest_dt
        except Exception:
            pass

    history_days = max((today - earliest_date).days, 1)
    weeks_maturity = max(1, round(history_days / 7))

    # 2. Active-Days Ratio (over observation window, up to 90 days)
    window_days = min(history_days, 90)
    window_start = today - timedelta(days=window_days)
    active_dates = set()
    for tx in all_txs:
        if tx.created_at:
            try:
                tx_date = datetime.fromisoformat(tx.created_at).date()
                if tx_date >= window_start:
                    active_dates.add(tx_date)
            except Exception:
                pass
    active_days_count = len(active_dates)
    active_days_ratio = round(active_days_count / max(window_days, 1), 2)

    # 3. Monthly Sales Trend
    sales_by_month: dict[str, dict] = {}
    sales_txs = [tx for tx in all_txs if tx.kind == "sale"]
    for s in sales_txs:
        if s.created_at:
            m_key = s.created_at[:7]  # YYYY-MM
            if m_key not in sales_by_month:
                sales_by_month[m_key] = {"month": m_key, "sales_kobo": 0, "count": 0}
            sales_by_month[m_key]["sales_kobo"] += s.total_kobo
            sales_by_month[m_key]["count"] += 1

    sorted_months = sorted(sales_by_month.values(), key=lambda x: x["month"])
    for sm in sorted_months:
        sm["sales_naira"] = fmt_naira(sm["sales_kobo"])

    # Determine trend direction
    trend_direction = "steady"
    growth_pct = 0.0
    if len(sorted_months) >= 2:
        last_m = sorted_months[-1]["sales_kobo"]
        prev_m = sorted_months[-2]["sales_kobo"]
        if prev_m > 0:
            growth_pct = round(((last_m - prev_m) / prev_m) * 100, 1)
            if growth_pct > 5.0:
                trend_direction = "increasing"
            elif growth_pct < -5.0:
                trend_direction = "declining"

    # 4. Collection Rate & Average Days to Collect
    credit_sales = [tx for tx in sales_txs if tx.due_date or tx.paid_kobo < tx.total_kobo]
    total_credit_kobo = sum(tx.total_kobo - tx.paid_kobo for tx in credit_sales)

    # Payments allocated
    all_payments = session.scalars(
        select(Payment).where(Payment.business_id == business_id)
    ).all()
    total_collected_kobo = sum(p.amount_kobo for p in all_payments)

    if total_credit_kobo > 0:
        collection_rate = min(1.0, round(total_collected_kobo / total_credit_kobo, 2))
    else:
        collection_rate = 1.0

    # Days to collect calculation
    collection_days_list = []
    for p in all_payments:
        if p.created_at and p.transaction_id:
            orig_tx = session.get(Transaction, p.transaction_id)
            if orig_tx and orig_tx.created_at:
                try:
                    p_date = datetime.fromisoformat(p.created_at).date()
                    tx_date = datetime.fromisoformat(orig_tx.created_at).date()
                    delta = max(0, (p_date - tx_date).days)
                    collection_days_list.append(delta)
                except Exception:
                    pass

    avg_days_to_collect = (
        round(sum(collection_days_list) / len(collection_days_list), 1)
        if collection_days_list
        else 0.0
    )

    # 5. Debt Concentration (Share of Top Debtors)
    customers = session.scalars(
        select(Customer).where(Customer.business_id == business_id)
    ).all()

    debts = []
    for c in customers:
        bal = ledger.customer_balance_kobo(session, business_id, c.id)
        if bal > 0:
            debts.append(bal)

    debts.sort(reverse=True)
    total_debt_kobo = sum(debts)
    debtor_count = len(debts)

    if total_debt_kobo > 0:
        top_1_share = round((debts[0] / total_debt_kobo) * 100, 1) if debts else 0.0
        top_3_sum = sum(debts[:3])
        top_3_share = round((top_3_sum / total_debt_kobo) * 100, 1)
    else:
        top_1_share = 0.0
        top_3_share = 0.0

    return {
        "business_name": biz.name or "Trade Merchant",
        "as_of_date": today.isoformat(),
        "record_maturity": {
            "weeks": weeks_maturity,
            "days": history_days,
            "earliest_date": earliest_date.isoformat(),
            "label": f"{weeks_maturity} weeks of trade history",
        },
        "monthly_sales_trend": {
            "months": sorted_months,
            "total_sales_kobo": sum(sm["sales_kobo"] for sm in sorted_months),
            "total_sales_naira": fmt_naira(sum(sm["sales_kobo"] for sm in sorted_months)),
            "growth_pct": growth_pct,
            "trend": trend_direction,
        },
        "active_days_ratio": {
            "active_days": active_days_count,
            "total_days": window_days,
            "ratio": active_days_ratio,
            "percentage": round(active_days_ratio * 100, 1),
        },
        "collection_rate": {
            "rate": collection_rate,
            "percentage": round(collection_rate * 100, 1),
            "total_credit_kobo": total_credit_kobo,
            "total_collected_kobo": total_collected_kobo,
        },
        "average_days_to_collect": {
            "days": avg_days_to_collect,
        },
        "debt_concentration": {
            "debtor_count": debtor_count,
            "total_debt_kobo": total_debt_kobo,
            "total_debt_naira": fmt_naira(total_debt_kobo),
            "top_1_share_pct": top_1_share,
            "top_3_share_pct": top_3_share,
        },
        "document_type": "profile",  # Strictly never "credit score"
    }


# ---------------------------------------------------------------------------
# Consent and Sharing Management
# ---------------------------------------------------------------------------

def can_access_profile(biz: Business) -> bool:
    """Verifies if business plan allows financial profile feature."""
    if not biz:
        return False
    return bool(plans.allowed(biz, "financial_profile"))


def create_consent_record(session: Session, business_id: int, partner: str,
                          scope: str = "read_profile", expires_in_days: int = 30) -> Consent:
    """Creates a consent record for an external partner."""
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_in_days)).isoformat()
    consent = Consent(
        business_id=business_id,
        partner=partner,
        scope=scope,
        expires_at=expires_at,
        granted_at=now_iso(),
    )
    session.add(consent)
    session.add(AuditLog(
        business_id=business_id,
        action="consent_granted",
        ref=f"partner:{partner}:scope:{scope}",
    ))
    session.commit()
    return consent


def revoke_consent(session: Session, business_id: int, consent_id: int,
                   actor_user_id: int | None = None) -> Consent:
    """Revokes an existing partner consent immediately."""
    consent = session.get(Consent, consent_id)
    if consent is None or consent.business_id != business_id:
        raise ValueError(f"Consent {consent_id} not found for business {business_id}")

    consent.revoked_at = now_iso()
    session.add(AuditLog(
        business_id=business_id,
        actor_user_id=actor_user_id,
        action="consent_revoked",
        ref=f"consent_id:{consent_id}:partner:{consent.partner}",
    ))
    session.commit()
    return consent


def create_share_link(session: Session, consent_id: int, expires_in_days: int = 7,
                      base_url: str | None = None) -> tuple[str, str, ShareLink]:
    """Creates an expiring tokenized share link for an approved partner consent."""
    consent = session.get(Consent, consent_id)
    if consent is None:
        raise ValueError(f"Consent {consent_id} not found")

    if consent.revoked_at is not None:
        raise ConsentAccessDenied("Cannot create share link for revoked consent.")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()

    now = datetime.now(timezone.utc)
    target_exp = (now + timedelta(days=expires_in_days)).isoformat()
    link_expires_at = min(consent.expires_at, target_exp)

    link = ShareLink(
        consent_id=consent_id,
        token_hash=token_hash,
        expires_at=link_expires_at,
    )
    session.add(link)
    session.add(AuditLog(
        business_id=consent.business_id,
        action="share_link_created",
        ref=f"consent_id:{consent_id}:partner:{consent.partner}",
    ))
    session.commit()

    root_url = (base_url or settings.base_url).rstrip("/")
    share_url = f"{root_url}/profile/shared/{raw_token}"
    return raw_token, share_url, link


def access_shared_profile(session: Session, raw_token: str) -> tuple[dict, Consent, ShareLink]:
    """Validates partner token and returns aggregated metrics, writing every access to audit log."""
    if not raw_token or not raw_token.strip():
        raise ConsentAccessDenied("Missing token.")

    token_hash = hashlib.sha256(raw_token.strip().encode()).hexdigest()
    link = session.scalar(select(ShareLink).where(ShareLink.token_hash == token_hash))
    if link is None:
        raise ConsentAccessDenied("Invalid or unknown share link.")

    # Check link expiration
    now = datetime.now(timezone.utc)
    if link.expires_at:
        try:
            link_exp = datetime.fromisoformat(link.expires_at)
            if link_exp.tzinfo is None:
                link_exp = link_exp.replace(tzinfo=timezone.utc)
            if now > link_exp:
                raise ConsentAccessDenied("Share link has expired.")
        except ConsentAccessDenied:
            raise
        except Exception:
            pass

    # Load consent
    consent = session.get(Consent, link.consent_id)
    if consent is None:
        raise ConsentAccessDenied("Associated consent record not found.")

    if consent.revoked_at is not None:
        raise ConsentAccessDenied("Access denied: Consent has been revoked by the merchant.")

    if consent.expires_at:
        try:
            cons_exp = datetime.fromisoformat(consent.expires_at)
            if cons_exp.tzinfo is None:
                cons_exp = cons_exp.replace(tzinfo=timezone.utc)
            if now > cons_exp:
                raise ConsentAccessDenied("Access denied: Consent period has expired.")
        except ConsentAccessDenied:
            raise
        except Exception:
            pass

    # Record access in Audit Log
    session.add(AuditLog(
        business_id=consent.business_id,
        action="profile_shared_accessed",
        ref=f"partner:{consent.partner}:link:{link.id}",
    ))
    session.commit()

    # Compute aggregated metrics (ZERO individual customer data)
    profile_data = compute_financial_profile(session, consent.business_id)
    return profile_data, consent, link


# ---------------------------------------------------------------------------
# PDF Generation (ReportLab)
# ---------------------------------------------------------------------------

def generate_profile_pdf(session: Session, business_id: int, consent_id: int | None = None,
                         today: date | None = None) -> str:
    """Generates an official Financial Trade Profile summary PDF.

    Strictly aggregated metrics only. No personal customer names or phones.
    """
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    metrics = compute_financial_profile(session, business_id, today=today)

    out_dir = documents.MEDIA_DIR / str(biz.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    file_path = out_dir / f"profile_{ts}.pdf"

    doc = SimpleDocTemplate(
        str(file_path),
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    styles = getSampleStyleSheet()
    font_name = documents.FONT_NAME

    primary_color = HexColor("#0B8457")
    dark_color = HexColor("#0F172A")
    muted_color = HexColor("#64748B")

    title_style = ParagraphStyle(
        "ProfileTitle",
        parent=styles["Heading1"],
        fontName=font_name,
        fontSize=18,
        leading=22,
        textColor=primary_color,
        spaceAfter=4,
    )
    subtitle_style = ParagraphStyle(
        "ProfileSubtitle",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=12,
        textColor=muted_color,
        spaceAfter=12,
    )
    h2_style = ParagraphStyle(
        "ProfileH2",
        parent=styles["Heading2"],
        fontName=font_name,
        fontSize=12,
        leading=16,
        textColor=dark_color,
        spaceBefore=10,
        spaceAfter=6,
    )
    body_style = ParagraphStyle(
        "ProfileBody",
        parent=styles["Normal"],
        fontName=font_name,
        fontSize=9,
        leading=13,
        textColor=dark_color,
    )

    elements = []
    elements.append(Paragraph(f"FINANCIAL TRADE PROFILE", title_style))
    elements.append(Paragraph(
        f"Merchant: <b>{metrics['business_name']}</b> &bull; As of: {metrics['as_of_date']} &bull; Verified Trade Data (Not A Credit Score)",
        subtitle_style,
    ))
    elements.append(HRFlowable(width="100%", thickness=1, color=primary_color, spaceAfter=14))

    # Key Metrics Grid
    mat = metrics["record_maturity"]
    act = metrics["active_days_ratio"]
    col = metrics["collection_rate"]
    days_col = metrics["average_days_to_collect"]
    debt = metrics["debt_concentration"]

    summary_data = [
        [
            Paragraph("<b>Record Maturity</b>", body_style),
            Paragraph(f"{mat['weeks']} weeks ({mat['days']} days)", body_style),
            Paragraph("<b>Active-Days Ratio</b>", body_style),
            Paragraph(f"{act['percentage']}% ({act['active_days']}/{act['total_days']} days)", body_style),
        ],
        [
            Paragraph("<b>Collection Rate</b>", body_style),
            Paragraph(f"{col['percentage']}% of credit", body_style),
            Paragraph("<b>Avg Days to Collect</b>", body_style),
            Paragraph(f"{days_col['days']} days", body_style),
        ],
        [
            Paragraph("<b>Total Outstanding Debt</b>", body_style),
            Paragraph(debt['total_debt_naira'], body_style),
            Paragraph("<b>Debt Concentration</b>", body_style),
            Paragraph(f"Top 3 share: {debt['top_3_share_pct']}%", body_style),
        ],
    ]

    summary_table = Table(summary_data, colWidths=[130, 140, 130, 140])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F8FAFC")),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#E2E8F0")),
        ("PADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 14))

    # Monthly Sales Trend
    elements.append(Paragraph("Monthly Sales Trend", h2_style))
    months = metrics["monthly_sales_trend"]["months"]
    if months:
        trend_rows = [[
            Paragraph("<b>Month</b>", body_style),
            Paragraph("<b>Sales Volume</b>", body_style),
            Paragraph("<b>Sales Count</b>", body_style),
        ]]
        for m in months[-6:]:
            trend_rows.append([
                Paragraph(m["month"], body_style),
                Paragraph(m["sales_naira"], body_style),
                Paragraph(str(m["count"]), body_style),
            ])
        trend_table = Table(trend_rows, colWidths=[120, 200, 120])
        trend_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), HexColor("#E2E8F0")),
            ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#CBD5E1")),
            ("PADDING", (0, 0), (-1, -1), 5),
        ]))
        elements.append(trend_table)
    else:
        elements.append(Paragraph("No historical sales records available.", body_style))

    elements.append(Spacer(1, 16))
    elements.append(HRFlowable(width="100%", thickness=0.5, color=muted_color, spaceAfter=8))
    elements.append(Paragraph(
        "CONFIDENTIAL &bull; Generated via Business Padi &bull; Aggregated Metrics Only &bull; Contains No Individual Customer Data",
        subtitle_style,
    ))

    doc.build(elements)
    return str(file_path)
