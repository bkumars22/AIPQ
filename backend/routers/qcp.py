"""
/qcp/* — the small HTTP contract the AI Quality Control Plane's AIPQ adapter
expects (github.com/chaitrishodaya/ai-quality-control, adapters/aipq.py and
docs/decisions/003-simulated-adapters.md).

Feature-flagged: main.py only mounts this router when QCP_ENABLED is truthy,
so default behaviour is unchanged. Every route is a thin wrapper over an
existing route function in routers/prompts.py (same auth, same project
ownership check) and only reports what those functions really return. The
rollback goes through rollback_ops.py to the ai-engine's targeted rollback, which
only restores a version that previously passed the quality gate. An operation with
no real equivalent here answers 501 — see docs/QCP_INTEGRATION.md.
"""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from auth.dependencies import AuthContext, get_auth_context
from config import qcp_enabled
from rollback_ops import request_targeted_rollback, requester
from routers.prompts import get_current_version, get_prompt_causal_impact

router = APIRouter(prefix="/qcp", tags=["qcp"])

__all__ = ["router", "qcp_enabled"]


class QcpRootCauseRequest(BaseModel):
    model_config = ConfigDict(extra="allow")  # the control plane may send extra incident fields

    incident_id: str
    system: str
    prompt_id: str
    version: str
    last_good_version: str
    score: float
    threshold: float


class QcpRootCauseResponse(BaseModel):
    cause: str
    suspect_version: str
    evidence: list[str]


class QcpRollbackRequest(BaseModel):
    incident_id: str
    prompt_id: str
    from_version: str
    to_version: str
    # Optional, so the contract the control plane already speaks keeps working. When it sends the human approver and
    # the reason, they are recorded in AIPQ's rollbacks audit row, beside the authenticated caller.
    requested_by: str | None = None
    reason: str | None = None


class QcpRollbackResponse(BaseModel):
    status: str  # "succeeded": a refused or failed rollback is an HTTP error, never a "succeeded" body
    to_version: str
    rollback_id: int
    from_version: str
    requested_by: str


def _version_number(value: str) -> int:
    """Accepts "3" or "v3" (the control plane's version labels) -> 3."""
    m = re.fullmatch(r"v?(\d+)", value.strip(), flags=re.IGNORECASE)
    if m is None:
        raise HTTPException(422, f"version {value!r} is not a version number (expected '3' or 'v3')")
    return int(m.group(1))


async def _resolve_prompt_id(request: Request, auth: AuthContext, prompt_ref: str) -> int:
    """The control plane names prompts by string. AIPQ prompts have a numeric id and a
    prompt_name unique within a project, so accept either: a numeric string is the id,
    anything else is looked up as a prompt_name in the caller's own project."""
    if prompt_ref.isdigit():
        return int(prompt_ref)
    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM prompts WHERE project_id = $1 AND prompt_name = $2",
            auth.project_id, prompt_ref,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Prompt {prompt_ref!r} not found")
    return row["id"]


@router.post("/rootcause", response_model=QcpRootCauseResponse)
async def qcp_rootcause(
    payload: QcpRootCauseRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Wraps GET /prompts/{id}/current and GET /prompts/{id}/causal-impact.

    The existing causal-impact analysis only covers the currently deployed
    version versus the one it replaced, so a request about any other version is
    refused (409) rather than answered with an analysis of a different version.
    """
    prompt_id = await _resolve_prompt_id(request, auth, payload.prompt_id)
    suspect = _version_number(payload.version)

    current = await get_current_version(prompt_id, request, auth)
    if current.version_number != suspect:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Root-cause analysis covers the currently deployed version "
            f"(v{current.version_number}), not v{suspect}",
        )

    impact = await get_prompt_causal_impact(prompt_id, request, auth)
    if "pre_period_mean" not in impact:
        # the existing route degrades to a stub when ai-engine is down; do not dress that up
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "ai-engine unavailable: no causal analysis")

    evidence = [
        f"Reported by caller: v{suspect} scored {payload.score} against threshold {payload.threshold}",
        f"AIPQ quality score for v{current.version_number}: {current.quality_score}",
        f"Pre-change mean {impact['pre_period_mean']} vs post-change mean {impact['post_period_mean']} "
        f"(estimated effect {impact['estimated_effect']}, p={impact['p_value']}, "
        f"significant={impact['is_significant']})",
        f"Samples: {impact['sample_size_pre']} before, {impact['sample_size_post']} after",
    ]
    if impact.get("caveat"):
        evidence.append(f"Caveat: {impact['caveat']}")

    return QcpRootCauseResponse(
        cause=impact["interpretation"],
        suspect_version=payload.version,
        evidence=evidence,
    )


@router.post("/rollback", response_model=QcpRollbackResponse)
async def qcp_rollback(
    payload: QcpRollbackRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Restore a specific, previously passing version of a prompt.

    Wraps the ai-engine's targeted rollback (rollback_ops.request_targeted_rollback), which refuses anything that never
    passed the quality gate (failed, still testing, unknown, already deployed) with a 404/409/422 and a clear message,
    and records who asked and why in the rollbacks table. The AUTOMATIC rollback is a separate path and is unchanged.
    """
    prompt_id = await _resolve_prompt_id(request, auth, payload.prompt_id)
    to_version = _version_number(payload.to_version)
    from_version = _version_number(payload.from_version)
    reason = (payload.reason or "").strip() or f"Requested by the AI Quality Control Plane for incident {payload.incident_id}"
    result = await request_targeted_rollback(
        request, auth, prompt_id, to_version, from_version_number=from_version, reason=reason,
        requested_by=requester(auth, payload.requested_by, "AI Quality Control Plane"))
    return QcpRollbackResponse(
        status="succeeded", to_version=f"v{result['to_version_number']}", from_version=f"v{result['from_version_number']}",
        rollback_id=result["rollback_id"], requested_by=result["requested_by"])
