"""Unit tests for db/migrate.py — no real Postgres needed, connection is mocked."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.migrate import run_migrations  # noqa: E402


class FakeConn:
    def __init__(self, already_applied: set[str]):
        self.already_applied = already_applied
        self.executed: list[str] = []
        self.inserted_versions: list[str] = []

    async def execute(self, sql: str, *args):
        self.executed.append(sql)
        if sql.startswith("INSERT INTO schema_migrations"):
            self.inserted_versions.append(args[0])

    async def fetch(self, sql: str):
        return [{"version": v} for v in self.already_applied]

    def transaction(self):
        conn = self

        class _Txn:
            async def __aenter__(self_inner):
                return None

            async def __aexit__(self_inner, *exc):
                return False

        return _Txn()


class FakePool:
    def __init__(self, conn: FakeConn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acquire()


def _tmp_migrations(tmp_path: Path, names: list[str]) -> None:
    for name in names:
        (tmp_path / name).write_text(f"-- {name}\nSELECT 1;", encoding="utf-8")


class TestRunMigrations:
    def test_applies_all_files_on_fresh_database(self, tmp_path):
        _tmp_migrations(tmp_path, ["V1__init.sql", "V2__prompts.sql"])
        conn = FakeConn(already_applied=set())
        pool = FakePool(conn)

        with patch("db.migrate.MIGRATIONS_DIR", tmp_path):
            asyncio.run(run_migrations(pool))

        assert conn.inserted_versions == ["V1__init.sql", "V2__prompts.sql"]

    def test_skips_already_applied_migrations(self, tmp_path):
        _tmp_migrations(tmp_path, ["V1__init.sql", "V2__prompts.sql"])
        conn = FakeConn(already_applied={"V1__init.sql"})
        pool = FakePool(conn)

        with patch("db.migrate.MIGRATIONS_DIR", tmp_path):
            asyncio.run(run_migrations(pool))

        assert conn.inserted_versions == ["V2__prompts.sql"]

    def test_no_op_when_all_applied(self, tmp_path):
        _tmp_migrations(tmp_path, ["V1__init.sql"])
        conn = FakeConn(already_applied={"V1__init.sql"})
        pool = FakePool(conn)

        with patch("db.migrate.MIGRATIONS_DIR", tmp_path):
            asyncio.run(run_migrations(pool))

        assert conn.inserted_versions == []

    def test_applies_in_correct_numeric_order_not_lexicographic(self, tmp_path):
        # Lexicographic sort would put V10/V11 before V2 — this must not happen.
        _tmp_migrations(tmp_path, ["V10__ab_results.sql", "V11__drift.sql", "V2__prompts.sql", "V1__init.sql"])
        conn = FakeConn(already_applied=set())
        pool = FakePool(conn)

        with patch("db.migrate.MIGRATIONS_DIR", tmp_path):
            asyncio.run(run_migrations(pool))

        assert conn.inserted_versions == [
            "V1__init.sql", "V2__prompts.sql", "V10__ab_results.sql", "V11__drift.sql",
        ]

    def test_creates_schema_migrations_table_first(self, tmp_path):
        _tmp_migrations(tmp_path, [])
        conn = FakeConn(already_applied=set())
        pool = FakePool(conn)

        with patch("db.migrate.MIGRATIONS_DIR", tmp_path):
            asyncio.run(run_migrations(pool))

        assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in sql for sql in conn.executed)
