"""POST /projects/register, GET /projects/{id}."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from auth.api_key import generate_api_key, hash_api_key
from auth.dependencies import AuthContext, get_auth_context
from models.schemas import (
    ProjectListResponse,
    ProjectRegisterRequest,
    ProjectRegisterResponse,
    ProjectResponse,
    ProjectSummary,
    PromptListResponse,
    PromptSummary,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request, auth: AuthContext = Depends(get_auth_context)):
    """Dashboard overview — every registered project with a quick health summary."""
    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.id, p.name, p.pipeline_type, p.created_at,
                   COUNT(pr.id) AS prompt_count,
                   AVG(pv.quality_score) AS avg_quality_score
            FROM projects p
            LEFT JOIN prompts pr ON pr.project_id = p.id
            LEFT JOIN prompt_versions pv ON pv.id = pr.current_version_id
            GROUP BY p.id, p.name, p.pipeline_type, p.created_at
            ORDER BY p.id
            """
        )
    return ProjectListResponse(projects=[ProjectSummary(**dict(r)) for r in rows])


@router.post("/register", response_model=ProjectRegisterResponse, status_code=status.HTTP_201_CREATED)
async def register_project(
    payload: ProjectRegisterRequest,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    """
    Registers a new AI project with AIPQ and issues its api_key.

    Requires a dashboard JWT (an admin session) — a project's own api_key
    cannot be used to spawn further projects.
    """
    if auth.via != "jwt":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only a dashboard session can register new projects")

    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO projects (name, description, owner_email, webhook_secret, pipeline_type)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id
                """,
                payload.name, payload.description, payload.owner_email, key_hash, payload.pipeline_type,
            )
        except Exception as exc:
            if "duplicate key" in str(exc).lower():
                raise HTTPException(status.HTTP_409_CONFLICT, f"Project '{payload.name}' already exists")
            raise

    return ProjectRegisterResponse(project_id=row["id"], api_key=raw_key)


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    if auth.via == "api_key" and auth.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "An api_key may only read its own project")

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM projects WHERE id = $1", project_id)

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Project not found")

    return ProjectResponse(**dict(row))


@router.get("/{project_id}/prompts", response_model=PromptListResponse)
async def list_project_prompts(
    project_id: int,
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
):
    if auth.via == "api_key" and auth.project_id != project_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "An api_key may only read its own project")

    pool = request.app.state.pg_pool
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT pr.id, pr.prompt_name, pr.description,
                   pv.version_number AS current_version_number,
                   pv.quality_score, pv.status, pv.deployed_at
            FROM prompts pr
            LEFT JOIN prompt_versions pv ON pv.id = pr.current_version_id
            WHERE pr.project_id = $1
            ORDER BY pr.id
            """,
            project_id,
        )
    return PromptListResponse(prompts=[PromptSummary(**dict(r)) for r in rows])
