# BK Spread — Multi-Dimension Combo Patterns (2D-5D)

**Issue:** NRL#230
**Date:** 2026-07-04
**Status:** Design approved
**Parent:** NRL#229 (7D patterns, closed)

## Overview

Expand BK Spread pattern detection from fixed 2D pairs to variable-dimension combos (2-5D) with a configurable min/max range. Add Min Games and Min ROI client-side filters. Replace "Top 2D Combos" with a merged "Top Combos" table showing all dimension levels.

## Backend

### New Parameters

`compute_spread_analysis()` signature adds:

```python
def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3):
```

- `combo_min`: minimum combo dimension count (2-5, default 2)
- `combo_max`: maximum combo dimension count (2-5, default 3, must be ≥ combo_min)

### Combo Generation

Replace the fixed 21-pair combo section with variable-dimension generation:

```python
from itertools import combinations

COMBO_DIMS_7 = ["side", "role", "margin", "entry_minute",
                "margin_context", "spread_price", "ev_edge"]

combos = []
for k in range(combo_min, combo_max + 1):
    for dim_combo in combinations(COMBO_DIMS_7, k):
        buckets = defaultdict(lambda: {"games": 0, "covered": 0, "pnl": 0.0})
        for s in strategies:
            b = _bucket(s)
            vals = tuple(b.get(d) for d in dim_combo)
            if None in vals:
                continue
            buckets[vals]["games"] += 1
            if s["strat"].get("covered"):
                buckets[vals]["covered"] += 1
            if s["strat"].get("pnl") is not None:
                buckets[vals]["pnl"] += s["strat"]["pnl"]
        for vals, val in buckets.items():
            if val["games"] < 2:
                continue
            val["pnl"] = round(val["pnl"], 2)
            val["roi"] = round(val["pnl"] / val["games"] * 100, 1) if val["games"] > 0 else None
            val["low_confidence"] = val["games"] < 5
            combos.append({
                "dims": k,
                "dimensions": list(dim_combo),
                "values": list(vals),
                "combo_label": " + ".join(str(v) for v in vals),
                "games": val["games"],
                "covered": val["covered"],
                "pnl": val["pnl"],
                "roi": val["roi"],
                "low_confidence": val["low_confidence"],
            })
combos.sort(key=lambda c: abs(c.get("roi") or 0), reverse=True)
```

### Pattern Detection

Same logic, using the unified `combos` list:

```python
profitable = [c for c in combos if (c.get("roi") or 0) > 20 and c["games"] >= 3]
anti = [c for c in combos if (c.get("roi") or 0) < -30 and c["games"] >= 3]
```

Thresholds unchanged (ROI > 20% / < -30%, ≥3 games). Client-side Min Games/Min ROI filters provide user-controllable narrowing.

### Combo Entry Shape (new)

Each combo entry:

```json
{
  "dims": 3,
  "dimensions": ["side", "margin", "margin_context"],
  "values": ["home", "0-6", "leading"],
  "combo_label": "home + 0-6 + leading",
  "games": 5,
  "covered": 4,
  "pnl": 3.50,
  "roi": 70.0,
  "low_confidence": false
}
```

This replaces the old `dimension1/value1/dimension2/value2` format. The `combo_label` field is pre-joined for display. `dimensions` array allows the dashboard to identify which dims are represented.

### Response Shape

```python
return {
    "threshold_sweep": sweep,
    "best_threshold": best_thresh,
    "dimensions": dims,
    "combos": combos,           # now variable-dimension
    "profitable_patterns": profitable,
    "anti_patterns": anti,
    "combo_5d": combo_5d,       # unchanged
    "total_games": len(history),
}
```

### Backwards Compatibility

The old `dimension1/value1/dimension2/value2` format is removed. Dashboard JS must use the new `dimensions`/`values`/`combo_label` fields. The `_levSetFilters()` click-through from patterns uses `dimensions[i]` and `values[i]` instead of fixed `dimension1`/`dimension2`.

## API

### Endpoint

`GET /api/odds-monitor-analysis?combo_min=2&combo_max=3`

| Param | Values | Default | Notes |
|-------|--------|---------|-------|
| `combo_min` | 2-5 | 2 | Min dimensions per combo |
| `combo_max` | 2-5 | 3 | Max dimensions per combo (≥ combo_min) |

Server validates: both clamped to 2-5, `combo_max = max(combo_max, combo_min)`.

## Dashboard

### Layout

```
📊 Spread Analysis
├── Controls row:
│   Combo dims: [2 ▾] to [3 ▾]  Min Games: [3 ▾]  Min ROI: [20 ▾]%
├── Profitable Patterns (green card, from all levels)
├── Anti-Patterns (red card, from all levels)
├── Threshold Sweep (unchanged)
├── Dimensional Breakdowns (unchanged)
├── Top Combos (merged table, replaces "Top 2D Combos")
│   Headers: Dims | Combo | Games | Covered | P&L | ROI
│   Top 30 by abs(ROI), sortable
└── 📊 5D Combo Table (unchanged)
```

### Controls Row

Placed at the top of the Spread Analysis section (inside the `<details>`):

```html
<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:0.78em">
  <label style="color:var(--muted)">Combo dims:
    <select id="lev-combo-min" onchange="_levReloadAnalysis()">
      <option value="2" selected>2</option>
      <option value="3">3</option>
      <option value="4">4</option>
      <option value="5">5</option>
    </select>
    to
    <select id="lev-combo-max" onchange="_levReloadAnalysis()">
      <option value="2">2</option>
      <option value="3" selected>3</option>
      <option value="4">4</option>
      <option value="5">5</option>
    </select>
  </label>
  <label style="color:var(--muted)">Min Games:
    <input type="number" id="lev-combo-mingames" value="3" min="1" max="50"
           onchange="_levReloadAnalysis()">
  </label>
  <label style="color:var(--muted)">Min ROI:
    <input type="number" id="lev-combo-minroi" value="20" min="0" max="200" step="5"
           onchange="_levReloadAnalysis()">%
  </label>
</div>
```

`_levReloadAnalysis()` re-fetches `/api/odds-monitor-analysis` with current combo_min/combo_max params and re-renders with client-side Min Games/Min ROI filtering.

### Pattern Rendering

Patterns use `combo_label` for display. Click-through sets filters for as many dimensions as the pattern has (loops `dimensions[]` and `values[]`):

```javascript
for (const p of profs) {
  if (p.games < minGames || Math.abs(p.roi) < minRoi) continue;
  h += '<div onclick="..." >' + esc(p.combo_label) + ': ' + p.covered + '/' + p.games + ' covered, ROI ' + fmtRoi(p.roi) + '</div>';
}
```

### Top Combos Table

Replaces "Top 2D Combos":

```html
<table>
  <thead>
    <tr><th>Dims</th><th>Combo</th><th>Games</th><th>Covered</th><th>P&L</th><th>ROI</th></tr>
  </thead>
  <tbody>
    <!-- top 30 by abs(ROI), filtered by minGames + minRoi -->
  </tbody>
</table>
```

- `Dims` column: "2D", "3D", "4D", "5D"
- `Combo` column: `combo_label` value
- Sortable by any column
- Client-side Min Games/Min ROI applied before rendering

### localStorage

Add to `lev_filters` or a new key `lev_analysis_settings`:

```json
{
  "combo_min": 2,
  "combo_max": 3,
  "combo_min_games": 3,
  "combo_min_roi": 20
}
```

Restored on tab init.

### Fetch Flow

```
Controls change (combo dims, min games, or min roi):
  → save to localStorage
  → fetch /api/odds-monitor-analysis?combo_min=X&combo_max=Y
  → render patterns (filtered by minGames + minRoi client-side)
  → render Top Combos table (filtered, top 30)
  → render rest unchanged (threshold sweep, dims, 5D table)
```

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Add `combo_min`/`combo_max` params. Replace fixed 2D combo loop with `itertools.combinations` variable-dimension loop. New combo entry format (`dims`, `dimensions`, `values`, `combo_label`). Pattern detection unchanged (uses same list). |
| `server.py` | Parse `combo_min`/`combo_max` from query params on `/api/odds-monitor-analysis`, pass to function. |
| `dashboard.html` | Add controls row in Spread Analysis. `_levReloadAnalysis()` function. Update pattern + combo rendering for new entry format. Replace "Top 2D Combos" with "Top Combos". Add Min Games/Min ROI filtering. Update pattern click-through for variable dimensions. localStorage persistence. |

## Out of Scope

- Applying multi-dim combos to Historical Win Rate view (BK only for now)
- Statistical significance testing for high-D combos
- Pre-filtering combos server-side by min games/roi (kept client-side for instant adjustment)
