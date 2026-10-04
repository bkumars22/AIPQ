"""Shared config helpers used by more than one router."""
from __future__ import annotations

import os


_TRUTHY = {"1", "true", "yes", "on"}


def qcp_enabled() -> bool:
    """QCP_ENABLED switches on the /qcp routes and the targeted rollback. Off by default: nothing changes unless set."""
    return os.getenv("QCP_ENABLED", "").strip().lower() in _TRUTHY


def ai_engine_url() -> str:
    """
    Base URL for calling ai-engine.

    Render's blueprint env vars can only pass a bare "host:port" (its
    fromService.hostport property — Render doesn't support string
    interpolation to prepend a scheme in render.yaml), while local Docker
    Compose already sets this to a full "http://ai-engine:8002" URL. Accept
    both so the same code works unmodified in either environment.
    """
    url = os.getenv("AI_ENGINE_URL", "http://localhost:8002")
    return url if "://" in url else f"http://{url}"
