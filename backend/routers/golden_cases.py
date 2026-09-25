"""
Golden Dataset Manager — real CRUD for a prompt's golden test cases.

Was POST /golden-cases only (the SDK's create_golden_case()). This adds
the read/update/delete side a real dashboard "Golden Dataset Manager"
page needs: list a prompt's datasets (flagging which one is actually
evaluated — see GoldenDatasetSummary.is_active's docstring), list/edit/
delete individual cases.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth.dependencies import AuthContext, get_auth_context
from models.schemas import (
    GoldenCaseCreateRequest,
    GoldenCaseCreateResponse,
    GoldenCaseListResponse,
    GoldenCaseSummary,
    GoldenCaseUpdateRequest,
    GoldenDatasetListResponse,
    GoldenDatasetSummary,
)

router = APIRouter(tags=["golden-cases"])


def _require_own_project(auth: AuthContext, project_id: int) -> None:
    """
    A project's api_key may only touch its own data. A dashboard JWT is an
    admin session with cross-project visibility — matches the same
    distinction used in routers/prompts.py and routers/projects.py. The
    original POST /golden-cases below this compared auth.project_id
    directly, which incorrectly rejected dashboard/admin JWT callers
    (whose project_id claim is 0, an admin sentinel, not a real project)
    — fixed here.
    """
    if auth.via == "api_key" and auth.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Credential does not belong to this project")


def _row_to_case(row) -> GoldenCaseSummary:
    forbidden = row["forbidden_patterns"]
    required = row["required_patterns"]
    return GoldenCaseSummary(
        id=row["id"],
        input_text=row["input_text"],
        expected_behavior=row["expected_behavior"],
        forbidden_patterns=json.loads(forbidden) if isinstance(forbidden, str) else forbidden,
        required_patterns=json.loads(required) if isinstance(required, str) else required,
        category=row["category"],
        created_at=row["created_at"].isoformat(),
    )


@router.post("/golden-cases", response_model=GoldenCaseCreateResponse, status_code=status.HTTP_201_CREATED)
async def create_golden_case(
    payload: GoldenCaseCreateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Adds a test case to a prompt's (first/active) golden dataset."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", payload.prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

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


@router.get("/prompts/{prompt_id}/golden-datasets", response_model=GoldenDatasetListResponse)
async def list_golden_datasets(
    prompt_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Every golden_datasets row for this prompt — only the first (lowest id) is ever real-evaluated."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        prompt_row = await conn.fetchrow("SELECT project_id FROM prompts WHERE id = $1", prompt_id)
        if prompt_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Prompt not found")
        _require_own_project(auth, prompt_row["project_id"])

        rows = await conn.fetch(
            """
            SELECT d.id, d.name, d.threshold, count(c.id) AS case_count
            FROM golden_datasets d
            LEFT JOIN golden_cases c ON c.dataset_id = d.id
            WHERE d.prompt_id = $1
            GROUP BY d.id, d.name, d.threshold
            ORDER BY d.id
            """,
            prompt_id,
        )

    datasets = [
        GoldenDatasetSummary(
            id=r["id"], name=r["name"], threshold=r["threshold"],
            case_count=r["case_count"], is_active=(i == 0),
        )
        for i, r in enumerate(rows)
    ]
    return GoldenDatasetListResponse(datasets=datasets)


@router.get("/golden-datasets/{dataset_id}/cases", response_model=GoldenCaseListResponse)
async def list_golden_cases(
    dataset_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        dataset_row = await conn.fetchrow(
            "SELECT d.id, d.name, d.threshold, d.prompt_id, p.project_id "
            "FROM golden_datasets d JOIN prompts p ON p.id = d.prompt_id WHERE d.id = $1",
            dataset_id,
        )
        if dataset_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Golden dataset not found")
        _require_own_project(auth, dataset_row["project_id"])

        first_id = await conn.fetchval(
            "SELECT id FROM golden_datasets WHERE prompt_id = $1 ORDER BY id LIMIT 1", dataset_row["prompt_id"]
        )
        case_rows = await conn.fetch(
            "SELECT id, input_text, expected_behavior, forbidden_patterns, required_patterns, category, created_at "
            "FROM golden_cases WHERE dataset_id = $1 ORDER BY id",
            dataset_id,
        )

    dataset = GoldenDatasetSummary(
        id=dataset_row["id"], name=dataset_row["name"], threshold=dataset_row["threshold"],
        case_count=len(case_rows), is_active=(dataset_row["id"] == first_id),
    )
    return GoldenCaseListResponse(dataset=dataset, cases=[_row_to_case(r) for r in case_rows])


@router.patch("/golden-cases/{case_id}", response_model=GoldenCaseSummary)
async def update_golden_case(
    case_id: int,
    payload: GoldenCaseUpdateRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Partial update — the exact operation this session did by hand, via a
    one-off script, every time a golden case's rubric turned out to be
    wrong rather than the prompt under test. Real quality-gate scoring
    keys its Redis cache on (prompt_content, case_id) only — editing a
    case here does NOT itself invalidate that cache; the same real gotcha
    documented in llm_judge.py/pipeline.py applies. A version created
    right after an edit may still need the cache cleared to reflect it.
    """
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        case_row = await conn.fetchrow(
            "SELECT c.id, d.prompt_id, p.project_id "
            "FROM golden_cases c "
            "JOIN golden_datasets d ON d.id = c.dataset_id "
            "JOIN prompts p ON p.id = d.prompt_id "
            "WHERE c.id = $1",
            case_id,
        )
        if case_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Golden case not found")
        _require_own_project(auth, case_row["project_id"])

        fields, values = [], []
        if payload.input_text is not None:
            fields.append("input_text"); values.append(payload.input_text)
        if payload.expected_behavior is not None:
            fields.append("expected_behavior"); values.append(payload.expected_behavior)
        if payload.forbidden_patterns is not None:
            fields.append("forbidden_patterns"); values.append(json.dumps(payload.forbidden_patterns))
        if payload.required_patterns is not None:
            fields.append("required_patterns"); values.append(json.dumps(payload.required_patterns))
        if payload.category is not None:
            fields.append("category"); values.append(payload.category)

        if not fields:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "No fields to update")

        set_clause = ", ".join(f"{f} = ${i + 2}" for i, f in enumerate(fields))
        row = await conn.fetchrow(
            f"UPDATE golden_cases SET {set_clause} WHERE id = $1 "
            f"RETURNING id, input_text, expected_behavior, forbidden_patterns, required_patterns, category, created_at",
            case_id, *values,
        )

    return _row_to_case(row)


@router.delete("/golden-cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_golden_case(
    case_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """Real deletion — the same operation this session did by hand to remove accumulated duplicate rows."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        case_row = await conn.fetchrow(
            "SELECT c.id, p.project_id "
            "FROM golden_cases c "
            "JOIN golden_datasets d ON d.id = c.dataset_id "
            "JOIN prompts p ON p.id = d.prompt_id "
            "WHERE c.id = $1",
            case_id,
        )
        if case_row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Golden case not found")
        _require_own_project(auth, case_row["project_id"])

        await conn.execute("DELETE FROM golden_cases WHERE id = $1", case_id)
