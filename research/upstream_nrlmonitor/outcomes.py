"""
outcomes.py — Game history storage and condition outcome analysis for NRL.

Data flow:
  monitor.py  →  append_game_record()  →  game_history.json
  server.py   →  compute_outcomes()    →  /api/outcomes
  server.py   →  /api/history
"""

import json
import re
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
WAL_FILE = os.path.join(SCRIPT_DIR, "game_history.wal")
BACKUP_ROOT = os.path.join(SCRIPT_DIR, "runtime_backups")

MIN_SAMPLE = 3


def load_history():
    """Load game history from disk.

    If the file is corrupted (JSONDecodeError), attempt auto-recovery from
    the newest backup + WAL replay (#16 Phase 2).
    """
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        # Not a list — treat as corruption
        raise ValueError("game_history.json is not a list")
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, ValueError):
        recovered = _attempt_recovery_from_backup()
        if recovered is not None:
            return recovered
        return []
    except Exception:
        return []


def _attempt_recovery_from_backup():
    """Auto-recover game_history.json from newest backup + WAL replay.

    Preserves corrupted file as game_history.json.corrupted.
    Returns recovered records list, or None if recovery fails.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("[CRITICAL] {} game_history.json is corrupted — attempting auto-recovery".format(ts),
          file=sys.stderr, flush=True)

    # Preserve corrupted file
    corrupted_path = HISTORY_FILE + ".corrupted"
    try:
        if os.path.exists(HISTORY_FILE):
            os.replace(HISTORY_FILE, corrupted_path)
            print("[CRITICAL] {} Corrupted file preserved as {}".format(ts, corrupted_path),
                  file=sys.stderr, flush=True)
    except Exception:
        pass

    # Search backups newest-first
    if not os.path.isdir(BACKUP_ROOT):
        print("[CRITICAL] {} No backup directory found — cannot recover".format(ts),
              file=sys.stderr, flush=True)
        return None

    backup_dirs = []
    for name in os.listdir(BACKUP_ROOT):
        if name.startswith("failed-run-"):
            continue
        full = os.path.join(BACKUP_ROOT, name)
        hist = os.path.join(full, "game_history.json")
        if os.path.isdir(full) and os.path.exists(hist):
            backup_dirs.append((name, hist))
    backup_dirs.sort(key=lambda x: x[0], reverse=True)  # newest first

    for backup_name, hist_path in backup_dirs:
        try:
            with open(hist_path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                continue

            # Replay WAL
            data, wal_added = replay_wal(data)
            save_history(data)

            print("[CRITICAL] {} Auto-recovered {} records from backup {} (+{} from WAL)".format(
                ts, len(data), backup_name, wal_added),
                  file=sys.stderr, flush=True)
            return data
        except Exception:
            continue

    print("[CRITICAL] {} All backups failed — cannot recover".format(ts),
          file=sys.stderr, flush=True)
    return None


def _recency_weight(days_ago, decay_lambda):
    """Compute exponential recency weight."""
    if decay_lambda <= 0:
        return 1.0
    return math.exp(-decay_lambda * days_ago)


def compute_outcomes(config=None):
    """Compute outcome statistics from game history.

    Returns dict with per-condition stats (win rates, margins, etc.)
    with optional recency weighting.
    """
    records = load_history()
    if not records:
        return {"conditions": {}, "total_games": 0}

    decay_lambda = 0.0
    if config:
        decay_lambda = float(config.get("outcomes_decay_lambda", 0.0))

    now = datetime.now(timezone.utc)
    condition_stats = defaultdict(lambda: {
        "wins": 0.0, "losses": 0.0, "total": 0.0,
        "n": 0, "effective_n": 0.0,
        "margins": [],
        "type": "",
    })

    for record in records:
        ts_str = record.get("ts", "")
        try:
            game_dt = datetime.fromisoformat(ts_str)
            if game_dt.tzinfo is None:
                game_dt = game_dt.replace(tzinfo=timezone.utc)
            days_ago = (now - game_dt).total_seconds() / 86400
        except Exception:
            days_ago = 365  # fallback

        weight = _recency_weight(days_ago, decay_lambda)

        # Process conditions fired in this game
        conditions_fired = record.get("conditions_fired", [])
        winner = record.get("winner")
        margin = record.get("margin", 0)

        for cf in conditions_fired:
            cond_name = cf.get("condition", "")
            cond_type = cf.get("type", "")
            team = cf.get("team", "")

            stats = condition_stats[cond_name]
            stats["n"] += 1
            if cond_type and not stats.get("type"):
                stats["type"] = cond_type
            stats["effective_n"] += weight
            stats["total"] += weight
            stats["margins"].append(margin)

            if winner and team:
                if team == winner:
                    stats["wins"] += weight
                else:
                    stats["losses"] += weight

    # Compute percentages
    result = {}
    for cond_name, stats in condition_stats.items():
        if stats["n"] < MIN_SAMPLE:
            continue
        if stats["effective_n"] < MIN_SAMPLE:
            continue

        total = stats["total"]
        win_pct = (stats["wins"] / total * 100) if total > 0 else 0
        avg_margin = sum(stats["margins"]) / len(stats["margins"]) if stats["margins"] else 0

        ctype = stats.get("type", "")
        pol = classify_polarity(ctype, condition_name=cond_name)
        result[cond_name] = {
            "win_pct": round(win_pct, 1),
            "loss_pct": round(100 - win_pct, 1),
            "n": stats["n"],
            "effective_n": round(stats["effective_n"], 1),
            "avg_margin": round(avg_margin, 1),
            "type": ctype,
            "polarity": pol,
        }

    return {
        "conditions": result,
        "total_games": len(records),
        "decay_lambda": decay_lambda,
    }


# --- ROI / Scenario helpers (NRL #191) ----------------------------------------

STAKE = 100.0
FLAT_WIN_PROFIT = STAKE * (100.0 / 110.0)  # ~90.91 — fallback -110 juice


def _decimal_odds_profit(dec_odds):
    """Profit on a $100 winning bet at decimal odds. Returns float."""
    if dec_odds is None or dec_odds <= 1.0:
        return FLAT_WIN_PROFIT  # fallback
    return STAKE * (dec_odds - 1.0)


def _did_team_cover_bk_line(rec, predicted_team, bk_spread):
    """Return True/False/None — did predicted team cover the BK spread line (#200).

    NRL uses team names (not IDs). ``bk_spread`` is from the predicted team's
    perspective (the line captured at fire time for that team).
    """
    home_score = rec.get("home_score")
    away_score = rec.get("away_score")
    if home_score is None or away_score is None:
        return None
    try:
        hs = int(home_score)
        as_ = int(away_score)
        line = float(bk_spread)
    except (TypeError, ValueError):
        return None
    pt = (predicted_team or "").strip()
    ht = (rec.get("home_team") or "").strip()
    at = (rec.get("away_team") or "").strip()
    if pt == ht:
        team_margin = hs - as_
    elif pt == at:
        team_margin = as_ - hs
    else:
        return None
    result = team_margin + line
    if result == 0:
        return None  # push — exclude from ROI (#258 Phase 4.4)
    return result > 0


def _ml_win_profit(ml_int):
    """Profit on a $100 winning bet at the given moneyline. Returns float."""
    if ml_int is None or ml_int == 0:
        return STAKE * (100.0 / 110.0)  # fallback: -110 juice
    if ml_int > 0:
        return STAKE * (ml_int / 100.0)
    return STAKE * (100.0 / abs(ml_int))


def _classify_spread_role(pred_team, spread_fav):
    """Return 'favorite', 'underdog', or '' if spread data unavailable.

    NRL uses team names (not IDs) for spread favourite.
    """
    pt = (pred_team or "").strip()
    sf = (spread_fav or "").strip()
    if not pt or not sf:
        return ""
    return "favorite" if pt.lower() == sf.lower() else "underdog"


def _edge_range_key(avg_edge):
    """Classify avg_edge into a bucket key matching ROI Matrix dimensions (NRL #191).
    Returns '0-5' for None/invalid so scenario keys always form (#234).
    """
    if avg_edge is None:
        return "0-5"
    try:
        e = float(avg_edge)
    except (TypeError, ValueError):
        return "0-5"
    if e >= 10:
        return "10+"
    if e >= 5:
        return "5-10"
    if e >= 0:
        return "0-5"
    return "<0"


def _margin_key(score_margin_at_fire):
    """Classify score_margin_at_fire into leading/trailing matching ROI Matrix (NRL #191)."""
    if score_margin_at_fire is None:
        return ""
    try:
        return "leading" if float(score_margin_at_fire) >= 0 else "trailing"
    except (TypeError, ValueError):
        return ""


def scenario_key_from_call(call, spread_fav=""):
    """
    Build a stable scenario key for a PW call from 4 dimensions (NRL #191):
    spread_role, margin, edge_range, quarter.
    Returns pipe-delimited string like "favorite|leading|5-10|Q3", or "" if any dimension missing.
    If the call already has a 'spread_role' field set, uses that directly.
    """
    role = str(call.get("spread_role") or "").strip().lower()
    if not role:
        pred_team = str(call.get("predicted_team") or "").strip()
        role = _classify_spread_role(pred_team, spread_fav)
    margin = _margin_key(call.get("score_margin_at_fire"))
    edge = _edge_range_key(call.get("avg_edge"))
    quarter = str(call.get("quarter") or "").strip()
    if not role or not margin or not edge or not quarter:
        return ""
    return f"{role}|{margin}|{edge}|{quarter}"


def _filter_calls_by_selection(calls, call_selection):
    """Apply call_selection filter: first, first_per_half, last, or all."""
    sel = (call_selection or "").strip().lower()
    if not sel or sel == "all" or not calls:
        return calls
    if sel == "first":
        return [min(calls, key=lambda c: c.get("ts") or "")]
    if sel == "last":
        return [max(calls, key=lambda c: c.get("ts") or "")]
    if sel == "first_per_half":
        by_h = {}
        for c in calls:
            h = c.get("half", "")
            if h not in by_h or (c.get("ts") or "") < (by_h[h].get("ts") or ""):
                by_h[h] = c
        return sorted(by_h.values(), key=lambda c: c.get("ts") or "")
    if sel == "first_per_quarter":
        by_q = {}
        for c in calls:
            q = c.get("quarter", "")
            if q not in by_q or (c.get("ts") or "") < (by_q[q].get("ts") or ""):
                by_q[q] = c
        return sorted(by_q.values(), key=lambda c: c.get("ts") or "")
    return calls


def _filter_records_by_segment(records, season_segment, season_year=""):
    """Filter game history records by season_segment and/or season_year (NRL #191)."""
    seg = (season_segment or "").strip()
    yr = (season_year or "").strip()
    out = records
    if seg and seg != "all":
        out = [r for r in out if r.get("season_segment") == seg]
    if yr and yr != "all":
        out = [r for r in out if str(r.get("season", "")) == yr]
    return out


def compute_scenario_stats(records, call_selection="", season_segment="", season_year="",
                           source="", strict_bk=False):
    """Compute per-scenario aggregate stats from game_history records (NRL #191).

    Optional filters (#217):
      source: "live" (non-synthetic calls only) or "backfill" (synthetic only)
      strict_bk: True to keep only calls with genuine BK odds (bk_ml_source)
    """
    filtered = _filter_records_by_segment(records, season_segment, season_year)
    norm_call_sel = (call_selection or "").strip().lower()
    norm_source = (source or "").strip().lower()
    scenarios = {}

    for rec in filtered:
        if not isinstance(rec, dict):
            continue
        calls = rec.get("predicted_winner_calls")
        if not isinstance(calls, list) or not calls:
            continue
        # NRL uses spread_fav (team name), not spread_fav_id
        spread_fav = ""
        hs = rec.get("home_spread")
        if hs is not None:
            try:
                spread_fav = rec.get("home_team", "") if float(hs) < 0 else rec.get("away_team", "")
            except (TypeError, ValueError):
                pass
        eligible = [c for c in calls if isinstance(c, dict) and not c.get("_suppressed")]
        eligible = _filter_calls_by_selection(eligible, norm_call_sel)
        if not eligible:
            continue
        for call in eligible:
            c = call.get("correct")
            if c is None:
                continue
            # Source filter (#217): synthetic=True → backfill, False/missing → live
            if norm_source == "live" and call.get("synthetic"):
                continue
            if norm_source == "backfill" and not call.get("synthetic"):
                continue
            # Live-priced filter (#258 Phase 4.1): exclude backfill-priced calls
            if strict_bk and not call.get("bk_ml_source"):
                continue
            skey = scenario_key_from_call(call, spread_fav)
            if not skey:
                continue
            if skey not in scenarios:
                scenarios[skey] = {"total_calls": 0, "correct": 0,
                                   "game_ids": set(),
                                   "bk_odds_pnl": 0.0, "bk_odds_count": 0,
                                   "bk_spr_pnl": 0.0, "bk_spr_count": 0,
                                   "bk_spr_covered": 0}
            acc = scenarios[skey]
            acc["total_calls"] += 1
            acc["game_ids"].add(str(rec.get("match_id") or rec.get("game_id") or ""))
            is_correct = bool(c)
            if is_correct:
                acc["correct"] += 1
            _call_bk_ml = call.get("bk_moneyline")
            if _call_bk_ml is not None:
                try:
                    _bk_ml_val = float(str(_call_bk_ml))
                    if _bk_ml_val > 1:
                        # NRL uses decimal odds
                        acc["bk_odds_count"] += 1
                        acc["bk_odds_pnl"] += STAKE * (_bk_ml_val - 1) if is_correct else -STAKE
                except (ValueError, TypeError):
                    pass

            # BK Spread ROI (#200: requires bk_spread + bk_spread_price, no fallback #258)
            _call_bk_spread = call.get("bk_spread")
            _call_bk_spr_price = call.get("bk_spread_price")
            if _call_bk_spread is not None and _call_bk_spr_price is not None:
                pred_team = str(call.get("predicted_team") or "").strip()
                _call_bk_cover = _did_team_cover_bk_line(
                    rec, pred_team, _call_bk_spread)
                if _call_bk_cover is not None:
                    try:
                        _spr_profit = _decimal_odds_profit(float(_call_bk_spr_price))
                    except (ValueError, TypeError):
                        _spr_profit = None
                    if _spr_profit is not None:
                        acc["bk_spr_count"] += 1
                        if _call_bk_cover:
                            acc["bk_spr_covered"] += 1
                            acc["bk_spr_pnl"] += _spr_profit
                        else:
                            acc["bk_spr_pnl"] -= STAKE

    def _pct(c, t):
        return round(c / t * 100, 1) if t > 0 else None
    def _roi_pct(pnl, n):
        return round(pnl / (n * STAKE) * 100.0, 1) if n > 0 else None

    result = {}
    for skey, acc in scenarios.items():
        result[skey] = {
            "total_calls": acc["total_calls"],
            "total_games": len(acc["game_ids"]),
            "correct": acc["correct"],
            "accuracy_pct": _pct(acc["correct"], acc["total_calls"]),
            "bk_odds_roi_pct": _roi_pct(acc["bk_odds_pnl"], acc["bk_odds_count"]),
            "bk_odds_count": acc["bk_odds_count"],
            "bk_odds_pnl": round(acc["bk_odds_pnl"], 2),
            "bk_spr_roi_pct": _roi_pct(acc["bk_spr_pnl"], acc["bk_spr_count"]),
            "bk_spr_count": acc["bk_spr_count"],
            "bk_spr_covered": acc["bk_spr_covered"],
            "bk_spr_pnl": round(acc["bk_spr_pnl"], 2),
        }
    return result


# ── PW Version Tracking (#15 Step 16) ────────────────────────────────────────

# Each tuple: (version, start_date_inclusive, end_date_inclusive)
# Add new entries as the PW algorithm evolves.
PW_VERSIONS = [
    ("v1.0", "2024-01-01", "2099-12-31"),
]


def resolve_pw_version(game_date_str):
    """Return the pw_version for a game date string (ISO format or YYYY-MM-DD).

    Delegates to pw_trend module if available (changelog-based resolution),
    falls back to PW_VERSIONS table.
    """
    try:
        import pw_trend
        return pw_trend.resolve_pw_version(game_date_str)
    except ImportError:
        pass
    d = game_date_str[:10] if game_date_str else ""
    for ver, start, end in PW_VERSIONS:
        if start <= d <= end:
            return ver
    return PW_VERSIONS[-1][0] if PW_VERSIONS else "v1.0"


# ── Predicted Winner: Bayesian Blending + Consensus (#15 Phase 2) ────────────

# Default Bayesian Beta priors
DEFAULT_PRIORS = {
    "team_wins":     {"alpha": 13.0, "beta": 7.0},
    "game_upset":    {"alpha": 7.0,  "beta": 13.0},
    "game_close":    {"alpha": 8.0,  "beta": 12.0},
    "game_comeback": {"alpha": 3.0,  "beta": 7.0},
}

DEFAULT_BLENDING = {
    "sample_threshold_low": 15,
    "sample_threshold_high": 50,
    "bayes_weight_low": 0.70,
    "bayes_weight_mid": 0.50,
    "bayes_weight_high": 0.20,
}


class ConditionEdgeStore:
    """Track per-condition team-win rate for polarity/edge calculations."""

    def __init__(self, records):
        self._edges = {}  # cond_name -> {fires, team_won}
        for rec in records:
            winner = rec.get("winner")
            if not winner:
                continue
            for cf in rec.get("conditions_fired", []):
                cname = cf.get("condition", "")
                team = cf.get("team", "")
                if cname not in self._edges:
                    self._edges[cname] = {"fires": 0, "team_won": 0}
                self._edges[cname]["fires"] += 1
                if team == winner:
                    self._edges[cname]["team_won"] += 1

    def edge(self, condition_name):
        """Return edge: team_won_pct - 50%. Positive = good indicator."""
        e = self._edges.get(condition_name)
        if not e or e["fires"] < 3:
            return 0.0
        return (e["team_won"] / e["fires"] * 100) - 50.0

    def compute_avg_edge(self, active_conditions, pred_team, cond_team_map):
        """Average edge across active conditions for predicted team."""
        edges = []
        for cname in active_conditions:
            team = cond_team_map.get(cname, "")
            e = self.edge(cname)
            # Flip sign if condition fired for opponent
            if team and team != pred_team:
                e = -e
            edges.append(e)
        return sum(edges) / len(edges) if edges else 0.0


# Polarity: positive/negative classification for NRL conditions
# Unambiguous types (always positive/negative regardless of direction)
_POSITIVE_TYPES = {"try_scoring_run", "points_run", "momentum_shift",
                   "line_break_surge", "halftime_turnaround",
                   "first_team_scores", "post_contact_metres",
                   "tackle_breaks", "line_breaks", "offloads", "intercepts",
                   "kick_return_metres"}
_NEGATIVE_TYPES = {"error_rate", "error_streak", "penalty_count",
                   "penalty_pressure", "sin_bin", "red_card",
                   "missed_tackles", "ineffective_tackles"}
# Name-based patterns for direction-dependent types (#118)
# Checked against individual words in condition name (whole-word match)
_POSITIVE_NAME_WORDS = {"dominant", "dominating", "winning", "strong",
                        "blowout", "high", "hot", "attacking", "creative"}
_NEGATIVE_NAME_WORDS = {"poor", "low", "trailing", "losing",
                        "critical", "ineffective"}


def classify_polarity(condition_type, condition_name=None):
    """Classify a condition as 'pos', 'neg', or None.

    Uses condition name first (handles direction-dependent types like
    run_metres, score_diff, possession), falls back to type-based lookup.
    """
    # Name-based check first — handles bidirectional types (#118)
    if condition_name:
        # Split into words for whole-word matching (avoids "low" in "blowout")
        words = set(re.findall(r'[a-z]+', condition_name.lower()))
        if words & _NEGATIVE_NAME_WORDS:
            return "neg"
        if words & _POSITIVE_NAME_WORDS:
            return "pos"
    # Type-based fallback
    if condition_type in _POSITIVE_TYPES:
        return "pos"
    if condition_type in _NEGATIVE_TYPES:
        return "neg"
    return None


def _bayesian_posterior(successes, total, alpha, beta):
    """Bayesian posterior mean using Beta-Binomial conjugate."""
    return (successes + alpha) / (total + alpha + beta)


def live_analysis_for_game(game_id, active_conditions, records, active_hits,
                           config, _edge_store=None):
    """Compute predicted winner analysis with Bayesian blending.

    Args:
        game_id: match ID
        active_conditions: list of condition name strings currently firing
        records: full game history for lookups
        active_hits: list of dicts with {condition, team, team_side, score_margin}
        config: runtime config dict
        _edge_store: optional pre-built ConditionEdgeStore

    Returns:
        dict with "predictions" and "suppressed_predictions" lists
    """
    if not records or not active_conditions:
        return {"predictions": [], "suppressed_predictions": []}

    priors = config.get("bayes_priors", DEFAULT_PRIORS)
    blending = config.get("pw_blending", DEFAULT_BLENDING)
    edge_store = _edge_store or ConditionEdgeStore(records)

    # Build condition index: cond_name -> list of {team_won, margin, ...}
    cond_index = defaultdict(list)
    for rec in records:
        winner = rec.get("winner")
        if not winner:
            continue
        for cf in rec.get("conditions_fired", []):
            cname = cf.get("condition", "")
            team = cf.get("team", "")
            cond_index[cname].append({
                "team_won": team == winner,
                "margin": rec.get("margin", 0),
            })

    # For each active condition combo, compute frequency + Bayesian posterior
    # Group hits by team
    team_hits = defaultdict(list)
    cond_team_map = {}
    for h in active_hits:
        team = h.get("team", "")
        cname = h.get("condition", h.get("name", ""))
        team_hits[team].append(cname)
        cond_team_map[cname] = team

    predictions = []
    for team, conds in team_hits.items():
        # Frequency: how often does this team win when these conditions fire?
        freq_wins = 0.0
        freq_total = 0.0
        for cname in conds:
            entries = cond_index.get(cname, [])
            for e in entries:
                freq_total += 1
                if e["team_won"]:
                    freq_wins += 1

        if freq_total < 1:
            continue

        freq_pct = freq_wins / freq_total * 100

        # Bayesian posterior for team_wins
        tw_prior = priors.get("team_wins", DEFAULT_PRIORS["team_wins"])
        bayes_pct = _bayesian_posterior(freq_wins, freq_total,
                                        tw_prior["alpha"], tw_prior["beta"]) * 100

        # Blend based on effective sample size
        eff_n = freq_total
        low = blending.get("sample_threshold_low", 15)
        high = blending.get("sample_threshold_high", 50)
        if eff_n < low:
            bw = blending.get("bayes_weight_low", 0.70)
        elif eff_n < high:
            bw = blending.get("bayes_weight_mid", 0.50)
        else:
            bw = blending.get("bayes_weight_high", 0.20)

        blended_pct = bayes_pct * bw + freq_pct * (1 - bw)

        # Polarity
        pol_pos = sum(1 for c in conds if classify_polarity(
            next((h.get("type", "") for h in active_hits
                  if h.get("condition", h.get("name", "")) == c), ""),
            condition_name=c) == "pos")
        pol_neg = sum(1 for c in conds if classify_polarity(
            next((h.get("type", "") for h in active_hits
                  if h.get("condition", h.get("name", "")) == c), ""),
            condition_name=c) == "neg")

        # Edge
        avg_edge = edge_store.compute_avg_edge(conds, team, cond_team_map)

        predictions.append({
            "label": "Team wins",
            "pct": round(freq_pct, 1),
            "bayes_pct": round(bayes_pct, 1),
            "winner_score_pct": round(blended_pct, 1),
            "predicted_team": team,
            "sample": int(freq_total),
            "effective_sample": round(eff_n, 1),
            "basis": "combo" if len(conds) > 1 else "single",
            "consensus": "",  # filled by caller after ML comparison
            "conditions": conds,
            "conditions_count": len(conds),
            "polarity_pos": pol_pos,
            "polarity_neg": pol_neg,
            "polarity_net": pol_pos - pol_neg,
            "avg_edge": round(avg_edge, 1),
            "blended": True,
        })

    # Sort by blended pct desc — highest confidence first
    predictions.sort(key=lambda p: p.get("winner_score_pct", 0), reverse=True)

    return {"predictions": predictions, "suppressed_predictions": []}


# ── Backup / WAL helpers (#16) ──────────────────────────────────────────────

def save_history(records):
    """Write game history list to disk atomically."""
    tmp = HISTORY_FILE + ".tmp.{}".format(os.getpid())
    with open(tmp, "w") as f:
        json.dump(records, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, HISTORY_FILE)


def append_to_wal(record):
    """Append a single game record to the WAL (JSONL, fsync'd).

    Called BEFORE writing to game_history.json so the record survives
    a crash between WAL append and main-file write.
    """
    try:
        line = json.dumps(record, separators=(",", ":"))
        with open(WAL_FILE, "a") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        pass  # WAL append failure must not block the monitor


def list_backups():
    """Return list of backup dicts sorted newest-first.

    Each dict: {name, path, records, size, mtime}.
    """
    if not os.path.isdir(BACKUP_ROOT):
        return []
    backups = []
    for name in os.listdir(BACKUP_ROOT):
        if name.startswith("failed-run-"):
            continue
        full = os.path.join(BACKUP_ROOT, name)
        if not os.path.isdir(full):
            continue
        hist = os.path.join(full, "game_history.json")
        if not os.path.exists(hist):
            continue
        stat = os.stat(hist)
        try:
            with open(hist) as f:
                records = len(json.load(f))
        except Exception:
            records = 0
        # Prefer metadata created_at for mtime
        mtime = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc
        ).isoformat()
        meta_path = os.path.join(full, "metadata.json")
        if os.path.exists(meta_path):
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
                mtime = meta.get("created_at") or mtime
            except Exception:
                pass
        # Count all files in backup directory
        try:
            file_count = len([f for f in os.listdir(full) if os.path.isfile(os.path.join(full, f))])
        except Exception:
            file_count = 0
        backups.append({
            "name": name,
            "path": full,
            "records": records,
            "size": stat.st_size,
            "mtime": mtime,
            "file_count": file_count,
        })
    backups.sort(key=lambda b: b["mtime"], reverse=True)
    return backups


def restore_from_backup(backup_name, replay_wal_entries=True):
    """Restore game_history.json from a named backup, optionally replaying WAL.

    Returns dict: {success, records, wal_recovered, error}.
    """
    backup_dir = os.path.join(BACKUP_ROOT, backup_name)
    hist_path = os.path.join(backup_dir, "game_history.json")
    if not os.path.exists(hist_path):
        return {"success": False, "records": 0, "wal_recovered": 0,
                "error": "Backup not found: {}".format(backup_name)}
    try:
        with open(hist_path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return {"success": False, "records": 0, "wal_recovered": 0,
                    "error": "Backup is not a valid list"}
    except Exception as e:
        return {"success": False, "records": 0, "wal_recovered": 0,
                "error": str(e)}

    wal_recovered = 0
    if replay_wal_entries:
        data, wal_recovered = replay_wal(data)

    save_history(data)
    return {"success": True, "records": len(data),
            "wal_recovered": wal_recovered, "error": None}


def replay_wal(records):
    """Append WAL entries that are not already in records.

    Returns (merged_records, count_added).
    """
    if not os.path.exists(WAL_FILE):
        return records, 0
    existing_ids = {r.get("match_id") for r in records}
    added = 0
    try:
        with open(WAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mid = entry.get("match_id")
                if mid and mid not in existing_ids:
                    records.append(entry)
                    existing_ids.add(mid)
                    added += 1
    except Exception:
        pass
    return records, added


def wal_status():
    """Return WAL file status dict."""
    if not os.path.exists(WAL_FILE):
        return {"exists": False, "entries": 0, "size": 0,
                "oldest": None, "newest": None}
    stat = os.stat(WAL_FILE)
    entries = []
    try:
        with open(WAL_FILE) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    dates = [e.get("ts", "") for e in entries if e.get("ts")]
    dates.sort()
    return {
        "exists": True,
        "entries": len(entries),
        "size": stat.st_size,
        "oldest": dates[0] if dates else None,
        "newest": dates[-1] if dates else None,
    }


def truncate_wal(keep_after=None):
    """Remove WAL entries older than keep_after timestamp.

    If keep_after is None, truncates the entire WAL.
    Returns count of removed entries.
    """
    if not os.path.exists(WAL_FILE):
        return 0
    if keep_after is None:
        # Truncate entire file
        try:
            with open(WAL_FILE) as f:
                count = sum(1 for line in f if line.strip())
            with open(WAL_FILE, "w") as f:
                pass  # empty file
            return count
        except Exception:
            return 0

    kept_lines = []
    removed = 0
    try:
        with open(WAL_FILE) as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                    ts = entry.get("ts", "")
                    if ts >= keep_after:
                        kept_lines.append(stripped)
                    else:
                        removed += 1
                except json.JSONDecodeError:
                    removed += 1
    except Exception:
        return 0

    tmp = WAL_FILE + ".tmp.{}".format(os.getpid())
    with open(tmp, "w") as f:
        for line in kept_lines:
            f.write(line + "\n")
    os.replace(tmp, WAL_FILE)
    return removed
