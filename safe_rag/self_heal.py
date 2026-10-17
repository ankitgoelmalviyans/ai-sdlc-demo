"""Self-correcting retrieval and generation (Corrective-RAG style).

Two loops, each bounded to ONE retry so a bad query can't spin forever:

1. Retrieval grading -> query rewrite.
   If the best retrieved chunk scores below a threshold, rewrite the query
   (expand synonyms / jargon) and search again. Rewriting never widens
   access: the second search goes through the same ACL filter.

2. Groundedness check -> regenerate.
   If the answer contains sentences not supported by the context, discard
   it and regenerate with the extractive fallback, which can only quote the
   context. If that still isn't grounded, say "I don't know".
"""

import re

from .retriever import STOPWORDS

RETRIEVAL_THRESHOLD = 0.15
GROUNDED_THRESHOLD = 0.6  # share of an answer sentence's words found in the context

# In production this is an LLM call ("rewrite this query for search") or a
# domain thesaurus; a fixed map keeps the demo deterministic.
SYNONYMS = {
    "pay": "salary",
    "compensation": "salary",
    "wage": "salary",
    "vacation": "paid leave",
    "pto": "paid leave",
    "holiday": "holidays office closed",
    "ship": "deployment production",
    "release": "deployment production",
    "rollback": "roll back deployment",
    "outage": "incident sev1",
    "hacked": "breach security",
    "maternity": "parental leave",
    "paternity": "parental leave",
}

_WORD = re.compile(r"[a-z0-9]+")
_CITE = re.compile(r"\s*\[[a-z]+-\d+\]")


def rewrite_query(query: str) -> str | None:
    """Return an expanded query, or None if there is nothing to expand."""
    words = _WORD.findall(query.lower())
    extra = [SYNONYMS[w] for w in words if w in SYNONYMS]
    return f"{query} {' '.join(extra)}" if extra else None


def is_grounded(answer: str, context: str) -> bool:
    """Every sentence of the answer must be mostly made of words from the context."""
    ctx_words = set(_WORD.findall(context.lower()))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", _CITE.sub("", answer)) if s.strip()]
    if not sentences:
        return False
    for s in sentences:
        words = [w for w in _WORD.findall(s.lower()) if w not in STOPWORDS]
        if words and sum(w in ctx_words for w in words) / len(words) < GROUNDED_THRESHOLD:
            return False
    return True
