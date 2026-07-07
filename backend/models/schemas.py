"""Pydantic request/response models for the projects and prompts routers."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, Field


# ── Projects ──────────────────────────────────────────────────────────────

class ProjectRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    owner_email: EmailStr
    pipeline_type: str = Field(default="CUSTOM", pattern="^(LANGGRAPH|LANGCHAIN|CUSTOM)$")


class ProjectRegisterResponse(BaseModel):
    project_id: int
    api_key: str  # shown once — caller must store it, it is never retrievable again


class ProjectResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    owner_email: str
    pipeline_type: str
    is_active: bool
    created_at: datetime


# ── Prompts ───────────────────────────────────────────────────────────────

class PromptRegisterRequest(BaseModel):
    project_id: int
    prompt_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    golden_dataset: Optional[str] = None
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class PromptRegisterResponse(BaseModel):
    prompt_id: int


class PromptVersionCreateRequest(BaseModel):
    prompt_id: int
    content: str = Field(..., min_length=1)
    dataset: Optional[str] = None
    threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    system_prompt: Optional[str] = None
    temperature: float = 0.3
    max_tokens: int = 4096
    changed_by: str = "sdk"
    change_message: Optional[str] = None


class PromptVersionCreateResponse(BaseModel):
    version_id: int
    version_number: int
    status: str  # TESTING while evaluation runs in the background


class PromptVersionSummary(BaseModel):
    id: int
    version_number: int
    quality_score: Optional[float]
    status: str
    changed_by: str
    change_message: Optional[str]
    created_at: datetime
    deployed_at: Optional[datetime]


class PromptVersionListResponse(BaseModel):
    versions: list[PromptVersionSummary]


class CurrentVersionResponse(BaseModel):
    id: int
    version_number: int
    content: str
    quality_score: Optional[float]
    status: str


# ── Drift ─────────────────────────────────────────────────────────────────

class DriftRecordRequest(BaseModel):
    prompt_version_id: int
    output: str
    context: str = ""
    quality_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)


class DriftStatusResponse(BaseModel):
    prompt_id: int
    prompt_name: str
    current_version_id: Optional[int]
    current_version_number: Optional[int]
    deployed_at: Optional[datetime]
    quality_score: Optional[float]
    recent_drift_severity: Optional[str]
    changed_recently: bool  # deployed within the last 7 days
    root_cause_hint: str


# ── Golden cases ──────────────────────────────────────────────────────────

class GoldenCaseCreateRequest(BaseModel):
    prompt_id: int
    input_text: str
    expected_behavior: str
    forbidden_patterns: list[str] = []
    required_patterns: list[str] = []
    category: str = "baseline"


class GoldenCaseCreateResponse(BaseModel):
    case_id: int
