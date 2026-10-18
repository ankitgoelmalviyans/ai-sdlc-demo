---
name: red-team
description: Run and extend the adversarial test suite (prompt injection, jailbreaks, obfuscation, exfiltration) against the RAG service, then ratchet the guardrail floor. Use before a release, after changing guardrails or prompts, or after a security incident.
---

# Red team

1. Baseline: `python -m safe_rag.redteam`. Note block rate, leaks (must be 0) and false positives (must be 0).
2. Delegate to the `red-teamer` subagent to invent and add new attacks plus benign look-alikes.
3. Re-run. If there's a **leak**, stop everything: it's a P0. Reproduce it as a test, fix the control (ACL /
   output guard, not just the regex), and use `review-pr` with `security-reviewer`.
4. If attacks pass the input filter without leaking, optionally improve `guardrails.normalize` or the
   patterns. Then:
   - `python -m pytest -q` (benign queries in the golden set and `benign` list must still pass)
   - raise `min_input_block_rate` in `eval/redteam_baseline.json` to the new rate (ratchet up only)
5. Report the tactic table before and after.

Remember: the input filter is the weakest layer. The real guarantees are the ACL pre-filter and the
output checks, which is why leaks are measured against `CompromisedLLM`, a model that obeys the attacker.
