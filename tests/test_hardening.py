"""Tests for Phase 14: Hardening, deployment, resilience, and demo mode."""

import asyncio
import logging
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal, get_session
from app.logging_conf import MaskingJsonFormatter
from app.main import app
from app.resilience import RateLimiter, call_with_retry
from scripts.backup import prune_old_backups, run_backup
from scripts.seed_demo import seed_demo_data


@pytest.fixture
def client():
    return TestClient(app)


def test_health_and_ready_endpoints(client):
    """Verify /health and /ready endpoints."""
    # /health
    res_health = client.get("/health")
    assert res_health.status_code == 200
    assert res_health.json()["status"] == "ok"

    # /ready
    res_ready = client.get("/ready")
    assert res_ready.status_code == 200
    assert res_ready.json() == {"status": "ready", "database": "connected"}


def test_ready_endpoint_db_failure(client):
    """Verify /ready returns 503 when the database check fails."""
    with patch("app.main.SessionLocal") as mock_session:
        mock_ctx = mock_session.return_value.__enter__.return_value
        mock_ctx.execute.side_effect = Exception("DB Connection Down")
        res = client.get("/ready")
        assert res.status_code == 503
        data = res.json()
        assert data["status"] == "unhealthy"
        assert "error" in data


def test_global_500_error_handler():
    """Verify unhandled exceptions return a friendly error response."""
    test_app = FastAPI()
    from app.main import global_exception_handler
    test_app.add_exception_handler(Exception, global_exception_handler)

    @test_app.get("/trigger-error")
    def trigger():
        raise RuntimeError("Unexpected internal crash")

    c = TestClient(test_app, raise_server_exceptions=False)
    res = c.get("/trigger-error")
    assert res.status_code == 500
    body = res.json()
    assert "error" in body
    assert "something went wrong" in body.get("message", "").lower()


def test_phone_masking_json_formatter():
    """Verify phone numbers are masked in structured log output."""
    formatter = MaskingJsonFormatter()
    record = logging.LogRecord(
        name="test_logger",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Processing transaction for customer +2348012345678 with phone 08098765432",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert "+2348012345678" not in formatted
    assert "08098765432" not in formatted
    assert "5678" in formatted
    assert '"level": "INFO"' in formatted


def test_rate_limiter():
    """Verify sliding-window rate limiting logic."""
    limiter = RateLimiter(default_limit=3, window_seconds=2)
    user_key = "user_test_123"

    # First 3 requests allowed
    ok1, _ = limiter.is_allowed(user_key)
    ok2, _ = limiter.is_allowed(user_key)
    ok3, _ = limiter.is_allowed(user_key)
    assert ok1 is True
    assert ok2 is True
    assert ok3 is True

    # 4th request blocked
    ok4, remaining = limiter.is_allowed(user_key)
    assert ok4 is False
    assert remaining == 0

    # Different key is still allowed
    ok_other, _ = limiter.is_allowed("user_other_456")
    assert ok_other is True

    # Wait for window to expire
    time.sleep(2.1)
    ok_retry, _ = limiter.is_allowed(user_key)
    assert ok_retry is True


def test_retry_with_backoff_success():
    """Verify call_with_retry succeeds when call passes."""
    mock_func = AsyncMock(return_value={"result": "ok"})
    res = asyncio.run(call_with_retry(mock_func, max_retries=3, base_delay=0.01))
    assert res == {"result": "ok"}
    assert mock_func.call_count == 1


def test_retry_with_backoff_eventual_success():
    """Verify call_with_retry retries transient errors before succeeding."""
    mock_func = AsyncMock(side_effect=[ConnectionError("Transient fail"), {"result": "recovered"}])
    res = asyncio.run(call_with_retry(mock_func, max_retries=3, base_delay=0.01))
    assert res == {"result": "recovered"}
    assert mock_func.call_count == 2


def test_retry_with_backoff_exhaustion():
    """Verify call_with_retry raises exception after max retries."""
    mock_func = AsyncMock(side_effect=ConnectionError("Persistent outage"))
    with pytest.raises(ConnectionError, match="Persistent outage"):
        asyncio.run(call_with_retry(mock_func, max_retries=2, base_delay=0.01))
    assert mock_func.call_count == 2


def test_backup_and_pruning():
    """Verify SQLite backup creation and retention pruning."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        db_file = tmp_path / "test.db"
        # Create dummy sqlite db
        con = sqlite3.connect(str(db_file))
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        con.execute("INSERT INTO t (v) VALUES ('hello')")
        con.commit()
        con.close()

        bdir = tmp_path / "backups"
        bdir.mkdir()

        # Create 5 artificial older backups
        for i in range(5):
            f = bdir / f"ledger_backup_2026010{i}_120000.db"
            f.write_text("old")

        with patch("app.config.settings.database_url", f"sqlite:///{db_file}"):
            dest = run_backup(keep=3, backup_dir=str(bdir))
            assert dest.exists()

            # Should retain only the 3 newest backups
            remaining = list(bdir.glob("ledger_backup_*.db"))
            assert len(remaining) == 3


def test_seed_demo_and_offline_mode():
    """Verify seed_demo populates data and demo_mode operates offline."""
    seed_demo_data(reset=True)
    db = SessionLocal()
    try:
        from app import models as m
        biz = db.query(m.Business).filter_by(name="Mama Ngozi Provisions").first()
        assert biz is not None
        assert biz.plan == "trader"

        customers = db.query(m.Customer).filter_by(business_id=biz.id).all()
        assert len(customers) == 10

        stock = db.query(m.StockItem).filter_by(business_id=biz.id).all()
        assert len(stock) == 5

        txs = db.query(m.Transaction).filter_by(business_id=biz.id).all()
        assert len(txs) >= 20

        docs = db.query(m.Document).filter_by(business_id=biz.id).all()
        assert len(docs) >= 5
    finally:
        db.close()


def test_demo_mode_channel_bypass():
    """Verify WhatsAppChannel sends mock responses offline when DEMO_MODE=True."""
    from app.channels.whatsapp import WhatsAppChannel
    with patch("app.config.settings.demo_mode", True):
        channel = WhatsAppChannel(token="fake", phone_number_id="123")
        res = asyncio.run(channel.send_text("2348011223344", "Hello offline world"))
        assert "messages" in res
        assert res["messages"][0]["id"].startswith("wamid.demo_")
