"""
/qcp/* — the small HTTP contract the AI Quality Control Plane's AIPQ adapter
expects (github.com/chaitrishodaya/ai-quality-control, adapters/aipq.py and
docs/decisions/003-simulated-adapters.md).

Feature-flagged: main.py only mounts this router when QCP_ENABLED is truthy,
so default behaviour is unchanged. Every route is a thin wrapper over an
existing route function in routers/prompts.py (same auth, same project
ownership check) and only reports what those functions really return. An
operation with no real equivalent here answers 501 — see docs/QCP_INTEGRATION.md.
"""
from __future__ import annotations

import os
import re

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict

from auth.dependencies import AuthContext, get_auth_context
from routers.prompts import get_current_version, get_prompt_causal_impact

router = APIRouter(prefix="/qcp", tags=["qcp"])

_TRUTHY = {"1", "true", "yes", "on"}


def qcp_enabled() -> bool:
    return os.getenv("QCP_ENABLED", "").strip().lower() in _TRUTHY


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


@router.post("/rollback")
async def qcp_rollback(
    payload: QcpRollbackRequest,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Gap. AIPQ's only rollback is RollbackEngine.rollback_if_critical (ai-engine), which
    fires on CRITICAL drift and chooses the target itself (best of the last 5 versions).
    Nothing rolls back to a caller-specified version, and re-implementing that here would
    duplicate business logic, so this answers 501 instead of pretending.
    """
    raise HTTPException(
        status.HTTP_501_NOT_IMPLEMENTED,
        "AIPQ has no rollback to a caller-specified version. Its only rollback is automatic "
        "(ai-engine RollbackEngine, on CRITICAL drift). See docs/QCP_INTEGRATION.md.",
    )
