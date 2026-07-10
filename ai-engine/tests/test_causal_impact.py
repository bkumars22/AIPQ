"""
Unit tests for predictors/causal_impact.py — pure math (scipy runs for
real), DB calls mocked for estimate_impact_for_version.

Run with:  pytest tests/test_causal_impact.py -v
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from predictors.causal_impact import CausalImpactAnalyzer


@pytest.fixture
def analyzer():
    return CausalImpactAnalyzer()


def _mock_pool(current_row, previous_row, pre_rows, post_rows):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [current_row, previous_row]
    mock_conn.fetch.side_effect = [pre_rows, post_rows]
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


class TestEstimateImpact:
    def test_insufficient_pre_period_returns_no_effect(self, analyzer):
        result = analyzer.estimate_impact(pre_scores=[0.9, 0.9], post_scores=[0.9] * 10)
        assert result.estimated_effect is None
        assert "Insufficient data" in result.interpretation

    def test_insufficient_post_period_returns_no_effect(self, analyzer):
        result = analyzer.estimate_impact(pre_scores=[0.9] * 10, post_scores=[0.9, 0.9])
        assert result.estimated_effect is None

    def test_counterfactual_extrapolates_declining_trend_not_flat_average(self, analyzer):
        # Pre-period declines linearly 1.0 -> 0.6; a naive flat-average
        # counterfactual would predict ~0.8, but the correct ITS
        # counterfactual continues the decline down to ~0.3.
        pre = [1.0, 0.9, 0.8, 0.7, 0.6]
        post = [0.5, 0.4, 0.3, 0.2, 0.1]  # exactly continues the same trend -> zero true effect
        result = analyzer.estimate_impact(pre, post)
        assert result.counterfactual_mean == pytest.approx(0.3, abs=1e-6)
        assert result.estimated_effect == pytest.approx(0.0, abs=1e-6)
        assert result.is_significant is False
        assert "No significant deviation" in result.interpretation

    def test_detects_significant_regression(self, analyzer):
        pre = [1.0, 0.9, 0.8, 0.7, 0.6]  # counterfactual continues to [0.5,0.4,0.3,0.2,0.1]
        post = [0.2, 0.15, 0.1, 0.05, 0.0]  # well below the counterfactual trend
        result = analyzer.estimate_impact(pre, post)
        assert result.estimated_effect == pytest.approx(-0.2, abs=1e-6)
        assert result.is_significant is True
        assert "regression" in result.interpretation.lower()

    def test_detects_significant_improvement(self, analyzer):
        pre = [1.0, 0.9, 0.8, 0.7, 0.6]
        post = [0.8, 0.75, 0.7, 0.65, 0.6]  # well above the [0.5..0.1] counterfactual
        result = analyzer.estimate_impact(pre, post)
        assert result.estimated_effect > 0
        assert result.is_significant is True
        assert "improvement" in result.interpretation.lower()

    def test_relative_effect_pct_matches_effect_over_counterfactual(self, analyzer):
        pre = [1.0, 0.9, 0.8, 0.7, 0.6]
        post = [0.2, 0.15, 0.1, 0.05, 0.0]
        result = analyzer.estimate_impact(pre, post)
        expected_pct = round(result.estimated_effect / result.counterfactual_mean * 100, 2)
        assert result.relative_effect_pct == pytest.approx(expected_pct)

    def test_every_result_carries_the_methodology_caveat(self, analyzer):
        result = analyzer.estimate_impact([0.9] * 6, [0.9] * 6)
        assert "interrupted time series" in result.caveat.lower()
        assert "confound" in result.caveat.lower()


class TestEstimateImpactForVersion:
    @pytest.mark.asyncio
    async def test_no_previous_version_returns_no_effect(self, analyzer):
        pool = _mock_pool(
            current_row={"id": 1, "version_number": 1}, previous_row=None, pre_rows=[], post_rows=[],
        )
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.estimate_impact_for_version(prompt_id=1)
        assert result.estimated_effect is None

    @pytest.mark.asyncio
    async def test_no_deployed_version_returns_no_effect(self, analyzer):
        pool = _mock_pool(current_row=None, previous_row=None, pre_rows=[], post_rows=[])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.estimate_impact_for_version(prompt_id=1)
        assert result.estimated_effect is None

    @pytest.mark.asyncio
    async def test_assembles_pre_and_post_scores_from_correct_versions(self, analyzer):
        pre_rows = [{"compliance_score": s} for s in [1.0, 0.9, 0.8, 0.7, 0.6]]
        post_rows = [{"compliance_score": s} for s in [0.2, 0.15, 0.1, 0.05, 0.0]]
        pool = _mock_pool(
            current_row={"id": 2, "version_number": 2}, previous_row={"id": 1},
            pre_rows=pre_rows, post_rows=post_rows,
        )
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.estimate_impact_for_version(prompt_id=1)
        assert result.sample_size_pre == 5
        assert result.sample_size_post == 5
        assert result.estimated_effect == pytest.approx(-0.2, abs=1e-6)
