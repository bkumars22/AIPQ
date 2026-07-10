"""
Unit tests for PredictiveDriftEngine — DB and Slack calls are mocked.
The Prophet path is exercised via its ImportError fallback rather than a
real Prophet fit (keeps tests fast and independent of whether prophet's
compiled Stan backend is installed in this environment).

Run with:  pytest tests/test_drift_predictor.py -v
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from predictors.drift_predictor import PredictiveDriftEngine, _risk_level


@pytest.fixture
def engine():
    return PredictiveDriftEngine()


def _mock_pool(rows):
    mock_conn = AsyncMock()
    mock_conn.fetch.return_value = rows
    mock_pool = MagicMock()
    mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
    mock_pool.acquire.return_value.__aexit__.return_value = None
    return mock_pool


class TestRiskLevelBuckets:
    def test_buckets(self):
        assert _risk_level(None) == "LOW"
        assert _risk_level(20) == "LOW"
        assert _risk_level(10) == "MEDIUM"
        assert _risk_level(5) == "HIGH"
        assert _risk_level(1) == "CRITICAL"


class TestPredictQualityTrend:
    @pytest.mark.asyncio
    async def test_insufficient_history_returns_low_risk(self, engine):
        rows = [{"recorded_at": datetime.now(timezone.utc), "compliance_score": 0.9}] * 3
        with patch("db.get_pool", new_callable=AsyncMock, return_value=_mock_pool(rows)):
            result = await engine.predict_quality_trend(prompt_version_id=1)

        assert result["days_until_risk"] is None
        assert result["risk_level"] == "LOW"
        assert "Not enough history" in result["recommendation"]

    @pytest.mark.asyncio
    async def test_declining_trend_uses_linear_fallback_and_flags_risk(self, engine):
        now = datetime.now(timezone.utc)
        # Steady decline from 0.95 down to 0.80 over 12 days -> will cross 0.85 in the forecast
        rows = [
            {"recorded_at": now - timedelta(days=12 - i), "compliance_score": 0.95 - i * 0.0125}
            for i in range(12)
        ]

        with patch("db.get_pool", new_callable=AsyncMock, return_value=_mock_pool(rows)), \
             patch.object(PredictiveDriftEngine, "_fit_and_forecast_prophet", side_effect=ImportError):
            result = await engine.predict_quality_trend(prompt_version_id=1, threshold=0.85)

        assert result["days_until_risk"] is not None
        assert result["risk_level"] in ("HIGH", "CRITICAL", "MEDIUM")
        assert result["predicted_score_7d"] < 0.95

    @pytest.mark.asyncio
    async def test_attaches_conformal_interval_around_7d_forecast(self, engine):
        now = datetime.now(timezone.utc)
        rows = [
            {"recorded_at": now - timedelta(days=15 - i), "compliance_score": 0.90 + (i % 3) * 0.01}
            for i in range(15)
        ]

        with patch("db.get_pool", new_callable=AsyncMock, return_value=_mock_pool(rows)), \
             patch.object(PredictiveDriftEngine, "_fit_and_forecast_prophet", side_effect=ImportError):
            result = await engine.predict_quality_trend(prompt_version_id=1, threshold=0.85)

        interval = result["confidence_interval_7d"]
        assert interval["calibration_size"] > 0
        assert interval["lower"] <= result["predicted_score_7d"] <= interval["upper"]
        assert interval["confidence_level"] == 0.90


class TestIdentifyDriftContributors:
    @pytest.mark.asyncio
    async def test_insufficient_data_returns_empty(self, engine):
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"deployed_at": datetime.now(timezone.utc)}
        mock_conn.fetch.return_value = [{"recorded_at": datetime.now(timezone.utc),
                                          "compliance_score": 0.9, "response_length": 100}] * 3
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None

        with patch("db.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            result = await engine.identify_drift_contributors(prompt_version_id=1)

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_top_3_contributors_with_labels(self, engine):
        deployed_at = datetime.now(timezone.utc) - timedelta(days=10)
        rows = [
            {
                "recorded_at": deployed_at + timedelta(hours=i * 6),
                "compliance_score": 0.95 - (i % 3) * 0.05,
                "response_length": 100 + i * 20,
            }
            for i in range(15)
        ]
        mock_conn = AsyncMock()
        mock_conn.fetchrow.return_value = {"deployed_at": deployed_at}
        mock_conn.fetch.return_value = rows
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None

        with patch("db.get_pool", new_callable=AsyncMock, return_value=mock_pool):
            result = await engine.identify_drift_contributors(prompt_version_id=1)

        assert 1 <= len(result) <= 3
        for item in result:
            assert set(item.keys()) == {"feature", "label", "contribution", "summary"}
            assert 0.0 <= item["contribution"] <= 1.0


class TestProactiveAlert:
    @pytest.mark.asyncio
    async def test_skips_when_no_imminent_risk(self, engine):
        with patch.object(engine, "predict_quality_trend", new_callable=AsyncMock,
                           return_value={"days_until_risk": None, "risk_level": "LOW"}):
            sent = await engine.proactive_alert(prompt_version_id=1)
        assert sent is False

    @pytest.mark.asyncio
    async def test_skips_silently_without_webhook_configured(self, engine, monkeypatch):
        monkeypatch.setattr("predictors.drift_predictor.SLACK_WEBHOOK_URL", "")
        with patch.object(engine, "predict_quality_trend", new_callable=AsyncMock,
                           return_value={"days_until_risk": 4, "risk_level": "HIGH", "recommendation": "review"}):
            sent = await engine.proactive_alert(prompt_version_id=1, prompt_name="aria_socratic_system")
        assert sent is False

    @pytest.mark.asyncio
    async def test_posts_to_slack_when_risk_imminent_and_configured(self, engine, monkeypatch):
        monkeypatch.setattr("predictors.drift_predictor.SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
        with patch.object(engine, "predict_quality_trend", new_callable=AsyncMock,
                           return_value={"days_until_risk": 6, "risk_level": "HIGH", "recommendation": "review"}), \
             patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post.return_value = MagicMock(raise_for_status=MagicMock())
            mock_client_class.return_value.__aenter__.return_value = mock_client

            sent = await engine.proactive_alert(prompt_version_id=1, prompt_name="aria_socratic_system")

        assert sent is True
        sent_payload = mock_client.post.call_args.kwargs["json"]
        assert "aria_socratic_system" in sent_payload["text"]
        assert "6 days" in sent_payload["text"]
