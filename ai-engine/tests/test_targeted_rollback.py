"""
Targeted rollback: restore a specific, previously passing version, refuse anything else, and record who asked and why.
The DB is faked; every statement is recorded so the tests can prove a refusal wrote nothing.

Run with:  pytest tests/test_targeted_rollback.py -v
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rollback import PASSED_STATUSES, RollbackRefused, rollback_to_version  # noqa: E402
from test_rollback_engine import FakePool, norm  # noqa: E402

# prompt 7 is on version 4 (id 40). v3 passed and was later superseded; v2 passed; v1 is the original.
VERSIONS = {
    1: {"id": 10, "version_number": 1, "status": "ROLLED_BACK", "quality_score": 0.91},
    2: {"id": 20, "version_number": 2, "status": "ROLLED_BACK", "quality_score": 0.88},
    3: {"id": 30, "version_number": 3, "status": "DEPLOYED", "quality_score": 0.93},
    4: {"id": 40, "version_number": 4, "status": "DEPLOYED", "quality_score": 0.71},
    5: {"id": 50, "version_number": 5, "status": "FAILED", "quality_score": 0.42},
    6: {"id": 60, "version_number": 6, "status": "TESTING", "quality_score": None},
    7: {"id": 70, "version_number": 7, "status": "ROLLED_BACK", "quality_score": None},  # status says passed, no score
}


class Conn:
    def __init__(self, prompt=None, versions=None, current_id=40):
        self.prompt = {"id": 7, "current_version_id": current_id} if prompt is None else prompt
        self.versions = versions or VERSIONS
        self.calls: list[tuple[str, str, tuple]] = []
        self.in_tx = False
        self.write_flags: list[bool] = []

    async def fetchrow(self, sql, *args):
        s = norm(sql)
        self.calls.append(("fetchrow", s, args))
        if s.startswith("SELECT id, current_version_id FROM prompts"):
            return self.prompt
        if s.startswith("SELECT id, version_number FROM prompt_versions WHERE id"):
            return next(v for v in self.versions.values() if v["id"] == args[0])
        if s.startswith("SELECT id, version_number, status, quality_score FROM prompt_versions"):
            return self.versions.get(args[1])
        if s.startswith("INSERT INTO rollbacks"):
            self.write_flags.append(self.in_tx)
            return {"id": 501}
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def execute(self, sql, *args):
        self.calls.append(("execute", norm(sql), args))
        self.write_flags.append(self.in_tx)

    def transaction(self):
        conn = self

        class _Tx:
            async def __aenter__(self):
                conn.in_tx = True

            async def __aexit__(self, *exc):
                conn.in_tx = False
                return False

        return _Tx()

    @property
    def writes(self):
        return [c for c in self.calls if c[0] == "execute" or c[1].startswith("INSERT")]


async def rollback(conn, to_version=3, **kw):
    async def fake_get_pool():
        return FakePool(conn)

    kw.setdefault("reason", "AI Quality Control Plane incident inc-42: quality dropped")
    kw.setdefault("requested_by", "qcp:api_key:project 10")
    with patch("rollback.get_pool", fake_get_pool):
        return await rollback_to_version(7, to_version, **kw)


# --------------------------------------------------------------------------------------- success


@pytest.mark.asyncio
async def test_restores_a_previously_passing_version():
    conn = Conn()
    result = await rollback(conn, 3, from_version_number=4)
    assert result["from_version_number"] == 4 and result["to_version_number"] == 3
    assert (result["from_version_id"], result["to_version_id"], result["rollback_id"]) == (40, 30, 501)
    assert result["to_quality_score"] == 0.93 and result["requested_by"] == "qcp:api_key:project 10"
    assert [(w[1], w[2]) for w in conn.writes][:3] == [
        ("UPDATE prompt_versions SET status = 'ROLLED_BACK' WHERE id = $1", (40,)),
        ("UPDATE prompt_versions SET status = 'DEPLOYED', deployed_at = now() WHERE id = $1", (30,)),
        ("UPDATE prompts SET current_version_id = $1 WHERE id = $2", (30, 7)),
    ]
    assert conn.write_flags == [True] * 4  # all four writes inside one transaction


@pytest.mark.asyncio
async def test_a_version_that_was_deployed_and_later_rolled_back_may_be_restored():
    result = await rollback(Conn(), 2)  # v2 is ROLLED_BACK: it was deployed, so it passed the gate once
    assert result["to_version_number"] == 2


@pytest.mark.asyncio
async def test_from_version_is_optional():
    assert (await rollback(Conn(), 3))["from_version_number"] == 4


@pytest.mark.asyncio
async def test_the_prompt_row_is_locked_so_nothing_changes_it_underneath():
    conn = Conn()
    await rollback(conn, 3)
    assert conn.calls[0][1] == "SELECT id, current_version_id FROM prompts WHERE id = $1 FOR UPDATE"


# ------------------------------------------------------------------------------- the audit entry


@pytest.mark.asyncio
async def test_the_audit_row_records_who_asked_and_why_as_a_manual_rollback():
    conn = Conn()
    await rollback(conn, 3, reason="  quality dropped after v4  ", requested_by="  alice via QCP  ")
    insert = next(c for c in conn.calls if c[1].startswith("INSERT INTO rollbacks"))
    assert "'MANUAL'" in insert[1] and "'AUTOMATIC'" not in insert[1] and "requested_by" in insert[1]
    assert insert[2] == (7, 40, 30, "quality dropped after v4", "alice via QCP")  # trimmed


# --------------------------------------------------------------------------------------- refusals


REFUSALS = [
    (dict(to_version=5), "not_passed", "failed the quality gate and was never deployed"),
    (dict(to_version=6), "not_passed", "has not finished evaluation"),
    (dict(to_version=7), "not_passed", "no recorded quality score"),
    (dict(to_version=99), "unknown_version", "version 99 does not exist for this prompt"),
    (dict(to_version=4), "already_current", "already the deployed version"),
    (dict(to_version=3, from_version_number=2), "not_current", "version 2 is not the deployed version (version 4 is)"),
    (dict(to_version=3, reason="  "), "invalid", "who requested it and why"),
    (dict(to_version=3, requested_by=""), "invalid", "who requested it and why"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,kind,message", REFUSALS)
async def test_refuses_with_a_clear_reason_and_writes_nothing(kwargs, kind, message):
    conn = Conn()
    with pytest.raises(RollbackRefused) as exc:
        await rollback(conn, **kwargs)
    assert exc.value.kind == kind and message in str(exc.value)
    assert conn.writes == []


@pytest.mark.asyncio
async def test_an_unknown_prompt_is_refused():
    conn = Conn(prompt=False)  # falsy sentinel replaced below
    conn.prompt = None
    with pytest.raises(RollbackRefused) as exc:
        await rollback(conn, 3)
    assert exc.value.kind == "not_found" and conn.writes == []


@pytest.mark.asyncio
async def test_a_prompt_with_no_deployed_version_is_refused():
    conn = Conn(current_id=None)
    conn.prompt = {"id": 7, "current_version_id": None}
    with pytest.raises(RollbackRefused) as exc:
        await rollback(conn, 3)
    assert exc.value.kind == "no_current" and conn.writes == []


def test_only_versions_that_passed_the_gate_count_as_passed():
    assert PASSED_STATUSES == ("DEPLOYED", "ROLLED_BACK")  # never FAILED or TESTING
