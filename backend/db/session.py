"""Postgres (asyncpg) pool + Redis connection, held on app.state."""
from __future__ import annotations

import os

import asyncpg
import redis.asyncio as aioredis


async def create_pg_pool() -> asyncpg.Pool:
    dsn = os.getenv("AIPQ_DATABASE_URL", "postgresql://aipq:aipq_local_2026@localhost:5433/aipq")
    return await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=10)


def create_redis_client() -> aioredis.Redis:
    url = os.getenv("REDIS_URL", "redis://localhost:6380/0")
    return aioredis.from_url(url, decode_responses=True)
