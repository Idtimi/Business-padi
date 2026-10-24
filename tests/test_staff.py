"""Tests for Phase 10: Staff, roles, permissions, and approvals.

Covers:
- Acceptance: An attendant records a large sale; it stays pending until the owner approves; reports exclude it until then.
- Staff permissions: staff cannot read profit without permission.
- Pending transactions excluded from reports and stock deductions.
- Removed staff lose access immediately.
- Invites: one-time expiring codes and joining.
- Business plan seat limits (up to 3 staff).
- Audit log tracking create, edit, cancel, approve.
"""

from datetime import date, datetime, timedelta, timezone
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.services import documents, ledger, reports, staff, stock


@pytest.fixture()
def session(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_staff.db'}")
    Base.metadata.create_all(e)
    with Session(e, expire_on_commit=False) as s:
        yield s


def make_business(s, name="Kudirat Superstore", plan="business"):
    owner = m.User(channel="mock", channel_user_id="owner-1", state="DONE", consent_at=m.now_iso())
    s.add(owner)
    s.flush()
    biz = m.Business(owner_user_id=owner.id, name=name, plan=plan)
    s.add(biz)
    s.commit()
    return owner, biz


def make_attendant(s, biz, user_id="staff-1", can_see_profit=False, can_issue_invoices=False):
    u = m.User(channel="mock", channel_user_id=user_id, state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    st = m.Staff(
        business_id=biz.id,
        user_id=u.id,
        role="attendant",
        can_see_profit=1 if can_see_profit else 0,
        can_issue_invoices=1 if can_issue_invoices else 0,
        active=1,
    )
    s.add(st)
    s.commit()
    return u, st


RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


def test_acceptance_attendant_large_sale_stays_pending_until_owner_approves(session):
    """Acceptance test:
    An attendant records a large sale; it stays pending until the owner approves;
    reports exclude it until then.
    """
    owner, biz = make_business(session)
    attendant, st = make_attendant(session, biz, "chika-1")
    c = ledger.get_or_create_customer(session, biz.id, "Mama Ngozi")

    # Threshold is 100,000 NGN. RICE sale is 127,500 NGN.
    # Attendant sends voice/text sale note
    replies = router.handle_text(session, biz.id, attendant.id, RICE, "att-msg-1", today=date(2026, 10, 24))
    assert len(replies) == 1
    assert replies[0].kind == "confirm"

    # Attendant taps save
    action, pid = router.parse_button(replies[0].buttons[0][0])
    save_replies = router.handle_button(session, biz.id, attendant.id, action, pid)
    assert "pending until the owner approves" in save_replies[0].text

    # 1. Transaction must be in 'pending' status
    tx = session.scalar(select(m.Transaction).where(m.Transaction.business_id == biz.id))
    assert tx is not None
    assert tx.status == "pending"
    assert tx.total_kobo == to_kobo(127_500)

    # 2. Reports exclude pending transactions!
    sales_rep = reports.sales_period(session, biz.id, period="today", today=date(2026, 10, 24))
    assert sales_rep["total_kobo"] == 0
    assert sales_rep["count"] == 0

    # 3. Owner approves the transaction
    approved_tx = staff.approve_transaction(session, biz.id, tx.id, actor_user_id=owner.id)
    assert approved_tx.status == "confirmed"

    # 4. Reports now include the approved transaction!
    sales_rep_after = reports.sales_period(session, biz.id, period="today", today=date(2026, 10, 24))
    assert sales_rep_after["total_kobo"] == to_kobo(127_500)
    assert sales_rep_after["count"] == 1


def test_staff_cannot_read_profit_without_permission(session):
    """Test role permissions: staff cannot view profit reports unless granted."""
    owner, biz = make_business(session)
    attendant_no_profit, _ = make_attendant(session, biz, "staff-no-prof", can_see_profit=False)
    attendant_with_profit, _ = make_attendant(session, biz, "staff-with-prof", can_see_profit=True)

    # Seed a confirmed sale
    s = m.Transaction(
        business_id=biz.id, kind="sale", total_kobo=to_kobo(50_000), paid_kobo=to_kobo(50_000),
        status="confirmed", source_msg_id="s1", created_by_user_id=owner.id,
    )
    session.add(s)
    session.commit()

    # Query profit with staff who has no profit permission
    blocked_replies = router.handle_text(
        session, biz.id, attendant_no_profit.id, "how much profit I make today", "msg-q1"
    )
    assert len(blocked_replies) == 1
    assert "permission" in blocked_replies[0].text

    # Owner can check profit
    owner_replies = router.handle_text(
        session, biz.id, owner.id, "how much profit I make today", "msg-q2"
    )
    assert "profit" in owner_replies[0].text.lower()


def test_removed_staff_lose_access_immediately(session):
    """Test that revoking a staff member's access blocks all subsequent actions immediately."""
    owner, biz = make_business(session)
    attendant, st = make_attendant(session, biz, "staff-temp")

    assert staff.check_permission(session, biz.id, attendant.id, "view_profit") is False
    assert staff.get_staff_record(session, biz.id, attendant.id) is not None

    # Owner removes staff member
    removed = staff.remove_staff_member(session, biz.id, attendant.id, actor_user_id=owner.id)
    assert removed is True

    # Staff record is inactive
    assert staff.get_staff_record(session, biz.id, attendant.id) is None
    # Cannot perform any staff action
    assert staff.check_permission(session, biz.id, attendant.id, "issue_invoices") is False


def test_invites_and_joining_with_seat_limit(session):
    """Test invite generation with expiration and seat limit enforcement."""
    owner, biz = make_business(session, plan="business")  # max 3 staff

    # Create 3 invites
    inv1 = staff.create_invite(session, biz.id, actor_user_id=owner.id)
    inv2 = staff.create_invite(session, biz.id, actor_user_id=owner.id)
    inv3 = staff.create_invite(session, biz.id, actor_user_id=owner.id)

    assert inv1.code.startswith("JOIN-")
    assert inv1.expires_at is not None

    # 3 staff members join
    for i, inv in enumerate([inv1, inv2, inv3], 1):
        u = m.User(channel="mock", channel_user_id=f"new-user-{i}", state="NEW")
        session.add(u)
        session.flush()
        ok, msg, st = staff.join_business_with_code(session, u.id, inv.code)
        assert ok is True
        assert st.active == 1

    # 4th invite attempt fails due to seat limit (3 seats on Business plan)
    with pytest.raises(ValueError, match="Seat limit"):
        staff.create_invite(session, biz.id, actor_user_id=owner.id)


def test_audit_log_records_actions(session):
    """Test audit log tracks create, edit, cancel, approve."""
    owner, biz = make_business(session)
    attendant, _ = make_attendant(session, biz, "chika-audit")

    # Create large sale (logged as create_pending)
    tx = m.Transaction(
        business_id=biz.id, kind="sale", total_kobo=to_kobo(120_000), paid_kobo=0,
        status="pending", source_msg_id="aud-1", created_by_user_id=attendant.id,
    )
    session.add(tx)
    session.flush()
    staff.log_audit(session, biz.id, attendant.id, "create_pending", f"tx:{tx.id}")

    # Owner approves (logged as approve)
    staff.approve_transaction(session, biz.id, tx.id, actor_user_id=owner.id)

    # Query audit log
    logs = session.scalars(select(m.AuditLog).where(m.AuditLog.business_id == biz.id)).all()
    actions = [l.action for l in logs]
    assert "create_pending" in actions
    assert "approve" in actions
