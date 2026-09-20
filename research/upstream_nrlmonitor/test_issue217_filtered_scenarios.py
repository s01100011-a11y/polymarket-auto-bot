#!/usr/bin/env python3
"""Tests for NRL #217: Filtered scenario stats (source + strict_bk)."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import outcomes

PASS = 0
FAIL = 0


def check(label, condition):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}")


def make_record(match_id, predicted_team, correct, synthetic=False,
                bk_moneyline=None, bk_spread=None, quarter="Q1"):
    return {
        "match_id": str(match_id),
        "home_team": "Panthers",
        "away_team": "Sharks",
        "home_score": 20,
        "away_score": 10,
        "home_spread": -6.5,
        "predicted_winner_calls": [{
            "predicted_team": predicted_team,
            "correct": correct,
            "pct": 70.0,
            "consensus": "strong",
            "quarter": quarter,
            "spread_role": "dog",
            "avg_edge": 2.0,
            "score_margin_at_fire": 1,
            "synthetic": synthetic,
            "bk_moneyline": bk_moneyline,
            "bk_spread": bk_spread,
            "ts": "2026-06-01T00:00:00Z",
        }],
    }


# 4 records:
# Game 1: live (synthetic=False), has BK odds
# Game 2: live (synthetic=False), NO BK odds
# Game 3: backfill (synthetic=True), has BK odds
# Game 4: backfill (synthetic=True), NO BK odds
records = [
    make_record("g1", "Sharks", True, synthetic=False, bk_moneyline=2.30, bk_spread=6.5),
    make_record("g2", "Sharks", False, synthetic=False),
    make_record("g3", "Sharks", True, synthetic=True, bk_moneyline=1.90, bk_spread=4.5),
    make_record("g4", "Sharks", False, synthetic=True),
]

print("=== compute_scenario_stats with source filter (#217) ===")

# Unfiltered: all 4 calls
all_stats = outcomes.compute_scenario_stats(records)
key = list(all_stats.keys())[0]
check("unfiltered: total_calls=4", all_stats[key]["total_calls"] == 4)

# Source=live: only non-synthetic (g1, g2)
live_stats = outcomes.compute_scenario_stats(records, source="live")
check("source=live: total_calls=2", live_stats[key]["total_calls"] == 2)
check("source=live: correct=1 (g1)", live_stats[key]["correct"] == 1)

# Source=backfill: only synthetic (g3, g4)
bf_stats = outcomes.compute_scenario_stats(records, source="backfill")
check("source=backfill: total_calls=2", bf_stats[key]["total_calls"] == 2)
check("source=backfill: correct=1 (g3)", bf_stats[key]["correct"] == 1)

print("\n=== compute_scenario_stats with strict_bk filter (#217) ===")

# strict_bk=True: only calls with bk_moneyline or bk_spread (g1, g3)
strict_stats = outcomes.compute_scenario_stats(records, strict_bk=True)
check("strict_bk: total_calls=2", strict_stats[key]["total_calls"] == 2)
check("strict_bk: correct=2 (g1, g3 both correct)", strict_stats[key]["correct"] == 2)
check("strict_bk: total_games=2", strict_stats[key]["total_games"] == 2)

print("\n=== combined filters (#217) ===")

# source=live + strict_bk=True: only g1 (live AND has BK)
combo_stats = outcomes.compute_scenario_stats(records, source="live", strict_bk=True)
check("live+strict_bk: total_calls=1", combo_stats[key]["total_calls"] == 1)
check("live+strict_bk: correct=1 (g1)", combo_stats[key]["correct"] == 1)

# source=backfill + strict_bk=True: only g3 (backfill AND has BK)
combo2_stats = outcomes.compute_scenario_stats(records, source="backfill", strict_bk=True)
check("backfill+strict_bk: total_calls=1", combo2_stats[key]["total_calls"] == 1)

# source=live + strict_bk=False: g1, g2 (live, no BK filter)
combo3_stats = outcomes.compute_scenario_stats(records, source="live", strict_bk=False)
check("live+no_strict: total_calls=2", combo3_stats[key]["total_calls"] == 2)


print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
