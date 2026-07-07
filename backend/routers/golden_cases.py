"""POST /golden-cases — SDK's create_golden_case()."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth.dependencies import AuthContext, get_auth_context
from models.schemas import GoldenCaseCreateRequest, GoldenCaseCreateResponse

router = APIRouter(prefix="/golden-cases", tags=["golden-cases"])


@router.post("", response_model=GoldenCaseCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_golden_case(
    payload: GoldenCaseCreateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Adds a test case to a prompt's (first/only) golden dataset."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", payload.prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        if auth.project_id != prompt_row["project_id"]:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")

        dataset_row = await conn.fetchrow(
            "SELECT id FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1", payload.prompt_id
        )
        if dataset_row is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This prompt has no golden_dataset registered yet — register one via /prompts/register first",
            )

        row = await conn.fetchrow(
            """
            INSERT INTO golden_cases
                (dataset_id, input_text, expected_behavior, forbidden_patterns, required_patterns, category)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING id
            """,
            dataset_row["id"], payload.input_text, payload.expected_behavior,
            json.dumps(payload.forbidden_patterns), json.dumps(payload.required_patterns), payload.category,
        )

    return GoldenCaseCreateResponse(case_id=row["id"])
