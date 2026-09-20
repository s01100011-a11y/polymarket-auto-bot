#!/usr/bin/env python3
"""
recovery.py — CLI for game_history.json backup management, WAL operations (#16),
and auto-recovery gap scanning (#136).

Usage:
    python3 recovery.py list                          List available backups
    python3 recovery.py restore <backup_name>         Restore from a specific backup + replay WAL
    python3 recovery.py restore <backup_name> --no-wal  Restore without WAL replay
    python3 recovery.py wal-status                    Show WAL file status
    python3 recovery.py wal-truncate [--keep N]       Truncate WAL (keep entries for last N backups, default 5)
    python3 recovery.py scan --season 2026            Scan all rounds for missed games
    python3 recovery.py scan --season 2026 --dry-run  Preview only
"""

import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outcomes


def cmd_list(args):
    backups = outcomes.list_backups()
    if not backups:
        print("No backups found.")
        return
    print("{:<40} {:>8} {:>10} {}".format("Name", "Records", "Size", "Date"))
    print("-" * 80)
    for b in backups:
        size_mb = b["size"] / 1024 / 1024
        print("{:<40} {:>8} {:>8.1f}MB  {}".format(
            b["name"], b["records"], size_mb, b["mtime"][:19]))
    print("\n{} backup(s) available.".format(len(backups)))


def cmd_restore(args):
    backup_name = args.backup
    replay = not args.no_wal
    print("Restoring from: {}".format(backup_name))
    print("WAL replay: {}".format("yes" if replay else "no"))

    if not args.yes:
        confirm = input("Proceed? This will replace game_history.json. [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    result = outcomes.restore_from_backup(backup_name, replay_wal_entries=replay)
    if result["success"]:
        print("Restored {} records.".format(result["records"]))
        if result["wal_recovered"]:
            print("WAL replay: {} additional records recovered.".format(
                result["wal_recovered"]))
        print("Done.")
    else:
        print("ERROR: {}".format(result["error"]), file=sys.stderr)
        sys.exit(1)


def cmd_wal_status(args):
    status = outcomes.wal_status()
    if not status["exists"]:
        print("WAL file does not exist.")
        return
    size_kb = status["size"] / 1024
    print("WAL file:  {}".format(outcomes.WAL_FILE))
    print("Entries:   {}".format(status["entries"]))
    print("Size:      {:.1f}KB".format(size_kb))
    print("Oldest:    {}".format(status["oldest"] or "n/a"))
    print("Newest:    {}".format(status["newest"] or "n/a"))


def cmd_wal_truncate(args):
    keep = args.keep
    backups = outcomes.list_backups()
    if keep and len(backups) >= keep:
        cutoff_backup = backups[keep - 1]
        keep_after = cutoff_backup["mtime"]
        print("Truncating WAL entries older than {}".format(keep_after[:19]))
        print("(keeping entries for last {} backups)".format(keep))
    else:
        keep_after = None
        print("Truncating entire WAL")

    if not args.yes:
        confirm = input("Proceed? [y/N] ")
        if confirm.lower() != "y":
            print("Cancelled.")
            return

    removed = outcomes.truncate_wal(keep_after=keep_after)
    print("Removed {} entries.".format(removed))


def cmd_scan(args):
    """Scan rounds for missed games and recover them (#136)."""
    import json
    import time
    from datetime import datetime, timezone
    import nrl_api
    from monitor import _recover_missed_game, load_state, save_state

    seasons = [int(s.strip()) for s in args.season.split(",")]
    dry_run = args.dry_run

    # Load config
    config_path = os.path.join(SCRIPT_DIR, "config.json")
    try:
        with open(config_path) as f:
            config = json.load(f)
    except Exception:
        print("ERROR: Cannot load config.json", file=sys.stderr)
        sys.exit(1)

    # Load history IDs
    history = outcomes.load_history()
    history_ids = {r.get("match_id") for r in history}
    state = load_state()
    now = datetime.now(timezone.utc)

    total_scanned = 0
    total_recovered = 0

    for season in seasons:
        print("\nScanning season {}...".format(season))

        # Scan Premiership rounds (1-27 regular + 28-31 finals #298)
        _prev_urls = None
        for rnd in range(1, nrl_api.MAX_FINALS_ROUND + 1):
            try:
                fixtures = nrl_api.get_draw(season, rnd)
            except Exception:
                if rnd > nrl_api.MAX_REGULAR_ROUND:
                    break
                break  # no more rounds
            if not fixtures:
                if rnd > nrl_api.MAX_REGULAR_ROUND:
                    break
                break

            # NRL API returns same finals fixtures for any round >= 28 (#298)
            _cur_urls = frozenset(f.get("matchCentreUrl", "") for f in fixtures)
            if rnd > nrl_api.MAX_REGULAR_ROUND and _prev_urls == _cur_urls:
                break
            _prev_urls = _cur_urls

            missed = []
            for fix in fixtures:
                fstate = fix.get("matchState", "")
                furl = fix.get("matchCentreUrl", "")
                if (fstate in nrl_api.COMPLETED_STATES
                        and furl
                        and furl not in history_ids
                        and not state.get("game_end_{}".format(furl))):
                    home = fix.get("homeTeam", {}).get("nickName", "?")
                    away = fix.get("awayTeam", {}).get("nickName", "?")
                    missed.append((furl, fix, rnd, home, away))

            total_scanned += 1
            if missed:
                for furl, fix, rnd_num, home, away in missed:
                    if dry_run:
                        print("  [DRY-RUN] R{}: {} vs {}".format(rnd_num, home, away))
                    else:
                        try:
                            ok = _recover_missed_game(furl, fix, rnd_num, season,
                                                      config, state, now)
                            if ok:
                                print("  Recovered R{}: {} vs {}".format(rnd_num, home, away))
                                history_ids.add(furl)
                                total_recovered += 1
                        except Exception as e:
                            print("  FAILED R{}: {} vs {} — {}".format(
                                rnd_num, home, away, e), file=sys.stderr)
                    time.sleep(0.4)  # rate limit

            time.sleep(0.2)

        # Scan SOO fixtures
        try:
            soo_fixes = nrl_api.get_soo_fixtures(season)
            for fix in (soo_fixes or []):
                fstate = fix.get("matchState", "")
                furl = fix.get("matchCentreUrl", "")
                if (fstate in nrl_api.COMPLETED_STATES
                        and furl
                        and furl not in history_ids
                        and not state.get("game_end_{}".format(furl))):
                    home = fix.get("homeTeam", {}).get("nickName", "?")
                    away = fix.get("awayTeam", {}).get("nickName", "?")
                    if dry_run:
                        print("  [DRY-RUN] SOO: {} vs {}".format(home, away))
                    else:
                        try:
                            ok = _recover_missed_game(furl, fix, 0, season,
                                                      config, state, now)
                            if ok:
                                print("  Recovered SOO: {} vs {}".format(home, away))
                                history_ids.add(furl)
                                total_recovered += 1
                        except Exception as e:
                            print("  FAILED SOO: {} vs {} — {}".format(
                                home, away, e), file=sys.stderr)
                    time.sleep(0.4)
        except Exception:
            pass

    if not dry_run and total_recovered > 0:
        save_state(state)

    action = "would recover" if dry_run else "recovered"
    print("\nScanned {} rounds, {} {} games.".format(
        total_scanned, action, total_recovered))


def main():
    parser = argparse.ArgumentParser(
        description="game_history.json backup management, WAL operations, and auto-recovery"
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("list", help="List available backups")

    restore_p = sub.add_parser("restore", help="Restore from a specific backup")
    restore_p.add_argument("backup",
                           help="Backup name (e.g. 20260505T160000Z-scheduled)")
    restore_p.add_argument("--no-wal", action="store_true",
                           help="Skip WAL replay")
    restore_p.add_argument("-y", "--yes", action="store_true",
                           help="Skip confirmation prompt")

    sub.add_parser("wal-status", help="Show WAL file status")

    trunc_p = sub.add_parser("wal-truncate", help="Truncate WAL entries")
    trunc_p.add_argument("--keep", type=int, default=5,
                         help="Keep entries for last N backups (default: 5)")
    trunc_p.add_argument("-y", "--yes", action="store_true",
                         help="Skip confirmation prompt")

    scan_p = sub.add_parser("scan", help="Scan for missed games and recover (#136)")
    scan_p.add_argument("--season", required=True,
                        help="Season(s) to scan (e.g. 2026 or 2025,2026)")
    scan_p.add_argument("--dry-run", action="store_true",
                        help="Preview missed games without recovering")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    {"list": cmd_list, "restore": cmd_restore, "wal-status": cmd_wal_status,
     "wal-truncate": cmd_wal_truncate, "scan": cmd_scan}[args.command](args)


if __name__ == "__main__":
    main()
