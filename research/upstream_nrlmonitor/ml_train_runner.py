#!/usr/bin/env python3
"""
ml_train_runner.py

Wrapper around ml_model.train_model() that adds:
- Staleness check (skip if trained within N days)
- Hash check (skip if game_history.json unchanged)
- Per-run log capture to logs/ml_train_<timestamp>.log
- Training history persistence (ml_training_history.json)
- Retention pruning (logs + history entries)
- Dashboard log notification

Adapted from NBA Monitor ml_train_runner.py for NRL parity (#165).

CLI:
    python3 ml_train_runner.py              # scheduled (respects interval + hash)
    python3 ml_train_runner.py --force      # manual (skips checks)
"""

import argparse
import glob
import hashlib
import io
import json
import os
import sys
import time
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
META_FILE = os.path.join(SCRIPT_DIR, "model_meta_nrl.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
TRAINING_HISTORY_FILE = os.path.join(SCRIPT_DIR, "ml_training_history.json")
LOCK_FILE = os.path.join(SCRIPT_DIR, "ml_train.lock")
LOGS_DIR = os.path.join(SCRIPT_DIR, "logs")
DASHBOARD_LOG = os.path.join(LOGS_DIR, "dashboard.log")

DEFAULT_INTERVAL_DAYS = 7
DEFAULT_RETENTION_COUNT = 10


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _load_config():
    cfg = _load_json(CONFIG_FILE)
    return cfg if isinstance(cfg, dict) else {}


def _history_hash():
    """Compute SHA-256 of game_history.json (same approach as server._game_history_hash)."""
    try:
        with open(HISTORY_FILE, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def _load_meta():
    meta = _load_json(META_FILE)
    return meta if isinstance(meta, dict) else {}


def _load_training_history():
    data = _load_json(TRAINING_HISTORY_FILE)
    return data if isinstance(data, list) else []


def _save_training_history(entries):
    tmp = TRAINING_HISTORY_FILE + ".tmp.{}".format(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, TRAINING_HISTORY_FILE)


def _acquire_lock():
    """Simple PID-based lock. Returns True if acquired."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            # Check if process is still running
            os.kill(pid, 0)
            return False  # process alive, lock held
        except (ValueError, OSError):
            pass  # stale lock
    with open(LOCK_FILE, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock():
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


def _dashboard_log(msg):
    """Append a single line to dashboard.log."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = "{} {}\n".format(ts, msg)
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        with open(DASHBOARD_LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _prune_history(entries, retention):
    """Keep last `retention` trained/error entries + last `retention` skipped entries separately.

    Prevents skipped entries from pushing out valuable training records.
    """
    trained = [e for e in entries if not (e.get("result") or "").startswith("skipped")]
    skipped = [e for e in entries if (e.get("result") or "").startswith("skipped")]

    kept_trained = trained[-retention:] if len(trained) > retention else trained
    kept_skipped = skipped[-retention:] if len(skipped) > retention else skipped

    kept = sorted(kept_trained + kept_skipped, key=lambda e: e.get("timestamp") or "")
    kept_logs = {e.get("log_file") for e in kept if e.get("log_file")}

    all_kept_set = set(id(e) for e in kept)
    for entry in entries:
        if id(entry) not in all_kept_set:
            log_file = entry.get("log_file")
            if log_file and log_file not in kept_logs:
                log_path = os.path.join(LOGS_DIR, log_file)
                try:
                    os.remove(log_path)
                except OSError:
                    pass
    return kept


def _prune_orphan_logs(entries, retention):
    """Remove ml_train_*.log files not referenced by retained history entries."""
    kept_logs = {e.get("log_file") for e in entries if e.get("log_file")}
    pattern = os.path.join(LOGS_DIR, "ml_train_*.log")
    for path in sorted(glob.glob(pattern)):
        basename = os.path.basename(path)
        if basename not in kept_logs:
            try:
                os.remove(path)
            except OSError:
                pass


def _make_skip_entry(trigger, result, error=None):
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "result": result,
        "duration_seconds": 0,
        "total_samples": 0,
        "cv_accuracy": None,
        "log_file": None,
        "error": error,
    }


def run_training(force=False):
    cfg = _load_config()
    enabled = cfg.get("ml_training_enabled", True)
    interval_days = cfg.get("ml_training_interval", DEFAULT_INTERVAL_DAYS)
    retention = cfg.get("ml_training_retention", DEFAULT_RETENTION_COUNT)
    trigger = "manual" if force else "scheduled"
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_filename = "ml_train_{}.log".format(ts_str)

    # Gate 1: enabled check (only for scheduled runs)
    if not force and not enabled:
        entry = _make_skip_entry(trigger, "skipped_disabled")
        history = _load_training_history()
        history.append(entry)
        history = _prune_history(history, retention)
        _save_training_history(history)
        _dashboard_log("[ML-TRAIN] result=skipped_disabled trigger={}".format(trigger))
        print("Skipped: auto-training disabled")
        return entry

    meta = _load_meta()

    # Gate 2: staleness check (only for scheduled runs)
    if not force and meta.get("trained_at"):
        try:
            trained_dt = datetime.fromisoformat(meta["trained_at"])
            if trained_dt.tzinfo is None:
                trained_dt = trained_dt.replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - trained_dt).total_seconds() / 86400
            if age_days < interval_days - 0.042:  # 0.042 days ≈ 1 hour grace for timer jitter
                entry = _make_skip_entry(trigger, "skipped_awaiting_schedule")
                history = _load_training_history()
                history.append(entry)
                history = _prune_history(history, retention)
                _save_training_history(history)
                _dashboard_log("[ML-TRAIN] result=skipped_awaiting_schedule age={:.1f}d interval={}d".format(age_days, interval_days))
                print("Skipped: model only {:.1f} days old (interval={}d)".format(age_days, interval_days))
                return entry
        except Exception:
            pass

    # Gate 3: hash check (only for scheduled runs)
    if not force:
        current_hash = _history_hash()
        stored_hash = meta.get("history_hash")
        if current_hash and stored_hash and current_hash == stored_hash:
            entry = _make_skip_entry(trigger, "skipped_unchanged")
            history = _load_training_history()
            history.append(entry)
            history = _prune_history(history, retention)
            _save_training_history(history)
            _dashboard_log("[ML-TRAIN] result=skipped_unchanged trigger={}".format(trigger))
            print("Skipped: game_history.json unchanged since last training")
            return entry

    # Acquire lock
    if not _acquire_lock():
        entry = _make_skip_entry(trigger, "skipped_locked", "Another training process is running")
        history = _load_training_history()
        history.append(entry)
        history = _prune_history(history, retention)
        _save_training_history(history)
        _dashboard_log("[ML-TRAIN] result=skipped_locked trigger={}".format(trigger))
        print("Skipped: lock held by another process")
        return entry

    # Run training with log capture
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, log_filename)
    capture = io.StringIO()
    t0 = time.monotonic()
    try:
        sys.path.insert(0, SCRIPT_DIR)
        import ml_model
        with redirect_stdout(capture), redirect_stderr(capture):
            model = ml_model.train_model(evaluate=True)
        elapsed = round(time.monotonic() - t0, 1)

        _captured = capture.getvalue()
        _has_error = "ERROR" in _captured.upper() or "Traceback" in _captured
        _train_ok = model is not None and not _has_error

        # Parse samples and CV from captured output
        _samples = 0
        _cv = None
        for line in _captured.split("\n"):
            if "Training on" in line and "samples" in line:
                try:
                    _samples = int(line.split("Training on")[1].split("samples")[0].strip())
                except (ValueError, IndexError):
                    pass
            if "CV accuracy:" in line:
                try:
                    _cv = float(line.split("CV accuracy:")[1].split("%")[0].strip())
                except (ValueError, IndexError):
                    pass

        # Update history hash in model meta on success
        if _train_ok:
            cur_meta = _load_meta()
            cur_meta["history_hash"] = _history_hash()
            tmp = META_FILE + ".tmp.{}".format(os.getpid())
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur_meta, f, indent=2)
            os.replace(tmp, META_FILE)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "result": "trained" if _train_ok else "error",
            "duration_seconds": elapsed,
            "total_samples": _samples,
            "cv_accuracy": _cv,
            "log_file": log_filename,
            "error": (_captured[:500] if _has_error else None),
        }

        # Write log file
        log_content = _captured
        log_content += "\n--- Training Result ---\n"
        log_content += "ok={} samples={} cv={}\n".format(_train_ok, _samples, _cv)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(log_content)

        _dashboard_log("[ML-TRAIN] result={} trigger={} samples={} cv={} duration={}s".format(
            entry["result"], trigger, _samples, _cv, elapsed
        ))
        print(json.dumps(entry, indent=2))

    except Exception as exc:
        elapsed = round(time.monotonic() - t0, 1)
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger": trigger,
            "result": "error",
            "duration_seconds": elapsed,
            "total_samples": 0,
            "cv_accuracy": None,
            "log_file": log_filename,
            "error": str(exc),
        }
        log_content = capture.getvalue()
        log_content += "\n--- Error ---\n{}\n".format(exc)
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(log_content)
        _dashboard_log("[ML-TRAIN] result=error trigger={} error={} duration={}s".format(trigger, exc, elapsed))
        print(json.dumps(entry, indent=2))
    finally:
        _release_lock()

    # Update history
    history = _load_training_history()
    history.append(entry)
    history = _prune_history(history, retention)
    _prune_orphan_logs(history, retention)
    _save_training_history(history)
    return entry


def main():
    parser = argparse.ArgumentParser(description="NRL ML model training runner (#165)")
    parser.add_argument("--force", action="store_true", help="Skip staleness/hash checks (manual trigger)")
    args = parser.parse_args()
    run_training(force=args.force)


if __name__ == "__main__":
    main()
