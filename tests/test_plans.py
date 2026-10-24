"""Tests for Phase 6: Plans and Entitlements.

Covers:
- Plan matrix and feature gating via allowed(business, feature)
- Monthly voice entry counting in usage_counters
- 21st voice entry refused on Starter with upgrade and pack options
- Buying entry packs (+25 entries)
- Quota reset on a new month using freezegun
- Starter cannot issue invoices, but receipt works
- Downgrade keeps historical documents fully readable
- Billing plan activation and test mode
"""

from datetime import datetime, timezone
from pathlib import Path
import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.pipeline.llm import MockLLM
from app.services import billing, documents, ledger, plans
from config.plans import ENTRY_PACK_COUNT, PLANS, get_plan

RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_plans.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Kudirat Provisions", plan="starter"):
    u = m.User(channel="mock", channel_user_id=f"user-{name}", state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name, plan=plan, phone="08012345678")
    s.add(b)
    s.commit()
    return u, b


def press(s, b, u, reply, index=0):
    action, pid = router.parse_button(reply.buttons[index][0])
    return router.handle_button(s, b.id, u.id, action, pid, "en")


def test_plan_matrix_and_allowed_checks(session):
    """Test feature gating across different tiers."""
    u, b_starter = make_business(session, "Starter Shop", plan="starter")
    u, b_trader = make_business(session, "Trader Shop", plan="trader")
    u, b_biz = make_business(session, "Business Shop", plan="business")

    # Invoices
    assert not plans.allowed(b_starter, "invoices")
    assert plans.allowed(b_trader, "invoices")
    assert plans.allowed(b_biz, "invoices")

    # Receipts (all tiers can issue receipts)
    assert plans.allowed(b_starter, "receipts")
    assert plans.allowed(b_trader, "receipts")
    assert plans.allowed(b_biz, "receipts")

    # Statements
    assert not plans.allowed(b_starter, "statements")
    assert plans.allowed(b_trader, "statements")
    assert plans.allowed(b_biz, "statements")

    # Stock tracking
    assert not plans.allowed(b_starter, "stock_tracking")
    assert plans.allowed(b_trader, "stock_tracking")
    assert plans.allowed(b_biz, "stock_tracking")

    # Staff accounts
    assert not plans.allowed(b_starter, "staff_accounts")
    assert not plans.allowed(b_trader, "staff_accounts")
    assert plans.allowed(b_biz, "staff_accounts")


def test_starter_cannot_issue_invoices_but_receipt_works(session, tmp_path, monkeypatch):
    """Acceptance test: invoice request on starter returns an upgrade prompt; receipt works."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session, plan="starter")

    # First, record a sale so there is a recent transaction
    replies = router.handle_text(session, b.id, u.id, RICE, "msg-sale-1")
    assert len(replies) == 1
    press(session, b, u, replies[0], 0)  # confirm and save

    # Ask for invoice on starter -> blocked with upgrade prompt
    inv_replies = router.handle_text(session, b.id, u.id, "Send invoice for that sale", "msg-inv-1")
    assert len(inv_replies) == 1
    prompt = inv_replies[0]
    assert "Trader plan" in prompt.text
    # Verify upgrade button is present
    button_actions = [router.parse_button(btn[0])[0] for btn in prompt.buttons]
    assert "upgrade_trader" in button_actions

    # Ask for receipt on starter -> works! Shows issue confirmation
    rec_replies = router.handle_text(session, b.id, u.id, "Send receipt for that sale", "msg-rec-1")
    assert len(rec_replies) == 1
    assert "Issue Receipt" in rec_replies[0].text
    # Press Issue button
    issue_reply = press(session, b, u, rec_replies[0], 0)
    assert "Receipt #R-0001 issued" in issue_reply[0].text


@freeze_time("2026-10-15 10:00:00")
def test_acceptance_voice_limit_refusal_and_monthly_reset(session):
    """Acceptance test:
    On starter, the 21st voice entry in a month is refused politely with an upgrade option
    and the option to buy an entry pack; quota resets on a new month."""
    u, b = make_business(session, plan="starter")

    # Send 20 voice entries in October 2026
    for i in range(20):
        replies = router.handle_text(
            session, b.id, u.id, RICE, f"v-msg-{i}", is_voice=True
        )
        assert len(replies) == 1
        # Each voice entry before cap should be processed
        assert replies[0].kind == "confirm"
        press(session, b, u, replies[0], 0)  # save

    # Check usage counter: exactly 20 used
    used, extra = plans.get_voice_usage(session, b.id, "2026-10")
    assert used == 20
    assert extra == 0

    # 21st voice entry in October 2026 -> refused politely
    refusal_replies = router.handle_text(
        session, b.id, u.id, RICE, "v-msg-21", is_voice=True
    )
    assert len(refusal_replies) == 1
    refusal = refusal_replies[0]
    assert "limit of 20 voice entries" in refusal.text
    btn_actions = [router.parse_button(btn[0])[0] for btn in refusal.buttons]
    assert "upgrade_trader" in btn_actions
    assert "buy_pack" in btn_actions

    # Test quota reset in November 2026 using freezegun
    with freeze_time("2026-11-01 08:00:00"):
        # In November, counter is fresh: 1st entry is allowed!
        nov_replies = router.handle_text(
            session, b.id, u.id, RICE, "v-msg-nov-1", is_voice=True
        )
        assert len(nov_replies) == 1
        assert nov_replies[0].kind == "confirm"
        press(session, b, u, nov_replies[0], 0)

        # Usage in November is 1, October remains 20
        used_nov, _ = plans.get_voice_usage(session, b.id, "2026-11")
        assert used_nov == 1
        used_oct, _ = plans.get_voice_usage(session, b.id, "2026-10")
        assert used_oct == 20


@freeze_time("2026-10-15 12:00:00")
def test_buying_entry_pack_allows_further_voice_entries(session):
    """Test buying an entry pack adds +25 voice entries when monthly limit is hit."""
    u, b = make_business(session, plan="starter")

    # Use up the 20 entries
    for i in range(20):
        router.handle_text(session, b.id, u.id, RICE, f"v-{i}", is_voice=True)

    # 21st is blocked
    r = router.handle_text(session, b.id, u.id, RICE, "v-21", is_voice=True)
    assert "limit of 20" in r[0].text

    # Trader clicks "Buy 25 Entries" button
    buy_resp = router.handle_button(session, b.id, u.id, "buy_pack")
    assert "Added +25 voice entries" in buy_resp[0].text

    # Now 21st voice entry is accepted!
    r21 = router.handle_text(session, b.id, u.id, RICE, "v-21-retry", is_voice=True)
    assert r21[0].kind == "confirm"


def test_downgrade_keeps_old_documents_readable(session, tmp_path, monkeypatch):
    """Test that downgrading to Starter keeps all previously issued documents readable."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session, plan="trader")

    # Issue an invoice on Trader plan
    items = [{"name": "Rice", "qty": 2, "unit": "bag", "unit_price_kobo": to_kobo(40_000), "line_total_kobo": to_kobo(80_000)}]
    doc = documents.issue_document(
        session=session,
        business_id=b.id,
        kind="invoice",
        items=items,
        total_kobo=to_kobo(80_000),
        paid_kobo=to_kobo(80_000),
    )
    doc_id = doc.id
    doc_path = Path(doc.file_path)
    assert doc.number == "INV-0001"
    assert doc_path.exists()

    # Downgrade business to Starter
    billing.downgrade_plan(session, b.id, "starter")
    session.refresh(b)
    assert b.plan == "starter"

    # Verify document is still intact, readable, and queryable
    reloaded_doc = session.get(m.Document, doc_id)
    assert reloaded_doc is not None
    assert reloaded_doc.number == "INV-0001"
    assert reloaded_doc.status == "issued"
    assert Path(reloaded_doc.file_path).exists()
    assert len(Path(reloaded_doc.file_path).read_bytes()) > 0


def test_upgrade_plan_via_button_enables_invoices(session, tmp_path, monkeypatch):
    """Test upgrading from Starter to Trader unlocks invoice generation."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session, plan="starter")

    # Record a sale
    replies = router.handle_text(session, b.id, u.id, RICE, "msg-s1")
    press(session, b, u, replies[0], 0)

    # Invoices blocked on starter
    blocked = router.handle_text(session, b.id, u.id, "Send invoice for that sale", "msg-i1")
    assert "Trader plan" in blocked[0].text

    # Click Upgrade to Trader button
    up_reply = router.handle_button(session, b.id, u.id, "upgrade_trader")
    assert "upgraded to the Trader plan" in up_reply[0].text

    # Invoices now permitted!
    allowed_reply = router.handle_text(session, b.id, u.id, "Send invoice for that sale", "msg-i2")
    assert "Issue Invoice" in allowed_reply[0].text
