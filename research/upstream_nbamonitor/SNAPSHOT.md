# NBAMonitor upstream research snapshot

Source repository: `bobcheong/NBAMonitor`

Pinned source commit: `7fc3285823eb7190b737edd8580ec69229e4b5ae`

Source commit message:
> feat: PW export access logging — minimal (default) + structured (opt-in) (#457)

Captured for PW/Polymarket research on 2026-09-20.

## Purpose

This directory is a read-only reference snapshot used to understand:
- PW call generation and threshold logic
- bookmaker/DraftKings-style live odds capture
- `bk_spread`, `bk_spread_price`, `bk_ts`, and source metadata
- PW repeat/cooldown/suppression behavior
- `/api/pw-export`
- grading, replay, trend analysis, and synthetic odds

It is **not part of the live Polymarket bot execution path** and files in this directory must not be imported by production trading code.

## Included files

- README.md
- CLAUDE.md
- monitor.py
- odds_api.py
- conditions.py
- server.py
- outcomes.py
- pw_trend.py
- backfill_pw_calls.py
- export_pw_jsonl.py
- league_config.py
- synthetic_odds.py

For changes upstream, compare against the pinned source commit before refreshing this snapshot.
