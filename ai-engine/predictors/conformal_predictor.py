"""
ConformalQualityPredictor — wraps a point forecast (e.g.
PredictiveDriftEngine's Prophet/linear-trend forecast) with a
distribution-free prediction interval that has a mathematically provable
marginal coverage guarantee, rather than an arbitrary-width "estimate."

This is split (inductive) conformal prediction — the simplest, most widely
used variant, described in Vovk, Gammerman & Shafer's "Algorithmic
Learning in a Random World" (2005) and the accessible tutorial by Shafer &
Vovk, "A Tutorial on Conformal Prediction" (2008, arxiv.org/abs/0706.3188).

The guarantee, precisely: given calibration data exchangeable with the test
point (no distribution shift between calibration and the point being
predicted — the one assumption this method needs), the interval covers the
true value with probability >= 1 - alpha, *marginally* over random draws of
the calibration set — not per-interval, and not a claim about the model's
accuracy. That's a narrower, more honest claim than "we're 90% confident,"
but it's one this method can actually prove rather than just assert.

Not wired to a live LLM-judge-scored dataset yet — calibrate_from_history()
backtests against whatever quality-score history a prompt version already
has in drift_records, via PredictiveDriftEngine (see that module's
predict_quality_trend, which now attaches a ConformalInterval when there's
enough history to calibrate one).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


MIN_CALIBRATION_POINTS = 5
DEFAULT_CONFIDENCE_LEVEL = 0.90


@dataclass
class ConformalInterval:
    point_prediction: float
    lower: Optional[float]
    upper: Optional[float]
    confidence_level: float
    calibration_size: int
    guarantee: str


class ConformalQualityPredictor:
    def calibrate(self, actual: list[float], predicted: list[float]) -> list[float]:
        """
        Nonconformity scores from historical (actual, predicted) pairs —
        the absolute residual, the standard nonconformity measure for
        real-valued regression targets like a quality score.
        """
        if len(actual) != len(predicted):
            raise ValueError(f"actual and predicted must be the same length ({len(actual)} != {len(predicted)})")
        return [abs(a - p) for a, p in zip(actual, predicted)]

    def calibrate_from_history(self, scores: list[float], min_window: int = MIN_CALIBRATION_POINTS) -> list[float]:
        """
        Backtests a rolling one-step-ahead linear forecast against a plain
        chronological score history to produce nonconformity scores,
        without needing a separately-tracked prediction log (nothing in
        AIPQ stores past forecasts today, only past actuals) — for each
        point after the first `min_window` scores, fits a line on
        everything before it and scores the residual against what actually
        happened next.
        """
        if len(scores) <= min_window:
            return []

        actual, predicted = [], []
        for i in range(min_window, len(scores)):
            window = scores[:i]
            predicted.append(self._linear_forecast_next(window))
            actual.append(scores[i])
        return self.calibrate(actual, predicted)

    @staticmethod
    def _linear_forecast_next(window: list[float]) -> float:
        n = len(window)
        if n == 1:
            return window[0]
        xs = list(range(n))
        mean_x = sum(xs) / n
        mean_y = sum(window) / n
        cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, window))
        var_x = sum((x - mean_x) ** 2 for x in xs)
        slope = cov / var_x if var_x else 0.0
        intercept = mean_y - slope * mean_x
        return slope * n + intercept  # forecast at x = n (one step past the window)

    def predict_interval(
        self,
        point_prediction: float,
        nonconformity_scores: list[float],
        confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
        value_bounds: tuple[float, float] = (0.0, 1.0),
    ) -> ConformalInterval:
        """
        Split conformal prediction interval around a point forecast.

        Uses the finite-sample-corrected quantile level
        ceil((m+1)(1-alpha)) / m (not a naive percentile) — this specific
        correction is what makes the coverage guarantee exact for any
        calibration size m, rather than merely asymptotic.
        """
        if not (0.0 < confidence_level < 1.0):
            raise ValueError("confidence_level must be strictly between 0 and 1")

        m = len(nonconformity_scores)
        if m < MIN_CALIBRATION_POINTS:
            return ConformalInterval(
                point_prediction=point_prediction, lower=None, upper=None,
                confidence_level=confidence_level, calibration_size=m,
                guarantee=f"Insufficient calibration data (need >= {MIN_CALIBRATION_POINTS}, have {m}) "
                          f"— keep collecting history before this interval is meaningful.",
            )

        alpha = 1.0 - confidence_level
        k = math.ceil((m + 1) * (1.0 - alpha))

        if k > m:
            # Not enough calibration points to guarantee this confidence level at all —
            # the honest answer is "the whole possible range," not a falsely narrow one.
            lower, upper = value_bounds
            guarantee = (
                f"Calibration set (m={m}) too small to guarantee {confidence_level:.0%} coverage — "
                f"interval widened to the full valid range [{lower}, {upper}]."
            )
        else:
            q = sorted(nonconformity_scores)[k - 1]
            lo, hi = value_bounds
            lower = max(lo, point_prediction - q)
            upper = min(hi, point_prediction + q)
            guarantee = (
                f"{confidence_level:.0%} conformal interval (m={m} calibration points) — "
                f"provably contains the true value with probability >= {confidence_level:.0%} "
                f"under exchangeability, not a heuristic estimate."
            )

        return ConformalInterval(
            point_prediction=point_prediction, lower=round(lower, 4), upper=round(upper, 4),
            confidence_level=confidence_level, calibration_size=m, guarantee=guarantee,
        )
