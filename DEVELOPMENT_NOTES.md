# Development Notes

> This file is the permanent engineering log for changes made to the Polymarket auto bot. It was added retrospectively on 2026-09-19 from the repository's actual commit history, then is intended to be updated alongside future code changes.

## Current system state

- Production runs on Railway with a persistent `/app/data` volume.
- WNBA PW history database contains the original 1,921 stored PW alerts. The database was preserved after a research-import deduplication issue was detected and cleaned up.
- Play-by-play research data contains 61,398 events covering all 151 WNBA games that currently have PW calls.
- PW historical research imports are separated from the live trading path so old calls cannot create trades.
- The private PW server is reached over Tailscale/Tailnet. The live endpoint has worked, but intermittent TLS/handshake/read timeouts have been observed. Research backfill retry logic is in place.
- Railway runtime variables are treated as the authoritative controls for live/auto trading where the later commits explicitly changed that behavior.

## Engineering areas covered

### Repository and deployment foundation

The bot was moved into a standalone repository, given a safe environment template, dependency/runtime setup, data-directory handling, Railway deployment configuration, and later a userspace Tailscale startup path for private PW-server connectivity.

### Dashboard and observability

The dashboard evolved from an authenticated status/trading view into a richer operational UI with Slack alert debugging, wallet status, current/history views, live P/L, filters, strategy performance, version/date display, executor status, sell/close controls, and reconciliation-aware metrics. Structured logging and watch-loop health reporting were added so failures can be diagnosed from Railway logs.

### Slack WNBA ingestion and trading flow

Slack WNBA alerts were added first as paper signals, then connected to live/remote execution paths. Subsequent work unified stake controls, added dashboard toggles, made Railway `AUTO_TRADING` and `LIVE_TRADING` runtime values authoritative, and added safeguards/queue behavior around execution. Later changes allowed Railway-authorized alerts to flow through the live handler while keeping runtime switches explicit.

### Termux execution bridge

A pull-based Termux bridge and execution worker were added for Polymarket order execution. Work then added heartbeat handling, IPv4/geoblock robustness, live SELL support, bounded exits, per-trade close tracking, partial closes, executor result persistence, duplicate SELL prevention, and dashboard controls/status.

### Position reconciliation and P/L accuracy

A series of fixes moved P/L accounting away from requested/limit values toward authoritative wallet/position/executed-trade data. The bot now reconciles fills, caches reconciliation source, tracks exact cost basis, handles partially closed positions, closes resolved positions correctly, and shows reconciliation-aware metrics in the dashboard.

### WNBA PW history and strategy research

Historical PW data was backfilled in multiple files, loaded into a persistent SQLite database, exposed via XLSX/database endpoints, and summarized at startup. PW strategy filters were added and tested independently, including deduplicated aggregate stats and forward-vs-historical performance views.

### Tailnet/PW-server integration

Railway was equipped with Tailscale binaries and userspace networking. Connectivity went through several iterations: diagnostics, HTTP over Tailnet, SOCKS5, direct socket handling, TLS/SNI correction, HTTP CONNECT tunneling, connection reuse, retry behavior, schema mapping, and safe bootstrap logic for the continuous PW export poller.

## 2026-09-19 — PW historical + play-by-play research work

This section records the detailed work completed in the current research expansion.

### Safe historical research sync

- Added `app/pw_research_sync.py` as a research-only synchronization module.
- Added a separate `play_by_play` table keyed by `(game_id, play_id)` with period, clock, scores, score differential, team, event type, scoring flag, raw JSON, and sequence fields.
- Added research metadata columns to PW alerts (`research_source`, `research_raw_json`) only when absent.
- Historical PW records are inserted directly into research/history storage instead of being routed through the live execution function. This is intentional: historical imports must never generate trades.
- Added `/api/pw-research/status`, `/api/pw-research/game/{game_id}`, and `/api/pw-research/sync` endpoints behind dashboard authentication.
- Added `app/wnba_pw_research_v13.py` and changed `start.sh` to launch the research-enabled application.

### Play-by-play import

- Imported ESPN WNBA play-by-play for every game currently represented by PW history.
- First completed pass: 151/151 games, 61,398 play-by-play rows inserted.
- Second verification pass: 61,398 rows seen and 0 inserted, confirming the `(game_id, play_id)` deduplication was working.
- Updated the importer to skip games that already have stored play-by-play so future deployments do not refetch completed games.

### PW historical import and deduplication incident

- The private PW server returned 2,556 historical rows during one successful full-history request.
- Initial research import inserted 2,550 rows because server timestamps were not normalized to the same `+08` representation used by the existing WNBA database.
- This was detected immediately before the expanded dataset was used for analysis.
- All 2,550 rows from that pass were identifiable through `research_source='pw-server'` and were deleted by a one-time migration, preserving the original 1,921-alert database.
- Timestamp normalization was changed to convert server timestamps through UTC and store them in the same Kuala Lumpur `YYYY-MM-DD HH:MM:SS +08` format used by the existing history.
- The research import version is recorded so the cleanup is not repeated.
- Full-season research scan was separated from the live poller's shorter lookback and now defaults to 2026-05-01.
- PW research backfill retries were added for temporary private-server outages; play-by-play does not need to be reloaded during those retries.
- Research fetch TLS/read timeouts were lengthened after intermittent Tailnet handshake/read timeouts were observed.

### Current research status

- Original WNBA PW database: 1,921 alerts.
- Games with PW calls: 151.
- Play-by-play stored: 61,398 rows across all 151 games.
- Historical PW expansion: code is ready and safe, but completion depends on a successful private PW-server full-season response after timestamp-normalized deduplication.
- No historical PW record is allowed to trigger a live trade.

### Commits for this research expansion

- `bedeffa` — Add safe PW historical and play-by-play research sync
- `eb30b1d` — Wire PW research sync into WNBA app
- `699f5d2` — Start WNBA research sync app
- `1d50d1c` — Scan full WNBA season and retry PW research backfill
- `95902e9` — Skip play-by-play games already stored
- `7d8e5e9` — Fix PW historical dedupe timestamp normalization
- `66b0691` — Harden full-season PW research fetch

## Important operating invariants

- Historical/backfill data must never be passed through a code path that can execute a trade.
- Persistent Railway data lives under `/app/data`; seed/backfill files that must survive image rebuilds are handled separately from runtime state.
- Runtime trading switches must be read from the authoritative Railway environment values rather than stale imported constants.
- P/L should use actual/reconciled fills and current authoritative positions whenever available, not requested limits.
- Duplicate event/trade protection must remain in place for Slack, PW and SELL/close flows.
- Private PW/Tailnet outages must not prevent the main API health endpoint from starting.

## Complete commit index

The repository contained 140 commits when this retrospective log was created. The index below preserves the exact commit subject history from the standalone repository's first commit through the most recent engineering change before this log.

### 2026-09-16

- `999a653` — Move Polymarket bot into standalone repository
- `4b71aff` — Add safe environment template
- `93c8f65` — Add gitignore
- `00429aa` — Add Python dependencies
- `d2ad8e5` — Initialize app package
- `2a856a2` — Add Polymarket auto-trading service
- `0067f4c` — Keep runtime data directory
- `b6e2425` — Add tzdata for Termux timezone support
- `5efb5a7` — Support sports event URLs and named outcomes
- `fc62b88` — Ignore local runtime state and secrets
### 2026-09-17

- `55eab00` — Add Railway deployment configuration
- `3818cc8` — Add authenticated trading dashboard
- `b6ee050` — Add Slack WNBA paper-trading receiver
- `058fd07` — Tune Slack WNBA predicted-winner paper signals
- `7cdc343` — Add Slack alert debug log to dashboard
- `535957c` — Add live P&L chart and richer trade views
- `8949503` — Add protected live trading auth and buy/sell test flow
- `c4ec5a6` — Add wallet balance and connection status to dashboard
- `e7399c7` — Add moneyline-only live test dashboard
- `0994db7` — Add pull-based Termux execution bridge
- `81bdadf` — Add Termux Polymarket execution worker
- `4f81266` — Improve Termux live sell execution with bounded FAK exits
- `08552b7` — Add per-trade live SELL controls and partial-close tracking
- `cad699a` — Fix Termux v2 direct script import
- `a979d0d` — Refresh CLOB token balance before Termux sells
- `bf13105` — Add dashboard performance metrics and position reconciliation
- `a7c38a5` — Fix paper close button HTML replacement
- `99dfdfc` — Make paper close UI replacement exact
- `c6e7477` — Fix dashboard button template literals
- `ff035d6` — Remove escaped dashboard template backticks
- `358068e` — Reconcile already-closed Termux sells in dashboard
- `b9f419a` — Reconcile stale open trades from failed zero-share sells
- `e6343f1` — Reconcile confirmed closed trades even if executor is offline
### 2026-09-18

- `c95f0b3` — Support live Slack trade queue results
- `dd1d24b` — Track Slack live Termux executions and allow exits
- `479d67c` — Clean Slack live execution metadata
- `a298dcf` — Show live SELL controls for Slack live positions
- `420ff68` — Add dashboard control for Slack live Termux trading
- `5157f88` — Add paper close action alongside live sell controls
- `59ecff6` — Fix Termux executor heartbeat timeout
- `821a949` — Force IPv4 geoblock checks and keep Termux bridge alive
- `3a57759` — Use one Slack stake for paper and live trades
- `6f3ba6f` — Add paper live both filters for stats and open trades
- `0fcf548` — Exclude manual Termux tests from live performance stats
- `ca43874` — Stop base dashboard refresh overwriting filtered stats
- `68a7ebf` — Apply paper live filters to P&L card and graph
- `67ca410` — Add WNBA PW historical backfill part 1
- `e7f855d` — Add WNBA PW historical backfill part 2
- `a5cec87` — Add WNBA PW historical backfill part 3
- `33a4336` — Add WNBA PW historical backfill part 4a
- `79917b5` — Add WNBA PW historical backfill part 4b
- `d48e07c` — Add WNBA PW historical backfill part 5
- `9eb575a` — Add WNBA PW historical backfill part 6
- `5929d1b` — Add WNBA PW historical backfill part 7
- `a5af41f` — Add current WNBA PW historical backfill
- `9df4a27` — Add persistent WNBA PW database and XLSX export
- `a0ed789` — Add compatibility wrapper for WNBA history database
- `8108620` — Log WNBA PW database summary on startup
- `087b628` — Copy WNBA PW seed data wnba_pw_backfill_01.jsonl
- `fdc8c1e` — Copy WNBA PW seed data wnba_pw_backfill_02.jsonl
- `85a4f01` — Copy WNBA PW seed data wnba_pw_backfill_03.jsonl
- `4d57a72` — Copy WNBA PW seed data wnba_pw_backfill_04a.jsonl
- `0d4d739` — Copy WNBA PW seed data wnba_pw_backfill_04b.jsonl
- `4cc85b3` — Copy WNBA PW seed data wnba_pw_backfill_05.jsonl
- `410ebc0` — Copy WNBA PW seed data wnba_pw_backfill_06.jsonl
- `bc471f0` — Copy WNBA PW seed data wnba_pw_backfill_07.jsonl
- `28925b1` — Copy WNBA PW seed data wnba_pw_backfill_08_current.jsonl
- `1b4242f` — Load WNBA PW backfill seeds outside persistent data mount
- `607173e` — Add PW bet filters and dashboard filter performance panel
- `eb40fe2` — Show cumulative performance after each enabled PW filter
- `2b8f8f9` — Test PW filters as independent paper strategies
- `0b722b4` — Show forward bot and historical stats for all PW filters
- `36bb296` — Add deduped total stats across PW filters
- `292376d` — Add dashboard Auto Trading toggle
- `2122d4b` — Replace unattended Slack live mode with approval queue
- `1099801` — Mark Slack live orders as prepared until approved
- `cd92507` — Use dashboard auto trade cap for Slack stake limit
- `e9c9953` — Make dashboard auto trade cap authoritative for remote execution
### 2026-09-19

- `ba2161f` — Make Railway AUTO_TRADING authoritative for Slack live execution
- `6444b85` — Fix AUTO_TRADING override syntax
- `2f5f294` — Fix literal backslash-n in Slack trading mode
- `6efe561` — Fix escaped newlines in AUTO_TRADING override
- `9c0a48a` — Restore escaped newline in dashboard HTML string
- `f2cdd66` — Make Railway AUTO_TRADING authoritative at runtime
- `55cc09b` — Use Railway AUTO_TRADING runtime override in Slack controls
- `26b47ef` — Apply runtime AUTO_TRADING override to approval queue
- `7a7f0dc` — Make dashboard honor Railway AUTO_TRADING override
- `34bdc44` — Use Railway AUTO_TRADING runtime status
- `c9cfab1` — Use Railway AUTO_TRADING runtime status
- `629549f` — Queue Railway-authorized Slack live trades automatically
- `806390b` — Make Railway LIVE_TRADING authoritative at runtime
- `b14f192` — Allow Railway-authorized PW alerts to use live handler
- `efcf3fb` — Expose watch loop health and failures
- `d9fdd17` — Add structured JSON logging foundation
- `0d2325d` — Wire structured logging into core watch loop
- `794b922` — Add regression tests for Railway trading switches
- `e52508e` — Use authoritative position data for live PnL
- `916904c` — Clarify live position cost and PnL labels
- `a2ece6e` — Use actual position entry for sell PnL accounting
- `21c73f3` — Reconcile live entries from Polymarket executed trades
- `860c515` — Show reconciled fill metrics in dashboard
- `4981f5f` — Verify and cache Polymarket fill reconciliation
- `ed91fc3` — Persist live fill reconciliation source
- `53e8c7a` — Make final dashboard estimator use network-reconciled fills
- `3917e5a` — Reconcile open live positions in background
- `b38dc01` — Use exact reconciled cost for live PnL
- `d06d64c` — Log reconciled live position marks
- `00c157e` — Reconcile stale open trades from wallet position
- `d452648` — Keep partially closed positions reconciled
- `7ac6c73` — Use full wallet address for position reconciliation
- `84287a4` — Show bot version and version date in dashboard header
- `ffc9c29` — Add live trade sell button
- `611205f` — Fix SELL pricing for sub-cent Polymarket markets
- `1081d14` — Expose executor status and prevent duplicate SELL requests
- `ebd3713` — Add persistent executor status to Current tab
- `f368849` — Close resolved positions even when wallet shares remain
- `40cf233` — Bump dashboard version for settlement reconciliation
- `41a50eb` — Add Tailscale userspace startup for Railway
- `e1f1d8a` — Build Railway bot with Tailscale binaries
- `3828c76` — Add PW tailnet connectivity diagnostics
- `e5e04e1` — Use HTTP PW endpoints over encrypted Tailnet
- `f9f8cce` — Enable SOCKS5 support for private PW access
- `6dc8bf0` — Route PW HTTPS checks through Tailscale SOCKS5
- `dae2f1f` — Add continuous PW export poller with safe bootstrap
- `ca31d55` — Wire PW export poller into strategy pipeline
- `e8822f5` — Start bot immediately after Tailscale is ready
- `ca69ce7` — Warm Tailscale route before PW export polling
- `3afeb52` — Add direct SOCKS socket support for PW feed
- `c58f464` — Connect PW HTTPS via Tailnet IP with correct TLS SNI
- `8b16e66` — Use Tailscale HTTP CONNECT tunnel for PW HTTPS
- `812ace9` — Map live PW export schema and version safe dedupe
- `ad78472` — Reuse PW Tailnet HTTPS connection with retry
- `ac341bd` — Stabilize PW poller startup and fast retries
- `bedeffa` — Add safe PW historical and play-by-play research sync
- `eb30b1d` — Wire PW research sync into WNBA app
- `699f5d2` — Start WNBA research sync app
- `1d50d1c` — Scan full WNBA season and retry PW research backfill
- `95902e9` — Skip play-by-play games already stored
- `7d8e5e9` — Fix PW historical dedupe timestamp normalization
- `66b0691` — Harden full-season PW research fetch

## 2026-09-19 — Documentation backfill and new logging standard

- Reconstructed the standalone repository history from GitHub and documented all 140 commits that existed before the documentation pass.
- Added this `DEVELOPMENT_NOTES.md` file as the permanent engineering/audit record.
- Added a README link so the development record is visible from the repository front page.
- Recorded the current WNBA PW/play-by-play state, Tailnet limitations, database safeguards, and operating invariants.
- Established the rule that future code changes must update this file with: objective, files changed, behavior change, data/schema impact, configuration impact, safety implications, deployment result, tests/verification, issues found/fixed, and outstanding work.
- Documentation commits created during this pass:
  - `9cef884` — Add complete development notes and commit history.
  - `3c143dc` — Link full engineering history from README.
- No application behavior, trading configuration, database contents, or runtime secrets were changed by this documentation-only pass.

## GitHub issue requirement for every change

Starting with issue #9, every repository/configuration change must have a GitHub issue created before or at the start of implementation.

Each issue must record:
- objective and reason for the change
- files/components changed
- implementation details
- database/schema impact
- environment/configuration impact
- trading/safety implications
- commits produced
- tests and verification performed
- Railway deployment result when applicable
- problems found and fixes applied
- outstanding work
- final completion/verification before closure

`DEVELOPMENT_NOTES.md` remains the chronological summary, while the GitHub issue is the detailed work record for the individual change.

Read-only investigation that produces no code/configuration change does not require a change issue unless it turns into implementation work.

## Future note format

For every future code change, append an entry containing: objective, files changed, behavior change, data/schema impact, configuration impact, safety implications, deployment result, tests/verification, problems found/fixed, and outstanding work. Commit messages should stay concise; this file carries the full operational record.


## 2026-09-19 — Fix LIVE stats omission for real Termux executions (#10)

- **Objective:** Fix a real live trade being omitted from the dashboard LIVE statistics/P&L view.
- **Root cause:** `dashboard_filters_v5._trade_bucket()` and `dashboard_pnl_filters_v6._mode_totals()` treated only `source=slack_live` as LIVE. Real funded `termux_executor` records were therefore omitted from LIVE stats/P&L.
- **Files changed:** `app/dashboard_filters_v5.py`, `app/dashboard_pnl_filters_v6.py`.
- **Behavior change:** Actual non-paper, non-dry-run funded executions from both `slack_live` and `termux_executor` are now classified as LIVE. Open positions are counted separately in the stats label and included in LIVE unrealized P/L.
- **Performance semantics:** W/L, accuracy, graded count, and realized ROI remain closed/graded-trade-only. Open trades do not become wins/losses before closure.
- **Safety:** Reporting/stat aggregation only. No trade placement, cancellation, closing, sizing, or execution behavior was changed.
- **Commits:** `a84ad7f` (LIVE classification/open count), `35e3be1` (LIVE P/L inclusion).
- **Deployment:** Railway production deployment `2c98983a-66ab-4ca6-ae3a-408eb32e357b` reached SUCCESS.
- **Verification:** Service startup completed, /health passed, and no startup exceptions were introduced by the stats changes.


## 2026-09-19 — Use top four PW single-condition filters (#11)

- **Objective:** Align the four individual WNBA PW strategy filters with the current top four raw alert-level ROI conditions from the 1,921-call database.
- **Enabled strategy set:** Away Underdog, Plus-money, Q3 Away, Home Underdog.
- **Replaced strategy:** Plus-money + PW edge >= 40pp was removed from the four-filter strategy set and replaced by Home Underdog.
- **Code change:** `app/wnba_pw_strategy_test_v12.py` now defines/matches `home_underdog` and reports `home_dog` at startup.
- **Railway variables:** `PW_STRAT_HOME_DOG=true`; legacy `PW_STRAT_PLUS_EDGE40=false`.
- **Unchanged:** Separate composite PW filter, moneyline-only execution, duplicate-position protection, max stake, max price, spread limits, live/auto switches, and other execution safeguards.
- **Commit:** `0f7fb90`.
- **Deployment:** Railway production deployment `898d6377-1b70-4138-8d00-89d7185cbf54` reached SUCCESS.
- **Verification:** Startup logged `PW_STRATEGY_TEST_READY enabled=True q3_away=True away_dog=True plus_money=True home_dog=True paper_only=True repeated_alerts=True`. PW database remained 1,921 alerts / 1,362 graded. No historical orders were created by deployment.
