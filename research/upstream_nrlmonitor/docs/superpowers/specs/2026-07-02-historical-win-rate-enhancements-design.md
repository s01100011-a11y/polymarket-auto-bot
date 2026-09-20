# Historical Win Rate — Min Games/pp Filters, Margin Multiselect, Dual Pattern Tables

**Issue:** NRL#228
**Date:** 2026-07-02
**Status:** Design approved
**Parent:** NRL#227 (filters, closed), NRL#226 (sub-view, closed)

> **Note:** "pp" means **percentage points** — the absolute difference between two percentages.

## Overview

Three enhancements to the Historical Win Rate sub-view:
1. New client-side "Min Games" and "Min pp" filters
2. Change margin bucketing globally from 4 → 3 buckets with multiselect checkboxes
3. Dual Leading + Trailing pattern tables in Final Stretch perspective

## 1. Margin Bucketing: 4 → 3 Buckets

### Change

**Old:** `[(6, "0-6"), (12, "7-12"), (20, "13-20"), (None, "21+")]`
**New:** `[(6, "0-6"), (12, "7-12"), (None, "13+")]`

Rationale: 13-20 (98.6% win rate) and 21+ (100%) have near-identical outcomes. Collapsing into a single 13+ bucket reduces noise without losing signal.

### Scope

This changes `_MARGIN_RANGES` in `odds_monitor.py` which is used by:
- `compute_historical_win_analysis()` — dimensional breakdown, 5D combos, post-filters
- `compute_spread_analysis()` — BK spread analysis `_bucket()` helper (margin dimension)
- Both functions share the same constant

### Dashboard Filter

Replace the margin dropdown with multiselect checkboxes (matching the minute checkbox pattern):

```html
Margin: ☑0-6 ☑7-12 ☑13+
```

All checked by default. CSS class `lev-margin-cb` for JS selection.

### API

`margin_bucket` param changes from single value to comma-separated:
- Old: `margin_bucket=0-6`
- New: `margin_bucket=0-6,13+`
- `all` still means no filter (all checked = send `all` or omit param)

Server-side filter change in `compute_historical_win_analysis()`:

```python
if margin_bucket and margin_bucket != "all":
    allowed = set(margin_bucket.split(","))
    games = [g for g in games if _hist_bucket(g["_margin"], _MARGIN_RANGES) in allowed]
```

## 2. Min Games + Min pp Filters

### Client-Side Only

No server changes. These filter the rendered output in JS after data arrives from the API.

### Min Games (default 5)

Numeric input (spinner, min 1, max 100). Hides:
- Dimensional breakdown rows where `bucket.games < minGames`
- Profitable/anti pattern entries where `pattern.games < minGames`
- 2D combo rows where `combo.games < minGames`
- 5D combo rows where `combo.games < minGames`

Does NOT affect: baseline stats, game list, filter note.

### Min pp (default 10)

Numeric input (spinner, min 0, max 50, step 1). Hides:
- Profitable/anti pattern entries where `abs(pattern.rate - baseline) < minPp`
- 2D combo rows where `abs(combo.rate - baseline) < minPp`
- 5D combo rows where `abs(combo.rate - baseline) < minPp`

Does NOT affect: dimensional breakdowns (show all buckets), baseline, game list.

### Filter Bar Placement

After the Reset button:

```html
... [Reset] <span style="border-left:...">Min Games: <input type="number" value="5" ...>
Min pp: <input type="number" value="10" ...></span>
```

Both inputs trigger re-render (not re-fetch) via a `_levHistApplyMinFilters()` function that shows/hides rows.

### Implementation Approach

Rather than re-fetching, the JS rendering functions apply min filters during HTML generation. The `loadHistoricalWinRate()` function stores `minGames` and `minPp` values and uses them when building pattern/combo HTML:

```javascript
// In pattern rendering:
for (const p of profs) {
  if (p.games < minGames) continue;
  if (Math.abs(p.rate - bl.rate) < minPp) continue;
  // render pattern
}
```

Same filtering applied to 2D combos, 5D combos. Dimensional breakdown tables filter by `minGames` only (not `minPp`).

When min filter inputs change, call `loadHistoricalWinRate()` to re-render with current data (the fetch URL hasn't changed, so use cached response or re-fetch — simplest is re-fetch since server computation is fast).

### localStorage

`lev_hist_filters` extended with:
```json
{
  "min_games": 5,
  "min_pp": 10,
  ...existing filters...
}
```

## 3. Dual Pattern Tables in Final Stretch

### When Active

Only when `perspective === 'final_stretch'`.

### Implementation

After the main Final Stretch fetch completes, fire two additional fetches in parallel:
- `/api/odds-monitor-historical?role=leading&...` (same filters except role)
- `/api/odds-monitor-historical?role=trailing&...` (same filters except role)

### Layout

Above the main Final Stretch patterns, render a 2-column grid:

```
┌──────────────────────────────┐  ┌──────────────────────────────┐
│ Leading — Profitable         │  │ Trailing — High Comeback     │
│ pattern1: 97.0% (525g)       │  │ pattern1: 35.8% (67g)        │
│ pattern2: ...                │  │ pattern2: ...                │
├──────────────────────────────┤  ├──────────────────────────────┤
│ Leading — Anti-Patterns      │  │ Trailing — Low Comeback      │
│ pattern1: 64.2% (67g)        │  │ pattern1: 3.0% (525g)        │
│ ...                          │  │ ...                          │
└──────────────────────────────┘  └──────────────────────────────┘

── Final Stretch Outscoring Patterns ──
(existing patterns from final_stretch perspective)
```

CSS: `display:grid; grid-template-columns: 1fr 1fr; gap:8px`

### Min Games/pp Applied

The dual pattern tables also respect Min Games and Min pp filters (same client-side filtering).

### Data Flow

```
Final Stretch selected:
  → fetch /api/odds-monitor-historical?role=final_stretch&...
  → fetch /api/odds-monitor-historical?role=leading&...     (parallel)
  → fetch /api/odds-monitor-historical?role=trailing&...    (parallel)
  → render dual patterns (leading + trailing) in grid
  → render main final_stretch analysis below
```

For Leading/Trailing perspectives (not Final Stretch), no dual fetch — patterns render as before.

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Change `_MARGIN_RANGES` from 4 → 3 buckets. Update `margin_bucket` filter to handle comma-separated values. |
| `server.py` | No changes (margin_bucket already passed as string, splitting happens in odds_monitor). |
| `dashboard.html` | Replace margin dropdown with checkboxes. Add Min Games + Min pp inputs. Apply min filters in rendering. Add dual pattern fetch + grid layout for Final Stretch. Update localStorage schema. |

## Out of Scope

- Server-side min games/pp filtering (kept client-side for simplicity)
- Dual patterns for Leading/Trailing perspectives (Final Stretch only)
- Chart visualisations of patterns
