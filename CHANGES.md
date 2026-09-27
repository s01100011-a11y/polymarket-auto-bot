# CHANGES

## 2026-09-27 — NFL ingest defense against cross-sport nickname collisions

- Added a downstream fail-closed guard: explicit matchup selections must resolve to exactly two NFL teams before the NFL trading worker can proceed.
- This blocks college lines such as Hawaii vs Wyoming Cowboys even if an older bridge parser mislabels “Cowboys” as Dallas.
- Valid explicit NFL totals such as Jaguars vs Patriots remain supported.

## 2026-09-27 — Safe Telegram outage recovery for NFL/CFB (#129)

- Expanded NFL bridge lookback to 24 hours by default so picks missed during a Telegram outage can be rediscovered after reconnect.
- Added a bounded 24-hour recovery window for NFL and CFB while preserving the normal 180-second fast-path freshness window.
- Delayed recovery can enqueue a real BUY only when the exact matched event is still confirmed pregame; live, closed, or lifecycle-unknown NFL events fail closed.
- CFB recovery likewise requires a matched PREGAME event plus `CFB_CAPPER_LIVE_ENABLED`.
- Every recovery BUY still passes the existing LIVE/AUTO gates, exact market/outcome matching, current best-ask and spread limits, per-trade cap, daily budget, executor readiness, and Termux geoblock checks.
- Existing stale NFL records are allowed one recovery re-check so picks missed during the incident are not permanently stranded.
- Added recovery-window and pregame lifecycle regression tests.


## 2026-09-27 — NBA/WNBA Slack PW failover hardening (#127)

- Confirmed the production Tailnet direct-feed outage is upstream of Railway: the Railway Tailscale node is healthy while TLS handshakes to the NBAMonitor host time out on both MagicDNS and the peer IP.
- Generalized Slack Predicted Winner parsing and Polymarket matching from WNBA-only aliases to both NBA and WNBA.
- Slack live routing now derives the correct NBA/WNBA Polymarket sports URL instead of hard-coding WNBA.
- Preserved direct /api/pw-export polling as the primary path, cross-source deduplication, Railway live/auto gates, Termux executor readiness/geoblock checks, max-price/spread/stake limits, and duplicate-position protection.
- Added NBA and WNBA failover parsing/league-routing tests.


## 2026-09-26 — CFB bridge-outage manual fallback (#125)

- Added `CFB_CAPPER_MANUAL_PICKS_JSON` as an explicit fallback transport for user-verified Slam/Syndicate CFB picks when the Telegram bridge is unavailable.
- Manual fallback picks use the existing source classification, Polymarket market matching, unit sizing, dashboard signal history, and existing live/auto triple gates; no trading safety setting is bypassed.
- The worker continues to surface the Telegram bridge error while processing manual fallback picks.
- Added semantic deduplication so the same wager is not processed again if Telegram later recovers and delivers it with a different post timestamp or posted odds.
- Added tests for manual-source validation and semantic deduplication.


## 2026-09-26 — Consistent auto-live dashboard status

- Standardized the NFL and CFB capper panels to the same state wording: `ENABLED · AUTO LIVE`, `ENABLED · AUTO OFF`, or `DISABLED`.
- NFL now reports AUTO LIVE only when both Railway master gates (`LIVE_TRADING` and `AUTO_TRADING`) are active.
- CFB reports AUTO LIVE only when both master gates plus `CFB_CAPPER_LIVE_ENABLED` are active.
- Renamed the CFB panel heading from “CFB capper status” to “CFB capper auto-trading” to match NFL.
- Kept the existing CFB `mode` field for backward compatibility while adding explicit `auto_trading`, `live_trading`, `sport_live_enabled`, and `auto_live` status fields.

## 2026-09-26 — Full auto-trading across all four sports (#121)

- **CFB live trading**: Promoted from preview-only to live auto-trading.
  - Added `_prepare_pick()` in `cfb_capper_preview.py` — enqueues `BUY` with `"auto": True`
  - Added `CFB_CAPPER_LIVE_ENABLED` env var (default `false`); triple-gate safety preserved
  - Updated status tracking for both preview and live statuses
  - Updated dashboard mode indicator (`LIVE` vs `PREVIEW_ONLY`)
  - Updated `_recent_status_items()` to accept `str | set[str]` for multi-status filtering
- **NBA ingestion pipeline**: Added NBA PW export polling alongside WNBA.
  - Added `NBA_ALIASES` (30 teams) to `slack_ingest.py`
  - Added `_nba_team_mentions()`, `_looks_nba()`, `_looks_basketball()` helpers
  - Updated Slack basketball filter to accept both WNBA and NBA signals
  - Added NBA team aliases to `_team_name()` in `pw_export_ingest.py`
  - Updated `_synth_text()` to accept `sport` parameter for correct alert headers
  - Added `PW_NBA_EXPORT_URL` — when set, polls NBA feed alongside WNBA
  - Refactored poll loop into reusable `_poll_feed()` with separate NBA state file
  - Updated `/api/pw-export/status` endpoint to include NBA state
- **Config**: Added `CFB_CAPPER_LIVE_ENABLED` and `PW_NBA_EXPORT_URL` to `.env.example`
- **README**: Updated sports status table to reflect all four sports active

## 2026-09-26 — Comprehensive project documentation (#120)

- Complete README.md rewrite with architecture diagrams, module chain, signal paths, execution flow
- Created `docs/index.html` standalone HTML documentation with dark theme and sidebar navigation

## Previous changes

- `c9cfab1` Use Railway AUTO_TRADING runtime status
- `7a7f0dc` Make dashboard honor Railway AUTO_TRADING override
- `26b47ef` Apply runtime AUTO_TRADING override to approval queue
- `55cc09b` Use Railway AUTO_TRADING runtime override in Slack controls
