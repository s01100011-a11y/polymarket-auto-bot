## 2026-09-30 — Termux self-update and capability reporting

- The Termux supervisor now safely fast-forwards a clean `main` checkout from `origin/main` before launching the executor. It never overwrites a dirty checkout or a non-main branch.
- If `requirements.txt` changed during the update, the supervisor refreshes the existing virtualenv before launch and refuses to start if that dependency sync fails.
- Executor heartbeats now report the running Git revision and explicit supported actions.
- Railway executor status now exposes `worker_revision`, `capabilities`, and `combo_capable`, allowing Combo readiness to be verified remotely instead of inferred from connectivity.
- Added regression coverage for revision detection and Combo capability heartbeat fields.

## 2026-09-30 — Polymarket Combo RFQ execution (#186)

- Added a separate Polymarket Combo path that resolves 2+ sports legs to exact outcome position IDs and keeps ordinary single-market orders on the existing CLOB executor.
- Added protected `/api/combo/preview` and `/api/combo/buy` endpoints. Preview requests an RFQ without accepting it; live BUY accepts the winning RFQ and waits for terminal fill confirmation.
- Combo live execution is fail-closed behind the dashboard master switch, `LIVE_TRADING`, `AUTO_TRADING`, and the new `COMBO_TRADING_ENABLED=false` default gate.
- Combo requests reuse the current dashboard auto-trade cap, daily budget cap, executor connectivity, geoblock checks, stale BUY expiry, and server-stamped handoff authorization.
- Added a maximum Combo price guard so the executor rejects an RFQ whose blended price is worse than the requested ceiling.
- Termux can use explicit Builder API credentials or create and persist a Builder API key locally on first Combo use with protected file permissions.
- Execution records store the Combo label, every leg, leg position IDs, Combo position ID, blended entry price, RFQ/quote IDs, taker order hash, and transaction hash as one trade.
- Added regression coverage for Combo leg constraints, duplicate-leg rejection, queue expiry/caps, fill validation, executor payload validation, and builder-key configuration.

# CHANGES

## 2026-09-30 — Consistent sizing-control layout across all sports

- Renamed the fixed sizing label to `FIXED 1U WIN $` and moved the currency symbol into the label instead of displaying a standalone `$` beside the input.
- Moved the `SET 1U` button to the left of the fixed-unit input.
- Renamed the dynamic sizing label to `PORTFOLIO %` and removed the standalone `%` beside the input.
- Moved the `AUTO %` button to the left of the portfolio-percentage input.
- Applied the same layout to NFL, CFB, WNBA, and NBA capper/monitor cards.
- Updated the mobile sizing row from three columns to two columns so the button/input pair fills the row cleanly after removing the standalone symbol column.

## 2026-09-30 — NFL/CFB signal cards now match WNBA

- Reworked NFL and CFB signal rows to use the same `monitor-signal` card structure as the WNBA monitor feed.
- NFL/CFB signals now render as compact black inset cards with the same white title, smaller metadata text, spacing, borders, and NOT TRADED / P&L action line placement as WNBA.
- Removed the legacy grey/tinted inline signal-row backgrounds that were making NFL/CFB look different from WNBA.
- Preserved existing event badges and manual BUY/SELL/market actions, but they now sit inside the shared signal-card layout.

## 2026-09-30 — Fix top-window MODE inset override

- Fixed a CSS cascade bug where the shared `.capper-panel-status-row` rule was overriding the top MODE row's horizontal padding back to zero.
- The MODE field and master Online/Offline button now keep the same effective left/right inset as the sport AUTO-TRADING windows.
- Updated/Uptime now use the same 8px horizontal body inset so the first window aligns consistently as one section.

## 2026-09-30 — Graph range typography and top MODE spacing

- Changed the graph range/sample line beneath the `1D / 1W / 1M / 1Y / YTD / ALL` buttons from small monospace text to the same 12px sans-serif styling used by the sizing/meta text below each AUTO-TRADING status field.
- Increased the left/right padding of the top MODE row so the MODE field and master Online/Offline button align with the spacing used inside the sport AUTO-TRADING windows.
- Kept the master button on the right while preventing the MODE field from crowding the window border.

## 2026-09-30 — Standalone live graph window

- Separated the live Portfolio P/L graph from the top S01807/Wallet window so it renders as its own Win95-style window with teal desktop visible around it.
- Kept Wallet inside the top application window, but moved the graph host outside the Wallet layout.
- Moved the graph duration controls (`1D`, `1W`, `1M`, `1Y`, `YTD`, `ALL`) into a dedicated grey control row directly underneath the blue graph title bar.
- Moved the range/sample text into that same row, leaving the graph title bar itself title-only.
- Kept the existing graph data, range persistence, and refresh behavior unchanged.

## 2026-09-30 — Teal window spacing and top MODE control

- Inset the major dashboard windows slightly from the page edges so the teal desktop wallpaper remains visible around each window on desktop and mobile.
- Removed the words `Version date` from the application title bar; the title bar now shows only the date next to the S01807 version.
- Moved the dashboard `LIVE / DRY RUN · Auto ON/OFF` MODE value out of Wallet and into the top application chrome.
- MODE now uses the same labeled white inset field dimensions and styling as the sport AUTO-TRADING status fields.
- Moved the dashboard master Online/Offline button onto the same MODE row at the right and applied the same capper power-button classes/color treatment.
- Removed the duplicate Mode box from the Wallet body.

## 2026-09-30 — Event badge parity

- Changed NFL/CFB result/state badges such as WIN, LOSS, PUSH, LIVE, FINISHED, PREGAME, QUEUED, DONE, FAILED, and RETRYING to use the same raised Windows-grey badge frame as the other capper badges.
- Removed the translucent coloured badge backgrounds/borders that were making those badges look like a different component family.
- Preserved status meaning using text colour only: green for positive/live, red for loss/failure, amber for push, blue for pregame, and dark grey for neutral/finished states.
- Matched badge height, font size, padding, border treatment, and shadow to the existing capper badge system.

## 2026-09-30 — Merge Wallet into app window and normalize signal badge

- Moved the Wallet/live P&L layout inside the main S01807 application window so it is visually part of the top window instead of a separate window below it.
- Removed the injected `WALLET` title bar; the top S01807 title bar is now the only title bar for that combined top area.
- Moved the version date into the blue S01807 title bar next to the version number.
- Split `Updated` and `Uptime` into separate larger white inset text boxes under the title bar.
- Increased the capper section badge to the same height, font weight, Win95 border treatment, and grid width as the surrounding signal/status badges so `Signals`, `Open positions`, and `Settled positions` no longer look like a different control style.

## 2026-09-30 — Consistent capper section badges

- Fixed the duplicated `Signals signals` heading in the NFL and CFB capper cards; the section now reads simply `Signals`.
- Standardized the `Open positions`, `Settled positions`, and signal/tab section headings onto one shared Win95-style badge treatment across NFL, CFB, WNBA, and NBA.
- Standardized NFL/CFB lifecycle badges such as PREGAME, LIVE, FINISHED, WIN, LOSS, and PUSH onto one shared shape, typography, padding, and alignment while preserving their status colors.
- Empty sections now keep the same section badge visible, so formatting does not change just because there are no rows.

## 2026-09-29 — White STATUS fields and grey capper cards

- Reworked the sport AUTO-TRADING status row so the current `ENABLED · AUTO LIVE/OFF` text sits inside a white Win95-style text field labeled `STATUS`.
- Applied the same STATUS field treatment to NFL, CFB, WNBA, and NBA.
- Changed the outer capper/monitor card background from black to Windows grey for Slam NFL, Syndicate NFL, Slam CFB, Syndicate CFB, WNBA Monitor, and NBA Monitor.
- Kept the inner sizing, metrics, position, and signal data areas dark so the grey card acts as a clear container around the black data boxes.
- Updated theme regression tests for the new status field and capper-card color hierarchy.

## 2026-09-29 — Sport AUTO-TRADING title bars and sport-level power buttons

- Renamed the sport windows to the shorter consistent titles: **NFL AUTO-TRADING**, **CFB AUTO-TRADING**, **WNBA AUTO-TRADING**, and **NBA AUTO-TRADING**.
- Moved each sport's `ENABLED · AUTO LIVE/OFF` status out of the blue title bar into a dedicated status row immediately underneath it.
- Added a dedicated Online/Offline button on that status row. NFL and CFB now have a sport-level master gate in addition to the existing Slam/Syndicate capper-level switches.
- WNBA and NBA are now rendered as separate sport windows instead of a combined basketball auto-trading window; each uses its existing persistent monitor switch as the sport-level Online/Offline control.
- Turning the NFL/CFB sport switch offline records new calls as paused and blocks new automatic order preparation while preserving queued/open positions and the individual capper settings.
- Added/updated regression coverage for the renamed title bars, status-row controls, sport-level endpoints, and execution gates.

## 2026-09-29 — Per-capper online controls and left-aligned sport headers

- NFL, CFB, WNBA, and NBA capper/monitor cards now have their own persistent **Online / Offline** control, using the same green/red status-button language as the dashboard master switch.
- Turning a capper offline stops that source from creating new automatic trades without touching existing queued orders or open positions. NFL/CFB paused signals remain visible and can resume normal freshness checks after the capper is turned back online.
- NBA/WNBA execution now checks the same persisted per-monitor switch before paper/live order preparation, so the dashboard button is an actual execution gate rather than a display-only control.
- Sport headers were reformatted as a left-aligned stack: **[SPORT] capper auto-trading** is the title, the **ENABLED · AUTO LIVE/OFF** state appears immediately below it, followed by sizing/meta text and controls.
- The redundant `S01-807 · POLYMARKET SPORTS DESK` text was removed from the top application info strip while preserving the version/date and master Online/Offline control.
- Added regression coverage for persistent independent capper states, backend execution gates, Online/Offline buttons, and the left-aligned title-bar layout.

## 2026-09-28 — Missed P/L uses call-time unit size

- Missed-P/L calculations no longer use the capper's current fixed/AUTO % unit value as the fallback for historical calls.
- Every newly ingested NFL/CFB signal now freezes an immutable call-time sizing snapshot: 1u dollar value, target profit, sizing mode, portfolio percentage, portfolio value, and snapshot timestamp.
- Later changes to fixed 1u, AUTO %, or portfolio value do not rewrite that snapshot, so a call made when 1u was $10 remains a $10-unit call even if 1u is now $50.
- When an initial quote is available, the intended cash risk is also frozen as `stake_usdc_at_call` for loss-side missed-P/L calculations.
- Legacy signals without a per-call snapshot fall back to their own persisted unit/target first, then to the sport's historical default unit — never today's dynamic capper unit.
- NFL and CFB signal cards now display the call-time sizing snapshot rather than the capper's current unit value.


## 2026-09-28 — Automatic portfolio-percentage capper units

- Added an **AUTO %** control next to the fixed **SET 1U** control for Slam and Syndicate on both NFL and CFB cards.
- The percentage input defaults to **10%**. Example: a $500 total wallet value produces a $50 **1u WIN** target; if the wallet moves to $650, 1u automatically becomes $65 without another manual update.
- Total wallet value is available USDC plus the current marked value of Polymarket positions. The calculation refreshes from the latest executor wallet heartbeat whenever capper status/sizing is evaluated.
- Clicking **SET 1U** switches that capper back to fixed-dollar mode. Clicking **AUTO %** enables/updates dynamic percentage mode. Each sport/capper keeps its own setting.
- Percentage mode fails closed when a usable portfolio value is unavailable, so it will not silently fall back to a stale fixed size for a new trade.
- Portfolio percentage controls the **unit profit target**, not the cash risk. Required risk is still calculated from current executable odds and remains subject to max-trade, daily-budget, price/spread, executor, and duplicate-position guards.


## 2026-09-28 — Capper units are profit targets with per-capper controls

- NFL and CFB Slam/Syndicate cards now each have an independent **1u WIN $** control with a **SET 1U** button. Overrides persist on the Railway data volume and apply to new/retried orders without changing already queued/open positions.
- Posted capper units now mean **profit to win**, not flat amount staked. A 1u call with a $10 unit targets $10 profit; a 2u call targets $20 profit.
- The required cash stake is calculated from the current executable Polymarket best ask using binary payout economics. Example: at 54¢, a $10 profit target risks $11.74; at 64¢, a $20 profit target risks $35.56.
- MAX_AUTO_TRADE_USDC, daily budget, max-price/spread, executor/geoblock, and duplicate-position gates still apply to the calculated cash stake.
- Signal cards now show posted units, **risk $**, and **to win $** separately, and execution audit fields store the unit value, target profit, and `TO_WIN` sizing mode.
- Missed-P/L calculations use the same profit-target unit semantics and each capper's configured unit value.


## 2026-09-28 — Safer NFL alternate-spread outage recovery (#148)

- NFL still requires the exact Telegram spread first. When the exact line is not listed, an opt-in fallback can resolve only the same unique nearby NFL event and choose the nearest **strictly better** spread for the selected team.
- The fallback is bounded by `NFL_CAPPER_ALT_SPREAD_MAX_POINTS` (default 1.0 point) and is disabled by default via `NFL_CAPPER_BETTER_SPREAD_FALLBACK_ENABLED=false`.
- Worse lines are never auto-substituted. Ambiguous events, duplicate markets, closed markets, and alternatives outside the configured distance remain blocked.
- Existing LIVE/AUTO, Termux executor/geoblock, MAX_PRICE, MAX_SPREAD, stake cap, daily budget, and duplicate-position controls remain unchanged.
- Execution audit records now retain requested vs executed spread line plus whether the safer-line fallback was used.
- Added regressions for nearest-safer selection and rejecting worse/too-distant alternatives.


## 2026-09-27 — Close finished CFB games from matched event date (#146)

- ESPN reconciliation now prefers the persisted matched CFB `event_start_at` date instead of relying only on the Telegram post date, so early-posted picks still find the actual game after it finishes.
- Persisted `espn_event_id` is reused when available, preventing short one-team hints from being reattached to a different same-window game.
- A final ESPN snapshot immediately changes the saved signal to `EVENT_CLOSED` and grades the original full-game pick even when Polymarket still reports the market as open/live.
- Existing trade execution, sizing, price/spread, executor, and geoblock gates are unchanged.
- Added regressions for early-posted picks and stored ESPN event-id disambiguation.

## 2026-09-27 — Hard CFB/NFL routing boundary (#144)

- CFB now classifies a potential NFL leak from the visible wager selection itself, so missing `team_hint` values or corrupt two-team `event_hints` can no longer let bare NFL picks such as Saints/Vikings/Steelers/Browns through.
- Cross-sport purge and legacy dedupe are persisted immediately at the start of each CFB poll, before ESPN/Polymarket reconciliation, so a later network/reconciliation failure cannot leave already-identified NFL contamination on disk.
- Explicit two-team college selections remain eligible for normal CFB matching.
- Added regressions for NFL moneyline picks with missing metadata and corrupt event hints.

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
