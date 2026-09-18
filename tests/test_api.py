from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import create_app


def test_api_endpoints_and_controls(settings_factory) -> None:
    settings_factory(app_mode="paper_auto")
    with TestClient(create_app()) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["mode"] == "paper_auto"
        assert health.json()["live_enabled"] is False

        status = client.get("/status")
        assert status.status_code == 200
        assert status.json()["safe_mode"] is True

        positions = client.get("/positions")
        trades = client.get("/trades")
        assert positions.status_code == 200
        assert trades.status_code == 200
        assert positions.json() == []
        assert trades.json() == []

        paused = client.post("/pause")
        resumed = client.post("/resume")
        assert paused.status_code == 200
        assert paused.json()["status"] == "paused"
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "running"

        set_shadow = client.post("/set-mode", json={"mode": "shadow"})
        assert set_shadow.status_code == 200
        assert set_shadow.json()["mode"] == "shadow"

        set_live = client.post("/set-mode", json={"mode": "live_auto"})
        assert set_live.status_code == 400

        reset = client.post("/reset-paper-account")
        assert reset.status_code == 200
        assert reset.json()["status"] == "reset"


def test_webhook_endpoints_require_secret(settings_factory) -> None:
    settings_factory(app_mode="paper_auto")
    with TestClient(create_app()) as client:
        unauthorized = client.post("/webhook/manual-approve", json={"market_id": "m1"})
        assert unauthorized.status_code == 401

        approved = client.post(
            "/webhook/manual-approve",
            json={"market_id": "m1", "reviewer": "tester"},
            headers={"X-Webhook-Token": "test-secret"},
        )
        rejected = client.post(
            "/webhook/manual-reject",
            json={"market_id": "m1", "reviewer": "tester"},
            headers={"X-Webhook-Token": "test-secret"},
        )

        assert approved.status_code == 200
        assert approved.json()["status"] == "stubbed"
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "stubbed"
