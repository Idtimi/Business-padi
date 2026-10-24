"""Web dashboard routes mounted under /d."""

import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.plans import PLANS
from app.db import SessionLocal
from app.models import (
    Business,
    Consent,
    Customer,
    Document,
    Invite,
    Payment,
    Staff,
    StockItem,
    Transaction,
    User,
    get_tz,
)
from app.money import fmt_naira
from app.services import dashboard, documents, ledger, plans, reports

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["fmt_naira"] = fmt_naira
templates.env.filters["fmt_date"] = lambda d: d[:10] if d else ""


def get_db():
    with SessionLocal() as s:
        yield s


def get_current_user_and_biz(
    request: Request,
    db: Session = Depends(get_db),
) -> tuple[User, Business]:
    """Dependency enforcing dashboard session authentication and business scoping."""
    cookie_val = request.cookies.get(dashboard.SESSION_COOKIE_NAME)
    res = dashboard.verify_session_cookie(cookie_val)
    if res is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard session invalid or expired. Please request a new login link via WhatsApp.",
        )
    user_id, biz_id = res
    user = db.get(User, user_id)
    biz = db.get(Business, biz_id)
    if user is None or biz is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User or business not found.",
        )
    return user, biz


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, token: str | None = None, db: Session = Depends(get_db)):
    if token:
        ok, msg, action = dashboard.verify_magic_token(db, token)
        if not ok or action is None:
            return templates.TemplateResponse(
                request,
                "error.html",
                {"title": "Login Failed", "message": msg},
                status_code=401,
            )
        # Create session cookie and redirect to overview
        cookie = dashboard.create_session_cookie(action.user_id, action.business_id)
        resp = RedirectResponse(url="/d", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            key=dashboard.SESSION_COOKIE_NAME,
            value=cookie,
            httponly=True,
            path="/d",
            samesite="lax",
        )
        return resp
    return templates.TemplateResponse(request, "login.html", {})


@router.get("/logout")
def logout(response: Response):
    resp = RedirectResponse(url="/d/login", status_code=status.HTTP_303_SEE_OTHER)
    resp.delete_cookie(key=dashboard.SESSION_COOKIE_NAME, path="/d")
    return resp


# ---------------------------------------------------------------------------
# Dashboard pages
# ---------------------------------------------------------------------------

@router.get("", response_class=HTMLResponse)
@router.get("/overview", response_class=HTMLResponse)
def overview(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    metrics = dashboard.get_overview_metrics(db, biz.id)

    # Fetch 10 most recent transactions
    recent_txs_raw = db.scalars(
        select(Transaction)
        .where(Transaction.business_id == biz.id)
        .order_by(Transaction.id.desc())
        .limit(10)
    ).all()

    recent_txs = []
    for tx in recent_txs_raw:
        cust_name = ""
        if tx.customer_id:
            c = db.get(Customer, tx.customer_id)
            if c:
                cust_name = c.name
        recent_txs.append({
            "id": tx.id,
            "created_at": tx.created_at,
            "kind": tx.kind,
            "customer_name": cust_name,
            "total_kobo": tx.total_kobo,
            "paid_kobo": tx.paid_kobo,
            "status": tx.status,
        })

    return templates.TemplateResponse(
        request,
        "overview.html",
        {
            "user": user,
            "business": biz,
            "metrics": metrics,
            "recent_txs": recent_txs,
            "active_page": "overview",
        },
    )


@router.get("/ledger", response_class=HTMLResponse)
def ledger_page(
    request: Request,
    kind: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
    q: str | None = None,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    stmt = select(Transaction).where(Transaction.business_id == biz.id)

    if kind:
        stmt = stmt.where(Transaction.kind == kind)
    if status_filter:
        stmt = stmt.where(Transaction.status == status_filter)

    txs_raw = db.scalars(stmt.order_by(Transaction.id.desc())).all()

    # Apply search filter in memory for customer name / items if specified
    txs = []
    for tx in txs_raw:
        cust_name = ""
        if tx.customer_id:
            c = db.get(Customer, tx.customer_id)
            if c:
                cust_name = c.name

        items_str = ""
        if tx.items_json and isinstance(tx.items_json, list):
            items_str = ", ".join(f"{i.get('item', '')} x{i.get('qty', 1)}" for i in tx.items_json)
        elif tx.transcript:
            items_str = tx.transcript

        if q:
            q_lower = q.lower()
            if q_lower not in cust_name.lower() and q_lower not in items_str.lower():
                continue

        txs.append({
            "id": tx.id,
            "created_at": tx.created_at,
            "kind": tx.kind,
            "customer_id": tx.customer_id,
            "customer_name": cust_name,
            "items_summary": items_str,
            "total_kobo": tx.total_kobo,
            "paid_kobo": tx.paid_kobo,
            "due_date": tx.due_date,
            "status": tx.status,
        })

    return templates.TemplateResponse(
        request,
        "ledger.html",
        {
            "user": user,
            "business": biz,
            "transactions": txs,
            "current_kind": kind,
            "current_status": status_filter,
            "current_q": q,
            "active_page": "ledger",
        },
    )


@router.get("/ledger/export.csv")
def export_ledger_csv(
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    csv_content = dashboard.generate_ledger_csv(db, biz.id)
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=ledger_{biz.id}.csv"},
    )


@router.get("/customers", response_class=HTMLResponse)
def customers_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    customers_raw = db.scalars(
        select(Customer).where(Customer.business_id == biz.id).order_by(Customer.name.asc())
    ).all()

    cust_list = []
    for c in customers_raw:
        bal = ledger.customer_balance_kobo(db, biz.id, c.id)
        cust_list.append({
            "id": c.id,
            "name": c.name,
            "phone": c.phone,
            "balance_kobo": bal,
            "opted_in": bool(c.opted_in),
            "opted_out": bool(c.opted_out),
        })

    return templates.TemplateResponse(
        request,
        "customers.html",
        {
            "user": user,
            "business": biz,
            "customers": cust_list,
            "active_page": "customers",
        },
    )


@router.get("/customers/{customer_id}", response_class=HTMLResponse)
def customer_detail_page(
    request: Request,
    customer_id: int,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    cust = db.get(Customer, customer_id)
    if cust is None or cust.business_id != biz.id:
        raise HTTPException(status_code=404, detail="Customer not found.")

    bal = ledger.customer_balance_kobo(db, biz.id, cust.id)

    txs_raw = db.scalars(
        select(Transaction)
        .where(Transaction.business_id == biz.id, Transaction.customer_id == cust.id)
        .order_by(Transaction.id.desc())
    ).all()

    tx_list = []
    for tx in txs_raw:
        items_str = ""
        if tx.items_json and isinstance(tx.items_json, list):
            items_str = ", ".join(f"{i.get('item', '')} x{i.get('qty', 1)}" for i in tx.items_json)
        elif tx.transcript:
            items_str = tx.transcript
        tx_list.append({
            "id": tx.id,
            "created_at": tx.created_at,
            "kind": tx.kind,
            "items_summary": items_str,
            "total_kobo": tx.total_kobo,
            "paid_kobo": tx.paid_kobo,
            "due_date": tx.due_date,
            "status": tx.status,
        })

    payments = db.scalars(
        select(Payment).where(Payment.business_id == biz.id, Payment.customer_id == cust.id)
    ).all()

    return templates.TemplateResponse(
        request,
        "customer_detail.html",
        {
            "user": user,
            "business": biz,
            "customer": cust,
            "balance_kobo": bal,
            "transactions": tx_list,
            "payments": payments,
            "active_page": "customers",
        },
    )


@router.get("/customers/{customer_id}/statement.pdf")
def download_customer_statement(
    customer_id: int,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    cust = db.get(Customer, customer_id)
    if cust is None or cust.business_id != biz.id:
        raise HTTPException(status_code=404, detail="Customer not found.")

    doc = documents.issue_customer_statement(db, biz.id, cust.id)
    if not doc.pdf_path or not os.path.exists(doc.pdf_path):
        raise HTTPException(status_code=500, detail="Statement PDF generation failed.")

    return FileResponse(
        doc.pdf_path,
        media_type="application/pdf",
        filename=f"statement_{cust.name}.pdf",
    )


@router.get("/documents", response_class=HTMLResponse)
def documents_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    docs_raw = db.scalars(
        select(Document)
        .where(Document.business_id == biz.id)
        .order_by(Document.id.desc())
    ).all()

    doc_list = []
    for d in docs_raw:
        cust_name = ""
        if d.customer_id:
            c = db.get(Customer, d.customer_id)
            if c:
                cust_name = c.name
        doc_list.append({
            "id": d.id,
            "doc_number": d.doc_number,
            "kind": d.kind,
            "customer_name": cust_name,
            "total_kobo": d.total_kobo,
            "issued_at": d.issued_at,
            "status": d.status,
            "pdf_path": d.pdf_path,
        })

    return templates.TemplateResponse(
        request,
        "documents.html",
        {
            "user": user,
            "business": biz,
            "documents": doc_list,
            "active_page": "documents",
        },
    )


@router.get("/documents/{document_id}/pdf")
def download_document_pdf(
    document_id: int,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    doc = db.get(Document, document_id)
    if doc is None or doc.business_id != biz.id:
        raise HTTPException(status_code=404, detail="Document not found.")

    if not doc.pdf_path or not os.path.exists(doc.pdf_path):
        raise HTTPException(status_code=404, detail="PDF file not found on disk.")

    return FileResponse(
        doc.pdf_path,
        media_type="application/pdf",
        filename=f"{doc.doc_number}.pdf",
    )


@router.get("/stock", response_class=HTMLResponse)
def stock_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    if not plans.allowed(biz, "stock_tracking"):
        return templates.TemplateResponse(
            request,
            "plan_upgrade.html",
            {
                "user": user,
                "business": biz,
                "feature_name": "Stock & Inventory Control",
                "required_plan": "trader",
                "message": "Real-time stock tracking and inventory movement is available on Trader and Business plans.",
                "active_page": "stock",
            },
        )

    items = db.scalars(
        select(StockItem).where(StockItem.business_id == biz.id).order_by(StockItem.name.asc())
    ).all()

    return templates.TemplateResponse(
        request,
        "stock.html",
        {
            "user": user,
            "business": biz,
            "stock_items": items,
            "active_page": "stock",
        },
    )


@router.get("/staff", response_class=HTMLResponse)
def staff_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    if not (biz.plan == "business" or plans.allowed(biz, "staff_accounts")):
        return templates.TemplateResponse(
            request,
            "plan_upgrade.html",
            {
                "user": user,
                "business": biz,
                "feature_name": "Staff Accounts & Approvals",
                "required_plan": "business",
                "message": "Multi-seat attendant permissions and approval thresholds are available exclusively on the Business plan.",
                "active_page": "staff",
            },
        )

    staff_rows = db.scalars(
        select(Staff).where(Staff.business_id == biz.id).order_by(Staff.id.asc())
    ).all()

    staff_list = []
    for st in staff_rows:
        u = db.get(User, st.user_id)
        staff_list.append({
            "id": st.id,
            "display_name": u.display_name if u else None,
            "phone": u.channel_user_id if u else "Unknown",
            "role": st.role,
            "can_view_reports": bool(st.can_view_reports),
            "can_issue_invoices": bool(st.can_issue_invoices),
            "active": bool(st.active),
        })

    invites = db.scalars(
        select(Invite).where(Invite.business_id == biz.id).order_by(Invite.id.desc())
    ).all()

    plan_def = PLANS.get(biz.plan or "starter")
    max_staff = plan_def.staff_accounts if plan_def else 0

    return templates.TemplateResponse(
        request,
        "staff.html",
        {
            "user": user,
            "business": biz,
            "staff_members": staff_list,
            "invites": invites,
            "max_staff": max_staff,
            "active_page": "staff",
        },
    )


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    summary = reports.generate_evening_summary(db, biz.id)

    return templates.TemplateResponse(
        request,
        "reports.html",
        {
            "user": user,
            "business": biz,
            "summary": summary,
            "active_page": "reports",
        },
    )


@router.get("/reports/pnl.pdf")
def download_pnl_pdf(
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    today_dt = datetime.now(get_tz()).date()
    pdf_path = reports.generate_pnl_pdf(db, biz.id, target_date=today_dt)
    if not pdf_path or not os.path.exists(pdf_path):
        raise HTTPException(status_code=500, detail="P&L statement PDF generation failed.")

    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"pnl_statement_{biz.id}.pdf",
    )


@router.get("/profile", response_class=HTMLResponse)
def profile_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    consents = db.scalars(
        select(Consent).where(Consent.business_id == biz.id).order_by(Consent.id.desc())
    ).all()
    plan_def = PLANS.get(biz.plan or "starter")

    return templates.TemplateResponse(
        request,
        "profile.html",
        {
            "user": user,
            "business": biz,
            "consents": consents,
            "plan_def": plan_def,
            "active_page": "profile",
        },
    )
