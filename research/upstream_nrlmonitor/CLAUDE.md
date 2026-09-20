# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

NRL Monitor is a polling-based NRL game intelligence service that evaluates live game conditions every minute during game windows (UTC 04:00–13:00 = AEST 14:00–23:00), sending alerts to Slack and serving a dashboard for live visibility, historical analysis, and predicted winner modelling.

Based on the architecture of the sibling NBA Monitor project (`~/.openclaw/workspace/scripts/nba-monitor/`), adapted for the NRL Telstra Premiership using NRL.com APIs.

**GitHub:** https://github.com/bobcheong/NRLMonitor (NRL), https://github.com/bobcheong/NBAMonitor (NBA/WNBA)

**Cross-repo workflow:** When a fix or feature applies to both NRL and NBA/WNBA repos, create/update separate GitHub issues in each repo (with cross-references), update `CHANGES.md` in both repos, and use each repo's own issue number in code comments.

**Code parity:** Maintain consistent patterns between NRL and NBA dashboards where possible. When adding shared features (tabs, filters, JS helpers, CSS classes), use the same variable names, function names, element IDs, localStorage keys, and fetch URL conventions. Before copying code between repos, check for repo-specific differences (e.g. NRL uses `const API = ''` prefix on fetch calls, NBA uses bare paths; NRL uses `--nrl-green` accent, NBA uses `--accent`). See #178 / NBA #339.

## Running the Project

```bash
# Create .venv with dependencies (matching NBA Monitor setup)
python3 -m venv .venv
.venv/bin/pip install requests scikit-learn

# Run one monitor cycle
.venv/bin/python3 monitor.py

# Start the dashboard server (port 8898)
.venv/bin/python3 server.py 8898
# → http://127.0.0.1:8898/dashboard.html

# Train the ML model (requires scikit-learn)
.venv/bin/python3 ml_model.py --train --evaluate
# Or via dashboard: Monitor Status → ML Training → Train Now

# Backfill historical data (2-phase)
.venv/bin/python3 backfill.py --fetch-cache --season 2024,2025,2026
.venv/bin/python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026
```

**Automated scheduling (production — Linux with systemd):**
```bash
cp systemd/nrl-monitor.{service,timer} ~/.config/systemd/user/
cp systemd/nrl-dashboard.service ~/.config/systemd/user/
cp systemd/nrl-backup.{service,timer} ~/.config/systemd/user/
cp systemd/nrl-recovery.{service,timer} ~/.config/systemd/user/
cp systemd/nrl-ml-train.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nrl-monitor.timer nrl-dashboard.service nrl-backup.timer nrl-recovery.timer nrl-ml-train.timer
```

**ChromeOS/Crostini (no systemd — uses supervisord):**
```bash
# supervisord.conf in NBA repo manages all 6 processes (NBA/WNBA/NRL)
# monitor_loop.py replaces systemd timer with a 30s polling loop
# See NBA #455 for full Crostini setup procedure
```

**View logs:**
```bash
tail -f logs/monitor.log
tail -f logs/dashboard.log
```

## Architecture

### Core Files

| File | Role |
|------|------|
| `monitor.py` | Main polling job — fetches NRL APIs, extracts PBP features from timeline, evaluates conditions, runs PW predictions, adaptive odds polling, sends Slack alerts, persists state |
| `server.py` | Dashboard HTTP backend — serves JSON APIs and static files (port 8898) |
| `nrl_api.py` | NRL.com API client — draw fixtures, match centre data, 17-team mappings with abbreviations/timezones |
| `conditions.py` | 28 condition evaluator functions (8 original + 6 PBP + 12 new stats + `first_team_scores` + `predicted_winner_threshold`) |
| `outcomes.py` | Historical analysis engine — outcome statistics, Bayesian blending (`live_analysis_for_game`), `ConditionEdgeStore`, polarity classification, PW version tracking, WAL helpers, backup/recovery |
| `timeline_features.py` | PBP feature extraction — 21 momentum features + per-quarter/half event counts from NRL match timeline events |
| `ml_model.py` | Logistic regression predicted winner model — trains on condition features + game context |
| `ml_train_runner.py` | ML training wrapper — 3-gate decision logic (enabled, staleness, hash), PID lock, log capture, history persistence. Triggered by systemd timer (#165) |
| `odds_api.py` | The Odds API client — live/historical odds, spreads, moneylines (sport: `rugbyleague_nrl`), odds monitor cache |
| `odds_monitor.py` | Background odds monitor — 60s polling from configurable minute (default 65) + ET, live snapshots, +EV analysis vs historical late-tries probability. SOO games auto-detected with separate sport key (#104) |
| `backfill.py` | 2-phase historical backfill — fetch-cache + replay-write for game_history.json; optional `--pw-calls` for PW generation |
| `backfill_odds.py` | Historical odds backfill from The Odds API with per-date caching |
| `pw_trend.py` | PW call performance tracking — snapshot computation, changelog, version resolution, bootstrap CLI |
| `pw_matrix.py` | PW ROI Matrix — 5-dimension cross-tab computation (Confidence Band, Consensus, Half, Quarter, Spread Role), 9 pair pivots |
| `evaluate_model.py` | Chronological model evaluation — Brier/LogLoss/ECE for 5 targets, rolling-origin + holdout, snapshot history |
| `pw_compare.py` | PW config comparison and filter evaluation — A/B testing, vs-stored, suppression filter analysis (#116) |
| `pw_roi_report.py` | Cross-league PW ROI report generator — reads NBA/WNBA/NRL history, produces `pw_roi_report.html` with dynamic insights, BK ROI heatmaps split by Leading/Trailing with Edge Range filter (#128, #155, #188) |
| `synthetic_odds.py` | Synthetic live odds generator — spread (linear, slope=-0.953), moneyline (logistic k=0.14 + pregame). Decimal odds output. Called by monitor.py every poll cycle (#235) |
| `analyse_bk_odds.py` | BK odds analysis script — 7-section HTML report comparing game state vs BK odds (#235) |
| `backfill_synth_odds.py` | Backfills `synth_moneyline`, `synth_spread`, `synth_h1_ml` onto historical PW calls (#235) |
| `backfill_q_correct.py` | Backfills `q_correct` and `h1_correct` onto PW calls from score_progression + 1H scores (#238) |
| `runtime_backup.py` | Runtime file snapshot creation, restore, pruning, orphan `.tmp` cleanup, PW trend snapshot |
| `recovery.py` | CLI for backup listing, restore with WAL replay, WAL status/truncate, auto-recovery gap scan (#136) |
| `dashboard.html` | Single-page dashboard — Control Panel with nested tabs (NBA Monitor-style layout) |
| `monitor_loop.py` | Polling loop wrapper — replaces systemd timer on ChromeOS/Crostini, runs monitor.py every 30s (#299) |
| `config.json` | Runtime config (excluded from git — use `config.example.json` as template) |

### Data Flow

```
Systemd timer (every minute, UTC 04:00–13:00)
  → monitor.py
      → NRL Draw API (round fixtures / scoreboard)
      → NRL Match Centre API (per-game stats + timeline)
      → Pass 1: Evaluate 28 base condition types per monitored team
      → Evaluate compound conditions (AND/OR)
      → Apply per-condition cooldowns
      → Adaptive odds polling (5min H1 / 2min late H2)
      → Pass 2: PW evaluation (ML + historical blend → PW threshold conditions)
      → Record PW calls in state + live_stats.json
      → Compute synthetic odds (synthetic_odds.compute_all) → live_stats.json + PW call records
      → Send Slack alerts (base + PW threshold alerts)
      → Write: alerts.json, live_stats.json, game_ticker.json, .alert_state.json
      → Append to game_history.wal (WAL) then game_history.json (at game end)
      → Game-end: enriched record (source:live, 30 stats, PW calls, conditions, tags, odds, try events)
      → Game-end: odds_monitor.persist_game() saves snapshots to odds_monitor_history.json
      → Cache raw NRL API data to nrl_cache/{season}/ for future backfill replay (#64)

server.py (long-running, port 8898)
  → Serves dashboard.html + /api/* endpoints
  → POST /api/config validates and atomically writes config.json
  → Pre-config-save snapshot before config writes
  → Backup/recovery API endpoints (6 endpoints)
  → PW API endpoints: /api/pw-calls, /api/pw-trend, /api/pw-roi-combos

backfill.py (batch, run manually)
  → Phase 1: fetch draw + match data → nrl_cache/<season>/
  → Phase 2: replay from cache → game_history.json with full stats + PBP features
  → Optional: --pw-calls generates predicted_winner_calls[] per game record

ml_model.py (batch, run manually)
  → Trains on game_history.json → model_nrl.pkl + model_meta_nrl.json

Systemd timer (daily at 16:00 UTC, outside game window)
  → runtime_backup.py snapshot --label scheduled --keep-last 10
      → Copies 15 runtime files to runtime_backups/<timestamp>-scheduled/
      → Writes metadata.json (config fingerprint, file sizes, record counts)
      → Cleans up orphaned .tmp.<pid> files
      → Truncates WAL (keeps entries for last 5 backups)
      → Appends PW trend snapshot via pw_trend.append_snapshot()

Systemd timer (daily at 14:00 UTC, after game window)
  → recovery.py scan --season 2026 (#136)
      → Iterates all rounds + SOO fixtures
      → Detects completed games not in game_history.json
      → _recover_missed_game(): fetch API → build record → conditions → PW calls → write
```

### Dashboard Structure (NBA Monitor-style)

```
🧭 Control Panel
├── Monitor Status
│   ├── Overview (polling, NRL API, round, live games, ML model status, scheduler, last backup, disk, Odds API credits)
│   ├── Logs (NBA-style: card entries with tag pills, filter chips, raw mode with file/tail/search/live tail)
│   ├── Odds API (#176: inner sub-tabs)
│   │   ├── Usage (charts, time range filter, interval selector, credit stats, BK coverage, fallback events)
│   │   └── API Test (#175, #176: league dropdown, market checkboxes, event fetcher, request/response viewer)
│   ├── ML Training (Train Now, staleness detection, history table, log viewer, auto-training status, 3s polling)
│   ├── Recovery (WAL status, backup list with restore/delete, retention prune)
│   └── Game Auto Recovery (#136: enable/disable toggles, last scan info, recovery log table with Full/Merged/Failed states)
├── 🏉 Games
│   ├── 🔴 Live Games (game cards: score+1H, PW with consensus/polarity/edge/scenario badges (#233), pregame/live odds bar, synthetic odds line, collapsible 12-stat grid with Full/1H/2H + Q1/Q2/Q3/Q4 scope buttons (#225), accumulated condition pills with timestamps, scoring timeline with player names, lead summary, NRL.com link, SOO badge, upcoming games line)
│   ├── 🎯 Predicted Winners (NBA-style: collapsible game cards with season/segment labels (#244), per-half call tables, accuracy stats block with price source breakdown (#258), Q/H Performance with synth ML/Spr + BK game-level stats (#245), Best Filter Combos with Apply button (resets non-exempt filters #283) + Clear link + Restore previous filters (#283, #259), 6-dim sweep incl. Consensus (#284), lock persistence (#285) and help modal (#243), pagination, Live Only filter (#258), Src column (LIVE/PRE/SYNTH per call #258), filter persistence, aggregate synth Q/H1 scenario badges with toggle (#238, #239))
│   ├── 🔔 Alert History (alerts with team icons)
│   ├── ⏰ Upcoming Games (kickoff times, odds, venues, countdowns, SOO fixtures merged with badges)
│   └── 📋 Post Game Summary (recently completed)
├── Configuration
│   ├── General Settings (season, teams, cooldown, alerts)
│   ├── Conditions (editable: add/edit/delete, 28 types, full-width dashed Add button, per-tab Save/Reset)
│   ├── Compounds (editable: AND/OR, checkbox refs, dashed Add button, help note)
│   ├── Settings (Teams, Dashboard bg, Alerting, Model Eval, ML Training, Upcoming, Scheduler, Log Rotation, Backups)
│   ├── Post-Game (summary windows, dynamic tag manager with add/remove/reorder, built-in tag protection)
│   └── PW Model (alerts, adaptive thresholds, confidence modifiers, half weights, gates, eval cadence, ML blend, Bayesian priors, Condition Edge Weights table)
├── 📊 Analysis
│   ├── Outcomes (NBA parity: ML Status, 10-col Condition table, Compound Outcomes, Condition Combos, Tags, PW Accuracy)
│   ├── 📈 PW Trends (Chart.js: 4 chart modes, 8 filters incl. season/segment, changelog table, version cohorts)
│   ├── 📊 ROI Matrix (5-dim heatmap incl. quarter + ROI Combos: half+quarter filter combos, 14-col table, 7 sort options)
│   ├── 🔬 BK Odds (iframe: analyse_bk_odds report — 7 sections, Regenerate button)
│   ├── 📡 Odds Monitor (live odds from min 65+ET: 60s polling, Chart.js line chart, +EV analysis, late-tries probability, expandable history with view persistence, aggregate stats: avg tries/over-under/+EV under ROI strategy (#133))
│   └── 🏉 Spread Strategy (#211, #213, #229: merged leading+trailing strategies — no role dropdown, both computed per game with margin_context dimension, price threshold with live recomputation + "Best" checkbox (#231), bet 1u when spread ≥ threshold, 7 history filters (Side/Role/Margin/Entry/EV/Context/Price). #226/#227/#228: view toggle BK Spread / Historical Win Rate — historical: 870+ games from PBP cache, 3 perspectives, 7 filters, multi-minute, 5D combo table, paginated game list, Min Games/pp, dual patterns in Final Stretch. #230: BK variable-dimension combos 2-5D with configurable range + Min Games/Min ROI controls. #231: threshold consistency — analysis uses user threshold by default, pattern labels show @$X.X. #232: dimension column muting — unfiltered dims grey out, table title with bet/game counts)
├── 📚 Game History
│   ├── Filter bar (team, condition, tag, season, segment, source, dates, search + PW filters — all persisted to localStorage)
│   ├── Game list table (scores, 1H, season, segment, tags, conditions count, PW calls/accuracy — #244)
│   └── Game Detail (score breakdown, 13-stat comparison, conditions with sort toggle + color margins + source label, PW calls table with Synth column, try timeline)
└── 📋 Custom Queries
    ├── First Scorer Analysis (summary + per-team breakdown table)
    ├── 1H Totals Over/Under
    ├── Late 2H Tries (1+, 2+, 3+)
    ├── Trailing 20+ with Late Tries
    ├── First Scorer Wins 1H → Wins Game? (summary stat)
    ├── First 2H Scorer → Wins Game? (summary + per-team table)
    ├── Leading Team Scores Late Try → Wins? (10/7/5/2 min windows)
    └── Trailing Team Scores Late Try → Wins? (10/7/5/2 min windows)
```

### API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/config` | GET/POST | Read/write config with validation |
| `/api/alerts` | GET | Recent alert history |
| `/api/live-stats` | GET | Per-game live stats snapshot |
| `/api/ticker` | GET | Active condition hits this run |
| `/api/upcoming` | GET | Upcoming round fixtures with kickoff times/odds |
| `/api/history` | GET | Filtered, paginated game history |
| `/api/history-game` | GET | Full game detail with stats/conditions/try events |
| `/api/history-facets` | GET | Facet counts for filter dropdowns |
| `/api/monitor-status` | GET | Polling status, config summary, disk space, Odds API credits |
| `/api/log-events` | GET | Structured log events parsed from monitor.log — `{ts, tag, message}` with tag classification. Params: limit (default 500, max 2000) |
| `/api/log-tail` | GET | Raw log file tail content with metadata. Params: file (monitor.log/dashboard.log), bytes (1024-262144) |
| `/api/condition-edges` | GET | Per-condition fire count, win count, win%, and edge (win% - 50%) from ConditionEdgeStore. Sorted by edge descending. |
| `/api/ml-training-status` | GET | Model meta, SHA256 staleness detection, config, training state, history (last 20) |
| `/api/ml-train` | GET/POST | GET: training state. POST: trigger background `ml_model.py --train` (202/409) |
| `/api/ml-training-log/{file}` | GET | Training log file content by filename |
| `/api/postgame` | GET | Recently completed games |
| `/api/analysis` | GET | Enhanced condition outcomes (10 metrics), compound outcomes, condition combinations (2-5 way), tag analytics, PW accuracy breakdown (by band/consensus with ROI). Params: season, segment, min_sample, max_combo_size, margin_fired, search |
| `/api/model-status` | GET | ML model training metrics |
| `/api/odds-api-log` | GET | Odds API usage log with credit status |
| `/api/odds-api-stats` | GET | Call results by context, BK coverage, fallback events |
| `/api/odds-api-test-events` | GET | Fetch events for a sport key (~1 credit). Params: sport |
| `/api/odds-api-test` | GET | Fetch odds for specific event. Params: sport, event_id, markets |
| `/api/odds-api-fallback-data` | GET | Saved fallback request/response data (#179). Params: ts (optional, returns specific entry or all) |
| `/api/outcomes` | GET | Historical outcome statistics |
| `/api/snapshots` | GET | List runtime backup snapshots |
| `/api/snapshots/create` | POST | Create manual snapshot |
| `/api/recovery/backups` | GET | List game_history backups + WAL status |
| `/api/recovery/restore` | POST | Restore from backup with optional WAL replay |
| `/api/recovery/delete-backup` | POST | Delete a single backup |
| `/api/recovery/prune-backups` | POST | Delete backups older than N days |
| `/api/pw-calls` | GET | Games-grouped PW calls with accuracy stats + 7 ROI metrics, live PW calls from state, call enrichment (quarter, live_line, bk_live_line, spread_role, moneyline, bk_*, condition_edge, avg_edge — all derived from game record when missing). Filters: season, segment, half, quarter, spread_role, margin_at_fire, call_selection, pw_source, pw_edge, pw_polarity (#122) |
| `/api/pw-trend` | GET | PW trend data (changelog + snapshots with cumulative/window/role/version/edge/filter metrics + `cumulative_snapshot`). Params: season, segment, date_from, date_to, last_n_games, interval, cumulative, source |
| `/api/pw-roi-combos` | GET | PW ROI filter combos — 36 combos (role x margin x call_selection x half) with full ROI metrics. Params: season, segment, sort (ml_roi_pct default), limit, min_calls |
| `/api/pw-matrix` | GET | PW ROI matrix — 6 cross-dimensional pair pivots with per-cell accuracy/ROI metrics. Filters: season, segment, matrix_pin_{threshold,consensus,half,spread_role} |
| `/api/pw-scenarios` | GET | Per-scenario aggregate stats (accuracy, BK ROI). Params: call_selection, season_segment, season_year |
| `/api/generate-bk-odds-report` | POST | Regenerate BK odds analysis HTML report (#235) |
| `/api/model-eval` | GET | Model evaluation metrics (Brier/LogLoss/ECE) for 5 targets. Optional `?persist=1&force=1` to save snapshot |
| `/api/model-eval-history` | GET | Snapshot history (compact Brier per target per snapshot) |
| `/api/odds-monitor` | GET | Live odds monitor status, per-game snapshots, late-tries probability reference |
| `/api/odds-monitor-history` | GET | Persisted historical odds monitor data (all games, indefinite retention) |
| `/api/odds-monitor-leading` | GET | Leading team spread strategy — games with leading_strategy results, aggregate stats, leading team try probability (#211) |
| `/api/odds-monitor-analysis` | GET | Spread analysis — threshold sweep, dimensional breakdowns (15 dims incl. margin_context + 5 game_history-enriched), variable-dimension combos (2-5D), 5D combo table, patterns. Params: `combo_min`, `combo_max`, `threshold` (float or "best") (#216, #226, #229, #230, #231) |
| `/api/odds-monitor-historical` | GET | Historical win rate analysis from game_history + PBP cache (870+ games). Params: `role` (leading/trailing/final_stretch), `minute` (65-75, default 70) (#226) |
| `/api/odds-monitor/start` | POST | Start odds monitor background thread |
| `/api/odds-monitor/stop` | POST | Stop odds monitor background thread |
| `/api/auto-recovery` | GET | Auto-recovery status: enabled, last scan, recovery log (#136) |
| `/api/auto-recovery/toggle` | POST | Toggle `auto_recovery_enabled` or `auto_recovery_pw_calls` in config |

### NRL-Specific Design

- **Game structure:** 2 x 40-minute halves (1H/2H) + optional golden-point extra time. Finals: 4 weeks (Week 1: 4 games, Week 2: 2, Preliminary: 2, Grand Final: 1)
- **Finals support (#298):** `nrl_api.py` exports `MAX_REGULAR_ROUND=27`, `MAX_FINALS_ROUND=31`, `_is_finals_round()`, `_detect_finals_week()`. NRL API returns `matchId=null`, `roundNumber=null`, `startTime=null` for finals — `matchCentreUrl` used as `match_id`, `clock.kickOffTimeLong` as start time, synthetic round numbers 28-31 derived from `roundTitle`. Finals games tagged `season_segment: "finals"` in both live and recovery paths. API returns same fixtures for any round ≥ 28 — deduped via `matchCentreUrl` set comparison.
- **Data sources:** NRL.com internal APIs (no auth) + The Odds API (`rugbyleague_nrl` for Premiership, `rugbyleague_nrl_state_of_origin` for SOO)
- **Game windows:** Thu–Mon AEST 14:00–23:00 (UTC 04:00–13:00)
- **Teams:** 17 NRL teams + 2 SOO teams (Blues NSW, Maroons QLD) with 3-letter abbreviations in `nrl_api.py`
- **State of Origin (#102):** Competition ID 116, auto-detected in polling loop. Isolated via `season_segment=state_of_origin` + `soo_game=1/2/3`. Excluded from ML training. Odds via `rugbyleague_nrl_state_of_origin` sport key. "State of Origin" segment + "Origin Game" (1/2/3) sub-filter on all tabs: PW, PW Trends, ROI Matrix, Outcomes, Game History, Custom Queries. `soo_game` param on all server endpoints. Backfilled 2016-2025 (30 games, 273 conditions, 78 PW calls at 89.7%).
- **Stats:** NRL-specific: errors, set completion %, penalties, possession %, missed tackles
- **PBP:** NRL timeline events (avg 110/game) — tries, errors, penalties, line breaks, sin bins, interchanges
- **Dashboard port:** 8898 (NBA Monitor uses 8899)
- **Team icons:** CSS circular badges with official NRL team colours (no external images)
- **Synthetic quarters (#31):** Q1 (0-20'), Q2 (20-40'), Q3 (40-60'), Q4 (60-80'), ET (80'+) — derived from `game_minute` at serve-time for PW analysis, ROI Matrix, and PW Trends. Doesn't affect PW eval cadence (stays H1/H2 phase-based). `quarter` field persisted on live PW calls, derived at serve-time for backfill calls.
- **NRL API matchState (#52):** API uses `FirstHalf`/`SecondHalf`/`ExtraTime` (not `InProgress`). `LIVE_STATES` includes all variants + `matchMode=="Live"` fallback. Stats in `stats.groups[].stats[]` format with `homeValue`/`awayValue` objects — `_STAT_TITLE_MAP` maps 27 titles to camelCase keys.
- **Monotonic game_minute (#333):** `_clamp_game_minute(game, match_id, state)` called once per game in polling loop. Tracks `max_game_minute_{match_id}` in state, prevents NRL API clock from going backwards between polls (clock corrections, stoppages, caching). `_parse_game_minute()` = raw, `_get_game_minute()` = clamped if available. Only affects poll-time metadata — try events/PBP use `gameSeconds` from timeline API.

### Condition Types (28 total)

**Stats-based (8):**

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

**PBP-based (6):**

| Type | What it checks |
|------|---------------|
| `momentum_shift` | X+ unanswered points (from timeline) |
| `error_streak` | X consecutive errors by one team |
| `penalty_pressure` | X penalties in a 10-minute window |
| `sin_bin` | Player sin-binned (team down to 12) |
| `line_break_surge` | X line breaks in a 10-minute window |
| `halftime_turnaround` | Trailing by X at HT but now leading |

**Stats-based (12 new):**

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

**Event-based (1):**

| Type | What it checks |
|------|---------------|
| `first_team_scores` | First team to score in the match (try, penalty goal, field goal) |

**Predicted Winner (1):**

| Type | What it checks |
|------|---------------|
| `predicted_winner_threshold` | Blended PW confidence crosses threshold (60/75/90%). Uses `min_half` (1=H1+, 2=H2+) |

**Quarter-scaled thresholds (#69):** Cumulative stat conditions support `quarter_scale: true` in config. When enabled, threshold scales by `min(game_minute / 80, 1.0)` — prevents early false fires. Applies to: error_rate, penalty_count, missed_tackles, run_metres, post_contact_metres, tackle_breaks, line_breaks, offloads, intercepts, ineffective_tackles, kick_return_metres. Does NOT apply to percentage/rate conditions (completion_rate, possession, effective_tackle_pct, kick_defusal, play_the_ball_speed).

**Quarter/half-scoped conditions (#120):** `error_rate`, `penalty_count`, and `line_breaks` support `scope: "quarter"` or `scope: "half"` config. When set, counts timeline events (Error, Penalty, LineBreak) within the current synthetic quarter or half instead of using full-game API cumulative stats. Default `scope: "game"` preserves existing behavior. Requires PBP features from `timeline_features.extract_features()` — works in both live (#119) and backfill.

**Live PBP features (#119, #126):** `monitor.py` calls `timeline_features.extract_features()` on raw timeline data during each poll and passes `pbp_features` to `build_team_context()`. All 6 PBP-based conditions (momentum_shift, error_streak, penalty_pressure, line_break_surge, try_scoring_run, points_run) now fire during live games. Prior to #119, these conditions had hardcoded zeros and could not fire live. **Note:** #119 had a teamId type mismatch bug (str vs int) that silently kept PBP at 0 — fixed in #126 by normalizing to int in `extract_features()`.

**Condition name/config scope validation (#185, NBA #347):** Condition names referencing a specific half/quarter (e.g. "in 2H", "1H", "2H") must have matching `period`/`scope` config. NRL "Trailing by 18+ in 2H" had `period: "game"` — fired during Q1/Q2 (21 PW calls affected). Fixed to `period: "2H"`. NBA had 4 FG% conditions with `scope: "quarter"` instead of `scope: "half"` — fixed in NBA #347.

### Predicted Winner (PW) System

Two-pass live evaluation adapted from NBA Monitor for NRL's 2×40-min half structure:

**Pass 1:** Evaluate all 27 base conditions → collect active hits per game
**Pass 2:** Compute PW analysis → evaluate `predicted_winner_threshold` conditions

**Prediction pipeline (Phase 2 — Bayesian blending):**
```
Active conditions for game
  → Frequency lookup: historical win% for each condition combo
  → Bayesian posterior: Beta priors (team_wins 13/7, upset 7/13, close 8/12, comeback 3/7)
  → Sample-adaptive blending: 70% Bayes / 30% freq at <15 samples → 20%/80% at 50+
  → ML predict_winner() from ml_model.py (logistic regression)
  → Consensus: strong (ML + historical agree), conflicted (disagree), ml_only, historical_only
  → Blend ML + Bayesian: 70% ML / 30% historical (configurable pw_blend_weights)
  → Apply half weights: H1 ×0.90, H2 ×1.05, ET ×1.10
  → Apply margin boost if predicted team is leading (1.05 in late H2)
  → Suppression gates: margin (-12 general / -8 late)
  → Duplicate suppression: skip if same team + same score as last call (#60)
  → Halftime suppression: skip eval when period=="HT" (#60)
  → Invalid ET suppression: skip eval at min 80+ when not in extra time (#60)
  → If blended pct ≥ threshold → record PW call + fire threshold conditions
```

**Bayesian engine (`outcomes.py`):**
- `live_analysis_for_game()` — full prediction with frequency + Bayesian posterior per condition combo
- `ConditionEdgeStore` — per-condition team-win rate tracking (edge = win% - 50%)
- Polarity classification: name-based word matching for direction-dependent types (#118), falls back to type-based sets (11 positive types, 8 negative types)
- `resolve_pw_version()` — delegates to `pw_trend.py` changelog-based resolution (falls back to `PW_VERSIONS` table)

**PW evaluation cadence (per-phase, configurable):**

| Game Phase | Default Interval | Config Key |
|------------|-----------------|------------|
| H1 (0'–40') | Every 10 min | `pw_eval_interval_h1` |
| H2 early (40'–60') | Every 5 min | `pw_eval_interval_h2` |
| H2 late (60'+) & ET | Every 2 min | `pw_eval_interval_late` |

Late threshold configurable via `pw_eval_late_threshold_min` (default 60).

**Score-change trigger (#24):** When a try or penalty goal changes the score between polls, PW eval fires immediately regardless of cooldown. Controlled by `pw_eval_on_score_change` (default `true`). Tracks `last_score_{game_id}` in state to detect changes.

**NRL-specific calibration (vs NBA):**

| Aspect | NBA | NRL |
|--------|-----|-----|
| Time periods | Q1-Q4, OT | H1, H2, ET |
| Margin gate (general) | -15 | **-12** (≈2 converted tries) |
| Margin gate (late) | -10 | **-8** (H2 60'+) |
| Half/quarter weights | Q1:0.85–Q4:1.10 | H1:0.90, H2:1.05, ET:1.10 |
| Eval cadence | Per-quarter | **Per-phase: 10/5/2 min** |
| Odds polling | Fixed | **Adaptive: 5min H1 / 2min late** |

**PW Dashboard (NBA Monitor layout, #17 + #19):**
- **Accuracy stats block** — bordered card: Accuracy%, Calls, Games, Won, Flat ROI, ML ROI, Spread Covered, Spread ROI, Live Spread ROI, BK Odds ROI, BK Spread ROI, Leading/Trailing splits. ROI pills hidden when null. Colors: green/red for ROI, green ≥55%/yellow ≥50%/red for spread covered. **Computed client-side (#186)** from filtered `/api/pw-calls` data — all filters (edge, polarity, consensus, source) reflected in stats. Previously server-side via `/api/outcomes` which missed edge/polarity params.
- **Collapsible game cards** — `<details>` with date/teams header, bright blue (#4fc3f7) nav links (🏉 Live, 📋 History, 🏉 NRL.com), PW header (muted "PW:" + green bold team name + %). Second header line visible when collapsed: Winner by margin, PW Won YES/NO, Final score, Odds, Spread → Live → BK (#4fc3f7), covered/missed by X, FAV/DOG badge.
- **Per-quarter call tables** — Q1/Q2/Q3/Q4/ET grouped (headers in #4fc3f7), 15 columns: Time, Game Time, Predicted, Win%, Score, Margin, Role, Odds, Handicap, Live Spread, BK Odds (#4fc3f7 + bookmaker tooltip), BK Spread, Polarity, Edge, Details (badges + expandable conditions in #4fc3f7)
- **Live PW calls** — `/api/pw-calls` includes live calls from `.alert_state.json` with `source: "live"`, live game cards auto-open. Live merge skipped when season filter excludes current season (#62).
- **Pagination** — First/Prev/Next/Last with 5/10/20/50 per page (localStorage)
- **Filter persistence** — Segment, Season, Role, Margin, Half, Quarter, Call Selection (incl. First Per Quarter), Source (game/call live/backfill), Edge Range, Polarity, ML ROI cb+min, AND/OR, Spr ROI cb+min, Strict BK with "Set as default" checkbox
- **Accuracy stats pills** — Polarity (Pol+/Pol−/Pol=) and Edge range (≥10/5-10/0-5/<0) breakdown with per-subset accuracy (#122)
- **Scenario badges (#233, #234):** ML/Spr ROI per 4D scenario (role|margin|edge|quarter). Live game cards fetch `_scenarioStatsCache` in parallel with live stats. Null `avg_edge` treated as `0-5` bucket so badges always render. Edge column shows `—` for null edge.
- **Toggle buttons** — Show/Hide Suppressed (45% opacity), Collapse/Expand All

**PW call record fields:** `ts`, `predicted_team`, `pct`, `winner_score_pct`, `consensus` (strong/conflicted/ml_only/historical_only), `blended`, `basis` (combo/ml/historical), `half`, `quarter` (Q1/Q2/Q3/Q4/ET — derived from game_minute, live calls persist, backfill derive at serve-time), `game_minute`, `score_margin_at_fire`, `home_score_at_fire`, `away_score_at_fire`, `active_conditions[]` (with `condition_edge` — sign-flipped for opponent conditions), `spread_role`, `moneyline` (pregame), `spread` (pregame), `live_moneyline`, `live_spread`, `bk_moneyline`, `bk_spread`, `bk_name`, `polarity_pos`, `polarity_neg`, `polarity_net`, `avg_edge`, `correct`, `synthetic`, `source` (live/backfill — #121), `pw_version`, `synth_moneyline` (decimal), `synth_spread`, `synth_h1_ml` (always None for NRL), `q_correct` (derived from score_progression, #238), `h1_correct` (derived from 1H scores, Q1/Q2 only, #238)

**Pregame odds capture:** `monitor.py` captures odds snapshot as `pregame_odds_{game_id}` in state when a game first appears live. Pregame odds (`moneyline`, `spread`) stored separately from live-polled odds (`live_moneyline`, `live_spread`). BK fields (`bk_moneyline`, `bk_spread`, `bk_name`) from live poll at PW fire time.

**Kickoff time capture:** `monitor.py` captures `kickoff_utc_{match_id}` in state when a game first goes live (from `game.start_time`). Used as fallback at game-end since NRL API `startTime` can be empty for completed games. Game-end records also derive `date` field from kickoff time.

**Backfill PW calls:**
```bash
# Generate PW calls for historical games (opt-in, default off)
python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026 --pw-calls
python3 backfill.py --use-cache --write-canonical --season 2026 --pw-calls --pw-threshold 65 --pw-cooldown-minutes 5 --pw-force
```

Backfill scores use exact PBP `score_progression` from NRL timeline (not linear interpolation).

**Backfill PW accuracy safeguards (#40):**
- ML model disabled during backfill (`ml_model = None`) — prevents data leakage from future-trained model
- Lookahead-safe prior — historical condition win% computed from games with dates strictly before current game only
- Cumulative condition snapshots — PBP conditions (score_diff, momentum_shift, points_run, halftime_turnaround) filtered by `game_seconds_at_fire <= eval_seconds`; stats-based conditions always included
- Serve-time derivation — `spread_role`, `moneyline`, `spread` derived from parent game record when missing on PW calls (#42). BK fields (`bk_moneyline`, `bk_spread`, `bk_name`) are NOT enriched from pregame odds — only genuine live bookmaker data is shown (#56)

**Backfill condition margins (#18):**
- PBP conditions (momentum_shift, points_run, score_diff, etc.) record the score at fire time using `score_progression` lookup
- `score_diff` conditions scan score_progression for when margin threshold was first crossed
- Stats-based conditions (errors, tackles, etc.) use final margin (cumulative, no single fire point)
- `game_seconds_at_fire` field stored on conditions with identifiable fire times (23% of all conditions)

**PW Trends Dashboard (#26):**

Chart.js-powered trend analysis tab under Analysis, matching NBA Monitor layout:

- **4 chart modes:** Performance (Accuracy + ROI), Unit P&L, Rolling Accuracy (5-snapshot window), Bankroll ($10k sim at $100/bet)
- **9 filters:** Season, Segment (Regular/Finals), Role (Favorite/Underdog), Half (H1/H2/ET), Quarter (Q1/Q2/Q3/Q4/ET), Calls (First/Last/1st/Half/1st/Qtr), Margin (Leading/Trailing), Chart Type, Window (Cumulative/30/60/90)
- **PW Logic Changelog table** — 13 columns with version cohort metrics per-row, "All prior" (v0-unknown) + "All" totals rows, GitHub issue links
- **Version Cohort Comparison table** — 12 columns grouping calls by algorithm version, "All Versions" totals row
- **Edge Range Breakdown table** — per-edge-range (10+/5-10/0-5/<0) accuracy and ROI from `cumulative_snapshot.edges`
- **Filter + legend state** persisted to localStorage (`pw_trend_state`, `pw_trend_legend`)
- **Chart.js v4** loaded via CDN (`https://cdn.jsdelivr.net/npm/chart.js@4`)

**PW Trend backend (`pw_trend.py`):**

```bash
python3 pw_trend.py snapshot     # Append snapshot from current game_history
python3 pw_trend.py bootstrap    # Build snapshots from existing runtime_backups/
python3 pw_trend.py show         # Print summary
```

- Snapshots auto-appended during scheduled runtime backups (`runtime_backup.py`)
- Data file: `pw_trend_nrl.json` (changelog + time-series snapshots)
- Each snapshot includes: cumulative metrics, rolling windows (30/60/90), per-role breakdown, per-version cohorts, per-edge-range breakdown (10+/5-10/0-5/<0), cross-dimensional filter breakdowns (role x half x call_sel x margin)
- ROI uses **decimal odds** math: `profit = stake * (decimal_odds - 1)` (not American moneyline)
- `resolve_pw_version(date_str)` walks changelog entries to assign version tags (v0-unknown through v6-live-pbp-parity)
- Version resolution used by `monitor.py`, `backfill.py`, and `outcomes.py` (all delegate to `pw_trend.py`)

**NRL PW changelog entries:**

| Date | Tag | Description | Issue |
|------|-----|-------------|-------|
| 2026-04-20 | v1-pw-launch | Bayesian blending + ML consensus + per-phase eval | #15 |
| 2026-05-04 | v2-pw-tab-redesign | NBA-style game cards, PBP score progression | #17 |
| 2026-05-10 | v3-nba-parity | ROI metrics, pregame odds, BK fields, 15-col table | #19 |
| 2026-05-15 | v4-score-trigger | Score-change triggered PW eval | #24 |
| 2026-05-18 | v5-scoring-moments | Backfill eval at actual PBP scoring moments | #25 |
| 2026-05-29 | v6-live-pbp-parity | Live PBP features, polarity fix, quarter-level stats | #118, #119, #120 |

**Analysis/Outcomes Dashboard (#32):**

Outcomes sub-tab under Analysis, matching NBA Monitor's Analysis tab:

- **Enhanced filter bar:** Min sample (1-50), Max combo size (2-5), Sort (8 options), Order, Season, Segment, Role (Favorites/Underdogs), Margin when fired, Search. **All filters persisted to localStorage** (`analysis_filters` key).
- **Condition Outcomes (11 columns):** Condition (bright blue #4fc3f7, dotted underline, ↗ arrow, clickable drill-down), Games, Team Wins, Upset%, Close%, Blowout%, Comeback%, +Δ%, −Δ%, Avg Δ, Covered%. All metric cells use `pctBar()` (120px-wide, 6px-tall colored horizontal bars with right-aligned percentage). Upset% computed from pregame decimal odds.
- **Compound Outcomes:** Same table with blue `rgba(59,130,246,0.15)` "compound" badge on names
- **Condition Combinations:** 2-5 way co-fires, top 100. Green `rgba(34,197,94,0.15)` rounded pills. Same 11 columns with pctBar + avgDiffCell. Drill-down click + hover highlight.
- **Tag Analytics:** 4-column table (Tag, Games, Eligible Games, Rate) with thin 8px accent-colored progress bar matching NBA `renderTagOutcomes()`
- **PW Accuracy section:** NBA-style large-font summary card (Overall Accuracy 1.6rem, Calls correct, Games tracked, Games won, Games won %, Flat ROI with P&L, ML ROI with P&L, Spread ROI with cover count, BK Spread ROI) + by-confidence-band table (9 cols) + by-consensus table (9 cols). Backend `_build_pw_accuracy_summary()` aggregates top-level ROI from threshold bands.
- **Row interactions:** Hover highlight (background toggle), drilldown arrow ↗, cursor:pointer
- **Help modals:** Conditions, PW Accuracy, ML Model Status, Condition Combinations, Game History, Alert History, Upcoming Games, Post Game Summary (14 total across dashboard)
- **Client-side helpers:** `_pctBar()` (bar+text), `_avgDiffCell()` (color-coded +/- margin), `_saveAnalysisFilters()` / `_restoreAnalysisFilters()` (localStorage)
- **Model Evaluation** loads automatically when Outcomes tab opens
- **All table containers** have `overflow-x:auto` for horizontal scroll

**Model Evaluation (#33):**

Chronological backtesting of Frequency and Bayesian predictors, matching NBA Monitor:

- **5 targets:** team_wins, game_upset (from odds), game_close, game_blowout, game_comeback
- **Rolling-origin evaluation:** Train on all games before day D, predict day D, advance by `step_days=7`
- **Holdout fallback:** First 80% train / last 20% test when insufficient history
- **3 metrics per predictor:** Brier score, Log Loss, ECE (Expected Calibration Error)
- **Dashboard section:** 9-column table (Target, Eval n, Mode, Freq/Bayes Brier/LogLoss/ECE), Refresh/Save buttons, snapshot history (last 8)
- **Bayesian Brier green highlight** when Bayes < Freq (smoothing helps)
- **Auto-snapshots (#166):** Background thread saves eval snapshots at `model_eval_snapshot_interval` (default 360 min). Config: `model_eval_snapshot_enabled`, `model_eval_snapshot_interval`.
- **CLI:** `python3 evaluate_model.py --target team_wins --step-days 7 --pretty --save-snapshot`
- **Snapshot history:** `model_eval_history_nrl.json` (max 100 entries, compact per-target metrics)

**PW ROI Matrix Dashboard (#29):**

Cross-dimensional heatmap under Analysis tab, matching NBA Monitor layout:

- **7 dimensions (#187):** Confidence Band (50-90%), Consensus Type (Strong/Conflicted/ML only/Hist only), Half (H1/H2/ET), Quarter (Q1-Q4/ET), Spread Role (Favorite/Underdog), Edge Range (≥10/5-10/0-5/<0), Margin (Leading/Trailing)
- **20 pair pivots:** All combinations of 2 dimensions as row/column axes
- **Per-cell metrics (7 lines):** Games Won%, Flat ROI (F), ML ROI, Spread ROI (Spr), BK Spread ROI (BkS), BK Odds ROI (BkO), calls/games count. Font sizes: Won% 0.72rem, ROI 0.65rem, count 0.6rem.
- **Compound axes (#187):** Optional "+" button adds secondary dimension per row/column. E.g. rows = "Confidence Band + Spread Role", columns = "Edge Range". Compound row labels split into separate columns. Uses `~` separator in compound keys (not `+` — conflicts with `10+` edge value).
- **Cell coloring:** Green/red background tint based on Flat ROI magnitude (`alpha = min(|roi|/30, 1) * 0.15`)
- **Pin controls:** Non-selected dimensions become pin dropdowns (default: spread_role=underdog)
- **Season/segment filters:** Independent from Analysis tab filters
- **Drill-down:** Click any cell → navigates to Game History tab with matching PW filters. Standard cells use `matrixDrill()`, compound cells use `matrixDrillCompound()` with `data-drill` JSON attribute.
- **Debounce:** 300ms on pin/filter changes before API fetch

**ROI Matrix backend (`pw_matrix.py`):**
- `compute_pw_matrix(history, pins={}, custom_pivot=None)` — computes all pair maps with per-cell bucket metrics. `custom_pivot={"row_dims": [...], "col_dims": [...]}` for compound axes.
- Decimal odds ROI: `profit = stake * (decimal_odds - 1)`, flat = +1/-1 units
- Spread cover tracking: pregame spread, synthetic live line, BK live line
- Dedicated `/api/pw-matrix` endpoint (not piggybacked on `/api/outcomes` like NBA)

**PW ROI Combos (#30, inside ROI Matrix panel):**

Below the heatmap, matching NBA layout. Iterates all filter combinations:
- **36 combos:** role (2) x margin (2) x call_selection (3) x half (3)
- **14-column table:** #, Role, Margin, Calls, Half, Total, Accuracy, Games Won, Flat ROI, ML ROI, Pre Spr Cov, Pre Spr ROI, Live Spr ROI, BK Spr ROI
- **7 sort options:** Games Won%, Flat ROI, ML ROI, Spread Covered%, Spread ROI, Live Spread ROI, BK Spread ROI
- **Client-side caching** (`_pwCombosCache`) — sort/limit changes re-render instantly without server round-trip
- **Load + Top ROI** buttons with computation timer ("N combos evaluated in Xs")
- **Row click drill-down** to Game History with matching filters
- Inherits season/segment from ROI Matrix controls above

**Adaptive odds polling intervals (monitor.py):**

| Game Phase | Poll Interval |
|------------|--------------|
| H1 (0'–40') | Every 5 minutes |
| H2 early (40'–70') | Every 5 minutes |
| H2 last 10 min (70'+) | Every 2 minutes |
| Extra Time | Every 2 minutes |

### The Odds API

`odds_api.py` fetches live bookmaker odds from The Odds API:
- Sport key: `rugbyleague_nrl`
- Markets: h2h (moneyline), spreads (handicap), totals (over/under)
- Bookmaker priority: TAB > PointsBet AU > Ladbrokes AU > Unibet (Bet365/Sportsbet not available — #204)
- Per-call credit tracking and persistent usage logging
- `backfill_odds.py` for historical odds with per-date caching in `odds_cache/`

### ML Model

`ml_model.py` trains a logistic regression from `game_history.json`:

```bash
python3 ml_model.py --train --evaluate
```

- 36 features: 27 condition binary (including `first_team_scores`) + 9 context (margin, is_home, halftime_margin, h2_momentum, etc.)
- Trained on 1,874 samples from 992 games
- 99.5% cross-validation accuracy
- Artifacts: `model_nrl.pkl` + `model_meta_nrl.json` (git-ignored)
- scikit-learn is optional — ML features degrade gracefully when unavailable
- `predict_winner()` called by `monitor.py` during live PW evaluation (Phase 1 blend)

**Automated ML training (`ml_train_runner.py` + systemd timer, #165):**
```bash
python3 ml_train_runner.py              # scheduled (respects 3 gates)
python3 ml_train_runner.py --force      # manual (skips all gates)
```

- **Gate 1:** `ml_training_enabled` config check
- **Gate 2:** Staleness — skip if model age < `ml_training_interval` days (default 7)
- **Gate 3:** Hash — skip if `game_history.json` SHA-256 unchanged
- PID-based lock prevents concurrent runs
- Systemd timer: daily at 13:00 UTC (`nrl-ml-train.timer`)
- Per-run log capture to `logs/ml_train_*.log`, history in `ml_training_history.json`

### Backfill

```bash
# Phase 1: Build local cache from NRL.com
python3 backfill.py --fetch-cache --season 2024,2025,2026

# Phase 2: Replay from cache → game_history.json
python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026

# Other flags
python3 backfill.py --use-cache --dry-run --season 2024     # preview
python3 backfill.py --use-cache --write-canonical --skip-existing  # no duplicates
python3 backfill.py --use-cache --write-canonical --resume   # continue from checkpoint

# Backfill WITH predicted winner calls (opt-in)
python3 backfill.py --use-cache --write-canonical --season 2024,2025,2026 --pw-calls
python3 backfill.py --use-cache --write-canonical --season 2026 --pw-calls --pw-threshold 65 --pw-force
```

Current backfill: 992 games (2022: 201, 2023: 213, 2024: 213, 2025: 213, 2026: 92, SOO: 60). PW calls: 1,281 across 385+ games. SOO PW: 146 calls at 91.8% across 60 games. HT calls suppressed (minute 40), duplicate calls suppressed (same team+score), ET calls valid (golden point games only). ML disabled during backfill; lookahead-safe prior stats; cumulative condition snapshots (#40). SOO games excluded from ML training (#102).

### Backup & Recovery

Three-layer protection architecture for `game_history.json` and runtime state:

**Layer 1 — Daily Backups:**
- `runtime_backup.py` creates timestamped snapshots in `runtime_backups/<ISO8601>-<label>/`
- Systemd timer runs daily at 16:00 UTC (outside NRL game window 04:00–13:00 UTC)
- 15 runtime files backed up: `game_history.json`, `game_history.wal`, `alerts.json`, `live_stats.json`, `game_ticker.json`, `.alert_state.json`, `config.json`, `odds_api_log.jsonl`, `model_nrl.pkl`, `model_meta_nrl.json`, `backfill_state.json`, `pw_trend_nrl.json`, `ml_training_history.json`, `model_eval_history_nrl.json`, `odds_monitor_history.json`
- `metadata.json` per snapshot (config fingerprint, file sizes, record counts, `pw_call_count`, `pw_enabled`, `pw_version`)

**Layer 2 — Write-Ahead Log (WAL):**
- `game_history.wal` — append-only JSONL, one game record per line
- `monitor.py` writes to WAL **before** `game_history.json` (crash safety)
- WAL auto-truncated after each backup (retains entries for last 5 backups)

**Layer 3 — Corruption Recovery:**
- `outcomes.py` `load_history()` detects corruption (JSONDecodeError)
- Restores from newest valid backup + replays WAL
- Preserves corrupted file as `game_history.json.corrupted`
- Logs `[CRITICAL]` to stderr

**Layer 4 — Round-Flip Recovery (#144):**
- Detects games tracked live but missed at game-end when NRL API switches to next round
- Scans state for `cond_hits_` keys without corresponding `game_end_` key
- Fetches match data directly to confirm completion, injects into completed_games list
- Game-end conditions use accumulated `cond_hits_` state with first-fire dedup (not `all_hits` which is empty at game-end)

**Layer 5 — Game Auto Recovery (#136):**
- Detects games the monitor never saw (offline during entire game or partial miss)
- **In-poll detection:** Each cycle checks previous 1-2 rounds for completed games not in history
- **Scheduled scan:** `recovery.py scan --season 2026` for batch gap detection
- **Recovery pipeline:** `_recover_missed_game()` → fetch API → `backfill._build_game_record()` → evaluate conditions → generate synthetic PW calls (ML disabled, lookahead-safe) → merge preserved live state
- **Config:** `auto_recovery_enabled` (master toggle), `auto_recovery_pw_calls` (PW generation)
- **Dashboard:** "Game Auto Recovery" tab under Monitor Status with toggles and recovery log
- **State TTL cleanup:** 48h for unrecorded game keys, 7d for `game_end_` keys, immediate for recorded games
- Records marked `source: "recovered"`, `replay_mode: "auto_recovered"`
- See `docs/GAME_AUTO_RECOVERY.md` for full architecture

**Snapshot labels and retention:**

| Label | Trigger | Retention |
|-------|---------|-----------|
| `scheduled` | Daily 16:00 UTC systemd timer | Last 10 |
| `pre-config-save` | Before dashboard config save | Last 10 |
| `pre-backfill` | Before backfill writes | Indefinite |
| `manual` | Dashboard button or CLI | No auto-prune |

**CLI (`recovery.py`):**
```bash
python3 recovery.py list                          # List backups
python3 recovery.py restore <name>                # Restore + WAL replay
python3 recovery.py restore <name> --no-wal       # Restore without WAL
python3 recovery.py wal-status                    # Show WAL status
python3 recovery.py wal-truncate --keep 5         # Truncate old WAL entries
python3 recovery.py scan --season 2026            # Scan for missed games (#136)
python3 recovery.py scan --season 2026 --dry-run  # Preview only
```

**Deploy:**
```bash
cp systemd/nrl-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nrl-backup.timer
```

### Persistent State Files (all git-ignored)

| File | Contents |
|------|----------|
| `.alert_state.json` | Cooldown timestamps, fired flags, PW calls per game (`pw_calls_{game_id}`), PW eval timestamps, pregame odds snapshots (`pregame_odds_{game_id}`), kickoff times (`kickoff_utc_{match_id}`), accumulated condition hits (`cond_hits_{match_id}`) |
| `alerts.json` | Recent alert history |
| `live_stats.json` | Last live stats snapshot (includes `pw_analysis`, `pw_calls`, `live_odds` per game) |
| `game_ticker.json` | Last run's active condition hits |
| `game_history.json` | All game records with stats, conditions (with fire-time margins), `score_progression`, PBP features, optional `predicted_winner_calls[]` |
| `game_history.wal` | Write-ahead log — JSONL, one game record per line |
| `model_nrl.pkl` | Trained sklearn model |
| `model_meta_nrl.json` | Model schema/accuracy metadata |
| `backfill_state.json` | Backfill resume checkpoint |
| `nrl_cache/` | Cached NRL API responses per season |
| `odds_api_log.jsonl` | Odds API usage log — JSONL format, append-only (startup/call/fallback records). Migrated from JSON array (#84) |
| `odds_api_fallback_data.jsonl` | Full raw API event data saved on bookmaker fallback (#179). JSONL, auto-pruned by retention config |
| `odds_cache/` | Cached historical odds responses per date |
| `pw_trend_nrl.json` | PW trend snapshots + changelog (computed during backups, used by /api/pw-trend) |
| `model_eval_history_nrl.json` | Model evaluation snapshot history (compact Brier/LogLoss/ECE per target, max 100) |
| `odds_monitor_history.json` | Persisted odds monitor snapshots per game (from configurable minute+ET, 60s intervals, indefinite retention) |
| `ml_training_history.json` | ML training run history (timestamp, trigger, result, samples, duration, log_file) |
| `runtime_backups/` | Timestamped backup snapshots (daily, pre-config, pre-backfill, manual) |

## Configuration

`config.json` key fields:
- `season` — NRL season year (default 2026)
- `monitor_all_teams` / `teams[]` — team filtering
- `slack_bot_token` / `slack_channel` — Slack delivery (preferred)
- `openclaw_gateway` / `openclaw_token` — fallback alert delivery
- `alert_cooldown_minutes` — cooldown for repeated alerts
- `end_of_game_alerts` — send final score alert when game ends
- `outcomes_decay_lambda` — recency weighting for historical analysis
- `odds_api_enabled` / `odds_api_key` — The Odds API integration
- `odds_poll_interval_h1` / `odds_poll_interval_late` / `odds_poll_late_threshold_min` — adaptive odds polling intervals
- `odds_monitor_activate_minute` — game minute to start odds monitoring (default 65, configurable via dashboard)
- `odds_monitor_leading_threshold` — minimum spread price for leading team strategy (default 3.0, #211)
- `odds_monitor_trailing_threshold` — minimum spread price for trailing team strategy (default 3.0, #213)
- `odds_api_fallback_save_enabled` — save full raw API response on bookmaker fallback (default false, #179)
- `odds_api_fallback_retention_days` — auto-delete fallback data older than N days (default 7, #179)
- `pw_alert_filters` — filter-driven PW Slack alerts (#263). When `enabled: true`, old `predicted_winner_threshold` + `pw_underdog_alert` conditions are suppressed. Filters: `quarter`, `spread_role`, `margin_at_fire`, `half`, `min_pct`, `edge_min`/`edge_max`, `polarity`, `consensus`, `source` (live_only). Planned (#278): `strict_bk`, scenario ML/Spread ROI filters, Q ML/Spread ROI filters, ROI logic, min calls/games. `game_summary: true` sends brief game-end alert with score/winner/PW accuracy/BK units. Editable in Config Settings tab + "Save as Alert Filters" button + "Use Saved Alert Filters" link in alert bar (#282) on PW Live sub-tab. **Note:** `send_alert()` returns `bool`, not tuple — two call sites had tuple-unpack bug that silently broke alert history + game summary alerts (#289)
- `predicted_winner_enabled` — enable/disable PW evaluation pipeline
- `predicted_winner_threshold` — global PW confidence threshold (default 65)
- `predicted_winner_alert` — PW alert config with per-role thresholds (favorite/underdog)
- `pw_half_weights` — confidence multipliers per half (H1/H2/ET)
- `pw_margin_gate` / `pw_margin_gate_late` — suppress PW when trailing by too much (-12/-8)
- `pw_edge_gate` — suppress PW calls when avg condition edge < threshold (disabled by default; alert filters `edge_min`/`edge_max` preferred, #296)
- `pw_blend_weights` — ML vs historical blend ratio (default 70/30)
- `pw_eval_interval_h1` / `pw_eval_interval_h2` / `pw_eval_interval_late` — PW eval cadence per phase (default 10/5/2 min)
- `pw_eval_on_score_change` — bypass PW cooldown when score changes between polls (default true)
- `pw_eval_late_threshold_min` — game minute when late-phase eval kicks in (default 60)
- `pw_version` — version string for tracking algorithm changes
- `auto_recovery_enabled` — enable/disable in-poll missed game detection (default true, #136)
- `auto_recovery_pw_calls` — generate synthetic PW calls for recovered games (default true, #136)
- `conditions[]` — base condition definitions (38 in example config including PW thresholds)
- `compound_conditions[]` — AND/OR rules over base conditions

See `config.example.json` for the full schema.

## Key Technical Constraints

- **Core dependency:** `requests` only; `scikit-learn` optional for ML
- **NRL.com API** is the sole data source; undocumented internal API with no SLA
- **No auth on dashboard** — assumes localhost or trusted network (Tailscale recommended)
- **Atomic writes:** All file writes use `os.replace()` after writing to a `.tmp` file; `game_history.json` also uses WAL + fsync for crash safety
- **`conditions.py` is the shared evaluator** — condition logic lives there, not in `monitor.py`
- **`timeline_features.py` is the PBP extractor** — used by both `backfill.py` and future live monitor enrichment
- **Dashboard port 8898** — NBA Monitor uses 8899, NRL uses 8898 to avoid conflict
