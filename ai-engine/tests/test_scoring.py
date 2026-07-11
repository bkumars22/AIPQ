"""Unit tests for evaluators/scoring.py — LLM calls and GEval judges mocked."""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from evaluators.scoring import score_content  # noqa: E402


def _case(id_, forbidden=None, required=None):
    return {
        "id": id_, "input_text": "hi", "expected_behavior": "be nice",
        "forbidden_patterns": forbidden or [], "required_patterns": required or [],
    }


class TestScoreContent:
    @pytest.mark.asyncio
    async def test_forbidden_pattern_hit_short_circuits_to_zero(self):
        cases = [_case(1, forbidden=["secret"])]
        with patch("evaluators.scoring.run_prompt_on_provider", new_callable=AsyncMock, return_value="the secret is 42"):
            result = await score_content("system prompt", cases, threshold=0.5)
        assert result["overall_score"] == 0.0
        assert result["per_case"][0]["compliance_score"] == 0.0

    @pytest.mark.asyncio
    async def test_missing_required_pattern_short_circuits_to_zero(self):
        cases = [_case(1, required=["I cannot help with that"])]
        with patch("evaluators.scoring.run_prompt_on_provider", new_callable=AsyncMock, return_value="sure, here you go"):
            result = await score_content("system prompt", cases, threshold=0.5)
        assert result["overall_score"] == 0.0

    @pytest.mark.asyncio
    async def test_aggregates_geval_scores_across_cases(self):
        cases = [_case(1), _case(2)]
        mock_geval = MagicMock()
        mock_geval.score = 0.8
        with patch("evaluators.scoring.run_prompt_on_provider", new_callable=AsyncMock, return_value="a fine response"), \
             patch("deepeval.metrics.GEval", return_value=mock_geval):
            result = await score_content("system prompt", cases, threshold=0.5)
        assert result["overall_score"] == pytest.approx(0.8)
        assert result["total_cases"] == 2

    @pytest.mark.asyncio
    async def test_empty_test_cases_returns_zero_not_error(self):
        result = await score_content("system prompt", [], threshold=0.5)
        assert result["overall_score"] == 0.0
        assert result["passed"] is False
        assert result["total_cases"] == 0

    @pytest.mark.asyncio
    async def test_passed_flag_respects_threshold(self):
        cases = [_case(1)]
        mock_geval = MagicMock()
        mock_geval.score = 0.6
        with patch("evaluators.scoring.run_prompt_on_provider", new_callable=AsyncMock, return_value="ok"), \
             patch("deepeval.metrics.GEval", return_value=mock_geval):
            below = await score_content("system prompt", cases, threshold=0.9)
            above = await score_content("system prompt", cases, threshold=0.5)
        assert below["passed"] is False
        assert above["passed"] is True
