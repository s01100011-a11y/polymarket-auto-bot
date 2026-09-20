"""
timeline_features.py — Extract momentum and PBP features from NRL match timeline.

Processes the raw timeline array from NRL match centre data and produces
structured features for condition evaluation and ML model training.
"""


HALF_SECONDS = 40 * 60  # 40 minutes per half
SCORING_TYPES = {"Try", "Goal", "GoalMissed", "PenaltyGoal",
                 "OnePointFieldGoal", "TwoPointFieldGoal"}
POINTS = {
    "Try": 4,
    "Goal": 2,           # conversion
    "PenaltyGoal": 2,
    "OnePointFieldGoal": 1,
    "TwoPointFieldGoal": 2,
}


def extract_features(timeline, home_team_id, away_team_id):
    """Extract all PBP features from a match timeline.

    Args:
        timeline: list of event dicts from match centre data
        home_team_id: home team's teamId
        away_team_id: away team's teamId

    Returns:
        dict of features for both teams (home_* and away_*)
    """
    events = sorted(timeline, key=lambda e: e.get("gameSeconds", 0))

    # Normalize team IDs to match timeline event format (int) (#126)
    # monitor.py passes str, but timeline events have int teamId
    try:
        home_team_id = int(home_team_id)
        away_team_id = int(away_team_id)
    except (TypeError, ValueError):
        pass

    # Build score progression
    score_prog = _build_score_progression(events, home_team_id, away_team_id)

    # Scoring runs
    h_try_run, a_try_run = _max_consecutive_tries(events, home_team_id, away_team_id)
    h_pts_run, a_pts_run = _max_unanswered_points(events, home_team_id, away_team_id)

    # Error streaks
    h_err_streak, a_err_streak = _max_error_streak(events, home_team_id, away_team_id)

    # Penalty windows (in any 10-min window)
    h_pen_window, a_pen_window = _max_penalty_window(events, home_team_id, away_team_id, window_secs=600)

    # Sin bins
    h_sinbins, a_sinbins = _count_sin_bins(events, home_team_id, away_team_id)

    # Line breaks
    h_lb, a_lb = _count_by_type(events, "LineBreak", home_team_id, away_team_id)

    # Line break surge (max in 10-min window)
    h_lb_surge, a_lb_surge = _max_event_window(events, "LineBreak", home_team_id, away_team_id, window_secs=600)

    # Error counts from timeline
    h_errors, a_errors = _count_by_type(events, "Error", home_team_id, away_team_id)

    # Penalty counts from timeline
    h_pens, a_pens = _count_by_type(events, "Penalty", home_team_id, away_team_id)

    # 40/20 kicks
    h_4020, a_4020 = _count_by_type(events, "FortyTwenty", home_team_id, away_team_id)

    # Margin at halftime (from score progression)
    ht_margin = _margin_at_time(score_prog, HALF_SECONDS)

    # Margin trajectory (every 10 minutes)
    margin_trajectory = []
    for t in range(0, 4800 + 1, 600):
        margin_trajectory.append(_margin_at_time(score_prog, t))

    # 2H momentum: margin gained in 2H
    margin_at_ht = _margin_at_time(score_prog, HALF_SECONDS)
    margin_final = _margin_at_time(score_prog, 9999)
    h2_momentum = margin_final - margin_at_ht  # positive = home gained in 2H

    # Comeback indicator: trailing at HT by 6+ but within 6 at end
    comeback_potential = (
        abs(margin_at_ht) >= 6 and
        abs(margin_final) < abs(margin_at_ht)
    )

    # Quarter/half event counts (#120)
    period_counts = count_events_by_period(events, home_team_id, away_team_id)

    return {
        # Scoring runs
        "home_max_consecutive_tries": h_try_run,
        "away_max_consecutive_tries": a_try_run,
        "home_max_unanswered_points": h_pts_run,
        "away_max_unanswered_points": a_pts_run,

        # Error streaks
        "home_max_error_streak": h_err_streak,
        "away_max_error_streak": a_err_streak,

        # Penalty pressure
        "home_max_penalty_window_10m": h_pen_window,
        "away_max_penalty_window_10m": a_pen_window,
        "home_penalties_timeline": h_pens,
        "away_penalties_timeline": a_pens,

        # Discipline
        "home_sin_bins": h_sinbins,
        "away_sin_bins": a_sinbins,

        # Line breaks
        "home_line_breaks_timeline": h_lb,
        "away_line_breaks_timeline": a_lb,
        "home_max_line_break_surge_10m": h_lb_surge,
        "away_max_line_break_surge_10m": a_lb_surge,

        # Errors from timeline
        "home_errors_timeline": h_errors,
        "away_errors_timeline": a_errors,

        # Kicks
        "home_forty_twenties": h_4020,
        "away_forty_twenties": a_4020,

        # Score context
        "halftime_margin": margin_at_ht,  # positive = home leading
        "final_margin": margin_final,
        "h2_momentum": h2_momentum,
        "comeback_potential": comeback_potential,
        "margin_trajectory": margin_trajectory,

        # Score progression (for live analysis)
        "score_progression": score_prog,

        # Event counts
        "total_timeline_events": len(events),

        # Quarter/half event counts (#120)
        **period_counts,
    }


def _build_score_progression(events, home_id, away_id):
    """Build chronological score progression from scoring events.

    Returns list of {gameSeconds, homeScore, awayScore} dicts.
    """
    prog = [{"gameSeconds": 0, "homeScore": 0, "awayScore": 0}]
    h_score = 0
    a_score = 0

    for ev in events:
        etype = ev.get("type", "")
        if etype not in POINTS:
            continue

        pts = POINTS.get(etype, 0)
        team_id = ev.get("teamId")
        game_secs = ev.get("gameSeconds", 0)

        # Use running scores from event if available, else estimate
        if ev.get("homeScore") is not None:
            h_score = ev["homeScore"]
        if ev.get("awayScore") is not None:
            a_score = ev["awayScore"]

        # If no running scores on event, estimate from team
        if ev.get("homeScore") is None and ev.get("awayScore") is None:
            if team_id == home_id:
                h_score += pts
            elif team_id == away_id:
                a_score += pts

        prog.append({
            "gameSeconds": game_secs,
            "homeScore": h_score,
            "awayScore": a_score,
        })

    return prog


def _margin_at_time(score_prog, game_seconds):
    """Get home margin at a given game time. Positive = home leading."""
    last = {"homeScore": 0, "awayScore": 0}
    for p in score_prog:
        if p["gameSeconds"] > game_seconds:
            break
        last = p
    return last["homeScore"] - last["awayScore"]


def _max_consecutive_tries(events, home_id, away_id):
    """Max consecutive tries per team without the opponent scoring a try."""
    h_max = a_max = h_run = a_run = 0
    for ev in events:
        if ev.get("type") != "Try":
            continue
        if ev.get("teamId") == home_id:
            h_run += 1
            a_run = 0
            h_max = max(h_max, h_run)
        elif ev.get("teamId") == away_id:
            a_run += 1
            h_run = 0
            a_max = max(a_max, a_run)
    return h_max, a_max


def _max_unanswered_points(events, home_id, away_id):
    """Max unanswered points per team (tries + goals without opponent scoring)."""
    h_max = a_max = h_run = a_run = 0
    for ev in events:
        etype = ev.get("type", "")
        pts = POINTS.get(etype, 0)
        if pts == 0:
            continue
        if ev.get("teamId") == home_id:
            h_run += pts
            a_run = 0
            h_max = max(h_max, h_run)
        elif ev.get("teamId") == away_id:
            a_run += pts
            h_run = 0
            a_max = max(a_max, a_run)
    return h_max, a_max


def _max_error_streak(events, home_id, away_id):
    """Max consecutive errors by each team (without the other team making an error)."""
    h_max = a_max = h_run = a_run = 0
    for ev in events:
        if ev.get("type") != "Error":
            continue
        if ev.get("teamId") == home_id:
            h_run += 1
            a_run = 0
            h_max = max(h_max, h_run)
        elif ev.get("teamId") == away_id:
            a_run += 1
            h_run = 0
            a_max = max(a_max, a_run)
    return h_max, a_max


def _max_penalty_window(events, home_id, away_id, window_secs=600):
    """Max penalties by each team in any sliding window of window_secs."""
    h_times = [ev.get("gameSeconds", 0) for ev in events
               if ev.get("type") == "Penalty" and ev.get("teamId") == home_id]
    a_times = [ev.get("gameSeconds", 0) for ev in events
               if ev.get("type") == "Penalty" and ev.get("teamId") == away_id]
    return _max_in_window(h_times, window_secs), _max_in_window(a_times, window_secs)


def _max_in_window(times, window_secs):
    """Max count of events in any sliding window."""
    if not times:
        return 0
    times = sorted(times)
    max_count = 0
    for i, t in enumerate(times):
        count = sum(1 for t2 in times[i:] if t2 - t <= window_secs)
        max_count = max(max_count, count)
    return max_count


def _max_event_window(events, event_type, home_id, away_id, window_secs=600):
    """Max events of a type per team in any sliding window."""
    h_times = [ev.get("gameSeconds", 0) for ev in events
               if ev.get("type") == event_type and ev.get("teamId") == home_id]
    a_times = [ev.get("gameSeconds", 0) for ev in events
               if ev.get("type") == event_type and ev.get("teamId") == away_id]
    return _max_in_window(h_times, window_secs), _max_in_window(a_times, window_secs)


def _count_sin_bins(events, home_id, away_id):
    h = sum(1 for ev in events if ev.get("type") == "SinBin" and ev.get("teamId") == home_id)
    a = sum(1 for ev in events if ev.get("type") == "SinBin" and ev.get("teamId") == away_id)
    return h, a


def _count_by_type(events, event_type, home_id, away_id):
    h = sum(1 for ev in events if ev.get("type") == event_type and ev.get("teamId") == home_id)
    a = sum(1 for ev in events if ev.get("type") == event_type and ev.get("teamId") == away_id)
    return h, a


# ── Quarter/half-level event counts (#120) ───────────────────────────────────

# Synthetic quarter boundaries in seconds (#31)
QUARTER_RANGES = {
    "Q1": (0, 20 * 60),       # 0-20'
    "Q2": (20 * 60, 40 * 60), # 20-40'
    "Q3": (40 * 60, 60 * 60), # 40-60'
    "Q4": (60 * 60, 80 * 60), # 60-80'
    "ET": (80 * 60, 99999),   # 80'+
}
HALF_RANGES = {
    "H1": (0, 40 * 60),       # 0-40'
    "H2": (40 * 60, 99999),   # 40'+
}
# Event types that can be counted per quarter
COUNTABLE_EVENTS = {"Error", "Penalty", "LineBreak", "Try", "SinBin",
                    "KickBomb", "LineDropout", "FortyTwenty",
                    "RuckInfringement", "Interchange"}


def count_events_by_period(events, home_id, away_id):
    """Count timeline events per synthetic quarter and half for each team.

    Returns dict with keys like:
        home_errors_Q1, away_penalties_H2, home_line_breaks_Q3, etc.
    """
    counts = {}
    event_key_map = {
        "Error": "errors",
        "Penalty": "penalties",
        "LineBreak": "line_breaks",
        "Try": "tries",
        "SinBin": "sin_bins",
    }

    for ev in events:
        etype = ev.get("type", "")
        if etype not in event_key_map:
            continue
        team_id = ev.get("teamId")
        if team_id is None:
            continue
        gs = ev.get("gameSeconds", 0)
        side = "home" if str(team_id) == str(home_id) else (
            "away" if str(team_id) == str(away_id) else None)
        if not side:
            continue

        stat_key = event_key_map[etype]

        # Quarter counts
        for qname, (qstart, qend) in QUARTER_RANGES.items():
            if qstart <= gs < qend:
                k = "{}_{}_{}".format(side, stat_key, qname)
                counts[k] = counts.get(k, 0) + 1
                break

        # Half counts
        for hname, (hstart, hend) in HALF_RANGES.items():
            if hstart <= gs < hend:
                k = "{}_{}_{}".format(side, stat_key, hname)
                counts[k] = counts.get(k, 0) + 1
                break

    return counts


def quarter_from_seconds(game_seconds):
    """Return synthetic quarter name for a given game_seconds value."""
    for qname, (qstart, qend) in QUARTER_RANGES.items():
        if qstart <= game_seconds < qend:
            return qname
    return "ET"
