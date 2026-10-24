from dataclasses import dataclass, field

from app.money import to_kobo
from app.pipeline.schemas import Extracted

MAX_AMOUNT_KOBO = to_kobo(100_000_000)  # N100 million: anything above is "absurd"


@dataclass
class Line:
    name: str | None
    qty: float | None
    unit: str | None
    unit_price_kobo: int | None
    line_total_kobo: int


@dataclass
class Validated:
    intent: str
    customer: str | None = None
    supplier: str | None = None
    lines: list[Line] = field(default_factory=list)
    total_kobo: int = 0
    paid_kobo: int = 0
    due_date: str | None = None
    category: str | None = None
    payment: str | None = None
    query: dict | None = None
    correction: dict | None = None
    problems: list[str] = field(default_factory=list)  # questions to ask the trader

    @property
    def balance_kobo(self) -> int:
        return self.total_kobo - self.paid_kobo


def clean_name(name: str | None) -> str | None:
    """Trim spaces and Title Case, e.g. ' mama ngozi ' -> 'Mama Ngozi'."""
    if not name or not name.strip():
        return None
    return " ".join(name.split()).title()


def _build_line(item) -> Line | None:
    """Work out one line's total in kobo. Returns None if we cannot."""
    price = to_kobo(item.unit_price) if item.unit_price is not None else None
    if item.qty is not None and price is not None:
        total = round(item.qty * price)
    elif item.line_total is not None:
        total = to_kobo(item.line_total)
        if price is None and item.qty:
            price = round(total / item.qty)
    else:
        return None
    return Line(item.name, item.qty, item.unit, price, total)


def validate(x: Extracted) -> Validated:
    v = Validated(
        intent=x.intent,
        customer=clean_name(x.customer),
        supplier=clean_name(x.supplier),
        due_date=x.due_date,
        category=x.category,
        payment=x.payment,
        query=x.query,
        correction=x.correction,
    )

    if x.needs_clarification:
        v.problems.append(x.needs_clarification)
        return v

    # Pass through query, correction, chitchat without demanding line items
    if x.intent in {"query", "correction", "chitchat", "unknown"}:
        return v

    # If receipt or invoice without items/amount, it refers to the last transaction
    if x.intent in {"receipt", "invoice"} and not x.items and x.amount is None:
        return v

    # Total: sum of lines, or the single amount if there are no items
    for item in x.items:
        line = _build_line(item)
        if line is None:
            v.problems.append(f"How much is {item.name or 'the item'}? Abeg tell me qty and price.")
            return v
        v.lines.append(line)
    if v.lines:
        v.total_kobo = sum(l.line_total_kobo for l in v.lines)
    elif x.amount is not None:
        v.total_kobo = to_kobo(x.amount)

    # Reject zero, negative or absurd amounts
    needs_amount = x.intent in {"sale", "expense", "payment_received", "restock", "invoice", "receipt"}
    if needs_amount and v.total_kobo <= 0 and (x.items or x.amount is not None):
        v.problems.append("I no see the amount. How much be am?")
        return v
    if v.total_kobo > MAX_AMOUNT_KOBO:
        v.problems.append("This amount big well well. Abeg confirm am again.")
        return v

    # Amount paid: cash = all, credit = none, partial = what was said
    if x.intent in {"sale", "invoice"}:
        if x.payment == "cash":
            v.paid_kobo = v.total_kobo
        elif x.payment == "partial":
            v.paid_kobo = to_kobo(x.amount_paid or 0)
            if v.paid_kobo > v.total_kobo:
                v.problems.append("The money wey dem pay pass the total. Abeg check am.")
        elif x.payment == "credit":
            v.paid_kobo = 0
        else:
            v.paid_kobo = 0 if x.intent == "invoice" else 0
    elif x.intent in {"expense", "restock", "payment_received", "receipt"}:
        v.paid_kobo = v.total_kobo

    return v
