from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends

from app import wnba_pw_history_v10 as base

app = base.app
history = base.history
dashboard = history.dashboard
core = history.core
ingest = history.ingest

PW_FILTERS_ENABLED = os.getenv("PW_FILTERS_ENABLED", "true").lower() == "true"
PW_FILTER_QUARTER = os.getenv("PW_FILTER_QUARTER", "Q4").upper()
PW_FILTER_VENUE = os.getenv("PW_FILTER_VENUE", "home").lower()
PW_FILTER_MIN_ML = int(os.getenv("PW_FILTER_MIN_ML", "100"))
PW_FILTER_MAX_ML = int(os.getenv("PW_FILTER_MAX_ML", "399"))
PW_FILTER_MIN_EDGE_PP = float(os.getenv("PW_FILTER_MIN_EDGE_PP", "8"))
PW_FILTER_MIN_EV_PCT = float(os.getenv("PW_FILTER_MIN_EV_PCT", "8"))
PW_FILTER_ONE_PER_GAME_SIDE = os.getenv("PW_FILTER_ONE_PER_GAME_SIDE", "true").lower() == "true"
PW_FILTER_DECISIONS_FILE = core.DATA_DIR / "pw_filter_decisions.json"

TEAM_NAME_BY_ABBR = {v: k for k, v in history.TEAM_ABBR.items()}


def _implied_probability(odds: int) -> float:
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def _expected_value_pct(probability_pct: float, odds: int) -> float:
    p = probability_pct / 100.0
    win_profit = odds / 100.0 if odds > 0 else 100.0 / abs(odds)
    return (p * win_profit - (1.0 - p)) * 100.0


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


def _already_bet_game_side(game_id: str, pick: str) -> bool:
    executions = core._load(core.EXECUTIONS_FILE)
    for rec in executions.values():
        meta = rec.get("pw_filter") or {}
        if (
            str(meta.get("game_id") or "") == str(game_id)
            and str(meta.get("predicted_winner") or "") == str(pick)
            and rec.get("status") in {
                "PAPER_OPEN",
                "PAPER_CLOSED",
                "ORDER_SUBMITTED",
                "PARTIALLY_CLOSED",
                "CLOSED",
                "SETTLED",
            }
        ):
            return True
    return False


def _decision(text: str, event_id: str | None = None) -> dict[str, Any]:
    row = _pw_row(text, event_id)
    if not row:
        return {
            "is_pw": False,
            "enabled": PW_FILTERS_ENABLED,
            "action": "BYPASS",
            "reason": "Not a recognized PW alert; PW filter not applied.",
        }

    game_id = str(row.get("game_id") or "")
    pick = str(row.get("predicted_winner") or "")
    quarter = str(row.get("quarter") or "").upper()
    odds = row.get("bk_ml")
    probability = row.get("win_probability")
    venue = _resolve_venue(game_id, pick)

    edge = None
    ev = None
    if odds is not None and probability is not None:
        edge = _edge_pp(float(probability), int(odds))
        ev = _expected_value_pct(float(probability), int(odds))

    checks = [
        {
            "id": "quarter",
            "label": "Quarter",
            "enabled": True,
            "rule": f"{PW_FILTER_QUARTER} only",
            "passed": quarter == PW_FILTER_QUARTER,
            "actual": quarter or "missing",
        },
        {
            "id": "venue",
            "label": "Venue",
            "enabled": True,
            "rule": f"{PW_FILTER_VENUE.upper()} team only",
            "passed": venue == PW_FILTER_VENUE,
            "actual": venue or "unknown",
        },
        {
            "id": "price",
            "label": "BK moneyline",
            "enabled": True,
            "rule": f"+{PW_FILTER_MIN_ML} to +{PW_FILTER_MAX_ML}",
            "passed": odds is not None and PW_FILTER_MIN_ML <= int(odds) <= PW_FILTER_MAX_ML,
            "actual": odds,
        },
        {
            "id": "edge",
            "label": "PW vs market edge",
            "enabled": True,
            "rule": f">= {PW_FILTER_MIN_EDGE_PP:.1f} pp",
            "passed": edge is not None and edge >= PW_FILTER_MIN_EDGE_PP,
            "actual": round(edge, 2) if edge is not None else None,
        },
        {
            "id": "ev",
            "label": "Expected value",
            "enabled": True,
            "rule": f">= {PW_FILTER_MIN_EV_PCT:.1f}%",
            "passed": ev is not None and ev >= PW_FILTER_MIN_EV_PCT,
            "actual": round(ev, 2) if ev is not None else None,
        },
    ]

    if PW_FILTER_ONE_PER_GAME_SIDE:
        duplicate = _already_bet_game_side(game_id, pick)
        checks.append(
            {
                "id": "one_per_game_side",
                "label": "Position limit",
                "enabled": True,
                "rule": "First bet per game/side only",
                "passed": not duplicate,
                "actual": "already bet" if duplicate else "clear",
            }
        )

    failed = [c for c in checks if c["enabled"] and not c["passed"]]
    action = "BET" if (not PW_FILTERS_ENABLED or not failed) else "PASS"
    if not PW_FILTERS_ENABLED:
        reason = "PW filters disabled."
    elif failed:
        reason = "; ".join(f"{c['label']}: {c['actual']} fails {c['rule']}" for c in failed)
    else:
        reason = "All enabled PW filters passed."

    return {
        "is_pw": True,
        "enabled": PW_FILTERS_ENABLED,
        "action": action,
        "reason": reason,
        "event_id": event_id,
        "game_id": game_id,
        "predicted_winner": pick,
        "quarter": quarter,
        "venue": venue,
        "bk_ml": odds,
        "win_probability": probability,
        "edge_pp": round(edge, 2) if edge is not None else None,
        "ev_pct": round(ev, 2) if ev is not None else None,
        "checks": checks,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _save_decision(event_id: str, decision: dict[str, Any]) -> None:
    data = core._load(PW_FILTER_DECISIONS_FILE)
    data[event_id] = decision
    if len(data) > 5000:
        items = sorted(data.items(), key=lambda kv: str((kv[1] or {}).get("evaluated_at") or ""))
        data = dict(items[-5000:])
    core._save(PW_FILTER_DECISIONS_FILE, data)


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

_ORIGINAL_PAPER_TRADE = ingest._paper_trade_from_alert


def _paper_trade_with_filters(
    parsed: dict[str, Any],
    slack_event_id: str,
    slack_event: dict[str, Any],
) -> dict[str, Any]:
    text = str(parsed.get("raw_text") or "")
    decision = _decision(text, slack_event_id)
    if decision.get("is_pw"):
        _save_decision(slack_event_id, decision)
        if PW_FILTERS_ENABLED and decision.get("action") != "BET":
            raise ValueError("PW FILTER PASS — " + str(decision.get("reason") or "filtered"))

    trade = _ORIGINAL_PAPER_TRADE(parsed, slack_event_id, slack_event)
    if decision.get("is_pw"):
        trade["pw_filter"] = decision
        executions = core._load(core.EXECUTIONS_FILE)
        if trade.get("id") in executions:
            executions[trade["id"]] = trade
            core._save(core.EXECUTIONS_FILE, executions)
    return trade


ingest._paper_trade_from_alert = _paper_trade_with_filters

_ORIGINAL_SAVE_ALERT = ingest._save_alert


def _save_alert_with_filter(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    decision = core._load(PW_FILTER_DECISIONS_FILE).get(event_id)
    if decision:
        payload = dict(payload)
        payload["pw_filter"] = decision
    return _ORIGINAL_SAVE_ALERT(event_id, payload)


ingest._save_alert = _save_alert_with_filter


def _stat(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(1 for row in rows if row.get("result") == "W")
    losses = sum(1 for row in rows if row.get("result") == "L")
    staked = sum(float(row.get("stake_usdc") or 0) for row in rows)
    pnl = sum(float(row.get("profit_usdc") or 0) for row in rows)
    return {
        "bets": len(rows),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / (wins + losses) * 100.0, 1) if wins + losses else None,
        "staked_usdc": round(staked, 2),
        "pnl_usdc": round(pnl, 2),
        "roi_pct": round(pnl / staked * 100.0, 1) if staked else None,
    }


def _decorate(row: dict[str, Any]) -> dict[str, Any]:
    abbr = row.get("predicted_winner_abbr")
    team_a = row.get("team_a")
    team_b = row.get("team_b")
    venue = "away" if abbr and abbr == team_a else "home" if abbr and abbr == team_b else None
    odds = row.get("bk_ml")
    probability = row.get("win_probability")
    edge = None
    ev = None
    if odds is not None and probability is not None:
        edge = _edge_pp(float(probability), int(odds))
        ev = _expected_value_pct(float(probability), int(odds))
    row = dict(row)
    row["venue"] = venue
    row["edge_pp_calc"] = edge
    row["ev_pct_calc"] = ev
    return row


def _first_per_game_side(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda r: (str(r.get("event_ts") or ""), int(r.get("id") or 0)))
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for row in ordered:
        key = (str(row.get("game_id") or ""), str(row.get("predicted_winner_abbr") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


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
    return [_decorate(dict(r)) for r in rows]


def _rule_stats(rows: list[dict[str, Any]], predicate) -> dict[str, Any]:
    matching = [r for r in rows if predicate(r)]
    return {
        "unique": _stat(_first_per_game_side(matching)),
        "raw_alerts": _stat(matching),
    }


def _combined_predicate(row: dict[str, Any]) -> bool:
    odds = row.get("bk_ml")
    edge = row.get("edge_pp_calc")
    ev = row.get("ev_pct_calc")
    return bool(
        row.get("quarter") == PW_FILTER_QUARTER
        and row.get("venue") == PW_FILTER_VENUE
        and odds is not None
        and PW_FILTER_MIN_ML <= int(odds) <= PW_FILTER_MAX_ML
        and edge is not None
        and edge >= PW_FILTER_MIN_EDGE_PP
        and ev is not None
        and ev >= PW_FILTER_MIN_EV_PCT
    )


@app.get("/api/wnba-pw/filters", dependencies=[Depends(dashboard._auth)])
def wnba_pw_filters():
    rows = _history_rows()
    rules = [
        {
            "id": "quarter",
            "label": "Quarter",
            "enabled": PW_FILTERS_ENABLED,
            "rule": f"{PW_FILTER_QUARTER} only",
            "stats": _rule_stats(rows, lambda r: r.get("quarter") == PW_FILTER_QUARTER),
        },
        {
            "id": "venue",
            "label": "Venue",
            "enabled": PW_FILTERS_ENABLED,
            "rule": f"{PW_FILTER_VENUE.upper()} team only",
            "stats": _rule_stats(rows, lambda r: r.get("venue") == PW_FILTER_VENUE),
        },
        {
            "id": "price",
            "label": "BK moneyline",
            "enabled": PW_FILTERS_ENABLED,
            "rule": f"+{PW_FILTER_MIN_ML} to +{PW_FILTER_MAX_ML}",
            "stats": _rule_stats(
                rows,
                lambda r: r.get("bk_ml") is not None
                and PW_FILTER_MIN_ML <= int(r["bk_ml"]) <= PW_FILTER_MAX_ML,
            ),
        },
        {
            "id": "edge",
            "label": "PW vs market edge",
            "enabled": PW_FILTERS_ENABLED,
            "rule": f">= {PW_FILTER_MIN_EDGE_PP:.1f} pp",
            "stats": _rule_stats(
                rows,
                lambda r: r.get("edge_pp_calc") is not None
                and float(r["edge_pp_calc"]) >= PW_FILTER_MIN_EDGE_PP,
            ),
        },
        {
            "id": "ev",
            "label": "Expected value",
            "enabled": PW_FILTERS_ENABLED,
            "rule": f">= {PW_FILTER_MIN_EV_PCT:.1f}%",
            "stats": _rule_stats(
                rows,
                lambda r: r.get("ev_pct_calc") is not None
                and float(r["ev_pct_calc"]) >= PW_FILTER_MIN_EV_PCT,
            ),
        },
    ]

    combined_raw = [r for r in rows if _combined_predicate(r)]
    combined_unique = _first_per_game_side(combined_raw) if PW_FILTER_ONE_PER_GAME_SIDE else combined_raw
    return {
        "enabled": PW_FILTERS_ENABLED,
        "strategy": "PW_FILTERED_AUTO_BET",
        "assumption": "Historical stats use posted BK ML and flat $100 stakes. Main dashboard numbers dedupe to the first qualifying alert per game/side.",
        "rules": rules,
        "position_limit": {
            "id": "one_per_game_side",
            "label": "Position limit",
            "enabled": PW_FILTERS_ENABLED and PW_FILTER_ONE_PER_GAME_SIDE,
            "rule": "First qualifying bet per game/side only",
        },
        "combined_before_dedupe": _stat(combined_raw),
        "auto_bet": _stat(combined_unique),
        "config": {
            "quarter": PW_FILTER_QUARTER,
            "venue": PW_FILTER_VENUE,
            "min_ml": PW_FILTER_MIN_ML,
            "max_ml": PW_FILTER_MAX_ML,
            "min_edge_pp": PW_FILTER_MIN_EDGE_PP,
            "min_ev_pct": PW_FILTER_MIN_EV_PCT,
            "one_per_game_side": PW_FILTER_ONE_PER_GAME_SIDE,
        },
    }


def _install_filter_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="pwFilterPanel"' in html:
        return

    panel = """
    <div class="slack-mode-box" id="pwFilterPanel" style="margin-top:16px">
      <div class="slack-mode-head">
        <div>
          <div class="label">PW BET FILTERS</div>
          <div class="slack-mode-value" id="pwFilterHeadline">Loading enabled filters…</div>
        </div>
        <div class="slack-mode-state" id="pwFilterCombined">Backtest loading…</div>
      </div>
      <div class="pw-filter-table-wrap">
        <table class="pw-filter-table">
          <thead>
            <tr><th>Filter</th><th>Status</th><th>Rule</th><th>Bets</th><th>W-L</th><th>Win %</th><th>P/L</th><th>ROI</th></tr>
          </thead>
          <tbody id="pwFilterRows"><tr><td colspan="8">Loading…</td></tr></tbody>
        </table>
      </div>
      <div class="pw-filter-final" id="pwFilterFinal"></div>
      <div class="slack-mode-note">Stats shown per filter use unique first game/side entries; the final AUTO BET row applies every enabled rule together. Raw repeated-alert stats are intentionally not the headline numbers.</div>
    </div>
"""
    html = html.replace('<form id="settingsForm">', panel + '<form id="settingsForm">', 1)

    css = """
.pw-filter-table-wrap{overflow-x:auto;margin-top:12px}
.pw-filter-table{width:100%;border-collapse:collapse;min-width:760px;font-size:12px}
.pw-filter-table th,.pw-filter-table td{padding:9px 8px;border-bottom:1px solid #273244;text-align:right;white-space:nowrap}
.pw-filter-table th:first-child,.pw-filter-table td:first-child,.pw-filter-table th:nth-child(2),.pw-filter-table td:nth-child(2),.pw-filter-table th:nth-child(3),.pw-filter-table td:nth-child(3){text-align:left}
.pw-filter-badge{display:inline-block;padding:4px 8px;border-radius:999px;font-size:10px;font-weight:900;letter-spacing:.04em}
.pw-filter-badge.on{background:#15382f;border:1px solid #2f7d66;color:#a7f3d0}
.pw-filter-badge.off{background:#3a2020;border:1px solid #7f3d3d;color:#fecaca}
.pw-filter-profit{font-weight:900}
.pw-filter-final{margin-top:12px;padding:11px 12px;border:1px solid #344157;border-radius:10px;background:#101827;font-size:12px;font-weight:800}
"""
    html = html.replace("</style>", css + "\n</style>", 1)

    js = r"""
function pwMoney(v){
 const n=Number(v||0),sign=n>0?'+':'';
 return sign+'$'+n.toFixed(2);
}
function pwPct(v){
 if(v===null||v===undefined)return '—';
 const n=Number(v),sign=n>0?'+':'';
 return sign+n.toFixed(1)+'%';
}
async function loadPwFilters(){
 try{
  const r=await fetch('/api/wnba-pw/filters',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'filter stats failed');
  const h=document.getElementById('pwFilterHeadline');
  const c=document.getElementById('pwFilterCombined');
  const body=document.getElementById('pwFilterRows');
  const fin=document.getElementById('pwFilterFinal');
  if(h){
   h.textContent=(d.enabled?'ENABLED':'DISABLED')+' · '+d.config.quarter+' · '+String(d.config.venue).toUpperCase()+
    ' · +'+d.config.min_ml+' to +'+d.config.max_ml+' · edge ≥'+Number(d.config.min_edge_pp).toFixed(1)+'pp · EV ≥'+Number(d.config.min_ev_pct).toFixed(1)+'%';
  }
  if(c){
   const s=d.auto_bet||{};
   c.textContent=(s.bets||0)+' unique bets · '+(s.wins||0)+'-'+(s.losses||0)+' · '+pwMoney(s.pnl_usdc)+' · '+pwPct(s.roi_pct)+' ROI';
  }
  if(body){
   let rows=(d.rules||[]).map(function(x){
    const s=(x.stats||{}).unique||{};
    return '<tr>'+
     '<td>'+x.label+'</td>'+
     '<td><span class="pw-filter-badge '+(x.enabled?'on':'off')+'">'+(x.enabled?'ENABLED':'OFF')+'</span></td>'+
     '<td>'+x.rule+'</td>'+
     '<td>'+(s.bets||0)+'</td>'+
     '<td>'+(s.wins||0)+'-'+(s.losses||0)+'</td>'+
     '<td>'+(s.win_rate_pct==null?'—':Number(s.win_rate_pct).toFixed(1)+'%')+'</td>'+
     '<td class="pw-filter-profit">'+pwMoney(s.pnl_usdc)+'</td>'+
     '<td class="pw-filter-profit">'+pwPct(s.roi_pct)+'</td>'+
    '</tr>';
   }).join('');
   const a=d.auto_bet||{};
   rows+='<tr><td>Position limit</td><td><span class="pw-filter-badge '+(d.position_limit.enabled?'on':'off')+'">'+(d.position_limit.enabled?'ENABLED':'OFF')+'</span></td>'+
    '<td>'+d.position_limit.rule+'</td><td colspan="5">Applied to final AUTO BET result below</td></tr>';
   rows+='<tr><td><strong>AUTO BET — ALL FILTERS</strong></td><td><span class="pw-filter-badge '+(d.enabled?'on':'off')+'">'+(d.enabled?'ENABLED':'OFF')+'</span></td>'+
    '<td>All enabled rules together</td><td><strong>'+(a.bets||0)+'</strong></td><td><strong>'+(a.wins||0)+'-'+(a.losses||0)+'</strong></td>'+
    '<td><strong>'+(a.win_rate_pct==null?'—':Number(a.win_rate_pct).toFixed(1)+'%')+'</strong></td>'+
    '<td class="pw-filter-profit"><strong>'+pwMoney(a.pnl_usdc)+'</strong></td><td class="pw-filter-profit"><strong>'+pwPct(a.roi_pct)+'</strong></td></tr>';
   body.innerHTML=rows;
  }
  if(fin){
   const raw=d.combined_before_dedupe||{},u=d.auto_bet||{};
   fin.textContent='Combined filter: '+(raw.bets||0)+' raw PW alerts → '+(u.bets||0)+' unique first entries. Unique result: '+(u.wins||0)+'-'+(u.losses||0)+' · P/L '+pwMoney(u.pnl_usdc)+' · ROI '+pwPct(u.roi_pct)+'.';
  }
 }catch(e){
  const h=document.getElementById('pwFilterHeadline');
  if(h)h.textContent='Filter stats unavailable: '+e.message;
 }
}
loadPwFilters();
setInterval(loadPwFilters,30000);
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_filter_ui()

print(
    "PW_FILTERS_READY "
    f"enabled={PW_FILTERS_ENABLED} "
    f"quarter={PW_FILTER_QUARTER} "
    f"venue={PW_FILTER_VENUE} "
    f"ml=+{PW_FILTER_MIN_ML}..+{PW_FILTER_MAX_ML} "
    f"edge={PW_FILTER_MIN_EDGE_PP}pp "
    f"ev={PW_FILTER_MIN_EV_PCT}% "
    f"one_per_game_side={PW_FILTER_ONE_PER_GAME_SIDE}"
)
