"""
Unit tests for the AIPQ SDK — all AIPQ API calls are mocked, no network needed.

Run with:  pytest sdk/tests/test_aipq_client.py -v
"""
import sys
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from aipq.client import AIPQClient, aipq_prompt
from aipq.exceptions import AIPQError, PromptQualityError


def _mock_response(json_body: dict | None, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = b"{}" if json_body is not None else b""
    resp.json.return_value = json_body
    if status >= 400:
        import httpx
        resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("error", request=MagicMock(), response=resp)
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture
def client():
    c = AIPQClient(api_key="test-key", project_id="proj-1", base_url="http://fake-aipq")
    yield c


class TestClientConstruction:
    def test_requires_api_key(self):
        with pytest.raises(ValueError):
            AIPQClient(api_key="", project_id="proj-1")

    def test_requires_project_id(self):
        with pytest.raises(ValueError):
            AIPQClient(api_key="key", project_id="")


class TestAipqPromptDecorator:
    @pytest.mark.asyncio
    async def test_creates_version_on_first_use(self, client):
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [
                _mock_response({"prompt_id": 42}),                                   # register
                _mock_response(None, status=404),                                    # get_current_version -> none yet
                _mock_response({"version_id": 1, "version_number": 1, "status": "TESTING"}),  # POST versions
                _mock_response({"versions": [                                        # poll: resolved DEPLOYED
                    {"id": 1, "version_number": 1, "status": "DEPLOYED", "quality_score": 0.95,
                     "changed_by": "sdk", "change_message": None,
                     "created_at": "2026-01-01T00:00:00Z", "deployed_at": "2026-01-01T00:00:01Z"},
                ]}),
            ]

            @aipq_prompt(name="aria_socratic", dataset="aria_golden", threshold=0.90, client=client)
            async def get_prompt() -> str:
                return "You are ARIA..."

            result = await get_prompt()
            assert result == "You are ARIA..."
            assert mock_req.call_count == 4

    @pytest.mark.asyncio
    async def test_returns_cached_when_unchanged(self, client):
        client._prompt_id_cache["aria_socratic"] = 42
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = _mock_response({
                "id": 42, "version_number": 3, "content": "Same prompt text",
                "quality_score": 0.95, "status": "DEPLOYED",
            })

            @aipq_prompt(name="aria_socratic", dataset="aria_golden", threshold=0.90, client=client)
            async def get_prompt() -> str:
                return "Same prompt text"

            result = await get_prompt()
            assert result == "Same prompt text"
            # Only the get_current_version call — no version creation for unchanged content
            assert mock_req.call_count == 1

    @pytest.mark.asyncio
    async def test_raises_quality_error_on_failing_score(self, client):
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [
                _mock_response({"prompt_id": 42}),
                _mock_response(None, status=404),
                _mock_response({"version_id": 2, "version_number": 2, "status": "TESTING"}),
                _mock_response({"versions": [
                    {"id": 2, "version_number": 2, "status": "FAILED", "quality_score": 0.62,
                     "changed_by": "sdk", "change_message": None,
                     "created_at": "2026-01-01T00:00:00Z", "deployed_at": None},
                ]}),
            ]

            @aipq_prompt(name="aria_socratic", dataset="aria_golden", threshold=0.90, client=client)
            async def get_prompt() -> str:
                return "A weaker prompt"

            with pytest.raises(PromptQualityError) as exc_info:
                await get_prompt()
            assert exc_info.value.score == 0.62

    @pytest.mark.asyncio
    async def test_fails_open_when_aipq_unreachable(self, client):
        """Network failure -> return raw text unvalidated, don't block the app."""
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            import httpx
            mock_req.side_effect = httpx.ConnectError("connection refused")

            @aipq_prompt(name="aria_socratic", dataset="aria_golden", threshold=0.90, client=client)
            async def get_prompt() -> str:
                return "You are ARIA..."

            result = await get_prompt()
            assert result == "You are ARIA..."

    @pytest.mark.asyncio
    async def test_raises_when_no_client_available(self):
        import aipq.client as client_module
        client_module._default_client = None

        @aipq_prompt(name="x", dataset="y", threshold=0.9)
        async def get_prompt() -> str:
            return "text"

        with pytest.raises(AIPQError):
            await get_prompt()


class TestCreateVersion:
    @pytest.mark.asyncio
    async def test_registers_prompt_then_creates_version(self, client):
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.side_effect = [
                _mock_response({"prompt_id": 7}),
                _mock_response({"version_id": 5, "version_number": 1, "status": "TESTING"}),
                _mock_response({"versions": [
                    {"id": 5, "version_number": 1, "status": "DEPLOYED", "quality_score": 0.99,
                     "changed_by": "sdk", "change_message": None,
                     "created_at": "2026-01-01T00:00:00Z", "deployed_at": "2026-01-01T00:00:01Z"},
                ]}),
            ]
            result = await client.create_version("my_prompt", "v2 text", "my_dataset", 0.85)
            assert result["status"] == "DEPLOYED"
            assert result["content"] == "v2 text"
            assert client._prompt_id_cache["my_prompt"] == 7

    @pytest.mark.asyncio
    async def test_raises_if_evaluation_never_resolves(self, client):
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req, \
             patch("aipq.client.asyncio.sleep", new_callable=AsyncMock):
            mock_req.side_effect = [
                _mock_response({"prompt_id": 7}),
                _mock_response({"version_id": 5, "version_number": 1, "status": "TESTING"}),
            ] + [_mock_response({"versions": [
                    {"id": 5, "version_number": 1, "status": "TESTING", "quality_score": None,
                     "changed_by": "sdk", "change_message": None,
                     "created_at": "2026-01-01T00:00:00Z", "deployed_at": None},
                ]})] * 100  # never resolves — always TESTING

            with pytest.raises(AIPQError):
                await client.create_version(
                    "my_prompt", "v2 text", "my_dataset", 0.85
                )


class TestReportUsage:
    @pytest.mark.asyncio
    async def test_silent_fail_on_unreachable(self, client):
        client._version_id_cache["p"] = 1
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            import httpx
            mock_req.side_effect = httpx.ConnectError("connection refused")
            # Must not raise
            await client.report_usage("p", output="some output", quality_score=0.8)

    @pytest.mark.asyncio
    async def test_skips_silently_when_prompt_unregistered(self, client):
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            await client.report_usage("never_registered", output="x")
            mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_sends_version_id_not_prompt_id(self, client):
        """drift_records is keyed by prompt_version_id — must not be confused with prompt_id."""
        client._prompt_id_cache["p"] = 999   # deliberately different from the version id
        client._version_id_cache["p"] = 42
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = _mock_response({"recorded": True})
            await client.report_usage("p", output="some output", quality_score=0.8)
            sent_json = mock_req.call_args.kwargs["json"]
            assert sent_json["prompt_version_id"] == 42


class TestGetBestVersion:
    @pytest.mark.asyncio
    async def test_returns_highest_scoring_version(self, client):
        client._prompt_id_cache["p"] = 1
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            mock_req.return_value = _mock_response({
                "versions": [
                    {"content": "v1", "quality_score": 0.70},
                    {"content": "v2", "quality_score": 0.95},
                    {"content": "v3", "quality_score": 0.80},
                ]
            })
            best = await client.get_best_version("p")
            assert best == "v2"


class TestCreateGoldenCase:
    @pytest.mark.asyncio
    async def test_silent_fail_on_unreachable(self, client):
        client._prompt_id_cache["p"] = 1
        with patch.object(client._session, "request", new_callable=AsyncMock) as mock_req:
            import httpx
            mock_req.side_effect = httpx.ConnectError("down")
            # Must not raise
            await client.create_golden_case("p", "input", "expected behavior")
