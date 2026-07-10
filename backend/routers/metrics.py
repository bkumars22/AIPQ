"""
GET /metrics/business — aggregated data for the dashboard's Business
Metrics page. Cross-project (like GET /projects), any authenticated
credential can read it.

Some of what this page shows has no real signal anywhere in AIPQ's schema
(nobody tracks how long a manual prompt-iteration pass takes, or how many
end-user sessions a deployment serves) — those figures are clearly
labeled ASSUMED_* constants below, combined with real counts from the
database, rather than either fabricating a fully "real" number or
refusing to show the metric at all. Every other figure here (blocked
deployment counts, real rollback timing, real quality trend) is a live
query, not a fixture.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Request

from auth.dependencies import AuthContext, get_auth_context
from config import ai_engine_url

logger = logging.getLogger("aipq.backend.metrics")
router = APIRouter(prefix="/metrics", tags=["metrics"])

AI_ENGINE_URL = ai_engine_url()

# ── Assumed constants (no real tracked signal exists for these in AIPQ) ────
ASSUMED_MANUAL_MINUTES_PER_ITERATION = 30   # a human iterate-eval-review pass
ASSUMED_AIPQ_MINUTES_PER_ITERATION = 2       # observed automated eval turnaround
ASSUMED_SESSIONS_PER_DEPLOYMENT = 1000       # end-user sessions a typical deployment serves
ASSUMED_MANUAL_ROLLBACK_MINUTES = 180        # industry "2-4 hours" -> 3hr midpoint


async def _fetch_from_ai_engine(path: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{AI_ENGINE_URL}{path}")
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for %s: %s", path, exc)
        return {}


@router.get("/business")
async def business_metrics(request: Request, auth: AuthContext = Depends(get_auth_context)):
    pool = request.app.state.pg_pool
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    async with pool.acquire() as conn:
        iterations_this_month = await conn.fetchval(
            "SELECT COUNT(*) FROM evaluations WHERE evaluated_at >= $1", month_start
        )

        blocked_count = await conn.fetchval(
            "SELECT COUNT(*) FROM evaluations WHERE blocked_deployment = TRUE AND evaluated_at >= $1",
            month_start,
        )
        avg_degradation_row = await conn.fetchrow(
            """
            SELECT AVG(GREATEST(gd.threshold - e.compliance_score, 0)) AS avg_degradation
            FROM evaluations e
            JOIN golden_datasets gd ON gd.id = e.golden_dataset_id
            WHERE e.blocked_deployment = TRUE AND e.evaluated_at >= $1
            """,
            month_start,
        )
        avg_degradation = avg_degradation_row["avg_degradation"] or 0.0

        rollback_avg_row = await conn.fetchrow(
            """
            SELECT AVG(EXTRACT(EPOCH FROM (resolved_at - triggered_at)) / 60.0) AS avg_minutes,
                   COUNT(*) AS n
            FROM rollbacks WHERE triggered_by = 'AUTOMATIC' AND resolved_at IS NOT NULL
            """
        )

        trend_rows = await conn.fetch(
            """
            SELECT proj.name AS project_name, date_trunc('day', dr.recorded_at) AS day,
                   AVG(dr.compliance_score) AS avg_score
            FROM drift_records dr
            JOIN prompt_versions pv ON pv.id = dr.prompt_version_id
            JOIN prompts p ON p.id = pv.prompt_id
            JOIN projects proj ON proj.id = p.project_id
            WHERE dr.recorded_at >= $1
            GROUP BY proj.name, day
            ORDER BY day ASC
            """,
            datetime.now(timezone.utc) - timedelta(days=30),
        )

        registered_projects = [r["name"] for r in await conn.fetch("SELECT name FROM projects ORDER BY name")]

    manual_minutes = iterations_this_month * ASSUMED_MANUAL_MINUTES_PER_ITERATION
    automated_minutes = iterations_this_month * ASSUMED_AIPQ_MINUTES_PER_ITERATION
    saved_minutes = manual_minutes - automated_minutes
    saved_pct = round(saved_minutes / manual_minutes * 100, 1) if manual_minutes else 0.0

    estimated_impact_prevented = round(blocked_count * ASSUMED_SESSIONS_PER_DEPLOYMENT * avg_degradation, 1)

    aipq_rollback_minutes = round(rollback_avg_row["avg_minutes"], 2) if rollback_avg_row["avg_minutes"] is not None else None
    rollback_improvement_pct = (
        round((ASSUMED_MANUAL_ROLLBACK_MINUTES - aipq_rollback_minutes) / ASSUMED_MANUAL_ROLLBACK_MINUTES * 100, 1)
        if aipq_rollback_minutes is not None else None
    )

    quality_trend: dict[str, list[dict]] = {name: [] for name in registered_projects}
    for row in trend_rows:
        quality_trend.setdefault(row["project_name"], []).append({
            "date": row["day"].date().isoformat(), "avg_score": round(row["avg_score"], 4),
        })

    coverage_gaps_data = await _fetch_from_ai_engine("/analyze/coverage-gaps")
    predictions_data = await _fetch_from_ai_engine("/analyze/predictions")

    return {
        "time_saved": {
            "iterations_this_month": iterations_this_month,
            "manual_minutes": manual_minutes,
            "automated_minutes": automated_minutes,
            "saved_minutes": saved_minutes,
            "saved_pct": saved_pct,
            "assumptions": {
                "manual_minutes_per_iteration": ASSUMED_MANUAL_MINUTES_PER_ITERATION,
                "aipq_minutes_per_iteration": ASSUMED_AIPQ_MINUTES_PER_ITERATION,
            },
        },
        "incidents_prevented": {
            "blocked_deployments": blocked_count,
            "avg_degradation_prevented": round(avg_degradation, 4),
            "estimated_impact_prevented": estimated_impact_prevented,
            "assumptions": {"sessions_per_deployment": ASSUMED_SESSIONS_PER_DEPLOYMENT},
        },
        "rollback_speed": {
            "manual_baseline_minutes": ASSUMED_MANUAL_ROLLBACK_MINUTES,
            "aipq_avg_minutes": aipq_rollback_minutes,
            "improvement_pct": rollback_improvement_pct,
            "automatic_rollback_count": rollback_avg_row["n"],
        },
        "quality_trend": quality_trend,
        "coverage_gaps": coverage_gaps_data.get("gaps", []),
        "predictions": predictions_data.get("predictions", []),
    }
