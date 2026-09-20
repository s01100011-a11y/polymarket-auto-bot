# NRL Monitor

NRL Monitor is a polling-based NRL game intelligence service that evaluates live game conditions every minute during game windows, sends alerts to Slack, and serves a dashboard with live visibility, historical analysis, and ML-powered predicted winner modelling.

Based on the architecture of [NBA Monitor](https://github.com/bobcheong/NBAMonitor), adapted for the NRL Telstra Premiership.

## Core Capabilities

- Monitors live NRL games via NRL.com APIs (no authentication required)
- **14 condition types** — 8 stats-based + 6 PBP-based (play-by-play timeline analysis)
- **ML predicted winner model** — logistic regression trained on 514 historical games (99.2% CV accuracy)
- **Play-by-play analysis** — 21 momentum features extracted from NRL match timeline (56,488 events)
- Compound conditions with AND/OR logic and optional base alert suppression
- Configurable alert cooldowns to avoid duplicate notifications
- End-of-game final score alerts with postgame tags (CLOSE, BLOWOUT, COMEBACK, UPSET)
- **NBA Monitor-style dashboard** — Control Panel with nested tabs, team colour icons, full game history browser
- **Historical backfill** — 2-phase fetch-cache/replay system with 514 games across 2024–2026 seasons

## Quick Start

```bash
# 1. Clone
git clone https://github.com/bobcheong/NRLMonitor.git
cd NRLMonitor

# 2. Configure
cp config.example.json config.json
# Edit config.json: set slack_bot_token and slack_channel

# 3. Install dependencies
pip3 install requests
pip3 install scikit-learn    # optional — for ML predicted winner model

# 4. Run
python3 monitor.py           # one monitor cycle
python3 server.py 8898       # start dashboard
# → http://127.0.0.1:8898/dashboard.html

# 5. Train ML model (optional)
python3 ml_model.py --train --evaluate

# 6. Backfill historical data (optional)
python3 backfill.py --fetch-cache --season 2024,2025,2026
python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026
```

## Automated Setup

```bash
bash setup.sh
```

Installs dependencies, tests the NRL API, and optionally sets up systemd timers or cron for automated polling.

## Architecture

```
Systemd timer (every minute, UTC 06:00–12:00 = AEST game windows)
  → monitor.py
      → NRL Draw API (round fixtures / scoreboard)
      → NRL Match Centre API (per-game stats + timeline)
      → Evaluate 26 condition types per monitored team
      → Evaluate compound conditions (AND/OR)
      → Apply per-condition cooldowns
      → Send Slack alerts (direct API or OpenClaw gateway)
      → Write: alerts.json, live_stats.json, game_ticker.json, .alert_state.json
      → Append: game_history.json (at game end)

server.py (long-running, port 8898)
  → Serves dashboard.html + 14 /api/* endpoints
  → POST /api/config validates and atomically writes config.json

backfill.py (batch, run manually)
  → Phase 1: fetch draw + match data → nrl_cache/<season>/
  → Phase 2: replay from cache → game_history.json with full stats + PBP features

ml_model.py (batch, run manually)
  → Trains on game_history.json → model_nrl.pkl + model_meta_nrl.json
```

### Project Files

| File | Role |
|------|------|
| `monitor.py` | Main polling job — fetches NRL APIs, evaluates conditions, sends alerts, persists state |
| `server.py` | Dashboard HTTP backend — 15 JSON API endpoints + static file serving |
| `nrl_api.py` | NRL.com API client — draw fixtures, match centre data, 17-team mappings |
| `conditions.py` | 26 condition evaluator functions (8 original + 6 PBP + 12 new stats) |
| `timeline_features.py` | PBP feature extraction — 21 momentum features from match timeline |
| `ml_model.py` | Logistic regression predicted winner model (scikit-learn) |
| `odds_api.py` | The Odds API client — live/historical odds, spreads, moneylines (`rugbyleague_nrl`) |
| `outcomes.py` | Historical outcome analysis with recency weighting |
| `backfill.py` | 2-phase historical backfill (fetch-cache + replay-write) |
| `backfill_odds.py` | Historical odds backfill from The Odds API with per-date caching |
| `dashboard.html` | Single-page dashboard — Control Panel with nested tabs |
| `config.example.json` | Config template with 34 condition definitions |
| `setup.sh` | One-command bootstrap (deps, API test, systemd/cron) |
| `systemd/` | Timer + service units for AEST game windows |

## Dashboard

NBA Monitor-style Control Panel with nested tabs:

```
🧭 Control Panel
├── Monitor Status (Overview | Logs | Odds API)
├── 🏉 Games (Live | Alerts | Upcoming | Post Game)
├── Configuration (General | Conditions | Compounds)
├── 📊 Analysis (ML Model Status | Condition Outcomes | Tag Analytics)
└── 📚 Game History (filters, game list, detail view)
```

**Game History** includes server-side filtering (team, condition, tag, season, segment, date range, free-text), paginated game list with tag pills, and drill-in detail with score breakdown, 13-stat team comparison, conditions fired, and try timeline.

**Team icons** — circular CSS badges with official NRL team colours and 3-letter abbreviations across all views.

## Condition Types (26)

### Stats-based — original (8)

| Type | What it checks |
|------|---------------|
| `score_diff` | Margin at 1H, 2H, or total game |
| `error_rate` | Errors + handling errors count |
| `completion_rate` | Set completion percentage |
| `penalty_count` | Penalties conceded |
| `possession` | Possession percentage |
| `try_scoring_run` | Consecutive tries by one team |
| `points_run` | Unanswered points scored |
| `missed_tackles` | Missed tackle count |

### PBP-based (6)

| Type | What it checks |
|------|---------------|
| `momentum_shift` | X+ unanswered points (from timeline scoring events) |
| `error_streak` | X consecutive errors by one team |
| `penalty_pressure` | X penalties in a 10-minute sliding window |
| `sin_bin` | Player sin-binned (team down to 12) |
| `line_break_surge` | X line breaks in a 10-minute window |
| `halftime_turnaround` | Trailing by X at HT but now leading |

### Stats-based — new (12)

| Type | What it checks |
|------|---------------|
| `run_metres` | All run metres (above/below threshold) |
| `post_contact_metres` | Post-contact metres (physical dominance) |
| `tackle_breaks` | Tackle break count |
| `line_breaks` | Raw line break count |
| `offloads` | Offload count (creative attack) |
| `intercepts` | Intercept count (defensive reads) |
| `ineffective_tackles` | Ineffective tackle count |
| `effective_tackle_pct` | Effective tackle percentage |
| `kick_defusal` | Kick defusal percentage |
| `kick_return_metres` | Kick return metres |
| `play_the_ball_speed` | Play-the-ball speed (fatigue indicator) |
| `red_card` | Player sent off |

## The Odds API

Live and historical bookmaker odds via [The Odds API](https://the-odds-api.com/):

```bash
# Test live odds
python3 backfill_odds.py --api-key YOUR_KEY --test-live

# Backfill historical odds
python3 backfill_odds.py --api-key YOUR_KEY --start-date 2025-01-01 --end-date 2025-12-31
python3 backfill_odds.py --api-key YOUR_KEY --dry-run
```

- **Sport key:** `rugbyleague_nrl`
- **Markets:** h2h (moneyline), spreads (handicap), totals (over/under)
- **Bookmaker priority:** TAB > PointsBet AU > Ladbrokes AU (#204)
- **Credit tracking:** persistent usage log, per-call records, low-credit warnings
- **Dashboard:** Odds API sub-tab with usage chart and call log
- **Config:** `odds_api_enabled`, `odds_api_key`

## ML Predicted Winner Model

Logistic regression trained on condition features + game context:

```bash
python3 ml_model.py --train --evaluate
```

- **35 features:** 26 condition binary + 9 context (score_margin, is_home, halftime_margin, h2_momentum, max_unanswered, max_error_streak, max_penalty_window, sin_bins, line_break_surge)
- **Training data:** 1,024 samples from 514 games (2024–2026)
- **Accuracy:** 99.7% training, 98.9% cross-validation
- **Top features:** score_margin (+5.2), halftime_margin (+4.1), h2_momentum (+3.7)
- scikit-learn is optional — degrades gracefully when unavailable

## Play-by-Play Analysis

The NRL match centre API provides a `timeline` array with detailed play-by-play events (avg 110 events per game, 22 event types). `timeline_features.py` extracts 21 momentum features:

- Scoring runs (consecutive tries, unanswered points)
- Error streaks (consecutive errors per team)
- Penalty windows (max penalties in any 10-minute window)
- Sin bin events and timing
- Line break surges
- Score progression with running scores
- Halftime margin, 2H momentum, margin trajectory

## Game Auto Recovery

Automatically detects and recovers games missed due to monitor downtime. Each poll cycle checks previous rounds for completed games not in history, fetches match data from NRL.com, replays conditions and PW calls, and writes a full game record. Preserved live state (conditions, PW calls, odds) from partial misses is merged with backfill data.

- **Config:** `auto_recovery_enabled`, `auto_recovery_pw_calls` (toggleable from dashboard)
- **CLI:** `python3 recovery.py scan --season 2026 [--dry-run]`
- **Dashboard:** "Game Auto Recovery" tab under Monitor Status
- **Systemd:** `nrl-recovery.timer` — daily scan at 14:00 UTC

See [docs/GAME_AUTO_RECOVERY.md](docs/GAME_AUTO_RECOVERY.md) for detailed architecture and flow.

## Historical Backfill

```bash
# Phase 1: Build local cache from NRL.com
python3 backfill.py --fetch-cache --season 2024,2025,2026

# Phase 2: Replay from cache → game_history.json
python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026 --skip-existing
```

**Current backfill:** 514 games across 3 seasons with full stats (32 keys avg), 56,488 timeline events, 6,197 conditions fired, postgame tags (175 BLOWOUT, 155 CLOSE, 104 COMEBACK).

Covers regular season (R1–R27) and finals (R28–R31). CLI flags: `--dry-run`, `--skip-existing`, `--resume`, `--delay`.

## NRL Data Source

All data comes from NRL.com internal APIs:

| Endpoint | Purpose |
|----------|---------|
| `/draw/data?competition=111&season={year}&round={N}` | Round fixtures with match states, odds, kickoff times |
| `/draw/nrl-premiership/{year}/round-{N}/{home}-v-{away}/data` | Per-game scores, stats (7 groups, 30+ stats), timeline events |

Match states: `Pre`, `Upcoming`, `InProgress`, `HalfTime`, `FullTime`, `Post`.

No authentication required. Browser User-Agent + Referer headers are used.

## Dashboard API (15 endpoints)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/config` | GET/POST | Read/write config with validation |
| `/api/alerts` | GET | Recent alert history |
| `/api/live-stats` | GET | Per-game live stats snapshot |
| `/api/ticker` | GET | Active condition hits this run |
| `/api/upcoming` | GET | Upcoming round fixtures with kickoff times/odds |
| `/api/history` | GET | Filtered, paginated game history (10 filter params) |
| `/api/history-game` | GET | Full game detail with stats/conditions/try events |
| `/api/history-facets` | GET | Facet counts for filter dropdowns |
| `/api/monitor-status` | GET | Polling status, config summary, disk space |
| `/api/log-events` | GET | Recent monitor.log entries |
| `/api/postgame` | GET | Recently completed games |
| `/api/analysis` | GET | Condition outcome stats and tag frequencies |
| `/api/model-status` | GET | ML model training metrics |
| `/api/odds-api-log` | GET | Odds API usage log with credit status |
| `/api/outcomes` | GET | Historical outcome statistics with recency weighting |

## Configuration

`config.json` key fields:
- `season` — NRL season year (default 2026)
- `monitor_all_teams` / `teams[]` — team filtering
- `slack_bot_token` / `slack_channel` — Slack delivery (preferred)
- `openclaw_gateway` / `openclaw_token` — fallback alert delivery
- `alert_cooldown_minutes` — cooldown for repeated alerts
- `end_of_game_alerts` — send final score alert when a game ends
- `outcomes_decay_lambda` — recency weighting for historical analysis
- `odds_api_enabled` / `odds_api_key` — The Odds API integration for live odds
- `conditions[]` — base condition definitions (34 in example config)
- `compound_conditions[]` — AND/OR rules over base conditions

See `config.example.json` for the full schema.

## NRL Teams (17)

| Short | Abbrev | Full |
|-------|--------|------|
| Broncos | BRI | Brisbane Broncos |
| Bulldogs | CBY | Canterbury-Bankstown Bulldogs |
| Cowboys | NQC | North Queensland Cowboys |
| Dolphins | DOL | Dolphins |
| Dragons | SGI | St. George Illawarra Dragons |
| Eels | PAR | Parramatta Eels |
| Knights | NEW | Newcastle Knights |
| Panthers | PEN | Penrith Panthers |
| Rabbitohs | SOU | South Sydney Rabbitohs |
| Raiders | CAN | Canberra Raiders |
| Roosters | SYD | Sydney Roosters |
| Sea Eagles | MAN | Manly Warringah Sea Eagles |
| Sharks | CRO | Cronulla-Sutherland Sharks |
| Storm | MEL | Melbourne Storm |
| Wests Tigers | WST | Wests Tigers |
| Titans | GCT | Gold Coast Titans |
| Warriors | WAR | New Zealand Warriors |

## Game Windows

NRL games are typically played Thursday through Monday evenings AEST:
- Thursday: 1 game (7:50 PM AEST)
- Friday: 2 games (6:00 PM, 8:00 PM AEST)
- Saturday: 3 games (3:00 PM, 5:30 PM, 7:35 PM AEST)
- Sunday: 1–2 games (2:00 PM, 4:05 PM AEST)
- Monday: occasional (7:00 PM AEST)

The default systemd timer polls every minute from 06:00–12:00 UTC, covering all standard AEST evening kickoffs.

## Systemd Setup

```bash
mkdir -p ~/.config/systemd/user
cp systemd/nrl-monitor.service ~/.config/systemd/user/
cp systemd/nrl-monitor.timer ~/.config/systemd/user/
cp systemd/nrl-dashboard.service ~/.config/systemd/user/
# Edit paths in copied files if needed
systemctl --user daemon-reload
systemctl --user enable --now nrl-monitor.timer nrl-dashboard.service
```

Verify:
```bash
systemctl --user status nrl-monitor.timer
systemctl --user status nrl-dashboard.service
```

## Related Projects

- **[nrl-stats](../nrl-stats/)** — NRL team stats fetcher and Excel workbook updater (same NRL.com APIs)
- **[NBA Monitor](https://github.com/bobcheong/NBAMonitor)** — The original project this is based on (ESPN APIs, NBA/WNBA)
