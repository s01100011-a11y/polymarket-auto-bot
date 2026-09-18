from __future__ import annotations

import os
from datetime import datetime, timezone
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
PW_STRAT_PLUS_EDGE40 = os.getenv("PW_STRAT_PLUS_EDGE40", "true").lower() == "true"
PW_EDGE40_PP = float(os.getenv("PW_EDGE40_PP", "40"))

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
            "id": "plus_edge40",
            "label": f"Plus-Money + Edge >= {PW_EDGE40_PP:.0f}pp",
            "enabled": PW_STRAT_PLUS_EDGE40,
            "rule": f"Plus-money and PW win probability exceeds market implied probability by at least {PW_EDGE40_PP:.0f} percentage points",
        },
    ]


def _match_strategies(row: dict[str, Any], venue: str | None) -> list[str]:
    quarter = str(row.get("quarter") or "").upper()
    odds = row.get("bk_ml")
    probability = row.get("win_probability")
    edge = None
    if odds is not None and probability is not None:
        edge = _edge_pp(float(probability), int(odds))

    matches: list[str] = []
    if PW_STRAT_Q3_AWAY and quarter == "Q3" and venue == "away":
        matches.append("q3_away")
    if PW_STRAT_AWAY_DOG and venue == "away" and odds is not None and int(odds) > 0:
        matches.append("away_underdog")
    if PW_STRAT_PLUS_MONEY and odds is not None and int(odds) > 0:
        matches.append("plus_money")
    if (
        PW_STRAT_PLUS_EDGE40
        and odds is not None
        and int(odds) > 0
        and edge is not None
        and edge >= PW_EDGE40_PP
    ):
        matches.append("plus_edge40")
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

    # Strategy testing is intentionally PAPER-ONLY even if the dashboard's
    # general Slack mode is switched to live. We are comparing filters, not
    # risking real money while the strategies are being evaluated.
    try:
        mode = live_control._mode()
        ingest.SLACK_PAPER_BUDGET_USDC = min(
            core.MAX_AUTO_TRADE_USDC,
            live_control.Decimal(str(mode.get("stake_usdc") or ingest.SLACK_PAPER_BUDGET_USDC)),
        )
    except Exception:
        pass

    trade = _PAPER_ONLY_HANDLER(parsed, slack_event_id, slack_event)
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
    if strategy_id == "plus_edge40":
        return bool(
            odds is not None
            and int(odds) > 0
            and row.get("edge_pp_calc") is not None
            and float(row["edge_pp_calc"]) >= PW_EDGE40_PP
        )
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


def _paper_test_stats(strategy_id: str) -> dict[str, Any]:
    executions = core._load(core.EXECUTIONS_FILE)
    rows = [
        rec
        for rec in executions.values()
        if strategy_id in (rec.get("pw_strategies") or [])
    ]
    open_count = sum(1 for r in rows if r.get("status") == "PAPER_OPEN")
    closed = [r for r in rows if r.get("status") != "PAPER_OPEN"]
    wins = 0
    losses = 0
    pnl = 0.0
    for r in closed:
        value = None
        for key in ("realized_pnl", "pnl_usdc", "profit_usdc"):
            if r.get(key) is not None:
                try:
                    value = float(r.get(key))
                    break
                except Exception:
                    pass
        if value is None:
            execution = r.get("execution") or {}
            for key in ("realized_pnl", "pnl_usdc", "profit_usdc"):
                if execution.get(key) is not None:
                    try:
                        value = float(execution.get(key))
                        break
                    except Exception:
                        pass
        if value is not None:
            pnl += value
            if value > 0:
                wins += 1
            elif value < 0:
                losses += 1
    stake = sum(float(r.get("budget_usdc") or 0) for r in closed if r.get("budget_usdc") is not None)
    return {
        "entries": len(rows),
        "open": open_count,
        "closed": len(closed),
        "wins": wins,
        "losses": losses,
        "pnl_usdc": round(pnl, 2),
        "roi_pct": round(pnl / stake * 100.0, 1) if stake else None,
    }


@app.get("/api/wnba-pw/strategies", dependencies=[Depends(dashboard._auth)])
def wnba_pw_strategies():
    rows = _history_rows()
    timestamps = [str(r.get("event_ts") or "") for r in rows if r.get("event_ts")]
    split = timestamps[len(timestamps) // 2] if timestamps else ""

    strategies = []
    for spec in _enabled_strategies():
        matched = [r for r in rows if _hist_match(spec["id"], r)]
        early = [r for r in matched if not split or str(r.get("event_ts") or "") < split]
        late = [r for r in matched if split and str(r.get("event_ts") or "") >= split]
        strategies.append(
            {
                **spec,
                "historical": _stats(matched),
                "early": _stats(early),
                "late": _stats(late),
                "paper_test": _paper_test_stats(spec["id"]),
            }
        )

    return {
        "enabled": PW_STRATEGY_TEST_ENABLED,
        "mode": "independent_or",
        "paper_only": True,
        "repeated_alerts_count_as_entries": True,
        "overlap_behavior": "One paper trade per Slack PW alert; every matched strategy receives attribution. No duplicate physical trade for strategy overlap.",
        "split_ts": split,
        "strategies": strategies,
    }


def _install_strategy_ui() -> None:
    html = dashboard.DASHBOARD_HTML

    # Remove the old combined-filter panel if present in a cached/imported HTML.
    if 'id="pwFilterPanel"' in html:
        start = html.find('<div class="slack-mode-box" id="pwFilterPanel"')
        if start >= 0:
            end = html.find('<form id="settingsForm">', start)
            if end > start:
                html = html[:start] + html[end:]

    if 'id="pwStrategyPanel"' in html:
        dashboard.DASHBOARD_HTML = html
        return

    panel = """
    <div class="slack-mode-box" id="pwStrategyPanel" style="margin-top:16px">
      <div class="slack-mode-head">
        <div>
          <div class="label">PW INDIVIDUAL STRATEGY TESTS</div>
          <div class="slack-mode-value" id="pwStrategyHeadline">Loading strategies…</div>
        </div>
        <div class="slack-mode-state" id="pwStrategyMode">PAPER ONLY · repeated PW alerts count</div>
      </div>
      <div class="pw-strategy-table-wrap">
        <table class="pw-strategy-table">
          <thead>
            <tr><th>Strategy</th><th>Status</th><th>Rule</th><th>PW bets</th><th>Games</th><th>W-L</th><th>Win %</th><th>P/L</th><th>ROI</th><th>Early ROI</th><th>Late ROI</th></tr>
          </thead>
          <tbody id="pwStrategyRows"><tr><td colspan="11">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="slack-mode-note">Each strategy is tested independently. If one PW alert matches several strategies, the bot creates one paper trade and credits that same entry to every matching strategy. Repeated PW opportunity alerts remain separate entries.</div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', panel + '<form id="settingsForm">', 1)

    css = """
.pw-strategy-table-wrap{overflow-x:auto;margin-top:12px}
.pw-strategy-table{width:100%;border-collapse:collapse;min-width:1050px;font-size:12px}
.pw-strategy-table th,.pw-strategy-table td{padding:9px 8px;border-bottom:1px solid #273244;text-align:right;white-space:nowrap}
.pw-strategy-table th:first-child,.pw-strategy-table td:first-child,.pw-strategy-table th:nth-child(2),.pw-strategy-table td:nth-child(2),.pw-strategy-table th:nth-child(3),.pw-strategy-table td:nth-child(3){text-align:left}
.pw-strategy-badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:900;letter-spacing:.04em}
.pw-strategy-badge.on{background:#15382f;border:1px solid #2f7d66;color:#a7f3d0}
.pw-strategy-badge.off{background:#3a2020;border:1px solid #7f3d3d;color:#fecaca}
"""
    html = html.replace("</style>", css + "\n</style>", 1)

    js = r"""
function pwStratMoney(v){
 const n=Number(v||0),sign=n>0?'+':'';
 return sign+'$'+n.toFixed(2);
}
function pwStratPct(v){
 if(v===null||v===undefined)return '—';
 const n=Number(v),sign=n>0?'+':'';
 return sign+n.toFixed(1)+'%';
}
async function loadPwStrategies(){
 try{
  const r=await fetch('/api/wnba-pw/strategies',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'strategy stats failed');
  const h=document.getElementById('pwStrategyHeadline');
  const m=document.getElementById('pwStrategyMode');
  const body=document.getElementById('pwStrategyRows');
  const enabled=(d.strategies||[]).filter(x=>x.enabled).length;
  if(h)h.textContent=enabled+' independent strategies enabled';
  if(m)m.textContent='PAPER ONLY · '+(d.repeated_alerts_count_as_entries?'repeated PW alerts count':'deduped');
  if(body){
   body.innerHTML=(d.strategies||[]).map(function(x){
    const s=x.historical||{},e=x.early||{},l=x.late||{};
    return '<tr>'+
      '<td><strong>'+x.label+'</strong></td>'+
      '<td><span class="pw-strategy-badge '+(x.enabled?'on':'off')+'">'+(x.enabled?'ENABLED':'OFF')+'</span></td>'+
      '<td>'+x.rule+'</td>'+
      '<td>'+Number(s.alerts||0)+'</td>'+
      '<td>'+Number(s.games||0)+'</td>'+
      '<td>'+Number(s.wins||0)+'-'+Number(s.losses||0)+'</td>'+
      '<td>'+(s.win_rate_pct==null?'—':Number(s.win_rate_pct).toFixed(1)+'%')+'</td>'+
      '<td><strong>'+pwStratMoney(s.pnl_usdc)+'</strong></td>'+
      '<td><strong>'+pwStratPct(s.roi_pct)+'</strong></td>'+
      '<td>'+pwStratPct(e.roi_pct)+'</td>'+
      '<td>'+pwStratPct(l.roi_pct)+'</td>'+
    '</tr>';
   }).join('');
  }
 }catch(e){
  const h=document.getElementById('pwStrategyHeadline');
  if(h)h.textContent='Strategy stats unavailable: '+e.message;
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
    f"plus_edge40={PW_STRAT_PLUS_EDGE40} "
    "paper_only=True repeated_alerts=True"
)
