from __future__ import annotations

import json
import re
import threading
import time
import uuid
from decimal import Decimal
from typing import Any

from fastapi import Depends, Request

from app import slack_ingest as ingest

app = ingest.app

SLACK_DEBUG_FILE = ingest.core.DATA_DIR / "slack_debug_log.json"
SLACK_DEBUG_MAX_ENTRIES = 500
_SLACK_DEBUG_LOCK = threading.Lock()


def _clean(value: str) -> str:
    return re.sub(r"[*_`]", "", value or "").strip()


def _canonical_team(value: str) -> str | None:
    target = ingest._norm_text(_clean(value))
    for team, aliases in ingest.WNBA_ALIASES.items():
        if target == ingest._norm_text(team):
            return team
        if any(target == ingest._norm_text(alias) for alias in aliases):
            return team
    # Header normally contains a full team name. Fall back to contained aliases.
    for team, aliases in ingest.WNBA_ALIASES.items():
        if ingest._norm_text(team) in target:
            return team
        if any(len(alias) > 3 and ingest._norm_text(alias) in target for alias in aliases):
            return team
    return None


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"(?:^|[·\n])\s*{re.escape(name)}\s*:\s*([^·\n]+)", text, flags=re.I)
    return _clean(m.group(1)) if m else None


def _parse_alert(text: str) -> dict[str, Any]:
    raw = text or ""
    cleaned = _clean(raw)
    teams = ingest._team_mentions(raw)

    header = re.search(r"Predicted\s+Winner\s*[—-]\s*([^\n]+)", cleaned, flags=re.I)
    alert_type = "predicted_winner" if header else "other"
    selection = _canonical_team(header.group(1)) if header else None

    # Explicitly ignore non-bet channel posts such as Final / GAME SUMMARY.
    if not header:
        return {
            "raw_text": raw,
            "teams": teams,
            "market_kind": "moneyline",
            "selection": None,
            "line": None,
            "units": None,
            "alert_type": alert_type,
            "actionable": False,
            "ignore_reason": "Not a Predicted Winner alert",
        }

    prob_match = re.search(r"(\d{1,3})%\s+win\s+probability", cleaned, flags=re.I)
    consensus_match = re.search(r"consensus\s*:\s*([^\n·]+)", cleaned, flags=re.I)
    game_match = re.search(r"Game\s*:\s*([^\n·]+?)\s+vs\s+([^\n·]+?)\s*[·\n]", cleaned, flags=re.I)
    quarter_match = re.search(r"Game\s*:[^\n]+?[·]\s*(Q[1-4]|OT\d*)", cleaned, flags=re.I)
    edge_match = re.search(r"Edge\s*:\s*([+\-]?\d+(?:\.\d+)?)", cleaned, flags=re.I)
    pol_match = re.search(r"Pol\s*:\s*([^·\n]+)", cleaned, flags=re.I)

    win_probability = int(prob_match.group(1)) if prob_match else None
    consensus = _clean(consensus_match.group(1)) if consensus_match else None
    game_teams = []
    if game_match:
        for raw_team in (game_match.group(1), game_match.group(2)):
            team = _canonical_team(raw_team)
            if team and team not in game_teams:
                game_teams.append(team)
    if selection and selection not in game_teams:
        game_teams.insert(0, selection)

    parsed = {
        "raw_text": raw,
        "teams": game_teams or teams,
        "market_kind": "moneyline",
        "selection": selection,
        "line": None,
        "units": None,
        "alert_type": alert_type,
        "actionable": bool(selection),
        "win_probability": win_probability,
        "consensus": consensus,
        "quarter": quarter_match.group(1).upper() if quarter_match else None,
        "score": _field(cleaned, "Score"),
        "margin": _field(cleaned, "Margin"),
        "pregame_odds": _field(cleaned, "Odds"),
        "pregame_spread": _field(cleaned, "Spread"),
        "live_ml": _field(cleaned, "Live ML"),
        "handicap": _field(cleaned, "Handicap"),
        "live_spread": _field(cleaned, "Live Spread"),
        "bk_odds": _field(cleaned, "BK Odds"),
        "bk_spread": _field(cleaned, "BK Spread"),
        "pol": _clean(pol_match.group(1)) if pol_match else None,
        "edge": edge_match.group(1) if edge_match else None,
        "scenario": _field(cleaned, "Scenario"),
    }
    return parsed


_original_paper_trade = ingest._paper_trade_from_alert


def _paper_trade_from_alert(parsed: dict[str, Any], slack_event_id: str, slack_event: dict[str, Any]) -> dict[str, Any]:
    if parsed.get("alert_type") != "predicted_winner" or not parsed.get("actionable"):
        raise ValueError(parsed.get("ignore_reason") or "Slack alert is not an actionable Predicted Winner signal")
    if parsed.get("market_kind") != "moneyline":
        raise ValueError("WNBA Slack automation currently paper-trades moneyline only")
    if not parsed.get("selection"):
        raise ValueError("Could not identify predicted winner")
    return _original_paper_trade(parsed, slack_event_id, slack_event)


# Patch the already-registered Slack endpoint's runtime globals.
ingest._parse_alert = _parse_alert
ingest._paper_trade_from_alert = _paper_trade_from_alert


def _event_id(body: dict[str, Any]) -> str | None:
    event = body.get("event") or {}
    value = body.get("event_id") or event.get("client_msg_id") or event.get("ts")
    return str(value) if value else None


def _ignored_reason(body: dict[str, Any], preexisting: bool = False) -> str | None:
    if body.get("type") == "url_verification":
        return "Slack URL verification"
    if body.get("type") != "event_callback":
        return "Unsupported payload type"
    event = body.get("event") or {}
    if event.get("type") != "message":
        return "Not a message event"
    if event.get("subtype") in {"message_changed", "message_deleted"}:
        return "Message edit/delete"
    if ingest.SLACK_CHANNEL_ID and event.get("channel") != ingest.SLACK_CHANNEL_ID:
        return "Different channel"
    text = str(event.get("text") or "").strip()
    if not text:
        return "Empty message"
    if ingest.SLACK_REQUIRE_WNBA and not ingest._looks_wnba(text):
        return "Not WNBA"
    if preexisting:
        return "Duplicate Slack event"
    event_time = body.get("event_time")
    try:
        event_time_int = int(str(event_time or "0"))
    except ValueError:
        event_time_int = 0
    if event_time_int and abs(int(time.time()) - event_time_int) > ingest.SLACK_MAX_ALERT_AGE_SECONDS:
        return "Stale alert"
    parsed = _parse_alert(text)
    if not parsed.get("actionable"):
        return parsed.get("ignore_reason") or "Not actionable"
    return None


def _append_debug(entry: dict[str, Any]) -> None:
    # Never store Slack signatures, signing secrets, auth headers, wallet secrets, or Railway variables here.
    with _SLACK_DEBUG_LOCK:
        logs = ingest.core._load(SLACK_DEBUG_FILE)
        debug_id = f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}"
        clean_entry = {
            "debug_id": debug_id,
            "received_at": ingest._now_iso(),
            "event_id": entry.get("event_id"),
            "channel": entry.get("channel"),
            "user": entry.get("user"),
            "bot_id": entry.get("bot_id"),
            "subtype": entry.get("subtype"),
            "http_status": entry.get("http_status"),
            "status": entry.get("status"),
            "reason": entry.get("reason"),
            "error": entry.get("error"),
            "text": str(entry.get("text") or "")[:6000],
            "parsed": entry.get("parsed"),
            "paper_trade_id": entry.get("paper_trade_id"),
        }
        logs[debug_id] = clean_entry
        if len(logs) > SLACK_DEBUG_MAX_ENTRIES:
            ordered = sorted(logs.items(), key=lambda kv: kv[1].get("received_at") or "")
            logs = dict(ordered[-SLACK_DEBUG_MAX_ENTRIES:])
        ingest.core._save(SLACK_DEBUG_FILE, logs)


@app.middleware("http")
async def slack_debug_middleware(request: Request, call_next):
    if request.method != "POST" or request.url.path != "/slack/events":
        return await call_next(request)

    raw = await request.body()
    body: dict[str, Any] = {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        pass

    event = body.get("event") or {}
    event_id = _event_id(body)
    preexisting = bool(event_id and event_id in ingest.core._load(ingest.SLACK_ALERTS_FILE))
    parsed = None
    text = str(event.get("text") or "").strip()
    if text:
        try:
            parsed = _parse_alert(text)
        except Exception:
            parsed = None

    response = await call_next(request)

    # Invalid/unverified signatures return 401 from the endpoint and are intentionally not persisted.
    if response.status_code != 401:
        saved = ingest.core._load(ingest.SLACK_ALERTS_FILE).get(event_id) if event_id else None
        reason = _ignored_reason(body, preexisting=preexisting)
        status = "RECEIVED"
        error = None
        paper_trade_id = None
        if body.get("type") == "url_verification":
            status = "URL_VERIFIED"
        elif saved:
            status = saved.get("status") or "RECEIVED"
            error = saved.get("error")
            paper_trade_id = saved.get("paper_trade_id")
            parsed = saved.get("parsed") or parsed
            reason = error or reason
        elif preexisting:
            status = "DUPLICATE"
        elif reason:
            status = "IGNORED"
        elif response.status_code >= 400:
            status = "HTTP_ERROR"
            reason = f"Slack endpoint returned HTTP {response.status_code}"

        _append_debug({
            "event_id": event_id,
            "channel": event.get("channel"),
            "user": event.get("user"),
            "bot_id": event.get("bot_id"),
            "subtype": event.get("subtype"),
            "http_status": response.status_code,
            "status": status,
            "reason": reason,
            "error": error,
            "text": text,
            "parsed": parsed,
            "paper_trade_id": paper_trade_id,
        })
    return response


@app.get("/api/slack/debug", dependencies=[Depends(ingest.dashboard._auth)])
def slack_debug(limit: int = 100):
    limit = max(1, min(limit, 200))
    entries = list(ingest.core._load(SLACK_DEBUG_FILE).values())
    entries.sort(key=lambda r: r.get("received_at") or "", reverse=True)
    entries = entries[:limit]
    return {
        "entries": entries,
        "count": len(entries),
        "stored_count": len(ingest.core._load(SLACK_DEBUG_FILE)),
        "max_stored": SLACK_DEBUG_MAX_ENTRIES,
        "channel_id": ingest.SLACK_CHANNEL_ID or None,
        "events_enabled": ingest.SLACK_EVENTS_ENABLED,
        "paper_only": ingest.SLACK_PAPER_ONLY,
        "generated_at": ingest._now_iso(),
    }


def _install_slack_debug_ui() -> None:
    html = ingest.dashboard.DASHBOARD_HTML
    if 'data-tab="slacklog"' in html:
        return

    html = html.replace(
        '<button class="tab" data-tab="settings">Configuration</button>',
        '<button class="tab" data-tab="slacklog">Slack debug</button>\n    <button class="tab" data-tab="settings">Configuration</button>',
    )

    debug_panel = '''  <section class="panel" id="slacklog">
    <h2>Slack Debug Log</h2><p class="note">Recent Slack webhook events seen by the Railway bot. Refreshes every 5 seconds. Secrets and signature headers are never stored.</p>
    <div class="debug-summary" id="slackDebugSummary">Loading Slack events…</div>
    <div id="slackDebugBody"></div>
  </section>
'''
    html = html.replace('  <section class="panel" id="settings">', debug_panel + '  <section class="panel" id="settings">')

    html = html.replace(
        '.market{max-width:320px}.muted{color:var(--muted)}',
        '.market{max-width:320px}.muted{color:var(--muted)}.debug-summary{display:flex;gap:12px;flex-wrap:wrap;padding:10px 12px;margin:0 0 12px;border:1px solid var(--border);border-radius:10px;background:#0d1522;color:var(--muted);font-size:12px}.debug-msg{white-space:pre-wrap;word-break:break-word;max-width:520px;max-height:180px;overflow:auto;margin:0;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;color:#d8e2f0}.debug-detail{font:11px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--muted);white-space:pre-wrap;max-width:300px}',
    )

    debug_js = r'''
async function loadSlackDebug(){
 try{
  const r=await fetch('/api/slack/debug?limit=100',{cache:'no-store'});if(!r.ok)throw new Error('HTTP '+r.status);const d=await r.json();
  const entries=d.entries||[];
  const counts={};entries.forEach(x=>counts[x.status]=(counts[x.status]||0)+1);
  document.getElementById('slackDebugSummary').textContent=`Stored ${d.stored_count}/${d.max_stored} · Showing ${entries.length} · Paper only ${d.paper_only?'YES':'NO'} · Events ${d.events_enabled?'ON':'OFF'} · `+Object.entries(counts).map(([k,v])=>k+': '+v).join(' · ');
  const rows=entries.map(x=>{
    const p=x.parsed||{};
    const parsed=[p.selection?('Selection: '+p.selection):'',p.market_kind?('Market: '+p.market_kind):'',p.win_probability!==null&&p.win_probability!==undefined?('Win%: '+p.win_probability):'',p.quarter?('Quarter: '+p.quarter):'',p.edge?('Edge: '+p.edge):'',x.paper_trade_id?('Paper trade: '+x.paper_trade_id):''].filter(Boolean).join('\n');
    const why=x.error||x.reason||'';
    const cls=x.status==='PAPER_TRADE_CREATED'?'green':x.status==='NO_TRADE'||x.status==='HTTP_ERROR'?'red':x.status==='IGNORED'||x.status==='DUPLICATE'?'yellow':'';
    return `<tr><td>${when(x.received_at)}</td><td><span class="status ${cls}">${esc(x.status||'—')}</span><div class="muted" style="margin-top:5px">HTTP ${esc(x.http_status||'—')}</div></td><td class="debug-detail">${esc(parsed||'—')}</td><td><pre class="debug-msg">${esc(x.text||'')}</pre></td><td class="debug-detail">${esc(why||'—')}</td><td class="debug-detail">${esc(x.event_id||'—')}</td></tr>`;
  });
  document.getElementById('slackDebugBody').innerHTML=table(['Received','Status','Parsed','Slack message','Reason / error','Event ID'],rows);
 }catch(e){const s=document.getElementById('slackDebugSummary');if(s)s.textContent='Slack debug error: '+String(e)}
}
'''
    html = html.replace(
        "function setPnl(el,v){const n=Number(v||0);el.textContent=money(n);el.className='value '+(n>0?'green':n<0?'red':'')}",
        "function setPnl(el,v){const n=Number(v||0);el.textContent=money(n);el.className='value '+(n>0?'green':n<0?'red':'')}" + debug_js,
    )
    html = html.replace(
        'load();setInterval(load,15000);',
        'load();loadSlackDebug();setInterval(load,15000);setInterval(loadSlackDebug,5000);',
    )
    ingest.dashboard.DASHBOARD_HTML = html


_install_slack_debug_ui()


@app.get("/api/slack/wnba-parser")
def wnba_parser_status():
    return {
        "ok": True,
        "mode": "paper_only",
        "channel_id": ingest.SLACK_CHANNEL_ID or None,
        "signal_type": "Predicted Winner",
        "market_kind": "moneyline",
        "events_enabled": ingest.SLACK_EVENTS_ENABLED,
        "signing_secret_configured": bool(ingest.SLACK_SIGNING_SECRET),
        "paper_budget_usdc": str(ingest.SLACK_PAPER_BUDGET_USDC),
        "debug_log": True,
        "debug_max_entries": SLACK_DEBUG_MAX_ENTRIES,
    }
