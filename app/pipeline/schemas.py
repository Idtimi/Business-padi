from pydantic import BaseModel, Field


class Item(BaseModel):
    name: str | None = None
    qty: float | None = None
    unit: str | None = None
    unit_price: float | None = None
    line_total: float | None = None


class Extracted(BaseModel):
    intent: str = "unknown"
    customer: str | None = None
    supplier: str | None = None
    items: list[Item] = Field(default_factory=list)
    amount: float | None = None
    amount_paid: float | None = None
    payment: str | None = None  # cash | credit | partial
    due_date: str | None = None
    category: str | None = None
    query: dict | None = None
    correction: dict | None = None
    report_period: str | None = None
    text_value: str | None = None
    needs_clarification: str | None = None


class ExtractionResult(BaseModel):
    language_detected: str = "unknown"
    transactions: list[Extracted] = Field(default_factory=list)