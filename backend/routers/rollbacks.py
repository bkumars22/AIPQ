"""
Rollback history and the dashboard's rollback action.

GET  /prompts/{id}/rollbacks  -- version history, rollback history (who, why) and which versions may be restored.
                                 Read-only; always available.
POST /prompts/{id}/rollback   -- restore a previously passing version. Off unless QCP_ENABLED is set (403 otherwise).

The action goes through rollback_ops.request_targeted_rollback to the ai-engine, which holds the one implementation of
the safety rules (only a version that previously passed the quality gate may be restored). The `can_roll_back_to` flags
returned by GET are a hint for the UI; the ai-engine is what actually enforces them.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from auth.dependencies import AuthContext, get_auth_context
from config import qcp_enabled
from rollback_ops import request_targeted_rollback, requester

router = APIRouter(tags=["rollbacks"])

# Same rule the ai-engine enforces (ai-engine/rollback.py PASSED_STATUSES), used here only to label versions.
_PASSED = ("DEPLOYED", "ROLLED_BACK")


class VersionRow(BaseModel):
    version_number: int
    status: str
    quality_score: float | None
    deployed_at: datetime | None
    changed_by: str
    change_message: str | None
    is_current: bool
    can_roll_back_to: bool
    why_not: str | None


class RollbackRow(BaseModel):
    id: int
    triggered_at: datetime
    resolved_at: datetime | None
    triggered_by: str  # AUTOMATIC | MANUAL
    from_version_number: int
    to_version_number: int
    requested_by: str | None  # only manual rollbacks record a requester
    reason: str | None


class RollbackHistoryResponse(BaseModel):
    prompt_id: int
    prompt_name: str
    targeted_rollback_enabled: bool
    current_version_number: int | None
    versions: list[VersionRow]
    rollbacks: list[RollbackRow]


class DashboardRollbackRequest(BaseModel):
    to_version_number: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)
    requested_by: str | None = Field(default=None, max_length=120)  # a name, recorded beside the authenticated caller


def _why_not(v: dict[str, Any], current_id: int | None) -> str | None:
    if v["id"] == current_id:
        return "This is the deployed version."
    if v["status"] == "FAILED":
        return "Failed the quality gate and was never deployed."
    if v["status"] == "TESTING":
        return "Still being evaluated."
    if v["status"] not in _PASSED or v["quality_score"] is None:
        return "No passing quality score is recorded."
    return None


async def _owned_prompt(request: Request, auth: AuthContext, prompt_id: int) -> dict[str, Any]:
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT project_id, prompt_name, current_version_id FROM prompts WHERE id = $1", prompt_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
    if auth.via == "api_key" and auth.project_id != row["project_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")
    return dict(row)


@router.get("/prompts/{prompt_id}/rollbacks", response_model=RollbackHistoryResponse)
async def get_rollback_history(prompt_id: int, request: Request, auth: AuthContext = Depends(get_auth_context)):
    prompt = await _owned_prompt(request, auth, prompt_id)
    async with request.app.state.pg_pool.acquire() as conn:
        versions = await conn.fetch(
            """
            SELECT id, version_number, status, quality_score, deployed_at, changed_by, change_message
            FROM prompt_versions WHERE prompt_id = $1 ORDER BY version_number DESC
            """, prompt_id)
        rollbacks = await conn.fetch(
            """
            SELECT r.id, r.triggered_at, r.resolved_at, r.triggered_by, r.reason, r.requested_by,
                   fv.version_number AS from_version_number, tv.version_number AS to_version_number
            FROM rollbacks r
            JOIN prompt_versions fv ON fv.id = r.from_version_id
            JOIN prompt_versions tv ON tv.id = r.to_version_id
            WHERE r.prompt_id = $1 ORDER BY r.triggered_at DESC, r.id DESC LIMIT 100
            """, prompt_id)

    current_id = prompt["current_version_id"]
    rows, current_number = [], None
    for v in versions:
        v = dict(v)
        why = _why_not(v, current_id)
        if v["id"] == current_id:
            current_number = v["version_number"]
        rows.append(VersionRow(
            version_number=v["version_number"], status=v["status"], quality_score=v["quality_score"],
            deployed_at=v["deployed_at"], changed_by=v["changed_by"], change_message=v["change_message"],
            is_current=v["id"] == current_id, can_roll_back_to=why is None, why_not=why))
    return RollbackHistoryResponse(
        prompt_id=prompt_id, prompt_name=prompt["prompt_name"], targeted_rollback_enabled=qcp_enabled(),
        current_version_number=current_number, versions=rows, rollbacks=[RollbackRow(**dict(r)) for r in rollbacks])


@router.post("/prompts/{prompt_id}/rollback")
async def roll_back_prompt(
    prompt_id: int, payload: DashboardRollbackRequest, request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Restore a previously passing version. The caller, and the name they give, are recorded with the reason."""
    return await request_targeted_rollback(
        request, auth, prompt_id, payload.to_version_number, reason=payload.reason,
        requested_by=requester(auth, payload.requested_by, "Dashboard"))
