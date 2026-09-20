#!/usr/bin/env python3
"""
backfill_odds.py — Backfill historical odds from The Odds API for NRL games.

Populates spread, moneyline, and totals in game_history.json for records
that are missing odds data.

Usage:
    python3 backfill_odds.py --api-key YOUR_KEY --dry-run
    python3 backfill_odds.py --api-key YOUR_KEY --start-date 2025-01-01 --end-date 2025-12-31
    python3 backfill_odds.py --api-key YOUR_KEY
    python3 backfill_odds.py --use-cache
    python3 backfill_odds.py --api-key YOUR_KEY --test-live
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

try:
    import requests
except ImportError:
    print("ERROR: requests required. pip install requests", file=sys.stderr)
    sys.exit(1)

import odds_api

HISTORY_FILE = os.path.join(SCRIPT_DIR, "game_history.json")
CACHE_DIR = os.path.join(SCRIPT_DIR, "odds_cache")


def _log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} {}".format(ts, msg), flush=True)


def _cache_path(date_str):
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, "odds_{}.json".format(date_str))


def _load_cache(date_str):
    path = _cache_path(date_str)
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return None


def _save_cache(date_str, data):
    path = _cache_path(date_str)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def fetch_historical_odds(api_key, date_str):
    """Fetch historical odds for a specific date from The Odds API.

    Uses the historical endpoint: /v4/historical/sports/{sport}/odds/
    Returns list of events with odds, or None on error.
    """
    cached = _load_cache(date_str)
    if cached is not None:
        return cached

    url = "{}/historical/sports/{}/odds/".format(odds_api.ODDS_API_BASE, odds_api.SPORT_KEY)
    params = {
        "apiKey": api_key,
        "regions": "au",
        "markets": odds_api.MARKETS,
        "oddsFormat": "decimal",
        "date": "{}T12:00:00Z".format(date_str),
    }

    try:
        resp = requests.get(url, params=params, timeout=15)
        credits_used, credits_remaining = odds_api._usage_from_headers(resp)
        odds_api.log_call(odds_api.MARKETS, credits_used, credits_remaining,
                          resp.status_code, 0, False, "backfill_historical")
        resp.raise_for_status()
        data = resp.json()
        events = data.get("data", [])
        _save_cache(date_str, events)
        _log("  Fetched {} events for {} ({} credits used, {} remaining)".format(
            len(events), date_str, credits_used,
            credits_remaining if credits_remaining is not None else "?"))
        return events
    except Exception as e:
        _log("  ERROR fetching {}: {}".format(date_str, e))
        return None


def _fuzzy_team_match(nrl_short, odds_api_name):
    """Check if an NRL short name matches an Odds API full name."""
    short = nrl_short.lower().strip()
    full = odds_api_name.lower().strip()
    # Direct substring: "raiders" in "canberra raiders"
    if short in full:
        return True
    # Handle multi-word short names: "sea eagles" in "manly warringah sea eagles"
    if " " in short and short in full:
        return True
    # Handle "wests tigers" → "wests tigers" (exact)
    if short == full:
        return True
    # Use NRL_TO_ODDS_API mapping
    mapped = odds_api.NRL_TO_ODDS_API.get(nrl_short, "").lower()
    if mapped and (mapped in full or full in mapped):
        return True
    # Last resort: check if any word in short (>3 chars) appears in full
    for word in short.split():
        if len(word) > 3 and word in full:
            return True
    return False


def match_game_to_event(record, events):
    """Match a game history record to an Odds API event by team names."""
    home = record.get("home_team", "")
    away = record.get("away_team", "")

    for event in events:
        eh = event.get("home_team", "")
        ea = event.get("away_team", "")

        # Try both orientations (home/away might be swapped)
        if (_fuzzy_team_match(home, eh) and _fuzzy_team_match(away, ea)):
            return event
        if (_fuzzy_team_match(home, ea) and _fuzzy_team_match(away, eh)):
            return event
    return None


def test_live(api_key):
    """Test mode: fetch current live odds to validate parsing."""
    _log("Testing live odds fetch for {}...".format(odds_api.SPORT_KEY))
    events = odds_api._fetch_all_events(api_key, context="test_live")
    if not events:
        _log("No events returned (season may be off or no upcoming games)")
        return

    _log("{} events found".format(len(events)))
    for ev in events[:5]:
        home = ev.get("home_team", "?")
        away = ev.get("away_team", "?")
        odds = odds_api.extract_odds(ev)
        if odds:
            spread = odds_api.format_spread_detail(odds, home, away)
            _log("  {} vs {} — ML: {}/{} Spread: {} Bk: {}".format(
                away, home,
                odds.get("away_ml", "?"), odds.get("home_ml", "?"),
                spread or "?",
                odds.get("bookmaker", "?")))
        else:
            _log("  {} vs {} — no odds".format(away, home))

    status = odds_api.format_credit_status()
    if status:
        _log(status)


def backfill(api_key, start_date=None, end_date=None, dry_run=False, use_cache=False):
    """Backfill historical odds into game_history.json.

    The Odds API historical endpoint returns odds for UPCOMING games from
    that date — not games played on that date. So for a game on Mar 7,
    we need to fetch odds from ~2-4 days before (e.g. Mar 3-5) when
    that game would have been in the upcoming window.

    Strategy:
    - For each game, try fetching odds from 1, 2, 3 days before kickoff
    - Also search ALL existing cached dates for matching events
    """
    with open(HISTORY_FILE) as f:
        history = json.load(f)

    _log("Loaded {} game records".format(len(history)))

    needs_odds = []
    for i, r in enumerate(history):
        if r.get("home_spread") is not None:
            continue
        kickoff = r.get("kickoff_utc") or r.get("ts") or ""
        if start_date and kickoff < start_date:
            continue
        if end_date and kickoff > end_date + "T23:59:59":
            continue
        needs_odds.append((i, r))

    _log("{} records need odds".format(len(needs_odds)))
    if not needs_odds:
        return

    # Build a pool of all cached events for quick searching
    all_cached_events = []
    if os.path.isdir(CACHE_DIR):
        for f in sorted(os.listdir(CACHE_DIR)):
            if not f.endswith(".json"):
                continue
            cached = _load_cache(f.replace("odds_", "").replace(".json", ""))
            if cached:
                all_cached_events.extend(cached)
    _log("Loaded {} cached events from {} files".format(
        len(all_cached_events), len(os.listdir(CACHE_DIR)) if os.path.isdir(CACHE_DIR) else 0))

    updated = 0
    skipped = 0
    fetched_dates = set()

    for idx, record in needs_odds:
        kickoff = record.get("kickoff_utc") or record.get("ts") or ""
        game_date = kickoff[:10] if kickoff else ""
        if not game_date:
            skipped += 1
            continue

        # First: try matching against all cached events
        event = match_game_to_event(record, all_cached_events)

        # If not found in cache, try fetching from 1-3 days before the game
        if not event and not use_cache and api_key:
            from datetime import timedelta as td
            base = datetime.strptime(game_date, "%Y-%m-%d")
            for days_before in [1, 2, 3]:
                fetch_date = (base - td(days=days_before)).strftime("%Y-%m-%d")
                if fetch_date in fetched_dates:
                    continue
                events = fetch_historical_odds(api_key, fetch_date)
                fetched_dates.add(fetch_date)
                if events:
                    all_cached_events.extend(events)
                    event = match_game_to_event(record, events)
                    if event:
                        break
                time.sleep(0.5)

        if not event:
            skipped += 1
            continue

        odds = odds_api.extract_odds(event)
        if not odds:
            skipped += 1
            continue

        if dry_run:
            spread = odds_api.format_spread_detail(odds, record["home_team"], record["away_team"])
            _log("  [DRY] {} R{}: {} vs {} — ML:{}/{} Spread:{} Bk:{}".format(
                record.get("season"), record.get("round"),
                record.get("home_team"), record.get("away_team"),
                odds.get("home_ml", "?"), odds.get("away_ml", "?"),
                spread or "?", odds.get("bookmaker", "?")))
        else:
            history[idx]["home_spread"] = odds.get("home_spread")
            history[idx]["away_spread"] = odds.get("away_spread")
            history[idx]["home_ml"] = odds.get("home_ml")
            history[idx]["away_ml"] = odds.get("away_ml")
            history[idx]["total_line"] = odds.get("total_over")
            history[idx]["odds_bookmaker"] = odds.get("bookmaker")
            history[idx]["spread_detail"] = odds_api.format_spread_detail(
                odds, record["home_team"], record["away_team"])
        updated += 1

    if not dry_run and updated > 0:
        # Pre-backfill snapshot (#16 Phase 3)
        try:
            import runtime_backup
            snap_dir, _ = runtime_backup.snapshot_runtime_files(label="pre-backfill")
            _log("Pre-backfill snapshot: {}".format(os.path.basename(snap_dir)))
        except Exception as e:
            _log("WARN: Pre-backfill snapshot failed: {}".format(e))
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp, HISTORY_FILE)

    _log("Done: {} updated, {} skipped{}".format(
        updated, skipped, " (dry-run)" if dry_run else ""))


def main():
    parser = argparse.ArgumentParser(description="Backfill NRL odds from The Odds API")
    parser.add_argument("--api-key", type=str, default="",
                        help="The Odds API key")
    parser.add_argument("--start-date", type=str, default="",
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, default="",
                        help="End date YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview without writing")
    parser.add_argument("--use-cache", action="store_true",
                        help="Use cached responses only")
    parser.add_argument("--test-live", action="store_true",
                        help="Test live odds fetch")
    args = parser.parse_args()

    if args.test_live:
        if not args.api_key:
            print("--api-key required for --test-live", file=sys.stderr)
            sys.exit(1)
        test_live(args.api_key)
        return

    if not args.api_key and not args.use_cache:
        parser.error("--api-key required (or use --use-cache)")

    backfill(args.api_key, args.start_date, args.end_date,
             args.dry_run, args.use_cache)


if __name__ == "__main__":
    main()
