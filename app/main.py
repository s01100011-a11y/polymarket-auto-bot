from __future__ import annotations

import asyncio
import json
import os
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from threading import Lock
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, HttpUrl
from polymarket import PublicClient, SecureClient

load_dotenv()

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = APP_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
PENDING_FILE = DATA_DIR / "pending.json"
WATCH_FILE = DATA_DIR / "watchlist.json"
EXECUTIONS_FILE = DATA_DIR / "executions.json"
FILE_LOCK = Lock()

def live_trading_enabled() -> bool:
    """Railway LIVE_TRADING is the authoritative live-mode switch."""
    return os.getenv("LIVE_TRADING", "false").strip().lower() in {"1", "true", "yes", "on"}

LIVE_TRADING = live_trading_enabled()

def auto_trading_enabled() -> bool:
    """Railway AUTO_TRADING is the authoritative automation switch."""
    return os.getenv("AUTO_TRADING", "false").strip().lower() in {"1", "true", "yes", "on"}

AUTO_TRADING = auto_trading_enabled()

WATCH_HEALTH = {"last_started_at": None, "last_completed_at": None, "last_error": None, "cycles": 0}
SIGNAL_SECRET = os.getenv("SIGNAL_SECRET", "")
MAX_TRADE_USDC = Decimal(os.getenv("MAX_TRADE_USDC", "100"))
MAX_AUTO_TRADE_USDC = Decimal(os.getenv("MAX_AUTO_TRADE_USDC", "25"))
MAX_DAILY_BUDGET_USDC = Decimal(os.getenv("MAX_DAILY_BUDGET_USDC", "100"))
MAX_PRICE = Decimal(os.getenv("MAX_PRICE", "0.95"))
MAX_SPREAD = Decimal(os.getenv("MAX_SPREAD", "0.08"))
AUTO_POLL_SECONDS = max(10, int(os.getenv("AUTO_POLL_SECONDS", "30")))
TRADING_TIMEZONE = ZoneInfo(os.getenv("TRADING_TIMEZONE", "Asia/Kuala_Lumpur"))
BLOCK_POLITICAL_AUTO = os.getenv("BLOCK_POLITICAL_AUTO", "true").lower() == "true"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str))
    tmp.replace(path)


def _load(path: Path) -> dict:
    with FILE_LOCK:
        return _read_json(path)


def _save(path: Path, data: dict) -> None:
    with FILE_LOCK:
        _write_json(path, data)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expires_at must be ISO-8601") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class TradeIntent(BaseModel):
    market_url: HttpUrl
    outcome: str = Field(min_length=1, max_length=160)
    market_type: str = Field(default="moneyline", min_length=1, max_length=120)
    max_price: Decimal = Field(gt=0, lt=1)
    budget_usdc: Decimal = Field(gt=0)
    note: str = ""


class Approval(BaseModel):
    confirmation: Literal["APPROVE"]


class AutoSignal(TradeIntent):
    signal_id: str = Field(min_length=4, max_length=120)
    category: Literal["sports", "other", "politics"] = "sports"
    expires_at: str


def _check_geoblock() -> dict:
    r = httpx.get("https://polymarket.com/api/geoblock", timeout=10)
    r.raise_for_status()
    geo = r.json()
    if geo.get("blocked"):
        raise HTTPException(
            status_code=451,
            detail=f"Polymarket trading is not available from this location ({geo.get('country')}/{geo.get('region')}).",
        )
    return geo


def _norm(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _sports_event_slug(raw_url: str) -> str | None:
    parsed = urlparse(raw_url)
    if parsed.scheme != "https" or parsed.netloc not in {"polymarket.com", "www.polymarket.com"}:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 3 and parts[0] == "sports":
        return parts[-1]
    return None


def _market_type(market) -> str:
    sports = getattr(market, "sports", None)
    return str(getattr(sports, "sports_market_type", "") or "")


def _outcome_matches_market(market, outcome: str) -> bool:
    target = _norm(outcome)
    if target in {"yes", "no"}:
        return True
    yes_label = _norm(str(getattr(market.outcomes.yes, "label", "")))
    no_label = _norm(str(getattr(market.outcomes.no, "label", "")))
    return target in {yes_label, no_label}


def _select_market(client: PublicClient, intent: TradeIntent):
    raw_url = str(intent.market_url)
    event_slug = _sports_event_slug(raw_url)

    if event_slug is None:
        try:
            return client.get_market(url=raw_url)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Market lookup failed: {exc}") from exc

    try:
        event = client.get_event(slug=event_slug)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Sports event lookup failed: {exc}") from exc

    wanted_type = _norm(intent.market_type)
    candidates = [
        market
        for market in event.markets
        if _norm(_market_type(market)) == wanted_type and _outcome_matches_market(market, intent.outcome)
    ]

    if not candidates:
        available_types = sorted({_market_type(m) for m in event.markets if _market_type(m)})
        raise HTTPException(
            status_code=400,
            detail=(
                f"No '{intent.market_type}' market matched outcome '{intent.outcome}' in event "
                f"'{getattr(event, 'title', event_slug)}'. Available market types include: "
                + ", ".join(available_types[:20])
            ),
        )

    accepting = [m for m in candidates if getattr(getattr(m, "state", None), "accepting_orders", None)]
    if len(accepting) == 1:
        return accepting[0]
    if len(candidates) == 1:
        return candidates[0]

    active = [m for m in candidates if getattr(getattr(m, "state", None), "active", None)]
    if len(active) == 1:
        return active[0]

    names = [getattr(m, "question", None) or getattr(m, "slug", None) or str(getattr(m, "id", "")) for m in candidates]
    raise HTTPException(
        status_code=400,
        detail=f"Multiple '{intent.market_type}' markets matched. Be more specific. Matches: {names[:8]}",
    )


def _resolve_asset(market, outcome: str) -> tuple[str, str, str]:
    target = _norm(outcome)
    yes = market.outcomes.yes
    no = market.outcomes.no
    yes_label = str(getattr(yes, "label", "Yes"))
    no_label = str(getattr(no, "label", "No"))

    if target in {"yes", _norm(yes_label)}:
        selected, side, label = yes, "YES", yes_label
    elif target in {"no", _norm(no_label)}:
        selected, side, label = no, "NO", no_label
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Outcome '{outcome}' does not match this market. Valid outcomes: '{yes_label}' or '{no_label}'.",
        )

    asset_id = getattr(selected, "token_id", None) or getattr(selected, "position_id", None)
    if not asset_id:
        raise HTTPException(status_code=400, detail=f"No tradable asset found for outcome '{label}'.")
    return str(asset_id), side, label


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _risk_checks(intent: TradeIntent, spread: Decimal | None, *, auto: bool = False) -> None:
    cap = MAX_AUTO_TRADE_USDC if auto else MAX_TRADE_USDC
    if intent.budget_usdc > cap:
        raise HTTPException(
            status_code=400,
            detail=f"Trade budget ${intent.budget_usdc} exceeds {'MAX_AUTO_TRADE_USDC' if auto else 'MAX_TRADE_USDC'}=${cap}.",
        )
    if intent.max_price > MAX_PRICE:
        raise HTTPException(status_code=400, detail=f"Limit price {intent.max_price} exceeds MAX_PRICE={MAX_PRICE}.")
    if spread is not None and spread > MAX_SPREAD:
        raise HTTPException(status_code=400, detail=f"Spread {spread} exceeds MAX_SPREAD={MAX_SPREAD}.")


def _quote(intent: TradeIntent, *, auto: bool = False) -> dict:
    geo = _check_geoblock()

    with PublicClient() as client:
        market = _select_market(client, intent)
        asset_id, outcome_side, outcome_label = _resolve_asset(market, intent.outcome)
        book = client.get_order_book(asset_id=asset_id)
        buy_price = _decimal(client.get_price(asset_id=asset_id, side="BUY"))
        midpoint = _decimal(client.get_midpoint(asset_id=asset_id))
        spread = _decimal(client.get_spread(asset_id=asset_id))

    _risk_checks(intent, spread, auto=auto)

    best_ask = None
    ask_size = None
    shares_available_at_limit = Decimal("0")
    if getattr(book, "asks", None):
        for level in book.asks:
            price = _decimal(level.price)
            size = _decimal(level.size)
            if best_ask is None or price < best_ask:
                best_ask = price
                ask_size = size
            if price <= intent.max_price:
                shares_available_at_limit += size

    shares = (intent.budget_usdc / intent.max_price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    question = getattr(market, "question", None) or getattr(market, "slug", None) or str(getattr(market, "id", "market"))

    return {
        "market": question,
        "market_type": _market_type(market),
        "market_url": str(intent.market_url),
        "requested_outcome": intent.outcome,
        "resolved_outcome": outcome_label,
        "outcome_side": outcome_side,
        "asset_id": asset_id,
        "limit_price": str(intent.max_price),
        "budget_usdc": str(intent.budget_usdc),
        "shares": str(shares),
        "current_buy_price": str(buy_price),
        "midpoint": str(midpoint),
        "spread": str(spread),
        "best_ask": str(best_ask) if best_ask is not None else None,
        "best_ask_size": str(ask_size) if best_ask is not None else None,
        "shares_available_at_limit": str(shares_available_at_limit),
        "would_cross_now": bool(best_ask is not None and best_ask <= intent.max_price),
        "geo_country": geo.get("country"),
        "geo_region": geo.get("region"),
        "live_trading_enabled": live_trading_enabled(),
        "auto_trading_enabled": auto_trading_enabled(),
        "note": intent.note,
    }


def _daily_budget_used() -> Decimal:
    records = _load(EXECUTIONS_FILE)
    today = datetime.now(TRADING_TIMEZONE).date()
    total = Decimal("0")
    for rec in records.values():
        ts = rec.get("submitted_at") or rec.get("created_at")
        if not ts:
            continue
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TRADING_TIMEZONE)
        except ValueError:
            continue
        if when.date() == today and rec.get("status") in {"ORDER_SUBMITTED", "AUTO_DRY_RUN"}:
            total += Decimal(str(rec.get("budget_usdc", "0")))
    return total


def _check_daily_budget(intent: TradeIntent) -> None:
    used = _daily_budget_used()
    if used + intent.budget_usdc > MAX_DAILY_BUDGET_USDC:
        raise HTTPException(
            status_code=400,
            detail=f"Daily auto budget would exceed MAX_DAILY_BUDGET_USDC=${MAX_DAILY_BUDGET_USDC}; used=${used}.",
        )


def _execute_limit(intent: TradeIntent, quote: dict, *, auto: bool, signal_id: str | None = None) -> dict:
    if auto:
        _check_daily_budget(intent)

    record_id = signal_id or uuid.uuid4().hex[:12]
    base = {
        "id": record_id,
        "created_at": _now().isoformat(),
        "budget_usdc": str(intent.budget_usdc),
        "intent": intent.model_dump(mode="json"),
        "quote": quote,
        "auto": auto,
    }

    if not live_trading_enabled() or (auto and not auto_trading_enabled()):
        base.update({
            "status": "AUTO_DRY_RUN" if auto else "APPROVED_DRY_RUN",
            "submitted_at": _now().isoformat(),
            "execution": {
                "placed": False,
                "reason": "Dry run: live/auto trading is disabled.",
                "would_place": {
                    "asset_id": quote["asset_id"],
                    "side": "BUY",
                    "price": quote["limit_price"],
                    "size": quote["shares"],
                    "outcome": quote["resolved_outcome"],
                },
            },
        })
    else:
        private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
        wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET")
        if not private_key or not wallet:
            raise HTTPException(status_code=500, detail="Live trading is enabled but wallet credentials are missing.")

        with SecureClient.create(private_key=private_key, wallet=wallet) as client:
            response = client.place_limit_order(
                token_id=quote["asset_id"],
                price=quote["limit_price"],
                size=quote["shares"],
                side="BUY",
            )
        base.update({
            "status": "ORDER_SUBMITTED",
            "submitted_at": _now().isoformat(),
            "execution": {
                "placed": True,
                "order_id": str(getattr(response, "order_id", "")),
                "response": str(response),
            },
        })

    executions = _load(EXECUTIONS_FILE)
    executions[record_id] = base
    _save(EXECUTIONS_FILE, executions)
    return base


def _require_signal_secret(value: str | None) -> None:
    if not SIGNAL_SECRET:
        raise HTTPException(status_code=503, detail="SIGNAL_SECRET is not configured.")
    if value is None or not secrets.compare_digest(value, SIGNAL_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signal secret.")


def _validate_auto_signal(signal: AutoSignal) -> datetime:
    expiry = _parse_time(signal.expires_at)
    if expiry <= _now():
        raise HTTPException(status_code=400, detail="Signal is already expired.")
    if BLOCK_POLITICAL_AUTO and signal.category == "politics":
        raise HTTPException(status_code=400, detail="Automatic execution is disabled for political markets.")
    return expiry


def _try_execute_signal(signal: AutoSignal) -> dict:
    _validate_auto_signal(signal)
    executions = _load(EXECUTIONS_FILE)
    if signal.signal_id in executions:
        return executions[signal.signal_id]

    quote = _quote(signal, auto=True)
    if not quote["would_cross_now"]:
        return {"status": "WAITING_FOR_PRICE", "signal_id": signal.signal_id, "quote": quote}

    return _execute_limit(signal, quote, auto=True, signal_id=signal.signal_id)


def _process_watchlist_once() -> None:
    WATCH_HEALTH["last_started_at"] = datetime.now(timezone.utc).isoformat()
    watch = _load(WATCH_FILE)
    changed = False
    for signal_id, rec in list(watch.items()):
        if rec.get("status") not in {"WATCHING", "ERROR"}:
            continue
        try:
            signal = AutoSignal.model_validate(rec["signal"])
            if _parse_time(signal.expires_at) <= _now():
                rec["status"] = "EXPIRED"
                rec["updated_at"] = _now().isoformat()
                changed = True
                continue
            result = _try_execute_signal(signal)
            rec["last_check_at"] = _now().isoformat()
            rec["last_result"] = result
            if result.get("status") in {"ORDER_SUBMITTED", "AUTO_DRY_RUN"}:
                rec["status"] = result["status"]
            else:
                rec["status"] = "WATCHING"
            rec["updated_at"] = _now().isoformat()
            changed = True
        except Exception as exc:
            rec["status"] = "ERROR"
            rec["last_error"] = str(exc)
            rec["updated_at"] = _now().isoformat()
            changed = True
    if changed:
        _save(WATCH_FILE, watch)
    WATCH_HEALTH["last_completed_at"] = datetime.now(timezone.utc).isoformat()
    WATCH_HEALTH["last_error"] = None
    WATCH_HEALTH["cycles"] = int(WATCH_HEALTH.get("cycles") or 0) + 1


async def _watch_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(_process_watchlist_once)
        except Exception as exc:
            WATCH_HEALTH["last_error"] = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(AUTO_POLL_SECONDS)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(_watch_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Polymarket Auto Bot", version="0.3.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "0.3.0",
        "live_trading": live_trading_enabled(),
        "auto_trading": auto_trading_enabled(),
        "watch_loop": dict(WATCH_HEALTH),
        "poll_seconds": AUTO_POLL_SECONDS,
        "max_trade_usdc": str(MAX_TRADE_USDC),
        "max_auto_trade_usdc": str(MAX_AUTO_TRADE_USDC),
        "max_daily_budget_usdc": str(MAX_DAILY_BUDGET_USDC),
        "max_price": str(MAX_PRICE),
        "max_spread": str(MAX_SPREAD),
        "block_political_auto": BLOCK_POLITICAL_AUTO,
    }


@app.post("/prepare")
def prepare(intent: TradeIntent):
    quote = _quote(intent)
    trade_id = uuid.uuid4().hex[:12]
    record = {
        "id": trade_id,
        "status": "PENDING_APPROVAL",
        "created_at": _now().isoformat(),
        "intent": intent.model_dump(mode="json"),
        "quote": quote,
    }
    pending = _load(PENDING_FILE)
    pending[trade_id] = record
    _save(PENDING_FILE, pending)
    return record


@app.post("/approve/{trade_id}")
def approve(trade_id: str, approval: Approval):
    pending = _load(PENDING_FILE)
    record = pending.get(trade_id)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown trade id")
    if record.get("status") != "PENDING_APPROVAL":
        raise HTTPException(status_code=400, detail=f"Trade status is {record.get('status')}")

    intent = TradeIntent.model_validate(record["intent"])
    fresh = _quote(intent)
    result = _execute_limit(intent, fresh, auto=False, signal_id=trade_id)
    record.update(result)
    pending[trade_id] = record
    _save(PENDING_FILE, pending)
    return record


@app.post("/auto/execute")
def auto_execute(signal: AutoSignal, x_signal_secret: str | None = Header(default=None)):
    _require_signal_secret(x_signal_secret)
    _validate_auto_signal(signal)
    return _try_execute_signal(signal)


@app.post("/auto/watch")
def auto_watch(signal: AutoSignal, x_signal_secret: str | None = Header(default=None)):
    _require_signal_secret(x_signal_secret)
    _validate_auto_signal(signal)

    executions = _load(EXECUTIONS_FILE)
    if signal.signal_id in executions:
        return executions[signal.signal_id]

    watch = _load(WATCH_FILE)
    if signal.signal_id in watch:
        return watch[signal.signal_id]

    rec = {
        "signal_id": signal.signal_id,
        "status": "WATCHING",
        "created_at": _now().isoformat(),
        "signal": signal.model_dump(mode="json"),
    }
    watch[signal.signal_id] = rec
    _save(WATCH_FILE, watch)

    try:
        result = _try_execute_signal(signal)
        rec["last_check_at"] = _now().isoformat()
        rec["last_result"] = result
        if result.get("status") in {"ORDER_SUBMITTED", "AUTO_DRY_RUN"}:
            rec["status"] = result["status"]
        _save(WATCH_FILE, {**_load(WATCH_FILE), signal.signal_id: rec})
    except Exception as exc:
        rec["status"] = "ERROR"
        rec["last_error"] = str(exc)
        _save(WATCH_FILE, {**_load(WATCH_FILE), signal.signal_id: rec})
    return rec


@app.get("/auto/watch")
def list_watch(x_signal_secret: str | None = Header(default=None)):
    _require_signal_secret(x_signal_secret)
    return _load(WATCH_FILE)


@app.get("/auto/executions")
def list_executions(x_signal_secret: str | None = Header(default=None)):
    _require_signal_secret(x_signal_secret)
    return _load(EXECUTIONS_FILE)


@app.get("/", response_class=HTMLResponse)
def index():
    mode = "LIVE" if live_trading_enabled() else "DRY RUN"
    auto = "ON" if auto_trading_enabled() else "OFF"
    return f"""
<!doctype html>
<html>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width,initial-scale=1'>
  <title>Polymarket Auto Bot</title>
  <style>
    body{{font-family:system-ui;max-width:760px;margin:40px auto;padding:0 18px;background:#0b0d10;color:#eef2f6}}
    code,pre{{background:#151a21;padding:3px 6px;border-radius:6px}}
    .pill{{padding:8px 12px;border:1px solid #4b5563;border-radius:999px;display:inline-block;margin-right:8px}}
  </style>
</head>
<body>
  <h1>Polymarket Auto Bot</h1>
  <p><span class='pill'>Trading: {mode}</span><span class='pill'>Automation: {auto}</span></p>
  <p>v0.3 accepts normal Polymarket sports event URLs and named outcomes such as a player or team.</p>
  <p>Open <a href='/docs'>/docs</a> for the API. Keep <code>LIVE_TRADING=false</code> and <code>AUTO_TRADING=false</code> until dry-run tests pass.</p>
  <p>Auto cap: ${MAX_AUTO_TRADE_USDC} per signal; daily cap: ${MAX_DAILY_BUDGET_USDC}; max spread: {MAX_SPREAD}.</p>
</body>
</html>
"""
