"""odds_monitor.py — Background odds monitoring for last 10 min + ET of NRL games.

Polls The Odds API every 60s during the final phase of live games, capturing
odds + NRL score/try data per snapshot. Computes +EV indicators by comparing
bookmaker implied probability against historical late-tries probability.

Designed to run as a background thread in server.py.
"""

import json
import os
import re
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "odds_monitor_history.json")
LIVE_FILE = os.path.join(SCRIPT_DIR, "odds_monitor_live.jsonl")
GAME_HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
NRL_CACHE_DIR = os.path.join(SCRIPT_DIR, "nrl_cache")

# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL = 60          # seconds between polls (#71)
ACTIVATE_MINUTE = 65        # default game minute to start monitoring (#90)
GAME_SECS_TOTAL = 80 * 60   # 80 min regulation
ROUND_CACHE_TTL = 300       # cache current round for 5 minutes
GAME_WINDOW_UTC = (4, 13)   # NRL game window: UTC 04:00-13:00 (AEST 14:00-23:00)

# ── In-memory state ───────────────────────────────────────────────────────────

_lock = threading.Lock()
_state = {
    "running": False,
    "thread": None,
    "config": {},          # updated via update_config() without restart
    "snapshots": {},       # match_id -> list of snapshot dicts (live, in-memory)
    "game_info": {},       # match_id -> {home_team, away_team, ...} metadata
}

_round_cache = {"round": None, "ts": 0}


# ── Historical late-tries probabilities ───────────────────────────────────────

_late_tries_cache = {"data": None, "ts": 0}
_leading_tries_cache = {"data": None, "ts": 0}
_LATE_TRIES_TTL = 3600  # refresh every hour
DEFAULT_LEADING_THRESHOLD = 3.0  # minimum spread price for leading team strategy


def _compute_late_tries_probs():
    """Compute probability of 1+/2+/3+ tries in remaining N minutes from game_history."""
    try:
        if not os.path.exists(GAME_HISTORY_FILE):
            return {}
        with open(GAME_HISTORY_FILE, "r") as f:
            records = json.load(f)
    except Exception:
        return {}

    total = len(records)
    if not total:
        return {}

    # For each remaining_minutes (1-15), compute probability of 1+/2+/3+ tries
    probs = {}
    for remaining in range(1, 16):
        cutoff = GAME_SECS_TOTAL - (remaining * 60)
        g1 = g2 = g3 = 0
        for r in records:
            tries = r.get("try_events", [])
            late = [t for t in tries if t.get("game_seconds", 0) >= cutoff]
            nl = len(late)
            if nl >= 1:
                g1 += 1
            if nl >= 2:
                g2 += 1
            if nl >= 3:
                g3 += 1
        # Exact counts: 0, exactly 1, exactly 2, exactly 3+ (#174)
        g0 = total - g1
        e1 = g1 - g2  # exactly 1
        e2 = g2 - g3  # exactly 2
        probs[remaining] = {
            "total_games": total,
            "prob_1plus": round(g1 / total, 4),
            "prob_2plus": round(g2 / total, 4),
            "prob_3plus": round(g3 / total, 4),
            "games_1plus": g1,
            "games_2plus": g2,
            "games_3plus": g3,
            "prob_0": round(g0 / total, 4),
            "prob_exactly_1": round(e1 / total, 4),
            "prob_exactly_2": round(e2 / total, 4),
            "prob_exactly_3plus": round(g3 / total, 4),
        }

    return probs


def get_late_tries_probs():
    """Get cached late-tries probabilities, refreshing if stale."""
    now = time.time()
    if _late_tries_cache["data"] is None or (now - _late_tries_cache["ts"]) > _LATE_TRIES_TTL:
        _late_tries_cache["data"] = _compute_late_tries_probs()
        _late_tries_cache["ts"] = now
    return _late_tries_cache["data"] or {}


# ── Leading team late-tries probabilities ─────────────────────────────────────

def _compute_leading_team_tries_probs():
    """Compute probability that the leading team scores 1+ try in remaining N minutes.

    Uses score_progression to determine who leads at the cutoff point,
    then checks try_events for that team after the cutoff.
    """
    try:
        if not os.path.exists(GAME_HISTORY_FILE):
            return {}
        with open(GAME_HISTORY_FILE, "r") as f:
            records = json.load(f)
    except Exception:
        return {}

    if not records:
        return {}

    probs = {}
    for remaining in range(1, 16):
        cutoff = GAME_SECS_TOTAL - (remaining * 60)
        qualified = 0  # games where one team was leading at cutoff
        scored = 0     # leading team scored 1+ try after cutoff

        for r in records:
            prog = r.get("score_progression", [])
            if not prog:
                continue

            # Find score at cutoff — last event at or before cutoff
            home_at_cutoff = 0
            away_at_cutoff = 0
            for ev in prog:
                gs = ev.get("gameSeconds", 0)
                if gs <= cutoff:
                    home_at_cutoff = ev.get("homeScore", 0)
                    away_at_cutoff = ev.get("awayScore", 0)
                else:
                    break

            if home_at_cutoff == away_at_cutoff:
                continue  # tied — no leading team

            leading_team = r.get("home_team") if home_at_cutoff > away_at_cutoff else r.get("away_team")
            qualified += 1

            # Check if leading team scored a try after cutoff
            try_events = r.get("try_events", [])
            for t in try_events:
                if t.get("game_seconds", 0) >= cutoff and t.get("team") == leading_team:
                    scored += 1
                    break

        if qualified:
            probs[remaining] = {
                "qualified_games": qualified,
                "scored": scored,
                "prob": round(scored / qualified, 4),
            }

    return probs


def get_leading_team_tries_probs():
    """Get cached leading-team-tries probabilities, refreshing if stale."""
    now = time.time()
    if _leading_tries_cache["data"] is None or (now - _leading_tries_cache["ts"]) > _LATE_TRIES_TTL:
        _leading_tries_cache["data"] = _compute_leading_team_tries_probs()
        _leading_tries_cache["ts"] = now
    return _leading_tries_cache["data"] or {}


# ── Snapshot creation ─────────────────────────────────────────────────────────

def _compute_leading_team_ev(odds_data, nrl_data, game_minute, config):
    """Compute leading team EV analysis for a snapshot.

    Returns dict with leading team info, spread price qualification,
    and EV edge vs historical probability.
    """
    home_score = nrl_data.get("home_score", 0)
    away_score = nrl_data.get("away_score", 0)

    if home_score == away_score:
        return {"leading_team": None, "qualifies": False}

    if home_score > away_score:
        leading_team = nrl_data.get("home_team", "")
        leading_side = "home"
        margin = home_score - away_score
        spread_price = odds_data.get("home_spread_price") if odds_data else None
        spread_line = odds_data.get("home_spread") if odds_data else None
    else:
        leading_team = nrl_data.get("away_team", "")
        leading_side = "away"
        margin = away_score - home_score
        spread_price = odds_data.get("away_spread_price") if odds_data else None
        spread_line = odds_data.get("away_spread") if odds_data else None

    threshold = config.get("odds_monitor_leading_threshold", DEFAULT_LEADING_THRESHOLD)
    qualifies = spread_price is not None and spread_price >= threshold

    remaining_minutes = max(1, 80 - game_minute)
    if remaining_minutes > 15:
        remaining_minutes = 15

    probs = get_leading_team_tries_probs()
    window = probs.get(remaining_minutes, {})
    hist_prob = window.get("prob", 0)

    # EV edge: hist probability of leading team scoring - implied prob from spread price
    implied_prob = round(1 / spread_price, 4) if spread_price and spread_price > 0 else None
    ev_edge = round(hist_prob - implied_prob, 4) if implied_prob is not None else None

    return {
        "leading_team": leading_team,
        "leading_side": leading_side,
        "margin": margin,
        "spread_price": spread_price,
        "spread_line": spread_line,
        "qualifies": qualifies,
        "hist_prob_leading_tries": hist_prob,
        "hist_qualified_games": window.get("qualified_games", 0),
        "implied_prob": implied_prob,
        "ev_edge": ev_edge,
        "remaining_minutes": remaining_minutes,
    }


def _compute_trailing_team_ev(odds_data, nrl_data, game_minute, config):
    """Compute trailing team EV analysis for a snapshot.

    Mirrors _compute_leading_team_ev() but for the losing team.
    Returns dict with trailing team info, spread price qualification,
    and EV edge vs historical late-tries probability.
    """
    home_score = nrl_data.get("home_score", 0)
    away_score = nrl_data.get("away_score", 0)

    if home_score == away_score:
        return {"trailing_team": None, "qualifies": False}

    if home_score < away_score:
        # home is trailing
        trailing_team = nrl_data.get("home_team", "")
        trailing_side = "home"
        margin = away_score - home_score
        spread_price = odds_data.get("home_spread_price") if odds_data else None
        spread_line = odds_data.get("home_spread") if odds_data else None
    else:
        # away is trailing
        trailing_team = nrl_data.get("away_team", "")
        trailing_side = "away"
        margin = home_score - away_score
        spread_price = odds_data.get("away_spread_price") if odds_data else None
        spread_line = odds_data.get("away_spread") if odds_data else None

    threshold = config.get(
        "odds_monitor_trailing_threshold",
        config.get("odds_monitor_leading_threshold", DEFAULT_LEADING_THRESHOLD),
    )
    qualifies = spread_price is not None and spread_price >= threshold

    remaining_minutes = max(1, 80 - game_minute)
    if remaining_minutes > 15:
        remaining_minutes = 15

    probs = get_late_tries_probs()
    window = probs.get(remaining_minutes, {})
    hist_prob = window.get("prob_1plus", 0)

    implied_prob = round(1 / spread_price, 4) if spread_price and spread_price > 0 else None
    ev_edge = round(hist_prob - implied_prob, 4) if implied_prob is not None else None

    return {
        "trailing_team": trailing_team,
        "trailing_side": trailing_side,
        "margin": margin,
        "spread_price": spread_price,
        "spread_line": spread_line,
        "qualifies": qualifies,
        "hist_prob_1plus_tries": hist_prob,
        "hist_total_games": window.get("total_games", 0),
        "implied_prob": implied_prob,
        "ev_edge": ev_edge,
        "remaining_minutes": remaining_minutes,
    }


def _compute_ev(odds_data, game_minute):
    """Compute +EV analysis for a snapshot.

    Compares bookmaker implied over/under probability against our
    historical late-tries probability for the remaining time.
    """
    remaining_minutes = max(1, 80 - game_minute)
    if remaining_minutes > 15:
        remaining_minutes = 15  # cap lookup at 15

    probs = get_late_tries_probs()
    window = probs.get(remaining_minutes, {})

    total_over_price = odds_data.get("total_over_price")
    total_under_price = odds_data.get("total_under_price")
    total_line = odds_data.get("total_over") or odds_data.get("total_under")

    implied_over = round(1 / total_over_price, 4) if total_over_price and total_over_price > 0 else None
    implied_under = round(1 / total_under_price, 4) if total_under_price and total_under_price > 0 else None

    hist_1plus = window.get("prob_1plus", 0)
    hist_2plus = window.get("prob_2plus", 0)
    hist_3plus = window.get("prob_3plus", 0)

    ev = {
        "remaining_minutes": remaining_minutes,
        "total_line": total_line,
        "implied_over_prob": implied_over,
        "implied_under_prob": implied_under,
        "hist_prob_1plus_tries": hist_1plus,
        "hist_prob_2plus_tries": hist_2plus,
        "hist_prob_3plus_tries": hist_3plus,
        "hist_total_games": window.get("total_games", 0),
    }

    # EV edge: if we know more tries are likely, over may be +EV
    if implied_over is not None:
        ev["ev_edge_1plus"] = round(hist_1plus - implied_over, 4)
    if implied_under is not None:
        ev["ev_edge_under"] = round((1 - hist_1plus) - implied_under, 4)

    return ev


def create_snapshot(match_id, game_info, odds_data, nrl_data, config=None):
    """Create a single odds monitor snapshot.

    Args:
        match_id: NRL match identifier
        game_info: dict with home_team, away_team
        odds_data: extracted odds from odds_api.extract_odds()
        nrl_data: dict with score, period, clock, game_minute, try_events
        config: runtime config dict (for leading threshold)
    """
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    game_minute = nrl_data.get("game_minute", 0)
    home_score = nrl_data.get("home_score", 0)
    away_score = nrl_data.get("away_score", 0)

    # Count tries that happened in the monitoring window
    activate_min = nrl_data.get("activate_minute", ACTIVATE_MINUTE)
    window_cutoff = activate_min * 60
    all_tries = nrl_data.get("try_events", [])
    tries_in_window = [t for t in all_tries if t.get("game_seconds", 0) >= window_cutoff]

    # Leading team EV analysis (#211)
    nrl_data_with_teams = dict(nrl_data)
    nrl_data_with_teams["home_team"] = game_info.get("home_team", "")
    nrl_data_with_teams["away_team"] = game_info.get("away_team", "")
    leading_ev = _compute_leading_team_ev(odds_data or {}, nrl_data_with_teams, game_minute, config or {})
    trailing_ev = _compute_trailing_team_ev(odds_data or {}, nrl_data_with_teams, game_minute, config or {})

    snapshot = {
        "ts": ts,
        "match_id": match_id,
        "home_team": game_info.get("home_team", ""),
        "away_team": game_info.get("away_team", ""),
        "game_minute": game_minute,
        "period": nrl_data.get("period", ""),
        "home_score": home_score,
        "away_score": away_score,
        "total_points": home_score + away_score,
        "tries_in_window": len(tries_in_window),
        "try_events_in_window": tries_in_window,
        "odds": {
            "total_line": odds_data.get("total_over") or odds_data.get("total_under") if odds_data else None,
            "total_over_price": odds_data.get("total_over_price") if odds_data else None,
            "total_under_price": odds_data.get("total_under_price") if odds_data else None,
            "home_ml": odds_data.get("home_ml") if odds_data else None,
            "away_ml": odds_data.get("away_ml") if odds_data else None,
            "home_spread": odds_data.get("home_spread") if odds_data else None,
            "away_spread": odds_data.get("away_spread") if odds_data else None,
            "home_spread_price": odds_data.get("home_spread_price") if odds_data else None,
            "away_spread_price": odds_data.get("away_spread_price") if odds_data else None,
            "bookmaker": odds_data.get("bookmaker") if odds_data else None,
            "ml_source": odds_data.get("ml_source") if odds_data else None,
            "spread_source": odds_data.get("spread_source") if odds_data else None,
            "totals_source": odds_data.get("totals_source") if odds_data else None,
        },
        "ev_analysis": _compute_ev(odds_data or {}, game_minute),
        "leading_team_ev": leading_ev,
        "trailing_team_ev": trailing_ev,
    }

    return snapshot


# ── Background polling loop ──────────────────────────────────────────────────

def _poll_loop(logger=None):
    """Main polling loop — runs in background thread."""
    try:
        import odds_api as odds_mod
        import nrl_api
    except ImportError as e:
        if logger:
            logger("[ODDS_MONITOR] Import error: {}".format(e))
        return

    if logger:
        logger("[ODDS_MONITOR] Background thread started")

    # Recover snapshots from previous session (#111)
    _recover_live_snapshots(logger=logger)

    idle_polls = 0
    MAX_IDLE_POLLS = 5  # auto-stop after 5 consecutive polls with no active games

    while True:
        with _lock:
            if not _state["running"]:
                break
            config = dict(_state["config"])  # read fresh config each iteration (#129)

        api_key = config.get("odds_api_key", "")
        season = config.get("season", 2026)

        try:
            had_active = _poll_once(config, api_key, season, odds_mod, nrl_api, logger)
            if had_active:
                idle_polls = 0
            else:
                idle_polls += 1
                if idle_polls >= MAX_IDLE_POLLS and _state.get("snapshots"):
                    # Had games before but none now — persist remaining + auto-stop
                    with _lock:
                        remaining = list(_state["snapshots"].keys())
                    for rid in remaining:
                        persist_game(rid)
                        clear_game(rid)
                        if logger:
                            logger("[ODDS_MONITOR] Auto-stop persist: {}".format(rid[:40]))
                    _clear_live_file()  # (#111)
                    if logger:
                        logger("[ODDS_MONITOR] No active games for {} polls — auto-stopping".format(idle_polls))
                    with _lock:
                        _state["running"] = False
                    break
        except Exception as e:
            if logger:
                logger("[ODDS_MONITOR] Poll error: {}".format(e))

        # Sleep in small increments so we can stop quickly
        for _ in range(POLL_INTERVAL):
            time.sleep(1)
            with _lock:
                if not _state["running"]:
                    break

    if logger:
        logger("[ODDS_MONITOR] Background thread stopped")


def _poll_once(config, api_key, season, odds_mod, nrl_api, logger=None):
    """Single poll iteration: fetch scoreboard, check game phases, capture snapshots.

    Returns True if active games were found, False otherwise.
    """
    # Skip outside game window UNLESS actively monitoring games (#129)
    # Use scheduler config hours if available, else fallback to GAME_WINDOW_UTC
    _win_start = config.get("scheduler_start_hour", GAME_WINDOW_UTC[0])
    _win_stop = config.get("scheduler_stop_hour", GAME_WINDOW_UTC[1])
    utc_hour = int(time.strftime("%H", time.gmtime()))
    with _lock:
        has_active_snapshots = bool(_state["snapshots"])
    if not has_active_snapshots and (utc_hour < _win_start or utc_hour >= _win_stop):
        return False

    # Get current round — cached for 5 minutes to avoid repeated API scans
    round_number = config.get("current_round")
    if not round_number:
        now = time.time()
        if _round_cache["round"] and (now - _round_cache["ts"]) < ROUND_CACHE_TTL:
            round_number = _round_cache["round"]
        else:
            try:
                round_number, _ = nrl_api.get_current_round_fixtures(season)
                _round_cache["round"] = round_number
                _round_cache["ts"] = now
            except Exception:
                return False
    if not round_number:
        return False

    # Fetch scoreboard to find live games
    scoreboard = nrl_api.get_live_scoreboard(season, round_number)
    if not scoreboard:
        return False

    games = list(scoreboard.get("games", []))

    # Merge SOO fixtures (#104)
    try:
        soo_fixtures = nrl_api.get_soo_fixtures(season)
        for fix in soo_fixtures:
            state = fix.get("matchState", "")
            is_live = (state in nrl_api.LIVE_STATES or fix.get("matchMode") == "Live") and state not in nrl_api.COMPLETED_STATES
            is_completed = state in nrl_api.COMPLETED_STATES
            if not is_live and not is_completed:
                continue
            home_team = fix.get("homeTeam", {})
            away_team = fix.get("awayTeam", {})
            clock_data = fix.get("clock", {})
            games.append({
                "match_id": fix.get("matchId") or fix.get("matchCentreUrl", ""),
                "match_centre_url": fix.get("matchCentreUrl", ""),
                "is_live": is_live,
                "is_completed": is_completed,
                "clock": clock_data.get("gameTime", ""),
                "period": nrl_api._get_period(fix),
                "home_team": home_team.get("nickName", ""),
                "away_team": away_team.get("nickName", ""),
                "home_score": home_team.get("score", 0),
                "away_score": away_team.get("score", 0),
                "venue": fix.get("venue", ""),
                "competition": "state_of_origin",
                "soo_game": fix.get("_soo_game", 0),
                "round_title": fix.get("roundTitle", ""),
            })
    except Exception:
        pass

    active_games = []

    for game in games:
        if not game.get("is_live"):
            continue

        match_id = game.get("match_id", "")
        period = game.get("period", "")
        game_minute = 0

        # Parse game minute
        clock = game.get("clock", "")
        if clock:
            try:
                parts = clock.replace("'", "").split(":")
                game_minute = int(parts[0])
            except (ValueError, IndexError):
                pass

        if period == "2H" and game_minute < 40:
            game_minute = max(game_minute, 40)
        if period in ("ET", "GoldenPoint"):
            game_minute = max(game_minute, 80)

        # Only monitor from activate_minute onward and ET
        _act_min = config.get("odds_monitor_activate_minute", ACTIVATE_MINUTE)
        if game_minute < _act_min and period not in ("ET", "GoldenPoint"):
            continue

        active_games.append((game, match_id, game_minute, period))

    # Persist games that were monitored but are no longer active (#78)
    active_ids = set(mid for _, mid, _, _ in active_games)
    with _lock:
        monitored_ids = set(_state["snapshots"].keys())
    ended_ids = monitored_ids - active_ids
    for eid in ended_ids:
        # Find final score from scoreboard
        final = {}
        for game in games:
            if game.get("match_id") == eid and game.get("is_completed"):
                hs = game.get("home_score", 0)
                aws = game.get("away_score", 0)
                final = {"home_score": hs, "away_score": aws, "total": hs + aws}
                break
        with _lock:
            snap_count = len(_state["snapshots"].get(eid, []))
        persist_game(eid, final_result=final if final else None)
        clear_game(eid)
        # Clear live file if no more games being monitored (#111)
        with _lock:
            if not _state["snapshots"]:
                _clear_live_file()
        if logger:
            logger("[ODDS_MONITOR] Game ended — persisted {} ({} snapshots)".format(
                eid[:40], snap_count))

    if not active_games:
        return False

    # Fetch odds — separate calls for Premiership and SOO (#104)
    events = None
    soo_events = None
    has_soo = any(g.get("competition") == "state_of_origin" for g, _, _, _ in active_games)
    if api_key:
        events = odds_mod.fetch_odds_monitor_data(api_key, logger=logger)
        if has_soo:
            try:
                soo_events = odds_mod._fetch_all_events(api_key, context="odds_monitor_soo",
                                                         sport_key=odds_mod.SPORT_KEY_SOO)
            except Exception:
                pass

    for game, match_id, game_minute, period in active_games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Get detailed match data for try events
        match_url = game.get("match_centre_url", "")
        try_events = []
        if match_url:
            try:
                match_data = nrl_api.get_match_data(match_url)
                if match_data:
                    timeline = match_data.get("timeline", [])
                    for ev in timeline:
                        if ev.get("type") == "Try":
                            try_events.append({
                                "game_seconds": ev.get("gameSeconds", 0),
                                "team": ev.get("team", ""),
                                "player": ev.get("description", ""),
                            })
            except Exception:
                pass

        # Extract odds for this game — use SOO events for SOO games (#104)
        odds_data = None
        _use_events = soo_events if game.get("competition") == "state_of_origin" and soo_events else events
        if _use_events:
            event = odds_mod._match_event(_use_events, home, away)
            if event:
                odds_data = odds_mod.extract_odds(event)

        nrl_data = {
            "home_score": game.get("home_score", 0),
            "away_score": game.get("away_score", 0),
            "period": period,
            "clock": game.get("clock", ""),
            "game_minute": game_minute,
            "try_events": try_events,
            "activate_minute": _act_min,
        }

        game_info = {
            "home_team": home, "away_team": away,
            "competition": game.get("competition", "nrl_premiership"),
            "soo_game": game.get("soo_game", 0),
            "round_title": game.get("round_title", ""),
        }
        snapshot = create_snapshot(match_id, game_info, odds_data, nrl_data, config=config)

        with _lock:
            _state["game_info"][match_id] = game_info
            if match_id not in _state["snapshots"]:
                _state["snapshots"][match_id] = []
            _state["snapshots"][match_id].append(snapshot)

        # Persist to live JSONL for crash recovery (#111)
        _append_live_snapshot(match_id, snapshot, game_info)

        if logger:
            total_line = snapshot["odds"].get("total_line", "?")
            logger("[ODDS_MONITOR] {} vs {}: min={}, score={}-{}, total_line={}, tries_in_window={}, bk={}(ml)/{}(spr)/{}(tot)".format(
                home, away, game_minute,
                snapshot["home_score"], snapshot["away_score"],
                total_line, snapshot["tries_in_window"],
                snapshot["odds"].get("ml_source", "?"),
                snapshot["odds"].get("spread_source", "?"),
                snapshot["odds"].get("totals_source", "?")))
            # Log fallback audit trail
            if odds_data:
                for msg in odds_data.get("fallback_log", []):
                    logger("[ODDS_API] {} vs {}: {}".format(home, away, msg))

    return True


# ── Thread management ────────────────────────────────────────────────────────

def update_config(config):
    """Update config for the running odds monitor thread (#129)."""
    with _lock:
        _state["config"] = dict(config)


def start(config, logger=None):
    """Start the odds monitor background thread."""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["config"] = dict(config)
        t = threading.Thread(target=_poll_loop, args=(logger,), daemon=True)
        t.start()
        _state["thread"] = t
    return True


def stop(logger=None):
    """Stop the odds monitor background thread."""
    with _lock:
        if not _state["running"]:
            return False
        _state["running"] = False
    # Thread will exit on next iteration
    if logger:
        logger("[ODDS_MONITOR] Stop requested")
    return True


def is_running():
    with _lock:
        return _state["running"]


# ── Live persistence (JSONL) (#111) ──────────────────────────────────────────

def _append_live_snapshot(match_id, snapshot, game_info):
    """Append a snapshot to the live JSONL file for crash recovery."""
    try:
        entry = {"match_id": match_id, "game_info": game_info, "snapshot": snapshot}
        with open(LIVE_FILE, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass


def _recover_live_snapshots(logger=None):
    """Recover in-memory state from live JSONL file on thread start."""
    if not os.path.exists(LIVE_FILE):
        return 0
    recovered = 0
    try:
        with open(LIVE_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    mid = entry.get("match_id", "")
                    snap = entry.get("snapshot")
                    ginfo = entry.get("game_info", {})
                    if mid and snap:
                        with _lock:
                            if mid not in _state["snapshots"]:
                                _state["snapshots"][mid] = []
                            _state["snapshots"][mid].append(snap)
                            if ginfo and mid not in _state["game_info"]:
                                _state["game_info"][mid] = ginfo
                        recovered += 1
                except json.JSONDecodeError:
                    pass
        if logger and recovered:
            logger("[ODDS_MONITOR] Recovered {} snapshots from live file".format(recovered))
    except Exception as e:
        if logger:
            logger("[ODDS_MONITOR] Live recovery failed: {}".format(e))
    return recovered


def _clear_live_file():
    """Remove the live JSONL file after game-end persistence."""
    try:
        if os.path.exists(LIVE_FILE):
            os.remove(LIVE_FILE)
    except Exception:
        pass


# ── Data access ──────────────────────────────────────────────────────────────

def get_live_snapshots():
    """Get all in-memory snapshots for live games."""
    with _lock:
        result = {}
        for mid, snaps in _state["snapshots"].items():
            result[mid] = list(snaps)
        return result


def get_game_snapshots(match_id):
    """Get snapshots for a specific game."""
    with _lock:
        return list(_state["snapshots"].get(match_id, []))


def clear_game(match_id):
    """Remove a game from in-memory state (after persisting)."""
    with _lock:
        _state["snapshots"].pop(match_id, None)
        _state["game_info"].pop(match_id, None)


# ── Persistence ──────────────────────────────────────────────────────────────

def _compute_leading_strategy(snapshots, record, threshold=None):
    """Compute leading team spread strategy result for a completed game (#211).

    Finds the first snapshot where leading_team_ev.qualifies is True,
    then determines if the leading team covered the spread.
    Returns strategy dict or None if no qualifying snapshot.

    If threshold is provided (#220), overrides the stored qualifies flag
    and checks spread_price >= threshold directly.
    """
    entry = None
    for s in snapshots:
        lev = s.get("leading_team_ev", {})
        if threshold is not None:
            price = lev.get("spread_price")
            if price is not None and lev.get("leading_team") and price >= threshold:
                entry = s
                break
        elif lev.get("qualifies"):
            entry = s
            break

    if not entry:
        return {"triggered": False}

    lev = entry["leading_team_ev"]
    team = lev.get("leading_team", "")
    side = lev.get("leading_side", "")
    spread_line = lev.get("spread_line")
    spread_price = lev.get("spread_price")

    final_home = record.get("final_home_score")
    final_away = record.get("final_away_score")

    covered = None
    pnl = None
    if final_home is not None and final_away is not None and spread_line is not None:
        # Spread is from leading team's perspective
        if side == "home":
            team_margin = final_home - final_away
        else:
            team_margin = final_away - final_home
        # Covered if team_margin + spread_line > 0
        covered = (team_margin + spread_line) > 0
        if spread_price is not None:
            pnl = round(spread_price - 1, 4) if covered else -1.0

    return {
        "triggered": True,
        "team": team,
        "side": side,
        "entry_minute": entry.get("game_minute", 0),
        "spread_line": spread_line,
        "spread_price": spread_price,
        "margin_at_entry": lev.get("margin", 0),
        "ev_edge": lev.get("ev_edge"),
        "covered": covered,
        "pnl": pnl,
    }


def _compute_trailing_strategy(snapshots, record, threshold=None):
    """Compute trailing team spread strategy result for a completed game (#213).

    Finds the first snapshot where trailing_team_ev.qualifies is True,
    then determines if the trailing team covered the spread.
    Returns strategy dict or None if no qualifying snapshot.

    If threshold is provided (#220), overrides the stored qualifies flag.
    """
    entry = None
    for s in snapshots:
        tev = s.get("trailing_team_ev", {})
        if threshold is not None:
            price = tev.get("spread_price")
            if price is not None and tev.get("trailing_team") and price >= threshold:
                entry = s
                break
        elif tev.get("qualifies"):
            entry = s
            break

    if not entry:
        return {"triggered": False}

    tev = entry["trailing_team_ev"]
    team = tev.get("trailing_team", "")
    side = tev.get("trailing_side", "")
    spread_line = tev.get("spread_line")
    spread_price = tev.get("spread_price")

    final_home = record.get("final_home_score")
    final_away = record.get("final_away_score")

    covered = None
    pnl = None
    if final_home is not None and final_away is not None and spread_line is not None:
        # Spread is from trailing team's perspective
        if side == "home":
            team_margin = final_home - final_away
        else:
            team_margin = final_away - final_home
        # Covered if team_margin + spread_line > 0
        covered = (team_margin + spread_line) > 0
        if spread_price is not None:
            pnl = round(spread_price - 1, 4) if covered else -1.0

    return {
        "triggered": True,
        "team": team,
        "side": side,
        "entry_minute": entry.get("game_minute", 0),
        "spread_line": spread_line,
        "spread_price": spread_price,
        "margin_at_entry": tev.get("margin", 0),
        "ev_edge": tev.get("ev_edge"),
        "covered": covered,
        "pnl": pnl,
    }


def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3, threshold=None):
    """Compute spread analysis across game history with threshold sweep and dimensional breakdowns.

    Runs BOTH leading and trailing strategies per game (merged, #229).

    Args:
        history: list of game records from load_history()
        thresholds: list of threshold values to sweep (default 1.5-6.0)
        game_history: optional list of game_history.json records for enrichment (#226)
        combo_min: minimum combo dimension count, 2-5 (default 2) (#230)
        combo_max: maximum combo dimension count, 2-5 (default 3) (#230)
        threshold: specific threshold for analysis (float). None = use sweep-best. (#231)

    Returns dict with threshold_sweep, best_threshold, dimensions, combos,
    profitable_patterns, anti_patterns, combo_5d, total_games.
    """
    from collections import defaultdict
    from itertools import combinations

    if thresholds is None:
        thresholds = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0]

    # Build game_history lookup for enrichment (#226)
    _gh_map = {}
    if game_history:
        for gh in game_history:
            mid = gh.get("match_id", "")
            if mid:
                _gh_map[mid] = gh

    # 1. Threshold sweep — run BOTH strategies per game (#229)
    sweep = []
    best_thresh = thresholds[0]
    best_roi = -999
    for thresh in thresholds:
        triggered = 0
        covered_count = 0
        total_pnl = 0.0
        for g in history:
            for fn in [_compute_leading_strategy, _compute_trailing_strategy]:
                strat = fn(g.get("snapshots", []), g, threshold=thresh)
                if strat.get("triggered"):
                    triggered += 1
                    if strat.get("covered"):
                        covered_count += 1
                    if strat.get("pnl") is not None:
                        total_pnl += strat["pnl"]
        roi = round(total_pnl / triggered * 100, 1) if triggered > 0 else None
        sweep.append({"threshold": thresh, "triggered": triggered, "covered": covered_count,
                       "pnl": round(total_pnl, 2), "roi": roi})
        if triggered >= 3 and roi is not None and roi > best_roi:
            best_roi = roi
            best_thresh = thresh

    # Determine analysis threshold (#231)
    analysis_thresh = threshold if threshold is not None else best_thresh

    # 2. Compute strategies at best threshold — BOTH per game (#229)
    strategies = []
    for g in history:
        for ctx, fn in [("leading", _compute_leading_strategy),
                        ("trailing", _compute_trailing_strategy)]:
            strat = fn(g.get("snapshots", []), g, threshold=analysis_thresh)
            if strat.get("triggered"):
                strat["margin_context"] = ctx
                entry_odds = {}
                entry_min = strat.get("entry_minute", 0)
                for snap in g.get("snapshots", []):
                    if snap.get("game_minute") == entry_min:
                        entry_odds = snap.get("odds", {})
                        break
                strategies.append({
                    "strat": strat,
                    "game": g,
                    "entry_odds": entry_odds,
                })

    # Helper to bucket a strategy into dimension values
    def _bucket(s):
        strat = s["strat"]
        game = s["game"]
        odds = s.get("entry_odds", {})
        margin = strat.get("margin_at_entry", 0)
        entry = strat.get("entry_minute", 0)
        total = game.get("final_total") or 0
        ev = strat.get("ev_edge") or 0
        price = strat.get("spread_price") or 0
        # Derive favorite/underdog from ML odds at entry (#223)
        side = strat.get("side", "unknown")
        home_ml = odds.get("home_ml")
        away_ml = odds.get("away_ml")
        if home_ml and away_ml:
            if side == "home":
                ml_role = "favorite" if home_ml < away_ml else "underdog"
            else:
                ml_role = "favorite" if away_ml < home_ml else "underdog"
        else:
            ml_role = "unknown"
        result = {
            "side": side,
            "role": ml_role,
            "margin": "0-6" if margin <= 6 else ("7-12" if margin <= 12 else "13+"),
            "entry_minute": "65-70" if entry <= 70 else ("71-75" if entry <= 75 else "76-80"),
            "total_points": "0-30" if total <= 30 else ("31-40" if total <= 40 else ("41-50" if total <= 50 else "51+")),
            "ev_edge": "negative" if ev < 0 else ("neutral" if ev <= 0.05 else "positive"),
            "spread_price": "$3-4" if price < 4 else ("$4-5" if price < 5 else "$5+"),
            "team": strat.get("team", ""),
            "competition": game.get("competition", ""),
            "margin_context": strat.get("margin_context", "unknown"),
        }
        # Enrich from game_history (#226)
        gh = _gh_map.get(game.get("match_id", ""))
        if gh:
            # Halftime context
            htm = gh.get("halftime_margin")
            if htm is not None:
                strat_side = strat.get("side", "")
                ht_lead = htm if strat_side == "home" else -htm
                ht_mb = _hist_bucket(abs(ht_lead), _HT_MARGIN_RANGES)
                if ht_lead > 0:
                    result["halftime_context"] = f"{ht_mb} (leading@HT)"
                    result["ht_position"] = "leading@HT"
                elif ht_lead < 0:
                    result["halftime_context"] = f"{ht_mb} (trailing@HT)"
                    result["ht_position"] = "trailing@HT"
                else:
                    result["halftime_context"] = f"{ht_mb} (tied@HT)"
                    result["ht_position"] = "tied@HT"
                # Margin trajectory
                diff = margin - abs(ht_lead) if ht_lead >= 0 else margin + ht_lead
                if diff > 4:
                    result["margin_trajectory"] = "lead_growing (>4pts)"
                elif diff > 0:
                    result["margin_trajectory"] = "lead_growing (1-4pts)"
                elif diff == 0:
                    result["margin_trajectory"] = "lead_stable"
                elif diff > -4:
                    result["margin_trajectory"] = "lead_shrinking (1-4pts)"
                else:
                    result["margin_trajectory"] = "lead_shrinking (>4pts)"
            # Key conditions
            cond_types = set()
            for c in gh.get("conditions_fired", []):
                ct = c.get("type", "")
                if ct in ("halftime_turnaround", "momentum_shift"):
                    cond_types.add(ct)
            if "halftime_turnaround" in cond_types:
                result["key_conditions"] = "has_halftime_turnaround"
            elif "momentum_shift" in cond_types:
                result["key_conditions"] = "has_momentum_shift"
            else:
                result["key_conditions"] = "neither"
            result["has_ht_turnaround"] = "yes" if "halftime_turnaround" in cond_types else "no"
            # Postgame tags
            tags = gh.get("postgame_tags", [])
            result["postgame_tags"] = tags[0] if tags else "no_tags"
            # Spread line size
            spr = gh.get("home_spread")
            if spr is not None:
                result["spread_line_size"] = _hist_bucket(abs(spr), _SPREAD_RANGES)
        return result

    # Build dimensional breakdowns
    dims = {}
    dim_names = ["side", "role", "margin", "entry_minute", "total_points", "ev_edge",
                 "spread_price", "team", "competition", "margin_context",
                 "halftime_context", "margin_trajectory", "key_conditions",
                 "postgame_tags", "spread_line_size"]
    for dim in dim_names:
        buckets = defaultdict(lambda: {"games": 0, "covered": 0, "pnl": 0.0})
        for s in strategies:
            b = _bucket(s)
            key = b.get(dim)
            if key is None:
                continue
            buckets[key]["games"] += 1
            if s["strat"].get("covered"):
                buckets[key]["covered"] += 1
            if s["strat"].get("pnl") is not None:
                buckets[key]["pnl"] += s["strat"]["pnl"]
        # Finalize
        dim_result = {}
        for key, val in buckets.items():
            val["pnl"] = round(val["pnl"], 2)
            val["roi"] = round(val["pnl"] / val["games"] * 100, 1) if val["games"] > 0 else None
            val["low_confidence"] = val["games"] < 5
            dim_result[key] = val
        dims[dim] = dim_result

    # 3. Variable-dimension combos from 7 core dimensions (#230)
    COMBO_DIMS_7 = ["side", "role", "margin", "entry_minute",
                    "margin_context", "spread_price", "ev_edge"]
    combo_min = max(2, min(5, combo_min))
    combo_max = max(combo_min, min(5, combo_max))

    combos = []
    for k in range(combo_min, combo_max + 1):
        for dim_combo in combinations(COMBO_DIMS_7, k):
            buckets = defaultdict(lambda: {"games": 0, "covered": 0, "pnl": 0.0})
            for s in strategies:
                b = _bucket(s)
                vals = tuple(b.get(d) for d in dim_combo)
                if None in vals:
                    continue
                buckets[vals]["games"] += 1
                if s["strat"].get("covered"):
                    buckets[vals]["covered"] += 1
                if s["strat"].get("pnl") is not None:
                    buckets[vals]["pnl"] += s["strat"]["pnl"]
            for vals, val in buckets.items():
                if val["games"] < 2:
                    continue
                val["pnl"] = round(val["pnl"], 2)
                val["roi"] = round(val["pnl"] / val["games"] * 100, 1) if val["games"] > 0 else None
                val["low_confidence"] = val["games"] < 5
                combos.append({
                    "dims": k,
                    "dimensions": list(dim_combo),
                    "values": list(vals),
                    "combo_label": " + ".join(str(v) for v in vals),
                    "games": val["games"],
                    "covered": val["covered"],
                    "pnl": val["pnl"],
                    "roi": val["roi"],
                    "low_confidence": val["low_confidence"],
                })
    combos.sort(key=lambda c: abs(c.get("roi") or 0), reverse=True)

    # 4/5. Notable patterns
    profitable = [c for c in combos if (c.get("roi") or 0) > 20 and c["games"] >= 3]
    anti = [c for c in combos if (c.get("roi") or 0) < -30 and c["games"] >= 3]

    # 5D combo table (#229)
    combo_5d_buckets = defaultdict(lambda: {"games": 0, "covered": 0, "pnl": 0.0})
    for s in strategies:
        b = _bucket(s)
        key = (b["side"], b["role"], b["margin"], b["entry_minute"], b["margin_context"])
        combo_5d_buckets[key]["games"] += 1
        if s["strat"].get("covered"):
            combo_5d_buckets[key]["covered"] += 1
        if s["strat"].get("pnl") is not None:
            combo_5d_buckets[key]["pnl"] += s["strat"]["pnl"]
    combo_5d = []
    for (side, role, margin, entry, ctx), val in combo_5d_buckets.items():
        if val["games"] < 3:
            continue
        val["pnl"] = round(val["pnl"], 2)
        val["roi"] = round(val["pnl"] / val["games"] * 100, 1) if val["games"] else None
        combo_5d.append({
            "side": side, "role": role, "margin": margin,
            "entry_minute": entry, "margin_context": ctx,
            "games": val["games"], "covered": val["covered"],
            "pnl": val["pnl"], "roi": val["roi"],
            "low_confidence": val["games"] < 5,
        })
    combo_5d.sort(key=lambda c: abs(c.get("roi") or 0), reverse=True)

    return {
        "threshold_sweep": sweep,
        "best_threshold": best_thresh,
        "analysis_threshold": analysis_thresh,
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "combo_5d": combo_5d,
        "total_games": len(history),
    }


def persist_game(match_id, final_result=None):
    """Persist a game's snapshots to odds_monitor_history.json.

    Called when a game ends. Optionally includes final result for
    historical +EV analysis.
    """
    with _lock:
        snapshots = list(_state["snapshots"].get(match_id, []))
        game_info = dict(_state["game_info"].get(match_id, {}))

    if not snapshots:
        return

    record = {
        "match_id": match_id,
        "home_team": game_info.get("home_team", ""),
        "away_team": game_info.get("away_team", ""),
        "competition": game_info.get("competition", "nrl_premiership"),
        "soo_game": game_info.get("soo_game", 0),
        "round_title": game_info.get("round_title", ""),
        "snapshot_count": len(snapshots),
        "first_snapshot_ts": snapshots[0]["ts"],
        "last_snapshot_ts": snapshots[-1]["ts"],
        "snapshots": snapshots,
    }

    if final_result:
        record["final_home_score"] = final_result.get("home_score")
        record["final_away_score"] = final_result.get("away_score")
        record["final_total"] = final_result.get("total")
        # Compute actual over/under result vs line at first snapshot
        first_line = snapshots[0]["odds"].get("total_line")
        if first_line is not None and record.get("final_total") is not None:
            record["over_hit"] = record["final_total"] > first_line
            record["under_hit"] = record["final_total"] < first_line

    # Leading + trailing team spread strategy (#211, #213)
    record["leading_strategy"] = _compute_leading_strategy(snapshots, record)
    record["trailing_strategy"] = _compute_trailing_strategy(snapshots, record)

    # Append to history file
    try:
        entries = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                entries = json.load(f)
            if not isinstance(entries, list):
                entries = []
        entries.append(record)
        tmp = HISTORY_FILE + ".tmp.{}".format(os.getpid())
        with open(tmp, "w") as f:
            json.dump(entries, f, indent=1)
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass

    # Auto-compute spread analysis and log notable patterns (#216)
    try:
        analysis = compute_spread_analysis(entries)
        profitable = analysis.get("profitable_patterns", [])
        if profitable:
            import logging
            _lg = logging.getLogger("nrl_monitor")
            for p in profitable[:5]:
                _lg.info("[ODDS_MONITOR] +ROI pattern: %s+%s %d/%d covered ROI=%s%% (%d games)",
                         p.get("value1"), p.get("value2"), p.get("covered"), p.get("games"),
                         p.get("roi"), p.get("games"))
    except Exception:
        pass

    clear_game(match_id)


def load_history():
    """Load persisted odds monitor history."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []


# ── PBP cache helpers (#226) ────────────────────────────────────────────────

SCORE_POINTS = {"Try": 4, "Goal": 2, "Penalty Goal": 2, "Field Goal": 1}


def match_id_to_cache_path(match_id):
    """Convert game_history match_id to nrl_cache file path.

    Example: /draw/nrl-premiership/2026/round-17/sea-eagles-v-storm/
          -> nrl_cache/2026/match_sea-eagles-v-storm_r17.json
    """
    parts = match_id.strip("/").split("/")
    if len(parts) < 5:
        return None
    year, round_str, teams = parts[2], parts[3], parts[4]
    m = re.search(r"round-(\d+)", round_str)
    if not m:
        return None
    path = os.path.join(NRL_CACHE_DIR, year, f"match_{teams}_r{m.group(1)}.json")
    return path if os.path.exists(path) else None


def score_at_minute(cache_path, target_minute=70):
    """Derive home/away score at a game minute from PBP timeline.

    Walks timeline scoring events (Try=4, Goal=2, Penalty Goal=2, Field Goal=1)
    and accumulates points up to target_minute * 60 gameSeconds.

    Returns dict with home_score, away_score, margin (home - away),
    home_id, away_id, or None on failure.
    """
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
    except Exception:
        return None
    timeline = data.get("timeline", [])
    home_id = (data.get("homeTeam") or {}).get("teamId")
    away_id = (data.get("awayTeam") or {}).get("teamId")
    if not home_id or not away_id or not timeline:
        return None
    target_seconds = target_minute * 60
    hs, aws = 0, 0
    for ev in timeline:
        if ev.get("gameSeconds", 0) > target_seconds:
            break
        pts = SCORE_POINTS.get(ev.get("type", ""), 0)
        if pts:
            tid = ev.get("teamId")
            if tid == home_id:
                hs += pts
            elif tid == away_id:
                aws += pts
    return {"home_score": hs, "away_score": aws, "margin": hs - aws,
            "home_id": home_id, "away_id": away_id}


def _hist_bucket(value, ranges):
    """Bucket a numeric value. ranges: list of (upper_bound, label); last upper=None."""
    for upper, label in ranges:
        if upper is None or value <= upper:
            return label
    return ranges[-1][1]


_MARGIN_RANGES = [(6, "0-6"), (12, "7-12"), (None, "13+")]
_HT_MARGIN_RANGES = [(3, "0-3"), (6, "4-6"), (12, "7-12"), (None, "13+")]
_TOTAL_RANGES = [(30, "0-30"), (40, "31-40"), (50, "41-50"), (None, "51+")]
_SPREAD_RANGES = [(3.5, "1-3.5"), (7.5, "4-7.5"), (13.5, "8-13.5"), (None, "14+")]


def compute_historical_win_analysis(game_history, role="leading", target_minute=70,
                                     minutes=None, season=None, segment=None,
                                     side=None, fav_role=None, margin_context=None,
                                     margin_bucket=None, page=1, page_size=25):
    """Analyse win rate of leading/trailing team at target_minute using PBP cache.

    Args:
        game_history: list of game records from game_history.json
        role: "leading" (win rate), "trailing" (comeback rate),
              or "final_stretch" (who outscores in last 10 min — score reset)
        target_minute: game minute to evaluate score (default 70)
        minutes: list of minutes to evaluate (multi-minute mode)
        season: season filter — int year, "last3", "last5", or "all"
        segment: season_segment filter (e.g. "regular_season", "finals")
        side: post-enrichment filter — "home" or "away"
        fav_role: post-enrichment filter — "favourite" or "underdog"
        margin_context: post-enrichment filter — "leading" or "trailing"
        margin_bucket: post-enrichment filter — margin bucket string
        page: pagination page number (1-based)
        page_size: games per page (default 25)

    Returns dict with baseline, dimensions, combos, patterns, 5D combos,
    paginated game list, metadata.
    """
    from collections import defaultdict

    # ── Minutes resolution ───────────────────────────────────────────────
    ALLOWED_MINUTES = {65, 68, 70, 72, 75}
    if minutes:
        eval_minutes = [m for m in minutes if m in ALLOWED_MINUTES] or [target_minute]
    else:
        eval_minutes = [target_minute]

    # ── Available seasons (before any filtering) ─────────────────────────
    available_seasons = sorted(set(
        str(g.get("season", "")) for g in game_history if g.get("season")), reverse=True)

    # ── Season/segment pre-filter ────────────────────────────────────────
    if season and season != "all":
        if season == "last3":
            all_seasons = sorted(set(g.get("season", 0) for g in game_history), reverse=True)
            valid_seasons = set(all_seasons[:3])
            game_history = [g for g in game_history if g.get("season") in valid_seasons]
        elif season == "last5":
            all_seasons = sorted(set(g.get("season", 0) for g in game_history), reverse=True)
            valid_seasons = set(all_seasons[:5])
            game_history = [g for g in game_history if g.get("season") in valid_seasons]
        else:
            try:
                yr = int(season)
                game_history = [g for g in game_history if g.get("season") == yr]
            except (ValueError, TypeError):
                pass
    if segment and segment != "all":
        game_history = [g for g in game_history if g.get("season_segment") == segment]

    games = []
    tied = 0
    draws = 0
    cache_misses = 0
    is_final_stretch = role == "final_stretch"

    for g in game_history:
        cp = match_id_to_cache_path(g.get("match_id", ""))
        if not cp:
            cache_misses += 1
            continue

        # Check final scores and draws BEFORE minute loop (per-game, not per-minute)
        home_final = g.get("home_score")
        away_final = g.get("away_score")
        if home_final is None or away_final is None:
            continue
        if home_final == away_final:
            draws += 1
            continue

        # ── Multi-minute loop ────────────────────────────────────────────
        for eval_min in eval_minutes:
            s = score_at_minute(cp, eval_min)
            if not s:
                # Only count cache miss once per game (first minute attempt)
                if eval_min == eval_minutes[0]:
                    cache_misses += 1
                continue

            if is_final_stretch:
                home_stretch = home_final - s["home_score"]
                away_stretch = away_final - s["away_score"]
                if home_stretch == away_stretch:
                    tied += 1
                    continue
                rec_side = "home"
                won = home_stretch > away_stretch
                stretch_margin = home_stretch - away_stretch
                rec = {"_won": won, "_side": rec_side, "_margin": abs(stretch_margin),
                       "_home_stretch": home_stretch, "_away_stretch": away_stretch}
                rec["_game"] = g
                # Margin context for final_stretch: based on score at target minute
                if s["margin"] > 0:
                    rec["_margin_context"] = "leading"
                elif s["margin"] < 0:
                    rec["_margin_context"] = "trailing"
                else:
                    rec["_margin_context"] = "tied"
            else:
                margin = s["margin"]
                if margin == 0:
                    tied += 1
                    continue
                if role == "trailing":
                    rec_side = "away" if margin > 0 else "home"
                    won = (away_final > home_final) if margin > 0 else (home_final > away_final)
                else:
                    rec_side = "home" if margin > 0 else "away"
                    won = (home_final > away_final) if margin > 0 else (away_final > home_final)
                rec = {"_won": won, "_side": rec_side, "_margin": abs(margin)}
                rec["_game"] = g
                # Margin context: for leading role, always "leading"; for trailing, always "trailing"
                rec["_margin_context"] = role

            # Entry minute and score at entry
            rec["_entry_minute"] = eval_min
            rec["_score_home_at_min"] = s["home_score"]
            rec["_score_away_at_min"] = s["away_score"]

            # Role (fav/underdog) from pregame odds or spread
            ho = g.get("home_odds")
            ao = g.get("away_odds")
            hs = g.get("home_spread")
            cur_side = rec["_side"]
            if ho and ao:
                rec["_role"] = "favourite" if (ho < ao if cur_side == "home" else ao < ho) else "underdog"
            elif hs is not None:
                rec["_role"] = "favourite" if (hs < 0 if cur_side == "home" else hs > 0) else "underdog"
            else:
                rec["_role"] = "unknown"

            # Halftime context
            htm = g.get("halftime_margin")
            if htm is not None:
                ht_lead = htm if cur_side == "home" else -htm
                ht_margin_bucket = _hist_bucket(abs(ht_lead), _HT_MARGIN_RANGES)
                if ht_lead > 0:
                    rec["_ht_context"] = f"{ht_margin_bucket} (leading@HT)"
                elif ht_lead < 0:
                    rec["_ht_context"] = f"{ht_margin_bucket} (trailing@HT)"
                else:
                    rec["_ht_context"] = f"{ht_margin_bucket} (tied@HT)"
                rec["_ht_position"] = "leading@HT" if ht_lead > 0 else ("trailing@HT" if ht_lead < 0 else "tied@HT")
                # Margin trajectory
                lead_now = rec["_margin"]
                lead_ht = ht_lead
                trajectory_diff = lead_now - lead_ht
                if trajectory_diff > 4:
                    rec["_trajectory"] = "lead_growing (>4pts)"
                elif trajectory_diff > 0:
                    rec["_trajectory"] = "lead_growing (1-4pts)"
                elif trajectory_diff == 0:
                    rec["_trajectory"] = "lead_stable"
                elif trajectory_diff > -4:
                    rec["_trajectory"] = "lead_shrinking (1-4pts)"
                else:
                    rec["_trajectory"] = "lead_shrinking (>4pts)"

            # Key conditions
            cond_types = set()
            for c in g.get("conditions_fired", []):
                ct = c.get("type", "")
                if ct in ("halftime_turnaround", "momentum_shift"):
                    cond_types.add(ct)
            if "halftime_turnaround" in cond_types:
                rec["_key_cond"] = "has_halftime_turnaround"
            elif "momentum_shift" in cond_types:
                rec["_key_cond"] = "has_momentum_shift"
            else:
                rec["_key_cond"] = "neither"
            rec["_has_ht_turnaround"] = "yes" if "halftime_turnaround" in cond_types else "no"
            rec["_has_momentum_shift"] = "yes" if "momentum_shift" in cond_types else "no"

            # Postgame tags
            tags = g.get("postgame_tags", [])
            rec["_tags"] = tags if tags else ["no_tags"]

            # Total points
            rec["_total"] = (home_final or 0) + (away_final or 0)

            # Team
            rec["_team"] = g.get("home_team") if cur_side == "home" else g.get("away_team")

            # Competition
            rec["_competition"] = g.get("season_segment", "regular_season")

            # Spread line size (optional)
            rec["_spread"] = abs(hs) if hs is not None else None

            games.append(rec)

            # Final stretch: also add away team record for per-team analysis
            if is_final_stretch:
                rec_away = dict(rec)
                rec_away["_won"] = not won
                rec_away["_side"] = "away"
                rec_away["_team"] = g.get("away_team")
                if ho and ao:
                    rec_away["_role"] = "favourite" if ao < ho else "underdog"
                elif hs is not None:
                    rec_away["_role"] = "favourite" if hs > 0 else "underdog"
                else:
                    rec_away["_role"] = "unknown"
                rec_away["_margin_context"] = rec["_margin_context"]
                games.append(rec_away)

    # ── Post-enrichment filters ──────────────────────────────────────────
    total_unfiltered = len(games)
    if side and side != "all":
        games = [g for g in games if g["_side"] == side]
    if fav_role and fav_role != "all":
        games = [g for g in games if g["_role"] == fav_role]
    if margin_context and margin_context != "all":
        games = [g for g in games if g.get("_margin_context") == margin_context]
    if margin_bucket and margin_bucket != "all":
        allowed_margins = set(m.strip() for m in margin_bucket.split(","))
        games = [g for g in games if _hist_bucket(g["_margin"], _MARGIN_RANGES) in allowed_margins]

    _empty = {
        "baseline": {"games": 0, "wins": 0, "rate": 0},
        "dimensions": {}, "combos": [], "profitable_patterns": [],
        "anti_patterns": [], "combo_5d": [], "game_list": [],
        "page": 1, "page_size": page_size, "total_pages": 1,
        "total_filtered": 0, "total_unfiltered": total_unfiltered,
        "total_games": len(game_history),
        "tied_at_minute": tied, "draws": draws,
        "cache_misses": cache_misses, "role": role,
        "target_minute": eval_minutes[0],
        "minutes_evaluated": eval_minutes,
        "available_seasons": available_seasons,
    }
    if not games:
        return _empty

    total_wins = sum(1 for g in games if g["_won"])
    baseline_rate = round(total_wins / len(games) * 100, 1)

    # ── Dimensional breakdown ────────────────────────────────────────────
    def _dim_stats(values):
        """values: list of (bucket_key, won_bool). Returns {key: {games, wins, rate, low_confidence}}."""
        buckets = defaultdict(lambda: {"games": 0, "wins": 0})
        for key, won in values:
            buckets[key]["games"] += 1
            if won:
                buckets[key]["wins"] += 1
        result = {}
        for key, val in buckets.items():
            val["rate"] = round(val["wins"] / val["games"] * 100, 1) if val["games"] else 0
            val["low_confidence"] = val["games"] < 10
            result[key] = val
        return result

    dims = {}
    dims["side"] = _dim_stats([(g["_side"], g["_won"]) for g in games])
    dims["role"] = _dim_stats([(g["_role"], g["_won"]) for g in games])

    margin_dim_key = "margin" if len(eval_minutes) > 1 else "margin_at_" + str(eval_minutes[0])
    dims[margin_dim_key] = _dim_stats([
        (_hist_bucket(g["_margin"], _MARGIN_RANGES), g["_won"]) for g in games])
    dims["total_points"] = _dim_stats([
        (_hist_bucket(g["_total"], _TOTAL_RANGES), g["_won"]) for g in games])
    dims["team"] = _dim_stats([(g["_team"], g["_won"]) for g in games if g["_team"]])
    dims["competition"] = _dim_stats([(g["_competition"], g["_won"]) for g in games])

    # Entry minute dimension (multi-minute only)
    if len(eval_minutes) > 1:
        dims["entry_minute"] = _dim_stats([
            (str(g["_entry_minute"]), g["_won"]) for g in games])

    ht_vals = [(g["_ht_context"], g["_won"]) for g in games if "_ht_context" in g]
    if ht_vals:
        dims["halftime_context"] = _dim_stats(ht_vals)

    traj_vals = [(g["_trajectory"], g["_won"]) for g in games if "_trajectory" in g]
    if traj_vals:
        dims["margin_trajectory"] = _dim_stats(traj_vals)

    dims["key_conditions"] = _dim_stats([(g["_key_cond"], g["_won"]) for g in games])

    tag_vals = []
    for g in games:
        for tag in g["_tags"]:
            tag_vals.append((tag, g["_won"]))
    dims["postgame_tags"] = _dim_stats(tag_vals)

    spread_vals = [(_hist_bucket(g["_spread"], _SPREAD_RANGES), g["_won"])
                   for g in games if g["_spread"] is not None]
    if spread_vals:
        dims["spread_line_size"] = _dim_stats(spread_vals)

    # ── 2D combo analysis ────────────────────────────────────────────────
    def _features(g):
        f = {
            "side": g["_side"],
            "role": g["_role"],
            "margin": _hist_bucket(g["_margin"], _MARGIN_RANGES),
            "total_pts": _hist_bucket(g["_total"], _TOTAL_RANGES),
            "team": g.get("_team", ""),
            "has_ht_turnaround": g["_has_ht_turnaround"],
            "has_momentum_shift": g["_has_momentum_shift"],
        }
        if "_ht_position" in g:
            f["ht_position"] = g["_ht_position"]
        return f

    combo_pairs = [
        ("side", "margin"), ("side", "role"),
        ("role", "margin"), ("role", "ht_position"),
        ("role", "has_ht_turnaround"), ("role", "has_momentum_shift"),
        ("margin", "ht_position"), ("margin", "has_ht_turnaround"),
        ("margin", "has_momentum_shift"),
        ("team", "role"), ("ht_position", "has_ht_turnaround"),
    ]

    combos = []
    for d1, d2 in combo_pairs:
        buckets = defaultdict(lambda: {"games": 0, "wins": 0})
        for g in games:
            f = _features(g)
            v1, v2 = f.get(d1), f.get(d2)
            if v1 is None or v2 is None:
                continue
            key = f"{v1}|{v2}"
            buckets[key]["games"] += 1
            if g["_won"]:
                buckets[key]["wins"] += 1
        for key, val in buckets.items():
            if val["games"] < 3:
                continue
            parts = key.split("|")
            rate = round(val["wins"] / val["games"] * 100, 1)
            val["rate"] = rate
            val["low_confidence"] = val["games"] < 5
            combos.append({
                "key": key,
                "dimension1": d1, "value1": parts[0],
                "dimension2": d2, "value2": parts[1],
                "games": val["games"], "wins": val["wins"],
                "rate": rate, "low_confidence": val["low_confidence"],
            })
    combos.sort(key=lambda c: abs(c["rate"] - baseline_rate), reverse=True)

    # ── Pattern detection ────────────────────────────────────────────────
    profitable = [c for c in combos
                  if c["rate"] > baseline_rate + 10 and c["games"] >= 5]
    anti = [c for c in combos
            if c["rate"] < baseline_rate - 10 and c["games"] >= 5]

    # ── 5D combo table ───────────────────────────────────────────────────
    combo_5d_buckets = defaultdict(lambda: {"games": 0, "wins": 0})
    for rec in games:
        key = (rec["_side"], rec["_role"], rec.get("_margin_context", "?"),
               _hist_bucket(rec["_margin"], _MARGIN_RANGES),
               str(rec.get("_entry_minute", eval_minutes[0])))
        combo_5d_buckets[key]["games"] += 1
        if rec["_won"]:
            combo_5d_buckets[key]["wins"] += 1
    combo_5d = []
    for (s, r, mc, m, em), val in combo_5d_buckets.items():
        if val["games"] < 3:
            continue
        rate = round(val["wins"] / val["games"] * 100, 1)
        combo_5d.append({
            "side": s, "role": r, "margin_context": mc,
            "margin": m, "entry_minute": em,
            "games": val["games"], "wins": val["wins"],
            "rate": rate, "low_confidence": val["games"] < 10,
        })
    combo_5d.sort(key=lambda c: abs(c["rate"] - baseline_rate), reverse=True)

    # ── Paginated game list ──────────────────────────────────────────────
    sorted_games = sorted(games, key=lambda rec: (
        rec["_game"].get("kickoff_utc") or rec["_game"].get("ts") or "",
        rec["_game"].get("match_id", ""),
        rec.get("_entry_minute", 0)))
    sorted_games.reverse()  # newest first
    total_filtered = len(sorted_games)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_games = sorted_games[start:start + page_size]
    game_list = []
    for rec in page_games:
        g = rec["_game"]
        entry_min = rec.get("_entry_minute", eval_minutes[0])
        score_at = f"{rec.get('_score_home_at_min', '?')}-{rec.get('_score_away_at_min', '?')}"
        game_list.append({
            "date": (g.get("kickoff_utc") or g.get("ts", ""))[:10],
            "home_team": g.get("home_team", ""),
            "away_team": g.get("away_team", ""),
            "home_score": g.get("home_score"),
            "away_score": g.get("away_score"),
            "final_margin": abs((g.get("home_score") or 0) - (g.get("away_score") or 0)),
            "team": rec.get("_team", ""),
            "entry_minute": entry_min,
            "score_at_entry": score_at,
            "margin_at_entry": rec["_margin"],
            "won": rec["_won"],
            "conditions_count": len(g.get("conditions_fired", [])),
            "tags": g.get("postgame_tags", []),
        })

    return {
        "baseline": {"games": len(games), "wins": total_wins, "rate": baseline_rate},
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "combo_5d": combo_5d,
        "game_list": game_list,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_filtered": total_filtered,
        "total_unfiltered": total_unfiltered,
        "total_games": len(game_history),
        "tied_at_minute": tied,
        "draws": draws,
        "cache_misses": cache_misses,
        "role": role,
        "target_minute": eval_minutes[0],
        "minutes_evaluated": eval_minutes,
        "available_seasons": available_seasons,
    }


def get_status():
    """Get current odds monitor status for dashboard."""
    with _lock:
        active_games = list(_state["snapshots"].keys())
        total_snapshots = sum(len(s) for s in _state["snapshots"].values())
        return {
            "running": _state["running"],
            "active_games": len(active_games),
            "active_match_ids": active_games,
            "total_live_snapshots": total_snapshots,
        }
