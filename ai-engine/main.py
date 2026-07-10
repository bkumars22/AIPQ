"""AIPQ ai-engine — receives evaluation triggers from the backend and runs the LangGraph pipeline."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from analyzers.coverage import COVERED_THRESHOLD, PromptCoverageAnalyzer
from db import close_all, get_pool
from evaluators.pipeline import run_evaluation
from predictors.drift_predictor import PredictiveDriftEngine
from scheduler import start_scheduler
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


@app.get("/health")
async def health():
    return {"status": "ok"}
