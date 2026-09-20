"""odds_api.py — Fetch live bookmaker odds from The Odds API (#213)

Provides fetch_live_game_odds() for use in the PW call flow to capture
real-time spread and moneyline at the moment a predicted winner fires.

Also provides persistent usage logging (#266): startup checks, per-call
records, and daily summary records written to odds_api_log_{league}.json.

Reuses team mapping and odds extraction from backfill_odds.py.
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import league_config

try:
    import requests
except ImportError:
    requests = None

from backfill_odds import (
    ODDS_API_BASE,
    SPORT_KEY,
    BOOKMAKER_PRIORITY,
    extract_odds,
    _normalize_team_name,
)

# ── Quarter/half market keys (#374) ─────────────────────────────────────────
QUARTER_MARKETS = {
    "Q1": "h2h_q1,spreads_q1,h2h_h1,spreads_h1",
    "Q2": "h2h_q2,spreads_q2,h2h_h1,spreads_h1",
    "Q3": "h2h_q3,spreads_q3",
    "Q4": "h2h_q4,spreads_q4",
}

# ── Persistent usage log (#266) ──────────────────────────────────────────────
LOG_FILE = league_config.state_path("odds_api_log.json")
_MAX_LOG_ENTRIES = 5000  # cap log file size; oldest entries trimmed on write

def _append_log(record):
    """Append a record to the persistent JSONL-style log (capped array)."""
    try:
        entries = []
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                entries = []
        entries.append(record)
        # Trim oldest entries if over cap
        if len(entries) > _MAX_LOG_ENTRIES:
            entries = entries[-_MAX_LOG_ENTRIES:]
        tmp = LOG_FILE + ".tmp.{}".format(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f)
        os.replace(tmp, LOG_FILE)
    except Exception:
        pass  # logging is best-effort


def _usage_from_headers(resp):
    """Extract credits_used and credits_remaining from response headers."""
    credits_used = 0
    credits_remaining = None
    try:
        credits_used = int(resp.headers.get("x-requests-last", 0))
    except (TypeError, ValueError):
        pass
    try:
        cr = resp.headers.get("x-requests-remaining")
        if cr is not None:
            credits_remaining = int(cr)
    except (TypeError, ValueError):
        pass
    return credits_used, credits_remaining


def log_startup(api_key, logger=None):
    """Check Odds API usage at monitor startup (free /v4/sports/ call).

    Returns dict {credits_remaining, requests_remaining} or None on error.
    """
    if not api_key or requests is None:
        return None
    try:
        url = "{}/sports/".format(ODDS_API_BASE)
        _req_params = {"apiKey": api_key}
        _t0 = time.perf_counter()
        resp = requests.get(url, params=_req_params, timeout=10)
        _lat = (time.perf_counter() - _t0) * 1000
        resp.raise_for_status()
        _, credits_remaining = _usage_from_headers(resp)
        _log_api_request(url, _req_params, resp.status_code,
                         resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:2000],
                         "startup", _lat)
        # Update in-memory tracker so is_credits_low() works even without live API calls
        _credits["credits_remaining"] = credits_remaining
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record = {
            "type": "startup",
            "ts": ts,
            "credits_remaining": credits_remaining,
        }
        _append_log(record)
        if logger:
            logger("[ODDS_API_LOG] Startup check: {} credits remaining".format(
                credits_remaining if credits_remaining is not None else "?"))
        return {"credits_remaining": credits_remaining}
    except Exception as exc:
        if logger:
            logger("[ODDS_API_LOG] Startup check failed: {}".format(exc))
        return None


def log_call(sport_key, markets, credits_used, credits_remaining,
             status_code, latency_ms, cache_hit, context=""):
    """Log a per-call record after an Odds API request."""
    _append_log({
        "type": "call",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sport": sport_key,
        "markets": markets,
        "credits_used": credits_used,
        "credits_remaining": credits_remaining,
        "status": status_code,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "cache_hit": cache_hit,
        "context": context,
    })


def log_daily_summary(summary_type, credits_remaining,
                      calls_in_window=None, credits_in_window=None):
    """Log a daily summary record (open or close).

    Args:
        summary_type: "daily_summary_open" or "daily_summary_close"
        credits_remaining: current credit balance
        calls_in_window: total API calls during window (close only)
        credits_in_window: total credits consumed during window (close only)
    """
    record = {
        "type": summary_type,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "credits_remaining": credits_remaining,
    }
    if calls_in_window is not None:
        record["calls_in_window"] = calls_in_window
    if credits_in_window is not None:
        record["credits_in_window"] = credits_in_window
    _append_log(record)


def load_log():
    """Load and return the full log as a list of records."""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                entries = json.load(f)
            if isinstance(entries, list):
                return entries
    except Exception:
        pass
    return []


# Module-level cache: avoid hitting the API multiple times in the same
# monitor cycle (multiple PW calls can fire in one tick).
_cache = {"data": None, "ts": 0, "ttl": 120}  # 120s cache — survives 1-2 monitor cycles

# Credit tracking — accumulates across the process lifetime (one monitor cycle)
_credits = {
    "api_calls": 0,
    "credits_used": 0,
    "credits_remaining": None,
    "last_call_ts": None,
    "errors": 0,
}

# Per-game BK odds tracking: {game_key: {"pw_fetches": N, "pw_hits": N}}
_game_stats = {}


def get_credit_status():
    """Return current credit tracking stats as a dict."""
    return dict(_credits)


_live_credit_cache = {"credits_remaining": None, "ts": 0}
_LIVE_CREDIT_TTL = 60


def query_live_credits(api_key):
    """Query live credit balance from /v4/sports/ (free call). Cached for 60s."""
    now = time.time()
    if _live_credit_cache["credits_remaining"] is not None and (now - _live_credit_cache["ts"]) < _LIVE_CREDIT_TTL:
        return _live_credit_cache.copy()
    if not api_key or requests is None:
        return {"credits_remaining": None, "ts": None, "error": "no api key"}
    try:
        _qurl = "{}/sports/".format(ODDS_API_BASE)
        _qparams = {"apiKey": api_key}
        _qt0 = time.perf_counter()
        resp = requests.get(_qurl, params=_qparams, timeout=10)
        _qlat = (time.perf_counter() - _qt0) * 1000
        _, remaining = _usage_from_headers(resp)
        _log_api_request(_qurl, _qparams, resp.status_code,
                         resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text[:2000],
                         "credit_check", _qlat)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _live_credit_cache["credits_remaining"] = remaining
        _live_credit_cache["ts"] = now
        return {"credits_remaining": remaining, "ts": ts}
    except Exception as e:
        return {"credits_remaining": None, "ts": None, "error": str(e)}


def get_log_credit_status():
    """Get credit status from most recent log entry."""
    entries = load_log()
    for e in reversed(entries):
        cr = e.get("credits_remaining")
        if cr is not None:
            return {"credits_remaining": cr, "ts": e.get("ts", ""), "type": e.get("type", "")}
    return {"credits_remaining": None, "ts": None}


def reset_credit_tracking():
    """Reset per-cycle credit counters (call at start of monitor cycle)."""
    _credits["api_calls"] = 0
    _credits["credits_used"] = 0
    _credits["errors"] = 0


def record_game_fetch(game_id, got_odds):
    """Track a BK odds fetch attempt for a specific game."""
    if game_id not in _game_stats:
        _game_stats[game_id] = {"pw_fetches": 0, "pw_hits": 0}
    _game_stats[game_id]["pw_fetches"] += 1
    if got_odds:
        _game_stats[game_id]["pw_hits"] += 1


def get_game_stats(game_id):
    """Return per-game BK stats dict or None."""
    return _game_stats.get(game_id)


def format_game_stats(game_id):
    """Return one-line per-game BK stats or None."""
    gs = _game_stats.get(game_id)
    if not gs:
        return None
    remaining = _credits.get("credits_remaining")
    rem_str = str(remaining) if remaining is not None else "?"
    return "BK odds: {}/{} PW calls had live odds · {} credits remaining".format(
        gs["pw_hits"], gs["pw_fetches"], rem_str)


LOW_CREDIT_THRESHOLD = 50

# Per-bookmaker market tracking (accumulated from extract_odds results)
_bk_stats = {}  # bk_name -> {"ml": count, "spread": count, "totals": count, "primary": count, "fallback": count}
_fallback_events = []  # list of {ts, message} — capped at 200

# Fallback request/response data saving (#341)
_fallback_save_enabled = False
_fallback_retention_days = 7
FALLBACK_DATA_FILE = league_config.state_path("odds_api_fallback_data.jsonl")

# Full request/response logging (#355)
_request_log_enabled = False
_request_log_retention_days = 1
REQUEST_LOG_FILE = league_config.state_path("odds_api_request_log.jsonl")


def _track_bk_usage(odds_result, raw_event=None):
    """Track which bookmaker provided each market. Persists to log (#285).
    Saves full raw event data on fallback (#341).
    """
    if not odds_result:
        return
    ml_src = odds_result.get("ml_source", "")
    spr_src = odds_result.get("spread_source", "")
    tot_src = odds_result.get("totals_source", "")

    for field, market in [("ml_source", "ml"), ("spread_source", "spread"), ("totals_source", "totals")]:
        src = odds_result.get(field)
        if src:
            entry = _bk_stats.setdefault(src, {"ml": 0, "spread": 0, "totals": 0, "primary": 0, "fallback": 0})
            entry[market] += 1
    if ml_src:
        _bk_stats[ml_src]["primary"] = _bk_stats[ml_src].get("primary", 0) + 1
    if spr_src and spr_src != ml_src:
        _bk_stats[spr_src]["fallback"] = _bk_stats[spr_src].get("fallback", 0) + 1
    if tot_src and tot_src != ml_src:
        _bk_stats[tot_src]["fallback"] = _bk_stats[tot_src].get("fallback", 0) + 1

    # Persist BK usage to log (#285)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _append_log({"type": "bk_usage", "ts": ts,
                 "ml_source": ml_src, "spread_source": spr_src,
                 "totals_source": tot_src,
                 "is_fallback": bool(odds_result.get("fallback_log"))})

    has_fallback = False
    for msg in odds_result.get("fallback_log", []):
        has_fallback = True
        _fallback_events.append({"ts": ts, "message": msg})
        if len(_fallback_events) > 200:
            _fallback_events.pop(0)
        _append_log({"type": "fallback", "ts": ts, "message": msg,
                     "ml_source": ml_src, "spread_source": spr_src,
                     "totals_source": tot_src})

    # Save full raw event data on fallback (#341)
    if has_fallback:
        _save_fallback_data(ts, odds_result, raw_event)


def get_bk_stats():
    """Return per-bookmaker market usage stats."""
    return dict(_bk_stats)


def get_fallback_events():
    """Return recent fallback events."""
    return list(_fallback_events)


def configure_fallback_save(enabled, retention_days=7):
    """Set fallback data save config from caller (#341)."""
    global _fallback_save_enabled, _fallback_retention_days
    _fallback_save_enabled = bool(enabled)
    _fallback_retention_days = int(retention_days) if retention_days else 7


def configure_request_log(enabled, retention_days=1):
    """Set request log config from caller (#355)."""
    global _request_log_enabled, _request_log_retention_days
    _request_log_enabled = bool(enabled)
    _request_log_retention_days = int(retention_days) if retention_days else 1


def _log_api_request(url, params, status_code, response_body, context,
                     latency_ms, home_team="", away_team="", event_id=""):
    """Append full API request/response to request log file (#355)."""
    if not _request_log_enabled:
        return
    # Strip API key from logged params
    safe_params = {k: v for k, v in (params or {}).items() if k != "apiKey"}
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "request_url": url,
        "request_params": safe_params,
        "response_status": status_code,
        "response_body": response_body,
        "context": context,
        "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
        "home_team": home_team,
        "away_team": away_team,
        "event_id": event_id,
    }
    try:
        with open(REQUEST_LOG_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def load_request_log():
    """Load all request log entries (#355)."""
    if not os.path.exists(REQUEST_LOG_FILE):
        return []
    entries = []
    try:
        with open(REQUEST_LOG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return entries


def get_request_log_entry(ts):
    """Get a single request log entry by timestamp (#355)."""
    for entry in load_request_log():
        if entry.get("ts") == ts:
            return entry
    return None


def prune_request_log(retention_days=None):
    """Remove request log entries older than retention period (#355)."""
    days = retention_days if retention_days is not None else _request_log_retention_days
    if not os.path.exists(REQUEST_LOG_FILE):
        return 0
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - days * 86400))
    entries = load_request_log()
    kept = [e for e in entries if e.get("ts", "") >= cutoff]
    pruned = len(entries) - len(kept)
    if pruned > 0:
        tmp = REQUEST_LOG_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                for e in kept:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, REQUEST_LOG_FILE)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    return pruned


def request_log_size():
    """Return request log file size in bytes (#355)."""
    try:
        return os.path.getsize(REQUEST_LOG_FILE) if os.path.exists(REQUEST_LOG_FILE) else 0
    except Exception:
        return 0


def _save_fallback_data(ts, odds_result, raw_event):
    """Append full raw event data to fallback data file (#341)."""
    if not _fallback_save_enabled or not raw_event:
        return
    entry = {
        "ts": ts,
        "home_team": raw_event.get("home_team", ""),
        "away_team": raw_event.get("away_team", ""),
        "event_id": raw_event.get("id", ""),
        "sport_key": raw_event.get("sport_key", ""),
        "fallback_log": odds_result.get("fallback_log", []),
        "ml_source": odds_result.get("ml_source", ""),
        "spread_source": odds_result.get("spread_source", ""),
        "totals_source": odds_result.get("totals_source", ""),
        "raw_event": raw_event,
    }
    try:
        with open(FALLBACK_DATA_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def load_fallback_data():
    """Load all fallback data entries (#341)."""
    if not os.path.exists(FALLBACK_DATA_FILE):
        return []
    entries = []
    try:
        with open(FALLBACK_DATA_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    return entries


def get_fallback_data_entry(ts):
    """Get a single fallback data entry by timestamp (#341)."""
    for entry in load_fallback_data():
        if entry.get("ts") == ts:
            return entry
    return None


def prune_fallback_data(retention_days=None):
    """Remove fallback data entries older than retention period (#341)."""
    days = retention_days if retention_days is not None else _fallback_retention_days
    if not os.path.exists(FALLBACK_DATA_FILE):
        return 0
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(time.time() - days * 86400))
    entries = load_fallback_data()
    kept = [e for e in entries if e.get("ts", "") >= cutoff]
    pruned = len(entries) - len(kept)
    if pruned > 0:
        tmp = FALLBACK_DATA_FILE + ".tmp"
        try:
            with open(tmp, "w") as f:
                for e in kept:
                    f.write(json.dumps(e) + "\n")
            os.replace(tmp, FALLBACK_DATA_FILE)
        except Exception:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
    return pruned


def is_credits_low():
    """Return True if credits remaining is known and below threshold."""
    r = _credits["credits_remaining"]
    return r is not None and r <= LOW_CREDIT_THRESHOLD


def format_credit_status():
    """Return a one-line status string for logging/alerts."""
    c = _credits
    if c["api_calls"] == 0:
        return None  # no API calls this cycle
    remaining = c["credits_remaining"]
    rem_str = str(remaining) if remaining is not None else "?"
    return "Odds API: {} call{}, {} credits used, {} remaining".format(
        c["api_calls"], "s" if c["api_calls"] != 1 else "",
        c["credits_used"], rem_str,
    )


def fetch_live_game_odds(api_key, home_team, away_team, logger=None, context="",
                         quarter=None, sport_key=None):
    """Fetch live bookmaker odds for a specific game.

    Args:
        api_key: The Odds API key.
        home_team: ESPN home team display name (e.g., "Boston Celtics").
        away_team: ESPN away team display name (e.g., "Los Angeles Lakers").
        logger: Optional logger for diagnostics.
        context: Caller context for log records (e.g. "pw_fire", "pregame").
        quarter: Optional quarter string (e.g. "Q1") to include quarter/half
                 markets in the API request (#374).

    Returns dict with keys:
        home_spread, away_spread, home_ml, away_ml, bookmaker, fetched_ts
    Or None if odds unavailable or on any error.
    """
    if not api_key or requests is None:
        return None

    try:
        _markets = "spreads,h2h"
        if quarter and quarter in QUARTER_MARKETS:
            _markets += "," + QUARTER_MARKETS[quarter]
        events = _fetch_all_events(api_key, context=context, markets=_markets, sport_key=sport_key)
        if not events:
            return None

        event = _match_event(events, home_team, away_team)
        if not event:
            if logger:
                logger("[ODDS_API] No matching event for {} vs {}".format(
                    home_team, away_team))
            return None

        odds = extract_odds(event)
        if not odds:
            if logger:
                logger("[ODDS_API] No odds data in event for {} vs {}".format(
                    home_team, away_team))
            return None

        _track_bk_usage(odds, raw_event=event)
        # Log fallback audit trail
        for msg in odds.get("fallback_log", []):
            if logger:
                logger("[ODDS_API] {} vs {}: {}".format(home_team, away_team, msg))

        odds["fetched_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        odds["_raw_event"] = event  # for extract_quarter_half_odds()
        return odds

    except Exception as exc:
        _credits["errors"] += 1
        if logger:
            logger("[ODDS_API] Error fetching odds: {}".format(exc))
        return None


def extract_team_odds(odds_data, team_name, home_team, away_team):
    """Extract spread and moneyline for a specific team from odds_data.

    Returns dict: {spread, spread_price, moneyline, bookmaker, fetched_ts} or None.
    """
    if not odds_data:
        return None

    is_home = (team_name == home_team)
    prefix = "home" if is_home else "away"

    spread = odds_data.get("{}_spread".format(prefix))
    spread_price = odds_data.get("{}_spread_price".format(prefix))
    ml = odds_data.get("{}_ml".format(prefix))

    if spread is None and ml is None:
        return None

    return {
        "spread": spread if spread != "--" else None,
        "spread_price": spread_price,
        "moneyline": ml if ml != "--" else None,
        "bookmaker": odds_data.get("bookmaker"),
        "fetched_ts": odds_data.get("fetched_ts"),
    }


def extract_quarter_half_odds(odds_data, team_name, home_team, away_team, quarter):
    """Extract quarter and half odds for a specific team from odds_data.

    Args:
        odds_data: Dict returned by fetch_live_game_odds() — contains raw event data
                   with bookmaker markets.
        team_name: The team to extract odds for (predicted team name).
        home_team: ESPN home team display name.
        away_team: ESPN away team display name.
        quarter: Which quarter PW fired in ("Q1", "Q2", "Q3", "Q4").

    Returns dict with keys:
        q_ml, q_spread, q_spread_price, q_ml_source,
        h1_ml, h1_spread, h1_spread_price, h1_ml_source
    All values None if market not available or quarter is OT.
    """
    result = {
        "q_ml": None, "q_spread": None, "q_spread_price": None, "q_ml_source": None,
        "h1_ml": None, "h1_spread": None, "h1_spread_price": None, "h1_ml_source": None,
    }
    if not odds_data or not quarter or quarter not in QUARTER_MARKETS:
        return result

    raw_event = odds_data.get("_raw_event")
    if not raw_event:
        return result

    is_home = (team_name == home_team)
    q_num = quarter[1]  # "Q3" -> "3"

    # Quarter markets: h2h_q3, spreads_q3
    q_ml_market = "h2h_q{}".format(q_num)
    q_spr_market = "spreads_q{}".format(q_num)
    # Half markets: h2h_h1, spreads_h1 (only for Q1/Q2)
    h1_ml_market = "h2h_h1"
    h1_spr_market = "spreads_h1"

    bookmakers = raw_event.get("bookmakers") or []

    def _extract_from_market(market_key, bookmakers_list):
        """Find best bookmaker for a market, return (outcomes, bookmaker_key)."""
        bk_map = {}
        for bk in bookmakers_list:
            bk_key = bk.get("key", "")
            for mkt in bk.get("markets") or []:
                if mkt.get("key") == market_key:
                    bk_map[bk_key] = mkt.get("outcomes") or []
        if not bk_map:
            return None, None
        # Pick by priority
        chosen_key = None
        for pk in BOOKMAKER_PRIORITY:
            if pk in bk_map:
                chosen_key = pk
                break
        if not chosen_key:
            chosen_key = next(iter(bk_map))
        outcomes = bk_map[chosen_key]
        return outcomes, chosen_key

    def _team_outcome(outcomes, team_nm, home_tm, away_tm, is_hm):
        """Find the outcome for our team in an outcomes array."""
        if not outcomes:
            return None, None
        norm_team = _normalize_team_name(team_nm)
        for o in outcomes:
            if _normalize_team_name(o.get("name", "")) == norm_team:
                return o.get("price"), o.get("point")
        norm_home = _normalize_team_name(home_tm)
        norm_away = _normalize_team_name(away_tm)
        for o in outcomes:
            o_name = _normalize_team_name(o.get("name", ""))
            if is_hm and o_name == norm_home:
                return o.get("price"), o.get("point")
            if not is_hm and o_name == norm_away:
                return o.get("price"), o.get("point")
        return None, None

    # Extract quarter ML
    q_ml_outcomes, q_ml_bk = _extract_from_market(q_ml_market, bookmakers)
    if q_ml_outcomes:
        price, _ = _team_outcome(q_ml_outcomes, team_name, home_team, away_team, is_home)
        if price is not None:
            result["q_ml"] = price
            result["q_ml_source"] = q_ml_bk

    # Extract quarter spread
    q_spr_outcomes, q_spr_bk = _extract_from_market(q_spr_market, bookmakers)
    if q_spr_outcomes:
        price, point = _team_outcome(q_spr_outcomes, team_name, home_team, away_team, is_home)
        if point is not None:
            result["q_spread"] = point
            result["q_spread_price"] = price
            if not result["q_ml_source"]:
                result["q_ml_source"] = q_spr_bk

    # Extract H1 odds (only for Q1/Q2)
    if quarter in ("Q1", "Q2"):
        h1_ml_outcomes, h1_ml_bk = _extract_from_market(h1_ml_market, bookmakers)
        if h1_ml_outcomes:
            price, _ = _team_outcome(h1_ml_outcomes, team_name, home_team, away_team, is_home)
            if price is not None:
                result["h1_ml"] = price
                result["h1_ml_source"] = h1_ml_bk

        h1_spr_outcomes, h1_spr_bk = _extract_from_market(h1_spr_market, bookmakers)
        if h1_spr_outcomes:
            price, point = _team_outcome(h1_spr_outcomes, team_name, home_team, away_team, is_home)
            if point is not None:
                result["h1_spread"] = point
                result["h1_spread_price"] = price
                if not result["h1_ml_source"]:
                    result["h1_ml_source"] = h1_spr_bk

    return result


def _fetch_all_events(api_key, context="", markets="spreads,h2h", sport_key=None):
    """Fetch all live/upcoming events, with in-process caching."""
    _sport = sport_key or SPORT_KEY
    now = time.time()
    cache_key = _sport + "|" + markets
    if _cache["data"] is not None and (now - _cache["ts"]) < _cache["ttl"] and _cache.get("markets") == cache_key:
        log_call(_sport, markets, 0, _credits.get("credits_remaining"),
                 200, 0, True, context)
        return _cache["data"]

    url = "{}/sports/{}/odds".format(ODDS_API_BASE, _sport)
    params = {
        "apiKey": api_key,
        "regions": "us",
        "markets": markets,
        "oddsFormat": "american",
    }

    t0 = time.perf_counter()
    resp = requests.get(url, params=params, timeout=15)
    latency_ms = (time.perf_counter() - t0) * 1000

    # Fallback: if expanded markets (quarter/half) cause 422, retry with base markets (#374, #375)
    if resp.status_code == 422 and markets != "spreads,h2h":
        log_call(_sport, markets, 0, _credits.get("credits_remaining"),
                 422, latency_ms, False, context + "_422_fallback")
        _base_markets = "spreads,h2h"
        params["markets"] = _base_markets
        t0 = time.perf_counter()
        resp = requests.get(url, params=params, timeout=15)
        latency_ms = (time.perf_counter() - t0) * 1000
        markets = _base_markets  # use base for logging/cache

    resp.raise_for_status()

    events = resp.json()

    _log_api_request(url, params, resp.status_code, events,
                     context or "live_poll", latency_ms)

    credits_used, credits_remaining = _usage_from_headers(resp)

    _credits["api_calls"] += 1
    _credits["credits_used"] += credits_used
    _credits["credits_remaining"] = credits_remaining
    _credits["last_call_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    log_call(_sport, markets, credits_used, credits_remaining,
             resp.status_code, latency_ms, False, context)

    _cache["data"] = events
    _cache["ts"] = now
    _cache["markets"] = cache_key
    return events


def _match_event(events, home_team, away_team):
    """Find the matching event by team names."""
    for ev in events:
        ev_home = _normalize_team_name(ev.get("home_team", ""))
        ev_away = _normalize_team_name(ev.get("away_team", ""))
        if ev_home == home_team and ev_away == away_team:
            return ev
    # Fallback: try matching just one side (handles name mismatches)
    for ev in events:
        ev_home = _normalize_team_name(ev.get("home_team", ""))
        ev_away = _normalize_team_name(ev.get("away_team", ""))
        if ev_home == home_team or ev_away == away_team:
            # Verify the other team is also present
            if ev_home == home_team and ev_away == away_team:
                return ev
            if ev_home == home_team or ev_away == away_team:
                return ev
    return None
