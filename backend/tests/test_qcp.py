"""
Tests for routers/qcp.py (the /qcp/* contract for the AI Quality Control Plane).
No real Postgres or ai-engine: the connection pool is faked in the same style as
tests/test_bct_results.py, and ai-engine is replaced by an httpx MockTransport.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from auth.dependencies import AuthContext, get_auth_context  # noqa: E402

CURRENT = {"id": 31, "version_number": 3, "content": "...", "quality_score": 0.78, "status": "DEPLOYED"}
IMPACT = {
    "prompt_id": 7, "pre_period_mean": 0.91, "post_period_mean": 0.78, "counterfactual_mean": 0.9,
    "estimated_effect": -0.12, "relative_effect_pct": -13.3, "p_value": 0.004, "is_significant": True,
    "sample_size_pre": 40, "sample_size_post": 25,
    "interpretation": "Deploying this version is associated with a significant drop in quality.",
    "caveat": "Observational estimate, not proof of causation.",
}
BODY = {"incident_id": "inc-1", "system": "ARIA", "prompt_id": "aria-tutor", "version": "v3",
        "last_good_version": "v2", "score": 0.78, "threshold": 0.9}


class FakeConn:
    def __init__(self, by_name=None, project_id=10, current=CURRENT):
        self.by_name, self.project_id, self.current = by_name or {"aria-tutor": 7}, project_id, current

    async def fetchrow(self, sql, *args):
        s = sql.strip()
        if s.startswith("SELECT id FROM prompts WHERE project_id"):
            pid = self.by_name.get(args[1])
            return None if pid is None else {"id": pid}
        if s.startswith("SELECT project_id FROM prompts WHERE id"):
            return {"project_id": self.project_id}
        if s.startswith("SELECT pv.id"):
            return self.current
        raise AssertionError(f"Unexpected query: {sql!r}")


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Acquire:
            async def __aenter__(self_inner):
                return conn

            async def __aexit__(self_inner, *exc):
                return False

        return _Acquire()


def _build_app(monkeypatch, enabled: bool):
    if enabled:
        monkeypatch.setenv("QCP_ENABLED", "true")
    else:
        monkeypatch.delenv("QCP_ENABLED", raising=False)
    import main
    return importlib.reload(main).app


def _ai_engine(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr("routers.prompts.httpx.AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def _client(app, conn=None, auth=AuthContext(project_id=10, via="api_key")):
    app.state.pg_pool = FakePool(conn or FakeConn())
    if auth is not None:
        app.dependency_overrides[get_auth_context] = lambda: auth
    return TestClient(app)


@pytest.fixture
def app(monkeypatch):
    app = _build_app(monkeypatch, enabled=True)
    yield app
    app.dependency_overrides.clear()


class TestFeatureFlag:
    def test_off_by_default_routes_are_not_mounted(self, monkeypatch):
        app = _build_app(monkeypatch, enabled=False)
        assert "/projects" in app.openapi()["paths"]  # guard: this check can see mounted routes
        assert not [p for p in app.openapi()["paths"] if p.startswith("/qcp")]
        client = _client(app)
        assert client.post("/qcp/rootcause", json=BODY).status_code == 404
        assert client.post("/qcp/rollback", json={}).status_code == 404
        assert client.get("/health").json() == {"status": "ok"}  # existing routes unaffected

    @pytest.mark.parametrize("value", ["", "false", "0", "no"])
    def test_falsy_values_keep_it_off(self, monkeypatch, value):
        monkeypatch.setenv("QCP_ENABLED", value)
        from routers import qcp
        assert qcp.qcp_enabled() is False

    def test_on_mounts_both_routes(self, app):
        assert {"/qcp/rootcause", "/qcp/rollback"} <= set(app.openapi()["paths"])


class TestRootCause:
    def test_success_by_prompt_name_reports_only_real_values(self, app, monkeypatch):
        _ai_engine(monkeypatch, lambda req: httpx.Response(200, json=IMPACT))
        r = _client(app).post("/qcp/rootcause", json=BODY)
        assert r.status_code == 200
        body = r.json()
        assert body["cause"] == IMPACT["interpretation"]
        assert body["suspect_version"] == "v3"
        text = " | ".join(body["evidence"])
        assert "0.91" in text and "0.78" in text and "p=0.004" in text
        assert "Observational estimate" in text
        assert set(body) == {"cause", "suspect_version", "evidence"}

    def test_numeric_prompt_id_and_bare_version(self, app, monkeypatch):
        seen = {}

        def handler(req):
            seen["prompt_id"] = req.url.params["prompt_id"]
            return httpx.Response(200, json=IMPACT)

        _ai_engine(monkeypatch, handler)
        r = _client(app).post("/qcp/rootcause", json={**BODY, "prompt_id": "7", "version": "3"})
        assert r.status_code == 200 and seen["prompt_id"] == "7"
        assert r.json()["suspect_version"] == "3"

    def test_extra_incident_fields_are_accepted(self, app, monkeypatch):
        _ai_engine(monkeypatch, lambda req: httpx.Response(200, json=IMPACT))
        assert _client(app).post("/qcp/rootcause", json={**BODY, "change_note": "sped up"}).status_code == 200

    def test_version_other_than_current_is_refused(self, app, monkeypatch):
        _ai_engine(monkeypatch, lambda req: httpx.Response(200, json=IMPACT))
        r = _client(app).post("/qcp/rootcause", json={**BODY, "version": "v2"})
        assert r.status_code == 409 and "v3" in r.json()["detail"]

    def test_ai_engine_down_is_502_not_a_made_up_answer(self, app, monkeypatch):
        def handler(req):
            raise httpx.ConnectError("down")

        _ai_engine(monkeypatch, handler)
        assert _client(app).post("/qcp/rootcause", json=BODY).status_code == 502

    def test_unknown_prompt_name_is_404(self, app):
        assert _client(app).post("/qcp/rootcause", json={**BODY, "prompt_id": "nope"}).status_code == 404

    def test_no_deployed_version_is_404(self, app):
        r = _client(app, FakeConn(current=None)).post("/qcp/rootcause", json=BODY)
        assert r.status_code == 404

    def test_bad_version_label_is_422(self, app):
        assert _client(app).post("/qcp/rootcause", json={**BODY, "version": "latest"}).status_code == 422

    def test_missing_field_is_422(self, app):
        bad = {k: v for k, v in BODY.items() if k != "score"}
        assert _client(app).post("/qcp/rootcause", json=bad).status_code == 422

    def test_other_projects_prompt_is_403(self, app, monkeypatch):
        _ai_engine(monkeypatch, lambda req: httpx.Response(200, json=IMPACT))
        r = _client(app, FakeConn(project_id=99)).post("/qcp/rootcause", json={**BODY, "prompt_id": "7"})
        assert r.status_code == 403

    def test_requires_credentials(self, app):
        assert _client(app, auth=None).post("/qcp/rootcause", json=BODY).status_code == 401


class TestRollback:
    ROLLBACK = {"incident_id": "inc-1", "prompt_id": "aria-tutor", "from_version": "v3", "to_version": "v2"}

    def test_is_501_with_clear_message(self, app):
        r = _client(app).post("/qcp/rollback", json=self.ROLLBACK)
        assert r.status_code == 501
        assert "caller-specified version" in r.json()["detail"]

    def test_requires_credentials(self, app):
        assert _client(app, auth=None).post("/qcp/rollback", json=self.ROLLBACK).status_code == 401

    def test_invalid_body_is_422(self, app):
        assert _client(app).post("/qcp/rollback", json={"incident_id": "x"}).status_code == 422
