#!/usr/bin/env python3
"""
NRL Score Monitor
Polls NRL.com for live game data, evaluates configurable alert conditions,
and sends notifications to Slack via direct API or OpenClaw gateway.

Condition types: score_diff, error_rate, completion_rate, penalty_count,
                 possession, try_scoring_run, points_run, missed_tackles.

Designed to run as a one-shot job every minute via systemd timer or cron
during NRL game windows (typically Thu–Mon AEST).
"""

import json
import os
import sys
import time
try:
    import requests
except ImportError:
    requests = None
from datetime import datetime, timezone, timedelta

import nrl_api
import conditions as condition_logic
import timeline_features

try:
    import odds_api as odds_mod
except ImportError:
    odds_mod = None

try:
    import ml_model
except ImportError:
    ml_model = None

try:
    import outcomes
except ImportError:
    outcomes = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
STATE_FILE = os.path.join(SCRIPT_DIR, ".alert_state.json")
ALERTS_FILE = os.path.join(SCRIPT_DIR, "alerts.json")
LIVE_STATS_FILE = os.path.join(SCRIPT_DIR, "live_stats.json")
TICKER_FILE = os.path.join(SCRIPT_DIR, "game_ticker.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
LOG_DIR = os.path.join(SCRIPT_DIR, "logs")

COLOR_EMOJI = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}


def _resolve_pw_version(date_str, config):
    """Resolve PW version from pw_trend module or config fallback."""
    try:
        import pw_trend
        return pw_trend.resolve_pw_version(date_str)
    except ImportError:
        pass
    if outcomes and hasattr(outcomes, "resolve_pw_version"):
        return outcomes.resolve_pw_version(date_str)
    return config.get("pw_version", "v1.0")
HOST_TZ = "Australia/Sydney"


def _get_model_schema_version():
    """Return the current ML model schema version, or None."""
    try:
        import ml_model
        return ml_model.SCHEMA_VERSION
    except Exception:
        return None


def _pw_call_passes_alert_filters(call, filters, scenario_stats=None):
    """Check if a PW call passes the pw_alert_filters from config (#263, #278).

    Returns True if the call should trigger a Slack alert.
    scenario_stats: optional dict of scenario_key -> stats for ROI filters (#278).
    """
    if not filters or not filters.get("enabled"):
        return False
    q = filters.get("quarter")
    if q and call.get("quarter") != q:
        return False
    # Spread role — case-insensitive (#280)
    r = filters.get("spread_role")
    if r and str(call.get("spread_role") or "").lower() != r.lower():
        return False
    m = filters.get("margin_at_fire")
    if m:
        margin = call.get("score_margin_at_fire", 0) or 0
        if m == "leading" and margin <= 0:
            return False
        if m == "trailing" and margin > 0:
            return False
    h = filters.get("half")
    if h:
        cq = call.get("quarter", "")
        if h == "H1" and cq not in ("Q1", "Q2"):
            return False
        if h == "H2" and cq not in ("Q3", "Q4"):
            return False
        if h in ("ET", "OT") and cq not in ("ET", "OT"):
            return False
    min_pct = filters.get("min_pct")
    if min_pct is not None:
        if (call.get("pct") or call.get("winner_score_pct") or 0) < float(min_pct):
            return False
    edge_min = filters.get("edge_min")
    edge_max = filters.get("edge_max")
    avg_edge = call.get("avg_edge")
    if edge_min is not None and (avg_edge is None or avg_edge < float(edge_min)):
        return False
    if edge_max is not None and (avg_edge is None or avg_edge > float(edge_max)):
        return False
    pol = filters.get("polarity")
    if pol:
        pn = call.get("polarity_net", 0) or 0
        if pol == "positive" and pn <= 0:
            return False
        if pol == "negative" and pn >= 0:
            return False
        if pol == "neutral" and pn != 0:
            return False
    cons = filters.get("consensus")
    if cons and call.get("consensus") != cons:
        return False
    src = filters.get("source")
    if src == "live_only" and not call.get("bk_ml_source"):
        return False
    # Strict BK (#278) — require both bk_moneyline and bk_spread present
    if filters.get("strict_bk"):
        if not call.get("bk_moneyline") or call.get("bk_spread") is None:
            return False
    # Scenario ROI filters (#278)
    _sc_checks = []
    _has_sc_filter = False
    for _sc_flag, _sc_min_key, _sc_stat_key in (
        ("sc_ml_roi_on", "sc_ml_roi_min", "bk_odds_roi_pct"),
        ("sc_spr_roi_on", "sc_spr_roi_min", "bk_spr_roi_pct"),
        ("sc_q_ml_roi_on", "sc_q_ml_roi_min", "q_ml_roi_pct"),
        ("sc_q_spr_roi_on", "sc_q_spr_roi_min", "q_spr_roi_pct"),
    ):
        if not filters.get(_sc_flag):
            continue
        _has_sc_filter = True
        _sc_min = float(filters.get(_sc_min_key) or 0)
        if not scenario_stats:
            _sc_checks.append(False)
            continue
        _sc_key = call.get("_scenario_key") or ""
        _sc = scenario_stats.get(_sc_key) if _sc_key else None
        if not _sc:
            _sc_checks.append(False)
            continue
        _min_calls = int(filters.get("sc_min_calls") or 0)
        _min_games = int(filters.get("sc_min_games") or 0)
        if _min_calls > 0 and (_sc.get("total_calls") or 0) < _min_calls:
            _sc_checks.append(False)
            continue
        if _min_games > 0 and (_sc.get("total_games") or 0) < _min_games:
            _sc_checks.append(False)
            continue
        _sc_val = _sc.get(_sc_stat_key)
        _sc_checks.append(_sc_val is not None and _sc_val >= _sc_min)
    if _has_sc_filter:
        _logic = (filters.get("sc_roi_logic") or "and").lower()
        if _logic == "or":
            if not any(_sc_checks):
                return False
        else:
            if not all(_sc_checks):
                return False
    return True


def _log(*parts):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = " ".join(str(p) for p in parts)
    print("{} {}".format(ts, msg), file=sys.stderr, flush=True)


def _format_alert_time(utc_dt):
    """Format timestamp in AEST/AEDT."""
    try:
        from zoneinfo import ZoneInfo
        local_dt = utc_dt.astimezone(ZoneInfo(HOST_TZ))
        return local_dt.strftime("%a %d %b %H:%M %Z")
    except Exception:
        return utc_dt.strftime("%a %d %b %H:%M UTC")


# ── I/O helpers ───────────────────────────────────────────────────────────────

def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def _atomic_write_json(path, data):
    """Write JSON atomically using tmp + replace."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def update_alert_log(entry):
    """Prepend alert entry to alerts file.

    Anchored types (summary, final, predicted_winner, pw_underdog_alert) are
    retained permanently.  Transient condition alerts are capped at 200 entries
    to prevent unbounded growth (#302).
    """
    try:
        with open(ALERTS_FILE) as f:
            log = json.load(f)
    except Exception:
        log = []
    log.insert(0, entry)
    _anchored_types = ("summary", "final", "predicted_winner", "pw_underdog_alert")
    anchored = [r for r in log if str(r.get("type") or "") in _anchored_types]
    transient = [r for r in log if str(r.get("type") or "") not in _anchored_types]
    retained = anchored + transient[:200]
    # Re-sort by ts descending (newest first)
    retained.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    _atomic_write_json(ALERTS_FILE, retained)


def append_game_record(record):
    """Append a completed game record to game history.

    Writes to WAL first for crash safety (#16 Phase 2).
    """
    try:
        with open(HISTORY_FILE) as f:
            history = json.load(f)
    except Exception:
        history = []
    # Avoid duplicates
    existing_ids = {r.get("match_id") for r in history}
    if record.get("match_id") not in existing_ids:
        # WAL append before main write — survives crash between steps
        try:
            import outcomes
            outcomes.append_to_wal(record)
        except Exception:
            pass
        history.append(record)
        _atomic_write_json(HISTORY_FILE, history)
        _log("Appended game record: {} vs {}".format(
            record.get("home_team", "?"), record.get("away_team", "?")))


# ── Auto-Recovery (#136) ──────────────────────────────────────────────────────

def _recover_missed_game(match_url, fixture, round_number, season, config, state, now):
    """Recover a missed game from NRL API data (#136).

    Fetches match data, replays conditions via backfill, generates PW calls,
    merges with any preserved live state, writes history record.
    Returns True if a record was written.
    """
    import backfill as _bf
    import outcomes

    match_id = fixture.get("matchCentreUrl", match_url)

    # 1. Fetch match data
    raw = nrl_api.get_match_data(match_url)
    if not raw:
        _log("[AUTO-RECOVERY] No match data for {}".format(match_url))
        _append_recovery_log(state, now, match_id, fixture, round_number,
                             status="failed", error="No match data")
        return False

    mstate = raw.get("matchState", "")
    if mstate not in nrl_api.COMPLETED_STATES:
        return False  # not finished yet

    # 2. Determine season segment (#298: detect finals)
    if fixture.get("_competition") == "state_of_origin":
        segment = "state_of_origin"
    elif nrl_api._is_finals_round(round_number):
        segment = "finals"
    else:
        segment = "regular_season"

    round_title = fixture.get("roundTitle", "") or "Round {}".format(round_number)

    # 3. Build record via backfill pipeline (stats, PBP, score_progression, try_events)
    try:
        record = _bf._build_game_record(fixture, raw, season, round_number,
                                        round_title, segment)
    except Exception as e:
        _log("[AUTO-RECOVERY] Failed to build record for {}: {}".format(match_url, e))
        _append_recovery_log(state, now, match_id, fixture, round_number,
                             status="failed", error=str(e))
        return False

    # Ensure match_id is set correctly (fixture may not have matchCentreUrl)
    if not record.get("match_id"):
        record["match_id"] = match_id

    # 4. Check for preserved live state (partial miss merge)
    live_conds = state.get("cond_hits_{}".format(match_id), [])
    live_pw = state.get("pw_calls_{}".format(match_id), [])
    live_pregame = state.get("pregame_odds_{}".format(match_id))
    had_live_state = bool(live_conds or live_pw)

    # 5. Conditions: merge live + backfill
    backfill_conds = _bf.evaluate_conditions_for_game(config, record)
    if live_conds:
        # Deduplicate live conditions (first-fire per team|condition)
        seen = {}
        for c in live_conds:
            k = "{}|{}".format(c.get("team", ""), c.get("condition", ""))
            if k not in seen:
                seen[k] = c
        # Merge backfill gap-fill: add backfill conditions not already in live
        for c in backfill_conds:
            k = "{}|{}".format(c.get("team", ""), c.get("condition", ""))
            if k not in seen:
                seen[k] = c
        record["conditions_fired"] = list(seen.values())
    elif backfill_conds:
        record["conditions_fired"] = backfill_conds

    # 6. PW calls: prefer live, generate synthetic if missing
    if live_pw:
        # Resolve correct field based on final winner
        for c in live_pw:
            if record.get("winner"):
                c["correct"] = c.get("predicted_team") == record["winner"]
        record["predicted_winner_calls"] = live_pw
    elif config.get("auto_recovery_pw_calls", True):
        try:
            history = outcomes.load_history()
            game_date = (record.get("date") or record.get("kickoff_utc", "")[:10]
                         or now.strftime("%Y-%m-%d"))
            prior = [r for r in history if (r.get("date") or "")[:10] < game_date]
            threshold = config.get("predicted_winner_threshold", 65)
            cooldown = config.get("pw_eval_interval_h2", 5)
            pw_calls = _bf._generate_pw_calls_for_record(
                record, prior, config, threshold=threshold,
                cooldown_minutes=cooldown)
            if pw_calls:
                record["predicted_winner_calls"] = pw_calls
        except Exception as e:
            _log("[AUTO-RECOVERY] PW generation failed for {}: {}".format(match_id, e))

    # 7. Postgame tags
    tags = _bf.compute_postgame_tags(record)
    if tags:
        record["postgame_tags"] = tags

    # 8. Odds from preserved state
    if live_pregame:
        record["home_ml"] = live_pregame.get("home_ml")
        record["away_ml"] = live_pregame.get("away_ml")
        record["home_odds"] = live_pregame.get("home_ml")
        record["away_odds"] = live_pregame.get("away_ml")
        record["home_spread"] = live_pregame.get("home_spread")
        record["away_spread"] = live_pregame.get("away_spread")
        record["odds_bookmaker"] = live_pregame.get("bookmaker", "")

    # 9. SOO metadata
    if segment == "state_of_origin":
        record["soo_game"] = fixture.get("_soo_game", 0)

    # 10. Mark as recovered
    record["source"] = "recovered"
    record["replay_mode"] = "auto_recovered"
    record["date"] = (record.get("date")
                      or (record.get("kickoff_utc") or "")[:10]
                      or now.strftime("%Y-%m-%d"))
    record["ts"] = record.get("ts") or now.isoformat()

    # 11. Cache raw API data
    try:
        cache_dir = os.path.join(SCRIPT_DIR, "nrl_cache", str(season))
        os.makedirs(cache_dir, exist_ok=True)
        slug = match_url.rstrip("/").split("/")[-1]
        cache_file = os.path.join(cache_dir, "match_{}_r{}.json".format(slug, round_number))
        if not os.path.exists(cache_file):
            tmp = cache_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as cf:
                json.dump(raw, cf, indent=2, default=str)
            os.replace(tmp, cache_file)
    except Exception:
        pass

    # 12. Write to history (WAL + dedup)
    append_game_record(record)

    # 13. Set state flags
    state["game_end_{}".format(match_id)] = now.isoformat()

    # 14. Log recovery event for dashboard
    n_conds = len(record.get("conditions_fired", []))
    n_pw = len(record.get("predicted_winner_calls", []))
    _append_recovery_log(state, now, match_id, fixture, round_number,
                         status="success", had_live_state=had_live_state,
                         conditions=n_conds, pw_calls=n_pw,
                         home_score=record.get("home_score", 0),
                         away_score=record.get("away_score", 0))

    # 15. Send final-score Slack alert (skip alert history — recovered games
    #     are old and should not appear as today's alerts in the dashboard #289)
    home_team = record.get("home_team", "")
    away_team = record.get("away_team", "")
    home_score = record.get("home_score", 0)
    away_score = record.get("away_score", 0)
    margin = record.get("margin", 0)
    winner = record.get("winner", "")
    msg = "🔄 *Auto-Recovered — Full Time*\n{} {} - {} {}\n_{} wins by {}_".format(
        home_team, home_score, away_score, away_team, winner, margin)
    if home_score == away_score:
        msg = "🔄 *Auto-Recovered — Full Time — Draw*\n{} {} - {} {}".format(
            home_team, home_score, away_score, away_team)
    send_alert(config, msg)

    _log("[AUTO-RECOVERY] Recovered: {} vs {} (R{}) | conds={} | pw={} | live_state={}".format(
        home_team, away_team, round_number, n_conds, n_pw, had_live_state))
    return True


def _append_recovery_log(state, now, match_id, fixture, round_number,
                         status="success", error=None, had_live_state=False,
                         conditions=0, pw_calls=0, home_score=0, away_score=0):
    """Append an entry to the auto_recovery_log in state (capped at 100)."""
    log = state.get("auto_recovery_log", [])
    home = fixture.get("homeTeam", {})
    away = fixture.get("awayTeam", {})
    entry = {
        "ts": now.isoformat(),
        "match_id": match_id,
        "home_team": home.get("nickName", "") if isinstance(home, dict) else str(home),
        "away_team": away.get("nickName", "") if isinstance(away, dict) else str(away),
        "round": round_number,
        "score": "{}-{}".format(home_score, away_score),
        "had_live_state": had_live_state,
        "conditions": conditions,
        "pw_calls": pw_calls,
        "status": status,
    }
    if error:
        entry["error"] = error
    log.insert(0, entry)
    state["auto_recovery_log"] = log[:100]


# ── Alert delivery ────────────────────────────────────────────────────────────

def send_alert(config, message):
    """Send alert message to Slack. Returns True on success."""
    if requests is None:
        _log("WARN: requests not installed, cannot send alert")
        return False

    # Direct Slack API (preferred)
    bot_token = config.get("slack_bot_token", "")
    channel = config.get("slack_channel", "")
    if bot_token and channel:
        try:
            r = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": "Bearer {}".format(bot_token)},
                json={"channel": channel, "text": message},
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                return True
            _log("Slack API error:", data.get("error", "unknown"))
            return False
        except Exception as e:
            _log("Slack API exception:", e)
            return False

    # Fallback: OpenClaw gateway
    gateway = config.get("openclaw_gateway", "")
    token = config.get("openclaw_token", "")
    if not gateway:
        _log("WARN: No slack_bot_token or openclaw_gateway configured")
        return False

    try:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer {}".format(token)
        payload = {
            "tool_name": "slack_send_message",
            "tool_args": {"channel_id": channel, "message": message},
        }
        r = requests.post(
            "{}/tools/invoke".format(gateway.rstrip("/")),
            headers=headers,
            json=payload,
            timeout=10,
        )
        return r.status_code == 200
    except Exception as e:
        _log("Gateway send error:", e)
        return False


def _get_alert_mode(cond):
    """Return alert_mode for a condition: 'slack', 'history', or 'none'.

    Supports new alert_mode field and backward-compatible alert boolean.
    """
    mode = cond.get("alert_mode")
    if mode in ("slack", "history", "none"):
        return mode
    alert = cond.get("alert")
    if alert is False:
        return "none"
    if alert is True:
        return "slack"
    return "none"


# ── Condition evaluation ──────────────────────────────────────────────────────

def build_team_context(game, match_detail, team_side, pbp_features=None):
    """Build team context dict for condition evaluation.

    Args:
        game: scoreboard game dict from nrl_api
        match_detail: parsed match detail (or None if unavailable)
        team_side: 'home' or 'away'
        pbp_features: dict from timeline_features.extract_features() (optional)
    """
    opp_side = "away" if team_side == "home" else "home"

    my_score = game.get("{}_score".format(team_side), 0) or 0
    opp_score = game.get("{}_score".format(opp_side), 0) or 0
    diff_total = my_score - opp_score

    # Halftime scores from match detail
    diff_1h = None
    diff_2h = None
    if match_detail:
        my_1h = match_detail.get("{}_score_1h".format(team_side))
        opp_1h = match_detail.get("{}_score_1h".format(opp_side))
        if my_1h is not None and opp_1h is not None:
            diff_1h = my_1h - opp_1h
            # 2H score = total - 1H
            my_2h = my_score - my_1h
            opp_2h = opp_score - opp_1h
            diff_2h = my_2h - opp_2h

    # Stats from match detail
    stats = {}
    if match_detail:
        stats = match_detail.get("{}_stats".format(team_side), {})

    # PBP features from timeline (#119)
    pbp = pbp_features or {}
    consecutive_tries = pbp.get("{}_max_consecutive_tries".format(team_side), 0)
    unanswered_points = pbp.get("{}_max_unanswered_points".format(team_side), 0)

    return {
        "team_name": game.get("{}_team".format(team_side), ""),
        "team_id": game.get("{}_team_id".format(team_side)),
        "team_side": team_side,
        "my_score": my_score,
        "opp_score": opp_score,
        "diff_total": diff_total,
        "diff_1h": diff_1h,
        "diff_2h": diff_2h,
        "is_losing": diff_total < 0,
        "stats": stats,
        "consecutive_tries": consecutive_tries,
        "unanswered_points": unanswered_points,
        "pbp_features": pbp,
    }


def get_state_key(team_name, cond_name, game_id):
    """Build cooldown state key."""
    return "{}_{}_{}".format(team_name, cond_name, game_id)


def _should_poll_odds(game, match_id, state, config, now):
    """Check if live odds should be polled for this game based on game phase.

    Returns True if enough time has passed since last poll:
      - H1 (0'-40'): every odds_poll_interval_h1 minutes (default 5)
      - H2 before threshold: every odds_poll_interval_h1 minutes (default 5)
      - H2 last N minutes: every odds_poll_interval_late minutes (default 2)
      - ET: every odds_poll_interval_late minutes (default 2)
    """
    if not config.get("odds_api_enabled") or not config.get("odds_api_key"):
        return False
    if odds_mod is None:
        return False

    interval_h1 = config.get("odds_poll_interval_h1", 5) * 60  # seconds
    interval_late = config.get("odds_poll_interval_late", 2) * 60
    late_threshold = config.get("odds_poll_late_threshold_min", 70)  # game minute

    # Use clamped game minute if available (#333)
    game_minute = _get_game_minute(game)

    # Choose interval based on game phase
    if game_minute >= late_threshold:
        interval = interval_late
    else:
        interval = interval_h1

    # Check last poll time
    last_key = "odds_poll_{}".format(match_id)
    last_poll = state.get(last_key)
    if last_poll:
        try:
            last_dt = datetime.fromisoformat(last_poll)
            if (now - last_dt).total_seconds() < interval:
                return False
        except Exception:
            pass

    return True


def _parse_game_minute(game):
    """Parse raw game minute from period/clock (no clamping)."""
    period = game.get("period", "")
    clock_str = game.get("clock", "")
    game_minute = 0
    if period in ("2H", "FT"):
        game_minute = 40
    if period in ("ET", "GoldenPoint"):
        game_minute = 80
    if clock_str:
        try:
            parts = clock_str.replace("'", "").split(":")
            game_minute = int(parts[0])
        except (ValueError, IndexError):
            pass
    return game_minute


def _get_game_minute(game):
    """Get game minute — returns clamped value if available, else raw."""
    if "_clamped_minute" in game:
        return game["_clamped_minute"]
    return _parse_game_minute(game)


def _clamp_game_minute(game, match_id, state):
    """Ensure game_minute is monotonically increasing across polls (#333).

    NRL API clock can report lower minutes on subsequent polls due to
    clock corrections, stoppages, or caching. Only affects poll-time
    metadata — try events and PBP use gameSeconds from timeline API.
    """
    raw = _parse_game_minute(game)
    state_key = "max_game_minute_{}".format(match_id)
    prev_max = state.get(state_key, 0)
    clamped = max(raw, prev_max)
    state[state_key] = clamped
    game["_clamped_minute"] = clamped
    return clamped


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


def _get_game_half(game):
    """Return 'H1', 'H2', or 'ET' based on period."""
    period = game.get("period", "")
    if period in ("1H",):
        return "H1"
    if period in ("HT", "2H", "FT"):
        return "H2"
    if period in ("ET", "GoldenPoint"):
        return "ET"
    # Try to infer from clock
    minute = _get_game_minute(game)
    if minute > 80:
        return "ET"
    if minute > 40:
        return "H2"
    return "H1"


def _should_evaluate_pw(game, match_id, state, config, now):
    """Check if PW should be evaluated for this game.

    Cadence is configurable per game phase via pw_eval_interval:
      - H1 (0'-40'):           every pw_eval_interval_h1 minutes (default 10)
      - H2 early (40'-60'):    every pw_eval_interval_h2 minutes (default 5)
      - H2 late (60'+):        every pw_eval_interval_late minutes (default 2)
      - ET:                    every pw_eval_interval_late minutes (default 2)
    """
    if not config.get("predicted_winner_enabled"):
        return False

    game_minute = _get_game_minute(game)
    # Skip minute 0 — no game context, conditions, or stats available (#169)
    if game_minute is not None and game_minute <= 0:
        return False
    late_threshold = config.get("pw_eval_late_threshold_min", 60)

    if game_minute >= late_threshold or _get_game_half(game) == "ET":
        interval = config.get("pw_eval_interval_late", 2)
    elif game_minute >= 40:
        interval = config.get("pw_eval_interval_h2", 5)
    else:
        interval = config.get("pw_eval_interval_h1", 10)

    cooldown_secs = interval * 60
    last_key = "pw_eval_{}".format(match_id)
    last_eval = state.get(last_key)
    if last_eval:
        try:
            last_dt = datetime.fromisoformat(last_eval)
            if (now - last_dt).total_seconds() < cooldown_secs:
                return False
        except Exception:
            pass
    return True


def compute_pw_analysis(game, match_detail, active_conditions, config):
    """Compute predicted winner analysis blending ML + historical win%.

    Phase 1: Simple blend of ML predict_winner() + outcomes condition win%.
    Returns dict with predictions list, or None if PW not available.
    """
    if not config.get("predicted_winner_enabled"):
        return None

    home = game.get("home_team", "")
    away = game.get("away_team", "")
    home_score = game.get("home_score", 0) or 0
    away_score = game.get("away_score", 0) or 0

    # ── ML prediction ────────────────────────────────────────────────────
    ml_result = None
    if ml_model:
        # Build a game record for predict_winner
        game_record = {
            "home_team": home,
            "away_team": away,
            "home_score": home_score,
            "away_score": away_score,
            "halftime_margin": 0,
            "h2_momentum": 0,
        }
        if match_detail:
            h1h = match_detail.get("home_score_1h")
            a1h = match_detail.get("away_score_1h")
            if h1h is not None and a1h is not None:
                game_record["halftime_margin"] = h1h - a1h
                game_record["h2_momentum"] = (home_score - away_score) - (h1h - a1h)

        try:
            ml_result = ml_model.predict_winner(game_record, active_conditions)
        except Exception:
            pass

    # ── Historical / Bayesian analysis ──────────────────────────────────
    hist_result = None
    bayes_predictions = []
    if outcomes and active_conditions:
        try:
            # Try full Bayesian blending (Phase 2) first
            if hasattr(outcomes, "live_analysis_for_game"):
                records = outcomes.load_history()
                analysis = outcomes.live_analysis_for_game(
                    game.get("match_id", ""),
                    [c.get("condition", c.get("name", "")) for c in active_conditions],
                    records, active_conditions, config,
                )
                bayes_predictions = analysis.get("predictions", [])
                # Extract home/away pct from Bayesian predictions
                for pred in bayes_predictions:
                    if pred.get("predicted_team") == home:
                        hist_result = {"home_pct": pred.get("winner_score_pct", 50),
                                       "away_pct": 100 - pred.get("winner_score_pct", 50)}
                        break
                    elif pred.get("predicted_team") == away:
                        hist_result = {"away_pct": pred.get("winner_score_pct", 50),
                                       "home_pct": 100 - pred.get("winner_score_pct", 50)}
                        break

            # Fallback: simple condition win% average
            if not hist_result:
                outcome_data = outcomes.compute_outcomes(config)
                cond_stats = outcome_data.get("conditions", {})
                home_conds = [c for c in active_conditions
                              if c.get("team_side") == "home" or c.get("team") == home]
                away_conds = [c for c in active_conditions
                              if c.get("team_side") == "away" or c.get("team") == away]

                def _avg_win_pct(conds):
                    pcts = []
                    for c in conds:
                        cname = c.get("condition", c.get("name", ""))
                        s = cond_stats.get(cname)
                        if s:
                            pcts.append(s.get("win_pct", 50))
                    return sum(pcts) / len(pcts) if pcts else 50

                home_hist_pct = _avg_win_pct(home_conds)
                away_hist_pct = _avg_win_pct(away_conds)
                hist_result = {"home_pct": home_hist_pct, "away_pct": away_hist_pct}
        except Exception:
            pass

    # ── Blend ────────────────────────────────────────────────────────────
    blend_weights = config.get("pw_blend_weights", {"ml": 0.7, "historical": 0.3})
    ml_w = blend_weights.get("ml", 0.7)
    hist_w = blend_weights.get("historical", 0.3)

    home_pct = 50.0
    away_pct = 50.0
    consensus = ""
    basis = ""

    if ml_result and hist_result:
        home_pct = ml_result["home_win_pct"] * ml_w + hist_result["home_pct"] * hist_w
        away_pct = ml_result["away_win_pct"] * ml_w + hist_result["away_pct"] * hist_w
        # Consensus: do ML and historical agree?
        ml_home = ml_result["home_win_pct"] > ml_result["away_win_pct"]
        hist_home = hist_result["home_pct"] > hist_result["away_pct"]
        consensus = "strong" if ml_home == hist_home else "conflicted"
        basis = "combo"
    elif ml_result:
        home_pct = ml_result["home_win_pct"]
        away_pct = ml_result["away_win_pct"]
        consensus = "ml_only"
        basis = "ml"
    elif hist_result:
        home_pct = hist_result["home_pct"]
        away_pct = hist_result["away_pct"]
        consensus = "historical_only"
        basis = "historical"
    else:
        return None

    # Apply half weights
    game_half = _get_game_half(game)
    half_weights = config.get("pw_half_weights", {"H1": 0.90, "H2": 1.05, "ET": 1.10})
    half_w = half_weights.get(game_half, 1.0)

    # Apply weight by boosting the leading team's pct
    if home_pct > away_pct:
        home_pct = min(99, home_pct * half_w)
        away_pct = 100 - home_pct
    else:
        away_pct = min(99, away_pct * half_w)
        home_pct = 100 - away_pct

    # Apply margin boost if predicted team is leading
    margin = home_score - away_score
    game_minute = _get_game_minute(game)
    if game_minute >= 60:
        boost = config.get("pw_margin_boost_leading_late", 1.05)
    else:
        boost = config.get("pw_margin_boost_leading", 1.0)

    if margin > 0 and home_pct > away_pct and boost != 1.0:
        home_pct = min(99, home_pct * boost)
        away_pct = 100 - home_pct
    elif margin < 0 and away_pct > home_pct and boost != 1.0:
        away_pct = min(99, away_pct * boost)
        home_pct = 100 - away_pct

    # Build predictions, enriching with Bayesian data when available
    predictions = []
    for side, pct, team in [("home", home_pct, home), ("away", away_pct, away)]:
        if pct > 50:
            pred = {
                "label": "Team wins",
                "pct": round(pct, 1),
                "winner_score_pct": round(pct, 1),
                "predicted_team": team,
                "consensus": consensus,
                "blended": basis == "combo",
                "basis": basis,
                "half": game_half,
                "game_minute": game_minute,
            }
            # Enrich with Bayesian prediction data if available
            for bp in bayes_predictions:
                if bp.get("predicted_team") == team:
                    pred["sample"] = bp.get("sample", 0)
                    pred["effective_sample"] = bp.get("effective_sample", 0)
                    pred["bayes_pct"] = bp.get("bayes_pct", 0)
                    pred["polarity_pos"] = bp.get("polarity_pos", 0)
                    pred["polarity_neg"] = bp.get("polarity_neg", 0)
                    pred["polarity_net"] = bp.get("polarity_net", 0)
                    pred["avg_edge"] = bp.get("avg_edge", 0)
                    pred["conditions_count"] = bp.get("conditions_count", 0)
                    break
            predictions.append(pred)

    return {"predictions": predictions}


def _record_pw_call(game, prediction, active_conditions, config, state, now, live_odds=None):
    """Record a PW call into state for persistence."""
    match_id = game.get("match_id")
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    home_score = game.get("home_score", 0) or 0
    away_score = game.get("away_score", 0) or 0
    margin = home_score - away_score
    pred_team = prediction.get("predicted_team", "")
    pred_is_home = pred_team == home

    # Determine spread role from pregame odds (preferred) or live odds (fallback)
    spread_role = ""
    pregame = state.get("pregame_odds_{}".format(match_id))
    pregame_spread = pregame if pregame else live_odds
    if pregame_spread:
        hs = pregame_spread.get("home_spread")
        if hs is not None:
            try:
                spread_role = "favorite" if (pred_is_home and float(hs) < 0) or (not pred_is_home and float(hs) > 0) else "underdog"
            except (TypeError, ValueError):
                pass

    # Pregame odds for predicted team
    moneyline = ""
    spread = ""
    if pregame:
        if pred_is_home:
            moneyline = str(pregame.get("home_ml", ""))
            spread = str(pregame.get("home_spread", ""))
        else:
            moneyline = str(pregame.get("away_ml", ""))
            spread = str(pregame.get("away_spread", ""))

    # Bookmaker odds at fire time (from live poll)
    bk_moneyline = ""
    bk_spread = ""
    bk_spread_price = None
    bk_name = ""
    if live_odds:
        bk_name = live_odds.get("bookmaker", "")
        if pred_is_home:
            bk_moneyline = str(live_odds.get("home_ml", ""))
            bk_spread = str(live_odds.get("home_spread", ""))
            bk_spread_price = live_odds.get("home_spread_price")
        else:
            bk_moneyline = str(live_odds.get("away_ml", ""))
            bk_spread = str(live_odds.get("away_spread", ""))
            bk_spread_price = live_odds.get("away_spread_price")

    call = {
        "ts": now.isoformat(),
        "predicted_team": pred_team,
        "pct": prediction.get("pct", 0),
        "winner_score_pct": prediction.get("winner_score_pct", 0),
        "consensus": prediction.get("consensus", ""),
        "blended": prediction.get("blended", False),
        "basis": prediction.get("basis", ""),
        "half": prediction.get("half", ""),
        "quarter": _quarter_from_minute(prediction.get("game_minute", 0)),
        "game_minute": prediction.get("game_minute", 0),
        "score_margin_at_fire": margin if pred_is_home else -margin,
        "home_score_at_fire": home_score,
        "away_score_at_fire": away_score,
        "active_conditions": [
            {"name": c.get("condition", c.get("name", "")), "team": c.get("team", "")}
            for c in active_conditions[:10]  # cap to avoid bloat
        ],
        "spread_role": spread_role,
        "moneyline": moneyline,
        "spread": spread,
        "live_moneyline": str(live_odds.get("home_ml", "")) if live_odds and pred_is_home else (str(live_odds.get("away_ml", "")) if live_odds else ""),
        "live_spread": str(live_odds.get("home_spread", "")) if live_odds and pred_is_home else (str(live_odds.get("away_spread", "")) if live_odds else ""),
        "bk_moneyline": bk_moneyline,
        "bk_spread": bk_spread,
        "bk_spread_price": bk_spread_price,
        "bk_name": bk_name,
        "bk_ml_source": live_odds.get("ml_source", "") if live_odds else "",
        "bk_spread_source": live_odds.get("spread_source", "") if live_odds else "",
        "bk_totals_source": live_odds.get("totals_source", "") if live_odds else "",
        "avg_edge": prediction.get("avg_edge"),
        "polarity_pos": prediction.get("polarity_pos", 0),
        "polarity_neg": prediction.get("polarity_neg", 0),
        "polarity_net": prediction.get("polarity_net", 0),
        "correct": None,
        "synthetic": False,
        "source": "live",
        "pw_version": _resolve_pw_version(now.isoformat(), config),
        "model_schema_version": _get_model_schema_version(),  # #258 Phase 4.12
        # Synthetic odds (#235)
        "synth_moneyline": None,
        "synth_spread": None,
        "synth_h1_ml": None,
    }

    # Compute synthetic odds for this PW call (#235)
    try:
        import synthetic_odds
        _synth_pw = synthetic_odds.compute_all(
            call.get("score_margin_at_fire"),
            call.get("quarter"),
            call.get("moneyline"),
            game_seconds=int((call.get("game_minute") or 0) * 60)
        )
        call["synth_moneyline"] = _synth_pw.get("moneyline")
        call["synth_spread"] = _synth_pw.get("spread")
        call["synth_h1_ml"] = _synth_pw.get("h1_ml")
    except Exception:
        pass

    # Store in state
    calls_key = "pw_calls_{}".format(match_id)
    if calls_key not in state:
        state[calls_key] = []
    state[calls_key].append(call)
    save_state(state)  # flush immediately so dashboard PW tab sees new call (#399)

    # Forward-test bet log (#258 Phase 5.2)
    if not call.get("_suppressed"):
        try:
            _bet_entry = {
                "ts": call["ts"],
                "game_id": match_id,
                "rule_id": "pw-default-v1",
                "predicted_team": call.get("predicted_team"),
                "model_prob": round((call.get("pct") or 0) / 100.0, 4),
                "price_at_signal": call.get("bk_moneyline") or call.get("moneyline"),
                "price_source": "live" if call.get("bk_ml_source") else "pregame",
                "ev": None,
                "spread_role": call.get("spread_role"),
                "margin_at_fire": call.get("score_margin_at_fire"),
                "quarter": call.get("quarter"),
            }
            import json as _json
            _bets_path = os.path.join(SCRIPT_DIR, "bets.jsonl")
            with open(_bets_path, "a") as _bf:
                _bf.write(_json.dumps(_bet_entry) + "\n")
        except Exception:
            pass

    return call


def _check_pw_suppression(game, prediction, config):
    """Check if a PW call should be suppressed by gates.

    Returns (suppressed: bool, reason: str).
    """
    pred_team = prediction.get("predicted_team", "")
    home = game.get("home_team", "")
    home_score = game.get("home_score", 0) or 0
    away_score = game.get("away_score", 0) or 0

    pred_is_home = pred_team == home
    margin = (home_score - away_score) if pred_is_home else (away_score - home_score)

    game_minute = _get_game_minute(game)

    # Margin gate
    if game_minute >= 60:
        gate = config.get("pw_margin_gate_late", -8)
    else:
        gate = config.get("pw_margin_gate", -12)

    if margin < gate:
        return True, "margin {} < gate {}".format(margin, gate)

    # Edge gate — suppress when avg condition edge is too low
    edge_gate = config.get("pw_edge_gate")
    if edge_gate is not None:
        avg_edge = prediction.get("avg_edge", 0)
        if avg_edge < float(edge_gate):
            return True, "avg_edge {:.1f} < gate {}".format(avg_edge, edge_gate)

    return False, ""


def _poll_live_odds(game, match_id, config, state, now):
    """Fetch live odds for a game and return odds dict or None."""
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    api_key = config.get("odds_api_key", "")
    markets = config.get("odds_api_markets", odds_mod.MARKETS if odds_mod else "h2h,spreads")

    try:
        _sport_key = odds_mod.SPORT_KEY_SOO if game.get("competition") == "state_of_origin" else None
        odds = odds_mod.fetch_live_game_odds(
            api_key, home, away,
            context="live_monitor",
            sport_key=_sport_key,
        )
        if odds:
            state["odds_poll_{}".format(match_id)] = now.isoformat()
            _log("Odds fetched for {} vs {}: spread={}, ml={}, bk={}(ml)/{}(spr)/{}(tot)".format(
                home, away,
                odds.get("home_spread", "?"), odds.get("home_ml", "?"),
                odds.get("ml_source", "?"), odds.get("spread_source", "?"),
                odds.get("totals_source", "?")))
            # Log fallback audit trail
            for msg in odds.get("fallback_log", []):
                _log("[ODDS_API] {} vs {}: {}".format(home, away, msg))
            return odds
    except Exception as e:
        _log("WARN: Odds fetch failed for {} vs {}: {}".format(home, away, e))
    return None


def main():
    now = datetime.now(timezone.utc)  # #258 Phase 1.3 — hoisted from line 1246
    os.makedirs(LOG_DIR, exist_ok=True)

    try:
        config = load_config()
    except Exception as e:
        _log("ERROR: Cannot load config:", e)
        sys.exit(1)

    state = load_state()
    state_dirty = False

    season = config.get("season", 2026)
    monitor_all = config.get("monitor_all_teams", True)
    team_filters = [t.lower() for t in config.get("teams", [])]
    all_conditions = config.get("conditions", [])
    # Split base conditions from PW threshold conditions (evaluated in 2nd pass)
    conditions = [c for c in all_conditions if c.get("type") != "predicted_winner_threshold"]
    pw_conditions = [c for c in all_conditions if c.get("type") == "predicted_winner_threshold"]

    # Warn if PW condition thresholds are misaligned with global threshold (#130)
    _global_pw_t = config.get("predicted_winner_threshold", 65)
    for _pwc in pw_conditions:
        _pwc_t = _pwc.get("threshold")
        if _pwc_t is not None:
            try:
                if float(_pwc_t) < float(_global_pw_t):
                    _log("WARN: PW condition '{}' threshold ({}) < global predicted_winner_threshold ({}). "
                         "Auto-aligning to {} at runtime.".format(
                             _pwc.get("name", ""), _pwc_t, _global_pw_t, _pwc_t))
            except (TypeError, ValueError):
                pass
    compound_conditions = config.get("compound_conditions", [])
    cooldown_minutes = config.get("alert_cooldown_minutes", 5)
    end_of_game = config.get("end_of_game_alerts", True)

    # Find current round
    try:
        current_round, fixtures = nrl_api.get_current_round_fixtures(season)
    except Exception as e:
        _log("ERROR: Failed to fetch NRL draw:", e)
        sys.exit(1)

    if not current_round or not fixtures:
        _log("No fixtures found for season", season)
        return

    # Build scoreboard
    scoreboard = nrl_api.get_live_scoreboard(season, current_round)
    games = scoreboard.get("games", [])

    # Auto-detect SOO games (#102)
    try:
        soo_fixtures = nrl_api.get_soo_fixtures(season)
        if soo_fixtures:
            soo_scoreboard = nrl_api.get_live_scoreboard.__wrapped__(season, soo_fixtures) if hasattr(nrl_api.get_live_scoreboard, '__wrapped__') else None
            # Build SOO game dicts using same normalization as get_live_scoreboard
            for fix in soo_fixtures:
                _soo_state = fix.get("matchState", "")
                home_team = fix.get("homeTeam", {})
                away_team = fix.get("awayTeam", {})
                clock_data = fix.get("clock", {})
                start_time = fix.get("startTime", "") or clock_data.get("kickOffTimeLong", "")
                is_live = (_soo_state in nrl_api.LIVE_STATES or fix.get("matchMode") == "Live") and _soo_state not in nrl_api.COMPLETED_STATES
                is_completed = _soo_state in nrl_api.COMPLETED_STATES
                if not is_live and not is_completed:
                    continue  # skip pre-game SOO
                game = {
                    "match_id": fix.get("matchId") or fix.get("matchCentreUrl", ""),
                    "match_centre_url": fix.get("matchCentreUrl", ""),
                    "match_state": _soo_state,
                    "is_live": is_live,
                    "is_completed": is_completed,
                    "is_pre": _soo_state in nrl_api.PRE_STATES,
                    "clock": clock_data.get("gameTime", ""),
                    "period": nrl_api._get_period(fix),
                    "home_team": home_team.get("nickName", ""),
                    "home_team_id": home_team.get("teamId"),
                    "home_score": home_team.get("score", 0),
                    "home_odds": home_team.get("odds"),
                    "away_team": away_team.get("nickName", ""),
                    "away_team_id": away_team.get("teamId"),
                    "away_score": away_team.get("score", 0),
                    "away_odds": away_team.get("odds"),
                    "venue": fix.get("venue", ""),
                    "venue_city": fix.get("venueCity", ""),
                    "start_time": start_time,
                    "competition": "state_of_origin",
                    "soo_game": fix.get("_soo_game", 0),
                    "round_title": fix.get("roundTitle", ""),
                }
                games.append(game)
    except Exception as e:
        _log("WARN: SOO fixture fetch failed: {}".format(e))

    live_games = [g for g in games if g.get("is_live")]
    completed_games = [g for g in games if g.get("is_completed")]

    # ── Detect missed game-end due to round flip (#144) ──────────────────────
    # If the NRL API switched to a new round while a game was still live,
    # that game never appeared as "completed" in the new round's fixtures.
    # Scan state for cond_hits_ keys without a game_end_ key — these are
    # games the monitor tracked live but never processed for game-end.
    current_match_ids = {g.get("match_id") for g in games}
    _cond_prefix = "cond_hits_"
    for _sk in list(state.keys()):
        if not _sk.startswith(_cond_prefix):
            continue
        _missed_mid = _sk[len(_cond_prefix):]
        if not _missed_mid or _missed_mid in current_match_ids:
            continue
        _end_key = "game_end_{}".format(_missed_mid)
        if state.get(_end_key):
            continue  # already processed
        # This game was tracked live but is missing from current round —
        # fetch its match data directly to check if it's completed.
        _match_url = _missed_mid  # match_id IS the matchCentreUrl path
        if not _match_url.startswith("/"):
            continue
        try:
            _raw = nrl_api.get_match_data(_match_url)
            _mstate = _raw.get("matchState", "") if _raw else ""
            if _mstate not in nrl_api.COMPLETED_STATES:
                continue  # still in progress somehow
            _log("Round flip recovery: {} ended as {} — processing game-end".format(
                _missed_mid, _mstate))
            # Build a minimal game dict for completed-game processing
            _home = _raw.get("homeTeam", {})
            _away = _raw.get("awayTeam", {})
            _clock = _raw.get("clock", {})
            _start_time = (_raw.get("startTime", "")
                           or _clock.get("kickOffTimeLong", "")
                           or state.get("kickoff_utc_{}".format(_missed_mid), ""))
            _recovered = {
                "match_id": _missed_mid,
                "match_centre_url": _match_url,
                "match_state": _mstate,
                "is_live": False,
                "is_completed": True,
                "home_team": _home.get("nickName", ""),
                "home_team_id": _home.get("teamId"),
                "home_score": _home.get("score", 0),
                "away_team": _away.get("nickName", ""),
                "away_team_id": _away.get("teamId"),
                "away_score": _away.get("score", 0),
                "venue": _raw.get("venue", ""),
                "venue_city": _raw.get("venueCity", ""),
                "start_time": _start_time,
                "competition": state.get("competition_{}".format(_missed_mid), ""),
                "soo_game": state.get("soo_game_{}".format(_missed_mid), 0),
                "round_title": state.get("round_title_{}".format(_missed_mid), ""),
                "_recovered": True,
            }
            # Derive round number from URL path (e.g. /draw/.../round-13/...) (#298: handle finals URLs)
            import re as _re
            _rnd_match = _re.search(r'/round-(\d+)/', _match_url)
            if _rnd_match:
                _recovered["_round"] = int(_rnd_match.group(1))
            elif _re.search(r'/(finals-week-\d+|preliminary-final|grand-final|qualifying-final|elimination-final|semi-final)/', _match_url):
                _recovered["_round"] = current_round or nrl_api._detect_finals_week([{"roundTitle": game.get("round_title", "")}]) or 28
            else:
                _recovered["_round"] = current_round
            completed_games.append(_recovered)
        except Exception as _e:
            _log("WARN: Round flip recovery failed for {}: {}".format(_missed_mid, _e))

    # Show round title for finals, round number for regular season (#298)
    _round_label = current_round
    if games and nrl_api._is_finals_round(current_round):
        _rt = next((g.get("round_title") for g in games if g.get("round_title")), None)
        if _rt:
            _round_label = _rt
    _log("{}: {} games ({} live, {} completed)".format(
        _round_label, len(games), len(live_games), len(completed_games)))

    # ── Auto-recovery: detect missed games from previous rounds (#136, #298) ──
    if config.get("auto_recovery_enabled", True) and current_round and current_round > 1:
        try:
            import outcomes as _outcomes_mod
            _history_ids = {r.get("match_id") for r in _outcomes_mod.load_history()}
        except Exception:
            _history_ids = set()

        _missed_games = []
        # Check previous 1-2 rounds for completed games not in history
        _rounds_to_check = [current_round - 1]
        if current_round > 2:
            _prev2_key = "recovery_checked_{}_{}".format(season, current_round - 2)
            _prev2_ts = state.get(_prev2_key, "")
            _prev2_age = 999999
            if _prev2_ts:
                try:
                    _prev2_age = (now - datetime.fromisoformat(
                        _prev2_ts.replace("Z", "+00:00"))).total_seconds()
                except (ValueError, TypeError):
                    pass
            if _prev2_age > 86400:  # re-check if >24h old
                _rounds_to_check.append(current_round - 2)

        for _chk_rnd in _rounds_to_check:
            _chk_key = "recovery_checked_{}_{}".format(season, _chk_rnd)
            _chk_ts = state.get(_chk_key, "")
            _chk_age = 999999
            if _chk_ts:
                try:
                    _chk_age = (now - datetime.fromisoformat(
                        _chk_ts.replace("Z", "+00:00"))).total_seconds()
                except (ValueError, TypeError):
                    pass
            if _chk_age < 21600:  # skip if checked <6h ago
                continue
            try:
                _chk_fixtures = nrl_api.get_draw(season, _chk_rnd)
                for _fix in _chk_fixtures:
                    _fstate = _fix.get("matchState", "")
                    _furl = _fix.get("matchCentreUrl", "")
                    if (_fstate in nrl_api.COMPLETED_STATES
                            and _furl
                            and _furl not in _history_ids
                            and not state.get("game_end_{}".format(_furl))):
                        _missed_games.append((_furl, _fix, _chk_rnd))
                state[_chk_key] = now.isoformat()
                state_dirty = True
            except Exception as _e:
                _log("WARN: Auto-recovery round {} scan failed: {}".format(_chk_rnd, _e))

        # Also check SOO fixtures for missed games
        try:
            _soo_fixes = nrl_api.get_soo_fixtures(season)
            for _fix in (_soo_fixes or []):
                _fstate = _fix.get("matchState", "")
                _furl = _fix.get("matchCentreUrl", "")
                if (_fstate in nrl_api.COMPLETED_STATES
                        and _furl
                        and _furl not in _history_ids
                        and not state.get("game_end_{}".format(_furl))):
                    _missed_games.append((_furl, _fix, 0))
        except Exception:
            pass

        for _m_url, _m_fix, _m_rnd in _missed_games:
            try:
                if _recover_missed_game(_m_url, _m_fix, _m_rnd, season,
                                        config, state, now):
                    state_dirty = True
            except Exception as _e:
                _log("WARN: Auto-recovery failed for {}: {}".format(_m_url, _e))

    if not live_games and not completed_games:
        _log("No live or recently completed games")
        # Still write empty state files for dashboard
        _atomic_write_json(LIVE_STATS_FILE, {"games": [], "ts": datetime.now(timezone.utc).isoformat()})
        _atomic_write_json(TICKER_FILE, {"hits": [], "ts": datetime.now(timezone.utc).isoformat()})
        return

    # ── Process live games ────────────────────────────────────────────────────

    all_hits = []
    live_stats = []
    cooldown_secs = cooldown_minutes * 60

    # Pre-compute scenario stats for Slack alert enrichment (NRL #191)
    _pw_scenario_stats = {}
    try:
        _pw_scenario_stats = outcomes.compute_scenario_stats(
            outcomes.load_history())
    except Exception:
        pass

    for game in live_games:
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        match_id = game.get("match_id")

        # Clamp game_minute to be monotonically increasing (#333)
        _clamp_game_minute(game, match_id, state)

        # Check if teams are monitored
        if not monitor_all:
            home_match = any(f in home_team.lower() for f in team_filters)
            away_match = any(f in away_team.lower() for f in team_filters)
            if not home_match and not away_match:
                continue

        # Fetch match detail for stats
        match_detail = None
        raw = None
        match_url = game.get("match_centre_url", "")
        if match_url:
            try:
                raw = nrl_api.get_match_data(match_url)
                match_detail = nrl_api.parse_match_detail(raw)
            except Exception as e:
                _log("WARN: Failed to fetch match detail for {}: {}".format(match_id, e))

        # Extract PBP features from timeline (#119)
        pbp_features = None
        if raw and raw.get("timeline"):
            try:
                _home_tid = str(raw.get("homeTeam", {}).get("teamId", ""))
                _away_tid = str(raw.get("awayTeam", {}).get("teamId", ""))
                pbp_features = timeline_features.extract_features(
                    raw["timeline"], _home_tid, _away_tid)
            except Exception as e:
                _log("WARN: PBP feature extraction failed for {}: {}".format(match_id, e))

        # Build team contexts
        for side in ("home", "away"):
            team_ctx = build_team_context(game, match_detail, side, pbp_features)
            team_name = team_ctx["team_name"]

            if not monitor_all:
                if not any(f in team_name.lower() for f in team_filters):
                    continue

            context = {
                "game_id": match_id,
                "period": game.get("period", ""),
                "game_minute": _get_game_minute(game),
                "state": state,
                "match_detail": match_detail,
            }

            # Evaluate each condition
            for cond in conditions:
                cond_name = cond.get("name", "")
                result = condition_logic.evaluate_condition(cond, team_ctx, context)

                if result.get("state_dirty"):
                    state_dirty = True

                if not result.get("matched"):
                    continue

                # Cooldown check
                state_key = get_state_key(team_name, cond_name, match_id)
                # One-time event conditions default to alert_once="game" (#289)
                _ONCE_PER_GAME_TYPES = {"sin_bin", "red_card", "first_team_scores"}
                alert_once = cond.get("alert_once", "")
                if not alert_once and cond.get("type", "") in _ONCE_PER_GAME_TYPES:
                    alert_once = "game"
                last_fired = state.get(state_key)
                if last_fired:
                    if alert_once == "game":
                        continue
                    try:
                        last_dt = datetime.fromisoformat(last_fired)
                        if (now - last_dt).total_seconds() < cooldown_secs:
                            continue
                    except Exception:
                        pass

                # Record hit
                hit = {
                    "team": team_name,
                    "team_abbrev": nrl_api.get_team_abbrev(team_name),
                    "condition": cond_name,
                    "type": cond.get("type", ""),
                    "color": cond.get("color", ""),
                    "direction": result.get("direction", ""),
                    "game_id": match_id,
                    "home_team": home_team,
                    "away_team": away_team,
                    "score": "{} {} - {} {}".format(
                        home_team, game.get("home_score", 0),
                        game.get("away_score", 0), away_team
                    ),
                    "period": game.get("period", ""),
                    "score_margin": team_ctx["diff_total"],
                    "ts": now.isoformat(),
                }
                all_hits.append(hit)

                # Accumulate in state for persistent live display (#70)
                cond_hits_key = "cond_hits_{}".format(match_id)
                if cond_hits_key not in state:
                    state[cond_hits_key] = []
                state[cond_hits_key].append(hit)
                state_dirty = True

                # Send alert if configured (#92)
                _alert_mode = _get_alert_mode(cond)
                if _alert_mode in ("slack", "history"):
                    emoji = COLOR_EMOJI.get(cond.get("color", ""), "⚡")
                    alert_time = _format_alert_time(now)
                    msg_lines = [
                        "{} *{}*".format(emoji, cond_name),
                        "*{}* — {}".format(team_name, result.get("direction", "")),
                        "{} {} - {} {}".format(
                            home_team, game.get("home_score", 0),
                            game.get("away_score", 0), away_team
                        ),
                        "_{} | {}_".format(game.get("period", ""), alert_time),
                    ]
                    message = "\n".join(msg_lines)

                    if _alert_mode == "slack":
                        success = send_alert(config, message)
                    else:
                        success = True
                    hit["alerted"] = success
                    if _alert_mode == "slack" and not success:
                        _log("WARN: Alert delivery failed for", cond_name, team_name)

                    # Log to alert history
                    update_alert_log({
                        "ts": now.isoformat(),
                        "team": team_name,
                        "condition": cond_name,
                        "type": cond.get("type", ""),
                        "color": cond.get("color", ""),
                        "direction": result.get("direction", ""),
                        "score": hit["score"],
                        "period": game.get("period", ""),
                        "game_id": match_id,
                        "success": success,
                    })

                # Update cooldown state
                state[state_key] = now.isoformat()
                state_dirty = True

        # ── Adaptive live odds polling ────────────────────────────────────────
        live_odds = None
        if _should_poll_odds(game, match_id, state, config, now):
            live_odds = _poll_live_odds(game, match_id, config, state, now)
            if live_odds:
                state["last_live_odds_{}".format(match_id)] = dict(live_odds)
                state_dirty = True
        # Use last polled odds as fallback for BK fields on PW calls
        if not live_odds:
            live_odds = state.get("last_live_odds_{}".format(match_id))

        # ── Capture kickoff time (once per game) ──────────────────────────
        kickoff_key = "kickoff_utc_{}".format(match_id)
        if kickoff_key not in state and game.get("start_time"):
            state[kickoff_key] = game["start_time"]
            state_dirty = True

        # ── Capture pregame odds snapshot (once per game) (#19) ───────────
        pregame_key = "pregame_odds_{}".format(match_id)
        if pregame_key not in state and live_odds:
            state[pregame_key] = dict(live_odds)
            state_dirty = True
            _log("Pregame odds captured for {} vs {}".format(home_team, away_team))

        # ── Detect score change since last poll (#24) ─────────────────────
        score_key = "last_score_{}".format(match_id)
        current_score = "{}:{}".format(game.get("home_score", 0), game.get("away_score", 0))
        last_score = state.get(score_key, "")
        score_changed = last_score != "" and last_score != current_score
        if last_score != current_score:
            state[score_key] = current_score
            state_dirty = True
        if score_changed:
            _log("Score change detected for {} vs {}: {} -> {}".format(
                home_team, away_team, last_score, current_score))

        # ── Predicted Winner evaluation (Phase 2) ─────────────────────────
        pw_analysis = None

        # Suppress PW eval during halftime break or invalid ET (#60)
        _game_period = game.get("period", "")
        _match_state = game.get("match_state", "")
        _pw_suppressed_period = False
        if _game_period == "HT" or _match_state == "HalfTime":
            _pw_suppressed_period = True  # skip during halftime
        elif _get_game_minute(game) >= 80 and _match_state not in ("ExtraTime",) and _game_period != "ET":
            _pw_suppressed_period = True  # skip — game ended, not in ET

        force_pw = score_changed and config.get("pw_eval_on_score_change", True)
        if not _pw_suppressed_period and (force_pw or _should_evaluate_pw(game, match_id, state, config, now)):
            if force_pw:
                _log("PW eval forced by score change for {} vs {}".format(home_team, away_team))
            # Collect active conditions for this game (from all_hits)
            game_active_conds = [h for h in all_hits if h.get("game_id") == match_id]
            pw_analysis = compute_pw_analysis(game, match_detail, game_active_conds, config)

            if pw_analysis:
                state["pw_eval_{}".format(match_id)] = now.isoformat()
                state_dirty = True

                # Check each prediction against suppression gates
                # Use min of global threshold and lowest PW condition threshold
                # so PW calls always record when any alert condition would fire (#130)
                threshold = config.get("predicted_winner_threshold", 65)
                for _pw_cond in pw_conditions:
                    _pw_t = _pw_cond.get("threshold")
                    if _pw_t is not None:
                        try:
                            threshold = min(threshold, float(_pw_t))
                        except (TypeError, ValueError):
                            pass
                for pred in pw_analysis.get("predictions", []):
                    pct = pred.get("winner_score_pct", pred.get("pct", 0))
                    if pct < threshold:
                        continue

                    # Skip 0-0 score — no scoring context (#183)
                    _hs = game.get("home_score", 0) or 0
                    _as = game.get("away_score", 0) or 0
                    if int(_hs) == 0 and int(_as) == 0:
                        _log("PW skipped for {} (0-0 score)".format(pred.get("predicted_team", "")))
                        continue

                    suppressed, reason = _check_pw_suppression(game, pred, config)
                    if suppressed:
                        _log("PW suppressed for {} ({}): {}".format(
                            pred.get("predicted_team", ""), pct, reason))
                        continue

                    # Duplicate suppression: skip if same team+score as last call (#60)
                    _pred_team = pred.get("predicted_team", "")
                    _prev_calls = state.get("pw_calls_{}".format(match_id), [])
                    if _prev_calls:
                        _last = _prev_calls[-1]
                        if (_last.get("predicted_team") == _pred_team
                                and _last.get("home_score_at_fire") == game.get("home_score", 0)
                                and _last.get("away_score_at_fire") == game.get("away_score", 0)):
                            _log("PW duplicate suppressed for {} (same team+score)".format(_pred_team))
                            continue

                    # Record PW call
                    call = _record_pw_call(game, pred, game_active_conds,
                                           config, state, now, live_odds)
                    _log("PW call: {} {:.1f}% [{}] ({})".format(
                        call["predicted_team"], call["pct"],
                        call["consensus"], call["half"]))

                    # ── Filter-driven PW alert (#263, #278, #279) ──
                    _pw_af = config.get("pw_alert_filters")
                    _pw_alert_delivered = False
                    _pw_game_name = "{} vs {}".format(home_team, away_team)
                    if not call.get("_suppressed") and _pw_af and _pw_af.get("enabled"):
                        # Stamp scenario key on call for ROI filter lookup (#278)
                        call["_scenario_key"] = outcomes.scenario_key_from_call(call) or ""
                        if _pw_call_passes_alert_filters(call, _pw_af, _pw_scenario_stats):
                            try:
                                _af_ts = _format_alert_time(now)
                                _af_stats = []
                                if call.get("home_score_at_fire") is not None:
                                    _af_stats.append("Score: {}-{}".format(
                                        call["home_score_at_fire"], call["away_score_at_fire"]))
                                if call.get("score_margin_at_fire") is not None:
                                    _af_stats.append("Margin: {:+d}".format(int(call["score_margin_at_fire"])))
                                if call.get("spread_role"):
                                    _af_stats.append(call["spread_role"])
                                if call.get("moneyline"):
                                    _af_stats.append("Odds: {}".format(call["moneyline"]))
                                if call.get("spread"):
                                    _af_stats.append("Spread: {}".format(call["spread"]))
                                if call.get("live_moneyline") and str(call["live_moneyline"]) != str(call.get("moneyline", "")):
                                    _af_stats.append("Live ML: {}".format(call["live_moneyline"]))
                                if call.get("live_spread"):
                                    _af_stats.append("Handicap: {}".format(call["live_spread"]))
                                if call.get("bk_moneyline"):
                                    _af_stats.append("BK Odds: {}".format(call["bk_moneyline"]))
                                if call.get("bk_spread"):
                                    _af_stats.append("BK Spread: {}".format(call["bk_spread"]))
                                if call.get("polarity_net") is not None:
                                    _af_stats.append("Pol: {:+d} ({}+ {}\u2212)".format(
                                        call["polarity_net"], call.get("polarity_pos", 0), call.get("polarity_neg", 0)))
                                if call.get("avg_edge") is not None:
                                    _af_stats.append("Edge: {:+.1f}".format(call["avg_edge"]))
                                # Scenario stats line (#279)
                                _af_sc_key = call.get("_scenario_key")
                                _af_sc = _pw_scenario_stats.get(_af_sc_key) if _af_sc_key else None
                                if _af_sc:
                                    _af_sc_parts = _af_sc_key.split("|")
                                    _af_sc_label = "{} {} {} E:{}".format(
                                        "FAV" if _af_sc_parts[0] == "favorite" else "DOG",
                                        _af_sc_parts[3],
                                        "+" if _af_sc_parts[1] == "leading" else "\u2212",
                                        _af_sc_parts[2])
                                    _af_sc_ml_roi = _af_sc.get("bk_odds_roi_pct")
                                    _af_sc_ml_str = "{:+.1f}%".format(_af_sc_ml_roi) if _af_sc_ml_roi is not None else "\u2014"
                                    _af_sc_spr_roi = _af_sc.get("bk_spr_roi_pct")
                                    _af_sc_spr_str = "{:+.1f}%".format(_af_sc_spr_roi) if _af_sc_spr_roi is not None else "\u2014"
                                    _af_sc_cov = _af_sc.get("bk_spr_covered", 0)
                                    _af_sc_spr_n = _af_sc.get("bk_spr_count", 0)
                                    _af_stats.append(
                                        "Scenario: {} W:{}/{} ML:{} Cov:{}/{} Spr:{}".format(
                                            _af_sc_label, _af_sc["correct"], _af_sc["total_calls"],
                                            _af_sc_ml_str, _af_sc_cov, _af_sc_spr_n, _af_sc_spr_str))
                                _af_msg = (
                                    "_{ts}_\n"
                                    "\U0001f3c9 :dart: *Predicted Winner* \u2014 {pred}\n"
                                    "{pct} win probability{blended}{consensus}\n"
                                    "Game: {game} \u00b7 {quarter}\n"
                                    "{stats}"
                                ).format(
                                    ts=_af_ts,
                                    pred=call["predicted_team"],
                                    pct="{:.0f}%".format(call["pct"]),
                                    blended=" \u00b7 blended" if call.get("blended") else "",
                                    consensus=" \u00b7 consensus: {}".format((call.get("consensus") or "").replace("_", " ")) if call.get("consensus") else "",
                                    game=_pw_game_name,
                                    quarter=call.get("quarter", ""),
                                    stats=" \u00b7 ".join(_af_stats),
                                )
                                _af_ok = send_alert(config, _af_msg)
                                if _af_ok:
                                    _pw_alert_delivered = True
                                _log("[PW_FILTER_ALERT] {} — {} {:.1f}% [{}] sent={}".format(
                                    _pw_game_name, call["predicted_team"], call["pct"],
                                    call.get("consensus"), _af_ok))
                            except Exception as _af_ex:
                                _log("[PW_FILTER_ALERT] Error: {}".format(_af_ex))
                        else:
                            _log("[PW_ALERT_FILTERED] {} — {} {:.1f}% — did not pass alert filters".format(
                                _pw_game_name, call["predicted_team"], call["pct"]))

                    # ── PW Underdog Alert (#92) ──
                    # Skip underdog alerts when filter-driven alerts are active (#263)
                    if call.get("spread_role") == "underdog" and not call.get("_suppressed") and not (config.get("pw_alert_filters") or {}).get("enabled", False):
                        _pua_conds = [c for c in config.get("conditions", [])
                                      if c.get("type") == "pw_underdog_alert"]
                        for _pua in _pua_conds:
                            _pua_mode = _get_alert_mode(_pua)
                            if _pua_mode == "none":
                                continue
                            _pua_quarters = _pua.get("quarters", ["Q3", "Q4"])
                            if call.get("quarter", "") not in _pua_quarters:
                                continue
                            _pua_min = float(_pua.get("min_confidence", 60))
                            if call["pct"] < _pua_min:
                                continue
                            _pua_max = int(_pua.get("max_alerts_per_quarter_per_team", 1))
                            _pua_key = "pua_{}_{}_{}_{}_count".format(
                                _pua["name"], match_id, call["predicted_team"], call["quarter"])
                            _pua_count = int(state.get(_pua_key, 0) or 0)
                            if _pua_count >= _pua_max:
                                continue
                            state[_pua_key] = _pua_count + 1
                            state_dirty = True

                            _pua_msg = "\n".join([
                                "🐕 *PW Underdog Alert* — *{}*".format(call["predicted_team"]),
                                "*{:.1f}%* confidence | {} | {}".format(
                                    call["pct"], call.get("consensus", ""), call.get("quarter", "")),
                                "🏉 {} {} - {} {} ({} {})".format(
                                    home_team, game.get("home_score", 0),
                                    game.get("away_score", 0), away_team,
                                    game.get("period", ""), game.get("clock", "")),
                                "Margin: {:+d} | Edge: {:.1f} | Role: underdog".format(
                                    call.get("score_margin_at_fire", 0), call.get("avg_edge") or 0),
                                "_{}_".format(_format_alert_time(now)),
                            ])
                            _log("[ALERT] [PW_UNDERDOG]", call["predicted_team"],
                                 call["quarter"], "{:.1f}%".format(call["pct"]),
                                 "[mode:{}]".format(_pua_mode))
                            _pua_ok = True
                            if _pua_mode == "slack":
                                _pua_ok = send_alert(config, _pua_msg)
                            update_alert_log({
                                "ts": now.isoformat(),
                                "team": call["predicted_team"],
                                "condition": _pua["name"],
                                "type": "pw_underdog_alert",
                                "color": _pua.get("color", "orange"),
                                "direction": "underdog",
                                "score": "{}-{}".format(game.get("home_score", 0), game.get("away_score", 0)),
                                "period": game.get("period", ""),
                                "game_id": match_id,
                                "success": _pua_ok,
                                "pw_pct": call["pct"],
                                "pw_consensus": call.get("consensus", ""),
                            })

                    # Write PW call to alert history (#279)
                    # When pw_alert_filters is enabled, only log if the call passed filters.
                    # When disabled, log all non-suppressed calls (existing behavior).
                    _pw_should_log = _pw_alert_delivered if (config.get("pw_alert_filters") or {}).get("enabled") else True
                    if not call.get("_suppressed") and _pw_should_log:
                        # Build scenario stats line for direction field
                        _pw_dir_parts = []
                        _pw_sc_key = call.get("_scenario_key") or outcomes.scenario_key_from_call(call) or ""
                        _pw_sc = _pw_scenario_stats.get(_pw_sc_key) if _pw_sc_key else None
                        if _pw_sc:
                            _sc_parts = _pw_sc_key.split("|")
                            _sc_label = "{} {} {} E:{}".format(
                                "FAV" if _sc_parts[0] == "favorite" else "DOG",
                                _sc_parts[3],
                                "+" if _sc_parts[1] == "leading" else "\u2212",
                                _sc_parts[2])
                            _sc_ml_roi = _pw_sc.get("bk_odds_roi_pct")
                            _sc_ml_str = "{:+.1f}%".format(_sc_ml_roi) if _sc_ml_roi is not None else "\u2014"
                            _sc_spr_roi = _pw_sc.get("bk_spr_roi_pct")
                            _sc_spr_str = "{:+.1f}%".format(_sc_spr_roi) if _sc_spr_roi is not None else "\u2014"
                            _sc_cov = _pw_sc.get("bk_spr_covered", 0)
                            _sc_spr_n = _pw_sc.get("bk_spr_count", 0)
                            _pw_dir_parts.append(
                                "Scenario: {} W:{}/{} ML:{} Cov:{}/{} Spr:{}".format(
                                    _sc_label, _pw_sc["correct"], _pw_sc["total_calls"],
                                    _sc_ml_str, _sc_cov, _sc_spr_n, _sc_spr_str))
                        update_alert_log({
                            "ts":          now.isoformat(),
                            "type":        "predicted_winner",
                            "team":        call["predicted_team"],
                            "predicted_team": call["predicted_team"],
                            "condition":   "Predicted Winner",
                            "color":       "green",
                            "score_str":   "{:.0f}%".format(call["pct"]),
                            "quarter_str": call.get("quarter", ""),
                            "game_clock":  call.get("game_clock", ""),
                            "direction":   " \u00b7 ".join(_pw_dir_parts) if _pw_dir_parts else "",
                            "game_id":     match_id,
                            "game_name":   _pw_game_name,
                            "pct":         round(call["pct"], 1),
                            "consensus":   call.get("consensus", ""),
                            "blended":     call.get("blended", False),
                            "basis":       call.get("basis", ""),
                            "score_margin_at_fire": call.get("score_margin_at_fire"),
                            "home_score_at_fire": call.get("home_score_at_fire"),
                            "away_score_at_fire": call.get("away_score_at_fire"),
                            "spread_role": call.get("spread_role", ""),
                            "live_moneyline": call.get("live_moneyline", ""),
                            "live_spread": call.get("live_spread", ""),
                            "polarity_pos": call.get("polarity_pos", 0),
                            "polarity_neg": call.get("polarity_neg", 0),
                            "polarity_net": call.get("polarity_net", 0),
                            "avg_edge":    call.get("avg_edge"),
                            "bk_moneyline": call.get("bk_moneyline", ""),
                            "bk_spread":   call.get("bk_spread", ""),
                            "bk_spread_price": call.get("bk_spread_price"),
                            "bk_name":     call.get("bk_name", ""),
                            "delivery_ok": True,
                        })

                # Evaluate PW threshold conditions (2nd pass)
                # Skip when filter-driven alerts are active (#263)
                _paf_active = (config.get("pw_alert_filters") or {}).get("enabled", False)
                if _paf_active:
                    pw_conditions = []
                for side in ("home", "away"):
                    team_ctx = build_team_context(game, match_detail, side, pbp_features)
                    team_name = team_ctx["team_name"]
                    if not monitor_all:
                        if not any(f in team_name.lower() for f in team_filters):
                            continue

                    pw_context = {
                        "game_id": match_id,
                        "period": game.get("period", ""),
                        "state": state,
                        "match_detail": match_detail,
                        "live_analysis": pw_analysis,
                    }

                    for cond in pw_conditions:
                        cond_name = cond.get("name", "")
                        result = condition_logic.evaluate_condition(cond, team_ctx, pw_context)
                        if not result.get("matched"):
                            continue

                        # Cooldown / alert_once check
                        state_key = get_state_key(team_name, cond_name, match_id)
                        alert_once = cond.get("alert_once", "")
                        if alert_once == "game" and state.get(state_key):
                            continue
                        last_fired = state.get(state_key)
                        if last_fired and alert_once != "game":
                            try:
                                last_dt = datetime.fromisoformat(last_fired)
                                if (now - last_dt).total_seconds() < cooldown_secs:
                                    continue
                            except Exception:
                                pass

                        hit = {
                            "team": team_name,
                            "team_abbrev": nrl_api.get_team_abbrev(team_name),
                            "condition": cond_name,
                            "type": cond.get("type", ""),
                            "color": cond.get("color", ""),
                            "direction": result.get("direction", ""),
                            "game_id": match_id,
                            "home_team": home_team,
                            "away_team": away_team,
                            "score": "{} {} - {} {}".format(
                                home_team, game.get("home_score", 0),
                                game.get("away_score", 0), away_team
                            ),
                            "period": game.get("period", ""),
                            "score_margin": team_ctx["diff_total"],
                            "pw_pct": result.get("win_probability_pct"),
                            "pw_consensus": result.get("consensus"),
                        }
                        all_hits.append(hit)

                        # Send PW alert (enhanced formatting #15 Step 17, #92 alert_mode)
                        _pw_alert_mode = _get_alert_mode(cond)
                        if _pw_alert_mode in ("slack", "history"):
                            pct = result.get("win_probability_pct", 0)
                            consensus_str = result.get("consensus", "")
                            basis_str = result.get("basis", "")
                            conds_count = result.get("conditions_count", 0)
                            alert_time = _format_alert_time(now)
                            game_half = _get_game_half(game)

                            # Get active condition names for this team
                            team_cond_names = [h["condition"] for h in all_hits
                                               if h.get("game_id") == match_id
                                               and h.get("team") == team_name][:5]

                            # Spread role from latest PW call
                            spread_info = ""
                            pw_calls = state.get("pw_calls_{}".format(match_id), [])
                            if pw_calls:
                                last_call = pw_calls[-1]
                                role = last_call.get("spread_role", "")
                                spread = last_call.get("live_spread", "")
                                if role and spread:
                                    spread_info = "\n⚡ Spread: {} {} ({})".format(
                                        team_name, spread, role.title())

                            msg_lines = [
                                "🎯 *NRL Predicted Winner: {} ({:.1f}%)*".format(team_name, pct),
                                "📊 Consensus: {} | Basis: {} ({} conditions)".format(
                                    consensus_str.title() if consensus_str else "N/A",
                                    basis_str or "N/A",
                                    conds_count or len(team_cond_names)),
                                "🏉 {} {} - {} {} ({} {})".format(
                                    home_team, game.get("home_score", 0),
                                    game.get("away_score", 0), away_team,
                                    game_half, game.get("clock", "")),
                            ]
                            if team_cond_names:
                                msg_lines.append("📈 Conditions: {}".format(
                                    ", ".join(team_cond_names)))
                            if spread_info:
                                msg_lines.append(spread_info)
                            # Scenario stats (NRL #191)
                            if pw_calls and _pw_scenario_stats:
                                _pw_sc_key = outcomes.scenario_key_from_call(pw_calls[-1])
                                _pw_sc = _pw_scenario_stats.get(_pw_sc_key) if _pw_sc_key else None
                                if _pw_sc:
                                    _sc_parts = _pw_sc_key.split("|")
                                    _sc_label = "{} {} {} E:{}".format(
                                        "FAV" if _sc_parts[0] == "favorite" else "DOG",
                                        _sc_parts[3],
                                        "+" if _sc_parts[1] == "leading" else "\u2212",
                                        _sc_parts[2])
                                    _sc_ml_roi = _pw_sc.get("bk_odds_roi_pct")
                                    _sc_ml_str = "{:+.1f}%".format(_sc_ml_roi) if _sc_ml_roi is not None else "\u2014"
                                    _sc_spr_roi = _pw_sc.get("bk_spr_roi_pct")
                                    _sc_spr_str = "{:+.1f}%".format(_sc_spr_roi) if _sc_spr_roi is not None else "\u2014"
                                    _sc_cov = _pw_sc.get("bk_spr_covered", "?")
                                    _sc_spr_n = _pw_sc.get("bk_spr_count", "?")
                                    msg_lines.append("📐 Scenario: {} W:{}/{} ML:{} Cov:{}/{} Spr:{}".format(
                                        _sc_label, _pw_sc["correct"], _pw_sc["total_calls"],
                                        _sc_ml_str, _sc_cov, _sc_spr_n, _sc_spr_str))
                            msg_lines.append("_{}_".format(alert_time))
                            message = "\n".join(msg_lines)
                            if _pw_alert_mode == "slack":
                                success = send_alert(config, message)
                            else:
                                success = True
                            hit["alerted"] = success

                            update_alert_log({
                                "ts": now.isoformat(),
                                "team": team_name,
                                "condition": cond_name,
                                "type": "predicted_winner_threshold",
                                "color": cond.get("color", ""),
                                "direction": result.get("direction", ""),
                                "score": hit["score"],
                                "period": game.get("period", ""),
                                "game_id": match_id,
                                "success": success,
                                "pw_pct": pct,
                                "pw_consensus": consensus_str,
                            })

                        state[state_key] = now.isoformat()
                        state_dirty = True

        # Collect live stats for dashboard
        live_stat = {
            "match_id": match_id,
            "home_team": home_team,
            "away_team": away_team,
            "home_score": game.get("home_score", 0),
            "away_score": game.get("away_score", 0),
            "period": game.get("period", ""),
            "clock": game.get("clock", ""),
            "match_state": game.get("match_state", ""),
            "is_live": True,
            "competition": game.get("competition", "nrl_premiership"),
            "soo_game": game.get("soo_game", 0),
            "round_title": game.get("round_title", ""),
        }
        if match_detail:
            _h_stats = match_detail.get("home_stats", {})
            _a_stats = match_detail.get("away_stats", {})
            # Check if API stats have real data (beyond scoring breakdown)
            _API_STAT_KEYS = {"possession", "completionRate", "errors",
                              "allRunMetres", "lineBreaks", "missedTackles"}
            _has_api_stats = any(k in _h_stats for k in _API_STAT_KEYS)
            if not _has_api_stats and pbp_features:
                # Derive partial stats from PBP timeline (#297)
                _h_stats = dict(_h_stats)
                _a_stats = dict(_a_stats)
                _h_stats["errors"] = pbp_features.get("home_errors_timeline", 0)
                _a_stats["errors"] = pbp_features.get("away_errors_timeline", 0)
                _h_stats["penaltiesConceded"] = pbp_features.get("home_penalties_timeline", 0)
                _a_stats["penaltiesConceded"] = pbp_features.get("away_penalties_timeline", 0)
                _h_stats["lineBreaks"] = pbp_features.get("home_line_breaks_timeline", 0)
                _a_stats["lineBreaks"] = pbp_features.get("away_line_breaks_timeline", 0)
                live_stat["stats_source"] = "pbp"
            else:
                live_stat["stats_source"] = "api" if _has_api_stats else "none"
            live_stat["home_stats"] = _h_stats
            live_stat["away_stats"] = _a_stats
            live_stat["home_score_1h"] = match_detail.get("home_score_1h")
            live_stat["away_score_1h"] = match_detail.get("away_score_1h")

            # Capture halftime stats snapshot for 1H/2H split (#125)
            _ht_key = "ht_stats_{}".format(match_id)
            _period = game.get("period", "")
            _ms = game.get("match_state", "")
            if _ht_key not in state and match_detail.get("home_stats"):
                # Capture when entering halftime or second half
                if _period == "HT" or _ms == "HalfTime":
                    state[_ht_key] = {
                        "home": dict(match_detail.get("home_stats", {})),
                        "away": dict(match_detail.get("away_stats", {})),
                    }
                    state_dirty = True
                    _log("Captured halftime stats for {} ({})".format(match_id, home_team + " vs " + away_team))

            # Capture Q1 boundary stats at ~minute 20 (#225)
            _q1_key = "q1_stats_{}".format(match_id)
            _gm = _get_game_minute(game)
            if _q1_key not in state and match_detail.get("home_stats") and 18 <= _gm <= 25:
                state[_q1_key] = {
                    "home": dict(match_detail.get("home_stats", {})),
                    "away": dict(match_detail.get("away_stats", {})),
                    "minute": _gm,
                }
                state_dirty = True
                _log("Captured Q1 boundary stats at min {} for {} ({})".format(
                    _gm, match_id, home_team + " vs " + away_team))

            # Capture Q3 boundary stats at ~minute 60 (#225)
            _q3_key = "q3_stats_{}".format(match_id)
            if _q3_key not in state and match_detail.get("home_stats") and 58 <= _gm <= 65:
                if _period in ("2H",) or _ms in ("SecondHalf",):
                    state[_q3_key] = {
                        "home": dict(match_detail.get("home_stats", {})),
                        "away": dict(match_detail.get("away_stats", {})),
                        "minute": _gm,
                    }
                    state_dirty = True
                    _log("Captured Q3 boundary stats at min {} for {} ({})".format(
                        _gm, match_id, home_team + " vs " + away_team))

            # Count-based stat keys for quarter subtraction
            _COUNT_KEYS = (
                "errors", "penalties", "penaltiesConceded",
                "missedTackles", "allRunMetres", "postContactMetres",
                "lineBreaks", "tackleBreaks", "offloads", "intercepts",
                "tries", "goals", "fieldGoals",
            )
            # Rate-based stat keys — shown as cumulative at boundary (#225)
            _RATE_KEYS = (
                "possession", "completionRate", "effectiveTacklePct", "playTheBallSpeed",
            )

            def _subtract_stats(current, baseline):
                """Subtract baseline count stats from current. Include rate stats from current."""
                result = {}
                for _sk in _COUNT_KEYS:
                    _cv = current.get(_sk)
                    _bv = baseline.get(_sk)
                    if _cv is not None and _bv is not None:
                        try:
                            result[_sk] = int(_cv) - int(_bv)
                        except (ValueError, TypeError):
                            pass
                return result

            # Include 1H/2H stats in live_stat if halftime snapshot exists
            _ht_snap = state.get(_ht_key)
            if _ht_snap:
                live_stat["home_stats_1h"] = _ht_snap.get("home", {})
                live_stat["away_stats_1h"] = _ht_snap.get("away", {})
                # Compute 2H stats = current - 1H
                if _period in ("2H", "ET") or _ms in ("SecondHalf", "ExtraTime"):
                    _cur_h = match_detail.get("home_stats", {})
                    _cur_a = match_detail.get("away_stats", {})
                    _ht_h = _ht_snap.get("home", {})
                    _ht_a = _ht_snap.get("away", {})
                    live_stat["home_stats_2h"] = _subtract_stats(_cur_h, _ht_h)
                    live_stat["away_stats_2h"] = _subtract_stats(_cur_a, _ht_a)

            # Include Q1-Q4 stats (#225)
            _q1_snap = state.get(_q1_key)
            _q3_snap = state.get(_q3_key)

            # Q1: stats at Q1 boundary (cumulative at ~min 20)
            if _q1_snap:
                live_stat["home_stats_q1"] = _q1_snap.get("home", {})
                live_stat["away_stats_q1"] = _q1_snap.get("away", {})
                live_stat["_q1_minute"] = _q1_snap.get("minute", 20)
                live_stat["_q1_approx"] = _q1_snap.get("minute", 20) != 20

            # Q2: halftime - Q1 (count stats only) + halftime rates
            if _q1_snap and _ht_snap:
                _q2_h = _subtract_stats(_ht_snap.get("home", {}), _q1_snap.get("home", {}))
                _q2_a = _subtract_stats(_ht_snap.get("away", {}), _q1_snap.get("away", {}))
                # Add rate stats from halftime as cumulative-at-boundary
                for _rk in _RATE_KEYS:
                    _rv_h = _ht_snap.get("home", {}).get(_rk)
                    _rv_a = _ht_snap.get("away", {}).get(_rk)
                    if _rv_h is not None:
                        _q2_h[_rk] = _rv_h
                    if _rv_a is not None:
                        _q2_a[_rk] = _rv_a
                live_stat["home_stats_q2"] = _q2_h
                live_stat["away_stats_q2"] = _q2_a

            # Q3: Q3 boundary - halftime (count stats only) + Q3 boundary rates
            if _ht_snap and _q3_snap:
                _q3_h = _subtract_stats(_q3_snap.get("home", {}), _ht_snap.get("home", {}))
                _q3_a = _subtract_stats(_q3_snap.get("away", {}), _ht_snap.get("away", {}))
                for _rk in _RATE_KEYS:
                    _rv_h = _q3_snap.get("home", {}).get(_rk)
                    _rv_a = _q3_snap.get("away", {}).get(_rk)
                    if _rv_h is not None:
                        _q3_h[_rk] = _rv_h
                    if _rv_a is not None:
                        _q3_a[_rk] = _rv_a
                live_stat["home_stats_q3"] = _q3_h
                live_stat["away_stats_q3"] = _q3_a
                live_stat["_q3_minute"] = _q3_snap.get("minute", 60)
                live_stat["_q3_approx"] = _q3_snap.get("minute", 60) != 60

            # Q4: current - Q3 boundary (count stats only, 2H only) + current rates
            if _q3_snap and (_period in ("2H", "ET") or _ms in ("SecondHalf", "ExtraTime")):
                _cur_h = match_detail.get("home_stats", {})
                _cur_a = match_detail.get("away_stats", {})
                _q4_h = _subtract_stats(_cur_h, _q3_snap.get("home", {}))
                _q4_a = _subtract_stats(_cur_a, _q3_snap.get("away", {}))
                for _rk in _RATE_KEYS:
                    _rv_h = _cur_h.get(_rk)
                    _rv_a = _cur_a.get(_rk)
                    if _rv_h is not None:
                        _q4_h[_rk] = _rv_h
                    if _rv_a is not None:
                        _q4_a[_rk] = _rv_a
                live_stat["home_stats_q4"] = _q4_h
                live_stat["away_stats_q4"] = _q4_a
        # Try/scoring timeline from raw match data (#70)
        if raw:
            try:
                _players = {}
                for _tk in ("homeTeam", "awayTeam"):
                    for _p in raw.get(_tk, {}).get("players", []):
                        _pid = _p.get("playerId")
                        if _pid:
                            _players[_pid] = "{} {}".format(_p.get("firstName", ""), _p.get("lastName", "")).strip()
                _home_tid = str(raw.get("homeTeam", {}).get("teamId", ""))
                _scoring = []
                for _ev in raw.get("timeline", []):
                    _etype = (_ev.get("type") or "").lower()
                    if _etype not in ("try", "penalty try", "penalty goal", "field goal", "conversion"):
                        continue
                    _tid = str(_ev.get("teamId", ""))
                    _team = home_team if _tid == _home_tid else away_team
                    _gs = _ev.get("gameSeconds", 0)
                    _min = _gs // 60 if _gs else 0
                    _scoring.append({
                        "team": _team,
                        "team_abbrev": nrl_api.get_team_abbrev(_team),
                        "player": _players.get(_ev.get("playerId"), ""),
                        "game_seconds": _gs,
                        "minute": _min,
                        "type": _ev.get("type", "Try"),
                    })
                if _scoring:
                    live_stat["scoring_events"] = _scoring
            except Exception:
                pass
        # Conditions fired for this game — use accumulated state (#70)
        cond_hits_key = "cond_hits_{}".format(match_id)
        accumulated_conds = state.get(cond_hits_key, [])
        if accumulated_conds:
            # Deduplicate: keep latest fire per team+condition (#106)
            _seen = {}
            for _ac in accumulated_conds:
                _dedup_key = (_ac.get("team", "") + "|" + _ac.get("condition", ""))
                _seen[_dedup_key] = _ac  # last one wins
            live_stat["conditions_fired"] = list(_seen.values())
        # Live odds (from this cycle or pregame snapshot)
        if live_odds:
            live_stat["live_odds"] = live_odds
        pregame = state.get("pregame_odds_{}".format(match_id))
        if pregame:
            live_stat["pregame_odds"] = pregame
        # PW analysis and calls (always include from state, not just when eval fires)
        if pw_analysis:
            live_stat["pw_analysis"] = pw_analysis
        pw_calls_state = state.get("pw_calls_{}".format(match_id), [])
        if pw_calls_state:
            live_stat["pw_calls"] = pw_calls_state[-5:]
        # Synthetic odds (#235)
        try:
            import synthetic_odds
            _synth_margin = (game.get("home_score", 0) or 0) - (game.get("away_score", 0) or 0)
            _synth_quarter = _quarter_from_minute(_get_game_minute(game))
            _synth_pregame_ml = None
            _synth_pregame = state.get("pregame_odds_{}".format(match_id))
            if _synth_pregame:
                _synth_pregame_ml = _synth_pregame.get("home_ml")
            _synth_game_secs = int((_get_game_minute(game) or 0) * 60)
            _synth = synthetic_odds.compute_all(_synth_margin, _synth_quarter, _synth_pregame_ml, game_seconds=_synth_game_secs)
            live_stat["synth_odds"] = _synth
        except Exception:
            pass

        live_stats.append(live_stat)

    # ── Evaluate compound conditions ──────────────────────────────────────────

    for compound in compound_conditions:
        comp_name = compound.get("name", "")
        operator = compound.get("operator", "AND")
        refs = compound.get("condition_refs", [])
        suppress_base = compound.get("suppress_base_alerts", False)

        # Group hits by game+team
        from collections import defaultdict
        hits_by_scope = defaultdict(list)
        for hit in all_hits:
            key = (hit["game_id"], hit["team"])
            hits_by_scope[key].append(hit)

        for scope_key, scope_hits in hits_by_scope.items():
            hit_names = {h["condition"] for h in scope_hits}

            if operator == "AND":
                matched = all(ref in hit_names for ref in refs)
            else:  # OR
                matched = any(ref in hit_names for ref in refs)

            if matched:
                # Use first matching hit for context
                sample = scope_hits[0]
                _log("Compound '{}' matched for {} in game {}".format(
                    comp_name, scope_key[1], scope_key[0]))

                if suppress_base:
                    # Remove base hits that were consumed
                    all_hits = [h for h in all_hits if not (
                        h["game_id"] == scope_key[0] and
                        h["team"] == scope_key[1] and
                        h["condition"] in refs
                    )]

    # ── Handle completed games ────────────────────────────────────────────────

    if end_of_game:
        for game in completed_games:
            match_id = game.get("match_id")
            end_key = "game_end_{}".format(match_id)
            if state.get(end_key):
                continue

            home_team = game.get("home_team", "")
            away_team = game.get("away_team", "")
            home_score = game.get("home_score", 0)
            away_score = game.get("away_score", 0)

            # Check if monitored
            if not monitor_all:
                home_match = any(f in home_team.lower() for f in team_filters)
                away_match = any(f in away_team.lower() for f in team_filters)
                if not home_match and not away_match:
                    continue

            # Send final score alert
            margin = abs(home_score - away_score)
            winner = home_team if home_score > away_score else away_team
            msg = "🏁 *Full Time*\n{} {} - {} {}\n_{} wins by {}_".format(
                home_team, home_score, away_score, away_team,
                winner, margin
            )
            if home_score == away_score:
                msg = "🏁 *Full Time — Draw*\n{} {} - {} {}".format(
                    home_team, home_score, away_score, away_team
                )

            success = send_alert(config, msg)
            update_alert_log({
                "ts": now.isoformat(),
                "type": "final",
                "home_team": home_team,
                "away_team": away_team,
                "home_score": home_score,
                "away_score": away_score,
                "game_id": match_id,
                "success": success,
            })

            # Record game in history — full enriched record (#63)
            _game_round = game.get("_round", current_round)  # recovered games carry their own round (#144)
            record = {
                "match_id": match_id,
                "round": _game_round,
                "round_title": game.get("round_title") or "Round {}".format(_game_round),
                "season": season,
                "source": "live",
                "home_team": home_team,
                "away_team": away_team,
                "home_team_id": game.get("home_team_id", ""),
                "away_team_id": game.get("away_team_id", ""),
                "home_score": home_score,
                "away_score": away_score,
                "margin": margin,
                "winner": winner if home_score != away_score else None,
                "kickoff_utc": game.get("start_time") or state.get("kickoff_utc_{}".format(match_id), ""),
                "date": (game.get("start_time") or state.get("kickoff_utc_{}".format(match_id), ""))[:10] or now.strftime("%Y-%m-%d"),
                "ts": now.isoformat(),
                "venue": game.get("venue", ""),
                "venue_city": game.get("venue_city", ""),
            }

            # Fetch match detail for stats + 1H scores
            match_url = game.get("match_centre_url", "")
            raw = None
            if match_url:
                try:
                    raw = nrl_api.get_match_data(match_url)
                    detail = nrl_api.parse_match_detail(raw)

                    # Cache raw data to nrl_cache/ for future backfill (#64)
                    try:
                        cache_dir = os.path.join(SCRIPT_DIR, "nrl_cache", str(season))
                        os.makedirs(cache_dir, exist_ok=True)
                        slug = match_url.rstrip("/").split("/")[-1]
                        cache_file = os.path.join(cache_dir, "match_{}_r{}.json".format(slug, _game_round))
                        if not os.path.exists(cache_file):
                            tmp = cache_file + ".tmp"
                            with open(tmp, "w", encoding="utf-8") as _cf:
                                json.dump(raw, _cf, indent=2, default=str)
                            os.replace(tmp, cache_file)
                    except Exception:
                        pass  # cache write failure is non-fatal
                    record["home_score_1h"] = detail.get("home_score_1h")
                    record["away_score_1h"] = detail.get("away_score_1h")
                    if detail.get("home_stats"):
                        record["home_stats"] = detail["home_stats"]
                    if detail.get("away_stats"):
                        record["away_stats"] = detail["away_stats"]
                except Exception:
                    pass

            # Try events from timeline (#63)
            if raw:
                try:
                    _players = {}
                    for _tk in ("homeTeam", "awayTeam"):
                        for _p in raw.get(_tk, {}).get("players", []):
                            _pid = _p.get("playerId")
                            if _pid:
                                _players[_pid] = "{} {}".format(_p.get("firstName", ""), _p.get("lastName", "")).strip()
                    _home_tid = str(raw.get("homeTeam", {}).get("teamId", ""))
                    _try_events = []
                    for _ev in raw.get("timeline", []):
                        if _ev.get("type", "").lower() not in ("try", "penalty try"):
                            continue
                        _tid = str(_ev.get("teamId", ""))
                        _team = home_team if _tid == _home_tid else away_team
                        _try_events.append({
                            "team": _team,
                            "player": _players.get(_ev.get("playerId"), ""),
                            "game_seconds": _ev.get("gameSeconds", 0),
                            "type": _ev.get("type", "Try"),
                        })
                    if _try_events:
                        record["try_events"] = _try_events
                        record["home_tries"] = sum(1 for t in _try_events if t["team"] == home_team)
                        record["away_tries"] = sum(1 for t in _try_events if t["team"] == away_team)
                except Exception:
                    pass

            # Build score_progression from timeline (#246)
            if raw and raw.get("timeline"):
                try:
                    _home_tid = str(raw.get("homeTeam", {}).get("teamId", ""))
                    _away_tid = str(raw.get("awayTeam", {}).get("teamId", ""))
                    _pbp = timeline_features.extract_features(raw["timeline"], _home_tid, _away_tid)
                    _sp = _pbp.get("score_progression", [])
                    if _sp:
                        record["score_progression"] = _sp
                except Exception:
                    pass

            # 2H scores
            if record.get("home_score_1h") is not None:
                record["home_score_2h"] = home_score - (record["home_score_1h"] or 0)
                record["away_score_2h"] = away_score - (record["away_score_1h"] or 0)

            # Conditions fired during this game — use accumulated state (#144)
            # cond_hits_ state tracks all conditions across the game's lifetime;
            # all_hits only has the current poll cycle's fires (usually empty at game-end)
            game_conds = state.get("cond_hits_{}".format(match_id), [])
            if not game_conds:
                game_conds = [h for h in all_hits if h.get("game_id") == match_id]
            if game_conds:
                # Deduplicate: keep first fire per team|condition (#144)
                # First fire = when threshold was crossed, matches backfill
                # game_seconds_at_fire behavior and supports PW/condition analysis
                _seen = {}
                for _c in game_conds:
                    _k = "{}|{}".format(_c.get("team", ""), _c.get("condition", ""))
                    if _k not in _seen:
                        _seen[_k] = _c
                record["conditions_fired"] = list(_seen.values())

            # PW calls from state
            pw_calls_key = "pw_calls_{}".format(match_id)
            pw_calls = state.get(pw_calls_key, [])
            if pw_calls:
                # Set correct field based on final winner
                for c in pw_calls:
                    if record["winner"]:
                        c["correct"] = c.get("predicted_team") == record["winner"]

                    # Quarter/half outcomes (#245)
                    _q = c.get("quarter", "")
                    _predicted = c.get("predicted_team", "")
                    _sp = record.get("score_progression")
                    _Q_BOUNDS = {"Q1": (0, 1200), "Q2": (1200, 2400), "Q3": (2400, 3600), "Q4": (3600, 4800)}
                    if _sp and _q in _Q_BOUNDS and _predicted:
                        _qs, _qe = _Q_BOUNDS[_q]
                        # Score at quarter start/end from score_progression
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
                        _is_pred_home = (_predicted == home_team)
                        _pred_q = _hq if _is_pred_home else _aq
                        _opp_q = _aq if _is_pred_home else _hq
                        c["q_correct"] = bool(_pred_q > _opp_q) if _pred_q != _opp_q else False
                        # q_covered: use synth_spread (NRL has no BK quarter spread)
                        _synth_spr = c.get("synth_spread")
                        if _synth_spr is not None:
                            try:
                                c["q_covered"] = (_pred_q - _opp_q + float(_synth_spr)) > 0
                            except (ValueError, TypeError):
                                pass

                        # H1 outcomes (Q1/Q2 fires only)
                        if _q in ("Q1", "Q2"):
                            _h1h = record.get("home_score_1h")
                            _a1h = record.get("away_score_1h")
                            if _h1h is not None and _a1h is not None:
                                _pred_h1 = _h1h if _is_pred_home else _a1h
                                _opp_h1 = _a1h if _is_pred_home else _h1h
                                c["h1_correct"] = bool(_pred_h1 > _opp_h1) if _pred_h1 != _opp_h1 else False
                                _synth_spr_h1 = c.get("synth_spread")
                                if _synth_spr_h1 is not None:
                                    try:
                                        c["h1_covered"] = (_pred_h1 - _opp_h1 + float(_synth_spr_h1)) > 0
                                    except (ValueError, TypeError):
                                        pass

                record["predicted_winner_calls"] = pw_calls

            # Postgame tags
            tags = []
            if margin <= 12 and margin > 0:
                tags.append("CLOSE")
            if margin > 20:
                tags.append("BLOWOUT")
            # COMEBACK: trailing at halftime but won
            h1h = record.get("home_score_1h")
            a1h = record.get("away_score_1h")
            if h1h is not None and a1h is not None and record["winner"]:
                ht_leader = home_team if h1h > a1h else (away_team if a1h > h1h else None)
                if ht_leader and ht_leader != record["winner"]:
                    tags.append("COMEBACK")
            if tags:
                record["postgame_tags"] = tags

            # Odds from state
            pregame_key = "pregame_odds_{}".format(match_id)
            pregame = state.get(pregame_key)
            if pregame:
                record["home_ml"] = pregame.get("home_ml")
                record["away_ml"] = pregame.get("away_ml")
                record["home_odds"] = pregame.get("home_ml")
                record["away_odds"] = pregame.get("away_ml")
                record["home_spread"] = pregame.get("home_spread")
                record["away_spread"] = pregame.get("away_spread")
                record["odds_bookmaker"] = pregame.get("bookmaker", "")

            # Season segment
            # Detect segment: SOO vs regular/finals (#102, #298)
            if game.get("competition") == "state_of_origin":
                record["season_segment"] = "state_of_origin"
                record["soo_game"] = game.get("soo_game", 0)
                record["round_title"] = game.get("round_title", "")
            elif game.get("_is_finals") or nrl_api._is_finals_round(_game_round):
                record["season_segment"] = "finals"
            else:
                record["season_segment"] = "regular_season"

            append_game_record(record)

            # ── Append PW calls to JSONL export (#302) ──
            if isinstance(pw_calls, list) and pw_calls:
                try:
                    _exp_path = os.path.join(SCRIPT_DIR, "pw_export.jsonl")
                    _exp_ctx = {
                        "game_id": str(record.get("match_id", "")),
                        "game_date": str(record.get("date", "")),
                        "home_team": str(record.get("home_team", "")),
                        "away_team": str(record.get("away_team", "")),
                        "home_id": str(record.get("home_team_id", "")),
                        "away_id": str(record.get("away_team_id", "")),
                        "home_score": record.get("home_score"),
                        "away_score": record.get("away_score"),
                        "winner": str(record.get("winner") or ""),
                        "round": str(record.get("round", "")),
                        "season": str(record.get("season", "")),
                        "season_segment": str(record.get("season_segment", "")),
                        "game_source": "live",
                        "game_completed": True,
                    }
                    _exp_lines = []
                    for _ec in pw_calls:
                        if _ec.get("_suppressed"):
                            continue
                        _row = dict(_ec)
                        _row.update(_exp_ctx)
                        _exp_lines.append(json.dumps(_row, separators=(",", ":")))
                    if _exp_lines:
                        with open(_exp_path, "a") as _ef:
                            _ef.write("\n".join(_exp_lines) + "\n")
                except Exception as _exp_err:
                    _log("[WARN] PW export JSONL write failed:", _exp_err)

            state[end_key] = now.isoformat()
            state_dirty = True

            # ── Game-end summary Slack alert (#270) ──
            _gsa_cfg = config.get("pw_alert_filters") or {}
            if _gsa_cfg.get("enabled") and _gsa_cfg.get("game_summary"):
                try:
                    _gsa_winner = winner if home_score != away_score else "Draw"
                    _gsa_pw = pw_calls if isinstance(pw_calls, list) else []
                    _gsa_nonsup = [c for c in _gsa_pw if not c.get("_suppressed")]
                    _gsa_correct = sum(1 for c in _gsa_nonsup if c.get("correct"))
                    _gsa_total = len(_gsa_nonsup)
                    _gsa_acc = "{}/{} correct ({:.0f}%)".format(
                        _gsa_correct, _gsa_total,
                        _gsa_correct / _gsa_total * 100) if _gsa_total else "No PW calls"
                    # BK ML units (decimal odds)
                    _gsa_ml_pnl = 0.0
                    _gsa_ml_n = 0
                    for _gc in _gsa_nonsup:
                        _gml = _gc.get("bk_moneyline")
                        if _gml is not None:
                            try:
                                _gml_f = float(_gml)
                                if _gml_f > 1:
                                    if _gc.get("correct"):
                                        _gsa_ml_pnl += 100 * (_gml_f - 1)
                                    else:
                                        _gsa_ml_pnl -= 100
                                    _gsa_ml_n += 1
                            except (ValueError, TypeError):
                                pass
                    # BK Spread units
                    _gsa_spr_pnl = 0.0
                    _gsa_spr_cov = 0
                    _gsa_spr_n = 0
                    for _gc in _gsa_nonsup:
                        _gspr = _gc.get("bk_spread")
                        _gsp = _gc.get("bk_spread_price")
                        _gm = _gc.get("score_margin_at_fire")
                        if _gspr is not None and _gm is not None:
                            try:
                                _covered = (float(_gm) + float(_gspr)) > 0
                                _juice = float(_gsp) if _gsp else 1.91
                                if _covered:
                                    _gsa_spr_cov += 1
                                    _gsa_spr_pnl += 100 * (_juice - 1)
                                else:
                                    _gsa_spr_pnl -= 100
                                _gsa_spr_n += 1
                            except (ValueError, TypeError):
                                pass
                    _gsa_parts = [
                        "\U0001f4cb *NRL Game Summary*",
                        "{} {} - {} {}".format(away_team, away_score, home_team, home_score),
                        "\U0001f3c6 {} wins by {}".format(_gsa_winner, margin) if margin else "\U0001f3c6 Draw",
                        "PW: {}".format(_gsa_acc),
                    ]
                    if _gsa_ml_n:
                        _gsa_parts.append("BK ML: {:+.1f}u".format(_gsa_ml_pnl / 100))
                    if _gsa_spr_n:
                        _gsa_parts.append("BK Spr: {}/{} covered {:+.1f}u".format(
                            _gsa_spr_cov, _gsa_spr_n, _gsa_spr_pnl / 100))
                    _gsa_msg = "\n".join(_gsa_parts)
                    _gsa_ok = send_alert(config, _gsa_msg)
                    _log("[GAME_SUMMARY_ALERT] {} {} vs {} {} sent={}".format(
                        away_team, away_score, home_team, home_score, _gsa_ok))
                except Exception as _gsa_ex:
                    _log("[GAME_SUMMARY_ALERT] Error: {}".format(_gsa_ex))

    # ── Write output files ────────────────────────────────────────────────────

    _atomic_write_json(LIVE_STATS_FILE, {
        "games": live_stats,
        "round": current_round,
        "season": season,
        "ts": now.isoformat(),
    })

    _atomic_write_json(TICKER_FILE, {
        "hits": all_hits,
        "round": current_round,
        "ts": now.isoformat(),
    })

    # ── State TTL cleanup (#136) ────────────────────────────────────────────
    # Clean up stale state keys for games no longer active
    _active_mids = {g.get("match_id") for g in games}
    _ttl_prefixes = ("cond_hits_", "pw_calls_", "pregame_odds_", "kickoff_utc_",
                     "ht_stats_", "last_live_odds_", "last_score_", "odds_poll_",
                     "pw_eval_", "competition_", "soo_game_", "round_title_")
    for _sk in list(state.keys()):
        _matched_prefix = None
        for _pfx in _ttl_prefixes:
            if _sk.startswith(_pfx):
                _matched_prefix = _pfx
                break
        if not _matched_prefix:
            continue
        _mid = _sk[len(_matched_prefix):]
        if _mid in _active_mids:
            continue  # still in current round
        if state.get("game_end_{}".format(_mid)):
            # Already recorded — clean up immediately
            del state[_sk]
            state_dirty = True
        else:
            # Not yet recorded — preserve for 48h for auto-recovery
            _age_ref = state.get("kickoff_utc_{}".format(_mid), "")
            if not _age_ref and _sk.startswith("cond_hits_"):
                _hits = state.get(_sk)
                if isinstance(_hits, list) and _hits and isinstance(_hits[0], dict):
                    _age_ref = _hits[0].get("ts", "")
            if _age_ref:
                try:
                    _dt = datetime.fromisoformat(_age_ref.replace("Z", "+00:00"))
                    if (now - _dt).total_seconds() > 172800:  # 48h
                        del state[_sk]
                        state_dirty = True
                except (ValueError, TypeError):
                    pass

    # Clean up game_end_ keys older than 7 days
    for _sk in list(state.keys()):
        if not _sk.startswith("game_end_"):
            continue
        _ts = state.get(_sk, "")
        if _ts:
            try:
                _dt = datetime.fromisoformat(_ts.replace("Z", "+00:00"))
                if (now - _dt).total_seconds() > 604800:  # 7 days
                    del state[_sk]
                    state_dirty = True
            except (ValueError, TypeError):
                pass

    if state_dirty:
        save_state(state)

    _log("Done. {} hits, {} live games, {} completed".format(
        len(all_hits), len(live_games), len(completed_games)))


if __name__ == "__main__":
    main()
