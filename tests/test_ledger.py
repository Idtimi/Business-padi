import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline.llm import MockLLM
from app.pipeline.schemas import ExtractionResult, Extracted, Item
from app.pipeline.validate import validate
from app.services import ledger, pending

RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Shop"):
    u = m.User(channel="mock", channel_user_id=name)
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name)
    s.add(b)
    s.flush()
    s.commit()
    return u, b


def first(text):
    raw = MockLLM().complete_json("", text, "extraction")
    return validate(ExtractionResult.model_validate(raw).transactions[0])


def count_tx(s):
    return s.scalar(select(func.count()).select_from(m.Transaction))


def test_nothing_saved_until_confirm_then_balance_correct(session):
    u, b = make_business(session)
    p = ledger.propose(session, b.id, u.id, first(RICE), RICE, "msg-1")
    assert count_tx(session) == 0
    tx = ledger.confirm(session, b.id, p.id)
    assert count_tx(session) == 1
    assert tx.total_kobo == to_kobo(127_500)
    cust = session.scalar(select(m.Customer))
    assert ledger.customer_balance_kobo(session, b.id, cust.id) == to_kobo(77_500)


def test_confirming_twice_does_not_duplicate(session):
    u, b = make_business(session)
    p = ledger.propose(session, b.id, u.id, first(RICE), RICE, "msg-1")
    a = ledger.confirm(session, b.id, p.id)
    b2 = ledger.confirm(session, b.id, p.id)
    assert a.id == b2.id
    assert count_tx(session) == 1


def test_cancelled_cannot_be_confirmed(session):
    u, b = make_business(session)
    p = ledger.propose(session, b.id, u.id, first(RICE), RICE, "msg-1")
    pending.cancel(session, b.id, p.id)
    with pytest.raises(ValueError):
        ledger.confirm(session, b.id, p.id)
    assert count_tx(session) == 0


def test_pending_survives_restart(engine):
    with Session(engine, expire_on_commit=False) as s1:
        u, b = make_business(s1)
        p = ledger.propose(s1, b.id, u.id, first(RICE), RICE, "msg-1")
        uid, bid, pid = u.id, b.id, p.id
    # brand-new session = the server restarted
    with Session(engine, expire_on_commit=False) as s2:
        tx = ledger.confirm(s2, bid, pid)
        assert tx.total_kobo == to_kobo(127_500)


def test_other_business_cannot_confirm(session):
    u1, b1 = make_business(session, "A")
    u2, b2 = make_business(session, "B")
    p = ledger.propose(session, b1.id, u1.id, first(RICE), RICE, "msg-1")
    with pytest.raises(LookupError):
        ledger.confirm(session, b2.id, p.id)


def test_expired_pending_is_refused(session):
    u, b = make_business(session)
    p = ledger.propose(session, b.id, u.id, first(RICE), RICE, "msg-1", ttl_minutes=-1)
    with pytest.raises(ValueError):
        ledger.confirm(session, b.id, p.id)
    assert count_tx(session) == 0


def test_payment_reduces_balance(session):
    u, b = make_business(session)
    p1 = ledger.propose(session, b.id, u.id, first(RICE), RICE, "msg-1")
    ledger.confirm(session, b.id, p1.id)
    pay = "Mama Ngozi don pay twenty thousand."
    p2 = ledger.propose(session, b.id, u.id, first(pay), pay, "msg-2")
    ledger.confirm(session, b.id, p2.id)
    cust = session.scalar(select(m.Customer))
    assert ledger.customer_balance_kobo(session, b.id, cust.id) == to_kobo(57_500)


def test_credit_sale_needs_a_customer_name(session):
    u, b = make_business(session)
    x = Extracted(intent="sale", payment="credit",
                  items=[Item(name="x", qty=1, unit_price=100)])
    with pytest.raises(ValueError):
        ledger.propose(session, b.id, u.id, validate(x), "t", "msg-9")