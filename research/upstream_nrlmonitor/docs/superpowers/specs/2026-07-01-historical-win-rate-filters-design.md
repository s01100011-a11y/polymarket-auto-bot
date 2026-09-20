# Historical Win Rate — Filters, 5D Combo Table, Game List

**Issue:** NRL#227
**Date:** 2026-07-01
**Status:** Design approved
**Parent:** NRL#226 (Historical Win Rate sub-view, closed)

> **Note:** "pp" means **percentage points** — the absolute difference between two percentages.

## Overview

Enhance the Spread Strategy → Historical Win Rate sub-view with server-side dropdown filters that narrow the entire analysis, a filterable/sortable 5D combo table, and a paginated game history list matching the BK history table format.

## Backend

### Filter Parameters

`compute_historical_win_analysis()` signature extended:

```python
def compute_historical_win_analysis(game_history, role="leading", target_minute=70,
                                     minutes=None, season=None, segment=None,
                                     side=None, fav_role=None, margin_context=None,
                                     margin_bucket=None, page=1, page_size=25):
```

| Param | Type | Values | Default | Notes |
|-------|------|--------|---------|-------|
| `role` | str | leading/trailing/final_stretch | leading | Perspective (existing) |
| `target_minute` | int | 65-75 | 70 | Fallback when `minutes` is None (existing) |
| `minutes` | list[int] | subset of [65,68,70,72,75] | None (uses target_minute) | Multi-minute: each game evaluated at each minute. Entry minute becomes a dimension. |
| `season` | str | all/last3/last5/YYYY | all | Dynamic from game_history |
| `segment` | str | all/regular_season/finals/state_of_origin | all | From `season_segment` field |
| `side` | str | all/home/away | all | Side of analysed team at target minute |
| `fav_role` | str | all/favourite/underdog | all | Pregame role. Unknown games excluded when specific value selected. |
| `margin_context` | str | all/leading/trailing | all | Whether analysed team was leading/trailing at target minute |
| `margin_bucket` | str | all/0-6/7-12/13-20/21+ | all | Absolute margin at target minute |
| `page` | int | ≥1 | 1 | Game list page number |
| `page_size` | int | 25/50/100 | 25 | Game list page size |

### Processing Flow

```
1. Load game_history
2. Filter by season/segment (pre-PBP, reduces cache lookups)
3. For each game, for each selected minute:
   a. Resolve PBP cache, derive score at minute
   b. Determine perspective (leading/trailing/final_stretch)
   c. Enrich with features (side, role, margin, halftime context, etc.)
   d. Create enriched record
4. Apply post-enrichment filters: side, fav_role, margin_context, margin_bucket
5. Compute from filtered records:
   - Baseline stats
   - Dimensional breakdowns (existing 11 dims + entry_minute when multi-minute)
   - 2D combos + pattern detection
   - 5D combo table
   - Paginated game list (sorted by kickoff_utc desc, match_id asc, entry_minute asc)
6. Return response with all sections + metadata
```

### Multi-Minute Behaviour

When `minutes=[68, 70, 72]`:
- Each game is evaluated at each minute, producing up to 3 records per game
- Records where the score is tied at a given minute are excluded (as before)
- `entry_minute` becomes a dimension in the breakdown with individual values: "68", "70", "72"
- The `_entry_minute` field is added to each enriched record
- Game list shows the entry minute per row (same game may appear multiple times)

### 5D Combo Computation

Five dimensions: side, role (fav/underdog/unknown), margin_context (leading/trailing), margin (0-6/7-12/13-20/21+), entry_minute.

```python
# Iterate all present 5D combinations from filtered records
combo_5d_buckets = defaultdict(lambda: {"games": 0, "wins": 0})
for rec in filtered_games:
    key = (rec["_side"], rec["_role"], rec["_margin_context"],
           _hist_bucket(rec["_margin"], _MARGIN_RANGES), str(rec["_entry_minute"]))
    combo_5d_buckets[key]["games"] += 1
    if rec["_won"]:
        combo_5d_buckets[key]["wins"] += 1

# Filter to ≥3 games, compute rate
combo_5d = []
for (side, role, ctx, margin, minute), val in combo_5d_buckets.items():
    if val["games"] < 3:
        continue
    rate = round(val["wins"] / val["games"] * 100, 1)
    combo_5d.append({
        "side": side, "role": role, "margin_context": ctx,
        "margin": margin, "entry_minute": minute,
        "games": val["games"], "wins": val["wins"],
        "rate": rate, "low_confidence": val["games"] < 10,
    })
combo_5d.sort(key=lambda c: abs(c["rate"] - baseline_rate), reverse=True)
```

### Game List Records

Each record in `game_list`:

```python
{
    "date": "2026-06-28",        # from kickoff_utc or ts
    "home_team": "Panthers",
    "away_team": "Storm",
    "home_score": 24,            # final
    "away_score": 18,            # final
    "final_margin": 6,           # absolute
    "team": "Panthers",          # analysed team
    "entry_minute": 70,
    "score_at_entry": "18-12",   # home-away at entry minute
    "margin_at_entry": 6,        # absolute
    "won": true,
    "conditions_count": 14,
    "tags": ["CLOSE"],
}
```

Sorted by date descending. Paginated server-side via `page` and `page_size`.

### Response Shape

```json
{
  "baseline": {"games": 342, "wins": 318, "rate": 93.0},
  "dimensions": {"side": {...}, "role": {...}, ...},
  "combos": [...],
  "profitable_patterns": [...],
  "anti_patterns": [...],
  "combo_5d": [
    {"side": "home", "role": "favourite", "margin_context": "leading",
     "margin": "0-6", "entry_minute": "70",
     "games": 15, "wins": 8, "rate": 53.3, "low_confidence": false}
  ],
  "game_list": [...],
  "page": 1,
  "page_size": 25,
  "total_pages": 14,
  "total_filtered": 342,
  "total_unfiltered": 870,
  "total_games": 1030,
  "tied_at_minute": 57,
  "draws": 3,
  "cache_misses": 100,
  "role": "leading",
  "target_minute": 70,
  "minutes_evaluated": [70],
  "available_seasons": ["2022", "2023", "2024", "2025", "2026"]
}
```

## API

### Endpoint

`GET /api/odds-monitor-historical`

Extended query params (all existing params preserved):

| Param | Example | Notes |
|-------|---------|-------|
| `role` | `leading` | Existing |
| `minutes` | `68,70,72` | Comma-separated. Replaces `minute` param. Default: `70` |
| `season` | `last3` or `2025` | New |
| `segment` | `regular_season` | New |
| `side` | `home` | New |
| `fav_role` | `favourite` | New |
| `margin_context` | `leading` | New |
| `margin_bucket` | `0-6` | New |
| `page` | `2` | New |
| `page_size` | `50` | New |

The old `minute` param is still accepted for backwards compatibility — treated as `minutes=<value>`.

### Server Implementation

```python
if path == "/api/odds-monitor-historical":
    params = parse_qs(parsed.query)
    role = _param(params, "role", "leading")
    # Parse minutes (comma-separated) or fallback to minute param
    minutes_raw = _param(params, "minutes", "")
    if minutes_raw:
        minutes = [int(m) for m in minutes_raw.split(",") if m.strip().isdigit()]
    else:
        minutes = [int(_param(params, "minute", "70"))]
    # Validate to allowed set only
    ALLOWED_MINUTES = {65, 68, 70, 72, 75}
    minutes = [m for m in minutes if m in ALLOWED_MINUTES] or [70]
    season = _param(params, "season", "all")
    segment = _param(params, "segment", "all")
    side = _param(params, "side", "all")
    fav_role = _param(params, "fav_role", "all")
    margin_context = _param(params, "margin_context", "all")
    margin_bucket = _param(params, "margin_bucket", "all")
    page = max(1, int(_param(params, "page", "1")))
    page_size = min(100, max(25, int(_param(params, "page_size", "25"))))

    game_hist = _load_history()
    result = odds_monitor.compute_historical_win_analysis(
        game_hist, role=role, minutes=minutes,
        season=season, segment=segment, side=side,
        fav_role=fav_role, margin_context=margin_context,
        margin_bucket=margin_bucket, page=page, page_size=page_size)
    self.send_json(200, result)
```

## Dashboard

### Layout

```
Historical Win Rate view
├── Filter bar (flex row, wrapping)
│   ├── Perspective: [Leading Team ▾]
│   ├── Season: [All ▾]          ← dynamic options from available_seasons
│   ├── Segment: [All ▾]
│   ├── Side: [All ▾]
│   ├── Role: [All ▾]
│   ├── Context: [All ▾]         ← margin context: leading/trailing
│   ├── Margin: [All ▾]
│   ├── Minutes: ☑65 ☑68 ☑70 ☑72 ☑75   ← checkboxes, default 70 only
│   ├── [Reset]
│   └── Filter note: "Filtered: 342/870 games"
│
├── Baseline stats card
├── Notable Patterns (profitable + anti)
├── Dimensional Breakdowns (collapsible details)
├── Top 2D Combos table
│
├── 📊 5D Combo Table (collapsible details)
│   ├── Headers: Side | Role | Context | Margin | Minute | Games | Wins | Rate | vs BL
│   ├── Sortable by any column (default: abs deviation desc)
│   ├── Click dimension cell value → sets corresponding filter dropdown
│   ├── ≥3 games to appear, ⚠️ for <10
│   └── Scrollable, max-height container
│
└── 📋 Game History (collapsible details)
    ├── Headers: Date | Teams | Final Score | Final Margin | Team | Entry Min | Score@Entry | Margin@Entry | Won | Conditions | Tags
    ├── Won rows normal, lost rows opacity:0.5
    ├── Page size: [25] [50] [100] selector
    └── Navigation: ◀ First | ◄ Prev | Page X of Y | Next ► | Last ▶
```

### Filter Behaviour

- All filter changes trigger a single `fetch()` to `/api/odds-monitor-historical` with all current filter values + `page=1` (reset to first page)
- Page navigation fetches with same filters + new page number
- Filters persisted to `localStorage['lev_hist_filters']` as JSON object
- Reset button: clears all filters to defaults, clears localStorage, re-fetches
- On tab init (`_levInitView`): restore filters from localStorage before first fetch

### Season Dropdown

Dynamic options built from `available_seasons` in API response:

```html
<option value="all">All Seasons</option>
<option value="last3">Last 3 Seasons</option>
<option value="last5">Last 5 Seasons</option>
<option value="2026">2026</option>
<option value="2025">2025</option>
<option value="2024">2024</option>
<option value="2023">2023</option>
<option value="2022">2022</option>
```

Season options populated on first API response. Subsequent fetches don't rebuild the dropdown unless `available_seasons` changes.

### Entry Minute Checkboxes

```html
<span style="...">Min:
  <label><input type="checkbox" id="lev-min-65" value="65"> 65</label>
  <label><input type="checkbox" id="lev-min-68" value="68"> 68</label>
  <label><input type="checkbox" id="lev-min-70" value="70" checked> 70</label>
  <label><input type="checkbox" id="lev-min-72" value="72"> 72</label>
  <label><input type="checkbox" id="lev-min-75" value="75"> 75</label>
</span>
```

If no checkboxes selected, defaults to 70.

### 5D Combo Table

```html
<details>
  <summary>📊 5D Combo Table (N combos)</summary>
  <table class="analysis-table">
    <thead><tr>
      <th onclick="sort">Side</th>
      <th onclick="sort">Role</th>
      <th onclick="sort">Context</th>
      <th onclick="sort">Margin</th>
      <th onclick="sort">Minute</th>
      <th onclick="sort">Games</th>
      <th onclick="sort">Wins</th>
      <th onclick="sort">Rate</th>
      <th onclick="sort">vs BL</th>
    </tr></thead>
    <tbody>...</tbody>
  </table>
</details>
```

- Clicking a dimension cell value (e.g., clicking "home" in the Side column) sets the corresponding filter dropdown to that value and re-fetches
- Sort state tracked in JS (column + direction), toggles asc/desc on click
- vs BL column shows `+X.Xpp` / `-X.Xpp` with green/red colouring

### Game History Table

```html
<details>
  <summary>📋 Game History (X games)</summary>
  <div>
    <span>Page size: <select onchange="..."><option>25</option><option>50</option><option>100</option></select></span>
    <span>◀ First | ◄ Prev | Page 1 of 35 | Next ► | Last ▶</span>
  </div>
  <table class="analysis-table">
    <thead><tr>
      <th>Date</th><th>Teams</th><th>Final Score</th><th>Final Margin</th>
      <th>Team</th><th>Entry Min</th><th>Score@Entry</th><th>Margin@Entry</th>
      <th>Won</th><th>Conditions</th><th>Tags</th>
    </tr></thead>
    <tbody>...</tbody>
  </table>
</details>
```

- Won rows: normal styling
- Lost rows: `opacity: 0.5`
- Date column: `YYYY-MM-DD` format
- Tags: comma-separated, or "—" if none
- Conditions: integer count

### localStorage

Single key `lev_hist_filters` stores all filter state:

```json
{
  "perspective": "leading",
  "season": "all",
  "segment": "all",
  "side": "all",
  "fav_role": "all",
  "margin_context": "all",
  "margin_bucket": "all",
  "minutes": [70],
  "page_size": 25
}
```

Page number is NOT persisted (always starts at page 1 on load).

### Fetch Flow

```
Any filter change or perspective change:
  → gather all filter values from dropdowns/checkboxes
  → persist to localStorage
  → fetch /api/odds-monitor-historical?role=...&minutes=...&season=...&page=1&page_size=...
  → render: baseline, patterns, dims, 2D combos, 5D table, game list, pagination

Page navigation click:
  → fetch with same filters + new page number
  → re-render game list + pagination only (keep analysis sections)

Page size change:
  → persist page_size to localStorage
  → fetch with page=1 + new page_size
  → re-render game list + pagination
```

## Files Changed

| File | Changes |
|------|---------|
| `odds_monitor.py` | Extend `compute_historical_win_analysis()`: season/segment pre-filter, multi-minute loop, post-enrichment filters (side, fav_role, margin_context, margin_bucket), 5D combo computation, game list with pagination. Add `_entry_minute` and `_margin_context` to enriched records. |
| `server.py` | Extend `/api/odds-monitor-historical` handler: parse new filter params + pagination, pass to function. Backwards-compatible with old `minute` param. |
| `dashboard.html` | Replace current historical view controls (perspective + minute dropdowns) with full filter bar. Add 5D combo table rendering with sort. Add game history table with pagination. Update `loadHistoricalWinRate()` to send all filters. Add `_levHistSort()`, `_levHistPage()` helpers. Update localStorage to `lev_hist_filters`. Update `_levInitView()` to restore filters. |

## Design Clarifications (from Copilot review)

### Minutes Validation
Server validates minutes strictly to `{65, 68, 70, 72, 75}`. Invalid values silently dropped. Empty set falls back to `[70]`.

### Entry Minute Representation
Individual minute values only (not ranges/buckets). Each selected minute appears as its own value in the entry_minute dimension and 5D combo table.

### "Filtered: X/Y" Denominator
- `X` = `total_filtered` = records after ALL filters (season, segment, side, role, margin_context, margin_bucket)
- `Y` = `total_unfiltered` = records from same perspective + minutes with NO dropdown filters applied
- Response includes both: `"total_filtered": 342, "total_unfiltered": 870`
- UI shows: "Filtered: 342/870 games" (or "870 games" when no filters active)

### Final Stretch Record Semantics
In `final_stretch` mode:
- Each game produces 2 records (home + away perspective)
- `baseline.games`, `total_filtered`, pagination counts all refer to **records** (not unique games)
- Dimensional totals sum to records, not games
- Game list may show the same match twice (once per team perspective)
- UI label: "Filtered: 1,470 records (735 games)" — show both counts

### Sort Tie-Breakers
Game list sort order: `kickoff_utc desc`, `match_id asc`, `entry_minute asc` — deterministic across page fetches.

### Role "Unknown" Handling
- Role filter = `all`: unknown games included in all outputs (5D table, dimensions, game list)
- Role filter = `favourite` or `underdog`: unknown games excluded
- This is consistent with investigation findings where 57% of games lack role data

### Caching
In-memory cache of enriched records keyed by `(role, minutes_tuple, season, segment)`. Post-enrichment filters (side, fav_role, margin_context, margin_bucket) and pagination applied on cached rows. Cache invalidated when `game_history.json` mtime changes.

### Acceptance Criteria
- [ ] Backward compatibility: `minute=70` still works as `minutes=70`
- [ ] Filter correctness: each dropdown changes baseline/dimensions/combos/5D/game_list consistently
- [ ] Multi-minute: same game appears once per selected minute (excluding tied-at-minute)
- [ ] Pagination: `total_filtered`, `total_pages`, page boundaries stable across fetches
- [ ] Final-stretch: dual-record behavior validated end-to-end
- [ ] 5D combo click-through: clicking cell value sets filter and re-fetches
- [ ] localStorage persistence: all filters restored on tab re-init

## Out of Scope

- Client-side filtering (all server-side per decision)
- Export/download of game list
- Cross-linking game list rows to Game History tab detail view
- Inline editing of filters in 5D combo table headers
- Chart visualisations of dimensional data
