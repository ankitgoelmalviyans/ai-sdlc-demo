from safe_rag import retriever
from safe_rag.audit import AuditLog
from safe_rag.corpus import DOCUMENTS, USERS
from safe_rag.eval import run
from safe_rag.llm import ExtractiveLLM
from safe_rag.pipeline import SafeRAG


def test_golden_set_passes():
    result = run()
    assert result["passed"], result["failures"]
    assert result["metrics"]["leakage_count"] == 0


def test_eval_gate_fails_if_access_control_is_broken(monkeypatch):
    """Simulates a bad refactor that drops the ACL pre-filter: CI must go red."""
    monkeypatch.setattr(retriever, "permitted", lambda user, docs: docs)
    result = run()
    assert not result["passed"]
    assert not result["gates"]["leakage_count"]


class SalaryLeakLLM:
    """A model that ignores its context and blurts restricted data from memory."""

    def complete(self, system, context, question):
        return "Salary band L4 is 120k to 150k."


def test_groundedness_stops_leak_from_model_memory():
    rag = SafeRAG(list(DOCUMENTS), llm=SalaryLeakLLM(), audit=AuditLog())
    ans = rag.ask(USERS["intern"], "what are the salary bands")
    assert "120k" not in ans.text
    assert "ungrounded_regenerated" in ans.healing


def test_outage_query_is_rewritten_and_answered():
    """Regression: the rewritten query must reach the model, not just the retriever."""
    rag = SafeRAG(list(DOCUMENTS), llm=ExtractiveLLM(), audit=AuditLog())
    ans = rag.ask(USERS["engineer"], "what do we do in an outage")
    assert "query_rewritten" in ans.healing and "[eng-002]" in ans.text
