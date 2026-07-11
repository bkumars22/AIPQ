"""
LLM plumbing for the evaluation pipeline: one model to RUN the prompt under
test against a golden case's input, and one model to JUDGE the resulting
output (deepeval's GEval, via a custom DeepEvalBaseLLM adapter since deepeval
defaults to OpenAI and this stack's default provider is Groq).

Note on deepeval's built-in FaithfulnessMetric: that metric is built for RAG
hallucination detection against a retrieval_context, which doesn't apply
here — there's no retrieved context, just a system prompt and a golden
expected_behavior description. So both "faithfulness" and "compliance"
scores below are GEval custom judges rather than the FaithfulnessMetric
class, which is the deepeval-recommended approach for non-RAG criteria.
"""
from __future__ import annotations

import asyncio
import os

from groq import Groq

_EXECUTOR_MODEL = "llama-3.3-70b-versatile"


def _groq_client() -> Groq:
    return Groq(api_key=os.getenv("GROQ_API_KEY", ""))


async def run_prompt_under_test(system_prompt: str, user_input: str) -> str:
    """Applies the prompt version being evaluated to one golden case's input."""
    def _call() -> str:
        client = _groq_client()
        resp = client.chat.completions.create(
            model=_EXECUTOR_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_input},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_call)


_groq_deep_eval_model_cls = None


def _build_groq_deep_eval_model_cls():
    """
    Builds the real GroqDeepEvalModel class on first use, importing
    deepeval.models lazily rather than at module load time — deepeval's
    package __init__ eagerly validates a pydantic Settings object that
    chokes on AZURE_OPENAI_ENDPOINT="" (present-but-empty, as Docker Compose
    sets it when unset, rather than truly absent), so importing deepeval at
    module scope crashes ai-engine's startup entirely. Importing it lazily,
    only when a GroqDeepEvalModel is actually constructed (i.e. when an
    evaluation actually runs), matches how deepeval was already imported
    everywhere else in this codebase and keeps that separate, pre-existing
    settings bug from turning into a hard startup crash.
    """
    from deepeval.models import DeepEvalBaseLLM

    class _GroqDeepEvalModel(DeepEvalBaseLLM):
        """
        deepeval-compatible adapter for Groq's chat completion API.

        Was previously a plain (non-subclassed) duck-typed class — it
        implemented the right method names but deepeval.metrics.utils'
        isinstance(model, DeepEvalBaseLLM) check rejected it outright, so
        every GEval(model=GroqDeepEvalModel()) construction raised
        TypeError. That means every evaluation in this project has been
        failing at exactly this point — swallowed by pipeline.py's broad
        `except Exception` handler and reported as a generic failed
        evaluation — regardless of whether a real GROQ_API_KEY was
        configured. Fixing the inheritance is what actually lets a
        configured key produce a real score.
        """

        def __init__(self, model_name: str = _EXECUTOR_MODEL):
            self.model_name = model_name
            super().__init__(model_name)

        def load_model(self):
            return _groq_client()

        def generate(self, prompt: str) -> str:
            resp = self.model.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            return resp.choices[0].message.content or ""

        async def a_generate(self, prompt: str) -> str:
            return await asyncio.to_thread(self.generate, prompt)

        def get_model_name(self) -> str:
            return f"groq/{self.model_name}"

    return _GroqDeepEvalModel


def GroqDeepEvalModel(model_name: str = _EXECUTOR_MODEL):
    """Factory (not a class) so deepeval stays a lazy import — see _build_groq_deep_eval_model_cls."""
    global _groq_deep_eval_model_cls
    if _groq_deep_eval_model_cls is None:
        _groq_deep_eval_model_cls = _build_groq_deep_eval_model_cls()
    return _groq_deep_eval_model_cls(model_name)
