#!/usr/bin/env python3
from __future__ import annotations

import getpass
import json
import os
import sys
import time
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = Path(os.getenv("EXECUTOR_ENV_FILE", str(Path.home() / ".polymarket_executor.env"))).expanduser()
load_dotenv(ENV_FILE)
sys.path.insert(0, str(ROOT))

from polymarket import PublicClient, SecureClient  # noqa: E402
from app import main as core  # noqa: E402

BRIDGE_URL = os.getenv("EXECUTOR_BRIDGE_URL", "https://polymarket-auto-bot-production.up.railway.app").rstrip("/")
WORKER_NAME = os.getenv("EXECUTOR_NAME", "termux-phone")
MAX_USDC = Decimal(os.getenv("EXECUTOR_MAX_USDC", "25"))
FILL_WAIT_SECONDS = max(3, min(15, int(os.getenv("EXECUTOR_FILL_WAIT_SECONDS", "8"))))
TOKEN_FILE = Path(os.getenv("EXECUTOR_TOKEN_FILE", str(Path.home() / ".config/polymarket-termux/executor_token"))).expanduser()
JOURNAL_FILE = Path(os.getenv("EXECUTOR_JOURNAL_FILE", str(Path.home() / ".config/polymarket-termux/executor_journal.json"))).expanduser()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, default=str))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _mask(value: str) -> str:
    if len(value) < 12:
        return value
    return value[:6] + "…" + value[-4:]


def _credentials() -> tuple[str, str]:
    private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "").strip()
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if not private_key:
        private_key = getpass.getpass("Polymarket private key (hidden, not saved): ").strip()
    if not wallet:
        wallet = input("Polymarket deposit/funder wallet (0x…): ").strip()
    if not private_key or not wallet:
        raise RuntimeError("Both POLYMARKET_PRIVATE_KEY and POLYMARKET_DEPOSIT_WALLET are required")
    return private_key, wallet


def _load_token() -> str | None:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        return token or None
    return None


def _save_token(token: str) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token)
    os.chmod(TOKEN_FILE, 0o600)


def _pair() -> str:
    code = os.getenv("EXECUTOR_PAIR_CODE", "").strip() or input("Pair code from Railway dashboard: ").strip()
    with httpx.Client(timeout=15) as h:
        r = h.post(f"{BRIDGE_URL}/api/executor/pair", json={"code": code, "name": WORKER_NAME})
        r.raise_for_status()
        token = str(r.json()["token"])
    _save_token(token)
    print("Paired with Railway executor bridge.")
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _geo() -> dict[str, Any]:
    # Some mobile networks advertise IPv6 for polymarket.com even when that route
    # is unusable. Bind the HTTP transport to IPv4 so the geoblock check follows
    # the same working path as `curl -4`.
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
    with httpx.Client(transport=transport, timeout=12) as h:
        r = h.get("https://polymarket.com/api/geoblock")
        r.raise_for_status()
        geo = r.json()
    if geo.get("blocked"):
        raise RuntimeError(
            f"Polymarket geoblock reports this phone network is blocked ({geo.get('country')}/{geo.get('region')}). "
            "Executor will not place trades."
        )
    return geo


def _position_size(wallet: str, asset_id: str) -> Decimal:
    with PublicClient() as client:
        positions = client.list_positions(user=wallet, page_size=100).iter_items()
        for position in positions:
            if str(getattr(position, "asset_id", "")) == str(asset_id):
                return Decimal(str(getattr(position, "current_size", "0") or "0"))
    return Decimal("0")


def _cancel_quietly(client: SecureClient, order_id: str | None) -> None:
    if not order_id:
        return
    try:
        client.cancel_order(order_id=order_id)
    except Exception:
        pass


def _secure(private_key: str, wallet: str) -> SecureClient:
    return SecureClient.create(private_key=private_key, wallet=wallet)


def _asset_market(client: PublicClient, asset_id: str) -> tuple[Any, str]:
    markets = list(
        client.list_markets(
            clob_token_ids=[asset_id],
            closed=False,
            page_size=5,
        ).iter_items()
    )
    for market in markets:
        outcomes = getattr(market, "outcomes", None)
        for outcome in (
            getattr(outcomes, "yes", None),
            getattr(outcomes, "no", None),
        ):
            if outcome is None:
                continue
            token_id = str(
                getattr(outcome, "token_id", None)
                or getattr(outcome, "position_id", None)
                or ""
            )
            if token_id == asset_id:
                return market, str(getattr(outcome, "label", "") or "")
    raise RuntimeError("Exact Polymarket outcome token is no longer an open tradable market")


def _canonical_market_type(value: Any) -> str:
    raw = core._norm(str(value or ""))
    if raw in {"moneyline", "spread", "total"}:
        return raw
    if "moneyline" in raw or "money line" in raw or "winner" in raw:
        return "moneyline"
    if "spread" in raw or "handicap" in raw:
        return "spread"
    if "total" in raw or "over/under" in raw or "over under" in raw:
        return "total"
    return raw


def _validate_buy(payload: dict[str, Any]) -> dict[str, Any]:
    market_type = _canonical_market_type(payload.get("market_type"))
    if market_type not in {"moneyline", "spread", "total"}:
        raise RuntimeError("Executor supports only moneyline, spread, and total game markets")

    budget = Decimal(str(payload.get("budget_usdc") or "0"))
    max_price = Decimal(str(payload.get("max_price") or "0"))
    max_spread = Decimal(str(payload.get("max_spread") or "0.08"))
    max_price_global = Decimal(str(payload.get("max_price_global") or "0.95"))
    if budget <= 0 or budget > MAX_USDC:
        raise RuntimeError(
            "Budget must be greater than $0 and no more than the local executor cap of $"
            + str(MAX_USDC)
        )
    if max_price <= 0 or max_price >= 1 or max_price > max_price_global:
        raise RuntimeError(f"Invalid max price {max_price}")

    market_url = str(payload.get("market_url") or "")
    outcome = str(payload.get("outcome") or "").strip()
    expected_asset_id = str(payload.get("asset_id") or "").strip()
    if core._sports_event_slug(market_url) is None:
        raise RuntimeError("Executor accepts only Polymarket /sports/ event URLs")
    if not outcome:
        raise RuntimeError("Outcome/team is required")
    if market_type != "moneyline" and not expected_asset_id:
        raise RuntimeError("Automated spread/total BUY requires an exact Railway-resolved outcome token")

    _geo()
    with PublicClient() as client:
        if expected_asset_id:
            market, outcome_label = _asset_market(client, expected_asset_id)
            asset_id = expected_asset_id
            actual_type = _canonical_market_type(core._market_type(market))
            if actual_type != market_type:
                raise RuntimeError(
                    f"Exact outcome token resolved to market type '{core._market_type(market)}', "
                    f"not requested '{market_type}'"
                )
        else:
            intent = core.TradeIntent(
                market_url=market_url,
                outcome=outcome,
                market_type=market_type,
                max_price=max_price,
                budget_usdc=budget,
                note="Termux remote live test",
            )
            market = core._select_market(client, intent)
            actual_type = _canonical_market_type(core._market_type(market))
            if actual_type != market_type:
                raise RuntimeError(
                    f"Resolved sports market type is '{core._market_type(market)}', not {market_type}"
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
        raise RuntimeError(f"Current BUY price {buy_price} exceeds your maximum price {max_price}")
    if spread > max_spread:
        raise RuntimeError(f"Current spread {spread} exceeds maximum spread {max_spread}")

    asks = getattr(book, "asks", None) or []
    best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    if best_ask is None or best_ask > max_price:
        raise RuntimeError("Selected limit would not cross the current best ask")

    shares = (budget / max_price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    if min_size > 0 and shares < min_size:
        raise RuntimeError(f"Order is below this market's minimum size of {min_size} shares")
    return {
        "market": str(getattr(market, "question", None) or getattr(market, "slug", "sports market")),
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


def _preview(payload: dict[str, Any]) -> dict[str, Any]:
    out = _validate_buy(payload)
    out.update({"ok": True, "executor": WORKER_NAME, "no_order_placed": True})
    return out


def _buy(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    quote = _validate_buy(payload)
    asset_id = quote["asset_id"]
    shares = Decimal(quote["requested_shares"])
    max_price = Decimal(quote["max_price"])
    before = _position_size(wallet, asset_id)
    order_id = None
    with _secure(private_key, wallet) as client:
        response = client.place_limit_order(token_id=asset_id, price=str(max_price), size=str(shares), side="BUY")
        order_id = str(getattr(response, "order_id", "") or "")
        deadline = time.time() + FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = _position_size(wallet, asset_id)
            if after > before:
                break
        _cancel_quietly(client, order_id)
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
        "executor": WORKER_NAME,
    })
    return result


def _sell(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    _geo()
    asset_id = str(payload.get("asset_id") or "")
    target = Decimal(str(payload.get("shares") or "0"))
    entry = Decimal(str(payload.get("entry_price") or "0"))
    if not asset_id or target <= 0:
        raise RuntimeError("SELL request has no tracked asset/shares")
    before = _position_size(wallet, asset_id)
    sell_size = min(before, target)
    if sell_size <= 0:
        raise RuntimeError("Wallet no longer holds the tracked test shares")
    with PublicClient() as public:
        sell_price = Decimal(str(public.get_price(asset_id=asset_id, side="SELL")))
    if sell_price <= 0 or sell_price >= 1:
        raise RuntimeError(f"Invalid current SELL price {sell_price}")
    order_id = None
    with _secure(private_key, wallet) as client:
        response = client.place_limit_order(token_id=asset_id, price=str(sell_price), size=str(sell_size), side="SELL")
        order_id = str(getattr(response, "order_id", "") or "")
        deadline = time.time() + FILL_WAIT_SECONDS
        after = before
        while time.time() < deadline:
            time.sleep(0.75)
            after = _position_size(wallet, asset_id)
            if after < before:
                break
        _cancel_quietly(client, order_id)
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
        "executor": WORKER_NAME,
    }


def _wallet_heartbeat(private_key: str, wallet: str, geo: dict[str, Any] | None, status: str) -> dict[str, Any]:
    balance = None
    portfolio = None
    wallet_type = None
    masked_wallet = _mask(wallet)
    try:
        with _secure(private_key, wallet) as client:
            ba = client.get_balance_allowance(asset_type="COLLATERAL")
            balance = str((Decimal(str(ba.balance)) / Decimal("1000000")).quantize(Decimal("0.01")))
            wallet_type = str(client.wallet_type)
            masked_wallet = _mask(str(client.wallet))
            try:
                pv = client.get_portfolio_value()
                portfolio = str(Decimal(str(getattr(pv, "value", "0") or "0")).quantize(Decimal("0.01")))
            except Exception:
                portfolio = None
    except Exception as exc:
        status = f"Wallet heartbeat error: {type(exc).__name__}: {exc}"
    return {
        "name": WORKER_NAME,
        "wallet": masked_wallet,
        "wallet_type": wallet_type,
        "usdc_balance": balance,
        "portfolio_value": portfolio,
        "geo_country": (geo or {}).get("country"),
        "geo_region": (geo or {}).get("region"),
        "geo_blocked": (geo or {}).get("blocked"),
        "status": status,
    }


def _post_heartbeat(token: str, private_key: str, wallet: str, geo: dict[str, Any] | None, status: str) -> None:
    body = _wallet_heartbeat(private_key, wallet, geo, status)
    try:
        httpx.post(f"{BRIDGE_URL}/api/executor/heartbeat", headers=_headers(token), json=body, timeout=15).raise_for_status()
    except Exception as exc:
        print(f"Heartbeat failed: {exc}")


def _send_result(token: str, request_id: str, body: dict[str, Any]) -> None:
    r = httpx.post(f"{BRIDGE_URL}/api/executor/result/{request_id}", headers=_headers(token), json=body, timeout=20)
    r.raise_for_status()


def _journal_started(request_id: str, action: str) -> None:
    journal = _read_json(JOURNAL_FILE)
    journal[request_id] = {"status": "STARTED", "action": action, "started_at": time.time()}
    _write_json(JOURNAL_FILE, journal)


def _journal_completed(request_id: str, action: str, body: dict[str, Any]) -> None:
    journal = _read_json(JOURNAL_FILE)
    journal[request_id] = {"status": "COMPLETED", "action": action, "completed_at": time.time(), "body": body}
    # Bound local journal size.
    if len(journal) > 300:
        items = sorted(journal.items(), key=lambda kv: kv[1].get("completed_at", kv[1].get("started_at", 0)))
        journal = dict(items[-300:])
    _write_json(JOURNAL_FILE, journal)


def _existing_journal(request_id: str) -> dict[str, Any] | None:
    return _read_json(JOURNAL_FILE).get(request_id)


def main() -> None:
    private_key, wallet = _credentials()
    token = _load_token() or _pair()

    geo: dict[str, Any] | None = None
    try:
        geo = _geo()
        print(f"Polymarket geoblock check passed: {geo.get('country')}/{geo.get('region')}")
        _post_heartbeat(token, private_key, wallet, geo, "Ready")
    except Exception as exc:
        # Report the blocked state to the dashboard, then stop. Do not bypass it.
        try:
            r = httpx.get("https://polymarket.com/api/geoblock", timeout=10)
            geo = r.json() if r.is_success else None
        except Exception:
            geo = None
        _post_heartbeat(token, private_key, wallet, geo, str(exc))
        print(str(exc))
        raise SystemExit(2)

    print(f"Termux executor online as {WORKER_NAME}. Ctrl+C to stop.")
    last_heartbeat = 0.0
    while True:
        try:
            if time.time() - last_heartbeat >= 20:
                heartbeat_status = "Ready"
                try:
                    geo = _geo()
                except Exception as geo_exc:
                    # Keep the Railway bridge alive even if the public geoblock
                    # endpoint is temporarily unreachable. Every BUY/SELL still
                    # performs its own mandatory fresh _geo() check and will fail
                    # closed if that check cannot be completed.
                    heartbeat_status = f"Geoblock check unavailable: {type(geo_exc).__name__}: {geo_exc}"
                _post_heartbeat(token, private_key, wallet, geo, heartbeat_status)
                last_heartbeat = time.time()

            r = httpx.get(f"{BRIDGE_URL}/api/executor/next", headers=_headers(token), timeout=20)
            if r.status_code == 401:
                print("Executor token was rejected. Delete the local token file and pair again:")
                print(TOKEN_FILE)
                raise SystemExit(3)
            r.raise_for_status()
            request = r.json().get("request")
            if not request:
                time.sleep(1.0)
                continue

            request_id = str(request["id"])
            action = str(request["action"]).upper()
            payload = request.get("payload") or {}
            existing = _existing_journal(request_id)
            if existing and existing.get("status") == "COMPLETED":
                _send_result(token, request_id, existing["body"])
                continue
            if existing and existing.get("status") == "STARTED" and action in {"BUY", "SELL"}:
                body = {
                    "ok": False,
                    "error": "This trade request was interrupted after execution began. Automatic retry was blocked to prevent a duplicate order; reconcile the wallet position manually.",
                }
                _journal_completed(request_id, action, body)
                _send_result(token, request_id, body)
                continue

            print(f"Processing {action} {request_id}")
            _journal_started(request_id, action)
            try:
                if action == "PREVIEW":
                    result = _preview(payload)
                elif action == "BUY":
                    result = _buy(payload, private_key, wallet)
                elif action == "SELL":
                    result = _sell(payload, private_key, wallet)
                else:
                    raise RuntimeError(f"Unknown executor action {action}")
                body = {"ok": True, "result": result}
            except Exception as exc:
                body = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            _journal_completed(request_id, action, body)
            _send_result(token, request_id, body)
            print(f"Completed {action} {request_id}: {'OK' if body['ok'] else body['error']}")
        except KeyboardInterrupt:
            print("\nExecutor stopped.")
            return
        except Exception as exc:
            print(f"Worker loop error: {type(exc).__name__}: {exc}")
            time.sleep(3)


if __name__ == "__main__":
    main()
