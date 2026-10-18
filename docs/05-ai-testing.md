# 5. AI testing

"AI testing" means two different things:

- **A. Testing AI systems.** How do you test something whose output isn't fixed?
- **B. Using AI to write tests.** How do you know the tests the AI wrote are any good?

## A. Testing AI systems

### The test pyramid for an LLM app

```
            ▲  slow, costly, fuzzy
            │   Human review / online A/B         (sampled, post-release)
            │   Real-model eval (nightly)         golden set with a real model, LLM-as-judge
            │   Red team                          attacks vs a COMPROMISED fake model
            │   Golden eval set                   quality thresholds + leakage = 0
            │   Metamorphic / property tests      relations that must hold for any output
            │   Deterministic unit tests          ACL, guardrails, parsing, fakes for the model
            ▼  fast, cheap, exact  (runs on every PR)
```

Push as much as possible **down** the pyramid. Most of what makes an LLM app safe (who can see
what, what's blocked, what's logged) is ordinary deterministic code and can be tested exactly.

### Techniques in this repo

| Technique | Problem it solves | File |
|---|---|---|
| **Fakes instead of a real model** | Unit tests must be fast, free, and repeatable | `ExtractiveLLM`, `LeakyLLM`, `StochasticLLM`, `CompromisedLLM` |
| **Invariant tests** | "No user ever sees a doc they can't read", for *all* users × queries | `test_retrieval_never_returns_unauthorized_docs` |
| **Metamorphic tests** | No single right answer, but relations must hold | `tests/test_metamorphic.py` |
| **Golden eval set + thresholds** | Catch quality regressions from prompt or retriever changes | `safe_rag/eval.py` |
| **LLM-as-judge, calibrated** | Fuzzy qualities like faithfulness and relevance | `safe_rag/judge.py`, `eval/judge_labels.json` |
| **Repeated runs / pass rate** | Temperature > 0 means one green run proves little | `test_answers_are_reliable_across_repeated_runs` |
| **Red team vs compromised model** | Prove the *architecture* doesn't leak even if the model obeys the attacker | `safe_rag/redteam.py` |
| **Fuzzing** | Random/garbage input never crashes or leaks | `test_fuzzed_queries_never_crash_or_leak` |

#### Metamorphic relations used here
- Paraphrases of a question cite the same source.
- Case and whitespace don't change the answer.
- Adding an unrelated document doesn't change existing answers.
- Adding a **restricted** document leaves the intern's outputs byte-identical. This is the strongest
  ACL test there is.
- Shuffling the corpus doesn't change citations.
- Tightening a document's classification never *grants* anyone access (monotonicity).

#### Red team: measure the right thing
`python -m safe_rag.redteam` runs 20 attacks across 14 tactics (direct, role-play, unicode tricks,
base64, letter splitting, multilingual, exfiltration...). Three numbers:

| Metric | Bar | Why |
|---|---|---|
| **Leaks** vs `CompromisedLLM` | **0, hard gate** | The fake obeys the attacker and dumps everything. If nothing leaks, the ACL + output checks hold regardless of the model. |
| **Input block rate** | ratchet (floor in `eval/redteam_baseline.json`) | Regex never catches everything. Track it and only let it go up. |
| **False positives** on benign look-alikes | **0** | Over-blocking ("how do I ignore a flaky test?") is a failure too. |

What happened when this was built: the first run blocked **30%** at the input, yet leaked **0**.
That's defence in depth working. The red team then drove cheap fixes (unicode normalisation,
zero-width stripping, base64 decoding, de-hyphenation, synonyms), which raised the block rate to
**90%** with 0 false positives. The remaining two "attacks" are an authority claim and a broad
summary request, which the ACL and PII redaction handle by design.

#### LLM-as-judge: calibrate before trusting
A judge is a model and can be wrong. `calibrate()` compares the judge with **human labels**
(`eval/judge_labels.json`) and the test requires ≥ 90% agreement. When this was built, calibration
caught a real weakness: the word-overlap check called "120k to 150k, *plus a 20% bonus*" faithful.
The fix (every figure in an answer must appear in the context) went into the live groundedness
check too, so the pipeline now rejects invented numbers.

Rules for judges:
- Use a rubric with discrete scores and JSON output (`RUBRIC` in `judge.py`).
- Tell the judge that the graded text is data, not instructions (judges can be prompt-injected too).
- Re-calibrate when you change the judge model or rubric.
- Judges **advise**; leakage and injection are gated by deterministic checks.

#### Non-determinism
- Run N times and assert a **pass rate** (`RUNS = 25`). Here it's 100%, because a 30%-hallucinating
  model is healed by the groundedness check.
- Include a **control group** (`test_raw_model_without_healing_is_unreliable`) so you know the test
  can fail. Otherwise a test that always passes looks the same as a test that works.
- Real-model evals are slow and cost money: run them **nightly**, not on every PR. Pin model
  versions, and record model + prompt version with every eval result.

## B. Testing tests that AI wrote

AI writes plausible tests quickly. Common weaknesses:
- asserting only "no exception was raised"
- asserting on the mock instead of the result
- re-implementing the function inside the test, so test and code share the same bug
- tests that still pass when the function body is deleted
- "fixing" a failing test by loosening the assertion

**Coverage can't catch these.** A line can run without anyone checking its result.

### Mutation testing
`python devops/scripts/mutation_check.py` breaks one security control at a time (removes the ACL
filter, makes admins bypass role checks, disables the canary, runs healing before the security
check...), runs the suite on a throwaway copy, and requires it to **fail**.

```
killed    ACL pre-filter removed
killed    admin bypasses role allow-list
killed    healing runs before security check
...
mutation score 14/14
```

It found a gap during the build: nothing pinned the exact query-length limit (2000 vs 2001 survived).
The boundary test that fixes this is `test_query_length_boundary`.

Policy in this repo:
- every new security control comes with a mutant, in the same PR
- `mutation_check.py`, `eval/*` and the red-team baseline are **high-risk paths** in `risk_gate.py`,
  so deleting a mutant or lowering a threshold needs security review
- for broad automatic mutation on other code, use `mutmut` / `cosmic-ray`, review survivors, and
  don't chase 100%

### Process guardrails for AI-written tests
- `test-writer` subagent edits `tests/` only, so it can't change production code to make its test pass.
- `.claude/rules/testing.md`: no `skip`/`xfail`/loosened asserts.
- `Stop` hook: the agent can't finish with red tests.
- `test-the-tests` skill after every AI test-writing session.

## Agents and skills for AI testing

| | Name | Does |
|---|---|---|
| agent | `ai-test-strategist` | picks which layers above a feature needs, with thresholds |
| agent | `red-teamer` | invents new attacks + benign twins, grows the corpus |
| agent | `test-writer` | writes tests (edits `tests/` only) |
| agent | `rag-evaluator` | golden-set before/after comparison |
| skill | `red-team` | run, extend, ratchet |
| skill | `test-the-tests` | mutation check → survivors to `test-writer` |
| skill | `write-unit-tests` | now ends with the mutation check |
| skill | `rag-eval` | eval gates + new cases |

## CI (every PR)

`pytest` (98 tests, including metamorphic, judge calibration, repeated-run and the red-team gate)
→ `safe_rag.eval` → `safe_rag.redteam` → `mutation_check.py`. All offline, about 10 seconds.
Real-model eval belongs in a nightly workflow: `devops/github-workflows/nightly-live-eval.yml` runs the
eval and red team against Claude on Azure Foundry, calibrates the GPT judge, and reports RAGAS
scores (see [06-azure-models.md](06-azure-models.md)).
