"""
Version rollback: the one place that switches which prompt version is deployed.

Two callers share `switch_deployed_version`:
  - the AUTOMATIC rollback (detectors.drift_detector.RollbackEngine), unchanged in behaviour: it picks the best recent
    version itself on CRITICAL drift;
  - the TARGETED rollback (`rollback_to_version`, below), where a caller names a version.

The targeted rollback is deliberately stricter than the automatic one: it only restores a version that previously PASSED
the quality gate, refuses anything else with a clear reason, and records who asked and why in the `rollbacks` table
(AIPQ's audit trail for rollbacks).

How a version "passed": the evaluation pipeline (evaluators/pipeline.py) sets a version DEPLOYED, with its quality_score,
only when overall_score >= threshold; otherwise FAILED. A version later superseded by a rollback is ROLLED_BACK. So a
version that is DEPLOYED or ROLLED_BACK with a recorded score has passed the gate; FAILED and TESTING never did.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from db import get_pool

logger = logging.getLogger("aipq.rollback")

PASSED_STATUSES = ("DEPLOYED", "ROLLED_BACK")


def targeted_rollback_enabled() -> bool:
    """The targeted rollback is off unless QCP_ENABLED is set (on the backend too), so default behaviour is unchanged."""
    return os.getenv("QCP_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


class RollbackRefused(Exception):
    """The requested rollback is not allowed. `kind` says why; the message is safe to show to the caller."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


async def switch_deployed_version(
    conn: Any, prompt_id: int, from_version_id: int, to_version_id: int, reason: str,
    requested_by: str | None = None,
) -> int:
    """Make `to_version_id` the deployed version and record it. Runs inside the CALLER's transaction.

    requested_by=None writes the AUTOMATIC audit row exactly as the drift-triggered rollback always has;
    a requester writes a MANUAL row that also records who asked.
    """
    await conn.execute("UPDATE prompt_versions SET status = 'ROLLED_BACK' WHERE id = $1", from_version_id)
    await conn.execute(
        "UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = $1", to_version_id
    )
    await conn.execute("UPDATE prompts SET current_version_id = $1 WHERE id = $2", to_version_id, prompt_id)

    if requested_by is None:
        row = await conn.fetchrow(
            """
            INSERT INTO rollbacks (prompt_id, from_version_id, to_version_id, triggered_by, reason, resolved_at)
            VALUES ($1, $2, $3, 'AUTOMATIC', $4, now())
            RETURNING id
            """,
            prompt_id, from_version_id, to_version_id, reason,
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO rollbacks (prompt_id, from_version_id, to_version_id, triggered_by, reason, requested_by,
                                   resolved_at)
            VALUES ($1, $2, $3, 'MANUAL', $4, $5, now())
            RETURNING id
            """,
            prompt_id, from_version_id, to_version_id, reason, requested_by,
        )
    return row["id"]


def _why_not_passed(version_number: int, status: str, score: float | None) -> str:
    if status == "FAILED":
        return f"version {version_number} failed the quality gate and was never deployed"
    if status == "TESTING":
        return f"version {version_number} has not finished evaluation, so it has not passed the quality gate"
    if score is None:
        return f"version {version_number} has no recorded quality score, so it cannot be shown to have passed the gate"
    return f"version {version_number} has status {status}, which is not a version that passed the quality gate"


async def rollback_to_version(
    prompt_id: int, to_version_number: int, *, reason: str, requested_by: str,
    from_version_number: int | None = None,
) -> dict[str, Any]:
    """Restore a specific, previously passing version of a prompt.

    Refused (RollbackRefused) when: the prompt or version does not exist; there is no deployed version; the caller's
    `from_version_number` is not the version currently deployed; the target is already deployed; the target never passed
    the quality gate (FAILED, still TESTING, or no score); or reason / requested_by is blank.
    """
    reason, requested_by = (reason or "").strip(), (requested_by or "").strip()
    if not reason or not requested_by:
        raise RollbackRefused("invalid", "a rollback must say who requested it and why")

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            # Lock the prompt so an automatic rollback or a new deployment cannot change it under us.
            prompt = await conn.fetchrow("SELECT id, current_version_id FROM prompts WHERE id = $1 FOR UPDATE", prompt_id)
            if prompt is None:
                raise RollbackRefused("not_found", f"prompt {prompt_id} does not exist")
            if prompt["current_version_id"] is None:
                raise RollbackRefused("no_current", "this prompt has no deployed version to roll back from")

            current = await conn.fetchrow(
                "SELECT id, version_number FROM prompt_versions WHERE id = $1", prompt["current_version_id"])
            if from_version_number is not None and current["version_number"] != from_version_number:
                raise RollbackRefused(
                    "not_current",
                    f"version {from_version_number} is not the deployed version (version {current['version_number']} is)")

            target = await conn.fetchrow(
                "SELECT id, version_number, status, quality_score FROM prompt_versions "
                "WHERE prompt_id = $1 AND version_number = $2", prompt_id, to_version_number)
            if target is None:
                raise RollbackRefused("unknown_version", f"version {to_version_number} does not exist for this prompt")
            if target["id"] == current["id"]:
                raise RollbackRefused("already_current", f"version {to_version_number} is already the deployed version")
            if target["status"] not in PASSED_STATUSES or target["quality_score"] is None:
                raise RollbackRefused(
                    "not_passed", _why_not_passed(target["version_number"], target["status"], target["quality_score"]))

            rollback_id = await switch_deployed_version(
                conn, prompt_id, current["id"], target["id"], reason, requested_by)

    logger.warning("AIPQ rolled back prompt %d: version %d -> %d, requested by %s (%s)", prompt_id,
                   current["version_number"], target["version_number"], requested_by, reason)
    return {
        "rollback_id": rollback_id, "prompt_id": prompt_id,
        "from_version_id": current["id"], "from_version_number": current["version_number"],
        "to_version_id": target["id"], "to_version_number": target["version_number"],
        "to_quality_score": target["quality_score"], "requested_by": requested_by, "reason": reason,
    }
