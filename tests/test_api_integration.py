from datetime import datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPBasicCredentials
from fastapi.testclient import TestClient

from app import main
from app import dashboard


def signal_payload(signal_id="sig-api"):
    return {
        "market_url": "https://polymarket.com/event/example",
        "outcome": "Team A",
        "market_type": "moneyline",
        "max_price": "0.50",
        "budget_usdc": "5",
        "signal_id": signal_id,
        "category": "sports",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


def quote():
    return {
        "asset_id": "token-1",
        "limit_price": "0.50",
        "shares": "10",
        "resolved_outcome": "Team A",
        "would_cross_now": True,
    }


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "PENDING_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(main, "WATCH_FILE", tmp_path / "watchlist.json")
    monkeypatch.setattr(main, "EXECUTIONS_FILE", tmp_path / "executions.json")
    monkeypatch.setattr(main, "SIGNAL_SECRET", "secret")
    monkeypatch.setattr(main, "_quote", lambda *a, **k: quote())
    return tmp_path


def test_auto_execute_endpoint(isolated, monkeypatch):
    monkeypatch.setattr(main, "_try_execute_signal", lambda s: {"status": "AUTO_DRY_RUN", "id": s.signal_id})
    with TestClient(main.app) as client:
        response = client.post("/auto/execute", json=signal_payload(), headers={"X-Signal-Secret": "secret"})
    assert response.status_code == 200
    assert response.json()["id"] == "sig-api"


def test_auto_watch_endpoint_creates_watch(isolated, monkeypatch):
    monkeypatch.setattr(main, "_try_execute_signal", lambda s: {"status": "WAITING_FOR_PRICE", "signal_id": s.signal_id, "quote": quote()})
    with TestClient(main.app) as client:
        response = client.post("/auto/watch", json=signal_payload("sig-watch"), headers={"X-Signal-Secret": "secret"})
    assert response.status_code == 200
    assert response.json()["signal_id"] == "sig-watch"
    assert "sig-watch" in main._load(main.WATCH_FILE)


def test_prepare_and_approve_endpoints(isolated, monkeypatch):
    def fake_execute(intent, fresh, *, auto, signal_id=None):
        return {
            "id": signal_id,
            "status": "APPROVED_DRY_RUN",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "budget_usdc": str(intent.budget_usdc),
        }
    monkeypatch.setattr(main, "_execute_limit", fake_execute)

    with TestClient(main.app) as client:
        prepared = client.post("/prepare", json={
            "market_url": "https://polymarket.com/event/example",
            "outcome": "Team A",
            "market_type": "moneyline",
            "max_price": "0.50",
            "budget_usdc": "5",
        })
        assert prepared.status_code == 200
        trade_id = prepared.json()["id"]
        approved = client.post(f"/approve/{trade_id}", json={"confirmation": "APPROVE"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED_DRY_RUN"


def test_dashboard_auth_valid_missing_and_wrong(monkeypatch):
    monkeypatch.setattr(dashboard, "DASHBOARD_USER", "admin")
    monkeypatch.setattr(dashboard, "DASHBOARD_PASSWORD", "pw")
    creds = HTTPBasicCredentials(username="admin", password="pw")
    assert dashboard._auth(creds) == "admin"

    with pytest.raises(Exception) as wrong:
        dashboard._auth(HTTPBasicCredentials(username="admin", password="bad"))
    assert wrong.value.status_code == 401

    monkeypatch.setattr(dashboard, "DASHBOARD_PASSWORD", "")
    with pytest.raises(Exception) as missing:
        dashboard._auth(creds)
    assert missing.value.status_code == 503
