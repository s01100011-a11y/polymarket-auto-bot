#!/usr/bin/env python3
"""
backfill_pw_calls.py — Retroactively compute predicted_winner_calls for
historical game records that don't have them.

For each game in game_history.json that is missing predicted_winner_calls,
this script:
  1. Groups the game's conditions_fired by the quarter in which they fired.
  2. At each quarter snapshot (after Q1, Q2, Q3, Q4/OT), calls
     live_analysis_for_game() with all conditions fired up to that point.
  3. Applies the same threshold logic as the live monitor.
  4. Records a synthetic predicted_winner_call entry (with correct=True/False
     resolved from winner_id).

Lookahead-safe: the historical context passed to live_analysis_for_game()
is always restricted to records whose date precedes the game being analysed,
so the predictions reflect only information that would have been available
at the time.

Usage:
    python3 backfill_pw_calls.py [--threshold PCT] [--dry-run] [--game-id ID]
                                  [--since DATE] [--until DATE] [--force]
                                  [--quiet] [--progress-interval N]

Options:
    --threshold PCT        Win-probability threshold to record a call (default: 70)
    --dry-run              Print what would be written; don't modify game_history.json
    --game-id ID           Process only the specified game_id
    --since DATE           Process only games on or after DATE (YYYY-MM-DD)
    --until DATE           Process only games on or before DATE (YYYY-MM-DD)
    --force                Recompute even if the record already has predicted_winner_calls
    --quiet                Suppress per-game progress output
    --progress-interval N  Log progress + intermediate save every N games (default: 100)
    --no-dedup             Record a call every quarter (disable same-team dedup)
    --margin-gate N        Margin suppression threshold (default: -15, use 0 to disable)
    --polarity-gate N      Net polarity threshold (default: 0). Use -999 to disable
    --sub-quarter          Evaluate at each condition-fire time-point within quarters (#181)
    --cooldown N           Cooldown between calls in minutes (default: 5, used with --sub-quarter)

Can also be imported and called from backfill.py:
    from backfill_pw_calls import run_pw_backfill
    run_pw_backfill(records, threshold=70, force=False, quiet=False)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import league_config
import outcomes
import pw_trend

# Disable ML model during backfill — model.pkl is trained on all historical
# data, so using it here would leak future outcomes into past predictions.
# The backfill should rely solely on the historical frequency/Bayesian signal.
outcomes.ml_model = None

__version__ = "2.1.0"


def _merge_and_save(records):
    """Save records to game_history.json, merging in any new records that
    monitor.py may have appended while this backfill was running.

    Without this merge, a long-running backfill would overwrite the file with
    its stale in-memory snapshot, silently dropping newly recorded games.
    """
    known_ids = {str(r.get("game_id") or "") for r in records}
    fresh = outcomes.load_history(migrate=False)
    merged = 0
    for r in fresh:
        gid = str(r.get("game_id") or "")
        if gid and gid not in known_ids:
            records.append(r)
            known_ids.add(gid)
            merged += 1
    outcomes.save_history(records)
    if merged:
        print(f"[PW-BACKFILL] Merged {merged} new record(s) from monitor before save.", flush=True)


# Quarter order used to iterate snapshots.
QUARTER_ORDER = ["PRE", "Q1", "Q2", "Q3", "Q4", "OT1", "OT2", "OT3", "OT4"]

# (Removed: _QUARTER_END_SECONDS static dict replaced by _quarter_end_seconds() below)


def _quarter_rank(q):
    """Return sort key for a quarter string."""
    q = str(q or "").upper().strip()
    try:
        return QUARTER_ORDER.index(q)
    except ValueError:
        if q.startswith("OT"):
            try:
                n = int(q[2:])
                return len(QUARTER_ORDER) + n
            except ValueError:
                pass
        return 999


def _parse_date(val):
    """Parse an ISO date string to a date object, or None."""
    if not val:
        return None
    try:
        s = str(val).replace("Z", "+00:00")
        return datetime.fromisoformat(s).date()
    except (ValueError, TypeError):
        try:
            return datetime.strptime(str(val)[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None


def _build_active_hits_from_conditions(conditions, home_id, away_id, home_name, away_name):
    """
    Convert a list of conditions_fired entries into the active_hits format
    expected by live_analysis_for_game().
    """
    hits = []
    for c in conditions:
        cond_name = str(c.get("name") or "").strip()
        team = str(c.get("team") or "").strip()
        team_id = str(c.get("team_id") or "").strip()
        role = str(c.get("home_away_role") or "").strip().lower()

        if not role and team_id:
            if team_id == str(home_id):
                role = "home"
            elif team_id == str(away_id):
                role = "away"

        if not team:
            if role == "home":
                team = str(home_name or "")
            elif role == "away":
                team = str(away_name or "")

        if not cond_name or (not team and not team_id):
            continue

        hits.append({
            "condition":            cond_name,
            "team":                 team,
            "team_id":              team_id,
            "home_away_role":       role,
            "score_margin_at_fire": c.get("score_margin_at_fire"),
            "quarter_elapsed_pct":  c.get("quarter_elapsed_pct"),
        })
    return hits


def _quarter_seconds(q, summer_league=False):
    """Return duration in seconds for a quarter string."""
    if str(q).upper().startswith("OT"):
        return 300
    return 600 if summer_league else 720


def _quarter_end_seconds(q, summer_league=False):
    """Return cumulative seconds elapsed at the end of quarter q."""
    reg = 600 if summer_league else 720
    table = {
        "PRE": 0,
        "Q1": reg,
        "Q2": reg * 2,
        "Q3": reg * 3,
        "Q4": reg * 4,
        "OT1": reg * 4 + 300,
        "OT2": reg * 4 + 600,
        "OT3": reg * 4 + 900,
        "OT4": reg * 4 + 1200,
    }
    return table.get(str(q).upper().strip(), 0)


def _game_clock_from_qep(quarter_str, quarter_elapsed_pct, summer_league=False):
    """Derive game clock string from quarter_elapsed_pct."""
    try:
        q_secs = _quarter_seconds(quarter_str, summer_league=summer_league)
        remaining = q_secs * (1.0 - float(quarter_elapsed_pct) / 100.0)
        mins = int(remaining) // 60
        secs = int(remaining) % 60
        return "{}:{:02d}".format(mins, secs)
    except (TypeError, ValueError):
        return None


def _build_time_slices(conditions_fired, snapshot_quarters, sub_quarter=False, summer_league=False):
    """Build list of (quarter, elapsed_pct, cumulative_conds) evaluation points.

    When sub_quarter=False (default): one evaluation per quarter (legacy).
    When sub_quarter=True: one evaluation per unique condition-fire time-point,
    skipping points where the cumulative condition set hasn't changed.
    """
    slices = []
    if not sub_quarter:
        # Legacy: one slice per quarter at 100% elapsed
        for snap_q in snapshot_quarters:
            snap_rank = _quarter_rank(snap_q)
            cumulative = [
                c for c in conditions_fired
                if _quarter_rank(str(c.get("quarter_str") or "").upper().strip()) <= snap_rank
            ]
            if cumulative:
                slices.append((snap_q, 100.0, cumulative))
        return slices

    # Sub-quarter: iterate each time-point within each quarter
    for snap_q in snapshot_quarters:
        snap_rank = _quarter_rank(snap_q)
        prior_q_conds = [
            c for c in conditions_fired
            if _quarter_rank(str(c.get("quarter_str") or "").upper().strip()) < snap_rank
        ]
        this_q_conds = [
            c for c in conditions_fired
            if _quarter_rank(str(c.get("quarter_str") or "").upper().strip()) == snap_rank
        ]
        # Get unique elapsed_pct values sorted ascending
        time_points = sorted({
            round(float(c.get("quarter_elapsed_pct") or 0), 1)
            for c in this_q_conds
            if c.get("quarter_elapsed_pct") is not None
        })
        if not time_points:
            time_points = [100.0]
        # Skip quarter-end (100% elapsed) for Q1-Q3 — no plays happening (#215)
        if snap_q in ("Q1", "Q2", "Q3") and len(time_points) > 1:
            time_points = [tp for tp in time_points if tp < 100.0]
            if not time_points:
                time_points = [100.0]  # safety: keep if it was the only real time-point

        prev_cond_names = None
        for ep in time_points:
            cumulative = prior_q_conds + [
                c for c in this_q_conds
                if (c.get("quarter_elapsed_pct") or 0) <= ep + 0.05  # small epsilon
            ]
            if not cumulative:
                continue
            # Skip if condition set hasn't changed (optimization)
            cond_names = frozenset(str(c.get("name") or "") for c in cumulative)
            if cond_names == prev_cond_names:
                continue
            prev_cond_names = cond_names
            slices.append((snap_q, ep, cumulative))

    return slices


def _compute_pw_calls_for_record(rec, prior_records, threshold=70.0, cond_index=None,
                                  prebuilt_index=None, no_dedup=False, config=None,
                                  sub_quarter=False, cooldown_minutes=5,
                                  edge_gate=None, cond_edge_store=None,
                                  summer_league=False):
    """
    Compute synthetic predicted_winner_calls for a single game record.

    Uses per-quarter cumulative snapshots to match live monitor behaviour:
    at each quarter boundary, only conditions fired up to that point are
    passed to live_analysis_for_game().  A call is recorded at the first
    quarter where confidence crosses the threshold, and again if the
    predicted team flips in a later quarter.

    When sub_quarter=True (#181 Option A): evaluates at each unique
    condition-fire time-point within each quarter, with simulated cooldown
    between calls, producing call density closer to live monitor behaviour.

    prior_records: list of records whose date < rec's date (lookahead-safe).
    threshold: minimum winner_score_pct (or pct) to record a call.
    edge_gate: if set, suppress calls where avg condition edge < this value.
    cond_edge_store: dict {cond_name: [fires, team_won]} for rolling edge.

    Returns a tuple (calls, suppression_counts) where suppression_counts is
    a dict { "candidates": int, "below_threshold": int, "dedup": int }.
    """
    game_id   = str(rec.get("game_id") or "")
    winner_id = str(rec.get("winner_id") or "")
    home_id   = str(rec.get("home_id") or "")
    away_id   = str(rec.get("away_id") or "")
    home_name = str(rec.get("home_name") or "")
    away_name = str(rec.get("away_name") or "")
    game_date = str(rec.get("date") or "")[:10]

    sup = {"candidates": 0, "below_threshold": 0, "dedup": 0, "cooldown": 0, "edge_gate": 0}

    conditions_fired = [c for c in (rec.get("conditions_fired") or []) if isinstance(c, dict)]
    if not conditions_fired:
        return [], sup

    quarters_seen = set()
    for c in conditions_fired:
        q = str(c.get("quarter_str") or "").upper().strip()
        if q:
            quarters_seen.add(q)

    snapshot_quarters = sorted(
        [q for q in quarters_seen if q != "PRE"],
        key=_quarter_rank,
    )
    if not snapshot_quarters:
        return [], sup

    def _pw_score(row):
        ws = row.get("winner_score_pct")
        try:
            v = float(ws)
            if v == v:  # not NaN
                return v
        except (TypeError, ValueError):
            pass
        try:
            return float(row.get("pct") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Build evaluation time-slices
    time_slices = _build_time_slices(conditions_fired, snapshot_quarters, sub_quarter=sub_quarter, summer_league=summer_league)
    if not time_slices:
        return [], sup

    # Cooldown tracking for sub-quarter mode (per-team, matching live monitor)
    cooldown_secs = cooldown_minutes * 60
    last_call_game_secs_by_team = {}  # {team_id: game_secs}
    calls = []
    last_pred_team_id = None

    for snap_q, elapsed_pct, cumulative_conds in time_slices:
        snap_cond_names = list({str(c.get("name") or "").strip()
                                for c in cumulative_conds if c.get("name")})
        snap_hits = _build_active_hits_from_conditions(
            cumulative_conds, home_id, away_id, home_name, away_name,
        )
        if not snap_cond_names or not snap_hits:
            continue

        try:
            analysis = outcomes.live_analysis_for_game(
                game_id=game_id,
                active_conditions=snap_cond_names,
                records=prior_records,
                active_hits=snap_hits,
                _prebuilt_index=prebuilt_index,
                config=config,
            )
        except Exception:
            continue

        preds = analysis.get("predictions") or []
        team_win_rows = [
            p for p in preds
            if "team wins" in str(p.get("label", "")).lower()
            and str(p.get("predicted_team") or "").strip()
        ]
        if not team_win_rows:
            continue

        top_row    = max(team_win_rows, key=_pw_score)
        top_score  = _pw_score(top_row)
        pred_team    = str(top_row.get("predicted_team") or "").strip()
        pred_team_id = str(top_row.get("predicted_team_id") or pred_team).strip()

        sup["candidates"] += 1

        # Adaptive threshold by spread role (#195.2)
        _effective_threshold = threshold
        _pw_cfg = (config or {}).get("predicted_winner_alert") or {}
        _spread_fav_id = str(rec.get("spread_fav_id") or "").strip()
        if _spread_fav_id and pred_team_id:
            if pred_team_id == _spread_fav_id:
                _effective_threshold = float(_pw_cfg.get("threshold_pct_favorite", threshold))
            else:
                _effective_threshold = float(_pw_cfg.get("threshold_pct_underdog", threshold))

        if top_score < _effective_threshold or not pred_team:
            sup["below_threshold"] += 1
            continue

        # Filter: edge gate (#217) — suppress calls where avg condition edge < threshold
        if edge_gate is not None and cond_edge_store:
            # Build name->team_id map from cumulative conditions
            _cond_team_map = {}
            for c in cumulative_conds:
                _cn = str(c.get("name") or "").strip()
                _ct = str(c.get("team_id") or "").strip()
                if _cn and _ct:
                    _cond_team_map[_cn] = _ct
            _edges = []
            for c in cumulative_conds:
                _cn = str(c.get("name") or "").strip()
                if not _cn:
                    continue
                _es = cond_edge_store.get(_cn)
                if not _es or _es[0] < 20:
                    _e = 0.0
                else:
                    _e = (_es[1] / _es[0] * 100.0) - 50.0
                _ct = _cond_team_map.get(_cn, "")
                if _ct == pred_team_id:
                    _edges.append(_e)
                else:
                    _edges.append(-_e)
            _avg_edge = (sum(_edges) / len(_edges)) if _edges else 0.0
            if _avg_edge < edge_gate:
                sup["edge_gate"] += 1
                continue

        # Dedup: skip if same team as last call (unless --no-dedup)
        if not no_dedup and last_pred_team_id is not None and pred_team_id == last_pred_team_id:
            # In sub-quarter mode, allow same team after cooldown
            if not sub_quarter:
                sup["dedup"] += 1
                continue

        # Sub-quarter cooldown check (per-team, matching live monitor)
        if sub_quarter:
            snap_rank = _quarter_rank(snap_q)
            prior_secs = sum(_quarter_seconds(q, summer_league=summer_league) for q in snapshot_quarters if _quarter_rank(q) < snap_rank)
            game_secs = prior_secs + (elapsed_pct / 100.0) * _quarter_seconds(snap_q, summer_league=summer_league)
            last_secs = last_call_game_secs_by_team.get(pred_team_id)
            if last_secs is not None and (game_secs - last_secs) < cooldown_secs:
                sup["cooldown"] += 1
                continue

        consensus  = str(top_row.get("consensus") or "").strip()
        is_blended = top_row.get("winner_score_pct") is not None
        basis      = str(top_row.get("basis") or "").strip()

        correct = None
        if winner_id and pred_team_id:
            correct = (winner_id == pred_team_id)

        # Derive score/margin/clock from the closest condition at this time-slice
        _pw_margin = None
        _pw_home_score = None
        _pw_away_score = None
        _pw_game_clock = None
        _pred_conds = [
            c for c in cumulative_conds
            if str(c.get("team_id") or "").strip() == pred_team_id
            and c.get("score_margin_at_fire") is not None
        ]
        if _pred_conds:
            _pred_conds.sort(key=lambda c: _quarter_rank(str(c.get("quarter_str") or "").upper().strip()), reverse=True)
            _latest = _pred_conds[0]
            _pw_margin = _latest.get("score_margin_at_fire")
            _pw_home_score = _latest.get("home_score_at_fire")
            _pw_away_score = _latest.get("away_score_at_fire")
            _pw_game_clock = _latest.get("game_clock")
            if not _pw_game_clock:
                _qep = _latest.get("quarter_elapsed_pct")
                if _qep is not None:
                    _pw_game_clock = _game_clock_from_qep(
                        _latest.get("quarter_str", ""), _qep, summer_league=summer_league)
        # For sub-quarter mode, derive clock from the current time-slice
        if sub_quarter and not _pw_game_clock:
            _pw_game_clock = _game_clock_from_qep(snap_q, elapsed_pct, summer_league=summer_league)

        _pw_active_conds = []
        for c in cumulative_conds:
            _ac_name = str(c.get("name") or "")
            if not _ac_name:
                continue
            _ac_entry = {"name": _ac_name, "team": str(c.get("team") or "")}
            if cond_edge_store:
                _ac_edge = cond_edge_store.get(_ac_name)
                if _ac_edge and _ac_edge[0] >= 20:
                    _ac_entry["condition_edge"] = round((_ac_edge[1] / _ac_edge[0] * 100.0) - 50.0, 1)
            _pw_active_conds.append(_ac_entry)

        _spread_role = ""
        if _spread_fav_id and pred_team_id:
            _spread_role = "favorite" if pred_team_id == _spread_fav_id else "underdog"

        # Derive moneyline/spread for predicted team from pregame snapshot or game-level fields (#207)
        _live_ml = None
        _live_spread = None
        _snap = rec.get("pregame_snapshot") or {}
        _hmm = _snap.get("home_matchup_meta") or {}
        _amm = _snap.get("away_matchup_meta") or {}
        if pred_team_id == home_id:
            _live_ml = _hmm.get("moneyline") or str(rec.get("home_moneyline") or "") or None
            _live_spread = _hmm.get("spread") or None
        elif pred_team_id == away_id:
            _live_ml = _amm.get("moneyline") or str(rec.get("away_moneyline") or "") or None
            _live_spread = _amm.get("spread") or None
        if _live_ml in ("", "--"):
            _live_ml = None
        if _live_spread in ("", "--"):
            _live_spread = None

        # Compute avg_edge for payload (even when edge_gate is not active)
        _call_avg_edge = None
        if cond_edge_store:
            _cond_team_map2 = {}
            for c in cumulative_conds:
                _cn2 = str(c.get("name") or "").strip()
                _ct2 = str(c.get("team_id") or "").strip()
                if _cn2 and _ct2:
                    _cond_team_map2[_cn2] = _ct2
            _edges2 = []
            for c in cumulative_conds:
                _cn2 = str(c.get("name") or "").strip()
                if not _cn2:
                    continue
                _es2 = cond_edge_store.get(_cn2)
                if not _es2 or _es2[0] < 20:
                    _e2 = 0.0
                else:
                    _e2 = (_es2[1] / _es2[0] * 100.0) - 50.0
                _ct2 = _cond_team_map2.get(_cn2, "")
                if _ct2 == pred_team_id:
                    _edges2.append(_e2)
                else:
                    _edges2.append(-_e2)
            if _edges2:
                _call_avg_edge = round(sum(_edges2) / len(_edges2), 2)

        # Compute polarity from active conditions (#294)
        _pol_pos = 0
        _pol_neg = 0
        for c in _pw_active_conds:
            _ct_pol = str(c.get("team_id") or "").strip()
            if _ct_pol and _ct_pol != str(pred_team_id):
                continue
            _cn_pol = str(c.get("name") or "").strip()
            if not _cn_pol:
                continue
            _p = outcomes.classify_polarity(_cn_pol)
            if _p == "pos":
                _pol_pos += 1
            elif _p == "neg":
                _pol_neg += 1

        _call = {
            "ts":                game_date + "T00:00:00+00:00",
            "predicted_team":    pred_team,
            "predicted_team_id": pred_team_id,
            "pct":               round(top_score, 1),
            "consensus":         consensus,
            "blended":           is_blended,
            "basis":             basis,
            "quarter":           snap_q,
            "score_margin_at_fire": _pw_margin,
            "home_score_at_fire": _pw_home_score,
            "away_score_at_fire": _pw_away_score,
            "active_conditions": _pw_active_conds,
            "spread_role":       _spread_role,
            "live_moneyline":    _live_ml,
            "live_spread":       _live_spread,
            "correct":           correct,
            "synthetic":         True,
            "source":            "backfill",
            "pw_version":        pw_trend.resolve_pw_version((rec.get("date") or "")[:10] or None),
            "polarity_pos":      _pol_pos,
            "polarity_neg":      _pol_neg,
            "polarity_net":      _pol_pos - _pol_neg,
        }
        if _call_avg_edge is not None:
            _call["avg_edge"] = _call_avg_edge
        if _pw_game_clock:
            _call["game_clock"] = _pw_game_clock
        calls.append(_call)
        last_pred_team_id = pred_team_id
        if sub_quarter:
            snap_rank = _quarter_rank(snap_q)
            prior_secs = sum(_quarter_seconds(q, summer_league=summer_league) for q in snapshot_quarters if _quarter_rank(q) < snap_rank)
            last_call_game_secs_by_team[pred_team_id] = prior_secs + (elapsed_pct / 100.0) * _quarter_seconds(snap_q, summer_league=summer_league)

    return calls, sup


def run_pw_backfill(
    records=None,
    threshold=70.0,
    force=False,
    quiet=False,
    dry_run=False,
    game_id_filter=None,
    since_date=None,
    until_date=None,
    progress_interval=100,
    no_dedup=False,
    margin_gate=None,
    sub_quarter=False,
    cooldown_minutes=5,
    polarity_gate=None,
    edge_gate=None,
    season_segment=None,
):
    """
    Main entry point — can be called from backfill.py or run standalone.

    records: if provided, use this list instead of loading from disk.
             The list is modified in-place and saved back to disk unless
             dry_run=True.
    progress_interval: log progress + intermediate save every N games (0=off).
    Returns: dict { processed, skipped, written, calls_added, errors }
    """
    if records is None:
        records = outcomes.load_history(migrate=True)

    since = _parse_date(since_date) if since_date else None
    until = _parse_date(until_date) if until_date else None

    # Pre-build the BayesianOutcomeStore once and pin it for the entire run.
    # live_analysis_for_game() calls load_or_rebuild() internally, which checks
    # game_history.json's mtime fingerprint.  Our intermediate saves change the
    # mtime on every 100-game checkpoint, causing a full Bayes rebuild on every
    # subsequent call (O(n²) rebuilds).  We bypass this by monkey-patching
    # load_or_rebuild to always return our pre-built instance.
    if not quiet:
        print("Pre-building BayesianOutcomeStore (pinned for run)…", flush=True)
    try:
        _pinned_bayes = outcomes.BayesianOutcomeStore.rebuild_from_history(
            records=records, save=True
        )
        # Pin: replace classmethod with a closure that always returns our instance
        outcomes.BayesianOutcomeStore.load_or_rebuild = classmethod(
            lambda cls, records=None, _store=_pinned_bayes: _store
        )
        if not quiet:
            print("BayesianOutcomeStore pinned.", flush=True)
    except Exception as exc:
        if not quiet:
            print(f"[WARN] Bayes store pre-build failed: {exc}", flush=True)

    # Sort records by date for lookahead-safe slicing.
    dated = []
    for i, r in enumerate(records):
        d = _parse_date(r.get("date") or "")
        dated.append((d, i, r))
    dated.sort(key=lambda x: (x[0] or datetime.min.date(), x[2].get("game_id", "")))

    # When --season-segment is specified, implicitly force-reprocess matching records
    segment_force = bool(season_segment)

    # Count eligible games up front for accurate progress %.
    total_eligible = sum(
        1 for _, _, r in dated
        if r.get("winner_id")
        and r.get("conditions_fired")
        and (force or segment_force or not r.get("predicted_winner_calls"))
        and (not since or (_parse_date(r.get("date")) or datetime.min.date()) >= since)
        and (not until or (_parse_date(r.get("date")) or datetime.max.date()) <= until)
        and (not game_id_filter or str(r.get("game_id")) == str(game_id_filter))
        and (not season_segment or r.get("season_segment") == season_segment)
    )

    stats = {"processed": 0, "skipped": 0, "written": 0, "calls_added": 0, "errors": 0,
             "candidates": 0, "below_threshold": 0, "dedup": 0, "cooldown": 0, "edge_gate": 0}
    dirty_indices = set()
    run_start = datetime.now(timezone.utc)

    def _log_progress(final=False):
        elapsed = (datetime.now(timezone.utc) - run_start).total_seconds()
        rate = stats["processed"] / elapsed if elapsed > 0 else 0
        remaining = max(0, total_eligible - stats["processed"])
        eta_s = remaining / rate if rate > 0 and not final else 0
        eta_str = f"{int(eta_s/60)}m{int(eta_s%60):02d}s" if eta_s > 0 else "done"
        pct = stats["processed"] / total_eligible * 100 if total_eligible else 0
        tag = "DONE" if final else "PROGRESS"
        print(
            f"[PW-BACKFILL {tag}] {stats['processed']}/{total_eligible} ({pct:.1f}%)"
            f" | candidates={stats['candidates']} calls_added={stats['calls_added']}"
            f" below_threshold={stats['below_threshold']} dedup={stats['dedup']} cooldown={stats['cooldown']} edge_gate={stats['edge_gate']}"
            f" written={stats['written']}"
            f" errors={stats['errors']} skipped={stats['skipped']}"
            f" | elapsed={int(elapsed/60)}m{int(elapsed%60):02d}s eta={eta_str}",
            flush=True,
        )

    if not quiet:
        print(f"Eligible to process: {total_eligible} games", flush=True)

    # Incrementally-built prior list — O(n) instead of O(n²) slice per game.
    # We walk dated in order; before processing each game we add all records
    # whose date is strictly before the current game's date.
    prior_records = []
    prior_frontier = 0          # next index in dated[] to potentially add
    prior_date_boundary = None  # the date we've built up to
    # Inverted index built incrementally and passed to live_analysis_for_game()
    # so it doesn't rebuild on every call.
    from collections import defaultdict as _dd
    _cond_team_idx = _dd(set)
    _cond_idx = _dd(set)
    cond_index = {}  # kept for compat (unused)

    # Rolling condition edge store (#217): {cond_name: [fires, team_won]}
    # Updated incrementally as prior_records grows.
    _cond_edge_store = {}
    _edge_frontier = 0  # tracks which dated[] entries have been added

    for pos, (game_date, orig_idx, rec) in enumerate(dated):
        gid = str(rec.get("game_id") or "")

        # Advance the incremental prior up to (but not including) game_date.
        # Multiple games on the same date all share the same prior snapshot.
        if game_date is not None and game_date != prior_date_boundary:
            while prior_frontier < pos:
                d, _, r = dated[prior_frontier]
                if d is None or d < game_date:
                    new_idx = len(prior_records)
                    prior_records.append(r)
                    _r_winner = str(r.get("winner_id") or "").strip()
                    # Update inverted index and edge store incrementally
                    for cf in (r.get("conditions_fired") or []):
                        if not isinstance(cf, dict):
                            continue
                        tid = str(cf.get("team_id") or "").strip()
                        cn  = str(cf.get("name") or "").strip()
                        if tid and cn:
                            _cond_team_idx[(cn, tid)].add(new_idx)
                            _cond_idx[cn].add(new_idx)
                            # Update rolling edge store (#217)
                            if _r_winner:
                                if cn not in _cond_edge_store:
                                    _cond_edge_store[cn] = [0, 0]
                                _cond_edge_store[cn][0] += 1
                                _tw = cf.get("team_won")
                                if _tw is None:
                                    _tw = (tid == _r_winner)
                                if _tw:
                                    _cond_edge_store[cn][1] += 1
                    prior_frontier += 1
                else:
                    break
            prior_date_boundary = game_date

        # Filters
        if game_id_filter and gid != str(game_id_filter):
            continue
        if season_segment and rec.get("season_segment") != season_segment:
            stats["skipped"] += 1
            continue
        if since and game_date and game_date < since:
            stats["skipped"] += 1
            continue
        if until and game_date and game_date > until:
            stats["skipped"] += 1
            continue
        # segment_force: --season-segment implies --force for matching records
        if not force and not segment_force and rec.get("predicted_winner_calls") is not None:
            # Already processed (even if empty list) — skip unless --force/--season-segment
            stats["skipped"] += 1
            continue
        if not rec.get("winner_id"):
            stats["skipped"] += 1
            continue
        if not rec.get("conditions_fired"):
            stats["skipped"] += 1
            continue

        if not prior_records:
            stats["skipped"] += 1
            continue

        stats["processed"] += 1

        # Periodic progress log + intermediate save
        if progress_interval and stats["processed"] % progress_interval == 0:
            _log_progress()
            if dirty_indices and not dry_run:
                _merge_and_save(records)
                print(
                    f"[PW-BACKFILL] Intermediate save: {len(dirty_indices)} records written so far.",
                    flush=True,
                )

        try:
            prebuilt = (_cond_team_idx, _cond_idx, prior_records)
            _bf_config = {}
            if margin_gate is not None:
                _bf_config["pw_margin_gate"] = margin_gate
                _bf_config["pw_margin_gate_late"] = margin_gate  # use same value for late-game
            if polarity_gate is not None:
                _bf_config["pw_polarity_gate"] = polarity_gate
            is_sl = rec.get("season_segment") == "summer_league"
            calls, sup = _compute_pw_calls_for_record(rec, prior_records, threshold=threshold, cond_index=cond_index, prebuilt_index=prebuilt, no_dedup=no_dedup, config=_bf_config or None, sub_quarter=sub_quarter, cooldown_minutes=cooldown_minutes, edge_gate=edge_gate, cond_edge_store=_cond_edge_store, summer_league=is_sl)
        except Exception as exc:
            stats["errors"] += 1
            print(f"  [ERROR] {gid}: {exc}", flush=True)
            continue

        stats["candidates"] += sup["candidates"]
        stats["below_threshold"] += sup["below_threshold"]
        stats["dedup"] += sup["dedup"]
        stats["cooldown"] += sup.get("cooldown", 0)
        stats["edge_gate"] += sup.get("edge_gate", 0)

        if not quiet:
            away = rec.get("away_name", "?")
            home = rec.get("home_name", "?")
            date_str = str(game_date or "")
            if calls:
                call_str = ", ".join(
                    f"{c['predicted_team']} {c['pct']}%@{c['quarter']} "
                    f"{'✓' if c.get('correct') else ('✗' if c.get('correct') is False else '?')}"
                    for c in calls
                )
            else:
                call_str = "no calls"
            print(f"  {date_str}  {away} @ {home}  ({gid})  → {call_str}", flush=True)

        if calls:
            stats["calls_added"] += len(calls)
            stats["written"] += 1
            if not dry_run:
                records[orig_idx]["predicted_winner_calls"] = calls
                dirty_indices.add(orig_idx)
        else:
            # Mark as checked (empty list) so future runs skip unless --force
            if not dry_run:
                records[orig_idx]["predicted_winner_calls"] = []
                dirty_indices.add(orig_idx)

    if dirty_indices and not dry_run:
        _merge_and_save(records)
        if not quiet:
            print(f"\nSaved {len(dirty_indices)} records to game_history.json", flush=True)

    _log_progress(final=True)
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Retroactively compute predicted_winner_calls for historical game records."
    )
    parser.add_argument(
        "--threshold", type=float, default=70.0,
        help="Win-probability threshold to record a call (default: 70)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written; don't modify game_history.json"
    )
    parser.add_argument(
        "--game-id",
        help="Process only the specified game_id"
    )
    parser.add_argument(
        "--since",
        help="Process only games on or after DATE (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--until",
        help="Process only games on or before DATE (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Recompute even if the record already has predicted_winner_calls"
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-game progress output"
    )
    parser.add_argument(
        "--progress-interval", type=int, default=100,
        help="Log progress and intermediate save every N games (default: 100, 0=off)"
    )
    parser.add_argument(
        "--no-dedup", action="store_true",
        help="Disable same-team dedup — record a call every quarter even if predicted team hasn't changed"
    )
    parser.add_argument(
        "--margin-gate", type=float, default=None,
        help="Set margin suppression threshold (default: -15). Use 0 to disable margin gate entirely."
    )
    parser.add_argument(
        "--polarity-gate", type=int, default=None,
        help="Net polarity threshold for PW filter (default: 0 = positive only). "
             "Use -999 to disable polarity gate entirely."
    )
    parser.add_argument(
        "--sub-quarter", action="store_true",
        help="Enable sub-quarter snapshots (#181 Option A): evaluate at each condition-fire "
             "time-point within each quarter, with simulated cooldown. Produces call density "
             "closer to live monitor (~15 calls/game vs ~3). Runs ~4-5x slower."
    )
    parser.add_argument(
        "--cooldown", type=int, default=5,
        help="Cooldown between calls in minutes (default: 5). Only used with --sub-quarter."
    )
    parser.add_argument(
        "--edge-gate", type=float, default=None,
        help="Avg condition edge threshold (#217). Suppress calls where avg edge < value. "
             "Use 0 for positive-edge-only. Use -999 to disable."
    )
    parser.add_argument(
        "--season-segment", type=str, default=None,
        help="Only process games with this season_segment (e.g. summer_league, playoffs). "
             "Implies --force for matching records."
    )
    args = parser.parse_args()

    print(f"backfill_pw_calls.py v{__version__}", flush=True)
    opts = []
    opts.append(f"threshold={args.threshold}%")
    if args.since:
        opts.append(f"since={args.since}")
    if args.until:
        opts.append(f"until={args.until}")
    if args.game_id:
        opts.append(f"game_id={args.game_id}")
    if args.force:
        opts.append("force")
    if args.dry_run:
        opts.append("dry-run")
    if args.quiet:
        opts.append("quiet")
    if args.no_dedup:
        opts.append("no-dedup")
    if args.margin_gate is not None:
        opts.append(f"margin_gate={args.margin_gate}")
    if args.polarity_gate is not None:
        opts.append(f"polarity_gate={args.polarity_gate}")
    if args.sub_quarter:
        opts.append(f"sub-quarter (cooldown={args.cooldown}min)")
    if args.edge_gate is not None:
        opts.append(f"edge_gate={args.edge_gate}")
    if args.season_segment:
        opts.append(f"season_segment={args.season_segment}")
    opts.append(f"progress_interval={args.progress_interval}")
    print(f"Options: {', '.join(opts)}", flush=True)
    print(f"Command: {' '.join(sys.argv)}", flush=True)
    print(f"League:  {league_config.LEAGUE}", flush=True)
    print(f"Config:  {league_config.CONFIG_FILE}", flush=True)

    # Log active filters and confidence modifiers
    try:
        _diag_cfg = json.load(open(league_config.CONFIG_FILE))
    except Exception:
        _diag_cfg = {}
    _diag_pw = _diag_cfg.get("predicted_winner_alert") or {}
    _diag_mg = args.margin_gate if args.margin_gate is not None else -15
    _diag_mg_late = args.margin_gate if args.margin_gate is not None else -10
    _diag_pg = args.polarity_gate if args.polarity_gate is not None else 0
    print("\n=== Active Filters ===", flush=True)
    print(f"  Margin gate (full):     {_diag_mg} (trailing by {abs(_diag_mg)}+ pts suppressed)", flush=True)
    print(f"  Margin gate (Q3/Q4):    {_diag_mg_late} (trailing by {abs(_diag_mg_late)}+ pts suppressed)", flush=True)
    print(f"  Polarity gate:          net >= {_diag_pg}", flush=True)
    _diag_eg = args.edge_gate if args.edge_gate is not None else "disabled"
    print(f"  Edge gate (#217):       avg_edge >= {_diag_eg}", flush=True)
    print(f"  Threshold:              {args.threshold}%", flush=True)
    _diag_fav_t = _diag_pw.get("threshold_pct_favorite", args.threshold)
    _diag_und_t = _diag_pw.get("threshold_pct_underdog", args.threshold)
    if _diag_fav_t != args.threshold or _diag_und_t != args.threshold:
        print(f"  Adaptive threshold:     favorite={_diag_fav_t}%, underdog={_diag_und_t}%", flush=True)

    print("\n=== Confidence Modifiers (from config) ===", flush=True)
    _diag_qw = _diag_pw.get("pw_quarter_weights") or _diag_cfg.get("pw_quarter_weights")
    if _diag_qw:
        print(f"  Quarter weights:        {_diag_qw}", flush=True)
    _diag_mlb = _diag_pw.get("pw_margin_boost_leading") or _diag_cfg.get("pw_margin_boost_leading")
    _diag_mlbl = _diag_pw.get("pw_margin_boost_leading_late") or _diag_cfg.get("pw_margin_boost_leading_late")
    if _diag_mlb:
        print(f"  Leading boost:          {_diag_mlb} (late: {_diag_mlbl})", flush=True)
    _diag_csb = _diag_pw.get("pw_consensus_boost_strong") or _diag_cfg.get("pw_consensus_boost_strong")
    _diag_cpc = _diag_pw.get("pw_consensus_penalty_conflicted") or _diag_cfg.get("pw_consensus_penalty_conflicted")
    if _diag_csb or _diag_cpc:
        print(f"  Consensus boost/pen:    strong={_diag_csb}, conflicted={_diag_cpc}", flush=True)
    _diag_bl = _diag_pw.get("pw_blending") or _diag_cfg.get("pw_blending")
    if _diag_bl:
        print(f"  Bayesian blending:      {_diag_bl}", flush=True)
    print("", flush=True)

    print("Loading game history…", flush=True)
    records = outcomes.load_history(migrate=True)
    total = len(records)
    print(f"Total records: {total}", flush=True)
    if args.dry_run:
        print("DRY RUN — no changes will be written.\n", flush=True)

    stats = run_pw_backfill(
        records=records,
        threshold=args.threshold,
        force=args.force,
        quiet=args.quiet,
        dry_run=args.dry_run,
        game_id_filter=args.game_id,
        since_date=args.since,
        until_date=args.until,
        progress_interval=args.progress_interval,
        no_dedup=args.no_dedup,
        margin_gate=args.margin_gate,
        sub_quarter=args.sub_quarter,
        cooldown_minutes=args.cooldown,
        polarity_gate=args.polarity_gate,
        edge_gate=args.edge_gate,
        season_segment=args.season_segment,
    )

    print(
        f"\nDone.  processed={stats['processed']}  skipped={stats['skipped']}  "
        f"written={stats['written']}  calls_added={stats['calls_added']}  errors={stats['errors']}"
    )
    print(
        f"Filtering: candidates={stats['candidates']}  "
        f"below_threshold={stats['below_threshold']}  dedup={stats['dedup']}  "
        f"cooldown={stats['cooldown']}  edge_gate={stats['edge_gate']}  passed={stats['calls_added']}"
    )
    if args.dry_run:
        print("(dry run — no files modified)")


if __name__ == "__main__":
    main()
