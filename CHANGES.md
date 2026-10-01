## 2026-10-01 — Repair basketball dashboard source and guard syntax

- Restored `app/basketball_monitor_capper.py` from the last known-good production revision after the first stake/shares UI patch corrupted the embedded JavaScript/Python boundary.
- Re-applied the open-position **Stake $X.XX · Shares X.XX** display cleanly.
- Added a regression test that compiles the dashboard Python source so malformed embedded-dashboard edits fail CI before deployment.

## 2026-10-01 — Show stake and share count on basketball open positions

- WNBA/NBA **Open positions** cards now display the tracked stake and current share holding.
- The card shows **Stake $X.XX · Shares X.XX** above Entry/Live price and Live P/L.
- Share count comes from the current live position mark, so partially sold positions display the remaining holding rather than the original quantity.
- Stake uses the execution's actual cost when available, falling back to the original trade budget.

## 2026-10-01 — Make PW retries flow through the normal basketball monitor pipeline

- Failed/replayed WNBA/NBA PW calls are now re-hydrated with normal monitor metadata before they are requeued: sport, monitor source, PW signal ID, selection, and posted timestamp when available.
- The retry execution therefore enters the same WNBA/NBA dashboard stats, open-position, live P/L, and settlement pipeline as a fresh legitimate PW call.
- Legacy Slack PW executions whose event IDs predate the newer `pwexport-...` prefix are now classified by their basketball selection/team when `source=slack_live`.
- Existing filled legacy PW positions such as the Dallas Wings execution can therefore appear under the correct WNBA **Open positions** section without rewriting the stored trade.
- Retry provenance is still preserved for safety/audit purposes; it no longer changes how the resulting legitimate PW trade is presented or counted.

## 2026-10-01 — Count distinct submitted PW calls on WNBA/NBA dashboard

- Added a separate **PW Calls** metric to the WNBA and NBA monitor cards.
- PW Calls counts distinct accepted bot order submissions by PW/Slack signal ID, so repeated calls on the same team count separately.
- Retries of the same signal are deduplicated and count once.
- A submitted order that fills 0 shares and is later canceled after the 120-second window still counts as a PW call because it reached Polymarket.
- **Bets**, W/L, stake, ROI, and P/L remain execution/fill based, so an unfilled canceled submission does not become a fake bet or alter performance.
- The Last 24h dashboard filter applies to PW Calls as well.
- This makes the two separately submitted Dallas Wings PW calls visible as two calls while preserving correct fill/performance accounting.

## 2026-10-01 — Colorize live trade activity in Termux

- Added bold ANSI trade-status colors to the phone executor so order activity stands out from queue polling noise.
- **Yellow**: a BUY/SELL/Combo order is being placed; BUY submission-start lines are also yellow.
- **Green**: an accepted or successfully completed BUY.
- **Red**: an accepted or successfully completed SELL.
- Preview, heartbeat, queue-poll, and ordinary diagnostic lines remain uncolored.
- Unfilled/canceled BUY completion lines are left uncolored so a canceled order is not visually mistaken for a successful buy.
- Colors default ON in Termux and can be disabled with `TERMUX_COLOR_LOGS=0` or the standard `NO_COLOR` environment variable.
- Added regression tests for color mapping and the disable switch.

## 2026-10-01 — Duplicate WNBA/NBA PW buys keyed by signal ID

- Slack live duplicate detection now keys repeat protection to the PW/Slack signal ID when one is available, rather than blocking every later call on the same team/outcome.
- Multiple legitimate PW calls on Dallas Wings (or any other selection) can therefore each generate their own live trade.
- The same Slack/PW call still cannot create a second active or already-open bot position.
- Both initial live dispatch and failed-BUY resend queue scans use the same signal-specific rule.
- Legacy/manual Slack-live payloads without a signal ID retain the broader market/outcome duplicate behavior.
- Added regression tests proving that different PW calls on the same asset are allowed while the same PW call remains blocked.

## 2026-10-01 — Allow explicit resend after pre-submission v2 wrapper TypeError

- The one-shot manual Slack BUY resend path now recognizes the known pre-submission v2 wrapper signature failure (`_buy() got an unexpected keyword argument 'request_id'`) as safely retryable.
- This is limited to explicit manual resend requests and still passes through the bot-owned duplicate/provenance checks before handoff.
- Added regression coverage for this exact historical wrapper failure.

## 2026-10-01 — Forward BUY provenance through Termux v2 wrapper

- Updated `scripts/termux_executor_v2.py` so its BUY wrapper accepts and forwards `request_id` and `executor_token` to the base executor.
- This fixes live BUY failures caused by the v2 wrapper retaining the old three-argument signature after accepted-order provenance tracking was added.
- Added regression coverage for provenance-argument forwarding.

## 2026-10-01 — Bot-order provenance separated from manual wallet holdings

- Existing wallet shares and open orders are no longer treated as proof that this bot already placed a WNBA/NBA PW signal.
- Slack live handoff now blocks duplicates using bot-owned execution records, accepted-order receipts, and active bot queue requests for the same signal.
- Existing manual/external shares are carried forward as `server_position_before` so the executor measures only incremental position changes.
- Existing external open-order IDs are recorded as baseline diagnostics but do not block the bot by themselves.
- Termux now persists the real Polymarket order ID locally immediately after acceptance and posts an authenticated submission receipt to Railway before waiting for fills.
- Railway stores that order ID on the exact executor request, so future audits can distinguish bot-placed orders from manual/external positions even if later reconciliation fails.
- Interrupted requests now report a known bot-owned order ID when one had already been accepted.
- Added regression coverage for manual-position baselines, external open-order baselines, bot-owned submission blocking, Railway submission receipts, and local order-ID journaling.

## 2026-10-01 — Explicit resend support for reconciled TransportError timeouts

- The one-shot manual Slack BUY resend path now recognizes `TransportError: timed out` in addition to explicit ConnectTimeout error codes.
- This does not create an automatic retry loop: it applies only when a specific failed request ID is explicitly armed for resend.
- The existing authoritative wallet-position and authenticated open-order reconciliation still runs first and blocks the resend if any exposure/order already exists.
- Added regression coverage for the explicit reconciled TransportError resend path.

## 2026-10-01 — Direct authenticated submit for server-authorized Slack live limits

- Moved duplicate-position and open-order reconciliation for WNBA/NBA `slack_live` resting BUYs to Railway immediately before executor handoff.
- Railway now requires a recent explicit non-blocked executor heartbeat, zero authoritative wallet shares for the outcome, and zero authenticated open orders before stamping a short-lived fast-preflight authorization.
- The Termux executor validates that short-lived authorization, exact sports URL/asset, budget, cap, price, and TTL locally, then skips redundant phone-side geoblock, public market lookup, and pre-buy wallet-position calls.
- Server-verified `position_before` is carried into post-submission reconciliation; once an order is accepted, duplicate BUY submission remains prohibited.
- SDK `TransportError: timed out` during submission is now treated as ambiguous unless an underlying `ConnectTimeout` cause proves the request never reached the peer.
- Added tests for no-network fast preflight, stale-authorization rejection, server handoff stamping, existing-position blocking, and open-order blocking.

## 2026-10-01 — Retry encoded preflight connection timeouts

- Failed Slack BUY retries now recognize both the raw `ConnectTimeout` class spelling and executor error codes such as `BUY_PREFLIGHT_CONNECT_TIMEOUT`.
- This allows safe pre-submission transport failures to be requeued through the 120-second resting-limit path after the existing wallet/open-order reconciliation.
- Added regression coverage for the encoded preflight timeout form.

## 2026-10-01 — NBA/WNBA live Slack orders rest at signal price for 120 seconds

- NBA/WNBA `slack_live` BUY payloads now include the exact Polymarket asset ID and a 120-second limit-order lifetime.
- The Termux executor no longer rejects these live limit orders merely because the current ask has moved above the signal price or the current spread widened; it submits the approved signal-price limit and lets it rest for a retrace.
- The local executor watches the order for 120 seconds, records partial/full fills, then cancels any unfilled remainder.
- A later exchange-side GTD expiration is attached as a safety backstop because the current SDK requires GTD expirations several minutes in the future.
- Executor queue leases are extended dynamically for resting BUYs so Railway cannot reclaim a legitimate 120-second order while it is active.
- Failed Slack BUY retries now carry the exact asset ID and the same 120-second limit behavior.
- Added regression tests for 120-second defaults, non-crossing resting limits, TTL bounds, and lease coverage.

## 2026-10-01 — Safe supervisor retirement fix

- Fixed the Termux cleanup path that could terminate the current launcher's process chain before the executor started.
- Legacy supervisors are now found through their existing `termux_executor_v2.py` child PID and only that worker's parent supervisor is terminated.
- The current launcher PID and parent PID are explicitly excluded.
- The prior direct supervisor command-line scan has been removed.

## 2026-10-01 — Retire legacy Termux supervisors on singleton startup

- The Termux launcher now detects and terminates older `start_termux_executor.sh` supervisor shells before launching the singleton worker.
- Legacy supervisors that ignore TERM are escalated to KILL after a short grace period, preventing them from respawning old workers behind the new supervisor.
- New supervisor revisions now exit cleanly on INT/TERM instead of merely running cleanup and continuing.
- Existing stale worker cleanup remains in place before the singleton executor starts.

## 2026-10-01 — Singleton Termux executor and longer live-order lease

- Added an OS-level exclusive worker lock so only one Termux executor process can consume the shared queue and journal at a time.
- A duplicate executor exits immediately with a dedicated exit code; the supervisor now treats that as a duplicate-supervisor condition and exits instead of restart-looping.
- Increased the executor queue lease from 20 seconds to a configurable 90-second default so a valid BUY cannot be reclaimed while its first worker is still completing network checks/fill reconciliation.
- This fixes repeated `interrupted after execution began` failures caused by overlapping Termux workers using the same journal.
- Added regression coverage for exclusive worker locking and lease duration.

## 2026-10-01 — Reconcile interrupted Slack BUY before resend

- Failed Slack BUYs interrupted after execution began can now be considered for a fresh resend only after authoritative reconciliation.
- The retry path checks the wallet for existing outcome shares and the authenticated CLOB for any still-open order on the same asset.
- Any existing position or open order blocks a new BUY; any reconciliation error fails closed and places no new order.
- Only when both checks prove empty can the interrupted request be requeued with a new request/trade ID.
- Added regression coverage for open-order blocking, empty-state resend, and reconciliation-query failure.

## 2026-10-01 — One-shot safe resend for failed Slack live BUYs

- Added a one-shot resend path keyed by `SLACK_RETRY_REQUEST_ID` for a specific failed Slack live BUY.
- Resend is allowed only for an original `BUY` with `status=FAILED`, `source=slack_live`, and a `ConnectTimeout` error.
- Before requeueing, the bot verifies dashboard/LIVE/AUTO gates, executor readiness, resolves the exact outcome token, checks the authoritative wallet for an existing position, and blocks if an equivalent BUY is already pending.
- The original request is stamped with the new retry request/trade IDs, making the resend idempotent across redeploys.
- Added regression coverage for one-time requeue and wallet-position duplicate prevention.

## 2026-10-01 — Safe retry for transient WNBA/NBA BUY connection timeouts

- Live single-market BUY execution now retries transient `ConnectTimeout` failures during Polymarket preflight, pre-order position lookup, and order submission.
- Submission retries are restricted to connection-establishment timeouts, where the request could not have reached the exchange; ambiguous read/write timeouts remain fail-closed to prevent duplicate orders.
- After an order has been accepted, position reconciliation can retry network reads but the executor never submits a second BUY for the same request.
- Added explicit BUY stage/retry logging so future failures identify preflight, position lookup, submission, reconciliation, or completion instead of reporting only a generic timeout.
- Added regression coverage for preflight retry, safe submission retry, and the no-duplicate guard after submission.

## 2026-09-30 — More Stats bet-type breakdowns (#198)

- Added ML, Spread, and Total performance breakdowns to every BY CAPPER row.
- Added the same ML, Spread, and Total breakdowns to every BY SPORT row.
- Added a third BET TYPES tab showing aggregate ML, Spread, and Total bets, W-L-P, win rate, stake, ROI, realized P/L, 7-day/30-day P/L, open count, and live P/L.
- Bet type is derived from the existing execution market type (moneyline/h2h, spread, total) so this is reporting-only and does not alter execution, matching, or sizing.
- Categories with no trades still render as zero rows so ML / Spread / Total are always visible.

## 2026-09-30 — Termux startup geoblock timeout recovery

- The Termux queue worker no longer exits when the public Polymarket geoblock endpoint temporarily times out during startup.
- Explicit blocked-region responses still stop the worker immediately.
- Live BUY, SELL, and COMBO_BUY execution still require a fresh successful geoblock check and remain fail-closed.
- Quote-only COMBO_PREVIEW can continue through a transient geoblock-endpoint timeout because it does not accept or place an order; an explicit blocked response still fails closed.
- Replaced the corrupted/duplicated Termux supervisor script with a clean deterministic launcher that safely self-updates a clean main checkout, kills stale workers, and launches the v2 wrapper/base queue worker.
- Added regression coverage for transient startup timeout handling, explicit geoblock blocking, quote-only Combo preview behavior, and strict Combo BUY geo enforcement.

## 2026-09-30 — Termux queue-poll diagnostics and single-BUY recorder fix

- Executor heartbeats now include queue-poll phase, last start/success timestamps, HTTP status, last error, and consecutive error count.
- Queue polling uses a bounded 8-second request timeout by default so a stuck `/api/executor/next` call cannot hide behind otherwise healthy heartbeats.
- Poll failures are immediately surfaced through the next heartbeat while keeping the worker fail-closed for order execution.
- Fixed a regression in normal single-BUY result recording where an undefined Combo-only `pending` variable could raise after a successful fill.
- Added regression tests for queue telemetry and successful single-BUY persistence.

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
