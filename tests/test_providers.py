"""Model providers (Claude API, Microsoft Foundry, Azure OpenAI judge) tested with fake
clients: no SDK, network or key needed. What matters here is that swapping the model
never changes the security behaviour around it."""

import itertools
import sys
from types import SimpleNamespace

import pytest

from safe_rag import llm as llm_module
from safe_rag.audit import AuditLog
from safe_rag.corpus import DOCUMENTS, USERS
from safe_rag.judge import AzureOpenAIJudge, ClaudeJudge, HeuristicJudge, make_judge, parse_verdict
from safe_rag.llm import AzureOpenAILLM, ClaudeLLM, ExtractiveLLM, ModelRefusal, make_llm
from safe_rag.pipeline import SafeRAG
from safe_rag.policy import can_read
from safe_rag.resilience import CircuitBreaker, ResilientLLM

PROVIDER_ENV = ["RAG_LLM_PROVIDER", "RAG_LLM_MODEL", "ANTHROPIC_API_KEY",
                "RAG_JUDGE_PROVIDER", "RAG_JUDGE_MODEL"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def claude_message(text="", stop_reason="end_turn", thinking=True):
    blocks = [SimpleNamespace(type="thinking", thinking="")] if thinking else []
    blocks.append(SimpleNamespace(type="text", text=text))
    return SimpleNamespace(stop_reason=stop_reason, content=blocks, stop_details=None)


class FakeAnthropicClient:
    """Stands in for anthropic.Anthropic() / AnthropicFoundry(); records requests."""

    def __init__(self, reply):
        self.reply = reply          # str, a message object, or a callable(request) -> str
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        reply = self.reply(request) if callable(self.reply) else self.reply
        return reply if not isinstance(reply, str) else claude_message(reply)


class FakeAzureOpenAIClient:
    """Stands in for openai.AzureOpenAI(). `content` may be a str, a callable(request) -> str,
    or an exception to raise; `finish_reason` mimics Azure's ("stop", "content_filter")."""

    def __init__(self, content, finish_reason="stop"):
        self.content = content
        self.finish_reason = finish_reason
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **request):
        self.requests.append(request)
        if isinstance(self.content, Exception):
            raise self.content
        text = self.content(request) if callable(self.content) else self.content
        message = SimpleNamespace(content=text)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=self.finish_reason)])


class AzureContentFilterError(Exception):
    """Shape of openai.BadRequestError for a filtered prompt: .code == "content_filter"."""
    code = "content_filter"


def obedient_gpt(request):
    """A 'real' GPT that answers with the best context sentence and cites it."""
    context, question = request["messages"][1]["content"].split("\n\nQuestion: ")
    return ExtractiveLLM().complete(request["messages"][0]["content"], context, question)


def obedient_claude(request):
    """A 'real' model that answers with the first context sentence and cites it."""
    return ExtractiveLLM().complete(request["system"], request["messages"][0]["content"].split("\n\nQuestion: ")[0],
                                    request["messages"][0]["content"].split("\n\nQuestion: ")[1])


# ---- provider selection --------------------------------------------------

def test_default_is_offline_stub():
    assert isinstance(make_llm(), ExtractiveLLM)


def test_unknown_provider_is_an_error_not_a_silent_stub(monkeypatch):
    monkeypatch.setenv("RAG_LLM_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        make_llm()


def test_foundry_provider_uses_foundry_client(monkeypatch):
    fake = FakeAnthropicClient("ok")
    monkeypatch.setattr(llm_module, "foundry_client", lambda: fake)
    monkeypatch.setenv("RAG_LLM_PROVIDER", "foundry")
    monkeypatch.setenv("RAG_LLM_MODEL", "claude-opus-5-5")
    backend = make_llm()
    backend.complete("sys", "ctx", "q")
    assert fake.requests[0]["model"] == "claude-opus-5-5"   # the Foundry deployment name


# ---- Claude backend (Claude API or Foundry) ------------------------------

def test_claude_text_skips_thinking_blocks():
    """Regression: current models put a thinking block first; content[0].text would crash."""
    backend = ClaudeLLM(client=FakeAnthropicClient(claude_message("The answer. [pub-001]")), model="m")
    assert backend.complete("sys", "ctx", "q") == "The answer. [pub-001]"


def test_claude_request_keeps_system_prompt_separate():
    fake = FakeAnthropicClient("x")
    ClaudeLLM(client=fake, model="m").complete("SYSTEM RULES", "<document>..</document>", "question?")
    req = fake.requests[0]
    assert req["system"] == "SYSTEM RULES"
    assert req["messages"] == [{"role": "user", "content": "<document>..</document>\n\nQuestion: question?"}]


def test_refusal_raises():
    backend = ClaudeLLM(client=FakeAnthropicClient(claude_message(stop_reason="refusal")), model="m")
    with pytest.raises(ModelRefusal):
        backend.complete("s", "c", "q")


def test_refusal_heals_to_extractive_fallback():
    refusing = ClaudeLLM(client=FakeAnthropicClient(claude_message(stop_reason="refusal")), model="m")
    rag = SafeRAG(list(DOCUMENTS), llm=ResilientLLM(refusing, ExtractiveLLM(), sleep=lambda _: None),
                  audit=AuditLog())
    ans = rag.ask(USERS["engineer"], "how do I deploy to production")
    assert "[eng-001]" in ans.text and "llm_fallback" in ans.healing


# ---- swapping the model never weakens security ---------------------------

def test_real_backend_leaking_system_prompt_is_blocked():
    leaky = ClaudeLLM(client=FakeAnthropicClient(lambda req: req["system"]), model="m")
    ans = SafeRAG(list(DOCUMENTS), llm=leaky, audit=AuditLog()).ask(USERS["intern"], "how do I deploy to production")
    assert ans.blocked and "system_prompt_leak" in ans.reasons


def test_real_backend_only_ever_sees_permitted_documents():
    fake = FakeAnthropicClient(obedient_claude)
    rag = SafeRAG(list(DOCUMENTS), llm=ClaudeLLM(client=fake, model="m"), audit=AuditLog())
    queries = ["what are the salary bands", "who do I contact about a breach", "parental leave", "deploy"]
    for user, query in itertools.product(USERS.values(), queries):
        before = len(fake.requests)
        ans = rag.ask(user, query)
        for doc_id in ans.retrieved:
            assert can_read(user, rag.index.docs[doc_id])
        for req in fake.requests[before:]:
            sent = req["messages"][0]["content"]
            for doc in DOCUMENTS:
                if not can_read(user, doc):
                    assert f'id="{doc.id}"' not in sent, (user.id, doc.id)


# ---- GPT answers (Azure OpenAI / Foundry) ---------------------------------

def test_azure_openai_provider_builds_gpt_backend(monkeypatch):
    fake = FakeAzureOpenAIClient("ok")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(AzureOpenAI=lambda: fake))
    monkeypatch.setenv("RAG_LLM_PROVIDER", "azure_openai")
    monkeypatch.setenv("RAG_LLM_MODEL", "gpt-5")
    backend = make_llm()
    assert isinstance(backend, AzureOpenAILLM)
    backend.complete("sys", "ctx", "q")
    assert fake.requests[0]["model"] == "gpt-5"          # the deployment name


def test_gpt_request_keeps_system_prompt_separate_and_uses_defaults():
    fake = FakeAzureOpenAIClient("x")
    AzureOpenAILLM(client=fake, deployment="gpt-5").complete("SYSTEM RULES", "<document>..</document>", "q?")
    req = fake.requests[0]
    assert req["messages"] == [{"role": "system", "content": "SYSTEM RULES"},
                               {"role": "user", "content": "<document>..</document>\n\nQuestion: q?"}]
    # GPT-5-class deployments reject non-default sampling; a token cap can truncate reasoning.
    assert not {"temperature", "top_p", "max_tokens", "max_completion_tokens"} & req.keys()


@pytest.mark.parametrize("client", [
    FakeAzureOpenAIClient("", finish_reason="content_filter"),   # filtered completion
    FakeAzureOpenAIClient(AzureContentFilterError()),             # filtered prompt (HTTP 400)
])
def test_azure_content_filter_is_a_refusal(client):
    with pytest.raises(ModelRefusal):
        AzureOpenAILLM(client=client, deployment="gpt-5").complete("s", "c", "q")


def test_empty_gpt_completion_is_an_error_not_an_answer():
    with pytest.raises(ValueError):
        AzureOpenAILLM(client=FakeAzureOpenAIClient(""), deployment="gpt-5").complete("s", "c", "q")


def test_refusal_is_not_retried_and_does_not_trip_the_breaker():
    client = FakeAzureOpenAIClient("", finish_reason="content_filter")
    breaker = CircuitBreaker(threshold=1)
    resilient = ResilientLLM(AzureOpenAILLM(client=client, deployment="gpt-5"), ExtractiveLLM(),
                             breaker=breaker, sleep=lambda _: None)
    resilient.complete("s", '<document id="pub-001" title="t">\nPaid leave is 24 days.\n</document>', "paid leave")
    assert len(client.requests) == 1                     # no pointless retries
    assert breaker.state == "closed" and breaker.failures == 0
    assert resilient.events == ["llm_refused", "llm_fallback"]


def test_provider_outage_is_still_retried():
    client = FakeAzureOpenAIClient(TimeoutError("azure 503"))
    resilient = ResilientLLM(AzureOpenAILLM(client=client, deployment="gpt-5"), ExtractiveLLM(),
                             sleep=lambda _: None)
    resilient.complete("s", "c", "q")
    assert len(client.requests) == 3 and "llm_fallback" in resilient.events


def test_gpt_backend_leaking_system_prompt_is_blocked():
    leaky = AzureOpenAILLM(client=FakeAzureOpenAIClient(lambda r: r["messages"][0]["content"]), deployment="gpt-5")
    ans = SafeRAG(list(DOCUMENTS), llm=leaky, audit=AuditLog()).ask(USERS["intern"], "how do I deploy to production")
    assert ans.blocked and "system_prompt_leak" in ans.reasons


def test_gpt_backend_only_ever_sees_permitted_documents():
    fake = FakeAzureOpenAIClient(obedient_gpt)
    rag = SafeRAG(list(DOCUMENTS), llm=AzureOpenAILLM(client=fake, deployment="gpt-5"), audit=AuditLog())
    for user, query in itertools.product(USERS.values(), ["salary bands", "security breach", "parental leave", "deploy"]):
        before = len(fake.requests)
        rag.ask(user, query)
        for req in fake.requests[before:]:
            for doc in DOCUMENTS:
                if not can_read(user, doc):
                    assert f'id="{doc.id}"' not in req["messages"][1]["content"], (user.id, doc.id)


def test_gpt_answers_pass_the_golden_set():
    from safe_rag.eval import run

    result = run(llm=AzureOpenAILLM(client=FakeAzureOpenAIClient(obedient_gpt), deployment="gpt-5"))
    assert result["passed"], result["failures"]


# ---- judges ----------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '{"faithfulness": 5, "relevance": 4, "reason": "ok"}',
    'Here is my grade:\n```json\n{"faithfulness": 5, "relevance": 4, "reason": "ok"}\n```',
])
def test_parse_verdict_accepts_wrapped_json(text):
    v = parse_verdict(text)
    assert (v.faithfulness, v.relevance) == (5, 4)


def test_parse_verdict_rejects_prose():
    with pytest.raises(ValueError):
        parse_verdict("Looks good to me!")


def test_azure_openai_judge_requests_json_from_deployment():
    fake = FakeAzureOpenAIClient('{"faithfulness": 1, "relevance": 5, "reason": "invented bonus"}')
    v = AzureOpenAIJudge(client=fake, deployment="gpt-5").grade("q", "ctx", "answer")
    req = fake.requests[0]
    assert req["model"] == "gpt-5" and req["response_format"] == {"type": "json_object"}
    assert req["messages"][0]["role"] == "system" and "<answer>answer</answer>" in req["messages"][1]["content"]
    assert not v.good


def test_claude_judge_skips_thinking_blocks():
    fake = FakeAnthropicClient(claude_message('{"faithfulness": 5, "relevance": 5}'))
    assert ClaudeJudge(client=fake, model="m").grade("q", "c", "a").good


def test_judge_default_is_heuristic():
    assert isinstance(make_judge(), HeuristicJudge)


def test_unknown_judge_provider_is_an_error(monkeypatch):
    monkeypatch.setenv("RAG_JUDGE_PROVIDER", "bogus")
    with pytest.raises(ValueError):
        make_judge()
