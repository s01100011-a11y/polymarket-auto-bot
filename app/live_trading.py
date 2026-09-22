from __future__ import annotations

import base64
import logging
import os
import secrets
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from typing import Any

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from polymarket import PublicClient, SecureClient

from app import slack_dashboard_v2 as base
from app.structured_logging import log_event

app = base.app
core = base.core
dashboard = base.dashboard
ingest = base.ingest
logger = logging.getLogger("polymarket_bot.live_trading")

LIVE_TEST_MAX_USDC = Decimal(os.getenv("LIVE_TEST_MAX_USDC", "5"))
LIVE_TEST_FILL_WAIT_SECONDS = max(2, min(15, int(os.getenv("LIVE_TEST_FILL_WAIT_SECONDS", "6"))))


class LiveTestBuy(BaseModel):
    market_url: str = Field(min_length=10, max_length=500)
    outcome: str = Field(min_length=1, max_length=160)
    market_type: str = Field(default="moneyline", min_length=1, max_length=120)
    max_price: Decimal = Field(gt=0, lt=1)
    budget_usdc: Decimal = Field(gt=0)


class LiveTestSell(BaseModel):
    min_price: Decimal | None = Field(default=None, gt=0, lt=1)


def _credentials() -> tuple[str, str]:
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if not private_key:
        raise HTTPException(status_code=503, detail="POLYMARKET_PRIVATE_KEY is not configured")
    if not wallet:
        raise HTTPException(status_code=503, detail="POLYMARKET_DEPOSIT_WALLET is not configured")
    return private_key, wallet


def _basic_ok(request: Request) -> bool:
    header = request.headers.get("authorization") or ""
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
        user, password = decoded.split(":", 1)
    except Exception:
        return False
    return secrets.compare_digest(user, dashboard.DASHBOARD_USER) and secrets.compare_digest(password, dashboard.DASHBOARD_PASSWORD)


@app.middleware("http")
async def protect_legacy_manual_trading(request: Request, call_next):
    # The original manual BUY endpoints were created before the dashboard existed.
    # Once a funded wallet is attached they must not be callable anonymously.
    protected = request.method == "POST" and (
        request.url.path == "/prepare"
        or request.url.path.startswith("/approve/")
        or request.url.path.startswith("/api/live/")
    )
    if protected and not _basic_ok(request):
        return JSONResponse(
            status_code=401,
            content={"detail": "Dashboard authentication required"},
            headers={"WWW-Authenticate": "Basic"},
        )
    return await call_next(request)


def _mask_wallet(value: str) -> str:
    if len(value) < 12:
        return value
    return value[:6] + "…" + value[-4:]


def _position_size(wallet: str, asset_id: str) -> Decimal:
    with PublicClient() as client:
        try:
            positions = client.list_positions(user=wallet, page_size=100).iter_items()
            for position in positions:
                if str(getattr(position, "asset_id", "")) == str(asset_id):
                    return Decimal(str(getattr(position, "current_size", "0") or "0"))
        except Exception:
            return Decimal("0")
    return Decimal("0")


def _cancel_quietly(client: SecureClient, order_id: str | None) -> None:
    if not order_id:
        return
    try:
        client.cancel_order(order_id=order_id)
    except Exception:
        pass


def _secure_client() -> SecureClient:
    private_key, wallet = _credentials()
    return SecureClient.create(private_key=private_key, wallet=wallet)


def _auth_probe() -> dict[str, Any]:
    private_key, wallet = _credentials()
    with SecureClient.create(private_key=private_key, wallet=wallet) as client:
        return {
            "ok": True,
            "wallet": _mask_wallet(str(client.wallet)),
            "signer": _mask_wallet(str(client.signer)),
            "wallet_type": str(client.wallet_type),
        }


@app.get("/api/live/status", dependencies=[Depends(dashboard._auth)])
def live_status():
    private_key = bool(os.getenv("POLYMARKET_PRIVATE_KEY", "").strip())
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    return {
        "credentials_configured": bool(private_key and wallet),
        "private_key_configured": private_key,
        "deposit_wallet_configured": bool(wallet),
        "deposit_wallet": _mask_wallet(wallet) if wallet else None,
        "live_trading": core.LIVE_TRADING,
        "auto_trading": core.auto_trading_enabled(),
        "slack_paper_only": ingest.SLACK_PAPER_ONLY,
        "test_max_usdc": str(LIVE_TEST_MAX_USDC),
    }


@app.post("/api/live/auth-check", dependencies=[Depends(dashboard._auth)])
def live_auth_check():
    return _auth_probe()


def _validate_test_market(req: LiveTestBuy):
    if req.budget_usdc > LIVE_TEST_MAX_USDC:
        raise HTTPException(status_code=400, detail=f"Test budget exceeds LIVE_TEST_MAX_USDC=${LIVE_TEST_MAX_USDC}")

    intent = core.TradeIntent(
        market_url=req.market_url,
        outcome=req.outcome,
        market_type=req.market_type,
        max_price=req.max_price,
        budget_usdc=req.budget_usdc,
        note="Protected dashboard live BUY/SELL test",
    )

    core._check_geoblock()
    with PublicClient() as client:
        market = core._select_market(client, intent)
        # Restrict this live test harness to sports markets. It is not a generic
        # political/event auto-trader.
        sports_type = core._market_type(market)
        if not sports_type:
            raise HTTPException(status_code=400, detail="Live test is restricted to Polymarket sports markets")
        asset_id, _, outcome_label = core._resolve_asset(market, req.outcome)
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        book = client.get_order_book(asset_id=asset_id)
        min_size = Decimal(str(getattr(getattr(market, "trading", None), "minimum_order_size", "0") or "0"))

    if buy_price <= 0 or buy_price >= 1:
        raise HTTPException(status_code=400, detail=f"Invalid current BUY price {buy_price}")
    if buy_price > req.max_price:
        raise HTTPException(status_code=400, detail=f"Current BUY price {buy_price} exceeds test max_price {req.max_price}")
    if spread > core.MAX_SPREAD:
        raise HTTPException(status_code=400, detail=f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    best_ask = None
    if getattr(book, "asks", None):
        best_ask = min(Decimal(str(level.price)) for level in book.asks)
    if best_ask is None or best_ask > req.max_price:
        raise HTTPException(status_code=400, detail="Test limit would not cross the current best ask")

    shares = (req.budget_usdc / req.max_price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if min_size > 0 and shares < min_size:
        min_budget = (min_size * req.max_price).quantize(Decimal("0.01"))
        raise HTTPException(
            status_code=400,
            detail=f"Order is below this market's minimum size ({min_size} shares; about ${min_budget} at the selected limit)",
        )

    return intent, market, asset_id, outcome_label, buy_price, spread, shares


@app.post("/api/live/test-buy", dependencies=[Depends(dashboard._auth)])
def live_test_buy(req: LiveTestBuy):
    if not core.LIVE_TRADING:
        raise HTTPException(status_code=409, detail="LIVE_TRADING is disabled")

    _, market, asset_id, outcome_label, buy_price, spread, requested_shares = _validate_test_market(req)
    _, wallet = _credentials()
    before = _position_size(wallet, asset_id)

    order_id = None
    response_text = None
    with _secure_client() as client:
        response = client.place_limit_order(
            token_id=asset_id,
            price=str(req.max_price),
            size=str(requested_shares),
            side="BUY",
        )
        order_id = str(getattr(response, "order_id", "") or "")
        response_text = str(response)

        deadline = time.time() + LIVE_TEST_FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = _position_size(wallet, asset_id)
            if after > before:
                break
        _cancel_quietly(client, order_id)

    filled = max(Decimal("0"), after - before)
    record_id = f"live-test-{uuid.uuid4().hex[:10]}"
    now = ingest._now_iso()
    market_url = req.market_url

    if filled <= 0:
        record = {
            "id": record_id,
            "status": "TEST_BUY_UNFILLED_CANCELED",
            "created_at": now,
            "submitted_at": now,
            "side": "BUY",
            "auto": False,
            "paper": False,
            "source": "dashboard_live_test",
            "budget_usdc": str(req.budget_usdc),
            "quote": {
                "market": str(getattr(market, "question", None) or getattr(market, "slug", "sports market")),
                "market_url": market_url,
                "market_type": core._market_type(market),
                "requested_outcome": req.outcome,
                "resolved_outcome": outcome_label,
                "asset_id": str(asset_id),
                "limit_price": str(req.max_price),
                "current_buy_price": str(buy_price),
                "spread": str(spread),
                "shares": "0",
            },
            "execution": {"placed": True, "order_id": order_id, "response": response_text, "canceled_remainder": True},
        }
    else:
        estimated_cost = (filled * req.max_price).quantize(Decimal("0.01"))
        record = {
            "id": record_id,
            "status": "ORDER_SUBMITTED",
            "created_at": now,
            "submitted_at": now,
            "side": "BUY",
            "auto": False,
            "paper": False,
            "source": "dashboard_live_test",
            "budget_usdc": str(estimated_cost),
            "filled_shares": str(filled),
            "pre_position_size": str(before),
            "quote": {
                "market": str(getattr(market, "question", None) or getattr(market, "slug", "sports market")),
                "market_url": market_url,
                "market_type": core._market_type(market),
                "requested_outcome": req.outcome,
                "resolved_outcome": outcome_label,
                "asset_id": str(asset_id),
                "limit_price": str(req.max_price),
                "entry_price": str(req.max_price),
                "current_buy_price": str(buy_price),
                "spread": str(spread),
                "shares": str(filled),
            },
            "execution": {"placed": True, "order_id": order_id, "response": response_text, "canceled_remainder": True},
        }

    executions = core._load(core.EXECUTIONS_FILE)
    executions[record_id] = record
    core._save(core.EXECUTIONS_FILE, executions)
    return {
        "ok": filled > 0,
        "trade_id": record_id,
        "status": record["status"],
        "order_id": order_id,
        "filled_shares": str(filled),
        "position_before": str(before),
        "position_after": str(after),
        "limit_price": str(req.max_price),
    }


@app.post("/api/live/test-sell/{trade_id}", dependencies=[Depends(dashboard._auth)])
def live_test_sell(trade_id: str, req: LiveTestSell):
    if not core.LIVE_TRADING:
        raise HTTPException(status_code=409, detail="LIVE_TRADING is disabled")

    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown trade id")
    if rec.get("source") != "dashboard_live_test" or rec.get("paper"):
        raise HTTPException(status_code=400, detail="Only protected live-test positions can be sold by this endpoint")
    if rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
        raise HTTPException(status_code=400, detail=f"Trade status is {rec.get('status')}")

    q = rec.get("quote") or {}
    asset_id = str(q.get("asset_id") or "")
    if not asset_id:
        raise HTTPException(status_code=400, detail="Trade has no asset id")
    target = Decimal(str(rec.get("remaining_shares") or rec.get("filled_shares") or q.get("shares") or "0"))
    if target <= 0:
        raise HTTPException(status_code=400, detail="Trade has no tracked filled shares to sell")

    _, wallet = _credentials()
    before = _position_size(wallet, asset_id)
    sell_size = min(before, target)
    if sell_size <= 0:
        raise HTTPException(status_code=400, detail="Wallet no longer holds the tracked test shares")

    with PublicClient() as public:
        sell_price = Decimal(str(public.get_price(asset_id=asset_id, side="SELL")))
    if sell_price <= 0 or sell_price >= 1:
        raise HTTPException(status_code=400, detail=f"Invalid current SELL price {sell_price}")
    if req.min_price is not None and sell_price < req.min_price:
        raise HTTPException(status_code=400, detail=f"Current SELL price {sell_price} is below min_price {req.min_price}")

    order_id = None
    response_text = None
    with _secure_client() as client:
        response = client.place_limit_order(
            token_id=asset_id,
            price=str(sell_price),
            size=str(sell_size),
            side="SELL",
        )
        order_id = str(getattr(response, "order_id", "") or "")
        response_text = str(response)

        deadline = time.time() + LIVE_TEST_FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = _position_size(wallet, asset_id)
            if after < before:
                break
        _cancel_quietly(client, order_id)

    sold = max(Decimal("0"), before - after)
    if sold <= 0:
        raise HTTPException(status_code=409, detail="SELL order did not fill; remainder was canceled")

    entry = Decimal(str(q.get("entry_price") or q.get("limit_price") or "0"))
    realized = ((sell_price - entry) * sold).quantize(Decimal("0.01"))
    remaining = max(Decimal("0"), target - sold)
    now = ingest._now_iso()

    rec["status"] = "CLOSED" if remaining <= Decimal("0.0001") else "PARTIALLY_CLOSED"
    rec["exit_price"] = str(sell_price)
    rec["realized_pnl"] = str(realized)
    rec["closed_at"] = now if rec["status"] == "CLOSED" else None
    rec["remaining_shares"] = str(remaining)
    rec.setdefault("execution", {})["close_order_id"] = order_id
    rec["execution"]["close_response"] = response_text

    sell_id = f"{trade_id}-sell-{uuid.uuid4().hex[:6]}"
    sell_record = {
        "id": sell_id,
        "status": "SELL_FILLED",
        "created_at": now,
        "submitted_at": now,
        "side": "SELL",
        "auto": False,
        "paper": False,
        "source": "dashboard_live_test",
        "parent_trade_id": trade_id,
        "display_pnl": str(realized),
        "budget_usdc": str((sell_price * sold).quantize(Decimal("0.01"))),
        "quote": {
            "market": q.get("market"),
            "market_url": q.get("market_url"),
            "market_type": q.get("market_type"),
            "requested_outcome": q.get("requested_outcome"),
            "resolved_outcome": q.get("resolved_outcome"),
            "asset_id": asset_id,
            "limit_price": str(sell_price),
            "shares": str(sold),
        },
        "execution": {"placed": True, "order_id": order_id, "response": response_text, "canceled_remainder": True},
    }
    executions[trade_id] = rec
    executions[sell_id] = sell_record
    core._save(core.EXECUTIONS_FILE, executions)

    return {
        "ok": True,
        "trade_id": trade_id,
        "sell_record_id": sell_id,
        "status": rec["status"],
        "order_id": order_id,
        "sold_shares": str(sold),
        "remaining_shares": str(remaining),
        "sell_price": str(sell_price),
        "realized_pnl": str(realized),
    }


_BASE_SNAPSHOT = dashboard._dashboard_snapshot


def _snapshot_with_sell_rows():
    data = _BASE_SNAPSHOT()
    executions = core._load(core.EXECUTIONS_FILE)
    by_id = {str(k): v for k, v in executions.items()}
    for row in data.get("history", []):
        rec = by_id.get(str(row.get("id"))) or {}
        if rec.get("display_pnl") is not None:
            row["pnl"] = str(rec.get("display_pnl"))
            row["pnl_type"] = "realized"
        if rec.get("side"):
            row["side"] = str(rec.get("side")).upper()
    return data


dashboard._dashboard_snapshot = _snapshot_with_sell_rows


# Read-only startup signal for Railway logs after the user adds credentials.
if os.getenv("POLYMARKET_PRIVATE_KEY", "").strip() and os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip():
    try:
        probe = _auth_probe()
        log_event(
            logger,
            "polymarket_auth_check",
            stage="startup",
            status="ok",
        )
    except Exception as exc:
        log_event(
            logger,
            "polymarket_auth_check",
            level=logging.ERROR,
            stage="startup",
            status="error",
            reason=type(exc).__name__,
            exc_info=True,
        )
