# CHANGES

## 2026-09-27 — Legacy CFB cleanup and game-state reconciliation (#142)

- CFB now purges persisted NFL nickname rows even when the old record has no normalized `pick` object or contains corrupted legacy event hints.
- Persisted CFB rows can reconstruct a minimal full-game pick from source, timestamp, and selection text (moneyline/spread/total) so older records are no longer stuck forever in RETRYING.
- ESPN game state is checked before Polymarket retry: pending rows are marked PREGAME, live rows LIVE, and finished rows CLOSED and graded WIN/LOSS/PUSH from the original line.
- Non-traded duplicate rows caused by parser/manual-fallback reprocessing are collapsed within the same game window, preferring graded/matched records and preserving score/result metadata.
- Reconciliation remains read/repair-only and does not enqueue a trade.

## 2026-09-27 — Reconcile CFB signals, alternate lines, ESPN scores (#140)

- Persisted NFL picks that leaked into the CFB store before the cross-sport ingest guard are now removed automatically on the next CFB poll.
- Persisted unmatched CFB picks are retried independently of Telegram replay, so legacy parser records such as Texas/Tennessee Under 55, Hawaii/Wyoming Under 45.5, and the Texas Tech typo can recover after the bridge has moved on.
- Exact spread misses now retry current same-game Polymarket alternate spreads and keep explicit BUY choices visible instead of silently changing the wager.
- If an unmatched game's original Polymarket market is already closed, ESPN final-score data can now identify the game and grade the original full-game moneyline/spread/total WIN/LOSS/PUSH.
- Live CFB cards now show the ESPN score/status, refreshed with the existing capper polling cycle.
- Reconciliation is read/repair-only: recovering a historical signal does not enqueue a new trade; existing auto/live/executor/risk gates remain unchanged.
- Added regression coverage for persisted NFL cleanup, closed unmatched grading, live alternate recovery, semantic reuse of normalized legacy records, and ESPN score rendering.

## 2026-09-27 — Capper open positions + live P/L (#138)

- NFL and CFB capper cards now show aggregate **Live P/L** for each capper separately from realized P/L.
- Added **Open positions** under every Slam/Syndicate capper card with entry price, executable live SELL price, shares, remaining cost basis, current value, and live unrealized P/L/%.
- Live marks reuse the dashboard's existing executable SELL-side Polymarket mark so capper P/L matches the portfolio dashboard rather than using a second pricing method.
- Open capper positions are now **amber**; settled wins remain green, losses red, and pushes amber/neutral.
- Position attribution continues to use `strategy_source` + `strategy_sport`, preventing cross-sport/capper P/L mixing.
- Added regression coverage for aggregate live P/L, per-position live values, and CFB amber/open-position UI.


## 2026-09-27 — Normalize legacy CFB bridge output in the bot (#136)

- Added an execution-side compatibility layer so CFB no longer depends on the bridge being on the newest parser build.
- Reconstructs explicit two-team matchup hints for slash/v/vs/@ totals such as Texas/Tennessee Under 55 and Hawaii/Wyoming Under 45.5.
- Normalizes the observed `TEXAS TEXCH` typo, strips score-prediction prefixes, and semantically deduplicates repeated picks.
- Bare NFL-nickname game picks leaking from the old NCAAF parser are failed closed, while explicit two-team college matchups remain eligible for exact CFB resolution.
- Existing CFB lifecycle, exact-market, price/spread, stake, budget, executor, and geoblock gates are unchanged.


## 2026-09-27 — Retry zero-fill capper BUYs safely (#134)

- Railway now treats a Termux BUY as successful only when the wallet actually gained positive shares; an old worker response with outer `ok=true` can no longer turn a 0-share fill into `DONE`.
- Zero-fill canceled limit orders are marked with `BUY_UNFILLED_RETRYABLE` and NFL/CFB workers can retry twice by default, refreshing the exact market, best ask, spread, budget, executor, and geoblock gates on every attempt.
- Existing 0-share `DONE` requests are migrated by the capper status sync, allowing Browns/Vikings incident signals to retry without duplicating the already-filled Steelers/Saints positions.
- Future Termux worker installs also return top-level failure for an unfilled BUY.


## 2026-09-27 — NFL exact-market resolution hardening (#132)

- NFL spreads now use Polymarket's structured `sports.line` and correctly invert the complementary outcome, matching alternate lines such as Steelers +3.5 when the market is encoded as Bengals -3.5.
- One-team NFL picks are narrowed to the unique nearby game date using the same fail-closed approach already used by CFB, preventing future-week events from tying current-week moneylines.
- NFL auto matching now requires exact full-game sports market types rather than accepting period variants containing the same type name.
- Added regression tests for structured complementary spread lines and current-week moneyline resolution.


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
