"""
ProductionValidator — the "production" layer of completeness_engine's
5-layer report: is this prompt's pipeline healthy in AIMO right now (cost,
latency, error rate), not just "did it pass its golden dataset."

AIMO tracks pipelines by an opaque pipeline_id string, AIPQ tracks prompts
by integer prompt_id — there's no shared key, so which AIMO pipeline (if
any) corresponds to a given AIPQ prompt is read from AIMO_PROMPT_PIPELINE_MAP,
a JSON env var: {"<prompt_id>": "<aimo_pipeline_id>"}. This mirrors AIMO's
own aipq_connector.py, which maps the other direction (AIMO pipeline/node ->
AIPQ project/prompt) via its AIPQ_PIPELINE_MAP env var — same two-system,
no-shared-key problem, same env-var-mapping answer.

A prompt with no entry in that map isn't an error — most prompts in a
learning/dev AIPQ instance won't have a matching AIMO pipeline — it's
reported as NOT_CONFIGURED, same honest-degrade posture as every other
validator here.

Caveat carried over from AIMO's own code: AIMO's GET /pipelines/{id}/health
currently ships cost_usd and avg_latency_ms as Phase-1 placeholders (hardcoded
0.0/0, per that endpoint's own comments) pending real run-history aggregation.
This validator calls the real endpoint and reports whatever it returns —
once AIMO fills those in for real, this layer's numbers become real with no
code change here, but until then a 0.0 cost/latency from AIMO should be read
as "not aggregated yet," not "verified zero cost."
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("aipq.validators.production_validator")

AIMO_BASE_URL = os.getenv("AIMO_BASE_URL", "http://localhost:8010")
AIMO_SERVICE_TOKEN = os.getenv("AIMO_SERVICE_TOKEN", "")

# Defaults are deliberately generous for a learning-project deployment —
# tune per-environment via env vars once real traffic volumes are known.
COST_THRESHOLD_USD_24H = float(os.getenv("AIMO_COST_THRESHOLD_USD_24H", "50.0"))
LATENCY_THRESHOLD_MS = float(os.getenv("AIMO_LATENCY_THRESHOLD_MS", "3000"))

_PIPELINE_MAP: dict[str, str] = {}
try:
    _PIPELINE_MAP = json.loads(os.getenv("AIMO_PROMPT_PIPELINE_MAP", "{}"))
except json.JSONDecodeError:
    logger.warning("AIMO_PROMPT_PIPELINE_MAP is not valid JSON — production_validator will report every prompt as NOT_CONFIGURED")


@dataclass
class ProductionValidationResult:
    prompt_id: int
    pipeline_id: Optional[str]
    status: str  # "NOT_CONFIGURED" | "UNREACHABLE" | "OK"
    health_score: Optional[float]
    cost_usd_24h: Optional[float]
    avg_latency_ms: Optional[float]
    open_incidents: Optional[dict]
    within_cost_budget: Optional[bool]
    within_latency_budget: Optional[bool]
    production_score: Optional[float]  # 0-100
    interpretation: str


class ProductionValidator:
    async def check_production_health(self, prompt_id: int) -> ProductionValidationResult:
        pipeline_id = _PIPELINE_MAP.get(str(prompt_id))
        if not pipeline_id:
            return ProductionValidationResult(
                prompt_id, None, "NOT_CONFIGURED", None, None, None, None, None, None, None,
                "No AIMO pipeline mapped to this prompt (AIMO_PROMPT_PIPELINE_MAP) — production layer not applicable.",
            )

        import httpx

        headers = {"Authorization": f"Bearer {AIMO_SERVICE_TOKEN}"} if AIMO_SERVICE_TOKEN else {}
        try:
            async with httpx.AsyncClient(timeout=5.0, headers=headers) as client:
                resp = await client.get(f"{AIMO_BASE_URL}/pipelines/{pipeline_id}/health")
                resp.raise_for_status()
                health = resp.json()
        except Exception as exc:
            logger.warning("production_validator: AIMO unreachable for pipeline %s (prompt %d): %s", pipeline_id, prompt_id, exc)
            return ProductionValidationResult(
                prompt_id, pipeline_id, "UNREACHABLE", None, None, None, None, None, None, None,
                f"AIMO unreachable at {AIMO_BASE_URL}: {exc}",
            )

        last_24h = health.get("last_24h") or {}
        cost_usd = last_24h.get("cost_usd")
        avg_latency_ms = last_24h.get("avg_latency_ms")
        incidents = health.get("active_incidents") or {}
        health_score = health.get("health_score")

        within_cost = (cost_usd is not None) and cost_usd <= COST_THRESHOLD_USD_24H
        within_latency = (avg_latency_ms is not None) and avg_latency_ms <= LATENCY_THRESHOLD_MS
        critical_incidents = incidents.get("P0", 0) + incidents.get("P1", 0)

        production_score = self._composite_score(health_score, within_cost, within_latency, critical_incidents)

        interpretation_parts = [f"AIMO health_score={health_score}"] if health_score is not None else []
        interpretation_parts.append(
            f"cost_24h=${cost_usd:.2f} ({'within' if within_cost else 'over'} ${COST_THRESHOLD_USD_24H:.0f} budget)"
            if cost_usd is not None else "cost data not yet aggregated by AIMO"
        )
        interpretation_parts.append(
            f"avg_latency={avg_latency_ms:.0f}ms ({'within' if within_latency else 'over'} {LATENCY_THRESHOLD_MS:.0f}ms budget)"
            if avg_latency_ms is not None else "latency data not yet aggregated by AIMO"
        )
        if critical_incidents:
            interpretation_parts.append(f"{critical_incidents} open P0/P1 incident(s)")

        return ProductionValidationResult(
            prompt_id=prompt_id, pipeline_id=pipeline_id, status="OK", health_score=health_score,
            cost_usd_24h=cost_usd, avg_latency_ms=avg_latency_ms, open_incidents=incidents,
            within_cost_budget=within_cost if cost_usd is not None else None,
            within_latency_budget=within_latency if avg_latency_ms is not None else None,
            production_score=production_score, interpretation="; ".join(interpretation_parts) + ".",
        )

    @staticmethod
    def _composite_score(health_score: Optional[float], within_cost: bool, within_latency: bool, critical_incidents: int) -> Optional[float]:
        score = float(health_score) if health_score is not None else 100.0
        if not within_cost:
            score -= 20.0
        if not within_latency:
            score -= 20.0
        score -= min(critical_incidents * 15.0, 45.0)
        return round(max(score, 0.0), 2)
