from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.i18n import t
from app.models import Business, Customer, Document, PendingAction, ProcessedMessage, Transaction, User
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
    document_path: str | None = None
    document_filename: str | None = None


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


def _parse_restock_text(text: str) -> dict | None:
    import re
    clean = text.strip()
    if clean.startswith("/"):
        clean = clean[1:]
    
    m_cmd = re.match(
        r"^restock(?:ed)?\s+(?:(\d+(?:\.\d+)?)\s*(?:([a-zA-Z]+)\s+of\s+|([a-zA-Z]+)\s+)?([a-zA-Z0-9\s]+?)|([a-zA-Z0-9\s]+?)\s+(\d+(?:\.\d+)?)\s*([a-zA-Z]+)?)(?:\s+(?:at|for|each|@)\s+([0-9kK,\.]+))?(?:\s+from\s+([a-zA-Z\s]+))?$",
        clean,
        re.IGNORECASE,
    )
    if m_cmd:
        g = m_cmd.groups()
        if g[0]:
            qty = float(g[0])
            unit = g[1] or g[2] or "unit"
            item = g[3].strip()
        else:
            item = g[4].strip()
            qty = float(g[5])
            unit = g[6] or "unit"
        price = g[7]
        supplier = g[8].strip() if g[8] else None
        return {"item": item, "qty": qty, "unit": unit, "price": price, "supplier": supplier}

    m_nat = re.search(
        r"(?:i\s+(?:just\s+)?(?:got|bought|receive|received)|(?:just\s+)?got|added)\s+(\d+(?:\.\d+)?)\s*(?:([a-zA-Z]+)\s+of\s+|([a-zA-Z]+)\s+)?([a-zA-Z0-9\s]+?)(?:\s+(?:in|to)\s+stock|\s+(?:at|for|each|@)\s+([0-9kK,\.]+)|$)",
        clean,
        re.IGNORECASE,
    )
    if m_nat:
        qty = float(m_nat.group(1))
        unit = m_nat.group(2) or m_nat.group(3) or "unit"
        item = m_nat.group(4).replace("in stock", "").replace("to stock", "").strip()
        price = m_nat.group(5)
        return {"item": item, "qty": qty, "unit": unit, "price": price, "supplier": None}

    return None



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
                out.append(f"{t(lang, 'total').upper()}: {fmt_naira(v.total_kobo)}")
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
            prob = v.problems[0]
            if "sorry, i no understand" in prob.lower() or "not understand" in prob.lower() or "abeg" in prob.lower():
                prob = t(lang, "not_understood")
            replies.append(_ask(session, business_id, user_id, text, prob))
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
                tx = ledger.get_last_transaction(session, business_id, user_id=user_id)
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
            buttons = [(f"issue:{p.id}", t(lang, "issue")), (f"cancel:{p.id}", t(lang, "cancel"))]
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
            tg_bot = getattr(settings, "telegram_bot_username", "Business_padi_bot").lstrip("@")
            join_link = f"https://t.me/{tg_bot}?start={inv.code}"
            msg = (
                f"Staff invite created!\n\n"
                f"Invite Code: *{inv.code}*\n"
                f"One-click Telegram Link:\n{join_link}\n\n"
                f"Share this with your shop attendant. When they tap the link and press Start (or send *{inv.code}* to the bot), they will immediately join {biz.name} as an attendant!"
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

    # Inventory & Stock commands: /stock, /items, /inventory
    if any(lower == k or lower.startswith(k + " ") for k in ("/stock", "stock", "/inventory", "inventory", "/items", "items", "check stock", "stock levels", "my stock", "stock report")):
        from app.models import StockItem
        if biz and not plans.allowed(biz, "stock_tracking"):
            return [Reply("Stock tracking is available on Trader and Business plans. Type /plan to upgrade.")]
        items = session.scalars(
            select(StockItem).where(StockItem.business_id == business_id).order_by(StockItem.name.asc())
        ).all()
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        if not items:
            return [Reply(
                f"No items in {biz.name if biz else 'shop'}'s inventory yet.\n\n"
                f"To add items:\n"
                f"- /add_item <Product Name> [unit]\n"
                f"- /restock <Product Name> <Quantity>\n"
                f"- Or manage inventory on your web dashboard: /dashboard"
            )]
        lines = [f"INVENTORY - {biz.name if biz else 'Shop'}\n"]
        for it in items:
            unit_str = f" {it.base_unit}" if it.base_unit else ""
            status_str = "In Stock"
            if it.qty < 0:
                status_str = "Negative Stock"
            elif it.low_at is not None and it.qty <= it.low_at:
                status_str = "Low Stock"
            elif it.qty == 0:
                status_str = "Out of Stock"
            cost_str = f" (Avg cost: {fmt_naira(it.avg_cost_kobo)})" if it.avg_cost_kobo > 0 else ""
            lines.append(f"- {it.name}: {it.qty:g}{unit_str} [{status_str}]{cost_str}")
        lines.append("\nTo add more stock: /restock <item> <qty>\nTo register a new item: /add_item <name>")
        return [Reply("\n".join(lines))]

    # Add item to catalog: /add_item <Product Name> [Unit]
    if lower.startswith("/add_item") or lower.startswith("add item "):
        from app.services import stock
        parts = text.split()
        if len(parts) >= 2:
            rem = text[len(parts[0]):].strip()
            rem_parts = rem.split()
            if len(rem_parts) == 1:
                item_name = rem_parts[0]
                base_unit = "unit"
            else:
                item_name = " ".join(rem_parts[:-1])
                base_unit = rem_parts[-1]
            item = stock.get_or_create_item(session, business_id, item_name, base_unit=base_unit)
            session.commit()
            session.add(ProcessedMessage(channel=channel, message_id=message_id))
            session.commit()
            return [Reply(f"Added \"{item.name}\" (unit: {item.base_unit}) to {biz.name if biz else 'shop'}'s catalog!\nType /restock {item.name} <qty> to add inventory.")]
        return [Reply("Usage: /add_item <Product Name> [Unit]\nExample: /add_item Rice bag")]

    # Check for empty /restock prompt
    if lower in ("/restock", "restock"):
        return [Reply(
            "To restock an item, type e.g.:\n"
            "- /restock Rice 30 bags\n"
            "- /restock Rice 30 bags at 45000\n"
            "- /restock Indomie 10 cartons from Alhaji Musa\n\n"
            "Or speak naturally: 'I just got 30 bags of rice in stock'."
        )]

    # Parse and execute restock command or natural language
    restock_data = _parse_restock_text(text)
    if restock_data and (lower.startswith("/restock") or lower.startswith("restock") or "in stock" in lower or "to stock" in lower or "got " in lower):
        from app.services import stock
        from app.money import to_kobo
        if biz and not plans.allowed(biz, "stock_tracking"):
            return [Reply("Stock tracking is available on Trader and Business plans. Type /plan to upgrade.")]
        cost_val = 0
        if restock_data.get("price"):
            raw_p = str(restock_data["price"]).lower().replace(",", "").replace("k", "000")
            try:
                cost_val = float(raw_p)
            except ValueError:
                cost_val = 0
        cost_kobo = to_kobo(cost_val) if cost_val > 0 else 0
        item, move = stock.record_restock(
            session=session,
            business_id=business_id,
            item_name=restock_data["item"],
            qty=restock_data["qty"],
            unit=restock_data["unit"],
            unit_cost_kobo=cost_kobo,
            supplier=restock_data.get("supplier"),
        )
        session.commit()
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()

        cost_txt = f" (Avg cost: {fmt_naira(item.avg_cost_kobo)})" if item.avg_cost_kobo > 0 else ""
        unit_txt = f" {item.base_unit}" if item.base_unit else ""
        reply_msg = (
            f"Restocked {restock_data['qty']:g}{unit_txt} of {item.name}!\n"
            f"Current stock on hand: {item.qty:g}{unit_txt}.{cost_txt}\n\n"
            f"You can view your full inventory anytime with /stock or on your dashboard."
        )
        return [Reply(reply_msg)]

    # Help and all commands guide
    if lower in ("/commands", "commands", "/help", "help", "/menu", "menu", "commands menu", "help menu", "show commands", "show menu"):
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        cmd_text = (
            "BUSINESS PADI - ALL COMMANDS\n\n"
            "Tap any quick button below, tap the Menu button beside the message bar, or speak a voice note.\n\n"
            "RECORD KEEPING (Voice & Text)\n"
            "- Voice Note: Speak naturally in English, Pidgin, Yoruba, Hausa, or Igbo.\n"
            "- Cash Sale: \"Sold 3 bags rice for 120k cash\"\n"
            "- Credit Sale: \"Sold 2 bags rice to Tunde for 80k, he owes 30k\"\n"
            "- Customer Payment: \"Tunde paid 30k\"\n"
            "- Expense: \"Paid 15k for generator fuel\"\n\n"
            "RECEIPTS & DOCUMENTS\n"
            "/receipt - Download PDF receipt for latest sale + send to customer\n"
            "/invoice - Generate PDF invoice for customer orders\n\n"
            "INVENTORY & STOCK\n"
            "/stock (or /items) - View all products, stock quantities, and alerts\n"
            "/restock <item> <qty> - Add new inventory for any product\n"
            "/add_item <name> [unit] - Register a new product to your shop catalog\n\n"
            "WEB DASHBOARD\n"
            "/dashboard (or /login) - Get direct link to your shop's web dashboard\n\n"
            "DEBTS & REPORTS\n"
            "/debtors (or \"who dey owe\") - View all customers owing money & total debt\n"
            "/report (or /today) - Today's sales summary, cash collected, and profit\n\n"
            "SETTINGS & PREFERENCES\n"
            "/language - Change UI language or Voice transcription language\n"
            "/settings - View shop profile (name, brand color, address, bank)\n"
            "/settings name <New Name> - Change your shop name\n"
            "/settings color <#hex or color> - Change receipt brand color\n"
            "/settings address <Address> - Set your shop physical address\n"
            "/settings bank <Bank Details> - Set bank details printed on receipts\n"
            "/start - Check shop status or welcome menu\n"
            "/delete_account (or /reset) - Permanently delete shop & reset account to start over\n\n"
            "TEAM & SUBSCRIPTION\n"
            "/staff - View active shop attendants\n"
            "/invite_staff - Generate a 24hr invite code for a shop attendant\n"
            "/audit - Review and approve today's attendant sales & stock book\n"
            "/plan - Check your subscription tier (Starter, Trader, Business) or upgrade"
        )
        quick_buttons = [
            ("cmd_receipt", "Receipt"),
            ("cmd_invoice", "Invoice"),
            ("cmd_report", "Today's Report"),
            ("cmd_debtors", "Debtors"),
            ("cmd_stock", "Stock Levels"),
            ("cmd_audit", "Daily Audit"),
        ]
        return [Reply(cmd_text, buttons=quick_buttons)]

    # Daily staff audit and reconciliation command (Owner only)
    if lower in ("/audit", "audit", "/daily_audit", "daily audit", "/daily-audit", "/stockbook", "stockbook", "/end_day", "/reconcile", "daily stockbook", "audit staff") and biz:
        from app.services import staff
        if not staff.is_owner(session, business_id, user_id):
            session.add(ProcessedMessage(channel=channel, message_id=message_id))
            session.commit()
            return [Reply("Access restricted: Daily staff audits are only available to the shop owner.")]
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        audit_data = staff.get_daily_staff_audit(session, business_id)
        report_text = staff.format_daily_audit_text(session, audit_data, biz.name)
        buttons = []
        if audit_data.get("pending_reconcile_count", 0) > 0:
            cash_fmt = fmt_naira(audit_data.get("total_cash_kobo", 0))
            buttons.append(("audit_approve_all", f"Approve & Reconcile All ({cash_fmt})"))
        return [Reply(report_text, buttons=buttons)]

    # Dashboard magic link command
    if any(lower == k or lower.startswith(k + " ") for k in ("/dashboard", "dashboard", "/login", "login")):
        from app.services import staff
        if biz and not staff.is_owner(session, biz.id, user_id):
            session.add(ProcessedMessage(channel=channel, message_id=message_id))
            session.commit()
            return [Reply("Access restricted: Shop attendants cannot access the owner's web dashboard. Please record transactions directly in this chat.")]

        if biz is None:
            biz = session.scalar(select(Business).where(Business.owner_user_id == user_id))
            if biz is None:
                user = session.get(User, user_id)
                shop_name = f"Shop {user.channel_user_id[-4:] if user else user_id}"
                biz = Business(owner_user_id=user_id, name=shop_name)
                session.add(biz)
                session.commit()
        from app.services import dashboard
        _, link_url = dashboard.generate_magic_link(session, user_id, biz.id)
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return [Reply(
            f"Here is your direct access link to your {biz.name} dashboard:\n\n"
            f"{link_url}\n\n"
            f"This link expires in 15 minutes.\n"
            f"Tap to view live sales charts, customer ledgers, inventory, and export reports."
        )]

    # Check if user is replying with a phone number for pending receipt
    user = session.get(User, user_id)
    if user and user.state_data and user.state_data.get("pending_receipt_doc_id"):
        st = dict(user.state_data)
        pending_doc_id = st.get("pending_receipt_doc_id")
        clean_digits = "".join(c for c in text if c.isdigit())
        is_phone = len(clean_digits) in (10, 11, 13, 14) and not any(w in lower for w in ("sell", "sold", "buy", "bought", "fuel", "rent", "paid", "credit", "bag", "rice", "carton"))
        if is_phone:
            if clean_digits.startswith("0") and len(clean_digits) == 11:
                clean_phone = "234" + clean_digits[1:]
            elif clean_digits.startswith("234"):
                clean_phone = clean_digits
            else:
                clean_phone = clean_digits

            doc = session.get(Document, pending_doc_id)
            cust_id = st.get("pending_receipt_cust_id")
            cust = session.get(Customer, cust_id) if cust_id else None
            if cust:
                cust.phone = clean_phone
                session.commit()

            st.pop("pending_receipt_doc_id", None)
            st.pop("pending_receipt_cust_id", None)
            user.state_data = st
            session.commit()

            if doc:
                download_url = f"{settings.base_url.rstrip('/')}/d/receipt/{doc.number}"
                wa_msg = f"Hello {cust.name if cust else 'Customer'}, here is your receipt #{doc.number} from {biz.name if biz else 'Shop'} for {fmt_naira(doc.total_kobo)}: {download_url}"
                wa_link = documents.wa_me_link(clean_phone, wa_msg)
                session.add(ProcessedMessage(channel=channel, message_id=message_id))
                session.commit()
                return [Reply(
                    f"Phone number saved (+{clean_phone})!\n\n"
                    f"Tap the link below to send receipt #{doc.number} to {cust.name if cust else 'customer'} on WhatsApp:\n"
                    f"{wa_link}\n\n"
                    f"Direct PDF download:\n{download_url}"
                )]
        else:
            st.pop("pending_receipt_doc_id", None)
            st.pop("pending_receipt_cust_id", None)
            user.state_data = st
            session.commit()

    # Direct receipt command
    if lower in ("/receipt", "receipt", "/send_receipt", "send receipt", "download receipt", "latest receipt", "my receipt", "get receipt") and biz:
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return handle_button(session, business_id, user_id, "get_receipt", lang=lang)

    # Direct invoice command
    if lower in ("/invoice", "invoice", "/send_invoice", "send invoice", "/invoices", "invoices", "download invoice", "latest invoice", "my invoice", "get invoice") and biz:
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        return handle_button(session, business_id, user_id, "get_invoice", lang=lang)

    # Debtors query command
    if lower in ("/debtors", "debtors", "/debts", "debts", "/debt", "debt", "who dey owe", "who owe me", "debtors list", "my debtors") and biz:
        from app.services import reports
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        ans = reports.run_query(session, business_id, "top_debtor", {}, lang, today, llm=llm)
        return [Reply(ans)]

    # Sales & Profit report command
    if lower in ("/report", "report", "/summary", "summary", "/sales", "sales", "/today", "today", "today's report", "todays report", "today report", "daily report") and biz:
        from app.services import reports
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        ans = reports.run_query(session, business_id, "profit_period", {"period": "today"}, lang, today, llm=llm)
        return [Reply(ans)]

    # Staff list command
    if lower in ("/staff", "staff", "/team", "team") and biz:
        from app.services import staff
        session.add(ProcessedMessage(channel=channel, message_id=message_id))
        session.commit()
        members = staff.list_staff(session, business_id)
        if not members:
            return [Reply(
                f"You currently have no attendants registered for {biz.name}.\n\n"
                f"Type /invite_staff to generate an invite code for your shop staff."
            )]
        lines = []
        for s in members:
            u_mem = session.get(User, s.user_id)
            display = (u_mem.display_name if u_mem else None) or (u_mem.channel_user_id if u_mem else None) or "Attendant"
            lines.append(f"- {s.role.title()}: {display}")
        return [Reply(f"Team members for {biz.name}:\n" + "\n".join(lines) + "\n\nType /invite_staff to add an attendant.")]

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
    if tx.kind == "restock":
        items_summary = []
        for line in (tx.items_json or []):
            qty_str = f"{line.get('qty', 0):g}"
            unit_str = f" {line.get('unit')}" if line.get("unit") else ""
            items_summary.append(f"{qty_str}{unit_str} {line.get('name', 'item')}")
        summary_str = ", ".join(items_summary) if items_summary else "Inventory"
        return f"Restocked {summary_str}! Stock level has been updated."

    if tx.status == "pending":
        if lang == "yo":
            return f"A ti se akosile tita {fmt_naira(tx.total_kobo)}, sugbon yoo duro de ase oga."
        elif lang == "ha":
            return f"An rubuta sayarwar {fmt_naira(tx.total_kobo)}, amma zai jira amincewar mai shago."
        elif lang == "ig":
            return f"E dekoolo ahia nke {fmt_naira(tx.total_kobo)}, mana o ga-eche nkwado onye nwe ulo ahia."
        elif lang == "pcm":
            return f"Sale for {fmt_naira(tx.total_kobo)} don record, but e go wait make oga approve am."
        return f"Sale for {fmt_naira(tx.total_kobo)} recorded, but stays pending until the owner approves."

    text = t(lang, "saved")
    biz = session.get(Business, business_id)
    is_staff = (biz is not None and biz.owner_user_id != tx.created_by_user_id)
    if is_staff and tx.kind == "sale":
        cash_rec = tx.paid_kobo or tx.total_kobo
        text += f" (Recorded for {biz.name}. Cash collected: {fmt_naira(cash_rec)})"
    if tx.customer_id and tx.kind in {"sale", "payment_received"}:
        balance = ledger.customer_balance_kobo(session, business_id, tx.customer_id)
        if balance > 0:
            name = session.get(Customer, tx.customer_id).name
            text += " " + t(lang, "balance_now", customer=name, amount=fmt_naira(balance))
    return text


def handle_button(session: Session, business_id: int, user_id: int, action: str,
                  pending_id: int = 0, lang: str = "en") -> list[Reply]:
    # Language change interactive buttons
    if action == "cmd_lang_ui":
        buttons = [
            ("set_ui_en", "English"),
            ("set_ui_pcm", "Nigerian Pidgin"),
            ("set_ui_yo", "Yorùbá"),
            ("set_ui_ha", "Hausa"),
            ("set_ui_ig", "Ìgbò"),
        ]
        return [Reply("Select your preferred UI language:", buttons=buttons)]

    if action == "cmd_lang_speech":
        buttons = [
            ("set_speech_en", "English"),
            ("set_speech_pcm", "Nigerian Pidgin"),
            ("set_speech_yo", "Yorùbá"),
            ("set_speech_ha", "Hausa"),
            ("set_speech_ig", "Ìgbò"),
        ]
        return [Reply("Select your preferred Voice language:", buttons=buttons)]

    if action.startswith("set_ui_"):
        code = action.replace("set_ui_", "")
        user = session.get(User, user_id)
        if user:
            user.language = code
            session.commit()
        from config.languages import get_language
        return [Reply(f"UI language updated to {get_language(code)['name']}.")]

    if action.startswith("set_speech_"):
        code = action.replace("set_speech_", "")
        user = session.get(User, user_id)
        if user:
            st = dict(user.state_data or {})
            st["speech_language"] = code
            user.state_data = st
            session.commit()
        from config.languages import get_language
        return [Reply(f"Voice language updated to {get_language(code)['name']}.")]

    # Staff transaction approval buttons
    if action == "approve_tx":
        from app.services import staff
        tx = staff.approve_transaction(session, business_id, pending_id, user_id)
        return [Reply(f"Sale of {fmt_naira(tx.total_kobo)} approved and added to ledger.")]
    if action == "reject_tx":
        from app.services import staff
        tx = staff.reject_transaction(session, business_id, pending_id, user_id)
        return [Reply(f"Sale of {fmt_naira(tx.total_kobo)} rejected.")]
    if action == "audit_approve_all":
        from app.services import staff
        count, total_kobo = staff.reconcile_all_daily_transactions(session, business_id, user_id)
        return [Reply(
            f"Daily audit approved!\n\nAll {count} attendant transactions for today (total {fmt_naira(total_kobo)} cash collected) have been verified and reconciled."
        )]

    # Quick tap command button actions from /commands and native menu
    if action == "cmd_receipt":
        return handle_button(session, business_id, user_id, "get_receipt", lang=lang)

    if action == "cmd_invoice":
        return handle_button(session, business_id, user_id, "get_invoice", lang=lang)

    if action == "cmd_report":
        from app.services import reports
        ans = reports.run_query(session, business_id, "profit_period", {"period": "today"}, lang, today=None)
        return [Reply(ans)]

    if action == "cmd_debtors":
        from app.services import reports
        ans = reports.run_query(session, business_id, "top_debtor", {}, lang, today=None)
        return [Reply(ans)]

    if action == "cmd_stock":
        from app.models import StockItem
        biz = session.get(Business, business_id)
        if biz and not plans.allowed(biz, "stock_tracking"):
            return [Reply("Stock tracking is available on Trader and Business plans. Type /plan to upgrade.")]
        items = session.scalars(
            select(StockItem).where(StockItem.business_id == business_id).order_by(StockItem.name.asc())
        ).all()
        if not items:
            return [Reply(
                f"No items in {biz.name if biz else 'shop'}'s inventory yet.\n\n"
                f"To add items:\n"
                f"- /add_item <Product Name> [unit]\n"
                f"- /restock <Product Name> <Quantity>\n"
                f"- Or manage inventory on your web dashboard: /dashboard"
            )]
        lines = [f"INVENTORY - {biz.name if biz else 'Shop'}\n"]
        for it in items:
            unit_str = f" {it.base_unit}" if it.base_unit else ""
            status_str = "In Stock"
            if it.qty < 0:
                status_str = "Negative Stock"
            elif it.low_at is not None and it.qty <= it.low_at:
                status_str = "Low Stock"
            elif it.qty == 0:
                status_str = "Out of Stock"
            cost_str = f" (Avg cost: {fmt_naira(it.avg_cost_kobo)})" if it.avg_cost_kobo > 0 else ""
            lines.append(f"- {it.name}: {it.qty:g}{unit_str} [{status_str}]{cost_str}")
        lines.append("\nTo add more stock: /restock <item> <qty>\nTo register a new item: /add_item <name>")
        return [Reply("\n".join(lines))]

    if action == "cmd_audit":
        from app.services import staff
        biz = session.get(Business, business_id)
        if not staff.is_owner(session, business_id, user_id):
            return [Reply("Access restricted: Daily staff audits are only available to the shop owner.")]
        audit_data = staff.get_daily_staff_audit(session, business_id)
        report_text = staff.format_daily_audit_text(session, audit_data, biz.name if biz else "Shop")
        btns = []
        if audit_data.get("pending_reconcile_count", 0) > 0:
            cash_fmt = fmt_naira(audit_data.get("total_cash_kobo", 0))
            btns.append(("audit_approve_all", f"Approve & Reconcile All ({cash_fmt})"))
        return [Reply(report_text, buttons=btns)]

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

    if action == "send_receipt" or action.startswith("get_receipt"):
        tx = None
        if action.startswith("get_receipt:"):
            try:
                tx_id = int(action.split(":", 1)[1])
                tx = session.get(Transaction, tx_id)
            except ValueError:
                pass
        if tx is None:
            tx = ledger.get_last_transaction(session, business_id, user_id=user_id)
        if tx is None:
            return [Reply("No recent sale found to create receipt.")]

        doc = session.scalar(
            select(Document).where(
                Document.business_id == business_id,
                Document.transaction_id == tx.id,
                Document.status == "issued",
            )
        )
        if doc is None:
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

        download_url = f"{settings.base_url.rstrip('/')}/d/receipt/{doc.number}"
        cust = session.get(Customer, tx.customer_id) if tx.customer_id else None
        cust_name = cust.name if cust else "customer"
        biz = session.get(Business, business_id)

        user = session.get(User, user_id)
        if user:
            st = dict(user.state_data or {})
            st["pending_receipt_doc_id"] = doc.id
            st["pending_receipt_cust_id"] = tx.customer_id
            user.state_data = st
            session.commit()

        replies = [
            Reply(
                text=f"Receipt #{doc.number} issued ({fmt_naira(doc.total_kobo)})\nDownload PDF: {download_url}",
                document_path=doc.file_path,
                document_filename=f"{doc.number}.pdf",
            )
        ]

        if cust and cust.phone:
            wa_msg = f"Hello {cust.name}, here is your receipt #{doc.number} from {biz.name if biz else 'Shop'} for {fmt_naira(doc.total_kobo)}: {download_url}"
            wa_link = documents.wa_me_link(cust.phone, wa_msg)
            replies.append(Reply(
                text=(
                    f"Forward receipt to {cust.name} on WhatsApp:\n{wa_link}\n\n"
                    f"To send to a different number, reply with customer's phone number, or tap [Done]."
                ),
                buttons=[(f"skip_receipt:{doc.id}", "Done")],
            ))
        else:
            replies.append(Reply(
                text=(
                    f"Send receipt to {cust_name} on WhatsApp?\n"
                    f"Reply with customer's phone number (e.g. 08012345678) or tap [Skip]."
                ),
                buttons=[(f"skip_receipt:{doc.id}", "Skip")],
            ))
        return replies

    if action == "send_invoice" or action.startswith("get_invoice"):
        tx = None
        doc = None
        if action.startswith("get_invoice_tx:"):
            try:
                tx_id = int(action.split(":", 1)[1])
                tx = session.get(Transaction, tx_id)
            except ValueError:
                pass
        elif action.startswith("get_invoice:"):
            try:
                doc_id = int(action.split(":", 1)[1])
                doc = session.get(Document, doc_id)
            except ValueError:
                pass

        if doc is None and tx is not None:
            doc = session.scalar(
                select(Document).where(
                    Document.business_id == business_id,
                    Document.transaction_id == tx.id,
                    Document.kind == "invoice",
                    Document.status == "issued",
                )
            )
            if doc is None:
                doc = documents.issue_document(
                    session=session,
                    business_id=business_id,
                    kind="invoice",
                    transaction_id=tx.id,
                    customer_id=tx.customer_id,
                    items=tx.items_json or [],
                    total_kobo=tx.total_kobo,
                    paid_kobo=tx.paid_kobo,
                )

        if doc is None:
            tx = ledger.get_last_transaction(session, business_id, user_id=user_id)
            if tx:
                doc = session.scalar(
                    select(Document).where(
                        Document.business_id == business_id,
                        Document.transaction_id == tx.id,
                        Document.kind == "invoice",
                    )
                )
                if doc is None:
                    doc = documents.issue_document(
                        session=session,
                        business_id=business_id,
                        kind="invoice",
                        transaction_id=tx.id,
                        customer_id=tx.customer_id,
                        items=tx.items_json or [],
                        total_kobo=tx.total_kobo,
                        paid_kobo=tx.paid_kobo,
                    )

        if doc is None:
            doc = session.scalar(
                select(Document).where(
                    Document.business_id == business_id,
                    Document.kind == "invoice",
                    Document.status == "issued",
                ).order_by(Document.id.desc())
            )

        if doc is None:
            return [Reply(
                "No invoice found yet.\n\n"
                "To create a new invoice, type e.g.:\n"
                "Invoice 5 bags of rice for 200,000 for Mr Bolu\n"
                "or manage invoices on your dashboard: /dashboard"
            )]

        download_url = f"{settings.base_url.rstrip('/')}/d/invoice/{doc.number}"
        cust = session.get(Customer, doc.customer_id) if doc.customer_id else None
        cust_name = cust.name if cust else "customer"
        biz = session.get(Business, business_id)

        user = session.get(User, user_id)
        if user:
            st = dict(user.state_data or {})
            st["pending_receipt_doc_id"] = doc.id
            st["pending_receipt_cust_id"] = doc.customer_id
            user.state_data = st
            session.commit()

        replies = [
            Reply(
                text=f"Invoice #{doc.number} issued ({fmt_naira(doc.total_kobo)})\nDownload PDF: {download_url}",
                document_path=doc.file_path,
                document_filename=f"{doc.number}.pdf",
            ),
        ]
        if cust and cust.phone:
            wa_msg = f"Hello {cust.name}, here is your invoice #{doc.number} from {biz.name if biz else 'Shop'} for {fmt_naira(doc.total_kobo)}: {download_url}"
            wa_link = documents.wa_me_link(cust.phone, wa_msg)
            replies.append(Reply(
                text=(
                    f"Forward invoice to {cust.name} on WhatsApp:\n{wa_link}\n\n"
                    f"To send to a different number, reply with customer's phone number, or tap [Done]."
                ),
                buttons=[(f"skip_receipt:{doc.id}", "Done")],
            ))
        else:
            replies.append(Reply(
                text=(
                    f"Send invoice to {cust_name} on WhatsApp?\n"
                    f"Reply with customer's phone number (e.g. 08012345678) or tap [Skip]."
                ),
                buttons=[(f"skip_receipt:{doc.id}", "Skip")],
            ))
        return replies

    if action.startswith("skip_receipt"):
        user = session.get(User, user_id)
        if user:
            st = dict(user.state_data or {})
            st.pop("pending_receipt_doc_id", None)
            st.pop("pending_receipt_cust_id", None)
            user.state_data = st
            session.commit()
        return [Reply("Ready. You can record another transaction or type /dashboard.")]

    try:
        p = pending.get_for_business(session, business_id, pending_id)
        if action in {"save", "issue"}:
            if p.kind.startswith("issue_"):
                data = p.payload
                tx_id = data.get("transaction_id")
                if not tx_id and data.get("items"):
                    from app.models import Transaction, now_iso
                    tx = Transaction(
                        business_id=business_id,
                        customer_id=data.get("customer_id"),
                        kind="sale",
                        items_json=data.get("items"),
                        total_kobo=data["total_kobo"],
                        paid_kobo=data.get("paid_kobo", 0),
                        due_date=data.get("due_date"),
                        status="confirmed",
                        created_by_user_id=user_id,
                        created_at=now_iso(),
                    )
                    session.add(tx)
                    session.flush()
                    tx_id = tx.id

                doc = documents.issue_document(
                    session=session,
                    business_id=business_id,
                    kind=data["kind"],
                    transaction_id=tx_id,
                    customer_id=data.get("customer_id"),
                    items=data.get("items"),
                    total_kobo=data["total_kobo"],
                    paid_kobo=data["paid_kobo"],
                    due_date=data.get("due_date"),
                )
                p.status = "applied"
                session.commit()
                cust = session.get(Customer, data.get("customer_id")) if data.get("customer_id") else None
                cust_name = cust.name if cust else "customer"
                biz = session.get(Business, business_id)
                download_url = f"{settings.base_url.rstrip('/')}/d/{doc.kind}/{doc.number}"

                user = session.get(User, user_id)
                if user:
                    st = dict(user.state_data or {})
                    st["pending_receipt_doc_id"] = doc.id
                    st["pending_receipt_cust_id"] = data.get("customer_id")
                    user.state_data = st
                    session.commit()

                wa_msg = f"Here is your {doc.kind} #{doc.number} from {biz.name if biz else 'Shop'}: {fmt_naira(doc.total_kobo)} {download_url}"
                link = documents.wa_me_link(cust.phone if cust else None, wa_msg)
                msg_text = (
                    f"{doc.kind.title()} #{doc.number} issued ({fmt_naira(doc.total_kobo)}).\n"
                    f"Download PDF: {download_url}\n"
                    f"Forward link: {link}"
                )
                return [Reply(
                    text=msg_text,
                    document_path=doc.file_path,
                    document_filename=f"{doc.number}.pdf",
                )]
            if p.kind == "correction":
                tx = ledger.confirm_correction(session, business_id, pending_id)
                return [Reply(_saved_text(session, business_id, tx, lang))]
            tx = ledger.confirm(session, business_id, pending_id)
            saved_msg = _saved_text(session, business_id, tx, lang)
            buttons = []
            if tx.kind == "sale":
                buttons.append((f"get_receipt:{tx.id}", "Download Receipt"))
                if tx.total_kobo > tx.paid_kobo:
                    buttons.append((f"get_invoice_tx:{tx.id}", "Download Invoice"))
            return [Reply(saved_msg, buttons=buttons)]
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
