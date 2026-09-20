#!/usr/bin/env python3
"""
pw_trend.py — PW call performance tracking over time.

Computes and stores snapshots of PW call quality metrics (accuracy, ROI,
win/loss, avg edge, etc.) linked to a changelog of PW logic improvements.

Data file: pw_trend_nrl.json
  {
    "changelog": [...],   # manual PW logic change entries with issue links
    "snapshots": [...]    # time-series of metric snapshots
  }

Usage:
    # Bootstrap from existing backups
    python3 pw_trend.py bootstrap [--backup-root DIR]

    # Append a snapshot from current game_history
    python3 pw_trend.py snapshot

    # Show current trend data summary
    python3 pw_trend.py show
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TREND_FILE = os.path.join(SCRIPT_DIR, "pw_trend_nrl.json")
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
STAKE = 100.0


def _quarter_from_minute(minute):
    """Derive synthetic quarter from game minute (#31)."""
    if minute is None:
        return ""
    if minute < 20:
        return "Q1"
    if minute < 40:
        return "Q2"
    if minute < 60:
        return "Q3"
    if minute < 80:
        return "Q4"
    return "ET"


# ── Decimal odds helpers ────────────────────────────────────────────────

def _decimal_win_profit(dec_odds):
    """Profit on a $100 winning bet at the given decimal odds."""
    if dec_odds is None:
        return STAKE  # fallback: even money
    try:
        dec_odds = float(dec_odds)
    except (TypeError, ValueError):
        return STAKE
    if dec_odds <= 1.0:
        return STAKE
    return STAKE * (dec_odds - 1.0)


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Game result helpers ─────────────────────────────────────────────────

def _final_team_margin(rec, pred_team):
    """Return final margin from the predicted team's perspective, or None."""
    winner = rec.get("winner", "")
    home = rec.get("home_team", "")
    away = rec.get("away_team", "")
    home_score = rec.get("home_score")
    away_score = rec.get("away_score")
    if home_score is None or away_score is None:
        return None
    try:
        home_score, away_score = int(home_score), int(away_score)
    except (ValueError, TypeError):
        return None
    if pred_team == home:
        return home_score - away_score
    elif pred_team == away:
        return away_score - home_score
    return None


def _did_cover_live_line(rec, pw):
    """Return True/False/None — synthetic live line (pregame_spread - margin)."""
    pred_team = (pw.get("predicted_team") or "").strip()
    spread = pw.get("live_spread") or pw.get("spread")
    margin = pw.get("score_margin_at_fire")
    if not pred_team or not spread or margin is None:
        return None
    team_margin = _final_team_margin(rec, pred_team)
    if team_margin is None:
        return None
    try:
        live_line = float(spread) - float(margin)
    except (ValueError, TypeError):
        return None
    return (team_margin + live_line) > 0


def _did_cover_bk_line(rec, pw):
    """Return True/False/None — bookmaker live spread at fire time."""
    pred_team = (pw.get("predicted_team") or "").strip()
    bk_spread = pw.get("bk_spread")
    if not pred_team or bk_spread is None:
        return None
    team_margin = _final_team_margin(rec, pred_team)
    if team_margin is None:
        return None
    try:
        return (team_margin + float(bk_spread)) > 0
    except (ValueError, TypeError):
        return None


# ── Call selection ──────────────────────────────────────────────────────

def _apply_call_selection(call_list, sel):
    """Apply call_selection filter per game."""
    if not sel or sel == "all":
        return call_list
    by_game = {}
    for c in call_list:
        by_game.setdefault(c.get("game_id", ""), []).append(c)
    result = []
    for game_calls in by_game.values():
        if sel == "first":
            result.append(min(game_calls, key=lambda c: c.get("ts") or ""))
        elif sel == "last":
            result.append(max(game_calls, key=lambda c: c.get("ts") or ""))
        elif sel == "first_per_half":
            by_h = {}
            for c in game_calls:
                h = c.get("half", "")
                if h not in by_h or (c.get("ts") or "") < (by_h[h].get("ts") or ""):
                    by_h[h] = c
            result.extend(by_h.values())
        elif sel == "first_per_quarter":
            by_q = {}
            for c in game_calls:
                q = c.get("quarter", "")
                if q not in by_q or (c.get("ts") or "") < (by_q[q].get("ts") or ""):
                    by_q[q] = c
            result.extend(by_q.values())
    return result


# ── Snapshot computation ────────────────────────────────────────────────

def compute_pw_snapshot(history, snapshot_date=None):
    """Compute PW metrics from a game_history list.

    Returns a snapshot dict with cumulative + rolling window metrics,
    per-role breakdown (favorite/underdog), and per-version cohorts.
    """
    snapshot_date = snapshot_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    calls = _collect_calls(history)

    _EMPTY = {
        "date": snapshot_date,
        "games_in_sample": 0,
        "date_range": {"from": None, "to": None},
        "pw_calls": 0, "correct": 0, "accuracy": 0.0,
        "roi_flat": 0.0, "roi_ml": 0.0, "roi_live_line": 0.0, "roi_bk_line": 0.0,
        "flat_pnl_units": 0.0, "ml_pnl_units": 0.0, "ll_pnl_units": 0.0, "bk_ll_pnl_units": 0.0,
        "avg_edge": None, "avg_pct": None,
        "window_30": None, "window_60": None, "window_90": None,
        "versions": {}, "roles": {},
    }
    if not calls:
        return _EMPTY

    games_with_pw = set()
    for rec in history:
        if rec.get("predicted_winner_calls"):
            games_with_pw.add(rec.get("game_id", id(rec)))

    dates = [c["date"] for c in calls if c["date"]]

    def _compute_metrics(call_list):
        total = len(call_list)
        if total == 0:
            return None
        wins = sum(1 for c in call_list if c["correct"])
        flat_pnl = sum(1 if c["correct"] else -1 for c in call_list)
        ml_pnl = 0.0
        ml_bets = 0
        ll_pnl = 0.0
        ll_bets = 0
        bk_ll_pnl = 0.0
        bk_ll_bets = 0
        bk_odds_pnl = 0.0
        bk_odds_bets = 0
        for c in call_list:
            ml = c["ml"]
            if ml is not None:
                ml_bets += 1
                ml_pnl += _decimal_win_profit(ml) if c["correct"] else -STAKE
            lc = c.get("live_cover")
            if lc is not None:
                ll_bets += 1
                ll_pnl += STAKE if lc else -STAKE  # flat unit for spread covers
            bkc = c.get("bk_cover")
            if bkc is not None:
                bk_ll_bets += 1
                _bk_sp = c.get("bk_spr_price")
                _bk_spr_win = _decimal_win_profit(_bk_sp) if _bk_sp is not None and _bk_sp > 1.0 else STAKE
                bk_ll_pnl += _bk_spr_win if bkc else -STAKE
            bk_ml = c.get("bk_ml")
            if bk_ml is not None and bk_ml > 1.0:
                bk_odds_bets += 1
                bk_odds_pnl += _decimal_win_profit(bk_ml) if c["correct"] else -STAKE
        edges = [c["avg_edge"] for c in call_list if c["avg_edge"] is not None]
        pcts = [c["pct"] for c in call_list if c["pct"] is not None]
        game_ids = set(c.get("game_id", "") for c in call_list if c.get("game_id"))
        return {
            "calls": total,
            "games_in_sample": len(game_ids),
            "correct": wins,
            "accuracy": round(wins / total * 100, 1) if total else 0.0,
            "roi_flat": round(flat_pnl / total * 100, 1) if total else 0.0,
            "roi_ml": round(ml_pnl / ml_bets * 100, 1) if ml_bets else 0.0,
            "roi_live_line": round(ll_pnl / ll_bets * 100, 1) if ll_bets else 0.0,
            "roi_bk_line": round(bk_ll_pnl / bk_ll_bets * 100, 1) if bk_ll_bets else 0.0,
            "roi_bk_odds": round(bk_odds_pnl / bk_odds_bets * 100, 1) if bk_odds_bets else None,
            "flat_pnl_units": round(flat_pnl, 2),
            "ml_pnl_units": round(ml_pnl / STAKE, 2) if ml_bets else 0.0,
            "ll_pnl_units": round(ll_pnl / STAKE, 2) if ll_bets else 0.0,
            "bk_ll_pnl_units": round(bk_ll_pnl / STAKE, 2) if bk_ll_bets else 0.0,
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
            "avg_pct": round(sum(pcts) / len(pcts), 1) if pcts else None,
        }

    cumulative = _compute_metrics(calls)

    # Rolling windows: last N game-dates (most recent first)
    by_date = {}
    for c in calls:
        by_date.setdefault(c["date"], []).append(c)
    sorted_dates = sorted(by_date.keys(), reverse=True)

    def _window_metrics(n_games):
        window_dates = sorted_dates[:n_games]
        window_calls = []
        for d in window_dates:
            window_calls.extend(by_date[d])
        m = _compute_metrics(window_calls)
        if m:
            m["games"] = len(window_dates)
        return m

    # Per-role breakdown (favorite / underdog)
    roles = {}
    for role_key in ("favorite", "underdog"):
        role_calls = [c for c in calls if c["spread_role"] == role_key]
        m = _compute_metrics(role_calls)
        if m:
            roles[role_key] = m
            rd = {}
            for c in role_calls:
                rd.setdefault(c["date"], []).append(c)
            rsd = sorted(rd.keys(), reverse=True)
            for wn in (30, 60, 90):
                wd = rsd[:wn]
                wc = []
                for d in wd:
                    wc.extend(rd[d])
                wm = _compute_metrics(wc)
                if wm:
                    wm["games"] = len(wd)
                m["window_{}".format(wn)] = wm

    # Per-version cohort metrics
    by_version = {}
    for c in calls:
        v = c.get("pw_version") or "untagged"
        by_version.setdefault(v, []).append(c)
    versions = {}
    for v, v_calls in sorted(by_version.items()):
        m = _compute_metrics(v_calls)
        if m:
            v_dates = [c["date"] for c in v_calls if c["date"]]
            m["games"] = len(set(c.get("game_id", "") for c in v_calls if c.get("game_id")))
            m["date_range"] = {
                "from": min(v_dates) if v_dates else None,
                "to": max(v_dates) if v_dates else None,
            }
            versions[v] = m

    # Per-edge-range breakdown (#122)
    def _edge_bucket(edge_val):
        if edge_val is None:
            return None
        if edge_val >= 10:
            return "10+"
        if edge_val >= 5:
            return "5-10"
        if edge_val >= 0:
            return "0-5"
        return "<0"

    by_edge = {}
    for c in calls:
        eb = _edge_bucket(c.get("avg_edge"))
        if eb is None:
            continue
        by_edge.setdefault(eb, []).append(c)
    edges = {}
    for eb, eb_calls in sorted(by_edge.items()):
        m = _compute_metrics(eb_calls)
        if m:
            m["games"] = len(set(c.get("game_id", "") for c in eb_calls if c.get("game_id")))
            edges[eb] = m

    # Cross-dimensional filter breakdowns
    _ROLES = ("all", "favorite", "underdog")
    _HALVES = ("all", "H1", "H2", "ET")
    _QUARTERS = ("all", "Q1", "Q2", "Q3", "Q4", "ET")
    _MARGINS = ("all", "trailing", "leading")
    _CALL_SELS = ("all", "first", "last", "first_per_half", "first_per_quarter")

    def _compute_filter_entry(subset):
        fm = _compute_metrics(subset)
        if fm is None:
            return None
        fd = {}
        for c in subset:
            fd.setdefault(c["date"], []).append(c)
        fsd = sorted(fd.keys(), reverse=True)
        for wn in (30, 60, 90):
            wd = fsd[:wn]
            wc = []
            for d in wd:
                wc.extend(fd[d])
            wm = _compute_metrics(wc)
            if wm:
                wm["games"] = len(wd)
            fm["window_{}".format(wn)] = wm
        return fm

    filters = {}
    # Half-based filter combos (backward compatible key format)
    for f_role in _ROLES:
        for f_half in _HALVES:
            for f_margin in _MARGINS:
                for f_csel in _CALL_SELS:
                    if f_role == "all" and f_half == "all" and f_margin == "all" and f_csel == "all":
                        continue
                    subset = _filter_calls(calls, f_role, f_half, f_margin, f_csel)
                    if not subset:
                        continue
                    fm = _compute_filter_entry(subset)
                    if fm:
                        filters["{}|{}|{}|{}".format(f_role, f_half, f_csel, f_margin)] = fm
    # Quarter-based filter combos (#31)
    for f_role in _ROLES:
        for f_quarter in _QUARTERS:
            if f_quarter == "all":
                continue
            for f_margin in _MARGINS:
                for f_csel in _CALL_SELS:
                    subset = _filter_calls(calls, f_role, quarter=f_quarter, margin=f_margin, call_sel=f_csel)
                    if not subset:
                        continue
                    fm = _compute_filter_entry(subset)
                    if fm:
                        filters["{}|q:{}|{}|{}".format(f_role, f_quarter, f_csel, f_margin)] = fm

    return {
        "date": snapshot_date,
        "games_in_sample": len(games_with_pw),
        "date_range": {
            "from": min(dates) if dates else None,
            "to": max(dates) if dates else None,
        },
        "pw_calls": cumulative["calls"],
        "correct": cumulative["correct"],
        "accuracy": cumulative["accuracy"],
        "roi_flat": cumulative["roi_flat"],
        "roi_ml": cumulative["roi_ml"],
        "roi_live_line": cumulative["roi_live_line"],
        "roi_bk_line": cumulative["roi_bk_line"],
        "flat_pnl_units": cumulative["flat_pnl_units"],
        "ml_pnl_units": cumulative["ml_pnl_units"],
        "ll_pnl_units": cumulative["ll_pnl_units"],
        "bk_ll_pnl_units": cumulative["bk_ll_pnl_units"],
        "avg_edge": cumulative["avg_edge"],
        "avg_pct": cumulative["avg_pct"],
        "window_30": _window_metrics(30),
        "window_60": _window_metrics(60),
        "window_90": _window_metrics(90),
        "versions": versions,
        "roles": roles,
        "edges": edges,
        "filters": filters,
    }


# ── Call collection ─────────────────────────────────────────────────────

def _collect_calls(history):
    """Extract resolved PW calls from game_history with metadata fields."""
    calls = []
    for rec in history:
        game_date = (rec.get("date") or rec.get("kickoff_utc") or rec.get("ts") or "")[:10]
        game_id = str(rec.get("game_id") or rec.get("match_id") or id(rec))
        for pw in (rec.get("predicted_winner_calls") or []):
            if not isinstance(pw, dict):
                continue
            correct = pw.get("correct")
            if correct is None:
                continue
            if pw.get("_suppressed"):
                continue
            live_cover = _did_cover_live_line(rec, pw)
            bk_cover = _did_cover_bk_line(rec, pw)
            bk_spr_price = pw.get("bk_spread_price")
            smaf = pw.get("score_margin_at_fire")
            margin_sign = None
            if smaf is not None:
                margin_sign = "trailing" if smaf <= 0 else "leading"
            # Derive spread_role from game record when missing (#40)
            _role = (pw.get("spread_role") or "").strip().lower()
            _ml = _safe_float(pw.get("moneyline"))
            if not _role:
                _pred = (pw.get("predicted_team") or "").strip()
                _home = rec.get("home_team", "")
                _hs = rec.get("home_spread")
                if _hs is not None and _pred:
                    try:
                        _pih = _pred == _home
                        _role = "favorite" if (_pih and float(_hs) < 0) or (not _pih and float(_hs) > 0) else "underdog"
                    except (TypeError, ValueError):
                        pass
                if _ml is None and _pred:
                    _ml = _safe_float(rec.get("home_ml") if _pred == _home else rec.get("away_ml"))
            calls.append({
                "correct": bool(correct),
                "ml": _ml,
                "bk_ml": _safe_float(pw.get("bk_moneyline")),
                "avg_edge": pw.get("avg_edge"),
                "pct": pw.get("pct") or pw.get("winner_score_pct"),
                "date": game_date,
                "pw_version": pw.get("pw_version", ""),
                "spread_role": _role,
                "live_cover": live_cover,
                "bk_cover": bk_cover,
                "bk_spr_price": _safe_float(bk_spr_price),
                "half": str(pw.get("half") or "").strip().upper(),
                "quarter": pw.get("quarter") or _quarter_from_minute(pw.get("game_minute")),
                "margin_sign": margin_sign,
                "ts": pw.get("ts") or "",
                "game_id": game_id,
            })
    return calls


def _filter_calls(calls, role="all", half="all", margin="all", call_sel="all",
                   quarter="all"):
    """Apply role/half/quarter/margin filters then call_selection."""
    subset = calls
    if role != "all":
        subset = [c for c in subset if c["spread_role"] == role]
    if quarter != "all":
        subset = [c for c in subset if c.get("quarter", "") == quarter]
    elif half != "all":
        if half == "ET":
            subset = [c for c in subset if c["half"].startswith("ET")]
        else:
            subset = [c for c in subset if c["half"] == half]
    if margin != "all":
        subset = [c for c in subset if c.get("margin_sign") == margin]
    return _apply_call_selection(subset, call_sel)


# ── Trend data I/O ─────────────────────────────────────────────────────

def load_trend_data():
    """Load pw_trend_nrl.json, returning default structure if missing."""
    try:
        with open(TREND_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("changelog", [])
        data.setdefault("snapshots", [])
        # Merge any new entries from _default_changelog() into existing data
        existing_tags = {c.get("tag") for c in data["changelog"]}
        for entry in _default_changelog():
            if entry.get("tag") not in existing_tags:
                data["changelog"].append(entry)
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"changelog": _default_changelog(), "snapshots": []}


def _default_changelog():
    """Known PW logic changes for NRL Monitor."""
    return [
        {"date": "2026-04-20", "tag": "v1-pw-launch",
         "description": "Predicted Winner feature launched -- Bayesian blending + ML consensus + per-phase eval cadence",
         "issues": ["#15"]},
        {"date": "2026-05-04", "tag": "v2-pw-tab-redesign",
         "description": "PW tab redesigned with NBA-style game cards, PBP score progression for backfill",
         "issues": ["#17"]},
        {"date": "2026-05-10", "tag": "v3-nba-parity",
         "description": "NBA parity -- ROI metrics, pregame odds, BK fields, 15-column call table",
         "issues": ["#19"]},
        {"date": "2026-05-15", "tag": "v4-score-trigger",
         "description": "Score-change triggered PW eval bypassing cooldown",
         "issues": ["#24"]},
        {"date": "2026-05-18", "tag": "v5-scoring-moments",
         "description": "PW backfill evaluates at actual scoring moments from PBP timeline",
         "issues": ["#25"]},
        {"date": "2026-05-29", "tag": "v6-live-pbp-parity",
         "description": "Live PBP features enabled -- 6 PBP conditions now fire during live games, polarity classification fixed for direction-dependent conditions, quarter-level stat scoping",
         "issues": ["#118", "#119", "#120"]},
    ]


def resolve_pw_version(date_str=None):
    """Return the pw_version tag for a given date (YYYY-MM-DD).

    Walks the changelog in reverse and returns the tag of the most recent
    entry on or before the given date.  Falls back to 'v0-unknown'.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    else:
        date_str = str(date_str)[:10]
    data = load_trend_data()
    changelog = sorted(data.get("changelog") or _default_changelog(),
                       key=lambda c: c.get("date", ""))
    result = "v0-unknown"
    for entry in changelog:
        if entry.get("date", "") <= date_str:
            result = entry.get("tag", result)
    return result


def current_pw_version():
    """Return the pw_version tag for today."""
    return resolve_pw_version()


def save_trend_data(data):
    """Atomically write trend data to disk."""
    tmp = TREND_FILE + ".tmp.{}".format(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, TREND_FILE)


def _bucket_key(date_str, interval):
    """Map a YYYY-MM-DD date to a bucket key based on interval."""
    if interval == "weekly":
        # ISO week: Monday-based
        d = datetime.strptime(date_str, "%Y-%m-%d")
        iso = d.isocalendar()
        return "{}-W{:02d}".format(iso[0], iso[1])
    elif interval == "monthly":
        return date_str[:7]  # YYYY-MM
    else:
        return date_str  # daily = per game date


def _parse_minute_interval(ival):
    """Parse minute interval like '5min' -> 5, or None if not minute-based."""
    if not ival:
        return None
    import re
    m = re.match(r'^(\d+)min$', ival.lower())
    return int(m.group(1)) if m else None


def _compute_minute_series(history, date_from, date_to, last_n_games,
                           minute_bucket, cumulative):
    """Compute trend series grouped by game_minute buckets.

    Each data point represents a game-minute range (e.g. 0-5', 5-10'),
    showing PW call performance by when during the game the call was made.
    """
    # Filter and collect all calls
    filtered = []
    for rec in history:
        gd = (rec.get("date") or rec.get("kickoff_utc") or rec.get("ts") or "")[:10]
        if not gd:
            continue
        if date_from and gd < date_from:
            continue
        if date_to and gd > date_to:
            continue
        if not rec.get("predicted_winner_calls"):
            continue
        filtered.append(rec)

    if not filtered:
        return []

    filtered.sort(key=lambda r: (r.get("date") or r.get("kickoff_utc") or "")[:10])
    if last_n_games and last_n_games > 0:
        filtered = filtered[-last_n_games:]

    calls = _collect_calls(filtered)
    if not calls:
        return []

    # Group calls by game_minute bucket
    buckets = {}  # bucket_start -> list of calls
    for c in calls:
        gm = c.get("game_minute")
        if gm is None:
            # Try to extract from ts within game context
            continue
        bk_start = (gm // minute_bucket) * minute_bucket
        buckets.setdefault(bk_start, []).append(c)

    # Also need game_minute from PW call records
    # Re-collect with game_minute
    calls_with_minute = []
    for rec in filtered:
        gd = (rec.get("date") or rec.get("kickoff_utc") or rec.get("ts") or "")[:10]
        game_id = str(rec.get("game_id") or id(rec))
        for pw in (rec.get("predicted_winner_calls") or []):
            if not isinstance(pw, dict):
                continue
            correct = pw.get("correct")
            if correct is None:
                continue
            if pw.get("_suppressed"):
                continue
            gm = pw.get("game_minute")
            if gm is None:
                continue
            bk_start = (gm // minute_bucket) * minute_bucket
            calls_with_minute.append((bk_start, pw, rec))

    if not calls_with_minute:
        return []

    # Group by bucket
    by_bucket = {}
    for bk_start, pw, rec in calls_with_minute:
        by_bucket.setdefault(bk_start, []).append((pw, rec))

    sorted_buckets = sorted(by_bucket.keys())

    # Build series — use _compute_metrics-like logic per bucket
    series = []
    cumulative_calls = []

    for bk_start in sorted_buckets:
        bucket_pairs = by_bucket[bk_start]
        # Build a mini-history for compute_pw_snapshot
        # Instead, compute metrics directly from calls
        bucket_recs = []
        seen_games = set()
        for pw, rec in bucket_pairs:
            gid = str(rec.get("game_id") or id(rec))
            if gid not in seen_games:
                # Create a synthetic record with just this call
                seen_games.add(gid)
            bucket_recs.append({"rec": rec, "pw": pw})

        # Compute metrics manually for this bucket
        total = len(bucket_pairs)
        wins = sum(1 for pw, rec in bucket_pairs if pw.get("correct"))
        flat_pnl = sum(1 if pw.get("correct") else -1 for pw, rec in bucket_pairs)

        if cumulative:
            cumulative_calls.extend(bucket_pairs)
            total = len(cumulative_calls)
            wins = sum(1 for pw, rec in cumulative_calls if pw.get("correct"))
            flat_pnl = sum(1 if pw.get("correct") else -1 for pw, rec in cumulative_calls)

        label = "{}-{}'".format(bk_start, bk_start + minute_bucket)
        snap = {
            "date": label,
            "label": label,
            "games_in_sample": len(set(id(rec) for _, rec in (cumulative_calls if cumulative else bucket_pairs))),
            "calls": total,
            "pw_calls": total,
            "correct": wins,
            "accuracy": round(wins / total * 100, 1) if total else 0.0,
            "roi_flat": round(flat_pnl / total * 100, 1) if total else 0.0,
            "flat_pnl_units": round(flat_pnl, 2),
        }
        series.append(snap)

    return series


def compute_trend_series(history, date_from=None, date_to=None,
                         last_n_games=None, interval=None,
                         cumulative=True):
    """Compute trend data points at specified intervals.

    Args:
        interval: 'daily' (default), 'weekly', 'monthly',
                  '1min', '5min', '15min', '30min', '60min'.
        cumulative: If True (default), each data point includes all data
                    up to that point. If False, shows only that bucket.
    """
    # Handle minute-based intervals
    minute_bucket = _parse_minute_interval(interval)
    if minute_bucket is not None:
        return _compute_minute_series(
            history, date_from, date_to, last_n_games,
            minute_bucket, cumulative)

    # Filter history by date
    filtered = []
    for rec in history:
        gd = (rec.get("date") or rec.get("kickoff_utc") or rec.get("ts") or "")[:10]
        if not gd:
            continue
        if date_from and gd < date_from:
            continue
        if date_to and gd > date_to:
            continue
        if not rec.get("predicted_winner_calls"):
            continue
        filtered.append(rec)

    if not filtered:
        return []

    # Sort by game date
    filtered.sort(key=lambda r: (r.get("date") or r.get("kickoff_utc") or "")[:10])

    # If last_n_games, take only last N
    if last_n_games and last_n_games > 0:
        filtered = filtered[-last_n_games:]

    # Group by date
    by_date = {}
    for rec in filtered:
        gd = (rec.get("date") or rec.get("kickoff_utc") or rec.get("ts") or "")[:10]
        by_date.setdefault(gd, []).append(rec)

    sorted_dates = sorted(by_date.keys())

    # Determine bucket boundaries
    ival = (interval or "daily").lower()
    if ival in ("weekly", "monthly"):
        buckets = {}  # bucket_key -> list of dates
        for gd in sorted_dates:
            bk = _bucket_key(gd, ival)
            buckets.setdefault(bk, []).append(gd)
        bucket_keys = sorted(buckets.keys())
    else:
        buckets = {gd: [gd] for gd in sorted_dates}
        bucket_keys = sorted_dates

    series = []
    cumulative_history = []

    for bk in bucket_keys:
        bucket_dates = buckets[bk]
        bucket_records = []
        for gd in bucket_dates:
            bucket_records.extend(by_date[gd])
            cumulative_history.extend(by_date[gd])

        label = bk if ival in ("weekly", "monthly") else bucket_dates[-1]
        snap_date = bucket_dates[-1]

        if cumulative:
            snap = compute_pw_snapshot(cumulative_history, snapshot_date=snap_date)
        else:
            snap = compute_pw_snapshot(bucket_records, snapshot_date=snap_date)

        snap["label"] = label
        series.append(snap)

    return series


def append_snapshot(history=None, snapshot_date=None):
    """Compute and append a snapshot from current (or provided) game_history.

    Deduplicates by date -- if a snapshot for the same date already exists,
    it is replaced with the new one.
    """
    if history is None:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            history = json.load(f)

    snapshot = compute_pw_snapshot(history, snapshot_date=snapshot_date)
    data = load_trend_data()

    # Deduplicate: replace same-date snapshot
    data["snapshots"] = [s for s in data["snapshots"] if s.get("date") != snapshot["date"]]
    data["snapshots"].append(snapshot)
    data["snapshots"].sort(key=lambda s: s.get("date", ""))

    save_trend_data(data)
    return snapshot


def bootstrap_from_backups(backup_root=None):
    """Scan runtime_backups/ and compute a snapshot per backup date.

    Only processes backups that contain game_history.json.
    """
    backup_root = backup_root or os.path.join(SCRIPT_DIR, "runtime_backups")
    if not os.path.isdir(backup_root):
        print("No backup directory found at {}".format(backup_root))
        return

    backup_dirs = sorted(d for d in os.listdir(backup_root)
                         if os.path.isdir(os.path.join(backup_root, d))
                         and not d.startswith("failed-run-"))

    data = load_trend_data()
    existing_dates = {s["date"] for s in data["snapshots"]}
    added = 0

    for dirname in backup_dirs:
        history_path = os.path.join(backup_root, dirname, "game_history.json")
        if not os.path.exists(history_path):
            continue

        # Extract date from dirname (format: YYYYMMDDTHHMMSSZ-label)
        try:
            ts_str = dirname[:16]  # e.g. "20260512T090016Z"
            dt = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ")
            snapshot_date = dt.strftime("%Y-%m-%d")
        except (ValueError, IndexError):
            continue

        # Skip if we already have a snapshot for this date
        if snapshot_date in existing_dates:
            continue

        try:
            with open(history_path, encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        snapshot = compute_pw_snapshot(history, snapshot_date=snapshot_date)
        if snapshot["pw_calls"] > 0:
            data["snapshots"].append(snapshot)
            existing_dates.add(snapshot_date)
            added += 1
            print("  {} -- {} calls, {:.1f}% accuracy, {:.1f}% flat ROI".format(
                snapshot_date, snapshot["pw_calls"], snapshot["accuracy"], snapshot["roi_flat"]))

    data["snapshots"].sort(key=lambda s: s.get("date", ""))
    save_trend_data(data)
    print("Bootstrap complete: {} snapshots added ({} total)".format(added, len(data["snapshots"])))


def show_summary():
    """Print a summary of the current trend data."""
    data = load_trend_data()
    print("PW Trend Data -- NRL ({})".format(os.path.basename(TREND_FILE)))
    print("Changelog entries: {}".format(len(data["changelog"])))
    print("Snapshots: {}".format(len(data["snapshots"])))
    if data["snapshots"]:
        first = data["snapshots"][0]
        last = data["snapshots"][-1]
        print("Date range: {} to {}".format(first["date"], last["date"]))
        print("\nLatest snapshot ({})".format(last["date"]))
        print("  Games in sample: {}".format(last["games_in_sample"]))
        print("  PW calls: {} ({} correct)".format(last["pw_calls"], last["correct"]))
        print("  Accuracy: {:.1f}%".format(last["accuracy"]))
        print("  Flat ROI: {:.1f}%".format(last["roi_flat"]))
        print("  ML ROI: {:.1f}%".format(last["roi_ml"]))
        if last.get("avg_edge") is not None:
            print("  Avg edge: {:.2f}".format(last["avg_edge"]))
        w30 = last.get("window_30")
        if w30:
            print("  Last 30 games: {:.1f}% accuracy, {:.1f}% flat ROI ({} calls)".format(
                w30["accuracy"], w30["roi_flat"], w30["calls"]))

    if data["changelog"]:
        print("\nChangelog:")
        for entry in data["changelog"]:
            issues = ", ".join(entry.get("issues", []))
            print("  {} [{}] {} ({})".format(
                entry["date"], entry["tag"], entry["description"], issues))


# ── CLI ─────────────────────────────────────────────────────────────────

def _build_parser():
    parser = argparse.ArgumentParser(description="PW call performance tracking")
    sub = parser.add_subparsers(dest="command")

    bp = sub.add_parser("bootstrap", help="Bootstrap snapshots from existing backups")
    bp.add_argument("--backup-root", default=None)

    sub.add_parser("snapshot", help="Append a snapshot from current game_history")
    sub.add_parser("show", help="Show trend data summary")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "bootstrap":
        bootstrap_from_backups(backup_root=args.backup_root)
        return 0

    if args.command == "snapshot":
        snap = append_snapshot()
        print("Snapshot appended: {} -- {} calls, {:.1f}% accuracy".format(
            snap["date"], snap["pw_calls"], snap["accuracy"]))
        return 0

    if args.command == "show":
        show_summary()
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
