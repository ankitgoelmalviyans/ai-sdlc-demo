---
name: ship-feature
description: Full SDLC chain for a feature from request to PR-ready, covering plan, threat model, build, tests, eval, layered review, and docs, with human checkpoints. Use when the user says "build/ship/implement X end to end".
---

# Ship a feature end to end

Each step names who does it. Stop at every **CHECKPOINT** for a human.

| # | Step | Who |
|---|---|---|
| 1 | Plan + acceptance criteria | `plan-feature` skill → `planner` |
| 2 | Threat model (if it touches data/auth/prompts/infra) | `threat-model` skill |
| | **CHECKPOINT: human approves plan** | |
| 3 | Test strategy for model-facing behaviour | `ai-test-strategist` |
| 3b | Write failing tests for the acceptance criteria | `test-writer` |
| 4 | Implement in small steps (hooks re-run tests after each edit) | you |
| 5 | Red anywhere? | `fix-failing-ci` skill → `debugger` |
| 6 | RAG quality/safety gates | `rag-eval` skill → `rag-evaluator` |
| 6b | Adversarial + mutation | `red-team` skill → `red-teamer`; `test-the-tests` skill |
| 7 | Layered review | `review-pr` skill → `code-reviewer` + `security-reviewer` (+ `infra-reviewer` for devops/) |
| 8 | Docs | `docs-writer` |
| | **CHECKPOINT: human reviews diff + opens/approves PR** | |

Done means: tests green, eval gates PASS, no blocker findings, docs updated, and the Stop hook lets you finish.
