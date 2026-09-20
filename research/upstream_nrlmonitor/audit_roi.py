#!/usr/bin/env python3
"""audit_roi.py — End-to-end audit of BK ML ROI + BK Spread ROI correctness (#205).

Cross-validates scenario stats, ROI computations, and spread cover logic
across all 5 surfaces:
  1. PW tab accuracy block (client-side from /api/pw-calls)
  2. Per-call scenario badges (server-side compute_scenario_stats)
  3. ROI Matrix (/api/pw-matrix)
  4. ROI Combos (/api/pw-roi-combos)
  5. Analysis Outcomes PW Accuracy (/api/analysis)

NRL-specific: uses decimal odds, team names (not IDs), home_team/away_team.

Usage:
  python3 audit_roi.py                    # full audit (requires running dashboard)
  python3 audit_roi.py --offline          # offline mode (no API calls, data-only checks)
  python3 audit_roi.py --verbose          # show per-call trace details
  python3 audit_roi.py --port 8898        # dashboard port override
"""

import json
import os
import sys
import argparse
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    import requests
except ImportError:
    requests = None

# ── Import core modules ──────────────────────────────────────────────────────
import outcomes
import pw_matrix

STAKE = outcomes.STAKE  # 100.0
FLAT_WIN_PROFIT = outcomes.FLAT_WIN_PROFIT  # 90.91


# ── Report data ──────────────────────────────────────────────────────────────

class AuditReport:
    """Collects pass/fail/warn results and prints structured output."""

    def __init__(self):
        self.sections = []
        self.current_section = None
        self.totals = {"pass": 0, "fail": 0, "warn": 0}

    def section(self, name):
        self.current_section = {"name": name, "results": []}
        self.sections.append(self.current_section)

    def ok(self, msg):
        self.current_section["results"].append(("PASS", msg))
        self.totals["pass"] += 1

    def fail(self, msg):
        self.current_section["results"].append(("FAIL", msg))
        self.totals["fail"] += 1

    def warn(self, msg):
        self.current_section["results"].append(("WARN", msg))
        self.totals["warn"] += 1

    def info(self, msg):
        self.current_section["results"].append(("INFO", msg))

    def print_report(self):
        print("\n" + "=" * 72)
        print("ROI AUDIT REPORT (NRL)")
        print("=" * 72)
        for sec in self.sections:
            print(f"\n{'─' * 72}")
            print(f"  {sec['name']}")
            print(f"{'─' * 72}")
            for status, msg in sec["results"]:
                marker = {"PASS": "✓", "FAIL": "✗", "WARN": "⚠", "INFO": "·"}.get(status, " ")
                print(f"  {marker} [{status}] {msg}")
        print(f"\n{'=' * 72}")
        print(f"  SUMMARY: {self.totals['pass']} passed, "
              f"{self.totals['fail']} failed, {self.totals['warn']} warnings")
        print(f"{'=' * 72}\n")
        return self.totals["fail"] == 0


# ── Helpers ──────────────────────────────────────────────────────────────────

def _load_history():
    """Load game history via outcomes module."""
    return outcomes.load_history()


def _api_get(base_url, path, params=None):
    """Fetch JSON from dashboard API."""
    if requests is None:
        return None
    try:
        r = requests.get(f"{base_url}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [API ERROR] {path}: {e}")
        return None


def _close_enough(a, b, tol=0.05):
    """Compare two floats within tolerance."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) <= tol


def _nrl_team_margin(rec, predicted_team):
    """Compute signed margin from predicted team's perspective (NRL uses team names)."""
    hs = rec.get("home_score")
    aws = rec.get("away_score")
    if hs is None or aws is None:
        return None
    try:
        hs = int(hs)
        aws = int(aws)
    except (ValueError, TypeError):
        return None
    pt = (predicted_team or "").strip()
    ht = (rec.get("home_team") or "").strip()
    at = (rec.get("away_team") or "").strip()
    if pt == ht:
        return hs - aws
    elif pt == at:
        return aws - hs
    return None


def _nrl_spread_fav(rec):
    """Derive spread favourite team name from home_spread (NRL convention)."""
    hs = rec.get("home_spread")
    if hs is None:
        return ""
    try:
        return rec.get("home_team", "") if float(hs) < 0 else rec.get("away_team", "")
    except (TypeError, ValueError):
        return ""


def _compare_bucket_fields(local, api, count_fields, pct_fields, prefix, tol=0.15, verbose=False, report=None):
    """Compare bucket count and percentage fields, returning mismatch count."""
    mismatches = 0
    for field in count_fields:
        if local.get(field, 0) != api.get(field, 0):
            mismatches += 1
            if verbose and report is not None:
                report.info(f"  {prefix}.{field}: local={local.get(field)} api={api.get(field)}")
    for field in pct_fields:
        if not _close_enough(local.get(field), api.get(field), tol):
            mismatches += 1
            if verbose and report is not None:
                report.info(f"  {prefix}.{field}: local={local.get(field)} api={api.get(field)}")
    return mismatches


def _roi_pct(pnl, n):
    """Return ROI percentage for a $100 stake."""
    return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None


def _pct(c, t):
    """Return rounded percentage."""
    return round(c / t * 100, 1) if t > 0 else None


def _build_local_pw_accuracy_summary(pw_data):
    """Aggregate top-level PW accuracy metrics from pw_matrix output."""
    thresh = pw_data.get("by_threshold", {})
    flat_pnl = sum(b.get("flat_pnl", 0) for b in thresh.values())
    ml_pnl = sum(b.get("ml_pnl", 0) for b in thresh.values())
    ml_cov = sum(b.get("ml_coverage", 0) for b in thresh.values())
    sc_yes = sum(b.get("spread_cover_yes", 0) for b in thresh.values())
    sc_no = sum(b.get("spread_cover_no", 0) for b in thresh.values())
    ll_pnl = sum(b.get("live_line_pnl", 0) for b in thresh.values())
    ll_count = sum(b.get("live_line_count", 0) for b in thresh.values())
    bk_pnl = sum(b.get("bk_line_pnl", 0) for b in thresh.values())
    bk_count = sum(b.get("bk_line_count", 0) for b in thresh.values())
    games = sum(b.get("games", 0) for b in thresh.values())
    games_correct = sum(b.get("games_correct", 0) for b in thresh.values())
    bk_odds_pnl = sum(b.get("bk_odds_pnl", 0) for b in thresh.values())
    bk_odds_count = sum(b.get("bk_odds_count", 0) for b in thresh.values())
    sc_total = sc_yes + sc_no
    return {
        "total_calls": pw_data.get("total_calls", 0),
        "correct": pw_data.get("correct", 0),
        "accuracy_pct": pw_data.get("accuracy_pct"),
        "total_games": games,
        "games_correct": games_correct,
        "games_won_pct": _pct(games_correct, games),
        "flat_pnl": round(flat_pnl, 2),
        "flat_roi_pct": _roi_pct(flat_pnl, pw_data.get("total_calls", 0)),
        "ml_pnl": round(ml_pnl, 2),
        "ml_roi_pct": _roi_pct(ml_pnl, ml_cov) if ml_cov > 0 else None,
        "ml_coverage": ml_cov,
        "spread_cover_yes": sc_yes,
        "spread_cover_no": sc_no,
        "spread_covered_pct": _pct(sc_yes, sc_total),
        "spread_roi_pct": _roi_pct(sc_yes * FLAT_WIN_PROFIT - sc_no * STAKE, sc_total) if sc_total > 0 else None,
        "live_line_roi_pct": _roi_pct(ll_pnl, ll_count) if ll_count > 0 else None,
        "live_line_count": ll_count,
        "bk_line_roi_pct": _roi_pct(bk_pnl, bk_count) if bk_count > 0 else None,
        "bk_line_count": bk_count,
        "bk_odds_roi_pct": _roi_pct(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        "bk_odds_count": bk_odds_count,
        "by_threshold": pw_data.get("by_threshold", {}),
        "by_consensus": pw_data.get("by_consensus", {}),
        "by_half": pw_data.get("by_half", {}),
        "by_spread_role": pw_data.get("by_spread_role", {}),
    }


def _summarize_pw_calls_games(games):
    """Recompute PW tab accuracy-block metrics from /api/pw-calls raw games payload."""
    all_calls = []
    call_game = {}
    games_total = 0
    games_correct = 0
    for game in games or []:
        pw_correct = game.get("pw_correct")
        if pw_correct is not None:
            games_total += 1
            if pw_correct is True:
                games_correct += 1
        for call in game.get("calls", []) or []:
            all_calls.append(call)
            call_game[id(call)] = game

    non_suppressed = [c for c in all_calls if not c.get("_suppressed")]
    decided = [c for c in non_suppressed if c.get("correct") is not None]
    correct = sum(1 for c in decided if c.get("correct") is True)
    flat_pnl = 0.0
    ml_pnl = 0.0
    ml_count = 0
    spread_cover_yes = 0
    spread_cover_no = 0
    live_line_pnl = 0.0
    live_line_count = 0
    bk_line_pnl = 0.0
    bk_line_count = 0
    bk_odds_pnl = 0.0
    bk_odds_count = 0

    for call in decided:
        is_correct = call.get("correct") is True
        game = call_game.get(id(call))
        flat_pnl += FLAT_WIN_PROFIT if is_correct else -STAKE

        ml_odds = pw_matrix._safe_float(call.get("moneyline") or call.get("live_moneyline"))
        if ml_odds is not None:
            ml_count += 1
            ml_pnl += pw_matrix._decimal_win_profit(ml_odds) if is_correct else -STAKE
        else:
            ml_pnl += FLAT_WIN_PROFIT if is_correct else -STAKE

        spread = pw_matrix._safe_float(call.get("spread"))
        pred_team = str(call.get("predicted_team") or "").strip()
        if spread is not None and game:
            spread_cover = pw_matrix._did_cover_spread(game, pred_team, spread)
            if spread_cover is True:
                spread_cover_yes += 1
            elif spread_cover is False:
                spread_cover_no += 1

        ll_cover = pw_matrix._did_cover_live_line(game, call) if game else None
        if ll_cover is not None:
            live_line_count += 1
            live_line_pnl += FLAT_WIN_PROFIT if ll_cover else -STAKE

        bk_cover = pw_matrix._did_cover_bk_line(game, call) if game else None
        if bk_cover is not None:
            bk_line_count += 1
            bk_spread_price = pw_matrix._safe_float(call.get("bk_spread_price"))
            bk_win_profit = (pw_matrix._decimal_win_profit(bk_spread_price)
                             if bk_spread_price is not None and bk_spread_price > 1.0
                             else FLAT_WIN_PROFIT)
            bk_line_pnl += bk_win_profit if bk_cover else -STAKE

        bk_ml = pw_matrix._safe_float(call.get("bk_moneyline"))
        if bk_ml is not None:
            bk_odds_count += 1
            bk_win_profit = pw_matrix._decimal_win_profit(bk_ml) if bk_ml > 1.0 else FLAT_WIN_PROFIT
            bk_odds_pnl += bk_win_profit if is_correct else -STAKE

    sc_total = spread_cover_yes + spread_cover_no
    return {
        "total_calls": len(non_suppressed),
        "correct": correct,
        "suppressed_calls": len([c for c in all_calls if c.get("_suppressed")]),
        "accuracy_pct": _pct(correct, len(decided)) or 0,
        "total_games": len(games or []),
        "games_correct": games_correct,
        "games_won_pct": _pct(games_correct, games_total) or 0,
        "flat_roi_pct": _roi_pct(flat_pnl, len(decided)),
        "ml_roi_pct": _roi_pct(ml_pnl, len(decided)),
        "ml_coverage": ml_count,
        "spread_cover_yes": spread_cover_yes,
        "spread_cover_no": spread_cover_no,
        "spread_covered_pct": _pct(spread_cover_yes, sc_total),
        "spread_roi_pct": _roi_pct(spread_cover_yes * FLAT_WIN_PROFIT - spread_cover_no * STAKE, sc_total) if sc_total > 0 else None,
        "live_line_roi_pct": _roi_pct(live_line_pnl, live_line_count) if live_line_count > 0 else None,
        "live_line_count": live_line_count,
        "bk_odds_roi_pct": _roi_pct(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        "bk_odds_count": bk_odds_count,
        "bk_line_roi_pct": _roi_pct(bk_line_pnl, bk_line_count) if bk_line_count > 0 else None,
        "bk_line_count": bk_line_count,
    }


def _compute_local_roi_combos(records, limit=100, min_calls=1, strict_bk=False, sort_by="ml_roi_pct"):
    """Recompute PW ROI combo payload locally from history records."""
    roles = ["favorite", "underdog"]
    margins = ["trailing", "leading"]
    selections = ["first", "first_per_half", "first_per_quarter", "last"]
    halves = ["H1", "H2", "ET"]
    quarters = ["Q1", "Q2", "Q3", "Q4", "ET"]

    all_calls = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        pw_calls = rec.get("predicted_winner_calls")
        if not isinstance(pw_calls, list):
            continue
        game_id = str(rec.get("game_id") or rec.get("match_id") or "")
        for call in pw_calls:
            if not isinstance(call, dict) or call.get("_suppressed"):
                continue
            c = call.get("correct")
            if c is None:
                continue
            has_genuine_bk = bool(call.get("bk_moneyline") or call.get("bk_spread"))
            if strict_bk and not has_genuine_bk:
                continue
            pred_team = (call.get("predicted_team") or "").strip()
            is_correct = bool(c)
            role = (call.get("spread_role") or "").strip().lower()
            ml_odds = pw_matrix._safe_float(call.get("moneyline"))
            if not role and pred_team:
                home = rec.get("home_team", "")
                home_spread = rec.get("home_spread")
                if home_spread is not None:
                    try:
                        pred_is_home = pred_team == home
                        role = "favorite" if ((pred_is_home and float(home_spread) < 0)
                                               or (not pred_is_home and float(home_spread) > 0)) else "underdog"
                    except (TypeError, ValueError):
                        pass
                if ml_odds is None:
                    ml_odds = pw_matrix._safe_float(rec.get("home_ml") if pred_team == home else rec.get("away_ml"))
            has_ml = ml_odds is not None and ml_odds > 1.0
            score_margin = call.get("score_margin_at_fire")
            margin_sign = "trailing" if score_margin is not None and score_margin <= 0 else "leading" if score_margin is not None else ""
            spread_val = pw_matrix._safe_float(call.get("spread"))
            if spread_val is None and pred_team:
                home = rec.get("home_team", "")
                spread_val = pw_matrix._safe_float(rec.get("home_spread") if pred_team == home else rec.get("away_spread"))
            spread_cover = pw_matrix._did_cover_spread(rec, pred_team, spread_val) if pred_team else None
            enriched_call = call
            if not call.get("bk_spread") and pred_team:
                enriched_call = dict(call)
                home = rec.get("home_team", "")
                bk_spread = rec.get("home_spread") if pred_team == home else rec.get("away_spread")
                bk_ml = rec.get("home_ml") if pred_team == home else rec.get("away_ml")
                if bk_spread is not None:
                    enriched_call["bk_spread"] = str(bk_spread)
                if bk_ml is not None:
                    enriched_call["bk_moneyline"] = str(bk_ml)
                if not enriched_call.get("spread") and spread_val is not None:
                    enriched_call["spread"] = str(spread_val)
            ll_cover = pw_matrix._did_cover_live_line(rec, enriched_call)
            bk_cover = pw_matrix._did_cover_bk_line(rec, enriched_call)
            all_calls.append({
                "correct": is_correct,
                "role": role,
                "half": str(call.get("half") or "").strip().upper(),
                "quarter": call.get("quarter") or pw_matrix._quarter_from_minute(call.get("game_minute")),
                "margin": margin_sign,
                "ts": call.get("ts") or "",
                "game_id": game_id,
                "ml_odds": ml_odds,
                "has_ml": has_ml,
                "spread_cover": spread_cover,
                "ll_cover": ll_cover,
                "bk_cover": bk_cover,
                "bk_ml": pw_matrix._safe_float(enriched_call.get("bk_moneyline")),
                "_has_genuine_bk": has_genuine_bk,
            })

    def _select_calls(call_list, selection):
        if selection == "first":
            by_game = {}
            for c in call_list:
                gid = c["game_id"]
                if gid not in by_game or c["ts"] < by_game[gid]["ts"]:
                    by_game[gid] = c
            return list(by_game.values())
        if selection == "last":
            by_game = {}
            for c in call_list:
                gid = c["game_id"]
                if gid not in by_game or c["ts"] > by_game[gid]["ts"]:
                    by_game[gid] = c
            return list(by_game.values())
        if selection == "first_per_half":
            by_game_half = {}
            for c in call_list:
                key = (c["game_id"], c["half"])
                if key not in by_game_half or c["ts"] < by_game_half[key]["ts"]:
                    by_game_half[key] = c
            return list(by_game_half.values())
        if selection == "first_per_quarter":
            by_game_quarter = {}
            for c in call_list:
                key = (c["game_id"], c["quarter"])
                if key not in by_game_quarter or c["ts"] < by_game_quarter[key]["ts"]:
                    by_game_quarter[key] = c
            return list(by_game_quarter.values())
        return call_list

    def _build_combo(subset, role, margin, selection, half="", quarter=""):
        total_calls = len(subset)
        if total_calls < min_calls:
            return None
        correct = sum(1 for c in subset if c["correct"])
        games = len(set(c["game_id"] for c in subset if c["game_id"]))
        games_correct = len(set(c["game_id"] for c in subset if c["game_id"] and c["correct"]))
        flat_pnl = sum(STAKE if c["correct"] else -STAKE for c in subset)
        ml_pnl = 0.0
        ml_count = 0
        for c in subset:
            if c["has_ml"]:
                ml_count += 1
                ml_pnl += pw_matrix._decimal_win_profit(c["ml_odds"]) if c["correct"] else -STAKE
        sc_yes = len(set(c["game_id"] for c in subset if c["spread_cover"] is True))
        sc_no = len(set(c["game_id"] for c in subset if c["spread_cover"] is False))
        sc_total = sc_yes + sc_no
        ll_count = sum(1 for c in subset if c["ll_cover"] is not None)
        ll_pnl = sum(FLAT_WIN_PROFIT if c["ll_cover"] else -STAKE for c in subset if c["ll_cover"] is not None)
        bk_count = sum(1 for c in subset if c["bk_cover"] is not None)
        bk_pnl = sum(FLAT_WIN_PROFIT if c["bk_cover"] else -STAKE for c in subset if c["bk_cover"] is not None)
        bk_odds_pnl = 0.0
        bk_odds_count = 0
        for c in subset:
            if c["bk_ml"] is not None and c["bk_ml"] > 1.0:
                bk_odds_count += 1
                bk_odds_pnl += pw_matrix._decimal_win_profit(c["bk_ml"]) if c["correct"] else -STAKE
        return {
            "role": role,
            "margin": margin,
            "call_selection": selection,
            "half": half,
            "quarter": quarter,
            "total_calls": total_calls,
            "correct": correct,
            "accuracy_pct": _pct(correct, total_calls),
            "total_games": games,
            "games_won_pct": _pct(games_correct, games),
            "flat_roi_pct": _roi_pct(flat_pnl, total_calls),
            "ml_roi_pct": _roi_pct(ml_pnl, ml_count) if ml_count > 0 else None,
            "spread_covered_pct": _pct(sc_yes, sc_total),
            "spread_roi_pct": _roi_pct(sc_yes * FLAT_WIN_PROFIT - sc_no * STAKE, sc_total) if sc_total > 0 else None,
            "live_line_roi_pct": _roi_pct(ll_pnl, ll_count) if ll_count > 0 else None,
            "bk_line_roi_pct": _roi_pct(bk_pnl, bk_count) if bk_count > 0 else None,
            "bk_odds_roi_pct": _roi_pct(bk_odds_pnl, bk_odds_count) if bk_odds_count > 0 else None,
        }

    combos = []
    for role in roles:
        for margin in margins:
            for selection in selections:
                for half in halves:
                    subset = [c for c in all_calls if c["role"] == role and c["margin"] == margin and c["half"] == half]
                    combo = _build_combo(_select_calls(subset, selection), role, margin, selection, half=half)
                    if combo:
                        combos.append(combo)
                for quarter in quarters:
                    subset = [c for c in all_calls if c["role"] == role and c["margin"] == margin and c["quarter"] == quarter]
                    combo = _build_combo(_select_calls(subset, selection), role, margin, selection, quarter=quarter)
                    if combo:
                        combos.append(combo)

    combos.sort(key=lambda c: (c.get(sort_by) is not None, c.get(sort_by) or 0), reverse=True)
    total_computed = len(combos)
    return {
        "combos": combos[:limit],
        "total_computed": total_computed,
        "strict_bk": strict_bk,
        "has_synthetic_bk": not strict_bk and any(not c.get("_has_genuine_bk", True) for c in all_calls),
    }


# ── Audit 1: End-to-end ROI formula verification ────────────────────────────

def audit_roi_formulas(records, report, verbose=False):
    """Trace individual PW calls through the ROI formula and verify correctness."""
    report.section("1. End-to-end ROI formula verification")

    total_calls = 0
    ml_checked = 0
    ml_errors = 0
    spread_checked = 0
    spread_errors = 0
    correct_mismatches = 0
    sample_errors = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list):
            continue

        winner = str(rec.get("winner") or "").strip()
        home_score = rec.get("home_score")
        away_score = rec.get("away_score")
        if home_score is None or away_score is None:
            continue

        for call in calls:
            if not isinstance(call, dict):
                continue
            c = call.get("correct")
            if c is None:
                continue
            total_calls += 1

            pred_team = str(call.get("predicted_team") or "").strip()
            is_correct = bool(c)

            # Verify correct field matches actual winner
            expected_correct = (pred_team == winner) if pred_team and winner else None
            if expected_correct is not None and expected_correct != is_correct:
                correct_mismatches += 1
                if len(sample_errors) < 5:
                    gid = rec.get("game_id", "?")
                    sample_errors.append(
                        f"game {gid}: correct={is_correct} but pred={pred_team} winner={winner}")

            # Verify ML profit formula (NRL uses decimal odds)
            bk_ml = call.get("bk_moneyline")
            if bk_ml is not None:
                try:
                    bk_ml_dec = float(str(bk_ml))
                    if bk_ml_dec > 1:
                        ml_checked += 1
                        expected_profit = outcomes._decimal_odds_profit(bk_ml_dec) if is_correct else -STAKE

                        # Manual verification: decimal odds profit = STAKE * (odds - 1)
                        manual_win_profit = STAKE * (bk_ml_dec - 1.0)
                        manual_pnl = manual_win_profit if is_correct else -STAKE

                        if not _close_enough(expected_profit, manual_pnl, 0.001):
                            ml_errors += 1
                            if verbose and len(sample_errors) < 10:
                                sample_errors.append(
                                    f"ML formula mismatch: ml={bk_ml_dec} "
                                    f"expected={expected_profit:.4f} manual={manual_pnl:.4f}")
                except (ValueError, TypeError):
                    pass

            # Verify spread cover logic
            bk_spread = call.get("bk_spread")
            if bk_spread is not None and pred_team:
                team_margin = _nrl_team_margin(rec, pred_team)
                if team_margin is not None:
                    spread_checked += 1
                    try:
                        expected_cover = (team_margin + float(bk_spread)) > 0
                        fn_cover = outcomes._did_team_cover_bk_line(rec, pred_team, bk_spread)
                        if fn_cover is not None and expected_cover != fn_cover:
                            spread_errors += 1
                            if verbose and len(sample_errors) < 10:
                                sample_errors.append(
                                    f"Spread cover mismatch: margin={team_margin} "
                                    f"spread={bk_spread} expected={expected_cover} fn={fn_cover}")
                    except (ValueError, TypeError):
                        pass

    report.info(f"Total resolved PW calls: {total_calls}")
    report.info(f"ML profit checked: {ml_checked}, Spread cover checked: {spread_checked}")

    if correct_mismatches == 0:
        report.ok(f"All {total_calls} calls: 'correct' field matches winner")
    else:
        report.fail(f"{correct_mismatches} calls: 'correct' field != (pred_team == winner)")
        for e in sample_errors[:3]:
            report.info(f"  Example: {e}")

    if ml_errors == 0:
        report.ok(f"ML profit formula (decimal odds) verified for {ml_checked} calls")
    else:
        report.fail(f"{ml_errors}/{ml_checked} ML profit formula mismatches")

    if spread_errors == 0:
        report.ok(f"Spread cover logic verified for {spread_checked} calls")
    else:
        report.fail(f"{spread_errors}/{spread_checked} spread cover mismatches")


# ── Audit 2: Client vs Server spread cover discrepancy ──────────────────────

def audit_client_spread_cover(records, report, verbose=False):
    """Check for discrepancies between abs(margin) (client) and signed margin (server)."""
    report.section("2. Client vs Server spread cover (final_margin abs vs signed)")

    discrepancies = 0
    checked = 0
    examples = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list):
            continue

        home_score = rec.get("home_score")
        away_score = rec.get("away_score")
        if home_score is None or away_score is None:
            continue
        try:
            hs = int(home_score)
            aws = int(away_score)
        except (ValueError, TypeError):
            continue

        abs_margin = abs(hs - aws)  # what JS gets as final_margin

        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("correct") is None:
                continue
            bk_spread = call.get("bk_spread")
            if bk_spread is None:
                continue
            pred_team = str(call.get("predicted_team") or "").strip()
            if not pred_team:
                continue

            team_margin = _nrl_team_margin(rec, pred_team)
            if team_margin is None:
                continue

            try:
                bk_spread_f = float(bk_spread)
            except (ValueError, TypeError):
                continue

            checked += 1
            server_cover = (team_margin + bk_spread_f) > 0
            client_cover = (abs_margin + bk_spread_f) > 0

            if server_cover != client_cover:
                discrepancies += 1
                if len(examples) < 5:
                    gid = rec.get("game_id", "?")
                    examples.append(
                        f"game {gid}: team_margin={team_margin} abs_margin={abs_margin} "
                        f"spread={bk_spread_f} server={server_cover} client={client_cover}")

    report.info(f"Checked {checked} calls with bk_spread")

    if discrepancies == 0:
        report.ok(f"No abs(margin) vs signed margin discrepancies across {checked} calls")
    else:
        report.warn(f"{discrepancies}/{checked} calls where abs(margin) != signed team_margin "
                    f"for spread cover (fixed in #205 — client now uses _teamMargin)")
        report.info("These calls have predicted team that LOST — abs(margin) was wrong.")
        report.info("Client JS now uses signed team margin via _teamMargin() helper.")
        for e in examples:
            report.info(f"  {e}")


# ── Audit 3: Scenario stats cross-validation ────────────────────────────────

def audit_scenario_stats(records, report, verbose=False):
    """Recompute scenario stats from scratch and compare to compute_scenario_stats()."""
    report.section("3. Scenario stats recomputation cross-validation")

    # Get official stats
    official = outcomes.compute_scenario_stats(records)

    # Recompute from scratch
    manual = defaultdict(lambda: {
        "total_calls": 0, "correct": 0, "game_ids": set(),
        "bk_odds_pnl": 0.0, "bk_odds_count": 0,
        "bk_spr_pnl": 0.0, "bk_spr_count": 0, "bk_spr_covered": 0,
    })

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list):
            continue
        spread_fav = _nrl_spread_fav(rec)

        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("_suppressed"):
                continue
            c = call.get("correct")
            if c is None:
                continue
            is_correct = bool(c)
            pred_team = str(call.get("predicted_team") or "").strip()

            skey = outcomes.scenario_key_from_call(call, spread_fav)
            if not skey:
                continue

            acc = manual[skey]
            acc["total_calls"] += 1
            acc["game_ids"].add(str(rec.get("match_id") or rec.get("game_id") or ""))
            if is_correct:
                acc["correct"] += 1

            # BK ML (NRL decimal odds)
            bk_ml = call.get("bk_moneyline")
            if bk_ml is not None:
                try:
                    bk_ml_val = float(str(bk_ml))
                    if bk_ml_val > 1:
                        acc["bk_odds_count"] += 1
                        acc["bk_odds_pnl"] += STAKE * (bk_ml_val - 1) if is_correct else -STAKE
                except (ValueError, TypeError):
                    pass

            # BK Spread
            bk_spread = call.get("bk_spread")
            if bk_spread is not None:
                bk_cover = outcomes._did_team_cover_bk_line(rec, pred_team, bk_spread)
                if bk_cover is not None:
                    acc["bk_spr_count"] += 1
                    spr_profit = FLAT_WIN_PROFIT
                    bk_spr_price = call.get("bk_spread_price")
                    if bk_spr_price is not None:
                        try:
                            spr_profit = outcomes._decimal_odds_profit(float(bk_spr_price))
                        except (ValueError, TypeError):
                            pass
                    if bk_cover:
                        acc["bk_spr_covered"] += 1
                        acc["bk_spr_pnl"] += spr_profit
                    else:
                        acc["bk_spr_pnl"] -= STAKE

    # Compare
    all_keys = set(official.keys()) | set(manual.keys())
    report.info(f"Official scenarios: {len(official)}, Manual: {len(manual)}")

    missing_in_official = set(manual.keys()) - set(official.keys())
    missing_in_manual = set(official.keys()) - set(manual.keys())
    if missing_in_official:
        report.fail(f"{len(missing_in_official)} scenarios in manual but not official: "
                    f"{list(missing_in_official)[:3]}")
    if missing_in_manual:
        report.fail(f"{len(missing_in_manual)} scenarios in official but not manual: "
                    f"{list(missing_in_manual)[:3]}")

    mismatches = 0
    for skey in sorted(all_keys):
        off = official.get(skey, {})
        man = manual.get(skey, {})

        # Derive total_games from game_ids set for manual comparison
        man["total_games"] = len(man.get("game_ids", set()))
        for field in ("total_calls", "total_games", "correct", "bk_odds_count", "bk_spr_count", "bk_spr_covered"):
            ov = off.get(field, 0)
            mv = man.get(field, 0)
            if ov != mv:
                mismatches += 1
                report.fail(f"Scenario {skey}: {field} official={ov} manual={mv}")

        def _roi_pct(pnl, n):
            return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None

        man_ml_roi = _roi_pct(man.get("bk_odds_pnl", 0), man.get("bk_odds_count", 0))
        off_ml_roi = off.get("bk_odds_roi_pct")
        if not _close_enough(man_ml_roi, off_ml_roi, 0.15):
            mismatches += 1
            report.fail(f"Scenario {skey}: bk_odds_roi_pct official={off_ml_roi} manual={man_ml_roi}")

        man_spr_roi = _roi_pct(man.get("bk_spr_pnl", 0), man.get("bk_spr_count", 0))
        off_spr_roi = off.get("bk_spr_roi_pct")
        if not _close_enough(man_spr_roi, off_spr_roi, 0.15):
            mismatches += 1
            report.fail(f"Scenario {skey}: bk_spr_roi_pct official={off_spr_roi} manual={man_spr_roi}")

    if mismatches == 0:
        report.ok(f"All {len(all_keys)} scenarios match between official and manual recomputation")
    else:
        report.fail(f"{mismatches} field mismatches across {len(all_keys)} scenarios")


# ── Audit 4: BK Spread price fallback analysis ──────────────────────────────

def audit_spread_price_fallback(records, report, verbose=False):
    """Quantify bk_spread_price coverage and impact of fallback."""
    report.section("4. BK Spread price fallback audit")

    total_with_spread = 0
    has_price = 0
    no_price = 0
    price_values = []

    pnl_with_actual = 0.0
    pnl_all_fallback = 0.0
    count_actual = 0
    count_fallback = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list):
            continue

        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("correct") is None or call.get("_suppressed"):
                continue

            bk_spread = call.get("bk_spread")
            if bk_spread is None:
                continue

            pred_team = str(call.get("predicted_team") or "").strip()
            bk_cover = outcomes._did_team_cover_bk_line(rec, pred_team, bk_spread)
            if bk_cover is None:
                continue

            total_with_spread += 1
            bk_spr_price = call.get("bk_spread_price")

            if bk_spr_price is not None:
                has_price += 1
                try:
                    price_dec = float(bk_spr_price)
                    price_values.append(price_dec)
                    actual_profit = outcomes._decimal_odds_profit(price_dec)

                    if bk_cover:
                        pnl_with_actual += actual_profit
                    else:
                        pnl_with_actual -= STAKE

                    if bk_cover:
                        pnl_all_fallback += FLAT_WIN_PROFIT
                    else:
                        pnl_all_fallback -= STAKE

                    count_actual += 1
                except (ValueError, TypeError):
                    no_price += 1
            else:
                no_price += 1
                count_fallback += 1

    report.info(f"Total calls with bk_spread: {total_with_spread}")
    report.info(f"  With bk_spread_price: {has_price} ({has_price/max(total_with_spread,1)*100:.1f}%)")
    report.info(f"  Without (using -110 fallback): {no_price} ({no_price/max(total_with_spread,1)*100:.1f}%)")

    if count_actual > 0:
        roi_actual = pnl_with_actual / (count_actual * STAKE) * 100
        roi_fallback = pnl_all_fallback / (count_actual * STAKE) * 100
        report.info(f"ROI impact (calls WITH actual price, n={count_actual}):")
        report.info(f"  Using actual price: {roi_actual:+.1f}%")
        report.info(f"  If all used -110 fallback: {roi_fallback:+.1f}%")
        report.info(f"  Difference: {roi_actual - roi_fallback:+.2f}pp")

    if price_values:
        from statistics import median
        report.info(f"Price distribution (decimal): min={min(price_values):.2f} "
                    f"max={max(price_values):.2f} median={median(price_values):.2f}")
    else:
        report.ok("No bk_spread_price values to audit (all using fallback)")


# ── Audit 5: Edge cases and missing data ─────────────────────────────────────

def audit_edge_cases(records, report, verbose=False):
    """Check consistency of missing/null field handling."""
    report.section("5. Edge cases and missing data")

    total_calls = 0
    no_scenario_key = 0
    has_ml_no_spread = 0
    has_spread_no_ml = 0
    has_both = 0
    has_neither = 0
    suppressed = 0
    missing_spread_role = 0
    missing_margin = 0
    missing_edge = 0
    missing_quarter = 0
    backfill_calls = 0
    live_calls = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list):
            continue
        spread_fav = _nrl_spread_fav(rec)

        for call in calls:
            if not isinstance(call, dict):
                continue
            if call.get("_suppressed"):
                suppressed += 1
                continue
            c = call.get("correct")
            if c is None:
                continue
            total_calls += 1

            has_ml = call.get("bk_moneyline") is not None
            has_spr = call.get("bk_spread") is not None
            if has_ml and not has_spr:
                has_ml_no_spread += 1
            elif has_spr and not has_ml:
                has_spread_no_ml += 1
            elif has_ml and has_spr:
                has_both += 1
            else:
                has_neither += 1

            skey = outcomes.scenario_key_from_call(call, spread_fav)
            if not skey:
                no_scenario_key += 1
                role = str(call.get("spread_role") or "").strip().lower()
                if not role:
                    pred = str(call.get("predicted_team") or "").strip()
                    role = outcomes._classify_spread_role(pred, spread_fav)
                if not role:
                    missing_spread_role += 1
                if call.get("score_margin_at_fire") is None:
                    missing_margin += 1
                if call.get("avg_edge") is None:
                    missing_edge += 1
                if not str(call.get("quarter") or "").strip():
                    missing_quarter += 1

            source = str(call.get("source") or "").lower()
            if source == "backfill":
                backfill_calls += 1
            elif source == "live":
                live_calls += 1

    report.info(f"Total resolved calls: {total_calls}, Suppressed: {suppressed}")
    report.info(f"BK data: both={has_both}, ML only={has_ml_no_spread}, "
                f"Spread only={has_spread_no_ml}, neither={has_neither}")
    report.info(f"Source: live={live_calls}, backfill={backfill_calls}, "
                f"other={total_calls - live_calls - backfill_calls}")

    if no_scenario_key > 0:
        report.warn(f"{no_scenario_key}/{total_calls} calls lack scenario key "
                    f"(role={missing_spread_role}, margin={missing_margin}, "
                    f"edge={missing_edge}, quarter={missing_quarter})")
    else:
        report.ok(f"All {total_calls} calls have valid scenario keys")


# ── Audit 6: Cross-surface API validation ────────────────────────────────────

def audit_api_cross_validation(records, base_url, report, verbose=False):
    """Cross-validate ROI across API surfaces (requires running dashboard)."""
    report.section("6. Cross-surface API validation")
    local_stats = outcomes.compute_scenario_stats(records)

    # 6a: /api/pw-scenarios vs local compute_scenario_stats
    scenarios_data = _api_get(base_url, "/api/pw-scenarios")
    if scenarios_data is None:
        report.warn("Could not reach /api/pw-scenarios — skipping API cross-validation")
        return

    api_scenarios = scenarios_data.get("scenarios", {})
    report.info(f"Local scenarios: {len(local_stats)}, API scenarios: {len(api_scenarios)}")

    scenario_mismatches = 0
    for skey in sorted(set(local_stats.keys()) | set(api_scenarios.keys())):
        scenario_mismatches += _compare_bucket_fields(
            local_stats.get(skey, {}),
            api_scenarios.get(skey, {}),
            ("total_calls", "total_games", "correct", "bk_odds_count", "bk_spr_count", "bk_spr_covered"),
            ("bk_odds_roi_pct", "bk_spr_roi_pct", "accuracy_pct"),
            skey,
            verbose=verbose,
            report=report,
        )

    if scenario_mismatches == 0:
        report.ok(f"/api/pw-scenarios matches local compute_scenario_stats ({len(api_scenarios)} scenarios)")
    else:
        report.fail(f"{scenario_mismatches} mismatches between /api/pw-scenarios and local")

    # 6b: /api/pw-calls scenario enrichment + PW accuracy block parity
    pw_calls_data = _api_get(base_url, "/api/pw-calls")
    if pw_calls_data:
        games = pw_calls_data.get("games", [])
        enriched_ok = 0
        enriched_mismatch = 0
        for g in games:
            for c in g.get("calls", []):
                skey = c.get("scenario_key", "")
                ss = c.get("scenario_stats")
                if skey and ss:
                    local_ss = local_stats.get(skey)
                    if local_ss and _compare_bucket_fields(
                        local_ss, ss,
                        ("total_calls", "total_games", "correct", "bk_odds_count", "bk_spr_count", "bk_spr_covered"),
                        ("bk_odds_roi_pct", "bk_spr_roi_pct", "accuracy_pct"),
                        f"pw-calls.{skey}",
                        verbose=verbose,
                        report=report,
                    ) == 0:
                        enriched_ok += 1
                    else:
                        enriched_mismatch += 1
        if enriched_mismatch == 0:
            report.ok(f"/api/pw-calls scenario enrichment matches ({enriched_ok} calls checked)")
        else:
            report.fail(f"{enriched_mismatch} pw-calls have stale/mismatched scenario_stats")

        api_acc = pw_calls_data.get("predicted_winner_accuracy", {})
        client_acc = _summarize_pw_calls_games(games)
        pw_calls_mismatches = _compare_bucket_fields(
            client_acc, api_acc,
            ("total_calls", "correct", "bk_odds_count", "bk_line_count"),
            ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct"),
            "pw-calls.accuracy",
            verbose=verbose,
            report=report,
        )
        if pw_calls_mismatches == 0:
            report.ok("/api/pw-calls predicted_winner_accuracy matches raw-call recomputation")
        else:
            report.fail(f"{pw_calls_mismatches} mismatches in /api/pw-calls predicted_winner_accuracy")
    else:
        report.warn("Could not reach /api/pw-calls — skipping enrichment and accuracy-block checks")

    # 6c: /api/analysis PW accuracy summary
    analysis_data = _api_get(base_url, "/api/analysis")
    if analysis_data:
        api_acc = analysis_data.get("predicted_winner_accuracy", {})
        local_acc = _build_local_pw_accuracy_summary(pw_matrix.compute_pw_matrix(records))

        top_level_mismatches = _compare_bucket_fields(
            local_acc, api_acc,
            ("total_calls", "correct", "bk_odds_count", "bk_line_count"),
            ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct"),
            "analysis.summary",
            verbose=verbose,
            report=report,
        )
        if top_level_mismatches == 0:
            report.ok("/api/analysis predicted_winner_accuracy summary matches local recomputation")
        else:
            report.fail(f"{top_level_mismatches} mismatches in /api/analysis predicted_winner_accuracy summary")

        breakdown_specs = (
            ("by_threshold", ("calls", "correct", "bk_odds_count", "bk_line_count"),
             ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct")),
            ("by_consensus", ("calls", "correct", "bk_odds_count", "bk_line_count"),
             ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct")),
            ("by_half", ("calls", "correct", "bk_odds_count", "bk_line_count"),
             ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct")),
            ("by_spread_role", ("calls", "correct", "bk_odds_count", "bk_line_count"),
             ("accuracy_pct", "bk_odds_roi_pct", "bk_line_roi_pct")),
        )
        breakdown_mismatches = 0
        bucket_count = 0
        for group_name, count_fields, pct_fields in breakdown_specs:
            local_group = local_acc.get(group_name, {}) or {}
            api_group = api_acc.get(group_name, {}) or {}
            for bucket_key in sorted(set(local_group.keys()) | set(api_group.keys())):
                bucket_count += 1
                breakdown_mismatches += _compare_bucket_fields(
                    local_group.get(bucket_key, {}) or {},
                    api_group.get(bucket_key, {}) or {},
                    count_fields,
                    pct_fields,
                    f"analysis.{group_name}.{bucket_key}",
                    verbose=verbose,
                    report=report,
                )
        if breakdown_mismatches == 0:
            report.ok(f"/api/analysis PW breakdown buckets match local across {bucket_count} buckets")
        else:
            report.fail(f"{breakdown_mismatches} mismatches across /api/analysis PW breakdown buckets")
    else:
        report.warn("Could not reach /api/analysis — skipping PW accuracy validation")

    # 6d: /api/pw-matrix — full matrix parity
    matrix_data = _api_get(base_url, "/api/pw-matrix")
    if matrix_data:
        local_matrix = pw_matrix.compute_pw_matrix(records)
        matrix_mismatches = 0
        matrix_cells_checked = 0
        for pair_key in sorted(set(local_matrix.get("by_matrix", {}).keys()) | set(matrix_data.get("by_matrix", {}).keys())):
            local_cells = (local_matrix.get("by_matrix", {}).get(pair_key) or {})
            api_cells = (matrix_data.get("by_matrix", {}).get(pair_key) or {})
            for cell_key in sorted(set(local_cells.keys()) | set(api_cells.keys())):
                matrix_cells_checked += 1
                matrix_mismatches += _compare_bucket_fields(
                    local_cells.get(cell_key, {}) or {},
                    api_cells.get(cell_key, {}) or {},
                    ("calls", "correct", "bk_line_count", "bk_odds_count"),
                    ("pct", "bk_line_roi_pct", "bk_odds_roi_pct"),
                    f"pw-matrix.{pair_key}.{cell_key}",
                    verbose=verbose,
                    report=report,
                )
        if matrix_mismatches == 0:
            report.ok(f"/api/pw-matrix matches local across {matrix_cells_checked} cells")
        else:
            report.fail(f"{matrix_mismatches} mismatches across /api/pw-matrix ({matrix_cells_checked} cells)")

        pin_params = {
            "matrix_pin_quarter": "Q4",
            "matrix_pin_spread_role": "underdog",
            "matrix_pin_margin": "leading",
        }
        pinned_data = _api_get(base_url, "/api/pw-matrix", params=pin_params)
        if pinned_data:
            pinned_local = pw_matrix.compute_pw_matrix(
                records,
                pins={"quarter": "Q4", "spread_role": "underdog", "margin": "leading"},
            )
            pin_mismatches = 0
            pin_cells_checked = 0
            for pair_key in sorted(set(pinned_local.get("by_matrix", {}).keys()) | set(pinned_data.get("by_matrix", {}).keys())):
                local_cells = (pinned_local.get("by_matrix", {}).get(pair_key) or {})
                api_cells = (pinned_data.get("by_matrix", {}).get(pair_key) or {})
                for cell_key in sorted(set(local_cells.keys()) | set(api_cells.keys())):
                    pin_cells_checked += 1
                    pin_mismatches += _compare_bucket_fields(
                        local_cells.get(cell_key, {}) or {},
                        api_cells.get(cell_key, {}) or {},
                        ("calls", "correct", "bk_line_count", "bk_odds_count"),
                        ("pct", "bk_line_roi_pct", "bk_odds_roi_pct"),
                        f"pw-matrix.pinned.{pair_key}.{cell_key}",
                        verbose=verbose,
                        report=report,
                    )
            if pin_mismatches == 0:
                report.ok(f"/api/pw-matrix pins match local across {pin_cells_checked} cells")
            else:
                report.fail(f"{pin_mismatches} mismatches across pinned /api/pw-matrix ({pin_cells_checked} cells)")
        else:
            report.warn("Could not reach pinned /api/pw-matrix path — skipping matrix pin validation")
    else:
        report.warn("Could not reach /api/pw-matrix — skipping matrix validation")

    # 6e: /api/pw-roi-combos — full combo parity
    combos_data = _api_get(base_url, "/api/pw-roi-combos", params={"limit": "100"})
    if combos_data:
        local_combos_data = _compute_local_roi_combos(records, limit=1000)
        if combos_data.get("total_computed", 0) == local_combos_data.get("total_computed", 0):
            report.ok(f"/api/pw-roi-combos total_computed matches ({combos_data.get('total_computed', 0)})")
        else:
            report.fail(f"/api/pw-roi-combos total_computed: api={combos_data.get('total_computed', 0)} local={local_combos_data.get('total_computed', 0)}")

        api_by_key = {
            (c.get("role"), c.get("margin"), c.get("call_selection"), c.get("half"), c.get("quarter")): c
            for c in (combos_data.get("combos", []) or [])
        }
        local_by_key = {
            (c.get("role"), c.get("margin"), c.get("call_selection"), c.get("half"), c.get("quarter")): c
            for c in (local_combos_data.get("combos", []) or [])
        }
        combo_mismatches = 0
        for combo_key in sorted(api_by_key.keys()):
            combo_mismatches += _compare_bucket_fields(
                local_by_key.get(combo_key, {}) or {},
                api_by_key.get(combo_key, {}) or {},
                ("total_calls", "correct"),
                ("accuracy_pct", "ml_roi_pct", "bk_line_roi_pct", "bk_odds_roi_pct"),
                f"pw-roi-combos.{combo_key}",
                verbose=verbose,
                report=report,
            )
        if combo_mismatches == 0:
            report.ok(f"/api/pw-roi-combos matches local across {len(api_by_key)} returned combos")
        else:
            report.fail(f"{combo_mismatches} mismatches across /api/pw-roi-combos")
        if combos_data.get("total_computed", 0) > len(api_by_key):
            report.info(f"/api/pw-roi-combos returns top {len(api_by_key)} rows due endpoint limit; validated all returned rows against local compute")
    else:
        report.warn("Could not reach /api/pw-roi-combos — skipping combo validation")


# ── Audit 7: Filter divergence between surfaces ─────────────────────────────

def audit_filter_divergence(report):
    """Document which default filters each surface uses."""
    report.section("7. Filter divergence between surfaces")

    report.info("Surface default filter comparison:")
    report.info("  /api/pw-calls:")
    report.info("    - scenario_stats: computed from ALL history (no filters)")
    report.info("    - calls: filtered by season/quarter/role/margin/source/edge/polarity")
    report.info("    - suppressed: excluded via _suppressed flag")
    report.info("  /api/pw-scenarios:")
    report.info("    - accepts call_selection, season_segment, season_year")
    report.info("    - suppressed: excluded in compute_scenario_stats")
    report.info("  /api/analysis (pw_accuracy):")
    report.info("    - accepts season_segment, spread_role, season_year, margin_at_fire")
    report.info("    - suppressed: excluded in pre-filter")
    report.info("  /api/pw-matrix:")
    report.info("    - serves matrix/breakdown data from pw_matrix.compute_pw_matrix")
    report.info("    - accepts season, segment, strict_bk, and matrix_pin_* filters")
    report.info("  /api/pw-roi-combos:")
    report.info("    - iterates hardcoded combos (role × margin × call_sel × half/quarter)")
    report.info("    - accepts season, segment, limit, min_calls, strict_bk")
    report.warn("KEY DIVERGENCE: /api/pw-calls scenario_stats uses unfiltered history, "
                "so stats remain stable regardless of PW tab filter state. This is by design "
                "but means scenario badges may not match filtered accuracy block stats.")
    report.warn("CLIENT-SIDE: PW accuracy stats block recomputes from filtered /api/pw-calls "
                "data, so it reflects current filters. Scenario badges do NOT reflect filters.")


# ── Audit 8: Scenario combination completeness ──────────────────────────────

def audit_scenario_completeness(records, report, verbose=False):
    """Check all observed vs theoretical scenario combinations."""
    report.section("8. Scenario combination completeness")

    roles = {"favorite", "underdog"}
    margins = {"leading", "trailing"}
    edges = {"10+", "5-10", "0-5", "<0"}
    quarters = {"Q1", "Q2", "Q3", "Q4", "ET"}

    theoretical = set()
    for r in roles:
        for m in margins:
            for e in edges:
                for q in quarters:
                    theoretical.add(f"{r}|{m}|{e}|{q}")

    stats = outcomes.compute_scenario_stats(records)
    observed = set(stats.keys())

    report.info(f"Theoretical combinations: {len(theoretical)}")
    report.info(f"Observed combinations: {len(observed)}")

    missing = theoretical - observed
    extra = observed - theoretical
    if missing:
        report.info(f"Unobserved combos ({len(missing)}): {sorted(missing)[:10]}...")
    if extra:
        report.warn(f"Unexpected combos outside theoretical set: {sorted(extra)}")
    else:
        report.ok("All observed scenarios are within expected theoretical set")

    empty = [k for k, v in stats.items() if v.get("total_calls", 0) == 0]
    if empty:
        report.fail(f"{len(empty)} scenarios with 0 calls in output: {empty[:5]}")
    else:
        report.ok("No empty (0-call) scenarios in output")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ROI Audit — NRL end-to-end validation (#205)")
    parser.add_argument("--offline", action="store_true",
                        help="Skip API calls (offline data-only checks)")
    parser.add_argument("--verbose", action="store_true",
                        help="Show per-call trace details")
    parser.add_argument("--port", type=int, default=8898,
                        help="Dashboard port (default: 8898)")
    parser.add_argument("--json", action="store_true",
                        help="Output results as JSON")
    args = parser.parse_args()

    base_url = f"http://127.0.0.1:{args.port}"

    print("Loading NRL game history...")
    records = _load_history()
    print(f"Loaded {len(records)} game records")

    report = AuditReport()

    # Run all audits
    audit_roi_formulas(records, report, args.verbose)
    audit_client_spread_cover(records, report, args.verbose)
    audit_scenario_stats(records, report, args.verbose)
    audit_spread_price_fallback(records, report, args.verbose)
    audit_edge_cases(records, report, args.verbose)
    if not args.offline:
        audit_api_cross_validation(records, base_url, report, args.verbose)
    else:
        report.section("6. Cross-surface API validation")
        report.info("Skipped (--offline mode)")
    audit_filter_divergence(report)
    audit_scenario_completeness(records, report, args.verbose)

    if args.json:
        result = {
            "league": "nrl",
            "records": len(records),
            "totals": report.totals,
            "sections": [{
                "name": s["name"],
                "results": [{"status": st, "msg": msg} for st, msg in s["results"]]
            } for s in report.sections],
        }
        print(json.dumps(result, indent=2))
    else:
        passed = report.print_report()
        sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
