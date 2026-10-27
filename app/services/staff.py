"""Staff, roles, permissions, and transaction approvals service.

Per Phase 10 of the Business Padi specification:
- One-time expiring invite codes for staff onboarding.
- Staff role permissions (cannot see profit or reports unless granted,
  cannot cancel or edit records without owner approval, cannot issue invoices unless allowed).
- Large transaction approval threshold producing pending transactions with
  Approve/Reject buttons to the owner.
- Audit logging for create, edit, cancel, approve, export.
- Business plan seat limit enforcement (up to 3 staff on Business plan).
- Removed staff lose access immediately.
"""

from datetime import datetime, timedelta, timezone
import logging
import secrets
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AuditLog, Business, Invite, Staff, Transaction, User, now_iso
from app.money import fmt_naira, to_kobo
from app.services import plans, stock

log = logging.getLogger(__name__)

# Default approval threshold for staff sales (100,000 NGN)
DEFAULT_APPROVAL_THRESHOLD_KOBO = to_kobo(100_000)


def log_audit(session: Session, business_id: int, actor_user_id: int, action: str, ref: str) -> AuditLog:
    """Record an action in the audit log."""
    entry = AuditLog(
        business_id=business_id,
        actor_user_id=actor_user_id,
        action=action,
        ref=ref,
        created_at=now_iso(),
    )
    session.add(entry)
    session.flush()
    return entry


def is_owner(session: Session, business_id: int, user_id: int) -> bool:
    """Check if the user is the owner of the business."""
    biz = session.get(Business, business_id)
    return biz is not None and biz.owner_user_id == user_id


def get_staff_record(session: Session, business_id: int, user_id: int) -> Staff | None:
    """Return active Staff record for the given user, or None."""
    return session.scalar(
        select(Staff).where(
            Staff.business_id == business_id,
            Staff.user_id == user_id,
            Staff.active == 1,
        )
    )


def check_permission(session: Session, business_id: int, user_id: int, permission: str) -> bool:
    """Check if a user has a specific permission in the business.

    Permissions:
    - 'view_profit' / 'view_reports': owner or staff with can_see_profit
    - 'issue_invoices': owner or staff with can_issue_invoices
    - 'edit_records' / 'cancel_records': owner only
    - 'approve_transactions': owner only
    """
    if is_owner(session, business_id, user_id):
        return True

    staff = get_staff_record(session, business_id, user_id)
    if staff is None:
        return False

    perm = permission.lower().strip()
    if perm in ("view_profit", "view_reports", "reports", "profit"):
        return bool(staff.can_see_profit)
    if perm in ("issue_invoices", "invoices", "invoice"):
        return bool(staff.can_issue_invoices)
    if perm in ("edit_records", "cancel_records", "approve_transactions"):
        return False  # only owner can approve or modify historical records

    return False


def create_invite(
    session: Session,
    business_id: int,
    actor_user_id: int,
    role: str = "staff",
    can_issue_invoices: bool = False,
    can_see_profit: bool = False,
    ttl_hours: int = 24,
) -> Invite:
    """Create a one-time expiring invite code. Enforces plan seat limits."""
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can create staff invites.")

    # Check plan seat limits (Part A: Business plan allows up to 3 staff)
    plan_def = plans.get_plan(biz.plan)
    max_seats = plan_def.staff_accounts
    if max_seats <= 0:
        raise ValueError(
            f"Staff accounts require the Business plan ({plan_def.name} plan has 0 staff seats). "
            "Please upgrade your plan to add staff."
        )

    active_staff_count = session.scalar(
        select(func.count())
        .select_from(Staff)
        .where(
            Staff.business_id == business_id,
            Staff.active == 1,
        )
    ) or 0

    if active_staff_count >= max_seats:
        raise ValueError(f"Seat limit of {max_seats} staff reached for your {plan_def.name} plan.")

    # Generate unique 6-character code
    for _ in range(10):
        code = f"JOIN-{secrets.token_hex(3).upper()}"
        if session.scalar(select(Invite).where(Invite.code == code)) is None:
            break

    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(hours=ttl_hours)).isoformat()

    inv = Invite(
        business_id=business_id,
        code=code,
        role=role,
        expires_at=expires_at,
        used_by_user_id=None,
    )
    session.add(inv)
    session.flush()

    log_audit(session, business_id, actor_user_id, "create_invite", f"Code {code} for role {role}")
    return inv


def join_business_with_code(session: Session, user_id: int, code: str) -> tuple[bool, str, Staff | None]:
    """Staff member joins by sending the invite code."""
    c_clean = code.strip().upper()
    if not c_clean.startswith("JOIN-"):
        c_clean = f"JOIN-{c_clean}"

    inv = session.scalar(select(Invite).where(Invite.code == c_clean))
    if inv is None:
        return False, "Invalid invite code. Please check with your shop owner.", None

    if inv.used_by_user_id is not None:
        return False, "This invite code has already been used.", None

    now_str = datetime.now(timezone.utc).isoformat()
    if inv.expires_at and inv.expires_at < now_str:
        return False, "This invite code has expired. Please ask for a new one.", None

    biz = session.get(Business, inv.business_id)
    if biz is None:
        return False, "Business not found.", None

    # Check if already a staff member
    existing_staff = session.scalar(
        select(Staff).where(
            Staff.business_id == biz.id,
            Staff.user_id == user_id,
        )
    )
    if existing_staff is not None:
        existing_staff.active = 1
        existing_staff.role = inv.role
        staff = existing_staff
    else:
        staff = Staff(
            business_id=biz.id,
            user_id=user_id,
            role=inv.role,
            can_issue_invoices=0,
            can_see_profit=0,
            active=1,
        )
        session.add(staff)

    inv.used_by_user_id = user_id
    session.flush()

    log_audit(session, biz.id, user_id, "join_staff", f"Joined via {c_clean}")
    session.commit()
    return True, f"Welcome! You have joined *{biz.name or 'the shop'}* as {inv.role}.", staff


def list_staff(session: Session, business_id: int) -> list[Staff]:
    """Return all active staff members for a business."""
    return list(session.scalars(
        select(Staff).where(
            Staff.business_id == business_id,
            Staff.active == 1,
        )
    ).all())


def remove_staff_member(session: Session, business_id: int, staff_user_id: int, actor_user_id: int) -> bool:
    """Owner revokes staff access immediately."""
    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the owner can remove staff members.")

    staff = session.scalar(
        select(Staff).where(
            Staff.business_id == business_id,
            Staff.user_id == staff_user_id,
            Staff.active == 1,
        )
    )
    if staff is None:
        return False

    staff.active = 0
    log_audit(session, business_id, actor_user_id, "remove_staff", f"user:{staff_user_id}")
    session.commit()
    return True


def add_staff_member(
    session: Session,
    business_id: int,
    actor_user_id: int,
    phone: str,
    name: str = "",
    role: str = "attendant",
    can_issue_invoices: bool = False,
    can_see_profit: bool = False,
) -> Staff:
    """Directly add or update a staff member for the business (enforces plan seat limits)."""
    biz = session.get(Business, business_id)
    if biz is None:
        raise ValueError(f"Business {business_id} not found")

    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can add staff members.")

    plan_def = plans.get_plan(biz.plan)
    max_seats = plan_def.staff_accounts
    if max_seats <= 0:
        raise ValueError(
            f"Staff accounts require the Business plan ({plan_def.name} plan has 0 staff seats). "
            "Please upgrade your plan to add staff."
        )

    phone_clean = phone.strip()
    if not phone_clean:
        raise ValueError("Phone number is required.")

    # Find or create user for this staff member
    user = session.scalar(
        select(User).where(User.channel == "whatsapp", User.channel_user_id == phone_clean)
    )
    if user is None:
        user = User(
            channel="whatsapp",
            channel_user_id=phone_clean,
            display_name=name.strip() or f"Attendant {phone_clean[-4:]}",
            state="DONE",
            consent_at=now_iso(),
        )
        session.add(user)
        session.flush()
    elif name.strip():
        user.display_name = name.strip()

    # Check active seats count
    existing_staff = session.scalar(
        select(Staff).where(
            Staff.business_id == business_id,
            Staff.user_id == user.id,
        )
    )

    if existing_staff is None or existing_staff.active == 0:
        active_staff_count = session.scalar(
            select(func.count())
            .select_from(Staff)
            .where(
                Staff.business_id == business_id,
                Staff.active == 1,
            )
        ) or 0
        if active_staff_count >= max_seats:
            raise ValueError(f"Seat limit of {max_seats} staff reached for your {plan_def.name} plan.")

    if existing_staff is None:
        st = Staff(
            business_id=business_id,
            user_id=user.id,
            role=role,
            can_issue_invoices=1 if can_issue_invoices else 0,
            can_see_profit=1 if can_see_profit else 0,
            active=1,
        )
        session.add(st)
        session.flush()
    else:
        st = existing_staff
        st.role = role
        st.can_issue_invoices = 1 if can_issue_invoices else 0
        st.can_see_profit = 1 if can_see_profit else 0
        st.active = 1
        session.flush()

    log_audit(session, business_id, actor_user_id, "add_staff", f"{user.display_name} ({phone_clean})")
    session.commit()
    return st


def get_approval_threshold_kobo(session: Session, business_id: int) -> int:
    """Retrieve the business's large transaction approval threshold."""
    biz = session.get(Business, business_id)
    custom = biz.custom_fields or {} if biz else {}
    return custom.get("approval_threshold_kobo", DEFAULT_APPROVAL_THRESHOLD_KOBO)


def needs_approval(session: Session, business_id: int, user_id: int, amount_kobo: int) -> bool:
    """Determine whether a transaction requires owner approval.

    Owner transactions never need approval.
    Staff transactions at or above the threshold require owner approval.
    """
    if is_owner(session, business_id, user_id):
        return False

    staff = get_staff_record(session, business_id, user_id)
    if staff is None:
        return False

    threshold = get_approval_threshold_kobo(session, business_id)
    return amount_kobo >= threshold


def approve_transaction(session: Session, business_id: int, transaction_id: int, actor_user_id: int) -> Transaction:
    """Owner approves a pending staff transaction."""
    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can approve transactions.")

    tx = session.get(Transaction, transaction_id)
    if tx is None or tx.business_id != business_id:
        raise ValueError(f"Transaction {transaction_id} not found")

    if tx.status != "pending":
        return tx

    tx.status = "confirmed"

    # Now that it's confirmed, process stock deduction if it was a sale
    if tx.kind == "sale" and tx.items_json:
        for line in tx.items_json:
            if line.get("name") and line.get("qty"):
                stock.record_sale(
                    session=session,
                    business_id=business_id,
                    item_name=line.get("name"),
                    qty=line.get("qty", 1.0),
                    unit=line.get("unit"),
                    transaction_id=tx.id,
                )

    log_audit(session, business_id, actor_user_id, "approve", f"tx:{transaction_id}")
    session.commit()
    return tx


def reject_transaction(session: Session, business_id: int, transaction_id: int, actor_user_id: int) -> Transaction:
    """Owner rejects a pending staff transaction."""
    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can reject transactions.")

    tx = session.get(Transaction, transaction_id)
    if tx is None or tx.business_id != business_id:
        raise ValueError(f"Transaction {transaction_id} not found")

    tx.status = "cancelled"
    log_audit(session, business_id, actor_user_id, "reject", f"tx:{transaction_id}")
    session.commit()
    return tx
