#!/usr/bin/env python3
"""Backfill q_correct and h1_correct onto PW call records.

q_correct:  Did the predicted team win the quarter the call was in?
            Derived from score_progression (gameSeconds boundaries).
h1_correct: Did the predicted team win the first half?
            Derived from home_score_1h / away_score_1h.
            Only set on Q1/Q2 calls.

Usage:
    python3 backfill_q_correct.py              # dry run
    python3 backfill_q_correct.py --write      # persist
"""

import json, os, sys

HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "game_history.json")

# NRL quarter boundaries in gameSeconds (4 x 20min = 80min total)
Q_BOUNDS = {
    "Q1": (0, 1200),
    "Q2": (1200, 2400),
    "Q3": (2400, 3600),
    "Q4": (3600, 4800),
}


def score_at(sp, seconds):
    """Return (homeScore, awayScore) at a given gameSeconds boundary."""
    last_home, last_away = 0, 0
    for entry in sp:
        if entry["gameSeconds"] <= seconds:
            last_home = entry["homeScore"]
            last_away = entry["awayScore"]
        else:
            break
    return last_home, last_away


def quarter_winner(sp, quarter, home_team, away_team):
    """Return the team name that won the given quarter, or None if tied/unknown."""
    bounds = Q_BOUNDS.get(quarter)
    if not bounds:
        return None
    start_s, end_s = bounds
    h_start, a_start = score_at(sp, start_s) if start_s > 0 else (0, 0)
    h_end, a_end = score_at(sp, end_s)
    h_q = h_end - h_start
    a_q = a_end - a_start
    if h_q > a_q:
        return home_team
    elif a_q > h_q:
        return away_team
    return None  # tied


def h1_winner(game):
    """Return the team name that won the first half, or None if tied."""
    h1h = game.get("home_score_1h")
    a1h = game.get("away_score_1h")
    if h1h is None or a1h is None:
        return None
    if h1h > a1h:
        return game["home_team"]
    elif a1h > h1h:
        return game["away_team"]
    return None


def main():
    write_mode = "--write" in sys.argv

    with open(HISTORY_PATH) as f:
        games = json.load(f)

    total_calls = 0
    q_set = 0
    h1_set = 0
    q_correct_count = 0
    h1_correct_count = 0
    skipped_no_sp = 0

    for g in games:
        calls = g.get("predicted_winner_calls")
        if not calls:
            continue

        home_team = g.get("home_team", "")
        away_team = g.get("away_team", "")
        sp = g.get("score_progression")
        h1w = h1_winner(g)

        for c in calls:
            total_calls += 1
            predicted = c.get("predicted_team", "")
            quarter = c.get("quarter", "")

            # q_correct from score_progression
            if sp and quarter in Q_BOUNDS:
                qw = quarter_winner(sp, quarter, home_team, away_team)
                if qw is not None:
                    c["q_correct"] = (predicted == qw)
                    q_set += 1
                    if c["q_correct"]:
                        q_correct_count += 1
                else:
                    c["q_correct"] = None  # tied quarter
                    q_set += 1
            elif not sp and quarter in Q_BOUNDS:
                skipped_no_sp += 1

            # h1_correct from home_score_1h / away_score_1h (Q1/Q2 only)
            if quarter in ("Q1", "Q2"):
                if h1w is not None:
                    c["h1_correct"] = (predicted == h1w)
                    h1_set += 1
                    if c["h1_correct"]:
                        h1_correct_count += 1
                else:
                    c["h1_correct"] = None  # tied H1
                    h1_set += 1

    q_with_val = sum(1 for g in games for c in g.get("predicted_winner_calls", []) if c.get("q_correct") is True or c.get("q_correct") is False)
    h1_with_val = sum(1 for g in games for c in g.get("predicted_winner_calls", []) if c.get("h1_correct") is True or c.get("h1_correct") is False)

    print(f"Total PW calls processed: {total_calls}")
    print(f"q_correct set: {q_set} ({q_correct_count} correct / {q_with_val} non-null = {q_correct_count/q_with_val*100:.1f}%)" if q_with_val else f"q_correct set: {q_set}")
    print(f"h1_correct set: {h1_set} ({h1_correct_count} correct / {h1_with_val} non-null = {h1_correct_count/h1_with_val*100:.1f}%)" if h1_with_val else f"h1_correct set: {h1_set}")
    print(f"Skipped (no score_progression): {skipped_no_sp}")

    if write_mode:
        tmp = HISTORY_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(games, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, HISTORY_PATH)
        print(f"\nWritten to {HISTORY_PATH}")
    else:
        print("\nDry run — pass --write to persist")


if __name__ == "__main__":
    main()
