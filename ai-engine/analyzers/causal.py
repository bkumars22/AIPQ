"""
CausalAttributionAnalyzer — decomposes an observed quality change between
two prompt versions into per-factor causal shares (temperature, max_tokens,
prompt length, example count), each backed by a REAL re-scored
counterfactual — an actual re-run of the golden dataset with just that one
factor reverted to the previous version's value — not a fitted/estimated
decomposition (no SHAP, no regression across versions; with typically 2-4
versions per prompt there isn't enough data to fit a multi-variable model
honestly).

Two of the four factors (temperature, max_tokens) are cleanly isolatable:
they're separate scalar columns, so "revert just this one" is exact. The
other two (prompt length, example count) are NOT cleanly isolatable —
you can't revert "just the length" of a block of text without touching its
content — so those two counterfactuals are explicit, documented heuristics
(truncating to a target length; stripping a matched number of "Example N:"
/ "e.g." lines), not a clean ablation. Every result's `note` field says so
per-factor rather than presenting a heuristic as equivalent to the exact
temperature/max_tokens counterfactuals.

Distinct from predictors/causal_impact.py (interrupted time series: did a
whole-version deployment shift quality relative to trend?) — this answers
a different, more granular question: of what changed IN that version,
which specific attribute mattered most.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from evaluators.scoring import score_content

_EXAMPLE_PATTERN = re.compile(r"^\s*(example\s*\d*[:.]|e\.g\.,?)", re.IGNORECASE)


def _count_examples(text: str) -> int:
    return sum(1 for line in text.split("\n") if _EXAMPLE_PATTERN.match(line))


def _truncate_to_length(text: str, target_length: int) -> str:
    """Crude length-matching counterfactual: truncate (never extend) to target_length characters."""
    return text if len(text) <= target_length else text[:target_length]


def _remove_last_n_examples(text: str, n: int) -> str:
    """Removes the last n example-like lines (matched by _EXAMPLE_PATTERN), to approximate an earlier, lower example count."""
    if n <= 0:
        return text
    lines = text.split("\n")
    matched_indices = [i for i, line in enumerate(lines) if _EXAMPLE_PATTERN.match(line)]
    to_remove = set(matched_indices[-n:])
    return "\n".join(line for i, line in enumerate(lines) if i not in to_remove)


@dataclass
class FactorAttribution:
    factor: str
    changed: bool
    current_value: object
    previous_value: object
    counterfactual_score: Optional[float]
    recovered_effect: Optional[float]  # counterfactual_score - current_score; sign matters, see note
    share_pct: Optional[float]
    note: str


@dataclass
class CausalAttributionResult:
    prompt_id: int
    current_version_id: Optional[int]
    previous_version_id: Optional[int]
    current_score: Optional[float]
    previous_score: Optional[float]
    total_gap: Optional[float]
    factors: list[FactorAttribution]
    interpretation: str


def _make_factor(name: str, current_value, previous_value, current_score: float, counterfactual_score: float, note: str) -> FactorAttribution:
    recovered_effect = round(counterfactual_score - current_score, 4)
    return FactorAttribution(
        factor=name, changed=True, current_value=current_value, previous_value=previous_value,
        counterfactual_score=round(counterfactual_score, 4), recovered_effect=recovered_effect,
        share_pct=None, note=note,
    )


def _unchanged_factor(name: str, value) -> FactorAttribution:
    return FactorAttribution(
        factor=name, changed=False, current_value=value, previous_value=value,
        counterfactual_score=None, recovered_effect=None, share_pct=None, note="Unchanged between versions.",
    )


class CausalAttributionAnalyzer:
    async def attribute_change(self, prompt_id: int) -> CausalAttributionResult:
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                """
                SELECT pv.id, pv.version_number, pv.content, pv.temperature, pv.max_tokens, pv.quality_score
                FROM prompts p JOIN prompt_versions pv ON pv.id = p.current_version_id
                WHERE p.id = $1
                """,
                prompt_id,
            )
            if current is None:
                return CausalAttributionResult(
                    prompt_id, None, None, None, None, None, [],
                    "No deployed version to analyze.",
                )

            previous = await conn.fetchrow(
                """
                SELECT id, content, temperature, max_tokens, quality_score
                FROM prompt_versions WHERE prompt_id = $1 AND version_number < $2
                ORDER BY version_number DESC LIMIT 1
                """,
                prompt_id, current["version_number"],
            )
            if previous is None:
                return CausalAttributionResult(
                    prompt_id, current["id"], None, current["quality_score"], None, None, [],
                    "No previous version to compare against.",
                )

            dataset_row = await conn.fetchrow(
                """
                SELECT gd.id, gd.threshold FROM golden_datasets gd
                WHERE gd.prompt_id = $1 ORDER BY gd.id LIMIT 1
                """,
                prompt_id,
            )
            if dataset_row is None:
                return CausalAttributionResult(
                    prompt_id, current["id"], previous["id"], current["quality_score"],
                    previous["quality_score"], None, [],
                    "No golden dataset registered for this prompt — cannot re-score counterfactuals.",
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
            return CausalAttributionResult(
                prompt_id, current["id"], previous["id"], current["quality_score"],
                previous["quality_score"], None, [], "No golden cases to re-score against.",
            )

        threshold = dataset_row["threshold"]
        current_content, previous_content = current["content"], previous["content"]
        current_temp, previous_temp = current["temperature"], previous["temperature"]
        current_tokens, previous_tokens = current["max_tokens"], previous["max_tokens"]

        current_result = await score_content(current_content, test_cases, threshold, temperature=current_temp, max_tokens=current_tokens)
        current_score = current_result["overall_score"]

        factors: list[FactorAttribution] = []

        if current_temp != previous_temp:
            variant = await score_content(current_content, test_cases, threshold, temperature=previous_temp, max_tokens=current_tokens)
            factors.append(_make_factor(
                "temperature", current_temp, previous_temp, current_score, variant["overall_score"],
                f"Exact counterfactual: current content re-scored with temperature reverted to "
                f"{previous_temp} (previous version's value), everything else held at current.",
            ))
        else:
            factors.append(_unchanged_factor("temperature", current_temp))

        if current_tokens != previous_tokens:
            variant = await score_content(current_content, test_cases, threshold, temperature=current_temp, max_tokens=previous_tokens)
            factors.append(_make_factor(
                "max_tokens", current_tokens, previous_tokens, current_score, variant["overall_score"],
                f"Exact counterfactual: current content re-scored with max_tokens reverted to "
                f"{previous_tokens} (previous version's value), everything else held at current.",
            ))
        else:
            factors.append(_unchanged_factor("max_tokens", current_tokens))

        current_len, previous_len = len(current_content), len(previous_content)
        if current_len != previous_len:
            variant_content = _truncate_to_length(current_content, previous_len) if current_len > previous_len else current_content
            variant = await score_content(variant_content, test_cases, threshold, temperature=current_temp, max_tokens=current_tokens)
            factors.append(_make_factor(
                "prompt_length", current_len, previous_len, current_score, variant["overall_score"],
                f"HEURISTIC counterfactual, not a clean isolation: current content truncated to the "
                f"previous version's character length ({previous_len} vs {current_len} chars). Truncation "
                f"can remove substantive rules, not just length, so this approximates 'the prompt got "
                f"longer' rather than isolating length as a pure variable.",
            ))
        else:
            factors.append(_unchanged_factor("prompt_length", current_len))

        current_examples, previous_examples = _count_examples(current_content), _count_examples(previous_content)
        if current_examples != previous_examples:
            if current_examples > previous_examples:
                variant_content = _remove_last_n_examples(current_content, current_examples - previous_examples)
                note_extra = f"removed the {current_examples - previous_examples} most recently added example-like line(s)"
            else:
                variant_content = current_content  # can't fabricate examples that were literally removed
                note_extra = "previous version had fewer examples — no meaningful counterfactual to construct by removal"
            variant = await score_content(variant_content, test_cases, threshold, temperature=current_temp, max_tokens=current_tokens)
            factors.append(_make_factor(
                "example_count", current_examples, previous_examples, current_score, variant["overall_score"],
                f"HEURISTIC counterfactual, not a clean isolation: {note_extra}, matched by a simple "
                f"'Example N:' / 'e.g.' line pattern — undercounts/miscounts examples written in other formats.",
            ))
        else:
            factors.append(_unchanged_factor("example_count", current_examples))

        total_gap = (
            round(current_score - previous["quality_score"], 4) if previous["quality_score"] is not None else None
        )
        changed = [f for f in factors if f.changed and f.recovered_effect is not None]
        total_recovered = sum(abs(f.recovered_effect) for f in changed)
        if total_recovered > 0:
            for f in changed:
                f.share_pct = round(abs(f.recovered_effect) / total_recovered * 100, 1)

        interpretation = _build_interpretation(current_score, changed)

        return CausalAttributionResult(
            prompt_id=prompt_id, current_version_id=current["id"], previous_version_id=previous["id"],
            current_score=current_score, previous_score=previous["quality_score"], total_gap=total_gap,
            factors=factors, interpretation=interpretation,
        )


def _build_interpretation(current_score: float, changed_factors: list[FactorAttribution]) -> str:
    if not changed_factors:
        return "No changed factors to attribute — nothing (tracked) differs between the two versions."
    top = max(changed_factors, key=lambda f: abs(f.recovered_effect))
    if top.recovered_effect > 0:
        return (
            f"{top.factor} is the largest contributor ({top.share_pct}% of the explained gap) — reverting "
            f"just {top.factor} to its previous value would have scored {top.counterfactual_score:.4f} "
            f"instead of {current_score:.4f}, a recovery of {top.recovered_effect:+.4f}."
        )
    return (
        f"{top.factor} is the largest contributor ({top.share_pct}% of the explained gap), but reverting it "
        f"alone would have made things worse ({top.counterfactual_score:.4f} vs {current_score:.4f}) — the "
        f"regression isn't cleanly attributable to this one factor."
    )
