"""
POST /drift/record — SDK's report_usage() ingestion.
GET  /drift/status — root-cause query, meant for cross-system callers like
                     AIMO ("did this prompt change recently, or is this
                     model drift?"). Deliberately NOT restricted to the
                     querying credential's own project — this endpoint's
                     whole purpose is cross-system observability, unlike
                     every other route in this service.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from auth.dependencies import AuthContext, get_auth_context
from models.schemas import DriftRecordRequest, DriftStatusResponse

router = APIRouter(prefix="/drift", tags=["drift"])


@router.post("/record", status_code=status.HTTP_201_CREATED)
async def record_drift_sample(
    payload: DriftRecordRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Called by the SDK's report_usage() after each real prompt usage.

    The SDK reports a single aggregate quality_score (not AIPQ's own
    faithfulness/compliance split), so that one score is written to both
    columns — this is an approximation, not a real two-metric evaluation.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        version_row = await conn.fetchrow(
            """
            SELECT pv.id, p.project_id FROM prompt_versions pv
            JOIN prompts p ON p.id = pv.prompt_id
            WHERE pv.id = $1
            """,
            payload.prompt_version_id,
        )
        if version_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "prompt_version not found")
        if auth.project_id != version_row["project_id"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")

        score = payload.quality_score if payload.quality_score is not None else 1.0
        await conn.execute(
            """
            INSERT INTO drift_records
                (prompt_version_id, faithfulness_score, compliance_score, response_length, token_count, sample_size)
            VALUES ($1, $2, $2, $3, $4, 1)
            """,
            payload.prompt_version_id, score, len(payload.output), len(payload.output.split()),
        )
    return {"recorded": True}


@router.get("/status", response_model=DriftStatusResponse)
async def drift_status(
    request: Request,
    project_id: int = Query(...),
    prompt_name: str = Query(...),
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow(
            "SELECT id FROM prompts WHERE project_id = $1 AND prompt_name = $2",
            project_id, prompt_name,
        )
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found for this project")
        prompt_id = prompt_row["id"]

        current = await conn.fetchrow(
            """
            SELECT pv.id, pv.version_number, pv.quality_score, pv.deployed_at
            FROM prompts p JOIN prompt_versions pv ON pv.id = p.current_version_id
            WHERE p.id = $1
            """,
            prompt_id,
        )

        latest_drift = None
        if current is not None:
            latest_drift = await conn.fetchrow(
                """
                SELECT drift_severity FROM drift_records
                WHERE prompt_version_id = $1
                ORDER BY recorded_at DESC LIMIT 1
                """,
                current["id"],
            )

    if current is None:
        return DriftStatusResponse(
            prompt_id=prompt_id, prompt_name=prompt_name, current_version_id=None,
            current_version_number=None, deployed_at=None, quality_score=None,
            recent_drift_severity=None, changed_recently=False,
            root_cause_hint="No deployed version for this prompt yet.",
        )

    severity = latest_drift["drift_severity"] if latest_drift else None
    changed_recently = (
        current["deployed_at"] is not None
        and current["deployed_at"] >= datetime.now(timezone.utc) - timedelta(days=7)
    )

    if severity in ("HIGH", "CRITICAL") and changed_recently:
        hint = (
            f"Prompt v{current['version_number']} deployed within the last 7 days and quality has "
            f"dropped ({severity}) — likely caused by that prompt change. Rollback recommended."
        )
    elif severity in ("HIGH", "CRITICAL") and not changed_recently:
        hint = (
            f"Quality has dropped ({severity}) but the prompt hasn't changed recently — "
            f"likely caused by underlying model drift, not the prompt itself."
        )
    else:
        hint = "No significant drift detected."

    return DriftStatusResponse(
        prompt_id=prompt_id, prompt_name=prompt_name, current_version_id=current["id"],
        current_version_number=current["version_number"], deployed_at=current["deployed_at"],
        quality_score=current["quality_score"], recent_drift_severity=severity,
        changed_recently=changed_recently, root_cause_hint=hint,
    )
