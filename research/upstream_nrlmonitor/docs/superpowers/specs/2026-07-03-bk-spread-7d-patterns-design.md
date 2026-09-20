# BK Spread — 7D Profitable Patterns, 5D Combo Table, Remove Role Dropdown

**Issue:** NRL#229
**Date:** 2026-07-03
**Status:** Design approved

## Overview

Three changes to the BK Spread sub-view:
1. Remove Role dropdown — compute both leading + trailing strategies per game, with `margin_context` as a dimension
2. Expand pattern detection from current combo pairs to all 21 pairs from 7 dimensions
3. Add a 5D combo table (side, role, margin, entry_minute, margin_context)

## 1. Merge Leading + Trailing Strategies

### Current Flow

```
Dashboard selects role (leading/trailing)
  → /api/odds-monitor-leading?role=leading&threshold=3.0
  → Server runs ONE strategy function per game
  → compute_spread_analysis(history, role="leading")
  → Dashboard renders one perspective
```

### New Flow

```
Dashboard (no role selection)
  → /api/odds-monitor-leading?threshold=3.0
  → Server runs BOTH strategy functions per game
  → Each triggered strategy tagged with margin_context
  → compute_spread_analysis(history)  ← no role param
  → Dashboard renders merged view with Context filter
```

### Server — `/api/odds-monitor-leading` Changes

For each game in odds_monitor_history:
1. Backfill `trailing_team_ev` for legacy snapshots (existing logic)
2. Compute `_compute_leading_strategy(snapshots, game, threshold=thresh)`
3. Compute `_compute_trailing_strategy(snapshots, game, threshold=thresh)`
4. Store both on the game record: `leading_strategy`, `trailing_strategy`
5. For aggregate stats, count triggered bets from BOTH strategies
6. Remove `role` param — endpoint always returns both perspectives

Response shape unchanged except:
- `games[].leading_strategy` and `games[].trailing_strategy` both always present
- `aggregate` covers both strategies combined
- `role` field removed from response (or set to `"both"`)

### `compute_spread_analysis()` Changes

**Remove `role` param.** New signature:

```python
def compute_spread_analysis(history, thresholds=None, game_history=None):
```

For each game, compute BOTH strategies at best threshold:

```python
    for g in history:
        for ctx, compute_fn in [("leading", _compute_leading_strategy),
                                ("trailing", _compute_trailing_strategy)]:
            strat = compute_fn(g.get("snapshots", []), g, threshold=best_thresh)
            if strat.get("triggered"):
                strat["margin_context"] = ctx
                # Find entry odds...
                strategies.append({"strat": strat, "game": g, "entry_odds": entry_odds})
```

Threshold sweep also runs both strategies:

```python
    for thresh in thresholds:
        for g in history:
            for compute_fn in [_compute_leading_strategy, _compute_trailing_strategy]:
                strat = compute_fn(g.get("snapshots", []), g, threshold=thresh)
                if strat.get("triggered"):
                    # accumulate...
```

### `_bucket()` Changes

Add `margin_context` to the returned dict:

```python
        result["margin_context"] = strat.get("margin_context", "unknown")
```

## 2. Expand Pattern Detection to 7D

### 7 Dimensions

1. `side` — home/away
2. `role` — favourite/underdog/unknown (from ML odds at entry)
3. `margin` — 0-6/7-12/13+ (at entry)
4. `entry_minute` — 65-70/71-75/76-80
5. `margin_context` — leading/trailing
6. `spread_price` — $3-4/$4-5/$5+
7. `ev_edge` — negative/neutral/positive

### Combo Pairs

All 21 pairs (7 choose 2):

```python
    combo_pairs = list(itertools.combinations(
        ["side", "role", "margin", "entry_minute", "margin_context",
         "spread_price", "ev_edge"], 2))
```

Or explicitly listed (no itertools import needed):

```python
    combo_pairs = [
        ("side", "role"), ("side", "margin"), ("side", "entry_minute"),
        ("side", "margin_context"), ("side", "spread_price"), ("side", "ev_edge"),
        ("role", "margin"), ("role", "entry_minute"), ("role", "margin_context"),
        ("role", "spread_price"), ("role", "ev_edge"),
        ("margin", "entry_minute"), ("margin", "margin_context"),
        ("margin", "spread_price"), ("margin", "ev_edge"),
        ("entry_minute", "margin_context"), ("entry_minute", "spread_price"),
        ("entry_minute", "ev_edge"),
        ("margin_context", "spread_price"), ("margin_context", "ev_edge"),
        ("spread_price", "ev_edge"),
    ]
```

Profitable patterns: ROI > 20% and ≥3 games. Anti-patterns: ROI < -30% and ≥3 games. Same thresholds as current.

The #226-added combo pairs (involving `has_ht_turnaround`, `ht_position`) are removed since those dimensions are not in the 7D set. They were for game_history enrichment which is less relevant now that the core 7 BK dimensions cover the key factors.

## 3. 5D Combo Table

5 dimensions: **side, role, margin, entry_minute, margin_context**

### Computation

Same pattern as historical 5D table:

```python
    combo_5d_buckets = defaultdict(lambda: {"games": 0, "covered": 0, "pnl": 0.0})
    for s in strategies:
        b = _bucket(s)
        key = (b["side"], b["role"], b["margin"], b["entry_minute"], b["margin_context"])
        combo_5d_buckets[key]["games"] += 1
        if s["strat"].get("covered"):
            combo_5d_buckets[key]["covered"] += 1
        if s["strat"].get("pnl") is not None:
            combo_5d_buckets[key]["pnl"] += s["strat"]["pnl"]

    combo_5d = []
    for (side, role, margin, entry, ctx), val in combo_5d_buckets.items():
        if val["games"] < 3:
            continue
        val["pnl"] = round(val["pnl"], 2)
        val["roi"] = round(val["pnl"] / val["games"] * 100, 1) if val["games"] else None
        combo_5d.append({
            "side": side, "role": role, "margin": margin,
            "entry_minute": entry, "margin_context": ctx,
            "games": val["games"], "covered": val["covered"],
            "pnl": val["pnl"], "roi": val["roi"],
            "low_confidence": val["games"] < 5,
        })
    combo_5d.sort(key=lambda c: abs(c.get("roi") or 0), reverse=True)
```

### Response

Added to `compute_spread_analysis()` return dict:

```python
    return {
        ...,
        "combo_5d": combo_5d,
    }
```

## Dashboard

### Layout Changes

```
BK Spread Coverage view
├── Header: Price threshold + Set as Default + Refresh + [BK Spread | Historical]
│   (Role dropdown REMOVED)
│
├── Aggregate stats (merged leading + trailing)
├── Leading tries probability reference
│
├── History table
│   ├── New "Context" column (Leading/Trailing)
│   ├── Both strategies shown per game (two rows if both triggered)
│   └── Filter dropdowns: Side, Role, Margin, Entry, EV Edge + Context (new)
│
└── Spread Analysis (collapsible)
    ├── Profitable / Anti patterns (7D pairs)
    ├── Threshold sweep
    ├── Dimensional breakdowns (existing dims + margin_context)
    ├── Top 2D Combos (from 21 pairs)
    └── 📊 5D Combo Table (new)
        ├── Headers: Side | Role | Margin | Entry Min | Context | Games | Covered | P&L | ROI
        ├── Sortable by any column
        ├── Click dimension cell → set filter
        └── ≥3 games, ⚠️ for <5
```

### History Table Changes

Currently each game shows one row (for the selected role's strategy). Now each game can show up to two rows:
- One for leading_strategy (if triggered)
- One for trailing_strategy (if triggered)

Add `data-lev-context` attribute to each row for filtering.

New "Context" filter dropdown in filter bar:
```html
<select id="lev-f-context">
  <option value="">All</option>
  <option value="leading">Leading</option>
  <option value="trailing">Trailing</option>
</select>
```

### 5D Combo Table

Same pattern as historical view but with BK metrics (Covered, P&L, ROI instead of Wins, Win Rate):

```html
<details>
  <summary>📊 5D Combo Table (N combos)</summary>
  <table>
    <thead>Side | Role | Margin | Entry Min | Context | Games | Covered | P&L | ROI</thead>
    <tbody>...sortable, clickable cells...</tbody>
  </table>
</details>
```

### `loadLeadingEv()` Changes

- Remove `role` from fetch URL — no longer sends `?role=...`
- For each game, render BOTH leading_strategy and trailing_strategy rows (if triggered)
- Add Context column + filter
- Remove role-based label switching

### `loadSpreadAnalysis()` Changes

- Remove `role` from fetch URL
- Render 5D combo table after 2D combos
- Pattern rendering unchanged (server returns expanded 7D patterns)

### `_levSwitchView()` Changes

- Remove BK role dropdown hiding/showing logic (dropdown no longer exists)

### `_levSavePrice()` Changes

- Remove role-specific config key logic — save a single threshold (or keep both keys for backwards compat and save same value to both)

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Remove `role` param from `compute_spread_analysis()`. Run both strategies per game. Add `margin_context` to `_bucket()`. Expand `combo_pairs` to 21 pairs from 7 dims. Add `combo_5d` computation. Add `margin_context` to `dim_names`. |
| `server.py` | `/api/odds-monitor-leading`: remove `role` param, run both strategies per game, merge aggregates. `/api/odds-monitor-analysis`: remove `role` param from call. |
| `dashboard.html` | Remove role dropdown. Update `loadLeadingEv()` to render both strategies. Add Context filter + column. Update `loadSpreadAnalysis()` to render 5D combo table. Remove role references from `_levSwitchView`, `_levOnRoleChange`, `_levSavePrice`. |

## Out of Scope

- Separate threshold for leading vs trailing strategies (use same threshold for both)
- Historical Win Rate view changes (unaffected)
- Client-side min games/pp filters for BK view (only in historical view for now)
