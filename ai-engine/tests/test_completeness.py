"""
Unit tests for the 5-layer completeness validation feature: each of
llm_validator.py, rag_validator.py, behavioral_validator.py,
drift_validator.py, production_validator.py tested separately (DB/HTTP
mocked, same style as test_portability.py), plus completeness_engine.py's
orchestration (combined validation, a layer raising, and the two edge
cases explicitly called out: no data at all, and every layer failing).
"""
import os
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from validators.behavioral_validator import BehavioralValidator  # noqa: E402
from validators.completeness_engine import CompletenessEngine  # noqa: E402
from validators.drift_validator import DriftValidator  # noqa: E402
from validators.llm_validator import LLMValidator  # noqa: E402
from validators.production_validator import ProductionValidator  # noqa: E402
from validators.rag_validator import RAGValidator  # noqa: E402


def _mock_pool(fetchrow_results=None, fetch_result=None):
    mock_conn = AsyncMock()
    if fetchrow_results is not None:
        mock_conn.fetchrow.side_effect = fetchrow_results
    if fetch_result is not None:
        mock_conn.fetch.return_value = fetch_result
    mock_conn.execute = AsyncMock()
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


def _mock_httpx_response(json_payload, status_error=None):
    resp = MagicMock()
    resp.json.return_value = json_payload
    resp.raise_for_status.side_effect = status_error

    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    client.get = AsyncMock(return_value=resp)

    client_cls = MagicMock()
    client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
    client_cls.return_value.__aexit__ = AsyncMock(return_value=None)
    return client_cls


# --- 1. llm_validator ------------------------------------------------------

class TestLLMValidator:
    @pytest.mark.asyncio
    async def test_no_deployed_version_is_honest_not_applicable(self):
        pool = _mock_pool(fetchrow_results=[None])
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await LLMValidator().validate(prompt_id=1)
        assert result.overall_score is None
        assert "No deployed version" in result.interpretation


# --- 2. rag_validator --------------------------------------------------

class TestRAGValidator:
    @pytest.mark.asyncio
    async def test_not_applicable_without_retrieval_context(self):
        current = {"id": 1, "content": "You are helpful."}
        cases = [{"id": 1, "input_text": "hi", "expected_behavior": "be nice", "retrieval_context": None}]
        pool = _mock_pool(fetchrow_results=[current], fetch_result=cases)
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool):
            result = await RAGValidator().validate(prompt_id=1)
        assert result.overall_score is None
        assert result.rag_applicable_cases == 0
        assert "not applicable" in result.interpretation


# --- 3/4. behavioral_validator ------------------------------------------

class TestBehavioralValidator:
    @pytest.mark.asyncio
    async def test_live_verify_persists_bct_result(self):
        current = {"id": 1, "content": "You are a Socratic tutor."}
        pool = _mock_pool(fetchrow_results=[current])
        verify_payload = {
            "compliance": {"passed": 9, "total": 10, "pass_rate": 90.0, "critical_failures": 0},
            "breaking_point": None,
            "generated_at": "2026-07-24T00:00:00Z",
        }
        client_cls = _mock_httpx_response(verify_payload)
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("httpx.AsyncClient", client_cls):
            result = await BehavioralValidator().check_behavioral_compliance(prompt_id=1)

        assert result.source == "live"
        assert result.compliance_pass_rate == pytest.approx(0.9)
        assert result.breaking_point is None
        # persisted the live result into bct_results
        pool.acquire.return_value.__aenter__.return_value.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_persisted_when_bct_unreachable(self):
        current = {"id": 1, "content": "You are a Socratic tutor."}
        persisted = {
            "overall_compliance": 0.72, "breaking_point": 3, "result": "FAIL",
            "created_at": datetime(2026, 7, 20, tzinfo=timezone.utc),
        }
        pool = _mock_pool(fetchrow_results=[current, persisted])
        client_cls = MagicMock()
        client_cls.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("httpx.AsyncClient", client_cls):
            result = await BehavioralValidator().check_behavioral_compliance(prompt_id=1)

        assert result.source == "persisted"
        assert result.compliance_pass_rate == 0.72
        assert result.breaking_point.cash_in_turn == 3
        assert "unreachable" in result.interpretation


# --- 5. drift_validator --------------------------------------------------

class TestDriftValidator:
    @pytest.mark.asyncio
    async def test_insufficient_history_returns_no_score(self):
        pool = _mock_pool(fetchrow_results=[{"current_version_id": 1}, None])
        trend = {"trend": "INSUFFICIENT_DATA", "slope": None, "sample_count": 1}
        with patch("db.get_pool", new_callable=AsyncMock, return_value=pool), \
             patch("detectors.drift_detector.TrendAnalyzer.analyze", new_callable=AsyncMock, return_value=trend):
            result = await DriftValidator().check_drift(prompt_id=1)

        assert result.drift_score is None
        assert result.trend == "INSUFFICIENT_DATA"
        assert "Not enough evaluation history" in result.interpretation


# --- 6. production_validator ----------------------------------------------

class TestProductionValidator:
    @pytest.mark.asyncio
    async def test_not_configured_and_scores_when_configured(self):
        # No pipeline mapping at all -> NOT_CONFIGURED, no HTTP call made.
        with patch("validators.production_validator._PIPELINE_MAP", {}):
            result = await ProductionValidator().check_production_health(prompt_id=99)
        assert result.status == "NOT_CONFIGURED"
        assert result.production_score is None

        # Configured, but over both cost and latency budget -> composite score penalized.
        health_payload = {
            "health_score": 90,
            "active_incidents": {"P0": 1, "P1": 0, "P2": 0, "P3": 0},
            "last_24h": {"cost_usd": 999.0, "avg_latency_ms": 9000, "avg_faithfulness": None},
        }
        client_cls = _mock_httpx_response(health_payload)
        with patch("validators.production_validator._PIPELINE_MAP", {"1": "aria-prod"}), \
             patch("httpx.AsyncClient", client_cls):
            result = await ProductionValidator().check_production_health(prompt_id=1)

        assert result.status == "OK"
        assert result.within_cost_budget is False
        assert result.within_latency_budget is False
        # 90 - 20 (cost) - 20 (latency) - 15 (one P0 incident) = 35
        assert result.production_score == pytest.approx(35.0)


# --- 7/8/9/10. completeness_engine -----------------------------------------

def _patch_layers(llm=None, rag=None, behavioral=None, drift=None, production=None):
    return (
        patch("validators.llm_validator.LLMValidator.validate", new_callable=AsyncMock, return_value=llm),
        patch("validators.rag_validator.RAGValidator.validate", new_callable=AsyncMock, return_value=rag),
        patch("validators.behavioral_validator.BehavioralValidator.check_behavioral_compliance",
              new_callable=AsyncMock, return_value=behavioral),
        patch("validators.drift_validator.DriftValidator.check_drift", new_callable=AsyncMock, return_value=drift),
        patch("validators.production_validator.ProductionValidator.check_production_health",
              new_callable=AsyncMock, return_value=production),
    )


class TestCompletenessEngine:
    @pytest.mark.asyncio
    async def test_combined_validation_happy_path(self):
        pool = _mock_pool(fetchrow_results=[{"current_version_id": 5}])
        llm = SimpleNamespace(overall_score=0.90, interpretation="llm ok")
        rag = SimpleNamespace(overall_score=0.80, interpretation="rag ok")
        behavioral = SimpleNamespace(compliance_pass_rate=0.95, breaking_point=None, interpretation="behavioral ok")
        drift = SimpleNamespace(drift_score=70.0, interpretation="drift ok")
        production = SimpleNamespace(status="OK", production_score=60.0, interpretation="production ok")

        with ExitStack() as stack:
            stack.enter_context(patch("db.get_pool", new_callable=AsyncMock, return_value=pool))
            for p in _patch_layers(llm, rag, behavioral, drift, production):
                stack.enter_context(p)
            report = await CompletenessEngine().validate_complete(prompt_id=1)

        assert report.overall_score == pytest.approx(79.0)
        assert report.weakest_layer == "production"
        by_name = {l.name: l for l in report.layers}
        assert by_name["llm_quality"].status == "GREEN"
        assert by_name["production"].status == "ORANGE"
        assert "production" in report.recommendation

    @pytest.mark.asyncio
    async def test_layer_exception_reported_as_error_not_crash(self):
        pool = _mock_pool(fetchrow_results=[{"current_version_id": 5}])
        llm = SimpleNamespace(overall_score=0.90, interpretation="llm ok")
        rag = SimpleNamespace(overall_score=0.85, interpretation="rag ok")
        behavioral = SimpleNamespace(compliance_pass_rate=0.90, breaking_point=None, interpretation="behavioral ok")
        drift = SimpleNamespace(drift_score=85.0, interpretation="drift ok")

        with ExitStack() as stack:
            stack.enter_context(patch("db.get_pool", new_callable=AsyncMock, return_value=pool))
            for p in _patch_layers(llm, rag, behavioral, drift, production=None):
                stack.enter_context(p)
            stack.enter_context(patch(
                "validators.production_validator.ProductionValidator.check_production_health",
                new_callable=AsyncMock, side_effect=RuntimeError("AIMO SDK exploded"),
            ))
            report = await CompletenessEngine().validate_complete(prompt_id=1)

        by_name = {l.name: l for l in report.layers}
        assert by_name["production"].status == "ERROR"
        assert by_name["production"].score is None
        assert "AIMO SDK exploded" in by_name["production"].detail
        # the errored layer is excluded from the average, not treated as 0
        assert report.overall_score == pytest.approx((90 + 85 + 90 + 85) / 4)

    @pytest.mark.asyncio
    async def test_edge_case_empty_data_all_not_applicable(self):
        pool = _mock_pool(fetchrow_results=[{"current_version_id": None}])
        llm = SimpleNamespace(overall_score=None, interpretation="No golden cases to validate against.")
        rag = SimpleNamespace(overall_score=None, interpretation="No golden case has a retrieval_context configured.")
        behavioral = SimpleNamespace(compliance_pass_rate=None, breaking_point=None, interpretation="No data.")
        drift = SimpleNamespace(drift_score=None, interpretation="Not enough evaluation history.")
        production = SimpleNamespace(status="NOT_CONFIGURED", production_score=None, interpretation="Not configured.")

        with ExitStack() as stack:
            stack.enter_context(patch("db.get_pool", new_callable=AsyncMock, return_value=pool))
            for p in _patch_layers(llm, rag, behavioral, drift, production):
                stack.enter_context(p)
            report = await CompletenessEngine().validate_complete(prompt_id=1)

        assert report.overall_score is None
        assert report.weakest_layer is None
        assert all(l.status == "NOT_APPLICABLE" for l in report.layers)
        assert "No layer produced a usable score" in report.recommendation

    @pytest.mark.asyncio
    async def test_edge_case_all_layers_failing(self):
        pool = _mock_pool(fetchrow_results=[{"current_version_id": 5}])
        llm = SimpleNamespace(overall_score=0.10, interpretation="llm failing")
        rag = SimpleNamespace(overall_score=0.05, interpretation="rag failing")
        behavioral = SimpleNamespace(
            compliance_pass_rate=0.20, breaking_point=SimpleNamespace(cash_in_turn=3), interpretation="broke MT-01",
        )
        drift = SimpleNamespace(drift_score=15.0, interpretation="critical drift")
        production = SimpleNamespace(status="OK", production_score=10.0, interpretation="over budget")

        with ExitStack() as stack:
            stack.enter_context(patch("db.get_pool", new_callable=AsyncMock, return_value=pool))
            for p in _patch_layers(llm, rag, behavioral, drift, production):
                stack.enter_context(p)
            report = await CompletenessEngine().validate_complete(prompt_id=1)

        assert all(l.status == "RED" for l in report.layers)
        # behavioral's 20 is capped further by the breaking_point rule (min(20, 40) == 20, unchanged here)
        by_name = {l.name: l for l in report.layers}
        assert by_name["behavioral"].score == 20.0
        assert report.weakest_layer == "rag_quality"  # 5 is the lowest of 10/5/20/15/10
        assert report.overall_score == pytest.approx((10 + 5 + 20 + 15 + 10) / 5)
