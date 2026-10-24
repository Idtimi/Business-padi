import os
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models as m
from app.db import Base
from app.main import app
from app.money import to_kobo
from app.pipeline import router
from app.services import profile


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'consent_test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.profile_routes as p_routes
    monkeypatch.setattr(p_routes, "SessionLocal", session_factory)

    with session_factory() as s:
        yield s, session_factory


def make_shop_with_history(session, name="Lagos Super Mart", plan="trader"):
    u = m.User(channel="whatsapp", channel_user_id="2348033334444", language="en", state="DONE")
    session.add(u)
    session.flush()

    biz = m.Business(owner_user_id=u.id, name=name, plan=plan)
    session.add(biz)
    session.flush()

    # Create 3 customers
    c1 = m.Customer(business_id=biz.id, name="Chief Okon", phone="2348011110001")
    c2 = m.Customer(business_id=biz.id, name="Madam Kemi", phone="2348011110002")
    c3 = m.Customer(business_id=biz.id, name="Alhaji Danladi", phone="2348011110003")
    session.add_all([c1, c2, c3])
    session.flush()

    # Dates: spanning 4 weeks ago
    base_date = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # 4 weeks ago sale
    d_4w = (base_date - timedelta(days=28)).isoformat()
    # 2 weeks ago sale
    d_2w = (base_date - timedelta(days=14)).isoformat()
    # 1 week ago payment
    d_1w = (base_date - timedelta(days=7)).isoformat()
    # Today sale
    d_today = base_date.isoformat()

    tx1 = m.Transaction(
        business_id=biz.id, kind="sale", status="confirmed",
        total_kobo=to_kobo(100_000), paid_kobo=to_kobo(100_000),
        customer_id=c1.id, created_at=d_4w,
        created_by_user_id=u.id, source_msg_id="m1",
    )
    tx2 = m.Transaction(
        business_id=biz.id, kind="sale", status="confirmed",
        total_kobo=to_kobo(200_000), paid_kobo=to_kobo(50_000),
        customer_id=c2.id, due_date="Friday", created_at=d_2w,
        created_by_user_id=u.id, source_msg_id="m2",
    )
    # Payment of 150k settling tx2
    p1 = m.Payment(
        business_id=biz.id, customer_id=c2.id, transaction_id=2,
        amount_kobo=to_kobo(150_000), created_at=d_1w,
    )
    # Unsettled credit sale to Alhaji Danladi
    tx3 = m.Transaction(
        business_id=biz.id, kind="sale", status="confirmed",
        total_kobo=to_kobo(80_000), paid_kobo=0,
        customer_id=c3.id, due_date="Next Week", created_at=d_today,
        created_by_user_id=u.id, source_msg_id="m3",
    )

    session.add_all([tx1, tx2, p1, tx3])
    session.commit()
    return u, biz, [c1, c2, c3]


def test_acceptance_partner_link_shows_metrics_and_stops_working_after_revoke_or_expiry(db_session):
    """Acceptance: A sample partner link shows the metrics and stops working after revoke or expiry."""
    session, _ = db_session
    user, biz, customers = make_shop_with_history(session, plan="trader")

    # 1. Create a consent record for a microfinance partner
    partner_name = "Lydia Microfinance Bank"
    consent = profile.create_consent_record(session, biz.id, partner=partner_name, scope="read_profile", expires_in_days=30)
    assert consent.id is not None
    assert consent.revoked_at is None

    # 2. Create tokenized expiring share link
    raw_token, share_url, link = profile.create_share_link(session, consent.id, expires_in_days=7)
    assert link.token_hash is not None
    assert f"/profile/shared/{raw_token}" in share_url

    # 3. Partner accesses the link via HTTP GET
    client = TestClient(app)
    resp = client.get(f"/profile/shared/{raw_token}")
    assert resp.status_code == 200
    html = resp.text

    # Shows verified trade metrics
    assert "Lagos Super Mart" in html
    assert "Verified Financial Trade Profile" in html
    assert "Record Maturity" in html
    assert "Active-Days Ratio" in html
    assert "Credit Collection Rate" in html
    assert "Avg Days to Collect" in html
    assert "Total Outstanding Debt" in html
    assert "Debt Concentration" in html
    assert "Monthly Turnover Trend" in html

    # Audit log records the access
    audit_rows = session.scalars(
        select(m.AuditLog).where(
            m.AuditLog.business_id == biz.id,
            m.AuditLog.action == "profile_shared_accessed",
        )
    ).all()
    assert len(audit_rows) == 1
    assert "partner:Lydia Microfinance Bank" in audit_rows[0].ref

    # Download PDF profile
    pdf_resp = client.get(f"/profile/shared/{raw_token}/pdf")
    assert pdf_resp.status_code == 200
    assert "application/pdf" in pdf_resp.headers["content-type"]

    # 4. Trader revokes consent
    profile.revoke_consent(session, biz.id, consent.id, actor_user_id=user.id)
    session.expire_all()

    # 5. Accessing the link now fails with 403 Forbidden
    resp_revoked = client.get(f"/profile/shared/{raw_token}")
    assert resp_revoked.status_code == 403
    assert "revoked" in resp_revoked.text.lower()

    # PDF download also fails with 403
    pdf_resp_revoked = client.get(f"/profile/shared/{raw_token}/pdf")
    assert pdf_resp_revoked.status_code == 403


def test_expired_link_blocks_access(db_session):
    """Spec: expired link blocks access."""
    session, _ = db_session
    user, biz, _ = make_shop_with_history(session, plan="trader")

    consent = profile.create_consent_record(session, biz.id, partner="QuickLend", scope="read_profile", expires_in_days=30)
    raw_token, _, link = profile.create_share_link(session, consent.id, expires_in_days=7)

    # Manually expire the link
    link.expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    session.commit()

    client = TestClient(app)
    resp = client.get(f"/profile/shared/{raw_token}")
    assert resp.status_code == 403
    assert "expired" in resp.text.lower()


def test_shared_output_contains_no_personal_customer_data(db_session):
    """Spec: no personal customer data in the shared output (names, phones strictly omitted)."""
    session, _ = db_session
    user, biz, customers = make_shop_with_history(session, plan="trader")

    consent = profile.create_consent_record(session, biz.id, partner="Partner Audit", scope="read_profile", expires_in_days=30)
    raw_token, _, _ = profile.create_share_link(session, consent.id)

    client = TestClient(app)
    resp = client.get(f"/profile/shared/{raw_token}")
    assert resp.status_code == 200
    html = resp.text

    # None of the customer names or phones appear anywhere in the HTML!
    for c in customers:
        assert c.name not in html
        assert c.phone not in html

    # Check generated PDF
    pdf_path = profile.generate_profile_pdf(session, biz.id, consent.id)
    assert os.path.exists(pdf_path)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    # Neither customer names nor phones appear in the PDF binary content
    for c in customers:
        assert c.name.encode() not in pdf_bytes
        assert c.phone.encode() not in pdf_bytes


def test_plan_gating_financial_profile(db_session):
    """Spec: gate by plan (Starter is blocked; Trader/Business allowed)."""
    session, _ = db_session
    u = m.User(channel="whatsapp", channel_user_id="2348000000001", language="en", state="DONE")
    session.add(u)
    session.flush()

    biz = m.Business(owner_user_id=u.id, name="Starter Mart", plan="starter")
    session.add(biz)
    session.commit()

    # Starter plan trader asks for profile in chat
    replies = router.handle_text(session, biz.id, u.id, "profile", "msg_p_1")
    assert len(replies) == 1
    assert "available on Trader and Business plans" in replies[0].text

    # Upgrade to Trader -> profile accessible
    biz.plan = "trader"
    session.commit()
    replies_trader = router.handle_text(session, biz.id, u.id, "profile", "msg_p_2")
    assert len(replies_trader) == 1
    assert "Verified Financial Trade Profile" in replies_trader[0].text
    assert "Record History" in replies_trader[0].text


def test_whatsapp_consent_approval_and_rejection_buttons(db_session):
    """Consent approval and decline via interactive buttons."""
    session, _ = db_session
    u = m.User(channel="whatsapp", channel_user_id="2348000000002", language="en", state="DONE")
    session.add(u)
    session.flush()
    biz = m.Business(owner_user_id=u.id, name="Shop Lagos", plan="trader")
    session.add(biz)
    session.flush()

    # 1. Partner creates pending consent request
    p_act = m.PendingAction(
        business_id=biz.id,
        user_id=u.id,
        kind="consent_approval",
        payload={"partner": "FirstCity Bank", "scope": "read_profile", "expires_in_days": 30},
        status="open",
    )
    session.add(p_act)
    session.commit()

    # Trader taps Approve
    replies_app = router.handle_button(session, biz.id, u.id, "approve_consent", p_act.id)
    assert len(replies_app) == 1
    assert "Consent approved for FirstCity Bank" in replies_app[0].text
    assert "/profile/shared/" in replies_app[0].text

    # 2. Trader declines another request
    p_act2 = m.PendingAction(
        business_id=biz.id,
        user_id=u.id,
        kind="consent_approval",
        payload={"partner": "Unknown App", "scope": "read_profile", "expires_in_days": 30},
        status="open",
    )
    session.add(p_act2)
    session.commit()

    replies_rej = router.handle_button(session, biz.id, u.id, "reject_consent", p_act2.id)
    assert len(replies_rej) == 1
    assert "declined" in replies_rej[0].text.lower()
