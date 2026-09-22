from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import main


def intent(**overrides):
    data = {
        "market_url": "https://polymarket.com/event/example",
        "outcome": "Team A",
        "market_type": "moneyline",
        "max_price": Decimal("0.50"),
        "budget_usdc": Decimal("5"),
    }
    data.update(overrides)
    return main.TradeIntent(**data)


def signal(**overrides):
    data = {
        **intent().model_dump(),
        "signal_id": "sig-123",
        "category": "sports",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    data.update(overrides)
    return main.AutoSignal(**data)


def test_risk_checks_enforce_budget_price_and_spread(monkeypatch):
    monkeypatch.setattr(main, "MAX_TRADE_USDC", Decimal("10"))
    monkeypatch.setattr(main, "MAX_AUTO_TRADE_USDC", Decimal("5"))
    monkeypatch.setattr(main, "MAX_PRICE", Decimal("0.80"))
    monkeypatch.setattr(main, "MAX_SPREAD", Decimal("0.10"))

    main._risk_checks(intent(budget_usdc=Decimal("10")), Decimal("0.10"))
    with pytest.raises(HTTPException):
        main._risk_checks(intent(budget_usdc=Decimal("11")), Decimal("0.01"))
    with pytest.raises(HTTPException):
        main._risk_checks(intent(budget_usdc=Decimal("6")), Decimal("0.01"), auto=True)
    with pytest.raises(HTTPException):
        main._risk_checks(intent(max_price=Decimal("0.81")), Decimal("0.01"))
    with pytest.raises(HTTPException):
        main._risk_checks(intent(), Decimal("0.11"))


def test_signal_validation_expiry_and_politics(monkeypatch):
    now = datetime(2026, 9, 22, 8, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(main, "_now", lambda: now)
    monkeypatch.setattr(main, "BLOCK_POLITICAL_AUTO", True)

    valid = signal(expires_at=(now + timedelta(minutes=5)).isoformat())
    assert main._validate_auto_signal(valid) > now

    with pytest.raises(HTTPException):
        main._validate_auto_signal(signal(expires_at=(now - timedelta(seconds=1)).isoformat()))
    with pytest.raises(HTTPException):
        main._validate_auto_signal(signal(
            category="politics",
            expires_at=(now + timedelta(minutes=5)).isoformat(),
        ))

    monkeypatch.setattr(main, "BLOCK_POLITICAL_AUTO", False)
    main._validate_auto_signal(signal(
        category="politics",
        expires_at=(now + timedelta(minutes=5)).isoformat(),
    ))


def test_daily_budget_aggregation_uses_trading_timezone(tmp_path, monkeypatch):
    tz = ZoneInfo("Asia/Kuala_Lumpur")
    monkeypatch.setattr(main, "TRADING_TIMEZONE", tz)
    monkeypatch.setattr(main, "EXECUTIONS_FILE", tmp_path / "executions.json")
    local_today = datetime.now(tz).date()
    local_stamp = datetime.combine(local_today, time(0, 30), tzinfo=tz).astimezone(timezone.utc)

    main._save(main.EXECUTIONS_FILE, {
        "a": {"status": "ORDER_SUBMITTED", "budget_usdc": "4", "submitted_at": local_stamp.isoformat()},
        "b": {"status": "AUTO_DRY_RUN", "budget_usdc": "3", "submitted_at": local_stamp.isoformat()},
        "c": {"status": "WAITING_FOR_PRICE", "budget_usdc": "99", "submitted_at": local_stamp.isoformat()},
        "d": {"status": "ORDER_SUBMITTED", "budget_usdc": "8", "submitted_at": (local_stamp - timedelta(days=1)).isoformat()},
    })
    assert main._daily_budget_used() == Decimal("7")

    monkeypatch.setattr(main, "MAX_DAILY_BUDGET_USDC", Decimal("10"))
    main._check_daily_budget(intent(budget_usdc=Decimal("3")))
    with pytest.raises(HTTPException):
        main._check_daily_budget(intent(budget_usdc=Decimal("4")))


def _market(yes="Team A", no="Team B", market_type="moneyline", accepting=True, active=True):
    return SimpleNamespace(
        id="m1",
        slug="m1",
        question="A vs B",
        sports=SimpleNamespace(sports_market_type=market_type),
        state=SimpleNamespace(accepting_orders=accepting, active=active),
        outcomes=SimpleNamespace(
            yes=SimpleNamespace(label=yes, token_id="yes-token", position_id=None),
            no=SimpleNamespace(label=no, token_id="no-token", position_id=None),
        ),
    )


def test_market_resolution_and_matching():
    market = _market()
    assert main._outcome_matches_market(market, "team a")
    assert main._outcome_matches_market(market, "YES")
    assert not main._outcome_matches_market(market, "Team C")
    assert main._resolve_asset(market, "Team A") == ("yes-token", "YES", "Team A")
    assert main._resolve_asset(market, "Team B") == ("no-token", "NO", "Team B")
    with pytest.raises(HTTPException):
        main._resolve_asset(market, "Team C")


def test_select_market_prefers_unique_accepting_candidate():
    active = _market(accepting=True)
    closed = _market(accepting=False)
    client = SimpleNamespace(get_event=lambda slug: SimpleNamespace(title="Game", markets=[closed, active]))
    selected = main._select_market(
        client,
        intent(market_url="https://polymarket.com/sports/basketball/game-slug"),
    )
    assert selected is active


def test_select_market_rejects_ambiguous_candidates():
    one = _market()
    two = _market()
    client = SimpleNamespace(get_event=lambda slug: SimpleNamespace(title="Game", markets=[one, two]))
    with pytest.raises(HTTPException):
        main._select_market(
            client,
            intent(market_url="https://polymarket.com/sports/basketball/game-slug"),
        )


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
    def raise_for_status(self):
        return None
    def json(self):
        return self.payload


def test_geoblock_allowed_and_blocked(monkeypatch):
    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResponse({"blocked": False, "country": "MY"}))
    assert main._check_geoblock()["blocked"] is False

    monkeypatch.setattr(main.httpx, "get", lambda *a, **k: FakeResponse({"blocked": True, "country": "XX", "region": "YY"}))
    with pytest.raises(HTTPException) as exc:
        main._check_geoblock()
    assert exc.value.status_code == 451


def test_signal_secret_valid_invalid_missing(monkeypatch):
    monkeypatch.setattr(main, "SIGNAL_SECRET", "secret")
    main._require_signal_secret("secret")
    with pytest.raises(HTTPException) as exc:
        main._require_signal_secret("wrong")
    assert exc.value.status_code == 401
    with pytest.raises(HTTPException):
        main._require_signal_secret(None)

    monkeypatch.setattr(main, "SIGNAL_SECRET", "")
    with pytest.raises(HTTPException) as exc:
        main._require_signal_secret("anything")
    assert exc.value.status_code == 503
