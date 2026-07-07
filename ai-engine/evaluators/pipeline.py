"""
AIPQ's 5-node LangGraph evaluation pipeline — runs whenever a prompt version
is created, decides whether it's good enough to deploy.

Node 1 load_test_cases        -> load prompt content + golden cases
Node 2 run_deterministic_checks -> forbidden/required pattern checks (no LLM)
Node 3 run_deepeval_scoring    -> GEval faithfulness + compliance (cached 1h)
Node 4 calculate_aggregate     -> pass rate, per-category breakdown, verdict
Node 5 store_and_decide        -> deploy or keep previous version deployed

LangSmith tracing: this graph is picked up automatically by LangGraph's
built-in tracing when LANGCHAIN_TRACING_V2=true is set (see .env.example) —
no per-node instrumentation needed beyond that.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from db import get_pool, get_redis
from llm_judge import GroqDeepEvalModel, run_prompt_under_test

logger = logging.getLogger("aipq.pipeline")

_CACHE_TTL_SECONDS = 3600
_judge_model = GroqDeepEvalModel()


class EvalState(TypedDict):
    version_id: int
    prompt_id: int
    golden_dataset_id: int
    threshold: float
    prompt_content: str
    test_cases: list[dict[str, Any]]
    deterministic_results: list[dict[str, Any]]
    deepeval_results: list[dict[str, Any]]
    evaluation_summary: dict[str, Any]
    error: str


# --- Node 1 -------------------------------------------------------------

async def load_test_cases(state: EvalState) -> EvalState:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            version_row = await conn.fetchrow(
                "SELECT prompt_id, content FROM prompt_versions WHERE id = $1", state["version_id"]
            )
            if version_row is None:
                return {**state, "error": f"prompt_version {state['version_id']} not found"}

            dataset_row = await conn.fetchrow(
                "SELECT id, threshold FROM golden_datasets WHERE id = $1", state["golden_dataset_id"]
            )
            if dataset_row is None:
                return {**state, "error": f"golden_dataset {state['golden_dataset_id']} not found"}

            case_rows = await conn.fetch(
                """
                SELECT id, input_text, expected_behavior, forbidden_patterns, required_patterns, category
                FROM golden_cases WHERE dataset_id = $1
                """,
                state["golden_dataset_id"],
            )

        cases = [
            {
                "id": r["id"],
                "input_text": r["input_text"],
                "expected_behavior": r["expected_behavior"],
                "forbidden_patterns": json.loads(r["forbidden_patterns"]) if isinstance(r["forbidden_patterns"], str) else r["forbidden_patterns"],
                "required_patterns": json.loads(r["required_patterns"]) if isinstance(r["required_patterns"], str) else r["required_patterns"],
                "category": r["category"],
            }
            for r in case_rows
        ]

        return {
            **state,
            "prompt_id": version_row["prompt_id"],
            "prompt_content": version_row["content"],
            "threshold": state["threshold"] or dataset_row["threshold"],
            "test_cases": cases,
        }
    except Exception as exc:
        logger.exception("load_test_cases failed")
        return {**state, "error": f"load_test_cases: {exc}"}


# --- Node 2 -------------------------------------------------------------

async def run_deterministic_checks(state: EvalState) -> EvalState:
    if state.get("error"):
        return state
    try:
        results = []
        for case in state["test_cases"]:
            output = await run_prompt_under_test(state["prompt_content"], case["input_text"])
            output_lower = output.lower()

            forbidden_hit = next((p for p in case["forbidden_patterns"] if p.lower() in output_lower), None)
            missing_required = [p for p in case["required_patterns"] if p.lower() not in output_lower]

            deterministic_passed = forbidden_hit is None and not missing_required
            results.append({
                "case_id": case["id"],
                "category": case["category"],
                "output": output,
                "deterministic_passed": deterministic_passed,
                "forbidden_hit": forbidden_hit,
                "missing_required": missing_required,
            })
        return {**state, "deterministic_results": results}
    except Exception as exc:
        logger.exception("run_deterministic_checks failed")
        return {**state, "error": f"run_deterministic_checks: {exc}"}


# --- Node 3 -------------------------------------------------------------

def _cache_key(prompt_content: str, case_id: int) -> str:
    digest = hashlib.sha256(prompt_content.encode("utf-8")).hexdigest()[:16]
    return f"aipq:deepeval:{digest}:{case_id}"


async def run_deepeval_scoring(state: EvalState) -> EvalState:
    if state.get("error"):
        return state
    try:
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        redis = get_redis()
        results = []
        by_case = {c["id"]: c for c in state["test_cases"]}

        faithfulness_judge = GEval(
            name="Faithfulness",
            criteria="Does the ACTUAL_OUTPUT stay faithful to the rules and persona described in the "
                     "system prompt, without contradicting or ignoring explicit instructions?",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=_judge_model,
        )
        compliance_judge = GEval(
            name="Compliance",
            criteria="Does the ACTUAL_OUTPUT match the EXPECTED_OUTPUT behavior description?",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.EXPECTED_OUTPUT],
            model=_judge_model,
        )

        for det in state["deterministic_results"]:
            case = by_case[det["case_id"]]

            if not det["deterministic_passed"]:
                # Forbidden pattern hit or required pattern missing -> automatic 0, no LLM judge call needed
                results.append({"case_id": case["id"], "faithfulness_score": 0.0, "compliance_score": 0.0})
                continue

            cache_key = _cache_key(state["prompt_content"], case["id"])
            cached = await redis.get(cache_key)
            if cached:
                results.append(json.loads(cached))
                continue

            test_case = LLMTestCase(
                input=case["input_text"],
                actual_output=det["output"],
                expected_output=case["expected_behavior"],
            )
            faithfulness_judge.measure(test_case)
            compliance_judge.measure(test_case)

            entry = {
                "case_id": case["id"],
                "faithfulness_score": faithfulness_judge.score,
                "compliance_score": compliance_judge.score,
            }
            await redis.set(cache_key, json.dumps(entry), ex=_CACHE_TTL_SECONDS)
            results.append(entry)

        return {**state, "deepeval_results": results}
    except ImportError:
        logger.warning("deepeval not installed — skipping LLM-judge scoring, using deterministic pass/fail only")
        fallback = [
            {"case_id": d["case_id"], "faithfulness_score": 1.0 if d["deterministic_passed"] else 0.0,
             "compliance_score": 1.0 if d["deterministic_passed"] else 0.0}
            for d in state["deterministic_results"]
        ]
        return {**state, "deepeval_results": fallback}
    except Exception as exc:
        logger.exception("run_deepeval_scoring failed")
        return {**state, "error": f"run_deepeval_scoring: {exc}"}


# --- Node 4 -------------------------------------------------------------

async def calculate_aggregate(state: EvalState) -> EvalState:
    if state.get("error"):
        return state
    try:
        det_by_case = {d["case_id"]: d for d in state["deterministic_results"]}
        deep_by_case = {d["case_id"]: d for d in state["deepeval_results"]}

        total = len(state["test_cases"])
        faithfulness_scores = [deep_by_case[c["id"]]["faithfulness_score"] for c in state["test_cases"]]
        compliance_scores = [deep_by_case[c["id"]]["compliance_score"] for c in state["test_cases"]]

        avg_faithfulness = sum(faithfulness_scores) / total if total else 0.0
        avg_compliance = sum(compliance_scores) / total if total else 0.0

        failed_case_ids = [
            c["id"] for c in state["test_cases"]
            if not det_by_case[c["id"]]["deterministic_passed"] or deep_by_case[c["id"]]["compliance_score"] < state["threshold"]
        ]
        passed_count = total - len(failed_case_ids)

        by_category: dict[str, dict[str, int]] = {}
        for c in state["test_cases"]:
            cat = c["category"]
            by_category.setdefault(cat, {"total": 0, "passed": 0})
            by_category[cat]["total"] += 1
            if c["id"] not in failed_case_ids:
                by_category[cat]["passed"] += 1

        overall_score = avg_compliance
        passed = overall_score >= state["threshold"] and total > 0

        pool = await get_pool()
        async with pool.acquire() as conn:
            prev = await conn.fetchrow(
                """
                SELECT quality_score FROM prompt_versions
                WHERE prompt_id = $1 AND status = 'DEPLOYED'
                ORDER BY version_number DESC LIMIT 1
                """,
                state["prompt_id"],
            )
        previous_score = prev["quality_score"] if prev else None

        summary = {
            "total_cases": total,
            "passed_cases": passed_count,
            "failed_cases": len(failed_case_ids),
            "failed_case_ids": failed_case_ids,
            "faithfulness_score": round(avg_faithfulness, 4),
            "compliance_score": round(avg_compliance, 4),
            "overall_score": round(overall_score, 4),
            "passed": passed,
            "by_category": by_category,
            "previous_score": previous_score,
            "delta_vs_previous": round(overall_score - previous_score, 4) if previous_score is not None else None,
        }
        return {**state, "evaluation_summary": summary}
    except Exception as exc:
        logger.exception("calculate_aggregate failed")
        return {**state, "error": f"calculate_aggregate: {exc}"}


# --- Node 5 -------------------------------------------------------------

async def store_and_decide(state: EvalState) -> EvalState:
    if state.get("error"):
        logger.error("Pipeline errored before store_and_decide: %s", state["error"])
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE prompt_versions SET status = 'FAILED' WHERE id = $1", state["version_id"]
            )
        return state

    summary = state["evaluation_summary"]
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO evaluations
                    (prompt_version_id, golden_dataset_id, total_cases, passed_cases, failed_cases,
                     faithfulness_score, compliance_score, passed, blocked_deployment, details)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                state["version_id"], state["golden_dataset_id"], summary["total_cases"],
                summary["passed_cases"], summary["failed_cases"], summary["faithfulness_score"],
                summary["compliance_score"], summary["passed"], not summary["passed"],
                json.dumps(summary),
            )

            new_status = "DEPLOYED" if summary["passed"] else "FAILED"
            if new_status == "DEPLOYED":
                await conn.execute(
                    """
                    UPDATE prompt_versions
                    SET status = $1, quality_score = $2, deployed_at = now()
                    WHERE id = $3
                    """,
                    new_status, summary["overall_score"], state["version_id"],
                )
            else:
                await conn.execute(
                    "UPDATE prompt_versions SET status = $1, quality_score = $2 WHERE id = $3",
                    new_status, summary["overall_score"], state["version_id"],
                )

            if summary["passed"]:
                await conn.execute(
                    "UPDATE prompts SET current_version_id = $1 WHERE id = $2",
                    state["version_id"], state["prompt_id"],
                )

    logger.info(
        "[version %d] evaluation complete: %s (score=%.3f, threshold=%.3f)",
        state["version_id"], new_status, summary["overall_score"], state["threshold"],
    )

    try:
        redis = get_redis()
        await redis.publish(
            "aipq:dashboard",
            json.dumps({"type": "evaluation_complete", "version_id": state["version_id"],
                        "status": new_status, "score": summary["overall_score"]}),
        )
    except Exception:
        logger.debug("Could not publish dashboard update (Redis pub/sub) — non-fatal", exc_info=True)

    return state


# --- Graph assembly -------------------------------------------------------

def build_graph():
    graph = StateGraph(EvalState)
    graph.add_node("load_test_cases", load_test_cases)
    graph.add_node("run_deterministic_checks", run_deterministic_checks)
    graph.add_node("run_deepeval_scoring", run_deepeval_scoring)
    graph.add_node("calculate_aggregate", calculate_aggregate)
    graph.add_node("store_and_decide", store_and_decide)

    graph.set_entry_point("load_test_cases")
    graph.add_edge("load_test_cases", "run_deterministic_checks")
    graph.add_edge("run_deterministic_checks", "run_deepeval_scoring")
    graph.add_edge("run_deepeval_scoring", "calculate_aggregate")
    graph.add_edge("calculate_aggregate", "store_and_decide")
    graph.add_edge("store_and_decide", END)

    return graph.compile()


async def run_evaluation(version_id: int, prompt_id: int, golden_dataset_id: int, threshold: float) -> dict:
    """Entry point called by ai-engine's /evaluate endpoint."""
    app_graph = build_graph()
    initial: EvalState = {
        "version_id": version_id,
        "prompt_id": prompt_id,
        "golden_dataset_id": golden_dataset_id,
        "threshold": threshold,
        "prompt_content": "",
        "test_cases": [],
        "deterministic_results": [],
        "deepeval_results": [],
        "evaluation_summary": {},
        "error": "",
    }
    final_state = await app_graph.ainvoke(initial)
    return final_state
