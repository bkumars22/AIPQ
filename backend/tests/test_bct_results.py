"""
Unit tests for routers/prompts.py's BCT-result endpoints (POST/GET
/prompts/{id}/bct-result[s]) — no real Postgres needed, connection is
mocked, matching tests/test_migrate.py's existing FakeConn/FakePool style.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.dependencies import AuthContext  # noqa: E402
from models.schemas import BCTResultRequest  # noqa: E402
from routers.prompts import create_bct_result, list_bct_results  # noqa: E402


class FakeConn:
    def __init__(self, prompt_project_id=None, insert_row=None, list_rows=None):
        self.prompt_project_id = prompt_project_id
        self.insert_row = insert_row
        self.list_rows = list_rows or []

    async def fetchrow(self, sql, *args):
        stripped = sql.strip()
        if stripped.startswith("SELECT project_id FROM prompts"):
            return None if self.prompt_project_id is None else {"project_id": self.prompt_project_id}
        if stripped.startswith("INSERT INTO bct_results"):
            return self.insert_row
        raise AssertionError(f"Unexpected fetchrow call: {sql!r}")

    async def fetch(self, sql, *args):
        return self.list_rows


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


def _fake_request(pool: FakePool):
    request = MagicMock()
    request.app.state.pg_pool = pool
    return request


def _row(**overrides):
    base = {
        "id": 1, "prompt_id": 4, "source_system": "qaip", "contract_name": "qaip_defect_explanation",
        "overall_compliance": 0.73, "breaking_point": 3, "result": "FAILED", "role_tested": None,
        "created_at": "2026-07-14T00:00:00Z",
    }
    base.update(overrides)
    return base


class TestCreateBctResult:
    @pytest.mark.asyncio
    async def test_creates_result_for_own_project(self):
        conn = FakeConn(prompt_project_id=10, insert_row=_row())
        payload = BCTResultRequest(
            source_system="qaip", contract_name="qaip_defect_explanation",
            overall_compliance=0.73, breaking_point=3, result="FAILED",
        )
        auth = AuthContext(project_id=10, via="api_key")

        result = await create_bct_result(prompt_id=4, payload=payload, request=_fake_request(FakePool(conn)), auth=auth)

        assert result.id == 1
        assert result.source_system == "qaip"
        assert result.overall_compliance == 0.73

    @pytest.mark.asyncio
    async def test_404_when_prompt_not_found(self):
        conn = FakeConn(prompt_project_id=None)
        payload = BCTResultRequest(source_system="qaip", contract_name="x", overall_compliance=0.5, result="FAILED")
        auth = AuthContext(project_id=10, via="api_key")

        with pytest.raises(HTTPException) as exc_info:
            await create_bct_result(prompt_id=999, payload=payload, request=_fake_request(FakePool(conn)), auth=auth)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_403_when_api_key_belongs_to_different_project(self):
        conn = FakeConn(prompt_project_id=10)
        payload = BCTResultRequest(source_system="qaip", contract_name="x", overall_compliance=0.5, result="FAILED")
        auth = AuthContext(project_id=999, via="api_key")  # different project than the prompt's owner

        with pytest.raises(HTTPException) as exc_info:
            await create_bct_result(prompt_id=4, payload=payload, request=_fake_request(FakePool(conn)), auth=auth)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_jwt_auth_bypasses_ownership_check(self):
        # Matches _require_own_project's documented behavior: a JWT is an
        # admin dashboard session with cross-project visibility.
        conn = FakeConn(prompt_project_id=10, insert_row=_row(source_system="zentravix", role_tested="team_member"))
        payload = BCTResultRequest(
            source_system="zentravix", contract_name="x", overall_compliance=0.9,
            result="PASSED", role_tested="team_member",
        )
        auth = AuthContext(project_id=999, via="jwt")  # different project id, but JWT

        result = await create_bct_result(prompt_id=4, payload=payload, request=_fake_request(FakePool(conn)), auth=auth)
        assert result.role_tested == "team_member"

    @pytest.mark.asyncio
    async def test_role_tested_defaults_to_none_for_qaip(self):
        conn = FakeConn(prompt_project_id=10, insert_row=_row())
        payload = BCTResultRequest(source_system="qaip", contract_name="x", overall_compliance=0.5, result="FAILED")
        auth = AuthContext(project_id=10, via="api_key")

        result = await create_bct_result(prompt_id=4, payload=payload, request=_fake_request(FakePool(conn)), auth=auth)
        assert result.role_tested is None


class TestListBctResults:
    @pytest.mark.asyncio
    async def test_lists_results_for_own_project(self):
        rows = [
            _row(id=2, source_system="zentravix", role_tested="team_member", created_at="2026-07-14T00:00:00Z"),
            _row(id=1, source_system="qaip", created_at="2026-07-13T00:00:00Z"),
        ]
        conn = FakeConn(prompt_project_id=10, list_rows=rows)
        auth = AuthContext(project_id=10, via="api_key")

        result = await list_bct_results(prompt_id=4, request=_fake_request(FakePool(conn)), auth=auth)

        assert len(result.results) == 2
        assert result.results[0].source_system == "zentravix"
        assert result.results[1].source_system == "qaip"

    @pytest.mark.asyncio
    async def test_404_when_prompt_not_found(self):
        conn = FakeConn(prompt_project_id=None)
        auth = AuthContext(project_id=10, via="api_key")

        with pytest.raises(HTTPException) as exc_info:
            await list_bct_results(prompt_id=999, request=_fake_request(FakePool(conn)), auth=auth)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_403_when_api_key_belongs_to_different_project(self):
        conn = FakeConn(prompt_project_id=10)
        auth = AuthContext(project_id=999, via="api_key")

        with pytest.raises(HTTPException) as exc_info:
            await list_bct_results(prompt_id=4, request=_fake_request(FakePool(conn)), auth=auth)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_list_when_no_results_yet(self):
        conn = FakeConn(prompt_project_id=10, list_rows=[])
        auth = AuthContext(project_id=10, via="api_key")

        result = await list_bct_results(prompt_id=4, request=_fake_request(FakePool(conn)), auth=auth)
        assert result.results == []
