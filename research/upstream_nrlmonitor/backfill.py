#!/usr/bin/env python3
"""
NRL Monitor — Historical Backfill

Two-phase backfill to populate game_history.json from NRL.com APIs.

Phase 1: Fetch & Cache (--fetch-cache)
  Downloads draw + match centre data into nrl_cache/<season>/.
  Idempotent — skips already-cached files.

Phase 2: Replay & Write (--use-cache + --write-canonical)
  Reads from local cache, evaluates conditions, computes postgame tags,
  and writes game records to game_history.json.

Usage:
  python3 backfill.py --fetch-cache --season 2024
  python3 backfill.py --fetch-cache --season 2024,2025,2026
  python3 backfill.py --use-cache --dry-run --season 2024
  python3 backfill.py --use-cache --write-canonical --season 2024
  python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026 --skip-existing
  python3 backfill.py --use-cache --write-canonical --season 2024 --resume
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import nrl_api
import conditions as condition_logic
import timeline_features

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(SCRIPT_DIR, "nrl_cache")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
CONFIG_FILE = os.path.join(SCRIPT_DIR, "config.json")
BACKFILL_STATE_FILE = os.path.join(SCRIPT_DIR, "backfill_state.json")

# Regular season R1-R27, finals R28-R31 (R32 is duplicate Grand Final in some years)
MAX_REGULAR_ROUND = 27
MAX_FINALS_ROUND = 31

# Postgame tag thresholds
CLOSE_MARGIN = 6       # 1 converted try
BLOWOUT_MARGIN = 20
COMEBACK_HALFTIME = True  # trailing at HT but wins


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} {}".format(ts, msg), flush=True)


# ── Cache I/O ─────────────────────────────────────────────────────────────────

def _cache_path(season, filename):
    d = os.path.join(CACHE_DIR, str(season))
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, filename)


def _cache_exists(season, filename):
    return os.path.isfile(_cache_path(season, filename))


def _write_cache(season, filename, data):
    path = _cache_path(season, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _read_cache(season, filename):
    path = _cache_path(season, filename)
    if not os.path.isfile(path):
        return None
    with open(path) as f:
        return json.load(f)


def _match_slug(match_centre_url):
    """Extract a slug from match centre URL for cache filename.

    /draw/nrl-premiership/2024/round-1/sea-eagles-v-rabbitohs/ → sea-eagles-v-rabbitohs
    """
    parts = match_centre_url.rstrip("/").split("/")
    return parts[-1] if parts else "unknown"


# ── Backfill state ────────────────────────────────────────────────────────────

def _load_backfill_state():
    try:
        with open(BACKFILL_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_backfill_state(state):
    with open(BACKFILL_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Phase 1: Fetch & Cache ───────────────────────────────────────────────────

def fetch_cache_season(season, delay=0.4):
    """Download all draw + match data for a season into the cache."""
    _log("Phase 1: Fetching season {} into cache...".format(season))
    total_games = 0
    cached_games = 0

    for rnd in range(1, MAX_FINALS_ROUND + 1):
        draw_file = "draw_round_{}.json".format(rnd)

        # Fetch draw if not cached
        if _cache_exists(season, draw_file):
            draw = _read_cache(season, draw_file)
        else:
            try:
                draw = nrl_api.get_draw(season, rnd)
                time.sleep(delay)
            except Exception as e:
                _log("  R{}: draw fetch failed: {}".format(rnd, e))
                if rnd > MAX_REGULAR_ROUND:
                    break  # no more finals
                continue

            if not draw:
                if rnd > MAX_REGULAR_ROUND:
                    break
                _log("  R{}: no fixtures (skipping)".format(rnd))
                continue

            _write_cache(season, draw_file, draw)

        if not draw:
            if rnd > MAX_REGULAR_ROUND:
                break
            continue

        # Only process completed games
        completed = [f for f in draw if f.get("matchState") in nrl_api.COMPLETED_STATES]
        if not completed:
            if rnd <= MAX_REGULAR_ROUND:
                _log("  R{}: no completed games".format(rnd))
            continue

        round_title = completed[0].get("roundTitle", "Round {}".format(rnd))
        _log("  R{} ({}): {} completed games".format(rnd, round_title, len(completed)))

        # Fetch match centre data for each game
        for fix in completed:
            match_url = fix.get("matchCentreUrl", "")
            if not match_url:
                continue

            slug = _match_slug(match_url)
            match_file = "match_{}_r{}.json".format(slug, rnd)

            if _cache_exists(season, match_file):
                cached_games += 1
                total_games += 1
                continue

            try:
                raw = nrl_api.get_match_data(match_url)
                _write_cache(season, match_file, raw)
                total_games += 1
                time.sleep(delay)
            except Exception as e:
                _log("    WARN: failed to fetch {}: {}".format(slug, e))

    _log("Phase 1 complete: {} season {} — {} games fetched, {} from cache".format(
        season, total_games, total_games - cached_games, cached_games))
    return total_games


# ── Stats extraction ──────────────────────────────────────────────────────────

def extract_team_stats_from_groups(match_data, side):
    """Extract team stats from match.stats.groups for home or away side.

    Args:
        match_data: raw match centre JSON
        side: 'home' or 'away'

    Returns:
        dict of stat_name → value
    """
    stats = {}
    groups = (match_data.get("stats") or {}).get("groups", [])
    value_key = "homeValue" if side == "home" else "awayValue"

    for group in groups:
        for stat in group.get("stats", []):
            title = stat.get("title", "")
            val_obj = stat.get(value_key, {})
            if isinstance(val_obj, dict):
                val = val_obj.get("value")
            else:
                val = val_obj

            if val is not None and title:
                # Normalize stat names to camelCase keys
                key = _normalize_stat_key(title)
                stats[key] = val

    return stats


def _normalize_stat_key(title):
    """Convert stat title to a camelCase key.

    'Possession %' → 'possession'
    'Completion Rate' → 'completionRate'
    'Missed Tackles' → 'missedTackles'
    'All Run Metres' → 'allRunMetres'
    """
    # Remove % and special chars
    s = title.replace("%", "").strip()
    words = s.split()
    if not words:
        return title.lower()
    result = words[0].lower()
    for w in words[1:]:
        result += w.capitalize()
    return result


# ── Postgame tags ─────────────────────────────────────────────────────────────

def compute_postgame_tags(record):
    """Compute postgame outcome tags for a game record.

    Returns list of tag strings.
    """
    tags = []
    margin = record.get("margin", 0)
    winner = record.get("winner")
    home_odds = record.get("home_odds")
    away_odds = record.get("away_odds")
    home_score_1h = record.get("home_score_1h")
    away_score_1h = record.get("away_score_1h")
    home_team = record.get("home_team", "")
    away_team = record.get("away_team", "")

    # UPSET: underdog wins (higher odds = underdog)
    if winner and home_odds and away_odds:
        try:
            h_odds = float(home_odds)
            a_odds = float(away_odds)
            if winner == home_team and h_odds > a_odds:
                tags.append("UPSET")
            elif winner == away_team and a_odds > h_odds:
                tags.append("UPSET")
        except (TypeError, ValueError):
            pass

    # CLOSE: margin ≤ threshold
    if margin <= CLOSE_MARGIN and margin > 0:
        tags.append("CLOSE")

    # BLOWOUT: margin ≥ threshold
    if margin >= BLOWOUT_MARGIN:
        tags.append("BLOWOUT")

    # COMEBACK: trailing at halftime but wins
    if (winner and home_score_1h is not None and away_score_1h is not None):
        if winner == home_team and home_score_1h < away_score_1h:
            tags.append("COMEBACK")
        elif winner == away_team and away_score_1h < home_score_1h:
            tags.append("COMEBACK")

    return tags


# ── Condition evaluation for backfill ─────────────────────────────────────────

def _score_at_seconds(score_prog, game_seconds, home_final, away_final):
    """Look up (home_score, away_score) at a game_seconds from score_progression."""
    if not score_prog:
        return home_final, away_final
    h, a = 0, 0
    for p in score_prog:
        if p.get("gameSeconds", 0) > game_seconds:
            break
        h = p.get("homeScore", h)
        a = p.get("awayScore", a)
    return h, a


def _margin_at_seconds(score_prog, game_seconds, home_final, away_final, side):
    """Get margin from team perspective at a game_seconds."""
    h, a = _score_at_seconds(score_prog, game_seconds, home_final, away_final)
    return (h - a) if side == "home" else (a - h)


def _estimate_fire_time(cond_type, side, record, cond=None):
    """Estimate the game_seconds when a condition would have first fired.

    Returns game_seconds (int) or None if no PBP data available.
    Uses try_events, score_progression, and PBP feature data.
    """
    HALF_SECS = 40 * 60
    try_events = record.get("try_events", [])
    score_prog = record.get("score_progression", [])
    team_id = record.get("{}_team_id".format(side))
    team_name = record.get("{}_team".format(side), "")

    if cond_type == "first_team_scores":
        if try_events:
            first = min(try_events, key=lambda t: t.get("game_seconds", 9999))
            return first.get("game_seconds", 0)
        return None

    if cond_type == "try_scoring_run":
        team_tries = sorted(
            [t for t in try_events if t.get("team_id") == team_id or t.get("team") == team_name],
            key=lambda t: t.get("game_seconds", 0))
        if team_tries:
            return team_tries[-1].get("game_seconds", None)
        return None

    if cond_type in ("points_run", "momentum_shift"):
        team_tries = sorted(
            [t for t in try_events if t.get("team_id") == team_id or t.get("team") == team_name],
            key=lambda t: t.get("game_seconds", 0))
        if team_tries:
            return team_tries[-1].get("game_seconds", None)
        return None

    if cond_type == "halftime_turnaround":
        return 80 * 60

    if cond_type == "score_diff" and score_prog and cond:
        # Find when the margin first crossed the threshold
        req_period = cond.get("period", "game")
        status = cond.get("team_status", "")
        min_lead = cond.get("min_lead")
        min_deficit = cond.get("min_deficit")

        if req_period == "1H":
            return HALF_SECS  # 1H condition → margin at halftime

        # For "game" or "2H" score_diff: scan score_progression for when threshold crossed
        for p in score_prog:
            gs = p.get("gameSeconds", 0)
            h = p.get("homeScore", 0)
            a = p.get("awayScore", 0)
            margin = (h - a) if side == "home" else (a - h)
            if status == "winning" and min_lead is not None and margin >= min_lead:
                return gs
            if status == "trailing" and min_deficit is not None and abs(margin) >= min_deficit and margin < 0:
                return gs
        return None

    # For stats-based conditions without PBP fire times, return None
    return None


def evaluate_conditions_for_game(config, record):
    """Evaluate all configured conditions against a completed game's stats.

    Uses score_progression for time-aware margins on PBP conditions.
    Returns list of condition hit dicts.
    """
    conditions = config.get("conditions", [])
    hits = []
    score_prog = record.get("score_progression", [])
    home_score_final = record.get("home_score", 0) or 0
    away_score_final = record.get("away_score", 0) or 0

    for side in ("home", "away"):
        team_name = record.get("{}_team".format(side), "")
        opp_side = "away" if side == "home" else "home"
        my_score = record.get("{}_score".format(side), 0) or 0
        opp_score = record.get("{}_score".format(opp_side), 0) or 0

        my_1h = record.get("{}_score_1h".format(side))
        opp_1h = record.get("{}_score_1h".format(opp_side))
        diff_1h = (my_1h - opp_1h) if (my_1h is not None and opp_1h is not None) else None
        diff_2h = None
        if my_1h is not None and opp_1h is not None:
            my_2h = my_score - my_1h
            opp_2h = opp_score - opp_1h
            diff_2h = my_2h - opp_2h

        # Build PBP features dict for condition evaluation
        pbp_features = {k: record.get(k, 0) for k in record
                        if k.startswith(("home_max_", "away_max_", "home_sin", "away_sin",
                                         "home_line", "away_line", "home_err", "away_err",
                                         "home_fort", "away_fort", "halftime_margin", "h2_momentum"))}

        team_ctx = {
            "team_name": team_name,
            "team_side": side,
            "my_score": my_score,
            "opp_score": opp_score,
            "diff_total": my_score - opp_score,
            "diff_1h": diff_1h,
            "diff_2h": diff_2h,
            "is_losing": my_score < opp_score,
            "stats": record.get("{}_stats".format(side), {}),
            "consecutive_tries": record.get("{}_max_consecutive_tries".format(side), 0),
            "unanswered_points": record.get("{}_max_unanswered_points".format(side), 0),
            "pbp_features": pbp_features,
        }

        # Evaluate at FT context — conditions see final game state
        context = {
            "game_id": record.get("match_id", ""),
            "period": "FT",
            "state": {},
            "match_detail": None,
        }

        for cond in conditions:
            cond_type = cond.get("type", "")
            result = condition_logic.evaluate_condition(cond, team_ctx, context)
            if not result.get("matched"):
                continue

            # Determine margin at fire time
            fire_secs = _estimate_fire_time(cond_type, side, record, cond)

            if fire_secs is not None and score_prog:
                # Use exact score from PBP at fire time
                margin = _margin_at_seconds(score_prog, fire_secs, home_score_final, away_score_final, side)
            elif cond_type == "score_diff":
                # Use period-specific margin
                req_period = cond.get("period", "game")
                if req_period == "1H" and diff_1h is not None:
                    margin = diff_1h
                elif req_period == "2H" and diff_2h is not None:
                    margin = diff_2h
                else:
                    margin = my_score - opp_score
            else:
                # Stats-based conditions: use final margin
                margin = my_score - opp_score

            hit = {
                "condition": cond.get("name", ""),
                "type": cond_type,
                "team": team_name,
                "team_side": side,
                "direction": result.get("direction", ""),
                "score_margin": margin,
            }
            if fire_secs is not None:
                hit["game_seconds_at_fire"] = fire_secs
            hits.append(hit)

    return hits


# ── Phase 2: Replay & Write ──────────────────────────────────────────────────

def _generate_pw_calls_for_record(record, prior_records, config,
                                  threshold=65, cooldown_minutes=5):
    """Generate synthetic PW calls for a historical game.

    Evaluates at PBP scoring moments + halftime anchor.
    ML model disabled during backfill to prevent data leakage (#40).
    Historical stats computed from prior_records only (lookahead-safe).
    """
    # ML model explicitly disabled — trained on all history, would leak
    # future outcomes into past predictions (#40, matches NBA approach)
    ml_model = None
    try:
        import outcomes as outcomes_mod
    except ImportError:
        outcomes_mod = None

    calls = []
    winner = record.get("winner")
    home = record.get("home_team", "")
    away = record.get("away_team", "")
    home_score = record.get("home_score", 0) or 0
    away_score = record.get("away_score", 0) or 0
    conditions_fired = record.get("conditions_fired", [])
    # Resolve PW version from game date
    try:
        import pw_trend
        _resolve_ver = pw_trend.resolve_pw_version
    except ImportError:
        try:
            import outcomes as _outcomes_mod
            _resolve_ver = _outcomes_mod.resolve_pw_version
        except (ImportError, AttributeError):
            _resolve_ver = lambda d: config.get("pw_version", "v1.0")
    game_date = record.get("kickoff_utc", record.get("ts", ""))
    pw_version = _resolve_ver(game_date)
    half_weights = config.get("pw_half_weights", {"H1": 0.90, "H2": 1.05, "ET": 1.10})
    blend = config.get("pw_blend_weights", {"ml": 0.70, "historical": 0.30})
    margin_gate = config.get("pw_margin_gate", -12)
    margin_gate_late = config.get("pw_margin_gate_late", -8)
    edge_gate = config.get("pw_edge_gate")

    # Build edge store for edge gate suppression (#28)
    _edge_store = None
    if edge_gate is not None and outcomes_mod:
        try:
            _edge_store = outcomes_mod.ConditionEdgeStore(prior_records or [])
        except Exception:
            pass

    # Build historical condition win% from prior_records only (lookahead-safe #40)
    hist_stats = {}
    if prior_records:
        _cond_fires = {}  # {cond_name: {fires: int, team_won: int}}
        for pr in prior_records:
            pr_winner = pr.get("winner")
            if not pr_winner:
                continue
            for cf in pr.get("conditions_fired", []):
                cname = cf.get("condition", "")
                if not cname:
                    continue
                if cname not in _cond_fires:
                    _cond_fires[cname] = {"fires": 0, "team_won": 0}
                _cond_fires[cname]["fires"] += 1
                if cf.get("team", "") == pr_winner:
                    _cond_fires[cname]["team_won"] += 1
        for cname, stats in _cond_fires.items():
            if stats["fires"] >= 3:
                hist_stats[cname] = {
                    "win_pct": stats["team_won"] / stats["fires"] * 100,
                }

    # ML prediction disabled during backfill (#40)
    ml_result = None

    # Evaluate at 10-minute intervals
    cooldown_secs = cooldown_minutes * 60
    last_call_secs = -cooldown_secs  # allow first call immediately

    # Use score_progression (from PBP timeline) for exact scores at any point
    score_prog = record.get("score_progression", [])
    margin_traj = record.get("margin_trajectory", [])

    def _scores_at_minute(minute):
        """Get home/away scores at a game minute from score progression."""
        if minute >= 80:
            return home_score, away_score
        target_secs = minute * 60
        # Prefer score_progression — exact scores from PBP timeline
        if score_prog:
            h, a = 0, 0
            for p in score_prog:
                if p.get("gameSeconds", 0) > target_secs:
                    break
                h = p.get("homeScore", h)
                a = p.get("awayScore", a)
            return h, a
        # Fallback: derive from halftime scores + margin trajectory
        home_1h = record.get("home_score_1h")
        away_1h = record.get("away_score_1h")
        if minute <= 40 and home_1h is not None and away_1h is not None:
            progress = minute / 40
            return round(home_1h * progress), round(away_1h * progress)
        if minute > 40 and home_1h is not None and away_1h is not None:
            progress_2h = (minute - 40) / 40
            h2_home = home_score - home_1h
            h2_away = away_score - away_1h
            return home_1h + round(h2_home * progress_2h), away_1h + round(h2_away * progress_2h)
        # Last resort: linear interpolation
        progress = min(1.0, minute / 80)
        return round(home_score * progress), round(away_score * progress)

    # Build eval points from scoring events + fixed anchors (#25)
    eval_minutes = set()  # minute 0 excluded (no game context)
    if score_prog:
        for p in score_prog:
            gs = p.get("gameSeconds", 0)
            if gs > 0:
                eval_minutes.add(gs // 60)
    else:
        # Fallback: fixed 10-minute intervals when no PBP data
        eval_minutes.update(range(10, 81, 10))
    # Suppress halftime (minute 40) — no play active during break (#60/#61)
    eval_minutes.discard(40)
    # Include minute 80 only if game went to ET (scores tied) (#21/#61)
    if home_score == away_score:
        eval_minutes.add(80)
    else:
        eval_minutes.discard(80)
    eval_points = sorted(eval_minutes)

    for minute in eval_points:
        game_secs = minute * 60

        # Cooldown check
        if (game_secs - last_call_secs) < cooldown_secs:
            continue

        # Skip when score is still 0-0 (no game context to predict from)
        h_at, a_at = _scores_at_minute(minute)
        if h_at == 0 and a_at == 0:
            continue

        # Determine half (#21: minute 80 is end of H2, not ET)
        if minute <= 40:
            half = "H1"
        elif minute <= 80:
            half = "H2"
        else:
            half = "ET"

        # Skip minute 80 unless game actually went to extra time (#21)
        if minute == 80 and home_score != away_score:
            continue

        # Cumulative condition snapshot (#40 item 3):
        # PBP conditions with fire time: only include if fired by this minute
        # Stats-based conditions (no fire time): always include (cumulative)
        active_conds = []
        for c in conditions_fired:
            fire_secs = c.get("game_seconds_at_fire")
            if fire_secs is not None:
                if fire_secs <= game_secs:
                    active_conds.append(c)
            else:
                active_conds.append(c)

        # Get margin at this point from trajectory
        traj_idx = minute // 10
        if margin_traj and traj_idx < len(margin_traj):
            margin_at_point = margin_traj[traj_idx]  # home perspective
        else:
            # Fallback: use final margin scaled by time
            margin_at_point = int(round((home_score - away_score) * min(1.0, minute / 80)))

        # Compute blended probability
        home_pct = 50.0
        away_pct = 50.0
        consensus = ""
        basis = ""

        # ML component
        ml_home = None
        if ml_result:
            ml_home_pct = ml_result.get("home_win_pct", 50)
            ml_away_pct = ml_result.get("away_win_pct", 50)
            ml_home = ml_home_pct > ml_away_pct

        # Historical component
        hist_home = None
        if hist_stats and active_conds:
            home_conds = [c for c in active_conds if c.get("team_side") == "home" or c.get("team") == home]
            away_conds = [c for c in active_conds if c.get("team_side") == "away" or c.get("team") == away]
            h_pcts = [hist_stats.get(c.get("condition", ""), {}).get("win_pct", 50) for c in home_conds if hist_stats.get(c.get("condition", ""))]
            a_pcts = [hist_stats.get(c.get("condition", ""), {}).get("win_pct", 50) for c in away_conds if hist_stats.get(c.get("condition", ""))]
            h_avg = sum(h_pcts) / len(h_pcts) if h_pcts else 50
            a_avg = sum(a_pcts) / len(a_pcts) if a_pcts else 50
            hist_home = h_avg > a_avg

        # Blend
        ml_w = blend.get("ml", 0.7)
        hist_w = blend.get("historical", 0.3)

        if ml_result and hist_stats and active_conds:
            home_pct = ml_result["home_win_pct"] * ml_w + (h_avg if 'h_avg' in dir() else 50) * hist_w
            away_pct = ml_result["away_win_pct"] * ml_w + (a_avg if 'a_avg' in dir() else 50) * hist_w
            consensus = "strong" if ml_home == hist_home else "conflicted"
            basis = "combo"
        elif ml_result:
            home_pct = ml_result["home_win_pct"]
            away_pct = ml_result["away_win_pct"]
            consensus = "ml_only"
            basis = "ml"
        elif hist_stats and active_conds:
            home_pct = h_avg if 'h_avg' in dir() else 50
            away_pct = a_avg if 'a_avg' in dir() else 50
            consensus = "historical_only"
            basis = "historical"
        else:
            continue

        # Apply half weight
        half_w = half_weights.get(half, 1.0)
        if home_pct > away_pct:
            home_pct = min(99, home_pct * half_w)
            away_pct = 100 - home_pct
        else:
            away_pct = min(99, away_pct * half_w)
            home_pct = 100 - away_pct

        # Pick predicted team
        if home_pct >= away_pct:
            pred_team = home
            pred_pct = home_pct
            pred_margin = margin_at_point
        else:
            pred_team = away
            pred_pct = away_pct
            pred_margin = -margin_at_point

        # Threshold check
        if pred_pct < threshold:
            continue

        # Margin gate check
        gate = margin_gate_late if minute >= 60 else margin_gate
        if pred_margin < gate:
            continue

        # Compute avg_edge from condition edges (#117 — always compute, not just for gate)
        avg_edge = None
        if _edge_store and active_conds:
            edges = []
            for cf in active_conds:
                raw = _edge_store.edge(cf.get("condition", ""))
                if cf.get("team", "") != pred_team:
                    raw = -raw
                edges.append(raw)
            avg_edge = round(sum(edges) / len(edges), 2) if edges else 0.0

        # Edge gate check (#28) — suppress when avg condition edge too low
        if edge_gate is not None and avg_edge is not None:
            if avg_edge < float(edge_gate):
                continue

        # Duplicate suppression: skip if same team+score as last call (#60/#61)
        if calls:
            _last = calls[-1]
            if (_last.get("predicted_team") == pred_team
                    and _last.get("home_score_at_fire") == h_at
                    and _last.get("away_score_at_fire") == a_at):
                continue

        # Record call
        correct = pred_team == winner if winner else None
        pred_is_home = pred_team == home

        # Pregame odds from backfill_odds.py enrichment (#19)
        moneyline = ""
        spread = ""
        spread_role = ""
        bk_name = record.get("odds_bookmaker", "")
        hs = record.get("home_spread")
        if hs is not None:
            try:
                spread = str(float(hs) if pred_is_home else -float(hs))
                spread_role = "favorite" if (pred_is_home and float(hs) < 0) or (not pred_is_home and float(hs) > 0) else "underdog"
            except (TypeError, ValueError):
                pass
        hml = record.get("home_ml")
        aml = record.get("away_ml")
        if pred_is_home and hml is not None:
            moneyline = str(hml)
        elif not pred_is_home and aml is not None:
            moneyline = str(aml)

        # Compute polarity from active conditions (#117, #118)
        _pol_pos = 0
        _pol_neg = 0
        try:
            from outcomes import classify_polarity as _cp
            for cf in active_conds:
                ctype = cf.get("type", "")
                cname = cf.get("condition", cf.get("name", ""))
                pol = _cp(ctype, condition_name=cname)
                if pol == "pos":
                    _pol_pos += 1
                elif pol == "neg":
                    _pol_neg += 1
        except Exception:
            pass
        _pol_net = _pol_pos - _pol_neg

        # Derive quarter from game_minute (#117)
        _quarter = ""
        if minute is not None:
            if minute < 20: _quarter = "Q1"
            elif minute < 40: _quarter = "Q2"
            elif minute < 60: _quarter = "Q3"
            elif minute < 80: _quarter = "Q4"
            else: _quarter = "ET"

        call = {
            "ts": record.get("kickoff_utc", record.get("ts", "")),
            "predicted_team": pred_team,
            "pct": round(pred_pct, 1),
            "winner_score_pct": round(pred_pct, 1),
            "consensus": consensus,
            "blended": basis == "combo",
            "basis": basis,
            "half": half,
            "quarter": _quarter,
            "game_minute": minute,
            "score_margin_at_fire": pred_margin,
            "home_score_at_fire": _scores_at_minute(minute)[0],
            "away_score_at_fire": _scores_at_minute(minute)[1],
            "active_conditions": [
                {"name": c.get("condition", ""), "team": c.get("team", "")}
                for c in active_conds[:10]
            ],
            "spread_role": spread_role,
            "moneyline": moneyline,
            "spread": spread,
            "live_moneyline": moneyline,
            "live_spread": spread,
            "bk_moneyline": moneyline,
            "bk_spread": spread,
            "bk_name": bk_name,
            "avg_edge": avg_edge,
            "polarity_pos": _pol_pos,
            "polarity_neg": _pol_neg,
            "polarity_net": _pol_net,
            "correct": correct,
            "synthetic": True,
            "source": "backfill",
            "pw_version": pw_version,
        }
        calls.append(call)
        last_call_secs = game_secs

    return calls


def replay_season(season, config, dry_run=False, write_canonical=False,
                  skip_existing=False, resume=False,
                  pw_calls=False, pw_threshold=65, pw_cooldown_minutes=5,
                  pw_force=False):
    """Replay cached data for a season and build game history records."""
    _log("Phase 2: Replaying season {} {}{}...".format(
        season,
        "(dry-run)" if dry_run else "",
        "(write-canonical)" if write_canonical else "",
    ))

    # Load existing history
    existing_ids = set()
    history = []
    if os.path.isfile(HISTORY_FILE):
        try:
            with open(HISTORY_FILE) as f:
                history = json.load(f)
            existing_ids = {r.get("match_id") for r in history}
        except Exception:
            history = []

    # Resume state
    resume_after = None
    if resume:
        state = _load_backfill_state()
        resume_after = state.get("last_round_{}".format(season), 0)
        if resume_after:
            _log("  Resuming after round {}".format(resume_after))

    new_records = []
    skipped = 0
    processed = 0

    for rnd in range(1, MAX_FINALS_ROUND + 1):
        if resume_after and rnd <= resume_after:
            continue

        draw_file = "draw_round_{}.json".format(rnd)
        draw = _read_cache(season, draw_file)
        if not draw:
            if rnd > MAX_REGULAR_ROUND:
                break
            continue

        completed = [f for f in draw if f.get("matchState") in nrl_api.COMPLETED_STATES]
        if not completed:
            continue

        round_title = completed[0].get("roundTitle", "Round {}".format(rnd))
        is_finals = rnd > MAX_REGULAR_ROUND
        season_segment = "finals" if is_finals else "regular_season"

        for fix in completed:
            match_url = fix.get("matchCentreUrl", "")
            if not match_url:
                continue

            match_id = match_url

            # Skip existing — always deduplicate (#258 Phase 3.8)
            if match_id in existing_ids:
                skipped += 1
                continue

            slug = _match_slug(match_url)
            match_file = "match_{}_r{}.json".format(slug, rnd)
            match_data = _read_cache(season, match_file)

            if not match_data:
                _log("    WARN: no cached match data for {} R{}".format(slug, rnd))
                continue

            # Build record
            record = _build_game_record(fix, match_data, season, rnd,
                                        round_title, season_segment)

            # Evaluate conditions
            conditions_fired = evaluate_conditions_for_game(config, record)
            record["conditions_fired"] = conditions_fired

            # Compute postgame tags
            record["postgame_tags"] = compute_postgame_tags(record)

            # PW call generation (opt-in)
            if pw_calls:
                if pw_force or not record.get("predicted_winner_calls"):
                    # Lookahead-safe: only pass games with dates strictly before this game (#40)
                    game_date = (record.get("kickoff_utc") or record.get("ts") or "")[:10]
                    _prior = [r for r in history + new_records
                              if (r.get("kickoff_utc") or r.get("ts") or "")[:10] < game_date]
                    pw_call_list = _generate_pw_calls_for_record(
                        record, _prior, config,
                        pw_threshold, pw_cooldown_minutes)
                    record["predicted_winner_calls"] = pw_call_list
                    record["pw_call_count"] = len(pw_call_list)
                    correct = sum(1 for c in pw_call_list if c.get("correct") is True)
                    record["pw_correct_count"] = correct
                    record["pw_accuracy_pct"] = round(correct / len(pw_call_list) * 100, 1) if pw_call_list else 0

            record["source"] = "backfill"

            if dry_run:
                _print_record_summary(record)
            else:
                new_records.append(record)
                existing_ids.add(match_id)

            processed += 1

        # Update resume state
        if not dry_run:
            bf_state = _load_backfill_state()
            bf_state["last_round_{}".format(season)] = rnd
            _save_backfill_state(bf_state)

    # Write history
    if write_canonical and not dry_run and new_records:
        # Pre-backfill snapshot (#16 Phase 3)
        try:
            import runtime_backup
            snap_dir, _ = runtime_backup.snapshot_runtime_files(label="pre-backfill")
            _log("  Pre-backfill snapshot: {}".format(os.path.basename(snap_dir)))
        except Exception as e:
            _log("  WARN: Pre-backfill snapshot failed: {}".format(e))
        history.extend(new_records)
        # Sort by kickoff time
        history.sort(key=lambda r: r.get("kickoff_utc") or r.get("ts") or "")
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, HISTORY_FILE)
        _log("  Wrote {} new records to game_history.json (total: {})".format(
            len(new_records), len(history)))

    _log("Phase 2 complete: season {} — {} processed, {} skipped".format(
        season, processed, skipped))
    return processed


def _build_game_record(fixture, match_data, season, rnd, round_title, season_segment):
    """Build a game history record from fixture + match centre data."""
    home = match_data.get("homeTeam", {})
    away = match_data.get("awayTeam", {})
    home_scoring = home.get("scoring", {})
    away_scoring = away.get("scoring", {})

    home_team = home.get("nickName", "")
    away_team = away.get("nickName", "")
    home_score = home.get("score", 0) or 0
    away_score = away.get("score", 0) or 0
    home_1h = home_scoring.get("halfTimeScore")
    away_1h = away_scoring.get("halfTimeScore")

    margin = abs(home_score - away_score)
    if home_score > away_score:
        winner = home_team
    elif away_score > home_score:
        winner = away_team
    else:
        winner = None  # draw

    # Derive 2H scores
    home_2h = (home_score - home_1h) if home_1h is not None else None
    away_2h = (away_score - away_1h) if away_1h is not None else None

    # Extract kickoff time
    clock_data = fixture.get("clock", {})
    kickoff_iso = (clock_data.get("kickOffTimeLong", "")
                   or fixture.get("startTime", "")
                   or match_data.get("startTime", ""))
    kickoff_utc = None
    if kickoff_iso:
        try:
            dt = datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            kickoff_utc = dt.isoformat()
        except Exception:
            pass

    # Pre-game odds from fixture
    home_odds = fixture.get("homeTeam", {}).get("odds")
    away_odds = fixture.get("awayTeam", {}).get("odds")

    # Team stats from match.stats.groups
    home_stats = extract_team_stats_from_groups(match_data, "home")
    away_stats = extract_team_stats_from_groups(match_data, "away")

    # Scoring breakdowns
    home_tries = home_scoring.get("tries", {})
    away_tries = away_scoring.get("tries", {})
    home_try_count = home_tries.get("made", 0) if isinstance(home_tries, dict) else 0
    away_try_count = away_tries.get("made", 0) if isinstance(away_tries, dict) else 0

    home_convs = home_scoring.get("conversions", {})
    away_convs = away_scoring.get("conversions", {})
    home_conv_count = home_convs.get("made", 0) if isinstance(home_convs, dict) else 0
    away_conv_count = away_convs.get("made", 0) if isinstance(away_convs, dict) else 0

    # Timeline — extract try events and full PBP features
    timeline = match_data.get("timeline", [])
    home_team_id = home.get("teamId")
    away_team_id = away.get("teamId")

    try_events = []
    for ev in timeline:
        if ev.get("type") == "Try":
            try_events.append({
                "team_id": ev.get("teamId"),
                "game_seconds": ev.get("gameSeconds", 0),
                "team": home_team if ev.get("teamId") == home_team_id else away_team,
            })

    # Extract full PBP momentum features from timeline
    pbp = timeline_features.extract_features(timeline, home_team_id, away_team_id)

    record = {
        "match_id": fixture.get("matchCentreUrl", ""),
        "season": season,
        "round": rnd,
        "round_title": round_title,
        "season_segment": season_segment,
        "home_team": home_team,
        "away_team": away_team,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "home_score": home_score,
        "away_score": away_score,
        "home_score_1h": home_1h,
        "away_score_1h": away_1h,
        "home_score_2h": home_2h,
        "away_score_2h": away_2h,
        "margin": margin,
        "winner": winner,
        "home_odds": home_odds,
        "away_odds": away_odds,
        "venue": match_data.get("venue") or fixture.get("venue", ""),
        "venue_city": match_data.get("venueCity") or fixture.get("venueCity", ""),
        "kickoff_utc": kickoff_utc,
        "attendance": match_data.get("attendance"),
        "home_tries": home_try_count,
        "away_tries": away_try_count,
        "home_conversions": home_conv_count,
        "away_conversions": away_conv_count,
        "home_stats": home_stats,
        "away_stats": away_stats,
        # PBP features from timeline
        "home_max_consecutive_tries": pbp["home_max_consecutive_tries"],
        "away_max_consecutive_tries": pbp["away_max_consecutive_tries"],
        "home_max_unanswered_points": pbp["home_max_unanswered_points"],
        "away_max_unanswered_points": pbp["away_max_unanswered_points"],
        "home_max_error_streak": pbp["home_max_error_streak"],
        "away_max_error_streak": pbp["away_max_error_streak"],
        "home_max_penalty_window_10m": pbp["home_max_penalty_window_10m"],
        "away_max_penalty_window_10m": pbp["away_max_penalty_window_10m"],
        "home_sin_bins": pbp["home_sin_bins"],
        "away_sin_bins": pbp["away_sin_bins"],
        "home_line_breaks_timeline": pbp["home_line_breaks_timeline"],
        "away_line_breaks_timeline": pbp["away_line_breaks_timeline"],
        "home_max_line_break_surge_10m": pbp["home_max_line_break_surge_10m"],
        "away_max_line_break_surge_10m": pbp["away_max_line_break_surge_10m"],
        "home_errors_timeline": pbp["home_errors_timeline"],
        "away_errors_timeline": pbp["away_errors_timeline"],
        "home_forty_twenties": pbp["home_forty_twenties"],
        "away_forty_twenties": pbp["away_forty_twenties"],
        "halftime_margin": pbp["halftime_margin"],
        "h2_momentum": pbp["h2_momentum"],
        "margin_trajectory": pbp["margin_trajectory"],
        "score_progression": pbp.get("score_progression", []),
        "try_events": try_events,
        "ts": kickoff_utc or datetime.now(timezone.utc).isoformat(),
    }

    return record


def _compute_try_scoring_runs(try_events, home_team, away_team):
    """Compute max consecutive tries per team from try event list."""
    home_max = 0
    away_max = 0
    home_run = 0
    away_run = 0

    for ev in sorted(try_events, key=lambda e: e.get("game_seconds", 0)):
        if ev["team"] == home_team:
            home_run += 1
            away_run = 0
            home_max = max(home_max, home_run)
        else:
            away_run += 1
            home_run = 0
            away_max = max(away_max, away_run)

    return home_max, away_max


def _compute_unanswered_points(try_events, home_team, away_team,
                                home_scoring, away_scoring):
    """Estimate max unanswered points per team.

    Simplified: assumes each try is worth 6 points (conversion success unknown
    from timeline alone). Uses scoring.conversions.made for total conversions.
    """
    # Build point events — each try = 4pts + potential conversion
    # We approximate: assign 6 points per try (4+2 avg)
    # since exact conversion-per-try mapping isn't in timeline
    POINTS_PER_TRY = 6
    home_max = 0
    away_max = 0
    home_run_pts = 0
    away_run_pts = 0

    for ev in sorted(try_events, key=lambda e: e.get("game_seconds", 0)):
        if ev["team"] == home_team:
            home_run_pts += POINTS_PER_TRY
            away_run_pts = 0
            home_max = max(home_max, home_run_pts)
        else:
            away_run_pts += POINTS_PER_TRY
            home_run_pts = 0
            away_max = max(away_max, away_run_pts)

    return home_max, away_max


def _print_record_summary(record):
    """Print a one-line summary of a game record for dry-run mode."""
    tags = ",".join(record.get("postgame_tags", [])) or "—"
    conds = len(record.get("conditions_fired", []))
    margin = record.get("margin", 0)
    winner = record.get("winner") or "Draw"
    home_stats = record.get("home_stats", {})
    has_stats = bool(home_stats)
    print("  {} R{:2d}: {:15s} {:2d} - {:2d} {:15s} | {} by {:2d} | tags: {:20s} | conds: {:2d} | stats: {}".format(
        record.get("season", ""),
        record.get("round", 0),
        record.get("home_team", "")[:15],
        record.get("home_score", 0),
        record.get("away_score", 0),
        record.get("away_team", "")[:15],
        winner[:12],
        margin,
        tags,
        conds,
        "yes ({} keys)".format(len(home_stats)) if has_stats else "no",
    ))


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NRL Monitor — Historical Backfill")
    parser.add_argument("--fetch-cache", action="store_true",
                        help="Phase 1: Download draw + match data into local cache")
    parser.add_argument("--use-cache", action="store_true",
                        help="Phase 2: Replay from local cache")
    parser.add_argument("--write-canonical", action="store_true",
                        help="Write records to game_history.json (requires --use-cache)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing (use with --use-cache)")
    parser.add_argument("--season", type=str, default="2024,2025,2026",
                        help="Comma-separated seasons (default: 2024,2025,2026)")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip games already in game_history.json")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last processed round per season")
    parser.add_argument("--delay", type=float, default=0.4,
                        help="Request delay in seconds (default: 0.4)")
    parser.add_argument("--pw-calls", action="store_true",
                        help="Generate predicted winner calls for each game (opt-in)")
    parser.add_argument("--pw-threshold", type=float, default=65,
                        help="PW confidence threshold (default: 65)")
    parser.add_argument("--pw-cooldown-minutes", type=float, default=5,
                        help="PW cooldown between calls per team (default: 5)")
    parser.add_argument("--pw-force", action="store_true",
                        help="Regenerate PW calls even if already present")
    args = parser.parse_args()

    seasons = [int(s.strip()) for s in args.season.split(",")]

    if not args.fetch_cache and not args.use_cache:
        parser.error("Specify --fetch-cache (Phase 1) or --use-cache (Phase 2)")

    # Load config for condition evaluation
    config = {}
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                config = json.load(f)
        except Exception as e:
            _log("WARN: Could not load config: {}".format(e))

    total_fetched = 0
    total_processed = 0

    if args.fetch_cache:
        _log("=== Phase 1: Fetch & Cache ===")
        for season in seasons:
            n = fetch_cache_season(season, delay=args.delay)
            total_fetched += n
        _log("Fetch complete: {} total games cached".format(total_fetched))

    if args.use_cache:
        _log("=== Phase 2: Replay & Write ===")
        for season in seasons:
            n = replay_season(
                season, config,
                dry_run=args.dry_run,
                write_canonical=args.write_canonical,
                skip_existing=args.skip_existing,
                resume=args.resume,
                pw_calls=args.pw_calls,
                pw_threshold=args.pw_threshold,
                pw_cooldown_minutes=args.pw_cooldown_minutes,
                pw_force=args.pw_force,
            )
            total_processed += n
        _log("Replay complete: {} total games processed".format(total_processed))


if __name__ == "__main__":
    main()
