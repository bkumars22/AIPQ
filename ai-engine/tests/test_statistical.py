"""
Unit tests for StatisticalValidator — DB calls are mocked, scipy runs for
real (fast, no network/model download involved).

Run with:  pytest tests/test_statistical.py -v
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from validators.statistical import StatisticalValidator


@pytest.fixture
def validator():
    return StatisticalValidator()


def _mock_pool(rows):
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = rows
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


class TestCollectScores:
    @pytest.mark.asyncio
    async def test_returns_compliance_scores_in_order(self, validator):
        rows = [{"compliance_score": 0.9}, {"compliance_score": 0.85}, {"compliance_score": 0.92}]
        with patch("db.get_pool", new_callable=AsyncMock, return_value=_mock_pool(rows)):
            scores = await validator.collect_scores(prompt_version_id=1)
        assert scores == [0.9, 0.85, 0.92]

    @pytest.mark.asyncio
    async def test_empty_when_no_records(self, validator):
        with patch("db.get_pool", new_callable=AsyncMock, return_value=_mock_pool([])):
            scores = await validator.collect_scores(prompt_version_id=1)
        assert scores == []


class TestValidateImprovement:
    def test_significantly_better(self, validator):
        current = [0.95, 0.94, 0.96, 0.93, 0.95, 0.94, 0.96, 0.95, 0.93, 0.94, 0.95, 0.94]
        previous = [0.60, 0.58, 0.62, 0.59, 0.61, 0.60, 0.58, 0.61, 0.59, 0.60, 0.58, 0.61]

        result = validator.validate_improvement(current, previous)

        assert result["is_significant"] is True
        assert result["p_value"] < 0.05
        assert result["effect_size"] > 0
        assert result["effect_size_label"] == "Large"
        assert "Deploy" in result["recommendation"]

    def test_no_significant_difference(self, validator):
        current = [0.90, 0.91, 0.89, 0.90, 0.92, 0.88, 0.91, 0.90, 0.89, 0.90, 0.91, 0.90]
        previous = [0.90, 0.89, 0.91, 0.90, 0.88, 0.92, 0.90, 0.91, 0.89, 0.90, 0.89, 0.91]

        result = validator.validate_improvement(current, previous)

        assert result["is_significant"] is False
        assert "No significant difference" in result["recommendation"]

    def test_insufficient_samples_handled_gracefully(self, validator):
        current = [0.9, 0.91, 0.92]  # only 3 — below MIN_SAMPLES (10)
        previous = [0.6, 0.61, 0.62]

        result = validator.validate_improvement(current, previous)

        assert result["is_significant"] is False
        assert result["p_value"] is None
        assert "Insufficient samples" in result["recommendation"]
        assert result["sample_size"] == 3
        assert result["previous_sample_size"] == 3


class TestMinimumSampleCalculator:
    def test_reasonable_value_for_medium_effect(self, validator):
        # Textbook value for d=0.5, alpha=0.05, power=0.80 is ~64 per group
        n = validator.minimum_sample_calculator(expected_effect=0.5)
        assert 55 <= n <= 75

    def test_smaller_effect_requires_more_samples(self, validator):
        n_small_effect = validator.minimum_sample_calculator(expected_effect=0.2)
        n_large_effect = validator.minimum_sample_calculator(expected_effect=0.8)
        assert n_small_effect > n_large_effect

    def test_rejects_non_positive_effect(self, validator):
        with pytest.raises(ValueError):
            validator.minimum_sample_calculator(expected_effect=0.0)
