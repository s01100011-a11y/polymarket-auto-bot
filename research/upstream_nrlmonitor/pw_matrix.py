#!/usr/bin/env python3
"""
pw_matrix.py — PW ROI Matrix cross-dimensional pivot computation (#29).

Computes cross-tab of PW call accuracy and ROI metrics across
6 dimensions: Confidence Band, Consensus Type, Half, Quarter, Spread Role, Edge Range.

Usage:
    import pw_matrix
    data = pw_matrix.compute_pw_matrix(history, pins={})
"""

import json
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
STAKE = 100.0
FLAT_WIN_PROFIT = 90.91  # -110 juice: $90.91 profit on $100 wager (#157)

# ── Dimensions ──────────────────────────────────────────────────────────

_MATRIX_DIMS = ("threshold", "consensus", "half", "quarter", "spread_role", "edge_range", "margin")
_MATRIX_PAIRS = [
    ("threshold", "consensus"),
    ("threshold", "half"),
    ("threshold", "quarter"),
    ("threshold", "spread_role"),
    ("threshold", "edge_range"),
    ("consensus", "half"),
    ("consensus", "quarter"),
    ("consensus", "spread_role"),
    ("consensus", "edge_range"),
    ("half", "spread_role"),
    ("half", "edge_range"),
    ("quarter", "spread_role"),
    ("quarter", "edge_range"),
    ("spread_role", "edge_range"),
    ("spread_role", "margin"),
    ("edge_range", "margin"),
    ("threshold", "margin"),
    ("consensus", "margin"),
    ("half", "margin"),
    ("quarter", "margin"),
]


def _edge_range_key(avg_edge):
    """Classify avg_edge into a range bucket."""
    if avg_edge is None:
        return ""
    try:
        e = float(avg_edge)
    except (TypeError, ValueError):
        return ""
    if e >= 10:
        return "10+"
    if e >= 5:
        return "5-10"
    if e >= 0:
        return "0-5"
    return "<0"


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
    """Profit on a $100 winning bet at decimal odds."""
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


# ── Spread cover helpers ────────────────────────────────────────────────

def _final_team_margin(rec, pred_team):
    """Return final margin from predicted team's perspective, or None."""
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


def _did_cover_spread(rec, pred_team, spread_val):
    """Return True/False/None for pregame spread cover."""
    if spread_val is None:
        return None
    team_margin = _final_team_margin(rec, pred_team)
    if team_margin is None:
        return None
    try:
        return (team_margin + float(spread_val)) > 0
    except (ValueError, TypeError):
        return None


def _did_cover_live_line(rec, pw):
    """Return True/False/None — synthetic live line."""
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
    """Return True/False/None — bookmaker live spread."""
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


# ── Bucket helpers ──────────────────────────────────────────────────────

def _new_bucket():
    return {
        "calls": 0, "correct": 0,
        "flat_pnl": 0.0, "ml_pnl": 0.0, "ml_coverage": 0,
        "live_line_pnl": 0.0, "live_line_count": 0,
        "bk_line_pnl": 0.0, "bk_line_count": 0,
        "_game_ids": set(), "_game_ids_correct": set(),
        "_spread_cover_yes": set(), "_spread_cover_no": set(),
        "_covered_not_won": set(), "_won_not_covered": set(),
        "bk_odds_pnl": 0.0, "bk_odds_count": 0,
        # Tag tracking (#140)
        "_tag_counts": {},  # {tag: set(game_ids)}
    }


def _finalize_bucket(d):
    """Convert internal sets to counts and compute derived metrics."""
    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    def _roi_pct(pnl, n):
        return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None

    d["pct"] = _pct(d["correct"], d["calls"])
    gc = len(d.pop("_game_ids_correct"))
    d["games"] = len(d.pop("_game_ids"))
    d["games_correct"] = gc
    d["games_won_pct"] = _pct(gc, d["games"])
    d["flat_pnl"] = round(d["flat_pnl"], 2)
    d["flat_roi_pct"] = _roi_pct(d["flat_pnl"], d["calls"])
    d["ml_pnl"] = round(d["ml_pnl"], 2)
    d["ml_roi_pct"] = _roi_pct(d["ml_pnl"], d["ml_coverage"]) if d["ml_coverage"] > 0 else None
    d["live_line_pnl"] = round(d["live_line_pnl"], 2)
    d["live_line_roi_pct"] = _roi_pct(d["live_line_pnl"], d["live_line_count"]) if d["live_line_count"] > 0 else None
    d["bk_line_pnl"] = round(d["bk_line_pnl"], 2)
    d["bk_line_roi_pct"] = _roi_pct(d["bk_line_pnl"], d["bk_line_count"]) if d["bk_line_count"] > 0 else None
    d["spread_cover_yes"] = len(d.pop("_spread_cover_yes"))
    d["spread_cover_no"] = len(d.pop("_spread_cover_no"))
    sc_total = d["spread_cover_yes"] + d["spread_cover_no"]
    d["spread_covered_pct"] = round(d["spread_cover_yes"] / sc_total * 100, 1) if sc_total > 0 else None
    # Spread ROI: -110 juice per game (#157)
    d["spread_roi_pct"] = _roi_pct(
        d["spread_cover_yes"] * FLAT_WIN_PROFIT - d["spread_cover_no"] * STAKE,
        sc_total) if sc_total > 0 else None
    # Covered/won cross metrics (#124)
    cnw = len(d.pop("_covered_not_won"))
    wnc = len(d.pop("_won_not_covered"))
    d["covered_not_won"] = cnw
    d["won_not_covered"] = wnc
    d["covered_not_won_pct"] = round(cnw / sc_total * 100, 1) if sc_total > 0 else None
    d["won_not_covered_pct"] = round(wnc / d["games"] * 100, 1) if d.get("games", 0) > 0 else None
    # BK odds ROI
    d["bk_odds_pnl"] = round(d["bk_odds_pnl"], 2)
    d["bk_odds_roi_pct"] = _roi_pct(d["bk_odds_pnl"], d["bk_odds_count"]) if d["bk_odds_count"] > 0 else None
    # Tag counts (#140)
    tag_sets = d.pop("_tag_counts", {})
    d["tag_counts"] = {tag: len(gids) for tag, gids in tag_sets.items()}


# ── Main computation ────────────────────────────────────────────────────

def compute_pw_matrix(history, pins=None, custom_pivot=None):
    """Compute PW ROI matrix from game_history records.

    Args:
        history: list of game history dicts
        pins: dict of pinned dimension values, e.g. {"spread_role": "underdog"}
        custom_pivot: optional dict {"row_dims": ["threshold", "spread_role"],
                      "col_dims": ["edge_range"]} for compound row/col axes.
                      When set, an extra ``by_custom`` map is added to the result
                      with cell keys ``row1+row2|col1+col2``.

    Returns dict with by_matrix (pair maps), by_threshold, by_consensus,
    by_half, by_spread_role, by_edge_range, and top-level accuracy stats.
    """
    pins = pins or {}
    _custom = custom_pivot or {}

    by_threshold = {str(b): _new_bucket() for b in (50, 60, 70, 80, 90)}
    by_consensus = {}
    by_half = {}
    by_quarter = {}
    by_spread_role = {}
    by_edge_range = {}
    by_margin = {}
    by_matrix = {"{}_{}_{}".format(a, "x", b): {} for a, b in _MATRIX_PAIRS}
    by_custom = {}  # compound row/col pivot (#187)
    _cust_row_dims = _custom.get("row_dims") or []
    _cust_col_dims = _custom.get("col_dims") or []
    _has_custom = bool(_cust_row_dims and _cust_col_dims)

    total_calls = 0
    correct = 0

    for rec in history:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list) or not calls:
            continue

        game_id = str(rec.get("game_id") or rec.get("match_id") or "")
        # Extract game tags for bucket tracking (#140)
        _game_tags = []
        for _tg in (rec.get("postgame_tags", []) or rec.get("postgame_tags_matched", []) or []):
            if isinstance(_tg, str) and _tg:
                _game_tags.append(_tg.upper())

        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("_suppressed"):
                continue
            c = call.get("correct")
            if c is None:
                continue

            pct = _safe_float(call.get("pct") or call.get("winner_score_pct")) or 0.0
            consensus = str(call.get("consensus") or "").strip().lower()
            half = str(call.get("half") or "").strip().upper()
            quarter = call.get("quarter") or _quarter_from_minute(call.get("game_minute")) or ""
            spread_role = str(call.get("spread_role") or "").strip().lower()
            # Derive spread_role from game record when missing (#40)
            if not spread_role:
                pred_team = (call.get("predicted_team") or "").strip()
                _home = rec.get("home_team", "")
                _hs = rec.get("home_spread")
                if _hs is not None and pred_team:
                    try:
                        _pih = pred_team == _home
                        spread_role = "favorite" if (_pih and float(_hs) < 0) or (not _pih and float(_hs) > 0) else "underdog"
                    except (TypeError, ValueError):
                        pass
            pred_team = (call.get("predicted_team") or "").strip()

            is_correct = bool(c)
            total_calls += 1
            if is_correct:
                correct += 1

            # Flat P&L: +1/-1 units
            flat_delta = STAKE if is_correct else -STAKE

            # ML P&L: pregame decimal odds (derive from game record if missing #42)
            ml_odds = _safe_float(call.get("moneyline"))
            if ml_odds is None and pred_team:
                _home = rec.get("home_team", "")
                ml_odds = _safe_float(rec.get("home_ml") if pred_team == _home else rec.get("away_ml"))
            has_ml = ml_odds is not None and ml_odds > 1.0
            ml_delta = (_decimal_win_profit(ml_odds) if is_correct else -STAKE) if has_ml else 0.0

            # Derive spread from game record if missing (#42)
            _call_spread = _safe_float(call.get("spread"))
            if _call_spread is None and pred_team:
                _home = rec.get("home_team", "")
                _call_spread = _safe_float(rec.get("home_spread") if pred_team == _home else rec.get("away_spread"))

            # Derive BK/spread fields from game record if missing (#42)
            _enriched_call = call
            if (not call.get("bk_spread") or not call.get("spread")) and pred_team:
                _enriched_call = dict(call)
                _home = rec.get("home_team", "")
                _bk_sp = rec.get("home_spread") if pred_team == _home else rec.get("away_spread")
                _bk_ml = rec.get("home_ml") if pred_team == _home else rec.get("away_ml")
                if _bk_sp is not None:
                    _enriched_call["bk_spread"] = str(_bk_sp)
                    if not _enriched_call.get("spread"):
                        _enriched_call["spread"] = str(_bk_sp)
                if _bk_ml is not None:
                    _enriched_call["bk_moneyline"] = str(_bk_ml)

            # Live line cover
            ll_cover = _did_cover_live_line(rec, _enriched_call)
            # BK line cover
            bk_cover = _did_cover_bk_line(rec, _enriched_call)
            _bk_spr_price = _safe_float(_enriched_call.get("bk_spread_price"))
            # Pregame spread cover (use derived spread #42)
            spread_cover = _did_cover_spread(rec, pred_team, _call_spread) if pred_team else None

            def _accum(bucket):
                bucket["calls"] += 1
                if is_correct:
                    bucket["correct"] += 1
                bucket["flat_pnl"] += flat_delta
                if has_ml:
                    bucket["ml_pnl"] += ml_delta
                    bucket["ml_coverage"] += 1
                if ll_cover is not None:
                    bucket["live_line_count"] += 1
                    bucket["live_line_pnl"] += FLAT_WIN_PROFIT if ll_cover else -STAKE  # -110 juice (#157)
                if bk_cover is not None:
                    bucket["bk_line_count"] += 1
                    _bk_spr_win = _decimal_win_profit(_bk_spr_price) if _bk_spr_price is not None and _bk_spr_price > 1.0 else FLAT_WIN_PROFIT
                    bucket["bk_line_pnl"] += _bk_spr_win if bk_cover else -STAKE  # actual spread price, -110 fallback (#193)
                if game_id:
                    bucket["_game_ids"].add(game_id)
                    if is_correct:
                        bucket["_game_ids_correct"].add(game_id)
                    if spread_cover is True:
                        bucket["_spread_cover_yes"].add(game_id)
                        if not is_correct:
                            bucket["_covered_not_won"].add(game_id)
                    elif spread_cover is False:
                        bucket["_spread_cover_no"].add(game_id)
                        if is_correct:
                            bucket["_won_not_covered"].add(game_id)
                # BK odds ROI (#124)
                bk_ml = _safe_float(call.get("bk_moneyline"))
                if bk_ml is not None and bk_ml > 1.0:
                    bucket["bk_odds_count"] += 1
                    bucket["bk_odds_pnl"] += _decimal_win_profit(bk_ml) if is_correct else -STAKE
                # Tag tracking (#140)
                if game_id:
                    for _tag in _game_tags:
                        bucket["_tag_counts"].setdefault(_tag, set()).add(game_id)

            # Confidence band
            thresh_val = ""
            for band in (90, 80, 70, 60, 50):
                if pct >= band:
                    thresh_val = str(band)
                    _accum(by_threshold[thresh_val])
                    break

            # Consensus
            ckey = consensus or ""
            if ckey not in by_consensus:
                by_consensus[ckey] = _new_bucket()
            _accum(by_consensus[ckey])

            # Half
            hkey = half or "?"
            if hkey not in by_half:
                by_half[hkey] = _new_bucket()
            _accum(by_half[hkey])

            # Quarter (#31)
            qkey = quarter or "?"
            if qkey not in by_quarter:
                by_quarter[qkey] = _new_bucket()
            _accum(by_quarter[qkey])

            # Spread role
            if spread_role:
                if spread_role not in by_spread_role:
                    by_spread_role[spread_role] = _new_bucket()
                _accum(by_spread_role[spread_role])

            # Edge range (#187)
            edge_range = _edge_range_key(call.get("avg_edge"))
            if edge_range:
                if edge_range not in by_edge_range:
                    by_edge_range[edge_range] = _new_bucket()
                _accum(by_edge_range[edge_range])

            # Margin (leading/trailing) (#187)
            _margin_raw = call.get("score_margin_at_fire")
            margin_key = ""
            if _margin_raw is not None:
                try:
                    margin_key = "leading" if float(_margin_raw) >= 0 else "trailing"
                except (TypeError, ValueError):
                    pass
            if margin_key:
                if margin_key not in by_margin:
                    by_margin[margin_key] = _new_bucket()
                _accum(by_margin[margin_key])

            # Matrix cross-dimensional pivots
            dim_vals = {
                "threshold": thresh_val,
                "consensus": consensus or "",
                "half": half or "?",
                "quarter": quarter or "?",
                "spread_role": spread_role,
                "edge_range": edge_range,
                "margin": margin_key,
            }
            # Check pin filters
            pin_ok = True
            for pdim, pval in pins.items():
                if pdim in dim_vals and pval and dim_vals.get(pdim) != pval:
                    pin_ok = False
                    break
            if pin_ok:
                for dim_a, dim_b in _MATRIX_PAIRS:
                    va, vb = dim_vals[dim_a], dim_vals[dim_b]
                    if not va or not vb:
                        continue  # skip when either dim value is empty
                    pair_key = "{}_x_{}".format(dim_a, dim_b)
                    cell_key = "{}|{}".format(va, vb)
                    bucket_map = by_matrix[pair_key]
                    if cell_key not in bucket_map:
                        bucket_map[cell_key] = _new_bucket()
                    _accum(bucket_map[cell_key])

                # Custom compound pivot (#187)
                if _has_custom:
                    _rv = [dim_vals.get(d, "") for d in _cust_row_dims]
                    _cv = [dim_vals.get(d, "") for d in _cust_col_dims]
                    if all(_rv) and all(_cv):
                        _ckey = "{}|{}".format("~".join(_rv), "~".join(_cv))
                        if _ckey not in by_custom:
                            by_custom[_ckey] = _new_bucket()
                        _accum(by_custom[_ckey])

    # Finalize all buckets
    for b in by_threshold.values():
        _finalize_bucket(b)
    for b in by_consensus.values():
        _finalize_bucket(b)
    for b in by_half.values():
        _finalize_bucket(b)
    for b in by_quarter.values():
        _finalize_bucket(b)
    for b in by_spread_role.values():
        _finalize_bucket(b)
    for b in by_edge_range.values():
        _finalize_bucket(b)
    for b in by_margin.values():
        _finalize_bucket(b)
    for pair_map in by_matrix.values():
        for b in pair_map.values():
            _finalize_bucket(b)
    for b in by_custom.values():
        _finalize_bucket(b)

    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None

    return {
        "total_calls": total_calls,
        "correct": correct,
        "accuracy_pct": _pct(correct, total_calls),
        "by_threshold": by_threshold,
        "by_consensus": by_consensus,
        "by_half": by_half,
        "by_quarter": by_quarter,
        "by_spread_role": by_spread_role,
        "by_edge_range": by_edge_range,
        "by_margin": by_margin,
        "by_matrix": by_matrix,
        "by_custom": by_custom if _has_custom else None,
    }
