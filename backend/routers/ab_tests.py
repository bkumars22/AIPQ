"""
POST /ab-tests                — create an A/B test between two prompt versions
GET  /ab-tests/active          — SDK-facing: weighted-random version assignment for a prompt
POST /ab-tests/{id}/results    — record one sample's outcome; auto-promotes the
                                  winner once min_samples is reached and the
                                  difference is statistically significant
GET  /ab-tests/{id}/results    — current stats, significance, recommendation
POST /ab-tests/{id}/promote    — manually end the test and deploy a version now
"""
from __future__ import annotations

import math
import random
import statistics
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from auth.dependencies import AuthContext, get_auth_context
from models.schemas import (
    ABResultRecordRequest,
    ABTestArmStats,
    ABTestAssignmentResponse,
    ABTestCreateRequest,
    ABTestCreateResponse,
    ABTestPromoteResponse,
    ABTestResultsResponse,
)

router = APIRouter(prefix="/ab-tests", tags=["ab-tests"])

SIGNIFICANCE_LEVEL = 0.05


def _require_own_project(auth: AuthContext, project_id: int) -> None:
    if auth.via == "api_key" and auth.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _welch_p_value(a: list[float], b: list[float]) -> Optional[float]:
    """
    Two-tailed Welch's t-test p-value, using a normal approximation instead of
    the exact Student's-t distribution — avoids adding scipy to this
    lightweight backend service (ai-engine's StatisticalValidator already
    covers the heavier analysis elsewhere). Accurate enough at the sample
    sizes A/B tests here are designed for (min_samples defaults to 100).
    """
    if len(a) < 2 or len(b) < 2:
        return None
    mean_a, mean_b = statistics.mean(a), statistics.mean(b)
    var_a, var_b = statistics.variance(a), statistics.variance(b)
    se = math.sqrt(var_a / len(a) + var_b / len(b))
    if se == 0:
        return 1.0 if mean_a == mean_b else 0.0
    t_stat = (mean_a - mean_b) / se
    return 2 * (1 - _norm_cdf(abs(t_stat)))


async def _load_test(conn, ab_test_id: int):
    row = await conn.fetchrow(
        """
        SELECT t.*, p.project_id
        FROM ab_tests t JOIN prompts p ON p.id = t.prompt_id
        WHERE t.id = $1
        """,
        ab_test_id,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "A/B test not found")
    return row


async def _arm_scores(conn, ab_test_id: int, arm: str) -> list[float]:
    rows = await conn.fetch(
        "SELECT quality_score FROM ab_results WHERE ab_test_id = $1 AND version_used = $2",
        ab_test_id, arm,
    )
    return [r["quality_score"] for r in rows]


async def _deploy_winner(conn, test_row, winner_version_id: int) -> None:
    await conn.execute(
        "UPDATE ab_tests SET status = 'COMPLETED', ended_at = now(), winner_version_id = $2 WHERE id = $1",
        test_row["id"], winner_version_id,
    )
    await conn.execute(
        "UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = $1",
        winner_version_id,
    )
    await conn.execute(
        "UPDATE prompts SET current_version_id = $1 WHERE id = $2",
        winner_version_id, test_row["prompt_id"],
    )


async def _maybe_auto_promote(conn, ab_test_id: int) -> None:
    """Auto-promote the winner once min_samples is reached, per the original
    spec ("Auto-promote winner after N runs") — only when the difference is
    actually statistically significant, otherwise keep the test running."""
    test_row = await conn.fetchrow("SELECT * FROM ab_tests WHERE id = $1", ab_test_id)
    scores_a = await _arm_scores(conn, ab_test_id, "A")
    scores_b = await _arm_scores(conn, ab_test_id, "B")
    p_value = _welch_p_value(scores_a, scores_b)
    if p_value is None or p_value >= SIGNIFICANCE_LEVEL:
        return
    winner_id = test_row["version_a_id"] if statistics.mean(scores_a) > statistics.mean(scores_b) else test_row["version_b_id"]
    await _deploy_winner(conn, test_row, winner_id)


@router.post("", response_model=ABTestCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_ab_test(
    payload: ABTestCreateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", payload.prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        if payload.version_a_id == payload.version_b_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "version_a_id and version_b_id must differ")
        for version_id in (payload.version_a_id, payload.version_b_id):
            v = await conn.fetchrow(
                "SELECT id FROM prompt_versions WHERE id = $1 AND prompt_id = $2",
                version_id, payload.prompt_id,
            )
            if v is None:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"Version {version_id} does not belong to prompt {payload.prompt_id}",
                )

        row = await conn.fetchrow(
            """
            INSERT INTO ab_tests (prompt_id, version_a_id, version_b_id, traffic_split, min_samples)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id, status
            """,
            payload.prompt_id, payload.version_a_id, payload.version_b_id,
            payload.traffic_split, payload.min_samples,
        )

    return ABTestCreateResponse(ab_test_id=row["id"], status=row["status"])


@router.get("/active", response_model=ABTestAssignmentResponse)
async def get_active_assignment(
    request: Request,
    prompt_id: int = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    """
    SDK calls this before generating a response: which version should this
    request use? Weighted-random per traffic_split, not sticky per-user —
    matches the coarse "split traffic" behaviour described in AIPQ's spec.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        test_row = await conn.fetchrow(
            """
            SELECT id, version_a_id, version_b_id, traffic_split
            FROM ab_tests
            WHERE prompt_id = $1 AND status = 'RUNNING'
            ORDER BY started_at DESC LIMIT 1
            """,
            prompt_id,
        )
        if test_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "No running A/B test for this prompt")

        use_a = random.random() < test_row["traffic_split"]
        version_id = test_row["version_a_id"] if use_a else test_row["version_b_id"]
        version = await conn.fetchrow("SELECT id, content FROM prompt_versions WHERE id = $1", version_id)

    return ABTestAssignmentResponse(
        ab_test_id=test_row["id"], version_used="A" if use_a else "B",
        version_id=version["id"], content=version["content"],
    )


@router.post("/{ab_test_id}/results", status_code=status.HTTP_201_CREATED)
async def record_ab_result(
    ab_test_id: int,
    payload: ABResultRecordRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        test_row = await _load_test(conn, ab_test_id)
        _require_own_project(auth, test_row["project_id"])
        if test_row["status"] != "RUNNING":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A/B test is {test_row['status']}, not RUNNING")

        async with conn.transaction():
            await conn.execute(
                "INSERT INTO ab_results (ab_test_id, version_used, quality_score) VALUES ($1, $2, $3)",
                ab_test_id, payload.version_used, payload.quality_score,
            )
            new_count = await conn.fetchval(
                "UPDATE ab_tests SET current_samples = current_samples + 1 WHERE id = $1 RETURNING current_samples",
                ab_test_id,
            )

        if new_count >= test_row["min_samples"]:
            await _maybe_auto_promote(conn, ab_test_id)

    return {"recorded": True, "current_samples": new_count}


@router.get("/{ab_test_id}/results", response_model=ABTestResultsResponse)
async def get_ab_test_results(
    ab_test_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        test_row = await _load_test(conn, ab_test_id)
        _require_own_project(auth, test_row["project_id"])

        version_a = await conn.fetchrow(
            "SELECT id, version_number FROM prompt_versions WHERE id = $1", test_row["version_a_id"]
        )
        version_b = await conn.fetchrow(
            "SELECT id, version_number FROM prompt_versions WHERE id = $1", test_row["version_b_id"]
        )
        scores_a = await _arm_scores(conn, ab_test_id, "A")
        scores_b = await _arm_scores(conn, ab_test_id, "B")

    p_value = _welch_p_value(scores_a, scores_b)
    significant = p_value is not None and p_value < SIGNIFICANCE_LEVEL
    winner_id = test_row["winner_version_id"]
    if winner_id is None and significant:
        winner_id = version_a["id"] if statistics.mean(scores_a) > statistics.mean(scores_b) else version_b["id"]

    pv_str = f"{p_value:.4f}" if p_value is not None else "n/a"
    if test_row["status"] == "COMPLETED":
        recommendation = f"Test completed — version {winner_id} was promoted."
    elif significant:
        recommendation = f"Statistically significant difference found (p={pv_str}) — version {winner_id} is winning. Promote it."
    elif test_row["current_samples"] < test_row["min_samples"]:
        recommendation = f"Collecting samples ({test_row['current_samples']}/{test_row['min_samples']})..."
    else:
        recommendation = f"No statistically significant difference yet (p={pv_str}). Consider running longer."

    return ABTestResultsResponse(
        ab_test_id=test_row["id"], prompt_id=test_row["prompt_id"], status=test_row["status"],
        traffic_split=test_row["traffic_split"], min_samples=test_row["min_samples"],
        current_samples=test_row["current_samples"],
        version_a=ABTestArmStats(
            version_id=version_a["id"], version_number=version_a["version_number"],
            n=len(scores_a), mean_score=statistics.mean(scores_a) if scores_a else None,
            stdev=statistics.stdev(scores_a) if len(scores_a) >= 2 else None,
        ),
        version_b=ABTestArmStats(
            version_id=version_b["id"], version_number=version_b["version_number"],
            n=len(scores_b), mean_score=statistics.mean(scores_b) if scores_b else None,
            stdev=statistics.stdev(scores_b) if len(scores_b) >= 2 else None,
        ),
        p_value=p_value, significant=significant, winner_version_id=winner_id,
        recommendation=recommendation,
    )


@router.post("/{ab_test_id}/promote", response_model=ABTestPromoteResponse)
async def promote_ab_test(
    ab_test_id: int,
    request: Request,
    version: str = Query(..., pattern="^[AB]$", description="Which arm to promote as the winner"),
    auth: AuthContext = Depends(get_auth_context),
):
    """Manual "Promote winner" button — ends the test immediately regardless
    of significance/min_samples, deploying the chosen arm."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        test_row = await _load_test(conn, ab_test_id)
        _require_own_project(auth, test_row["project_id"])
        if test_row["status"] != "RUNNING":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"A/B test is already {test_row['status']}")

        winner_id = test_row["version_a_id"] if version == "A" else test_row["version_b_id"]
        await _deploy_winner(conn, test_row, winner_id)

    return ABTestPromoteResponse(promoted_version_id=winner_id, status="COMPLETED")
