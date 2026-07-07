"""Rate limiting — 100 requests/minute per api_key (falls back to per-IP for unauthenticated calls)."""
from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


def _key_func(request: Request) -> str:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:]  # the raw token — one bucket per credential, not per IP
    return get_remote_address(request)


limiter = Limiter(key_func=_key_func, default_limits=["100/minute"])
