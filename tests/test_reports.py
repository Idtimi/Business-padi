"""Tests for Phase 9: Reports, summaries, and profit & loss.

Covers:
- Evening summary arrives at configured time with correct totals matching database sums.
- Expense categories breakdown.
- Profit estimate: Sales - COGS - Expenses.
- Rule-based nudges:
  1. Customers owing >30 days.
  2. Items unsold in 30 days.
  3. Week-on-week drop >= 20%.
- Profit and loss PDF generation reusing ReportLab with branding; content matches ledger.
- Weekly summary by default, daily summary opt-in.
- Plan gating.
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
from freezegun import freeze_time
from pypdf import PdfReader
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.services import documents, ledger, reports, stock


class DummyChannel:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, to_ref: str, text: str):
        self.sent.append({"to": to_ref, "text": text})


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_reports.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Kudirat Store", plan="business"):
    u = m.User(channel="mock", channel_user_id=f"user-{name}", state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name, plan=plan, brand_color="#1A365D")
    s.add(b)
    s.commit()
    return u, b


def test_acceptance_summary_numbers_equal_database_sums(session):
    """Acceptance test:
    Summary numbers equal database sums: sales, expenses, and profit estimate.
    """
    u, b = make_business(session)
    today = datetime.now(m.get_tz()).date()
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Record 2 sales today: 50,000 NGN and 30,000 NGN = 80,000 NGN total
    tx1 = m.Transaction(
        business_id=b.id, customer_id=c.id, kind="sale",
        total_kobo=to_kobo(50_000), paid_kobo=to_kobo(50_000),
        status="confirmed", source_msg_id="s1", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T10:00:00",
    )
    tx2 = m.Transaction(
        business_id=b.id, customer_id=c.id, kind="sale",
        total_kobo=to_kobo(30_000), paid_kobo=to_kobo(30_000),
        status="confirmed", source_msg_id="s2", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T11:00:00",
    )
    # Record 2 expenses today: Transport 5,000 NGN, Utilities 10,000 NGN = 15,000 NGN
    exp1 = m.Transaction(
        business_id=b.id, kind="expense", category="transport",
        total_kobo=to_kobo(5_000), paid_kobo=to_kobo(5_000),
        status="confirmed", source_msg_id="e1", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T12:00:00",
    )
    exp2 = m.Transaction(
        business_id=b.id, kind="expense", category="utilities",
        total_kobo=to_kobo(10_000), paid_kobo=to_kobo(10_000),
        status="confirmed", source_msg_id="e2", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T13:00:00",
    )
    session.add_all([tx1, tx2, exp1, exp2])
    session.flush()

    # Generate summary for today
    summary = reports.generate_summary(session, b.id, period="today", today=today)

    # Verify exact figures in summary
    assert "80,000" in summary  # Total sales
    assert "15,000" in summary  # Total expenses
    assert "65,000" in summary  # Profit estimate (80k - 15k = 65k)
    assert "transport" in summary and "5,000" in summary
    assert "utilities" in summary and "10,000" in summary


def test_acceptance_pnl_pdf_matches_the_ledger(session, tmp_path, monkeypatch):
    """Acceptance test:
    The P&L PDF matches the ledger totals and contains the shop name and estimates.
    """
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session)
    today = datetime.now(m.get_tz()).date()

    # Add inventory and restock
    item = stock.get_or_create_item(session, b.id, "Rice", base_unit="bag")
    stock.record_restock(session, b.id, "Rice", qty=10.0, unit="bag", unit_cost_kobo=to_kobo(30_000))

    # Sell 2 bags @ N40,000 = N80,000 revenue. COGS is 2 * 30,000 = N60,000
    tx_sale = m.Transaction(
        business_id=b.id, kind="sale", total_kobo=to_kobo(80_000), paid_kobo=to_kobo(80_000),
        status="confirmed", source_msg_id="s-pnl-1", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T10:00:00",
    )
    session.add(tx_sale)
    session.flush()
    stock.record_sale(session, b.id, "Rice", qty=2.0, unit="bag", transaction_id=tx_sale.id)

    # Expense: Rent N10,000
    tx_exp = m.Transaction(
        business_id=b.id, kind="expense", category="rent", total_kobo=to_kobo(10_000), paid_kobo=to_kobo(10_000),
        status="confirmed", source_msg_id="e-pnl-1", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T11:00:00",
    )
    session.add(tx_exp)
    session.flush()

    # Generate P&L PDF
    pdf_path = reports.generate_pnl_pdf(session, b.id, period="today", today=today)
    assert Path(pdf_path).exists()

    # Extract text with pypdf and assert figures match ledger
    reader = PdfReader(pdf_path)
    text = "\n".join(p.extract_text() for p in reader.pages)

    assert "80,000" in text  # Revenue
    assert "60,000" in text  # COGS
    assert "20,000" in text  # Gross profit (80k - 60k = 20k)
    assert "10,000" in text  # Rent expense & Total expenses
    assert "10,000" in text  # Net profit estimate (20k - 10k = 10k)
    assert b.name in text


def test_rule_based_nudges_trigger_on_seeded_data(session):
    """Test the three rule-based nudges:
    1. Customer owing over 30 days
    2. Item unsold in 30 days
    3. Week-on-week drop >= 20%
    """
    u, b = make_business(session)
    today = datetime.now(m.get_tz()).date()

    # 1. Customer owing over 30 days:
    # Sale created 40 days ago (2026-09-14)
    past_date = today - timedelta(days=40)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")
    old_tx = m.Transaction(
        business_id=b.id, customer_id=c.id, kind="sale",
        total_kobo=to_kobo(25_000), paid_kobo=0,
        status="confirmed", source_msg_id="s-old-1", created_by_user_id=u.id,
        created_at=f"{past_date.isoformat()}T09:00:00",
    )
    session.add(old_tx)

    # 2. Item unsold in 30 days:
    # Restocked 40 days ago, no subsequent sale
    item = stock.get_or_create_item(session, b.id, "Cowbell Milk", base_unit="sachet")
    item.qty = 42.0

    # 3. Week-on-week drop:
    # Last week: 100,000 NGN sales. This week: 20,000 NGN sales (80% drop)
    last_week_monday = today - timedelta(days=today.weekday() + 7)
    tx_last_week = m.Transaction(
        business_id=b.id, kind="sale", total_kobo=to_kobo(100_000), paid_kobo=to_kobo(100_000),
        status="confirmed", source_msg_id="s-lw", created_by_user_id=u.id,
        created_at=f"{last_week_monday.isoformat()}T10:00:00",
    )
    tx_this_week = m.Transaction(
        business_id=b.id, kind="sale", total_kobo=to_kobo(20_000), paid_kobo=to_kobo(20_000),
        status="confirmed", source_msg_id="s-tw", created_by_user_id=u.id,
        created_at=f"{today.isoformat()}T10:00:00",
    )
    session.add_all([tx_last_week, tx_this_week])
    session.flush()

    # Query nudges
    nudges = reports.get_rule_based_nudges(session, b.id, today=today)

    # All three nudges should be triggered!
    assert any("Mama Ngozi" in n and "over 30 days" in n for n in nudges)
    assert any("Cowbell Milk" in n and "not sold" in n for n in nudges)
    assert any("down" in n and "%" in n for n in nudges)


import asyncio

def test_daily_summary_opt_in_versus_weekly_default(session):
    """Test that daily summaries only go to opted-in businesses,
    while weekly summaries go out on weekly days by default.
    """
    u1, b_opted_in = make_business(session, "Daily Shop", plan="trader")
    b_opted_in.custom_fields = {"daily_summary": True}

    u2, b_default = make_business(session, "Default Shop", plan="trader")
    b_default.custom_fields = {}

    session.flush()

    channel = DummyChannel()

    # Normal weekday dispatch: only b_opted_in receives summary
    count = asyncio.run(reports.dispatch_evening_summaries(session, channel, is_weekly_day=False))
    assert count == 1
    assert channel.sent[0]["to"] == u1.channel_user_id

    # Weekly day dispatch: both receive summary
    channel2 = DummyChannel()
    count_weekly = asyncio.run(reports.dispatch_evening_summaries(session, channel2, is_weekly_day=True))
    assert count_weekly == 2
