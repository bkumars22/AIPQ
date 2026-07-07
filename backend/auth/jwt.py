"""JWT encode/decode for the human-facing dashboard session."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt

ALGORITHM = "HS256"
DEFAULT_EXPIRY_HOURS = 12


def _secret() -> str:
    secret = os.getenv("JWT_SECRET", "")
    if not secret:
        raise RuntimeError("JWT_SECRET is not set")
    return secret


def create_access_token(subject: str, extra_claims: dict[str, Any] | None = None) -> str:
    payload = {
        "sub": subject,
        "exp": datetime.now(timezone.utc) + timedelta(hours=DEFAULT_EXPIRY_HOURS),
        **(extra_claims or {}),
    }
    return jwt.encode(payload, _secret(), algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jose.JWTError on an invalid/expired token."""
    return jwt.decode(token, _secret(), algorithms=[ALGORITHM])


__all__ = ["create_access_token", "decode_access_token", "JWTError"]
