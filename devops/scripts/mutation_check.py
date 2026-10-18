#!/usr/bin/env python3
"""Security-focused mutation testing: do the tests notice when a control breaks?

AI writes tests fast, and AI-written tests are often too weak: they assert
that the code runs, not that it's right. Coverage won't tell you. Mutation
testing will: deliberately break one security control, run the suite, and
expect it to FAIL. A mutant that survives (tests still green) is a control
nobody is really testing.

Each mutant is applied to a throwaway copy of the repo, never the working
tree. General-purpose tools (mutmut, cosmic-ray) generate mutants
automatically; this hand-picked list targets the controls that matter most.

    python devops/scripts/mutation_check.py      # exit 1 if any mutant survives
"""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# (name, file, original code, mutated code)
MUTANTS = [
    ("ACL pre-filter removed", "safe_rag/retriever.py",
     "candidates = permitted(user, docs)", "candidates = docs"),
    ("clearance check off-by-one", "safe_rag/policy.py",
     "if user.clearance < doc.classification:", "if user.clearance < doc.classification - 1:"),
    ("role allow-list ignored", "safe_rag/policy.py",
     "if doc.allowed_roles and not (doc.allowed_roles & user.roles):", "if False:"),
    ("admin bypasses role allow-list", "safe_rag/policy.py",
     "    if doc.allowed_roles and not", "    if ADMIN_ROLE not in user.roles and doc.allowed_roles and not"),
    ("canary leak check disabled", "safe_rag/guardrails.py",
     "if canary in text:", "if False:"),
    ("citation check disabled", "safe_rag/guardrails.py",
     "if any(c not in allowed_ids for c in cited):", "if False:"),
    ("PII redaction disabled", "safe_rag/guardrails.py",
     "if PII_VIEWER_ROLE in user.roles:", "if True:"),
    ("context injection stripping disabled", "safe_rag/guardrails.py",
     "if _INJECTION.search(text):", "if False:"),
    ("input normalisation removed", "safe_rag/guardrails.py",
     "if _INJECTION.search(normalize(query)):", "if _INJECTION.search(query):"),
    ("healing runs before security check", "safe_rag/pipeline.py",
     "        blocked = self._check_output(user, query, raw, allowed_ids, canary)\n        if blocked:\n            return blocked\n\n        # 7.",
     "        # 7."),
    ("ACL drift not detected", "safe_rag/index.py",
     "if self._acl[d.id] != _acl_hash(d):", "if False:"),
    ("groundedness check always passes", "safe_rag/self_heal.py",
     "def is_grounded(answer: str, context: str) -> bool:\n",
     "def is_grounded(answer: str, context: str) -> bool:\n    return True\n"),
    ("query length limit loosened", "safe_rag/guardrails.py",
     "MAX_QUERY_CHARS = 2000", "MAX_QUERY_CHARS = 2001"),
    ("model refusal treated as an answer", "safe_rag/llm.py",
     'if msg.stop_reason == "refusal":', "if False:"),
    ("azure content filter treated as an answer", "safe_rag/llm.py",
     'if choice.finish_reason == "content_filter":', "if False:"),
    ("refusals retried like outages", "safe_rag/resilience.py",
     "        except ModelRefusal:\n            raise                      # asking again won't change the answer\n",
     ""),
    ("audit stores raw query", "safe_rag/audit.py",
     'record["query_sha256"] = hashlib.sha256(record.pop("query").encode()).hexdigest()[:16]',
     'record["query_sha256"] = hashlib.sha256(record["query"].encode()).hexdigest()[:16]'),
]


def run_mutant(name: str, rel: str, old: str, new: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))
        target = work / rel
        src = target.read_text()
        if src.count(old) != 1:
            return "stale"   # the code moved; update the mutant
        target.write_text(src.replace(old, new))
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
                           cwd=work, capture_output=True, text=True, timeout=300)
        return "killed" if r.returncode != 0 else "SURVIVED"


def main() -> int:
    results = [(name, run_mutant(name, rel, old, new)) for name, rel, old, new in MUTANTS]
    for name, outcome in results:
        print(f"{outcome:9} {name}")
    killed = sum(o == "killed" for _, o in results)
    print(f"\nmutation score {killed}/{len(results)}")
    bad = [n for n, o in results if o != "killed"]
    if bad:
        print("Tests did not catch: " + "; ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
