"""Golden-set evaluation with quality AND safety gates.

    python -m safe_rag.eval                 # human-readable report, exit 1 on regression
    python -m safe_rag.eval --json          # machine-readable (for the rag-evaluator agent / CI)
    python -m safe_rag.eval --live          # same gates against the real model (RAG_LLM_PROVIDER)

Case types in eval/golden_set.json:
  expect_doc / expect_text   -> quality: right source retrieved, right fact in the answer
  forbid_docs / forbid_text  -> safety: nothing the user may not see shows up
  expect_blocked             -> safety: injection attempts are refused

Leakage has a hard max of 0. Quality metrics have thresholds so a prompt or
retriever change that quietly makes answers worse fails CI.
"""

import argparse
import json
import sys
from pathlib import Path

from .audit import AuditLog
from .corpus import DOCUMENTS, USERS
from .llm import ExtractiveLLM, make_llm
from .pipeline import SafeRAG
from .retriever import search

GOLDEN = Path(__file__).parent.parent / "eval" / "golden_set.json"


def run(path: Path = GOLDEN, llm=None) -> dict:
    spec = json.loads(path.read_text())
    rag = SafeRAG(list(DOCUMENTS), llm=llm or ExtractiveLLM(), audit=AuditLog())
    hits = quality = accurate = leaks = inj = inj_blocked = 0
    failures = []

    for case in spec["cases"]:
        user, query = USERS[case["user"]], case["query"]
        ans = rag.ask(user, query)

        if case.get("expect_blocked"):
            inj += 1
            inj_blocked += ans.blocked
            if not ans.blocked:
                failures.append(f"NOT BLOCKED: {case['user']}: {query}")
            continue

        if "expect_doc" in case:
            quality += 1
            # retrieval hit = the expected doc was in what the model was shown
            shown = {d.id for d, _ in search(user, query, list(rag.index.docs.values()))} | set(ans.citations)
            hit = case["expect_doc"] in shown
            ok = case.get("expect_text", "") in ans.text
            hits += hit
            accurate += ok
            if not (hit and ok):
                failures.append(f"QUALITY: {case['user']}: {query!r} -> {ans.text!r}")

        leaked = [d for d in case.get("forbid_docs", []) if d in ans.citations]
        leaked += [t for t in case.get("forbid_text", []) if t in ans.text]
        if leaked:
            leaks += 1
            failures.append(f"LEAK: {case['user']}: {query!r} leaked {leaked}")

    metrics = {
        "retrieval_hit_rate": round(hits / quality, 3) if quality else 1.0,
        "answer_accuracy": round(accurate / quality, 3) if quality else 1.0,
        "leakage_count": leaks,
        "injection_block_rate": round(inj_blocked / inj, 3) if inj else 1.0,
    }
    t = spec["thresholds"]
    gates = {
        "retrieval_hit_rate": metrics["retrieval_hit_rate"] >= t["retrieval_hit_rate"],
        "answer_accuracy": metrics["answer_accuracy"] >= t["answer_accuracy"],
        "leakage_count": metrics["leakage_count"] <= t["leakage_count_max"],
        "injection_block_rate": metrics["injection_block_rate"] >= t["injection_block_rate"],
    }
    return {"metrics": metrics, "gates": gates, "passed": all(gates.values()), "failures": failures}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--json", action="store_true")
    p.add_argument("--live", action="store_true",
                   help="use the model from RAG_LLM_PROVIDER instead of the offline stub")
    args = p.parse_args()
    result = run(llm=make_llm() if args.live else None)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for name, value in result["metrics"].items():
            print(f"{'PASS' if result['gates'][name] else 'FAIL'}  {name:22} {value}")
        for f in result["failures"]:
            print(f"  - {f}")
        print("eval passed" if result["passed"] else "eval FAILED")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
