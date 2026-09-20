# Historical Win Rate Enhancements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Min Games/pp client-side filters, change margin from 4→3 buckets globally with multiselect checkboxes, and show dual Leading+Trailing pattern tables in Final Stretch perspective.

**Architecture:** Change `_MARGIN_RANGES` constant and BK `_bucket()` inline margin logic from 4→3 buckets. Update `margin_bucket` server filter to handle comma-separated values. Dashboard: replace margin dropdown with checkboxes, add Min Games/Min pp inputs, add dual-fetch pattern rendering for Final Stretch.

**Tech Stack:** Python 3 (stdlib only), vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-02-historical-win-rate-enhancements-design.md`

---

### Task 1: Backend — Margin 4→3 Buckets + Comma-Separated Filter

**Files:**
- Modify: `odds_monitor.py:1022` (BK `_bucket()` inline margin)
- Modify: `odds_monitor.py:1317` (`_MARGIN_RANGES` constant)
- Modify: `odds_monitor.py:1544-1545` (margin_bucket filter)

- [ ] **Step 1: Change `_MARGIN_RANGES` constant**

At line 1317, change:
```python
_MARGIN_RANGES = [(6, "0-6"), (12, "7-12"), (20, "13-20"), (None, "21+")]
```
to:
```python
_MARGIN_RANGES = [(6, "0-6"), (12, "7-12"), (None, "13+")]
```

- [ ] **Step 2: Change BK `_bucket()` inline margin**

At line 1022, change:
```python
            "margin": "0-6" if margin <= 6 else ("7-12" if margin <= 12 else ("13-20" if margin <= 20 else "21+")),
```
to:
```python
            "margin": "0-6" if margin <= 6 else ("7-12" if margin <= 12 else "13+"),
```

- [ ] **Step 3: Change margin_bucket filter to handle comma-separated**

At lines 1544-1545, change:
```python
    if margin_bucket and margin_bucket != "all":
        games = [g for g in games if _hist_bucket(g["_margin"], _MARGIN_RANGES) == margin_bucket]
```
to:
```python
    if margin_bucket and margin_bucket != "all":
        allowed_margins = set(m.strip() for m in margin_bucket.split(","))
        games = [g for g in games if _hist_bucket(g["_margin"], _MARGIN_RANGES) in allowed_margins]
```

- [ ] **Step 4: Test**

```bash
python3 -c "
import json, odds_monitor as om
h = json.load(open('game_history.json'))

# Verify 3 buckets
r = om.compute_historical_win_analysis(h, 'leading', 70)
margin_dim = r['dimensions'].get('margin_at_70', {})
print(f'Margin buckets: {list(margin_dim.keys())}')
assert '13+' in margin_dim, 'Should have 13+ bucket'
assert '13-20' not in margin_dim, 'Should NOT have 13-20'
assert '21+' not in margin_dim, 'Should NOT have 21+'

# Verify comma-separated filter
r2 = om.compute_historical_win_analysis(h, 'leading', 70, margin_bucket='0-6,7-12')
print(f'Filtered 0-6+7-12: {r2[\"total_filtered\"]}g')
# Should only have 0-6 and 7-12 margin games
r3 = om.compute_historical_win_analysis(h, 'leading', 70, margin_bucket='13+')
print(f'Filtered 13+: {r3[\"total_filtered\"]}g')
assert r2['total_filtered'] + r3['total_filtered'] == r['total_filtered']

# Verify BK analysis also uses 3 buckets
bk = om.compute_spread_analysis(om.load_history(), 'leading')
bk_margin = bk['dimensions'].get('margin', {})
print(f'BK margin buckets: {list(bk_margin.keys())}')
assert '13+' in bk_margin or not bk_margin, 'BK should use 13+ bucket'

print('PASS')
"
```

- [ ] **Step 5: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: margin 4→3 buckets (13+ replaces 13-20/21+) + comma-separated filter (#228)"
```

---

### Task 2: Dashboard HTML — Margin Checkboxes + Min Games/pp Inputs

**Files:**
- Modify: `dashboard.html:1553-1559` (margin dropdown → checkboxes)
- Modify: `dashboard.html:1568` (add Min Games/pp after Reset)

- [ ] **Step 1: Replace margin dropdown with checkboxes**

Replace lines 1553-1559 (the margin `<label>` and `<select>`) with:

```html
          <span style="color:var(--muted)">Margin:
            <label><input type="checkbox" class="lev-margin-cb" value="0-6" checked> 0-6</label>
            <label><input type="checkbox" class="lev-margin-cb" value="7-12" checked> 7-12</label>
            <label><input type="checkbox" class="lev-margin-cb" value="13+" checked> 13+</label>
          </span>
```

- [ ] **Step 2: Add Min Games and Min pp inputs after Reset button**

After the Reset button (line 1568), add:

```html
          <span style="border-left:1px solid var(--border);padding-left:8px;color:var(--muted)">
            Min Games: <input type="number" id="lev-hist-mingames" value="5" min="1" max="100" step="1" onchange="_levHistFetch()" style="width:45px;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:1px 3px;border-radius:3px;font-size:1em">
            Min pp: <input type="number" id="lev-hist-minpp" value="10" min="0" max="50" step="1" onchange="_levHistFetch()" style="width:45px;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:1px 3px;border-radius:3px;font-size:1em">
          </span>
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: margin checkboxes + Min Games/pp inputs in filter bar (#228)"
```

---

### Task 3: Dashboard JS — Wire Up Margin Checkboxes + Min Filters + Dual Patterns

**Files:**
- Modify: `dashboard.html` (JS functions)

- [ ] **Step 1: Update `_levHistGetFilters()`**

Find `function _levHistGetFilters()` and replace it with:

```javascript
function _levHistGetFilters() {
  const minutes = [];
  document.querySelectorAll('.lev-min-cb:checked').forEach(cb => minutes.push(parseInt(cb.value)));
  if (!minutes.length) minutes.push(70);
  const margins = [];
  document.querySelectorAll('.lev-margin-cb:checked').forEach(cb => margins.push(cb.value));
  return {
    perspective: document.getElementById('lev-hist-role')?.value || 'leading',
    season: document.getElementById('lev-hist-season')?.value || 'all',
    segment: document.getElementById('lev-hist-segment')?.value || 'all',
    side: document.getElementById('lev-hist-side')?.value || 'all',
    fav_role: document.getElementById('lev-hist-favrole')?.value || 'all',
    margin_context: document.getElementById('lev-hist-context')?.value || 'all',
    margin_bucket: margins.length === 3 ? 'all' : margins.join(','),
    minutes: minutes,
    page_size: parseInt(document.getElementById('lev-hist-pagesize')?.value || '25'),
    min_games: parseInt(document.getElementById('lev-hist-mingames')?.value || '5'),
    min_pp: parseInt(document.getElementById('lev-hist-minpp')?.value || '10'),
  };
}
```

- [ ] **Step 2: Update `_levInitView()`**

In `_levInitView()`, update the defaults and add margin checkbox + min filter restoration. Find the defaults line and replace the function:

```javascript
function _levInitView() {
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('lev_hist_filters') || '{}'); } catch(e) {}
  const defaults = {perspective:'leading',season:'all',segment:'all',side:'all',fav_role:'all',margin_context:'all',margin_bucket:'all',minutes:[70],page_size:25,min_games:5,min_pp:10};
  const f = Object.assign({}, defaults, saved);
  const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  _s('lev-hist-role', f.perspective);
  _s('lev-hist-season', f.season);
  _s('lev-hist-segment', f.segment);
  _s('lev-hist-side', f.side);
  _s('lev-hist-favrole', f.fav_role);
  _s('lev-hist-context', f.margin_context);
  // Margin checkboxes (#228)
  const marginSelected = f.margin_bucket === 'all' ? ['0-6','7-12','13+'] : f.margin_bucket.split(',');
  document.querySelectorAll('.lev-margin-cb').forEach(cb => {
    cb.checked = marginSelected.includes(cb.value);
    cb.onchange = () => _levHistFetch();
  });
  // Minute checkboxes
  document.querySelectorAll('.lev-min-cb').forEach(cb => {
    cb.checked = (f.minutes || [70]).includes(parseInt(cb.value));
    cb.onchange = () => _levHistFetch();
  });
  // Min Games/pp (#228)
  _s('lev-hist-mingames', f.min_games);
  _s('lev-hist-minpp', f.min_pp);
  try { localStorage.removeItem('lev_hist_minute'); localStorage.removeItem('lev_hist_role'); } catch(e) {}
  const savedView = localStorage.getItem('lev_view') || 'bk';
  _levSwitchView(savedView);
}
```

- [ ] **Step 3: Update `_levHistReset()`**

Replace the function:

```javascript
function _levHistReset() {
  const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  _s('lev-hist-role', 'leading');
  _s('lev-hist-season', 'all');
  _s('lev-hist-segment', 'all');
  _s('lev-hist-side', 'all');
  _s('lev-hist-favrole', 'all');
  _s('lev-hist-context', 'all');
  document.querySelectorAll('.lev-margin-cb').forEach(cb => { cb.checked = true; });
  document.querySelectorAll('.lev-min-cb').forEach(cb => { cb.checked = cb.value === '70'; });
  _s('lev-hist-mingames', '5');
  _s('lev-hist-minpp', '10');
  _levHistFetch(1);
}
```

- [ ] **Step 4: Update `_levHistSetFilter()` for margin checkboxes**

Replace the function:

```javascript
function _levHistSetFilter(dim, val) {
  const map = {side:'lev-hist-side', role:'lev-hist-favrole', margin_context:'lev-hist-context'};
  const id = map[dim];
  if (id) {
    const el = document.getElementById(id);
    if (el) { el.value = val; _levHistFetch(1); }
  } else if (dim === 'margin') {
    // Margin uses checkboxes — check only the clicked value (#228)
    document.querySelectorAll('.lev-margin-cb').forEach(cb => { cb.checked = cb.value === val; });
    _levHistFetch(1);
  }
}
```

- [ ] **Step 5: Update `loadHistoricalWinRate()` — apply Min Games/pp + dual patterns**

In `loadHistoricalWinRate()`, make these changes:

**5a.** After `const f = _levHistGetFilters();`, add:
```javascript
  const minGames = f.min_games || 5;
  const minPp = f.min_pp || 10;
```

**5b.** In the hasFilters check, also check margin_bucket:
```javascript
    const hasFilters = f.season !== 'all' || f.segment !== 'all' || f.side !== 'all' || f.fav_role !== 'all' || f.margin_context !== 'all' || f.margin_bucket !== 'all';
```
This line already exists and already checks margin_bucket — no change needed.

**5c.** In the Notable Patterns rendering, add min filters. Replace the pattern loops:

For profitable patterns:
```javascript
      for (const p of profs.slice(0, 15)) {
```
Change to:
```javascript
      for (const p of profs) {
        if (p.games < minGames) continue;
        if (Math.abs(p.rate - bl.rate) < minPp) continue;
```

For anti patterns:
```javascript
      for (const p of antis.slice(0, 15)) {
```
Change to:
```javascript
      for (const p of antis) {
        if (p.games < minGames) continue;
        if (Math.abs(p.rate - bl.rate) < minPp) continue;
```

**5d.** In the dimensional breakdowns, apply minGames filter. In the bucket row loop:

After `for (const bk of bKeys) {`, add:
```javascript
          const b = buckets[bk];
          if (b.games < minGames) continue;
```
And remove the duplicate `const b = buckets[bk];` that follows.

**5e.** In the 2D combos section, apply both filters. After `for (const c of top20) {`, change to filter:
```javascript
      const filtered2d = combos.filter(c => c.games >= minGames && Math.abs(c.rate - bl.rate) >= minPp);
      const top20 = filtered2d.slice(0, 20);
```
Replace the existing `const top20 = combos.slice(0, 20);` line.

**5f.** In the 5D combo table, apply both filters. After `window._levHist5dData = c5d.map(...)`:
```javascript
      window._levHist5dData = c5d
        .filter(c => c.games >= minGames && Math.abs(c.rate - bl.rate) >= minPp)
        .map(c => Object.assign({}, c, {_delta: Math.round((c.rate - bl.rate) * 10) / 10}));
```

**5g.** Add dual pattern fetch for Final Stretch. After the main fetch's `.then(data => {` handler processes the data and before `contentEl.innerHTML = h`, add the dual fetch logic. The cleanest approach: build the main content as before, then if Final Stretch, prepend dual patterns via a second pass.

At the TOP of the `.then(data => {` callback, before any rendering, add:
```javascript
    // Dual pattern fetch for Final Stretch (#228)
    if (isStretch) {
      const baseQs = qs.replace('role=final_stretch', 'role=PLACEHOLDER');
      Promise.all([
        fetch(API + '/api/odds-monitor-historical?' + baseQs.replace('PLACEHOLDER', 'leading')).then(r => r.json()),
        fetch(API + '/api/odds-monitor-historical?' + baseQs.replace('PLACEHOLDER', 'trailing')).then(r => r.json()),
      ]).then(([leadData, trailData]) => {
        const dualEl = document.getElementById('lev-hist-dual-patterns');
        if (!dualEl || !leadData?.baseline || !trailData?.baseline) return;
        let dh = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">';
        // Leading patterns
        dh += '<div>';
        const lProfs = (leadData.profitable_patterns || []).filter(p => p.games >= minGames && Math.abs(p.rate - leadData.baseline.rate) >= minPp);
        const lAnti = (leadData.anti_patterns || []).filter(p => p.games >= minGames && Math.abs(p.rate - leadData.baseline.rate) >= minPp);
        if (lProfs.length) {
          dh += '<div style="border:1px solid var(--green);border-radius:6px;padding:8px 12px;margin-bottom:6px;background:var(--bg)">';
          dh += '<div style="font-size:0.78em;font-weight:600;color:var(--green);margin-bottom:4px">Leading — Profitable (' + leadData.baseline.rate + '% baseline)</div>';
          for (const p of lProfs.slice(0, 10)) {
            const delta = (p.rate - leadData.baseline.rate).toFixed(1);
            dh += '<div style="font-size:0.74em;margin:2px 0;color:var(--green)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp</div>';
          }
          dh += '</div>';
        }
        if (lAnti.length) {
          dh += '<div style="border:1px solid var(--red);border-radius:6px;padding:8px 12px;background:var(--bg)">';
          dh += '<div style="font-size:0.78em;font-weight:600;color:var(--red);margin-bottom:4px">Leading — Anti-Patterns</div>';
          for (const p of lAnti.slice(0, 10)) {
            const delta = (p.rate - leadData.baseline.rate).toFixed(1);
            dh += '<div style="font-size:0.74em;margin:2px 0;color:var(--red)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp</div>';
          }
          dh += '</div>';
        }
        dh += '</div>';
        // Trailing patterns
        dh += '<div>';
        const tProfs = (trailData.profitable_patterns || []).filter(p => p.games >= minGames && Math.abs(p.rate - trailData.baseline.rate) >= minPp);
        const tAnti = (trailData.anti_patterns || []).filter(p => p.games >= minGames && Math.abs(p.rate - trailData.baseline.rate) >= minPp);
        if (tProfs.length) {
          dh += '<div style="border:1px solid var(--green);border-radius:6px;padding:8px 12px;margin-bottom:6px;background:var(--bg)">';
          dh += '<div style="font-size:0.78em;font-weight:600;color:var(--green);margin-bottom:4px">Trailing — High Comeback (' + trailData.baseline.rate + '% baseline)</div>';
          for (const p of tProfs.slice(0, 10)) {
            const delta = (p.rate - trailData.baseline.rate).toFixed(1);
            dh += '<div style="font-size:0.74em;margin:2px 0;color:var(--green)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp</div>';
          }
          dh += '</div>';
        }
        if (tAnti.length) {
          dh += '<div style="border:1px solid var(--red);border-radius:6px;padding:8px 12px;background:var(--bg)">';
          dh += '<div style="font-size:0.78em;font-weight:600;color:var(--red);margin-bottom:4px">Trailing — Low Comeback</div>';
          for (const p of tAnti.slice(0, 10)) {
            const delta = (p.rate - trailData.baseline.rate).toFixed(1);
            dh += '<div style="font-size:0.74em;margin:2px 0;color:var(--red)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp</div>';
          }
          dh += '</div>';
        }
        dh += '</div></div>';
        dualEl.innerHTML = dh;
      }).catch(() => {});
    }
```

And in the main HTML output, BEFORE the Notable Patterns section, add a placeholder div:
```javascript
    // Dual patterns placeholder for Final Stretch (#228)
    if (isStretch) {
      h += '<div id="lev-hist-dual-patterns" style="margin-bottom:8px"></div>';
    }
```

- [ ] **Step 6: Commit**

```bash
git add dashboard.html
git commit -m "feat: margin checkboxes, Min Games/pp filters, dual patterns in Final Stretch (#228)"
```

---

### Task 4: Testing + CHANGES.md + Issue Close

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart dashboard and test**

```bash
systemctl --user restart nrl-dashboard.service
sleep 2

# Test 3-bucket margin
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=70' | python3 -c "
import json,sys; d=json.load(sys.stdin)
margin = d['dimensions'].get('margin_at_70', d['dimensions'].get('margin', {}))
print(f'Margin buckets: {list(margin.keys())}')
assert '13+' in margin and '13-20' not in margin and '21+' not in margin
print('PASS: 3-bucket margin')
"

# Test comma-separated margin filter
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=70&margin_bucket=0-6,7-12' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Filtered: {d[\"total_filtered\"]}/{d[\"total_unfiltered\"]}')
assert d['total_filtered'] < d['total_unfiltered']
print('PASS: comma margin filter')
"

echo 'ALL PASS'
```

- [ ] **Step 2: Browser test**

1. Load dashboard → Spread Strategy → Historical Win Rate
2. Verify margin shows 3 checkboxes (0-6, 7-12, 13+) — all checked
3. Uncheck 13+ — data reloads with fewer games
4. Verify Min Games input (default 5) — change to 20, patterns and combos with <20 games should disappear
5. Verify Min pp input (default 10) — change to 20, only patterns with ≥20pp deviation show
6. Switch to Final Stretch — verify dual Leading + Trailing pattern grid appears above the main outscoring patterns
7. Reset — all checkboxes checked, Min Games=5, Min pp=10
8. Refresh page — filters persist

- [ ] **Step 3: Update CHANGES.md**

Add at top:

```markdown
## 2026-07-02 — Historical Win Rate: Min Games/pp, margin 3-bucket, dual patterns (#228)

- **`odds_monitor.py`**: Changed `_MARGIN_RANGES` from 4 buckets (0-6/7-12/13-20/21+) to 3 (0-6/7-12/13+). Updated BK spread analysis `_bucket()` margin to match. `margin_bucket` filter now accepts comma-separated values for multiselect.
- **`dashboard.html`**: Margin filter changed from dropdown to multiselect checkboxes. Added Min Games (default 5) and Min pp (default 10) client-side filters — hide dimensional/pattern/combo rows below thresholds. Final Stretch perspective shows dual Leading + Trailing pattern tables side by side via two additional API calls.

**Files changed:** `odds_monitor.py`, `dashboard.html`
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for Historical Win Rate enhancements (#228)"
git push
```

- [ ] **Step 5: Close issue**

```bash
gh issue comment 228 --body "Implementation complete. Margin 4→3 buckets, Min Games/pp filters, dual Leading+Trailing patterns in Final Stretch. See CHANGES.md."
gh issue close 228
```
