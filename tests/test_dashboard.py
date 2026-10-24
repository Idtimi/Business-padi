import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.models as m
from app.db import Base
from app.main import app
from app.money import to_kobo
from app.pipeline import router
from app.services import dashboard, ledger
from app.services.documents import issue_document

SECRET = "s3cret"


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'dash_test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    import app.dashboard_routes as d_routes
    monkeypatch.setattr(d_routes, "SessionLocal", session_factory)

    with session_factory() as s:
        yield s, session_factory


def make_test_shop(session, name="Shop Alpha", plan="starter", user_ref="2348000000001"):
    u = m.User(channel="whatsapp", channel_user_id=user_ref, language="en", state="DONE")
    session.add(u)
    session.flush()
    biz = m.Business(owner_user_id=u.id, name=name, plan=plan)
    session.add(biz)
    session.commit()
    return u, biz


def test_acceptance_owner_types_dashboard_receives_link_and_exports_ledger(db_session):
    """Acceptance: Owner types 'dashboard', receives a link, sees real numbers matching the chat answers, and can export the ledger."""
    session, _ = db_session
    user, biz = make_test_shop(session, "Mama Ngozi Provisions", plan="trader")

    # Record some transactions for the shop
    today_iso = datetime.now(m.get_tz()).date().isoformat()
    cust = m.Customer(business_id=biz.id, name="Emeka", phone="2348022223333")
    session.add(cust)
    session.flush()

    # Sale 1: N50,000 paid in full
    tx1 = m.Transaction(
        business_id=biz.id, kind="sale", status="confirmed",
        total_kobo=to_kobo(50_000), paid_kobo=to_kobo(50_000),
        customer_id=cust.id, created_at=f"{today_iso}T09:00:00",
        created_by_user_id=user.id, source_msg_id="m1",
    )
    # Sale 2: N30,000 credit sale (paid 0)
    tx2 = m.Transaction(
        business_id=biz.id, kind="sale", status="confirmed",
        total_kobo=to_kobo(30_000), paid_kobo=0,
        customer_id=cust.id, due_date="Friday", created_at=f"{today_iso}T10:00:00",
        created_by_user_id=user.id, source_msg_id="m2",
    )
    session.add_all([tx1, tx2])
    session.commit()

    # 1. Owner types "dashboard" in chat
    replies = router.handle_text(session, biz.id, user.id, "dashboard", "msg_dash_1")
    assert len(replies) == 1
    reply_text = replies[0].text
    assert "/d/login?token=" in reply_text
    assert "expires in 15 minutes" in reply_text

    # Extract token from link
    raw_token = reply_text.split("?token=")[1].split()[0].strip()

    # Verify token is hashed in database
    from sqlalchemy import select
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    action = session.scalar(
        select(m.PendingAction).where(m.PendingAction.business_id == biz.id)
    )
    assert action is not None
    assert action.payload.get("token_hash") == token_hash

    # 2. Open dashboard via TestClient
    client = TestClient(app)
    login_resp = client.get(f"/d/login?token={raw_token}", follow_redirects=False)
    assert login_resp.status_code == 303
    assert login_resp.headers["location"] == "/d"
    assert "vl_session=" in login_resp.headers["set-cookie"]

    # Session cookie allows opening /d
    cookie = login_resp.cookies["vl_session"]
    overview_resp = client.get("/d", cookies={"vl_session": cookie})
    assert overview_resp.status_code == 200
    html = overview_resp.text
    assert "Mama Ngozi Provisions" in html
    # Overview shows real numbers matching ledger: Today's sales = N80,000, Owed = N30,000
    assert "80,000" in html
    assert "30,000" in html

    # 3. Export ledger CSV
    csv_resp = client.get("/d/ledger/export.csv", cookies={"vl_session": cookie})
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    csv_text = csv_resp.text
    assert "Transaction ID,Date,Kind,Status" in csv_text
    assert "50000" in csv_text or "5000000" in csv_text
    assert "Emeka" in csv_text


def test_expired_token_rejected(db_session):
    """Spec: expired token rejected."""
    session, _ = db_session
    user, biz = make_test_shop(session, "Expired Shop")

    # Generate token but set expiry 20 minutes ago
    raw_token = "expired_secret_token_123"
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()

    action = m.PendingAction(
        business_id=biz.id,
        user_id=user.id,
        kind="dashboard_login",
        payload={"token_hash": token_hash},
        status="open",
        expires_at=past_time,
    )
    session.add(action)
    session.commit()

    client = TestClient(app)
    resp = client.get(f"/d/login?token={raw_token}")
    assert resp.status_code == 401
    assert "expired" in resp.text.lower()


def test_used_token_cannot_be_reused(db_session):
    session, _ = db_session
    user, biz = make_test_shop(session, "One Time Shop")

    raw_token, _ = dashboard.generate_magic_link(session, user.id, biz.id)

    client = TestClient(app)
    # First login succeeds
    r1 = client.get(f"/d/login?token={raw_token}", follow_redirects=False)
    assert r1.status_code == 303

    # Second login with same token fails
    r2 = client.get(f"/d/login?token={raw_token}")
    assert r2.status_code == 401
    assert "invalid or already used" in r2.text.lower()


def test_user_cannot_open_another_business_data(db_session):
    """Spec: a user cannot open another business's data (strict business scoping)."""
    session, _ = db_session
    user_a, biz_a = make_test_shop(session, "Shop A", user_ref="2348000000001")
    user_b, biz_b = make_test_shop(session, "Shop B", user_ref="2348000000002")

    # Add customer to Shop B
    cust_b = m.Customer(business_id=biz_b.id, name="Customer Secret of B", phone="2348099999999")
    session.add(cust_b)
    session.commit()

    # Add document to Shop B
    doc_b = issue_document(session, biz_b.id, kind="receipt", total_kobo=to_kobo(15_000), customer_id=cust_b.id)

    # Owner A logs into Dashboard
    cookie_a = dashboard.create_session_cookie(user_a.id, biz_a.id)
    client = TestClient(app, cookies={"vl_session": cookie_a})

    # Owner A cannot view Customer B's detail page
    resp_cust = client.get(f"/d/customers/{cust_b.id}")
    assert resp_cust.status_code == 404

    # Owner A cannot download Customer B's statement
    resp_stmt = client.get(f"/d/customers/{cust_b.id}/statement.pdf")
    assert resp_stmt.status_code == 404

    # Owner A cannot download Document B's PDF
    resp_doc = client.get(f"/d/documents/{doc_b.id}/pdf")
    assert resp_doc.status_code == 404

    # Customer list for Shop A does NOT contain Customer B
    resp_cust_list = client.get("/d/customers")
    assert resp_cust_list.status_code == 200
    assert "Customer Secret of B" not in resp_cust_list.text


def test_unauthenticated_request_rejected():
    client = TestClient(app)
    resp = client.get("/d/overview")
    assert resp.status_code == 401


def test_plan_gating_stock_and_staff(db_session):
    """Enforce plan gating on /d/stock and /d/staff routes."""
    session, _ = db_session
    user, biz = make_test_shop(session, "Gated Shop", plan="starter")
    cookie = dashboard.create_session_cookie(user.id, biz.id)
    client = TestClient(app, cookies={"vl_session": cookie})

    # 1. On starter plan, stock is gated
    resp_stock = client.get("/d/stock")
    assert resp_stock.status_code == 200
    assert "Stock &amp; Inventory Control" in resp_stock.text
    assert "Trader" in resp_stock.text  # Upgrade CTA

    # 2. Upgrade to trader plan -> stock is now accessible
    biz.plan = "trader"
    session.commit()
    resp_stock_trader = client.get("/d/stock")
    assert resp_stock_trader.status_code == 200
    assert "Qty on Hand" in resp_stock_trader.text

    # 3. On trader plan, staff is gated
    resp_staff = client.get("/d/staff")
    assert resp_staff.status_code == 200
    assert "Staff Accounts &amp; Approvals" in resp_staff.text
    assert "Business" in resp_staff.text  # Upgrade CTA

    # 4. Upgrade to business plan -> staff is accessible
    biz.plan = "business"
    session.commit()
    resp_staff_biz = client.get("/d/staff")
    assert resp_staff_biz.status_code == 200
    assert "Team Members" in resp_staff_biz.text
