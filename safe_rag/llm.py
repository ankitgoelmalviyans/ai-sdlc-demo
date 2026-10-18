"""LLM backends.

`ExtractiveLLM` is a deterministic stand-in so the demo and the tests run
offline with zero API keys. Real models plug in behind the same
`complete(system, context, question)` interface:

    RAG_LLM_PROVIDER   backend                       credentials (read by the SDK)
    ----------------   ---------------------------   -----------------------------------------
    stub (default)     ExtractiveLLM, offline        none
    anthropic          Claude via the Claude API     ANTHROPIC_API_KEY
    foundry            Claude on Microsoft Foundry   ANTHROPIC_FOUNDRY_API_KEY + ANTHROPIC_FOUNDRY_RESOURCE
                       (Azure)                       (or keyless Entra ID, see docs/06-azure-models.md)
    azure_openai       a GPT deployment on Azure     AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY
                       OpenAI / Foundry (e.g. gpt-5)  + OPENAI_API_VERSION

`RAG_LLM_MODEL` names the model; on Foundry / Azure OpenAI it is your deployment name.
The SDKs are optional dependencies (requirements-live.txt) and are only
imported when a real backend is selected.
"""

import os
import re
from typing import Protocol

from .retriever import STOPWORDS

_DOC = re.compile(r'<document id="([^"]+)"[^>]*>\n(.*?)\n</document>', re.DOTALL)
_WORD = re.compile(r"[a-z0-9]+")

DONT_KNOW = "I don't know based on the documents you can access."
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"


class LLM(Protocol):
    def complete(self, system: str, context: str, question: str) -> str: ...


class ModelRefusal(Exception):
    """The model or the provider's safety system declined: Claude's stop_reason
    "refusal" or an Azure content-filter block. ResilientLLM doesn't retry it and
    falls back to the extractive answerer, which only quotes permitted documents."""


class ExtractiveLLM:
    """Picks the context sentence with the most word overlap and cites it."""

    def complete(self, system: str, context: str, question: str) -> str:
        q = set(_WORD.findall(question.lower())) - STOPWORDS
        best, best_id, best_score = None, None, 0
        for doc_id, body in _DOC.findall(context):
            for sentence in re.split(r"(?<=[.!?])\s+", body):
                score = len(q & set(_WORD.findall(sentence.lower())))
                if score > best_score:
                    best, best_id, best_score = sentence, doc_id, score
        if best is None:
            return DONT_KNOW
        return f"{best} [{best_id}]"


def message_text(msg) -> str:
    """Join the text blocks of a Claude response. Current models think by default,
    so content[0] can be a thinking block -- never index it blindly."""
    if msg.stop_reason == "refusal":
        raise ModelRefusal(getattr(msg, "stop_details", None))
    return "".join(b.text for b in msg.content if b.type == "text").strip()


class ClaudeLLM:
    """Claude through any Anthropic SDK client: the Claude API or Microsoft Foundry."""

    def __init__(self, client=None, model: str | None = None):
        if client is None:
            import anthropic  # optional dependency

            client = anthropic.Anthropic()
        self._client = client
        self._model = model or os.environ.get("RAG_LLM_MODEL", DEFAULT_CLAUDE_MODEL)

    def complete(self, system: str, context: str, question: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=16000,
            system=system,
            messages=[{"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
        )
        return message_text(msg)


class AzureOpenAILLM:
    """A GPT deployment on Azure OpenAI or a Foundry resource's OpenAI endpoint."""

    def __init__(self, client=None, deployment: str | None = None):
        if client is None:
            from openai import AzureOpenAI  # optional dependency

            client = AzureOpenAI()
        self._client = client
        self._deployment = deployment or os.environ["RAG_LLM_MODEL"]

    def complete(self, system: str, context: str, question: str) -> str:
        # No temperature / max_tokens: GPT-5-class deployments accept only the
        # defaults, and a token cap can cut off the reasoning before the answer.
        try:
            resp = self._client.chat.completions.create(
                model=self._deployment,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": f"{context}\n\nQuestion: {question}"}],
            )
        except Exception as e:
            # Azure rejects a filtered *prompt* with HTTP 400, code "content_filter".
            if getattr(e, "code", None) == "content_filter":
                raise ModelRefusal("azure content filter (prompt)") from e
            raise
        choice = resp.choices[0]
        if choice.finish_reason == "content_filter":
            raise ModelRefusal("azure content filter (completion)")
        text = (choice.message.content or "").strip()
        if not text:
            raise ValueError(f"empty completion (finish_reason={choice.finish_reason!r})")
        return text


def foundry_client():
    """Anthropic client for Claude deployed in a Microsoft Foundry resource.
    Reads ANTHROPIC_FOUNDRY_API_KEY and ANTHROPIC_FOUNDRY_RESOURCE."""
    from anthropic import AnthropicFoundry  # optional dependency

    return AnthropicFoundry()


def make_llm(provider: str | None = None) -> LLM:
    provider = (provider or os.environ.get("RAG_LLM_PROVIDER", "")).lower()
    if not provider:
        # Backwards compatible: an Anthropic key alone selects the Claude API.
        provider = "anthropic" if os.environ.get("ANTHROPIC_API_KEY") else "stub"
    if provider == "stub":
        return ExtractiveLLM()
    if provider == "anthropic":
        return ClaudeLLM()
    if provider == "foundry":
        return ClaudeLLM(client=foundry_client())
    if provider == "azure_openai":
        return AzureOpenAILLM()
    raise ValueError(f"unknown RAG_LLM_PROVIDER {provider!r} "
                     "(use stub, anthropic, foundry or azure_openai)")


def default_llm() -> LLM:
    """What the pipeline uses when no LLM is passed in. An explicitly configured
    provider that fails to load is an error, not a silent switch to the stub."""
    if os.environ.get("RAG_LLM_PROVIDER"):
        return make_llm()
    try:
        return make_llm()
    except ImportError:
        return ExtractiveLLM()
