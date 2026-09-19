from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Depends

from app import wnba_pw_history_v10 as base
from app import dashboard_live_control_v4 as live_control

app = base.app
history = base.history
dashboard = history.dashboard
core = history.core
ingest = history.ingest

PW_STRATEGY_TEST_ENABLED = os.getenv("PW_STRATEGY_TEST_ENABLED", "true").lower() == "true"
PW_STRAT_Q3_AWAY = os.getenv("PW_STRAT_Q3_AWAY", "true").lower() == "true"
PW_STRAT_AWAY_DOG = os.getenv("PW_STRAT_AWAY_DOG", "true").lower() == "true"
PW_STRAT_PLUS_MONEY = os.getenv("PW_STRAT_PLUS_MONEY", "true").lower() == "true"
PW_STRAT_HOME_DOG = os.getenv("PW_STRAT_HOME_DOG", "true").lower() == "true"

PW_STRATEGY_DECISIONS_FILE = core.DATA_DIR / "pw_strategy_decisions.json"


def _implied_probability(odds: int) -> float:
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _edge_pp(probability_pct: float, odds: int) -> float:
    return probability_pct - (_implied_probability(odds) * 100.0)


def _pw_row(text: str, event_id: str | None = None) -> dict[str, Any] | None:
    for row in history._parse_current_message(text, event_id):
        if row.get("record_type") == "alert":
            return row
    return None


def _venue_from_history(game_id: str, pick: str) -> str | None:
    abbr = history.TEAM_ABBR.get(pick)
    if not abbr:
        return None
    try:
        with history._db() as con:
            row = con.execute(
                "SELECT team_a,team_b FROM games WHERE game_id=?",
                (game_id,),
            ).fetchone()
    except Exception:
        row = None
    if not row:
        return None
    if abbr == row["team_a"]:
        return "away"
    if abbr == row["team_b"]:
        return "home"
    return None


def _venue_from_espn(game_id: str, pick: str) -> str | None:
    try:
        r = httpx.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
            params={"event": game_id},
            timeout=5.0,
        )
        r.raise_for_status()
        data = r.json()
        competitions = ((data.get("header") or {}).get("competitions") or [])
        if not competitions:
            return None
        for comp in competitions[0].get("competitors") or []:
            team = comp.get("team") or {}
            names = {
                str(team.get("displayName") or "").strip().casefold(),
                str(team.get("shortDisplayName") or "").strip().casefold(),
                str(team.get("name") or "").strip().casefold(),
            }
            if pick.casefold() in names:
                venue = str(comp.get("homeAway") or "").lower()
                return venue if venue in {"home", "away"} else None
    except Exception:
        return None
    return None


def _resolve_venue(game_id: str | None, pick: str | None) -> str | None:
    if not game_id or not pick:
        return None
    return _venue_from_history(game_id, pick) or _venue_from_espn(game_id, pick)


def _enabled_strategies() -> list[dict[str, Any]]:
    return [
        {
            "id": "q3_away",
            "label": "Q3 Away",
            "enabled": PW_STRAT_Q3_AWAY,
            "rule": "PW alert is Q3 and predicted winner is the away team",
        },
        {
            "id": "away_underdog",
            "label": "Away Underdog",
            "enabled": PW_STRAT_AWAY_DOG,
            "rule": "Predicted winner is away and BK moneyline is plus-money",
        },
        {
            "id": "plus_money",
            "label": "Plus-Money BK ML",
            "enabled": PW_STRAT_PLUS_MONEY,
            "rule": "BK moneyline is +100 or higher",
        },
        {
            "id": "home_underdog",
            "label": "Home Underdog",
            "enabled": PW_STRAT_HOME_DOG,
            "rule": "Predicted winner is home and BK moneyline is plus-money",
        },
    ]


def _match_strategies(row: dict[str, Any], venue: str | None) -> list[str]:
    quarter = str(row.get("quarter") or "").upper()
    odds = row.get("bk_ml")
    matches: list[str] = []
    if PW_STRAT_Q3_AWAY and quarter == "Q3" and venue == "away":
        matches.append("q3_away")
    if PW_STRAT_AWAY_DOG and venue == "away" and odds is not None and int(odds) > 0:
        matches.append("away_underdog")
    if PW_STRAT_PLUS_MONEY and odds is not None and int(odds) > 0:
        matches.append("plus_money")
    if PW_STRAT_HOME_DOG and venue == "home" and odds is not None and int(odds) > 0:
        matches.append("home_underdog")
    return matches


def _decision(text: str, event_id: str | None = None) -> dict[str, Any]:
    row = _pw_row(text, event_id)
    if not row:
        return {
            "is_pw": False,
            "action": "BYPASS",
            "matched_strategies": [],
            "reason": "Not a recognized PW alert.",
        }

    game_id = str(row.get("game_id") or "")
    pick = str(row.get("predicted_winner") or "")
    venue = _resolve_venue(game_id, pick)
    odds = row.get("bk_ml")
    probability = row.get("win_probability")
    edge = None
    if odds is not None and probability is not None:
        edge = _edge_pp(float(probability), int(odds))

    matches = _match_strategies(row, venue)
    action = "BET" if PW_STRATEGY_TEST_ENABLED and matches else "PASS"
    reason = (
        "Matched: " + ", ".join(matches)
        if matches
        else "No enabled individual PW strategy matched this alert."
    )
    return {
        "is_pw": True,
        "action": action,
        "reason": reason,
        "matched_strategies": matches,
        "game_id": game_id,
        "predicted_winner": pick,
        "quarter": str(row.get("quarter") or "").upper(),
        "venue": venue,
        "bk_ml": odds,
        "win_probability": probability,
        "edge_pp": round(edge, 2) if edge is not None else None,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_decision(event_id: str, decision: dict[str, Any]) -> None:
    data = core._load(PW_STRATEGY_DECISIONS_FILE)
    data[event_id] = decision
    if len(data) > 5000:
        items = sorted(data.items(), key=lambda kv: str((kv[1] or {}).get("evaluated_at") or ""))
        data = dict(items[-5000:])
    core._save(PW_STRATEGY_DECISIONS_FILE, data)


_ORIGINAL_PARSE_ALERT = ingest._parse_alert


def _parse_alert_with_pw(text: str) -> dict[str, Any]:
    parsed = _ORIGINAL_PARSE_ALERT(text)
    row = _pw_row(text)
    if row:
        parsed["selection"] = row.get("predicted_winner")
        parsed["market_kind"] = "moneyline"
        parsed["pw"] = row
    return parsed


ingest._parse_alert = _parse_alert_with_pw

_CURRENT_TRADE_HANDLER = ingest._paper_trade_from_alert
_PAPER_ONLY_HANDLER = live_control._PAPER_HANDLER


def _strategy_trade_handler(
    parsed: dict[str, Any],
    slack_event_id: str,
    slack_event: dict[str, Any],
) -> dict[str, Any]:
    text = str(parsed.get("raw_text") or "")
    decision = _decision(text, slack_event_id)

    if not decision.get("is_pw"):
        return _CURRENT_TRADE_HANDLER(parsed, slack_event_id, slack_event)

    _save_decision(slack_event_id, decision)
    if decision.get("action") != "BET":
        raise ValueError("PW STRATEGY PASS — " + str(decision.get("reason") or "no match"))

    # Railway AUTO_TRADING is authoritative for qualifying PW alerts.
    # When enabled, use the current live-capable Slack handler; otherwise
    # preserve paper-only strategy evaluation.
    try:
        mode = live_control._mode()
        ingest.SLACK_PAPER_BUDGET_USDC = min(
            core.MAX_AUTO_TRADE_USDC,
            live_control.Decimal(str(mode.get("stake_usdc") or ingest.SLACK_PAPER_BUDGET_USDC)),
        )
    except Exception:
        pass

    handler = _CURRENT_TRADE_HANDLER if core.auto_trading_enabled() else _PAPER_ONLY_HANDLER
    trade = handler(parsed, slack_event_id, slack_event)
    trade["pw_strategies"] = list(decision.get("matched_strategies") or [])
    trade["pw_strategy_decision"] = decision
    executions = core._load(core.EXECUTIONS_FILE)
    if trade.get("id") in executions:
        executions[trade["id"]] = trade
        core._save(core.EXECUTIONS_FILE, executions)
    return trade


ingest._paper_trade_from_alert = _strategy_trade_handler

_ORIGINAL_SAVE_ALERT = ingest._save_alert


def _save_alert_with_strategy(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    decision = core._load(PW_STRATEGY_DECISIONS_FILE).get(event_id)
    if decision:
        payload = dict(payload)
        payload["pw_strategy"] = decision
    return _ORIGINAL_SAVE_ALERT(event_id, payload)


ingest._save_alert = _save_alert_with_strategy


def _history_rows() -> list[dict[str, Any]]:
    with history._db() as con:
        rows = con.execute(
            """
            SELECT a.*,g.team_a,g.team_b,g.winner_abbr
            FROM alerts a
            LEFT JOIN games g ON g.game_id=a.game_id
            WHERE a.backtest_eligible=1
            ORDER BY a.event_ts ASC,a.id ASC
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        abbr = row.get("predicted_winner_abbr")
        if abbr and abbr == row.get("team_a"):
            row["venue"] = "away"
        elif abbr and abbr == row.get("team_b"):
            row["venue"] = "home"
        else:
            row["venue"] = None
        if row.get("bk_ml") is not None and row.get("win_probability") is not None:
            row["edge_pp_calc"] = _edge_pp(float(row["win_probability"]), int(row["bk_ml"]))
        else:
            row["edge_pp_calc"] = None
        out.append(row)
    return out


def _hist_match(strategy_id: str, row: dict[str, Any]) -> bool:
    odds = row.get("bk_ml")
    if strategy_id == "q3_away":
        return row.get("quarter") == "Q3" and row.get("venue") == "away"
    if strategy_id == "away_underdog":
        return row.get("venue") == "away" and odds is not None and int(odds) > 0
    if strategy_id == "plus_money":
        return odds is not None and int(odds) > 0
    if strategy_id == "home_underdog":
        return row.get("venue") == "home" and odds is not None and int(odds) > 0
    return False


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for r in rows if r.get("result") == "W")
    losses = sum(1 for r in rows if r.get("result") == "L")
    pnl = sum(float(r.get("profit_usdc") or 0) for r in rows)
    staked = sum(float(r.get("stake_usdc") or 0) for r in rows)
    games = len({str(r.get("game_id") or "") for r in rows if r.get("game_id")})
    return {
        "alerts": len(rows),
        "games": games,
        "alerts_per_game": round(len(rows) / games, 2) if games else None,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / (wins + losses) * 100.0, 1) if wins + losses else None,
        "pnl_usdc": round(pnl, 2),
        "roi_pct": round(pnl / staked * 100.0, 1) if staked else None,
    }


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _realized_pnl(rec: dict[str, Any]) -> Decimal | None:
    for key in ("realized_pnl", "pnl_usdc", "profit_usdc", "display_pnl"):
        if rec.get(key) is not None:
            try:
                return Decimal(str(rec.get(key)))
            except Exception:
                pass
    execution = rec.get("execution") or {}
    for key in ("realized_pnl", "pnl_usdc", "profit_usdc"):
        if execution.get(key) is not None:
            try:
                return Decimal(str(execution.get(key)))
            except Exception:
                pass
    return None


def _bot_test_stats_all(strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    executions = core._load(core.EXECUTIONS_FILE)
    rows = [
        rec for rec in executions.values()
        if not rec.get("parent_trade_id")
        and rec.get("pw_strategies")
        and any(sid in (rec.get("pw_strategies") or []) for sid in strategy_ids)
    ]

    # Price each open paper position once, even when the same PW alert is
    # attributed to several strategies.
    open_values: dict[str, Decimal | None] = {}
    open_rows = [r for r in rows if r.get("status") == "PAPER_OPEN"]
    if open_rows:
        try:
            with ingest.PublicClient() as client:
                for rec in open_rows:
                    trade_id = str(rec.get("id") or "")
                    q = rec.get("quote") or {}
                    asset_id = str(q.get("asset_id") or "")
                    shares = _d(q.get("shares"))
                    entry = _d(q.get("paper_entry_price") or q.get("entry_price") or q.get("limit_price"))
                    if not trade_id or not asset_id or shares <= 0 or entry <= 0:
                        open_values[trade_id] = None
                        continue
                    try:
                        midpoint = _d(client.get_midpoint(asset_id=asset_id))
                        open_values[trade_id] = (midpoint - entry) * shares
                    except Exception:
                        open_values[trade_id] = None
        except Exception:
            for rec in open_rows:
                open_values[str(rec.get("id") or "")] = None

    def summarize(matched: list[dict[str, Any]]) -> dict[str, Any]:
        wins = losses = pushes = 0
        closed = 0
        ungraded_closed = 0
        realized = Decimal("0")
        open_pnl = Decimal("0")
        roi_stake = Decimal("0")
        open_count = 0
        open_unpriced = 0
        games: set[str] = set()

        for rec in matched:
            decision = rec.get("pw_strategy_decision") or {}
            game_id = str(decision.get("game_id") or "")
            if game_id:
                games.add(game_id)

            budget = _d(rec.get("budget_usdc") or (rec.get("quote") or {}).get("budget_usdc"))
            if rec.get("status") == "PAPER_OPEN":
                open_count += 1
                value = open_values.get(str(rec.get("id") or ""))
                if value is None:
                    open_unpriced += 1
                    continue
                open_pnl += value
                if budget > 0:
                    roi_stake += budget
                continue

            pnl = _realized_pnl(rec)
            if pnl is None:
                ungraded_closed += 1
                continue
            closed += 1
            realized += pnl
            if budget > 0:
                roi_stake += budget
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1
            else:
                pushes += 1

        total_pnl = realized + open_pnl
        decided = wins + losses
        return {
            "entries": len(matched),
            "games": len(games),
            "open": open_count,
            "open_unpriced": open_unpriced,
            "closed": closed,
            "ungraded_closed": ungraded_closed,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_rate_pct": round(wins / decided * 100.0, 1) if decided else None,
            "realized_pnl_usdc": round(float(realized), 2),
            "open_pnl_usdc": round(float(open_pnl), 2),
            "pnl_usdc": round(float(total_pnl), 2),
            "roi_pct": round(float(total_pnl / roi_stake * Decimal("100")), 1) if roi_stake > 0 else None,
            "roi_basis_stake_usdc": round(float(roi_stake), 2),
        }

    result: dict[str, dict[str, Any]] = {}
    for strategy_id in strategy_ids:
        result[strategy_id] = summarize(
            [r for r in rows if strategy_id in (r.get("pw_strategies") or [])]
        )

    # TOTAL is the union of all qualifying filter trades. Each physical PW
    # alert/trade appears once here no matter how many strategies matched it.
    total = summarize(rows)
    filter_entry_credits = sum(int(result[sid].get("entries") or 0) for sid in strategy_ids)
    total["filter_entry_credits"] = filter_entry_credits
    total["duplicate_entry_credits_removed"] = max(0, filter_entry_credits - int(total.get("entries") or 0))
    result["__total__"] = total
    return result


@app.get("/api/wnba-pw/strategies", dependencies=[Depends(dashboard._auth)])
def wnba_pw_strategies():
    rows = _history_rows()
    timestamps = [str(r.get("event_ts") or "") for r in rows if r.get("event_ts")]
    split = timestamps[len(timestamps) // 2] if timestamps else ""
    specs = _enabled_strategies()
    enabled_ids = [spec["id"] for spec in specs if spec.get("enabled")]
    bot_stats = _bot_test_stats_all(enabled_ids)

    strategies = []
    for spec in specs:
        matched = [r for r in rows if _hist_match(spec["id"], r)]
        early = [r for r in matched if not split or str(r.get("event_ts") or "") < split]
        late = [r for r in matched if split and str(r.get("event_ts") or "") >= split]
        strategies.append(
            {
                **spec,
                "historical": _stats(matched),
                "early": _stats(early),
                "late": _stats(late),
                "bot_test": bot_stats.get(spec["id"], {}),
            }
        )

    # Deduped portfolio total: each historical PW alert is counted once if it
    # matches ANY enabled filter, even when it matches multiple filters.
    total_hist_rows = [
        r for r in rows
        if any(_hist_match(strategy_id, r) for strategy_id in enabled_ids)
    ]
    total_early_rows = [
        r for r in total_hist_rows
        if not split or str(r.get("event_ts") or "") < split
    ]
    total_late_rows = [
        r for r in total_hist_rows
        if split and str(r.get("event_ts") or "") >= split
    ]
    total_historical = _stats(total_hist_rows)
    historical_filter_credits = sum(
        int((s.get("historical") or {}).get("alerts") or 0)
        for s in strategies if s.get("enabled")
    )
    total_historical["filter_entry_credits"] = historical_filter_credits
    total_historical["duplicate_entry_credits_removed"] = max(
        0, historical_filter_credits - int(total_historical.get("alerts") or 0)
    )

    return {
        "enabled": PW_STRATEGY_TEST_ENABLED,
        "mode": "independent_or",
        "paper_only": True,
        "repeated_alerts_count_as_entries": True,
        "overlap_behavior": "One paper trade per Slack PW alert; every matched strategy receives attribution. TOTAL counts each PW alert/trade once across all enabled filters.",
        "historical_stake_model": "Flat $100 per unique qualifying PW alert at posted BK moneyline.",
        "bot_pnl_model": "Actual paper stake. P/L includes realized closed P/L plus current midpoint P/L for open paper positions when priceable.",
        "split_ts": split,
        "strategies": strategies,
        "total": {
            "label": "TOTAL · ALL ENABLED FILTERS",
            "rule": "Union of all enabled filters; crossover alerts are counted once.",
            "historical": total_historical,
            "early": _stats(total_early_rows),
            "late": _stats(total_late_rows),
            "bot_test": bot_stats.get("__total__", {}),
        },
    }

def _install_strategy_ui() -> None:
    html = dashboard.DASHBOARD_HTML

    # Remove the previous combined filter panel if it exists.
    if 'id="pwFilterPanel"' in html:
        start = html.find('<div class="slack-mode-box" id="pwFilterPanel"')
        if start >= 0:
            end = html.find('<form id="settingsForm">', start)
            if end > start:
                html = html[:start] + html[end:]

    panel = """
    <div class="slack-mode-box" id="pwStrategyPanel" style="margin-top:16px">
      <div class="slack-mode-head">
        <div>
          <div class="label">PW FILTER PERFORMANCE</div>
          <div class="slack-mode-value" id="pwStrategyHeadline">Loading 4 strategy filters…</div>
        </div>
        <div class="slack-mode-state" id="pwStrategyMode">PAPER ONLY · repeated PW alerts count</div>
      </div>
      <div id="pwStrategyTotal" class="pw-strategy-total">
        <div class="pw-strategy-loading">Loading deduped total stats…</div>
      </div>
      <div class="pw-strategy-cards" id="pwStrategyCards">
        <div class="pw-strategy-loading">Loading bot and historical stats…</div>
      </div>
      <div class="slack-mode-note">
        BOT TEST = forward paper trades since these filters were enabled, using the configured bot stake.
        HISTORICAL = full PW archive backtest using flat $100 per qualifying PW alert at the posted BK moneyline.
        A repeated PW alert is a new opportunity and counts as a new entry. If one alert matches multiple filters,
        one paper trade is made and the result is attributed to every matching filter.
      </div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', panel + '<form id="settingsForm">', 1)

    css = """
.pw-strategy-total{margin-top:14px}
.pw-strategy-total .pw-strategy-card{border-color:#41516d;background:#0d1726}
.pw-strategy-total .pw-strategy-name{font-size:17px}
.pw-strategy-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:12px}
.pw-strategy-card{border:1px solid #273244;border-radius:12px;background:#0a111d;padding:13px;min-width:0}
.pw-strategy-card-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px}
.pw-strategy-name{font-size:15px;font-weight:900}
.pw-strategy-rule{font-size:10px;color:var(--muted);line-height:1.4;margin-top:4px}
.pw-strategy-badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:900;letter-spacing:.04em;white-space:nowrap}
.pw-strategy-badge.on{background:#15382f;border:1px solid #2f7d66;color:#a7f3d0}
.pw-strategy-badge.off{background:#3a2020;border:1px solid #7f3d3d;color:#fecaca}
.pw-strategy-section{border-top:1px solid #202b3d;margin-top:10px;padding-top:10px}
.pw-strategy-section-title{font-size:10px;font-weight:900;letter-spacing:.08em;color:#93a4bb;margin-bottom:8px}
.pw-strategy-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:6px}
.pw-strategy-metric{background:#0d1725;border:1px solid #1e2b3e;border-radius:8px;padding:7px;min-width:0}
.pw-strategy-metric-label{font-size:8px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);white-space:nowrap}
.pw-strategy-metric-value{font-size:13px;font-weight:850;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pw-strategy-sub{font-size:9px;color:var(--muted);margin-top:7px;line-height:1.35}
.pw-strategy-loading{font-size:12px;color:var(--muted);padding:8px 0}
@media(max-width:900px){.pw-strategy-cards{grid-template-columns:1fr}.pw-strategy-metrics{grid-template-columns:repeat(5,minmax(70px,1fr));overflow-x:auto}}
"""
    html = html.replace("</style>", css + "\n</style>", 1)

    js = r"""
function pwStratMoney(v){
 if(v===null||v===undefined)return '—';
 const n=Number(v),sign=n>0?'+':'';
 return sign+'$'+n.toFixed(2);
}
function pwStratPct(v){
 if(v===null||v===undefined)return '—';
 const n=Number(v),sign=n>0?'+':'';
 return sign+n.toFixed(1)+'%';
}
function pwStratClass(v){
 const n=Number(v);
 return n>0?'green':n<0?'red':'';
}
function pwMetric(label,value,cls){
 return '<div class="pw-strategy-metric"><div class="pw-strategy-metric-label">'+label+'</div><div class="pw-strategy-metric-value '+(cls||'')+'">'+value+'</div></div>';
}
function pwStrategyCard(x,isTotal){
 const b=x.bot_test||{},s=x.historical||{},e=x.early||{},l=x.late||{};
 const botWL=Number(b.wins||0)+'-'+Number(b.losses||0);
 const histWL=Number(s.wins||0)+'-'+Number(s.losses||0);
 const botDup=Number(b.duplicate_entry_credits_removed||0);
 const histDup=Number(s.duplicate_entry_credits_removed||0);
 const badge=isTotal?'<span class="pw-strategy-badge on">DEDUPED TOTAL</span>':'<span class="pw-strategy-badge '+(x.enabled?'on':'off')+'">'+(x.enabled?'ENABLED':'OFF')+'</span>';
 return '<div class="pw-strategy-card">'+
   '<div class="pw-strategy-card-head"><div><div class="pw-strategy-name">'+x.label+'</div><div class="pw-strategy-rule">'+x.rule+'</div></div>'+badge+'</div>'+
   '<div class="pw-strategy-section"><div class="pw-strategy-section-title">BOT TEST · FORWARD PAPER RESULTS</div>'+
     '<div class="pw-strategy-metrics">'+
       pwMetric('Entries',Number(b.entries||0))+
       pwMetric('Open',Number(b.open||0))+
       pwMetric('W-L',botWL)+
       pwMetric('P/L',pwStratMoney(b.pnl_usdc),pwStratClass(b.pnl_usdc))+
       pwMetric('ROI',pwStratPct(b.roi_pct),pwStratClass(b.roi_pct))+
     '</div>'+
     '<div class="pw-strategy-sub">Realized '+pwStratMoney(b.realized_pnl_usdc)+' · Open P/L '+pwStratMoney(b.open_pnl_usdc)+(isTotal?' · '+botDup+' crossover credits removed':'')+'</div>'+
   '</div>'+
   '<div class="pw-strategy-section"><div class="pw-strategy-section-title">HISTORICAL PW ARCHIVE · FLAT $100</div>'+
     '<div class="pw-strategy-metrics">'+
       pwMetric('PW bets',Number(s.alerts||0))+
       pwMetric('Games',Number(s.games||0))+
       pwMetric('W-L',histWL)+
       pwMetric('P/L',pwStratMoney(s.pnl_usdc),pwStratClass(s.pnl_usdc))+
       pwMetric('ROI',pwStratPct(s.roi_pct),pwStratClass(s.roi_pct))+
     '</div>'+
     '<div class="pw-strategy-sub">Win '+(s.win_rate_pct==null?'—':Number(s.win_rate_pct).toFixed(1)+'%')+' · Early ROI '+pwStratPct(e.roi_pct)+' · Late ROI '+pwStratPct(l.roi_pct)+' · '+(s.alerts_per_game==null?'—':Number(s.alerts_per_game).toFixed(2))+' PW bets/game'+(isTotal?' · '+histDup+' crossover credits removed':'')+'</div>'+
   '</div>'+
 '</div>';
}
async function loadPwStrategies(){
 try{
  const r=await fetch('/api/wnba-pw/strategies',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'strategy stats failed');
  const h=document.getElementById('pwStrategyHeadline');
  const m=document.getElementById('pwStrategyMode');
  const cards=document.getElementById('pwStrategyCards');
  const total=document.getElementById('pwStrategyTotal');
  const enabled=(d.strategies||[]).filter(x=>x.enabled).length;
  if(h)h.textContent=enabled+' of 4 independent filters enabled · total is deduped';
  if(m)m.textContent='PAPER ONLY · '+(d.repeated_alerts_count_as_entries?'repeated PW alerts count':'deduped');
  if(total&&d.total)total.innerHTML=pwStrategyCard(d.total,true);
  if(cards)cards.innerHTML=(d.strategies||[]).map(function(x){return pwStrategyCard(x,false)}).join('');
 }catch(e){
  const h=document.getElementById('pwStrategyHeadline');
  const cards=document.getElementById('pwStrategyCards');
  if(h)h.textContent='Strategy stats unavailable';
  if(cards)cards.innerHTML='<div class="pw-strategy-loading">'+String(e.message||e)+'</div>';
 }
}
loadPwStrategies();
setInterval(loadPwStrategies,30000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_strategy_ui()

print(
    "PW_STRATEGY_TEST_READY "
    f"enabled={PW_STRATEGY_TEST_ENABLED} "
    f"q3_away={PW_STRAT_Q3_AWAY} "
    f"away_dog={PW_STRAT_AWAY_DOG} "
    f"plus_money={PW_STRAT_PLUS_MONEY} "
    f"home_dog={PW_STRAT_HOME_DOG} "
    "paper_only=True repeated_alerts=True"
)


# Start the private PW-export poller only after all Slack/PW strategy monkey
# patches above are installed, so direct feed records use the identical
# filtering, sizing, duplicate guards, and live/paper execution path.
from app import pw_export_ingest as pw_export_ingest

pw_export_ingest.install(
    app=app,
    ingest=ingest,
    core=core,
    history=history,
    dashboard=dashboard,
)
