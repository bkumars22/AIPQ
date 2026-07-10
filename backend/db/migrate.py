"""
Minimal migration runner — applies backend/db/migrations/V*.sql files that
haven't been applied yet, tracked in a schema_migrations table.

Nothing in this codebase ever actually ran these migrations automatically
(no Flyway, no startup hook) — they'd only ever been applied by hand
against local dev databases. That's fine for a machine someone already set
up by hand, but it means a genuinely fresh database (a new Render Postgres
instance, a new contributor's first `docker compose up`) starts with zero
tables. Called from main.py's lifespan on every startup; idempotent, so
running it against an already-migrated database is a safe no-op.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import asyncpg

logger = logging.getLogger("aipq.backend.migrate")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_VERSION_PATTERN = re.compile(r"^V(\d+)__")


def _sorted_migration_files() -> list[Path]:
    files = list(MIGRATIONS_DIR.glob("V*.sql"))

    def version_of(path: Path) -> int:
        match = _VERSION_PATTERN.match(path.name)
        if not match:
            raise ValueError(f"Migration file does not match V<n>__*.sql naming: {path.name}")
        return int(match.group(1))

    return sorted(files, key=version_of)


async def run_migrations(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        applied = {row["version"] for row in await conn.fetch("SELECT version FROM schema_migrations")}

        for path in _sorted_migration_files():
            if path.name in applied:
                continue
            sql = path.read_text(encoding="utf-8")
            logger.info("Applying migration %s", path.name)
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)", path.name
                )
