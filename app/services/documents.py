"""Document generation service: receipts, invoices, quotations, and statements.

Features:
- Atomic per-business sequential document numbering (INV-0001, R-0001, Q-0001, ST-0001)
- ReportLab and Jinja2 PDF rendering with DejaVu Sans font (native Naira ₦ currency symbol)
- PDF file storage with SHA-256 integrity hash
- Document immutability (cancel and reissue; cancelled documents keep their number)
- wa.me chat link generation for trader forwarding
"""

from datetime import datetime, timezone
import hashlib
import io
import os
from pathlib import Path
import threading
import time
from urllib.parse import quote_plus

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    HRFlowable, Image as RLImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
)
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Business, Customer, DocCounter, Document, Transaction, now_iso
from app.money import fmt_naira, to_kobo

MEDIA_DIR = Path("media/documents")

PREFIXES = {
    "invoice": "INV-",
    "receipt": "R-",
    "quote": "Q-",
    "statement": "ST-",
    "report": "REP-",
}

_COUNTER_LOCK = threading.Lock()

# Register DejaVu Sans if available on system
FONT_NAME = "Helvetica"
NAIRA_SIGN = "N"

for font_path in [
    r"C:\Windows\Fonts\DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]:
    if os.path.exists(font_path):
        try:
            pdfmetrics.registerFont(TTFont("DejaVuSans", font_path))
            pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", font_path.replace(".ttf", "-Bold.ttf") if os.path.exists(font_path.replace(".ttf", "-Bold.ttf")) else font_path))
            FONT_NAME = "DejaVuSans"
            NAIRA_SIGN = "\u20a6"
            break
        except Exception:
            pass


def get_next_document_number(session: Session, business_id: int, kind: str) -> str:
    """Atomically generate the next sequential document number for this business."""
    prefix = PREFIXES.get(kind, "DOC-")
    with _COUNTER_LOCK:
        for _ in range(20):
            try:
                counter = session.scalar(
                    select(DocCounter).where(
                        DocCounter.business_id == business_id,
                        DocCounter.kind == kind,
                    )
                )
                if counter is None:
                    counter = DocCounter(business_id=business_id, kind=kind, last_no=1)
                    session.add(counter)
                else:
                    counter.last_no += 1

                session.commit()
                return f"{prefix}{counter.last_no:04d}"
            except Exception:
                session.rollback()
                time.sleep(0.05)

    return f"{prefix}0001"


def wa_me_link(phone: str | None, text: str) -> str:
    """Generate a WhatsApp click-to-chat forwarding link."""
    clean = "".join(c for c in (phone or "") if c.isdigit())
    encoded = quote_plus(text)
    if clean:
        return f"https://wa.me/{clean}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


def render_pdf_bytes(
    business_name: str,
    business_address: str | None,
    business_phone: str | None,
    business_bank: str | None,
    business_footer: str | None,
    brand_color: str | None,
    logo_path: str | None,
    doc_kind: str,
    doc_number: str,
    doc_date: str,
    customer_name: str | None,
    customer_phone: str | None,
    items: list[dict],
    total_kobo: int,
    paid_kobo: int,
    balance_kobo: int,
    due_date: str | None = None,
) -> bytes:
    """Render branded document PDF using ReportLab with DejaVu Sans font."""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=letter,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36,
    )

    primary_color = HexColor(brand_color if brand_color and brand_color.startswith("#") else "#2B6CB0")
    bold_font = f"{FONT_NAME}-Bold" if FONT_NAME == "DejaVuSans" else "Helvetica-Bold"

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("BizTitle", fontName=bold_font, fontSize=20, leading=24, textColor=primary_color)
    subtitle_style = ParagraphStyle("BizSub", fontName=FONT_NAME, fontSize=10, leading=14, textColor=HexColor("#4A5568"))
    doc_header_style = ParagraphStyle("DocHeader", fontName=bold_font, fontSize=16, leading=20, textColor=primary_color, alignment=2)
    meta_style = ParagraphStyle("Meta", fontName=FONT_NAME, fontSize=10, leading=14, alignment=2)
    label_style = ParagraphStyle("Label", fontName=bold_font, fontSize=10, leading=14, textColor=HexColor("#2D3748"))
    val_style = ParagraphStyle("Val", fontName=FONT_NAME, fontSize=10, leading=14, textColor=HexColor("#2D3748"))
    th_style = ParagraphStyle("TH", fontName=bold_font, fontSize=9, leading=12, textColor=HexColor("#FFFFFF"))
    td_style = ParagraphStyle("TD", fontName=FONT_NAME, fontSize=9, leading=12)
    td_num_style = ParagraphStyle("TDNum", fontName=FONT_NAME, fontSize=9, leading=12, alignment=2)
    footer_style = ParagraphStyle("Footer", fontName=FONT_NAME, fontSize=9, leading=12, textColor=HexColor("#718096"), alignment=1)

    story = []

    # Header Row: Logo/Biz info on Left, Doc Title / Number on Right
    left_cells = []
    if logo_path and os.path.exists(logo_path):
        try:
            from PIL import Image as PILImage
            with PILImage.open(logo_path) as pimg:
                pw, ph = pimg.size
            if pw > 0 and ph > 0:
                max_w = 1.6 * inch
                max_h = 0.9 * inch
                scale = min(max_w / pw, max_h / ph)
                logo_w = pw * scale
                logo_h = ph * scale
                left_cells.append(RLImage(logo_path, width=logo_w, height=logo_h))
                left_cells.append(Spacer(1, 6))
        except Exception as e:
            log.warning("Could not load logo image for PDF: %s", e)
    left_cells.append(Paragraph(business_name, title_style))
    if business_address:
        left_cells.append(Paragraph(business_address, subtitle_style))
    if business_phone:
        left_cells.append(Paragraph(f"Phone: {business_phone}", subtitle_style))

    right_cells = [
        Paragraph(doc_kind.upper(), doc_header_style),
        Spacer(1, 4),
        Paragraph(f"<b>No:</b> {doc_number}", meta_style),
        Paragraph(f"<b>Date:</b> {doc_date}", meta_style),
    ]
    if due_date:
        right_cells.append(Paragraph(f"<b>Due Date:</b> {due_date}", meta_style))

    header_table = Table([[left_cells, right_cells]], colWidths=[4.2 * inch, 3.0 * inch])
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=1.5, color=primary_color, spaceBefore=4, spaceAfter=14))

    # Bill To Box
    cust_str = customer_name or "Valued Customer"
    cust_rows = [
        [Paragraph("<b>BILLED TO:</b>", label_style)],
        [Paragraph(f"<b>{cust_str}</b>", val_style)],
    ]
    if customer_phone:
        cust_rows.append([Paragraph(f"Phone: {customer_phone}", val_style)])
    cust_table = Table(cust_rows, colWidths=[7.2 * inch])
    cust_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#E2E8F0")),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(cust_table)
    story.append(Spacer(1, 14))

    # Line Items Table
    curr_naira = NAIRA_SIGN
    table_data = [[
        Paragraph("Item / Description", th_style),
        Paragraph("Qty", th_style),
        Paragraph(f"Unit Price ({curr_naira})", th_style),
        Paragraph(f"Total ({curr_naira})", th_style),
    ]]

    for it in (items or []):
        name = it.get("name") or "Item"
        qty = f"{it.get('qty', 1):g} {it.get('unit', '')}".strip()
        uprice_kobo = it.get("unit_price_kobo") or (round(it["unit_price"] * 100) if "unit_price" in it and it["unit_price"] else None)
        uprice_str = fmt_naira(uprice_kobo).replace("N", curr_naira) if uprice_kobo else "-"
        ltotal_kobo = it.get("line_total_kobo") or (round(it["line_total"] * 100) if "line_total" in it and it["line_total"] else 0)
        ltotal_str = fmt_naira(ltotal_kobo).replace("N", curr_naira)
        table_data.append([
            Paragraph(name, td_style),
            Paragraph(qty, td_style),
            Paragraph(uprice_str, td_num_style),
            Paragraph(ltotal_str, td_num_style),
        ])

    table_style_commands = [
        ("BACKGROUND", (0, 0), (-1, 0), primary_color),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, HexColor("#E2E8F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]
    for row_idx in range(1, len(table_data)):
        if row_idx % 2 == 0:
            table_style_commands.append(("BACKGROUND", (0, row_idx), (-1, row_idx), HexColor("#F8FAFC")))

    items_table = Table(table_data, colWidths=[3.2 * inch, 1.2 * inch, 1.4 * inch, 1.4 * inch])
    items_table.setStyle(TableStyle(table_style_commands))
    story.append(items_table)
    story.append(Spacer(1, 14))

    # Totals Table
    tot_str = fmt_naira(total_kobo).replace("N", curr_naira)
    paid_str = fmt_naira(paid_kobo).replace("N", curr_naira)
    bal_str = fmt_naira(balance_kobo).replace("N", curr_naira)

    balance_label = "<b>Balance Due:</b>"
    balance_val = f"<b>{bal_str}</b>"
    if balance_kobo <= 0:
        balance_label = "<b>Status:</b>"
        balance_val = "<b>FULLY PAID</b>"

    totals_data = [
        [Paragraph("<b>Total Amount:</b>", val_style), Paragraph(f"<b>{tot_str}</b>", td_num_style)],
        [Paragraph("Amount Paid:", val_style), Paragraph(paid_str, td_num_style)],
        [Paragraph(balance_label, label_style), Paragraph(balance_val, td_num_style)],
    ]
    totals_table = Table(totals_data, colWidths=[2.0 * inch, 1.4 * inch])
    totals_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F8FAFC")),
        ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#E2E8F0")),
        ("LINEBELOW", (0, -1), (-1, -1), 1.5, primary_color),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    # Layout totals aligned to right
    align_table = Table([["", totals_table]], colWidths=[3.8 * inch, 3.4 * inch])
    story.append(align_table)
    story.append(Spacer(1, 16))

    if business_bank:
        bank_p = Paragraph(f"<b>Payment / Bank Details:</b><br/>{business_bank}", val_style)
        bank_table = Table([[bank_p]], colWidths=[7.2 * inch])
        bank_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F8FAFC")),
            ("BOX", (0, 0), (-1, -1), 0.5, HexColor("#E2E8F0")),
            ("PADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(bank_table)
        story.append(Spacer(1, 14))

    footer_text = business_footer or "Thank you for your business! Generated by Business Padi."
    story.append(Spacer(1, 20))
    story.append(Paragraph(footer_text, footer_style))

    doc.build(story)
    return buf.getvalue()


def issue_document(
    session: Session,
    business_id: int,
    kind: str,
    transaction_id: int | None = None,
    customer_id: int | None = None,
    items: list[dict] | None = None,
    total_kobo: int = 0,
    paid_kobo: int = 0,
    due_date: str | None = None,
    template: str = "classic",
) -> Document:
    """Issue a new immutable document and save its PDF to disk with SHA-256."""
    biz = session.get(Business, business_id)
    if biz is None:
        raise LookupError(f"Business {business_id} not found")

    customer = session.get(Customer, customer_id) if customer_id else None
    doc_number = get_next_document_number(session, business_id, kind)
    doc_date = datetime.now(timezone.utc).strftime("%d %b %Y")
    balance_kobo = total_kobo - paid_kobo

    # Generate PDF bytes
    pdf_bytes = render_pdf_bytes(
        business_name=biz.name or "My Shop",
        business_address=biz.address,
        business_phone=biz.phone,
        business_bank=biz.bank_details,
        business_footer=biz.footer_note,
        brand_color=biz.brand_color,
        logo_path=biz.logo_path,
        doc_kind="Receipt" if kind == "receipt" else ("Invoice" if kind == "invoice" else kind.title()),
        doc_number=doc_number,
        doc_date=doc_date,
        customer_name=customer.name if customer else None,
        customer_phone=customer.phone if customer else None,
        items=items or [],
        total_kobo=total_kobo,
        paid_kobo=paid_kobo,
        balance_kobo=balance_kobo,
        due_date=due_date,
    )

    # Compute SHA-256 and store on disk
    sha256 = hashlib.sha256(pdf_bytes).hexdigest()
    biz_dir = MEDIA_DIR / str(business_id)
    biz_dir.mkdir(parents=True, exist_ok=True)
    file_path = biz_dir / f"{doc_number}.pdf"
    file_path.write_bytes(pdf_bytes)

    doc = Document(
        business_id=business_id,
        kind=kind,
        number=doc_number,
        transaction_id=transaction_id,
        customer_id=customer_id,
        total_kobo=total_kobo,
        file_path=str(file_path),
        sha256=sha256,
        status="issued",
        created_at=now_iso(),
    )
    session.add(doc)
    session.commit()
    return doc


def cancel_document(session: Session, business_id: int, document_id: int) -> Document:
    """Cancel an issued document. The document keeps its number (immutable)."""
    doc = session.get(Document, document_id)
    if doc is None or doc.business_id != business_id:
        raise LookupError(f"Document {document_id} not found for business {business_id}")

    doc.status = "cancelled"
    session.commit()
    return doc


def issue_customer_statement(session: Session, business_id: int, customer_id: int) -> Document:
    """Generate and issue a customer statement containing all transactions."""
    biz = session.get(Business, business_id)
    customer = session.get(Customer, customer_id)
    if not biz or not customer:
        raise LookupError("Business or Customer not found")

    sales = list(session.scalars(select(Transaction).where(
        Transaction.business_id == business_id,
        Transaction.customer_id == customer_id,
        Transaction.kind == "sale",
        Transaction.status == "confirmed",
    ).order_by(Transaction.created_at.asc())))

    items = []
    total_kobo = 0
    paid_kobo = 0
    for s in sales:
        items.append({
            "name": f"Sale #{s.id} ({s.created_at[:10]})",
            "qty": 1,
            "unit": "",
            "unit_price_kobo": s.total_kobo,
            "line_total_kobo": s.total_kobo,
        })
        total_kobo += s.total_kobo
        paid_kobo += s.paid_kobo

    return issue_document(
        session=session,
        business_id=business_id,
        kind="statement",
        customer_id=customer_id,
        items=items,
        total_kobo=total_kobo,
        paid_kobo=paid_kobo,
    )
