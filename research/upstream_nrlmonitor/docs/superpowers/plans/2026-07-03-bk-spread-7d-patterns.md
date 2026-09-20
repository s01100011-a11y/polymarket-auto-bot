# BK Spread 7D Patterns — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge leading + trailing strategies (remove role dropdown), expand BK pattern detection to 7 dimensions with all 21 2D pairs, and add a 5D combo table to the BK Spread view.

**Architecture:** Modify `compute_spread_analysis()` to run both strategy functions per game, adding `margin_context` to each strategy. Expand combo pairs to 21 from 7 dimensions. Add 5D combo computation. Update server endpoint to always compute both strategies. Dashboard removes role dropdown, renders both strategies per game with Context filter, adds 5D combo table to Spread Analysis.

**Tech Stack:** Python 3 (stdlib only), vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-03-bk-spread-7d-patterns-design.md`

---

### Task 1: Backend — Merge Strategies + 7D Combos + 5D Table

**Files:**
- Modify: `odds_monitor.py:930-1145` (`compute_spread_analysis()`)

This is the largest task — rewriting the core analysis function.

- [ ] **Step 1: Remove `role` param, run both strategies in threshold sweep**

Change the function signature (line 930) from:
```python
def compute_spread_analysis(history, role="leading", thresholds=None, game_history=None):
```
to:
```python
def compute_spread_analysis(history, thresholds=None, game_history=None):
```

Update docstring to remove `role` param, add note about merged strategies.

Replace the threshold sweep section (lines 947-978). Remove `compute_fn = ...` line. Replace the sweep loop:

```python
    # 1. Threshold sweep — run BOTH strategies per game (#229)
    sweep = []
    best_thresh = thresholds[0]
    best_roi = -999
    for thresh in thresholds:
        triggered = 0
        covered_count = 0
        total_pnl = 0.0
        for g in history:
            for fn in [_compute_leading_strategy, _compute_trailing_strategy]:
                strat = fn(g.get("snapshots", []), g, threshold=thresh)
                if strat.get("triggered"):
                    triggered += 1
                    if strat.get("covered"):
                        covered_count += 1
                    if strat.get("pnl") is not None:
                        total_pnl += strat["pnl"]
        roi = round(total_pnl / triggered * 100, 1) if triggered > 0 else None
        sweep.append({"threshold": thresh, "triggered": triggered, "covered": covered_count,
                       "pnl": round(total_pnl, 2), "roi": roi})
        if triggered >= 3 and roi is not None and roi > best_roi:
            best_roi = roi
            best_thresh = thresh
```

- [ ] **Step 2: Run both strategies for dimensional analysis**

Replace the strategy collection section (lines 980-996):

```python
    # 2. Compute strategies at best threshold — BOTH per game (#229)
    strategies = []
    for g in history:
        for ctx, fn in [("leading", _compute_leading_strategy),
                        ("trailing", _compute_trailing_strategy)]:
            strat = fn(g.get("snapshots", []), g, threshold=best_thresh)
            if strat.get("triggered"):
                strat["margin_context"] = ctx
                # Find entry snapshot for ML odds (#223)
                entry_odds = {}
                entry_min = strat.get("entry_minute", 0)
                for snap in g.get("snapshots", []):
                    if snap.get("game_minute") == entry_min:
                        entry_odds = snap.get("odds", {})
                        break
                strategies.append({
                    "strat": strat,
                    "game": g,
                    "entry_odds": entry_odds,
                })
```

- [ ] **Step 3: Add `margin_context` to `_bucket()`**

In the `_bucket()` function, add `margin_context` to the `result` dict. After line 1028 (`"competition": game.get("competition", ""),`), add:

```python
            "margin_context": strat.get("margin_context", "unknown"),
```

- [ ] **Step 4: Add `margin_context` to dim_names**

Change the dim_names list (line 1084) to include `margin_context`:

```python
    dim_names = ["side", "role", "margin", "entry_minute", "total_points", "ev_edge",
                 "spread_price", "team", "competition", "margin_context",
                 "halftime_context", "margin_trajectory", "key_conditions",
                 "postgame_tags", "spread_line_size"]
```

- [ ] **Step 5: Replace combo_pairs with 21 pairs from 7 dimensions**

Replace lines 1110-1117:

```python
    # 3. 2D combos — all 21 pairs from 7 core dimensions (#229)
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

- [ ] **Step 6: Add 5D combo computation**

After the pattern detection section (after `anti = [...]`), add:

```python
    # 5D combo table (#229)
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

- [ ] **Step 7: Update return dict**

Add `combo_5d` to the return dict and remove `role`:

```python
    return {
        "threshold_sweep": sweep,
        "best_threshold": best_thresh,
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "combo_5d": combo_5d,
        "total_games": len(history),
    }
```

- [ ] **Step 8: Test**

```bash
python3 -c "
import odds_monitor as om
history = om.load_history()
result = om.compute_spread_analysis(history)
print(f'Total games: {result[\"total_games\"]}')
dims = list(result['dimensions'].keys())
print(f'Dimensions: {dims}')
assert 'margin_context' in dims, 'Should have margin_context dimension'
print(f'Combos: {len(result[\"combos\"])}, Profitable: {len(result[\"profitable_patterns\"])}, Anti: {len(result[\"anti_patterns\"])}')
print(f'5D combos: {len(result[\"combo_5d\"])}')
assert 'combo_5d' in result
# Verify margin_context appears in dimensional breakdown
mc = result['dimensions'].get('margin_context', {})
print(f'Margin context: {mc}')
print('PASS')
"
```

- [ ] **Step 9: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: compute_spread_analysis — merge leading+trailing, 7D combos, 5D table (#229)"
```

---

### Task 2: Server — Remove Role from Endpoints

**Files:**
- Modify: `server.py:4097-4156` (`/api/odds-monitor-leading`)
- Modify: `server.py:4158-4185` (`/api/odds-monitor-analysis`)

- [ ] **Step 1: Update `/api/odds-monitor-leading` to run both strategies**

Replace the handler (lines 4097-4156). The key changes:
- Remove `role` param parsing
- Run BOTH `_compute_leading_strategy` and `_compute_trailing_strategy` per game
- Backfill `trailing_team_ev` for all games (not just when role=trailing)
- Aggregate stats from both strategies combined

```python
        if path == "/api/odds-monitor-leading":
            try:
                params = parse_qs(parsed.query)
                # #220: accept threshold param
                _thresh_raw = (params.get("threshold") or [""])[0].strip()
                _thresh = None
                if _thresh_raw:
                    try:
                        _thresh = float(_thresh_raw)
                    except (ValueError, TypeError):
                        pass
                history = odds_monitor.load_history()
                probs_leading = odds_monitor.get_leading_team_tries_probs()
                probs_trailing = odds_monitor.get_late_tries_probs()
                games = []
                agg = {"bets": 0, "wins": 0, "pnl": 0.0, "total_games": len(history)}
                cfg = _read_json(CONFIG_FILE) or {}
                for g in history:
                    # Backfill trailing_team_ev for legacy snapshots (#213)
                    for s in g.get("snapshots", []):
                        if "trailing_team_ev" not in s:
                            nrl_data = {
                                "home_score": s.get("home_score", 0),
                                "away_score": s.get("away_score", 0),
                                "home_team": s.get("home_team", g.get("home_team", "")),
                                "away_team": s.get("away_team", g.get("away_team", "")),
                                "game_minute": s.get("game_minute", 0),
                            }
                            odds = s.get("odds", {})
                            s["trailing_team_ev"] = odds_monitor._compute_trailing_team_ev(
                                odds, nrl_data, nrl_data["game_minute"], cfg)
                    # Compute BOTH strategies (#229)
                    lead_strat = odds_monitor._compute_leading_strategy(
                        g.get("snapshots", []), g, threshold=_thresh)
                    trail_strat = odds_monitor._compute_trailing_strategy(
                        g.get("snapshots", []), g, threshold=_thresh)
                    lead_strat["margin_context"] = "leading"
                    trail_strat["margin_context"] = "trailing"
                    g["leading_strategy"] = lead_strat
                    g["trailing_strategy"] = trail_strat
                    games.append(g)
                    for strat in [lead_strat, trail_strat]:
                        if strat.get("triggered"):
                            agg["bets"] += 1
                            if strat.get("covered"):
                                agg["wins"] += 1
                            if strat.get("pnl") is not None:
                                agg["pnl"] += strat["pnl"]
                agg["pnl"] = round(agg["pnl"], 2)
                agg["roi"] = round(agg["pnl"] / agg["bets"] * 100, 1) if agg["bets"] else None
                self.send_json(200, {
                    "games": games,
                    "aggregate": agg,
                    "leading_probs": probs_leading,
                    "trailing_probs": probs_trailing,
                })
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return
```

- [ ] **Step 2: Update `/api/odds-monitor-analysis` to remove role param**

Find the handler (around line 4158-4185). Change the call to `compute_spread_analysis`:

Replace:
```python
                game_hist = _load_history()
                result = odds_monitor.compute_spread_analysis(history, role=role, game_history=game_hist)
```

With:
```python
                game_hist = _load_history()
                result = odds_monitor.compute_spread_analysis(history, game_history=game_hist)
```

Also remove the `role` param parsing and the trailing backfill logic from this handler (it's now done in the `/api/odds-monitor-leading` handler). Simplify to:

```python
        if path == "/api/odds-monitor-analysis":
            try:
                history = odds_monitor.load_history()
                # Backfill trailing_team_ev for legacy snapshots
                cfg = _read_json(CONFIG_FILE) or {}
                for g in history:
                    for s in g.get("snapshots", []):
                        if "trailing_team_ev" not in s:
                            nrl_data = {
                                "home_score": s.get("home_score", 0),
                                "away_score": s.get("away_score", 0),
                                "home_team": s.get("home_team", g.get("home_team", "")),
                                "away_team": s.get("away_team", g.get("away_team", "")),
                                "game_minute": s.get("game_minute", 0),
                            }
                            odds = s.get("odds", {})
                            s["trailing_team_ev"] = odds_monitor._compute_trailing_team_ev(
                                odds, nrl_data, nrl_data["game_minute"], cfg)
                game_hist = _load_history()
                result = odds_monitor.compute_spread_analysis(history, game_history=game_hist)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return
```

- [ ] **Step 3: Commit**

```bash
git add server.py
git commit -m "feat: merge leading+trailing in /api/odds-monitor-leading, remove role param (#229)"
```

---

### Task 3: Dashboard — Remove Role Dropdown + Render Both Strategies

**Files:**
- Modify: `dashboard.html` (HTML + JS)

- [ ] **Step 1: Remove role dropdown from HTML**

Find the role dropdown HTML (lines 1335-1340):
```html
        <label style="font-size:0.78em;color:var(--muted)">Role:
          <select id="lev-role" onchange="_levOnRoleChange()" style="...">
            <option value="leading">Leading</option>
            <option value="trailing">Trailing</option>
          </select>
        </label>
```

Remove this entire label+select block.

- [ ] **Step 2: Update `loadLeadingEv()` to render both strategies**

Replace the function. Key changes:
- Remove `role` from fetch URL — just send `threshold`
- Remove `roleLabel` — use "Leading/Trailing" in the table header as "Context"
- For each game, render rows for BOTH `leading_strategy` and `trailing_strategy` (if triggered)
- Add `data-lev-context` attribute to each row
- Update margin bucket derivation to use 3 buckets (0-6/7-12/13+)

Find `function loadLeadingEv()` and replace it. The table header changes from:
```javascript
html += '<th>' + roleLabel + ' Team</th>';
```
to:
```javascript
html += '<th>Context</th><th>Team</th>';
```

The per-game loop changes from rendering one strategy to rendering both:

```javascript
    for (const g of games.slice().reverse()) {
      const dateStr = (g.first_snapshot_ts || '').split('T')[0];
      const scoreStr = g.final_home_score != null ? g.final_home_score + '-' + g.final_away_score : '—';

      for (const [ctx, stratKey] of [['leading', 'leading_strategy'], ['trailing', 'trailing_strategy']]) {
        const strat = g[stratKey] || {};
        if (!strat.triggered) continue;

        // ... render row with data-lev-context="leading" or "trailing"
        // Context column shows "Leading" or "Trailing"
```

- [ ] **Step 3: Add Context filter dropdown**

In the BK filter controls section (around line 1465-1494 with the existing 5 filter dropdowns), add a Context dropdown:

```html
          <select id="lev-f-context" onchange="_levApplyFilters()" style="...">
            <option value="">All</option>
            <option value="leading">Leading</option>
            <option value="trailing">Trailing</option>
          </select>
```

Update `_levApplyFilters()` to check `data-lev-context` attribute against the Context filter.

Update `_levResetFilters()` to reset the Context filter.

Update localStorage save/restore for `lev_filters` to include `context`.

- [ ] **Step 4: Remove `_levOnRoleChange()` and update related functions**

Remove the `_levOnRoleChange()` function entirely.

In `_levSwitchView()`, remove the line that hides/shows the BK role dropdown:
```javascript
  const bkRoleLabel = document.getElementById('lev-role')?.parentElement;
  if (bkRoleLabel) bkRoleLabel.style.display = isBk ? '' : 'none';
```

Update `_levSavePrice()` to save threshold to both config keys:
```javascript
  fetch(API + '/api/config').then(r => r.json()).then(cfg => {
    cfg['odds_monitor_leading_threshold'] = price;
    cfg['odds_monitor_trailing_threshold'] = price;
    return fetch(API + '/api/config', { method: 'POST', ... });
  });
```

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: remove BK role dropdown, render both strategies, add Context filter (#229)"
```

---

### Task 4: Dashboard — Add 5D Combo Table to Spread Analysis

**Files:**
- Modify: `dashboard.html` (`loadSpreadAnalysis()` function)

- [ ] **Step 1: Add 5D combo table rendering to `loadSpreadAnalysis()`**

Find `function loadSpreadAnalysis()`. After the 2D combos section (after the combos table `</div></div>`), add the 5D combo table:

```javascript
    // 5D Combo Table (#229)
    const c5d = data.combo_5d || [];
    if (c5d.length) {
      h += '<div style="margin-top:10px"><details><summary style="cursor:pointer;font-size:0.78em;font-weight:600;color:var(--nrl-green)">📊 5D Combo Table (' + c5d.length + ' combos)</summary>';
      h += '<div style="overflow-x:auto;max-height:400px;overflow-y:auto;margin-top:4px">';
      h += '<table class="analysis-table" id="lev-bk-5d-table"><thead><tr>';
      const hdrs = ['Side','Role','Margin','Entry Min','Context','Games','Covered','P&L','ROI'];
      const sortKeys = ['side','role','margin','entry_minute','margin_context','games','covered','pnl','roi'];
      for (let i = 0; i < hdrs.length; i++) {
        h += '<th style="cursor:pointer;user-select:none" onclick="_levBk5dSort(\'' + sortKeys[i] + '\')">' + hdrs[i] + '</th>';
      }
      h += '</tr></thead><tbody>';
      window._levBk5dData = c5d.map(c => Object.assign({}, c));
      h += _levBk5dRows(c5d);
      h += '</tbody></table></div></details></div>';
    }
```

- [ ] **Step 2: Add 5D table sort + row render helpers**

After `loadSpreadAnalysis()`, add:

```javascript
window._levBk5dSortCol = 'roi';
window._levBk5dSortAsc = false;

function _levBk5dRows(data) {
  let h = '';
  const roiColor = v => v != null && v >= 0 ? 'var(--green)' : 'var(--red)';
  const fmtRoi = v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(1) + '%' : '—';
  const fmtPnl = v => v != null ? (v >= 0 ? '+' : '') + v.toFixed(2) + 'u' : '—';
  const lcIcon = lc => lc ? ' <span title="Low confidence" style="cursor:help">⚠️</span>' : '';
  for (const c of data) {
    h += '<tr>';
    h += '<td>' + esc(c.side) + '</td>';
    h += '<td>' + esc(c.role) + '</td>';
    h += '<td>' + esc(c.margin) + '</td>';
    h += '<td style="text-align:center">' + esc(c.entry_minute) + '</td>';
    h += '<td>' + esc(c.margin_context) + '</td>';
    h += '<td style="text-align:center">' + c.games + lcIcon(c.low_confidence) + '</td>';
    h += '<td style="text-align:center">' + c.covered + '</td>';
    h += '<td style="text-align:center;color:' + roiColor(c.pnl) + '">' + fmtPnl(c.pnl) + '</td>';
    h += '<td style="text-align:center;font-weight:600;color:' + roiColor(c.roi) + '">' + fmtRoi(c.roi) + '</td>';
    h += '</tr>';
  }
  return h;
}

function _levBk5dSort(col) {
  if (window._levBk5dSortCol === col) {
    window._levBk5dSortAsc = !window._levBk5dSortAsc;
  } else {
    window._levBk5dSortCol = col;
    window._levBk5dSortAsc = (col === 'side' || col === 'role' || col === 'margin' || col === 'entry_minute' || col === 'margin_context');
  }
  const data = window._levBk5dData || [];
  const asc = window._levBk5dSortAsc;
  data.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (va == null) va = -999;
    if (vb == null) vb = -999;
    if (typeof va === 'string') return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    return asc ? va - vb : vb - va;
  });
  const tbody = document.querySelector('#lev-bk-5d-table tbody');
  if (tbody) tbody.innerHTML = _levBk5dRows(data);
}
```

- [ ] **Step 3: Update `loadSpreadAnalysis()` fetch URL to remove role**

Find the line:
```javascript
  fetch(API + '/api/odds-monitor-analysis?role=' + role).then(r => r.json()).then(data => {
```
Replace with:
```javascript
  fetch(API + '/api/odds-monitor-analysis').then(r => r.json()).then(data => {
```

Also remove the `const role = ...` line at the top of the function if it references the removed dropdown.

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: BK 5D combo table + remove role from analysis fetch (#229)"
```

---

### Task 5: Testing + CHANGES.md + Issue Close

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart and test endpoints**

```bash
systemctl --user restart nrl-dashboard.service
sleep 2

# Test merged strategies
curl -s 'http://127.0.0.1:8898/api/odds-monitor-leading?threshold=3.0' | python3 -c "
import json,sys; d=json.load(sys.stdin)
games = d['games']
has_both = sum(1 for g in games if g.get('leading_strategy',{}).get('triggered') or g.get('trailing_strategy',{}).get('triggered'))
print(f'Games: {len(games)}, with triggered strategy: {has_both}')
print(f'Aggregate: bets={d[\"aggregate\"][\"bets\"]}, roi={d[\"aggregate\"][\"roi\"]}')
# Verify margin_context
for g in games:
    ls = g.get('leading_strategy', {})
    ts = g.get('trailing_strategy', {})
    if ls.get('triggered'):
        assert ls.get('margin_context') == 'leading'
    if ts.get('triggered'):
        assert ts.get('margin_context') == 'trailing'
print('PASS: merged strategies')
"

# Test analysis with 5D combos
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Dims: {list(d[\"dimensions\"].keys())}')
assert 'margin_context' in d['dimensions']
print(f'5D combos: {len(d.get(\"combo_5d\", []))}')
print(f'2D combos: {len(d[\"combos\"])}')
print('PASS: analysis')
"

echo 'ALL PASS'
```

- [ ] **Step 2: Browser test**

1. Load dashboard → Spread Strategy → BK Spread
2. Verify Role dropdown is GONE
3. Verify history table shows Context column (Leading/Trailing per row)
4. Verify both leading and trailing strategies appear for games where both triggered
5. Test Context filter (All/Leading/Trailing)
6. Open Spread Analysis — verify 5D Combo Table section appears
7. Sort 5D table by ROI, by Context
8. Verify profitable/anti patterns show more variety (7D pairs)

- [ ] **Step 3: Update CHANGES.md**

```markdown
## 2026-07-03 — BK Spread: merged strategies, 7D patterns, 5D combo table (#229)

- **`odds_monitor.py`**: `compute_spread_analysis()` now runs both leading + trailing strategies per game (role param removed). Each strategy tagged with `margin_context`. Pattern detection expanded from subset to all 21 2D pairs from 7 dimensions (side, role, margin, entry_minute, margin_context, spread_price, ev_edge). Added 5D combo table (side × role × margin × entry_minute × margin_context).
- **`server.py`**: `/api/odds-monitor-leading` always computes both strategies, merged aggregates. `/api/odds-monitor-analysis` no longer accepts role param.
- **`dashboard.html`**: Removed Role dropdown from BK Spread header. History table renders both strategies per game with Context column. Added Context filter dropdown. Spread Analysis gains 5D Combo Table (sortable).

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for BK Spread merged strategies (#229)"
git push
```

- [ ] **Step 5: Close issue**

```bash
gh issue comment 229 --body "Implementation complete. Leading+trailing merged, 7D patterns (21 pairs), 5D combo table. Role dropdown removed."
gh issue close 229
```
