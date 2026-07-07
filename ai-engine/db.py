"""Postgres (asyncpg) pool + Redis connection for the ai-engine service."""
from __future__ import annotations

import os

import asyncpg
import redis.asyncio as aioredis

_pool: asyncpg.Pool | None = None
_redis: aioredis.Redis | None = None
_redis_binary: aioredis.Redis | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.getenv("AIPQ_DATABASE_URL", "postgresql://aipq:aipq_local_2026@localhost:5433/aipq")
        _pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)
    return _pool


def get_redis() -> aioredis.Redis:
    """Text-mode client (decode_responses=True) — for JSON-serialized cache values."""
    global _redis
    if _redis is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6380/0")
        _redis = aioredis.from_url(url, decode_responses=True)
    return _redis


def get_redis_binary() -> aioredis.Redis:
    """Binary-mode client (decode_responses=False) — for pickled model blobs etc."""
    global _redis_binary
    if _redis_binary is None:
        url = os.getenv("REDIS_URL", "redis://localhost:6380/0")
        _redis_binary = aioredis.from_url(url, decode_responses=False)
    return _redis_binary


async def close_all() -> None:
    global _pool, _redis, _redis_binary
    if _redis is not None:
        await _redis.aclose()
        _redis = None
    if _redis_binary is not None:
        await _redis_binary.aclose()
        _redis_binary = None
    if _pool is not None:
        await _pool.close()
        _pool = None
