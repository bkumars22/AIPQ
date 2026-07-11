"""
Multi-provider LLM execution — runs a prompt-under-test against Groq, Azure
OpenAI, or Anthropic Claude, so the portability validator (validators/
portability.py) can compare the same prompt across providers, and the
causal attribution analyzer (analyzers/causal.py) can re-run the same
prompt with one generation parameter (temperature, max_tokens) reverted to
an earlier version's value.

Deliberately independent from llm_judge.py/evaluators/pipeline.py, which
stay untouched: that's the live, already-verified deployment-gating
evaluation path, and duplicating ~15 lines of Groq-calling code here is a
smaller risk than adding a shared dependency to code that blocks real
deployments.
"""
from __future__ import annotations

import asyncio
import os

SUPPORTED_PROVIDERS = ("groq", "azure", "anthropic")

_GROQ_MODEL = "llama-3.3-70b-versatile"
_ANTHROPIC_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 1024


def configured_providers() -> list[str]:
    """Which providers have a real API key/config present in this environment."""
    configured = []
    if os.getenv("GROQ_API_KEY"):
        configured.append("groq")
    if os.getenv("AZURE_OPENAI_API_KEY") and os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME"):
        configured.append("azure")
    if os.getenv("ANTHROPIC_API_KEY"):
        configured.append("anthropic")
    return configured


async def run_prompt_on_provider(
    provider: str, system_prompt: str, user_input: str,
    temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = DEFAULT_MAX_TOKENS,
) -> str:
    if provider == "groq":
        return await _run_groq(system_prompt, user_input, temperature, max_tokens)
    if provider == "azure":
        return await _run_azure(system_prompt, user_input, temperature, max_tokens)
    if provider == "anthropic":
        return await _run_anthropic(system_prompt, user_input, temperature, max_tokens)
    raise ValueError(f"Unsupported provider: {provider!r} (supported: {SUPPORTED_PROVIDERS})")


async def _run_groq(system_prompt: str, user_input: str, temperature: float, max_tokens: int) -> str:
    from groq import Groq

    def _call() -> str:
        client = Groq(api_key=os.getenv("GROQ_API_KEY", ""))
        resp = client.chat.completions.create(
            model=_GROQ_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
            temperature=temperature, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_call)


async def _run_azure(system_prompt: str, user_input: str, temperature: float, max_tokens: int) -> str:
    from openai import AzureOpenAI

    def _call() -> str:
        client = AzureOpenAI(
            api_key=os.getenv("AZURE_OPENAI_API_KEY", ""),
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
        )
        resp = client.chat.completions.create(
            model=os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", ""),
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_input}],
            temperature=temperature, max_tokens=max_tokens,
        )
        return resp.choices[0].message.content or ""

    return await asyncio.to_thread(_call)


async def _run_anthropic(system_prompt: str, user_input: str, temperature: float, max_tokens: int) -> str:
    from anthropic import Anthropic

    def _call() -> str:
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))
        resp = client.messages.create(
            model=_ANTHROPIC_MODEL, max_tokens=max_tokens, temperature=temperature,
            system=system_prompt, messages=[{"role": "user", "content": user_input}],
        )
        return resp.content[0].text if resp.content else ""

    return await asyncio.to_thread(_call)
