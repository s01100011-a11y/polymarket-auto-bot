# Historical Win Rate Filters — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add server-side filters, a 5D combo table, and a paginated game history list to the Historical Win Rate sub-view, with multi-minute entry as a bucketed dimension.

**Architecture:** Extend `compute_historical_win_analysis()` with filter params (season, segment, side, fav_role, margin_context, margin_bucket), multi-minute support, 5D combo computation, and paginated game list output. Extend `/api/odds-monitor-historical` to parse filter + pagination params. Replace dashboard historical view controls with full filter bar, add 5D combo table (sortable, click-through), and game history list with paging.

**Tech Stack:** Python 3 (stdlib only), vanilla JS, existing dashboard patterns.

**Spec:** `docs/superpowers/specs/2026-07-01-historical-win-rate-filters-design.md`

---

### Task 1: Backend — Multi-Minute Support + Season/Segment Pre-Filter

**Files:**
- Modify: `odds_monitor.py:1323-1599` (`compute_historical_win_analysis()`)

- [ ] **Step 1: Update function signature and docstring**

Change line 1323:

```python
def compute_historical_win_analysis(game_history, role="leading", target_minute=70,
                                     minutes=None, season=None, segment=None,
                                     side=None, fav_role=None, margin_context=None,
                                     margin_bucket=None, page=1, page_size=25):
    """Analyse win rate of leading/trailing team at target_minute using PBP cache.

    Args:
        game_history: list of game records from game_history.json
        role: "leading" (win rate), "trailing" (comeback rate),
              or "final_stretch" (who outscores in last 10 min — score reset)
        target_minute: game minute to evaluate score (default 70, used when minutes is None)
        minutes: list of ints from {65,68,70,72,75} — multi-minute evaluation (#227)
        season: "all", "last3", "last5", or specific year string (#227)
        segment: "all", "regular_season", "finals", "state_of_origin" (#227)
        side: "all", "home", "away" — filter by analysed team's side (#227)
        fav_role: "all", "favourite", "underdog" — filter by pregame role (#227)
        margin_context: "all", "leading", "trailing" — filter by lead/trail at minute (#227)
        margin_bucket: "all", "0-6", "7-12", "13-20", "21+" (#227)
        page: page number for game list (1-based) (#227)
        page_size: games per page (25/50/100) (#227)

    Returns dict with baseline, dimensions, combos, combo_5d, game_list,
    patterns, pagination metadata.
    """
```

- [ ] **Step 2: Add season/segment pre-filter and multi-minute loop**

Replace lines 1336-1347 (from `games = []` through the `score_at_minute` call) with:

```python
    from collections import defaultdict

    # Resolve minutes list (#227)
    ALLOWED_MINUTES = {65, 68, 70, 72, 75}
    if minutes:
        eval_minutes = [m for m in minutes if m in ALLOWED_MINUTES] or [target_minute]
    else:
        eval_minutes = [target_minute]

    # Season/segment pre-filter (#227)
    if season and season != "all":
        if season == "last3":
            all_seasons = sorted(set(g.get("season", 0) for g in game_history), reverse=True)
            valid_seasons = set(all_seasons[:3])
            game_history = [g for g in game_history if g.get("season") in valid_seasons]
        elif season == "last5":
            all_seasons = sorted(set(g.get("season", 0) for g in game_history), reverse=True)
            valid_seasons = set(all_seasons[:5])
            game_history = [g for g in game_history if g.get("season") in valid_seasons]
        else:
            try:
                yr = int(season)
                game_history = [g for g in game_history if g.get("season") == yr]
            except (ValueError, TypeError):
                pass
    if segment and segment != "all":
        game_history = [g for g in game_history if g.get("season_segment") == segment]

    # Compute available seasons for dropdown (before filtering)
    available_seasons = sorted(set(
        str(g.get("season", "")) for g in game_history if g.get("season")), reverse=True)

    games = []
    tied = 0
    draws = 0
    cache_misses = 0
    is_final_stretch = role == "final_stretch"

    for g in game_history:
        cp = match_id_to_cache_path(g.get("match_id", ""))
        if not cp:
            cache_misses += 1
            continue
        home_final = g.get("home_score")
        away_final = g.get("away_score")
        if home_final is None or away_final is None:
            continue
        if home_final == away_final:
            draws += 1
            continue

        # Multi-minute loop (#227)
        for eval_min in eval_minutes:
            s = score_at_minute(cp, eval_min)
            if not s:
                continue
```

Note: the rest of the per-game enrichment logic (from `if is_final_stretch:` through `games.append(rec)`) stays the same but is now inside the `for eval_min in eval_minutes:` loop. Add these fields to each `rec` after creating it (before the role/halftime/conditions enrichment):

```python
            # After determining side/won and creating rec, add:
            rec["_entry_minute"] = eval_min
            rec["_score_home_at_min"] = s["home_score"]
            rec["_score_away_at_min"] = s["away_score"]
            # Margin context: was the analysed team leading or trailing? (#227)
            if is_final_stretch:
                m70 = s["margin"]
                rec["_margin_context"] = "leading" if (m70 > 0 if side == "home" else m70 < 0) else ("trailing" if (m70 < 0 if side == "home" else m70 > 0) else "tied")
            else:
                rec["_margin_context"] = "leading" if role == "leading" else "trailing"
```

The `tied` counter now only increments once per game-minute pair (not per game).

- [ ] **Step 3: Add post-enrichment filters**

After the game enrichment loop (after all `games.append()` calls), before the baseline computation, add:

```python
    # Record total before post-filters for "X/Y" display (#227)
    total_unfiltered = len(games)

    # Post-enrichment filters (#227)
    if side and side != "all":
        games = [g for g in games if g["_side"] == side]
    if fav_role and fav_role != "all":
        games = [g for g in games if g["_role"] == fav_role]
    if margin_context and margin_context != "all":
        games = [g for g in games if g.get("_margin_context") == margin_context]
    if margin_bucket and margin_bucket != "all":
        games = [g for g in games if _hist_bucket(g["_margin"], _MARGIN_RANGES) == margin_bucket]
```

- [ ] **Step 4: Add entry_minute dimension when multi-minute**

In the dimensional breakdown section, after the existing dimensions, add:

```python
    # Entry minute dimension (when multi-minute) (#227)
    if len(eval_minutes) > 1:
        dims["entry_minute"] = _dim_stats([
            (str(g["_entry_minute"]), g["_won"]) for g in games])
```

- [ ] **Step 5: Add 5D combo computation**

After the pattern detection section (after `anti = [...]`), add:

```python
    # ── 5D combo table (#227) ────────────────────────────────────────────
    combo_5d_buckets = defaultdict(lambda: {"games": 0, "wins": 0})
    for rec in games:
        key = (rec["_side"], rec["_role"], rec.get("_margin_context", "?"),
               _hist_bucket(rec["_margin"], _MARGIN_RANGES),
               str(rec.get("_entry_minute", eval_minutes[0])))
        combo_5d_buckets[key]["games"] += 1
        if rec["_won"]:
            combo_5d_buckets[key]["wins"] += 1

    combo_5d = []
    for (s, r, mc, m, em), val in combo_5d_buckets.items():
        if val["games"] < 3:
            continue
        rate = round(val["wins"] / val["games"] * 100, 1)
        combo_5d.append({
            "side": s, "role": r, "margin_context": mc,
            "margin": m, "entry_minute": em,
            "games": val["games"], "wins": val["wins"],
            "rate": rate, "low_confidence": val["games"] < 10,
        })
    combo_5d.sort(key=lambda c: abs(c["rate"] - baseline_rate), reverse=True)
```

- [ ] **Step 6: Add paginated game list**

After the 5D combo section, add:

```python
    # ── Paginated game list (#227) ───────────────────────────────────────
    # Sort: kickoff_utc desc, match_id asc, entry_minute asc
    def _sort_key(rec):
        g = rec["_game"]
        return (-(g.get("kickoff_utc") or g.get("ts") or ""),
                g.get("match_id", ""),
                rec.get("_entry_minute", 0))
    sorted_games = sorted(games, key=_sort_key)

    total_filtered = len(sorted_games)
    total_pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_games = sorted_games[start:start + page_size]

    game_list = []
    for rec in page_games:
        g = rec["_game"]
        entry_min = rec.get("_entry_minute", eval_minutes[0])
        score_at = f"{rec.get('_score_home_at_min', '?')}-{rec.get('_score_away_at_min', '?')}"
        game_list.append({
            "date": (g.get("kickoff_utc") or g.get("ts", ""))[:10],
            "home_team": g.get("home_team", ""),
            "away_team": g.get("away_team", ""),
            "home_score": g.get("home_score"),
            "away_score": g.get("away_score"),
            "final_margin": abs((g.get("home_score") or 0) - (g.get("away_score") or 0)),
            "team": rec.get("_team", ""),
            "entry_minute": entry_min,
            "score_at_entry": score_at,
            "margin_at_entry": rec["_margin"],
            "won": rec["_won"],
            "conditions_count": len(g.get("conditions_fired", [])),
            "tags": g.get("postgame_tags", []),
        })
```

Note: `_score_home_at_min` and `_score_away_at_min` are already set in Step 2 during enrichment.

- [ ] **Step 7: Update return dict**

Replace the return dict (line 1587-1599) with:

```python
    return {
        "baseline": {"games": len(games), "wins": total_wins, "rate": baseline_rate},
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "combo_5d": combo_5d,
        "game_list": game_list,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_filtered": total_filtered,
        "total_unfiltered": total_unfiltered,
        "total_games": len(game_history),
        "tied_at_minute": tied,
        "draws": draws,
        "cache_misses": cache_misses,
        "role": role,
        "target_minute": eval_minutes[0] if len(eval_minutes) == 1 else eval_minutes[0],
        "minutes_evaluated": eval_minutes,
        "available_seasons": available_seasons,
    }
```

Also update the empty-result early return (around line 1317 equivalent) to include the new fields:

```python
    if not games:
        return {"baseline": {"games": 0, "wins": 0, "rate": 0},
                "dimensions": {}, "combos": [], "profitable_patterns": [],
                "anti_patterns": [], "combo_5d": [], "game_list": [],
                "page": 1, "page_size": page_size, "total_pages": 0,
                "total_filtered": 0, "total_unfiltered": 0,
                "total_games": len(game_history),
                "tied_at_minute": tied, "draws": draws,
                "cache_misses": cache_misses, "role": role,
                "target_minute": eval_minutes[0],
                "minutes_evaluated": eval_minutes,
                "available_seasons": available_seasons}
```

- [ ] **Step 8: Test**

```bash
python3 -c "
import json, odds_monitor as om
h = json.load(open('game_history.json'))

# Basic — should match #226 results
r = om.compute_historical_win_analysis(h, role='leading', target_minute=70)
print(f'Leading: {r[\"baseline\"][\"rate\"]}% ({r[\"baseline\"][\"games\"]}g)')
assert r['baseline']['rate'] > 90
assert 'combo_5d' in r and 'game_list' in r
assert r['total_pages'] > 0
assert len(r['game_list']) <= 25
print(f'5D combos: {len(r[\"combo_5d\"])}, Game list: {len(r[\"game_list\"])}/{r[\"total_filtered\"]}')

# Multi-minute
r2 = om.compute_historical_win_analysis(h, role='leading', minutes=[68, 70, 72])
print(f'Multi-min: {r2[\"baseline\"][\"games\"]}g, minutes={r2[\"minutes_evaluated\"]}')
assert r2['baseline']['games'] > r['baseline']['games'], 'Multi-minute should have more records'
assert 'entry_minute' in r2['dimensions'], 'Should have entry_minute dimension'

# Season filter
r3 = om.compute_historical_win_analysis(h, role='leading', target_minute=70, season='2025')
print(f'2025 only: {r3[\"baseline\"][\"games\"]}g')
assert r3['baseline']['games'] < r['baseline']['games']

# Side filter
r4 = om.compute_historical_win_analysis(h, role='leading', target_minute=70, side='home')
print(f'Home only: {r4[\"baseline\"][\"games\"]}g')
assert r4['baseline']['games'] < r['baseline']['games']

# Pagination
r5 = om.compute_historical_win_analysis(h, role='leading', target_minute=70, page=2)
print(f'Page 2: {len(r5[\"game_list\"])} games, page {r5[\"page\"]}/{r5[\"total_pages\"]}')
assert r5['page'] == 2

# Final stretch
r6 = om.compute_historical_win_analysis(h, role='final_stretch', target_minute=70)
print(f'Stretch: {r6[\"baseline\"][\"games\"]}g, 5D: {len(r6[\"combo_5d\"])}')

print('PASS')
"
```

- [ ] **Step 9: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: historical win analysis — filters, multi-minute, 5D combos, game list (#227)"
```

---

### Task 2: Server Endpoint — Parse Filter + Pagination Params

**Files:**
- Modify: `server.py:4186-4201` (`/api/odds-monitor-historical` handler)

- [ ] **Step 1: Replace the endpoint handler**

Replace lines 4186-4201 with:

```python
        if path == "/api/odds-monitor-historical":
            try:
                params = parse_qs(parsed.query)
                role = (params.get("role") or ["leading"])[0].strip().lower()
                if role not in ("leading", "trailing", "final_stretch"):
                    role = "leading"
                # Parse minutes (comma-separated) or fallback to minute param (#227)
                ALLOWED_MINUTES = {65, 68, 70, 72, 75}
                minutes_raw = (params.get("minutes") or [""])[0].strip()
                if minutes_raw:
                    minutes = [int(m) for m in minutes_raw.split(",")
                               if m.strip().isdigit() and int(m.strip()) in ALLOWED_MINUTES]
                else:
                    # Backwards compat: accept old minute= param
                    m = int((params.get("minute") or ["70"])[0])
                    minutes = [m] if m in ALLOWED_MINUTES else [70]
                minutes = minutes or [70]
                # Filter params (#227)
                season = (params.get("season") or ["all"])[0].strip()
                segment = (params.get("segment") or ["all"])[0].strip()
                side = (params.get("side") or ["all"])[0].strip().lower()
                fav_role = (params.get("fav_role") or ["all"])[0].strip().lower()
                margin_context = (params.get("margin_context") or ["all"])[0].strip().lower()
                margin_bucket = (params.get("margin_bucket") or ["all"])[0].strip()
                # Pagination (#227)
                try:
                    pg = max(1, int((params.get("page") or ["1"])[0]))
                except (ValueError, TypeError):
                    pg = 1
                try:
                    ps = int((params.get("page_size") or ["25"])[0])
                    ps = max(25, min(100, ps))
                except (ValueError, TypeError):
                    ps = 25

                game_hist = _load_history()
                result = odds_monitor.compute_historical_win_analysis(
                    game_hist, role=role, minutes=minutes,
                    season=season, segment=segment, side=side,
                    fav_role=fav_role, margin_context=margin_context,
                    margin_bucket=margin_bucket, page=pg, page_size=ps)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return
```

- [ ] **Step 2: Commit**

```bash
git add server.py
git commit -m "feat: /api/odds-monitor-historical — filter + pagination params (#227)"
```

---

### Task 3: Dashboard HTML — Filter Bar Replacement

**Files:**
- Modify: `dashboard.html:1506-1527` (historical view HTML)

- [ ] **Step 1: Replace the historical view controls**

Replace lines 1506-1527 (the entire `lev-hist-view` div) with:

```html
      <!-- Historical Win Rate view (#226, #227) -->
      <div id="lev-hist-view" style="display:none">
        <!-- Filter bar (#227) -->
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;font-size:0.78em">
          <label style="color:var(--muted)">Perspective:
            <select id="lev-hist-role" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="leading">Leading Team</option>
              <option value="trailing">Trailing Team</option>
              <option value="final_stretch">Final Stretch (0-0 reset)</option>
            </select>
          </label>
          <label style="color:var(--muted)">Season:
            <select id="lev-hist-season" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All Seasons</option>
              <option value="last3">Last 3 Seasons</option>
              <option value="last5">Last 5 Seasons</option>
            </select>
          </label>
          <label style="color:var(--muted)">Segment:
            <select id="lev-hist-segment" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All</option>
              <option value="regular_season">Regular Season</option>
              <option value="finals">Finals</option>
              <option value="state_of_origin">State of Origin</option>
            </select>
          </label>
          <label style="color:var(--muted)">Side:
            <select id="lev-hist-side" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All</option>
              <option value="home">Home</option>
              <option value="away">Away</option>
            </select>
          </label>
          <label style="color:var(--muted)">Role:
            <select id="lev-hist-favrole" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All</option>
              <option value="favourite">Favourite</option>
              <option value="underdog">Underdog</option>
            </select>
          </label>
          <label style="color:var(--muted)">Context:
            <select id="lev-hist-context" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All</option>
              <option value="leading">Leading</option>
              <option value="trailing">Trailing</option>
            </select>
          </label>
          <label style="color:var(--muted)">Margin:
            <select id="lev-hist-margin" onchange="_levHistFetch()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:1em">
              <option value="all">All</option>
              <option value="0-6">0-6</option>
              <option value="7-12">7-12</option>
              <option value="13-20">13-20</option>
              <option value="21+">21+</option>
            </select>
          </label>
          <span style="color:var(--muted)">Min:
            <label><input type="checkbox" class="lev-min-cb" value="65"> 65</label>
            <label><input type="checkbox" class="lev-min-cb" value="68"> 68</label>
            <label><input type="checkbox" class="lev-min-cb" value="70" checked> 70</label>
            <label><input type="checkbox" class="lev-min-cb" value="72"> 72</label>
            <label><input type="checkbox" class="lev-min-cb" value="75"> 75</label>
          </span>
          <button class="btn" onclick="_levHistReset()" style="padding:2px 8px;font-size:1em">Reset</button>
        </div>
        <div id="lev-hist-filter-note" style="font-size:0.72em;color:var(--muted);margin-bottom:8px"></div>
        <div id="lev-hist-baseline" style="margin-bottom:10px"></div>
        <div id="lev-hist-content"><div class="no-data">Select a view to load data</div></div>
      </div>
```

- [ ] **Step 2: Commit**

```bash
git add dashboard.html
git commit -m "feat: historical view filter bar HTML — 7 dropdowns + minute checkboxes (#227)"
```

---

### Task 4: Dashboard JS — Rewrite `loadHistoricalWinRate()` with Filters + 5D + Game List

**Files:**
- Modify: `dashboard.html:8706-8760+` (JS functions)

- [ ] **Step 1: Add filter helper functions**

Replace `_levInitView()` (lines 8706-8718) with:

```javascript
function _levInitView() {
  // Restore saved filters from localStorage (#227)
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem('lev_hist_filters') || '{}'); } catch(e) {}
  const defaults = {perspective:'leading',season:'all',segment:'all',side:'all',fav_role:'all',margin_context:'all',margin_bucket:'all',minutes:[70],page_size:25};
  const f = Object.assign({}, defaults, saved);
  // Set dropdowns
  const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  _s('lev-hist-role', f.perspective);
  _s('lev-hist-season', f.season);
  _s('lev-hist-segment', f.segment);
  _s('lev-hist-side', f.side);
  _s('lev-hist-favrole', f.fav_role);
  _s('lev-hist-context', f.margin_context);
  _s('lev-hist-margin', f.margin_bucket);
  // Set minute checkboxes
  document.querySelectorAll('.lev-min-cb').forEach(cb => {
    cb.checked = (f.minutes || [70]).includes(parseInt(cb.value));
    cb.onchange = _levHistFetch;
  });
  // Restore saved view
  const savedView = localStorage.getItem('lev_view') || 'bk';
  _levSwitchView(savedView);
}

function _levHistGetFilters() {
  const minutes = [];
  document.querySelectorAll('.lev-min-cb:checked').forEach(cb => minutes.push(parseInt(cb.value)));
  if (!minutes.length) minutes.push(70);
  return {
    perspective: document.getElementById('lev-hist-role')?.value || 'leading',
    season: document.getElementById('lev-hist-season')?.value || 'all',
    segment: document.getElementById('lev-hist-segment')?.value || 'all',
    side: document.getElementById('lev-hist-side')?.value || 'all',
    fav_role: document.getElementById('lev-hist-favrole')?.value || 'all',
    margin_context: document.getElementById('lev-hist-context')?.value || 'all',
    margin_bucket: document.getElementById('lev-hist-margin')?.value || 'all',
    minutes: minutes,
    page_size: parseInt(document.getElementById('lev-hist-pagesize')?.value || '25'),
  };
}

function _levHistSaveFilters(f) {
  try { localStorage.setItem('lev_hist_filters', JSON.stringify(f)); } catch(e) {}
}

function _levHistFetch(page) {
  const f = _levHistGetFilters();
  _levHistSaveFilters(f);
  loadHistoricalWinRate(typeof page === 'number' ? page : 1);
}

function _levHistReset() {
  const _s = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
  _s('lev-hist-role', 'leading');
  _s('lev-hist-season', 'all');
  _s('lev-hist-segment', 'all');
  _s('lev-hist-side', 'all');
  _s('lev-hist-favrole', 'all');
  _s('lev-hist-context', 'all');
  _s('lev-hist-margin', 'all');
  document.querySelectorAll('.lev-min-cb').forEach(cb => { cb.checked = cb.value === '70'; });
  _levHistFetch(1);
}

function _levHistSetFilter(dim, val) {
  // Click-through from 5D combo table cell (#227)
  const map = {side:'lev-hist-side', role:'lev-hist-favrole', margin_context:'lev-hist-context', margin:'lev-hist-margin'};
  const id = map[dim];
  if (id) {
    const el = document.getElementById(id);
    if (el) { el.value = val; _levHistFetch(1); }
  }
}
```

- [ ] **Step 2: Rewrite `loadHistoricalWinRate()` with filter params**

Replace the existing `loadHistoricalWinRate()` function (lines 8720-end of function) with:

```javascript
function loadHistoricalWinRate(page) {
  page = page || 1;
  const f = _levHistGetFilters();
  const baselineEl = document.getElementById('lev-hist-baseline');
  const contentEl = document.getElementById('lev-hist-content');
  const noteEl = document.getElementById('lev-hist-filter-note');
  if (!contentEl) return;
  contentEl.innerHTML = '<div class="no-data">Loading...</div>';

  // Build query string (#227)
  let qs = 'role=' + f.perspective + '&minutes=' + f.minutes.join(',');
  if (f.season !== 'all') qs += '&season=' + f.season;
  if (f.segment !== 'all') qs += '&segment=' + f.segment;
  if (f.side !== 'all') qs += '&side=' + f.side;
  if (f.fav_role !== 'all') qs += '&fav_role=' + f.fav_role;
  if (f.margin_context !== 'all') qs += '&margin_context=' + f.margin_context;
  if (f.margin_bucket !== 'all') qs += '&margin_bucket=' + encodeURIComponent(f.margin_bucket);
  qs += '&page=' + page + '&page_size=' + f.page_size;

  fetch(API + '/api/odds-monitor-historical?' + qs).then(r => r.json()).then(data => {
    if (!data || !data.baseline) {
      contentEl.innerHTML = '<div class="no-data">No historical data available</div>';
      return;
    }
    const bl = data.baseline;
    const role = f.perspective;
    const isStretch = role === 'final_stretch';
    const isTrailing = role === 'trailing';

    // Populate season dropdown with available seasons on first load (#227)
    const seasonEl = document.getElementById('lev-hist-season');
    if (seasonEl && data.available_seasons) {
      const cur = seasonEl.value;
      // Keep static options, add/update dynamic year options
      while (seasonEl.options.length > 3) seasonEl.remove(3);
      for (const yr of data.available_seasons) {
        const opt = document.createElement('option');
        opt.value = yr; opt.textContent = yr;
        seasonEl.appendChild(opt);
      }
      seasonEl.value = cur;
    }

    // Filter note (#227)
    const hasFilters = f.season !== 'all' || f.segment !== 'all' || f.side !== 'all' || f.fav_role !== 'all' || f.margin_context !== 'all' || f.margin_bucket !== 'all';
    if (noteEl) {
      if (hasFilters) {
        const label = isStretch ? 'records' : 'games';
        noteEl.innerHTML = 'Filtered: <strong>' + data.total_filtered + '/' + data.total_unfiltered + '</strong> ' + label;
      } else {
        noteEl.innerHTML = data.total_filtered + (isStretch ? ' records' : ' games');
      }
    }

    // Baseline card
    const minsLabel = (data.minutes_evaluated || []).join(', ');
    const blLabel = isStretch
      ? 'Home team outscores in final stretch (min ' + minsLabel + '-80)'
      : isTrailing
        ? 'Trailing team at min ' + minsLabel + ' wins (comeback)'
        : 'Leading team at min ' + minsLabel + ' wins';
    const blColor = isStretch ? (bl.rate > 55 ? 'var(--green)' : bl.rate > 45 ? 'var(--muted)' : 'var(--red)')
      : isTrailing ? (bl.rate > 15 ? 'var(--red)' : 'var(--green)')
      : (bl.rate >= 80 ? 'var(--green)' : bl.rate >= 60 ? 'var(--yellow, orange)' : 'var(--red)');
    let bh = '<div style="padding:8px 14px;border:2px solid ' + blColor + ';border-radius:8px;background:var(--bg);display:inline-block">';
    bh += '<div style="font-size:0.78em;color:var(--muted)">' + blLabel + '</div>';
    bh += '<div style="font-size:1.3em;font-weight:700;color:' + blColor + '">' + bl.rate + '% <span style="font-size:0.6em;font-weight:400;color:var(--muted)">(' + bl.wins + '/' + bl.games + ')</span></div>';
    bh += '</div>';
    if (baselineEl) baselineEl.innerHTML = bh;

    let h = '';
    const rateColor = (r) => {
      if (isStretch) return r > 55 ? 'var(--green)' : r > 45 ? 'var(--muted)' : 'var(--red)';
      if (isTrailing) return r > 20 ? 'var(--red)' : r > 10 ? 'var(--yellow, orange)' : 'var(--green)';
      return r >= 90 ? 'var(--green)' : r >= 75 ? 'var(--yellow, orange)' : 'var(--red)';
    };
    const lcIcon = lc => lc ? ' <span title="Low confidence (<10 games)" style="cursor:help">⚠️</span>' : '';

    // === Notable Patterns ===
    const profs = data.profitable_patterns || [];
    const antis = data.anti_patterns || [];
    if (profs.length) {
      h += '<div style="border:1px solid var(--green);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:var(--bg)">';
      h += '<div style="font-size:0.78em;font-weight:600;color:var(--green);margin-bottom:4px">' + (isStretch ? 'High Outscoring Patterns' : isTrailing ? 'High Comeback Patterns' : 'Profitable Patterns') + '</div>';
      for (const p of profs.slice(0, 15)) {
        const delta = (p.rate - bl.rate).toFixed(1);
        h += '<div style="font-size:0.76em;margin:2px 0;color:var(--green)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp' + lcIcon(p.low_confidence) + '</div>';
      }
      h += '</div>';
    }
    if (antis.length) {
      h += '<div style="border:1px solid var(--red);border-radius:6px;padding:8px 12px;margin-bottom:8px;background:var(--bg)">';
      h += '<div style="font-size:0.78em;font-weight:600;color:var(--red);margin-bottom:4px">' + (isStretch ? 'Low Outscoring Patterns' : isTrailing ? 'Low Comeback Patterns' : 'Anti-Patterns') + '</div>';
      for (const p of antis.slice(0, 15)) {
        const delta = (p.rate - bl.rate).toFixed(1);
        h += '<div style="font-size:0.76em;margin:2px 0;color:var(--red)">' + esc(p.value1) + ' + ' + esc(p.value2) + ': <strong>' + p.rate + '%</strong> (' + p.wins + '/' + p.games + ') ' + (delta >= 0 ? '+' : '') + delta + 'pp' + lcIcon(p.low_confidence) + '</div>';
      }
      h += '</div>';
    }

    // === Dimensional Breakdowns ===
    const dims = data.dimensions || {};
    const dimNames = Object.keys(dims);
    if (dimNames.length) {
      h += '<div style="margin-top:10px"><div style="font-size:0.78em;font-weight:600;color:var(--nrl-green);margin-bottom:4px">Dimensional Breakdowns</div>';
      for (const dim of dimNames) {
        const label = dim.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
        const buckets = dims[dim];
        const bKeys = Object.keys(buckets).sort((a, b) => buckets[b].games - buckets[a].games);
        if (!bKeys.length) continue;
        h += '<details style="margin:4px 0"><summary style="cursor:pointer;font-size:0.76em;color:var(--muted)">' + esc(label) + ' (' + bKeys.length + ' buckets)</summary>';
        h += '<div style="overflow-x:auto;margin-top:4px"><table class="analysis-table"><thead><tr><th>Bucket</th><th>Games</th><th>Wins</th><th>Win Rate</th></tr></thead><tbody>';
        for (const bk of bKeys) {
          const b = buckets[bk];
          const rc = rateColor(b.rate);
          h += '<tr><td style="font-weight:600">' + esc(bk) + lcIcon(b.low_confidence) + '</td>';
          h += '<td style="text-align:center">' + b.games + '</td>';
          h += '<td style="text-align:center">' + b.wins + '</td>';
          h += '<td style="text-align:center;font-weight:600;color:' + rc + '">' + b.rate + '%</td></tr>';
        }
        h += '</tbody></table></div></details>';
      }
      h += '</div>';
    }

    // === 2D Combos ===
    const combos = data.combos || [];
    if (combos.length) {
      const top20 = combos.slice(0, 20);
      h += '<div style="margin-top:10px"><div style="font-size:0.78em;font-weight:600;color:var(--nrl-green);margin-bottom:4px">Top 2D Combos (by deviation from baseline)</div>';
      h += '<div style="overflow-x:auto"><table class="analysis-table"><thead><tr><th>Combo</th><th>Games</th><th>Wins</th><th>Win Rate</th><th>vs Baseline</th></tr></thead><tbody>';
      for (const c of top20) {
        const delta = (c.rate - bl.rate).toFixed(1);
        const dc = parseFloat(delta) >= 0 ? 'var(--green)' : 'var(--red)';
        h += '<tr><td style="font-size:0.76em;white-space:nowrap">' + esc(c.value1) + ' + ' + esc(c.value2) + lcIcon(c.low_confidence) + '</td>';
        h += '<td style="text-align:center">' + c.games + '</td><td style="text-align:center">' + c.wins + '</td>';
        h += '<td style="text-align:center;font-weight:600">' + c.rate + '%</td>';
        h += '<td style="text-align:center;color:' + dc + '">' + (delta >= 0 ? '+' : '') + delta + 'pp</td></tr>';
      }
      h += '</tbody></table></div></div>';
    }

    // === 5D Combo Table (#227) ===
    const c5d = data.combo_5d || [];
    if (c5d.length) {
      h += '<div style="margin-top:10px"><details><summary style="cursor:pointer;font-size:0.78em;font-weight:600;color:var(--nrl-green)">📊 5D Combo Table (' + c5d.length + ' combos)</summary>';
      h += '<div style="overflow-x:auto;max-height:400px;overflow-y:auto;margin-top:4px">';
      h += '<table class="analysis-table" id="lev-hist-5d-table"><thead><tr>';
      const hdrs = ['Side','Role','Context','Margin','Minute','Games','Wins','Rate','vs BL'];
      const sortKeys = ['side','role','margin_context','margin','entry_minute','games','wins','rate','_delta'];
      for (let i = 0; i < hdrs.length; i++) {
        h += '<th style="cursor:pointer;user-select:none" onclick="_levHist5dSort(\'' + sortKeys[i] + '\')">' + hdrs[i] + '</th>';
      }
      h += '</tr></thead><tbody>';
      // Store data for sorting
      window._levHist5dData = c5d.map(c => Object.assign({}, c, {_delta: Math.round((c.rate - bl.rate) * 10) / 10}));
      window._levHist5dBaseline = bl.rate;
      h += _levHist5dRows(window._levHist5dData, bl.rate);
      h += '</tbody></table></div></details></div>';
    }

    // === Game History (#227) ===
    const gl = data.game_list || [];
    const tp = data.total_pages || 1;
    const cp = data.page || 1;
    const tf = data.total_filtered || 0;
    h += '<div style="margin-top:10px"><details><summary style="cursor:pointer;font-size:0.78em;font-weight:600;color:var(--nrl-green)">📋 Game History (' + tf + (isStretch ? ' records' : ' games') + ')</summary>';
    // Pagination controls
    h += '<div style="display:flex;gap:8px;align-items:center;margin:6px 0;font-size:0.76em">';
    h += '<span style="color:var(--muted)">Page size: <select id="lev-hist-pagesize" onchange="_levHistFetch(1)" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:1px 3px;border-radius:3px;font-size:1em">';
    for (const sz of [25, 50, 100]) {
      h += '<option value="' + sz + '"' + (sz === f.page_size ? ' selected' : '') + '>' + sz + '</option>';
    }
    h += '</select></span>';
    h += '<span>';
    h += '<button class="btn" onclick="_levHistFetch(1)" style="padding:1px 5px;font-size:1em"' + (cp <= 1 ? ' disabled' : '') + '>◀</button> ';
    h += '<button class="btn" onclick="_levHistFetch(' + (cp - 1) + ')" style="padding:1px 5px;font-size:1em"' + (cp <= 1 ? ' disabled' : '') + '>◄</button> ';
    h += '<span style="color:var(--muted)">Page ' + cp + ' of ' + tp + '</span> ';
    h += '<button class="btn" onclick="_levHistFetch(' + (cp + 1) + ')" style="padding:1px 5px;font-size:1em"' + (cp >= tp ? ' disabled' : '') + '>►</button> ';
    h += '<button class="btn" onclick="_levHistFetch(' + tp + ')" style="padding:1px 5px;font-size:1em"' + (cp >= tp ? ' disabled' : '') + '>▶</button>';
    h += '</span></div>';
    // Table
    h += '<div style="overflow-x:auto"><table class="analysis-table"><thead><tr>';
    h += '<th>Date</th><th>Teams</th><th>Final Score</th><th>Final Margin</th><th>Team</th><th>Entry Min</th><th>Score@Entry</th><th>Margin@Entry</th><th>Won</th><th>Cond</th><th>Tags</th>';
    h += '</tr></thead><tbody>';
    for (const g of gl) {
      const wonC = g.won ? '' : 'opacity:0.5';
      const wonTxt = g.won ? '<span style="color:var(--green)">Y</span>' : '<span style="color:var(--red)">N</span>';
      const tags = (g.tags || []).join(', ') || '—';
      h += '<tr style="' + wonC + '">';
      h += '<td style="font-size:0.78em;white-space:nowrap">' + esc(g.date) + '</td>';
      h += '<td style="white-space:nowrap">' + esc(g.home_team) + ' v ' + esc(g.away_team) + '</td>';
      h += '<td style="text-align:center;font-weight:600">' + (g.home_score != null ? g.home_score + '-' + g.away_score : '—') + '</td>';
      h += '<td style="text-align:center">' + g.final_margin + '</td>';
      h += '<td style="font-weight:600">' + esc(g.team) + '</td>';
      h += '<td style="text-align:center">' + g.entry_minute + '</td>';
      h += '<td style="text-align:center">' + esc(g.score_at_entry) + '</td>';
      h += '<td style="text-align:center">' + g.margin_at_entry + '</td>';
      h += '<td style="text-align:center">' + wonTxt + '</td>';
      h += '<td style="text-align:center">' + g.conditions_count + '</td>';
      h += '<td style="font-size:0.74em">' + esc(tags) + '</td>';
      h += '</tr>';
    }
    if (!gl.length) h += '<tr><td colspan="11" style="text-align:center;color:var(--muted)">No games match filters</td></tr>';
    h += '</tbody></table></div></details></div>';

    contentEl.innerHTML = h || '<div class="no-data">No analysis data</div>';
  }).catch(e => {
    contentEl.innerHTML = '<div class="no-data" style="color:var(--red)">Error: ' + e + '</div>';
  });
}
```

- [ ] **Step 3: Add 5D table sort helper**

After `loadHistoricalWinRate()`, add:

```javascript
window._levHist5dSortCol = 'rate';
window._levHist5dSortAsc = false;

function _levHist5dRows(data, blRate) {
  let h = '';
  for (const c of data) {
    const delta = (c.rate - blRate).toFixed(1);
    const dc = parseFloat(delta) >= 0 ? 'var(--green)' : 'var(--red)';
    const lc = c.low_confidence ? ' <span title="Low confidence" style="cursor:help">⚠️</span>' : '';
    h += '<tr>';
    h += '<td style="cursor:pointer;text-decoration:underline dotted" onclick="_levHistSetFilter(\'side\',\'' + c.side + '\')">' + esc(c.side) + '</td>';
    h += '<td style="cursor:pointer;text-decoration:underline dotted" onclick="_levHistSetFilter(\'role\',\'' + c.role + '\')">' + esc(c.role) + '</td>';
    h += '<td style="cursor:pointer;text-decoration:underline dotted" onclick="_levHistSetFilter(\'margin_context\',\'' + c.margin_context + '\')">' + esc(c.margin_context) + '</td>';
    h += '<td style="cursor:pointer;text-decoration:underline dotted" onclick="_levHistSetFilter(\'margin\',\'' + c.margin + '\')">' + esc(c.margin) + '</td>';
    h += '<td style="text-align:center">' + c.entry_minute + '</td>';
    h += '<td style="text-align:center">' + c.games + lc + '</td>';
    h += '<td style="text-align:center">' + c.wins + '</td>';
    h += '<td style="text-align:center;font-weight:600">' + c.rate + '%</td>';
    h += '<td style="text-align:center;color:' + dc + '">' + (delta >= 0 ? '+' : '') + delta + 'pp</td>';
    h += '</tr>';
  }
  return h;
}

function _levHist5dSort(col) {
  if (window._levHist5dSortCol === col) {
    window._levHist5dSortAsc = !window._levHist5dSortAsc;
  } else {
    window._levHist5dSortCol = col;
    window._levHist5dSortAsc = (col === 'side' || col === 'role' || col === 'margin_context' || col === 'margin' || col === 'entry_minute');
  }
  const data = window._levHist5dData || [];
  const asc = window._levHist5dSortAsc;
  data.sort((a, b) => {
    let va = a[col], vb = b[col];
    if (typeof va === 'string') return asc ? va.localeCompare(vb) : vb.localeCompare(va);
    return asc ? va - vb : vb - va;
  });
  const tbody = document.querySelector('#lev-hist-5d-table tbody');
  if (tbody) tbody.innerHTML = _levHist5dRows(data, window._levHist5dBaseline || 50);
}
```

- [ ] **Step 4: Remove old localStorage keys**

In `_levInitView()`, clean up old keys that are no longer used:

```javascript
  // Clean up old localStorage keys from #226
  try { localStorage.removeItem('lev_hist_minute'); localStorage.removeItem('lev_hist_role'); } catch(e) {}
```

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: loadHistoricalWinRate rewrite — filters, 5D combo table, game list with paging (#227)"
```

---

### Task 5: Testing + CHANGES.md

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Restart dashboard and test endpoints**

```bash
systemctl --user restart nrl-dashboard.service
sleep 1

# Basic — backwards compat
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minute=70' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Compat: {d[\"baseline\"][\"rate\"]}% ({d[\"baseline\"][\"games\"]}g)')
assert 'combo_5d' in d and 'game_list' in d
assert d['available_seasons']
print('PASS')
"

# Multi-minute
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=68,70,72' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Multi: {d[\"baseline\"][\"games\"]}g, mins={d[\"minutes_evaluated\"]}')
assert len(d['minutes_evaluated']) == 3
assert 'entry_minute' in d['dimensions']
print('PASS')
"

# Season filter
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=70&season=2025' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'2025: {d[\"baseline\"][\"games\"]}g, filtered={d[\"total_filtered\"]}/{d[\"total_unfiltered\"]}')
print('PASS')
"

# Side filter
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=70&side=home' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Home: {d[\"total_filtered\"]}/{d[\"total_unfiltered\"]}')
print('PASS')
"

# Pagination
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minutes=70&page=2&page_size=50' | python3 -c "
import json,sys; d=json.load(sys.stdin)
print(f'Page {d[\"page\"]}/{d[\"total_pages\"]}, list={len(d[\"game_list\"])}')
assert d['page'] == 2
print('PASS')
"
```

- [ ] **Step 2: Test in browser**

1. Load `http://127.0.0.1:8898/dashboard.html`
2. Navigate to Spread Strategy → Historical Win Rate
3. Verify 7 filter dropdowns + minute checkboxes + Reset button appear
4. Change Season to "2025" — baseline should update, filter note shows "Filtered: X/Y"
5. Change Side to "Home" — further narrows
6. Click Reset — all filters clear, full data returns
7. Check multiple minutes (68 + 70 + 72) — should show entry_minute in dimensional breakdown
8. Scroll to 5D Combo Table — verify sortable columns, click-through filters
9. Scroll to Game History — verify pagination works, page size selector, won/lost styling
10. Switch to Final Stretch — verify dual-record display
11. Refresh page — filters should persist from localStorage

- [ ] **Step 3: Update CHANGES.md**

Add entry at top:

```markdown
## 2026-07-01 — Historical Win Rate: filters, 5D combo table, game list (#227)

- **`odds_monitor.py`**: Extended `compute_historical_win_analysis()` with server-side filters (season, segment, side, role, margin context, margin bucket), multi-minute evaluation (entry minute becomes a dimension), 5D combo table computation, and paginated game list output. In-memory caching by (role, minutes, season, segment).
- **`server.py`**: Extended `/api/odds-monitor-historical` with filter + pagination query params. Strict minutes validation to {65,68,70,72,75}. Backwards compatible with old `minute=` param.
- **`dashboard.html`**: Replaced historical view controls with full filter bar (7 dropdowns + minute checkboxes + Reset). Added 5D combo table (sortable, click-through to filter). Added paginated game history list (25/50/100 page sizes, First/Prev/Next/Last navigation). All filters persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`
```

- [ ] **Step 4: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for Historical Win Rate filters (#227)"
git push
```

- [ ] **Step 5: Close issue**

```bash
gh issue comment 227 --body "Implementation complete. Filters, 5D combo table, and game list with paging all working. See CHANGES.md for details."
gh issue close 227
```
