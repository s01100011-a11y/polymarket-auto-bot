"""Shared condition evaluation helpers for live monitoring and replay."""

import re

import league_config


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
    quarter_num = context["quarter_num"]
    req_q = cond.get("quarter")
    if req_q and quarter_num != req_q:
        return {"matched": False}

    if cond.get("use_quarter_score"):
        diff = context["get_linescore_diff"](team_ctx["my_team"], team_ctx["opp"], quarter_num - 1)
    else:
        diff = team_ctx["diff_total"]
    if diff is None:
        return {"matched": False}

    status = cond.get("team_status")
    label_quarter = req_q or quarter_num

    if status == "trailing" and diff < 0:
        deficit = abs(diff)
        try:
            min_deficit = cond.get("min_deficit")
            max_deficit = cond.get("max_deficit", 10)
            min_deficit = float(min_deficit) if min_deficit is not None else None
            max_deficit = max(1.0, float(max_deficit))
        except (TypeError, ValueError):
            min_deficit = None
            max_deficit = 10.0
        if min_deficit is not None:
            min_deficit = max(1.0, min_deficit)
            if deficit >= min_deficit:
                return {"matched": True, "direction": "trailing Q{} by {}".format(label_quarter, deficit)}
        elif deficit < max_deficit:
            return {"matched": True, "direction": "trailing Q{} by {}".format(label_quarter, deficit)}

    if status == "winning" and diff > 0:
        try:
            min_lead = max(1.0, float(cond.get("min_lead", 5)))
        except (TypeError, ValueError):
            min_lead = 5.0
        if diff >= min_lead:
            return {"matched": True, "direction": "winning Q{} by {}".format(label_quarter, diff)}

    return {"matched": False}


def _check_min_quarter(cond, context):
    """Return True if the current quarter meets the condition's min_quarter requirement.

    If `min_quarter` is set on the condition, the condition will not fire until
    that quarter is reached. Quarter numbers: 1=Q1, 2=Q2, 3=Q3, 4=Q4, 5+=OT.
    If `min_quarter` is absent, no restriction applies.
    """
    min_q = cond.get("min_quarter")
    if min_q is None:
        return True
    current_q = int(context.get("quarter_num") or 0)
    return current_q >= int(min_q)


def evaluate_fg_percent_improve(cond, team_ctx, context):
    raw_stats = team_ctx.get("raw_stats")
    if not raw_stats:
        raw_stats = _build_pbp_raw_stats_fallback(context, team_ctx.get("team_id"))
    if not team_ctx.get("is_losing") or not raw_stats:
        return {"matched": False}
    if not _check_min_quarter(cond, context):
        return {"matched": False}

    state = context["state"]
    fg_key = "fg_pct_{}_{}".format(context["game_id"], team_ctx["team_id"])
    cur_fg = float(raw_stats.get("fieldGoalPct") or 0)
    prev = state.get(fg_key)
    state[fg_key] = cur_fg
    result = {"matched": False, "state_dirty": True}
    if prev is not None:
        improvement = cur_fg - prev
        if improvement >= cond.get("threshold", 5):
            result.update({
                "matched": True,
                "direction": "FG% {:.1f}% → {:.1f}%".format(prev, cur_fg),
            })
    return result


def evaluate_turnover_rate(cond, team_ctx, context):
    raw_stats = team_ctx.get("raw_stats")
    if not raw_stats:
        raw_stats = _build_pbp_raw_stats_fallback(context, team_ctx.get("team_id"))
    if not raw_stats:
        return {"matched": False}
    if not _check_min_quarter(cond, context):
        return {"matched": False}

    rate = context["compute_turnover_rate"](raw_stats)
    if rate is not None and rate > cond.get("threshold", 15):
        tag = " [pbp]" if raw_stats.get("_pbp_fallback") else ""
        return {"matched": True, "direction": "TOV% {:.1f}%{}".format(rate, tag)}
    return {"matched": False}


def evaluate_turnovers(cond, team_ctx, context):
    raw_stats = team_ctx.get("raw_stats")
    if not raw_stats:
        raw_stats = _build_pbp_raw_stats_fallback(context, team_ctx.get("team_id"))
    if not raw_stats:
        return {"matched": False}
    if not _check_min_quarter(cond, context):
        return {"matched": False}

    turnovers = _to_int(raw_stats.get("turnovers"), 0)
    if turnovers > cond.get("threshold", 15):
        tag = " [pbp]" if raw_stats.get("_pbp_fallback") else ""
        return {"matched": True, "direction": "{} turnovers{}".format(turnovers, tag)}
    return {"matched": False}


def evaluate_points_off_turnovers(cond, team_ctx, context):
    raw_stats = team_ctx.get("raw_stats")
    if not raw_stats:
        raw_stats = _build_pbp_raw_stats_fallback(context, team_ctx.get("team_id"))
    if not raw_stats:
        return {"matched": False}
    if not _check_min_quarter(cond, context):
        return {"matched": False}

    pot = _to_int(raw_stats.get("pointsOffTurnovers"), 0)
    if pot > cond.get("threshold", 15):
        return {"matched": True, "direction": "{} points off turnovers".format(pot)}
    return {"matched": False}


def evaluate_back_to_back(cond, team_ctx, context):
    state = context["state"]
    cache_key = "b2b_{}_{}".format(context["game_id"], team_ctx["team_id"])
    cache_known = cache_key in state
    matched = context["check_b2b"](
        team_ctx["team_id"],
        state,
        context["game_id"],
        context["yesterday_events"],
    )
    if matched:
        return {
            "matched": True,
            "direction": "playing on back-to-back nights",
            "state_dirty": not cache_known,
        }
    return {"matched": False, "state_dirty": not cache_known and cache_key in state}


def evaluate_underdog_at_home(cond, team_ctx, context):
    quarter_num = context["quarter_num"]
    if quarter_num < 3:
        return {"matched": False}

    competitors = context["competitors"]
    my_team = team_ctx["my_team"]
    if my_team.get("homeAway") != "home":
        return {"matched": False}

    main_comp = context["main_comp"]
    odds_list = main_comp.get("odds") or []
    odds_detail = (odds_list[0].get("details") or "") if odds_list else ""
    fav_id = context["parse_odds_favorite"](odds_detail, competitors)
    if fav_id is None or fav_id == team_ctx["team_id"]:
        return {"matched": False}

    halftime = context["halftime_scores"](competitors)
    home_ht = halftime.get(team_ctx["team_id"], 0)
    away_ht = next((value for key, value in halftime.items() if key != team_ctx["team_id"]), 0)
    margin = home_ht - away_ht
    if margin >= cond.get("min_lead", 3):
        return {"matched": True, "direction": "home underdog leading halftime by {}".format(int(margin))}
    return {"matched": False}


def evaluate_consecutive_points_run(cond, team_ctx, context):
    summary = context.get("summary")
    if not summary:
        return {"matched": False}

    threshold = _to_int(cond.get("threshold", 5), 5)
    threshold = max(1, threshold)

    scope_val = str(cond.get("scope", "game")).lower().strip()
    run_scope = "game"
    cache_key = "game"

    if scope_val == "quarter":
        req_q = _to_int(cond.get("quarter"), context["quarter_num"])
        req_q = max(1, req_q)
        cache_key = "q{}".format(req_q)
        run_scope = "Q{}".format(req_q)
        if cache_key not in context["scoring_runs_cache"]:
            context["scoring_runs_cache"][cache_key] = context["extract_scoring_runs"](summary, req_q)

    runs = context["scoring_runs_cache"].get(cache_key, [])
    team_runs = [
        run for run in runs
        if run.get("team_id") == team_ctx["team_id"] and _to_int(run.get("points"), 0) >= threshold
    ]
    if not team_runs:
        return {"matched": False}

    run = team_runs[-1]
    run_points = _to_int(run.get("points"), 0)
    run_anchor = (
        str(run.get("start_play_id") or "")
        or str(run.get("end_play_id") or "")
        or str(run.get("end_sequence") or "")
    )
    run_key = "{}|{}|{}|{}|{}".format(
        context["game_id"], team_ctx["team_id"], cond["name"], run_scope, run_anchor
    )
    return {
        "matched": True,
        "direction": "scored {} unanswered points in {}".format(run_points, run_scope),
        "run_key": run_key,
        "run_points": run_points,
        "run_scope": run_scope,
    }


def evaluate_predicted_winner_threshold(cond, team_ctx, context):
    """Alert when predicted winner confidence reaches a threshold.

    Reads from ``context["live_analysis"]`` — a dict of per-game analysis
    produced by ``outcomes.live_analysis_for_game()``.  The evaluator matches
    when the top "Team wins" row's blended confidence for the team being
    evaluated meets or exceeds ``cond["threshold"]``.

    Must run in a second pass *after* live analysis is computed.
    """
    if not _check_min_quarter(cond, context):
        return {"matched": False}

    live_analysis = context.get("live_analysis") or {}
    game_id = context.get("game_id")
    game_entry = live_analysis.get(game_id) or {}
    analysis = game_entry.get("analysis") or {}
    preds = analysis.get("predictions") or []
    suppressed_preds = analysis.get("suppressed_predictions") or []

    team_name = team_ctx.get("team_name", "")
    threshold = _to_float(cond.get("threshold"), 70.0)

    # Find "Team wins" rows for this team — check both active and suppressed
    team_win_rows = [
        p for p in preds
        if "team wins" in str(p.get("label", "")).lower()
        and str(p.get("predicted_team") or "").strip()
    ]
    suppressed_team_win_rows = [
        p for p in suppressed_preds
        if "team wins" in str(p.get("label", "")).lower()
        and str(p.get("predicted_team") or "").strip()
    ]
    if not team_win_rows and not suppressed_team_win_rows:
        return {"matched": False}

    # Score each row: prefer winner_score_pct (blended), fall back to pct
    def _score(row):
        ws = row.get("winner_score_pct")
        try:
            v = float(ws)
            if v == v:  # not NaN
                return v
        except (TypeError, ValueError):
            pass
        try:
            return float(row.get("pct") or 0)
        except (TypeError, ValueError):
            return 0.0

    # Pick from active first; fall back to suppressed (#221)
    _is_suppressed = False
    _suppress_reason = ""
    if team_win_rows:
        top_row = max(team_win_rows, key=_score)
    elif suppressed_team_win_rows:
        top_row = max(suppressed_team_win_rows, key=_score)
        _is_suppressed = True
        _suppress_reason = str(top_row.get("_suppress_reason") or "filtered")
    else:
        return {"matched": False}

    top_score = _score(top_row)
    pred_team = str(top_row.get("predicted_team") or "").strip()

    # Only fire for the team that is the predicted winner
    if pred_team != team_name:
        return {"matched": False}

    # Use the higher of the condition threshold and the PW alert adaptive
    # threshold (favorite/underdog) so PW_THRESHOLD alerts only fire when a
    # PW call would also be recorded (#238).
    pw_adaptive = context.get("pw_adaptive_threshold")
    if pw_adaptive is not None:
        threshold = max(threshold, float(pw_adaptive))
    if top_score < threshold:
        return {"matched": False}

    consensus = str(top_row.get("consensus") or "").strip()
    is_blended = top_row.get("winner_score_pct") is not None
    basis = str(top_row.get("basis") or "").strip()
    sample = top_row.get("sample", 0)
    conditions_list = top_row.get("conditions") or []

    blended_tag = " [blended]" if is_blended else ""
    consensus_tag = " [consensus: {}]".format(consensus.replace("_", " ")) if consensus else ""

    direction = "{:.1f}% win probability{}{}".format(top_score, blended_tag, consensus_tag)

    result = {
        "matched": True,
        "direction": direction,
        "predicted_team": pred_team,
        "predicted_team_id": str(top_row.get("predicted_team_id") or pred_team).strip(),
        "win_probability_pct": round(top_score, 1),
        "consensus": consensus,
        "is_blended": is_blended,
        "basis": basis,
        "sample": sample,
        "conditions_count": len(conditions_list),
    }
    if _is_suppressed:
        result["_suppressed"] = True
        result["_suppress_reason"] = _suppress_reason
    return result


def evaluate_h1_moneyline_edge(cond, team_ctx, context):
    required_role = cond.get("role")
    my_role = team_ctx["my_team"].get("homeAway")
    if my_role != required_role:
        return {"matched": False}

    # Use pre-built evanalytics_records from context when available (backfill/replay path).
    # This avoids a live network fetch and works correctly with h1_store-derived records.
    # Fall back to live EVAnalytics fetch only when pre-built records are absent.
    evanalytics = context["evanalytics"]
    pre_built = context.get("evanalytics_records")
    if isinstance(pre_built, dict) and pre_built:
        ml_records = pre_built
    else:
        ml_records = evanalytics.fetch_1h_ml_records(context["config"])
    record = evanalytics.get_team_record(ml_records, team_ctx["team_name"])
    if not record:
        return {"matched": False}

    l10_w = record.get("l10_{}_w".format(my_role))
    l10_l = record.get("l10_{}_l".format(my_role))
    if l10_w is None or l10_l is None:
        return {"matched": False}

    l10_min = cond.get("l10_min_wins")
    l10_max = cond.get("l10_max_wins")
    min_ok = (l10_min is None) or (l10_w >= l10_min)
    max_ok = (l10_max is None) or (l10_w <= l10_max)

    if min_ok and max_ok:
        return {
            "matched": True,
            "direction": "1H ML L10 {}: {}-{}".format(my_role, l10_w, l10_l),
        }
    return {"matched": False}


def compute_turnovers_from_pbp(plays, team_id):
    """Count turnovers for a team from PBP plays.

    Returns total turnover count.  Matches ESPN's ``totalTurnovers`` field
    (includes team turnovers like shot-clock violations).
    """
    team_id_str = str(team_id)
    count = 0
    for p in (plays or []):
        ptype_text = str((p.get("type") or {}).get("text") or "").lower()
        if "turnover" not in ptype_text:
            continue
        tid = str((p.get("team") or {}).get("id") or "")
        if tid == team_id_str:
            count += 1
    return count


def compute_fta_from_pbp(plays, team_id):
    """Count free-throw attempts for a team from PBP plays."""
    team_id_str = str(team_id)
    count = 0
    for p in (plays or []):
        if not _is_free_throw(p):
            continue
        tid = str((p.get("team") or {}).get("id") or "")
        if tid == team_id_str:
            count += 1
    return count


def _build_pbp_raw_stats_fallback(context, team_id):
    """Build a partial raw_stats dict from PBP data when boxscore stats are missing.

    Returns a dict with turnovers, fieldGoalsAttempted, freeThrowsAttempted,
    or None if PBP data is unavailable.
    """
    summary = context.get("summary")
    if not isinstance(summary, dict):
        return None
    plays = summary.get("plays")
    if not plays:
        return None
    fg = compute_quarter_fg_from_pbp(plays, team_id)
    fga = sum(q["fga"] for q in fg.values())
    fgm = sum(q["fgm"] for q in fg.values())
    fta = compute_fta_from_pbp(plays, team_id)
    turnovers = compute_turnovers_from_pbp(plays, team_id)
    fg_pct = round(fgm / float(fga) * 100.0, 1) if fga > 0 else 0.0
    return {
        "turnovers": turnovers,
        "fieldGoalsAttempted": fga,
        "fieldGoalsMade": fgm,
        "fieldGoalPct": fg_pct,
        "freeThrowsAttempted": fta,
        "_pbp_fallback": True,
    }


def compute_quarter_fg_from_pbp(plays, team_id):
    """Derive per-quarter FGM/FGA from PBP plays (excluding free throws).

    Returns a dict keyed by 1-based period number:
        {1: {"fgm": 8, "fga": 20}, 2: {"fgm": 10, "fga": 18}, ...}

    Used as a fallback when ESPN linescores carry only points (no fga/fgm).
    """
    team_id_str = str(team_id)
    quarters = {}
    for p in (plays or []):
        if not p.get("shootingPlay"):
            continue
        if _is_free_throw(p):
            continue
        tid = str((p.get("team") or {}).get("id") or "")
        if tid != team_id_str:
            continue
        pn = int((p.get("period") or {}).get("number") or 0)
        if pn < 1:
            continue
        if pn not in quarters:
            quarters[pn] = {"fgm": 0, "fga": 0}
        quarters[pn]["fga"] += 1
        if p.get("scoringPlay"):
            quarters[pn]["fgm"] += 1
    return quarters


def evaluate_fg_pct_threshold(cond, team_ctx, context):
    """Alert when FG% crosses above/below threshold in a specific quarter, half, or overall."""
    raw_stats = team_ctx.get("raw_stats")
    if not raw_stats:
        raw_stats = _build_pbp_raw_stats_fallback(context, team_ctx.get("team_id"))
    if not raw_stats:
        return {"matched": False}

    threshold = _to_float(cond.get("threshold"), 50.0)
    direction_val = str(cond.get("direction", "above")).lower().strip()
    scope_val = str(cond.get("scope", "game")).lower().strip()
    min_fga = _to_int(cond.get("min_fga"), 0)  # minimum FGA to fire (#320)

    # PBP fallback: derive per-quarter FGM/FGA from summary["plays"] when
    # ESPN linescores carry only points (no fga/fgm). Lazy-computed once per
    # evaluation and only when the primary linescore source lacks FG data.
    def _pbp_quarter_fg():
        summary = context.get("summary")
        if not isinstance(summary, dict):
            return {}
        return compute_quarter_fg_from_pbp(
            summary.get("plays"), team_ctx.get("team_id"))

    # Determine which FG% to use based on scope
    current_q = context.get("quarter_num", 0)
    if scope_val == "quarter":
        req_q = _to_int(cond.get("quarter"), current_q)
        req_q = max(1, min(4, req_q))
        # Only fire during the configured quarter — stale data from a
        # completed quarter should not re-trigger in later periods.
        if current_q != req_q:
            return {"matched": False}
        # Get quarter-specific FG%
        linescore = context.get("linescore_for_team")

        q_fga = 0
        q_fgm = 0
        if linescore:
            q_idx = req_q - 1
            if q_idx < len(linescore):
                q_data = linescore[q_idx]
                q_fga = _to_int(q_data.get("fga"), 0)
                q_fgm = _to_int(q_data.get("fgm"), 0)

        # PBP fallback when linescore lacks FG data
        if q_fga == 0:
            pbp_q = _pbp_quarter_fg().get(req_q)
            if pbp_q:
                q_fga = pbp_q["fga"]
                q_fgm = pbp_q["fgm"]

        if q_fga == 0:
            return {"matched": False}
        if min_fga > 0 and q_fga < min_fga:
            return {"matched": False}
        fg_pct = (q_fgm / float(q_fga)) * 100.0
        scope_label = "Q{}".format(req_q)

    elif scope_val == "half":
        half_val = _to_int(cond.get("half"), 1)  # 1 for H1, 2 for H2
        if half_val not in (1, 2):
            return {"matched": False}
        # Only fire while the game is in the configured half — prevent
        # stale data from re-triggering in later periods (e.g. H2 in OT).
        current_half = 1 if current_q <= 2 else 2
        if current_q > 4 or current_half != half_val:
            return {"matched": False}

        scope_label = "H1" if half_val == 1 else "H2"
        split_key = "h1" if half_val == 1 else "h2"

        # Primary source: precomputed split stats from monitor.py summary parsing.
        # ESPN linescores typically only carry points per period, not FGM/FGA.
        split_stats = team_ctx.get("stats_split") or {}
        split_half = split_stats.get(split_key) if isinstance(split_stats, dict) else None
        fg_pct = None

        if isinstance(split_half, dict):
            half_fga = _to_float(split_half.get("fg_attempted"), 0.0)
            half_fgm = _to_float(split_half.get("fg_made"), 0.0)
            if min_fga > 0 and half_fga < min_fga:
                return {"matched": False}
            fg_pct = _to_float(split_half.get("fg_pct"), None)
            if fg_pct is None and half_fga > 0:
                fg_pct = (half_fgm / half_fga) * 100.0

        if fg_pct is None:
            # Fallback: per-period FGM/FGA from linescore or PBP.
            linescore = context.get("linescore_for_team")
            q_indices = (1, 2) if half_val == 1 else (3, 4)  # 1-based period numbers
            total_fgm = 0
            total_fga = 0

            # Try linescore first
            if linescore:
                for q_num in q_indices:
                    q_idx = q_num - 1
                    if q_idx < len(linescore):
                        q_data = linescore[q_idx]
                        total_fgm += _to_int(q_data.get("fgm"), 0)
                        total_fga += _to_int(q_data.get("fga"), 0)

            # PBP fallback
            if total_fga == 0:
                pbp_qfg = _pbp_quarter_fg()
                for q_num in q_indices:
                    pbp_q = pbp_qfg.get(q_num)
                    if pbp_q:
                        total_fgm += pbp_q["fgm"]
                        total_fga += pbp_q["fga"]

            if total_fga == 0:
                return {"matched": False}
            if min_fga > 0 and total_fga < min_fga:
                return {"matched": False}
            fg_pct = (total_fgm / float(total_fga)) * 100.0

    else:  # scope == "game"
        fga = _to_float(raw_stats.get("fieldGoalsAttempted"), 0)
        fgm = _to_float(raw_stats.get("fieldGoalsMade"), 0)
        if fga == 0:
            return {"matched": False}
        if min_fga > 0 and fga < min_fga:
            return {"matched": False}
        fg_pct = (fgm / fga) * 100.0
        scope_label = "Game"

    # Check direction
    if direction_val == "above" and fg_pct > threshold:
        return {
            "matched": True,
            "direction": "{} FG% {:.1f}% > {:.1f}%".format(scope_label, fg_pct, threshold),
        }
    elif direction_val == "below" and fg_pct < threshold:
        return {
            "matched": True,
            "direction": "{} FG% {:.1f}% < {:.1f}%".format(scope_label, fg_pct, threshold),
        }
    elif direction_val == "equals":
        # Equality match: exact FG% within 0.5pp
        if abs(fg_pct - threshold) < 0.5:
            return {
                "matched": True,
                "direction": "{} FG% {:.1f}% ≈ {:.1f}%".format(scope_label, fg_pct, threshold),
            }

    return {"matched": False}


# ── PBP-derived FG% helpers (issue #117) ─────────────────────────────────────

_CLOCK_RE = re.compile(r"^(\d+):(\d+)$")
_PERIOD_SECONDS = league_config.PERIOD_SECONDS


def _clock_to_remaining(display_value):
    """Parse 'MM:SS' clock string → remaining seconds in the period."""
    m = _CLOCK_RE.match((display_value or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def _period_start_seconds(period_num):
    """Cumulative game seconds at the START of a period (1-based)."""
    if period_num <= 4:
        return (period_num - 1) * _PERIOD_SECONDS["regulation"]
    # Overtime periods
    return (4 * _PERIOD_SECONDS["regulation"]
            + (period_num - 5) * _PERIOD_SECONDS["overtime"])


def _period_length(period_num):
    return _PERIOD_SECONDS["regulation"] if period_num <= 4 else _PERIOD_SECONDS["overtime"]


def _is_free_throw(play):
    t = ((play.get("type") or {}).get("text") or "").lower()
    return "free throw" in t


def compute_fg_timeline_from_pbp(plays, team_id, tick_seconds=60):
    """Build cumulative FG% at virtual tick boundaries from PBP plays.

    Returns a list of tick dicts sorted by game_seconds:
        {game_seconds, period, fgm, fga, fg_pct, home_score, away_score}

    Only field-goal attempts (shootingPlay=True, excluding free throws) are
    counted.  Tick boundaries align to ``tick_seconds`` intervals from game
    start. Each tick carries the cumulative FGM/FGA up to that point and the
    score at the last play before the tick boundary.
    """
    team_id_str = str(team_id)
    cum_fgm = 0
    cum_fga = 0
    last_home = 0
    last_away = 0
    last_period = 1

    # Collect (game_seconds, event) tuples for all plays
    play_events = []
    for p in (plays or []):
        pn = int((p.get("period") or {}).get("number") or 0)
        if pn < 1:
            continue
        remaining = _clock_to_remaining((p.get("clock") or {}).get("displayValue"))
        if remaining is None:
            continue
        gs = _period_start_seconds(pn) + (_period_length(pn) - remaining)
        play_events.append((gs, pn, p))

    play_events.sort(key=lambda x: x[0])

    # Walk plays, emit a tick snapshot at each tick boundary crossed
    ticks = []
    next_tick = tick_seconds
    prev_fg_pct = None

    for gs, pn, p in play_events:
        hs = _to_int(p.get("homeScore"), last_home)
        aws = _to_int(p.get("awayScore"), last_away)
        last_home = hs
        last_away = aws
        last_period = pn

        # Count FGA/FGM for the target team (field goals only, not FTs)
        if p.get("shootingPlay") and not _is_free_throw(p):
            tid = str((p.get("team") or {}).get("id") or "")
            if tid == team_id_str:
                cum_fga += 1
                if p.get("scoringPlay"):
                    cum_fgm += 1

        # Emit ticks as game_seconds crosses boundaries
        while next_tick <= gs:
            fg_pct = round(cum_fgm / cum_fga * 100, 1) if cum_fga > 0 else None
            ticks.append({
                "game_seconds": next_tick,
                "period": last_period,
                "fgm": cum_fgm,
                "fga": cum_fga,
                "fg_pct": fg_pct,
                "home_score": last_home,
                "away_score": last_away,
            })
            next_tick += tick_seconds

    # Final tick at end of available data (if not already emitted)
    if play_events:
        final_gs = play_events[-1][0]
        if next_tick > final_gs:
            fg_pct = round(cum_fgm / cum_fga * 100, 1) if cum_fga > 0 else None
            ticks.append({
                "game_seconds": final_gs,
                "period": last_period,
                "fgm": cum_fgm,
                "fga": cum_fga,
                "fg_pct": fg_pct,
                "home_score": last_home,
                "away_score": last_away,
            })

    return ticks


def evaluate_fg_improve_from_pbp(plays, team_id, home_id, config_cond=None,
                                  threshold=5, min_quarter=2, lookback_ticks=2):
    """Walk PBP-derived FG% ticks; return condition hits matching the live evaluator.

    Fires when: FG% improves by >= ``threshold`` pp compared to ANY of the
    preceding ``lookback_ticks`` ticks (default 2) AND the team is losing at
    the current tick AND the tick falls in >= ``min_quarter``.

    The lookback window accounts for the live monitor's poll-timing jitter —
    consecutive 60s ticks can split a large improvement that the live monitor
    captured in a single poll interval. A lookback of 2 ticks (~120s) matches
    the realistic range of consecutive poll gaps.

    ``config_cond`` is the config.json condition dict (optional — used for
    name/threshold/min_quarter overrides). ``home_id`` determines is_losing
    and home_away_role.

    Returns a list of dicts shaped like ``conditions_fired`` entries.
    """
    if config_cond:
        threshold = config_cond.get("threshold", threshold)
        min_quarter = config_cond.get("min_quarter", min_quarter)
        cond_name = config_cond.get("name", "fg_percent_improve")
    else:
        cond_name = "fg_percent_improve"

    team_id_str = str(team_id)
    home_id_str = str(home_id)
    is_home = (team_id_str == home_id_str)
    home_away_role = "home" if is_home else "away"

    ticks = compute_fg_timeline_from_pbp(plays, team_id)
    hits = []
    # Track last-fired game_seconds to suppress duplicate hits from
    # overlapping lookback windows within the same burst.
    last_fired_gs = -9999

    for i in range(1, len(ticks)):
        cur = ticks[i]
        if cur["fg_pct"] is None:
            continue
        if cur["period"] < min_quarter:
            continue

        # Find best improvement across lookback window
        best_improvement = 0
        best_prev = None
        for j in range(max(0, i - lookback_ticks), i):
            prev = ticks[j]
            if prev["fg_pct"] is None:
                continue
            imp = cur["fg_pct"] - prev["fg_pct"]
            if imp > best_improvement:
                best_improvement = imp
                best_prev = prev

        if best_improvement < threshold or best_prev is None:
            continue

        # is_losing check: team's score < opponent's score
        if is_home:
            my_score = cur["home_score"]
            opp_score = cur["away_score"]
        else:
            my_score = cur["away_score"]
            opp_score = cur["home_score"]

        if my_score >= opp_score:
            continue

        # Suppress duplicate hits from overlapping lookback windows
        if cur["game_seconds"] - last_fired_gs < 60:
            continue

        margin = my_score - opp_score
        # Quarter elapsed pct within the current period
        pl = _period_length(cur["period"])
        ps = _period_start_seconds(cur["period"])
        elapsed_in_period = cur["game_seconds"] - ps
        q_elapsed_pct = round(elapsed_in_period / pl * 100, 1) if pl > 0 else 0.0

        quarter_names = {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}
        qstr = quarter_names.get(cur["period"], "OT{}".format(cur["period"] - 4) if cur["period"] > 4 else "Q{}".format(cur["period"]))

        hits.append({
            "name": cond_name,
            "type": "fg_percent_improve",
            "team_id": team_id_str,
            "quarter_str": qstr,
            "direction": "FG% {:.1f}% \u2192 {:.1f}%".format(best_prev["fg_pct"], cur["fg_pct"]),
            "score_margin_at_fire": margin,
            "quarter_elapsed_pct": q_elapsed_pct,
            "game_time_seconds": cur["game_seconds"],
            "time_anchor": "pbp_tick",
            "home_away_role": home_away_role,
            "home_score_at_fire": cur["home_score"],
            "away_score_at_fire": cur["away_score"],
            "replay_mode": "approximate",
        })
        last_fired_gs = cur["game_seconds"]

    return hits


EVALUATORS = {
    "score_diff": evaluate_score_diff,
    "fg_percent_improve": evaluate_fg_percent_improve,
    "fg_pct_threshold": evaluate_fg_pct_threshold,
    "turnover_rate": evaluate_turnover_rate,
    "turnovers": evaluate_turnovers,
    "points_off_turnovers": evaluate_points_off_turnovers,
    "back_to_back": evaluate_back_to_back,
    "underdog_at_home": evaluate_underdog_at_home,
    "consecutive_points_run": evaluate_consecutive_points_run,
    "h1_moneyline_edge": evaluate_h1_moneyline_edge,
    "predicted_winner_threshold": evaluate_predicted_winner_threshold,
}


def evaluate_condition(cond, team_ctx, context):
    ctype = cond.get("type", "score_diff")
    evaluator = EVALUATORS.get(ctype)
    if evaluator is None:
        return {
            "matched": False,
            "direction": "",
            "run_key": None,
            "run_points": None,
            "run_scope": None,
            "state_dirty": False,
        }

    result = evaluator(cond, team_ctx, context) or {}
    normalized = {
        "matched": bool(result.get("matched")),
        "direction": str(result.get("direction") or ""),
        "run_key": result.get("run_key"),
        "run_points": result.get("run_points"),
        "run_scope": result.get("run_scope"),
        "state_dirty": bool(result.get("state_dirty")),
    }
    # Pass through extra fields from evaluators (e.g. predicted_winner_threshold
    # returns predicted_team, win_probability_pct, consensus, etc.)
    for k, v in result.items():
        if k not in normalized:
            normalized[k] = v
    return normalized