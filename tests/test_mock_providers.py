import json

from app.pipeline.asr import MockASR
from app.pipeline.llm import FIXTURES, MockLLM
from app.pipeline.schemas import ExtractionResult


def test_all_12_fixtures_load_and_validate():
    llm = MockLLM()
    rows = [json.loads(l) for l in FIXTURES.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) == 12
    for row in rows:
        raw = llm.complete_json("", row["transcript"], "extraction")
        result = ExtractionResult.model_validate(raw)
        assert len(result.transactions) >= 1


def test_two_sales_in_one_note():
    llm = MockLLM()
    text = "Two sales: five bags beans to Chika cash, 30k each, and one bag rice to Bayo on credit, 43k."
    result = ExtractionResult.model_validate(llm.complete_json("", text, "extraction"))
    assert [t.customer for t in result.transactions] == ["Chika", "Bayo"]


def test_unknown_sentence_asks_to_repeat():
    result = ExtractionResult.model_validate(MockLLM().complete_json("", "blah blah", "extraction"))
    assert result.transactions[0].needs_clarification


def test_mock_asr_uses_filename(tmp_path):
    asr = MockASR()
    out = asr.transcribe(str(tmp_path / "hello.wav"), "pcm", [])
    assert out.text == "hello"