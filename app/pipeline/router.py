from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import t
from app.models import Business, Customer, PendingAction, ProcessedMessage, Transaction
from app.money import fmt_naira
from app.pipeline.extract import ExtractionFailed, extract
from app.pipeline.llm import LLMClient, get_llm
from app.pipeline.validate import Validated, validate
from app.services import documents, ledger, pending, reports


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (id, title)
    kind: str = "info"  # info | confirm | clarify
    pending_id: int | None = None


def parse_button(button_id: str) -> tuple[str, int]:
    action, pid = button_id.split(":")
    return action, int(pid)


def _fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d:%a} {d.day} {d:%b}"
    except ValueError:
        return iso


def confirmation_text(lang: str, v: Validated) -> str:
    label = t(lang, v.intent)
    out = []
    if v.lines:
        for l in v.lines:
            qty = f"{l.qty:g}" if l.qty is not None else "?"
            unit = f" {l.unit}" if l.unit else ""
            price = f" x {fmt_naira(l.unit_price_kobo)}" if l.unit_price_kobo is not None else ""
            out.append(f"{label}: {qty}{unit} {l.name}{price} = {fmt_naira(l.line_total_kobo)}")
        if len(v.lines) > 1:
            out.append(f"{t(lang, 'total')}: {fmt_naira(v.total_kobo)}")
    else:
        extra = f" ({v.category})" if v.category else ""
        out.append(f"{label}: {fmt_naira(v.total_kobo)}{extra}")
    if v.customer:
        out.append(f"{t(lang, 'customer')}: {v.customer}")
    if v.supplier:
        out.append(f"{t(lang, 'supplier')}: {v.supplier}")
    if v.intent in {"sale", "invoice"}:
        money = f"{t(lang, 'paid')}: {fmt_naira(v.paid_kobo)}"
        if v.balance_kobo > 0:
            money += f"   {t(lang, 'owes')}: {fmt_naira(v.balance_kobo)}"
            if v.due_date:
                money += f" ({t(lang, 'due')} {_fmt_date(v.due_date)})"
        out.append(money)
    return "\n".join(out)


def _open_clarify(session: Session, business_id: int, user_id: int) -> PendingAction | None:
    p = session.scalar(select(PendingAction).where(
        PendingAction.business_id == business_id, PendingAction.user_id == user_id,
        PendingAction.kind == "clarify", PendingAction.status == "open",
    ).order_by(PendingAction.id.desc()))
    if p is not None and pending.is_expired(p):
        p.status = "expired"
        session.commit()
        return None
    return p


def _ask(session, business_id, user_id, text: str, question: str) -> Reply:
    """Store the half-understood note so the next message continues it."""
    p = pending.create(session, business_id, user_id, "clarify",
                       {"transcript": text, "question": question})
    session.commit()
    return Reply(question, kind="clarify", pending_id=p.id)


def _process(session, business_id, user_id, text, message_id, lang, llm, today) -> list[Reply]:
    names = list(session.scalars(select(Customer.name).where(Customer.business_id == business_id)))
    try:
        result = extract(llm, text, lang, [], names, today)
    except ExtractionFailed:
        return [Reply(t(lang, "try_again"))]

    replies: list[Reply] = []
    for i, x in enumerate(result.transactions):
        v = validate(x)
        if v.problems:
            replies.append(_ask(session, business_id, user_id, text, v.problems[0]))
            continue
        if v.intent == "chitchat":
            continue  # no filler messages (they cost money)
        if v.intent == "query":
            q_info = v.query or {}
            qtype = q_info.get("type", "total_owed")
            params = q_info.get("params", {})
            ans = reports.run_query(session, business_id, qtype, params, lang, today, llm=llm)
            replies.append(Reply(ans, kind="info"))
            continue
        if v.intent == "correction":
            corr = v.correction or {}
            try:
                p, reply_text = ledger.propose_correction(session, business_id, user_id, corr, text, f"{message_id}#{i}", lang)
                buttons = [(f"save:{p.id}", t(lang, "save")),
                           (f"cancel:{p.id}", t(lang, "cancel"))]
                replies.append(Reply(reply_text, buttons, "confirm", p.id))
            except ValueError as e:
                replies.append(Reply(str(e), kind="info"))
            continue
        if v.intent in {"receipt", "invoice"}:
            cust = ledger.get_or_create_customer(session, business_id, v.customer)
            tx = None
            if not v.lines and v.total_kobo <= 0:
                tx = ledger.get_last_transaction(session, business_id)
                if tx is None:
                    replies.append(Reply("No recent sale found to create document."))
                    continue
                cust_id = tx.customer_id
                items = tx.items_json or []
                tot_kobo = tx.total_kobo
                paid_kobo = tx.paid_kobo
            else:
                cust_id = cust.id if cust else None
                items = [
                    {"name": l.name, "qty": l.qty, "unit": l.unit, "unit_price_kobo": l.unit_price_kobo, "line_total_kobo": l.line_total_kobo}
                    for l in v.lines
                ]
                tot_kobo = v.total_kobo
                paid_kobo = v.paid_kobo

            cust_name = session.get(Customer, cust_id).name if cust_id else (v.customer or "Customer")
            preview = f"Issue {v.intent.title()} for {cust_name}: {fmt_naira(tot_kobo)}"
            payload = {
                "kind": v.intent,
                "customer_id": cust_id,
                "items": items,
                "total_kobo": tot_kobo,
                "paid_kobo": paid_kobo,
                "transaction_id": tx.id if tx else None,
                "due_date": v.due_date,
            }
            p = pending.create(session, business_id, user_id, f"issue_{v.intent}", payload)
            session.commit()
            buttons = [(f"issue:{p.id}", "Issue"), (f"cancel:{p.id}", t(lang, "cancel"))]
            replies.append(Reply(preview, buttons=buttons, kind="confirm", pending_id=p.id))
            continue
        if v.intent not in ledger.SAVEABLE:
            replies.append(Reply(t(lang, "not_yet")))
            continue
        try:
            p = ledger.propose(session, business_id, user_id, v, text, f"{message_id}#{i}")
        except ValueError as e:
            replies.append(_ask(session, business_id, user_id, text, str(e)))
            continue
        buttons = [(f"save:{p.id}", t(lang, "save")),
                   (f"edit:{p.id}", t(lang, "edit")),
                   (f"cancel:{p.id}", t(lang, "cancel"))]
        replies.append(Reply(confirmation_text(lang, v), buttons, "confirm", p.id))
    return replies


def handle_text(session: Session, business_id: int, user_id: int, text: str, message_id: str,
                lang: str = "en", llm: LLMClient | None = None, channel: str = "mock",
                today: date | None = None) -> list[Reply]:
    # Same message delivered twice? Ignore the repeat (spec rule 4).
    if session.get(ProcessedMessage, (channel, message_id)) is not None:
        return []
    llm = llm or get_llm()

    clarify = _open_clarify(session, business_id, user_id)
    if clarify is not None:  # this message answers our earlier question
        text = f"{clarify.payload['transcript']} {text}"
        clarify.status = "applied"
        session.commit()

    replies = _process(session, business_id, user_id, text, message_id, lang, llm, today)
    session.add(ProcessedMessage(channel=channel, message_id=message_id))
    session.commit()
    return replies


def _saved_text(session, business_id, tx, lang) -> str:
    text = t(lang, "saved")
    if tx.customer_id and tx.kind in {"sale", "payment_received"}:
        balance = ledger.customer_balance_kobo(session, business_id, tx.customer_id)
        if balance > 0:
            name = session.get(Customer, tx.customer_id).name
            text += " " + t(lang, "balance_now", customer=name, amount=fmt_naira(balance))
    return text


def handle_button(session: Session, business_id: int, user_id: int, action: str,
                  pending_id: int, lang: str = "en") -> list[Reply]:
    try:
        p = pending.get_for_business(session, business_id, pending_id)
        if action in {"save", "issue"}:
            if p.kind.startswith("issue_"):
                data = p.payload
                doc = documents.issue_document(
                    session=session,
                    business_id=business_id,
                    kind=data["kind"],
                    transaction_id=data.get("transaction_id"),
                    customer_id=data.get("customer_id"),
                    items=data.get("items"),
                    total_kobo=data["total_kobo"],
                    paid_kobo=data["paid_kobo"],
                    due_date=data.get("due_date"),
                )
                p.status = "applied"
                session.commit()
                cust = session.get(Customer, data.get("customer_id")) if data.get("customer_id") else None
                biz = session.get(Business, business_id)
                link = documents.wa_me_link(
                    cust.phone if cust else None,
                    f"Here is your {doc.kind} #{doc.number} from {biz.name if biz else 'Shop'}: {fmt_naira(doc.total_kobo)}"
                )
                msg_text = f"{doc.kind.title()} #{doc.number} issued ({fmt_naira(doc.total_kobo)}).\nForward link: {link}"
                return [Reply(msg_text)]
            if p.kind == "correction":
                tx = ledger.confirm_correction(session, business_id, pending_id)
                return [Reply(_saved_text(session, business_id, tx, lang))]
            tx = ledger.confirm(session, business_id, pending_id)
            return [Reply(_saved_text(session, business_id, tx, lang))]
        if action == "cancel":
            pending.cancel(session, business_id, pending_id)
            return [Reply(t(lang, "cancelled"))]
        if action == "edit":
            pending.cancel(session, business_id, pending_id)
            return [Reply(t(lang, "edit_prompt"))]
    except ValueError as e:
        return [Reply(str(e))]
    except LookupError:
        return [Reply(t(lang, "try_again"))]
    return [Reply(t(lang, "try_again"))]
