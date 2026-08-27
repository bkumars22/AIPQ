"""
POST /review/{thread_id}, GET /pending-reviews — human-in-the-loop
borderline review. Proxies to ai-engine, which owns the LangGraph
checkpointer these threads actually live in (see
ai-engine/evaluators/pipeline.py's resume_review/list_pending_reviews).
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth.dependencies import AuthContext, get_auth_context
from config import ai_engine_url
from models.schemas import (
    PendingReviewListResponse,
    PendingReviewSummary,
    ReviewDecisionRequest,
    ReviewDecisionResponse,
)

logger = logging.getLogger("aipq.backend.review")
router = APIRouter(tags=["review"])


@router.post("/review/{thread_id}", response_model=ReviewDecisionResponse)
async def review(
    thread_id: str,
    payload: ReviewDecisionRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    if payload.decision not in ("approve", "reject"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "decision must be 'approve' or 'reject'")

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        owner_row = await conn.fetchrow(
            """
            SELECT p.project_id FROM pending_reviews pr
            JOIN prompts p ON p.id = pr.prompt_id
            WHERE pr.thread_id = $1
            """,
            thread_id,
        )
    if owner_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No pending review found for this thread_id")
    if auth.via == "api_key" and auth.project_id != owner_row["project_id"]:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")

    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(f"{url}/review/{thread_id}", json={"decision": payload.decision})
            resp.raise_for_status()
            return ReviewDecisionResponse(**resp.json())
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /review/%s: %s", thread_id, exc)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"ai-engine unreachable: {exc}")


@router.get("/pending-reviews", response_model=PendingReviewListResponse)
async def pending_reviews(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    A dashboard JWT sees pending reviews across every project (matching the
    same admin cross-project visibility already used elsewhere — see
    routers/prompts.py's _require_own_project docstring); an api_key caller
    only ever sees its own project's reviews.
    """
    url = ai_engine_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{url}/pending-reviews")
            resp.raise_for_status()
            all_reviews = resp.json()["pending_reviews"]
    except httpx.HTTPError as exc:
        logger.warning("ai-engine unreachable for /pending-reviews: %s", exc)
        return PendingReviewListResponse(pending_reviews=[])

    if auth.via != "api_key":
        return PendingReviewListResponse(pending_reviews=[PendingReviewSummary(**r) for r in all_reviews])

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        own_prompt_ids = {
            r["id"] for r in await conn.fetch("SELECT id FROM prompts WHERE project_id = $1", auth.project_id)
        }
    filtered = [r for r in all_reviews if r["prompt_id"] in own_prompt_ids]
    return PendingReviewListResponse(pending_reviews=[PendingReviewSummary(**r) for r in filtered])
