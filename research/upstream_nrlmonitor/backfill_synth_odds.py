#!/usr/bin/env python3
"""Backfill synthetic odds onto historical PW call records (#235).

Computes synth_moneyline, synth_spread, synth_h1_ml for all PW calls
that have score_margin_at_fire + quarter but missing synthetic fields.

Usage:
    python3 backfill_synth_odds.py              # dry run (report only)
    python3 backfill_synth_odds.py --write       # write updates to game_history
"""

import json
import os
import sys

import synthetic_odds

HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "game_history.json")


def backfill(write=False):
    """Backfill synthetic odds for all PW calls.

    Returns (updated_count, skipped_count, total_calls).
    """
    if not os.path.exists(HISTORY_FILE):
        print("  game_history.json not found")
        return 0, 0, 0

    with open(HISTORY_FILE) as f:
        games = json.load(f)

    updated = 0
    skipped = 0
    total = 0
    modified = False

    for g in games:
        for c in g.get("predicted_winner_calls", []):
            total += 1

            # Skip if already populated
            if c.get("synth_moneyline") is not None:
                continue

            margin = c.get("score_margin_at_fire")
            quarter = c.get("quarter")
            if margin is None or not quarter:
                skipped += 1
                continue

            # Pregame ML from PW call's moneyline field (decimal odds string)
            pregame_ml = None
            ml_val = c.get("moneyline")
            if ml_val:
                try:
                    pregame_ml = float(ml_val)
                except (TypeError, ValueError):
                    pass

            synth = synthetic_odds.compute_all(margin, quarter, pregame_ml)
            c["synth_moneyline"] = synth.get("moneyline")
            c["synth_spread"] = synth.get("spread")
            c["synth_h1_ml"] = synth.get("h1_ml")
            updated += 1
            modified = True

    if write and modified:
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(games, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
        print("  Wrote {}".format(HISTORY_FILE))

    return updated, skipped, total


def main():
    write = "--write" in sys.argv
    mode = "WRITE" if write else "DRY RUN"
    print("Backfill synthetic odds ({})".format(mode))
    print()

    updated, skipped, total = backfill(write=write)
    already = total - updated - skipped
    print("  {} updated, {} already populated, {} skipped (missing inputs), "
          "{} total".format(updated, already, skipped, total))

    if not write and updated > 0:
        print("\nRun with --write to persist changes.")


if __name__ == "__main__":
    main()
