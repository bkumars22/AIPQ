"""
Unit tests for analyzers/causal.py. DB calls and score_content are mocked;
the text-manipulation helpers (_count_examples, _truncate_to_length,
_remove_last_n_examples) are tested directly with real strings, since
those are pure functions worth verifying exactly.
"""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from analyzers.causal import (  # noqa: E402
    CausalAttributionAnalyzer,
    _count_examples,
    _remove_last_n_examples,
    _truncate_to_length,
)


class TestTextHelpers:
    def test_count_examples_matches_example_colon_lines(self):
        text = "Rule 1: be nice\nExample 1: hi -> hello\nExample 2: bye -> goodbye\nRule 2: be brief"
        assert _count_examples(text) == 2

    def test_count_examples_matches_eg_lines(self):
        text = "Be concise.\ne.g. keep answers short\nAlso be kind."
        assert _count_examples(text) == 1

    def test_count_examples_zero_when_none_present(self):
        assert _count_examples("You are a helpful assistant. Be polite.") == 0

    def test_truncate_to_length_shortens_when_longer(self):
        assert _truncate_to_length("hello world", 5) == "hello"

    def test_truncate_to_length_unchanged_when_already_shorter(self):
        assert _truncate_to_length("hi", 100) == "hi"

    def test_remove_last_n_examples_removes_trailing_matches(self):
        text = "Rule 1\nExample 1: a\nExample 2: b\nExample 3: c"
        result = _remove_last_n_examples(text, 2)
        assert _count_examples(result) == 1
        assert "Example 1: a" in result
        assert "Example 3: c" not in result

    def test_remove_last_n_examples_noop_when_n_is_zero(self):
        text = "Example 1: a\nExample 2: b"
        assert _remove_last_n_examples(text, 0) == text


def _golden_case(id_=1):
    return {
        "id": id_, "input_text": "hi", "expected_behavior": "be nice",
        "forbidden_patterns": [], "required_patterns": [],
    }


def _mock_pool(current_row, previous_row, dataset_row, case_rows):
    mock_conn = AsyncMock()
    mock_conn.fetchrow.side_effect = [current_row, previous_row, dataset_row]
    mock_conn.fetch.return_value = case_rows
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


class TestAttributeChange:
    @pytest.fixture
    def analyzer(self):
        return CausalAttributionAnalyzer()

    @pytest.mark.asyncio
    async def test_no_current_version_returns_empty_result(self, analyzer):
        pool = _mock_pool(None, None, None, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.attribute_change(prompt_id=1)
        assert result.factors == []
        assert "No deployed version" in result.interpretation

    @pytest.mark.asyncio
    async def test_no_previous_version_returns_empty_result(self, analyzer):
        current = {"id": 1, "version_number": 1, "content": "x", "temperature": 0.3, "max_tokens": 100, "quality_score": 0.9}
        pool = _mock_pool(current, None, None, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.attribute_change(prompt_id=1)
        assert result.previous_version_id is None
        assert "No previous version" in result.interpretation

    @pytest.mark.asyncio
    async def test_no_golden_dataset_returns_empty_result(self, analyzer):
        current = {"id": 2, "version_number": 2, "content": "x", "temperature": 0.3, "max_tokens": 100, "quality_score": 0.6}
        previous = {"id": 1, "content": "y", "temperature": 0.7, "max_tokens": 100, "quality_score": 0.9}
        pool = _mock_pool(current, previous, None, [])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await analyzer.attribute_change(prompt_id=1)
        assert "No golden dataset" in result.interpretation

    @pytest.mark.asyncio
    async def test_temperature_only_change_produces_exact_counterfactual(self, analyzer):
        current = {"id": 2, "version_number": 2, "content": "same content", "temperature": 0.9, "max_tokens": 100, "quality_score": 0.6}
        previous = {"id": 1, "content": "same content", "temperature": 0.3, "max_tokens": 100, "quality_score": 0.9}
        dataset = {"id": 1, "threshold": 0.85}
        pool = _mock_pool(current, previous, dataset, [_golden_case()])

        # score_content called twice: once for current, once for the temperature counterfactual
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("analyzers.causal.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.60}, {"overall_score": 0.88}]):
            result = await analyzer.attribute_change(prompt_id=1)

        temp_factor = next(f for f in result.factors if f.factor == "temperature")
        assert temp_factor.changed is True
        assert temp_factor.counterfactual_score == 0.88
        assert temp_factor.recovered_effect == pytest.approx(0.28)
        assert temp_factor.share_pct == 100.0  # only changed factor

        max_tokens_factor = next(f for f in result.factors if f.factor == "max_tokens")
        assert max_tokens_factor.changed is False

    @pytest.mark.asyncio
    async def test_share_pct_normalizes_across_two_changed_factors(self, analyzer):
        current = {"id": 2, "version_number": 2, "content": "x" * 100, "temperature": 0.9, "max_tokens": 100, "quality_score": 0.5}
        previous = {"id": 1, "content": "x" * 50, "temperature": 0.3, "max_tokens": 100, "quality_score": 0.9}
        dataset = {"id": 1, "threshold": 0.85}
        pool = _mock_pool(current, previous, dataset, [_golden_case()])

        # current, temperature-variant (+0.3 recovered), length-variant (+0.1 recovered)
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("analyzers.causal.score_content", new_callable=AsyncMock,
                   side_effect=[{"overall_score": 0.5}, {"overall_score": 0.8}, {"overall_score": 0.6}]):
            result = await analyzer.attribute_change(prompt_id=1)

        temp_factor = next(f for f in result.factors if f.factor == "temperature")
        length_factor = next(f for f in result.factors if f.factor == "prompt_length")
        # recovered effects: temp=0.3, length=0.1 -> shares 75%/25%
        assert temp_factor.share_pct == pytest.approx(75.0)
        assert length_factor.share_pct == pytest.approx(25.0)

    @pytest.mark.asyncio
    async def test_no_changed_factors_gives_honest_interpretation(self, analyzer):
        current = {"id": 2, "version_number": 2, "content": "same", "temperature": 0.3, "max_tokens": 100, "quality_score": 0.6}
        previous = {"id": 1, "content": "same", "temperature": 0.3, "max_tokens": 100, "quality_score": 0.9}
        dataset = {"id": 1, "threshold": 0.85}
        pool = _mock_pool(current, previous, dataset, [_golden_case()])

        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("analyzers.causal.score_content", new_callable=AsyncMock, return_value={"overall_score": 0.6}):
            result = await analyzer.attribute_change(prompt_id=1)

        assert all(not f.changed for f in result.factors)
        assert "nothing (tracked) differs" in result.interpretation
