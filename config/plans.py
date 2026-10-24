"""Plan definitions, entitlements, caps, and pricing matrix.

Per Part A and Part M of the Business Padi specification.
All monetary amounts are integer kobo.
"""

from dataclasses import dataclass
from typing import Any

from app.money import to_kobo


@dataclass(frozen=True)
class PlanDefinition:
    key: str
    name: str
    price_kobo: int
    monthly_voice_entries: int
    extra_packs_allowed: bool
    credit_book: bool
    summaries: list[str]  # e.g. ["weekly"], ["daily", "weekly"]
    receipts: str  # "basic", "branded", "custom"
    invoices: bool
    quotations: bool
    statements: bool
    debt_reminders: str  # "manual", "scheduled", "smart"
    stock_tracking: str | bool  # False, "basic", "full"
    reports: list[str]  # ["weekly_summary"], etc.
    staff_accounts: int
    financial_profile: str | bool  # False, "basic", "full", "api"


PLANS: dict[str, PlanDefinition] = {
    "starter": PlanDefinition(
        key="starter",
        name="Starter",
        price_kobo=0,
        monthly_voice_entries=20,
        extra_packs_allowed=True,
        credit_book=True,
        summaries=["weekly"],
        receipts="basic",
        invoices=False,
        quotations=False,
        statements=False,
        debt_reminders="manual",
        stock_tracking=False,
        reports=["weekly_summary"],
        staff_accounts=0,
        financial_profile=False,
    ),
    "solo": PlanDefinition(
        key="solo",
        name="Solo Trader",
        price_kobo=to_kobo(2_500),
        monthly_voice_entries=50,
        extra_packs_allowed=True,
        credit_book=True,
        summaries=["weekly", "daily"],
        receipts="branded",
        invoices=True,
        quotations=False,
        statements=True,
        debt_reminders="scheduled",
        stock_tracking="basic",
        reports=["weekly_summary"],
        staff_accounts=0,
        financial_profile="basic",
    ),
    "trader": PlanDefinition(
        key="trader",
        name="Trader",
        price_kobo=to_kobo(5_000),
        monthly_voice_entries=100,
        extra_packs_allowed=True,
        credit_book=True,
        summaries=["daily", "weekly"],
        receipts="branded",
        invoices=True,
        quotations=True,
        statements=True,
        debt_reminders="scheduled",
        stock_tracking="basic",
        reports=["weekly_summary", "expense_categories"],
        staff_accounts=0,
        financial_profile="basic",
    ),
    "business": PlanDefinition(
        key="business",
        name="Business",
        price_kobo=to_kobo(15_000),
        monthly_voice_entries=300,
        extra_packs_allowed=True,
        credit_book=True,
        summaries=["daily", "weekly"],
        receipts="custom",
        invoices=True,
        quotations=True,
        statements=True,
        debt_reminders="smart",
        stock_tracking="full",
        reports=["weekly_summary", "expense_categories", "pnl", "trends", "exports"],
        staff_accounts=3,
        financial_profile="full",
    ),
    "partner": PlanDefinition(
        key="partner",
        name="Partner",
        price_kobo=0,
        monthly_voice_entries=999_999,
        extra_packs_allowed=True,
        credit_book=True,
        summaries=["daily", "weekly"],
        receipts="custom",
        invoices=True,
        quotations=True,
        statements=True,
        debt_reminders="smart",
        stock_tracking="full",
        reports=["weekly_summary", "expense_categories", "pnl", "trends", "exports", "consented_insights"],
        staff_accounts=999,
        financial_profile="api",
    ),
}

# Entry pack constants (Part M)
ENTRY_PACK_COUNT = 25
ENTRY_PACK_PRICE_KOBO = to_kobo(2_500)  # 2,500 NGN for 25 entries

DEFAULT_PLAN = "starter"


def get_plan(key: str | None) -> PlanDefinition:
    """Retrieve plan definition by name, defaulting to starter."""
    return PLANS.get((key or "").lower(), PLANS[DEFAULT_PLAN])
