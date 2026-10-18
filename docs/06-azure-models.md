# 6. Running on Azure models

The service talks to models through one interface, `complete(system, context, question)`,
so the model is configuration, not code. Security doesn't depend on which model answers.
The ACL pre-filter, guardrails, groundedness check and audit log run the same, and
`tests/test_providers.py` proves it with fake Azure clients.

## Which model does which job

| Job | Model | Azure resource | Setting |
|---|---|---|---|
| Write and review code | Claude Code | (developer tool) | — |
| Answer RAG questions | **Claude Opus 5.5** (Generally available, *Hosted on Azure*) | Foundry resource `foundry-ankit-resource` | `RAG_LLM_PROVIDER=foundry`, `RAG_LLM_MODEL=<deployment>` |
| Judge answers / run RAGAS | **GPT-5**, a *different* model family | same Foundry resource (or `bookstore-openai-ankit1`) | `RAG_JUDGE_PROVIDER=azure_openai`, `RAG_JUDGE_MODEL=gpt-5` |
| RAGAS embeddings | text-embedding-3-small | same Foundry resource | `RAG_EMBEDDING_MODEL` |
| Offline tests and PR CI | `ExtractiveLLM` stub | none | (nothing set) |

A judge from a different family avoids self-preference: models tend to grade their own
answers kindly. `claude-sonnet-5-5` is the cheaper Azure-hosted alternative for the RAG
model. Run the nightly eval on both and compare.

### "Hosted on Azure" vs "Anthropic infrastructure"
The Foundry catalog marks where each Claude model runs. Models *hosted on Azure* serve
requests inside Azure. Models on *Anthropic infrastructure* are billed through Azure but served
by Anthropic, and some features (code execution, Files API) exist only there. This demo uses
none of those features, so prefer an Azure-hosted deployment if data residency matters.

## Resource map (what the portal shows)
- **Foundry resource** (`foundry-ankit-resource`): the parent account with endpoint, keys, billing and
  **model deployments**. `ANTHROPIC_FOUNDRY_RESOURCE` is this name.
- **Foundry project** (`foundry-ankit`): a workspace inside the resource (agents, evaluations).
  It uses the resource's deployments; it is not a second model host.
- **Azure OpenAI** (`bookstore-openai-ankit1`): a separate, older-style OpenAI resource. Not needed
  if the Foundry resource already has gpt-5 and embeddings deployed.
- **Azure AI Search**: a candidate for the production retriever. Apply the ACL as a filter inside
  the search query, the same rule as `retriever.py`.

## Setup
1. **Deploy Claude.** Foundry portal → Models → model catalog → search "claude" → `claude-opus-5-5`
   → Deploy. Use the deployment name `claude-opus-5-5` (the code's default). If Deploy is
   unavailable, check subscription eligibility, region and Marketplace terms.
2. **Collect settings** (see `azure.env.example`):
   - Claude: resource name, plus the key from *Keys and Endpoint*.
   - GPT judge: endpoint and API version exactly as shown by **Get code** on the gpt-5 deployment.
3. **Run locally:** `pip install -r requirements-live.txt`, then export the variables (keep them in a
   git-ignored `.env`, never in code or chat):
   ```bash
   python -m safe_rag.cli --user intern "how many days of paid leave do I get"
   python -m safe_rag.eval --live          # golden set against Claude on Foundry
   python -m safe_rag.redteam --live       # leaks must still be 0
   python -m safe_rag.judge                # live judge vs human labels, bar 0.9
   pip install -r requirements-ragas.txt && python eval/ragas_eval.py
   ```
4. **Nightly CI:** copy `devops/github-workflows/nightly-live-eval.yml` to `.github/workflows/` and add
   the secrets and variables listed at the top of that file.

## Keys: least privilege
- Store keys as GitHub Actions **secrets** or in **Azure Key Vault**. The repo's hooks block secrets
  pasted into prompts or written into files.
- **Keyless (recommended for production):** use Microsoft Entra ID instead of keys. Both SDKs accept
  an Entra token provider (`AnthropicFoundry(azure_ad_token_provider=...)`,
  `AzureOpenAI(azure_ad_token_provider=...)`, using `azure-identity`). In GitHub Actions, sign in with
  `azure/login` via OIDC to a managed identity that holds only a data-plane inference role on the Foundry
  resource (for example *Cognitive Services User* or *Azure AI User*; check Microsoft's docs for the current
  name), never Contributor or Owner.
- Give the nightly job its own identity, separate from anything that can change infrastructure.

## RAGAS (nightly only)
`eval/ragas_eval.py` scores **faithfulness, answer correctness, context recall and context
precision**. The thresholds (0.90 / 0.80 / 0.85 / 0.80) are the ones the original assignment set.
- Contexts are the **sanitised, ACL-permitted** documents the model saw, so an injected instruction
  in a document can't steer the grading model either.
- `nan` (unscored) counts as a failure.
- Start report-only. Add `--enforce` after a few nights show the baseline is stable.
- **Versions are pinned** (`requirements-ragas.txt`). ragas 0.4.3 still fails to import with
  LangChain 1.x (`langchain_community.chat_models.vertexai`), the same error documented in
  `rag-assignment`. The pinned set was verified in a fresh virtualenv.
- GPT-5-class models accept only the default temperature, so the RAGAS wrapper is created with
  `bypass_temperature=True, bypass_n=True`.

RAGAS covers answer quality only. Leakage and injection stay on the deterministic gates, which
also run first in the nightly job.

## Refusals
When Claude declines a request (`stop_reason == "refusal"`), `ClaudeLLM` raises `ModelRefusal`.
`ResilientLLM` then falls back to the extractive answerer, which can only quote permitted
documents, and records `llm_fallback` in the healing log. Anthropic's server-side model
fallback isn't available on Foundry, so this client-side path is the fallback.
