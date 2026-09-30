#!/usr/bin/env python3
from __future__ import annotations

import getpass
import json
import os
import subprocess
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

from polymarket import BuilderApiKey, PublicClient, SecureClient  # noqa: E402
from app import main as core  # noqa: E402

BRIDGE_URL = os.getenv("EXECUTOR_BRIDGE_URL", "https://polymarket-auto-bot-production.up.railway.app").rstrip("/")
WORKER_NAME = os.getenv("EXECUTOR_NAME", "termux-phone")
WORKER_CAPABILITIES = ("PREVIEW", "BUY", "SELL", "COMBO_PREVIEW", "COMBO_BUY")
FILL_WAIT_SECONDS = max(3, min(15, int(os.getenv("EXECUTOR_FILL_WAIT_SECONDS", "8"))))
TOKEN_FILE = Path(os.getenv("EXECUTOR_TOKEN_FILE", str(Path.home() / ".config/polymarket-termux/executor_token"))).expanduser()
JOURNAL_FILE = Path(os.getenv("EXECUTOR_JOURNAL_FILE", str(Path.home() / ".config/polymarket-termux/executor_journal.json"))).expanduser()
BUILDER_KEY_FILE = Path(
    os.getenv(
        "POLYMARKET_BUILDER_KEY_FILE",
        str(Path.home() / ".config/polymarket-termux/builder_api_key.json"),
    )
).expanduser()
COMBO_MAX_LEGS = max(2, min(12, int(os.getenv("COMBO_MAX_LEGS", "8"))))
COMBO_FILL_WAIT_SECONDS = max(10, min(120, int(os.getenv("COMBO_FILL_WAIT_SECONDS", "45"))))


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


def _worker_revision() -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short=12", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip() or "unknown"
    except Exception:
        return "unknown"


def _mask(value: str) -> str:
    if len(value) < 12:
        return value
    return value[:6] + "…" + value[-4:]


def _persist_env_value(key: str, value: str) -> None:
    """Persist a non-empty executor setting in the local protected env file."""
    value = value.strip()
    if not value:
        return
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    prefix = f"{key}="
    rendered = f"{key}={value}"
    replaced = False
    output: list[str] = []
    for line in lines:
        if line.startswith(prefix):
            if not replaced:
                output.append(rendered)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(rendered)
    ENV_FILE.write_text("\n".join(output).rstrip() + "\n")
    os.chmod(ENV_FILE, 0o600)
    os.environ[key] = value


def _first_env(*keys: str) -> str:
    for key in keys:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return ""


def _credentials() -> tuple[str, str]:
    private_key = _first_env("POLYMARKET_PRIVATE_KEY")
    wallet = _first_env(
        "POLYMARKET_DEPOSIT_WALLET",
        "POLYMARKET_FUNDER_WALLET",
        "POLYMARKET_FUNDER_ADDRESS",
        "POLYMARKET_WALLET",
    )
    if not private_key:
        private_key = getpass.getpass("Polymarket private key (hidden, not saved): ").strip()
    if not wallet:
        wallet = input("Polymarket deposit/funder wallet (0x…; saved for future starts): ").strip()
        if wallet:
            _persist_env_value("POLYMARKET_DEPOSIT_WALLET", wallet)
    elif not os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip():
        # Migrate legacy wallet variable names into the canonical local env key.
        _persist_env_value("POLYMARKET_DEPOSIT_WALLET", wallet)
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
    with _bridge_client(timeout=15) as h:
        r = h.post(f"{BRIDGE_URL}/api/executor/pair", json={"code": code, "name": WORKER_NAME})
        r.raise_for_status()
        token = str(r.json()["token"])
    _save_token(token)
    print("Paired with Railway executor bridge.")
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _bridge_client(timeout: float = 20) -> httpx.Client:
    """Use the phone's working IPv4 path for Railway executor traffic too."""
    transport = httpx.HTTPTransport(local_address="0.0.0.0", retries=2)
    return httpx.Client(transport=transport, timeout=timeout)


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


def _builder_key_from_env() -> BuilderApiKey | None:
    key = os.getenv("POLYMARKET_BUILDER_API_KEY", "").strip()
    secret = os.getenv("POLYMARKET_BUILDER_SECRET", "").strip()
    passphrase = os.getenv("POLYMARKET_BUILDER_PASSPHRASE", "").strip()
    values = [key, secret, passphrase]
    if any(values) and not all(values):
        raise RuntimeError(
            "Builder credentials are incomplete. Set POLYMARKET_BUILDER_API_KEY, "
            "POLYMARKET_BUILDER_SECRET and POLYMARKET_BUILDER_PASSPHRASE together."
        )
    if all(values):
        return BuilderApiKey(key=key, secret=secret, passphrase=passphrase)
    return None


def _builder_key(private_key: str, wallet: str) -> BuilderApiKey:
    env_key = _builder_key_from_env()
    if env_key is not None:
        return env_key

    saved = _read_json(BUILDER_KEY_FILE)
    if all(saved.get(name) for name in ("key", "secret", "passphrase")):
        return BuilderApiKey(
            key=str(saved["key"]),
            secret=str(saved["secret"]),
            passphrase=str(saved["passphrase"]),
        )

    try:
        with _secure(private_key, wallet) as client:
            created = client.create_builder_api_key()
    except Exception as exc:
        raise RuntimeError(
            "Combo RFQ requires a Polymarket Builder API key and automatic key creation failed. "
            "Configure POLYMARKET_BUILDER_API_KEY / POLYMARKET_BUILDER_SECRET / "
            f"POLYMARKET_BUILDER_PASSPHRASE. Underlying error: {type(exc).__name__}: {exc}"
        ) from exc

    _write_json(
        BUILDER_KEY_FILE,
        {"key": created.key, "secret": created.secret, "passphrase": created.passphrase},
    )
    return created


def _secure_combo(private_key: str, wallet: str) -> SecureClient:
    return SecureClient.create(
        private_key=private_key,
        wallet=wallet,
        api_key=_builder_key(private_key, wallet),
    )


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


def _validate_budget_caps(payload: dict[str, Any]) -> Decimal:
    """Enforce Railway's current dashboard cap plus a high local emergency ceiling."""
    try:
        budget = Decimal(str(payload.get("budget_usdc") or "0"))
        authorized_cap = Decimal(str(payload.get("authorized_max_auto_trade_usdc") or "0"))
    except Exception as exc:
        raise RuntimeError("Invalid executor budget/cap payload") from exc

    if budget <= 0:
        raise RuntimeError("Budget must be greater than $0")
    if authorized_cap <= 0:
        raise RuntimeError("Railway did not provide a valid dashboard Auto trade cap")
    if budget > authorized_cap:
        raise RuntimeError(
            f"Budget ${budget} exceeds the current dashboard Auto trade cap of ${authorized_cap}"
        )
    return budget


def _validate_buy(payload: dict[str, Any]) -> dict[str, Any]:
    market_type = _canonical_market_type(payload.get("market_type"))
    if market_type not in {"moneyline", "spread", "total"}:
        raise RuntimeError("Executor supports only moneyline, spread, and total game markets")

    budget = _validate_budget_caps(payload)
    max_price = Decimal(str(payload.get("max_price") or "0"))
    max_spread = Decimal(str(payload.get("max_spread") or "0.08"))
    max_price_global = Decimal(str(payload.get("max_price_global") or "0.95"))
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


def _validate_combo_payload(payload: dict[str, Any]) -> tuple[list[str], Decimal, Decimal]:
    budget = _validate_budget_caps(payload)
    raw_ids = payload.get("leg_position_ids") or []
    if not isinstance(raw_ids, list):
        raise RuntimeError("Combo leg_position_ids must be a list")
    leg_position_ids = [str(value).strip() for value in raw_ids if str(value).strip()]
    if len(leg_position_ids) < 2:
        raise RuntimeError("A Combo requires at least two position IDs")
    if len(leg_position_ids) > COMBO_MAX_LEGS:
        raise RuntimeError(f"Combo exceeds COMBO_MAX_LEGS={COMBO_MAX_LEGS}")
    if len(set(leg_position_ids)) != len(leg_position_ids):
        raise RuntimeError("Combo contains duplicate position IDs")

    max_price = Decimal(str(payload.get("max_price") or "0.95"))
    if max_price <= 0 or max_price >= 1:
        raise RuntimeError(f"Invalid Combo max price {max_price}")
    return leg_position_ids, budget, max_price


def _combo_quote(payload: dict[str, Any], private_key: str, wallet: str) -> tuple[Any, dict[str, Any]]:
    leg_position_ids, budget, max_price = _validate_combo_payload(payload)
    _geo()
    with _secure_combo(private_key, wallet) as client:
        quote_result = client.request_combo_quote(
            leg_position_ids=leg_position_ids,
            direction="BUY",
            amount=str(budget),
            side="YES",
        )

    quote = getattr(quote_result, "quote", None)
    if quote is None:
        reason = getattr(quote_result, "reason", None)
        raise RuntimeError(f"Combo RFQ returned no usable quote ({reason or 'NO_QUOTES'})")

    blended_price = Decimal(str(getattr(quote, "blended_price", "0") or "0"))
    if blended_price <= 0 or blended_price >= 1:
        raise RuntimeError(f"Combo RFQ returned invalid blended price {blended_price}")
    if blended_price > max_price:
        raise RuntimeError(
            f"Combo RFQ blended price {blended_price} exceeds maximum price {max_price}"
        )

    summary = {
        "ok": True,
        "rfq_id": str(getattr(quote, "rfq_id", "") or getattr(quote_result, "rfq_id", "")),
        "quote_id": str(getattr(quote, "quote_id", "") or ""),
        "combo_position_id": str(getattr(quote, "position_id", "") or ""),
        "blended_price": str(blended_price),
        "maker_amount": str(getattr(quote, "maker_amount", "") or ""),
        "taker_amount": str(getattr(quote, "taker_amount", "") or ""),
        "total_required": str(getattr(quote, "total_required", "") or ""),
        "expires_at": getattr(quote, "expires_at", None),
        "budget_usdc": str(budget),
        "max_price": str(max_price),
        "leg_position_ids": leg_position_ids,
        "legs": payload.get("legs") or [],
        "label": payload.get("label") or "Polymarket Combo",
        "trade_id": payload.get("trade_id"),
        "executor": WORKER_NAME,
    }
    return quote, summary


def _combo_preview(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    _, summary = _combo_quote(payload, private_key, wallet)
    summary["no_order_placed"] = True
    summary["status"] = "QUOTE_ONLY"
    return summary


def _combo_buy(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    quote, summary = _combo_quote(payload, private_key, wallet)
    with _secure_combo(private_key, wallet) as client:
        acceptance = client.accept_combo_quote(quote)
        if str(getattr(acceptance, "status", "")).lower() != "executing":
            reason = getattr(acceptance, "reason", None)
            error = getattr(acceptance, "error", None)
            raise RuntimeError(f"Combo RFQ acceptance failed ({reason or error or 'unknown reason'})")
        rfq_id = str(getattr(acceptance, "rfq_id", summary["rfq_id"]))
        try:
            fill = client.wait_for_combo_fill(
                rfq_id=rfq_id,
                timeout=float(COMBO_FILL_WAIT_SECONDS),
                polling_interval=1.0,
            )
        except TimeoutError:
            status = client.fetch_rfq_status(rfq_id=rfq_id)
            current = str(getattr(status, "status", "")).upper()
            if current in {"FAILED", "EXPIRED", "CANCELED"}:
                error = getattr(status, "error", None)
                raise RuntimeError(
                    f"Combo RFQ terminated after acceptance: {current} {error or ''}".strip()
                )
            summary.update(
                {
                    "ok": True,
                    "status": "COMBO_EXECUTION_PENDING",
                    "accepted": True,
                    "filled_shares": "0",
                    "entry_price": summary["blended_price"],
                    "taker_order_hash": str(
                        getattr(acceptance, "taker_order_hash", "")
                        or getattr(status, "taker_order_hash", "")
                        or ""
                    ),
                    "tx_hash": str(getattr(status, "tx_hash", "") or ""),
                    "rfq_status": current or "EXECUTING",
                    "execution_type": "COMBO_RFQ",
                }
            )
            return summary

    fill_status = str(getattr(fill, "status", "")).upper()
    if fill_status != "FILLED":
        error = getattr(fill, "error", None)
        raise RuntimeError(f"Combo RFQ did not fill: {fill_status or 'UNKNOWN'} {error or ''}".strip())

    filled_shares = Decimal(str(summary["taker_amount"] or "0"))
    if filled_shares <= 0:
        raise RuntimeError("Combo RFQ reported FILLED but returned zero outcome shares")

    summary.update(
        {
            "ok": True,
            "status": "ORDER_SUBMITTED",
            "accepted": True,
            "filled_shares": str(filled_shares),
            "entry_price": summary["blended_price"],
            "taker_order_hash": str(getattr(acceptance, "taker_order_hash", "") or ""),
            "tx_hash": str(getattr(fill, "tx_hash", "") or ""),
            "execution_type": "COMBO_RFQ",
        }
    )
    return summary


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
        "worker_revision": _worker_revision(),
        "capabilities": list(WORKER_CAPABILITIES),
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
        with _bridge_client(timeout=15) as h:
            h.post(
                f"{BRIDGE_URL}/api/executor/heartbeat",
                headers=_headers(token),
                json=body,
            ).raise_for_status()
    except Exception as exc:
        print(f"Heartbeat failed: {exc}")


def _send_result(token: str, request_id: str, body: dict[str, Any]) -> None:
    with _bridge_client(timeout=20) as h:
        r = h.post(
            f"{BRIDGE_URL}/api/executor/result/{request_id}",
            headers=_headers(token),
            json=body,
        )
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
    # The startup path already posted a full wallet heartbeat above. Mark it as
    # current so the first worker-loop action is an executor queue poll instead
    # of a second potentially slow wallet/portfolio heartbeat.
    last_heartbeat = time.time()
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

            with _bridge_client(timeout=20) as h:
                r = h.get(f"{BRIDGE_URL}/api/executor/next", headers=_headers(token))
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
            if existing and existing.get("status") == "STARTED" and action in {"BUY", "SELL", "COMBO_BUY"}:
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
                elif action == "COMBO_PREVIEW":
                    result = _combo_preview(payload, private_key, wallet)
                elif action == "COMBO_BUY":
                    result = _combo_buy(payload, private_key, wallet)
                elif action == "SELL":
                    result = _sell(payload, private_key, wallet)
                else:
                    raise RuntimeError(f"Unknown executor action {action}")
                if action in {"BUY", "COMBO_BUY"} and result.get("ok") is False:
                    body = {
                        "ok": False,
                        "result": result,
                        "error": (
                            "BUY_UNFILLED_RETRYABLE: limit order filled 0 shares "
                            "before the unfilled remainder was canceled"
                        ),
                    }
                else:
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
