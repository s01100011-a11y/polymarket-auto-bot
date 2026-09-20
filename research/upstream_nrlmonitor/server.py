#!/usr/bin/env python3
"""
NRL Monitor — Dashboard server
Serves static files + handles config read/write via /api/* endpoints.
Usage: python3 server.py [port]   (default: 8899)
"""

import gc
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nrl_api
import odds_monitor

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
SECRET_KEYS = {"slack_bot_token", "openclaw_token", "odds_api_key"}  # #258 Phase 2.2/2.5
STATIC_ALLOWLIST = {"dashboard.html", "favicon.ico", "pw_roi_report.html"}  # #258 Phase 2.2

def _safe_backup_name(name, backup_root):
    """Validate and sanitise a backup directory name. Returns (safe_name, error)."""
    name = os.path.basename(str(name or "").strip())
    if not name or ".." in name or "/" in name:
        return None, "invalid backup name"
    full = os.path.realpath(os.path.join(backup_root, name))
    if not full.startswith(os.path.realpath(backup_root) + os.sep):
        return None, "path escapes backup root"
    return name, None
ALERTS_FILE = os.path.join(SCRIPT_DIR, "alerts.json")
LIVE_STATS_FILE = os.path.join(SCRIPT_DIR, "live_stats.json")
TICKER_FILE = os.path.join(SCRIPT_DIR, "game_ticker.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8898

MIME = {
    ".html": "text/html",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
}

CONDITION_TYPES = {
    "score_diff", "error_rate", "completion_rate",
    "penalty_count", "possession", "try_scoring_run",
    "points_run", "missed_tackles",
    "momentum_shift", "error_streak", "penalty_pressure",
    "sin_bin", "line_break_surge", "halftime_turnaround",
    "run_metres", "post_contact_metres", "tackle_breaks",
    "line_breaks", "offloads", "intercepts",
    "ineffective_tackles", "effective_tackle_pct", "kick_defusal",
    "kick_return_metres", "play_the_ball_speed", "red_card",
    "first_team_scores", "predicted_winner_threshold", "pw_underdog_alert",
}


def _log(*parts):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = " ".join(str(p) for p in parts)
    print("{} {}".format(ts, msg), flush=True)


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


# Alert-state cache — avoids re-parsing .alert_state.json every 15s (#423)
_alert_state_cache_lock = threading.Lock()
_alert_state_cache = {"mtime": None, "data": None}


def _load_alert_state_cached():
    state_file = os.path.join(SCRIPT_DIR, ".alert_state.json")
    try:
        mtime = os.path.getmtime(state_file)
    except OSError:
        return {}
    with _alert_state_cache_lock:
        if _alert_state_cache["mtime"] == mtime and _alert_state_cache["data"] is not None:
            return _alert_state_cache["data"]
    try:
        with open(state_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    with _alert_state_cache_lock:
        _alert_state_cache["mtime"] = mtime
        _alert_state_cache["data"] = data
    return data


def validate_config(cfg):
    """Validate config payload. Returns error string or None."""
    if not isinstance(cfg, dict):
        return "config must be a JSON object"
    conditions = cfg.get("conditions", [])
    if not isinstance(conditions, list):
        return "conditions must be an array"
    names = set()
    for i, c in enumerate(conditions):
        name = c.get("name", "")
        if not name:
            return "condition {} has no name".format(i)
        if name in names:
            return "duplicate condition name: {}".format(name)
        names.add(name)
        ctype = c.get("type", "")
        if ctype and ctype not in CONDITION_TYPES:
            return "unknown condition type: {}".format(ctype)

    compounds = cfg.get("compound_conditions", [])
    if not isinstance(compounds, list):
        return "compound_conditions must be an array"
    for comp in compounds:
        for ref in comp.get("condition_refs", []):
            if ref not in names:
                return "compound ref '{}' not found in conditions".format(ref)
    return None


def validate_config_warnings(cfg):
    """Return list of non-blocking warnings about config issues."""
    warnings = []
    global_threshold = cfg.get("predicted_winner_threshold", 65)
    pw_conds = [c for c in cfg.get("conditions", [])
                if c.get("type") == "predicted_winner_threshold"]
    for c in pw_conds:
        t = c.get("threshold")
        if t is not None:
            try:
                if float(t) < float(global_threshold):
                    warnings.append(
                        "PW alert '{}' threshold ({}) is lower than global "
                        "predicted_winner_threshold ({}). PW calls won't record "
                        "below {} — alerts may fire without a matching PW call. "
                        "The monitor auto-aligns at runtime, but consider lowering "
                        "the global threshold.".format(
                            c.get("name", ""), t, global_threshold, global_threshold))
            except (TypeError, ValueError):
                pass
    return warnings


# ── Upcoming games cache ──────────────────────────────────────────────────────

_upcoming_cache = {"payload": None, "ts": 0.0}
_upcoming_lock = threading.Lock()
UPCOMING_CACHE_TTL = 120  # seconds


def build_upcoming_payload(config):
    """Fetch upcoming NRL games for the current round.

    Returns a list of upcoming game dicts with kickoff times and team info.
    Cached for UPCOMING_CACHE_TTL seconds.
    """
    now_ts = time.time()
    with _upcoming_lock:
        if _upcoming_cache["payload"] is not None and (now_ts - _upcoming_cache["ts"]) < UPCOMING_CACHE_TTL:
            return _upcoming_cache["payload"]

    season = int((config or {}).get("season", 2026))
    upcoming_hours = max(1, min(336, int((config or {}).get("upcoming_hours", 168))))
    now_utc = datetime.now(timezone.utc)
    deadline = now_utc + timedelta(hours=upcoming_hours)

    games = []
    try:
        rnd, fixtures = nrl_api.get_current_round_fixtures(season)
        if not fixtures:
            result = {"games": [], "round": None, "window_hours": upcoming_hours}
            with _upcoming_lock:
                _upcoming_cache["payload"] = result
                _upcoming_cache["ts"] = time.time()
            return result

        # Fetch ladder data and Odds API data
        ladder = nrl_api.get_ladder(season)
        odds_by_game = {}
        if (config or {}).get("odds_api_enabled"):
            try:
                import odds_api as odds_mod
                api_key = (config or {}).get("odds_api_key", "")
                if api_key:
                    # Adaptive cache TTL: shorter closer to kickoff, longer otherwise
                    # Find earliest upcoming kickoff
                    _min_hours_to_ko = 999
                    for fix in fixtures:
                        if fix.get("matchState") not in nrl_api.PRE_STATES:
                            continue
                        ko_str = fix.get("clock", {}).get("kickOffTimeLong", "") or fix.get("startTime", "")
                        if ko_str:
                            try:
                                ko_dt = datetime.fromisoformat(ko_str.replace("Z", "+00:00"))
                                if ko_dt.tzinfo is None:
                                    ko_dt = ko_dt.replace(tzinfo=timezone.utc)
                                _hrs = (ko_dt - now_utc).total_seconds() / 3600
                                if _hrs > 0:
                                    _min_hours_to_ko = min(_min_hours_to_ko, _hrs)
                            except Exception:
                                pass
                    # Graduated TTL: 30min within 2h, 1h within 6h, 2h within 24h, skip beyond
                    if _min_hours_to_ko <= 2:
                        odds_ttl = 1800   # 30 min
                    elif _min_hours_to_ko <= 6:
                        odds_ttl = 3600   # 1 hour
                    elif _min_hours_to_ko <= 24:
                        odds_ttl = 7200   # 2 hours
                    else:
                        odds_ttl = 0      # skip — no games within 24h
                    # Allow config override (minimum)
                    _cfg_ttl = int((config or {}).get("odds_api_upcoming_cache_ttl", 0))
                    if _cfg_ttl > 0:
                        odds_ttl = max(odds_ttl, _cfg_ttl)
                    has_soon = odds_ttl > 0
                    if has_soon:
                        odds_mod.set_upcoming_cache_ttl(odds_ttl)
                        odds_markets = (config or {}).get("odds_api_markets", odds_mod.MARKETS)
                        events = odds_mod.fetch_upcoming_odds(api_key, cache_ttl=odds_ttl, markets=odds_markets)
                        if events:
                            for ev in events:
                                odds = odds_mod.extract_odds(ev)
                                if odds:
                                    key = "{}|{}".format(
                                        odds_mod._normalize(ev.get("home_team", "")),
                                        odds_mod._normalize(ev.get("away_team", "")))
                                    odds_by_game[key] = odds
            except Exception:
                pass

        for fix in fixtures:
            state = fix.get("matchState", "")
            if state not in nrl_api.PRE_STATES:
                continue

            # Parse kickoff time — prefer clock.kickOffTimeLong over startTime
            start_iso = (fix.get("clock", {}).get("kickOffTimeLong", "")
                         or fix.get("startTime", ""))
            kickoff = None
            if start_iso:
                try:
                    kickoff = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                    if kickoff.tzinfo is None:
                        kickoff = kickoff.replace(tzinfo=timezone.utc)
                except Exception:
                    kickoff = None

            # Filter by upcoming window
            if kickoff and kickoff > deadline:
                continue

            home = fix.get("homeTeam", {})
            away = fix.get("awayTeam", {})
            home_name = home.get("nickName", "")
            away_name = away.get("nickName", "")
            home_odds = home.get("odds")
            away_odds = away.get("odds")

            # Format kickoff times
            kickoff_utc = kickoff.isoformat() if kickoff else None
            kickoff_aest = None
            kickoff_local_label = None
            if kickoff:
                try:
                    from zoneinfo import ZoneInfo
                    aest = kickoff.astimezone(ZoneInfo("Australia/Sydney"))
                    kickoff_aest = aest.strftime("%a %d %b %H:%M %Z")
                    kickoff_local_label = aest.strftime("%a %H:%M %Z")
                except Exception:
                    kickoff_aest = kickoff.strftime("%a %d %b %H:%M UTC")

            # Countdown
            countdown = None
            if kickoff and kickoff > now_utc:
                delta = kickoff - now_utc
                hours = int(delta.total_seconds() // 3600)
                mins = int((delta.total_seconds() % 3600) // 60)
                if hours > 0:
                    countdown = "{}h {}m".format(hours, mins)
                else:
                    countdown = "{}m".format(mins)

            # Ladder data
            home_ladder = ladder.get(home_name, {})
            away_ladder = ladder.get(away_name, {})

            # Odds API data (match by team names)
            bk_odds = None
            if odds_by_game:
                import odds_api as odds_mod
                home_lookup = odds_mod.NRL_TO_ODDS_API.get(home_name, home_name)
                away_lookup = odds_mod.NRL_TO_ODDS_API.get(away_name, away_name)
                for key, val in odds_by_game.items():
                    if (odds_mod._normalize(home_lookup) in key
                            and odds_mod._normalize(away_lookup) in key):
                        bk_odds = val
                        break

            game = {
                "match_id": fix.get("matchId"),
                "match_centre_url": fix.get("matchCentreUrl", ""),
                "round": rnd,
                "round_title": fix.get("roundTitle", ""),
                "home_team": home_name,
                "home_team_full": nrl_api.get_team_full_name(home_name),
                "home_abbrev": nrl_api.get_team_abbrev(home_name),
                "away_team": away_name,
                "away_team_full": nrl_api.get_team_full_name(away_name),
                "away_abbrev": nrl_api.get_team_abbrev(away_name),
                "venue": fix.get("venue", ""),
                "venue_city": fix.get("venueCity", ""),
                "home_odds": home_odds,
                "away_odds": away_odds,
                "kickoff_utc": kickoff_utc,
                "kickoff_aest": kickoff_aest,
                "kickoff_local_label": kickoff_local_label,
                "countdown": countdown,
                # Ladder data
                "home_record": home_ladder.get("record", ""),
                "away_record": away_ladder.get("record", ""),
                "home_position": home_ladder.get("position_label", ""),
                "away_position": away_ladder.get("position_label", ""),
                "home_streak": home_ladder.get("streak", ""),
                "away_streak": away_ladder.get("streak", ""),
                "home_form": home_ladder.get("form", ""),
                "away_form": away_ladder.get("form", ""),
                "home_home_record": home_ladder.get("home_record", ""),
                "away_away_record": away_ladder.get("away_record", ""),
                "home_pts_diff": home_ladder.get("pts_diff", 0),
                "away_pts_diff": away_ladder.get("pts_diff", 0),
            }

            # Merge Odds API spread/ML if available
            if bk_odds:
                game["bk_home_spread"] = bk_odds.get("home_spread")
                game["bk_away_spread"] = bk_odds.get("away_spread")
                game["bk_home_ml"] = bk_odds.get("home_ml")
                game["bk_away_ml"] = bk_odds.get("away_ml")
                game["bk_total"] = bk_odds.get("total_over")
                game["bk_bookmaker"] = bk_odds.get("bookmaker")
                game["spread_detail"] = odds_mod.format_spread_detail(
                    bk_odds, home_name, away_name)

            games.append(game)

        # Merge SOO upcoming fixtures (#103)
        try:
            soo_fixtures = nrl_api.get_soo_fixtures(season)
            for fix in soo_fixtures:
                state = fix.get("matchState", "")
                if state not in nrl_api.PRE_STATES:
                    continue
                start_iso = fix.get("startTime", "") or fix.get("clock", {}).get("kickOffTimeLong", "")
                kickoff = None
                if start_iso:
                    try:
                        kickoff = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                        if kickoff.tzinfo is None:
                            kickoff = kickoff.replace(tzinfo=timezone.utc)
                    except Exception:
                        kickoff = None
                if kickoff and kickoff > deadline:
                    continue
                home = fix.get("homeTeam", {})
                away = fix.get("awayTeam", {})
                home_name = home.get("nickName", "")
                away_name = away.get("nickName", "")
                kickoff_utc = kickoff.isoformat() if kickoff else None
                kickoff_aest = None
                countdown = None
                if kickoff:
                    try:
                        from zoneinfo import ZoneInfo
                        aest = kickoff.astimezone(ZoneInfo("Australia/Sydney"))
                        kickoff_aest = aest.strftime("%a %d %b %H:%M %Z")
                    except Exception:
                        kickoff_aest = kickoff.strftime("%a %d %b %H:%M UTC") if kickoff else None
                    if kickoff > now_utc:
                        delta = kickoff - now_utc
                        hours = int(delta.total_seconds() // 3600)
                        mins = int((delta.total_seconds() % 3600) // 60)
                        countdown = "{}h {}m".format(hours, mins) if hours > 0 else "{}m".format(mins)
                games.append({
                    "match_id": fix.get("matchId"),
                    "match_centre_url": fix.get("matchCentreUrl", ""),
                    "round": fix.get("roundNumber", 0),
                    "round_title": fix.get("roundTitle", ""),
                    "home_team": home_name,
                    "home_team_full": nrl_api.get_team_full_name(home_name),
                    "home_abbrev": nrl_api.get_team_abbrev(home_name),
                    "away_team": away_name,
                    "away_team_full": nrl_api.get_team_full_name(away_name),
                    "away_abbrev": nrl_api.get_team_abbrev(away_name),
                    "venue": fix.get("venue", ""),
                    "venue_city": fix.get("venueCity", ""),
                    "home_odds": home.get("odds"),
                    "away_odds": away.get("odds"),
                    "kickoff_utc": kickoff_utc,
                    "kickoff_aest": kickoff_aest,
                    "countdown": countdown,
                    "competition": "state_of_origin",
                    "soo_game": fix.get("_soo_game", 0),
                })
        except Exception:
            pass

        # Sort by kickoff time
        games.sort(key=lambda g: g.get("kickoff_utc") or "9999")

    except Exception as e:
        _log("upcoming fetch error:", e)

    # Derive round_title from fixtures for finals (#298)
    _round_title = None
    if fixtures:
        _round_title = next((f.get("roundTitle") for f in fixtures if f.get("roundTitle")), None)
    result = {"games": games, "round": rnd if fixtures else None, "round_title": _round_title, "window_hours": upcoming_hours}
    with _upcoming_lock:
        _upcoming_cache["payload"] = result
        _upcoming_cache["ts"] = time.time()
    return result




# ── History API helpers ───────────────────────────────────────────────────────

# In-memory history cache — avoids re-parsing game_history.json on every
# API request. Cache is invalidated when the file's mtime changes (#248).
_history_cache_lock = threading.Lock()
_history_cache = {"mtime": None, "records": None}


def _load_history():
    data = _read_json(HISTORY_FILE)
    return data if isinstance(data, list) else []


def _load_history_cached():
    """Return game history records, using an mtime-keyed in-memory cache."""
    try:
        mtime = os.path.getmtime(HISTORY_FILE)
    except OSError:
        return []
    with _history_cache_lock:
        if _history_cache["mtime"] == mtime and _history_cache["records"] is not None:
            return list(_history_cache["records"])
    records = _load_history()
    with _history_cache_lock:
        _history_cache["mtime"] = mtime
        _history_cache["records"] = records
    return records


def _param(params, key, default=""):
    vals = params.get(key, [])
    return vals[0] if vals else default


def _match_text(record, q):
    """Check if free-text query matches any searchable field."""
    q = q.lower()
    for field in ("home_team", "away_team", "venue", "venue_city", "round_title", "winner"):
        val = str(record.get(field, "") or "").lower()
        if q in val:
            return True
    return False


def build_history_payload(params):
    """Build filtered, paginated history list."""
    import pw_matrix
    records = _load_history()

    # Filters
    f_team = _param(params, "team", "").lower()
    f_condition = _param(params, "condition", "").lower()
    f_tag = _param(params, "tag", "").upper()
    f_season = _param(params, "season", "")
    f_segment = _param(params, "segment", "")
    f_soo_game = _param(params, "soo_game", "")
    f_source = _param(params, "source", "").lower()
    f_date_from = _param(params, "date_from", "")
    f_date_to = _param(params, "date_to", "")
    f_q = _param(params, "q", "").strip()
    f_outcome = _param(params, "outcome", "").upper()
    f_pw_band = _param(params, "pw_band", "")
    _pw_cons_raw = _param(params, "pw_consensus", "")
    f_pw_consensus = set(c.strip() for c in _pw_cons_raw.split(",") if c.strip()) if _pw_cons_raw else set()
    f_pw_half = _param(params, "pw_half", "")
    f_pw_quarter = _param(params, "pw_quarter", "")
    f_pw_margin = _param(params, "pw_margin", "")
    f_pw_spread = _param(params, "pw_spread_role", "")
    f_pw_call_sel = _param(params, "pw_call_selection", "")
    f_pw_edge = _param(params, "pw_edge", "")
    f_pw_version = _param(params, "pw_version", "")
    f_strict_bk = _param(params, "strict_bk", "") == "1"
    sort_order = _param(params, "sort", "date_desc")
    page = max(1, int(_param(params, "page", "1")))
    per_page = min(100, max(1, int(_param(params, "per_page", "25"))))

    filtered = []
    for r in records:
        if f_team:
            home = str(r.get("home_team", "") or "").lower()
            away = str(r.get("away_team", "") or "").lower()
            if f_team not in home and f_team not in away:
                continue

        if f_condition:
            conds = r.get("conditions_fired", [])
            # Support comma-separated conditions with same-team AND semantics (#140)
            _cond_parts = [p.strip() for p in f_condition.split(",") if p.strip()]
            if len(_cond_parts) > 1:
                # Group conditions by team, check if any team has ALL parts
                _by_team = {}
                for _cf in conds:
                    _t = _cf.get("team", "")
                    _cn = str(_cf.get("condition", "")).lower()
                    _by_team.setdefault(_t, set()).add(_cn)
                _matched = False
                for _t_conds in _by_team.values():
                    if all(any(cp in cn for cn in _t_conds) for cp in _cond_parts):
                        _matched = True
                        break
                if not _matched:
                    continue
            elif _cond_parts:
                if not any(_cond_parts[0] in str(c.get("condition", "")).lower() for c in conds):
                    continue

        if f_tag:
            tags = r.get("postgame_tags", [])
            if f_tag not in tags:
                continue

        if f_outcome:
            tags = r.get("postgame_tags", [])
            if f_outcome not in tags:
                continue

        if f_season:
            if str(r.get("season", "")) != f_season:
                continue

        if f_segment:
            if r.get("season_segment", "") != f_segment:
                continue

        if f_soo_game:
            if str(r.get("soo_game", "")) != f_soo_game:
                continue

        if f_source:
            if (r.get("source", "") or "").lower() != f_source:
                continue

        if f_date_from:
            ts = str(r.get("kickoff_utc") or r.get("ts") or "")
            if ts < f_date_from:
                continue

        if f_date_to:
            ts = str(r.get("kickoff_utc") or r.get("ts") or "")
            if ts > f_date_to + "T23:59:59":
                continue

        if f_q and not _match_text(r, f_q):
            continue

        # PW filters — match if ANY pw call in the game matches
        pw_calls = r.get("predicted_winner_calls", [])
        if f_pw_band or f_pw_consensus or f_pw_half or f_pw_quarter or f_pw_margin or f_pw_spread or f_pw_call_sel or f_pw_edge or f_pw_version or f_strict_bk:
            if not pw_calls:
                continue
            # Filter-then-select: match calls against filters first,
            # then apply call_selection to the matched set (#135).
            _pw_matched = []
            for c in pw_calls:
                # Skip suppressed and unresolved calls (match pw_matrix behavior #140)
                if c.get("_suppressed") or c.get("correct") is None:
                    continue
                # Live-priced filter (#258 Phase 4.1): skip backfill-priced calls
                if f_strict_bk and not c.get("bk_ml_source"):
                    continue
                pct = c.get("winner_score_pct") or c.get("pct", 0)
                if f_pw_band:
                    band = int(f_pw_band)
                    if pct < band or pct >= band + 10:
                        continue
                if f_pw_consensus and c.get("consensus", "") not in f_pw_consensus:
                    continue
                if f_pw_half and c.get("half", "") != f_pw_half:
                    continue
                if f_pw_quarter:
                    cq = c.get("quarter") or _quarter_from_minute(c.get("game_minute"))
                    if cq != f_pw_quarter:
                        continue
                if f_pw_margin:
                    smaf = c.get("score_margin_at_fire")
                    if smaf is None:
                        continue
                    if f_pw_margin == "leading" and smaf <= 0:
                        continue
                    if f_pw_margin == "trailing" and smaf > 0:
                        continue
                if f_pw_spread:
                    # Derive spread_role from game record when missing on call (#135)
                    _sr = c.get("spread_role", "")
                    if not _sr:
                        _pred = (c.get("predicted_team") or "").strip()
                        _hs = r.get("home_spread")
                        if _hs is not None and _pred:
                            try:
                                _pih = _pred == r.get("home_team", "")
                                _sr = "favorite" if (_pih and float(_hs) < 0) or (not _pih and float(_hs) > 0) else "underdog"
                            except (TypeError, ValueError):
                                pass
                    if _sr != f_pw_spread:
                        continue
                if f_pw_edge:
                    _ae = c.get("avg_edge")
                    if _ae is None:
                        continue
                    try:
                        _ae = float(_ae)
                    except (TypeError, ValueError):
                        continue
                    if f_pw_edge == "10+" and _ae < 10:
                        continue
                    if f_pw_edge == "5-10" and (_ae < 5 or _ae >= 10):
                        continue
                    if f_pw_edge == "0-5" and (_ae < 0 or _ae >= 5):
                        continue
                    if f_pw_edge == "neg" and _ae >= 0:
                        continue
                if f_pw_version and c.get("pw_version", "") != f_pw_version:
                    continue
                _pw_matched.append(c)
            # Apply call_selection to matched calls
            if f_pw_call_sel and _pw_matched:
                _sorted_m = sorted(_pw_matched, key=lambda x: x.get("ts", ""))
                if f_pw_call_sel == "first":
                    _pw_matched = [_sorted_m[0]]
                elif f_pw_call_sel == "last":
                    _pw_matched = [_sorted_m[-1]]
                elif f_pw_call_sel == "first_per_quarter":
                    _seen_q = {}
                    for _sc in _sorted_m:
                        _sq = _sc.get("quarter") or _quarter_from_minute(_sc.get("game_minute"))
                        if _sq not in _seen_q:
                            _seen_q[_sq] = _sc
                    _pw_matched = list(_seen_q.values())
                elif f_pw_call_sel == "first_per_half":
                    _seen_h = {}
                    for _sc in _sorted_m:
                        _sh = _sc.get("half", "")
                        if _sh not in _seen_h:
                            _seen_h[_sh] = _sc
                    _pw_matched = list(_seen_h.values())
            pw_match = len(_pw_matched) > 0
            if not pw_match:
                continue

        filtered.append(r)

    # Sort
    reverse = sort_order != "date_asc"
    filtered.sort(key=lambda r: r.get("kickoff_utc") or r.get("ts") or "", reverse=reverse)

    total = len(filtered)
    start = (page - 1) * per_page
    page_records = filtered[start:start + per_page]

    # Slim down records for list view (don't send full stats/try_events)
    _is_cond_drill = bool(f_condition)
    slim = []
    for r in page_records:
        rec = {
            "match_id": r.get("match_id"),
            "season": r.get("season"),
            "round": r.get("round"),
            "round_title": r.get("round_title"),
            "season_segment": r.get("season_segment"),
            "soo_game": r.get("soo_game"),
            "home_team": r.get("home_team"),
            "away_team": r.get("away_team"),
            "home_score": r.get("home_score"),
            "away_score": r.get("away_score"),
            "home_score_1h": r.get("home_score_1h"),
            "away_score_1h": r.get("away_score_1h"),
            "margin": r.get("margin"),
            "winner": r.get("winner"),
            "home_odds": r.get("home_odds"),
            "away_odds": r.get("away_odds"),
            "venue": r.get("venue"),
            "kickoff_utc": r.get("kickoff_utc"),
            "postgame_tags": r.get("postgame_tags", []),
            "conditions_count": len(r.get("conditions_fired", [])),
            "home_tries": r.get("home_tries"),
            "away_tries": r.get("away_tries"),
            "ts": r.get("ts"),
            "pw_call_count": r.get("pw_call_count", len(r.get("predicted_winner_calls", []))),
            "pw_correct_count": r.get("pw_correct_count", 0),
            "pw_accuracy_pct": r.get("pw_accuracy_pct", 0),
        }
        # PW drilldown: include per-game PW call summary (#138)
        _has_pw_filter = bool(f_pw_band or f_pw_consensus or f_pw_half or f_pw_quarter or f_pw_margin or f_pw_spread or f_pw_call_sel or f_pw_edge or f_pw_version)
        if _has_pw_filter:
            _pw_calls = [c for c in r.get("predicted_winner_calls", []) if isinstance(c, dict) and c.get("correct") is not None]
            _pw_total = len(_pw_calls)
            _pw_correct = sum(1 for c in _pw_calls if c.get("correct"))
            _flat_pnl = sum(100.0 if c.get("correct") else -100.0 for c in _pw_calls)
            _ml_pnl = 0.0
            _bk_odds_pnl = 0.0
            _bk_spr_pnl = 0.0
            _ml_n = 0
            _bk_odds_n = 0
            _bk_spr_n = 0
            for c in _pw_calls:
                _is_c = bool(c.get("correct"))
                _ml = pw_matrix._safe_float(c.get("moneyline"))
                if _ml is None:
                    _pred = (c.get("predicted_team") or "").strip()
                    _home = r.get("home_team", "")
                    _ml = pw_matrix._safe_float(r.get("home_ml") if _pred == _home else r.get("away_ml"))
                if _ml is not None and _ml > 1.0:
                    _ml_n += 1
                    _ml_pnl += pw_matrix._decimal_win_profit(_ml) if _is_c else -100.0
                _bk_ml = pw_matrix._safe_float(c.get("bk_moneyline"))
                if _bk_ml is not None and _bk_ml > 1.0:
                    _bk_odds_n += 1
                    _bk_odds_pnl += pw_matrix._decimal_win_profit(_bk_ml) if _is_c else -100.0
                _bk_spr = c.get("bk_spread")
                if _bk_spr is not None:
                    _bk_spr_n += 1
                    _covered = pw_matrix._did_cover_bk_line(r, c)
                    _bk_sp_price = pw_matrix._safe_float(c.get("bk_spread_price"))
                    _bk_sp_win = pw_matrix._decimal_win_profit(_bk_sp_price) if _bk_sp_price is not None and _bk_sp_price > 1.0 else 100.0
                    _bk_spr_pnl += _bk_sp_win if _covered else -100.0
            rec["pw_summary"] = {
                "total": _pw_total,
                "correct": _pw_correct,
                "pct": round(_pw_correct / _pw_total * 100, 1) if _pw_total > 0 else None,
                "flat_pnl": round(_flat_pnl, 2),
                "ml_pnl": round(_ml_pnl, 2) if _ml_n > 0 else None,
                "bk_odds_pnl": round(_bk_odds_pnl, 2) if _bk_odds_n > 0 else None,
                "bk_spr_pnl": round(_bk_spr_pnl, 2) if _bk_spr_n > 0 else None,
            }
        # Condition drilldown: include per-condition fire summary (#138)
        if _is_cond_drill:
            _winner = r.get("winner", "")
            _home_abbrev = nrl_api.get_team_abbrev(r.get("home_team", ""))
            _away_abbrev = nrl_api.get_team_abbrev(r.get("away_team", ""))
            rec["home_abbr"] = _home_abbrev
            rec["away_abbr"] = _away_abbrev
            _summary = []
            for cf in r.get("conditions_fired", []):
                _cn = cf.get("condition", "")
                _team = cf.get("team", "")
                _summary.append({
                    "name": _cn,
                    "team": _team,
                    "team_abbrev": cf.get("team_abbrev", ""),
                    "team_won": bool(_winner and _team == _winner),
                    "score_margin": cf.get("score_margin"),
                    "score": cf.get("score", ""),
                    "period": cf.get("period", ""),
                    "game_seconds_at_fire": cf.get("game_seconds_at_fire"),
                })
            rec["conditions_fired_summary"] = _summary
        slim.append(rec)

    # Drilldown stats (#138)
    drilldown_stats = None
    _has_cond_filter = bool(f_condition)
    _has_pw_filter2 = bool(f_pw_band or f_pw_consensus or f_pw_half or f_pw_quarter or f_pw_margin or f_pw_spread or f_pw_call_sel or f_pw_edge or f_pw_version)
    if _has_cond_filter and not _has_pw_filter2:
        _cond_parts = [p.strip() for p in f_condition.split(",") if p.strip()] if "," in f_condition else [f_condition]
        wins = 0
        pos_margin = 0
        margin_diffs = []
        per_condition = {}
        for rec in filtered:
            conds = rec.get("conditions_fired", [])
            winner = rec.get("winner", "")
            for cp in _cond_parts:
                matching = [c for c in conds if cp in str(c.get("condition", "")).lower()]
                for c in matching:
                    team = c.get("team", "")
                    team_won = bool(winner and team == winner)
                    margin = c.get("score_margin")
                    pc_key = c.get("condition", cp)
                    if pc_key not in per_condition:
                        per_condition[pc_key] = {"total": 0, "wins": 0, "margins": []}
                    per_condition[pc_key]["total"] += 1
                    if team_won:
                        per_condition[pc_key]["wins"] += 1
                        wins += 1
                    if margin is not None:
                        final_margin = rec.get("margin", 0)
                        diff = (final_margin if team_won else -final_margin) - margin if final_margin else None
                        if diff is not None:
                            margin_diffs.append(diff)
                            per_condition[pc_key]["margins"].append(diff)
                            if diff > 0:
                                pos_margin += 1
                    break  # one match per condition part per game
        pc_out = {}
        for cn, pc in per_condition.items():
            pc_out[cn] = {
                "total_events": pc["total"],
                "teams_won_events": pc["wins"],
                "teams_won_pct": round(pc["wins"] / pc["total"] * 100, 1) if pc["total"] > 0 else 0,
                "avg_margin_diff": round(sum(pc["margins"]) / len(pc["margins"]), 1) if pc["margins"] else None,
            }
        drilldown_stats = {
            "total_games": total,
            "teams_won_games": wins,
            "teams_won_pct": round(wins / total * 100, 1) if total > 0 else 0,
            "positive_margin_diff_games": pos_margin,
            "positive_margin_diff_pct": round(pos_margin / total * 100, 1) if total > 0 else 0,
            "avg_margin_diff": round(sum(margin_diffs) / len(margin_diffs), 1) if margin_diffs else None,
            "per_condition": pc_out,
        }
    elif _has_pw_filter2:
        pw_total = 0
        pw_correct = 0
        pw_games_won = 0
        for rec in filtered:
            calls = [c for c in (rec.get("predicted_winner_calls") or []) if isinstance(c, dict) and c.get("correct") is not None]
            matching = [c for c in calls if not c.get("_suppressed")]
            if not matching:
                continue
            correct_in = sum(1 for c in matching if c.get("correct"))
            pw_total += len(matching)
            pw_correct += correct_in
            if correct_in > 0:
                pw_games_won += 1
        drilldown_stats = {
            "is_pw_drilldown": True,
            "total_games": total,
            "teams_won_games": pw_games_won,
            "teams_won_pct": round(pw_games_won / total * 100, 1) if total > 0 else 0,
            "pw_total_calls": pw_total,
            "pw_correct_calls": pw_correct,
            "pw_accuracy_pct": round(pw_correct / pw_total * 100, 1) if pw_total > 0 else 0,
        }

    return {
        "records": slim,
        "total": total,
        "page": page,
        "per_page": per_page,
        "drilldown_stats": drilldown_stats,
    }


def build_history_game_payload(game_id):
    """Return full detail for a single game."""
    records = _load_history()
    for r in records:
        if r.get("match_id") == game_id:
            return r
    return None


def build_history_facets_payload():
    """Return facet counts for filter dropdowns."""
    records = _load_history()
    from collections import Counter

    teams = Counter()
    conditions = Counter()
    tags = Counter()
    seasons = Counter()
    segments = Counter()

    for r in records:
        for team in (r.get("home_team", ""), r.get("away_team", "")):
            if team:
                teams[team] += 1
        for c in r.get("conditions_fired", []):
            name = c.get("condition", "")
            if name:
                conditions[name] += 1
        for t in r.get("postgame_tags", []):
            tags[t] += 1
        s = r.get("season")
        if s:
            seasons[str(s)] += 1
        seg = r.get("season_segment")
        if seg:
            segments[seg] += 1

    return {
        "teams": [{"name": k, "count": v} for k, v in teams.most_common(20)],
        "conditions": [{"name": k, "count": v} for k, v in conditions.most_common(20)],
        "tags": [{"name": k, "count": v} for k, v in tags.most_common(10)],
        "seasons": [{"name": k, "count": v} for k, v in sorted(seasons.items())],
        "segments": [{"name": k, "count": v} for k, v in segments.most_common()],
        "total_games": len(records),
    }



# ── ML Training helpers (#44) ─────────────────────────────────────────────────

import hashlib
import subprocess
import threading
import re as _re

ML_TRAINING_HISTORY_FILE = os.path.join(SCRIPT_DIR, "ml_training_history.json")
ML_TRAIN_LOCK_FILE = os.path.join(SCRIPT_DIR, "ml_train.lock")
_ml_train_lock = threading.Lock()
_ml_train_state = {"status": "idle", "started_at": None, "finished_at": None, "duration_s": None, "error": None}


def _game_history_hash():
    """SHA256 of game_history.json for staleness detection."""
    path = os.path.join(SCRIPT_DIR, "game_history.json")
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except Exception:
        return None


def _get_ml_training_status():
    """Build ML training status payload."""
    meta_path = os.path.join(SCRIPT_DIR, "model_meta_nrl.json")
    meta = _read_json(meta_path) or {}
    current_hash = _game_history_hash()
    stored_hash = meta.get("history_hash")
    config = _read_json(CONFIG_FILE) or {}

    history = []
    try:
        with open(ML_TRAINING_HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)
        if not isinstance(history, list):
            history = []
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    # Compute next scheduled training
    next_scheduled = None
    trained_at = meta.get("trained_at")
    interval_days = config.get("ml_training_interval", 7)
    schedule_utc = config.get("ml_training_schedule", "09:15")
    if config.get("ml_training_enabled") and trained_at:
        try:
            last_dt = datetime.fromisoformat(trained_at.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_day = last_dt + timedelta(days=interval_days)
            # Apply schedule time
            hh, mm = (schedule_utc or "09:15").split(":")
            next_dt = next_day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            next_scheduled = next_dt.isoformat()
        except Exception:
            pass

    return {
        "model_meta": {
            "trained_at": trained_at,
            "history_hash": stored_hash,
            "training_games": meta.get("training_games", 0),
            "cv_accuracy": meta.get("cv_accuracy"),
            "schema_version": meta.get("schema_version"),
        },
        "current_hash": current_hash,
        "is_stale": current_hash != stored_hash if (current_hash and stored_hash) else True,
        "config": {
            "enabled": config.get("ml_training_enabled", False),
            "interval_days": interval_days,
            "schedule_utc": schedule_utc,
            "retention_count": config.get("ml_training_retention", 10),
        },
        "next_scheduled_at": next_scheduled,
        "training_in_progress": _ml_train_state["status"] == "running",
        "train_state": dict(_ml_train_state),
        "history": history[-20:],
    }


def _do_ml_train_background():
    """Run ml_model.py --train in background thread."""
    import time as _time
    t0 = _time.time()
    _ml_train_state["status"] = "running"
    _ml_train_state["started_at"] = datetime.now(timezone.utc).isoformat()
    _ml_train_state["finished_at"] = None
    _ml_train_state["error"] = None

    log_name = "ml_train_{}.log".format(datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    log_path = os.path.join(SCRIPT_DIR, "logs", log_name)

    try:
        result = subprocess.run(
            [sys.executable, os.path.join(SCRIPT_DIR, "ml_model.py"), "--train", "--evaluate"],
            capture_output=True, text=True, timeout=300, cwd=SCRIPT_DIR)

        # Write log
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            f.write(result.stdout or "")
            if result.stderr:
                f.write("\n--- STDERR ---\n")
                f.write(result.stderr)

        duration = round(_time.time() - t0, 1)
        _ml_train_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _ml_train_state["duration_s"] = duration

        # Parse samples and CV accuracy from output (check both stdout and stderr)
        import re as _re2
        _all_output = (result.stdout or "") + "\n" + (result.stderr or "")
        _parsed_samples = 0
        _parsed_cv = None
        for line in _all_output.split("\n"):
            m = _re2.search(r'(\d+)\s*samples', line)
            if m and not _parsed_samples:
                _parsed_samples = int(m.group(1))
            m = _re2.search(r'CV accuracy:\s*([\d.]+)%', line)
            if m:
                _parsed_cv = float(m.group(1))

        # Determine result: check exit code AND actual training output
        _has_error = "ERROR" in _all_output.upper()
        _train_ok = result.returncode == 0 and _parsed_cv is not None and not _has_error

        if _train_ok:
            _ml_train_state["status"] = "done"
            # Update history hash in model meta
            meta_path = os.path.join(SCRIPT_DIR, "model_meta_nrl.json")
            meta = _read_json(meta_path) or {}
            meta["history_hash"] = _game_history_hash()
            tmp = meta_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(meta, f, indent=2)
            os.replace(tmp, meta_path)
        else:
            _ml_train_state["status"] = "error"
            _ml_train_state["error"] = "Training failed" + (" (exit {})".format(result.returncode) if result.returncode else "")

        # Append to history
        entry = {
            "timestamp": _ml_train_state["started_at"],
            "trigger": "manual",
            "result": "trained" if _train_ok else "error",
            "total_samples": _parsed_samples,
            "cv_accuracy": _parsed_cv,
            "duration_seconds": duration,
            "log_file": log_name,
        }

        history = []
        try:
            with open(ML_TRAINING_HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        if not isinstance(history, list):
            history = []
        history.append(entry)

        # Retention — separate limits for trained/error vs skipped entries
        config = _read_json(CONFIG_FILE) or {}
        retention = config.get("ml_training_retention", 10)
        trained_entries = [e for e in history if not (e.get("result") or "").startswith("skipped")]
        skipped_entries = [e for e in history if (e.get("result") or "").startswith("skipped")]
        if len(trained_entries) > retention:
            trained_entries = trained_entries[-retention:]
        if len(skipped_entries) > retention:
            skipped_entries = skipped_entries[-retention:]
        history = sorted(trained_entries + skipped_entries, key=lambda e: e.get("timestamp") or "")

        tmp = ML_TRAINING_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, ML_TRAINING_HISTORY_FILE)

    except Exception as e:
        _ml_train_state["status"] = "error"
        _ml_train_state["finished_at"] = datetime.now(timezone.utc).isoformat()
        _ml_train_state["duration_s"] = round(_time.time() - t0, 1)
        _ml_train_state["error"] = str(e)


# ── Monitor Status helpers ────────────────────────────────────────────────────

LOG_FILE = os.path.join(SCRIPT_DIR, "logs", "monitor.log")
LOG_FILES = {
    "monitor.log": os.path.join(SCRIPT_DIR, "logs", "monitor.log"),
    "dashboard.log": os.path.join(SCRIPT_DIR, "logs", "dashboard.log"),
    "cloud_backup.log": os.path.join(SCRIPT_DIR, "logs", "cloud_backup.log"),
}
LOG_ROTATION_STATE_FILE = os.path.join(SCRIPT_DIR, ".log_rotation_state.json")
LOG_ROTATED_SUFFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$|^\d{1,2}$")


def _rotate_logs(config):
    """Rotate configured log files and enforce retention.

    Ported from NBA Monitor's monitor.py to run from the dashboard server,
    which runs 24/7.  The monitor process only runs during game windows,
    so rotation configured for hours outside the window would never fire.
    """
    import shutil

    lr = (config or {}).get("log_rotation", {})
    if not isinstance(lr, dict):
        lr = {}
    if not lr.get("enabled"):
        return

    now_utc = datetime.now(timezone.utc)
    try:
        rotation_hour = int(lr.get("rotation_hour_utc", 14))
    except (TypeError, ValueError):
        rotation_hour = 14
    if now_utc.hour != rotation_hour:
        return

    today_utc = now_utc.date()

    default_files = ["logs/monitor.log", "logs/dashboard.log"]
    files = lr.get("files", default_files)
    if not isinstance(files, list) or not files:
        files = default_files

    try:
        retention_days = max(1, int(lr.get("retention_days", 10)))
    except (TypeError, ValueError):
        retention_days = 10

    # Load rotation state
    state = {}
    try:
        with open(LOG_ROTATION_STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
            if not isinstance(state, dict):
                state = {}
    except Exception:
        pass

    state_dirty = False
    for rel_path in files:
        log_path = rel_path if os.path.isabs(rel_path) else os.path.join(SCRIPT_DIR, rel_path)
        if not os.path.isfile(log_path):
            continue

        state_key = os.path.abspath(log_path).replace("\\", "/")
        entry = state.get(state_key, {})
        if not isinstance(entry, dict):
            entry = {}

        last_rotated = entry.get("last_rotated")
        if last_rotated:
            try:
                last_date = datetime.strptime(last_rotated, "%Y-%m-%d").date()
                if (today_utc - last_date).days < 1:
                    continue
            except ValueError:
                pass

        try:
            mtime_date = datetime.fromtimestamp(os.path.getmtime(log_path), timezone.utc).date()
        except OSError:
            continue

        rotated_path = "{}.{}".format(log_path, today_utc.isoformat())
        try:
            mode = "ab" if os.path.exists(rotated_path) else "wb"
            with open(log_path, "rb") as src, open(rotated_path, mode) as dst:
                shutil.copyfileobj(src, dst)
            open(log_path, "w").close()
            _log("[INFO] Rotated log {} -> {}".format(log_path, rotated_path))
            state[state_key] = {"last_rotated": today_utc.isoformat()}
            state_dirty = True
        except Exception as e:
            _log("[WARN] Log rotation failed for {}: {}".format(log_path, e))

        # Remove rotated files older than retention_days
        base_name = os.path.basename(log_path) + "."
        parent = os.path.dirname(log_path)
        try:
            for fname in os.listdir(parent):
                if not fname.startswith(base_name):
                    continue
                suffix = fname[len(base_name):]
                try:
                    rotated_date = datetime.strptime(suffix, "%Y-%m-%d").date()
                except ValueError:
                    continue
                if (today_utc - rotated_date).days > retention_days:
                    try:
                        os.remove(os.path.join(parent, fname))
                        _log("[INFO] Removed old rotated log {}".format(fname))
                    except OSError as e:
                        _log("[WARN] Failed to remove old rotated log {}: {}".format(fname, e))
        except OSError:
            pass

    if state_dirty:
        try:
            tmp = LOG_ROTATION_STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f)
            os.replace(tmp, LOG_ROTATION_STATE_FILE)
        except Exception as e:
            _log("[WARN] Failed to save log rotation state: {}".format(e))


def _log_rotation_worker():
    """Background worker that runs log rotation every 5 minutes."""
    while True:
        try:
            cfg = _read_json(CONFIG_FILE) or {}
            _rotate_logs(cfg)
        except Exception as exc:
            _log("[LOG ROTATION] worker error: {}".format(exc))
        time.sleep(300)


def _get_odds_api_credits(config=None):
    """Get combined log + live credit status for Odds API."""
    try:
        import odds_api as odds_mod
        log_status = odds_mod.get_log_credit_status()
        api_key = (config or {}).get("odds_api_key", "")
        live_status = odds_mod.query_live_credits(api_key) if api_key else {}
        in_memory = odds_mod.get_credit_status()
        return {
            "log_remaining": log_status.get("credits_remaining"),
            "log_ts": log_status.get("ts"),
            "log_type": log_status.get("type"),
            "live_remaining": live_status.get("credits_remaining"),
            "live_ts": live_status.get("ts"),
            "live_error": live_status.get("error"),
            "session_calls": in_memory.get("api_calls", 0),
            "session_used": in_memory.get("credits_used", 0),
            "session_errors": in_memory.get("errors", 0),
            "last_call_ts": in_memory.get("last_call_ts"),
        }
    except Exception:
        return {}


def _parse_log_entry_ts(value):
    """Parse a log entry timestamp string to UTC datetime, or None."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_log_date_param(value, end_of_day=False):
    """Parse YYYY-MM-DD query params into UTC datetimes."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
    return d.replace(tzinfo=timezone.utc)


def _discover_log_files(logical_name=""):
    """Return current + rotated log files for one logical stream or all."""
    logical_name = str(logical_name or "").strip()
    items = []
    logs_dir = os.path.join(SCRIPT_DIR, "logs")
    for base_name, base_path in LOG_FILES.items():
        if logical_name and base_name != logical_name:
            continue
        if os.path.isfile(base_path):
            try:
                st = os.stat(base_path)
                items.append({
                    "name": base_name,
                    "logical_name": base_name,
                    "path": base_path,
                    "is_active": True,
                    "date": None,
                    "mtime": st.st_mtime,
                    "size": st.st_size,
                })
            except OSError:
                pass
        prefix = base_name + "."
        if not os.path.isdir(logs_dir):
            continue
        try:
            for fname in os.listdir(logs_dir):
                if not fname.startswith(prefix):
                    continue
                suffix = fname[len(prefix):]
                if not LOG_ROTATED_SUFFIX_RE.match(suffix):
                    continue
                fpath = os.path.join(logs_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    st = os.stat(fpath)
                    items.append({
                        "name": fname,
                        "logical_name": base_name,
                        "path": fpath,
                        "is_active": False,
                        "date": suffix,
                        "mtime": st.st_mtime,
                        "size": st.st_size,
                    })
                except OSError:
                    continue
        except OSError:
            continue
    items.sort(key=lambda it: (it["logical_name"], 0 if it["is_active"] else 1, it.get("date") or "", it["name"]), reverse=True)
    return items


def _resolve_log_file(request_name):
    """Resolve an allowed active/rotated log filename to an absolute path."""
    req = str(request_name or "").strip()
    if not req:
        return None
    for item in _discover_log_files():
        if item["name"] == req:
            return item["path"]
    return None


def build_log_files_payload():
    """List active and rotated log files available to the Logs tab."""
    items = _discover_log_files()
    logical = sorted(LOG_FILES.keys())
    return {
        "logical_files": logical,
        "files": [{
            "name": it["name"],
            "logical_name": it["logical_name"],
            "is_active": it["is_active"],
            "date": it["date"],
            "size": it["size"],
        } for it in items],
    }


def build_monitor_status_payload():
    """Build monitor status overview."""
    config = _read_json(CONFIG_FILE) or {}
    live_stats = _read_json(LIVE_STATS_FILE) or {}

    # Disk space
    import shutil
    disk = shutil.disk_usage(SCRIPT_DIR)
    disk_free_mb = disk.free // (1024 * 1024)
    disk_total_mb = disk.total // (1024 * 1024)
    disk_status = "OK" if disk_free_mb > 1024 else ("Low" if disk_free_mb > 256 else "Critical")

    # Live game counts from last stats write
    games = live_stats.get("games", [])
    live_count = len(games)
    last_run = live_stats.get("ts", "")

    # Game history file size
    history_path = os.path.join(SCRIPT_DIR, "game_history.json")
    history_records = _load_history()
    history_size_mb = 0
    try:
        history_size_mb = round(os.path.getsize(history_path) / (1024 * 1024), 1)
    except OSError:
        pass

    # Model status
    model_meta = _read_json(os.path.join(SCRIPT_DIR, "model_meta_nrl.json")) or {}
    model_trained = bool(model_meta.get("trained_at"))
    model_stale = False
    if model_trained:
        current_hash = _game_history_hash()
        stored_hash = model_meta.get("history_hash")
        model_stale = current_hash != stored_hash if (current_hash and stored_hash) else True

    # Last backup
    import runtime_backup
    last_backup = None
    try:
        backup_root = runtime_backup.DEFAULT_BACKUP_ROOT
        if os.path.isdir(backup_root):
            dirs = sorted([d for d in os.listdir(backup_root) if os.path.isdir(os.path.join(backup_root, d)) and not d.startswith("failed-run-")], reverse=True)
            if dirs:
                last_backup = dirs[0]
    except Exception:
        pass

    # Scheduler config
    sched_start = config.get("scheduler_start_hour", 4)
    sched_stop = config.get("scheduler_stop_hour", 13)
    sched_interval = config.get("scheduler_interval_min", 1)

    # Games today vs upcoming (#115)
    games_today = []
    games_upcoming = []
    try:
        from zoneinfo import ZoneInfo
        _aest = ZoneInfo("Australia/Sydney")
        _today_aest = datetime.now(_aest).strftime("%Y-%m-%d")
        _today_label = datetime.now(_aest).strftime("%a %d %b %Y AEST")
        upcoming = build_upcoming_payload(config)
        for g in upcoming.get("games", []):
            ko = g.get("kickoff_utc", "")
            _game_entry = {
                "home_team": g.get("home_team", ""),
                "away_team": g.get("away_team", ""),
                "kickoff_utc": ko,
                "kickoff_aest": g.get("kickoff_aest", ""),
                "competition": g.get("competition", ""),
                "round_title": g.get("round_title", ""),
            }
            # Check if game is today in AEST
            _is_today = False
            if ko:
                try:
                    _ko_dt = datetime.fromisoformat(ko.replace("Z", "+00:00"))
                    _ko_aest = _ko_dt.astimezone(_aest).strftime("%Y-%m-%d")
                    _is_today = _ko_aest == _today_aest
                except Exception:
                    pass
            if _is_today:
                games_today.append(_game_entry)
            else:
                games_upcoming.append(_game_entry)
    except Exception:
        _today_label = ""

    return {
        "season": config.get("season", 2026),
        "monitor_all_teams": config.get("monitor_all_teams", True),
        "teams": config.get("teams", []),
        "total_teams": 17,
        "alert_cooldown_minutes": config.get("alert_cooldown_minutes", 5),
        "slack_channel_name": config.get("slack_channel_name", ""),
        "slack_channel": config.get("slack_channel", ""),
        "end_of_game_alerts": config.get("end_of_game_alerts", True),
        "live_games": live_count,
        "games_today": games_today,
        "games_today_label": _today_label if games_today else "",
        "games_upcoming": games_upcoming,
        "upcoming_hours": int(config.get("upcoming_hours", 168)),
        "round": live_stats.get("round"),
        "last_run": last_run,
        "conditions_count": len(config.get("conditions", [])),
        "compound_count": len(config.get("compound_conditions", [])),
        "disk_free_mb": disk_free_mb,
        "disk_total_mb": disk_total_mb,
        "disk_pct_free": round(disk_free_mb / disk_total_mb * 100, 1) if disk_total_mb else 0,
        "disk_status": disk_status,
        "history_count": len(history_records),
        "history_size_mb": history_size_mb,
        "odds_api_enabled": config.get("odds_api_enabled", False),
        "odds_api_credits": _get_odds_api_credits(config),
        "model_trained": model_trained,
        "model_trained_at": model_meta.get("trained_at"),
        "model_stale": model_stale,
        "last_backup": last_backup,
        "scheduler": {"start": sched_start, "stop": sched_stop, "interval": sched_interval},
        "cloud_backup": _get_cloud_backup_status(),
    }


# Cloud backup subprocess guard (#250)
_cloud_backup_proc = None
def _set_cloud_backup_proc(proc):
    global _cloud_backup_proc
    _cloud_backup_proc = proc

def _get_cloud_backup_status():
    """Parse cloud_backup.log for last run status and query timer for next scheduled (#249)."""
    import re, subprocess
    log_path = os.path.join(SCRIPT_DIR, "logs", "cloud_backup.log")
    result = {"available": False}
    if _cloud_backup_proc and _cloud_backup_proc.poll() is None:
        result["running"] = True

    if os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", errors="replace")

            lines = tail.strip().split("\n")
            start_idx = None
            for i in range(len(lines) - 1, -1, -1):
                if "cloud backup started ===" in lines[i]:
                    start_idx = i
                    break

            if start_idx is not None:
                result["available"] = True
                run_lines = lines[start_idx:]
                ts_re = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)\]")

                start_match = ts_re.search(run_lines[0])
                if start_match:
                    result["started"] = start_match.group(1)

                for line in run_lines:
                    if "completed successfully ===" in line:
                        result["status"] = "success"
                        m = ts_re.search(line)
                        if m:
                            result["last_run"] = m.group(1)
                    elif "completed with errors" in line:
                        result["status"] = "errors"
                        m = ts_re.search(line)
                        if m:
                            result["last_run"] = m.group(1)

                if result.get("started") and result.get("last_run"):
                    try:
                        _s = datetime.strptime(result["started"], "%Y-%m-%d %H:%M:%S UTC")
                        _e = datetime.strptime(result["last_run"], "%Y-%m-%d %H:%M:%S UTC")
                        result["duration_seconds"] = int((_e - _s).total_seconds())
                    except Exception:
                        pass

                for line in run_lines:
                    m = re.search(r"(\d+(?:\.\d+)?\s+\w+iB)\s*/\s*\d+(?:\.\d+)?\s+\w+iB,\s*100%", line)
                    if m:
                        result["size_synced"] = m.group(1).strip()

                errors = []
                for line in run_lines:
                    if "<3>ERROR" in line:
                        em = re.search(r"<3>ERROR\s*:\s*(.+)", line)
                        if em:
                            msg = em.group(1).strip()
                            short = re.sub(r'(Failed to copy|error reading destination directory):.*', lambda m: m.group(1), msg)
                            if "size changed" in msg:
                                sm = re.search(r'size changed from \d+ to \d+', msg)
                                if sm:
                                    short = short + ": " + sm.group(0)
                            elif "i/o timeout" in msg:
                                short = short + ": i/o timeout"
                            if "Attempt " in msg:
                                continue
                            if "not deleting" in msg:
                                continue
                            errors.append(short)
                result["error_count"] = len(errors)
                if errors:
                    result["errors"] = errors[:5]
        except Exception:
            pass

    try:
        proc = subprocess.run(
            ["systemctl", "--user", "show", "nrl-cloud-backup.timer",
             "--property=NextElapseUSecRealtime", "--no-pager"],
            capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            for _line in proc.stdout.split("\n"):
                if _line.startswith("NextElapseUSecRealtime="):
                    _val = _line.split("=", 1)[1].strip()
                    if _val and _val != "n/a":
                        result["next_scheduled"] = _val
    except Exception:
        pass

    # Stale check (#250 Phase 4)
    if result.get("last_run"):
        try:
            _last = datetime.strptime(result["last_run"], "%Y-%m-%d %H:%M:%S UTC")
            _last = _last.replace(tzinfo=None)
            _age_h = (datetime.utcnow() - _last).total_seconds() / 3600
            result["age_hours"] = round(_age_h, 1)
            if _age_h > 36:
                result["stale"] = True
        except Exception:
            pass

    return result


def _get_cloud_backup_history(last_n=20):
    """Parse cloud_backup.log for all runs (#250 Phase 3)."""
    import re
    log_path = os.path.join(SCRIPT_DIR, "logs", "cloud_backup.log")
    runs = []
    if not os.path.exists(log_path):
        return runs
    try:
        with open(log_path, "r", errors="replace") as f:
            lines = f.readlines()
    except Exception:
        return runs

    ts_re = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)\]")
    starts = [i for i, line in enumerate(lines) if "cloud backup started ===" in line]
    for si, start_idx in enumerate(starts):
        end_idx = starts[si + 1] if si + 1 < len(starts) else len(lines)
        run_lines = lines[start_idx:end_idx]
        run = {}
        m = ts_re.search(run_lines[0])
        if m:
            run["started"] = m.group(1)
        for line in run_lines:
            if "completed successfully ===" in line:
                run["status"] = "success"
                m2 = ts_re.search(line)
                if m2:
                    run["completed"] = m2.group(1)
            elif "completed with errors" in line:
                run["status"] = "errors"
                m2 = ts_re.search(line)
                if m2:
                    run["completed"] = m2.group(1)
        if run.get("started") and run.get("completed"):
            try:
                _s = datetime.strptime(run["started"], "%Y-%m-%d %H:%M:%S UTC")
                _e = datetime.strptime(run["completed"], "%Y-%m-%d %H:%M:%S UTC")
                run["duration_seconds"] = int((_e - _s).total_seconds())
            except Exception:
                pass
        for line in run_lines:
            m3 = re.search(r"(\d+(?:\.\d+)?\s+\w+iB)\s*/\s*\d+(?:\.\d+)?\s+\w+iB,\s*100%", line)
            if m3:
                run["size_synced"] = m3.group(1).strip()
        errs = sum(1 for line in run_lines if "<3>ERROR" in line
                   and "Attempt " not in line and "not deleting" not in line)
        run["error_count"] = errs
        if run.get("started"):
            runs.append(run)
    runs.reverse()
    return runs[:last_n]


def build_log_events_payload(limit=500, logical_name="", date_from="", date_to=""):
    """Read recent lines from configured log files and parse into structured events."""
    import re
    lines = []
    total_lines = 0
    dt_from = _parse_log_date_param(date_from, end_of_day=False)
    dt_to = _parse_log_date_param(date_to, end_of_day=True)
    for item in _discover_log_files(logical_name=logical_name):
        source_name = item["name"]
        source_path = item["path"]
        if not os.path.isfile(source_path):
            continue
        try:
            with open(source_path, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 131072))  # last 128KB per file
                raw = f.read().decode("utf-8", errors="replace")
                source_lines = [ln for ln in raw.strip().split("\n") if ln.strip()]
                total_lines += len(source_lines)
                lines.extend((source_name, ln) for ln in source_lines)
        except Exception:
            continue

    # Parse lines into structured events
    ts_re = re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?)\s+(.*)')
    cb_ts_re = re.compile(r'^\[(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}:\d{2}) UTC\]\s*(.*)')
    events = []
    for source_name, line in lines:
        m = ts_re.match(line)
        if m:
            ts = m.group(1)
            msg = m.group(2)
        else:
            m2 = cb_ts_re.match(line)
            if m2:
                ts = "{}T{}Z".format(m2.group(1), m2.group(2))
                msg = m2.group(3)
            else:
                ts = ""
                msg = line
        ts_dt = _parse_log_entry_ts(ts)
        if dt_from and ts_dt and ts_dt < dt_from:
            continue
        if dt_to and ts_dt and ts_dt > dt_to:
            continue

        # Classify tag from message content
        msg_upper = msg.upper()
        if "ERROR" in msg_upper or "CRITICAL" in msg_upper or "TRACEBACK" in msg_upper:
            tag = "ERROR"
        elif "WARN" in msg_upper:
            tag = "WARN"
        elif "ALERT" in msg_upper or "SLACK" in msg_upper:
            tag = "ALERT"
        elif "PW " in msg_upper or "PREDICTED WINNER" in msg_upper:
            tag = "PW"
        elif "ODDS" in msg_upper:
            tag = "ODDS"
        elif "AUTO-RECOVERY" in msg_upper or "RECOVERED" in msg_upper:
            tag = "RECOVERY"
        elif "BACKFILL" in msg_upper or "HISTORY" in msg_upper:
            tag = "HISTORY"
        elif source_name.startswith("cloud_backup") or "CLOUD BACKUP" in msg_upper:
            tag = "CLOUD"
        elif "BACKUP" in msg_upper or "SNAPSHOT" in msg_upper:
            tag = "BACKUP"
        else:
            tag = "INFO"

        events.append({"ts": ts, "tag": tag, "message": msg, "source": source_name})

    # Return newest first, capped. ISO timestamps sort lexicographically.
    events.sort(key=lambda ev: (ev.get("ts") or "", ev.get("source") or "", ev.get("message") or ""), reverse=True)
    events = events[:limit]
    return {"events": events, "count": len(events), "total_lines": total_lines}


def build_postgame_payload(config):
    """Return recently completed games for post-game summary."""
    hours = max(1, min(168, int((config or {}).get("postgame_summary_hours", 24))))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    cutoff_iso = cutoff.isoformat()

    records = _load_history()
    recent = []
    for r in reversed(records):
        ts = r.get("kickoff_utc") or r.get("ts") or ""
        if ts < cutoff_iso:
            break
        recent.append({
            "match_id": r.get("match_id"),
            "season": r.get("season"),
            "round": r.get("round"),
            "round_title": r.get("round_title"),
            "home_team": r.get("home_team"),
            "away_team": r.get("away_team"),
            "home_score": r.get("home_score"),
            "away_score": r.get("away_score"),
            "home_score_1h": r.get("home_score_1h"),
            "away_score_1h": r.get("away_score_1h"),
            "margin": r.get("margin"),
            "winner": r.get("winner"),
            "postgame_tags": r.get("postgame_tags", []),
            "season_segment": r.get("season_segment"),
            "round_title": r.get("round_title"),
            "conditions_count": len(r.get("conditions_fired", [])),
            "home_tries": r.get("home_tries"),
            "away_tries": r.get("away_tries"),
            "venue": r.get("venue"),
            "kickoff_utc": r.get("kickoff_utc"),
        })
    return {"games": recent, "window_hours": hours}


def _filter_records(records, params):
    """Apply shared filters to game history records.

    Supported filters: season, segment, date_from, date_to, tag, role.
    """
    f_season = _param(params, "season", "")
    f_segment = _param(params, "segment", "")
    f_soo_game = _param(params, "soo_game", "")
    f_date_from = _param(params, "date_from", "")
    f_date_to = _param(params, "date_to", "")
    f_tag = _param(params, "tag", "").upper()
    f_role = _param(params, "role", "").lower()

    filtered = []
    for r in records:
        if f_season and str(r.get("season", "")) != f_season:
            continue
        if f_segment and r.get("season_segment", "") != f_segment:
            continue
        if f_soo_game and str(r.get("soo_game", "")) != f_soo_game:
            continue
        ts = str(r.get("kickoff_utc") or r.get("ts") or "")
        if f_date_from and ts < f_date_from:
            continue
        if f_date_to and ts > f_date_to + "T23:59:59":
            continue
        if f_tag:
            tags = r.get("postgame_tags", [])
            # "WIN" is a special pseudo-tag: game had a winner (not a draw)
            if f_tag == "WIN" and not r.get("winner"):
                continue
            elif f_tag != "WIN" and f_tag not in tags:
                continue
        if f_role:
            # Determine favourite/underdog from odds — keep games where
            # the favourite won (role=favorite) or underdog won (role=underdog)
            h_odds = r.get("home_ml") or r.get("home_odds")
            a_odds = r.get("away_ml") or r.get("away_odds")
            if not h_odds and not a_odds:
                hs = r.get("home_spread")
                if hs is not None:
                    h_odds = 1.5 if hs < 0 else 2.5
                    a_odds = 2.5 if hs < 0 else 1.5
            if not h_odds or not a_odds:
                continue  # skip games without odds data
            try:
                h_fav = float(h_odds) < float(a_odds)
            except (TypeError, ValueError):
                continue
            winner = r.get("winner")
            if not winner:
                continue  # skip draws
            home = r.get("home_team", "")
            fav_team = home if h_fav else r.get("away_team", "")
            fav_won = winner == fav_team
            if f_role == "favorite" and not fav_won:
                continue
            elif f_role == "underdog" and fav_won:
                continue
        filtered.append(r)

    filters_applied = {}
    if f_season: filters_applied["season"] = f_season
    if f_segment: filters_applied["segment"] = f_segment
    if f_date_from: filters_applied["date_from"] = f_date_from
    if f_date_to: filters_applied["date_to"] = f_date_to
    if f_tag: filters_applied["tag"] = f_tag
    if f_role: filters_applied["role"] = f_role

    return filtered, filters_applied


def _build_pw_accuracy_summary(pw_data):
    """Aggregate top-level PW accuracy with full ROI from threshold bands."""
    tc = pw_data.get("total_calls", 0)
    correct = pw_data.get("correct", 0)
    thresh = pw_data.get("by_threshold", {})
    # Sum from bands for aggregate ROI
    flat_pnl = sum(b.get("flat_pnl", 0) for b in thresh.values())
    ml_pnl = sum(b.get("ml_pnl", 0) for b in thresh.values())
    ml_cov = sum(b.get("ml_coverage", 0) for b in thresh.values())
    sc_yes = sum(b.get("spread_cover_yes", 0) for b in thresh.values())
    sc_no = sum(b.get("spread_cover_no", 0) for b in thresh.values())
    ll_pnl = sum(b.get("live_line_pnl", 0) for b in thresh.values())
    ll_count = sum(b.get("live_line_count", 0) for b in thresh.values())
    bk_pnl = sum(b.get("bk_line_pnl", 0) for b in thresh.values())
    bk_count = sum(b.get("bk_line_count", 0) for b in thresh.values())
    games = sum(b.get("games", 0) for b in thresh.values())
    games_correct = sum(b.get("games_correct", 0) for b in thresh.values())
    STAKE = 100.0
    _roi = lambda pnl, n: round(pnl / (n * STAKE) * 100, 1) if n > 0 else None
    _pct = lambda c, t: round(c / t * 100, 1) if t > 0 else None
    sc_total = sc_yes + sc_no
    return {
        "total_calls": tc,
        "correct": correct,
        "suppressed_calls": pw_data.get("suppressed_calls", 0),
        "accuracy_pct": pw_data.get("accuracy_pct"),
        "total_games": games,
        "games_correct": games_correct,
        "games_won_pct": _pct(games_correct, games),
        "flat_pnl": round(flat_pnl, 2),
        "flat_roi_pct": _roi(flat_pnl, tc),
        "ml_pnl": round(ml_pnl, 2),
        "ml_roi_pct": _roi(ml_pnl, ml_cov) if ml_cov > 0 else None,
        "ml_coverage": ml_cov,
        "spread_cover_yes": sc_yes,
        "spread_cover_no": sc_no,
        "spread_covered_pct": _pct(sc_yes, sc_total),
        "spread_roi_pct": _roi(sc_yes * 90.91 - sc_no * STAKE, sc_total) if sc_total > 0 else None,  # -110 juice (#157)
        "live_line_roi_pct": _roi(ll_pnl, ll_count) if ll_count > 0 else None,
        "live_line_count": ll_count,
        "bk_line_roi_pct": _roi(bk_pnl, bk_count) if bk_count > 0 else None,
        "bk_line_count": bk_count,
        "bk_odds_roi_pct": _roi(
            sum(b.get("bk_odds_pnl", 0) for b in thresh.values()),
            sum(b.get("bk_odds_count", 0) for b in thresh.values())
        ) if sum(b.get("bk_odds_count", 0) for b in thresh.values()) > 0 else None,
        "bk_odds_count": sum(b.get("bk_odds_count", 0) for b in thresh.values()),
        "by_threshold": pw_data.get("by_threshold", {}),
        "by_consensus": pw_data.get("by_consensus", {}),
        "by_half": pw_data.get("by_half", {}),
        "by_spread_role": pw_data.get("by_spread_role", {}),
        # Collect all tag keys across buckets (#140)
        "tag_keys": sorted(set(
            t for d in [pw_data.get("by_threshold", {}), pw_data.get("by_consensus", {}),
                        pw_data.get("by_half", {}), pw_data.get("by_spread_role", {})]
            for b in d.values() if isinstance(b, dict)
            for t in b.get("tag_counts", {}).keys()
        )),
    }


def build_analysis_payload(config, params=None):
    """Build condition outcome analysis for the Analysis tab (#32).

    Returns enhanced stats matching NBA Monitor: condition outcomes with
    win/upset/close/comeback/margin/spread, compound outcomes, condition
    combinations, tag outcomes, and PW accuracy breakdown.
    """
    records = _load_history()
    if params:
        records, filters = _filter_records(records, params)
    else:
        filters = {}

    all_records = _load_history()
    seasons = sorted(set(str(r.get("season", "")) for r in all_records if r.get("season")))

    if not records:
        return {"conditions": {}, "compounds": {}, "combinations": [],
                "tags": {}, "predicted_winner_accuracy": {},
                "total_games": 0, "filters": filters, "seasons": seasons}

    from collections import Counter, defaultdict
    from itertools import combinations as itertools_combos
    import math
    import pw_matrix

    decay_lambda = float((config or {}).get("outcomes_decay_lambda", 0.0))
    now = datetime.now(timezone.utc)
    min_sample = int(_param(params or {}, "min_sample", "3"))
    max_combo_size = int(_param(params or {}, "max_combo_size", "3"))
    max_combo_size = max(2, min(5, max_combo_size))
    f_margin_fired = _param(params or {}, "margin_fired", "").lower()
    f_search = _param(params or {}, "search", "").lower()

    total = len(records)
    tag_counts = Counter()

    # ── Condition stats ──────────────────────────────────────────────
    cond_map = defaultdict(list)  # cond_name -> list of fire entries
    compound_map = defaultdict(list)

    for rec in records:
        ts_str = rec.get("kickoff_utc") or rec.get("ts") or ""
        try:
            game_dt = datetime.fromisoformat(ts_str)
            if game_dt.tzinfo is None:
                game_dt = game_dt.replace(tzinfo=timezone.utc)
            days_ago = (now - game_dt).total_seconds() / 86400
        except Exception:
            days_ago = 365
        weight = math.exp(-decay_lambda * days_ago) if decay_lambda > 0 else 1.0

        winner = rec.get("winner", "")
        game_tags = rec.get("postgame_tags", [])
        game_id = str(rec.get("game_id") or rec.get("match_id") or "")
        is_close = "CLOSE" in game_tags
        is_blowout = "BLOWOUT" in game_tags
        is_comeback = "COMEBACK" in game_tags
        home_team = rec.get("home_team", "")
        away_team = rec.get("away_team", "")

        # Compute upset from odds (underdog won)
        is_upset = None
        home_ml = rec.get("home_ml") or rec.get("home_odds")
        away_ml = rec.get("away_ml") or rec.get("away_odds")
        if home_ml and away_ml and winner:
            try:
                fav = home_team if float(home_ml) < float(away_ml) else away_team
                is_upset = (winner != fav)
            except (TypeError, ValueError):
                pass

        for t in game_tags:
            tag_counts[t] += 1
        if is_upset is True:
            tag_counts["UPSET"] = tag_counts.get("UPSET", 0) + 1

        # Determine spread favourite
        home_spread = rec.get("home_spread")
        spread_fav = ""
        if home_spread is not None:
            try:
                spread_fav = home_team if float(home_spread) < 0 else away_team
            except (TypeError, ValueError):
                pass

        seen = set()
        for cf in rec.get("conditions_fired", []):
            cname = cf.get("condition", "")
            team = cf.get("team", "")
            cf_type = cf.get("type", "")
            key = (cname, team, game_id)
            if key in seen:
                continue
            seen.add(key)

            # Margin at fire filter
            margin = cf.get("score_margin")
            if f_margin_fired:
                if margin is None:
                    continue
                if f_margin_fired == "trailing" and margin > 0:
                    continue
                if f_margin_fired == "leading" and margin < 0:
                    continue

            team_won = bool(winner and team == winner)

            # Spread covered check
            covered = None
            if team and spread_fav:
                team_spread = None
                if team == home_team:
                    team_spread = pw_matrix._safe_float(rec.get("home_spread"))
                elif team == away_team:
                    team_spread = pw_matrix._safe_float(rec.get("away_spread"))
                if team_spread is not None:
                    team_margin = pw_matrix._final_team_margin(rec, team)
                    if team_margin is not None:
                        try:
                            covered = (team_margin + float(team_spread)) > 0
                        except (TypeError, ValueError):
                            pass

            entry = {
                "game_id": game_id,
                "team_won": team_won,
                "upset": is_upset,
                "close": is_close,
                "blowout": is_blowout,
                "comeback": is_comeback,
                "weight": weight,
                "margin": margin,
                "covered": covered,
                "tags": game_tags,
                "cond_type": cf_type,
            }

            if cf_type == "compound":
                compound_map[cname].append(entry)
            else:
                cond_map[cname].append(entry)

    def _build_stats(entries_map):
        out = {}
        for name, entries in entries_map.items():
            n = len(entries)
            if n < min_sample:
                continue
            total_w = sum(e["weight"] for e in entries)
            wins_w = sum(e["weight"] for e in entries if e["team_won"])
            margins = [e["margin"] for e in entries if e["margin"] is not None]
            pos_margins = sum(1 for m in margins if m > 0)
            neg_margins = sum(1 for m in margins if m < 0)
            covered_known = [e for e in entries if e["covered"] is not None]
            covered_yes = sum(1 for e in covered_known if e["covered"])
            tag_rates = {}
            for tag in sorted(tag_counts.keys()):
                tag_hits = sum(1 for e in entries if tag in e["tags"])
                tag_rates[tag] = round(tag_hits / n * 100, 1)

            upset_known = [e for e in entries if e.get("upset") is not None]
            upset_yes = sum(1 for e in upset_known if e["upset"])

            # Polarity from condition type + name (#122)
            _ctype = next((e.get("cond_type", "") for e in entries if e.get("cond_type")), "")
            try:
                import outcomes as _out_mod
                _pol = _out_mod.classify_polarity(_ctype, condition_name=name)
            except Exception:
                _pol = None

            unique_games = len(set(e["game_id"] for e in entries if e.get("game_id")))

            out[name] = {
                "games": n,
                "unique_games": unique_games,
                "polarity": _pol,
                "team_wins": round(wins_w / total_w * 100, 1) if total_w > 0 else 0,
                "game_upset": round(upset_yes / len(upset_known) * 100, 1) if upset_known else None,
                "game_close": round(sum(1 for e in entries if e["close"]) / n * 100, 1),
                "game_blowout": round(sum(1 for e in entries if e["blowout"]) / n * 100, 1),
                "game_comeback": round(sum(1 for e in entries if e["comeback"]) / n * 100, 1),
                "positive_margin_diff_pct": round(pos_margins / len(margins) * 100, 1) if margins else None,
                "negative_margin_diff_pct": round(neg_margins / len(margins) * 100, 1) if margins else None,
                "avg_margin_diff": round(sum(margins) / len(margins), 1) if margins else None,
                "spread_covered_pct": round(covered_yes / len(covered_known) * 100, 1) if covered_known else None,
                "tag_rates": tag_rates,
            }
        return out

    conditions = _build_stats(cond_map)
    compounds = _build_stats(compound_map)

    # ── Condition combinations ───────────────────────────────────────
    # Build combos from same-game, same-team condition co-fires
    combo_map = defaultdict(list)
    eligible_conds = frozenset(n for n, entries in cond_map.items() if len(entries) >= min_sample)

    for rec in records:
        winner = rec.get("winner", "")
        game_id = str(rec.get("game_id") or rec.get("match_id") or "")
        game_tags = rec.get("postgame_tags", [])
        is_close = "CLOSE" in game_tags
        is_blowout = "BLOWOUT" in game_tags
        is_comeback = "COMEBACK" in game_tags
        # Compute upset for combos
        combo_is_upset = None
        _hml = rec.get("home_ml") or rec.get("home_odds")
        _aml = rec.get("away_ml") or rec.get("away_odds")
        if _hml and _aml and winner:
            try:
                _fav = rec.get("home_team") if float(_hml) < float(_aml) else rec.get("away_team")
                combo_is_upset = (winner != _fav)
            except (TypeError, ValueError):
                pass
        ts_str = rec.get("kickoff_utc") or rec.get("ts") or ""
        try:
            game_dt = datetime.fromisoformat(ts_str)
            if game_dt.tzinfo is None:
                game_dt = game_dt.replace(tzinfo=timezone.utc)
            days_ago = (now - game_dt).total_seconds() / 86400
        except Exception:
            days_ago = 365
        weight = math.exp(-decay_lambda * days_ago) if decay_lambda > 0 else 1.0

        by_team = defaultdict(set)
        team_won_map = {}
        team_margins_map = defaultdict(list)
        team_covered_map = {}
        home_team_c = rec.get("home_team", "")
        away_team_c = rec.get("away_team", "")
        for cf in rec.get("conditions_fired", []):
            if cf.get("type") == "compound":
                continue
            cname = cf.get("condition", "")
            team = cf.get("team", "")
            if not cname or cname not in eligible_conds:
                continue
            if f_margin_fired:
                margin = cf.get("score_margin")
                if margin is None:
                    continue
                if f_margin_fired == "trailing" and margin > 0:
                    continue
                if f_margin_fired == "leading" and margin < 0:
                    continue
            by_team[team].add(cname)
            if team not in team_won_map:
                team_won_map[team] = bool(winner and team == winner)
            sm = cf.get("score_margin")
            if sm is not None:
                team_margins_map[team].append(sm)
            if team not in team_covered_map:
                t_spread = None
                if team == home_team_c:
                    t_spread = pw_matrix._safe_float(rec.get("home_spread"))
                elif team == away_team_c:
                    t_spread = pw_matrix._safe_float(rec.get("away_spread"))
                if t_spread is not None:
                    t_margin = pw_matrix._final_team_margin(rec, team)
                    if t_margin is not None:
                        try:
                            team_covered_map[team] = (t_margin + float(t_spread)) > 0
                        except (TypeError, ValueError):
                            pass

        for team, names in by_team.items():
            if len(names) > 8:
                names = set(sorted(names, key=lambda n: len(cond_map.get(n, [])), reverse=True)[:8])
            fired = sorted(names)
            if len(fired) < 2:
                continue
            team_won = team_won_map.get(team, False)
            margins = team_margins_map.get(team, [])
            covered = team_covered_map.get(team)
            for size in range(2, min(max_combo_size + 1, len(fired) + 1)):
                for combo in itertools_combos(fired, size):
                    combo_map[combo].append({
                        "game_id": game_id,
                        "team_won": team_won,
                        "upset": combo_is_upset,
                        "close": is_close,
                        "blowout": is_blowout,
                        "comeback": is_comeback,
                        "weight": weight,
                        "margins": margins,
                        "covered": covered,
                    })

    combinations_list = []
    for combo, entries in combo_map.items():
        # Deduplicate by game_id
        seen_games = {}
        for e in entries:
            gid = e["game_id"]
            if gid not in seen_games:
                seen_games[gid] = e
            elif e["team_won"]:
                seen_games[gid] = e  # prefer won
        deduped = list(seen_games.values())
        n = len(deduped)
        if n < min_sample:
            continue
        total_w = sum(e["weight"] for e in deduped)
        wins_w = sum(e["weight"] for e in deduped if e["team_won"])
        combo_upset_known = [e for e in deduped if e.get("upset") is not None]
        combo_upset_yes = sum(1 for e in combo_upset_known if e["upset"])
        # Margin stats from combo entries
        all_margins = []
        for e in deduped:
            all_margins.extend(e.get("margins") or [])
        pos_m = sum(1 for m in all_margins if m > 0)
        neg_m = sum(1 for m in all_margins if m < 0)
        cov_known = [e for e in deduped if e.get("covered") is not None]
        cov_yes = sum(1 for e in cov_known if e["covered"])
        combinations_list.append({
            "conditions": list(combo),
            "games": n,
            "unique_games": n,  # combos are already deduped by game_id
            "team_wins": round(wins_w / total_w * 100, 1) if total_w > 0 else 0,
            "game_upset": round(combo_upset_yes / len(combo_upset_known) * 100, 1) if combo_upset_known else None,
            "game_close": round(sum(1 for e in deduped if e["close"]) / n * 100, 1),
            "game_blowout": round(sum(1 for e in deduped if e["blowout"]) / n * 100, 1),
            "game_comeback": round(sum(1 for e in deduped if e["comeback"]) / n * 100, 1),
            "positive_margin_diff_pct": round(pos_m / len(all_margins) * 100, 1) if all_margins else None,
            "negative_margin_diff_pct": round(neg_m / len(all_margins) * 100, 1) if all_margins else None,
            "avg_margin_diff": round(sum(all_margins) / len(all_margins), 1) if all_margins else None,
            "spread_covered_pct": round(cov_yes / len(cov_known) * 100, 1) if cov_known else None,
        })

    # ── Predicted Winner Accuracy breakdown ──────────────────────────
    f_strict_bk = _param(params or {}, "strict_bk", "") == "1"
    _pw_records = records
    if f_strict_bk:
        _pw_records, _ = _filter_strict_bk(records)
    pw_data = pw_matrix.compute_pw_matrix(_pw_records)

    return {
        "conditions": conditions,
        "compounds": compounds,
        "combinations": combinations_list,
        "tags": dict(tag_counts),
        "predicted_winner_accuracy": _build_pw_accuracy_summary(pw_data),
        "total_games": total,
        "known_tags": sorted(tag_counts.keys()),
        "filters": filters,
        "seasons": seasons,
    }



def _parse_moneyline(ml_str):
    """Parse decimal or American moneyline string to float, or None."""
    if ml_str is None:
        return None
    s = str(ml_str).strip()
    if not s or s == "--" or s == "":
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _decimal_ml_profit(ml_val):
    """Profit on a $100 winning bet at decimal odds. Returns float."""
    if ml_val is None or ml_val <= 1.0:
        return 90.91  # fallback (like -110 American)
    return 100.0 * (ml_val - 1.0)


_ROI_STAKE = 100.0


def _roi_pct(pnl, n):
    return round(pnl / (n * _ROI_STAKE) * 100.0, 1) if n > 0 else None


def _did_team_cover_spread(home_score, away_score, spread_line, is_home):
    """Return True/False/None whether team covered the spread."""
    if home_score is None or away_score is None or spread_line is None:
        return None
    try:
        team_margin = (int(home_score) - int(away_score)) if is_home else (int(away_score) - int(home_score))
        return (team_margin + float(spread_line)) > 0
    except (TypeError, ValueError):
        return None


def _quarter_from_minute(minute):
    """Derive synthetic quarter from game minute (#31)."""
    if minute is None:
        return ""
    if minute < 20:
        return "Q1"
    if minute < 40:
        return "Q2"
    if minute < 60:
        return "Q3"
    if minute < 80:
        return "Q4"
    return "ET"


def _enrich_pw_call(c, rec, _edge_store=None):
    """Enrich a PW call dict with computed fields (#19).

    Returns a new enriched dict — does not mutate the input (#258 Phase 1.6).
    Adds: quarter, live_line, bk_live_line, condition_edge per active_condition.
    """
    c = dict(c)  # shallow copy — do not mutate the cached original
    if c.get("active_conditions"):
        c["active_conditions"] = [dict(ac) for ac in c["active_conditions"]]
    # Derive quarter from game_minute (#31)
    c["quarter"] = _quarter_from_minute(c.get("game_minute"))
    # Derive spread_role from game record odds when missing (#40)
    pred_team = c.get("predicted_team", "")
    if not c.get("spread_role") and rec and pred_team:
        home = rec.get("home_team", "")
        hs = rec.get("home_spread")
        if hs is not None:
            try:
                pred_is_home = pred_team == home
                c["spread_role"] = "favorite" if (pred_is_home and float(hs) < 0) or (not pred_is_home and float(hs) > 0) else "underdog"
            except (TypeError, ValueError):
                pass
        # Also backfill moneyline/spread/bk fields on call if missing
        if pred_team == home:
            ml = rec.get("home_ml")
            sp = rec.get("home_spread")
        else:
            ml = rec.get("away_ml")
            sp = rec.get("away_spread")
        if not c.get("moneyline") and ml is not None:
            c["moneyline"] = str(ml)
        if not c.get("spread") and sp is not None:
            c["spread"] = str(sp)
        # BK fields: do NOT backfill from pregame odds (#56 item 4)
        # BK columns should only show genuine live bookmaker data captured at fire time
    # Synthetic live line: pregame spread - margin at fire
    spread = c.get("spread")
    margin = c.get("score_margin_at_fire")
    if spread and margin is not None:
        try:
            c["live_line"] = round(float(spread) - float(margin), 1)
        except (TypeError, ValueError):
            pass
    # BK live line from bk_spread
    bk_sp = c.get("bk_spread")
    if bk_sp:
        try:
            c["bk_live_line"] = round(float(bk_sp), 1)
        except (TypeError, ValueError):
            pass
    # Condition edge enrichment — flip sign for opponent conditions (#28)
    pred_team = c.get("predicted_team", "")
    if _edge_store and c.get("active_conditions"):
        edge_sum = 0.0
        edge_count = 0
        for ac in c["active_conditions"]:
            raw = _edge_store.edge(ac.get("name", ""))
            if ac.get("team") and ac["team"] != pred_team:
                raw = -raw
            ac["condition_edge"] = round(raw, 1)
            edge_sum += raw
            edge_count += 1
        if edge_count > 0:
            c["avg_edge"] = round(edge_sum / edge_count, 2)
    # Compute q_correct/h1_correct/q_covered/h1_covered at serve-time if missing (#245)
    if c.get("q_correct") is None and rec:
        _q = c.get("quarter", "")
        _predicted = c.get("predicted_team", "")
        _sp = rec.get("score_progression")
        _Q_BOUNDS = {"Q1": (0, 1200), "Q2": (1200, 2400), "Q3": (2400, 3600), "Q4": (3600, 4800)}
        if _sp and _q in _Q_BOUNDS and _predicted:
            _qs, _qe = _Q_BOUNDS[_q]
            _hs, _as2 = 0, 0
            for _e in _sp:
                if _e.get("gameSeconds", 0) <= _qs:
                    _hs = _e.get("homeScore", 0)
                    _as2 = _e.get("awayScore", 0)
            _he, _ae = _hs, _as2
            for _e in _sp:
                if _e.get("gameSeconds", 0) <= _qe:
                    _he = _e.get("homeScore", 0)
                    _ae = _e.get("awayScore", 0)
            _hq = _he - _hs
            _aq = _ae - _as2
            _is_pred_home = (_predicted == rec.get("home_team", ""))
            _pred_q = _hq if _is_pred_home else _aq
            _opp_q = _aq if _is_pred_home else _hq
            c["q_correct"] = bool(_pred_q > _opp_q) if _pred_q != _opp_q else False
            _synth_spr = c.get("synth_spread")
            if _synth_spr is not None and c.get("q_covered") is None:
                try:
                    c["q_covered"] = (_pred_q - _opp_q + float(_synth_spr)) > 0
                except (ValueError, TypeError):
                    pass
            if _q in ("Q1", "Q2") and c.get("h1_correct") is None:
                _h1h = rec.get("home_score_1h")
                _a1h = rec.get("away_score_1h")
                if _h1h is not None and _a1h is not None:
                    _pred_h1 = _h1h if _is_pred_home else _a1h
                    _opp_h1 = _a1h if _is_pred_home else _h1h
                    c["h1_correct"] = bool(_pred_h1 > _opp_h1) if _pred_h1 != _opp_h1 else False
                    if _synth_spr is not None and c.get("h1_covered") is None:
                        try:
                            c["h1_covered"] = (_pred_h1 - _opp_h1 + float(_synth_spr)) > 0
                        except (ValueError, TypeError):
                            pass
    # Enrich missing pw_version and source on older calls (#121)
    if not c.get("pw_version"):
        ts = c.get("ts", "")
        if ts:
            try:
                import pw_trend
                c["pw_version"] = pw_trend.resolve_pw_version(ts[:10])
            except Exception:
                pass
    if not c.get("source"):
        c["source"] = "backfill" if c.get("synthetic") else "live"
    # Derive price_source on read — never written to disk (#258 Phase 4.2)
    if c.get("bk_ml_source"):
        c["price_source"] = "live"
    elif c.get("moneyline") or c.get("bk_moneyline"):
        c["price_source"] = "pregame"
    elif c.get("synth_moneyline") is not None:
        c["price_source"] = "synthetic"
    else:
        c["price_source"] = "pregame"
    # De-vig: multiplicative method from game record odds (#258 Phase 4.9)
    if c.get("market_prob_devig") is None and rec:
        hml = rec.get("home_ml")
        aml = rec.get("away_ml")
        if hml and aml:
            try:
                h_imp = 1.0 / float(hml) if float(hml) > 1 else None
                a_imp = 1.0 / float(aml) if float(aml) > 1 else None
                if h_imp and a_imp:
                    total = h_imp + a_imp
                    pt = str(c.get("predicted_team") or "").strip()
                    ht = str(rec.get("home_team") or "").strip()
                    if pt == ht:
                        c["market_prob_devig"] = round(h_imp / total, 4)
                        c["market_prob_raw"] = round(h_imp, 4)
                    else:
                        c["market_prob_devig"] = round(a_imp / total, 4)
                        c["market_prob_raw"] = round(a_imp, 4)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    # EV and edge (#258 Phase 4.10)
    model_prob = (c.get("pct") or 0) / 100.0
    fair = c.get("market_prob_devig")
    if fair and model_prob:
        c["edge"] = round((model_prob - fair) * 100, 1)
    ml = c.get("bk_moneyline") or c.get("moneyline")
    if ml and model_prob:
        try:
            dec_odds = float(ml)
            if dec_odds > 1:
                c["ev"] = round(model_prob * (dec_odds - 1) - (1 - model_prob), 4)
        except (TypeError, ValueError):
            pass
    return c


def build_pw_calls_payload(params):
    """Return games-grouped PW calls with accuracy stats and ROI (#19).

    Response: {games: [...], predicted_winner_accuracy: {...}, available_seasons: [...]}
    """
    records = _load_history_cached()
    records, filters = _filter_records(records, params)

    f_half = _param(params, "half", "") or _param(params, "pw_half", "")
    f_quarter = _param(params, "quarter", "")
    f_band = _param(params, "pw_band", "")
    _cons_raw = _param(params, "pw_consensus", "")
    f_consensus = set(c.strip() for c in _cons_raw.split(",") if c.strip()) if _cons_raw else set()
    f_spread = _param(params, "spread_role", "")
    f_margin = _param(params, "margin_at_fire", "")
    f_selection = _param(params, "call_selection", "")
    f_pw_source = _param(params, "pw_source", "")
    f_pw_edge = _param(params, "pw_edge", "")
    f_pw_polarity = _param(params, "pw_polarity", "")
    f_strict_bk = _param(params, "strict_bk", "") == "1"

    import nrl_api as _nrl

    # Build edge store for condition_edge enrichment
    _edge_store = None
    try:
        import outcomes
        all_hist = _load_history_cached()
        _edge_store = outcomes.ConditionEdgeStore(all_hist)
    except Exception:
        pass

    games = {}  # game_id -> game_entry
    all_calls_flat = []  # for accuracy stats

    for r in records:
        pw_calls = r.get("predicted_winner_calls", [])
        if not pw_calls:
            continue
        winner = r.get("winner")
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        home_score = r.get("home_score")
        away_score = r.get("away_score")

        filtered_calls = []
        for c in pw_calls:
            if f_half and c.get("half", "") != f_half:
                continue
            if f_quarter and _quarter_from_minute(c.get("game_minute")) != f_quarter:
                continue
            pct = c.get("winner_score_pct") or c.get("pct", 0)
            if f_band:
                band = int(f_band)
                if pct < band or pct >= band + 10:
                    continue
            if f_consensus and c.get("consensus", "") not in f_consensus:
                continue
            if f_spread and c.get("spread_role", "") != f_spread:
                continue
            if f_margin:
                m = c.get("score_margin_at_fire", 0)
                if f_margin == "leading" and m < 0:
                    continue
                if f_margin == "trailing" and m >= 0:
                    continue
            if f_pw_source:
                if f_pw_source.startswith("game_"):
                    _gs = r.get("source", "")
                    if _gs != f_pw_source[5:]:
                        continue
                elif f_pw_source.startswith("call_"):
                    _is_synthetic = c.get("synthetic", True)
                    if f_pw_source == "call_live" and _is_synthetic:
                        continue
                    if f_pw_source == "call_backfill" and not _is_synthetic:
                        continue
                else:
                    _cs = c.get("source") or r.get("source", "")
                    if _cs != f_pw_source:
                        continue
            if f_pw_edge:
                _ce = c.get("avg_edge")
                if _ce is None:
                    continue
                if f_pw_edge == "10+" and _ce < 10:
                    continue
                if f_pw_edge == "5-10" and (_ce < 5 or _ce >= 10):
                    continue
                if f_pw_edge == "0-5" and (_ce < 0 or _ce >= 5):
                    continue
                if f_pw_edge == "neg" and _ce >= 0:
                    continue
            if f_pw_polarity:
                _cpn = c.get("polarity_net", 0) or 0
                if f_pw_polarity == "positive" and _cpn <= 0:
                    continue
                if f_pw_polarity == "negative" and _cpn >= 0:
                    continue
                if f_pw_polarity == "neutral" and _cpn != 0:
                    continue
            # Live-priced filter: require genuine BK odds at fire time
            if f_strict_bk and not c.get("bk_ml_source"):
                continue
            # Enrich call — returns a new dict, does not mutate cached original (#258 1.6)
            ec = _enrich_pw_call(c, r, _edge_store)
            # Mark correct
            if winner and ec.get("correct") is None:
                ec["correct"] = ec.get("predicted_team") == winner
            # Suppress 0-0 score calls — no scoring context (#183)
            _h_at = ec.get("home_score_at_fire")
            _a_at = ec.get("away_score_at_fire")
            if _h_at is not None and _a_at is not None and int(_h_at) == 0 and int(_a_at) == 0:
                ec["_suppressed"] = True
                ec["_suppress_reason"] = "0-0 score"
            filtered_calls.append(ec)

        # Apply call selection
        if f_selection == "first" and filtered_calls:
            filtered_calls = [filtered_calls[0]]
        elif f_selection == "first_per_half" and filtered_calls:
            seen = set()
            sel = []
            for c in filtered_calls:
                h = c.get("half", "")
                if h not in seen:
                    sel.append(c)
                    seen.add(h)
            filtered_calls = sel
        elif f_selection == "first_per_quarter" and filtered_calls:
            seen = set()
            sel = []
            for c in filtered_calls:
                q = _quarter_from_minute(c.get("game_minute"))
                if q not in seen:
                    sel.append(c)
                    seen.add(q)
            filtered_calls = sel
        elif f_selection == "last" and filtered_calls:
            filtered_calls = [filtered_calls[-1]]

        if not filtered_calls:
            continue

        # Determine PW outcome for the game (based on last call)
        last_call = filtered_calls[-1]
        pw_team = last_call.get("predicted_team", "")
        pw_correct = pw_team == winner if winner else None
        pw_is_home = pw_team == home

        # Pregame spread for PW team
        pw_spread = None
        pw_covered = None
        pw_cover_margin = None
        # Use pregame spread from call if available, fall back to game record
        pw_spread_raw = last_call.get("spread")
        if not pw_spread_raw:
            home_spread = r.get("home_spread")
            if home_spread is not None:
                try:
                    pw_spread_raw = str(float(home_spread) if pw_is_home else -float(home_spread))
                except (TypeError, ValueError):
                    pass
        if pw_spread_raw and winner:
            try:
                pw_spread = float(pw_spread_raw)
                covered = _did_team_cover_spread(home_score, away_score, pw_spread, pw_is_home)
                if covered is not None:
                    pw_covered = covered
                    team_margin = (int(home_score) - int(away_score)) if pw_is_home else (int(away_score) - int(home_score))
                    pw_cover_margin = round(team_margin + pw_spread, 1)
            except (TypeError, ValueError):
                pass

        # Determine spread favourite
        _home_spread = r.get("home_spread")
        spread_fav = ""
        if _home_spread is not None:
            try:
                spread_fav = home if float(_home_spread) < 0 else away
            except (TypeError, ValueError):
                pass

        match_id = r.get("match_id")
        game_entry = {
            "game_id": match_id,
            "game_name": "{} vs {}".format(home, away),
            "game_start": r.get("kickoff_utc") or r.get("ts"),
            "home_team": home,
            "away_team": away,
            "home_abbr": _nrl.get_team_abbrev(home),
            "away_abbr": _nrl.get_team_abbrev(away),
            "home_score": home_score,
            "away_score": away_score,
            "winner": winner,
            "final_margin": r.get("margin"),
            "source": "history",
            "pw_correct": pw_correct,
            "pw_role": last_call.get("spread_role", ""),
            "pw_moneyline": last_call.get("moneyline") or last_call.get("live_moneyline", ""),
            "pw_spread": pw_spread,
            "pw_covered": pw_covered,
            "pw_cover_margin": pw_cover_margin,
            "home_ml": r.get("home_ml"),
            "away_ml": r.get("away_ml"),
            "home_spread": r.get("home_spread"),
            "away_spread": r.get("away_spread"),
            "spread_fav": spread_fav,
            "season": r.get("season"),
            "season_segment": r.get("season_segment"),
            "calls": filtered_calls,
        }
        games[match_id] = game_entry
        all_calls_flat.extend(filtered_calls)

    # ── Merge live PW calls from .alert_state.json (#19) ──────────────────
    # Skip live/recent games when season filter excludes current season (#62)
    # Also skip when segment filter is set to something specific (#102)
    _skip_live = False
    _f_season = filters.get("season", "")
    _f_segment = filters.get("segment", "")
    if _f_season and _f_season != str(time.gmtime().tm_year):
        _skip_live = True
    if _f_segment and _f_segment == "state_of_origin":
        _skip_live = True  # Live games are Premiership, not SOO (unless SOO is actually live)

    _live_stats_fresh = False
    try:
        if os.path.exists(LIVE_STATS_FILE):
            _ls_mtime = os.path.getmtime(LIVE_STATS_FILE)
            _live_stats_fresh = (time.time() - _ls_mtime) < 600  # 10 minutes
    except Exception:
        pass

    state_file = os.path.join(SCRIPT_DIR, ".alert_state.json")
    live_stats_data = _read_json(LIVE_STATS_FILE) or {}
    live_games_map = {}
    for lg in live_stats_data.get("games", []):
        mid = lg.get("match_id")
        if mid:
            live_games_map[mid] = lg

    state_data = (_read_json(state_file) or {}) if not _skip_live else {}
    for key, calls in state_data.items():
        if not key.startswith("pw_calls_") or not isinstance(calls, list):
            continue
        gid = key[len("pw_calls_"):]
        if gid in games:
            continue  # already in history
        if _skip_live:
            continue  # season filter excludes current/live games
        enriched = []
        for c in calls:
            ec = _enrich_pw_call(c, {}, _edge_store)
            # Suppress 0-0 score calls (#183)
            _h_at = ec.get("home_score_at_fire")
            _a_at = ec.get("away_score_at_fire")
            if _h_at is not None and _a_at is not None and int(_h_at) == 0 and int(_a_at) == 0:
                ec["_suppressed"] = True
                ec["_suppress_reason"] = "0-0 score"
            # Apply same per-call filters as history calls (#159)
            if f_half and ec.get("half", "") != f_half:
                continue
            if f_quarter and _quarter_from_minute(ec.get("game_minute")) != f_quarter:
                continue
            if f_spread and ec.get("spread_role", "") != f_spread:
                continue
            if f_band:
                _pct = ec.get("winner_score_pct") or ec.get("pct", 0)
                _b = int(f_band)
                if _pct < _b or _pct >= _b + 10:
                    continue
            if f_consensus and ec.get("consensus", "") not in f_consensus:
                continue
            if f_margin:
                _m = ec.get("score_margin_at_fire", 0)
                if f_margin == "leading" and _m < 0:
                    continue
                if f_margin == "trailing" and _m >= 0:
                    continue
            if f_strict_bk and not ec.get("bk_ml_source"):
                continue
            enriched.append(ec)
        # Build game entry from live_stats or derive from PW calls / match_id
        lg = live_games_map.get(gid, {})
        home = lg.get("home_team", "")
        away = lg.get("away_team", "")
        # Fallback: extract team names from PW calls
        if not home and enriched:
            pred = enriched[-1].get("predicted_team", "")
            # Try to find home/away from call data
            for c in enriched:
                if c.get("home_score_at_fire") is not None:
                    break
            # Parse from match_id URL: /draw/.../round-N/team1-v-team2/
            if not home and "/" in gid:
                parts = gid.rstrip("/").split("/")
                if parts and "-v-" in parts[-1]:
                    slugs = parts[-1].split("-v-")
                    home = slugs[0].replace("-", " ").title() if len(slugs) > 0 else ""
                    away = slugs[1].replace("-", " ").title() if len(slugs) > 1 else ""
        # Only mark as "live" if the game is actually in the current live games list
        _is_actually_live = gid in live_games_map and live_games_map[gid].get("is_live")
        source = "live" if (_live_stats_fresh and _is_actually_live) else "recent"
        # Derive scores from live_stats or last PW call
        _hs = lg.get("home_score")
        _as = lg.get("away_score")
        if _hs is None and enriched:
            _last = enriched[-1]
            _hs = _last.get("home_score_at_fire")
            _as = _last.get("away_score_at_fire")
        _winner = None
        _margin = None
        if _hs is not None and _as is not None:
            try:
                _hsi, _asi = int(_hs), int(_as)
                if _hsi > _asi:
                    _winner = home
                    _margin = _hsi - _asi
                elif _asi > _hsi:
                    _winner = away
                    _margin = _asi - _hsi
            except (TypeError, ValueError):
                pass
        # PW correctness
        _pw_correct = None
        _last_pred = enriched[-1].get("predicted_team", "") if enriched else ""
        if _winner and _last_pred:
            _pw_correct = _last_pred == _winner
        # Spread from last call or pregame
        _pw_spread = None
        if enriched:
            _sp = enriched[-1].get("spread") or enriched[-1].get("bk_spread")
            if _sp:
                try:
                    _pw_spread = float(_sp)
                except (TypeError, ValueError):
                    pass
        g = {
            "game_id": gid,
            "game_name": "{} vs {}".format(home, away) if home and away else gid,
            "game_start": enriched[0].get("ts", "") if enriched else "",
            "home_team": home,
            "away_team": away,
            "home_abbr": _nrl.get_team_abbrev(home),
            "away_abbr": _nrl.get_team_abbrev(away),
            "home_score": _hs,
            "away_score": _as,
            "winner": _winner,
            "final_margin": _margin,
            "source": source,
            "pw_correct": _pw_correct,
            "pw_role": enriched[-1].get("spread_role", "") if enriched else "",
            "pw_moneyline": enriched[-1].get("moneyline", "") if enriched else "",
            "pw_spread": _pw_spread,
            "pw_covered": None,
            "pw_cover_margin": None,
            "period": lg.get("period", ""),
            "clock": lg.get("clock", ""),
            "calls": enriched,
        }
        # Include pregame odds for live games (#74)
        _pre = lg.get("pregame_odds") or {}
        if _pre:
            g["pregame_home_ml"] = _pre.get("home_ml")
            g["pregame_away_ml"] = _pre.get("away_ml")
            g["pregame_home_spread"] = _pre.get("home_spread")
            g["pregame_away_spread"] = _pre.get("away_spread")
            g["pregame_total"] = _pre.get("total_over")
            g["pregame_bookmaker"] = _pre.get("bookmaker") or _pre.get("ml_source", "")
        games[gid] = g
        all_calls_flat.extend(enriched)

    # Inject live games with no PW calls yet (show card with 0 calls)
    # Also update existing games with current game state (#196)
    if not _skip_live and _live_stats_fresh:
        for mid, lg in live_games_map.items():
            if not lg.get("is_live"):
                continue
            if mid in games:
                # Update existing game with current live state (#196)
                games[mid]["period"] = lg.get("period", "")
                games[mid]["clock"] = lg.get("clock", "")
                games[mid]["home_score"] = lg.get("home_score")
                games[mid]["away_score"] = lg.get("away_score")
                games[mid]["source"] = "live"
                continue
            home = lg.get("home_team", "")
            away = lg.get("away_team", "")
            g = {
                "game_id": mid,
                "game_name": "{} vs {}".format(home, away) if home and away else mid,
                "game_start": "",
                "home_team": home,
                "away_team": away,
                "home_abbr": _nrl.get_team_abbrev(home),
                "away_abbr": _nrl.get_team_abbrev(away),
                "home_score": lg.get("home_score"),
                "away_score": lg.get("away_score"),
                "winner": None,
                "final_margin": None,
                "source": "live",
                "pw_correct": None,
                "pw_role": "",
                "pw_moneyline": "",
                "pw_spread": None,
                "pw_covered": None,
                "pw_cover_margin": None,
                "period": lg.get("period", ""),
                "clock": lg.get("clock", ""),
                "calls": [],
            }
            _pre = lg.get("pregame_odds") or {}
            if _pre:
                g["pregame_home_ml"] = _pre.get("home_ml")
                g["pregame_away_ml"] = _pre.get("away_ml")
                g["pregame_home_spread"] = _pre.get("home_spread")
                g["pregame_away_spread"] = _pre.get("away_spread")
                g["pregame_total"] = _pre.get("total_over")
                g["pregame_bookmaker"] = _pre.get("bookmaker") or _pre.get("ml_source", "")
            games[mid] = g

    games_list = list(games.values())

    # ── Accuracy stats + ROI metrics (#19) ────────────────────────────────
    # Exclude suppressed calls from accuracy/ROI (#325)
    _non_suppressed = [c for c in all_calls_flat if not c.get("_suppressed")]
    total_calls = len(_non_suppressed)
    decided = [c for c in _non_suppressed if c.get("correct") is not None]
    correct = sum(1 for c in decided if c.get("correct") is True)
    leading = [c for c in decided if c.get("score_margin_at_fire", 0) >= 0]
    trailing = [c for c in decided if c.get("score_margin_at_fire", 0) < 0]
    games_correct = sum(1 for g in games_list if g.get("pw_correct") is True)
    games_total = sum(1 for g in games_list if g.get("pw_correct") is not None)

    # Build call→game index for ROI lookups (O(1) instead of O(n*m))
    _call_game = {}  # id(call) -> game_entry
    for g in games_list:
        for c in g.get("calls", []):
            _call_game[id(c)] = g

    # ROI accumulators
    flat_pnl = 0.0
    ml_pnl = 0.0
    ml_count = 0
    spread_cover_yes = 0
    spread_cover_no = 0
    live_line_pnl = 0.0
    live_line_count = 0
    bk_line_pnl = 0.0
    bk_line_count = 0
    bk_odds_pnl = 0.0
    bk_odds_count = 0

    for c in decided:
        is_correct = c.get("correct") is True
        cg = _call_game.get(id(c))
        # Flat ROI: -110 equivalent ($90.91 profit / $100 stake)
        flat_pnl += (90.91 if is_correct else -_ROI_STAKE)
        # ML ROI using moneyline
        ml = _parse_moneyline(c.get("moneyline") or c.get("live_moneyline"))
        if ml is not None:
            ml_count += 1
            ml_pnl += (_decimal_ml_profit(ml) if is_correct else -_ROI_STAKE)
        else:
            ml_pnl += (90.91 if is_correct else -_ROI_STAKE)
        # Spread coverage
        sp = c.get("spread")
        if sp and cg and cg.get("home_score") is not None and cg.get("away_score") is not None:
            try:
                sp_val = float(sp)
                pred_is_home = c.get("predicted_team") == cg.get("home_team")
                covered = _did_team_cover_spread(
                    cg["home_score"], cg["away_score"], sp_val, pred_is_home)
                if covered is True:
                    spread_cover_yes += 1
                elif covered is False:
                    spread_cover_no += 1
            except (TypeError, ValueError):
                pass
        # Live line ROI: pregame spread - margin at fire
        ll = c.get("live_line")
        if ll is not None and cg and cg.get("home_score") is not None and cg.get("winner"):
            pred_is_home = c.get("predicted_team") == cg.get("home_team")
            ll_covered = _did_team_cover_spread(
                cg["home_score"], cg["away_score"], ll, pred_is_home)
            if ll_covered is not None:
                live_line_count += 1
                live_line_pnl += (90.91 if ll_covered else -_ROI_STAKE)
        # BK spread ROI
        bk_ll = c.get("bk_live_line")
        if bk_ll is not None and cg and cg.get("home_score") is not None and cg.get("winner"):
            pred_is_home = c.get("predicted_team") == cg.get("home_team")
            bk_covered = _did_team_cover_spread(
                cg["home_score"], cg["away_score"], bk_ll, pred_is_home)
            if bk_covered is not None:
                bk_line_count += 1
                _bk_sp_price = _parse_moneyline(c.get("bk_spread_price"))
                _bk_sp_win = _decimal_ml_profit(_bk_sp_price) if _bk_sp_price is not None and _bk_sp_price > 1.0 else 90.91
                bk_line_pnl += (_bk_sp_win if bk_covered else -_ROI_STAKE)
        # BK odds ROI
        bk_ml = _parse_moneyline(c.get("bk_moneyline"))
        if bk_ml is not None:
            bk_odds_count += 1
            bk_odds_pnl += (_decimal_ml_profit(bk_ml) if is_correct else -_ROI_STAKE)

    n_decided = len(decided)
    sc_total = spread_cover_yes + spread_cover_no
    spread_pnl = spread_cover_yes * 90.91 - spread_cover_no * _ROI_STAKE

    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    _suppressed_count = len([c for c in all_calls_flat if c.get("_suppressed")])
    accuracy = {
        "total_calls": total_calls,
        "correct": correct,
        "suppressed_calls": _suppressed_count,
        "accuracy_pct": _pct(correct, n_decided) or 0,
        "total_games": len(games_list),
        "games_correct": games_correct,
        "games_won_pct": _pct(games_correct, games_total) or 0,
        "leading_calls": len(leading),
        "leading_correct": sum(1 for c in leading if c.get("correct") is True),
        "leading_accuracy_pct": _pct(sum(1 for c in leading if c.get("correct") is True), len(leading)) or 0,
        "trailing_calls": len(trailing),
        "trailing_correct": sum(1 for c in trailing if c.get("correct") is True),
        "trailing_accuracy_pct": _pct(sum(1 for c in trailing if c.get("correct") is True), len(trailing)) or 0,
        # ROI metrics (#19)
        "flat_roi_pct": _roi_pct(flat_pnl, n_decided),
        "ml_roi_pct": _roi_pct(ml_pnl, n_decided),
        "ml_coverage": ml_count,
        "spread_cover_yes": spread_cover_yes,
        "spread_cover_no": spread_cover_no,
        "spread_covered_pct": _pct(spread_cover_yes, sc_total),
        "spread_roi_pct": _roi_pct(spread_pnl, sc_total) if sc_total > 0 else None,
        "live_line_roi_pct": _roi_pct(live_line_pnl, live_line_count) if live_line_count > 0 else None,
        "live_line_count": live_line_count,
        "bk_odds_roi_pct": _roi_pct(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        "bk_odds_count": bk_odds_count,
        "bk_line_roi_pct": _roi_pct(bk_line_pnl, bk_line_count) if bk_line_count > 0 else None,
        "bk_line_count": bk_line_count,
        "strict_bk": f_strict_bk,
        "has_synthetic_bk": not f_strict_bk and any(
            not c.get("bk_ml_source") for c in decided
        ),
    }

    # Scenario enrichment: stamp each call with scenario_key + scenario_stats (NRL #191)
    # Uses ALL history with no filters so stats reflect full historical
    # profitability — stable regardless of the PW tab's current filters.
    try:
        import outcomes as _sc_outcomes_mod
        _sc_history = _load_history_cached()
        _sc_stats = _sc_outcomes_mod.compute_scenario_stats(_sc_history)
        for g in games.values():
            _g_sf = g.get("spread_fav", "")
            for c in g.get("calls", []):
                skey = _sc_outcomes_mod.scenario_key_from_call(c, _g_sf)
                if skey:
                    c["scenario_key"] = skey
                    c["scenario_stats"] = _sc_stats.get(skey)
    except Exception:
        pass  # Scenario enrichment is best-effort

    # Available seasons
    all_records = _load_history()
    seasons = sorted(set(str(r.get("season", "")) for r in all_records if r.get("season")))

    return {
        "games": games_list,
        "predicted_winner_accuracy": accuracy,
        "available_seasons": seasons,
        "filters": filters,
    }


def _filter_strict_bk(records):
    """Keep only calls with genuine BK odds at fire time (#258 Phase 4.1).

    Requires bk_ml_source (set only by monitor.py from a live Odds API quote).
    Returns (filtered_records, had_synthetic) tuple.
    """
    had_synthetic = False
    filtered = []
    for r in records:
        pw_calls = r.get("predicted_winner_calls")
        if not isinstance(pw_calls, list):
            filtered.append(r)
            continue
        genuine = []
        for c in pw_calls:
            if c.get("bk_ml_source"):
                genuine.append(c)
            else:
                had_synthetic = True
        if genuine:
            r2 = dict(r)
            r2["predicted_winner_calls"] = genuine
            filtered.append(r2)
        # else: skip game entirely (no genuine BK calls)
    return filtered, had_synthetic


def build_pw_trend_payload(params):
    """Return PW trend data (changelog + snapshots) from pw_trend module.

    When date_from/date_to/last_n_games are provided, computes per-game-date
    series from game_history instead of returning stored snapshots.
    """
    import pw_trend
    date_from = _param(params, "date_from", "")
    date_to = _param(params, "date_to", "")
    last_n_games = _param(params, "last_n_games", "")
    interval = _param(params, "interval", "")
    is_cumulative = _param(params, "cumulative", "1") != "0"
    source = _param(params, "source", "")
    f_season = _param(params, "season", "")
    f_segment = _param(params, "segment", "")
    f_soo_game = _param(params, "soo_game", "")
    f_strict_bk = _param(params, "strict_bk", "") == "1"

    if date_from or date_to or last_n_games or interval or not is_cumulative or source or f_season or f_segment or f_soo_game or f_strict_bk:
        history = _load_history()
        if f_strict_bk:
            history, _ = _filter_strict_bk(history)
        if f_season:
            history = [r for r in history if str(r.get("season", "")) == f_season]
        if f_segment:
            history = [r for r in history if r.get("season_segment", "") == f_segment]
        if f_soo_game:
            history = [r for r in history if str(r.get("soo_game", "")) == f_soo_game]
        if source:
            if source == "other":
                history = [r for r in history if r.get("source", "") not in ("live", "backfill", "recovered")]
            else:
                history = [r for r in history if r.get("source", "") == source]
        n_games = None
        if last_n_games:
            try:
                n_games = int(last_n_games)
            except ValueError:
                pass
        series = pw_trend.compute_trend_series(
            history,
            date_from=date_from or None,
            date_to=date_to or None,
            last_n_games=n_games,
            interval=interval or None,
            cumulative=is_cumulative,
        )
        # Always include a cumulative totals snapshot for comparison tables
        cumulative_snapshot = pw_trend.compute_pw_snapshot(history)
        data = pw_trend.load_trend_data()
        return {
            "changelog": data.get("changelog", []),
            "snapshots": series,
            "cumulative_snapshot": cumulative_snapshot,
            "filtered": True,
            "date_from": date_from,
            "date_to": date_to,
            "last_n_games": n_games,
            "interval": interval,
            "cumulative": is_cumulative,
        }

    data = pw_trend.load_trend_data()
    # Always include cumulative snapshot for comparison tables
    history = _load_history()
    if f_strict_bk:
        history, _ = _filter_strict_bk(history)
    data["cumulative_snapshot"] = pw_trend.compute_pw_snapshot(history)
    return data


def build_pw_matrix_payload(params):
    """Return PW ROI matrix cross-dimensional pivot data (#29)."""
    import pw_matrix
    records = _load_history()
    # Apply season/segment filters
    f_season = _param(params, "season", "")
    f_segment = _param(params, "segment", "")
    f_soo_game = _param(params, "soo_game", "")
    f_strict_bk = _param(params, "strict_bk", "") == "1"
    if f_season or f_segment or f_soo_game:
        records, _ = _filter_records(records, params)
    if f_strict_bk:
        records, _ = _filter_strict_bk(records)
    # Collect pin values
    pins = {}
    for dim in ("threshold", "consensus", "half", "quarter", "spread_role", "edge_range", "margin"):
        val = _param(params, "matrix_pin_" + dim, "")
        if val:
            pins[dim] = val
    # Custom compound pivot — row_dims/col_dims are comma-separated dimension names
    _row_dims_raw = _param(params, "row_dims", "")
    _col_dims_raw = _param(params, "col_dims", "")
    _valid_dims = {"threshold", "consensus", "half", "quarter", "spread_role", "edge_range", "margin"}
    custom_pivot = None
    if _row_dims_raw and _col_dims_raw:
        _rd = [d.strip() for d in _row_dims_raw.split(",") if d.strip() in _valid_dims]
        _cd = [d.strip() for d in _col_dims_raw.split(",") if d.strip() in _valid_dims]
        if _rd and _cd:
            custom_pivot = {"row_dims": _rd, "col_dims": _cd}
    data = pw_matrix.compute_pw_matrix(records, pins=pins, custom_pivot=custom_pivot)
    data["filters"] = {}
    if f_season:
        data["filters"]["season"] = f_season
    if f_segment:
        data["filters"]["segment"] = f_segment
    if pins:
        data["filters"]["pins"] = pins
    return data


def build_pw_roi_combos_payload(params):
    """Return ROI analysis by filter combination (#30).

    Iterates all combos of role x margin x call_selection x half (36 total),
    computing full ROI metrics for each. Replaces the old condition-name grouping.
    """
    import time as _time
    import pw_matrix
    t0 = _time.time()

    records = _load_history()
    f_season = _param(params, "season", "")
    f_segment = _param(params, "segment", "")
    f_soo_game = _param(params, "soo_game", "")
    if f_season or f_segment or f_soo_game:
        records, _ = _filter_records(records, params)

    sort_by = _param(params, "sort", "ml_roi_pct")
    f_strict_bk = _param(params, "strict_bk", "") == "1"
    try:
        limit = max(1, min(100, int(_param(params, "limit", "10"))))
    except (TypeError, ValueError):
        limit = 10
    try:
        min_calls = max(1, min(500, int(_param(params, "min_calls", "1"))))
    except (TypeError, ValueError):
        min_calls = 1

    valid_sorts = {"games_won_pct", "flat_roi_pct", "ml_roi_pct",
                   "spread_covered_pct", "spread_roi_pct", "live_line_roi_pct",
                   "bk_line_roi_pct", "accuracy_pct"}
    if sort_by not in valid_sorts:
        sort_by = "ml_roi_pct"

    roles = ["favorite", "underdog"]
    margins = ["trailing", "leading"]
    selections = ["first", "first_per_half", "first_per_quarter", "last"]
    halves = ["H1", "H2", "ET"]
    quarters = ["Q1", "Q2", "Q3", "Q4", "ET"]

    # Build call list once, then filter per combo
    all_calls = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pw_calls = rec.get("predicted_winner_calls")
        if not isinstance(pw_calls, list):
            continue
        game_id = str(rec.get("game_id") or rec.get("match_id") or "")
        for call in pw_calls:
            if not isinstance(call, dict):
                continue
            if call.get("_suppressed"):
                continue
            c = call.get("correct")
            if c is None:
                continue
            # Strict BK filter (#132): exclude calls without genuine BK data
            _has_genuine_bk = bool(call.get("bk_moneyline") or call.get("bk_spread"))
            if f_strict_bk and not _has_genuine_bk:
                continue
            pred_team = (call.get("predicted_team") or "").strip()
            is_correct = bool(c)
            pct = pw_matrix._safe_float(call.get("pct") or call.get("winner_score_pct")) or 0.0
            # Derive spread_role from game record when missing (#40)
            role = (call.get("spread_role") or "").strip().lower()
            ml_odds = pw_matrix._safe_float(call.get("moneyline"))
            if not role and pred_team:
                home = rec.get("home_team", "")
                hs = rec.get("home_spread")
                if hs is not None:
                    try:
                        pred_is_home = pred_team == home
                        role = "favorite" if (pred_is_home and float(hs) < 0) or (not pred_is_home and float(hs) > 0) else "underdog"
                    except (TypeError, ValueError):
                        pass
                if ml_odds is None and pred_team:
                    if pred_team == home:
                        ml_odds = pw_matrix._safe_float(rec.get("home_ml"))
                    else:
                        ml_odds = pw_matrix._safe_float(rec.get("away_ml"))
            has_ml = ml_odds is not None and ml_odds > 1.0
            smaf = call.get("score_margin_at_fire")
            margin_sign = "trailing" if smaf is not None and smaf <= 0 else "leading" if smaf is not None else ""
            spread_val = pw_matrix._safe_float(call.get("spread"))
            if spread_val is None and pred_team:
                home = rec.get("home_team", "")
                if pred_team == home:
                    spread_val = pw_matrix._safe_float(rec.get("home_spread"))
                else:
                    spread_val = pw_matrix._safe_float(rec.get("away_spread"))
            spread_cover = pw_matrix._did_cover_spread(rec, pred_team, spread_val) if pred_team else None
            # Enrich call with derived BK/spread for cover checks (#42)
            _ec = call
            if not call.get("bk_spread") and pred_team:
                _ec = dict(call)
                _h = rec.get("home_team", "")
                _bk_sp = rec.get("home_spread") if pred_team == _h else rec.get("away_spread")
                _bk_ml = rec.get("home_ml") if pred_team == _h else rec.get("away_ml")
                if _bk_sp is not None:
                    _ec["bk_spread"] = str(_bk_sp)
                if _bk_ml is not None:
                    _ec["bk_moneyline"] = str(_bk_ml)
                if not _ec.get("spread") and spread_val is not None:
                    _ec["spread"] = str(spread_val)
            ll_cover = pw_matrix._did_cover_live_line(rec, _ec)
            bk_cover = pw_matrix._did_cover_bk_line(rec, _ec)

            _bk_ml_val = pw_matrix._safe_float(_ec.get("bk_moneyline"))
            all_calls.append({
                "correct": is_correct,
                "role": role,
                "half": str(call.get("half") or "").strip().upper(),
                "quarter": call.get("quarter") or _quarter_from_minute(call.get("game_minute")),
                "margin": margin_sign,
                "ts": call.get("ts") or "",
                "game_id": game_id,
                "ml_odds": ml_odds,
                "has_ml": has_ml,
                "spread_cover": spread_cover,
                "ll_cover": ll_cover,
                "bk_cover": bk_cover,
                "bk_ml": _bk_ml_val,
                "_has_genuine_bk": _has_genuine_bk,
            })

    def _select_calls(call_list, sel):
        if sel == "first":
            by_game = {}
            for c in call_list:
                gid = c["game_id"]
                if gid not in by_game or c["ts"] < by_game[gid]["ts"]:
                    by_game[gid] = c
            return list(by_game.values())
        elif sel == "last":
            by_game = {}
            for c in call_list:
                gid = c["game_id"]
                if gid not in by_game or c["ts"] > by_game[gid]["ts"]:
                    by_game[gid] = c
            return list(by_game.values())
        elif sel == "first_per_half":
            by_game_half = {}
            for c in call_list:
                key = (c["game_id"], c["half"])
                if key not in by_game_half or c["ts"] < by_game_half[key]["ts"]:
                    by_game_half[key] = c
            return list(by_game_half.values())
        elif sel == "first_per_quarter":
            by_game_qtr = {}
            for c in call_list:
                key = (c["game_id"], c["quarter"])
                if key not in by_game_qtr or c["ts"] < by_game_qtr[key]["ts"]:
                    by_game_qtr[key] = c
            return list(by_game_qtr.values())
        return call_list

    STAKE = 100.0

    def _roi(pnl, n):
        return round(pnl / (n * STAKE) * 100, 1) if n > 0 else None

    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    def _build_combo(subset, role, margin, sel, half="", quarter=""):
        tc = len(subset)
        if tc < min_calls:
            return None
        correct = sum(1 for c in subset if c["correct"])
        games = len(set(c["game_id"] for c in subset if c["game_id"]))
        games_correct = len(set(c["game_id"] for c in subset if c["game_id"] and c["correct"]))
        flat_pnl = sum(STAKE if c["correct"] else -STAKE for c in subset)
        ml_pnl = 0.0
        ml_count = 0
        for c in subset:
            if c["has_ml"]:
                ml_count += 1
                ml_pnl += pw_matrix._decimal_win_profit(c["ml_odds"]) if c["correct"] else -STAKE
        sc_yes = len(set(c["game_id"] for c in subset if c["spread_cover"] is True))
        sc_no = len(set(c["game_id"] for c in subset if c["spread_cover"] is False))
        sc_total = sc_yes + sc_no
        ll_count = sum(1 for c in subset if c["ll_cover"] is not None)
        ll_pnl = sum(90.91 if c["ll_cover"] else -STAKE for c in subset if c["ll_cover"] is not None)  # -110 juice (#157)
        bk_count = sum(1 for c in subset if c["bk_cover"] is not None)
        bk_pnl = sum(90.91 if c["bk_cover"] else -STAKE for c in subset if c["bk_cover"] is not None)  # -110 juice (#157)
        bk_odds_pnl = 0.0
        bk_odds_count = 0
        for c in subset:
            bml = c.get("bk_ml")
            if bml is not None and bml > 1.0:
                bk_odds_count += 1
                bk_odds_pnl += pw_matrix._decimal_win_profit(bml) if c["correct"] else -STAKE
        return {
            "role": role, "margin": margin, "call_selection": sel,
            "half": half, "quarter": quarter,
            "total_calls": tc, "correct": correct,
            "accuracy_pct": _pct(correct, tc),
            "total_games": games, "games_won_pct": _pct(games_correct, games),
            "flat_roi_pct": _roi(flat_pnl, tc),
            "ml_roi_pct": _roi(ml_pnl, ml_count) if ml_count > 0 else None,
            "spread_covered_pct": _pct(sc_yes, sc_total),
            "spread_roi_pct": _roi(sc_yes * 90.91 - sc_no * STAKE, sc_total) if sc_total > 0 else None,  # -110 juice (#157)
            "live_line_roi_pct": _roi(ll_pnl, ll_count) if ll_count > 0 else None,
            "bk_line_roi_pct": _roi(bk_pnl, bk_count) if bk_count > 0 else None,
            "bk_odds_roi_pct": _roi(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        }

    combos = []
    # Half-based combos
    for role in roles:
        for margin in margins:
            for sel in selections:
                for half in halves:
                    subset = [c for c in all_calls
                              if c["role"] == role and c["margin"] == margin and c["half"] == half]
                    subset = _select_calls(subset, sel)
                    combo = _build_combo(subset, role, margin, sel, half=half)
                    if combo:
                        combos.append(combo)
    # Quarter-based combos (#31)
    for role in roles:
        for margin in margins:
            for sel in selections:
                for qtr in quarters:
                    subset = [c for c in all_calls
                              if c["role"] == role and c["margin"] == margin and c.get("quarter") == qtr]
                    subset = _select_calls(subset, sel)
                    combo = _build_combo(subset, role, margin, sel, quarter=qtr)
                    if combo:
                        combos.append(combo)

    # Sort descending (None values last)
    combos.sort(key=lambda c: (c.get(sort_by) is not None, c.get(sort_by) or 0), reverse=True)
    total_computed = len(combos)
    combos = combos[:limit]

    elapsed_ms = int((_time.time() - t0) * 1000)
    _any_synthetic = not f_strict_bk and any(not c.get("_has_genuine_bk", True) for c in all_calls)
    return {"combos": combos, "total_computed": total_computed, "elapsed_ms": elapsed_ms,
            "strict_bk": f_strict_bk, "has_synthetic_bk": _any_synthetic}


def build_custom_queries_payload(params):
    """Compute custom query results from game history."""
    records = _load_history()
    records, filters = _filter_records(records, params)

    # Seasons available
    all_records = _load_history()
    seasons = sorted(set(str(r.get("season", "")) for r in all_records if r.get("season")))

    total_games = len(records)
    if not total_games:
        return {"total_games": 0, "first_scorer": {}, "first_scorer_by_team": [],
                "h1_totals": {},
                "late_tries_1st": [], "late_tries_2nd": [], "late_tries_3rd": [],
                "trailing_late": [],
                "first_scorer_wins_1h_wins_game": {"games": 0, "wins_game": 0, "pct": 0},
                "first_2h_scorer": {"games": 0, "wins_game": 0, "pct": 0},
                "first_2h_scorer_by_team": [],
                "late_leading_tries": [], "late_trailing_tries": [],
                "filters": filters, "seasons": seasons}

    HALF_SECS = 40 * 60
    GAME_SECS = 80 * 60

    # 1 & 2: First half totals O/U
    lines = [10.5, 12.5, 14.5, 16.5, 18.5, 20.5, 22.5, 24.5, 26.5,
             28.5, 30.5, 32.5, 34.5, 36.5, 38.5, 40.5]
    h1_totals = []
    for r in records:
        h1h = r.get("home_score_1h")
        a1h = r.get("away_score_1h")
        if h1h is not None and a1h is not None:
            h1_totals.append(h1h + a1h)

    h1_data = []
    n = len(h1_totals)
    for line in lines:
        under = sum(1 for t in h1_totals if t < line)
        over = sum(1 for t in h1_totals if t > line)
        h1_data.append({
            "line": line,
            "under": under,
            "under_pct": round(under / n * 100, 1) if n else 0,
            "over": over,
            "over_pct": round(over / n * 100, 1) if n else 0,
        })

    h1_summary = {
        "games": n,
        "avg": round(sum(h1_totals) / n, 1) if n else 0,
        "min": min(h1_totals) if h1_totals else 0,
        "max": max(h1_totals) if h1_totals else 0,
        "median": sorted(h1_totals)[n // 2] if h1_totals else 0,
        "lines": h1_data,
    }

    # First scorer analysis — summary + per-team breakdown
    first_scorer = {"games": 0, "first_wins_1h": 0, "first_wins_game": 0}
    team_first_scorer = {}  # team_name -> stats dict
    for r in records:
        tries = r.get("try_events", [])
        if not tries:
            continue
        first = min(tries, key=lambda t: t.get("game_seconds", 9999))
        first_team = first.get("team", "")
        if not first_team:
            continue
        first_scorer["games"] += 1

        # Determine if first scorer was home/away and favourite/underdog
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        is_home = first_team == home
        # Favourite detection from odds
        h_odds = r.get("home_ml") or r.get("home_odds")
        a_odds = r.get("away_ml") or r.get("away_odds")
        if not h_odds and not a_odds:
            hs = r.get("home_spread")
            if hs is not None:
                h_odds = 1.5 if hs < 0 else 2.5
                a_odds = 2.5 if hs < 0 else 1.5
        is_fav = None
        if h_odds and a_odds:
            try:
                h_fav = float(h_odds) < float(a_odds)
                is_fav = (is_home and h_fav) or (not is_home and not h_fav)
            except (TypeError, ValueError):
                pass

        # Did first scorer win 1H?
        h1h = r.get("home_score_1h")
        a1h = r.get("away_score_1h")
        won_1h = False
        if h1h is not None and a1h is not None:
            h1_winner = home if h1h > a1h else (away if a1h > h1h else None)
            if h1_winner == first_team:
                first_scorer["first_wins_1h"] += 1
                won_1h = True

        # Did first scorer win the game?
        winner = r.get("winner")
        won_game = winner == first_team
        if won_game:
            first_scorer["first_wins_game"] += 1

        margin = r.get("margin", 0) or 0
        # Margin from first scorer's perspective
        team_margin = margin if won_game else -margin

        # Per-team accumulation
        if first_team not in team_first_scorer:
            team_first_scorer[first_team] = {
                "team": first_team, "games": 0,
                "wins_1h": 0, "wins_game": 0,
                "margins": [], "wins_as_fav": 0, "wins_as_dog": 0,
                "games_as_fav": 0, "games_as_dog": 0,
            }
        ts = team_first_scorer[first_team]
        ts["games"] += 1
        if won_1h:
            ts["wins_1h"] += 1
        if won_game:
            ts["wins_game"] += 1
            if is_fav is True:
                ts["wins_as_fav"] += 1
            elif is_fav is False:
                ts["wins_as_dog"] += 1
        ts["margins"].append(team_margin)
        if is_fav is True:
            ts["games_as_fav"] += 1
        elif is_fav is False:
            ts["games_as_dog"] += 1

    fg = first_scorer["games"] or 1
    first_scorer["first_wins_1h_pct"] = round(first_scorer["first_wins_1h"] / fg * 100, 1)
    first_scorer["first_wins_game_pct"] = round(first_scorer["first_wins_game"] / fg * 100, 1)

    # Build per-team table sorted by games desc
    first_scorer_by_team = []
    for ts in sorted(team_first_scorer.values(), key=lambda x: x["games"], reverse=True):
        g = ts["games"] or 1
        avg_margin = round(sum(ts["margins"]) / g, 1) if ts["margins"] else 0
        fav_g = ts["games_as_fav"] or 1
        dog_g = ts["games_as_dog"] or 1
        first_scorer_by_team.append({
            "team": ts["team"],
            "games": ts["games"],
            "wins_1h": ts["wins_1h"],
            "wins_1h_pct": round(ts["wins_1h"] / g * 100, 1),
            "wins_game": ts["wins_game"],
            "wins_game_pct": round(ts["wins_game"] / g * 100, 1),
            "avg_margin": avg_margin,
            "wins_as_fav": ts["wins_as_fav"],
            "wins_as_fav_pct": round(ts["wins_as_fav"] / fav_g * 100, 1) if ts["games_as_fav"] else 0,
            "wins_as_dog": ts["wins_as_dog"],
            "wins_as_dog_pct": round(ts["wins_as_dog"] / dog_g * 100, 1) if ts["games_as_dog"] else 0,
            "total_game_prob": round(ts["wins_game"] / g * 100, 1),
        })

    # 3: Late 2H tries — Nth try scored within window (#78)
    late_windows = [10, 7, 5, 2]
    late_tries_1st = []  # 1st try scored within window
    late_tries_2nd = []  # 2nd try scored within window
    late_tries_3rd = []  # 3rd try scored within window
    # Pre-compute: for each game, get sorted late tries (within last 10 min)
    _outer_cutoff = GAME_SECS - (10 * 60)
    _game_late_tries = []
    for r in records:
        late = sorted(
            [t for t in r.get("try_events", []) if t.get("game_seconds", 0) >= _outer_cutoff],
            key=lambda t: t.get("game_seconds", 0)
        )
        _game_late_tries.append(late)
    for mins in late_windows:
        cutoff = GAME_SECS - (mins * 60)
        g1 = g2 = g3 = 0
        eligible_1 = eligible_2 = eligible_3 = 0
        for late in _game_late_tries:
            if not late:
                continue
            # 1st try: was it within this window?
            eligible_1 += 1
            if late[0].get("game_seconds", 0) >= cutoff:
                g1 += 1
            # 2nd try
            if len(late) >= 2:
                eligible_2 += 1
                if late[1].get("game_seconds", 0) >= cutoff:
                    g2 += 1
            # 3rd try
            if len(late) >= 3:
                eligible_3 += 1
                if late[2].get("game_seconds", 0) >= cutoff:
                    g3 += 1
        late_tries_1st.append({
            "window_minutes": mins, "games": g1, "eligible": eligible_1,
            "games_pct": round(g1 / eligible_1 * 100, 1) if eligible_1 else 0,
            "all_games_pct": round(g1 / total_games * 100, 1) if total_games else 0,
        })
        late_tries_2nd.append({
            "window_minutes": mins, "games": g2, "eligible": eligible_2,
            "games_pct": round(g2 / eligible_2 * 100, 1) if eligible_2 else 0,
            "all_games_pct": round(g2 / total_games * 100, 1) if total_games else 0,
        })
        late_tries_3rd.append({
            "window_minutes": mins, "games": g3, "eligible": eligible_3,
            "games_pct": round(g3 / eligible_3 * 100, 1) if eligible_3 else 0,
            "all_games_pct": round(g3 / total_games * 100, 1) if total_games else 0,
        })

    # 4: Trailing 20+ with late tries
    trailing_data = []
    for mins in late_windows:
        cutoff = GAME_SECS - (mins * 60)
        count_1plus = 0
        count_2plus = 0
        games_trailing = 0
        for r in records:
            for side in ("home", "away"):
                opp = "away" if side == "home" else "home"
                my_score = r.get("{}_score".format(side), 0) or 0
                opp_score = r.get("{}_score".format(opp), 0) or 0
                if opp_score - my_score >= 20:
                    games_trailing += 1
                    team_name = r.get("{}_team".format(side), "")
                    late = [t for t in r.get("try_events", [])
                            if t.get("game_seconds", 0) >= cutoff
                            and t.get("team") == team_name]
                    if len(late) >= 1:
                        count_1plus += 1
                    if len(late) >= 2:
                        count_2plus += 1
        trailing_data.append({
            "window_minutes": mins,
            "games_trailing_20": games_trailing,
            "trailing_1plus_tries": count_1plus,
            "trailing_1plus_pct": round(count_1plus / games_trailing * 100, 1) if games_trailing else 0,
            "trailing_1plus_total_pct": round(count_1plus / total_games * 100, 1) if total_games else 0,
            "trailing_2plus_tries": count_2plus,
            "trailing_2plus_pct": round(count_2plus / games_trailing * 100, 1) if games_trailing else 0,
            "trailing_2plus_total_pct": round(count_2plus / total_games * 100, 1) if total_games else 0,
        })

    # ── Query: First scorer wins 1H → wins game? ──
    fs_wins_1h_wins_game = {"games": 0, "wins_game": 0}
    for r in records:
        tries = r.get("try_events", [])
        if not tries:
            continue
        first = min(tries, key=lambda t: t.get("game_seconds", 9999))
        first_team = first.get("team", "")
        if not first_team:
            continue
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        h1h = r.get("home_score_1h")
        a1h = r.get("away_score_1h")
        if h1h is None or a1h is None:
            continue
        # Did first scorer win 1H?
        is_home = first_team == home
        won_1h = (is_home and h1h > a1h) or (not is_home and a1h > h1h)
        if not won_1h:
            continue
        fs_wins_1h_wins_game["games"] += 1
        winner = r.get("winner", "")
        if winner == first_team:
            fs_wins_1h_wins_game["wins_game"] += 1
    g = fs_wins_1h_wins_game["games"]
    fs_wins_1h_wins_game["pct"] = round(
        fs_wins_1h_wins_game["wins_game"] / g * 100, 1) if g else 0

    # ── Query: First team to score in 2H → wins game? ──
    first_2h_scorer = {"games": 0, "wins_game": 0}
    first_2h_by_team = {}
    for r in records:
        sp = r.get("score_progression", [])
        home = r.get("home_team", "")
        away = r.get("away_team", "")
        winner = r.get("winner", "")
        if not sp or not home or not away:
            continue
        # Find halftime score
        ht_home = r.get("home_score_1h", 0) or 0
        ht_away = r.get("away_score_1h", 0) or 0
        # Find first scoring event after halftime
        first_2h_team = None
        for ev in sorted(sp, key=lambda e: e.get("gameSeconds", 0)):
            gs = ev.get("gameSeconds", 0)
            if gs < HALF_SECS:
                continue
            eh = ev.get("homeScore", 0)
            ea = ev.get("awayScore", 0)
            if eh > ht_home:
                first_2h_team = home
                break
            elif ea > ht_away:
                first_2h_team = away
                break
        if not first_2h_team:
            continue
        first_2h_scorer["games"] += 1
        if winner == first_2h_team:
            first_2h_scorer["wins_game"] += 1
        # Per-team breakdown
        entry = first_2h_by_team.setdefault(first_2h_team,
            {"team": first_2h_team, "games": 0, "wins_game": 0})
        entry["games"] += 1
        if winner == first_2h_team:
            entry["wins_game"] += 1
    g2 = first_2h_scorer["games"]
    first_2h_scorer["pct"] = round(
        first_2h_scorer["wins_game"] / g2 * 100, 1) if g2 else 0
    first_2h_team_list = sorted(first_2h_by_team.values(),
        key=lambda t: t.get("games", 0), reverse=True)
    for t in first_2h_team_list:
        tg = t["games"]
        t["wins_pct"] = round(t["wins_game"] / tg * 100, 1) if tg else 0

    # ── Query: Trailing/leading team scores late try → wins? ──
    def _score_at_time(sp_list, game_seconds):
        """Get home/away score just before a given game_seconds."""
        h, a = 0, 0
        for ev in sorted(sp_list, key=lambda e: e.get("gameSeconds", 0)):
            if ev.get("gameSeconds", 0) > game_seconds:
                break
            h = ev.get("homeScore", 0)
            a = ev.get("awayScore", 0)
        return h, a

    late_windows = [10, 7, 5, 2]
    late_try_prob = []  # probability of try in last N minutes (overall + by role)
    for mins in late_windows:
        cutoff = GAME_SECS - (mins * 60)
        games_with_try = set()
        total_tries = 0
        lead_tries = 0
        lead_games = set()
        lead_wins = 0
        trail_tries = 0
        trail_games = set()
        trail_wins = 0
        tied_tries = 0
        for r in records:
            tries = r.get("try_events", [])
            sp = r.get("score_progression", [])
            home = r.get("home_team", "")
            away = r.get("away_team", "")
            winner = r.get("winner", "")
            gid = r.get("match_id") or r.get("game_id", "")
            for t in tries:
                gs = t.get("game_seconds", 0)
                if gs < cutoff:
                    continue
                total_tries += 1
                games_with_try.add(gid)
                try_team = t.get("team", "")
                if not try_team or not sp:
                    continue
                h_at, a_at = _score_at_time(sp, gs)
                is_home = try_team == home
                team_score = h_at if is_home else a_at
                opp_score = a_at if is_home else h_at
                if team_score > opp_score:
                    lead_tries += 1
                    lead_games.add(gid)
                    if winner == try_team:
                        lead_wins += 1
                elif team_score < opp_score:
                    trail_tries += 1
                    trail_games.add(gid)
                    if winner == try_team:
                        trail_wins += 1
                else:
                    tied_tries += 1
        games_with = len(games_with_try)
        late_try_prob.append({
            "window_minutes": mins,
            "games_with_try": games_with,
            "games_pct": round(games_with / total_games * 100, 1) if total_games else 0,
            "total_tries": total_tries,
            "avg_tries": round(total_tries / total_games, 2) if total_games else 0,
            "leading_tries": lead_tries,
            "leading_pct": round(lead_tries / total_tries * 100, 1) if total_tries else 0,
            "leading_games": len(lead_games),
            "leading_wins": lead_wins,
            "leading_wins_pct": round(lead_wins / lead_tries * 100, 1) if lead_tries else 0,
            "trailing_tries": trail_tries,
            "trailing_pct": round(trail_tries / total_tries * 100, 1) if total_tries else 0,
            "trailing_games": len(trail_games),
            "trailing_wins": trail_wins,
            "trailing_wins_pct": round(trail_wins / trail_tries * 100, 1) if trail_tries else 0,
            "tied_tries": tied_tries,
        })

    return {
        "total_games": total_games,
        "filters": filters,
        "seasons": seasons,
        "first_scorer": first_scorer,
        "first_scorer_by_team": first_scorer_by_team,
        "h1_totals": h1_summary,
        "late_tries_1st": late_tries_1st,
        "late_tries_2nd": late_tries_2nd,
        "late_tries_3rd": late_tries_3rd,
        "trailing_late": trailing_data,
        "first_scorer_wins_1h_wins_game": fs_wins_1h_wins_game,
        "first_2h_scorer": first_2h_scorer,
        "first_2h_scorer_by_team": first_2h_team_list,
        "late_try_probability": late_try_prob,
    }


class ThreadedHTTPServer(HTTPServer):
    """HTTP server with a fixed-size thread pool (#423 parity).

    Replaces ThreadingMixIn (unbounded threads) with ThreadPoolExecutor
    to cap concurrent request threads and prevent memory accumulation.
    """
    _pool = None

    def server_activate(self):
        super().server_activate()
        self._pool = ThreadPoolExecutor(max_workers=4)

    def process_request(self, request, client_address):
        self._pool.submit(self.process_request_thread, request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)
        # Force GC after large responses to prevent memory accumulation (#423)
        if len(body) > 100_000:
            del body
            gc.collect()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        # API routes
        if path == "/api/config":
            data = _read_json(CONFIG_FILE)
            if data is None:
                self.send_json(500, {"error": "cannot read config"})
            else:
                # Strip secrets before sending (#258 Phase 2.2)
                safe = dict(data)
                for k in SECRET_KEYS:
                    if k in safe:
                        safe[k] = "***"
                self.send_json(200, safe)
            return

        if path == "/api/alerts":
            data = _read_json(ALERTS_FILE)
            self.send_json(200, data or [])
            return

        if path == "/api/live-stats":
            data = _read_json(LIVE_STATS_FILE) or {"games": []}
            # Server-side stale check: clear game data if file is too old (#414 parity)
            _ts_str = data.get("ts")
            if _ts_str:
                try:
                    _ts_dt = datetime.fromisoformat(_ts_str.replace("Z", "+00:00"))
                    _age_min = (datetime.now(timezone.utc) - _ts_dt).total_seconds() / 60
                    if _age_min > 15:
                        data["games"] = []
                        data["stale"] = True
                        data["stale_age_minutes"] = round(_age_min)
                except Exception:
                    pass
            self.send_json(200, data)
            return

        if path == "/api/ticker":
            data = _read_json(TICKER_FILE)
            self.send_json(200, data or {"hits": []})
            return

        if path == "/api/history":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_history_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history-game":
            params = parse_qs(parsed.query)
            game_id = params.get("game_id", [""])[0]
            if not game_id:
                self.send_json(400, {"error": "game_id required"})
                return
            try:
                payload = build_history_game_payload(game_id)
                if payload is None:
                    self.send_json(404, {"error": "game not found"})
                else:
                    self.send_json(200, payload)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history-facets":
            try:
                self.send_json(200, build_history_facets_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/ladder":
            try:
                config = _read_json(CONFIG_FILE) or {}
                season = int(config.get("season", 2026))
                ladder = nrl_api.get_ladder(season)
                self.send_json(200, {"teams": ladder, "season": season})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/monitor-status":
            try:
                self.send_json(200, build_monitor_status_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/log-files":
            try:
                self.send_json(200, build_log_files_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e), "files": [], "logical_files": []})
            return

        if path == "/api/log-tail":
            params = parse_qs(parsed.query)
            fname = _param(params, "file", "monitor.log")
            tail_bytes = min(262144, max(1024, int(_param(params, "bytes", "32768"))))
            fpath = _resolve_log_file(fname)
            if not fpath or not os.path.isfile(fpath):
                self.send_json(200, {"ok": False, "error": "File not found: " + fname})
                return
            try:
                with open(fpath, "rb") as f:
                    f.seek(0, 2)
                    file_size = f.tell()
                    f.seek(max(0, file_size - tail_bytes))
                    raw = f.read().decode("utf-8", errors="replace")
                lines = raw.strip().split("\n")
                self.send_json(200, {
                    "ok": True,
                    "content": raw,
                    "file_size": file_size,
                    "byte_count": len(raw.encode("utf-8")),
                    "total_lines": len(lines),
                    "truncated": file_size > tail_bytes,
                    "path": fname,
                })
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/condition-edges":
            try:
                import outcomes
                records = _load_history()
                store = outcomes.ConditionEdgeStore(records)
                conds = []
                for cname, data in store._edges.items():
                    fires = data["fires"]
                    won = data["team_won"]
                    if fires < 3:
                        continue
                    win_pct = won / fires * 100
                    conds.append({
                        "name": cname,
                        "fires": fires,
                        "won": won,
                        "win_pct": round(win_pct, 1),
                        "edge": round(win_pct - 50, 1),
                    })
                conds.sort(key=lambda c: c["edge"], reverse=True)
                self.send_json(200, {"conditions": conds})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/log-events":
            params = parse_qs(parsed.query)
            limit = min(2000, max(10, int(_param(params, "limit", "500"))))
            logical_name = _param(params, "logical_name", "")
            date_from = _param(params, "date_from", "")
            date_to = _param(params, "date_to", "")
            try:
                self.send_json(200, build_log_events_payload(limit, logical_name=logical_name, date_from=date_from, date_to=date_to))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/postgame":
            try:
                config = _read_json(CONFIG_FILE) or {}
                self.send_json(200, build_postgame_payload(config))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/analysis":
            params = parse_qs(parsed.query)
            try:
                config = _read_json(CONFIG_FILE) or {}
                self.send_json(200, build_analysis_payload(config, params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-api-log":
            try:
                import odds_api as odds_mod
                entries = odds_mod.load_log()
                params = parse_qs(parsed.query)
                exclude_type = _param(params, "exclude_type", "")
                if exclude_type:
                    _ex = set(t.strip() for t in exclude_type.split(",") if t.strip())
                    entries = [e for e in entries if e.get("type") not in _ex]
                last_param = _param(params, "last", "")
                if last_param:
                    try:
                        n = int(last_param)
                        entries = entries[-n:]
                    except (TypeError, ValueError):
                        pass
                credit_status = odds_mod.get_credit_status()
                # Mark fallback entries with saved data flag (#179)
                _fb_ts = set(e.get("ts", "") for e in odds_mod.load_fallback_data())
                for e in entries:
                    if e.get("type") == "fallback":
                        e["has_saved_data"] = e.get("ts", "") in _fb_ts
                self.send_json(200, {
                    "entries": entries,
                    "total": len(entries),
                    "credit_status": credit_status,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        def _compute_bk_stats_from_log(log_entries):
            """Compute per-bookmaker market stats from log entries (#97)."""
            bk = {}
            for e in log_entries:
                etype = e.get("type", "")
                if etype not in ("bk_usage", "fallback"):
                    continue
                ml = e.get("ml_source", "")
                spr = e.get("spread_source", "")
                tot = e.get("totals_source", "")
                for src, market in [(ml, "ml"), (spr, "spread"), (tot, "totals")]:
                    if src:
                        entry = bk.setdefault(src, {"ml": 0, "spread": 0, "totals": 0, "primary": 0, "fallback": 0})
                        entry[market] += 1
                if ml and ml in bk:
                    bk[ml]["primary"] += 1
                if spr and spr != ml and spr in bk:
                    bk[spr]["fallback"] += 1
                if tot and tot != ml and tot in bk:
                    bk[tot]["fallback"] += 1
            return bk

        if path == "/api/odds-api-stats":
            try:
                import odds_api as odds_mod
                params = parse_qs(parsed.query)
                days = _param(params, "days", "")
                date_from = _param(params, "date_from", "")
                date_to = _param(params, "date_to", "")

                entries = odds_mod.load_log()
                # Filter by time range
                if days:
                    try:
                        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%SZ")
                        entries = [e for e in entries if (e.get("ts", "") >= cutoff)]
                    except (ValueError, TypeError):
                        pass
                elif date_from or date_to:
                    if date_from:
                        entries = [e for e in entries if (e.get("ts", "")[:10] >= date_from)]
                    if date_to:
                        entries = [e for e in entries if (e.get("ts", "")[:10] <= date_to)]

                # Call stats by context
                from collections import Counter, defaultdict
                calls = [e for e in entries if e.get("type") == "call"]
                by_context = defaultdict(lambda: {"calls": 0, "credits": 0, "ok": 0, "rate_limited": 0, "errors": 0, "latency_sum": 0})
                for c in calls:
                    ctx = c.get("context", "") or "unknown"
                    s = by_context[ctx]
                    s["calls"] += 1
                    s["credits"] += c.get("credits_used", 0)
                    status = c.get("status", 0)
                    if status == 200:
                        s["ok"] += 1
                    elif status == 429:
                        s["rate_limited"] += 1
                    else:
                        s["errors"] += 1
                    if c.get("latency_ms"):
                        s["latency_sum"] += c["latency_ms"]
                context_stats = []
                for ctx, s in sorted(by_context.items()):
                    s["context"] = ctx
                    s["avg_latency_ms"] = round(s["latency_sum"] / s["calls"], 1) if s["calls"] else 0
                    del s["latency_sum"]
                    context_stats.append(s)

                # Overall summary
                total_calls = len(calls)
                total_credits = sum(c.get("credits_used", 0) for c in calls)
                total_ok = sum(1 for c in calls if c.get("status") == 200)
                total_429 = sum(1 for c in calls if c.get("status") == 429)
                total_errors = total_calls - total_ok - total_429

                # Check which fallback events have saved data (#179)
                fb_events = [e for e in entries if e.get("type") == "fallback"]
                _fb_saved_ts = set(e.get("ts", "") for e in odds_mod.load_fallback_data())
                for fb in fb_events:
                    fb["has_saved_data"] = fb.get("ts", "") in _fb_saved_ts

                _cfg_fb = _read_json(CONFIG_FILE) or {}
                self.send_json(200, {
                    "summary": {
                        "total_calls": total_calls,
                        "total_credits": total_credits,
                        "ok": total_ok,
                        "rate_limited": total_429,
                        "errors": total_errors,
                        "credits_remaining": odds_mod.get_credit_status().get("credits_remaining"),
                    },
                    "by_context": context_stats,
                    "bookmaker_stats": _compute_bk_stats_from_log(entries),
                    "fallback_events": fb_events,
                    "entries_in_range": len(entries),
                    "credits": _get_odds_api_credits(_cfg_fb),
                    "fallback_save_enabled": _cfg_fb.get("odds_api_fallback_save_enabled", False),
                    "fallback_retention_days": _cfg_fb.get("odds_api_fallback_retention_days", 7),
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Odds API Test Tool (#175) ─────────────────────────────────────

        if path == "/api/odds-api-test-events":
            params = parse_qs(parsed.query)
            sport = _param(params, "sport", "")
            if not sport:
                self.send_json(400, {"error": "sport param required"})
                return
            config = _read_json(CONFIG_FILE) or {}
            api_key = config.get("odds_api_key", "")
            if not api_key:
                self.send_json(400, {"error": "No odds_api_key configured"})
                return
            try:
                # Use /events/ endpoint (free, 0 credits) — NOT /odds/ which costs per event (#179)
                url = "https://api.the-odds-api.com/v4/sports/{}/events/".format(sport)
                req_params = {"apiKey": api_key}
                import requests as _req
                import time as _tm
                _t0 = _tm.time()
                resp = _req.get(url, params=req_params, timeout=15)
                _latency = round((_tm.time() - _t0) * 1000, 1)
                credits_used = resp.headers.get("x-requests-last", "?")
                credits_remaining = resp.headers.get("x-requests-remaining", "?")
                import odds_api as _oa
                _oa._append_log({"type": "call", "ts": _tm.strftime("%Y-%m-%dT%H:%M:%SZ", _tm.gmtime()),
                    "sport": sport, "markets": "", "credits_used": int(credits_used) if str(credits_used).isdigit() else 0,
                    "credits_remaining": int(credits_remaining) if str(credits_remaining).isdigit() else None,
                    "status": resp.status_code, "latency_ms": _latency, "cache_hit": False, "context": "api_test_events"})
                events = resp.json() if resp.status_code == 200 else []
                game_list = []
                for ev in events:
                    game_list.append({
                        "id": ev.get("id", ""),
                        "home": ev.get("home_team", ""),
                        "away": ev.get("away_team", ""),
                        "commence": ev.get("commence_time", ""),
                        "label": "{} vs {} \u2014 {}".format(
                            ev.get("home_team", "?"), ev.get("away_team", "?"),
                            (ev.get("commence_time") or "")[:10]),
                    })
                self.send_json(200, {
                    "sport": sport,
                    "events": game_list,
                    "credits_used": credits_used,
                    "credits_remaining": credits_remaining,
                    "status": resp.status_code,
                    "request": {"url": url, "params": {k: v for k, v in req_params.items() if k != "apiKey"}},
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-api-test":
            params = parse_qs(parsed.query)
            sport = _param(params, "sport", "")
            event_id = _param(params, "event_id", "")
            markets = _param(params, "markets", "h2h")
            if not sport or not event_id:
                self.send_json(400, {"error": "sport and event_id params required"})
                return
            config = _read_json(CONFIG_FILE) or {}
            api_key = config.get("odds_api_key", "")
            if not api_key:
                self.send_json(400, {"error": "No odds_api_key configured"})
                return
            try:
                url = "https://api.the-odds-api.com/v4/sports/{}/events/{}/odds/".format(sport, event_id)
                req_params = {"apiKey": api_key, "regions": "au", "markets": markets, "oddsFormat": "decimal"}
                import requests as _req
                import time as _tm
                _t0 = _tm.time()
                resp = _req.get(url, params=req_params, timeout=15)
                _latency = round((_tm.time() - _t0) * 1000, 1)
                credits_used = resp.headers.get("x-requests-last", "?")
                credits_remaining = resp.headers.get("x-requests-remaining", "?")
                import odds_api as _oa
                _oa._append_log({"type": "call", "ts": _tm.strftime("%Y-%m-%dT%H:%M:%SZ", _tm.gmtime()),
                    "sport": sport, "markets": markets, "credits_used": int(credits_used) if str(credits_used).isdigit() else 0,
                    "credits_remaining": int(credits_remaining) if str(credits_remaining).isdigit() else None,
                    "status": resp.status_code, "latency_ms": _latency, "cache_hit": False, "context": "api_test"})
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text[:2000]
                self.send_json(200, {
                    "request": {
                        "url": url,
                        "params": {k: v for k, v in req_params.items() if k != "apiKey"},
                        "method": "GET",
                    },
                    "response": {
                        "status": resp.status_code,
                        "body": body,
                    },
                    "credits_used": credits_used,
                    "credits_remaining": credits_remaining,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-api-fallback-data":
            import odds_api as _oa_fb
            _fb_params = parse_qs(parsed.query)
            ts = _param(_fb_params, "ts")
            if ts:
                entry = _oa_fb.get_fallback_data_entry(ts)
                if entry:
                    self.send_json(200, entry)
                else:
                    self.send_json(404, {"error": "No fallback data for timestamp"})
            else:
                entries = _oa_fb.load_fallback_data()
                self.send_json(200, {"entries": entries, "count": len(entries)})
            return

        if path == "/api/ml-training-status":
            try:
                self.send_json(200, _get_ml_training_status())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/ml-train":
            self.send_json(200, {"train_state": dict(_ml_train_state)})
            return

        if path and path.startswith("/api/ml-training-log/"):
            filename = path.split("/")[-1]
            if not _re.match(r'^ml_train_\d{8}_\d{6}\.log$', filename):
                self.send_json(400, {"ok": False, "error": "Invalid filename"})
                return
            log_path = os.path.join(SCRIPT_DIR, "logs", filename)
            if not os.path.isfile(log_path):
                self.send_json(404, {"ok": False, "error": "Log file not found"})
                return
            try:
                with open(log_path, encoding="utf-8", errors="replace") as f:
                    content = f.read()
                self.send_json(200, {"ok": True, "filename": filename, "content": content, "size": len(content)})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})
            return

        if path == "/api/model-status":
            try:
                meta_path = os.path.join(SCRIPT_DIR, "model_meta_nrl.json")
                meta = _read_json(meta_path)
                if meta:
                    self.send_json(200, {"available": True, **meta})
                else:
                    self.send_json(200, {"available": False, "reason": "model not trained"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-calls-live":
            # Lightweight endpoint: only live/recent PW calls from .alert_state.json (#248)
            # No game_history parse — used by dashboard for fast live polling.
            try:
                import nrl_api as _nrl_live

                _live_stats_fresh = False
                try:
                    if os.path.exists(LIVE_STATS_FILE):
                        _ls_mtime = os.path.getmtime(LIVE_STATS_FILE)
                        _live_stats_fresh = (time.time() - _ls_mtime) < 600
                except Exception:
                    pass

                state_data = _load_alert_state_cached()
                live_stats_data = _read_json(LIVE_STATS_FILE) or {}
                live_games_map = {}
                for lg in live_stats_data.get("games", []):
                    mid = lg.get("match_id")
                    if mid:
                        live_games_map[mid] = lg

                _now_ts = time.time()
                _RECENCY_SECS = 7200  # 2 hours

                games = {}
                for key, calls in state_data.items():
                    if not key.startswith("pw_calls_") or not isinstance(calls, list):
                        continue
                    gid = key[len("pw_calls_"):]
                    # Skip games already recorded in history (#252)
                    if state_data.get("game_end_{}".format(gid)):
                        continue
                    # Skip stale games: no calls within recency window and not in live_stats (#252)
                    _is_live = gid in live_games_map and live_games_map[gid].get("is_live")
                    if not _is_live and calls:
                        _latest_ts = max((c.get("ts") or "" for c in calls), default="")
                        if _latest_ts:
                            try:
                                from datetime import datetime, timezone
                                _lt = datetime.fromisoformat(_latest_ts.replace("Z", "+00:00")).timestamp()
                                if (_now_ts - _lt) > _RECENCY_SECS:
                                    continue
                            except Exception:
                                pass
                    enriched = [_enrich_pw_call(c, {}) for c in calls]
                    lg = live_games_map.get(gid, {})
                    home = lg.get("home_team", "")
                    away = lg.get("away_team", "")
                    if not home and "/" in gid:
                        parts = gid.rstrip("/").split("/")
                        if parts and "-v-" in parts[-1]:
                            slugs = parts[-1].split("-v-")
                            home = slugs[0].replace("-", " ").title() if len(slugs) > 0 else ""
                            away = slugs[1].replace("-", " ").title() if len(slugs) > 1 else ""
                    source = "live" if (_live_stats_fresh and _is_live) else "recent"
                    # Pregame odds from alert state (#257)
                    _pre = state_data.get("pregame_odds_{}".format(gid)) or {}
                    _hs = _pre.get("home_spread")
                    _spread_fav = ""
                    if _hs is not None:
                        try:
                            _spread_fav = home if float(_hs) < 0 else away
                        except (TypeError, ValueError):
                            pass
                    games[gid] = {
                        "game_id": gid,
                        "game_name": "{} vs {}".format(home, away) if home and away else gid,
                        "home_team": home, "away_team": away,
                        "home_abbr": _nrl_live.get_team_abbrev(home),
                        "away_abbr": _nrl_live.get_team_abbrev(away),
                        "home_score": lg.get("home_score"),
                        "away_score": lg.get("away_score"),
                        "source": source,
                        "period": lg.get("period", ""),
                        "clock": lg.get("clock", ""),
                        "home_ml": _pre.get("home_ml"),
                        "away_ml": _pre.get("away_ml"),
                        "home_spread": _pre.get("home_spread"),
                        "away_spread": _pre.get("away_spread"),
                        "spread_fav": _spread_fav,
                        "calls": enriched,
                    }

                # Fingerprint from alert_state + live_stats mtimes
                _state_file = os.path.join(SCRIPT_DIR, ".alert_state.json")
                try:
                    _fp_parts = [str(os.path.getmtime(_state_file))]
                except OSError:
                    _fp_parts = ["0"]
                try:
                    _fp_parts.append(str(os.path.getmtime(LIVE_STATS_FILE)))
                except OSError:
                    pass
                _fp = "|".join(_fp_parts)
                self.send_json(200, {"games": list(games.values()), "fingerprint": _fp})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-calls":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_pw_calls_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-trend":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_pw_trend_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-matrix":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_pw_matrix_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-roi-combos":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_pw_roi_combos_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/custom-queries":
            params = parse_qs(parsed.query)
            try:
                self.send_json(200, build_custom_queries_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-monitor":
            try:
                # Refresh config for running odds monitor thread (#129)
                odds_monitor.update_config(_read_json(CONFIG_FILE) or {})
                live = odds_monitor.get_live_snapshots()
                status = odds_monitor.get_status()
                late_probs = odds_monitor.get_late_tries_probs()
                self.send_json(200, {
                    "status": status,
                    "games": live,
                    "late_tries_probs": late_probs,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-monitor-history":
            try:
                history = odds_monitor.load_history()
                # Recalculate EV at serve-time using current late-tries probs (#174)
                probs = odds_monitor.get_late_tries_probs()
                for g in history:
                    for s in g.get("snapshots", []):
                        gm = s.get("game_minute")
                        odds = s.get("odds")
                        if gm is None or not odds:
                            continue
                        remaining = max(1, min(15, 80 - gm))
                        window = probs.get(remaining, {})
                        h1 = window.get("prob_1plus", 0)
                        total_under_price = odds.get("total_under_price")
                        total_over_price = odds.get("total_over_price")
                        implied_under = round(1 / total_under_price, 4) if total_under_price and total_under_price > 0 else None
                        implied_over = round(1 / total_over_price, 4) if total_over_price and total_over_price > 0 else None
                        ev = s.get("ev_analysis") or {}
                        ev["hist_prob_1plus_tries"] = h1
                        ev["hist_prob_2plus_tries"] = window.get("prob_2plus", 0)
                        ev["hist_prob_3plus_tries"] = window.get("prob_3plus", 0)
                        ev["hist_total_games"] = window.get("total_games", 0)
                        if implied_over is not None:
                            ev["ev_edge_1plus"] = round(h1 - implied_over, 4)
                        if implied_under is not None:
                            ev["ev_edge_under"] = round((1 - h1) - implied_under, 4)
                        ev["remaining_minutes"] = remaining
                        s["ev_analysis"] = ev
                self.send_json(200, {"games": history, "total": len(history)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-monitor-leading":
            try:
                params = parse_qs(parsed.query)
                # #220: accept threshold param
                _thresh_raw = (params.get("threshold") or [""])[0].strip()
                _thresh = None
                if _thresh_raw:
                    try:
                        _thresh = float(_thresh_raw)
                    except (ValueError, TypeError):
                        pass
                history = odds_monitor.load_history()
                probs_leading = odds_monitor.get_leading_team_tries_probs()
                probs_trailing = odds_monitor.get_late_tries_probs()
                games = []
                agg = {"bets": 0, "wins": 0, "pnl": 0.0, "total_games": len(history)}
                cfg = _read_json(CONFIG_FILE) or {}
                for g in history:
                    # Backfill trailing_team_ev for legacy snapshots (#213)
                    for s in g.get("snapshots", []):
                        if "trailing_team_ev" not in s:
                            nrl_data = {
                                "home_score": s.get("home_score", 0),
                                "away_score": s.get("away_score", 0),
                                "home_team": s.get("home_team", g.get("home_team", "")),
                                "away_team": s.get("away_team", g.get("away_team", "")),
                                "game_minute": s.get("game_minute", 0),
                            }
                            odds = s.get("odds", {})
                            s["trailing_team_ev"] = odds_monitor._compute_trailing_team_ev(
                                odds, nrl_data, nrl_data["game_minute"], cfg)
                    # Compute BOTH strategies (#229)
                    lead_strat = odds_monitor._compute_leading_strategy(
                        g.get("snapshots", []), g, threshold=_thresh)
                    trail_strat = odds_monitor._compute_trailing_strategy(
                        g.get("snapshots", []), g, threshold=_thresh)
                    lead_strat["margin_context"] = "leading"
                    trail_strat["margin_context"] = "trailing"
                    g["leading_strategy"] = lead_strat
                    g["trailing_strategy"] = trail_strat
                    games.append(g)
                    for strat in [lead_strat, trail_strat]:
                        if strat.get("triggered"):
                            agg["bets"] += 1
                            if strat.get("covered"):
                                agg["wins"] += 1
                            if strat.get("pnl") is not None:
                                agg["pnl"] += strat["pnl"]
                agg["pnl"] = round(agg["pnl"], 2)
                agg["roi"] = round(agg["pnl"] / agg["bets"] * 100, 1) if agg["bets"] else None
                self.send_json(200, {
                    "games": games,
                    "aggregate": agg,
                    "leading_probs": probs_leading,
                    "trailing_probs": probs_trailing,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-monitor-analysis":
            try:
                params = parse_qs(parsed.query)
                try:
                    combo_min = max(2, min(5, int((params.get("combo_min") or ["2"])[0])))
                except (ValueError, TypeError):
                    combo_min = 2
                try:
                    combo_max = max(2, min(5, int((params.get("combo_max") or ["3"])[0])))
                except (ValueError, TypeError):
                    combo_max = 3
                combo_max = max(combo_max, combo_min)
                # Threshold for analysis (#231)
                threshold_raw = (params.get("threshold") or ["best"])[0].strip()
                if threshold_raw == "best":
                    threshold = None
                else:
                    try:
                        threshold = float(threshold_raw)
                    except (ValueError, TypeError):
                        threshold = None
                history = odds_monitor.load_history()
                # Backfill trailing_team_ev for legacy snapshots
                cfg = _read_json(CONFIG_FILE) or {}
                for g in history:
                    for s in g.get("snapshots", []):
                        if "trailing_team_ev" not in s:
                            nrl_data = {
                                "home_score": s.get("home_score", 0),
                                "away_score": s.get("away_score", 0),
                                "home_team": s.get("home_team", g.get("home_team", "")),
                                "away_team": s.get("away_team", g.get("away_team", "")),
                                "game_minute": s.get("game_minute", 0),
                            }
                            odds = s.get("odds", {})
                            s["trailing_team_ev"] = odds_monitor._compute_trailing_team_ev(
                                odds, nrl_data, nrl_data["game_minute"], cfg)
                game_hist = _load_history()
                result = odds_monitor.compute_spread_analysis(
                    history, game_history=game_hist,
                    combo_min=combo_min, combo_max=combo_max,
                    threshold=threshold)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-monitor-historical":
            try:
                params = parse_qs(parsed.query)
                role = (params.get("role") or ["leading"])[0].strip().lower()
                if role not in ("leading", "trailing", "final_stretch"):
                    role = "leading"
                # Parse minutes (comma-separated) or fallback to minute param (#227)
                ALLOWED_MINUTES = {65, 68, 70, 72, 75}
                minutes_raw = (params.get("minutes") or [""])[0].strip()
                if minutes_raw:
                    minutes = [int(m) for m in minutes_raw.split(",")
                               if m.strip().isdigit() and int(m.strip()) in ALLOWED_MINUTES]
                else:
                    # Backwards compat: accept old minute= param
                    try:
                        m = int((params.get("minute") or ["70"])[0])
                    except (ValueError, TypeError):
                        m = 70
                    minutes = [m] if m in ALLOWED_MINUTES else [70]
                minutes = minutes or [70]
                # Filter params (#227)
                season = (params.get("season") or ["all"])[0].strip()
                segment = (params.get("segment") or ["all"])[0].strip()
                side = (params.get("side") or ["all"])[0].strip().lower()
                fav_role = (params.get("fav_role") or ["all"])[0].strip().lower()
                margin_context = (params.get("margin_context") or ["all"])[0].strip().lower()
                margin_bucket = (params.get("margin_bucket") or ["all"])[0].strip()
                # Pagination (#227)
                try:
                    pg = max(1, int((params.get("page") or ["1"])[0]))
                except (ValueError, TypeError):
                    pg = 1
                try:
                    ps = int((params.get("page_size") or ["25"])[0])
                    ps = max(25, min(100, ps))
                except (ValueError, TypeError):
                    ps = 25
                game_hist = _load_history()
                result = odds_monitor.compute_historical_win_analysis(
                    game_hist, role=role, minutes=minutes,
                    season=season, segment=segment, side=side,
                    fav_role=fav_role, margin_context=margin_context,
                    margin_bucket=margin_bucket, page=pg, page_size=ps)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/paper-trading":
            try:
                bets_path = os.path.join(SCRIPT_DIR, "bets.jsonl")
                bets = []
                if os.path.exists(bets_path):
                    with open(bets_path, "r") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    bets.append(json.loads(line))
                                except json.JSONDecodeError:
                                    pass
                history = _load_history_cached()
                game_map = {str(r.get("match_id") or ""): r for r in history if r.get("match_id")}
                rules_path = os.path.join(SCRIPT_DIR, "rules.json")
                rules = {}
                if os.path.exists(rules_path):
                    with open(rules_path) as f:
                        for r in json.load(f).get("rules", []):
                            rules[r["id"]] = r
                rule_stats = {}
                for bet in bets:
                    rid = bet.get("rule_id", "unknown")
                    if rid not in rule_stats:
                        ri = rules.get(rid, {})
                        rule_stats[rid] = {"rule_id": rid, "rule_name": ri.get("name", rid), "registered": ri.get("registered", ""), "total": 0, "resolved": 0, "correct": 0, "ev_sum": 0.0, "ev_count": 0, "bets": []}
                    st = rule_stats[rid]
                    st["total"] += 1
                    gid = str(bet.get("game_id") or "")
                    rec = game_map.get(gid)
                    enriched = dict(bet)
                    if rec and rec.get("winner"):
                        pt = bet.get("predicted_team", "")
                        enriched["correct"] = (pt == rec["winner"])
                        st["resolved"] += 1
                        if enriched["correct"]:
                            st["correct"] += 1
                    if bet.get("ev") is not None:
                        st["ev_sum"] += bet["ev"]
                        st["ev_count"] += 1
                    st["bets"].append(enriched)
                result = []
                for st in rule_stats.values():
                    acc = st["correct"] / st["resolved"] * 100 if st["resolved"] else 0
                    mean_ev = st["ev_sum"] / st["ev_count"] * 100 if st["ev_count"] else None
                    result.append({"rule_id": st["rule_id"], "rule_name": st["rule_name"], "registered": st["registered"], "total_bets": st["total"], "resolved": st["resolved"], "correct": st["correct"], "accuracy_pct": round(acc, 1), "mean_ev_pct": round(mean_ev, 1) if mean_ev is not None else None, "recent_bets": st["bets"][-20:]})
                self.send_json(200, {"rules": result, "total_bets": len(bets)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── PW Export — flat PW call feed for external consumers (#302) ──
        if path == "/api/pw-export":
            try:
                f_since = _param(params, "since")
                f_until = _param(params, "until")
                f_source = _param(params, "source").lower()
                f_include_suppressed = _param(params, "include_suppressed") == "1"

                history = _load_history_cached()

                # Merge live in-progress calls from .alert_state.json
                live_pw_by_game = {}
                if os.path.exists(STATE_FILE):
                    try:
                        with open(STATE_FILE, encoding="utf-8") as f:
                            state = json.load(f)
                        for key, calls in state.items():
                            if key.startswith("pw_calls_") and isinstance(calls, list):
                                gid = key[len("pw_calls_"):]
                                live_pw_by_game[gid] = calls
                    except Exception:
                        pass

                completed_gids = set()
                rows = []

                for rec in history:
                    pw_calls = rec.get("predicted_winner_calls")
                    if not isinstance(pw_calls, list) or not pw_calls:
                        continue
                    rec_source = str(rec.get("source") or "")
                    if f_source and f_source != "all" and rec_source != f_source:
                        continue
                    game_date = str(rec.get("date") or "")
                    if f_since and game_date < f_since:
                        continue
                    if f_until and game_date > f_until:
                        continue

                    gid = str(rec.get("match_id") or "")
                    completed_gids.add(gid)
                    game_ctx = {
                        "game_id": gid,
                        "game_date": game_date,
                        "home_team": str(rec.get("home_team") or ""),
                        "away_team": str(rec.get("away_team") or ""),
                        "home_id": str(rec.get("home_team_id") or ""),
                        "away_id": str(rec.get("away_team_id") or ""),
                        "home_score": rec.get("home_score"),
                        "away_score": rec.get("away_score"),
                        "winner": str(rec.get("winner") or ""),
                        "round": str(rec.get("round") or ""),
                        "season": str(rec.get("season") or ""),
                        "season_segment": str(rec.get("season_segment") or ""),
                        "game_source": rec_source,
                        "game_completed": True,
                    }

                    for c in pw_calls:
                        if not f_include_suppressed and c.get("_suppressed"):
                            continue
                        row = dict(c)
                        row.update(game_ctx)
                        rows.append(row)

                # Append live in-progress calls
                for gid, calls in live_pw_by_game.items():
                    if gid in completed_gids:
                        continue
                    game_ctx = {
                        "game_id": gid,
                        "game_date": "",
                        "home_team": "", "away_team": "",
                        "home_id": "", "away_id": "",
                        "home_score": None, "away_score": None,
                        "winner": "", "round": "", "season": "",
                        "season_segment": "",
                        "game_source": "live",
                        "game_completed": False,
                    }
                    for c in calls:
                        if not f_include_suppressed and c.get("_suppressed"):
                            continue
                        row = dict(c)
                        row.update(game_ctx)
                        rows.append(row)

                _export_ts = datetime.now(timezone.utc).isoformat()
                _client_ip = self.client_address[0] if self.client_address else "?"
                _log(f"[PW-EXPORT] client={_client_ip} since={f_since or '-'} until={f_until or '-'} source={f_source or 'all'} rows={len(rows)}")

                # Optional structured access log (pw_export_log_enabled in config)
                try:
                    _cfg = _read_json(CONFIG_FILE) or {}
                    if _cfg.get("pw_export_log_enabled"):
                        _access_path = os.path.join(SCRIPT_DIR, "pw_export_access.jsonl")
                        _entry = json.dumps({
                            "ts": _export_ts,
                            "client_ip": _client_ip,
                            "filters": {"since": f_since or None, "until": f_until or None, "source": f_source or "all", "include_suppressed": f_include_suppressed},
                            "total_calls": len(rows),
                            "live_calls": sum(1 for r in rows if not r.get("game_completed")),
                        }, separators=(",", ":"))
                        with open(_access_path, "a") as _af:
                            _af.write(_entry + "\n")
                except Exception:
                    pass

                self.send_json(200, {
                    "league": "nrl",
                    "exported_at": _export_ts,
                    "total_calls": len(rows),
                    "filters": {
                        "since": f_since or None,
                        "until": f_until or None,
                        "source": f_source or "all",
                        "include_suppressed": f_include_suppressed,
                    },
                    "calls": rows,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-scenarios":
            try:
                params = parse_qs(parsed.query)
                _sc_call_sel = (params.get("call_selection") or [""])[0].strip().lower()
                _sc_segment = (params.get("season_segment") or [""])[0].strip()
                _sc_season = (params.get("season_year") or [""])[0].strip()
                _sc_source = (params.get("source") or [""])[0].strip()
                _sc_strict = (params.get("strict_bk") or [""])[0].strip() == "1"
                import outcomes as _sc_outcomes
                history = _sc_outcomes.load_history()
                scenarios = _sc_outcomes.compute_scenario_stats(
                    history, call_selection=_sc_call_sel,
                    season_segment=_sc_segment, season_year=_sc_season,
                    source=_sc_source, strict_bk=_sc_strict)
                self.send_json(200, {"scenarios": scenarios})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/upcoming":
            try:
                config = _read_json(CONFIG_FILE) or {}
                data = build_upcoming_payload(config)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/outcomes":
            try:
                import outcomes
                config = _read_json(CONFIG_FILE)
                data = outcomes.compute_outcomes(config)
                self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Model Evaluation endpoints (#33) ──────────────────────────────

        if path == "/api/model-eval":
            params = parse_qs(parsed.query)
            try:
                import evaluate_model
                persist = _param(params, "persist", "") == "1"
                force = _param(params, "force", "") == "1"
                records = evaluate_model.load_history()
                payload = evaluate_model.evaluate_all(records, step_days=7)
                snap_result = None
                if persist:
                    snap_result = evaluate_model.save_eval_snapshot(payload)
                if snap_result:
                    payload["snapshot"] = {"persisted": True, "reason": "manual" if force else "auto"}
                self.send_json(200, payload)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/model-eval-history":
            try:
                import evaluate_model
                history = evaluate_model.load_eval_history()
                self.send_json(200, {
                    "count": len(history),
                    "history": history,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Auto-Recovery endpoint (#136) ─────────────────────────────────

        if path == "/api/auto-recovery":
            try:
                _ar_state = _read_json(os.path.join(SCRIPT_DIR, ".alert_state.json")) or {}
                _ar_config = _read_json(CONFIG_FILE) or {}
                _ar_log = _ar_state.get("auto_recovery_log", [])
                # Find last scan timestamp from recovery_checked_ keys
                _ar_last_ts = ""
                _ar_last_rnd = 0
                for _k, _v in _ar_state.items():
                    if _k.startswith("recovery_checked_") and isinstance(_v, str):
                        if _v > _ar_last_ts:
                            _ar_last_ts = _v
                            # Extract round from key: recovery_checked_YYYY_RR
                            parts = _k.split("_")
                            if len(parts) >= 3:
                                try:
                                    _ar_last_rnd = int(parts[-1])
                                except ValueError:
                                    pass
                self.send_json(200, {
                    "enabled": _ar_config.get("auto_recovery_enabled", True),
                    "pw_calls_enabled": _ar_config.get("auto_recovery_pw_calls", True),
                    "last_scan_ts": _ar_last_ts,
                    "last_scan_round": _ar_last_rnd,
                    "total_recovered": len([e for e in _ar_log if e.get("status") == "success"]),
                    "log": _ar_log[:50],
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Backup / Recovery endpoints (#16 Phase 4) ─────────────────────

        if path == "/api/snapshots":
            try:
                import runtime_backup
                backup_root = runtime_backup.DEFAULT_BACKUP_ROOT
                snapshots = []
                if os.path.isdir(backup_root):
                    for name in sorted(os.listdir(backup_root), reverse=True):
                        if name.startswith("failed-run-"):
                            continue
                        full = os.path.join(backup_root, name)
                        if not os.path.isdir(full):
                            continue
                        meta_path = os.path.join(full, "metadata.json")
                        meta = {}
                        if os.path.exists(meta_path):
                            try:
                                with open(meta_path) as f:
                                    meta = json.load(f)
                            except Exception:
                                pass
                        label = name.split("-", 1)[1] if "-" in name and not name[0].isalpha() else name
                        # Extract label from dir name: <timestamp>-<label>
                        parts = name.split("-", 1)
                        if len(parts) == 2 and parts[0].endswith("Z"):
                            label = parts[1]
                        else:
                            label = ""
                        try:
                            _fc = len([f for f in os.listdir(full) if os.path.isfile(os.path.join(full, f))])
                        except Exception:
                            _fc = 0
                        snapshots.append({
                            "dir": name,
                            "created_at": meta.get("created_at", ""),
                            "label": label,
                            "files": meta.get("copied_files", []),
                            "file_count": _fc,
                        })
                self.send_json(200, {
                    "snapshots": snapshots,
                    "runtime_files": list(runtime_backup.RUNTIME_FILES),
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/cloud-backup/history":
            try:
                last_n = int(_param(parse_qs(parsed.query), "last", "20"))
                self.send_json(200, {"runs": _get_cloud_backup_history(last_n)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/recovery/backups":
            try:
                import outcomes
                backups = outcomes.list_backups()
                wal = outcomes.wal_status()
                _cfg = _read_json(CONFIG_FILE) or {}
                _prune_days = (_cfg.get("backup_retention") or {}).get("prune_days", 60)
                self.send_json(200, {"backups": backups, "wal": wal, "config_prune_days": _prune_days})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # Static files
        if path == "/":
            path = "/dashboard.html"

        # Security: prevent path traversal
        safe_path = os.path.normpath(path.lstrip("/"))
        if safe_path.startswith(".."):
            self.send_error(403)
            return

        file_path = os.path.join(SCRIPT_DIR, safe_path)
        # Allowlist static files — block config.json and other sensitive files (#258 Phase 2.2)
        if os.path.basename(file_path) not in STATIC_ALLOWLIST:
            self.send_error(403)
            return
        if not os.path.isfile(file_path):
            self.send_error(404)
            return

        ext = os.path.splitext(file_path)[1]
        content_type = MIME.get(ext, "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if ext == ".html":
                self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", len(content))
            self.end_headers()
            self.wfile.write(content)
        except Exception:
            self.send_error(500)

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/config":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                payload = json.loads(body)
            except Exception as e:
                self.send_json(400, {"error": "invalid JSON: {}".format(e)})
                return

            err = validate_config(payload)
            if err:
                self.send_json(400, {"error": err})
                return

            # Re-merge masked secrets from current config (#258 Phase 2.5)
            current = _read_json(CONFIG_FILE) or {}
            for key in SECRET_KEYS:
                if payload.get(key) in (None, "", "***") and key in current:
                    payload[key] = current[key]

            # Pre-config-save snapshot (#16 Phase 3) — run in background
            # thread to avoid blocking the HTTP response (#290)
            snapshot_name = None
            def _bg_snapshot(p):
                try:
                    import runtime_backup
                    runtime_backup.snapshot_runtime_files(
                        label="pre-config-save")
                    _bk_ret = p.get("backup_retention", {})
                    _keep_pc = _bk_ret.get("keep_preconfig", 10)
                    runtime_backup.prune_old_snapshots(
                        keep_last=_keep_pc, label_filter="pre-config-save")
                except Exception:
                    pass
            import threading
            threading.Thread(target=_bg_snapshot, args=(payload,),
                             daemon=True).start()

            # Atomic write
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp, CONFIG_FILE)

            # Update fallback save config (#179)
            try:
                import odds_api as _oa_cfg
                _oa_cfg.configure_fallback_save(
                    payload.get("odds_api_fallback_save_enabled", False),
                    payload.get("odds_api_fallback_retention_days", 7))
            except Exception:
                pass

            resp = {"status": "saved"}
            if snapshot_name:
                resp["snapshot"] = snapshot_name
            config_warnings = validate_config_warnings(payload)
            if config_warnings:
                resp["warnings"] = config_warnings
            self.send_json(200, resp)
            return

        # ── Backup / Recovery POST endpoints (#16 Phase 4) ───────────

        if parsed.path == "/api/cloud-backup/run":
            try:
                if _cloud_backup_proc and _cloud_backup_proc.poll() is None:
                    self.send_json(409, {"ok": False, "error": "Cloud backup already running"})
                    return
                script = os.path.join(SCRIPT_DIR, "cloud_backup.sh")
                if not os.path.isfile(script):
                    self.send_json(404, {"ok": False, "error": "cloud_backup.sh not found"})
                    return
                import subprocess
                _set_cloud_backup_proc(subprocess.Popen(
                    ["/usr/bin/env", "bash", script],
                    cwd=SCRIPT_DIR,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ))
                self.send_json(200, {"ok": True, "status": "started"})
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})
            return

        if parsed.path == "/api/snapshots/create":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                label = body.get("label", "manual")
                import runtime_backup
                snap_dir, meta = runtime_backup.snapshot_runtime_files(label=label)
                self.send_json(200, {
                    "ok": True,
                    "snapshot_dir": os.path.basename(snap_dir),
                    "copied_files": meta.get("copied_files", []),
                    "created_at": meta.get("created_at", ""),
                })
            except Exception as e:
                self.send_json(500, {"ok": False, "error": str(e)})
            return

        if parsed.path == "/api/recovery/restore":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                import runtime_backup
                raw_name = body.get("backup", "")
                replay = body.get("replay_wal", True)
                backup_name, _err = _safe_backup_name(raw_name, runtime_backup.DEFAULT_BACKUP_ROOT)
                if _err:
                    self.send_json(400, {"success": False, "error": _err})
                    return
                import outcomes
                result = outcomes.restore_from_backup(backup_name, replay_wal_entries=replay)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"success": False, "error": str(e)})
            return

        if parsed.path == "/api/recovery/delete-backup":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                # Support single (backup) or batch (backups) delete (#87)
                names = body.get("backups") or []
                if not names and body.get("backup"):
                    names = [body["backup"]]
                if not names:
                    self.send_json(400, {"success": False, "error": "backup name(s) required"})
                    return
                import runtime_backup, shutil
                deleted = []
                errors = []
                for name in names:
                    name, _err = _safe_backup_name(name, runtime_backup.DEFAULT_BACKUP_ROOT)
                    if _err:
                        errors.append("{}: {}".format(name or "?", _err))
                        continue
                    backup_dir = os.path.join(runtime_backup.DEFAULT_BACKUP_ROOT, name)
                    if os.path.isdir(backup_dir):
                        try:
                            shutil.rmtree(backup_dir)
                            deleted.append(name)
                        except Exception as e:
                            errors.append("{}: {}".format(name, e))
                    else:
                        errors.append("{}: not found".format(name))
                self.send_json(200, {"success": len(deleted) > 0, "deleted": deleted, "errors": errors})
            except Exception as e:
                self.send_json(500, {"success": False, "error": str(e)})
            return

        if parsed.path == "/api/recovery/prune-backups":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                days = int(body.get("retention_days", 60))
                if days < 1:
                    self.send_json(400, {"success": False, "error": "retention_days must be >= 1"})
                    return
                import runtime_backup
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                cutoff_str = cutoff.isoformat()
                backup_root = runtime_backup.DEFAULT_BACKUP_ROOT
                if not os.path.isdir(backup_root):
                    self.send_json(200, {"success": True, "pruned": [], "kept": 0})
                    return
                pruned = []
                kept = 0
                for name in os.listdir(backup_root):
                    if name.startswith("failed-run-"):
                        continue
                    full = os.path.join(backup_root, name)
                    if not os.path.isdir(full):
                        continue
                    # Get creation time from metadata or dir mtime
                    created = None
                    meta_path = os.path.join(full, "metadata.json")
                    if os.path.exists(meta_path):
                        try:
                            with open(meta_path) as f:
                                meta = json.load(f)
                            created = meta.get("created_at")
                        except Exception:
                            pass
                    if not created:
                        created = datetime.fromtimestamp(
                            os.path.getmtime(full), tz=timezone.utc
                        ).isoformat()
                    if created < cutoff_str:
                        import shutil
                        shutil.rmtree(full)
                        pruned.append(name)
                    else:
                        kept += 1
                self.send_json(200, {"success": True, "pruned": pruned, "kept": kept})
            except Exception as e:
                self.send_json(500, {"success": False, "error": str(e)})
            return

        if parsed.path == "/api/ml-train":
            if _ml_train_state["status"] == "running":
                self.send_json(409, {"error": "Training already in progress", "train_state": dict(_ml_train_state)})
                return
            t = threading.Thread(target=_do_ml_train_background, daemon=True)
            t.start()
            self.send_json(202, {"status": "started", "train_state": dict(_ml_train_state)})
            return

        if parsed.path == "/api/generate-roi-report":
            try:
                import pw_roi_report
                pw_roi_report.generate_report(copy_to_nba=True)
                self.send_json(200, {"ok": True, "status": "generated", "path": "pw_roi_report.html"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if parsed.path == "/api/generate-bk-odds-report":
            try:
                import importlib
                import analyse_bk_odds
                importlib.reload(analyse_bk_odds)
                analyse_bk_odds.generate_report()
                self.send_json(200, {"ok": True, "status": "generated", "path": "bk_odds_analysis_report.html"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if parsed.path == "/api/test-pw-alert":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                source = body.get("source", "sample")
                game_id = str(body.get("game_id", "")).strip()

                cfg = _read_json(CONFIG_FILE) or {}
                from monitor import send_alert
                now = datetime.now(timezone.utc)
                ts = now.strftime("%b %d %I:%M %p AEST")

                msg = None

                if source == "history":
                    records = _load_history_cached()
                    pw_games = [r for r in records if r.get("predicted_winner_calls")]
                    if not pw_games:
                        self.send_json(400, {"error": "No games with PW calls in history"})
                        return
                    if game_id:
                        match = [r for r in pw_games if game_id in str(r.get("match_id", ""))]
                        if not match:
                            self.send_json(400, {"error": "Game {} not found or has no PW calls".format(game_id)})
                            return
                        rec = match[0]
                    else:
                        import random
                        rec = random.choice(pw_games)
                    calls = rec["predicted_winner_calls"]
                    c = calls[0]
                    game_label = "{} vs {}".format(rec.get("home_team", "Home"), rec.get("away_team", "Away"))
                    stats_parts = []
                    if c.get("away_score_at_fire") is not None and c.get("home_score_at_fire") is not None:
                        stats_parts.append("Score: {}-{}".format(c["home_score_at_fire"], c["away_score_at_fire"]))
                    if c.get("score_margin_at_fire") is not None:
                        stats_parts.append("Margin: {:+d}".format(c["score_margin_at_fire"]))
                    if c.get("spread_role"):
                        stats_parts.append(c["spread_role"])
                    if c.get("bk_moneyline"):
                        stats_parts.append("BK Odds: {}".format(c["bk_moneyline"]))
                    if c.get("polarity_net") is not None:
                        stats_parts.append("Pol: {:+d} ({}+ {}\u2212)".format(
                            c["polarity_net"], c.get("polarity_pos", 0), c.get("polarity_neg", 0)))
                    if c.get("avg_edge") is not None:
                        stats_parts.append("Edge: {:+.1f}".format(c["avg_edge"]))
                    stats_line = " \u00b7 ".join(stats_parts)
                    blend_tag = " \u00b7 ML+Hist" if c.get("blended") else ""
                    consensus_tag = " \u00b7 consensus: {}".format(
                        c["consensus"].replace("_", " ")) if c.get("consensus") else ""
                    q_label = c.get("quarter", c.get("half", "?"))
                    msg = (
                        "_{ts}_\n"
                        "\U0001f3c9 :dart: *Predicted Winner* \u2014 {pred}\n"
                        "{pct}% win probability{blended}{consensus}\n"
                        "Game: {game} \u00b7 {quarter}\n"
                        "{stats}\n"
                        "\u2014 _This is a test alert (from history)_"
                    ).format(ts=ts, pred=c.get("predicted_team", "?"),
                             pct="{:.0f}".format(c.get("pct", 0)),
                             blended=blend_tag, consensus=consensus_tag,
                             game=game_label, quarter=q_label,
                             stats=stats_line)

                elif source == "upcoming":
                    upcoming = build_upcoming_payload(cfg)
                    games = upcoming.get("games", [])
                    if not games:
                        self.send_json(400, {"error": "No upcoming games found"})
                        return
                    if game_id:
                        match = [g for g in games if game_id in str(g.get("match_id", ""))]
                        if not match:
                            self.send_json(400, {"error": "Upcoming game {} not found".format(game_id)})
                            return
                        ug = match[0]
                    else:
                        import random
                        ug = random.choice(games)
                    game_label = "{} vs {}".format(ug.get("home_team", "Home"), ug.get("away_team", "Away"))
                    pred_team = ug.get("home_team", "Home")
                    msg = (
                        "_{ts}_\n"
                        "\U0001f3c9 :dart: *Predicted Winner* \u2014 {pred}\n"
                        "78% win probability \u00b7 ML+Hist \u00b7 consensus: strong\n"
                        "Game: {game} \u00b7 2H 55:00\n"
                        "Score: 18-12 \u00b7 Margin: +6 \u00b7 favorite \u00b7 Pol: +3 (4+ 1\u2212) \u00b7 Edge: +5.2\n"
                        "\u2014 _This is a test alert (upcoming)_"
                    ).format(ts=ts, pred=pred_team, game=game_label)

                else:  # sample
                    msg = (
                        "_{ts}_\n"
                        "\U0001f3c9 :dart: *Predicted Winner* \u2014 Storm\n"
                        "78% win probability \u00b7 ML+Hist \u00b7 consensus: strong\n"
                        "Game: Storm vs Roosters \u00b7 2H 55:00\n"
                        "Score: 18-12 \u00b7 Margin: +6 \u00b7 favorite \u00b7 Pol: +3 (4+ 1\u2212) \u00b7 Edge: +5.2\n"
                        "\u2014 _This is a test alert_"
                    ).format(ts=ts)

                ok = send_alert(cfg, msg)
                if ok:
                    self.send_json(200, {"ok": True, "message": "Test PW alert sent", "source": source})
                else:
                    self.send_json(500, {"ok": False, "error": "Send failed"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if parsed.path == "/api/odds-monitor/start":
            try:
                config = _read_json(CONFIG_FILE) or {}
                if odds_monitor.is_running():
                    self.send_json(409, {"error": "Already running"})
                else:
                    odds_monitor.start(config, logger=_log)
                    self.send_json(200, {"status": "started"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if parsed.path == "/api/odds-monitor/stop":
            try:
                if not odds_monitor.is_running():
                    self.send_json(409, {"error": "Not running"})
                else:
                    odds_monitor.stop(logger=_log)
                    self.send_json(200, {"status": "stopped"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Auto-Recovery toggle (#136) ──────────────────────────────────
        if parsed.path == "/api/auto-recovery/toggle":
            try:
                body = json.loads(self.rfile.read(
                    int(self.headers.get("Content-Length", 0))))
                config = _read_json(CONFIG_FILE) or {}
                key = body.get("key", "auto_recovery_enabled")
                if key not in ("auto_recovery_enabled", "auto_recovery_pw_calls"):
                    self.send_json(400, {"error": "Invalid key"})
                    return
                config[key] = not config.get(key, True)
                tmp = CONFIG_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(config, f, indent=2)
                os.replace(tmp, CONFIG_FILE)
                self.send_json(200, {"key": key, "value": config[key]})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_error(404)



# ── Model Evaluation auto-snapshot background thread (#166) ──────────

_eval_stop = threading.Event()


def _eval_snapshot_loop():
    """Background thread that auto-saves model evaluation snapshots on schedule."""
    while not _eval_stop.is_set():
        try:
            config = _read_json(CONFIG_FILE) or {}
            enabled = config.get("model_eval_snapshot_enabled", False)
            interval_min = config.get("model_eval_snapshot_interval", 360)
            if not enabled:
                _eval_stop.wait(300)  # check config every 5 min when disabled
                continue

            import evaluate_model
            records = evaluate_model.load_history()
            payload = evaluate_model.evaluate_all(records, step_days=7)
            evaluate_model.save_eval_snapshot(payload)
            _log("[MODEL-EVAL] Auto-snapshot saved ({} records)".format(
                payload.get("records", 0)))
        except Exception as e:
            _log("[MODEL-EVAL] Auto-snapshot error:", e)

        # Sleep for configured interval (in minutes), checking stop flag
        _eval_stop.wait(max(interval_min, 15) * 60)


def main():
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)  # #258 Phase 2.4 — loopback only
    _log("NRL Monitor dashboard listening on port", PORT)

    # Start odds monitor background thread
    try:
        config = _read_json(CONFIG_FILE) or {}
        if config.get("odds_api_enabled") and config.get("odds_api_key"):
            odds_monitor.start(config, logger=_log)
            _log("Odds monitor started")
    except Exception as e:
        _log("WARN: Could not start odds monitor:", e)

    # Configure fallback data saving (#179)
    # configure_fallback_save is synchronous (fast, needed before server starts so log writes work)
    # prune_fallback_data is moved to a background thread — it can block on large files (#293)
    try:
        import odds_api as _oa_init
        _oa_init.configure_fallback_save(
            config.get("odds_api_fallback_save_enabled", False),
            config.get("odds_api_fallback_retention_days", 7))
    except Exception as e:
        _log("WARN: Could not configure fallback save:", e)

    def _prune_odds_logs():
        try:
            pruned = _oa_init.prune_fallback_data()
            if pruned:
                _log("[ODDS_API] Pruned {} old fallback data entries".format(pruned))
        except Exception as _pe:
            _log("WARN: Could not prune fallback data:", _pe)
    threading.Thread(target=_prune_odds_logs, name="odds-log-prune", daemon=True).start()

    # Start model eval auto-snapshot thread (#166)
    _eval_thread = threading.Thread(target=_eval_snapshot_loop, daemon=True)
    _eval_thread.start()

    # Start log rotation worker (runs 24/7, fires at configured hour)
    _rotation_thread = threading.Thread(target=_log_rotation_worker, name="log-rotation", daemon=True)
    _rotation_thread.start()

    # Memory watchdog — auto-restart when RSS exceeds threshold (#423)
    # Raised from 512 → 768 MB: matches NBA fix; gives headroom for cache warming (#293)
    _RSS_LIMIT_MB = 768
    def _memory_watchdog():
        import resource
        while True:
            time.sleep(60)
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            if rss_mb > _RSS_LIMIT_MB:
                _log("[MEMORY_WATCHDOG] RSS={:.0f}MB exceeds {}MB — restarting".format(rss_mb, _RSS_LIMIT_MB))
                os._exit(1)
    _mem_worker = threading.Thread(target=_memory_watchdog, name="memory-watchdog", daemon=True)
    _mem_worker.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("Shutting down")
        _eval_stop.set()
        odds_monitor.stop(logger=_log)
        server.shutdown()


if __name__ == "__main__":
    main()
