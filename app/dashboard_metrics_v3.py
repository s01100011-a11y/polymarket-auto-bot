from __future__ import annotations

import os
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException

from app import termux_executor_dashboard_v2 as base

app = base.app
dashboard = base.dashboard
core = base.core
ingest = base.ingest
remote = base.base

_RECONCILE_LOCK = threading.Lock()
_RECONCILE_LAST = 0.0
_RECONCILE_MIN_SECONDS = 5.0
_EPS = Decimal("0.0001")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _wallet_for_reconciliation() -> str | None:
    # Railway keeps the full deposit/funder address. The Termux heartbeat stores
    # a masked display value, which must never be sent back to Polymarket APIs.
    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if wallet and "…" not in wallet and wallet.startswith("0x") and len(wallet) == 42:
        return wallet
    try:
        state = remote._state()
        candidate = str(state.get("wallet") or "").strip()
        if candidate and "…" not in candidate and candidate.startswith("0x") and len(candidate) == 42:
            return candidate
    except Exception:
        pass
    return None


def _full_wallet_positions(wallet: str) -> dict[str, Any]:
    positions: dict[str, Any] = {}
    with ingest.PublicClient() as client:
        paginator = client.list_positions(user=wallet, full_history=True, page_size=100)
        for pos in paginator.iter_items():
            asset_id = str(getattr(pos, "asset_id", None) or getattr(pos, "token_id", None) or "")
            if asset_id:
                positions[asset_id] = pos
    return positions


def _executor_confirmed_closed_trade_ids() -> set[str]:
    """Return trades for which Termux already confirmed the tracked wallet shares are zero."""
    marker = "Wallet no longer holds the tracked test shares"
    try:
        queue = remote._queue_load()
    except Exception:
        return set()
    closed: set[str] = set()
    for item in queue.values():
        if item.get("action") != "SELL" or item.get("status") != "FAILED":
            continue
        if marker not in str(item.get("error") or ""):
            continue
        trade_id = str((item.get("payload") or {}).get("trade_id") or "")
        if trade_id:
            closed.add(trade_id)
    return closed


def _reconcile_live_positions(force: bool = False) -> dict[str, Any]:
    global _RECONCILE_LAST
    now = time.time()
    if not force and now - _RECONCILE_LAST < _RECONCILE_MIN_SECONDS:
        return {"ok": True, "skipped": "recent"}

    with _RECONCILE_LOCK:
        now = time.time()
        if not force and now - _RECONCILE_LAST < _RECONCILE_MIN_SECONDS:
            return {"ok": True, "skipped": "recent"}

        executions = core._load(core.EXECUTIONS_FILE)
        active = [
            rec
            for rec in executions.values()
            if rec.get("source") == "termux_executor"
            and not rec.get("paper")
            and rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}
        ]
        if not active:
            _RECONCILE_LAST = now
            return {"ok": True, "checked": 0, "changed": 0}

        changed = 0
        checked = 0
        stamp = _now_iso()
        executor_confirmed_closed = _executor_confirmed_closed_trade_ids()
        unresolved: list[dict[str, Any]] = []
        for rec in active:
            if str(rec.get("id") or "") not in executor_confirmed_closed:
                unresolved.append(rec)
                continue
            rec["status"] = "CLOSED_RECONCILED"
            rec["remaining_shares"] = "0"
            rec["closed_at"] = rec.get("closed_at") or stamp
            rec["wallet_reconciled_at"] = stamp
            rec["wallet_position_size"] = "0"
            rec["reconciliation_note"] = (
                "Termux checked the Polymarket wallet before SELL and found zero tracked shares. "
                "The position was already closed outside this SELL request; exit price and realized P/L remain unknown."
            )
            changed += 1

        if changed:
            core._save(core.EXECUTIONS_FILE, executions)
        if not unresolved:
            _RECONCILE_LAST = now
            return {"ok": True, "checked": len(active), "changed": changed}

        wallet = _wallet_for_reconciliation()
        if not wallet:
            if changed:
                _RECONCILE_LAST = now
            return {
                "ok": bool(changed),
                "reason": "Termux wallet is not known yet",
                "checked": len(active) - len(unresolved),
                "changed": changed,
            }

        try:
            positions = _full_wallet_positions(wallet)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "changed": changed}

        for rec in unresolved:
            q = rec.get("quote") or {}
            asset_id = str(q.get("asset_id") or "")
            if not asset_id or asset_id not in positions:
                continue

            checked += 1
            pos = positions[asset_id]
            wallet_size = _d(getattr(pos, "current_size", "0"))
            pos_status = str(getattr(pos, "status", "") or "")
            pre_size = _d(rec.get("pre_position_size"))
            filled = _d(rec.get("filled_shares") or q.get("shares"))
            old_remaining = _d(
                rec.get("remaining_shares")
                if rec.get("remaining_shares") is not None
                else filled
            )

            attributable = max(Decimal("0"), wallet_size - pre_size)
            if filled > 0:
                attributable = min(attributable, filled)

            rec["wallet_reconciled_at"] = stamp
            rec["wallet_position_size"] = str(wallet_size)
            rec["wallet_position_status"] = pos_status

            if attributable <= _EPS:
                rec["status"] = "CLOSED_RECONCILED"
                rec["remaining_shares"] = "0"
                rec["closed_at"] = rec.get("closed_at") or stamp
                rec["reconciliation_note"] = (
                    "Polymarket wallet position is closed. Realized P/L is left unknown "
                    "unless this bot recorded the exit execution."
                )
                changed += 1
            elif old_remaining > 0 and attributable + _EPS < old_remaining:
                rec["status"] = "PARTIALLY_CLOSED"
                rec["remaining_shares"] = str(attributable)
                rec["reconciliation_note"] = "Remaining shares reconciled from Polymarket wallet holdings."
                changed += 1

        if changed:
            core._save(core.EXECUTIONS_FILE, executions)
        _RECONCILE_LAST = now
        return {"ok": True, "checked": checked, "changed": changed}


@app.post("/api/dashboard/reconcile", dependencies=[Depends(dashboard._auth)])
def reconcile_now():
    return _reconcile_live_positions(force=True)


@app.post("/api/paper/close/{trade_id}", dependencies=[Depends(dashboard._auth)])
def close_paper_trade(trade_id: str):
    executions = core._load(core.EXECUTIONS_FILE)
    rec = executions.get(trade_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Unknown paper trade")
    if not rec.get("paper"):
        raise HTTPException(status_code=400, detail="This endpoint only closes paper trades")
    if rec.get("status") != "PAPER_OPEN":
        raise HTTPException(status_code=400, detail=f"Paper trade status is {rec.get('status')}")

    q = rec.get("quote") or {}
    asset_id = str(q.get("asset_id") or "")
    shares = _d(
        rec.get("remaining_shares")
        if rec.get("remaining_shares") is not None
        else q.get("shares")
    )
    entry = _d(q.get("paper_entry_price") or q.get("entry_price") or q.get("limit_price"))
    if not asset_id or shares <= 0 or entry <= 0:
        raise HTTPException(status_code=400, detail="Paper trade is missing asset, shares, or entry price")

    try:
        with ingest.PublicClient() as client:
            exit_price = _d(client.get_price(asset_id=asset_id, side="SELL"))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Could not get current SELL price: {exc}") from exc

    if exit_price <= 0 or exit_price >= 1:
        raise HTTPException(status_code=502, detail=f"Invalid current SELL price {exit_price}")

    realized = ((exit_price - entry) * shares).quantize(Decimal("0.01"))
    proceeds = (exit_price * shares).quantize(Decimal("0.01"))
    stamp = _now_iso()

    rec["status"] = "PAPER_CLOSED"
    rec["exit_price"] = str(exit_price)
    rec["realized_pnl"] = str(realized)
    rec["remaining_shares"] = "0"
    rec["closed_at"] = stamp
    rec.setdefault("execution", {})["paper_close"] = {
        "at": stamp,
        "exit_price": str(exit_price),
        "shares": str(shares),
        "proceeds_usdc": str(proceeds),
    }
    executions[trade_id] = rec

    sell_id = f"{trade_id}-paper-sell-{uuid.uuid4().hex[:6]}"
    executions[sell_id] = {
        "id": sell_id,
        "status": "PAPER_SELL",
        "created_at": stamp,
        "submitted_at": stamp,
        "side": "SELL",
        "auto": False,
        "paper": True,
        "source": rec.get("source") or "paper",
        "parent_trade_id": trade_id,
        "display_pnl": str(realized),
        "budget_usdc": str(proceeds),
        "quote": {
            "market": q.get("market"),
            "market_url": q.get("market_url"),
            "market_type": q.get("market_type"),
            "requested_outcome": q.get("requested_outcome"),
            "resolved_outcome": q.get("resolved_outcome"),
            "asset_id": asset_id,
            "limit_price": str(exit_price),
            "shares": str(shares),
        },
        "execution": {
            "placed": False,
            "paper": True,
            "reason": "Paper position closed at the current Polymarket SELL price; no real order was sent.",
        },
    }
    core._save(core.EXECUTIONS_FILE, executions)

    return {
        "ok": True,
        "trade_id": trade_id,
        "status": "PAPER_CLOSED",
        "exit_price": str(exit_price),
        "shares": str(shares),
        "realized_pnl": str(realized),
        "proceeds_usdc": str(proceeds),
    }


_BASE_SNAPSHOT = dashboard._dashboard_snapshot


def _performance_metrics(executions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    wins = 0
    losses = 0
    pushes = 0
    realized_total = Decimal("0")
    realized_7d = Decimal("0")
    realized_30d = Decimal("0")
    stake_total = Decimal("0")
    graded = 0
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)

    for rec in executions.values():
        if rec.get("parent_trade_id"):
            continue
        if rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
            continue

        realized = base.pnl_base._explicit_realized_pnl(rec)
        if realized is None:
            continue

        stake = _d(rec.get("budget_usdc"))
        realized_total += realized

        realized_at = None
        for raw_time in (
            rec.get("closed_at"),
            rec.get("updated_at"),
            rec.get("submitted_at"),
            rec.get("created_at"),
        ):
            if not raw_time:
                continue
            try:
                parsed = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            realized_at = parsed.astimezone(timezone.utc)
            break
        if realized_at is not None:
            if realized_at >= cutoff_30d:
                realized_30d += realized
            if realized_at >= cutoff_7d:
                realized_7d += realized

        if stake > 0:
            stake_total += stake
        graded += 1

        if realized > 0:
            wins += 1
        elif realized < 0:
            losses += 1
        else:
            pushes += 1

    decided = wins + losses
    accuracy = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
    roi = (realized_total / stake_total * Decimal("100")) if stake_total > 0 else None

    return {
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "graded_trades": graded,
        "graded_stake_usdc": str(stake_total.quantize(Decimal("0.01"))),
        "realized_pnl_for_metrics": str(realized_total.quantize(Decimal("0.01"))),
        "realized_pnl_7d_usdc": str(realized_7d.quantize(Decimal("0.01"))),
        "realized_pnl_30d_usdc": str(realized_30d.quantize(Decimal("0.01"))),
        "accuracy_pct": str(accuracy.quantize(Decimal("0.1"))) if accuracy is not None else None,
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
    }


def _metric_time(rec: dict[str, Any]) -> datetime | None:
    for raw_time in (
        rec.get("closed_at"),
        rec.get("updated_at"),
        rec.get("submitted_at"),
        rec.get("created_at"),
    ):
        if not raw_time:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _capper_name(source: Any, sport: Any) -> str | None:
    name = str(source or "").strip()
    sport_name = str(sport or "").strip().upper()
    if not name:
        return None
    suffix = f" - {sport_name}" if sport_name else ""
    if suffix and name.upper().endswith(suffix):
        return name[: -len(suffix)].strip() or name
    return name


def _legacy_monitor_identity(rec: dict[str, Any]) -> tuple[str, str]:
    source = str(rec.get("strategy_source") or "").strip()
    sport = str(rec.get("strategy_sport") or "").strip().upper()
    if source or sport:
        return source, sport

    event_id = str(rec.get("slack_event_id") or rec.get("strategy_pick_id") or "")
    if not event_id.startswith("pwexport-"):
        return source, sport

    decision = rec.get("pw_strategy_decision") or {}
    quote = rec.get("quote") or {}
    selection = (
        rec.get("strategy_selection")
        or decision.get("predicted_winner")
        or quote.get("requested_outcome")
        or quote.get("resolved_outcome")
    )
    league = str(ingest._league_for_team(str(selection or "")) or "").upper()
    if league in {"WNBA", "NBA"}:
        return f"{league} Monitor - {league}", league
    return source, sport


def _more_stats_payload(mode: str = "live") -> dict[str, Any]:
    mode = str(mode or "live").strip().lower()
    if mode not in {"live", "paper", "both"}:
        mode = "live"

    executions = core._load(core.EXECUTIONS_FILE)
    if not isinstance(executions, dict):
        executions = {}

    records = [
        rec
        for rec in executions.values()
        if isinstance(rec, dict) and not rec.get("parent_trade_id")
    ]
    scoped = [
        rec
        for rec in records
        if mode == "both"
        or (mode == "paper" and bool(rec.get("paper")))
        or (mode == "live" and not bool(rec.get("paper")))
    ]

    try:
        current, _ = dashboard._estimate_pnl(scoped)
    except Exception:
        current = []
    marks = {
        str(row.get("id")): row
        for row in current
        if isinstance(row, dict) and row.get("id") is not None
    }

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    active_statuses = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}
    bet_type_order = ("ML", "Spread", "Total")
    grouped: dict[str, dict[str, dict[str, Any]]] = {
        "capper": {},
        "sport": {},
        "bet_type": {},
        "capper_bet_type": {},
        "sport_bet_type": {},
    }

    def bet_type_for(rec: dict[str, Any]) -> str | None:
        quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
        raw = (
            rec.get("market_type")
            or rec.get("strategy_market_type")
            or quote.get("market_type")
            or ""
        )
        normalized = re.sub(r"[^a-z0-9]+", "", str(raw).casefold())
        if normalized in {"ml", "moneyline", "h2h", "headtohead", "winner", "matchwinner"}:
            return "ML"
        if normalized in {"spread", "spreads", "handicap", "line"}:
            return "Spread"
        if normalized in {"total", "totals", "overunder", "ou"}:
            return "Total"
        return None

    def blank_row(name: str) -> dict[str, Any]:
        return {
            "name": name,
            "bets": 0,
            "open": 0,
            "wins": 0,
            "losses": 0,
            "pushes": 0,
            "graded": 0,
            "stake": Decimal("0"),
            "realized": Decimal("0"),
            "realized_7d": Decimal("0"),
            "realized_30d": Decimal("0"),
            "unrealized": Decimal("0"),
            "open_value": Decimal("0"),
            "sports": set(),
            "cappers": set(),
        }

    def ensure(bucket: str, key: str, *, name: str | None = None) -> dict[str, Any]:
        return grouped[bucket].setdefault(key, blank_row(name or key))

    def add_trade(
        row: dict[str, Any],
        *,
        sport: str,
        capper: str | None,
        status: str,
        mark: dict[str, Any],
        realized: Decimal | None,
        stake: Decimal,
        settled_at: datetime | None,
    ) -> None:
        row["bets"] += 1
        if sport:
            row["sports"].add(sport)
        if capper:
            row["cappers"].add(capper)

        if status in active_statuses:
            row["open"] += 1
            row["unrealized"] += _d(mark.get("estimated_pnl"))
            row["open_value"] += _d(mark.get("current_value_usdc"))

        if realized is None:
            return
        row["graded"] += 1
        row["realized"] += realized
        if stake > 0:
            row["stake"] += stake
        if realized > 0:
            row["wins"] += 1
        elif realized < 0:
            row["losses"] += 1
        else:
            row["pushes"] += 1
        if settled_at is not None:
            if settled_at >= cutoff_30d:
                row["realized_30d"] += realized
            if settled_at >= cutoff_7d:
                row["realized_7d"] += realized

    for rec in scoped:
        source, sport = _legacy_monitor_identity(rec)
        capper = _capper_name(source, sport)
        if not capper and not sport:
            continue

        bet_type = bet_type_for(rec)
        targets: list[tuple[str, str, str | None]] = []
        if capper:
            targets.append(("capper", capper, capper))
        if sport:
            targets.append(("sport", sport, sport))
        if bet_type:
            targets.append(("bet_type", bet_type, bet_type))
            if capper:
                targets.append(("capper_bet_type", f"{capper}::{bet_type}", bet_type))
            if sport:
                targets.append(("sport_bet_type", f"{sport}::{bet_type}", bet_type))

        status = str(rec.get("status") or "")
        trade_id = str(rec.get("id") or "")
        mark = marks.get(trade_id) or {}
        realized = base.pnl_base._explicit_realized_pnl(rec)
        stake = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
        settled_at = _metric_time(rec)

        for bucket, key, display_name in targets:
            row = ensure(bucket, key, name=display_name)
            add_trade(
                row,
                sport=sport,
                capper=capper,
                status=status,
                mark=mark,
                realized=realized,
                stake=stake,
                settled_at=settled_at,
            )

    def summarize(row: dict[str, Any]) -> dict[str, Any]:
        decided = row["wins"] + row["losses"]
        win_pct = (
            Decimal(row["wins"]) / Decimal(decided) * Decimal("100")
            if decided
            else None
        )
        roi = (
            row["realized"] / row["stake"] * Decimal("100")
            if row["stake"] > 0
            else None
        )
        total_live = row["realized"] + row["unrealized"]
        return {
            "name": row["name"],
            "bets": row["bets"],
            "open": row["open"],
            "wins": row["wins"],
            "losses": row["losses"],
            "pushes": row["pushes"],
            "graded": row["graded"],
            "win_pct": str(win_pct.quantize(Decimal("0.1"))) if win_pct is not None else None,
            "stake_usdc": str(row["stake"].quantize(Decimal("0.01"))),
            "realized_pnl_usdc": str(row["realized"].quantize(Decimal("0.01"))),
            "realized_pnl_7d_usdc": str(row["realized_7d"].quantize(Decimal("0.01"))),
            "realized_pnl_30d_usdc": str(row["realized_30d"].quantize(Decimal("0.01"))),
            "unrealized_pnl_usdc": str(row["unrealized"].quantize(Decimal("0.01"))),
            "total_live_pnl_usdc": str(total_live.quantize(Decimal("0.01"))),
            "open_value_usdc": str(row["open_value"].quantize(Decimal("0.01"))),
            "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
            "sports": sorted(row["sports"]),
            "cappers": sorted(row["cappers"]),
        }

    def finish(rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [summarize(rows[key]) for key in sorted(rows, key=str.casefold)]

    def breakdown(bucket: str, parent_name: str) -> list[dict[str, Any]]:
        output = []
        for bet_type in bet_type_order:
            key = f"{parent_name}::{bet_type}"
            output.append(
                summarize(grouped[bucket].get(key) or blank_row(bet_type))
            )
        return output

    by_capper = finish(grouped["capper"])
    for row in by_capper:
        row["bet_types"] = breakdown("capper_bet_type", row["name"])

    by_sport = finish(grouped["sport"])
    for row in by_sport:
        row["bet_types"] = breakdown("sport_bet_type", row["name"])

    by_bet_type = [
        summarize(grouped["bet_type"].get(name) or blank_row(name))
        for name in bet_type_order
    ]

    return {
        "mode": mode,
        "by_capper": by_capper,
        "by_sport": by_sport,
        "by_bet_type": by_bet_type,
        "generated_at": _now_iso(),
    }


@app.get("/api/dashboard/more-stats", dependencies=[Depends(dashboard._auth)])
def dashboard_more_stats(mode: str = "live"):
    return _more_stats_payload(mode)


def _dashboard_snapshot_v3() -> dict[str, Any]:
    reconcile = _reconcile_live_positions()
    data = _BASE_SNAPSHOT()
    executions = core._load(core.EXECUTIONS_FILE)
    metrics = _performance_metrics(executions)
    status = data.setdefault("status", {})
    status.update(metrics)
    status["reconciliation"] = reconcile
    status["submitted_live_trades"] = len(data.get("live_trades", []))
    status["performance_note"] = (
        "ROI and accuracy use closed trades with known realized P/L. "
        "Accuracy excludes pushes; externally reconciled closes without a recorded exit are not graded."
    )
    return data


dashboard._dashboard_snapshot = _dashboard_snapshot_v3


def _install_performance_and_paper_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="performanceRoi"' in html:
        return

    performance_html = '''
  <div class="performance-strip">
    <div class="performance-card"><div class="label">Portfolio value</div><div class="performance-value" id="performancePortfolio">—</div><div class="performance-sub">available USDC + positions</div></div>
    <div class="performance-card"><div class="label">Missed P/L · total</div><div class="performance-value" id="performanceMissedPnl">—</div><div class="performance-sub" id="performanceMissedCount">NFL + CFB untraded calls</div></div>
    <div class="performance-card"><div class="label">Last 7 days P/L</div><div class="performance-value" id="performancePnl7d">—</div><div class="performance-sub">realized</div></div>
    <div class="performance-card"><div class="label">Last 30 days P/L</div><div class="performance-value" id="performancePnl30d">—</div><div class="performance-sub">realized</div></div>
    <div class="performance-card"><div class="label">Volume L7</div><div class="performance-value" id="performanceVolume7d">—</div><div class="performance-sub">executed stake</div></div>
    <div class="performance-card"><div class="label">Volume L30</div><div class="performance-value" id="performanceVolume30d">—</div><div class="performance-sub">executed stake</div></div>
    <div class="performance-card"><div class="label">ROI · closed trades</div><div class="performance-value" id="performanceRoi">—</div></div>
    <div class="performance-card"><div class="label">Win / Loss</div><div class="performance-value" id="performanceWL">—</div><div class="performance-sub" id="performancePushes"></div></div>
    <div class="performance-card"><div class="label">Accuracy</div><div class="performance-value" id="performanceAccuracy">—</div><div class="performance-sub" id="performanceGraded"></div></div>
  </div>
'''
    more_stats_html = '''
  <div class="more-stats-shell">
    <button type="button" id="moreStatsToggle" class="more-stats-main-btn" onclick="toggleMoreStats()">MORE STATS</button>
    <div id="moreStatsPanel" class="more-stats-panel" style="display:none">
      <div class="more-stats-head">
        <div><div class="label">Performance breakdown</div><div id="moreStatsScope" class="performance-sub">LIVE trades</div></div>
        <div class="more-stats-tabs">
          <button type="button" data-more-stats-tab="capper" onclick="setMoreStatsTab('capper')">BY CAPPER</button>
          <button type="button" data-more-stats-tab="sport" onclick="setMoreStatsTab('sport')">BY SPORT</button>
          <button type="button" data-more-stats-tab="bet_type" onclick="setMoreStatsTab('bet_type')">BET TYPES</button>
        </div>
      </div>
      <div id="moreStatsBody">Loading…</div>
    </div>
  </div>
'''
    html = html.replace('  <div class="tabs">', performance_html + more_stats_html + '  <div class="tabs">', 1)

    css = '''
.performance-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}.performance-card{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:14px;padding:15px}.performance-value{font-size:24px;font-weight:900;margin-top:6px}.performance-sub{font-size:10px;color:var(--muted);margin-top:4px}.paper-close-btn{border:1px solid #7c5b15;background:#33270c;color:#fde68a;border-radius:8px;padding:7px 11px;font-size:11px;font-weight:850;cursor:pointer;white-space:nowrap}.paper-close-btn:hover{background:#49350d}.paper-close-btn:disabled{opacity:.55;cursor:wait}.more-stats-shell{margin:0 0 14px}.more-stats-main-btn{font-weight:850}.more-stats-panel{margin-top:9px;background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:14px;padding:14px}.more-stats-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}.more-stats-tabs{display:flex;gap:6px;flex-wrap:wrap}.more-stats-tabs button.active{font-weight:850;border-color:#86efac}.more-stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:9px;margin-top:12px}.more-stats-row{border:1px solid var(--border);border-radius:10px;background:#0d1522;padding:11px;line-height:1.55}.more-stats-row .name{font-size:16px;font-weight:850}.more-stats-row .pnl{font-weight:850}.more-stats-row .positive{color:#86efac}.more-stats-row .negative{color:#fca5a5}.more-stats-bet-types{display:grid;gap:5px;margin-top:8px;padding-top:7px;border-top:1px solid var(--border)}.more-stats-bet-type{font-size:11px;line-height:1.4}.more-stats-bet-type b{display:inline-block;min-width:48px}@media(max-width:1100px){.performance-strip{grid-template-columns:repeat(3,minmax(0,1fr))}}@media(max-width:700px){.performance-strip{grid-template-columns:1fr 1fr}.performance-card:last-child{grid-column:1/-1}.more-stats-head{flex-direction:column}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    live_only = """${(!x.paper&&x.source==='termux_executor')?`<button class="live-sell-btn" data-live-sell="${esc(x.id||'')}">SELL</button>`:'—'}"""
    with_paper = """${x.paper?`<button class="paper-close-btn" data-paper-close="${esc(x.id||'')}">CLOSE</button>`:((x.source==='termux_executor')?`<button class="live-sell-btn" data-live-sell="${esc(x.id||'')}">SELL</button>`:'—')}"""
    html = html.replace(live_only, with_paper)
    metric_anchor = "document.getElementById('budget').textContent=money(s.daily_budget_used)+' / '+money(s.max_daily_budget_usdc);"
    metric_js = """document.getElementById('budget').textContent=money(s.daily_budget_used)+' / '+money(s.max_daily_budget_usdc);
  const roi=document.getElementById('performanceRoi'),wl=document.getElementById('performanceWL'),acc=document.getElementById('performanceAccuracy'),push=document.getElementById('performancePushes'),graded=document.getElementById('performanceGraded'),p7=document.getElementById('performancePnl7d'),p30=document.getElementById('performancePnl30d');
  if(p7){const n=Number(s.realized_pnl_7d_usdc||0);p7.textContent=(n>0?'+':'')+'$'+n.toFixed(2);p7.className='performance-value '+(n>0?'green':n<0?'red':'')}
  if(p30){const n=Number(s.realized_pnl_30d_usdc||0);p30.textContent=(n>0?'+':'')+'$'+n.toFixed(2);p30.className='performance-value '+(n>0?'green':n<0?'red':'')}
  if(roi){const n=Number(s.roi_pct);roi.textContent=s.roi_pct===null||s.roi_pct===undefined?'—':n.toFixed(1)+'%';roi.className='performance-value '+(n>0?'green':n<0?'red':'')}
  if(wl)wl.textContent=String(s.wins||0)+' / '+String(s.losses||0);
  if(push)push.textContent=(s.pushes||0)+' push'+((s.pushes||0)===1?'':'es');
  if(acc){const a=Number(s.accuracy_pct);acc.textContent=s.accuracy_pct===null||s.accuracy_pct===undefined?'—':a.toFixed(1)+'%'}
  if(graded)graded.textContent=(s.graded_trades||0)+' graded closed trades';"""
    html = html.replace(metric_anchor, metric_js)

    paper_js = r'''
let capperLast24hOnly=localStorage.getItem('capperLast24hOnly')==='1';
if(localStorage.getItem('capperLast24hOnly')===null&&localStorage.getItem('nflLast24hOnly')==='1'){
 capperLast24hOnly=true;
 localStorage.setItem('capperLast24hOnly','1');
}
function capperWithin24h(v){
 if(!v)return false;
 const t=new Date(v).getTime();
 return Number.isFinite(t)&&t>=(Date.now()-24*60*60*1000);
}
function capperSyncLast24hButtons(){
 document.querySelectorAll('[data-capper-last24h-toggle]').forEach(btn=>{
  btn.textContent='Last 24h only: '+(capperLast24hOnly?'ON':'OFF');
  btn.classList.toggle('active',capperLast24hOnly);
  btn.setAttribute('aria-pressed',capperLast24hOnly?'true':'false');
 });
}
function capperToggleLast24h(){
 capperLast24hOnly=!capperLast24hOnly;
 localStorage.setItem('capperLast24hOnly',capperLast24hOnly?'1':'0');
 capperSyncLast24hButtons();
 window.dispatchEvent(new CustomEvent('capper-history-filter-change',{detail:{last24h:capperLast24hOnly}}));
}
window.addEventListener('load',capperSyncLast24hButtons);

let moreStatsOpen=false;
let moreStatsTab=localStorage.getItem('moreStatsTab')||'capper';
if(!['capper','sport','bet_type'].includes(moreStatsTab))moreStatsTab='capper';
let moreStatsData=null;
function moreStatsEsc(v){
 return String(v??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
}
function moreStatsMoney(v){
 const n=Number(v||0);
 return (n>0?'+':'')+'$'+n.toFixed(2);
}
function moreStatsPnlClass(v){
 const n=Number(v||0);
 return n>0?'positive':n<0?'negative':'';
}
function moreStatsBetTypes(types){
 if(!Array.isArray(types)||!types.length)return '';
 return '<div class="more-stats-bet-types">'+types.map(row=>{
  const roi=row.roi_pct===null||row.roi_pct===undefined?'—':Number(row.roi_pct).toFixed(1)+'%';
  return '<div class="more-stats-bet-type"><b>'+moreStatsEsc(row.name)+'</b> Bets '+Number(row.bets||0)
   +' · W-L-P '+Number(row.wins||0)+'-'+Number(row.losses||0)+'-'+Number(row.pushes||0)
   +' · ROI '+roi
   +' · <span class="pnl '+moreStatsPnlClass(row.realized_pnl_usdc)+'">'+moreStatsMoney(row.realized_pnl_usdc)+'</span></div>';
 }).join('')+'</div>';
}
function renderMoreStats(){
 const body=document.getElementById('moreStatsBody');
 const scope=document.getElementById('moreStatsScope');
 if(!body||!moreStatsData)return;
 const mode=String(moreStatsData.mode||'live').toUpperCase();
 if(scope)scope.textContent=mode+' trades · grouped automatically from execution history';
 document.querySelectorAll('[data-more-stats-tab]').forEach(btn=>btn.classList.toggle('active',btn.dataset.moreStatsTab===moreStatsTab));
 const rows=moreStatsTab==='sport'
  ? (moreStatsData.by_sport||[])
  : moreStatsTab==='bet_type'
  ? (moreStatsData.by_bet_type||[])
  : (moreStatsData.by_capper||[]);
 if(!rows.length){
  body.innerHTML='<div style="margin-top:12px;opacity:.7">No tracked capper trades in this scope.</div>';
  return;
 }
 body.innerHTML='<div class="more-stats-grid">'+rows.map(row=>{
  const win=row.win_pct===null||row.win_pct===undefined?'—':Number(row.win_pct).toFixed(1)+'%';
  const roi=row.roi_pct===null||row.roi_pct===undefined?'—':Number(row.roi_pct).toFixed(1)+'%';
  const related=moreStatsTab==='sport'
   ? (row.cappers||[]).join(', ')
   : moreStatsTab==='bet_type'
   ? [...(row.sports||[]),...(row.cappers||[])].join(', ')
   : (row.sports||[]).join(', ');
  const betTypes=moreStatsTab==='bet_type'?'':moreStatsBetTypes(row.bet_types||[]);
  return '<div class="more-stats-row"><div class="name">'+moreStatsEsc(row.name)+'</div>'
   +'<div>'+(related?moreStatsEsc(related)+' · ':'')+'Bets '+Number(row.bets||0)+' · Open '+Number(row.open||0)+'</div>'
   +'<div>W-L-P '+Number(row.wins||0)+'-'+Number(row.losses||0)+'-'+Number(row.pushes||0)+' · Win '+win+'</div>'
   +'<div>Stake $'+Number(row.stake_usdc||0).toFixed(2)+' · ROI '+roi+'</div>'
   +'<div class="pnl '+moreStatsPnlClass(row.realized_pnl_usdc)+'">Realized P/L '+moreStatsMoney(row.realized_pnl_usdc)+'</div>'
   +'<div><span class="pnl '+moreStatsPnlClass(row.realized_pnl_7d_usdc)+'">7D '+moreStatsMoney(row.realized_pnl_7d_usdc)+'</span> · <span class="pnl '+moreStatsPnlClass(row.realized_pnl_30d_usdc)+'">30D '+moreStatsMoney(row.realized_pnl_30d_usdc)+'</span></div>'
   +'<div class="pnl '+moreStatsPnlClass(row.total_live_pnl_usdc)+'">Live P/L '+moreStatsMoney(row.total_live_pnl_usdc)+'</div>'
   +betTypes
   +'</div>';
 }).join('')+'</div>';
}
async function loadMoreStats(){
 if(!moreStatsOpen)return;
 const mode=localStorage.getItem('dashboardStatsFilter')||'live';
 try{
  const r=await fetch('/api/dashboard/more-stats?mode='+encodeURIComponent(mode),{cache:'no-store'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'More stats failed');
  moreStatsData=d;
  renderMoreStats();
 }catch(e){
  const body=document.getElementById('moreStatsBody');
  if(body)body.textContent='Stats unavailable: '+String(e.message||e);
 }
}
function toggleMoreStats(){
 moreStatsOpen=!moreStatsOpen;
 const panel=document.getElementById('moreStatsPanel');
 const btn=document.getElementById('moreStatsToggle');
 if(panel)panel.style.display=moreStatsOpen?'block':'none';
 if(btn){btn.textContent=moreStatsOpen?'HIDE MORE STATS':'MORE STATS';btn.classList.toggle('active',moreStatsOpen);btn.setAttribute('aria-pressed',moreStatsOpen?'true':'false')}
 if(moreStatsOpen)loadMoreStats();
}
function setMoreStatsTab(tab){
 moreStatsTab=['capper','sport','bet_type'].includes(tab)?tab:'capper';
 localStorage.setItem('moreStatsTab',moreStatsTab);
 renderMoreStats();
}
document.addEventListener('click',function(ev){
 if(ev.target.closest('[data-stats-filter]')&&moreStatsOpen)setTimeout(loadMoreStats,50);
});
setInterval(()=>{if(moreStatsOpen)loadMoreStats()},10000);

document.addEventListener('click',async function(ev){
 const btn=ev.target.closest('[data-paper-close]');
 if(!btn)return;
 const tradeId=btn.getAttribute('data-paper-close');
 if(!tradeId)return;
 if(!confirm('Close this PAPER position at the current Polymarket SELL price? No real order will be sent.'))return;
 const original=btn.textContent;
 try{
  btn.disabled=true;btn.textContent='CLOSING…';
  const r=await fetch('/api/paper/close/'+encodeURIComponent(tradeId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Paper close failed');
  btn.textContent='CLOSED';
  setTimeout(()=>load(),350);
 }catch(e){
  btn.disabled=false;btn.textContent=original;
  alert('Paper close failed: '+String(e));
 }
});
'''
    html = html.replace("</script>", paper_js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_performance_and_paper_ui()
