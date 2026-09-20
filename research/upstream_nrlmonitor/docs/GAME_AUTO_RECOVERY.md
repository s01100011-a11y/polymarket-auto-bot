# Game Auto Recovery

Automatically detects and recovers games missed due to monitor downtime, network outages, or process crashes. Ensures `game_history.json` has no gaps even when the monitor wasn't running during a game.

## Problem

The NRL monitor runs as a one-shot systemd timer every minute during game windows (UTC 04:00-13:00). If the monitor is offline when a game completes, the game-end processing never runs and the game is never recorded in history. This creates gaps in analysis, PW accuracy tracking, and condition outcomes.

Prior to this feature, the only protection was round-flip recovery (#144), which handles a narrow case where the NRL API switches rounds while the last game is still live. Auto-recovery covers all other scenarios.

## Architecture

```
                    ┌─────────────────────────────────────────┐
                    │           Detection Layer               │
                    ├─────────────────────────────────────────┤
                    │                                         │
                    │  ┌─────────────┐   ┌────────────────┐   │
                    │  │  In-Poll    │   │  Scheduled     │   │
                    │  │  Detection  │   │  Scan (CLI)    │   │
                    │  │             │   │                │   │
                    │  │ monitor.py  │   │ recovery.py    │   │
                    │  │ each cycle  │   │ scan --season  │   │
                    │  │ prev 1-2    │   │ all rounds     │   │
                    │  │ rounds      │   │ + SOO          │   │
                    │  └──────┬──────┘   └───────┬────────┘   │
                    │         │                  │            │
                    └─────────┼──────────────────┼────────────┘
                              │                  │
                              ▼                  ▼
                    ┌─────────────────────────────────────────┐
                    │      _recover_missed_game()             │
                    ├─────────────────────────────────────────┤
                    │                                         │
                    │  1. Fetch match data (NRL API)          │
                    │  2. Build record (backfill pipeline)    │
                    │  3. Check preserved live state          │
                    │  4. Evaluate conditions (27 types)      │
                    │  5. Generate/merge PW calls             │
                    │  6. Compute postgame tags               │
                    │  7. Write to game_history.json (WAL)    │
                    │  8. Log recovery event                  │
                    │  9. Send alert                          │
                    │                                         │
                    └─────────────────────────────────────────┘
```

## Detection

### In-Poll Detection (monitor.py)

Runs every poll cycle during the game window, after the existing round-flip recovery block.

1. Checks `current_round - 1` via `nrl_api.get_draw(season, round)`
2. Checks `current_round - 2` if not scanned in the last 24 hours
3. Filters for completed games (`FullTime`/`Post`) not in `game_history.json` and without a `game_end_` state key
4. Also checks SOO fixtures via `nrl_api.get_soo_fixtures(season)`
5. Round scan timestamps cached in state (`recovery_checked_{season}_{round}`) to avoid redundant API calls — re-checks every 6 hours

**Cost:** 1-2 extra `get_draw()` API calls per poll cycle (NRL.com, no auth, no rate limit).

**Guard:** Controlled by `config.auto_recovery_enabled` (default `true`). When disabled, detection is skipped entirely and a one-time log message is emitted.

### Scheduled Scan (recovery.py)

CLI command for batch gap detection across all rounds of a season:

```bash
python3 recovery.py scan --season 2026              # Recover all gaps
python3 recovery.py scan --season 2026 --dry-run    # Preview only
python3 recovery.py scan --season 2025,2026          # Multi-season
```

Always runs regardless of `auto_recovery_enabled` — this is an explicit manual action.

**Systemd timer:** `nrl-recovery.timer` runs daily at 14:00 UTC (1 hour after game window closes).

```bash
cp systemd/nrl-recovery.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nrl-recovery.timer
```

## Recovery Pipeline

`_recover_missed_game()` in `monitor.py` handles the full recovery of a single game:

### 1. Fetch Match Data

```python
raw = nrl_api.get_match_data(match_centre_url)
```

NRL.com match centre data remains available for completed games indefinitely. Contains full stats (30+ metrics), timeline events (avg 110/game), and scoring details.

### 2. Build Base Record

Uses `backfill._build_game_record()` — the same function used for historical backfill:
- Scores, halftime splits, 2H scores
- Team stats from `stats.groups` (27 mapped stats)
- PBP features via `timeline_features.extract_features()` — 21 momentum metrics
- Score progression from timeline events
- Try events with player names
- Venue, kickoff time, attendance

### 3. Merge Preserved Live State (Partial Miss)

If the monitor tracked part of the game before crashing, state keys may still exist:

| State Key | Contains | Merge Strategy |
|-----------|----------|----------------|
| `cond_hits_{match_id}` | Accumulated condition fires | Live takes precedence per team\|condition key; backfill fills gaps |
| `pw_calls_{match_id}` | Real-time PW predictions | Use live calls as-is (preferred over synthetic) |
| `pregame_odds_{match_id}` | Pre-game odds snapshot | Use if available (NRL has no free odds source) |

State keys are preserved for 48 hours after a game's kickoff time (TTL cleanup runs each poll cycle).

### 4. Evaluate Conditions

```python
backfill_conds = backfill.evaluate_conditions_for_game(config, record)
```

All 27 base condition types evaluated against the completed game's final stats and timeline. For partial misses, live conditions (with real fire times) take precedence; backfill conditions fill any gaps.

### 5. Generate PW Calls

If no live PW calls exist in state:

```python
pw_calls = backfill._generate_pw_calls_for_record(record, prior_records, config, threshold, cooldown)
```

- **ML model disabled** — prevents data leakage from future-trained model
- **Lookahead-safe** — `prior_records` filtered to games with dates strictly before current game
- **Evaluates at scoring moments** — uses `score_progression` from PBP timeline
- Controlled by `config.auto_recovery_pw_calls` (default `true`)

### 6. Record Metadata

```python
record["source"] = "recovered"
record["replay_mode"] = "auto_recovered"
```

- `source: "recovered"` — distinguishes from `"live"` and `"backfill"` records
- `replay_mode: "auto_recovered"` — indicates automatic (not manual) recovery
- Postgame tags computed: CLOSE (margin <= 12), BLOWOUT (margin > 20), COMEBACK (trailing at HT but won)

### 7. Write and Alert

- Written via `append_game_record()` with WAL crash safety
- Raw API data cached to `nrl_cache/{season}/` for future backfill
- Final score alert sent with recovered indicator
- Recovery event logged to `state["auto_recovery_log"]` (capped at 100 entries)

## Configuration

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `auto_recovery_enabled` | bool | `true` | Master toggle for in-poll detection |
| `auto_recovery_pw_calls` | bool | `true` | Generate synthetic PW calls for recovered games |

Both toggleable from the dashboard "Game Auto Recovery" tab under Monitor Status.

## Dashboard

### Game Auto Recovery Tab (Monitor Status)

```
Game Auto Recovery                                    [Refresh]

┌─ Status ────────────────────────────────────────────────────┐
│  Enabled: ON  [Disable]     PW Calls: ON  [Disable]        │
│  Last scan: 2026-06-02 12:05:18 UTC (Round 13)              │
│  Total recovered: 3                                          │
└─────────────────────────────────────────────────────────────┘

┌─ Recovery Log ──────────────────────────────────────────────┐
│  Time           │ Game                 │ R  │ Score │ State │
│  Jun 02 12:05   │ Panthers vs Warriors │ 13 │ 20-18 │ Full  │
│  May 31 12:40   │ Cowboys vs Rabbitohs │ 12 │ 30-18 │Merged │
└─────────────────────────────────────────────────────────────┘
```

**State column:**
- **Full** — No live state available; full backfill replay
- **Merged** — Had preserved live state; merged with backfill
- **Failed** — Recovery attempt failed (error shown below row)

### Source Filters

Recovered games appear in:
- **Game History** — `source: "recovered"` option in source dropdown
- **PW Tab** — `Game: Recovered` option in Game Source filter group
- **Logs Tab** — `RECOVERY` tag on auto-recovery log entries

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/auto-recovery` | GET | Returns enabled status, last scan info, recovery log |
| `/api/auto-recovery/toggle` | POST | Toggle `auto_recovery_enabled` or `auto_recovery_pw_calls` |

### GET /api/auto-recovery Response

```json
{
  "enabled": true,
  "pw_calls_enabled": true,
  "last_scan_ts": "2026-06-02T12:05:18+00:00",
  "last_scan_round": 13,
  "total_recovered": 3,
  "log": [
    {
      "ts": "2026-06-02T12:05:18+00:00",
      "match_id": "/draw/nrl-premiership/2026/round-13/panthers-v-warriors/",
      "home_team": "Panthers",
      "away_team": "Warriors",
      "round": 13,
      "score": "20-18",
      "had_live_state": false,
      "conditions": 23,
      "pw_calls": 7,
      "status": "success"
    }
  ]
}
```

## State TTL Cleanup

Old state keys are cleaned up each poll cycle to prevent unbounded growth:

| State Key Pattern | Condition | Action |
|-------------------|-----------|--------|
| `cond_hits_`, `pw_calls_`, `pregame_odds_`, etc. | `game_end_` exists | Delete immediately |
| `cond_hits_`, `pw_calls_`, `pregame_odds_`, etc. | No `game_end_`, age > 48h | Delete (recovery window expired) |
| `game_end_` | Age > 7 days | Delete |
| Any key | Match ID in current round | Preserved (still active) |

## Scenarios

| Scenario | Detection | Data Quality |
|----------|-----------|-------------|
| Monitor offline entire game | In-poll: prev round check | Full backfill: conditions + synthetic PW |
| Monitor crashed mid-game | In-poll: prev round check | Merged: live conditions/PW + backfill gaps |
| Multi-day outage | Scheduled scan (CLI/timer) | Full backfill per game |
| NRL API round flip | #144 orphan scan | Game-end with accumulated live state |
| User disables recovery | Config toggle OFF | Detection skipped; CLI scan still available |

## Relationship to Other Recovery Features

| Feature | Scope | Trigger |
|---------|-------|---------|
| **Round-flip recovery (#144)** | Games tracked live but missed at game-end due to API round switch | `cond_hits_` state without `game_end_` |
| **Game Auto Recovery (#136)** | Games never seen live (monitor was offline) | Completed fixtures not in history |
| **WAL recovery (#16)** | Corrupted `game_history.json` | JSONDecodeError on file load |
| **Backup/Restore (#16)** | Manual point-in-time recovery | CLI or dashboard button |

## Files

| File | Role |
|------|------|
| `monitor.py` | `_recover_missed_game()`, `_detect_missed_games()` detection loop, state TTL cleanup |
| `recovery.py` | `scan` CLI subcommand for batch gap detection |
| `server.py` | `/api/auto-recovery` endpoint, toggle endpoint, source filters, log tag |
| `dashboard.html` | "Game Auto Recovery" tab, source dropdown options |
| `config.example.json` | `auto_recovery_enabled`, `auto_recovery_pw_calls` |
| `systemd/nrl-recovery.service` | Oneshot service for scheduled scan |
| `systemd/nrl-recovery.timer` | Daily timer at 14:00 UTC |
