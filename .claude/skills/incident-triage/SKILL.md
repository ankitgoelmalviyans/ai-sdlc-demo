---
name: incident-triage
description: Triage a RAG production incident (bad answers, blocked-request spikes, suspected data leak, model provider outage) from audit logs. Use when the user reports something wrong in prod or shares audit.jsonl.
---

# Incident triage

1. Ask for (or locate) the audit log and the time window. Never ask for raw user queries; the log holds hashes.
2. Delegate to the `incident-responder` subagent with the log path and symptom.
3. If a **leak** is suspected, put it in the first line of your reply and recommend, in order:
   - quarantine the document (remove it from `corpus.py`; the index self-heals with `orphaned:<id>`)
   - fix the ACL at the source; confirm `reindexed:acl_changed:<id>`
   - add a `forbid_docs` golden case reproducing the leak, so it can never regress
4. If it's a **provider outage** (`llm_fallback` / `llm_circuit_open` in healing): service is degraded but
   safe, since the extractive fallback only quotes authorized docs. Recommend status-page comms, not code changes.
5. Produce: summary, impact (users/docs by id), timeline, root cause, actions, and follow-ups for a postmortem.
