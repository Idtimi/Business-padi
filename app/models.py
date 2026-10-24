from datetime import datetime, timezone

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def now_iso() -> str:
    """Current UTC time as text, e.g. 2026-10-23T12:00:00+00:00."""
    return datetime.now(timezone.utc).isoformat()


def get_tz():
    from app.config import settings
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.tz)
    except Exception:
        from datetime import timezone, timedelta
        return timezone(timedelta(hours=1))



# ---------- identity ----------
class User(Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("channel", "channel_user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String)
    channel_user_id: Mapped[str] = mapped_column(String)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="pcm")
    state: Mapped[str] = mapped_column(String, default="NEW")
    state_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    consent_at: Mapped[str | None] = mapped_column(String, nullable=True)
    last_user_message_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Business(Base):
    __tablename__ = "businesses"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    logo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    brand_color: Mapped[str | None] = mapped_column(String, nullable=True)
    address: Mapped[str | None] = mapped_column(String, nullable=True)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    bank_details: Mapped[str | None] = mapped_column(String, nullable=True)
    footer_note: Mapped[str | None] = mapped_column(String, nullable=True)
    custom_fields: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    plan: Mapped[str] = mapped_column(String, default="starter")
    plan_renews_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Staff(Base):
    __tablename__ = "staff"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String)  # owner | staff
    can_issue_invoices: Mapped[int] = mapped_column(Integer, default=0)
    can_see_profit: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[int] = mapped_column(Integer, default=1)


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    code: Mapped[str] = mapped_column(String, unique=True)
    role: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)
    used_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------- ledger ----------
class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (UniqueConstraint("business_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    name: Mapped[str] = mapped_column(String)
    phone: Mapped[str | None] = mapped_column(String, nullable=True)
    opted_out: Mapped[int] = mapped_column(Integer, default=0)
    opted_in: Mapped[int] = mapped_column(Integer, default=0)


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (UniqueConstraint("business_id", "source_msg_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    supplier: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)  # sale|expense|payment_received|restock
    items_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    total_kobo: Mapped[int] = mapped_column(Integer)
    paid_kobo: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[str | None] = mapped_column(String, nullable=True)
    category: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="confirmed")  # pending|confirmed|cancelled
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    source_msg_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    amount_kobo: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)
    source_msg_id: Mapped[str | None] = mapped_column(String, nullable=True)


class PendingAction(Base):
    __tablename__ = "pending_actions"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="open")  # open|applied|cancelled|expired
    expires_at: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


# ---------- documents ----------
class DocCounter(Base):
    __tablename__ = "doc_counters"
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    last_no: Mapped[int] = mapped_column(Integer, default=0)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("business_id", "number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    kind: Mapped[str] = mapped_column(String)  # invoice|receipt|quote|statement|report
    number: Mapped[str] = mapped_column(String)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    customer_id: Mapped[int | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    total_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="issued")  # issued|cancelled
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


# ---------- reminders ----------
class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"))
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"))
    send_at: Mapped[str] = mapped_column(String)
    tone: Mapped[str] = mapped_column(String)  # gentle|firm|final
    status: Mapped[str] = mapped_column(String, default="scheduled")
    sent_at: Mapped[str | None] = mapped_column(String, nullable=True)


# ---------- stock ----------
class StockItem(Base):
    __tablename__ = "stock_items"
    __table_args__ = (UniqueConstraint("business_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    name: Mapped[str] = mapped_column(String)
    base_unit: Mapped[str | None] = mapped_column(String, nullable=True)
    qty: Mapped[float] = mapped_column(Float, default=0)
    avg_cost_kobo: Mapped[int] = mapped_column(Integer, default=0)
    low_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class ItemAlias(Base):
    __tablename__ = "item_aliases"
    __table_args__ = (UniqueConstraint("business_id", "alias"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"))
    alias: Mapped[str] = mapped_column(String)


class UnitConversion(Base):
    __tablename__ = "unit_conversions"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"))
    unit: Mapped[str] = mapped_column(String)
    to_base: Mapped[float] = mapped_column(Float)


class StockMove(Base):
    __tablename__ = "stock_moves"
    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("stock_items.id"))
    qty: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String)  # sale|restock|adjust
    unit_cost_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier: Mapped[str | None] = mapped_column(String, nullable=True)
    transaction_id: Mapped[int | None] = mapped_column(ForeignKey("transactions.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


# ---------- plans and usage ----------
class UsageCounter(Base):
    __tablename__ = "usage_counters"
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"), primary_key=True)
    month: Mapped[str] = mapped_column(String, primary_key=True)
    voice_entries: Mapped[int] = mapped_column(Integer, default=0)
    extra_entries: Mapped[int] = mapped_column(Integer, default=0)


class UsageLog(Base):
    __tablename__ = "usage_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    kind: Mapped[str] = mapped_column(String)  # asr|llm|tts|doc
    units: Mapped[float] = mapped_column(Float)
    cost_estimate_kobo: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


# ---------- consent and sharing ----------
class Consent(Base):
    __tablename__ = "consents"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    partner: Mapped[str] = mapped_column(String)
    scope: Mapped[str] = mapped_column(String)
    granted_at: Mapped[str] = mapped_column(String, default=now_iso)
    expires_at: Mapped[str] = mapped_column(String)
    revoked_at: Mapped[str | None] = mapped_column(String, nullable=True)


class ShareLink(Base):
    __tablename__ = "share_links"
    id: Mapped[int] = mapped_column(primary_key=True)
    consent_id: Mapped[int] = mapped_column(ForeignKey("consents.id"))
    token_hash: Mapped[str] = mapped_column(String)
    expires_at: Mapped[str] = mapped_column(String)


# ---------- trust and analytics ----------
class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int] = mapped_column(ForeignKey("businesses.id"))
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String)
    ref: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String)
    props: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"
    channel: Mapped[str] = mapped_column(String, primary_key=True)
    message_id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)


class WaMessageLog(Base):
    __tablename__ = "wa_message_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    business_id: Mapped[int | None] = mapped_column(ForeignKey("businesses.id"), nullable=True)
    to_ref: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)  # service|utility|marketing|auth
    msg_type: Mapped[str] = mapped_column(String)
    template_name: Mapped[str | None] = mapped_column(String, nullable=True)
    wa_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="sent")
    est_cost_kobo: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[str] = mapped_column(String, default=now_iso)