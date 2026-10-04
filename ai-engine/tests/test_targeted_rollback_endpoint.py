"""
POST /rollback on the ai-engine: behind QCP_ENABLED, success passes through, each refusal has its own status.

Run with:  pytest tests/test_targeted_rollback_endpoint.py -v
"""
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import main  # noqa: E402
from rollback import RollbackRefused  # noqa: E402

BODY = {"prompt_id": 7, "to_version_number": 3, "from_version_number": 4, "reason": "quality dropped",
        "requested_by": "qcp:api_key:project 10"}
RESULT = {"rollback_id": 501, "prompt_id": 7, "from_version_number": 4, "to_version_number": 3}


@pytest.fixture
def client():
    return TestClient(main.app)  # not used as a context manager: no lifespan, no DB


def test_it_is_off_unless_qcp_enabled_is_set(client, monkeypatch):
    monkeypatch.delenv("QCP_ENABLED", raising=False)
    with patch("main.rollback_to_version", new=AsyncMock(return_value=RESULT)) as op:
        r = client.post("/rollback", json=BODY)
    assert r.status_code == 403 and "QCP_ENABLED" in r.json()["detail"]
    op.assert_not_awaited()  # the operation is never reached while the flag is off


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_falsy_flag_values_keep_it_off(client, monkeypatch, value):
    monkeypatch.setenv("QCP_ENABLED", value)
    assert client.post("/rollback", json=BODY).status_code == 403


def test_success_passes_the_request_through(client, monkeypatch):
    monkeypatch.setenv("QCP_ENABLED", "true")
    with patch("main.rollback_to_version", new=AsyncMock(return_value=RESULT)) as op:
        r = client.post("/rollback", json=BODY)
    assert r.status_code == 200 and r.json() == RESULT
    op.assert_awaited_once_with(7, 3, reason="quality dropped", requested_by="qcp:api_key:project 10",
                                from_version_number=4)


@pytest.mark.parametrize("kind,status", [("not_found", 404), ("unknown_version", 404), ("not_current", 409),
                                         ("already_current", 409), ("no_current", 409), ("not_passed", 422),
                                         ("invalid", 422)])
def test_each_refusal_has_its_own_status_and_message(client, monkeypatch, kind, status):
    monkeypatch.setenv("QCP_ENABLED", "1")
    with patch("main.rollback_to_version", new=AsyncMock(side_effect=RollbackRefused(kind, f"because {kind}"))):
        r = client.post("/rollback", json=BODY)
    assert r.status_code == status and r.json()["detail"] == f"because {kind}"


def test_a_malformed_request_is_422(client, monkeypatch):
    monkeypatch.setenv("QCP_ENABLED", "1")
    assert client.post("/rollback", json={"prompt_id": 7}).status_code == 422
