#!/usr/bin/env python3
"""
NBA Score Monitor
Supports: score_diff (per-quarter linescores), fg_percent_improve,
          turnover_rate, turnovers, points_off_turnovers,
          back_to_back, underdog_at_home, consecutive_points_run,
          h1_moneyline_edge.
Alerts via OpenClaw gateway -> Slack. Zero tokens during normal polling.
"""
import json
import os
import resource
import shutil
import sys
import time
try:
    import requests
except ImportError:
    requests = None  # Will fail gracefully if HTTP fetch is needed
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import conditions as condition_logic
import evanalytics
import h1_store
import digest
import league_config
import odds_api
import summer_league_odds as sl_odds_mod
import outcomes
import pw_trend
from espn_cache_io import cache_game_summary, cache_pbp, cache_scoreboard

SCRIPT_DIR      = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE     = league_config.CONFIG_FILE
STATE_FILE      = league_config.state_path(".alert_state.json")
LIVE_STATS_FILE = league_config.state_path("live_stats.json")
TICKER_FILE     = league_config.state_path("game_ticker.json")
LOG_DIR         = os.path.join(SCRIPT_DIR, "logs")
LOG_ROTATION_STATE_FILE = league_config.state_path(".log_rotation_state.json")
VALID_LEAGUES   = ("nba", "wnba")
_DEFAULT_LEAGUE = league_config.LEAGUE

def _espn_urls(league=_DEFAULT_LEAGUE):
    """Return (scoreboard_url, summary_url, yesterday_url) for the given league."""
    base = "https://site.api.espn.com/apis/site/v2/sports/basketball/{}".format(league)
    sb = base + "/scoreboard"
    return sb, base + "/summary?event={}", sb + "?dates={}"

ESPN_URL, SUMMARY_URL, YESTERDAY_URL = _espn_urls()
ESPN_SL_SCOREBOARD = (
    "https://site.api.espn.com/apis/site/v2/sports/basketball"
    "/nba-summer-las-vegas/scoreboard"
)

QUARTER_NAMES   = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4", 5: "OT", 6: "2OT"}
COLOR_EMOJI     = {"green": "🟢", "yellow": "🟡", "orange": "🟠", "red": "🔴"}
STAT_COND_TYPES = {
    "fg_percent_improve", "turnover_rate", "turnovers",
    "points_off_turnovers", "back_to_back", "underdog_at_home",
    "consecutive_points_run", "h1_moneyline_edge"
}
PRE_GAME_SNAPSHOT_CONDITION_TYPES = {"back_to_back", "h1_moneyline_edge"}
LOG_ROTATION_PERIODS = {"daily", "weekly", "monthly", "days"}

# Team abbreviation → IANA timezone (home venue)
_TEAM_TZ = {
    # NBA — Eastern
    "ATL": "America/New_York", "BOS": "America/New_York", "BKN": "America/New_York",
    "CHA": "America/New_York", "CLE": "America/New_York", "DET": "America/New_York",
    "IND": "America/New_York", "MIA": "America/New_York", "NYK": "America/New_York",
    "ORL": "America/New_York", "PHI": "America/New_York", "TOR": "America/Toronto",
    "WAS": "America/New_York",
    # NBA — Central
    "CHI": "America/Chicago", "DAL": "America/Chicago", "HOU": "America/Chicago",
    "MEM": "America/Chicago", "MIL": "America/Chicago", "MIN": "America/Chicago",
    "NOP": "America/Chicago", "NO": "America/Chicago", "OKC": "America/Chicago",
    "SA": "America/Chicago", "SAS": "America/Chicago",
    # NBA — Mountain
    "DEN": "America/Denver", "UTA": "America/Denver",
    # NBA — Pacific
    "GS": "America/Los_Angeles", "GSW": "America/Los_Angeles", "LAC": "America/Los_Angeles",
    "LAL": "America/Los_Angeles", "PHX": "America/Phoenix", "POR": "America/Los_Angeles",
    "SAC": "America/Los_Angeles", "SEA": "America/Los_Angeles",
    # WNBA
    "NY": "America/New_York", "CON": "America/New_York", "WSH": "America/New_York",
    "LA": "America/Los_Angeles", "LV": "America/Los_Angeles",
}
_HOST_TZ = "Australia/Sydney"


def _format_alert_time(utc_dt, home_abbr):
    """Format timestamp as 'Day DD Mon HH:MM TZ (Day DD Mon HH:MM TZ)' with venue first, host second."""
    _fmt = "%a %d %b %H:%M %Z"
    try:
        host_zi = ZoneInfo(_HOST_TZ)
        host_dt = utc_dt.astimezone(host_zi)
        host_str = host_dt.strftime(_fmt)
    except Exception:
        host_str = utc_dt.strftime("%a %d %b %H:%M UTC")
    venue_tz = _TEAM_TZ.get((home_abbr or "").upper(), "")
    if venue_tz:
        try:
            venue_zi = ZoneInfo(venue_tz)
            venue_dt = utc_dt.astimezone(venue_zi)
            venue_str = venue_dt.strftime(_fmt)
            if venue_str != host_str:
                return "{} ({})".format(venue_str, host_str)
            return venue_str
        except Exception:
            pass
    return host_str
DISK_WARN_FREE_MB = 1024
DISK_CRITICAL_FREE_MB = 256

# Season boundary dates (UTC calendar dates), keyed by league.
# Keep these tables updated annually before playoffs begin.
ALL_STAR_BREAK_DATES = {
    "nba": {
        "2022-23": "2023-02-17",
        "2023-24": "2024-02-16",
        "2024-25": "2025-02-14",
        "2025-26": "2026-02-13",
    },
    "wnba": {
        "2023": "2023-07-15",
        "2024": "2024-07-20",
        "2025": "2025-07-19",
    },
}

REGULAR_SEASON_END_DATES = {
    "nba": {
        "2022-23": "2023-04-09",
        "2023-24": "2024-04-14",
        "2024-25": "2025-04-13",
        "2025-26": "2026-04-12",
    },
    "wnba": {
        "2023": "2023-09-10",
        "2024": "2024-09-19",
        "2025": "2025-09-12",
    },
}

PLAYOFFS_START_DATES = {
    "nba": {
        "2022-23": "2023-04-15",
        "2023-24": "2024-04-20",
        "2024-25": "2025-04-19",
        "2025-26": "2026-04-16",
    },
    "wnba": {
        "2023": "2023-09-13",
        "2024": "2024-09-22",
        "2025": "2025-09-14",
    },
}

FINALS_START_DATES = {
    "nba": {
        "2022-23": "2023-06-01",
        "2023-24": "2024-06-06",
        "2024-25": "2025-06-05",
        "2025-26": "2026-06-04",
    },
    "wnba": {
        "2023": "2023-10-08",
        "2024": "2024-10-10",
        "2025": "2025-10-09",
    },
}


# ── Logging helper ─────────────────────────────────────────────────────────────

def _get_model_schema_version():
    """Return the current ML model schema version, or None if unavailable."""
    try:
        import ml_model
        return ml_model.FEATURE_SCHEMA_VERSION
    except Exception:
        return None


def _pw_call_passes_alert_filters(call, filters, scenario_stats=None):
    """Check if a PW call passes the pw_alert_filters from config (#418, #435).

    Returns True if the call should trigger a Slack alert.
    Only evaluates real-time filters (fields on the call record itself).
    scenario_stats: optional dict of scenario_key → stats for ROI filters (#435).
    """
    if not filters or not filters.get("enabled"):
        return False
    # Quarter filter
    q = filters.get("quarter")
    if q and call.get("quarter") != q:
        return False
    # Spread role — case-insensitive (#436: ESPN pickcenter uses Title Case)
    r = filters.get("spread_role")
    if r and str(call.get("spread_role") or "").lower() != r.lower():
        return False
    # Margin at fire (leading/trailing)
    m = filters.get("margin_at_fire")
    if m:
        margin = call.get("score_margin_at_fire", 0) or 0
        if m == "leading" and margin <= 0:
            return False
        if m == "trailing" and margin > 0:
            return False
    # Half
    h = filters.get("half")
    if h:
        cq = call.get("quarter", "")
        if h == "H1" and cq not in ("Q1", "Q2"):
            return False
        if h == "H2" and cq not in ("Q3", "Q4"):
            return False
        if h == "OT" and not cq.startswith("OT") and cq != "OT":
            return False
    # Confidence band (min_pct)
    min_pct = filters.get("min_pct")
    if min_pct is not None:
        if (call.get("pct") or 0) < float(min_pct):
            return False
    # Edge range
    edge_min = filters.get("edge_min")
    edge_max = filters.get("edge_max")
    avg_edge = call.get("avg_edge")
    if edge_min is not None and (avg_edge is None or avg_edge < float(edge_min)):
        return False
    if edge_max is not None and (avg_edge is None or avg_edge > float(edge_max)):
        return False
    # Polarity
    pol = filters.get("polarity")
    if pol:
        pn = call.get("polarity_net", 0) or 0
        if pol == "positive" and pn <= 0:
            return False
        if pol == "negative" and pn >= 0:
            return False
        if pol == "neutral" and pn != 0:
            return False
    # Consensus
    cons = filters.get("consensus")
    if cons and call.get("consensus") != cons:
        return False
    # Source (Live Only) — require BK odds at fire time
    src = filters.get("source")
    if src == "live_only" and not call.get("bk_ml_source"):
        return False
    # Strict BK (#435) — require both bk_moneyline and bk_spread present
    if filters.get("strict_bk"):
        if not call.get("bk_moneyline") or call.get("bk_spread") is None:
            return False
    # Scenario ROI filters (#435)
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
        # Look up scenario stats for this call
        if not scenario_stats:
            _sc_checks.append(False)
            continue
        _sc_key = call.get("_scenario_key") or ""
        _sc = scenario_stats.get(_sc_key) if _sc_key else None
        if not _sc:
            _sc_checks.append(False)
            continue
        # Check min calls/games thresholds
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


def _log(*parts, dest=None):
    """Print a UTC-timestamped log line.  dest defaults to sys.stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = " ".join(str(p) for p in parts)
    print("{} {}".format(ts, msg), file=(dest or sys.stderr))


def parse_event_datetime_utc(event):
    """Return event datetime in UTC, falling back to now when unavailable."""
    raw = str((event or {}).get("date") or "").strip()
    if not raw:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


SEASON_START_MONTH = {"nba": 10, "wnba": 5}


def get_season_key(game_date, league="nba"):
    """Return season key string for a given date and league.

    NBA: cross-year format e.g. '2024-25' (Oct 2024 – Jun 2025).
    WNBA: single-year format e.g. '2025' (May 2025 – Oct 2025).
    """
    start_month = SEASON_START_MONTH.get(league, 10)
    year = game_date.year
    month = game_date.month
    if league == "wnba":
        return str(year if month >= start_month else year - 1)
    if month >= start_month:
        return "{}-{}".format(year, str(year + 1)[-2:])
    return "{}-{}".format(year - 1, str(year)[-2:])


def _parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except Exception:
        return None


def _is_summer_league_date(game_date, config=None):
    """Return True if game_date falls within the Summer League window.

    Checks config for override: summer_league_enabled (true/false/null).
    null = auto-detect from summer_league_window date range.
    """
    if config is None:
        config = {}
    enabled = config.get("summer_league_enabled")
    if enabled is False:
        return False
    if enabled is True:
        return True
    # Auto-detect: check if date falls within window
    window = config.get("summer_league_window", ["07-01", "07-25"])
    if not window or len(window) < 2:
        return False
    try:
        year = game_date.year
        start = datetime.strptime("{}-{}".format(year, window[0]), "%Y-%m-%d").date()
        end = datetime.strptime("{}-{}".format(year, window[1]), "%Y-%m-%d").date()
        return start <= game_date <= end
    except Exception:
        return False


def get_season_segment(game_date, league=None, config=None):
    """Classify game date into pre/post-allstar, playoffs, finals, or summer_league.

    If playoff/finals boundaries are missing for a season, this falls back to
    regular-season end and then pre/post-allstar classification.
    """
    # Summer League check (before regular classification)
    if league in (None, "nba") and _is_summer_league_date(game_date, config):
        return "summer_league"

    if league is None:
        league = _DEFAULT_LEAGUE
    season_key = get_season_key(game_date, league=league)

    finals_start = _parse_date(FINALS_START_DATES.get(league, {}).get(season_key, ""))
    if finals_start and game_date >= finals_start:
        return "finals"

    playoffs_start = _parse_date(PLAYOFFS_START_DATES.get(league, {}).get(season_key, ""))
    if playoffs_start and game_date >= playoffs_start:
        return "playoffs"

    reg_end = _parse_date(REGULAR_SEASON_END_DATES.get(league, {}).get(season_key, ""))
    if reg_end and game_date > reg_end:
        return "playoffs"

    break_date = _parse_date(ALL_STAR_BREAK_DATES.get(league, {}).get(season_key, ""))
    if break_date and game_date >= break_date:
        return "post_allstar"
    return "pre_allstar"


# ── I/O helpers ────────────────────────────────────────────────────────────────

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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def update_alert_log(entry):
    log_path = league_config.state_path("alerts.json")
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    alert_history_hours = max(1, min(168, float(cfg.get("alert_history_hours", 24))))
    cutoff = datetime.now(timezone.utc) - timedelta(hours=alert_history_hours)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%S")

    try:
        with open(log_path) as f:
            log = json.load(f)
    except Exception:
        log = []
    log.insert(0, entry)

    # Keep anchored alert types permanently (pruned by postgame_summary_hours on
    # the dashboard side).  PW alerts are retained so downstream consumers
    # (trading bot, alert history) always have the full record (#457).
    # Transient condition alerts are pruned by alert_history_hours (#157).
    _anchored_types = ("summary", "final", "predicted_winner", "pw_underdog_alert")
    retained = []
    for row in log:
        rtype = str(row.get("type") or "")
        if rtype in _anchored_types:
            retained.append(row)
            continue
        ts = str(row.get("ts") or "")
        if ts >= cutoff_iso:
            retained.append(row)

    tmp_path = log_path + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(retained, f, indent=2)
        os.replace(tmp_path, log_path)
    except Exception as e:
        _log("[WARN] Failed to write alerts log atomically:", e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _game_hits_state_key(game_id):
    return "history_hits_{}".format(game_id)


def _condition_hit_signature(entry):
    return (
        str(entry.get("type") or "").strip(),
        str(entry.get("team") or "").strip(),
        str(entry.get("condition") or "").strip(),
        str(entry.get("quarter_str") or "").strip(),
        str(entry.get("direction") or "").strip(),
        str(entry.get("game_id") or "").strip(),
        str(entry.get("ts") or "").strip(),
    )


def persist_game_condition_hit(state, entry):
    game_id = str(entry.get("game_id") or "").strip()
    entry_type = str(entry.get("type") or "").strip()
    if not game_id or entry_type in ("final", "summary"):
        return False

    state_key = _game_hits_state_key(game_id)
    existing = state.get(state_key)
    if not isinstance(existing, list):
        existing = []

    normalized = {
        "ts": str(entry.get("ts") or "").strip(),
        "type": entry_type,
        "team": str(entry.get("team") or "").strip(),
        "condition": str(entry.get("condition") or "").strip(),
        "color": str(entry.get("color") or "").strip(),
        "score_str": str(entry.get("score_str") or "").strip(),
        "quarter_str": str(entry.get("quarter_str") or "").strip(),
        "direction": str(entry.get("direction") or "").strip(),
        "game_id": game_id,
        "delivery_ok": bool(entry.get("delivery_ok")),
        "score_margin_at_fire": entry.get("score_margin_at_fire"),
        "margin_diff": entry.get("margin_diff"),
        "quarter_elapsed_pct": entry.get("quarter_elapsed_pct"),
        "game_time_seconds": entry.get("game_time_seconds"),
        "time_anchor": entry.get("time_anchor"),
        "home_away_role": entry.get("home_away_role"),
        "home_score_at_fire": entry.get("home_score_at_fire"),
        "away_score_at_fire": entry.get("away_score_at_fire"),
    }
    if entry.get("operator") is not None:
        normalized["operator"] = entry.get("operator")
    if entry.get("hits") is not None:
        normalized["hits"] = entry.get("hits")
    if entry.get("delivery_error"):
        normalized["delivery_error"] = str(entry.get("delivery_error"))

    sig = _condition_hit_signature(normalized)
    if any(_condition_hit_signature(row) == sig for row in existing if isinstance(row, dict)):
        return False

    existing.append(normalized)
    state[state_key] = existing
    return True


def get_game_condition_hits(state, game_id, alerts_log=None):
    state_hits = state.get(_game_hits_state_key(game_id))
    if isinstance(state_hits, list) and state_hits:
        return [row for row in state_hits if isinstance(row, dict)]

    rows = alerts_log if isinstance(alerts_log, list) else []
    return [
        row for row in rows
        if str(row.get("game_id") or "") == str(game_id)
        and str(row.get("type") or "") not in ("final", "summary", "predicted_winner")
    ]


def load_log_rotation_state():
    """Load persisted log-rotation state from disk."""
    try:
        with open(LOG_ROTATION_STATE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_log_rotation_state(state):
    """Persist log-rotation state to disk."""
    try:
        with open(LOG_ROTATION_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        _log("[WARN] Log rotation: failed to save state: {}".format(e))


def get_disk_space_status(path=SCRIPT_DIR, warn_free_mb=DISK_WARN_FREE_MB, critical_free_mb=DISK_CRITICAL_FREE_MB):
    """Return disk headroom status for the filesystem containing path."""
    try:
        usage = shutil.disk_usage(path)
    except Exception as e:
        return {
            "ok": False,
            "path": path,
            "level": "unknown",
            "error": str(e),
            "warn_free_mb": int(warn_free_mb),
            "critical_free_mb": int(critical_free_mb),
        }

    total_mb = usage.total / (1024 * 1024)
    free_mb = usage.free / (1024 * 1024)
    used_mb = usage.used / (1024 * 1024)
    free_pct = (usage.free / usage.total * 100.0) if usage.total else 0.0

    level = "ok"
    if free_mb <= critical_free_mb:
        level = "critical"
    elif free_mb <= warn_free_mb:
        level = "warn"

    return {
        "ok": True,
        "path": path,
        "level": level,
        "warn_free_mb": int(warn_free_mb),
        "critical_free_mb": int(critical_free_mb),
        "free_mb": round(free_mb, 1),
        "used_mb": round(used_mb, 1),
        "total_mb": round(total_mb, 1),
        "free_pct": round(free_pct, 2),
    }


def parse_iso_date(value):
    """Parse YYYY-MM-DD strings into date objects."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def latest_rotated_date_for_log(log_path):
    """Return most recent rotated date suffix found for a log file."""
    base_name = os.path.basename(log_path) + "."
    parent = os.path.dirname(log_path) or LOG_DIR
    latest = None

    try:
        for fname in os.listdir(parent):
            if not fname.startswith(base_name):
                continue
            suffix = fname[len(base_name):]
            rotated_date = parse_iso_date(suffix)
            if rotated_date and (latest is None or rotated_date > latest):
                latest = rotated_date
    except OSError:
        return None

    return latest


def should_rotate_for_period(period, period_days, baseline_date, today):
    """Return True when rotation is due for the selected period."""
    if baseline_date is None:
        return False

    if period == "daily":
        return baseline_date < today
    if period == "weekly":
        return (today - baseline_date).days >= 7
    if period == "monthly":
        return (today.year, today.month) != (baseline_date.year, baseline_date.month)
    if period == "days":
        return (today - baseline_date).days >= period_days

    return False


def get_log_rotation_settings(config):
    """Return normalized log rotation settings from config."""
    cfg = config.get("log_rotation", {})
    if not isinstance(cfg, dict):
        cfg = {}

    _default_log_files = [
        "logs/monitor_{}.log".format(league_config.LEAGUE),
        "logs/dashboard_{}.log".format(league_config.LEAGUE),
    ]
    files = cfg.get("files", _default_log_files)
    if not isinstance(files, list):
        files = _default_log_files
    files = [f.strip() for f in files if isinstance(f, str) and f.strip()]
    if not files:
        files = _default_log_files

    try:
        retention_days = int(cfg.get("retention_days", 7))
    except (TypeError, ValueError):
        retention_days = 7
    retention_days = max(1, retention_days)

    raw_period = str(cfg.get("period", "daily")).lower().strip()
    period = raw_period if raw_period in LOG_ROTATION_PERIODS else "daily"

    try:
        period_days = int(cfg.get("period_days", 7))
    except (TypeError, ValueError):
        period_days = 7
    period_days = min(99, max(1, period_days))

    enabled = bool(cfg.get("enabled", False))

    try:
        rotation_hour_utc = int(cfg.get("rotation_hour_utc", 12))
    except (TypeError, ValueError):
        rotation_hour_utc = 12
    rotation_hour_utc = max(0, min(23, rotation_hour_utc))

    return {
        "enabled": enabled,
        "period": period,
        "period_days": period_days,
        "retention_days": retention_days,
        "rotation_hour_utc": rotation_hour_utc,
        "files": files,
    }


def rotate_logs(config):
    """Rotate configured log files and enforce retention."""
    settings = get_log_rotation_settings(config)
    if not settings["enabled"]:
        return

    now_utc = datetime.now(timezone.utc)
    if now_utc.hour != settings["rotation_hour_utc"]:
        return

    today_utc = now_utc.date()
    rotation_state = load_log_rotation_state()
    state_dirty = False

    for rel_path in settings["files"]:
        log_path = rel_path if os.path.isabs(rel_path) else os.path.join(SCRIPT_DIR, rel_path)
        if not os.path.exists(log_path) or not os.path.isfile(log_path):
            continue

        try:
            mtime_date = datetime.fromtimestamp(os.path.getmtime(log_path), timezone.utc).date()
        except OSError as e:
            _log("[WARN] Log rotation: cannot stat {}: {}".format(log_path, e))
            continue

        state_key = os.path.abspath(log_path).replace("\\", "/")
        state_entry = rotation_state.get(state_key, {})
        if not isinstance(state_entry, dict):
            state_entry = {}

        baseline_date = parse_iso_date(state_entry.get("last_rotated"))
        if baseline_date is None:
            baseline_date = latest_rotated_date_for_log(log_path)

        if baseline_date is None:
            if settings["period"] == "daily":
                baseline_date = mtime_date
            else:
                first_seen = parse_iso_date(state_entry.get("first_seen"))
                if first_seen is None:
                    first_seen = today_utc
                    rotation_state[state_key] = {"first_seen": first_seen.isoformat()}
                    state_dirty = True
                baseline_date = first_seen

        if not should_rotate_for_period(settings["period"], settings["period_days"], baseline_date, today_utc):
            continue

        # Rotate using copy-truncate so open file descriptors keep working.
        rotate_suffix_date = mtime_date if settings["period"] == "daily" and mtime_date < today_utc else today_utc
        rotated_path = "{}.{}".format(log_path, rotate_suffix_date.isoformat())
        try:
            mode = "ab" if os.path.exists(rotated_path) else "wb"
            with open(log_path, "rb") as src, open(rotated_path, mode) as dst:
                shutil.copyfileobj(src, dst)
            open(log_path, "w").close()
            _log("[INFO] Rotated log {} -> {}".format(log_path, rotated_path))
            rotation_state[state_key] = {"last_rotated": today_utc.isoformat()}
            state_dirty = True
        except Exception as e:
            _log("[WARN] Log rotation failed for {}: {}".format(log_path, e))

        # Remove rotated files older than retention_days.
        base_name = os.path.basename(log_path) + "."
        parent = os.path.dirname(log_path) or LOG_DIR
        try:
            for fname in os.listdir(parent):
                if not fname.startswith(base_name):
                    continue
                suffix = fname[len(base_name):]
                try:
                    rotated_date = datetime.strptime(suffix, "%Y-%m-%d").date()
                except ValueError:
                    continue
                age_days = (today_utc - rotated_date).days
                if age_days > settings["retention_days"]:
                    old_path = os.path.join(parent, fname)
                    try:
                        os.remove(old_path)
                        _log("[INFO] Removed old rotated log {}".format(old_path))
                    except OSError as e:
                        _log("[WARN] Failed to remove old rotated log {}: {}".format(old_path, e))
        except OSError as e:
            _log("[WARN] Log rotation: cannot list {}: {}".format(parent, e))

    if state_dirty:
        save_log_rotation_state(rotation_state)


# ── Slack delivery ─────────────────────────────────────────────────────────────

def send_alert_result(config, message):
    # Try direct Slack API first (bot token), fallback to OpenClaw gateway
    slack_token = config.get("slack_bot_token", "")
    channel = config.get("slack_channel", "")

    if slack_token and channel:
        url = "https://slack.com/api/chat.postMessage"
        payload = {"channel": channel, "text": message}
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + slack_token,
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.ok:
                body = resp.json()
                if body.get("ok"):
                    return True, None
                err_detail = body.get("error", str(body))
            else:
                err_detail = resp.text[:200] if resp.text else "(no body)"
            _log("[WARN] Alert delivery failed (Slack API): channel={} detail={}".format(channel, err_detail))
            return False, err_detail
        except Exception as e:
            _log("[WARN] Alert delivery error (Slack API): error={}".format(e))
            return False, str(e)

    # Fallback: OpenClaw gateway /tools/invoke
    url = config.get("openclaw_gateway", "").rstrip("/") + "/tools/invoke"
    payload = {
        "tool": "message",
        "args": {
            "action": "send",
            "channel": "slack",
            "target": channel,
            "message": message,
        }
    }
    token = config.get("openclaw_token", "")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if not resp.ok:
            try:
                body = resp.json()
                err_detail = (body.get("error") or {}).get("message") or str(body)
            except Exception:
                err_detail = resp.text[:200] if resp.text else "(no body)"
            _log("[WARN] Alert delivery failed: HTTP {} url={} channel={} detail={}".format(
                resp.status_code, url,
                channel,
                err_detail,
            ))
            return False, "HTTP {} {}".format(resp.status_code, err_detail)
        return True, None
    except Exception as e:
        _log("[WARN] Alert delivery error: url={} error={}".format(url, e))
        return False, str(e)


def send_alert(config, message):
    """Backwards-compatible bool wrapper for alert delivery."""
    ok, _err = send_alert_result(config, message)
    return ok


def _get_alert_mode(cond):
    """Return alert_mode for a condition: 'slack', 'history', or 'none'.

    Supports new alert_mode field and backward-compatible alert boolean.
    """
    mode = cond.get("alert_mode")
    if mode in ("slack", "history", "none"):
        return mode
    # Backward compatibility: map old boolean
    alert = cond.get("alert")
    if alert is False:
        return "none"
    if alert is True:
        return "slack"
    # Default
    return "none"


# ── ESPN helpers ───────────────────────────────────────────────────────────────

def fetch_with_retry(url, timeout=10, retries=2, backoff=2):
    """GET url with simple retry/backoff on transient errors."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
    raise last_exc


def fetch_summary(game_id, cache):
    """Fetch per-game boxscore summary. Cached within a single run."""
    if game_id in cache:
        return cache[game_id]
    try:
        resp = fetch_with_retry(SUMMARY_URL.format(game_id))
        data = resp.json()
        cache[game_id] = data
        return data
    except Exception as e:
        _log("[WARN] Summary fetch failed for", game_id, ":", e)
        cache[game_id] = None
        return None


def _is_summary_cacheable(summary):
    """True if an ESPN summary is complete enough to persist (issue #87 Phase 2).

    ESPN briefly marks games `completed=True` before the summary endpoint has
    fully populated `plays` and `boxscore.teams[*].statistics`. Persisting a
    partial snapshot would regress replay quality vs. the backfill-fetched
    equivalent. We defer the cache write until both shape-checks pass; the
    caller retries on subsequent monitor ticks (bounded to 5 attempts).
    """
    if not isinstance(summary, dict):
        return False
    plays = summary.get("plays")
    if not isinstance(plays, list) or len(plays) == 0:
        return False
    boxscore = summary.get("boxscore") or {}
    teams = boxscore.get("teams") or []
    if not teams:
        return False
    for t in teams:
        if t.get("statistics"):
            return True
    return False


def process_cache_writes_for_completed_games(events, state, summary_cache):
    """Persist completed games' summary + play-by-play to `espn_cache/`.

    Issue #87 Phase 2 — continuous cache update from live games. Runs on every
    monitor tick; no-ops per-game once `state["cache_written_{game_id}"]` is
    set. Completeness-guarded via `_is_summary_cacheable`; uses a bounded
    (max 5) deferred-retry counter at `state["cache_retry_{game_id}"]` to
    cover the brief post-final window where ESPN hasn't yet fully hydrated
    the summary response.

    Best-effort: individual cache failures are logged and do not raise, so
    alert delivery is never blocked. Returns True if `state` was mutated.
    """
    state_dirty = False
    for ev in events or []:
        if ((ev.get("status") or {}).get("type") or {}).get("state") != "post":
            continue
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        cwk = "cache_written_{}".format(gid)
        if state.get(cwk):
            continue
        crk = "cache_retry_{}".format(gid)
        crc = int(state.get(crk, 0) or 0)
        if crc >= 5:
            continue  # abandoned after bounded retries (warning already emitted)
        try:
            summary = fetch_summary(gid, summary_cache)
            if not _is_summary_cacheable(summary):
                state[crk] = crc + 1
                state_dirty = True
                if crc + 1 >= 5:
                    _log("[WARN] [CACHE] defer_abandoned {} after 5 incomplete summaries".format(gid))
                else:
                    _log("[CACHE] defer {} (summary incomplete — retry {}/5)".format(gid, crc + 1))
                continue
            cache_game_summary(gid, summary)
            cache_pbp(gid, summary)
            state[cwk] = True
            state.pop(crk, None)
            state_dirty = True
            _log("[CACHE] write_ok {} (game_summary + play_by_play)".format(gid))
        except Exception as e:
            state[crk] = crc + 1
            state_dirty = True
            _log("[CACHE] write_fail {}: {} (retry {}/5)".format(gid, e, crc + 1))
    return state_dirty


def extract_team_stats(summary, team_id):
    """Return flat {stat_name: float} dict for a team from boxscore summary.
    ESPN returns value=null with data in displayValue, so fall back to displayValue.
    Also parses compound fields (e.g. '25-59') to extract attempted counts."""
    if not summary:
        return None
    for t in (summary.get("boxscore") or {}).get("teams", []):
        if str((t.get("team") or {}).get("id", "")) == str(team_id):
            out = {}
            for s in t.get("statistics", []):
                name = s.get("name")
                val  = s.get("value")
                # Fall back to displayValue if value is None
                if val is None:
                    val = s.get("displayValue")
                if name is None or val is None:
                    continue
                # Parse compound "made-attempted" fields (e.g. "25-59")
                if isinstance(val, str) and "-" in val:
                    parts = val.split("-")
                    if len(parts) == 2:
                        try:
                            made, attempted = float(parts[0]), float(parts[1])
                            if name == "fieldGoalsMade-fieldGoalsAttempted":
                                out["fieldGoalsMade"]     = made
                                out["fieldGoalsAttempted"] = attempted
                            elif name == "freeThrowsMade-freeThrowsAttempted":
                                out["freeThrowsMade"]     = made
                                out["freeThrowsAttempted"] = attempted
                            elif name == "threePointFieldGoalsMade-threePointFieldGoalsAttempted":
                                out["threePointFieldGoalsMade"]     = made
                                out["threePointFieldGoalsAttempted"] = attempted
                        except ValueError:
                            pass
                    continue
                try:
                    out[name] = float(val)
                except (TypeError, ValueError):
                    out[name] = val
            # ESPN uses 'turnoverPoints' — alias to what conditions expect
            if "turnoverPoints" in out and "pointsOffTurnovers" not in out:
                out["pointsOffTurnovers"] = out["turnoverPoints"]
            return out
    return None


def compute_turnover_rate(stats):
    """TO / (FGA + 0.44*FTA + TO) * 100"""
    if not stats:
        return None
    to  = float(stats.get("turnovers") or 0)
    fga = float(stats.get("fieldGoalsAttempted") or 0)
    fta = float(stats.get("freeThrowsAttempted") or 0)
    denom = fga + 0.44 * fta + to
    return round(to / denom * 100, 1) if denom > 0 else None


def to_int_or_none(value):
    """Return integer value when numeric, else None."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def to_float_or_none(value):
    """Return float value when numeric, else None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_default_team_stat_split(raw_stats):
    """Build default split payload when half-by-half data is unavailable."""
    raw = raw_stats or {}

    fg_made = to_int_or_none(raw.get("fieldGoalsMade"))
    fg_attempted = to_int_or_none(raw.get("fieldGoalsAttempted"))
    fg_pct = to_float_or_none(raw.get("fieldGoalPct"))
    if fg_pct is not None:
        fg_pct = round(fg_pct, 1)

    three_pt_made = to_int_or_none(raw.get("threePointFieldGoalsMade"))
    three_pt_attempted = to_int_or_none(raw.get("threePointFieldGoalsAttempted"))
    three_pt_pct = to_float_or_none(raw.get("threePointFieldGoalPct"))
    if three_pt_pct is not None:
        three_pt_pct = round(three_pt_pct, 1)

    ft_made = to_int_or_none(raw.get("freeThrowsMade"))
    ft_attempted = to_int_or_none(raw.get("freeThrowsAttempted"))
    ft_pct = to_float_or_none(raw.get("freeThrowPct"))
    if ft_pct is not None:
        ft_pct = round(ft_pct, 1)

    turnovers = to_int_or_none(raw.get("turnovers"))
    points_off_turnovers = to_int_or_none(raw.get("pointsOffTurnovers"))
    fouls = to_int_or_none(raw.get("fouls"))

    total = {
        "fg_made": fg_made,
        "fg_attempted": fg_attempted,
        "fg_pct": fg_pct,
        "three_pt_made": three_pt_made,
        "three_pt_attempted": three_pt_attempted,
        "three_pt_pct": three_pt_pct,
        "ft_made": ft_made,
        "ft_attempted": ft_attempted,
        "ft_pct": ft_pct,
        "turnovers": turnovers,
        "turnover_rate": compute_turnover_rate(raw),
        "points_off_turnovers": points_off_turnovers,
        "fouls": fouls,
    }

    empty_half = {
        "points": None,
        "fg_made": None,
        "fg_attempted": None,
        "fg_pct": None,
        "three_pt_made": None,
        "three_pt_attempted": None,
        "three_pt_pct": None,
        "ft_made": None,
        "ft_attempted": None,
        "ft_pct": None,
        "turnovers": None,
        "turnover_rate": None,
        "points_off_turnovers": None,
        "fouls": None,
    }

    return {
        "q1": dict(empty_half),
        "q2": dict(empty_half),
        "q3": dict(empty_half),
        "q4": dict(empty_half),
        "h1": dict(empty_half),
        "h2": dict(empty_half),
        "total": total,
    }


def summarize_play_bucket(bucket):
    """Convert play counters into display-ready stats."""
    fg_made = int(bucket.get("fg_made", 0))
    fg_attempted = int(bucket.get("fg_attempted", 0))
    three_pt_made = int(bucket.get("three_pt_made", 0))
    three_pt_attempted = int(bucket.get("three_pt_attempted", 0))
    fta = int(bucket.get("fta", 0))
    ft_made = int(bucket.get("ft_made", 0))
    turnovers = int(bucket.get("turnovers", 0))
    fouls = int(bucket.get("fouls", 0))

    fg_pct = round((float(fg_made) / float(fg_attempted)) * 100, 1) if fg_attempted > 0 else None
    three_pt_pct = round((float(three_pt_made) / float(three_pt_attempted)) * 100, 1) if three_pt_attempted > 0 else None
    ft_pct = round((float(ft_made) / float(fta)) * 100, 1) if fta > 0 else None
    turnover_rate = compute_turnover_rate({
        "turnovers": turnovers,
        "fieldGoalsAttempted": fg_attempted,
        "freeThrowsAttempted": fta,
    })

    return {
        "fg_made": fg_made,
        "fg_attempted": fg_attempted,
        "fg_pct": fg_pct,
        "three_pt_made": three_pt_made,
        "three_pt_attempted": three_pt_attempted,
        "three_pt_pct": three_pt_pct,
        "ft_made": ft_made,
        "ft_attempted": fta,
        "ft_pct": ft_pct,
        "turnovers": turnovers,
        "turnover_rate": turnover_rate,
        "points_off_turnovers": None,
        "fouls": fouls,
    }


def extract_team_stat_splits(summary, team_ids, total_stats_by_team, current_period=None):
    """Return per-team 1H/2H/total stat splits derived from summary plays."""
    split_out = {
        tid: build_default_team_stat_split(total_stats_by_team.get(tid, {}))
        for tid in team_ids
    }

    if not summary:
        return split_out

    plays = summary.get("plays") or []
    if not isinstance(plays, list) or not plays:
        return split_out

    _empty_bucket = lambda: {"fg_made": 0, "fg_attempted": 0, "three_pt_made": 0, "three_pt_attempted": 0, "fta": 0, "ft_made": 0, "turnovers": 0, "fouls": 0}
    buckets = {
        tid: {
            "q1": _empty_bucket(), "q2": _empty_bucket(),
            "q3": _empty_bucket(), "q4": _empty_bucket(),
            "h1": _empty_bucket(), "h2": _empty_bucket(),
        }
        for tid in team_ids
    }

    for play in plays:
        tid = str((play.get("team") or {}).get("id", ""))
        if tid not in buckets:
            continue

        try:
            period_num = int(((play.get("period") or {}).get("number") or 0))
        except (TypeError, ValueError):
            period_num = 0
        if period_num <= 0:
            continue

        half_key = "h1" if period_num <= 2 else "h2"
        quarter_key = "q%d" % min(period_num, 4) if period_num <= 4 else None
        bucket = buckets[tid][half_key]
        q_bucket = buckets[tid][quarter_key] if quarter_key else None

        shooting_play = bool(play.get("shootingPlay"))
        try:
            points_attempted = int(float(play.get("pointsAttempted") or 0))
        except (TypeError, ValueError):
            points_attempted = 0
        score_value = to_float_or_none(play.get("scoreValue")) or 0.0

        targets = [bucket] + ([q_bucket] if q_bucket else [])
        if shooting_play and points_attempted in (2, 3):
            for b in targets:
                b["fg_attempted"] += 1
                if score_value > 0:
                    b["fg_made"] += 1
                if points_attempted == 3:
                    b["three_pt_attempted"] += 1
                    if score_value > 0:
                        b["three_pt_made"] += 1
        elif shooting_play and points_attempted == 1:
            for b in targets:
                b["fta"] += 1
                if score_value > 0:
                    b["ft_made"] += 1

        play_text = "{} {}".format(
            (play.get("type") or {}).get("text", ""),
            play.get("shortDescription") or "",
        ).lower()
        if "foul" in play_text and "flagrant" not in play_text:
            for b in targets:
                b["fouls"] += 1
        if "turnover" in play_text:
            for b in targets:
                b["turnovers"] += 1

    for tid in team_ids:
        h1 = summarize_play_bucket(buckets[tid]["h1"])
        h2 = summarize_play_bucket(buckets[tid]["h2"])
        q_summaries = {qk: summarize_play_bucket(buckets[tid][qk]) for qk in ("q1", "q2", "q3", "q4")}
        total_bucket = {
            "fg_made": h1["fg_made"] + h2["fg_made"],
            "fg_attempted": h1["fg_attempted"] + h2["fg_attempted"],
            "three_pt_made": h1["three_pt_made"] + h2["three_pt_made"],
            "three_pt_attempted": h1["three_pt_attempted"] + h2["three_pt_attempted"],
            "fta": buckets[tid]["h1"]["fta"] + buckets[tid]["h2"]["fta"],
            "ft_made": h1["ft_made"] + h2["ft_made"],
            "turnovers": h1["turnovers"] + h2["turnovers"],
            "fouls": h1["fouls"] + h2["fouls"],
        }
        total = summarize_play_bucket(total_bucket)

        h2_fta = int(buckets[tid]["h2"].get("fta", 0))
        total_fta = int(total_bucket.get("fta", 0))

        # Keep total column aligned with official feed totals when available.
        # Middle column remains in-progress (Q3-only in Q3, Q3+Q4 in Q4) via Total - 1H.
        official_total = split_out[tid].get("total", {})
        official_fgm = to_int_or_none(official_total.get("fg_made"))
        official_fga = to_int_or_none(official_total.get("fg_attempted"))
        official_fg_pct = to_float_or_none(official_total.get("fg_pct"))
        official_3pm = to_int_or_none(official_total.get("three_pt_made"))
        official_3pa = to_int_or_none(official_total.get("three_pt_attempted"))
        official_3p_pct = to_float_or_none(official_total.get("three_pt_pct"))
        official_to = to_int_or_none(official_total.get("turnovers"))
        official_to_rate = to_float_or_none(official_total.get("turnover_rate"))
        official_ftm = to_int_or_none(official_total.get("ft_made"))
        official_fta = to_int_or_none(official_total.get("ft_attempted"))
        official_ft_pct = to_float_or_none(official_total.get("ft_pct"))
        official_fouls = to_int_or_none(official_total.get("fouls"))

        # Reconcile 2H against official totals so split arithmetic stays consistent.
        h1_fgm = to_int_or_none(h1.get("fg_made"))
        h1_fga = to_int_or_none(h1.get("fg_attempted"))
        h1_3pm = to_int_or_none(h1.get("three_pt_made"))
        h1_3pa = to_int_or_none(h1.get("three_pt_attempted"))
        h1_to = to_int_or_none(h1.get("turnovers"))
        h1_ftm = to_int_or_none(h1.get("ft_made"))
        h1_fta_val = to_int_or_none(h1.get("ft_attempted"))
        h1_fouls = to_int_or_none(h1.get("fouls"))

        if official_fgm is not None and h1_fgm is not None and official_fgm >= h1_fgm:
            total["fg_made"] = official_fgm
            h2["fg_made"] = official_fgm - h1_fgm
        if official_fga is not None and h1_fga is not None and official_fga >= h1_fga:
            total["fg_attempted"] = official_fga
            h2["fg_attempted"] = official_fga - h1_fga
        if official_3pm is not None and h1_3pm is not None and official_3pm >= h1_3pm:
            total["three_pt_made"] = official_3pm
            h2["three_pt_made"] = official_3pm - h1_3pm
        if official_3pa is not None and h1_3pa is not None and official_3pa >= h1_3pa:
            total["three_pt_attempted"] = official_3pa
            h2["three_pt_attempted"] = official_3pa - h1_3pa
        if official_to is not None and h1_to is not None and official_to >= h1_to:
            total["turnovers"] = official_to
            h2["turnovers"] = official_to - h1_to
        if official_ftm is not None and h1_ftm is not None and official_ftm >= h1_ftm:
            total["ft_made"] = official_ftm
            h2["ft_made"] = official_ftm - h1_ftm
        if official_fta is not None and h1_fta_val is not None and official_fta >= h1_fta_val:
            total["ft_attempted"] = official_fta
            h2["ft_attempted"] = official_fta - h1_fta_val
        if official_fouls is not None and h1_fouls is not None and official_fouls >= h1_fouls:
            total["fouls"] = official_fouls
            h2["fouls"] = official_fouls - h1_fouls

        h2_fgm = to_int_or_none(h2.get("fg_made"))
        h2_fga = to_int_or_none(h2.get("fg_attempted"))
        h2_3pm = to_int_or_none(h2.get("three_pt_made"))
        h2_3pa = to_int_or_none(h2.get("three_pt_attempted"))
        if h2_fgm is not None and h2_fga is not None and h2_fga > 0:
            h2["fg_pct"] = round((float(h2_fgm) / float(h2_fga)) * 100, 1)
        elif h2_fga == 0:
            h2["fg_pct"] = None
        if h2_3pm is not None and h2_3pa is not None and h2_3pa > 0:
            h2["three_pt_pct"] = round((float(h2_3pm) / float(h2_3pa)) * 100, 1)
        elif h2_3pa == 0:
            h2["three_pt_pct"] = None
        h2_ftm = to_int_or_none(h2.get("ft_made"))
        h2_fta_val = to_int_or_none(h2.get("ft_attempted"))
        if h2_ftm is not None and h2_fta_val is not None and h2_fta_val > 0:
            h2["ft_pct"] = round((float(h2_ftm) / float(h2_fta_val)) * 100, 1)
        elif h2_fta_val == 0:
            h2["ft_pct"] = None

        total_fgm = to_int_or_none(total.get("fg_made"))
        total_fga = to_int_or_none(total.get("fg_attempted"))
        total_3pm = to_int_or_none(total.get("three_pt_made"))
        total_3pa = to_int_or_none(total.get("three_pt_attempted"))
        total_to = to_int_or_none(total.get("turnovers"))

        if (
            official_fg_pct is not None
            and official_fga is not None
            and h1_fga is not None
            and official_fga >= h1_fga
            and total_fga == official_fga
            and official_fga > 0
        ):
            total["fg_pct"] = round(official_fg_pct, 1)
        elif total_fgm is not None and total_fga is not None and total_fga > 0:
            total["fg_pct"] = round((float(total_fgm) / float(total_fga)) * 100, 1)

        if (
            official_3p_pct is not None
            and official_3pa is not None
            and h1_3pa is not None
            and official_3pa >= h1_3pa
            and total_3pa == official_3pa
            and official_3pa > 0
        ):
            total["three_pt_pct"] = round(official_3p_pct, 1)
        elif total_3pm is not None and total_3pa is not None and total_3pa > 0:
            total["three_pt_pct"] = round((float(total_3pm) / float(total_3pa)) * 100, 1)

        total_ftm = to_int_or_none(total.get("ft_made"))
        total_fta_val = to_int_or_none(total.get("ft_attempted"))
        if (
            official_ft_pct is not None
            and official_fta is not None
            and h1_fta_val is not None
            and official_fta >= h1_fta_val
            and total_fta_val == official_fta
            and official_fta > 0
        ):
            total["ft_pct"] = round(official_ft_pct, 1)
        elif total_ftm is not None and total_fta_val is not None and total_fta_val > 0:
            total["ft_pct"] = round((float(total_ftm) / float(total_fta_val)) * 100, 1)

        h2["turnover_rate"] = compute_turnover_rate({
            "turnovers": h2.get("turnovers"),
            "fieldGoalsAttempted": h2.get("fg_attempted"),
            "freeThrowsAttempted": h2_fta,
        })

        if (
            official_to_rate is not None
            and official_to is not None
            and h1_to is not None
            and official_to >= h1_to
        ):
            total["turnover_rate"] = round(official_to_rate, 1)
        else:
            total["turnover_rate"] = compute_turnover_rate({
                "turnovers": total_to,
                "fieldGoalsAttempted": total_fga,
                "freeThrowsAttempted": total_fta,
            })

        pot_total = to_int_or_none(split_out[tid]["total"].get("points_off_turnovers"))
        if pot_total is not None:
            # ESPN summary currently exposes turnover points as a total only.
            # Split proportionally by turnover counts to keep the UI consistent.
            total_turnovers = total.get("turnovers") or 0
            if total_turnovers > 0:
                h1_pot = int(round(float(pot_total) * float(h1.get("turnovers") or 0) / float(total_turnovers)))
            else:
                h1_pot = 0
            h2_pot = int(pot_total) - h1_pot
            h1["points_off_turnovers"] = h1_pot
            h2["points_off_turnovers"] = h2_pot
            total["points_off_turnovers"] = int(pot_total)
            # Distribute POT across quarters proportionally by turnover counts.
            for qk in ("q1", "q2", "q3", "q4"):
                parent_half = h1 if qk in ("q1", "q2") else h2
                parent_pot = parent_half.get("points_off_turnovers") or 0
                parent_to = parent_half.get("turnovers") or 0
                q_to = q_summaries[qk].get("turnovers") or 0
                if parent_to > 0 and parent_pot > 0:
                    q_summaries[qk]["points_off_turnovers"] = int(round(float(parent_pot) * float(q_to) / float(parent_to)))
                else:
                    q_summaries[qk]["points_off_turnovers"] = 0
        else:
            total["points_off_turnovers"] = None

        split_out[tid] = {
            "q1": q_summaries["q1"],
            "q2": q_summaries["q2"],
            "q3": q_summaries["q3"],
            "q4": q_summaries["q4"],
            "h1": h1,
            "h2": h2,
            "total": total,
        }

    return split_out


def extract_scoring_runs(summary, quarter=None):
    """Return chronological unanswered scoring runs from summary plays.

    Output rows contain:
    - team_id: scoring team id
    - points: total unanswered points in run
    - start_play_id / end_play_id
    - end_sequence: sequence number of the latest play in run
    - quarter: period number of the run's latest play
    """
    if not summary:
        return []

    plays = summary.get("plays") or []
    if not isinstance(plays, list) or not plays:
        return []

    req_q = None
    if quarter is not None:
        try:
            req_q = int(quarter)
        except (TypeError, ValueError):
            req_q = None

    runs = []
    current = None

    for idx, play in enumerate(plays, start=1):
        if not play.get("scoringPlay"):
            continue

        team_id = str((play.get("team") or {}).get("id") or "").strip()
        if not team_id:
            continue

        pts = to_int_or_none(play.get("scoreValue"))
        if pts is None:
            pts = to_int_or_none(play.get("pointsAttempted"))
        if pts is None or pts <= 0:
            continue

        try:
            period_num = int(((play.get("period") or {}).get("number") or 0))
        except (TypeError, ValueError):
            period_num = 0
        if period_num <= 0:
            continue
        if req_q is not None and period_num != req_q:
            continue

        play_id = str(play.get("id") or "")
        seq = to_int_or_none(play.get("sequenceNumber"))
        if seq is None:
            seq = idx

        if current and current["team_id"] == team_id:
            current["points"] += int(pts)
            current["end_play_id"] = play_id
            current["end_sequence"] = seq
            current["quarter"] = period_num
        else:
            if current:
                runs.append(current)
            current = {
                "team_id": team_id,
                "points": int(pts),
                "start_play_id": play_id,
                "end_play_id": play_id,
                "end_sequence": seq,
                "quarter": period_num,
            }

    if current:
        runs.append(current)

    return runs


def get_linescore_diff(my_team, opp, quarter_idx):
    """Per-quarter score diff using linescores. Falls back to total score."""
    try:
        my_ls  = my_team.get("linescores", [])
        opp_ls = opp.get("linescores", [])
        if len(my_ls) > quarter_idx and len(opp_ls) > quarter_idx:
            return (float(my_ls[quarter_idx].get("value", 0))
                    - float(opp_ls[quarter_idx].get("value", 0)))
    except Exception:
        pass
    try:
        return int(my_team.get("score", 0)) - int(opp.get("score", 0))
    except Exception:
        return None


def _parse_iso_utc(value):
    """Parse an ISO timestamp string into UTC datetime, or None."""
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


def _game_scoreboard_date(event):
    """Return the ESPN scoreboard date (US local) for a game event.

    ESPN's scoreboard date = US local (ET) date of tip-off.  A late-night
    game with raw UTC datetime on Sat 02:00Z is listed under Friday's
    scoreboard.  Anchor to US local time by subtracting 6h (covers both
    EST/EDT and late-PT tips) before extracting .date().
    """
    raw_date = ((event.get("competitions") or [{}])[0].get("date")
                or event.get("date"))
    dt = _parse_iso_utc(raw_date)
    if dt is None:
        return None
    return (dt - timedelta(hours=6)).date()


def _recover_missed_game(game_id, scoreboard_date, config, cfg_conditions,
                         cfg_compound_conditions, outcome_criteria,
                         config_fingerprint, allowed_types, state):
    """Recover a single missed game inline during a monitor cycle (#227).

    Fetches the ESPN summary, replays conditions, builds pregame snapshot,
    writes game_history record with PW calls, inserts alerts, and sets state
    flags.  Returns True if a record was written.
    """
    import backfill as _bf

    # Fetch summary
    summary_cache = {}
    summary = fetch_summary(game_id, summary_cache)
    if summary is None:
        _log("[WARN] Auto-recovery: no summary for game {}".format(game_id))
        return False

    # Find the event on the scoreboard
    events = fetch_prior_day_events(scoreboard_date)
    event = None
    for ev in events:
        if str(ev.get("id") or "") == game_id:
            event = ev
            break
    if event is None:
        # Try next day's scoreboard (late-night UTC games, same fix as #226)
        next_date = scoreboard_date + timedelta(days=1)
        try:
            next_events = fetch_prior_day_events(next_date)
        except Exception:
            next_events = []
        for ev in next_events:
            if str(ev.get("id") or "") == game_id:
                event = ev
                scoreboard_date = next_date
                break
    if event is None:
        _log("[WARN] Auto-recovery: game {} not found on scoreboard".format(game_id))
        return False
    if not _bf.event_is_complete(event):
        return False

    # Yesterday's events for B2B detection
    try:
        yesterday_events = fetch_prior_day_events(scoreboard_date - timedelta(days=1))
    except Exception:
        yesterday_events = []

    # Evaluate conditions
    replay_state = {}
    conds_fired, _ = _bf.evaluate_conditions_for_game(
        event=event,
        summary=summary,
        cfg_conditions=cfg_conditions,
        cfg_compound_conditions=cfg_compound_conditions,
        allowed_types=allowed_types,
        yesterday_events=yesterday_events,
        replay_state=replay_state,
        b2b_index=None,
        game_date_str=scoreboard_date.isoformat(),
        h1_store=None,
    )

    # ── Use preserved live data when available, fall back to backfill (#230) ──

    # Pregame snapshot: prefer live snapshot captured at tip-off (has live odds)
    _live_snaps = state.get("pregame_snapshots")
    pregame_snapshot = None
    _pregame_source = "backfill"
    if isinstance(_live_snaps, dict) and isinstance(_live_snaps.get(game_id), dict):
        pregame_snapshot = _live_snaps[game_id]
        _pregame_source = "live_preserved"
    if pregame_snapshot is None:
        _h1_store_data = h1_store.load_h1_records_store()
        pregame_snapshot = _bf.build_pregame_snapshot_for_backfill(
            event=event,
            summary=summary,
            game_date_str=scoreboard_date.isoformat(),
            config=config,
            h1_store=_h1_store_data,
        )

    # Condition hits: prefer live hits (have delivery_ok, real-time scores)
    _live_hits_key = _game_hits_state_key(game_id)
    _live_hits = state.get(_live_hits_key)
    _conds_source = "backfill"
    if isinstance(_live_hits, list) and _live_hits:
        # Rebuild conditions_fired from live hits in the same format as
        # the end-of-game block (monitor.py line 3356+)
        _winner_id = record_winner_id = ""
        _comps = (event.get("competitions") or [{}])[0].get("competitors") or []
        _home_c = next((c for c in _comps if c.get("homeAway") == "home"), _comps[0] if _comps else {})
        _away_c = next((c for c in _comps if c.get("homeAway") == "away"), _comps[1] if len(_comps) > 1 else {})
        _hs = int(float(_home_c.get("score") or 0))
        _as = int(float(_away_c.get("score") or 0))
        if _hs > _as:
            record_winner_id = str((_home_c.get("team") or {}).get("id") or "")
        elif _as > _hs:
            record_winner_id = str((_away_c.get("team") or {}).get("id") or "")

        _live_conds_fired = []
        _seen_cf = set()
        for _a in _live_hits:
            if not isinstance(_a, dict):
                continue
            _cf_key = (_a.get("condition", ""), _a.get("team", ""))
            if _cf_key in _seen_cf:
                continue
            _seen_cf.add(_cf_key)
            _a_team_id = ""
            for _c in _comps:
                if (_c.get("team") or {}).get("displayName") == _a.get("team"):
                    _a_team_id = str((_c.get("team") or {}).get("id", ""))
                    break
            _team_won = bool(record_winner_id and _a_team_id and record_winner_id == _a_team_id)
            _smf = _a.get("score_margin_at_fire")
            _md = None
            try:
                if _smf is not None:
                    _gm = float(abs(_hs - _as))
                    _fm = _gm if _team_won else -_gm
                    _md = _fm - float(_smf)
            except (TypeError, ValueError):
                pass
            _live_conds_fired.append({
                "name": _a.get("condition", ""),
                "type": _a.get("type", ""),
                "team": _a.get("team", ""),
                "team_id": _a_team_id,
                "quarter_str": _a.get("quarter_str", ""),
                "direction": _a.get("direction", ""),
                "alerted": bool(_a.get("delivery_ok")),
                "team_won": _team_won,
                "score_margin_at_fire": _smf,
                "margin_diff": _md,
                "quarter_elapsed_pct": _a.get("quarter_elapsed_pct"),
                "game_time_seconds": _a.get("game_time_seconds"),
                "time_anchor": _a.get("time_anchor"),
                "home_away_role": _a.get("home_away_role"),
                "home_score_at_fire": _a.get("home_score_at_fire"),
                "away_score_at_fire": _a.get("away_score_at_fire"),
            })
        if _live_conds_fired:
            conds_fired = _live_conds_fired
            _conds_source = "live_preserved"

    # Build record
    record = _bf.build_record(
        event=event,
        summary=summary,
        conditions_fired=conds_fired,
        config_fingerprint=config_fingerprint,
        outcome_criteria=outcome_criteria,
        pregame_snapshot=pregame_snapshot,
    )
    record["source"] = outcomes.BACKFILL_SOURCE
    record["replay_mode"] = "auto_recovered"

    # ── PW calls: prefer preserved live calls (#230) ─────────────────────────
    _pw_calls_count = 0
    _pw_source = "synthetic"
    _live_pw_key = "pw_calls_{}".format(game_id)
    _live_pw = state.get(_live_pw_key)
    if isinstance(_live_pw, list) and _live_pw:
        # Resolve correct/incorrect using winner_id from the record
        _winner = str(record.get("winner_id") or "")
        for _call in _live_pw:
            if isinstance(_call, dict) and _winner:
                _call["correct"] = bool(_call.get("predicted_team_id") == _winner)
        record["predicted_winner_calls"] = _live_pw
        _pw_calls_count = len(_live_pw)
        _pw_source = "live_preserved"
    else:
        # Fall back to synthetic PW computation in-memory (#227)
        try:
            from backfill_pw_calls import _compute_pw_calls_for_record
            from collections import defaultdict as _dd

            pw_cfg_threshold = float(config.get("predicted_winner_threshold") or 70)
            pw_cfg_edge = config.get("pw_edge_gate")
            pw_edge = float(pw_cfg_edge) if pw_cfg_edge is not None else None

            _prior = outcomes.load_history()
            _ct_idx = _dd(set)
            _c_idx = _dd(set)
            _edge_store = {}
            for _i, _r in enumerate(_prior):
                _rw = str(_r.get("winner_id") or "").strip()
                for _cf in (_r.get("conditions_fired") or []):
                    if not isinstance(_cf, dict):
                        continue
                    _tid = str(_cf.get("team_id") or "").strip()
                    _cn = str(_cf.get("name") or "").strip()
                    if _tid and _cn:
                        _ct_idx[(_cn, _tid)].add(_i)
                        _c_idx[_cn].add(_i)
                        if _rw:
                            if _cn not in _edge_store:
                                _edge_store[_cn] = [0, 0]
                            _edge_store[_cn][0] += 1
                            _tw = _cf.get("team_won")
                            if _tw is None:
                                _tw = (_tid == _rw)
                            if _tw:
                                _edge_store[_cn][1] += 1

            _bf_config = {}
            if config.get("pw_margin_gate") is not None:
                _bf_config["pw_margin_gate"] = config["pw_margin_gate"]
                _bf_config["pw_margin_gate_late"] = config["pw_margin_gate"]
            if config.get("pw_polarity_gate") is not None:
                _bf_config["pw_polarity_gate"] = config["pw_polarity_gate"]

            _prebuilt = (_ct_idx, _c_idx, _prior)
            _pw_calls, _ = _compute_pw_calls_for_record(
                record, _prior,
                threshold=pw_cfg_threshold,
                prebuilt_index=_prebuilt,
                sub_quarter=bool(config.get("pw_sub_quarter")),
                cooldown_minutes=int(config.get("pw_cooldown_minutes") or 5),
                config=_bf_config or None,
                edge_gate=pw_edge,
                cond_edge_store=_edge_store,
            )
            record["predicted_winner_calls"] = _pw_calls or []
            _pw_calls_count = len(_pw_calls) if _pw_calls else 0
        except Exception as _pw_exc:
            _log("[WARN] Auto-recovery PW computation failed for {}: {}".format(game_id, _pw_exc))
            record["predicted_winner_calls"] = []

    # Extract score/team info for logging and alerts
    comps = (event.get("competitions") or [{}])[0]
    competitors = comps.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
    home_abbr = (home.get("team") or {}).get("abbreviation", "")
    away_abbr = (away.get("team") or {}).get("abbreviation", "")
    home_score = int(float(home.get("score") or 0))
    away_score = int(float(away.get("score") or 0))
    winner_abbr = home_abbr if home_score > away_score else (away_abbr if away_score > home_score else "TIE")

    # Log recovery details
    _margin = abs(home_score - away_score)
    _tags_str = ", ".join(record.get("postgame_tags_matched") or []) or "none"
    _spread = (pregame_snapshot or {}).get("spread_detail", "n/a")
    _log("[AUTO-RECOVERY] Game {} {} {} vs {} {} | margin={} | spread={} | tags=[{}]".format(
        game_id, away_abbr, away_score, home_abbr, home_score, _margin, _spread, _tags_str))
    _log("[AUTO-RECOVERY] Sources: conditions={} ({}) | PW calls={} ({}) | pregame={}".format(
        len(conds_fired), _conds_source, _pw_calls_count, _pw_source, _pregame_source))
    if _pw_calls_count and isinstance(record.get("predicted_winner_calls"), list):
        for _pc in record["predicted_winner_calls"]:
            if isinstance(_pc, dict):
                _log("[AUTO-RECOVERY]   PW: {} {} {:.1f}% correct={}".format(
                    _pc.get("quarter", "?"), _pc.get("predicted_team", "?"),
                    float(_pc.get("pct") or 0), _pc.get("correct")))

    # Write to game_history — single atomic write with PW calls already attached
    written, _ = _bf.write_records_canonical([record])
    if not written:
        return False

    game_condition_hits = []
    summary_msg, tags, full_meta, summary_tags = build_game_summary(
        event, summary_cache, game_condition_hits, cfg_conditions, outcome_criteria
    )
    primary_tag_color = ""
    if summary_tags:
        primary_tag_color = str(summary_tags[0].get("color") or "yellow").strip().lower() or "yellow"

    event_dt = _parse_iso_utc(event.get("date") or ((event.get("competitions") or [{}])[0].get("date") or ""))
    game_end_ts = (event_dt + timedelta(hours=3)).isoformat() if event_dt else datetime.now(timezone.utc).isoformat()

    alerts_path = league_config.state_path("alerts.json")
    try:
        with open(alerts_path) as f:
            alerts_log = json.load(f)
    except Exception:
        alerts_log = []

    # Remove any existing summary/final for this game
    alerts_log = [
        a for a in alerts_log
        if not (str(a.get("game_id") or "") == game_id and str(a.get("type") or "") in ("summary", "final"))
    ]

    final_entry = {
        "ts": game_end_ts, "type": "final",
        "team": "{} @ {}".format(away_abbr, home_abbr),
        "condition": "Final", "color": "",
        "score_str": "{} {} - {} {}".format(away_abbr, away_score, home_abbr, home_score),
        "quarter_str": "Final",
        "direction": "\U0001f3c6 {} win".format(winner_abbr),
        "game_id": game_id, "delivery_ok": True, "recovered": True,
    }
    summary_entry = {
        "ts": game_end_ts, "type": "summary",
        "team": "{} @ {}".format(away_abbr, home_abbr),
        "condition": "Game Summary", "color": primary_tag_color,
        "score_str": "{} {} - {} {}".format(away_abbr, away_score, home_abbr, home_score),
        "quarter_str": "Final",
        "direction": " \u00b7 ".join(tags) if tags else "\U0001f3c6 {} win".format(winner_abbr),
        "game_id": game_id, "delivery_ok": True,
        "matchup_meta": full_meta, "summary_tags": summary_tags, "recovered": True,
    }
    alerts_log.insert(0, final_entry)
    alerts_log.insert(0, summary_entry)

    tmp_alerts = alerts_path + ".tmp"
    with open(tmp_alerts, "w") as f:
        json.dump(alerts_log, f, indent=2)
    os.replace(tmp_alerts, alerts_path)

    # Set state flags
    state["history_recorded_{}".format(game_id)] = True
    state["summary_alerted_{}".format(game_id)] = True
    state["final_alerted_{}".format(game_id)] = True

    # Clean up preserved state now that it's written to history
    state.pop(_live_pw_key, None)
    state.pop(_live_hits_key, None)
    if isinstance(_live_snaps, dict):
        _live_snaps.pop(game_id, None)
        state["pregame_snapshots"] = _live_snaps

    _log("[AUTO-RECOVERY] {} conditions ({}), {} PW calls ({}), pregame ({}), alerts — all written".format(
        len(conds_fired), _conds_source, _pw_calls_count, _pw_source, _pregame_source))
    return True


def fetch_prior_day_events(prior_date):
    """Fetch ESPN scoreboard events for a specific date.  Returns [] on failure."""
    try:
        url = YESTERDAY_URL.format(prior_date.strftime("%Y%m%d"))
        resp = fetch_with_retry(url)
        return resp.json().get("events", [])
    except Exception as e:
        _log("[WARN] Prior-day scoreboard fetch failed for %s: %s", prior_date, e)
        return []


def check_b2b(team_id, state, game_id, yesterday_events):
    """Return True if team played a *different* game yesterday. Result cached in state."""
    cache_key = "b2b_{}_{}".format(game_id, team_id)
    if cache_key in state:
        return bool(state[cache_key])
    try:
        for ev in yesterday_events:
            # Skip the current game — it may span midnight and appear on both days
            if str(ev.get("id", "")) == str(game_id):
                continue
            for comp in ev.get("competitions", []):
                for c in comp.get("competitors", []):
                    if str((c.get("team") or {}).get("id", "")) == str(team_id):
                        state[cache_key] = True
                        return True
        state[cache_key] = False
        return False
    except Exception as e:
        _log("[WARN] B2B check failed:", e)
        state[cache_key] = False
        return False


def q4_tie_after_deficit(competitors, summary=None, deficit=6, allow_fallback=True):
    """
    Return True if the team trailing entering Q4 by deficit+ points tied or took
    the lead during Q4.

    Detection order:
    1) Use summary play-by-play scores in Q4 when available (strict, preferred).
    2) Optional fallback to end-of-Q4 score state (supports OT games without full plays).
    """
    if len(competitors) < 2:
        return False
    c0, c1 = competitors[0], competitors[1]
    ls0 = c0.get("linescores", [])
    ls1 = c1.get("linescores", [])

    def qval(ls, q):
        try:
            return float(ls[q].get("value", 0))
        except (IndexError, TypeError):
            return 0.0

    # Score entering Q4 (sum of Q1-Q3)
    c0_pre_q4 = sum(qval(ls0, q) for q in range(3))
    c1_pre_q4 = sum(qval(ls1, q) for q in range(3))

    pre_margin = abs(c0_pre_q4 - c1_pre_q4)
    if pre_margin < deficit:
        return False

    # Identify which side was trailing entering Q4.
    if c0_pre_q4 < c1_pre_q4:
        trailing_idx = 0
    elif c1_pre_q4 < c0_pre_q4:
        trailing_idx = 1
    else:
        return False

    trailing_id = str((competitors[trailing_idx].get("team") or {}).get("id") or "")
    opp_idx = 1 - trailing_idx
    opp_id = str((competitors[opp_idx].get("team") or {}).get("id") or "")

    # Try strict play-by-play detection first.
    plays = (summary or {}).get("plays") if isinstance(summary, dict) else None
    if isinstance(plays, list) and plays:
        for play in plays:
            try:
                period_num = int(((play.get("period") or {}).get("number") or 0))
            except (TypeError, ValueError):
                continue
            if period_num != 4:
                continue

            away_score = to_int_or_none(play.get("awayScore"))
            home_score = to_int_or_none(play.get("homeScore"))
            if away_score is None or home_score is None:
                continue

            away_team = next((c for c in competitors if c.get("homeAway") == "away"), None)
            home_team = next((c for c in competitors if c.get("homeAway") == "home"), None)
            if not away_team or not home_team:
                continue

            away_id = str((away_team.get("team") or {}).get("id") or "")
            home_id = str((home_team.get("team") or {}).get("id") or "")

            score_by_id = {
                away_id: away_score,
                home_id: home_score,
            }
            trailing_score = score_by_id.get(trailing_id)
            opp_score = score_by_id.get(opp_id)
            if trailing_score is None or opp_score is None:
                continue

            if trailing_score >= opp_score:
                return True

    if not allow_fallback:
        return False

    # Fallback: use end-of-Q4 state from linescores (works for OT games too).
    c0_end_q4 = c0_pre_q4 + qval(ls0, 3)
    c1_end_q4 = c1_pre_q4 + qval(ls1, 3)
    trailing_end_q4 = c0_end_q4 if trailing_idx == 0 else c1_end_q4
    opp_end_q4 = c1_end_q4 if trailing_idx == 0 else c0_end_q4
    if trailing_end_q4 >= opp_end_q4:
        return True

    return False


def _default_postgame_tag_defs(close_margin, comeback_deficit, close_when_upset, outcome_order):
    base = {
        "UPSET": {
            "key": "UPSET",
            "label": "UPSET",
            "emoji": "🚨",
            "kind": "upset",
            "color": "red",
            "enabled": True,
        },
        "CLOSE": {
            "key": "CLOSE",
            "label": "CLOSE GAME",
            "emoji": "⚡",
            "kind": "close_margin_lte",
            "threshold": int(close_margin),
            "color": "yellow",
            "enabled": True,
            "suppress_if_tags": [] if close_when_upset else ["UPSET"],
        },
        "COMEBACK": {
            "key": "COMEBACK",
            "label": "Q4 COMEBACK",
            "emoji": "🔄",
            "kind": "comeback_q4",
            "threshold": int(comeback_deficit),
            "color": "blue",
            "enabled": True,
        },
    }
    tags = []
    for key in outcome_order:
        if key in base:
            tags.append(dict(base[key]))
    for key in ("UPSET", "CLOSE", "COMEBACK"):
        if not any(t.get("key") == key for t in tags):
            tags.append(dict(base[key]))
    return tags


def normalize_postgame_outcome_criteria(config):
    """Return normalized post-game outcome criteria config with safe defaults."""
    defaults = {
        "close_margin": 5,
        "comeback_deficit": 6,
        "close_when_upset": False,
        "outcome_order": ["UPSET", "CLOSE", "COMEBACK"],
        "require_q4_pbp_for_comeback": False,
    }

    raw = config.get("postgame_outcome_criteria", {})
    if not isinstance(raw, dict):
        return defaults

    out = dict(defaults)

    try:
        out["close_margin"] = min(30, max(1, int(raw.get("close_margin", defaults["close_margin"]))))
    except (TypeError, ValueError):
        pass

    try:
        out["comeback_deficit"] = min(40, max(1, int(raw.get("comeback_deficit", defaults["comeback_deficit"]))))
    except (TypeError, ValueError):
        pass

    cwu = raw.get("close_when_upset", defaults["close_when_upset"])
    out["close_when_upset"] = bool(cwu) if isinstance(cwu, bool) else defaults["close_when_upset"]

    require_pbp = raw.get("require_q4_pbp_for_comeback", defaults["require_q4_pbp_for_comeback"])
    out["require_q4_pbp_for_comeback"] = bool(require_pbp) if isinstance(require_pbp, bool) else defaults["require_q4_pbp_for_comeback"]

    order = raw.get("outcome_order")
    allowed = {"UPSET", "CLOSE", "COMEBACK"}
    if isinstance(order, list):
        cleaned = []
        for item in order:
            key = str(item).strip().upper()
            if key in allowed and key not in cleaned:
                cleaned.append(key)
        for key in defaults["outcome_order"]:
            if key not in cleaned:
                cleaned.append(key)
        out["outcome_order"] = cleaned[:3]

    # New configurable tag system. Backward-compatible defaults are generated
    # from legacy fields so existing configs keep current behavior.
    default_tags = _default_postgame_tag_defs(
        out["close_margin"],
        out["comeback_deficit"],
        out["close_when_upset"],
        out["outcome_order"],
    )

    tag_kinds = {"upset", "close_margin_lte", "comeback_q4", "margin_gte"}
    color_allowed = {"red", "orange", "yellow", "green", "blue", "muted"}
    raw_tags = raw.get("tags")
    if isinstance(raw_tags, list):
        normalized_tags = []
        seen_keys = set()
        for idx, item in enumerate(raw_tags):
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip().upper()
            if not key:
                key = "TAG_{}".format(idx + 1)
            if key in seen_keys:
                continue

            kind = str(item.get("kind") or "").strip().lower()
            if kind not in tag_kinds:
                continue

            label = str(item.get("label") or key).strip() or key
            emoji = str(item.get("emoji") or "").strip()
            color = str(item.get("color") or "yellow").strip().lower()
            if color not in color_allowed:
                color = "yellow"
            enabled = item.get("enabled", True)
            enabled = bool(enabled) if isinstance(enabled, bool) else True

            tag = {
                "key": key,
                "label": label,
                "emoji": emoji,
                "kind": kind,
                "color": color,
                "enabled": enabled,
            }

            if kind in ("close_margin_lte", "comeback_q4", "margin_gte"):
                try:
                    thr = int(item.get("threshold", out["close_margin"]))
                except (TypeError, ValueError):
                    thr = out["close_margin"]
                if kind == "close_margin_lte":
                    thr = min(30, max(1, thr))
                elif kind == "comeback_q4":
                    thr = min(40, max(1, thr))
                else:
                    thr = min(80, max(1, thr))
                tag["threshold"] = thr

            suppress = item.get("suppress_if_tags")
            if isinstance(suppress, list):
                cleaned_suppress = []
                for raw_sup in suppress:
                    skey = str(raw_sup or "").strip().upper()
                    if skey and skey not in cleaned_suppress:
                        cleaned_suppress.append(skey)
                if cleaned_suppress:
                    tag["suppress_if_tags"] = cleaned_suppress

            normalized_tags.append(tag)
            seen_keys.add(key)

        if normalized_tags:
            out["tags"] = normalized_tags
        else:
            out["tags"] = default_tags
    else:
        out["tags"] = default_tags

    return out


def _format_signed_ml(val):
    """Format moneyline integer as +150 / -180 / --."""
    try:
        v = int(val)
        return "+{}".format(v) if v > 0 else str(v)
    except (TypeError, ValueError):
        return "--"

def _format_signed_spread(val):
    """Format spread as -3.5 / +3.5 / --."""
    try:
        v = float(val)
        return "+{}".format(v) if v > 0 else str(v)
    except (TypeError, ValueError):
        return "--"

def _team_record_str(competitor):
    """Return 'W-L' from competitor records array, or '--'."""
    for rec in (competitor.get("records") or []):
        if str(rec.get("type","")).lower() in ("total","overall"):
            return rec.get("summary","--")
    return "--"

def _team_streak_from_summary(summary, team_id):
    """Derive W/L streak from lastFiveGames in summary, if available."""
    try:
        games = summary.get("lastFiveGames") or []
        results = []
        for g in games:
            for c in (g.get("competitors") or []):
                if str((c.get("team") or {}).get("id","")) == str(team_id):
                    results.append(bool(c.get("winner")))
        if not results:
            return "--"
        first = results[-1]  # most recent
        count = 0
        for r in reversed(results):
            if r != first:
                break
            count += 1
        return "{}{}".format("W" if first else "L", count)
    except Exception:
        return "--"


def compute_streak_from_history(team_id, before_date, records=None):
    """Compute W/L streak for a team from game history records.

    Fallback for when ESPN's lastFiveGames is unavailable (WNBA, cached summaries).
    ``records`` is a list of game_history dicts; if None, loads from disk.
    ``before_date`` is an ISO date string (YYYY-MM-DD) — only games before this date count.
    Returns e.g. "W3", "L1", or "--" if no data.
    """
    if records is None:
        try:
            records = outcomes.load_history()
        except Exception:
            return "--"
    tid = str(team_id)
    # Collect (date, won) for this team, sorted by date desc
    team_games = []
    for r in records:
        if not isinstance(r, dict):
            continue
        home_id = str(r.get("home_id") or "")
        away_id = str(r.get("away_id") or "")
        if tid not in (home_id, away_id):
            continue
        d = str(r.get("date") or "")[:10]
        if not d or d >= before_date:
            continue
        winner_id = str(r.get("winner_id") or "")
        if not winner_id:
            continue
        team_games.append((d, winner_id == tid))
    if not team_games:
        return "--"
    team_games.sort(key=lambda x: x[0], reverse=True)
    last_won = team_games[0][1]
    count = 0
    for _, won in team_games:
        if won == last_won:
            count += 1
        else:
            break
    return "{}{}".format("W" if last_won else "L", count)


def evaluate_postgame_tag_keys(competitors, summary, outcome_criteria, margin, fav_id, winner_id):
    """
    Evaluate which post-game tags fire for a completed game.

    Returns a list of matched tag key strings (e.g. ["UPSET", "BLOWOUT"]).
    Compatible with the tag definitions from normalize_postgame_outcome_criteria().
    """
    criteria = outcome_criteria or {
        "close_margin": 5,
        "comeback_deficit": 6,
        "close_when_upset": False,
        "outcome_order": ["UPSET", "CLOSE", "COMEBACK"],
        "require_q4_pbp_for_comeback": False,
        "tags": _default_postgame_tag_defs(5, 6, False, ["UPSET", "CLOSE", "COMEBACK"]),
    }
    require_pbp = bool(criteria.get("require_q4_pbp_for_comeback", False))

    underdog_won = False
    if fav_id and winner_id:
        underdog_won = (winner_id != fav_id)

    matched_keys = []
    matched_key_set = set()

    for tag_def in (criteria.get("tags") or []):
        if not isinstance(tag_def, dict):
            continue
        if tag_def.get("enabled", True) is False:
            continue

        tag_key = str(tag_def.get("key") or "").strip().upper()
        if not tag_key:
            continue

        suppress_if = [
            str(k or "").strip().upper()
            for k in (tag_def.get("suppress_if_tags") or [])
            if str(k or "").strip()
        ]
        if any(k in matched_key_set for k in suppress_if):
            continue

        kind = str(tag_def.get("kind") or "").strip().lower()
        matched = False

        if kind == "upset":
            matched = underdog_won
        elif kind == "close_margin_lte":
            try:
                threshold = int(tag_def.get("threshold", criteria.get("close_margin", 5)))
            except (TypeError, ValueError):
                threshold = int(criteria.get("close_margin", 5))
            matched = margin <= min(30, max(1, threshold))
        elif kind == "margin_gte":
            try:
                threshold = int(tag_def.get("threshold", 15))
            except (TypeError, ValueError):
                threshold = 15
            matched = margin >= min(80, max(1, threshold))
        elif kind == "comeback_q4":
            try:
                threshold = int(tag_def.get("threshold", criteria.get("comeback_deficit", 6)))
            except (TypeError, ValueError):
                threshold = int(criteria.get("comeback_deficit", 6))
            matched = q4_tie_after_deficit(
                competitors,
                summary=summary,
                deficit=min(40, max(1, threshold)),
                allow_fallback=not require_pbp,
            )

        if matched:
            matched_keys.append(tag_key)
            matched_key_set.add(tag_key)

    return matched_keys


def _build_retroactive_matchup_meta(event, summary, home, away):
    """Build matchup_meta dict for a retroactive game (no tags triggered)."""
    home_abbr = home["team"].get("abbreviation", home["team"]["displayName"])
    away_abbr = away["team"].get("abbreviation", away["team"]["displayName"])
    home_name = home["team"].get("displayName", home_abbr)
    away_name = away["team"].get("displayName", away_abbr)
    home_id   = str(home["team"].get("id", ""))
    away_id   = str(away["team"].get("id", ""))

    pc = ((summary.get("pickcenter") or [{}])[0]) if summary else {}
    away_ml = str(pc.get("awayTeamOdds", {}).get("moneyLine", "--"))
    home_ml = str(pc.get("homeTeamOdds", {}).get("moneyLine", "--"))
    away_spread = str(pc.get("awayTeamOdds", {}).get("spreadOdds", "--"))
    home_spread = str(pc.get("homeTeamOdds", {}).get("spreadOdds", "--"))
    if away_spread == "--" and pc.get("spread"):
        try:
            sp = float(pc["spread"])
            away_spread = "{:+.1f}".format(sp)
            home_spread = "{:+.1f}".format(-sp)
        except (TypeError, ValueError):
            pass

    away_record = _team_record_str(away)
    home_record = _team_record_str(home)
    away_streak = _team_streak_from_summary(summary, away_id) if summary else "--"
    home_streak = _team_streak_from_summary(summary, home_id) if summary else "--"

    return {
        "away_name": away_name, "home_name": home_name,
        "away_abbr": away_abbr, "home_abbr": home_abbr,
        "away_ml": away_ml, "home_ml": home_ml,
        "away_spread": away_spread, "home_spread": home_spread,
        "away_record": away_record, "home_record": home_record,
        "away_streak": away_streak, "home_streak": home_streak,
    }


def build_game_summary(event, summary_cache, alerts_log, conditions, outcome_criteria=None):
    """
    Build an end-of-game summary message if the game qualifies:
    - Upset: underdog (by spread) won
    - Close game: final margin ≤5 pts
    - Q4 comeback: team tied/led after trailing 6+ in Q4

    Includes full matchup line (ML, spread, record, streak) same as live games.
    Returns (message_str, tags_list, meta_dict, summary_tags_list)
    or (None, [], {}, []) if not interesting.
    meta_dict is passed through to the alert log for dashboard enrichment.
    """
    game_id = str(event["id"])
    comp_list = event.get("competitions", [])
    if not comp_list:
        return None, [], {}, []
    main_comp   = comp_list[0]
    competitors = main_comp.get("competitors", [])
    if len(competitors) < 2:
        return None, [], {}, []

    home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
    away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
    home_score = int(home.get("score") or 0)
    away_score = int(away.get("score") or 0)
    home_abbr  = home["team"].get("abbreviation", home["team"]["displayName"])
    away_abbr  = away["team"].get("abbreviation", away["team"]["displayName"])
    home_name  = home["team"].get("displayName", home_abbr)
    away_name  = away["team"].get("displayName", away_abbr)
    home_id    = str(home["team"].get("id", ""))
    away_id    = str(away["team"].get("id", ""))
    margin     = abs(home_score - away_score)

    winner_abbr = home_abbr if home_score > away_score else (away_abbr if away_score > home_score else None)

    tags = []
    summary_tags = []

    # ── Fetch summary (pickcenter + lastFiveGames) ─────────────────────────────
    summary     = fetch_summary(game_id, summary_cache)
    pc          = ((summary.get("pickcenter") or [{}])[0]) if summary else {}

    # ── Records ───────────────────────────────────────────────────────────────
    away_record = _team_record_str(away)
    home_record = _team_record_str(home)

    # ── Streaks ───────────────────────────────────────────────────────────────
    away_streak = _team_streak_from_summary(summary, away_id) if summary else "--"
    home_streak = _team_streak_from_summary(summary, home_id) if summary else "--"

    # ── Odds (moneyline + spread) ─────────────────────────────────────────────
    away_ml = "--"; home_ml = "--"
    away_spread = "--"; home_spread = "--"

    if pc:
        raw_spread = pc.get("spread")
        ato = pc.get("awayTeamOdds") or {}
        hto = pc.get("homeTeamOdds") or {}

        # Map ML by team id if available
        ato_id = str(ato.get("teamId",""))
        hto_id = str(hto.get("teamId",""))
        if ato_id == away_id or (not ato_id and not hto_id):
            away_ml = _format_signed_ml(ato.get("moneyLine"))
            home_ml = _format_signed_ml(hto.get("moneyLine"))
        elif ato_id == home_id:
            # ESPN sometimes swaps them
            away_ml = _format_signed_ml(hto.get("moneyLine"))
            home_ml = _format_signed_ml(ato.get("moneyLine"))
        else:
            away_ml = _format_signed_ml(ato.get("moneyLine"))
            home_ml = _format_signed_ml(hto.get("moneyLine"))

        if raw_spread is not None:
            try:
                sv = float(raw_spread)   # ESPN spread = home perspective (negative = home favoured)
                home_spread = _format_signed_spread(sv)
                away_spread = _format_signed_spread(-sv)
            except (TypeError, ValueError):
                pass

    # ── Spread / upset detection ───────────────────────────────────────────────
    spread_str   = None
    underdog_won = False
    fav_abbr = dog_abbr = None

    odds_list   = main_comp.get("odds") or []
    odds_detail = (odds_list[0].get("details") or "") if odds_list else ""
    if not odds_detail and pc:
        odds_detail = pc.get("details") or ""

    fav_id = parse_odds_favorite(odds_detail, competitors)
    if fav_id:
        fav = next((c for c in competitors if str(c["team"]["id"]) == fav_id), None)
        dog = next((c for c in competitors if str(c["team"]["id"]) != fav_id), None)
        if fav and dog:
            fav_abbr  = fav["team"].get("abbreviation","")
            dog_abbr  = dog["team"].get("abbreviation","")
            fav_score = int(fav.get("score") or 0)
            dog_score = int(dog.get("score") or 0)
            if dog_score > fav_score:
                underdog_won = True
            parts = odds_detail.strip().split()
            try:
                sv = abs(float(parts[1])) if len(parts) >= 2 else None
                spread_str = "{} -{} vs {} +{}".format(fav_abbr, sv, dog_abbr, sv) if sv else odds_detail.strip()
            except (ValueError, IndexError):
                spread_str = odds_detail.strip()

    criteria = outcome_criteria or {
        "close_margin": 5,
        "comeback_deficit": 6,
        "close_when_upset": False,
        "outcome_order": ["UPSET", "CLOSE", "COMEBACK"],
        "require_q4_pbp_for_comeback": False,
        "tags": _default_postgame_tag_defs(5, 6, False, ["UPSET", "CLOSE", "COMEBACK"]),
    }

    # ── Dynamic post-game tag detection ───────────────────────────────────────
    require_pbp = bool(criteria.get("require_q4_pbp_for_comeback", False))
    matched_tag_keys = set()
    for tag_def in (criteria.get("tags") or []):
        if not isinstance(tag_def, dict):
            continue
        if tag_def.get("enabled", True) is False:
            continue

        tag_key = str(tag_def.get("key") or "").strip().upper()
        if not tag_key:
            continue

        suppress_if = [
            str(k or "").strip().upper()
            for k in (tag_def.get("suppress_if_tags") or [])
            if str(k or "").strip()
        ]
        if any(k in matched_tag_keys for k in suppress_if):
            continue

        kind = str(tag_def.get("kind") or "").strip().lower()
        matched = False
        detail = ""

        if kind == "upset":
            matched = underdog_won
        elif kind == "close_margin_lte":
            try:
                threshold = int(tag_def.get("threshold", criteria.get("close_margin", 5)))
            except (TypeError, ValueError):
                threshold = int(criteria.get("close_margin", 5))
            threshold = min(30, max(1, threshold))
            matched = margin <= threshold
        elif kind == "margin_gte":
            try:
                threshold = int(tag_def.get("threshold", 15))
            except (TypeError, ValueError):
                threshold = 15
            threshold = min(80, max(1, threshold))
            matched = margin >= threshold
        elif kind == "comeback_q4":
            try:
                threshold = int(tag_def.get("threshold", criteria.get("comeback_deficit", 6)))
            except (TypeError, ValueError):
                threshold = int(criteria.get("comeback_deficit", 6))
            threshold = min(40, max(1, threshold))
            matched = q4_tie_after_deficit(
                competitors,
                summary=summary,
                deficit=threshold,
                allow_fallback=not require_pbp,
            )
            if matched:
                detail = "tied/took lead after trailing {}+ in Q4".format(threshold)
        else:
            continue

        if not matched:
            continue

        label = str(tag_def.get("label") or tag_key).strip() or tag_key
        emoji = str(tag_def.get("emoji") or "").strip()
        color = str(tag_def.get("color") or "yellow").strip().lower() or "yellow"

        tag_text = "*{}*".format(label)
        if emoji:
            tag_text = "{} {}".format(emoji, tag_text)
        if detail:
            tag_text = "{} — {}".format(tag_text, detail)

        tags.append(tag_text)
        summary_tags.append({
            "key": tag_key,
            "label": label,
            "emoji": emoji,
            "color": color,
        })
        matched_tag_keys.add(tag_key)

    if not tags:
        return None, [], {}, []

    # ── Conditions that fired ────────────────────────────────────────────────
    game_alerts = [
        a for a in alerts_log
        if str(a.get("game_id","")) == game_id
        and a.get("type") not in ("final", "summary", "predicted_winner")
    ]
    fired_conditions = {}
    for a in game_alerts:
        cname = a.get("condition","")
        team  = a.get("team","")
        key   = "{} ({})".format(cname, team) if team else cname
        fired_conditions[key] = fired_conditions.get(key, 0) + 1

    # ── Build Slack message ────────────────────────────────────────────────────
    ts       = _format_alert_time(datetime.now(timezone.utc), home_abbr)
    tag_line = " · ".join(tags)

    # Matchup title line — same format as live game: AWAY ML (spread) (record/streak) @ HOME ...
    matchup_line = "{} {} ({}) ({}/{}) @ {} {} ({}) ({}/{})".format(
        away_name, away_ml, away_spread, away_record, away_streak,
        home_name, home_ml, home_spread, home_record, home_streak,
    )

    lines = [
        "_{}_".format(ts),
        ":basketball: 📋 *GAME SUMMARY* — {} {} · {} {}".format(
            away_abbr, away_score, home_abbr, home_score),
        tag_line,
        "_{}_ ".format(matchup_line),
    ]

    if spread_str:
        lines.append("_Spread: {}_".format(spread_str))
    if underdog_won and dog_abbr and fav_abbr:
        lines.append("*{}* upset *{}* — final margin: {} pts".format(dog_abbr, fav_abbr, margin))
    elif margin <= int(criteria.get("close_margin", 5)):
        lines.append("Final margin: {} pts".format(margin))

    if fired_conditions:
        lines.append("")
        lines.append("*Conditions that fired:*")
        for key, count in fired_conditions.items():
            lines.append("  {} {}".format("{}x".format(count) if count > 1 else "✓", key))
    else:
        lines.append("_No conditions fired during this game._")

    # Odds API per-game stats (#243)
    _oa_game = odds_api.format_game_stats(game_id)
    if _oa_game:
        lines.append("")
        lines.append("_📊 {}_".format(_oa_game))


    # ── Meta dict for dashboard enrichment ────────────────────────────────────
    meta = {
        "away_name":   away_name,   "home_name":   home_name,
        "away_abbr":   away_abbr,   "home_abbr":   home_abbr,
        "away_ml":     away_ml,     "home_ml":     home_ml,
        "away_spread": away_spread, "home_spread": home_spread,
        "away_record": away_record, "home_record": home_record,
        "away_streak": away_streak, "home_streak": home_streak,
        "spread_line": spread_str or "",
        "margin":      margin,
        "underdog_won": underdog_won,
        "fav_abbr":    fav_abbr or "",
        "dog_abbr":    dog_abbr or "",
        "odds_api_stats": odds_api.get_game_stats(game_id),
    }

    return "\n".join(lines), tags, meta, summary_tags


def parse_odds_favorite(odds_detail, competitors):
    """
    Parse odds string like 'DEN -3' -> team_id of the FAVORITE (negative spread).
    Returns None if unparseable or pick-em.
    """
    if not odds_detail:
        return None
    parts = odds_detail.strip().split()
    if len(parts) < 2:
        return None
    abbr = parts[0].upper()
    try:
        spread = float(parts[1])
    except ValueError:
        return None
    if spread >= 0:
        return None
    for c in competitors:
        if (c.get("team") or {}).get("abbreviation", "").upper() == abbr:
            return str(c["team"]["id"])
    return None


def format_live_handicap_line(odds_detail, team_abbr, diff_total):
    """
    Format a live handicap line showing how a team tracks against the pre-game spread.

    Args:
        odds_detail: ESPN odds string like 'JAZZ -7.5'
        team_abbr:   Abbreviation of the team that triggered the alert (e.g. 'UTA')
        diff_total:  Current score margin: team_score - opponent_score

    Returns:
        String like 'Line: UTA beating by 2' / 'Line: UTA behind by 5',
        or None if odds are unavailable or unparseable.
    """
    if not odds_detail or not team_abbr:
        return None
    parts = odds_detail.strip().split()
    if len(parts) < 2:
        return None
    fav_abbr = parts[0].upper()
    try:
        spread = float(parts[1])
    except ValueError:
        return None
    if spread >= 0:
        return None  # pick-em or no clear favourite

    spread_abs = abs(spread)
    if team_abbr.upper() == fav_abbr:
        # Our team is the favourite: expected to win by spread_abs
        vs_spread = diff_total - spread_abs
    else:
        # Our team is the underdog: expected to lose by spread_abs
        vs_spread = diff_total + spread_abs

    rounded = round(vs_spread)
    if rounded > 0:
        return "Line: {} beating by {}".format(team_abbr, rounded)
    elif rounded < 0:
        return "Line: {} behind by {}".format(team_abbr, abs(rounded))
    else:
        return "Line: {} right on the line".format(team_abbr)


def _format_condition_probability(cond_name, score_margin, outcomes_data, threshold=65.0):
    """
    Returns a Slack-formatted win/loss probability line, or empty string if below threshold.

    Args:
        cond_name:     Condition name to look up in outcomes_data
        score_margin:  my_score - opp_score (positive = leading, negative = trailing)
        outcomes_data: Result of outcomes.compute_outcomes() — may be None
        threshold:     Minimum win% or loss% required to show the line (default 65.0)
    """
    try:
        if not outcomes_data or not cond_name:
            return ""
        cond_stats = (outcomes_data.get("conditions") or {}).get(cond_name)
        if not cond_stats:
            return ""
        team_wins = cond_stats.get("team_wins")
        if team_wins is None:
            return ""
        # team_wins is already a percentage (0–100) from outcomes.compute_outcomes().
        win_pct  = float(team_wins)
        loss_pct = 100.0 - win_pct
        dominant_pct = max(win_pct, loss_pct)
        if dominant_pct < threshold:
            return ""
        score_margin = score_margin or 0
        if score_margin > 0:
            margin_str = "+{}".format(int(round(score_margin)))
        elif score_margin < 0:
            margin_str = "\u2212{}".format(int(round(abs(score_margin))))
        else:
            margin_str = None
        if win_pct >= threshold:
            pct = win_pct
            if score_margin > 0:
                return "📈 *Leading ({}) — wins {:.0f}% of the time when this fires*".format(margin_str, pct)
            elif score_margin < 0:
                return "📈 *Trailing ({}) — still wins {:.0f}% of the time when this fires*".format(margin_str, pct)
            else:
                return "📈 *Tied — wins {:.0f}% of the time when this fires*".format(pct)
        else:
            pct = loss_pct
            if score_margin < 0:
                return "📉 *Trailing ({}) — loses {:.0f}% of the time when this fires*".format(margin_str, pct)
            elif score_margin > 0:
                return "📉 *Leading ({}) — loses {:.0f}% of the time when this fires*".format(margin_str, pct)
            else:
                return "📉 *Tied — loses {:.0f}% of the time when this fires*".format(pct)
    except Exception:
        return ""


def _pregame_tag_key(condition_type):
    key = str(condition_type or "").strip().upper()
    return "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in key)


def _build_pregame_h1_record(ev_record, role):
    role = str(role or "").strip().lower()
    if role not in ("home", "away"):
        return None
    data = {
        "{}_w".format(role): to_int_or_none(ev_record.get("{}_w".format(role))) if isinstance(ev_record, dict) else None,
        "{}_l".format(role): to_int_or_none(ev_record.get("{}_l".format(role))) if isinstance(ev_record, dict) else None,
        "l10_{}_w".format(role): to_int_or_none(ev_record.get("l10_{}_w".format(role))) if isinstance(ev_record, dict) else None,
        "l10_{}_l".format(role): to_int_or_none(ev_record.get("l10_{}_l".format(role))) if isinstance(ev_record, dict) else None,
    }
    if all(v is None for v in data.values()):
        return None
    return data


def _build_pregame_tags_for_team(config, event, competitor, opponent, state, yesterday_events, evanalytics_records):
    tags = []
    main_comp = (event.get("competitions") or [{}])[0]
    competitors = main_comp.get("competitors") or []
    team = competitor.get("team") or {}
    team_id = str(team.get("id") or "")
    team_name = str(team.get("displayName") or "")
    context = {
        "state": state,
        "game_id": str(event.get("id") or ""),
        "yesterday_events": yesterday_events,
        "check_b2b": check_b2b,
        "quarter_num": 0,
        "config": config,
        "main_comp": main_comp,
        "competitors": competitors,
        "parse_odds_favorite": parse_odds_favorite,
        "evanalytics": evanalytics,
        # Pre-built records for h1_moneyline_edge — avoids live EVAnalytics fetch.
        # Passed in by callers that have h1_store-derived records (backfill, live monitor).
        "evanalytics_records": evanalytics_records if isinstance(evanalytics_records, dict) else None,
    }
    team_ctx = {
        "team_id": team_id,
        "team_name": team_name,
        "my_team": competitor,
        "opp": opponent,
    }

    for condition in config.get("conditions") or []:
        ctype = str((condition or {}).get("type") or "").strip()
        if ctype not in PRE_GAME_SNAPSHOT_CONDITION_TYPES:
            continue
        try:
            result = condition_logic.evaluate_condition(condition, team_ctx, context)
        except Exception:
            continue
        if not result.get("matched"):
            continue
        tags.append({
            "key": _pregame_tag_key(ctype),
            "label": str(condition.get("name") or ctype).strip() or ctype,
            "type": ctype,
        })

    h1_record = None
    if isinstance(evanalytics_records, dict) and team_name:
        ev_record = evanalytics.get_team_record(evanalytics_records, team_name)
        h1_record = _build_pregame_h1_record(ev_record, str(competitor.get("homeAway") or ""))

    return tags, h1_record


def build_pregame_snapshot_for_event(config, event, state, yesterday_events, evanalytics_records, summary_cache=None, sl_game_ids=None):
    """Build a minimal pre-game snapshot payload for one game event."""
    try:
        game_id = str(event.get("id") or "")
        main_comp = (event.get("competitions") or [{}])[0]
        competitors = main_comp.get("competitors") or []
        if len(competitors) < 2:
            return None

        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
        home_abbr = (home.get("team") or {}).get("abbreviation", "")
        away_abbr = (away.get("team") or {}).get("abbreviation", "")
        home_name = (home.get("team") or {}).get("displayName", home_abbr)
        away_name = (away.get("team") or {}).get("displayName", away_abbr)
        home_id = str((home.get("team") or {}).get("id") or "")
        away_id = str((away.get("team") or {}).get("id") or "")

        summary = fetch_summary(game_id, summary_cache) if isinstance(summary_cache, dict) else fetch_summary(game_id, {})
        pc = ((summary.get("pickcenter") or [{}])[0]) if summary else {}

        away_record = _team_record_str(away)
        home_record = _team_record_str(home)
        away_streak = _team_streak_from_summary(summary, away_id) if summary else "--"
        home_streak = _team_streak_from_summary(summary, home_id) if summary else "--"

        # Fallback: compute streak from game history when ESPN lacks lastFiveGames
        if away_streak == "--" or home_streak == "--":
            event_date = str(event.get("date") or "")[:10]
            if event_date:
                _hist = None  # loaded lazily
                if away_streak == "--":
                    _hist = outcomes.load_history()
                    away_streak = compute_streak_from_history(away_id, event_date, _hist)
                if home_streak == "--":
                    if _hist is None:
                        _hist = outcomes.load_history()
                    home_streak = compute_streak_from_history(home_id, event_date, _hist)

        away_ml = "--"
        home_ml = "--"
        away_spread = "--"
        home_spread = "--"
        if pc:
            raw_spread = pc.get("spread")
            ato = pc.get("awayTeamOdds") or {}
            hto = pc.get("homeTeamOdds") or {}

            ato_id = str(ato.get("teamId", ""))
            hto_id = str(hto.get("teamId", ""))
            if ato_id == away_id or (not ato_id and not hto_id):
                away_ml = _format_signed_ml(ato.get("moneyLine"))
                home_ml = _format_signed_ml(hto.get("moneyLine"))
            elif ato_id == home_id:
                away_ml = _format_signed_ml(hto.get("moneyLine"))
                home_ml = _format_signed_ml(ato.get("moneyLine"))
            else:
                away_ml = _format_signed_ml(ato.get("moneyLine"))
                home_ml = _format_signed_ml(hto.get("moneyLine"))

            if raw_spread is not None:
                try:
                    sv = float(raw_spread)
                    home_spread = _format_signed_spread(sv)
                    away_spread = _format_signed_spread(-sv)
                except (TypeError, ValueError):
                    pass

        # Fallback: fill missing odds from The Odds API (#213 Phase 3)
        odds_source = "espn" if (home_ml != "--" and away_ml != "--") else "none"
        if (home_ml == "--" or away_ml == "--" or home_spread == "--" or away_spread == "--"):
            if config.get("odds_api_enabled") and config.get("odds_api_key"):
                try:
                    _sl_sport = "basketball_nba_summer_league" if str(game_id) in (sl_game_ids or set()) else None
                    _oa = odds_api.fetch_live_game_odds(
                        config["odds_api_key"], home_name, away_name, logger=_log, context="pregame",
                        sport_key=_sl_sport)
                    if _oa:
                        if home_ml == "--" and _oa.get("home_ml", "--") != "--":
                            home_ml = _oa["home_ml"]
                        if away_ml == "--" and _oa.get("away_ml", "--") != "--":
                            away_ml = _oa["away_ml"]
                        if home_spread == "--" and _oa.get("home_spread", "--") != "--":
                            home_spread = _oa["home_spread"]
                        if away_spread == "--" and _oa.get("away_spread", "--") != "--":
                            away_spread = _oa["away_spread"]
                        if odds_source == "none":
                            odds_source = "odds_api"
                        elif home_ml != "--":
                            odds_source = "espn+odds_api"
                except Exception as _oa_exc:
                    _log("[ODDS_API] Pregame odds fallback failed: {}".format(_oa_exc))

        odds_list = main_comp.get("odds") or []
        spread_detail = (odds_list[0].get("details") or "").strip() if odds_list else ""
        if not spread_detail and pc:
            spread_detail = str(pc.get("details") or "").strip()
        # Build spread_detail from Odds API data if ESPN didn't provide one
        if not spread_detail and home_spread != "--" and away_spread != "--":
            try:
                _hs = float(home_spread)
                if _hs < 0:
                    spread_detail = "{} {}".format(home_abbr, home_spread)
                elif _hs > 0:
                    spread_detail = "{} {}".format(away_abbr, away_spread)
            except (TypeError, ValueError):
                pass
        spread_fav_id = parse_odds_favorite(spread_detail, competitors) or ""

        home_tags, home_h1 = _build_pregame_tags_for_team(
            config, event, home, away, state, yesterday_events, evanalytics_records
        )
        away_tags, away_h1 = _build_pregame_tags_for_team(
            config, event, away, home, state, yesterday_events, evanalytics_records
        )

        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "spread_detail": spread_detail,
            "spread_fav_id": spread_fav_id,
            "odds_source": odds_source,
            "home_tags": home_tags,
            "away_tags": away_tags,
            "home_h1_record": home_h1,
            "away_h1_record": away_h1,
            "home_matchup_meta": {
                "team_id": home_id,
                "abbr": home_abbr,
                "name": home_name,
                "moneyline": home_ml,
                "spread": home_spread,
                "record": home_record,
                "streak": home_streak,
            },
            "away_matchup_meta": {
                "team_id": away_id,
                "abbr": away_abbr,
                "name": away_name,
                "moneyline": away_ml,
                "spread": away_spread,
                "record": away_record,
                "streak": away_streak,
            },
        }
    except Exception as exc:
        _log("[WARN] Pregame snapshot capture failed for game {}: {}".format(event.get("id"), exc))
        return None


def halftime_scores(competitors):
    """Return {team_id: halftime_score} using Q1+Q2 linescores."""
    out = {}
    for c in competitors:
        ls = c.get("linescores", [])
        q1 = float(ls[0].get("value", 0)) if len(ls) > 0 else 0.0
        q2 = float(ls[1].get("value", 0)) if len(ls) > 1 else 0.0
        out[str(c["team"]["id"])] = q1 + q2
    return out


# ── State key helper ───────────────────────────────────────────────────────────

def get_state_key(team_name, cond, quarter, game_id):
    """
    Build the dedup/cooldown key for a condition hit.

    Scoping rules (checked in priority order):
      alert_once: "game"    → one alert per game       (key: GAME_{id})
      alert_once: "half"    → one alert per half        (key: GAME_{id}|H{1or2})
      alert_once: "quarter" → one alert per quarter     (key: GAME_{id}|Q{n})
      alert_once: "always"  → time-based cooldown       (key: GAME_{id}|Q{n}, re-fires after cooldown)
      scope: "game" or stat conditions → same as alert_once: "game"
    """
    ctype = cond.get("type", "score_diff")
    alert_once = cond.get("alert_once", None)

    # Stat conditions and B2B are always game-scoped
    if cond.get("scope") == "game" or ctype in STAT_COND_TYPES or ctype == "back_to_back":
        return "{}|{}|GAME_{}".format(team_name, cond["name"], game_id)

    if alert_once == "game":
        return "{}|{}|GAME_{}".format(team_name, cond["name"], game_id)

    if alert_once == "half":
        half = 1 if quarter <= 2 else 2
        return "{}|{}|GAME_{}|H{}".format(team_name, cond["name"], game_id, half)

    if alert_once == "quarter":
        return "{}|{}|GAME_{}|Q{}".format(team_name, cond["name"], game_id, quarter)

    # Default: per-quarter key (time-based cooldown applied on top)
    return "{}|{}|Q{}".format(team_name, cond["name"], quarter)


def format_score(my_team, opp):
    return "{} {} - {} {}".format(
        my_team["team"]["abbreviation"], my_team.get("score", 0),
        opp["team"]["abbreviation"],    opp.get("score", 0)
    )

def format_quarter_score(my_team, opp, quarter_idx):
    """Format the score for a specific quarter using linescores."""
    try:
        my_ls  = my_team.get("linescores", [])
        opp_ls = opp.get("linescores", [])
        if len(my_ls) > quarter_idx and len(opp_ls) > quarter_idx:
            my_q  = int(float(my_ls[quarter_idx].get("value", 0)))
            opp_q = int(float(opp_ls[quarter_idx].get("value", 0)))
            return "{} {} - {} {} (Q{})".format(
                my_team["team"]["abbreviation"], my_q,
                opp["team"]["abbreviation"],    opp_q,
                quarter_idx + 1
            )
    except Exception:
        pass
    return None


def format_game_clock(status):
    """Format ESPN status clock (seconds remaining) as 'M:SS'."""
    try:
        secs = float((status or {}).get("clock", 0) or 0)
    except (TypeError, ValueError):
        return ""
    mins = int(secs) // 60
    sec = int(secs) % 60
    return "{}:{:02d}".format(mins, sec)


def compute_quarter_elapsed_pct(status, quarter_num):
    """Return elapsed percentage (0..100) for the current quarter, or None."""
    try:
        clock_remaining = float((status or {}).get("clock", 0) or 0)
    except (TypeError, ValueError):
        return None

    quarter_len = 300.0 if int(quarter_num or 0) > 4 else 720.0
    if quarter_len <= 0:
        return None

    clock_remaining = min(max(clock_remaining, 0.0), quarter_len)
    elapsed = quarter_len - clock_remaining
    return round((elapsed / quarter_len) * 100.0, 1)


def get_config_warnings(config):
    """Return non-fatal config warnings to log at startup."""
    warnings = []

    conditions = config.get("conditions", [])
    compounds  = config.get("compound_conditions", [])

    if not isinstance(conditions, list):
        return ["'conditions' must be an array; cannot validate compound references"]

    cond_names = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            warnings.append("conditions[{}] is not an object".format(i))
            continue
        name = cond.get("name")
        if not isinstance(name, str) or not name.strip():
            warnings.append("conditions[{}].name is missing or empty".format(i))
            continue
        cond_names.append(name.strip())

    seen = set()
    dupes = set()
    for name in cond_names:
        if name in seen:
            dupes.add(name)
        seen.add(name)
    if dupes:
        warnings.append(
            "Duplicate condition names detected (compound matching is name-based): {}".format(
                ", ".join(sorted(dupes))
            )
        )

    if compounds is None:
        compounds = []
    if not isinstance(compounds, list):
        warnings.append("'compound_conditions' must be an array")
        return warnings

    cond_name_set = set(cond_names)
    for i, cc in enumerate(compounds):
        if not isinstance(cc, dict):
            warnings.append("compound_conditions[{}] is not an object".format(i))
            continue
        cc_name = cc.get("name") if isinstance(cc.get("name"), str) and cc.get("name").strip() else "compound_conditions[{}]".format(i)
        refs = cc.get("condition_refs", [])
        if not isinstance(refs, list) or not refs:
            warnings.append("{} has no condition_refs and will never trigger".format(cc_name))
            continue

        invalid = [
            r for r in refs
            if not isinstance(r, str) or not r.strip() or r.strip() not in cond_name_set
        ]
        if invalid:
            warnings.append(
                "{} references unknown condition(s): {}".format(
                    cc_name,
                    ", ".join(str(r) for r in invalid)
                )
            )

    return warnings


# ── Daily digest ───────────────────────────────────────────────────────────────

def _check_and_send_digest(config, state):
    """Send the daily 1H ML digest if it's the right time and hasn't been sent today."""
    digest_cfg = config.get("daily_digest", {})
    target_time_str = digest_cfg.get("time_utc", "20:00")

    now_utc = datetime.now(timezone.utc)
    try:
        target_h, target_m = map(int, target_time_str.split(":"))
    except (ValueError, TypeError):
        return

    # Check if we're in the 1-minute window
    if now_utc.hour != target_h or now_utc.minute != target_m:
        return

    today_str = now_utc.strftime("%Y-%m-%d")
    if state.get("last_digest_date") == today_str:
        return  # Already sent today

    # Load h1_records store for digest; fallback to EVAnalytics if unavailable
    store = h1_store.load_h1_records_store()
    ml_records = h1_store.build_all_evanalytics_records(store) if store.get("teams") else {}
    if not ml_records:
        ml_records = evanalytics.fetch_1h_ml_records(config)
    msg = digest.build_daily_digest(config, ml_records)

    if msg:
        channel = digest_cfg.get("slack_channel", config.get("slack_channel"))
        send_alert_result({**config, "slack_channel": channel}, msg)

    state["last_digest_date"] = today_str
    state["__digest_dirty"] = True


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    _perf_t0  = time.perf_counter()
    _perf_ru0 = resource.getrusage(resource.RUSAGE_SELF)

    config   = load_config()
    state    = load_state()

    # Derive league-specific ESPN URLs from config
    global ESPN_URL, SUMMARY_URL, YESTERDAY_URL
    _league = config.get("league", _DEFAULT_LEAGUE)
    if _league not in VALID_LEAGUES:
        _league = _DEFAULT_LEAGUE
    ESPN_URL, SUMMARY_URL, YESTERDAY_URL = _espn_urls(_league)
    _LEAGUE_LABEL = _league.upper()  # "NBA" or "WNBA"

    now      = time.time()
    cooldown = config.get("alert_cooldown_minutes", 15) * 60
    outcome_criteria = normalize_postgame_outcome_criteria(config)
    config_fingerprint = outcomes.compute_config_fingerprint(config)
    _pw_version = pw_trend.current_pw_version()
    state_dirty = False

    # ── Summer League odds capture thread (#384) ─────────────────────────────
    _sl_odds_monitor = None
    if config.get("summer_league_odds_enabled"):
        _sl_odds_monitor = sl_odds_mod.SummerLeagueOddsMonitor(config)
        _sl_odds_monitor.start()

    # ── Timer health check (#227) ────────────────────────────────────────────
    _last_run_ts = state.get("__last_run_ts")
    if _last_run_ts is not None:
        try:
            _gap_sec = now - float(_last_run_ts)
            _gap_min = _gap_sec / 60.0
            _had_live = bool(state.get("prev_live_games"))
            if _gap_min > 5 and _had_live:
                _log("[TIMER_HEALTH] Gap of {:.0f}m since last run — live games were active".format(_gap_min))
            elif _gap_min > 10:
                _log("[TIMER_HEALTH] Gap of {:.0f}m since last run".format(_gap_min))
        except (TypeError, ValueError):
            pass
    state["__last_run_ts"] = now
    state_dirty = True

    disk_status = get_disk_space_status()
    disk_level = disk_status.get("level", "unknown")
    prev_disk_level = state.get("__disk_space_level")
    if disk_level != prev_disk_level:
        state["__disk_space_level"] = disk_level
        state_dirty = True

    if disk_level in ("warn", "critical") and disk_level != prev_disk_level:
        _log(
            "[WARN] Low disk space: {:.1f} MB free ({:.2f}% free) on {} (warn <= {} MB, critical <= {} MB)".format(
                float(disk_status.get("free_mb", 0.0)),
                float(disk_status.get("free_pct", 0.0)),
                disk_status.get("path", SCRIPT_DIR),
                int(disk_status.get("warn_free_mb", DISK_WARN_FREE_MB)),
                int(disk_status.get("critical_free_mb", DISK_CRITICAL_FREE_MB)),
            )
        )
    elif disk_level == "unknown" and disk_level != prev_disk_level:
        _log("[WARN] Disk preflight unavailable: {}".format(disk_status.get("error", "unknown error")))

    rotate_logs(config)

    for warning in get_config_warnings(config):
        _log("[WARN] Config:", warning)

    # ── Odds API window-boundary credit check (#266) ────────────────────────
    # Only log startup record on the first run of a new UTC day (window open)
    # and at date rollover (window close is handled in daily stats section).
    if config.get("odds_api_enabled") and config.get("odds_api_key"):
        odds_api.configure_fallback_save(
            config.get("odds_api_fallback_save_enabled", False),
            config.get("odds_api_fallback_retention_days", 7))
        odds_api.configure_request_log(
            config.get("odds_api_request_log_enabled", False),
            config.get("odds_api_request_log_retention_days", 1))
        _oa_today = datetime.now(timezone.utc).date().isoformat()
        if state.get("__odds_api_startup_date") != _oa_today:
            odds_api.log_startup(config["odds_api_key"], logger=_log)
            state["__odds_api_startup_date"] = _oa_today
            state_dirty = True

    # ── Daily 1H ML digest ─────────────────────────────────────────────────────
    if config.get("daily_digest", {}).get("enabled"):
        _check_and_send_digest(config, state)
        if state.get("__digest_dirty"):
            state_dirty = True
            del state["__digest_dirty"]

    conditions          = config.get("conditions", [])
    compound_conditions = config.get("compound_conditions", [])
    monitor_all         = config.get("monitor_all_teams", False)
    team_list           = config.get("teams", [])

    needs_stats = any(c.get("type") in STAT_COND_TYPES for c in conditions)

    # ── Load history once per run and cache outcomes (issue #147) ────────────────
    # Avoids loading the 63 MB game_history.json multiple times per cycle.
    _prob_threshold = float(config.get("condition_alert_probability_threshold", 65))
    try:
        _cached_history = outcomes.load_history()
    except Exception:
        _cached_history = []
    _cached_history_index = outcomes.build_condition_index(_cached_history) if _cached_history else None
    try:
        _cached_outcomes_data = outcomes.compute_outcomes(
            min_sample=outcomes.MIN_SAMPLE,
            decay_lambda=config.get("outcomes_decay_lambda", 0.0),
        )
    except Exception:
        _cached_outcomes_data = None

    # h1_records store: load from disk (built by backfill.py, updated on game completion)
    _h1_store = h1_store.load_h1_records_store()

    # ── Fetch scoreboard ───────────────────────────────────────────────────────
    try:
        resp = fetch_with_retry(ESPN_URL)
        data = resp.json()
    except Exception as e:
        _log("[ERROR] ESPN fetch failed:", e)
        return

    # ── Summer League dual scoreboard (#387) — NBA only, not WNBA (#393) ──
    _sl_game_ids = set()
    if league_config.LEAGUE == "nba" and _is_summer_league_date(datetime.now(timezone.utc).date(), config):
        try:
            _sl_resp = fetch_with_retry(ESPN_SL_SCOREBOARD)
            _sl_data = _sl_resp.json()
            _sl_events = _sl_data.get("events", [])
            for _sl_ev in _sl_events:
                _sl_game_ids.add(str(_sl_ev.get("id", "")))
            data.setdefault("events", []).extend(_sl_events)
            if _sl_events:
                _log("[SL] Merged {} Summer League events".format(len(_sl_events)))
        except Exception as _sl_exc:
            _log("[SL] Summer League fetch failed (non-fatal): {}".format(_sl_exc))

    summary_cache  = {}
    live_stats_out = {}     # written to live_stats.json at end
    triggered_map  = {}     # cond_name -> list of hit dicts (for compounds)
    all_hits       = []     # flat list of base condition hits
    alerted_hit_keys = set()  # base-condition hits successfully sent to Slack this run
    pw_threshold_ctx = {}   # (game_id, team_id) -> context for predicted_winner_threshold 2nd pass

    # ── Auto-recovery: detect games that fell off the scoreboard (#227) ──────
    # The current scoreboard only shows "today's" games (ET).  If the monitor
    # was offline when a game from the previous scoreboard went FINAL, that
    # game is never seen as finished.  Check the prior scoreboard date for
    # any FINAL games missing from game_history and recover them.
    _recovery_checked_key = "__recovery_checked_date"
    _today_sb_date = None
    for _ev in data.get("events", []):
        _today_sb_date = _game_scoreboard_date(_ev)
        if _today_sb_date is not None:
            break
    if _today_sb_date is None:
        _today_sb_date = (datetime.now(timezone.utc) - timedelta(hours=6)).date()

    _prev_sb_date = _today_sb_date - timedelta(days=1)
    _prev_date_str = _prev_sb_date.isoformat()
    _last_recovery_date = state.get(_recovery_checked_key)

    if _last_recovery_date != _prev_date_str:
        # Build set of game IDs already in history
        _history_ids = {str(r.get("game_id", "")) for r in _cached_history}

        try:
            _prev_events = fetch_prior_day_events(_prev_sb_date)
        except Exception:
            _prev_events = []

        _missed_games = []
        for _ev in _prev_events:
            _gid = str(_ev.get("id") or "")
            if not _gid:
                continue
            _ev_state = str(((_ev.get("status") or {}).get("type") or {}).get("state") or "")
            if _ev_state == "post" and _gid not in _history_ids and not state.get("history_recorded_{}".format(_gid)):
                _missed_games.append((_gid, _ev.get("shortName") or _gid))

        if _missed_games:
            try:
                import backfill as _backfill_mod
            except Exception:
                _backfill_mod = None

            if _backfill_mod:
                _allowed_types = set(_backfill_mod.PARITY_APPROVED_TYPES)
                for _gid, _short in _missed_games:
                    try:
                        _log("[AUTO-RECOVERY] Detected missed game {} ({}) on {} scoreboard — recovering".format(
                            _gid, _short, _prev_date_str))
                        _rec_result = _recover_missed_game(
                            _gid, _prev_sb_date, config, conditions,
                            compound_conditions, outcome_criteria,
                            config_fingerprint, _allowed_types, state,
                        )
                        if _rec_result:
                            _log("[AUTO-RECOVERY] Recovered game {} ({})".format(_gid, _short))
                            state_dirty = True
                            # Reload history so the rest of the run sees the new record
                            _cached_history = outcomes.load_history()
                            _cached_history_index = outcomes.build_condition_index(_cached_history) if _cached_history else None
                        else:
                            _log("[AUTO-RECOVERY] Game {} — no record written (already exists or error)".format(_gid))
                    except Exception as _rec_exc:
                        _log("[WARN] Auto-recovery failed for game {}: {}".format(_gid, _rec_exc))
            else:
                _log("[WARN] Auto-recovery: backfill module unavailable, skipping {} missed game(s)".format(len(_missed_games)))

        state[_recovery_checked_key] = _prev_date_str
        state_dirty = True

    # ── B2B pre-check for all live games ──────────────────────────────────────
    # Determine the prior-day scoreboard per game's own ESPN scoreboard date
    # (US local), NOT a single global UTC "yesterday" (issue #124).
    needs_b2b = any(c.get("type") == "back_to_back" for c in conditions)
    prior_events_by_date = {}
    if needs_b2b:
        prior_dates_needed = set()
        for event in data.get("events", []):
            if (event.get("status") or {}).get("type", {}).get("state") != "in":
                continue
            sb_date = _game_scoreboard_date(event)
            if sb_date is not None:
                prior_dates_needed.add(sb_date - timedelta(days=1))
        for d in prior_dates_needed:
            prior_events_by_date[d] = fetch_prior_day_events(d)

    yesterday_events = []
    for event in data.get("events", []):
        if (event.get("status") or {}).get("type", {}).get("state") != "in":
            continue
        game_id = str(event["id"])
        sb_date = _game_scoreboard_date(event)
        if needs_b2b and sb_date is not None:
            yesterday_events = prior_events_by_date.get(sb_date - timedelta(days=1), [])
        for comp in event.get("competitions", []):
            for c in comp.get("competitors", []):
                tid = str((c.get("team") or {}).get("id", ""))
                bk  = "b2b_{}_{}".format(game_id, tid)
                # Always re-evaluate B2B each run — yesterday_events is fetched fresh
                # and a cached True from a prior run (e.g. transient ESPN data or
                # first-run timing near UTC midnight) should not persist.
                state.pop(bk, None)
                check_b2b(tid, state, game_id, yesterday_events)
                state_dirty = True

    prev_live = set(state.get("prev_live_games", []))
    current_live_ids = {
        str(ev["id"])
        for ev in data.get("events", [])
        if (ev.get("status") or {}).get("type", {}).get("state") == "in"
    }
    just_started = current_live_ids - prev_live
    just_finished = prev_live - current_live_ids

    # Also catch games already in "post" state that were never seen live
    # (e.g. monitor was offline/suspended while the game was in progress).
    retroactive_game_ids = set()
    for ev in data.get("events", []):
        gid = str(ev.get("id") or "")
        if not gid:
            continue
        ev_state = str(((ev.get("status") or {}).get("type") or {}).get("state") or "")
        if ev_state == "post" and not state.get("final_alerted_{}".format(gid)):
            if gid not in prev_live:
                retroactive_game_ids.add(gid)
            just_finished.add(gid)

    # ── Retroactive enrichment for missed games ──────────────────────────────
    # For games that finished while the monitor was offline, retroactively
    # evaluate conditions from ESPN cached data so the post-game card is
    # fully populated instead of showing minimal information.
    if retroactive_game_ids:
        try:
            import backfill as _backfill_mod
            _retro_allowed = _backfill_mod.PARITY_APPROVED_TYPES
        except Exception:
            _backfill_mod = None
            _retro_allowed = set()

        for ev in data.get("events", []):
            gid = str(ev.get("id") or "")
            if gid not in retroactive_game_ids:
                continue

            _ev_short = ev.get("shortName") or gid
            _log("[WARN] Game {} ({}) was not live-tracked — monitor was offline. "
                 "Generating retroactive post-game data from ESPN cache.".format(gid, _ev_short))

            # Load ESPN cached summary into summary_cache so build_game_summary
            # and history recording can use it.
            _cache_path = os.path.join(league_config.cache_dir(), "game_summary", "{}.json".format(gid))
            if os.path.exists(_cache_path) and gid not in summary_cache:
                try:
                    with open(_cache_path) as _cf:
                        summary_cache[gid] = json.load(_cf)
                except Exception as _ce:
                    _log("[WARN] Failed to load ESPN cache for {}: {}".format(gid, _ce))

            # Retroactively evaluate conditions via backfill replay
            if _backfill_mod and summary_cache.get(gid):
                try:
                    _replay_state = {}
                    _retro_hits, _ = _backfill_mod.evaluate_conditions_for_game(
                        event=ev,
                        summary=summary_cache[gid],
                        cfg_conditions=conditions,
                        cfg_compound_conditions=config.get("compound_conditions", []),
                        allowed_types=_retro_allowed,
                        yesterday_events=yesterday_events,
                        replay_state=_replay_state,
                    )
                    # Inject hits into alert state so get_game_condition_hits
                    # picks them up for summary and history recording.
                    # backfill returns conditions_fired dicts with "name" key.
                    for h in _retro_hits:
                        persist_game_condition_hit(state, {
                            "ts":        datetime.now(timezone.utc).isoformat(),
                            "type":      str(h.get("type") or ""),
                            "team":      str(h.get("team") or ""),
                            "condition": str(h.get("name") or ""),
                            "color":     "",
                            "score_str": "",
                            "quarter_str":        str(h.get("quarter_str") or ""),
                            "direction":          str(h.get("direction") or ""),
                            "game_id":            gid,
                            "delivery_ok":        False,
                            "score_margin_at_fire":  h.get("score_margin_at_fire"),
                            "margin_diff":          h.get("margin_diff"),
                            "quarter_elapsed_pct":  h.get("quarter_elapsed_pct"),
                            "game_time_seconds":    h.get("game_time_seconds"),
                            "time_anchor":          h.get("time_anchor"),
                            "home_away_role":       h.get("home_away_role"),
                            "home_score_at_fire":   h.get("home_score_at_fire"),
                            "away_score_at_fire":   h.get("away_score_at_fire"),
                        })
                    state_dirty = True
                    if _retro_hits:
                        _log("[INFO] Retroactive replay found {} condition hits for game {} ({})".format(
                            len(_retro_hits), gid, _ev_short))
                    else:
                        _log("[INFO] Retroactive replay found no condition hits for game {} ({})".format(gid, _ev_short))
                except Exception as _re:
                    _log("[WARN] Retroactive condition replay failed for {}: {}".format(gid, _re))
            elif not summary_cache.get(gid):
                _log("[WARN] No ESPN cache available for retroactive replay of game {}".format(gid))

    pregame_snapshots = state.get("pregame_snapshots")
    if not isinstance(pregame_snapshots, dict):
        pregame_snapshots = {}
    needs_h1_snapshot = any(str((c or {}).get("type") or "") == "h1_moneyline_edge" for c in conditions)
    if needs_h1_snapshot and _h1_store.get("teams"):
        # Use local h1_records store (built/updated by backfill.py + monitor) instead of EVAnalytics
        evanalytics_records = h1_store.build_all_evanalytics_records(_h1_store)
    elif needs_h1_snapshot:
        # Fallback to EVAnalytics when h1_store has no teams yet
        evanalytics_records = evanalytics.fetch_1h_ml_records(config)
    else:
        evanalytics_records = {}
    for event in data.get("events", []):
        game_state = str(((event.get("status") or {}).get("type") or {}).get("state") or "").strip().lower()
        if game_state not in ("pre", "in"):
            continue
        game_id = str(event.get("id") or "")
        if not game_id or game_id in pregame_snapshots:
            continue
        pregame_snapshots[game_id] = build_pregame_snapshot_for_event(
            config,
            event,
            state,
            yesterday_events,
            evanalytics_records,
            summary_cache=summary_cache,
            sl_game_ids=_sl_game_ids,
        )
        state_dirty = True
        if pregame_snapshots.get(game_id) is None:
            _log("[WARN] Pregame snapshot unavailable for game {}".format(game_id))
    state["pregame_snapshots"] = pregame_snapshots

    # ── End-of-game alerts ─────────────────────────────────────────────────────
    if config.get("end_of_game_alerts", True):

        for event in data.get("events", []):
            game_id = str(event["id"])
            if game_id not in just_finished:
                continue
            if (event.get("status") or {}).get("type", {}).get("state") != "post":
                continue

            comp_list   = event.get("competitions", [])
            if not comp_list:
                continue
            main_comp   = comp_list[0]
            competitors = main_comp.get("competitors", [])
            if len(competitors) < 2:
                continue

            # Respect team filter
            if not monitor_all:
                monitored = any(
                    tname.lower() in c["team"]["displayName"].lower()
                    for tname in team_list
                    for c in competitors
                )
                if not monitored:
                    continue

            final_key = "final_alerted_{}".format(game_id)
            if state.get(final_key):
                continue

            home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[0])
            away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[1])
            home_score = int(home.get("score") or 0)
            away_score = int(away.get("score") or 0)
            home_abbr  = home["team"].get("abbreviation", home["team"]["displayName"])
            away_abbr  = away["team"].get("abbreviation", away["team"]["displayName"])

            status_desc = (event.get("status") or {}).get("type", {}).get("description", "Final")

            if home_score > away_score:
                winner = home_abbr
            elif away_score > home_score:
                winner = away_abbr
            else:
                winner = None

            result_str = "🏆 {} win".format(winner) if winner else "Tie"
            ts   = _format_alert_time(datetime.now(timezone.utc), home_abbr)

            msg = (
                "_{}_\n"
                ":basketball: 🏁 *{}* — {} {} · {} {}\n"
                "{}"
            ).format(
                ts,
                status_desc,
                away_abbr, away_score,
                home_abbr, home_score,
                result_str
            )
            if str(game_id) in _sl_game_ids:
                msg = "[SL] " + msg

            sent_ok, sent_err = send_alert_result(config, msg)
            if sent_ok:
                state[final_key] = True
                state_dirty = True
            is_retroactive = game_id in retroactive_game_ids
            entry = {
                "ts":          datetime.now(timezone.utc).isoformat(),
                "type":        "final",
                "team":        "{} @ {}".format(away_abbr, home_abbr),
                "condition":   status_desc,
                "color":       "",
                "score_str":   "{} {} - {} {}".format(away_abbr, away_score, home_abbr, home_score),
                "quarter_str": status_desc,
                "direction":   result_str,
                "game_id":     game_id,
                "delivery_ok": bool(sent_ok),
            }
            if is_retroactive:
                entry["retroactive"] = True
            if not sent_ok and sent_err:
                entry["delivery_error"] = sent_err
            update_alert_log(entry)

            # ── Game summary (upset / close game / Q4 comeback) ────────────────
            if config.get("end_of_game_alerts", True):
                try:
                    # Load current alerts log for condition history
                    log_path = league_config.state_path("alerts.json")
                    try:
                        with open(log_path) as f:
                            alerts_log = json.load(f)
                    except Exception:
                        alerts_log = []
                    game_condition_hits = get_game_condition_hits(state, game_id, alerts_log)

                    summary_key = "summary_alerted_{}".format(game_id)
                    if not state.get(summary_key):
                        summary_msg, tags, meta, summary_tags = build_game_summary(
                            event, summary_cache, game_condition_hits, conditions, outcome_criteria
                        )
                        if summary_msg:
                            if str(game_id) in _sl_game_ids:
                                summary_msg = "[SL] " + summary_msg
                            sum_ok, _ = send_alert_result(config, summary_msg)
                            if sum_ok:
                                state[summary_key] = True
                                state_dirty = True
                            primary_tag_color = "yellow"
                            if summary_tags:
                                primary_tag_color = str(summary_tags[0].get("color") or "yellow").strip().lower() or "yellow"
                            _oa_summary = odds_api.format_game_stats(game_id)
                            log_entry = {
                                "ts":          datetime.now(timezone.utc).isoformat(),
                                "type":        "summary",
                                "team":        "{} @ {}".format(away_abbr, home_abbr),
                                "condition":   "Game Summary",
                                "color":       primary_tag_color,
                                "score_str":   "{} {} - {} {}".format(away_abbr, away_score, home_abbr, home_score),
                                "quarter_str": "Final",
                                "direction":   " · ".join(tags),
                                "game_id":     game_id,
                                "delivery_ok": bool(sum_ok),
                                "matchup_meta": meta,
                                "summary_tags": summary_tags,
                            }
                            if _oa_summary:
                                log_entry["odds_api_stats"] = _oa_summary
                            if is_retroactive:
                                log_entry["retroactive"] = True
                            update_alert_log(log_entry)
                        elif is_retroactive:
                            # No interesting tags, but still write a summary
                            # entry with matchup_meta for the dashboard card.
                            _retro_summary = fetch_summary(game_id, summary_cache)
                            _retro_meta = _build_retroactive_matchup_meta(
                                event, _retro_summary, home, away,
                            )
                            log_entry = {
                                "ts":          datetime.now(timezone.utc).isoformat(),
                                "type":        "summary",
                                "team":        "{} @ {}".format(away_abbr, home_abbr),
                                "condition":   "Game Summary",
                                "color":       "",
                                "score_str":   "{} {} - {} {}".format(away_abbr, away_score, home_abbr, home_score),
                                "quarter_str": "Final",
                                "direction":   result_str,
                                "game_id":     game_id,
                                "delivery_ok": True,
                                "matchup_meta": _retro_meta,
                                "summary_tags": [],
                                "retroactive": True,
                            }
                            update_alert_log(log_entry)
                            state[summary_key] = True
                            state_dirty = True
                except Exception as e:
                    _log("[WARN] Game summary failed for {}: {}".format(game_id, e))

            # ── Append to game history ─────────────────────────────────────────
            try:
                history_key = "history_recorded_{}".format(game_id)
                if not state.get(history_key):
                    # Gather conditions that fired for this game from alerts log
                    log_path = league_config.state_path("alerts.json")
                    try:
                        with open(log_path) as f:
                            alerts_log = json.load(f)
                    except Exception:
                        alerts_log = []

                    # Reload summary for meta (may already be cached)
                    summary = fetch_summary(game_id, summary_cache)
                    pc = ((summary.get("pickcenter") or [{}])[0]) if summary else {}
                    odds_list   = main_comp.get("odds") or []
                    odds_detail = (odds_list[0].get("details") or "") if odds_list else ""
                    if not odds_detail and pc:
                        odds_detail = pc.get("details") or ""

                    fav_id = parse_odds_favorite(odds_detail, competitors)
                    winner_id = str(home["team"]["id"]) if home_score > away_score else (
                                str(away["team"]["id"]) if away_score > home_score else None)

                    unavailable_inputs = []
                    was_upset = None
                    if fav_id and winner_id:
                        was_upset = bool(winner_id != fav_id)
                    elif not fav_id:
                        unavailable_inputs.append("odds")
                    was_close    = abs(home_score - away_score) <= int(outcome_criteria.get("close_margin", 5))
                    was_comeback = q4_tie_after_deficit(
                        competitors,
                        summary=summary,
                        deficit=int(outcome_criteria.get("comeback_deficit", 6)),
                        allow_fallback=not bool(outcome_criteria.get("require_q4_pbp_for_comeback", False)),
                    )

                    game_alerts = get_game_condition_hits(state, game_id, alerts_log)
                    # Dedupe by condition+team
                    seen_cf = set()
                    conditions_fired = []
                    for a in game_alerts:
                        cf_key = (a.get("condition",""), a.get("team",""))
                        if cf_key in seen_cf:
                            continue
                        seen_cf.add(cf_key)
                        # Determine which team triggered and whether that team won
                        a_team = a.get("team","")
                        a_team_id = ""
                        for c in competitors:
                            if c["team"]["displayName"] == a_team:
                                a_team_id = str(c["team"].get("id", ""))
                                break
                        team_won = bool(winner_id and a_team_id and winner_id == a_team_id)
                        score_margin_at_fire = a.get("score_margin_at_fire")
                        margin_diff = None
                        try:
                            if score_margin_at_fire is not None:
                                game_margin = float(abs(home_score - away_score))
                                margin_at_fire = float(score_margin_at_fire)
                                final_margin_from_cond_team = game_margin if team_won else -game_margin
                                margin_diff = final_margin_from_cond_team - margin_at_fire
                        except (TypeError, ValueError):
                            margin_diff = None
                        _cf_entry = {
                            "name":        a.get("condition",""),
                            "type":        a.get("type",""),
                            "team":        a_team,
                            "team_id":     a_team_id,
                            "quarter_str": a.get("quarter_str",""),
                            "direction":   a.get("direction",""),
                            "alerted":     bool(a.get("delivery_ok")),
                            "team_won":    team_won,
                            "score_margin_at_fire": score_margin_at_fire,
                            "margin_diff": margin_diff,
                            "quarter_elapsed_pct": a.get("quarter_elapsed_pct"),
                            "game_time_seconds": a.get("game_time_seconds"),
                            "time_anchor": a.get("time_anchor"),
                            "home_away_role": a.get("home_away_role"),
                            "home_score_at_fire": a.get("home_score_at_fire"),
                            "away_score_at_fire": a.get("away_score_at_fire"),
                        }
                        # Attach condition edge from rolling store (#217)
                        try:
                            _edge_store = outcomes.ConditionEdgeStore.load_or_rebuild()
                            _cf_entry["condition_edge"] = round(_edge_store.edge(a.get("condition", "")), 1)
                        except Exception:
                            pass
                        conditions_fired.append(_cf_entry)

                    # Sort conditions chronologically: PRE before Q1, then by
                    # game_time_seconds or quarter+elapsed_pct (issue #124).
                    def _cf_sort_key(c):
                        qs = str(c.get("quarter_str") or "").strip().upper()
                        if qs == "PRE":
                            return (-1, 0.0)
                        gts = c.get("game_time_seconds")
                        if gts is not None:
                            try:
                                return (int(gts), 0.0)
                            except (TypeError, ValueError):
                                pass
                        if qs.startswith("Q") and qs[1:].isdigit():
                            qn = int(qs[1:])
                        else:
                            qn = 99
                        qep = c.get("quarter_elapsed_pct")
                        try:
                            qep = float(qep) if qep is not None else 100.0
                        except (TypeError, ValueError):
                            qep = 100.0
                        quarter_len = 300.0 if qn >= 5 else 720.0
                        synthetic_gts = ((qn - 1) * 720 if qn <= 4 else (4 * 720 + (qn - 5) * 300)) + (qep / 100.0 * quarter_len)
                        return (int(synthetic_gts), qep)
                    conditions_fired.sort(key=_cf_sort_key)

                    postgame_tags = evaluate_postgame_tag_keys(
                        competitors, summary, outcome_criteria,
                        margin=abs(home_score - away_score),
                        fav_id=fav_id,
                        winner_id=winner_id,
                    )

                    pregame_snapshot = None
                    snapshots = state.get("pregame_snapshots")
                    if isinstance(snapshots, dict):
                        candidate = snapshots.get(game_id)
                        if isinstance(candidate, dict):
                            pregame_snapshot = candidate

                    event_dt = parse_event_datetime_utc(event)
                    season_segment = get_season_segment(event_dt.date(), league=_DEFAULT_LEAGUE, config=config)

                    # Extract per-team box score stats (#280)
                    _home_id_str = str(home["team"]["id"])
                    _away_id_str = str(away["team"]["id"])
                    _home_stats = extract_team_stats(summary, _home_id_str)
                    _away_stats = extract_team_stats(summary, _away_id_str)

                    # Extract quarter scores from linescores (#280)
                    _home_linescores = home.get("linescores") or []
                    _away_linescores = away.get("linescores") or []
                    _home_quarters = {}
                    _away_quarters = {}
                    for ls in _home_linescores:
                        p = ls.get("period")
                        v = ls.get("value")
                        if p and v is not None:
                            _home_quarters["Q{}".format(p)] = int(v)
                    for ls in _away_linescores:
                        p = ls.get("period")
                        v = ls.get("value")
                        if p and v is not None:
                            _away_quarters["Q{}".format(p)] = int(v)
                    _home_1h = (_home_quarters.get("Q1", 0) + _home_quarters.get("Q2", 0)) if _home_quarters else None
                    _away_1h = (_away_quarters.get("Q1", 0) + _away_quarters.get("Q2", 0)) if _away_quarters else None

                    # Extract per-team odds from pregame_snapshot (#280)
                    _home_ml = None
                    _away_ml = None
                    _home_spread = None
                    _away_spread = None
                    _odds_bookmaker = None
                    if pregame_snapshot:
                        _home_ml = pregame_snapshot.get("home_ml")
                        _away_ml = pregame_snapshot.get("away_ml")
                        _home_spread = pregame_snapshot.get("home_spread")
                        _away_spread = pregame_snapshot.get("away_spread")
                        _odds_bookmaker = pregame_snapshot.get("bookmaker") or pregame_snapshot.get("ml_source")

                    record = {
                        "schema_version": outcomes.HISTORY_SCHEMA_VERSION,
                        "game_id":      game_id,
                        "date":         event_dt.isoformat(),
                        "record_updated_at": datetime.now(timezone.utc).isoformat(),
                        "away_abbr":    away_abbr,
                        "home_abbr":    home_abbr,
                        "away_name":    away["team"].get("displayName", away_abbr),
                        "home_name":    home["team"].get("displayName", home_abbr),
                        "away_score":   away_score,
                        "home_score":   home_score,
                        "away_id":      _away_id_str,
                        "home_id":      _home_id_str,
                        "spread_detail":odds_detail,
                        "spread_fav_id":fav_id or "",
                        "winner_id":    winner_id or "",
                        "was_upset":    was_upset,
                        "was_close":    was_close,
                        "was_comeback": was_comeback,
                        "margin":       abs(home_score - away_score),
                        "source":       "live",
                        "replay_mode":  "live_poll",
                        "config_fingerprint": config_fingerprint,
                        "season_segment": season_segment,
                        "unavailable_inputs": unavailable_inputs,
                        "pregame_snapshot": pregame_snapshot,
                        # Closing prices for CLV (#412 Phase 5.1)
                        "closing_home_ml": (pregame_snapshot or {}).get("home_matchup_meta", {}).get("moneyline"),
                        "closing_away_ml": (pregame_snapshot or {}).get("away_matchup_meta", {}).get("moneyline"),
                        "postgame_tags_matched": postgame_tags,
                        "conditions_fired": conditions_fired,
                        # Enriched fields (#280)
                        "home_stats":   _home_stats,
                        "away_stats":   _away_stats,
                        "home_quarters": _home_quarters or None,
                        "away_quarters": _away_quarters or None,
                        "home_score_1h": _home_1h,
                        "away_score_1h": _away_1h,
                        "home_ml":      _home_ml,
                        "away_ml":      _away_ml,
                        "home_spread":  _home_spread,
                        "away_spread":  _away_spread,
                        "odds_bookmaker": _odds_bookmaker,
                    }

                    # Resolve and attach predicted winner call history, if any.
                    pw_calls_key = "pw_calls_{}".format(game_id)
                    pw_calls_raw = state.get(pw_calls_key)
                    if isinstance(pw_calls_raw, list) and pw_calls_raw:
                        for call in pw_calls_raw:
                            if isinstance(call, dict) and call.get("predicted_team_id"):
                                call["correct"] = bool(
                                    winner_id and winner_id == call["predicted_team_id"]
                                )
                                # Resolve quarter/half outcomes (#374)
                                _q = call.get("quarter", "")
                                _pred_id = call["predicted_team_id"]
                                _is_pred_home = (_pred_id == _home_id_str)
                                _hq = _home_quarters or {}
                                _aq = _away_quarters or {}

                                if _q in ("Q1", "Q2", "Q3", "Q4") and _hq and _aq:
                                    # Quarter winner
                                    _pred_q = _hq.get(_q, 0) if _is_pred_home else _aq.get(_q, 0)
                                    _opp_q = _aq.get(_q, 0) if _is_pred_home else _hq.get(_q, 0)
                                    call["q_correct"] = bool(_pred_q > _opp_q) if (_pred_q != _opp_q) else False

                                    # Quarter spread cover
                                    _bk_q_spr = call.get("bk_q_spread")
                                    if _bk_q_spr is not None:
                                        try:
                                            _q_margin = _pred_q - _opp_q
                                            call["q_covered"] = (_q_margin + float(_bk_q_spr)) > 0
                                        except (ValueError, TypeError):
                                            pass

                                    # H1 outcomes (Q1/Q2 fires only)
                                    if _q in ("Q1", "Q2"):
                                        _pred_h1 = (_hq.get("Q1", 0) + _hq.get("Q2", 0)) if _is_pred_home else (_aq.get("Q1", 0) + _aq.get("Q2", 0))
                                        _opp_h1 = (_aq.get("Q1", 0) + _aq.get("Q2", 0)) if _is_pred_home else (_hq.get("Q1", 0) + _hq.get("Q2", 0))
                                        call["h1_correct"] = bool(_pred_h1 > _opp_h1) if (_pred_h1 != _opp_h1) else False

                                        _bk_h1_spr = call.get("bk_h1_spread")
                                        if _bk_h1_spr is not None:
                                            try:
                                                _h1_margin = _pred_h1 - _opp_h1
                                                call["h1_covered"] = (_h1_margin + float(_bk_h1_spr)) > 0
                                            except (ValueError, TypeError):
                                                pass
                        record["predicted_winner_calls"] = pw_calls_raw
                        state.pop(pw_calls_key, None)

                        # ── End-of-game PW call summary (issue #152) ─────
                        try:
                            _pw_total = len(pw_calls_raw)
                            _pw_correct = sum(1 for c in pw_calls_raw if c.get("correct"))
                            _pw_wrong = _pw_total - _pw_correct
                            _pw_acc = (_pw_correct / _pw_total * 100) if _pw_total else 0

                            # Winner name
                            _pw_winner = home_abbr if winner_id == str(home["team"]["id"]) else away_abbr

                            # Group calls by predicted team
                            _pw_by_team = {}
                            for _c in pw_calls_raw:
                                _pt = _c.get("predicted_team", "?")
                                _pw_by_team.setdefault(_pt, {"n": 0, "correct": 0, "pcts": [], "quarters": set()})
                                _pw_by_team[_pt]["n"] += 1
                                if _c.get("correct"):
                                    _pw_by_team[_pt]["correct"] += 1
                                _pw_by_team[_pt]["pcts"].append(_c.get("pct", 0))
                                _pw_by_team[_pt]["quarters"].add(_c.get("quarter", "?"))

                            _pw_team_parts = []
                            for _pt, _ps in sorted(_pw_by_team.items(), key=lambda kv: -kv[1]["n"]):
                                _avg_pct = sum(_ps["pcts"]) / len(_ps["pcts"]) if _ps["pcts"] else 0
                                _qs = ",".join(sorted(_ps["quarters"]))
                                _mark = "correct" if _ps["correct"] == _ps["n"] else (
                                    "wrong" if _ps["correct"] == 0 else "{}/{}".format(_ps["correct"], _ps["n"]))
                                _pw_team_parts.append("{} x{} avg={:.0f}% [{}] ({})".format(
                                    _pt, _ps["n"], _avg_pct, _qs, _mark))

                            _log("[PW_SUMMARY] {} {} vs {} {} | winner={} | calls={} correct={} wrong={} acc={:.0f}% | {}".format(
                                away_abbr, away_score, home_abbr, home_score,
                                _pw_winner, _pw_total, _pw_correct, _pw_wrong, _pw_acc,
                                " | ".join(_pw_team_parts)))
                        except Exception:
                            pass

                    outcomes.append_game_record(record)
                    state[history_key] = True

                    # ── Append PW calls to JSONL export (#457) ──
                    if isinstance(pw_calls_raw, list) and pw_calls_raw:
                        try:
                            _exp_path = os.path.join(SCRIPT_DIR, league_config.state_path("pw_export.jsonl"))
                            _exp_ctx = {
                                "game_id": str(record.get("game_id", "")),
                                "game_date": str(record.get("date", "")),
                                "home_team": str(record.get("home_name") or record.get("home_abbr", "")),
                                "away_team": str(record.get("away_name") or record.get("away_abbr", "")),
                                "home_id": str(record.get("home_id", "")),
                                "away_id": str(record.get("away_id", "")),
                                "home_abbr": str(record.get("home_abbr", "")),
                                "away_abbr": str(record.get("away_abbr", "")),
                                "home_score": record.get("home_score"),
                                "away_score": record.get("away_score"),
                                "winner_id": str(record.get("winner_id", "")),
                                "spread_fav_id": str(record.get("spread_fav_id", "")),
                                "season_segment": str(record.get("season_segment", "")),
                                "game_source": "live",
                                "game_completed": True,
                            }
                            _exp_lines = []
                            for _ec in pw_calls_raw:
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

                    # ── Game-end summary Slack alert (#427) ──
                    _gsa_cfg = config.get("pw_alert_filters") or {}
                    if _gsa_cfg.get("enabled") and _gsa_cfg.get("game_summary"):
                        try:
                            _gsa_margin = abs(home_score - away_score)
                            _gsa_winner = home["team"].get("displayName", home_abbr) if home_score > away_score else (
                                away["team"].get("displayName", away_abbr) if away_score > home_score else "Draw")
                            _gsa_away_name = away["team"].get("displayName", away_abbr)
                            _gsa_home_name = home["team"].get("displayName", home_abbr)
                            _gsa_pw = pw_calls_raw if isinstance(pw_calls_raw, list) else []
                            _gsa_nonsup = [c for c in _gsa_pw if not c.get("_suppressed")]
                            _gsa_correct = sum(1 for c in _gsa_nonsup if c.get("correct"))
                            _gsa_total = len(_gsa_nonsup)
                            _gsa_acc = "{}/{} correct ({:.0f}%)".format(_gsa_correct, _gsa_total, _gsa_correct / _gsa_total * 100) if _gsa_total else "No PW calls"
                            # BK ML units
                            _gsa_ml_pnl = 0.0
                            _gsa_ml_n = 0
                            for _gc in _gsa_nonsup:
                                _gml = _gc.get("bk_moneyline")
                                if _gml is not None:
                                    try:
                                        _gml_f = float(_gml)
                                        if _gc.get("correct"):
                                            _gsa_ml_pnl += (100 * (100 / abs(_gml_f))) if _gml_f < 0 else (100 * (_gml_f / 100))
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
                                        _juice = float(_gsp) if _gsp else -110
                                        if _covered:
                                            _gsa_spr_cov += 1
                                            _gsa_spr_pnl += (100 * (100 / abs(_juice))) if _juice < 0 else (100 * (_juice / 100))
                                        else:
                                            _gsa_spr_pnl -= 100
                                        _gsa_spr_n += 1
                                    except (ValueError, TypeError):
                                        pass
                            _gsa_parts = [
                                "\U0001f4cb *{} Game Summary*".format(_LEAGUE_LABEL),
                                "{} {} - {} {}".format(_gsa_away_name, away_score, _gsa_home_name, home_score),
                                "\U0001f3c6 {} wins by {}".format(_gsa_winner, _gsa_margin) if _gsa_margin else "\U0001f3c6 Draw",
                                "PW: {}".format(_gsa_acc),
                            ]
                            if _gsa_ml_n:
                                _gsa_parts.append("BK ML: {:+.1f}u".format(_gsa_ml_pnl / 100))
                            if _gsa_spr_n:
                                _gsa_parts.append("BK Spr: {}/{} covered {:+.1f}u".format(_gsa_spr_cov, _gsa_spr_n, _gsa_spr_pnl / 100))
                            _gsa_msg = "\n".join(_gsa_parts)
                            if str(game_id) in _sl_game_ids:
                                _gsa_msg = "[SL] " + _gsa_msg
                            _gsa_ok, _ = send_alert_result(config, _gsa_msg)
                            _log("[GAME_SUMMARY_ALERT] {} {} vs {} {} sent={}".format(
                                away_abbr, away_score, home_abbr, home_score, _gsa_ok))
                        except Exception as _gsa_ex:
                            _log("[GAME_SUMMARY_ALERT] Error: {}".format(_gsa_ex))

                    if isinstance(snapshots, dict) and game_id in snapshots:
                        snapshots.pop(game_id, None)
                        state["pregame_snapshots"] = snapshots
                    state_dirty = True
                    _log("[HISTORY] Recorded game {} ({} {} vs {} {})".format(
                        game_id, away_abbr, away_score, home_abbr, home_score))

                    # ── Update h1_records store with halftime result ───────────
                    try:
                        if _h1_store is not None and summary:
                            _home_id = str(home["team"]["id"])
                            _away_id = str(away["team"]["id"])
                            _home_name = home["team"].get("displayName", home_abbr)
                            _away_name = away["team"].get("displayName", away_abbr)
                            _h1_home, _h1_away = h1_store.get_h1_winner_from_summary(
                                summary, _home_id, _away_id
                            )
                            _today_str = event_dt.date().isoformat()
                            h1_store.update_h1_records_store(
                                _h1_store, _home_id, _away_id,
                                _home_name, _away_name,
                                _h1_home, _h1_away, _today_str,
                                league=league_config.LEAGUE,
                            )
                            h1_store.save_h1_records_store(_h1_store)
                            _log("[H1] Updated h1_records for game {} ({} @ {})".format(
                                game_id, away_abbr, home_abbr))
                    except Exception as _h1e:
                        _log("[WARN] h1_records update failed for {}: {}".format(game_id, _h1e))

            except Exception as e:
                _log("[WARN] History record failed for {}: {}".format(game_id, e))

    # Update tracked live game IDs for next run
    if current_live_ids != prev_live:
        state["prev_live_games"] = sorted(current_live_ids)
        state_dirty = True

    # ── ESPN cache writes (issue #87 Phase 2) ──────────────────────────────────
    # Persist every completed game's summary + play-by-play to espn_cache/.
    # Best-effort; per-game idempotent via state["cache_written_{id}"]; bounded
    # deferred-retry guards against ESPN's brief post-final hydration window.
    try:
        if process_cache_writes_for_completed_games(data.get("events", []), state, summary_cache):
            state_dirty = True
    except Exception as _cexc:
        _log("[WARN] cache write pass failed: {}".format(_cexc))

    # ── Scoreboard cache (issue #126) ──────────────────────────────────────────
    # Persist the full scoreboard payload once per tick. Idempotent by
    # overwrite — the file keeps being rewritten until it stabilises at the
    # final slate state. No state-key bookkeeping needed.
    try:
        _sb_path = cache_scoreboard(data)
        if _sb_path:
            _log("[CACHE] write_ok scoreboard_{}".format(
                os.path.splitext(os.path.basename(_sb_path))[0]))
    except Exception as _sbexc:
        _log("[WARN] [CACHE] cache_scoreboard failed: {}".format(_sbexc))

    # ── Main event loop ────────────────────────────────────────────────────────
    for event in data.get("events", []):
        status = (event.get("status") or {})
        if status.get("type", {}).get("state") != "in":
            continue

        game_id     = str(event["id"])
        quarter_num = int(status.get("period", 0))
        quarter_str = QUARTER_NAMES.get(quarter_num, "Q{}".format(quarter_num))
        quarter_elapsed_pct = compute_quarter_elapsed_pct(status, quarter_num)

        comp_list   = event.get("competitions", [])
        if not comp_list:
            continue
        main_comp   = comp_list[0]
        competitors = main_comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        # Build candidate (my_team, opponent) pairs
        if monitor_all:
            pairs = [(competitors[i], competitors[1 - i]) for i in range(2)]
        else:
            pairs = []
            for tname in team_list:
                for i, c in enumerate(competitors):
                    if tname.lower() in c["team"]["displayName"].lower():
                        pairs.append((c, competitors[1 - i]))

        # Fetch summary once per game if stat conditions exist
        summary = fetch_summary(game_id, summary_cache) if needs_stats else None
        scoring_runs_cache = {"game": extract_scoring_runs(summary)} if summary else {"game": []}

        # Resolve pre-game odds for the live handicap line. ESPN's scoreboard
        # feed frequently returns competitions[].odds = [] for in-progress games,
        # so fall back to the summary API's pickcenter[0].details (e.g. 'LAC -5.5').
        # Fetch summary lazily if we didn't already need it for stats.
        _game_odds_detail = ""
        _scoreboard_odds = main_comp.get("odds") or []
        if _scoreboard_odds:
            _game_odds_detail = (_scoreboard_odds[0].get("details") or "").strip()
        if not _game_odds_detail:
            _sum_for_odds = summary if summary is not None else fetch_summary(game_id, summary_cache)
            if _sum_for_odds:
                _pc_list = _sum_for_odds.get("pickcenter") or []
                if _pc_list:
                    _game_odds_detail = (_pc_list[0].get("details") or "").strip()
        # Extract per-team moneyline + spread from pickcenter for alert enrichment (#149)
        _game_team_odds = {}  # team_id -> {"moneyline": str, "spread": str, "spread_role": "Favorite"/"Underdog"}
        try:
            _pc_for_odds = None
            if _sum_for_odds:
                _pc_for_odds = ((_sum_for_odds.get("pickcenter") or [{}])[0])
            elif summary is not None:
                _pc_for_odds = ((summary.get("pickcenter") or [{}])[0])
            if _pc_for_odds:
                _ato = _pc_for_odds.get("awayTeamOdds") or {}
                _hto = _pc_for_odds.get("homeTeamOdds") or {}
                _ato_id = str(_ato.get("teamId", ""))
                _hto_id = str(_hto.get("teamId", ""))
                _home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
                _away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
                _home_tid = str(_home_c["team"]["id"]) if _home_c else ""
                _away_tid = str(_away_c["team"]["id"]) if _away_c else ""
                # Map moneylines by team id
                if _ato_id == _away_tid or (not _ato_id and not _hto_id):
                    _ml_away = _format_signed_ml(_ato.get("moneyLine"))
                    _ml_home = _format_signed_ml(_hto.get("moneyLine"))
                elif _ato_id == _home_tid:
                    _ml_away = _format_signed_ml(_hto.get("moneyLine"))
                    _ml_home = _format_signed_ml(_ato.get("moneyLine"))
                else:
                    _ml_away = _format_signed_ml(_ato.get("moneyLine"))
                    _ml_home = _format_signed_ml(_hto.get("moneyLine"))
                # Map spreads
                _raw_sp = _pc_for_odds.get("spread")
                _sp_home = "--"
                _sp_away = "--"
                if _raw_sp is not None:
                    try:
                        _sv = float(_raw_sp)
                        _sp_home = _format_signed_spread(_sv)
                        _sp_away = _format_signed_spread(-_sv)
                    except (TypeError, ValueError):
                        pass
                # Determine favorite from odds_detail
                _fav_tid = parse_odds_favorite(_game_odds_detail, competitors)
                for _tid, _ml, _sp in [(_home_tid, _ml_home, _sp_home), (_away_tid, _ml_away, _sp_away)]:
                    if _tid:
                        _role = "Favorite" if _tid == _fav_tid else "Underdog" if _fav_tid else ""
                        _game_team_odds[_tid] = {"moneyline": _ml, "spread": _sp, "spread_role": _role}
        except Exception:
            pass  # Never let odds extraction break the main loop
        team_ids = [str(c["team"]["id"]) for c in competitors]
        team_raw_stats = {
            tid: (extract_team_stats(summary, tid) or {})
            for tid in team_ids
        }
        # Diagnostic: log when boxscore stats are missing for a live game
        for _diag_tid in team_ids:
            if summary and not team_raw_stats.get(_diag_tid):
                _diag_name = next(
                    (c["team"]["displayName"] for c in competitors
                     if str(c["team"]["id"]) == _diag_tid), _diag_tid)
                _log("[WARN] [STATS_MISSING] {} (id={}) in game {} — boxscore statistics empty, stat conditions will not fire".format(
                    _diag_name, _diag_tid, game_id))
        team_split_stats = extract_team_stat_splits(summary, team_ids, team_raw_stats, quarter_num)

        # Build live_stats entry for this game
        game_entry = {"quarter": quarter_str, "game_clock": format_game_clock(status), "teams": {}}
        for c in competitors:
            tid  = str(c["team"]["id"])
            raw  = team_raw_stats.get(tid, {})
            rate = compute_turnover_rate(raw)
            game_entry["teams"][tid] = {
                "name":     c["team"]["displayName"],
                "abbr":     c["team"].get("abbreviation", ""),
                "b2b":      bool(state.get("b2b_{}_{}".format(game_id, tid), False)),
                "score":    c.get("score", "0"),
                "homeAway": c.get("homeAway", ""),
                "stats": {
                    "points":             int(float(c.get("score") or 0)),
                    "fieldGoalPct":       round(float(raw.get("fieldGoalPct") or 0), 1) if raw else None,
                    "threePointers":      "{}-{}".format(
                        int(float(raw.get("threePointFieldGoalsMade") or 0)),
                        int(float(raw.get("threePointFieldGoalsAttempted") or 0)),
                    ) if raw else None,
                    "threePointPct":      round(float(raw.get("threePointFieldGoalPct") or 0), 1) if raw else None,
                    "freeThrows":         "{}-{}".format(
                        int(float(raw.get("freeThrowsMade") or 0)),
                        int(float(raw.get("freeThrowsAttempted") or 0)),
                    ) if raw else None,
                    "freeThrowPct":       round(float(raw.get("freeThrowPct") or 0), 1) if raw else None,
                    "turnovers":          int(float(raw.get("turnovers") or 0)) if raw else None,
                    "turnoverRate":       rate,
                    "pointsOffTurnovers": int(float(raw.get("pointsOffTurnovers") or 0)) if raw else None,
                    "fouls":              int(float(raw.get("fouls") or 0)) if raw else None,
                },
                "stats_split": team_split_stats.get(tid, build_default_team_stat_split(raw)),
            }
            # Inject points into splits from linescores (score/points come
            # from the competitor object, not the boxscore stats).
            _split = game_entry["teams"][tid].get("stats_split") or {}
            _total = _split.get("total")
            if isinstance(_total, dict):
                _total["points"] = int(float(c.get("score") or 0))
            _ls = c.get("linescores") or []
            for qi, qk in enumerate(["q1", "q2", "q3", "q4"]):
                _qd = _split.get(qk)
                if isinstance(_qd, dict) and qi < len(_ls):
                    try:
                        _qd["points"] = int(float(_ls[qi].get("value", 0)))
                    except (TypeError, ValueError):
                        pass
            for hk, q_indices in [("h1", [0, 1]), ("h2", [2, 3])]:
                _hd = _split.get(hk)
                if isinstance(_hd, dict):
                    h_pts = 0
                    h_has = False
                    for qi in q_indices:
                        if qi < len(_ls):
                            try:
                                h_pts += int(float(_ls[qi].get("value", 0)))
                                h_has = True
                            except (TypeError, ValueError):
                                pass
                    if h_has:
                        _hd["points"] = h_pts
        live_stats_out[game_id] = game_entry

        # ── Synthetic odds (#376) ─────────────────────────────────────────────
        try:
            import synthetic_odds
            _synth_home_c = next((c for c in competitors if c.get("homeAway") == "home"), None)
            _synth_away_c = next((c for c in competitors if c.get("homeAway") == "away"), None)
            _synth_home_score = int(float((_synth_home_c or {}).get("score") or 0))
            _synth_away_score = int(float((_synth_away_c or {}).get("score") or 0))
            _synth_margin = _synth_home_score - _synth_away_score  # home perspective
            _synth_pregame = None
            _synth_fav_id = parse_odds_favorite(_game_odds_detail, competitors)
            _synth_home_id = str((_synth_home_c or {}).get("team", {}).get("id", "")) if _synth_home_c else ""
            if _game_odds_detail and _synth_fav_id:
                _sd_parts = _game_odds_detail.strip().rsplit(" ", 1)
                if len(_sd_parts) == 2:
                    try:
                        _sd_line = float(_sd_parts[1])
                        if _synth_fav_id == _synth_home_id:
                            _synth_pregame = _sd_line
                        else:
                            _synth_pregame = -_sd_line
                    except (TypeError, ValueError):
                        pass
            _synth = synthetic_odds.compute_all(
                _synth_margin, quarter_str, _synth_pregame, league_config.LEAGUE
            )
            game_entry["synth_odds"] = _synth
        except Exception:
            pass

        # Home team abbreviation for timezone display in alerts
        _home_comp_for_tz = next((c for c in competitors if c.get("homeAway") == "home"), None)
        _game_home_abbr = (_home_comp_for_tz.get("team") or {}).get("abbreviation", "") if _home_comp_for_tz else ""

        # ── Evaluate conditions per team ───────────────────────────────────────
        for my_team, opp in pairs:
            team_name = my_team["team"]["displayName"]
            team_id   = str(my_team["team"]["id"])
            my_score  = int(my_team.get("score") or 0)
            opp_score = int(opp.get("score") or 0)
            is_losing = my_score < opp_score
            diff_total = my_score - opp_score
            score_str  = format_score(my_team, opp)
            raw_stats  = team_raw_stats.get(team_id) or None
            team_linescore = my_team.get("linescores", [])
            team_ctx = {
                "my_team": my_team,
                "opp": opp,
                "team_name": team_name,
                "team_id": team_id,
                "my_score": my_score,
                "opp_score": opp_score,
                "is_losing": is_losing,
                "diff_total": diff_total,
                "raw_stats": raw_stats,
                "stats_split": team_split_stats.get(team_id),
            }
            condition_context = {
                "config": config,
                "state": state,
                "game_id": game_id,
                "quarter_num": quarter_num,
                "main_comp": main_comp,
                "competitors": competitors,
                "summary": summary,
                "scoring_runs_cache": scoring_runs_cache,
                "yesterday_events": yesterday_events,
                "compute_turnover_rate": compute_turnover_rate,
                "get_linescore_diff": get_linescore_diff,
                "check_b2b": check_b2b,
                "parse_odds_favorite": parse_odds_favorite,
                "halftime_scores": halftime_scores,
                "extract_scoring_runs": extract_scoring_runs,
                "evanalytics": evanalytics,
                "linescore_for_team": team_linescore,
            }

            # Capture context for predicted_winner_threshold second pass
            pw_threshold_ctx[(game_id, team_id)] = {
                "team_ctx": team_ctx,
                "team_name": team_name,
                "team_id": team_id,
                "team_abbr": my_team.get("team", {}).get("abbreviation", ""),
                "home_abbr": _game_home_abbr,
                "score_str": score_str,
                "quarter_str": quarter_str,
                "quarter_num": quarter_num,
                "quarter_elapsed_pct": quarter_elapsed_pct,
                "game_clock": format_game_clock(status),
                "game_id": game_id,
                "home_away": my_team.get("homeAway", ""),
                "score_margin_at_fire": diff_total,
                "home_score_at_fire": my_score if my_team.get("homeAway") == "home" else (opp_score if my_team.get("homeAway") == "away" else None),
                "away_score_at_fire": opp_score if my_team.get("homeAway") == "home" else (my_score if my_team.get("homeAway") == "away" else None),
                "odds_detail": _game_odds_detail,
                "team_odds": _game_team_odds.get(team_id, {}),
            }

            for cond in conditions:
                ctype     = cond.get("type", "score_diff")
                if ctype == "predicted_winner_threshold":
                    continue  # evaluated in second pass after live analysis
                state_key = get_state_key(team_name, cond, quarter_num, game_id)
                eval_result = condition_logic.evaluate_condition(cond, team_ctx, condition_context)
                if eval_result.get("state_dirty"):
                    state_dirty = True

                matched = bool(eval_result.get("matched"))
                direction = eval_result.get("direction", "")
                run_key = eval_result.get("run_key")
                run_points = eval_result.get("run_points")
                run_scope = eval_result.get("run_scope")

                if not matched:
                    continue

                # Quarter score for score_diff conditions with use_quarter_score
                quarter_score_str = None
                if ctype == "score_diff" and cond.get("use_quarter_score"):
                    quarter_score_str = format_quarter_score(my_team, opp, quarter_num - 1)

                _ha = my_team.get("homeAway", "")
                _home_score_at_fire = my_score if _ha == "home" else (opp_score if _ha == "away" else None)
                _away_score_at_fire = opp_score if _ha == "home" else (my_score if _ha == "away" else None)
                _odds_detail = _game_odds_detail
                hit = {
                    "cond":              cond,
                    "team_name":         team_name,
                    "team_id":           team_id,
                    "team_abbr":         my_team.get("team", {}).get("abbreviation", ""),
                    "home_abbr":         _game_home_abbr,
                    "score_str":         score_str,
                    "quarter_score_str": quarter_score_str,
                    "direction":         direction,
                    "quarter":           quarter_num,
                    "quarter_str":       quarter_str,
                    "event_id":          game_id,
                    "state_key":         state_key,
                    "run_key":           run_key,
                    "run_points":        run_points,
                    "run_scope":         run_scope,
                    "home_away":         _ha,
                    "score_margin_at_fire": diff_total,
                    "quarter_elapsed_pct": quarter_elapsed_pct,
                    "home_away_role": _ha,
                    "home_score_at_fire": _home_score_at_fire,
                    "away_score_at_fire": _away_score_at_fire,
                    "odds_detail":       _odds_detail,
                    "team_odds":         _game_team_odds.get(team_id, {}),
                }
                triggered_map.setdefault(cond["name"], []).append(hit)
                all_hits.append(hit)

    # ── Compound condition evaluation ──────────────────────────────────────────
    suppressed = set()

    for cc in compound_conditions:
        refs     = cc.get("condition_refs", [])
        operator = cc.get("operator", "AND").upper()

        # Scope compound matching by (game_id, team_id) so different teams/games do not cross-match.
        scoped_hits = {}  # (event_id, team_id) -> {ref_name: [hit, ...]}
        for ref_name in refs:
            for h in triggered_map.get(ref_name, []):
                scope_key = (h["event_id"], h["team_id"])
                scoped_hits.setdefault(scope_key, {}).setdefault(ref_name, []).append(h)

        for scope_key, ref_map in scoped_hits.items():
            if operator == "AND":
                if not all(r in ref_map for r in refs):
                    continue
                matched_refs = {r: ref_map[r] for r in refs}
            else:
                matched_refs = {r: ref_map[r] for r in refs if r in ref_map}
                if not matched_refs:
                    continue

            anchor_hit = next(iter(matched_refs.values()))[0]
            game_id = anchor_hit["event_id"]
            team_id = anchor_hit["team_id"]
            cc_key  = "__compound__|{}|{}|{}".format(cc["name"], game_id, team_id)
            last_cc = state.get(cc_key, 0)
            if now - last_cc < cooldown:
                continue

            _cc_alert_mode = _get_alert_mode(cc)
            if _cc_alert_mode in ("slack", "history"):
                ts    = _format_alert_time(datetime.now(timezone.utc), anchor_hit.get("home_abbr", ""))
                emoji = COLOR_EMOJI.get(cc.get("color", ""), "🔔")
                lines = ["_{}_".format(ts), ":basketball: {} *Compound Alert* — *{}*".format(emoji, cc["name"])]
                if cc.get("description"):
                    lines.append("_{}_ ".format(cc["description"]))
                lines.append("")

                compound_hits = []
                for ref_name, hits in matched_refs.items():
                    for h in hits[:2]:
                        lines.append(
                            "• *{}* ({} | {} | _{}_ )\n  _{}_".format(
                                h["team_name"], h["quarter_str"], h["score_str"],
                                h["direction"], ref_name
                            )
                        )
                        compound_hits.append({
                            "team":        h["team_name"],
                            "score_str":   h["score_str"],
                            "quarter_str": h["quarter_str"],
                            "direction":   h["direction"],
                            "ref_name":    ref_name,
                            "game_id":     h["event_id"],
                        })
                # Build stats line for compound alert (#250)
                _cc_stats = []
                _cc_margin = anchor_hit.get("score_margin_at_fire")
                _cc_home_s = anchor_hit.get("home_score_at_fire")
                _cc_away_s = anchor_hit.get("away_score_at_fire")
                if _cc_away_s is not None and _cc_home_s is not None:
                    _cc_stats.append("Score: {}-{}".format(_cc_away_s, _cc_home_s))
                if _cc_margin is not None:
                    _cc_stats.append("Margin: {:+d}".format(int(_cc_margin)))
                _cc_odds = anchor_hit.get("team_odds") or {}
                _cc_role = _cc_odds.get("spread_role", "")
                _cc_ml = _cc_odds.get("moneyline", "")
                _cc_sp = _cc_odds.get("spread", "")
                if _cc_role:
                    _cc_stats.append(_cc_role)
                if _cc_ml and _cc_ml != "--":
                    _cc_stats.append("Odds: {}".format(_cc_ml))
                if _cc_sp and _cc_sp != "--":
                    _cc_stats.append("Handicap: {}".format(_cc_sp))
                if _cc_sp and _cc_sp != "--" and _cc_margin is not None:
                    try:
                        _cc_ll = float(_cc_sp) - int(_cc_margin)
                        _cc_stats.append("Live Spread: {:+.1f}".format(_cc_ll))
                    except (TypeError, ValueError):
                        pass
                if _cc_stats:
                    lines.append(" · ".join(_cc_stats))
                # Enrich compound alert with 1H ML data
                try:
                    _ml_role = anchor_hit.get("home_away")
                    if _ml_role:
                        if _h1_store.get("teams"):
                            _ml_records = h1_store.build_all_evanalytics_records(_h1_store)
                        else:
                            _ml_records = evanalytics.fetch_1h_ml_records(config)
                        _ml_rec = evanalytics.get_team_record(_ml_records, anchor_hit["team_name"])
                        _ml_line = evanalytics.format_record_str(_ml_rec, _ml_role) if _ml_rec else ""
                        if _ml_line:
                            lines.append(_ml_line)
                except Exception:
                    pass
                # Enrich compound alert with win/loss probability from outcomes history.
                _cc_prob_line = ""
                try:
                    for _ref_name in list(matched_refs.keys()):
                        _cc_prob_line = _format_condition_probability(
                            _ref_name,
                            anchor_hit.get("score_margin_at_fire", 0),
                            _cached_outcomes_data,
                            threshold=_prob_threshold,
                        )
                        if _cc_prob_line:
                            break
                    if not _cc_prob_line:
                        _cc_prob_line = _format_condition_probability(
                            cc["name"],
                            anchor_hit.get("score_margin_at_fire", 0),
                            _cached_outcomes_data,
                            threshold=_prob_threshold,
                        )
                    if _cc_prob_line:
                        lines.append(_cc_prob_line)
                except Exception:
                    pass

                _cc_msg = "\n".join(lines)
                if str(game_id) in _sl_game_ids:
                    _cc_msg = "[SL] " + _cc_msg
                if _cc_alert_mode == "slack":
                    sent_ok, sent_err = send_alert_result(config, _cc_msg)
                else:
                    sent_ok, sent_err = True, None
                if sent_ok:
                    state[cc_key] = now
                    state_dirty   = True
                entry = {
                    "ts":          datetime.now(timezone.utc).isoformat(),
                    "type":        "compound",
                    "team":        anchor_hit["team_name"],
                    "condition":   cc["name"],
                    "operator":    operator,
                    "color":       cc.get("color", ""),
                    "score_str":   compound_hits[0]["score_str"] if compound_hits else "",
                    "quarter_str": compound_hits[0]["quarter_str"] if compound_hits else "",
                    "direction":   "",
                    "game_id":     game_id,
                    "hits":        compound_hits,
                    "score_margin_at_fire": anchor_hit.get("score_margin_at_fire"),
                    "quarter_elapsed_pct": anchor_hit.get("quarter_elapsed_pct"),
                    "home_away_role": anchor_hit.get("home_away_role"),
                    "home_score_at_fire": anchor_hit.get("home_score_at_fire"),
                    "away_score_at_fire": anchor_hit.get("away_score_at_fire"),
                    "delivery_ok": bool(sent_ok),
                }
                if _cc_prob_line:
                    entry["probability_line"] = _cc_prob_line
                if not sent_ok and sent_err:
                    entry["delivery_error"] = sent_err
                update_alert_log(entry)
                if persist_game_condition_hit(state, entry):
                    state_dirty = True

            if cc.get("suppress_base_alerts", False):
                for hits in matched_refs.values():
                    for h in hits:
                        suppressed.add(h["state_key"])

    # ── Base condition alerts ──────────────────────────────────────────────────
    for h in all_hits:
        sk = h["state_key"]
        hit_key = (h["event_id"], h["team_id"], h["cond"]["name"], sk)
        if sk in suppressed:
            continue

        cond_type  = h["cond"].get("type", "score_diff")
        alert_once = h["cond"].get("alert_once", None)
        run_once_key = None

        # Determine suppression logic:
        # 1. B2B — always once per game (permanent flag)
        # 2. consecutive_points_run — once per unique run (permanent flag)
        # 3. alert_once: "game" / "half" / "quarter" — once per scope (permanent flag)
        # 4. default — time-based cooldown

        if cond_type == "back_to_back":
            if state.get("fired_{}".format(sk)):
                continue
        elif cond_type == "consecutive_points_run":
            run_once_key = "fired_{}|RUN_{}".format(sk, h.get("run_key") or "")
            if state.get(run_once_key):
                continue
        elif alert_once in ("game", "half", "quarter"):
            # Permanent once-per-scope suppression via fired_ flag
            if state.get("fired_{}".format(sk)):
                continue
        elif now - state.get(sk, 0) < cooldown:
            continue

        cond  = h["cond"]
        color = cond.get("color", "")
        emoji = COLOR_EMOJI.get(color, "🔔")
        ts    = _format_alert_time(datetime.now(timezone.utc), h.get("home_abbr", ""))

        _alert_mode = _get_alert_mode(cond)
        if _alert_mode in ("slack", "history"):
            # Build stats line for condition alert (#250)
            _ca_stats = []
            _ca_margin = h.get("score_margin_at_fire")
            _ca_home_s = h.get("home_score_at_fire")
            _ca_away_s = h.get("away_score_at_fire")
            if _ca_away_s is not None and _ca_home_s is not None:
                _ca_stats.append("Score: {}-{}".format(_ca_away_s, _ca_home_s))
            if _ca_margin is not None:
                _ca_stats.append("Margin: {:+d}".format(int(_ca_margin)))
            _ca_odds = h.get("team_odds") or {}
            _ca_role = _ca_odds.get("spread_role", "")
            _ca_ml = _ca_odds.get("moneyline", "")
            _ca_sp = _ca_odds.get("spread", "")
            if _ca_role:
                _ca_stats.append(_ca_role)
            if _ca_ml and _ca_ml != "--":
                _ca_stats.append("Odds: {}".format(_ca_ml))
            if _ca_sp and _ca_sp != "--":
                _ca_stats.append("Handicap: {}".format(_ca_sp))
            if _ca_sp and _ca_sp != "--" and _ca_margin is not None:
                try:
                    _ca_ll = float(_ca_sp) - int(_ca_margin)
                    _ca_stats.append("Live Spread: {:+.1f}".format(_ca_ll))
                except (TypeError, ValueError):
                    pass
            # Condition edge
            try:
                _ca_edge_store = outcomes.ConditionEdgeStore.load_or_rebuild()
                _ca_edge = _ca_edge_store.edge(cond["name"])
                _ca_stats.append("Edge: {:+.1f}".format(_ca_edge))
            except Exception:
                pass
            _ca_stats_line = " · ".join(_ca_stats) if _ca_stats else ""

            msg = (
                "_{}_\n"
                ":basketball: {} *{} Alert* — {}\n"
                "{} | {} | _{}_\n"
                "Condition: *{}* — {}\n"
                "{}"
            ).format(
                ts,
                emoji, _LEAGUE_LABEL, h["team_name"],
                h["quarter_str"], h["score_str"], h["direction"],
                cond["name"], cond.get("description", ""),
                _ca_stats_line
            )
            # Enrich with 1H ML record if available
            try:
                _ml_role = h.get("home_away")
                if _ml_role:
                    # Try h1_store first, fallback to EVAnalytics
                    if _h1_store.get("teams"):
                        _ml_records = h1_store.build_all_evanalytics_records(_h1_store)
                    else:
                        _ml_records = evanalytics.fetch_1h_ml_records(config)
                    _ml_rec = evanalytics.get_team_record(_ml_records, h["team_name"])
                    _ml_line = evanalytics.format_record_str(_ml_rec, _ml_role) if _ml_rec else ""
                    if _ml_line:
                        msg += "\n" + _ml_line
            except Exception:
                pass  # Never let enrichment break an alert
            # Enrich with win/loss probability from outcomes history
            _prob_line = ""
            try:
                _prob_line = _format_condition_probability(
                    cond["name"],
                    h.get("score_margin_at_fire", 0),
                    _cached_outcomes_data,
                    threshold=_prob_threshold,
                )
                if _prob_line:
                    msg += "\n" + _prob_line
            except Exception:
                pass  # Never let enrichment break an alert
            if str(h["event_id"]) in _sl_game_ids:
                msg = "[SL] " + msg
            _log("[ALERT]", emoji, h["team_name"], "—", cond["name"], "[mode:{}]".format(_alert_mode))
            if _alert_mode == "slack":
                sent_ok, sent_err = send_alert_result(config, msg)
            else:
                sent_ok, sent_err = True, None  # history-only: skip Slack
            if sent_ok:
                alerted_hit_keys.add(hit_key)
                if cond_type == "back_to_back":
                    state["fired_{}".format(sk)] = True
                elif cond_type == "consecutive_points_run":
                    state[run_once_key] = True
                elif alert_once in ("game", "half", "quarter"):
                    state["fired_{}".format(sk)] = True
                else:
                    state[sk] = now
                state_dirty = True
            entry = {
                "ts":          datetime.now(timezone.utc).isoformat(),
                "type":        cond.get("type", "score_diff"),
                "team":        h["team_name"],
                "condition":   cond["name"],
                "color":       color,
                "score_str":   h["score_str"],
                "quarter_str": h["quarter_str"],
                "direction":   h["direction"],
                "game_id":     h["event_id"],
                "score_margin_at_fire": h.get("score_margin_at_fire"),
                "margin_diff": h.get("margin_diff"),
                "quarter_elapsed_pct": h.get("quarter_elapsed_pct"),
                "game_time_seconds": h.get("game_time_seconds"),
                "time_anchor": h.get("time_anchor"),
                "home_away_role": h.get("home_away_role"),
                "home_score_at_fire": h.get("home_score_at_fire"),
                "away_score_at_fire": h.get("away_score_at_fire"),
                "delivery_ok": bool(sent_ok),
            }
            if _prob_line:
                entry["probability_line"] = _prob_line
            if not sent_ok and sent_err:
                entry["delivery_error"] = sent_err
            update_alert_log(entry)
            if persist_game_condition_hit(state, entry):
                state_dirty = True
        else:
            if cond_type == "back_to_back":
                state["fired_{}".format(sk)] = True
            elif cond_type == "consecutive_points_run":
                state[run_once_key] = True
            elif alert_once in ("game", "half", "quarter"):
                state["fired_{}".format(sk)] = True
            else:
                state[sk] = now
            state_dirty = True
            silent_entry = {
                "ts":          datetime.now(timezone.utc).isoformat(),
                "type":        cond.get("type", "score_diff"),
                "team":        h["team_name"],
                "condition":   cond["name"],
                "color":       color,
                "score_str":   h["score_str"],
                "quarter_str": h["quarter_str"],
                "direction":   h["direction"],
                "game_id":     h["event_id"],
                "score_margin_at_fire": h.get("score_margin_at_fire"),
                "margin_diff": h.get("margin_diff"),
                "quarter_elapsed_pct": h.get("quarter_elapsed_pct"),
                "game_time_seconds": h.get("game_time_seconds"),
                "time_anchor": h.get("time_anchor"),
                "home_away_role": h.get("home_away_role"),
                "home_score_at_fire": h.get("home_score_at_fire"),
                "away_score_at_fire": h.get("away_score_at_fire"),
                "delivery_ok": False,
            }
            if persist_game_condition_hit(state, silent_entry):
                state_dirty = True

    # ── Stale state cleanup ────────────────────────────────────────────────────
    live_game_ids = set()
    for event in data.get("events", []):
        if (event.get("status") or {}).get("type", {}).get("state") != "in":
            continue
        live_game_ids.add(str(event["id"]))
        qnum    = int((event.get("status") or {}).get("period", 0))
        game_id = str(event["id"])
        for comp in event.get("competitions", []):
            for c in comp.get("competitors", []):
                tname = c["team"]["displayName"]
                for cond in conditions:
                    if cond.get("type", "score_diff") != "score_diff":
                        continue
                    for q in range(1, qnum):
                        old = "{}|{}|Q{}".format(tname, cond["name"], q)
                        if old in state:
                            del state[old]
                            state_dirty = True

    # Clean up fired_* keys (e.g. B2B once-per-game flags) for games no longer live
    # Key format: fired_{TeamName|ConditionName|GAME_gameId}[|RUN_...]
    for key in list(state.keys()):
        if key.startswith("fired_") and "GAME_" in key:
            gid = key.rsplit("GAME_", 1)[-1].split("|", 1)[0]
            if gid not in live_game_ids:
                del state[key]
                state_dirty = True
        elif key.startswith("history_hits_"):
            gid = key[len("history_hits_"):]
            if gid not in live_game_ids:
                # Preserve for auto-recovery if game has no history record (#230).
                # Delete after 48h TTL to prevent unbounded growth.
                if not state.get("history_recorded_{}".format(gid)):
                    _hits = state.get(key)
                    if isinstance(_hits, list) and _hits:
                        _oldest_ts = None
                        for _h in _hits:
                            _ts = str(_h.get("ts") or "") if isinstance(_h, dict) else ""
                            if _ts and (_oldest_ts is None or _ts < _oldest_ts):
                                _oldest_ts = _ts
                        if _oldest_ts:
                            try:
                                _age = now - datetime.fromisoformat(_oldest_ts.replace("Z", "+00:00")).timestamp()
                                if _age < 172800:  # 48 hours
                                    continue  # preserve
                            except (ValueError, TypeError):
                                pass
                del state[key]
                state_dirty = True

    # Clean up final_alerted_*, summary_alerted_*, predicted_winner_alerted_*, pw_calls_*
    # for games no longer in scoreboard.  history_recorded_* is excluded —
    # once a game is written to game_history.json it must never be re-recorded,
    # even if the game reappears on the scoreboard in a later tick.
    all_game_ids = {str(ev["id"]) for ev in data.get("events", [])}
    for key in list(state.keys()):
        if key.startswith("final_alerted_") or key.startswith("summary_alerted_"):
            gid = key.split("_", 2)[-1]
            if gid not in all_game_ids:
                del state[key]
                state_dirty = True
        elif key.startswith("predicted_winner_alerted_") or key.startswith("pw_calls_"):
            # These keys encode game_id after the last underscore-delimited prefix segment
            # predicted_winner_alerted_{gid}_{team} → split on "_alerted_" or "_calls_"
            if key.startswith("pw_calls_"):
                gid = key[len("pw_calls_"):]
            else:
                # predicted_winner_alerted_{gid}_{team_slug} — gid is first segment after prefix
                rest = key[len("predicted_winner_alerted_"):]
                gid = rest.split("_")[0]
            # Also clean up pw_calls for games already recorded to history,
            # even if still on the scoreboard (ESPN keeps Final games visible).
            if key.startswith("pw_calls_") and state.get("history_recorded_{}".format(gid)):
                del state[key]
                state_dirty = True
                continue
            if gid not in all_game_ids:
                # Preserve pw_calls for auto-recovery if game has no history record (#230)
                if key.startswith("pw_calls_") and not state.get("history_recorded_{}".format(gid)):
                    _calls = state.get(key)
                    if isinstance(_calls, list) and _calls:
                        _first_ts = str(_calls[0].get("timestamp") or _calls[0].get("ts") or "") if isinstance(_calls[0], dict) else ""
                        if _first_ts:
                            try:
                                _age = now - datetime.fromisoformat(_first_ts.replace("Z", "+00:00")).timestamp()
                                if _age < 172800:  # 48 hours
                                    continue  # preserve
                            except (ValueError, TypeError):
                                pass
                del state[key]
                state_dirty = True

    # ── Write game_ticker.json ─────────────────────────────────────────────────
    # Per-game active conditions this run — written every minute regardless of cooldown
    ticker_out = {}
    for h in all_hits:
        gid  = h["event_id"]
        cond = h["cond"]
        sk   = h["state_key"]
        hit_key = (h["event_id"], h["team_id"], cond["name"], sk)
        color = cond.get("color", "")
        if gid not in ticker_out:
            # Populate game name from live_stats_out
            teams = live_stats_out.get(gid, {}).get("teams", {})
            tnames = [t["name"] for t in teams.values()]
            ticker_out[gid] = {
                "game_name": " vs ".join(tnames) if tnames else gid,
                "quarter":   live_stats_out.get(gid, {}).get("quarter", ""),
                "hits": []
            }
        ticker_out[gid]["hits"].append({
            "ts":               datetime.now(timezone.utc).isoformat(),
            "team":             h["team_name"],
            "team_id":          h.get("team_id"),
            "condition":        cond["name"],
            "color":            color,
            "score_str":        h["score_str"],
            "quarter_score_str": h.get("quarter_score_str"),
            "quarter_str":      h["quarter_str"],
            "direction":        h["direction"],
            "run_key":          h.get("run_key"),
            "run_points":       h.get("run_points"),
            "run_scope":        h.get("run_scope"),
            "alerted":          hit_key in alerted_hit_keys,
            "score_margin_at_fire": h.get("score_margin_at_fire"),
            "quarter_elapsed_pct": h.get("quarter_elapsed_pct"),
            "home_away_role":      h.get("home_away_role"),
        })
    with open(TICKER_FILE, "w") as f:
        json.dump({"updated": datetime.now(timezone.utc).isoformat(), "games": ticker_out}, f, indent=2)

    # ── Write live_stats.json ──────────────────────────────────────────────────
    with open(LIVE_STATS_FILE, "w") as f:
        json.dump(
            {
                "updated": datetime.now(timezone.utc).isoformat(),
                "games": live_stats_out,
                "host": {
                    "disk": disk_status,
                },
            },
            f,
            indent=2,
        )

    # ── Write live_analysis.json ───────────────────────────────────────────────
    games_analysis = {}
    try:
        history = _cached_history  # Issue #147 — reuse history loaded at start of run
        for gid, game_entry in live_stats_out.items():
            # Collect unique condition names active this tick for this game
            active_conds = list({
                h["condition"]
                for h in ticker_out.get(gid, {}).get("hits", [])
                if h.get("condition")
            })
            if not active_conds:
                continue
            teams = game_entry.get("teams", {})
            active_hits = []
            for h in ticker_out.get(gid, {}).get("hits", []):
                cond_name = h.get("condition")
                team_name = h.get("team")
                team_id = str(h.get("team_id") or "").strip()
                if not cond_name or (not team_name and not team_id):
                    continue

                score = None
                team_data = teams.get(team_id) if team_id else None
                if team_data is None and team_name:
                    team_data = next(
                        (t for t in teams.values() if str(t.get("name", "")).strip() == str(team_name).strip()),
                        None,
                    )
                if team_data is not None:
                    try:
                        score = int(float(team_data.get("score") or 0))
                    except (TypeError, ValueError):
                        score = None

                active_hits.append({
                    "team": team_name,
                    "team_id": team_id,
                    "condition": cond_name,
                    "score": score,
                    "quarter_str": h.get("quarter_str"),
                    "quarter": h.get("quarter"),
                    "home_away_role": h.get("home_away_role"),
                    "score_margin_at_fire": h.get("score_margin_at_fire"),
                    "quarter_elapsed_pct": h.get("quarter_elapsed_pct"),
                })
            analysis = outcomes.live_analysis_for_game(
                gid,
                active_conds,
                history,
                active_hits=active_hits,
                decay_lambda=config.get("outcomes_decay_lambda", 0.0),
                _prebuilt_index=_cached_history_index,
                config=config,
            )
            tnames = [t["name"] for t in teams.values()]
            games_analysis[gid] = {
                "game_name":  " vs ".join(tnames) if tnames else gid,
                "quarter":    game_entry.get("quarter", ""),
                "analysis":   analysis,
            }
        outcomes.write_live_analysis(games_analysis)
    except Exception as e:
        _log("[WARN] Live analysis failed:", e)

    # ── Predicted winner threshold condition alerts (second pass) ──────────────
    # These conditions depend on live analysis results, so they run after
    # games_analysis is computed.  They follow the same alert/cooldown pattern
    # as base conditions.
    try:
        pw_threshold_conds = [c for c in conditions if c.get("type") == "predicted_winner_threshold"]
        # Skip old threshold alerts when filter-driven alerts are enabled (#418)
        _paf_active = (config.get("pw_alert_filters") or {}).get("enabled", False)
        if _paf_active:
            pw_threshold_conds = []
        if pw_threshold_conds and games_analysis and pw_threshold_ctx:
            for cond in pw_threshold_conds:
                for (gid, tid), ctx in pw_threshold_ctx.items():
                    team_ctx = ctx["team_ctx"]
                    # Pass PW alert adaptive threshold so evaluator can gate
                    # against the same threshold the PW call section uses (#238)
                    _pw_role = (ctx.get("team_odds") or {}).get("spread_role", "")
                    _pw_cfg = config.get("predicted_winner_alert") or {}
                    _pw_base = float(_pw_cfg.get("threshold_pct", 70))
                    if _pw_role.lower() == "favorite":
                        _pw_adaptive = float(_pw_cfg.get("threshold_pct_favorite", _pw_base))
                    elif _pw_role.lower() == "underdog":
                        _pw_adaptive = float(_pw_cfg.get("threshold_pct_underdog", _pw_base))
                    else:
                        _pw_adaptive = _pw_base
                    condition_context = {
                        "config": config,
                        "state": state,
                        "game_id": gid,
                        "quarter_num": ctx["quarter_num"],
                        "live_analysis": games_analysis,
                        "pw_adaptive_threshold": _pw_adaptive,
                    }
                    eval_result = condition_logic.evaluate_condition(cond, team_ctx, condition_context)
                    if not eval_result.get("matched"):
                        continue

                    state_key = get_state_key(ctx["team_name"], cond, ctx["quarter_num"], gid)
                    alert_once = cond.get("alert_once", "game")

                    # Cooldown / dedup — same logic as base conditions
                    if alert_once in ("game", "half", "quarter"):
                        if state.get("fired_{}".format(state_key)):
                            continue
                    elif now - state.get(state_key, 0) < cooldown:
                        continue

                    # Check if the prediction was suppressed (#221)
                    _pwt_suppressed = eval_result.get("_suppressed", False)
                    _pwt_suppress_reason = eval_result.get("_suppress_reason", "")
                    if _pwt_suppressed:
                        _log("[PW_THRESHOLD_SUPPRESSED] {} — {} {:.1f}% — {}".format(
                            games_analysis.get(gid, {}).get("game_name", gid),
                            eval_result.get("predicted_team", ctx["team_name"]),
                            eval_result.get("win_probability_pct", 0),
                            _pwt_suppress_reason))
                        continue

                    color = cond.get("color", "green")
                    emoji = COLOR_EMOJI.get(color, "🔔")
                    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                    game_name = games_analysis.get(gid, {}).get("game_name", gid)
                    win_pct = eval_result.get("win_probability_pct", 0)
                    consensus = eval_result.get("consensus", "")
                    is_blended = eval_result.get("is_blended", False)
                    basis = eval_result.get("basis", "")
                    sample = eval_result.get("sample", 0)
                    conds_count = eval_result.get("conditions_count", 0)

                    blended_str = " [blended]" if is_blended else ""
                    consensus_str = " [consensus: {}]".format(consensus.replace("_", " ")) if consensus else ""
                    basis_str = ""
                    if conds_count or sample:
                        if conds_count and sample:
                            basis_str = "\nBasis: {} active condition{} ({} historical game{})".format(
                                conds_count, "s" if conds_count != 1 else "",
                                sample, "s" if sample != 1 else "")
                        elif conds_count:
                            basis_str = "\nBasis: {} active condition{}".format(
                                conds_count, "s" if conds_count != 1 else "")
                        else:
                            basis_str = "\nBasis: {} historical game{}".format(
                                sample, "s" if sample != 1 else "")

                    _pw_alert_mode = _get_alert_mode(cond)
                    if _pw_alert_mode in ("slack", "history"):
                        game_clock = ctx.get("game_clock", "")
                        quarter_clock = ctx["quarter_str"]
                        if game_clock:
                            quarter_clock = "{} {}".format(ctx["quarter_str"], game_clock)
                        # Format score as "LAL 78, BOS 71"
                        pw_score = ctx["score_str"].replace(" - ", ", ")
                        _now_utc = datetime.now(timezone.utc)
                        ts_display = _format_alert_time(_now_utc, ctx.get("home_abbr", ""))

                        # Build stats line from available context
                        _pw_odds = ctx.get("team_odds") or {}
                        _pw_role = _pw_odds.get("spread_role", "")
                        _pw_ml = _pw_odds.get("moneyline", "")
                        _pw_sp = _pw_odds.get("spread", "")
                        _pwt_margin = ctx.get("score_margin_at_fire")
                        _pwt_home_s = ctx.get("home_score_at_fire")
                        _pwt_away_s = ctx.get("away_score_at_fire")
                        _pwt_stats = []
                        if _pwt_away_s is not None and _pwt_home_s is not None:
                            _pwt_stats.append("Score: {}-{}".format(_pwt_away_s, _pwt_home_s))
                        if _pwt_margin is not None:
                            _pwt_stats.append("Margin: {:+d}".format(int(_pwt_margin)))
                        if _pw_role:
                            _pwt_stats.append(_pw_role)
                        if _pw_ml and _pw_ml != "--":
                            _pwt_stats.append("Odds: {}".format(_pw_ml))
                        if _pw_sp and _pw_sp != "--":
                            _pwt_stats.append("Handicap: {}".format(_pw_sp))
                        if _pw_sp and _pw_sp != "--" and _pwt_margin is not None:
                            try:
                                _pwt_ll = float(_pw_sp) - int(_pwt_margin)
                                _pwt_stats.append("Live Spread: {:+.1f}".format(_pwt_ll))
                            except (TypeError, ValueError):
                                pass
                        # BK odds not fetched here — main PW section handles it (#253)
                        _pwt_stats_line = " · ".join(_pwt_stats) if _pwt_stats else ""

                        msg = (
                            "_{ts}_\n"
                            ":dart: *{league_label} Alert* \u2014 {team}\n"
                            "{quarter_clock} | {score}\n"
                            "Predicted Winner: *{pred_team}*\n"
                            "{pct:.1f}% win probability{blended}{consensus}{basis}\n"
                            "{stats}"
                        ).format(
                            ts=ts_display,
                            league_label=_LEAGUE_LABEL,
                            team=ctx["team_name"],
                            quarter_clock=quarter_clock,
                            score=pw_score,
                            pred_team=eval_result.get("predicted_team", ctx["team_name"]),
                            pct=win_pct,
                            blended=blended_str,
                            consensus=consensus_str,
                            basis=basis_str,
                            stats=_pwt_stats_line,
                        )
                        if str(gid) in _sl_game_ids:
                            msg = "[SL] " + msg
                        _log("[ALERT] [PW_THRESHOLD]", emoji, ctx["team_name"], "—",
                             cond["name"], "{:.1f}%".format(win_pct), "[mode:{}]".format(_pw_alert_mode))
                        if _pw_alert_mode == "slack":
                            sent_ok, sent_err = send_alert_result(config, msg)
                        else:
                            sent_ok, sent_err = True, None
                        if sent_ok:
                            if alert_once in ("game", "half", "quarter"):
                                state["fired_{}".format(state_key)] = True
                            else:
                                state[state_key] = now
                            state_dirty = True
                        entry = {
                            "ts":          datetime.now(timezone.utc).isoformat(),
                            "type":        "predicted_winner_threshold",
                            "team":        ctx["team_name"],
                            "condition":   cond["name"],
                            "color":       color,
                            "score_str":   ctx["score_str"],
                            "quarter_str": ctx["quarter_str"],
                            "game_clock":  ctx.get("game_clock", ""),
                            "direction":   eval_result.get("direction", ""),
                            "game_id":     gid,
                            "score_margin_at_fire": ctx.get("score_margin_at_fire"),
                            "quarter_elapsed_pct": ctx.get("quarter_elapsed_pct"),
                            "home_away_role": ctx.get("home_away"),
                            "home_score_at_fire": ctx.get("home_score_at_fire"),
                            "away_score_at_fire": ctx.get("away_score_at_fire"),
                            "predicted_team": eval_result.get("predicted_team", ""),
                            "win_probability_pct": win_pct,
                            "consensus":   consensus,
                            "is_blended":  is_blended,
                            "basis":       basis,
                            "conditions_count": conds_count,
                            "sample":      sample,
                            "threshold":   cond.get("threshold"),
                            "delivery_ok": bool(sent_ok),
                        }
                        if not sent_ok and sent_err:
                            entry["delivery_error"] = sent_err
                        update_alert_log(entry)
                        if persist_game_condition_hit(state, entry):
                            state_dirty = True
                    else:
                        # Silent tracking (alert: false)
                        if alert_once in ("game", "half", "quarter"):
                            state["fired_{}".format(state_key)] = True
                        else:
                            state[state_key] = now
                        state_dirty = True
                        silent_entry = {
                            "ts":          datetime.now(timezone.utc).isoformat(),
                            "type":        "predicted_winner_threshold",
                            "team":        ctx["team_name"],
                            "condition":   cond["name"],
                            "color":       color,
                            "score_str":   ctx["score_str"],
                            "quarter_str": ctx["quarter_str"],
                            "game_clock":  ctx.get("game_clock", ""),
                            "direction":   eval_result.get("direction", ""),
                            "game_id":     gid,
                            "score_margin_at_fire": ctx.get("score_margin_at_fire"),
                            "quarter_elapsed_pct": ctx.get("quarter_elapsed_pct"),
                            "home_away_role": ctx.get("home_away"),
                            "home_score_at_fire": ctx.get("home_score_at_fire"),
                            "away_score_at_fire": ctx.get("away_score_at_fire"),
                            "predicted_team": eval_result.get("predicted_team", ""),
                            "win_probability_pct": eval_result.get("win_probability_pct", 0),
                            "consensus":   consensus,
                            "is_blended":  is_blended,
                            "basis":       basis,
                            "conditions_count": conds_count,
                            "sample":      sample,
                            "threshold":   cond.get("threshold"),
                            "delivery_ok": False,
                        }
                        if persist_game_condition_hit(state, silent_entry):
                            state_dirty = True
    except Exception as e:
        _log("[WARN] Predicted winner threshold alerts failed:", e)

    # ── Predicted winner alerts ────────────────────────────────────────────────
    try:
        pw_cfg = config.get("predicted_winner_alert") or {}
        if pw_cfg.get("enabled") and games_analysis:
            pw_threshold = float(pw_cfg.get("threshold_pct", 70))
            pw_cooldown  = int(pw_cfg.get("cooldown_minutes", 5)) * 60
            pw_alert     = pw_cfg.get("alert", True)
            # Pre-compute scenario stats for Slack alert enrichment (#352)
            _pw_scenario_stats = {}
            try:
                _pw_scenario_stats = outcomes.compute_scenario_stats(history)
            except Exception:
                pass
            for gid, game_entry in games_analysis.items():
                analysis  = game_entry.get("analysis") or {}
                preds     = analysis.get("predictions") or []
                game_name = game_entry.get("game_name", gid)
                quarter   = game_entry.get("quarter", "")

                # Skip PW calls at quarter-end intermissions (#215)
                _pw_clock = live_stats_out.get(gid, {}).get("game_clock", "")
                if _pw_clock == "0:00" and quarter in ("Q1", "Q2", "Q3"):
                    _log("[PW_SKIP] {} — clock 0:00 at {} end, no plays happening".format(game_name, quarter))
                    continue

                # Mirror getPredictedWinnerForGame() logic: find Team wins rows
                # with a named predicted_team, score by winner_score_pct or pct,
                # pick the top scorer.  Also check suppressed predictions (#221).
                _pw_suppressed_preds = analysis.get("suppressed_predictions") or []
                team_win_rows = [
                    p for p in preds
                    if "team wins" in str(p.get("label", "")).lower()
                    and str(p.get("predicted_team") or "").strip()
                ]
                _pw_suppressed_tw_rows = [
                    p for p in _pw_suppressed_preds
                    if "team wins" in str(p.get("label", "")).lower()
                    and str(p.get("predicted_team") or "").strip()
                ]
                if not team_win_rows and not _pw_suppressed_tw_rows:
                    continue

                def _pw_score(row):
                    ws = row.get("winner_score_pct")
                    try:
                        v = float(ws)
                        if v == v:  # not NaN
                            return v
                    except (TypeError, ValueError):
                        pass
                    try:
                        return float(row.get("pct") or 0)
                    except (TypeError, ValueError):
                        return 0.0

                # Pick from active first; fall back to suppressed (#221)
                _pw_call_suppressed = False
                _pw_call_suppress_reason = ""
                if team_win_rows:
                    top_row = max(team_win_rows, key=_pw_score)
                else:
                    top_row = max(_pw_suppressed_tw_rows, key=_pw_score)
                    _pw_call_suppressed = True
                    _pw_call_suppress_reason = str(top_row.get("_suppress_reason") or "filtered")
                top_score = _pw_score(top_row)
                pred_team = str(top_row.get("predicted_team") or "").strip()
                pred_team_id = str(top_row.get("predicted_team_id") or pred_team).strip()
                consensus = str(top_row.get("consensus") or "").strip()
                is_blended = top_row.get("winner_score_pct") is not None
                basis = str(top_row.get("basis") or "").strip()

                if not pred_team:
                    continue

                # Adaptive threshold by spread role (#195.2)
                _pw_effective_threshold = pw_threshold
                _pw_role_for_threshold = ""
                for (_gk, _tk), _pctx in pw_threshold_ctx.items():
                    if _gk == gid and _pctx.get("team_name") == pred_team:
                        _pw_role_for_threshold = (_pctx.get("team_odds") or {}).get("spread_role", "")
                        break
                if _pw_role_for_threshold.lower() == "favorite":
                    _pw_effective_threshold = float(pw_cfg.get("threshold_pct_favorite", pw_threshold))
                elif _pw_role_for_threshold.lower() == "underdog":
                    _pw_effective_threshold = float(pw_cfg.get("threshold_pct_underdog", pw_threshold))

                if top_score < _pw_effective_threshold:
                    continue

                # Absolute margin hard stop (#205): suppress PW call if predicted
                # team is trailing by a large margin in the live game.  This catches
                # garbage-time false positives that slip through the per-condition
                # margin gate (e.g. PHI predicted at 82% while trailing by 32 pts).
                _pw_hard_stop_margin = int(pw_cfg.get("pw_hard_stop_margin", 20))
                if _pw_hard_stop_margin > 0:
                    try:
                        _pw_hs_teams = live_stats_out.get(gid, {}).get("teams") or {}
                        _pw_hs_pred_score = None
                        _pw_hs_opp_score = None
                        for _pw_hs_tid, _pw_hs_td in _pw_hs_teams.items():
                            _pw_hs_s = int(float(_pw_hs_td.get("score") or 0))
                            if str(_pw_hs_tid) == pred_team_id:
                                _pw_hs_pred_score = _pw_hs_s
                            else:
                                _pw_hs_opp_score = _pw_hs_s
                        if _pw_hs_pred_score is not None and _pw_hs_opp_score is not None:
                            _pw_hs_live_margin = _pw_hs_pred_score - _pw_hs_opp_score
                            _pw_hs_q = quarter
                            if _pw_hs_live_margin <= -_pw_hard_stop_margin and any(
                                q in str(_pw_hs_q) for q in ("Q3", "Q4", "OT")
                            ):
                                _log("[PW_HARD_STOP] {} trailing by {} in {} — suppressing PW call".format(
                                    pred_team, abs(_pw_hs_live_margin), _pw_hs_q))
                                continue
                    except Exception:
                        pass

                # Per-game, per-team cooldown key — allows re-alert when team flips
                pw_key = "predicted_winner_alerted_{}_{}".format(gid, pred_team_id.lower().replace(" ", "_"))
                if now - state.get(pw_key, 0) < pw_cooldown:
                    continue

                _now_utc = datetime.now(timezone.utc)
                # Resolve home team abbreviation for venue timezone display
                _pw_home_abbr = ""
                _pw_ls_teams = live_stats_out.get(gid, {}).get("teams") or {}
                for _pw_lt_td in _pw_ls_teams.values():
                    if str(_pw_lt_td.get("homeAway") or "").lower() == "home":
                        _pw_home_abbr = str(_pw_lt_td.get("abbr") or "")
                        break
                ts = _format_alert_time(_now_utc, _pw_home_abbr)
                blended_tag  = " [blended]" if is_blended else ""
                combo_tag    = " [combo]" if basis == "combo" else ""
                consensus_tag = " [consensus: {}]".format(consensus.replace("_", " ")) if consensus else ""

                if _pw_call_suppressed:
                    _log("[PW_SUPPRESSED] {} — {} {:.0f}% ({}) — {}".format(
                        game_name, pred_team, top_score, basis, _pw_call_suppress_reason))
                else:
                    _log("[PREDICTED WINNER] {} — {} {:.0f}%{}{}{}".format(
                        game_name, pred_team, top_score, blended_tag, combo_tag, consensus_tag))

                # ── Compute all PW call stats before message/record ─────────────

                # Derive score margin and scores at call time (#158)
                _pw_margin = None
                _pw_home_score = None
                _pw_away_score = None
                try:
                    _pw_teams = live_stats_out.get(gid, {}).get("teams") or {}
                    _pw_pred_score = None
                    _pw_opp_score = None
                    for _pw_tid, _pw_tdata in _pw_teams.items():
                        _pw_s = int(float(_pw_tdata.get("score") or 0))
                        _pw_ha = str(_pw_tdata.get("homeAway") or "").lower()
                        if _pw_ha == "home":
                            _pw_home_score = _pw_s
                        elif _pw_ha == "away":
                            _pw_away_score = _pw_s
                        if str(_pw_tid) == pred_team_id:
                            _pw_pred_score = _pw_s
                        else:
                            _pw_opp_score = _pw_s
                    if _pw_pred_score is not None and _pw_opp_score is not None:
                        _pw_margin = _pw_pred_score - _pw_opp_score
                except Exception as _pw_exc:
                    _log("[WARN] PW score derivation failed for game {}: {} (teams_keys={}, pred_team_id={})".format(
                        gid, _pw_exc, list((live_stats_out.get(gid, {}).get("teams") or {}).keys()), repr(pred_team_id)))

                # Capture active conditions at call time (#163), with edge (#217)
                _pw_active_conds = []
                try:
                    _pw_edge_store = outcomes.ConditionEdgeStore.load_or_rebuild()
                    for _h in ticker_out.get(gid, {}).get("hits", []):
                        _pw_ac = {
                            "name": _h.get("condition", ""),
                            "team": _h.get("team", ""),
                            "team_id": _h.get("team_id", ""),
                        }
                        _pw_ac["condition_edge"] = round(_pw_edge_store.edge(_h.get("condition", "")), 1)
                        _pw_active_conds.append(_pw_ac)
                except Exception:
                    pass

                # Capture pregame odds at call time (#195.7)
                _pw_live_ml = None
                _pw_live_spread = None
                _pw_spread_role = _pw_role_for_threshold
                _pw_spread_role_source = "espn" if _pw_spread_role else ""
                try:
                    for (_gk, _tk), _pctx in pw_threshold_ctx.items():
                        if _gk == gid and _pctx.get("team_name") == pred_team:
                            _pw_odds = _pctx.get("team_odds") or {}
                            _pw_live_ml = _pw_odds.get("moneyline")
                            _pw_live_spread = _pw_odds.get("spread")
                            if not _pw_spread_role:
                                _pw_spread_role = _pw_odds.get("spread_role", "")
                            break
                except Exception:
                    pass

                # Extract pregame moneyline/spread from snapshot (#380)
                _pw_pregame_ml = None
                _pw_pregame_spread = None
                try:
                    _pw_snap = (state.get("pregame_snapshots") or {}).get(str(gid))
                    if _pw_snap:
                        _pw_hmm = _pw_snap.get("home_matchup_meta") or {}
                        _pw_amm = _pw_snap.get("away_matchup_meta") or {}
                        if str(pred_team_id) == _pw_hmm.get("team_id") or pred_team == _pw_hmm.get("name"):
                            _pw_pregame_ml = _pw_hmm.get("moneyline")
                            _pw_pregame_spread = _pw_hmm.get("spread")
                        elif str(pred_team_id) == _pw_amm.get("team_id") or pred_team == _pw_amm.get("name"):
                            _pw_pregame_ml = _pw_amm.get("moneyline")
                            _pw_pregame_spread = _pw_amm.get("spread")
                        # Normalize "--" to None
                        if _pw_pregame_ml == "--":
                            _pw_pregame_ml = None
                        if _pw_pregame_spread == "--":
                            _pw_pregame_spread = None
                except Exception:
                    pass

                # Fetch real-time bookmaker odds at PW fire time (#213)
                # Also fetch for suppressed calls (#374)
                _pw_bk_spread = None
                _pw_bk_spread_price = None
                _pw_bk_ml = None
                _pw_bk_name = None
                _pw_bk_ts = None
                _pw_bk_ml_source = ""
                _pw_bk_spread_source = ""
                # Quarter/half odds variables (#374)
                _pw_bk_q_ml = None
                _pw_bk_q_spread = None
                _pw_bk_q_spread_price = None
                _pw_bk_q_ml_source = ""
                _pw_bk_h1_ml = None
                _pw_bk_h1_spread = None
                _pw_bk_h1_spread_price = None
                _pw_bk_h1_ml_source = ""
                if config.get("odds_api_enabled") and config.get("odds_api_key"):
                    try:
                        # Derive home/away names from pw_threshold_ctx
                        _pw_home_name = None
                        _pw_away_name = None
                        for (_gk, _tk), _pctx in pw_threshold_ctx.items():
                            if _gk == gid:
                                if _pctx.get("home_away") == "home":
                                    _pw_home_name = _pctx.get("team_name")
                                elif _pctx.get("home_away") == "away":
                                    _pw_away_name = _pctx.get("team_name")
                        if _pw_home_name and _pw_away_name:
                            _sl_sport = "basketball_nba_summer_league" if str(gid) in _sl_game_ids else None
                            _pw_bk_odds = odds_api.fetch_live_game_odds(
                                config["odds_api_key"],
                                _pw_home_name, _pw_away_name,
                                quarter=quarter,
                                logger=_log, context="pw_fire",
                                sport_key=_sl_sport,
                            )
                            if _pw_bk_odds:
                                _pw_bk_team = odds_api.extract_team_odds(
                                    _pw_bk_odds, pred_team,
                                    _pw_home_name, _pw_away_name,
                                )
                                if _pw_bk_team:
                                    _pw_bk_spread = _pw_bk_team.get("spread")
                                    _pw_bk_spread_price = _pw_bk_team.get("spread_price")
                                    _pw_bk_ml = _pw_bk_team.get("moneyline")
                                    _pw_bk_name = _pw_bk_team.get("bookmaker")
                                    _pw_bk_ts = _pw_bk_team.get("fetched_ts")
                                _pw_bk_ml_source = _pw_bk_odds.get("ml_source", "")
                                _pw_bk_spread_source = _pw_bk_odds.get("spread_source", "")
                                # Quarter/half odds extraction (#374)
                                _pw_qh = odds_api.extract_quarter_half_odds(
                                    _pw_bk_odds, pred_team,
                                    _pw_home_name, _pw_away_name, quarter,
                                )
                                if _pw_qh:
                                    _pw_bk_q_ml = _pw_qh.get("q_ml")
                                    _pw_bk_q_spread = _pw_qh.get("q_spread")
                                    _pw_bk_q_spread_price = _pw_qh.get("q_spread_price")
                                    _pw_bk_q_ml_source = _pw_qh.get("q_ml_source") or ""
                                    _pw_bk_h1_ml = _pw_qh.get("h1_ml")
                                    _pw_bk_h1_spread = _pw_qh.get("h1_spread")
                                    _pw_bk_h1_spread_price = _pw_qh.get("h1_spread_price")
                                    _pw_bk_h1_ml_source = _pw_qh.get("h1_ml_source") or ""
                    except Exception as _bk_exc:
                        _log("[ODDS_API] PW odds fetch failed: {}".format(_bk_exc))
                    odds_api.record_game_fetch(gid, _pw_bk_spread is not None)

                # Fallback: use BK odds as pregame when snapshot had none (#380)
                if _pw_pregame_ml is None and _pw_bk_ml is not None:
                    _pw_pregame_ml = _pw_bk_ml
                if _pw_pregame_spread is None and _pw_bk_spread is not None:
                    _pw_pregame_spread = _pw_bk_spread

                # Derive spread_role from pregame spread when ESPN pickcenter unavailable (#391)
                if not _pw_spread_role and _pw_pregame_spread is not None:
                    try:
                        _sp_val = float(_pw_pregame_spread)
                        _pw_spread_role = "favorite" if _sp_val < 0 else "underdog" if _sp_val > 0 else ""
                        if _pw_spread_role:
                            _pw_spread_role_source = "spread"
                    except (TypeError, ValueError):
                        pass

                # Compute polarity/edge from active conditions when top_row
                # doesn't carry them (ML-only PW calls).  Must scope to
                # predicted team's conditions only, matching outcomes.py.
                _pw_pol_pos = top_row.get("polarity_pos")
                _pw_pol_neg = top_row.get("polarity_neg")
                _pw_pol_net = top_row.get("polarity_net")
                _pw_avg_edge = top_row.get("avg_edge")
                _pw_pred_conds = [ac for ac in _pw_active_conds
                                  if str(ac.get("team_id") or "") == str(pred_team_id)
                                  or (not ac.get("team_id") and ac.get("team") == pred_team)]
                if _pw_pol_net is None and _pw_pred_conds:
                    _pw_pol_neg = sum(1 for ac in _pw_pred_conds
                                     if outcomes.classify_polarity(ac.get("name", "")) == "neg")
                    _pw_pol_pos = sum(1 for ac in _pw_pred_conds
                                     if outcomes.classify_polarity(ac.get("name", "")) == "pos")
                    _pw_pol_net = _pw_pol_pos - _pw_pol_neg
                if _pw_avg_edge is None and _pw_pred_conds:
                    try:
                        _pw_avg_edge = round(sum(
                            ac.get("condition_edge", 0) for ac in _pw_pred_conds
                        ) / len(_pw_pred_conds), 2)
                    except Exception:
                        pass

                # Compute live spread (synthetic: pregame spread - margin)
                _pw_live_line = None
                if _pw_live_spread and _pw_live_spread != "--" and _pw_margin is not None:
                    try:
                        _pw_live_line = float(_pw_live_spread) - _pw_margin
                    except (TypeError, ValueError):
                        pass

                # ── Build stats line for Slack message (#250) ─────────────────
                _pw_stats_parts = []
                if _pw_away_score is not None and _pw_home_score is not None:
                    _pw_stats_parts.append("Score: {}-{}".format(_pw_away_score, _pw_home_score))
                if _pw_margin is not None:
                    _pw_stats_parts.append("Margin: {:+d}".format(_pw_margin))
                if _pw_spread_role:
                    _pw_stats_parts.append(_pw_spread_role)
                if _pw_pregame_ml:
                    _pw_stats_parts.append("Odds: {}".format(_pw_pregame_ml))
                if _pw_pregame_spread:
                    _pw_stats_parts.append("Spread: {}".format(_pw_pregame_spread))
                if _pw_live_ml and _pw_live_ml != "--" and str(_pw_live_ml) != str(_pw_pregame_ml):
                    _pw_stats_parts.append("Live ML: {}".format(_pw_live_ml))
                if _pw_live_spread and _pw_live_spread != "--":
                    _pw_stats_parts.append("Handicap: {}".format(_pw_live_spread))
                if _pw_live_line is not None:
                    _pw_stats_parts.append("Live Spread: {:+.1f}".format(_pw_live_line))
                if _pw_bk_ml:
                    _pw_stats_parts.append("BK Odds: {}".format(_pw_bk_ml))
                if _pw_bk_spread is not None:
                    _pw_stats_parts.append("BK Spread: {}".format(_pw_bk_spread))
                if _pw_pol_net is not None:
                    _pw_stats_parts.append("Pol: {:+d} ({}+ {}\u2212)".format(
                        _pw_pol_net, _pw_pol_pos or 0, _pw_pol_neg or 0))
                if _pw_avg_edge is not None:
                    _pw_stats_parts.append("Edge: {:+.1f}".format(_pw_avg_edge))
                # Scenario stats (#352)
                _pw_sc_call = {"spread_role": _pw_spread_role or "", "score_margin_at_fire": _pw_margin,
                               "avg_edge": _pw_avg_edge, "quarter": quarter}
                _pw_sc_key = outcomes.scenario_key_from_call(_pw_sc_call)
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
                    _pw_stats_parts.append(
                        "Scenario: {} W:{}/{} ML:{} Cov:{}/{} Spr:{}".format(
                            _sc_label, _pw_sc["correct"], _pw_sc["total_calls"],
                            _sc_ml_str, _sc_cov, _sc_spr_n, _sc_spr_str))
                _pw_stats_line = " · ".join(_pw_stats_parts) if _pw_stats_parts else ""

                # ── Send Slack alert ──────────────────────────────────────────
                # Skip when filter-driven alerts are active (#435) — filters handle delivery
                sent_ok = True
                _pw_direct_alert_suppressed = (config.get("pw_alert_filters") or {}).get("enabled", False)
                if pw_alert and not _pw_call_suppressed and not _pw_direct_alert_suppressed:
                    msg = (
                        "_{ts}_\n"
                        ":basketball: :dart: *Predicted Winner* \u2014 {pred}\n"
                        "{pct}{blended}{consensus}\n"
                        "Game: {game} \u00b7 {quarter}\n"
                        "{stats}"
                    ).format(
                        ts=ts,
                        pred=pred_team,
                        pct="{:.0f}% win probability".format(top_score),
                        blended=" \u00b7 {}".format(blended_tag.strip(" []")) if is_blended else "",
                        consensus=" \u00b7 consensus: {}".format(consensus.replace("_", " ")) if consensus else "",
                        game=game_name, quarter=quarter,
                        stats=_pw_stats_line,
                    )
                    if str(gid) in _sl_game_ids:
                        msg = "[SL] " + msg
                    sent_ok, sent_err = send_alert_result(config, msg)
                    if not sent_ok:
                        _log("[WARN] Predicted winner alert delivery failed:", sent_err)

                # Update cooldown key so we don't spam even if Slack is down.
                # Suppressed calls use a short 2-minute cooldown (dedup only) so
                # they don't block unsuppressed calls that form when conditions
                # improve shortly after (e.g. polarity gate lifts).  (#235)
                if _pw_call_suppressed:
                    state[pw_key] = now - pw_cooldown + 120  # expires in ~2 min
                else:
                    state[pw_key] = now
                state_dirty = True

                # Persist call snapshot for later accuracy scoring at game-end.
                # Stored under pw_calls_{game_id} — a list of all calls this game.
                # Skip if game already recorded — ESPN API can lag state transitions.
                if state.get("history_recorded_{}".format(gid)):
                    _log("[PW_SKIP] {} — game already recorded, ignoring stale ESPN data".format(game_name))
                    continue
                pw_calls_key = "pw_calls_{}".format(gid)
                pw_calls = state.get(pw_calls_key)
                if not isinstance(pw_calls, list):
                    pw_calls = []

                _pw_call_record = {
                    "ts":                datetime.now(timezone.utc).isoformat(),
                    "predicted_team":    pred_team,
                    "predicted_team_id": pred_team_id,
                    "pct":               round(top_score, 1),
                    "consensus":         consensus,
                    "blended":           is_blended,
                    "basis":             basis,
                    "quarter":           quarter,
                    "game_clock":        live_stats_out.get(gid, {}).get("game_clock", ""),
                    "score_margin_at_fire": _pw_margin,
                    "home_score_at_fire": _pw_home_score,
                    "away_score_at_fire": _pw_away_score,
                    "active_conditions": _pw_active_conds,
                    "polarity_pos":      _pw_pol_pos or 0,
                    "polarity_neg":      _pw_pol_neg or 0,
                    "polarity_net":      _pw_pol_net or 0,
                    "avg_edge":          _pw_avg_edge,
                    "spread_role":       _pw_spread_role,
                    "spread_role_source": _pw_spread_role_source,
                    "moneyline":         _pw_pregame_ml,
                    "spread":            _pw_pregame_spread,
                    "live_moneyline":    _pw_live_ml,
                    "live_spread":       _pw_live_spread,
                    "bk_spread":         _pw_bk_spread,
                    "bk_spread_price":   _pw_bk_spread_price,
                    "bk_moneyline":      _pw_bk_ml,
                    "bk_name":           _pw_bk_name,
                    "bk_ml_source":      _pw_bk_ml_source,
                    "bk_spread_source":  _pw_bk_spread_source,
                    "bk_ts":             _pw_bk_ts,
                    # Quarter/half odds (#374)
                    "bk_q_ml":            _pw_bk_q_ml,
                    "bk_q_spread":        _pw_bk_q_spread,
                    "bk_q_spread_price":  _pw_bk_q_spread_price,
                    "bk_q_ml_source":     _pw_bk_q_ml_source,
                    "bk_h1_ml":           _pw_bk_h1_ml,
                    "bk_h1_spread":       _pw_bk_h1_spread,
                    "bk_h1_spread_price": _pw_bk_h1_spread_price,
                    "bk_h1_ml_source":    _pw_bk_h1_ml_source,
                    # Quarter/half outcomes — resolved at game end (#374)
                    "q_correct":          None,
                    "q_covered":          None,
                    "h1_correct":         None,
                    "h1_covered":         None,
                    # Synthetic odds (#376)
                    "synth_moneyline":   None,
                    "synth_spread":      None,
                    "synth_h1_ml":       None,
                    "correct":           None,  # resolved at game-end
                    "source":            "live",
                    "pw_version":        _pw_version,
                    "model_schema_version": _get_model_schema_version(),  # #412 Phase 4.12
                }
                # Compute synthetic odds for this PW call (#376)
                try:
                    import synthetic_odds
                    _synth_pregame_for_pw = None
                    _synth_fav_for_pw = parse_odds_favorite(_game_odds_detail, competitors)
                    if _game_odds_detail and _synth_fav_for_pw:
                        _sd_p = _game_odds_detail.strip().rsplit(" ", 1)
                        if len(_sd_p) == 2:
                            try:
                                _sd_v = float(_sd_p[1])
                                _pred_id = _pw_call_record.get("predicted_team_id")
                                if str(_pred_id) == str(_synth_fav_for_pw):
                                    _synth_pregame_for_pw = _sd_v
                                else:
                                    _synth_pregame_for_pw = -_sd_v
                            except (TypeError, ValueError):
                                pass
                    _synth_pw = synthetic_odds.compute_all(
                        _pw_call_record.get("score_margin_at_fire"),
                        quarter,
                        _synth_pregame_for_pw,
                        league_config.LEAGUE
                    )
                    _pw_call_record["synth_moneyline"] = _synth_pw.get("moneyline")
                    _pw_call_record["synth_spread"] = _synth_pw.get("spread")
                    _pw_call_record["synth_h1_ml"] = _synth_pw.get("h1_ml")
                except Exception:
                    pass
                if _pw_call_suppressed:
                    _pw_call_record["_suppressed"] = True
                    _pw_call_record["_suppress_reason"] = _pw_call_suppress_reason
                else:
                    _pw_call_record["_suppressed"] = False
                pw_calls.append(_pw_call_record)
                state[pw_calls_key] = pw_calls
                save_state(state)  # flush immediately so dashboard PW tab sees new call (#399)

                # Forward-test bet log (#412 Phase 5.2)
                if not _pw_call_suppressed:
                    try:
                        _bet_entry = {
                            "ts": _pw_call_record["ts"],
                            "game_id": gid,
                            "game_minute": _pw_call_record.get("quarter", ""),
                            "rule_id": "pw-default-v1",
                            "predicted_team": pred_team,
                            "model_prob": round(top_score / 100.0, 4),
                            "price_at_signal": _pw_bk_ml or _pw_pregame_ml,
                            "price_source": "live" if _pw_bk_ml_source else "pregame",
                            "ev": None,  # filled at serve-time
                            "spread_role": _pw_spread_role,
                            "margin_at_fire": _pw_margin,
                        }
                        _bets_path = os.path.join(SCRIPT_DIR, league_config.state_path("bets.jsonl"))
                        with open(_bets_path, "a") as _bf:
                            _bf.write(json.dumps(_bet_entry) + "\n")
                    except Exception:
                        pass

                # ── Filter-driven PW alert (#418, #435) ──
                _pw_alert_delivered = False
                _pw_af = config.get("pw_alert_filters")
                if not _pw_call_suppressed and _pw_af and _pw_af.get("enabled"):
                    # Stamp scenario key on call for ROI filter lookup (#435)
                    _pw_call_record["_scenario_key"] = _pw_sc_key or ""
                    if _pw_call_passes_alert_filters(_pw_call_record, _pw_af, _pw_scenario_stats):
                        try:
                            _af_ts = _format_alert_time(datetime.now(timezone.utc), _pw_home_abbr)
                            _af_msg = (
                                "_{ts}_\n"
                                ":basketball: :dart: *Predicted Winner* \u2014 {pred}\n"
                                "{pct} win probability{blended}{consensus}\n"
                                "Game: {game} \u00b7 {quarter}\n"
                                "{stats}"
                            ).format(
                                ts=_af_ts,
                                pred=pred_team,
                                pct="{:.0f}%".format(top_score),
                                blended=" \u00b7 {}".format(blended_tag.strip(" []")) if is_blended else "",
                                consensus=" \u00b7 consensus: {}".format(consensus.replace("_", " ")) if consensus else "",
                                game=game_name, quarter=quarter,
                                stats=_pw_stats_line,
                            )
                            if str(gid) in _sl_game_ids:
                                _af_msg = "[SL] " + _af_msg
                            _af_ok, _af_err = send_alert_result(config, _af_msg)
                            if _af_ok:
                                _pw_alert_delivered = True
                            _log("[PW_FILTER_ALERT] {} — {} {:.1f}% [{}] sent={}".format(
                                game_name, pred_team, top_score, consensus, _af_ok))
                        except Exception as _af_ex:
                            _log("[PW_FILTER_ALERT] Error: {}".format(_af_ex))
                    else:
                        _log("[PW_ALERT_FILTERED] {} — {} {:.1f}% — did not pass alert filters".format(
                            game_name, pred_team, top_score))

                # ── PW Underdog Alert condition (#282) ──
                # Skip when filter-driven alerts are active (#418)
                if not _pw_call_suppressed and _pw_spread_role == "underdog" and not (config.get("pw_alert_filters") or {}).get("enabled", False):
                    _pua_conds = [c for c in config.get("conditions", [])
                                  if c.get("type") == "pw_underdog_alert"]
                    for _pua in _pua_conds:
                        _pua_mode = _get_alert_mode(_pua)
                        if _pua_mode == "none":
                            continue
                        _pua_quarters = _pua.get("quarters", ["Q3", "Q4"])
                        if quarter not in _pua_quarters:
                            continue
                        _pua_min_conf = float(_pua.get("min_confidence", 60))
                        if top_score < _pua_min_conf:
                            continue
                        _pua_max = int(_pua.get("max_alerts_per_quarter_per_team", 1))
                        _pua_key = "pua_{}_{}_{}_{}_{}".format(_pua["name"], gid, pred_team_id, quarter, "count")
                        _pua_count = int(state.get(_pua_key, 0) or 0)
                        if _pua_count >= _pua_max:
                            continue
                        state[_pua_key] = _pua_count + 1
                        state_dirty = True

                        _pua_gname = games_analysis.get(gid, {}).get("game_name", gid)
                        _pua_msg_parts = [
                            "_{}_".format(_format_alert_time(datetime.now(timezone.utc), _pw_home_abbr)),
                            ":dog: *PW Underdog Alert* — *{}*".format(pred_team),
                            "*{:.1f}%* confidence | {} | {}".format(top_score, consensus, quarter),
                            "{} | Score: {}-{}".format(_pua_gname, _pw_away_score, _pw_home_score),
                            "Margin: {:+d} | Edge: {:.1f} | Role: underdog".format(
                                _pw_margin or 0, _pw_avg_edge or 0),
                        ]
                        if _pw_bk_ml:
                            _pua_msg_parts.append("Odds: {} | Spread: {}".format(_pw_bk_ml, _pw_bk_spread or "?"))
                        _pua_msg = "\n".join(_pua_msg_parts)
                        if str(gid) in _sl_game_ids:
                            _pua_msg = "[SL] " + _pua_msg

                        _log("[ALERT] [PW_UNDERDOG]", pred_team, quarter, "{:.1f}%".format(top_score), "[mode:{}]".format(_pua_mode))
                        if _pua_mode == "slack":
                            _pua_ok, _pua_err = send_alert_result(config, _pua_msg)
                        else:
                            _pua_ok, _pua_err = True, None

                        _pua_entry = {
                            "ts": datetime.now(timezone.utc).isoformat(),
                            "type": "pw_underdog_alert",
                            "team": pred_team,
                            "condition": _pua["name"],
                            "color": _pua.get("color", "orange"),
                            "score_str": "{:.1f}%".format(top_score),
                            "quarter_str": quarter,
                            "direction": "underdog",
                            "game_id": gid,
                            "pct": round(top_score, 1),
                            "consensus": consensus,
                            "spread_role": "underdog",
                            "score_margin_at_fire": _pw_margin,
                            "home_score_at_fire": _pw_home_score,
                            "away_score_at_fire": _pw_away_score,
                            "avg_edge": _pw_avg_edge,
                            "delivery_ok": bool(_pua_ok),
                        }
                        if not _pua_ok and _pua_err:
                            _pua_entry["delivery_error"] = _pua_err
                        update_alert_log(_pua_entry)

                    # Write PW call to alert history (#435)
                    # When pw_alert_filters is enabled, only log if the call passed filters.
                    # When disabled, log all non-suppressed calls (existing behavior).
                    _pw_should_log = _pw_alert_delivered if (config.get("pw_alert_filters") or {}).get("enabled") else True
                    if not _pw_call_suppressed and _pw_should_log:
                        entry = {
                            "ts":          datetime.now(timezone.utc).isoformat(),
                            "type":        "predicted_winner",
                            "team":        pred_team,
                            "predicted_team": pred_team,
                            "predicted_team_id": pred_team_id,
                            "condition":   "Predicted Winner",
                            "color":       "green",
                            "score_str":   "{:.0f}%".format(top_score),
                            "quarter_str": quarter,
                            "game_clock":  live_stats_out.get(gid, {}).get("game_clock", ""),
                            "direction":   _pw_stats_line,
                            "game_id":     gid,
                            "game_name":   game_name,
                            "pct":         round(top_score, 1),
                            "consensus":   consensus,
                            "blended":     is_blended,
                            "basis":       basis,
                            "score_margin_at_fire": _pw_margin,
                            "home_score_at_fire": _pw_home_score,
                            "away_score_at_fire": _pw_away_score,
                            "spread_role": _pw_spread_role,
                            "live_moneyline": _pw_live_ml,
                            "live_spread": _pw_live_spread,
                            "live_line":   _pw_live_line,
                            "polarity_pos": _pw_pol_pos or 0,
                            "polarity_neg": _pw_pol_neg or 0,
                            "polarity_net": _pw_pol_net or 0,
                            "avg_edge":    _pw_avg_edge,
                            "bk_moneyline": _pw_bk_ml,
                            "bk_spread":   _pw_bk_spread,
                            "bk_spread_price": _pw_bk_spread_price,
                            "bk_name":     _pw_bk_name,
                            "delivery_ok": True,
                        }
                        update_alert_log(entry)

    except Exception as e:
        _log("[WARN] Predicted winner alert check failed:", e)

    # ── Daily stats accumulation ───────────────────────────────────────────────
    _today_key = "__daily_{}".format(datetime.now(timezone.utc).date().isoformat())
    _prev_day_key = state.get("__daily_current_key")

    # Date rolled over — emit yesterday's summary before starting fresh
    if _prev_day_key and _prev_day_key != _today_key:
        _prev = state.get(_prev_day_key, {})
        _alerts_total = int(_prev.get("alerts_sent", 0))
        _alerts_ok    = int(_prev.get("alerts_delivered", 0))
        _games        = int(_prev.get("games_seen", 0))
        _games_final  = int(_prev.get("games_finalised", 0))
        _runs         = int(_prev.get("runs", 0))
        _wall_sum     = float(_prev.get("wall_ms_sum", 0))
        _conds        = _prev.get("conditions_fired", {})
        _avg_wall     = (_wall_sum / _runs) if _runs else 0
        _top_conds    = sorted(_conds.items(), key=lambda x: -x[1])[:5]
        _top_str      = ", ".join("{} x{}".format(k, v) for k, v in _top_conds) if _top_conds else "none"
        _log(
            "[DAILY SUMMARY] date={} runs={} games_live={} games_final={} "
            "alerts={} delivered={} avg_wall={:.0f}ms top_conditions=[{}]".format(
                _prev_day_key.replace("__daily_", ""),
                _runs, _games, _games_final,
                _alerts_total, _alerts_ok,
                _avg_wall, _top_str,
            )
        )
        # Odds API window close summary (#266) — log credits consumed during previous day
        _oa_prev = _prev.get("odds_api_calls", 0)
        _oa_prev_credits = _prev.get("odds_api_credits_used", 0)
        _oa_rem = odds_api.get_credit_status().get("credits_remaining")
        odds_api.log_daily_summary("daily_summary_close", _oa_rem,
                                   calls_in_window=_oa_prev,
                                   credits_in_window=_oa_prev_credits)
        # Odds API window open summary (#266) — log credits at start of new day
        odds_api.log_daily_summary("daily_summary_open", _oa_rem)
        # Prune old daily keys (keep last 3 days)
        for _k in list(state.keys()):
            if _k.startswith("__daily_") and _k not in (_today_key, _prev_day_key):
                del state[_k]
        state_dirty = True

    # Accumulate this run's stats into today's bucket
    _ds = state.setdefault(_today_key, {
        "runs": 0,
        "alerts_sent": 0,
        "alerts_delivered": 0,
        "games_seen": 0,
        "games_finalised": 0,
        "wall_ms_sum": 0.0,
        "conditions_fired": {},
    })
    if not isinstance(_ds, dict):
        _ds = {}
        state[_today_key] = _ds

    _run_games_seen     = len(set(
        str(ev.get("id") or "")
        for ev in data.get("events", [])
        if (ev.get("status") or {}).get("type", {}).get("state") == "in"
    )) if isinstance(data, dict) else 0
    _run_games_final    = len(just_finished) if isinstance(just_finished, set) else 0
    _run_alerts_sent    = sum(
        1 for h in all_hits
        if (h["event_id"], h["team_id"], h["cond"]["name"],
            h["state_key"]) in alerted_hit_keys
    )
    _run_alerts_ok      = _run_alerts_sent  # delivery check already gated alerted_hit_keys

    _ds["runs"]             = int(_ds.get("runs", 0)) + 1
    _ds["alerts_sent"]      = int(_ds.get("alerts_sent", 0)) + _run_alerts_sent
    _ds["alerts_delivered"] = int(_ds.get("alerts_delivered", 0)) + _run_alerts_ok
    _ds["games_seen"]       = max(int(_ds.get("games_seen", 0)), _run_games_seen)
    _ds["games_finalised"]  = int(_ds.get("games_finalised", 0)) + _run_games_final

    # Accumulate Odds API usage into daily bucket (#266)
    _oa_cs = odds_api.get_credit_status()
    _ds["odds_api_calls"]        = int(_ds.get("odds_api_calls", 0)) + _oa_cs["api_calls"]
    _ds["odds_api_credits_used"] = int(_ds.get("odds_api_credits_used", 0)) + _oa_cs["credits_used"]

    _cf = _ds.setdefault("conditions_fired", {})
    if not isinstance(_cf, dict):
        _cf = {}
        _ds["conditions_fired"] = _cf
    for _h in all_hits:
        if (_h["event_id"], _h["team_id"], _h["cond"]["name"],
                _h["state_key"]) in alerted_hit_keys:
            _cn = str(_h["cond"].get("name") or "unknown")
            _cf[_cn] = int(_cf.get(_cn, 0)) + 1

    state["__daily_current_key"] = _today_key
    state_dirty = True

    if state_dirty:
        save_state(state)

    _perf_wall_ms = (time.perf_counter() - _perf_t0) * 1000
    _perf_ru1     = resource.getrusage(resource.RUSAGE_SELF)
    _perf_cpu_ms  = (_perf_ru1.ru_utime + _perf_ru1.ru_stime
                     - _perf_ru0.ru_utime - _perf_ru0.ru_stime) * 1000
    _perf_rss_kb  = _perf_ru1.ru_maxrss

    # Backfill wall_ms_sum now that we have the final value
    _ds["wall_ms_sum"] = float(_ds.get("wall_ms_sum", 0)) + _perf_wall_ms
    save_state(state)

    _log(
        "[PERF] wall={:.0f}ms cpu={:.0f}ms rss={}KB".format(
            _perf_wall_ms, _perf_cpu_ms, _perf_rss_kb
        )
    )

    # One-shot Slack alert when credits are running low (#213)
    if odds_api.is_credits_low() and not state.get("__odds_api_low_credit_alerted"):
        _oa_cs = odds_api.get_credit_status()
        _oa_warn = ":warning: *Odds API credits low* — {} remaining (threshold: {})".format(
            _oa_cs.get("credits_remaining"), odds_api.LOW_CREDIT_THRESHOLD)
        _log("[ODDS_API_WARN] {}".format(_oa_warn))
        send_alert_result(config, _oa_warn)
        state["__odds_api_low_credit_alerted"] = True
        state_dirty = True
        save_state(state)
    elif odds_api.get_credit_status()["api_calls"] > 0 and not odds_api.is_credits_low() and state.get("__odds_api_low_credit_alerted"):
        # Credits replenished (monthly reset) — clear the flag
        state.pop("__odds_api_low_credit_alerted", None)
        state_dirty = True
        save_state(state)


if __name__ == "__main__":
    main()
