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
import re

from groq import Groq

from prompt_library import AIPQ_EVAL_JUDGE

_EXECUTOR_MODEL = "openai/gpt-oss-120b"

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _extract_json_object(text: str) -> str:
    """
    Best-effort extraction of the single JSON object a GEval judge prompt
    asked for, out of whatever surrounding text the model actually returned.

    Two real, observed failure modes this fixes (found live, 2026-09-23,
    running deepeval's GEval against Groq's openai/gpt-oss-120b — see
    GroqDeepEvalModel.generate's docstring for the two things that were
    tried and didn't work before this): a ```json ... ``` markdown fence
    around otherwise-valid JSON, and/or a short preamble or trailing
    sentence around it (e.g. "Here is the evaluation: {...}"). Deepeval's
    own parser (trimAndLoadJson) only strips trailing commas — it does a
    bare json.loads on whatever string generate() returns, so either of
    these was enough to fail every single evaluation at score 0, even
    though the judgment itself inside the braces was usually fine.

    Falls back to returning the input unchanged if no `{...}` object is
    found at all, so a genuinely empty/broken response still reaches
    deepeval's own error path (and its message) rather than being
    swallowed here.
    """
    stripped = _FENCE_RE.sub("", text.strip()).strip()

    start = stripped.find("{")
    if start == -1:
        return stripped

    depth = 0
    for i, ch in enumerate(stripped[start:], start=start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]

    # Unbalanced (e.g. truncated by max_tokens) — return from the first
    # brace onward anyway; deepeval's own JSONDecodeError is more useful
    # here than silently returning the pre-fence-strip original.
    return stripped[start:]


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
            # Temperature sourced from prompt_library.AIPQ_EVAL_JUDGE rather
            # than hardcoded — GEval builds its own judging instructions from
            # a `criteria` string, so only that config value (not
            # AIPQ_EVAL_JUDGE.system/max_tokens — see below) applies here.
            #
            # RESOLVED (2026-09-23) — see llm_judge.py's _extract_json_object
            # docstring for the fence/preamble fix. Two dead ends tried
            # first for THAT bug, kept here as the record of what didn't
            # work:
            #  1. No format constraint at all: the model's raw text often
            #     wraps its JSON in markdown fences or a short preamble even
            #     though GEval's own template explicitly says "Only return
            #     valid JSON" — deepeval's parser does a bare json.loads with
            #     no fence-stripping, so it raised "outputted an invalid
            #     JSON" on exactly those responses.
            #  2. Groq's response_format={"type":"json_object"}: rejected the
            #     request outright (400 json_validate_failed, empty
            #     failed_generation) for this model/prompt combination —
            #     strictly worse than (1), since it failed before generation
            #     even started.
            # Fix: leave the request unconstrained (avoids (2)'s outright
            # rejection) and do our own robust extraction of the JSON object
            # from the response text (fixes (1)) before handing it back to
            # deepeval's parser.
            #
            # SECOND, SEPARATE bug found after (1)/(2) above: even with the
            # extraction fix, generate() was returning "" — json.loads then
            # failed immediately with "Expecting value: line 1 column 1
            # (char 0)". Root cause: openai/gpt-oss-120b is a reasoning
            # model — Groq bills its internal chain-of-thought tokens
            # against the same max_tokens budget as the visible answer.
            # AIPQ_EVAL_JUDGE.max_tokens=200 (prompt_library.py — not
            # touched here, that config is shared with non-reasoning-model
            # judges elsewhere) is nowhere near enough for gpt-oss-120b's
            # default ("medium") reasoning effort, so the whole budget was
            # spent thinking and zero tokens were left for the JSON answer
            # itself, every single call. Fixed with two changes specific to
            # this Groq/reasoning-model call: reasoning_effort="low" (uses
            # meaningfully fewer thinking tokens) and a higher token floor
            # so the visible JSON answer has room to be emitted even so.
            resp = self.model.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=AIPQ_EVAL_JUDGE.temperature,
                max_tokens=max(AIPQ_EVAL_JUDGE.max_tokens, 1024),
                reasoning_effort="low",
            )
            return _extract_json_object(resp.choices[0].message.content or "")

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
