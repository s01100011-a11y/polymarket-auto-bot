#!/usr/bin/env python3
"""Synthetic live odds generation from game state (#376).

Pure functions — no I/O, no state, stdlib only.
Coefficients from Phase 1 BK odds analysis.
"""

import math


# ── Coefficients (from Phase 1 regression) ────────────────────────────

_SPREAD_COEFFS = {
    "nba":  {"slope": -0.923, "intercept": 1.90},
    "wnba": {"slope": -0.984, "intercept": 0.85},
}

# H1 win rate lookup: (margin_lo, margin_hi) → win_rate
# From Phase 1 Section 8, Q2 fire data (3,152 outcomes)
_H1_WIN_RATES = [
    (-99, -15, 0.033),
    (-14, -10, 0.136),
    (-9,  -5,  0.323),
    (-4,  -1,  0.634),
    (0,    4,  0.795),
    (5,    9,  0.908),
    (10,  14,  0.972),
    (15,  99,  0.994),
]

# Quarter multipliers for moneyline (later = more certainty from margin)
_QUARTER_MULT = {"Q1": 0.85, "Q2": 0.95, "Q3": 1.0, "Q4": 1.10, "OT": 1.15}

# Pregame spread → probability adjustment coefficient
_PREGAME_COEFF = 0.012  # ~1.2% shift per point of pregame line

# Bookmaker overround (~4.5% for NBA/WNBA h2h) — applied to synthetic odds (#412 Phase 4.8)
_VIG = 0.045


# ── Helpers ───────────────────────────────────────────────────────────

def _prob_to_american(prob):
    """Convert implied probability (0-1) to American odds with bookmaker vig.

    Applies _VIG overround so synthetic odds approximate real market prices
    rather than fair odds. Without this, every synth ROI is overstated by
    roughly the bookmaker's margin.
    """
    if prob is None or prob <= 0.01 or prob >= 0.99:
        return None
    # Apply vig: shift probability away from 50% toward the favourite
    vigged = prob * (1.0 + _VIG / 2.0)
    vigged = max(0.02, min(0.98, vigged))
    if vigged >= 0.5:
        return round(-100.0 * vigged / (1.0 - vigged))
    else:
        return round(100.0 * (1.0 - vigged) / vigged)


def _margin_to_base_prob(margin):
    """Convert score margin to base implied probability using logistic curve.

    Calibrated to Phase 1 heatmap: margin 0 → ~50%, margin ±15 → ~90%/10%.
    Uses logistic function: 1 / (1 + exp(-k * margin))
    k calibrated so margin=10 → ~75% (matching Phase 1 Q3 data).
    """
    k = 0.12  # steepness — calibrated to Phase 1 heatmap
    return 1.0 / (1.0 + math.exp(-k * margin))


def _interpolate_h1_rate(margin):
    """Interpolate H1 win rate from lookup table using margin midpoints."""
    # Bucket midpoints for interpolation
    midpoints = [
        (-20, 0.033),
        (-12, 0.136),
        (-7,  0.323),
        (-2.5, 0.634),
        (2,   0.795),
        (7,   0.908),
        (12,  0.972),
        (20,  0.994),
    ]

    if margin <= midpoints[0][0]:
        return midpoints[0][1]
    if margin >= midpoints[-1][0]:
        return midpoints[-1][1]

    for i in range(len(midpoints) - 1):
        x0, y0 = midpoints[i]
        x1, y1 = midpoints[i + 1]
        if x0 <= margin <= x1:
            t = (margin - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)

    return 0.5  # fallback


# ── Public API ────────────────────────────────────────────────────────

def compute_synth_spread(margin, pregame_spread_line, league="nba"):
    """Compute synthetic live spread.

    Args:
        margin: int/float, score margin from predicted team's perspective
                (positive = leading)
        pregame_spread_line: float, pregame spread from predicted team's
                perspective (negative = favored)
        league: "nba" or "wnba"

    Returns: float (e.g. -5.5) or None if inputs invalid
    """
    if margin is None or pregame_spread_line is None:
        return None
    coeffs = _SPREAD_COEFFS.get(league, _SPREAD_COEFFS["nba"])
    # Base from margin
    base = coeffs["slope"] * margin + coeffs["intercept"]
    # Blend with pregame (pregame anchoring accounts for ~7% additional R2)
    # Weight: 70% margin-based, 30% pregame-anchored
    pregame_adj = pregame_spread_line * 0.3
    synth = base * 0.7 + (base + pregame_adj) * 0.3
    # Round to nearest 0.5 (standard spread increment)
    return round(synth * 2) / 2


def compute_synth_moneyline(margin, quarter, pregame_spread_line, league="nba"):
    """Compute synthetic live moneyline (American odds).

    Args:
        margin: int/float, score margin from predicted team's perspective
        quarter: str, "Q1"/"Q2"/"Q3"/"Q4"/"OT"
        pregame_spread_line: float, pregame spread from predicted team's
                perspective (negative = favored)
        league: "nba" or "wnba"

    Returns: int (American odds, e.g. -140) or None
    """
    if margin is None or quarter is None:
        return None

    # Base probability from margin (logistic curve)
    base_prob = _margin_to_base_prob(margin)

    # Quarter adjustment (later quarters = margin more decisive)
    q_mult = _QUARTER_MULT.get(quarter, 1.0)
    # Amplify distance from 0.5 by quarter multiplier
    adjusted_prob = 0.5 + (base_prob - 0.5) * q_mult

    # Pregame adjustment (favorites get probability boost at same margin)
    if pregame_spread_line is not None:
        # Negative pregame = favored → boost probability
        pregame_adj = -pregame_spread_line * _PREGAME_COEFF
        adjusted_prob += pregame_adj

    # Clamp
    adjusted_prob = max(0.02, min(0.98, adjusted_prob))

    return _prob_to_american(adjusted_prob)


def compute_synth_h1_ml(margin, quarter, league="nba"):
    """Compute synthetic H1 moneyline (American odds).

    Only valid for Q1/Q2 (before H1 decided).

    Args:
        margin: int/float, score margin from predicted team's perspective
        quarter: str, "Q1" or "Q2" (returns None for Q3/Q4/OT)
        league: "nba" or "wnba"

    Returns: int (American odds) or None
    """
    if quarter not in ("Q1", "Q2"):
        return None
    if margin is None:
        return None

    win_rate = _interpolate_h1_rate(margin)

    # Q1 fires have less certainty than Q2 (less game played)
    if quarter == "Q1":
        # Regress toward 50% — Q1 margin is less predictive of H1 outcome
        win_rate = 0.5 + (win_rate - 0.5) * 0.6

    win_rate = max(0.02, min(0.98, win_rate))
    return _prob_to_american(win_rate)


def compute_all(margin, quarter, pregame_spread_line, league="nba"):
    """Convenience wrapper returning all synthetic odds.

    Returns: {
        "moneyline": int or None,
        "spread": float or None,
        "h1_ml": int or None,
    }
    """
    return {
        "moneyline": compute_synth_moneyline(margin, quarter, pregame_spread_line, league),
        "spread": compute_synth_spread(margin, pregame_spread_line, league),
        "h1_ml": compute_synth_h1_ml(margin, quarter, league),
    }
