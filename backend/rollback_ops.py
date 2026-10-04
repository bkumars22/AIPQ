"""
Targeted rollback, backend side: check the caller may touch the prompt, then ask the ai-engine to do it.

The rollback itself (the safety rules and the write) lives once, in ai-engine/rollback.py, next to the automatic
rollback it shares its write with. This module adds only what the backend owns: authentication, project ownership, and
turning the caller's identity into the "who" that is recorded in the rollbacks table. Used by both POST /qcp/rollback
(the AI Quality Control Plane) and POST /prompts/{id}/rollback (the dashboard).

Off unless QCP_ENABLED is set.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException, Request, status

from auth.dependencies import AuthContext
from config import ai_engine_url, qcp_enabled

logger = logging.getLogger("aipq.backend.rollback")

def requester(auth: AuthContext, claimed: str | None, channel: str) -> str:
    """The audit 'who'. The authenticated identity is always recorded; a name the caller supplies is recorded as a
    claim beside it, never in place of it."""
    verified = f"{channel} ({auth.via}, project {auth.project_id})"
    claimed = (claimed or "").strip()
    return f"{claimed} via {verified}" if claimed else verified


async def request_targeted_rollback(
    request: Request, auth: AuthContext, prompt_id: int, to_version_number: int, *,
    reason: str, requested_by: str, from_version_number: int | None = None,
) -> dict[str, Any]:
    """Ask the ai-engine to restore a previously passing version. Raises HTTPException with the ai-engine's own
    refusal message (404/409/422), or 502 if the ai-engine cannot be reached: a rollback is never reported as done
    unless it was."""
    if not qcp_enabled():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Targeted rollback is disabled: set QCP_ENABLED=1")

    async with request.app.state.pg_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
    if auth.via == "api_key" and auth.project_id != row["project_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")

    body = {"prompt_id": prompt_id, "to_version_number": to_version_number, "reason": reason,
            "requested_by": requested_by, "from_version_number": from_version_number}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{ai_engine_url()}/rollback", json=body)
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /rollback (prompt %d): %s", prompt_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "ai-engine unavailable: no rollback was performed") from exc

    if resp.status_code == 200:
        return resp.json()
    if resp.status_code in (403, 404, 409, 422):  # the ai-engine's deliberate refusals carry a clear message
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise HTTPException(resp.status_code, detail)
    raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                        f"ai-engine answered {resp.status_code}: no rollback was performed")
