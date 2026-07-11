"""AIPQ ai-engine — receives evaluation triggers from the backend and runs the LangGraph pipeline."""
from __future__ import annotations

import os

# deepeval's own pydantic Settings validates AZURE_OPENAI_ENDPOINT as a URL
# and crashes the whole process on import if it's present-but-empty (as
# opposed to truly unset) — which is exactly what happens whether it comes
# from a docker-compose.yml default, a stray blank line in .env, or an
# unset Render dashboard var. Scrub it before anything below (pipeline.py
# -> llm_judge.py) can trigger a deepeval import, regardless of how this
# process was launched.
if not os.environ.get("AZURE_OPENAI_ENDPOINT"):
    os.environ.pop("AZURE_OPENAI_ENDPOINT", None)

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from analyzers.causal import CausalAttributionAnalyzer
from analyzers.coverage import COVERED_THRESHOLD, PromptCoverageAnalyzer
from db import close_all, get_pool
from evaluators.pipeline import run_evaluation
from predictors.causal_impact import CausalImpactAnalyzer
from predictors.drift_predictor import PredictiveDriftEngine
from scheduler import start_scheduler
from validators.portability import PromptPortabilityValidator
from validators.statistical import StatisticalValidator, confidence_interval_95

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("aipq.ai-engine")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    app.state.scheduler = start_scheduler()
    yield
    app.state.scheduler.shutdown(wait=False)
    await close_all()


app = FastAPI(title="AIPQ AI Engine", version="0.1.0", lifespan=lifespan)


class EvaluateRequest(BaseModel):
    version_id: int


async def _resolve_and_run(version_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT prompt_id FROM prompt_versions WHERE id = $1", version_id
        )
        if row is None:
            logger.error("evaluate: prompt_version %d not found", version_id)
            return
        prompt_id = row["prompt_id"]

        dataset_row = await conn.fetchrow(
            "SELECT id, threshold FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1",
            prompt_id,
        )
        if dataset_row is None:
            logger.error("evaluate: no golden_dataset registered for prompt %d — cannot evaluate version %d",
                         prompt_id, version_id)
            await conn.execute("UPDATE prompt_versions SET status = 'FAILED' WHERE id = $1", version_id)
            return

    await run_evaluation(
        version_id=version_id,
        prompt_id=prompt_id,
        golden_dataset_id=dataset_row["id"],
        threshold=dataset_row["threshold"],
    )


@app.post("/evaluate", status_code=202)
async def evaluate(payload: EvaluateRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(_resolve_and_run, payload.version_id)
    return {"accepted": True, "version_id": payload.version_id}


@app.get("/analyze/coverage-gaps")
async def coverage_gaps():
    """
    Runs PromptCoverageAnalyzer.analyze on every prompt's currently deployed
    content, flags categories below COVERED_THRESHOLD with their status
    (PARTIAL/GAP) and a specific recommendation string. Read-only, computed
    fresh each call (no caching — this is meant to be called occasionally by
    the dashboard, not per-request-hot-path).
    """
    analyzer = PromptCoverageAnalyzer()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT proj.id AS project_id, proj.name AS project_name,
                   p.prompt_name, pv.content
            FROM prompts p
            JOIN projects proj ON proj.id = p.project_id
            JOIN prompt_versions pv ON pv.id = p.current_version_id
            """
        )

    gaps = []
    for row in rows:
        analysis = analyzer.analyze(row["content"])
        for category, data in analysis["categories"].items():
            if data["score"] < COVERED_THRESHOLD:
                gaps.append({
                    "project_id": row["project_id"], "project_name": row["project_name"],
                    "prompt_name": row["prompt_name"], "category": category,
                    "score": data["score"], "status": data["status"],
                    "recommendation": data["recommendation"],
                })
    return {"gaps": gaps}


@app.get("/analyze/predictions")
async def predictions():
    """Runs PredictiveDriftEngine.predict_quality_trend for every currently deployed prompt version."""
    engine = PredictiveDriftEngine()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT proj.id AS project_id, proj.name AS project_name,
                   p.prompt_name, pv.id AS version_id
            FROM prompts p
            JOIN projects proj ON proj.id = p.project_id
            JOIN prompt_versions pv ON pv.id = p.current_version_id
            """
        )

    results = []
    for row in rows:
        trend = await engine.predict_quality_trend(row["version_id"])
        results.append({
            "project_id": row["project_id"], "project_name": row["project_name"],
            "prompt_name": row["prompt_name"], **trend,
        })
    return {"predictions": results}


@app.get("/analyze/confidence")
async def confidence(prompt_id: int):
    """
    Per-version 95% confidence interval and significance-vs-previous-version,
    for every version of one prompt — StatisticalValidator's intended
    dashboard integration (see that module's docstring): turns "Score: 0.93"
    into "Score: 0.93 +/- 0.02 (95% CI) — significantly better than v2:
    yes (p=0.0001, effect size: Large d=4.2)" in the version history table.
    """
    validator = StatisticalValidator()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, version_number FROM prompt_versions WHERE prompt_id = $1 ORDER BY version_number ASC",
            prompt_id,
        )

    results = []
    previous_scores: list[float] | None = None
    previous_version_number: int | None = None
    for row in rows:
        scores = await validator.collect_scores(row["id"])
        entry = {
            "version_id": row["id"], "version_number": row["version_number"],
            "sample_size": len(scores),
            "mean_score": round(sum(scores) / len(scores), 4) if scores else None,
            "confidence_interval_95": list(confidence_interval_95(scores)) if scores else None,
            "vs_previous": None,
        }
        if previous_scores is not None:
            comparison = validator.validate_improvement(scores, previous_scores)
            entry["vs_previous"] = {"version_number": previous_version_number, **comparison}
        results.append(entry)
        previous_scores, previous_version_number = scores, row["version_number"]

    return {"prompt_id": prompt_id, "versions": results}


@app.get("/analyze/causal-impact")
async def causal_impact(prompt_id: int):
    """
    Interrupted-time-series causal impact of the currently-deployed version
    vs. the one it replaced — see predictors/causal_impact.py's module
    docstring for the method and its limitations. Returns a "no effect"
    result (not an error) when there's no previous version or too little
    history on either side to estimate anything.
    """
    analyzer = CausalImpactAnalyzer()
    result = await analyzer.estimate_impact_for_version(prompt_id)
    return {
        "prompt_id": prompt_id,
        "pre_period_mean": result.pre_period_mean,
        "post_period_mean": result.post_period_mean,
        "counterfactual_mean": result.counterfactual_mean,
        "estimated_effect": result.estimated_effect,
        "relative_effect_pct": result.relative_effect_pct,
        "p_value": result.p_value,
        "is_significant": result.is_significant,
        "sample_size_pre": result.sample_size_pre,
        "sample_size_post": result.sample_size_post,
        "interpretation": result.interpretation,
        "caveat": result.caveat,
    }


@app.get("/analyze/causal-attribution")
async def causal_attribution(prompt_id: int):
    """
    Per-factor causal attribution for the currently-deployed version vs.
    the one it replaced — see analyzers/causal.py's module docstring for
    the method (real re-scored counterfactuals, not a fitted decomposition)
    and which factors are exact (temperature, max_tokens) vs. heuristic
    (prompt_length, example_count).

    Unlike the DB-only /analyze/* routes, this one makes real LLM calls per
    changed factor — if the configured provider is unreachable or has no
    valid API key, that's reported as a clear message, not an opaque 500.
    """
    analyzer = CausalAttributionAnalyzer()
    try:
        result = await analyzer.attribute_change(prompt_id)
    except Exception as exc:
        logger.warning("causal_attribution failed for prompt %d: %s", prompt_id, exc)
        return {
            "prompt_id": prompt_id, "current_version_id": None, "previous_version_id": None,
            "current_score": None, "previous_score": None, "total_gap": None, "factors": [],
            "interpretation": f"Could not complete analysis — LLM provider call failed: {exc}",
        }
    return {
        "prompt_id": result.prompt_id,
        "current_version_id": result.current_version_id,
        "previous_version_id": result.previous_version_id,
        "current_score": result.current_score,
        "previous_score": result.previous_score,
        "total_gap": result.total_gap,
        "factors": [
            {
                "factor": f.factor, "changed": f.changed, "current_value": f.current_value,
                "previous_value": f.previous_value, "counterfactual_score": f.counterfactual_score,
                "recovered_effect": f.recovered_effect, "share_pct": f.share_pct, "note": f.note,
            }
            for f in result.factors
        ],
        "interpretation": result.interpretation,
    }


@app.get("/analyze/portability")
async def portability(prompt_id: int):
    """
    Cross-provider portability check for the currently-deployed version —
    see validators/portability.py's module docstring for the method,
    which providers are actually tested (only ones with a configured key),
    and the judge-model caveat.
    """
    validator = PromptPortabilityValidator()
    try:
        result = await validator.check_portability(prompt_id)
    except Exception as exc:
        logger.warning("portability check failed for prompt %d: %s", prompt_id, exc)
        return {
            "prompt_id": prompt_id, "version_id": None, "providers_tested": [], "providers_skipped": [],
            "scores": [], "min_score": None, "max_score": None, "portability_score": None, "warning": None,
            "interpretation": f"Could not complete portability check: {exc}",
        }
    return {
        "prompt_id": result.prompt_id,
        "version_id": result.version_id,
        "providers_tested": result.providers_tested,
        "providers_skipped": result.providers_skipped,
        "scores": [
            {"provider": s.provider, "overall_score": s.overall_score, "error": s.error}
            for s in result.scores
        ],
        "min_score": result.min_score,
        "max_score": result.max_score,
        "portability_score": result.portability_score,
        "warning": result.warning,
        "interpretation": result.interpretation,
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
