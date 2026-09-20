# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

NBA Monitor is a polling-based NBA/WNBA game intelligence service that evaluates live game conditions every 30 seconds during game windows (NBA: UTC 16:00–08:00, WNBA: UTC 17:00–06:00), sending alerts to Slack and serving a dashboard for live visibility, historical analysis, and predicted winner modelling.

NBA and WNBA share this codebase via `league_config.py`. Sibling project: NRL Monitor (`~/.openclaw/workspace/scripts/nrl-monitor/`).

**GitHub:** https://github.com/bobcheong/NBAMonitor (NBA/WNBA), https://github.com/bobcheong/NRLMonitor (NRL)

**Cross-repo workflow:** When a fix or feature applies to both NBA/WNBA and NRL repos, create/update separate GitHub issues in each repo (with cross-references), update `CHANGES.md` in both repos, and use each repo's own issue number in code comments.

**Code parity:** Maintain consistent patterns between NBA and NRL dashboards where possible. When adding shared features (tabs, filters, JS helpers, CSS classes), use the same variable names, function names, element IDs, localStorage keys, and fetch URL conventions. Before copying code between repos, check for repo-specific differences (e.g. NBA uses bare `'/api/...'` fetch paths, NRL uses `const API = ''` prefix; NBA uses `--accent` (orange), NRL uses `--nrl-green`). See #339 / NRL #178.

## Running the Project

```bash
# Create .venv with dependencies
python3 -m venv .venv
.venv/bin/pip install requests scikit-learn

# Run one monitor cycle
.venv/bin/python3 monitor.py

# Start the dashboard server (port 8899)
.venv/bin/python3 server.py 8899
# → http://127.0.0.1:8899/dashboard.html

# Train the ML model (requires scikit-learn)
.venv/bin/python3 ml_model.py --train --evaluate
# Or via dashboard: Monitor Status → ML Training → Train Now

# Send pre-game digest with 1H moneyline records
.venv/bin/python3 digest.py

# Backfill historical data (2-phase)
.venv/bin/python3 backfill.py --fetch-cache --start-date 2024-10-01 --end-date 2025-06-30
.venv/bin/python3 backfill.py --use-cache --write-canonical --start-date 2024-10-01 --end-date 2025-06-30
```

**Automated scheduling (production — Linux with systemd):**
```bash
cp systemd/nba-monitor.{service,timer} ~/.config/systemd/user/
cp systemd/nba-dashboard.service ~/.config/systemd/user/
cp systemd/nba-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nba-monitor.timer nba-dashboard.service nba-backup.timer
```

**WNBA (separate config + services):**
```bash
cp systemd/wnba-monitor.{service,timer} ~/.config/systemd/user/
cp systemd/wnba-dashboard.service ~/.config/systemd/user/
systemctl --user enable --now wnba-monitor.timer wnba-dashboard.service
```

**ChromeOS/Crostini (no systemd — uses supervisord):**
```bash
sudo apt install supervisor
sudo cp supervisord.conf /etc/supervisor/conf.d/openclaw-monitors.conf
sudo supervisorctl reread && sudo supervisorctl update
# monitor_loop.py replaces systemd timer with a 30s polling loop
# supervisord.conf manages all 6 processes (NBA/WNBA/NRL monitors + dashboards)
```

**View logs:**
```bash
tail -f logs/monitor_nba.log
tail -f logs/dashboard_nba.log
```

**Testing without live games:**
```bash
# Full simulation mode — generates synthetic conditions, predictions, and alerts
http://127.0.0.1:8899/?simulate_live=1&simulate_game_id=401810887
```

## Architecture

### League Sharing (NBA/WNBA)

Single codebase shared via `league_config.py`:
- Config: `config.json` with `"league": "nba"` or env var `NBA_MONITOR_CONFIG=config-wnba.json`
- State files league-suffixed: `game_history_nba.json` / `game_history_wnba.json`
- Log paths: `logs/monitor_nba.log` / `logs/monitor_wnba.log`
- Cache dirs: `espn_cache/nba/` / `espn_cache/wnba/`
- Season formats: NBA cross-year (2025-26), WNBA single-year (2026)
- Period lengths: NBA Q1-Q4 12min, WNBA Q1-Q4 10min, both OT 5min

### Core Files

| File | Role |
|------|------|
| `monitor.py` | Main polling job — fetches ESPN APIs, evaluates conditions, runs PW predictions, sends Slack alerts, persists state |
| `server.py` | Dashboard HTTP backend — serves JSON APIs and static files (port 8899 NBA, 8900 WNBA) |
| `league_config.py` | NBA/WNBA config resolver — league detection, suffixed state/log/cache paths, season date helpers |
| `conditions.py` | 12 condition evaluator functions — shared by `monitor.py` and `backfill.py` |
| `outcomes.py` | Historical analysis engine — outcome statistics, Bayesian blending, `ConditionEdgeStore`, polarity classification, PW version tracking, WAL helpers, backup/recovery |
| `ml_model.py` | Logistic regression predicted winner model — trains on condition features + game context (39 features) |
| `evaluate_model.py` | Chronological model evaluation — Brier/LogLoss/ECE for 5 targets, rolling-origin + holdout, snapshot history |
| `odds_api.py` | The Odds API client — live/historical odds, spreads, moneylines; dynamic quarter/half markets (`QUARTER_MARKETS`) for PW fire (#374); `sport_key` override for Summer League (#384) |
| `summer_league_odds.py` | Summer League live odds capture — `SummerLeagueOddsMonitor` background thread, ESPN discovery (`nba-summer-las-vegas`), The Odds API polling (`basketball_nba_summer_league`), atomic persistence (#384) |
| `backfill.py` | 2-phase historical backfill — fetch-cache + replay-write for game_history |
| `backfill_pw_calls.py` | PW call generation for historical games with lookahead-safe priors |
| `backfill_odds.py` | Historical odds backfill from The Odds API with per-date caching |
| `pw_trend.py` | PW call performance tracking — snapshot computation, changelog, version resolution, bootstrap CLI |
| `evaluate_pw_improvements.py` | PW filter evaluation and ROI analysis |
| `compare_edge_gate.py` | Edge gate threshold evaluation |
| `espn_cache_io.py` | ESPN cache I/O helpers — .json/.json.gz transparent reads |
| `h1_store.py` | H1 moneyline edge caching — per-team home/away results |
| `evanalytics.py` | EVAnalytics integration for H1 player prop analysis |
| `digest.py` | Pre-game daily digest — tonight's games + 1H ML records for Slack |
| `migrate_enrich_stats.py` | Enriches existing game records with box score stats from ESPN cache |
| `runtime_backup.py` | Runtime file snapshot creation, restore, pruning, orphan `.tmp` cleanup, PW trend snapshot |
| `cloud_backup.sh` | rclone sync of NBA/WNBA + NRL to Google Drive — runtime files, config, cache. Twice daily via systemd timer (#394) |
| `recovery.py` | CLI for backup listing, restore with WAL replay, WAL status/truncate |
| `pw_roi_report.py` | Cross-league PW ROI report generator — BK ROI heatmaps with Edge Range filter, 5-way scenarios (#300, NRL #188) |
| `synthetic_odds.py` | Synthetic live odds generator — spread (linear), moneyline (logistic+pregame), H1 ML (lookup). Called by monitor.py every poll cycle (#376) |
| `analyse_bk_odds.py` | BK odds analysis script — 10-section HTML report comparing game state vs BK odds, synthetic validation (#376) |
| `export_pw_jsonl.py` | Bootstrap `pw_export_{league}.jsonl` from game_history — one PW call per JSONL line with game context. Overwrites output. Params: --source, --since, --until, --include-suppressed (#457) |
| `backfill_synth_odds.py` | Backfills `synth_moneyline`, `synth_spread`, `synth_h1_ml` onto historical PW calls (#379) |
| `dashboard.html` | Single-page dashboard — Control Panel with nested tabs (shared layout with NRL) |
| `docs/polymarket-trading-bot.html` | Polymarket trading bot Rev 1 requirements — architecture, edge evaluation, risk management, 5 phases (#214) |
| `docs/polymarket-trading-bot-review.html` | Polymarket trading bot Rev 2.3 technical review — 38 findings, corrected math, CLV KPI, Betfair alternative (#214) |
| `monitor_loop.py` | Polling loop wrapper — replaces systemd timer on ChromeOS/Crostini, runs monitor.py every 30s (#455) |
| `supervisord.conf` | Process manager config for ChromeOS/Crostini — all 6 NBA/WNBA/NRL processes (#455) |
| `config.json` | Runtime config (excluded from git — use `config.example.json` as template) |

### Data Flow

```
Systemd timer (every 30s, NBA: UTC 16:00–08:00, WNBA: UTC 17:00–06:00)
  → Covers Summer League tip-offs (20:00 UTC = 4pm ET) through late West Coast games
  → monitor.py
      → ESPN Scoreboard API + Summary API (per-game stats + PBP)
      → Summer League: merges nba-summer-las-vegas scoreboard events (#387)
      → Pass 1: Evaluate 12 base condition types per monitored team
      → Evaluate compound conditions (AND/OR with optional base suppression)
      → Apply per-condition cooldowns
      → Adaptive odds polling
      → Pass 2: PW evaluation (ML + historical blend → PW threshold conditions)
      → Record PW calls in state + live_stats.json (pregame odds from snapshot, BK fallback #389)
      → save_state() immediately after each PW call append (#399) — dashboard sees new calls within 15s
      → Compute synthetic odds (synthetic_odds.compute_all) → live_stats.json + PW call records
      → Send Slack alerts (base + PW threshold alerts) — immediate, no delay after PW fires
      → Write: alerts.json, live_stats.json, game_ticker.json, .alert_state.json
      → Append to game_history.wal (WAL) then game_history.json (at game end)
      → Game-end: enriched record (source:live, box score stats, PW calls, conditions, tags, odds)

server.py (long-running, port 8899/8900)
  → Serves dashboard.html + /api/* endpoints
  → /api/scoreboard proxies ESPN + merges Summer League events in SL window (#390)
  → /api/upcoming merges SL scheduled games (#390)
  → POST /api/config validates and atomically writes config.json
  → Pre-config-save snapshot before config writes
  → Backup/recovery API endpoints
  → PW API endpoints: /api/pw-calls, /api/pw-calls-live, /api/pw-trend, /api/pw-roi-combos, /api/pw-matrix, /api/pw-scenarios

backfill.py (batch, run manually)
  → Phase 1: fetch ESPN data → espn_cache/<league>/
  → Phase 2: replay from cache → game_history.json with full stats
  → Optional: --pw-calls generates predicted_winner_calls[] per game record

ml_model.py (batch, run manually or via systemd timer)
  → Trains on game_history.json → model.pkl + model_meta.json

Systemd timer (daily at 09:00/09:05 UTC, outside game window)
  → runtime_backup.py snapshot --label scheduled --keep-last 10
      → Copies 14+ runtime files to runtime_backups/<timestamp>-scheduled/
      → Truncates WAL (keeps entries for last 5 backups)
      → Appends PW trend snapshot via pw_trend.append_snapshot()
```

### Dashboard Structure

```
🧭 Control Panel
├── Monitor Status
│   ├── Overview (polling, ESPN API, round, live games, ML model status, scheduler, last backup, disk, Odds API credits)
│   ├── Logs (card entries with tag pills, filter chips, raw mode with file/tail/search/live tail)
│   ├── Odds API (#338: inner sub-tabs)
│   │   ├── Usage (charts, time range filter, interval selector, credit stats, BK coverage, fallback events, request logging toggle #355)
│   │   ├── API Test (#337, #338: league dropdown, market checkboxes, event fetcher, request/response viewer)
│   │   └── Summer League (#384: enable/disable toggle, start/stop, poll interval 30/60/120/300s, auto-refresh 10s, live/completed/scheduled game cards with odds, clickable drilldown with snapshot table)
│   ├── ML Training (Train Now, staleness detection, history table, log viewer, auto-training status)
│   └── Recovery (WAL status, backup list with restore/delete, retention prune)
├── 🏀 Games
│   ├── 🔴 Live Games (game cards: score+1H, PW with consensus/polarity/edge badges, pregame/live odds bar, synthetic odds row, collapsible stat grid, accumulated condition pills, scoring timeline, lead summary; live conditions ticker at bottom)
│   ├── 🎯 Predicted Winners (#401: Live/History sub-tabs)
│   │   ├── 🔴 Live (full filters+stats+Q/H+Best Combos from all history (#407), persistent alert filter bar with colored pills (#426), "Save as Alert Filters" button, "Use Saved Alert Filters" link in alert bar (#438), "🔴 Live Games" section with heading+timestamp below stats (#408), live/recent game cards only, "No live games currently" empty state, 15s auto-refresh via /api/pw-calls-live, initial load+filter change via /api/pw-calls, independent filter state with pw-live-* element IDs)
│   │   └── 📚 History (full historical view: collapsible game cards with season/segment labels + per-game BK ML/Spread units (#377, #393), per-quarter call tables with Src column (LIVE/PRE/SYNTH per call #412), accuracy stats block with BK ML/Spread units + synth Q/H fallback (#380) + price source breakdown (#412), Q/H Performance with BK ML/Spread stats (#377), scenario badges with units (#377), Best Filter Combos report with dimension sweep (#431), lock checkboxes + All/None toggle (#432), Apply button (resets non-exempt filters #439) + Clear link + Restore previous filters (#439, #413), 6-dim sweep incl. Consensus (#440), lock persistence (#441), help modal (#377, #392, #431), "Top 3 by Spread (Pregame)" table (#431), Live Only filter replacing Strict BK (#412), pagination, filter persistence, aggregate synth Q/H1 scenario badges with toggle (#382, #383))
│   ├── 🔔 Alert History (alerts with team icons)
│   ├── ⏰ Upcoming Games (tip-off times, odds, venues, countdowns)
│   └── 📋 Post Game Summary (recently completed)
├── Configuration
│   ├── General Settings (season, teams, cooldown, alerts)
│   ├── Conditions (editable: add/edit/delete, 12 types)
│   ├── Compounds (editable: AND/OR, checkbox refs)
│   ├── Settings (Teams, Alerting, PW Alert Filters (#426), Model Eval, ML Training, Upcoming, Scheduler, Log Rotation, Backups)
│   ├── Post-Game (summary windows, dynamic tag manager)
│   └── PW Model (alerts, thresholds, quarter weights, gates, eval cadence, ML blend, Bayesian priors, Condition Edge Weights)
├── 📊 Analysis
│   ├── Outcomes (ML Status, Condition table, Compound Outcomes, Condition Combos, Tags, PW Accuracy)
│   ├── 📈 PW Trends (Chart.js: 4 chart modes, filters, changelog table, version cohorts, edge range breakdown)
│   ├── 📊 ROI Matrix (7-dim heatmap + compound axes + ROI Combos)
│   └── 🔬 BK Odds (iframe: analyse_bk_odds report — 10 sections, Regenerate button)
├── 📚 Game History
│   ├── Filter bar (team, condition, tag, season, source, dates, search + PW filters — all persisted to localStorage)
│   ├── Game list table (scores, 1H, season, segment, tags, conditions count, PW calls/accuracy — #393)
│   └── Game Detail (quarter scores, box score comparison, conditions, PW calls table with Synth column)
└── 🔍 Custom Queries (#326)
    ├── Winning Margin Distribution (Under/Over at 0.5-point lines, league-aware ranges)
    ├── 1H Totals Over/Under (league-aware lines: NBA 90.5–140.5, WNBA 60.5–100.5)
    ├── 1H Winner → Wins Game? (halftime leader win rate)
    ├── Q3 Winner → Wins Game? (per-team breakdown)
    └── Leading After Quarter → Wins? (Q1/Q2/Q3 cumulative lead persistence)
```

### API Surface

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/config` | GET/POST | Read/write config with validation |
| `/api/alerts` | GET | Recent alert history |
| `/api/live-stats` | GET | Per-game live stats snapshot |
| `/api/live-analysis` | GET | Current-tick Bayesian analysis output |
| `/api/ticker` | GET | Active condition hits this run |
| `/api/upcoming` | GET | Upcoming round fixtures with tip-off times/odds |
| `/api/scoreboard` | GET | Current games from ESPN |
| `/api/history` | GET | Filtered, paginated game history |
| `/api/history-game` | GET | Full game detail with stats/conditions |
| `/api/history-facets` | GET | Facet counts for filter dropdowns |
| `/api/history-seasons` | GET | Season list with counts |
| `/api/monitor-status` | GET | Polling status, config summary, disk space, Odds API credits |
| `/api/monitor-timer` | GET/POST | Systemd timer schedule metadata and updates |
| `/api/monitor-timer-auto` | GET/POST | Auto-scheduler state |
| `/api/monitor-log-events` | GET | Structured log events with tag classification |
| `/api/log-outputs` | GET | Detected log paths |
| `/api/log-tail` | GET | Raw log file tail content |
| `/api/condition-edges` | GET | Per-condition fire count, win count, edge |
| `/api/ml-meta` | GET | Model schema/accuracy metadata |
| `/api/ml-training-status` | GET | Model meta, staleness detection, training state, history |
| `/api/ml-train` | GET/POST | Trigger background ML training |
| `/api/postgame` | GET | Recently completed games |
| `/api/outcomes` | GET | Historical outcome rates with decay weighting |
| `/api/analysis` | GET | Enhanced condition outcomes, compound outcomes, combos, tags, PW accuracy |
| `/api/model-eval` | GET | Model evaluation metrics (Brier/LogLoss/ECE) for 5 targets |
| `/api/model-eval-retrain` | GET/POST | Trigger retrain |
| `/api/model-eval-history` | GET | Evaluation snapshot history |
| `/api/snapshots` | GET | List runtime backup snapshots |
| `/api/snapshots/create` | POST | Create manual snapshot |
| `/api/recovery/backups` | GET | List game_history backups + WAL status |
| `/api/recovery/restore` | POST | Restore from backup with optional WAL replay |
| `/api/recovery/delete-backup` | POST | Delete a single backup |
| `/api/recovery/prune-backups` | POST | Delete backups older than N days |
| `/api/pw-calls` | GET | Games-grouped PW calls with accuracy stats + ROI metrics, live merge, enrichment, scenario_key + scenario_stats per call (#352). Uses `_load_history_cached()` (#401). Filters: season, quarter, spread_role, margin_at_fire, call_selection, pw_source, pw_edge, pw_polarity |
| `/api/pw-calls-live` | GET | Lightweight live-only PW calls from `.alert_state.json` — no game_history parse, no filters (#401). Used by PW Live sub-tab for 15s fast polling |
| `/api/pw-trend` | GET | PW trend data (changelog + snapshots + `cumulative_snapshot`). Params: season, date_from, date_to, last_n_games, interval, cumulative, source |
| `/api/pw-roi-combos` | GET | PW ROI filter combos — 36 combos with full ROI metrics |
| `/api/pw-matrix` | GET | PW ROI matrix — cross-dimensional pair pivots with per-cell metrics |
| `/api/pw-scenarios` | GET | PW scenario stats lookup — per-scenario accuracy + BK ROI. Params: call_selection, season_segment, season_year |
| `/api/pw-export` | GET | Flat PW call feed for external consumers (trading bot). Each row = one PW call with game context merged. Includes live in-progress calls from `.alert_state.json`. Params: since, until, source (live/backfill/all), include_suppressed (0/1) |
| `/api/odds-api-log` | GET | Odds API usage log entries |
| `/api/odds-api-stats` | GET | Call results by context, BK coverage, fallback events |
| `/api/odds-api-test-events` | GET | Fetch events for a sport key (free). Params: sport |
| `/api/odds-api-test` | GET | Fetch odds for specific event. Params: sport, event_id, markets |
| `/api/odds-api-fallback-data` | GET | Saved fallback request/response data (#341). Params: ts (optional, returns specific entry or all) |
| `/api/odds-api-request-log` | GET | Request log entries without response bodies (#355). Params: last, context |
| `/api/odds-api-request-log-data` | GET | Full request log entry with response body (#355). Params: ts |
| `/api/custom-queries` | GET | Custom query results (margin dist, 1H totals, quarter leads). Params: season_segment, season_year |
| `/api/generate-roi-report` | GET | Generate cross-league PW ROI report |
| `/api/generate-bk-odds-report` | POST | Regenerate BK odds analysis HTML report (#376) |
| `/api/summer-league-odds` | GET | Summer League odds monitor status + active games (#384) |
| `/api/summer-league-odds/history` | GET | Persisted Summer League odds snapshots. Params: game_id (#384) |
| `/api/summer-league-odds/start` | POST | Start Summer League odds background thread (#384) |
| `/api/summer-league-odds/stop` | POST | Stop Summer League odds background thread (#384) |

### NBA/WNBA-Specific Design

- **Game structure:** 4 x 12-minute quarters (NBA) or 4 x 10-minute quarters (WNBA) + optional overtime (5 min)
- **Data sources:** ESPN APIs (Scoreboard + Summary/PBP) + The Odds API
- **Game windows:** NBA UTC 16:00–08:00, WNBA UTC 17:00–04:00
- **Teams:** 30 NBA teams + 12 WNBA teams
- **Season format:** NBA cross-year (2025-26), WNBA single-year (2026). `_game_season_year()` computes at serve-time — not stored on records.
- **Dashboard ports:** NBA 8899, WNBA 8900, NRL 8901. Tailscale HTTPS proxy: NBA 8444, WNBA 8445, NRL 8446
- **ESPN cache:** Compressed `.json.gz` support via `espn_cache_io.py`. `resolve_cache_path()` + `load_json_file()` read transparently.

### Condition Types (12 total)

| Type | What it checks |
|------|---------------|
| `score_diff` | Margin at Q1/Q2/Q3/Q4 or game total; `use_quarter_score` for quarter-only |
| `consecutive_points_run` | Consecutive unanswered points (from ESPN PBP) |
| `fg_percent_improve` | FG% improvement from previous poll (state-based) |
| `fg_pct_threshold` | FG% above/below threshold (game, quarter, or half scope) |
| `turnover_rate` | TOV% > threshold (turnovers ÷ possessions) |
| `turnovers` | Turnover count > threshold |
| `points_off_turnovers` | Points off turnovers > threshold |
| `back_to_back` | Playing on back-to-back nights (pre-game only) |
| `underdog_at_home` | Home underdog leading halftime by min_lead (Q3+) |
| `h1_moneyline_edge` | H1 moneyline edge from h1_records store (pre-game) |
| `predicted_winner_threshold` | Blended PW confidence ≥ threshold (fires on PW evaluation) |
| `pw_underdog_alert` | Underdog PW fires in configurable quarters (alert-only) |

**Condition name/config scope validation (#347, NRL #185):** Condition names referencing a specific half (e.g. "1H", "2H") must have matching `scope`/`period` config. 4 FG% conditions ("FG% >50% 1H", "FG% <40% 1H", "FG% >50% 2H", "FG% <40% 2H") had `scope: "quarter"` with `quarter: 2/4` — only checked Q2/Q4 instead of the entire half. Fixed to `scope: "half"` with `half: 1/2`.

### Predicted Winner (PW) System

Two-pass live evaluation matching NRL Monitor's architecture:

**Pass 1:** Evaluate all base conditions → collect active hits per game
**Pass 2:** Compute PW analysis → evaluate `predicted_winner_threshold` conditions

**Prediction pipeline:**
```
Active conditions for game
  → Frequency lookup: historical win% for each condition combo
  → Bayesian posterior: Beta priors
  → Sample-adaptive blending
  → ML predict_winner() from ml_model.py (logistic regression, 39 features)
  → Consensus: strong/conflicted/ml_only/historical_only
  → Blend ML + Bayesian (configurable pw_blend_weights)
  → Apply quarter weights: Q1 ×0.85, Q2 ×0.90, Q3 ×1.0, Q4 ×1.10, OT ×1.10
  → Apply margin boost if predicted team is leading
  → Suppression gates: margin (-15 general / -10 late)
  → If blended pct ≥ threshold → record PW call + fire threshold conditions
```

**PW call record fields:** `ts`, `predicted_team`, `predicted_team_id`, `pct`, `consensus`, `blended`, `basis`, `quarter`, `score_margin_at_fire`, `home_score_at_fire`, `away_score_at_fire`, `active_conditions[]` (with `condition_edge`), `spread_role`, `moneyline`, `spread`, `live_moneyline`, `live_spread`, `bk_moneyline`, `bk_spread`, `bk_name`, `polarity_pos`, `polarity_neg`, `polarity_net`, `avg_edge`, `correct`, `synthetic`, `source` (live/backfill), `pw_version`, `bk_q_ml`, `bk_q_spread`, `bk_q_spread_price`, `bk_q_ml_source`, `bk_h1_ml`, `bk_h1_spread`, `bk_h1_spread_price`, `bk_h1_ml_source`, `q_correct`, `q_covered`, `h1_correct`, `h1_covered`, `synth_moneyline`, `synth_spread`, `synth_h1_ml`

**PW Dashboard features:**
- **Live/History sub-tabs (#401, #407):** Both sub-tabs have full filters, stats block, Q/H Performance, Best Filter Combos. All PW functions parameterized with `prefix` ('pw' or 'pw-live') — `loadPWPanel(force, prefix)`, `_getPwFilters(prefix)`, `_renderPwPage({prefix, accuracyHtml, ...})`. Independent filter state per tab (separate localStorage keys). Live tab: stats from all history, game cards filtered to live/recent only; initial load + filter change via `/api/pw-calls`, 15s auto-refresh via `/api/pw-calls-live`.
- Accuracy stats block with ROI metrics, polarity pills, edge range pills. **Computed client-side (#348)** from filtered `/api/pw-calls` data — all filters (edge, polarity, consensus, source) reflected in stats
- Quarter/Half PW Performance block (#374): grouped Q1-Q4/OT/H1 rows with Won (accuracy), ML Won, Covered, Units — computed client-side. Won accuracy shows for all calls (including historical backfill); ML/Covered/Units require BK quarter odds (forward-looking). BK Spr uses same logic as top stats (skip pushes, skip missing spread price — #416)
- Collapsible game cards with per-quarter call tables
- Filters: Season, Quarter, Role, Margin, Half, Call Selection, Source, Edge Range, Polarity, Scenario ROI+, Scenario Accuracy≥, Q ML ROI≥, Q Spr ROI≥
- Quarter scenario badges (#374): Q Won accuracy (always shown), Q ML ROI + Q Spr cover (when BK quarter odds available). Colors: amber `#f59e0b`, emerald `#10b981`. H1 badges for Q1/Q2 keys
- PW Trends: Chart.js charts, changelog table (with "All prior" + "All" rows), version cohorts (with "All Versions" row), edge range breakdown table
- ROI Matrix (#349): 7 dimensions (Confidence Band, Consensus, Quarter, Spread Role, Half, Edge Range, Margin), 21 pair pivots, optional compound axes via "+" toggle, per-cell: Won%/F/ML/Spr/BkS/BkO/count. `custom_pivot` param for compound axes, `~` separator in compound keys. + ROI Combos (36 filter combinations)
- Analysis Outcomes: PW Accuracy by-threshold and by-consensus tables (14 columns including BK Odds ROI, Cov!Won%, Won!Cov%)

**PW changelog entries:**

| Date | Tag | Description | Issues |
|------|-----|-------------|--------|
| 2026-04-09 | v1-pw-launch | Predicted Winner feature launched | #72 |
| 2026-04-13 | v2-bayes-blend | Bayesian blending + ML consensus | #105 |
| 2026-05-04 | v3-polarity | Polarity gate added | #197 |
| 2026-05-09 | v4-multi-league | Multi-league support (NBA + WNBA) | #151 |
| 2026-05-13 | v5-edge-gate | Condition edge gate enabled | #217, #219, #221 |
| 2026-05-17 | v6-edge-cleanup | Edge store filtered: end-of-game artifacts removed | #258 |

### ML Model

`ml_model.py` trains logistic regression from `game_history.json`:

```bash
python3 ml_model.py --train --evaluate
```

- 39 features: condition binary + context (margin, is_home, quarter, etc.)
- Artifacts: `model_nba.pkl` + `model_meta_nba.json` (git-ignored, league-suffixed)
- scikit-learn is optional — ML features degrade gracefully when unavailable
- Auto-training via systemd timer (daily at 09:15/09:20 UTC)

### Backfill

```bash
# Phase 1: Fetch and cache ESPN data
python3 backfill.py --fetch-cache --start-date 2024-10-01 --end-date 2025-06-30

# Phase 2: Replay from cache → game_history
python3 backfill.py --use-cache --write-canonical --start-date 2024-10-01 --end-date 2025-06-30

# Optional: Backfill PW calls (opt-in)
python3 backfill_pw_calls.py --since 2024-10-01 --until 2025-06-30
```

Current backfill: NBA 5,285 games (2022-23 through 2025-26), WNBA 892 games (2023-2025). ESPN cache compressed to `.json.gz` for disk savings.

### Backup & Recovery

Four-layer protection matching NRL Monitor:

1. **Cloud backup** (`gdrive:openclaw-backups/`) — rclone sync to Google Drive. NBA/WNBA at 09:30 UTC (`cloud-backup.timer`), NRL at 02:30 UTC (`nrl-cloud-backup.timer`). Separate scripts per repo. See `docs/disaster-recovery.md` for full rebuild runbook.
2. **Daily local backups** (`runtime_backups/<timestamp>/`) — 14+ runtime files, automatic via systemd timer
3. **Write-Ahead Log** (`game_history.wal`) — append-only JSONL, written before game_history.json
4. **Auto-recovery** — on JSONDecodeError, restores from newest valid backup + replays WAL

**Cloud backup:**
```bash
./cloud_backup.sh                                 # Sync to Google Drive
./cloud_backup.sh --dry-run                       # Preview what would sync
rclone copy gdrive:openclaw-backups/nba-monitor/ . --progress  # Restore
```

**Local recovery CLI (`recovery.py`):**
```bash
python3 recovery.py list                          # List backups
python3 recovery.py restore <name>                # Restore + WAL replay
python3 recovery.py restore <name> --no-wal       # Restore without WAL
python3 recovery.py wal-status                    # Show WAL status
```

### Persistent State Files (all git-ignored, league-suffixed)

| File | Contents |
|------|----------|
| `.alert_state_{league}.json` | Cooldown timestamps, fired flags, PW calls per game, pregame odds snapshots |
| `alerts_{league}.json` | Recent alert history |
| `live_stats_{league}.json` | Last live stats snapshot |
| `game_ticker_{league}.json` | Last run's active condition hits |
| `game_history_{league}.json` | All game records with stats, conditions, PW calls |
| `game_history.wal` | Write-ahead log — JSONL |
| `bayes_state_{league}.json` | Historical win% per condition combo (frequency store) |
| `edge_state_{league}.json` | Per-condition team-win rate tracking |
| `h1_records_{league}.json` | H1 moneyline edge store |
| `live_analysis_{league}.json` | PW analysis output |
| `model_{league}.pkl` | Trained sklearn model |
| `model_meta_{league}.json` | Model schema/accuracy metadata |
| `model_eval_history_{league}.json` | Model evaluation snapshot history |
| `ml_training_history_{league}.json` | ML training run history |
| `pw_trend_{league}.json` | PW trend snapshots + changelog |
| `odds_api_log_{league}.json` | Odds API usage log |
| `odds_api_fallback_data_{league}.jsonl` | Full raw API event data saved on bookmaker fallback (#341). JSONL, auto-pruned by retention config |
| `odds_api_request_log_{league}.jsonl` | Full request/response data for every Odds API call (#355). JSONL, auto-pruned by retention config (default 1 day) |
| `summer_league_odds_{league}.json` | Summer League odds snapshots — per-game arrays of timestamped ML/spread captures (#384) |
| `pw_export_{league}.jsonl` | Flat PW call export — one JSONL line per non-suppressed call with game context. Appended at game-end by monitor.py, bootstrapped by `export_pw_jsonl.py` (#457) |
| `runtime_backups/` | Timestamped backup snapshots |

## Configuration

`config.json` key fields:
- `league` — "nba" or "wnba"
- `monitor_all_teams` / `teams[]` — team filtering
- `slack_bot_token` / `slack_channel` — Slack delivery
- `openclaw_gateway` / `openclaw_token` — fallback alert delivery
- `alert_cooldown_minutes` — cooldown for repeated alerts
- `outcomes_decay_lambda` — recency weighting for historical analysis
- `contextual_margin_band` / `contextual_quarter_band` / `contextual_boost_max` — contextual weighting (#73)
- `odds_api_enabled` / `odds_api_key` — The Odds API integration
- `odds_api_fallback_save_enabled` — save full raw API response on bookmaker fallback (default false, #341)
- `odds_api_fallback_retention_days` — auto-delete fallback data older than N days (default 7, #341)
- `odds_api_request_log_enabled` — log full request URL, params, and response body for every Odds API call (default false, #355)
- `odds_api_request_log_retention_days` — auto-delete request log entries older than N days (default 1, #355)
- `summer_league_odds_enabled` — enable Summer League odds capture background thread (default false, #384)
- `summer_league_odds_poll_interval` — seconds between odds polls per live Summer League game (default 120, #384)
- `predicted_winner_enabled` — enable/disable PW evaluation pipeline
- `pw_alert_filters` — filter-driven PW Slack alerts (#418). When `enabled: true`, old `predicted_winner_threshold` + `pw_underdog_alert` conditions are suppressed. Filters: `quarter`, `spread_role`, `margin_at_fire`, `half`, `min_pct`, `edge_min`/`edge_max`, `polarity`, `consensus`, `source` (live_only). Planned (#435): `strict_bk`, scenario ML/Spread ROI filters, Q ML/Spread ROI filters, ROI logic, min calls/games. `game_summary: true` sends brief game-end alert with score/winner/PW accuracy/BK units. Editable in Config Settings tab + "Save as Alert Filters" button + "Use Saved Alert Filters" link in alert bar (#438) on PW Live sub-tab
- `pw_quarter_weights` — confidence multipliers per quarter (Q1:0.85, Q2:0.90, Q3:1.0, Q4:1.10, OT:1.10)
- `pw_margin_gate` / `pw_margin_gate_late` — suppress PW when trailing by too much (-15/-10)
- `pw_edge_gate` — suppress PW calls when avg condition edge < threshold (disabled by default; alert filters `edge_min`/`edge_max` preferred, #453)
- `pw_blend_weights` — ML vs historical blend ratio
- `ml_training_enabled` / `ml_training_interval_days` — auto-training config
- `postgame_outcome_criteria` — close_margin, comeback_deficit, tags
- `conditions[]` — base condition definitions (12 types)
- `compound_conditions[]` — AND/OR rules over base conditions

See `config.example.json` for the full schema.

## Running Tests

```bash
python3 test_phase3.py              # dashboard UI structure checks
python3 test_phase3_integration.py  # server/API integration tests
python3 test_phase4.py              # phase 4 feature tests
python3 test_issue7_bugs.py         # regression tests for issue #7 bugs
python3 test_issue87_cache_io.py    # cache-IO helpers
python3 test_issue117_fg_improve.py # PBP FG% tick simulation
python3 test_issue126_scoreboard_cache.py  # scoreboard caching
python3 test_issue73_phase1.py      # contextual weighting
```

Tests are standalone scripts (no test runner required); they print PASS/FAIL summaries.

## Key Technical Constraints

- **Core dependency:** `requests` only; `scikit-learn` optional for ML
- **ESPN API** is the sole data source; undocumented, graceful degradation on failures
- **No auth on dashboard** — assumes localhost or trusted network (Tailscale recommended)
- **League switcher** — header bar links between NBA/WNBA/NRL dashboards. Auto-detects direct vs Tailscale HTTPS port set. Defined in `_buildLeagueSwitcher()` in each `dashboard.html`.
- **Atomic writes:** All file writes use `os.replace()` after writing to a `.tmp` file; `game_history.json` also uses WAL + fsync for crash safety
- **`conditions.py` is the shared evaluator** — condition logic lives there, not in `monitor.py`
- **Dashboard ports:** NBA 8899, WNBA 8900, NRL 8901. Tailscale HTTPS proxy: NBA 8444, WNBA 8445, NRL 8446
- **Do not routinely re-run `backfill_pw_calls.py`** — introduces lookahead bias and destroys real-time call provenance. Only re-backfill selectively for a specific targeted reason (#247).
- **PW Logic Changelog is manual** — when making PW behavior changes, add a new entry to `_default_changelog()` in `pw_trend.py` with date, version tag, description, and issue references.

## Behavioral Guidelines

### 1. Think Before Coding
Don't assume. Surface tradeoffs. If uncertain, ask.

### 2. Simplicity First
Minimum code that solves the problem. No speculative features, abstractions for single-use code, or error handling for impossible scenarios.

### 3. Surgical Changes
Touch only what you must. Don't "improve" adjacent code. Match existing style. Remove only orphans YOUR changes created.

### 4. Goal-Driven Execution
Define success criteria. Transform tasks into verifiable goals. State a brief plan for multi-step tasks.

### 5. Change Documentation and Commit
Every change: update the GitHub issue, update `CHANGES.md`, commit and push.
