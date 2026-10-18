"""Testing the fuzzy parts: LLM-as-judge calibration and non-determinism."""

import random

import pytest

from safe_rag.audit import AuditLog
from safe_rag.corpus import DOCUMENTS, USERS
from safe_rag.judge import HeuristicJudge, calibrate
from safe_rag.llm import ExtractiveLLM
from safe_rag.pipeline import SafeRAG
from safe_rag.redteam import run as run_redteam

# ---- LLM-as-judge --------------------------------------------------------

from safe_rag.judge import AGREEMENT_BAR as JUDGE_AGREEMENT_BAR


def test_judge_agrees_with_humans():
    """Don't trust a judge you haven't calibrated against human labels."""
    result = calibrate(HeuristicJudge())
    assert result["agreement"] >= JUDGE_AGREEMENT_BAR, result["disagreements"]


def test_judge_catches_plausible_hallucination():
    v = HeuristicJudge().grade(
        "what is salary band L4",
        "Salary band L4 is 120k to 150k.",
        "Salary band L4 is 120k to 150k, plus a 20% bonus and stock options. [hr-001]")
    assert not v.good


def test_invented_figure_is_ungrounded():
    """Found by judge calibration: word overlap alone passed '...plus a 20% bonus'."""
    from safe_rag.self_heal import is_grounded
    ctx = "Salary band L4 is 120k to 150k."
    assert is_grounded("Salary band L4 is 120k to 150k. [hr-001]", ctx)
    assert not is_grounded("Salary band L4 is 120k to 150k, plus a 20% bonus. [hr-001]", ctx)


def test_pipeline_answers_score_well_with_judge():
    rag = SafeRAG(list(DOCUMENTS), llm=ExtractiveLLM(), audit=AuditLog())
    judge = HeuristicJudge()
    for user, q in [("engineer", "how do I deploy to production"),
                    ("hr_partner", "how long is parental leave"),
                    ("intern", "how many days of paid leave do I get")]:
        docs = [rag.index.docs[c] for c in rag.ask(USERS[user], q).citations]
        ans = rag.ask(USERS[user], q)
        context = " ".join(d.text for d in docs)
        assert judge.grade(q, context, ans.text).good, (q, ans.text)


# ---- non-determinism -----------------------------------------------------

class StochasticLLM:
    """Like a real model at temperature > 0: right most of the time, sometimes invents things."""

    def __init__(self, seed, hallucination_rate=0.3):
        self.rnd = random.Random(seed)
        self.rate = hallucination_rate
        self.inner = ExtractiveLLM()

    def complete(self, system, context, question):
        if self.rnd.random() < self.rate:
            return "Per the 2019 memo, this requires VP sign-off and a 30-day notice period."
        return self.inner.complete(system, context, question)


CASES = [("engineer", "how do I deploy to production", "production"),
         ("hr_partner", "how long is parental leave", "16 weeks"),
         ("intern", "how many days of paid leave do I get", "24 days"),
         ("hr_comp", "what is salary band L4", "120k")]
RUNS = 25


@pytest.mark.parametrize("user,query,expected", CASES)
def test_answers_are_reliable_across_repeated_runs(user, query, expected):
    """Run N times and assert a pass RATE, not a single lucky run.
    With a 30%-hallucinating model, self-healing should still give 100% correct answers."""
    rag = SafeRAG(list(DOCUMENTS), llm=StochasticLLM(seed=hash(query) % 1000), audit=AuditLog())
    results = [rag.ask(USERS[user], query) for _ in range(RUNS)]
    pass_rate = sum(expected in r.text for r in results) / RUNS
    healed = sum("ungrounded_regenerated" in r.healing for r in results)
    assert pass_rate == 1.0
    assert healed > 0, "the stochastic model should have hallucinated at least once"


def test_raw_model_without_healing_is_unreliable():
    """Control group: proves the previous test isn't passing by accident."""
    model = StochasticLLM(seed=42)
    ctx = '<document id="hr-002" title="Leave">\nParental leave is 16 weeks paid.\n</document>'
    outputs = [model.complete("", ctx, "how long is parental leave") for _ in range(RUNS)]
    assert sum("16 weeks" in o for o in outputs) / RUNS < 1.0


# ---- red team gate -------------------------------------------------------

def test_red_team_gate():
    r = run_redteam()
    assert r["leak_count"] == 0, r["leaks"]
    assert not r["false_positives"], r["false_positives"]
    assert r["input_block_rate"] >= r["baseline_floor"]
