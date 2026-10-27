"""Tests for Phase 4: onboarding state machine, branding, logo resizing, and settings."""

import asyncio
from io import BytesIO
from pathlib import Path
from PIL import Image
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

import app.models as m
from app.channels.base import IncomingMessage
from app.channels.mock import MockChannel
from app.db import Base
from app.pipeline.handler import handle_incoming
from app.services import onboarding


@pytest.fixture()
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test_onboarding.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_msg(user_ref: str, text: str | None = None, button_id: str | None = None,
             kind: str = "text", media_ref: str | None = None, mid: str = "m1"):
    from datetime import datetime, timezone
    return IncomingMessage(
        channel="whatsapp",
        user_ref=user_ref,
        message_id=mid,
        kind=kind,
        text=text,
        media_ref=media_ref,
        button_id=button_id,
        timestamp=datetime.now(timezone.utc),
    )


def test_logo_resized_with_pillow(tmp_path, monkeypatch):
    """Test logo resize to max (512, 512)."""
    monkeypatch.setattr(onboarding, "MEDIA_DIR", tmp_path / "logos")

    # Create a 1200x800 test image in memory
    buf = BytesIO()
    with Image.new("RGB", (1200, 800), color="blue") as img:
        img.save(buf, format="PNG")
    data = buf.getvalue()

    saved_path = onboarding.save_and_resize_logo(data, business_id=42, max_size=(512, 512))
    assert Path(saved_path).exists()

    with Image.open(saved_path) as res:
        w, h = res.size
        assert w <= 512 and h <= 512
        assert w == 512
        assert h == round(800 * (512 / 1200))


def test_full_onboarding_flow_with_skip_logo(factory):
    """Test fresh user stepping through the full onboarding flow with logo skipped."""
    channel = MockChannel()
    user_ref = "2348099990001"

    # Step 1: First message (NEW -> CHOOSE_LANGUAGE)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="Hello", mid="step1"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        assert u.state == "CHOOSE_LANGUAGE"
        assert len(channel.sent) == 1
        assert "language" in channel.sent[0]["text"].lower()

    # Step 2: Choose English (CHOOSE_LANGUAGE -> CONSENT)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, button_id="lang:en", kind="button", mid="step2"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        assert u.language == "en"
        assert u.state == "CONSENT"
        assert "agree" in channel.sent[-1]["text"].lower()

    # Step 3: Consent (CONSENT -> BUSINESS_NAME)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, button_id="consent:yes", kind="button", mid="step3"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        assert u.consent_at is not None  # consent stored with timestamp
        assert u.state == "BUSINESS_NAME"
        assert "shop name" in channel.sent[-1]["text"].lower()

    # Step 4: Shop Name (BUSINESS_NAME -> LOGO)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="Mama K Supermarket", mid="step4"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        b = s.scalar(select(m.Business).where(m.Business.owner_user_id == u.id))
        assert b is not None
        assert b.name == "Mama K Supermarket"
        assert u.state == "LOGO"
        assert "logo" in channel.sent[-1]["text"].lower()

    # Step 5: Skip Logo (LOGO -> BRAND_COLOR)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, button_id="logo:skip", kind="button", mid="step5"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        assert u.state == "BRAND_COLOR"
        assert "colour" in channel.sent[-1]["text"].lower() or "color" in channel.sent[-1]["text"].lower()

    # Step 6: Pick Color (BRAND_COLOR -> CONTACT)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, button_id="color:green", kind="button", mid="step6"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        b = s.scalar(select(m.Business).where(m.Business.owner_user_id == u.id))
        assert b.brand_color in ("green", "#16A34A")
        assert u.state == "CONTACT"
        assert "address" in channel.sent[-1]["text"].lower()

    # Step 7: Contact / Address (CONTACT -> PRODUCTS)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="12 Balogun Street, Lagos. 08012345678", mid="step7"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        b = s.scalar(select(m.Business).where(m.Business.owner_user_id == u.id))
        assert u.state == "PRODUCTS"
        assert b.address == "12 Balogun Street, Lagos. 08012345678"
        assert "products" in channel.sent[-1]["text"].lower() or "items" in channel.sent[-1]["text"].lower()

    # Step 8: Products Catalog (PRODUCTS -> DONE)
    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="Rice, Cement, Indomie", mid="step8"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        b = s.scalar(select(m.Business).where(m.Business.owner_user_id == u.id))
        assert u.state == "DONE"
        assert "all set" in channel.sent[-1]["text"].lower()
        items = s.scalars(select(m.StockItem).where(m.StockItem.business_id == b.id)).all()
        assert len(items) == 3


def test_ledger_blocked_before_consent(factory):
    """Test that a transaction message is blocked from writing to ledger before consent."""
    channel = MockChannel()
    user_ref = "2348011112222"
    tx_text = "I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each."

    with factory() as s:
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text=tx_text, mid="t1"), s))
        u = s.scalar(select(m.User).where(m.User.channel_user_id == user_ref))
        # User is placed in onboarding, NOT permitted to create a transaction
        assert u.state != "DONE"
        assert u.consent_at is None
        # 0 transactions created
        assert s.scalar(select(func.count()).select_from(m.Transaction)) == 0
        assert s.scalar(select(func.count()).select_from(m.PendingAction)) == 0


def test_help_menu_in_chosen_language(factory):
    """Test 'help' returns the help menu in the user's language."""
    channel = MockChannel()
    user_ref_en = "2348010000001"
    user_ref_pcm = "2348010000002"

    with factory() as s:
        s.add(m.User(channel="whatsapp", channel_user_id=user_ref_en, language="en", state="DONE"))
        s.add(m.User(channel="whatsapp", channel_user_id=user_ref_pcm, language="pcm", state="DONE"))
        s.commit()

        # English user asks help
        asyncio.run(handle_incoming(channel, make_msg(user_ref_en, text="help", mid="h1"), s))
        assert "Business Padi Help:" in channel.sent[-1]["text"]

        # Pidgin user asks help
        asyncio.run(handle_incoming(channel, make_msg(user_ref_pcm, text="help", mid="h2"), s))
        assert "Business Padi Help:" in channel.sent[-1]["text"]
        assert "You fit talk voice note" in channel.sent[-1]["text"]


def test_settings_command_flow(factory):
    """Test /settings view and field updates."""
    channel = MockChannel()
    user_ref = "2348033334444"

    with factory() as s:
        u = m.User(channel="whatsapp", channel_user_id=user_ref, language="en", state="DONE", consent_at=m.now_iso())
        s.add(u)
        s.flush()
        b = m.Business(owner_user_id=u.id, name="Initial Shop", brand_color="blue")
        s.add(b)
        s.commit()

        # View settings
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="/settings", mid="st1"), s))
        assert "Shop Settings:" in channel.sent[-1]["text"]
        assert "Initial Shop" in channel.sent[-1]["text"]

        # Update name
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="/settings name New Glory Ventures", mid="st2"), s))
        assert "Shop name updated to New Glory Ventures" in channel.sent[-1]["text"]
        s.refresh(b)
        assert b.name == "New Glory Ventures"

        # Update color
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="/settings color #FF5733", mid="st3"), s))
        s.refresh(b)
        assert b.brand_color == "#FF5733"

        # Update bank details
        asyncio.run(handle_incoming(channel, make_msg(user_ref, text="/settings bank GTBank 0123456789", mid="st4"), s))
        s.refresh(b)
        assert b.bank_details == "GTBank 0123456789"
