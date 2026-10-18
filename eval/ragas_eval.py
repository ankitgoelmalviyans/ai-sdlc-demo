"""Nightly RAGAS evaluation against the real models (not part of PR CI).

    pip install -r requirements-live.txt -r requirements-ragas.txt
    python eval/ragas_eval.py             # report scores vs thresholds
    python eval/ragas_eval.py --enforce   # exit 1 if any metric is below its threshold

What runs where:
- the RAG pipeline answers with the model from RAG_LLM_PROVIDER (e.g. Claude on Foundry)
- RAGAS grades with a *different* model family: an Azure OpenAI / Foundry GPT deployment
  (RAG_JUDGE_MODEL) plus an embeddings deployment (RAG_EMBEDDING_MODEL). The openai SDK
  reads AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY and OPENAI_API_VERSION.

RAGAS measures answer quality only. Leakage and injection stay on the deterministic
gates (safe_rag.eval / safe_rag.redteam), which also run here first.

Versions are pinned in requirements-ragas.txt: ragas 0.4.3 fails to import with
LangChain 1.x (the same error documented in rag-assignment's TECHNICAL_DETAILS.md).
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from safe_rag import guardrails  # noqa: E402
from safe_rag.audit import AuditLog  # noqa: E402
from safe_rag.corpus import DOCUMENTS, USERS  # noqa: E402
from safe_rag.llm import make_llm  # noqa: E402
from safe_rag.pipeline import SafeRAG  # noqa: E402

GOLDEN = ROOT / "eval" / "golden_set.json"
# The thresholds the original assignment set for RAGAS.
THRESHOLDS = {"faithfulness": 0.90, "answer_correctness": 0.80,
              "context_recall": 0.85, "context_precision": 0.80}
_TAGS = re.compile(r"</?document[^>]*>")


def build_samples(llm=None) -> list[dict]:
    """Run each quality case through the real pipeline and collect RAGAS rows.
    Contexts are the sanitised, ACL-permitted documents the model actually saw, so an
    injected instruction in a document can't steer the grading model either."""
    cases = [c for c in json.loads(GOLDEN.read_text())["cases"] if "reference" in c]
    rag = SafeRAG(list(DOCUMENTS), llm=llm or make_llm(), audit=AuditLog())
    samples = []
    for c in cases:
        ans = rag.ask(USERS[c["user"]], c["query"])
        docs = [rag.index.docs[i] for i in ans.retrieved]
        contexts = [_TAGS.sub("", guardrails.sanitize_context([d])[0]).strip() for d in docs]
        samples.append({"user_input": c["query"], "retrieved_contexts": contexts,
                        "response": ans.text, "reference": c["reference"]})
    return samples


def score(samples: list[dict]) -> dict[str, float]:
    import os

    from langchain_openai import AzureChatOpenAI, AzureOpenAIEmbeddings
    from ragas import EvaluationDataset, RunConfig, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import answer_correctness, context_precision, context_recall, faithfulness

    # GPT-5-class deployments only accept the default temperature and n=1.
    judge = LangchainLLMWrapper(AzureChatOpenAI(azure_deployment=os.environ["RAG_JUDGE_MODEL"]),
                                bypass_temperature=True, bypass_n=True)
    embeddings = LangchainEmbeddingsWrapper(
        AzureOpenAIEmbeddings(azure_deployment=os.environ["RAG_EMBEDDING_MODEL"]))
    result = evaluate(EvaluationDataset.from_list(samples),
                      metrics=[faithfulness, answer_correctness, context_recall, context_precision],
                      llm=judge, embeddings=embeddings,
                      run_config=RunConfig(max_workers=4, timeout=180),
                      show_progress=False)
    frame = result.to_pandas()
    return {m: round(float(frame[m].mean()), 3) for m in THRESHOLDS}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--enforce", action="store_true", help="exit 1 if a metric is below threshold")
    args = p.parse_args()
    scores = score(build_samples())
    failed = []
    for metric, threshold in THRESHOLDS.items():
        value = scores[metric]
        ok = value == value and value >= threshold   # NaN (unscored) counts as a failure
        failed += [] if ok else [metric]
        print(f"{'PASS' if ok else 'FAIL'}  {metric:20} {value}  (threshold {threshold})")
    print(json.dumps({"ragas": scores}))
    return 1 if args.enforce and failed else 0


if __name__ == "__main__":
    sys.exit(main())
