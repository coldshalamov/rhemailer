import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

TEST_DB_PATH = Path("tmp/test_emailer.db")
TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("DB_PATH", f"sqlite:///{TEST_DB_PATH}")
os.environ.setdefault("API_BEARER_TOKEN", "dev")

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def auth() -> dict[str, str]:
    return {"Authorization": "Bearer dev"}


@pytest.fixture(autouse=True)
def reset_db() -> None:
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    yield


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def test_health_includes_version(client: TestClient) -> None:
    res = client.get("/health", headers=auth())
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["version"]


def test_direct_send_json_dry_run_ok(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    called = {"value": False}

    def fake_send(_payload):
        called["value"] = True
        return True, None

    monkeypatch.setattr("app.main.send_email_with_fallback", fake_send)

    payload = {
        "to_email": "test@example.com",
        "subject": "Test",
        "body_html": "<p>hi</p>",
        "dry_run": True,
    }
    res = client.post("/direct_send", headers=auth(), json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is True
    assert "id" in body
    assert called["value"] is False
    assert body["reason"] is None
    assert body["results"][0]["email"] == "test@example.com"
    assert body["results"][0]["sent"] is True


def test_direct_send_legacy_payload_ok(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    def fake_send(_payload):
        return True, None

    monkeypatch.setattr("app.main.send_email_with_fallback", fake_send)

    legacy = {
        "to_email": "test@example.com",
        "subject": "Test",
        "body_html": "<p>hi</p>",
        "dry_run": True,
    }
    q = json.dumps(legacy)
    res = client.post(f"/direct_send?payload={q}", headers=auth())
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is True
    assert body["results"][0]["email"] == "test@example.com"


def test_direct_send_suppressed(client: TestClient) -> None:
    with db.get_session() as session:
        db.add_to_suppression(session, "test@example.com")

    payload = {
        "to_email": "test@example.com",
        "subject": "Test",
        "body_html": "<p>hi</p>",
        "dry_run": False,
    }
    res = client.post("/direct_send", headers=auth(), json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is False
    assert body["reason"] == "suppressed"
    assert body["results"][0]["email"] == "test@example.com"
    assert body["results"][0]["sent"] is False


def test_direct_send_empty_body_html_rejected(client: TestClient) -> None:
    payload = {
        "to_email": "test@example.com",
        "subject": "Test",
        "body_html": "   ",
        "dry_run": True,
    }
    res = client.post("/direct_send", headers=auth(), json=payload)
    assert res.status_code == 422
    assert "body_html" in res.json()["detail"]


def test_direct_send_invalid_legacy_json(client: TestClient) -> None:
    res = client.post("/direct_send?payload={not-json}", headers=auth())
    assert res.status_code == 422
    assert "Invalid JSON" in res.json()["detail"]


def test_direct_send_real_send_failure(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    def fake_send(_payload):
        return False, "send failed"

    monkeypatch.setattr("app.main.send_email_with_fallback", fake_send)

    payload = {
        "to_email": "test@example.com",
        "subject": "Test",
        "body_html": "<p>hi</p>",
        "dry_run": False,
    }
    res = client.post("/direct_send", headers=auth(), json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is False
    assert body["reason"] == "send failed"
    assert body["results"][0]["reason"] == "send failed"


def test_direct_send_multiple_recipients(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> None:
    calls = []

    def fake_send(payload):
        calls.append(payload.to_email)
        return True, None

    monkeypatch.setattr("app.main.send_email_with_fallback", fake_send)

    payload = {
        "to_email": ["user1@example.com", "user2@example.com"],
        "subject": "Test",
        "body_html": "<p>hi</p>",
        "dry_run": False,
    }
    res = client.post("/direct_send", headers=auth(), json=payload)
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is True
    assert len(body["results"]) == 2
    assert {result["email"] for result in body["results"]} == {"user1@example.com", "user2@example.com"}
    assert len(calls) == 2
