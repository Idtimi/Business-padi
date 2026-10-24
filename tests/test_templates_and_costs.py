import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

import app.models as m
import app.whatsapp_routes as routes
from app.channels.mock import MockChannel
from app.config import settings
from app.db import Base
from app.main import app
from app.money import to_kobo
from app.pipeline import router
from app.pipeline.handler import IncomingMessage, handle_incoming
from app.services import costs, templates
from app.services.templates import (
    NotOptedInError,
    OptedOutError,
    WindowClosedError,
    build_document_delivery_components,
    build_payment_reminder_components,
    send_freeform_with_window_check,
    send_utility_template,
)
from scripts.price_check import run_price_check

SECRET = "s3cret"
URL = "/whatsapp/webhook"
SENDER = "2348011112222"


@pytest.fixture()
def db_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'costs_test.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as s:
        yield s, session_factory


def test_freeform_sends_outside_24h_window_are_blocked(db_session):
    """Spec: free-form sends outside the window are blocked."""
    session, _ = db_session
    channel = MockChannel()
    now_utc = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # 1. Recipient has never messaged us -> window is closed
    with pytest.raises(WindowClosedError):
        import asyncio
        asyncio.run(send_freeform_with_window_check(
            channel, session, business_id=1, to_phone=SENDER, text="Hello trader", now=now_utc
        ))

    # 2. Recipient messaged 25 hours ago -> window is closed
    old_time = (now_utc - timedelta(hours=25)).isoformat()
    u = m.User(channel="whatsapp", channel_user_id=SENDER, language="en", state="DONE",
               last_user_message_at=old_time)
    session.add(u)
    session.commit()

    with pytest.raises(WindowClosedError):
        import asyncio
        asyncio.run(send_freeform_with_window_check(
            channel, session, business_id=1, to_phone=SENDER, text="Hello trader", now=now_utc
        ))

    # 3. Recipient messaged 2 hours ago -> window is OPEN, free-form send succeeds
    recent_time = (now_utc - timedelta(hours=2)).isoformat()
    u.last_user_message_at = recent_time
    session.commit()

    import asyncio
    log_entry = asyncio.run(send_freeform_with_window_check(
        channel, session, business_id=1, to_phone=SENDER, text="Hello trader", now=now_utc
    ))
    assert log_entry is not None
    assert log_entry.category == "service"
    assert log_entry.status == "sent"
    assert len(channel.sent) == 1
    assert channel.sent[0]["type"] == "text"


def test_template_message_to_opted_in_customer_is_sent_and_tracked(db_session):
    """Spec: a template message to an opted-in customer is sent and tracked outside the window."""
    session, _ = db_session
    channel = MockChannel()
    now_utc = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)

    # Create business and opted-in customer
    biz = m.Business(owner_user_id=1, name="Alhaja Stores")
    session.add(biz)
    session.commit()

    cust = m.Customer(business_id=biz.id, name="Mama Ngozi", phone=SENDER, opted_in=1, opted_out=0)
    session.add(cust)
    session.commit()

    # User last messaged 3 days ago (outside 24h window)
    u = m.User(channel="whatsapp", channel_user_id=SENDER, language="en", state="DONE",
               last_user_message_at=(now_utc - timedelta(days=3)).isoformat())
    session.add(u)
    session.commit()

    components = build_document_delivery_components(
        doc_type="receipt",
        doc_number="R-0001",
        amount_kobo=to_kobo(77_500),
        customer_name="Mama Ngozi",
        business_name="Alhaja Stores",
        media_id="MEDIA_DOC_1",
        filename="Receipt_R-0001.pdf",
    )

    import asyncio
    log_entry = asyncio.run(send_utility_template(
        channel=channel,
        session=session,
        business_id=biz.id,
        to_phone=SENDER,
        template_name=templates.TEMPLATE_DOCUMENT_DELIVERY,
        language="en",
        components=components,
        require_opt_in=True,
    ))

    assert log_entry is not None
    assert log_entry.category == "utility"
    assert log_entry.template_name == templates.TEMPLATE_DOCUMENT_DELIVERY
    assert log_entry.status == "sent"
    assert log_entry.wa_message_id is not None
    assert len(channel.sent) == 1
    assert channel.sent[0]["type"] == "template"
    assert channel.sent[0]["template_name"] == templates.TEMPLATE_DOCUMENT_DELIVERY


def test_stop_message_opts_out_and_prevents_further_sends(db_session):
    """Spec: a STOP message prevents further sends; opt-out stops messages."""
    session, factory = db_session
    channel = MockChannel()

    biz = m.Business(owner_user_id=1, name="Alhaja Stores")
    session.add(biz)
    session.commit()

    cust = m.Customer(business_id=biz.id, name="Emeka", phone=SENDER, opted_in=1, opted_out=0)
    session.add(cust)
    session.commit()

    # 1. Customer sends incoming STOP message
    stop_msg = IncomingMessage(
        channel="whatsapp",
        user_ref=SENDER,
        message_id="msg_stop_1",
        kind="text",
        text="STOP",
        media_ref=None,
        button_id=None,
        timestamp=datetime.now(timezone.utc),
    )

    import asyncio
    asyncio.run(handle_incoming(channel, stop_msg, session))

    # Verify customer is now opted out
    session.expire_all()
    cust_updated = session.get(m.Customer, cust.id)
    assert cust_updated.opted_out == 1

    # Confirmation reply was sent
    assert len(channel.sent) >= 1
    assert "opted out" in channel.sent[-1]["text"].lower()

    # 2. Further attempts to send to this customer are blocked with OptedOutError
    with pytest.raises(OptedOutError):
        asyncio.run(send_utility_template(
            channel=channel,
            session=session,
            business_id=biz.id,
            to_phone=SENDER,
            template_name=templates.TEMPLATE_PAYMENT_REMINDER,
        ))

    with pytest.raises(OptedOutError):
        asyncio.run(send_freeform_with_window_check(
            channel=channel,
            session=session,
            business_id=biz.id,
            to_phone=SENDER,
            text="Trying to text you",
        ))

    # 3. Customer sends START to opt back in
    start_msg = IncomingMessage(
        channel="whatsapp",
        user_ref=SENDER,
        message_id="msg_start_1",
        kind="text",
        text="START",
        media_ref=None,
        button_id=None,
        timestamp=datetime.now(timezone.utc),
    )
    asyncio.run(handle_incoming(channel, start_msg, session))

    session.expire_all()
    cust_reopt = session.get(m.Customer, cust.id)
    assert cust_reopt.opted_out == 0
    assert cust_reopt.opted_in == 1


def test_failed_delivery_status_updates_log(tmp_path, monkeypatch):
    """Spec: a failed delivery status updates the log."""
    engine = create_engine(f"sqlite:///{tmp_path / 'webhook_status.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    channel = MockChannel()
    monkeypatch.setattr(routes, "SessionLocal", factory)
    monkeypatch.setattr(routes, "get_channel", lambda: channel)
    monkeypatch.setattr(settings, "whatsapp_app_secret", SECRET)

    mid = "wamid.HBgL12345"
    with factory() as s:
        log_entry = m.WaMessageLog(
            business_id=1,
            to_ref="********2222",
            category="utility",
            msg_type="template",
            template_name="payment_reminder",
            wa_message_id=mid,
            status="sent",
            est_cost_kobo=1078,
            created_at=m.now_iso(),
        )
        s.add(log_entry)
        s.commit()

    import hashlib
    import hmac

    payload_dict = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "1",
            "changes": [{
                "field": "messages",
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1"},
                    "statuses": [{
                        "id": mid,
                        "status": "failed",
                        "timestamp": "1761219600",
                        "recipient_id": SENDER,
                        "errors": [{"code": 131026, "title": "Message undeliverable"}],
                    }],
                },
            }],
        }],
    }
    raw_body = json.dumps(payload_dict).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()

    client = TestClient(app)
    resp = client.post(URL, content=raw_body, headers={
        "X-Hub-Signature-256": sig,
        "Content-Type": "application/json",
    })
    assert resp.status_code == 200

    # Verify status in database updated to "failed"
    with factory() as s:
        row = s.scalar(select(m.WaMessageLog).where(m.WaMessageLog.wa_message_id == mid))
        assert row is not None
        assert row.status == "failed"


def test_cost_report_equals_the_sum_of_wa_message_log(db_session):
    """Spec: the cost report equals the sum of wa_message_log exactly."""
    session, _ = db_session
    now_date = datetime.now(m.get_tz()).date()
    today_str = now_date.isoformat()

    biz1 = m.Business(owner_user_id=1, name="Shop Alpha")
    biz2 = m.Business(owner_user_id=2, name="Shop Beta")
    session.add_all([biz1, biz2])
    session.commit()

    # Seed diverse log entries
    logs_to_add = [
        m.WaMessageLog(business_id=biz1.id, to_ref="****1", category="service", msg_type="text", status="sent", est_cost_kobo=1078, created_at=f"{today_str}T08:00:00"),
        m.WaMessageLog(business_id=biz1.id, to_ref="****2", category="utility", msg_type="template", status="delivered", est_cost_kobo=1078, created_at=f"{today_str}T09:00:00"),
        m.WaMessageLog(business_id=biz2.id, to_ref="****3", category="marketing", msg_type="template", status="sent", est_cost_kobo=7700, created_at=f"{today_str}T10:00:00"),
        m.WaMessageLog(business_id=biz2.id, to_ref="****4", category="service", msg_type="interactive", status="read", est_cost_kobo=1078, created_at=f"{today_str}T11:00:00"),
    ]
    session.add_all(logs_to_add)
    session.commit()

    expected_total_count = len(logs_to_add)
    expected_total_cost = sum(l.est_cost_kobo for l in logs_to_add)

    # Calculate via service
    report_data = costs.get_cost_report_data(session, target_date=now_date)
    today_res = report_data["today"]

    assert today_res["total_count"] == expected_total_count
    assert today_res["total_kobo"] == expected_total_cost
    assert today_res["by_category"]["service"]["cost_kobo"] == 1078 * 2
    assert today_res["by_category"]["utility"]["cost_kobo"] == 1078
    assert today_res["by_category"]["marketing"]["cost_kobo"] == 7700
    assert today_res["by_business"][biz1.id]["cost_kobo"] == 1078 * 2
    assert today_res["by_business"][biz2.id]["cost_kobo"] == 7700 + 1078

    # Formatted report text check
    report_text = costs.generate_cost_report_text(session, today=now_date)
    assert f"{expected_total_count} messages" in report_text
    assert f"{expected_total_cost:,} kobo" in report_text
    assert "Shop Alpha" in report_text
    assert "Shop Beta" in report_text


def test_cost_report_admin_command_via_router(db_session):
    """Spec: admin command 'cost report' showing messages sent and estimated cost today and this month."""
    session, _ = db_session
    now_date = datetime.now(m.get_tz()).date()
    today_str = now_date.isoformat()

    biz = m.Business(owner_user_id=1, name="My Store")
    session.add(biz)
    session.commit()

    session.add(m.WaMessageLog(business_id=biz.id, to_ref="****1", category="service", msg_type="text",
                               status="sent", est_cost_kobo=1078, created_at=f"{today_str}T12:00:00"))
    session.commit()

    replies = router.handle_text(session, biz.id, 1, "cost report", "msg_rep_1", today=now_date)
    assert len(replies) == 1
    assert "WhatsApp Message Cost Report" in replies[0].text
    assert "Today" in replies[0].text
    assert "This Month" in replies[0].text
    assert "1,078 kobo" in replies[0].text


def test_daily_budget_alert_when_threshold_exceeded(db_session, monkeypatch):
    """Spec: daily message budget with an admin alert when estimated cost passes DAILY_MESSAGE_COST_ALERT_KOBO."""
    session, _ = db_session
    now_date = datetime.now(m.get_tz()).date()
    today_str = now_date.isoformat()

    # Set threshold low for testing
    monkeypatch.setattr(settings, "daily_message_cost_alert_kobo", 2000)

    # 1. Cost is below threshold
    session.add(m.WaMessageLog(business_id=1, to_ref="****1", category="service", msg_type="text",
                               status="sent", est_cost_kobo=1078, created_at=f"{today_str}T10:00:00"))
    session.commit()

    current_cost, alert_triggered, thresh = costs.check_daily_budget(session, today=now_date)
    assert current_cost == 1078
    assert not alert_triggered

    # 2. Add second message -> cost = 2156 >= 2000 -> alert triggers!
    session.add(m.WaMessageLog(business_id=1, to_ref="****1", category="service", msg_type="text",
                               status="sent", est_cost_kobo=1078, created_at=f"{today_str}T11:00:00"))
    session.commit()

    current_cost, alert_triggered, thresh = costs.check_daily_budget(session, today=now_date)
    assert current_cost == 2156
    assert alert_triggered


def test_price_check_script(db_session):
    """Verifies scripts/price_check.py calculates tier stats cleanly."""
    session, _ = db_session
    b1 = m.Business(owner_user_id=1, name="Starter Shop", plan="starter")
    b2 = m.Business(owner_user_id=2, name="Trader Shop", plan="trader")
    session.add_all([b1, b2])
    session.commit()

    results = run_price_check(session=session)
    assert "starter" in results
    assert "trader" in results
    assert "business" in results
    assert results["starter"]["active_businesses"] == 1
    assert results["trader"]["active_businesses"] == 1
