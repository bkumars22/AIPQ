"""
CausalImpactAnalyzer — did deploying this prompt version actually CAUSE a
quality change, or did the score just happen to move around the same time
for some other reason (model drift, traffic mix, seasonality)?

Method: interrupted time series / segmented regression, a well-established
quasi-experimental causal design (see e.g. Bernal, Cummins & Gasparrini,
"Interrupted time series regression for the evaluation of public health
interventions," Int J Epidemiol 2017 — the same design behind Google's
CausalImpact package). Not a full Pearl-style DAG/do-calculus analysis
(AIPQ doesn't track the kind of multi-variable confounder data — model
version, traffic composition, etc. — that a backdoor-adjustment analysis
would need); this is the honest, data-grounded version of "was this
correlation or causation" that fits what AIPQ actually measures: one
score time series with a clean before/after cutpoint at deployed_at.

The idea: fit the trend from BEFORE the deployment, extrapolate it forward
as the counterfactual ("what quality would have been without this
change"), then test whether the AFTER period's actual scores differ from
that counterfactual — not from the pre-period's raw average, which would
wrongly attribute a pre-existing trend to the deployment.

Biggest threat to validity, stated plainly: if something else changed at
the same moment (the LLM provider shipped a model update the same day),
this design cannot separate that from the prompt change — it measures
"did quality change right at the cutpoint," not "was the prompt the only
possible cause." Report this alongside every result, not just in a
docstring nobody reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from scipy import stats

MIN_PERIOD_POINTS = 5
DEFAULT_ALPHA = 0.05


@dataclass
class CausalImpactResult:
    pre_period_mean: Optional[float]
    post_period_mean: Optional[float]
    counterfactual_mean: Optional[float]
    estimated_effect: Optional[float]
    relative_effect_pct: Optional[float]
    p_value: Optional[float]
    is_significant: bool
    sample_size_pre: int
    sample_size_post: int
    interpretation: str
    caveat: str


_CAVEAT = (
    "Interrupted time series design: measures whether quality changed at the "
    "deployment cutpoint relative to the pre-existing trend, not true causal "
    "isolation — a simultaneous confound (e.g. an LLM provider model update at "
    "the same time) cannot be distinguished from the prompt change itself."
)


def _linear_fit(ys: list[float]) -> tuple[float, float]:
    n = len(ys)
    xs = list(range(n))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    slope = cov / var_x if var_x else 0.0
    intercept = mean_y - slope * mean_x
    return slope, intercept


class CausalImpactAnalyzer:
    def estimate_impact(
        self, pre_scores: list[float], post_scores: list[float], alpha: float = DEFAULT_ALPHA,
    ) -> CausalImpactResult:
        """
        pre_scores / post_scores: chronologically-ordered quality scores from
        immediately before and after a single intervention (e.g. a prompt
        version's deployment).
        """
        n_pre, n_post = len(pre_scores), len(post_scores)

        if n_pre < MIN_PERIOD_POINTS or n_post < MIN_PERIOD_POINTS:
            return CausalImpactResult(
                pre_period_mean=sum(pre_scores) / n_pre if pre_scores else None,
                post_period_mean=sum(post_scores) / n_post if post_scores else None,
                counterfactual_mean=None, estimated_effect=None, relative_effect_pct=None,
                p_value=None, is_significant=False, sample_size_pre=n_pre, sample_size_post=n_post,
                interpretation=(
                    f"Insufficient data for a causal estimate (need >= {MIN_PERIOD_POINTS} points "
                    f"per period, have {n_pre} pre / {n_post} post) — keep collecting data."
                ),
                caveat=_CAVEAT,
            )

        # Counterfactual: extrapolate the PRE-period's own trend forward across
        # the post period's timespan — this is "what would have happened
        # without the change," not "the pre-period's raw average."
        slope, intercept = _linear_fit(pre_scores)
        counterfactual = [slope * (n_pre + i) + intercept for i in range(n_post)]
        counterfactual_mean = sum(counterfactual) / n_post

        pre_period_mean = sum(pre_scores) / n_pre
        post_period_mean = sum(post_scores) / n_post
        estimated_effect = post_period_mean - counterfactual_mean
        relative_effect_pct = (
            round(estimated_effect / counterfactual_mean * 100, 2) if counterfactual_mean else None
        )

        # One-sample t-test on (actual - counterfactual) residuals against 0 —
        # tests whether the post period is systematically off the pre-trend's
        # own trajectory, which is the correct ITS significance test (a
        # two-sample test on raw pre vs post means would wrongly credit the
        # deployment for a trend that was already happening).
        residuals = [actual - cf for actual, cf in zip(post_scores, counterfactual)]
        if all(r == residuals[0] for r in residuals):
            p_value = 0.0 if residuals[0] != 0 else 1.0
        else:
            _, p_value = stats.ttest_1samp(residuals, 0.0)
            p_value = float(p_value)
        is_significant = p_value < alpha

        if is_significant and estimated_effect > 0:
            interpretation = (
                f"Significant improvement: quality is {estimated_effect:+.4f} above what the "
                f"pre-deployment trend predicted (p={p_value:.4g})."
            )
        elif is_significant and estimated_effect < 0:
            interpretation = (
                f"Significant regression: quality is {estimated_effect:+.4f} below what the "
                f"pre-deployment trend predicted (p={p_value:.4g}) — this deployment likely caused it."
            )
        else:
            interpretation = (
                f"No significant deviation from the pre-existing trend (p={p_value:.4g}) — "
                f"the post-period change, if any, is consistent with a trend already in motion."
            )

        return CausalImpactResult(
            pre_period_mean=round(pre_period_mean, 4), post_period_mean=round(post_period_mean, 4),
            counterfactual_mean=round(counterfactual_mean, 4), estimated_effect=round(estimated_effect, 4),
            relative_effect_pct=relative_effect_pct, p_value=round(p_value, 6), is_significant=is_significant,
            sample_size_pre=n_pre, sample_size_post=n_post, interpretation=interpretation, caveat=_CAVEAT,
        )

    async def estimate_impact_for_version(self, prompt_id: int) -> CausalImpactResult:
        """
        Compares the currently-deployed version's quality history against
        the version it replaced, split at the deployment boundary — the
        DB-integrated entry point ai-engine's /analyze/causal-impact uses.
        """
        from db import get_pool

        pool = await get_pool()
        async with pool.acquire() as conn:
            current = await conn.fetchrow(
                """
                SELECT pv.id, pv.version_number FROM prompts p
                JOIN prompt_versions pv ON pv.id = p.current_version_id
                WHERE p.id = $1
                """,
                prompt_id,
            )
            if current is None:
                return self.estimate_impact([], [])

            previous = await conn.fetchrow(
                """
                SELECT id FROM prompt_versions
                WHERE prompt_id = $1 AND version_number < $2
                ORDER BY version_number DESC LIMIT 1
                """,
                prompt_id, current["version_number"],
            )
            if previous is None:
                return self.estimate_impact([], [])

            pre_rows = await conn.fetch(
                "SELECT compliance_score FROM drift_records WHERE prompt_version_id = $1 ORDER BY recorded_at ASC",
                previous["id"],
            )
            post_rows = await conn.fetch(
                "SELECT compliance_score FROM drift_records WHERE prompt_version_id = $1 ORDER BY recorded_at ASC",
                current["id"],
            )

        pre_scores = [r["compliance_score"] for r in pre_rows]
        post_scores = [r["compliance_score"] for r in post_rows]
        return self.estimate_impact(pre_scores, post_scores)
