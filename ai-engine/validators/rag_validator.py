"""
RAGValidator — RAGAS (context_precision, context_recall, faithfulness,
answer_correctness) against golden_cases that carry a retrieval_context
(golden_cases.retrieval_context, migration V13).

AIPQ's golden cases were written for plain system-prompt evaluation, not
retrieval-augmented generation — most prompts have no retrieved documents at
all, so this validator only has anything to say about the subset of cases
that were explicitly given a retrieval_context. A prompt with zero such
cases is reported as NOT_APPLICABLE (see completeness_engine.py), not
scored 0 — an untested RAG layer is not the same as a failing one.

expected_behavior (a free-text behavior description, e.g. "declines to
answer beyond the provided data") is reused as RAGAS's `ground_truth` for
answer_correctness/context_recall. That's an approximation: those two
metrics were designed for a literal reference answer, not a behavior
description, so their scores here skew toward "did the answer's substance
match the described behavior" rather than exact-answer correctness. Treat
them as directionally useful, not a precise correctness number — same
caveat class as portability.py's judge-model note.

Judge/embeddings: RAGAS needs an LLM (for context_precision/recall/
answer_correctness's classification steps) and an embedding model (for
answer_correctness's semantic-similarity component). Both are wired to
run locally/on already-configured infra rather than defaulting to OpenAI:
Groq (already a dependency) via langchain-groq for the LLM, and
sentence-transformers (already a dependency) via langchain-community's
HuggingFaceEmbeddings for embeddings — no new API key required beyond the
GROQ_API_KEY this project already needs.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("aipq.validators.rag_validator")

_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
_RAGAS_JUDGE_MODEL = "llama-3.3-70b-versatile"


@dataclass
class MetricSummary:
    name: str
    mean_score: Optional[float]


@dataclass
class RagValidationResult:
    prompt_id: int
    version_id: Optional[int]
    total_cases: int
    rag_applicable_cases: int
    metrics: list[MetricSummary]
    overall_score: Optional[float]  # 0-1, mean of the 4 RAGAS metrics
    interpretation: str


def _ragas_pipeline_available() -> tuple[bool, str]:
    import os

    if not os.getenv("GROQ_API_KEY"):
        return False, "GROQ_API_KEY not set — rag_quality layer needs an LLM judge to run RAGAS metrics."
    try:
        import ragas  # noqa: F401
        import datasets  # noqa: F401
        import langchain_groq  # noqa: F401
        import langchain_community.embeddings  # noqa: F401
    except ImportError as exc:
        return False, f"RAGAS dependency not installed ({exc}) — see ai-engine/requirements.txt."
    return True, ""


class RAGValidator:
    async def validate(self, prompt_id: int) -> RagValidationResult:
        from db import get_pool
        from llm_judge import run_prompt_under_test

        pool = await get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                "SELECT pv.id, pv.content FROM prompts p JOIN prompt_versions pv ON pv.id = p.current_version_id "
                "WHERE p.id = $1",
                prompt_id,
            )
            if current is None:
                return RagValidationResult(prompt_id, None, 0, 0, [], None, "No deployed version to validate.")

            case_rows = await conn.fetch(
                """
                SELECT id, input_text, expected_behavior, retrieval_context
                FROM golden_cases WHERE dataset_id = (
                    SELECT id FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1
                )
                """,
                prompt_id,
            )

        total = len(case_rows)

        def _context(row) -> Optional[list[str]]:
            ctx = row["retrieval_context"]
            if isinstance(ctx, str):
                ctx = json.loads(ctx) if ctx else None
            return ctx or None

        rag_cases = [r for r in case_rows if _context(r)]
        if not rag_cases:
            return RagValidationResult(
                prompt_id, current["id"], total, 0, [], None,
                "No golden case has a retrieval_context configured — rag_quality is not applicable to this prompt.",
            )

        available, reason = _ragas_pipeline_available()
        if not available:
            return RagValidationResult(prompt_id, current["id"], total, len(rag_cases), [], None, reason)

        questions, answers, contexts, ground_truths = [], [], [], []
        for row in rag_cases:
            try:
                output = await run_prompt_under_test(current["content"], row["input_text"])
            except Exception as exc:
                logger.warning("rag_validator: prompt run failed for case %d: %s", row["id"], exc)
                continue
            questions.append(row["input_text"])
            answers.append(output)
            contexts.append(_context(row))
            ground_truths.append(row["expected_behavior"])

        if not questions:
            return RagValidationResult(
                prompt_id, current["id"], total, len(rag_cases), [], None,
                "Every prompt run failed for the RAG-applicable cases — see logs.",
            )

        try:
            from datasets import Dataset
            from langchain_community.embeddings import HuggingFaceEmbeddings
            from langchain_groq import ChatGroq
            from ragas import evaluate
            from ragas.embeddings import LangchainEmbeddingsWrapper
            from ragas.llms import LangchainLLMWrapper
            from ragas.metrics import answer_correctness, context_precision, context_recall, faithfulness

            judge_llm = LangchainLLMWrapper(ChatGroq(model=_RAGAS_JUDGE_MODEL, temperature=0.0))
            judge_embeddings = LangchainEmbeddingsWrapper(HuggingFaceEmbeddings(model_name=_EMBEDDING_MODEL))

            dataset = Dataset.from_dict({
                "question": questions, "answer": answers, "contexts": contexts, "ground_truth": ground_truths,
            })

            result = evaluate(
                dataset,
                metrics=[context_precision, context_recall, faithfulness, answer_correctness],
                llm=judge_llm, embeddings=judge_embeddings,
            )
            scored = result.to_pandas()
        except Exception as exc:
            logger.warning("RAGAS evaluation failed for prompt %d: %s", prompt_id, exc)
            return RagValidationResult(
                prompt_id, current["id"], total, len(rag_cases), [], None,
                f"RAGAS evaluation failed: {exc}",
            )

        metrics = []
        contributing = []
        for name in ("context_precision", "context_recall", "faithfulness", "answer_correctness"):
            if name not in scored.columns:
                metrics.append(MetricSummary(name, None))
                continue
            values = [v for v in scored[name].tolist() if v is not None and v == v]  # drop NaN
            mean = round(sum(values) / len(values), 4) if values else None
            metrics.append(MetricSummary(name, mean))
            if mean is not None:
                contributing.append(mean)

        overall = round(sum(contributing) / len(contributing), 4) if contributing else None
        interpretation = (
            f"{len(questions)}/{len(rag_cases)} RAG-applicable case(s) scored "
            f"(of {total} total golden cases). "
            + (f"Overall rag_quality score: {overall:.2f}." if overall is not None
               else "RAGAS ran but produced no usable scores.")
        )

        return RagValidationResult(
            prompt_id=prompt_id, version_id=current["id"], total_cases=total,
            rag_applicable_cases=len(rag_cases), metrics=metrics, overall_score=overall,
            interpretation=interpretation,
        )
