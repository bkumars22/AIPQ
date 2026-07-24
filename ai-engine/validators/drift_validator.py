"""
DriftValidator — the "drift" layer of completeness_engine's 5-layer report.

Deliberately a thin adapter over detectors/drift_detector.py's DriftDetector
(IsolationForest anomaly scoring against a per-version baseline) and
TrendAnalyzer (14-day linear regression over compliance_score), not a
reimplementation — those two already do the real IsolationForest/regression
work for scheduler.py's periodic monitoring, and duplicating that logic here
would mean two places that could disagree about what "drifting" means for
the same version. This module's job is just to combine their two
independent verdicts (a point-in-time anomaly check + a trend direction)
into the one drift_score/status completeness_engine needs, the same way
validators/portability.py combines evaluators/scoring.score_content's
per-provider runs into a single portability_score.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("aipq.validators.drift_validator")

FEATURE_NAMES = ("faithfulness_score", "compliance_score", "response_length", "token_count")

# drift_severity / trend -> a 0-100 "how healthy is this layer" score.
# NONE/STABLE/IMPROVING keep full marks; CRITICAL/ACUTE_DRIFT bottom out low
# rather than 0 — an anomaly detector flagging drift isn't proof of total
# failure the way a hard threshold breach is.
_SEVERITY_SCORE = {"NONE": 100.0, "LOW": 75.0, "HIGH": 45.0, "CRITICAL": 15.0}
_TREND_SCORE = {"STABLE": 100.0, "IMPROVING": 100.0, "DRIFT_ALERT": 45.0, "ACUTE_DRIFT": 15.0, "INSUFFICIENT_DATA": None}


@dataclass
class DriftValidationResult:
    prompt_id: int
    version_id: Optional[int]
    is_anomaly: Optional[bool]
    drift_severity: Optional[str]
    trend: Optional[str]
    trend_slope: Optional[float]
    sample_count: int
    drift_score: Optional[float]  # 0-100, None if not enough history to say anything
    interpretation: str


class DriftValidator:
    async def check_drift(self, prompt_id: int) -> DriftValidationResult:
        from db import get_pool
        from detectors.drift_detector import DriftDetector, TrendAnalyzer

        pool = await get_pool()
        async with pool.acquire() as conn:
            version_row = await conn.fetchrow(
                "SELECT current_version_id FROM prompts WHERE id = $1", prompt_id,
            )
            version_id = version_row["current_version_id"] if version_row else None
            if version_id is None:
                return DriftValidationResult(prompt_id, None, None, None, None, None, 0, None, "No deployed version to validate.")

            latest_sample = await conn.fetchrow(
                f"""
                SELECT {', '.join(FEATURE_NAMES)} FROM drift_records
                WHERE prompt_version_id = $1 ORDER BY recorded_at DESC LIMIT 1
                """,
                version_id,
            )

        trend = await TrendAnalyzer.analyze(version_id)

        is_anomaly = drift_severity = None
        if latest_sample is not None:
            detector = DriftDetector()
            metrics = {f: latest_sample[f] for f in FEATURE_NAMES}
            drift_result = await detector.score_new_sample(version_id, metrics)
            is_anomaly, drift_severity = drift_result.is_anomaly, drift_result.drift_severity

        severity_score = _SEVERITY_SCORE.get(drift_severity) if drift_severity else None
        trend_score = _TREND_SCORE.get(trend["trend"])
        candidate_scores = [s for s in (severity_score, trend_score) if s is not None]
        drift_score = round(min(candidate_scores), 2) if candidate_scores else None

        if drift_score is None:
            interpretation = "Not enough evaluation history yet to assess drift for this version."
        else:
            parts = []
            if drift_severity is not None:
                parts.append(f"anomaly check: {drift_severity.lower()}" + (" (anomalous)" if is_anomaly else ""))
            parts.append(f"14-day trend: {trend['trend'].lower().replace('_', ' ')}")
            interpretation = f"{'; '.join(parts)}."

        return DriftValidationResult(
            prompt_id=prompt_id, version_id=version_id, is_anomaly=is_anomaly, drift_severity=drift_severity,
            trend=trend["trend"], trend_slope=trend.get("slope"), sample_count=trend.get("sample_count", 0),
            drift_score=drift_score, interpretation=interpretation,
        )
