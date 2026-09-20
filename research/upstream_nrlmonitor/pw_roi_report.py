#!/usr/bin/env python3
"""Generate cross-league PW ROI report from current game history data.

Reads NBA, WNBA, and NRL game_history files and produces pw_roi_report.html
with current statistics. Can be run on-demand or scheduled.

Usage:
    python3 pw_roi_report.py                  # Generate report
    python3 pw_roi_report.py --output /path   # Custom output path
    python3 pw_roi_report.py --copy-to-nba    # Also copy to NBA repo
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NBA_DIR = os.path.join(os.path.expanduser("~"), ".openclaw/workspace/scripts/nba-monitor")

HISTORY_FILES = {
    "NBA": os.path.join(NBA_DIR, "game_history_nba.json"),
    "WNBA": os.path.join(NBA_DIR, "game_history_wnba.json"),
    "NRL": os.path.join(SCRIPT_DIR, "game_history.json"),
}

OUTPUT_FILE = os.path.join(SCRIPT_DIR, "pw_roi_report.html")
NBA_OUTPUT = os.path.join(NBA_DIR, "pw_roi_report.html")


def _load_history(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _extract_calls(history, league):
    """Extract all PW calls with enriched fields."""
    calls = []
    for r in history:
        pw = r.get("predicted_winner_calls", [])
        winner = r.get("winner_id") or r.get("winner")
        gid = str(r.get("game_id") or r.get("match_id") or id(r))
        # Compute final margin from predicted team's perspective (#154)
        home_score = r.get("home_score")
        away_score = r.get("away_score")
        home_team = r.get("home_team") or r.get("home_name") or ""
        home_id = str(r.get("home_id") or r.get("home_team") or "")
        away_id = str(r.get("away_id") or r.get("away_team") or "")
        spread_fav_id = str(r.get("spread_fav_id") or "")
        # Per-team pregame odds from game record (#157)
        home_spread = r.get("home_spread")
        away_spread = r.get("away_spread")
        home_ml = r.get("home_moneyline") or r.get("home_ml")
        away_ml = r.get("away_moneyline") or r.get("away_ml")
        # Season/segment from game record (#190)
        # NRL: stored as r["season"] (calendar year, e.g. "2026")
        # NBA/WNBA: not stored — derive from date using same logic as server.py _game_season_year()
        _season = ""
        if league == "NRL":
            _season = str(r.get("season") or "")
        if not _season and r.get("date"):
            try:
                from datetime import datetime as _dt_cls, timedelta as _td_cls
                _dt_str = str(r["date"]).replace("Z", "+00:00")
                _dt_obj = _dt_cls.fromisoformat(_dt_str)
                _us_date = (_dt_obj - _td_cls(hours=8)).date()
                if league == "WNBA":
                    # WNBA: May-Oct single year, e.g. "2025"
                    _start_mo = 5
                    _y = _us_date.year if _us_date.month >= _start_mo else _us_date.year - 1
                    _season = str(_y)
                else:
                    # NBA: Oct-Jun cross-year, e.g. "2025-26"
                    _start_mo = 10
                    _y = _us_date.year if _us_date.month >= _start_mo else _us_date.year - 1
                    _season = "{}-{}".format(_y, str(_y + 1)[-2:])
            except Exception:
                pass
        _segment = r.get("season_segment") or ""
        for c in pw:
            if c.get("correct") is None and winner:
                pred = c.get("predicted_team_id") or c.get("predicted_team")
                c["correct"] = pred == winner
            c["_source"] = c.get("source", "backfill" if c.get("synthetic") else "live")
            c["_league"] = league
            c["_game_id"] = gid
            c["_season"] = _season
            c["_segment"] = _segment
            # Final margin from predicted team's POV
            pred_team = c.get("predicted_team") or c.get("predicted_team_id") or ""
            pred_id = str(c.get("predicted_team_id") or pred_team)
            if home_score is not None and away_score is not None and pred_team:
                try:
                    hs, as_ = int(home_score), int(away_score)
                    c["_final_margin"] = (hs - as_) if pred_team == home_team else (as_ - hs)
                except (ValueError, TypeError):
                    pass
            # Derive spread_role from game record when missing (#157)
            if not c.get("spread_role") and spread_fav_id and pred_id:
                c["spread_role"] = "favorite" if pred_id == spread_fav_id else "Underdog"
            # Derive pregame spread/moneyline from game record when missing (#157)
            if not c.get("spread") and pred_id:
                if pred_id == home_id and home_spread is not None:
                    c["spread"] = str(home_spread)
                elif pred_id == away_id and away_spread is not None:
                    c["spread"] = str(away_spread)
            if not c.get("moneyline") and pred_id:
                if pred_id == home_id and home_ml is not None:
                    c["moneyline"] = str(home_ml)
                elif pred_id == away_id and away_ml is not None:
                    c["moneyline"] = str(away_ml)
            calls.append(c)
    return [c for c in calls if c.get("correct") is not None and not c.get("_suppressed")]


def _ml_roi(odds_str, correct, league):
    """Compute ML ROI from odds string."""
    if not odds_str:
        return None
    try:
        odds = float(odds_str)
        if league == "NRL":
            if odds <= 1:
                return None
            return (odds - 1) if correct else -1.0
        else:
            if odds > 0:
                return (odds / 100) if correct else -1.0
            elif odds < 0:
                return (100 / abs(odds)) if correct else -1.0
        return None
    except (TypeError, ValueError):
        return None


def _analyze(calls, league):
    """Compute all metrics for a set of calls."""
    n = len(calls)
    if n == 0:
        return None
    correct = sum(1 for c in calls if c["correct"])
    acc = correct / n * 100
    games = len(set(c.get("_game_id", "") for c in calls if c.get("_game_id")))

    flat = sum(1 if c["correct"] else -1 for c in calls)
    flat_roi = flat / n * 100

    ml_pnl = 0.0
    ml_n = 0
    for c in calls:
        ml = c.get("moneyline") or c.get("live_moneyline")
        roi = _ml_roi(ml, c["correct"], league)
        if roi is not None:
            ml_pnl += roi
            ml_n += 1

    # Spread cover checks use FINAL margin, not margin at fire (#154)
    sp_yes = sp_no = 0
    for c in calls:
        sp = c.get("spread")
        fm = c.get("_final_margin")
        if sp is not None and fm is not None:
            try:
                if float(fm) + float(sp) > 0:
                    sp_yes += 1
                else:
                    sp_no += 1
            except (TypeError, ValueError):
                pass

    ls_yes = ls_no = 0
    for c in calls:
        ls = c.get("live_spread")
        fm = c.get("_final_margin")
        if ls is not None and fm is not None:
            try:
                if float(fm) + float(ls) > 0:
                    ls_yes += 1
                else:
                    ls_no += 1
            except (TypeError, ValueError):
                pass

    bks_yes = bks_no = 0
    for c in calls:
        bks = c.get("bk_spread")
        fm = c.get("_final_margin")
        if bks is not None and fm is not None:
            try:
                if float(fm) + float(bks) > 0:
                    bks_yes += 1
                else:
                    bks_no += 1
            except (TypeError, ValueError):
                pass

    bko_pnl = 0.0
    bko_n = 0
    for c in calls:
        bkml = c.get("bk_moneyline")
        roi = _ml_roi(bkml, c["correct"], league)
        if roi is not None:
            bko_pnl += roi
            bko_n += 1

    sp_total = sp_yes + sp_no
    ls_total = ls_yes + ls_no
    bks_total = bks_yes + bks_no

    # Spread ROI uses -110 juice: win $90.91, lose $100 per unit (#157)
    _SPREAD_WIN = 90.91
    _SPREAD_LOSE = 100.0
    def _spread_roi(yes, no, total):
        if total == 0:
            return 0
        pnl = yes * _SPREAD_WIN - no * _SPREAD_LOSE
        return round(pnl / (total * _SPREAD_LOSE) * 100, 1)

    return {
        "n": n, "games": games, "correct": correct, "acc": round(acc, 1),
        "flat": flat, "flat_roi": round(flat_roi, 1),
        "ml_n": ml_n, "ml_roi": round(ml_pnl / ml_n * 100, 1) if ml_n else 0,
        "sp_total": sp_total, "sp_pct": round(sp_yes / sp_total * 100, 1) if sp_total else 0,
        "sp_roi": _spread_roi(sp_yes, sp_no, sp_total),
        "ls_total": ls_total, "ls_pct": round(ls_yes / ls_total * 100, 1) if ls_total else 0,
        "bks_total": bks_total, "bks_pct": round(bks_yes / bks_total * 100, 1) if bks_total else 0,
        "bks_roi": _spread_roi(bks_yes, bks_no, bks_total),
        "bko_n": bko_n, "bko_roi": round(bko_pnl / bko_n * 100, 1) if bko_n else 0,
    }


def _segment(calls, key_fn):
    """Group calls by a key function and analyze each group."""
    groups = {}
    for c in calls:
        k = key_fn(c)
        if k is None:
            continue
        groups.setdefault(k, []).append(c)
    return groups


def generate_report(output_path=None, copy_to_nba=False):
    """Generate the HTML report from current data."""
    output_path = output_path or OUTPUT_FILE
    _now_utc = datetime.now(timezone.utc)
    _now_local = datetime.now()
    now = _now_local.strftime("%Y-%m-%d %H:%M") + " (" + _now_utc.strftime("%Y-%m-%d %H:%M UTC") + ")"

    # Load all leagues
    data = {}
    for league, path in HISTORY_FILES.items():
        history = _load_history(path)
        calls = _extract_calls(history, league)
        data[league] = {
            "calls": calls,
            "games": len(history),
            "overall": _analyze(calls, league),
        }

        # By source
        data[league]["by_source"] = {}
        for src in ["live", "backfill"]:
            sc = [c for c in calls if c["_source"] == src]
            if sc:
                data[league]["by_source"][src] = _analyze(sc, league)

        # By role
        data[league]["by_role"] = {}
        for role in ["favorite", "underdog"]:
            rc = [c for c in calls if c.get("spread_role") == role]
            if rc:
                data[league]["by_role"][role] = _analyze(rc, league)

        # By quarter
        quarters = ["Q1", "Q2", "Q3", "Q4", "OT"] if league != "NRL" else ["Q1", "Q2", "Q3", "Q4", "ET"]
        data[league]["by_quarter"] = {}
        for q in quarters:
            qc = [c for c in calls if c.get("quarter") == q]
            if qc:
                data[league]["by_quarter"][q] = _analyze(qc, league)

        # By consensus
        data[league]["by_consensus"] = {}
        for con in ["strong", "conflicted", "ml_only", "historical_only"]:
            cc = [c for c in calls if c.get("consensus") == con]
            if cc:
                data[league]["by_consensus"][con] = _analyze(cc, league)

        # By margin bucket
        data[league]["by_margin"] = {}
        for lo, hi, lbl in [(0, 6, "0-6"), (7, 12, "7-12"), (13, 20, "13-20"), (21, 999, "21+")]:
            mc = [c for c in calls if lo <= abs(c.get("score_margin_at_fire") or 0) <= hi]
            if mc:
                data[league]["by_margin"][lbl] = _analyze(mc, league)

        # By edge bucket
        data[league]["by_edge"] = {}
        for lo, hi, lbl in [(-999, -0.01, "<0"), (0, 4.99, "0-5"), (5, 9.99, "5-10"), (10, 999, "10+")]:
            ec = [c for c in calls if lo <= (c.get("avg_edge") or 0) <= hi]
            if ec:
                data[league]["by_edge"][lbl] = _analyze(ec, league)

        # Leading vs trailing
        data[league]["by_position"] = {}
        lead = [c for c in calls if (c.get("score_margin_at_fire") or 0) >= 0]
        trail = [c for c in calls if (c.get("score_margin_at_fire") or 0) < 0]
        if lead:
            data[league]["by_position"]["leading"] = _analyze(lead, league)
        if trail:
            data[league]["by_position"]["trailing"] = _analyze(trail, league)

        # Confidence calibration
        data[league]["calibration"] = {}
        for lo in range(50, 100, 5):
            hi = lo + 5
            bc = [c for c in calls if lo <= (c.get("pct") or c.get("winner_score_pct") or 0) < hi]
            if bc:
                cor = sum(1 for c in bc if c["correct"])
                actual = round(cor / len(bc) * 100, 1)
                expected = (lo + hi) / 2
                data[league]["calibration"][f"{lo}-{hi}%"] = {
                    "n": len(bc), "correct": cor, "actual": actual, "expected": expected,
                }

        # ── BK 4-way scenarios (#150) ─────────────────────────────────────
        BK_MIN_SAMPLE = 1  # client-side dropdown handles min filter

        def _apply_call_sel(call_list, sel):
            """Apply call selection filter per game."""
            by_game = {}
            for c in call_list:
                by_game.setdefault(c.get("_game_id", ""), []).append(c)
            result = []
            for g_calls in by_game.values():
                s = sorted(g_calls, key=lambda x: x.get("ts") or "")
                if sel == "first":
                    result.append(s[0])
                elif sel == "last":
                    result.append(s[-1])
                elif sel == "1st/qtr":
                    by_q = {}
                    for c in s:
                        q = c.get("quarter") or c.get("half") or ""
                        if q not in by_q:
                            by_q[q] = c
                    result.extend(by_q.values())
                elif sel == "1st/half":
                    by_h = {}
                    for c in s:
                        h = c.get("half") or ""
                        if h not in by_h:
                            by_h[h] = c
                    result.extend(by_h.values())
            return result

        bk_calls = [c for c in calls if c.get("bk_moneyline") or c.get("bk_spread")]
        _roles = ["favorite", "underdog"]
        _margins = ["leading", "trailing"]
        _periods = ["Q1", "Q2", "Q3", "Q4", "ET"] if league == "NRL" else ["Q1", "Q2", "Q3", "Q4", "OT"]
        _call_sels = ["first", "last", "1st/qtr", "1st/half"]
        _edge_ranges = ["10+", "5-10", "0-5", "<0"]

        def _edge_range_key(c):
            e = c.get("avg_edge")
            if e is None:
                return ""
            try:
                e = float(e)
            except (TypeError, ValueError):
                return ""
            if e >= 10:
                return "10+"
            if e >= 5:
                return "5-10"
            if e >= 0:
                return "0-5"
            return "<0"

        bk_scenarios = []
        for role in _roles:
            for margin in ["all"] + _margins:
                for edge in ["all"] + _edge_ranges:
                    for period in _periods:
                        # Build subset: role + margin + edge + period
                        subset = []
                        for c in bk_calls:
                            if (c.get("spread_role") or "").lower() != role:
                                continue
                            if (c.get("quarter") or "") != period:
                                continue
                            if margin != "all":
                                c_margin = "leading" if (c.get("score_margin_at_fire") or 0) >= 0 else "trailing"
                                if c_margin != margin:
                                    continue
                            if edge != "all":
                                if _edge_range_key(c) != edge:
                                    continue
                            subset.append(c)
                        if not subset:
                            continue
                        for csel in ["all"] + _call_sels:
                            filtered = subset if csel == "all" else _apply_call_sel(subset, csel)
                            if len(filtered) < BK_MIN_SAMPLE:
                                continue
                            a = _analyze(filtered, league)
                            if not a:
                                continue
                            bk_scenarios.append({
                                "role": role, "margin": margin, "edge": edge, "period": period, "call_sel": csel,
                                "label": f"{role} · {margin} · {edge} · {period} · {csel}",
                                "n": a["n"], "correct": a.get("correct", 0), "games": a.get("games", 0), "acc": a["acc"],
                                "bko_roi": a.get("bko_roi", 0), "bko_n": a.get("bko_n", 0),
                                "bks_roi": a.get("bks_roi", 0), "bks_total": a.get("bks_total", 0),
                            })
        data[league]["bk_scenarios"] = bk_scenarios
        data[league]["bk_coverage"] = {
            "total_calls": len(calls),
            "bk_calls": len(bk_calls),
            "pct": round(len(bk_calls) / len(calls) * 100, 1) if calls else 0,
        }

    # ── Statistical helpers (#147) ──────────────────────────────────────
    import math

    def _wilson_ci(wins, total, z=1.96):
        """Wilson score 95% confidence interval for a proportion."""
        if total == 0:
            return (0, 0)
        p = wins / total
        denom = 1 + z * z / total
        centre = (p + z * z / (2 * total)) / denom
        margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
        return (max(0, centre - margin), min(1, centre + margin))

    def _roi_ci(pnl, n, z=1.96):
        """Bootstrap-approximated 95% CI for ROI % using normal approx."""
        if n < 2:
            return None
        roi = pnl / n * 100
        # Approximate std dev: each bet is +profit or -100, assume variance ~ STAKE^2
        se = 100.0 / math.sqrt(n)  # simplified SE
        return (roi - z * se, roi + z * se)

    def _confidence_label(n):
        if n >= 500:
            return "high"
        if n >= 100:
            return "medium"
        if n >= 30:
            return "low"
        return "very low"

    def _conf_badge(n):
        lbl = _confidence_label(n)
        colors = {"high": "pill-green", "medium": "pill-yellow", "low": "pill-red", "very low": "pill-red"}
        return f'<span class="pill {colors.get(lbl, "")}" style="font-size:0.7rem">{lbl} (N={n:,})</span>'

    # Min sample thresholds per insight type
    MIN_ROI_CALLS = 50
    MIN_ACC_CALLS = 30
    MIN_BK_CALLS = 20
    MIN_CAL_CALLS = 10

    # ── Generate dynamic insights and recommendations from data (#147) ──
    def _generate_insights(data):
        insights = []
        recs_immediate = []
        recs_strategic = []
        recs_research = []

        for league in ["NBA", "WNBA", "NRL"]:
            ld = data.get(league, {})
            o = ld.get("overall")
            if not o:
                continue

            # Role analysis (with CI)
            roles = ld.get("by_role", {})
            fav = roles.get("favorite")
            und = roles.get("underdog")
            if fav and und and und.get("ml_n", 0) >= MIN_ROI_CALLS and fav.get("ml_n", 0) >= MIN_ROI_CALLS:
                if und["ml_roi"] > fav.get("ml_roi", 0) + 10:
                    ci = _roi_ci(und["ml_roi"] * und["ml_n"] / 100, und["ml_n"])
                    ci_str = f' (95% CI: {ci[0]:+.0f}% to {ci[1]:+.0f}%)' if ci else ''
                    insights.append(
                        f'<strong>{league} Underdog Edge:</strong> {und["acc"]:.0f}% accuracy but '
                        f'<span class="pos">{und["ml_roi"]:+.1f}% ML ROI</span>{ci_str} — odds overcompensate. '
                        f'Favorites: {fav["acc"]:.0f}% acc, {fav.get("ml_roi",0):+.1f}% ML ROI. {_conf_badge(und["ml_n"])}')
                    recs_strategic.append(f'<strong>{league}:</strong> Focus underdog calls ({und["ml_roi"]:+.1f}% ML ROI, {und["ml_n"]:,} calls)')

            # ML ROI standout (with CI)
            if o.get("ml_n", 0) >= MIN_ROI_CALLS and o["ml_roi"] > 50:
                ci = _roi_ci(o["ml_roi"] * o["ml_n"] / 100, o["ml_n"])
                ci_str = f' (95% CI: {ci[0]:+.0f}% to {ci[1]:+.0f}%)' if ci else ''
                insights.append(
                    f'<strong>{league} ML is exceptional:</strong> <span class="pos">{o["ml_roi"]:+.1f}% ML ROI</span>{ci_str} '
                    f'overall ({o["ml_n"]:,} calls). {_conf_badge(o["ml_n"])}')
                recs_strategic.append(f'<strong>{league}:</strong> Increase ML blend weight ({o["ml_roi"]:+.1f}% ROI)')

            # Spread edge
            live = ld.get("by_source", {}).get("live")
            if live and live.get("sp_total", 0) >= MIN_ROI_CALLS and live["sp_roi"] > 20:
                bko = live.get("bko_roi", 0)
                insights.append(
                    f'<strong>{league} edge is pregame spreads:</strong> <span class="pos">{live["sp_pct"]:.1f}% coverage, '
                    f'{live["sp_roi"]:+.1f}% ROI</span>. BK odds ROI is '
                    f'<span class="neg">{bko:+.1f}%</span>. {_conf_badge(live["sp_total"])}')
                recs_strategic.append(f'<strong>{league}:</strong> Shift to spread analysis ({live["sp_roi"]:+.1f}% ROI)')

            # Consensus analysis (with CI)
            cons = ld.get("by_consensus", {})
            conf = cons.get("conflicted")
            if conf and conf.get("n", 0) >= MIN_ACC_CALLS and conf["flat_roi"] < -5:
                lo, hi = _wilson_ci(conf["correct"], conf["n"])
                insights.append(
                    f'<strong>{league} conflicted consensus is poison:</strong> '
                    f'<span class="neg">{conf["flat_roi"]:+.1f}%</span> flat ROI '
                    f'({conf["acc"]:.1f}% accuracy, 95% CI: {lo*100:.0f}-{hi*100:.0f}%, {conf["n"]} calls). {_conf_badge(conf["n"])}')

            # Q1 weakness — with what-if backtest
            q1 = ld.get("by_quarter", {}).get("Q1")
            if q1 and q1.get("n", 0) >= MIN_ACC_CALLS and q1.get("ml_n", 0) >= MIN_ACC_CALLS and q1["ml_roi"] < -20:
                # What-if: ROI without Q1
                non_q1_n = o.get("n", 0) - q1["n"]
                non_q1_flat = o.get("flat", 0) - q1.get("flat", 0)
                whatif_roi = round(non_q1_flat / non_q1_n * 100, 1) if non_q1_n > 0 else 0
                insights.append(
                    f'<strong>{league} Q1 destroys value:</strong> '
                    f'<span class="neg">{q1["ml_roi"]:+.1f}% ML ROI</span> ({q1["n"]} calls). {_conf_badge(q1["n"])}')
                recs_immediate.append(
                    f'Suppress <strong>{league} Q1</strong> ({q1["ml_roi"]:+.1f}% ML ROI). '
                    f'What-if: overall flat ROI would be {whatif_roi:+.1f}% (vs {o.get("flat_roi",0):+.1f}% current)')

            # Edge <0 — with what-if backtest
            edge_neg = ld.get("by_edge", {}).get("<0")
            if edge_neg and edge_neg.get("n", 0) >= MIN_ACC_CALLS:
                non_neg_n = o.get("n", 0) - edge_neg["n"]
                non_neg_flat = o.get("flat", 0) - edge_neg.get("flat", 0)
                whatif_roi = round(non_neg_flat / non_neg_n * 100, 1) if non_neg_n > 0 else 0
                recs_immediate.append(
                    f'Keep <strong>{league} edge_gate=0</strong> — edge&lt;0 is {edge_neg.get("flat_roi",0):+.1f}% flat ROI ({edge_neg["n"]} calls). '
                    f'What-if without: {whatif_roi:+.1f}% flat ROI')
            elif edge_neg and edge_neg.get("n", 0) < MIN_ACC_CALLS and edge_neg.get("n", 0) > 0:
                recs_research.append(
                    f'{league} edge&lt;0 has only {edge_neg["n"]} calls — insufficient for reliable recommendation')

            # Calibration issues
            over_bands = []
            for band, cal in ld.get("calibration", {}).items():
                if cal["n"] >= MIN_CAL_CALLS and cal["actual"] < cal["expected"] - 10:
                    over_bands.append(f'{band} ({cal["actual"]:.0f}% vs {cal["expected"]:.0f}%, N={cal["n"]})')
            if len(over_bands) >= 2:
                recs_research.append(f'{league} confidence overconfident in {len(over_bands)} bands — needs recalibration: {", ".join(over_bands[:3])}')

            # BK odds/spread analysis
            if o.get("bko_n", 0) >= MIN_BK_CALLS and o["bko_roi"] < -10:
                recs_research.append(f'{league} BK odds ROI {o["bko_roi"]:+.1f}% ({o["bko_n"]} calls) — bookmakers too efficient')
            elif o.get("bko_n", 0) > 0 and o.get("bko_n", 0) < MIN_BK_CALLS:
                recs_research.append(f'{league} BK odds: only {o["bko_n"]} calls — need more data for reliable ROI')

            if o.get("bks_total", 0) >= MIN_BK_CALLS and o.get("bks_roi", 0) > 20:
                insights.append(
                    f'<strong>{league} BK Spread edge:</strong> <span class="pos">{o["bks_roi"]:+.1f}% ROI</span> '
                    f'({o["bks_pct"]:.1f}% cover, {o["bks_total"]} calls). {_conf_badge(o["bks_total"])}')
            elif o.get("bks_total", 0) >= MIN_BK_CALLS and o.get("bks_roi", 0) < -20:
                insights.append(
                    f'<strong>{league} BK Spread loss:</strong> <span class="neg">{o["bks_roi"]:+.1f}% ROI</span> '
                    f'({o["bks_total"]} calls). {_conf_badge(o["bks_total"])}')

            if o.get("bko_n", 0) >= MIN_BK_CALLS and o.get("bko_roi", 0) > 20:
                insights.append(
                    f'<strong>{league} BK Odds profitable:</strong> <span class="pos">{o["bko_roi"]:+.1f}% ROI</span> '
                    f'({o["bko_n"]} calls). {_conf_badge(o["bko_n"])}')

        # ── Cross-league comparisons ──
        leagues_with_ml = [(l, data[l]["overall"]) for l in ["NBA", "WNBA", "NRL"]
                           if data.get(l, {}).get("overall", {}).get("ml_n", 0) >= MIN_ROI_CALLS]
        if len(leagues_with_ml) >= 2:
            best = max(leagues_with_ml, key=lambda x: x[1]["ml_roi"])
            worst = min(leagues_with_ml, key=lambda x: x[1]["ml_roi"])
            if best[1]["ml_roi"] - worst[1]["ml_roi"] > 30:
                insights.append(
                    f'<strong>Cross-league ML ROI gap:</strong> {best[0]} '
                    f'<span class="pos">{best[1]["ml_roi"]:+.1f}%</span> vs {worst[0]} '
                    f'<span class="{"neg" if worst[1]["ml_roi"]<0 else ""}">{worst[1]["ml_roi"]:+.1f}%</span> '
                    f'— {abs(best[1]["ml_roi"] - worst[1]["ml_roi"]):.0f}pp difference suggests different market efficiency.')

        # ── Universal recs ──
        all_conf = [(l, data[l].get("by_consensus", {}).get("conflicted"))
                     for l in data if data[l].get("by_consensus", {}).get("conflicted", {}).get("n", 0) >= MIN_ACC_CALLS]
        if all_conf and all(c[1]["flat_roi"] < 0 for c in all_conf):
            # What-if: suppress conflicted across all leagues
            total_saved = sum(abs(c[1].get("flat", 0)) for c in all_conf)
            recs_immediate.insert(0,
                f'Suppress <strong>conflicted consensus</strong> — ROI-negative across {len(all_conf)} league(s). '
                f'What-if: saves ~{total_saved:.0f} units')

        # Min threshold rec (with what-if)
        for league in ["NBA", "WNBA", "NRL"]:
            cal65 = data.get(league, {}).get("calibration", {}).get("65-70%")
            if cal65 and cal65["n"] >= MIN_CAL_CALLS and cal65["actual"] < 50:
                # What-if: remove 65-70% band
                o = data[league].get("overall", {})
                new_n = o.get("n", 0) - cal65["n"]
                new_correct = o.get("correct", 0) - cal65["correct"]
                new_acc = round(new_correct / new_n * 100, 1) if new_n > 0 else 0
                recs_immediate.append(
                    f'Raise min threshold to <strong>70%</strong> — {league} 65-70% band at {cal65["actual"]:.0f}% actual '
                    f'({cal65["n"]} calls). What-if: accuracy improves to {new_acc:.1f}% (vs {o.get("acc",0):.1f}%)')
                break

        return insights, recs_immediate, recs_strategic, recs_research

    insights, recs_imm, recs_strat, recs_res = _generate_insights(data)

    def _insights_html():
        return '\n'.join(f'<div class="insight">{i}</div>' for i in insights) if insights else '<div class="insight">Insufficient data for insights.</div>'

    def _recs_list(items):
        if not items:
            return '<li>No recommendations — insufficient data</li>'
        return '\n'.join(f'<li>{r}</li>' for r in items)

    # Helper functions for template
    def _v(d, key, fmt=".1f", suffix="%"):
        if d is None:
            return "—"
        v = d.get(key, 0)
        if v == 0 and key.endswith("_roi") and d.get(key.replace("_roi", "_n"), 0) == 0:
            return "—"
        return format(v, fmt) + suffix

    def _cls(d, key):
        if d is None:
            return ""
        v = d.get(key, 0)
        return "pos" if v > 0 else ("neg" if v < 0 else "")

    def _roi_cell(d, key):
        if d is None:
            return '<td>—</td>'
        v = d.get(key, 0)
        n_key = key.replace("_roi", "_n")
        if v == 0 and d.get(n_key, 0) == 0:
            return '<td>—</td>'
        c = "pos" if v > 0 else ("neg" if v < 0 else "")
        return f'<td class="{c}">{v:+.1f}%</td>'

    def _pct_cell(d, key):
        if d is None:
            return '<td>—</td>'
        return f'<td>{d.get(key, 0):.1f}%</td>'

    def _n_cell(d, key="n"):
        if d is None:
            return '<td>—</td>'
        return f'<td>{d.get(key, 0):,}</td>'

    # ── BK section generators (#148) ─────────────────────────────────────
    def _bk_scenario_row(league_name, s):
        bko_c = "pos" if s["bko_roi"] > 0 else ("neg" if s["bko_roi"] < 0 else "")
        bks_c = "pos" if s["bks_roi"] > 0 else ("neg" if s["bks_roi"] < 0 else "")
        return (f'<tr><td>{league_name}</td><td>{s.get("role","")}</td><td>{s.get("margin","")}</td>'
                f'<td>{s.get("period","")}</td><td>{s.get("call_sel","")}</td>'
                f'<td class="{bko_c}" data-v="{s["bko_roi"]}">{s["bko_roi"]:+.1f}%</td><td>{s["bko_n"]}</td>'
                f'<td class="{bks_c}" data-v="{s["bks_roi"]}">{s["bks_roi"]:+.1f}%</td><td>{s["bks_total"]}</td>'
                f'<td>{s["acc"]:.1f}%</td><td>{s["n"]}</td><td>{s.get("games",0)}</td></tr>')

    # Build all scenario data as JSON for client-side sorting (#150)
    _all_bk_scenarios_json = []
    for league_name in ["NBA", "WNBA", "NRL"]:
        for s in data.get(league_name, {}).get("bk_scenarios", []):
            _all_bk_scenarios_json.append({
                "league": league_name,
                "role": s.get("role", ""), "margin": s.get("margin", ""), "edge": s.get("edge", "all"),
                "period": s.get("period", ""), "call_sel": s.get("call_sel", ""),
                "bko_roi": s.get("bko_roi", 0), "bko_n": s.get("bko_n", 0),
                "bks_roi": s.get("bks_roi", 0), "bks_total": s.get("bks_total", 0),
                "acc": s.get("acc", 0), "n": s.get("n", 0), "correct": s.get("correct", 0), "games": s.get("games", 0),
            })
    # Build compact raw calls array for client-side season/segment filtering (#190)
    _all_raw_calls = []
    for league_name in ["NBA", "WNBA", "NRL"]:
        for c in data.get(league_name, {}).get("calls", []):
            _all_raw_calls.append({
                "lg": league_name,
                "s": c.get("_season", ""),
                "sg": c.get("_segment", ""),
                "ok": 1 if c.get("correct") else 0,
                "gid": c.get("_game_id", ""),
                "role": (c.get("spread_role") or "").lower(),
                "ml": c.get("moneyline") or c.get("live_moneyline") or "",
                "bkml": c.get("bk_moneyline") or "",
                "sp": c.get("spread") or "",
                "bksp": c.get("bk_spread") or "",
                "lsp": c.get("live_spread") or "",
                "fm": c.get("_final_margin"),
                "mf": c.get("score_margin_at_fire"),
                "q": c.get("quarter") or "",
                "h": c.get("half") or "",
                "edge": c.get("avg_edge"),
                "src": c.get("_source", ""),
                "pct": c.get("pct") or c.get("winner_score_pct") or 0,
                "con": c.get("consensus") or "",
            })
    import json as _json
    _bk_scenarios_json_str = _json.dumps(_all_bk_scenarios_json)
    _raw_calls_json_str = _json.dumps(_all_raw_calls)

    # Per-league season/segment sets for JS-driven dropdowns (#190)
    _league_seasons = {}
    _league_segments = {}
    for league_name in ["NBA", "WNBA", "NRL"]:
        _league_seasons[league_name] = sorted(set(c.get("_season", "") for c in data.get(league_name, {}).get("calls", []) if c.get("_season")))
        _league_segments[league_name] = sorted(set(c.get("_segment", "") for c in data.get(league_name, {}).get("calls", []) if c.get("_segment")))
    _segment_labels = {"regular_season": "Regular Season", "finals": "Finals", "state_of_origin": "State of Origin",
                       "pre_allstar": "Pre All-Star", "post_allstar": "Post All-Star", "playoffs": "Playoffs", "playin": "Play-In"}
    import json as _json_mod
    _league_seasons_json = _json_mod.dumps(_league_seasons)
    _league_segments_json = _json_mod.dumps(_league_segments)
    _segment_labels_json = _json_mod.dumps(_segment_labels)

    def _season_options():
        # Populated dynamically by JS
        return ""

    def _segment_options():
        # Populated dynamically by JS
        return ""

    def _bk_coverage_rows():
        rows = ""
        for league_name in ["NBA", "WNBA", "NRL"]:
            cov = data.get(league_name, {}).get("bk_coverage", {})
            o = data.get(league_name, {}).get("overall", {})
            rows += f'<tr><td>{league_name}</td><td>{cov.get("total_calls",0):,}</td><td>{cov.get("bk_calls",0):,}</td><td>{cov.get("pct",0):.1f}%</td>'
            rows += _roi_cell(o, "bko_roi")
            rows += _roi_cell(o, "bks_roi")
            rows += '</tr>'
        return rows

    def _bk_recommendations():
        recs = []
        for league_name in ["NBA", "WNBA", "NRL"]:
            scenarios = data.get(league_name, {}).get("bk_scenarios", [])
            o = data.get(league_name, {}).get("overall", {})
            cov = data.get(league_name, {}).get("bk_coverage", {})

            # Best BK bet (4-way scenario)
            best_bko = sorted([s for s in scenarios if s.get("bko_n", 0) >= 15], key=lambda s: s["bko_roi"], reverse=True)
            if best_bko and best_bko[0]["bko_roi"] > 10:
                s = best_bko[0]
                recs.append(f'<strong>{league_name} best BK Odds:</strong> {s["role"]} · {s["margin"]} · {s.get("edge","all")} · {s["period"]} · {s["call_sel"]} → <span class="pos">{s["bko_roi"]:+.1f}% ROI</span> ({s["bko_n"]} calls, {s["games"]} games). {_conf_badge(s["bko_n"])}')

            best_bks = sorted([s for s in scenarios if s.get("bks_total", 0) >= 15], key=lambda s: s["bks_roi"], reverse=True)
            if best_bks and best_bks[0]["bks_roi"] > 10:
                s = best_bks[0]
                recs.append(f'<strong>{league_name} best BK Spread:</strong> {s["role"]} · {s["margin"]} · {s.get("edge","all")} · {s["period"]} · {s["call_sel"]} → <span class="pos">{s["bks_roi"]:+.1f}% ROI</span> ({s["bks_total"]} calls, {s["games"]} games). {_conf_badge(s["bks_total"])}')

            # Worst BK scenario
            worst_bks = sorted([s for s in scenarios if s.get("bks_total", 0) >= 15], key=lambda s: s["bks_roi"])
            if worst_bks and worst_bks[0]["bks_roi"] < -20:
                s = worst_bks[0]
                recs.append(f'<strong>{league_name} avoid BK Spread:</strong> {s["role"]} · {s["margin"]} · {s.get("edge","all")} · {s["period"]} · {s["call_sel"]} → <span class="neg">{s["bks_roi"]:+.1f}% ROI</span> ({s["bks_total"]} calls)')

            # BK vs ML comparison
            if o.get("bko_n", 0) >= 20 and o.get("ml_n", 0) >= 20:
                diff = o["bko_roi"] - o["ml_roi"]
                if abs(diff) > 10:
                    better = "BK Odds" if diff > 0 else "ML"
                    recs.append(f'<strong>{league_name}:</strong> {better} outperforms by {abs(diff):.0f}pp (BK Odds {o["bko_roi"]:+.1f}% vs ML {o["ml_roi"]:+.1f}%)')

            # Coverage warning
            if cov.get("pct", 0) < 10:
                recs.append(f'<strong>{league_name}:</strong> Only {cov["pct"]:.1f}% BK coverage ({cov["bk_calls"]}/{cov["total_calls"]} calls) — results unreliable. Need more live games with bookmaker data.')

        return '\n'.join(f'<div class="insight">{r}</div>' for r in recs) if recs else '<div class="insight">Insufficient BK data for recommendations.</div>'

    # Chart data extraction
    nba = data.get("NBA", {})
    wnba = data.get("WNBA", {})
    nrl = data.get("NRL", {})

    def _qval(league_data, q, key):
        d = league_data.get("by_quarter", {}).get(q)
        return d.get(key, 0) if d else "null"

    def _mval(league_data, m, key):
        d = league_data.get("by_margin", {}).get(m)
        return d.get(key, 0) if d else "null"

    def _eval(league_data, e, key):
        d = league_data.get("by_edge", {}).get(e)
        return d.get(key, 0) if d else "null"

    def _cval(league_data, c, key):
        d = league_data.get("by_consensus", {}).get(c)
        return d.get(key, 0) if d else "null"

    def _pval(league_data, p, key):
        d = league_data.get("by_position", {}).get(p)
        return d.get(key, 0) if d else "null"

    def _calval(league_data, band):
        d = league_data.get("calibration", {}).get(band)
        return d["actual"] if d else "null"

    # Role chart data — all 3 leagues
    def _rval(ld, role, key):
        d = ld.get("by_role", {}).get(role)
        return d.get(key, 0) if d else "null"

    nba_role = nba.get("by_role", {})
    wnba_role = wnba.get("by_role", {})
    nrl_role = nrl.get("by_role", {})

    # Summary card values
    nba_o = nba.get("overall") or {}
    wnba_o = wnba.get("overall") or {}
    nrl_o = nrl.get("overall") or {}
    nrl_live = nrl.get("by_source", {}).get("live") or {}

    # Build role table rows — all 3 leagues
    def _role_rows():
        rows = ""
        for role in ["favorite", "underdog"]:
            for league_name, ld in [("NBA", nba), ("WNBA", wnba), ("NRL", nrl)]:
                d = ld.get("by_role", {}).get(role)
                if not d:
                    continue
                rows += f'<tr><td>{role.title()}</td><td>{league_name}</td>'
                rows += _n_cell(d)
                rows += _pct_cell(d, "acc")
                rows += _roi_cell(d, "flat_roi")
                rows += _roi_cell(d, "ml_roi")
                rows += _roi_cell(d, "bko_roi")
                rows += _roi_cell(d, "bks_roi")
                rows += '</tr>'
        return rows

    # Period table rows — all 3 leagues with BK
    def _period_rows():
        rows = ""
        for q in ["Q1", "Q2", "Q3", "Q4"]:
            nrl_q = nrl.get("by_quarter", {}).get(q if q != "OT" else "ET")
            rows += f'<tr><td>{q}</td>'
            for ld in [nba, wnba, nrl if q != "OT" else {}]:
                qd = (ld or {}).get("by_quarter", {}).get(q) if ld != nrl else nrl_q
                rows += _roi_cell(qd, "ml_roi")
                rows += _roi_cell(qd, "bko_roi")
                rows += _roi_cell(qd, "bks_roi")
            rows += '</tr>'
        return rows

    # Margin table rows — all 3 leagues with BK
    def _margin_rows():
        rows = ""
        for m in ["0-6", "7-12", "13-20", "21+"]:
            rows += f'<tr><td>{m}</td>'
            for ld in [nba, wnba, nrl]:
                md = ld.get("by_margin", {}).get(m)
                rows += _roi_cell(md, "flat_roi")
                rows += _roi_cell(md, "bko_roi")
                rows += _roi_cell(md, "bks_roi")
            rows += '</tr>'
        return rows

    # Edge table rows — all 3 leagues with BK
    def _edge_rows():
        rows = ""
        for e in ["<0", "0-5", "5-10", "10+"]:
            rows += f'<tr><td>{"&lt;0" if e == "<0" else e}</td>'
            for ld in [nba, wnba, nrl]:
                ed = ld.get("by_edge", {}).get(e)
                rows += _roi_cell(ed, "ml_roi")
                rows += _roi_cell(ed, "bko_roi")
                rows += _roi_cell(ed, "bks_roi")
            rows += '</tr>'
        return rows

    # Consensus table rows
    def _consensus_rows():
        rows = ""
        pills = {"strong": "pill-green", "conflicted": "pill-red", "ml_only": "pill-yellow", "historical_only": ""}
        for con in ["strong", "conflicted", "ml_only", "historical_only"]:
            pill = f'<span class="pill {pills[con]}">{con}</span>' if pills[con] else con
            rows += f'<tr><td>{pill}</td>'
            for ld in [nba, wnba, nrl]:
                cd = ld.get("by_consensus", {}).get(con)
                rows += _roi_cell(cd, "flat_roi")
                rows += _roi_cell(cd, "bko_roi")
                rows += _roi_cell(cd, "bks_roi")
            rows += '</tr>'
        return rows

    # Position table rows
    def _position_rows():
        rows = ""
        pills = {"leading": "pill-green", "trailing": "pill-red"}
        for pos in ["leading", "trailing"]:
            rows += f'<tr><td><span class="pill {pills[pos]}">{pos.title()}</span></td>'
            for ld in [nba, wnba, nrl]:
                pd = ld.get("by_position", {}).get(pos)
                rows += _n_cell(pd)
                rows += _roi_cell(pd, "flat_roi")
                rows += _roi_cell(pd, "bko_roi")
                rows += _roi_cell(pd, "bks_roi")
            rows += '</tr>'
        return rows

    # Calibration table rows
    def _calibration_rows():
        rows = ""
        bands = ["60-65%", "65-70%", "70-75%", "75-80%", "80-85%", "85-90%", "90-95%", "95-100%"]
        for band in bands:
            rows += f'<tr><td>{band}</td>'
            for ld in [nba, wnba, nrl]:
                cal = ld.get("calibration", {}).get(band)
                if cal:
                    diff = cal["actual"] - cal["expected"]
                    status = "OK" if abs(diff) < 5 else ("OVER" if diff < -5 else "UNDER")
                    c = "" if status == "OK" else ("neg" if status == "OVER" else "pos")
                    rows += f'<td>{cal["n"]:,}</td><td class="{c}">{cal["actual"]:.1f}%</td>'
                else:
                    rows += '<td>—</td><td>—</td>'
            cal0 = nba.get("calibration", {}).get(band) or wnba.get("calibration", {}).get(band)
            exp = cal0["expected"] if cal0 else ""
            rows += f'<td>{exp}</td>'
            if cal0:
                # Status based on NBA (largest sample)
                nba_cal = nba.get("calibration", {}).get(band)
                if nba_cal:
                    diff = nba_cal["actual"] - nba_cal["expected"]
                    if abs(diff) < 5:
                        rows += '<td><span class="pill pill-green">OK</span></td>'
                    else:
                        rows += '<td><span class="pill pill-red">OVER</span></td>'
                else:
                    rows += '<td>—</td>'
            else:
                rows += '<td>—</td>'
            rows += '</tr>'
        return rows

    html = f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PW Cross-League ROI Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
:root {{
  --bg: #0a1628; --card: #111d33; --border: #1e3a5f; --text: #c8d6e5;
  --muted: #7f8c9b; --accent: #4fc3f7; --green: #4caf50; --red: #ef5350;
  --yellow: #ffb74d; --purple: #b39ddb;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, monospace; background: var(--bg); color: var(--text); padding: 20px; line-height: 1.5; }}
h1 {{ color: var(--accent); font-size: 1.6rem; margin-bottom: 4px; }}
h2 {{ color: var(--accent); font-size: 1.2rem; margin: 30px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }}
.subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }}
.grid {{ display: grid; gap: 16px; margin: 16px 0; }}
.grid-2 {{ grid-template-columns: 1fr 1fr; }}
.grid-3 {{ grid-template-columns: 1fr 1fr 1fr; }}
.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }}
.card-header {{ font-size: 0.8rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
.card-value {{ font-size: 1.8rem; font-weight: 700; }}
.card-detail {{ font-size: 0.8rem; color: var(--muted); margin-top: 4px; }}
.pos {{ color: var(--green); }} .neg {{ color: var(--red); }} .neu {{ color: var(--yellow); }} .accent {{ color: var(--accent); }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 8px 0; }}
th {{ background: rgba(79,195,247,0.1); color: var(--accent); text-align: left; padding: 8px 10px; font-weight: 600; border-bottom: 2px solid var(--border); white-space: nowrap; }}
td {{ padding: 6px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); white-space: nowrap; }}
tr:hover td {{ background: rgba(79,195,247,0.04); }}
.tbl-container {{ overflow-x: auto; }}
.chart-container {{ position: relative; height: 400px; margin: 12px 0; }}
.chart-container-tall {{ position: relative; height: 450px; margin: 12px 0; }}
th[data-sort] {{ user-select: none; }}
th[data-sort]::after {{ content: ' ⇅'; opacity: 0.3; font-size: 0.7em; }}
th.sort-asc::after {{ content: ' ▲'; opacity: 0.8; }}
th.sort-desc::after {{ content: ' ▼'; opacity: 0.8; }}
.insight {{ background: rgba(79,195,247,0.08); border-left: 3px solid var(--accent); padding: 10px 14px; margin: 12px 0; border-radius: 0 6px 6px 0; font-size: 0.85rem; }}
.insight strong {{ color: var(--accent); }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 0.75rem; font-weight: 600; margin-right: 4px; }}
.pill-green {{ background: rgba(76,175,80,0.2); color: var(--green); }}
.pill-red {{ background: rgba(239,83,80,0.2); color: var(--red); }}
.pill-yellow {{ background: rgba(255,183,77,0.2); color: var(--yellow); }}
.section {{ margin-bottom: 30px; }}
@media (max-width: 900px) {{ .grid-2, .grid-3 {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<h1>PW Cross-League ROI Analysis</h1>
<div class="subtitle">NBA ({nba_o.get("n",0):,} calls) &middot; WNBA ({wnba_o.get("n",0):,} calls) &middot; NRL ({nrl_o.get("n",0):,} all / {nrl_live.get("n",0):,} live) &middot; Generated {now} &middot; Issues: NRL #128, NBA #300</div>
<div style="font-size:0.72rem;color:#999;margin-top:2px">Suppressed PW calls excluded from all ROI calculations and call counts. Spread ROI uses -110 juice (+$90.91/-$100 per unit).</div>

<div style="display:flex;gap:14px;align-items:flex-start;flex-wrap:wrap;margin-bottom:10px;font-size:0.78rem">
  <div style="display:flex;gap:6px;align-items:center"><span style="color:#4fc3f7;font-weight:600">NBA</span>
    <select id="report-season-NBA" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
    <select id="report-segment-NBA" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
  </div>
  <div style="display:flex;gap:6px;align-items:center"><span style="color:#b39ddb;font-weight:600">WNBA</span>
    <select id="report-season-WNBA" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
    <select id="report-segment-WNBA" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
  </div>
  <div style="display:flex;gap:6px;align-items:center"><span style="color:#4caf50;font-weight:600">NRL</span>
    <select id="report-season-NRL" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
    <select id="report-segment-NRL" onchange="applyReportFilters()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 4px;font-size:0.78rem"></select>
  </div>
  <span id="report-filter-badge"></span>
</div>

<div style="display:flex;gap:8px;margin-bottom:16px">
  <button class="pill pill-green" style="cursor:pointer;padding:4px 14px;font-size:0.85rem" onclick="switchReportTab('overall')" id="tab-overall">Overall Performance</button>
  <button class="pill" style="cursor:pointer;padding:4px 14px;font-size:0.85rem;background:rgba(128,128,128,0.15)" onclick="switchReportTab('bk')" id="tab-bk">📈 Bookmaker ROI</button>
</div>

<div id="report-tab-overall">
<div class="section">
<h2>Overall Performance</h2>
<div class="grid grid-3">
  <div class="card">
    <div class="card-header">NBA</div>
    <div id="overview-card-NBA" data-original-html="">
    <div class="card-value">{nba_o.get("acc",0):.1f}% <span style="font-size:0.9rem" class="pos">+{nba_o.get("flat_roi",0):.1f}% flat</span></div>
    <div class="card-detail">{nba_o.get("n",0):,} calls &middot; ML ROI {nba_o.get("ml_roi",0):+.1f}% ({nba_o.get("ml_n",0):,} w/odds) &middot; BK Odds <span class="{"pos" if nba_o.get("bko_roi",0)>0 else "neg"}">{nba_o.get("bko_roi",0):+.1f}%</span> &middot; BK Spr <span class="{"pos" if nba_o.get("bks_roi",0)>0 else "neg"}">{nba_o.get("bks_roi",0):+.1f}%</span></div>
    </div>
  </div>
  <div class="card">
    <div class="card-header">WNBA</div>
    <div id="overview-card-WNBA" data-original-html="">
    <div class="card-value">{wnba_o.get("acc",0):.1f}% <span style="font-size:0.9rem" class="pos">+{wnba_o.get("flat_roi",0):.1f}% flat</span></div>
    <div class="card-detail">{wnba_o.get("n",0):,} calls &middot; ML ROI <span class="pos">{wnba_o.get("ml_roi",0):+.1f}%</span> ({wnba_o.get("ml_n",0):,} w/odds) &middot; BK Odds <span class="{"pos" if wnba_o.get("bko_roi",0)>0 else "neg"}">{wnba_o.get("bko_roi",0):+.1f}%</span> &middot; BK Spr <span class="{"pos" if wnba_o.get("bks_roi",0)>0 else "neg"}">{wnba_o.get("bks_roi",0):+.1f}%</span></div>
    </div>
  </div>
  <div class="card">
    <div class="card-header">NRL (Live)</div>
    <div id="overview-card-NRL" data-original-html="">
    <div class="card-value">{nrl_live.get("acc",0):.1f}% <span style="font-size:0.9rem" class="pos">+{nrl_live.get("flat_roi",0):.1f}% flat</span></div>
    <div class="card-detail">{nrl_live.get("n",0):,} live calls &middot; ML ROI {nrl_live.get("ml_roi",0):+.1f}% &middot; Spread ROI <span class="pos">{nrl_live.get("sp_roi",0):+.1f}%</span> &middot; BK Odds <span class="{"pos" if nrl_live.get("bko_roi",0)>0 else "neg"}">{nrl_live.get("bko_roi",0):+.1f}%</span> &middot; BK Spr <span class="{"pos" if nrl_live.get("bks_roi",0)>0 else "neg"}">{nrl_live.get("bks_roi",0):+.1f}%</span></div>
    </div>
  </div>
</div>
</div>

<div class="section">
<h2>Key Insights</h2>
{_insights_html()}
</div>

<div class="section">
<h2>ROI by Role (Favorite vs Underdog)</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartRole"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
    <tr><th>Role</th><th>League</th><th>Calls</th><th>Acc%</th><th>Flat ROI</th><th>ML ROI</th><th>BK Odds</th><th>BK Spr</th></tr>
    {_role_rows()}
  </table>
</div></div>
</div>

<div class="section">
<h2>ROI by Period / Quarter</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartPeriod"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
    <tr><th>Period</th><th>NBA ML</th><th>NBA BK Odds</th><th>NBA BK Spr</th><th>WNBA ML</th><th>WNBA BK Odds</th><th>WNBA BK Spr</th><th>NRL ML</th><th>NRL BK Odds</th><th>NRL BK Spr</th></tr>
    {_period_rows()}
  </table>
</div></div>
</div>

<div class="section">
<h2>ROI by Margin at Fire</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartMargin"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
    <tr><th>Margin</th><th>NBA Flat</th><th>NBA BK Odds</th><th>NBA BK Spr</th><th>WNBA Flat</th><th>WNBA BK Odds</th><th>WNBA BK Spr</th><th>NRL Flat</th><th>NRL BK Odds</th><th>NRL BK Spr</th></tr>
    {_margin_rows()}
  </table>
</div></div>
</div>

<div class="section">
<h2>ROI by Condition Edge</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartEdge"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
    <tr><th>Edge</th><th>NBA ML</th><th>NBA BK Odds</th><th>NBA BK Spr</th><th>WNBA ML</th><th>WNBA BK Odds</th><th>WNBA BK Spr</th><th>NRL ML</th><th>NRL BK Odds</th><th>NRL BK Spr</th></tr>
    {_edge_rows()}
  </table>
</div></div>
</div>

<div class="section">
<h2>ROI by Consensus Type</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartConsensus"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
    <tr><th>Consensus</th><th>NBA Flat</th><th>NBA BK Odds</th><th>NBA BK Spr</th><th>WNBA Flat</th><th>WNBA BK Odds</th><th>WNBA BK Spr</th><th>NRL Flat</th><th>NRL BK Odds</th><th>NRL BK Spr</th></tr>
    {_consensus_rows()}
  </table>
</div></div>
</div>

<div class="section">
<h2>Leading vs Trailing</h2>
<div class="card"><div class="chart-container-tall"><canvas id="chartPosition"></canvas></div></div>
<div class="card" style="margin-top:12px"><div class="tbl-container">
  <table>
      <tr><th>Position</th><th>NBA #</th><th>NBA Flat</th><th>NBA BK Odds</th><th>NBA BK Spr</th><th>WNBA #</th><th>WNBA Flat</th><th>WNBA BK Odds</th><th>WNBA BK Spr</th><th>NRL #</th><th>NRL Flat</th><th>NRL BK Odds</th><th>NRL BK Spr</th></tr>
      {_position_rows()}
    </table>
  </div></div>
</div>

<div class="section">
<h2>Confidence Calibration</h2>
<div class="card">
  <div class="chart-container" style="height:350px"><canvas id="chartCalibration"></canvas></div>
</div>
<div class="tbl-container" style="margin-top:12px">
<table>
  <tr><th>Band</th><th>NBA #</th><th>NBA Actual</th><th>WNBA #</th><th>WNBA Actual</th><th>NRL #</th><th>NRL Actual</th><th>Expected</th><th>Status</th></tr>
  {_calibration_rows()}
</table>
</div>
</div>

</div><!-- end report-tab-overall -->

<div id="report-tab-bk" style="display:none">
<div class="section">
<h2>📈 Bookmaker ROI Analysis</h2>
<p style="color:var(--muted);font-size:0.85rem">BK Odds ROI = profit from bookmaker moneyline at PW fire time. BK Spread ROI = profit from bookmaker live spread at fire time. Only calls with genuine bookmaker data are included.</p>

<h3 style="color:var(--green)">BK Coverage by League</h3>
<div class="tbl-container">
<table>
  <tr><th>League</th><th>Total Calls</th><th>BK Calls</th><th>Coverage</th><th>BK Odds ROI</th><th>BK Spr ROI</th></tr>
  <tbody id="bk-coverage-body" data-original-html="">{_bk_coverage_rows()}</tbody>
</table>
</div>

<h3 style="color:var(--green);margin-top:20px">🏆 Best BK Scenarios (Top 3 per league)</h3>
<p style="color:var(--muted);font-size:0.82rem">4-way filter combinations (role × margin × period × call selection).</p>
<div style="display:flex;flex-wrap:wrap;gap:8px 16px;margin-bottom:8px;align-items:center">
  <label style="color:var(--muted);font-size:0.82rem">Sort by:
    <select id="bk-sort" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="bks_roi">BK Spread ROI %</option>
      <option value="bko_roi">BK Odds ROI %</option>
      <option value="acc">Accuracy %</option>
      <option value="n">Calls</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Min calls:
    <input type="number" id="bk-min" value="5" min="1" max="99" onchange="renderBkTables()" style="width:50px;background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Role:
    <select id="bk-role" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="favorite">Favorite</option><option value="underdog">Underdog</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Margin:
    <select id="bk-margin" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="leading">Leading</option><option value="trailing">Trailing</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Period:
    <select id="bk-period" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="Q1">Q1</option><option value="Q2">Q2</option><option value="Q3">Q3</option><option value="Q4">Q4</option><option value="ET">ET</option><option value="OT">OT</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Edge:
    <select id="bk-edge" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="10+">&ge;10</option><option value="5-10">5-10</option><option value="0-5">0-5</option><option value="<0">&lt;0</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Call Sel:
    <select id="bk-csel" onchange="renderBkTables()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="first">first</option><option value="last">last</option><option value="1st/qtr">1st/qtr</option><option value="1st/half">1st/half</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.75rem;cursor:pointer"><input type="checkbox" id="bk-save-default" onchange="saveBkDefaults()" style="width:12px;height:12px"> Set as default</label>
</div>
<div class="tbl-container" id="best-bk-container"></div>

<h3 style="color:var(--red);margin-top:20px">⚠ Worst BK Scenarios (Bottom 3 per league)</h3>
<div class="tbl-container" id="worst-bk-container"></div>

<h3 style="color:var(--green);margin-top:20px">🗺 BK ROI Heatmap: Role × Period</h3>
<div style="margin-bottom:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
  <label style="color:var(--muted);font-size:0.82rem">Edge:
    <select id="heatmap-edge" onchange="renderBkHeatmaps()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="10+">&ge;10</option><option value="5-10">5-10</option><option value="0-5">0-5</option><option value="<0">&lt;0</option>
    </select>
  </label>
  <label style="color:var(--muted);font-size:0.82rem">Call Sel:
    <select id="heatmap-csel" onchange="renderBkHeatmaps()" style="background:#1a2332;color:#e0e6ed;border:1px solid #2a3a4a;border-radius:4px;padding:2px 6px;font-size:0.82rem">
      <option value="">All</option><option value="first">first</option><option value="last">last</option><option value="1st/qtr">1st/qtr</option><option value="1st/half">1st/half</option>
    </select>
  </label>
  <span id="heatmap-filters-badge"></span>
</div>
<h4 style="color:#4fc3f7;margin:12px 0 6px;font-size:0.9rem">📈 Leading at Fire</h4>
<div class="grid grid-3">
  <div class="card"><div class="card-header">NBA</div><div class="tbl-container" id="heatmap-leading-NBA"></div></div>
  <div class="card"><div class="card-header">WNBA</div><div class="tbl-container" id="heatmap-leading-WNBA"></div></div>
  <div class="card"><div class="card-header">NRL</div><div class="tbl-container" id="heatmap-leading-NRL"></div></div>
</div>
<h4 style="color:#ff9800;margin:16px 0 6px;font-size:0.9rem">📉 Trailing at Fire</h4>
<div class="grid grid-3">
  <div class="card"><div class="card-header">NBA</div><div class="tbl-container" id="heatmap-trailing-NBA"></div></div>
  <div class="card"><div class="card-header">WNBA</div><div class="tbl-container" id="heatmap-trailing-WNBA"></div></div>
  <div class="card"><div class="card-header">NRL</div><div class="tbl-container" id="heatmap-trailing-NRL"></div></div>
</div>

<h3 style="color:var(--green);margin-top:20px">💡 BK-Specific Recommendations</h3>
{_bk_recommendations()}
</div>

</div><!-- end report-tab-bk -->

<div class="section">
<h2>Recommendations</h2>
<div class="grid grid-3">
  <div class="card" style="border-color:var(--green)">
    <div class="card-header" style="color:var(--green)">Immediate</div>
    <ul style="padding-left:18px;font-size:0.85rem;line-height:1.8">
      {_recs_list(recs_imm)}
    </ul>
  </div>
  <div class="card" style="border-color:var(--yellow)">
    <div class="card-header" style="color:var(--yellow)">Strategic</div>
    <ul style="padding-left:18px;font-size:0.85rem;line-height:1.8">
      {_recs_list(recs_strat)}
    </ul>
  </div>
  <div class="card" style="border-color:var(--purple)">
    <div class="card-header" style="color:var(--purple)">Research</div>
    <ul style="padding-left:18px;font-size:0.85rem;line-height:1.8">
      {_recs_list(recs_res)}
    </ul>
  </div>
</div>
</div>

<script>
const C = {{ nba: '#4fc3f7', wnba: '#b39ddb', nrl: '#4caf50', nrlSp: '#ffb74d', nbaBkO: '#29b6f6', nbaBkS: '#81d4fa', wnbaBkO: '#9575cd', wnbaBkS: '#ce93d8', nrlBkO: '#e57373', nrlBkS: '#ff8a65' }};
const opts = (t) => ({{
  responsive:true, maintainAspectRatio:false,
  plugins: {{ title:{{ display:true, text:t, color:'#c8d6e5', font:{{size:13}} }}, legend:{{ labels:{{ color:'#c8d6e5', font:{{size:11}} }} }} }},
  scales: {{ x:{{ ticks:{{color:'#7f8c9b'}}, grid:{{color:'rgba(255,255,255,0.05)'}} }},
             y:{{ ticks:{{color:'#7f8c9b',callback:v=>v+'%'}}, grid:{{color:'rgba(255,255,255,0.08)'}}, title:{{display:true,text:'ROI %',color:'#7f8c9b'}} }} }}
}});

new Chart(document.getElementById('chartRole'), {{
  type:'bar', data:{{ labels:['Favorite','Underdog'], datasets:[
    {{label:'NBA ML',data:[{_rval(nba,'favorite','ml_roi')},{_rval(nba,'underdog','ml_roi')}],backgroundColor:C.nba}},
    {{label:'NBA BK Odds',data:[{_rval(nba,'favorite','bko_roi')},{_rval(nba,'underdog','bko_roi')}],backgroundColor:C.nbaBkO}},
    {{label:'WNBA ML',data:[{_rval(wnba,'favorite','ml_roi')},{_rval(wnba,'underdog','ml_roi')}],backgroundColor:C.wnba}},
    {{label:'WNBA BK Odds',data:[{_rval(wnba,'favorite','bko_roi')},{_rval(wnba,'underdog','bko_roi')}],backgroundColor:C.wnbaBkO}},
    {{label:'NRL ML',data:[{_rval(nrl,'favorite','ml_roi')},{_rval(nrl,'underdog','ml_roi')}],backgroundColor:C.nrl}},
    {{label:'NRL BK Odds',data:[{_rval(nrl,'favorite','bko_roi')},{_rval(nrl,'underdog','bko_roi')}],backgroundColor:C.nrlBkO}},
    {{label:'NRL BK Spr',data:[{_rval(nrl,'favorite','bks_roi')},{_rval(nrl,'underdog','bks_roi')}],backgroundColor:C.nrlBkS}}
  ] }}, options:opts('ROI by Role (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartPeriod'), {{
  type:'line', data:{{ labels:['Q1','Q2','Q3','Q4'], datasets:[
    {{label:'NBA ML',data:[{_qval(nba,'Q1','ml_roi')},{_qval(nba,'Q2','ml_roi')},{_qval(nba,'Q3','ml_roi')},{_qval(nba,'Q4','ml_roi')}],borderColor:C.nba,fill:false,tension:0.3}},
    {{label:'NBA BK Odds',data:[{_qval(nba,'Q1','bko_roi')},{_qval(nba,'Q2','bko_roi')},{_qval(nba,'Q3','bko_roi')},{_qval(nba,'Q4','bko_roi')}],borderColor:C.nbaBkO,fill:false,tension:0.3,borderDash:[3,3]}},
    {{label:'WNBA ML',data:[{_qval(wnba,'Q1','ml_roi')},{_qval(wnba,'Q2','ml_roi')},{_qval(wnba,'Q3','ml_roi')},{_qval(wnba,'Q4','ml_roi')}],borderColor:C.wnba,fill:false,tension:0.3}},
    {{label:'WNBA BK Odds',data:[{_qval(wnba,'Q1','bko_roi')},{_qval(wnba,'Q2','bko_roi')},{_qval(wnba,'Q3','bko_roi')},{_qval(wnba,'Q4','bko_roi')}],borderColor:C.wnbaBkO,fill:false,tension:0.3,borderDash:[3,3]}},
    {{label:'NRL ML',data:[{_qval(nrl,'Q1','ml_roi')},{_qval(nrl,'Q2','ml_roi')},{_qval(nrl,'Q3','ml_roi')},{_qval(nrl,'Q4','ml_roi')}],borderColor:C.nrl,fill:false,tension:0.3}},
    {{label:'NRL BK Odds',data:[{_qval(nrl,'Q1','bko_roi')},{_qval(nrl,'Q2','bko_roi')},{_qval(nrl,'Q3','bko_roi')},{_qval(nrl,'Q4','bko_roi')}],borderColor:C.nrlBkO,fill:false,tension:0.3,borderDash:[3,3]}}
  ] }}, options:opts('ROI by Quarter (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartMargin'), {{
  type:'bar', data:{{ labels:['0-6','7-12','13-20','21+'], datasets:[
    {{label:'NBA Flat',data:[{_mval(nba,'0-6','flat_roi')},{_mval(nba,'7-12','flat_roi')},{_mval(nba,'13-20','flat_roi')},{_mval(nba,'21+','flat_roi')}],backgroundColor:C.nba}},
    {{label:'NBA BK Odds',data:[{_mval(nba,'0-6','bko_roi')},{_mval(nba,'7-12','bko_roi')},{_mval(nba,'13-20','bko_roi')},{_mval(nba,'21+','bko_roi')}],backgroundColor:C.nbaBkO}},
    {{label:'WNBA Flat',data:[{_mval(wnba,'0-6','flat_roi')},{_mval(wnba,'7-12','flat_roi')},{_mval(wnba,'13-20','flat_roi')},{_mval(wnba,'21+','flat_roi')}],backgroundColor:C.wnba}},
    {{label:'WNBA BK Odds',data:[{_mval(wnba,'0-6','bko_roi')},{_mval(wnba,'7-12','bko_roi')},{_mval(wnba,'13-20','bko_roi')},{_mval(wnba,'21+','bko_roi')}],backgroundColor:C.wnbaBkO}},
    {{label:'NRL Flat',data:[{_mval(nrl,'0-6','flat_roi')},{_mval(nrl,'7-12','flat_roi')},{_mval(nrl,'13-20','flat_roi')},{_mval(nrl,'21+','flat_roi')}],backgroundColor:C.nrl}},
    {{label:'NRL BK Odds',data:[{_mval(nrl,'0-6','bko_roi')},{_mval(nrl,'7-12','bko_roi')},{_mval(nrl,'13-20','bko_roi')},{_mval(nrl,'21+','bko_roi')}],backgroundColor:C.nrlBkO}}
  ] }}, options:opts('ROI by Margin (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartEdge'), {{
  type:'bar', data:{{ labels:['<0','0-5','5-10','10+'], datasets:[
    {{label:'NBA ML',data:[{_eval(nba,'<0','ml_roi')},{_eval(nba,'0-5','ml_roi')},{_eval(nba,'5-10','ml_roi')},{_eval(nba,'10+','ml_roi')}],backgroundColor:C.nba}},
    {{label:'NBA BK Odds',data:[{_eval(nba,'<0','bko_roi')},{_eval(nba,'0-5','bko_roi')},{_eval(nba,'5-10','bko_roi')},{_eval(nba,'10+','bko_roi')}],backgroundColor:C.nbaBkO}},
    {{label:'WNBA ML',data:[{_eval(wnba,'<0','ml_roi')},{_eval(wnba,'0-5','ml_roi')},{_eval(wnba,'5-10','ml_roi')},{_eval(wnba,'10+','ml_roi')}],backgroundColor:C.wnba}},
    {{label:'WNBA BK Odds',data:[{_eval(wnba,'<0','bko_roi')},{_eval(wnba,'0-5','bko_roi')},{_eval(wnba,'5-10','bko_roi')},{_eval(wnba,'10+','bko_roi')}],backgroundColor:C.wnbaBkO}},
    {{label:'NRL ML',data:[{_eval(nrl,'<0','ml_roi')},{_eval(nrl,'0-5','ml_roi')},{_eval(nrl,'5-10','ml_roi')},{_eval(nrl,'10+','ml_roi')}],backgroundColor:C.nrl}},
    {{label:'NRL BK Odds',data:[{_eval(nrl,'<0','bko_roi')},{_eval(nrl,'0-5','bko_roi')},{_eval(nrl,'5-10','bko_roi')},{_eval(nrl,'10+','bko_roi')}],backgroundColor:C.nrlBkO}}
  ] }}, options:opts('ROI by Edge (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartConsensus'), {{
  type:'bar', data:{{ labels:['Strong','Conflicted','ML Only','Hist Only'], datasets:[
    {{label:'NBA Flat',data:[{_cval(nba,'strong','flat_roi')},{_cval(nba,'conflicted','flat_roi')},{_cval(nba,'ml_only','flat_roi')},{_cval(nba,'historical_only','flat_roi')}],backgroundColor:C.nba}},
    {{label:'NBA BK Odds',data:[{_cval(nba,'strong','bko_roi')},{_cval(nba,'conflicted','bko_roi')},{_cval(nba,'ml_only','bko_roi')},{_cval(nba,'historical_only','bko_roi')}],backgroundColor:C.nbaBkO}},
    {{label:'WNBA Flat',data:[{_cval(wnba,'strong','flat_roi')},{_cval(wnba,'conflicted','flat_roi')},{_cval(wnba,'ml_only','flat_roi')},{_cval(wnba,'historical_only','flat_roi')}],backgroundColor:C.wnba}},
    {{label:'WNBA BK Odds',data:[{_cval(wnba,'strong','bko_roi')},{_cval(wnba,'conflicted','bko_roi')},{_cval(wnba,'ml_only','bko_roi')},{_cval(wnba,'historical_only','bko_roi')}],backgroundColor:C.wnbaBkO}},
    {{label:'NRL Flat',data:[{_cval(nrl,'strong','flat_roi')},{_cval(nrl,'conflicted','flat_roi')},{_cval(nrl,'ml_only','flat_roi')},{_cval(nrl,'historical_only','flat_roi')}],backgroundColor:C.nrl}},
    {{label:'NRL BK Odds',data:[{_cval(nrl,'strong','bko_roi')},{_cval(nrl,'conflicted','bko_roi')},{_cval(nrl,'ml_only','bko_roi')},{_cval(nrl,'historical_only','bko_roi')}],backgroundColor:C.nrlBkO}}
  ] }}, options:opts('ROI by Consensus (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartPosition'), {{
  type:'bar', data:{{ labels:['Leading','Trailing'], datasets:[
    {{label:'NBA Flat',data:[{_pval(nba,'leading','flat_roi')},{_pval(nba,'trailing','flat_roi')}],backgroundColor:C.nba}},
    {{label:'NBA BK Odds',data:[{_pval(nba,'leading','bko_roi')},{_pval(nba,'trailing','bko_roi')}],backgroundColor:C.nbaBkO}},
    {{label:'WNBA Flat',data:[{_pval(wnba,'leading','flat_roi')},{_pval(wnba,'trailing','flat_roi')}],backgroundColor:C.wnba}},
    {{label:'WNBA BK Odds',data:[{_pval(wnba,'leading','bko_roi')},{_pval(wnba,'trailing','bko_roi')}],backgroundColor:C.wnbaBkO}},
    {{label:'NRL Flat',data:[{_pval(nrl,'leading','flat_roi')},{_pval(nrl,'trailing','flat_roi')}],backgroundColor:C.nrl}},
    {{label:'NRL BK Odds',data:[{_pval(nrl,'leading','bko_roi')},{_pval(nrl,'trailing','bko_roi')}],backgroundColor:C.nrlBkO}}
  ] }}, options:opts('ROI: Leading vs Trailing (all leagues, incl. BK)')
}});

new Chart(document.getElementById('chartCalibration'), {{
  type:'line', data:{{ labels:['60-65%','65-70%','70-75%','75-80%','80-85%','85-90%','90-95%','95-100%'], datasets:[
    {{label:'Perfect',data:[62.5,67.5,72.5,77.5,82.5,87.5,92.5,97.5],borderColor:'#ffffff44',borderDash:[4,4],pointRadius:0,fill:false}},
    {{label:'NBA',data:[{_calval(nba,'60-65%')},{_calval(nba,'65-70%')},{_calval(nba,'70-75%')},{_calval(nba,'75-80%')},{_calval(nba,'80-85%')},{_calval(nba,'85-90%')},{_calval(nba,'90-95%')},{_calval(nba,'95-100%')}],borderColor:C.nba,fill:false,tension:0.3}},
    {{label:'WNBA',data:[{_calval(wnba,'60-65%')},{_calval(wnba,'65-70%')},{_calval(wnba,'70-75%')},{_calval(wnba,'75-80%')},{_calval(wnba,'80-85%')},{_calval(wnba,'85-90%')},{_calval(wnba,'90-95%')},{_calval(wnba,'95-100%')}],borderColor:C.wnba,fill:false,tension:0.3}},
    {{label:'NRL',data:[{_calval(nrl,'60-65%')},{_calval(nrl,'65-70%')},{_calval(nrl,'70-75%')},{_calval(nrl,'75-80%')},{_calval(nrl,'80-85%')},{_calval(nrl,'85-90%')},{_calval(nrl,'90-95%')},{_calval(nrl,'95-100%')}],borderColor:C.nrl,fill:false,tension:0.3}}
  ] }}, options:{{
    ...opts('Confidence Calibration: Actual vs Expected Accuracy'),
    scales:{{ x:{{ticks:{{color:'#7f8c9b'}},grid:{{color:'rgba(255,255,255,0.05)'}}}},
              y:{{min:0,max:105,ticks:{{color:'#7f8c9b',callback:v=>v+'%'}},grid:{{color:'rgba(255,255,255,0.08)'}},title:{{display:true,text:'Actual Accuracy %',color:'#7f8c9b'}}}} }}
  }}
}});

// Raw call data for client-side season/segment filtering (#190)
const _rawCalls = {_raw_calls_json_str};
const _allBkScenarios = {_bk_scenarios_json_str};
const _leagueSeasons = {_league_seasons_json};
const _leagueSegments = {_league_segments_json};
const _segmentLabels = {_segment_labels_json};
let _bkScenarios = _allBkScenarios;

// Season/Segment filter helpers (#190)
function _populateFilterDropdowns() {{
  for (const lg of ['NBA','WNBA','NRL']) {{
    const sSel = document.getElementById('report-season-' + lg);
    const sgSel = document.getElementById('report-segment-' + lg);
    if (sSel) {{
      const prev = sSel.value;
      sSel.innerHTML = '<option value="">All Seasons</option>' +
        (_leagueSeasons[lg] || []).map(s => '<option value="' + s + '">' + s + '</option>').join('');
      if (prev) sSel.value = prev;
    }}
    if (sgSel) {{
      const prev = sgSel.value;
      sgSel.innerHTML = '<option value="">All Segments</option>' +
        (_leagueSegments[lg] || []).map(s => {{
          const label = _segmentLabels[s] || s.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
          return '<option value="' + s + '">' + label + '</option>';
        }}).join('');
      if (prev) sgSel.value = prev;
    }}
  }}
}}

function _getLeagueFilters() {{
  const f = {{}};
  let anyActive = false;
  for (const lg of ['NBA','WNBA','NRL']) {{
    const s = (document.getElementById('report-season-' + lg) || {{}}).value || '';
    const sg = (document.getElementById('report-segment-' + lg) || {{}}).value || '';
    f[lg] = {{ season: s, segment: sg }};
    if (s || sg) anyActive = true;
  }}
  return anyActive ? f : null;
}}

function _getFilteredCalls() {{
  const f = _getLeagueFilters();
  if (!f) return null; // no filter — use pre-computed
  return _rawCalls.filter(c => {{
    const lf = f[c.lg];
    if (!lf) return true;
    if (lf.season && c.s !== lf.season) return false;
    if (lf.segment && c.sg !== lf.segment) return false;
    return true;
  }});
}}

function _mlRoiJs(mlStr, correct, league) {{
  if (!mlStr) return null;
  try {{
    const odds = parseFloat(mlStr);
    if (league === 'NRL') {{ return odds > 1 ? (correct ? odds - 1 : -1) : null; }}
    else {{ if (odds > 0) return correct ? odds/100 : -1; if (odds < 0) return correct ? 100/Math.abs(odds) : -1; }}
  }} catch(e) {{}}
  return null;
}}

function _analyzeJs(calls, league) {{
  const n = calls.length; if (!n) return null;
  const correct = calls.filter(c => c.ok).length;
  const games = new Set(calls.map(c => c.gid).filter(Boolean)).size;
  const flat = calls.reduce((s,c) => s + (c.ok ? 1 : -1), 0);
  let mlPnl = 0, mlN = 0;
  calls.forEach(c => {{ const r = _mlRoiJs(c.ml, c.ok, league); if (r !== null) {{ mlPnl += r; mlN++; }} }});
  let spY=0, spN=0; calls.forEach(c => {{ if (c.sp && c.fm != null) {{ try {{ if (c.fm + parseFloat(c.sp) > 0) spY++; else spN++; }} catch(e){{}} }} }});
  let lsY=0, lsN=0; calls.forEach(c => {{ if (c.lsp && c.fm != null) {{ try {{ if (c.fm + parseFloat(c.lsp) > 0) lsY++; else lsN++; }} catch(e){{}} }} }});
  let bksY=0, bksN=0; calls.forEach(c => {{ if (c.bksp && c.fm != null) {{ try {{ if (c.fm + parseFloat(c.bksp) > 0) bksY++; else bksN++; }} catch(e){{}} }} }});
  let bkoPnl=0, bkoN=0; calls.forEach(c => {{ const r = _mlRoiJs(c.bkml, c.ok, league); if (r !== null) {{ bkoPnl += r; bkoN++; }} }});
  const spT = spY+spN, lsT = lsY+lsN, bksT = bksY+bksN;
  const W = 90.91, L = 100;
  const spRoi = spT ? ((spY*W - spN*L) / (spT*L) * 100) : 0;
  const lsRoi = lsT ? ((lsY*W - lsN*L) / (lsT*L) * 100) : 0;
  const bksRoi = bksT ? ((bksY*W - bksN*L) / (bksT*L) * 100) : 0;
  return {{ n, games, correct, acc: +(correct/n*100).toFixed(1), flat, flat_roi: +(flat/n*100).toFixed(1),
    ml_pnl: +mlPnl.toFixed(2), ml_n: mlN, ml_roi: mlN ? +(mlPnl/mlN*100).toFixed(1) : 0,
    sp_yes: spY, sp_no: spN, sp_roi: +spRoi.toFixed(1), ls_yes: lsY, ls_no: lsN, ls_roi: +lsRoi.toFixed(1),
    bks_yes: bksY, bks_no: bksN, bks_roi: +bksRoi.toFixed(1), bks_total: bksT,
    bko_pnl: +bkoPnl.toFixed(2), bko_n: bkoN, bko_roi: bkoN ? +(bkoPnl/bkoN*100).toFixed(1) : 0 }};
}}

function _edgeKey(e) {{
  if (e == null) return '';
  const v = parseFloat(e); if (isNaN(v)) return '';
  if (v >= 10) return '10+'; if (v >= 5) return '5-10'; if (v >= 0) return '0-5'; return '<0';
}}

function _recomputeBkScenarios(calls) {{
  const scenarios = [];
  const roles = ['favorite','underdog'];
  const margins = ['all','leading','trailing'];
  const edges = ['all','10+','5-10','0-5','<0'];
  for (const lg of ['NBA','WNBA','NRL']) {{
    const lgCalls = calls.filter(c => c.lg === lg && (c.bkml || c.bksp));
    for (const role of roles) {{
      for (const margin of margins) {{
        for (const edge of edges) {{
          const periods = lg === 'NRL' ? ['Q1','Q2','Q3','Q4','ET'] : ['Q1','Q2','Q3','Q4','OT'];
          for (const period of periods) {{
            let subset = lgCalls.filter(c => c.role === role && c.q === period);
            if (margin !== 'all') subset = subset.filter(c => ((c.mf||0) >= 0 ? 'leading' : 'trailing') === margin);
            if (edge !== 'all') subset = subset.filter(c => _edgeKey(c.edge) === edge);
            if (!subset.length) continue;
            const csels = ['all','first','last','1st/qtr','1st/half'];
            for (const csel of csels) {{
              let filtered = subset;
              if (csel !== 'all') {{
                const byG = {{}};
                filtered.forEach(c => {{ (byG[c.gid] = byG[c.gid] || []).push(c); }});
                filtered = [];
                for (const gs of Object.values(byG)) {{
                  const sorted = gs.slice().sort((a,b) => (a.ts||'').localeCompare(b.ts||''));
                  if (csel === 'first') filtered.push(sorted[0]);
                  else if (csel === 'last') filtered.push(sorted[sorted.length-1]);
                  else if (csel === '1st/qtr') {{ const byQ = {{}}; sorted.forEach(c => {{ if (!byQ[c.q]) byQ[c.q] = c; }}); filtered.push(...Object.values(byQ)); }}
                  else if (csel === '1st/half') {{ const byH = {{}}; sorted.forEach(c => {{ if (!byH[c.h]) byH[c.h] = c; }}); filtered.push(...Object.values(byH)); }}
                }}
              }}
              if (filtered.length < 1) continue;
              const a = _analyzeJs(filtered, lg);
              if (!a) continue;
              scenarios.push({{ league: lg, role, margin, edge, period, call_sel: csel, n: a.n, correct: a.correct, games: a.games, acc: a.acc, bko_roi: a.bko_roi, bko_n: a.bko_n, bks_roi: a.bks_roi, bks_total: a.bks_total }});
            }}
          }}
        }}
      }}
    }}
  }}
  return scenarios;
}}

function applyReportFilters() {{
  const filtered = _getFilteredCalls();
  if (!filtered) {{
    _bkScenarios = _allBkScenarios;
    // Restore original overview cards
    document.querySelectorAll('[data-original-html]').forEach(el => {{ el.innerHTML = el.dataset.originalHtml; }});
  }} else {{
    _bkScenarios = _recomputeBkScenarios(filtered);
    // Update overview cards per league
    for (const lg of ['NBA','WNBA','NRL']) {{
      const lgCalls = filtered.filter(c => c.lg === lg);
      const a = _analyzeJs(lgCalls, lg);
      const el = document.getElementById('overview-card-' + lg);
      if (el && a) {{
        const pos = v => v >= 0 ? 'pos' : 'neg';
        const fmt = v => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
        el.innerHTML = `<div style="font-size:1.8rem;font-weight:700">${{a.acc.toFixed(1)}}%</div>`
          + `<div style="color:var(--muted);font-size:0.82rem">${{a.correct}}/${{a.n}} calls · ${{a.games}} games</div>`
          + `<div style="margin-top:6px"><span class="${{pos(a.flat_roi)}}">Flat ${{fmt(a.flat_roi)}}</span> · <span class="${{pos(a.ml_roi)}}">ML ${{fmt(a.ml_roi)}}</span></div>`
          + `<div><span class="${{pos(a.sp_roi)}}">Spr ${{fmt(a.sp_roi)}}</span>` + (a.bko_n ? ` · <span class="${{pos(a.bko_roi)}}">BkO ${{fmt(a.bko_roi)}}</span>` : '') + `</div>`;
      }} else if (el) {{
        el.innerHTML = '<div style="color:var(--muted)">No data for filter</div>';
      }}
    }}
  }}
  // Update BK coverage table (#190)
  const covBody = document.getElementById('bk-coverage-body');
  if (covBody) {{
    if (!filtered) {{
      covBody.innerHTML = covBody.dataset.originalHtml;
    }} else {{
      let rows = '';
      for (const lg of ['NBA','WNBA','NRL']) {{
        const lgAll = filtered.filter(c => c.lg === lg);
        const lgBk = lgAll.filter(c => c.bkml || c.bksp);
        const a = _analyzeJs(lgAll, lg);
        const pct = lgAll.length ? (lgBk.length / lgAll.length * 100).toFixed(1) : '0.0';
        const fmtR = v => v == null || v === 0 ? '<td style="color:#666">\u2014</td>' : '<td class="' + (v > 0 ? 'pos' : 'neg') + '">' + (v >= 0 ? '+' : '') + v.toFixed(1) + '%</td>';
        rows += '<tr><td>' + lg + '</td><td>' + lgAll.length + '</td><td>' + lgBk.length + '</td><td>' + pct + '%</td>';
        rows += fmtR(a ? a.bko_roi : null) + fmtR(a ? a.bks_roi : null) + '</tr>';
      }}
      covBody.innerHTML = rows;
    }}
  }}
  renderBkTables();
  renderBkHeatmaps();
  // Update filter badge
  const badge = document.getElementById('report-filter-badge');
  if (badge) {{
    const f = _getLeagueFilters();
    let b = '';
    if (f) {{
      for (const lg of ['NBA','WNBA','NRL']) {{
        const lf = f[lg];
        if (lf.season || lf.segment) {{
          const parts = [];
          if (lf.season) parts.push(lf.season);
          if (lf.segment) parts.push((_segmentLabels[lf.segment] || lf.segment).replace(/_/g,' '));
          b += '<span style="background:rgba(79,195,247,0.15);color:#4fc3f7;padding:2px 8px;border-radius:4px;font-size:0.72rem;margin-right:4px">' + lg + ': ' + parts.join(' · ') + '</span>';
        }}
      }}
    }}
    badge.innerHTML = b;
  }}
  // Save per-league filters
  const saved = {{}};
  for (const lg of ['NBA','WNBA','NRL']) {{
    saved[lg] = {{
      season: (document.getElementById('report-season-' + lg) || {{}}).value || '',
      segment: (document.getElementById('report-segment-' + lg) || {{}}).value || '',
    }};
  }}
  localStorage.setItem('roi_report_filters', JSON.stringify(saved));
}}
function saveBkDefaults() {{
  if (document.getElementById('bk-save-default').checked) {{
    const d = {{ sort: document.getElementById('bk-sort').value, min: document.getElementById('bk-min').value,
      role: document.getElementById('bk-role').value, margin: document.getElementById('bk-margin').value,
      edge: document.getElementById('bk-edge').value,
      period: document.getElementById('bk-period').value, csel: document.getElementById('bk-csel').value }};
    localStorage.setItem('bk_scenario_defaults', JSON.stringify(d));
  }} else {{
    localStorage.removeItem('bk_scenario_defaults');
  }}
}}
(function restoreBkDefaults() {{
  try {{
    const d = JSON.parse(localStorage.getItem('bk_scenario_defaults'));
    if (!d) return;
    if (d.sort) document.getElementById('bk-sort').value = d.sort;
    if (d.min) document.getElementById('bk-min').value = d.min;
    if (d.role) document.getElementById('bk-role').value = d.role;
    if (d.margin) document.getElementById('bk-margin').value = d.margin;
    if (d.edge) document.getElementById('bk-edge').value = d.edge;
    if (d.period) document.getElementById('bk-period').value = d.period;
    if (d.csel) document.getElementById('bk-csel').value = d.csel;
    document.getElementById('bk-save-default').checked = true;
  }} catch(e) {{}}
}})();
function renderBkTables() {{
  const sortKey = document.getElementById('bk-sort').value;
  const minCalls = parseInt(document.getElementById('bk-min').value) || 5;
  const fRole = document.getElementById('bk-role').value;
  const fMargin = document.getElementById('bk-margin').value;
  const fPeriod = document.getElementById('bk-period').value;
  const fCsel = document.getElementById('bk-csel').value;
  const fEdge = document.getElementById('bk-edge').value;
  if (document.getElementById('bk-save-default').checked) saveBkDefaults();
  const hdr = '<table class="sortable"><tr><th>League</th><th>Role</th><th>Margin</th><th>Edge</th><th>Period</th><th>Call Sel</th><th>BK Odds ROI</th><th>BK Odds #</th><th>BK Spr ROI</th><th>BK Spr #</th><th>Accuracy</th><th>Calls</th><th>Games</th></tr>';
  const lgColor = {{'NBA':'rgba(79,195,247,0.12)','WNBA':'rgba(179,157,219,0.12)','NRL':'rgba(76,175,80,0.12)'}};
  const lgBorder = {{'NBA':'#4fc3f7','WNBA':'#b39ddb','NRL':'#4caf50'}};
  function row(s) {{
    const bkoC = s.bko_roi > 0 ? 'pos' : s.bko_roi < 0 ? 'neg' : '';
    const bksC = s.bks_roi > 0 ? 'pos' : s.bks_roi < 0 ? 'neg' : '';
    const bg = lgColor[s.league] || '';
    const bc = lgBorder[s.league] || '';
    return `<tr style="background:${{bg}};border-left:3px solid ${{bc}}"><td style="font-weight:600;color:${{bc}}">${{s.league}}</td><td>${{s.role}}</td><td>${{s.margin}}</td><td>${{s.edge||'all'}}</td><td>${{s.period}}</td><td>${{s.call_sel}}</td><td class="${{bkoC}}">${{s.bko_roi >= 0 ? '+' : ''}}${{s.bko_roi.toFixed(1)}}%</td><td>${{s.bko_n}}</td><td class="${{bksC}}">${{s.bks_roi >= 0 ? '+' : ''}}${{s.bks_roi.toFixed(1)}}%</td><td>${{s.bks_total}}</td><td>${{s.acc.toFixed(1)}}%</td><td>${{s.n}}</td><td>${{s.games}}</td></tr>`;
  }}
  const filtered = _bkScenarios.filter(s => s.n >= minCalls
    && (!fRole || s.role === fRole)
    && (!fMargin || s.margin === fMargin)
    && (!fEdge || s.edge === fEdge)
    && (!fPeriod || s.period === fPeriod)
    && (!fCsel || s.call_sel === fCsel));
  // Best: top 3 per league
  let bestHtml = hdr;
  for (const lg of ['NBA','WNBA','NRL']) {{
    const lgData = filtered.filter(s => s.league === lg);
    const sorted = lgData.slice().sort((a,b) => b[sortKey] - a[sortKey]);
    sorted.slice(0, 3).forEach(s => {{ bestHtml += row(s); }});
  }}
  bestHtml += '</table>';
  document.getElementById('best-bk-container').innerHTML = bestHtml;
  // Worst: bottom 3 per league
  let worstHtml = hdr;
  for (const lg of ['NBA','WNBA','NRL']) {{
    const lgData = filtered.filter(s => s.league === lg);
    const sorted = lgData.slice().sort((a,b) => a[sortKey] - b[sortKey]);
    sorted.slice(0, 3).forEach(s => {{ worstHtml += row(s); }});
  }}
  worstHtml += '</table>';
  document.getElementById('worst-bk-container').innerHTML = worstHtml;
}}
function renderBkHeatmaps() {{
  const fCsel = document.getElementById('heatmap-csel').value;
  const fEdge = document.getElementById('heatmap-edge').value;
  const targetCsel = fCsel || 'all';
  const targetEdge = fEdge || 'all';
  const badgeEl = document.getElementById('heatmap-filters-badge');
  if (badgeEl) {{
    let badges = '';
    if (fEdge) badges += '<span style="background:rgba(79,195,247,0.15);color:#4fc3f7;padding:2px 8px;border-radius:4px;font-size:0.75rem">Edge: ' + fEdge + '</span> ';
    if (fCsel) badges += '<span style="background:rgba(79,195,247,0.15);color:#4fc3f7;padding:2px 8px;border-radius:4px;font-size:0.75rem">Call Sel: ' + fCsel + '</span>';
    badgeEl.innerHTML = badges;
  }}
  const periods = {{'NBA':['Q1','Q2','Q3','Q4','OT'],'WNBA':['Q1','Q2','Q3','Q4','OT'],'NRL':['Q1','Q2','Q3','Q4','ET']}};
  function renderGrid(margin) {{
    for (const lg of ['NBA','WNBA','NRL']) {{
      const el = document.getElementById('heatmap-' + margin + '-' + lg);
      if (!el) continue;
      const lgData = _bkScenarios.filter(s => s.league === lg && s.call_sel === targetCsel && s.edge === targetEdge && s.margin === margin);
      let html = '<table><tr><th>Role</th><th>Period</th><th>BK Odds ROI</th><th>BK Spr ROI</th><th>Calls</th></tr>';
      for (const role of ['favorite','underdog']) {{
        for (const period of periods[lg]) {{
          const match = lgData.filter(s => s.role === role && s.period === period);
          if (!match.length) {{
            html += `<tr><td>${{role.charAt(0).toUpperCase()+role.slice(1)}}</td><td>${{period}}</td><td style="color:#666">—</td><td style="color:#666">—</td><td style="color:#666">—</td></tr>`;
            continue;
          }}
          const d = match[0];
          const ao = Math.min(Math.abs(d.bko_roi)/50,1)*0.3;
          const as_ = Math.min(Math.abs(d.bks_roi)/50,1)*0.3;
          const bgo = `rgba(${{d.bko_roi>=0?'76,175,80':'244,67,54'}},${{ao.toFixed(2)}})`;
          const bgs = `rgba(${{d.bks_roi>=0?'76,175,80':'244,67,54'}},${{as_.toFixed(2)}})`;
          html += `<tr><td>${{role.charAt(0).toUpperCase()+role.slice(1)}}</td><td>${{period}}</td><td style="background:${{bgo}};text-align:center">${{d.bko_roi>=0?'+':''}}${{d.bko_roi.toFixed(1)}}%</td><td style="background:${{bgs}};text-align:center">${{d.bks_roi>=0?'+':''}}${{d.bks_roi.toFixed(1)}}%</td><td>${{d.correct}}/${{d.n}}</td></tr>`;
        }}
      }}
      html += '</table>';
      el.innerHTML = html;
    }}
  }}
  renderGrid('leading');
  renderGrid('trailing');
}}
// Initial render + filter restore (#190)
setTimeout(() => {{
  // Populate per-league filter dropdowns (#190)
  _populateFilterDropdowns();
  // Capture original card HTML for restore
  document.querySelectorAll('[data-original-html]').forEach(el => {{ el.dataset.originalHtml = el.innerHTML; }});
  // Restore saved per-league filters
  try {{
    const saved = JSON.parse(localStorage.getItem('roi_report_filters') || '{{}}');
    let anyActive = false;
    for (const lg of ['NBA','WNBA','NRL']) {{
      const lf = saved[lg];
      if (lf) {{
        if (lf.season) {{ const el = document.getElementById('report-season-' + lg); if (el) {{ el.value = lf.season; anyActive = true; }} }}
        if (lf.segment) {{ const el = document.getElementById('report-segment-' + lg); if (el) {{ el.value = lf.segment; anyActive = true; }} }}
      }}
    }}
    if (anyActive) applyReportFilters();
  }} catch(e) {{}}
  renderBkTables(); renderBkHeatmaps();
}}, 100);

// Sub-tab switching
function switchReportTab(name) {{
  document.getElementById('report-tab-overall').style.display = name === 'overall' ? '' : 'none';
  document.getElementById('report-tab-bk').style.display = name === 'bk' ? '' : 'none';
  document.getElementById('tab-overall').style.background = name === 'overall' ? '' : 'rgba(128,128,128,0.15)';
  document.getElementById('tab-overall').className = name === 'overall' ? 'pill pill-green' : 'pill';
  document.getElementById('tab-bk').style.background = name === 'bk' ? '' : 'rgba(128,128,128,0.15)';
  document.getElementById('tab-bk').className = name === 'bk' ? 'pill pill-green' : 'pill';
}}

// Sortable tables
document.querySelectorAll('table.sortable').forEach(tbl => {{
  const headerRow = tbl.querySelector('tr');
  const ths = headerRow ? Array.from(headerRow.querySelectorAll('th[data-sort]')) : [];
  ths.forEach((th, idx) => {{
    th.style.cursor = 'pointer';
    th.title = 'Click to sort';
    th.addEventListener('click', () => {{
      const type = th.dataset.sort;
      const asc = th.classList.contains('sort-desc');
      ths.forEach(h => h.classList.remove('sort-active','sort-asc','sort-desc'));
      th.classList.add('sort-active', asc ? 'sort-asc' : 'sort-desc');
      const allRows = Array.from(tbl.querySelectorAll('tr')).slice(1);
      const colIdx = Array.from(headerRow.children).indexOf(th);
      allRows.sort((a, b) => {{
        const ca = a.children[colIdx], cb = b.children[colIdx];
        let va = (ca && ca.dataset.v != null ? ca.dataset.v : (ca ? ca.textContent : '')).trim();
        let vb = (cb && cb.dataset.v != null ? cb.dataset.v : (cb ? cb.textContent : '')).trim();
        if (type === 'num') {{
          va = parseFloat(va.replace(/[%,+—]/g, '')) || 0;
          vb = parseFloat(vb.replace(/[%,+—]/g, '')) || 0;
          return asc ? va - vb : vb - va;
        }}
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
      }});
      allRows.forEach(r => tbl.appendChild(r));
    }});
  }});
}});
</script>

</body>
</html>'''

    # Write output
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Report generated: {output_path}")
    print(f"  NBA: {nba_o.get('n',0):,} calls, {nba_o.get('acc',0):.1f}% acc")
    print(f"  WNBA: {wnba_o.get('n',0):,} calls, {wnba_o.get('acc',0):.1f}% acc")
    print(f"  NRL: {nrl_o.get('n',0):,} calls ({nrl_live.get('n',0)} live), {nrl_o.get('acc',0):.1f}% acc")

    if copy_to_nba and os.path.isdir(NBA_DIR):
        shutil.copy2(output_path, NBA_OUTPUT)
        print(f"  Copied to: {NBA_OUTPUT}")


def main():
    parser = argparse.ArgumentParser(description="Generate cross-league PW ROI report")
    parser.add_argument("--output", help="Output HTML path (default: pw_roi_report.html)")
    parser.add_argument("--copy-to-nba", action="store_true",
                        help="Also copy report to NBA repo")
    args = parser.parse_args()
    generate_report(output_path=args.output, copy_to_nba=args.copy_to_nba)


if __name__ == "__main__":
    main()
