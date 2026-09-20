#!/usr/bin/env python3
"""Create and restore dated runtime snapshots for NRL Monitor (#16)."""

import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BACKUP_ROOT = os.path.join(SCRIPT_DIR, "runtime_backups")
RUNTIME_FILES = (
    "game_history.json",
    "game_history.wal",
    "alerts.json",
    "live_stats.json",
    "game_ticker.json",
    ".alert_state.json",
    "config.json",
    "odds_api_log.jsonl",
    "model_nrl.pkl",
    "model_meta_nrl.json",
    "backfill_state.json",
    "pw_trend_nrl.json",
    "ml_training_history.json",
    "model_eval_history_nrl.json",
    "odds_monitor_history.json",
)


def _now_utc():
    return datetime.now(timezone.utc)


def _timestamp_slug(now=None):
    return (now or _now_utc()).strftime("%Y%m%dT%H%M%SZ")


def _sanitize_label(label):
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or "").strip())
    return text.strip("-")


def _load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _history_row_count(path):
    data = _load_json(path, default=[])
    return len(data) if isinstance(data, list) else 0


def cleanup_orphaned_tmp_files():
    """Remove *.tmp.<pid> files whose PID is no longer running.

    These are left behind when atomic writes crash between writing the temp
    file and the os.replace() call.  Returns count removed.
    """
    removed = 0
    for name in os.listdir(SCRIPT_DIR):
        if ".tmp." not in name:
            continue
        parts = name.rsplit(".tmp.", 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        try:
            os.kill(pid, 0)
            continue  # still running
        except ProcessLookupError:
            pass  # dead — safe to remove
        except PermissionError:
            continue  # running but owned by another user
        path = os.path.join(SCRIPT_DIR, name)
        try:
            os.remove(path)
            removed += 1
        except OSError:
            pass
    return removed


def _resolve_backup_dir(backup_root, label=None):
    timestamp = _timestamp_slug()
    slug = _sanitize_label(label)
    dirname = "{}-{}".format(timestamp, slug) if slug else timestamp
    return os.path.join(os.path.abspath(backup_root), dirname)


def _config_fingerprint(config_path=None):
    """Compute a short hash of config.json for change detection."""
    import hashlib
    cfg_path = config_path or os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(cfg_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:12]
    except Exception:
        return None


def collect_runtime_metadata(config_path=None, files=None):
    file_names = tuple(files or RUNTIME_FILES)
    metadata = {
        "created_at": _now_utc().isoformat(),
        "config_fingerprint": _config_fingerprint(config_path),
        "counts": {},
        "files": {},
    }

    for name in file_names:
        path = os.path.join(SCRIPT_DIR, name)
        exists = os.path.exists(path)
        file_meta = {"path": path, "exists": exists}
        if exists:
            stat = os.stat(path)
            file_meta["size"] = stat.st_size
            file_meta["mtime"] = datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat()
        metadata["files"][name] = file_meta

    history_path = os.path.join(SCRIPT_DIR, "game_history.json")
    if os.path.exists(history_path):
        metadata["counts"]["history_rows"] = _history_row_count(history_path)
        # PW metadata (#15 Phase 6)
        try:
            with open(history_path) as f:
                records = json.load(f)
            pw_count = sum(len(r.get("predicted_winner_calls", [])) for r in records)
            metadata["counts"]["pw_call_count"] = pw_count
        except Exception:
            pass

    # PW config info
    cfg_path = config_path or os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(cfg_path) as f:
            cfg = json.load(f)
        metadata["pw_enabled"] = cfg.get("predicted_winner_enabled", False)
        metadata["pw_version"] = cfg.get("pw_version", "v1.0")
    except Exception:
        pass

    return metadata


def snapshot_runtime_files(
    backup_root=DEFAULT_BACKUP_ROOT, label=None, extra_metadata=None, files=None
):
    """Create a timestamped snapshot of all runtime files.

    Returns (snapshot_dir, metadata) tuple.
    """
    file_names = tuple(files or RUNTIME_FILES)
    snapshot_dir = _resolve_backup_dir(backup_root, label=label)
    os.makedirs(snapshot_dir, exist_ok=False)

    metadata = collect_runtime_metadata(files=file_names)
    if isinstance(extra_metadata, dict):
        metadata.update(extra_metadata)

    copied = []
    missing = []
    for name in file_names:
        src = os.path.join(SCRIPT_DIR, name)
        if not os.path.exists(src):
            missing.append(name)
            continue
        dst = os.path.join(snapshot_dir, name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(name)

    metadata["copied_files"] = copied
    metadata["missing_files"] = missing
    metadata["snapshot_dir"] = snapshot_dir

    with open(os.path.join(snapshot_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    # Clean up orphaned .tmp.<pid> files from crashed writes
    try:
        tmp_removed = cleanup_orphaned_tmp_files()
        if tmp_removed:
            metadata["orphaned_tmp_removed"] = tmp_removed
    except Exception:
        pass  # non-fatal

    # Truncate WAL after successful backup — keep entries for last 5 backups
    try:
        import outcomes
        backups = outcomes.list_backups()
        if len(backups) >= 5:
            keep_after = backups[4]["mtime"]  # 5th newest backup
            removed = outcomes.truncate_wal(keep_after=keep_after)
            if removed:
                metadata["wal_truncated"] = removed
    except Exception:
        pass  # WAL truncation failure is non-fatal

    # Append PW trend snapshot (#26)
    try:
        import pw_trend
        snap = pw_trend.append_snapshot()
        metadata["pw_trend_snapshot"] = {
            "date": snap.get("date"),
            "pw_calls": snap.get("pw_calls"),
            "accuracy": snap.get("accuracy"),
        }
    except Exception:
        pass  # PW trend snapshot failure is non-fatal

    # Auto-generate ROI report (#149)
    try:
        import pw_roi_report
        pw_roi_report.generate_report(copy_to_nba=True)
        metadata["roi_report_generated"] = True
    except Exception:
        pass  # ROI report failure is non-fatal

    return snapshot_dir, metadata


def restore_runtime_snapshot(snapshot_dir, archive_root=None, files=None):
    """Restore all runtime files from a snapshot, archiving current files first."""
    snapshot_dir = os.path.abspath(snapshot_dir)
    if not os.path.isdir(snapshot_dir):
        raise FileNotFoundError(
            "Snapshot directory not found: {}".format(snapshot_dir)
        )

    file_names = tuple(files or RUNTIME_FILES)
    archive_base = os.path.abspath(archive_root or DEFAULT_BACKUP_ROOT)
    archive_dir = os.path.join(
        archive_base, "failed-run-{}".format(_timestamp_slug())
    )
    os.makedirs(archive_dir, exist_ok=False)

    restored = []
    archived = []
    missing_in_snapshot = []

    for name in file_names:
        src = os.path.join(snapshot_dir, name)
        dst = os.path.join(SCRIPT_DIR, name)
        if not os.path.exists(src):
            missing_in_snapshot.append(name)
            continue
        if os.path.exists(dst):
            archive_path = os.path.join(archive_dir, name)
            os.makedirs(os.path.dirname(archive_path), exist_ok=True)
            shutil.copy2(dst, archive_path)
            archived.append(name)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        restored.append(name)

    restore_meta = {
        "restored_at": _now_utc().isoformat(),
        "snapshot_dir": snapshot_dir,
        "archive_dir": archive_dir,
        "restored_files": restored,
        "archived_files": archived,
        "missing_in_snapshot": missing_in_snapshot,
    }
    with open(os.path.join(archive_dir, "restore.json"), "w") as f:
        json.dump(restore_meta, f, indent=2)

    return restore_meta


def prune_old_snapshots(
    backup_root=DEFAULT_BACKUP_ROOT, keep_last=7, label_filter=None
):
    """Remove oldest snapshot directories, keeping the most recent keep_last.

    Args:
        backup_root: Root directory containing snapshot subdirs.
        keep_last: Number of most-recent snapshots to retain.
        label_filter: If set, only prune snapshots whose directory name contains
                      this string (e.g. 'scheduled'). None = prune all snapshots
                      (excluding failed-run-* archive dirs).
    Returns:
        dict with 'kept', 'pruned', and 'errors' lists.
    """
    backup_root = os.path.abspath(backup_root)
    if not os.path.isdir(backup_root):
        return {"kept": [], "pruned": [], "errors": []}

    candidates = []
    for name in os.listdir(backup_root):
        if name.startswith("failed-run-"):
            continue
        full = os.path.join(backup_root, name)
        if not os.path.isdir(full):
            continue
        if label_filter and label_filter not in name:
            continue
        meta_path = os.path.join(full, "metadata.json")
        sort_key = os.path.getmtime(full)
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    m = json.load(f)
                ts = m.get("created_at") or ""
                if ts:
                    sort_key = ts  # ISO strings sort correctly
            except Exception:
                pass
        candidates.append((sort_key, full, name))

    candidates.sort(key=lambda x: x[0])  # oldest first
    to_prune = candidates[:-keep_last] if len(candidates) > keep_last else []
    to_keep = (
        candidates[-keep_last:] if len(candidates) >= keep_last else candidates
    )

    pruned = []
    errors = []
    for _, full, name in to_prune:
        try:
            shutil.rmtree(full)
            pruned.append(name)
        except Exception as exc:
            errors.append({"dir": name, "error": str(exc)})

    return {
        "kept": [name for _, _, name in to_keep],
        "pruned": pruned,
        "errors": errors,
    }


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Create or restore NRL Monitor runtime snapshots"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser(
        "snapshot", help="Create a runtime snapshot"
    )
    snapshot_parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    snapshot_parser.add_argument("--label", default="prewrite")
    snapshot_parser.add_argument(
        "--keep-last",
        type=int,
        default=None,
        metavar="N",
        help="After creating snapshot, prune old snapshots keeping N most recent (0 = no pruning)",
    )
    snapshot_parser.add_argument(
        "--prune-label",
        default=None,
        metavar="LABEL",
        help="When pruning, only consider snapshots whose dir name contains this label",
    )

    restore_parser = subparsers.add_parser(
        "restore", help="Restore a runtime snapshot"
    )
    restore_parser.add_argument("snapshot_dir")
    restore_parser.add_argument("--archive-root", default=DEFAULT_BACKUP_ROOT)

    prune_parser = subparsers.add_parser(
        "prune", help="Prune old snapshots, keeping the N most recent"
    )
    prune_parser.add_argument("--backup-root", default=DEFAULT_BACKUP_ROOT)
    prune_parser.add_argument(
        "--keep-last", type=int, default=7, metavar="N"
    )
    prune_parser.add_argument(
        "--label-filter", default=None, metavar="LABEL"
    )

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "snapshot":
        snapshot_dir, metadata = snapshot_runtime_files(
            backup_root=args.backup_root,
            label=args.label,
        )
        result = {
            "snapshot_dir": snapshot_dir,
            "copied_files": metadata.get("copied_files", []),
            "missing_files": metadata.get("missing_files", []),
            "config_fingerprint": metadata.get("config_fingerprint"),
            "counts": metadata.get("counts", {}),
        }
        keep_last = args.keep_last
        # Fallback to config.json backup_retention if --keep-last not specified
        if keep_last is None:
            try:
                config_path = os.path.join(SCRIPT_DIR, "config.json")
                with open(config_path) as _cf:
                    _cfg = json.load(_cf)
                br = _cfg.get("backup_retention", {})
                label = args.label or ""
                if "scheduled" in label:
                    keep_last = br.get("keep_scheduled", 10)
                elif "pre-config" in label:
                    keep_last = br.get("keep_preconfig", 10)
            except Exception:
                pass
        if keep_last is not None and keep_last > 0:
            prune_result = prune_old_snapshots(
                backup_root=args.backup_root,
                keep_last=keep_last,
                label_filter=args.prune_label,
            )
            result["prune"] = prune_result
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "restore":
        restore_meta = restore_runtime_snapshot(
            snapshot_dir=args.snapshot_dir,
            archive_root=args.archive_root,
        )
        print(json.dumps(restore_meta, indent=2))
        return 0

    if args.command == "prune":
        prune_result = prune_old_snapshots(
            backup_root=args.backup_root,
            keep_last=args.keep_last,
            label_filter=args.label_filter,
        )
        print(json.dumps(prune_result, indent=2))
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
