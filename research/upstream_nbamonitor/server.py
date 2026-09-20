#!/usr/bin/env python3
"""
NBA Monitor — Dashboard server
Serves static files + handles config read/write via /api/* endpoints.
Usage: python3 server.py [port]   (default: 8899)
"""

import gc
import json
import os
import re
import base64
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import league_config
import outcomes as outcomes_mod
import evaluate_model as eval_mod
import conditions as conditions_mod
import evanalytics as evanalytics_mod
import h1_store
import odds_api as odds_api_mod
import runtime_backup
import summer_league_odds as sl_odds_mod

CONFIG_FILE        = league_config.CONFIG_FILE
ALERTS_FILE        = league_config.state_path("alerts.json")
LIVE_STATS_FILE    = league_config.state_path("live_stats.json")
TICKER_FILE        = league_config.state_path("game_ticker.json")
HISTORY_FILE       = league_config.state_path("game_history.json")
LIVE_ANALYSIS_FILE = league_config.state_path("live_analysis.json")
MODEL_EVAL_HISTORY_FILE = league_config.state_path("model_eval_history.json")
ML_META_FILE       = league_config.state_path("model_meta.json")
MONITOR_TIMER_AUTO_STATE_FILE = league_config.state_path(".monitor_timer_auto_state.json")
PORT        = int(sys.argv[1]) if len(sys.argv) > 1 else 8899

MIME = {
    ".html": "text/html",
    ".css":  "text/css",
    ".js":   "application/javascript",
    ".json": "application/json",
    ".png":  "image/png",
    ".ico":  "image/x-icon",
}

LOG_FILE_PATH_RE = re.compile(r"^logs/[A-Za-z0-9._/-]+\.log(?:\.\d{4}-\d{2}-\d{2})?$")
SYSTEMD_ABS_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+\-]+$")
LOG_ROTATION_PERIODS = {"daily", "weekly", "monthly", "days"}
CONDITION_TYPES = {
    "score_diff",
    "consecutive_points_run",
    "fg_percent_improve",
    "fg_pct_threshold",
    "turnover_rate",
    "turnovers",
    "points_off_turnovers",
    "back_to_back",
    "underdog_at_home",
    "h1_moneyline_edge",
    "predicted_winner_threshold",
    "pw_underdog_alert",
}
PRE_GAME_CONDITION_TYPES = {"back_to_back", "h1_moneyline_edge"}
PW_HALF_QUARTERS = {"H1": ("Q1", "Q2"), "H2": ("Q3", "Q4"), "OT": ("OT", "2OT")}  # #412 Phase 1.2
SECRET_KEYS = {"slack_bot_token", "openclaw_token", "odds_api_key"}  # #412 Phase 2.2/2.5
STATIC_ALLOWLIST = {"dashboard.html", "favicon.ico", "pw_roi_report.html"}  # #412 Phase 2.2

def _safe_backup_name(name, backup_root):
    """Validate and sanitise a backup directory name. Returns (safe_name, error)."""
    name = os.path.basename(str(name or "").strip())
    if not name or ".." in name or "/" in name:
        return None, "invalid backup name"
    full = os.path.realpath(os.path.join(backup_root, name))
    if not full.startswith(os.path.realpath(backup_root) + os.sep):
        return None, "path escapes backup root"
    return name, None
DEFAULT_TIMER_ON_CALENDAR = "*-*-* 22,23,00,01,02,03,04,05,06,07,08:*:00 UTC"
TIMER_FILE_NAME = "{}-monitor.timer".format(league_config.LEAGUE)
TIMER_UNIT_NAME = "{}-monitor.timer".format(league_config.LEAGUE)
VALID_LEAGUES = ("nba", "wnba")

def _espn_scoreboard_url(league="nba"):
    return "https://site.api.espn.com/apis/site/v2/sports/basketball/{}/scoreboard".format(league)

# Default; overridden at startup from config["league"]
ESPN_SCOREBOARD_URL = _espn_scoreboard_url()
DEFAULT_MODEL_EVAL_SNAPSHOT_ENABLED = False
DEFAULT_MODEL_EVAL_SNAPSHOT_INTERVAL_MINUTES = 360
MODEL_EVAL_SNAPSHOT_MIN_INTERVAL_SECONDS = 15 * 60
MODEL_EVAL_SNAPSHOT_MAX_ENTRIES = 120
MODEL_EVAL_AUTOMATION_POLL_SECONDS = 60
MODEL_EVAL_HISTORY_LOCK = threading.Lock()
DEFAULT_MONITOR_TIMER_AUTO_ENABLED = False
DEFAULT_MONITOR_TIMER_AUTO_PRE_MINUTES = 60
DEFAULT_MONITOR_TIMER_AUTO_POST_MINUTES = 150
DEFAULT_MONITOR_TIMER_AUTO_INTERVAL_MINUTES = 1
DEFAULT_MONITOR_TIMER_AUTO_MIN_END_HOUR_UTC = 8
MONITOR_TIMER_AUTO_WORKER_POLL_SECONDS = 600
MONITOR_TIMER_AUTO_LOCK = threading.Lock()


def _log(*parts):
    """Print a UTC-timestamped log line to stderr."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    msg = " ".join(str(p) for p in parts)
    print("{} {}".format(ts, msg), flush=True)


def normalize_log_file_path(path):
    """Return normalized log path using forward slashes and trimmed whitespace."""
    return str(path).strip().replace("\\", "/")


def validate_log_file_path(path):
    """Return validation error string for invalid log file path, else None."""
    p = normalize_log_file_path(path)

    if not p:
        return "must be a non-empty string"
    if p.startswith("/") or re.match(r"^[A-Za-z]:/", p):
        return "must be a relative path like logs/monitor.log"
    if ".." in p or "//" in p:
        return "must not include path traversal or empty path segments"
    if not LOG_FILE_PATH_RE.fullmatch(p):
        return "must match logs/<name>.log or logs/<name>.log.YYYY-MM-DD"

    return None


def resolve_log_tail_path(path):
    """Resolve and validate a log tail request path under SCRIPT_DIR/logs."""
    p = normalize_log_file_path(path)
    validation_error = validate_log_file_path(p)
    if validation_error:
        return None, validation_error

    abs_path = os.path.realpath(os.path.join(SCRIPT_DIR, p))
    logs_root = os.path.realpath(os.path.join(SCRIPT_DIR, "logs"))
    if not abs_path.startswith(logs_root + os.sep):
        return None, "path must remain inside logs/"

    return abs_path, None


def count_file_lines(path):
    """Count total lines in a file efficiently."""
    if not os.path.exists(path):
        return 0
    try:
        with open(path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def read_log_tail_bytes(path, tail_bytes=32768):
    """Return a base64-encoded byte tail for a validated log file path."""
    try:
        requested = int(tail_bytes)
    except (TypeError, ValueError):
        requested = 32768
    requested = min(262144, max(1024, requested))

    abs_path, path_err = resolve_log_tail_path(path)
    if path_err:
        return {
            "ok": False,
            "error": path_err,
            "path": normalize_log_file_path(path),
            "requested_bytes": requested,
        }

    if not os.path.exists(abs_path):
        return {
            "ok": True,
            "path": normalize_display_path(abs_path),
            "requested_bytes": requested,
            "byte_count": 0,
            "file_size": 0,
            "truncated": False,
            "total_lines": 0,
            "encoding": "base64",
            "content_b64": "",
        }

    try:
        file_size = os.path.getsize(abs_path)
    except OSError:
        file_size = 0

    tail_len = min(requested, max(0, file_size))
    start_pos = max(0, file_size - tail_len)
    try:
        with open(abs_path, "rb") as f:
            f.seek(start_pos)
            data = f.read(tail_len)
    except OSError as exc:
        return {
            "ok": False,
            "error": "failed to read log file: {}".format(exc),
            "path": normalize_display_path(abs_path),
            "requested_bytes": requested,
        }

    total_lines = count_file_lines(abs_path)

    return {
        "ok": True,
        "path": normalize_display_path(abs_path),
        "requested_bytes": requested,
        "byte_count": len(data),
        "file_size": file_size,
        "truncated": file_size > len(data),
        "total_lines": total_lines,
        "encoding": "base64",
        "content_b64": base64.b64encode(data).decode("ascii"),
    }


def normalize_display_path(path):
    """Return path normalized for display and config comparison.

    If the path lives under SCRIPT_DIR, return a relative path. Otherwise,
    keep the absolute path.
    """
    p = str(path).strip().replace("\\", "/")
    if not p:
        return p

    abs_path = os.path.abspath(p)
    script_root = os.path.abspath(SCRIPT_DIR)
    try:
        rel = os.path.relpath(abs_path, script_root)
        if not rel.startswith(".." + os.sep) and rel != "..":
            return rel.replace("\\", "/")
    except ValueError:
        pass

    return abs_path.replace("\\", "/")


def parse_systemd_output_path(value):
    """Extract a filesystem path from a systemd StandardOutput/StandardError value."""
    v = str(value).strip()
    for prefix in ("append:", "file:", "truncate:"):
        if v.startswith(prefix):
            raw = v[len(prefix):].strip()
            return raw if raw else None

    # Support direct absolute file paths if present.
    if SYSTEMD_ABS_PATH_RE.fullmatch(v):
        return v

    return None


def detect_output_from_unit_file(unit_file_path):
    """Return detected output log path from a systemd unit file, or None."""
    if not os.path.isfile(unit_file_path):
        return None

    try:
        with open(unit_file_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None

    # Prefer StandardOutput, then StandardError as fallback.
    for key in ("StandardOutput", "StandardError"):
        for line in lines:
            stripped = line.strip()
            if not stripped.startswith(key + "="):
                continue
            raw_value = stripped.split("=", 1)[1].strip()
            log_path = parse_systemd_output_path(raw_value)
            if log_path:
                return log_path

    return None


def detect_service_log_output(service_file_name, default_path):
    """Detect service log output path from user unit first, then repo template."""
    candidates = [
        (
            "user-systemd",
            os.path.expanduser(os.path.join("~", ".config", "systemd", "user", service_file_name)),
        ),
        (
            "repo-systemd-template",
            os.path.join(SCRIPT_DIR, "systemd", service_file_name),
        ),
    ]

    for source, unit_path in candidates:
        detected = detect_output_from_unit_file(unit_path)
        if detected:
            return {
                "path": normalize_display_path(detected),
                "source": source,
                "unit_file": unit_path,
            }

    return {
        "path": default_path,
        "source": "default",
        "unit_file": None,
    }


def _parse_log_date_param(value, end_of_day=False):
    """Parse YYYY-MM-DD query params into UTC datetimes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
    return dt.replace(tzinfo=timezone.utc)


def _discover_log_files(base_paths):
    """Return current + rotated log files for the provided logical base paths."""
    items = []
    seen = set()
    logs_root = os.path.realpath(os.path.join(SCRIPT_DIR, "logs"))
    for base in base_paths:
        rel_base = normalize_log_file_path(base)
        if not rel_base or rel_base in seen:
            continue
        seen.add(rel_base)
        abs_base, err = resolve_log_tail_path(rel_base)
        if err:
            continue
        base_name = os.path.basename(rel_base)
        if os.path.exists(abs_base):
            try:
                st = os.stat(abs_base)
                items.append({
                    "path": rel_base,
                    "logical_name": rel_base,
                    "display_name": base_name + " (current)",
                    "is_active": True,
                    "date": None,
                    "size": st.st_size,
                })
            except OSError:
                pass
        parent = os.path.dirname(abs_base)
        prefix = os.path.basename(abs_base) + "."
        if not os.path.isdir(parent) or os.path.realpath(parent) != logs_root:
            continue
        try:
            for fname in sorted(os.listdir(parent), reverse=True):
                if not fname.startswith(prefix):
                    continue
                suffix = fname[len(prefix):]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}|\d{1,2}", suffix):
                    continue
                rel_path = "logs/" + fname
                abs_path = os.path.join(parent, fname)
                try:
                    st = os.stat(abs_path)
                    items.append({
                        "path": rel_path,
                        "logical_name": rel_base,
                        "display_name": f"{base_name} ({suffix})",
                        "is_active": False,
                        "date": suffix,
                        "size": st.st_size,
                    })
                except OSError:
                    continue
        except OSError:
            continue
    return items


def get_rotation_files_from_config(cfg):
    """Return normalized log rotation settings from config payload."""
    log_rotation = cfg.get("log_rotation", {})
    if not isinstance(log_rotation, dict):
        log_rotation = {}

    _default_log_files = [
        "logs/monitor_{}.log".format(league_config.LEAGUE),
        "logs/dashboard_{}.log".format(league_config.LEAGUE),
    ]
    files = log_rotation.get("files", _default_log_files)
    if not isinstance(files, list):
        files = _default_log_files

    normalized_files = []
    for f in files:
        if not isinstance(f, str):
            continue
        nf = normalize_log_file_path(f)
        if nf:
            normalized_files.append(nf)

    if not normalized_files:
        normalized_files = _default_log_files

    raw_period = str(log_rotation.get("period", "daily")).lower().strip()
    period = raw_period if raw_period in LOG_ROTATION_PERIODS else "daily"

    try:
        period_days = int(log_rotation.get("period_days", 7))
    except (TypeError, ValueError):
        period_days = 7
    period_days = min(99, max(1, period_days))

    try:
        rotation_hour_utc = int(log_rotation.get("rotation_hour_utc", 12))
    except (TypeError, ValueError):
        rotation_hour_utc = 12
    rotation_hour_utc = max(0, min(23, rotation_hour_utc))

    return {
        "enabled": bool(log_rotation.get("enabled", False)),
        "period": period,
        "period_days": period_days,
        "rotation_hour_utc": rotation_hour_utc,
        "files": normalized_files,
    }


def detect_log_outputs_payload():
    """Build payload describing detected process log outputs and rotation coverage."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}

    rotation = get_rotation_files_from_config(cfg)
    rotation_set = set(rotation["files"])

    _svc_prefix = "wnba" if league_config.LEAGUE == "wnba" else "nba"
    monitor = detect_service_log_output("{}-monitor.service".format(_svc_prefix),
                                        "logs/monitor_{}.log".format(league_config.LEAGUE))
    dashboard = detect_service_log_output("{}-dashboard.service".format(_svc_prefix),
                                          "logs/dashboard_{}.log".format(league_config.LEAGUE))

    cloud_backup_log = "logs/cloud_backup.log"
    outputs = [
        {
            "process": "monitor",
            "path": monitor["path"],
            "source": monitor["source"],
            "unit_file": monitor["unit_file"],
            "in_rotation": monitor["path"] in rotation_set,
        },
        {
            "process": "dashboard",
            "path": dashboard["path"],
            "source": dashboard["source"],
            "unit_file": dashboard["unit_file"],
            "in_rotation": dashboard["path"] in rotation_set,
        },
        {
            "process": "cloud_backup",
            "path": cloud_backup_log,
            "source": "file",
            "unit_file": "cloud-backup.service",
            "in_rotation": cloud_backup_log in rotation_set,
        },
    ]

    logical_files = []
    for item in outputs:
        p = normalize_log_file_path(item.get("path") or "")
        if p and p not in logical_files:
            logical_files.append(p)
    for p in rotation["files"]:
        np = normalize_log_file_path(p)
        if np and np not in logical_files:
            logical_files.append(np)

    files = _discover_log_files(logical_files)

    return {
        "rotation": rotation,
        "outputs": outputs,
        "files": files,
    }


def normalize_bool(value, default=False):
    """Coerce common truthy/falsey values to bool, with a fallback default."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def trim_text(value, max_len=800):
    """Return a trimmed single-string command output excerpt."""
    txt = str(value or "").strip()
    if len(txt) > max_len:
        return txt[:max_len] + "...(truncated)"
    return txt


def parse_iso_utc(value):
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


def read_json_list(path):
    """Read a JSON list file, returning [] on missing/invalid content."""
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def atomic_write_json(path, payload):
    """Write JSON atomically to disk."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def parse_model_eval_step_days(cfg):
    """Normalize model eval step_days setting (default 7)."""
    raw = cfg.get("model_eval_step_days", 7)
    try:
        step_days = int(raw)
    except (TypeError, ValueError):
        step_days = 7
    return min(30, max(1, step_days))


def parse_model_eval_min_train(cfg):
    """Normalize model eval min train setting."""
    raw_min = cfg.get("model_eval_min_train", 50)
    try:
        min_train = int(raw_min)
    except (TypeError, ValueError):
        min_train = 50
    return min(500, max(1, min_train))


def parse_model_eval_snapshot_settings(cfg):
    """Normalize model-eval snapshot automation settings from config."""
    enabled = normalize_bool(
        cfg.get("model_eval_snapshot_enabled"),
        DEFAULT_MODEL_EVAL_SNAPSHOT_ENABLED,
    )

    raw_interval = cfg.get(
        "model_eval_snapshot_interval_minutes",
        DEFAULT_MODEL_EVAL_SNAPSHOT_INTERVAL_MINUTES,
    )
    try:
        interval_minutes = int(raw_interval)
    except (TypeError, ValueError):
        interval_minutes = DEFAULT_MODEL_EVAL_SNAPSHOT_INTERVAL_MINUTES
    interval_minutes = min(10080, max(15, interval_minutes))

    return {
        "enabled": enabled,
        "interval_minutes": interval_minutes,
        "interval_seconds": interval_minutes * 60,
        "default_enabled": DEFAULT_MODEL_EVAL_SNAPSHOT_ENABLED,
        "default_interval_minutes": DEFAULT_MODEL_EVAL_SNAPSHOT_INTERVAL_MINUTES,
    }


def summarize_model_eval_targets(results):
    """Build compact target metrics for persisted snapshot history."""
    out = {}
    for key, row in (results or {}).items():
        metrics = row.get("metrics") if isinstance(row, dict) else {}
        fm = (metrics or {}).get("frequency") or {}
        bm = (metrics or {}).get("bayesian") or {}
        out[key] = {
            "evaluation_mode": row.get("evaluation_mode") if isinstance(row, dict) else None,
            "eval_n": max(int(fm.get("n", 0) or 0), int(bm.get("n", 0) or 0)),
            "frequency": {
                "brier": fm.get("brier"),
                "log_loss": fm.get("log_loss"),
                "ece": ((fm.get("calibration") or {}).get("ece") if isinstance(fm, dict) else None),
            },
            "bayesian": {
                "brier": bm.get("brier"),
                "log_loss": bm.get("log_loss"),
                "ece": ((bm.get("calibration") or {}).get("ece") if isinstance(bm, dict) else None),
            },
        }
    return out


def build_model_eval_payload(cfg, targets=None):
    """Compute the current model-eval payload for one or more targets.

    Results are cached keyed on (history mtime, min_train, targets) so the
    expensive rolling-origin evaluation (~47s for 5 targets × 2500 records)
    only runs when the history file actually changes.
    """
    min_train = parse_model_eval_min_train(cfg)
    step_days = parse_model_eval_step_days(cfg)

    resolved_targets = []
    for t in (targets or []):
        key = str(t or "").strip()
        if key in eval_mod.TARGET_MAP and key not in resolved_targets:
            resolved_targets.append(key)
    if not resolved_targets:
        resolved_targets = ["team_wins", "game_upset", "game_close", "game_comeback", "game_blowout"]

    try:
        mtime = os.path.getmtime(HISTORY_FILE)
    except OSError:
        mtime = None

    cache_key = (mtime, min_train, step_days, tuple(sorted(resolved_targets)))

    # 1. Check in-memory cache first (fastest)
    with _model_eval_cache_lock:
        if _model_eval_cache["key"] == cache_key and _model_eval_cache["payload"] is not None:
            return _model_eval_cache["payload"]

        # 2. Try loading persisted cache from disk (survives restarts).
        #    Done inside the lock so concurrent threads don't all race to recompute.
        try:
            if os.path.exists(MODEL_EVAL_CACHE_FILE):
                with open(MODEL_EVAL_CACHE_FILE, encoding="utf-8") as _f:
                    persisted = json.load(_f)
                if (
                    isinstance(persisted, dict)
                    and persisted.get("_cache_mtime") == mtime
                    and persisted.get("_cache_min_train") == min_train
                    and persisted.get("_cache_step_days", 1) == step_days
                    and set(persisted.get("_cache_targets") or []) == set(resolved_targets)
                ):
                    payload = {k: v for k, v in persisted.items() if not k.startswith("_cache_")}
                    _model_eval_cache["key"] = cache_key
                    _model_eval_cache["payload"] = payload
                    return payload
        except Exception:
            pass  # non-fatal — fall through to full recompute

    # 3. Full recompute (expensive — ~47s for 5 targets × 2500 records).
    #    Use an Event to serialise concurrent threads: the first thread to reach here
    #    clears the event (signals "computing"), does the work, then sets it again.
    #    Other threads wait on the event and re-check the in-memory cache after it fires.
    _model_eval_compute_event.wait()  # wait if another thread is already computing

    # Re-check cache after waiting — another thread may have just populated it
    with _model_eval_cache_lock:
        if _model_eval_cache["key"] == cache_key and _model_eval_cache["payload"] is not None:
            return _model_eval_cache["payload"]

    # Claim the compute slot
    _model_eval_compute_event.clear()
    try:
        _log("[NBA Monitor] model-eval cache miss — recomputing (this takes ~1-3 min)...")
        records = eval_mod.load_history(HISTORY_FILE)
        results = {}
        for target in resolved_targets:
            results[target] = eval_mod.evaluate_target(
                records=records,
                target=target,
                min_train=min_train,
                bins=10,
                step_days=step_days,
            )

        payload = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "history_file": HISTORY_FILE,
            "records": len(records),
            "min_train": min_train,
            "step_days": step_days,
            "snapshot_settings": parse_model_eval_snapshot_settings(cfg),
            "targets": results,
        }

        # Persist to disk so the next restart loads instantly
        try:
            persisted = dict(payload)
            persisted["_cache_mtime"] = mtime
            persisted["_cache_min_train"] = min_train
            persisted["_cache_step_days"] = step_days
            persisted["_cache_targets"] = sorted(resolved_targets)
            tmp = MODEL_EVAL_CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as _f:
                json.dump(persisted, _f)
            os.replace(tmp, MODEL_EVAL_CACHE_FILE)
            _log("[NBA Monitor] model-eval cache persisted to disk")
        except Exception as _e:
            _log(f"[NBA Monitor] model-eval cache persist failed (non-fatal): {_e}")

        with _model_eval_cache_lock:
            _model_eval_cache["key"] = cache_key
            _model_eval_cache["payload"] = payload

        return payload
    finally:
        _model_eval_compute_event.set()  # always unblock waiting threads



def _get_ml_training_status():
    """Return ML training status: current model meta + training history."""
    meta = {}
    meta_path = league_config.state_path("model_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass

    history = []
    if os.path.exists(ML_TRAINING_HISTORY_FILE):
        try:
            with open(ML_TRAINING_HISTORY_FILE, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            pass

    # Check staleness
    cfg = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass

    import hashlib as _hs
    current_hash = None
    hist_path = league_config.state_path("game_history.json")
    if os.path.exists(hist_path):
        try:
            with open(hist_path, "rb") as f:
                current_hash = _hs.sha256(f.read()).hexdigest()
        except Exception:
            pass

    stored_hash = meta.get("training_history_hash")
    is_stale = current_hash is not None and stored_hash is not None and current_hash != stored_hash

    # Lock status
    lock_held = False
    if os.path.exists(ML_TRAIN_LOCK_FILE):
        try:
            with open(ML_TRAIN_LOCK_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)
            lock_held = True
        except (ValueError, OSError):
            pass

    with _ml_train_lock:
        train_state = dict(_ml_train_state)

    # Compute next scheduled training
    next_scheduled = None
    trained_at = meta.get("trained_at")
    interval_days = cfg.get("ml_training_interval_days", 7)
    schedule_utc = cfg.get("ml_training_schedule_utc", "09:15")
    if cfg.get("ml_training_enabled", True) and trained_at:
        try:
            last_dt = datetime.fromisoformat(trained_at.replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            next_day = last_dt + timedelta(days=interval_days)
            hh, mm = (schedule_utc or "09:15").split(":")
            next_dt = next_day.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            next_scheduled = next_dt.isoformat()
        except Exception:
            pass

    return {
        "model_meta": {
            "trained_at": trained_at,
            "history_hash": stored_hash,
            "targets": list((meta.get("targets") or {}).keys()),
        },
        "current_hash": current_hash,
        "is_stale": is_stale,
        "config": {
            "enabled": cfg.get("ml_training_enabled", True),
            "interval_days": interval_days,
            "schedule_utc": schedule_utc,
            "retention_count": cfg.get("ml_training_retention_count", 10),
        },
        "next_scheduled_at": next_scheduled,
        "training_in_progress": lock_held or train_state.get("status") == "running",
        "train_state": train_state,
        "history": history if isinstance(history, list) else [],
    }


def _get_ml_training_log(filename):
    """Return contents of a training log file (validated against logs dir)."""
    import re as _re
    if not _re.match(r'^ml_train_\d{8}_\d{6}\.log$', filename):
        return {"ok": False, "error": "Invalid log filename"}
    log_path = os.path.join(SCRIPT_DIR, "logs", filename)
    if not os.path.isfile(log_path):
        return {"ok": False, "error": "Log file not found"}
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            content = f.read()
        return {"ok": True, "filename": filename, "content": content, "size": len(content)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _do_ml_train_background():
    """Run ml_train_runner.py --force in a background thread."""
    with _ml_train_lock:
        if _ml_train_state["status"] == "running":
            return
        _ml_train_state["status"] = "running"
        _ml_train_state["started_at"] = datetime.now(timezone.utc).isoformat()
        _ml_train_state["finished_at"] = None
        _ml_train_state["duration_s"] = None
        _ml_train_state["error"] = None
    t0 = time.time()
    try:
        venv_python = os.path.join(SCRIPT_DIR, ".venv", "bin", "python")
        python_cmd = venv_python if os.path.isfile(venv_python) else sys.executable
        runner = os.path.join(SCRIPT_DIR, "ml_train_runner.py")
        proc = subprocess.run(
            [python_cmd, runner, "--force"],
            capture_output=True, text=True, timeout=300, cwd=SCRIPT_DIR,
        )
        elapsed = round(time.time() - t0, 1)
        _all_output = (proc.stdout or "") + "\n" + (proc.stderr or "")
        _has_error = "ERROR" in _all_output.upper() or "Traceback" in _all_output
        _train_ok = proc.returncode == 0 and not _has_error
        with _ml_train_lock:
            _ml_train_state["status"] = "done" if _train_ok else "error"
            _ml_train_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _ml_train_state["duration_s"] = elapsed
            _ml_train_state["error"] = None if _train_ok else (_all_output[:500] if _has_error else "exit code {}".format(proc.returncode))
        _log("[ML-TRAIN] Background training {} in {:.1f}s returncode={}".format("completed" if _train_ok else "FAILED", elapsed, proc.returncode))
    except Exception as exc:
        elapsed = round(time.time() - t0, 1)
        with _ml_train_lock:
            _ml_train_state["status"] = "error"
            _ml_train_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            _ml_train_state["duration_s"] = elapsed
            _ml_train_state["error"] = str(exc)
        _log("[ML-TRAIN] Background training failed after {:.1f}s: {}".format(elapsed, exc))


def append_model_eval_snapshot(payload, interval_seconds, force=False):
    """Append a compact model-eval snapshot to history, with throttling."""
    now = datetime.now(timezone.utc)
    effective_interval_seconds = max(
        MODEL_EVAL_SNAPSHOT_MIN_INTERVAL_SECONDS,
        int(interval_seconds or 0),
    )

    with MODEL_EVAL_HISTORY_LOCK:
        history = read_json_list(MODEL_EVAL_HISTORY_FILE)

        if history and not force:
            last_ts = parse_iso_utc((history[-1] or {}).get("snapshot_ts"))
            if last_ts is not None:
                elapsed = (now - last_ts).total_seconds()
                if elapsed < effective_interval_seconds:
                    wait_seconds = int(max(0, effective_interval_seconds - elapsed))
                    return {
                        "persisted": False,
                        "reason": "throttled",
                        "wait_seconds": wait_seconds,
                        "history_count": len(history),
                    }

        snapshot = {
            "snapshot_ts": now.isoformat(),
            "records": int(payload.get("records", 0) or 0),
            "min_train": int(payload.get("min_train", 0) or 0),
            "targets": summarize_model_eval_targets(payload.get("targets") or {}),
        }
        history.append(snapshot)
        if len(history) > MODEL_EVAL_SNAPSHOT_MAX_ENTRIES:
            history = history[-MODEL_EVAL_SNAPSHOT_MAX_ENTRIES:]

        atomic_write_json(MODEL_EVAL_HISTORY_FILE, history)
        return {
            "persisted": True,
            "reason": "saved",
            "history_count": len(history),
            "snapshot_ts": snapshot["snapshot_ts"],
        }


def maybe_auto_snapshot_model_eval(cfg, reason="auto"):
    """Persist a model-eval snapshot when automation is enabled and due."""
    settings = parse_model_eval_snapshot_settings(cfg)
    if not settings["enabled"]:
        return {
            "persisted": False,
            "reason": "disabled",
            "history_count": len(read_json_list(MODEL_EVAL_HISTORY_FILE)),
        }

    payload = build_model_eval_payload(cfg=cfg)
    result = append_model_eval_snapshot(
        payload,
        interval_seconds=settings["interval_seconds"],
        force=False,
    )
    result["trigger"] = reason
    return result


def model_eval_auto_snapshot_worker():
    """Background worker that periodically persists model-eval snapshots."""
    while True:
        try:
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg = json.load(f)
            maybe_auto_snapshot_model_eval(cfg, reason="worker")
        except Exception as exc:
            _log("[MODEL EVAL] auto snapshot worker error: {}".format(exc))
        time.sleep(MODEL_EVAL_AUTOMATION_POLL_SECONDS)


def get_user_systemd_unit_path(unit_file_name):
    """Return the active user-systemd unit path for a given unit file."""
    return os.path.expanduser(os.path.join("~", ".config", "systemd", "user", unit_file_name))


def get_repo_timer_template_path():
    """Return repository timer template path."""
    return os.path.join(SCRIPT_DIR, "systemd", TIMER_FILE_NAME)


def detect_timer_unit_path_for_read():
    """Detect timer unit path for read operations."""
    user_timer = get_user_systemd_unit_path(TIMER_FILE_NAME)
    if os.path.isfile(user_timer):
        return {
            "path": user_timer,
            "source": "user-systemd",
            "exists": True,
        }

    repo_timer = get_repo_timer_template_path()
    if os.path.isfile(repo_timer):
        return {
            "path": repo_timer,
            "source": "repo-systemd-template",
            "exists": True,
        }

    return {
        "path": user_timer,
        "source": "user-systemd-missing",
        "exists": False,
    }


def parse_timer_unit_settings(unit_file_path):
    """Parse basic fields from a systemd timer unit file."""
    parsed = {
        "description": "",
        "on_calendar_lines": [],
        "on_calendar": "",
        "persistent": True,
    }

    if not os.path.isfile(unit_file_path):
        return parsed

    try:
        with open(unit_file_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return parsed

    current_section = ""
    persistent_seen = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue

        sec_match = re.match(r"^\[([^\]]+)\]$", line)
        if sec_match:
            current_section = sec_match.group(1).strip()
            continue

        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if current_section == "Unit" and key == "Description" and not parsed["description"]:
            parsed["description"] = value

        if current_section != "Timer":
            continue

        if key == "OnCalendar":
            parsed["on_calendar_lines"].append(value)
            if not parsed["on_calendar"]:
                parsed["on_calendar"] = value
        elif key == "Persistent" and not persistent_seen:
            parsed["persistent"] = normalize_bool(value, True)
            persistent_seen = True

    return parsed


def find_section_bounds(lines, section_name):
    """Return (start_idx, end_idx) bounds for a section in systemd unit lines."""
    section_lower = section_name.strip().lower()
    start = None
    end = len(lines)

    for idx, raw_line in enumerate(lines):
        m = re.match(r"^\s*\[([^\]]+)\]\s*$", raw_line)
        if not m:
            continue

        sec = m.group(1).strip().lower()
        if sec == section_lower and start is None:
            start = idx
            continue

        if start is not None:
            end = idx
            break

    return start, end


def upsert_unit_setting(lines, section_name, key, value):
    """Insert or replace a single key=value setting inside a systemd section."""
    setting_line = "{}={}\n".format(key, value)
    key_lower = key.strip().lower()

    sec_start, sec_end = find_section_bounds(lines, section_name)
    if sec_start is None:
        if lines and lines[-1].strip():
            lines.append("\n")
        lines.append("[{}]\n".format(section_name))
        lines.append(setting_line)
        return lines

    match_indexes = []
    for i in range(sec_start + 1, sec_end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#") or stripped.startswith(";"):
            continue
        if "=" not in stripped:
            continue

        k = stripped.split("=", 1)[0].strip().lower()
        if k == key_lower:
            match_indexes.append(i)

    if match_indexes:
        lines[match_indexes[0]] = setting_line
        for i in reversed(match_indexes[1:]):
            del lines[i]
    else:
        lines.insert(sec_end, setting_line)

    return lines


def default_timer_unit_content():
    """Return fallback timer unit content when no template exists."""
    return (
        "[Unit]\n"
        "Description=NBA Monitor timer\n"
        "Requires=nba-monitor.service\n"
        "\n"
        "[Timer]\n"
        "OnCalendar={}\n"
        "AccuracySec=1s\n"
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    ).format(DEFAULT_TIMER_ON_CALENDAR)


def ensure_user_timer_unit_file():
    """Ensure a user-systemd timer unit file exists for edits."""
    user_timer_path = get_user_systemd_unit_path(TIMER_FILE_NAME)
    if os.path.isfile(user_timer_path):
        return user_timer_path, False, None

    try:
        os.makedirs(os.path.dirname(user_timer_path), exist_ok=True)
    except OSError as exc:
        return None, False, str(exc)

    repo_timer_path = get_repo_timer_template_path()
    try:
        if os.path.isfile(repo_timer_path):
            with open(repo_timer_path, encoding="utf-8") as src:
                content = src.read()
        else:
            content = default_timer_unit_content()

        with open(user_timer_path, "w", encoding="utf-8") as dst:
            dst.write(content)
    except OSError as exc:
        return None, False, str(exc)

    return user_timer_path, True, None


def validate_on_calendar(value):
    """Validate OnCalendar payload value and return (normalized, error)."""
    if not isinstance(value, str):
        return None, "'on_calendar' must be a string"

    cleaned = value.strip()
    if not cleaned:
        return None, "'on_calendar' must be non-empty"
    if len(cleaned) > 200:
        return None, "'on_calendar' is too long (max 200 characters)"
    if any(ch in cleaned for ch in ("\n", "\r", "\x00")):
        return None, "'on_calendar' must be a single line"

    return cleaned, None


def _get_cloud_backup_status():
    """Parse cloud_backup.log for last run status and query timer for next scheduled (#402)."""
    import re
    log_path = os.path.join(SCRIPT_DIR, "logs", "cloud_backup.log")
    result = {"available": False}
    # Check if a manual run is in progress (#403)
    if _cloud_backup_proc and _cloud_backup_proc.poll() is None:
        result["running"] = True

    # Parse log file — scan backwards for last run
    if os.path.exists(log_path):
        try:
            with open(log_path, "rb") as f:
                # Read last 256KB — covers long rclone runs with verbose error URLs
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 262144))
                tail = f.read().decode("utf-8", errors="replace")

            lines = tail.strip().split("\n")
            # Find last "started" line
            start_idx = None
            for i in range(len(lines) - 1, -1, -1):
                if "cloud backup started ===" in lines[i]:
                    start_idx = i
                    break

            if start_idx is not None:
                result["available"] = True
                run_lines = lines[start_idx:]
                # Parse timestamp: [YYYY-MM-DD HH:MM:SS UTC]
                ts_re = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC)\]")

                start_match = ts_re.search(run_lines[0])
                if start_match:
                    result["started"] = start_match.group(1)

                # Find completion line and status
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

                # Duration
                if result.get("started") and result.get("last_run"):
                    try:
                        from datetime import datetime as _dt
                        _s = _dt.strptime(result["started"], "%Y-%m-%d %H:%M:%S UTC")
                        _e = _dt.strptime(result["last_run"], "%Y-%m-%d %H:%M:%S UTC")
                        result["duration_seconds"] = int((_e - _s).total_seconds())
                    except Exception:
                        pass

                # Size synced — look for rclone stats-one-line summary
                for line in run_lines:
                    m = re.search(r"(\d+(?:\.\d+)?\s+\w+iB)\s*/\s*\d+(?:\.\d+)?\s+\w+iB,\s*100%", line)
                    if m:
                        result["size_synced"] = m.group(1).strip()

                # Error count + messages
                errors = []
                for line in run_lines:
                    if "<3>ERROR" in line:
                        em = re.search(r"<3>ERROR\s*:\s*(.+)", line)
                        if em:
                            msg = em.group(1).strip()
                            # Shorten: filename + reason, drop URLs and DNS details
                            short = re.sub(r'(Failed to copy|error reading destination directory):.*', lambda m: m.group(1), msg)
                            if "size changed" in msg:
                                sm = re.search(r'size changed from \d+ to \d+', msg)
                                if sm:
                                    short = short + ": " + sm.group(0)
                            elif "i/o timeout" in msg:
                                short = short + ": i/o timeout"
                            # Skip retry/summary lines
                            if "Attempt " in msg:
                                continue
                            if "not deleting" in msg:
                                continue
                            errors.append(short)
                result["error_count"] = len(errors)
                if errors:
                    result["errors"] = errors[:5]  # cap at 5

        except Exception:
            pass

    # Query timer for next scheduled run
    _timer_name = "cloud-backup.timer" if league_config.LEAGUE != "nrl" else "nrl-cloud-backup.timer"
    try:
        _timer = run_systemctl_user([
            "show", _timer_name,
            "--property=NextElapseUSecRealtime",
            "--no-pager",
        ])
        if _timer.get("ok"):
            for _line in (_timer.get("stdout") or "").split("\n"):
                if _line.startswith("NextElapseUSecRealtime="):
                    _val = _line.split("=", 1)[1].strip()
                    if _val and _val != "n/a":
                        result["next_scheduled"] = _val
    except Exception:
        pass

    # Stale check (#403 Phase 4)
    if result.get("last_run"):
        try:
            _last = datetime.strptime(result["last_run"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
            _age_h = (datetime.now(timezone.utc) - _last).total_seconds() / 3600
            result["age_hours"] = round(_age_h, 1)
            if _age_h > 36:
                result["stale"] = True
        except Exception:
            pass

    return result


def _get_cloud_backup_history(last_n=20):
    """Parse cloud_backup.log for all runs, return list of run summaries (#403 Phase 3)."""
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
    # Find all run boundaries
    starts = []
    for i, line in enumerate(lines):
        if "cloud backup started ===" in line:
            starts.append(i)

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
                from datetime import datetime as _dt
                _s = _dt.strptime(run["started"], "%Y-%m-%d %H:%M:%S UTC")
                _e = _dt.strptime(run["completed"], "%Y-%m-%d %H:%M:%S UTC")
                run["duration_seconds"] = int((_e - _s).total_seconds())
            except Exception:
                pass
        # Size
        for line in run_lines:
            m3 = re.search(r"(\d+(?:\.\d+)?\s+\w+iB)\s*/\s*\d+(?:\.\d+)?\s+\w+iB,\s*100%", line)
            if m3:
                run["size_synced"] = m3.group(1).strip()
        # Error count
        errs = sum(1 for line in run_lines if "<3>ERROR" in line
                   and "Attempt " not in line and "not deleting" not in line)
        run["error_count"] = errs
        if run.get("started"):
            runs.append(run)

    # Return most recent first, capped
    runs.reverse()
    return runs[:last_n]


def run_systemctl_user(args):
    """Run a systemctl --user command and return structured command result."""
    command = ["systemctl", "--user"] + list(args)
    cmd_str = " ".join(command)

    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=12)
    except FileNotFoundError:
        return {
            "ok": False,
            "command": cmd_str,
            "exit_code": None,
            "stdout": "",
            "stderr": "systemctl command not found",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "command": cmd_str,
            "exit_code": None,
            "stdout": "",
            "stderr": "command timed out",
        }

    return {
        "ok": proc.returncode == 0,
        "command": cmd_str,
        "exit_code": proc.returncode,
        "stdout": trim_text(proc.stdout),
        "stderr": trim_text(proc.stderr),
    }


def parse_systemctl_show_properties(output):
    """Parse `systemctl show` key-value output into a dict."""
    props = {}
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        if not line or "=" not in line:
            continue
        k, v = line.split("=", 1)
        props[k] = v
    return props


def get_timer_runtime_state():
    """Return runtime/systemd status for the monitor timer unit."""
    result = run_systemctl_user([
        "show",
        TIMER_UNIT_NAME,
        "--property=LoadState",
        "--property=ActiveState",
        "--property=UnitFileState",
        "--property=NextElapseUSecRealtime",
        "--property=Triggers",
        "--no-pager",
    ])

    if not result["ok"]:
        return {
            "ok": False,
            "command": result["command"],
            "error": result["stderr"] or result["stdout"] or "systemctl show failed",
        }

    return {
        "ok": True,
        "command": result["command"],
        "properties": parse_systemctl_show_properties(result["stdout"]),
    }


def get_monitor_timer_payload():
    """Build monitor timer payload used by dashboard timer controls."""
    timer_ref = detect_timer_unit_path_for_read()
    parsed = parse_timer_unit_settings(timer_ref["path"])

    on_calendar_lines = parsed.get("on_calendar_lines") or [DEFAULT_TIMER_ON_CALENDAR]
    on_calendar = parsed.get("on_calendar") or on_calendar_lines[0]

    return {
        "unit_file": timer_ref["path"],
        "display_path": normalize_display_path(timer_ref["path"]),
        "source": timer_ref["source"],
        "exists": timer_ref["exists"],
        "description": parsed.get("description") or "",
        "on_calendar": on_calendar,
        "on_calendar_lines": on_calendar_lines,
        "persistent": bool(parsed.get("persistent", True)),
        "runtime": get_timer_runtime_state(),
    }


_LOG_TAG_RE = re.compile(
    r"^\[(ALERT|HISTORY|PERF|DAILY SUMMARY|WARN|ERROR|INFO|CACHE|PW_FILTER|PW_SUMMARY|PREDICTED WINNER|ODDS_API\w*)\]",
    re.IGNORECASE,
)

# Matches an ISO-8601 UTC timestamp prefix written by _log():
# e.g. "2026-04-08T07:28:35Z " at the very start of a line.
_LOG_TS_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+"
)

def _strip_log_ts_prefix(stripped):
    """Return (ts_or_None, line_without_ts) for a stripped log line."""
    m = _LOG_TS_PREFIX_RE.match(stripped)
    if m:
        return m.group(1), stripped[m.end():]
    return None, stripped

def _classify_log_line(line):
    """Return (level, log_tag, message) for a raw log line, or (None, None, None) to skip."""
    stripped = line.strip()
    if not stripped:
        return None, None, None

    # Strip optional leading timestamp before classification
    _ts, body = _strip_log_ts_prefix(stripped)
    upper = body.upper()

    # Structured tag at start of body — covers [ALERT], [HISTORY], [PERF], [DAILY SUMMARY]
    m = _LOG_TAG_RE.match(body)
    if m:
        tag = m.group(1).upper()
        level_map = {
            "ALERT": "info",
            "HISTORY": "info",
            "PERF": "info",
            "DAILY SUMMARY": "info",
            "INFO": "info",
            "CACHE": "info",
            "PW_FILTER": "info",
            "PW_SUMMARY": "info",
            "PREDICTED WINNER": "info",
            "ODDS_API": "info",
            "WARN": "warn",
            "ERROR": "error",
        }
        # Normalize ODDS_API_* variants to single ODDS_API tag
        if tag.startswith("ODDS_API"):
            level = "warn" if "WARN" in tag else "info"
            tag = "ODDS_API"
        else:
            level = level_map.get(tag, "info")
        message = stripped
        return level, tag, message

    # Cloud backup log lines: [YYYY-MM-DD HH:MM:SS UTC] message
    _cb_m = re.match(r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC\]\s*(.*)", stripped)
    if _cb_m:
        _cb_msg = _cb_m.group(1)
        _cb_level = "error" if "<3>ERROR" in _cb_msg else "info"
        return _cb_level, "CLOUD", stripped

    # Untagged lines — WARN/ERROR by keyword; INFO for known service lifecycle messages
    if "[ERROR]" in upper:
        return "error", "ERROR", stripped
    if "[WARN]" in upper:
        return "warn", "WARN", stripped

    # Service lifecycle INFO patterns (journald-only, no structured tag)
    info_patterns = [
        re.compile(r"started\s+(?:nba|wnba)-(monitor|dashboard)\.service", re.IGNORECASE),
        re.compile(r"\b(rotated log|removed old rotated log)\b", re.IGNORECASE),
        re.compile(r"\b(pre-save snapshot|snapshot created|snapshot_dir)\b", re.IGNORECASE),
        re.compile(r"\b(runtime backup completed|backup completed|restore completed)\b", re.IGNORECASE),
        re.compile(r"^\[(?:NBA|WNBA) Monitor\]", re.IGNORECASE),
    ]
    if any(p.search(stripped) for p in info_patterns):
        return "info", "INFO", stripped

    return None, None, None


def get_monitor_log_events(limit=200, logical_name="", date_from="", date_to=""):
    """Return recent log events from monitor.log (all tagged types) plus journal service events."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = 200
    n = min(2000, max(20, n))

    def include_info_message(msg):
        # Kept for compatibility with journal path below
        level, tag, _ = _classify_log_line(str(msg or ""))
        return level is not None

    dt_from = _parse_log_date_param(date_from, end_of_day=False)
    dt_to = _parse_log_date_param(date_to, end_of_day=True)
    norm_logical_name = normalize_log_file_path(logical_name)

    def _in_requested_range(ts_value):
        parsed = parse_iso_utc(ts_value)
        if parsed is None:
            return True
        if dt_from and parsed < dt_from:
            return False
        if dt_to and parsed > dt_to:
            return False
        return True

    def collect_journal_events_for_unit(unit_name, source_name):
        cmd = [
            "journalctl",
            "--user",
            "-u",
            unit_name,
            "-n",
            str(n),
            "--no-pager",
            "--output=short-iso",
        ]

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=4)
        except FileNotFoundError:
            return None, {
                "ok": False,
                "events": [],
                "error": "journalctl command not found",
            }
        except subprocess.TimeoutExpired:
            return None, {
                "ok": False,
                "events": [],
                "error": "journalctl query timed out",
            }

        if proc.returncode not in (0, 1):
            return None, {
                "ok": False,
                "events": [],
                "error": trim_text(proc.stderr) or "journalctl query failed",
            }

        rows = []
        for raw_line in str(proc.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue

            first_space = line.find(" ")
            if first_space <= 0:
                continue
            ts = line[:first_space]
            rest = line[first_space + 1 :].strip()

            message = rest.split(": ", 1)[1] if ": " in rest else rest

            if "ALERT DELIVERY ERROR" in message.upper():
                continue

            level, log_tag, _ = _classify_log_line(message)
            if level is None:
                continue

            if not _in_requested_range(ts):
                continue

            rows.append(
                {
                    "ts": ts,
                    "level": level,
                    "log_tag": log_tag or "INFO",
                    "source": source_name,
                    "message": message,
                }
            )

        return rows, None

    _svc_prefix = "wnba" if league_config.LEAGUE == "wnba" else "nba"
    monitor_path = normalize_display_path(league_config.log_path("monitor.log"))
    dashboard_path = normalize_display_path(league_config.log_path("dashboard.log"))
    cloud_backup_path = "logs/cloud_backup.log"
    logical_files = [monitor_path, dashboard_path, cloud_backup_path]
    logical_name_set = set()
    for p in logical_files:
        logical_name_set.add(normalize_log_file_path(p))
    if norm_logical_name and norm_logical_name not in logical_name_set:
        logical_files = [norm_logical_name]
    files = _discover_log_files(logical_files)

    monitor_events = monitor_err = dashboard_events = dashboard_err = None
    if not norm_logical_name or norm_logical_name == normalize_log_file_path(monitor_path):
        monitor_events, monitor_err = collect_journal_events_for_unit("{}-monitor.service".format(_svc_prefix), "monitor")
    if not norm_logical_name or norm_logical_name == normalize_log_file_path(dashboard_path):
        dashboard_events, dashboard_err = collect_journal_events_for_unit("{}-dashboard.service".format(_svc_prefix), "dashboard")

    def collect_file_log_events(file_items):
        events = []
        for item in file_items:
            rel_path = item.get("path")
            abs_path, err = resolve_log_tail_path(rel_path)
            if err or not os.path.exists(abs_path):
                continue
            _TAIL_BYTES = 256 * 1024
            try:
                file_size = os.path.getsize(abs_path)
                with open(abs_path, "rb") as f:
                    if file_size > _TAIL_BYTES:
                        f.seek(-_TAIL_BYTES, 2)
                        f.readline()
                    raw = f.read().decode("utf-8", errors="replace")
                lines = raw.splitlines()
            except OSError:
                continue
            try:
                mtime_iso = datetime.fromtimestamp(os.path.getmtime(abs_path), timezone.utc).isoformat()
            except OSError:
                mtime_iso = datetime.now(timezone.utc).isoformat()
            _ln = str(item.get("logical_name") or "")
            source_name = "dashboard" if "dashboard" in _ln else "cloud_backup" if "cloud_backup" in _ln else "monitor"
            for raw_line in reversed(lines):
                level, log_tag, message = _classify_log_line(raw_line)
                if level is None or not message:
                    continue
                if "ALERT DELIVERY ERROR" in message.upper():
                    continue
                line_ts, _ = _strip_log_ts_prefix(raw_line.strip())
                ts = (line_ts.rstrip("Z") + "+00:00") if line_ts else mtime_iso
                if not _in_requested_range(ts):
                    continue
                events.append({
                    "ts": ts,
                    "level": level,
                    "log_tag": log_tag or "INFO",
                    "source": source_name,
                    "log_file": rel_path,
                    "message": message,
                })
        return events

    file_log_events = collect_file_log_events(files)

    if monitor_events is None and dashboard_events is None:
        # journalctl unavailable — use file log only
        monitor_events = []
        dashboard_events = []
    else:
        # Merge journal service events with file log events (all tagged lines)
        events = (monitor_events or []) + (dashboard_events or []) + file_log_events
        events.sort(key=lambda item: parse_iso_utc(item.get("ts") or "") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        # Deduplicate by message (journal and file may overlap for WARN/ERROR)
        seen_msgs = set()
        deduped = []
        for ev in events:
            key = "{}|{}|{}".format(ev.get("source", ""), ev.get("log_file", ""), ev.get("message", ""))
            if key not in seen_msgs:
                seen_msgs.add(key)
                deduped.append(ev)
        return {
            "ok": True,
            "events": deduped[:n],
            "source": "journal+file",
        }

    if monitor_err and dashboard_err:
        return {
            "ok": False,
            "events": [],
            "error": "{}; {}".format(monitor_err.get("error"), dashboard_err.get("error")),
        }

    # File-only fallback path
    if not file_log_events:
        return {
            "ok": True,
            "events": [],
            "source": "file",
        }

    return {
        "ok": True,
        "events": file_log_events[:n],
        "source": "monitor-log-file",
    }


def update_monitor_timer(payload):
    """Apply timer schedule updates in user systemd timer unit file."""
    on_calendar, on_calendar_err = validate_on_calendar(payload.get("on_calendar"))
    if on_calendar_err:
        return None, on_calendar_err

    persistent = normalize_bool(payload.get("persistent"), True)
    reload_and_restart = normalize_bool(payload.get("reload_and_restart"), True)

    user_timer_path, created_user_unit, ensure_err = ensure_user_timer_unit_file()
    if ensure_err:
        return None, "Failed to prepare user timer file: {}".format(ensure_err)

    try:
        with open(user_timer_path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        return None, "Failed to read timer file: {}".format(exc)

    if not lines:
        lines = default_timer_unit_content().splitlines(keepends=True)

    lines = upsert_unit_setting(lines, "Timer", "OnCalendar", on_calendar)
    lines = upsert_unit_setting(lines, "Timer", "AccuracySec", "1s")
    lines = upsert_unit_setting(lines, "Timer", "Persistent", "true" if persistent else "false")

    tmp_path = user_timer_path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        os.replace(tmp_path, user_timer_path)
    except OSError as exc:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return None, "Failed to write timer file: {}".format(exc)

    actions = []
    warnings = []
    if reload_and_restart:
        daemon_reload = run_systemctl_user(["daemon-reload"])
        restart_timer = run_systemctl_user(["restart", TIMER_UNIT_NAME])
        actions.extend([daemon_reload, restart_timer])
        for action in actions:
            if not action.get("ok"):
                msg = action.get("stderr") or action.get("stdout") or "unknown systemctl error"
                warnings.append("{} failed: {}".format(action.get("command"), msg))

    timer_payload = get_monitor_timer_payload()
    timer_payload["unit_file"] = user_timer_path
    timer_payload["display_path"] = normalize_display_path(user_timer_path)
    timer_payload["source"] = "user-systemd"
    timer_payload["exists"] = True

    return {
        "ok": True,
        "timer": timer_payload,
        "actions": actions,
        "created_user_unit": created_user_unit,
        "reload_and_restart": reload_and_restart,
        "warnings": warnings,
    }, None


def parse_monitor_timer_auto_settings(cfg):
    """Normalize monitor timer auto-scheduling settings from config."""
    enabled = normalize_bool(
        cfg.get("monitor_timer_auto_from_schedule_enabled"),
        DEFAULT_MONITOR_TIMER_AUTO_ENABLED,
    )

    try:
        pre_minutes = int(cfg.get("monitor_timer_auto_pre_minutes", DEFAULT_MONITOR_TIMER_AUTO_PRE_MINUTES))
    except (TypeError, ValueError):
        pre_minutes = DEFAULT_MONITOR_TIMER_AUTO_PRE_MINUTES
    pre_minutes = min(360, max(0, pre_minutes))

    try:
        post_minutes = int(cfg.get("monitor_timer_auto_post_minutes", DEFAULT_MONITOR_TIMER_AUTO_POST_MINUTES))
    except (TypeError, ValueError):
        post_minutes = DEFAULT_MONITOR_TIMER_AUTO_POST_MINUTES
    post_minutes = min(360, max(0, post_minutes))

    try:
        interval_minutes = int(cfg.get("monitor_timer_auto_interval_minutes", DEFAULT_MONITOR_TIMER_AUTO_INTERVAL_MINUTES))
    except (TypeError, ValueError):
        interval_minutes = DEFAULT_MONITOR_TIMER_AUTO_INTERVAL_MINUTES
    interval_minutes = min(60, max(1, interval_minutes))

    try:
        min_end_hour_utc = int(cfg.get("monitor_timer_auto_min_end_hour_utc", DEFAULT_MONITOR_TIMER_AUTO_MIN_END_HOUR_UTC))
    except (TypeError, ValueError):
        min_end_hour_utc = DEFAULT_MONITOR_TIMER_AUTO_MIN_END_HOUR_UTC
    min_end_hour_utc = min(23, max(0, min_end_hour_utc))

    return {
        "enabled": enabled,
        "pre_minutes": pre_minutes,
        "post_minutes": post_minutes,
        "interval_minutes": interval_minutes,
        "min_end_hour_utc": min_end_hour_utc,
        "default_enabled": DEFAULT_MONITOR_TIMER_AUTO_ENABLED,
        "default_pre_minutes": DEFAULT_MONITOR_TIMER_AUTO_PRE_MINUTES,
        "default_post_minutes": DEFAULT_MONITOR_TIMER_AUTO_POST_MINUTES,
        "default_interval_minutes": DEFAULT_MONITOR_TIMER_AUTO_INTERVAL_MINUTES,
        "default_min_end_hour_utc": DEFAULT_MONITOR_TIMER_AUTO_MIN_END_HOUR_UTC,
    }


def minute_spec_from_interval(interval_minutes):
    """Convert interval minutes to OnCalendar minute spec."""
    iv = min(60, max(1, int(interval_minutes or 1)))
    if iv == 1:
        return "*"
    if iv >= 60:
        return "0"
    return "0/{}".format(iv)


def hour_window_list(start_hour, end_hour):
    """Return contiguous circular hour list inclusive of start/end."""
    out = []
    h = int(start_hour) % 24
    target = int(end_hour) % 24
    for _ in range(24):
        out.append(h)
        if h == target:
            return out
        h = (h + 1) % 24
    return list(range(24))


def fetch_scoreboard_events_for_utc_date(date_utc):
    """Fetch ESPN scoreboard events for a UTC date."""
    dates_token = date_utc.strftime("%Y%m%d")
    url = "{}?dates={}".format(ESPN_SCOREBOARD_URL, dates_token)

    with urlopen(url, timeout=12) as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    events = payload.get("events")
    if not isinstance(events, list):
        return []
    return events


def fetch_scoreboard_events_for_utc_date_range(start_date_utc, end_date_utc=None):
    """Fetch and merge ESPN scoreboard events for an inclusive UTC date range."""
    start = start_date_utc
    end = end_date_utc or start_date_utc
    if end < start:
        start, end = end, start

    merged = {}
    current = start
    while current <= end:
        for event in fetch_scoreboard_events_for_utc_date(current):
            event_id = str((event or {}).get("id") or "").strip()
            if not event_id:
                continue
            merged[event_id] = event
        current += timedelta(days=1)

    return list(merged.values())


def fetch_upcoming_scoreboard_events(window_hours=24):
    """Fetch scheduled/pre-game scoreboard events starting within the next window."""
    now_utc = datetime.now(timezone.utc)
    deadline_utc = now_utc + timedelta(hours=max(1, int(window_hours or 24)))
    # Build a set covering every UTC date between now and deadline so intermediate
    # days are not skipped for windows longer than 24 hours.
    dates = set()
    current = now_utc.date()
    end = deadline_utc.date()
    while current <= end:
        dates.add(current)
        current += timedelta(days=1)

    merged = {}
    for date_utc in sorted(dates):
        for event in fetch_scoreboard_events_for_utc_date(date_utc):
            event_id = str((event or {}).get("id") or "").strip()
            if not event_id:
                continue
            merged[event_id] = event

    # Merge Summer League events when in SL window (#390) — NBA only, not WNBA
    if league_config.LEAGUE == "nba":
      try:
        with open(CONFIG_FILE, encoding="utf-8") as _cf:
            _cfg = json.load(_cf)
        _sl_enabled = _cfg.get("summer_league_enabled")
        if _sl_enabled is True or (_sl_enabled is None and _cfg.get("summer_league_window")):
            _sl_window = _cfg.get("summer_league_window", ["07-01", "07-25"])
            _today_md = now_utc.date().strftime("%m-%d")
            if len(_sl_window) == 2 and _sl_window[0] <= _today_md <= _sl_window[1]:
                _sl_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba-summer-las-vegas/scoreboard"
                with urlopen(_sl_url, timeout=12) as _sl_resp:
                    _sl_data = json.loads(_sl_resp.read().decode("utf-8"))
                for _sl_ev in _sl_data.get("events", []):
                    _sl_id = str(_sl_ev.get("id", "")).strip()
                    if _sl_id and _sl_id not in merged:
                        merged[_sl_id] = _sl_ev
      except Exception:
        pass  # SL merge is best-effort

    filtered = []
    for event in merged.values():
        state = str((((event or {}).get("status") or {}).get("type") or {}).get("state") or "").lower()
        if state != "pre":
            continue
        raw_date = (event.get("competitions") or [{}])[0].get("date") or event.get("date")
        event_dt = parse_iso_utc(raw_date)
        if event_dt is None:
            continue
        if event_dt < now_utc or event_dt > deadline_utc:
            continue
        filtered.append(event)

    filtered.sort(key=lambda event: parse_iso_utc(((event.get("competitions") or [{}])[0].get("date") or event.get("date"))) or now_utc)
    return filtered


def check_b2b(team_id, state, game_id, yesterday_events):
    """Return True if the team played a different game yesterday."""
    cache_key = "b2b_{}_{}".format(game_id, team_id)
    if cache_key in state:
        return bool(state[cache_key])
    try:
        for event in yesterday_events:
            if str(event.get("id", "")) == str(game_id):
                continue
            for comp in event.get("competitions", []):
                for competitor in comp.get("competitors", []):
                    comp_team = (competitor.get("team") or {}).get("id")
                    if str(comp_team or "") == str(team_id):
                        state[cache_key] = True
                        return True
        state[cache_key] = False
        return False
    except Exception:
        state[cache_key] = False
        return False


def parse_odds_favorite(odds_detail, competitors):
    """Parse ESPN odds detail like 'DEN -3' and return favorite team id."""
    if not odds_detail:
        return None
    parts = str(odds_detail).strip().split()
    if len(parts) < 2:
        return None
    abbr = parts[0].upper()
    try:
        spread = float(parts[1])
    except ValueError:
        return None
    if spread >= 0:
        return None
    for competitor in competitors:
        team = competitor.get("team") or {}
        if str(team.get("abbreviation") or "").upper() == abbr:
            return str(team.get("id") or "") or None
    return None


def is_monitored_team(config, display_name):
    """Return whether the display name matches monitored team config."""
    if not isinstance(config, dict):
        return False
    if config.get("monitor_all_teams"):
        return True
    name = str(display_name or "").lower()
    for configured in config.get("teams") or []:
        token = str(configured or "").strip()
        if not token:
            continue
        if token.lower() in name:
            return True
        parts = token.split()
        if parts and parts[-1].lower() in name:
            return True
    return False


def evaluate_pregame_team_conditions(config, event, competitor, opponent, state, yesterday_events, evanalytics_records):
    """Evaluate configured pre-game conditions and informational tags for one team."""
    team = competitor.get("team") or {}
    team_id = str(team.get("id") or "")
    team_name = str(team.get("displayName") or "")
    game_id = str(event.get("id") or "")
    main_comp = (event.get("competitions") or [{}])[0]
    competitors = main_comp.get("competitors") or []
    odds_list = main_comp.get("odds") or []
    odds_detail = (odds_list[0].get("details") or "") if odds_list else ""
    favorite_team_id = parse_odds_favorite(odds_detail, competitors)

    context = {
        "state": state,
        "game_id": game_id,
        "yesterday_events": yesterday_events,
        "check_b2b": check_b2b,
        "quarter_num": 0,
        "config": config,
        "main_comp": main_comp,
        "competitors": competitors,
        "parse_odds_favorite": parse_odds_favorite,
        "evanalytics": evanalytics_mod,
    }
    team_ctx = {
        "team_id": team_id,
        "team_name": team_name,
        "my_team": competitor,
        "opp": opponent,
    }

    conditions = []
    for condition in config.get("conditions") or []:
        if str(condition.get("type") or "") not in PRE_GAME_CONDITION_TYPES:
            continue
        result = conditions_mod.evaluate_condition(condition, team_ctx, context)
        if not result.get("matched"):
            continue
        conditions.append({
            "name": str(condition.get("name") or condition.get("type") or "Condition"),
            "type": str(condition.get("type") or ""),
            "detail": str(result.get("direction") or ""),
        })

    info_tags = []
    if favorite_team_id:
        if team_id == favorite_team_id:
            info_tags.append({"name": "Favorite", "type": "odds", "detail": odds_detail})
        else:
            home_away = str(competitor.get("homeAway") or "")
            label = "Home underdog" if home_away == "home" else "Underdog"
            info_tags.append({"name": label, "type": "odds", "detail": odds_detail})

    if evanalytics_records and team_name:
        record = evanalytics_mod.get_team_record(evanalytics_records, team_name)
    else:
        record = None
    l10_summary = None
    if record:
        role = str(competitor.get("homeAway") or "")
        wins = record.get("l10_{}_w".format(role))
        losses = record.get("l10_{}_l".format(role))
        other_role = "away" if role == "home" else ("home" if role == "away" else "")
        other_wins = record.get("l10_{}_w".format(other_role)) if other_role else None
        other_losses = record.get("l10_{}_l".format(other_role)) if other_role else None
        if wins is not None and losses is not None:
            l10_summary = {
                "role": role,
                "wins": wins,
                "losses": losses,
                "other_role": other_role,
                "other_wins": other_wins,
                "other_losses": other_losses,
            }

    return {
        "team_id": team_id,
        "name": team_name,
        "abbreviation": str(team.get("abbreviation") or "?"),
        "home_away": str(competitor.get("homeAway") or ""),
        "monitored": is_monitored_team(config, team_name),
        "conditions": conditions,
        "info_tags": info_tags,
        "l10_1h": l10_summary,
    }


def _do_upcoming_compute(config, upcoming_hours, cache_key):
    """Execute the full upcoming-games compute and write result to cache."""
    events = fetch_upcoming_scoreboard_events(window_hours=upcoming_hours)
    needs_b2b = any(str((condition or {}).get("type") or "") == "back_to_back" for condition in (config.get("conditions") or []))
    needs_h1 = any(str((condition or {}).get("type") or "") == "h1_moneyline_edge" for condition in (config.get("conditions") or []))

    # For B2B tagging, the relevant "prior day" is the day before each upcoming
    # game's own scoreboard date — NOT "yesterday relative to now". Using now-1
    # would false-positive any game >1 day away when the team played earlier
    # this week (issue #124).
    #
    # ESPN's scoreboard date = US local (ET) date of tip-off. A night-tip game
    # with raw UTC datetime on Sat 02:00Z is listed under the scoreboard date
    # Fri. Anchor to ET by subtracting 6h (covers both EST/EDT and late-PT tips)
    # before taking .date().
    prior_events_by_date = {}
    if needs_b2b:
        prior_dates_needed = set()
        for event in events:
            raw_date = ((event.get("competitions") or [{}])[0].get("date") or event.get("date"))
            event_dt = parse_iso_utc(raw_date)
            if event_dt is None:
                continue
            scoreboard_date = (event_dt - timedelta(hours=6)).date()
            prior_dates_needed.add(scoreboard_date - timedelta(days=1))
        for d in prior_dates_needed:
            try:
                prior_events_by_date[d] = fetch_scoreboard_events_for_utc_date(d)
            except Exception:
                prior_events_by_date[d] = []

    evanalytics_records = {}
    h1_source = "none"
    if needs_h1:
        _h1_st = h1_store.load_h1_records_store()
        if _h1_st.get("teams"):
            evanalytics_records = h1_store.build_all_evanalytics_records(_h1_st)
            h1_source = "h1_store"
        if not evanalytics_records:
            evanalytics_records = evanalytics_mod.fetch_1h_ml_records(config)
            h1_source = "evanalytics"
    evanalytics_diag = evanalytics_mod.get_cache_diagnostics() if needs_h1 else {
        "enabled": False,
        "reason": "h1_moneyline_edge_not_configured",
    }
    if needs_h1 and isinstance(evanalytics_diag, dict):
        evanalytics_diag["enabled"] = True
        evanalytics_diag["h1_source"] = h1_source
    state = {}
    games = []
    for event in events:
        main_comp = (event.get("competitions") or [{}])[0]
        competitors = main_comp.get("competitors") or []
        if len(competitors) < 2:
            continue
        home = next((c for c in competitors if c.get("homeAway") == "home"), competitors[-1])
        away = next((c for c in competitors if c.get("homeAway") == "away"), competitors[0])
        # Pick the day-prior events for THIS game (not a global yesterday list).
        yesterday_events = []
        if needs_b2b:
            raw_date = main_comp.get("date") or event.get("date")
            event_dt = parse_iso_utc(raw_date)
            if event_dt is not None:
                scoreboard_date = (event_dt - timedelta(hours=6)).date()
                yesterday_events = prior_events_by_date.get(scoreboard_date - timedelta(days=1), [])
        home_tags = evaluate_pregame_team_conditions(config, event, home, away, state, yesterday_events, evanalytics_records)
        away_tags = evaluate_pregame_team_conditions(config, event, away, home, state, yesterday_events, evanalytics_records)
        games.append({
            "id": str(event.get("id") or ""),
            "date": main_comp.get("date") or event.get("date"),
            "event": event,
            "monitored": bool(home_tags.get("monitored") or away_tags.get("monitored")),
            "home": home_tags,
            "away": away_tags,
        })
    result = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "window_hours": upcoming_hours,
        "evanalytics": evanalytics_diag,
        "count": len(games),
        "games": games,
    }
    with _upcoming_cache_lock:
        _upcoming_cache["key"] = cache_key
        _upcoming_cache["payload"] = result
        _upcoming_cache["ts"] = time.time()
    return result


def build_upcoming_games_payload(config):
    """Return upcoming games plus pre-game evaluable condition tags.

    Results are cached for UPCOMING_CACHE_TTL seconds to avoid hammering ESPN
    on every page load. The cache key includes upcoming_hours so config changes
    bust the cache.
    """
    upcoming_hours = max(1, min(72, int(config.get("upcoming_hours") or 24)))
    cache_key = upcoming_hours
    now_ts = time.time()
    with _upcoming_cache_lock:
        cache_hit  = (_upcoming_cache["key"] == cache_key and _upcoming_cache["payload"] is not None)
        cache_fresh = cache_hit and (now_ts - _upcoming_cache["ts"]) < UPCOMING_CACHE_TTL
        stale_payload = _upcoming_cache["payload"] if cache_hit else None

    if cache_fresh:
        return stale_payload

    # TTL expired but stale data exists — serve stale immediately, refresh in background
    if stale_payload is not None and _upcoming_cache["key"] == cache_key:
        can_spawn = _upcoming_compute_event.is_set()  # True = not already computing
        if can_spawn:
            _upcoming_compute_event.clear()  # claim the compute slot
            def _refresh_upcoming_bg(cfg=config, hrs=upcoming_hours, key=cache_key):
                try:
                    _do_upcoming_compute(cfg, hrs, key)
                finally:
                    _upcoming_compute_event.set()
            threading.Thread(target=_refresh_upcoming_bg, daemon=True).start()
        return stale_payload

    # Cache cold (no stale): serialise concurrent computes
    _upcoming_compute_event.wait()  # wait if another thread is computing
    with _upcoming_cache_lock:
        if (
            _upcoming_cache["key"] == cache_key
            and _upcoming_cache["payload"] is not None
            and (time.time() - _upcoming_cache["ts"]) < UPCOMING_CACHE_TTL
        ):
            return _upcoming_cache["payload"]
    _upcoming_compute_event.clear()  # signal "computing"
    try:
        return _do_upcoming_compute(config, upcoming_hours, cache_key)
    finally:
        _upcoming_compute_event.set()  # signal "done computing"


def build_auto_timer_schedule_preview(pre_minutes, post_minutes, interval_minutes,
                                      date_utc=None, min_end_hour_utc=None):
    """Build timer schedule preview from today's scheduled game times."""
    target_date = date_utc or datetime.now(timezone.utc).date()
    # Always fetch single-date scoreboard for the target date.
    # The dashboard's scoreboard cache merges yesterday+today events, which can
    # cause hour_window_list to compute an incorrect window when yesterday's
    # late-night tips dominate min(event_times).
    events = fetch_scoreboard_events_for_utc_date(target_date)

    event_times = []
    for event in events:
        dt = parse_iso_utc(event.get("date") if isinstance(event, dict) else None)
        if dt is not None:
            event_times.append(dt)

    if not event_times:
        return {
            "ok": False,
            "reason": "no_games",
            "date_utc": target_date.isoformat(),
            "event_count": 0,
            "on_calendar": "",
        }

    first_tip = min(event_times)
    last_tip = max(event_times)
    window_start = first_tip - timedelta(minutes=pre_minutes)
    window_end = last_tip + timedelta(minutes=post_minutes)

    # Apply minimum end-hour floor (#227).  Ensures the timer window never
    # ends before this UTC hour, even when post_minutes computes an earlier
    # boundary.  Prevents timer gaps that cause missed game-end events.
    if min_end_hour_utc is not None:
        end_h = window_end.hour
        floor_h = int(min_end_hour_utc) % 24
        # Compare circularly: if the computed end falls before the floor
        # within the same overnight window, extend to the floor hour.
        # e.g. computed end 05:30 (h=5), floor 8 → extend to 08:00
        start_h = window_start.hour
        def _hours_from_start(h):
            return (h - start_h) % 24
        if _hours_from_start(floor_h) > _hours_from_start(end_h):
            window_end = window_end.replace(hour=floor_h, minute=0, second=0)

    hours = hour_window_list(window_start.hour, window_end.hour)
    hour_spec = ",".join("{:02d}".format(h) for h in hours)
    minute_spec = minute_spec_from_interval(interval_minutes)
    on_calendar = "*-*-* {}:{}:00 UTC".format(hour_spec, minute_spec)

    return {
        "ok": True,
        "reason": "computed",
        "date_utc": target_date.isoformat(),
        "event_count": len(event_times),
        "first_tip_utc": first_tip.isoformat(),
        "last_tip_utc": last_tip.isoformat(),
        "window_start_utc": window_start.isoformat(),
        "window_end_utc": window_end.isoformat(),
        "on_calendar": on_calendar,
        "interval_minutes": int(interval_minutes),
    }


def read_monitor_timer_auto_state():
    """Read persisted monitor timer auto state."""
    if not os.path.exists(MONITOR_TIMER_AUTO_STATE_FILE):
        return {}
    try:
        with open(MONITOR_TIMER_AUTO_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_monitor_timer_auto_state(payload):
    """Persist monitor timer auto state atomically."""
    atomic_write_json(MONITOR_TIMER_AUTO_STATE_FILE, payload)


def get_monitor_timer_auto_payload(cfg=None):
    """Return current monitor timer auto settings and computed preview."""
    config = cfg if isinstance(cfg, dict) else {}
    settings = parse_monitor_timer_auto_settings(config)
    state = read_monitor_timer_auto_state()

    try:
        preview = build_auto_timer_schedule_preview(
            pre_minutes=settings["pre_minutes"],
            post_minutes=settings["post_minutes"],
            interval_minutes=settings["interval_minutes"],
            min_end_hour_utc=settings["min_end_hour_utc"],
        )
    except Exception as exc:
        preview = {
            "ok": False,
            "reason": "fetch_failed",
            "error": str(exc),
            "on_calendar": "",
            "event_count": 0,
            "date_utc": datetime.now(timezone.utc).date().isoformat(),
        }

    return {
        "settings": settings,
        "preview": preview,
        "state": state,
    }


def auto_update_monitor_timer_from_schedule(cfg, force=False, reason="worker"):
    """Auto-update monitor timer based on today's scheduled game times."""
    with MONITOR_TIMER_AUTO_LOCK:
        settings = parse_monitor_timer_auto_settings(cfg or {})
        if not settings["enabled"] and not force:
            return {
                "ok": False,
                "applied": False,
                "reason": "disabled",
                "settings": settings,
            }

        payload = get_monitor_timer_auto_payload(cfg or {})
        preview = payload.get("preview") or {}
        if not preview.get("ok"):
            return {
                "ok": False,
                "applied": False,
                "reason": str(preview.get("reason") or "preview_unavailable"),
                "settings": settings,
                "preview": preview,
            }

        desired_on_calendar = str(preview.get("on_calendar") or "").strip()
        if not desired_on_calendar:
            return {
                "ok": False,
                "applied": False,
                "reason": "empty_on_calendar",
                "settings": settings,
                "preview": preview,
            }

        current_timer = get_monitor_timer_payload()
        current_on_calendar = str(current_timer.get("on_calendar") or "").strip()
        if not force and desired_on_calendar == current_on_calendar:
            write_monitor_timer_auto_state({
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "applied": False,
                "status": "unchanged",
                "date_utc": preview.get("date_utc"),
                "event_count": preview.get("event_count"),
                "on_calendar": desired_on_calendar,
            })
            return {
                "ok": True,
                "applied": False,
                "reason": "unchanged",
                "settings": settings,
                "preview": preview,
                "timer": current_timer,
            }

        update_result, update_err = update_monitor_timer({
            "on_calendar": desired_on_calendar,
            "persistent": bool(current_timer.get("persistent", True)),
            "reload_and_restart": True,
        })

        if update_err:
            write_monitor_timer_auto_state({
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "applied": False,
                "status": "error",
                "date_utc": preview.get("date_utc"),
                "event_count": preview.get("event_count"),
                "on_calendar": desired_on_calendar,
                "error": update_err,
            })
            return {
                "ok": False,
                "applied": False,
                "reason": "update_failed",
                "error": update_err,
                "settings": settings,
                "preview": preview,
            }

        write_monitor_timer_auto_state({
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "reason": reason,
            "applied": True,
            "status": "updated",
            "date_utc": preview.get("date_utc"),
            "event_count": preview.get("event_count"),
            "on_calendar": desired_on_calendar,
        })
        return {
            "ok": True,
            "applied": True,
            "reason": "updated",
            "settings": settings,
            "preview": preview,
            "update": update_result,
            "timer": (update_result or {}).get("timer") if isinstance(update_result, dict) else get_monitor_timer_payload(),
        }


def monitor_timer_auto_worker():
    """Background worker to auto-tune monitor timer window from daily game schedule."""
    while True:
        try:
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg = json.load(f)
            auto_update_monitor_timer_from_schedule(cfg, force=False, reason="worker")
        except Exception as exc:
            _log("[MONITOR TIMER AUTO] worker error: {}".format(exc))
        time.sleep(MONITOR_TIMER_AUTO_WORKER_POLL_SECONDS)


def _log_rotation_worker():
    """Background worker that runs log rotation from the dashboard server.

    The monitor process only runs during game windows, so rotation configured
    for hours outside that window (to avoid splitting game session logs) would
    never fire.  The dashboard server runs 24/7, so this worker ensures
    rotation happens at the configured hour regardless of game window.
    """
    from monitor import rotate_logs
    while True:
        try:
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg = json.load(f)
            rotate_logs(cfg)
        except Exception as exc:
            _log("[LOG ROTATION] worker error: {}".format(exc))
        time.sleep(300)  # check every 5 minutes


def validate_config(cfg):
    """Return a list of validation error strings for config payload."""
    errors = []

    league = cfg.get("league", "nba")
    if league not in ("nba", "wnba"):
        errors.append("'league' must be one of: nba, wnba")

    conditions = cfg.get("conditions")
    teams = cfg.get("teams")
    compounds = cfg.get("compound_conditions", [])

    if not isinstance(conditions, list):
        errors.append("'conditions' must be an array")
        conditions = []
    if not isinstance(teams, list):
        errors.append("'teams' must be an array")

    cond_names = []
    for i, cond in enumerate(conditions):
        if not isinstance(cond, dict):
            errors.append("conditions[{}] must be an object".format(i))
            continue
        name = cond.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("conditions[{}].name must be a non-empty string".format(i))
            continue

        ctype = str(cond.get("type", "score_diff")).strip()
        if ctype not in CONDITION_TYPES:
            errors.append(
                "conditions[{}].type must be one of: {}".format(
                    i,
                    ", ".join(sorted(CONDITION_TYPES)),
                )
            )

        if ctype == "consecutive_points_run":
            threshold = cond.get("threshold")
            if threshold is None:
                errors.append("conditions[{}].threshold is required for consecutive_points_run".format(i))
            else:
                try:
                    th = int(threshold)
                    if th < 1:
                        errors.append("conditions[{}].threshold must be >= 1 for consecutive_points_run".format(i))
                except (TypeError, ValueError):
                    errors.append("conditions[{}].threshold must be an integer for consecutive_points_run".format(i))

            scope = str(cond.get("scope", "game")).strip().lower()
            if scope not in {"game", "quarter"}:
                errors.append("conditions[{}].scope must be 'game' or 'quarter' for consecutive_points_run".format(i))
            if scope == "quarter":
                q = cond.get("quarter")
                try:
                    qn = int(q)
                    if qn < 1 or qn > 5:
                        errors.append("conditions[{}].quarter must be between 1 and 5 when scope is 'quarter'".format(i))
                except (TypeError, ValueError):
                    errors.append("conditions[{}].quarter must be an integer when scope is 'quarter'".format(i))

        if ctype == "score_diff":
            quarter = cond.get("quarter")
            try:
                qn = int(quarter)
                if qn < 1 or qn > 5:
                    errors.append("conditions[{}].quarter must be between 1 and 5 for score_diff".format(i))
            except (TypeError, ValueError):
                errors.append("conditions[{}].quarter must be an integer for score_diff".format(i))

            team_status = str(cond.get("team_status", "")).strip().lower()
            if team_status not in {"winning", "trailing"}:
                errors.append("conditions[{}].team_status must be 'winning' or 'trailing' for score_diff".format(i))

            use_quarter_score = cond.get("use_quarter_score")
            if use_quarter_score is not None and not isinstance(use_quarter_score, bool):
                errors.append("conditions[{}].use_quarter_score must be true or false for score_diff".format(i))

            if team_status == "winning":
                threshold = cond.get("min_lead")
                if threshold is None:
                    errors.append("conditions[{}].min_lead is required when team_status is 'winning' for score_diff".format(i))
                else:
                    try:
                        if float(threshold) < 1:
                            errors.append("conditions[{}].min_lead must be >= 1 for score_diff".format(i))
                    except (TypeError, ValueError):
                        errors.append("conditions[{}].min_lead must be a number for score_diff".format(i))

            if team_status == "trailing":
                has_max = cond.get("max_deficit") is not None
                has_min = cond.get("min_deficit") is not None
                if has_max and has_min:
                    errors.append("conditions[{}] cannot set both max_deficit and min_deficit for score_diff".format(i))
                elif not has_max and not has_min:
                    errors.append("conditions[{}] must set either max_deficit or min_deficit when team_status is 'trailing' for score_diff".format(i))
                else:
                    threshold_key = "min_deficit" if has_min else "max_deficit"
                    threshold = cond.get(threshold_key)
                    try:
                        if float(threshold) < 1:
                            errors.append("conditions[{}].{} must be >= 1 for score_diff".format(i, threshold_key))
                    except (TypeError, ValueError):
                        errors.append("conditions[{}].{} must be a number for score_diff".format(i, threshold_key))

        # Validate alert_once if present
        alert_once = cond.get("alert_once")
        if alert_once is not None and alert_once not in ("game", "half", "quarter"):
            errors.append(
                "conditions[{}].alert_once must be one of: game, half, quarter (or omit for always)".format(i)
            )

        cond_names.append(name.strip())

    seen = set()
    dupes = set()
    for name in cond_names:
        if name in seen:
            dupes.add(name)
        seen.add(name)
    if dupes:
        errors.append("Duplicate condition names are not allowed: {}".format(
            ", ".join(sorted(dupes))
        ))

    if compounds is None:
        compounds = []
    if not isinstance(compounds, list):
        errors.append("'compound_conditions' must be an array")
        compounds = []

    cond_name_set = set(cond_names)
    for i, cc in enumerate(compounds):
        if not isinstance(cc, dict):
            errors.append("compound_conditions[{}] must be an object".format(i))
            continue
        cc_name = cc.get("name") if isinstance(cc.get("name"), str) and cc.get("name").strip() else "compound_conditions[{}]".format(i)
        refs = cc.get("condition_refs", [])
        if not isinstance(refs, list) or not refs:
            errors.append("{} must define a non-empty 'condition_refs' array".format(cc_name))
            continue

        bad_refs = [r for r in refs if not isinstance(r, str) or not r.strip() or r.strip() not in cond_name_set]
        if bad_refs:
            errors.append("{} has invalid condition_refs: {}".format(
                cc_name,
                ", ".join(str(r) for r in bad_refs)
            ))

    log_rotation = cfg.get("log_rotation")
    if log_rotation is not None:
        if not isinstance(log_rotation, dict):
            errors.append("'log_rotation' must be an object")
        else:
            enabled = log_rotation.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                errors.append("'log_rotation.enabled' must be true or false")

            period = log_rotation.get("period")
            period_value = str(period).lower() if period is not None else "daily"
            if period_value not in LOG_ROTATION_PERIODS:
                errors.append("'log_rotation.period' must be one of: daily, weekly, monthly, days")

            period_days = log_rotation.get("period_days")
            if period_value == "days":
                if period_days is None:
                    errors.append("'log_rotation.period_days' is required when period is 'days'")
                else:
                    try:
                        interval = int(period_days)
                        if interval < 1 or interval > 99:
                            errors.append("'log_rotation.period_days' must be between 1 and 99")
                    except (TypeError, ValueError):
                        errors.append("'log_rotation.period_days' must be an integer")
            elif period_days is not None:
                try:
                    interval = int(period_days)
                    if interval < 1 or interval > 99:
                        errors.append("'log_rotation.period_days' must be between 1 and 99")
                except (TypeError, ValueError):
                    errors.append("'log_rotation.period_days' must be an integer")

            rotation_hour_utc = log_rotation.get("rotation_hour_utc")
            if rotation_hour_utc is not None:
                try:
                    hour_val = int(rotation_hour_utc)
                    if hour_val < 0 or hour_val > 23:
                        errors.append("'log_rotation.rotation_hour_utc' must be between 0 and 23")
                except (TypeError, ValueError):
                    errors.append("'log_rotation.rotation_hour_utc' must be an integer")

            retention_days = log_rotation.get("retention_days")
            if retention_days is not None:
                try:
                    retention_int = int(retention_days)
                    if retention_int < 1:
                        errors.append("'log_rotation.retention_days' must be >= 1")
                except (TypeError, ValueError):
                    errors.append("'log_rotation.retention_days' must be an integer")

            files = log_rotation.get("files")
            if files is not None:
                if not isinstance(files, list) or not files:
                    errors.append("'log_rotation.files' must be a non-empty array")
                else:
                    normalized_files = []
                    seen_files = set()
                    duplicate_files = set()

                    for i, f in enumerate(files):
                        if not isinstance(f, str):
                            errors.append("'log_rotation.files[{}]' must be a string".format(i))
                            continue

                        err = validate_log_file_path(f)
                        if err:
                            errors.append("'log_rotation.files[{}]' {}".format(i, err))
                            continue

                        nf = normalize_log_file_path(f)
                        normalized_files.append(nf)
                        if nf in seen_files:
                            duplicate_files.add(nf)
                        seen_files.add(nf)

                    if duplicate_files:
                        errors.append(
                            "'log_rotation.files' contains duplicates: {}".format(
                                ", ".join(sorted(duplicate_files))
                            )
                        )

    # Validate postgame_summary_hours if present
    pgh = cfg.get("postgame_summary_hours")
    if pgh is not None:
        try:
            pgh_val = float(pgh)
            if pgh_val <= 0 or pgh_val > 48:
                errors.append("'postgame_summary_hours' must be between 0 and 48")
        except (TypeError, ValueError):
            errors.append("'postgame_summary_hours' must be a number")

    # Validate alert_history_hours if present (#157)
    ahh = cfg.get("alert_history_hours")
    if ahh is not None:
        try:
            ahh_val = float(ahh)
            if ahh_val < 1 or ahh_val > 168:
                errors.append("'alert_history_hours' must be between 1 and 168")
        except (TypeError, ValueError):
            errors.append("'alert_history_hours' must be a number")

    odl = cfg.get("outcomes_decay_lambda")
    if odl is not None:
        try:
            odl_val = float(odl)
            if odl_val < 0 or odl_val > 1:
                errors.append("'outcomes_decay_lambda' must be between 0 and 1")
        except (TypeError, ValueError):
            errors.append("'outcomes_decay_lambda' must be a number")

    capt = cfg.get("condition_alert_probability_threshold")
    if capt is not None:
        try:
            capt_val = float(capt)
            if capt_val < 50 or capt_val > 100:
                errors.append("'condition_alert_probability_threshold' must be between 50 and 100")
        except (TypeError, ValueError):
            errors.append("'condition_alert_probability_threshold' must be a number")

    me_step = cfg.get("model_eval_step_days")
    if me_step is not None:
        try:
            step_val = int(me_step)
            if step_val < 1 or step_val > 30:
                errors.append("'model_eval_step_days' must be between 1 and 30")
        except (TypeError, ValueError):
            errors.append("'model_eval_step_days' must be an integer")

    mes_enabled = cfg.get("model_eval_snapshot_enabled")
    if mes_enabled is not None and not isinstance(mes_enabled, bool):
        errors.append("'model_eval_snapshot_enabled' must be true or false")

    mes_interval = cfg.get("model_eval_snapshot_interval_minutes")
    if mes_interval is not None:
        try:
            interval_val = int(mes_interval)
            if interval_val < 15 or interval_val > 10080:
                errors.append("'model_eval_snapshot_interval_minutes' must be between 15 and 10080")
        except (TypeError, ValueError):
            errors.append("'model_eval_snapshot_interval_minutes' must be an integer")

    ls_max_age = cfg.get("live_stats_max_age_minutes")
    if ls_max_age is not None:
        try:
            ls_max_age_val = int(ls_max_age)
            if ls_max_age_val < 5 or ls_max_age_val > 120:
                errors.append("'live_stats_max_age_minutes' must be between 5 and 120")
        except (TypeError, ValueError):
            errors.append("'live_stats_max_age_minutes' must be an integer")

    mt_enabled = cfg.get("monitor_timer_auto_from_schedule_enabled")
    if mt_enabled is not None and not isinstance(mt_enabled, bool):
        errors.append("'monitor_timer_auto_from_schedule_enabled' must be true or false")

    mt_pre = cfg.get("monitor_timer_auto_pre_minutes")
    if mt_pre is not None:
        try:
            mt_pre_val = int(mt_pre)
            if mt_pre_val < 0 or mt_pre_val > 360:
                errors.append("'monitor_timer_auto_pre_minutes' must be between 0 and 360")
        except (TypeError, ValueError):
            errors.append("'monitor_timer_auto_pre_minutes' must be an integer")

    mt_post = cfg.get("monitor_timer_auto_post_minutes")
    if mt_post is not None:
        try:
            mt_post_val = int(mt_post)
            if mt_post_val < 0 or mt_post_val > 360:
                errors.append("'monitor_timer_auto_post_minutes' must be between 0 and 360")
        except (TypeError, ValueError):
            errors.append("'monitor_timer_auto_post_minutes' must be an integer")

    mt_interval = cfg.get("monitor_timer_auto_interval_minutes")
    if mt_interval is not None:
        try:
            mt_interval_val = int(mt_interval)
            if mt_interval_val < 1 or mt_interval_val > 60:
                errors.append("'monitor_timer_auto_interval_minutes' must be between 1 and 60")
        except (TypeError, ValueError):
            errors.append("'monitor_timer_auto_interval_minutes' must be an integer")

    mt_min_end = cfg.get("monitor_timer_auto_min_end_hour_utc")
    if mt_min_end is not None:
        try:
            mt_min_end_val = int(mt_min_end)
            if mt_min_end_val < 0 or mt_min_end_val > 23:
                errors.append("'monitor_timer_auto_min_end_hour_utc' must be between 0 and 23")
        except (TypeError, ValueError):
            errors.append("'monitor_timer_auto_min_end_hour_utc' must be an integer")

    # Validate postgame_outcome_criteria if present
    poc = cfg.get("postgame_outcome_criteria")
    if poc is not None:
        if not isinstance(poc, dict):
            errors.append("'postgame_outcome_criteria' must be an object")
        else:
            cm = poc.get("close_margin")
            if cm is not None:
                try:
                    cmv = int(cm)
                    if cmv < 1 or cmv > 30:
                        errors.append("'postgame_outcome_criteria.close_margin' must be between 1 and 30")
                except (TypeError, ValueError):
                    errors.append("'postgame_outcome_criteria.close_margin' must be an integer")

            cd = poc.get("comeback_deficit")
            if cd is not None:
                try:
                    cdv = int(cd)
                    if cdv < 1 or cdv > 40:
                        errors.append("'postgame_outcome_criteria.comeback_deficit' must be between 1 and 40")
                except (TypeError, ValueError):
                    errors.append("'postgame_outcome_criteria.comeback_deficit' must be an integer")

            cwu = poc.get("close_when_upset")
            if cwu is not None and not isinstance(cwu, bool):
                errors.append("'postgame_outcome_criteria.close_when_upset' must be true or false")

            req_pbp = poc.get("require_q4_pbp_for_comeback")
            if req_pbp is not None and not isinstance(req_pbp, bool):
                errors.append("'postgame_outcome_criteria.require_q4_pbp_for_comeback' must be true or false")

            order = poc.get("outcome_order")
            if order is not None:
                if not isinstance(order, list) or len(order) != 3:
                    errors.append("'postgame_outcome_criteria.outcome_order' must be an array of 3 values")
                else:
                    allowed = {"UPSET", "CLOSE", "COMEBACK"}
                    cleaned = [str(x).strip().upper() for x in order]
                    bad = [x for x in cleaned if x not in allowed]
                    if bad:
                        errors.append("'postgame_outcome_criteria.outcome_order' contains invalid values: {}".format(", ".join(bad)))
                    if len(set(cleaned)) != 3:
                        errors.append("'postgame_outcome_criteria.outcome_order' must not contain duplicates")

            tags = poc.get("tags")
            if tags is not None:
                if not isinstance(tags, list):
                    errors.append("'postgame_outcome_criteria.tags' must be an array")
                else:
                    allowed_kinds = {"upset", "close_margin_lte", "comeback_q4", "margin_gte"}
                    allowed_colors = {"red", "orange", "yellow", "green", "blue", "muted"}
                    seen_keys = set()
                    for i, tag in enumerate(tags):
                        path = "'postgame_outcome_criteria.tags[{}]'".format(i)
                        if not isinstance(tag, dict):
                            errors.append("{} must be an object".format(path))
                            continue

                        key = str(tag.get("key") or "").strip().upper()
                        if not key:
                            errors.append("{}.key is required".format(path))
                        else:
                            if len(key) > 32:
                                errors.append("{}.key must be <= 32 chars".format(path))
                            if not all(ch.isalnum() or ch == "_" for ch in key):
                                errors.append("{}.key must contain only A-Z, 0-9, and _".format(path))
                            if key in seen_keys:
                                errors.append("{}.key duplicates an earlier tag key".format(path))
                            seen_keys.add(key)

                        kind = str(tag.get("kind") or "").strip().lower()
                        if kind not in allowed_kinds:
                            errors.append("{}.kind must be one of: {}".format(path, ", ".join(sorted(allowed_kinds))))

                        label = tag.get("label")
                        if label is not None and not isinstance(label, str):
                            errors.append("{}.label must be a string".format(path))

                        emoji = tag.get("emoji")
                        if emoji is not None and not isinstance(emoji, str):
                            errors.append("{}.emoji must be a string".format(path))

                        color = tag.get("color")
                        if color is not None:
                            cval = str(color).strip().lower()
                            if cval not in allowed_colors:
                                errors.append("{}.color must be one of: {}".format(path, ", ".join(sorted(allowed_colors))))

                        enabled = tag.get("enabled")
                        if enabled is not None and not isinstance(enabled, bool):
                            errors.append("{}.enabled must be true or false".format(path))

                        if kind in ("close_margin_lte", "comeback_q4", "margin_gte"):
                            threshold = tag.get("threshold")
                            if threshold is None:
                                errors.append("{}.threshold is required for kind '{}'".format(path, kind))
                            else:
                                try:
                                    tval = int(threshold)
                                    if kind == "close_margin_lte" and (tval < 1 or tval > 30):
                                        errors.append("{}.threshold must be between 1 and 30".format(path))
                                    elif kind == "comeback_q4" and (tval < 1 or tval > 40):
                                        errors.append("{}.threshold must be between 1 and 40".format(path))
                                    elif kind == "margin_gte" and (tval < 1 or tval > 80):
                                        errors.append("{}.threshold must be between 1 and 80".format(path))
                                except (TypeError, ValueError):
                                    errors.append("{}.threshold must be an integer".format(path))

                        suppress = tag.get("suppress_if_tags")
                        if suppress is not None:
                            if not isinstance(suppress, list):
                                errors.append("{}.suppress_if_tags must be an array".format(path))
                            else:
                                bad = [x for x in suppress if not isinstance(x, str) or not str(x).strip()]
                                if bad:
                                    errors.append("{}.suppress_if_tags must contain non-empty strings".format(path))

    # ML training settings
    ml_enabled = cfg.get("ml_training_enabled")
    if ml_enabled is not None and not isinstance(ml_enabled, bool):
        errors.append("'ml_training_enabled' must be true or false")

    ml_interval = cfg.get("ml_training_interval_days")
    if ml_interval is not None:
        try:
            iv = int(ml_interval)
            if iv < 1 or iv > 90:
                errors.append("'ml_training_interval_days' must be between 1 and 90")
        except (TypeError, ValueError):
            errors.append("'ml_training_interval_days' must be an integer")

    ml_schedule = cfg.get("ml_training_schedule_utc")
    if ml_schedule is not None:
        import re as _re
        if not isinstance(ml_schedule, str) or not _re.match(r'^\d{1,2}:\d{2}$', ml_schedule):
            errors.append("'ml_training_schedule_utc' must be in HH:MM format")
        else:
            parts = ml_schedule.split(":")
            try:
                h, m = int(parts[0]), int(parts[1])
                if h < 0 or h > 23 or m < 0 or m > 59:
                    errors.append("'ml_training_schedule_utc' must be a valid time (00:00-23:59)")
            except ValueError:
                errors.append("'ml_training_schedule_utc' must be a valid time")

    ml_retention = cfg.get("ml_training_retention_count")
    if ml_retention is not None:
        try:
            rc = int(ml_retention)
            if rc < 1 or rc > 100:
                errors.append("'ml_training_retention_count' must be between 1 and 100")
        except (TypeError, ValueError):
            errors.append("'ml_training_retention_count' must be an integer")

    # Validate pw_alert_filters (#418)
    paf = cfg.get("pw_alert_filters")
    if paf is not None:
        if not isinstance(paf, dict):
            errors.append("'pw_alert_filters' must be an object")
        else:
            _paf_valid_quarters = {"", "Q1", "Q2", "Q3", "Q4", "OT"}
            _paf_valid_roles = {"", "favorite", "underdog"}
            _paf_valid_margins = {"", "leading", "trailing"}
            _paf_valid_halves = {"", "H1", "H2", "OT"}
            _paf_valid_pols = {"", "positive", "negative", "neutral"}
            _paf_valid_cons = {"", "strong", "conflicted", "ml_only", "historical_only"}
            _paf_valid_srcs = {"", "live_only"}
            if paf.get("quarter", "") not in _paf_valid_quarters:
                errors.append("'pw_alert_filters.quarter' must be one of: " + ", ".join(sorted(_paf_valid_quarters)))
            if paf.get("spread_role", "") not in _paf_valid_roles:
                errors.append("'pw_alert_filters.spread_role' must be one of: " + ", ".join(sorted(_paf_valid_roles)))
            if paf.get("margin_at_fire", "") not in _paf_valid_margins:
                errors.append("'pw_alert_filters.margin_at_fire' must be one of: " + ", ".join(sorted(_paf_valid_margins)))
            if paf.get("half", "") not in _paf_valid_halves:
                errors.append("'pw_alert_filters.half' must be one of: " + ", ".join(sorted(_paf_valid_halves)))
            if paf.get("polarity", "") not in _paf_valid_pols:
                errors.append("'pw_alert_filters.polarity' must be one of: " + ", ".join(sorted(_paf_valid_pols)))
            if paf.get("consensus", "") not in _paf_valid_cons:
                errors.append("'pw_alert_filters.consensus' must be one of: " + ", ".join(sorted(_paf_valid_cons)))
            if paf.get("source", "") not in _paf_valid_srcs:
                errors.append("'pw_alert_filters.source' must be one of: " + ", ".join(sorted(_paf_valid_srcs)))
            for _nf in ("min_pct", "edge_min", "edge_max"):
                _nv = paf.get(_nf)
                if _nv is not None:
                    try:
                        float(_nv)
                    except (TypeError, ValueError):
                        errors.append("'pw_alert_filters.{}' must be a number".format(_nf))

    return errors


def _param_first(params, key, default=""):
    """Return first query-string value for key."""
    values = params.get(key) or []
    if not values:
        return default
    return values[0]


def _filter_strict_bk(records):
    """Keep only calls with genuine BK odds at fire time (#412 Phase 4.1).

    Requires bk_ml_source (set only by monitor.py from a live Odds API quote).
    Excludes backfill-priced calls AND live calls without BK capture.

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
    return filtered, had_synthetic


def _parse_int_param(raw, default, minimum, maximum):
    """Parse bounded integer query parameter with fallback."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return min(maximum, max(minimum, value))


def _parse_iso_utc_or_none(raw):
    """Parse ISO-like datetime to UTC, returning None on failure."""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# In-memory alert-state cache — avoids re-parsing .alert_state.json every
# 15s for /api/pw-calls-live.  Invalidated on mtime change (#423).
# ---------------------------------------------------------------------------
_alert_state_cache_lock = threading.Lock()
_alert_state_cache = {"mtime": None, "data": None}


def _load_alert_state_cached():
    """Return alert state dict, using an mtime-keyed in-memory cache."""
    state_file = league_config.state_path(".alert_state.json")
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


# ---------------------------------------------------------------------------
# In-memory history cache — avoids re-parsing game_history.json on every
# API request. Cache is invalidated when the file's mtime changes (i.e. a
# new live game is appended or backfill writes). Thread-safe via a lock.
# ---------------------------------------------------------------------------
_history_cache_lock = threading.Lock()
_history_cache = {"mtime": None, "records": None}

# Model-eval cache — rolling_origin is expensive (~47s for 5 targets × 2500 records).
# Keyed on (mtime, min_train, frozenset(targets)) — invalidates only when history changes.
# Persisted to MODEL_EVAL_CACHE_FILE so the cache survives server restarts.
# _model_eval_compute_event: threading.Event that is cleared while a recompute is in
# progress, so concurrent threads wait on it rather than all starting their own recompute.
_model_eval_cache_lock = threading.Lock()
_model_eval_cache = {"key": None, "payload": None}
_model_eval_compute_event = threading.Event()
_model_eval_compute_event.set()  # initially "not computing"
MODEL_EVAL_CACHE_FILE = league_config.state_path("model_eval_cache.json")


# ML training state — tracks background training status for the dashboard
_ml_train_lock = threading.Lock()
_ml_train_state = {"status": "idle", "started_at": None, "finished_at": None, "duration_s": None, "error": None}
ML_TRAINING_HISTORY_FILE = league_config.state_path("ml_training_history.json")
ML_TRAIN_LOCK_FILE = os.path.join(SCRIPT_DIR, "ml_train.lock")

# Upcoming games cache — short TTL (60s) to avoid hammering ESPN on every page load
_upcoming_cache_lock = threading.Lock()
_upcoming_cache = {"key": None, "payload": None, "ts": 0.0}
_upcoming_compute_event = threading.Event()
_upcoming_compute_event.set()  # initially "not computing"
UPCOMING_CACHE_TTL = 300  # seconds (5 min) — upcoming games don't change rapidly; must exceed worst-case compute time

# Live scoreboard proxy cache — 20s TTL; browser hits /api/scoreboard instead of ESPN directly
_scoreboard_cache_lock = threading.Lock()
_scoreboard_cache = {"payload": None, "ts": 0.0}
SCOREBOARD_CACHE_TTL = 20  # seconds

# Cloud backup subprocess guard (#403)
_cloud_backup_proc = None
def _set_cloud_backup_proc(proc):
    global _cloud_backup_proc
    _cloud_backup_proc = proc

# Summer League odds monitor instance (#384)
_sl_odds_monitor = None

def _get_sl_monitor():
    """Get or create the Summer League odds monitor singleton."""
    global _sl_odds_monitor
    if _sl_odds_monitor is None:
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                config = json.load(f)
        except Exception:
            config = {}
        _sl_odds_monitor = sl_odds_mod.SummerLeagueOddsMonitor(config)
    return _sl_odds_monitor


def _load_history_cached():
    """Return game history records, using an mtime-keyed in-memory cache."""
    history_path = outcomes_mod.HISTORY_FILE
    try:
        mtime = os.path.getmtime(history_path)
    except OSError:
        return []

    with _history_cache_lock:
        if _history_cache["mtime"] == mtime and _history_cache["records"] is not None:
            return list(_history_cache["records"])

    # Load outside the lock to avoid blocking other requests during IO
    records = outcomes_mod.load_history()

    with _history_cache_lock:
        _history_cache["mtime"] = mtime
        _history_cache["records"] = records

    return records


def _history_record_dt(rec):
    """Return best-effort datetime for history record sorting/filtering.

    Always prefer the game date ('date' field) over the write timestamp
    ('record_updated_at'). For backfill records, record_updated_at is the
    backfill run time — not the game date — and must not be used for
    date-range filtering or chronological sorting.
    """
    if not isinstance(rec, dict):
        return None
    return (
        _parse_iso_utc_or_none(rec.get("date"))
        or _parse_iso_utc_or_none(rec.get("record_updated_at"))
    )


def _history_outcome_pills(rec):
    """Return merged, de-duplicated outcome/tag pills for a history record."""
    if not isinstance(rec, dict):
        return []

    pills = []
    seen = set()

    legacy_flags = [
        ("was_upset", "UPSET"),
        ("was_close", "CLOSE"),
        ("was_comeback", "COMEBACK"),
        ("was_blowout", "BLOWOUT"),
    ]
    for field, label in legacy_flags:
        if rec.get(field) and label not in seen:
            seen.add(label)
            pills.append(label)

    raw_tags = rec.get("postgame_tags_matched")
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            label = str(tag or "").strip().upper()
            if not label or label in seen:
                continue
            seen.add(label)
            pills.append(label)

    return pills


_UTC8_OFFSET = timedelta(hours=8)
_SEASON_START_MONTH = {"nba": 10, "wnba": 5}


def _game_season_year(rec, league=None):
    """Return season key from a game record's UTC date.

    NBA: cross-year format e.g. '2025-26' (Oct–Jun).
    WNBA: single-year format e.g. '2025' (May–Oct).
    UTC-8 normalisation mirrors the B2B cache logic.
    """
    if league is None:
        league = league_config.LEAGUE
    start_month = _SEASON_START_MONTH.get(league, 10)
    try:
        dt = datetime.fromisoformat(str(rec.get("date") or "").replace("Z", "+00:00"))
        us_date = (dt - _UTC8_OFFSET).date()
        y = us_date.year if us_date.month >= start_month else us_date.year - 1
        if league == "wnba":
            return str(y)
        return "{}-{}".format(y, str(y + 1)[-2:])
    except Exception:
        return ""


def _history_matches_filters(rec, filters):
    """Return whether history record matches requested filters."""
    if not isinstance(rec, dict):
        return False

    team_fields = [
        rec.get("away_name"),
        rec.get("home_name"),
        rec.get("away_abbr"),
        rec.get("home_abbr"),
    ]
    haystack = " ".join(str(x or "") for x in team_fields).lower()

    team_set = [str(t or "").strip().lower() for t in (filters.get("team_set") or []) if str(t or "").strip()]
    # Team precedence rule:
    # - when team_set is non-empty, ignore scalar team and apply OR semantics across team_set
    # - when team_set is empty, fall back to scalar team behavior
    if team_set:
        if not any(team_q in haystack for team_q in team_set):
            return False
    else:
        team_q = filters.get("team")
        if team_q and team_q not in haystack:
            return False

    cond_q = filters.get("condition")
    if cond_q:
        matched = False
        for cond in rec.get("conditions_fired") or []:
            if not isinstance(cond, dict):
                continue
            cond_text = " ".join(
                str(cond.get(k) or "")
                for k in ("name", "type", "team", "quarter_str", "direction")
            ).lower()
            if cond_q in cond_text:
                matched = True
                break
        if not matched:
            return False

    cond_set = filters.get("condition_set")
    if cond_set:
        if len(cond_set) > 1:
            # For combinations: require that a single team fired ALL conditions,
            # matching the same-team grouping used in compute_combination_stats.
            by_team = {}
            for cond in (rec.get("conditions_fired") or []):
                if not isinstance(cond, dict):
                    continue
                name = cond.get("name")
                raw_tid = cond.get("team_id")
                tid = str(raw_tid).strip() if raw_tid is not None else ""
                if name and tid:
                    if tid not in by_team:
                        by_team[tid] = set()
                    by_team[tid].add(name)
            required = set(cond_set)
            if by_team:
                if not any(required <= team_names for team_names in by_team.values()):
                    return False
            else:
                # Fallback for legacy records without team_id: require all conditions present anywhere.
                fired_names = {
                    cond.get("name")
                    for cond in (rec.get("conditions_fired") or [])
                    if isinstance(cond, dict)
                }
                for required_name in cond_set:
                    if required_name not in fired_names:
                        return False
        else:
            fired_names = {
                cond.get("name")
                for cond in (rec.get("conditions_fired") or [])
                if isinstance(cond, dict)
            }
            for required_name in cond_set:
                if required_name not in fired_names:
                    return False

    tag_set = [str(t or "").strip().upper() for t in (filters.get("tag_set") or []) if str(t or "").strip()]
    # Tag precedence rule:
    # - when tag_set is non-empty, ignore scalar tag and apply OR semantics across tag_set
    # - when tag_set is empty, fall back to scalar tag behavior
    if tag_set:
        pills = {str(p or "").strip().upper() for p in _history_outcome_pills(rec)}
        if not any(tag in pills for tag in tag_set):
            return False
    else:
        tag_q = str(filters.get("tag") or "").strip().upper()
        if tag_q:
            pills = {str(p or "").strip().upper() for p in _history_outcome_pills(rec)}
            if tag_q not in pills:
                return False

    search_q = filters.get("q")
    if search_q:
        search_chunks = [
            rec.get("game_id"),
            rec.get("away_name"),
            rec.get("home_name"),
            rec.get("away_abbr"),
            rec.get("home_abbr"),
            rec.get("spread_detail"),
        ]
        for cond in rec.get("conditions_fired") or []:
            if not isinstance(cond, dict):
                continue
            search_chunks.extend([
                cond.get("name"), cond.get("type"), cond.get("team"), cond.get("direction")
            ])
        haystack = " ".join(str(x or "") for x in search_chunks).lower()
        if search_q not in haystack:
            return False

    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if date_from or date_to:
        rec_dt = _history_record_dt(rec)
        if rec_dt is None:
            return False
        if date_from and rec_dt < date_from:
            return False
        if date_to and rec_dt > date_to:
            return False

    season_year = filters.get("season_year", "")
    if season_year and season_year != "all":
        if _game_season_year(rec) != season_year:
            return False

    seg_filter = filters.get("season_segment", "")
    if seg_filter and seg_filter != "all":
        rec_seg = rec.get("season_segment", "")
        if seg_filter == "regular_season":
            if rec_seg not in ("pre_allstar", "post_allstar"):
                return False
        elif seg_filter == "playoffs_finals":
            if rec_seg not in ("playoffs", "finals"):
                return False
        else:
            if rec_seg != seg_filter:
                return False

    # Phase 2: outcome_set — AND-semantics multi-outcome filter
    outcome_set = filters.get("outcome_set") or []
    if outcome_set:
        pills = {str(p or "").strip().lower() for p in _history_outcome_pills(rec)}
        for required_outcome in outcome_set:
            if required_outcome not in pills:
                return False

    # Condition-entry-level filters: margin_when_fired, margin_min/max, team_role, spread_role, team_won
    # All active predicates must be satisfied by the SAME condition entry — i.e. the team
    # that triggered the condition must also be the team that won (and/or had the matching
    # margin/role).  This prevents, e.g., team A's losing margin satisfying margin_when_fired
    # while team B's win satisfies team_won.
    margin_filter = filters.get("margin_when_fired", "")
    margin_min = filters.get("margin_min")
    margin_max = filters.get("margin_max")
    team_role = filters.get("team_role", "")
    spread_role = filters.get("spread_role", "")
    team_won_filter = filters.get("team_won", "")

    has_cond_level_filter = bool(
        margin_filter or margin_min is not None or margin_max is not None
        or team_role or spread_role or team_won_filter
    )
    if has_cond_level_filter:
        active_condition_set = filters.get("condition_set")
        conds_to_check = rec.get("conditions_fired") or []
        if active_condition_set:
            conds_to_check = [c for c in conds_to_check
                              if isinstance(c, dict) and c.get("name") in active_condition_set]

        def _cond_entry_ok(c):
            if not isinstance(c, dict):
                return False
            # margin_when_fired (positive / negative / zero)
            if margin_filter:
                val = c.get("score_margin_at_fire")
                if val is None:
                    return False
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return False
                if margin_filter == "positive" and v <= 0:
                    return False
                if margin_filter == "negative" and v >= 0:
                    return False
                if margin_filter == "zero" and v != 0:
                    return False
            # margin_min / margin_max bounds
            if margin_min is not None or margin_max is not None:
                val = c.get("score_margin_at_fire")
                if val is None:
                    return False
                try:
                    v = float(val)
                except (TypeError, ValueError):
                    return False
                if margin_min is not None and v < margin_min:
                    return False
                if margin_max is not None and v > margin_max:
                    return False
            # team_role (home / away)
            if team_role:
                if str(c.get("home_away_role") or "").lower() != team_role:
                    return False
            # spread_role (favorite / underdog) — compare team_id vs spread_fav_id
            if spread_role:
                tid = str(c.get("team_id") or "").strip()
                fav = str(rec.get("spread_fav_id") or "").strip()
                if not tid or not fav:
                    return False
                is_fav = (tid == fav)
                if spread_role == "favorite" and not is_fav:
                    return False
                if spread_role == "underdog" and is_fav:
                    return False
            # team_won — a missing (None) team_won means the outcome is unknown and
            # must not match either filter direction (symmetric with team_won=true which
            # already excluded None via bool(None) != True; team_won=false previously
            # incorrectly included None because bool(None)==False==want_win).
            if team_won_filter:
                want_win = (team_won_filter == "true")
                raw_won = c.get("team_won")
                if raw_won is None:
                    return False
                if bool(raw_won) != want_win:
                    return False
            return True

        if not any(_cond_entry_ok(c) for c in conds_to_check):
            return False

        # Same-team AND check for multi-condition combos (#309, port from NRL #140)
        if active_condition_set and len(active_condition_set) > 1:
            _by_team_combo = {}
            for c in conds_to_check:
                if not isinstance(c, dict) or not _cond_entry_ok(c):
                    continue
                _tid = str(c.get("team_id") or c.get("team") or "").strip()
                _cname = c.get("name", "")
                if _tid and _cname:
                    _by_team_combo.setdefault(_tid, set()).add(_cname)
            if not any(t_conds >= set(active_condition_set) for t_conds in _by_team_combo.values()):
                return False

        # Issue #98: for single-condition drill-downs with team_won=false, the any() check
        # above can be satisfied by a losing team while a *different* team that also fired
        # the same condition happened to win.  Enforce that no condition entry with
        # team_won=True exists when the filter requires a loss (single condition only;
        # the multi-condition combo check below handles the >1 case separately).
        if team_won_filter == "false" and active_condition_set and len(active_condition_set) == 1:
            if any(
                isinstance(c, dict) and c.get("team_won") is not None and bool(c.get("team_won"))
                for c in conds_to_check
            ):
                return False

        # For combo condition sets (>1 conditions) with team_won filter: the any() check
        # above can be satisfied by a different team that only fired a subset of the combo
        # conditions and happened to win.  Enforce that the team which fired ALL combo
        # conditions is the one that satisfies team_won.
        if team_won_filter and active_condition_set and len(active_condition_set) > 1:
            want_win = (team_won_filter == "true")
            required = set(active_condition_set)
            by_team = {}  # tid -> {cond_name -> team_won_value}
            for c in conds_to_check:
                if not isinstance(c, dict):
                    continue
                name = c.get("name")
                raw_tid = c.get("team_id")
                tid = str(raw_tid).strip() if raw_tid is not None else ""
                if not tid:
                    tid = str(c.get("team") or "").strip()
                raw_won = c.get("team_won")
                # Skip entries with unknown team_won to avoid legacy None values
                # being misread as False and incorrectly satisfying team_won=false.
                if name and tid and raw_won is not None:
                    if tid not in by_team:
                        by_team[tid] = {}
                    by_team[tid][name] = bool(raw_won)

            def _team_satisfies_combo_filter(cond_map):
                """Return True if this team fired all required conditions with matching team_won."""
                return set(cond_map.keys()) >= required and all(
                    v == want_win for v in cond_map.values()
                )

            # Only apply the stricter check when team identifiers are present.
            # For legacy records with no team_id/team, the any() check above is best-effort.
            if by_team and not any(_team_satisfies_combo_filter(cm) for cm in by_team.values()):
                return False

    # Phase 2: data_source — provenance filter via 'source' field
    data_source = filters.get("data_source", "")
    if data_source:
        rec_source = str(rec.get("source") or "").strip().lower()
        if rec_source != data_source:
            return False

    # PW drill-down filters — game must have ≥1 resolved pw_call matching all provided PW filters
    # Filter-then-select: match calls against filters first, then apply call_selection (#135 port)
    pw_band = filters.get("pw_band", "")
    _fc_raw = filters.get("pw_consensus", "")
    pw_consensus = _fc_raw if isinstance(_fc_raw, set) else (set(c.strip() for c in _fc_raw.split(",") if c.strip()) if _fc_raw else set())
    pw_quarter = filters.get("pw_quarter", "")
    pw_spread_role = filters.get("pw_spread_role", "")
    pw_margin = filters.get("pw_margin", "")
    pw_half = filters.get("pw_half", "")
    pw_call_selection = filters.get("pw_call_selection", "")
    pw_edge = filters.get("pw_edge", "")
    pw_strict_bk = filters.get("pw_strict_bk", False)
    if pw_band or pw_consensus or pw_quarter or pw_spread_role or pw_margin or pw_half or pw_call_selection or pw_edge or pw_strict_bk:
        calls = [c for c in (rec.get("predicted_winner_calls") or []) if c.get("correct") is not None]
        band_min = int(pw_band) if pw_band else None
        band_max = (band_min + 10) if (band_min is not None and band_min < 90) else 200
        pw_fav = str(rec.get("spread_fav_id") or "").strip()
        _pw_matched = []
        for c in calls:
            # Live-priced filter (#412 Phase 4.1)
            if pw_strict_bk:
                if c.get("synthetic") is True:
                    continue
                if c.get("synthetic") is None and not c.get("bk_ml_source"):
                    continue
            if band_min is not None and not (band_min <= float(c.get("pct") or 0) < band_max):
                continue
            if pw_consensus and str(c.get("consensus") or "") not in pw_consensus:
                continue
            if pw_half and str(c.get("quarter") or "") not in PW_HALF_QUARTERS.get(pw_half, ()):
                continue
            if pw_quarter and str(c.get("quarter") or "") != pw_quarter:
                continue
            if pw_margin:
                smaf = c.get("score_margin_at_fire")
                if smaf is None:
                    continue
                if pw_margin == "leading" and smaf <= 0:
                    continue
                if pw_margin == "trailing" and smaf > 0:
                    continue
            if pw_spread_role:
                # Derive spread_role from game record when missing on call (#135 port)
                pred_tid = str(c.get("predicted_team_id") or "").strip()
                _sr = ""
                if pred_tid and pw_fav:
                    _sr = "favorite" if pred_tid == pw_fav else "underdog"
                elif not pred_tid:
                    # Fallback: try spread_role already on call
                    _sr = str(c.get("spread_role") or "").strip().lower()
                if _sr != pw_spread_role:
                    continue
            if pw_edge:
                _ae = c.get("avg_edge")
                if _ae is None:
                    continue
                try:
                    _ae = float(_ae)
                except (TypeError, ValueError):
                    continue
                if pw_edge == "10+" and _ae < 10:
                    continue
                if pw_edge == "5-10" and (_ae < 5 or _ae >= 10):
                    continue
                if pw_edge == "0-5" and (_ae < 0 or _ae >= 5):
                    continue
                if pw_edge == "neg" and _ae >= 0:
                    continue
            _pw_matched.append(c)
        # Apply call_selection to matched calls
        if pw_call_selection and _pw_matched:
            _sorted_m = sorted(_pw_matched, key=lambda x: x.get("ts", ""))
            if pw_call_selection == "first":
                _pw_matched = [_sorted_m[0]]
            elif pw_call_selection == "last":
                _pw_matched = [_sorted_m[-1]]
            elif pw_call_selection == "first_per_quarter":
                _seen_q = {}
                for _sc in _sorted_m:
                    _sq = str(_sc.get("quarter") or "")
                    if _sq not in _seen_q:
                        _seen_q[_sq] = _sc
                _pw_matched = list(_seen_q.values())
            elif pw_call_selection == "first_per_half":
                _seen_h = {}
                for _sc in _sorted_m:
                    # NBA quarter → half mapping: Q1/Q2 = 1H, Q3/Q4 = 2H, OT/2OT = OT
                    _cq = str(_sc.get("quarter") or "")
                    _sh = "1H" if _cq in ("Q1", "Q2") else ("2H" if _cq in ("Q3", "Q4") else "OT")
                    if _sh not in _seen_h:
                        _seen_h[_sh] = _sc
                _pw_matched = list(_seen_h.values())
        if not _pw_matched:
            return False

    return True


def _history_project_list_item(rec, condition_set=None, include_pw_summary=False):
    """Return compact history row for list endpoint.

    When condition_set is provided (list of condition names), also include
    winner_name and conditions_fired_summary (all fired conditions with
    available score-at-fire fields) for drill-down column rendering.

    When include_pw_summary is True, include a pw_summary dict with
    aggregate predicted_winner_calls stats for PW drill-down columns.
    """
    away_name = str(rec.get("away_name") or rec.get("away_abbr") or "Away")
    home_name = str(rec.get("home_name") or rec.get("home_abbr") or "Home")
    away_score = rec.get("away_score")
    home_score = rec.get("home_score")

    score_line = "--"
    try:
        score_line = "{}-{}".format(int(away_score), int(home_score))
    except (TypeError, ValueError):
        pass

    away_id = str(rec.get("away_id") or "")
    winner_id = str(rec.get("winner_id") or "")
    winner_name = (
        away_name if (winner_id and winner_id == away_id)
        else home_name if winner_id
        else ""
    )

    result = {
        "game_id": str(rec.get("game_id") or ""),
        "date": rec.get("date"),
        "record_updated_at": rec.get("record_updated_at"),
        "away_name": away_name,
        "home_name": home_name,
        "away_abbr": str(rec.get("away_abbr") or ""),
        "home_abbr": str(rec.get("home_abbr") or ""),
        "away_score": away_score,
        "home_score": home_score,
        "score_line": score_line,
        "margin": rec.get("margin"),
        "winner_id": str(rec.get("winner_id") or ""),
        "winner_name": winner_name,
        "spread_detail": str(rec.get("spread_detail") or ""),
        "conditions_fired_count": len(rec.get("conditions_fired") or []),
        "outcome_pills": _history_outcome_pills(rec),
        "source": str(rec.get("source") or ""),
        "replay_mode": str(rec.get("replay_mode") or ""),
        "season": str(rec.get("season") or "") or outcomes_mod._game_season_year(rec),
        "season_segment": str(rec.get("season_segment") or ""),
    }

    if condition_set:
        summary = []
        for cond in (rec.get("conditions_fired") or []):
            if not isinstance(cond, dict):
                continue
            entry = {
                "name": str(cond.get("name") or ""),
                "team": str(cond.get("team") or ""),
                "quarter_str": str(cond.get("quarter_str") or ""),
                "team_won": bool(cond.get("team_won")),
                "direction": str(cond.get("direction") or ""),
            }
            if cond.get("score_margin_at_fire") is not None:
                entry["score_margin_at_fire"] = cond["score_margin_at_fire"]
            if cond.get("quarter_elapsed_pct") is not None:
                entry["quarter_elapsed_pct"] = cond["quarter_elapsed_pct"]
            if cond.get("away_score_at_fire") is not None and cond.get("home_score_at_fire") is not None:
                entry["away_score_at_fire"] = cond["away_score_at_fire"]
                entry["home_score_at_fire"] = cond["home_score_at_fire"]
            summary.append(entry)
        result["conditions_fired_summary"] = summary

    if include_pw_summary:
        calls = [c for c in (rec.get("predicted_winner_calls") or []) if isinstance(c, dict) and c.get("correct") is not None]
        total = len(calls)
        correct = sum(1 for c in calls if bool(c.get("correct")))
        # Per-game ROI (#150)
        flat_pnl = 0.0
        ml_pnl = 0.0
        snap = rec.get("pregame_snapshot") or {}
        home_meta = snap.get("home_matchup_meta") or {}
        away_meta = snap.get("away_matchup_meta") or {}
        for c in calls:
            is_correct = bool(c.get("correct"))
            flat_pnl += outcomes_mod.FLAT_WIN_PROFIT if is_correct else -outcomes_mod.STAKE
            # Look up actual moneyline for predicted team
            pred_tid = str(c.get("predicted_team_id") or "").strip()
            ml_int = None
            if pred_tid:
                if pred_tid == str(home_meta.get("team_id") or ""):
                    ml_int = outcomes_mod._parse_moneyline(home_meta.get("moneyline"))
                elif pred_tid == str(away_meta.get("team_id") or ""):
                    ml_int = outcomes_mod._parse_moneyline(away_meta.get("moneyline"))
            ml_pnl += (outcomes_mod._ml_win_profit(ml_int) if is_correct else -outcomes_mod.STAKE)
        # BK odds/spread ROI (#315)
        bk_odds_pnl = 0.0
        bk_spr_pnl = 0.0
        bk_odds_n = 0
        bk_spr_n = 0
        for c in calls:
            is_correct = bool(c.get("correct"))
            bk_ml = outcomes_mod._parse_moneyline(c.get("bk_moneyline"))
            if bk_ml is not None:
                bk_odds_n += 1
                bk_odds_pnl += (outcomes_mod._ml_win_profit(bk_ml) if is_correct else -outcomes_mod.STAKE)
            bk_spr = c.get("bk_spread")
            if bk_spr is not None:
                bk_spr_n += 1
                pred_tid = str(c.get("predicted_team_id") or "").strip()
                covered = outcomes_mod._did_team_cover_bk_line(rec, pred_tid, bk_spr) if pred_tid else None
                if covered is not None:
                    _spr_win = outcomes_mod.FLAT_WIN_PROFIT
                    _spr_price = c.get("bk_spread_price")
                    if _spr_price is not None:
                        try:
                            _spr_win = outcomes_mod._ml_win_profit(int(_spr_price))
                        except (ValueError, TypeError):
                            pass
                    bk_spr_pnl += _spr_win if covered else -outcomes_mod.STAKE
        result["pw_summary"] = {
            "total": total,
            "correct": correct,
            "pct": round(correct / total * 100, 1) if total > 0 else None,
            "flat_pnl": round(flat_pnl, 2),
            "ml_pnl": round(ml_pnl, 2),
            "bk_odds_pnl": round(bk_odds_pnl, 2) if bk_odds_n > 0 else None,
            "bk_spr_pnl": round(bk_spr_pnl, 2) if bk_spr_n > 0 else None,
        }

    return result


def build_history_list_payload(params):
    """Build paginated history list payload from query params."""
    page = _parse_int_param(_param_first(params, "page", "1"), 1, 1, 50000)
    page_size = _parse_int_param(_param_first(params, "page_size", "25"), 25, 5, 200)

    sort_raw = str(_param_first(params, "sort", "date_desc") or "date_desc").strip().lower()
    sort_mode = sort_raw if sort_raw in ("date_desc", "date_asc") else "date_desc"

    def _to_utc_day_start(raw):
        txt = str(raw or "").strip()
        if not txt:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
            return _parse_iso_utc_or_none(txt + "T00:00:00+00:00")
        return _parse_iso_utc_or_none(txt)

    def _to_utc_day_end(raw):
        txt = str(raw or "").strip()
        if not txt:
            return None
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
            return _parse_iso_utc_or_none(txt + "T23:59:59.999999+00:00")
        return _parse_iso_utc_or_none(txt)

    cond_set_raw = str(_param_first(params, "condition_set", "") or "").strip()
    condition_set = [c.strip() for c in cond_set_raw.split(",") if c.strip()] if cond_set_raw else []

    tag_set_raw = str(_param_first(params, "tag_set", "") or "").strip()
    tag_set = []
    seen_tags = set()
    if tag_set_raw:
        for token in tag_set_raw.split(","):
            normalized = str(token or "").strip().upper()
            if not normalized or normalized in seen_tags:
                continue
            seen_tags.add(normalized)
            tag_set.append(normalized)

    team_set_raw = str(_param_first(params, "team_set", "") or "").strip()
    team_set = []
    seen_teams = set()
    if team_set_raw:
        for token in team_set_raw.split(","):
            normalized = str(token or "").strip().lower()
            if not normalized or normalized in seen_teams:
                continue
            seen_teams.add(normalized)
            team_set.append(normalized)

    # Phase 1: margin_when_fired — condition-level score state filter
    margin_when_fired_raw = str(_param_first(params, "margin_when_fired", "") or "").strip().lower()
    margin_when_fired = margin_when_fired_raw if margin_when_fired_raw in ("positive", "negative", "zero") else ""

    # Phase 2: outcome_set — AND-semantics multi-outcome filter (upset, close, comeback, blowout)
    outcome_set_raw = str(_param_first(params, "outcome_set", "") or "").strip()
    outcome_set = []
    seen_outcomes = set()
    if outcome_set_raw:
        for token in outcome_set_raw.split(","):
            normalized = str(token or "").strip().lower()
            if not normalized or normalized in seen_outcomes:
                continue
            seen_outcomes.add(normalized)
            outcome_set.append(normalized)

    # Phase 2: margin_min / margin_max — game-level final score margin bounds
    def _parse_optional_float(raw):
        txt = str(raw or "").strip()
        if not txt:
            return None
        try:
            return float(txt)
        except (TypeError, ValueError):
            return None

    margin_min = _parse_optional_float(_param_first(params, "margin_min", ""))
    margin_max = _parse_optional_float(_param_first(params, "margin_max", ""))

    # Phase 2: team_role — condition-level home/away role filter
    team_role_raw = str(_param_first(params, "team_role", "") or "").strip().lower()
    team_role = team_role_raw if team_role_raw in ("home", "away") else ""

    # Phase 2: team_won — condition-level win/loss filter
    team_won_raw = str(_param_first(params, "team_won", "") or "").strip().lower()
    team_won_filter = team_won_raw if team_won_raw in ("true", "false") else ""

    # Phase 2: data_source — provenance filter (live, backfill, canonical)
    data_source_raw = str(_param_first(params, "data_source", "") or "").strip().lower()
    data_source = data_source_raw if data_source_raw in ("live", "backfill", "canonical") else ""

    # PW drill-down filters — match games with ≥1 resolved predicted_winner_call in the group
    pw_band_raw = str(_param_first(params, "pw_band", "") or "").strip()
    pw_band = pw_band_raw if pw_band_raw in ("50", "60", "70", "80", "90") else ""

    _pw_cons_raw = str(_param_first(params, "pw_consensus", "") or "").strip()
    pw_consensus = set(c.strip() for c in _pw_cons_raw.split(",") if c.strip()) if _pw_cons_raw else set()

    pw_quarter_raw = str(_param_first(params, "pw_quarter", "") or "").strip().upper()
    pw_quarter = pw_quarter_raw if pw_quarter_raw in ("Q1", "Q2", "Q3", "Q4", "OT", "2OT") else ""

    pw_margin_raw = str(_param_first(params, "pw_margin", "") or "").strip().lower()
    pw_margin = pw_margin_raw if pw_margin_raw in ("leading", "trailing") else ""

    spread_role_raw = str(_param_first(params, "spread_role", "") or "").strip().lower()
    spread_role = spread_role_raw if spread_role_raw in ("favorite", "underdog") else ""

    pw_spread_role_raw = str(_param_first(params, "pw_spread_role", "") or "").strip().lower()
    pw_spread_role = pw_spread_role_raw if pw_spread_role_raw in ("favorite", "underdog") else ""

    pw_half_raw = str(_param_first(params, "pw_half", "") or "").strip().upper()
    pw_half = pw_half_raw if pw_half_raw in ("H1", "H2", "OT") else ""
    _pw_half_quarters = {"H1": ("Q1", "Q2"), "H2": ("Q3", "Q4"), "OT": ("OT", "2OT")}.get(pw_half, ())

    pw_call_selection_raw = str(_param_first(params, "pw_call_selection", "") or "").strip().lower()
    pw_call_selection = pw_call_selection_raw if pw_call_selection_raw in ("first", "last", "first_per_quarter", "first_per_half") else ""

    pw_edge_raw = str(_param_first(params, "pw_edge", "") or "").strip()
    pw_edge = pw_edge_raw if pw_edge_raw in ("10+", "5-10", "0-5", "neg") else ""

    pw_strict_bk = _param_first(params, "strict_bk", "") == "1"

    filters = {
        "team": str(_param_first(params, "team", "") or "").strip().lower(),
        "team_set": team_set,
        "condition": str(_param_first(params, "condition", "") or "").strip().lower(),
        "tag": str(_param_first(params, "tag", "") or "").strip().upper(),
        "tag_set": tag_set,
        "q": str(_param_first(params, "q", "") or "").strip().lower(),
        "date_from": _to_utc_day_start(_param_first(params, "date_from", "")),
        "date_to": _to_utc_day_end(_param_first(params, "date_to", "")),
        "condition_set": condition_set,
        "season_year": str(_param_first(params, "season_year", "") or "").strip(),
        "season_segment": str(_param_first(params, "season_segment", "") or "").strip(),
        # Phase 1
        "margin_when_fired": margin_when_fired,
        # Phase 2
        "outcome_set": outcome_set,
        "margin_min": margin_min,
        "margin_max": margin_max,
        "team_role": team_role,
        "spread_role": spread_role,
        "team_won": team_won_filter,
        "data_source": data_source,
        # PW drill-down
        "pw_band": pw_band,
        "pw_consensus": pw_consensus,
        "pw_quarter": pw_quarter,
        "pw_spread_role": pw_spread_role,
        "pw_margin": pw_margin,
        "pw_half": pw_half,
        "pw_call_selection": pw_call_selection,
        "pw_edge": pw_edge,
        "pw_strict_bk": pw_strict_bk,
    }
    effective_tag_mode = "set" if tag_set else "scalar"
    effective_team_mode = "set" if team_set else "scalar"

    records = _load_history_cached()
    total_all = len(records)
    rows = [r for r in records if _history_matches_filters(r, filters)]

    rows.sort(
        key=lambda r: _history_record_dt(r) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=(sort_mode == "date_desc"),
    )

    total = len(rows)
    start = (page - 1) * page_size
    end = start + page_size
    page_rows = rows[start:end]

    # Drill-down stats should be computed from the full filtered result set,
    # not only the current page, so client headers can match browser totals.
    drilldown_stats = None
    if condition_set:
        cond_names = set(str(c or "") for c in condition_set)
        per_condition_stats = {
            str(name): {
                "total_events": 0,
                "teams_won_events": 0,
                "margin_diff_events": 0,
                "positive_margin_diff_events": 0,
                "negative_margin_diff_events": 0,
                "_margin_diffs": [],
            }
            for name in condition_set
        }
        wins = 0
        positive_margin_diff_games = 0
        game_avg_margin_diffs = []

        for rec in rows:
            if not isinstance(rec, dict):
                continue
            conds = rec.get("conditions_fired") or []
            game_margin_raw = rec.get("margin")
            game_has_win = False
            diffs = []
            qualifying_tids = set()  # populated by combo path; empty for single-condition

            if len(condition_set) > 1:
                # Combination drill-down: only count a win if a team that fired
                # ALL conditions in the combo won. This matches the same-team
                # grouping used in compute_combination_stats.
                by_team_conds = {}  # tid -> {cond_name -> team_won}
                for cond in conds:
                    if not isinstance(cond, dict):
                        continue
                    name = str(cond.get("name") or "")
                    if name not in cond_names:
                        continue
                    raw_tid = cond.get("team_id")
                    tid = str(raw_tid).strip() if raw_tid is not None else ""
                    if not tid:
                        continue
                    if tid not in by_team_conds:
                        by_team_conds[tid] = {}
                    by_team_conds[tid][name] = bool(cond.get("team_won"))

                required = set(cond_names)
                qualifying_tids = set()
                for tid, team_conds in by_team_conds.items():
                    if required <= set(team_conds.keys()):
                        qualifying_tids.add(tid)
                        team_actually_won = any(team_conds.values())
                        # When team_won_filter is active, only count a win if the
                        # qualifying team's outcome matches the filter.
                        if team_won_filter:
                            want_win = (team_won_filter == "true")
                            if team_actually_won == want_win and team_actually_won:
                                game_has_win = True
                        else:
                            if team_actually_won:
                                game_has_win = True

                # margin diffs only from qualifying teams
                for cond in conds:
                    if not isinstance(cond, dict):
                        continue
                    name = str(cond.get("name") or "")
                    if name not in cond_names:
                        continue
                    raw_tid = cond.get("team_id")
                    tid = str(raw_tid).strip() if raw_tid is not None else ""
                    if tid not in qualifying_tids:
                        continue
                    team_won = bool(cond.get("team_won"))
                    stored_diff = cond.get("margin_diff")
                    diff = None
                    try:
                        if stored_diff is not None:
                            diff = float(stored_diff)
                    except (TypeError, ValueError):
                        diff = None
                    if diff is None:
                        score_margin_at_fire = cond.get("score_margin_at_fire")
                        if score_margin_at_fire is None or game_margin_raw is None:
                            continue
                        try:
                            game_margin = float(game_margin_raw)
                            margin_at_fire = float(score_margin_at_fire)
                        except (TypeError, ValueError):
                            continue
                        final_margin_from_cond_team = game_margin if team_won else -game_margin
                        diff = final_margin_from_cond_team - margin_at_fire
                    diffs.append(diff)
            else:
                # Single condition: any team that fired it and won counts.
                # When team_won_filter is active, only count entries matching that
                # filter so the win% reflects the filtered perspective (e.g. when
                # team_won=false, wins should always be 0 for filtered records).
                for cond in conds:
                    if not isinstance(cond, dict):
                        continue
                    name = str(cond.get("name") or "")
                    if name not in cond_names:
                        continue

                    raw_won = cond.get("team_won")
                    # Skip entries with unknown team_won
                    if raw_won is None:
                        continue
                    team_won = bool(raw_won)
                    # When a team_won filter is active, skip entries that don't match it
                    # so wins are only counted from the filtered team's perspective.
                    if team_won_filter:
                        want_win = (team_won_filter == "true")
                        if team_won != want_win:
                            continue
                    if team_won:
                        game_has_win = True

                    stored_diff = cond.get("margin_diff")
                    diff = None
                    try:
                        if stored_diff is not None:
                            diff = float(stored_diff)
                    except (TypeError, ValueError):
                        diff = None

                    if diff is None:
                        score_margin_at_fire = cond.get("score_margin_at_fire")
                        if score_margin_at_fire is None or game_margin_raw is None:
                            continue
                        try:
                            game_margin = float(game_margin_raw)
                            margin_at_fire = float(score_margin_at_fire)
                        except (TypeError, ValueError):
                            continue
                        final_margin_from_cond_team = game_margin if team_won else -game_margin
                        diff = final_margin_from_cond_team - margin_at_fire
                    diffs.append(diff)

            if game_has_win:
                wins += 1

            if diffs:
                avg_diff = sum(diffs) / len(diffs)
                game_avg_margin_diffs.append(avg_diff)
                if avg_diff > 0:
                    positive_margin_diff_games += 1

            for cond_name in condition_set:
                cond_matches = [
                    c for c in conds
                    if isinstance(c, dict) and str(c.get("name") or "") == str(cond_name)
                ]
                if not cond_matches:
                    continue

                # For combo drill-downs, restrict per-condition stats to qualifying teams
                # (those that fired ALL conditions) so the per-condition event counts
                # reflect only the combo team's entries, not every team that fired Q1 etc.
                if len(condition_set) > 1 and qualifying_tids:
                    cond_matches = [
                        c for c in cond_matches
                        if str(c.get("team_id") or "").strip() in qualifying_tids
                    ]
                if not cond_matches:
                    continue

                # When team_won_filter is active, restrict per-condition stats to entries
                # matching the filter so total_events and teams_won_events reflect the
                # filtered perspective (team_won=false → teams_won_events should be 0).
                if team_won_filter:
                    want_win = (team_won_filter == "true")
                    cond_matches = [
                        c for c in cond_matches
                        if c.get("team_won") is not None and bool(c.get("team_won")) == want_win
                    ]
                if not cond_matches:
                    continue

                bucket = per_condition_stats[str(cond_name)]
                bucket["total_events"] += len(cond_matches)
                bucket["teams_won_events"] += sum(1 for c in cond_matches if bool(c.get("team_won")))

                for c in cond_matches:
                    stored_diff = c.get("margin_diff")
                    diff = None
                    try:
                        if stored_diff is not None:
                            diff = float(stored_diff)
                    except (TypeError, ValueError):
                        diff = None

                    if diff is None:
                        score_margin_at_fire = c.get("score_margin_at_fire")
                        if score_margin_at_fire is None or game_margin_raw is None:
                            continue
                        try:
                            game_margin = float(game_margin_raw)
                            margin_at_fire = float(score_margin_at_fire)
                        except (TypeError, ValueError):
                            continue
                        team_won = bool(c.get("team_won"))
                        final_margin_from_cond_team = game_margin if team_won else -game_margin
                        diff = final_margin_from_cond_team - margin_at_fire
                    bucket["_margin_diffs"].append(diff)
                    bucket["margin_diff_events"] += 1
                    if diff > 0:
                        bucket["positive_margin_diff_events"] += 1
                    elif diff < 0:
                        bucket["negative_margin_diff_events"] += 1

        avg_margin_diff = (
            (sum(game_avg_margin_diffs) / len(game_avg_margin_diffs))
            if game_avg_margin_diffs
            else None
        )

        per_condition = {}
        for cond_name in condition_set:
            bucket = per_condition_stats.get(str(cond_name), {})
            cond_total = int(bucket.get("total_events") or 0)
            cond_wins = int(bucket.get("teams_won_events") or 0)
            cond_margin_events = int(bucket.get("margin_diff_events") or 0)
            cond_pos = int(bucket.get("positive_margin_diff_events") or 0)
            cond_neg = int(bucket.get("negative_margin_diff_events") or 0)
            cond_avg_list = list(bucket.get("_margin_diffs") or [])
            cond_avg_margin_diff = (
                (sum(cond_avg_list) / len(cond_avg_list))
                if cond_avg_list
                else None
            )
            per_condition[str(cond_name)] = {
                "total_events": cond_total,
                "teams_won_events": cond_wins,
                "teams_won_pct": ((cond_wins / cond_total) * 100.0) if cond_total > 0 else 0.0,
                "margin_diff_events": cond_margin_events,
                "positive_margin_diff_events": cond_pos,
                "positive_margin_diff_pct": ((cond_pos / cond_margin_events) * 100.0) if cond_margin_events > 0 else 0.0,
                "negative_margin_diff_events": cond_neg,
                "negative_margin_diff_pct": ((cond_neg / cond_margin_events) * 100.0) if cond_margin_events > 0 else 0.0,
                "avg_margin_diff": cond_avg_margin_diff,
            }

        drilldown_stats = {
            "total_games": total,
            "teams_won_games": wins,
            "teams_won_pct": ((wins / total) * 100.0) if total > 0 else 0.0,
            "positive_margin_diff_games": positive_margin_diff_games,
            "positive_margin_diff_pct": ((positive_margin_diff_games / total) * 100.0) if total > 0 else 0.0,
            "avg_margin_diff": avg_margin_diff,
            "per_condition": per_condition,
        }

    # PW drill-down stats — computed when PW filters are active but no condition_set
    # Filter-then-select semantics matching _history_matches_filters (#135 port)
    if drilldown_stats is None and (pw_band or pw_consensus or pw_quarter or pw_spread_role or pw_margin or pw_half or pw_call_selection or pw_strict_bk):
        pw_band_min = int(pw_band) if pw_band else None
        pw_band_max = (pw_band_min + 10) if (pw_band_min is not None and pw_band_min < 90) else 200

        def _pw_stat_call_matches(c, rec_fav=""):
            if pw_strict_bk and not c.get("bk_ml_source"):
                return False
            if pw_band_min is not None and not (pw_band_min <= float(c.get("pct") or 0) < pw_band_max):
                return False
            if pw_consensus and str(c.get("consensus") or "") not in pw_consensus:
                return False
            if pw_half and str(c.get("quarter") or "") not in _pw_half_quarters:
                return False
            if pw_quarter and str(c.get("quarter") or "") != pw_quarter:
                return False
            if pw_margin:
                smaf = c.get("score_margin_at_fire")
                if smaf is None:
                    return False
                if pw_margin == "leading" and smaf <= 0:
                    return False
                if pw_margin == "trailing" and smaf > 0:
                    return False
            if pw_spread_role:
                # Derive spread_role from game record when missing (#135 port)
                pred_tid = str(c.get("predicted_team_id") or "").strip()
                _sr = ""
                if pred_tid and rec_fav:
                    _sr = "favorite" if pred_tid == rec_fav else "underdog"
                elif not pred_tid:
                    _sr = str(c.get("spread_role") or "").strip().lower()
                if _sr != pw_spread_role:
                    return False
            return True

        def _apply_call_selection(matched):
            """Apply call_selection filter to matched PW calls."""
            if not pw_call_selection or not matched:
                return matched
            _sorted_m = sorted(matched, key=lambda x: x.get("ts", ""))
            if pw_call_selection == "first":
                return [_sorted_m[0]]
            elif pw_call_selection == "last":
                return [_sorted_m[-1]]
            elif pw_call_selection == "first_per_quarter":
                _seen_q = {}
                for _sc in _sorted_m:
                    _sq = str(_sc.get("quarter") or "")
                    if _sq not in _seen_q:
                        _seen_q[_sq] = _sc
                return list(_seen_q.values())
            elif pw_call_selection == "first_per_half":
                _seen_h = {}
                for _sc in _sorted_m:
                    _cq = str(_sc.get("quarter") or "")
                    _sh = "1H" if _cq in ("Q1", "Q2") else ("2H" if _cq in ("Q3", "Q4") else "OT")
                    if _sh not in _seen_h:
                        _seen_h[_sh] = _sc
                return list(_seen_h.values())
            return matched

        pw_total_calls = 0
        pw_correct_calls = 0
        pw_games_won = 0  # games where ≥1 matching call was correct

        for rec in rows:
            if not isinstance(rec, dict):
                continue
            all_calls = [c for c in (rec.get("predicted_winner_calls") or []) if isinstance(c, dict) and c.get("correct") is not None]
            rec_fav = str(rec.get("spread_fav_id") or "").strip()
            matching = [c for c in all_calls if _pw_stat_call_matches(c, rec_fav)]
            matching = _apply_call_selection(matching)
            if not matching:
                continue
            pw_total_calls += len(matching)
            correct_in_game = sum(1 for c in matching if bool(c.get("correct")))
            pw_correct_calls += correct_in_game
            if correct_in_game > 0:
                pw_games_won += 1

        drilldown_stats = {
            "total_games": total,
            "teams_won_games": pw_games_won,
            "teams_won_pct": ((pw_games_won / total) * 100.0) if total > 0 else 0.0,
            "pw_total_calls": pw_total_calls,
            "pw_correct_calls": pw_correct_calls,
            "pw_accuracy_pct": ((pw_correct_calls / pw_total_calls) * 100.0) if pw_total_calls > 0 else 0.0,
            "is_pw_drilldown": True,
        }

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "total": total,
        "total_all": total_all,
        "page": page,
        "page_size": page_size,
        "has_more": end < total,
        "sort": sort_mode,
        "filters": {
            "team": filters["team"],
            "team_set": filters["team_set"],
            "effective_team_mode": effective_team_mode,
            "condition": filters["condition"],
            "tag": filters["tag"],
            "tag_set": filters["tag_set"],
            "effective_tag_mode": effective_tag_mode,
            "q": filters["q"],
            "date_from": filters["date_from"].isoformat() if filters["date_from"] else "",
            "date_to": filters["date_to"].isoformat() if filters["date_to"] else "",
            "condition_set": condition_set,
            "season_year": filters["season_year"],
            "season_segment": filters["season_segment"],
            # Phase 1
            "margin_when_fired": filters["margin_when_fired"],
            # Phase 2
            "outcome_set": filters["outcome_set"],
            "margin_min": filters["margin_min"],
            "margin_max": filters["margin_max"],
            "team_role": filters["team_role"],
            "team_won": filters["team_won"],
            "data_source": filters["data_source"],
        },
        "drilldown_stats": drilldown_stats,
        "items": [_history_project_list_item(r, condition_set=condition_set or None, include_pw_summary=bool(pw_band or pw_consensus or pw_quarter or pw_spread_role or pw_margin or pw_half or pw_call_selection or pw_edge or pw_strict_bk)) for r in page_rows],
    }


def build_history_game_payload(game_id):
    """Return full record payload for a single game_id."""
    gid = str(game_id or "").strip()
    if not gid:
        return None

    for rec in _load_history_cached():
        if str((rec or {}).get("game_id") or "") != gid:
            continue
        payload = dict(rec)
        payload["outcome_pills"] = _history_outcome_pills(rec)
        # Enrich conditions_fired with condition_edge (#217)
        cfs = payload.get("conditions_fired")
        if isinstance(cfs, list):
            needs_edge = any(isinstance(cf, dict) and cf.get("condition_edge") is None for cf in cfs)
            if needs_edge:
                try:
                    _es = outcomes_mod.ConditionEdgeStore.load_or_rebuild()
                    for cf in cfs:
                        if isinstance(cf, dict) and cf.get("condition_edge") is None:
                            name = str(cf.get("name") or "").strip()
                            if name:
                                cf["condition_edge"] = round(_es.edge(name), 1)
                except Exception:
                    pass
        return payload
    return None


def build_history_facets_payload(limit=40):
    """Return facet counts for history filters (teams, conditions, tags)."""
    try:
        max_items = int(limit)
    except (TypeError, ValueError):
        max_items = 40
    max_items = min(200, max(5, max_items))

    team_counts = {}
    condition_counts = {}
    tag_counts = {}

    for rec in _load_history_cached():
        if not isinstance(rec, dict):
            continue

        teams = [
            str(rec.get("away_name") or rec.get("away_abbr") or "").strip(),
            str(rec.get("home_name") or rec.get("home_abbr") or "").strip(),
        ]
        for team in teams:
            if not team:
                continue
            team_counts[team] = team_counts.get(team, 0) + 1

        seen_conditions = set()
        for cond in rec.get("conditions_fired") or []:
            if not isinstance(cond, dict):
                continue
            name = str(cond.get("name") or "").strip()
            if not name or name in seen_conditions:
                continue
            seen_conditions.add(name)
            condition_counts[name] = condition_counts.get(name, 0) + 1

        for tag in _history_outcome_pills(rec):
            label = str(tag or "").strip().upper()
            if not label:
                continue
            tag_counts[label] = tag_counts.get(label, 0) + 1

    def _as_sorted_rows(counter):
        rows = [{"value": k, "count": v} for k, v in counter.items()]
        rows.sort(key=lambda row: (-row["count"], row["value"].lower()))
        return rows[:max_items]

    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "limit": max_items,
        "teams": _as_sorted_rows(team_counts),
        "conditions": _as_sorted_rows(condition_counts),
        "tags": _as_sorted_rows(tag_counts),
    }


def build_history_seasons_payload():
    """Return sorted list of NBA season keys and their game counts.

    Season keys are auto-derived from game dates in the corpus so the
    dropdown always reflects available data without hardcoding.
    """
    from collections import Counter
    counts = Counter(_game_season_year(r) for r in _load_history_cached())
    seasons = sorted(counts.items(), reverse=True)  # newest first
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "seasons": [{"key": k, "count": v} for k, v in seasons if k],
    }


def _get_odds_api_credits():
    """Get combined log + live credit status for Odds API."""
    try:
        log_status = odds_api_mod.get_log_credit_status()
        api_key = ""
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                api_key = json.load(f).get("odds_api_key", "")
        except Exception:
            pass
        live_status = odds_api_mod.query_live_credits(api_key) if api_key else {}
        in_memory = odds_api_mod.get_credit_status()
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


def build_custom_queries_payload(params):
    """Compute custom query results from game history (#326)."""
    records = outcomes_mod.load_history(migrate=False)
    all_records = list(records)

    # Apply season/segment filters
    season_segment = str(_param_first(params, "season_segment", "") or "").strip()
    season_year = str(_param_first(params, "season_year", "") or "").strip()
    if season_segment or season_year:
        records = outcomes_mod._filter_records_by_segment(records, season_segment, season_year=season_year)

    seasons = outcomes_mod._available_seasons(all_records)
    total_games = len(records)
    is_wnba = league_config.LEAGUE == "wnba"

    empty = {
        "total_games": 0,
        "margin_dist": {"games": 0, "lines": []},
        "h1_totals": {"games": 0, "lines": []},
        "first_scorer_1h_wins_game": {"games": 0, "wins_game": 0, "pct": 0},
        "first_2h_scorer": {"games": 0, "wins_game": 0, "pct": 0},
        "first_2h_scorer_by_team": [],
        "quarter_lead": [],
        "filters": {"season_segment": season_segment, "season_year": season_year},
        "seasons": seasons,
    }
    if not total_games:
        return empty

    # ── Query 1: Winning Margin Distribution ──
    margins = []
    for r in records:
        m = r.get("margin")
        if m is not None:
            margins.append(abs(int(m)))

    if is_wnba:
        margin_lines = [x + 0.5 for x in range(1, 31)]
    else:
        margin_lines = [x + 0.5 for x in range(1, 41)]
    margin_data = []
    n_m = len(margins)
    for line in margin_lines:
        under = sum(1 for m in margins if m < line)
        over = n_m - under
        margin_data.append({
            "line": line,
            "under": under,
            "under_pct": round(under / n_m * 100, 1) if n_m else 0,
            "over": over,
            "over_pct": round(over / n_m * 100, 1) if n_m else 0,
        })
    margin_dist = {
        "games": n_m,
        "avg": round(sum(margins) / n_m, 1) if n_m else 0,
        "min": min(margins) if margins else 0,
        "max": max(margins) if margins else 0,
        "median": sorted(margins)[n_m // 2] if margins else 0,
        "lines": margin_data,
    }

    # ── Query 2: 1H Totals Over/Under ──
    h1_totals_list = []
    for r in records:
        h1h = r.get("home_score_1h")
        a1h = r.get("away_score_1h")
        if h1h is not None and a1h is not None:
            h1_totals_list.append(int(h1h) + int(a1h))

    if is_wnba:
        h1_lines = [x + 0.5 for x in range(60, 101, 2)]
    else:
        h1_lines = [x + 0.5 for x in range(90, 141, 2)]
    h1_data = []
    n_h1 = len(h1_totals_list)
    for line in h1_lines:
        under = sum(1 for t in h1_totals_list if t < line)
        over = n_h1 - under
        h1_data.append({
            "line": line,
            "under": under,
            "under_pct": round(under / n_h1 * 100, 1) if n_h1 else 0,
            "over": over,
            "over_pct": round(over / n_h1 * 100, 1) if n_h1 else 0,
        })
    h1_totals = {
        "games": n_h1,
        "avg": round(sum(h1_totals_list) / n_h1, 1) if n_h1 else 0,
        "min": min(h1_totals_list) if h1_totals_list else 0,
        "max": max(h1_totals_list) if h1_totals_list else 0,
        "median": sorted(h1_totals_list)[n_h1 // 2] if h1_totals_list else 0,
        "lines": h1_data,
    }

    # ── Query 3: First to score in Q1+Q2 wins 1H → wins game? ──
    fs_1h_wins_game = {"games": 0, "wins_game": 0}
    for r in records:
        hq = r.get("home_quarters") or {}
        aq = r.get("away_quarters") or {}
        if not hq or not aq:
            continue
        h1h = r.get("home_score_1h")
        a1h = r.get("away_score_1h")
        if h1h is None or a1h is None or h1h == a1h:
            continue
        # Who won 1H?
        winner_1h_is_home = h1h > a1h
        # Did they win the game?
        winner_id = r.get("winner_id")
        home_id = r.get("home_id")
        away_id = r.get("away_id")
        if not winner_id:
            continue
        fs_1h_wins_game["games"] += 1
        game_winner_is_home = str(winner_id) == str(home_id)
        if winner_1h_is_home == game_winner_is_home:
            fs_1h_wins_game["wins_game"] += 1
    g = fs_1h_wins_game["games"] or 1
    fs_1h_wins_game["pct"] = round(fs_1h_wins_game["wins_game"] / g * 100, 1)

    # ── Query 4: First to score in 2H (Q3) → wins game? ──
    first_2h = {"games": 0, "wins_game": 0}
    first_2h_by_team = {}
    for r in records:
        hq = r.get("home_quarters") or {}
        aq = r.get("away_quarters") or {}
        q3h = hq.get("Q3")
        q3a = aq.get("Q3")
        if q3h is None or q3a is None:
            continue
        # We don't have play-by-play to know who scored first in Q3,
        # but we can use who won Q3 as a proxy
        if q3h == q3a:
            continue
        q3_winner_is_home = q3h > q3a
        winner_id = r.get("winner_id")
        home_id = r.get("home_id")
        away_id = r.get("away_id")
        if not winner_id:
            continue
        q3_winner_name = r.get("home_name" if q3_winner_is_home else "away_name", "")
        first_2h["games"] += 1
        game_winner_is_home = str(winner_id) == str(home_id)
        won_game = q3_winner_is_home == game_winner_is_home
        if won_game:
            first_2h["wins_game"] += 1
        entry = first_2h_by_team.setdefault(q3_winner_name,
            {"team": q3_winner_name, "games": 0, "wins_game": 0})
        entry["games"] += 1
        if won_game:
            entry["wins_game"] += 1

    g2 = first_2h["games"] or 1
    first_2h["pct"] = round(first_2h["wins_game"] / g2 * 100, 1)
    first_2h_team_list = sorted(first_2h_by_team.values(),
        key=lambda t: t.get("games", 0), reverse=True)
    for t in first_2h_team_list:
        tg = t["games"] or 1
        t["wins_pct"] = round(t["wins_game"] / tg * 100, 1)

    # ── Query 5: Quarter lead → wins game? ──
    quarter_lead = []
    for label in ("Q1", "Q2", "Q3"):
        leading = 0
        leading_wins = 0
        tied = 0
        for r in records:
            hq = r.get("home_quarters") or {}
            aq = r.get("away_quarters") or {}
            # Cumulative score through this quarter
            h_cum = 0
            a_cum = 0
            for q in ("Q1", "Q2", "Q3", "Q4"):
                h_cum += hq.get(q, 0)
                a_cum += aq.get(q, 0)
                if q == label:
                    break
            if h_cum == a_cum:
                tied += 1
                continue
            leader_is_home = h_cum > a_cum
            winner_id = r.get("winner_id")
            home_id = r.get("home_id")
            if not winner_id:
                continue
            leading += 1
            if leader_is_home == (str(winner_id) == str(home_id)):
                leading_wins += 1
        quarter_lead.append({
            "after": label,
            "games_with_lead": leading,
            "tied": tied,
            "wins": leading_wins,
            "wins_pct": round(leading_wins / leading * 100, 1) if leading else 0,
        })

    return {
        "total_games": total_games,
        "margin_dist": margin_dist,
        "h1_totals": h1_totals,
        "first_scorer_1h_wins_game": fs_1h_wins_game,
        "first_2h_scorer": first_2h,
        "first_2h_scorer_by_team": first_2h_team_list,
        "quarter_lead": quarter_lead,
        "filters": {"season_segment": season_segment, "season_year": season_year},
        "seasons": seasons,
    }


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # Suppress default access log spam

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
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
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        params = parse_qs(parsed_url.query)

        # ── API endpoints ──────────────────────────────────────────
        if path == "/api/config":
            try:
                with open(CONFIG_FILE) as f:
                    cfg = json.load(f)
                for k in SECRET_KEYS:
                    if k in cfg:
                        cfg[k] = "***"
                self.send_json(200, cfg)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/alerts":
            try:
                if os.path.exists(ALERTS_FILE):
                    with open(ALERTS_FILE) as f:
                        self.send_json(200, json.load(f))
                else:
                    self.send_json(200, [])
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/postgame":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                window_hours = float(cfg.get("postgame_summary_hours", 4))
                window_secs  = window_hours * 3600
                now_ts       = time.time()
                summaries    = []
                if os.path.exists(ALERTS_FILE):
                    with open(ALERTS_FILE) as f:
                        log = json.load(f)

                    # Build per-game condition map from all non-summary/non-final entries
                    game_conditions = {}  # game_id -> [{condition, team, quarter_str, direction, type}]
                    recent_finals = {}    # game_id -> latest final entry within window
                    for entry in log:
                        gid = str(entry.get("game_id") or "")
                        if not gid:
                            continue
                        etype = entry.get("type")
                        if etype == "final":
                            try:
                                entry_ts = datetime.fromisoformat(
                                    entry["ts"].replace("Z", "+00:00")
                                ).timestamp()
                            except Exception:
                                entry_ts = None
                            if entry_ts is not None and now_ts - entry_ts <= window_secs:
                                prev = recent_finals.get(gid)
                                if not prev:
                                    recent_finals[gid] = entry
                                else:
                                    try:
                                        prev_ts = datetime.fromisoformat(
                                            prev["ts"].replace("Z", "+00:00")
                                        ).timestamp()
                                    except Exception:
                                        prev_ts = 0
                                    if entry_ts >= prev_ts:
                                        recent_finals[gid] = entry
                        if etype in ("summary", "final"):
                            continue
                        game_conditions.setdefault(gid, [])
                        # Deduplicate: same condition+team combo only once
                        key = "{}|{}".format(entry.get("condition",""), entry.get("team",""))
                        if not any(
                            "{}|{}".format(e.get("condition",""), e.get("team","")) == key
                            for e in game_conditions[gid]
                        ):
                            game_conditions[gid].append({
                                "condition":   entry.get("condition", ""),
                                "team":        entry.get("team", ""),
                                "quarter_str": entry.get("quarter_str", ""),
                                "direction":   entry.get("direction", ""),
                                "type":        entry.get("type", ""),
                                "color":       entry.get("color", ""),
                                "delivery_ok": entry.get("delivery_ok", True),
                            })

                    # Load conditions_fired from game_history.json (use cache to avoid 61MB re-parse)
                    try:
                        game_history = _load_history_cached()
                        for rec in game_history:
                            gid = str(rec.get("game_id") or "")
                            if gid and rec.get("conditions_fired"):
                                game_conditions[gid] = [
                                    {
                                        "condition": cond.get("name", ""),
                                        "team": cond.get("team", ""),
                                        "quarter_str": cond.get("quarter_str", ""),
                                        "direction": cond.get("direction", ""),
                                        "type": cond.get("type", ""),
                                        "color": cond.get("color", "📌"),
                                        "delivery_ok": cond.get("alerted", True),
                                    }
                                    for cond in rec.get("conditions_fired", [])
                                ]
                    except Exception:
                        pass
                    
                    for entry in log:
                        if entry.get("type") != "summary":
                            continue
                        try:
                            entry_ts = datetime.fromisoformat(
                                entry["ts"].replace("Z", "+00:00")
                            ).timestamp()
                        except Exception:
                            continue
                        if now_ts - entry_ts <= window_secs:
                            gid = str(entry.get("game_id") or "")
                            enriched = dict(entry)
                            enriched["fired_conditions"] = game_conditions.get(gid, [])
                            summaries.append(enriched)

                    # Fallback: if no explicit summary exists for a recent final game,
                    # expose a synthetic summary card so post-game panel is never empty.
                    existing_summary_gids = {
                        str(s.get("game_id") or "")
                        for s in summaries
                        if s.get("game_id")
                    }
                    for gid, final_entry in recent_finals.items():
                        if gid in existing_summary_gids:
                            continue
                        enriched = dict(final_entry)
                        enriched["type"] = "summary"
                        enriched["condition"] = "Game Summary"
                        if not enriched.get("direction"):
                            enriched["direction"] = "📌 FINAL"
                        enriched["fired_conditions"] = game_conditions.get(gid, [])
                        summaries.append(enriched)

                    summaries.sort(
                        key=lambda s: str(s.get("ts") or ""),
                        reverse=True,
                    )
                self.send_json(200, summaries)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history":
            try:
                self.send_json(200, build_history_list_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history-game":
            try:
                game_id = _param_first(params, "game_id", "")
                payload = build_history_game_payload(game_id)
                if payload is None:
                    self.send_json(404, {"error": "game_id not found"})
                else:
                    self.send_json(200, payload)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history-facets":
            try:
                raw_limit = _param_first(params, "limit", "40")
                self.send_json(200, build_history_facets_payload(limit=raw_limit))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/history-seasons":
            try:
                self.send_json(200, build_history_seasons_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/outcomes":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                raw_min_sample = cfg.get("outcomes_min_sample", outcomes_mod.MIN_SAMPLE)
                raw_decay_lambda = cfg.get("outcomes_decay_lambda", 0.0)
                raw_max_combo = cfg.get("outcomes_max_combo_size", 3)
                try:
                    min_sample = int(raw_min_sample)
                except (TypeError, ValueError):
                    min_sample = outcomes_mod.MIN_SAMPLE
                try:
                    decay_lambda = float(raw_decay_lambda)
                except (TypeError, ValueError):
                    decay_lambda = 0.0
                try:
                    max_combo_size = int(raw_max_combo)
                except (TypeError, ValueError):
                    max_combo_size = 3
                min_sample = min(200, max(1, min_sample))
                decay_lambda = min(1.0, max(0.0, decay_lambda))
                max_combo_size = max(2, min(5, max_combo_size))
                season_segment = str(_param_first(params, "season_segment", "") or "").strip()
                spread_role = str(_param_first(params, "spread_role", "") or "").strip().lower()
                season_year = str(_param_first(params, "season_year", "") or "").strip()
                margin_at_fire_raw = str(_param_first(params, "margin_at_fire", "") or "").strip().lower()
                margin_at_fire = margin_at_fire_raw if margin_at_fire_raw in ("trailing", "leading") else ""
                pw_quarter_raw = str(_param_first(params, "pw_quarter", "") or "").strip().upper()
                pw_quarter = pw_quarter_raw if pw_quarter_raw in ("Q1", "Q2", "Q3", "Q4", "OT", "2OT") else ""
                # Matrix cross-dimensional pivots (#150)
                do_matrix = _param_first(params, "matrix", "1") != "0"
                matrix_pins = {}
                for dim in ("threshold", "consensus", "quarter", "spread_role", "half", "edge_range", "margin"):
                    val = str(_param_first(params, f"matrix_pin_{dim}", "") or "").strip()
                    if val:
                        matrix_pins[dim] = val
                # Custom compound pivot (#349)
                _valid_dims = {"threshold", "consensus", "quarter", "spread_role", "half", "edge_range", "margin"}
                _row_dims_raw = str(_param_first(params, "row_dims", "") or "").strip()
                _col_dims_raw = str(_param_first(params, "col_dims", "") or "").strip()
                custom_pivot = None
                if _row_dims_raw and _col_dims_raw:
                    _rd = [d.strip() for d in _row_dims_raw.split(",") if d.strip() in _valid_dims]
                    _cd = [d.strip() for d in _col_dims_raw.split(",") if d.strip() in _valid_dims]
                    if _rd and _cd:
                        custom_pivot = {"row_dims": _rd, "col_dims": _cd}
                result = outcomes_mod.compute_outcomes(min_sample, decay_lambda=decay_lambda, max_combo_size=max_combo_size, season_segment=season_segment, spread_role=spread_role, season_year=season_year, margin_at_fire=margin_at_fire, matrix=do_matrix, matrix_pins=None)
                # Eager PW accuracy refresh (#165): when the cached result is
                # stale (background recompute running), recompute just the
                # lightweight PW accuracy inline so the dashboard sees updated
                # predicted_winner_calls immediately.
                call_selection = str(_param_first(params, "call_selection", "") or "").strip().lower()
                if call_selection not in ("first", "first_per_quarter", "last"):
                    call_selection = ""
                f_strict_bk = _param_first(params, "strict_bk", "") == "1"
                _pw_empty = not (result.get("predicted_winner_accuracy") or {}).get("total_calls")
                pw_needs_refresh = (
                    _pw_empty
                    or (result.get("cache_status") or {}).get("recomputing", False)
                    or (do_matrix and matrix_pins)
                    or pw_quarter
                    or call_selection
                    or f_strict_bk
                )
                if pw_needs_refresh:
                    records = outcomes_mod.load_history(migrate=False)
                    if season_segment or season_year:
                        records = outcomes_mod._filter_records_by_segment(records, season_segment, season_year=season_year)
                    if f_strict_bk:
                        records, _ = _filter_strict_bk(records)
                    result = dict(result)  # shallow copy to avoid mutating cache
                    result["predicted_winner_accuracy"] = outcomes_mod.compute_predicted_winner_accuracy(
                        records, spread_role=spread_role, margin_at_fire=margin_at_fire,
                        quarter=pw_quarter,
                        matrix=do_matrix, matrix_pins=matrix_pins if matrix_pins else None,
                        call_selection=call_selection, custom_pivot=custom_pivot)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/live-analysis":
            try:
                self.send_json(200, outcomes_mod.load_live_analysis())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        def _pw_add_result(gdata, rec):
            """Add final result metadata to a PW game entry from a history record."""
            gdata["season"] = str(rec.get("season") or "") or outcomes_mod._game_season_year(rec)
            gdata["season_segment"] = str(rec.get("season_segment") or "")
            gdata["home_id"] = str(rec.get("home_id") or "")
            gdata["away_id"] = str(rec.get("away_id") or "")
            gdata["spread_fav_id"] = str(rec.get("spread_fav_id") or "")
            gdata["away_abbr"] = str(rec.get("away_abbr") or "")
            gdata["home_name"] = str(rec.get("home_name") or rec.get("home_abbr") or "")
            gdata["away_name"] = str(rec.get("away_name") or rec.get("away_abbr") or "")
            hs = rec.get("home_score")
            aws = rec.get("away_score")
            if hs is not None and aws is not None:
                gdata["home_score"] = hs
                gdata["away_score"] = aws
                margin = abs(int(hs) - int(aws))
                winner_id = str(rec.get("winner_id") or "")
                home_id = str(rec.get("home_id") or "")
                away_id = str(rec.get("away_id") or "")
                if winner_id == home_id:
                    gdata["winner"] = str(rec.get("home_name") or rec.get("home_abbr") or "Home")
                    gdata["winner_abbr"] = str(rec.get("home_abbr") or "")
                else:
                    gdata["winner"] = str(rec.get("away_name") or rec.get("away_abbr") or "Away")
                    gdata["winner_abbr"] = str(rec.get("away_abbr") or "")
                gdata["final_margin"] = margin

                # PW result summary from the latest call (#166)
                calls = gdata.get("calls") or []
                if calls:
                    latest = max(calls, key=lambda c: c.get("ts") or "")
                    pid = str(latest.get("predicted_team_id") or "")
                    gdata["pw_correct"] = latest.get("correct")
                    gdata["pw_team"] = latest.get("predicted_team", "")
                    gdata["pw_predicted_team"] = latest.get("predicted_team", "")
                    # Get role/odds/spread from pregame_snapshot (not call — may not be enriched yet)
                    snap = rec.get("pregame_snapshot") or {}
                    sfid = str(rec.get("spread_fav_id") or "")
                    h_meta = snap.get("home_matchup_meta") or {}
                    a_meta = snap.get("away_matchup_meta") or {}
                    if pid and sfid:
                        gdata["pw_role"] = "favorite" if pid == sfid else "underdog"
                    if pid == home_id:
                        gdata["pw_moneyline"] = h_meta.get("moneyline", "")
                        pw_spread_val = h_meta.get("spread")
                    elif pid == away_id:
                        gdata["pw_moneyline"] = a_meta.get("moneyline", "")
                        pw_spread_val = a_meta.get("spread")
                    else:
                        pw_spread_val = None
                    if pw_spread_val is not None:
                        gdata["pw_spread"] = pw_spread_val
                        try:
                            sv = float(pw_spread_val)
                            if pid == home_id:
                                pw_actual_margin = int(hs) - int(aws)
                            elif pid == away_id:
                                pw_actual_margin = int(aws) - int(hs)
                            else:
                                pw_actual_margin = None
                            if pw_actual_margin is not None:
                                cover_val = pw_actual_margin + sv
                                gdata["pw_covered"] = cover_val > 0
                                gdata["pw_cover_margin"] = round(cover_val, 1)
                        except (TypeError, ValueError):
                            pass
                    # Winner's odds for two-line display (#291)
                    _win_id = str(rec.get("winner_id") or "")
                    if _win_id:
                        if _win_id == home_id:
                            gdata["winner_ml"] = h_meta.get("moneyline", "")
                            gdata["winner_spread"] = h_meta.get("spread")
                        elif _win_id == away_id:
                            gdata["winner_ml"] = a_meta.get("moneyline", "")
                            gdata["winner_spread"] = a_meta.get("spread")
                        gdata["winner_role"] = "favorite" if _win_id == sfid else "underdog" if sfid else ""

        if path == "/api/pw-trend":
            try:
                import pw_trend
                date_from = _param_first(params, "date_from", "")
                date_to = _param_first(params, "date_to", "")
                last_n_games = _param_first(params, "last_n_games", "")
                interval = _param_first(params, "interval", "")
                is_cumulative = _param_first(params, "cumulative", "1") != "0"
                source = _param_first(params, "source", "")
                f_season = _param_first(params, "season", "")
                f_segment = _param_first(params, "segment", "")
                f_strict_bk = _param_first(params, "strict_bk", "") == "1"

                if date_from or date_to or last_n_games or interval or not is_cumulative or source or f_season or f_segment or f_strict_bk:
                    history = _load_history_cached()
                    if f_strict_bk:
                        history, _ = _filter_strict_bk(history)
                    if f_season:
                        history = [r for r in history if _game_season_year(r) == f_season]
                    if f_segment:
                        history = [r for r in history if r.get("season_segment", "") == f_segment]
                    if source:
                        if source == "other":
                            history = [r for r in history if r.get("source", "") not in ("live", "backfill")]
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
                    data = pw_trend.load_trend_data()
                    cumulative_snapshot = pw_trend.compute_pw_snapshot(history)
                    self.send_json(200, {
                        "changelog": data.get("changelog", []),
                        "snapshots": series,
                        "cumulative_snapshot": cumulative_snapshot,
                        "filtered": True,
                        "date_from": date_from,
                        "date_to": date_to,
                        "last_n_games": n_games,
                        "interval": interval,
                        "cumulative": is_cumulative,
                    })
                else:
                    data = pw_trend.load_trend_data()
                    history = _load_history_cached()
                    if f_strict_bk:
                        history, _ = _filter_strict_bk(history)
                    cumulative_snapshot = pw_trend.compute_pw_snapshot(history)
                    data["cumulative_snapshot"] = cumulative_snapshot
                    self.send_json(200, data)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-roi-combos":
            try:
                seg = str(_param_first(params, "season_segment", "") or "").strip()
                yr = str(_param_first(params, "season_year", "") or "").strip()
                sort_by = str(_param_first(params, "sort", "ml_roi_pct") or "ml_roi_pct").strip()
                try:
                    limit = int(_param_first(params, "limit", "10"))
                except (TypeError, ValueError):
                    limit = 10
                try:
                    min_calls = int(_param_first(params, "min_calls", "10"))
                except (TypeError, ValueError):
                    min_calls = 10
                limit = max(1, min(100, limit))
                min_calls = max(1, min(500, min_calls))
                f_strict_bk = _param_first(params, "strict_bk", "") == "1"
                records = outcomes_mod.load_history(migrate=False)
                if f_strict_bk:
                    records, _ = _filter_strict_bk(records)
                result = outcomes_mod.compute_pw_roi_combos(
                    records, season_segment=seg, season_year=yr,
                    sort_by=sort_by, limit=limit, min_calls=min_calls)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/pw-calls-live":
            # Lightweight endpoint: only live/recent PW calls from .alert_state.json (#401)
            # No game_history parse — used by dashboard for fast live polling.
            try:
                _live_stats_fresh = False
                try:
                    if os.path.exists(LIVE_STATS_FILE):
                        _ls_mtime = os.path.getmtime(LIVE_STATS_FILE)
                        _live_stats_fresh = (time.time() - _ls_mtime) < 600
                except Exception:
                    pass

                _now_ts = time.time()
                _RECENCY_SECS = 7200  # 2 hours

                # Pre-load live game IDs for stale check (#406)
                _live_gids = set()
                try:
                    if _live_stats_fresh and os.path.exists(LIVE_STATS_FILE):
                        with open(LIVE_STATS_FILE, encoding="utf-8") as _lf:
                            _ls_check = json.load(_lf)
                        _live_gids = set((_ls_check.get("games") or {}).keys())
                except Exception:
                    pass

                games = {}
                state = _load_alert_state_cached()
                if state:
                    for key, calls in state.items():
                        if key.startswith("pw_calls_") and isinstance(calls, list):
                            gid = key[len("pw_calls_"):]
                            # Skip games already recorded in history (#406)
                            if state.get("history_recorded_{}".format(gid)):
                                continue
                            # Skip stale games not in live_stats and older than recency window (#406)
                            if gid not in _live_gids and calls:
                                _latest_ts = max((c.get("ts") or "" for c in calls), default="")
                                if _latest_ts:
                                    try:
                                        _lt = datetime.fromisoformat(_latest_ts.replace("Z", "+00:00")).timestamp()
                                        if (_now_ts - _lt) > _RECENCY_SECS:
                                            continue
                                    except Exception:
                                        pass
                            games[gid] = {
                                "game_id": gid, "game_name": gid,
                                "calls": [dict(c) for c in calls],
                                "source": "live" if _live_stats_fresh else "recent",
                            }
                    # Resolve game names from pregame snapshots
                    _snaps = state.get("pregame_snapshots") or {}
                    for gid, g in games.items():
                        if g["game_name"] == gid and gid in _snaps:
                            _s = _snaps[gid]
                            _hm = (_s.get("home_matchup_meta") or {}).get("name", "")
                            _am = (_s.get("away_matchup_meta") or {}).get("name", "")
                            if _hm and _am:
                                g["game_name"] = "{} vs {}".format(_am, _hm)
                    # Enrich calls with pregame odds from snapshots
                    for gid, g in games.items():
                        if gid not in _snaps:
                            continue
                        snap = _snaps[gid]
                        h_meta = snap.get("home_matchup_meta") or {}
                        a_meta = snap.get("away_matchup_meta") or {}
                        h_id = str(h_meta.get("team_id") or "")
                        a_id = str(a_meta.get("team_id") or "")
                        sfid = str(snap.get("spread_fav_id") or "")
                        for c in g["calls"]:
                            pid = str(c.get("predicted_team_id") or "")
                            if pid and sfid:
                                c["spread_role"] = "favorite" if pid == sfid else "underdog"
                            if pid == h_id:
                                c.setdefault("moneyline", h_meta.get("moneyline"))
                                c.setdefault("spread", h_meta.get("spread"))
                            elif pid == a_id:
                                c.setdefault("moneyline", a_meta.get("moneyline"))
                                c.setdefault("spread", a_meta.get("spread"))
                    # Expose pregame odds at game level (#411)
                    for gid, g in games.items():
                        if gid not in _snaps:
                            continue
                        snap = _snaps[gid]
                        h_meta = snap.get("home_matchup_meta") or {}
                        a_meta = snap.get("away_matchup_meta") or {}
                        g["home_team"] = h_meta.get("name", "")
                        g["away_team"] = a_meta.get("name", "")
                        g["home_ml"] = h_meta.get("moneyline")
                        g["away_ml"] = a_meta.get("moneyline")
                        g["home_spread"] = h_meta.get("spread")
                        _sfid = str(snap.get("spread_fav_id") or "")
                        _hid = str(h_meta.get("team_id") or "")
                        g["spread_fav"] = h_meta.get("name", "") if _sfid and _sfid == _hid else (a_meta.get("name", "") if _sfid else "")

                    # Enrich with live_stats game state
                    try:
                        if _live_stats_fresh and os.path.exists(LIVE_STATS_FILE):
                            with open(LIVE_STATS_FILE, encoding="utf-8") as f:
                                _ls_data = json.load(f)
                            for _ls_gid, ls in (_ls_data.get("games") or {}).items():
                                if not isinstance(ls, dict) or _ls_gid not in games:
                                    continue
                                g = games[_ls_gid]
                                g["quarter"] = ls.get("quarter", "")
                                g["game_clock"] = ls.get("game_clock", "")
                                teams = ls.get("teams") or {}
                                home_t = next((t for t in teams.values() if isinstance(t, dict) and t.get("homeAway") == "home"), None)
                                away_t = next((t for t in teams.values() if isinstance(t, dict) and t.get("homeAway") == "away"), None)
                                if home_t:
                                    _hs = home_t.get("score")
                                    if _hs is not None:
                                        try:
                                            g["home_score"] = int(_hs)
                                        except (ValueError, TypeError):
                                            pass
                                if away_t:
                                    _as = away_t.get("score")
                                    if _as is not None:
                                        try:
                                            g["away_score"] = int(_as)
                                        except (ValueError, TypeError):
                                            pass
                                if g["game_name"] == _ls_gid and home_t and away_t:
                                    g["game_name"] = "{} vs {}".format(
                                        away_t.get("name") or away_t.get("abbr") or "?",
                                        home_t.get("name") or home_t.get("abbr") or "?")
                                if not g.get("home_abbr") and home_t:
                                    g["home_abbr"] = home_t.get("abbr") or ""
                                if not g.get("away_abbr") and away_t:
                                    g["away_abbr"] = away_t.get("abbr") or ""
                    except Exception:
                        pass

                # Enrich calls with computed fields (bk_live_line, live_line)
                for _g in games.values():
                    for _ec in _g.get("calls", []):
                        _sp = _ec.get("spread")
                        _mg = _ec.get("score_margin_at_fire")
                        if _sp is not None and _mg is not None:
                            try:
                                _ec["live_line"] = round(float(_sp) - float(_mg), 1)
                            except (TypeError, ValueError):
                                pass
                        _bk_sp = _ec.get("bk_spread")
                        if _bk_sp is not None:
                            try:
                                _ec["bk_live_line"] = round(float(_bk_sp), 1)
                            except (TypeError, ValueError):
                                pass

                # Fingerprint from alert_state + live_stats mtimes (#426)
                _state_file = league_config.state_path(".alert_state.json")
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
            def _pred_team_conds(call):
                """Return active_conditions scoped to predicted team only."""
                ac = call.get("active_conditions")
                if not isinstance(ac, list):
                    return []
                pt_id = str(call.get("predicted_team_id") or "").strip()
                pt_name = str(call.get("predicted_team") or "").strip()
                return [a for a in ac if isinstance(a, dict)
                        and (str(a.get("team_id") or "") == pt_id if pt_id and a.get("team_id")
                             else a.get("team") == pt_name)]

            def _american_to_implied(ml):
                """Convert American odds to implied probability (0-1)."""
                try:
                    ml = float(ml)
                except (TypeError, ValueError):
                    return None
                if ml == 0:
                    return None
                if ml < 0:
                    return abs(ml) / (abs(ml) + 100.0)
                return 100.0 / (ml + 100.0)

            def _devig_multiplicative(prob_a, prob_b):
                """Return de-vigged fair probability for side A using multiplicative method."""
                if prob_a is None or prob_b is None:
                    return None
                total = prob_a + prob_b
                if total <= 0:
                    return None
                return round(prob_a / total, 4)

            def _enrich_pw_source_version(call):
                """Add source and pw_version when missing on older calls (#297)."""
                if not call.get("pw_version"):
                    ts = call.get("ts", "")
                    if ts:
                        try:
                            import pw_trend as _pwt
                            call["pw_version"] = _pwt.resolve_pw_version(ts[:10])
                        except Exception:
                            pass
                if not call.get("source"):
                    call["source"] = "backfill" if call.get("synthetic") else "live"
                # Derive price_source on read — never written to disk (#412 Phase 4.2)
                if call.get("bk_ml_source"):
                    call["price_source"] = "live"
                elif call.get("moneyline") or call.get("bk_moneyline"):
                    call["price_source"] = "pregame"
                elif call.get("synth_moneyline") is not None:
                    call["price_source"] = "synthetic"
                else:
                    call["price_source"] = "pregame"

            def _enrich_pw_devig(call, rec):
                """Add de-vigged market prob, edge, and EV (#412 Phase 4.9, 4.10)."""
                pred_id = str(call.get("predicted_team_id") or "").strip()
                home_id = str(rec.get("home_id") or "").strip()
                hml = rec.get("home_moneyline")
                aml = rec.get("away_moneyline")
                h_imp = _american_to_implied(hml)
                a_imp = _american_to_implied(aml)
                if h_imp is None or a_imp is None:
                    return
                if pred_id == home_id:
                    call["market_prob_devig"] = _devig_multiplicative(h_imp, a_imp)
                    call["market_prob_raw"] = round(h_imp, 4) if h_imp else None
                else:
                    call["market_prob_devig"] = _devig_multiplicative(a_imp, h_imp)
                    call["market_prob_raw"] = round(a_imp, 4) if a_imp else None
                # Compute edge and EV inline (#412 Phase 4.10)
                _mp = (call.get("pct") or 0) / 100.0
                _fair = call.get("market_prob_devig")
                if _fair and _mp:
                    call["edge"] = round((_mp - _fair) * 100, 1)
                _eml = call.get("bk_moneyline") or call.get("moneyline")
                if _eml and _mp:
                    _eimp = _american_to_implied(_eml)
                    if _eimp and _eimp > 0:
                        _edec = 1.0 / _eimp
                        call["ev"] = round(_mp * (_edec - 1) - (1 - _mp), 4)

            def _enrich_pw_polarity(call):
                """Add polarity_pos/neg/net from predicted team's conditions."""
                if call.get("polarity_net") is not None:
                    return
                ptc = _pred_team_conds(call)
                if not ptc:
                    return
                cond_names = [str(a.get("name") or "") for a in ptc]
                neg = sum(1 for c in cond_names if outcomes_mod.classify_polarity(c) == "neg")
                pos = sum(1 for c in cond_names if outcomes_mod.classify_polarity(c) == "pos")
                call["polarity_pos"] = pos
                call["polarity_neg"] = neg
                call["polarity_net"] = pos - neg

            def _enrich_pw_avg_edge(call):
                """Add avg_edge from predicted team's condition edges.

                Fills in avg_edge for any call missing it — both legacy calls
                and live calls where outcomes.py didn't compute it (#248).
                """
                if call.get("avg_edge") is not None:
                    return
                ptc = _pred_team_conds(call)
                edges = [a["condition_edge"] for a in ptc
                         if isinstance(a.get("condition_edge"), (int, float))]
                if edges:
                    call["avg_edge"] = round(sum(edges) / len(edges), 2)

            def _enrich_pw_condition_edges(call):
                """Add condition_edge to each active_condition entry (#217)."""
                ac = call.get("active_conditions")
                if not isinstance(ac, list):
                    return
                needs_edge = any(
                    isinstance(a, dict) and a.get("condition_edge") is None
                    for a in ac
                )
                if not needs_edge:
                    return
                try:
                    edge_store = outcomes_mod.ConditionEdgeStore.load_or_rebuild()
                except Exception:
                    return
                for a in ac:
                    if not isinstance(a, dict) or a.get("condition_edge") is not None:
                        continue
                    name = str(a.get("name") or "").strip()
                    if name:
                        a["condition_edge"] = round(edge_store.edge(name), 1)

            # Load config once for suppression checks (#221)
            _pw_supp_cfg = {}
            try:
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8") as _cf:
                        _pw_supp_cfg = json.load(_cf)
            except Exception:
                pass

            def _enrich_pw_suppressed(call):
                """Retroactively tag calls that would be suppressed by polarity/edge gates (#221).

                Only applies to legacy calls recorded before live suppression was added.
                If the call already has '_suppressed' (True or False), the live monitor
                made the authoritative decision — do not override (#231).
                """
                if "_suppressed" in call:
                    return  # live monitor already evaluated — trust its decision
                pol_net = call.get("polarity_net")
                avg_edge = call.get("avg_edge")
                # Polarity gate
                pol_min = _pw_supp_cfg.get("pw_polarity_gate", 0)
                if pol_min is not None and pol_net is not None and pol_net < pol_min:
                    call["_suppressed"] = True
                    call["_suppress_reason"] = "polarity_gate: net={:+d} (pos={}, neg={})".format(
                        pol_net, call.get("polarity_pos", 0), call.get("polarity_neg", 0))
                    return
                # Edge gate
                edge_min = _pw_supp_cfg.get("pw_edge_gate")
                if edge_min is not None and avg_edge is not None and avg_edge < float(edge_min):
                    call["_suppressed"] = True
                    call["_suppress_reason"] = "edge_gate: avg_edge={:+.1f} (threshold={})".format(avg_edge, edge_min)
                    return
                # 0-0 score — no scoring context (#183)
                _h_at = call.get("home_score_at_fire")
                _a_at = call.get("away_score_at_fire")
                if _h_at is not None and _a_at is not None and int(_h_at) == 0 and int(_a_at) == 0:
                    call["_suppressed"] = True
                    call["_suppress_reason"] = "0-0 score"

            try:
                # Parse filter params
                f_segment = (params.get("season_segment") or [""])[0].strip()
                f_season = (params.get("season_year") or [""])[0].strip()
                f_role = (params.get("spread_role") or [""])[0].strip()
                f_margin = (params.get("margin_at_fire") or [""])[0].strip()
                f_quarter = (params.get("quarter") or [""])[0].strip().upper()
                if f_quarter and f_quarter not in ("Q1", "Q2", "Q3", "Q4", "OT", "2OT"):
                    f_quarter = ""
                f_half_raw = (params.get("pw_half") or [""])[0].strip().upper()
                f_half = f_half_raw if f_half_raw in ("H1", "H2", "OT") else ""
                _f_half_quarters = {"H1": ("Q1", "Q2"), "H2": ("Q3", "Q4"), "OT": ("OT", "2OT")}.get(f_half, ())
                f_call_sel = (params.get("call_selection") or [""])[0].strip().lower()
                if f_call_sel not in ("first", "first_per_quarter", "first_per_half", "last"):
                    f_call_sel = ""  # default = all calls
                # Source/edge/polarity filters (#298)
                f_pw_source = (params.get("pw_source") or [""])[0].strip()
                f_pw_edge = (params.get("pw_edge") or [""])[0].strip()
                f_pw_polarity = (params.get("pw_polarity") or [""])[0].strip()
                _pw_cons_raw = (params.get("pw_consensus") or [""])[0].strip()
                f_pw_consensus = set(c.strip() for c in _pw_cons_raw.split(",") if c.strip()) if _pw_cons_raw else set()
                f_strict_bk = (params.get("strict_bk") or [""])[0].strip() == "1"
                has_filters = bool(f_segment or f_season or f_role or f_margin or f_half or f_quarter or f_pw_source or f_pw_edge or f_pw_polarity or f_pw_consensus or f_strict_bk)

                games = {}  # game_id -> {game_name, calls: [...]}
                _pregame_snaps = {}  # game_id -> snapshot from .alert_state.json

                # Determine if live_stats is fresh (monitor running within last 10 min)
                _live_stats_fresh = False
                try:
                    if os.path.exists(LIVE_STATS_FILE):
                        with open(LIVE_STATS_FILE, encoding="utf-8") as f:
                            _ls_check = json.load(f)
                        _ls_upd = _ls_check.get("updated", "")
                        if _ls_upd:
                            _ls_age = time.time() - datetime.fromisoformat(_ls_upd.replace("Z", "+00:00")).timestamp()
                            _live_stats_fresh = _ls_age < 600  # 10 minutes
                except Exception:
                    pass

                # Include pw_calls from .alert_state.json — tagged as "live" only if monitor is active
                # Skip when season filter excludes current season (#277)
                _skip_live_merge = bool(f_season) and f_season != _game_season_year({"date": datetime.now(timezone.utc).isoformat()})
                state_file = league_config.state_path(".alert_state.json")
                if os.path.exists(state_file) and not _skip_live_merge:
                    with open(state_file, encoding="utf-8") as f:
                        state = json.load(f)
                    for key, calls in state.items():
                        if key.startswith("pw_calls_") and isinstance(calls, list):
                            gid = key[len("pw_calls_"):]
                            _enriched = []
                            for _rc in calls:
                                _ec = dict(_rc)
                                _enrich_pw_condition_edges(_ec)
                                _enrich_pw_polarity(_ec)
                                _enrich_pw_avg_edge(_ec)
                                _enrich_pw_suppressed(_ec)
                                _enrich_pw_source_version(_ec)
                                _enriched.append(_ec)
                            games[gid] = {"game_id": gid, "game_name": gid, "calls": _enriched,
                                          "source": "live" if _live_stats_fresh else "recent"}
                    _snaps = state.get("pregame_snapshots") or {}
                    # Resolve game_name from pregame_snapshot for live/recent entries
                    for gid, g in games.items():
                        if g.get("game_name") == gid and gid in _snaps:
                            _s = _snaps[gid]
                            _hm = (_s.get("home_matchup_meta") or {}).get("name", "")
                            _am = (_s.get("away_matchup_meta") or {}).get("name", "")
                            if _hm and _am:
                                g["game_name"] = "{} vs {}".format(_am, _hm)
                    for gid_snap, snap_data in (_snaps).items():
                        if isinstance(snap_data, dict):
                            _pregame_snaps[str(gid_snap)] = snap_data

                if has_filters:
                    # When filters are active, pull from game_history.json
                    history = _load_history_cached()
                    for rec in history:
                        pw_calls = rec.get("predicted_winner_calls")
                        if not isinstance(pw_calls, list) or not pw_calls:
                            continue
                        # Apply filters (use same segment mapping as outcomes.py)
                        if f_segment:
                            rec_seg = str(rec.get("season_segment") or "")
                            if f_segment == "regular_season":
                                if rec_seg not in ("pre_allstar", "post_allstar"):
                                    continue
                            elif f_segment == "playoffs_finals":
                                if rec_seg not in ("playoffs", "finals"):
                                    continue
                            elif rec_seg != f_segment:
                                continue
                        if f_season and outcomes_mod._game_season_year(rec) != f_season:
                            continue
                        gid = str(rec.get("game_id") or "")
                        if not gid or gid in games:
                            continue  # live source takes precedence
                        # Resolve spread_fav_id for role/margin filtering
                        spread_fav_id = str(rec.get("spread_fav_id") or "")
                        snap = rec.get("pregame_snapshot") or {}
                        home_meta = snap.get("home_matchup_meta") or {}
                        away_meta = snap.get("away_matchup_meta") or {}
                        home_id = str(rec.get("home_id") or "")
                        away_id = str(rec.get("away_id") or "")
                        # Derive per-team odds from game-level spread_detail when
                        # pregame_snapshot is missing odds (e.g. WNBA backfill)
                        if spread_fav_id and not home_meta.get("spread"):
                            _sd = str(rec.get("spread_detail") or "")
                            _hml = str(rec.get("home_moneyline") or "")
                            _aml = str(rec.get("away_moneyline") or "")
                            if _sd:
                                import re as _re
                                _sp_match = _re.search(r'[+-]?\d+\.?\d*$', _sd)
                                _sp_val = _sp_match.group() if _sp_match else ""
                                if _sp_val:
                                    _opp_val = ("+" + _sp_val.lstrip("+-")) if _sp_val.startswith("-") else ("-" + _sp_val.lstrip("+"))
                                    if spread_fav_id == home_id:
                                        home_meta = dict(home_meta, spread=_sp_val, moneyline=_hml or "")
                                        away_meta = dict(away_meta, spread=_opp_val, moneyline=_aml or "")
                                    elif spread_fav_id == away_id:
                                        away_meta = dict(away_meta, spread=_sp_val, moneyline=_aml or "")
                                        home_meta = dict(home_meta, spread=_opp_val, moneyline=_hml or "")

                        filtered_calls = []
                        for c in pw_calls:
                            pred_id = str(c.get("predicted_team_id") or "")
                            # Role filter
                            if f_role:
                                if not spread_fav_id or not pred_id:
                                    continue  # can't determine role — skip
                                is_fav = pred_id == spread_fav_id
                                if f_role == "favorite" and not is_fav:
                                    continue
                                if f_role == "underdog" and is_fav:
                                    continue
                            # Margin filter
                            if f_margin:
                                m = c.get("score_margin_at_fire")
                                if m is None:
                                    continue
                                if f_margin == "trailing" and m > 0:
                                    continue
                                if f_margin == "leading" and m <= 0:
                                    continue
                            # Half filter
                            if f_half:
                                cq = str(c.get("quarter") or "").upper()
                                if f_half == "OT":
                                    if not cq.startswith("OT"):
                                        continue
                                elif cq not in _f_half_quarters:
                                    continue
                            # Quarter filter
                            if f_quarter:
                                cq = str(c.get("quarter") or "").upper()
                                if f_quarter == "OT":
                                    if not cq.startswith("OT"):
                                        continue
                                elif cq != f_quarter:
                                    continue
                            # Source/edge/polarity filters (#298)
                            if f_pw_source:
                                if f_pw_source.startswith("game_"):
                                    if rec.get("source", "") != f_pw_source[5:]:
                                        continue
                                elif f_pw_source.startswith("call_"):
                                    _is_syn = c.get("synthetic", True)
                                    if f_pw_source == "call_live" and _is_syn:
                                        continue
                                    if f_pw_source == "call_backfill" and not _is_syn:
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
                            if f_pw_consensus and str(c.get("consensus") or "") not in f_pw_consensus:
                                continue
                            # Live-priced filter: require genuine BK odds at fire time
                            if f_strict_bk:
                                if not c.get("bk_ml_source"):
                                    continue
                            # Enrich call with pregame data
                            enriched = dict(c)
                            _enrich_pw_polarity(enriched)
                            _enrich_pw_condition_edges(enriched)
                            _enrich_pw_avg_edge(enriched)
                            _enrich_pw_suppressed(enriched)
                            _enrich_pw_source_version(enriched)
                            _enrich_pw_devig(enriched, rec)
                            if pred_id and spread_fav_id:
                                enriched["spread_role"] = "favorite" if pred_id == spread_fav_id else "underdog"
                            # Add pregame odds
                            if pred_id == home_id:
                                enriched["moneyline"] = home_meta.get("moneyline")
                                enriched["spread"] = home_meta.get("spread")
                            elif pred_id == away_id:
                                enriched["moneyline"] = away_meta.get("moneyline")
                                enriched["spread"] = away_meta.get("spread")
                            filtered_calls.append(enriched)
                        if filtered_calls:
                            gname = "{} vs {}".format(
                                rec.get("away_name") or rec.get("away_abbr", "?"),
                                rec.get("home_name") or rec.get("home_abbr", "?"))
                            gdata = {"game_id": gid, "game_name": gname, "calls": filtered_calls, "source": "history",
                                     "game_start": str(rec.get("date") or ""),
                                     "home_abbr": str(rec.get("home_abbr") or ""),
                                     "away_abbr": str(rec.get("away_abbr") or "")}
                            _pw_add_result(gdata, rec)
                            games[gid] = gdata
                else:
                    # No filters — all history with pw_calls
                    try:
                        history = _load_history_cached()
                        for rec in reversed(history):
                            pw_calls = rec.get("predicted_winner_calls")
                            if not isinstance(pw_calls, list) or not pw_calls:
                                continue
                            gid = str(rec.get("game_id") or "")
                            if not gid or gid in games:
                                continue  # live source takes precedence
                            gname = "{} vs {}".format(
                                rec.get("away_name") or rec.get("away_abbr", "?"),
                                rec.get("home_name") or rec.get("home_abbr", "?"))
                            # Enrich calls with spread/odds from game record
                            _sfid = str(rec.get("spread_fav_id") or "")
                            _snap = rec.get("pregame_snapshot") or {}
                            _hmeta = _snap.get("home_matchup_meta") or {}
                            _ameta = _snap.get("away_matchup_meta") or {}
                            _hid = str(rec.get("home_id") or "")
                            _aid = str(rec.get("away_id") or "")
                            if _sfid and not _hmeta.get("spread"):
                                _sd = str(rec.get("spread_detail") or "")
                                if _sd:
                                    import re as _re
                                    _spm = _re.search(r'[+-]?\d+\.?\d*$', _sd)
                                    _spv = _spm.group() if _spm else ""
                                    if _spv:
                                        _opp = ("+" + _spv.lstrip("+-")) if _spv.startswith("-") else ("-" + _spv.lstrip("+"))
                                        if _sfid == _hid:
                                            _hmeta = dict(_hmeta, spread=_spv, moneyline=str(rec.get("home_moneyline") or ""))
                                            _ameta = dict(_ameta, spread=_opp, moneyline=str(rec.get("away_moneyline") or ""))
                                        elif _sfid == _aid:
                                            _ameta = dict(_ameta, spread=_spv, moneyline=str(rec.get("away_moneyline") or ""))
                                            _hmeta = dict(_hmeta, spread=_opp, moneyline=str(rec.get("home_moneyline") or ""))
                            _enriched_calls = []
                            for _c in pw_calls:
                                _ec = dict(_c)
                                _enrich_pw_polarity(_ec)
                                _enrich_pw_condition_edges(_ec)
                                _enrich_pw_avg_edge(_ec)
                                _enrich_pw_suppressed(_ec)
                                _enrich_pw_source_version(_ec)
                                _pid = str(_c.get("predicted_team_id") or "")
                                if _pid and _sfid:
                                    _ec["spread_role"] = "favorite" if _pid == _sfid else "underdog"
                                if _pid == _hid:
                                    _ec["moneyline"] = _hmeta.get("moneyline")
                                    _ec["spread"] = _hmeta.get("spread")
                                elif _pid == _aid:
                                    _ec["moneyline"] = _ameta.get("moneyline")
                                    _ec["spread"] = _ameta.get("spread")
                                _enrich_pw_devig(_ec, rec)  # must be after moneyline is set
                                _enriched_calls.append(_ec)
                            gdata = {"game_id": gid, "game_name": gname, "calls": _enriched_calls, "source": "history"}
                            _pw_add_result(gdata, rec)
                            games[gid] = gdata
                    except Exception:
                        pass

                # ── Common enrichment (runs for both filtered and unfiltered paths) ──

                # Pull game_start from ESPN scoreboard cache for live games
                try:
                    with _scoreboard_cache_lock:
                        _sb_payload = _scoreboard_cache.get("payload")
                    if _sb_payload:
                        for ev in (_sb_payload.get("events") or []):
                            eid = str(ev.get("id") or "")
                            if eid in games and not games[eid].get("game_start"):
                                ev_date = str(ev.get("date") or "")
                                if ev_date:
                                    games[eid]["game_start"] = ev_date
                except Exception:
                    pass
                # 4) Enrich with pregame odds from game_history or live pregame snapshots
                def _enrich_calls_with_snapshot(g, snap, h_id, a_id, sfid, h_name="", a_name=""):
                    """Enrich call dicts with spread_role, moneyline, spread from a pregame snapshot."""
                    h_meta = snap.get("home_matchup_meta") or {}
                    a_meta = snap.get("away_matchup_meta") or {}
                    for c in g["calls"]:
                        pid = str(c.get("predicted_team_id") or "")
                        if not pid:
                            pname = str(c.get("predicted_team") or "").strip().lower()
                            if pname and h_name and pname == h_name:
                                pid = h_id
                            elif pname and a_name and pname == a_name:
                                pid = a_id
                            if pid:
                                c["predicted_team_id"] = pid
                        if pid and sfid:
                            c["spread_role"] = "favorite" if pid == sfid else "underdog"
                        if pid == h_id:
                            c.setdefault("moneyline", h_meta.get("moneyline"))
                            c.setdefault("spread", h_meta.get("spread"))
                        elif pid == a_id:
                            c.setdefault("moneyline", a_meta.get("moneyline"))
                            c.setdefault("spread", a_meta.get("spread"))

                try:
                    _hist_by_gid = {}
                    try:
                        history = _load_history_cached()
                        _hist_by_gid = {str(r.get("game_id") or ""): r for r in history if r.get("game_id")}
                    except Exception:
                        pass
                    for gid, g in games.items():
                        rec = _hist_by_gid.get(gid)
                        if rec:
                            # Game in history — use its pregame snapshot
                            snap = rec.get("pregame_snapshot") or {}
                            h_id = str(rec.get("home_id") or "")
                            a_id = str(rec.get("away_id") or "")
                            sfid = str(rec.get("spread_fav_id") or "")
                            if g["game_name"] == gid:
                                g["game_name"] = "{} vs {}".format(
                                    rec.get("away_name") or "?", rec.get("home_name") or "?")
                            if not g.get("game_start"):
                                g["game_start"] = str(rec.get("date") or "")
                                g["home_abbr"] = str(rec.get("home_abbr") or "")
                            h_name = str(rec.get("home_name") or "").strip().lower()
                            a_name = str(rec.get("away_name") or "").strip().lower()
                            _enrich_calls_with_snapshot(g, snap, h_id, a_id, sfid, h_name, a_name)
                        elif gid in _pregame_snaps:
                            # Live game — use pregame snapshot from .alert_state.json
                            snap = _pregame_snaps[gid]
                            h_meta = snap.get("home_matchup_meta") or {}
                            a_meta = snap.get("away_matchup_meta") or {}
                            h_id = str(h_meta.get("team_id") or "")
                            a_id = str(a_meta.get("team_id") or "")
                            sfid = str(snap.get("spread_fav_id") or "")
                            h_name = str(h_meta.get("name") or "").strip().lower()
                            a_name = str(a_meta.get("name") or "").strip().lower()
                            _enrich_calls_with_snapshot(g, snap, h_id, a_id, sfid, h_name, a_name)
                except Exception:
                    pass  # Enrichment is best-effort
                # Add in-progress games from live_stats.json + enrich existing live games (#169)
                try:
                    if _live_stats_fresh and os.path.exists(LIVE_STATS_FILE):
                        with open(LIVE_STATS_FILE, encoding="utf-8") as f:
                            _ls_data = json.load(f)
                        _ls_games = _ls_data.get("games") or {}
                        for _ls_gid, ls in _ls_games.items():
                            if not isinstance(ls, dict):
                                continue
                            teams = ls.get("teams") or {}
                            home_t = next((t for t in teams.values() if isinstance(t, dict) and t.get("homeAway") == "home"), None)
                            away_t = next((t for t in teams.values() if isinstance(t, dict) and t.get("homeAway") == "away"), None)
                            if _ls_gid not in games:
                                gname = "{} vs {}".format(
                                    away_t.get("name") or away_t.get("abbr") or "?" if away_t else "?",
                                    home_t.get("name") or home_t.get("abbr") or "?" if home_t else "?")
                                games[_ls_gid] = {
                                    "game_id": _ls_gid, "game_name": gname, "calls": [],
                                    "source": "live",
                                    "quarter": ls.get("quarter", ""),
                                    "game_clock": ls.get("game_clock", ""),
                                    "home_abbr": home_t.get("abbr", "") if home_t else "",
                                }
                            else:
                                g = games[_ls_gid]
                                if g.get("game_name") == _ls_gid and home_t and away_t:
                                    g["game_name"] = "{} vs {}".format(
                                        away_t.get("name") or away_t.get("abbr") or "?",
                                        home_t.get("name") or home_t.get("abbr") or "?")
                                if not g.get("home_abbr") and home_t:
                                    g["home_abbr"] = home_t.get("abbr") or ""
                                # Always update live game state from live_stats (#358)
                                g["quarter"] = ls.get("quarter", "")
                                g["game_clock"] = ls.get("game_clock", "")
                                g["source"] = "live"
                except Exception:
                    pass
                # Update quarter/game_clock from ESPN scoreboard cache (#359)
                # Runs AFTER live_stats merge so scoreboard (fresher) wins
                _QUARTER_NAMES = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4", 5: "OT", 6: "2OT"}
                try:
                    with _scoreboard_cache_lock:
                        _sb_payload = _scoreboard_cache.get("payload")
                    if _sb_payload:
                        for ev in (_sb_payload.get("events") or []):
                            eid = str(ev.get("id") or "")
                            if eid not in games or games[eid].get("source") != "live":
                                continue
                            _ev_status = ev.get("status") or {}
                            _ev_state = (_ev_status.get("type") or {}).get("state", "")
                            if _ev_state == "in":
                                _ev_period = _ev_status.get("period")
                                _ev_clock = _ev_status.get("displayClock", "")
                                if _ev_period:
                                    games[eid]["quarter"] = _QUARTER_NAMES.get(_ev_period, "Q{}".format(_ev_period))
                                if _ev_clock:
                                    games[eid]["game_clock"] = _ev_clock
                except Exception:
                    pass
                # Derive game_start from earliest call ts when not set (#165)
                for g in games.values():
                    if not g.get("game_start"):
                        earliest = min((c.get("ts") or "" for c in g.get("calls", [])), default="")
                        if earliest:
                            g["game_start"] = earliest
                # Deduplicate calls within each game by timestamp
                for g in games.values():
                    seen = set()
                    deduped = []
                    for c in g["calls"]:
                        key = (c.get("ts"), c.get("predicted_team"), c.get("pct"))
                        if key not in seen:
                            seen.add(key)
                            deduped.append(c)
                    g["calls"] = sorted(deduped, key=lambda c: c.get("ts") or "", reverse=True)
                # Apply half filter to all sources
                if f_half:
                    for g in games.values():
                        if g.get("source") == "live" or not has_filters:
                            g["calls"] = [c for c in g["calls"]
                                          if (f_half == "OT" and str(c.get("quarter") or "").upper().startswith("OT"))
                                          or str(c.get("quarter") or "").upper() in _f_half_quarters]
                    games = {gid: g for gid, g in games.items() if g["calls"] or g.get("source") == "live"}
                # Apply quarter filter to all sources (live games aren't filtered earlier)
                if f_quarter:
                    for g in games.values():
                        if g.get("source") == "live" or not has_filters:
                            g["calls"] = [c for c in g["calls"]
                                          if (f_quarter == "OT" and str(c.get("quarter") or "").upper().startswith("OT"))
                                          or str(c.get("quarter") or "").upper() == f_quarter]
                    games = {gid: g for gid, g in games.items() if g["calls"] or g.get("source") == "live"}
                # Call selection filter (#183)
                if f_call_sel:
                    for g in games.values():
                        calls = g.get("calls") or []
                        if not calls:
                            continue
                        if f_call_sel == "first":
                            first = min(calls, key=lambda c: c.get("ts") or "")
                            g["calls"] = [first]
                        elif f_call_sel == "last":
                            last = max(calls, key=lambda c: c.get("ts") or "")
                            g["calls"] = [last]
                        elif f_call_sel == "first_per_quarter":
                            by_q = {}
                            for c in calls:
                                q = c.get("quarter", "")
                                if q not in by_q or (c.get("ts") or "") < (by_q[q].get("ts") or ""):
                                    by_q[q] = c
                            g["calls"] = sorted(by_q.values(), key=lambda c: c.get("ts") or "")
                        elif f_call_sel == "first_per_half":
                            by_h = {}
                            for c in calls:
                                q = str(c.get("quarter") or "").upper()
                                h = "H1" if q in ("Q1", "Q2") else "H2" if q in ("Q3", "Q4") else "OT"
                                if h not in by_h or (c.get("ts") or "") < (by_h[h].get("ts") or ""):
                                    by_h[h] = c
                            g["calls"] = sorted(by_h.values(), key=lambda c: c.get("ts") or "")
                # Recompute PW result from selected call (#183)
                if f_call_sel:
                    for g in games.values():
                        calls = g.get("calls") or []
                        if not calls or g.get("source") == "live":
                            continue
                        # Use last call in the (now-filtered) list as the representative
                        rep = max(calls, key=lambda c: c.get("ts") or "")
                        g["pw_correct"] = rep.get("correct")
                        g["pw_team"] = rep.get("predicted_team", "")
                        # Recompute spread cover from rep call's margin
                        pid = str(rep.get("predicted_team_id") or "")
                        h_id = str(g.get("home_id") or "")
                        a_id = str(g.get("away_id") or "")
                        hs = g.get("home_score")
                        aws = g.get("away_score")
                        spread_val = rep.get("spread")
                        if pid and spread_val is not None and hs is not None and aws is not None:
                            try:
                                sv = float(spread_val)
                                pw_actual = (int(hs) - int(aws)) if pid == h_id else (int(aws) - int(hs)) if pid == a_id else None
                                if pw_actual is not None:
                                    cv = pw_actual + sv
                                    g["pw_covered"] = cv > 0
                                    g["pw_cover_margin"] = round(cv, 1)
                            except (TypeError, ValueError):
                                pass
                # Compute live_line (always synthetic) and bk_live_line
                # (bookmaker) independently for each call (#213)
                for g in games.values():
                    for c in g["calls"]:
                        # Synthetic live line: pregame_spread - margin (#183)
                        spread = c.get("spread")
                        margin = c.get("score_margin_at_fire")
                        if spread is not None and margin is not None:
                            try:
                                c["live_line"] = round(float(spread) - float(margin), 1)
                            except (TypeError, ValueError):
                                pass
                        # Bookmaker live line: bk_spread directly (#213)
                        bk_sp = c.get("bk_spread")
                        if bk_sp is not None:
                            try:
                                c["bk_live_line"] = round(float(bk_sp), 1)
                            except (TypeError, ValueError):
                                pass
                # Scenario enrichment: stamp each call with scenario_key + scenario_stats (#352)
                # Uses ALL history with no filters so stats reflect full historical
                # profitability — stable regardless of the PW tab's current filters.
                try:
                    _sc_history = _load_history_cached()
                    _sc_stats = outcomes_mod.compute_scenario_stats(_sc_history)
                    for g in games.values():
                        for c in g.get("calls", []):
                            skey = outcomes_mod.scenario_key_from_call(c)
                            if skey:
                                c["scenario_key"] = skey
                                c["scenario_stats"] = _sc_stats.get(skey)
                except Exception:
                    pass  # Scenario enrichment is best-effort

                # Build fingerprint from source file mtimes so dashboard can skip re-render (#165)
                _fp_parts = []
                for _fp_path in [league_config.state_path(".alert_state.json"), ALERTS_FILE, HISTORY_FILE]:
                    try:
                        _fp_parts.append(str(os.path.getmtime(_fp_path)))
                    except OSError:
                        _fp_parts.append("0")
                _fp = "|".join(_fp_parts)
                # Live-priced metadata (#412 Phase 4.1)
                _all_pw_calls = [c for g in games.values() for c in g.get("calls", []) if c.get("correct") is not None]
                _any_synthetic = not f_strict_bk and any(
                    c.get("synthetic") is True or (c.get("synthetic") is None and not c.get("bk_ml_source")) for c in _all_pw_calls)
                self.send_json(200, {"games": list(games.values()), "fingerprint": _fp,
                                     "strict_bk": f_strict_bk, "has_synthetic_bk": _any_synthetic})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/paper-trading":
            # Forward-test paper-trading stats (#412 Phase 5.3)
            try:
                bets_path = league_config.state_path("bets.jsonl")
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
                # Enrich with outcomes from game_history
                history = _load_history_cached()
                game_map = {}
                for rec in history:
                    gid = str(rec.get("game_id") or "")
                    if gid:
                        game_map[gid] = rec
                # Load rules
                rules_path = os.path.join(SCRIPT_DIR, "rules.json")
                rules = {}
                if os.path.exists(rules_path):
                    with open(rules_path) as f:
                        for r in json.load(f).get("rules", []):
                            rules[r["id"]] = r
                # Compute per-rule stats
                rule_stats = {}
                for bet in bets:
                    rid = bet.get("rule_id", "unknown")
                    if rid not in rule_stats:
                        rule_info = rules.get(rid, {})
                        rule_stats[rid] = {
                            "rule_id": rid,
                            "rule_name": rule_info.get("name", rid),
                            "registered": rule_info.get("registered", ""),
                            "total": 0, "resolved": 0, "correct": 0,
                            "pnl": 0.0, "bk_pnl": 0.0, "bk_count": 0,
                            "ev_sum": 0.0, "ev_count": 0,
                            "clv_sum": 0.0, "clv_count": 0,
                            "bets": [],
                        }
                    st = rule_stats[rid]
                    st["total"] += 1
                    gid = str(bet.get("game_id") or "")
                    rec = game_map.get(gid)
                    enriched = dict(bet)
                    if rec:
                        winner = rec.get("winner_id") or ""
                        pred_id = ""
                        # Resolve predicted_team_id from call
                        for c in rec.get("predicted_winner_calls", []):
                            if c.get("predicted_team") == bet.get("predicted_team"):
                                pred_id = str(c.get("predicted_team_id") or "")
                                break
                        if winner and pred_id:
                            enriched["correct"] = (pred_id == winner)
                            st["resolved"] += 1
                            if enriched["correct"]:
                                st["correct"] += 1
                        # CLV: compare bet price to closing price
                        closing = None
                        home_id = str(rec.get("home_id") or "")
                        if pred_id == home_id:
                            closing = rec.get("closing_home_ml") or rec.get("home_moneyline")
                        else:
                            closing = rec.get("closing_away_ml") or rec.get("away_moneyline")
                        if closing and bet.get("price_at_signal"):
                            enriched["closing_price"] = closing
                    # EV
                    if bet.get("ev") is not None:
                        st["ev_sum"] += bet["ev"]
                        st["ev_count"] += 1
                    st["bets"].append(enriched)
                # Compute summary stats
                result = []
                for rid, st in rule_stats.items():
                    acc = st["resolved"] and st["correct"] / st["resolved"] * 100 or 0
                    mean_ev = st["ev_sum"] / st["ev_count"] * 100 if st["ev_count"] else None
                    result.append({
                        "rule_id": st["rule_id"],
                        "rule_name": st["rule_name"],
                        "registered": st["registered"],
                        "total_bets": st["total"],
                        "resolved": st["resolved"],
                        "correct": st["correct"],
                        "accuracy_pct": round(acc, 1),
                        "mean_ev_pct": round(mean_ev, 1) if mean_ev is not None else None,
                        "recent_bets": st["bets"][-20:],  # last 20
                    })
                self.send_json(200, {"rules": result, "total_bets": len(bets)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── PW Export — flat PW call feed for external consumers (#457) ──
        if path == "/api/pw-export":
            try:
                # Optional filters: since (ISO date), until (ISO date), source (live/backfill/all)
                f_since = (params.get("since") or [""])[0].strip()
                f_until = (params.get("until") or [""])[0].strip()
                f_source = (params.get("source") or [""])[0].strip().lower()  # live, backfill, all
                f_include_suppressed = (params.get("include_suppressed") or ["0"])[0].strip() == "1"

                history = _load_history_cached()

                # Also merge live in-progress calls from .alert_state.json
                live_pw_by_game = {}
                state_file = league_config.state_path(".alert_state.json")
                if os.path.exists(state_file):
                    try:
                        with open(state_file, encoding="utf-8") as f:
                            state = json.load(f)
                        _snaps = state.get("pregame_snapshots") or {}
                        for key, calls in state.items():
                            if key.startswith("pw_calls_") and isinstance(calls, list):
                                gid = key[len("pw_calls_"):]
                                snap = _snaps.get(gid) or {}
                                live_pw_by_game[gid] = {
                                    "calls": calls,
                                    "snap": snap,
                                }
                    except Exception:
                        pass

                # Completed game IDs — used to avoid duplicating live calls
                completed_gids = set()

                rows = []
                _season_year = outcomes_mod._game_season_year

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

                    gid = str(rec.get("game_id") or "")
                    completed_gids.add(gid)
                    game_ctx = {
                        "game_id": gid,
                        "game_date": game_date,
                        "home_team": str(rec.get("home_name") or rec.get("home_abbr") or ""),
                        "away_team": str(rec.get("away_name") or rec.get("away_abbr") or ""),
                        "home_id": str(rec.get("home_id") or ""),
                        "away_id": str(rec.get("away_id") or ""),
                        "home_abbr": str(rec.get("home_abbr") or ""),
                        "away_abbr": str(rec.get("away_abbr") or ""),
                        "home_score": rec.get("home_score"),
                        "away_score": rec.get("away_score"),
                        "winner_id": str(rec.get("winner_id") or ""),
                        "spread_fav_id": str(rec.get("spread_fav_id") or ""),
                        "season": _season_year(rec),
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

                # Append live in-progress calls not yet in history
                for gid, live_data in live_pw_by_game.items():
                    if gid in completed_gids:
                        continue
                    snap = live_data["snap"]
                    h_meta = snap.get("home_matchup_meta") or {}
                    a_meta = snap.get("away_matchup_meta") or {}
                    game_ctx = {
                        "game_id": gid,
                        "game_date": str(snap.get("game_date") or ""),
                        "home_team": h_meta.get("name", ""),
                        "away_team": a_meta.get("name", ""),
                        "home_id": str(h_meta.get("id") or ""),
                        "away_id": str(a_meta.get("id") or ""),
                        "home_abbr": str(h_meta.get("abbr") or ""),
                        "away_abbr": str(a_meta.get("abbr") or ""),
                        "home_score": None,
                        "away_score": None,
                        "winner_id": "",
                        "spread_fav_id": str(snap.get("spread_fav_id") or ""),
                        "season": "",
                        "season_segment": "",
                        "game_source": "live",
                        "game_completed": False,
                    }
                    for c in live_data["calls"]:
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
                    _cfg = {}
                    if os.path.exists(CONFIG_FILE):
                        with open(CONFIG_FILE, encoding="utf-8") as _cf:
                            _cfg = json.load(_cf)
                    if _cfg.get("pw_export_log_enabled"):
                        _access_path = os.path.join(SCRIPT_DIR, league_config.state_path("pw_export_access.jsonl"))
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
                    "league": league_config.LEAGUE,
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
            # Standalone scenario stats lookup for Live Games tab (#352)
            try:
                _sc_call_sel = (params.get("call_selection") or [""])[0].strip().lower()
                _sc_segment = (params.get("season_segment") or [""])[0].strip()
                _sc_season = (params.get("season_year") or [""])[0].strip()
                _sc_source = (params.get("source") or [""])[0].strip()
                _sc_strict = (params.get("strict_bk") or [""])[0].strip() == "1"
                history = outcomes_mod.load_history(migrate=False)
                scenarios = outcomes_mod.compute_scenario_stats(
                    history, call_selection=_sc_call_sel,
                    season_segment=_sc_segment, season_year=_sc_season,
                    source=_sc_source, strict_bk=_sc_strict)
                self.send_json(200, {"scenarios": scenarios})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/upcoming":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                self.send_json(200, build_upcoming_games_payload(cfg))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return


        if path == "/api/scoreboard":
            # Proxy ESPN scoreboard with server-side cache — avoids browser hitting ESPN directly
            try:
                now_ts = time.time()
                with _scoreboard_cache_lock:
                    if (
                        _scoreboard_cache["payload"] is not None
                        and (now_ts - _scoreboard_cache["ts"]) < SCOREBOARD_CACHE_TTL
                    ):
                        self.send_json(200, _scoreboard_cache["payload"])
                        return
                # Cache miss — fetch yesterday+today UTC to survive UTC rollover while
                # games from the previous UTC slate are still live.
                today_utc = datetime.now(timezone.utc).date()
                events = fetch_scoreboard_events_for_utc_date_range(today_utc - timedelta(days=1), today_utc)
                # Merge Summer League events when in SL window (#390) — NBA only, not WNBA
                if league_config.LEAGUE == "nba":
                  try:
                    with open(CONFIG_FILE, encoding="utf-8") as _cf:
                        _cfg = json.load(_cf)
                    _sl_enabled = _cfg.get("summer_league_enabled")
                    if _sl_enabled is True or (_sl_enabled is None and _cfg.get("summer_league_window")):
                        _sl_window = _cfg.get("summer_league_window", ["07-01", "07-25"])
                        _md = today_utc.strftime("%m-%d")
                        if len(_sl_window) == 2 and _sl_window[0] <= _md <= _sl_window[1]:
                            _sl_url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba-summer-las-vegas/scoreboard"
                            with urlopen(_sl_url, timeout=12) as _sl_resp:
                                _sl_data = json.loads(_sl_resp.read().decode("utf-8"))
                            _sl_events = _sl_data.get("events", [])
                            _existing_ids = {str(e.get("id", "")) for e in events}
                            for _sl_ev in _sl_events:
                                if str(_sl_ev.get("id", "")) not in _existing_ids:
                                    events.append(_sl_ev)
                  except Exception:
                    pass  # SL merge is best-effort
                payload = {
                    "events": events,
                    "updated": datetime.now(timezone.utc).isoformat(),
                }
                with _scoreboard_cache_lock:
                    _scoreboard_cache["payload"] = payload
                    _scoreboard_cache["ts"] = time.time()
                self.send_json(200, payload)
            except Exception as e:
                # Return stale cache if available on error, else 503
                with _scoreboard_cache_lock:
                    stale = _scoreboard_cache["payload"]
                if stale:
                    self.send_json(200, stale)
                else:
                    self.send_json(503, {"error": str(e), "events": []})
            return

        if path == "/api/ml-meta":
            try:
                if os.path.exists(ML_META_FILE):
                    with open(ML_META_FILE, encoding="utf-8") as f:
                        meta = json.load(f)
                    self.send_json(200, {"exists": True, "meta": meta})
                else:
                    self.send_json(200, {"exists": False, "meta": None})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/condition-edges":
            try:
                es = outcomes_mod.ConditionEdgeStore.load_or_rebuild()
                items = []
                for name, (fires, won) in es.conditions.items():
                    win_pct = round(won / fires * 100, 1) if fires > 0 else 0.0
                    edge = round(win_pct - 50.0, 1)
                    items.append({"name": name, "fires": fires, "won": won, "win_pct": win_pct, "edge": edge})
                items.sort(key=lambda x: x["win_pct"], reverse=True)
                self.send_json(200, {"conditions": items})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/odds-api-log":
            try:
                entries = odds_api_mod.load_log()
                # Optional ?exclude_type= to filter out entry types (#336)
                exclude_type = _param_first(params, "exclude_type", "")
                if exclude_type:
                    _ex = set(t.strip() for t in exclude_type.split(",") if t.strip())
                    entries = [e for e in entries if e.get("type") not in _ex]
                # Optional ?last=N parameter to limit entries
                last_param = (params.get("last") or [None])[0]
                if last_param:
                    try:
                        n = int(last_param)
                        entries = entries[-n:]
                    except (TypeError, ValueError):
                        pass
                # Mark fallback entries with saved data flag (#341)
                _fb_ts = set(e.get("ts", "") for e in odds_api_mod.load_fallback_data())
                # Mark call entries with request log data flag (#355)
                _rl_ts = set(e.get("ts", "") for e in odds_api_mod.load_request_log())
                for e in entries:
                    if e.get("type") == "fallback":
                        e["has_saved_data"] = e.get("ts", "") in _fb_ts
                    if e.get("type") == "call":
                        e["has_request_data"] = e.get("ts", "") in _rl_ts
                self.send_json(200, {"entries": entries, "total": len(entries),
                                     "request_log_size": odds_api_mod.request_log_size()})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        def _compute_bk_stats_from_log(log_entries):
            """Compute per-bookmaker market stats from log entries (#285)."""
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
                days = _param_first(params, "days", "")
                date_from = _param_first(params, "date_from", "")
                date_to = _param_first(params, "date_to", "")
                entries = odds_api_mod.load_log()
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
                from collections import defaultdict
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
                total_calls = len(calls)
                total_credits = sum(c.get("credits_used", 0) for c in calls)
                total_ok = sum(1 for c in calls if c.get("status") == 200)
                total_429 = sum(1 for c in calls if c.get("status") == 429)
                # Check which fallback events have saved data (#341)
                fb_events = [e for e in entries if e.get("type") == "fallback"]
                _fb_saved_ts = set(e.get("ts", "") for e in odds_api_mod.load_fallback_data())
                for fb in fb_events:
                    fb["has_saved_data"] = fb.get("ts", "") in _fb_saved_ts

                try:
                    with open(CONFIG_FILE, encoding="utf-8") as _cf:
                        _cfg_fb = json.load(_cf)
                except Exception:
                    _cfg_fb = {}
                self.send_json(200, {
                    "summary": {
                        "total_calls": total_calls,
                        "total_credits": total_credits,
                        "ok": total_ok,
                        "rate_limited": total_429,
                        "errors": total_calls - total_ok - total_429,
                        "credits_remaining": odds_api_mod.get_credit_status().get("credits_remaining"),
                    },
                    "by_context": context_stats,
                    "bookmaker_stats": _compute_bk_stats_from_log(entries),
                    "fallback_events": fb_events,
                    "entries_in_range": len(entries),
                    "credits": _get_odds_api_credits(),
                    "fallback_save_enabled": _cfg_fb.get("odds_api_fallback_save_enabled", False),
                    "fallback_retention_days": _cfg_fb.get("odds_api_fallback_retention_days", 7),
                    "request_log_enabled": _cfg_fb.get("odds_api_request_log_enabled", False),
                    "request_log_retention_days": _cfg_fb.get("odds_api_request_log_retention_days", 1),
                    "request_log_size": odds_api_mod.request_log_size(),
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Odds API Test Tool (#337) ─────────────────────────────────────

        if path == "/api/odds-api-test-events":
            params = parse_qs(parsed_url.query)
            sport = _param_first(params, "sport", "")
            if not sport:
                self.send_json(400, {"error": "sport param required"})
                return
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    pass
            api_key = cfg.get("odds_api_key", "")
            if not api_key:
                self.send_json(400, {"error": "No odds_api_key configured"})
                return
            try:
                import requests as _req
                # Use /events/ endpoint (free, 0 credits) — NOT /odds/ which costs per event (#337)
                url = "https://api.the-odds-api.com/v4/sports/{}/events/".format(sport)
                req_params = {"apiKey": api_key}
                import time as _tm
                _t0 = _tm.time()
                resp = _req.get(url, params=req_params, timeout=15)
                _latency = round((_tm.time() - _t0) * 1000, 1)
                credits_used = resp.headers.get("x-requests-last", "?")
                credits_remaining = resp.headers.get("x-requests-remaining", "?")
                try:
                    import odds_api as _oa
                    _oa._append_log({"type": "call", "ts": _tm.strftime("%Y-%m-%dT%H:%M:%SZ", _tm.gmtime()),
                        "sport": sport, "markets": "", "credits_used": int(credits_used) if str(credits_used).isdigit() else 0,
                        "credits_remaining": int(credits_remaining) if str(credits_remaining).isdigit() else None,
                        "status": resp.status_code, "latency_ms": _latency, "cache_hit": False, "context": "api_test_events"})
                    _oa._log_api_request(url, req_params, resp.status_code,
                                         resp.json() if resp.status_code == 200 else resp.text[:2000],
                                         "api_test_events", _latency)
                except Exception:
                    pass
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
            params = parse_qs(parsed_url.query)
            sport = _param_first(params, "sport", "")
            event_id = _param_first(params, "event_id", "")
            markets = _param_first(params, "markets", "h2h")
            if not sport or not event_id:
                self.send_json(400, {"error": "sport and event_id params required"})
                return
            cfg = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                except Exception:
                    pass
            api_key = cfg.get("odds_api_key", "")
            if not api_key:
                self.send_json(400, {"error": "No odds_api_key configured"})
                return
            try:
                import requests as _req
                url = "https://api.the-odds-api.com/v4/sports/{}/events/{}/odds/".format(sport, event_id)
                req_params = {"apiKey": api_key, "regions": "au", "markets": markets, "oddsFormat": "decimal"}
                import time as _tm
                _t0 = _tm.time()
                resp = _req.get(url, params=req_params, timeout=15)
                _latency = round((_tm.time() - _t0) * 1000, 1)
                credits_used = resp.headers.get("x-requests-last", "?")
                credits_remaining = resp.headers.get("x-requests-remaining", "?")
                try:
                    import odds_api as _oa
                    _oa._append_log({"type": "call", "ts": _tm.strftime("%Y-%m-%dT%H:%M:%SZ", _tm.gmtime()),
                        "sport": sport, "markets": markets, "credits_used": int(credits_used) if str(credits_used).isdigit() else 0,
                        "credits_remaining": int(credits_remaining) if str(credits_remaining).isdigit() else None,
                        "status": resp.status_code, "latency_ms": _latency, "cache_hit": False, "context": "api_test"})
                except Exception:
                    pass
                try:
                    body = resp.json()
                except Exception:
                    body = resp.text[:2000]
                try:
                    import odds_api as _oa2
                    _oa2._log_api_request(url, req_params, resp.status_code, body,
                                          "api_test", _latency, event_id=event_id)
                except Exception:
                    pass
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
            ts = _param_first(params, "ts", "")
            if ts:
                entry = odds_api_mod.get_fallback_data_entry(ts)
                if entry:
                    self.send_json(200, entry)
                else:
                    self.send_json(404, {"error": "No fallback data for timestamp"})
            else:
                entries = odds_api_mod.load_fallback_data()
                self.send_json(200, {"entries": entries, "count": len(entries)})
            return

        # Request log endpoints (#355)
        if path == "/api/odds-api-request-log":
            try:
                entries = odds_api_mod.load_request_log()
                ctx_filter = _param_first(params, "context", "")
                if ctx_filter:
                    entries = [e for e in entries if e.get("context") == ctx_filter]
                last_param = _param_first(params, "last", "")
                if last_param:
                    try:
                        entries = entries[-int(last_param):]
                    except (TypeError, ValueError):
                        pass
                # Strip response_body from list view for performance — client fetches via /data endpoint
                summary = []
                for e in entries:
                    s = {k: v for k, v in e.items() if k != "response_body"}
                    body = e.get("response_body")
                    s["response_size"] = len(json.dumps(body)) if body is not None else 0
                    summary.append(s)
                self.send_json(200, {"entries": summary, "total": len(summary),
                                     "file_size": odds_api_mod.request_log_size()})
            except Exception as ex:
                self.send_json(500, {"error": str(ex)})
            return

        if path == "/api/odds-api-request-log-data":
            ts = _param_first(params, "ts", "")
            if not ts:
                self.send_json(400, {"error": "ts param required"})
                return
            entry = odds_api_mod.get_request_log_entry(ts)
            if entry:
                self.send_json(200, entry)
            else:
                self.send_json(404, {"error": "No request log entry for timestamp"})
            return

        if path == "/api/model-eval":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE) as f:
                        cfg = json.load(f)
                target_list = params.get("target") or []
                payload = build_model_eval_payload(cfg=cfg, targets=target_list)

                persist = normalize_bool((params.get("persist") or ["0"])[0], False)
                force = normalize_bool((params.get("force") or ["0"])[0], False)
                if persist:
                    snapshot_settings = parse_model_eval_snapshot_settings(cfg)
                    payload["snapshot"] = append_model_eval_snapshot(
                        payload,
                        interval_seconds=snapshot_settings["interval_seconds"],
                        force=force,
                    )

                self.send_json(200, payload)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/model-eval-history":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                snapshot_settings = parse_model_eval_snapshot_settings(cfg)
                history = read_json_list(MODEL_EVAL_HISTORY_FILE)
                self.send_json(200, {
                    "updated": datetime.now(timezone.utc).isoformat(),
                    "file": MODEL_EVAL_HISTORY_FILE,
                    "snapshot_settings": snapshot_settings,
                    "max_entries": MODEL_EVAL_SNAPSHOT_MAX_ENTRIES,
                    "count": len(history),
                    "history": history,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/snapshots":
            try:
                backup_root = runtime_backup.DEFAULT_BACKUP_ROOT
                # League marker: any RUNTIME_FILES member present means this league owns the snapshot (#263)
                league_markers = set(runtime_backup.RUNTIME_FILES)
                snapshots = []
                if os.path.isdir(backup_root):
                    for name in sorted(os.listdir(backup_root), reverse=True):
                        if name.startswith("failed-run-"):
                            continue
                        full = os.path.join(backup_root, name)
                        if not os.path.isdir(full):
                            continue
                        # Filter: only show snapshots belonging to this league (#263)
                        if not any(os.path.exists(os.path.join(full, m)) for m in league_markers):
                            continue
                        meta_path = os.path.join(full, "metadata.json")
                        entry = {"dir": name, "created_at": None, "label": None, "files": []}
                        if os.path.exists(meta_path):
                            try:
                                with open(meta_path) as f:
                                    m = json.load(f)
                                entry["created_at"] = m.get("created_at")
                                entry["files"] = m.get("copied_files", [])
                                # Extract label from dir name: <timestamp>-<label>
                                parts = name.split("-", 1)
                                entry["label"] = parts[1] if len(parts) > 1 else None
                            except Exception:
                                pass
                        try:
                            entry["file_count"] = len([f for f in os.listdir(full) if os.path.isfile(os.path.join(full, f))])
                        except Exception:
                            entry["file_count"] = 0
                        snapshots.append(entry)
                self.send_json(200, {"snapshots": snapshots, "runtime_files": list(runtime_backup.RUNTIME_FILES)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/live-stats":
            try:
                if os.path.exists(LIVE_STATS_FILE):
                    with open(LIVE_STATS_FILE) as f:
                        result = json.load(f)
                else:
                    result = {}
                # Server-side stale check: clear game data if file is too old (#414)
                _updated_str = result.get("updated")
                if _updated_str:
                    try:
                        _updated_dt = datetime.fromisoformat(_updated_str.replace("Z", "+00:00"))
                        _age_min = (datetime.now(timezone.utc) - _updated_dt).total_seconds() / 60
                        _max_age = 15
                        try:
                            if os.path.exists(CONFIG_FILE):
                                with open(CONFIG_FILE, encoding="utf-8") as _cf:
                                    _cfg_max = json.load(_cf).get("live_stats_max_age_minutes")
                                if _cfg_max is not None:
                                    _max_age = max(5, int(_cfg_max))
                        except Exception:
                            pass
                        if _age_min > _max_age:
                            result["games"] = {}
                            result["stale"] = True
                            result["stale_age_minutes"] = round(_age_min)
                    except Exception:
                        pass
                # Inject cloud backup status into host object (#402)
                if isinstance(result.get("host"), dict):
                    result["host"]["cloud_backup"] = _get_cloud_backup_status()
                # Inject league metadata for dashboard display
                result["league"] = league_config.LEAGUE
                result["league_label"] = league_config.LEAGUE.upper()
                # Surface corruption alert if marker exists (#188)
                _corruption_marker = league_config.state_path("game_history.json") + ".corrupted_at"
                if os.path.exists(_corruption_marker):
                    try:
                        with open(_corruption_marker) as _mf:
                            _lines = _mf.read().strip().split("\n")
                        result.setdefault("alerts", []).append({
                            "level": "critical",
                            "message": "game_history.json was corrupted at {} — auto-recovered from backup. Check logs.".format(_lines[0] if _lines else "unknown"),
                        })
                    except OSError:
                        pass
                # Stale cloud backup alert (#403 Phase 4)
                _cb = result.get("host", {}).get("cloud_backup", {})
                if _cb.get("available") and _cb.get("last_run"):
                    try:
                        _last = datetime.strptime(_cb["last_run"], "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)
                        _age_hours = (datetime.now(timezone.utc) - _last).total_seconds() / 3600
                        if _age_hours > 36:
                            result.setdefault("alerts", []).append({
                                "level": "warning",
                                "message": "Cloud backup is {:.0f}h old (last: {}). Check cloud-backup.timer or run manually.".format(_age_hours, _cb["last_run"]),
                            })
                    except Exception:
                        pass
                elif _cb.get("available") and _cb.get("status") == "errors":
                    result.setdefault("alerts", []).append({
                        "level": "warning",
                        "message": "Last cloud backup completed with errors ({} errors). Check logs/cloud_backup.log.".format(_cb.get("error_count", "?")),
                    })
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/ml-training-status":
            try:
                self.send_json(200, _get_ml_training_status())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path.startswith("/api/ml-training-log/"):
            filename = path[len("/api/ml-training-log/"):]
            try:
                self.send_json(200, _get_ml_training_log(filename))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/cloud-backup/history":
            try:
                last_n = int((params.get("last") or ["20"])[0])
                self.send_json(200, {"runs": _get_cloud_backup_history(last_n)})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/recovery/backups":
            try:
                backups = outcomes_mod.list_backups()
                wal = outcomes_mod.wal_status()
                self.send_json(200, {"backups": backups, "wal": wal})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/ticker":
            try:
                if os.path.exists(TICKER_FILE):
                    with open(TICKER_FILE) as f:
                        self.send_json(200, json.load(f))
                else:
                    self.send_json(200, {"updated": None, "games": {}})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/log-outputs":
            try:
                self.send_json(200, detect_log_outputs_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/log-tail":
            req_path = (params.get("path") or [""])[0]
            req_bytes = (params.get("bytes") or [""])[0]
            payload = read_log_tail_bytes(req_path, req_bytes)
            self.send_json(200 if payload.get("ok") else 400, payload)
            return

        if path == "/api/monitor-timer":
            try:
                self.send_json(200, get_monitor_timer_payload())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/monitor-timer-auto":
            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                self.send_json(200, get_monitor_timer_auto_payload(cfg))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/monitor-log-events":
            try:
                _limit = (params.get("limit") or [None])[0]
                _logical_name = _param_first(params, "logical_name", "")
                _date_from = _param_first(params, "date_from", "")
                _date_to = _param_first(params, "date_to", "")
                self.send_json(200, get_monitor_log_events(limit=_limit or 500, logical_name=_logical_name, date_from=_date_from, date_to=_date_to))
            except Exception as e:
                self.send_json(500, {"error": str(e), "events": []})
            return

        if path == "/api/custom-queries":
            try:
                self.send_json(200, build_custom_queries_payload(params))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        # ── Static files ───────────────────────────────────────────
        if path == "/" or path == "":
            path = "/dashboard.html"

        file_path = os.path.realpath(os.path.join(SCRIPT_DIR, path.lstrip("/")))
        script_root = os.path.realpath(SCRIPT_DIR)
        if not file_path.startswith(script_root + os.sep) and file_path != script_root:
            self.send_json(403, {"error": "Forbidden"})
            return
        # Allowlist static files — block config.json and other sensitive files (#412 Phase 2.2)
        _fname = os.path.basename(file_path)
        if _fname not in STATIC_ALLOWLIST:
            self.send_json(403, {"error": "Forbidden"})
            return
        if os.path.isfile(file_path):
            ext = os.path.splitext(file_path)[1].lower()
            mime = MIME.get(ext, "application/octet-stream")
            try:
                with open(file_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", len(body))
                # Prevent browser from caching dashboard.html so JS/CSS changes take effect immediately
                if file_path.endswith(".html"):
                    self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif path == "/api/summer-league-odds":
            try:
                mon = _get_sl_monitor()
                self.send_json(200, mon.status())
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        elif path == "/api/summer-league-odds/history":
            try:
                mon = _get_sl_monitor()
                game_id = _param_first(params, "game_id", "")
                self.send_json(200, mon.get_history(game_id or None))
            except Exception as e:
                self.send_json(500, {"error": str(e)})
        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = self.path.split("?")[0]

        if path == "/api/recovery/restore":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length)) if content_length else {}
                raw_name = str(body.get("backup", "")).strip()
                do_wal = body.get("replay_wal", True)
                backup_name, err = _safe_backup_name(raw_name, runtime_backup.DEFAULT_BACKUP_ROOT)
                if err:
                    self.send_json(400, {"error": err})
                    return
                result = outcomes_mod.restore_from_backup(backup_name, replay_wal_entries=do_wal)
                self.send_json(200 if result["success"] else 500, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/recovery/delete-backup":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length)) if content_length else {}
                # Support single (backup) or batch (backups) delete (#279)
                names = body.get("backups") or []
                if not names and body.get("backup"):
                    names = [body["backup"]]
                if not names:
                    self.send_json(400, {"error": "backup name(s) required"})
                    return
                import shutil
                _history_basename = os.path.basename(outcomes_mod.HISTORY_FILE)
                deleted = []
                errors = []
                for name in names:
                    name = str(name).strip()
                    if not name or ".." in name or "/" in name:
                        errors.append("{}: invalid name".format(name))
                        continue
                    backup_dir = os.path.join(runtime_backup.DEFAULT_BACKUP_ROOT, name)
                    if not os.path.isdir(backup_dir):
                        errors.append("{}: not found".format(name))
                        continue
                    # Only allow deleting backups belonging to this league (#246)
                    if not os.path.exists(os.path.join(backup_dir, _history_basename)):
                        errors.append("{}: belongs to a different league".format(name))
                        continue
                    try:
                        shutil.rmtree(backup_dir)
                        deleted.append(name)
                    except Exception as e:
                        errors.append("{}: {}".format(name, e))
                self.send_json(200, {"success": len(deleted) > 0, "deleted": deleted, "errors": errors})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/recovery/prune-backups":
            try:
                content_length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(content_length)) if content_length else {}
                retention_days = int(body.get("retention_days", 60))
                if retention_days < 1:
                    self.send_json(400, {"error": "retention_days must be >= 1"})
                    return
                import shutil
                backup_root = runtime_backup.DEFAULT_BACKUP_ROOT
                if not os.path.isdir(backup_root):
                    self.send_json(200, {"success": True, "pruned": [], "kept": 0})
                    return
                _history_basename = os.path.basename(outcomes_mod.HISTORY_FILE)
                cutoff = time.time() - (retention_days * 86400)
                pruned = []
                kept = 0
                for name in os.listdir(backup_root):
                    full = os.path.join(backup_root, name)
                    if not os.path.isdir(full) or name.startswith("failed-run-"):
                        continue
                    # Only prune backups belonging to this league (#246)
                    if not os.path.exists(os.path.join(full, _history_basename)):
                        continue
                    mtime = os.path.getmtime(full)
                    if mtime < cutoff:
                        shutil.rmtree(full)
                        pruned.append(name)
                    else:
                        kept += 1
                self.send_json(200, {"success": True, "pruned": pruned, "kept": kept,
                                     "retention_days": retention_days})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/ml-train":
            with _ml_train_lock:
                if _ml_train_state["status"] == "running":
                    self.send_json(409, {"error": "Training already in progress", **_ml_train_state})
                    return
            t = threading.Thread(target=_do_ml_train_background, name="ml-train", daemon=True)
            t.start()
            with _ml_train_lock:
                self.send_json(202, dict(_ml_train_state))
            return

        if path == "/api/generate-roi-report":
            try:
                _nrl_dir = os.path.join(os.path.expanduser("~"), ".openclaw/workspace/scripts/nrl-monitor")
                if _nrl_dir not in sys.path:
                    sys.path.insert(0, _nrl_dir)
                import importlib
                import pw_roi_report
                importlib.reload(pw_roi_report)
                pw_roi_report.generate_report(copy_to_nba=True)
                self.send_json(200, {"ok": True, "status": "generated", "path": "pw_roi_report.html"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/generate-bk-odds-report":
            try:
                import importlib
                import analyse_bk_odds
                importlib.reload(analyse_bk_odds)
                analyse_bk_odds.generate_report()
                self.send_json(200, {"ok": True, "status": "generated", "path": "bk_odds_analysis_report.html"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/test-pw-alert":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                source = body.get("source", "sample")
                game_id = str(body.get("game_id", "")).strip()

                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg = json.load(f)
                from monitor import send_alert_result
                now = datetime.now(timezone.utc)
                ts = now.strftime("%b %d %I:%M %p ET")
                league = league_config.LEAGUE.upper()

                msg = None

                if source == "history":
                    records = _load_history_cached()
                    # Filter to games with PW calls
                    pw_games = [r for r in records if r.get("predicted_winner_calls")]
                    if not pw_games:
                        self.send_json(400, {"error": "No games with PW calls in history"})
                        return
                    if game_id:
                        match = [r for r in pw_games if str(r.get("game_id")) == game_id]
                        if not match:
                            self.send_json(400, {"error": "Game {} not found or has no PW calls".format(game_id)})
                            return
                        rec = match[0]
                    else:
                        import random
                        rec = random.choice(pw_games)
                    calls = rec["predicted_winner_calls"]
                    c = calls[0]
                    game_label = "{} @ {}".format(rec.get("away_name", "Away"), rec.get("home_name", "Home"))
                    stats_parts = []
                    if c.get("away_score_at_fire") is not None and c.get("home_score_at_fire") is not None:
                        stats_parts.append("Score: {}-{}".format(c["away_score_at_fire"], c["home_score_at_fire"]))
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
                    msg = (
                        "_{ts}_\n"
                        ":basketball: :dart: *Predicted Winner* \u2014 {pred}\n"
                        "{pct}% win probability{blended}{consensus}\n"
                        "Game: {game} \u00b7 {quarter}\n"
                        "{stats}\n"
                        "\u2014 _This is a test alert (from history: {gid})_"
                    ).format(ts=ts, pred=c.get("predicted_team", "?"),
                             pct="{:.0f}".format(c.get("pct", 0)),
                             blended=blend_tag, consensus=consensus_tag,
                             game=game_label, quarter=c.get("quarter", "?"),
                             stats=stats_line, gid=rec.get("game_id", "?"))

                elif source == "upcoming":
                    upcoming = build_upcoming_games_payload(cfg)
                    games = upcoming.get("games", [])
                    if not games:
                        self.send_json(400, {"error": "No upcoming games found"})
                        return
                    if game_id:
                        match = [g for g in games if str(g.get("id")) == game_id]
                        if not match:
                            self.send_json(400, {"error": "Upcoming game {} not found".format(game_id)})
                            return
                        ug = match[0]
                    else:
                        import random
                        ug = random.choice(games)
                    home = ug.get("home", {})
                    away = ug.get("away", {})
                    game_label = "{} @ {}".format(away.get("name", "Away"), home.get("name", "Home"))
                    pred_team = home.get("name", "Home")
                    msg = (
                        "_{ts}_\n"
                        ":basketball: :dart: *Predicted Winner* \u2014 {pred}\n"
                        "78% win probability \u00b7 ML+Hist \u00b7 consensus: strong\n"
                        "Game: {game} \u00b7 Q3 4:32\n"
                        "Score: 72-81 \u00b7 Margin: +9 \u00b7 favorite \u00b7 Pol: +3 (4+ 1\u2212) \u00b7 Edge: +5.2\n"
                        "\u2014 _This is a test alert (upcoming: {gid})_"
                    ).format(ts=ts, pred=pred_team, game=game_label,
                             gid=ug.get("id", "?"))

                else:  # sample
                    if league == "WNBA":
                        pred_team, game_label = "Las Vegas Aces", "Seattle Storm @ Las Vegas Aces"
                        score, margin, role = "58-64", "+6", "favorite"
                    else:
                        pred_team, game_label = "Boston Celtics", "Miami Heat @ Boston Celtics"
                        score, margin, role = "72-81", "+9", "favorite"
                    msg = (
                        "_{ts}_\n"
                        ":basketball: :dart: *Predicted Winner* \u2014 {pred}\n"
                        "78% win probability \u00b7 ML+Hist \u00b7 consensus: strong\n"
                        "Game: {game} \u00b7 Q3 4:32\n"
                        "Score: {score} \u00b7 Margin: {margin} \u00b7 {role} \u00b7 Pol: +3 (4+ 1\u2212) \u00b7 Edge: +5.2\n"
                        "\u2014 _This is a test alert_"
                    ).format(ts=ts, pred=pred_team, game=game_label,
                             score=score, margin=margin, role=role)

                ok, err = send_alert_result(cfg, msg)
                if ok:
                    self.send_json(200, {"ok": True, "message": "Test PW alert sent", "source": source})
                else:
                    self.send_json(500, {"ok": False, "error": err or "Send failed"})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/monitor-timer":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body)
            except Exception as e:
                self.send_json(400, {"error": "Invalid JSON: {}".format(e)})
                return

            if not isinstance(payload, dict):
                self.send_json(400, {"error": "Payload must be an object"})
                return

            result, update_err = update_monitor_timer(payload)
            if update_err:
                self.send_json(400, {"error": update_err})
                return

            self.send_json(200, result)
            return

        if path == "/api/monitor-timer-auto":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            payload = {}
            if body:
                try:
                    payload = json.loads(body)
                except Exception as e:
                    self.send_json(400, {"error": "Invalid JSON: {}".format(e)})
                    return

            if not isinstance(payload, dict):
                self.send_json(400, {"error": "Payload must be an object"})
                return

            try:
                cfg = {}
                if os.path.exists(CONFIG_FILE):
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        cfg = json.load(f)
                force = normalize_bool(payload.get("force"), False)
                result = auto_update_monitor_timer_from_schedule(cfg, force=force, reason="manual")
                status_code = 200 if result.get("ok") else 400
                self.send_json(status_code, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/config":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                new_cfg = json.loads(body)
                # Re-merge masked secrets from the current config (#412 Phase 2.5)
                try:
                    with open(CONFIG_FILE) as _cf:
                        _cur = json.load(_cf)
                    for k in SECRET_KEYS:
                        if new_cfg.get(k) in (None, "", "***") and k in _cur:
                            new_cfg[k] = _cur[k]
                except Exception:
                    pass
                validation_errors = validate_config(new_cfg)
                if validation_errors:
                    self.send_json(400, {
                        "error": "Invalid config payload",
                        "details": validation_errors
                    })
                    return
                # Write atomically FIRST, then snapshot in background
                tmp = CONFIG_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(new_cfg, f, indent=2)
                os.replace(tmp, CONFIG_FILE)
                _log("[CONFIG] Saved via dashboard")

                # Update fallback save config (#341) and request log (#355)
                try:
                    odds_api_mod.configure_fallback_save(
                        new_cfg.get("odds_api_fallback_save_enabled", False),
                        new_cfg.get("odds_api_fallback_retention_days", 7))
                    odds_api_mod.configure_request_log(
                        new_cfg.get("odds_api_request_log_enabled", False),
                        new_cfg.get("odds_api_request_log_retention_days", 1))
                except Exception:
                    pass

                # Update Summer League odds monitor config (#384)
                try:
                    mon = _get_sl_monitor()
                    mon.update_config(new_cfg)
                except Exception:
                    pass

                self.send_json(200, {"ok": True})

                # Pre-save snapshot in background (best-effort, non-blocking)
                def _bg_snapshot():
                    try:
                        snap_dir, _ = runtime_backup.snapshot_runtime_files(label="pre-config-save")
                        _log(f"[CONFIG] Pre-save snapshot: {snap_dir}")
                        runtime_backup.prune_old_snapshots(keep_last=10, label_filter="pre-config-save")
                    except Exception as snap_exc:
                        _log(f"[CONFIG] Pre-save snapshot failed (non-fatal): {snap_exc}")
                threading.Thread(target=_bg_snapshot, daemon=True).start()
            except Exception as e:
                self.send_json(400, {"error": str(e)})
            return

        if path == "/api/cloud-backup/run":
            try:
                if _cloud_backup_proc and _cloud_backup_proc.poll() is None:
                    self.send_json(409, {"ok": False, "error": "Cloud backup already running"})
                    return
                script = os.path.join(SCRIPT_DIR, "cloud_backup.sh")
                if not os.path.isfile(script):
                    self.send_json(404, {"ok": False, "error": "cloud_backup.sh not found"})
                    return
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

        if path == "/api/snapshots/create":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                req = json.loads(body) if body.strip() else {}
                label = str(req.get("label") or "manual").strip() or "manual"
                snap_dir, snap_meta = runtime_backup.snapshot_runtime_files(label=label)
                self.send_json(200, {
                    "ok": True,
                    "snapshot_dir": os.path.basename(snap_dir),
                    "copied_files": snap_meta.get("copied_files", []),
                    "missing_files": snap_meta.get("missing_files", []),
                    "created_at": snap_meta.get("created_at"),
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/summer-league-odds/start":
            try:
                mon = _get_sl_monitor()
                try:
                    with open(CONFIG_FILE, encoding="utf-8") as f:
                        mon.update_config(json.load(f))
                except Exception:
                    pass
                started = mon.start()
                self.send_json(200, {"started": started, **mon.status()})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        if path == "/api/summer-league-odds/stop":
            try:
                mon = _get_sl_monitor()
                mon.stop()
                self.send_json(200, {"stopped": True, **mon.status()})
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return

        self.send_json(404, {"error": "Not found"})


class ThreadingHTTPServer(HTTPServer):
    """HTTP server with a fixed-size thread pool (#423).

    Replaces ThreadingMixIn (unbounded threads) with a ThreadPoolExecutor
    to cap concurrent request threads. Prevents memory accumulation from
    thread-local data during live game polling (15s PW Live + 60s refresh).
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


def _warmup_cache():
    """Pre-warm the outcomes + model-eval + history caches on startup.

    Both compute_outcomes (~5s) and build_model_eval_payload (~47s) are
    expensive on first call. Running them in a background thread means the
    cache is hot by the time the user opens the Analysis tab.
    """
    try:
        import time as _time
        t0 = _time.time()
        _log("[NBA Monitor] Warming caches (outcomes + model-eval)...")
        # Load config first so warmup uses same params as the /api/outcomes handler
        cfg = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE) as _f:
                    cfg = json.load(_f)
            except Exception:
                pass
        try:
            _warmup_min_sample = min(200, max(1, int(cfg.get("outcomes_min_sample", outcomes_mod.MIN_SAMPLE))))
        except (TypeError, ValueError):
            _warmup_min_sample = outcomes_mod.MIN_SAMPLE
        try:
            _warmup_decay = min(1.0, max(0.0, float(cfg.get("outcomes_decay_lambda", 0.0))))
        except (TypeError, ValueError):
            _warmup_decay = 0.0
        try:
            _warmup_max_combo = max(2, min(5, int(cfg.get("outcomes_max_combo_size", 3))))
        except (TypeError, ValueError):
            _warmup_max_combo = 3
        outcomes_mod.compute_outcomes(_warmup_min_sample, decay_lambda=_warmup_decay, max_combo_size=_warmup_max_combo, season_segment="regular_season")
        _load_history_cached()
        # Pre-warm scoreboard cache so /api/monitor-timer-auto and /api/upcoming don't hit ESPN cold
        try:
            today_utc = datetime.now(timezone.utc).date()
            _sb_events = fetch_scoreboard_events_for_utc_date_range(today_utc - timedelta(days=1), today_utc)
            _sb_payload = {"events": _sb_events, "updated": datetime.now(timezone.utc).isoformat()}
            with _scoreboard_cache_lock:
                _scoreboard_cache["payload"] = _sb_payload
                _scoreboard_cache["ts"] = _time.time()
        except Exception:
            pass  # non-fatal, just means first ESPN-dependent request will fetch live
        t1 = _time.time()
        _log(f"[NBA Monitor] outcomes + scoreboard cache done in {t1-t0:.1f}s — warming upcoming + model-eval...")
        # Pre-warm upcoming games cache (includes EVAnalytics + ESPN fetches)
        try:
            build_upcoming_games_payload(cfg)
        except Exception:
            pass  # non-fatal
        build_model_eval_payload(cfg)
        _log(f"[NBA Monitor] All caches warm in {_time.time()-t0:.1f}s total")
    except Exception as exc:
        _log(f"[NBA Monitor] Cache warm-up failed (non-fatal): {exc}")


if __name__ == "__main__":
    # Derive league-specific ESPN URL from config
    try:
        with open(CONFIG_FILE, encoding="utf-8") as _cf:
            _startup_league = json.load(_cf).get("league", "nba")
        if _startup_league in VALID_LEAGUES:
            ESPN_SCOREBOARD_URL = _espn_scoreboard_url(_startup_league)
    except Exception:
        pass  # keep default

    # Configure fallback data saving (#341) and request logging (#355)
    # configure_* calls are synchronous (fast, needed before server starts so log writes work)
    # prune_* calls are moved to a background thread — they can block for 60-80s on large files (#451)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as _cf:
            _startup_cfg = json.load(_cf)
        odds_api_mod.configure_fallback_save(
            _startup_cfg.get("odds_api_fallback_save_enabled", False),
            _startup_cfg.get("odds_api_fallback_retention_days", 7))
        odds_api_mod.configure_request_log(
            _startup_cfg.get("odds_api_request_log_enabled", False),
            _startup_cfg.get("odds_api_request_log_retention_days", 1))
    except Exception as _e:
        _log(f"WARN: Could not configure fallback/request log: {_e}")

    def _prune_odds_logs():
        try:
            _pruned = odds_api_mod.prune_fallback_data()
            if _pruned:
                _log(f"[ODDS_API] Pruned {_pruned} old fallback data entries")
            _req_pruned = odds_api_mod.prune_request_log()
            if _req_pruned:
                _log(f"[ODDS_API] Pruned {_req_pruned} old request log entries")
        except Exception as _pe:
            _log(f"WARN: Could not prune fallback/request log: {_pe}")
    threading.Thread(target=_prune_odds_logs, name="odds-log-prune", daemon=True).start()

    worker = threading.Thread(target=model_eval_auto_snapshot_worker, name="model-eval-auto-snapshot", daemon=True)
    worker.start()
    timer_worker = threading.Thread(target=monitor_timer_auto_worker, name="monitor-timer-auto", daemon=True)
    timer_worker.start()
    log_rotation_worker = threading.Thread(target=_log_rotation_worker, name="log-rotation", daemon=True)
    log_rotation_worker.start()
    warmup_worker = threading.Thread(target=_warmup_cache, name="cache-warmup", daemon=True)
    warmup_worker.start()

    # Memory watchdog — auto-restart when RSS exceeds threshold (#423)
    # Raised from 512 → 768 MB: startup cache warming peaks at ~518 MB on NBA, 512 was too tight (#451)
    _RSS_LIMIT_MB = 768
    def _memory_watchdog():
        import resource
        while True:
            time.sleep(60)
            rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
            if rss_mb > _RSS_LIMIT_MB:
                _log(f"[MEMORY_WATCHDOG] RSS={rss_mb:.0f}MB exceeds {_RSS_LIMIT_MB}MB — restarting")
                os._exit(1)  # systemd will restart the service
    _mem_worker = threading.Thread(target=_memory_watchdog, name="memory-watchdog", daemon=True)
    _mem_worker.start()

    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)  # #412 Phase 2.4 — loopback only
    _log(f"[NBA Monitor] Dashboard server running on http://0.0.0.0:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        _log("[NBA Monitor] Server stopped.")
