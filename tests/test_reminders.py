"""Tests for Phase 7: Debt reminders.

Covers:
- Reminder scheduling for credit sales with due dates:
  1 day before (gentle), on the day (gentle), 3 days after (firm),
  7 days after (firm), weekly up to limit (final).
- APScheduler integration & quiet hours (send window 08:00 to 20:00 Africa/Lagos only).
  At 22:00 nothing is sent.
- Full payment stops further reminders.
- Idempotency across restarts (no duplicate reminders/digests).
- At most ONE reminder digest message per trader per day.
- Commands: remind now, pause, stop, resume for customer.

NOTE: APScheduler runs in-process and requires a single worker to prevent duplicate
job executions in multi-process setups.
"""

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.services import ledger, plans, reminders

RICE_DUE_FRIDAY = (
    "I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
    "She pay fifty thousand, the rest she go pay Friday."
)


class DummyChannel:
    """Mock channel that records sent texts for verification."""
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, to_ref: str, text: str):
        self.sent.append({"to": to_ref, "text": text})


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_reminders.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Kudirat Provisions", plan="trader"):
    u = m.User(channel="mock", channel_user_id="2348012345678", state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name, plan=plan, phone="08012345678")
    s.add(b)
    s.commit()
    return u, b


def press(s, b, u, reply, index=0):
    action, pid = router.parse_button(reply.buttons[index][0])
    return router.handle_button(s, b.id, u.id, action, pid, "en")


def test_acceptance_sale_due_friday_creates_expected_schedule(session):
    """Acceptance test:
    A sale due Friday creates the expected reminder schedule:
    - 1 day before (gentle)
    - on the day (gentle)
    - 3 days after (firm)
    - 7 days after (firm)
    - +14d, +21d, +28d (final).
    """
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Due date: Friday 2026-10-30
    friday = date(2026, 10, 30)

    # Record credit sale with due date
    tx = m.Transaction(
        business_id=b.id,
        customer_id=c.id,
        kind="sale",
        total_kobo=to_kobo(127_500),
        paid_kobo=to_kobo(50_000),
        due_date=friday.isoformat(),
        status="confirmed",
        source_msg_id="tx-friday-1",
        created_by_user_id=u.id,
    )
    session.add(tx)
    session.flush()

    # Schedule reminders
    sched = reminders.schedule_reminders_for_sale(
        session=session,
        business_id=b.id,
        customer_id=c.id,
        transaction_id=tx.id,
        due_date_val=friday,
    )

    assert len(sched) == 7
    # 1 day before: Thursday 2026-10-29
    assert sched[0].send_at.startswith("2026-10-29")
    assert sched[0].tone == "gentle"
    assert sched[0].status == "scheduled"

    # On the day: Friday 2026-10-30
    assert sched[1].send_at.startswith("2026-10-30")
    assert sched[1].tone == "gentle"

    # 3 days after: Monday 2026-11-02
    assert sched[2].send_at.startswith("2026-11-02")
    assert sched[2].tone == "firm"

    # 7 days after: Friday 2026-11-06
    assert sched[3].send_at.startswith("2026-11-06")
    assert sched[3].tone == "firm"

    # Weekly (+14d, +21d, +28d)
    assert sched[4].send_at.startswith("2026-11-13")
    assert sched[4].tone == "final"
    assert sched[5].send_at.startswith("2026-11-20")
    assert sched[5].tone == "final"
    assert sched[6].send_at.startswith("2026-11-27")
    assert sched[6].tone == "final"


def test_acceptance_quiet_hours_at_22_00_nothing_is_sent(session):
    """Acceptance test: At 22:00 nothing is sent (outside 08:00 - 20:00 send window)."""
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Due date was today
    today = date(2026, 10, 24)
    tx = m.Transaction(
        business_id=b.id,
        customer_id=c.id,
        kind="sale",
        total_kobo=to_kobo(50_000),
        paid_kobo=0,
        due_date=today.isoformat(),
        status="confirmed",
        source_msg_id="tx-due-today",
        created_by_user_id=u.id,
    )
    session.add(tx)
    session.flush()

    reminders.schedule_reminders_for_sale(session, b.id, c.id, tx.id, today)

    # Freeze time to 22:00 Lagos time (UTC+1: 21:00 UTC)
    with freeze_time("2026-10-24 21:00:00"):
        channel = DummyChannel()
        processed = asyncio.run(reminders.dispatch_due_reminders(session, channel))
        assert processed == 0
        assert len(channel.sent) == 0


def test_acceptance_after_full_payment_no_reminder_is_sent(session):
    """Acceptance test: After full payment no reminder is sent."""
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    today = date(2026, 10, 24)
    # Credit sale of 50,000 NGN
    tx = m.Transaction(
        business_id=b.id,
        customer_id=c.id,
        kind="sale",
        total_kobo=to_kobo(50_000),
        paid_kobo=0,
        due_date=today.isoformat(),
        status="confirmed",
        source_msg_id="tx-unpaid",
        created_by_user_id=u.id,
    )
    session.add(tx)
    session.flush()

    reminders.schedule_reminders_for_sale(session, b.id, c.id, tx.id, today)

    # Now customer pays the entire 50,000 NGN balance via ledger allocation
    ledger._allocate_payment(session, b.id, c.id, to_kobo(50_000), "pay-msg-1")
    session.flush()

    # Sync reminders after payment
    cancelled_count = reminders.sync_reminders_after_payment(session, b.id, c.id)
    assert cancelled_count > 0

    # During daytime window (10:00 Lagos time)
    with freeze_time("2026-10-24 09:00:00"):
        channel = DummyChannel()
        processed = asyncio.run(reminders.dispatch_due_reminders(session, channel))
        # Zero reminders sent because balance is settled
        assert processed == 0
        assert len(channel.sent) == 0


def test_reminder_digest_contains_wa_me_link_and_sends_at_most_once_per_day(session):
    """Test daily reminder digest groups multiple reminders with wa.me links
    and enforces Part M Rule 3 (at most ONE digest per day per trader)."""
    u, b = make_business(session)
    c1 = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")
    c1.phone = "08011111111"
    c2 = ledger.get_or_create_customer(session, b.id, "Baba Segun")
    c2.phone = "08022222222"

    today = date(2026, 10, 24)
    # Two credit sales due today
    for c, amt in [(c1, 40_000), (c2, 30_000)]:
        tx = m.Transaction(
            business_id=b.id, customer_id=c.id, kind="sale",
            total_kobo=to_kobo(amt), paid_kobo=0,
            due_date=today.isoformat(), status="confirmed",
            source_msg_id=f"tx-{c.name}", created_by_user_id=u.id,
        )
        session.add(tx)
        session.flush()
        reminders.schedule_reminders_for_sale(session, b.id, c.id, tx.id, today)

    # 10:00 Lagos time (within send window)
    with freeze_time("2026-10-24 09:00:00"):
        channel = DummyChannel()
        count = asyncio.run(reminders.dispatch_due_reminders(session, channel))
        assert count == 2
        # Exactly ONE digest message sent to the trader!
        assert len(channel.sent) == 1
        digest = channel.sent[0]["text"]
        assert "Mama Ngozi" in digest
        assert "Baba Segun" in digest
        assert "https://wa.me/" in digest

        # Calling dispatch again on the same day sends NOTHING (at most one digest per day)
        second_channel = DummyChannel()
        second_count = asyncio.run(reminders.dispatch_due_reminders(session, second_channel))
        assert second_count == 0
        assert len(second_channel.sent) == 0


def test_reminder_commands_via_router(session):
    """Test trader commands: remind now, pause, stop, resume."""
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")
    c.phone = "08012345678"

    today = date(2026, 10, 24)
    tx = m.Transaction(
        business_id=b.id, customer_id=c.id, kind="sale",
        total_kobo=to_kobo(42_500), paid_kobo=0,
        due_date=today.isoformat(), status="confirmed",
        source_msg_id="tx-cmd-1", created_by_user_id=u.id,
    )
    session.add(tx)
    session.flush()
    reminders.schedule_reminders_for_sale(session, b.id, c.id, tx.id, today)

    # 1. Remind now command
    replies = router.handle_text(session, b.id, u.id, "remind now Mama Ngozi", "msg-rem-1")
    assert len(replies) == 1
    assert "https://wa.me/" in replies[0].text
    assert "Mama Ngozi" in replies[0].text

    # 2. Pause reminders
    pause_replies = router.handle_text(session, b.id, u.id, "pause Mama Ngozi", "msg-pause-1")
    assert "paused" in pause_replies[0].text
    sched_rem = session.scalars(select(m.Reminder).where(m.Reminder.customer_id == c.id, m.Reminder.status == "scheduled")).all()
    assert len(sched_rem) == 0

    # 3. Resume reminders
    resume_replies = router.handle_text(session, b.id, u.id, "resume Mama Ngozi", "msg-res-1")
    assert "resumed" in resume_replies[0].text
    sched_rem = session.scalars(select(m.Reminder).where(m.Reminder.customer_id == c.id, m.Reminder.status == "scheduled")).all()
    assert len(sched_rem) > 0

    # 4. Stop reminders
    stop_replies = router.handle_text(session, b.id, u.id, "stop Mama Ngozi", "msg-stop-1")
    assert "stopped" in stop_replies[0].text
    session.refresh(c)
    assert bool(c.opted_out) is True
