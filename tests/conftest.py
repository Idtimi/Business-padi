import pytest
from app.config import settings
from app.pipeline.llm import MockLLM
import app.pipeline.router as router_module
import app.pipeline.llm as llm_module

@pytest.fixture(autouse=True)
def force_mock_llm(monkeypatch):
    """Ensure all unit and acceptance tests run with MockLLM offline."""
    monkeypatch.setattr(llm_module, "get_llm", lambda: MockLLM())
    monkeypatch.setattr(router_module, "get_llm", lambda: MockLLM())
