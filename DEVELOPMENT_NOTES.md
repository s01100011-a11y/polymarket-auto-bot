## 2026-09-26 — Forward executable PW spread capture (#15)

- Added `app/pw_spread_capture.py` as a research/shadow-only forward recorder; it has no order-placement, sizing, routing, or position-management path.
- Every genuine persisted PW alert now gets a parallel spread-capture pass after the existing alert handlers. The recorder resolves the same WNBA event and enumerates every open full-game Polymarket spread token that can be mapped unambiguously to the PW-selected team.
- Spread-line decoding reuses the existing fail-closed signed-line logic used by the CFB sports resolver, including inversion for the complementary team side.
- Each watch stores the PW event/game/team/quarter/score/timestamp, BK moneyline and BK spread, same-side call number, Polymarket event/market/condition/token identifiers, selected signed Polymarket line, and outcome label.
- New durable SQLite tables `pw_spread_watches` and `pw_spread_ticks` store natural live captures separately from historical/proxy research.
- Active spread tokens are sampled at `PW_SPREAD_CAPTURE_SAMPLE_SECONDS` (default 1 second) for `PW_SPREAD_CAPTURE_WINDOW_SECONDS` (default 300 seconds).
- Every sample records executable BUY/SELL prices, midpoint, CLOB spread, best bid/ask and sizes, total visible bid/ask depth, plus latest quarter/clock/score context.
- Added authenticated `/api/pw-spread-capture/status` for recorder health/coverage and `/api/pw-spread-capture/forward` for forward paper diagnostics.
- Forward diagnostics separate first PW calls from repeats and also provide a one-position-per-game/team view. The one-position view takes the first PW call for that side and selects the captured Polymarket line closest to the stored BK spread, breaking ties on lower first executable ask.
- Forward paper entry is explicitly the first observed natural executable best ask after the PW call; settled spread results use the stored final game score plus the captured signed line. These are forward paper diagnostics, not live orders.
- Added regression tests for selected-team signed spread mapping, complementary-line inversion, and rejecting closed spread markets.
- Environment controls: `PW_SPREAD_CAPTURE_ENABLED=true`, `PW_SPREAD_CAPTURE_SAMPLE_SECONDS=1`, `PW_SPREAD_CAPTURE_WINDOW_SECONDS=300`.
- Natural sample counts will accumulate only when genuine future PW alerts arrive; no synthetic alert is injected into the live pipeline for validation.

## 2026-09-26 — Top portfolio value + total missed P/L stats (#114)

- Added two account-level cards to the top performance strip: `Portfolio value` and `Missed P/L · total`.
- Portfolio value is total wallet value: latest available USDC plus current Polymarket position value reported by the paired Termux executor heartbeat.
- Total missed P/L aggregates the existing missed-call accounting across Slam + Syndicate for both NFL and CFB. The card also shows the number of graded missed calls.
- These two cards are account/total metrics and intentionally do not change when the BOTH/PAPER/LIVE trade-performance filter changes.
- Positive missed P/L is green, negative missed P/L is red, and portfolio value remains neutral.
- Expanded the performance strip to five cards with responsive 5-column / 3-column / 2-column layouts.
- No execution, sizing, settlement, auto-trading, or risk-control logic changed.
## 2026-09-26 — Capper missed P/L tracking (#112)

- Added `Missed P/L` to every Slam/Syndicate NFL and CFB capper performance card.
- A missed call is a graded Telegram signal with no real execution record. Real executions are excluded even after they are sold or settled; paper-only executions still count as missed real trades.
- The dashboard shows both the number of graded missed calls and their net hypothetical P/L.
- Hypothetical missed P/L uses the configured $10-per-unit stake (or the sport-specific configured unit value) and the odds carried by the Telegram pick. If neither decimal nor American odds are present, the existing audit default of -115 is used. WIN earns stake × (decimal odds - 1), LOSS loses the stake, and PUSH is $0.
- CFB uses its existing persisted pick results, including final-score fallback grading, so already-finished untraded calls feed the missed-P/L total immediately.
- NFL signals now persist the parsed Telegram pick and periodically grade supported full-game moneyline/spread/total calls from completed ESPN NFL scores. This lets stale, failed, or otherwise unexecuted NFL calls populate missed P/L after the game finishes.
- NFL final-score lookup is throttled and caches scoreboard days within each grading pass to avoid repeated requests across signals.
- Added tests for -115 fallback math, posted decimal odds, exclusion of real trades, treatment of paper-only executions, NFL spread grading, and the CFB dashboard label.
## 2026-09-26 — Regrade unresolved closed CFB signals (#110)

- Production verification after #109 showed Northwestern +21 remained `EVENT_CLOSED` without `pick_result` because closed alternate-market records no longer entered the alternate refresh branch.
- The poller now sends every unresolved `EVENT_CLOSED` CFB record through the completed-game scoreboard fallback directly, even when no exact saved Polymarket market remains.
- Once WIN/LOSS/PUSH is persisted, later polls skip further grading work.
- Added a regression test for a persisted Northwestern +21 closed signal being upgraded to WIN with its final score.
## 2026-09-26 — Finished-game grading fallback + capper hide/sell controls (#108)

- Added a completed-game CFB grading fallback using ESPN final-score data when the exact original Polymarket market is no longer available, including legacy records that had previously rolled onto a later game.
- The fallback grades supported full-game moneylines, spreads, and totals from the original parsed pick and final score. It only persists a result when exactly one completed game matches the pick/date hints.
- Closed CFB alternate-line and invalid-future-match records now get a real WIN/LOSS/PUSH result when the final game can be identified. Final score/game context is shown on the finished card.
- Finished cards no longer show stale live BUY quotes, and invalid future-game records no longer present the unrelated future market as the matched/open market.
- Added a persistent Hide finished / Show finished toggle to the CFB capper panel.
- Added SELL POSITION on CFB cards only when an actual execution record is still open (`ORDER_SUBMITTED` or `PARTIALLY_CLOSED`). The button uses the existing authenticated Railway -> Termux sell queue and sells the tracked position, not an untraded signal.
- Added per-position NFL cards under each capper, with the same persistent Hide finished / Show finished toggle and SELL POSITION action for actual open NFL positions.
- Finished NFL position rows retain settlement result and realized P/L; open rows remain distinct and sellable.
- Signals that were never executed continue to show `Trade P/L — · NOT TRADED`; result grading does not create fictional betting P/L.
- Added regression coverage for Northwestern +21 (final-score WIN), Army moneyline (WIN), Temple +3.5 at a 21-17 final (LOSS), CFB sell availability, and NFL open/finished position serialization.
- Files changed: `app/cfb_capper_preview.py`, `app/nfl_capper_ingest.py`, `tests/test_cfb_capper_preview.py`, `tests/test_nfl_capper_ingest.py`, and `DEVELOPMENT_NOTES.md`.
## 2026-09-26 — CFB capper performance + finished result/P&L dashboard (#106)

- Added NFL-style performance summaries to each CFB capper card for Slam and Syndicate: bets, open positions, W-L-P, win rate, graded stake, realized P/L, and ROI.
- CFB aggregate performance is calculated from actual execution records tagged with the matching CFB capper/source; signal cards that were never bought do not create fictional betting P/L.
- Finished matched CFB signals now re-check the authoritative resolved Polymarket outcome token and display WIN, LOSS, or PUSH once resolution is available.
- Finished WIN rows use green styling, LOSS rows use red styling, PUSH rows use amber styling, and unresolved finished rows remain neutral/grey.
- Finished signal cards show actual trade P/L prominently when an execution exists. Signals that were tracked but never traded explicitly show `Trade P/L — · NOT TRADED`.
- The CFB poller continues checking an `EVENT_CLOSED` record until Polymarket publishes a resolved/settled token value; once WIN/LOSS/PUSH is recorded, the result is persisted and no longer needs repeated settlement lookup.
- Enlarged capper titles, status text, KPI text, and P/L emphasis for both NFL and CFB, including mobile layouts. Positive aggregate P/L is green and negative aggregate P/L is red.
- Generalized the existing NFL execution-stat helper so the same accounting rules can be reused safely for CFB while filtering by sport when that metadata is present.
- Files changed: `app/nfl_capper_ingest.py`, `app/cfb_capper_preview.py`, `tests/test_nfl_capper_ingest.py`, `tests/test_cfb_capper_preview.py`, and `DEVELOPMENT_NOTES.md`.
- Verification added for CFB sport-filtered performance accounting, authoritative token resolution, per-signal execution/P&L enrichment, no fabricated P/L on untraded signals, and finished-result dashboard styling.
- Deployment: implementation is on the task branch; production deployment follows merge to `main`.

## 2026-09-26 — CFB game-state colours (#104)

- CFB signal rows are now colour-coded consistently in every tab, including the combined Signals tab.
- Pregame/not-started events use a blue left rail/background and a PREGAME badge.
- Live events use a green left rail/background and a LIVE badge.
- Finished/closed events use a muted grey left rail/background and a FINISHED badge.
- Unmatched/retrying/unsupported signals without a confirmed event phase remain neutral rather than being given a misleading lifecycle colour.
- Closed rows also display `GAME FINISHED` in metadata. Existing BUY LIVE and alternate-line actions are unchanged.

## 2026-09-26 — Reject legacy CFB future-game rollovers (#101 follow-up)

- Production tracing confirmed the old Temple +3.5 signal had been attached to `cfb-templ-sfl-2026-10-03`, a later Temple game, after the original event disappeared from the open-event search.
- Existing stored one-team matches are now checked against the pick's original posted date on every runtime refresh.
- A stored event more than four days after (or more than one day before) the pick is treated as a legacy wrong-game match and cannot remain buyable.
- The bot first attempts to recover the correct nearby exact market; for spread picks it can fall back to explicit nearby alternate lines.
- If no valid nearby open event remains, the signal moves to `EVENT_CLOSED` with `INVALID_FUTURE_MATCH` rather than carrying the pick into a later game.
- This migration also repairs already-persisted bad matches from before the stricter date guard was deployed.

## 2026-09-26 — Finished CFB lifecycle revalidation (#101)

- Fixed finished games such as Temple remaining under Pregame.
- Polymarket Gamma event lifecycle is now read from the SDK's real nested fields: `event.state.closed/ended/live`, `event.schedule.start_time/finished_at/closed_time`, and `event.sports.game_status`.
- Exact matched signals now refresh the original stored event by its exact Gamma slug every poll, in both Pregame and Live states. The bot no longer waits for a signal to become Live before re-checking closure/final state.
- Event refresh locates the exact stored outcome token on that same event, so lifecycle refresh cannot silently jump to another game.
- `ended`, `closed`, a passed `finished_at`, a final game status, or a non-accepting market all classify the signal as Closed and remove manual BUY availability.
- One-team picks now enforce posted-date proximity even when only one open candidate event exists. An old Temple pick cannot roll forward onto a later Temple game after its original event closes.
- Full-game spread alternatives use the same one-team date protection.

## 2026-09-26 — Structured CFB spread-side resolution (#98)

- Fixed live CFB spread matching for Polymarket Gamma markets where the selected team is the complementary outcome of the displayed spread.
- Verified production metadata for Northwestern vs Indiana uses forms such as `Spread: Indiana (-20.5)` with outcomes `[Indiana, Northwestern]`; the Northwestern outcome is therefore interpreted explicitly as `+20.5`.
- The resolver now maps the question's named spread team to exactly one outcome and inverts the structured line only when the selected team is unambiguously the other outcome.
- Structured `sports.line` must agree with the signed line in the question; mismatches fail closed.
- Exact spread matching and live-alternate matching now share the same selected-team effective-line logic.
- Full-game CFB spread/total/moneyline matching now requires full-game sports market types. Second-half and quarter spreads such as `second_half_spreads` and `q3_spreads` cannot satisfy a full-game Telegram pick.
- Removed the temporary verbose candidate diagnostic logging after confirming Gamma's representation.
- This enables an original Northwestern +21 signal to surface explicit live +20.5/+21.5 alternatives when +21 itself is unavailable, without silently changing the wager.

## 2026-09-26 — Explicit CFB live spread alternatives (#96)

- If an exact tracked CFB spread is no longer listed, the signal is kept visible and the bot discovers nearby open spread markets for the same team/event.
- Alternate spread buttons are explicit: they show the actual current line, executable best-ask cents, American-odds equivalent, and whether the line is better/worse than the original pick.
- The original wager is never silently changed. An alternate is bought only after the user clicks that specifically labeled line.
- Alternate discovery is fail-closed: it requires an explicit signed spread tied to the selected team and a unique same-date CFB event.
- Each alternate is re-resolved and re-quoted again at click time. If the line has disappeared, the click is rejected and the dashboard must be refreshed.
- Existing Termux online/geoblock, per-trade cap, daily budget, MAX_PRICE, MAX_SPREAD, duplicate/open-position, and exact asset-token guards remain in force.
- Alternate orders retain the original strategy pick id/selection for dedupe/audit and also record the executed alternate line.
- Pregame/Live tab counts include signals with explicit alternatives even when the original exact spread is unavailable.

## 2026-09-26 — CFB status tabs and current-slate recovery (#94)

- Each Slam/Syndicate CFB card now exposes viewable tabs for Signals, Queued, Done, Failed, Retrying, Pregame, Live, Closed, and Unsupported instead of flattening all status counts into one block.
- Every tab has its own item list; the Signals tab shows the most recent records across all states and preserves each record's status.
- The selected tab is preserved while the dashboard refreshes, so a 10-second status refresh does not kick the user back to Signals.
- Existing BUY LIVE / OPEN MARKET actions continue to render inside Pregame and Live tabs when the record is actionable.
- The CFB bridge lookup window is now configurable with `CFB_CAPPER_FEED_WINDOW_MINUTES` and defaults to 1440 minutes (24 hours), with a 300-post request limit.
- The 24-hour scan window is separate from the 180-second unattended auto freshness gate. Older same-slate picks can therefore be recovered and shown pregame/live without becoming unattended auto entries.
- Dashboard metadata now shows the active scan window.

## 2026-09-26 — Keep CFB signals actionable during live games (#92)

- Matched CFB signals now transition from `MATCHED_PREGAME` to `MATCHED_LIVE` after kickoff while the matched Polymarket market remains open.
- Live signals remain visible in a dedicated “Matched live picks” section and continue refreshing executable best-ask pricing and American-odds equivalents.
- `BUY LIVE` remains available during the game while the matched market is still open; the click still refreshes the exact token’s current best ask/spread before queueing the order through Termux.
- The dashboard shows `GAME LIVE` plus the current quote instead of treating kickoff as an expired signal.
- During live games, the resolver also re-checks that the exact matched Polymarket market remains open. A confirmed absence of the exact open market or `accepting_orders=False` moves the record to `EVENT_CLOSED`.
- Closed markets remain visible in counts but are no longer actionable.
- Legacy `EVENT_STARTED` rows from the prior deployment are migrated automatically to `MATCHED_LIVE` on the next poll when the market is still open.
- The 180-second Telegram freshness gate remains limited to unattended handling; it does not remove pregame or live manual actions.

## 2026-09-26 — Keep matched CFB picks pregame and show live odds (#90)

- The 180-second CFB freshness window now gates only unattended preview/auto handling. It no longer labels a valid pregame pick as stale.
- Older matched picks move to `MATCHED_PREGAME` and remain manually actionable until the matched event start time or market closure.
- The dashboard now shows signal age separately instead of using “stale” as a game-status label.
- Active matched CFB rows refresh the Polymarket executable BUY quote every poll cycle, including current best ask, spread, and an American-odds equivalent.
- Dashboard rows show live odds in the form `Live BUY 58¢ (-138)`.
- Exact event start metadata is persisted when Polymarket provides a timestamp; date-only metadata is deliberately not treated as midnight kickoff.
- Manual BUY is blocked once the matched event has started or closed, including a second lifecycle check at click time.
- Older stored `IGNORED_STALE` records are migrated to matched pregame on the next runtime refresh when the event is still pregame.

## 2026-09-26 — Match CFB picks before exposing BUY (#88)

- Tracked Slam/Syndicate CFB picks now resolve to an exact Polymarket event, market, outcome, and token before the dashboard exposes a live-buy action.
- Supported stale picks are matched first and only then marked stale, so stale/manual rows already know their exact Polymarket event.
- Persisted match metadata includes the event slug/title, market URL/question, outcome, market type, and exact asset token.
- Manual BUY uses the persisted token/event and refreshes only the executable price, spread, and order book at click time; it does not rediscover the game on click.
- Dashboard rows show the matched market/outcome plus an OPEN MARKET link. BUY LIVE is shown only when a safe persisted match exists.
- Matched signals whose preview path is retrying (for example because the automatic-preview gate is unavailable) remain manually actionable.
- Existing old stale records without match metadata are backfilled on the next CFB poll instead of being permanently skipped.

## 2026-09-26 — Resolve one-team CFB manual buys to the correct dated game (#86)

- Fixed `BUY LIVE` for one-team CFB signals such as Clemson when Polymarket exposes more than one open/future event for that team.
- The CFB resolver now uses the original pick `posted_at` plus structured event dates or the dated CFB event slug/title to narrow to one nearby game.
- Safety remains fail-closed: only events from one day before through four days after the pick date are eligible for this date disambiguation, and ties/unknown dates still block instead of guessing.
- A week-later game cannot inherit an old stale pick.
- Added regression tests for selecting the current Clemson event and rejecting unrelated future events.

## 2026-09-26 — Manual CFB BUY LIVE button (#84)

- Added a per-signal **BUY LIVE** button for CFB queued-preview, completed-preview, and stale signals in the dashboard.
- The click is treated as explicit manual authorization and does **not** enable or depend on `AUTO_TRADING`; unattended CFB remains preview-only.
- Each click re-resolves the original sanitized pick against the current Polymarket CFB event/market, refreshes BUY price, best ask and spread, and then enqueues a Termux `BUY` with `auto=false`.
- Existing dashboard per-order cap, daily budget, exact outcome token, max price/spread, executor heartbeat, Termux geoblock, BUY TTL and phone emergency ceiling remain enforced.
- Duplicate queued/open manual buys for the same CFB signal are blocked.
- Signal state tracks the manual request separately from the preview/stale classification so the dashboard can show PENDING/LEASED/DONE/FAILED without converting the CFB worker to automatic trading. Completed PREVIEW signals stay visible/actionable because PREVIEW execution normally finishes quickly.

## 2026-09-25 — One-shot CFB capper PREVIEW validation (#74)

- Added `CFB_CAPPER_TEST_PREVIEW_JSON` for deterministic end-to-end CFB validation without waiting for a Telegram post.
- The test payload receives a fresh runtime timestamp, runs the same CFB classification, unit sizing, event/market resolution, price/spread and cap checks, then queues only Termux `PREVIEW`.
- The trigger is idempotent by payload hash, persists its marker under `/app/data`, waits for the paired executor to reconnect, and logs the terminal executor result.
- No CFB test path can enqueue `BUY`.

## 2026-09-25 — SLAM/Syndicate CFB Telegram preview pipeline (#72)

- Added a College Football worker that polls the sanitized `/public/ncaaf` bridge every 15 seconds with a 180-second freshness cutoff.
- Tracks SLAM and The Syndicate separately as `Slam - CFB` and `Syndicate - CFB`.
- Uses the same unit convention as NFL: 1u = $10 USDC; explicit units above 1 scale the preview stake.
- Supports full-game moneyline, spread, and total previews only. Period/half/quarter, team totals, props, missing matchup context, and ambiguous event matches fail closed.
- Resolves only Polymarket College Football events with `cfb-` slugs, exact market type/line/outcome, live best ask and spread checks, existing per-order cap, and daily-budget guard visibility.
- Sends only `PREVIEW` to the Termux executor. This worker contains no path that enqueues `BUY`.
- Saves preview state/dedupe records under `/app/data` and exposes authenticated status at `/api/cfb-cappers/status`.

## 2026-09-25 — Fix NFL event discovery by team aliases (#70)

- **Incident:** Safe PREVIEW diagnostics for SLAM `UNDER 43.5` returned zero candidate events for `title_search=Atlanta Falcons`, while the live Polymarket event is titled `Falcons vs. Packers`.
- **Fix:** Event discovery now tries the team's full name and configured aliases/nicknames, deduplicates results, and only considers NFL-slug events before the existing expected-team and exact-market checks.
- **Safety:** Alias search only broadens discovery; it does not bypass NFL event validation, expected-team matching, exact market type/line/outcome checks, price/spread caps, or Termux validation.
- **Tests:** Added regression coverage where the full-name search returns no event but the nickname search (`Falcons`) resolves the correct NFL event.

## 2026-09-25 — Match NFL totals using structured Polymarket line (#64)

- **Incident:** A production dry-run of SLAM `UNDER 43.5` failed exact-market resolution even though Falcons-Packers U43.5 remained active on Polymarket.
- **Root cause:** The matcher tried to recover the total line from market question/slug text and ignored the SDK's structured `market.sports.line` field. Sports questions/slugs can encode the threshold differently from the displayed decimal line.
- **Fix:** Total-market matching now compares the requested line against `market.sports.line` first when present, falling back to the existing text matcher only when structured line metadata is absent.
- **Safety:** A structured line mismatch fails closed; the change does not weaken source, freshness, market-type, price/spread, cap, token, or Termux validation.
- **Tests:** Added exact-match and wrong-line regression coverage where the market text omits `43.5` but structured line metadata is present.

## 2026-09-25 — Harden Termux executor connectivity and supervision (#62)

- **Incident:** The NFL dry-run showed no Termux heartbeat/`next` traffic reaching Railway. There were no 401 responses, so the server was not rejecting the saved pair token; the phone worker simply was not reaching the bridge.
- **IPv4 bridge transport:** Railway executor bridge calls (pair, heartbeat, request polling, and result delivery) now use an IPv4-bound `httpx` transport with retries. This mirrors the existing IPv4-only Polymarket geoblock path used for mobile networks with unreliable IPv6 routing.
- **Supervisor launcher:** Added `scripts/start_termux_executor.sh` with `termux-wake-lock`, crash restart supervision, persistent supervisor logging, and a fail-stop on executor-token rejection instead of an auth retry loop. It continues to run `scripts/termux_executor_v2.py` and preserves the existing token/env locations.
- **Local cap compatibility:** `EXECUTOR_MAX_USDC` remains accepted as the fallback emergency ceiling when the newer `EXECUTOR_EMERGENCY_MAX_USDC` is not set, so the existing phone `$50` local cap remains effective.
- **NFL preview handoff:** The one-shot synthetic NFL preview now runs as a separate background task and waits for the paired executor to reconnect without blocking the normal NFL feed poller. A failed previous test marker may retry; a queued/done marker remains idempotent.
- **Tests:** Added coverage that the Railway bridge client binds to IPv4 with retries.

## 2026-09-25 — One-shot NFL capper dry-run preview (#58)

- Added an environment-triggered one-shot production dry-run for the NFL Telegram capper path.
- The synthetic pick runs the same source allowlist, market classification, unit sizing, exact Polymarket market resolution, dashboard cap, daily budget, current price, spread, and Termux executor readiness checks as a live capper signal.
- The dry-run can enqueue only `PREVIEW`; it never enqueues `BUY`. Termux `PREVIEW` independently revalidates the exact token, market type, geoblock, price/spread, and minimum order size and returns `no_order_placed: true`.
- A persistent marker in `/app/data` makes the trigger one-shot across restarts. The request and terminal executor result are logged for verification.
- Regression coverage asserts a 2u total produces a $20 PREVIEW payload and never a BUY action.

## 2026-09-25 — Manual sports limit orders through Termux (#54)

- **Incident:** The authenticated manual executor endpoint still rejected every non-moneyline BUY and `_buy_payload()` overwrote the requested market type with `moneyline`, even though the Termux executor already supports exact-token spread and total validation.
- **Fix:** `/api/executor/request-preview` and `/api/executor/request-buy` now support only the existing sports market types `moneyline`, `spread`, and `total`, and preserve the requested type in the queued payload.
- **Dashboard:** The manual panel now includes a market-type selector and uses the live Railway/dashboard Auto trade cap reported by `/api/executor/status` instead of hard-coding a $5 UI ceiling for remote Termux orders.
- **Preserved safeguards:** Dashboard authentication, sports-only URL validation, dashboard amount cap, global max price, max spread, executor heartbeat, BUY TTL, Termux geoblock check, exact market type/outcome validation, minimum size, and local emergency ceiling remain in force.
- **Tests:** Added regression coverage for total/spread payload preservation, unsupported market-type rejection, the three-option UI selector, and dynamic dashboard-cap propagation.

## 2026-09-25 — Resolve NFL totals from one known team safely (#52)

- **Incident:** A SLAM 2u `UNDER 43.5` was parsed as a valid full-game total, but the source post exposed only Atlanta/Falcons context rather than both matchup teams, so the bot rejected it before Polymarket resolution.
- **Bridge contract:** The Telegram bridge may now provide one recognized NFL team on a bare game total when that is the only team mentioned in the same NFL post.
- **Eligibility:** Full-game totals require at least one recognized NFL team plus an explicit OVER/UNDER side and exact total line. Zero-team totals remain unsupported.
- **Fail-closed resolution:** For a one-team total, Polymarket must produce matching exact-line candidates from exactly one NFL event. If two different events involving that team match the exact line, unattended execution is blocked instead of choosing by rank.
- **Preserved safeguards:** The 180-second freshness window, source allowlist, unit sizing, dashboard cap, daily budget, price/spread checks, exact token handoff, Termux validation, and geoblock checks are unchanged.
- **Tests:** Added one-team eligibility, zero-team rejection, unique-event resolution, and multi-event ambiguity regression coverage.

## 2026-09-25 — Log Telegram bridge freshness error (#50)

- NFL feed diagnostics now include the bridge's sanitized freshness error and resolved channel labels.
- Feed change detection includes the freshness state so a changing reconnect/auth failure is surfaced even when pick counts remain zero.
- No Telegram credentials, raw messages, or execution behavior are logged or changed.

## 2026-09-25 — Log NFL feed and terminal ingest decisions (#48)

- Logs a sanitized NFL feed summary only when the feed content changes, avoiding 15-second log spam.
- Logs parsed pick metadata and metadata-only unparsed-post diagnostics; raw Telegram message text is not logged.
- Emits one-time `NFL_CAPPER_SIGNAL` records for stale and unsupported picks, in addition to the existing queued/retrying/untracked-source logs.
- Feed signatures deliberately exclude generated timestamps, so identical feed content does not produce repeated logs.
- No execution, sizing, freshness, market-resolution, or risk-limit behavior changed.

## 2026-09-25 — Persist last NFL bridge diagnostics (#46)

- Saves the latest sanitized /public/nfl response to /app/data/nfl_capper_last_feed.json on every successful poll.
- Captures counts, listener/freshness metadata, safe unparsed-post diagnostics, and parsed picks so zero-pick failures can be diagnosed after the fact.
- The public bridge contract does not include raw Telegram message text, so this file does not persist raw channel content.
- No execution, sizing, risk-limit, or freshness behavior changed.

## 2026-09-25 — Fix NFL Telegram source identity matching (#44)

- **Root cause:** The Telegram bridge stores/serves a channel username when Telegram exposes one, but the NFL auto-trader previously required exact display titles (`SLAM - All Access` / `The Syndicate`). Valid capper picks could therefore be dropped before any signal record or executor request was created.
- **Fix:** Source matching now resolves exact legacy titles, canonical bridge `source_key` values, normalized Slam/Syndicate username variants, and optional stable Telegram `source_id` allowlists.
- **Auditability:** The capper now persists every parsed NFL feed pick. Non-target analysts are recorded as `IGNORED_UNTRACKED_SOURCE` instead of disappearing silently, and queue/retry/source-ignore decisions emit explicit `NFL_CAPPER_SIGNAL` / `NFL_CAPPER_SOURCE_IGNORED` logs.
- **Bridge compatibility:** New bridge payloads expose `source_id` and `source_key`; older payloads remain supported through normalized aliases.
- **Safety:** Only Slam and Syndicate map to live-trade source labels. Blacksmith and unknown channels remain ignored.
- **Tests:** Added exact-title, username-variant, canonical-key, and unknown-source regression coverage.

## 2026-09-24 — Make dashboard auto-trade cap authoritative for Termux (#41)

- **Objective:** Remove the need to keep a normal per-order cap synchronized separately on the Android/Termux executor.
- **Authoritative cap:** Railway/dashboard `MAX_AUTO_TRADE_USDC` is now re-checked when an eligible BUY/PREVIEW is handed to Termux. A queued BUY that was valid when created but is above a subsequently reduced dashboard cap is failed before executor pickup.
- **Trusted handoff:** Railway stamps `authorized_max_auto_trade_usdc` into the executor payload at handoff. The original caller cannot make an oversized queued BUY executable by supplying its own stale/higher authorization value because the server overwrites the value before lease.
- **Phone validation:** Termux requires the server-stamped dashboard cap and independently verifies `budget_usdc <= authorized_max_auto_trade_usdc` before any market/order work.
- **Emergency backstop:** The old everyday `EXECUTOR_MAX_USDC` gate is replaced by `EXECUTOR_EMERGENCY_MAX_USDC`, default $500. This is intentionally a high local catastrophe ceiling, not a second operational cap that must track the dashboard.
- **Preserved safeguards:** Existing executor pairing/heartbeat, geoblock, BUY TTL, daily budget, exact market/token, price, spread, order-book and minimum-size checks remain unchanged.
- **Tests:** Added queue handoff coverage for server cap stamping and for blocking a queued BUY after the dashboard cap is reduced. Termux budget-cap logic is isolated so the server authorization and emergency ceiling are explicit/fail-closed.
- **Deployment:** No merge or production deployment in this branch.

## 2026-09-24 — Auto-trade Slam/Syndicate NFL picks with unit sizing (#33)

- **Objective:** Automatically buy new NFL game-market plays posted by SLAM - All Access and The Syndicate on Polymarket and maintain separate bot performance records as Slam - NFL and Syndicate - NFL.
- **Sizing:** Default/missing/<=1 posted unit = 1 unit = $10 USDC. Explicit posted units above 1 scale linearly (for example 1.25u = $12.50, 2u = $20, 3u = $30), subject to the existing dashboard auto-trade cap, daily budget, price/spread limits, executor availability, and Termux local cap.
- **Ingestion:** app/nfl_capper_ingest.py polls the sanitized Telegram bridge NFL feed every 15 seconds by default. Only picks from Slam/Syndicate are considered. Persistent SHA-256 fingerprints prevent duplicate buys across polling cycles and restarts.
- **Freshness:** Only picks posted within NFL_CAPPER_MAX_PICK_AGE_SECONDS (default 180 seconds) can enter execution. Older bridge/backfill picks are permanently marked stale and never bought. Transient preparation failures can retry only while the pick remains fresh.
- **Market scope:** Automatic execution supports NFL full-game moneyline, spread, and total plays. Player props, period/half/quarter markets, mixed/ambiguous markets, and verbose commentary-like parser output are fail-closed and recorded as unsupported rather than guessed.
- **Market matching:** Railway identifies the exact NFL event, line, side, and Polymarket outcome token. For binary total markets, Under correctly maps to NO when the market question is phrased as Over (and vice versa). Termux independently validates that exact outcome token, market type, price, spread, minimum order size, sports URL, and geoblock immediately before placing the order.
- **Execution path:** Real orders continue through the existing Railway → Termux executor path. The manual live-test endpoint remains moneyline-only. scripts/termux_executor.py now permits exact-token automated spread/total payloads in addition to moneyline and defaults its independent local cap to $25 unless EXECUTOR_MAX_USDC is set.
- **Tracking:** Executor fill records preserve strategy_source, NFL sport, posted units, unit value, source message timestamp/selection, and signal fingerprint. The dashboard exposes a dedicated NFL capper panel and /api/nfl-cappers/stats with bets, open positions, W/L/pushes, stake, realized P/L, ROI, and win rate for each capper.
- **Settlement:** Closed sports markets resolving 1/0 continue to grade WIN/LOSS. A resolved 0.50/0.50 market now grades SETTLED_PUSH, which is required for pushed NFL spread/total markets.
- **Files changed:** app/nfl_capper_ingest.py, app/wnba_pw_research_v13.py, app/termux_executor_dashboard.py, scripts/termux_executor.py, app/slack_ingest.py, .env.example, tests/test_nfl_capper_ingest.py, DEVELOPMENT_NOTES.md.
- **Verification:** Unit coverage added for $10 unit sizing, explicit multi-unit sizing, prop/period/ambiguous rejection, dedupe fingerprints, binary total side mapping, separated capper performance stats, and push settlement semantics. Existing executor queue/live safety behavior remains in place.
- **Deployment:** No production or infrastructure change made in this branch. Merge/deploy remains a separate user-controlled step.
- **Operational note:** A posted multi-unit play larger than the current MAX_AUTO_TRADE_USDC, daily budget, or local EXECUTOR_MAX_USDC is blocked rather than silently reduced. Those caps must be set high enough if the user wants the full posted unit size executed.

## 2026-09-24 — Preserve Termux executor pairing across Railway deploys (#38)

- **Incident:** The production Railway service used a custom start command that ran `rm -f /app/data/termux_executor_state.json` before every app start.
- **Impact:** `termux_executor_state.json` is stored on the persistent Railway volume and contains the paired executor token hash/state. Deleting it forced the Android Termux executor back to `NOT PAIRED` after every deployment.
- **Production config fix:** Railway start command changed to `sh -lc 'exec /app/start.sh'`; the pairing-state file is no longer intentionally removed at startup.
- **Deployment nuance:** A Railway `redeploy` can reuse the previous deployment snapshot, so a fresh source deployment is required to ensure the corrected start command is actually used.
- **Verification target:** New source-deployment logs must not contain `EXEC_STATE_BEFORE`, `EXEC_STATE_AFTER`, or an `rm -f` deletion of the pairing file.
- **User action:** Because the pairing file had already been deleted before this fix, Android/Termux must pair one final time. Subsequent deploys should retain pairing.
- **Unchanged:** No trading authorization, order routing, sizing, risk, wallet, or strategy logic changed.

## 2026-09-24 — Migrate existing Stats preference to LIVE (#36)

- **Issue:** Browsers that had previously saved `dashboardStatsFilter=both` kept showing BOTH after the new LIVE fallback shipped.
- **Fix:** Added a versioned one-time localStorage migration. On first load after this release, the saved Stats mode is set to LIVE and the migration version is recorded.
- **After migration:** Any later manual Stats selection is preserved normally because the migration does not repeat for the same version.
- **Unchanged:** Open trades still defaults to BOTH. No trading, execution, routing, sizing, risk, wallet, or order behavior changed.

## 2026-09-24 — Default dashboard Stats view to LIVE (#34)

- **Objective:** Make the Trading Bot Dashboard open the Stats performance view on LIVE data by default.
- **Behavior:** When no `dashboardStatsFilter` preference exists in browser localStorage, Stats now defaults to `live`. An explicit saved BOTH or PAPER selection is still respected.
- **P/L alignment:** The Portfolio P/L card/chart uses the same LIVE fallback so its initial label and values match the Stats selection.
- **Unchanged:** Open trades still defaults to BOTH. No execution, trading, risk, sizing, routing, or wallet behavior changed.
- **Tests:** Added `tests/test_dashboard_default_stats_mode.py`.

## 2026-09-23 — Executor offline/stale BUY protection (#29)

- Incident: automatic LIVE BUYs were left PENDING while the Termux executor was offline.
- Root cause: automatic BUY creation did not require a live executor heartbeat and queued BUYs had no expiry.
- Fix: unattended BUYs now fail closed when Termux is offline/geoblocked.
- Queued BUYs expire after EXECUTOR_BUY_TTL_SECONDS, default 180 seconds.
- Expired BUYs cannot be picked up later when the executor reconnects.
- Active leases are preserved; late executor results remain reconcilable.
- Stale queue entries are expired on startup and before executor pickup/status display.
- No change to SELL handling, stake sizing, market filters, or Polymarket credentials.
- Tests: executor queue safety tests, runtime switch tests, compileall, diff check.
- Issue: #29.

## 2026-09-21 — PW direct-feed recovery + Slack failover (#23)

- **Incident:** A WNBA PW call was present in NBA Monitor history but never reached the Railway trading bot. Railway logs during the game showed continuous `PW_EXPORT_POLL_ERROR` TLS handshake timeouts to the private WNBA `/api/pw-export` endpoint.
- **Primary feed:** The trading bot receives PW calls directly from NBA Monitor `/api/pw-export`; Slack is not required for the primary path.
- **Transport fix:** The Tailscale outbound proxy connection now prefers the configured MagicDNS hostname and falls back to the peer IP, instead of forcing the peer IP for every HTTPS CONNECT. Each poll uses a fresh TLS tunnel so a stale connection cannot poison later polls.
- **Research sync:** The historical/research export sync reuses the same hardened transport.
- **Health:** Direct feed state now records `last_success_at`, `last_route`, and `consecutive_errors`.
- **Slack backup:** Existing `POST /slack/events` remains an independent second ingestion path. Cross-source PW deduplication was added so the same call arriving through both direct export and Slack cannot create two trades.
- **Slack Events callback:** `https://polymarket-auto-bot-production.up.railway.app/slack/events`. Slack Event Subscriptions must send message events for the WNBA alert channel to this URL, and Railway `SLACK_EVENTS_ENABLED=true` with the matching signing secret/channel ID.
- **Notification behavior:** NBA Monitor continues to send PW alerts to Slack for human notification. The bot's direct feed and Slack backup are independent delivery paths.
- **Safety:** No PW strategy filters, trade sizing, auto-trading authorization, or risk rules were changed.

## 2026-09-21 — Pregame-spread resting limit-order backtest (#20)

- **Objective:** Test every stored graded WNBA PW call as a hypothetical resting Polymarket BUY limit order on the exact stored pregame spread (`alerts.handicap`) for the PW-predicted team.
- **Limit levels:** Decimal 1.90, 1.95 and 2.00, corresponding to maximum share prices 52.632c, 51.282c and 50.000c.
- **Order lifetime:** From the PW fire timestamp through the final recorded play-by-play timestamp for that game.
- **Fill model:** First historical spread-token `prices-history` point at or below the limit price. This is an indicative/proxy fill test because archived prices-history is approximately minute fidelity and does not reconstruct historical executable ask/depth.
- **Settlement:** Uses the resolved Polymarket spread token outcome. Threshold-price P/L is reported conservatively at the submitted limit odds; first-observed proxy-price P/L is also reported separately.
- **Reports:** All PW calls as the primary sample, plus first-call-per-game/team deduplication to remove repeat-call correlation; includes coverage, fill rate, unique games, W/L/pushes, win rate, unit P/L, ROI, time-to-fill and quarter breakdowns.
- **Implementation:** `app/pw_spread_backtest.py` now reads `alerts.handicap`, extends spread-token historical price capture through game end, locates the exact listed Polymarket pregame spread market, and emits `PW_PREGAME_LIMIT_BACKTEST`.
- **Safety:** Research/reporting only. No live order placement, cancellation, sizing, routing, auto-trading, risk gates or execution controls changed.
- **Completed production research run:** 1,921 stored PW alerts; 1,362 graded/backtest-eligible. 841 graded calls had a stored pregame spread; 642 had an exact matching listed Polymarket pregame-spread market; 638 had sufficient post-signal price history + resolved settlement for the limit test.
- **All-call results — 1.90 limit (52.632c):** 368/638 proxy fills (57.68%) across 51 games, 163-205, 44.29% win rate. Conservative threshold-fill P/L -58.30u / -15.84% ROI. First-observed historical-price proxy P/L -5.00u / -1.36% ROI. Median fill 179s; average 463.9s.
- **All-call results — 1.95 limit (51.282c):** 357/638 fills (55.96%) across 51 games, 156-201, 43.70% win rate. Threshold P/L -52.80u / -14.79% ROI. First-observed proxy -1.92u / -0.54%. Median fill 207s; average 536.5s.
- **All-call results — 2.00 limit (50.000c):** 337/638 fills (52.82%) across 49 games, 136-201, 40.36% win rate. Threshold P/L -65.00u / -19.29% ROI. First-observed proxy -18.25u / -5.42%. Median fill 212s; average 533.6s.
- **Dedup first PW call per game/team:** 223 first-side calls; 116 had stored pregame spread, 90 exact/evaluable. At 1.90: 70 fills, 31-39, threshold -11.10u / -15.86%; first-observed proxy +0.12u / +0.17%. At 1.95: 68 fills, 30-38, threshold -9.50u / -13.97%; first-observed proxy +1.57u / +2.31%. At 2.00: 65 fills, 27-38, threshold -11.00u / -16.92%; first-observed proxy -0.53u / -0.82%.
- **Quarter note:** Q2 was positive in the all-call threshold model (+18.75% / +21.88% / +25.00% at 1.90/1.95/2.00) but only 8 fills at each level, so it is not a robust standalone finding. Q1, Q3 and Q4 were negative at threshold execution.
- **Interpretation:** The strong historical pregame-spread ROI shown in NBAMonitor does not carry over to a strategy that waits for the exact pregame spread to reach 1.90-2.00 after a PW signal. The conservative limit-price model is negative at all three thresholds. The first-observed-price proxy is near flat overall and mildly positive only in the deduplicated 1.95 sample, but historical prices-history is midpoint/proxy data rather than executable ask/depth, so that +2.31% is not evidence of an executable edge.
- **Run:** Railway deployment `1fcdcd08-aeb0-47bc-8b90-ea3eef82dc83`; `PW_SPREAD_BACKTEST_ENABLED` was returned to `false` immediately after completion.

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


## 2026-09-19 — PW play-by-play scalping research (#12)

- **Objective:** Join WNBA PW calls to play-by-play and evaluate short-horizon momentum/scalping behavior without touching execution.
- **Research-only analyzer:** Added `app/pw_scalping_research.py`, wired through `app/wnba_pw_research_v13.py`.
- **Integrity correction:** A prior research sync imported 1,464 PW-server historical rows after startup, temporarily inflating the canonical database from 1,921 to 3,385 alerts. The sync is now protected with `PW_RESEARCH_IMPORT_ENABLED=false` by default and explicitly in Railway.
- **Cleanup:** Migration v4 removed the 1,464 research-import alerts (v3) and then removed 61,448 orphan play-by-play rows left from games that were no longer referenced. Canonical startup is restored to 1,921 PW alerts / 1,362 graded.
- **Current play-by-play:** 60,959 events across 151 canonical WNBA games. Note: the current stored PBP rows are sourced from ESPN via the research sync; the private PW server provides the PW calls. No separate PW-server-native PBP endpoint is configured in this repository.
- **Alignment:** 1,344 of 1,362 graded PW calls aligned to a PBP state (98.7%).
- **All aligned calls:** next-120s average PW-side score delta -0.282; average 120s MFE +1.939 and MAE -2.400. Within 180s, +4 favorable occurred before -4 adverse on 27.7% of calls versus 35.5% adverse-first. A -2 adverse move occurred on 66.8%; among those, 61.0% recovered to the original score and 24.9% reached +2 versus the call score within the same window. Final win rate after a -2 dip was 66.4% versus 71.6% overall.
- **Late Q3 (last 3m):** 181 calls; average 120s delta -0.171; +4 first 31.5%, -4 first 38.1%; -2 dip 66.3%; final win 69.1%.
- **Early Q4 (first 3m):** 156 calls; average 120s delta -0.641; +4 first 30.1%, -4 first 42.9%; -2 dip 72.4%; final win 68.6%. This is the clearest short-term reversal/fade window in the initial study.
- **First vs repeated same-side PW calls:** first calls (223) had average 120s delta +0.036 and final win 60.5%; repeated calls (1,121) had average delta -0.345 but final win 73.8%. Repeated calls appear stronger for final direction but weaker for immediate same-direction scalp momentum.
- **Current top-four filter PBP behavior:**
  - Away Underdog: 148 aligned calls; delta120 -0.574; +4 first 25.0%; -4 first 44.6%; -2 dip 72.3%; final win 26.4%.
  - Plus-money: 308; delta120 -0.351; +4 first 25.3%; -4 first 38.6%; -2 dip 69.5%; final win 34.4%.
  - Q3 Away: 241; delta120 -0.191; +4 first 25.7%; -4 first 37.8%; -2 dip 69.3%; final win 60.6%.
  - Home Underdog: 160; delta120 -0.144; +4 first 25.6%; -4 first 33.1%; -2 dip 66.9%; final win 41.9%.
- **Interpretation:** The initial PBP study supports testing (1) short fade scalps immediately after selected PW calls, especially early Q4 / Away Underdog; and (2) a delayed PW-side entry after a short adverse move, particularly on repeated same-side calls. These are score-movement proxies, not measured Polymarket P/L.
- **Limitation:** True scalp ROI/P&L requires historical Polymarket price ticks or a forward price recorder. Score movement alone cannot establish executable market returns, spreads, or slippage.
- **Commits:** `30b2d53`, `4231bdc`, `c1a247d`, `21efa5c`, `6ff6d06`.
- **Verified deployment:** `70d0f960-5489-4a34-8ba5-e7bffa902d7d` SUCCESS.


## 2026-09-19 — Polymarket tick monitoring + PW scalp price backtest (#13)

- **Objective:** Add executable live market monitoring around genuine WNBA PW calls and historical Polymarket price backtesting for scalp hypotheses.
- **Safety:** Research-only. No order placement, cancellation, position resizing, or trade-closing path was added.
- **Live recorder:** Added `app/pw_market_research.py` and wired it after the existing WNBA/PW research stack.
- **Railway settings:** `PW_MARKET_RESEARCH_ENABLED=true`, `PW_MARKET_SAMPLE_SECONDS=1`, `PW_MARKET_WINDOW_SECONDS=300`, `PW_MARKET_HISTORY_BACKTEST_ENABLED=true`.
- **Per-call live fields:** PW/game metadata, resolved asset/token, executable BUY and SELL price, midpoint, spread, best bid/ask and top sizes, event/market IDs, strategy attribution, same-game/side PW call number, and available game state.
- **Repeat-call correction:** The recorder derives same-side call number from the canonical PW alerts table, so calls that occurred before the recorder deployment are included.
- **Storage:** Added durable SQLite tables `pw_market_watches`, `pw_market_ticks`, `pw_market_history_map`, and `pw_market_history_points`.
- **Research endpoints:** `/api/pw-market-research/status`, `/api/pw-market-research/backtest`, and `/api/pw-market-research/backtest/recompute`, all behind dashboard authentication.
- **Historical market mapping:** 223 canonical game/side combinations were considered; 169 currently map to resolved Polymarket moneyline tokens and 54 exhausted the current slug/alias lookup.
- **Historical price coverage:** 1,018 of 1,362 graded PW calls had usable mapped historical Polymarket price data. Historical price requests completed with zero price-fetch errors for the mapped tokens.
- **Method:** Historical results use Polymarket price-history midpoint/proxy data at approximately one-minute fidelity. They are not executable bid/ask fills and do not include live spread/slippage. $100 is treated as the stake for each simulated entry.
- **Early-Q4 correction:** The fade backtest now joins each Q4 PW call to exact-score play-by-play and restricts the strategy to the first three minutes of Q4 (10:00 through 7:00 remaining). It does not use the entire fourth quarter.
- **Grid searched:** TP +3c/+5c/+8c; SL -3c/-5c/-8c; maximum hold 60/120/180/300 seconds.
- **Best in-sample grid results:**
  - Immediate PW: 1,017 trades, +$363.98, +0.36% ROI; TP +3c / SL -3c / 60s; max drawdown $357.61.
  - Early-Q4 fade: 116 trades, +$1,755.08, +15.13% ROI; TP +5c / SL -3c / 300s; max drawdown $580.45.
  - Away-underdog fade: 123 trades, +$150.30, +1.22% ROI; TP +3c / SL -3c / 120s; max drawdown $89.33.
  - Repeat-PW wait for 3c dip then buy PW: 189 trades, -$233.17, -1.23% ROI; best tested grid TP +3c / SL -5c / 300s.
  - Repeat-PW wait for 5c dip then buy PW: 130 trades, -$210.73, -1.62% ROI; best tested grid TP +3c / SL -3c / 60s.
- **Interpretation:** Early-Q4 fade is the strongest initial price-history hypothesis. Immediate PW entry is approximately flat in the best grid; the simple repeat-call price-dip strategies were negative. Because the best settings were selected from a parameter grid on the same history, these are exploratory/in-sample figures and require forward/out-of-sample validation.
- **Live validation status:** Recorder is armed in production at 1-second sampling for 300 seconds after each new genuine PW alert. No genuine post-deployment PW alert has yet produced a `PW_MARKET_WATCH` log, so live tick capture remains pending natural verification rather than injecting a synthetic alert through the trading pipeline.
- **Verified deployment:** `b4071172-973e-4dc8-a716-1bcddcc201d7` SUCCESS.
- **Commits:** `05aa6bf`, `195adf2`, `2422c79`, `72a4396`, `8360e71`, `9c674df`, `1ead4ae`, `64e7a19`.

## 2026-09-19 — Full WNBA game reconstruction with Polymarket pricing (#13)

- **Objective:** Reconstruct every stored WNBA game as a unified research timeline containing ESPN play-by-play, historical Polymarket moneyline prices for both teams, and PW calls aligned to the relevant play state.
- **Files changed:** Added `app/pw_game_reconstruction.py`; wired it through `app/wnba_pw_research_v13.py`; corrected WNBA Polymarket slug aliases in `app/pw_market_research.py`; added `tests/test_game_reconstruction.py`.
- **Storage:** Added research-only SQLite tables `pw_game_reconstruction_coverage` and `pw_game_reconstruction`. Existing `pw_market_history_map` and `pw_market_history_points` are reused rather than duplicating historical token/price data.
- **Timeline fields:** Each reconstructed PBP row carries game/quarter/clock/score/play metadata, away/home historical prices, source price timestamps and lag/alignment metadata, plus any PW calls aligned to that play.
- **Price alignment:** Historical Polymarket `prices-history` data is requested at fidelity=1. A play uses the latest known price at/before its timestamp within 90 seconds; only when no recent prior mark exists may it use the first mark after the play within 90 seconds. Missing marks are left null rather than synthesized.
- **Coverage endpoints:** Added authenticated `/api/pw-game-reconstruction/status`, `/api/pw-game-reconstruction/coverage`, `/api/pw-game-reconstruction/game/{game_id}`, and `/api/pw-game-reconstruction/recompute`.
- **Mapping recovery:** The original market mapper missed a large group of historical games because archived Polymarket WNBA slugs use aliases including `conn` for Connecticut and `las` for Las Vegas. The alias set was expanded and the reconstruction layer can independently recover verified away/home outcome tokens for PBP games, including games that did not previously have a successful PW-side mapping.
- **Missing metadata recovery:** Two PBP game IDs (`401857095`, `401857096`) were absent from canonical `games` metadata. The reconstruction layer now uses the existing ESPN summary pattern to recover away/home competitors without mutating the canonical `games` table. They resolved as POR @ LV and NY @ LA, respectively, and their Polymarket mappings were then recovered.
- **Final reconstruction coverage:** 151/151 PBP games are represented; 148 are fully price-aligned and 3 are partial. There are 61,398 total PBP rows and 61,364 have both away/home historical price marks, for 99.94% row coverage. Remaining 34 play rows are deliberately left without a price because no mark met the 90-second alignment rule. Final status: 0 missing-game-metadata games, 0 unmapped games, 0 games with no price history, and 0 reconstruction failures.
- **PW coverage:** All 1,921 stored PW calls are associated with the reconstructed-game coverage pass. The canonical graded history remains unchanged at 1,362 graded calls (970-392), +$7,188.80 and +5.28% under the existing flat-stake historical grading.
- **Backtest coverage correction:** Fixing the historical market aliases also eliminated the earlier price-mapping gap in the scalp backtest. It now prepares all 1,362 graded PW calls with 245 successful game/side map rows, 0 map errors and 0 historical price-fetch errors. The prior 1,018-call headline results are superseded by this fuller sample.
- **Updated best in-sample grid results on the expanded mapped sample:** Immediate PW: 1,361 trades, +$175.75, +0.13% ROI (TP +3c / SL -3c / 60s; max DD $496.38). Early-Q4 fade: 154 trades, +$1,481.81, +9.62% ROI (TP +5c / SL -8c / 300s; max DD $896.28). Away-underdog fade: 154 trades, +$202.91, +1.32% ROI (TP +3c / SL -3c / 120s; max DD $96.31). Repeat-PW dip3: 253 trades, +$457.88, +1.81% ROI (TP +3c / SL -3c / 300s; max DD $571.46). Repeat-PW dip5: 180 trades, +$55.28, +0.31% ROI (TP +3c / SL -5c / 300s; max DD $512.40).
- **Interpretation caution:** These remain midpoint/proxy, approximately minute-fidelity, best-of-grid in-sample results. They are not executable historical bid/ask fills, do not reconstruct historical order-book depth, and should not be treated as forward-validated net returns.
- **Safety:** Research-only. No order placement, cancellation, sell/close, position sizing, live/paper strategy routing, or risk gate was changed.
- **Verification:** Railway production reconstruction completed with `PW_GAME_RECON_SUMMARY games=151 full=148 partial=3 no_game_meta=0 no_map=0 no_price=0 failed=0 pbp_rows=61398 priced_rows=61364 coverage_pct=99.94 pw_calls=1921`. Canonical startup remained `alerts=1921 graded=1362 wins=970 losses=392`.
- **Implementation issues found/fixed:** An initial 149-game query used an inner join and omitted two PBP-only IDs; changed to a PBP-first left join. Two follow-up reconstruction boots exposed missing `ZoneInfo` and `TEAM_SLUGS` symbols; both were fixed before the final successful pass. No trade path was involved in these failures.
- **Commits:** `86a641b`, `49d7983`, `5bce2a5`, `b9301da`, `2ef11be`, `14eee0a`, `d45de93`, `c7c3364`, `275e6f7`, `d60cf1c`, `ee62e67`.
- **Verified deployment for final reconstruction pass:** `ec6c7eb7-3cb4-4d87-a77a-7e15f42dccfc` reached SUCCESS.
- **Outstanding:** Issue #13 remains open for the first natural live `PW_MARKET_WATCH`/1-second executable tick capture and for out-of-sample/walk-forward robustness analysis of the reconstructed dataset.

## 2026-09-19 — Chronological holdout and walk-forward scalp validation (#13)

- **Objective:** Test whether the five current PW/Polymarket scalp hypotheses survive chronological out-of-sample validation after completing full historical game/price reconstruction.
- **Method:** `app/pw_market_research.py` now splits distinct graded-call games chronologically. The first 70% of games select TP/SL/hold parameters; the final 30% remain untouched holdout. Four expanding walk-forward folds independently reselect parameters on prior games and evaluate the next chronological block. No game appears in both train and test for a given split.
- **Parameter grid:** TP +3c/+5c/+8c; SL -3c/-5c/-8c; hold 60/120/180/300 seconds, unchanged from the prior research grid.
- **Execution-friction stress:** Holdout trades are also re-marked with a worse entry and exit by 0.5c each side and 1.0c each side. This is a simple adverse-price stress, not a reconstructed historical order-book simulation.
- **Immediate PW:** 70/30-selected config TP +3c / SL -3c / 60s. Train: 824 trades, -0.23% ROI. Holdout: 537 trades, +$364.50, +0.68% ROI. 75.0% of parameter configs were positive on holdout and median holdout config ROI was +0.68%. Expanding walk-forward OOS: 704 trades, +$330.22, +0.47% ROI. Stress: -1.77% at 0.5c each side and -3.76% at 1c each side.
- **Early-Q4 fade:** 70/30-selected config TP +3c / SL -8c / 300s. Train: 95 trades, +7.44% ROI. Holdout: 59 trades, +$334.44, +5.67% ROI. 75.0% of configs were positive on holdout; median holdout config ROI +2.25%. Expanding walk-forward OOS: 70 trades, +$682.96, +9.76% ROI. Fold ROIs: +30.13%, -14.16%, +41.63%, +22.73%; the 300s horizon was selected in every fold. Stress: -13.87% at 0.5c each side and -22.27% at 1c each side.
- **Away-underdog fade:** Selected config TP +3c / SL -8c / 180s. Train +2.12%; holdout 46 trades, -$95.81, -2.08%. Only 27.8% of configs positive on holdout; median -1.04%. Walk-forward OOS: 76 trades, +$29.13, +0.38%. Stress remains negative. This hypothesis is not stable on the final holdout.
- **Repeat PW, wait for 3c dip:** Selected TP +3c / SL -3c / 300s. Train +1.98%; holdout 105 trades, +$164.17, +1.56%. 55.6% of configs positive; median +0.26%. Walk-forward OOS: 138 trades, +$181.40, +1.31%. Later folds weakened sharply (-5.71%, -17.17%), and stress turned negative (-1.05% at 0.5c each side, -3.52% at 1c each side).
- **Repeat PW, wait for 5c dip:** Selected TP +5c / SL -5c / 300s. Train -0.85%; holdout 74 trades, -$26.30, -0.36%. Walk-forward OOS: 96 trades, -$82.95, -0.86%. This hypothesis failed chronological validation despite some individual configs being positive.
- **Interpretation:** At the midpoint/proxy level, early-Q4 fade is the only hypothesis with a clearly positive 70/30 holdout, broad positive parameter coverage, and positive aggregate walk-forward performance. However, it is highly sensitive to small adverse execution-price assumptions, so the historical evidence is insufficient to justify calling it an executable edge. Immediate PW appears too small to survive friction. Away-underdog fade and repeat-dip5 fail the holdout/walk-forward robustness test. Repeat-dip3 is mildly positive overall but unstable in the most recent folds and also fails friction stress.
- **Safety:** Research/reporting only. No live trading, order placement, cancellation, sizing, routing, risk gate, or dashboard execution control changed.
- **Commit:** `2f01949` — Add walk-forward scalp robustness analysis (#13).
- **Verified deployment:** Railway deployment `7f5f0d95-8258-434c-bc9a-53915f133ebf` reached SUCCESS. Startup preserved the canonical PW history at 1,921 alerts / 1,362 graded.
- **Outstanding:** Validate actual spread/slippage with natural 1-second live bid/ask captures before considering any strategy execution change. A live forward paper test should use fixed predeclared parameters rather than reoptimizing after each result.


## 2026-09-20 — Actual Polymarket WNBA spread-market backtest (#14)

- **Objective:** Backtest WNBA PW signals against only spread markets that were actually listed by Polymarket, respecting the platform's limited alternate-spread inventory rather than synthesizing sportsbook-style lines.
- **Files changed:** Added `app/pw_spread_backtest.py`; wired it into `app/wnba_pw_research_v13.py`.
- **Research controls:** Added one-off `PW_SPREAD_BACKTEST_ENABLED`, `PW_SPREAD_BACKTEST_REQUEST_SLEEP`, and `PW_SPREAD_BACKTEST_MAX_PRICE_LAG_SECONDS` controls. The backtest flag was returned to `false` after the scan.
- **Availability:** All 151 reconstructed WNBA games had at least one archived Polymarket spread market. There were 380 actual spread markets total (2.52/game average). Distribution by game: 17 had 1, 70 had 2, 43 had 3, 14 had 4, 5 had 5, 1 had 6, and 1 had 7. Thus 57.6% of games had only 1-2 spread markets and 86.1% had at most 3.
- **Historical price coverage:** 1,361 of 1,362 graded PW calls had a usable historical spread-token price aligned to the signal. Historical CLOB `prices-history` data is approximately minute fidelity and was aligned within 120 seconds; it is not reconstructed executable bid/ask depth.
- **Flat-$100 historical results on actual listed spread tokens:** All PW side: 1,361 trades, 935-426, +$6,368.50, +4.68% ROI. Plus-money PW: 313 trades, 198-115, +$3,888.85, +12.42%. Q4 PW: 704 trades, 499-205, +$4,592.97, +6.52%. Repeat same-side PW: 1,138 trades, 802-336, +$6,232.55, +5.48%. Q3 PW: 497 trades, 342-155, +$1,392.61, +2.80%. Q4 fade: 704 trades, 205-499, -$14,047.42, -19.95%.
- **PW-winner diagnostic:** Of the 970 PW calls that ultimately won moneyline, 774 (79.79%) also won the selected available Polymarket spread token; conditional historical spread P/L was +$20,195.85 / +20.82% ROI. This is diagnostic because it conditions on knowing the eventual PW moneyline winner and is not an ex-ante strategy.
- **Data-quality limitation:** The archived WNBA Gamma spread objects continue to expose a numeric handicap value of 1.0 through the fields tested (`groupItemThreshold`, title parsing, and `line`). Actual spread-market identification, token prices, availability and resolved outcomes remain usable, but handicap-number comparisons against PW's stored `live_spread` are not trusted. Exact-line / within-N-points strategy results must not be used until raw archived handicap metadata is decoded correctly.
- **Interpretation:** The strongest unvalidated historical buckets are plus-money PW, Q4 PW and repeated same-side PW. Q4 fading is strongly negative for spread settlement even though earlier moneyline-price scalp work found short-horizon early-Q4 fade behavior; these are different hypotheses/outcomes.
- **Robustness caveat:** These new spread-market results are historical/in-sample and exclude historical order-book spread, depth, slippage and fees. They require chronological holdout/walk-forward testing and execution-friction stress before any trading-route change is considered.
- **Safety:** Research/reporting only. No order placement, cancellation, sizing, routing, execution gate or risk control was changed.
- **Implementation commits:** `4c922660`, `0699cda0`, `13256839`, `6480513d`.
- **Verified research deployment:** Railway production deployment `9e63e70e-6bb0-44f1-bbe6-4df9ae2fbda3` reached SUCCESS.
- **Outstanding:** Decode archived Polymarket spread handicap metadata; run chronological holdout/walk-forward validation; apply 0.5c/1.0c adverse-entry/exit friction stress; validate against natural live executable bid/ask captures before considering automation.


## 2026-09-20 — WNBA underdog loss spread audit (#14)

Research-only rerun after fixing archived Gamma spread-line decoding. The decoder now prefers the signed handicap embedded in the market question/title before falling back to `groupItemThreshold`. This fixed the previous bogus all-`1.0` line metadata. Verified reconstructed Polymarket spread line distribution now spans 1.5 through 16.5.

Scope:
- Canonical graded PW alerts: 1,362.
- Historical Polymarket spread price matched: 1,361.
- Plus-money/underdog PW moneyline losses represented in matched spread records: 207.
- Underdog losses by 1–10 points with matched Polymarket spread history: 177 signals across 48 games.
- Canonical raw count before Polymarket-price matching is 178 signals, so one 1–10 point underdog-loss call lacks a usable Polymarket historical spread price.
- BK Spread is available on 92/177 matched calls in this subset (93/178 in the canonical raw subset).

DraftKings BK Spread in the matched 1–10-point-loss subset:
- +6.5 or higher: 37/92 calls with BK Spread present.
- +7.5 or higher: 22/92.
- +8.5 or higher: 15/92.

Polymarket historical availability at the PW signal time:
- +6.5 or better existed on 126/177 signals (32 games); 29 signals / 15 games had +6.5-or-better priced 50–55c, and 17 signals were in the tighter 51.5–53.5c band around theoretical -110.
- +7.5 or better existed on 109/177 signals (27 games); 25 signals / 12 games were 50–55c, 14 were 51.5–53.5c.
- +8.5 or better existed on 92/177 signals (23 games); 18 signals / 9 games were 50–55c, 10 were 51.5–53.5c.

Typical cost when each target-or-better line existed, measured as the listed spread nearest to 52.38c:
- +6.5 or better median 60.5c (~-153 equivalent).
- +7.5 or better median 65.5c (~-190).
- +8.5 or better median 70.5c (~-239).

For the near--110 50–55c opportunities, the selected target-or-better spread would have covered the actual final losing margin on:
- +6.5-or-better: 18/29 signals.
- +7.5-or-better: 15/25.
- +8.5-or-better: 14/18.

Interpretation:
- Larger live Polymarket cushions were real and sometimes available around 52c, but they were not the normal price state.
- +8.5-or-better near -110 was relatively rare (18/177 matched signals) but 14 of those 18 would have covered the eventual final margin in this conditional loss-only sample.
- This is a retrospective diagnostic on calls already known to have lost the moneyline; it is not an ex-ante ROI estimate and must not be treated as a standalone live edge.
- Historical `prices-history` remains approximate one-minute midpoint/proxy data, not reconstructed executable bid/ask/depth.

Implementation:
- Commit `dc1b4f07a79c328c70b0c8ed9852d374bb2982ea`: fix archived spread-line decoding and add underdog-loss audit output.
- Research deployment `5d1bf581-ff59-4d43-afaf-e47b1353caba` reached SUCCESS.
- One-off `PW_SPREAD_BACKTEST_ENABLED` was returned to `false` after the scan.


## 2026-09-20 — All PW underdog spread-target backtest (#14)

Extended the corrected Polymarket spread-line audit from losing PW underdog calls to every matched graded PW underdog signal.

Scope:
- 313 matched PW underdog signals with usable historical Polymarket spread pricing.
- 106 PW moneyline winners, 207 PW moneyline losers.
- 88 unique games represented.
- Strategy rule is ex-ante/deterministic: when a positive spread at or above the target exists under the price cap, buy the largest cushion available under that cap and hold to settlement.
- Flat $100 stake per signal. Historical prices-history remains ~1-minute proxy pricing, not executable bid/ask/depth.

Results:
+6.5 or better:
- <=55c: 76 trades, 46-30, 60.53% win, +$1,827.73, +24.05% ROI, avg entry 49.34c, avg line +8.67, 30 unique games.
- 50-55c: 46 trades, 29-17, 63.04%, +$919.10, +19.98% ROI, avg entry 52.52c, avg line +8.80, 24 unique games.
- 51.5-53.5c: 26 trades, 18-8, 69.23%, +$820.07, +31.54% ROI, avg entry 52.69c, avg line +8.58, 19 unique games.
- <=60c: 95 trades, 62-33, 65.26%, +$2,460.48, +25.90% ROI, avg entry 52.08c, avg line +8.95, 36 unique games.

+7.5 or better:
- <=55c: 58 trades, 30-28, 51.72%, +$163.54, +2.82% ROI, avg entry 50.06c, avg line +9.34, 23 unique games.
- 50-55c: 36 trades, 20-16, 55.56%, +$206.01, +5.72% ROI, avg entry 52.51c, avg line +9.44, 19 unique games.
- 51.5-53.5c: 18 trades, 11-7, 61.11%, +$282.97, +15.72% ROI, avg entry 52.83c, avg line +9.50, 14 unique games.
- <=60c: 78 trades, 48-30, 61.54%, +$1,094.13, +14.03% ROI, avg entry 53.08c, avg line +9.49, 29 unique games.

+8.5 or better:
- <=55c: 43 trades, 27-16, 62.79%, +$1,086.95, +25.28% ROI, avg entry 49.80c, avg line +9.99, 16 unique games.
- 50-55c: 27 trades, 18-9, 66.67%, +$735.60, +27.24% ROI, avg entry 52.41c, avg line +10.09, 14 unique games.
- 51.5-53.5c: 14 trades, 10-4, 71.43%, +$496.05, +35.43% ROI, avg entry 52.75c, avg line +10.07, 11 unique games.
- <=60c: 55 trades, 37-18, 67.27%, +$1,461.43, +26.57% ROI, avg entry 52.48c, avg line +10.32, 22 unique games.

Interpretation:
- +8.5-or-better is the strongest target in the 50-55c near--110 band in this in-sample test: 27 trades, 18-9, +27.24% ROI.
- +6.5-or-better also performed strongly and produced more opportunities: 46 near--110 trades, +19.98% ROI.
- +7.5-or-better was notably weaker in the broad 50-55c band (+5.72% ROI).
- The tight 51.5-53.5c bands look stronger but are small samples (14-26 trades) and highly correlated because repeated PW calls can occur in the same game.
- These are not yet forward/executable edge estimates. Next robustness checks should include one-per-game-side deduplication, chronological holdout/walk-forward, and entry-friction stress using live executable bid/ask captures where available.

Implementation:
- Commit `6cce44cc6aa3bacf899093e98667a4969e8d89c2`: add all-underdog target/price-cap strategy audit.
- Research deployment `a4309b00-0ed5-4e6e-aec0-bf67226bbddd` reached SUCCESS.
- One-off `PW_SPREAD_BACKTEST_ENABLED` returned to `false`.


## 2026-09-20 — Plus-money Polymarket spread audit vs BK live spread (#14)

Extended the all-underdog spread audit to explicitly test plus-money spread prices and compare them with the stored DraftKings BK live spread at the PW signal time.

Scope:
- 313 matched PW underdog signals across 88 unique games.
- Targets: +6.5 or better, +7.5 or better, +8.5 or better.
- Price bands: <=50c (plus money), <=40c (~+150 or better), <=33.33c (~+200 or better), <=25c (~+300 or better).
- Strategy selection remains deterministic: buy the largest eligible positive spread at/above target under the price cap and hold to settlement.
- BK comparison uses the alert's stored `bk_spread` field.

Key results:

+6.5 or better:
- <=50c: 50 trades / 22 games, 25-25, +$538.08, +10.76% ROI, avg entry 45.92c, avg line +8.50.
- <=40c (~+150+): 16 trades / 10 games, 6-10, +$83.54, +5.22% ROI, avg entry 35.16c, avg line +7.38.
  - BK spread present on 6; BK live spread was higher than Polymarket line on 5, equal on 1, lower on 0; avg BK-minus-Poly gap +2.5 points.
- <=33.33c (~+200+): 4 trades / 3 games, 0-4, -$400, -100% ROI, avg entry 29.5c, avg line +7.5.
  - BK spread present on 2; BK higher than Poly on 1, equal on 1.
- <=25c (~+300+): 0 trades.

+7.5 or better:
- <=50c: 41 trades / 17 games, 18-23, -$313.02, -7.63% ROI, avg entry 47.05c, avg line +8.94.
- <=40c (~+150+): 11 trades / 7 games, 2-9, -$519.80, -47.25% ROI, avg entry 34.59c, avg line +7.77.
  - BK spread present on 4; BK higher than Poly on 3, equal on 1, lower on 0; avg gap +1.75 points.
- <=33.33c (~+200+): 4 trades / 3 games, 0-4, -$400, -100% ROI, avg entry 29.5c, avg line +7.5.
  - Same four signals as the +6.5 target because the selected line was +7.5.
- <=25c (~+300+): 0 trades.

+8.5 or better:
- <=50c: 27 trades / 12 games, 16-11, +$678.77, +25.14% ROI, avg entry 46.85c, avg line +9.69.
- <=40c (~+150+): only 2 trades / 2 games, 2-0, +$380.20, +190.1% ROI, avg entry 34.5c, avg line +9.0.
  - BK spread present on 1; BK live spread was 3 points higher than the selected Polymarket spread.
- <=33.33c (~+200+): 0 trades.
- <=25c (~+300+): 0 trades.

Interpretation:
- True +200-or-better spread opportunities were extremely rare: 4 PW signals total, all at +7.5, across only 3 games; all four lost in-sample.
- The more interesting market-dislocation zone is around +150-or-better (<=40c), where BK live spread was usually higher than the selected Polymarket spread when BK data existed. That supports the user's intuition that the smaller Polymarket cushion can pay materially better odds when DraftKings' live line has moved further out.
- +8.5-or-better at <=40c occurred only twice, so the 2-0 result is far too small to treat as an edge.
- At generic plus-money prices <=50c, +8.5-or-better remained the strongest of these tiers in this in-sample backtest, while +7.5-or-better was negative.
- Historical prices-history is approximate 1-minute proxy pricing, not historical executable bid/ask/depth.

Implementation:
- Commit `a985cbb54fc4c311172ed0482de3d9a00fe4f5d6`: add plus-money price bands and BK-vs-Polymarket live-spread comparisons.
- Research deployment `d24d0841-0072-4dfe-9c12-6454f3d9f795` reached SUCCESS.
- One-off `PW_SPREAD_BACKTEST_ENABLED` returned to `false`.


## 2026-09-20 — Full PW feed spread/price backtest (#14)

Corrected methodology scope from underdog-only to the full matched graded PW feed.

Scope:
- 1,361 matched graded PW calls with usable Polymarket spread history.
- 970 PW moneyline winners / 391 losers.
- 313 underdog calls / 1,048 favorite calls.
- 147 unique games represented in the matched full-feed audit.
- Same deterministic rule: for each target (+6.5/+7.5/+8.5), buy the largest listed positive Polymarket spread at or above target that satisfies the price cap, then hold to settlement.
- Flat $100 stake per signal.

Full-feed results:
+6.5 or better:
- <=50c: 51 trades, 26-25, +$638.08, +12.51% ROI, avg entry 46.00c, avg line +8.58.
- <=55c: 87 trades, 57-30, +$2,848.61, +32.74% ROI, avg entry 49.67c, avg line +8.63.
- 50-55c: 57 trades, 40-17, +$1,939.98, +34.03% ROI, avg entry 52.40c, avg line +8.71.
- <=60c: 110 trades, 77-33, +$3,794.21, +34.49% ROI, avg entry 52.21c, avg line +8.89.
- <=40c (~+150+): 16 trades, 6-10, +5.22% ROI.
- <=33.33c (~+200+): 4 trades, 0-4, -100% ROI.

+7.5 or better:
- <=50c: 42 trades, 19-23, -$213.02, -5.07% ROI.
- <=55c: 69 trades, 41-28, +$1,184.42, +17.17% ROI.
- 50-55c: 47 trades, 31-16, +$1,226.89, +26.10% ROI.
- <=60c: 93 trades, 63-30, +$2,427.85, +26.11% ROI.
- <=40c (~+150+): 11 trades, 2-9, -47.25% ROI.
- <=33.33c (~+200+): 4 trades, 0-4, -100% ROI.

+8.5 or better:
- <=50c: 28 trades, 17-11, +$778.77, +27.81% ROI, avg entry 46.96c, avg line +9.79.
- <=55c: 48 trades, 32-16, +$1,542.74, +32.14% ROI, avg entry 50.07c, avg line +9.92.
- 50-55c: 32 trades, 23-9, +$1,191.40, +37.23% ROI, avg entry 52.41c, avg line +9.97.
- <=60c: 64 trades, 46-18, +$2,230.06, +34.84% ROI, avg entry 52.70c, avg line +10.16.
- <=40c (~+150+): 2 trades, 2-0, +190.1% ROI; sample too small.
- <=33.33c (~+200+): 0 trades.

Important contrast with underdog-only test:
- The full-feed 50-55c results improved materially because some favorite PW calls also had positive-spread Polymarket markets at attractive prices.
- +6.5 50-55c: underdog-only +19.98% ROI (46 trades) -> all-PW +34.03% (57 trades).
- +7.5 50-55c: underdog-only +5.72% (36) -> all-PW +26.10% (47).
- +8.5 50-55c: underdog-only +27.24% (27) -> all-PW +37.23% (32).

Caveats:
- Repeated PW calls in the same game are correlated and count as separate trades.
- Historical prices-history is approximate ~1-minute proxy pricing, not reconstructed executable bid/ask/depth.
- These are in-sample results; next robustness work should deduplicate to one trade per game/side and run chronological holdout/walk-forward plus friction stress.

Implementation:
- Commit `7ebc896864b14ee96fa274e499af6086a56fc958`: full-feed spread/price rules.
- Research deployment `2a444968-3a94-4ebe-8710-3fe21b8cf3f2` reached SUCCESS.
- One-off `PW_SPREAD_BACKTEST_ENABLED` returned to false.


## 2026-09-20 — Deduplicated chronological spread robustness validation (#14)

Added a stricter robustness pass after the full-feed in-sample spread results.

Method:
- Universe: all 1,361 matched graded PW calls with usable historical Polymarket spread history.
- Candidate rules: +6.5/+7.5/+8.5 targets crossed with <=50c, <=55c, 50-55c, and <=60c price bands (12 configs).
- Deduplication: for each candidate rule, only the FIRST qualifying PW signal for each game/team can create a position. Repeated PW calls on the same team in the same game do not create additional trades.
- Game-level chronology: 147 games with usable event timestamps, first 70% (102 games) train, final 30% (45 games) untouched holdout.
- Walk-forward: expanding 60/10, 70/10, 80/10, 90/10 chronological folds; each fold chooses the highest-training-ROI candidate with at least 10 training trades, then evaluates only the next unseen game block.
- Settlement strategy: hold spread share to resolution.
- Entry-friction stress: reprice holdout entries 0.5c and 1.0c worse; no exit friction because the simulated strategy settles rather than sells.
- Historical price source remains approximate Polymarket prices-history (~1-minute proxy), not historical executable bid/ask/depth.

Key fixed-config 70/30 results after one-position-per-game/team dedup:

+6.5:
- <=50c: 23 all trades, +14.95% all-sample ROI; train 16 trades -8.19%; holdout 7 trades +67.85%.
- <=55c: 34 all, +24.43%; train 26 +19.38%; holdout 8 +40.86%; stress +39.24% at +0.5c and +37.65% at +1c.
- 50-55c: 28 all, +29.49%; train 21 +36.28%; holdout 7 +9.13%; stress +8.10% / +7.08%.
- <=60c: 39 all, +28.58%; train 28 +20.58%; holdout 11 +48.92%; stress +47.34% / +45.80%.

+7.5:
- <=50c: 18 all, +3.89%; train 13 -4.10%; holdout 5 +24.65%.
- <=55c: 27 all, +14.09%; train 22 +13.16%; holdout 5 +18.19%.
- 50-55c: 23 all, +24.11%; train 19 +29.99%; holdout 4 -3.81%.
- <=60c: 32 all, +20.32%; train 25 +18.17%; holdout 7 +27.99%; stress +26.85% / +25.73%.

+8.5:
- <=50c: 13 all, +12.45%; train 10 +24.68%; holdout 3 -28.32%.
- <=55c: 18 all, +29.74%; train 14 +39.32%; holdout 4 -3.81%.
- 50-55c: 16 all, +42.77%; train 12 +58.30%; holdout 4 -3.81%.
- <=60c: 23 all, +35.28%; train 17 +40.55%; holdout 6 +20.34%; stress +19.26% / +18.20%.

Train-selected holdout:
- The rule selected solely from the first 70% was +8.5 at 50-55c.
- Training: 12 trades, 10-2, +58.30% ROI.
- Untouched final 30%: 4 trades, 2-2, -$15.24, -3.81% ROI.
- Stress: -4.73% at +0.5c adverse entry; -5.63% at +1c.
- This candidate therefore FAILED the train-selected holdout test despite its strong in-sample/full-feed result.

Expanding walk-forward:
- Every fold selected +8.5 at 50-55c from prior training data.
- Fold 1: 2 OOS trades, 1-1, -9.09%.
- Fold 2: 1 OOS trade, 0-1, -100%.
- Fold 3: 1 OOS trade, 0-1, -100%.
- Fold 4: 2 OOS trades, 2-0, +92.38%.
- Aggregate OOS: 6 trades, 3-3, -$33.42, -5.57% ROI.
- Entry stress: -6.45% at +0.5c and -7.32% at +1c.

Interpretation:
- Repeated-call correlation materially inflated the apparent sample sizes in the earlier signal-level results.
- The headline +8.5 / 50-55c rule did not survive chronological selection and OOS validation.
- Several fixed wider-cap rules remain positive on both train and final holdout, especially +6.5 <=55c/<=60c, +7.5 <=60c, and +8.5 <=60c. However, final holdout samples are only 5-11 trades, so these are promising hypotheses, not established executable edges.
- The strongest fixed holdout by sample size was +6.5 <=60c: train 28 trades +20.58%, holdout 11 trades +48.92%, with positive 0.5c/1c stress. This rule was NOT selected by the train-only optimizer because +8.5 / 50-55c had a much higher training ROI, demonstrating selection-overfit risk.
- More forward data / natural executable order-book captures are needed before live automation.

Implementation / ops:
- Commit `9deef3112bffcf415206175fa74caba2b45b7d9d`: add event timestamps, game/team dedup, fixed-config chronological split, train-selected holdout, expanding walk-forward, and settlement entry-friction stress.
- Research deployment `09fb33e6-099d-4352-916a-236e94788049` reached SUCCESS and completed the 151-game scan.
- One-off `PW_SPREAD_BACKTEST_ENABLED` returned to `false`.
- Research only. No live execution path, sizing, routing, dashboard live-control, or real-money setting was changed.


## 2026-09-20 — Broad WNBA spread scan + fixed walk-forward (#14)

Expanded the historical spread research to avoid overfitting tiny +6.5/+7.5/+8.5 samples.

Method:
- 1,361 matched graded PW calls with usable Polymarket spread history.
- One position per game/team per rule (first qualifying PW signal only).
- Absolute spread family: +2.5 through +12.5, price caps 45/50/55/60/65c (55 configs).
- DK-gap family: where stored BK Spread is positive, allow max sacrifice of 0/1/2/3/4/6 points vs DraftKings and price caps 40/45/50/55/60/65c (36 configs). Select cheapest PM spread satisfying the cushion constraint.
- 70/30 chronological game split retained.
- Added fixed-rule expanding OOS blocks (60-70%, 70-80%, 80-90%, 90-100%) with no fold-by-fold rule selection.
- Adverse entry stress +0.5c / +1.0c.
- Historical PM pricing is still ~1-minute proxy data, not executable order-book reconstruction.

Higher-volume absolute-spread findings:
- +2.5 <=65c: 79 total, 50-29, +24.67% ROI; train 56 +16.74%; final holdout 23 +43.98%; fixed OOS 31 +36.66%; +1c stress +33.11%. Fold ROIs +15.60%, +85.09%, +37.68%, +11.64%.
- +2.5 <=60c: 76 total, 47-29, +25.77%; train 53 +17.64%; holdout 23 +44.50%; fixed OOS 31 +37.04%; +1c stress +33.48%. Fold ROIs +15.60%, +86.57%, +37.68%, +11.64%.
- +2.5 <=55c: 68 total, 40-28, +25.29%; train 48 +18.91%; holdout 20 +40.61%; fixed OOS 27 +41.50%; +1c stress +37.62%. Fold ROIs +44.05%, +89.22%, +31.38%, +3.85%.
- +3.5 <=60c: 65 total +25.02%; train 46 +18.78%; holdout 19 +40.12%; fixed OOS 26 +29.69%; +1c stress +26.18%. Last fold was -6.89%.
- +4.5 <=60c: 57 total +21.52%; train 42 +20.90%; holdout 15 +23.24%; fixed OOS 22 +16.29%; +1c stress +13.97%.
- +5.5 <=60c: 51 total, 35-16, +33.44%; train 38 +30.44%; holdout 13 +42.20%; fixed OOS 19 +37.05%; +1c stress +34.27%. All four fixed OOS folds positive (+25.88%, +44.60%, +40.79%, +39.67%).

Interpretation:
- Broadening to smaller positive spreads materially increases independent sample size without destroying the historical signal.
- The best volume/stability candidate from this pass is not +8.5; it is the broad +2.5-or-better family around a 55-60c cap.
- +5.5 <=60c is lower-volume but also notably stable across all four fixed OOS blocks.
- None of these has 100+ independent historical WNBA positions yet, so forward validation remains necessary.

DK-gap findings:
- Stored BK Spread is present on 570 matched signal records overall, but positive-BK-spread gap rules still produce only ~30-37 independent positions.
- Several DK-gap rules were strongly positive in historical holdout/OOS, e.g. <=4-point sacrifice, <=60c: 36 total, +52.42%; fixed OOS 26 +47.20%; +1c stress +42.05%.
- Some selected PM spreads were actually larger than the stored BK spread (negative average sacrifice). Because historical PM pricing is proxy data and timestamps can differ by up to the allowed lag, these unusually strong gap results must be treated as suspect until confirmed with natural live executable captures.
- This directly motivates forward executable spread capture issue #15.

Implementation:
- Commit `5a74028eacacbad8698c16d21c0067ccfdd43095`: broad absolute-spread + DK-gap scan.
- Commit `b50394a88d7ae9a2db867b8cd291cf529c9427ff`: fixed-rule walk-forward.
- Research deployment `496d2674-2c9b-42be-9065-662c76d01a3b` completed successfully.
- Research flag returned OFF.
- Issue #15 created for natural live executable spread capture.


## 2026-09-20 — Vendor NBAMonitor PW research snapshot (#16)

Created `research/upstream_nbamonitor/` as a read-only reference snapshot of the upstream PW generator.

Source:
- Repository: `bobcheong/NBAMonitor`
- Pinned commit: `7fc3285823eb7190b737edd8580ec69229e4b5ae`
- Commit date: 2026-09-19
- Purpose: inspect exact PW firing logic, bookmaker live-odds capture, `bk_spread`/`bk_ts` semantics, repeat/suppression behavior, export generation, grading and replay.

Copied research-relevant files:
`README.md`, `CLAUDE.md`, `monitor.py`, `odds_api.py`, `conditions.py`, `server.py`, `outcomes.py`, `pw_trend.py`, `backfill_pw_calls.py`, `export_pw_jsonl.py`, `league_config.py`, and `synthetic_odds.py`.

The snapshot is isolated under `research/` and is not imported by the live trading path. `SNAPSHOT.md` records provenance and the pinned upstream revision.

Initial source inspection confirms the upstream system explicitly stores `bk_spread`, `bk_spread_price`, `bk_spread_source`, and `bk_ts`, and `odds_api.py` describes `fetch_live_game_odds()` as capturing real-time spread and moneyline when a PW call fires. These fields will be used to tighten the DK-vs-Polymarket timing analysis in #14/#15.


## 2026-09-20 — NBAMonitor source review: implications for PW/Polymarket research (#16, #14, #15)

Reviewed pinned upstream source `bobcheong/NBAMonitor@7fc3285823eb7190b737edd8580ec69229e4b5ae`.

### PW fire ordering / timing

Live PW path in `monitor.py`:
1. Compute PW final-winner score and apply adaptive favorite/underdog threshold.
2. Apply Q3/Q4/OT hard stop when predicted team trails by configured large margin (default 20).
3. Apply per-game/per-team cooldown (default 5 minutes).
4. Capture score, active conditions and pregame context.
5. Call `odds_api.fetch_live_game_odds(..., context="pw_fire", quarter=quarter)`.
6. Extract `bk_spread`, `bk_spread_price`, `bk_moneyline`, `bk_name`, `bk_ml_source`, `bk_spread_source`, `bk_ts`.
7. Build/send Slack PW message including BK Odds and BK Spread.
8. Persist PW call record with timestamp and all BK metadata.

This means the Slack alert timestamp used in the canonical WNBA archive occurs after the PW-side BK fetch and should normally be within seconds of the quote fetch.

### Odds freshness nuance

`odds_api.py` has a 120-second in-process event cache, but production polling launches a fresh `monitor.py` subprocess every ~30 seconds (via systemd or `monitor_loop.py` subprocess), so the cache does not persist between ticks. It can only be reused within one monitor invocation.

Important caveat: `fetched_ts` is stamped when `fetch_live_game_odds()` returns, even if the underlying event response came from that same-run cache. It is therefore a local fetch/return timestamp, not necessarily the sportsbook's own market last-update timestamp. The raw Odds API event/bookmaker payload may carry more precise update metadata, but that is not persisted on the PW call.

### BK Spread is DraftKings-preferred, not guaranteed DraftKings

`BOOKMAKER_PRIORITY` is:
`draftkings -> fanduel -> betmgm -> caesars -> bet365`, then any bookmaker with a spreads market.

`extract_odds()` can also source spread and moneyline from different fallback books. Therefore historical `bk_spread` must be described as **live bookmaker spread (DraftKings preferred)** unless `bk_spread_source` confirms DraftKings.

The Slack-derived canonical archive preserves `bk_spread` but not `bk_spread_source`, `bk_spread_price` or `bk_ts`. Those richer fields are present in upstream `/api/pw-export`.

### Live vs synthetic backfill

Upstream `backfill_pw_calls.py` synthetic calls contain `source="backfill"`, `synthetic=True` and do **not** populate `bk_spread`/`bk_moneyline`/live BK timestamp metadata. The canonical 1,921-call bot archive is reconstructed from actual Slack PW messages with real alert timestamps; calls that include BK Spread therefore originate from the live PW alert format, not the synthetic backfill call schema.

### Repeat calls

Current live code uses a per-game/per-team default 5-minute cooldown. Repeated same-side calls are confirmations at least ~5 minutes apart, not independent observations. If the predicted side flips, the other team's separate cooldown key can allow an alert without waiting for the first team's cooldown. Suppressed calls use an approximately 2-minute dedup cooldown and are not sent as ordinary PW Slack alerts.

This reinforces the research choice to count one independent position per game/team while using repeats as timing/confirmation features.

### PW objective vs spread strategy

PW chooses the top **final game winner** probability (historical/Bayesian/ML blend). It is not directly trained to maximize ATS/spread cover. Our Polymarket spread strategy is therefore a second-stage market strategy:
- PW supplies directional/final-winner information.
- Positive spread protection converts that directional signal into a higher cover probability.
- Price/line selection determines whether the extra protection is worth the cost.

This explains why a fixed PW ML rule and a PW + positive-spread rule can have very different profitability.

### New state segmentation from canonical WNBA data

Among 1,362 graded calls, 571 have both pregame handicap and live `bk_spread` available.

Signal-level:
- Pregame favorite still live favorite: 286 calls, 89.51% PW outright win, 39.16% cover of live BK spread.
- Pregame favorite flipped to live dog: 5 calls / 4 games, 60.00% outright win, 80.00% BK-spread cover. Too small for inference.
- Pregame dog still live dog: 144 calls / 45 games, 27.78% outright win, 60.42% BK-spread cover.
- Pregame dog flipped to live favorite: 136 calls / 29 games, 62.50% outright win, 40.44% BK-spread cover.

First call per game/team:
- Favorite still favorite: 60, 81.67% outright, 43.33% BK cover.
- Favorite -> live dog: 4, 75.00% outright, 100% BK cover (tiny sample).
- Dog still dog: 45, 24.44% outright, 62.22% BK cover.
- Dog -> live favorite: 29, 58.62% outright, 44.83% BK cover.

Interpretation: for meaningful samples, positive live spread protection is most useful when the team remains a live underdog; negative live favorite spreads cover much less often even though outright PW accuracy is high. This supports researching positive Polymarket alternate spreads as protection on strong PW directions rather than treating the BK live spread itself as the desired betting line.

### Research-data gap discovered in bot

`app/pw_research_sync.py` can fetch full upstream `/api/pw-export` rows and has a `research_raw_json` column, but `PW_RESEARCH_IMPORT_ENABLED` defaults false because the canonical Slack 1,921-call history is authoritative. Even when enabled, natural duplicates are currently skipped rather than enriching the existing canonical row with upstream raw metadata.

Recommended next research-data change:
- match upstream live export rows onto existing canonical alerts;
- enrich existing rows (do not insert duplicates) with `bk_spread_price`, `bk_spread_source`, `bk_ml_source`, `bk_ts`, `bk_name`, source/live flags, active conditions, consensus/basis and raw record;
- then rerun DK-vs-PM gap analysis using only source-confirmed live bookmaker quotes and known juice.

This should precede strong conclusions from the historical BK-gap strategy.

### Current strategy interpretation

Earlier broad Polymarket spread scan remains the higher-volume candidate family:
- +2.5-or-better <=55-60c: 68-76 independent positions with positive train, final holdout and fixed walk-forward in the historical proxy-price test.
- +5.5-or-better <=60c: lower volume but also positive across all four fixed OOS blocks.

However, historical Polymarket prices are still ~1-minute proxy prices. Issue #15 forward executable order-book capture remains required before any live-spread strategy is treated as executable edge.


## 2026-09-20 — Vendor NRLMonitor research snapshot (#17)

Created `research/upstream_nrlmonitor/` as a read-only reference snapshot of `bobcheong/NRLMonitor`.

Pinned source commit:
- `990bcbb37c12c0719112409099063acd6bb07033`
- Commit message: `Initial commit`
- Commit date: 2026-05-18

Current upstream contents at that commit:
- `README.md` only, containing `# NRLMonitor`.

There is currently no NRL monitor/odds/PW/export/backfill implementation in the upstream repository to inspect or copy. `SNAPSHOT.md` records provenance and explicitly notes that the snapshot should be refreshed when source code is added.

The snapshot is isolated under `research/` and is not imported by the live trading path.
