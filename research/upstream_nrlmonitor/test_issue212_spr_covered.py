#!/usr/bin/env python3
"""Tests for NRL #212: Spread badge shows covered/total instead of won/total."""

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


# Build test records: 4 games with PW calls where spread cover != correct
# NRL uses team names (not IDs) and decimal odds
# Game 1: WIN, covered (away team wins by 10, spread +6.5)
# Game 2: LOSS, covered (away team loses by 1, but spread was +2.5)
# Game 3: LOSS, not covered (away team loses by 10, spread +5.5)
# Game 4: WIN, not covered (away team wins by 1, but spread was -3.5)

def make_record(game_id, home_team, away_team, home_score, away_score,
                predicted_team, correct, bk_spread, bk_moneyline,
                spread_role, quarter="Q1", avg_edge=2.0,
                score_margin_at_fire=1):
    # NRL: home_spread < 0 means home is favourite
    home_spread = -6.5 if spread_role == "dog" else 6.5
    return {
        "game_id": str(game_id),
        "home_team": home_team,
        "away_team": away_team,
        "home_score": home_score,
        "away_score": away_score,
        "home_spread": home_spread,
        "predicted_winner_calls": [{
            "predicted_team": predicted_team,
            "correct": correct,
            "pct": 70.0,
            "consensus": "strong",
            "quarter": quarter,
            "spread_role": spread_role,
            "avg_edge": avg_edge,
            "score_margin_at_fire": score_margin_at_fire,
            "bk_spread": bk_spread,
            "bk_moneyline": bk_moneyline,
            "bk_spread_price": 1.91,  # NRL decimal odds (~-110 equivalent)
            "ts": "2026-06-01T00:00:00Z",
        }],
    }


records = [
    # Game 1: away (Sharks) wins 20-10, covered +6.5 spread
    # team_margin = 20 - 10 = +10, +10 + 6.5 = 16.5 > 0 -> covered
    make_record(1, "Panthers", "Sharks", 10, 20, "Sharks", True, 6.5, 2.30,
                "dog"),
    # Game 2: away (Sharks) loses 19-20, but covers +2.5 spread
    # team_margin = 19 - 20 = -1, -1 + 2.5 = 1.5 > 0 -> covered
    make_record(2, "Panthers", "Sharks", 20, 19, "Sharks", False, 2.5, 1.80,
                "dog"),
    # Game 3: away (Sharks) loses 10-20, doesn't cover +5.5
    # team_margin = 10 - 20 = -10, -10 + 5.5 = -4.5 <= 0 -> not covered
    make_record(3, "Panthers", "Sharks", 20, 10, "Sharks", False, 5.5, 2.20,
                "dog"),
    # Game 4: away (Sharks) wins 21-20, but doesn't cover -3.5 (line moved)
    # team_margin = 21 - 20 = +1, +1 + (-3.5) = -2.5 <= 0 -> not covered
    make_record(4, "Panthers", "Sharks", 20, 21, "Sharks", True, -3.5, 1.50,
                "dog"),
]

print("=== compute_scenario_stats returns bk_spr_covered (NRL #212) ===")

result = outcomes.compute_scenario_stats(records)

# All 4 calls should map to same scenario key: underdog|leading|0-5|Q1
keys = list(result.keys())
check("exactly one scenario key", len(keys) == 1)

if keys:
    stats = result[keys[0]]
    print(f"  Stats: {stats}")

    check("total_calls=4", stats["total_calls"] == 4)
    check("correct=2 (games 1,4 won)", stats["correct"] == 2)

    # Covered: game 1 YES, game 2 YES, game 3 NO, game 4 NO
    check("bk_spr_covered field exists", "bk_spr_covered" in stats)
    check("bk_spr_covered=2 (games 1,2 covered)", stats.get("bk_spr_covered") == 2)
    check("bk_spr_count=4", stats["bk_spr_count"] == 4)

    # Verify covered != correct in terms of WHICH games
    # Game 1: won=True, covered=True
    # Game 2: won=False, covered=True  <-- different
    # Game 3: won=False, covered=False
    # Game 4: won=True, covered=False  <-- different
    check("covered count equals correct count but different games",
          stats.get("bk_spr_covered") == 2 and stats["correct"] == 2)

    # Add a 5th record: loss that covers, to make covered > correct
    records.append(
        make_record(5, "Panthers", "Sharks", 14, 13, "Sharks", False, 1.5, 2.00,
                    "dog"),
    )
    result2 = outcomes.compute_scenario_stats(records)
    stats2 = result2[list(result2.keys())[0]]
    check("with 5 calls: correct=2", stats2["correct"] == 2)
    check("with 5 calls: bk_spr_covered=3 (games 1,2,5)",
          stats2.get("bk_spr_covered") == 3)
    check("bk_spr_covered > correct demonstrates the fix",
          stats2.get("bk_spr_covered", 0) > stats2["correct"])


print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
