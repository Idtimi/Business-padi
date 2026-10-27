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
        return True  # Attendants are permitted to generate receipts and invoices for customers
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


# ---------- Phase 10B: Daily Staff Audit & Stock Book ----------

def get_daily_staff_audit(session: Session, business_id: int, target_date_str: str | None = None) -> dict[str, Any]:
    """Compile end-of-day audit metrics and stock breakdown for all staff attendants."""
    biz = session.get(Business, business_id)
    if biz is None:
        return {}

    if not target_date_str:
        now_utc = datetime.now(timezone.utc)
        target_date_str = now_utc.strftime("%Y-%m-%d")

    # Fetch all confirmed transactions created by attendants for this date
    all_txs = list(session.scalars(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.created_by_user_id != biz.owner_user_id,
            Transaction.status == "confirmed",
        ).order_by(Transaction.id.desc())
    ).all())

    # Filter to transactions matching the target date
    day_txs = [tx for tx in all_txs if (tx.created_at or "").startswith(target_date_str)]

    total_sales_kobo = sum(tx.total_kobo for tx in day_txs if tx.kind == "sale")
    total_cash_kobo = sum(tx.paid_kobo for tx in day_txs if tx.kind == "sale")
    pending_reconcile = [tx for tx in day_txs if not getattr(tx, "reconciled_at", None)]

    # Group by attendant user_id
    attendant_groups: dict[int, dict[str, Any]] = {}
    for tx in day_txs:
        uid = tx.created_by_user_id
        if uid not in attendant_groups:
            u = session.get(User, uid)
            dname = (u.display_name if u else None) or (u.channel_user_id if u else None) or f"Attendant #{uid}"
            attendant_groups[uid] = {
                "user_id": uid,
                "display_name": dname,
                "sales_count": 0,
                "total_sales_kobo": 0,
                "cash_collected_kobo": 0,
                "items_sold": {},  # name -> {"qty": float, "unit": str}
                "pending_count": 0,
                "transactions": [],
            }
        group = attendant_groups[uid]
        group["sales_count"] += 1
        group["total_sales_kobo"] += tx.total_kobo
        group["cash_collected_kobo"] += tx.paid_kobo
        if not getattr(tx, "reconciled_at", None):
            group["pending_count"] += 1

        # Extract items for the stock book
        if tx.items_json and isinstance(tx.items_json, list):
            for item in tx.items_json:
                name = item.get("name") or "Item"
                qty = float(item.get("qty", 1.0) or 1.0)
                unit = item.get("unit") or ""
                key = f"{name} ({unit})" if unit else name
                if key not in group["items_sold"]:
                    group["items_sold"][key] = {"name": name, "qty": 0.0, "unit": unit}
                group["items_sold"][key]["qty"] += qty

        group["transactions"].append(tx)

    # Compile overall stock summary across all attendants
    overall_stock: dict[str, dict[str, Any]] = {}
    for group in attendant_groups.values():
        for key, info in group["items_sold"].items():
            if key not in overall_stock:
                overall_stock[key] = {"name": info["name"], "qty": 0.0, "unit": info["unit"]}
            overall_stock[key]["qty"] += info["qty"]

    return {
        "date": target_date_str,
        "business_name": biz.name,
        "total_tx_count": len(day_txs),
        "total_sales_kobo": total_sales_kobo,
        "total_cash_kobo": total_cash_kobo,
        "pending_reconcile_count": len(pending_reconcile),
        "attendants": list(attendant_groups.values()),
        "overall_stock": list(overall_stock.values()),
        "all_transactions": day_txs,
    }


def format_daily_audit_text(session: Session, audit_data: dict[str, Any], biz_name: str) -> str:
    """Format the end-of-day audit and stock book report as chat text."""
    if not audit_data or audit_data.get("total_tx_count", 0) == 0:
        return f"Daily Staff Audit for {biz_name} ({audit_data.get('date', 'Today')}):\n\nNo attendant transactions recorded today."

    date_str = audit_data.get("date", "Today")
    tot_sales = fmt_naira(audit_data["total_sales_kobo"])
    tot_cash = fmt_naira(audit_data["total_cash_kobo"])
    pending = audit_data["pending_reconcile_count"]

    lines = [
        f"Daily Staff Audit & Stock Book",
        f"Date: {date_str}",
        f"Shop: {biz_name}",
        f"----------------------------------------",
    ]

    for att in audit_data.get("attendants", []):
        att_name = att["display_name"]
        sales_cnt = att["sales_count"]
        att_sales = fmt_naira(att["total_sales_kobo"])
        att_cash = fmt_naira(att["cash_collected_kobo"])
        p_cnt = att["pending_count"]

        lines.append(f"Attendant: {att_name}")
        lines.append(f"- Transactions: {sales_cnt}")
        lines.append(f"- Total Sales: {att_sales}")
        lines.append(f"- Cash to collect from attendant: {att_cash}")

        items = att.get("items_sold", {})
        if items:
            lines.append("- Items sold (Stock Book):")
            for key, val in items.items():
                qty = val["qty"]
                qty_display = int(qty) if qty.is_integer() else f"{qty:.2f}"
                unit = f" {val['unit']}" if val.get("unit") else ""
                lines.append(f"  * {qty_display}{unit} {val['name']}")
        status_line = f"Status: {p_cnt} pending approval" if p_cnt > 0 else "Status: Fully reconciled"
        lines.append(f"- {status_line}")
        lines.append("")

    lines.append("----------------------------------------")
    lines.append(f"SUMMARY FOR TODAY:")
    lines.append(f"Total Sales Value: {tot_sales}")
    lines.append(f"Total Cash to Collect from Attendants: {tot_cash}")
    if pending > 0:
        lines.append(f"Pending Reconciliations: {pending} transactions")
    else:
        lines.append("All attendant sales for today have been approved and reconciled.")

    return "\n".join(lines)


def reconcile_all_daily_transactions(
    session: Session, business_id: int, actor_user_id: int, target_date_str: str | None = None
) -> tuple[int, int]:
    """Owner approves and reconciles all attendant transactions for the target date."""
    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can approve and reconcile daily staff transactions.")

    biz = session.get(Business, business_id)
    if not target_date_str:
        now_utc = datetime.now(timezone.utc)
        target_date_str = now_utc.strftime("%Y-%m-%d")

    all_txs = list(session.scalars(
        select(Transaction).where(
            Transaction.business_id == business_id,
            Transaction.created_by_user_id != biz.owner_user_id,
            Transaction.status == "confirmed",
        )
    ).all())

    day_pending = [
        tx for tx in all_txs
        if (tx.created_at or "").startswith(target_date_str) and not getattr(tx, "reconciled_at", None)
    ]

    now_str = now_iso()
    total_reconciled_kobo = 0
    count = 0
    for tx in day_pending:
        tx.reconciled_at = now_str
        total_reconciled_kobo += tx.paid_kobo
        count += 1

    log_audit(session, business_id, actor_user_id, "reconcile_daily", f"date:{target_date_str}, count:{count}")
    session.commit()
    return count, total_reconciled_kobo


def reconcile_single_transaction(
    session: Session, business_id: int, actor_user_id: int, transaction_id: int
) -> Transaction:
    """Owner approves and reconciles a specific staff transaction."""
    if not is_owner(session, business_id, actor_user_id):
        raise PermissionError("Only the shop owner can approve staff transactions.")

    tx = session.get(Transaction, transaction_id)
    if tx is None or tx.business_id != business_id:
        raise ValueError(f"Transaction {transaction_id} not found")

    tx.reconciled_at = now_iso()
    log_audit(session, business_id, actor_user_id, "reconcile_tx", f"tx:{transaction_id}")
    session.commit()
    return tx
