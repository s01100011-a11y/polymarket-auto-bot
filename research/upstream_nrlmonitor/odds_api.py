"""odds_api.py — Fetch live bookmaker odds from The Odds API for NRL.

Provides fetch_live_game_odds() for attaching spread/moneyline to alerts
and game history. Includes persistent usage logging and credit tracking.

Sport key: rugbyleague_nrl
Australian bookmaker priority: TAB > PointsBet AU > Ladbrokes
"""

import json
import os
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    import requests
except ImportError:
    requests = None

# ── Constants ────────────────────────────────────────────────────────────────

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
SPORT_KEY = "rugbyleague_nrl"
SPORT_KEY_SOO = "rugbyleague_nrl_state_of_origin"  # (#102)
MARKETS = "h2h,spreads"  # default: 2 credits/call live, 20 historical
MARKETS_WITH_TOTALS = "h2h,spreads,totals"  # 3 credits/call live, 30 historical

# Australian bookmaker priority — TAB primary for NRL (#204)
# Bet365 not available for NRL on The Odds API (AU region)
# Sportsbet removed (#204)
BOOKMAKER_PRIORITY = ["tab", "pointsbetau", "ladbrokes_au",
                      "unibet", "neds", "tabtouch", "betright", "betr_au",
                      "playup", "betfair_ex_au"]

# NRL team names as they appear on The Odds API → our abbreviations
TEAM_MAP = {
    "Brisbane Broncos": "BRI",
    "Canterbury Bulldogs": "CBY",
    "Canterbury-Bankstown Bulldogs": "CBY",
    "North Queensland Cowboys": "NQC",
    "Dolphins": "DOL",
    "Redcliffe Dolphins": "DOL",
    "St George Illawarra Dragons": "SGI",
    "St. George Illawarra Dragons": "SGI",
    "Parramatta Eels": "PAR",
    "Newcastle Knights": "NEW",
    "Penrith Panthers": "PEN",
    "South Sydney Rabbitohs": "SOU",
    "Canberra Raiders": "CAN",
    "Sydney Roosters": "SYD",
    "Manly Sea Eagles": "MAN",
    "Manly Warringah Sea Eagles": "MAN",
    "Cronulla Sharks": "CRO",
    "Cronulla-Sutherland Sharks": "CRO",
    "Cronulla Sutherland Sharks": "CRO",
    "Melbourne Storm": "MEL",
    "Wests Tigers": "WST",
    "Gold Coast Titans": "GCT",
    "New Zealand Warriors": "WAR",
}

# Reverse: NRL.com short names → Odds API lookup names
NRL_TO_ODDS_API = {
    "Broncos": "Brisbane Broncos",
    "Bulldogs": "Canterbury Bulldogs",
    "Cowboys": "North Queensland Cowboys",
    "Dolphins": "Dolphins",
    "Dragons": "St George Illawarra Dragons",
    "Eels": "Parramatta Eels",
    "Knights": "Newcastle Knights",
    "Panthers": "Penrith Panthers",
    "Rabbitohs": "South Sydney Rabbitohs",
    "Raiders": "Canberra Raiders",
    "Roosters": "Sydney Roosters",
    "Sea Eagles": "Manly Sea Eagles",
    "Sharks": "Cronulla Sharks",
    "Storm": "Melbourne Storm",
    "Wests Tigers": "Wests Tigers",
    "Titans": "Gold Coast Titans",
    "Warriors": "New Zealand Warriors",
    # State of Origin (#102)
    "Blues": "New South Wales Blues",
    "Maroons": "Queensland Maroons",
}


def _normalize(name):
    return name.strip().lower()


# ── Persistent usage log ─────────────────────────────────────────────────────

LOG_FILE = os.path.join(SCRIPT_DIR, "odds_api_log.jsonl")
_LOG_FILE_LEGACY = os.path.join(SCRIPT_DIR, "odds_api_log.json")
_MAX_LOG_ENTRIES = 5000


def _append_log(record):
    """Append a single log entry as a JSONL line — append-only, no race condition (#84)."""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _usage_from_headers(resp):
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
    """Check Odds API usage at monitor startup (free /v4/sports/ call)."""
    if not api_key or requests is None:
        return None
    try:
        resp = requests.get("{}/sports/".format(ODDS_API_BASE),
                            params={"apiKey": api_key}, timeout=10)
        resp.raise_for_status()
        _, credits_remaining = _usage_from_headers(resp)
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _append_log({"type": "startup", "ts": ts, "credits_remaining": credits_remaining})
        if logger:
            logger("[ODDS_API] Startup: {} credits remaining".format(
                credits_remaining if credits_remaining is not None else "?"))
        return {"credits_remaining": credits_remaining}
    except Exception as exc:
        if logger:
            logger("[ODDS_API] Startup check failed: {}".format(exc))
        return None


def log_call(markets, credits_used, credits_remaining, status_code,
             latency_ms, cache_hit, context=""):
    _append_log({
        "type": "call",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sport": SPORT_KEY,
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


def _migrate_legacy_log():
    """One-time migration from JSON array to JSONL format (#84)."""
    if not os.path.exists(_LOG_FILE_LEGACY):
        return
    if os.path.exists(LOG_FILE):
        return  # already migrated
    try:
        with open(_LOG_FILE_LEGACY, "r") as f:
            data = f.read()
        # Handle corrupted file (concatenated arrays)
        entries = []
        decoder = json.JSONDecoder()
        start = 0
        while start < len(data):
            while start < len(data) and data[start] in " \t\n\r":
                start += 1
            if start >= len(data):
                break
            try:
                obj, end = decoder.raw_decode(data, start)
                if isinstance(obj, list):
                    entries.extend(obj)
                else:
                    entries.append(obj)
                start = end
            except json.JSONDecodeError:
                start += 1
        if entries:
            with open(LOG_FILE, "w") as f:
                for e in entries:
                    f.write(json.dumps(e, default=str) + "\n")
            os.rename(_LOG_FILE_LEGACY, _LOG_FILE_LEGACY + ".migrated")
    except Exception:
        pass


def truncate_log(keep=None):
    """Truncate JSONL log to keep last N entries. Called during scheduled backups."""
    keep = keep or _MAX_LOG_ENTRIES
    entries = load_log()
    if len(entries) <= keep:
        return len(entries)
    entries = entries[-keep:]
    tmp = LOG_FILE + ".tmp.{}".format(os.getpid())
    try:
        with open(tmp, "w") as f:
            for e in entries:
                f.write(json.dumps(e, default=str) + "\n")
        os.replace(tmp, LOG_FILE)
    except Exception:
        pass
    return len(entries)


def load_log():
    """Load log entries from JSONL file. Migrates legacy JSON array on first call."""
    _migrate_legacy_log()
    entries = []
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, "r") as f:
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


# ── Credit tracking ──────────────────────────────────────────────────────────

_cache = {"data": None, "ts": 0, "ttl": 120}  # live game context (2min)
_upcoming_cache = {"data": None, "ts": 0, "ttl": 900}  # upcoming/pre-game (15min default)
_odds_monitor_cache = {"data": None, "ts": 0, "ttl": 25}  # odds monitor (25s for 30s polling)

# Rate limit backoff: skip API calls until this timestamp
_rate_limit_until = 0
_RATE_LIMIT_BACKOFF = 60  # seconds to back off after 429

_credits = {
    "api_calls": 0,
    "credits_used": 0,
    "credits_remaining": None,
    "last_call_ts": None,
    "errors": 0,
}

LOW_CREDIT_THRESHOLD = 50

# Per-bookmaker market tracking (accumulated from extract_odds results)
_bk_stats = {}  # bk_name -> {"ml": count, "spread": count, "totals": count, "primary": count, "fallback": count}
_fallback_events = []  # list of {ts, message} — capped at 200

# Fallback request/response data saving (#179)
_fallback_save_enabled = False
_fallback_retention_days = 7
FALLBACK_DATA_FILE = os.path.join(SCRIPT_DIR, "odds_api_fallback_data.jsonl")


def _track_bk_usage(odds_result, raw_event=None):
    """Track which bookmaker provided each market from an extract_odds result.
    Persists to log for cross-process visibility (#97).
    Saves full raw event data on fallback (#179).
    """
    if not odds_result:
        return
    ml_src = odds_result.get("ml_source", "")
    spr_src = odds_result.get("spread_source", "")
    tot_src = odds_result.get("totals_source", "")

    # In-memory accumulation
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

    # Persist BK usage to log (#97) — enables server to compute stats from log
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _append_log({"type": "bk_usage", "ts": ts,
                 "ml_source": ml_src, "spread_source": spr_src,
                 "totals_source": tot_src,
                 "is_fallback": bool(odds_result.get("fallback_log"))})

    # Track fallback events (in-memory + persistent log)
    has_fallback = False
    for msg in odds_result.get("fallback_log", []):
        has_fallback = True
        _fallback_events.append({"ts": ts, "message": msg})
        if len(_fallback_events) > 200:
            _fallback_events.pop(0)
        _append_log({"type": "fallback", "ts": ts, "message": msg,
                     "ml_source": ml_src, "spread_source": spr_src,
                     "totals_source": tot_src})

    # Save full raw event data on fallback (#179)
    if has_fallback:
        _save_fallback_data(ts, odds_result, raw_event)


def get_bk_stats():
    """Return per-bookmaker market usage stats."""
    return dict(_bk_stats)


def get_fallback_events():
    """Return recent fallback events."""
    return list(_fallback_events)


def configure_fallback_save(enabled, retention_days=7):
    """Set fallback data save config from caller (#179)."""
    global _fallback_save_enabled, _fallback_retention_days
    _fallback_save_enabled = bool(enabled)
    _fallback_retention_days = int(retention_days) if retention_days else 7


def _save_fallback_data(ts, odds_result, raw_event):
    """Append full raw event data to fallback data file (#179)."""
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
    """Load all fallback data entries (#179)."""
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
    """Get a single fallback data entry by timestamp (#179)."""
    for entry in load_fallback_data():
        if entry.get("ts") == ts:
            return entry
    return None


def prune_fallback_data(retention_days=None):
    """Remove fallback data entries older than retention period (#179)."""
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


def get_credit_status():
    return dict(_credits)


_live_credit_cache = {"credits_remaining": None, "ts": 0}
_LIVE_CREDIT_TTL = 60  # cache for 60s


def query_live_credits(api_key):
    """Query live credit balance from /v4/sports/ (free call). Cached for 60s."""
    now = time.time()
    if _live_credit_cache["credits_remaining"] is not None and (now - _live_credit_cache["ts"]) < _LIVE_CREDIT_TTL:
        return _live_credit_cache.copy()
    if not api_key or requests is None:
        return {"credits_remaining": None, "ts": None, "error": "no api key"}
    try:
        resp = requests.get("{}/sports/".format(ODDS_API_BASE),
                            params={"apiKey": api_key}, timeout=10)
        _, remaining = _usage_from_headers(resp)
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
    _credits["api_calls"] = 0
    _credits["credits_used"] = 0
    _credits["errors"] = 0


def is_credits_low():
    r = _credits["credits_remaining"]
    return r is not None and r <= LOW_CREDIT_THRESHOLD


def format_credit_status():
    c = _credits
    if c["api_calls"] == 0:
        return None
    remaining = c["credits_remaining"]
    rem_str = str(remaining) if remaining is not None else "?"
    return "Odds API: {} call{}, {} credits used, {} remaining".format(
        c["api_calls"], "s" if c["api_calls"] != 1 else "",
        c["credits_used"], rem_str)


# ── Odds extraction ──────────────────────────────────────────────────────────

def extract_odds(event):
    """Extract best available odds from an Odds API event.

    Returns dict with home_spread, away_spread, home_ml, away_ml, bookmaker,
    per-market source fields (ml_source, spread_source, totals_source),
    and audit trail (fallback_log list) — or None.
    """
    bookmakers = event.get("bookmakers", [])
    if not bookmakers:
        return None

    # Sort by priority
    bk_map = {bk.get("key", "").lower(): bk for bk in bookmakers}
    ordered = []
    for name in BOOKMAKER_PRIORITY:
        if name in bk_map:
            ordered.append(bk_map[name])
    for bk in bookmakers:
        if bk not in ordered:
            ordered.append(bk)

    home_team = event.get("home_team", "")
    away_team = event.get("away_team", "")

    # Build result by merging across bookmakers, tracking source per market
    result = {}
    fallback_log = []
    # Resolve primary to the TITLE of the first priority BK in the response (#96)
    primary_name = ""
    for _pk in BOOKMAKER_PRIORITY:
        if _pk in bk_map:
            _pbk = bk_map[_pk]
            primary_name = _pbk.get("title", _pbk.get("key", ""))
            break
    if not primary_name and ordered:
        primary_name = ordered[0].get("title", ordered[0].get("key", ""))

    for bk in ordered:
        bk_name = bk.get("title", bk.get("key", ""))
        bk_markets = {m.get("key", ""): m for m in bk.get("markets", [])}

        # H2H (moneyline) — take from first bookmaker that has it
        h2h = bk_markets.get("h2h")
        if h2h and not result.get("home_ml"):
            for outcome in h2h.get("outcomes", []):
                name = outcome.get("name", "")
                price = outcome.get("price")
                if _normalize(name) == _normalize(home_team):
                    result["home_ml"] = price
                elif _normalize(name) == _normalize(away_team):
                    result["away_ml"] = price
            if result.get("home_ml"):
                result["bookmaker"] = bk_name
                result["ml_source"] = bk_name
                if bk_name != primary_name:
                    fallback_log.append("ML from {} (primary {} unavailable: no h2h market)".format(
                        bk_name, primary_name))
        elif not result.get("home_ml") and not h2h:
            pass  # this bk has no h2h, will try next

        # Spreads — take from first bookmaker that has it
        spreads = bk_markets.get("spreads")
        if spreads and not result.get("home_spread"):
            for outcome in spreads.get("outcomes", []):
                name = outcome.get("name", "")
                point = outcome.get("point")
                price = outcome.get("price")
                if _normalize(name) == _normalize(home_team):
                    result["home_spread"] = point
                    result["home_spread_price"] = price
                elif _normalize(name) == _normalize(away_team):
                    result["away_spread"] = point
                    result["away_spread_price"] = price
            if result.get("home_spread"):
                result["spread_source"] = bk_name
                if not result.get("bookmaker"):
                    result["bookmaker"] = bk_name
                ml_src = result.get("ml_source", "")
                if ml_src and bk_name != ml_src:
                    fallback_log.append("Spread from {} (primary {} unavailable: no spreads market)".format(
                        bk_name, ml_src))

        # Totals — take from first bookmaker that has it
        totals = bk_markets.get("totals")
        if totals and not result.get("total_over"):
            for outcome in totals.get("outcomes", []):
                if outcome.get("name") == "Over":
                    result["total_over"] = outcome.get("point")
                    result["total_over_price"] = outcome.get("price")
                elif outcome.get("name") == "Under":
                    result["total_under"] = outcome.get("point")
                    result["total_under_price"] = outcome.get("price")
            if result.get("total_over"):
                result["totals_source"] = bk_name
                ml_src = result.get("ml_source", primary_name)
                if bk_name != ml_src:
                    fallback_log.append("Totals from {} (primary {} unavailable: no totals market)".format(
                        bk_name, ml_src))

    if fallback_log:
        result["fallback_log"] = fallback_log

    if result.get("home_ml") or result.get("home_spread"):
        _track_bk_usage(result, raw_event=event)
        return result

    return None


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _is_rate_limited():
    """Check if we're in a rate-limit backoff period."""
    global _rate_limit_until
    return time.time() < _rate_limit_until


def _set_rate_limited():
    """Enter rate-limit backoff."""
    global _rate_limit_until
    _rate_limit_until = time.time() + _RATE_LIMIT_BACKOFF


def _fetch_all_events(api_key, context="", markets=None, sport_key=None):
    """Fetch all NRL events with odds. Uses module-level cache.

    Cross-populates the odds monitor cache when data is fresh enough,
    and respects rate-limit backoff.
    """
    now = time.time()

    # Check own cache first
    if _cache["data"] is not None and (now - _cache["ts"]) < _cache["ttl"]:
        return _cache["data"]

    # Check odds monitor cache as fallback (it's a superset with totals)
    if (_odds_monitor_cache["data"] is not None
            and (now - _odds_monitor_cache["ts"]) < _cache["ttl"]):
        return _odds_monitor_cache["data"]

    if requests is None:
        return None

    # Rate limit backoff
    if _is_rate_limited():
        # Return stale cache if available
        if _cache["data"] is not None:
            return _cache["data"]
        if _odds_monitor_cache["data"] is not None:
            return _odds_monitor_cache["data"]
        return None

    use_markets = markets or MARKETS
    use_sport = sport_key or SPORT_KEY
    url = "{}/sports/{}/odds/".format(ODDS_API_BASE, use_sport)
    params = {
        "apiKey": api_key,
        "regions": "au",
        "markets": use_markets,
        "oddsFormat": "decimal",
    }

    t0 = time.time()
    try:
        resp = requests.get(url, params=params, timeout=15)
        latency = (time.time() - t0) * 1000
        credits_used, credits_remaining = _usage_from_headers(resp)

        _credits["api_calls"] += 1
        _credits["credits_used"] += credits_used
        _credits["credits_remaining"] = credits_remaining
        _credits["last_call_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        log_call(use_markets, credits_used, credits_remaining,
                 resp.status_code, latency, False, context)

        if resp.status_code == 429:
            _set_rate_limited()
            _credits["errors"] += 1
            # Return stale cache
            return _cache["data"] or _odds_monitor_cache["data"]

        resp.raise_for_status()
        data = resp.json()
        now_ts = time.time()
        _cache["data"] = data
        _cache["ts"] = now_ts
        # Cross-populate odds monitor cache (data is compatible)
        _odds_monitor_cache["data"] = data
        _odds_monitor_cache["ts"] = now_ts
        return data

    except Exception as exc:
        _credits["errors"] += 1
        latency = (time.time() - t0) * 1000

        # Check for 429 in the exception
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status == 429:
            _set_rate_limited()

        log_call(MARKETS, 0, _credits["credits_remaining"],
                 status, latency, False, context)
        return None


def _match_event(events, home_team_nrl, away_team_nrl):
    """Match an NRL.com team pair to an Odds API event."""
    # Convert NRL short names to Odds API names
    home_lookup = NRL_TO_ODDS_API.get(home_team_nrl, home_team_nrl)
    away_lookup = NRL_TO_ODDS_API.get(away_team_nrl, away_team_nrl)

    for event in events:
        eh = _normalize(event.get("home_team", ""))
        ea = _normalize(event.get("away_team", ""))

        # Try exact match
        if (_normalize(home_lookup) == eh and _normalize(away_lookup) == ea):
            return event

        # Try fuzzy: check if our lookup name is contained in the event name
        if (_normalize(home_lookup) in eh or eh in _normalize(home_lookup)):
            if (_normalize(away_lookup) in ea or ea in _normalize(away_lookup)):
                return event

        # Try abbreviation match via TEAM_MAP
        eh_abbr = TEAM_MAP.get(event.get("home_team", ""), "")
        ea_abbr = TEAM_MAP.get(event.get("away_team", ""), "")
        from nrl_api import TEAM_ABBREVS
        h_abbr = TEAM_ABBREVS.get(home_team_nrl, "")
        a_abbr = TEAM_ABBREVS.get(away_team_nrl, "")
        if eh_abbr and ea_abbr and eh_abbr == h_abbr and ea_abbr == a_abbr:
            return event

    return None


def fetch_upcoming_odds(api_key, cache_ttl=None, markets=None):
    """Fetch all NRL odds for upcoming/pre-game context with long cache.

    Uses a separate cache from live game odds (default 15 min vs 2 min).
    Returns list of events with odds, or None.
    """
    if not api_key or requests is None:
        return None

    ttl = cache_ttl or _upcoming_cache["ttl"]
    now = time.time()
    if _upcoming_cache["data"] is not None and (now - _upcoming_cache["ts"]) < ttl:
        return _upcoming_cache["data"]

    # Fetch fresh data
    events = _fetch_all_events(api_key, context="upcoming", markets=markets)
    if events is not None:
        _upcoming_cache["data"] = events
        _upcoming_cache["ts"] = time.time()
    return events


def set_upcoming_cache_ttl(ttl):
    """Set the upcoming odds cache TTL in seconds."""
    _upcoming_cache["ttl"] = max(60, int(ttl))


def fetch_live_game_odds(api_key, home_team, away_team, logger=None, context="", sport_key=None):
    """Fetch live bookmaker odds for a specific NRL game.

    Args:
        api_key: The Odds API key.
        home_team: NRL.com home team short name (e.g. "Storm").
        away_team: NRL.com away team short name (e.g. "Panthers").
        sport_key: Override sport key (e.g. SPORT_KEY_SOO for State of Origin).

    Returns dict with home_spread, away_spread, home_ml, away_ml, bookmaker, fetched_ts
    or None.
    """
    if not api_key or requests is None:
        return None

    try:
        events = _fetch_all_events(api_key, context=context, sport_key=sport_key)
        if not events:
            return None

        event = _match_event(events, home_team, away_team)
        if not event:
            if logger:
                logger("[ODDS_API] No event for {} vs {}".format(home_team, away_team))
            return None

        odds = extract_odds(event)
        if not odds:
            if logger:
                logger("[ODDS_API] No odds in event for {} vs {}".format(home_team, away_team))
            return None

        odds["fetched_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return odds

    except Exception as exc:
        _credits["errors"] += 1
        if logger:
            logger("[ODDS_API] Error: {}".format(exc))
        return None


def fetch_odds_monitor_data(api_key, logger=None):
    """Fetch all NRL events with h2h+spreads+totals for the Odds Monitor.

    Uses a separate 25s cache. Cross-populates the main cache since
    totals response is a superset of h2h+spreads. Respects rate limiting.
    Returns list of events or None.
    """
    if not api_key or requests is None:
        return None

    now = time.time()

    # Check own cache first
    if (_odds_monitor_cache["data"] is not None
            and (now - _odds_monitor_cache["ts"]) < _odds_monitor_cache["ttl"]):
        return _odds_monitor_cache["data"]

    # Check main cache as fallback (may lack totals but better than nothing)
    if _cache["data"] is not None and (now - _cache["ts"]) < _odds_monitor_cache["ttl"]:
        return _cache["data"]

    # Rate limit backoff
    if _is_rate_limited():
        if logger:
            logger("[ODDS_API] Rate limited, using stale cache")
        return _odds_monitor_cache["data"] or _cache["data"]

    url = "{}/sports/{}/odds/".format(ODDS_API_BASE, SPORT_KEY)
    params = {
        "apiKey": api_key,
        "regions": "au",
        "markets": MARKETS_WITH_TOTALS,
        "oddsFormat": "decimal",
    }

    t0 = time.time()
    try:
        resp = requests.get(url, params=params, timeout=15)
        latency = (time.time() - t0) * 1000
        credits_used, credits_remaining = _usage_from_headers(resp)

        _credits["api_calls"] += 1
        _credits["credits_used"] += credits_used
        _credits["credits_remaining"] = credits_remaining
        _credits["last_call_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        log_call(MARKETS_WITH_TOTALS, credits_used, credits_remaining,
                 resp.status_code, latency, False, "odds_monitor")

        if resp.status_code == 429:
            _set_rate_limited()
            _credits["errors"] += 1
            if logger:
                logger("[ODDS_API] Rate limited (429), backing off {}s".format(
                    _RATE_LIMIT_BACKOFF))
            return _odds_monitor_cache["data"] or _cache["data"]

        resp.raise_for_status()
        data = resp.json()
        now_ts = time.time()
        _odds_monitor_cache["data"] = data
        _odds_monitor_cache["ts"] = now_ts
        # Cross-populate main cache (totals is superset)
        _cache["data"] = data
        _cache["ts"] = now_ts
        return data

    except Exception as exc:
        _credits["errors"] += 1
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        if status == 429:
            _set_rate_limited()
            if logger:
                logger("[ODDS_API] Rate limited (429), backing off {}s".format(
                    _RATE_LIMIT_BACKOFF))
        else:
            if logger:
                logger("[ODDS_API] Odds monitor fetch error: {}".format(exc))
        return _odds_monitor_cache["data"] or _cache["data"]


def format_spread_detail(odds_data, home_team, away_team):
    """Format a human-readable spread string like 'Storm -4.5'."""
    if not odds_data:
        return None
    hs = odds_data.get("home_spread")
    if hs is not None:
        if hs < 0:
            return "{} {}".format(home_team, hs)
        elif hs > 0:
            return "{} {}".format(away_team, -hs)
        else:
            return "Pick'em"
    return None
