---
name: fix-failing-ci
description: Self-healing dev loop for red CI or failing tests. Gets the logs, reproduces locally, root-causes with the debugger subagent, fixes, verifies, and escalates after 3 attempts. Use when CI is red, a test fails, or the user says "fix the build".
---

# Fix failing CI

1. **Collect the failure.** From CI, take the failing job's log (the `ai-ci-failure-triage` workflow posts
   one on the PR). Locally, run `python -m pytest -q` and `python -m safe_rag.eval`.
2. **Is it ours?** Check whether the same check is red on `main`. If it is, this PR didn't cause it:
   say so and stop. Don't "fix" unrelated code inside this PR.
3. **Delegate** to the `debugger` subagent with the exact failing command and output.
4. **Validate** the fix yourself. It's not done until all three pass:
   - the originally failing test
   - full `python -m pytest -q`
   - `python -m safe_rag.eval` (all gates PASS)
5. **Bounded:** max 3 fix attempts. On the 3rd failure, stop and report what was tried, what was
   learned, and the best remaining hypothesis. A human takes it from there.
6. If the fix touched `safe_rag/policy.py`, `safe_rag/guardrails.py`, `devops/` or `.github/`, run
   `security-reviewer` / `infra-reviewer` before reporting done.

Forbidden shortcuts: skipping/xfail-ing tests, lowering eval thresholds, broad `except:`, re-running
until green, `--no-verify`.
