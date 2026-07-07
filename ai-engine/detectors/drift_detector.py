"""
Drift detection: IsolationForest baseline + scoring, SHAP explainability,
a 14-day linear-regression trend analyzer, and the automatic rollback
engine that fires on CRITICAL drift. Scheduled monitoring (APScheduler,
every 15 minutes) lives in scheduler.py and calls into this module.
"""
from __future__ import annotations

import logging
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import numpy as np
from sklearn.ensemble import IsolationForest

from db import get_pool, get_redis_binary

logger = logging.getLogger("aipq.drift_detector")

FEATURE_NAMES = ["faithfulness_score", "compliance_score", "response_length", "token_count"]
MIN_SAMPLES_FOR_BASELINE = 10
BASELINE_SAMPLE_LIMIT = 50
MODEL_CACHE_TTL_SECONDS = 24 * 3600


@dataclass
class DriftResult:
    is_anomaly: bool
    anomaly_score: float
    drift_severity: str  # NONE / LOW / HIGH / CRITICAL
    explanation: Optional[str] = None


def _model_cache_key(prompt_version_id: int) -> str:
    return f"aipq:driftmodel:{prompt_version_id}"


class DriftDetector:
    async def train_baseline(self, prompt_version_id: int) -> bool:
        """Train an IsolationForest on the last N samples for this version. Returns False if too little data."""
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT {', '.join(FEATURE_NAMES)}
                FROM drift_records
                WHERE prompt_version_id = $1
                ORDER BY recorded_at DESC
                LIMIT $2
                """,
                prompt_version_id, BASELINE_SAMPLE_LIMIT,
            )
        if len(rows) < MIN_SAMPLES_FOR_BASELINE:
            logger.info(
                "Not enough samples (%d/%d) to train a drift baseline for version %d",
                len(rows), MIN_SAMPLES_FOR_BASELINE, prompt_version_id,
            )
            return False

        X = np.array([[r[f] for f in FEATURE_NAMES] for r in rows], dtype=float)
        model = IsolationForest(n_estimators=100, contamination=0.1, random_state=42)
        model.fit(X)

        redis = get_redis_binary()
        await redis.set(_model_cache_key(prompt_version_id), pickle.dumps(model), ex=MODEL_CACHE_TTL_SECONDS)
        logger.info("Trained drift baseline for version %d on %d samples", prompt_version_id, len(rows))
        return True

    async def score_new_sample(self, prompt_version_id: int, metrics: dict[str, float]) -> DriftResult:
        """Score one new sample (faithfulness/compliance/response_length/token_count) against the trained baseline."""
        redis = get_redis_binary()
        cached = await redis.get(_model_cache_key(prompt_version_id))
        if cached is None:
            trained = await self.train_baseline(prompt_version_id)
            if not trained:
                return DriftResult(
                    is_anomaly=False, anomaly_score=0.0, drift_severity="NONE",
                    explanation="Not enough historical samples to assess drift yet",
                )
            cached = await redis.get(_model_cache_key(prompt_version_id))

        model: IsolationForest = pickle.loads(cached)
        x = np.array([[metrics.get(f, 0.0) for f in FEATURE_NAMES]], dtype=float)

        prediction = model.predict(x)[0]            # -1 = anomaly, 1 = normal
        raw_score = model.decision_function(x)[0]   # higher = more normal
        is_anomaly = prediction == -1
        anomaly_score = float(-raw_score)            # flip sign so higher = more anomalous

        severity = self._severity_from_score(anomaly_score, is_anomaly)
        explanation = await self._explain(model, x) if is_anomaly else None

        return DriftResult(
            is_anomaly=is_anomaly, anomaly_score=round(anomaly_score, 4),
            drift_severity=severity, explanation=explanation,
        )

    @staticmethod
    def _severity_from_score(anomaly_score: float, is_anomaly: bool) -> str:
        if not is_anomaly:
            return "NONE"
        if anomaly_score >= 0.25:
            return "CRITICAL"
        if anomaly_score >= 0.15:
            return "HIGH"
        return "LOW"

    @staticmethod
    async def _explain(model: IsolationForest, x: np.ndarray) -> str:
        """SHAP explanation of which metric drove the anomaly (falls back gracefully without shap)."""
        try:
            import shap
            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(x)[0]
        except Exception as exc:
            logger.warning("SHAP unavailable (%s) — using raw feature values as an approximation", exc)
            shap_values = x[0]

        contributions = sorted(zip(FEATURE_NAMES, shap_values), key=lambda kv: -abs(kv[1]))
        top_feature, top_value = contributions[0]
        direction = "dropped" if top_value < 0 else "spiked"
        return f"{top_feature.replace('_', ' ')} {direction} {abs(top_value):.2f} beyond normal range"


class TrendAnalyzer:
    """Linear regression over the last 14 days of compliance scores."""

    @staticmethod
    async def analyze(prompt_version_id: int) -> dict[str, Any]:
        pool = await get_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT recorded_at, compliance_score FROM drift_records
                WHERE prompt_version_id = $1 AND recorded_at >= $2
                ORDER BY recorded_at ASC
                """,
                prompt_version_id, cutoff,
            )
        if len(rows) < 3:
            return {"trend": "INSUFFICIENT_DATA", "slope": None, "sample_count": len(rows)}

        t0 = rows[0]["recorded_at"]
        xs = np.array([(r["recorded_at"] - t0).total_seconds() / 86400.0 for r in rows])
        ys = np.array([r["compliance_score"] for r in rows])
        slope, intercept = np.polyfit(xs, ys, 1)

        if slope < -0.01:
            trend = "DRIFT_ALERT"
        elif slope > 0.01:
            trend = "IMPROVING"
        else:
            trend = "STABLE"

        predicted_last = slope * xs[-1] + intercept
        if ys[-1] < predicted_last - 0.15:
            trend = "ACUTE_DRIFT"

        return {"trend": trend, "slope": round(float(slope), 5), "sample_count": len(rows)}


class RollbackEngine:
    """Automatic rollback to the best-scoring recent version on CRITICAL drift."""

    @staticmethod
    async def rollback_if_critical(prompt_id: int, drift_result: DriftResult) -> Optional[dict]:
        if drift_result.drift_severity != "CRITICAL":
            return None

        pool = await get_pool()
        async with pool.acquire() as conn:
            prompt_row = await conn.fetchrow("SELECT current_version_id FROM prompts WHERE id = $1", prompt_id)
            if prompt_row is None or prompt_row["current_version_id"] is None:
                return None
            from_version_id = prompt_row["current_version_id"]

            candidates = await conn.fetch(
                """
                SELECT id, version_number, quality_score FROM prompt_versions
                WHERE prompt_id = $1 AND status IN ('DEPLOYED', 'ROLLED_BACK') AND quality_score IS NOT NULL
                ORDER BY version_number DESC LIMIT 5
                """,
                prompt_id,
            )
            if not candidates:
                return None

            best = max(candidates, key=lambda r: r["quality_score"])
            if best["id"] == from_version_id:
                return None  # already on the best version

            async with conn.transaction():
                await conn.execute("UPDATE prompt_versions SET status = 'ROLLED_BACK' WHERE id = $1", from_version_id)
                await conn.execute(
                    "UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = $1", best["id"]
                )
                await conn.execute("UPDATE prompts SET current_version_id = $1 WHERE id = $2", best["id"], prompt_id)

                rollback_row = await conn.fetchrow(
                    """
                    INSERT INTO rollbacks (prompt_id, from_version_id, to_version_id, triggered_by, reason, resolved_at)
                    VALUES ($1, $2, $3, 'AUTOMATIC', $4, now())
                    RETURNING id
                    """,
                    prompt_id, from_version_id, best["id"],
                    f"Critical drift (anomaly_score={drift_result.anomaly_score}): {drift_result.explanation}",
                )

        logger.warning(
            "AIPQ auto-rolled back prompt %d: version %d -> %d (%s)",
            prompt_id, from_version_id, best["id"], drift_result.explanation,
        )
        return {"rollback_id": rollback_row["id"], "from_version_id": from_version_id, "to_version_id": best["id"]}
