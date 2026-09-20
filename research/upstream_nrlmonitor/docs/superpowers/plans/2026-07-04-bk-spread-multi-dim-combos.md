# BK Spread Multi-Dimension Combos — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expand BK Spread pattern detection from fixed 2D pairs to variable-dimension combos (2-5D) with configurable min/max range, client-side Min Games/Min ROI filters, and a merged "Top Combos" table.

**Architecture:** Replace the fixed `combo_pairs` loop in `compute_spread_analysis()` with `itertools.combinations` over 7 dimensions at each level from `combo_min` to `combo_max`. New combo entry format with `dims`/`dimensions`/`values`/`combo_label`. Server passes `combo_min`/`combo_max` params. Dashboard adds controls row, rewrites pattern/combo rendering for variable-dimension format.

**Tech Stack:** Python 3 (stdlib: itertools), vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-04-bk-spread-multi-dim-combos-design.md`

---

### Task 1: Backend — Variable-Dimension Combo Generation

**Files:**
- Modify: `odds_monitor.py:930-1160` (`compute_spread_analysis()`)

- [ ] **Step 1: Add `combo_min`/`combo_max` params and import**

Change the function signature (line 930) from:
```python
def compute_spread_analysis(history, thresholds=None, game_history=None):
```
to:
```python
def compute_spread_analysis(history, thresholds=None, game_history=None,
                             combo_min=2, combo_max=3):
```

Add `from itertools import combinations` at the top of the function (after the `from collections import defaultdict` line):
```python
    from itertools import combinations
```

Update docstring to document new params:
```python
    """Compute spread analysis across game history with threshold sweep and dimensional breakdowns.

    Runs BOTH leading and trailing strategies per game (merged, #229).

    Args:
        history: list of game records from load_history()
        thresholds: list of threshold values to sweep (default 1.5-6.0)
        game_history: optional list of game_history.json records for enrichment (#226)
        combo_min: minimum combo dimension count, 2-5 (default 2) (#230)
        combo_max: maximum combo dimension count, 2-5 (default 3) (#230)

    Returns dict with threshold_sweep, best_threshold, dimensions, combos,
    profitable_patterns, anti_patterns, combo_5d, total_games.
    """
```

- [ ] **Step 2: Replace the combo section with variable-dimension generation**

Find the combo section (lines 1112-1155). Replace the entire block from `# 3. 2D combos` through `combos.sort(...)` with:

```python
    # 3. Variable-dimension combos from 7 core dimensions (#230)
    COMBO_DIMS_7 = ["side", "role", "margin", "entry_minute",
                    "margin_context", "spread_price", "ev_edge"]
    combo_min = max(2, min(5, combo_min))
    combo_max = max(combo_min, min(5, combo_max))

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

- [ ] **Step 3: Pattern detection unchanged**

The existing pattern detection lines stay the same:
```python
    profitable = [c for c in combos if (c.get("roi") or 0) > 20 and c["games"] >= 3]
    anti = [c for c in combos if (c.get("roi") or 0) < -30 and c["games"] >= 3]
```

These naturally work with the new combo format since they only check `roi` and `games`.

- [ ] **Step 4: Test**

```bash
python3 -c "
import odds_monitor as om
history = om.load_history()

# Default 2-3D
r = om.compute_spread_analysis(history, combo_min=2, combo_max=3)
print(f'2-3D: {len(r[\"combos\"])} combos')
for c in r['combos'][:3]:
    print(f'  {c[\"dims\"]}D: {c[\"combo_label\"]} ({c[\"games\"]}g, ROI {c[\"roi\"]}%)')
assert all(c['dims'] in (2, 3) for c in r['combos']), 'Should only have 2D and 3D'
assert all('dimensions' in c and 'values' in c and 'combo_label' in c for c in r['combos'])

# 2-5D
r2 = om.compute_spread_analysis(history, combo_min=2, combo_max=5)
print(f'2-5D: {len(r2[\"combos\"])} combos')
dims_seen = set(c['dims'] for c in r2['combos'])
print(f'Dimension levels seen: {sorted(dims_seen)}')
assert len(r2['combos']) >= len(r['combos']), 'More dims = more combos'

# Only 4D
r3 = om.compute_spread_analysis(history, combo_min=4, combo_max=4)
print(f'4D only: {len(r3[\"combos\"])} combos')
assert all(c['dims'] == 4 for c in r3['combos']), 'Should only have 4D'

# Patterns still work
print(f'Profitable: {len(r[\"profitable_patterns\"])}, Anti: {len(r[\"anti_patterns\"])}')
assert all('combo_label' in p for p in r['profitable_patterns'])

print('PASS')
"
```

- [ ] **Step 5: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: variable-dimension combos (2-5D) in compute_spread_analysis (#230)"
```

---

### Task 2: Server — Parse combo_min/combo_max Params

**Files:**
- Modify: `server.py:4157-4180` (`/api/odds-monitor-analysis` handler)

- [ ] **Step 1: Add param parsing**

In the `/api/odds-monitor-analysis` handler, add param parsing before the `compute_spread_analysis()` call. Change lines 4157-4180:

Find:
```python
        if path == "/api/odds-monitor-analysis":
            try:
                history = odds_monitor.load_history()
```

Add after the `try:` line:
```python
                params = parse_qs(parsed.query)
                try:
                    combo_min = max(2, min(5, int((params.get("combo_min") or ["2"])[0])))
                except (ValueError, TypeError):
                    combo_min = 2
                try:
                    combo_max = max(2, min(5, int((params.get("combo_max") or ["3"])[0])))
                except (ValueError, TypeError):
                    combo_max = 3
                combo_max = max(combo_max, combo_min)
```

Then update the `compute_spread_analysis` call (line 4176) to pass the new params:
```python
                result = odds_monitor.compute_spread_analysis(
                    history, game_history=game_hist,
                    combo_min=combo_min, combo_max=combo_max)
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: /api/odds-monitor-analysis accepts combo_min/combo_max params (#230)"
```

---

### Task 3: Dashboard — Controls Row + Rewrite Pattern/Combo Rendering

**Files:**
- Modify: `dashboard.html` (JS section around lines 8601-8723)

- [ ] **Step 1: Add controls row at top of Spread Analysis render**

In `loadSpreadAnalysis()`, after `let h = '';` (line 8612), add the controls row:

```javascript
    // Combo controls (#230)
    const _comboSettings = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
    const comboMin = parseInt(_comboSettings.combo_min || '2');
    const comboMax = parseInt(_comboSettings.combo_max || '3');
    const minGames = parseInt(_comboSettings.combo_min_games || '3');
    const minRoi = parseInt(_comboSettings.combo_min_roi || '20');

    h += '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:0.78em">';
    h += '<label style="color:var(--muted)">Combo dims: <select id="lev-combo-min" onchange="_levReloadAnalysis()">';
    for (let i = 2; i <= 5; i++) h += '<option value="' + i + '"' + (i === comboMin ? ' selected' : '') + '>' + i + '</option>';
    h += '</select> to <select id="lev-combo-max" onchange="_levReloadAnalysis()">';
    for (let i = 2; i <= 5; i++) h += '<option value="' + i + '"' + (i === comboMax ? ' selected' : '') + '>' + i + '</option>';
    h += '</select></label>';
    h += '<label style="color:var(--muted)">Min Games: <input type="number" id="lev-combo-mingames" value="' + minGames + '" min="1" max="50" step="1" onchange="_levReloadAnalysis()" style="width:45px;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:1px 3px;border-radius:3px;font-size:1em"></label>';
    h += '<label style="color:var(--muted)">Min ROI: <input type="number" id="lev-combo-minroi" value="' + minRoi + '" min="0" max="200" step="5" onchange="_levReloadAnalysis()" style="width:50px;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:1px 3px;border-radius:3px;font-size:1em">%</label>';
    h += '</div>';
```

- [ ] **Step 2: Rewrite pattern rendering for new combo format**

Replace the profitable patterns section (lines 8618-8636). Change from using `p.dimension1`/`p.value1`/`p.dimension2`/`p.value2` to using `p.combo_label`:

```javascript
    // Notable Patterns (#230 — variable-dimension)
    const profs = (data.profitable_patterns || []).filter(p => p.games >= minGames && Math.abs(p.roi || 0) >= minRoi);
    const antis = (data.anti_patterns || []).filter(p => p.games >= minGames && Math.abs(p.roi || 0) >= minRoi);
    if (profs.length) {
      h += '<div style="border:1px solid var(--green);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:var(--bg)">';
      h += '<div style="font-size:0.78em;font-weight:600;color:var(--green);margin-bottom:4px">Profitable Patterns</div>';
      for (const p of profs.slice(0, 20)) {
        const dimLabel = p.dims + 'D';
        h += '<div style="font-size:0.76em;margin:2px 0;cursor:pointer;text-decoration:underline dotted;color:var(--green)" onclick="_levSetMultiFilters(' + JSON.stringify(p.dimensions) + ',' + JSON.stringify(p.values) + ')">';
        h += '<span style="color:var(--muted);font-size:0.9em">[' + dimLabel + ']</span> ' + esc(p.combo_label) + ': <span style="color:var(--green)">' + p.covered + '/' + p.games + ' covered, ROI ' + fmtRoi(p.roi) + '</span>' + lcIcon(p.low_confidence) + '</div>';
      }
      h += '</div>';
    }
    if (antis.length) {
      h += '<div style="border:1px solid var(--red);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:var(--bg)">';
      h += '<div style="font-size:0.78em;font-weight:600;color:var(--red);margin-bottom:4px">Anti-Patterns</div>';
      for (const p of antis.slice(0, 20)) {
        const dimLabel = p.dims + 'D';
        h += '<div style="font-size:0.76em;margin:2px 0;cursor:pointer;text-decoration:underline dotted;color:var(--red)" onclick="_levSetMultiFilters(' + JSON.stringify(p.dimensions) + ',' + JSON.stringify(p.values) + ')">';
        h += '<span style="color:var(--muted);font-size:0.9em">[' + dimLabel + ']</span> ' + esc(p.combo_label) + ': <span style="color:var(--red)">' + p.covered + '/' + p.games + ' covered, ROI ' + fmtRoi(p.roi) + '</span>' + lcIcon(p.low_confidence) + '</div>';
      }
      h += '</div>';
    }
```

- [ ] **Step 3: Replace "Top 2D Combos" with "Top Combos" table**

Replace lines 8684-8700 (the 2D combos section) with:

```javascript
    // Top Combos — merged multi-dimension (#230)
    const combos = (data.combos || []).filter(c => c.games >= minGames && Math.abs(c.roi || 0) >= minRoi);
    if (combos.length) {
      const top30 = combos.slice(0, 30);
      h += '<div style="margin-top:10px"><div style="font-size:0.78em;font-weight:600;color:var(--nrl-green);margin-bottom:4px">Top Combos (by ROI)</div>';
      h += '<div style="overflow-x:auto"><table class="analysis-table"><thead><tr><th>Dims</th><th>Combo</th><th>Games</th><th>Covered</th><th>P&L</th><th>ROI</th></tr></thead><tbody>';
      for (const c of top30) {
        h += '<tr>';
        h += '<td style="text-align:center;font-size:0.76em;color:var(--muted)">' + c.dims + 'D</td>';
        h += '<td style="font-size:0.76em;white-space:nowrap">' + esc(c.combo_label) + lcIcon(c.low_confidence) + '</td>';
        h += '<td style="text-align:center">' + c.games + '</td>';
        h += '<td style="text-align:center">' + c.covered + '</td>';
        h += '<td style="text-align:center;color:' + roiColor(c.pnl) + '">' + fmtPnl(c.pnl) + '</td>';
        h += '<td style="text-align:center;font-weight:600;color:' + roiColor(c.roi) + '">' + fmtRoi(c.roi) + '</td>';
        h += '</tr>';
      }
      h += '</tbody></table></div></div>';
    }
```

- [ ] **Step 4: Update fetch URL to pass combo params**

Change line 8606:
```javascript
  fetch(API + '/api/odds-monitor-analysis').then(r => r.json()).then(data => {
```
to:
```javascript
  const _cs = JSON.parse(localStorage.getItem('lev_analysis_settings') || '{}');
  const _cmin = _cs.combo_min || 2;
  const _cmax = _cs.combo_max || 3;
  fetch(API + '/api/odds-monitor-analysis?combo_min=' + _cmin + '&combo_max=' + _cmax).then(r => r.json()).then(data => {
```

- [ ] **Step 5: Add `_levReloadAnalysis()` and `_levSetMultiFilters()` functions**

After `loadSpreadAnalysis()` (before the 5D sort helpers), add:

```javascript
function _levReloadAnalysis() {
  // Save settings to localStorage (#230)
  const settings = {
    combo_min: parseInt(document.getElementById('lev-combo-min')?.value || '2'),
    combo_max: parseInt(document.getElementById('lev-combo-max')?.value || '3'),
    combo_min_games: parseInt(document.getElementById('lev-combo-mingames')?.value || '3'),
    combo_min_roi: parseInt(document.getElementById('lev-combo-minroi')?.value || '20'),
  };
  // Ensure max >= min
  if (settings.combo_max < settings.combo_min) settings.combo_max = settings.combo_min;
  try { localStorage.setItem('lev_analysis_settings', JSON.stringify(settings)); } catch(e) {}
  loadSpreadAnalysis();
}

function _levSetMultiFilters(dimensions, values) {
  // Click-through from multi-dim patterns — set as many filters as possible (#230)
  const dimMap = {
    side: 'lev-f-side', role: 'lev-f-role', margin: 'lev-f-margin',
    entry_minute: 'lev-f-entry', ev_edge: 'lev-f-ev', margin_context: 'lev-f-context'
  };
  for (let i = 0; i < dimensions.length; i++) {
    const id = dimMap[dimensions[i]];
    if (id) {
      const el = document.getElementById(id);
      if (el) el.value = values[i];
    }
  }
  _levApplyFilters();
  // Scroll to history table
  const hist = document.getElementById('lev-history');
  if (hist) hist.scrollIntoView({behavior: 'smooth', block: 'start'});
}
```

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: multi-dim combo controls, pattern rendering, Top Combos table (#230)"
```

---

### Task 4: Testing + CHANGES.md + Issue Close

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart and test endpoints**

```bash
systemctl --user restart nrl-dashboard.service
sleep 2

# Default 2-3D
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis' | python3 -c "
import json,sys; d=json.load(sys.stdin)
combos = d['combos']
dims_seen = set(c['dims'] for c in combos)
print(f'Default: {len(combos)} combos, dims: {sorted(dims_seen)}')
assert all(c['dims'] in (2,3) for c in combos)
assert all('combo_label' in c and 'dimensions' in c for c in combos)
print('PASS')
"

# 2-5D
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis?combo_min=2&combo_max=5' | python3 -c "
import json,sys; d=json.load(sys.stdin)
combos = d['combos']
dims_seen = set(c['dims'] for c in combos)
print(f'2-5D: {len(combos)} combos, dims: {sorted(dims_seen)}')
assert max(dims_seen) <= 5
print('PASS')
"

echo 'ALL PASS'
```

- [ ] **Step 2: Browser test**

1. Load dashboard → Spread Strategy → BK Spread → open Spread Analysis
2. Verify controls row: "Combo dims: [2] to [3]", "Min Games: [3]", "Min ROI: [20]%"
3. Verify patterns show "[2D]" and "[3D]" labels
4. Change combo max to 5 — should re-fetch, patterns now include 4D and 5D combos
5. Change Min Games to 5 — fewer patterns/combos visible
6. Change Min ROI to 50 — only high-ROI combos shown
7. Verify "Top Combos" table has Dims column (2D/3D/4D/5D)
8. Click a pattern — should set multiple filter dropdowns and scroll to history
9. Refresh page — settings persist from localStorage

- [ ] **Step 3: Update CHANGES.md**

```markdown
## 2026-07-04 — BK Spread: multi-dimension combos 2D-5D (#230)

- **`odds_monitor.py`**: `compute_spread_analysis()` now accepts `combo_min`/`combo_max` params (default 2-3). Uses `itertools.combinations` to generate combos at each dimension level from the 7 core dimensions. New combo entry format: `dims`, `dimensions[]`, `values[]`, `combo_label`. Pattern detection works across all levels.
- **`server.py`**: `/api/odds-monitor-analysis` accepts `combo_min`/`combo_max` query params (2-5, validated).
- **`dashboard.html`**: Controls row in Spread Analysis: combo dims min/max dropdowns + Min Games/Min ROI inputs. Patterns show dimension level label ([2D], [3D], etc.). "Top 2D Combos" replaced with "Top Combos" (merged multi-level, top 30). Multi-dim pattern click-through sets as many filters as possible. Settings persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for multi-dimension combos (#230)"
git push
```

- [ ] **Step 5: Close issue**

```bash
gh issue comment 230 --body "Implementation complete. Combos now configurable 2-5D with Min Games/Min ROI filters. Top Combos merged table shows all dimension levels."
gh issue close 230
```
