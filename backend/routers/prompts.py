"""
POST /prompts/register, POST /prompts/versions,
GET /prompts/{id}/versions, GET /prompts/{id}/current.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status

from auth.dependencies import AuthContext, get_auth_context
from config import ai_engine_url
from models.schemas import (
    CurrentVersionResponse,
    PromptRegisterRequest,
    PromptRegisterResponse,
    PromptVersionCreateRequest,
    PromptVersionCreateResponse,
    PromptVersionListResponse,
    PromptVersionSummary,
)

logger = logging.getLogger("aipq.backend.prompts")
router = APIRouter(prefix="/prompts", tags=["prompts"])


def _require_own_project(auth: AuthContext, project_id: int) -> None:
    """
    A project's api_key may only touch its own data. A dashboard JWT is an
    admin session with cross-project visibility (matches the same
    distinction already made in routers/projects.py) — only api_key callers
    get this ownership check.
    """
    if auth.via == "api_key" and auth.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")


@router.post("/register", response_model=PromptRegisterResponse, status_code=status.HTTP_201_CREATED)
async def register_prompt(
    payload: PromptRegisterRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Idempotent: registering an already-known (project_id, prompt_name) returns its existing id."""
    _require_own_project(auth, payload.project_id)

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT id FROM prompts WHERE project_id = $1 AND prompt_name = $2",
                payload.project_id, payload.prompt_name,
            )
            if row is None:
                row = await conn.fetchrow(
                    """
                    INSERT INTO prompts (project_id, prompt_name, description)
                    VALUES ($1, $2, $3)
                    RETURNING id
                    """,
                    payload.project_id, payload.prompt_name, payload.description,
                )

            prompt_id = row["id"]

            if payload.golden_dataset:
                existing_ds = await conn.fetchrow(
                    "SELECT id FROM golden_datasets WHERE project_id = $1 AND name = $2",
                    payload.project_id, payload.golden_dataset,
                )
                if existing_ds is None:
                    await conn.execute(
                        """
                        INSERT INTO golden_datasets (project_id, prompt_id, name, threshold)
                        VALUES ($1, $2, $3, $4)
                        """,
                        payload.project_id, prompt_id, payload.golden_dataset, payload.threshold,
                    )

    return PromptRegisterResponse(prompt_id=prompt_id)


async def _trigger_evaluation(version_id: int) -> None:
    """
    Fire-and-forget call to the ai-engine's evaluation pipeline (Prompt 6).

    ai-engine may not be running yet at this stage of the build — failures
    here are logged, not raised, so version creation is never blocked by it.
    Evaluation stays in TESTING status until ai-engine processes it.
    """
    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(f"{url}/evaluate", json={"version_id": version_id})
    except httpx.HTTPError as exc:
        logger.warning("Could not trigger evaluation for version %d (ai-engine unreachable): %s", version_id, exc)


@router.post("/versions", response_model=PromptVersionCreateResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_prompt_version(
    payload: PromptVersionCreateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Creates a new prompt version and returns immediately with status=TESTING.
    Evaluation (Prompt 6's LangGraph pipeline) runs in the background and
    updates this version's status/quality_score once it completes.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", payload.prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        next_version = await conn.fetchval(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM prompt_versions WHERE prompt_id = $1",
            payload.prompt_id,
        )
        version_row = await conn.fetchrow(
            """
            INSERT INTO prompt_versions
                (prompt_id, version_number, content, system_prompt, temperature,
                 max_tokens, changed_by, change_message, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'TESTING')
            RETURNING id, version_number, status
            """,
            payload.prompt_id, next_version, payload.content, payload.system_prompt,
            payload.temperature, payload.max_tokens, payload.changed_by, payload.change_message,
        )

    background_tasks.add_task(_trigger_evaluation, version_row["id"])

    return PromptVersionCreateResponse(
        version_id=version_row["id"],
        version_number=version_row["version_number"],
        status=version_row["status"],
    )


@router.get("/{prompt_id}/versions", response_model=PromptVersionListResponse)
async def list_prompt_versions(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        rows = await conn.fetch(
            """
            SELECT id, version_number, quality_score, status, changed_by, change_message,
                   created_at, deployed_at
            FROM prompt_versions
            WHERE prompt_id = $1
            ORDER BY version_number DESC
            """,
            prompt_id,
        )

    return PromptVersionListResponse(versions=[PromptVersionSummary(**dict(r)) for r in rows])


@router.get("/{prompt_id}/confidence")
async def get_prompt_confidence(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Proxies ai-engine's StatisticalValidator analysis for this prompt's
    version history — per-version 95% CI and significance-vs-previous-version
    (see StatisticalValidator's docstring). Returns an empty version list
    rather than an error if ai-engine is unreachable, matching the same
    "never block the dashboard on ai-engine" approach as metrics.py.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/analyze/confidence", params={"prompt_id": prompt_id})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /analyze/confidence (prompt %d): %s", prompt_id, exc)
        return {"prompt_id": prompt_id, "versions": []}


@router.get("/{prompt_id}/causal-impact")
async def get_prompt_causal_impact(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Proxies ai-engine's interrupted-time-series causal impact estimate for
    this prompt's current version vs. the one it replaced (see
    predictors/causal_impact.py). Returns a "no effect" shape rather than
    an error if ai-engine is unreachable, matching the same
    "never block the dashboard on ai-engine" approach as metrics.py.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/analyze/causal-impact", params={"prompt_id": prompt_id})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /analyze/causal-impact (prompt %d): %s", prompt_id, exc)
        return {"prompt_id": prompt_id, "estimated_effect": None, "interpretation": "ai-engine unreachable"}


@router.get("/{prompt_id}/causal-attribution")
async def get_prompt_causal_attribution(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Proxies ai-engine's per-factor causal attribution (see
    analyzers/causal.py) — unlike the other /analyze/* proxies, this one
    triggers real LLM calls per factor being tested (a re-scored
    counterfactual per changed factor), so it gets a much longer timeout.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.get(f"{url}/analyze/causal-attribution", params={"prompt_id": prompt_id})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /analyze/causal-attribution (prompt %d): %s", prompt_id, exc)
        return {"prompt_id": prompt_id, "factors": [], "interpretation": "ai-engine unreachable"}


@router.get("/{prompt_id}/current", response_model=CurrentVersionResponse)
async def get_current_version(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        row = await conn.fetchrow(
            """
            SELECT pv.id, pv.version_number, pv.content, pv.quality_score, pv.status
            FROM prompts p
            JOIN prompt_versions pv ON pv.id = p.current_version_id
            WHERE p.id = $1
            """,
            prompt_id,
        )

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No deployed version yet for this prompt")

    return CurrentVersionResponse(**dict(row))
