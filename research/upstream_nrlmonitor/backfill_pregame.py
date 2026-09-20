#!/usr/bin/env python3
"""
backfill_pregame.py — Backfill historical pregame info from NRL.com ladder API.

Fetches per-round ladder snapshots and attaches pregame records/positions/streaks
to every game in game_history.json.

Usage:
    python3 backfill_pregame.py --fetch-cache --season 2024,2025,2026
    python3 backfill_pregame.py --use-cache --season 2024,2025,2026
    python3 backfill_pregame.py --use-cache --dry-run --season 2024
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import nrl_api

HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
CACHE_DIR = os.path.join(SCRIPT_DIR, "nrl_cache")
MAX_ROUND = 31


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} {}".format(ts, msg), flush=True)


def _cache_path(season, rnd):
    d = os.path.join(CACHE_DIR, str(season))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "ladder_round_{}.json".format(rnd))


def _cache_exists(season, rnd):
    return os.path.isfile(_cache_path(season, rnd))


def _write_cache(season, rnd, data):
    with open(_cache_path(season, rnd), "w") as f:
        json.dump(data, f, indent=2)


def _read_cache(season, rnd):
    path = _cache_path(season, rnd)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _parse_ladder(raw_data):
    """Parse NRL ladder API response into a dict keyed by team nickname."""
    positions = raw_data.get("positions", [])
    result = {}
    for i, p in enumerate(positions):
        nickname = p.get("teamNickname", "")
        if not nickname:
            theme_key = p.get("theme", {}).get("key", "")
            nickname = theme_key.capitalize() if theme_key else ""
        if not nickname:
            continue

        stats = p.get("stats", {})
        result[nickname] = {
            "position": i + 1,
            "position_label": nrl_api._ordinal(i + 1),
            "played": stats.get("played", 0),
            "wins": stats.get("wins", 0),
            "lost": stats.get("lost", 0),
            "drawn": stats.get("drawn", 0),
            "points": stats.get("points", 0),
            "record": "{}-{}".format(stats.get("wins", 0), stats.get("lost", 0)),
            "home_record": stats.get("home record", ""),
            "away_record": stats.get("away record", ""),
            "streak": stats.get("streak", ""),
            "form": stats.get("form", ""),
            "pts_for": stats.get("points for", 0),
            "pts_against": stats.get("points against", 0),
            "pts_diff": stats.get("points difference", 0),
            "avg_win_margin": stats.get("average winning margin", 0),
            "avg_loss_margin": stats.get("average losing margin", 0),
        }
    return result


def fetch_cache_ladders(seasons, delay=0.3):
    """Phase 1: Fetch and cache ladder data for every round."""
    total = 0
    cached = 0
    for season in seasons:
        _log("Fetching ladders for {}...".format(season))
        for rnd in range(1, MAX_ROUND + 1):
            if _cache_exists(season, rnd):
                cached += 1
                total += 1
                continue

            url = "{}/ladder/data?competition={}&season={}&round={}".format(
                nrl_api.BASE, nrl_api.COMPETITION, season, rnd)
            try:
                data = nrl_api._get(url, referer="{}/ladder/".format(nrl_api.BASE))
                positions = data.get("positions", [])
                if not positions:
                    _log("  {} R{}: no ladder data (skipping remaining)".format(season, rnd))
                    break
                _write_cache(season, rnd, data)
                total += 1
                time.sleep(delay)
            except Exception as e:
                _log("  {} R{}: ERROR {}".format(season, rnd, e))
                break

        _log("  {} done".format(season))

    _log("Ladder fetch complete: {} total, {} from cache".format(total, cached))
    return total


def backfill_pregame(seasons, dry_run=False, use_cache=False):
    """Phase 2: Attach pregame ladder data to game history records."""
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    _log("Loaded {} game records".format(len(history)))

    updated = 0
    skipped = 0

    for i, rec in enumerate(history):
        season = rec.get("season")
        if season not in seasons:
            continue

        rnd = rec.get("round", 0)
        home = rec.get("home_team", "")
        away = rec.get("away_team", "")

        # Use the ladder from the round BEFORE this game
        # For R1, use R1 itself (pre-season state)
        ladder_rnd = max(1, rnd - 1) if rnd > 1 else 1

        ladder_data = _read_cache(season, ladder_rnd)
        if not ladder_data:
            skipped += 1
            continue

        ladder = _parse_ladder(ladder_data)
        home_ladder = ladder.get(home, {})
        away_ladder = ladder.get(away, {})

        if not home_ladder and not away_ladder:
            skipped += 1
            continue

        pregame = {
            "home_position": home_ladder.get("position_label", ""),
            "home_record": home_ladder.get("record", ""),
            "home_home_record": home_ladder.get("home_record", ""),
            "home_streak": home_ladder.get("streak", ""),
            "home_form": home_ladder.get("form", ""),
            "home_pts_for": home_ladder.get("pts_for", 0),
            "home_pts_against": home_ladder.get("pts_against", 0),
            "home_pts_diff": home_ladder.get("pts_diff", 0),
            "away_position": away_ladder.get("position_label", ""),
            "away_record": away_ladder.get("record", ""),
            "away_away_record": away_ladder.get("away_record", ""),
            "away_streak": away_ladder.get("streak", ""),
            "away_form": away_ladder.get("form", ""),
            "away_pts_for": away_ladder.get("pts_for", 0),
            "away_pts_against": away_ladder.get("pts_against", 0),
            "away_pts_diff": away_ladder.get("pts_diff", 0),
        }

        if dry_run:
            print("  {} R{:2d}: {:15s} ({:5s} {:4s}) vs {:15s} ({:5s} {:4s})".format(
                season, rnd,
                home, pregame["home_record"], pregame["home_position"],
                away, pregame["away_record"], pregame["away_position"]))
        else:
            history[i]["pregame"] = pregame

        updated += 1

    if not dry_run and updated > 0:
        # Pre-backfill snapshot (#16 Phase 3)
        try:
            import runtime_backup
            snap_dir, _ = runtime_backup.snapshot_runtime_files(label="pre-backfill")
            _log("Pre-backfill snapshot: {}".format(os.path.basename(snap_dir)))
        except Exception as e:
            _log("WARN: Pre-backfill snapshot failed: {}".format(e))
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
        _log("Wrote pregame data to {} records".format(updated))

    _log("Done: {} updated, {} skipped{}".format(
        updated, skipped, " (dry-run)" if dry_run else ""))
    return updated


def main():
    parser = argparse.ArgumentParser(description="Backfill NRL pregame ladder data")
    parser.add_argument("--fetch-cache", action="store_true",
                        help="Fetch and cache ladder data per round")
    parser.add_argument("--use-cache", action="store_true",
                        help="Use cached ladder data to populate game history")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--season", type=str, default="2024,2025,2026")
    parser.add_argument("--delay", type=float, default=0.3)
    args = parser.parse_args()

    seasons = [int(s.strip()) for s in args.season.split(",")]

    if not args.fetch_cache and not args.use_cache:
        parser.error("Specify --fetch-cache or --use-cache")

    if args.fetch_cache:
        fetch_cache_ladders(seasons, delay=args.delay)

    if args.use_cache:
        backfill_pregame(seasons, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
