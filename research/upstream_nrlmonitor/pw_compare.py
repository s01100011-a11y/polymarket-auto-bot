#!/usr/bin/env python3
"""PW config comparison and filter evaluation tool (#116).

Offline analysis of predicted winner call performance under different
configurations. Read-only — never modifies game_history or config.

Usage:
    # A/B comparison: baseline vs edge-gated
    python3 pw_compare.py --since 2026-04-01 --until 2026-05-01 --edge-gate 1.0

    # A/B: compare two arbitrary thresholds
    python3 pw_compare.py --since 2026-04-01 --threshold-a 60 --threshold-b 75

    # A/B: compare two margin gates
    python3 pw_compare.py --margin-gate-a=-12 --margin-gate-b=-8

    # Filter evaluation: test suppression strategies
    python3 pw_compare.py --evaluate-filters --since 2026-04-01

    # Compare stored calls vs freshly generated
    python3 pw_compare.py --vs-stored --since 2026-04-01

    # Restrict to specific season
    python3 pw_compare.py --season 2026 --evaluate-filters
"""

import argparse
import copy
import json
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import outcomes as outcomes_mod
from backfill import _generate_pw_calls_for_record

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_config():
    cfg_path = os.path.join(SCRIPT_DIR, "config.json")
    if os.path.exists(cfg_path):
        with open(cfg_path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _date_filter(records, since=None, until=None, season=None):
    """Filter records by date range and/or season."""
    out = []
    for r in records:
        if season and str(r.get("season", "")) != str(season):
            continue
        dt = str(r.get("kickoff_utc") or r.get("date") or r.get("ts") or "")[:10]
        if since and dt < since:
            continue
        if until and dt > until:
            continue
        out.append(r)
    return out


def _call_source(call):
    """Derive source label for a PW call: 'live' or 'backfill'."""
    src = call.get("source")
    if src:
        return src
    # Fallback: synthetic flag (pre-#121 calls)
    return "backfill" if call.get("synthetic") else "live"


def _filter_calls_by_source(calls, source):
    """Filter calls by source ('live' or 'backfill'). Returns all if source is empty."""
    if not source:
        return calls
    return [c for c in calls if _call_source(c) == source]


def _filter_calls_by_minute(calls, min_minute=0, max_minute=999):
    """Filter calls by game_minute range."""
    if min_minute <= 0 and max_minute >= 999:
        return calls
    return [c for c in calls
            if min_minute <= (c.get("game_minute") or 0) <= max_minute]


def _filter_calls_by_margin(calls, min_margin=None, max_margin=None):
    """Filter calls by absolute margin at fire.

    min_margin: exclude calls where abs(margin) > min_margin (remove blowouts)
    max_margin: exclude calls where abs(margin) < max_margin (remove close games)
    """
    if min_margin is None and max_margin is None:
        return calls
    out = []
    for c in calls:
        m = abs(c.get("score_margin_at_fire") or 0)
        if min_margin is not None and m > min_margin:
            continue
        if max_margin is not None and m < max_margin:
            continue
        out.append(c)
    return out


def _apply_call_filters(calls, args):
    """Apply all call-level filters from args."""
    calls = _filter_calls_by_source(calls, args.source)
    calls = _filter_calls_by_minute(calls, args.min_minute, args.max_minute)
    calls = _filter_calls_by_margin(calls, args.min_margin, args.max_margin)
    return calls


def _decimal_roi(odds_str, correct):
    """ROI from decimal odds: profit = stake * (odds - 1) if correct, else -stake."""
    if not odds_str:
        return None
    try:
        odds = float(odds_str)
        if odds <= 1:
            return None
        return (odds - 1) if correct else -1.0
    except (TypeError, ValueError):
        return None


def _quarter_from_minute(minute):
    if minute is None:
        return ""
    if minute < 20:
        return "Q1"
    elif minute < 40:
        return "Q2"
    elif minute < 60:
        return "Q3"
    elif minute < 80:
        return "Q4"
    return "ET"


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze_calls(calls, label=""):
    """Compute accuracy, ROI, and breakdowns for a list of PW calls."""
    if not calls:
        return {"label": label, "total": 0}

    decided = [c for c in calls if c.get("correct") is not None]
    correct = sum(1 for c in decided if c["correct"])
    total = len(decided)
    accuracy = (correct / total * 100) if total else 0

    # Flat ROI: +1 / -1 units
    flat_pnl = sum(1 if c["correct"] else -1 for c in decided)

    # ML ROI from decimal odds
    ml_pnl = 0.0
    ml_count = 0
    for c in decided:
        ml = c.get("moneyline") or c.get("live_moneyline")
        roi = _decimal_roi(ml, c["correct"])
        if roi is not None:
            ml_pnl += roi
            ml_count += 1

    # Spread covered
    spread_yes = 0
    spread_no = 0
    for c in decided:
        sp = c.get("spread")
        margin = c.get("score_margin_at_fire")
        if sp is not None and margin is not None:
            try:
                covered = float(margin) + float(sp) > 0
                if covered:
                    spread_yes += 1
                else:
                    spread_no += 1
            except (TypeError, ValueError):
                pass

    # By half
    by_half = {}
    for c in decided:
        h = c.get("half", "?")
        if h not in by_half:
            by_half[h] = {"calls": 0, "correct": 0}
        by_half[h]["calls"] += 1
        if c["correct"]:
            by_half[h]["correct"] += 1

    # By quarter
    by_quarter = {}
    for c in decided:
        q = c.get("quarter") or _quarter_from_minute(c.get("game_minute"))
        if not q:
            continue
        if q not in by_quarter:
            by_quarter[q] = {"calls": 0, "correct": 0}
        by_quarter[q]["calls"] += 1
        if c["correct"]:
            by_quarter[q]["correct"] += 1

    # Edge distribution
    edge_bins = {"<-5": [0, 0], "-5..0": [0, 0], "0..+5": [0, 0],
                 "+5..+10": [0, 0], "+10+": [0, 0]}
    for c in decided:
        e = c.get("avg_edge")
        if e is None:
            continue
        if e < -5:
            b = "<-5"
        elif e < 0:
            b = "-5..0"
        elif e < 5:
            b = "0..+5"
        elif e < 10:
            b = "+5..+10"
        else:
            b = "+10+"
        edge_bins[b][0] += 1
        if c["correct"]:
            edge_bins[b][1] += 1

    # By consensus
    by_consensus = {}
    for c in decided:
        con = c.get("consensus", "?")
        if con not in by_consensus:
            by_consensus[con] = {"calls": 0, "correct": 0}
        by_consensus[con]["calls"] += 1
        if c["correct"]:
            by_consensus[con]["correct"] += 1

    # Leading vs trailing
    leading = [c for c in decided if (c.get("score_margin_at_fire") or 0) >= 0]
    trailing = [c for c in decided if (c.get("score_margin_at_fire") or 0) < 0]
    lead_correct = sum(1 for c in leading if c["correct"])
    trail_correct = sum(1 for c in trailing if c["correct"])

    return {
        "label": label,
        "total": total,
        "correct": correct,
        "accuracy": accuracy,
        "flat_pnl": flat_pnl,
        "flat_roi": (flat_pnl / total * 100) if total else 0,
        "ml_pnl": round(ml_pnl, 2),
        "ml_count": ml_count,
        "ml_roi": (ml_pnl / ml_count * 100) if ml_count else 0,
        "spread_yes": spread_yes,
        "spread_no": spread_no,
        "spread_pct": (spread_yes / (spread_yes + spread_no) * 100) if (spread_yes + spread_no) else 0,
        "by_half": by_half,
        "by_quarter": by_quarter,
        "by_consensus": by_consensus,
        "edge_bins": edge_bins,
        "leading": len(leading),
        "leading_correct": lead_correct,
        "leading_acc": (lead_correct / len(leading) * 100) if leading else 0,
        "trailing": len(trailing),
        "trailing_correct": trail_correct,
        "trailing_acc": (trail_correct / len(trailing) * 100) if trailing else 0,
    }


def print_analysis(a, indent=0):
    """Pretty-print analysis result."""
    pad = " " * indent
    if a["total"] == 0:
        print(f"{pad}{a['label']}: no calls")
        return

    print(f"{pad}{a['label']}:")
    print(f"{pad}  Calls: {a['total']}  Correct: {a['correct']}  Accuracy: {a['accuracy']:.1f}%")
    print(f"{pad}  Flat ROI: {a['flat_pnl']:+.0f}u ({a['flat_roi']:+.1f}%)")
    if a["ml_count"]:
        print(f"{pad}  ML ROI:  {a['ml_pnl']:+.2f}u ({a['ml_roi']:+.1f}%) [{a['ml_count']} with odds]")
    if a["spread_yes"] + a["spread_no"]:
        print(f"{pad}  Spread:  {a['spread_yes']}/{a['spread_yes'] + a['spread_no']} covered ({a['spread_pct']:.1f}%)")
    print(f"{pad}  Leading: {a['leading']} calls ({a['leading_acc']:.1f}% acc)  "
          f"Trailing: {a['trailing']} calls ({a['trailing_acc']:.1f}% acc)")

    # Half breakdown
    if a["by_half"]:
        parts = []
        for h in ["H1", "H2", "ET"]:
            d = a["by_half"].get(h)
            if d:
                acc = d["correct"] / d["calls"] * 100 if d["calls"] else 0
                parts.append(f"{h}: {d['calls']} ({acc:.0f}%)")
        print(f"{pad}  By half: {', '.join(parts)}")

    # Quarter breakdown
    if a["by_quarter"]:
        parts = []
        for q in ["Q1", "Q2", "Q3", "Q4", "ET"]:
            d = a["by_quarter"].get(q)
            if d:
                acc = d["correct"] / d["calls"] * 100 if d["calls"] else 0
                parts.append(f"{q}: {d['calls']} ({acc:.0f}%)")
        print(f"{pad}  By quarter: {', '.join(parts)}")

    # Edge distribution
    edge_parts = []
    for b in ["<-5", "-5..0", "0..+5", "+5..+10", "+10+"]:
        cnt, cor = a["edge_bins"].get(b, [0, 0])
        if cnt:
            acc = cor / cnt * 100
            edge_parts.append(f"{b}: {cnt} ({acc:.0f}%)")
    if edge_parts:
        print(f"{pad}  Edge dist: {', '.join(edge_parts)}")

    # Consensus
    if a["by_consensus"]:
        parts = []
        for con in ["strong", "conflicted", "ml_only", "historical_only"]:
            d = a["by_consensus"].get(con)
            if d:
                acc = d["correct"] / d["calls"] * 100 if d["calls"] else 0
                parts.append(f"{con}: {d['calls']} ({acc:.0f}%)")
        if parts:
            print(f"{pad}  Consensus: {', '.join(parts)}")


def print_comparison(a, b):
    """Side-by-side comparison of two analyses."""
    print("=" * 70)
    print(f"  {'':30s} {'Config A':>15s}  {'Config B':>15s}  {'Delta':>10s}")
    print("-" * 70)

    def _row(label, va, vb, fmt=".1f"):
        try:
            delta = vb - va
            sa = format(va, fmt)
            sb = format(vb, fmt)
            sd = format(delta, "+" + fmt.lstrip("+"))
            print(f"  {label:30s} {sa:>14s}  {sb:>14s}  {sd:>10s}")
        except (TypeError, ValueError):
            print(f"  {label:30s} {str(va):>14s}  {str(vb):>14s}")

    _row("Calls", a["total"], b["total"], "d")
    _row("Correct", a["correct"], b["correct"], "d")
    _row("Accuracy %", a["accuracy"], b["accuracy"], ".1f")
    _row("Flat P&L (units)", a["flat_pnl"], b["flat_pnl"], "+.0f")
    _row("Flat ROI %", a["flat_roi"], b["flat_roi"], "+.1f")
    if a["ml_count"] or b["ml_count"]:
        _row("ML ROI %", a["ml_roi"], b["ml_roi"], "+.1f")
    if (a["spread_yes"] + a["spread_no"]) or (b["spread_yes"] + b["spread_no"]):
        _row("Spread Covered %", a["spread_pct"], b["spread_pct"], ".1f")
    _row("Leading calls", a["leading"], b["leading"], "d")
    _row("Leading acc %", a["leading_acc"], b["leading_acc"], ".1f")
    _row("Trailing calls", a["trailing"], b["trailing"], "d")
    _row("Trailing acc %", a["trailing_acc"], b["trailing_acc"], ".1f")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Generate PW calls with a given config
# ---------------------------------------------------------------------------

def generate_calls(records, config, threshold=65, cooldown=5):
    """Run _generate_pw_calls_for_record for each game, return flat call list with game context."""
    all_calls = []
    for i, rec in enumerate(records):
        prior = records[:i]
        calls = _generate_pw_calls_for_record(rec, prior, config,
                                              threshold=threshold,
                                              cooldown_minutes=cooldown)
        winner = rec.get("winner")
        for c in calls:
            if c.get("correct") is None and winner:
                c["correct"] = c.get("predicted_team") == winner
            # Attach game context for ROI
            if not c.get("moneyline"):
                _is_home = c.get("predicted_team") == rec.get("home_team")
                hml = rec.get("home_ml")
                aml = rec.get("away_ml")
                if _is_home and hml:
                    c["moneyline"] = str(hml)
                elif not _is_home and aml:
                    c["moneyline"] = str(aml)
        all_calls.extend(calls)
    return all_calls


# ---------------------------------------------------------------------------
# Mode: Config A/B comparison
# ---------------------------------------------------------------------------

def run_config_ab(records, args):
    """Compare two configs side by side."""
    base_config = _load_config()

    # Config A
    config_a = copy.deepcopy(base_config)
    threshold_a = args.threshold_a or args.threshold or 65
    if args.margin_gate_a is not None:
        config_a["pw_margin_gate"] = args.margin_gate_a
    if args.edge_gate_a is not None:
        config_a["pw_edge_gate"] = args.edge_gate_a

    # Config B
    config_b = copy.deepcopy(base_config)
    threshold_b = args.threshold_b or args.threshold or 65
    if args.margin_gate_b is not None:
        config_b["pw_margin_gate"] = args.margin_gate_b
    if args.edge_gate_b is not None:
        config_b["pw_edge_gate"] = args.edge_gate_b
    elif args.edge_gate is not None:
        config_b["pw_edge_gate"] = args.edge_gate

    cooldown = args.cooldown or 5

    # Describe configs
    desc_a = f"threshold={threshold_a}"
    desc_b = f"threshold={threshold_b}"
    if args.margin_gate_a is not None or args.margin_gate_b is not None:
        desc_a += f", margin_gate={config_a.get('pw_margin_gate', -12)}"
        desc_b += f", margin_gate={config_b.get('pw_margin_gate', -12)}"
    eg_a = config_a.get("pw_edge_gate")
    eg_b = config_b.get("pw_edge_gate")
    if eg_a is not None or eg_b is not None:
        desc_a += f", edge_gate={eg_a}"
        desc_b += f", edge_gate={eg_b}"

    print(f"\nConfig A: {desc_a}")
    print(f"Config B: {desc_b}")
    print(f"Games: {len(records)}")
    print()

    calls_a = generate_calls(records, config_a, threshold=threshold_a, cooldown=cooldown)
    calls_b = generate_calls(records, config_b, threshold=threshold_b, cooldown=cooldown)

    a = analyze_calls(calls_a, f"Config A ({desc_a})")
    b = analyze_calls(calls_b, f"Config B ({desc_b})")

    print_comparison(a, b)
    print()
    print_analysis(a)
    print()
    print_analysis(b)


# ---------------------------------------------------------------------------
# Mode: Compare stored calls vs freshly generated
# ---------------------------------------------------------------------------

def run_vs_stored(records, args):
    """Compare stored PW calls in game_history vs freshly generated."""
    config = _load_config()
    threshold = args.threshold or 65
    cooldown = args.cooldown or 5

    stored_calls = []
    for rec in records:
        pw = rec.get("predicted_winner_calls", [])
        winner = rec.get("winner")
        for c in pw:
            if c.get("correct") is None and winner:
                c["correct"] = c.get("predicted_team") == winner
        stored_calls.extend(pw)
    stored_calls = _apply_call_filters(stored_calls, args)

    fresh_calls = generate_calls(records, config, threshold=threshold, cooldown=cooldown)
    fresh_calls = _filter_calls_by_minute(fresh_calls, args.min_minute, args.max_minute)
    fresh_calls = _filter_calls_by_margin(fresh_calls, args.min_margin, args.max_margin)

    src_label = f" [{args.source}]" if args.source else ""
    a = analyze_calls(stored_calls, f"Stored calls{src_label} (game_history)")
    b = analyze_calls(fresh_calls, f"Fresh backfill (threshold={threshold})")

    print(f"\nGames: {len(records)}")
    print(f"Stored calls: {len(stored_calls)}, Fresh calls: {len(fresh_calls)}")
    print()
    print_comparison(a, b)
    print()
    print_analysis(a)
    print()
    print_analysis(b)


# ---------------------------------------------------------------------------
# Mode: Filter evaluation
# ---------------------------------------------------------------------------

def _filter_margin_gate(calls, gate=-12, late_gate=-8, late_min=60):
    """Suppress calls where predicted team is trailing beyond gate."""
    return [c for c in calls
            if (c.get("score_margin_at_fire") or 0) >= (late_gate if (c.get("game_minute") or 0) >= late_min else gate)]


def _filter_edge_gate(calls, min_edge=0):
    """Suppress calls with avg_edge below threshold."""
    return [c for c in calls if (c.get("avg_edge") or 0) >= min_edge]


def _filter_polarity_gate(calls, min_net=-2):
    """Suppress calls where net polarity (positive - negative conditions) is too low."""
    return [c for c in calls if (c.get("polarity_net") or 0) >= min_net]


def _filter_polarity_strict(calls):
    """Suppress calls with any net negative polarity."""
    return _filter_polarity_gate(calls, min_net=0)


def _filter_consensus_conflicted(calls):
    """Suppress conflicted consensus calls above 80%."""
    return [c for c in calls
            if not (c.get("consensus") == "conflicted" and (c.get("pct") or c.get("winner_score_pct") or 0) >= 80)]


def _filter_high_confidence(calls, max_pct=98):
    """Suppress unrealistically high confidence calls."""
    return [c for c in calls
            if (c.get("pct") or c.get("winner_score_pct") or 0) <= max_pct]


def _filter_trailing_only(calls):
    """Keep only trailing calls (the hardest to get right)."""
    return [c for c in calls if (c.get("score_margin_at_fire") or 0) < 0]


def evaluate_filter(all_calls, filtered_calls, label):
    """Evaluate a filter's impact vs baseline."""
    baseline_decided = [c for c in all_calls if c.get("correct") is not None]
    filtered_decided = [c for c in filtered_calls if c.get("correct") is not None]

    base_total = len(baseline_decided)
    filt_total = len(filtered_decided)
    suppressed = base_total - filt_total

    base_correct = sum(1 for c in baseline_decided if c["correct"])
    filt_correct = sum(1 for c in filtered_decided if c["correct"])

    base_wrong = base_total - base_correct
    filt_wrong = filt_total - filt_correct

    wrong_removed = base_wrong - filt_wrong
    correct_lost = base_correct - filt_correct
    net_improvement = wrong_removed - correct_lost

    base_acc = (base_correct / base_total * 100) if base_total else 0
    filt_acc = (filt_correct / filt_total * 100) if filt_total else 0

    return {
        "label": label,
        "base_total": base_total,
        "filt_total": filt_total,
        "suppressed": suppressed,
        "base_acc": base_acc,
        "filt_acc": filt_acc,
        "wrong_removed": wrong_removed,
        "correct_lost": correct_lost,
        "net_improvement": net_improvement,
        "precision": (wrong_removed / suppressed * 100) if suppressed else 0,
    }


def run_evaluate_filters(records, args):
    """Test various suppression filters against stored or generated calls."""
    if args.generate_fresh:
        config = _load_config()
        threshold = args.threshold or 65
        cooldown = args.cooldown or 5
        all_calls = generate_calls(records, config, threshold=threshold, cooldown=cooldown)
        all_calls = _filter_calls_by_minute(all_calls, args.min_minute, args.max_minute)
        all_calls = _filter_calls_by_margin(all_calls, args.min_margin, args.max_margin)
        source = f"freshly generated (threshold={threshold})"
    else:
        all_calls = []
        for rec in records:
            pw = rec.get("predicted_winner_calls", [])
            winner = rec.get("winner")
            for c in pw:
                if c.get("correct") is None and winner:
                    c["correct"] = c.get("predicted_team") == winner
            all_calls.extend(pw)
        all_calls = _apply_call_filters(all_calls, args)
        src_label = f" [{args.source}]" if args.source else ""
        source = f"stored calls{src_label} (game_history)"

    decided = [c for c in all_calls if c.get("correct") is not None]
    correct = sum(1 for c in decided if c["correct"])
    print(f"\nSource: {source}")
    print(f"Games: {len(records)}, Total calls: {len(decided)}, "
          f"Correct: {correct}, Accuracy: {correct / len(decided) * 100:.1f}%\n" if decided else "\n")

    # Individual filters
    filters = [
        ("A. Margin gate (-12/-8)", _filter_margin_gate(decided)),
        ("B. Edge gate (>=0)", _filter_edge_gate(decided, 0)),
        ("C. Edge gate (>=1)", _filter_edge_gate(decided, 1)),
        ("D. Polarity gate (>=-2)", _filter_polarity_gate(decided)),
        ("E. Polarity strict (>=0)", _filter_polarity_strict(decided)),
        ("F. No conflicted >=80%", _filter_consensus_conflicted(decided)),
        ("G. Cap confidence <=98%", _filter_high_confidence(decided)),
    ]

    # Combined filters
    ab = _filter_edge_gate(_filter_margin_gate(decided), 0)
    abd = _filter_polarity_gate(ab)
    abdf = _filter_consensus_conflicted(abd)
    ace = _filter_polarity_strict(_filter_edge_gate(_filter_margin_gate(decided), 1))

    combos = [
        ("A+B (margin + edge>=0)", ab),
        ("A+B+D (+ polarity>=-2)", abd),
        ("A+B+D+F (+ no conflicted)", abdf),
        ("A+C+E (margin + edge>=1 + strict pol)", ace),
    ]

    print(f"{'Filter':<40s} {'Kept':>5s} {'Supp':>5s} {'Acc%':>6s} "
          f"{'Wrong-':>6s} {'Corr-':>6s} {'Net':>5s} {'Prec%':>6s}")
    print("-" * 80)

    for label, filtered in filters + combos:
        r = evaluate_filter(decided, filtered, label)
        print(f"  {r['label']:<38s} {r['filt_total']:>5d} {r['suppressed']:>5d} "
              f"{r['filt_acc']:>5.1f}% {r['wrong_removed']:>5d}  {r['correct_lost']:>5d}  "
              f"{r['net_improvement']:>+4d}  {r['precision']:>5.1f}%")

    # Confidence calibration
    print(f"\nConfidence calibration:")
    print(f"  {'Band':>8s} {'Calls':>6s} {'Correct':>8s} {'Actual%':>8s} {'Expected':>9s} {'Status':>12s}")
    print("  " + "-" * 55)
    for lo in range(50, 100, 5):
        hi = lo + 5
        band = [c for c in decided if lo <= (c.get("pct") or c.get("winner_score_pct") or 0) < hi]
        if not band:
            continue
        band_correct = sum(1 for c in band if c["correct"])
        actual = band_correct / len(band) * 100
        expected = (lo + hi) / 2
        diff = actual - expected
        status = "calibrated" if abs(diff) < 5 else ("OVER-confident" if diff < -5 else "under-confident")
        print(f"  {lo}-{hi}%  {len(band):>6d} {band_correct:>8d} {actual:>7.1f}% {expected:>8.1f}%  {status:>12s}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="PW config comparison and filter evaluation tool (#116)")

    # Date/season filters
    parser.add_argument("--since", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", help="End date (YYYY-MM-DD)")
    parser.add_argument("--season", help="Season year (e.g. 2026)")

    # Mode selection
    parser.add_argument("--evaluate-filters", action="store_true",
                        help="Run filter evaluation on stored/generated calls")
    parser.add_argument("--vs-stored", action="store_true",
                        help="Compare stored calls vs freshly generated")
    parser.add_argument("--generate-fresh", action="store_true",
                        help="Use freshly generated calls (not stored) for filter eval")

    # Config A/B params
    parser.add_argument("--threshold", type=float, help="PW confidence threshold (both configs)")
    parser.add_argument("--threshold-a", type=float, help="Threshold for Config A")
    parser.add_argument("--threshold-b", type=float, help="Threshold for Config B")
    parser.add_argument("--edge-gate", type=float, help="Edge gate for Config B (A uses current config)")
    parser.add_argument("--edge-gate-a", type=float, help="Edge gate for Config A")
    parser.add_argument("--edge-gate-b", type=float, help="Edge gate for Config B")
    parser.add_argument("--margin-gate-a", type=float, help="Margin gate for Config A")
    parser.add_argument("--margin-gate-b", type=float, help="Margin gate for Config B")
    parser.add_argument("--cooldown", type=int, help="Cooldown minutes (default 5)")

    # Call filters
    parser.add_argument("--source", choices=["live", "backfill"],
                        help="Filter stored calls by source (live or backfill)")
    parser.add_argument("--min-minute", type=int, default=0,
                        help="Exclude calls before this game minute (default 0)")
    parser.add_argument("--max-minute", type=int, default=999,
                        help="Exclude calls after this game minute (default unlimited)")
    parser.add_argument("--min-margin", type=int,
                        help="Exclude calls where abs(margin) > this value (filter out blowouts)")
    parser.add_argument("--max-margin", type=int,
                        help="Exclude calls where abs(margin) < this value (filter out close games)")

    # Output
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--quiet", action="store_true", help="Suppress detailed output")

    args = parser.parse_args()

    # Load history
    records = outcomes_mod.load_history()
    records = _date_filter(records, since=args.since, until=args.until, season=args.season)
    # Sort chronologically
    records.sort(key=lambda r: r.get("kickoff_utc") or r.get("date") or r.get("ts") or "")

    if not records:
        print("No games found matching filters.")
        sys.exit(1)

    date_range = ""
    first_dt = (records[0].get("kickoff_utc") or records[0].get("date") or "")[:10]
    last_dt = (records[-1].get("kickoff_utc") or records[-1].get("date") or "")[:10]
    if first_dt:
        date_range = f" ({first_dt} to {last_dt})"
    print(f"Games: {len(records)}{date_range}")
    filters = []
    if args.min_minute > 0:
        filters.append(f"min_minute={args.min_minute}")
    if args.max_minute < 999:
        filters.append(f"max_minute={args.max_minute}")
    if args.min_margin is not None:
        filters.append(f"margin<={args.min_margin}")
    if args.max_margin is not None:
        filters.append(f"margin>={args.max_margin}")
    if filters:
        print(f"Call filters: {', '.join(filters)}")

    # Dispatch
    if args.evaluate_filters:
        run_evaluate_filters(records, args)
    elif args.vs_stored:
        run_vs_stored(records, args)
    else:
        run_config_ab(records, args)


if __name__ == "__main__":
    main()
