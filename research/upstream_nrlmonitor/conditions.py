"""Shared condition evaluation helpers for NRL live monitoring.

NRL game structure: 2 x 40-minute halves (1H / 2H), optional golden-point extra time.
Stats available from NRL.com match centre API.

Condition types:
  - score_diff: margin at a given period (1H, 2H, game)
  - try_scoring_run: consecutive tries by one team
  - error_rate: errors / possessions or raw error count
  - completion_rate: set completion percentage threshold
  - penalty_count: penalties conceded threshold
  - possession: possession percentage threshold
  - points_run: unanswered points scored
"""


def _to_int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_score_diff(cond, team_ctx, context):
    """Evaluate score margin condition.

    Config keys:
      - period: '1H', '2H', 'game' (default 'game')
      - team_status: 'winning' or 'trailing'
      - min_lead: minimum lead to trigger (winning)
      - max_deficit: maximum deficit to trigger (trailing by less than X)
      - min_deficit: minimum deficit to trigger (trailing by at least X)
    """
    game_period = context.get("period", "")
    req_period = cond.get("period", "game")

    # Only check halftime score if requested period is 1H and game is at HT or beyond
    if req_period == "1H" and game_period not in ("HT", "2H", "FT"):
        return {"matched": False}
    if req_period == "2H" and game_period not in ("2H", "FT"):
        return {"matched": False}

    if req_period == "1H":
        diff = team_ctx.get("diff_1h")
    elif req_period == "2H":
        diff = team_ctx.get("diff_2h")
    else:
        diff = team_ctx.get("diff_total")

    if diff is None:
        return {"matched": False}

    status = cond.get("team_status")
    period_label = req_period if req_period != "game" else game_period

    if status == "trailing" and diff < 0:
        deficit = abs(diff)
        min_deficit = _to_float(cond.get("min_deficit"))
        max_deficit = _to_float(cond.get("max_deficit", 10))
        if max_deficit is not None:
            max_deficit = max(1.0, max_deficit)

        if min_deficit is not None:
            min_deficit = max(1.0, min_deficit)
            if deficit >= min_deficit:
                return {"matched": True, "direction": "trailing {} by {}".format(period_label, deficit)}
        elif deficit < max_deficit:
            return {"matched": True, "direction": "trailing {} by {}".format(period_label, deficit)}

    if status == "winning" and diff > 0:
        min_lead = _to_float(cond.get("min_lead", 6))
        if min_lead is not None:
            min_lead = max(1.0, min_lead)
        if diff >= min_lead:
            return {"matched": True, "direction": "leading {} by {}".format(period_label, diff)}

    return {"matched": False}


def evaluate_error_rate(cond, team_ctx, context):
    """Evaluate team error count or rate.

    Config keys:
      - threshold: error count threshold
      - scope: 'game' (default, API cumulative), 'quarter', or 'half' (#120)
    """
    scope = cond.get("scope", "game")

    if scope == "quarter":
        count, period = _get_period_count(team_ctx, context, "errors")
        if count is not None:
            threshold = _to_int(cond.get("threshold", 10))
            if count >= threshold:
                return {"matched": True, "direction": "{} errors in {}".format(count, period)}
            return {"matched": False}
        # Fall through to game scope if no PBP data

    if scope == "half":
        count, period = _get_half_count(team_ctx, context, "errors")
        if count is not None:
            threshold = _to_int(cond.get("threshold", 10))
            if count >= threshold:
                return {"matched": True, "direction": "{} errors in {}".format(count, period)}
            return {"matched": False}

    # Game scope (default) — API cumulative stats
    stats = team_ctx.get("stats", {})
    errors = _to_int(stats.get("errors") or stats.get("Errors"), 0)
    handling_errors = _to_int(stats.get("handlingErrors") or stats.get("Handling Errors"), 0)
    total_errors = errors + handling_errors

    threshold = _quarter_scaled_threshold(cond, context, _to_int(cond.get("threshold", 10)))
    if total_errors >= threshold:
        return {
            "matched": True,
            "direction": "{} errors ({}+{} handling)".format(total_errors, errors, handling_errors),
        }
    return {"matched": False}


def evaluate_completion_rate(cond, team_ctx, context):
    """Evaluate set completion rate.

    Config keys:
      - threshold: completion percentage (e.g. 70 means below 70% triggers)
      - direction: 'below' (default) or 'above'
    """
    stats = team_ctx.get("stats", {})
    completion = _to_float(
        stats.get("completionRate") or stats.get("Set Completion %") or
        stats.get("setCompletionRate")
    )
    if completion is None:
        return {"matched": False}

    threshold = _to_float(cond.get("threshold", 70))
    direction = cond.get("direction", "below")

    if direction == "below" and completion < threshold:
        return {"matched": True, "direction": "completion {:.0f}%".format(completion)}
    if direction == "above" and completion > threshold:
        return {"matched": True, "direction": "completion {:.0f}%".format(completion)}
    return {"matched": False}


def evaluate_penalty_count(cond, team_ctx, context):
    """Evaluate penalties conceded.

    Config keys:
      - threshold: penalty count
      - scope: 'game' (default, API cumulative), 'quarter', or 'half' (#120)
    """
    scope = cond.get("scope", "game")

    if scope == "quarter":
        count, period = _get_period_count(team_ctx, context, "penalties")
        if count is not None:
            threshold = _to_int(cond.get("threshold", 8))
            if count >= threshold:
                return {"matched": True, "direction": "{} penalties in {}".format(count, period)}
            return {"matched": False}

    if scope == "half":
        count, period = _get_half_count(team_ctx, context, "penalties")
        if count is not None:
            threshold = _to_int(cond.get("threshold", 8))
            if count >= threshold:
                return {"matched": True, "direction": "{} penalties in {}".format(count, period)}
            return {"matched": False}

    # Game scope (default) — API cumulative stats
    stats = team_ctx.get("stats", {})
    penalties = _to_int(
        stats.get("penaltiesConceded") or stats.get("Penalties Conceded") or
        stats.get("penalties"), 0
    )
    threshold = _quarter_scaled_threshold(cond, context, _to_int(cond.get("threshold", 8)))
    if penalties >= threshold:
        return {"matched": True, "direction": "{} penalties".format(penalties)}
    return {"matched": False}


def evaluate_possession(cond, team_ctx, context):
    """Evaluate possession percentage.

    Config keys:
      - threshold: possession percentage
      - direction: 'below' or 'above' (default 'below')
    """
    stats = team_ctx.get("stats", {})
    possession = _to_float(
        stats.get("possession") or stats.get("Possession %") or
        stats.get("possessionPercentage")
    )
    if possession is None:
        return {"matched": False}

    threshold = _to_float(cond.get("threshold", 45))
    direction = cond.get("direction", "below")

    if direction == "below" and possession < threshold:
        return {"matched": True, "direction": "possession {:.0f}%".format(possession)}
    if direction == "above" and possession > threshold:
        return {"matched": True, "direction": "possession {:.0f}%".format(possession)}
    return {"matched": False}


def evaluate_try_scoring_run(cond, team_ctx, context):
    """Evaluate consecutive tries by one team without the opponent scoring.

    Config keys:
      - threshold: minimum consecutive tries (default 2)
    """
    # This requires event-level data; check context for scoring events
    scoring_run = team_ctx.get("consecutive_tries", 0)
    threshold = _to_int(cond.get("threshold", 2))
    if scoring_run >= threshold:
        return {
            "matched": True,
            "direction": "{} consecutive tries".format(scoring_run),
        }
    return {"matched": False}


def evaluate_points_run(cond, team_ctx, context):
    """Evaluate unanswered points scored.

    Config keys:
      - threshold: minimum unanswered points (default 12)
    """
    unanswered = team_ctx.get("unanswered_points", 0)
    threshold = _to_int(cond.get("threshold", 12))
    if unanswered >= threshold:
        return {
            "matched": True,
            "direction": "{} unanswered points".format(unanswered),
        }
    return {"matched": False}


def evaluate_missed_tackles(cond, team_ctx, context):
    """Evaluate missed tackles count.

    Config keys:
      - threshold: missed tackle count
    """
    stats = team_ctx.get("stats", {})
    missed = _to_int(
        stats.get("missedTackles") or stats.get("Missed Tackles"), 0
    )
    threshold = _quarter_scaled_threshold(cond, context, _to_int(cond.get("threshold", 25)))
    if missed >= threshold:
        return {"matched": True, "direction": "{} missed tackles".format(missed)}
    return {"matched": False}


# ── PBP-based conditions ─────────────────────────────────────────────────────

def evaluate_momentum_shift(cond, team_ctx, context):
    """Evaluate scoring momentum — team scored X+ unanswered points.

    Uses PBP features from timeline_features if available,
    falls back to team_ctx.unanswered_points for live monitoring.

    Config keys:
      - threshold: minimum unanswered points (default 12)
    """
    # Try PBP feature first (from enriched game history)
    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    unanswered = pbp.get("{}_max_unanswered_points".format(side), 0)
    if not unanswered:
        unanswered = team_ctx.get("unanswered_points", 0)

    threshold = _to_int(cond.get("threshold", 12))
    if unanswered >= threshold:
        return {"matched": True, "direction": "{} unanswered points".format(unanswered)}
    return {"matched": False}


def evaluate_error_streak(cond, team_ctx, context):
    """Evaluate consecutive errors by this team.

    Config keys:
      - threshold: minimum consecutive errors (default 3)
    """
    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    streak = pbp.get("{}_max_error_streak".format(side), 0)
    threshold = _to_int(cond.get("threshold", 3))
    if streak >= threshold:
        return {"matched": True, "direction": "{} consecutive errors".format(streak)}
    return {"matched": False}


def evaluate_penalty_pressure(cond, team_ctx, context):
    """Evaluate penalties in a 10-minute window.

    Config keys:
      - threshold: minimum penalties in 10-min window (default 3)
    """
    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    pen_window = pbp.get("{}_max_penalty_window_10m".format(side), 0)
    threshold = _to_int(cond.get("threshold", 3))
    if pen_window >= threshold:
        return {"matched": True, "direction": "{} penalties in 10min".format(pen_window)}
    return {"matched": False}


def evaluate_sin_bin(cond, team_ctx, context):
    """Evaluate sin bin events (team down to 12 players).

    Config keys:
      - threshold: minimum sin bins (default 1)
    """
    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    sinbins = pbp.get("{}_sin_bins".format(side), 0)
    threshold = _to_int(cond.get("threshold", 1))
    if sinbins >= threshold:
        return {"matched": True, "direction": "{} sin bin{}".format(sinbins, "s" if sinbins > 1 else "")}
    return {"matched": False}


def evaluate_line_break_surge(cond, team_ctx, context):
    """Evaluate line break concentration in a 10-minute window.

    Config keys:
      - threshold: minimum line breaks in 10-min window (default 3)
    """
    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    surge = pbp.get("{}_max_line_break_surge_10m".format(side), 0)
    threshold = _to_int(cond.get("threshold", 3))
    if surge >= threshold:
        return {"matched": True, "direction": "{} line breaks in 10min".format(surge)}
    return {"matched": False}


def evaluate_halftime_turnaround(cond, team_ctx, context):
    """Evaluate if team was trailing at halftime but is now winning/close.

    Only fires at full time (backfill) or in 2H (live).

    Config keys:
      - min_ht_deficit: minimum HT deficit to qualify (default 6)
    """
    period = context.get("period", "")
    if period not in ("2H", "FT"):
        return {"matched": False}

    pbp = team_ctx.get("pbp_features", {})
    side = team_ctx.get("team_side", "home")
    ht_margin = pbp.get("halftime_margin", 0)

    # Convert to team perspective
    team_ht_margin = ht_margin if side == "home" else -ht_margin
    diff_total = team_ctx.get("diff_total", 0)

    min_deficit = _to_int(cond.get("min_ht_deficit", 6))
    if team_ht_margin <= -min_deficit and diff_total > 0:
        return {
            "matched": True,
            "direction": "trailed by {} at HT, now leading by {}".format(abs(team_ht_margin), diff_total),
        }
    return {"matched": False}


# ── Stats-based conditions (new) ──────────────────────────────────────────────

def _get_stat(team_ctx, *keys):
    """Get a stat value trying multiple key names."""
    stats = team_ctx.get("stats", {})
    for k in keys:
        v = stats.get(k)
        if v is not None:
            return _to_float(v)
    return None


def _get_period_count(team_ctx, context, stat_key):
    """Get event count for the current quarter or half from PBP features (#120).

    Args:
        team_ctx: team context dict (with pbp_features)
        context: evaluation context (with game_minute)
        stat_key: base stat key e.g. 'errors', 'penalties', 'line_breaks'

    Returns:
        (count, period_label) or (None, None) if not available
    """
    pbp = team_ctx.get("pbp_features", {})
    if not pbp:
        return None, None
    side = team_ctx.get("team_side", "home")
    game_minute = context.get("game_minute", 0) or 0

    # Determine current quarter
    if game_minute < 20:
        quarter = "Q1"
    elif game_minute < 40:
        quarter = "Q2"
    elif game_minute < 60:
        quarter = "Q3"
    elif game_minute < 80:
        quarter = "Q4"
    else:
        quarter = "ET"

    key = "{}_{}_{}".format(side, stat_key, quarter)
    count = pbp.get(key, 0)
    return count, quarter


def _get_half_count(team_ctx, context, stat_key):
    """Get event count for the current half from PBP features (#120)."""
    pbp = team_ctx.get("pbp_features", {})
    if not pbp:
        return None, None
    side = team_ctx.get("team_side", "home")
    game_minute = context.get("game_minute", 0) or 0
    half = "H1" if game_minute < 40 else "H2"
    key = "{}_{}_{}".format(side, stat_key, half)
    count = pbp.get(key, 0)
    return count, half


def _quarter_scaled_threshold(cond, context, threshold):
    """Scale a cumulative threshold by game progress when quarter_scale is enabled (#69).

    At minute 0 the threshold is ~0, at minute 80 it's the full value.
    This prevents cumulative stat conditions (run metres, errors, etc.) from
    firing misleadingly early when totals are naturally low.
    """
    if not cond.get("quarter_scale"):
        return threshold
    game_minute = context.get("game_minute", 0) or 0
    scale = min(game_minute / 80.0, 1.0)
    if scale <= 0:
        scale = 0.01  # avoid zero threshold
    return threshold * scale


def evaluate_run_metres(cond, team_ctx, context):
    """All run metres — measures forward momentum.

    Config: threshold (default 2000), direction 'above'(default) or 'below'.
    Positive: >2000 = dominant running game.  Negative: <1400 = poor.
    """
    val = _get_stat(team_ctx, "allRunMetres", "All Run Metres")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 2000)))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} run metres".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} run metres".format(val)}
    return {"matched": False}


def evaluate_post_contact_metres(cond, team_ctx, context):
    """Post contact metres — measures physical dominance in contact.

    Config: threshold (default 700), direction 'above'(default) or 'below'.
    Positive: >700 = hard to bring down.
    """
    val = _get_stat(team_ctx, "postContactMetres", "Post Contact Metres")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 700)))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} post-contact metres".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} post-contact metres".format(val)}
    return {"matched": False}


def evaluate_tackle_breaks(cond, team_ctx, context):
    """Tackle breaks — measures ability to beat defenders.

    Config: threshold (default 40), direction 'above'(default) or 'below'.
    Positive: >40 = very hard to stop.
    """
    val = _get_stat(team_ctx, "tackleBreaks", "Tackle Breaks")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 40)))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} tackle breaks".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} tackle breaks".format(val)}
    return {"matched": False}


def evaluate_line_breaks(cond, team_ctx, context):
    """Line breaks — measures attacking penetration.

    Config: threshold (default 7), direction 'above'(default) or 'below'.
    Positive: >7 = creating lots of opportunities.
    scope: 'game' (default), 'quarter', or 'half' (#120)
    """
    scope = cond.get("scope", "game")
    direction = cond.get("direction", "above")

    if scope == "quarter":
        count, period = _get_period_count(team_ctx, context, "line_breaks")
        if count is not None:
            threshold = _to_float(cond.get("threshold", 7))
            if direction == "above" and count >= threshold:
                return {"matched": True, "direction": "{} line breaks in {}".format(count, period)}
            if direction == "below" and count <= threshold:
                return {"matched": True, "direction": "{} line breaks in {}".format(count, period)}
            return {"matched": False}

    if scope == "half":
        count, period = _get_half_count(team_ctx, context, "line_breaks")
        if count is not None:
            threshold = _to_float(cond.get("threshold", 7))
            if direction == "above" and count >= threshold:
                return {"matched": True, "direction": "{} line breaks in {}".format(count, period)}
            if direction == "below" and count <= threshold:
                return {"matched": True, "direction": "{} line breaks in {}".format(count, period)}
            return {"matched": False}

    # Game scope (default) — API cumulative stats
    val = _get_stat(team_ctx, "lineBreaks", "Line Breaks")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 7)))
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} line breaks".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} line breaks".format(val)}
    return {"matched": False}


def evaluate_offloads(cond, team_ctx, context):
    """Offloads — measures creativity in attack / ball-playing forwards.

    Config: threshold (default 14), direction 'above'(default) or 'below'.
    Positive: >14 = creative, ball-playing style.
    """
    val = _get_stat(team_ctx, "offloads", "Offloads")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 14)))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} offloads".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} offloads".format(val)}
    return {"matched": False}


def evaluate_intercepts(cond, team_ctx, context):
    """Intercepts — measures defensive reads / turnovers forced.

    Config: threshold (default 2).
    Positive: >2 = reading the play well, creating scoring chances.
    """
    val = _get_stat(team_ctx, "intercepts", "Intercepts")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 2)))
    if val >= threshold:
        return {"matched": True, "direction": "{:.0f} intercepts".format(val)}
    return {"matched": False}


def evaluate_ineffective_tackles(cond, team_ctx, context):
    """Ineffective tackles — defender makes contact but doesn't stop momentum.

    Config: threshold (default 18).
    Negative: >18 = weak tackling technique, getting run over.
    """
    val = _get_stat(team_ctx, "ineffectiveTackles", "Ineffective Tackles")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 18)))
    if val >= threshold:
        return {"matched": True, "direction": "{:.0f} ineffective tackles".format(val)}
    return {"matched": False}


def evaluate_effective_tackle_pct(cond, team_ctx, context):
    """Effective tackle percentage — overall tackling efficiency.

    Config: threshold (default 85), direction 'below'(default) or 'above'.
    Negative: <85% = leaking metres, poor defence.  Positive: >92% = brick wall.
    """
    val = _get_stat(team_ctx, "effectiveTackle", "Effective Tackle %")
    if val is None:
        return {"matched": False}
    threshold = _to_float(cond.get("threshold", 85))
    direction = cond.get("direction", "below")
    if direction == "below" and val < threshold:
        return {"matched": True, "direction": "effective tackle {:.1f}%".format(val)}
    if direction == "above" and val > threshold:
        return {"matched": True, "direction": "effective tackle {:.1f}%".format(val)}
    return {"matched": False}


def evaluate_kick_defusal(cond, team_ctx, context):
    """Kick defusal percentage — ability to handle high balls and bombs.

    Config: threshold (default 60), direction 'below'(default) or 'above'.
    Negative: <60% = vulnerable under high ball, likely conceding from kicks.
    """
    val = _get_stat(team_ctx, "kickDefusal", "Kick Defusal %")
    if val is None:
        return {"matched": False}
    threshold = _to_float(cond.get("threshold", 60))
    direction = cond.get("direction", "below")
    if direction == "below" and val < threshold:
        return {"matched": True, "direction": "kick defusal {:.0f}%".format(val)}
    if direction == "above" and val > threshold:
        return {"matched": True, "direction": "kick defusal {:.0f}%".format(val)}
    return {"matched": False}


def evaluate_kick_return_metres(cond, team_ctx, context):
    """Kick return metres — measures ability to gain field position off kicks.

    Config: threshold (default 250), direction 'above'(default) or 'below'.
    Positive: >250 = strong kick returns, gaining territory.
    """
    val = _get_stat(team_ctx, "kickReturnMetres", "Kick Return Metres")
    if val is None:
        return {"matched": False}
    threshold = _quarter_scaled_threshold(cond, context, _to_float(cond.get("threshold", 250)))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.0f} kick return metres".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.0f} kick return metres".format(val)}
    return {"matched": False}


def evaluate_play_the_ball_speed(cond, team_ctx, context):
    """Average play-the-ball speed — measures fatigue and tempo.

    Config: threshold (default 4.0), direction 'above'(default, slow=bad) or 'below'(fast=good).
    Negative: >4.0s = fatigued/slow, losing tempo.  Positive: <3.0s = fast, high tempo.
    """
    val = _get_stat(team_ctx, "averagePlayTheBallSpeed", "Average Play The Ball Speed")
    if val is None:
        return {"matched": False}
    threshold = _to_float(cond.get("threshold", 4.0))
    direction = cond.get("direction", "above")
    if direction == "above" and val >= threshold:
        return {"matched": True, "direction": "{:.2f}s play-the-ball".format(val)}
    if direction == "below" and val <= threshold:
        return {"matched": True, "direction": "{:.2f}s play-the-ball".format(val)}
    return {"matched": False}


def evaluate_red_card(cond, team_ctx, context):
    """Red card / sent off — player permanently dismissed.

    Config: threshold (default 1).
    Major event: team plays remainder with 12 (or fewer) players.
    """
    # Check stats first
    val = _get_stat(team_ctx, "sendOffs", "Send Offs")
    # Also check PBP for SendOff events
    if val is None or val == 0:
        pbp = team_ctx.get("pbp_features", {})
        side = team_ctx.get("team_side", "home")
        # sendOffs might be in timeline events — check if available
        val = val or 0
    if val is None:
        return {"matched": False}
    threshold = _to_int(cond.get("threshold", 1))
    if val >= threshold:
        return {"matched": True, "direction": "{:.0f} send off{}".format(val, "s" if val > 1 else "")}
    return {"matched": False}


def evaluate_first_team_scores(cond, team_ctx, context):
    """First team to score — fires once for the team that scores first.

    Detects first scoring event from try_events (PBP timeline) or
    live scores when one team has points and the game just started.

    The condition uses game state to ensure it only fires once per game.
    """
    game_id = context.get("game_id", "")
    state = context.get("state", {})
    first_scorer_key = "first_scorer_{}".format(game_id)

    # Already determined for this game?
    cached = state.get(first_scorer_key)
    if cached:
        team_name = team_ctx.get("team_name", "")
        if cached.get("team") == team_name:
            return {
                "matched": True,
                "direction": "First to score ({})".format(cached.get("scoring_type", "try")),
            }
        return {"matched": False}

    # Try PBP features (backfill or live enriched)
    pbp = team_ctx.get("pbp_features", {})
    score_prog = pbp.get("score_progression", [])
    if score_prog and len(score_prog) > 1:
        # First non-zero scoring event
        first_event = score_prog[1]  # index 0 is always 0-0
        h_score = first_event.get("homeScore", 0)
        a_score = first_event.get("awayScore", 0)
        if h_score > 0 or a_score > 0:
            side = team_ctx.get("team_side", "home")
            if (side == "home" and h_score > 0) or (side == "away" and a_score > 0):
                team_name = team_ctx.get("team_name", "")
                state[first_scorer_key] = {"team": team_name, "scoring_type": "try"}
                context["state_dirty"] = True
                return {
                    "matched": True,
                    "direction": "First to score",
                    "state_dirty": True,
                }
        return {"matched": False}

    # Fallback: check try_events from match detail
    match_detail = context.get("match_detail")
    if match_detail:
        try_events = match_detail.get("try_events", [])
        if try_events:
            first = min(try_events, key=lambda t: t.get("game_seconds", 9999))
            first_team = first.get("team", "")
            team_name = team_ctx.get("team_name", "")
            if first_team == team_name:
                scoring_type = first.get("type", "try")
                state[first_scorer_key] = {"team": team_name, "scoring_type": scoring_type}
                context["state_dirty"] = True
                return {
                    "matched": True,
                    "direction": "First to score ({})".format(scoring_type),
                    "state_dirty": True,
                }
            return {"matched": False}

    # Fallback: check live scores (if one team has scored and other hasn't)
    my_score = team_ctx.get("my_score", 0)
    opp_score = team_ctx.get("opp_score", 0)
    if my_score > 0 and opp_score == 0:
        team_name = team_ctx.get("team_name", "")
        state[first_scorer_key] = {"team": team_name, "scoring_type": "unknown"}
        context["state_dirty"] = True
        return {
            "matched": True,
            "direction": "First to score",
            "state_dirty": True,
        }

    return {"matched": False}


# ── Predicted Winner condition ────────────────────────────────────────────────

def evaluate_predicted_winner_threshold(cond, team_ctx, context):
    """Evaluate whether predicted winner confidence crosses a threshold.

    Reads from context["live_analysis"] which is populated by the PW
    evaluation pass in monitor.py after all base conditions are evaluated.

    Config keys:
      - threshold: confidence percentage to trigger (default 65)
      - min_half: minimum game half (1=H1+, 2=H2+) to fire (default 1)
    """
    live_analysis = context.get("live_analysis")
    if not live_analysis:
        return {"matched": False}

    threshold = _to_float(cond.get("threshold", 65))
    min_half = _to_int(cond.get("min_half", 1))

    # Determine current half
    period = context.get("period", "")
    current_half = 0
    if period in ("1H",):
        current_half = 1
    elif period in ("HT", "2H", "FT"):
        current_half = 2
    elif period in ("ET", "GoldenPoint"):
        current_half = 2  # ET counts as 2+ for min_half purposes

    if current_half < min_half:
        return {"matched": False}

    # Find the prediction for this team
    team_name = team_ctx.get("team_name", "")
    predictions = live_analysis.get("predictions", [])

    for pred in predictions:
        if pred.get("predicted_team") != team_name:
            continue

        pct = pred.get("winner_score_pct") or pred.get("pct", 0)
        if pct >= threshold:
            consensus = pred.get("consensus", "")
            basis = pred.get("basis", "")
            blended = pred.get("blended", False)
            direction = "{:.1f}% win probability".format(pct)
            if blended:
                direction += " [blended]"
            if consensus:
                direction += " [consensus: {}]".format(consensus)

            return {
                "matched": True,
                "direction": direction,
                "predicted_team": team_name,
                "win_probability_pct": round(pct, 1),
                "consensus": consensus,
                "is_blended": blended,
                "basis": basis,
                "sample": pred.get("sample", 0),
                "conditions_count": pred.get("conditions_count", 0),
            }

    return {"matched": False}


# ── Condition dispatch table ─────────────────────────────────────────────────

CONDITION_EVALUATORS = {
    # Stats-based (original 8)
    "score_diff": evaluate_score_diff,
    "error_rate": evaluate_error_rate,
    "completion_rate": evaluate_completion_rate,
    "penalty_count": evaluate_penalty_count,
    "possession": evaluate_possession,
    "try_scoring_run": evaluate_try_scoring_run,
    "points_run": evaluate_points_run,
    "missed_tackles": evaluate_missed_tackles,
    # PBP-based (6)
    "momentum_shift": evaluate_momentum_shift,
    "error_streak": evaluate_error_streak,
    "penalty_pressure": evaluate_penalty_pressure,
    "sin_bin": evaluate_sin_bin,
    "line_break_surge": evaluate_line_break_surge,
    "halftime_turnaround": evaluate_halftime_turnaround,
    # Stats-based (new 12)
    "run_metres": evaluate_run_metres,
    "post_contact_metres": evaluate_post_contact_metres,
    "tackle_breaks": evaluate_tackle_breaks,
    "line_breaks": evaluate_line_breaks,
    "offloads": evaluate_offloads,
    "intercepts": evaluate_intercepts,
    "ineffective_tackles": evaluate_ineffective_tackles,
    "effective_tackle_pct": evaluate_effective_tackle_pct,
    "kick_defusal": evaluate_kick_defusal,
    "kick_return_metres": evaluate_kick_return_metres,
    "play_the_ball_speed": evaluate_play_the_ball_speed,
    "red_card": evaluate_red_card,
    "first_team_scores": evaluate_first_team_scores,
    "predicted_winner_threshold": evaluate_predicted_winner_threshold,
}

CONDITION_TYPES = set(CONDITION_EVALUATORS.keys())


def evaluate_condition(cond, team_ctx, context):
    """Evaluate a single condition. Returns dict with 'matched' bool and optional details."""
    cond_type = cond.get("type", "")
    evaluator = CONDITION_EVALUATORS.get(cond_type)
    if not evaluator:
        return {"matched": False, "error": "unknown condition type: {}".format(cond_type)}
    return evaluator(cond, team_ctx, context)
