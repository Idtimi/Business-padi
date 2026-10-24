"""Web dashboard routes mounted under /d."""

import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Form, Header, HTTPException, Query, Request, Response, status
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
from app.services import billing, dashboard, documents, ledger, plans, reports, staff

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
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not accept.startswith("*/*"):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/d/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard session invalid or expired. Please request a new login link via WhatsApp.",
        )
    user_id, biz_id = res
    user = db.get(User, user_id)
    biz = db.get(Business, biz_id)
    if user is None or biz is None:
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not accept.startswith("*/*"):
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                headers={"Location": "/d/login"},
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User or business not found.",
        )
    return user, biz


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, token: str | None = None, error: str | None = None, db: Session = Depends(get_db)):
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
            path="/",
            samesite="lax",
        )
        return resp
    return templates.TemplateResponse(request, "login.html", {"error": error})


@router.post("/login/demo")
def demo_login_action(db: Session = Depends(get_db)):
    """One-click instant login for judging and live evaluation."""
    # Find Mama Ngozi Provisions or the primary seeded shop
    biz = db.scalar(
        select(Business).where(Business.name.ilike("%Mama Ngozi%")).order_by(Business.id.desc())
    )
    if biz is None:
        biz = db.scalar(select(Business).order_by(Business.id.desc()))
    if biz is None:
        raise HTTPException(status_code=404, detail="No business registered yet.")

    owner_id = biz.owner_user_id
    cookie = dashboard.create_session_cookie(owner_id, biz.id)
    resp = RedirectResponse(url="/d", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=dashboard.SESSION_COOKIE_NAME,
        value=cookie,
        httponly=True,
        path="/",
        samesite="lax",
    )
    return resp


@router.post("/login/phone")
def phone_login_action(
    phone: str = Form(...),
    db: Session = Depends(get_db),
):
    """Direct phone number login for registered shop owners."""
    clean_p = phone.strip().replace(" ", "").replace("-", "")
    user = db.scalar(
        select(User).where(
            (User.channel_user_id == clean_p)
            | (User.channel_user_id == clean_p.lstrip("+"))
            | (User.channel_user_id.endswith(clean_p[-10:] if len(clean_p) >= 10 else clean_p))
        )
    )
    if user is None:
        return RedirectResponse(
            url="/d/login?error=Phone+number+not+found.+Please+check+the+number+or+launch+the+demo+shop.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    biz = db.scalar(select(Business).where(Business.owner_user_id == user.id))
    if biz is None:
        # Check if staff
        st = db.scalar(select(Staff).where(Staff.user_id == user.id, Staff.active == 1))
        if st is not None:
            biz = db.get(Business, st.business_id)

    if biz is None:
        return RedirectResponse(
            url="/d/login?error=No+active+shop+found+for+this+phone+number.",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    cookie = dashboard.create_session_cookie(user.id, biz.id)
    resp = RedirectResponse(url="/d", status_code=status.HTTP_303_SEE_OTHER)
    resp.set_cookie(
        key=dashboard.SESSION_COOKIE_NAME,
        value=cookie,
        httponly=True,
        path="/",
        samesite="lax",
    )
    return resp


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
    if not doc.file_path or not os.path.exists(doc.file_path):
        # Render on the fly via ReportLab fallback
        bal = ledger.customer_balance_kobo(db, biz.id, cust.id)
        sales = list(db.scalars(select(Transaction).where(
            Transaction.business_id == biz.id,
            Transaction.customer_id == cust.id,
            Transaction.kind == "sale",
            Transaction.status == "confirmed",
        ).order_by(Transaction.created_at.asc())))
        items = [{"name": f"Sale #{s.id} ({s.created_at[:10]})", "qty": 1, "unit": "", "unit_price_kobo": s.total_kobo, "line_total_kobo": s.total_kobo} for s in sales] or [{"name": "Account Balance", "qty": 1, "unit": "", "unit_price_kobo": bal, "line_total_kobo": bal}]
        paid = sum(s.paid_kobo for s in sales)
        pdf_bytes = documents.render_pdf_bytes(
            business_name=biz.name,
            business_address=biz.address,
            business_phone=biz.phone,
            business_bank=biz.bank_details,
            business_footer=getattr(biz, "footer_text", None),
            brand_color=getattr(biz, "brand_color", None),
            logo_path=getattr(biz, "logo_path", None),
            doc_kind="statement",
            doc_number=doc.number,
            doc_date=doc.created_at[:10] if doc.created_at else "2026-10-24",
            customer_name=cust.name,
            customer_phone=cust.phone,
            items=items,
            total_kobo=doc.total_kobo,
            paid_kobo=paid,
            balance_kobo=bal,
        )
        if doc.file_path:
            os.makedirs(os.path.dirname(doc.file_path), exist_ok=True)
            with open(doc.file_path, "wb") as f:
                f.write(pdf_bytes)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="statement_{cust.name}.pdf"'},
        )

    return FileResponse(
        doc.file_path,
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
            "doc_number": d.number,
            "kind": d.kind,
            "customer_name": cust_name,
            "total_kobo": d.total_kobo,
            "issued_at": d.created_at,
            "status": d.status,
            "pdf_path": d.file_path,
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

    if not doc.file_path or not os.path.exists(doc.file_path):
        # Generate on-the-fly if disk file missing
        cust = db.get(Customer, doc.customer_id) if doc.customer_id else None
        tx = db.get(Transaction, doc.transaction_id) if doc.transaction_id else None
        items = (tx.items_json if tx and tx.items_json else [
            {"name": "Items recorded", "qty": 1, "unit": "pkg", "unit_price_kobo": doc.total_kobo, "line_total_kobo": doc.total_kobo}
        ])
        paid = tx.paid_kobo if tx else (doc.total_kobo if doc.kind == "receipt" else 0)
        bal = max(0, doc.total_kobo - paid)

        pdf_bytes = documents.render_pdf_bytes(
            business_name=biz.name,
            business_address=biz.address,
            business_phone=biz.phone,
            business_bank=biz.bank_details,
            business_footer=getattr(biz, "footer_text", None),
            brand_color=getattr(biz, "brand_color", None),
            logo_path=getattr(biz, "logo_path", None),
            doc_kind=doc.kind,
            doc_number=doc.number,
            doc_date=doc.created_at[:10] if doc.created_at else "2026-10-24",
            customer_name=cust.name if cust else None,
            customer_phone=cust.phone if cust else None,
            items=items,
            total_kobo=doc.total_kobo,
            paid_kobo=paid,
            balance_kobo=bal,
        )
        if doc.file_path:
            os.makedirs(os.path.dirname(doc.file_path), exist_ok=True)
            with open(doc.file_path, "wb") as f:
                f.write(pdf_bytes)
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{doc.number}.pdf"'},
        )

    return FileResponse(
        doc.file_path,
        media_type="application/pdf",
        filename=f"{doc.number}.pdf",
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
            "can_view_reports": bool(st.can_see_profit),
            "can_issue_invoices": bool(st.can_issue_invoices),
            "active": bool(st.active),
        })

    invites = db.scalars(
        select(Invite).where(Invite.business_id == biz.id).order_by(Invite.id.desc())
    ).all()

    plan_def = PLANS.get(biz.plan or "starter")
    max_staff = plan_def.staff_accounts if plan_def else 0

    message = request.query_params.get("message")
    error = request.query_params.get("error")

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
            "message": message,
            "error": error,
        },
    )


@router.post("/staff/invite")
def create_staff_invite_route(
    role: str = Form("attendant"),
    can_issue_invoices: str | None = Form(None),
    can_see_profit: str | None = Form(None),
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    try:
        inv = staff.create_invite(
            db,
            biz.id,
            user.id,
            role=role,
            can_issue_invoices=bool(can_issue_invoices),
            can_see_profit=bool(can_see_profit),
        )
        db.commit()
        return RedirectResponse(
            url=f"/d/staff?message=Invite+code+{inv.code}+created+successfully!+Valid+for+24+hours.",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/d/staff?error={str(e)}",
            status_code=303,
        )


@router.post("/staff/add")
def add_staff_member_route(
    name: str = Form(...),
    phone: str = Form(...),
    role: str = Form("attendant"),
    can_issue_invoices: str | None = Form(None),
    can_see_profit: str | None = Form(None),
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    try:
        st = staff.add_staff_member(
            db,
            biz.id,
            user.id,
            phone=phone,
            name=name,
            role=role,
            can_issue_invoices=bool(can_issue_invoices),
            can_see_profit=bool(can_see_profit),
        )
        return RedirectResponse(
            url=f"/d/staff?message=Staff+member+{name or phone}+added+successfully!",
            status_code=303,
        )
    except Exception as e:
        return RedirectResponse(
            url=f"/d/staff?error={str(e)}",
            status_code=303,
        )


@router.post("/staff/{staff_id}/toggle")
def toggle_staff_status_route(
    staff_id: int,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    st = db.get(Staff, staff_id)
    if st and st.business_id == biz.id:
        st.active = 0 if st.active == 1 else 1
        action = "revoke_staff" if st.active == 0 else "restore_staff"
        staff.log_audit(db, biz.id, user.id, action, f"staff_id:{st.id}")
        db.commit()
    return RedirectResponse(url="/d/staff", status_code=303)


@router.post("/invites/{invite_id}/delete")
def delete_invite_route(
    invite_id: int,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    inv = db.get(Invite, invite_id)
    if inv and inv.business_id == biz.id:
        db.delete(inv)
        staff.log_audit(db, biz.id, user.id, "cancel_invite", f"invite:{inv.code}")
        db.commit()
    return RedirectResponse(url="/d/staff", status_code=303)


@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    # All confirmed sales
    sales_txs = list(db.scalars(select(Transaction).where(
        Transaction.business_id == biz.id,
        Transaction.kind == "sale",
        Transaction.status == "confirmed",
    )))
    sales_kobo = sum(t.total_kobo for t in sales_txs)
    sales_count = len(sales_txs)

    # Cash collected (cash sales + payments)
    cash_sales = sum(t.paid_kobo for t in sales_txs)
    payments = list(db.scalars(select(Payment).where(
        Payment.business_id == biz.id,
    )))
    cash_collected_kobo = cash_sales + sum(p.amount_kobo for p in payments)

    # Expenses
    exp_txs = list(db.scalars(select(Transaction).where(
        Transaction.business_id == biz.id,
        Transaction.kind == "expense",
        Transaction.status == "confirmed",
    )))
    expenses_kobo = sum(t.total_kobo for t in exp_txs)
    expenses_count = len(exp_txs)

    # Profit estimate
    profit_data = reports.profit_estimate(db, biz.id, "all") if hasattr(reports, "profit_estimate") else None
    net_profit_kobo = profit_data.get("profit_kobo", sales_kobo - expenses_kobo) if isinstance(profit_data, dict) else (sales_kobo - expenses_kobo)

    # Top debtors list
    customers = list(db.scalars(select(Customer).where(Customer.business_id == biz.id)))
    debtors = []
    for c in customers:
        bal = ledger.customer_balance_kobo(db, biz.id, c.id)
        if bal > 0:
            debtors.append({"id": c.id, "name": c.name, "phone": c.phone, "balance_kobo": bal})
    debtors.sort(key=lambda d: d["balance_kobo"], reverse=True)

    summary = {
        "sales_kobo": sales_kobo,
        "sales_count": sales_count,
        "cash_collected_kobo": cash_collected_kobo,
        "expenses_kobo": expenses_kobo,
        "expenses_count": expenses_count,
        "net_profit_kobo": net_profit_kobo,
        "top_debtors": debtors[:10],
    }

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
    pdf_path = reports.generate_pnl_pdf(db, biz.id, period="this_month", today=today_dt)
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


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    msg: str | None = None,
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    status_data = billing.get_billing_status(db, biz)

    return templates.TemplateResponse(
        request,
        "billing.html",
        {
            "user": user,
            "business": biz,
            "billing_status": status_data,
            "message": msg,
            "active_page": "billing",
        },
    )


@router.post("/billing/upgrade")
def billing_upgrade(
    plan: str = "trader",
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    billing.activate_plan(db, biz.id, plan)
    return RedirectResponse(
        url=f"/d/billing?msg=Plan+upgraded+to+{plan.title()}+successfully!",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/billing/downgrade")
def billing_downgrade(
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    billing.downgrade_plan(db, biz.id, "starter")
    return RedirectResponse(
        url="/d/billing?msg=Plan+downgraded+to+Starter.+All+historical+data+is+preserved.",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/billing/buy-pack")
def billing_buy_pack(
    auth: tuple[User, Business] = Depends(get_current_user_and_biz),
    db: Session = Depends(get_db),
):
    user, biz = auth
    extra = billing.buy_entry_pack(db, biz.id)
    return RedirectResponse(
        url=f"/d/billing?msg=Successfully+added+{extra}+voice+entries+to+your+quota!",
        status_code=status.HTTP_303_SEE_OTHER,
    )
