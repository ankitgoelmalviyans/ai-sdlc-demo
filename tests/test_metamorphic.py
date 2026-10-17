"""Metamorphic tests: for AI systems there's often no single 'right' output to
assert, but there ARE relations that must hold between outputs.

    "If I paraphrase the question, I should get the same source."
    "If I add an unrelated document, existing answers shouldn't change."
    "If I add a restricted document, what the intern sees must not change at all."

These catch whole classes of bugs without hand-writing an expected answer per case.
"""

import random
from dataclasses import replace

import pytest

from safe_rag.audit import AuditLog
from safe_rag.corpus import DOCUMENTS, USERS
from safe_rag.llm import ExtractiveLLM
from safe_rag.models import Classification, Document
from safe_rag.pipeline import SafeRAG

PARAPHRASES = [
    ("engineer", "eng-001", ["how do I deploy to production",
                             "what is the process to deploy to production",
                             "production deployment steps?",
                             "HOW DO I DEPLOY TO PRODUCTION"]),
    ("hr_partner", "hr-002", ["how long is parental leave",
                              "parental leave duration",
                              "how many weeks of parental leave do we get"]),
    ("intern", "pub-001", ["how many days of paid leave do I get",
                           "paid leave days per year",
                           "how much vacation do I get"]),   # needs the query rewrite
]

GOLDEN_QUERIES = [
    ("intern", "how many days of paid leave do I get"),
    ("engineer", "how do I deploy to production"),
    ("hr_partner", "how long is parental leave"),
    ("hr_comp", "what is salary band L4"),
    ("intern", "what are the salary bands"),
]


def rag(docs=None):
    return SafeRAG(list(docs or DOCUMENTS), llm=ExtractiveLLM(), audit=AuditLog())


@pytest.mark.parametrize("user,doc_id,questions", PARAPHRASES)
def test_paraphrases_cite_the_same_source(user, doc_id, questions):
    r = rag()
    for q in questions:
        assert r.ask(USERS[user], q).citations == [doc_id], q


@pytest.mark.parametrize("user,query", GOLDEN_QUERIES)
def test_whitespace_and_case_do_not_change_the_answer(user, query):
    r = rag()
    base = r.ask(USERS[user], query).text
    assert r.ask(USERS[user], f"  {query.upper()}  ").text == base


def test_unrelated_document_does_not_change_answers():
    noise = Document("pub-900", "Cafeteria menu", "Monday is pasta. Tuesday is tacos.", Classification.PUBLIC)
    before, after = rag(), rag([*DOCUMENTS, noise])
    for user, q in GOLDEN_QUERIES:
        assert after.ask(USERS[user], q).text == before.ask(USERS[user], q).text, q


def test_restricted_document_is_invisible_to_intern():
    """Strongest form of the ACL property: the intern's outputs are byte-identical
    whether or not a restricted doc that matches their query exists."""
    secret = Document("hr-900", "Layoff plan", "The layoff plan cuts paid leave and deployment staff.",
                      Classification.RESTRICTED, department="hr", allowed_roles=frozenset({"comp_viewer"}))
    before, after = rag(), rag([*DOCUMENTS, secret])
    for q in ["how many days of paid leave do I get", "how do I deploy to production", "layoff plan"]:
        assert after.ask(USERS["intern"], q).text == before.ask(USERS["intern"], q).text, q


def test_corpus_order_does_not_change_answers():
    shuffled = list(DOCUMENTS)
    random.Random(7).shuffle(shuffled)
    a, b = rag(), rag(shuffled)
    for user, q in GOLDEN_QUERIES:
        assert a.ask(USERS[user], q).citations == b.ask(USERS[user], q).citations, q


def test_tightening_acl_never_grants_access():
    """Monotonicity: raising a doc's classification can only remove readers, never add."""
    for i, doc in enumerate(DOCUMENTS):
        if doc.classification == Classification.RESTRICTED:
            continue
        tighter = list(DOCUMENTS)
        tighter[i] = replace(doc, classification=Classification(doc.classification + 1))
        for user in USERS.values():
            loose_hits = set(rag().ask(user, doc.title).citations)
            tight_hits = set(rag(tighter).ask(user, doc.title).citations)
            assert doc.id not in tight_hits - loose_hits


def test_fuzzed_queries_never_crash_or_leak():
    rnd = random.Random(1234)
    alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 ?!.-_\n\t​ａé"
    vocab = ["salary", "band", "ignore", "previous", "instructions", "deploy", "leave", "L4", "system", "prompt"]
    r = rag()
    for _ in range(300):
        parts = [rnd.choice(vocab) if rnd.random() < 0.5 else
                 "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 8)))
                 for _ in range(rnd.randint(0, 12))]
        ans = r.ask(USERS["intern"], " ".join(parts))
        assert "120k" not in ans.text and "160k" not in ans.text and "CANARY-" not in ans.text
