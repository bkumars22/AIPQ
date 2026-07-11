"""
Unit tests for llm_judge.py's GroqDeepEvalModel.

Deliberately does NOT mock deepeval.metrics.GEval in the inheritance test —
that's the whole point: GroqDeepEvalModel previously duck-typed the
DeepEvalBaseLLM interface without actually subclassing it, so
deepeval.metrics.utils' isinstance() check rejected it and every
GEval(model=GroqDeepEvalModel()) call raised TypeError, regardless of
whether the Groq client itself was ever exercised. Mocking GEval away
would hide exactly the bug this file exists to catch.
"""
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from llm_judge import GroqDeepEvalModel  # noqa: E402


class TestGroqDeepEvalModel:
    def test_instance_is_a_real_deepeval_base_llm(self):
        # GroqDeepEvalModel is a factory function (not a class) so deepeval
        # stays a lazy import — see llm_judge.py's module docstring on the
        # instance for why. What matters is the returned instance still
        # really is a DeepEvalBaseLLM, not just duck-typed.
        assert isinstance(GroqDeepEvalModel(), DeepEvalBaseLLM)

    def test_geval_accepts_it_without_raising_typeerror(self):
        # The regression test: this exact construction used to raise
        # "TypeError: Unsupported type for model: <class 'llm_judge.GroqDeepEvalModel'>"
        from deepeval.metrics import GEval
        from deepeval.test_case import LLMTestCaseParams

        model = GroqDeepEvalModel()
        judge = GEval(
            name="Test", criteria="does it work",
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
            model=model,
        )
        assert judge is not None

    def test_get_model_name_includes_groq_prefix(self):
        model = GroqDeepEvalModel(model_name="llama-3.3-70b-versatile")
        assert model.get_model_name() == "groq/llama-3.3-70b-versatile"

    def test_generate_calls_groq_chat_completion(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="hello from groq"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp

        with patch("llm_judge._groq_client", return_value=mock_client):
            model = GroqDeepEvalModel()
            result = model.generate("some prompt")

        assert result == "hello from groq"
        mock_client.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_generate_delegates_to_generate(self):
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock(message=MagicMock(content="async hello"))]
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_resp

        with patch("llm_judge._groq_client", return_value=mock_client):
            model = GroqDeepEvalModel()
            result = await model.a_generate("some prompt")

        assert result == "async hello"
