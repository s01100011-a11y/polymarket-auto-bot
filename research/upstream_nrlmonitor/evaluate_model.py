#!/usr/bin/env python3
"""
evaluate_model.py — Chronological evaluation of NRL Monitor prediction models (#33).

Runs rolling-origin backtests over game_history.json and reports per-target
metrics for frequency and Bayesian predictors:
  - Brier score (probability accuracy)
  - Log loss (penalises overconfident wrong calls)
  - ECE (expected calibration error)

Usage:
    python3 evaluate_model.py --target team_wins
    python3 evaluate_model.py --target game_close --step-days 7
"""

import argparse
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
EVAL_CACHE_FILE = os.path.join(SCRIPT_DIR, "model_eval_cache_nrl.json")
EVAL_HISTORY_FILE = os.path.join(SCRIPT_DIR, "model_eval_history_nrl.json")

TARGET_MAP = {
    "team_wins": "team_won",       # computed: winner == condition team
    "game_upset": "was_upset",     # computed: underdog won (from odds)
    "game_close": "tag_CLOSE",     # computed: CLOSE in postgame_tags
    "game_blowout": "tag_BLOWOUT", # computed: BLOWOUT in postgame_tags
    "game_comeback": "tag_COMEBACK",# computed: COMEBACK in postgame_tags
}

# NRL Bayesian priors (matching outcomes.py DEFAULT_PRIORS)
BAYES_PRIORS = {
    "team_wins":     {"alpha": 13.0, "beta": 7.0},    # ~0.65 base rate
    "game_upset":    {"alpha": 7.0,  "beta": 13.0},   # ~0.35
    "game_close":    {"alpha": 8.0,  "beta": 12.0},   # ~0.40
    "game_blowout":  {"alpha": 2.0,  "beta": 9.0},    # ~0.18
    "game_comeback": {"alpha": 3.0,  "beta": 7.0},    # ~0.30
}


def parse_iso_date(date_value):
    if not isinstance(date_value, str):
        return None
    raw = date_value.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def load_history(path=None):
    path = path or HISTORY_FILE
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def to_samples(records, target):
    """Extract (date, condition, label) samples from game history.

    NRL adaptations:
    - Uses 'condition' field (not 'name')
    - Computes team_won from winner + team (no team_won field)
    - Uses postgame_tags for close/blowout/comeback targets
    """
    target_key = TARGET_MAP[target]
    samples = []

    for rec in records:
        if not isinstance(rec, dict):
            continue
        game_date = parse_iso_date(rec.get("kickoff_utc") or rec.get("ts") or rec.get("date"))
        if game_date is None:
            continue

        conditions = rec.get("conditions_fired") or []
        if not isinstance(conditions, list):
            continue

        winner = rec.get("winner", "")
        tags = rec.get("postgame_tags", [])

        # Compute upset: underdog won (from decimal odds)
        was_upset = None
        if target_key == "was_upset":
            home_ml = rec.get("home_ml") or rec.get("home_odds")
            away_ml = rec.get("away_ml") or rec.get("away_odds")
            if home_ml and away_ml and winner:
                try:
                    fav = rec.get("home_team") if float(home_ml) < float(away_ml) else rec.get("away_team")
                    was_upset = (winner != fav)
                except (TypeError, ValueError):
                    pass

        seen = set()
        for cond in conditions:
            if not isinstance(cond, dict):
                continue
            if cond.get("type") == "compound":
                continue

            name = str(cond.get("condition") or "").strip()
            team = str(cond.get("team") or "").strip()
            if not name:
                continue

            game_id = str(rec.get("game_id") or rec.get("match_id") or "")
            dedupe_key = (game_id, name, team)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            if target_key == "team_won":
                label = 1 if (winner and team == winner) else 0
            elif target_key == "was_upset":
                if was_upset is None:
                    continue  # skip games without odds data
                label = 1 if was_upset else 0
            elif target_key == "tag_CLOSE":
                label = 1 if "CLOSE" in tags else 0
            elif target_key == "tag_BLOWOUT":
                label = 1 if "BLOWOUT" in tags else 0
            elif target_key == "tag_COMEBACK":
                label = 1 if "COMEBACK" in tags else 0
            else:
                label = 0

            samples.append({
                "date": game_date,
                "condition": name,
                "label": label,
            })

    samples.sort(key=lambda row: row["date"])
    return samples


# ── Predictors ──────────────────────────────────────────────────────────

def fit_frequency(train_samples):
    counts = defaultdict(lambda: [0, 0])
    total = [0, 0]
    for s in train_samples:
        cond = s["condition"]
        y = int(s["label"])
        counts[cond][0] += y
        counts[cond][1] += 1
        total[0] += y
        total[1] += 1

    global_rate = (total[0] / total[1]) if total[1] else 0.5

    def predict(sample):
        success, n = counts.get(sample["condition"], (0, 0))
        if n <= 0:
            return global_rate
        return success / n

    return predict


def fit_bayesian(train_samples, target):
    prior = BAYES_PRIORS.get(target, {"alpha": 1.0, "beta": 1.0})
    alpha0 = float(prior["alpha"])
    beta0 = float(prior["beta"])

    counts = defaultdict(lambda: [0, 0])
    total = [0, 0]
    for s in train_samples:
        cond = s["condition"]
        y = int(s["label"])
        counts[cond][0] += y
        counts[cond][1] += 1
        total[0] += y
        total[1] += 1

    global_alpha = alpha0 + total[0]
    global_beta = beta0 + (total[1] - total[0])
    global_rate = global_alpha / (global_alpha + global_beta)

    def predict(sample):
        success, n = counts.get(sample["condition"], (0, 0))
        if n <= 0:
            return global_rate
        alpha = alpha0 + success
        beta = beta0 + (n - success)
        return alpha / (alpha + beta)

    return predict


# ── Evaluation methods ──────────────────────────────────────────────────

def rolling_origin_predictions(samples, fit_fn, min_train, step_days=1):
    by_date = defaultdict(list)
    for s in samples:
        by_date[s["date"].date().isoformat()].append(s)

    ordered_dates = sorted(by_date.keys())
    train = []
    scored = []
    split_count = 0
    predictor = None
    last_fit_idx = -step_days

    for i, day in enumerate(ordered_dates):
        test_rows = by_date[day]
        if len(train) >= min_train:
            if predictor is None or (i - last_fit_idx) >= step_days:
                predictor = fit_fn(train)
                last_fit_idx = i
            for row in test_rows:
                p = predictor(row)
                scored.append({"p": float(p), "y": int(row["label"]), "date": day})
            split_count += 1
        train.extend(test_rows)

    return scored, split_count


def holdout_predictions(samples, fit_fn, train_ratio=0.8):
    if not samples:
        return []
    split_idx = int(len(samples) * float(train_ratio))
    split_idx = max(1, min(len(samples) - 1, split_idx))
    train = samples[:split_idx]
    test = samples[split_idx:]
    if not train or not test:
        return []
    predictor = fit_fn(train)
    return [{"p": float(predictor(row)), "y": int(row["label"]),
             "date": row["date"].date().isoformat()} for row in test]


# ── Metrics ─────────────────────────────────────────────────────────────

def brier(rows):
    if not rows:
        return None
    return sum((r["p"] - r["y"]) ** 2 for r in rows) / len(rows)


def log_loss_metric(rows, eps=1e-12):
    if not rows:
        return None
    total = 0.0
    for r in rows:
        p = min(1.0 - eps, max(eps, r["p"]))
        y = r["y"]
        total += y * math.log(p) + (1 - y) * math.log(1 - p)
    return -total / len(rows)


def calibration(rows, bins=10):
    if not rows:
        return {"ece": None, "bins": []}
    bins = max(2, int(bins))
    bucket = [{"n": 0, "sum_p": 0.0, "sum_y": 0.0} for _ in range(bins)]

    for r in rows:
        p = min(0.999999, max(0.0, r["p"]))
        idx = min(bins - 1, int(p * bins))
        bucket[idx]["n"] += 1
        bucket[idx]["sum_p"] += p
        bucket[idx]["sum_y"] += r["y"]

    out_bins = []
    ece = 0.0
    total_n = len(rows)
    for i, b in enumerate(bucket):
        if b["n"] <= 0:
            continue
        avg_p = b["sum_p"] / b["n"]
        avg_y = b["sum_y"] / b["n"]
        weight = b["n"] / total_n
        ece += abs(avg_y - avg_p) * weight
        out_bins.append({
            "bin": i,
            "range": [round(i / bins, 3), round((i + 1) / bins, 3)],
            "n": b["n"],
            "avg_pred": round(avg_p, 4),
            "avg_obs": round(avg_y, 4),
        })

    return {"ece": ece, "bins": out_bins}


def summarize(rows, bins):
    return {
        "n": len(rows),
        "brier": brier(rows),
        "log_loss": log_loss_metric(rows),
        "calibration": calibration(rows, bins=bins),
    }


def evaluate_target(records, target, min_train=50, bins=10, step_days=1):
    """Evaluate a single target using rolling-origin (or holdout fallback)."""
    samples = to_samples(records, target)

    freq_rows, freq_splits = rolling_origin_predictions(
        samples=samples, fit_fn=fit_frequency,
        min_train=min_train, step_days=step_days)
    bayes_rows, bayes_splits = rolling_origin_predictions(
        samples=samples,
        fit_fn=lambda train: fit_bayesian(train, target),
        min_train=min_train, step_days=step_days)

    evaluation_mode = "rolling_origin"
    if not freq_rows and not bayes_rows and len(samples) >= 2:
        evaluation_mode = "chronological_holdout_fallback"
        freq_rows = holdout_predictions(samples, fit_frequency)
        bayes_rows = holdout_predictions(samples, lambda train: fit_bayesian(train, target))

    return {
        "target": target,
        "samples": len(samples),
        "min_train": min_train,
        "evaluation_mode": evaluation_mode,
        "rolling_splits": {
            "frequency": freq_splits,
            "bayesian": bayes_splits,
        },
        "metrics": {
            "frequency": summarize(freq_rows, bins=bins),
            "bayesian": summarize(bayes_rows, bins=bins),
        },
    }


def evaluate_all(records=None, targets=None, min_train=50, bins=10, step_days=7):
    """Evaluate all targets and return full payload."""
    if records is None:
        records = load_history()
    if targets is None:
        targets = sorted(TARGET_MAP.keys())

    result = {}
    for target in targets:
        if target in TARGET_MAP:
            result[target] = evaluate_target(records, target, min_train, bins, step_days)
    return {
        "updated": datetime.now(timezone.utc).isoformat(),
        "records": len(records),
        "min_train": min_train,
        "step_days": step_days,
        "targets": result,
    }


# ── Snapshot history ────────────────────────────────────────────────────

def load_eval_history():
    try:
        with open(EVAL_HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_eval_snapshot(payload, max_entries=100):
    """Append a compact snapshot to history file."""
    history = load_eval_history()
    compact = {
        "snapshot_ts": datetime.now(timezone.utc).isoformat(),
        "records": payload.get("records", 0),
        "min_train": payload.get("min_train", 50),
        "targets": {},
    }
    for tname, tdata in (payload.get("targets") or {}).items():
        freq = (tdata.get("metrics") or {}).get("frequency") or {}
        bayes = (tdata.get("metrics") or {}).get("bayesian") or {}
        compact["targets"][tname] = {
            "evaluation_mode": tdata.get("evaluation_mode", ""),
            "eval_n": freq.get("n", 0),
            "frequency": {
                "brier": freq.get("brier"),
                "log_loss": freq.get("log_loss"),
                "ece": (freq.get("calibration") or {}).get("ece"),
            },
            "bayesian": {
                "brier": bayes.get("brier"),
                "log_loss": bayes.get("log_loss"),
                "ece": (bayes.get("calibration") or {}).get("ece"),
            },
        }
    history.append(compact)
    if len(history) > max_entries:
        history = history[-max_entries:]
    tmp = EVAL_HISTORY_FILE + ".tmp.{}".format(os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, EVAL_HISTORY_FILE)
    return compact


# ── CLI ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Chronological backtest for NRL Monitor outcomes")
    parser.add_argument("--history", default=HISTORY_FILE)
    parser.add_argument("--target", choices=sorted(TARGET_MAP.keys()), default=None,
                        help="Evaluate single target (default: all)")
    parser.add_argument("--min-train", type=int, default=50)
    parser.add_argument("--bins", type=int, default=10)
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--save-snapshot", action="store_true", help="Save results to history")
    args = parser.parse_args()

    records = load_history(args.history)
    if args.target:
        result = evaluate_target(records, args.target, args.min_train, args.bins, args.step_days)
        payload = {"targets": {args.target: result}, "records": len(records),
                   "min_train": args.min_train, "step_days": args.step_days}
    else:
        payload = evaluate_all(records, min_train=args.min_train, bins=args.bins, step_days=args.step_days)

    print(json.dumps(payload, indent=2 if args.pretty else None, default=str))

    if args.save_snapshot:
        snap = save_eval_snapshot(payload)
        print("\nSnapshot saved:", snap["snapshot_ts"], file=__import__("sys").stderr)


if __name__ == "__main__":
    main()
