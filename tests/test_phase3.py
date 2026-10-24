"""Tests for Phase 3: queries, corrections, credit book."""

from datetime import date
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.pipeline.llm import MockLLM
from app.pipeline.schemas import ExtractionResult
from app.pipeline.validate import validate
from app.services import ledger, reports

RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_p3.db'}")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Mama's Store"):
    u = m.User(channel="mock", channel_user_id=f"user-{name}")
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name)
    s.add(b)
    s.flush()
    s.commit()
    return u, b


def count_tx(s):
    return s.scalar(select(func.count()).select_from(m.Transaction))


def press(s, b, u, reply, index=0):
    action, pid = router.parse_button(reply.buttons[index][0])
    return router.handle_button(s, b.id, u.id, action, pid, "en")


# ============================================================================
# Acceptance Tests
# ============================================================================

def test_acceptance_who_owe_me_pass_returns_top_debtor(session):
    """Acceptance: 'Who owe me pass?' returns the customer with the largest balance."""
    u, b = make_business(session)

    # Seed two credit sales: Chika owes 30k, Mama Ngozi owes 77.5k
    c_chika = ledger.get_or_create_customer(session, b.id, "Chika")
    c_ngozi = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Chika credit sale: total 30k, paid 0 -> owes 30k
    tx1 = m.Transaction(
        business_id=b.id, customer_id=c_chika.id, kind="sale",
        total_kobo=to_kobo(30_000), paid_kobo=0, status="confirmed",
        created_by_user_id=u.id, source_msg_id="seed-1",
    )
    # Mama Ngozi credit sale: total 127.5k, paid 50k -> owes 77.5k
    tx2 = m.Transaction(
        business_id=b.id, customer_id=c_ngozi.id, kind="sale",
        total_kobo=to_kobo(127_500), paid_kobo=to_kobo(50_000), status="confirmed",
        created_by_user_id=u.id, source_msg_id="seed-2",
    )
    session.add_all([tx1, tx2])
    session.commit()

    # Query via router using MockLLM
    replies = router.handle_text(session, b.id, u.id, "Who owe me pass?", "m-query", "pcm", llm=MockLLM())
    assert len(replies) == 1
    assert replies[0].kind == "info"
    # Mama Ngozi has the largest balance (N77,500)
    assert "Mama Ngozi" in replies[0].text
    assert "N77,500" in replies[0].text


def test_acceptance_repayment_settles_oldest_sale_first(session):
    """Acceptance: repayment of N20,000 reduces the balance and the oldest sale is settled first."""
    u, b = make_business(session)
    cust = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Oldest sale: total 15,000, paid 0 (unpaid 15,000)
    sale1 = m.Transaction(
        business_id=b.id, customer_id=cust.id, kind="sale",
        total_kobo=to_kobo(15_000), paid_kobo=0, status="confirmed",
        created_by_user_id=u.id, source_msg_id="s1",
        created_at="2026-10-01T10:00:00+00:00",
    )
    # Newer sale: total 50,000, paid 0 (unpaid 50,000)
    sale2 = m.Transaction(
        business_id=b.id, customer_id=cust.id, kind="sale",
        total_kobo=to_kobo(50_000), paid_kobo=0, status="confirmed",
        created_by_user_id=u.id, source_msg_id="s2",
        created_at="2026-10-05T10:00:00+00:00",
    )
    session.add_all([sale1, sale2])
    session.commit()

    # Customer owes 15k + 50k = 65k
    assert ledger.customer_balance_kobo(session, b.id, cust.id) == to_kobo(65_000)

    # Repayment of N20,000 via router / MockLLM
    pay_text = "Mama Ngozi don pay twenty thousand."
    replies = router.handle_text(session, b.id, u.id, pay_text, "m-pay", "en", llm=MockLLM())
    assert len(replies) == 1
    confirm_reply = replies[0]
    press(session, b, u, confirm_reply)

    # Balance is reduced by 20,000: 65k - 20k = 45k
    assert ledger.customer_balance_kobo(session, b.id, cust.id) == to_kobo(45_000)

    # Oldest sale (15k) is completely settled: paid_kobo == 15,000
    session.refresh(sale1)
    assert sale1.paid_kobo == to_kobo(15_000)

    # Remaining 5,000 went to newer sale (sale2): paid_kobo == 5,000
    session.refresh(sale2)
    assert sale2.paid_kobo == to_kobo(5_000)


def test_acceptance_correction_edits_same_row_never_creates_new(session):
    """Acceptance: a correction changes amount 20000 to 2000 on the same row;
    never creating a new transaction."""
    u, b = make_business(session)

    # 1. First record a payment of 20,000
    pay_text = "Mama Ngozi don pay twenty thousand."
    r1 = router.handle_text(session, b.id, u.id, pay_text, "m-p1", "en", llm=MockLLM())[0]
    press(session, b, u, r1)

    assert count_tx(session) == 1
    tx = session.scalar(select(m.Transaction))
    orig_id = tx.id
    assert tx.total_kobo == to_kobo(20_000)

    # 2. Trader says: "No, na two thousand, no be twenty thousand."
    corr_text = "No, na two thousand, no be twenty thousand."
    r2 = router.handle_text(session, b.id, u.id, corr_text, "m-corr", "en", llm=MockLLM())
    assert len(r2) == 1
    assert r2[0].kind == "confirm"
    assert "20,000" in r2[0].text and "2,000" in r2[0].text

    # 3. Trader taps Save to confirm correction
    out = press(session, b, u, r2[0])
    assert "Saved" in out[0].text

    # 4. Verified: same row modified, NO new transaction created!
    assert count_tx(session) == 1
    session.refresh(tx)
    assert tx.id == orig_id
    assert tx.total_kobo == to_kobo(2_000)


# ============================================================================
# Parameterised Query Handlers Tests
# ============================================================================

def test_query_handlers_with_seeded_data(session):
    """Test all 7 query handlers on seeded data."""
    u, b = make_business(session)
    today = date(2026, 10, 24)

    # Seed customers
    c1 = ledger.get_or_create_customer(session, b.id, "Bayo")
    c2 = ledger.get_or_create_customer(session, b.id, "Chika")

    # Seed transactions
    # 1. Sale to Bayo: 50,000 total, 10,000 paid (owes 40,000)
    tx_sale1 = m.Transaction(
        business_id=b.id, customer_id=c1.id, kind="sale",
        items_json=[{"name": "Rice", "qty": 1, "line_total_kobo": to_kobo(50_000)}],
        total_kobo=to_kobo(50_000), paid_kobo=to_kobo(10_000), status="confirmed",
        created_by_user_id=u.id, source_msg_id="q-s1",
        created_at="2026-10-24T12:00:00+00:00",
    )
    # 2. Sale to Chika: 30,000 total, 0 paid (owes 30,000)
    tx_sale2 = m.Transaction(
        business_id=b.id, customer_id=c2.id, kind="sale",
        items_json=[{"name": "Beans", "qty": 2, "line_total_kobo": to_kobo(30_000)}],
        total_kobo=to_kobo(30_000), paid_kobo=0, status="confirmed",
        created_by_user_id=u.id, source_msg_id="q-s2",
        created_at="2026-10-24T13:00:00+00:00",
    )
    # 3. Expense: fuel 5,000
    tx_exp = m.Transaction(
        business_id=b.id, kind="expense", category="fuel",
        total_kobo=to_kobo(5_000), paid_kobo=to_kobo(5_000), status="confirmed",
        created_by_user_id=u.id, source_msg_id="q-e1",
        created_at="2026-10-24T14:00:00+00:00",
    )
    session.add_all([tx_sale1, tx_sale2, tx_exp])
    session.commit()

    # 1. total_owed: 40k + 30k = 70k
    res_owed = reports.total_owed(session, b.id)
    assert res_owed["total_kobo"] == to_kobo(70_000)
    assert res_owed["debtor_count"] == 2

    # 2. top_debtor: Bayo (40k)
    res_top = reports.top_debtor(session, b.id)
    assert res_top["customer"] == "Bayo"
    assert res_top["balance_kobo"] == to_kobo(40_000)

    # 3. customer_balance: Bayo -> 40k, Unknown -> 0
    res_bal_bayo = reports.customer_balance(session, b.id, "Bayo")
    assert res_bal_bayo["balance_kobo"] == to_kobo(40_000)
    assert res_bal_bayo["found"] is True

    res_bal_none = reports.customer_balance(session, b.id, "Nobody")
    assert res_bal_none["balance_kobo"] == 0
    assert res_bal_none["found"] is False

    # 4. sales_period: today -> 50k + 30k = 80k
    res_sales = reports.sales_period(session, b.id, "today", today=today)
    assert res_sales["total_kobo"] == to_kobo(80_000)
    assert res_sales["count"] == 2

    # 5. expenses_period: today -> 5k
    res_exp = reports.expenses_period(session, b.id, "today", today=today)
    assert res_exp["total_kobo"] == to_kobo(5_000)
    assert res_exp["count"] == 1

    # 6. profit_period: today -> 80k - 5k = 75k
    res_profit = reports.profit_period(session, b.id, "today", today=today)
    assert res_profit["profit_kobo"] == to_kobo(75_000)
    assert res_profit["is_estimate"] is True

    # 7. best_sellers: Beans (qty 2), Rice (qty 1)
    res_sellers = reports.best_sellers(session, b.id, "today", today=today)
    assert len(res_sellers["items"]) == 2
    assert res_sellers["items"][0]["name"] == "Beans"
    assert res_sellers["items"][0]["qty"] == 2.0


def test_query_routing_via_router(session):
    """Test router running queries and returning formatted replies."""
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Tunde")
    tx = m.Transaction(
        business_id=b.id, customer_id=c.id, kind="sale",
        total_kobo=to_kobo(25_000), paid_kobo=0, status="confirmed",
        created_by_user_id=u.id, source_msg_id="t1",
    )
    session.add(tx)
    session.commit()

    # "Wetin people owe me?" (fixture 5)
    replies = router.handle_text(session, b.id, u.id, "Wetin people owe me?", "m-owed", "pcm", llm=MockLLM())
    assert len(replies) == 1
    assert "N25,000" in replies[0].text
    assert count_tx(session) == 1  # query never writes new transactions
