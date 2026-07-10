"""
PredictiveDriftEngine — forecasts a deployed prompt version's quality
trend forward (Prophet, falling back to linear extrapolation if Prophet
isn't installed), flags predicted future drift before it happens, explains
which tracked factors are driving the trend (SHAP on a small auxiliary
model), and proactively alerts Slack when a drop is imminent.

Complements detectors/drift_detector.py (reactive: is THIS sample
anomalous?) with a forward-looking view (proactive: WILL quality drop
soon, based on the trend?). Wired into the 15-minute scheduler alongside
it — see scheduler.py's run_predictions_all_deployed_versions.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import numpy as np

from predictors.conformal_predictor import ConformalQualityPredictor

logger = logging.getLogger("aipq.predictors.drift_predictor")

MIN_HISTORY_POINTS = 10
HISTORY_WINDOW_DAYS = 90
DEFAULT_QUALITY_THRESHOLD = 0.85
PROACTIVE_ALERT_DAYS = 7

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

FEATURE_NAMES = ["time_since_deployment", "llm_model_version", "input_length_trend", "seasonal_patterns"]
FEATURE_LABELS = {
    "time_since_deployment": "Time since deployment",
    "llm_model_version": "LLM model update",
    "input_length_trend": "Input length increase",
    "seasonal_patterns": "Seasonal pattern",
}


def _risk_level(days_until_risk: Optional[int]) -> str:
    if days_until_risk is None:
        return "LOW"
    if days_until_risk < 3:
        return "CRITICAL"
    if days_until_risk < 7:
        return "HIGH"
    if days_until_risk < 14:
        return "MEDIUM"
    return "LOW"


def _recommendation(risk_level: str, days_until_risk: Optional[int], threshold: float) -> str:
    if risk_level == "LOW":
        return "No action needed — quality trend is stable."
    return (
        f"Quality predicted to drop below {threshold} in {days_until_risk} day(s) — "
        f"review this prompt version soon."
    )


class PredictiveDriftEngine:
    async def _load_history(self, prompt_version_id: int) -> list[tuple[datetime, float]]:
        from db import get_pool

        pool = await get_pool()
        cutoff = datetime.now(timezone.utc) - timedelta(days=HISTORY_WINDOW_DAYS)
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT recorded_at, compliance_score FROM drift_records
                WHERE prompt_version_id = $1 AND recorded_at >= $2
                ORDER BY recorded_at ASC
                """,
                prompt_version_id, cutoff,
            )
        return [(r["recorded_at"], r["compliance_score"]) for r in rows]

    # ── 1. Quality trend forecast ──────────────────────────────────────────

    async def predict_quality_trend(
        self, prompt_version_id: int, days_ahead: int = 30, threshold: float = DEFAULT_QUALITY_THRESHOLD,
    ) -> dict:
        history = await self._load_history(prompt_version_id)

        if len(history) < MIN_HISTORY_POINTS:
            return {
                "days_until_risk": None, "predicted_score_7d": None, "predicted_score_30d": None,
                "risk_level": "LOW",
                "recommendation": f"Not enough history ({len(history)}/{MIN_HISTORY_POINTS} points) to forecast yet.",
            }

        try:
            forecast = self._fit_and_forecast_prophet(history, days_ahead)
        except ImportError:
            logger.info("prophet not installed — using linear trend extrapolation instead")
            forecast = self._fit_and_forecast_linear(history, days_ahead)

        # Quality scores are only ever meaningful in [0, 1] — an extrapolated
        # trend (especially the linear fallback) can otherwise run away
        # arbitrarily far past a single recent outlier.
        forecast = [(day, min(1.0, max(0.0, predicted))) for day, predicted in forecast]

        days_until_risk = next((day for day, predicted in forecast if predicted < threshold), None)
        predicted_7d = next((p for d, p in forecast if d >= 7), forecast[-1][1])
        predicted_30d = next((p for d, p in forecast if d >= min(30, days_ahead)), forecast[-1][1])

        risk_level = _risk_level(days_until_risk)

        conformal = ConformalQualityPredictor()
        nonconformity_scores = conformal.calibrate_from_history([score for _, score in history])
        interval_7d = conformal.predict_interval(predicted_7d, nonconformity_scores)

        return {
            "days_until_risk": days_until_risk,
            "predicted_score_7d": round(predicted_7d, 4),
            "predicted_score_30d": round(predicted_30d, 4),
            "risk_level": risk_level,
            "recommendation": _recommendation(risk_level, days_until_risk, threshold),
            "confidence_interval_7d": {
                "lower": interval_7d.lower, "upper": interval_7d.upper,
                "confidence_level": interval_7d.confidence_level,
                "calibration_size": interval_7d.calibration_size,
                "guarantee": interval_7d.guarantee,
            },
        }

    @staticmethod
    def _fit_and_forecast_prophet(history: list[tuple[datetime, float]], days_ahead: int) -> list[tuple[int, float]]:
        from prophet import Prophet
        import pandas as pd

        df = pd.DataFrame(history, columns=["ds", "y"])
        df["ds"] = pd.to_datetime(df["ds"]).dt.tz_localize(None)

        model = Prophet(daily_seasonality=False, weekly_seasonality=True, yearly_seasonality=False)
        model.fit(df)

        future = model.make_future_dataframe(periods=days_ahead)
        forecast_df = model.predict(future)

        last_history_date = df["ds"].max()
        future_rows = forecast_df[forecast_df["ds"] > last_history_date]

        return [
            (int((row["ds"] - last_history_date).days), float(row["yhat"]))
            for _, row in future_rows.iterrows()
        ]

    @staticmethod
    def _fit_and_forecast_linear(history: list[tuple[datetime, float]], days_ahead: int) -> list[tuple[int, float]]:
        """Fallback when the prophet package isn't installed: simple linear-trend extrapolation."""
        t0 = history[0][0]
        xs = np.array([(ts - t0).total_seconds() / 86400.0 for ts, _ in history])
        ys = np.array([score for _, score in history])
        slope, intercept = np.polyfit(xs, ys, 1)

        last_day = xs[-1]
        return [(day, float(slope * (last_day + day) + intercept)) for day in range(1, days_ahead + 1)]

    # ── 2. Drift contributors (SHAP) ───────────────────────────────────────

    async def identify_drift_contributors(self, prompt_version_id: int) -> list[dict]:
        """
        SHAP contribution of 4 drift-related features to compliance_score,
        from a small RandomForestRegressor trained on this version's own
        recorded samples.

        llm_model_version has no tracked signal anywhere in AIPQ today (no
        such column exists) — included as a constant so SHAP honestly
        reports ~0 contribution rather than fabricating a fake trend.
        input_length_trend is approximated via response_length, the closest
        signal AIPQ actually tracks (output length, not input length).
        """
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            version_row = await conn.fetchrow(
                "SELECT deployed_at FROM prompt_versions WHERE id = $1", prompt_version_id
            )
            rows = await conn.fetch(
                """
                SELECT recorded_at, compliance_score, response_length FROM drift_records
                WHERE prompt_version_id = $1 ORDER BY recorded_at ASC
                """,
                prompt_version_id,
            )

        if version_row is None or len(rows) < MIN_HISTORY_POINTS:
            return []

        deployed_at = version_row["deployed_at"] or rows[0]["recorded_at"]

        X, y = [], []
        for r in rows:
            X.append([
                (r["recorded_at"] - deployed_at).total_seconds() / 3600.0,  # time_since_deployment (hours)
                0.0,                                                        # llm_model_version — not tracked
                float(r["response_length"]),                                # input_length_trend (proxy)
                float(r["recorded_at"].weekday()),                          # seasonal_patterns
            ])
            y.append(r["compliance_score"])

        from sklearn.ensemble import RandomForestRegressor

        X_arr, y_arr = np.array(X), np.array(y)
        model = RandomForestRegressor(n_estimators=50, random_state=42)
        model.fit(X_arr, y_arr)

        try:
            import shap
            explainer = shap.TreeExplainer(model)
            mean_abs = np.abs(explainer.shap_values(X_arr)).mean(axis=0)
        except ImportError:
            logger.warning("shap not installed — using feature_importances_ instead")
            mean_abs = model.feature_importances_

        total = mean_abs.sum()
        if total == 0:
            return []

        top3 = sorted(zip(FEATURE_NAMES, mean_abs / total), key=lambda kv: -kv[1])[:3]
        return [
            {
                "feature": name,
                "label": FEATURE_LABELS[name],
                "contribution": round(float(c), 4),
                "summary": f"{FEATURE_LABELS[name]} ({c:.2f})",
            }
            for name, c in top3
        ]

    # ── 3. Proactive alert ──────────────────────────────────────────────────

    async def proactive_alert(self, prompt_version_id: int, prompt_name: str = "") -> bool:
        """Posts a Slack alert when the predicted quality drop is within 7 days. Returns True if one was sent."""
        trend = await self.predict_quality_trend(prompt_version_id)
        if trend["days_until_risk"] is None or trend["days_until_risk"] >= PROACTIVE_ALERT_DAYS:
            return False

        label = prompt_name or f"prompt_version {prompt_version_id}"
        text = (
            f"AIPQ Prediction: {label} quality likely to drop below "
            f"{DEFAULT_QUALITY_THRESHOLD} in {trend['days_until_risk']} days. Review recommended."
        )

        if not SLACK_WEBHOOK_URL:
            logger.info("SLACK_WEBHOOK_URL not set — would have alerted: %s", text)
            return False

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(SLACK_WEBHOOK_URL, json={"text": text})
                resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning("Failed to send proactive Slack alert: %s", exc)
            return False
