"""
Rollback history (GET /prompts/{id}/rollbacks) and the dashboard's rollback action (POST /prompts/{id}/rollback).
No real Postgres or ai-engine: the pool is faked and the ai-engine is an httpx MockTransport.
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.dependencies import AuthContext, get_auth_context  # noqa: E402

T = lambda day: datetime(2026, 10, day, 12, 0, tzinfo=timezone.utc)  # noqa: E731

VERSIONS = [  # newest first, as the query returns them. v4 (id 40) is deployed.
    {"id": 70, "version_number": 7, "status": "ROLLED_BACK", "quality_score": None, "deployed_at": None,
     "changed_by": "dev", "change_message": "odd row"},
    {"id": 60, "version_number": 6, "status": "TESTING", "quality_score": None, "deployed_at": None,
     "changed_by": "dev", "change_message": "wip"},
    {"id": 50, "version_number": 5, "status": "FAILED", "quality_score": 0.42, "deployed_at": None,
     "changed_by": "dev", "change_message": "too aggressive"},
    {"id": 40, "version_number": 4, "status": "DEPLOYED", "quality_score": 0.71, "deployed_at": T(4),
     "changed_by": "dev", "change_message": "sped up responses"},
    {"id": 30, "version_number": 3, "status": "ROLLED_BACK", "quality_score": 0.93, "deployed_at": T(3),
     "changed_by": "dev", "change_message": "tuned tone"},
    {"id": 10, "version_number": 1, "status": "ROLLED_BACK", "quality_score": 0.91, "deployed_at": T(1),
     "changed_by": "dev", "change_message": "initial"},
]
ROLLBACKS = [
    {"id": 2, "triggered_at": T(5), "resolved_at": T(5), "triggered_by": "MANUAL", "reason": "v4 sped up and broke",
     "requested_by": "alice via Dashboard (jwt, project 10)", "from_version_number": 4, "to_version_number": 3},
    {"id": 1, "triggered_at": T(2), "resolved_at": T(2), "triggered_by": "AUTOMATIC",
     "reason": "Critical drift (anomaly_score=0.97): length collapsed", "requested_by": None,
     "from_version_number": 2, "to_version_number": 1},
]


class Conn:
    def __init__(self, project_id=10, prompt=True):
        self.prompt = {"project_id": project_id, "prompt_name": "aria-tutor", "current_version_id": 40} if prompt else None

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT project_id"):
            return self.prompt
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def fetch(self, sql, *args):
        s = " ".join(sql.split())
        if "FROM prompt_versions WHERE prompt_id" in s:
            return VERSIONS
        if "FROM rollbacks r" in s:
            return ROLLBACKS
        raise AssertionError(f"unexpected fetch: {s}")


class Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _A:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *e):
                return False

        return _A()


def build(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("QCP_ENABLED", "true")
    else:
        monkeypatch.delenv("QCP_ENABLED", raising=False)
    import main
    return importlib.reload(main).app


def client(app, conn=None, auth=AuthContext(project_id=10, via="jwt")):
    app.state.pg_pool = Pool(conn or Conn())
    if auth is not None:
        app.dependency_overrides[get_auth_context] = lambda: auth
    return TestClient(app)


def engine(monkeypatch, status=200, body=None, seen=None):
    def handler(req):
        if seen is not None:
            seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(status, json=body if body is not None else {
            "rollback_id": 9, "from_version_number": 4, "to_version_number": 3, "requested_by": "x"})

    real = httpx.AsyncClient
    monkeypatch.setattr("routers.prompts.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def on(monkeypatch):
    app = build(monkeypatch, True)
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
def off(monkeypatch):
    app = build(monkeypatch, False)
    yield app
    app.dependency_overrides.clear()


class TestHistory:
    def test_versions_rollbacks_and_what_may_be_restored(self, on):
        body = client(on).get("/prompts/7/rollbacks").json()
        assert body["prompt_name"] == "aria-tutor" and body["current_version_number"] == 4
        by = {v["version_number"]: v for v in body["versions"]}
        assert [v["version_number"] for v in body["versions"]] == [7, 6, 5, 4, 3, 1]  # newest first
        assert by[4]["is_current"] and not by[4]["can_roll_back_to"] and "deployed version" in by[4]["why_not"]
        assert by[3]["can_roll_back_to"] and by[1]["can_roll_back_to"] and by[3]["why_not"] is None
        assert not by[5]["can_roll_back_to"] and "never deployed" in by[5]["why_not"]
        assert not by[6]["can_roll_back_to"] and "Still being evaluated" in by[6]["why_not"]
        assert not by[7]["can_roll_back_to"] and "No passing quality score" in by[7]["why_not"]

    def test_automatic_and_manual_rollbacks_both_appear_with_who_and_why(self, on):
        rb = client(on).get("/prompts/7/rollbacks").json()["rollbacks"]
        manual, auto = rb
        assert (manual["triggered_by"], manual["requested_by"], manual["reason"]) == (
            "MANUAL", "alice via Dashboard (jwt, project 10)", "v4 sped up and broke")
        assert (auto["triggered_by"], auto["requested_by"]) == ("AUTOMATIC", None)  # the automatic one is unchanged
        assert (manual["from_version_number"], manual["to_version_number"]) == (4, 3)

    def test_it_reports_that_the_action_is_enabled(self, on):
        assert client(on).get("/prompts/7/rollbacks").json()["targeted_rollback_enabled"] is True

    def test_it_reports_that_the_action_is_disabled(self, off):
        assert client(off).get("/prompts/7/rollbacks").json()["targeted_rollback_enabled"] is False

    def test_history_is_readable_with_the_flag_off(self, off):
        r = client(off).get("/prompts/7/rollbacks")
        assert r.status_code == 200 and len(r.json()["versions"]) == 6

    def test_unknown_prompt_is_404(self, on):
        assert client(on, Conn(prompt=False)).get("/prompts/7/rollbacks").status_code == 404

    def test_another_projects_prompt_is_403(self, on):
        assert client(on, Conn(project_id=99), AuthContext(10, "api_key")).get("/prompts/7/rollbacks").status_code == 403

    def test_credentials_are_required(self, on):
        on.dependency_overrides.clear()
        assert client(on, auth=None).get("/prompts/7/rollbacks").status_code == 401


class TestDashboardAction:
    BODY = {"to_version_number": 3, "reason": "v4 sped up and broke"}

    def test_it_is_refused_when_the_flag_is_off_and_never_reaches_the_ai_engine(self, off, monkeypatch):
        seen = []
        engine(monkeypatch, seen=seen)
        r = client(off).post("/prompts/7/rollback", json=self.BODY)
        assert r.status_code == 403 and "QCP_ENABLED" in r.json()["detail"] and seen == []

    def test_it_restores_the_version_and_records_who_and_why(self, on, monkeypatch):
        seen = []
        engine(monkeypatch, seen=seen)
        r = client(on).post("/prompts/7/rollback", json={**self.BODY, "requested_by": "alice"})
        assert r.status_code == 200 and r.json()["to_version_number"] == 3
        path, body = seen[0]
        assert path == "/rollback" and body["prompt_id"] == 7 and body["to_version_number"] == 3
        assert body["reason"] == "v4 sped up and broke"
        assert body["requested_by"] == "alice via Dashboard (jwt, project 10)"

    def test_without_a_name_the_authenticated_caller_is_still_recorded(self, on, monkeypatch):
        seen = []
        engine(monkeypatch, seen=seen)
        client(on).post("/prompts/7/rollback", json=self.BODY)
        assert seen[0][1]["requested_by"] == "Dashboard (jwt, project 10)"

    def test_a_reason_is_required(self, on, monkeypatch):
        seen = []
        engine(monkeypatch, seen=seen)
        for body in ({"to_version_number": 3}, {"to_version_number": 3, "reason": "x"},
                     {"to_version_number": 3, "reason": ""}, {"to_version_number": 0, "reason": "valid reason"}):
            assert client(on).post("/prompts/7/rollback", json=body).status_code == 422
        assert seen == []

    @pytest.mark.parametrize("code,detail", [(422, "version 5 failed the quality gate and was never deployed"),
                                             (404, "version 99 does not exist for this prompt"),
                                             (409, "version 3 is already the deployed version")])
    def test_refusals_come_through_with_their_reason(self, on, monkeypatch, code, detail):
        engine(monkeypatch, status=code, body={"detail": detail})
        r = client(on).post("/prompts/7/rollback", json=self.BODY)
        assert r.status_code == code and r.json()["detail"] == detail

    def test_an_unreachable_ai_engine_is_502(self, on, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("down")

        real = httpx.AsyncClient
        monkeypatch.setattr("routers.prompts.httpx.AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
        r = client(on).post("/prompts/7/rollback", json=self.BODY)
        assert r.status_code == 502 and "no rollback was performed" in r.json()["detail"]

    def test_another_projects_prompt_is_refused(self, on, monkeypatch):
        seen = []
        engine(monkeypatch, seen=seen)
        r = client(on, Conn(project_id=99), AuthContext(10, "api_key")).post("/prompts/7/rollback", json=self.BODY)
        assert r.status_code == 403 and seen == []


def test_the_migration_adds_a_nullable_requester_column():
    sql = (Path(__file__).parent.parent / "db" / "migrations" / "V15__rollbacks_requested_by.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE rollbacks ADD COLUMN requested_by VARCHAR(255);" in sql
    assert "NOT NULL" not in sql  # existing rows and the automatic rollback must keep working
