import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.i18n import t
from app.pipeline import router
from app.pipeline.llm import MockLLM

RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")
TWO = "Two sales: five bags beans to Chika cash, 30k each, and one bag rice to Bayo on credit, 43k."


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Shop"):
    u = m.User(channel="mock", channel_user_id=name)
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name)
    s.add(b)
    s.commit()
    return u, b


def count(s, model):
    return s.scalar(select(func.count()).select_from(model))


def press(s, b, u, reply, index=0):
    action, pid = router.parse_button(reply.buttons[index][0])
    return router.handle_button(s, b.id, u.id, action, pid, "en")


class ScriptedLLM:
    """Asks a question first; understands the answer when it contains '5 bags'."""
    def __init__(self):
        self.seen = []

    def complete_json(self, system, user, schema_name):
        self.seen.append(user)
        if "5 bags" in user:
            return {"language_detected": "english", "transactions": [{
                "intent": "sale", "customer": "Tunde", "payment": "cash",
                "items": [{"name": "rice", "qty": 5, "unit": "bag", "unit_price": 40000}]}]}
        return {"language_detected": "pidgin", "transactions": [{
            "intent": "sale", "customer": "Tunde", "items": [{"name": "rice"}],
            "needs_clarification": "How many bags of rice, and at what price?"}]}


def test_voice_sale_to_saved(session):
    u, b = make_business(session)
    replies = router.handle_text(session, b.id, u.id, RICE, "m1", "en", llm=MockLLM())
    assert len(replies) == 1
    r = replies[0]
    assert r.kind == "confirm"
    assert "N127,500" in r.text and "N77,500" in r.text and "Mama Ngozi" in r.text
    assert "Fri 30 Oct" in r.text
    assert [title for _, title in r.buttons] == ["Save", "Edit", "Cancel"]
    assert count(session, m.Transaction) == 0  # nothing saved before Save
    out = press(session, b, u, r)
    assert "Saved." in out[0].text and "N77,500" in out[0].text
    assert count(session, m.Transaction) == 1


def test_duplicate_message_is_ignored(session):
    u, b = make_business(session)
    first = router.handle_text(session, b.id, u.id, RICE, "m1", "en", llm=MockLLM())
    second = router.handle_text(session, b.id, u.id, RICE, "m1", "en", llm=MockLLM())
    assert len(first) == 1 and second == []
    assert count(session, m.PendingAction) == 1


def test_two_sales_in_one_note(session):
    u, b = make_business(session)
    replies = router.handle_text(session, b.id, u.id, TWO, "m1", "en", llm=MockLLM())
    assert [r.kind for r in replies] == ["confirm", "confirm"]
    for r in replies:
        press(session, b, u, r)
    ids = set(session.scalars(select(m.Transaction.source_msg_id)))
    assert ids == {"m1#0", "m1#1"}


def test_clarifying_question_continues_the_same_note(session):
    u, b = make_business(session)
    llm = ScriptedLLM()
    q = router.handle_text(session, b.id, u.id, "I sell rice give Tunde.", "m1", "en", llm=llm)
    assert q[0].kind == "clarify"
    r = router.handle_text(session, b.id, u.id, "5 bags at 40k each, cash", "m2", "en", llm=llm)
    assert r[0].kind == "confirm" and "N200,000" in r[0].text
    assert llm.seen[1].startswith("I sell rice give Tunde.") and "5 bags" in llm.seen[1]
    open_clarify = session.scalar(select(func.count()).select_from(m.PendingAction).where(
        m.PendingAction.kind == "clarify", m.PendingAction.status == "open"))
    assert open_clarify == 0


def test_cancel_saves_nothing(session):
    u, b = make_business(session)
    r = router.handle_text(session, b.id, u.id, RICE, "m1", "en", llm=MockLLM())[0]
    out = press(session, b, u, r, index=2)
    assert out[0].text == t("en", "cancelled")
    assert count(session, m.Transaction) == 0


def test_question_is_not_saved_as_a_transaction(session):
    u, b = make_business(session)
    replies = router.handle_text(session, b.id, u.id, "How I sell this week?", "m1", "en", llm=MockLLM())
    assert len(replies) == 1 and replies[0].kind == "info"
    assert replies[0].text == t("en", "not_yet")
    assert count(session, m.PendingAction) == 0


def test_other_business_cannot_press_save(session):
    u1, b1 = make_business(session, "A")
    u2, b2 = make_business(session, "B")
    r = router.handle_text(session, b1.id, u1.id, RICE, "m1", "en", llm=MockLLM())[0]
    action, pid = router.parse_button(r.buttons[0][0])
    out = router.handle_button(session, b2.id, u2.id, action, pid, "en")
    assert out[0].text == t("en", "try_again")
    assert count(session, m.Transaction) == 0