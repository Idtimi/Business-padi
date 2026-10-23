import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base


@pytest.fixture()
def session():
    engine = create_engine("sqlite://")  # fresh in-memory database
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_business(s):
    user = m.User(channel="mock", channel_user_id="demo")
    s.add(user)
    s.flush()
    biz = m.Business(owner_user_id=user.id, name="Test Shop")
    s.add(biz)
    s.flush()
    return user, biz


def test_all_23_tables_created():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert len(Base.metadata.tables) == 23


def test_duplicate_message_is_rejected(session):
    user, biz = make_business(session)
    for _ in range(2):
        session.add(m.Transaction(
            business_id=biz.id, kind="sale", total_kobo=100,
            created_by_user_id=user.id, source_msg_id="msg-1"))
    with pytest.raises(IntegrityError):
        session.flush()


def test_processed_message_is_unique(session):
    session.add(m.ProcessedMessage(channel="whatsapp", message_id="abc"))
    session.flush()
    session.add(m.ProcessedMessage(channel="whatsapp", message_id="abc"))
    with pytest.raises(IntegrityError):
        session.flush()