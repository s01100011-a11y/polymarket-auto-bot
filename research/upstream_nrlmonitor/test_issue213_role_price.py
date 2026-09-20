#!/usr/bin/env python3
"""Tests for NRL #213: trailing_team_ev in odds monitor snapshots."""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import odds_monitor

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


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_odds(home_spread=-6.5, home_spread_price=1.65,
              away_spread=6.5, away_spread_price=2.20):
    return {
        "home_spread": home_spread,
        "home_spread_price": home_spread_price,
        "away_spread": away_spread,
        "away_spread_price": away_spread_price,
        "home_ml": 1.65,
        "away_ml": 2.30,
        "total_over": 42.5,
        "total_over_price": 1.85,
        "total_under_price": 1.95,
        "bookmaker": "tab",
    }


def make_nrl(home_score, away_score, game_minute=70):
    return {
        "home_score": home_score,
        "away_score": away_score,
        "game_minute": game_minute,
        "period": "2H",
        "try_events": [],
        "activate_minute": 65,
    }


GAME_INFO = {"home_team": "Panthers", "away_team": "Sharks"}


# ── Tests: _compute_trailing_team_ev ─────────────────────────────────────────

print("=== _compute_trailing_team_ev (NRL #213) ===")

# When home leads (20-10), trailing team is away
odds = make_odds()
nrl = dict(make_nrl(20, 10))
nrl["home_team"] = "Panthers"
nrl["away_team"] = "Sharks"

result = odds_monitor._compute_trailing_team_ev(odds, nrl, 70, {})

check("trailing_team key present", "trailing_team" in result)
check("trailing_side key present", "trailing_side" in result)
check("qualifies key present", "qualifies" in result)
check("when home leads, trailing_team is away team",
      result.get("trailing_team") == "Sharks")
check("trailing_side is 'away'", result.get("trailing_side") == "away")

# Trailing team's spread_price comes from the away side
check("spread_price is away_spread_price (2.20)",
      result.get("spread_price") == 2.20)

# Trailing team's spread_line comes from the away side
check("spread_line is away_spread (6.5)",
      result.get("spread_line") == 6.5)

# qualifies True when spread_price >= threshold (default 3.0 — 2.20 < 3.0 → False)
check("qualifies False when spread_price < default threshold",
      result.get("qualifies") is False)

# qualifies True with lower threshold
result_low = odds_monitor._compute_trailing_team_ev(odds, nrl, 70, {"odds_monitor_trailing_threshold": 2.0})
check("qualifies True when spread_price >= custom threshold (2.20 >= 2.0)",
      result_low.get("qualifies") is True)

# When away leads (10-20), trailing team is home
nrl2 = dict(make_nrl(10, 20))
nrl2["home_team"] = "Panthers"
nrl2["away_team"] = "Sharks"

result2 = odds_monitor._compute_trailing_team_ev(odds, nrl2, 70, {})
check("when away leads, trailing_team is home team",
      result2.get("trailing_team") == "Panthers")
check("trailing_side is 'home' when away leads",
      result2.get("trailing_side") == "home")
check("spread_price is home_spread_price (1.65)",
      result2.get("spread_price") == 1.65)
check("spread_line is home_spread (-6.5)",
      result2.get("spread_line") == -6.5)

# When scores are tied, trailing_team is None
nrl_tied = dict(make_nrl(10, 10))
nrl_tied["home_team"] = "Panthers"
nrl_tied["away_team"] = "Sharks"
result_tied = odds_monitor._compute_trailing_team_ev(odds, nrl_tied, 70, {})
check("when tied, trailing_team is None",
      result_tied.get("trailing_team") is None)
check("when tied, qualifies is False",
      result_tied.get("qualifies") is False)

# Falls back to odds_monitor_leading_threshold when trailing-specific key absent
result_fb = odds_monitor._compute_trailing_team_ev(
    odds, nrl, 70, {"odds_monitor_leading_threshold": 2.0}
)
check("falls back to odds_monitor_leading_threshold when trailing key absent",
      result_fb.get("qualifies") is True)


# ── Tests: create_snapshot includes trailing_team_ev ─────────────────────────

print("\n=== create_snapshot includes trailing_team_ev (NRL #213) ===")

snapshot = odds_monitor.create_snapshot(
    "match123",
    GAME_INFO,
    make_odds(),
    make_nrl(20, 10),
    config={},
)

check("snapshot has trailing_team_ev key", "trailing_team_ev" in snapshot)
trailing = snapshot.get("trailing_team_ev", {})
check("trailing_team_ev.trailing_team is 'Sharks'",
      trailing.get("trailing_team") == "Sharks")
check("trailing_team_ev.trailing_side is 'away'",
      trailing.get("trailing_side") == "away")
check("trailing_team_ev.spread_price is 2.20",
      trailing.get("spread_price") == 2.20)
check("snapshot still has leading_team_ev key",
      "leading_team_ev" in snapshot)
check("leading_team_ev.leading_team is 'Panthers'",
      snapshot["leading_team_ev"].get("leading_team") == "Panthers")


# ── Tests: _compute_trailing_strategy ────────────────────────────────────────

print("\n=== _compute_trailing_strategy (NRL #213) ===")

# Snapshot helpers
def make_trailing_snapshot(minute, qualifies, trailing_team, trailing_side,
                           spread_line, spread_price, margin):
    return {
        "game_minute": minute,
        "trailing_team_ev": {
            "qualifies": qualifies,
            "trailing_team": trailing_team,
            "trailing_side": trailing_side,
            "spread_line": spread_line,
            "spread_price": spread_price,
            "margin": margin,
            "ev_edge": 0.12,
        },
    }


# Two snapshots: first does not qualify, second qualifies
# Trailing team: Rabbitohs (away), spread_line=10.5, spread_price=3.20
# Record: home=30, away=22 → away margin = 22-30 = -8; covered = (-8 + 10.5) > 0 = True
snaps_trigger = [
    make_trailing_snapshot(70, False, "Rabbitohs", "away", 10.5, 3.20, -8),
    make_trailing_snapshot(73, True,  "Rabbitohs", "away", 10.5, 3.20, -8),
]
record_trigger = {"final_home_score": 30, "final_away_score": 22}

ts_result = odds_monitor._compute_trailing_strategy(snaps_trigger, record_trigger)

check("triggered is True when qualifying snapshot exists",
      ts_result.get("triggered") is True)
check("team is 'Rabbitohs'",
      ts_result.get("team") == "Rabbitohs")
check("side is 'away'",
      ts_result.get("side") == "away")
check("entry_minute is 73 (first qualifying snapshot)",
      ts_result.get("entry_minute") == 73)
check("spread_line is 10.5",
      ts_result.get("spread_line") == 10.5)
check("spread_price is 3.20",
      ts_result.get("spread_price") == 3.20)
check("margin_at_entry is -8",
      ts_result.get("margin_at_entry") == -8)
check("ev_edge is 0.12",
      ts_result.get("ev_edge") == 0.12)

# covered: away_margin + spread_line > 0 → (-8 + 10.5) = 2.5 > 0 → True
check("covered is True when away margin + spread_line > 0",
      ts_result.get("covered") is True)

# pnl: spread_price - 1 = 3.20 - 1 = 2.20 when covered
check("pnl is 2.20 when covered",
      ts_result.get("pnl") == 2.20)

# Not covered case: home=30, away=18 → away margin = -12; (-12 + 10.5) = -1.5 → False
record_not_covered = {"final_home_score": 30, "final_away_score": 18}
ts_not_covered = odds_monitor._compute_trailing_strategy(snaps_trigger, record_not_covered)

check("covered is False when team_margin + spread_line <= 0",
      ts_not_covered.get("covered") is False)
check("pnl is -1.0 when not covered",
      ts_not_covered.get("pnl") == -1.0)

# No qualifying snapshot → triggered False
snaps_none = [
    make_trailing_snapshot(70, False, "Rabbitohs", "away", 10.5, 3.20, -8),
    make_trailing_snapshot(73, False, "Rabbitohs", "away", 10.5, 3.20, -8),
]
ts_no_trigger = odds_monitor._compute_trailing_strategy(snaps_none, record_trigger)

check("triggered is False when no qualifying snapshot",
      ts_no_trigger.get("triggered") is False)
check("no 'team' key when not triggered",
      "team" not in ts_no_trigger)


# ── Summary ───────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"Results: {PASS} passed, {FAIL} failed")
if FAIL > 0:
    sys.exit(1)
