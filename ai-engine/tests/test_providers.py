"""Unit tests for providers.py — no real network calls, SDK clients mocked."""
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import providers  # noqa: E402


class TestConfiguredProviders:
    def test_empty_when_no_keys_set(self):
        with patch.dict(os.environ, {}, clear=True):
            assert providers.configured_providers() == []

    def test_detects_groq(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "gsk_x"}, clear=True):
            assert providers.configured_providers() == ["groq"]

    def test_azure_requires_all_three_vars(self):
        with patch.dict(os.environ, {"AZURE_OPENAI_API_KEY": "x"}, clear=True):
            assert "azure" not in providers.configured_providers()
        with patch.dict(os.environ, {
            "AZURE_OPENAI_API_KEY": "x", "AZURE_OPENAI_ENDPOINT": "https://x",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o",
        }, clear=True):
            assert "azure" in providers.configured_providers()

    def test_detects_anthropic(self):
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-x"}, clear=True):
            assert providers.configured_providers() == ["anthropic"]

    def test_detects_all_three_together(self):
        with patch.dict(os.environ, {
            "GROQ_API_KEY": "gsk_x", "ANTHROPIC_API_KEY": "sk-ant-x",
            "AZURE_OPENAI_API_KEY": "x", "AZURE_OPENAI_ENDPOINT": "https://x",
            "AZURE_OPENAI_DEPLOYMENT_NAME": "gpt-4o",
        }, clear=True):
            assert set(providers.configured_providers()) == {"groq", "azure", "anthropic"}


class TestRunPromptOnProvider:
    @pytest.mark.asyncio
    async def test_routes_to_groq(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="groq says hi"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch("groq.Groq", return_value=mock_client):
            result = await providers.run_prompt_on_provider("groq", "system", "hello")
        assert result == "groq says hi"

    @pytest.mark.asyncio
    async def test_routes_to_azure(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="azure says hi"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp
        with patch("openai.AzureOpenAI", return_value=mock_client):
            result = await providers.run_prompt_on_provider("azure", "system", "hello")
        assert result == "azure says hi"

    @pytest.mark.asyncio
    async def test_routes_to_anthropic(self):
        mock_resp = MagicMock()
        mock_resp.content = [MagicMock(text="claude says hi")]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_resp
        with patch("anthropic.Anthropic", return_value=mock_client):
            result = await providers.run_prompt_on_provider("anthropic", "system", "hello")
        assert result == "claude says hi"

    @pytest.mark.asyncio
    async def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError):
            await providers.run_prompt_on_provider("openai-direct", "system", "hello")
