"""
CompletenessEngine — orchestrates the 5 validator layers (llm_quality,
rag_quality, behavioral, drift, production) into one completeness report
for a prompt: a single 0-100 score, a traffic-light status per layer, which
layer is weakest, and one specific recommendation.

Each layer is independent and can fail, be not-applicable, or run fine —
this engine's only real job is turning 5 differently-shaped results into
one comparable 0-100 number per layer plus an overall score that only
averages the layers that actually produced one. A layer erroring (an
unhandled exception inside a validator) is reported as its own status
(ERROR) rather than crashing the whole report or silently vanishing from
it — same principle as main.py's /analyze/causal-attribution and
/analyze/portability routes, which turn a failed sub-call into a readable
result instead of a 500.

Layer order is fixed (LAYER_ORDER) so "weakest layer" ties resolve the same
way every time a report is regenerated for the same prompt.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("aipq.validators.completeness_engine")

LAYER_ORDER = ("llm_quality", "rag_quality", "behavioral", "drift", "production")

GREEN_THRESHOLD = 80.0
ORANGE_THRESHOLD = 50.0

_RECOMMENDATIONS = {
    "llm_quality": "Review the prompt's system instructions and golden_cases — low answer relevancy/compliance "
                   "usually means the prompt is ambiguous about what a correct response looks like.",
    "rag_quality": "Check retrieval quality first (are the right documents being retrieved at all?) before "
                   "tuning the prompt — low context_precision/recall is a retrieval problem, not a prompt one.",
    "behavioral": "A multi-turn escalation broke the prompt's stated rule, or overall BCT compliance is low — "
                  "harden the system prompt against authority claims and role-play framing (see the breaking_point detail).",
    "drift": "Recent evaluation runs are anomalous or trending down — check /analyze/predictions and consider "
             "whether the last deployed version needs a rollback.",
    "production": "Cost, latency, or open incidents in AIMO are out of budget for this prompt's pipeline — "
                  "this is a runtime/infra problem, not a prompt-quality one.",
}


@dataclass
class LayerResult:
    name: str
    status: str  # GREEN | ORANGE | RED | NOT_APPLICABLE | ERROR
    score: Optional[float]  # 0-100
    detail: str


@dataclass
class CompletenessReport:
    prompt_id: int
    version_id: Optional[int]
    layers: list[LayerResult]
    overall_score: Optional[float]
    weakest_layer: Optional[str]
    recommendation: str
    generated_at: str


def _status_for_score(score: Optional[float]) -> str:
    if score is None:
        return "NOT_APPLICABLE"
    if score >= GREEN_THRESHOLD:
        return "GREEN"
    if score >= ORANGE_THRESHOLD:
        return "ORANGE"
    return "RED"


class CompletenessEngine:
    async def validate_complete(self, prompt_id: int) -> CompletenessReport:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            version_row = await conn.fetchrow("SELECT current_version_id FROM prompts WHERE id = $1", prompt_id)
        version_id = version_row["current_version_id"] if version_row else None

        results = await asyncio.gather(
            self._run_llm(prompt_id), self._run_rag(prompt_id), self._run_behavioral(prompt_id),
            self._run_drift(prompt_id), self._run_production(prompt_id),
            return_exceptions=True,
        )

        layers: list[LayerResult] = []
        for name, result in zip(LAYER_ORDER, results):
            if isinstance(result, Exception):
                logger.warning("completeness_engine: layer %s raised for prompt %d: %s", name, prompt_id, result)
                layers.append(LayerResult(name, "ERROR", None, f"Validator raised an exception: {result}"))
            else:
                layers.append(result)

        applicable = [l for l in layers if l.score is not None]
        overall_score = round(sum(l.score for l in applicable) / len(applicable), 2) if applicable else None

        weakest_layer = None
        if applicable:
            weakest = min(applicable, key=lambda l: (l.score, LAYER_ORDER.index(l.name)))
            weakest_layer = weakest.name

        recommendation = self._build_recommendation(overall_score, weakest_layer, layers)

        return CompletenessReport(
            prompt_id=prompt_id, version_id=version_id, layers=layers, overall_score=overall_score,
            weakest_layer=weakest_layer, recommendation=recommendation,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _build_recommendation(overall_score: Optional[float], weakest_layer: Optional[str], layers: list[LayerResult]) -> str:
        if overall_score is None:
            return "No layer produced a usable score — check that a golden dataset and prompt version are configured."

        by_name = {l.name: l for l in layers}
        weak = by_name[weakest_layer]
        base = _RECOMMENDATIONS.get(weakest_layer, "Investigate this layer further.")
        return (
            f"Overall completeness {overall_score:.0f}/100. Weakest layer: {weakest_layer} "
            f"({weak.status}, score={weak.score}). {weak.detail} {base}"
        )

    # --- per-layer adapters --------------------------------------------------

    async def _run_llm(self, prompt_id: int) -> LayerResult:
        from validators.llm_validator import LLMValidator

        result = await LLMValidator().validate(prompt_id)
        score = round(result.overall_score * 100, 2) if result.overall_score is not None else None
        return LayerResult("llm_quality", _status_for_score(score), score, result.interpretation)

    async def _run_rag(self, prompt_id: int) -> LayerResult:
        from validators.rag_validator import RAGValidator

        result = await RAGValidator().validate(prompt_id)
        score = round(result.overall_score * 100, 2) if result.overall_score is not None else None
        return LayerResult("rag_quality", _status_for_score(score), score, result.interpretation)

    async def _run_behavioral(self, prompt_id: int) -> LayerResult:
        from validators.behavioral_validator import BehavioralValidator

        result = await BehavioralValidator().check_behavioral_compliance(prompt_id)
        if result.compliance_pass_rate is None:
            return LayerResult("behavioral", "NOT_APPLICABLE", None, result.interpretation)

        score = round(result.compliance_pass_rate * 100, 2)
        if result.breaking_point is not None:
            # A confirmed multi-turn break is a real red flag regardless of the
            # aggregate pass rate — cap the score so it can't hide behind an
            # otherwise-high compliance number.
            score = min(score, 40.0)
        return LayerResult("behavioral", _status_for_score(score), score, result.interpretation)

    async def _run_drift(self, prompt_id: int) -> LayerResult:
        from validators.drift_validator import DriftValidator

        result = await DriftValidator().check_drift(prompt_id)
        return LayerResult("drift", _status_for_score(result.drift_score), result.drift_score, result.interpretation)

    async def _run_production(self, prompt_id: int) -> LayerResult:
        from validators.production_validator import ProductionValidator

        result = await ProductionValidator().check_production_health(prompt_id)
        if result.status in ("NOT_CONFIGURED", "UNREACHABLE"):
            return LayerResult("production", "NOT_APPLICABLE", None, result.interpretation)
        return LayerResult("production", _status_for_score(result.production_score), result.production_score, result.interpretation)
