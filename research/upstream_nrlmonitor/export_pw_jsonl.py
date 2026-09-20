#!/usr/bin/env python3
"""Bootstrap pw_export.jsonl from game_history.json.

Reads game_history.json and writes one JSONL line per non-suppressed
PW call with game context merged.  Safe to re-run — overwrites the output file.

Usage:
    python3 export_pw_jsonl.py                     # all sources
    python3 export_pw_jsonl.py --source live        # live games only
    python3 export_pw_jsonl.py --include-suppressed # include _suppressed calls
"""
import argparse
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(SCRIPT_DIR)


def main():
    parser = argparse.ArgumentParser(description="Bootstrap PW export JSONL from game_history")
    parser.add_argument("--source", default="all", choices=["live", "backfill", "all"],
                        help="Filter by game source (default: all)")
    parser.add_argument("--include-suppressed", action="store_true",
                        help="Include _suppressed PW calls")
    parser.add_argument("--since", default="", help="Only games on or after this ISO date")
    parser.add_argument("--until", default="", help="Only games on or before this ISO date")
    args = parser.parse_args()

    history_path = os.path.join(SCRIPT_DIR, "game_history.json")
    if not os.path.exists(history_path):
        print(f"No history file at {history_path}")
        sys.exit(1)

    with open(history_path) as f:
        history = json.load(f)

    out_path = os.path.join(SCRIPT_DIR, "pw_export.jsonl")
    total = 0
    skipped_suppressed = 0

    with open(out_path, "w") as out:
        for rec in history:
            pw_calls = rec.get("predicted_winner_calls")
            if not isinstance(pw_calls, list) or not pw_calls:
                continue
            rec_source = str(rec.get("source") or "")
            if args.source != "all" and rec_source != args.source:
                continue
            game_date = str(rec.get("date") or "")
            if args.since and game_date < args.since:
                continue
            if args.until and game_date > args.until:
                continue

            game_ctx = {
                "game_id": str(rec.get("match_id", "")),
                "game_date": game_date,
                "home_team": str(rec.get("home_team", "")),
                "away_team": str(rec.get("away_team", "")),
                "home_id": str(rec.get("home_team_id", "")),
                "away_id": str(rec.get("away_team_id", "")),
                "home_score": rec.get("home_score"),
                "away_score": rec.get("away_score"),
                "winner": str(rec.get("winner") or ""),
                "round": str(rec.get("round", "")),
                "season": str(rec.get("season", "")),
                "season_segment": str(rec.get("season_segment", "")),
                "game_source": rec_source,
                "game_completed": True,
            }

            for c in pw_calls:
                if not args.include_suppressed and c.get("_suppressed"):
                    skipped_suppressed += 1
                    continue
                row = dict(c)
                row.update(game_ctx)
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
                total += 1

    print(f"Wrote {total} PW calls to {out_path}")
    if skipped_suppressed:
        print(f"Skipped {skipped_suppressed} suppressed calls (use --include-suppressed to include)")


if __name__ == "__main__":
    main()
