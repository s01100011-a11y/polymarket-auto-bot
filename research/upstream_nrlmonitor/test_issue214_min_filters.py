#!/usr/bin/env python3
"""Tests for NRL #214: Min calls/games filters + total_games in scenario stats."""

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


def make_record(match_id, home_team, away_team, home_score, away_score,
                predicted_team, correct, quarter="Q1", avg_edge=2.0,
                score_margin_at_fire=1, spread_role="dog"):
    home_spread = -6.5 if spread_role == "dog" else 6.5
    return {
        "match_id": str(match_id),
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
            "ts": "2026-06-01T00:00:00Z",
        }],
    }


# Build records: 3 distinct games, game 2 has 2 calls (same scenario)
# All map to same scenario key: dog|leading|0-5|Q1
records = [
    make_record("game_A", "Panthers", "Sharks", 10, 20, "Sharks", True),
    make_record("game_B", "Panthers", "Sharks", 10, 20, "Sharks", False),
    make_record("game_B", "Panthers", "Sharks", 10, 20, "Sharks", True),  # 2nd call same game
    make_record("game_C", "Panthers", "Sharks", 10, 20, "Sharks", False),
]
# But records[1] and records[2] have the same match_id "game_B",
# so total_calls=4 but total_games=3

# Actually we need records[1] and [2] to be the same game record with 2 calls
records_multi = [
    make_record("game_A", "Panthers", "Sharks", 10, 20, "Sharks", True),
    {
        "match_id": "game_B",
        "home_team": "Panthers",
        "away_team": "Sharks",
        "home_score": 10,
        "away_score": 20,
        "home_spread": -6.5,
        "predicted_winner_calls": [
            {
                "predicted_team": "Sharks", "correct": False,
                "pct": 70.0, "consensus": "strong", "quarter": "Q1",
                "spread_role": "dog", "avg_edge": 2.0,
                "score_margin_at_fire": 1, "ts": "2026-06-01T00:01:00Z",
            },
            {
                "predicted_team": "Sharks", "correct": True,
                "pct": 72.0, "consensus": "strong", "quarter": "Q1",
                "spread_role": "dog", "avg_edge": 2.0,
                "score_margin_at_fire": 2, "ts": "2026-06-01T00:02:00Z",
            },
        ],
    },
    make_record("game_C", "Panthers", "Sharks", 10, 20, "Sharks", False),
]

print("=== compute_scenario_stats returns total_games (#214) ===")

result = outcomes.compute_scenario_stats(records_multi)
keys = list(result.keys())
check("exactly one scenario key", len(keys) == 1)

if keys:
    stats = result[keys[0]]
    print(f"  Stats: {stats}")

    check("total_calls=4 (1 + 2 + 1)", stats["total_calls"] == 4)
    check("total_games field exists", "total_games" in stats)
    check("total_games=3 (game_A, game_B, game_C)", stats.get("total_games") == 3)
    check("correct=2", stats["correct"] == 2)

# Test with single-call games: total_games == total_calls
records_single = [
    make_record("g1", "Panthers", "Sharks", 10, 20, "Sharks", True),
    make_record("g2", "Panthers", "Sharks", 10, 20, "Sharks", False),
]
result2 = outcomes.compute_scenario_stats(records_single)
stats2 = result2[list(result2.keys())[0]]
check("single-call games: total_calls=2", stats2["total_calls"] == 2)
check("single-call games: total_games=2", stats2.get("total_games") == 2)

# Test with different scenarios: each has its own game count
records_mixed = [
    make_record("g1", "Panthers", "Sharks", 10, 20, "Sharks", True, quarter="Q1"),
    make_record("g1", "Panthers", "Sharks", 10, 20, "Sharks", True, quarter="Q3"),  # different scenario
    make_record("g2", "Panthers", "Sharks", 10, 20, "Sharks", False, quarter="Q1"),
]
result3 = outcomes.compute_scenario_stats(records_mixed)
# Q1 scenario: g1 + g2 = 2 games, 2 calls
# Q3 scenario: g1 = 1 game, 1 call
q1_stats = None
q3_stats = None
for k, v in result3.items():
    if "Q1" in k:
        q1_stats = v
    elif "Q3" in k:
        q3_stats = v
check("Q1 scenario: total_games=2", q1_stats and q1_stats.get("total_games") == 2)
check("Q3 scenario: total_games=1", q3_stats and q3_stats.get("total_games") == 1)


print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
