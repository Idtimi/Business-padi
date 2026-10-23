import json

from app.money import to_kobo
from app.pipeline.llm import FIXTURES, MockLLM
from app.pipeline.schemas import ExtractionResult
from app.pipeline.validate import validate


def run(transcript: str):
    raw = MockLLM().complete_json("", transcript, "extraction")
    return [validate(t) for t in ExtractionResult.model_validate(raw).transactions]


def test_rice_sale_totals_in_code():
    v = run("I sell three bag of rice to Mama Ngozi, forty-two thousand five hundred each. "
            "She pay fifty thousand, the rest she go pay Friday.")[0]
    assert v.total_kobo == to_kobo(127_500)
    assert v.paid_kobo == to_kobo(50_000)
    assert v.balance_kobo == to_kobo(77_500)
    assert v.customer == "Mama Ngozi"
    assert v.due_date == "2026-10-30"
    assert not v.problems


def test_expense_uses_amount():
    v = run("Today I buy fuel for generator, five thousand naira.")[0]
    assert v.total_kobo == to_kobo(5000)
    assert v.category == "fuel"


def test_cash_sale_is_fully_paid():
    first = run("Two sales: five bags beans to Chika cash, 30k each, and one bag rice to Bayo on credit, 43k.")
    assert first[0].total_kobo == to_kobo(150_000)
    assert first[0].balance_kobo == 0
    assert first[1].total_kobo == to_kobo(43_000)
    assert first[1].balance_kobo == to_kobo(43_000)


def test_missing_quantity_asks_question():
    v = run("I sell rice give Tunde.")[0]
    assert v.problems


def test_overpayment_is_flagged():
    from app.pipeline.schemas import Extracted, Item
    x = Extracted(intent="sale", customer="a", payment="partial", amount_paid=999,
                  items=[Item(name="x", qty=1, unit_price=100)])
    assert validate(x).problems


def test_llm_cannot_set_totals():
    """Even if the AI sends a wrong line_total, code recomputes qty x price."""
    from app.pipeline.schemas import Extracted, Item
    x = Extracted(intent="sale", payment="cash",
                  items=[Item(name="x", qty=3, unit_price=100, line_total=1)])
    assert validate(x).total_kobo == to_kobo(300)


def test_all_fixtures_validate_without_crashing():
    rows = [json.loads(l) for l in FIXTURES.read_text(encoding="utf-8").splitlines() if l.strip()]
    for row in rows:
        for v in run(row["transcript"]):
            assert v.intent