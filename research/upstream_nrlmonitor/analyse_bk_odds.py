#!/usr/bin/env python3
"""BK Live Odds Analysis — NRL (#235).

Analyses the relationship between game state and bookmaker live odds
across NRL historical data.  Outputs bk_odds_analysis_report.html.

NRL differences from NBA version:
- Decimal odds (e.g. 1.65), not American (-154)
- Game structure: 2 x 40-min halves (not 4 quarters)
  PW calls have game_minute (0-80+), half (H1/H2), quarter (Q1-Q4 synthetic)
- Single game_history.json (no league suffix)
- Team identification: home_team/away_team/predicted_team/winner (names, not IDs)
- Pregame odds on PW call: moneyline + spread fields directly
- spread_role on PW call (not spread_fav at game level)
- No q_correct/h1_correct — Section 8 skipped
- live_moneyline IS bk_moneyline (100% identical) — ESPN comparison skipped
"""

import json
import math
import os
import statistics


# ── Helpers ────────────────────────────────────────────────────────────

def _decimal_to_prob(odds):
    """Convert decimal odds to implied probability."""
    try:
        o = float(odds)
        if o <= 1.0:
            return None
        return 1.0 / o
    except (TypeError, ValueError):
        return None


def _prob_to_decimal(prob):
    """Convert implied probability to decimal odds."""
    if prob is None or prob <= 0 or prob >= 1:
        return None
    return round(1.0 / prob, 2)


def _linear_regression(x_vals, y_vals):
    """Simple OLS linear regression.  Returns (slope, intercept, r_squared)."""
    n = len(x_vals)
    if n < 3:
        return None, None, None
    x_mean = sum(x_vals) / n
    y_mean = sum(y_vals) / n
    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_vals, y_vals))
    ss_xx = sum((x - x_mean) ** 2 for x in x_vals)
    ss_yy = sum((y - y_mean) ** 2 for y in y_vals)
    if ss_xx == 0 or ss_yy == 0:
        return None, None, None
    slope = ss_xy / ss_xx
    intercept = y_mean - slope * x_mean
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)
    return slope, intercept, r_squared


def _multi_regression_r2(X_matrix, y_vals):
    """Approximate R-squared for multi-feature regression via normal equations.

    X_matrix: list of lists (each inner list is one row of features, NO intercept column).
    Returns r_squared or None.
    Uses simplified approach: compute R2 = 1 - SS_res/SS_tot.
    """
    n = len(y_vals)
    k = len(X_matrix[0]) if X_matrix else 0
    if n < k + 2:
        return None

    # Add intercept column
    X = [[1.0] + row for row in X_matrix]
    k1 = k + 1

    # X^T X
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k1)] for a in range(k1)]
    # X^T y
    Xty = [sum(X[i][a] * y_vals[i] for i in range(n)) for a in range(k1)]

    # Solve via Gaussian elimination
    aug = [XtX[r][:] + [Xty[r]] for r in range(k1)]
    for col in range(k1):
        # Pivot
        max_row = max(range(col, k1), key=lambda r: abs(aug[r][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return None
        for row in range(k1):
            if row == col:
                continue
            factor = aug[row][col] / aug[col][col]
            for j in range(k1 + 1):
                aug[row][j] -= factor * aug[col][j]

    beta = [aug[r][k1] / aug[r][r] for r in range(k1)]

    # Predictions and R2
    y_mean = sum(y_vals) / n
    ss_tot = sum((y - y_mean) ** 2 for y in y_vals)
    if ss_tot == 0:
        return None
    ss_res = 0.0
    for i in range(n):
        pred = sum(beta[j] * X[i][j] for j in range(k1))
        ss_res += (y_vals[i] - pred) ** 2
    return 1.0 - ss_res / ss_tot


# ── Data Loading ──────────────────────────────────────────────────────

def load_data():
    """Load PW calls with BK odds from NRL game history.

    Returns (records, game_count) where:
      - records: PW calls with BK moneyline odds
      - game_count: total games in history
    """
    records = []
    path = "game_history.json"
    if not os.path.exists(path):
        print(f"ERROR: {path} not found")
        return [], 0

    with open(path) as f:
        games = json.load(f)

    game_count = len(games)

    for g in games:
        winner = g.get("winner")
        home_team = g.get("home_team")
        home_stats = g.get("home_stats") or {}
        away_stats = g.get("away_stats") or {}

        for c in g.get("predicted_winner_calls", []):
            bk_ml = c.get("bk_moneyline")
            if not bk_ml or bk_ml in ("", "--"):
                continue

            impl_prob = _decimal_to_prob(bk_ml)
            if impl_prob is None:
                continue

            margin = c.get("score_margin_at_fire")
            quarter = c.get("quarter")
            half = c.get("half")
            game_minute = c.get("game_minute")
            if margin is None or not quarter:
                continue

            # Pregame moneyline from the PW call (decimal odds)
            pregame_ml_raw = c.get("moneyline")
            pregame_ml_prob = _decimal_to_prob(pregame_ml_raw) if pregame_ml_raw else None

            # Pregame spread from PW call — expressed from predicted team's perspective
            pregame_spread_raw = c.get("spread")
            pregame_spread = None
            if pregame_spread_raw:
                try:
                    raw = float(pregame_spread_raw)
                    # spread field is from the predicted team's perspective
                    # (positive = underdog, negative = favorite)
                    # We store as-is — role is already in spread_role
                    pregame_spread = raw
                except (TypeError, ValueError):
                    pass

            # Determine which team's stats (predicted team)
            predicted_team = c.get("predicted_team")
            if predicted_team and home_team and predicted_team == home_team:
                pred_stats = home_stats
                opp_stats = away_stats
            else:
                pred_stats = away_stats
                opp_stats = home_stats

            rec = {
                "game_id": g.get("match_id"),
                "date": g.get("date", ""),
                "margin": margin,
                "quarter": quarter,
                "half": half,
                "game_minute": game_minute,
                "bk_implied_prob": impl_prob,
                "bk_moneyline": float(bk_ml),
                "bk_spread": float(c["bk_spread"]) if c.get("bk_spread") and c["bk_spread"] not in ("", "--") else None,
                "bk_source": c.get("bk_name", ""),
                "spread_role": (c.get("spread_role") or "").lower(),
                "pregame_ml_prob": pregame_ml_prob,
                "pregame_spread": pregame_spread,
                "home_score": c.get("home_score_at_fire", 0),
                "away_score": c.get("away_score_at_fire", 0),
                "predicted_team": predicted_team,
                "home_team": home_team,
                "winner": winner,
                "correct": c.get("correct"),
                # Game-end stats (proxy for in-game trends)
                "pred_completion": pred_stats.get("completionRate"),
                "pred_possession": pred_stats.get("possession"),
                "pred_run_metres": pred_stats.get("allRunMetres"),
                "pred_tackles": pred_stats.get("tacklesMade"),
                "pred_missed_tackles": pred_stats.get("missedTackles"),
                "pred_errors": pred_stats.get("errors"),
                "pred_penalties": pred_stats.get("penaltiesConceded"),
                "pred_line_breaks": pred_stats.get("lineBreaks"),
                "pred_offloads": pred_stats.get("offloads"),
                "pred_tackle_breaks": pred_stats.get("tackleBreaks"),
                "opp_errors": opp_stats.get("errors"),
                "opp_penalties": opp_stats.get("penaltiesConceded"),
            }
            records.append(rec)

    return records, game_count


def _load_spread_data():
    """Load PW calls with BK spread from NRL game history.

    Separate from load_data() because spread records don't require bk_moneyline.
    Returns list of dicts with: margin, quarter, half, game_minute, bk_spread, pregame_spread.

    Note: live_spread IS bk_spread in NRL (100% identical) — no ESPN comparison.
    """
    records = []
    path = "game_history.json"
    if not os.path.exists(path):
        return records

    with open(path) as f:
        games = json.load(f)

    for g in games:
        for c in g.get("predicted_winner_calls", []):
            bk_sp = c.get("bk_spread")
            margin = c.get("score_margin_at_fire")
            quarter = c.get("quarter")
            if not bk_sp or bk_sp in ("", "--") or margin is None or not quarter:
                continue
            try:
                sp = float(bk_sp)
            except (TypeError, ValueError):
                continue

            pregame_spread = None
            raw_sp = c.get("spread")
            if raw_sp:
                try:
                    pregame_spread = float(raw_sp)
                except (TypeError, ValueError):
                    pass

            records.append({
                "margin": margin,
                "quarter": quarter,
                "half": c.get("half"),
                "game_minute": c.get("game_minute"),
                "bk_spread": sp,
                "pregame_spread": pregame_spread,
            })
    return records


# ── Analysis Functions ─────────────────────────────────────────────────

MARGIN_BUCKETS = [
    (-99, -15, "-15+"),
    (-14, -10, "-14 to -10"),
    (-9, -5, "-9 to -5"),
    (-4, -1, "-4 to -1"),
    (0, 4, "0 to +4"),
    (5, 9, "+5 to +9"),
    (10, 14, "+10 to +14"),
    (15, 99, "+15+"),
]

QUARTER_LIST = ["Q1", "Q2", "Q3", "Q4"]

# Game minute buckets (NRL: 0-80 mins, two 40-min halves)
MINUTE_BUCKETS = [
    (0, 20, "0-20"),
    (21, 40, "21-40"),
    (41, 60, "41-60"),
    (61, 80, "61-80"),
]

STAT_FIELDS = [
    ("pred_completion", "Completion%"),
    ("pred_possession", "Possession%"),
    ("pred_run_metres", "Run Metres"),
    ("pred_tackles", "Tackles Made"),
    ("pred_missed_tackles", "Missed Tackles"),
    ("pred_errors", "Errors"),
    ("pred_penalties", "Penalties"),
    ("pred_line_breaks", "Line Breaks"),
    ("pred_offloads", "Offloads"),
    ("pred_tackle_breaks", "Tackle Breaks"),
    ("opp_errors", "Opp Errors"),
    ("opp_penalties", "Opp Penalties"),
]


def _margin_bucket(margin):
    for lo, hi, label in MARGIN_BUCKETS:
        if lo <= margin <= hi:
            return label
    return "?"


def _minute_bucket(game_minute):
    if game_minute is None:
        return None
    for lo, hi, label in MINUTE_BUCKETS:
        if lo <= game_minute <= hi:
            return label
    return "80+"


def analyse_coverage(records, game_count):
    """Section 1: Data coverage summary.

    Returns dict with:
      - total_games: total game count
      - pw_calls_with_bk: count of PW calls with BK odds
      - by_quarter: {quarter: count}
      - by_half: {half: count}
      - by_source: {bookmaker: count}
    """
    by_quarter = {}
    for q in QUARTER_LIST:
        by_quarter[q] = sum(1 for r in records if r["quarter"] == q)

    by_half = {}
    for h in ["H1", "H2"]:
        by_half[h] = sum(1 for r in records if r.get("half") == h)

    by_source = {}
    for r in records:
        src = r["bk_source"] or "unknown"
        by_source[src] = by_source.get(src, 0) + 1
    by_source = dict(sorted(by_source.items(), key=lambda x: -x[1]))

    return {
        "total_games": game_count,
        "pw_calls_with_bk": len(records),
        "by_quarter": by_quarter,
        "by_half": by_half,
        "by_source": by_source,
    }


def analyse_margin(records):
    """Section 2: Margin -> BK implied probability relationship.

    NRL extension: also analyses by game_minute buckets alongside quarter.

    Returns dict with:
      - simple_r2: R2 of margin alone -> implied prob
      - margin_quarter_r2: R2 of margin + quarter dummies -> implied prob
      - heatmap: {quarter: {bucket: {mean_prob, count, actual_win_rate}}}
      - minute_heatmap: {minute_bucket: {margin_bucket: {mean_prob, count}}}
      - scatter_data: list of (margin, implied_prob, quarter, game_minute)
    """
    margins = [r["margin"] for r in records]
    probs = [r["bk_implied_prob"] for r in records]
    _, _, simple_r2 = _linear_regression(margins, probs)

    # Multi regression: margin + quarter dummies -> implied prob
    X = []
    for r in records:
        row = [float(r["margin"])]
        for q in QUARTER_LIST[1:]:  # Q1 is reference
            row.append(1.0 if r["quarter"] == q else 0.0)
        X.append(row)
    margin_quarter_r2 = _multi_regression_r2(X, probs)

    # Heatmap: quarter x margin bucket -> mean implied prob + actual win rate
    heatmap = {}
    for q in QUARTER_LIST:
        heatmap[q] = {}
        for _, _, label in MARGIN_BUCKETS:
            heatmap[q][label] = {"probs": [], "wins": 0, "total": 0}

    for r in records:
        q = r["quarter"]
        bucket = _margin_bucket(r["margin"])
        if q in heatmap and bucket in heatmap[q]:
            heatmap[q][bucket]["probs"].append(r["bk_implied_prob"])
            if r.get("correct") is True:
                heatmap[q][bucket]["wins"] += 1
            if r.get("correct") is not None:
                heatmap[q][bucket]["total"] += 1

    for q in heatmap:
        for bucket in heatmap[q]:
            cell = heatmap[q][bucket]
            cell["count"] = len(cell["probs"])
            cell["mean_prob"] = statistics.mean(cell["probs"]) if cell["probs"] else None
            cell["actual_win_rate"] = cell["wins"] / cell["total"] if cell["total"] > 0 else None
            del cell["probs"]

    # Minute heatmap: minute_bucket x margin_bucket -> mean implied prob
    minute_heatmap = {}
    for _, _, mlabel in MINUTE_BUCKETS:
        minute_heatmap[mlabel] = {}
        for _, _, blabel in MARGIN_BUCKETS:
            minute_heatmap[mlabel][blabel] = {"probs": []}
    minute_heatmap["80+"] = {blabel: {"probs": []} for _, _, blabel in MARGIN_BUCKETS}

    for r in records:
        mbucket = _minute_bucket(r.get("game_minute"))
        bucket = _margin_bucket(r["margin"])
        if mbucket and mbucket in minute_heatmap and bucket in minute_heatmap[mbucket]:
            minute_heatmap[mbucket][bucket]["probs"].append(r["bk_implied_prob"])

    for mb in minute_heatmap:
        for bucket in minute_heatmap[mb]:
            cell = minute_heatmap[mb][bucket]
            probs = cell["probs"]
            cell["count"] = len(probs)
            cell["mean_prob"] = statistics.mean(probs) if probs else None
            del cell["probs"]

    scatter_data = [(r["margin"], r["bk_implied_prob"], r["quarter"], r.get("game_minute")) for r in records]

    return {
        "simple_r2": simple_r2,
        "margin_quarter_r2": margin_quarter_r2,
        "heatmap": heatmap,
        "minute_heatmap": minute_heatmap,
        "scatter_data": scatter_data,
        "total_records": len(records),
    }


def analyse_time(records):
    """Section 3: Game minute effect on BK odds.

    NRL uses game_minute (continuous 0-80+) instead of a clock string.
    R² improvement of adding game_minute to margin+quarter.

    Returns dict with:
      - has_minute_data: bool (enough records with game_minute)
      - minute_r2_improvement: R2 gain when adding game_minute to margin+quarter
      - quarter_effect: {quarter: {margin_bucket: {mean_prob, count}}}
      - records_with_minute: count
    """
    with_minute = [r for r in records if r.get("game_minute") is not None]
    has_minute_data = len(with_minute) >= 30

    minute_r2_improvement = None
    if has_minute_data:
        X_no_min = []
        y = []
        for r in with_minute:
            row = [float(r["margin"])]
            for q in QUARTER_LIST[1:]:
                row.append(1.0 if r["quarter"] == q else 0.0)
            X_no_min.append(row)
            y.append(r["bk_implied_prob"])
        r2_no_min = _multi_regression_r2(X_no_min, y)

        X_with_min = []
        for i, r in enumerate(with_minute):
            X_with_min.append(X_no_min[i] + [float(r["game_minute"])])
        r2_with_min = _multi_regression_r2(X_with_min, y)

        if r2_no_min is not None and r2_with_min is not None:
            minute_r2_improvement = r2_with_min - r2_no_min

    # Quarter effect: same margin bucket at different quarters
    quarter_effect = {}
    for q in QUARTER_LIST:
        quarter_effect[q] = {}
        q_recs = [r for r in records if r["quarter"] == q]
        for _, _, label in MARGIN_BUCKETS:
            bucket_recs = [r for r in q_recs if _margin_bucket(r["margin"]) == label]
            if bucket_recs:
                quarter_effect[q][label] = {
                    "mean_prob": statistics.mean(r["bk_implied_prob"] for r in bucket_recs),
                    "count": len(bucket_recs),
                }

    return {
        "has_minute_data": has_minute_data,
        "records_with_minute": len(with_minute),
        "minute_r2_improvement": minute_r2_improvement,
        "quarter_effect": quarter_effect,
    }


def analyse_pregame(records):
    """Section 4: Pregame moneyline influence on BK live odds.

    Uses the pregame moneyline implied probability from the PW call.

    Returns dict with:
      - has_pregame_data: bool
      - pregame_r2_improvement: R2 gain when adding pregame ML prob to margin+quarter
      - role_split: {favorite: {total, by_bucket}, underdog: {total, by_bucket}}
    """
    with_pregame = [r for r in records if r.get("pregame_ml_prob") is not None]
    has_pregame_data = len(with_pregame) >= 30

    pregame_r2_improvement = None
    if has_pregame_data:
        X_base = []
        X_plus = []
        y = []
        for r in with_pregame:
            row = [float(r["margin"])]
            for q in QUARTER_LIST[1:]:
                row.append(1.0 if r["quarter"] == q else 0.0)
            X_base.append(row)
            X_plus.append(row + [r["pregame_ml_prob"]])
            y.append(r["bk_implied_prob"])

        r2_base = _multi_regression_r2(X_base, y)
        r2_plus = _multi_regression_r2(X_plus, y)

        if r2_base is not None and r2_plus is not None:
            pregame_r2_improvement = r2_plus - r2_base

    # Role split: favorites vs underdogs at similar margins
    role_split = {}
    for role in ("favorite", "underdog"):
        role_recs = [r for r in records if r["spread_role"] == role]
        if role_recs:
            by_bucket = {}
            for _, _, label in MARGIN_BUCKETS:
                bucket_recs = [r for r in role_recs if _margin_bucket(r["margin"]) == label]
                if bucket_recs:
                    by_bucket[label] = {
                        "mean_prob": statistics.mean(r["bk_implied_prob"] for r in bucket_recs),
                        "count": len(bucket_recs),
                    }
            role_split[role] = {"total": len(role_recs), "by_bucket": by_bucket}

    return {
        "has_pregame_data": has_pregame_data,
        "records_with_pregame": len(with_pregame),
        "pregame_r2_improvement": pregame_r2_improvement,
        "role_split": role_split,
    }


def analyse_stats(records):
    """Section 5: Box score stats correlation with BK odds residuals.

    Uses NRL-specific stat fields. Computes residuals from margin+quarter regression,
    then correlates each stat field with the residual to measure incremental signal.

    Returns dict with:
      - stats_r2_improvement: R2 gain when adding all stats to margin+quarter
      - per_stat_correlation: [{field, label, correlation, count}] sorted by |correlation|
      - r2_base: R2 of the base margin+quarter model
    """
    X_base = []
    y = []
    valid_records = []
    for r in records:
        row = [float(r["margin"])]
        for q in QUARTER_LIST[1:]:
            row.append(1.0 if r["quarter"] == q else 0.0)
        X_base.append(row)
        y.append(r["bk_implied_prob"])
        valid_records.append(r)

    r2_base = _multi_regression_r2(X_base, y)

    # Solve for beta to get residuals
    n = len(y)
    k1 = len(X_base[0]) + 1
    X = [[1.0] + row for row in X_base]
    XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k1)] for a in range(k1)]
    Xty = [sum(X[i][a] * y[i] for i in range(n)) for a in range(k1)]
    aug = [XtX[r][:] + [Xty[r]] for r in range(k1)]
    for col in range(k1):
        max_row = max(range(col, k1), key=lambda r2: abs(aug[r2][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]
        if abs(aug[col][col]) < 1e-12:
            return {"stats_r2_improvement": None, "per_stat_correlation": [], "r2_base": r2_base}
        for row in range(k1):
            if row == col:
                continue
            factor = aug[row][col] / aug[col][col]
            for j in range(k1 + 1):
                aug[row][j] -= factor * aug[col][j]
    beta = [aug[r][k1] / aug[r][r] for r in range(k1)]

    residuals = []
    for i in range(n):
        pred = sum(beta[j] * X[i][j] for j in range(k1))
        residuals.append(y[i] - pred)

    # Per-stat correlation with residuals
    per_stat = []
    for field, label in STAT_FIELDS:
        stat_vals = []
        res_vals = []
        for i, r in enumerate(valid_records):
            v = r.get(field)
            if v is not None:
                try:
                    stat_vals.append(float(v))
                    res_vals.append(residuals[i])
                except (TypeError, ValueError):
                    pass
        if len(stat_vals) >= 20:
            slope, _, r2 = _linear_regression(stat_vals, res_vals)
            corr = math.sqrt(r2) if r2 and r2 > 0 else 0.0
            if slope and slope < 0:
                corr = -corr
            per_stat.append({"field": field, "label": label, "correlation": corr, "count": len(stat_vals)})

    per_stat.sort(key=lambda x: abs(x["correlation"]), reverse=True)

    # R2 improvement when adding all stats
    stats_r2_improvement = None
    X_plus = []
    y_plus = []
    for i, r in enumerate(valid_records):
        stat_row = []
        all_present = True
        for field, _ in STAT_FIELDS:
            v = r.get(field)
            if v is not None:
                try:
                    stat_row.append(float(v))
                except (TypeError, ValueError):
                    all_present = False
                    break
            else:
                all_present = False
                break
        if all_present:
            X_plus.append(X_base[i] + stat_row)
            y_plus.append(y[i])

    if len(X_plus) >= 30:
        r2_plus = _multi_regression_r2(X_plus, y_plus)
        if r2_base is not None and r2_plus is not None:
            stats_r2_improvement = r2_plus - r2_base

    return {
        "stats_r2_improvement": stats_r2_improvement,
        "per_stat_correlation": per_stat,
        "r2_base": r2_base,
    }


def analyse_spread(spread_records):
    """Section 9: BK spread prediction analysis.

    NRL note: live_spread IS bk_spread (100% identical) — no ESPN comparison.
    Pregame spread is from the PW call's spread field.

    Returns dict with:
      - total: record count
      - margin_r2: R2 of margin alone -> spread
      - margin_quarter_r2: R2 of margin + quarter -> spread
      - full_r2: R2 of margin + quarter + pregame -> spread
      - pregame_r2_improvement: delta from adding pregame
      - slope, intercept: from simple margin -> spread regression
      - heatmap: {quarter: {margin_bucket: {mean_spread, count}}}
    """
    if len(spread_records) < 10:
        return {"total": len(spread_records)}

    margins = [r["margin"] for r in spread_records]
    spreads = [r["bk_spread"] for r in spread_records]

    slope, intercept, margin_r2 = _linear_regression(margins, spreads)

    # Margin + quarter
    X_mq = []
    for r in spread_records:
        row = [float(r["margin"])]
        for q in QUARTER_LIST[1:]:
            row.append(1.0 if r["quarter"] == q else 0.0)
        X_mq.append(row)
    margin_quarter_r2 = _multi_regression_r2(X_mq, spreads)

    # + Pregame spread
    with_pregame = [r for r in spread_records if r.get("pregame_spread") is not None]
    full_r2 = None
    pregame_r2_improvement = None
    if len(with_pregame) >= 30:
        X_base = []
        X_plus = []
        y_p = []
        for r in with_pregame:
            row = [float(r["margin"])]
            for q in QUARTER_LIST[1:]:
                row.append(1.0 if r["quarter"] == q else 0.0)
            X_base.append(row)
            X_plus.append(row + [r["pregame_spread"]])
            y_p.append(r["bk_spread"])
        r2_base = _multi_regression_r2(X_base, y_p)
        full_r2 = _multi_regression_r2(X_plus, y_p)
        if r2_base is not None and full_r2 is not None:
            pregame_r2_improvement = full_r2 - r2_base

    # Heatmap: quarter x margin bucket -> mean spread
    heatmap = {}
    for q in QUARTER_LIST:
        heatmap[q] = {}
        q_recs = [r for r in spread_records if r["quarter"] == q]
        for lo, hi, label in MARGIN_BUCKETS:
            bucket_recs = [r for r in q_recs if lo <= r["margin"] <= hi]
            if bucket_recs:
                heatmap[q][label] = {
                    "mean_spread": statistics.mean(r["bk_spread"] for r in bucket_recs),
                    "count": len(bucket_recs),
                }

    # Also compute minute_bucket heatmap for NRL
    minute_heatmap = {}
    all_minute_labels = [lbl for _, _, lbl in MINUTE_BUCKETS] + ["80+"]
    for mlabel in all_minute_labels:
        minute_heatmap[mlabel] = {}
        m_recs = [r for r in spread_records if _minute_bucket(r.get("game_minute")) == mlabel]
        for lo, hi, blabel in MARGIN_BUCKETS:
            b_recs = [r for r in m_recs if lo <= r["margin"] <= hi]
            if b_recs:
                minute_heatmap[mlabel][blabel] = {
                    "mean_spread": statistics.mean(r["bk_spread"] for r in b_recs),
                    "count": len(b_recs),
                }

    return {
        "total": len(spread_records),
        "margin_r2": margin_r2,
        "margin_quarter_r2": margin_quarter_r2,
        "full_r2": full_r2,
        "pregame_r2_improvement": pregame_r2_improvement,
        "slope": slope,
        "intercept": intercept,
        "heatmap": heatmap,
        "minute_heatmap": minute_heatmap,
    }


# ── HTML Rendering ─────────────────────────────────────────────────────

OUTPUT_FILE = "bk_odds_analysis_report.html"

# NRL accent colour
NRL_GREEN = "#10b981"


def _fmt_pct(val, decimals=1):
    if val is None:
        return "\u2014"
    return f"{val * 100:.{decimals}f}%"


def _fmt_r2(val):
    if val is None:
        return "\u2014"
    return f"{val:.4f}"


def _prob_color(prob):
    if prob is None:
        return "transparent"
    if prob >= 0.7:
        g = int(120 + (prob - 0.7) / 0.3 * 135)
        return f"rgba(0,{min(g,255)},0,0.25)"
    elif prob >= 0.4:
        return "rgba(200,200,0,0.15)"
    else:
        r = int(120 + (0.4 - prob) / 0.4 * 135)
        return f"rgba({min(r,255)},0,0,0.25)"


def render_html(coverage, margin_results, time_results, pregame_results,
                stats_results, records, spread_results=None):
    """Render all analysis results into a self-contained HTML report."""
    from datetime import datetime, timezone

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    quarters = QUARTER_LIST

    html_parts = []
    html_parts.append(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>BK Live Odds Analysis \u2014 NRL</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
       background: #0f1117; color: #e0e0e0; margin: 0; padding: 16px; font-size: 14px; }}
h1 {{ color: {NRL_GREEN}; margin-bottom: 4px; font-size: 1.4rem; }}
h2 {{ color: {NRL_GREEN}; border-bottom: 1px solid #333; padding-bottom: 4px; margin-top: 28px; font-size: 1.1rem; }}
h3 {{ color: #94a3b8; font-size: 0.95rem; margin-top: 16px; }}
.subtitle {{ color: #94a3b8; font-size: 0.82rem; margin-bottom: 16px; }}
table {{ border-collapse: collapse; margin: 8px 0; font-size: 0.82rem; }}
th, td {{ padding: 4px 10px; border: 1px solid #333; text-align: center; }}
th {{ background: #1a1d2e; color: {NRL_GREEN}; font-weight: 600; }}
td {{ background: #12141e; }}
.metric {{ display: inline-block; background: #1a1d2e; border: 1px solid #333; border-radius: 6px;
           padding: 8px 14px; margin: 4px; text-align: center; }}
.metric-value {{ font-size: 1.3rem; font-weight: 700; color: {NRL_GREEN}; }}
.metric-label {{ font-size: 0.72rem; color: #94a3b8; }}
.note {{ color: #94a3b8; font-size: 0.78rem; font-style: italic; margin: 4px 0; }}
.good {{ color: #22c55e; }}
.warn {{ color: #eab308; }}
.bad {{ color: #ef4444; }}
.finding {{ background: #1a1d2e; border-left: 3px solid {NRL_GREEN}; padding: 8px 12px; margin: 8px 0;
            font-size: 0.85rem; border-radius: 0 4px 4px 0; }}
</style></head><body>
<h1>BK Live Odds Analysis Report \u2014 NRL</h1>
<div class="subtitle">Generated {now_utc} | NRL Monitor #235</div>
""")

    # ── Section 1: Coverage ──
    html_parts.append("<h2>1. Data Coverage Summary</h2>")
    total = coverage["total_games"]
    pw_bk = coverage["pw_calls_with_bk"]
    html_parts.append(f'<div class="metric"><div class="metric-value">{total:,}</div><div class="metric-label">Total Games</div></div>')
    html_parts.append(f'<div class="metric"><div class="metric-value">{pw_bk:,}</div><div class="metric-label">PW Calls w/ BK Odds</div></div>')

    # By quarter
    bq = coverage.get("by_quarter", {})
    bh = coverage.get("by_half", {})
    if bq:
        html_parts.append("<table><tr><th>Period</th>")
        for q in quarters:
            html_parts.append(f"<th>{q}</th>")
        for h in ["H1", "H2"]:
            html_parts.append(f"<th>{h}</th>")
        html_parts.append("</tr><tr><td>Count</td>")
        for q in quarters:
            html_parts.append(f"<td>{bq.get(q, 0)}</td>")
        for h in ["H1", "H2"]:
            html_parts.append(f"<td>{bh.get(h, 0)}</td>")
        html_parts.append("</tr></table>")

    # By source
    bs = coverage.get("by_source", {})
    if bs:
        html_parts.append("<table><tr><th>BK Source</th><th>Count</th></tr>")
        for src, cnt in bs.items():
            html_parts.append(f"<tr><td>{src}</td><td>{cnt}</td></tr>")
        html_parts.append("</table>")

    # ── Section 2: Margin ──
    html_parts.append("<h2>2. Margin \u2192 BK Implied Probability</h2>")
    html_parts.append(f'<div class="metric"><div class="metric-value">{_fmt_r2(margin_results["simple_r2"])}</div><div class="metric-label">R\u00b2 (Margin only)</div></div>')
    html_parts.append(f'<div class="metric"><div class="metric-value">{_fmt_r2(margin_results["margin_quarter_r2"])}</div><div class="metric-label">R\u00b2 (Margin + Quarter)</div></div>')

    # Quarter x margin heatmap
    html_parts.append("<h3>Heatmap: Quarter \u00d7 Margin \u2192 BK Implied Probability (Actual Win Rate)</h3>")
    html_parts.append("<table><tr><th>Margin</th>")
    for q in quarters:
        html_parts.append(f"<th>{q}</th>")
    html_parts.append("</tr>")
    heatmap = margin_results["heatmap"]
    for _, _, label in MARGIN_BUCKETS:
        html_parts.append(f"<tr><td style='text-align:left;font-weight:600'>{label}</td>")
        for q in quarters:
            cell = heatmap.get(q, {}).get(label, {})
            mean_p = cell.get("mean_prob")
            win_r = cell.get("actual_win_rate")
            count = cell.get("count", 0)
            bg = _prob_color(mean_p)
            p_str = _fmt_pct(mean_p) if mean_p is not None else "\u2014"
            w_str = f" ({_fmt_pct(win_r)})" if win_r is not None else ""
            html_parts.append(f'<td style="background:{bg}" title="n={count}">{p_str}{w_str}</td>')
        html_parts.append("</tr>")
    html_parts.append("</table>")
    html_parts.append('<div class="note">Format: BK Implied Prob (Actual Win Rate). Hover for sample size.</div>')

    # Game minute heatmap (NRL-specific)
    minute_heatmap = margin_results.get("minute_heatmap", {})
    all_minute_labels = [lbl for _, _, lbl in MINUTE_BUCKETS] + ["80+"]
    html_parts.append("<h3>Heatmap: Game Minute \u00d7 Margin \u2192 BK Implied Probability</h3>")
    html_parts.append('<div class="note">NRL: 2 \u00d7 40-min halves. Minute 0-40 = H1, 41-80 = H2.</div>')
    html_parts.append("<table><tr><th>Margin</th>")
    for ml in all_minute_labels:
        html_parts.append(f"<th>{ml} min</th>")
    html_parts.append("</tr>")
    for _, _, blabel in MARGIN_BUCKETS:
        html_parts.append(f"<tr><td style='text-align:left;font-weight:600'>{blabel}</td>")
        for ml in all_minute_labels:
            cell = minute_heatmap.get(ml, {}).get(blabel, {})
            mean_p = cell.get("mean_prob")
            count = cell.get("count", 0)
            bg = _prob_color(mean_p)
            html_parts.append(f'<td style="background:{bg}" title="n={count}">{_fmt_pct(mean_p)}</td>')
        html_parts.append("</tr>")
    html_parts.append("</table>")

    # ── Section 3: Time ──
    html_parts.append("<h2>3. Game Minute Effect</h2>")
    html_parts.append(f'<div class="metric"><div class="metric-value">{time_results["records_with_minute"]}</div><div class="metric-label">Records with Minute Data</div></div>')
    impr = time_results.get("minute_r2_improvement")
    if impr is not None:
        cls = "good" if impr > 0.01 else "warn" if impr > 0 else "bad"
        html_parts.append(f'<div class="metric"><div class="metric-value {cls}">{impr:+.4f}</div><div class="metric-label">R\u00b2 Improvement (Minute)</div></div>')
        html_parts.append(f'<div class="finding">Adding game_minute to margin+quarter {"meaningfully improves" if impr > 0.01 else "marginally improves" if impr > 0 else "does not improve"} prediction (\u0394R\u00b2 = {impr:+.4f}).</div>')
    else:
        html_parts.append('<div class="finding">Insufficient minute data for analysis.</div>')

    # Quarter effect table
    qe = time_results.get("quarter_effect", {})
    if qe:
        html_parts.append("<h3>Same Margin, Different Quarter \u2192 BK Implied Prob</h3>")
        html_parts.append("<table><tr><th>Margin</th>")
        for q in quarters:
            html_parts.append(f"<th>{q}</th>")
        html_parts.append("</tr>")
        for _, _, label in MARGIN_BUCKETS:
            html_parts.append(f"<tr><td style='text-align:left;font-weight:600'>{label}</td>")
            for q in quarters:
                cell = qe.get(q, {}).get(label, {})
                mp = cell.get("mean_prob")
                cnt = cell.get("count", 0)
                html_parts.append(f'<td style="background:{_prob_color(mp)}" title="n={cnt}">{_fmt_pct(mp)}</td>')
            html_parts.append("</tr>")
        html_parts.append("</table>")

    # ── Section 4: Pregame ──
    html_parts.append("<h2>4. Pregame Moneyline Influence</h2>")
    html_parts.append(f'<div class="metric"><div class="metric-value">{pregame_results["records_with_pregame"]}</div><div class="metric-label">Records with Pregame ML</div></div>')
    pi = pregame_results.get("pregame_r2_improvement")
    if pi is not None:
        cls = "good" if pi > 0.01 else "warn" if pi > 0 else "bad"
        html_parts.append(f'<div class="metric"><div class="metric-value {cls}">{pi:+.4f}</div><div class="metric-label">R\u00b2 Improvement (Pregame)</div></div>')
        html_parts.append(f'<div class="finding">Pregame moneyline {"significantly adds" if pi > 0.01 else "marginally adds" if pi > 0 else "does not add"} predictive power beyond margin+quarter (\u0394R\u00b2 = {pi:+.4f}).</div>')

    rs = pregame_results.get("role_split", {})
    if "favorite" in rs and "underdog" in rs:
        html_parts.append("<h3>Favorites vs Underdogs at Same Margin</h3>")
        html_parts.append("<table><tr><th>Margin</th><th>Favorites</th><th>Underdogs</th><th>Diff</th></tr>")
        fav_bk = rs["favorite"].get("by_bucket", {})
        dog_bk = rs["underdog"].get("by_bucket", {})
        for _, _, label in MARGIN_BUCKETS:
            f_cell = fav_bk.get(label, {})
            d_cell = dog_bk.get(label, {})
            fp = f_cell.get("mean_prob")
            dp = d_cell.get("mean_prob")
            diff = (fp - dp) if fp is not None and dp is not None else None
            fn = f_cell.get("count", 0)
            dn = d_cell.get("count", 0)
            html_parts.append(f'<tr><td style="text-align:left;font-weight:600">{label}</td>')
            html_parts.append(f'<td title="n={fn}">{_fmt_pct(fp)}</td>')
            html_parts.append(f'<td title="n={dn}">{_fmt_pct(dp)}</td>')
            html_parts.append(f'<td>{_fmt_pct(diff) if diff is not None else "\u2014"}</td></tr>')
        html_parts.append("</table>")

    # ── Section 5: Stats ──
    html_parts.append("<h2>5. Game Stats Impact</h2>")
    html_parts.append('<div class="note">NRL stats: completion%, possession%, run metres, tackles, errors, penalties, line breaks, offloads, tackle breaks.</div>')
    si = stats_results.get("stats_r2_improvement")
    if si is not None:
        cls = "good" if si > 0.01 else "warn" if si > 0 else "bad"
        html_parts.append(f'<div class="metric"><div class="metric-value {cls}">{si:+.4f}</div><div class="metric-label">R\u00b2 Improvement (All Stats)</div></div>')
        html_parts.append(f'<div class="finding">Game stats {"significantly improve" if si > 0.01 else "marginally improve" if si > 0 else "do not improve"} predictions beyond margin+quarter (\u0394R\u00b2 = {si:+.4f}).</div>')

    psc = stats_results.get("per_stat_correlation", [])
    if psc:
        html_parts.append("<h3>Feature Correlation with BK Odds Residuals</h3>")
        html_parts.append('<div class="note">Correlation with residuals after removing margin+quarter effect. Higher |r| = more incremental signal.</div>')
        html_parts.append("<table><tr><th>Stat</th><th>Correlation (r)</th><th>Count</th></tr>")
        for s in psc:
            r_val = s["correlation"]
            cls = "good" if abs(r_val) > 0.1 else ""
            html_parts.append(f'<tr><td style="text-align:left">{s["label"]}</td><td class="{cls}">{r_val:+.4f}</td><td>{s["count"]}</td></tr>')
        html_parts.append("</table>")

    # ── Section 7: Model Recommendation ──
    html_parts.append("<h2>7. Model Recommendation</h2>")
    html_parts.append('<div class="note">NRL uses decimal odds \u2014 synthetic odds formulas output in decimal format.</div>')

    r2_mq = margin_results.get("margin_quarter_r2") or 0
    min_gain = time_results.get("minute_r2_improvement") or 0
    pregame_gain = pregame_results.get("pregame_r2_improvement") or 0
    stats_gain = stats_results.get("stats_r2_improvement") or 0

    features = ["margin", "quarter"]
    r2_total = r2_mq
    if min_gain > 0.005:
        features.append("game_minute")
        r2_total += min_gain
    if pregame_gain > 0.005:
        features.append("pregame_moneyline")
        r2_total += pregame_gain
    if stats_gain > 0.01:
        features.append("game_stats")
        r2_total += stats_gain

    html_parts.append(f'<div class="finding"><strong>Recommended features:</strong> {", ".join(features)}<br>')
    html_parts.append(f'<strong>Combined R\u00b2:</strong> ~{r2_total:.4f}<br>')

    if r2_mq > 0.6:
        html_parts.append("Margin + quarter alone explain most of the variance. A simple regression or lookup table may suffice.")
    elif r2_mq > 0.4:
        html_parts.append("Margin + quarter explain moderate variance. Additional features provide meaningful improvement. Consider gradient boosted model.")
    else:
        html_parts.append("Margin + quarter explain limited variance. BK pricing likely incorporates factors not captured in our data. More data collection recommended.")
    html_parts.append("</div>")

    html_parts.append("<h3>Key Findings Summary</h3><ul>")
    html_parts.append(f"<li>Margin alone \u2192 R\u00b2 = {_fmt_r2(margin_results['simple_r2'])}</li>")
    html_parts.append(f"<li>Margin + Quarter \u2192 R\u00b2 = {_fmt_r2(margin_results['margin_quarter_r2'])}</li>")
    if min_gain is not None:
        html_parts.append(f"<li>+ Game minute \u2192 \u0394R\u00b2 = {min_gain:+.4f}</li>")
    if pregame_gain is not None:
        html_parts.append(f"<li>+ Pregame moneyline \u2192 \u0394R\u00b2 = {pregame_gain:+.4f}</li>")
    if stats_gain is not None:
        html_parts.append(f"<li>+ Game stats \u2192 \u0394R\u00b2 = {stats_gain:+.4f}</li>")
    html_parts.append("</ul>")

    # Synthetic odds note
    html_parts.append('<div class="finding"><strong>Synthetic odds formula (NRL decimal):</strong> '
                      '<code>decimal_odds = 1 / implied_prob</code><br>'
                      'Where implied_prob is derived from the regression model. '
                      'For example, if margin=+6 in Q3 predicts 70% win probability, '
                      'synthetic decimal odds = 1/0.70 = 1.43. '
                      'NRL uses decimal odds (e.g. 1.65), not American (-154).</div>')

    # ── Section 9: Spread Analysis ──
    if spread_results and spread_results.get("total", 0) >= 10:
        html_parts.append("<h2>9. BK Spread Prediction</h2>")
        html_parts.append('<div class="note">NRL: live_spread IS bk_spread (identical) \u2014 no ESPN comparison possible. Pregame spread from PW call field.</div>')
        html_parts.append(f'<div class="metric"><div class="metric-value">{spread_results["total"]:,}</div><div class="metric-label">PW Calls with BK Spread</div></div>')
        html_parts.append(f'<div class="metric"><div class="metric-value">{_fmt_r2(spread_results.get("margin_r2"))}</div><div class="metric-label">R\u00b2 (Margin only)</div></div>')
        html_parts.append(f'<div class="metric"><div class="metric-value">{_fmt_r2(spread_results.get("margin_quarter_r2"))}</div><div class="metric-label">R\u00b2 (Margin + Quarter)</div></div>')
        full_r2 = spread_results.get("full_r2")
        if full_r2 is not None:
            html_parts.append(f'<div class="metric"><div class="metric-value good">{_fmt_r2(full_r2)}</div><div class="metric-label">R\u00b2 (+ Pregame Spread)</div></div>')

        # Regression formula
        slope = spread_results.get("slope")
        intercept = spread_results.get("intercept")
        if slope is not None:
            html_parts.append(f'<div class="finding"><strong>Formula:</strong> <code>BK_spread \u2248 {slope:.3f} \u00d7 margin + {intercept:+.2f}</code><br>'
                              f'Each point of in-game margin corresponds to ~{abs(slope):.2f} points of spread adjustment. '
                              f'Slope near -1.0 means BK spreads closely track the live score margin.</div>')

        pi = spread_results.get("pregame_r2_improvement")
        if pi is not None:
            html_parts.append(f'<div class="finding"><strong>Pregame spread influence:</strong> \u0394R\u00b2 = {pi:+.4f}. '
                              f'{"Significant \u2014 BK adjusts live spread relative to pregame expectations." if pi > 0.01 else "Marginal."}</div>')

        # Heatmap: quarter x margin
        heatmap = spread_results.get("heatmap", {})
        if heatmap:
            html_parts.append("<h3>Quarter \u00d7 Margin \u2192 Mean BK Spread</h3>")
            html_parts.append("<table><tr><th>Margin</th>")
            for q in quarters:
                html_parts.append(f"<th>{q}</th>")
            html_parts.append("</tr>")
            for _, _, label in MARGIN_BUCKETS:
                html_parts.append(f'<tr><td style="text-align:left;font-weight:600">{label}</td>')
                for q in quarters:
                    cell = heatmap.get(q, {}).get(label, {})
                    ms = cell.get("mean_spread")
                    cnt = cell.get("count", 0)
                    if ms is not None:
                        color = "#22c55e" if ms < 0 else "#ef4444" if ms > 0 else "#e0e0e0"
                        html_parts.append(f'<td style="color:{color}" title="n={cnt}">{ms:+.1f}</td>')
                    else:
                        html_parts.append(f'<td>\u2014</td>')
                html_parts.append("</tr>")
            html_parts.append("</table>")
            html_parts.append('<div class="note">Negative = predicted team favored. Green = favored, Red = underdog.</div>')

        # Minute heatmap (NRL-specific)
        minute_heatmap = spread_results.get("minute_heatmap", {})
        all_minute_labels = [lbl for _, _, lbl in MINUTE_BUCKETS] + ["80+"]
        non_empty_mlabels = [ml for ml in all_minute_labels if any(minute_heatmap.get(ml, {}).values())]
        if non_empty_mlabels:
            html_parts.append("<h3>Game Minute \u00d7 Margin \u2192 Mean BK Spread</h3>")
            html_parts.append("<table><tr><th>Margin</th>")
            for ml in non_empty_mlabels:
                html_parts.append(f"<th>{ml} min</th>")
            html_parts.append("</tr>")
            for _, _, blabel in MARGIN_BUCKETS:
                html_parts.append(f'<tr><td style="text-align:left;font-weight:600">{blabel}</td>')
                for ml in non_empty_mlabels:
                    cell = minute_heatmap.get(ml, {}).get(blabel, {})
                    ms = cell.get("mean_spread")
                    cnt = cell.get("count", 0)
                    if ms is not None:
                        color = "#22c55e" if ms < 0 else "#ef4444" if ms > 0 else "#e0e0e0"
                        html_parts.append(f'<td style="color:{color}" title="n={cnt}">{ms:+.1f}</td>')
                    else:
                        html_parts.append(f'<td>\u2014</td>')
                html_parts.append("</tr>")
            html_parts.append("</table>")

        # Feasibility verdict
        mr2 = spread_results.get("margin_r2") or 0
        html_parts.append("<h3>Synthetic Spread Feasibility</h3>")
        html_parts.append('<div class="finding">')
        if mr2 > 0.8:
            html_parts.append(f'<strong class="good">Highly viable.</strong> Margin alone explains {mr2:.0%} of BK spread variance. ')
            html_parts.append(f'A simple linear formula (<code>spread \u2248 {slope:.2f} \u00d7 margin + pregame_adjustment</code>) ')
            html_parts.append(f'can produce realistic synthetic spreads with R\u00b2 \u2248 {full_r2:.2f}.' if full_r2 else 'can produce realistic synthetic spreads.')
        elif mr2 > 0.5:
            html_parts.append(f'<strong class="warn">Partially viable.</strong> Margin explains {mr2:.0%} \u2014 additional features needed.')
        else:
            html_parts.append(f'<strong class="bad">Needs more data.</strong> R\u00b2 = {mr2:.2f} is too low for reliable synthetic spreads.')
        html_parts.append('<br><strong>Spread is easier to simulate than moneyline</strong> because it is a linear function of margin, '
                          'not a nonlinear probability transformation.')
        html_parts.append('</div>')

    html_parts.append("</body></html>")

    return "".join(html_parts)


# ── Entry Point ────────────────────────────────────────────────────────

def generate_report(output_path=None):
    """Generate BK odds analysis HTML report."""
    output_path = output_path or OUTPUT_FILE
    records, game_count = load_data()

    if not records:
        print("No BK-odds PW call records found. Ensure game_history.json exists with PW calls.")
        return

    coverage = analyse_coverage(records, game_count)
    margin_results = analyse_margin(records)
    time_results = analyse_time(records)
    pregame_results = analyse_pregame(records)
    stats_results = analyse_stats(records)
    spread_records = _load_spread_data()
    spread_results = analyse_spread(spread_records)

    html = render_html(coverage, margin_results, time_results, pregame_results,
                       stats_results, records, spread_results)

    tmp = output_path + ".tmp"
    with open(tmp, "w") as f:
        f.write(html)
    os.replace(tmp, output_path)
    print(f"Report written to {output_path} ({len(records)} BK-odds records from {game_count} games analysed)")

    # Print key R2 findings
    print(f"  Margin only R2:          {_fmt_r2(margin_results.get('simple_r2'))}")
    print(f"  Margin + Quarter R2:     {_fmt_r2(margin_results.get('margin_quarter_r2'))}")
    print(f"  + Game minute delta:     {time_results.get('minute_r2_improvement'):+.4f}" if time_results.get('minute_r2_improvement') is not None else "  + Game minute delta:     n/a")
    print(f"  + Pregame ML delta:      {pregame_results.get('pregame_r2_improvement'):+.4f}" if pregame_results.get('pregame_r2_improvement') is not None else "  + Pregame ML delta:      n/a")
    print(f"  + Game stats delta:      {stats_results.get('stats_r2_improvement'):+.4f}" if stats_results.get('stats_r2_improvement') is not None else "  + Game stats delta:      n/a")
    if spread_results:
        print(f"  Spread margin R2:        {_fmt_r2(spread_results.get('margin_r2'))}")
        print(f"  Spread formula slope:    {spread_results.get('slope'):.3f}" if spread_results.get('slope') is not None else "  Spread formula slope:    n/a")


if __name__ == "__main__":
    generate_report()
