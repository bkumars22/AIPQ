"""
PromptPortabilityValidator — tests whether a deployed prompt's quality
holds up across multiple LLM providers (Groq, Azure OpenAI, Anthropic
Claude), not just the one it happens to be evaluated against day to day.

Method: re-scores the SAME prompt content against the SAME golden dataset
once per configured provider (evaluators/scoring.score_content, with the
judge model held fixed — see caveat below), then reports a
portability_score = min_score / max_score across providers (1.0 = fully
portable, no quality gap between providers).

Only providers with a real API key configured in this environment are
actually tested — configured_providers() reports which ones, and any
others show up as "skipped," not silently ignored. A prompt tested against
only one provider can't have a meaningful portability score; that's
reported honestly rather than defaulting one provider's score to 1.0.

Caveat, stated plainly: the JUDGE model (GEval via GroqDeepEvalModel,
scoring.py's fixed judge) stays the same across every provider being
compared, for a fair comparison of the EXECUTOR models — but that judge
was itself trained/tuned implicitly by whatever criteria it was built
against, so a systematic judge bias toward one provider's output style
isn't ruled out by this design. A truly bias-free comparison would need a
provider-independent judge (e.g. a panel), which isn't built here.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from evaluators.scoring import score_content
from providers import SUPPORTED_PROVIDERS, configured_providers

PORTABILITY_WARNING_THRESHOLD = 0.9  # min/max ratio below this triggers a warning


@dataclass
class ProviderScore:
    provider: str
    overall_score: Optional[float]
    error: Optional[str]


@dataclass
class PortabilityResult:
    prompt_id: int
    version_id: Optional[int]
    providers_tested: list[str]
    providers_skipped: list[str]
    scores: list[ProviderScore]
    min_score: Optional[float]
    max_score: Optional[float]
    portability_score: Optional[float]
    warning: Optional[str]
    interpretation: str


class PromptPortabilityValidator:
    async def check_portability(self, prompt_id: int) -> PortabilityResult:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                """
                SELECT pv.id, pv.content, pv.temperature, pv.max_tokens
                FROM prompts p JOIN prompt_versions pv ON pv.id = p.current_version_id
                WHERE p.id = $1
                """,
                prompt_id,
            )
            if current is None:
                return PortabilityResult(
                    prompt_id, None, [], [], [], None, None, None, None, "No deployed version to test.",
                )

            dataset_row = await conn.fetchrow(
                "SELECT id, threshold FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1",
                prompt_id,
            )
            if dataset_row is None:
                return PortabilityResult(
                    prompt_id, current["id"], [], [], [], None, None, None, None,
                    "No golden dataset registered for this prompt — cannot test portability.",
                )

            case_rows = await conn.fetch(
                """
                SELECT id, input_text, expected_behavior, forbidden_patterns, required_patterns
                FROM golden_cases WHERE dataset_id = $1
                """,
                dataset_row["id"],
            )

        test_cases = [
            {
                "id": r["id"], "input_text": r["input_text"], "expected_behavior": r["expected_behavior"],
                "forbidden_patterns": json.loads(r["forbidden_patterns"]) if isinstance(r["forbidden_patterns"], str) else r["forbidden_patterns"],
                "required_patterns": json.loads(r["required_patterns"]) if isinstance(r["required_patterns"], str) else r["required_patterns"],
            }
            for r in case_rows
        ]
        if not test_cases:
            return PortabilityResult(
                prompt_id, current["id"], [], [], [], None, None, None, None,
                "No golden cases to test against.",
            )

        available = configured_providers()
        skipped = [p for p in SUPPORTED_PROVIDERS if p not in available]

        if not available:
            return PortabilityResult(
                prompt_id, current["id"], [], skipped, [], None, None, None, None,
                "No LLM provider configured — set at least one of GROQ_API_KEY / ANTHROPIC_API_KEY / "
                "AZURE_OPENAI_* to test portability.",
            )

        scores: list[ProviderScore] = []
        for provider in available:
            try:
                result = await score_content(
                    current["content"], test_cases, dataset_row["threshold"], provider=provider,
                    temperature=current["temperature"], max_tokens=current["max_tokens"],
                )
                scores.append(ProviderScore(provider, result["overall_score"], None))
            except Exception as exc:
                scores.append(ProviderScore(provider, None, str(exc)))

        successful = [s for s in scores if s.overall_score is not None]
        if not successful:
            return PortabilityResult(
                prompt_id, current["id"], available, skipped, scores, None, None, None, None,
                "Every configured provider call failed — check API keys/connectivity "
                f"(errors: {', '.join(f'{s.provider}: {s.error}' for s in scores)}).",
            )

        if len(successful) < 2:
            only = successful[0]
            return PortabilityResult(
                prompt_id=prompt_id, version_id=current["id"], providers_tested=available,
                providers_skipped=skipped, scores=scores, min_score=round(only.overall_score, 4),
                max_score=round(only.overall_score, 4), portability_score=None, warning=None,
                interpretation=(
                    f"Only {only.provider} is configured (score {only.overall_score:.2f}) — "
                    f"add another provider's key to get a real portability comparison."
                ),
            )

        min_entry = min(successful, key=lambda s: s.overall_score)
        max_entry = max(successful, key=lambda s: s.overall_score)
        min_score, max_score = min_entry.overall_score, max_entry.overall_score
        portability_score = round(min_score / max_score, 4) if max_score > 0 else None

        warning = None
        if portability_score is not None and portability_score < PORTABILITY_WARNING_THRESHOLD:
            drop_pct = round((1 - portability_score) * 100, 1)
            warning = (
                f"This prompt is NOT reliably portable: {min_entry.provider} scores {min_score:.2f} vs "
                f"{max_entry.provider}'s {max_score:.2f} — a {drop_pct}% relative drop if you switch from "
                f"{max_entry.provider} to {min_entry.provider}."
            )

        score_summary = ", ".join(f"{s.provider}={s.overall_score:.2f}" for s in successful)
        interpretation = f"Tested on {len(successful)} provider(s): {score_summary}. " + (
            warning or "Scores are consistent across tested providers — this prompt looks portable."
        )

        return PortabilityResult(
            prompt_id=prompt_id, version_id=current["id"], providers_tested=available,
            providers_skipped=skipped, scores=scores, min_score=round(min_score, 4),
            max_score=round(max_score, 4), portability_score=portability_score,
            warning=warning, interpretation=interpretation,
        )
