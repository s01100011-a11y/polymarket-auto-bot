# BK Spread Threshold Consistency — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Spread Analysis use the user's price threshold by default (consistent with game history click-through), with a "Best" checkbox to opt into sweep-best.

**Architecture:** Add `threshold` param to `compute_spread_analysis()` — when provided, uses that threshold for strategy collection instead of sweep-best. Server passes the threshold from query params. Dashboard adds "Best" checkbox, passes threshold to fetch, highlights the active threshold in sweep table, and shows threshold in pattern labels.

**Tech Stack:** Python 3 (stdlib only), vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-04-bk-spread-threshold-consistency-design.md`

---

### Task 1: Backend — Add `threshold` Param

**Files:**
- Modify: `odds_monitor.py:930-1193`

- [ ] **Step 1: Add `threshold` param to signature**

Change line 930-931 from:
```python
def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3):
```
to:
```python
def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3, threshold=None):
```

Add to docstring after the `combo_max` arg:
```
        threshold: specific threshold for analysis (float). None = use sweep-best. (#231)
```

- [ ] **Step 2: Determine analysis threshold after sweep**

After the sweep loop (after line 982 where `best_thresh` is finalized), add:

```python
    # Determine analysis threshold (#231)
    analysis_thresh = threshold if threshold is not None else best_thresh
```

- [ ] **Step 3: Use `analysis_thresh` for strategy collection**

Change line 989 from:
```python
            strat = fn(g.get("snapshots", []), g, threshold=best_thresh)
```
to:
```python
            strat = fn(g.get("snapshots", []), g, threshold=analysis_thresh)
```

- [ ] **Step 4: Add `analysis_threshold` to return dict**

In the return dict (lines 1185-1193), add `analysis_threshold`:

```python
    return {
        "threshold_sweep": sweep,
        "best_threshold": best_thresh,
        "analysis_threshold": analysis_thresh,
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "combo_5d": combo_5d,
        "total_games": len(history),
    }
```

- [ ] **Step 5: Test**

```bash
python3 -c "
import odds_monitor as om
history = om.load_history()

# Default (None) — should use sweep-best
r1 = om.compute_spread_analysis(history)
print(f'Default: analysis_thresh={r1[\"analysis_threshold\"]}, best={r1[\"best_threshold\"]}')
assert r1['analysis_threshold'] == r1['best_threshold'], 'Default should use best'

# Explicit threshold
r2 = om.compute_spread_analysis(history, threshold=2.0)
print(f'Threshold=2.0: analysis_thresh={r2[\"analysis_threshold\"]}, best={r2[\"best_threshold\"]}')
assert r2['analysis_threshold'] == 2.0, 'Should use provided threshold'
assert r2['best_threshold'] == r1['best_threshold'], 'Best should still be computed from sweep'

# Results should differ (different entries qualify at different thresholds)
print(f'Default combos: {len(r1[\"combos\"])}, @2.0 combos: {len(r2[\"combos\"])}')
print('PASS')
"
```

- [ ] **Step 6: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: compute_spread_analysis accepts threshold param for consistent analysis (#231)"
```

---

### Task 2: Server — Parse `threshold` Param

**Files:**
- Modify: `server.py:4157-4192`

- [ ] **Step 1: Add threshold parsing**

In the `/api/odds-monitor-analysis` handler, after `combo_max = max(combo_max, combo_min)` (line 4168), add:

```python
                # Threshold for analysis (#231)
                threshold_raw = (params.get("threshold") or ["best"])[0].strip()
                if threshold_raw == "best":
                    threshold = None
                else:
                    try:
                        threshold = float(threshold_raw)
                    except (ValueError, TypeError):
                        threshold = None
```

Then update the `compute_spread_analysis` call (lines 4186-4188) to pass `threshold`:

```python
                result = odds_monitor.compute_spread_analysis(
                    history, game_history=game_hist,
                    combo_min=combo_min, combo_max=combo_max,
                    threshold=threshold)
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: /api/odds-monitor-analysis accepts threshold param (#231)"
```

---

### Task 3: Dashboard — "Best" Checkbox + Threshold in Patterns

**Files:**
- Modify: `dashboard.html`

- [ ] **Step 1: Add "Best" checkbox to BK Spread header**

Find the price threshold input (around line 1342):
```html
        <label style="font-size:0.78em;color:var(--muted)">Price threshold ($):
          <input type="number" id="lev-price" ...>
        </label>
```

After this label (before "Set as default" button), add:

```html
        <label style="font-size:0.78em;color:var(--muted)"><input type="checkbox" id="lev-use-best" onchange="_levReloadAll()"> Best</label>
```

- [ ] **Step 2: Update `loadSpreadAnalysis()` fetch to pass threshold**

Change lines 8606-8609 from:
```javascript
  const _cs = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  const _cmin = _cs.combo_min || 2;
  const _cmax = _cs.combo_max || 3;
  fetch(API + '/api/odds-monitor-analysis?combo_min=' + _cmin + '&combo_max=' + _cmax).then(r => r.json()).then(data => {
```
to:
```javascript
  const _cs = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  const _cmin = _cs.combo_min || 2;
  const _cmax = _cs.combo_max || 3;
  const _useBest = document.getElementById('lev-use-best')?.checked;
  const _thresh = _useBest ? 'best' : (parseFloat(document.getElementById('lev-price')?.value) || 3.0);
  fetch(API + '/api/odds-monitor-analysis?combo_min=' + _cmin + '&combo_max=' + _cmax + '&threshold=' + _thresh).then(r => r.json()).then(data => {
```

- [ ] **Step 3: Update sweep table highlight to use `analysis_threshold`**

Find the sweep table rendering where it checks the best threshold (around line 8644):
```javascript
        const isBest = t.threshold === data.best_threshold;
```
Change to:
```javascript
        const isBest = t.threshold === data.analysis_threshold;
```

- [ ] **Step 4: Add threshold to pattern row labels**

Find the pattern rendering (around lines 8646, 8656). The current pattern label shows:
```javascript
        h += '<span style="color:var(--muted);font-size:0.9em">[' + dimLabel + ']</span> ' + esc(p.combo_label) + ': ...';
```

Change to include threshold:
```javascript
        h += '<span style="color:var(--muted);font-size:0.9em">[' + dimLabel + ' @$' + (data.analysis_threshold || 0).toFixed(1) + ']</span> ' + esc(p.combo_label) + ': ...';
```

Apply this to BOTH the profitable and anti-pattern loops.

- [ ] **Step 5: Add `_levReloadAll()` function**

After `_levReloadAnalysis()`, add:

```javascript
function _levReloadAll() {
  const useBest = document.getElementById('lev-use-best')?.checked;
  const priceEl = document.getElementById('lev-price');
  if (priceEl) priceEl.disabled = !!useBest;
  // Save setting
  const settings = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  settings.use_best = !!useBest;
  try { localStorage.setItem('lev_analysis_settings', JSON.stringify(settings)); } catch(e) {}
  loadSpreadAnalysis();
}
```

- [ ] **Step 6: Restore "Best" state on init**

Find where BK view initializes (in `loadLeadingEv()` or the tab init). After the Spread Analysis is loaded, the "Best" checkbox state needs restoring.

Add at the TOP of `loadSpreadAnalysis()` (after `const el = ...`):

```javascript
  // Restore Best checkbox state (#231)
  const _settings = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  const bestCb = document.getElementById('lev-use-best');
  if (bestCb) {
    bestCb.checked = !!_settings.use_best;
    const priceEl = document.getElementById('lev-price');
    if (priceEl) priceEl.disabled = !!_settings.use_best;
  }
```

- [ ] **Step 7: Commit**

```bash
git add dashboard.html
git commit -m "feat: Best checkbox, threshold in analysis fetch + pattern labels (#231)"
```

---

### Task 4: Testing + CHANGES.md + Issue Close

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart and test**

```bash
systemctl --user restart nrl-dashboard.service
sleep 2

# Test threshold param
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis?threshold=2.0' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'analysis_threshold: {d[\"analysis_threshold\"]}, best: {d[\"best_threshold\"]}')
assert d['analysis_threshold'] == 2.0
assert d['best_threshold'] != 2.0 or True  # best may happen to be 2.0
print('PASS: explicit threshold')
"

# Test best
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis?threshold=best' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'analysis_threshold: {d[\"analysis_threshold\"]}, best: {d[\"best_threshold\"]}')
assert d['analysis_threshold'] == d['best_threshold']
print('PASS: best threshold')
"

echo 'ALL PASS'
```

- [ ] **Step 2: Browser test**

1. Load dashboard → Spread Strategy → BK Spread
2. Set price threshold to $2.00, "Best" unchecked
3. Open Spread Analysis — verify patterns show `@$2.0` in labels
4. Verify sweep table highlights the $2.0 row
5. Click a profitable pattern — game history filter should show CONSISTENT numbers
6. Check "Best" checkbox — analysis reloads, threshold changes to sweep-best, price input disabled
7. Patterns now show `@$1.5` (or whatever best is), sweep highlights best row
8. Uncheck "Best" — price input re-enabled, analysis reloads at user's threshold
9. Refresh page — "Best" state persists

- [ ] **Step 3: Update CHANGES.md**

```markdown
## 2026-07-04 — BK Spread: threshold consistency + "Best" option (#231)

- **`odds_monitor.py`**: `compute_spread_analysis()` accepts `threshold` param — when provided, uses it for strategy collection instead of sweep-best. Returns `analysis_threshold` in response.
- **`server.py`**: `/api/odds-monitor-analysis` accepts `threshold` query param (float or "best").
- **`dashboard.html`**: Added "Best" checkbox next to price threshold. When unchecked (default), analysis uses user's price threshold for consistent click-through to game history. When checked, uses sweep-best. Sweep table highlights active threshold. Pattern labels show `@$X.X`. Price input disabled when "Best" active. Setting persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for threshold consistency (#231)"
git push
```

- [ ] **Step 5: Close issue**

```bash
gh issue comment 231 --body "Implementation complete. Analysis now uses user's price threshold by default. 'Best' checkbox opts into sweep-best. Pattern labels show threshold. Click-through is consistent."
gh issue close 231
```
