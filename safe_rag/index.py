"""Vector index with drift detection and self-healing re-indexing.

A RAG index is a *copy* of the source system, and copies go stale:

- content edited in the source        -> index serves an old answer
- document deleted in the source      -> index keeps serving it (orphan)
- permissions changed in the source   -> index enforces the OLD ACL  <-- security bug
- new document added                  -> index can't find it

`check()` compares a fingerprint of every source document with what was
indexed; `heal()` re-indexes only what drifted. `ensure_fresh()` does both
and is cheap enough to call before each query in this demo (in production,
run it on a schedule and on source-system change events).
"""

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from .models import Document
from .retriever import vectorize


def _content_hash(d: Document) -> str:
    return hashlib.sha256((d.title + "\x00" + d.text).encode()).hexdigest()


def _acl_hash(d: Document) -> str:
    acl = f"{int(d.classification)}|{d.department}|{','.join(sorted(d.allowed_roles))}"
    return hashlib.sha256(acl.encode()).hexdigest()


@dataclass
class DriftReport:
    missing: list[str] = field(default_factory=list)      # in source, not indexed
    stale: list[str] = field(default_factory=list)        # content changed
    acl_changed: list[str] = field(default_factory=list)  # permissions changed
    orphaned: list[str] = field(default_factory=list)     # indexed, gone from source

    @property
    def healthy(self) -> bool:
        return not (self.missing or self.stale or self.acl_changed or self.orphaned)

    def summary(self) -> list[str]:
        return [f"{kind}:{doc_id}" for kind in ("missing", "stale", "acl_changed", "orphaned")
                for doc_id in getattr(self, kind)]


class VectorIndex:
    def __init__(self) -> None:
        self.docs: dict[str, Document] = {}
        self.vectors: dict[str, Counter] = {}
        self._content: dict[str, str] = {}
        self._acl: dict[str, str] = {}

    @classmethod
    def build(cls, source: list[Document]) -> "VectorIndex":
        idx = cls()
        for d in source:
            idx._put(d)
        return idx

    def _put(self, d: Document) -> None:
        self.docs[d.id] = d
        self.vectors[d.id] = vectorize(d.title + " " + d.text)
        self._content[d.id] = _content_hash(d)
        self._acl[d.id] = _acl_hash(d)

    def _drop(self, doc_id: str) -> None:
        for store in (self.docs, self.vectors, self._content, self._acl):
            store.pop(doc_id, None)

    def check(self, source: list[Document]) -> DriftReport:
        report = DriftReport()
        source_ids = set()
        for d in source:
            source_ids.add(d.id)
            if d.id not in self.docs:
                report.missing.append(d.id)
                continue
            if self._content[d.id] != _content_hash(d):
                report.stale.append(d.id)
            if self._acl[d.id] != _acl_hash(d):
                report.acl_changed.append(d.id)
        report.orphaned = sorted(set(self.docs) - source_ids)
        return report

    def heal(self, source: list[Document], report: DriftReport) -> None:
        by_id = {d.id: d for d in source}
        for doc_id in report.orphaned:
            self._drop(doc_id)
        for doc_id in {*report.missing, *report.stale, *report.acl_changed}:
            self._put(by_id[doc_id])

    def ensure_fresh(self, source: list[Document]) -> DriftReport:
        report = self.check(source)
        if not report.healthy:
            self.heal(source, report)
        return report
