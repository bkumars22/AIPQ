"""
BehavioralValidator — the "behavioral" layer of completeness_engine's
5-layer report. Two sources feed it, same as bct_results' own design intent
(see backend/db/migrations/V12__bct_results.sql: "external BCT integrations
... push their behavioral-contract verification result here"):

  1. Live check: POST {BCT_BASE_URL}/verify against the SCIP repo's BCT
     suite (ai-service/main.py), sending this prompt's currently deployed
     content as the system prompt under test. A pass/fail-per-scenario
     compliance rate plus the first multi-turn scenario that broke (the
     "breaking point"), scored fresh right now.
  2. Persisted history: bct_results rows already in AIPQ's own DB, pushed
     there by backend/routers/prompts.py's POST /{prompt_id}/bct-result —
     the same table any other BCT-integrated adapter (QAIP, ZENTRAVIX)
     writes to. A successful live check is itself persisted here
     (source_system="ai-engine-live"), so this layer's history accumulates
     across both paths in one place.

BCT_BASE_URL unset or the live call failing is not an error: bct_results
may still have a recent externally-pushed result, and behavioral_validator
falls back to that — only when BOTH are unavailable does this layer report
"no data", same honest-degrade posture as validators/portability.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("aipq.validators.behavioral_validator")

BCT_BASE_URL = os.getenv("BCT_BASE_URL", "http://localhost:8001")
BCT_VERIFY_TIMEOUT_SECONDS = 60.0  # /verify runs a real multi-turn/injection/leakage suite, not instant


@dataclass
class BreakingPoint:
    scenario_id: str
    description: str
    cash_in_turn: Optional[int]
    evidence: str


@dataclass
class BehavioralValidationResult:
    prompt_id: int
    version_id: Optional[int]
    source: str  # "live" | "persisted" | "none"
    compliance_pass_rate: Optional[float]  # 0-1
    breaking_point: Optional[BreakingPoint]
    checked_at: Optional[str]
    interpretation: str


class BehavioralValidator:
    async def check_behavioral_compliance(self, prompt_id: int) -> BehavioralValidationResult:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                "SELECT pv.id, pv.content FROM prompts p JOIN prompt_versions pv ON pv.id = p.current_version_id "
                "WHERE p.id = $1",
                prompt_id,
            )
        if current is None:
            return BehavioralValidationResult(prompt_id, None, "none", None, None, None, "No deployed version to validate.")

        live_result = await self._run_live_verify(prompt_id, current["id"], current["content"])
        if live_result is not None:
            return live_result

        return await self._fallback_to_persisted(prompt_id, current["id"])

    async def _run_live_verify(self, prompt_id: int, version_id: int, content: str) -> Optional[BehavioralValidationResult]:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=BCT_VERIFY_TIMEOUT_SECONDS) as client:
                resp = await client.post(
                    f"{BCT_BASE_URL}/verify",
                    json={"system_prompt": content, "target": "live"},
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            logger.info("behavioral_validator: live BCT /verify unavailable for prompt %d (%s) — falling back to persisted results", prompt_id, exc)
            return None

        compliance = payload.get("compliance") or {}
        pass_rate = compliance.get("pass_rate")
        pass_rate_fraction = round(pass_rate / 100.0, 4) if pass_rate is not None else None

        bp_payload = payload.get("breaking_point")
        breaking_point = None
        if bp_payload:
            breaking_point = BreakingPoint(
                scenario_id=bp_payload.get("scenario_id", ""),
                description=bp_payload.get("description", ""),
                cash_in_turn=bp_payload.get("cash_in_turn"),
                evidence=bp_payload.get("evidence", ""),
            )

        checked_at = payload.get("generated_at")
        await self._persist(prompt_id, pass_rate_fraction, breaking_point, passed=(pass_rate_fraction or 0) >= 0.85 and not breaking_point)

        interpretation = (
            f"Live BCT check: {pass_rate:.1f}% compliance" if pass_rate is not None else "Live BCT check returned no compliance figure"
        ) + (
            f", broke at scenario {breaking_point.scenario_id} ({breaking_point.description})." if breaking_point
            else " — held the line on every multi-turn escalation scenario."
        )

        return BehavioralValidationResult(
            prompt_id=prompt_id, version_id=version_id, source="live",
            compliance_pass_rate=pass_rate_fraction, breaking_point=breaking_point,
            checked_at=checked_at, interpretation=interpretation,
        )

    async def _persist(self, prompt_id: int, pass_rate_fraction: Optional[float], breaking_point: Optional[BreakingPoint], passed: bool) -> None:
        from db import get_pool

        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO bct_results
                        (prompt_id, source_system, contract_name, overall_compliance, breaking_point, result, role_tested)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                    """,
                    prompt_id, "ai-engine-live", "bct-suite-verify",
                    pass_rate_fraction if pass_rate_fraction is not None else 0.0,
                    breaking_point.cash_in_turn if breaking_point else None,
                    "PASS" if passed else "FAIL", None,
                )
        except Exception:
            logger.warning("behavioral_validator: failed to persist live bct_results row for prompt %d", prompt_id, exc_info=True)

    async def _fallback_to_persisted(self, prompt_id: int, version_id: int) -> BehavioralValidationResult:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT overall_compliance, breaking_point, result, created_at
                FROM bct_results WHERE prompt_id = $1 ORDER BY created_at DESC LIMIT 1
                """,
                prompt_id,
            )

        if row is None:
            return BehavioralValidationResult(
                prompt_id, version_id, "none", None, None, None,
                f"BCT suite unreachable at {BCT_BASE_URL} and no persisted bct_results for this prompt.",
            )

        breaking_point = None
        if row["breaking_point"] is not None:
            breaking_point = BreakingPoint(
                scenario_id="", description="(from persisted result — no scenario detail stored)",
                cash_in_turn=row["breaking_point"], evidence="",
            )

        return BehavioralValidationResult(
            prompt_id=prompt_id, version_id=version_id, source="persisted",
            compliance_pass_rate=row["overall_compliance"], breaking_point=breaking_point,
            checked_at=row["created_at"].isoformat() if row["created_at"] else None,
            interpretation=(
                f"BCT suite unreachable — using last persisted result ({row['result']}, "
                f"compliance={row['overall_compliance']:.2f}, recorded {row['created_at']})."
            ),
        )
