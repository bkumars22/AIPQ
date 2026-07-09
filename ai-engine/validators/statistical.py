"""
StatisticalValidator — is a prompt version's quality change actually
significant, or noise from a small sample of runs?

Not yet wired into the dashboard (see routers/prompts.py, frontend
ProjectPrompts.tsx) — this file implements the validator only, per this
task's scope. The intended integration: after every evaluation run, call
collect_scores() for the new and previous DEPLOYED version, run
validate_improvement(), and show "Score: 0.93 ± 0.02 (95% CI) —
Significantly better than v2: Yes (p=0.0001, effect size: Large d=4.2)"
next to each version in the dashboard's version table.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from scipy import stats

MIN_SAMPLES = 10
DEFAULT_ALPHA = 0.05


@dataclass
class ImprovementResult:
    p_value: Optional[float]
    effect_size: Optional[float]
    effect_size_label: Optional[str]
    confidence_interval_95: Optional[tuple[float, float]]
    sample_size: int
    previous_sample_size: int
    is_significant: bool
    recommendation: str


def _effect_size_label(d: float) -> str:
    ad = abs(d)
    if ad < 0.2:
        return "Negligible"
    if ad < 0.5:
        return "Small"
    if ad < 0.8:
        return "Medium"
    return "Large"


def _cohens_d(a: list[float], b: list[float]) -> float:
    mean_a, mean_b = sum(a) / len(a), sum(b) / len(b)
    var_a = sum((x - mean_a) ** 2 for x in a) / (len(a) - 1) if len(a) > 1 else 0.0
    var_b = sum((x - mean_b) ** 2 for x in b) / (len(b) - 1) if len(b) > 1 else 0.0
    pooled_n = len(a) + len(b) - 2
    pooled_std = math.sqrt(((len(a) - 1) * var_a + (len(b) - 1) * var_b) / pooled_n) if pooled_n > 0 else 0.0
    if pooled_std == 0:
        return 0.0
    return (mean_a - mean_b) / pooled_std


def _confidence_interval_95(scores: list[float]) -> tuple[float, float]:
    n = len(scores)
    mean = sum(scores) / n
    if n < 2:
        return (round(mean, 4), round(mean, 4))
    std = math.sqrt(sum((x - mean) ** 2 for x in scores) / (n - 1))
    sem = std / math.sqrt(n)
    t_crit = stats.t.ppf(0.975, df=n - 1)
    margin = t_crit * sem
    return (round(mean - margin, 4), round(mean + margin, 4))


class StatisticalValidator:
    async def collect_scores(self, prompt_version_id: int) -> list[float]:
        """
        Every recorded compliance_score for this version, oldest first —
        compliance_score is treated as "the" quality score throughout AIPQ
        (see evaluators/pipeline.py's evaluation_summary.overall_score).

        Fewer than MIN_SAMPLES (10) scores isn't an error here — it's a
        valid (if statistically underpowered) result; validate_improvement
        is what actually enforces the minimum before drawing a conclusion.
        """
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT compliance_score FROM drift_records
                WHERE prompt_version_id = $1
                ORDER BY recorded_at ASC
                """,
                prompt_version_id,
            )
        return [row["compliance_score"] for row in rows]

    def validate_improvement(
        self,
        current_scores: list[float],
        previous_scores: list[float],
        alpha: float = DEFAULT_ALPHA,
    ) -> dict:
        n_current, n_previous = len(current_scores), len(previous_scores)

        if n_current < MIN_SAMPLES or n_previous < MIN_SAMPLES:
            return ImprovementResult(
                p_value=None, effect_size=None, effect_size_label=None,
                confidence_interval_95=_confidence_interval_95(current_scores) if current_scores else None,
                sample_size=n_current, previous_sample_size=n_previous,
                is_significant=False,
                recommendation=(
                    f"Insufficient samples for a significance test (need >= {MIN_SAMPLES} per version, "
                    f"have {n_current} current / {n_previous} previous) — keep collecting data."
                ),
            ).__dict__

        _, p_value = stats.ttest_ind(current_scores, previous_scores, equal_var=False)
        effect_size = _cohens_d(current_scores, previous_scores)
        effect_label = _effect_size_label(effect_size)
        is_significant = p_value < alpha

        mean_current = sum(current_scores) / n_current
        mean_previous = sum(previous_scores) / n_previous

        if is_significant and mean_current > mean_previous:
            recommendation = f"Deploy — significantly better ({effect_label.lower()} effect, p={p_value:.4g})"
        elif is_significant and mean_current < mean_previous:
            recommendation = f"Do not deploy — significantly worse ({effect_label.lower()} effect, p={p_value:.4g})"
        else:
            recommendation = f"No significant difference detected (p={p_value:.4g}) — treat as equivalent"

        return ImprovementResult(
            p_value=round(float(p_value), 6),
            effect_size=round(float(effect_size), 4),
            effect_size_label=effect_label,
            confidence_interval_95=_confidence_interval_95(current_scores),
            sample_size=n_current,
            previous_sample_size=n_previous,
            is_significant=bool(is_significant),
            recommendation=recommendation,
        ).__dict__

    def minimum_sample_calculator(
        self, expected_effect: float, alpha: float = DEFAULT_ALPHA, power: float = 0.80,
    ) -> int:
        """
        Required samples per group to detect `expected_effect` (Cohen's d) at
        the given alpha/power, for a two-sided two-sample t-test.

        Uses the standard normal-approximation formula
        (n = 2 * ((z_a/2 + z_beta) / d)^2) rather than iterating the exact
        noncentral-t solution — the standard first-pass estimate used by most
        sample-size calculators, accurate enough to plan how many eval runs
        to collect before checking significance.
        """
        if expected_effect <= 0:
            raise ValueError("expected_effect must be positive")

        z_alpha = stats.norm.ppf(1 - alpha / 2)
        z_beta = stats.norm.ppf(power)
        n = 2 * ((z_alpha + z_beta) / expected_effect) ** 2
        return math.ceil(n)
