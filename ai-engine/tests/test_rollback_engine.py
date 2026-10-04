"""
Characterisation tests for the AUTOMATIC rollback (RollbackEngine.rollback_if_critical).

They pin down exactly what it does today -- which versions it considers, which one it picks, the SQL it runs and in
what order -- so that adding the targeted rollback (a caller-chosen version) cannot change it. The DB is faked and
records every statement.

Run with:  pytest tests/test_rollback_engine.py -v
"""
import os
import re
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from detectors.drift_detector import DriftResult, RollbackEngine  # noqa: E402


def norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


class FakeConn:
    """Records every statement in order; answers fetch/fetchrow from scripted data."""

    def __init__(self, prompt_row, candidates, rollback_id=77):
        self.prompt_row, self.candidates, self.rollback_id = prompt_row, candidates, rollback_id
        self.calls: list[tuple[str, str, tuple]] = []
        self.in_transaction = False
        self.writes_in_transaction: list[bool] = []

    async def fetchrow(self, sql, *args):
        s = norm(sql)
        self.calls.append(("fetchrow", s, args))
        if s.startswith("SELECT current_version_id FROM prompts"):
            return self.prompt_row
        if s.startswith("INSERT INTO rollbacks"):
            self.writes_in_transaction.append(self.in_transaction)
            return {"id": self.rollback_id}
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetch(self, sql, *args):
        self.calls.append(("fetch", norm(sql), args))
        return self.candidates

    async def execute(self, sql, *args):
        self.calls.append(("execute", norm(sql), args))
        self.writes_in_transaction.append(self.in_transaction)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.in_transaction = True

            async def __aexit__(self, *exc):
                conn.in_transaction = False
                return False

        return _Tx()


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Acquire:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Acquire()


def critical(score=0.97, explanation="response length collapsed"):
    return DriftResult(is_anomaly=True, anomaly_score=score, drift_severity="CRITICAL", explanation=explanation)


async def run_engine(conn, drift):
    async def fake_get_pool():
        return FakePool(conn)

    with patch("detectors.drift_detector.get_pool", fake_get_pool):
        return await RollbackEngine.rollback_if_critical(7, drift)


CANDIDATES = [  # as the query returns them: newest first
    {"id": 40, "version_number": 4, "quality_score": 0.71},
    {"id": 30, "version_number": 3, "quality_score": 0.93},
    {"id": 20, "version_number": 2, "quality_score": 0.88},
    {"id": 10, "version_number": 1, "quality_score": 0.93},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("severity", ["NONE", "LOW", "HIGH"])
async def test_only_critical_drift_triggers_a_rollback_and_nothing_is_read(severity):
    conn = FakeConn({"current_version_id": 40}, CANDIDATES)
    result = await run_engine(conn, DriftResult(True, 0.9, severity, "x"))
    assert result is None and conn.calls == []


@pytest.mark.asyncio
async def test_no_current_version_means_no_rollback():
    for row in (None, {"current_version_id": None}):
        conn = FakeConn(row, CANDIDATES)
        assert await run_engine(conn, critical()) is None
        assert [c[0] for c in conn.calls] == ["fetchrow"]


@pytest.mark.asyncio
async def test_no_scored_candidates_means_no_rollback():
    conn = FakeConn({"current_version_id": 40}, [])
    assert await run_engine(conn, critical()) is None
    assert not [c for c in conn.calls if c[0] == "execute"]


@pytest.mark.asyncio
async def test_already_on_the_best_version_means_no_rollback_and_no_writes():
    conn = FakeConn({"current_version_id": 30}, CANDIDATES)  # v3 (0.93) is the best and is current
    assert await run_engine(conn, critical()) is None
    assert not [c for c in conn.calls if c[0] in ("execute",) or c[1].startswith("INSERT")]


@pytest.mark.asyncio
async def test_it_considers_the_last_five_deployed_or_rolled_back_scored_versions():
    conn = FakeConn({"current_version_id": 40}, CANDIDATES)
    await run_engine(conn, critical())
    (select,) = [c for c in conn.calls if c[0] == "fetch"]
    assert select[1] == norm("""
        SELECT id, version_number, quality_score FROM prompt_versions
        WHERE prompt_id = $1 AND status IN ('DEPLOYED', 'ROLLED_BACK') AND quality_score IS NOT NULL
        ORDER BY version_number DESC LIMIT 5""")
    assert select[2] == (7,)


@pytest.mark.asyncio
async def test_it_picks_the_best_scoring_candidate_and_the_first_on_a_tie():
    conn = FakeConn({"current_version_id": 40}, CANDIDATES)  # v3 and v1 both 0.93: the first listed (v3) wins
    result = await run_engine(conn, critical())
    assert result == {"rollback_id": 77, "from_version_id": 40, "to_version_id": 30}


@pytest.mark.asyncio
async def test_the_exact_writes_in_order_all_inside_one_transaction():
    conn = FakeConn({"current_version_id": 40}, CANDIDATES)
    await run_engine(conn, critical(0.97, "response length collapsed"))
    writes = [c for c in conn.calls if c[0] == "execute" or c[1].startswith("INSERT")]
    assert [(w[1], w[2]) for w in writes] == [
        ("UPDATE prompt_versions SET status = 'ROLLED_BACK' WHERE id = $1", (40,)),
        ("UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = $1", (30,)),
        ("UPDATE prompts SET current_version_id = $1 WHERE id = $2", (30, 7)),
        ("INSERT INTO rollbacks (prompt_id, from_version_id, to_version_id, triggered_by, reason, resolved_at) "
         "VALUES ($1, $2, $3, 'AUTOMATIC', $4, now()) RETURNING id",
         (7, 40, 30, "Critical drift (anomaly_score=0.97): response length collapsed")),
    ]
    assert conn.writes_in_transaction == [True, True, True, True]


@pytest.mark.asyncio
async def test_the_automatic_rollback_never_records_a_requester():
    conn = FakeConn({"current_version_id": 40}, CANDIDATES)
    await run_engine(conn, critical())
    insert = next(c for c in conn.calls if c[1].startswith("INSERT INTO rollbacks"))
    assert "requested_by" not in insert[1] and "MANUAL" not in insert[1]
