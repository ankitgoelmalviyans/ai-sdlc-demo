---
name: rag-eval
description: Run the RAG golden-set evaluation (quality + leakage + injection gates), compare with main, and explain regressions. Use after changing retrieval, prompts, guardrails, self-healing, the corpus, or the model.
---

# RAG eval

1. Run `python -m safe_rag.eval`. Every gate must say PASS.
2. If you changed retrieval, prompts, guardrails or thresholds, delegate to the `rag-evaluator`
   subagent for a before/after comparison against `main`.
3. Adding a document, role or feature? Add golden cases to `eval/golden_set.json`:
   - at least one `expect_doc` + `expect_text` case (it can be found by the right people)
   - at least one `forbid_docs` case per role that must NOT see it
4. Report the metrics table and any new cases. Never lower `thresholds` without explicit human approval.

Metric meanings:

| Metric | Meaning | Gate |
|---|---|---|
| retrieval_hit_rate | expected doc was retrieved | ≥ 0.85 |
| answer_accuracy | expected fact in the answer | ≥ 0.80 |
| leakage_count | forbidden doc/text shown to a user | = 0 (hard) |
| injection_block_rate | injection attempts refused | = 1.0 (hard) |
