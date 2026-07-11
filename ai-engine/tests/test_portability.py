"""Unit tests for validators/portability.py — DB and score_content mocked."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from validators.portability import PromptPortabilityValidator  # noqa: E402


def _golden_case(id_=1):
    return {
        "id": id_, "input_text": "hi", "expected_behavior": "be nice",
        "forbidden_patterns": [], "required_patterns": [],
    }


def _mock_pool(current_row, dataset_row, case_rows):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [current_row, dataset_row]
    mock_conn.fetch.return_value = case_rows
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


CURRENT = {"id": 1, "content": "You are helpful.", "temperature": 0.3, "max_tokens": 1024}
DATASET = {"id": 1, "threshold": 0.85}


class TestCheckPortability:
    @pytest.fixture
    def validator(self):
        return PromptPortabilityValidator()

    @pytest.mark.asyncio
    async def test_no_current_version_returns_empty(self, validator):
        pool = _mock_pool(None, None, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await validator.check_portability(prompt_id=1)
        assert "No deployed version" in result.interpretation

    @pytest.mark.asyncio
    async def test_no_golden_dataset_returns_empty(self, validator):
        pool = _mock_pool(CURRENT, None, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await validator.check_portability(prompt_id=1)
        assert "No golden dataset" in result.interpretation

    @pytest.mark.asyncio
    async def test_no_golden_cases_returns_empty(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await validator.check_portability(prompt_id=1)
        assert "No golden cases" in result.interpretation

    @pytest.mark.asyncio
    async def test_no_providers_configured_returns_clear_message(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=[]):
            result = await validator.check_portability(prompt_id=1)
        assert "No LLM provider configured" in result.interpretation
        assert result.providers_skipped == ["groq", "azure", "anthropic"]

    @pytest.mark.asyncio
    async def test_single_provider_gives_no_portability_score(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   return_value={"overall_score": 0.9}):
            result = await validator.check_portability(prompt_id=1)
        assert result.portability_score is None
        assert "Only groq is configured" in result.interpretation

    @pytest.mark.asyncio
    async def test_two_providers_computes_portability_score_correctly(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq", "anthropic"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.96}, {"overall_score": 0.87}]):
            result = await validator.check_portability(prompt_id=1)
        assert result.min_score == 0.87
        assert result.max_score == 0.96
        assert result.portability_score == pytest.approx(0.87 / 0.96, abs=1e-4)

    @pytest.mark.asyncio
    async def test_warning_triggered_when_scores_diverge_a_lot(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq", "azure"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.96}, {"overall_score": 0.60}]):
            result = await validator.check_portability(prompt_id=1)
        assert result.warning is not None
        assert "NOT reliably portable" in result.warning

    @pytest.mark.asyncio
    async def test_no_warning_when_scores_consistent(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq", "azure"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.95}, {"overall_score": 0.94}]):
            result = await validator.check_portability(prompt_id=1)
        assert result.warning is None

    @pytest.mark.asyncio
    async def test_provider_call_failure_recorded_not_crashed(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq", "anthropic"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.9}, Exception("connection refused")]):
            result = await validator.check_portability(prompt_id=1)
        failed = next(s for s in result.scores if s.provider == "anthropic")
        assert failed.overall_score is None
        assert "connection refused" in failed.error
        # one success still gives a (degenerate but real) result, not a crash
        assert result.min_score is not None

    @pytest.mark.asyncio
    async def test_all_providers_fail_returns_clear_message(self, validator):
        pool = _mock_pool(CURRENT, DATASET, [_golden_case()])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("validators.portability.configured_providers", return_value=["groq"]), \
             patch("validators.portability.score_content", new_callable=AsyncMock,
                   side_effect=Exception("boom")):
            result = await validator.check_portability(prompt_id=1)
        assert "Every configured provider call failed" in result.interpretation
