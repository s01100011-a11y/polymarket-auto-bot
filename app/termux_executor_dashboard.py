from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException
from pydantic import BaseModel, Field
from polymarket import PublicClient

from app import live_test_dashboard as base
from app import live_trading

app = base.app
dashboard = base.dashboard
core = base.core
ingest = live_trading.ingest

RAILWAY_EXECUTION_ENABLED = os.getenv("RAILWAY_EXECUTION_ENABLED", "true").lower() == "true"
# Compatibility alias for older modules. Execution is local to Railway.
REMOTE_EXECUTION_ENABLED = RAILWAY_EXECUTION_ENABLED
REMOTE_MAX_USDC = Decimal(os.getenv("REMOTE_MAX_USDC", "5"))
EXECUTION_BACKEND = "railway"
EXECUTOR_QUEUE_FILE = core.DATA_DIR / "termux_executor_queue.json"
EXECUTOR_STATE_FILE = core.DATA_DIR / "termux_executor_state.json"
_QUEUE_LOCK = threading.Lock()
PAIR_TTL_SECONDS = 1800
LEASE_SECONDS = 20
CONNECTED_SECONDS = 60
EXECUTOR_BUY_TTL_SECONDS = max(
    30,
    int(
        os.getenv(
            "EXECUTOR_BUY_TTL_SECONDS",
            str(getattr(ingest, "SLACK_MAX_ALERT_AGE_SECONDS", 180)),
        )
    ),
)
MAX_QUEUE_ITEMS = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _state() -> dict[str, Any]:
    return core._load(EXECUTOR_STATE_FILE)


def _save_state(state: dict[str, Any]) -> None:
    core._save(EXECUTOR_STATE_FILE, state)


def _ensure_pair_code() -> tuple[str | None, dict[str, Any]]:
    state = _state()
    if state.get("token_hash"):
        return None, state
    now = time.time()
    if not state.get("pair_code") or float(state.get("pair_expires_unix") or 0) <= now:
        state["pair_code"] = f"{secrets.randbelow(1_000_000):06d}"
        state["pair_expires_unix"] = now + PAIR_TTL_SECONDS
        state["pair_created_at"] = _now_iso()
        _save_state(state)
    return str(state["pair_code"]), state


class PairRequest(BaseModel):
    code: str = Field(min_length=6, max_length=12)
    name: str = Field(default="termux", min_length=1, max_length=80)


class ExecutorResult(BaseModel):
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = Field(default=None, max_length=2000)


class Heartbeat(BaseModel):
    name: str = Field(default="termux", min_length=1, max_length=80)
    wallet: str | None = Field(default=None, max_length=120)
    wallet_type: str | None = Field(default=None, max_length=80)
    usdc_balance: str | None = Field(default=None, max_length=80)
    portfolio_value: str | None = Field(default=None, max_length=80)
    geo_country: str | None = Field(default=None, max_length=20)
    geo_region: str | None = Field(default=None, max_length=40)
    geo_blocked: bool | None = None
    status: str | None = Field(default=None, max_length=300)


def _executor_auth(authorization: str = Header(default="")) -> dict[str, Any]:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Executor bearer token required")
    token = authorization[7:].strip()
    state = _state()
    expected = str(state.get("token_hash") or "")
    if not token or not expected or not secrets.compare_digest(_token_hash(token), expected):
        raise HTTPException(status_code=401, detail="Invalid executor token")
    state["last_seen_unix"] = time.time()
    state["last_seen_at"] = _now_iso()
    _save_state(state)
    return state


@app.post("/api/executor/pair")
def executor_pair(req: PairRequest):
    raise HTTPException(
        status_code=410,
        detail="Remote executor pairing is retired; Railway executes orders directly",
    )


@app.post("/api/executor/unpair", dependencies=[Depends(dashboard._auth)])
def executor_unpair():
    raise HTTPException(
        status_code=410,
        detail="Remote executor pairing is retired; Railway executes orders directly",
    )


@app.get("/api/executor/status", dependencies=[Depends(dashboard._auth)])
def executor_status():
    state = _railway_execution_status()
    return {
        **state,
        "pair_code": None,
        "pair_expires_unix": None,
        "last_seen_at": _now_iso(),
        "wallet_type": "RAILWAY_SECURE_CLIENT" if state.get("configured") else None,
        "usdc_balance": None,
        "portfolio_value": None,
        "worker_status": state.get("error") or ("ready" if state.get("ready") else "not ready"),
        "remote_max_usdc": str(core.MAX_AUTO_TRADE_USDC),
        "railway_live_trading": core.live_trading_enabled(),
    }


@app.post("/api/executor/heartbeat")
def executor_heartbeat(req: Heartbeat):
    raise HTTPException(
        status_code=410,
        detail="Remote executor heartbeat is retired; Railway executes orders directly",
    )


@app.get("/api/executor/wallet", dependencies=[Depends(dashboard._auth)])
def executor_wallet():
    state = _railway_execution_status()
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    balance = None
    portfolio = None
    wallet_type = None
    error = state.get("error")
    if state.get("configured"):
        try:
            with live_trading._secure_client() as client:
                allowance = client.get_balance_allowance(asset_type="COLLATERAL")
                balance = str(
                    (Decimal(str(allowance.balance)) / Decimal("1000000")).quantize(
                        Decimal("0.01")
                    )
                )
                wallet_type = str(client.wallet_type)
                try:
                    pv = client.get_portfolio_value()
                    portfolio = str(
                        Decimal(str(getattr(pv, "value", "0") or "0")).quantize(
                            Decimal("0.01")
                        )
                    )
                except Exception:
                    portfolio = None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"

    return {
        "ok": bool(state.get("ready") and not error),
        "connected": bool(state.get("ready") and not error),
        "backend": EXECUTION_BACKEND,
        "wallet": live_trading._mask_wallet(wallet) if wallet else "Railway wallet not configured",
        "wallet_type": wallet_type or "RAILWAY_SECURE_CLIENT",
        "usdc_balance": balance or "0",
        "portfolio_value": portfolio,
        "live_trading": bool(state.get("ready")),
        "auto_trading": core.auto_trading_enabled(),
        "slack_paper_only": ingest.SLACK_PAPER_ONLY,
        "error": error,
    }


def _authoritative_position(asset_id: str):
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if not wallet or not asset_id:
        return None
    try:
        with PublicClient() as client:
            for position in client.list_positions(user=wallet, page_size=100).iter_items():
                if str(getattr(position, "asset_id", "")) == str(asset_id):
                    return position
    except Exception:
        return None
    return None


def _queue_load() -> dict[str, Any]:
    return core._load(EXECUTOR_QUEUE_FILE)


def _queue_save(data: dict[str, Any]) -> None:
    if len(data) > MAX_QUEUE_ITEMS:
        ordered = sorted(data.items(), key=lambda kv: kv[1].get("created_unix", 0))
        data = dict(ordered[-MAX_QUEUE_ITEMS:])
    core._save(EXECUTOR_QUEUE_FILE, data)


def _expire_stale_buys(
    data: dict[str, Any],
    *,
    now: float | None = None,
) -> list[str]:
    """Fail stale BUYs before they can be picked up or retried late."""
    current = time.time() if now is None else float(now)
    expired_ids: list[str] = []

    for rec in data.values():
        if rec.get("action") != "BUY":
            continue

        status = str(rec.get("status") or "")
        if status not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
            continue

        created = float(rec.get("created_unix") or 0)
        if not created or current - created <= EXECUTOR_BUY_TTL_SECONDS:
            continue

        lease_until = float(rec.get("lease_until_unix") or 0)

        # Do not invalidate a BUY that is still actively leased.
        if status == "LEASED" and lease_until > current:
            continue

        stamp = _now_iso()
        rec["status"] = "FAILED"
        rec["expired_at"] = stamp
        rec["updated_at"] = stamp
        rec.pop("lease_until_unix", None)

        if status in {"WAITING_APPROVAL", "PENDING"}:
            rec["error"] = (
                f"BUY expired after {EXECUTOR_BUY_TTL_SECONDS}s before Railway execution; "
                "no order was submitted"
            )
        else:
            rec["error"] = (
                f"BUY executor lease expired after {EXECUTOR_BUY_TTL_SECONDS}s without a result; "
                "automatic late retry was blocked. Reconcile the wallet before retrying"
            )

        expired_ids.append(str(rec.get("id") or ""))

    return [request_id for request_id in expired_ids if request_id]


def _expire_stale_buys_persisted() -> list[str]:
    with _QUEUE_LOCK:
        data = _queue_load()
        expired = _expire_stale_buys(data)
        if expired:
            _queue_save(data)

    if expired:
        print(
            f"EXECUTOR_EXPIRED_BUYS count={len(expired)} ids={','.join(expired)}",
            flush=True,
        )

    return expired


def _railway_execution_status(*, refresh_geo: bool = False) -> dict[str, Any]:
    private_key = bool(os.getenv("POLYMARKET_PRIVATE_KEY", "").strip())
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    configured = bool(private_key and wallet)

    cache = getattr(_railway_execution_status, "_cache", None)
    now = time.time()
    if not isinstance(cache, dict):
        cache = {
            "checked_unix": 0.0,
            "geo_country": None,
            "geo_region": None,
            "geo_blocked": None,
            "error": None,
        }

    if configured and (refresh_geo or now - float(cache.get("checked_unix") or 0) >= 30):
        try:
            geo = core._check_geoblock()
            cache.update({
                "checked_unix": now,
                "geo_country": geo.get("country"),
                "geo_region": geo.get("region"),
                "geo_blocked": bool(geo.get("blocked")),
                "error": None,
            })
        except HTTPException as exc:
            cache.update({
                "checked_unix": now,
                "geo_blocked": exc.status_code == 451,
                "error": str(exc.detail),
            })
        except Exception as exc:
            cache.update({
                "checked_unix": now,
                "geo_blocked": None,
                "error": f"{type(exc).__name__}: {exc}",
            })

    _railway_execution_status._cache = cache
    ready = bool(
        RAILWAY_EXECUTION_ENABLED
        and core.live_trading_enabled()
        and configured
        and cache.get("geo_blocked") is False
        and not cache.get("error")
    )
    return {
        "backend": EXECUTION_BACKEND,
        "enabled": RAILWAY_EXECUTION_ENABLED,
        "configured": configured,
        "ready": ready,
        "connected": ready,
        "paired": True,
        "worker_name": "railway-direct",
        "wallet": live_trading._mask_wallet(wallet) if wallet else None,
        "geo_country": cache.get("geo_country"),
        "geo_region": cache.get("geo_region"),
        "geo_blocked": cache.get("geo_blocked"),
        "error": cache.get("error"),
    }


def _validate_direct_buy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if core._norm(str(payload.get("market_type") or "")) != "moneyline":
        raise RuntimeError("Railway executor is hard-locked to moneyline only")

    budget = Decimal(str(payload.get("budget_usdc") or "0"))
    max_price = Decimal(str(payload.get("max_price") or "0"))
    max_spread = min(
        Decimal(str(payload.get("max_spread") or core.MAX_SPREAD)),
        core.MAX_SPREAD,
    )
    if budget <= 0 or budget > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            f"BUY budget must be greater than 0 and no more than {core.MAX_AUTO_TRADE_USDC} USDC"
        )
    if max_price <= 0 or max_price >= 1 or max_price > core.MAX_PRICE:
        raise RuntimeError(f"Invalid max price {max_price}")

    market_url = str(payload.get("market_url") or "")
    outcome = str(payload.get("outcome") or "").strip()
    if core._sports_event_slug(market_url) is None:
        raise RuntimeError("Railway executor accepts only Polymarket /sports/ event URLs")
    if not outcome:
        raise RuntimeError("Moneyline outcome/team is required")

    # Fail closed on Polymarket's location check from Railway itself.
    core._check_geoblock()

    intent = core.TradeIntent(
        market_url=market_url,
        outcome=outcome,
        market_type="moneyline",
        max_price=max_price,
        budget_usdc=budget,
        note="Railway direct live execution",
    )
    if payload.get("auto"):
        core._check_daily_budget(intent)

    with PublicClient() as client:
        market = core._select_market(client, intent)
        actual_type = core._norm(core._market_type(market))
        if actual_type != "moneyline":
            raise RuntimeError(
                f"Resolved sports market type is '{core._market_type(market)}', not moneyline"
            )
        asset_id, _, outcome_label = core._resolve_asset(market, outcome)
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        book = client.get_order_book(asset_id=asset_id)
        min_size = Decimal(
            str(getattr(getattr(market, "trading", None), "minimum_order_size", "0") or "0")
        )

    if buy_price <= 0 or buy_price >= 1:
        raise RuntimeError(f"Invalid current BUY price {buy_price}")
    if buy_price > max_price:
        raise RuntimeError(
            f"Current BUY price {buy_price} exceeds your maximum price {max_price}"
        )
    if spread > max_spread:
        raise RuntimeError(f"Current spread {spread} exceeds maximum spread {max_spread}")

    asks = getattr(book, "asks", None) or []
    best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    if best_ask is None or best_ask > max_price:
        raise RuntimeError("Selected limit would not cross the current best ask")

    shares = (budget / max_price).quantize(
        Decimal("0.0001"), rounding=live_trading.ROUND_DOWN
    )
    if min_size > 0 and shares < min_size:
        raise RuntimeError(
            f"Order is below this market's minimum size of {min_size} shares"
        )

    return {
        "intent": intent,
        "market": str(
            getattr(market, "question", None)
            or getattr(market, "slug", "sports market")
        ),
        "market_type": core._market_type(market),
        "outcome": outcome_label,
        "asset_id": str(asset_id),
        "current_buy_price": str(buy_price),
        "spread": str(spread),
        "best_ask": str(best_ask),
        "requested_shares": str(shares),
        "budget_usdc": str(budget),
        "max_price": str(max_price),
    }


def _preview_direct(payload: dict[str, Any]) -> dict[str, Any]:
    quote = _validate_direct_buy_payload(payload)
    quote.pop("intent", None)
    quote.update({
        "ok": True,
        "executor": EXECUTION_BACKEND,
        "no_order_placed": True,
    })
    return quote


def _buy_direct(payload: dict[str, Any]) -> dict[str, Any]:
    quote = _validate_direct_buy_payload(payload)
    quote.pop("intent", None)
    asset_id = quote["asset_id"]
    shares = Decimal(quote["requested_shares"])
    max_price = Decimal(quote["max_price"])
    _, wallet = live_trading._credentials()
    before = live_trading._position_size(wallet, asset_id)

    order_id = None
    with live_trading._secure_client() as client:
        response = client.place_limit_order(
            token_id=asset_id,
            price=str(max_price),
            size=str(shares),
            side="BUY",
        )
        order_id = str(getattr(response, "order_id", "") or "")
        deadline = time.time() + live_trading.LIVE_TEST_FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = live_trading._position_size(wallet, asset_id)
            if after > before:
                break
        live_trading._cancel_quietly(client, order_id)

    filled = max(Decimal("0"), after - before)
    result = dict(quote)
    result.update({
        "ok": filled > 0,
        "status": "ORDER_SUBMITTED" if filled > 0 else "TEST_BUY_UNFILLED_CANCELED",
        "order_id": order_id,
        "filled_shares": str(filled),
        "position_before": str(before),
        "position_after": str(after),
        "entry_price": str(max_price),
        "canceled_remainder": True,
        "trade_id": payload.get("trade_id"),
        "executor": EXECUTION_BACKEND,
    })
    return result


def _sell_direct(payload: dict[str, Any]) -> dict[str, Any]:
    core._check_geoblock()
    asset_id = str(payload.get("asset_id") or "")
    target = Decimal(str(payload.get("shares") or "0"))
    entry = Decimal(str(payload.get("entry_price") or "0"))
    if not asset_id or target <= 0:
        raise RuntimeError("SELL request has no tracked asset/shares")

    _, wallet = live_trading._credentials()
    before = live_trading._position_size(wallet, asset_id)
    sell_size = min(before, target)
    if sell_size <= 0:
        raise RuntimeError("Wallet no longer holds the tracked test shares")

    with PublicClient() as public:
        sell_price = Decimal(str(public.get_price(asset_id=asset_id, side="SELL")))
    if sell_price <= 0 or sell_price >= 1:
        raise RuntimeError(f"Invalid current SELL price {sell_price}")

    order_id = None
    with live_trading._secure_client() as client:
        response = client.place_limit_order(
            token_id=asset_id,
            price=str(sell_price),
            size=str(sell_size),
            side="SELL",
        )
        order_id = str(getattr(response, "order_id", "") or "")
        deadline = time.time() + live_trading.LIVE_TEST_FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = live_trading._position_size(wallet, asset_id)
            if after < before:
                break
        live_trading._cancel_quietly(client, order_id)

    sold = max(Decimal("0"), before - after)
    if sold <= 0:
        raise RuntimeError("SELL did not fill; remainder was canceled")

    remaining = max(Decimal("0"), target - sold)
    realized = ((sell_price - entry) * sold).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "status": "CLOSED" if remaining <= Decimal("0.0001") else "PARTIALLY_CLOSED",
        "trade_id": payload.get("trade_id"),
        "order_id": order_id,
        "sold_shares": str(sold),
        "remaining_shares": str(remaining),
        "sell_price": str(sell_price),
        "realized_pnl": str(realized),
        "executor": EXECUTION_BACKEND,
    }


def _execute_existing(request_id: str, *, approved: bool = False) -> dict[str, Any]:
    with _QUEUE_LOCK:
        data = _queue_load()
        rec = data.get(request_id)
        if not rec:
            raise HTTPException(status_code=404, detail="Unknown executor request")
        if rec.get("status") in {"DONE", "FAILED", "CANCELLED"}:
            return rec

        created = float(rec.get("created_unix") or 0)
        if (
            rec.get("action") == "BUY"
            and created
            and time.time() - created > EXECUTOR_BUY_TTL_SECONDS
        ):
            rec["status"] = "FAILED"
            rec["expired_at"] = _now_iso()
            rec["updated_at"] = _now_iso()
            rec["error"] = (
                f"BUY expired after {EXECUTOR_BUY_TTL_SECONDS}s before Railway execution; "
                "no order was submitted"
            )
            data[request_id] = rec
            _queue_save(data)
            return rec

        rec["status"] = "RUNNING"
        rec["updated_at"] = _now_iso()
        data[request_id] = rec
        _queue_save(data)

    action = str(rec.get("action") or "")
    payload = rec.get("payload") or {}
    try:
        if not RAILWAY_EXECUTION_ENABLED:
            raise RuntimeError("Railway execution is disabled")
        if action in {"BUY", "SELL"} and not core.live_trading_enabled():
            raise RuntimeError("LIVE_TRADING is disabled")
        if (
            action == "BUY"
            and payload.get("auto")
            and not approved
            and not core.auto_trading_enabled()
        ):
            raise RuntimeError("AUTO_TRADING is disabled")

        if action == "PREVIEW":
            result = _preview_direct(payload)
        elif action == "BUY":
            result = _buy_direct(payload)
        elif action == "SELL":
            result = _sell_direct(payload)
        else:
            raise RuntimeError(f"Unsupported executor action {action}")

        rec["status"] = "DONE"
        rec["result"] = result
        rec["error"] = None
    except Exception as exc:
        error = str(getattr(exc, "detail", None) or exc)
        already_closed = bool(
            action == "SELL"
            and "Wallet no longer holds the tracked test shares" in error
        )
        if already_closed:
            result = {
                "ok": True,
                "status": "CLOSED_RECONCILED",
                "already_closed": True,
                "trade_id": payload.get("trade_id"),
                "message": (
                    "Railway found zero tracked wallet shares; dashboard position "
                    "was reconciled closed."
                ),
                "executor": EXECUTION_BACKEND,
            }
            rec["status"] = "DONE"
            rec["result"] = result
            rec["error"] = None
            _record_already_closed_sell(rec, error)
        else:
            rec["status"] = "FAILED"
            rec["result"] = {}
            rec["error"] = error

    rec["updated_at"] = _now_iso()
    with _QUEUE_LOCK:
        data = _queue_load()
        data[request_id] = rec
        _queue_save(data)

    result = rec.get("result") or {}
    if rec.get("status") == "DONE" and action == "BUY" and result.get("ok"):
        _record_buy_result(rec, result)
    elif (
        rec.get("status") == "DONE"
        and action == "SELL"
        and result.get("ok")
        and not result.get("already_closed")
    ):
        _record_sell_result(rec, result)

    event = _executor_event(rec)
    print(
        f"RAILWAY_EXECUTOR_EVENT request={request_id} action={event.get('action')} "
        f"status={event.get('status')} trade={event.get('trade_id')} "
        f"message={event.get('message')}",
        flush=True,
    )
    return rec


def _enqueue(action: str, payload: dict[str, Any]) -> dict[str, Any]:
    req_id = f"exec-{uuid.uuid4().hex[:14]}"
    record = {
        "id": req_id,
        "action": action,
        "status": "PENDING",
        "payload": payload,
        "created_at": _now_iso(),
        "created_unix": time.time(),
        "updated_at": _now_iso(),
    }
    with _QUEUE_LOCK:
        data = _queue_load()
        data[req_id] = record
        _queue_save(data)
    return _execute_existing(req_id)


def _active_sell_request(trade_id: str) -> dict[str, Any] | None:
    """Return an already queued/leased SELL for this trade to prevent duplicate exits."""
    with _QUEUE_LOCK:
        data = _queue_load()
        matches = [
            rec for rec in data.values()
            if rec.get("action") == "SELL"
            and str((rec.get("payload") or {}).get("trade_id") or "") == str(trade_id)
            and rec.get("status") in {"PENDING", "RUNNING"}
        ]
    if not matches:
        return None
    return sorted(matches, key=lambda r: r.get("created_unix", 0), reverse=True)[0]


def _executor_event(rec: dict[str, Any]) -> dict[str, Any]:
    payload = rec.get("payload") or {}
    result = rec.get("result") or {}
    action = str(rec.get("action") or "")
    status = str(rec.get("status") or "")
    message = None
    if status == "PENDING":
        message = f"{action} queued for executor"
    elif status == "LEASED":
        message = f"{action} executing on Railway"
    elif status == "FAILED":
        message = str(rec.get("error") or result.get("message") or f"{action} failed")
    elif status == "DONE":
        if action == "SELL":
            if result.get("already_closed"):
                message = str(result.get("message") or "Position already closed")
            elif result.get("sold_shares"):
                message = (
                    f"SELL confirmed: {result.get('sold_shares')} shares"
                    + (f" @ {result.get('sell_price')}" if result.get("sell_price") else "")
                    + (f" · P/L {result.get('realized_pnl')}" if result.get("realized_pnl") is not None else "")
                )
            else:
                message = str(result.get("message") or "SELL completed")
        elif action == "BUY":
            message = (
                f"BUY confirmed: {result.get('filled_shares')} shares"
                + (f" @ {result.get('entry_price')}" if result.get("entry_price") else "")
            )
        else:
            message = str(result.get("message") or f"{action} completed")
    return {
        "request_id": rec.get("id"),
        "trade_id": payload.get("trade_id"),
        "action": action,
        "status": status,
        "message": message,
        "error": rec.get("error"),
        "created_at": rec.get("created_at"),
        "updated_at": rec.get("updated_at"),
        "result": {
            k: result.get(k)
            for k in (
                "status", "sold_shares", "remaining_shares", "sell_price",
                "realized_pnl", "filled_shares", "entry_price", "order_id",
                "already_closed", "message", "initial_best_bid", "min_sell_price",
                "tick_size",
            )
            if result.get(k) is not None
        },
    }


@app.get("/api/executor/recent-actions", dependencies=[Depends(dashboard._auth)])
def recent_executor_actions(limit: int = 12):
    _expire_stale_buys_persisted()
    limit = max(1, min(50, int(limit)))
    data = _queue_load()
    ordered = sorted(
        data.values(),
        key=lambda r: r.get("created_unix", 0),
        reverse=True,
    )[:limit]
    return {"ok": True, "events": [_executor_event(rec) for rec in ordered]}


def _validate_remote_buy(req: live_trading.LiveTestBuy) -> None:
    if core._norm(req.market_type) != "moneyline":
        raise HTTPException(status_code=400, detail="Remote BUY is locked to moneyline only")
    if req.budget_usdc > core.MAX_AUTO_TRADE_USDC:
        raise HTTPException(status_code=400, detail=f"Remote amount exceeds dashboard Auto trade cap ${core.MAX_AUTO_TRADE_USDC}")
    if req.max_price > core.MAX_PRICE:
        raise HTTPException(status_code=400, detail=f"Maximum price exceeds MAX_PRICE={core.MAX_PRICE}")
    if core._sports_event_slug(str(req.market_url)) is None:
        raise HTTPException(status_code=400, detail="Use a Polymarket /sports/ event URL")


def _buy_payload(req: live_trading.LiveTestBuy, trade_id: str | None = None) -> dict[str, Any]:
    return {
        "market_url": str(req.market_url),
        "outcome": req.outcome,
        "market_type": "moneyline",
        "max_price": str(req.max_price),
        "budget_usdc": str(req.budget_usdc),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
    }


@app.post("/api/executor/request-preview", dependencies=[Depends(dashboard._auth)])
def request_preview(req: live_trading.LiveTestBuy):
    _validate_remote_buy(req)
    rec = _enqueue("PREVIEW", _buy_payload(req))
    return {"ok": True, "queued": True, "request_id": rec["id"]}


@app.post("/api/executor/request-buy", dependencies=[Depends(dashboard._auth)])
def request_buy(req: live_trading.LiveTestBuy):
    _validate_remote_buy(req)
    trade_id = f"live-test-railway-{uuid.uuid4().hex[:10]}"
    rec = _enqueue("BUY", _buy_payload(req, trade_id=trade_id))
    return {"ok": True, "queued": True, "request_id": rec["id"], "trade_id": trade_id}


@app.post("/api/executor/request-sell/{trade_id}", dependencies=[Depends(dashboard._auth)])
def request_sell(trade_id: str):
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown remote test trade")
    if rec.get("source") not in {"railway_executor", "termux_executor", "slack_live"} or rec.get("paper"):
        raise HTTPException(status_code=400, detail="Only tracked live positions can be sold here")
    if rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
        raise HTTPException(status_code=400, detail=f"Trade status is {rec.get('status')}")
    q = rec.get("quote") or {}
    asset_id = str(q.get("asset_id") or "")
    shares = str(rec.get("remaining_shares") or rec.get("filled_shares") or q.get("shares") or "0")
    entry_price = str(q.get("entry_price") or q.get("limit_price") or "0")

    # If this bot opened the wallet position from zero shares, Polymarket's
    # wallet-position average price and entry cost are authoritative.
    position = _authoritative_position(asset_id)
    pre_size = Decimal(str(rec.get("pre_position_size") or "0"))
    if position is not None and pre_size <= Decimal("0.0001"):
        pos_entry = Decimal(str(getattr(position, "avg_price", "0") or "0"))
        pos_cost = Decimal(str(getattr(position, "entry_cost_usdc", "0") or "0"))
        if pos_entry > 0:
            entry_price = str(pos_entry)
            q["entry_price"] = entry_price
            rec["quote"] = q
        if pos_cost > 0:
            rec["budget_usdc"] = str(pos_cost.quantize(Decimal("0.01")))
        executions[trade_id] = rec
        core._save(core.EXECUTIONS_FILE, executions)

    payload = {
        "trade_id": trade_id,
        "asset_id": asset_id,
        "shares": shares,
        "entry_price": entry_price,
        "market": q.get("market"),
        "market_url": q.get("market_url"),
        "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
    }
    if not payload["asset_id"] or Decimal(shares) <= 0:
        raise HTTPException(status_code=400, detail="Tracked trade has no sellable shares")
    existing = _active_sell_request(trade_id)
    if existing is not None:
        return {
            "ok": True,
            "queued": True,
            "duplicate_prevented": True,
            "request_id": existing["id"],
            "trade_id": trade_id,
        }
    queued = _enqueue("SELL", payload)
    return {"ok": True, "queued": True, "request_id": queued["id"], "trade_id": trade_id}


@app.get("/api/executor/request-status/{request_id}", dependencies=[Depends(dashboard._auth)])
def request_status(request_id: str):
    _expire_stale_buys_persisted()
    rec = _queue_load().get(request_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown executor request")
    return {
        "request_id": request_id,
        "action": rec.get("action"),
        "status": rec.get("status"),
        "result": rec.get("result"),
        "error": rec.get("error"),
        "updated_at": rec.get("updated_at"),
    }


@app.get("/api/executor/next")
def executor_next():
    raise HTTPException(
        status_code=410,
        detail="Remote executor polling is retired; Railway executes orders directly",
    )


def _record_buy_result(queue_rec: dict[str, Any], result: dict[str, Any]) -> None:
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id or not result.get("ok"):
        return
    filled = Decimal(str(result.get("filled_shares") or "0"))
    if filled <= 0:
        return
    now = _now_iso()
    entry_price = str(result.get("entry_price") or payload.get("max_price") or "0")
    estimated_cost = (filled * Decimal(entry_price)).quantize(Decimal("0.01"))
    record = {
        "id": trade_id,
        "status": "ORDER_SUBMITTED",
        "created_at": now,
        "submitted_at": now,
        "side": "BUY",
        "paper": False,
        "source": str(payload.get("source") or "railway_executor"),
        "budget_usdc": str(estimated_cost),
        "slack_event_id": payload.get("slack_event_id"),
        "auto": bool(payload.get("auto", False)),
        "filled_shares": str(filled),
        "pre_position_size": str(result.get("position_before") or "0"),
        "quote": {
            "market": result.get("market"),
            "market_url": payload.get("market_url"),
            "market_type": "moneyline",
            "requested_outcome": payload.get("outcome"),
            "resolved_outcome": result.get("outcome") or payload.get("outcome"),
            "asset_id": str(result.get("asset_id") or ""),
            "limit_price": str(payload.get("max_price")),
            "entry_price": entry_price,
            "current_buy_price": str(result.get("current_buy_price") or ""),
            "spread": str(result.get("spread") or ""),
            "shares": str(filled),
        },
        "execution": {
            "placed": True,
            "order_id": result.get("order_id"),
            "canceled_remainder": bool(result.get("canceled_remainder", True)),
            "executor": "railway",
        },
    }
    executions = core._load(core.EXECUTIONS_FILE)
    executions[trade_id] = record
    core._save(core.EXECUTIONS_FILE, executions)


def _record_sell_result(queue_rec: dict[str, Any], result: dict[str, Any]) -> None:
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id or not result.get("ok"):
        return
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        return
    q = rec.get("quote") or {}
    sold = Decimal(str(result.get("sold_shares") or "0"))
    remaining = Decimal(str(result.get("remaining_shares") or "0"))
    sell_price = Decimal(str(result.get("sell_price") or "0"))
    entry = Decimal(str(payload.get("entry_price") or q.get("entry_price") or q.get("limit_price") or "0"))
    realized = Decimal(str(result.get("realized_pnl") or ((sell_price - entry) * sold).quantize(Decimal("0.01"))))
    now = _now_iso()
    rec["status"] = "CLOSED" if remaining <= Decimal("0.0001") else "PARTIALLY_CLOSED"
    rec["exit_price"] = str(sell_price)
    rec["realized_pnl"] = str(realized)
    rec["remaining_shares"] = str(remaining)
    rec["closed_at"] = now if rec["status"] == "CLOSED" else None
    rec.setdefault("execution", {})["close_order_id"] = result.get("order_id")
    sell_id = f"{trade_id}-sell-{uuid.uuid4().hex[:6]}"
    executions[trade_id] = rec
    executions[sell_id] = {
        "id": sell_id,
        "status": "SELL_FILLED",
        "created_at": now,
        "submitted_at": now,
        "side": "SELL",
        "paper": False,
        "source": str(rec.get("source") or "railway_executor"),
        "parent_trade_id": trade_id,
        "display_pnl": str(realized),
        "budget_usdc": str((sell_price * sold).quantize(Decimal("0.01"))),
        "quote": {
            "market": q.get("market"),
            "market_url": q.get("market_url"),
            "market_type": "moneyline",
            "requested_outcome": q.get("requested_outcome"),
            "resolved_outcome": q.get("resolved_outcome"),
            "asset_id": q.get("asset_id"),
            "limit_price": str(sell_price),
            "shares": str(sold),
        },
        "execution": {"placed": True, "order_id": result.get("order_id"), "executor": "termux"},
    }
    core._save(core.EXECUTIONS_FILE, executions)


def _record_already_closed_sell(queue_rec: dict[str, Any], error: str) -> bool:
    """Reconcile a tracked trade when Railway confirms the wallet already has zero shares."""
    marker = "Wallet no longer holds the tracked test shares"
    if marker not in str(error or ""):
        return False
    payload = queue_rec.get("payload") or {}
    trade_id = str(payload.get("trade_id") or "")
    if not trade_id:
        return False
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec or rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
        return False
    now = _now_iso()
    rec["status"] = "CLOSED_RECONCILED"
    rec["remaining_shares"] = "0"
    rec["closed_at"] = rec.get("closed_at") or now
    rec["wallet_reconciled_at"] = now
    rec["wallet_position_size"] = "0"
    rec["reconciliation_note"] = (
        "Railway checked the Polymarket wallet before SELL and found zero tracked shares. "
        "The position was already closed outside this SELL request; exit price and realized P/L remain unknown."
    )
    rec.setdefault("execution", {})["close_reconciliation"] = {
        "at": now,
        "executor": "railway",
        "reason": marker,
    }
    executions[trade_id] = rec
    core._save(core.EXECUTIONS_FILE, executions)
    return True


@app.post("/api/executor/result/{request_id}")
def executor_result(request_id: str, body: ExecutorResult):
    raise HTTPException(
        status_code=410,
        detail="Remote executor result callbacks are retired; Railway executes orders directly",
    )


def _install_remote_executor_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if "RAILWAY EXECUTION" in html:
        return

    html = html.replace("fetch('/api/live/wallet'", "fetch('/api/executor/wallet'")

    status_box = '''
      <div class="executor-box">
        <div><b>RAILWAY EXECUTION</b> · <span id="executorConnection">Checking…</span></div>
        <div id="executorPairLine" class="executor-pair"></div>
        <div id="executorGeo" class="executor-small"></div>
      </div>
'''
    html = html.replace('<div class="live-test-warning">', status_box + '<div class="live-test-warning">', 1)

    css = '''
.executor-box{border:1px solid var(--border);background:#0b1320;border-radius:12px;padding:12px;margin:12px 0;font-size:12px}.executor-pair{margin-top:7px;font:700 14px ui-monospace,SFMono-Regular,Menlo,monospace}.executor-small{margin-top:5px;color:var(--muted);font-size:11px}.executor-online{color:var(--accent)}.executor-offline{color:var(--bad)}
'''
    html = html.replace('</style>', css + '</style>', 1)

    js = r'''
let remoteExecutorConnected=false;
async function remoteStatus(){
 try{
  const r=await fetch('/api/executor/status',{cache:'no-store'}),d=await r.json();
  remoteExecutorConnected=!!d.ready;
  const c=document.getElementById('executorConnection'),p=document.getElementById('executorPairLine'),g=document.getElementById('executorGeo');
  if(c){c.textContent=d.ready?'READY':'NOT READY';c.className=d.ready?'executor-online':'executor-offline'}
  if(p){p.textContent='Direct Railway SecureClient · no phone/Termux worker required'}
  if(g){g.textContent=[d.geo_country?('Network '+d.geo_country+(d.geo_region?'/'+d.geo_region:'')):'',d.geo_blocked===true?'POLYMARKET BLOCKED':d.geo_blocked===false?'Polymarket geoblock passed':'',d.error||''].filter(Boolean).join(' · ')}
 }catch(e){const c=document.getElementById('executorConnection');if(c){c.textContent='STATUS ERROR';c.className='executor-offline'}}
}
async function remoteRequest(path,payload){
 const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:payload===undefined?'{}':JSON.stringify(payload)});return await ltJson(r)
}
async function waitRemote(requestId,timeoutMs=90000){
 const started=Date.now();
 while(Date.now()-started<timeoutMs){
  const r=await fetch('/api/executor/request-status/'+encodeURIComponent(requestId),{cache:'no-store'}),d=await ltJson(r);
  if(d.status==='DONE')return d.result||{};
  if(d.status==='FAILED')throw new Error(d.error||'Railway execution failed');
  await new Promise(res=>setTimeout(res,250));
 }
 throw new Error('Timed out waiting for Railway execution.');
}
function replaceLiveButton(id,handler){const old=document.getElementById(id);if(!old)return;const neo=old.cloneNode(true);old.parentNode.replaceChild(neo,old);neo.addEventListener('click',handler)}
async function remotePreview(){
 try{ltEl('ltMsg').textContent='Checking market from Railway…';const q=await remoteRequest('/api/executor/request-preview',ltPayload());const d=await waitRemote(q.request_id);ltShow(d);ltEl('ltMsg').textContent='Railway market check passed. No order placed.'}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Railway market check failed.'}
}
async function remoteBuy(){
 try{
  const p=ltPayload();if(!remoteExecutorConnected)throw new Error('Railway direct execution is not ready');
  if(!confirm('Place a REAL $'+Number(p.budget_usdc).toFixed(2)+' moneyline limit BUY for '+p.outcome+' directly from Railway?'))return;
  ltEl('ltBuy').disabled=true;ltEl('ltMsg').textContent='Executing Railway BUY…';const q=await remoteRequest('/api/executor/request-buy',p);const d=await waitRemote(q.request_id);ltShow(d);
  if(d.ok&&q.trade_id){liveTestTradeId=q.trade_id;ltEl('ltSell').disabled=false;ltEl('ltMsg').textContent='Railway BUY filled. SELL is ready.'}else{ltEl('ltMsg').textContent='BUY did not fill; any remainder was canceled.'}
 }catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Railway BUY failed.'}
 finally{ltEl('ltBuy').disabled=false}
}
async function remoteSell(){
 if(!liveTestTradeId){ltShow('No filled Railway test trade is available to sell.');return}
 if(!remoteExecutorConnected){ltShow('Railway direct execution is not ready.');return}
 if(!confirm('SELL the tracked live shares directly from Railway?'))return;
 try{ltEl('ltSell').disabled=true;ltEl('ltMsg').textContent='Executing Railway SELL…';const q=await remoteRequest('/api/executor/request-sell/'+encodeURIComponent(liveTestTradeId));const d=await waitRemote(q.request_id);ltShow(d);ltEl('ltMsg').textContent='Railway SELL completed. Round-trip test finished.';liveTestTradeId=null}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Railway SELL failed; verify the position before retrying.';ltEl('ltSell').disabled=false}
}
replaceLiveButton('ltPreview',remotePreview);replaceLiveButton('ltBuy',remoteBuy);replaceLiveButton('ltSell',remoteSell);
remoteStatus();setInterval(remoteStatus,10000);
'''
    html = html.replace('</script>', js + '\n</script>', 1)
    dashboard.DASHBOARD_HTML = html


_expire_stale_buys_persisted()
_install_remote_executor_ui()
print(
    "RAILWAY_EXECUTOR backend=direct auto_cap_usdc=" + str(core.MAX_AUTO_TRADE_USDC),
    flush=True,
)
