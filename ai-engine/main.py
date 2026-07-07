"""AIPQ ai-engine — receives evaluation triggers from the backend and runs the LangGraph pipeline."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel

from db import close_all, get_pool
from evaluators.pipeline import run_evaluation
from scheduler import start_scheduler

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


@app.get("/health")
async def health():
    return {"status": "ok"}
