# Spread Strategy: Historical Win Rate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Historical Win Rate" sub-view to the Spread Strategy tab that analyses whether the leading/trailing team at minute 70 wins the game, plus a "Final Stretch" mode that analyses who outscores whom in the last 10 minutes (score reset to 0-0 at target minute), using 870+ games from game_history + PBP cache. Also enrich the existing BK spread analysis with 5 new dimensions.

**Architecture:** New `compute_historical_win_analysis()` function in `odds_monitor.py` processes game_history records cross-referenced with PBP cache timeline data to derive score at a target minute. Supports three roles: "leading" (win rate), "trailing" (comeback rate), "final_stretch" (who outscores in last 10 min, independent of current lead). New `/api/odds-monitor-historical` endpoint in `server.py`. Dashboard gets a view toggle between BK Spread Coverage and Historical Win Rate, with the historical view showing dimensional breakdowns, patterns, and 2D combos using win/loss metrics instead of P&L/ROI.

**Tech Stack:** Python 3 (stdlib only), vanilla JS, existing dashboard HTML/CSS patterns.

**Spec:** `docs/superpowers/specs/2026-06-30-spread-strategy-historical-win-rate-design.md`

---

### Task 1: PBP Cache Helpers in odds_monitor.py

**Files:**
- Modify: `odds_monitor.py:10-18` (imports and constants)

- [ ] **Step 1: Add imports and constants**

Add `re` import and `NRL_CACHE_DIR` constant after existing constants:

```python
# At line 10, add re to imports:
import re

# After line 18 (GAME_HISTORY_FILE), add:
NRL_CACHE_DIR = os.path.join(SCRIPT_DIR, "nrl_cache")
```

- [ ] **Step 2: Add `match_id_to_cache_path()` function**

Add after `load_history()` (after line 1175):

```python
# ── PBP cache helpers (#226) ────────────────────────────────────────────────

SCORE_POINTS = {"Try": 4, "Goal": 2, "Penalty Goal": 2, "Field Goal": 1}


def match_id_to_cache_path(match_id):
    """Convert game_history match_id to nrl_cache file path.

    Example: /draw/nrl-premiership/2026/round-17/sea-eagles-v-storm/
          -> nrl_cache/2026/match_sea-eagles-v-storm_r17.json
    """
    parts = match_id.strip("/").split("/")
    if len(parts) < 5:
        return None
    year, round_str, teams = parts[2], parts[3], parts[4]
    m = re.search(r"round-(\d+)", round_str)
    if not m:
        return None
    path = os.path.join(NRL_CACHE_DIR, year, f"match_{teams}_r{m.group(1)}.json")
    return path if os.path.exists(path) else None
```

- [ ] **Step 3: Add `score_at_minute()` function**

Add directly after `match_id_to_cache_path()`:

```python
def score_at_minute(cache_path, target_minute=70):
    """Derive home/away score at a game minute from PBP timeline.

    Walks timeline scoring events (Try=4, Goal=2, Penalty Goal=2, Field Goal=1)
    and accumulates points up to target_minute * 60 gameSeconds.

    Returns dict with home_score, away_score, margin (home - away),
    home_id, away_id, or None on failure.
    """
    try:
        with open(cache_path, "r") as f:
            data = json.load(f)
    except Exception:
        return None
    timeline = data.get("timeline", [])
    home_id = (data.get("homeTeam") or {}).get("teamId")
    away_id = (data.get("awayTeam") or {}).get("teamId")
    if not home_id or not away_id or not timeline:
        return None
    target_seconds = target_minute * 60
    hs, aws = 0, 0
    for ev in timeline:
        if ev.get("gameSeconds", 0) > target_seconds:
            break
        pts = SCORE_POINTS.get(ev.get("type", ""), 0)
        if pts:
            tid = ev.get("teamId")
            if tid == home_id:
                hs += pts
            elif tid == away_id:
                aws += pts
    return {"home_score": hs, "away_score": aws, "margin": hs - aws,
            "home_id": home_id, "away_id": away_id}
```

- [ ] **Step 4: Test PBP helpers manually**

Run:
```bash
cd /home/bobcheong/.openclaw/workspace/scripts/nrl-monitor
python3 -c "
import odds_monitor as om
# Test match_id_to_cache_path
p = om.match_id_to_cache_path('/draw/nrl-premiership/2026/round-1/broncos-v-panthers/')
print('Cache path:', p)
assert p is not None, 'Should find cache file'
# Test score_at_minute
s = om.score_at_minute(p, 70)
print('Score at min 70:', s)
assert s is not None, 'Should derive score'
assert 'home_score' in s and 'away_score' in s and 'margin' in s
# Test bad match_id
assert om.match_id_to_cache_path('/bad/path/') is None
print('PASS: PBP helpers')
"
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: PBP cache helpers — match_id_to_cache_path + score_at_minute (#226)"
```

---

### Task 2: `compute_historical_win_analysis()` Function

**Files:**
- Modify: `odds_monitor.py` (add after PBP helpers from Task 1)

- [ ] **Step 1: Add the bucketing helper**

Add after `score_at_minute()`:

```python
def _hist_bucket(value, ranges):
    """Bucket a numeric value. ranges: list of (upper_bound, label); last upper=None."""
    for upper, label in ranges:
        if upper is None or value <= upper:
            return label
    return ranges[-1][1]


_MARGIN_RANGES = [(6, "0-6"), (12, "7-12"), (20, "13-20"), (None, "21+")]
_HT_MARGIN_RANGES = [(3, "0-3"), (6, "4-6"), (12, "7-12"), (None, "13+")]
_TOTAL_RANGES = [(30, "0-30"), (40, "31-40"), (50, "41-50"), (None, "51+")]
_SPREAD_RANGES = [(3.5, "1-3.5"), (7.5, "4-7.5"), (13.5, "8-13.5"), (None, "14+")]
```

- [ ] **Step 2: Add `compute_historical_win_analysis()` — game enrichment**

```python
def compute_historical_win_analysis(game_history, role="leading", target_minute=70):
    """Analyse win rate of leading/trailing team at target_minute using PBP cache.

    Args:
        game_history: list of game records from game_history.json
        role: "leading" (win rate), "trailing" (comeback rate),
              or "final_stretch" (who outscores in last 10 min — score reset)
        target_minute: game minute to evaluate score (default 70)

    Returns dict with baseline, dimensions, combos, patterns, metadata.
    """
    from collections import defaultdict

    games = []  # enriched records for analysis
    tied = 0
    draws = 0
    cache_misses = 0
    is_final_stretch = role == "final_stretch"

    for g in game_history:
        cp = match_id_to_cache_path(g.get("match_id", ""))
        if not cp:
            cache_misses += 1
            continue
        s = score_at_minute(cp, target_minute)
        if not s:
            cache_misses += 1
            continue
        home_final = g.get("home_score")
        away_final = g.get("away_score")
        if home_final is None or away_final is None:
            continue
        if home_final == away_final:
            draws += 1
            continue

        if is_final_stretch:
            # Final stretch: who outscores in last 10 min?
            # Score from target_minute to full time
            home_stretch = home_final - s["home_score"]
            away_stretch = away_final - s["away_score"]
            if home_stretch == away_stretch:
                tied += 1  # even scoring in final stretch
                continue
            # Home team perspective — "won" means home outscored away in stretch
            side = "home"
            won = home_stretch > away_stretch
            stretch_margin = home_stretch - away_stretch
            margin = s["margin"]  # needed for halftime context below
            rec = {"_won": won, "_side": side, "_margin": abs(stretch_margin),
                   "_home_stretch": home_stretch, "_away_stretch": away_stretch}
            rec["_game"] = g
        else:
            margin = s["margin"]  # home - away at target minute
            if margin == 0:
                tied += 1
                continue
            # Determine perspective
            if role == "trailing":
                side = "away" if margin > 0 else "home"
                won = (away_final > home_final) if margin > 0 else (home_final > away_final)
            else:
                side = "home" if margin > 0 else "away"
                won = (home_final > away_final) if margin > 0 else (away_final > home_final)
            rec = {"_won": won, "_side": side, "_margin": abs(margin)}
            rec["_game"] = g

        # Role (fav/underdog) from pregame odds or spread
        ho = g.get("home_odds")
        ao = g.get("away_odds")
        hs = g.get("home_spread")
        side = rec["_side"]
        if ho and ao:
            rec["_role"] = "favourite" if (ho < ao if side == "home" else ao < ho) else "underdog"
        elif hs is not None:
            rec["_role"] = "favourite" if (hs < 0 if side == "home" else hs > 0) else "underdog"
        else:
            rec["_role"] = "unknown"

        # Halftime context
        htm = g.get("halftime_margin")
        if htm is not None:
            ht_lead = htm if side == "home" else -htm
            ht_margin_bucket = _hist_bucket(abs(ht_lead), _HT_MARGIN_RANGES)
            if ht_lead > 0:
                rec["_ht_context"] = f"{ht_margin_bucket} (leading@HT)"
            elif ht_lead < 0:
                rec["_ht_context"] = f"{ht_margin_bucket} (trailing@HT)"
            else:
                rec["_ht_context"] = f"{ht_margin_bucket} (tied@HT)"
            # Halftime position for combos
            rec["_ht_position"] = "leading@HT" if ht_lead > 0 else ("trailing@HT" if ht_lead < 0 else "tied@HT")
            # Margin trajectory
            lead_at_min = abs(margin)
            diff = lead_at_min - abs(ht_lead) if ht_lead > 0 else lead_at_min + ht_lead
            # Simpler: compute from perspective of the team's lead
            lead_now = rec["_margin"]
            lead_ht = ht_lead
            trajectory_diff = lead_now - lead_ht
            if trajectory_diff > 4:
                rec["_trajectory"] = "lead_growing (>4pts)"
            elif trajectory_diff > 0:
                rec["_trajectory"] = "lead_growing (1-4pts)"
            elif trajectory_diff == 0:
                rec["_trajectory"] = "lead_stable"
            elif trajectory_diff > -4:
                rec["_trajectory"] = "lead_shrinking (1-4pts)"
            else:
                rec["_trajectory"] = "lead_shrinking (>4pts)"

        # Key conditions
        cond_types = set()
        for c in g.get("conditions_fired", []):
            ct = c.get("type", "")
            if ct in ("halftime_turnaround", "momentum_shift"):
                cond_types.add(ct)
        if "halftime_turnaround" in cond_types:
            rec["_key_cond"] = "has_halftime_turnaround"
        elif "momentum_shift" in cond_types:
            rec["_key_cond"] = "has_momentum_shift"
        else:
            rec["_key_cond"] = "neither"
        # Individual flags for combos
        rec["_has_ht_turnaround"] = "yes" if "halftime_turnaround" in cond_types else "no"
        rec["_has_momentum_shift"] = "yes" if "momentum_shift" in cond_types else "no"

        # Postgame tags
        tags = g.get("postgame_tags", [])
        rec["_tags"] = tags if tags else ["no_tags"]

        # Total points
        rec["_total"] = (home_final or 0) + (away_final or 0)

        # Team — for final_stretch, use home team (away is the opponent)
        rec["_team"] = g.get("home_team") if side == "home" else g.get("away_team")

        # Competition
        rec["_competition"] = g.get("season_segment", "regular_season")

        # Spread line size (optional)
        rec["_spread"] = abs(hs) if hs is not None else None

        games.append(rec)

        # Final stretch: also add away team record for per-team analysis
        if is_final_stretch:
            rec_away = dict(rec)
            rec_away["_won"] = not won
            rec_away["_side"] = "away"
            rec_away["_team"] = g.get("away_team")
            # Role flipped for away
            if ho and ao:
                rec_away["_role"] = "favourite" if ao < ho else "underdog"
            elif hs is not None:
                rec_away["_role"] = "favourite" if hs > 0 else "underdog"
            else:
                rec_away["_role"] = "unknown"
            games.append(rec_away)

    if not games:
        return {"baseline": {"games": 0, "wins": 0, "rate": 0},
                "dimensions": {}, "combos": [], "profitable_patterns": [],
                "anti_patterns": [], "total_games": len(game_history),
                "tied_at_minute": tied, "draws": draws,
                "cache_misses": cache_misses, "role": role,
                "target_minute": target_minute}

    total_wins = sum(1 for g in games if g["_won"])
    baseline_rate = round(total_wins / len(games) * 100, 1)

    # ── Dimensional breakdown ────────────────────────────────────────────
    def _dim_stats(values):
        """values: list of (bucket_key, won_bool). Returns {key: {games, wins, rate, low_confidence}}."""
        buckets = defaultdict(lambda: {"games": 0, "wins": 0})
        for key, won in values:
            buckets[key]["games"] += 1
            if won:
                buckets[key]["wins"] += 1
        result = {}
        for key, val in buckets.items():
            val["rate"] = round(val["wins"] / val["games"] * 100, 1) if val["games"] else 0
            val["low_confidence"] = val["games"] < 10
            result[key] = val
        return result

    dims = {}
    dims["side"] = _dim_stats([(g["_side"], g["_won"]) for g in games])
    dims["role"] = _dim_stats([(g["_role"], g["_won"]) for g in games])
    dims["margin_at_" + str(target_minute)] = _dim_stats([
        (_hist_bucket(g["_margin"], _MARGIN_RANGES), g["_won"]) for g in games])
    dims["total_points"] = _dim_stats([
        (_hist_bucket(g["_total"], _TOTAL_RANGES), g["_won"]) for g in games])
    dims["team"] = _dim_stats([(g["_team"], g["_won"]) for g in games if g["_team"]])
    dims["competition"] = _dim_stats([(g["_competition"], g["_won"]) for g in games])

    # Halftime context
    ht_vals = [(g["_ht_context"], g["_won"]) for g in games if "_ht_context" in g]
    if ht_vals:
        dims["halftime_context"] = _dim_stats(ht_vals)

    # Margin trajectory
    traj_vals = [(g["_trajectory"], g["_won"]) for g in games if "_trajectory" in g]
    if traj_vals:
        dims["margin_trajectory"] = _dim_stats(traj_vals)

    # Key conditions
    dims["key_conditions"] = _dim_stats([(g["_key_cond"], g["_won"]) for g in games])

    # Postgame tags (one game can appear in multiple tag buckets)
    tag_vals = []
    for g in games:
        for tag in g["_tags"]:
            tag_vals.append((tag, g["_won"]))
    dims["postgame_tags"] = _dim_stats(tag_vals)

    # Spread line size (only games with spread)
    spread_vals = [(_hist_bucket(g["_spread"], _SPREAD_RANGES), g["_won"])
                   for g in games if g["_spread"] is not None]
    if spread_vals:
        dims["spread_line_size"] = _dim_stats(spread_vals)

    # ── 2D combo analysis ────────────────────────────────────────────────
    def _features(g):
        f = {
            "side": g["_side"],
            "role": g["_role"],
            "margin": _hist_bucket(g["_margin"], _MARGIN_RANGES),
            "total_pts": _hist_bucket(g["_total"], _TOTAL_RANGES),
            "team": g.get("_team", ""),
            "has_ht_turnaround": g["_has_ht_turnaround"],
            "has_momentum_shift": g["_has_momentum_shift"],
        }
        if "_ht_position" in g:
            f["ht_position"] = g["_ht_position"]
        return f

    combo_pairs = [
        ("side", "margin"), ("side", "role"),
        ("role", "margin"), ("role", "ht_position"),
        ("role", "has_ht_turnaround"), ("role", "has_momentum_shift"),
        ("margin", "ht_position"), ("margin", "has_ht_turnaround"),
        ("margin", "has_momentum_shift"),
        ("team", "role"), ("ht_position", "has_ht_turnaround"),
    ]

    combos = []
    for d1, d2 in combo_pairs:
        buckets = defaultdict(lambda: {"games": 0, "wins": 0})
        for g in games:
            f = _features(g)
            v1, v2 = f.get(d1), f.get(d2)
            if v1 is None or v2 is None:
                continue
            key = f"{v1}|{v2}"
            buckets[key]["games"] += 1
            if g["_won"]:
                buckets[key]["wins"] += 1
        for key, val in buckets.items():
            if val["games"] < 3:
                continue
            parts = key.split("|")
            rate = round(val["wins"] / val["games"] * 100, 1)
            val["rate"] = rate
            val["low_confidence"] = val["games"] < 5
            combos.append({
                "key": key,
                "dimension1": d1, "value1": parts[0],
                "dimension2": d2, "value2": parts[1],
                "games": val["games"], "wins": val["wins"],
                "rate": rate, "low_confidence": val["low_confidence"],
            })
    combos.sort(key=lambda c: abs(c["rate"] - baseline_rate), reverse=True)

    # ── Pattern detection ────────────────────────────────────────────────
    profitable = [c for c in combos
                  if c["rate"] > baseline_rate + 10 and c["games"] >= 5]
    anti = [c for c in combos
            if c["rate"] < baseline_rate - 10 and c["games"] >= 5]

    return {
        "baseline": {"games": len(games), "wins": total_wins, "rate": baseline_rate},
        "dimensions": dims,
        "combos": combos,
        "profitable_patterns": profitable,
        "anti_patterns": anti,
        "total_games": len(game_history),
        "tied_at_minute": tied,
        "draws": draws,
        "cache_misses": cache_misses,
        "role": role,
        "target_minute": target_minute,
    }
```

- [ ] **Step 3: Test with real data**

Run:
```bash
python3 -c "
import json, odds_monitor as om
history = json.load(open('game_history.json'))
result = om.compute_historical_win_analysis(history, role='leading', target_minute=70)
print(f'Baseline: {result[\"baseline\"][\"rate\"]}% ({result[\"baseline\"][\"wins\"]}/{result[\"baseline\"][\"games\"]})')
print(f'Tied: {result[\"tied_at_minute\"]}, Draws: {result[\"draws\"]}, Misses: {result[\"cache_misses\"]}')
print(f'Dimensions: {list(result[\"dimensions\"].keys())}')
print(f'Combos: {len(result[\"combos\"])}, Profitable: {len(result[\"profitable_patterns\"])}, Anti: {len(result[\"anti_patterns\"])}')
assert result['baseline']['rate'] > 90, 'Leading team should win >90%'
assert len(result['dimensions']) >= 8, 'Should have 8+ dimensions'
# Test trailing
trail = om.compute_historical_win_analysis(history, role='trailing', target_minute=70)
print(f'Trailing baseline: {trail[\"baseline\"][\"rate\"]}%')
assert trail['baseline']['rate'] < 15, 'Trailing comeback rate should be <15%'
# Test final stretch
stretch = om.compute_historical_win_analysis(history, role='final_stretch', target_minute=70)
print(f'Final stretch baseline: {stretch[\"baseline\"][\"rate\"]}% ({stretch[\"baseline\"][\"wins\"]}/{stretch[\"baseline\"][\"games\"]})')
print(f'Final stretch dims: {list(stretch[\"dimensions\"].keys())}')
# Should have ~2x records (home + away per game), baseline ~50% (home advantage)
assert stretch['baseline']['games'] > 1500, 'Should have 2x records for final stretch'
assert 45 < stretch['baseline']['rate'] < 60, 'Home stretch win rate should be near 50%'
print('PASS: compute_historical_win_analysis')
"
```
Expected: Baseline ~93.1%, trailing ~6.9%, PASS

- [ ] **Step 4: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: compute_historical_win_analysis — win rate by dimension from PBP cache (#226)"
```

---

### Task 3: Enrich Existing `compute_spread_analysis()` with New Dimensions

**Files:**
- Modify: `odds_monitor.py:928-1089` (`compute_spread_analysis()`)

- [ ] **Step 1: Add `game_history` parameter**

Change the function signature at line 928:

```python
def compute_spread_analysis(history, role="leading", thresholds=None, game_history=None):
```

Update the docstring to document the new param:

```python
    """Compute spread analysis across game history with threshold sweep and dimensional breakdowns.

    Args:
        history: list of game records from odds_monitor load_history()
        role: "leading" or "trailing"
        thresholds: list of threshold values to sweep (default 1.5-6.0)
        game_history: optional list of game_history.json records for enrichment (#226)

    Returns dict with threshold_sweep, best_threshold, dimensions, combos,
    profitable_patterns, anti_patterns, total_games, role.
    """
```

- [ ] **Step 2: Build game_history lookup map**

Add after the `compute_fn` assignment (after line 944):

```python
    # Build game_history lookup for enrichment (#226)
    _gh_map = {}
    if game_history:
        for gh in game_history:
            mid = gh.get("match_id", "")
            if mid:
                _gh_map[mid] = gh
```

- [ ] **Step 3: Add new dimensions to `_bucket()` helper**

Extend the `_bucket()` function (lines 988-1018). The function receives `s` which has `s["game"]` containing the odds_monitor record. Add game_history enrichment:

Replace the return dict at lines 1008-1018 with:

```python
        result = {
            "side": side,
            "role": ml_role,
            "margin": "0-6" if margin <= 6 else ("7-12" if margin <= 12 else ("13-20" if margin <= 20 else "21+")),
            "entry_minute": "65-70" if entry <= 70 else ("71-75" if entry <= 75 else "76-80"),
            "total_points": "0-30" if total <= 30 else ("31-40" if total <= 40 else ("41-50" if total <= 50 else "51+")),
            "ev_edge": "negative" if ev < 0 else ("neutral" if ev <= 0.05 else "positive"),
            "spread_price": "$3-4" if price < 4 else ("$4-5" if price < 5 else "$5+"),
            "team": strat.get("team", ""),
            "competition": game.get("competition", ""),
        }
        # Enrich from game_history (#226)
        gh = _gh_map.get(game.get("match_id", ""))
        if gh:
            # Halftime context
            htm = gh.get("halftime_margin")
            if htm is not None:
                strat_side = strat.get("side", "")
                ht_lead = htm if strat_side == "home" else -htm
                ht_mb = _hist_bucket(abs(ht_lead), _HT_MARGIN_RANGES)
                if ht_lead > 0:
                    result["halftime_context"] = f"{ht_mb} (leading@HT)"
                    result["ht_position"] = "leading@HT"
                elif ht_lead < 0:
                    result["halftime_context"] = f"{ht_mb} (trailing@HT)"
                    result["ht_position"] = "trailing@HT"
                else:
                    result["halftime_context"] = f"{ht_mb} (tied@HT)"
                    result["ht_position"] = "tied@HT"
                # Margin trajectory
                diff = margin - abs(ht_lead) if ht_lead >= 0 else margin + ht_lead
                if diff > 4:
                    result["margin_trajectory"] = "lead_growing (>4pts)"
                elif diff > 0:
                    result["margin_trajectory"] = "lead_growing (1-4pts)"
                elif diff == 0:
                    result["margin_trajectory"] = "lead_stable"
                elif diff > -4:
                    result["margin_trajectory"] = "lead_shrinking (1-4pts)"
                else:
                    result["margin_trajectory"] = "lead_shrinking (>4pts)"
            # Key conditions
            cond_types = set()
            for c in gh.get("conditions_fired", []):
                ct = c.get("type", "")
                if ct in ("halftime_turnaround", "momentum_shift"):
                    cond_types.add(ct)
            if "halftime_turnaround" in cond_types:
                result["key_conditions"] = "has_halftime_turnaround"
            elif "momentum_shift" in cond_types:
                result["key_conditions"] = "has_momentum_shift"
            else:
                result["key_conditions"] = "neither"
            result["has_ht_turnaround"] = "yes" if "halftime_turnaround" in cond_types else "no"
            # Postgame tags
            tags = gh.get("postgame_tags", [])
            result["postgame_tags"] = tags[0] if tags else "no_tags"
            # Spread line size
            spr = gh.get("home_spread")
            if spr is not None:
                result["spread_line_size"] = _hist_bucket(abs(spr), _SPREAD_RANGES)
        return result
```

- [ ] **Step 4: Add new dimension names to the dim_names list**

Change line 1022:

```python
    dim_names = ["side", "role", "margin", "entry_minute", "total_points", "ev_edge",
                 "spread_price", "team", "competition",
                 "halftime_context", "margin_trajectory", "key_conditions",
                 "postgame_tags", "spread_line_size"]
```

The dimensional breakdown loop (lines 1023-1040) already handles missing keys gracefully — if a bucket key doesn't exist in `_bucket()` output, it just won't appear.

- [ ] **Step 5: Add new combo pairs**

Extend `combo_pairs` at lines 1043-1047:

```python
    combo_pairs = [
        ("side", "margin"), ("side", "entry_minute"), ("side", "ev_edge"),
        ("side", "role"), ("role", "margin"), ("role", "entry_minute"), ("role", "ev_edge"),
        ("margin", "entry_minute"), ("margin", "ev_edge"), ("entry_minute", "ev_edge"),
        # New pairs (#226)
        ("side", "has_ht_turnaround"), ("role", "has_ht_turnaround"),
        ("margin", "has_ht_turnaround"), ("margin", "ht_position"),
    ]
```

- [ ] **Step 6: Verify existing BK analysis still works**

Run:
```bash
python3 -c "
import odds_monitor as om
history = om.load_history()
# Without game_history — should work identically to before
result = om.compute_spread_analysis(history, role='leading')
print(f'Total games: {result[\"total_games\"]}')
print(f'Dimensions: {list(result[\"dimensions\"].keys())}')
assert 'side' in result['dimensions'], 'Should have side dimension'
assert 'ev_edge' in result['dimensions'], 'Should still have ev_edge'
# With game_history — should add new dimensions
import json
gh = json.load(open('game_history.json'))
result2 = om.compute_spread_analysis(history, role='leading', game_history=gh)
dims2 = list(result2['dimensions'].keys())
print(f'Enriched dimensions: {dims2}')
# New dims may be empty if no match_ids overlap, but keys should exist
print('PASS: compute_spread_analysis enrichment')
"
```
Expected: PASS, enriched dimensions include new names

- [ ] **Step 7: Commit**

```bash
git add odds_monitor.py
git commit -m "feat: enrich compute_spread_analysis with 5 new game_history dimensions (#226)"
```

---

### Task 4: Server Endpoint `/api/odds-monitor-historical`

**Files:**
- Modify: `server.py:4158-4183` (add new endpoint after existing analysis endpoint)

- [ ] **Step 1: Add the new endpoint handler**

After the `/api/odds-monitor-analysis` block (after line 4183), add:

```python
        if path == "/api/odds-monitor-historical":
            try:
                params = parse_qs(parsed.query)
                role = (params.get("role") or ["leading"])[0].strip().lower()
                if role not in ("leading", "trailing", "final_stretch"):
                    role = "leading"
                minute = int((params.get("minute") or ["70"])[0])
                if minute < 60 or minute > 80:
                    minute = 70
                game_hist = _load_history()
                result = odds_monitor.compute_historical_win_analysis(
                    game_hist, role=role, target_minute=minute)
                self.send_json(200, result)
            except Exception as e:
                self.send_json(500, {"error": str(e)})
            return
```

- [ ] **Step 2: Pass game_history to existing analysis endpoint**

Modify the existing `/api/odds-monitor-analysis` handler at line 4179. Change:

```python
                result = odds_monitor.compute_spread_analysis(history, role=role)
```

to:

```python
                game_hist = _load_history()
                result = odds_monitor.compute_spread_analysis(history, role=role, game_history=game_hist)
```

- [ ] **Step 3: Test endpoints**

Start the server and test:
```bash
# Test new endpoint
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=leading&minute=70' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Baseline: {d[\"baseline\"][\"rate\"]}% ({d[\"baseline\"][\"wins\"]}/{d[\"baseline\"][\"games\"]})')
print(f'Dims: {list(d[\"dimensions\"].keys())}')
print(f'Combos: {len(d[\"combos\"])}')
assert d['baseline']['games'] > 800, 'Should have 800+ games'
print('PASS')
"

# Test trailing
curl -s 'http://127.0.0.1:8898/api/odds-monitor-historical?role=trailing&minute=70' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'Trailing baseline: {d[\"baseline\"][\"rate\"]}%')
assert d['baseline']['rate'] < 15
print('PASS')
"

# Test existing endpoint still works with enrichment
curl -s 'http://127.0.0.1:8898/api/odds-monitor-analysis?role=leading' | python3 -c "
import json,sys
d=json.load(sys.stdin)
print(f'BK dims: {list(d[\"dimensions\"].keys())}')
print('PASS')
"
```

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat: /api/odds-monitor-historical endpoint + pass game_history to spread analysis (#226)"
```

---

### Task 5: Dashboard — View Toggle + Historical View HTML

**Files:**
- Modify: `dashboard.html:1334-1362` (Spread Strategy section HTML)

- [ ] **Step 1: Add view toggle buttons after the role dropdown**

After the Refresh button (line 1345), before the closing `</div>`, add the view toggle:

```html
        <span style="margin-left:12px;display:inline-flex;gap:2px;border:1px solid var(--border);border-radius:6px;padding:1px">
          <button id="lev-view-bk" class="btn" onclick="_levSwitchView('bk')" style="padding:2px 8px;font-size:0.72em;border-radius:4px" title="BK-tracked spread coverage analysis">BK Spread</button>
          <button id="lev-view-hist" class="btn" onclick="_levSwitchView('historical')" style="padding:2px 8px;font-size:0.72em;border-radius:4px" title="Historical win rate at 70+ min from game history">Historical Win Rate</button>
        </span>
```

- [ ] **Step 2: Wrap existing BK content in a container div**

Wrap lines 1348-1362 (from `<!-- Aggregate stats -->` through the Spread Analysis `</div>`) plus the filter controls and history table in a container:

```html
      <!-- BK Spread Coverage view -->
      <div id="lev-bk-view">
        <!-- Aggregate stats -->
        <div id="lev-stats"></div>
        <!-- Leading tries probability reference -->
        <div id="lev-probs" style="margin-top:8px"></div>
        <!-- Spread Analysis (#216) -->
        <div id="lev-analysis" style="margin-top:12px">
          ... (existing content unchanged) ...
        </div>
        ... (existing help modal, filters, history table unchanged) ...
      </div>
```

- [ ] **Step 3: Add Historical Win Rate view container**

After `</div><!-- end lev-bk-view -->`, add:

```html
      <!-- Historical Win Rate view (#226) -->
      <div id="lev-hist-view" style="display:none">
        <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
          <label style="font-size:0.78em;color:var(--muted)">Perspective:
            <select id="lev-hist-role" onchange="loadHistoricalWinRate()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:0.78em">
              <option value="leading">Leading Team</option>
              <option value="trailing">Trailing Team</option>
              <option value="final_stretch">Final Stretch (0-0 reset)</option>
            </select>
          </label>
          <label style="font-size:0.78em;color:var(--muted)">Minute:
            <select id="lev-hist-minute" onchange="loadHistoricalWinRate()" style="background:var(--bg);border:1px solid var(--border);color:var(--text);padding:2px 4px;border-radius:4px;font-size:0.78em">
              <option value="65">65</option>
              <option value="68">68</option>
              <option value="70" selected>70</option>
              <option value="72">72</option>
              <option value="75">75</option>
            </select>
          </label>
        </div>
        <div id="lev-hist-baseline" style="margin-bottom:10px"></div>
        <div id="lev-hist-content"><div class="no-data">Select a view to load data</div></div>
      </div>
```

- [ ] **Step 4: Commit**

```bash
git add dashboard.html
git commit -m "feat: Spread Strategy view toggle + historical win rate HTML structure (#226)"
```

---

### Task 6: Dashboard — View Toggle JS + `loadHistoricalWinRate()`

**Files:**
- Modify: `dashboard.html` (JS section, after `_levSavePrice()` at line 8633)

- [ ] **Step 1: Add `_levSwitchView()` function**

After `_levSavePrice()`:

```javascript
function _levSwitchView(view) {
  const bkEl = document.getElementById('lev-bk-view');
  const histEl = document.getElementById('lev-hist-view');
  const bkBtn = document.getElementById('lev-view-bk');
  const histBtn = document.getElementById('lev-view-hist');
  if (!bkEl || !histEl) return;
  const isBk = view === 'bk';
  bkEl.style.display = isBk ? '' : 'none';
  histEl.style.display = isBk ? 'none' : '';
  // Active button styling
  const activeStyle = 'background:var(--nrl-green);color:#fff;border-color:var(--nrl-green)';
  const inactiveStyle = '';
  if (bkBtn) bkBtn.style.cssText = bkBtn.style.cssText.replace(/background:[^;]+;?|color:[^;]+;?|border-color:[^;]+;?/g, '') + (isBk ? activeStyle : inactiveStyle);
  if (histBtn) histBtn.style.cssText = histBtn.style.cssText.replace(/background:[^;]+;?|color:[^;]+;?|border-color:[^;]+;?/g, '') + (isBk ? inactiveStyle : activeStyle);
  try { localStorage.setItem('lev_view', view); } catch(e) {}
  if (isBk) {
    loadLeadingEv();
  } else {
    loadHistoricalWinRate();
  }
}
```

- [ ] **Step 2: Add `loadHistoricalWinRate()` function**

```javascript
function loadHistoricalWinRate() {
  const role = document.getElementById('lev-hist-role')?.value || 'leading';
  const minute = document.getElementById('lev-hist-minute')?.value || '70';
  const baselineEl = document.getElementById('lev-hist-baseline');
  const contentEl = document.getElementById('lev-hist-content');
  if (!contentEl) return;
  contentEl.innerHTML = '<div class="no-data">Loading...</div>';
  try { localStorage.setItem('lev_hist_minute', minute); localStorage.setItem('lev_hist_role', role); } catch(e) {}

  fetch(API + '/api/odds-monitor-historical?role=' + role + '&minute=' + minute).then(r => r.json()).then(data => {
    if (!data || !data.baseline || !data.baseline.games) {
      contentEl.innerHTML = '<div class="no-data">No historical data available</div>';
      return;
    }
    const bl = data.baseline;
    const isStretch = role === 'final_stretch';
    const isTrailing = role === 'trailing';
    const blLabel = isStretch
      ? `Home team outscores in final stretch (min ${data.target_minute}-80)`
      : isTrailing
        ? `Trailing team at min ${data.target_minute} wins (comeback)`
        : `Leading team at min ${data.target_minute} wins`;

    // Baseline card
    const blColor = bl.rate >= 80 ? 'var(--green)' : bl.rate >= 60 ? 'var(--yellow, orange)' : 'var(--red)';
    let bh = '<div style="padding:8px 14px;border:2px solid ' + blColor + ';border-radius:8px;background:var(--bg);display:inline-block">';
    bh += '<div style="font-size:0.78em;color:var(--muted)">' + blLabel + '</div>';
    bh += '<div style="font-size:1.3em;font-weight:700;color:' + blColor + '">' + bl.rate + '% <span style="font-size:0.6em;font-weight:400;color:var(--muted)">(' + bl.wins + '/' + bl.games + ')</span></div>';
    bh += '<div style="font-size:0.68em;color:var(--muted)">' + data.tied_at_minute + ' tied excluded · ' + data.cache_misses + ' cache misses</div>';
    bh += '</div>';
    if (baselineEl) baselineEl.innerHTML = bh;

    let h = '';
    const rateColor = (r) => {
      if (isStretch) return r > 55 ? 'var(--green)' : r > 45 ? 'var(--muted)' : 'var(--red)';
      if (isTrailing) return r > 20 ? 'var(--red)' : r > 10 ? 'var(--yellow, orange)' : 'var(--green)';
      return r >= 90 ? 'var(--green)' : r >= 75 ? 'var(--yellow, orange)' : 'var(--red)';
    };
    const lcIcon = lc => lc ? ' <span title="Low confidence (<10 games)" style="cursor:help">⚠️</span>' : '';

    // Notable Patterns
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

    // Dimensional Breakdowns
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
          h += '<tr>';
          h += '<td style="font-weight:600">' + esc(bk) + lcIcon(b.low_confidence) + '</td>';
          h += '<td style="text-align:center">' + b.games + '</td>';
          h += '<td style="text-align:center">' + b.wins + '</td>';
          h += '<td style="text-align:center;font-weight:600;color:' + rc + '">' + b.rate + '%</td>';
          h += '</tr>';
        }
        h += '</tbody></table></div></details>';
      }
      h += '</div>';
    }

    // 2D Combos
    const combos = data.combos || [];
    if (combos.length) {
      const top20 = combos.slice(0, 20);
      h += '<div style="margin-top:10px"><div style="font-size:0.78em;font-weight:600;color:var(--nrl-green);margin-bottom:4px">Top 2D Combos (by deviation from baseline)</div>';
      h += '<div style="overflow-x:auto"><table class="analysis-table"><thead><tr><th>Combo</th><th>Games</th><th>Wins</th><th>Win Rate</th><th>vs Baseline</th></tr></thead><tbody>';
      for (const c of top20) {
        const delta = (c.rate - bl.rate).toFixed(1);
        const dc = parseFloat(delta) >= 0 ? 'var(--green)' : 'var(--red)';
        h += '<tr>';
        h += '<td style="font-size:0.76em;white-space:nowrap">' + esc(c.value1) + ' + ' + esc(c.value2) + lcIcon(c.low_confidence) + '</td>';
        h += '<td style="text-align:center">' + c.games + '</td>';
        h += '<td style="text-align:center">' + c.wins + '</td>';
        h += '<td style="text-align:center;font-weight:600">' + c.rate + '%</td>';
        h += '<td style="text-align:center;color:' + dc + '">' + (delta >= 0 ? '+' : '') + delta + 'pp</td>';
        h += '</tr>';
      }
      h += '</tbody></table></div></div>';
    }

    contentEl.innerHTML = h || '<div class="no-data">No analysis data</div>';
  }).catch(e => {
    contentEl.innerHTML = '<div class="no-data" style="color:var(--red)">Error: ' + e + '</div>';
  });
}
```

- [ ] **Step 3: Commit**

```bash
git add dashboard.html
git commit -m "feat: _levSwitchView + loadHistoricalWinRate JS functions (#226)"
```

---

### Task 7: Dashboard — Restore State + Wire Up Role Dropdown

**Files:**
- Modify: `dashboard.html` (JS section)

- [ ] **Step 1: Update the Spread Strategy tab load handler**

Find the onclick handler for the Spread Strategy tab (around line 759 where the tab link calls a load function). The tab's onclick should call `_levInitView()` instead of directly calling `loadLeadingEv()`. Add a new init function:

```javascript
function _levInitView() {
  // Restore saved view
  const savedView = localStorage.getItem('lev_view') || 'bk';
  // Restore saved minute
  const savedMin = localStorage.getItem('lev_hist_minute') || '70';
  const minEl = document.getElementById('lev-hist-minute');
  if (minEl) minEl.value = savedMin;
  // Restore saved historical role
  const savedRole = localStorage.getItem('lev_hist_role') || 'leading';
  const roleEl = document.getElementById('lev-hist-role');
  if (roleEl) roleEl.value = savedRole;
  _levSwitchView(savedView);
}
```

- [ ] **Step 2: Update role dropdown to handle both views**

Modify the role dropdown `onchange` at line 1336. Change:

```html
onchange="loadLeadingEv()"
```

to:

```html
onchange="_levOnRoleChange()"
```

Add the handler function:

```javascript
function _levOnRoleChange() {
  const view = localStorage.getItem('lev_view') || 'bk';
  if (view === 'historical') {
    loadHistoricalWinRate();
  } else {
    loadLeadingEv();
  }
}
```

- [ ] **Step 3: Hide BK-only controls when in historical view**

In `_levSwitchView()`, also toggle visibility of the price threshold and BK-specific controls. Add after setting display:

```javascript
  // Hide BK-only controls in historical view
  const priceLabel = document.getElementById('lev-price')?.parentElement;
  const saveBtn = priceLabel?.nextElementSibling;
  const refreshBtn = saveBtn?.nextElementSibling;
  if (priceLabel) priceLabel.style.display = isBk ? '' : 'none';
  if (saveBtn) saveBtn.style.display = isBk ? '' : 'none';
  if (refreshBtn) refreshBtn.style.display = isBk ? '' : 'none';
```

- [ ] **Step 4: Test in browser**

1. Load `http://127.0.0.1:8898/dashboard.html`
2. Navigate to Spread Strategy tab
3. Verify "BK Spread" and "Historical Win Rate" toggle buttons appear
4. Click "Historical Win Rate" — should show baseline card + dimensional breakdowns
5. Switch perspective to "Trailing Team" — should show comeback rate (~6.9%)
6. Switch perspective to "Final Stretch (0-0 reset)" — should show ~50% baseline, per-team breakdown
7. Change minute selector — should re-fetch and update
8. Switch back to "BK Spread" — existing view should load normally
9. Refresh page — view selection, minute, and perspective should persist from localStorage

- [ ] **Step 5: Commit**

```bash
git add dashboard.html
git commit -m "feat: wire up view toggle, role dropdown, localStorage persistence (#226)"
```

---

### Task 8: Update CHANGES.md and Issue

**Files:**
- Modify: `CHANGES.md`

- [ ] **Step 1: Read current CHANGES.md**

Read the top of CHANGES.md to see the current format and latest entries.

- [ ] **Step 2: Add entry**

Add at the top of the changelog (after the header):

```markdown
## [Unreleased]

### Added
- **Spread Strategy: Historical Win Rate sub-view** (#226): Analyses whether the leading/trailing team at minute 70+ wins the game using 870+ historical games from PBP cache. Three perspectives: Leading (win rate), Trailing (comeback rate), Final Stretch (who outscores in last 10 min — score reset to 0-0). New view toggle in Spread Strategy tab with dimensional breakdowns (margin, halftime context, margin trajectory, key conditions, postgame tags), 2D combo analysis, and pattern detection. Leading team wins 93.1% baseline; `has_halftime_turnaround` condition strongest anti-signal at 64.2%.
- **Spread Strategy: 5 new dimensions in BK analysis** (#226): halftime_context, margin_trajectory, key_conditions, postgame_tags, spread_line_size added to existing BK-tracked spread coverage analysis.
- **New endpoint** `/api/odds-monitor-historical` (#226): Win rate analysis from game_history + PBP cache. Params: `role` (leading/trailing/final_stretch), `minute` (65-75, default 70).
```

- [ ] **Step 3: Commit and push**

```bash
git add CHANGES.md
git commit -m "docs: CHANGES.md for Spread Strategy historical win rate (#226)"
git push
```

- [ ] **Step 4: Update GitHub issue**

Comment on #226 with implementation summary and close the issue if all success criteria are met.
