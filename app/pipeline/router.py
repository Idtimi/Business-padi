from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.i18n import t
from app.models import Business, Customer, PendingAction, ProcessedMessage, Transaction, User
from app.money import fmt_naira
from app.pipeline.extract import ExtractionFailed, extract
from app.pipeline.llm import LLMClient, get_llm
from app.pipeline.validate import Validated, validate
from app.services import billing, documents, ledger, pending, plans, reports


@dataclass
class Reply:
    text: str
    buttons: list[tuple[str, str]] = field(default_factory=list)  # (id, title)
    kind: str = "info"  # info | confirm | clarify
    pending_id: int | None = None


def parse_button(button_id: str) -> tuple[str, int]:
    if ":" in button_id:
        action, pid_str = button_id.split(":", 1)
        try:
            return action, int(pid_str)
        except ValueError:
            return action, 0
    return button_id, 0


def _fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d:%a} {d.day} {d:%b}"
    except ValueError:
        return iso


def confirmation_text(lang: str, v: Validated, speech_lang: str | None = None) -> str:
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

    # Stricter confirmation for non-supported / beta languages: amounts isolated on their own line
    if speech_lang:
        try:
            from config.languages import is_supported
            if not is_supported(speech_lang):
                out.append("")
                out.append(f"TOTAL AMOUNT: {fmt_naira(v.total_kobo)}")
                out.append(t(lang, "strict_confirm_amount", amount=fmt_naira(v.total_kobo)))
        except Exception:
            pass

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


def _process(session, business_id, user_id, text, message_id, lang, llm, today, speech_lang: str | None = None) -> list[Reply]:
    biz = session.get(Business, business_id)
    names = list(session.scalars(select(Customer.name).where(Customer.business_id == business_id)))
    try:
        result = extract(llm, text, speech_lang or lang, [], names, today)
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
            if qtype in ("profit_period", "profit_estimate", "pnl"):
                from app.services import staff
                if not staff.check_permission(session, business_id, user_id, "view_profit"):
                    replies.append(Reply("You do not have permission to view profit reports.", kind="info"))
                    continue
            ans = reports.run_query(session, business_id, qtype, params, lang, today, llm=llm)
            replies.append(Reply(ans, kind="info"))
            continue
        if v.intent == "correction":
            from app.services import staff
            if not staff.check_permission(session, business_id, user_id, "edit_records"):
                replies.append(Reply("Attendants cannot edit past records. Please ask the shop owner.", kind="info"))
                continue
            corr = v.correction or {}
            try:
                p, reply_text = ledger.propose_correction(session, business_id, user_id, corr, text, f"{message_id}#{i}", lang)
                buttons = [(f"save:{p.id}", t(lang, "save")),
                           (f"cancel:{p.id}", t(lang, "cancel"))]
                replies.append(Reply(reply_text, buttons, "confirm", p.id))
            except ValueError as e:
                replies.append(Reply(str(e), kind="info"))
            continue
        if v.intent in {"receipt", "invoice", "quote", "quotation"}:
            doc_kind = "invoice" if v.intent == "invoice" else ("quote" if v.intent in ("quote", "quotation") else "receipt")
            feature_name = "invoices" if doc_kind == "invoice" else ("quotations" if doc_kind == "quote" else "receipts")

            # Check staff permissions
            if doc_kind == "invoice":
                from app.services import staff
                if not staff.check_permission(session, business_id, user_id, "issue_invoices"):
                    replies.append(Reply("You do not have permission to issue invoices.", kind="info"))
                    continue

            # Check plan entitlements
            if not plans.allowed(biz, feature_name):
                prompt_text, buttons = plans.upgrade_prompt_for_feature(feature_name, lang)
                replies.append(Reply(prompt_text, buttons=buttons, kind="info"))
                continue

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
            preview = f"Issue {doc_kind.title()} for {cust_name}: {fmt_naira(tot_kobo)}"
            payload = {
                "kind": doc_kind,
                "customer_id": cust_id,
                "items": items,
                "total_kobo": tot_kobo,
                "paid_kobo": paid_kobo,
                "transaction_id": tx.id if tx else None,
                "due_date": v.due_date,
            }
            p = pending.create(session, business_id, user_id, f"issue_{doc_kind}", payload)
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
        replies.append(Reply(confirmation_text(lang, v, speech_lang=speech_lang), buttons, "confirm", p.id))
    return replies


def handle_text(session: Session, business_id: int, user_id: int, text: str, message_id: str,
                lang: str = "en", llm: LLMClient | None = None, channel: str = "mock",
                today: date | None = None, is_voice: bool = False, speech_lang: str | None = None) -> list[Reply]:
    # Same message delivered twice? Ignore the repeat (spec rule 4).
    if session.get(ProcessedMessage, (channel, message_id)) is not None:
        return []
    llm = llm or get_llm()

    biz = session.get(Business, business_id)

    # Entitlement check for voice entry quota
    if is_voice and biz:
        allowed_v, cur_u, cap = plans.can_use_voice_entry(session, biz)
        if not allowed_v:
            text_prompt, buttons = plans.upgrade_prompt_for_voice_quota(cur_u, cap, biz.plan, lang)
            return [Reply(text_prompt, buttons=buttons, kind="info")]
        plans.increment_voice_usage(session, business_id)

    # Billing & Plan text commands
    stripped = text.strip()
    lower = stripped.lower()
    if lower in ("/plan", "plan", "/plans", "plans", "/billing", "billing") and biz:
        status = billing.get_billing_status(session, biz)
        msg_text, buttons = billing.format_plan_status_message(status, lang)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(msg_text, buttons=buttons)]

    if (lower.startswith("/upgrade") or lower.startswith("upgrade ")) and biz:
        parts = lower.split()
        target_plan = parts[1] if len(parts) > 1 else "trader"
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        try:
            b = billing.activate_plan(session, business_id, target_plan)
            return [Reply(f"Plan upgraded to {b.plan.title()} successfully!")]
        except ValueError as e:
            return [Reply(str(e))]

    if (lower.startswith("/downgrade") or lower.startswith("downgrade ")) and biz:
        parts = lower.split()
        target_plan = parts[1] if len(parts) > 1 else "starter"
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        try:
            b = billing.downgrade_plan(session, business_id, target_plan)
            return [Reply(f"Plan downgraded to {b.plan.title()}. Historical documents and records remain preserved.")]
        except ValueError as e:
            return [Reply(str(e))]

    if lower in ("/buy_pack", "buy_pack", "buy pack", "/buy pack") and biz:
        extra = billing.buy_entry_pack(session, business_id)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(f"Added +25 voice entries! Total extra entries this month: {extra}.")]

    # Staff Join with Code (e.g. "JOIN-A1B2C3" or "join A1B2C3")
    if lower.startswith("join-") or (lower.startswith("join ") and len(lower.split()) == 2):
        from app.services import staff
        code_part = stripped if lower.startswith("join-") else f"JOIN-{stripped.split(None, 1)[1]}"
        ok, msg, _ = staff.join_business_with_code(session, user_id, code_part)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(msg)]

    # Staff Invite command (e.g. "invite staff", "add staff")
    if lower in ("/invite staff", "invite staff", "/invite_staff", "invite_staff", "add staff", "/add staff") and biz:
        from app.services import staff
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        try:
            inv = staff.create_invite(session, business_id, user_id, role="attendant")
            msg = (
                f"Staff invite created!\n\n"
                f"Invite Code: *{inv.code}*\n"
                f"Expires in 24 hours.\n\n"
                f"Share this code with your shop attendant. When they message this bot and send:\n"
                f"*{inv.code}*\n"
                f"they will be added to your team!"
            )
            return [Reply(msg)]
        except Exception as e:
            return [Reply(str(e))]

    # Reminder text commands (remind now, pause, stop, resume)
    from app.services import reminders
    rem_resp = reminders.handle_reminder_text_command(session, business_id, text, lang)
    if rem_resp is not None:
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(rem_resp)]

    # Admin cost report command
    if lower in ("/cost report", "cost report", "/cost_report", "cost_report"):
        from app.services import costs
        rep_text = costs.generate_cost_report_text(session, today=today)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(rep_text)]

    # Dashboard magic link command
    if lower in ("/dashboard", "dashboard", "/login", "login") and biz:
        from app.services import dashboard
        _, link_url = dashboard.generate_magic_link(session, user_id, business_id)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(f"Here is your secure link to access the shop dashboard:\n{link_url}\n\nThis link expires in 15 minutes.")]

    # Financial trade profile command
    if lower in ("/profile", "profile", "/trade_profile", "trade profile") and biz:
        from app.services import profile
        if not profile.can_access_profile(biz):
            session.add(ProcessedMessage(channel=channel, message_id=message_id))
            session.commit()
            return [Reply("Financial trade profile sharing is available on Trader and Business plans. Upgrade your plan to share verified trade metrics with lenders and suppliers.")]
        p_data = profile.compute_financial_profile(session, biz.id, today=today)
        mat = p_data["record_maturity"]
        act = p_data["active_days_ratio"]
        col = p_data["collection_rate"]
        days_col = p_data["average_days_to_collect"]
        debt = p_data["debt_concentration"]
        msg_profile = (
            f" *Verified Financial Trade Profile*\n"
            f"Merchant: {p_data['business_name']}\n"
            f"• Record History: {mat['weeks']} weeks ({mat['days']} days)\n"
            f"• Active Days: {act['percentage']}%\n"
            f"• Collection Rate: {col['percentage']}%\n"
            f"• Avg Days to Collect: {days_col['days']} days\n"
            f"• Outstanding Debt: {debt['total_debt_naira']} (Top 3 share: {debt['top_3_share_pct']}%)\n\n"
            "View partner sharing links and consents in your dashboard under Profile."
        )
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(msg_profile)]

    # Multi-language command: /language ui <code/name> or /language speech <code/name>
    if any(lower == k or lower.startswith(k + " ") for k in (
        "/language", "language", "/lang", "lang", "change language", "change my language", "can i change my language"
    )):
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        user = session.get(User, user_id)
        parts = lower.split()
        if len(parts) >= 3 and parts[1] in ("ui", "speech"):
            subcmd, target_code = parts[1], parts[2]
            from config.languages import LANGUAGES, resolve_ui_language, get_language
            if subcmd == "ui":
                resolved = resolve_ui_language(target_code)
                if user:
                    user.language = resolved
                    session.commit()
                return [Reply(f"UI language updated to {get_language(resolved)['name']}.")]
            elif subcmd == "speech":
                if user:
                    st = dict(user.state_data or {})
                    st["speech_language"] = target_code
                    user.state_data = st
                    session.commit()
                # If planned language, record demand
                if target_code not in LANGUAGES or LANGUAGES[target_code].get("level") == "planned":
                    from app.models import Event
                    session.add(Event(
                        business_id=business_id,
                        user_id=user_id,
                        name="language_demand",
                        props={"language": target_code, "action": "set_speech_language"},
                    ))
                    session.commit()
                return [Reply(f"Speech language set to {get_language(target_code)['name']}.")]
        # Show status
        current_speech = (user.state_data or {}).get("speech_language", user.language) if user else lang
        buttons = [("cmd_lang_ui", "Change UI Lang"), ("cmd_lang_speech", "Change Voice Lang")]
        return [Reply(f"Current Settings:\n- UI Language: {lang}\n- Speech Language: {current_speech}\n\nTo change, send: /language ui <code/name> or /language speech <code/name>", buttons=buttons)]

        # Shop info command
    if any(k in lower for k in ("company name", "shop name", "my company", "my shop", "business name", "my business", "wetin be my")):
        biz = session.get(Business, business_id)
        if biz:
            category = (biz.custom_fields or {}).get("category", "General Trade")
            session.add(ProcessedMessage(channel=channel, message_id=message_id))
            session.commit()
            return [Reply(f"Shop Name: *{biz.name}*\nPhone: {biz.phone}\nPlan: {biz.plan.capitalize()}\nCategory: {category}")]

    clarify = _open_clarify(session, business_id, user_id)
    if clarify is not None:  # this message answers our earlier question
        text = f"{clarify.payload['transcript']} {text}"
        clarify.status = "applied"
        session.commit()

    replies = _process(session, business_id, user_id, text, message_id, lang, llm, today, speech_lang=speech_lang)
    session.add(ProcessedMessage(channel=channel, message_id=message_id))
    session.commit()
    return replies


def _saved_text(session, business_id, tx, lang) -> str:
    if tx.status == "pending":
        return f"Sale for {fmt_naira(tx.total_kobo)} recorded, but stays pending until the owner approves."
    text = t(lang, "saved")
    if tx.customer_id and tx.kind in {"sale", "payment_received"}:
        balance = ledger.customer_balance_kobo(session, business_id, tx.customer_id)
        if balance > 0:
            name = session.get(Customer, tx.customer_id).name
            text += " " + t(lang, "balance_now", customer=name, amount=fmt_naira(balance))
    return text


def handle_button(session: Session, business_id: int, user_id: int, action: str,
                  pending_id: int = 0, lang: str = "en") -> list[Reply]:
    # Staff transaction approval buttons
    if action == "approve_tx":
        from app.services import staff
        tx = staff.approve_transaction(session, business_id, pending_id, user_id)
        return [Reply(f"Sale of {fmt_naira(tx.total_kobo)} approved and added to ledger.")]
    if action == "reject_tx":
        from app.services import staff
        tx = staff.reject_transaction(session, business_id, pending_id, user_id)
        return [Reply(f"Sale of {fmt_naira(tx.total_kobo)} rejected.")]

    # Partner consent approval buttons
    if action == "approve_consent":
        from app.services import profile
        p_act = session.get(PendingAction, pending_id)
        if p_act and p_act.business_id == business_id and p_act.status == "open":
            partner = p_act.payload.get("partner", "Partner")
            scope = p_act.payload.get("scope", "read_profile")
            exp_days = p_act.payload.get("expires_in_days", 30)
            consent = profile.create_consent_record(session, business_id, partner, scope, exp_days)
            _, share_url, _ = profile.create_share_link(session, consent.id)
            p_act.status = "applied"
            session.commit()
            return [Reply(f"Consent approved for {partner}! Share link:\n{share_url}")]
        return [Reply("Consent request not found or already processed.")]

    if action == "reject_consent":
        p_act = session.get(PendingAction, pending_id)
        if p_act and p_act.business_id == business_id and p_act.status == "open":
            partner = p_act.payload.get("partner", "Partner")
            p_act.status = "cancelled"
            session.commit()
            return [Reply(f"Consent request for {partner} declined.")]
        return [Reply("Consent request not found or already processed.")]
    # Billing & Plan button actions
    if action == "upgrade_trader":
        billing.activate_plan(session, business_id, "trader")
        msg = ("Your account has been upgraded to the Trader plan (₦5,000/month)!"
               if lang != "pcm" else "Your account don upgrade to Trader plan (₦5,000/month)!")
        return [Reply(msg)]

    if action == "upgrade_business":
        billing.activate_plan(session, business_id, "business")
        msg = ("Your account has been upgraded to the Business plan (₦15,000/month)!"
               if lang != "pcm" else "Your account don upgrade to Business plan (₦15,000/month)!")
        return [Reply(msg)]

    if action == "buy_pack":
        extra = billing.buy_entry_pack(session, business_id)
        msg = (f"Added +25 voice entries for this month! Total extra entries: {extra}."
               if lang != "pcm" else f"We don add +25 voice notes for dis month! Extra notes now: {extra}.")
        return [Reply(msg)]

    if action == "view_plans":
        biz = session.get(Business, business_id)
        if biz:
            status = billing.get_billing_status(session, biz)
            text, buttons = billing.format_plan_status_message(status, lang)
            return [Reply(text, buttons=buttons)]
        return [Reply("Shop not found.")]

    if action == "send_receipt":
        tx = ledger.get_last_transaction(session, business_id)
        if tx is None:
            return [Reply("No recent sale found to create receipt.")]
        doc = documents.issue_document(
            session=session,
            business_id=business_id,
            kind="receipt",
            transaction_id=tx.id,
            customer_id=tx.customer_id,
            items=tx.items_json or [],
            total_kobo=tx.total_kobo,
            paid_kobo=tx.paid_kobo,
        )
        link = documents.wa_me_link(None, f"Receipt #{doc.number}: {fmt_naira(doc.total_kobo)}")
        return [Reply(f"Receipt #{doc.number} issued ({fmt_naira(doc.total_kobo)}).\nForward link: {link}")]

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
