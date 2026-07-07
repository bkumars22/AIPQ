"""
Authentication dependency shared by every route.

Two kinds of caller hit this backend:
  - The React dashboard, authenticated with a JWT (human login session).
  - The SDK / GitHub Action, authenticated with a project's raw api_key
    (issued once at /projects/register — see auth/api_key.py).

Both present the same `Authorization: Bearer <token>` header, so a single
dependency tries JWT decode first and falls back to an api_key DB lookup.
This keeps every route's signature identical regardless of caller type.
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .api_key import hash_api_key
from .jwt import JWTError, decode_access_token

_bearer = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    project_id: int
    via: str  # "jwt" | "api_key"


async def get_auth_context(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Authorization header")

    token = credentials.credentials

    try:
        claims = decode_access_token(token)
        return AuthContext(project_id=int(claims["project_id"]), via="jwt")
    except (JWTError, KeyError, ValueError):
        pass  # not a valid JWT — try api_key next

    pool = request.app.state.pg_pool
    key_hash = hash_api_key(token)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id FROM projects WHERE webhook_secret = $1 AND is_active = TRUE", key_hash
        )
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired credentials")

    return AuthContext(project_id=row["id"], via="api_key")
