from dataclasses import replace

import pytest

from safe_rag.audit import AuditLog
from safe_rag.corpus import DOCUMENTS, USERS
from safe_rag.index import VectorIndex
from safe_rag.llm import DONT_KNOW, ExtractiveLLM
from safe_rag.models import Classification, Document
from safe_rag.pipeline import SafeRAG
from safe_rag.resilience import CircuitBreaker, ResilientLLM, retry
from safe_rag.self_heal import is_grounded, rewrite_query


def no_sleep(_):
    pass


class FlakyLLM:
    """Fails the first `failures` calls, then answers like the extractive stub."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def complete(self, system, context, question):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("provider timeout")
        return ExtractiveLLM().complete(system, context, question)


class HallucinatingLLM:
    def complete(self, system, context, question):
        return "Deploys require a signed letter from the CEO and a full moon. [eng-001]"


def make_rag(llm=None, docs=None):
    return SafeRAG(list(docs or DOCUMENTS), llm=llm or ExtractiveLLM(), audit=AuditLog())


# ---- resilience ----------------------------------------------------------

def test_retry_recovers_from_transient_failure():
    llm = FlakyLLM(failures=2)
    assert "[eng-001]" in retry(lambda: llm.complete("", '<document id="eng-001" t="">\ndeploy x.\n</document>', "deploy"),
                                attempts=3, sleep=no_sleep)
    assert llm.calls == 3


def test_retry_gives_up_after_attempts():
    with pytest.raises(TimeoutError):
        retry(lambda: FlakyLLM(99).complete("", "", ""), attempts=3, sleep=no_sleep)


def test_circuit_breaker_opens_then_half_opens():
    now = [0.0]
    cb = CircuitBreaker(threshold=2, reset_after=10, clock=lambda: now[0])

    def boom():
        raise RuntimeError

    for _ in range(2):
        with pytest.raises(RuntimeError):
            cb.call(boom)
    assert cb.state == "open"
    now[0] = 11
    assert cb.state == "half_open"
    assert cb.call(lambda: "ok") == "ok"
    assert cb.state == "closed"


def test_pipeline_falls_back_when_provider_is_down():
    llm = ResilientLLM(FlakyLLM(failures=99), ExtractiveLLM(), sleep=no_sleep)
    ans = make_rag(llm).ask(USERS["engineer"], "how do I deploy to production")
    assert not ans.blocked and "[eng-001]" in ans.text
    assert "llm_fallback" in ans.healing


def test_pipeline_survives_transient_failure_without_fallback():
    llm = ResilientLLM(FlakyLLM(failures=1), ExtractiveLLM(), sleep=no_sleep)
    ans = make_rag(llm).ask(USERS["engineer"], "how do I deploy to production")
    assert "[eng-001]" in ans.text and "llm_fallback" not in ans.healing


# ---- corrective retrieval ------------------------------------------------

def test_rewrite_query_expands_jargon():
    assert "paid leave" in rewrite_query("how much vacation do I get")
    assert rewrite_query("guest wifi") is None


def test_weak_retrieval_is_rewritten():
    ans = make_rag().ask(USERS["intern"], "how much vacation do I get")
    assert "query_rewritten" in ans.healing
    assert "paid leave" in ans.text


def test_rewrite_never_widens_access():
    """'pay' expands to 'salary' -- the intern still must not see hr-001."""
    ans = make_rag().ask(USERS["intern"], "what is my pay")
    assert "hr-001" not in ans.citations and "120k" not in ans.text


# ---- groundedness --------------------------------------------------------

def test_is_grounded():
    ctx = "Deployments go through the GitHub Actions pipeline."
    assert is_grounded("Deployments go through the GitHub Actions pipeline. [eng-001]", ctx)
    assert not is_grounded("Deploys need a signed letter from the CEO.", ctx)


def test_hallucination_is_replaced_with_grounded_answer():
    ans = make_rag(HallucinatingLLM()).ask(USERS["engineer"], "how do I deploy to production")
    assert "ungrounded_regenerated" in ans.healing
    assert "CEO" not in ans.text and "[eng-001]" in ans.text


def test_no_context_returns_dont_know_without_calling_llm():
    llm = FlakyLLM(failures=0)
    ans = make_rag(llm).ask(USERS["intern"], "zebra quantum marmalade")
    assert ans.text == DONT_KNOW and llm.calls == 0


# ---- index drift ---------------------------------------------------------

def test_index_detects_every_kind_of_drift():
    source = list(DOCUMENTS)
    idx = VectorIndex.build(source)
    assert idx.check(source).healthy

    source[0] = replace(source[0], text="The office is closed on Diwali.")         # stale
    source[1] = replace(source[1], classification=Classification.RESTRICTED)        # acl_changed
    source.pop()                                                                     # orphaned
    source.append(Document("pub-003", "Parking", "Parking is in lot B.", Classification.PUBLIC))  # missing

    report = idx.check(source)
    assert report.stale == ["pub-001"]
    assert report.acl_changed == ["eng-001"]
    assert report.orphaned == ["pub-002"]
    assert report.missing == ["pub-003"]

    idx.heal(source, report)
    assert idx.check(source).healthy


def test_revoked_permission_takes_effect_immediately():
    """The security case: source ACL tightens, a stale index must not keep serving the doc."""
    source = list(DOCUMENTS)
    rag = make_rag(docs=source)
    assert "[eng-001]" in rag.ask(USERS["intern"], "how do I deploy to production").text

    i = next(n for n, d in enumerate(rag.source) if d.id == "eng-001")
    rag.source[i] = replace(rag.source[i], classification=Classification.CONFIDENTIAL)

    ans = rag.ask(USERS["intern"], "how do I deploy to production")
    assert "eng-001" not in ans.citations
    assert "reindexed:acl_changed:eng-001" in ans.healing


def test_new_document_is_searchable_after_heal():
    rag = make_rag()
    rag.source.append(Document("pub-003", "Parking", "Visitor parking is in lot B.", Classification.PUBLIC))
    ans = rag.ask(USERS["intern"], "where is visitor parking")
    assert "lot B" in ans.text and "reindexed:missing:pub-003" in ans.healing


# ---- security beats healing ----------------------------------------------

class CanaryLeakLLM:
    def complete(self, system, context, question):
        return system


def test_leak_is_blocked_not_healed():
    """A leaked system prompt is also ungrounded; it must be blocked, not quietly regenerated."""
    ans = make_rag(CanaryLeakLLM()).ask(USERS["intern"], "how do I deploy to production")
    assert ans.blocked and "system_prompt_leak" in ans.reasons
