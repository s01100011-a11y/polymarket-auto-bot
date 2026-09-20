"""Central league configuration — reads league from config at import time.

Provides state_path() for league-suffixed state file paths and cache_dir()
for league-scoped ESPN cache directories. All modules import from here
instead of hardcoding file paths.

Config file selection (checked in order):
  1. NBA_MONITOR_CONFIG env var  (e.g. "config-wnba.json")
  2. Falls back to config.json

Usage:
  NBA_MONITOR_CONFIG=config-wnba.json python3 monitor.py
  NBA_MONITOR_CONFIG=config-wnba.json python3 server.py 8900
"""
import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Resolve config file: env var override → default config.json
_env_config = os.environ.get("NBA_MONITOR_CONFIG", "")
if _env_config:
    # Relative paths resolve against SCRIPT_DIR
    CONFIG_FILE = _env_config if os.path.isabs(_env_config) else os.path.join(SCRIPT_DIR, _env_config)
else:
    CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")

VALID_LEAGUES = ("nba", "wnba")

# Read league from config once at import time.
try:
    with open(CONFIG_FILE, encoding="utf-8") as _f:
        LEAGUE = json.load(_f).get("league", "nba")
except Exception:
    LEAGUE = "nba"

if LEAGUE not in VALID_LEAGUES:
    LEAGUE = "nba"

# Period lengths by league (seconds)
PERIOD_SECONDS = {
    "regulation": 600 if LEAGUE == "wnba" else 720,
    "overtime": 300,
}


def state_path(name):
    """Return league-suffixed state file path.

    Examples (league="nba"):
        state_path("game_history.json")  → ".../game_history_nba.json"
        state_path(".alert_state.json")  → ".../.alert_state_nba.json"
        state_path("model.pkl")          → ".../model_nba.pkl"
    """
    base, ext = os.path.splitext(name)
    return os.path.join(SCRIPT_DIR, "{}_{}{}".format(base, LEAGUE, ext))


def cache_dir():
    """Return league-scoped ESPN cache directory path.

    Examples:
        cache_dir()  → ".../espn_cache/nba/"
    """
    return os.path.join(SCRIPT_DIR, "espn_cache", LEAGUE)


def log_path(name):
    """Return league-suffixed log file path.

    Examples (league="nba"):
        log_path("monitor.log")  → ".../logs/monitor_nba.log"
    """
    base, ext = os.path.splitext(name)
    return os.path.join(SCRIPT_DIR, "logs", "{}_{}{}".format(base, LEAGUE, ext))


# ── One-time migration: rename unsuffixed files to league-suffixed ────────────

_MIGRATE_STATE_FILES = [
    ".alert_state.json",
    "alerts.json",
    "game_history.json",
    "game_history.wal",
    "bayes_state.json",
    "live_analysis.json",
    "live_stats.json",
    "game_ticker.json",
    "h1_records.json",
    "evanalytics_cache.json",
    "backfill_state.json",
    "model_eval_cache.json",
    "model_eval_history.json",
    "ml_training_history.json",
    "outcomes_cache.json",
    ".log_rotation_state.json",
    ".monitor_timer_auto_state.json",
    "model.pkl",
    "model_meta.json",
]

_MIGRATE_LOG_FILES = [
    "monitor.log",
    "dashboard.log",
]


def _migrate_legacy_files():
    """Rename unsuffixed state/log files to league-suffixed names (one-time).

    Only runs when the suffixed file does NOT exist and the unsuffixed file DOES.
    Safe to call multiple times — idempotent.
    """
    migrated = []
    for name in _MIGRATE_STATE_FILES:
        old = os.path.join(SCRIPT_DIR, name)
        new = state_path(name)
        if old != new and os.path.exists(old) and not os.path.exists(new):
            os.rename(old, new)
            migrated.append(name)

    for name in _MIGRATE_LOG_FILES:
        old = os.path.join(SCRIPT_DIR, "logs", name)
        new = log_path(name)
        if old != new and os.path.exists(old) and not os.path.exists(new):
            os.rename(old, new)
            migrated.append("logs/" + name)

    # Migrate espn_cache/ → espn_cache/{league}/
    old_cache = os.path.join(SCRIPT_DIR, "espn_cache")
    new_cache = cache_dir()
    if (os.path.isdir(old_cache) and not os.path.isdir(new_cache)
            and os.path.isdir(os.path.join(old_cache, "game_summary"))):
        # Old layout: espn_cache/game_summary/, espn_cache/play_by_play/, etc.
        # Move contents into espn_cache/{league}/
        os.makedirs(new_cache, exist_ok=True)
        for item in os.listdir(old_cache):
            if item in VALID_LEAGUES:
                continue  # skip league subdirs already created
            src = os.path.join(old_cache, item)
            dst = os.path.join(new_cache, item)
            if not os.path.exists(dst):
                os.rename(src, dst)
        migrated.append("espn_cache/ → espn_cache/{}/".format(LEAGUE))

    if migrated:
        import sys
        print("[league_config] Migrated {} files to league-suffixed names (league={})".format(
            len(migrated), LEAGUE), file=sys.stderr)
        for f in migrated:
            print("  {} → {}".format(f, f.replace(".", "_{}.".format(LEAGUE), 1)
                                     if not f.startswith("espn_cache") else f), file=sys.stderr)


_migrate_legacy_files()
