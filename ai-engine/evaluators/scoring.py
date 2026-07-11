"""
score_content() — scores an arbitrary prompt string against a set of golden
cases, without creating a prompt_version row or writing to the evaluations
table.

This is the DB-free core of pipeline.py's Nodes 2-4 (deterministic checks,
GEval scoring, aggregation), extracted so the causal attribution analyzer
(analyzers/causal.py) and portability validator (validators/portability.py)
can re-score ad-hoc content variants — a counterfactual with one factor
reverted, or the same content run through a different provider — without
polluting prompt_versions with a fake "test" row every time they need a
comparison score.

pipeline.py's own Nodes 2-4 are deliberately NOT refactored to call this:
that file is the live, already-verified deployment-gating evaluation path,
and duplicating this logic here is a smaller risk than making it depend on
new shared code.
"""
from __future__ import annotations

import logging

from providers import run_prompt_on_provider

logger = logging.getLogger("aipq.evaluators.scoring")


async def score_content(
    content: str, test_cases: list[dict], threshold: float = 0.85, provider: str = "groq",
) -> dict:
    """
    Runs `content` as the system prompt against every test case's input on
    the given provider, judges each response the same way pipeline.py's
    run_deepeval_scoring does, and returns an aggregate result — nothing
    is persisted.
    """
    per_case: list[dict] = []

    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams
        from llm_judge import GroqDeepEvalModel

        judge_model = GroqDeepEvalModel()
        faithfulness_judge = GEval(
            name="Faithfulness",
            criteria="Does the ACTUAL_OUTPUT stay faithful to the rules and persona described in the "
                     "system prompt, without contradicting or ignoring explicit instructions?",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=judge_model,
        )
        compliance_judge = GEval(
            name="Compliance",
            criteria="Does the ACTUAL_OUTPUT match the EXPECTED_OUTPUT behavior description?",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.EXPECTED_OUTPUT],
            model=judge_model,
        )
        deepeval_available = True
    except ImportError:
        logger.warning("deepeval not installed — falling back to deterministic-only scoring")
        deepeval_available = False

    for case in test_cases:
        output = await run_prompt_on_provider(provider, content, case["input_text"])
        output_lower = output.lower()

        forbidden_hit = next((p for p in case.get("forbidden_patterns", []) if p.lower() in output_lower), None)
        missing_required = [p for p in case.get("required_patterns", []) if p.lower() not in output_lower]
        deterministic_passed = forbidden_hit is None and not missing_required

        if not deterministic_passed or not deepeval_available:
            score = 1.0 if (deterministic_passed and not deepeval_available) else 0.0
            per_case.append({"case_id": case["id"], "faithfulness_score": score, "compliance_score": score})
            continue

        test_case = LLMTestCase(input=case["input_text"], actual_output=output, expected_output=case["expected_behavior"])
        faithfulness_judge.measure(test_case)
        compliance_judge.measure(test_case)
        per_case.append({
            "case_id": case["id"],
            "faithfulness_score": faithfulness_judge.score,
            "compliance_score": compliance_judge.score,
        })

    total = len(per_case)
    avg_compliance = sum(c["compliance_score"] for c in per_case) / total if total else 0.0
    avg_faithfulness = sum(c["faithfulness_score"] for c in per_case) / total if total else 0.0

    return {
        "overall_score": round(avg_compliance, 4),
        "faithfulness_score": round(avg_faithfulness, 4),
        "compliance_score": round(avg_compliance, 4),
        "passed": avg_compliance >= threshold and total > 0,
        "total_cases": total,
        "per_case": per_case,
    }
