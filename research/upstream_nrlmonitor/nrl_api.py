#!/usr/bin/env python3
"""
NRL API client — fetches live and completed game data from NRL.com.

Data sources:
  1. Draw API — round fixture list with match states (Pre, InProgress, HalfTime, FullTime)
  2. Match Centre API — per-game scores, stats, and events
  3. Live Scores API — lightweight scoreboard for all current-round games

All data sourced from NRL.com internal APIs (no auth required).
"""

import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

BASE = "https://www.nrl.com"
COMPETITION = 111  # NRL Telstra Premiership
COMPETITION_SOO = 116  # Ampol State of Origin (#102)
SOO_TEAM_IDS = {500146, 500147}  # Blues, Maroons

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Match states from the NRL API
LIVE_STATES = {"InProgress", "HalfTime", "FirstHalf", "SecondHalf", "ExtraTime"}
COMPLETED_STATES = {"FullTime", "Post"}
PRE_STATES = {"Pre", "Upcoming"}

# NRL team short names → full names (17 teams)
TEAM_NAMES = {
    "Broncos": "Brisbane Broncos",
    "Bulldogs": "Canterbury-Bankstown Bulldogs",
    "Cowboys": "North Queensland Cowboys",
    "Dolphins": "Dolphins",
    "Dragons": "St. George Illawarra Dragons",
    "Eels": "Parramatta Eels",
    "Knights": "Newcastle Knights",
    "Panthers": "Penrith Panthers",
    "Rabbitohs": "South Sydney Rabbitohs",
    "Raiders": "Canberra Raiders",
    "Roosters": "Sydney Roosters",
    "Sea Eagles": "Manly Warringah Sea Eagles",
    "Sharks": "Cronulla-Sutherland Sharks",
    "Storm": "Melbourne Storm",
    "Wests Tigers": "Wests Tigers",
    "Titans": "Gold Coast Titans",
    "Warriors": "New Zealand Warriors",
    # State of Origin (#102)
    "Blues": "NSW Blues",
    "Maroons": "QLD Maroons",
}

# Team abbreviations used in alerts/dashboard
TEAM_ABBREVS = {
    "Brisbane Broncos": "BRI",
    "Canterbury-Bankstown Bulldogs": "CBY",
    "North Queensland Cowboys": "NQC",
    "Dolphins": "DOL",
    "St. George Illawarra Dragons": "SGI",
    "Parramatta Eels": "PAR",
    "Newcastle Knights": "NEW",
    "Penrith Panthers": "PEN",
    "South Sydney Rabbitohs": "SOU",
    "Canberra Raiders": "CAN",
    "Sydney Roosters": "SYD",
    "Manly Warringah Sea Eagles": "MAN",
    "Cronulla-Sutherland Sharks": "CRO",
    "Melbourne Storm": "MEL",
    "Wests Tigers": "WST",
    "Gold Coast Titans": "GCT",
    "New Zealand Warriors": "WAR",
    # Also map short names
    "Broncos": "BRI", "Bulldogs": "CBY", "Cowboys": "NQC",
    "Dolphins": "DOL", "Dragons": "SGI", "Eels": "PAR",
    "Knights": "NEW", "Panthers": "PEN", "Rabbitohs": "SOU",
    "Raiders": "CAN", "Roosters": "SYD", "Sea Eagles": "MAN",
    "Sharks": "CRO", "Storm": "MEL", "Wests Tigers": "WST",
    "Titans": "GCT", "Warriors": "WAR",
    # State of Origin (#102)
    "NSW Blues": "NSW", "QLD Maroons": "QLD",
    "Blues": "NSW", "Maroons": "QLD",
}

# Team timezones (for alert formatting)
TEAM_TIMEZONES = {
    "BRI": "Australia/Brisbane",
    "CBY": "Australia/Sydney",
    "NQC": "Australia/Brisbane",
    "DOL": "Australia/Brisbane",
    "SGI": "Australia/Sydney",
    "PAR": "Australia/Sydney",
    "NEW": "Australia/Sydney",
    "PEN": "Australia/Sydney",
    "SOU": "Australia/Sydney",
    "CAN": "Australia/Sydney",
    "SYD": "Australia/Sydney",
    "MAN": "Australia/Sydney",
    "CRO": "Australia/Sydney",
    "MEL": "Australia/Melbourne",
    "WST": "Australia/Sydney",
    "GCT": "Australia/Brisbane",
    "WAR": "Pacific/Auckland",
    # State of Origin (#102)
    "NSW": "Australia/Sydney",
    "QLD": "Australia/Brisbane",
}


def _get(url, referer=None, retries=3):
    """Fetch JSON from URL with retries and backoff."""
    headers = dict(HEADERS)
    headers["Referer"] = referer or BASE
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)


def get_draw(season, round_number):
    """Fetch the draw for a given round. Returns list of all fixture dicts."""
    url = "{}/draw/data?competition={}&season={}&round={}".format(
        BASE, COMPETITION, season, round_number
    )
    referer = "{}/draw/?competition={}&season={}".format(BASE, COMPETITION, season)
    data = _get(url, referer=referer)
    return data.get("fixtures", [])


def get_soo_fixtures(season):
    """Fetch State of Origin fixtures for a season (#102).

    Returns list of fixture dicts with added `competition` and `soo_game` fields.
    """
    url = "{}/draw/data?competition={}&season={}".format(
        BASE, COMPETITION_SOO, season
    )
    referer = "{}/draw/?competition={}&season={}".format(BASE, COMPETITION_SOO, season)
    try:
        data = _get(url, referer=referer)
        fixtures = data.get("fixtures", [])
        for i, f in enumerate(fixtures):
            f["_competition"] = "state_of_origin"
            # roundTitle is "Game 1", "Game 2", "Game 3"
            rt = f.get("roundTitle", "")
            game_num = 0
            if "1" in rt:
                game_num = 1
            elif "2" in rt:
                game_num = 2
            elif "3" in rt:
                game_num = 3
            f["_soo_game"] = game_num
        return fixtures
    except Exception:
        return []


MAX_REGULAR_ROUND = 27
MAX_FINALS_ROUND = 31  # Finals Week 1-4 (rounds 28-31)


def _is_finals_round(round_number):
    """Return True if round_number is in the finals range (#298)."""
    return isinstance(round_number, int) and round_number > MAX_REGULAR_ROUND


def _detect_finals_week(fixtures):
    """Derive a synthetic round number from finals fixture roundTitle (#298).

    NRL API returns roundNumber=null for finals, but roundTitle is present:
      "Finals Week 1", "Finals Week 2", "Preliminary Final", "Grand Final"
    Maps to synthetic round numbers 28-31.
    """
    if not fixtures:
        return None
    title = (fixtures[0].get("roundTitle") or "").lower()
    if "finals week 1" in title or "qualifying" in title or "elimination" in title:
        return 28
    if "finals week 2" in title or "semi-final" in title:
        return 29
    if "preliminary" in title:
        return 30
    if "grand final" in title:
        return 31
    if "finals" in title:
        return 28  # fallback for unknown finals format
    return None


def get_current_round_fixtures(season):
    """Find the current round and return its fixtures.

    Scans rounds from 1 upward, returns the first round that has
    live games or, failing that, the latest round with any fixtures.
    Includes finals rounds 28-31 (#298).
    """
    last_with_fixtures = None
    last_round = None
    prev_urls = None  # detect duplicate finals fixtures from API

    for rnd in range(1, MAX_FINALS_ROUND + 1):
        try:
            fixtures = get_draw(season, rnd)
        except Exception:
            if rnd > MAX_REGULAR_ROUND:
                break  # no more finals
            break

        if not fixtures:
            if rnd > MAX_REGULAR_ROUND:
                break  # no more finals
            break

        # NRL API returns same finals fixtures for any round >= 28.
        # Detect duplicates by comparing matchCentreUrl sets (#298).
        cur_urls = frozenset(f.get("matchCentreUrl", "") for f in fixtures)
        if rnd > MAX_REGULAR_ROUND and prev_urls == cur_urls:
            break  # already seen these finals fixtures
        prev_urls = cur_urls

        # For finals, derive synthetic round from roundTitle
        effective_round = rnd
        if rnd > MAX_REGULAR_ROUND:
            synthetic = _detect_finals_week(fixtures)
            if synthetic:
                effective_round = synthetic

        last_with_fixtures = fixtures
        last_round = effective_round

        # If any game is live or pre-game, this is the current round
        has_live = any(f.get("matchState") in LIVE_STATES for f in fixtures)
        has_pre = any(f.get("matchState") in PRE_STATES for f in fixtures)
        all_done = all(f.get("matchState") in COMPLETED_STATES for f in fixtures)

        if has_live or has_pre:
            return effective_round, fixtures

        if all_done:
            # Check if there's a next round
            continue

        time.sleep(0.2)

    return last_round, last_with_fixtures or []


def get_match_data(match_centre_url):
    """Fetch detailed match data from match centre URL.

    Args:
        match_centre_url: Path like /draw/nrl-premiership/2026/round-3/raiders-v-bulldogs/

    Returns:
        Parsed match dict with scores, stats, events.
    """
    url = "{}{}data".format(BASE, match_centre_url)
    referer = "{}{}".format(BASE, match_centre_url)
    return _get(url, referer=referer)


def get_live_scoreboard(season, round_number):
    """Fetch all fixtures for a round and return a scoreboard-like structure.

    Returns:
        dict with:
            round: round number
            season: season year
            games: list of normalized game dicts
    """
    fixtures = get_draw(season, round_number)
    games = []

    for fix in fixtures:
        state = fix.get("matchState", "")
        home_team = fix.get("homeTeam", {})
        away_team = fix.get("awayTeam", {})

        clock_data = fix.get("clock", {})
        start_time = (fix.get("startTime", "")
                      or clock_data.get("kickOffTimeLong", ""))

        game = {
            "match_id": fix.get("matchId") or fix.get("matchCentreUrl", ""),
            "match_centre_url": fix.get("matchCentreUrl", ""),
            "match_state": state,
            "is_live": (state in LIVE_STATES or fix.get("matchMode") == "Live") and state not in COMPLETED_STATES,
            "is_completed": state in COMPLETED_STATES,
            "is_pre": state in PRE_STATES,
            "clock": clock_data.get("gameTime", ""),
            "period": _get_period(fix),
            "home_team": home_team.get("nickName", ""),
            "home_team_id": home_team.get("teamId"),
            "home_score": home_team.get("score", 0),
            "home_odds": home_team.get("odds"),
            "away_team": away_team.get("nickName", ""),
            "away_team_id": away_team.get("teamId"),
            "away_score": away_team.get("score", 0),
            "away_odds": away_team.get("odds"),
            "venue": fix.get("venue", ""),
            "venue_city": fix.get("venueCity", ""),
            "start_time": start_time,
            # SOO metadata (#102)
            "competition": fix.get("_competition", "nrl_premiership"),
            "soo_game": fix.get("_soo_game", 0),
            "round_title": fix.get("roundTitle", ""),
            # Finals metadata (#298)
            "_is_finals": _is_finals_round(round_number),
        }
        games.append(game)

    return {
        "round": round_number,
        "season": season,
        "games": games,
    }


def _get_period(fixture):
    """Extract current period from fixture data.

    NRL games have: 1st Half, Half Time, 2nd Half, Full Time, Extra Time.
    Returns: '1H', '2H', 'HT', 'FT', 'ET', or 'PRE'.
    """
    state = fixture.get("matchState", "")
    if state in PRE_STATES:
        return "PRE"
    if state in ("HalfTime",):
        return "HT"
    if state in COMPLETED_STATES:
        return "FT"

    # New API states map directly to periods
    if state == "FirstHalf":
        return "1H"
    if state == "SecondHalf":
        return "2H"
    if state == "ExtraTime":
        return "ET"

    # For InProgress (legacy), determine half from clock or matchPeriod
    clock = fixture.get("clock", {})
    period = clock.get("period", "")
    if period:
        period_lower = period.lower()
        if "first" in period_lower or "1st" in period_lower:
            return "1H"
        if "second" in period_lower or "2nd" in period_lower:
            return "2H"
        if "extra" in period_lower or "golden" in period_lower:
            return "ET"

    # Fallback: if game is live, assume 1st half
    if state in LIVE_STATES or fixture.get("matchMode") == "Live":
        return "1H"
    return "PRE"


def parse_match_detail(match_data):
    """Extract normalized match detail from match centre data.

    Returns:
        dict with scores, halftime scores, team stats, and key events.
    """
    home = match_data.get("homeTeam", {})
    away = match_data.get("awayTeam", {})

    home_scoring = home.get("scoring", {})
    away_scoring = away.get("scoring", {})

    start_iso = match_data.get("startTime", "")
    try:
        dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = None

    return {
        "match_id": match_data.get("matchId"),
        "round_number": match_data.get("roundNumber"),
        "round_title": match_data.get("roundTitle"),
        "match_state": match_data.get("matchState", ""),
        "start_time_utc": dt,
        "venue": match_data.get("venue"),

        "home_team": home.get("nickName", ""),
        "home_team_id": home.get("teamId"),
        "home_score": home.get("score", 0),
        "home_score_1h": home_scoring.get("halfTimeScore"),
        "home_tries": home_scoring.get("tries", 0),
        "home_goals": home_scoring.get("goals", 0),
        "home_field_goals": home_scoring.get("fieldGoals", 0),

        "away_team": away.get("nickName", ""),
        "away_team_id": away.get("teamId"),
        "away_score": away.get("score", 0),
        "away_score_1h": away_scoring.get("halfTimeScore"),
        "away_tries": away_scoring.get("tries", 0),
        "away_goals": away_scoring.get("goals", 0),
        "away_field_goals": away_scoring.get("fieldGoals", 0),

        "home_stats": _extract_team_stats(home, match_data.get("stats", {}), "home"),
        "away_stats": _extract_team_stats(away, match_data.get("stats", {}), "away"),
    }


_STAT_TITLE_MAP = {
    "Possession %": "possession",
    "Completion Rate": "completionRate",
    "All Run Metres": "allRunMetres",
    "Post Contact Metres": "postContactMetres",
    "Line Breaks": "lineBreaks",
    "Tackle Breaks": "tackleBreaks",
    "Offloads": "offloads",
    "Kicks": "kicks",
    "Kicking Metres": "kickingMetres",
    "Kick Defusal %": "kickDefusal",
    "Kick Return Metres": "kickReturnMetres",
    "Effective Tackle %": "effectiveTackle",
    "Tackles Made": "tacklesMade",
    "Missed Tackles": "missedTackles",
    "Ineffective Tackles": "ineffectiveTackles",
    "Errors": "errors",
    "Average Play The Ball Speed": "averagePlayTheBallSpeed",
    "Average Set Distance": "averageSetDistance",
    "Intercepts": "intercepts",
    "All Runs": "allRuns",
    "Penalties Conceded": "penaltiesConceded",
    "Ruck Infringements": "ruckInfringements",
    "Receipts": "receipts",
    "Total Passes": "totalPasses",
    "Dummy Passes": "dummyPasses",
    "Bombs": "bombs",
    "Time In Possession": "timeInPossession",
}


def _stat_title_to_key(title):
    """Map NRL API stat title to camelCase key used by conditions.py."""
    return _STAT_TITLE_MAP.get(title, "")


def _extract_team_stats(team_data, match_stats=None, side="home"):
    """Extract team statistics from match centre team data.

    NRL stats may be in:
    1. team.stats (old format, flat dict or list)
    2. match_data.stats.groups[] (new format, homeValue/awayValue objects)

    Returns a flat dict of stat name → value.
    """
    stats = {}
    val_key = "homeValue" if side == "home" else "awayValue"

    # Method 1: Extract from top-level stats.groups (new NRL API format)
    if match_stats and isinstance(match_stats, dict):
        for group in match_stats.get("groups", []):
            for stat in group.get("stats", []):
                title = stat.get("title", "")
                key = _stat_title_to_key(title)
                if not key:
                    continue
                val_obj = stat.get(val_key, {})
                if isinstance(val_obj, dict):
                    stats[key] = _parse_stat_value(val_obj.get("value"))
                else:
                    stats[key] = _parse_stat_value(val_obj)

    # Method 2: team.stats (old format)
    raw_stats = team_data.get("stats", {})
    if isinstance(raw_stats, dict):
        for key, val in raw_stats.items():
            if key not in stats:
                stats[key] = _parse_stat_value(val)
    elif isinstance(raw_stats, list):
        for item in raw_stats:
            if isinstance(item, dict):
                name = item.get("name", item.get("key", ""))
                value = item.get("value", item.get("statValue", ""))
                if name and name not in stats:
                    stats[name] = _parse_stat_value(value)

    # Also extract from scoring breakdown if available
    scoring = team_data.get("scoring", {})
    if scoring:
        stats["tries"] = _parse_stat_value(scoring.get("tries", 0))
        stats["goals"] = _parse_stat_value(scoring.get("goals", 0))
        stats["fieldGoals"] = _parse_stat_value(scoring.get("fieldGoals", 0))
        stats["halfTimeScore"] = _parse_stat_value(scoring.get("halfTimeScore"))

    return stats


def _parse_stat_value(val):
    """Parse a stat value, handling strings like '65%' or '12/15'."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return val
    s = str(val).strip()
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except ValueError:
            return s
    try:
        return int(s)
    except ValueError:
        try:
            return float(s)
        except ValueError:
            return s


def get_team_abbrev(team_name):
    """Get 3-letter abbreviation for a team name."""
    return TEAM_ABBREVS.get(team_name, team_name[:3].upper())


def get_team_full_name(short_name):
    """Get full team name from short name."""
    return TEAM_NAMES.get(short_name, short_name)


# ── Ladder data ───────────────────────────────────────────────────────────────

_ladder_cache = {"data": None, "ts": 0, "season": None}
_LADDER_CACHE_TTL = 3600  # 1 hour


def get_ladder(season):
    """Fetch NRL ladder data with caching.

    Returns dict keyed by team nickname with W-L, position, streak, form, etc.
    """
    now = time.time()
    if (_ladder_cache["data"] is not None
            and _ladder_cache["season"] == season
            and (now - _ladder_cache["ts"]) < _LADDER_CACHE_TTL):
        return _ladder_cache["data"]

    url = "{}/ladder/data?competition={}&season={}".format(BASE, COMPETITION, season)
    try:
        data = _get(url, referer="{}/ladder/".format(BASE))
    except Exception:
        return _ladder_cache["data"] or {}

    positions = data.get("positions", [])
    result = {}
    for i, p in enumerate(positions):
        nickname = p.get("teamNickname", "")
        if not nickname:
            # Try to extract from theme key
            theme_key = p.get("theme", {}).get("key", "")
            nickname = theme_key.capitalize() if theme_key else ""
        if not nickname:
            continue

        stats = p.get("stats", {})
        result[nickname] = {
            "position": i + 1,
            "position_label": _ordinal(i + 1),
            "played": stats.get("played", 0),
            "wins": stats.get("wins", 0),
            "lost": stats.get("lost", 0),
            "drawn": stats.get("drawn", 0),
            "points": stats.get("points", 0),
            "record": "{}-{}".format(stats.get("wins", 0), stats.get("lost", 0)),
            "home_record": stats.get("home record", ""),
            "away_record": stats.get("away record", ""),
            "streak": stats.get("streak", ""),
            "form": stats.get("form", ""),
            "pts_for": stats.get("points for", 0),
            "pts_against": stats.get("points against", 0),
            "pts_diff": stats.get("points difference", 0),
            "avg_win_margin": stats.get("average winning margin", 0),
            "avg_loss_margin": stats.get("average losing margin", 0),
            "close_games": stats.get("close games", 0),
            "golden_point": stats.get("golden point", 0),
            "premiership_odds": stats.get("odds", ""),
        }

    _ladder_cache["data"] = result
    _ladder_cache["ts"] = time.time()
    _ladder_cache["season"] = season
    return result


def _ordinal(n):
    """Return ordinal string for a number: 1 → '1st', 2 → '2nd', etc."""
    if 11 <= (n % 100) <= 13:
        return "{}th".format(n)
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return "{}{}".format(n, suffix)


def get_completed_rounds(season, max_rounds=27):
    """Return list of round numbers that have all games completed."""
    completed = []
    for rnd in range(1, max_rounds + 1):
        try:
            fixtures = get_draw(season, rnd)
            if not fixtures:
                break
            if all(f.get("matchState") in COMPLETED_STATES for f in fixtures):
                completed.append(rnd)
            time.sleep(0.2)
        except Exception:
            break
    return completed


if __name__ == "__main__":
    import sys
    season = int(sys.argv[1]) if len(sys.argv) > 1 else 2026

    print("Finding current NRL {} round...\n".format(season))
    rnd, fixtures = get_current_round_fixtures(season)
    if not rnd:
        print("No fixtures found.")
        sys.exit(1)

    print("Round {}: {} fixtures".format(rnd, len(fixtures)))
    for fix in fixtures:
        home = fix.get("homeTeam", {}).get("nickName", "?")
        away = fix.get("awayTeam", {}).get("nickName", "?")
        state = fix.get("matchState", "?")
        h_score = fix.get("homeTeam", {}).get("score", 0)
        a_score = fix.get("awayTeam", {}).get("score", 0)
        print("  {} vs {} — {} ({}-{})".format(home, away, state, h_score, a_score))
