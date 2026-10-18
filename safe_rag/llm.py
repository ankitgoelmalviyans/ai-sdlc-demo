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

`RAG_LLM_MODEL` names the model; on Foundry it is your deployment name.
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
    """The model declined (stop_reason == "refusal"). ResilientLLM treats it like
    any other failure and falls back to the extractive answerer."""


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
    raise ValueError(f"unknown RAG_LLM_PROVIDER {provider!r} (use stub, anthropic or foundry)")


def default_llm() -> LLM:
    """What the pipeline uses when no LLM is passed in. An explicitly configured
    provider that fails to load is an error, not a silent switch to the stub."""
    if os.environ.get("RAG_LLM_PROVIDER"):
        return make_llm()
    try:
        return make_llm()
    except ImportError:
        return ExtractiveLLM()
