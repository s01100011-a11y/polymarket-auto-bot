# BK Spread — Threshold Consistency + "Best" Option

**Issue:** NRL#231
**Date:** 2026-07-04
**Status:** Design approved
**Parent:** NRL#230 (multi-dim combos, closed)

## Problem

The Spread Analysis patterns are computed at "best threshold" (from sweep), while the game history table uses the user's price threshold. Clicking a pattern to drill into the game list shows different cover/ROI numbers because different thresholds produce different entry points.

## Solution

- Default: analysis uses the user's current price threshold (consistent click-through)
- "Best" checkbox: opt into sweep-best threshold (useful for exploration)
- Threshold displayed on pattern rows
- Sweep table always shown, selected threshold highlighted

## Backend

### `compute_spread_analysis()` Changes

Add `threshold` param:

```python
def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3, threshold=None):
```

- `threshold=None` → use sweep-best (current behaviour, "Best" mode)
- `threshold=<float>` → use that specific threshold for dimensional/combo analysis

The sweep always runs (for the table). The difference is which threshold is used for the strategy collection section:

```python
    # Determine analysis threshold
    if threshold is not None:
        analysis_thresh = threshold
    else:
        analysis_thresh = best_thresh  # from sweep
```

Then use `analysis_thresh` instead of `best_thresh` for the strategy collection loop.

### Response

Add `analysis_threshold` field to the return dict:

```python
    return {
        "threshold_sweep": sweep,
        "best_threshold": best_thresh,      # always the sweep-best
        "analysis_threshold": analysis_thresh,  # what was actually used (#231)
        "dimensions": dims,
        "combos": combos,
        ...
    }
```

## API

### Endpoint

`GET /api/odds-monitor-analysis?threshold=2.0&combo_min=2&combo_max=3`

| Param | Values | Default | Notes |
|-------|--------|---------|-------|
| `threshold` | float or `"best"` | `best` | Threshold for analysis. `best` = use sweep-best. |
| `combo_min` | 2-5 | 2 | Existing |
| `combo_max` | 2-5 | 3 | Existing |

Server parsing:
```python
threshold_raw = (params.get("threshold") or ["best"])[0].strip()
if threshold_raw == "best":
    threshold = None  # use sweep-best
else:
    try:
        threshold = float(threshold_raw)
    except (ValueError, TypeError):
        threshold = None
```

## Dashboard

### "Best" Checkbox

Add next to the price threshold input in the BK Spread header:

```html
<label><input type="checkbox" id="lev-use-best" onchange="_levReloadAll()"> Best</label>
```

- **Unchecked** (default): analysis uses the price threshold value from the input
- **Checked**: analysis uses sweep-best, price input becomes read-only/greyed showing the best value

### Behaviour

```
Price threshold input changes:
  → if "Best" unchecked: reload game history + analysis at new threshold
  → if "Best" checked: no effect (analysis uses sweep-best)

"Best" checkbox changes:
  → if now checked: reload analysis with threshold=best, grey out price input
  → if now unchecked: reload analysis with threshold=<price input value>
```

### `loadSpreadAnalysis()` Changes

Pass threshold to the analysis endpoint:

```javascript
const useBest = document.getElementById('lev-use-best')?.checked;
const threshold = useBest ? 'best' : (parseFloat(document.getElementById('lev-price')?.value) || 3.0);
fetch(API + '/api/odds-monitor-analysis?threshold=' + threshold + '&combo_min=...');
```

### Sweep Table Highlight

Currently highlights `best_threshold` row with green border. Change to highlight `analysis_threshold`:

```javascript
const isBest = t.threshold === data.analysis_threshold;
```

This way the highlighted row matches what the patterns/combos were computed at.

### Pattern Row Display

Show the threshold used:

```javascript
h += '<span style="color:var(--muted);font-size:0.9em">[' + dimLabel + ' @$' + data.analysis_threshold.toFixed(1) + ']</span> ' + esc(p.combo_label) + ': ...';
```

Format: `[3D @$2.0] away + 0-6 + leading: 5/7 covered, ROI +45.2%`

### localStorage

Persist "use best" setting. Add to `lev_analysis_settings`:

```json
{
  "combo_min": 2,
  "combo_max": 3,
  "combo_min_games": 3,
  "combo_min_roi": 20,
  "use_best": false
}
```

### `_levReloadAll()` Helper

When "Best" checkbox changes, need to reload both analysis AND potentially update the price input:

```javascript
function _levReloadAll() {
  const useBest = document.getElementById('lev-use-best')?.checked;
  const priceEl = document.getElementById('lev-price');
  if (priceEl) priceEl.disabled = useBest;
  // Save setting
  const settings = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  settings.use_best = useBest;
  localStorage.setItem('lev_analysis_settings', JSON.stringify(settings));
  loadSpreadAnalysis();
}
```

### Init Restore

In `_levInitView()` or the BK view init, restore the "Best" checkbox state:

```javascript
const settings = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
const bestCb = document.getElementById('lev-use-best');
if (bestCb) bestCb.checked = !!settings.use_best;
if (settings.use_best) document.getElementById('lev-price').disabled = true;
```

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Add `threshold` param to `compute_spread_analysis()`. Use it (or sweep-best when None) for strategy collection. Add `analysis_threshold` to return dict. |
| `server.py` | Parse `threshold` param on `/api/odds-monitor-analysis`. |
| `dashboard.html` | Add "Best" checkbox. Pass threshold to analysis fetch. Highlight `analysis_threshold` row in sweep. Show threshold in pattern rows. Persist + restore. |

## Out of Scope

- Changing the threshold sweep itself (always runs $1.50-$6.00)
- Syncing threshold between BK game history and Historical Win Rate view
- Making the 5D combo table also respect the threshold (it already uses the same strategies)
