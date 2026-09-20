#!/usr/bin/env python3
"""
ml_model.py — NRL predicted winner model using logistic regression.

Trains on game_history.json using condition features + game context.
Produces predicted_winner with confidence percentage.

Usage:
  python3 ml_model.py --train              # Train and save model
  python3 ml_model.py --train --evaluate   # Train with cross-validation metrics
  python3 ml_model.py --predict <game_id>  # Predict for a specific game

Dependencies: scikit-learn (optional — fails gracefully if unavailable)
"""

import json
import os
import pickle
import sys
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
MODEL_FILE = os.path.join(SCRIPT_DIR, "model_nrl.pkl")
MODEL_META_FILE = os.path.join(SCRIPT_DIR, "model_meta_nrl.json")

try:
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import cross_val_score
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

# Feature columns extracted from each game record (per-team perspective)
CONDITION_TYPES = [
    "score_diff", "error_rate", "completion_rate", "penalty_count",
    "possession", "try_scoring_run", "points_run", "missed_tackles",
    "momentum_shift", "error_streak", "penalty_pressure", "sin_bin",
    "line_break_surge", "halftime_turnaround",
    "run_metres", "post_contact_metres", "tackle_breaks", "line_breaks",
    "offloads", "intercepts", "ineffective_tackles", "effective_tackle_pct",
    "kick_defusal", "kick_return_metres", "play_the_ball_speed", "red_card",
    "first_team_scores",
]

CONTEXT_FEATURES = [
    "score_margin",    # team score - opponent score
    "is_home",         # 1 if home team, 0 if away
    "halftime_margin", # from team perspective
    "h2_momentum",     # from team perspective
    "max_unanswered",  # team's max unanswered points
    "max_error_streak", # team's max error streak
    "max_penalty_window", # team's max penalties in 10m
    "sin_bins",        # team's sin bin count
    "line_break_surge", # team's max line break surge
]

FEATURE_NAMES = ["cond_" + t for t in CONDITION_TYPES] + CONTEXT_FEATURES
SCHEMA_VERSION = "nrl_ml_v1"


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} {}".format(ts, msg), flush=True)


def load_history():
    try:
        with open(HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def build_training_data(records):
    """Build feature matrix X and target vector y from game history.

    Each game produces two rows — one per team (home perspective, away perspective).
    Target: 1 if team won, 0 if team lost. Draws are excluded.
    """
    X_rows = []
    y_rows = []

    for rec in records:
        winner = rec.get("winner")
        if not winner:
            continue  # skip draws

        home = rec.get("home_team", "")
        away = rec.get("away_team", "")
        conditions = rec.get("conditions_fired", [])

        # Build condition fire sets per team
        home_conds = set()
        away_conds = set()
        for c in conditions:
            ctype = c.get("type", "")
            if c.get("team_side") == "home" or c.get("team") == home:
                home_conds.add(ctype)
            else:
                away_conds.add(ctype)

        ht_margin = rec.get("halftime_margin", 0) or 0
        h2_mom = rec.get("h2_momentum", 0) or 0

        for side in ("home", "away"):
            team = home if side == "home" else away
            conds = home_conds if side == "home" else away_conds
            is_home = 1 if side == "home" else 0

            # Condition binary features
            cond_feats = [1 if t in conds else 0 for t in CONDITION_TYPES]

            # Context features
            my_score = rec.get("{}_score".format(side), 0) or 0
            opp_score = rec.get("{}_score".format("away" if side == "home" else "home"), 0) or 0
            margin = my_score - opp_score

            # HT margin from team perspective
            team_ht = ht_margin if side == "home" else -ht_margin
            team_h2 = h2_mom if side == "home" else -h2_mom

            context_feats = [
                margin,
                is_home,
                team_ht,
                team_h2,
                rec.get("{}_max_unanswered_points".format(side), 0) or 0,
                rec.get("{}_max_error_streak".format(side), 0) or 0,
                rec.get("{}_max_penalty_window_10m".format(side), 0) or 0,
                rec.get("{}_sin_bins".format(side), 0) or 0,
                rec.get("{}_max_line_break_surge_10m".format(side), 0) or 0,
            ]

            row = cond_feats + context_feats
            target = 1 if team == winner else 0

            X_rows.append(row)
            y_rows.append(target)

    return X_rows, y_rows


def train_model(evaluate=False):
    """Train logistic regression model and save to disk."""
    if not SKLEARN_AVAILABLE:
        _log("ERROR: scikit-learn not available. Install: pip install scikit-learn")
        return None

    records = [r for r in load_history() if r.get("season_segment") != "state_of_origin"]  # Exclude SOO (#102)
    if len(records) < 20:
        _log("ERROR: Not enough game history ({} games, need 20+)".format(len(records)))
        return None

    X_rows, y_rows = build_training_data(records)
    X = np.array(X_rows, dtype=float)
    y = np.array(y_rows, dtype=int)

    _log("Training on {} samples from {} games...".format(len(X), len(records)))
    _log("  Features: {} ({} condition + {} context)".format(
        len(FEATURE_NAMES), len(CONDITION_TYPES), len(CONTEXT_FEATURES)))
    _log("  Win rate: {:.1f}%".format(y.mean() * 100))

    # Scale features
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Train
    model = LogisticRegression(max_iter=1000, C=1.0, random_state=42)
    model.fit(X_scaled, y)

    train_acc = model.score(X_scaled, y)
    _log("  Training accuracy: {:.1f}%".format(train_acc * 100))

    # Cross-validation
    if evaluate:
        cv_scores = cross_val_score(model, X_scaled, y, cv=5, scoring="accuracy")
        _log("  CV accuracy: {:.1f}% (+/- {:.1f}%)".format(
            cv_scores.mean() * 100, cv_scores.std() * 100))

        # Feature importance (coefficients)
        _log("  Top features by |coefficient|:")
        coefs = list(zip(FEATURE_NAMES, model.coef_[0]))
        coefs.sort(key=lambda x: abs(x[1]), reverse=True)
        for name, coef in coefs[:10]:
            _log("    {:30s} {:+.3f}".format(name, coef))

    # Save model
    with open(MODEL_FILE, "wb") as f:
        pickle.dump({"model": model, "scaler": scaler}, f)

    meta = {
        "schema_version": SCHEMA_VERSION,
        "feature_names": FEATURE_NAMES,
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "training_games": len(records),
        "training_samples": len(X),
        "training_accuracy": round(train_acc * 100, 1),
    }
    if evaluate:
        meta["cv_accuracy"] = round(cv_scores.mean() * 100, 1)
        meta["cv_std"] = round(cv_scores.std() * 100, 1)

    with open(MODEL_META_FILE, "w") as f:
        json.dump(meta, f, indent=2)

    _log("Model saved: {} ({} features, {:.1f}% accuracy)".format(
        MODEL_FILE, len(FEATURE_NAMES), train_acc * 100))
    return model


def load_model():
    """Load trained model from disk. Returns (model, scaler, meta) or None."""
    if not SKLEARN_AVAILABLE:
        return None
    if not os.path.isfile(MODEL_FILE) or not os.path.isfile(MODEL_META_FILE):
        return None
    try:
        with open(MODEL_FILE, "rb") as f:
            data = pickle.load(f)
        with open(MODEL_META_FILE) as f:
            meta = json.load(f)
        if meta.get("schema_version") != SCHEMA_VERSION:
            return None
        return data["model"], data["scaler"], meta
    except Exception:
        return None


def predict_winner(game_record, conditions_fired):
    """Predict winner for a game given current conditions.

    Args:
        game_record: dict with game scores and PBP features
        conditions_fired: list of condition dicts from current evaluation

    Returns:
        dict with predicted_team, confidence_pct, details — or None if model unavailable
    """
    loaded = load_model()
    if not loaded:
        return None
    model, scaler, meta = loaded

    home = game_record.get("home_team", "")
    away = game_record.get("away_team", "")
    ht_margin = game_record.get("halftime_margin", 0) or 0
    h2_mom = game_record.get("h2_momentum", 0) or 0

    # Build features for both teams
    home_conds = set()
    away_conds = set()
    for c in conditions_fired:
        ctype = c.get("type", "")
        if c.get("team_side") == "home" or c.get("team") == home:
            home_conds.add(ctype)
        else:
            away_conds.add(ctype)

    results = {}
    for side in ("home", "away"):
        team = home if side == "home" else away
        conds = home_conds if side == "home" else away_conds
        is_home = 1 if side == "home" else 0

        cond_feats = [1 if t in conds else 0 for t in CONDITION_TYPES]

        my_score = game_record.get("{}_score".format(side), 0) or 0
        opp_score = game_record.get("{}_score".format("away" if side == "home" else "home"), 0) or 0
        margin = my_score - opp_score

        team_ht = ht_margin if side == "home" else -ht_margin
        team_h2 = h2_mom if side == "home" else -h2_mom

        context_feats = [
            margin, is_home, team_ht, team_h2,
            game_record.get("{}_max_unanswered_points".format(side), 0) or 0,
            game_record.get("{}_max_error_streak".format(side), 0) or 0,
            game_record.get("{}_max_penalty_window_10m".format(side), 0) or 0,
            game_record.get("{}_sin_bins".format(side), 0) or 0,
            game_record.get("{}_max_line_break_surge_10m".format(side), 0) or 0,
        ]

        row = [cond_feats + context_feats]
        X = scaler.transform(row)
        prob = model.predict_proba(X)[0]
        win_prob = prob[1]  # probability of class 1 (win)

        results[side] = {"team": team, "win_prob": win_prob}

    # Pick the team with higher win probability
    if results["home"]["win_prob"] >= results["away"]["win_prob"]:
        winner = results["home"]
        loser = results["away"]
    else:
        winner = results["away"]
        loser = results["home"]

    confidence = round(winner["win_prob"] * 100, 1)

    return {
        "predicted_team": winner["team"],
        "confidence_pct": confidence,
        "home_win_pct": round(results["home"]["win_prob"] * 100, 1),
        "away_win_pct": round(results["away"]["win_prob"] * 100, 1),
        "model_version": SCHEMA_VERSION,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NRL predicted winner model")
    parser.add_argument("--train", action="store_true", help="Train model")
    parser.add_argument("--evaluate", action="store_true", help="Include CV metrics")
    args = parser.parse_args()

    if args.train:
        train_model(evaluate=args.evaluate)
    else:
        parser.print_help()
