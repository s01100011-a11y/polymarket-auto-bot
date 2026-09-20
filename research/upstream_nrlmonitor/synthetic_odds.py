#!/usr/bin/env python3
"""Synthetic live odds generation from game state (#235).

Pure functions -- no I/O, no state, stdlib only.
Coefficients from Phase 1 BK odds analysis (analyse_bk_odds.py).
NRL version: decimal odds, NRL-calibrated coefficients.
"""

import math


# -- Coefficients (from Phase 1 regression) ------------------------------------

_SPREAD_SLOPE = -0.953
_SPREAD_INTERCEPT = 0.10

# Total game seconds (2 × 40-min halves)
_TOTAL_GAME_SECS = 4800

# Half multipliers for moneyline (H2 = more certainty from margin)
_QUARTER_MULT = {"Q1": 0.80, "Q2": 0.90, "Q3": 1.0, "Q4": 1.10, "ET": 1.15}

# Pregame moneyline probability adjustment coefficient
# ~1.5% shift per 10% of pregame implied probability difference from 50%
_PREGAME_COEFF = 0.015

# Bookmaker overround (~5% for NRL h2h) — applied to synthetic odds (#258 Phase 4.8)
_VIG = 0.05


# -- Helpers -------------------------------------------------------------------

def _prob_to_decimal(prob):
    """Convert implied probability (0-1) to decimal odds with bookmaker vig.

    Applies _VIG overround so synthetic odds approximate real market prices
    rather than fair odds.
    """
    if prob is None or prob <= 0.01 or prob >= 0.99:
        return None
    vigged = prob * (1.0 + _VIG / 2.0)
    vigged = max(0.02, min(0.98, vigged))
    return round(1.0 / vigged, 2)


def _margin_to_base_prob(margin):
    """Convert score margin to base implied probability using logistic curve.

    Calibrated to Phase 1 heatmap: margin 0 -> ~50%, margin +/-15 -> ~90%/10%.
    k=0.14 (steeper than NBA's 0.12 -- NRL margins are smaller and more decisive).
    """
    k = 0.14
    return 1.0 / (1.0 + math.exp(-k * margin))


# -- Public API ----------------------------------------------------------------

def compute_synth_spread(margin, pregame_spread_line=None, game_seconds=None):
    """Compute synthetic live spread with time decay (#258 Phase 4.11).

    Args:
        margin: int/float, score margin from predicted team's perspective
                (positive = leading)
        pregame_spread_line: unused (NRL spread R^2=0.75 from margin alone)
        game_seconds: int/float, elapsed game seconds (0-4800 for NRL).
                When provided, the spread tightens as time runs out —
                a 10-point lead at minute 75 is nearly the final margin,
                while at minute 10 the spread is closer to pregame.

    Returns: float (e.g. -5.5) or None if inputs invalid
    """
    if margin is None:
        return None
    base = _SPREAD_SLOPE * margin + _SPREAD_INTERCEPT
    # Time decay: as game progresses, spread converges toward -margin
    # At kickoff (0s): full spread adjustment from margin
    # At full time (4800s): spread ≈ -margin (no adjustment needed)
    if game_seconds is not None and game_seconds >= 0:
        elapsed_frac = min(1.0, game_seconds / _TOTAL_GAME_SECS)
        # Blend between spread model and raw -margin as time elapses
        final_spread = -margin
        base = base * (1.0 - elapsed_frac) + final_spread * elapsed_frac
    # Round to nearest 0.5 (standard spread increment)
    return round(base * 2) / 2


def compute_synth_moneyline(margin, quarter, pregame_ml_prob=None, game_seconds=None):
    """Compute synthetic live moneyline (decimal odds) (#258 Phase 4.11).

    Args:
        margin: int/float, score margin from predicted team's perspective
        quarter: str, "Q1"/"Q2"/"Q3"/"Q4"/"ET"
        pregame_ml_prob: float (0-1), implied probability from pregame
                decimal odds (e.g. 1.80 -> 1/1.80 = 0.556)
        game_seconds: int/float, elapsed game seconds (0-4800).
                When provided, uses continuous time weighting instead of
                quarter buckets — more accurate than the 4-bucket approximation.

    Returns: float (decimal odds, e.g. 1.72) or None
    """
    if margin is None or (quarter is None and game_seconds is None):
        return None

    # Base probability from margin (logistic curve)
    base_prob = _margin_to_base_prob(margin)

    # Time adjustment: continuous when game_seconds available, else quarter buckets
    if game_seconds is not None and game_seconds >= 0:
        # Continuous: 0.80 at kickoff → 1.15 at full time (same range as quarter mult)
        elapsed_frac = min(1.0, game_seconds / _TOTAL_GAME_SECS)
        q_mult = 0.80 + elapsed_frac * 0.35  # 0.80 → 1.15
    else:
        q_mult = _QUARTER_MULT.get(quarter, 1.0)
    # Amplify distance from 0.5 by time multiplier
    adjusted_prob = 0.5 + (base_prob - 0.5) * q_mult

    # Pregame adjustment (higher pregame prob = boost at same margin)
    if pregame_ml_prob is not None:
        # pregame_ml_prob > 0.5 means favoured -> boost probability
        pregame_adj = (pregame_ml_prob - 0.5) * _PREGAME_COEFF * 10
        adjusted_prob += pregame_adj

    # Clamp
    adjusted_prob = max(0.02, min(0.98, adjusted_prob))

    return _prob_to_decimal(adjusted_prob)


def compute_synth_h1_ml(margin, quarter):
    """Compute synthetic H1 moneyline.

    NRL lacks h1_correct data, so this always returns None.
    """
    return None


def compute_all(margin, quarter, pregame_ml_decimal=None, game_seconds=None):
    """Convenience wrapper returning all synthetic odds.

    Args:
        margin: score margin from predicted team's perspective
        quarter: "Q1"/"Q2"/"Q3"/"Q4"/"ET"
        pregame_ml_decimal: float, pregame decimal odds (e.g. 1.80)
        game_seconds: int/float, elapsed game seconds (0-4800)

    Returns: {
        "moneyline": float (decimal odds) or None,
        "spread": float or None,
        "h1_ml": None (always),
    }
    """
    pregame_prob = None
    if pregame_ml_decimal is not None:
        try:
            v = float(pregame_ml_decimal)
            if v > 1.0:
                pregame_prob = 1.0 / v
        except (TypeError, ValueError):
            pass

    return {
        "moneyline": compute_synth_moneyline(margin, quarter, pregame_prob, game_seconds),
        "spread": compute_synth_spread(margin, game_seconds=game_seconds),
        "h1_ml": compute_synth_h1_ml(margin, quarter),
    }
