"""Unit tests for predictors/conformal_predictor.py — pure math, no DB/network."""
import pytest

from predictors.conformal_predictor import ConformalQualityPredictor, MIN_CALIBRATION_POINTS


@pytest.fixture
def predictor():
    return ConformalQualityPredictor()


class TestCalibrate:
    def test_computes_absolute_residuals(self, predictor):
        scores = predictor.calibrate(actual=[0.9, 0.5, 0.7], predicted=[0.8, 0.6, 0.7])
        assert scores == pytest.approx([0.1, 0.1, 0.0])

    def test_raises_on_mismatched_lengths(self, predictor):
        with pytest.raises(ValueError):
            predictor.calibrate(actual=[0.9, 0.5], predicted=[0.8])


class TestCalibrateFromHistory:
    def test_produces_len_minus_window_scores(self, predictor):
        scores = [0.9] * 12
        nonconformity = predictor.calibrate_from_history(scores, min_window=5)
        assert len(nonconformity) == 12 - 5

    def test_empty_when_history_too_short(self, predictor):
        assert predictor.calibrate_from_history([0.9, 0.9, 0.9], min_window=5) == []

    def test_near_zero_residuals_for_flat_history(self, predictor):
        # A perfectly flat series is trivially linearly predictable — the
        # backtest's one-step-ahead forecasts should match almost exactly.
        scores = [0.85] * 12
        nonconformity = predictor.calibrate_from_history(scores, min_window=5)
        assert all(s < 1e-9 for s in nonconformity)


class TestPredictInterval:
    def test_insufficient_calibration_returns_none_bounds(self, predictor):
        result = predictor.predict_interval(0.8, nonconformity_scores=[0.1, 0.2, 0.3])
        assert result.lower is None
        assert result.upper is None
        assert "Insufficient calibration data" in result.guarantee

    def test_correct_finite_sample_quantile_index(self, predictor):
        # m=9, confidence=0.8 -> k = ceil(10 * 0.8) = 8 -> 8th smallest of 9 sorted scores.
        scores = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09]
        result = predictor.predict_interval(0.5, scores, confidence_level=0.8)
        assert result.lower == pytest.approx(0.5 - 0.08)
        assert result.upper == pytest.approx(0.5 + 0.08)
        assert result.calibration_size == 9

    def test_clips_to_value_bounds(self, predictor):
        scores = [0.5] * 10
        result = predictor.predict_interval(0.95, scores, confidence_level=0.8, value_bounds=(0.0, 1.0))
        assert result.upper == 1.0  # 0.95 + q would exceed 1.0

    def test_widens_to_full_range_when_k_exceeds_calibration_size(self, predictor):
        scores = [0.1, 0.2, 0.3, 0.4, 0.5]  # m=5, at MIN_CALIBRATION_POINTS
        result = predictor.predict_interval(0.5, scores, confidence_level=0.99, value_bounds=(0.0, 1.0))
        assert result.lower == 0.0
        assert result.upper == 1.0
        assert "too small to guarantee" in result.guarantee

    def test_higher_confidence_level_gives_wider_or_equal_interval(self, predictor):
        scores = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35]
        narrow = predictor.predict_interval(0.5, scores, confidence_level=0.7, value_bounds=(0.0, 1.0))
        wide = predictor.predict_interval(0.5, scores, confidence_level=0.95, value_bounds=(0.0, 1.0))
        assert (wide.upper - wide.lower) >= (narrow.upper - narrow.lower)

    def test_rejects_invalid_confidence_level(self, predictor):
        with pytest.raises(ValueError):
            predictor.predict_interval(0.5, [0.1] * 10, confidence_level=1.5)
