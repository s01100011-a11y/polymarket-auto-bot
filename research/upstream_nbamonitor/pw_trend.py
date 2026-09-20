#!/usr/bin/env python3
"""
pw_trend.py — PW call performance tracking over time.

Computes and stores snapshots of PW call quality metrics (accuracy, ROI,
win/loss, avg edge, etc.) linked to a changelog of PW logic improvements.

Data file: pw_trend_{league}.json
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
from datetime import datetime, timezone

import league_config

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TREND_FILE = league_config.state_path("pw_trend.json")
STAKE = 100.0
FLAT_JUICE_ML = -110
FLAT_WIN_PROFIT = STAKE * (100.0 / abs(FLAT_JUICE_ML))


def _ml_win_profit(ml_int):
    """Profit on a $100 winning bet at the given moneyline."""
    if ml_int is None or ml_int == 0:
        return FLAT_WIN_PROFIT
    if ml_int > 0:
        return STAKE * (ml_int / 100.0)
    return STAKE * (100.0 / abs(ml_int))


def _safe_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _final_team_margin(rec, pred_id):
    """Return final team margin or None."""
    home_id = str(rec.get("home_id") or "").strip()
    away_id = str(rec.get("away_id") or "").strip()
    home_score = rec.get("home_score")
    away_score = rec.get("away_score")
    if home_score is None or away_score is None:
        return None
    try:
        home_score, away_score = int(home_score), int(away_score)
    except (ValueError, TypeError):
        return None
    if pred_id == home_id:
        return home_score - away_score
    elif pred_id == away_id:
        return away_score - home_score
    return None


def _did_cover_live_line(rec, pw):
    """Return True/False/None — synthetic live line (pregame_spread - margin)."""
    pred_id = str(pw.get("predicted_team_id") or "").strip()
    spread = pw.get("live_spread")
    margin = pw.get("score_margin_at_fire")
    if not pred_id or spread is None or margin is None:
        return None
    team_margin = _final_team_margin(rec, pred_id)
    if team_margin is None:
        return None
    try:
        live_line = float(spread) - float(margin)
    except (ValueError, TypeError):
        return None
    return (team_margin + live_line) > 0


def _did_cover_bk_line(rec, pw):
    """Return True/False/None — bookmaker live spread at fire time (#213)."""
    pred_id = str(pw.get("predicted_team_id") or "").strip()
    bk_spread = pw.get("bk_spread")
    if not pred_id or bk_spread is None:
        return None
    team_margin = _final_team_margin(rec, pred_id)
    if team_margin is None:
        return None
    try:
        return (team_margin + float(bk_spread)) > 0
    except (ValueError, TypeError):
        return None


def _apply_call_selection(call_list, sel):
    """Apply call_selection filter per game. Matches outcomes._filter_calls_by_selection."""
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
        elif sel == "first_per_quarter":
            by_q = {}
            for c in game_calls:
                q = c.get("quarter", "")
                if q not in by_q or (c.get("ts") or "") < (by_q[q].get("ts") or ""):
                    by_q[q] = c
            result.extend(by_q.values())
    return result


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
        "roi_flat": 0.0, "roi_ml": 0.0, "roi_live_line": 0.0, "roi_bk_line": 0.0, "roi_bk_odds": None,
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
                ml_pnl += _ml_win_profit(ml) if c["correct"] else -STAKE
            lc = c.get("live_cover")
            if lc is not None:
                ll_bets += 1
                ll_pnl += FLAT_WIN_PROFIT if lc else -STAKE
            bkc = c.get("bk_cover")
            if bkc is not None:
                bk_ll_bets += 1
                _bk_spr_win = FLAT_WIN_PROFIT
                _bk_sp = c.get("bk_spr_price")
                if _bk_sp is not None and _bk_sp != 0:
                    _bk_spr_win = _ml_win_profit(_bk_sp)
                bk_ll_pnl += _bk_spr_win if bkc else -STAKE
            bk_ml = c.get("bk_ml")
            if bk_ml is not None and bk_ml != 0:
                bk_odds_bets += 1
                bk_odds_pnl += _ml_win_profit(bk_ml) if c["correct"] else -STAKE
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

    # Rolling windows: last N games (by game date, most recent first)
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
            # Add per-role rolling windows
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

    # Per-edge-range breakdown (#298)
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

    # Cross-dimensional filter breakdowns (#232)
    _ROLES = ("all", "favorite", "underdog")
    _QUARTERS = ("all", "Q1", "Q2", "Q3", "Q4", "OT")
    _MARGINS = ("all", "trailing", "leading")
    _CALL_SELS = ("all", "first", "last", "first_per_quarter")

    filters = {}
    for f_role in _ROLES:
        for f_quarter in _QUARTERS:
            for f_margin in _MARGINS:
                for f_csel in _CALL_SELS:
                    if f_role == "all" and f_quarter == "all" and f_margin == "all" and f_csel == "all":
                        continue
                    subset = _filter_calls(calls, f_role, f_quarter, f_margin, f_csel)
                    if not subset:
                        continue
                    fm = _compute_metrics(subset)
                    if fm is None:
                        continue
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
                    filters["{}|{}|{}|{}".format(f_role, f_quarter, f_csel, f_margin)] = fm

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
        "roi_bk_odds": cumulative.get("roi_bk_odds"),
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
        "filters": filters,
        "edges": edges,
    }


def _collect_calls(history):
    """Extract resolved PW calls from game_history with metadata fields."""
    calls = []
    for rec in history:
        game_date = (rec.get("date") or "")[:10]
        game_id = str(rec.get("game_id") or id(rec))
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
            calls.append({
                "correct": bool(correct),
                "ml": _safe_int(pw.get("live_moneyline")),
                "bk_ml": _safe_float(pw.get("bk_moneyline")),
                "avg_edge": pw.get("avg_edge"),
                "pct": pw.get("pct"),
                "date": game_date,
                "pw_version": pw.get("pw_version", ""),
                "spread_role": (pw.get("spread_role") or "").strip().lower(),
                "live_cover": live_cover,
                "bk_cover": bk_cover,
                "bk_spr_price": _safe_int(bk_spr_price) if bk_spr_price is not None else None,
                "quarter": str(pw.get("quarter") or "").strip().upper(),
                "margin_sign": margin_sign,
                "ts": pw.get("ts") or "",
                "game_id": game_id,
            })
    return calls


def _filter_calls(calls, role="all", quarter="all", margin="all", call_sel="all"):
    """Apply role/quarter/margin filters then call_selection."""
    subset = calls
    if role != "all":
        subset = [c for c in subset if c["spread_role"] == role]
    if quarter != "all":
        if quarter == "OT":
            subset = [c for c in subset if c["quarter"].startswith("OT")]
        else:
            subset = [c for c in subset if c["quarter"] == quarter]
    if margin != "all":
        subset = [c for c in subset if c.get("margin_sign") == margin]
    return _apply_call_selection(subset, call_sel)


def load_trend_data():
    """Load pw_trend_{league}.json, returning default structure if missing."""
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
    """Known PW logic changes for bootstrapping."""
    return [
        {"date": "2026-04-09", "tag": "v1-pw-launch", "description": "Predicted Winner feature launched", "issues": ["#72"]},
        {"date": "2026-04-13", "tag": "v2-bayes-blend", "description": "Bayesian blending + ML consensus", "issues": ["#105"]},
        {"date": "2026-05-04", "tag": "v3-polarity", "description": "Polarity gate added", "issues": ["#197"]},
        {"date": "2026-05-09", "tag": "v4-multi-league", "description": "Multi-league support (NBA + WNBA)", "issues": ["#151"]},
        {"date": "2026-05-13", "tag": "v5-edge-gate", "description": "Condition edge gate enabled", "issues": ["#217", "#219", "#221"]},
        {"date": "2026-05-17", "tag": "v6-edge-cleanup", "description": "Edge store filtered: end-of-game backfill artifacts removed", "issues": ["#258"]},
    ]


def resolve_pw_version(date_str=None):
    """Return the pw_version tag for a given date (YYYY-MM-DD).

    Walks the changelog in reverse and returns the tag of the most recent
    entry on or before the given date.  Falls back to 'v0-unknown'.
    """
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
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


def _parse_minute_interval(ival):
    """Parse minute interval like '5min' -> 5, or None if not minute-based."""
    if not ival:
        return None
    import re
    m = re.match(r'^(\d+)min$', ival.lower())
    return int(m.group(1)) if m else None


def _derive_game_minute(pw):
    """Derive game_minute from PW call fields.

    NBA: quarter (Q1-Q4/OT) + game_clock (countdown "9:44").
    NRL: game_minute field directly.
    """
    gm = pw.get("game_minute")
    if gm is not None:
        return gm
    # NBA: derive from quarter + game_clock
    q = pw.get("quarter", "")
    clock = pw.get("game_clock", "")
    if not q or not clock:
        return None
    quarter_offsets = {"Q1": 0, "Q2": 12, "Q3": 24, "Q4": 36}
    if q.startswith("OT"):
        base = 48
    elif q in quarter_offsets:
        base = quarter_offsets[q]
    else:
        return None
    try:
        parts = clock.split(":")
        mins_remaining = int(parts[0])
        elapsed_in_quarter = 12 - mins_remaining
        return base + max(0, elapsed_in_quarter)
    except (ValueError, IndexError):
        return None


def _compute_minute_series(history, date_from, date_to, last_n_games,
                           minute_bucket, cumulative):
    """Compute trend series grouped by game_minute buckets."""
    filtered = []
    for rec in history:
        gd = (rec.get("date") or rec.get("game_date") or "")[:10]
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

    filtered.sort(key=lambda r: (r.get("date") or r.get("game_date") or "")[:10])
    if last_n_games and last_n_games > 0:
        filtered = filtered[-last_n_games:]

    # Collect calls with game_minute
    calls_with_minute = []
    for rec in filtered:
        for pw in (rec.get("predicted_winner_calls") or []):
            if not isinstance(pw, dict):
                continue
            if pw.get("correct") is None or pw.get("_suppressed"):
                continue
            gm = _derive_game_minute(pw)
            if gm is None:
                continue
            bk_start = (gm // minute_bucket) * minute_bucket
            calls_with_minute.append((bk_start, pw, rec))

    if not calls_with_minute:
        return []

    by_bucket = {}
    for bk_start, pw, rec in calls_with_minute:
        by_bucket.setdefault(bk_start, []).append((pw, rec))

    sorted_buckets = sorted(by_bucket.keys())
    series = []
    cumulative_calls = []

    for bk_start in sorted_buckets:
        bucket_pairs = by_bucket[bk_start]

        if cumulative:
            cumulative_calls.extend(bucket_pairs)
            total = len(cumulative_calls)
            wins = sum(1 for pw, rec in cumulative_calls if pw.get("correct"))
            flat_pnl = sum(1 if pw.get("correct") else -1 for pw, rec in cumulative_calls)
        else:
            total = len(bucket_pairs)
            wins = sum(1 for pw, rec in bucket_pairs if pw.get("correct"))
            flat_pnl = sum(1 if pw.get("correct") else -1 for pw, rec in bucket_pairs)

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


def _bucket_key(date_str, interval):
    """Map a YYYY-MM-DD date to a bucket key based on interval."""
    if interval == "weekly":
        d = datetime.strptime(date_str, "%Y-%m-%d")
        iso = d.isocalendar()
        return "{}-W{:02d}".format(iso[0], iso[1])
    elif interval == "monthly":
        return date_str[:7]
    else:
        return date_str


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
        gd = (rec.get("date") or rec.get("game_date") or "")[:10]
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
    filtered.sort(key=lambda r: (r.get("date") or r.get("game_date") or "")[:10])

    # If last_n_games, take only last N
    if last_n_games and last_n_games > 0:
        filtered = filtered[-last_n_games:]

    # Group by date
    by_date = {}
    for rec in filtered:
        gd = (rec.get("date") or rec.get("game_date") or "")[:10]
        by_date.setdefault(gd, []).append(rec)

    sorted_dates = sorted(by_date.keys())

    # Determine bucket boundaries
    ival = (interval or "daily").lower()
    if ival in ("weekly", "monthly"):
        buckets = {}
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

    Deduplicates by date — if a snapshot for the same date already exists,
    it is replaced with the new one.
    """
    if history is None:
        history_path = league_config.state_path("game_history.json")
        with open(history_path, encoding="utf-8") as f:
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

    Only processes backups that contain game_history_{league}.json.
    """
    backup_root = backup_root or os.path.join(SCRIPT_DIR, "runtime_backups")
    if not os.path.isdir(backup_root):
        print("No backup directory found at {}".format(backup_root))
        return

    history_filename = os.path.basename(league_config.state_path("game_history.json"))
    backup_dirs = sorted(d for d in os.listdir(backup_root)
                         if os.path.isdir(os.path.join(backup_root, d))
                         and not d.startswith("failed-run-"))

    data = load_trend_data()
    existing_dates = {s["date"] for s in data["snapshots"]}
    added = 0

    for dirname in backup_dirs:
        history_path = os.path.join(backup_root, dirname, history_filename)
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
            print("  {} — {} calls, {:.1f}% accuracy, {:.1f}% flat ROI".format(
                snapshot_date, snapshot["pw_calls"], snapshot["accuracy"], snapshot["roi_flat"]))

    data["snapshots"].sort(key=lambda s: s.get("date", ""))
    save_trend_data(data)
    print("Bootstrap complete: {} snapshots added ({} total)".format(added, len(data["snapshots"])))


def backfill_versions(dry_run=False):
    """Tag existing PW calls in game_history with pw_version based on game date.

    Only modifies calls that don't already have a pw_version field.
    """
    import outcomes as _outcomes
    history = _outcomes.load_history(migrate=False)
    tagged = 0
    skipped = 0
    for rec in history:
        game_date = (rec.get("date") or "")[:10]
        for pw in (rec.get("predicted_winner_calls") or []):
            if not isinstance(pw, dict):
                continue
            if pw.get("pw_version"):
                skipped += 1
                continue
            pw["pw_version"] = resolve_pw_version(game_date or None)
            tagged += 1

    print("Tagged {} PW calls ({} already had version)".format(tagged, skipped))
    if dry_run:
        print("Dry run — no changes written.")
    else:
        if tagged > 0:
            _outcomes.save_history(history)
            print("Saved game_history.")


def show_summary():
    """Print a summary of the current trend data."""
    data = load_trend_data()
    print("PW Trend Data — {} ({})".format(league_config.LEAGUE.upper(),
                                            os.path.basename(TREND_FILE)))
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


def _build_parser():
    parser = argparse.ArgumentParser(description="PW call performance tracking")
    sub = parser.add_subparsers(dest="command")

    bp = sub.add_parser("bootstrap", help="Bootstrap snapshots from existing backups")
    bp.add_argument("--backup-root", default=None)

    sub.add_parser("snapshot", help="Append a snapshot from current game_history")
    sub.add_parser("show", help="Show trend data summary")

    vp = sub.add_parser("backfill-versions", help="Tag existing PW calls with pw_version")
    vp.add_argument("--dry-run", action="store_true")

    return parser


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "backfill-versions":
        backfill_versions(dry_run=args.dry_run)
    elif args.command == "bootstrap":
        bootstrap_from_backups(backup_root=args.backup_root)
    elif args.command == "snapshot":
        snap = append_snapshot()
        print("Snapshot appended: {} — {} calls, {:.1f}% accuracy".format(
            snap["date"], snap["pw_calls"], snap["accuracy"]))
    elif args.command == "show":
        show_summary()
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
