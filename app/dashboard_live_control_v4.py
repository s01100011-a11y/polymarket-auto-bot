from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

from app import capper_control
from app import dashboard_metrics_v3 as base
from app import live_trading

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.ingest
remote = base.remote

SLACK_MODE_FILE = core.DATA_DIR / "slack_trading_mode.json"

MONITOR_UNIT_SETTINGS_FILE = core.DATA_DIR / "capper_unit_sizes.json"
MONITOR_DEFAULT_UNIT_USDC = Decimal("10")


def _monitor_label(sport: str) -> str:
    key = str(sport or "").strip().upper()
    if key not in {"WNBA", "NBA"}:
        raise ValueError("Unsupported basketball monitor sport")
    return f"{key} Monitor - {key}"


def _monitor_portfolio_value_usdc() -> Decimal | None:
    try:
        state = remote._state()
        cash_raw = state.get("usdc_balance")
        positions_raw = state.get("portfolio_value")
        if cash_raw is None or cash_raw == "" or positions_raw is None or positions_raw == "":
            return None
        return (
            Decimal(str(cash_raw)) + Decimal(str(positions_raw))
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def _monitor_unit_config(label: str) -> dict[str, Any]:
    saved = core._load(MONITOR_UNIT_SETTINGS_FILE)
    settings = saved if isinstance(saved, dict) else {}
    try:
        fixed = Decimal(str(settings.get(label, MONITOR_DEFAULT_UNIT_USDC)))
    except Exception:
        fixed = MONITOR_DEFAULT_UNIT_USDC
    if fixed <= 0:
        fixed = MONITOR_DEFAULT_UNIT_USDC
    fixed = fixed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    mode = str(settings.get(f"{label}::mode", "fixed") or "fixed").strip().lower()
    if mode not in {"fixed", "portfolio_pct"}:
        mode = "fixed"

    try:
        pct = Decimal(str(settings.get(f"{label}::portfolio_pct", "10")))
    except Exception:
        pct = Decimal("10")
    if pct <= 0 or pct > 100:
        pct = Decimal("10")
    pct = pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    portfolio = _monitor_portfolio_value_usdc()
    effective = fixed
    error = None
    if mode == "portfolio_pct":
        if portfolio is None:
            error = "Portfolio value is unavailable; percentage unit sizing is waiting for a wallet heartbeat."
        elif portfolio <= 0:
            error = "Portfolio value must be greater than $0 for percentage unit sizing."
        else:
            effective = (
                portfolio * pct / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if effective <= 0:
                error = "Calculated percentage unit size is not positive."

    return {
        "mode": mode,
        "unit_usdc": str(effective),
        "fixed_unit_usdc": str(fixed),
        "portfolio_pct": str(pct),
        "portfolio_value_usdc": str(portfolio) if portfolio is not None else None,
        "error": error,
    }


def _monitor_stake_to_win(target_profit_usdc: Decimal, price: Decimal) -> Decimal:
    if target_profit_usdc <= 0:
        raise ValueError("Monitor unit size must be positive")
    if price <= 0 or price >= 1:
        raise ValueError(f"Invalid current BUY price {price}")
    return (
        target_profit_usdc * price / (Decimal("1") - price)
    ).quantize(Decimal("0.01"), rounding=ROUND_UP)



class SlackTradingMode(BaseModel):
    live_enabled: bool
    stake_usdc: Decimal = Field(gt=0, le=100)


def _mode() -> dict[str, Any]:
    saved = core._load(SLACK_MODE_FILE)
    # Railway AUTO_TRADING is authoritative. When enabled, no saved/dashboard
    # state may disable automatic live preparation/execution.
    auto_prepare_enabled = bool(core.bot_enabled()) and (
        bool(core.auto_trading_enabled()) or bool(saved.get("auto_prepare_enabled", saved.get("live_enabled", False)))
    )
    default_stake = min(Decimal("5"), core.MAX_AUTO_TRADE_USDC)
    stake = Decimal(str(saved.get("stake_usdc") or default_stake))
    stake = min(stake, core.MAX_AUTO_TRADE_USDC)
    return {"auto_prepare_enabled": auto_prepare_enabled, "live_enabled": bool(core.auto_trading_enabled()), "stake_usdc": str(stake), "railway_override": bool(core.auto_trading_enabled())}


def _save_mode(auto_prepare_enabled: bool, stake_usdc: Decimal) -> dict[str, Any]:
    stake = min(Decimal(str(stake_usdc)), core.MAX_AUTO_TRADE_USDC)
    effective_auto = bool(core.bot_enabled()) and (bool(core.auto_trading_enabled()) or bool(auto_prepare_enabled))
    data = {"auto_prepare_enabled": effective_auto, "live_enabled": bool(core.auto_trading_enabled()), "stake_usdc": str(stake), "railway_override": bool(core.auto_trading_enabled())}
    core._save(SLACK_MODE_FILE, data)
    # Railway AUTO_TRADING authorizes unattended dispatch; normal risk filters still apply.
    ingest.SLACK_PAPER_ONLY = not effective_auto
    return data


def _executor_ready() -> tuple[bool, dict[str, Any]]:
    state = remote._state()
    last_seen = float(state.get("last_seen_unix") or 0)
    connected = bool(
        state.get("token_hash")
        and last_seen
        and time.time() - last_seen <= remote.CONNECTED_SECONDS
    )
    ready = bool(
        remote.REMOTE_EXECUTION_ENABLED
        and connected
        and not state.get("geo_blocked")
    )
    return ready, state


def _pw_signal_key(payload: dict[str, Any]) -> str:
    return str(
        payload.get("slack_event_id")
        or payload.get("strategy_pick_id")
        or ""
    ).strip()


def _active_or_pending(
    asset_id: str,
    market_url: str,
    outcome: str,
    *,
    signal_key: str | None = None,
) -> bool:
    """Block duplicates for the same PW/Slack signal, not every call on the same side."""
    signal_key = str(signal_key or "").strip()
    remote._expire_stale_buys_persisted()
    executions = core._load(core.EXECUTIONS_FILE)
    for rec in executions.values():
        if rec.get("paper") or rec.get("source") != "slack_live":
            continue
        if rec.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
            continue
        q = rec.get("quote") or {}
        if str(q.get("asset_id") or "") != asset_id:
            continue
        if signal_key:
            rec_key = str(rec.get("slack_event_id") or rec.get("strategy_pick_id") or "").strip()
            if rec_key != signal_key:
                continue
        return True

    queue = remote._queue_load()
    for rec in queue.values():
        if rec.get("action") != "BUY" or rec.get("status") not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
            continue
        payload = rec.get("payload") or {}
        if payload.get("source") != "slack_live":
            continue
        if (
            str(payload.get("market_url") or "") != market_url
            or str(payload.get("outcome") or "").casefold() != outcome.casefold()
        ):
            continue
        if signal_key and _pw_signal_key(payload) != signal_key:
            continue
        return True
    return False


_PAPER_HANDLER = ingest._paper_trade_from_alert


def _prepare_remote_buy(payload: dict[str, Any]) -> dict[str, Any]:
    odds_approval_required = bool(remote._buy_requires_min_odds_approval(payload))
    if core.auto_trading_enabled() and not odds_approval_required:
        ready, state = _executor_ready()

        if not ready:
            if state.get("geo_blocked"):
                raise ValueError("Termux executor is geoblocked")

            raise ValueError(
                "Termux executor is offline; live BUY was not queued"
            )
    req_id = f"exec-{uuid.uuid4().hex[:14]}"
    record = {
        "id": req_id,
        "action": "BUY",
        "status": (
            "WAITING_APPROVAL"
            if odds_approval_required or not core.auto_trading_enabled()
            else "PENDING"
        ),
        "payload": payload,
        "created_at": ingest._now_iso(),
        "created_unix": time.time(),
        "updated_at": ingest._now_iso(),
    }
    with remote._QUEUE_LOCK:
        data = remote._queue_load()
        data[req_id] = record
        remote._queue_save(data)
    return record


def _slack_trade_handler(
    parsed: dict[str, Any],
    slack_event_id: str,
    slack_event: dict[str, Any],
) -> dict[str, Any]:
    if not core.bot_enabled():
        raise ValueError("Dashboard master switch is OFF")
    mode = _mode()
    monitor_sport = str(slack_event.get("monitor_sport") or "").upper()
    monitor_source = str(slack_event.get("monitor_source") or "").strip()
    if monitor_sport not in {"WNBA", "NBA"}:
        monitor_sport = ""
        monitor_source = ""
    monitor_label = _monitor_label(monitor_sport) if monitor_sport else None
    if monitor_label and not capper_control.is_enabled(core, monitor_label):
        raise ValueError(f"{monitor_label} is OFFLINE")

    ingest.SLACK_PAPER_ONLY = not mode["auto_prepare_enabled"]
    if not mode["auto_prepare_enabled"]:
        if not monitor_sport:
            ingest.SLACK_PAPER_BUDGET_USDC = Decimal(str(mode["stake_usdc"]))
            return _PAPER_HANDLER(parsed, slack_event_id, slack_event)

        # WNBA/NBA monitor paper trades use the exact same TO-WIN unit sizing
        # as live trades so historical strategy stats remain comparable.
        event, market, outcome_label, outcome_obj = ingest._find_market(parsed)
        asset_id = str(
            getattr(outcome_obj, "token_id", None)
            or getattr(outcome_obj, "position_id", None)
            or ""
        )
        if not asset_id:
            raise ValueError("Matched moneyline has no tradable asset id")
        with ingest.PublicClient() as client:
            buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        unit_config = _monitor_unit_config(str(monitor_label))
        if unit_config.get("error"):
            raise ValueError(str(unit_config["error"]))
        unit_usdc = Decimal(str(unit_config["unit_usdc"]))
        target_profit = unit_usdc
        stake = _monitor_stake_to_win(target_profit, buy_price)
        if stake > core.MAX_AUTO_TRADE_USDC:
            raise ValueError(
                "1u target profit $" + str(target_profit)
                + " requires $" + str(stake) + " stake at " + str(buy_price)
                + "; exceeds dashboard Auto trade cap $" + str(core.MAX_AUTO_TRADE_USDC)
            )
        ingest.SLACK_PAPER_BUDGET_USDC = stake
        trade = _PAPER_HANDLER(parsed, slack_event_id, slack_event)
        trade.update({
            "strategy_units": "1",
            "strategy_unit_usdc": str(unit_usdc),
            "strategy_target_profit_usdc": str(target_profit),
            "sizing_mode": "TO_WIN",
            "unit_mode": unit_config["mode"],
            "portfolio_pct": unit_config["portfolio_pct"],
            "portfolio_value_usdc": unit_config["portfolio_value_usdc"],
        })
        trade_id = str(trade.get("id") or "")
        if trade_id:
            executions = core._load(core.EXECUTIONS_FILE)
            if isinstance(executions, dict) and isinstance(executions.get(trade_id), dict):
                executions[trade_id].update({
                    "strategy_units": "1",
                    "strategy_unit_usdc": str(unit_usdc),
                    "strategy_target_profit_usdc": str(target_profit),
                    "sizing_mode": "TO_WIN",
                    "unit_mode": unit_config["mode"],
                    "portfolio_pct": unit_config["portfolio_pct"],
                    "portfolio_value_usdc": unit_config["portfolio_value_usdc"],
                })
                core._save(core.EXECUTIONS_FILE, executions)
        return trade

    if parsed.get("market_kind") != "moneyline":
        raise ValueError("Slack LIVE mode is hard-locked to moneyline only")
    if not parsed.get("selection"):
        raise ValueError("Could not identify the predicted winner")

    event, market, outcome_label, outcome_obj = ingest._find_market(parsed)
    asset_id = str(
        getattr(outcome_obj, "token_id", None)
        or getattr(outcome_obj, "position_id", None)
        or ""
    )
    if not asset_id:
        raise ValueError("Matched moneyline has no tradable asset id")

    with ingest.PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))

    if buy_price <= 0 or buy_price >= 1:
        raise ValueError(f"Invalid current BUY price {buy_price}")
    if buy_price > core.MAX_PRICE:
        raise ValueError(f"Current BUY price {buy_price} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise ValueError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug:
        raise ValueError("Matched Polymarket event has no slug")
    league = ingest._league_for_team(str(parsed.get("selection") or ""))
    if league not in {"nba", "wnba"}:
        raise ValueError("Could not determine NBA/WNBA league for predicted winner")
    market_url = f"https://polymarket.com/sports/{league}/{event_slug}"

    if _active_or_pending(
        asset_id,
        market_url,
        outcome_label,
        signal_key=slack_event_id,
    ):
        raise ValueError("This PW/Slack signal already has an open or pending LIVE position")

    unit_config = None
    unit_usdc = None
    target_profit = None
    if monitor_sport:
        unit_config = _monitor_unit_config(str(monitor_label))
        if unit_config.get("error"):
            raise ValueError(str(unit_config["error"]))
        unit_usdc = Decimal(str(unit_config["unit_usdc"]))
        target_profit = unit_usdc
        stake = _monitor_stake_to_win(target_profit, buy_price)
        if stake > core.MAX_AUTO_TRADE_USDC:
            raise ValueError(
                "1u target profit $" + str(target_profit)
                + " requires $" + str(stake) + " stake at " + str(buy_price)
                + "; exceeds dashboard Auto trade cap $" + str(core.MAX_AUTO_TRADE_USDC)
            )
    else:
        stake = min(Decimal(str(mode["stake_usdc"])), core.MAX_AUTO_TRADE_USDC)

    trade_id = f"slack-live-{slack_event_id[:12]}-{uuid.uuid4().hex[:6]}"
    payload = {
        "market_url": market_url,
        "outcome": outcome_label,
        "market_type": "moneyline",
        "asset_id": asset_id,
        "market_label": str(getattr(market, "question", None) or getattr(event, "title", "") or outcome_label),
        "max_price": str(buy_price),
        "limit_order_ttl_seconds": 120,
        "signal_buy_price": str(buy_price),
        "signal_spread": str(spread),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "slack_live",
        "auto": True,
        "slack_event_id": slack_event_id,
        "strategy_source": f"{monitor_source} - {monitor_sport}" if monitor_sport else None,
        "strategy_sport": monitor_sport or None,
        "strategy_units": "1" if monitor_sport else None,
        "strategy_unit_usdc": str(unit_usdc) if unit_usdc is not None else None,
        "strategy_target_profit_usdc": str(target_profit) if target_profit is not None else None,
        "strategy_pick_id": slack_event_id if monitor_sport else None,
        "strategy_posted_at": ((parsed.get("pw") or {}).get("event_ts") if monitor_sport else None),
        "strategy_selection": parsed.get("selection") if monitor_sport else None,
        "sizing_mode": "TO_WIN" if monitor_sport else "STAKE",
        "unit_mode": unit_config["mode"] if unit_config else None,
        "portfolio_pct": unit_config["portfolio_pct"] if unit_config else None,
        "portfolio_value_usdc": unit_config["portfolio_value_usdc"] if unit_config else None,
    }
    queued = _prepare_remote_buy(payload)
    return {
        "id": trade_id,
        "trade_id": trade_id,
        "paper": False,
        "queued": queued.get("status") == "PENDING",
        "prepared": True,
        "requires_approval": queued.get("status") == "WAITING_APPROVAL",
        "request_id": queued["id"],
        "approval_reason": payload.get("approval_reason"),
        "signal_decimal_odds": payload.get("signal_decimal_odds"),
        "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
        "source": "slack_live",
        "market": str(getattr(market, "question", None) or getattr(event, "title", f"{league.upper()} moneyline")),
        "market_url": market_url,
        "outcome": outcome_label,
        "asset_id": asset_id,
        "buy_price": str(buy_price),
        "spread": str(spread),
        "stake_usdc": str(stake),
        "strategy_source": payload.get("strategy_source"),
        "strategy_sport": payload.get("strategy_sport"),
        "strategy_units": payload.get("strategy_units"),
        "strategy_unit_usdc": payload.get("strategy_unit_usdc"),
        "strategy_target_profit_usdc": payload.get("strategy_target_profit_usdc"),
        "strategy_pick_id": payload.get("strategy_pick_id"),
        "strategy_posted_at": payload.get("strategy_posted_at"),
        "strategy_selection": payload.get("strategy_selection"),
    }

ingest._paper_trade_from_alert = _slack_trade_handler
ingest.SLACK_PAPER_ONLY = not _mode()["auto_prepare_enabled"]


def _hydrate_monitor_retry_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore WNBA/NBA monitor identity on an older PW retry before it is queued."""
    hydrated = dict(payload)
    event_id = str(
        hydrated.get("strategy_pick_id")
        or hydrated.get("slack_event_id")
        or ""
    ).strip()

    alert = None
    try:
        alerts = core._load(ingest.SLACK_ALERTS_FILE)
        if isinstance(alerts, dict):
            direct = alerts.get(event_id)
            if isinstance(direct, dict):
                alert = direct
            else:
                for candidate in alerts.values():
                    if not isinstance(candidate, dict):
                        continue
                    candidate_id = str(candidate.get("event_id") or "").strip()
                    if candidate_id == event_id:
                        alert = candidate
                        break
    except Exception:
        alert = None

    parsed = (alert or {}).get("parsed") or {}
    meta = (alert or {}).get("pw_export_meta") or {}
    alert_sport = str(
        (alert or {}).get("monitor_sport")
        or (alert or {}).get("pw_export_sport")
        or meta.get("sport")
        or ""
    ).upper()

    selection = str(
        hydrated.get("strategy_selection")
        or parsed.get("selection")
        or meta.get("predicted_winner")
        or hydrated.get("outcome")
        or ""
    ).strip()

    sport = str(hydrated.get("strategy_sport") or alert_sport or "").upper()
    if sport not in {"WNBA", "NBA"}:
        inferred = str(ingest._league_for_team(selection) or "").upper()
        sport = inferred if inferred in {"WNBA", "NBA"} else ""

    if not sport:
        return hydrated

    hydrated["strategy_sport"] = sport
    hydrated["strategy_source"] = (
        hydrated.get("strategy_source")
        or f"{sport} Monitor - {sport}"
    )
    if event_id:
        hydrated["strategy_pick_id"] = (
            hydrated.get("strategy_pick_id") or event_id
        )
    if selection:
        hydrated["strategy_selection"] = (
            hydrated.get("strategy_selection") or selection
        )

    pw = parsed.get("pw") or {}
    posted_at = (
        hydrated.get("strategy_posted_at")
        or meta.get("event_ts")
        or pw.get("event_ts")
        or (alert or {}).get("received_at")
    )
    if posted_at:
        hydrated["strategy_posted_at"] = posted_at

    return hydrated


def _retry_asset_id(payload: dict[str, Any]) -> str:
    intent = core.TradeIntent(
        market_url=str(payload.get("market_url") or ""),
        outcome=str(payload.get("outcome") or ""),
        market_type=str(payload.get("market_type") or "moneyline"),
        max_price=Decimal(str(payload.get("max_price") or "0")),
        budget_usdc=Decimal(str(payload.get("budget_usdc") or "0")),
        note="Retry failed Slack live BUY",
    )
    with ingest.PublicClient() as client:
        market = core._select_market(client, intent)
        asset_id, _, _ = core._resolve_asset(market, intent.outcome)
    return str(asset_id)


def _open_orders_for_asset(asset_id: str) -> list[Any]:
    """Authenticated CLOB reconciliation before any failed BUY is resent."""
    with live_trading._secure_client() as client:
        try:
            return list(client.list_open_orders(asset_id=asset_id).iter_items())
        except TypeError:
            # Compatibility fallback for SDK builds that do not accept asset_id
            # as a server-side filter. Filter the authenticated open-order list.
            orders = list(client.list_open_orders().iter_items())
            return [
                order
                for order in orders
                if str(
                    getattr(order, "asset_id", None)
                    or getattr(order, "token_id", None)
                    or ""
                ) == str(asset_id)
            ]


def _retry_failed_slack_buy_once(request_id: str | None = None) -> dict[str, Any]:
    request_id = str(request_id or os.getenv("SLACK_RETRY_REQUEST_ID", "")).strip()
    if not request_id:
        return {"status": "disabled"}

    if not (core.bot_enabled() and core.live_trading_enabled() and core.auto_trading_enabled()):
        return {"status": "waiting_gates"}

    ready, state = _executor_ready()
    if not ready:
        return {
            "status": "waiting_executor",
            "geo_blocked": bool(state.get("geo_blocked")),
        }

    queue = remote._queue_load()
    original = queue.get(request_id)
    if not isinstance(original, dict):
        return {"status": "missing_request"}
    if original.get("manual_retry_request_id"):
        return {
            "status": "already_retried",
            "request_id": original.get("manual_retry_request_id"),
            "trade_id": original.get("manual_retry_trade_id"),
        }

    payload = _hydrate_monitor_retry_payload(
        dict(original.get("payload") or {})
    )
    error = str(original.get("error") or "")
    interrupted_after_start = "interrupted after execution began" in error.lower()
    normalized_error = error.replace("-", "_").upper()
    connect_timeout = (
        "CONNECTTIMEOUT" in normalized_error.replace("_", "")
        or "CONNECT_TIMEOUT" in normalized_error
    )
    # This helper is only invoked for an explicit one-shot manual resend request.
    # Generic TransportError timeouts are therefore eligible only here, and only
    # after the wallet/open-order reconciliation below proves the asset is clear.
    manual_transport_timeout = (
        type(error).__name__ == "str"
        and "TRANSPORTERROR" in normalized_error.replace("_", "")
        and "TIMED OUT" in normalized_error
    )
    wrapper_signature_error = (
        "TYPEERROR" in normalized_error.replace("_", "")
        and "_BUY()" in normalized_error
        and "UNEXPECTED KEYWORD ARGUMENT" in normalized_error
        and "REQUEST_ID" in normalized_error
    )
    if (
        original.get("action") != "BUY"
        or original.get("status") != "FAILED"
        or payload.get("source") != "slack_live"
        or not (
            connect_timeout
            or interrupted_after_start
            or manual_transport_timeout
            or wrapper_signature_error
        )
    ):
        return {"status": "not_retryable"}

    market_url = str(payload.get("market_url") or "")
    outcome = str(payload.get("outcome") or "")
    if not market_url or not outcome:
        return {"status": "invalid_payload"}

    try:
        asset_id = _retry_asset_id(payload)
    except Exception as exc:
        return {"status": "market_resolution_failed", "error": f"{type(exc).__name__}: {exc}"}

    # Duplicate attribution must come from this bot's own execution/queue
    # records, not from total wallet shares or manual/external open orders.
    signal_key = _pw_signal_key(payload)
    if _active_or_pending(
        asset_id,
        market_url,
        outcome,
        signal_key=signal_key or None,
    ):
        return {
            "status": "duplicate_active",
            "asset_id": asset_id,
            "signal_key": signal_key or None,
        }

    new_trade_id = f"{payload.get('trade_id') or 'slack-live'}-retry-{uuid.uuid4().hex[:6]}"
    new_request_id = f"exec-{uuid.uuid4().hex[:14]}"
    retry_payload = dict(payload)
    retry_payload.update({
        "trade_id": new_trade_id,
        "asset_id": asset_id,
        "limit_order_ttl_seconds": 120,
        "retry_of_request_id": request_id,
        "retry_reason": (
            "manual_resend_after_interrupted_execution_reconciled"
            if interrupted_after_start
            else "manual_resend_after_connect_timeout"
        ),
    })
    now_iso = ingest._now_iso()
    record = {
        "id": new_request_id,
        "action": "BUY",
        "status": "PENDING",
        "payload": retry_payload,
        "created_at": now_iso,
        "created_unix": time.time(),
        "updated_at": now_iso,
    }

    with remote._QUEUE_LOCK:
        data = remote._queue_load()
        latest = data.get(request_id)
        if not isinstance(latest, dict):
            return {"status": "missing_request"}
        latest["payload"] = _hydrate_monitor_retry_payload(
            dict(latest.get("payload") or {})
        )
        if latest.get("manual_retry_request_id"):
            return {
                "status": "already_retried",
                "request_id": latest.get("manual_retry_request_id"),
                "trade_id": latest.get("manual_retry_trade_id"),
            }
        for existing in data.values():
            ep = existing.get("payload") or {}
            if not (
                existing.get("action") == "BUY"
                and existing.get("status") in {"PENDING", "LEASED"}
                and ep.get("source") == "slack_live"
                and str(ep.get("market_url") or "") == market_url
                and str(ep.get("outcome") or "").casefold() == outcome.casefold()
            ):
                continue
            if signal_key and _pw_signal_key(ep) != signal_key:
                continue
            return {
                "status": "duplicate_active",
                "request_id": existing.get("id"),
                "signal_key": signal_key or None,
            }

        data[new_request_id] = record
        latest["manual_retry_request_id"] = new_request_id
        latest["manual_retry_trade_id"] = new_trade_id
        latest["manual_retry_at"] = now_iso
        latest["updated_at"] = now_iso
        data[request_id] = latest
        remote._queue_save(data)

    print(
        "SLACK_FAILED_BUY_REQUEUED "
        f"original={request_id} request={new_request_id} trade={new_trade_id} "
        f"outcome={outcome} asset_id={asset_id}",
        flush=True,
    )
    return {
        "status": "queued",
        "request_id": new_request_id,
        "trade_id": new_trade_id,
        "asset_id": asset_id,
        "outcome": outcome,
    }


def _run_requested_retry_worker() -> None:
    request_id = os.getenv("SLACK_RETRY_REQUEST_ID", "").strip()
    if not request_id:
        return
    terminal = {
        "queued",
        "already_retried",
        "duplicate_active",
        "missing_request",
        "not_retryable",
        "invalid_payload",
    }
    for _ in range(30):
        result = _retry_failed_slack_buy_once(request_id)
        print(
            f"SLACK_FAILED_BUY_RETRY status={result.get('status')} request={request_id}",
            flush=True,
        )
        if result.get("status") in terminal:
            return
        time.sleep(3)


@app.get("/api/slack/trading-mode", dependencies=[Depends(dashboard._auth)])
def slack_trading_mode_get():
    mode = _mode()
    ready, state = _executor_ready()
    last_seen = float(state.get("last_seen_unix") or 0)
    connected = bool(
        state.get("token_hash")
        and last_seen
        and time.time() - last_seen <= remote.CONNECTED_SECONDS
    )
    return {
        **mode,
        "paper_only": not mode["auto_prepare_enabled"],
        "executor_ready": ready,
        "executor_connected": connected,
        "executor_geo_blocked": state.get("geo_blocked"),
        "executor_country": state.get("geo_country"),
        "executor_region": state.get("geo_region"),
        "max_stake_usdc": str(core.MAX_AUTO_TRADE_USDC),
        "moneyline_only": True,
        "duplicate_position_guard": True,
        "unattended_live_execution": bool(core.auto_trading_enabled()),
        "approval_required": not bool(core.auto_trading_enabled()),
    }


@app.get("/api/slack/pending-live", dependencies=[Depends(dashboard._auth)])
def slack_pending_live():
    queue = remote._queue_load()
    rows = []
    for rec in queue.values():
        payload = rec.get("payload") or {}
        if rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
            continue
        rows.append({
            "request_id": rec.get("id"),
            "created_at": rec.get("created_at"),
            "market_url": payload.get("market_url"),
            "outcome": payload.get("outcome"),
            "max_price": payload.get("max_price"),
            "budget_usdc": payload.get("budget_usdc"),
            "slack_event_id": payload.get("slack_event_id"),
            "source": payload.get("source"),
            "strategy_source": payload.get("strategy_source"),
            "strategy_sport": payload.get("strategy_sport"),
            "strategy_selection": payload.get("strategy_selection"),
            "approval_reason": payload.get("approval_reason"),
            "approval_message": payload.get("approval_message"),
            "signal_decimal_odds": payload.get("signal_decimal_odds"),
            "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
        })
    rows.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"pending": rows[:50]}


def _sync_approval_source_state(
    request_id: str,
    *,
    approved: bool,
    decided_at: str,
) -> None:
    """Keep sport signal cards in sync with the executor approval queue."""
    signal_status = "QUEUED" if approved else "APPROVAL_REJECTED"
    for filename in ("nfl_capper_signals.json", "cfb_capper_preview_signals.json"):
        path = core.DATA_DIR / filename
        data = core._load(path)
        if not isinstance(data, dict):
            continue
        changed = False
        for key, row in data.items():
            if not isinstance(row, dict) or str(row.get("request_id") or "") != request_id:
                continue
            row["status"] = signal_status
            row["approval_required"] = False
            row["approval_decision"] = "APPROVED" if approved else "REJECTED"
            row["approval_decided_at"] = decided_at
            row["updated_at"] = decided_at
            data[key] = row
            changed = True
        if changed:
            core._save(path, data)

    alerts = core._load(ingest.SLACK_ALERTS_FILE)
    if isinstance(alerts, dict):
        changed = False
        for key, row in alerts.items():
            if not isinstance(row, dict):
                continue
            live_trade = row.get("live_trade") or {}
            row_request_id = str(
                row.get("executor_request_id")
                or live_trade.get("request_id")
                or ""
            )
            if row_request_id != request_id:
                continue
            row["status"] = "LIVE_TRADE_QUEUED" if approved else "LIVE_TRADE_REJECTED"
            if isinstance(live_trade, dict):
                live_trade["requires_approval"] = False
                live_trade["queued"] = approved
                live_trade["approval_decision"] = "APPROVED" if approved else "REJECTED"
                live_trade["approval_decided_at"] = decided_at
                row["live_trade"] = live_trade
            alerts[key] = row
            changed = True
        if changed:
            core._save(ingest.SLACK_ALERTS_FILE, alerts)


def _waiting_buy_live_quote(rec: dict[str, Any]) -> dict[str, Any]:
    payload = rec.get("payload") or {}
    asset_id = str(payload.get("asset_id") or "").strip()
    if not asset_id:
        raise HTTPException(status_code=409, detail="Prepared BUY has no exact Polymarket asset id")
    try:
        with ingest.PublicClient() as client:
            book = client.get_order_book(asset_id=asset_id)
            spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        asks = getattr(book, "asks", None) or []
        best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not refresh live Polymarket quote: {type(exc).__name__}: {exc}") from exc
    if best_ask is None or best_ask <= 0 or best_ask >= 1:
        raise HTTPException(status_code=409, detail="No valid live Polymarket ask is available")
    # Wide spreads remain visible as explicit approval opportunities.
    # They never enter the executor automatically; approval stamps a one-request override.
    decimal_odds = remote._decimal_odds_from_price(best_ask)
    target_raw = payload.get("strategy_target_profit_usdc")
    budget = Decimal(str(payload.get("budget_usdc") or "0"))
    if target_raw not in {None, ""}:
        target = Decimal(str(target_raw))
        budget = (target * best_ask / (Decimal("1") - best_ask)).quantize(Decimal("0.01"), rounding=ROUND_UP)
    if budget <= 0:
        raise HTTPException(status_code=409, detail="Prepared BUY has an invalid stake")
    return {
        "best_ask": best_ask,
        "spread": spread,
        "decimal_odds": decimal_odds,
        "budget_usdc": budget,
        "polymarket_cents": (best_ask * Decimal("100")).quantize(Decimal("0.1")),
        "auto_trade_cap_usdc": Decimal(str(core.MAX_AUTO_TRADE_USDC)),
        "budget_cap_exceeded": budget > Decimal(str(core.MAX_AUTO_TRADE_USDC)),
    }


@app.get("/api/executor/approval-quote/{request_id}", dependencies=[Depends(dashboard._auth)])
def waiting_buy_approval_quote(request_id: str):
    rec = remote._queue_load().get(request_id)
    if not rec or rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
        raise HTTPException(status_code=404, detail="Prepared BUY is not awaiting approval")
    live = _waiting_buy_live_quote(rec)
    payload = rec.get("payload") or {}
    return {
        "ok": True,
        "request_id": request_id,
        "decimal_odds": str(live["decimal_odds"]) if live["decimal_odds"] is not None else None,
        "polymarket_price": str(live["best_ask"]),
        "polymarket_cents": str(live["polymarket_cents"]),
        "spread": str(live["spread"]),
        "budget_usdc": str(live["budget_usdc"]),
        "auto_trade_cap_usdc": str(live["auto_trade_cap_usdc"]),
        "budget_cap_exceeded": bool(live["budget_cap_exceeded"]),
        "target_profit_usdc": payload.get("strategy_target_profit_usdc"),
        "units": payload.get("strategy_units"),
        "approval_reason": payload.get("approval_reason"),
    }

def _approve_waiting_buy(request_id: str) -> dict[str, Any]:
    ready, state = _executor_ready()
    if not ready:
        if state.get("geo_blocked"):
            raise HTTPException(status_code=409, detail="Termux executor is geoblocked")
        raise HTTPException(status_code=409, detail="Termux executor is not connected")

    initial = remote._queue_load().get(request_id)
    if not initial or initial.get("action") != "BUY" or initial.get("status") != "WAITING_APPROVAL":
        raise HTTPException(status_code=404, detail="Prepared BUY is not awaiting approval")
    live = _waiting_buy_live_quote(initial)
    if bool(live.get("budget_cap_exceeded")):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Current live quote requires ${live['budget_usdc']} stake, above "
                f"the dashboard Auto trade cap ${live['auto_trade_cap_usdc']}. "
                "Raise the cap explicitly before approving this BUY."
            ),
        )

    with remote._QUEUE_LOCK:
        queue = remote._queue_load()
        rec = queue.get(request_id)
        if not rec or rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
            raise HTTPException(status_code=404, detail="Prepared BUY is not awaiting approval")
        payload = dict(rec.get("payload") or {})
        live_price = Decimal(str(live["best_ask"]))
        payload["max_price"] = str(live_price)
        payload["signal_buy_price"] = str(live_price)
        payload["signal_decimal_odds"] = str(live["decimal_odds"])
        payload["budget_usdc"] = str(live["budget_usdc"])
        payload["approved_live_price"] = str(live_price)
        payload["approved_live_decimal_odds"] = str(live["decimal_odds"])
        payload["approved_price_override"] = live_price > core.MAX_PRICE
        live_spread = Decimal(str(live["spread"]))
        payload["approved_spread_override"] = live_spread > core.MAX_SPREAD
        # Explicit approval widens only this request; dashboard defaults stay unchanged.
        payload["max_price_global"] = str(max(core.MAX_PRICE, live_price))
        payload["max_spread"] = str(max(core.MAX_SPREAD, live_spread))
        payload["signal_spread"] = str(live_spread)
        decided_at = ingest._now_iso()
        payload["approved_at"] = decided_at
        rec["payload"] = payload
        rec["status"] = "PENDING"
        rec["approved_at"] = decided_at
        rec["updated_at"] = decided_at
        queue[request_id] = rec
        remote._queue_save(queue)
    _sync_approval_source_state(request_id, approved=True, decided_at=decided_at)
    return {
        "ok": True,
        "request_id": request_id,
        "status": "PENDING",
        "decimal_odds": str(live["decimal_odds"]) if live["decimal_odds"] is not None else None,
        "polymarket_price": str(live["best_ask"]),
        "budget_usdc": str(live["budget_usdc"]),
    }


def _reject_waiting_buy(request_id: str) -> dict[str, Any]:
    with remote._QUEUE_LOCK:
        queue = remote._queue_load()
        rec = queue.get(request_id)
        if not rec or rec.get("action") != "BUY" or rec.get("status") != "WAITING_APPROVAL":
            raise HTTPException(status_code=404, detail="Prepared BUY is not awaiting approval")
        decided_at = ingest._now_iso()
        rec["status"] = "CANCELLED"
        rec["rejected_at"] = decided_at
        rec["updated_at"] = decided_at
        queue[request_id] = rec
        remote._queue_save(queue)
    _sync_approval_source_state(request_id, approved=False, decided_at=decided_at)
    return {"ok": True, "request_id": request_id, "status": "CANCELLED"}


@app.post("/api/executor/approve-buy/{request_id}", dependencies=[Depends(dashboard._auth)])
def approve_waiting_buy(request_id: str):
    return _approve_waiting_buy(request_id)


@app.post("/api/executor/reject-buy/{request_id}", dependencies=[Depends(dashboard._auth)])
def reject_waiting_buy(request_id: str):
    return _reject_waiting_buy(request_id)


@app.post("/api/slack/approve-live/{request_id}", dependencies=[Depends(dashboard._auth)])
def slack_approve_live(request_id: str):
    return _approve_waiting_buy(request_id)


@app.post("/api/slack/reject-live/{request_id}", dependencies=[Depends(dashboard._auth)])
def slack_reject_live(request_id: str):
    return _reject_waiting_buy(request_id)


@app.put("/api/slack/trading-mode", dependencies=[Depends(dashboard._auth)])
def slack_trading_mode_put(req: SlackTradingMode):
    if req.stake_usdc > core.MAX_AUTO_TRADE_USDC:
        raise HTTPException(
            status_code=400,
            detail=f"Slack stake cannot exceed the dashboard Auto trade cap of {core.MAX_AUTO_TRADE_USDC} USDC",
        )
    saved = _save_mode(req.live_enabled, req.stake_usdc)
    return {
        "ok": True,
        **saved,
        "paper_only": not saved["auto_prepare_enabled"],
        "unattended_live_execution": False,
        "approval_required": True,
    }


def _install_slack_live_controls() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="slackTradingMode"' in html:
        return

    box = """
    <div class="slack-mode-box">
      <div class="slack-mode-head">
        <div><div class="label">Slack NBA/WNBA live auto-prepare</div><div class="slack-mode-value" id="slackTradingMode">Checking…</div></div>
        <div class="slack-mode-state" id="slackExecutorState">Executor status…</div>
      </div>
      <div class="slack-mode-controls">
        <label>Stake per Slack alert (USDC, max <span id="slackStakeMax">—</span>)</label>
        <input id="slackLiveStake" type="number" min="0.01" step="0.01" value="5.00">
        <button type="button" class="mode-paper-btn" id="slackPaperBtn">PAPER</button>
        <button type="button" class="mode-live-btn" id="slackLiveBtn">ENABLE AUTO-PREPARE</button>
      </div>
      <div class="slack-mode-note">AUTO-PREPARE builds qualifying Predicted Winner moneyline orders from Slack alerts and places them in the approval queue. The Configuration → Auto trade cap is the maximum stake. No real order is sent to the Termux executor until you approve that specific order.</div><div id="slackPendingApprovals" class="slack-pending"></div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', box + '<form id="settingsForm">', 1)

    css = """
.slack-mode-box{border:1px solid var(--border);border-radius:12px;background:#0d1522;padding:14px;margin:0 0 16px}.slack-pending{margin-top:12px}.slack-pending-row{display:flex;justify-content:space-between;gap:10px;align-items:center;border-top:1px solid var(--border);padding:9px 0}.slack-pending-actions{display:flex;gap:6px}.slack-approve-btn,.slack-reject-btn{border-radius:8px;padding:6px 9px;font-size:11px;font-weight:850;cursor:pointer}.slack-approve-btn{border:1px solid #2e7d64;background:#12362d;color:#a7f3d0}.slack-reject-btn{border:1px solid #7f1d1d;background:#3f1118;color:#fecdd3}.slack-mode-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.slack-mode-value{font-size:19px;font-weight:900;margin-top:5px}.slack-mode-state{font-size:11px;color:var(--muted);text-align:right}.slack-mode-controls{display:flex;gap:9px;align-items:end;flex-wrap:wrap;margin-top:12px}.slack-mode-controls label{width:100%;font-size:10px;color:var(--muted);text-transform:uppercase;font-weight:800;letter-spacing:.07em}.slack-mode-controls input{width:145px;background:#080e18;border:1px solid #344157;color:#fff;border-radius:9px;padding:9px;font:inherit}.mode-paper-btn,.mode-live-btn{border-radius:9px;padding:9px 13px;font-weight:850;cursor:pointer}.mode-paper-btn{border:1px solid #3d4d67;background:#172033;color:#fff}.mode-live-btn{border:1px solid #7f1d1d;background:#3f1118;color:#fecdd3}.mode-live-btn.active{background:#7f1d1d;color:#fff}.mode-paper-btn.active{border-color:#2e7d64;background:#12362d;color:#a7f3d0}.slack-mode-note{font-size:10px;color:var(--muted);margin-top:9px;line-height:1.45}@media(max-width:650px){.slack-mode-head{align-items:flex-start;flex-direction:column}.slack-mode-state{text-align:left}}
"""
    html = html.replace("</style>", css + "</style>", 1)

    js = r"""
async function loadSlackTradingMode(){
 try{
  const r=await fetch('/api/slack/trading-mode',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Could not load Slack mode');
  const live=!!d.auto_prepare_enabled;
  const mode=document.getElementById('slackTradingMode'),state=document.getElementById('slackExecutorState'),stake=document.getElementById('slackLiveStake');
  mode.textContent=d.railway_override?'LIVE AUTO · RAILWAY OVERRIDE':(live?'LIVE AUTO-PREPARE · APPROVAL REQUIRED':'PAPER ONLY');
  mode.className='slack-mode-value '+(live?'red':'green');
  state.textContent=(d.executor_connected?'Termux connected':'Termux offline')+(d.executor_geo_blocked?' · BLOCKED':'')+(d.executor_country?' · '+d.executor_country+'/'+(d.executor_region||''):'');
  try{const pr=await fetch('/api/slack/pending-live',{cache:'no-store'}),pd=await pr.json();const box=document.getElementById('slackPendingApprovals');const rows=(pd.pending||[]);box.innerHTML=rows.length?'<div class="label" style="margin-bottom:5px">Awaiting approval</div>'+rows.map(x=>{const who=x.strategy_source||x.strategy_sport||x.source||'Sports bot';const pick=x.strategy_selection||x.outcome||'Order';const odds=x.signal_decimal_odds?(' · odds '+Number(x.signal_decimal_odds).toFixed(2)):'';const gate=x.approval_reason==='MIN_ODDS'?(' · MIN '+Number(x.minimum_decimal_odds||1.70).toFixed(2)):'';return `<div class="slack-pending-row"><div><b>${who} · ${pick}</b><div class="muted">${Number(x.budget_usdc||0).toFixed(2)} · max ${Number(x.max_price||0).toFixed(3)}${odds}${gate}</div></div><div class="slack-pending-actions"><button class="slack-approve-btn" data-slack-approve="${x.request_id}">APPROVE</button><button class="slack-reject-btn" data-slack-reject="${x.request_id}">REJECT</button></div></div>`}).join(''):''}catch(_e){}
  if(stake&&!stake.dataset.dirty)stake.value=d.stake_usdc;
  document.getElementById('slackStakeMax').textContent='$'+Number(d.max_stake_usdc).toFixed(2);
  document.getElementById('slackPaperBtn').classList.toggle('active',!live);
  document.getElementById('slackLiveBtn').classList.toggle('active',live);
 }catch(e){
  const m=document.getElementById('slackTradingMode');
  if(m)m.textContent='Mode error: '+String(e);
 }
}
async function setSlackTradingMode(live){
 const stake=document.getElementById('slackLiveStake').value;
 if(live&&!confirm('Enable LIVE AUTO-PREPARE?'))return;
 try{
  const r=await fetch('/api/slack/trading-mode',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({live_enabled:live,stake_usdc:stake})});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Mode change failed');
  delete document.getElementById('slackLiveStake').dataset.dirty;
  await loadSlackTradingMode();
 }catch(e){alert('Slack trading mode change failed: '+String(e))}
}
document.getElementById('slackLiveStake').addEventListener('input',function(e){e.target.dataset.dirty='1'});
document.getElementById('slackPaperBtn').addEventListener('click',function(){setSlackTradingMode(false)});
document.getElementById('slackLiveBtn').addEventListener('click',function(){setSlackTradingMode(true)});
document.addEventListener('click',async function(ev){const a=ev.target.closest('[data-slack-approve]'),r=ev.target.closest('[data-slack-reject]');if(!a&&!r)return;const id=(a||r).getAttribute(a?'data-slack-approve':'data-slack-reject');if(a&&!confirm('Approve this prepared live order for execution?'))return;try{const resp=await fetch(a?'/api/slack/approve-live/'+encodeURIComponent(id):'/api/slack/reject-live/'+encodeURIComponent(id),{method:'POST'}),d=await resp.json();if(!resp.ok)throw new Error(d.detail||'Action failed');await loadSlackTradingMode()}catch(e){alert(String(e))}});
loadSlackTradingMode();
setInterval(loadSlackTradingMode,5000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_slack_live_controls()


def _start_requested_retry_worker() -> None:
    if not os.getenv("SLACK_RETRY_REQUEST_ID", "").strip():
        return
    threading.Thread(
        target=_run_requested_retry_worker,
        name="slack-failed-buy-retry",
        daemon=True,
    ).start()


# This application stack uses a custom lifespan and does not execute router
# on_startup callbacks reliably. Start the one-shot retry worker at runtime
# module import instead, while explicitly suppressing it under the Railway
# pre-deploy unittest process so tests can never enqueue a live BUY.
if "unittest" not in sys.modules:
    _start_requested_retry_worker()
