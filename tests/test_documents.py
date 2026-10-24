"""Tests for Phase 5: documents (receipts, invoices, statements), atomic numbering, and immutability."""

import concurrent.futures
from datetime import datetime, timezone
from pathlib import Path
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.models as m
from app.db import Base
from app.money import to_kobo
from app.pipeline import router
from app.pipeline.llm import MockLLM
from app.services import documents, ledger

RICE = ("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
        "She pay fifty thousand, the rest she go pay Friday.")


@pytest.fixture()
def engine(tmp_path):
    e = create_engine(f"sqlite:///{tmp_path / 'test_docs.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture()
def factory(engine):
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_business(s, name="Kudirat Provisions"):
    u = m.User(channel="mock", channel_user_id=f"user-{name}", state="DONE", consent_at=m.now_iso())
    s.add(u)
    s.flush()
    b = m.Business(owner_user_id=u.id, name=name, brand_color="#1A365D", phone="08012345678")
    s.add(b)
    s.flush()
    s.commit()
    return u, b


def press(s, b, u, reply, index=0):
    action, pid = router.parse_button(reply.buttons[index][0])
    return router.handle_button(s, b.id, u.id, action, pid, "en")


def test_atomic_number_uniqueness_under_concurrent_requests(factory):
    """Test that concurrent requests for document numbers never share a number."""
    with factory() as s:
        u, b = make_business(s, "Concurrent Store")
        biz_id = b.id

    def fetch_number(i):
        with factory() as session:
            return documents.get_next_document_number(session, biz_id, "invoice")

    # Run 20 concurrent threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        numbers = list(executor.map(fetch_number, range(20)))

    # All 20 numbers must be distinct
    assert len(numbers) == 20
    assert len(set(numbers)) == 20
    # Sequential from INV-0001 to INV-0020
    assert numbers[0] == "INV-0001"
    assert "INV-0020" in numbers


def extract_pdf_text(pdf_bytes: bytes) -> str:
    import io
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_totals_match_ledger_and_pdf_contains_amount(session, tmp_path, monkeypatch):
    """Test document creation matches ledger totals and PDF contains the amount and currency."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session)
    c = ledger.get_or_create_customer(session, b.id, "Mama Ngozi")

    # Issue an invoice
    items = [{"name": "Rice", "qty": 3, "unit": "bag", "unit_price_kobo": to_kobo(42_500), "line_total_kobo": to_kobo(127_500)}]
    doc = documents.issue_document(
        session=session,
        business_id=b.id,
        kind="invoice",
        customer_id=c.id,
        items=items,
        total_kobo=to_kobo(127_500),
        paid_kobo=to_kobo(50_000),
    )

    assert doc.number == "INV-0001"
    assert doc.total_kobo == to_kobo(127_500)
    assert doc.status == "issued"
    assert doc.sha256 is not None
    assert Path(doc.file_path).exists()

    # Read PDF content bytes and check that the amount 127,500 is in the decompressed text
    pdf_bytes = Path(doc.file_path).read_bytes()
    pdf_text = extract_pdf_text(pdf_bytes)
    assert "127,500" in pdf_text or "127500" in pdf_text or b"127,500" in pdf_bytes


def test_cancelled_document_keeps_its_number(session, tmp_path, monkeypatch):
    """Test document immutability: cancelled document retains its number,
    and subsequent document gets the next number."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session)

    doc1 = documents.issue_document(session, b.id, "receipt", total_kobo=to_kobo(10_000), paid_kobo=to_kobo(10_000))
    assert doc1.number == "R-0001"

    # Cancel doc1
    cancelled = documents.cancel_document(session, b.id, doc1.id)
    assert cancelled.status == "cancelled"
    assert cancelled.number == "R-0001"  # keeps number

    # Issue next receipt
    doc2 = documents.issue_document(session, b.id, "receipt", total_kobo=to_kobo(15_000), paid_kobo=to_kobo(15_000))
    assert doc2.number == "R-0002"  # next sequential number
    assert doc2.status == "issued"


def test_acceptance_send_receipt_for_that_sale(session, tmp_path, monkeypatch):
    """Acceptance: 'Send receipt for that sale' returns a PDF with the correct total."""
    monkeypatch.setattr(documents, "MEDIA_DIR", tmp_path / "docs")
    u, b = make_business(session)

    # 1. Record a sale first
    r1 = router.handle_text(session, b.id, u.id, RICE, "m-s1", "en", llm=MockLLM())[0]
    press(session, b, u, r1)
    tx = ledger.get_last_transaction(session, b.id)
    assert tx is not None
    assert tx.total_kobo == to_kobo(127_500)

    # 2. Trader says: "Send receipt for that sale"
    r2 = router.handle_text(session, b.id, u.id, "Send receipt for that sale", "m-r1", "en", llm=MockLLM())
    assert len(r2) == 1
    assert r2[0].kind == "confirm"
    assert "Receipt" in r2[0].text
    assert "N127,500" in r2[0].text

    # 3. Press Issue
    out = press(session, b, u, r2[0])
    assert len(out) == 1
    assert "issued" in out[0].text.lower()
    assert "N127,500" in out[0].text
    assert "wa.me" in out[0].text  # forwarding link present

    # Verify document in database
    doc = session.scalar(select(m.Document).where(m.Document.business_id == b.id))
    assert doc is not None
    assert doc.total_kobo == to_kobo(127_500)
    assert doc.number == "R-0001"
    assert Path(doc.file_path).exists()
