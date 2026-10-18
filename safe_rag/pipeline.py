"""End-to-end safe RAG pipeline with self-healing.

Security checks always win over healing: a guardrail violation is blocked
and audited, never silently "repaired" by regenerating.
"""

import re

from . import guardrails
from .audit import AuditLog
from .index import VectorIndex
from .llm import DONT_KNOW, LLM, ExtractiveLLM, default_llm
from .models import Answer, Document, User
from .resilience import ResilientLLM
from .retriever import search
from .self_heal import RETRIEVAL_THRESHOLD, is_grounded, rewrite_query

_CITE = re.compile(r"\[([a-z]+-\d+)\]")


class SafeRAG:
    def __init__(self, docs: list[Document], llm: LLM | None = None, audit: AuditLog | None = None):
        # `docs` is the live source of truth; the index is a copy that can drift.
        self.source = docs
        self.index = VectorIndex.build(docs)
        self.fallback = ExtractiveLLM()
        primary = llm or default_llm()
        self.llm = primary if isinstance(primary, ResilientLLM) else ResilientLLM(primary, self.fallback)
        self.audit = audit or AuditLog()

    def ask(self, user: User, query: str) -> Answer:
        # 1. Input guardrails
        reasons = guardrails.check_input(query)
        if reasons:
            self.audit.write(event="blocked_input", user=user.id, query=query, reasons=reasons)
            return Answer("Request blocked by input policy.", [], blocked=True, reasons=reasons)

        # 2. Self-heal the index: re-sync content and ACLs that drifted from the source
        report = self.index.ensure_fresh(self.source)
        healing = [f"reindexed:{item}" for item in report.summary()]

        # 3. ACL-filtered retrieval, with one corrective query rewrite if it's weak
        docs, effective_query = self._retrieve(user, query)
        if effective_query != query:
            healing.append("query_rewritten")
        if not docs:
            self.audit.write(event="no_context", user=user.id, query=query, healing=healing)
            return Answer(DONT_KNOW, [], healing=healing)
        allowed_ids = {d.id for d in docs}

        # 4. Context sanitising
        context, flagged = guardrails.sanitize_context(docs)

        # 5. Generation (retry / circuit breaker / fallback live in ResilientLLM)
        canary = guardrails.new_canary()
        system = guardrails.system_prompt(canary)
        raw = self.llm.complete(system, context, effective_query)
        healing += self.llm.events

        # 6. Output guardrails -- checked BEFORE any healing so leaks are blocked, not hidden
        blocked = self._check_output(user, query, raw, allowed_ids, canary)
        if blocked:
            return blocked

        # 7. Groundedness: regenerate once from the extractive fallback, else "don't know"
        if raw != DONT_KNOW and not is_grounded(raw, context):
            healing.append("ungrounded_regenerated")
            raw = self.fallback.complete(system, context, effective_query)
            if not is_grounded(raw, context):
                raw = DONT_KNOW
            blocked = self._check_output(user, query, raw, allowed_ids, canary)
            if blocked:
                return blocked

        cited = _CITE.findall(raw)
        text = guardrails.redact_pii(raw, user)

        # 8. Audit
        self.audit.write(event="answered", user=user.id, query=query, docs=sorted(allowed_ids),
                         flagged_docs=flagged, cited=cited, healing=healing)
        return Answer(text, cited, reasons=[f"flagged:{d}" for d in flagged], healing=healing)

    def _retrieve(self, user: User, query: str) -> tuple[list[Document], str]:
        """Return (docs, query actually used). The rewrite only appends known synonyms,
        so it is safe to hand to the model after the input check on the original."""
        docs, vectors = list(self.index.docs.values()), self.index.vectors
        hits = search(user, query, docs, vectors=vectors)
        best = hits[0][1] if hits else 0.0
        if best < RETRIEVAL_THRESHOLD and (better_query := rewrite_query(query)):
            retry_hits = search(user, better_query, docs, vectors=vectors)
            if retry_hits and retry_hits[0][1] > best:
                return [d for d, _ in retry_hits], better_query
        return [d for d, _ in hits], query

    def _check_output(self, user, query, raw, allowed_ids, canary) -> Answer | None:
        reasons = guardrails.check_output(raw, _CITE.findall(raw), allowed_ids, canary)
        if not reasons:
            return None
        self.audit.write(event="blocked_output", user=user.id, query=query,
                         docs=sorted(allowed_ids), reasons=reasons)
        return Answer("Response withheld by output policy.", [], blocked=True, reasons=reasons)
