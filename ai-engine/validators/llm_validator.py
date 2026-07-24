"""
LLMValidator — deepeval's own metric library (FaithfulnessMetric,
AnswerRelevancyMetric, HallucinationMetric, GEval) run against a prompt's
currently deployed version, as the "llm_quality" layer of
completeness_engine.CompletenessEngine.

This is deliberately separate from llm_judge.py / evaluators/scoring.py's
existing GEval-only judging (see llm_judge.py's module docstring: those two
custom GEval criteria — "Faithfulness"/"Compliance" — are a workaround for
FaithfulnessMetric and HallucinationMetric both requiring RAG-style
retrieval_context, which most AIPQ golden_cases don't have). This module
runs the real deepeval metric classes wherever a case's data supports them:

  AnswerRelevancyMetric — input + actual_output only, always runs.
  GEval (compliance)    — input + actual_output + expected_output, always
                           runs (same criteria as evaluators/scoring.py, for
                           a like-for-like compliance number across modules).
  FaithfulnessMetric     — needs retrieval_context (golden_cases.retrieval_context,
  HallucinationMetric      migration V13). Skipped per-case, not faked, when a
                           case has none — see _CaseScore.applicable.

A case with no retrieval_context still gets a real llm_quality contribution
from AnswerRelevancy + GEval; it just can't speak to faithfulness/hallucination,
which is reported honestly via each metric's `applicable_cases` count rather
than averaging in a manufactured score.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("aipq.validators.llm_validator")

RELEVANCY_THRESHOLD = 0.7
FAITHFULNESS_THRESHOLD = 0.7
HALLUCINATION_MAX = 0.3  # HallucinationMetric scores 0=no hallucination..1=hallucinated; lower is better
COMPLIANCE_THRESHOLD = 0.7


@dataclass
class MetricSummary:
    name: str
    mean_score: Optional[float]
    applicable_cases: int
    total_cases: int
    passed: Optional[bool]


@dataclass
class LLMValidationResult:
    prompt_id: int
    version_id: Optional[int]
    total_cases: int
    metrics: list[MetricSummary]
    overall_score: Optional[float]  # 0-1, mean of metrics that had >=1 applicable case
    interpretation: str


class LLMValidator:
    async def validate(self, prompt_id: int) -> LLMValidationResult:
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
                return LLMValidationResult(prompt_id, None, 0, [], None, "No deployed version to validate.")

            case_rows = await conn.fetch(
                """
                SELECT id, input_text, expected_behavior, retrieval_context
                FROM golden_cases WHERE dataset_id = (
                    SELECT id FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1
                )
                """,
                prompt_id,
            )

        if not case_rows:
            return LLMValidationResult(
                prompt_id, current["id"], 0, [], None, "No golden cases to validate against.",
            )

        try:
            from deepeval.metrics import GEval, HallucinationMetric, AnswerRelevancyMetric, FaithfulnessMetric
            from deepeval.test_case import LLMTestCase, LLMTestCaseParams
            from llm_judge import GroqDeepEvalModel
        except ImportError:
            return LLMValidationResult(
                prompt_id, current["id"], len(case_rows), [], None,
                "deepeval not installed — cannot run llm_quality layer.",
            )

        judge_model = GroqDeepEvalModel()
        compliance_judge = GEval(
            name="Compliance",
            criteria="Does the ACTUAL_OUTPUT match the EXPECTED_OUTPUT behavior description?",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.EXPECTED_OUTPUT],
            model=judge_model,
        )
        relevancy_metric = AnswerRelevancyMetric(threshold=RELEVANCY_THRESHOLD, model=judge_model, include_reason=False)
        faithfulness_metric = FaithfulnessMetric(threshold=FAITHFULNESS_THRESHOLD, model=judge_model, include_reason=False)
        hallucination_metric = HallucinationMetric(threshold=HALLUCINATION_MAX, model=judge_model, include_reason=False)

        compliance_scores: list[float] = []
        relevancy_scores: list[float] = []
        faithfulness_scores: list[float] = []
        hallucination_scores: list[float] = []

        for case in case_rows:
            context = case["retrieval_context"]
            if isinstance(context, str):
                context = json.loads(context) if context else None

            try:
                output = await run_prompt_under_test(current["content"], case["input_text"])
            except Exception as exc:
                logger.warning("llm_validator: prompt run failed for case %d: %s", case["id"], exc)
                continue

            test_case = LLMTestCase(
                input=case["input_text"], actual_output=output, expected_output=case["expected_behavior"],
                retrieval_context=context or None, context=context or None,
            )

            # a_measure(), not measure(): this method runs inside FastAPI/uvloop's
            # already-running event loop, and deepeval's synchronous measure()
            # calls nest_asyncio.apply() internally to patch that loop so it can
            # run its own async work — which nest_asyncio cannot do to a uvloop
            # loop ("Can't patch loop of type uvloop.Loop"), only to the stdlib
            # asyncio loop. a_measure() is deepeval's own async entry point and
            # sidesteps that patch entirely.
            try:
                await compliance_judge.a_measure(test_case)
                compliance_scores.append(compliance_judge.score)
            except Exception as exc:
                logger.warning("GEval compliance failed for case %d: %s", case["id"], exc)

            try:
                await relevancy_metric.a_measure(test_case)
                relevancy_scores.append(relevancy_metric.score)
            except Exception as exc:
                logger.warning("AnswerRelevancyMetric failed for case %d: %s", case["id"], exc)

            if context:
                try:
                    await faithfulness_metric.a_measure(test_case)
                    faithfulness_scores.append(faithfulness_metric.score)
                except Exception as exc:
                    logger.warning("FaithfulnessMetric failed for case %d: %s", case["id"], exc)
                try:
                    await hallucination_metric.a_measure(test_case)
                    hallucination_scores.append(hallucination_metric.score)
                except Exception as exc:
                    logger.warning("HallucinationMetric failed for case %d: %s", case["id"], exc)

        total = len(case_rows)

        def _summary(name: str, scores: list[float], threshold: float, higher_is_better: bool) -> MetricSummary:
            if not scores:
                return MetricSummary(name, None, 0, total, None)
            mean = sum(scores) / len(scores)
            passed = (mean >= threshold) if higher_is_better else (mean <= threshold)
            return MetricSummary(name, round(mean, 4), len(scores), total, passed)

        metrics = [
            _summary("compliance", compliance_scores, COMPLIANCE_THRESHOLD, True),
            _summary("answer_relevancy", relevancy_scores, RELEVANCY_THRESHOLD, True),
            _summary("faithfulness", faithfulness_scores, FAITHFULNESS_THRESHOLD, True),
            _summary("hallucination", hallucination_scores, HALLUCINATION_MAX, False),
        ]

        # hallucination is inverted (lower=better) — flip it to a 0..1 "good" score before averaging
        contributing = []
        for m in metrics:
            if m.mean_score is None:
                continue
            contributing.append(1.0 - m.mean_score if m.name == "hallucination" else m.mean_score)

        overall = round(sum(contributing) / len(contributing), 4) if contributing else None

        context_cases = sum(1 for c in case_rows if c["retrieval_context"])
        interpretation = (
            f"{total} case(s) evaluated ({context_cases} with retrieval_context). "
            + (f"Overall llm_quality score: {overall:.2f}." if overall is not None
               else "No metric produced a usable score.")
        )

        return LLMValidationResult(
            prompt_id=prompt_id, version_id=current["id"], total_cases=total,
            metrics=metrics, overall_score=overall, interpretation=interpretation,
        )
