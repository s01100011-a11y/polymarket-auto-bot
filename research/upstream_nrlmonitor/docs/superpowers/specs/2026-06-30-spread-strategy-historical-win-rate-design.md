# Spread Strategy: Historical Win Rate Analysis

**Issue:** NRL#226
**Date:** 2026-06-30
**Status:** Design approved

## Overview

Enrich the Spread Strategy tab with a new "Historical Win Rate" sub-view that analyses whether the team leading or trailing at minute 70 wins the game, using the full game_history dataset (870+ games) cross-referenced with PBP cache data. This complements the existing BK-tracked spread coverage analysis.

## Investigation Summary

> **Note:** "pp" throughout this document means **percentage points** — the absolute difference between two percentages. For example, 97.0% vs 64.2% = 32.8pp.

Analysis of 870 games with PBP-derived score at minute 70 found:

- **Leading team at min 70 wins 93.1%** of the time (810/870)
- **Trailing team comebacks: 6.9%** (60/870)
- `has_halftime_turnaround` condition is the strongest predictor: drops leading team win rate to 64.2%, elevates trailing comeback rate to 35.8%
- Margin at 70 cleanly separates outcomes: 7+ point leads are 98-100% safe; 0-6 is the danger zone (76.4%)
- `leading@HT + has_halftime_turnaround` combo: 0% win rate for the min-70 leader (23 games)
- `has_momentum_shift` is a strong positive signal for the leader (97.0%)

## Architecture

### Approach C: Layered — Existing BK Analysis + Historical Win Rate Toggle

Two sub-views in the Spread Strategy tab, toggled by the user:

1. **BK Spread Coverage** — existing analysis enhanced with new dimensions
2. **Historical Win Rate** — new 870-game analysis from game_history + PBP cache

### Why Two Views

- Different data sources: odds_monitor_history (BK snapshots) vs game_history + PBP cache
- Different metrics: spread cover + P&L/ROI vs win/loss rate
- Different sample sizes: ~41 BK-tracked vs ~870 historical
- Clean separation avoids mixing apples and oranges

## Backend

### New Function: `compute_historical_win_analysis()`

**Location:** `odds_monitor.py`

**Signature:**
```python
def compute_historical_win_analysis(game_history, role="leading", target_minute=70):
```

**Parameters:**
- `game_history`: list of game records from `load_history()`
- `role`: `"leading"` — win rate of team leading at target minute; `"trailing"` — comeback rate of trailing team
- `target_minute`: game minute to evaluate score (default 70)

**Processing:**
1. For each game, resolve PBP cache file via `match_id_to_cache_path(match_id)`
2. Derive score at `target_minute` from timeline scoring events (Try=4, Goal=2, Penalty Goal=2, Field Goal=1)
3. Skip tied-at-minute games and final draws
4. Determine leading/trailing team and whether they won
5. Compute per-game features and bucket into dimensions
6. Run 2D combo analysis on dimension pairs
7. Detect profitable patterns (win rate > baseline + 10pp, ≥5 games) and anti-patterns (< baseline - 10pp, ≥5 games)

**Dimensions (10 + 1 optional):**

| Dimension | Buckets | Source |
|-----------|---------|--------|
| side | home, away | PBP-derived leading side |
| role_fav_underdog | favourite, underdog, unknown | Pregame `home_odds`/`away_odds` or `home_spread` sign |
| margin_at_70 | 0-6, 7-12, 13-20, 21+ | PBP-derived absolute margin |
| total_points | 0-30, 31-40, 41-50, 51+ | Final game total |
| team | Team names | Leading/trailing team name |
| competition | regular_season, etc. | `season_segment` |
| halftime_context | `{ht_margin_bucket} ({position}@HT)` | `halftime_margin` — perspective-relative |
| margin_trajectory | lead_growing >4/1-4, stable, shrinking 1-4/>4 | HT margin vs PBP margin at 70 |
| key_conditions | has_halftime_turnaround, has_momentum_shift, neither | `conditions_fired[].type` |
| postgame_tags | BLOWOUT, CLOSE, COMEBACK, no_tags | `postgame_tags[]` |
| spread_line_size (optional) | 1-3.5, 4-7.5, 8-13.5, 14+ | `home_spread` where available (~421 games) |

**2D Combo Pairs (11):**
- side × margin_at_70, side × role
- role × margin_at_70, role × ht_position, role × has_ht_turnaround, role × has_momentum_shift
- margin_at_70 × ht_position, margin_at_70 × has_ht_turnaround, margin_at_70 × has_momentum_shift
- team × role
- ht_position × has_ht_turnaround

**Return Structure:**
```python
{
    "baseline": {"games": int, "wins": int, "rate": float},
    "dimensions": {
        "margin_at_70": {
            "0-6": {"games": int, "wins": int, "rate": float, "low_confidence": bool},
            ...
        },
        ...
    },
    "combos": [
        {"dims": str, "key": str, "games": int, "wins": int, "rate": float},
        ...
    ],
    "profitable_patterns": [...],  # rate > baseline + 10pp, ≥5 games
    "anti_patterns": [...],        # rate < baseline - 10pp, ≥5 games
    "total_games": int,
    "tied_at_minute": int,
    "draws": int,
    "cache_misses": int,
    "role": str,
    "target_minute": int
}
```

### PBP Cache Helpers

Add to `odds_monitor.py` (or a small helper module):

```python
def match_id_to_cache_path(match_id):
    """Convert match_id to nrl_cache file path."""

def score_at_minute(cache_path, target_minute):
    """Derive home/away score at a game minute from PBP timeline."""
```

These are extracted from the investigation script. Logic:
- Parse match_id: `/draw/nrl-premiership/2026/round-17/sea-eagles-v-storm/` → `nrl_cache/2026/match_sea-eagles-v-storm_r17.json`
- Walk timeline events, accumulate scoring points up to `target_minute * 60` gameSeconds

### Modifications to Existing `compute_spread_analysis()`

**Add new dimensions** (sourced by matching `match_id` to game_history records passed as optional param):
- halftime_context
- margin_trajectory (HT → entry minute)
- key_conditions (has_halftime_turnaround, has_momentum_shift)
- postgame_tags
- spread_line_size

**EV edge:** Kept as-is — only available for BK-tracked games with live odds snapshots.

**New parameter:**
```python
def compute_spread_analysis(history, role="leading", thresholds=None, game_history=None):
```
When `game_history` is provided, match records by `match_id` to enrich BK-tracked games with the new dimensional data.

**New combo pairs** added to existing 10:
- side × has_ht_turnaround
- role × has_ht_turnaround
- margin × has_ht_turnaround
- margin × ht_position

## API

### Existing Endpoint (unchanged)

`GET /api/odds-monitor-analysis?role=leading|trailing`

Enhanced response includes new dimensions when game_history data is available for matching. Server passes `game_history=load_history()` to `compute_spread_analysis()`.

### New Endpoint

`GET /api/odds-monitor-historical?role=leading|trailing|final_stretch&minute=70`

**Parameters:**
- `role` (required): `"leading"`, `"trailing"`, or `"final_stretch"` (who outscores in last 10 min — score reset to 0-0 at target minute)
- `minute` (optional, default 70): target game minute for score evaluation

**Response:** Output of `compute_historical_win_analysis()`

**Implementation:** Server loads `game_history` via `load_history()`, calls `compute_historical_win_analysis()`, returns JSON.

**Caching:** Consider in-memory cache keyed by `(role, minute)` with TTL or invalidation on game_history change, since PBP cache is static and game_history changes infrequently.

## Dashboard

### Layout

```
Spread Strategy
├── View toggle: [BK Spread Coverage] | [Historical Win Rate]
├── Role dropdown (Leading / Trailing) ← BK view only
│
├── === BK Spread Coverage (existing, enhanced) ===
│   ├── Price threshold input + Set as Default + Refresh
│   ├── History table (per-game rows, BK-tracked only)
│   ├── Filter dropdowns (Side, Role, Margin, Entry, EV Edge)
│   └── Spread Analysis (collapsible)
│       ├── Profitable / Anti patterns
│       ├── Threshold sweep table
│       ├── Dimensional breakdown (original dims + 5 new)
│       └── Top 2D combos (expanded pairs)
│
└── === Historical Win Rate (new) ===
    ├── Perspective dropdown (Leading Team / Trailing Team / Final Stretch)
    ├── Minute selector dropdown (65/68/70/72/75, default 70)
    ├── Baseline stats card
    │   Leading: "Leading at min 70 wins 93.1% (810/870)"
    │   Trailing: "Trailing at min 70 comebacks 6.9% (60/870)"
    │   Final Stretch: "Home outscores in final stretch 52.3% (456/872)"
    ├── Dimensional breakdown (10 dimensions + spread_line_size)
    │   Each dimension card: table of buckets with Games, Wins, Win%, flag
    ├── Profitable / Anti patterns
    └── Top 2D combos
```

### View Toggle

- Two buttons styled as sub-tabs (matching existing dashboard patterns like PW Trends chart mode buttons)
- Active view shown, other hidden via `display:none`
- State persisted to `localStorage['lev_view']`
- Historical view has its own "Perspective" dropdown (Leading/Trailing/Final Stretch) separate from the BK role dropdown

### Role / Perspective Dropdown Behaviour

- **BK view** uses the existing Role dropdown (Leading/Trailing) — unchanged
- **Historical view** has its own Perspective dropdown with three options:
  - **Leading Team:** win rate of team leading at min X (93.1% baseline)
  - **Trailing Team:** comeback rate of team trailing at min X (6.9% baseline)
  - **Final Stretch (0-0 reset):** who outscores in the last 10 min, ignoring accumulated lead — analyses each team's late-game scoring ability given contextual factors. Baseline ~50% (slight home advantage). Most actionable for spread betting since spread lines are themselves a "score reset."
- Changing either dropdown re-fetches the active view's endpoint

### BK View Enhancements

New dimension cards appended after existing ones:
- **Halftime Context** — e.g. "13+ (leading@HT): 5 games, 4 covered, 80.0%"
- **Margin Trajectory** — e.g. "lead_growing (>4pts): 12 games, 10 covered, 83.3%"
- **Key Conditions** — has_halftime_turnaround / has_momentum_shift / neither
- **Postgame Tags** — BLOWOUT / CLOSE / COMEBACK / no_tags
- **Spread Line Size** — 1-3.5 / 4-7.5 / 8-13.5 / 14+

These show covered/P&L/ROI like existing dimension cards (BK data available for all these games).

### Historical View

**Baseline stats card:**
- Prominent card at top showing baseline win/comeback rate
- Format: "Leading team at min {X} wins {rate}% ({wins}/{games})"
- Includes metadata: "{tied} tied games excluded, {misses} cache misses"

**Minute selector:**
- Small dropdown: 65, 68, 70, 72, 75
- Default: 70
- Changing triggers re-fetch of `/api/odds-monitor-historical?role=...&minute=...`
- Persisted to `localStorage['lev_hist_minute']`

**Dimensional breakdown:**
- Same collapsible card pattern as BK view's Spread Analysis section
- Each dimension: sorted table of buckets
- Columns: Bucket, Games, Wins, Win Rate
- Low-confidence flag (⚠️) for buckets with <10 games
- Win rate bar or colour coding: green >90%, yellow 75-90%, red <75% (leading); inverted for trailing

**Patterns section:**
- Profitable patterns: green cards, sorted by deviation from baseline
- Anti-patterns: red cards
- Format: "{dim1} × {dim2}: {value1} + {value2} — {rate}% ({wins}/{games}), {delta}pp vs baseline"

**2D Combos:**
- Same table format as BK view
- Columns: Dimensions, Pattern, Games, Wins, Win Rate, vs Baseline
- Sorted by absolute deviation from baseline

### localStorage Keys

| Key | Values | Default |
|-----|--------|---------|
| `lev_view` | `"bk"` / `"historical"` | `"bk"` |
| `lev_hist_minute` | `"65"` / `"68"` / `"70"` / `"72"` / `"75"` | `"70"` |
| `lev_role` | `"leading"` / `"trailing"` | `"leading"` (existing) |
| `lev_filters` | JSON object | (existing) |

### Fetch Flow

```
View toggle change or Role change
  → if BK view active:
      fetch /api/odds-monitor-leading?role={role}&threshold={price}  (existing)
      fetch /api/odds-monitor-analysis?role={role}                   (existing)
  → if Historical view active:
      fetch /api/odds-monitor-historical?role={role}&minute={minute}  (new)

Minute selector change (Historical view only):
  → fetch /api/odds-monitor-historical?role={role}&minute={minute}
```

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Add `match_id_to_cache_path()`, `score_at_minute()`, `compute_historical_win_analysis()`. Add `game_history` param + new dimensions to `compute_spread_analysis()`. |
| `server.py` | Add `/api/odds-monitor-historical` endpoint. Pass `game_history` to existing spread analysis call. |
| `dashboard.html` | Add view toggle, historical view HTML structure, `loadHistoricalWinRate()` JS function, minute selector, localStorage persistence. Add new dimension cards to BK Spread Analysis section. |

## Out of Scope

- Pagination or per-game table for historical view (870 games — aggregate only)
- Statistical significance testing (chi-squared, p-values) — may add later
- Finals/SOO games (only regular_season in current data)
- Combining BK and historical data in a single dimensional view
- NRL ↔ NBA cross-repo parity (NRL-only feature)
