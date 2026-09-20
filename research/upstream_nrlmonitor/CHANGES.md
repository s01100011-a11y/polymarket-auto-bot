# CHANGES.md

All notable changes to NRL Monitor, ordered newest first.

---

## 2026-09-18 — fix: PW alert retention in alerts.json + /api/pw-export + JSONL export (#302)

### Bug fix: PW entries lost from alerts.json

- **`monitor.py`**: `update_alert_log()` kept only the last 20 entries regardless of type — `predicted_winner` and `pw_underdog_alert` entries were quickly pushed out by newer condition alerts. Fixed: anchored types (summary, final, predicted_winner, pw_underdog_alert) are now retained permanently; transient condition alerts capped at 200.

### New: /api/pw-export — flat PW call feed for external consumers

- **`server.py`**: New `GET /api/pw-export` endpoint — flat array of PW calls with game context. For external trading bot consumption. Includes live in-progress calls from `.alert_state.json`.
  - Params: `since`, `until`, `source` (live/backfill/all), `include_suppressed` (0/1)
  - Response: `{league, exported_at, total_calls, filters, calls[]}`

### New: JSONL file export for PW calls

- **`monitor.py`**: At game-end, appends non-suppressed PW calls to `pw_export.jsonl` — one JSON object per line with game context merged.
- **`export_pw_jsonl.py`** (new): Bootstrap script to generate `pw_export.jsonl` from existing `game_history.json`. Supports `--source`, `--since`, `--until`, `--include-suppressed`.

Cross-ref: NBA/WNBA #457.

---

## 2026-09-18 — feat: Test PW Alert — source options (history, upcoming, sample) (#301)

- **`server.py`**: Enhanced `POST /api/test-pw-alert` with source selection. Accepts JSON body `{source, game_id}`. History source loads a real PW call from `game_history.json` with actual confidence, consensus, polarity, edge, and odds. Upcoming source uses game teams with sample PW values. Sample source uses hardcoded data (Storm vs Roosters). Optional `game_id` targets a specific match (partial match against match_id).
- **`dashboard.html`**: Source dropdown (Sample / From History / From Upcoming), optional Match ID input field, and help modal (?) with source descriptions.

Cross-ref: NBA/WNBA #457. Builds on #300.

---

## 2026-09-18 — feat: Test PW Alert button in dashboard Configuration Settings (#300)

- **`server.py`**: New `POST /api/test-pw-alert` endpoint — reads config for Slack credentials, builds an NRL PW alert message (Storm vs Roosters), sends via `send_alert()`. Message includes `— _This is a test alert_` suffix for identification.
- **`dashboard.html`**: "Send Test PW Alert" button in Configuration → Settings → Alerting group with inline status feedback (Sent!/Failed, auto-clears after 8s).

Cross-ref: NBA/WNBA #456.

---

## 2026-09-15 — feat: ChromeOS (Crostini) dual-environment support (#299)

- **`monitor_loop.py`** (new): Polling loop wrapper that replaces systemd timer on ChromeOS/Crostini. Runs `monitor.py` every 30s (configurable via `MONITOR_POLL_INTERVAL` env var). supervisord config lives in NBA repo (`supervisord.conf`, NBA #455) and manages all 6 processes across both repos.

Cross-ref: NBA #455.

**Files added:** `monitor_loop.py`

---

## 2026-09-11 — fix: monitor cannot see Finals games — round scan capped at 27 (#298)

- **`nrl_api.py`**: `get_current_round_fixtures()` scanned `range(1, 28)` — rounds 1-27 only. Extended to `range(1, 32)` to cover Finals Week 1-4 (rounds 28-31). NRL API returns the same finals fixtures for any round ≥ 28, so duplicate detection via `matchCentreUrl` sets prevents redundant processing. Added `_detect_finals_week()` to derive synthetic round numbers (28-31) from `roundTitle` since the API returns `roundNumber=null` for finals. Added `_is_finals_round()` helper and `MAX_REGULAR_ROUND`/`MAX_FINALS_ROUND` constants.
- **`nrl_api.py`**: `get_live_scoreboard()` now sets `_is_finals` flag on normalized game dicts for downstream finals detection.
- **`monitor.py`**: Game-end records now tagged `season_segment: "finals"` instead of `"regular_season"` when `_is_finals` or round > 27. Auto-recovery segment detection also handles finals. Round-flip URL regex handles finals URL formats (`finals-week-N`, `preliminary-final`, `grand-final`). Log output shows round title ("Finals Week 1") instead of round number for finals. Auto-recovery null-guarded against `current_round=None`.
- **`recovery.py`**: Scanner extended from `range(1, 28)` to `range(1, MAX_FINALS_ROUND + 1)` with duplicate fixture detection for finals rounds.

---

## 2026-09-07 — fix: disable pw_edge_gate — alert filters replace hard suppression (#296)

- **`config.json`**: Set `pw_edge_gate: null` (was `0`). The edge gate in `_check_pw_suppression()` ran upstream of `pw_alert_filters`, silently killing PW calls before they could be recorded or evaluated by alert filters. Evidence: Sharks vs Storm (2026-09-05) — Storm PW evals suppressed for entire first half (`avg_edge -0.5 to -7.7 < gate 0`), no calls on dashboard until prediction flipped to Sharks at 20:29 AEST. With `pw_alert_filters.enabled: true`, the `edge_min`/`edge_max` filter fields already provide edge-based control at the Slack alert level while still allowing all calls to appear on the PW Live dashboard tab.

Cross-ref: NBA #453.

**Config-only change.**

---

## 2026-08-29 — fix: dashboard startup blocking — prune to background thread + memory watchdog threshold (#295)

- **`server.py`**: `prune_fallback_data()` was called synchronously between the server socket bind and `serve_forever()`, blocking request handling during the prune. Moved to a background thread (`odds-log-prune`); `configure_fallback_save()` stays synchronous (fast, needed before log writes). On NBA Monitor, the equivalent issue caused 55–80s startup timeouts after a reboot (NBA #451).
- **`server.py`**: Memory watchdog threshold raised from 512 MB → 768 MB to give headroom for cache warming within the 2 GB service memory limit. Matches NBA fix (NBA #451).

## 2026-08-25 — fix: PW filter-driven Slack alert missing key fields vs legacy format (#293)

- **`monitor.py`**: The `pw_alert_filters` alert path (#263) was missing live moneyline, live spread, polarity, edge, and had abbreviated/inconsistent field labels. Enriched with all fields matching NBA #449: pregame odds ("Odds"), pregame spread ("Spread"), live ML (when different from pregame), handicap (live spread), BK Odds, BK Spread, polarity, edge, and inline scenario stats. Unified message format to match legacy style.

Cross-repo: NBA #449.

**Files changed:** `monitor.py`

---

## 2026-08-21 — fix: Alert History and PW alert bugs (#290)

- **`monitor.py`**: `send_alert()` returns a plain `bool`, but two call sites (PW filter alert at line 1712, game summary alert at line 2575) tried to unpack as `(ok, err)` tuple — crashing with `cannot unpack non-iterable bool object`. Because the crash was caught silently, `_pw_alert_delivered` stayed `False`, and with `pw_alert_filters.enabled=True` the guard `_pw_should_log = _pw_alert_delivered if ... else True` skipped `update_alert_log()`. Result: zero PW calls in alert history + game summary alerts silently broken. Fixed both to plain bool assignment.
- **`dashboard.html`**: `loadAlerts()` called undefined `renderConsensusBadge()` for PW alert entries — JS error prevented the entire alert list from rendering. Added the function with strong/conflicted/fallback badge styles.
- **`monitor.py`**: `sin_bin`, `red_card`, and `first_team_scores` conditions kept re-firing every 5-minute cooldown because PBP cumulative count stays above threshold for the rest of the game. Now default to `alert_once="game"` — fire once per team per game. Overridable via config `alert_once` field.
- **`monitor.py`**: Auto-recovered games (`_recover_missed_game`) wrote game-end alerts to `alerts.json` with the current timestamp, making old games (e.g. Round 7) appear as today's Full Time alerts. Removed `update_alert_log` call from recovery — Slack alert kept, alert history now only shows live events.
- **`server.py`**: POST `/api/config` ran `runtime_backup.snapshot_runtime_files()` synchronously before responding (~2 min with 3.8GB backups), causing "Save as Alert Filters" to fail with `Unexpected end of JSON input` (browser timeout). Moved snapshot to background daemon thread.

**Files changed:** `monitor.py`, `dashboard.html`, `server.py`

---

## 2026-08-21 — fix: Best Filter Combos lock checkboxes desync on live refresh (#288)

- **`dashboard.html`**: The combo section HTML (including lock checkboxes) is baked into `_pwLiveAccuracyHtml` at initial load. Every 15s live refresh rebuilds the DOM from this stale HTML, resetting checkbox visual state. Clicking a visually-stale checkbox toggles the lock in the wrong direction — the lock silently disables while the checkbox shows checked. Fixed by syncing lock checkboxes with `_comboLockDims` after each DOM rebuild via `data-lock-dim` attributes.

Cross-repo: NBA #444.

**Files changed:** `dashboard.html`

---

## 2026-08-21 — fix: PW Live auto-refresh ignores consensus and Live Only filters (#287)

- **`dashboard.html`**: `_pwFilterLiveCalls()` only applied scenario ROI filters. The 15s auto-refresh path (`/api/pw-calls-live`) returns all live calls unfiltered, so consensus checkboxes and Live Only toggle had no effect after the initial load — calls with any consensus type would reappear. Now applies consensus, Live Only (`bk_ml_source`), and scenario ROI filters client-side.

Cross-repo: NBA #443.

**Files changed:** `dashboard.html`

---

## 2026-08-21 — fix: "Edit in Settings" scroll + PW help modal updates (#286)

- **`dashboard.html`**: Added scroll-to-view for PW Alert Filters section when clicking "Edit in Settings" in the alert filter bar.
- **`dashboard.html`**: Added Consensus Types section to PW help modal explaining Strong, Conflicted, ML Only, and Historical Only classification.
- **`dashboard.html`**: Updated Slack Alert Filters help to document Use Saved Alert Filters, Edit in Settings, and Disable links in the alert bar.
- **`dashboard.html`**: Updated Best Filter Combos help — added Consensus (Cons) column to table docs, noted lock persistence to localStorage, documented Apply filter reset behavior and Restore previous filters link.

Cross-repo: NBA #442.

**Files changed:** `dashboard.html`

---

## 2026-08-20 — fix: Best Filter Combos lock checkboxes reset on refresh (#285)

- **`dashboard.html`**: `_comboLockDims` was purely in-memory — never saved to or restored from localStorage. Added `_saveComboLockDims()` helper called on every toggle, and localStorage restore on page load.

Cross-repo: NBA #441.

**Files changed:** `dashboard.html`

---

## 2026-08-20 — feat: Best Filter Combos: Consensus as 6th sweep dimension (#284)

- **`dashboard.html`**: Added Consensus (strong, conflicted, ml_only, historical_only) as a 6th sweep dimension in Best Filter Combos. Adds 5 new pairwise combinations (Role+Consensus, Margin+Consensus, etc.). Consensus column in table, lock checkbox, Apply maps to consensus filter checkboxes.

Cross-repo: NBA #440.

**Files changed:** `dashboard.html`

---

## 2026-08-20 — feat: Best Filter Combos Apply resets filters + restore (#283)

- **`dashboard.html`**: Apply now resets all PW filters (except season, segment, call selection) before applying the combo's 5 dimension + ROI filters — prevents stale filters from mixing with combo results. Full filter state saved beforehand for restore.
- **`dashboard.html`**: "Restore previous filters" link next to "Clear combo filters" — restores all filters to pre-Apply state. Only visible after an Apply has been clicked. Disappears after restoring.

Cross-repo: NBA #439.

**Files changed:** `dashboard.html`

---

## 2026-08-20 — feat: "Use Saved Alert Filters" button on PW Live (#282)

- **`dashboard.html`**: Added "Use Saved Alert Filters" link in the alert filter bar (before "Edit in Settings"). Reads `pw_alert_filters` from config and applies them to all PW Live filter dropdowns/checkboxes — the reverse of "Save as Alert Filters". Shows "✓ Filters applied" confirmation.

Cross-repo: NBA #438.

**Files changed:** `dashboard.html`

---

## 2026-08-19 — fix: "Save as Alert Filters" button shows no feedback (#281)

- **`dashboard.html`**: Added missing `pw-alert-filter-status` `<span>` element next to the button — "✓ Alert filters saved" / error messages now render visibly.

Cross-repo: NBA #437.

**Files changed:** `dashboard.html`

---

## 2026-08-19 — fix: PW alert filter spread_role case mismatch (#280)

- **`monitor.py`**: `_pw_call_passes_alert_filters()` used case-sensitive comparison for `spread_role`, causing filtered PW alerts to be silently dropped when call records have different casing. Fixed with `.lower()` on both sides.

Cross-repo: NBA #436.

**Files changed:** `monitor.py`

---

## 2026-08-18 — feat: PW alerts parity with NBA/WNBA (#279)

- **`monitor.py`**: Filter-driven PW Slack alert now includes score, margin, spread, BK spread, and scenario stats line — matching NBA format. Fixed latent `game_name` NameError in PW alert log lines.
- **`monitor.py`**: Added unified `update_alert_log()` entry for every non-suppressed PW call with 25+ fields (pct, consensus, blended, basis, margin, scores, spread_role, live odds, BK odds, polarity, edge, game_name, scenario stats). Gated by `_pw_should_log` when `pw_alert_filters` is enabled — only logs calls that passed filters.
- **`dashboard.html`**: Alert History renders `predicted_winner` entries with rich format — team name with percentage, badges (blended, combo, consensus, edge), game name, quarter, scenario stats line, History link. Matches NBA Alert History PW card layout.

Cross-repo: NBA #435.

**Files changed:** `monitor.py`, `dashboard.html`

---

## 2026-08-18 — feat: Add missing PW Live filters to alert filters (#278)

- **`monitor.py`**: Extended `_pw_call_passes_alert_filters()` with `strict_bk` (require both `bk_moneyline` and `bk_spread` present), scenario ML/Spread ROI, Q ML/Spread ROI, ROI logic (AND/OR), and min calls/games filters. Scenario stats looked up via `_scenario_key` stamped on the call record at fire time.
- **`dashboard.html`**: `_saveAsAlertFilters()` now maps all new filter fields from PW Live tab to `pw_alert_filters` config. Alert filter bar shows new pills (strict BK, ML ROI, Spr ROI, Q ML, Q Spr, ROI logic, min calls/games). Config Settings UI has new fields for all scenario ROI filters.
- **`config.example.json`**: Added `pw_alert_filters` section with all filter keys.

Cross-repo: NBA #435.

**Files changed:** `monitor.py`, `dashboard.html`, `config.example.json`

---

## 2026-08-15 — fix: Best Filter Combos Min Bets/Games inputs reset on re-render (#277)

- **`dashboard.html`**: Min Bets/Games inputs in Best Filter Combos lost their values on every `_renderPwPage()` call (15s auto-refresh, filter changes, page reload) because the inputs were rebuilt with hardcoded `value="0"` and `_renderComboTables()` read from the just-destroyed DOM.
- **`dashboard.html`**: Added `_comboMinFilters` state object with localStorage persistence. New `_updateComboMinFilter()` handler saves to state + localStorage. `_renderComboTables()` reads from state instead of DOM. Input elements restore values from state on each rebuild.

Cross-repo: NBA #434.

**Files changed:** `dashboard.html`

---

## 2026-08-15 — feat: Best Filter Combos ROI column + min bets/games filter (#276)

- **`dashboard.html`**: ROI % column (units ÷ bets × 100) next to Units in all 3 combo tables, color-coded green/red.
- **`dashboard.html`**: Min Bets and Games inputs in combo header — filters aggregate combo row totals (distinct from scenario-level Min Calls/Min Games). Instant re-render from cached sweep data.
- **`dashboard.html`**: `scenGames` (distinct game count) tracked per combo via `_game_id` on each call. `_allCallsUnfiltered` now includes `_game_id`.
- **`dashboard.html`**: Help modal updated with ROI column definition and Min Bets/Games filter description.

Cross-repo: NBA #433.

**Files changed:** `dashboard.html`

---

## 2026-08-14 — feat: Best Filter Combos lock dimension controls (#275)

- **`dashboard.html`**: Lock checkboxes (Role, Margin, Half, Qtr, Edge) + All/None toggle next to Best Filter Combos header. Locked dimensions force every combo row to have a non-"all" value for that dimension. Combo tables re-render instantly from cached sweep results via `_renderComboTables()` — no API re-fetch. Extracted combo table rendering into reusable function with `_comboSweepResults` and `_comboLockDims` state.
- **`dashboard.html`**: Updated Best Filter Combos help modal with comprehensive documentation.

Cross-repo: NBA #432.

**Files changed:** `dashboard.html`

---

## 2026-08-14 — feat: Best Filter Combos sweep dimensions + Spread (Pregame) table (#274)

- **`dashboard.html`**: Combo sweep now includes 5 dimension filters (Role, Margin, Half, Quarter, Edge) swept pairwise alongside existing ROI thresholds. Each combo row shows a single value per dimension ("—" = all). New columns added to table header: Role, Margin, Half, Qtr, Edge.
- **`dashboard.html`**: New "Top 3 by Spread (Pregame)" ranking table — uses pregame spread line (`spread` field) with assumed 1.91 decimal price (standard BK pregame spread odds).
- **`dashboard.html`**: `_applyComboFilters()` now accepts and sets dimension dropdown filters (role, margin, half, quarter, edge). Triggers `loadPWPanel()` server reload when dimensions change. `_clearComboFilters()` saves/restores dimension filter state.

Cross-repo: NBA #431.

**Files changed:** `dashboard.html`

---

## 2026-08-04 — fix: Best Filter Combos changes after Apply — use unfiltered calls (#272)

- **`dashboard.html`**: Combo sweep used `allCalls` (post-scenario-filter), so combos changed when Apply set ROI filters. Now saves `_allCallsUnfiltered` before scenario filtering with game scores attached (`_home_score`, `_away_score`, `_home_team`), and uses that for combo `_callBk` pre-computation. Combos stay stable regardless of applied filters.

Cross-repo: NBA #429.

**Files changed:** `dashboard.html`

---

## 2026-08-03 — feat: persistent alert filter bar + Config Settings editor (#269)

- **`dashboard.html`**: Persistent summary bar on PW Live sub-tab with colored filter pills. Config Settings section with editable dropdowns for all PW alert filter fields.

Cross-repo: NBA #426.

**Files changed:** `dashboard.html`

---

## 2026-08-03 — feat: game-end summary Slack alert (#270)

- **`monitor.py`**: Sends brief Slack alert at game end when `pw_alert_filters.game_summary` is true. Shows final score, winner + margin, PW accuracy, BK ML units, BK Spread covered + units. Uses decimal odds for ML profit.

Cross-repo: NBA #427.

**Files changed:** `monitor.py`

---

## 2026-08-02 — fix: dashboard memory leak — ThreadPoolExecutor + watchdog (#423 parity)

- **`server.py`**: Replaced `ThreadingMixIn` with `ThreadPoolExecutor(max_workers=4)`.
- **`server.py`**: Memory watchdog — checks RSS every 60s, `os._exit(1)` at 512MB. systemd auto-restarts.
- **`server.py`**: `gc.collect()` after large responses + `_load_alert_state_cached()` mtime cache.

Cross-repo: NBA #423.

**Files changed:** `server.py`

---

## 2026-08-01 — fix: /api/pw-calls-live NameError on state_file (#265)

- **`server.py`**: `state_file` was referenced for fingerprint computation but not defined after refactor to `_load_alert_state_cached()`. Endpoint returned 500 for all requests, hiding live games from PW Live tab. Fixed by defining `_state_file` locally.

Cross-repo: NBA #420 (same bug).

**Files changed:** `server.py`

---

## 2026-08-01 — fix: Best Filter Combos Apply stays on current sub-tab (#268)

- **`dashboard.html`**: Removed `switchPwSubTab('history')` override from `_applyComboFilters()`. Apply now stays on the current sub-tab — Live Apply sets `pw-live-*` filters and stays on Live tab, History Apply sets `pw-*` filters and stays on History tab. Allows users to see how historically-computed combo filters affect live games without switching tabs.

Cross-repo: NBA #424.

**Files changed:** `dashboard.html`

---

## 2026-08-01 — fix: Best Filter Combos Apply logic + units mismatch (#267)

- **`dashboard.html`**: Two fixes for Best Filter Combos:
  1. **OR/AND logic mismatch**: Combo sweep used OR logic but `_applyComboFilters()` didn't set the `roi-logic` dropdown, defaulting to AND. Now sweeps both AND and OR separately, shows Logic badge (OR/AND) per row, and Apply sets the dropdown to match.
  2. **Units mismatch after Apply**: Combo summed `bk_spr_pnl` from server-side scenario cache (all calls in matching scenarios), but stats block computed from client-side filtered calls (fewer due to additional server-side filters). Now pre-computes per-call BK ML/Spr profit from `allCalls` and aggregates by matching scenario keys — units match stats block exactly.
- **`dashboard.html`**: `_clearComboFilters()` restores saved logic dropdown value.

Cross-repo: NBA #422.

**Files changed:** `dashboard.html`

---

## 2026-08-01 — fix: PW Live tab retains completed games after auto-refresh (#265)

- **`dashboard.html`**: `_pwRefreshLiveOnly()` merge branch added/updated games from `/api/pw-calls-live` but never removed games absent from the response. Completed games stayed visible on the Live tab indefinitely. Fixed by filtering `_pwLiveGames` to only retain games present in the fresh API response after merging.

Cross-repo: NBA #420.

**Files changed:** `dashboard.html`

---

## 2026-08-01 — fix: gc.collect() after large JSON responses (#423 parity)

- **`server.py`**: Added `gc.collect()` after `send_json` responses >100KB to reduce memory accumulation. NRL was growing at ~14MB/min pre-fix. Still needs deeper profiling.

Cross-repo: NBA #423.

**Files changed:** `server.py`

---

## 2026-08-01 — feat: filter-driven PW Slack alerts (#263)

- **`monitor.py`**: `_pw_call_passes_alert_filters()` evaluates each non-suppressed PW call against `pw_alert_filters` config. Checks quarter, spread_role, margin, half, confidence, edge range, polarity, consensus, source. Sends Slack alert if call passes filters.
- **`monitor.py`**: When `pw_alert_filters.enabled` is true, old `predicted_winner_threshold` and `pw_underdog_alert` conditions are suppressed — prevents duplicate alerts.
- **`dashboard.html`**: "Save as Alert Filters" button on PW Live sub-tab. Reads current filter state, writes to config. Shows active filter summary with disable link.
- **`dashboard.html`**: PW Fires help modal rewritten to document all current features: Live/History sub-tabs, EV column, Q ML/Spr ROI filters, stats block sections, Slack Alert Filters, Forward Test.

Cross-repo: NBA #418.

**Files changed:** `monitor.py`, `dashboard.html`

---

## 2026-07-31 — fix: PW Live tab retains completed games after auto-refresh (#265)

- **`dashboard.html`**: `_pwRefreshLiveOnly()` merge branch added/updated games from `/api/pw-calls-live` but never removed games absent from the response. Completed games stayed visible on the Live tab indefinitely. Fixed by filtering `_pwLiveGames` to only retain games present in the fresh API response after merging.

Cross-repo: NBA #420.

**Files changed:** `dashboard.html`

---

## 2026-07-31 — feat: PW dashboard parity with NBA (#264)

- **`dashboard.html`**: Added Q ML ROI≥ and Q Spr ROI≥ filter controls to both PW Live and History sub-tabs (Gap 1). Wired into `_pwFilterLiveCalls`, `loadPWPanel` filter block, `_getPwFilters` save/restore, `_resetPwFilters`. Uses `_synthScenarioCache` for quarter ROI lookups.
- **`dashboard.html`**: Expanded Game History detail `renderPWCallsSection()` from 13 to 20 columns (Gap 2): added Score, Odds, Handicap, Live Spread, BK ML (+source), BK Spread (+price+source), Src (LIVE/PRE/SYNTH), EV, Details (consensus/blended badges + condition drilldown).

Cross-repo: NBA #419.

**Files changed:** `dashboard.html`

---

## 2026-07-31 — fix: server-side stale filter on /api/live-stats (#414 parity)

- **`server.py`**: `/api/live-stats` now clears game data when `ts` timestamp is older than 15 minutes. Returns `stale: true` and `stale_age_minutes`. Prevents serving frozen game state after monitor window closes. NRL dashboard had no stale detection — this is the first layer.

Cross-repo: NBA #414.

**Files changed:** `server.py`

---

## 2026-07-31 — fix: PW sub-tab layout — align stats block order with NBA/WNBA (#260)

- **`dashboard.html`**: Moved suppressed calls, price source, and EV summary lines inside the stats block (after BK Spread Units, before Q/H Performance). Added polarity & edge breakdown as a separate bordered block (Pol+/−/=, Edge≥10/5-10/0-5/<0, Leading/Level/Trailing) — was completely missing. Moved Best Filter Combos outside the stats block, after the polarity/edge block. Layout now matches NBA.

Cross-repo: NBA #415.

**Files changed:** `dashboard.html`

---

## 2026-07-31 — fix: BK Spread Units mismatch between stats block and Q/H Performance (#261)

- **`dashboard.html`**: Q/H Performance BK Spr computation now matches top stats block logic: pushes skipped, missing `bk_spread_price` skipped (no 90.91 fallback). Both quarterly and H1 blocks fixed.
- **`dashboard.html`**: Added ET row to Q/H Performance — ET calls were excluded (only Q1-Q4 initialized). ET row only renders when ET calls exist.

Cross-repo: NBA #416.

**Files changed:** `dashboard.html`

---

## 2026-07-30 — feat: Phase 5.3 — paper-trading dashboard panel (#258)

- **`server.py`**: `GET /api/paper-trading` endpoint — reads `bets.jsonl`, enriches with game outcomes from history, computes per-rule stats (bets, resolved, accuracy, mean EV).
- **`dashboard.html`**: Forward Test section in PW History stats area. Per-rule card with stats + expandable recent bets table (time, team, price, source, margin, result). Loaded on History tab only.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-30 — feat: NRL synth spread + moneyline with time remaining (#258 Phase 4.11)

- **`synthetic_odds.py`**: `compute_synth_spread` and `compute_synth_moneyline` accept `game_seconds`. Spread blends model → raw `-margin` as time elapses (0s=full model, 4800s=final margin). Moneyline uses continuous time multiplier 0.80→1.15 instead of 4-bucket quarter approximation. Backward-compatible (`game_seconds=None` uses old quarter buckets).
- **`monitor.py`**: passes `game_seconds` (game_minute × 60) to `compute_all` at PW call time and live stats computation.

**Files changed:** `synthetic_odds.py`, `monitor.py`

---

## 2026-07-29 — feat: Phase 5 — rules.json + bets.jsonl (#258)

- **`rules.json`** (new): Rule pre-registration with `pw-default-v1`.
- **`monitor.py`**: `bets.jsonl` forward-test log at PW call time. NRL closing prices already in `home_ml`/`away_ml`.

**Files changed:** `rules.json` (new), `monitor.py`

---

## 2026-07-29 — fix: combo Apply/Clear + exact units + scope fixes (#259, #258)

- **`dashboard.html`**: Apply on Live auto-switches to History. Clear reverts to pre-Apply state. `_pfx` in `loadPWPanel`. "No calls match" empty state. Combo units use raw P&L.
- **`server.py`**: Live Only requires `bk_ml_source`. All old `bk_moneyline` checks replaced.
- **`outcomes.py`**: `bk_odds_pnl`/`bk_spr_pnl` in scenario output. BK spread requires `bk_spread_price`.

**Files changed:** `dashboard.html`, `server.py`, `outcomes.py`

---

## 2026-07-29 — feat: Best Filter Combos Apply button + tighten Live Only (#259, #258)

(Superseded by entry above)

- **`dashboard.html`**: Apply button on combo rows. Subtitle clarifying combo scope.
- **`server.py`** + **`outcomes.py`**: Live Only requires `bk_ml_source`. All old `bk_moneyline` checks replaced. NRL 844 calls.

**Files changed:** `dashboard.html`, `server.py`, `outcomes.py`

---

## 2026-07-29 — feat: EV engine — expected value + edge per call (#258 Phase 4.10)

- **`server.py`**: EV + edge computed in `_enrich_pw_call`. NRL: 1,246 calls have EV, 953 +EV.
- **`dashboard.html`**: EV column + stats summary.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-28 — feat: model_schema_version on PW calls (#258 Phase 4.12)

- **`monitor.py`**: `_get_model_schema_version()` stamps `SCHEMA_VERSION` ("nrl_ml_v1") on every live PW call.

**Files changed:** `monitor.py`

---

## 2026-07-28 — feat: de-vig layer — multiplicative fair probability (#258 Phase 4.9)

- **`server.py`**: `_enrich_pw_call` computes `market_prob_devig` and `market_prob_raw` from `home_ml`/`away_ml` (decimal odds). NRL: 1,332/2,132 calls have de-vigged probabilities.

**Files changed:** `server.py`

---

## 2026-07-28 — fix: synth overround + remove fallback + push handling (#258 Phase 4.4, 4.8)

- **`synthetic_odds.py`**: `_prob_to_decimal` applies 5% vig. Synth prices no longer fair odds.
- **`dashboard.html`**: BK spread skips bets with no price (was 90.91 fallback). Pushes excluded from ROI. Level margin split added.
- **`outcomes.py`**: Coverage functions return `None` for pushes.

**Files changed:** `synthetic_odds.py`, `dashboard.html`, `outcomes.py`

---

## 2026-07-28 — feat: ROI sample size + price source tooltips (#258 Phase 4.2, 4.3)

- **`dashboard.html`**: ROI figures show `(n=X)`, suppressed below n=30. Price source tooltip + help update explaining PRE with Live Only (live calls without BK quote at fire). `outcomes.py` scenario stats fixed.

**Files changed:** `dashboard.html`, `outcomes.py`

---

## 2026-07-28 — feat: three-way price_source + Src column + PW help update (#258 Phase 4.2)

- **`server.py`**: `price_source` derived on read — `live`, `pregame`, or `synthetic`. Never written to disk.
- **`dashboard.html`**: "Src" column on PW call tables — LIVE/PRE/SYNTH badges. Stats block shows price source breakdown. PW filter help updated with Price Source section.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-28 — docs: Polymarket trading bot — requirements & technical review (NBA #214)

- **`docs/polymarket-trading-bot.html`**: Rev 1 requirements — 14-section HTML covering architecture, edge evaluation, risk management, hosting, and 5 implementation phases.
- **`docs/polymarket-trading-bot-review.html`**: Rev 2.3 technical review — 38 findings (6 critical, 11 high). Corrects Kelly formula, fee model, validation gates, hosting (Singapore blocked). Adds Phase 0 data capture, CLV as primary KPI, maker-first execution, Betfair alternative.

**Files changed:** `docs/polymarket-trading-bot.html` (new), `docs/polymarket-trading-bot-review.html` (new)

---

## 2026-07-28 — fix: Phase 4.0/4.1 — Live Only filter replaces Strict BK (#258)

- **`server.py`**: `_filter_strict_bk()` uses `call["synthetic"]` flag. Inline check updated. "Strict BK" renamed to "Live Only" in dashboard. **Verified: NRL 893 live-priced calls.**
- **4.0**: No duplicate `match_id`s found.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-27 — feat: Phase 3 CI + code quality (#258)

- **`.github/workflows/lint.yml`**: Added ruff check on push/PR (F401/F601/F811).
- **`backfill.py`**: Duplicate guard now unconditional — was gated on `--skip-existing` opt-in flag (3.8).
- **`requirements.txt`**: Pinned exact versions.
- Auto-fixed 13 unused imports.

**Files changed:** `.github/workflows/lint.yml` (new), `requirements.txt`, `backfill.py`

---

## 2026-07-27 — ops: disable NRL scheduled ML retrain (#258 Phase 1.8)

- Stopped and disabled `nrl-ml-train.timer`. Weekly retraining was re-fitting the leaked model (trains on final margin) on the same leaked features, freshening the artefact date without improving anything. Existing model continues serving. Timer re-enables after 4.6 (leakage fix) lands.

---

## 2026-07-27 — fix: Phase 2 security hardening (#258)

- **`server.py`**: Removed wildcard CORS header (2.1). `odds_api_key` now masked on `/api/config` GET via `SECRET_KEYS` constant (was leaking); POST re-merges all secrets (2.2/2.5). Static allowlist blocks `config.json` (2.2). `_safe_backup_name()` on delete + restore endpoints (2.3). Bound to `127.0.0.1` (2.4).

**Files changed:** `server.py`

---

## 2026-07-27 — fix: Phase 1 critical defects + heartbeat (#258)

- **`monitor.py`**: Fixed SO-01 — `now` hoisted to top of `main()` (was assigned at line 1246, causing `UnboundLocalError` at 4 sites in auto-recovery path). Recovery has been silently broken all season.
- **`server.py`**: Fixed SO-02 — `new_config` changed to `payload` (snapshot pruning has never run). Fixed SO-03 — `_enrich_pw_call` now returns a new dict instead of mutating the shared cache (cross-request data race in threaded server).
- **`dashboard.html`**: Phase 1.7 — monitor heartbeat staleness badge. "Monitor last polled" row shows warning when >5 minutes since last cycle.

**Files changed:** `monitor.py`, `server.py`, `dashboard.html`

---

## 2026-07-26 — feat: PW Live sub-tab game cards — pregame odds, spread, role header (#257)

- **`server.py`**: `/api/pw-calls-live` now reads `pregame_odds_<gid>` from `.alert_state.json` and includes `home_ml`, `away_ml`, `home_spread`, `away_spread`, `spread_fav` at the game level.
- **`dashboard.html`**: Live game cards now show a 2-3 line header matching History tab format:
  - Line 1: Pregame H2H odds + spread
  - Line 2: Role (FAV/DOG), pregame odds, spread, live spread, BK ML, BK Spread, polarity badge, edge badge (from latest call)
  - Line 3: Synthetic odds (Q ML, Q Spr) when available

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-25 — fix: PW Live sub-tab missing BK Spread + scenario ROI filter bypass (#255)

- **`server.py`**: `/api/pw-calls-live` now calls `_enrich_pw_call()` on each PW call before returning. Previously the lightweight live endpoint skipped enrichment, so `bk_live_line` and `live_line` were never computed — causing the BK Spread column to show "—" on the Live tab.
- **`dashboard.html`**: Live tab's 15s auto-refresh now applies scenario ROI and min calls/games filters to game cards via `_pwFilterLiveCalls()`. Previously, the auto-refresh from `/api/pw-calls-live` bypassed client-side scenario filtering, showing calls with negative ROI even when ROI≥0 filters were active.

Cross-repo: NBA #409.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-24 — feat: PW Live sub-tab — Live Games section with heading (#254)

- **`dashboard.html`**: Added structured "🔴 Live Games" section below stats block in PW Live sub-tab. Shows "Last update: HH:MM:SS · Refreshing every 15s" timestamp. Displays "No live games currently" empty state (stats still visible).

**Files changed:** `dashboard.html`

---

## 2026-07-24 — feat: PW Live sub-tab — full filters, stats, Q/H Performance, Best Combos (#253)

- **`dashboard.html`**: PW Live sub-tab now has same filters, stats, Q/H Performance, Best Filter Combos as History tab. All PW functions parameterized with `prefix` ('pw' or 'pw-live'). Full filter bar with `pw-live-*` element IDs, independent localStorage persistence. Stats from all history, game cards filtered to only live/recent source. Initial load + filter change fetches full `/api/pw-calls`; 15s auto-refresh uses `/api/pw-calls-live`.

**Files changed:** `dashboard.html`

---

## 2026-07-24 — fix: PW Live sub-tab stale games + match History tab rendering (#252)

- **`server.py`**: `/api/pw-calls-live` now filters out games with `game_end_{gid}` flag set and games whose latest PW call is older than 2 hours. Prevents stale SOO/completed games from appearing in Live sub-tab.
- **`dashboard.html`**: `_renderPwPage()` refactored to accept `{containerId, games, showAccuracy, showPagination}` options. Live sub-tab now reuses the same full game card rendering pipeline as History tab. Live top bar has reduced filter set: title + call count + suppressed toggle + collapse all + sort + call selection + refresh.
- **`dashboard.html`**: Fixed `_pwStartLiveTimer()` not starting — was gated on `_pwLiveGames` having data, but array is empty on first load (async fetch). Timer now starts unconditionally. Also fixed stale `game_end_` vs `history_recorded_` flag name.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-24 — feat: Backup Health Summary card (#250, NBA #403 Phase 5)

- **`dashboard.html`**: Health summary card at top of Backup & Recovery tab — Backup Health, Runtime age, Cloud age, WAL pending, Cloud errors, Last sync size.

**Files changed:** `dashboard.html`

---

## 2026-07-24 — feat: backup health alerts — stale/error warnings (#250, NBA #403 Phase 4)

- **`server.py`**: `_get_cloud_backup_status()` computes `age_hours` and `stale` (>36h). Displayed in Overview.
- **`dashboard.html`**: Cloud backup row shows "STALE" (blinking red) with age, or normal status with "(Xh ago)".

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-24 — feat: cloud backup history in Backup & Recovery tab (#250, NBA #403 Phase 3)

- **`server.py`**: Added `_get_cloud_backup_history()` and `GET /api/cloud-backup/history?last=N` endpoint.
- **`dashboard.html`**: "Cloud Backup History" section at bottom of Backup & Recovery tab — table of past 10 runs.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-24 — feat: cloud backup "Run Now" button (#250, NBA #403 Phase 2)

- **`server.py`**: Added `POST /api/cloud-backup/run` endpoint — spawns `cloud_backup.sh` in background. Guards against concurrent runs. Added `running` field to cloud backup status.
- **`dashboard.html`**: "Run Now" button on cloud backup status row. Shows "Running…" with blink while in progress.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-24 — feat: cloud backup log rotation (#250, NBA #403 Phase 1)

- **`cloud_backup.sh`**: Added log rotation at script start — rotates `logs/cloud_backup.log` when over 5MB, keeps 3 rotated files (`.1`, `.2`, `.3`).

**Files changed:** `cloud_backup.sh`

---

## 2026-07-24 — feat: cloud backup status in Monitor Status Overview (#249, NBA #402)

- **`server.py`**: Added `_get_cloud_backup_status()` — parses last run from `logs/cloud_backup.log` and queries `nrl-cloud-backup.timer` for next scheduled. Returns status, size, duration, shortened error messages. Added to `/api/monitor-status` response as `cloud_backup` object.
- **`dashboard.html`**: Added "Cloud backup" row to Monitor Status Overview showing status, last run, size, duration, errors, next scheduled.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-23 — perf: 30s monitor polling + 15s dashboard live refresh (NBA #400)

- **`systemd/nrl-monitor.timer`**: Added second `OnCalendar` line with `:*:30` offset for 30-second polling during game window (was 60s). Added `AccuracySec=1s`.
- **`dashboard.html`**: PW live sub-tab refresh interval reduced from 30s to 15s.

**Files changed:** `systemd/nrl-monitor.timer`, `dashboard.html`

---

## 2026-07-23 — perf: /api/pw-calls cache + /api/pw-calls-live + Live/History sub-tabs (#248)

- **`server.py`**: Added `_load_history_cached()` — mtime-keyed in-memory cache for `game_history.json` (NRL had no history cache). `build_pw_calls_payload` now uses cached reads (~0.9s → <1ms on cache hit). New `/api/pw-calls-live` endpoint reads only `.alert_state.json` for live/recent PW calls.
- **`dashboard.html`**: PW tab split into **Live** and **History** sub-tabs. Live sub-tab shows only live/recent games with 30s auto-refresh via `/api/pw-calls-live`. History sub-tab contains the full historical view with all filters, loaded on demand. Sub-tab state persisted to localStorage.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-07-23 — fix: flush alert state immediately after PW call append (NBA #399)

- **`monitor.py`**: `save_state()` now runs immediately after each PW call is appended to `.alert_state.json` in `_record_pw_call()`, instead of waiting until end of monitor cycle. Eliminates multi-minute delay between Slack alert delivery and PW tab visibility.

**Files changed:** `monitor.py`

---

## 2026-07-19 — feat: capture score_progression on live games (#246)

- **`monitor.py`**: Build `score_progression` at game-end from NRL match timeline via `timeline_features.extract_features()`. Previously only populated by backfill — live-captured games (62 with BK odds) lacked it, preventing `q_correct` computation. Now live games get the same scoring timeline as backfilled games, enabling full Q/H quarter stats.

**Files changed:** `monitor.py`

---

## 2026-07-19 — fix: Q/H Performance not showing with Strict BK (#245)

- **`monitor.py`**: Compute `q_correct`, `h1_correct`, `q_covered`, `h1_covered` at game-end enrichment from `score_progression` and halftime scores. Previously only set by `backfill_q_correct.py`, so live calls (which have BK odds and pass Strict BK filter) lacked these fields, causing the entire Q/H Performance block to be hidden.
- **`server.py`**: Compute `q_correct`/`h1_correct`/`q_covered`/`h1_covered` at serve-time in `_enrich_pw_call()` for records missing these fields. Uses `score_progression` from game record + `synth_spread` for coverage. Ensures existing historical records get Q/H stats without re-running backfill.
- **`dashboard.html`**: Q/H Performance row visibility now also checks `bkMlTotal`/`bkSprTotal` — rows with only BK game-level stats (no synth/quarter data) now appear. Live-captured games lack `score_progression` so `q_correct` stays null, but BK game-level ML/Spread per quarter still displays.

**Files changed:** `monitor.py`, `server.py`, `dashboard.html`

---

## 2026-07-18 — feat: Season/segment columns in PW tab and Game History (#244, NBA #393)

- **`dashboard.html`**: PW tab game cards now show season (shortened, e.g. "25") and segment (e.g. "Regular", "State of Origin") in card header after team names. Game History table adds Season and Segment columns after Date. Added `seasonDisplayName()` and `segmentDisplayName()` helper functions.
- **`server.py`**: `/api/pw-calls` now includes `season` field in game entries.

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-07-18 — feat: Quarter/Half PW Performance section in PW tab (#245, NBA #374)

- **`dashboard.html`**: Added Q/H Performance block to PW tab stats area showing per-quarter (Q1–Q4) and half (H1) PW accuracy and ROI breakdown. Rows: Q1, Q2, Q3, Q4, H1 (Q1+Q2 fires). Columns per row: Won (always), ML Won(S) + Units(S) (synth ML, decimal odds), Covered(S) (synth spread, -110 juice), BK ML (game-level BK odds by firing quarter), BK Spr (game-level BK spread by firing quarter). H1 row omits ML Won column (`synth_h1_ml` always null for NRL). All quarter ML/Spread columns show `(S)` suffix — NRL has no BK quarter/half markets. Color coding: green ≥55%, yellow ≥50%, red <50%. Respects all active PW filters.

**Files changed:** `dashboard.html`

---

## 2026-07-17 — feat: PW extra stats — BK ML/Spread units (#242, NBA #377)

- **`dashboard.html`**: PW top stats bar now shows **BK ML Units** and **BK Spread Units** pills after their respective ROI metrics. Per-game cards show BK ML Units, BK Spread Covered, and BK Spread Units. Scenario badges (ML/Spr) include unit tally after ROI%. New "Best Filter Combos" section sweeps ~2K threshold combinations (ML ROI%, Spr ROI%, Min Calls, Min Games) and shows top 3 by BK ML Units and top 3 by BK Spread Units. All using NRL decimal odds (`profit = STAKE * (odds - 1)`).

**Files changed:** `dashboard.html`

---

## 2026-07-17 — feat: Help modal for PW tab Best Filter Combos (#243)

- **`dashboard.html`**: Added `?` help button and modal to the Best Filter Combos section header. Modal explains the ~2K threshold sweep algorithm, all 7 table columns (ML ROI≥, Spr ROI≥, Min Calls, Min Games, Scenarios, Bets, Units), and how to use results to set scenario ROI filters. Cross-post from NBA #392.

**Files changed:** `dashboard.html`

---

## 2026-07-14 — feat: Aggregate synth scenario badges (#239)

- **`dashboard.html`**: Synth badges now show aggregate scenario stats matching BK badge format: won/total, ROI%, game count per 4D scenario key (role|margin|edge|quarter). Client-side `_buildSynthScenarioCache()` computes Q Won/ML/Spr and H1 Won/Spr aggregate stats. NRL decimal odds ROI. No H1 ML badge (synth_h1_ml always null). Replaces per-call badges from #238.

**Files changed:** `dashboard.html`

---

## 2026-07-13 — feat: backfill q/h1_correct + synth Q/H1 badges (#238)

- **`backfill_q_correct.py`** (new): Backfills `q_correct` and `h1_correct` onto PW call records. `q_correct` derived from `score_progression` quarter boundaries (Q1=0-1200s, Q2=1200-2400s, Q3=2400-3600s, Q4=3600-4800s). `h1_correct` derived from `home_score_1h`/`away_score_1h` (Q1/Q2 calls only). Backfilled 1,218 q_correct (72.3% accuracy) and 339 h1_correct (85.5% accuracy).
- **`dashboard.html`**: `renderSynthBadges(call)` function renders synthetic quarter/half badges on PW calls. Badges: Q Won(S) amber, Q ML(S) cyan, Q Spr(S) violet; H1 Won(S) dark amber, H1 Spr(S) indigo. NRL decimal odds ROI: `profit = STAKE * (odds - 1)`. "Synth Badges" toggle checkbox (default checked) with filter persistence. Badges added to PW tab Details cell and Game History PW calls Scenario column.

**Files changed:** `backfill_q_correct.py` (new), `dashboard.html`, `CHANGES.md`

---

## 2026-07-13 — feat: synthetic odds — module, backfill, monitor, dashboard (#235)

- **`synthetic_odds.py`** (new): Pure-function module generating synthetic live odds from game state. NRL-calibrated: decimal odds via `_prob_to_decimal()`, logistic k=0.14, spread slope=-0.953, quarter multipliers Q1:0.80/Q2:0.90/Q3:1.0/Q4:1.10/ET:1.15, pregame coefficient 0.015. H1 ML always returns None (NRL lacks h1_correct data). `compute_all(margin, quarter, pregame_ml_decimal)` returns `{moneyline, spread, h1_ml}`.
- **`backfill_synth_odds.py`** (new): Backfills `synth_moneyline`, `synth_spread`, `synth_h1_ml` onto all historical PW call records. Pregame ML from each call's `moneyline` field (decimal odds). Backfilled 1,937 calls.
- **`monitor.py`**: Adds `synth_odds` to live_stat entries (home perspective) for live dashboard display. Adds `synth_moneyline`, `synth_spread`, `synth_h1_ml` fields to PW call records at creation time.
- **`dashboard.html`**: "Synth" column added to PW tab per-call tables and Game History PW calls table. Format: `ML:X.XX Spr:+/-Y.Y` (decimal odds for ML, signed for spread). Color: `var(--muted)` when BK odds present, `#94a3b8` when absent. Synth line added to Live Games PW card display.

**Files changed:** `synthetic_odds.py` (new), `backfill_synth_odds.py` (new), `monitor.py`, `dashboard.html`

---

## 2026-07-12 — feat: BK Live Odds Analysis report + dashboard tab (#235)

- **`analyse_bk_odds.py`** (new): Analysis script examining correlation between game state (margin, game minute, pregame odds, stats) and bookmaker live odds. Loads 581 PW calls with BK odds from 1,040 games. Runs regression analysis and outputs self-contained HTML report.
  - Key findings: margin alone R²=0.28, margin+quarter R²=0.33, pregame moneyline adds +0.28 R² (dominant signal), stats add +0.09. Spread: margin R²=0.75, slope≈-0.96.
- **`server.py`**: `/api/generate-bk-odds-report` POST endpoint — regenerates report on demand.
- **`dashboard.html`**: "BK Odds" tab in Analysis panel — iframe with Regenerate/Refresh buttons, auto-resize.

**Files changed:** `analyse_bk_odds.py` (new), `server.py`, `dashboard.html`

---

## 2026-07-10 — PW scenario badges: live games + null edge handling (#233, #234)

- **`dashboard.html`**: Fixed scenario badges not rendering on live game cards — `_scenarioStatsCache` was only populated when PW tab was opened. `loadLiveStats()` now fetches scenario stats in parallel with live stats (skipped if cache exists). Null `avg_edge` treated as `0-5` bucket in `_edgeRangeKey()` so scenario badges always render when role/margin/quarter exist. Edge column shows `—` in muted color for null edge instead of misleading `+0.0`. Date column in Spread Strategy game history table no longer wraps (`white-space:nowrap`).
- **`outcomes.py`**: `_edge_range_key()` returns `'0-5'` for `None`/invalid edge (was returning empty string, preventing scenario key formation).

**Files changed:** `dashboard.html`, `outcomes.py`

---

## 2026-07-04 — BK Spread: game list muting + title (#232)

- **`dashboard.html`**: Dimension columns (Context, Entry Min, Spread Price, Margin, EV Edge) mute to grey when their filter is not active — highlights filtered dimensions visually. Non-dimension columns never muted. Added "Spread Strategy — Game History (X bets / Y games)" title above table, updates with filtered bet count. Spread Price and EV Edge preserve custom colors via `data-orig-color` attribute.

**Files changed:** `dashboard.html`

---

## 2026-07-04 — BK Spread: threshold consistency + "Best" option (#231)

- **`odds_monitor.py`**: `compute_spread_analysis()` accepts `threshold` param — when provided, uses it for strategy collection instead of sweep-best. Returns `analysis_threshold` in response (distinct from `best_threshold`).
- **`server.py`**: `/api/odds-monitor-analysis` accepts `threshold` query param (float or "best").
- **`dashboard.html`**: Added "Best" checkbox next to price threshold. When unchecked (default), analysis uses user's price threshold for consistent pattern→game history click-through. When checked, uses sweep-best. Sweep table highlights active threshold row. Pattern labels show `[XD @$Y.Z]`. Price input disabled when "Best" active. Pattern click-through reloads game history when thresholds differ. Added Spread Price filter ($3-4/$4-5/$5+) — all 7 pattern dimensions now have history table filters. Setting persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-07-04 — BK Spread: multi-dimension combos 2D-5D (#230)

- **`odds_monitor.py`**: `compute_spread_analysis()` accepts `combo_min`/`combo_max` params (default 2-3). Uses `itertools.combinations` to generate combos at each dimension level from 7 core dimensions. New combo entry format: `dims`, `dimensions[]`, `values[]`, `combo_label`. 2-3D: 379 combos, 2-5D: 1001 combos.
- **`server.py`**: `/api/odds-monitor-analysis` accepts `combo_min`/`combo_max` query params (2-5, validated).
- **`dashboard.html`**: Controls row in Spread Analysis: combo dims min/max dropdowns + Min Games/Min ROI inputs. Patterns show dimension level label ([2D], [3D], etc.) with `combo_label`. "Top 2D Combos" replaced with "Top Combos" (merged multi-level, top 30). Multi-dim pattern click-through via `_levSetMultiFilters()` — resets all filters before setting pattern dims, uses `&quot;` escaping for JSON in onclick attributes. Settings persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-07-03 — BK Spread: merged strategies, 7D patterns, 5D combo table (#229)

- **`odds_monitor.py`**: `compute_spread_analysis()` now runs both leading + trailing strategies per game (role param removed). Each strategy tagged with `margin_context` (leading/trailing). Pattern detection expanded to all 21 2D pairs from 7 dimensions (side, role, margin, entry_minute, margin_context, spread_price, ev_edge). Added 5D combo table (side × role × margin × entry_minute × margin_context). `margin_context` added to dimensional breakdowns.
- **`server.py`**: `/api/odds-monitor-leading` always computes both strategies per game with merged aggregates. `/api/odds-monitor-analysis` no longer accepts role param.
- **`dashboard.html`**: Removed Role dropdown from BK Spread header. History table renders both strategies per game with Context column (Lead/Trail). Added Context filter dropdown. Spread Analysis gains sortable 5D Combo Table. `_levSavePrice()` saves threshold to both config keys.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-07-02 — Historical Win Rate: Min Games/pp, margin 3-bucket, dual patterns (#228)

- **`odds_monitor.py`**: Changed `_MARGIN_RANGES` from 4 buckets (0-6/7-12/13-20/21+) to 3 (0-6/7-12/13+). Updated BK spread analysis `_bucket()` margin to match. `margin_bucket` filter now accepts comma-separated values for multiselect.
- **`dashboard.html`**: Margin filter changed from dropdown to multiselect checkboxes. Added Min Games (default 5) and Min pp (default 10) client-side filters — hide dimensional/pattern/combo rows below thresholds. Final Stretch perspective shows dual Leading + Trailing pattern tables side by side via two additional API calls.

**Files changed:** `odds_monitor.py`, `dashboard.html`

---

## 2026-07-01 — Historical Win Rate: filters, 5D combo table, game list (#227)

- **`odds_monitor.py`**: Extended `compute_historical_win_analysis()` with server-side filters (season, segment, side, role, margin context, margin bucket), multi-minute evaluation (entry minute becomes a dimension with individual values), 5D combo table computation (side × role × margin_context × margin × entry_minute), and paginated game list output. Strict minutes validation to {65, 68, 70, 72, 75}.
- **`server.py`**: Extended `/api/odds-monitor-historical` with filter + pagination query params. Backwards compatible with old `minute=` param.
- **`dashboard.html`**: Replaced historical view controls with full filter bar (7 dropdowns + minute checkboxes + Reset). Added sortable 5D combo table with click-through to filters. Added paginated game history list (25/50/100 page sizes, First/Prev/Next/Last navigation). All filters persisted to localStorage.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-30 — Spread Strategy: Historical Win Rate sub-view + BK enrichment (#226)

- **`odds_monitor.py`**: New `compute_historical_win_analysis()` — analyses 870+ games from game_history + PBP cache to determine win rate of leading/trailing team at a configurable minute (default 70). Three perspectives: Leading (93.1% win rate), Trailing (6.9% comeback rate), Final Stretch (who outscores in last 10 min — score reset to 0-0, ~50% baseline). 10+1 dimensions, 11 2D combo pairs, pattern detection. PBP cache helpers: `match_id_to_cache_path()`, `score_at_minute()`. Existing `compute_spread_analysis()` enriched with 5 new game_history dimensions (halftime_context, margin_trajectory, key_conditions, postgame_tags, spread_line_size) + 4 new combo pairs.
- **`server.py`**: New `/api/odds-monitor-historical` endpoint (params: role, minute). Existing `/api/odds-monitor-analysis` now passes game_history for dimensional enrichment.
- **`dashboard.html`**: View toggle (BK Spread / Historical Win Rate) in Spread Strategy tab. Historical view with Perspective dropdown (Leading/Trailing/Final Stretch), Minute selector, baseline stats card, dimensional breakdowns, notable patterns, 2D combos. BK-only controls hidden in historical view. localStorage persistence for view, minute, perspective.

**Key investigation findings:** `has_halftime_turnaround` condition is the strongest predictor — drops leading team win rate to 64.2% (32.8pp spread). Margin at 70 (23.6pp), postgame tags (27.4pp), halftime context (20.6pp) also strongly predictive. "pp" = percentage points (absolute difference between two percentages).

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-28 — Live Games: Q1-Q4 quarter stat breakdowns (#225)

- **`monitor.py`**: Captures stats snapshots at synthetic quarter boundaries (~min 20 for Q1, ~min 60 for Q3). Computes Q1-Q4 stats by subtraction from boundary snapshots. Rate stats (Poss%, Comp%, etc.) shown as cumulative at boundary. Flags approximate data when boundary was missed. New live_stat fields: `home_stats_q1`..`home_stats_q4`, `away_stats_q1`..`away_stats_q4`, `_q1_approx`, `_q3_approx`.
- **`dashboard.html`**: Q1/Q2/Q3/Q4 buttons on a second row below Full/1H/2H. Each shows the 12-stat grid for that quarter. Approximate quarters flagged with ⚠ on the button. `_switchStatHalf()` extended for quarter scopes with cross-row button highlighting via `data-stat-btns` wrapper.

**Files changed:** `monitor.py`, `dashboard.html`

---

## 2026-06-27 — Spread Strategy: dimension filters on history table (#224)

- **`dashboard.html`**: 5 filter dropdowns (Side, Role, Margin, Entry Minute, EV Edge) above the per-game history table. Client-side row filtering via data attributes. Filtered aggregate stats (bets, covered, P&L, ROI) shown when filters active. Pattern click-through from Spread Analysis auto-sets filters and scrolls to matching games. localStorage persistence. Reset button.

**Files changed:** `dashboard.html`

---

## 2026-06-27 — Spread Analysis: favorite/underdog dimension (#223)

- **`odds_monitor.py`**: `compute_spread_analysis()` now derives favorite/underdog role from moneyline odds at entry snapshot. Added as 9th dimension + 4 new 2D combo pairs (side×role, role×margin, role×entry, role×ev_edge).
- **`dashboard.html`**: Help modal updated with Role dimension description.
- Finding: `away+favorite` covers 3/5 (+95% ROI) vs `home+favorite` 3/15 (-32% ROI) — the away-leading signal is specifically about favorites leading away from home.

**Files changed:** `odds_monitor.py`, `dashboard.html`

---

## 2026-06-26 — Spread Strategy: automated spread analysis (#216)

- **`odds_monitor.py`**: New `compute_spread_analysis()` — multi-threshold sweep ($1.50–$6.00), dimensional breakdowns (side, margin, entry minute, total points, EV edge, spread price, team, competition), 2D combo analysis, profitable/anti pattern detection with low confidence warnings. Auto-logs notable +ROI patterns after each `persist_game()`.
- **`server.py`**: New `/api/odds-monitor-analysis` endpoint accepting `role` param.
- **`dashboard.html`**: Collapsible "Spread Analysis" section in the Spread Strategy tab — notable patterns card, threshold sweep table (best threshold highlighted), dimensional breakdown tables, top 2D combos table. Loads alongside main data, updates with role changes. Help modal documenting all sections (#222). Null guard for ROI/PnL `toFixed()`.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-26 — Spread Strategy: threshold filter recomputes history (#220)

- **`odds_monitor.py`**: `_compute_leading_strategy()` and `_compute_trailing_strategy()` accept optional `threshold` param that overrides stored `qualifies` flag, checking `spread_price >= threshold` directly.
- **`server.py`**: `/api/odds-monitor-leading` accepts `threshold` query param. Always recomputes strategies with the provided threshold instead of using stale stored values.
- **`dashboard.html`**: Price threshold input now has `onchange` triggering immediate refresh. `loadLeadingEv()` passes threshold to the API. Added Refresh button. Price input persists across role changes (#221) — "Set as default" saves per-role to config.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-25 — fix: PW client-side filters used unfiltered scenario stats (#218, #219)

- **`dashboard.html`**: `renderScenarioBadge()` was reading unfiltered `call.scenario_stats` before the filtered `_scenarioStatsCache`, so badges showed all-history stats even with source/season filters active (#218). ROI and Min Calls/Games filters had the same issue — filter decisions used all-history stats instead of filtered stats (#219). All three PW tab consumers now prefer `_scenarioStatsCache` (filtered) over `call.scenario_stats` (unfiltered). `forceShow` paths (Live Games, Game History) remain unfiltered.
- Cross-ref: NBAMonitor#372, NBAMonitor#373

**Files changed:** `dashboard.html`

---

## 2026-06-25 — PW scenario badges: filter-aware stats (#217)

- **`outcomes.py`**: `compute_scenario_stats()` now accepts `source` and `strict_bk` params — filters PW calls by source (live/backfill via `synthetic` flag) and genuine BK odds presence.
- **`server.py`**: `/api/pw-scenarios` passes `source` and `strict_bk` query params through to `compute_scenario_stats()`.
- **`dashboard.html`**: `_fetchScenarioStats()` now passes current PW filter state (source, strict_bk, call_selection, season_segment, season_year) to `/api/pw-scenarios` and re-fetches on every filter change. Scenario badges now reflect active filters instead of always showing unfiltered history.
- Per-call `scenario_stats` enrichment in `/api/pw-calls` remains unfiltered (Live Games and Game History badges show stable global stats).
- Cross-ref: NBAMonitor#371

**Files changed:** `outcomes.py`, `server.py`, `dashboard.html`

---

## 2026-06-25 — PW: Min Calls + Min Games scenario filters (#214)

- **`outcomes.py`**: `compute_scenario_stats()` now tracks `total_games` — distinct game count per scenario key. Included in `/api/pw-scenarios` response.
- **`dashboard.html`**: Scenario badge shows game count as `(Xg)`. New "Min Calls≥" and "Min Games≥" number inputs in the scenario filter group, AND'd with existing ROI filters. Persisted to localStorage.
- **`audit_roi.py`**: `total_games` added to manual recomputation and all field comparison tuples.
- Cross-ref: NBAMonitor#370

**Files changed:** `outcomes.py`, `dashboard.html`, `audit_roi.py`

---

## 2026-06-24 — Odds Monitor: role selector + manual spread price (#213)

- **`odds_monitor.py`**: Added `_compute_trailing_team_ev()` and `_compute_trailing_strategy()` — mirrors leading team logic for the trailing team's spread. Trailing EV now included in every snapshot. `persist_game()` stores `trailing_strategy` alongside `leading_strategy`.
- **`server.py`**: `/api/odds-monitor-leading` accepts `role` param (`leading`/`trailing`) to switch strategy view.
- **`dashboard.html`**: Spread Strategy tab (renamed from "Leading Team EV") now has a role dropdown (Leading/Trailing) and manual price threshold input with "Set as default" button that saves to config. Trailing threshold added to Configuration settings.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-24 — PW scenario Spread badge shows covered/total (#212)

- **`outcomes.py`**: Added `bk_spr_covered` counter to `compute_scenario_stats()` — tracks how many calls covered the BK spread line, independent of win/loss.
- **`dashboard.html`**: `_renderOneScenarioBadge()` Spread badge now shows `covered/bk_spr_count` instead of `correct/total_calls`. ML badge unchanged (still shows `correct/total`). Tooltip updated with "Covered" label for Spread.
- **`monitor.py`**: Slack alert scenario line updated to `W:won/total ML:roi Cov:covered/spr_count Spr:roi`.
- **`audit_roi.py`**: Added `bk_spr_covered` to manual accumulator and all field comparison tuples.
- Cross-ref: NBA Monitor #369.

**Files changed:** `outcomes.py`, `dashboard.html`, `monitor.py`, `audit_roi.py`

---

## 2026-06-22 — Odds Monitor: Leading Team Spread Strategy (#211)

- **`odds_monitor.py`**: Added leading team EV analysis to each snapshot — computes historical probability of the leading team scoring 1+ try in remaining time, compares against implied probability from spread price. New `leading_team_ev` field per snapshot and `leading_strategy` result per persisted game.
- Strategy: when the leading team's spread price >= $3.00 (configurable), record a 1u bet on their spread. Track whether they covered + P&L.
- Historical probabilities computed from `game_history.json` using `score_progression` to determine who leads at each time cutoff and `try_events` for the leading team's late scoring.
- **`server.py`**: New `/api/odds-monitor-leading` endpoint returning games with leading strategy results + aggregate stats + leading team probability reference.
- **`dashboard.html`**: New "Leading Team EV" sub-tab under Analysis with aggregate stats card (bets, wins, P&L, ROI), historical probability reference table, and per-game history table.
- Config: `odds_monitor_leading_threshold` (default 3.0) — exposed in Configuration → Settings → Odds Monitor.
- Backfilled all 32 existing `odds_monitor_history.json` records (21 triggered, 6W/14L, -5.8% ROI).

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`, `config.example.json`

---

## 2026-06-21 — Logs tab: aggregate current + rotated logs across monitor and dashboard streams

- **`server.py`**: Fixed the Monitor Status → Logs structured feed to read from both `logs/monitor.log` and `logs/dashboard.log` instead of only `monitor.log`.
- Extended log discovery to include rotated files (`*.YYYY-MM-DD`) for both logical streams, added a `log-files` payload for the dashboard, and allowed structured log queries to filter by logical log name plus entry timestamp date range across active + archived files.
- Raw log viewing can now open archived rotated log files, while live tail remains limited to the current active file.
- **`dashboard.html`**: Added a **Clear tags** button to drop all tag chips at once, plus a **Timeframe** selector with `Today`, `Last 24h`, `Last 3d`, and `Last 7d` presets wired into the existing date filters.
- Structured log cards now show **both local time (Australia/Sydney)** and the original **UTC** timestamp for each parsed log event. Raw log lines remain unchanged and continue to show the original UTC line text.
- This restores log cards when the monitor log is empty but dashboard/server activity is still being written to `dashboard.log` (for example rotation, model-eval snapshots, and other always-on server events).
- Added `source` metadata to structured log events and corrected `total_lines` to reflect the combined inputs.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-06-20 — ROI Report: add BK Spread covered count column to scenario tables

- **`pw_roi_report.py`**: Added a new **Covered** column to the **Best BK Scenarios** and **Worst BK Scenarios** tables, showing BK Spread covers as `yes/total` (for example `5/8`).
- The covered count now travels through both the server-generated BK scenario dataset and the client-side recomputation path, so it stays correct after sort/filter changes.
- **`audit_roi_report.py`**: Extended BK scenario parity checks to validate the embedded covered-count field (`bks_yes`) as part of the generated artifact audit.

**Files changed:** `pw_roi_report.py`, `audit_roi_report.py`, `pw_roi_report.html`

---

## 2026-06-20 — ROI Report: add bankroll simulation column to Best/Worst BK Scenarios tables (#207)

- **`pw_roi_report.py`**: Added a new **Bankroll ($10k @ $100/bet)** column to the **Best BK Scenarios** and **Worst BK Scenarios** tables in the ROI Report.
- Shortened the visible column heading to **Bankroll** and moved the `$10k @ $100/bet` explanation into a tooltip to keep table widths tighter.
- Bankroll simulates an independent **$10,000** starting balance with **$100 flat stake per qualifying bet** for each scenario row.
- The bankroll column follows the active bookmaker stream for the table: **BK Odds** when sorting by BK Odds ROI, otherwise **BK Spread**.
- Added bankroll sort options for both streams: **Bankroll (BK Spread)** and **Bankroll (BK Odds)**.
- Added bankroll fields to the embedded BK scenario data so the report audit can validate the new values end-to-end.

**Files changed:** `pw_roi_report.py`, `pw_roi_report.html`, `audit_roi_report.py`

---

## 2026-06-20 — Audit and validate cross-league PW ROI Report correctness end-to-end (#206)

- **`audit_roi_report.py`**: New end-to-end audit for the generated cross-league ROI report. Validates embedded report data, summary sections, BK scenario / heatmap source rows, narrative blocks, filter metadata, and the copy-to-NBA artifact path.
- **`pw_roi_report.py`**: Embedded a structured audit payload in the generated HTML so the report artifact can be validated directly.
- **Cross-league ROI report fixes:** normalized derived `spread_role` values to lowercase and excluded missing `avg_edge` values from edge-bucket breakdowns. This fixed report summary drift in role and edge sections.
- Cross-repo extension of NRL #205 and NBA #367.

**Files changed:** `audit_roi_report.py` (new), `pw_roi_report.py`, `pw_roi_report.html`

---

## 2026-06-20 — Audit and validate scenario BK ML ROI + BK Spread ROI correctness (#205)

- **`audit_roi.py`**: End-to-end audit script now validates all 5 claimed surfaces for NRL: PW tab accuracy block from `/api/pw-calls`, scenario badges via `/api/pw-scenarios`, `/api/analysis` PW accuracy summary, `/api/pw-matrix`, and `/api/pw-roi-combos`.
- **Scenario completeness audit:** Fixed the theoretical scenario buckets to match NRL’s actual quarter keys (`Q1`-`Q4`, `ET`) instead of the incorrect half-based assumption.
- **`dashboard.html`**: Fixed client-side spread cover bug — replaced `g.final_margin` (always positive) with `_teamMargin(g, c)` (signed from predicted team's perspective) for pregame spread, live line, and BK spread cover calculations. 40/355 calls were affected.
- Cross-repo: NBA #367.

**Files changed:** `audit_roi.py` (new), `dashboard.html`

---

## 2026-06-19 — Remove Sportsbet from bookmaker priority list (#204)

- Removed `"sportsbet"` from `BOOKMAKER_PRIORITY` in `odds_api.py`. TAB is now the primary bookmaker.
- New priority order: TAB → PointsBet AU → Ladbrokes AU → Unibet → Neds → TABtouch → BetRight → Betr AU → PlayUp → Betfair Exchange AU.
- **Odds API Usage tab**: added bookmaker priority display showing the full priority chain with the primary bookmaker highlighted.
- Updated help modal, game detail priority string, `CLAUDE.md`, and `README.md`.
- Historical game records with `odds_bookmaker: "SportsBet"` are unaffected.

**Files changed:** `odds_api.py`, `dashboard.html`, `CLAUDE.md`, `README.md`

---

## 2026-06-19 — feat: PW Accuracy block — BK ML Won and BK Spread Covered stats (#202)

- **BK ML Won**: `correct/total (X%)` — predicted team win rate for calls with BK moneyline odds.
- **BK Spread Covered**: `covered/total (X%)` — spread cover rate for calls with BK spread data.
- Both displayed alongside existing BK ML ROI and BK Spread ROI pills.
- Cross-repo: NBA #364.

**Files changed:** `dashboard.html`

---

## 2026-06-18 — feat: PW tab help modal (#201)

- Added `?` help button to the PW Fires tab header that opens a modal explaining all filters, checkboxes, scenario badges, and persistence behavior.
- Uses `--nrl-green` accent for section headers consistent with NRL dashboard.
- Cross-ref: NBA #363.

**Files changed:** `dashboard.html`

---

## 2026-06-18 — feat: PW scenario dual badges — BK ML ROI + BK Spread ROI (#200)

- **`outcomes.py`**: `compute_scenario_stats()` now computes both BK ML ROI and BK Spread ROI per scenario. Spread ROI uses `_did_team_cover_bk_line()` + `bk_spread_price` (decimal odds, fallback -110 juice). New output fields: `bk_spr_roi_pct`, `bk_spr_count`. New helpers: `_did_team_cover_bk_line()`, `_decimal_odds_profit()`, `FLAT_WIN_PROFIT` constant.
- **`dashboard.html`**: Replaced single ROI+/Acc≥ filters with dual checkbox+spinner: ML ROI (cyan #06b6d4) + AND/OR toggle + Spr ROI (violet #8b5cf6). Updated `renderScenarioBadge()` with `forceShow` param and `_renderOneScenarioBadge()` helper. Live Games and Game History use `forceShow=true` (always show both). Updated filter persistence (`_getPwFilters`, `_resetPwFilters`, `_initPwFilterValues`).
- **`monitor.py`**: Slack alerts now show both ML and Spread ROI: `Scenario: {label} {correct}/{total} ML:{roi} Spr:{roi}`.
- Cross-ref: NBA #362.

**Files changed:** `outcomes.py`, `dashboard.html`, `monitor.py`

---

## 2026-06-18 — fix: Log rotation never fires — rotation hour outside game window (#198)

- **Bug:** Log rotation was configured (`rotation_hour_utc: 14`) but never fired because the monitor only runs during the game window (UTC 04:00–12:59). `monitor.log` grew to 60K+ lines without rotation.
- **Root cause:** NRL had the log rotation UI config in the dashboard but no rotation code was implemented in any Python file.
- **Fix:** Added self-contained log rotation to `server.py` (dashboard server runs 24/7) as a background worker thread. Supports copy-truncate, daily period, retention pruning, and state persistence.
- Cross-ref: NBA #360 (same issue for WNBA).

**Files changed:** `server.py`

---

## 2026-06-18 — fix: PW tab scenario badge stats change with call_selection filter (#197)

- **Bug:** Scenario badge on a PW call row showed different stats depending on Call Selection filter (All calls vs First call only).
- **Root cause:** `/api/pw-calls` scenario enrichment passed `call_selection` to `compute_scenario_stats()`, filtering the historical call population.
- **Fix:** Removed `call_selection` parameter. Scenario stats now always computed from full unfiltered history.
- Cross-repo: NBA #359.

**Files changed:** `server.py`

---

## 2026-06-18 — feat: Add game time display to PW tab live game card header (#196)

- Moved game time (period + clock) and live score to top header line, right after the LIVE badge.
- **Fix:** Server now updates existing games with current `period`/`clock`/score from live_stats. Previously games with PW calls were skipped entirely in the live merge loop.
- Dashboard falls back to latest call's period/clock when live_stats unavailable.
- Cross-repo: NBA #358.

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-06-18 — fix: PW tab broken — pw_matrix not imported in build_pw_calls_payload (#195)

- **Bug:** PW tab showed "0 calls across 0 games" after #193 changes. `/api/pw-calls` returned 500 error: `name 'pw_matrix' is not defined`.
- **Cause:** BK spread price ROI change in #193 used `pw_matrix._safe_float()` / `pw_matrix._decimal_win_profit()` inside `build_pw_calls_payload()`, which doesn't import `pw_matrix`.
- **Fix:** Replaced with module-level `_parse_moneyline()` / `_decimal_ml_profit()` already available in `server.py`.

**Files changed:** `server.py`

---

## 2026-06-18 — docs: Align ROI Matrix help format with NBA and add BK ML ROI (#194)

- Rewrote ROI Matrix help modal to match NBA structure: `<h3>` section headers, `<ul>` bullet lists, individual metric descriptions.
- Added all 7 cell metrics (Games Won%, F, ML, Spr, BkS, BkML, Nc/Ng) with full descriptions.
- Expanded Pin controls, drill-down, and tips sections to match NBA detail level.
- Added BK ML ROI to PW Trends ROI Metrics help section.
- NRL-specific adaptations: Half (H1/H2/ET) instead of Quarter, decimal odds references.
- Cross-ref: NBA #357.

**Files changed:** `dashboard.html`

---

## 2026-06-18 — feat: Capture BK spread price and rename BK Odds → BK ML (#193)

### Problem
PW call records stored the bookmaker spread line but not the spread price (juice/vig). BK Spread ROI used hardcoded values (90.91 or 100.0) instead of actual bookmaker price. Display label "BK Odds" was ambiguous.

### Data flow changes
- **`monitor.py`:** PW call records now include `bk_spread_price` extracted from `live_odds` (already captured in `odds_api.py` as `home_spread_price`/`away_spread_price`).

### Display label renames
- "BK Odds" → "BK ML" (column headers in PW call tables, PW Trends, ROI Matrix, edge range breakdown)
- "BkO" → "BkML" (compact ROI badge labels)
- "BK Odds ROI" → "BK ML ROI" (stats block, filter dropdown, ROI Combos)
- "BK (Odds/Spr)" → "BK (ML/Spr)" (Game History PW column header)
- "BK Odds / BK Spread P&L" → "BK ML / BK Spread P&L" (tooltip)

### BK Spread price displayed in 3 places
1. **PW tab game card headers** — prediction and winner summary lines show spread price in parentheses (e.g., `BK Spread: -3.5 (1.91)`)
2. **PW tab per-call table rows** — price in parentheses after BK Spread value
3. **Help text** updated to describe actual spread price usage

### BK Spread ROI uses actual price
- `pw_matrix.py`: bucket accumulation uses `_decimal_win_profit(bk_spread_price)` with `FLAT_WIN_PROFIT` fallback
- `pw_trend.py`: trend snapshot computation uses actual price with `STAKE` fallback
- `server.py`: PW summary and PW accuracy ROI calculations use actual price
- `dashboard.html`: client-side PW tab stats block uses decimal odds profit formula with 90.91 fallback
- All fall back to previous hardcoded values for historical records missing `bk_spread_price`

### Note
Existing PW call records do not have `bk_spread_price` — it only appears on new calls. For those records, the price displays as blank and ROI falls back to previous values.

- Cross-ref: NBA #356.

**Files changed:** `monitor.py`, `pw_matrix.py`, `pw_trend.py`, `server.py`, `dashboard.html`

---

## 2026-06-17 — feat: PW Scenario Categorisation with BK ROI stats and filtering (#191)

- Each PW call classified into a 4-dimensional scenario: spread_role, margin, edge_range, quarter.
- Scenario badge on PW tab call rows, Live Games PW header, and Game History PW call table showing accuracy + BK ROI.
- New `/api/pw-scenarios` endpoint returns per-scenario aggregate stats.
- PW calls enriched with `scenario_key` and `scenario_stats` via `/api/pw-calls`.
- ROI+ checkbox and Acc≥ input filters in PW filter bar for client-side scenario filtering.
- Scenario stats included in PW Slack alert messages.
- Cross-ref: NBA #352.

**Files changed:** `outcomes.py`, `server.py`, `monitor.py`, `dashboard.html`, `CHANGES.md`, `CLAUDE.md`

---

## 2026-06-13 — feat: Season/Segment top-level filters in PW ROI Report (#190)

- Season and Segment dropdowns above sub-tabs, apply to all report content.
- Client-side recomputation: raw calls embedded as JSON, `_analyzeJs()` mirrors Python `_analyze()`.
- BK scenarios recomputed client-side when filters active. Overview cards update dynamically.
- Filter state persisted to localStorage. Cross-ref: NBA #351.

**Files changed:** `pw_roi_report.py`

---

## 2026-06-13 — feat: ROI Report refresh button + tab-switch refresh (#189)

- Refresh button reloads iframe without regenerating report.
- Auto-refresh on tab switch to ROI Report panel.
- Cross-ref: NBA #350.

**Files changed:** `dashboard.html`

---

## 2026-06-13 — feat: Edge Range filter in PW ROI Report BK heatmaps (#188)

- Edge Range dropdown (≥10/5-10/0-5/<0) on BK scenario tables and heatmaps.
- 5-way scenario computation: role × margin × edge × period × call_sel (up from 4-way).
- Edge column in Best/Worst BK tables. Filter persisted in localStorage defaults.

**Files changed:** `pw_roi_report.py`

---

## 2026-06-12 — feat: Edge Range, Margin dims + compound axes + BK ROI in ROI Matrix (#187)

- **3 new dimensions:** Edge Range (≥10/5-10/0-5/<0), Margin (Leading/Trailing), Quarter. 20 pair pivots (up from 9).
- **Compound axes:** "+" button adds optional secondary dimension per row/column. `~` separator in compound keys (avoids `10+` conflict). Split row labels into separate columns.
- **BK ROI in cells:** 7 lines per cell — Won%, F, ML, Spr, BkS, BkO, count. Normalised font sizes (0.72/0.65/0.6rem).
- **Drilldown:** Compound cells use `matrixDrillCompound()` + `data-drill` JSON attribute. Edge→`'edge'`, Margin→`'margin'` key mapping.
- **Fixes:** Empty dim values excluded from pivots, NRL `esc()` quote escaping for attributes.

**Files changed:** `pw_matrix.py`, `server.py`, `dashboard.html`

---

## 2026-06-11 — fix: PW accuracy stats block ignores edge/polarity filters (#186)

- Accuracy block was computed server-side via `/api/outcomes` which didn't receive `pw_edge`/`pw_polarity` params.
- Now computed client-side from filtered `/api/pw-calls` data — all filters (edge, polarity, consensus, source, etc.) reflected in stats.
- Cross-ref: NBA #348.

**Files changed:** `dashboard.html`

---

## 2026-06-09 — fix: condition "Trailing by 18+ in 2H" period mismatch (#185)

- Condition had `period: "game"` but name implies 2H-only — fired during Q1/Q2 (21 historical PW calls affected).
- Changed `period` to `"2H"` so it only fires in the second half, matching its comeback-detection intent.

**Files changed:** `config.json`, `config.example.json`

---

## 2026-06-08 — fix: ML auto-training skipped due to timer jitter (#165)

- **Bug:** Staleness check `age_days < interval_days` failed when systemd timer fired seconds before the exact interval boundary (e.g. 6.9999 < 7.0 = True → skipped as "not stale"). NBA model was 7.6d overdue.
- Added 1-hour grace period: `age_days < interval_days - 0.042`.
- **Note:** `skipped_not_stale` result label is misleading — the model may be stale (hash changed) but skipped because the age hasn't reached the interval threshold. The label means "not old enough" not "data unchanged".

**Files changed:** `ml_train_runner.py`

---

## 2026-06-07 — feat: suppress PW calls at 0-0 score (#183)

- Calls with score 0-0 at fire time marked `_suppressed` with reason "0-0 score".
- Hidden by default, visible via Show Suppressed, excluded from accuracy/ROI. 7 calls affected.
- Replaces old minute-0 skip that silently dropped calls instead of flagging them.

---

## 2026-06-07 — feat: Backup & Recovery tab improvements (NBA cross-repo)

- Tab renamed "Recovery" → **"Backup & Recovery"**.
- **WAL banner:** "WAL Active: N games recorded since last backup" with date range and recovery guidance.
- **WAL Recovery Data row** at top of backup table with entry count, size, date range.
- **LATEST badge** on most recent backup. Removed confusing YES/GAP/— badges.
- Help modal updated. Initial text changed to "Loading..." (NBA parity).

---

## 2026-06-07 — feat: PW tab consensus multi-select filter (#182, NBA #345)

- Checkbox group (Strong, Conflicted, ML, Hist) in PW filter bar for multi-select consensus filtering.
- Server-side filtering via comma-separated `pw_consensus` param.
- Persisted to localStorage with existing PW filter defaults.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-06-06 — fix: Odds API test — use x-requests-last for per-call credits (#181, NBA #344)

- **Bug:** Test endpoints logged `x-requests-used` (cumulative monthly total) as `credits_used` instead of `x-requests-last` (per-call cost). Showed 954/955 credits per call when actual cost was 0-3.
- Fixed in both `/api/odds-api-test-events` and `/api/odds-api-test` endpoints.
- **Note:** Existing incorrect log entries remain in `odds_api_log.jsonl` — affects Odds API usage stats display until those entries age out of the selected time range.

**Files changed:** `server.py`

---

## 2026-06-06 — feat: copy buttons on fallback viewer and API test panels (#180, NBA #343)

- Copy button on each JSON panel in fallback data viewer modal (Fallback Details + Raw API Response) and API Test tool (Request + Response).
- `_copyPre()` helper copies `<pre>` content to clipboard with "Copied!" feedback (1.5s).

**Files changed:** `dashboard.html`

---

## 2026-06-06 — fix: Odds API test — use /events/ endpoint for listing (free) (#175)

- **Bug:** "Fetch Events" was calling `/v4/sports/{sport}/odds` (costs credits per event) instead of `/v4/sports/{sport}/events/` (free, 0 credits). WNBA had 807 events → 807 credits burned in a single click.
- Switched to `/events/` endpoint which returns event IDs, teams, and commence times at no cost.
- Added trailing slash to individual event `/odds/` URL.
- Dashboard hint changed from "~1 credit" to "free".

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-06-06 — feat: Odds API fallback data saving and viewer (#179, NBA #341)

- **Fallback data persistence:** When bookmaker fallback occurs, saves full raw API event response to `odds_api_fallback_data.jsonl` (one JSONL entry per fallback event).
- **Dashboard controls:** Enable/disable toggle ("Save data") and retention period dropdown (1-90 days) in the Bookmaker Fallback Audit Trail section header.
- **Viewer modal:** "View" button on fallback audit trail rows and log entries fallback rows opens a modal showing fallback details + full raw API response JSON side-by-side.
- **Server endpoint:** `GET /api/odds-api-fallback-data?ts=<timestamp>` returns saved data for a specific fallback event.
- **Auto-pruning:** Old entries pruned on dashboard startup based on retention config.
- **localStorage persistence fix:** Toggle and retention saved to localStorage immediately (sync) in addition to config.json (async), preventing settings from resetting on hard refresh.
- Config keys: `odds_api_fallback_save_enabled`, `odds_api_fallback_retention_days`.

**Files changed:** `odds_api.py`, `server.py`, `dashboard.html`, `config.example.json`

---

## 2026-06-06 — feat: Odds API — split into Usage and API Test sub-tabs (#176)

- Odds API section under Monitor Status now has two inner sub-tabs: **Usage** (default) and **API Test**.
- Usage sub-tab contains all existing content: usage charts, time range filters, credit stats, BK coverage, fallback events.
- API Test sub-tab contains the test tool from #175 (league dropdown, market checkboxes, event fetcher, request/response viewer).
- Active inner sub-tab persisted to localStorage (`odds_inner_tab`).
- New `.inner-tab` CSS class for nested tab styling (avoids conflict with `switchSub` which scopes to panel container).

**Files changed:** `dashboard.html`

---

## 2026-06-06 — feat: Odds API test tool (#175, NBA #337)

- New "API Test" section in Monitor Status > Odds API tab.
- League dropdown (NRL/SOO/NBA/WNBA), market checkboxes (h2h/spreads/totals).
- Fetch Events populates game dropdown (~1 credit). Credit cost estimate shown before call.
- Test API Call shows formatted JSON request + response side-by-side.
- Two new endpoints: `/api/odds-api-test-events`, `/api/odds-api-test`.
- Test calls logged to `odds_api_log.jsonl` with context `api_test_events` / `api_test` — visible in Odds API context tables.

---

## 2026-06-06 — Odds Monitor: exact try buckets + serve-time EV recalculation (#174)

- **Reference table:** Changed from cumulative (1+/2+/3+) to exact try count buckets (0/1/2/3+). All rows sum to 100%.
- **Serve-time EV recalculation:** `/api/odds-monitor-history` now recomputes `ev_edge_under` and `ev_edge_1plus` on every request using current `get_late_tries_probs()` (1,004 games). Original stored values untouched.
- **Data audit:** 1,004 games, 7,884 try events, all with valid `game_seconds`. 2 games missing PBP (2023 backfill). Computed probs verified against manual count.

**Files changed:** `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-06-05 — feat: Odds Monitor history — help modal, BK names, selection docs (#171)

- Help modal (`?` button) explaining all stats: aggregate card, per-game summary, expanded detail, snapshot table columns, bookmaker priority order, and how per-market BK selection works (no retries, independent fallback).
- History snapshot table rows show bookmaker name under Total Line, Spread, and Home ML columns.
- Game expanded detail shows per-market bookmakers (Totals BK, ML BK, Spread BK) + priority order.
- Fix: BK list collects from all snapshots (was first-only).

---

## 2026-06-05 — feat: Odds API tab improvements + chart fix (#173, NBA #336)

- **Filter persistence:** range dropdown, date pickers, chart interval saved to localStorage across sessions.
- **Range dropdown:** added 3 days, 5 days, 6 months, 1 year; removed 1 day.
- **Credits card:** renamed "Total API Calls" → "Credits Used / API Calls" with cache hits on separate line.
- **Chart default:** interval changed from 5 min to Daily.
- **Chart fix:** `bk_usage` entries (31,674 of 36,257) were flooding the 5,000-entry API limit, leaving only 37 call entries for the chart. Server now supports `exclude_type` param; client excludes `bk_usage`.

---

## 2026-06-05 — feat: PW tab filter reset button (#172, NBA #335)

- Reset button clears all PW filter dropdowns (incl. SOO Game), unchecks Strict BK + Set as default, removes localStorage, reloads data.

---

## 2026-06-05 — feat: Odds Monitor history help modal + bookmaker on total line (#171)

- Help modal (`?` button) explaining all Odds Monitor history stats: aggregate card, per-game summary, expanded detail, and 14-column snapshot table. Includes bookmaker priority order.
- History expanded rows now show bookmaker name (small muted text) under Total Line, matching the live view.
- Game expanded detail shows bookmaker priority order alongside actual per-market bookmakers (Totals BK, ML BK, Spread BK).
- Fix: BK list now collects from all snapshots (was first-only, missing bookmakers from later rows due to market fallbacks).
- History snapshot table rows now show bookmaker name under Spread and Home ML columns (matching Total Line pattern).

---

## 2026-06-05 — fix: scroll to top on browser hard refresh (#170, NBA#334)

- `history.scrollRestoration = 'manual'` + `scrollTo(0,0)` at script start.
- Only fires on full page load/refresh — auto-refresh polling unaffected.

---

## 2026-06-05 — Fix Odds Monitor history: EV display, strategy stats, duplicate merge (#168)

- **EV display fix:** Row header now shows `ev_edge_under` (total stays under line) instead of `ev_edge_1plus` (1+ tries probability). These are different metrics — the old display was misleading since most games had high 1+ tries EV but negative under EV.
- **Strategy stats fix:** "Under +EV >25% Strategy" now shows `wins/bets won` (e.g., "1/2 won") instead of `bets/games bets` (was "2/10 bets").
- **First qualifying bet:** Games matching the strategy show a green badge with the bet details: "Under 42.5 @ 1.90" (first snapshot at minute 65+ where +EV >25%).
- **Duplicate merge:** Merged 1-snap duplicate Raiders vs Cowboys entry into the 20-snap original (now 21 snaps, 9 total games).

**Files changed:** `dashboard.html`, `odds_monitor_history.json`

---

## 2026-06-05 — Suppress PW calls at Q1 0' (#169)

- **Fix:** PW calls at game_minute=0 (kickoff) had no conditions/stats. Now suppressed in `monitor.py` (`_should_evaluate_pw`) and filtered in `server.py` (both history and live/recent paths). Backfill already excluded minute 0. 3 existing live calls removed from display.

**Files changed:** `monitor.py`, `server.py`

---

## 2026-06-05 — fix: Remove redundant Model Eval Retrain button (NBA #332, NRL #166)

- NBA: Removed "Retrain" button (was cache-busting Force Refresh). Auto-snapshots already handled by `model_eval_auto_snapshot_worker()`.
- Removed `_do_retrain_background()`, `_retrain_state`, `/api/model-eval-retrain` endpoints, `triggerRetrain()` JS.

---

## 2026-06-05 — fix: PW tab game header PW/Winner lines (NBA #333)

- **Two-line layout:** PW correct shows "PW: Team YES" + "Winner: Team" with full stats. PW wrong shows "PW: Team NO" with full stats + stripped "Winner:" line (margin, score, role, pregame only — no BK data).
- **Game time on PW line:** shows selected call's game time, e.g. `PW: (Q2 35') Team YES`.
- **Consistent stat ordering:** Role → Odds → Spread → Live → BK Odds → BK Spread → covered/missed — matches per-call table column order.
- **Selected call's odds/spread:** PW line uses selected call's moneyline/spread instead of game-level fields. Updates with First/Last/Game PW dropdown.
- **Polarity + edge badges:** `Pol+N` and `edge:+N.N` from selected call. Shows `edge:—` when no conditions.
- **First/Last Call ordering:** uses wall-clock `ts` (when prediction was made). `game_minute` can be non-monotonic due to API clock corrections/stoppages.
- **Monotonic game_minute clamping:** `_clamp_game_minute()` in monitor.py tracks `max_game_minute_{match_id}` in state, prevents API clock from going backwards. Only affects poll-time metadata (PW calls, condition eval, cadence) — try events and PBP use `gameSeconds` from timeline API (unaffected).
- **Bug fix:** `spread_fav` undefined in `build_pw_calls_payload` — caused 0 PW games to load.

---

## 2026-06-04 — feat: Model Evaluation auto-snapshots (#166)

- Background thread auto-saves eval snapshots at configured interval (default 360 min / 6 hours).
- Reads `model_eval_snapshot_enabled` / `model_eval_snapshot_interval` from config each cycle.
- Removed redundant "Retrain" button (was identical to "Save snapshot").
- Removed `/api/model-eval-retrain` GET and POST endpoints.

---

## 2026-06-04 — feat: ML auto-training via systemd timer (#165)

- New `ml_train_runner.py` with 3-gate decision logic (enabled, staleness, hash) matching NBA Monitor parity.
- Systemd timer `nrl-ml-train.timer` runs daily at 13:00 UTC.
- PID-based lock prevents concurrent training runs.
- Per-run log capture, training history persistence, retention pruning (separate limits for trained vs skipped).
- Previously all training was manual — no auto-trigger mechanism existed.

---

## 2026-06-04 — feat: ROI Matrix row/column totals (NBA #318)

- ROI Matrix heatmap now shows **Total** column (right edge) and **Total** row (bottom edge).
- Grand total cell at bottom-right corner aggregates all cells.
- Totals computed client-side from raw P&L data with proper ROI recomputation.

---

## 2026-06-04 — note: live conditions already in Games tab (#164, NBA#331)

- NBA required moving live conditions from standalone card into Games tab. NRL already had this — `ticker-hits` is inside `sub-games-live`. No changes needed.

---

## 2026-06-04 — feat: cross-dashboard league switcher (#163, NBA#330)

- Header bar links to NBA and WNBA dashboards.
- Auto-detects port set: direct (8899/8900/8901) vs Tailscale HTTPS (8444/8445/8446).

---

## 2026-06-04 — feat: BK ROI heatmap split into Leading/Trailing (#155)

- Split single heatmap into separate **Leading** and **Trailing** grids (both always visible).
- Calls column now shows `correct/total` instead of just count.
- Removed Margin dropdown; Call Sel filter retained.

---

## 2026-06-04 — feat: Odds Monitor totals under +EV ROI stats (#133)

- **Live game cards**: show tries count and best EV edge on each card.
- **History game headers**: tries + best EV badge visible when collapsed.
- **Aggregate stats card** at top of history view: games, avg tries in window, over/under/push breakdown, under +EV >25% strategy P&L and ROI.
- All computed client-side from existing history payload.

---

## 2026-06-04 — fix: PW tab FAV/DOG role derived from pregame spread (#162, NBA#329)

- PW and Winner roles now derived from pregame `spread_fav`, not per-call `spread_role` or ML odds.
- Fixes both teams showing as FAV when selected call differs from game-level PW team.
- Server sends `spread_fav` in PW game data for client-side derivation.

---

## 2026-06-03 — feat: PW tab suppressed call filtering + header call selector (#161, NBA#328)

- **Header call selector**: new "First Call / Last Call / Game PW" dropdown in PW tab header bar. Controls which call's data appears in the game header (team, odds, spread, PW Won). Persists to localStorage.
- **PW Won per-call**: PW Won YES/NO now reflects whether the *selected call's* predicted team won, not the game-level `pw_correct`.
- Game header and half/quarter labels show filtered counts when "Hide Suppressed" is active.
- Groups with only suppressed calls are hidden entirely when hiding suppressed.
- Accuracy block shows "+ N suppressed calls excluded from stats" footnote.
- Server: added `suppressed_calls` field to PW accuracy response.

---

## 2026-06-03 — feat: search/filter bar in Config Conditions editor (#160, NBA#327)

- Text filter at the top of the Conditions list in the Configuration tab.
- Filters condition rows in real-time by name or type (case-insensitive).
- Shows match count (e.g. "4/18"). Persists across re-renders but not page reloads.

---

## 2026-06-03 — Auto-recovery for missed games (#136)

- **Auto-recovery detection:** Each poll cycle checks previous 1-2 rounds for completed games not in history. Detects games missed due to monitor downtime, network outages, or process crashes.
- **Recovery pipeline:** Fetches match data from NRL API, replays conditions via `backfill.evaluate_conditions_for_game()`, generates synthetic PW calls (ML disabled, lookahead-safe), merges with any preserved live state (partial miss).
- **Config toggles:** `auto_recovery_enabled` (master on/off), `auto_recovery_pw_calls` (synthetic PW generation). Both controllable from dashboard.
- **Dashboard tab:** New "Auto Recovery" sub-tab under Monitor Status with enable/disable toggles, last scan info, and recovery log table showing each recovered game with state (Full/Merged/Failed), conditions, and PW call counts.
- **CLI scan:** `recovery.py scan --season 2026` for batch gap detection across all rounds. `--dry-run` to preview. Always runs regardless of config toggle.
- **State TTL cleanup:** Old state keys (cond_hits_, pw_calls_, pregame_odds_, etc.) cleaned up: immediately for recorded games, 48h TTL for unrecorded, 7d for game_end_ keys. Prevents unbounded state growth.
- **Source filter:** `source: "recovered"` on recovered game records. Added to Game History and PW tab source dropdowns.
- **Systemd timer:** `nrl-recovery.timer` runs daily at 14:00 UTC (after game window).

**Files changed:** `monitor.py`, `recovery.py`, `server.py`, `dashboard.html`, `config.example.json`, `systemd/nrl-recovery.{service,timer}`

---

## 2026-06-03 — Fix live/recent PW calls bypassing filters + heatmap alignment (#159)

- **Fix:** Live/recent calls from `.alert_state.json` were injected into PW tab without per-call filters (quarter, spread_role, band, consensus, margin, strict_bk). Caused inflated counts and ROI mismatch vs ROI Report heatmap.
- **ROI Report heatmap fix:** Added Margin filter dropdown, aggregate `margin="all"` entries. Heatmap default (Call Sel=All) now shows true aggregate matching PW tab. All 3 leagues 100% aligned (was 18/28 NBA+WNBA only, 0/10 NRL).

**Files changed:** `server.py`, `pw_roi_report.py`, `pw_roi_report.html`

---

## 2026-06-02 — Exclude suppressed PW calls from ROI calculations (NBA #325)

- **Fix:** Suppressed PW calls (`_suppressed: true`) were included in accuracy/ROI stats on PW tab. Now excluded from `build_pw_calls_payload` accuracy (server-side) and polarity/edge pills (client-side). Aligns with ROI Report and `pw_matrix.py` which already excluded them.

**Files changed:** `server.py`, `dashboard.html` (both NRL + NBA)

---

## 2026-06-02 — Standardize spread/line ROI to -110 juice (#157)

- **Fix:** Spread ROI, live line ROI, and BK line ROI were using even-money math (`+$100/-$100`) in `pw_roi_report.py`, `server.py` (`_build_pw_accuracy_summary`, `_build_combo`), and `pw_matrix.py`. Now standardized to -110 juice (`+$90.91/-$100`) matching NBA `outcomes.py` and real spread betting. This resolves the WNBA underdog Q2 spread ROI mismatch across views (+25.0% report vs +11.4% PW tab).

**Files changed:** `pw_roi_report.py`, `server.py`, `pw_matrix.py`

---

## 2026-06-02 — Drilldown: PW drills preserve strict BK (#153)

- **Fix:** `_isPwDrill` now checks `pw.edge`, `pw.version`, `pw._showPwCols`. PW Trends version/edge drills no longer clear strict BK. Only condition drills (no PW params) clear it.

**Files changed:** `dashboard.html` (both NRL + NBA)

---

## 2026-06-02 — Game History: pw_version server-side filter (#139)

- **pw_version filter:** Server-side filter on `/api/history` matches games where any PW call has the specified `pw_version`. `versionDrill()` now passes `pw_version` instead of date range. Exact match: v5 cohort 55 games = drilldown 55 (was 6 with date approximation).

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-06-01 — PW ROI Report: Bookmaker ROI section + BK all leagues + stats (#146, #147, #148)

- **Dedicated Bookmaker ROI section (#148):** New section with BK Coverage table, Best/Worst BK Scenarios (2-way cross-tabs from 5 dimensions: role, edge, consensus, quarter, margin), Role×Edge heatmaps per league (green/red intensity), BK-specific recommendations with BK vs ML comparison and coverage warnings.
- **ROI Report dashboard tab (#149):** New "📊 ROI Report" tab on NRL, NBA, and WNBA dashboards with iframe serving `pw_roi_report.html`. Regenerate button. Auto-resize iframe. Auto-generates daily via `runtime_backup.py`. Removed ROI Report link + Generate button from PW Trends header (moved to dedicated tab).
- **Chart layout fix (#149):** Charts changed from squashed 2-column grid to full-width stacked layout with 450px height. Tables below each chart.
- **4-way BK scenarios (#150):** Sub-tabs (Overall/BK Analysis). 4-way combos. Top/bottom 3 per league. Sort + min calls (1-99) + 4 dimension filters + set as default. League color-coded rows.
- **BK heatmap Call Sel filter (#152):** Role × Period heatmap has Call Selection dropdown. Client-side rendering.

- **BK Odds/Spread ROI for all 3 leagues (#146):** Tables and charts now show NBA, WNBA, and NRL BK columns (was NRL-only). Role table includes WNBA. Summary cards show BK Spr for all leagues.
- **Statistical significance (#147):** Wilson 95% CI on accuracy, ROI CI on moneyline. Confidence badges (high/medium/low with N=). Min sample thresholds (50/30/20/10). What-if backtesting for Q1/edge_gate/threshold/conflicted suppression. Cross-league ML ROI gap detection. Small samples flagged in research recommendations.

**Files changed:** `pw_roi_report.py`

---

## 2026-06-01 — PW Trends: Edge Range drilldown + PW columns on all drills (#145)

- **Edge Range rows clickable:** `edgeDrill` with server-side `pw_edge` filter on `/api/history`.
- **PW columns on all 3 drills:** Changelog, Version Cohort, and Edge Range drills all show PW Calls, ROI (F/ML), BK (Odds/Spr) via `_showPwCols` flag.

Cross-ref: NBA #316

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-06-01 — Drilldown: inline condition columns + stats banner (#138)

- **Inline condition columns:** When drilling from Outcomes, game list shows 5 extra columns per drilled condition: Won? (✅/❌ + team), Score, Margin, Diff (green/red), Time. Grouped under condition name header. Server includes `conditions_fired_summary` in slim records during condition drills.
- **Drilldown stats banner:** Condition drill shows "📊 Games/Teams: X of Y won (Z%)" with per-condition breakdown + avg margin diff. PW drill shows "🎯 PW: X of Y games won + calls correct (accuracy%)". Server computes `drilldown_stats` in `build_history_payload`.
- **PW drilldown columns:** PW Accuracy table drills show 3 extra columns: PW Calls (correct/total + accuracy%), ROI (Flat/ML P&L), BK (Odds/Spr P&L). Server adds `pw_summary` with `bk_odds_pnl`/`bk_spr_pnl` to slim records.
- Full NBA Game History drilldown parity achieved.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-31 — Round-flip game-end recovery + conditions fix (#144)

- **Round-flip recovery:** When the NRL API switches to the next round while the last game is still live/just ended, the monitor now detects previously-live games (via `cond_hits_` state without `game_end_`) and fetches match data directly to confirm completion. Recovered games injected into completed_games for normal game-end processing with correct round number derived from URL path.
- **Game-end conditions fix (systemic):** Records now use accumulated `cond_hits_` state (all fires across game lifetime) instead of `all_hits` (current poll only — always empty at game-end). All prior live games had 0 conditions in their history records.
- **Condition deduplication:** Game-end records deduplicated per `team|condition` key (keep latest fire), matching backfill record format and display-time dedup (#108).
- **Data recovery:** Panthers 20-18 Warriors (R13) and Cowboys 30-18 Rabbitohs (R12) recovered with full stats, PW calls, try events, odds. 9 live game records repaired with conditions from state.

**Files changed:** `monitor.py`, `game_history.json`

---

## 2026-05-31 — Analysis tab: Refresh button + strict_bk empty message (#143)

- **Refresh button:** Added to Analysis Outcomes filter bar for force reload.
- **Context-aware empty message (#143):** When Strict BK + filter combo results in 0 PW calls, shows "No PW calls with genuine bookmaker data for the current filter combination" instead of generic "No PW data yet".

Cross-ref: NBA #314

**Files changed:** `dashboard.html`

---

## 2026-05-31 — Game History drilldown: banner, return nav, unified drill (#138)

- **`drilldownToHistory()`:** Unified drilldown function replacing manual DOM manipulation in `analysisDrill`, `matrixDrill`, `combosDrill`. Resets all filters, sets drilldown values, saves return context.
- **Drilldown banner:** Colored banner at top of Game History showing source tab, removable condition chips (×), PW filter chips (band/consensus/half/quarter/role/margin/calls), season/segment context chips.
- **Return navigation:** "↩ Return" button goes back to originating sub-tab (Outcomes/ROI Matrix/PW Trends) with scroll position restore.
- **Clear/reset:** "✕ Clear" button resets drilldown context. `resetHistoryFilters()` also clears drilldown state.
- **Bugfix:** Duplicate `let historyPage=1` declaration broke all dashboard JS — removed from line 6476 (already declared at line 1288).
- **Multi-condition combo drilldown:** Condition Combinations rows drill with all conditions (AND semantics). Server splits comma-separated condition filter. Banner shows each condition as removable chip.
- **PW Trends version drilldown:** Changelog + Version Cohort rows clickable → Game History filtered by changelog-derived date range. Banner shows version tag + date range chip. Return goes to PW Trends tab. Follow-up #139 for server-side `pw_version` filter.
- **Strict BK preserved on drilldown:** Was unconditionally clearing strict_bk on drill. Now preserves user's setting.
- **Return scroll retry:** Uses 5-attempt retry loop (100ms intervals) instead of single 50ms timeout for accurate scroll restore after sub-tab render.
- **Condition tables Games/Fires split (#140):** Added "Games" (unique, matches drilldown) and "Fires" (events, both teams) columns with tooltips. Server adds `unique_games` via game_id dedup.
- **PW Accuracy tables parity (#140):** Added "By Half" (H1/H2/ET) and "By Spread Role" (Fav/Dog) tables. All 4 tables expanded: Games, Calls, Correct, Accuracy, Games Won, Games Won%, Flat ROI, ML ROI, Spr ROI, BK Spr ROI, tag columns (BLOWOUT/CLOSE/COMEBACK), Covered, Cov!Won, Won!Cov. Shared `_pwAccTable()` builder. All rows clickable with drilldown. Note: 1-3 game count discrepancy between PW Accuracy tables and drilldown — PW buckets count unique game_ids per bucket while History filters by game date + PW call presence.
- **Onclick fix:** Condition names with `>`, `%`, `(` broke HTML onclick — changed from `esc()` to escaped single-quoted string.
- **Combo drilldown same-team AND (#140):** History filter for comma-separated conditions now requires all conditions fired by the SAME team (was cross-team). Remaining gap: combo table applies `eligible_conds` + `min_sample` + decay weighting that history can't replicate.
- **Drilldown note for combo mismatch:** Banner shows explanatory note when combo drilldown game count differs from combo table.
- **No reload on drilldown return (#141):** Analysis tab skips data reload when returning from Game History drilldown. Uses `_skipAnalysisReload` flag + `switchSub()` instead of `subTab.click()`. Cached data persists across drilldowns.
- **Strict BK cleared for condition drills (#142):** Non-PW drills (conditions, combos, versions) clear strict_bk to avoid 0-game results. PW drills (band, consensus, half, role) preserve it. Return button restores previous state. Banner shows yellow warning when temporarily disabled.

Cross-ref: NBA #303, #304

**Files changed:** `dashboard.html`

---

## 2026-05-31 — Reduce upcoming odds API calls with adaptive cache TTL

Graduated cache TTL based on time to nearest kickoff (NRL only — NBA uses ESPN for free):

| Time to Kickoff | Cache TTL | Previous |
|----------------|-----------|----------|
| ≤2 hours | 30 min | 15 min |
| ≤6 hours | 1 hour | 15 min |
| ≤24 hours | 2 hours | 15 min |
| >24 hours | Skip entirely | 15 min |

Expected savings: ~75% fewer credits (~32→~8 calls/day). Config `odds_api_upcoming_cache_ttl` acts as minimum floor if set.

---

## 2026-05-31 — Game History: Strict BK checkbox + reset fix (#137)

- **Bug:** Strict BK filter silently applied to Game History via `localStorage`, not cleared by Reset, not visible in filter bar.
- **Fix:** Added visible "Strict BK (cross-tab)" checkbox (`id="hf-strict-bk"`) to filter bar. `resetHistoryFilters()` clears `localStorage` strict_bk and unchecks all checkboxes. `buildHistoryQuery` reads history checkbox first. `_getStrictBk()` falls back to localStorage for early page-load.

Cross-ref: NBA #307

**Files changed:** `dashboard.html`

---

## 2026-05-30 — ROI Matrix/Combos drilldown fixes + settings persistence (#135)

### Drilldown fixes
- Reset ALL history filters (including season/segment/source/soo-game) before applying drilldown values — stale filters caused 0 results
- Skip localStorage restore when entering via drilldown (`_historyDrilldownActive` flag)
- Save/restore pre-drill filters — user's History filters restored when navigating back directly
- Fix `pw_spread` → `pw_spread_role` param name mismatch in `buildHistoryQuery`
- Derive `spread_role` from game record when missing on PW call (93% of calls had empty `spread_role`)
- Filter-then-select semantics: match all calls first, then apply `call_selection` — matches ROI Combos behavior
- Pass `strict_bk` through to History drilldown
- `combosDrill` passes `call_selection` as 5th parameter

### New History PW filters
- Server: `pw_quarter`, `pw_margin`, `pw_call_selection`, `strict_bk` params on `/api/history`
- Dashboard: PW Quarter, PW Margin, PW Calls dropdowns in Game History filter bar

### ROI Matrix settings persistence
- Save/restore all controls to localStorage: row/col dimensions, season, segment, pin values, combo sort/limit/min
- `_saveMatrixState()` on every control change, `_restoreMatrixState()` on tab open
- Pending pin values applied after dynamic pin controls render

Cross-ref: NBA #303, #304

---

## 2026-05-30 — BK Odds ROI and BK Spread ROI on ROI Matrix and PW Trends tables (#134)

- `pw_trend.py`: Track `bk_ml` per call, compute `roi_bk_odds` in `_compute_metrics()` for all snapshots, versions, and edge breakdowns
- ROI Matrix heatmap: BkS (BK Spread ROI) and BkO (BK Odds ROI) lines in cells when data available
- ROI Combos table: BK Odds ROI column + Games count column (renamed Total→Calls). Server `_build_combo()` now tracks `bk_odds_roi_pct`
- PW Trends Changelog/Version Cohort/Edge Breakdown tables: BK Odds ROI column added

Cross-ref: NBA #302

---

## 2026-05-30 — Fix PW alert/call threshold mismatch (#130)

- PW threshold alert conditions (e.g. "Predicted Winner 60%") could fire at confidence levels below the global `predicted_winner_threshold` (65%), causing alerts without a matching PW call on the PW tab
- **Auto-alignment:** `monitor.py` now derives effective threshold as `min(global, lowest_pw_condition_threshold)` at runtime — PW calls always record when any alert would fire
- **Config save warning:** dashboard shows alert popup when PW condition thresholds are misaligned with global threshold
- **Monitor startup warning:** logs `WARN` to monitor.log when thresholds are misaligned
- Root cause: "Predicted Winner 60%" condition at threshold=60 fired a 64.3% prediction, but global threshold=65 blocked PW call recording

**Files changed:** `monitor.py`, `server.py`, `dashboard.html`

---

## 2026-05-30 — Strict BK data toggle (#131, #132)

BK Spread ROI discrepancy between ROI Combos and PW tab caused by synthesized pregame data mixed with genuine bookmaker data.

### Fix
- "Strict BK" checkbox on all 4 PW-related tabs: PW tab, ROI Matrix, PW Trends, Analysis/Outcomes
- When ON: excludes PW calls without genuine BK data (`bk_moneyline`/`bk_spread`) from ALL ROI calculations
- When OFF: warning badge "⚠ BK ROI includes synthesized data from pregame odds" with tooltip
- Synced across tabs via `_syncStrictBk()` + `class="strict-bk-cb"`, persisted to localStorage
- Server: `strict_bk=1` param on `/api/pw-calls`, `/api/pw-trend`, `/api/pw-matrix`, `/api/pw-roi-combos`, `/api/analysis`
- `_filter_strict_bk(records)` helper strips PW calls without genuine BK from game records

### BK data coverage
- NRL: 5.9% of calls have genuine BK data (78/1317, all from live games)
- Backfill calls never have BK data — ROI Combos was synthesizing from pregame spreads

Cross-ref: NBA #301

---

## 2026-05-30 — Sync game window to UTC 04:00-13:00 + dynamic config reload (#129)

- **Root cause:** `odds_monitor.py` fallback `GAME_WINDOW_UTC` was hardcoded to `(6, 12)` while config.json and dashboard had `(4, 13)`. `server.py` defaults also stale at `6/12`.
- **Fix 1 — Sync:** Fallback updated to `(4, 13)`. `server.py` defaults updated. `systemd/nrl-monitor.timer` updated. CLAUDE.md references updated (4 locations). All sources now consistent: UTC 04:00-13:00 = AEST 14:00-23:00.
- **Fix 2 — Dynamic config:** Odds monitor thread now stores config in `_state["config"]` and reads it fresh each poll iteration. `update_config()` called from `/api/odds-monitor` endpoint (triggered on Live view open or auto-refresh). Config changes (e.g. scheduler hours) take effect without dashboard restart.

**Files changed:** `odds_monitor.py`, `server.py`, `systemd/nrl-monitor.timer`, `CLAUDE.md`

---

## 2026-05-30 — Cross-league PW ROI analysis with dynamic report generation (#128)

### ROI analysis
- Cross-league ROI analysis across NBA (15,134 calls), WNBA (1,655), NRL (1,302/74 live)
- Breakdowns by role, period, margin, edge, consensus, leading/trailing, and confidence calibration
- Key findings: NBA underdog ML ROI +59.9%, WNBA ML ROI +73.8%, NRL pregame spread ROI +67.6%, NRL BK odds ROI -23.9%, conflicted consensus ROI-negative in NBA/NRL

### Dynamic report generation
- `pw_roi_report.py` — generates `pw_roi_report.html` from current game history data across all 3 leagues
- Insights and recommendations are data-driven: automatically detects underdog edges, ML ROI standouts, spread advantages, Q1 weakness, edge gate value, calibration issues, conflicted consensus, BK odds efficiency
- `POST /api/generate-roi-report` endpoint in server.py (NRL + NBA)
- 🔄 Generate button on PW Trends tab in all 3 dashboards
- `--copy-to-nba` flag copies report to NBA repo
- 7 Chart.js charts, data tables, insight cards, and recommendation panels
- Cross-ref: NBA #300

**Files changed:** `pw_roi_report.py` (new), `pw_roi_report.html` (now generated), `server.py`, `dashboard.html`

---

## 2026-05-30 — PW live call analysis and pw_compare.py enhancements (#127, #126)

### PW live analysis (#127)
- Comprehensive analysis of 74 live PW calls (2026 season): 67.6% accuracy vs 100% backfill
- H1 calls are the accuracy drag (Q1=17%, Q2=54%); H2=83.7%
- Edge gate (≥0) is the best filter: +5 net, 85.7% precision, 100% precision in 20-70min window
- Confidence calibration broken: 65-70% band = 0% actual, 80-90% = 28-46% actual
- Conflicted consensus = 42.9%, strong = 79.3%, ml_only = 63.2%
- Cross-league analysis (NBA #300): calibration issues universal across NBA/WNBA/NRL

### pw_compare.py enhancements (#116)
- Added `--min-minute`/`--max-minute` to filter by game minute range (isolate competitive windows)
- Added `--min-margin`/`--max-margin` to filter by absolute margin at fire (exclude blowouts)
- Call filter labels shown in output header

**Files changed:** `pw_compare.py`

---

## 2026-05-30 — Fix PBP conditions never firing live: teamId type mismatch (#126)

- `monitor.py` passes `teamId` as `str` to `timeline_features.extract_features()`, but NRL timeline events have `teamId` as `int`
- All PBP helper functions (`_max_consecutive_tries`, `_max_unanswered_points`, `_max_error_streak`, etc.) silently returned 0 for every live game
- Fix: normalize `home_team_id`/`away_team_id` to `int` at top of `extract_features()`
- Discovered via `pw_compare.py` analysis — all 6 live games (2026-05-21 to 2026-05-29) had 0 PBP conditions despite active scoring runs
- PBP conditions (momentum_shift, error_streak, penalty_pressure, line_break_surge, try_scoring_run, points_run) will now fire during live games for the first time

**Files changed:** `timeline_features.py`

---

## 2026-05-29 — LIVE: split live stats into 1H/2H halves (#125)

- **Monitor:** Captures halftime stats snapshot (`ht_stats_{match_id}` in alert state) when `matchState == "HalfTime"`. During 2H/ET, computes per-half stats by subtracting 1H from current cumulative values.
- **Dashboard:** Full/1H/2H toggle buttons above the 12-stat grid when halftime data is available. Count stats (errors, penalties, run metres, line breaks, etc.) split correctly; rate stats (possession%, completion%, effective tackle%, PTB speed) show "–" in per-half views since they can't be subtracted.

**Files changed:** `monitor.py`, `dashboard.html`

---

## 2026-05-29 — PW Trends comparison tables and edge breakdown (#122)

- **Edge Range Breakdown table:** New table below Version Cohorts with per-edge-range metrics (10+, 5-10, 0-5, <0). Color-coded labels. Edge ≥10 = 97.9% accuracy vs <0 = 71.4%.
- **Changelog table fixes:** Switched from `findSnapshotAfter` (mapped v1-v5 to same sparse snapshot) to version cohort data from `cumulative_snapshot`. Each row now shows that version's actual calls/games. Added "v0-unknown" prior row and "All" totals row.
- **Version Cohort "All Versions" row:** Uses `cumulative_snapshot.games_in_sample` (409) instead of sum of per-version games (408).
- **Non-cumulative mode fix:** Comparison tables (changelog, versions, edges) were showing 1 game when chart toggle set to "Per Interval". Server now always includes `cumulative_snapshot` in response; tables use it regardless of chart display mode.
- **Cache-Control:** Added `no-cache` header for HTML static files to prevent stale dashboard after updates.

**Files changed:** `pw_trend.py`, `server.py`, `dashboard.html`

---

## 2026-05-29 — Fix Analysis Outcomes crash from #124 typo

- **Bug:** Analysis tab returned `NameError: name 'pw' is not defined`, breaking the entire Outcomes tab.
- **Root cause:** `pw_matrix.py:325` used `pw.get("bk_moneyline")` but the call dict variable is `call`, not `pw`. Typo introduced when adding BK odds ROI tracking in #124.
- **Fix:** Changed to `call.get("bk_moneyline")`.

**Files changed:** `pw_matrix.py`

---

## 2026-05-29 — Analysis Outcomes PW Accuracy NBA parity (#124)

- **By-threshold table:** Added 4 columns — BK Spread ROI, BK Odds ROI, Covered Not Won %, Won Not Covered %.
- **By-consensus table:** Same 4 new columns added for parity.
- **Backend (`pw_matrix.py`):** Added `covered_not_won`, `won_not_covered` set tracking in `_accum()`, BK odds P&L from `bk_moneyline`, finalization in `_finalize_bucket()`.
- Both tables now have 13 columns matching NBA Monitor layout.

**Files changed:** `pw_matrix.py`, `dashboard.html`

---

## 2026-05-29 — Fix Analysis Outcomes segment filter reverting to SOO (#123)

- **Bug:** Segment filter reverted to "State of Origin" when any other value was selected.
- **Root cause:** `_restoreAnalysisFilters()` called on every `loadAnalysis()`, overwriting user selection with stale localStorage.
- **Fix:** Only restore on first load (`_analysisFiltersRestored` flag). Save current values on subsequent loads.

**Files changed:** `dashboard.html`

---

## 2026-05-29 — PW tab: Source, Edge Range, Polarity filters + edge badge (#122)

- **3 new filter dropdowns:** Source (Game: Live/Backfill + Call: Live/Backfill), Edge Range (All/≥10/5-10/0-5/<0), Polarity (All/Positive/Negative/Neutral). Server-side filtering per-call in `build_pw_calls_payload`.
- **Source filter split:** Distinguishes game data origin (`source` field) from PW call generation method (`synthetic` flag). Game: Live = monitored live; Call: Live = non-synthetic call.
- **Edge badge** on game card header (both PW-correct and PW-wrong lines). Color: green ≥5, yellow ≥0, red <0.
- **Edge/Polarity columns** in call table now fully populated from migration data.
- **Accuracy stats breakdown:** Polarity (Pol+/Pol−/Pol=) and Edge range (≥10/5-10/0-5/<0) pills added to accuracy stats block. Client-side aggregation from call data.
- **Game History PW table:** Added Edge (color-coded), Polarity (net + counts), Quarter columns.
- **Outcomes polarity badges:** Green `+` / red `−` badge next to each condition name. 17 positive, 15 negative, 1 neutral. Computed via `classify_polarity()` from type + name.
- Key findings: Edge ≥10 backfill calls = 96.2% accuracy. Positive polarity calls = 92.4% accuracy.

**Files changed:** `server.py`, `outcomes.py`, `dashboard.html`

---

## 2026-05-29 — Migrate all PW calls: avg_edge, polarity, quarter, source (#117, #118, #121)

- **Migration script** enriched all 1,292 PW calls in-place (no re-backfill):
  - `avg_edge`: 11% → 100%. Computed from `active_conditions[].condition_edge` or `ConditionEdgeStore`.
  - `polarity_pos/neg/net`: 11% → 100%. Computed using `classify_polarity()` with name-based word matching (#118).
  - `quarter`: 16% → 100%. Derived from `game_minute`.
  - `source`: 0% → 100%. Copied from parent game record (#121).
  - `pw_version`: already 100%.
- **Code fix** (`f54e4a3`): `monitor.py` now stores these fields on new live PW calls.
- **Polarity fix** (#118): `classify_polarity()` uses name-based word matching for direction-dependent types (e.g. "Poor run metres" → neg, "Dominant run metres" → pos).

---

## 2026-05-29 — Add source and pw_version tagging to all PW calls (#121)

PW calls now include explicit `source` and `pw_version` fields for provenance tracking.

- `monitor.py`: Live PW calls tagged `"source": "live"`
- `backfill.py`: Backfill PW calls tagged `"source": "backfill"`
- `server.py`: Older calls without these fields enriched at serve-time — `pw_version` derived from `resolve_pw_version(ts[:10])`, `source` derived from `synthetic` flag

Cross-ref: NBA #297

---

## 2026-05-29 — Quarter-level stats from timeline events (#120)

Added `scope: "quarter"` and `scope: "half"` config option for `error_rate`, `penalty_count`, and `line_breaks` conditions. When set, counts events from the NRL timeline within the current synthetic quarter or half instead of using full-game cumulative API stats.

### Changes
- `timeline_features.py`: New `count_events_by_period()` function counts Error, Penalty, LineBreak, Try, and SinBin events per synthetic quarter (Q1-Q4/ET) and half (H1/H2). Counts included in `extract_features()` output.
- `conditions.py`: `evaluate_error_rate()`, `evaluate_penalty_count()`, `evaluate_line_breaks()` now support `scope` config key. Default `"game"` preserves existing behavior.
- `conditions.py`: New helpers `_get_period_count()` and `_get_half_count()` resolve counts from `pbp_features`.

### Config example
```json
{"name": "Q penalties 3+", "type": "penalty_count", "scope": "quarter", "threshold": 3, "alert": true}
{"name": "H1 errors 5+", "type": "error_rate", "scope": "half", "threshold": 5, "alert": true}
```

---

## 2026-05-29 — Live monitoring PBP features parity (#119)

Fixed live monitoring not passing PBP features to condition evaluation — 6 PBP-based conditions (`momentum_shift`, `error_streak`, `penalty_pressure`, `line_break_surge`, `try_scoring_run`, `points_run`) could not fire during live games.

### Changes
- `monitor.py`: Import `timeline_features`, call `extract_features()` on raw timeline data during live polling
- `monitor.py`: `build_team_context()` accepts optional `pbp_features` parameter, populates `consecutive_tries`, `unanswered_points`, and `pbp_features` in team context
- Both condition evaluation passes (base + PW threshold) now receive PBP features

### Impact
- PBP conditions now fire during live games (were silently failing before)
- PW calls during live games now have full condition signal (parity with backfill)
- Compound conditions referencing PBP conditions now work live

---

## 2026-05-29 — Fix polarity classification for direction-dependent conditions (#118)

`classify_polarity()` in `outcomes.py` now accepts an optional `condition_name` parameter for name-based classification of direction-dependent condition types.

### Problem
- NRL used condition **type** for polarity classification, but bidirectional types (e.g., `run_metres`) could be positive or negative depending on the threshold direction
- `Poor run metres (<1400)` was misclassified as positive (type `run_metres` in `_POSITIVE_PATTERNS`) — edge was -40.1
- 13 conditions were unclassified (types like `score_diff`, `possession`, `completion_rate`)

### Fix
- Name-based word matching checked first: negative words (`poor`, `low`, `trailing`, `losing`, `critical`, `ineffective`) and positive words (`dominant`, `dominating`, `winning`, `strong`, `blowout`, `high`, `hot`, `attacking`, `creative`)
- Whole-word matching via `re.findall()` to avoid substring issues (e.g., "low" in "blowout")
- Removed ambiguous `run_metres` from `_POSITIVE_TYPES`, added unambiguous `kick_return_metres`
- Falls back to type-based classification for unambiguous types
- Updated callers in `outcomes.py` and `backfill.py` to pass condition name
- Result: 0 mismatches, 1 intentionally unclassified (`Slow play-the-ball` — counterintuitively positive edge)

Cross-ref: NBA #296

---

## 2026-05-29 — PW config comparison and filter evaluation tool (#116)

New `pw_compare.py` for offline A/B testing of PW configurations. Read-only — never modifies game_history or config.

### Three modes
- **Config A/B** (`--threshold-a 60 --threshold-b 75`): compare two configs with different thresholds, margin gates, or edge gates
- **vs-stored** (`--vs-stored`): compare stored PW calls in game_history vs freshly generated with current config
- **evaluate-filters** (`--evaluate-filters`): test suppression strategies (margin gate, edge gate, polarity, consensus) with net improvement metrics

### Metrics
- Accuracy, flat/ML/spread ROI, leading/trailing splits
- Half, quarter, consensus, edge distribution breakdowns
- Confidence calibration table (expected vs actual accuracy per 5% band)
- Filter evaluation: wrong removed, correct lost, net improvement, precision

### Usage
```bash
python3 pw_compare.py --evaluate-filters --season 2026
python3 pw_compare.py --vs-stored --since 2026-04-01
python3 pw_compare.py --threshold-a 60 --threshold-b 75 --season 2026
python3 pw_compare.py --edge-gate 2.0 --season 2026
python3 pw_compare.py --evaluate-filters --season 2026 --source live
```

- `--source live|backfill` filter on all modes — uses `source` field (#121) with `synthetic` flag fallback for older calls

**Files changed:** `pw_compare.py` (new)

---

## 2026-05-28 — Fix Odds API tab TypeError for NBA/WNBA (NBA #293)

- Same fix as NRL #107. Wrapped `live_ts`/`log_ts` with `String()` in NBA/WNBA dashboard. Crashed when filtering by time range (e.g. "24 hours").

---

## 2026-05-28 — Monitor Overview: separate Games Today and Upcoming lines (#115)

- Two separate lines instead of combined "Games today": "Games today (Wed 28 May 2026 AEST)" and "Upcoming games (next 3 days)".
- Server splits by AEST date. Upcoming window from `upcoming_hours` config. SOO games show 🏆 badge.
- Also ported to NBA/WNBA (#292). NBA fix: upcoming count fetches from `/api/upcoming` (not scoreboard which only has today's games), uses `CONFIG.upcoming_hours` for label.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-28 — PW tab: separate PW and Winner lines when prediction wrong (#113)

- **When PW wrong:** Two lines in game card header instead of one misleading combined line.
  - **PW line:** Predicted team (red), NO badge, role (FAV/DOG), pregame odds, BK odds (blue + bookmaker), spread → Live → BK, missed by X.
  - **Winner line:** Actual winner (green), margin, final score, winner's odds from game record, spread → BK, covered/missed by X, role.
- **When PW correct:** Single combined line (unchanged).
- Server passes `home_ml`, `away_ml`, `home_spread`, `away_spread`, `season_segment` in PW calls response.
- BK spread on Winner line correctly flips sign from PW-predicted team to winner team (`_winIsPwTeam`, not home/away).
- Also ported to NBA/WNBA (#291). NBA fix: `winner_id` lookup instead of `winner_name` (doesn't exist on NBA records). Added `home_name`/`away_name` to gdata.

**Files changed:** `server.py`, `dashboard.html` (NRL + NBA)

---

## 2026-05-28 — Games tab: count indicators on all sub-tabs (#114)

- Added `(N)` count badges to PW (game count), Alert History (alert count), Upcoming (game count), Post Game (game count) sub-tabs. Live Games already had one. Each updated when tab data loads.

**Files changed:** `dashboard.html`

---

## 2026-05-28 — Config save: feedback on all tabs (#112)

- All 5 config sub-tab save-status spans updated simultaneously (previously only first tab showed feedback).
- Shows "Saving..." on press, "Saved ✓" in green on success (auto-fades after 3s), error in red on failure.
- `_setAllSaveStatus()` helper updates all status spans at once.

**Files changed:** `dashboard.html`

---

## 2026-05-27 — Odds Monitor: live snapshot persistence via JSONL (#111)

- Each snapshot appended to `odds_monitor_live.jsonl` as captured — survives dashboard restarts.
- On thread start, recovers snapshots from live file into in-memory state.
- File cleared when all games persisted to history or on auto-stop.
- Same append-only JSONL pattern as `odds_api_log.jsonl`.

**Files changed:** `odds_monitor.py`, `.gitignore`

---

## 2026-05-27 — Odds Monitor: continue polling past game window when active (#110)

- **Bug:** Odds Monitor stopped capturing data when SOO game extended past 12:00 UTC game window. Dashboard restart at 12:04 UTC lost all in-memory snapshots.
- **Fix:** Only skip outside UTC 06-12 window when no games are being monitored. If `_state["snapshots"]` has active data, continue polling regardless of time.

**Files changed:** `odds_monitor.py`

---

## 2026-05-27 — Fix SOO odds: missing team name mapping (#109)

- `NRL_TO_ODDS_API` was missing Blues→"New South Wales Blues" and Maroons→"Queensland Maroons". Odds Monitor captured snapshots but all odds were None because `_match_event()` couldn't match the SOO game.
- SOO odds limited: only 2 bookmakers (Unibet, TABtouch) with h2h only — no spread or total markets.

**Files changed:** `odds_api.py`

---

## 2026-05-27 — Fix live conditions: deduplicate accumulated hits (#108)

- **Bug:** Live Games tab showed 90+ conditions for a single game — same conditions repeated every poll cycle.
- **Root cause:** `cond_hits_{match_id}` accumulated ALL fires across every poll cycle. Cumulative stat conditions (e.g. "Poor run metres") fire every minute since the value stays above threshold.
- **Fix:** Deduplicate when building `live_stat["conditions_fired"]` — keep only latest fire per unique `team|condition` key. 95 fires → 17 unique conditions.

**Files changed:** `monitor.py`

---

## 2026-05-27 — Fix Odds API tab TypeError on live_ts.replace (#107)

- `_liveCredits.live_ts` can be null or non-string when live credit query hasn't run. Wrapped with `String()` to prevent TypeError when filtering by time range.

**Files changed:** `dashboard.html`

---

## 2026-05-27 — CRITICAL: Fix SOO fixture loop overwriting state dict (#106)

- **Bug:** `state = fix.get("matchState", "")` in the SOO fixture merge loop (added in #102) shadowed the outer `state` dict (alert state) with a string. Every subsequent `state.get(...)` call crashed with `AttributeError`.
- **Impact:** All monitor runs crashed when 2026 SOO fixtures existed. No live games detected (Premiership or SOO), no conditions, no PW calls, no alerts.
- **Fix:** Renamed to `_soo_state` (d157bf6).

**Files changed:** `monitor.py`

---

## 2026-05-27 — Backfill SOO 2006-2015 with conditions and PW calls (#105)

- **30 games added** (2006-2015) → 60 total SOO games (2006-2025, 20 seasons).
- **491 conditions** across 60 SOO games (8.2 avg/game).
- **146 PW calls** at 91.8% accuracy (condition-based with edge gate, polarity).
- **Total history:** 992 games (932 Premiership + 60 SOO).
- **Backfill note:** First background run failed for 2006-2011 — NRL.com API returned different URL formats for older SOO seasons (e.g. `/draw/state-of-origin/2012/game-1/blues-vs-maroons/` vs newer `/draw/state-of-origin/2025/round-1/maroons-v-blues/`). The `get_soo_fixtures()` function returned fixtures but `matchCentreUrl` paths differed, causing some match detail fetches to timeout (503/timeout). Subsequent direct run succeeded for all 30 games by retrying with the correct URLs from `get_soo_fixtures()`.

---

## 2026-05-27 — Odds Monitor: SOO live capture + history filter (#104)

- **Live capture:** Poll loop merges SOO fixtures and fetches odds from `rugbyleague_nrl_state_of_origin` when SOO games are in the monitoring window (typically 09:40-10:10 UTC — within existing systemd 06-12 UTC window).
- **History tagging:** Persisted records include `competition`, `soo_game`, `round_title`.
- **History filter:** Dropdown (All/Premiership/State of Origin) with client-side filtering + purple SOO badge on history rows.
- **Systemd:** No change needed — SOO kickoffs (09:40-10:10 UTC) are within the existing 06:00-12:00 UTC game window.

**Files changed:** `odds_monitor.py`, `dashboard.html`

---

## 2026-05-27 — SOO badges + Upcoming Games support (#103)

- **Upcoming Games:** SOO fixtures merged from `get_soo_fixtures()` into upcoming payload. Purple badge with "State of Origin — Game 1/2/3".
- **Live Games:** Purple "SOO Game 1" badge in game card header. Competition/soo_game/round_title passed through `live_stat` dict.
- **Post Game:** Purple "State of Origin — Game 1" badge above completed SOO game cards. `season_segment` + `round_title` added to postgame response.
- Consistent purple badge style (`#a855f7`) across all tabs.

**Files changed:** `server.py`, `monitor.py`, `dashboard.html`

---

## 2026-05-27 — State of Origin support: isolated segment + backfill (#102)

- **SOO isolation:** New `season_segment=state_of_origin` with `soo_game=1/2/3` tagging. Fully isolated from Premiership data in all analysis, PW, ML training, outcomes.
- **Auto-detection:** `monitor.py` fetches SOO fixtures (competition=116) alongside Premiership. SOO games auto-detected, tagged, and monitored with separate Odds API sport key (`rugbyleague_nrl_state_of_origin`).
- **Dashboard filters:** "State of Origin" option in all 8 segment dropdowns (PW, PW Trends, ROI Matrix, Outcomes, Game History, Custom Queries). SOO Game sub-filter (Game 1/2/3) appears conditionally. SOO rounds display as "Game 1/2/3".
- **Backfill:** 30 SOO games (2016-2025, 3 per season) with full stats, try events, score progression. Teams: Blues (NSW) / Maroons (QLD).
- **Conditions backfill:** 273 conditions across 30 SOO games (9.1 avg/game).
- **PW backfill (condition-based):** 78 calls across 26 games at 89.7% accuracy. Avg edge: 9.31. Active conditions, polarity, edge gate applied. Score progression computed from timeline events (SOO timeline lacks running scores).
- **ML exclusion:** SOO games excluded from `ml_model.py` training data.
- **Origin Game filter:** `soo_game` dropdown (Game 1/2/3) on all tabs — PW, PW Trends, ROI Matrix, Outcomes, Game History, Custom Queries. Appears conditionally when State of Origin segment selected. Server-side filter on all endpoints (`pw-calls`, `pw-trend`, `pw-matrix`, `analysis`, `custom-queries`, `history`).
- **Filter fixes:** Fixed PW tab leaking Premiership games under SOO filter. Fixed `soo_game` filter missing from `build_history_payload`. Added `onchange` handlers.
- Total history: 962 games (932 Premiership + 30 SOO).

**Files changed:** `nrl_api.py`, `odds_api.py`, `monitor.py`, `server.py`, `ml_model.py`, `dashboard.html`

---

## 2026-05-27 — NRL 2022+2023 backfill + PW coverage analysis (#100)

- **2022**: 201 games, 84 with PW (41.8%), 264 calls, 95.1% accuracy
- **2023**: 213 games, 79 with PW (37.1%), 254 calls, 94.5% accuracy
- **Total**: 932 games, 359 with PW (38.5%), 1135 calls, 93.0% accuracy
- **Edge gate audit**: `avg_edge` is `None` on all 1135 calls — backfill computes edge for gate check but never stores on call dict. Missing fields: `avg_edge`, `polarity_pos/neg/net`, `quarter` (except 2026 live)
- **Coverage analysis**: 61.5% no-PW games are mostly close games (66% ≤12pt margin). 98% of calls in H2. PW avg margin at fire: 19.8pts.

**Files changed:** `game_history.json`

---

## 2026-05-27 — Live Odds API credits in Overview + Odds API tabs (#101)

- `odds_api.py`: `query_live_credits()` calls free `/v4/sports/` endpoint (cached 60s). `get_log_credit_status()` reads last log entry with credits.
- `server.py`: `_get_odds_api_credits()` returns combined log + live + session data. Added to `/api/monitor-status` and `/api/odds-api-stats`.
- **Overview panel:** Odds API status row with live balance, OK/Low/Critical indicator, session calls/credits/errors.
- **Odds API tab:** Credits card shows live balance vs log reading with timestamps. "Live: X · Log: Y" when values differ.
- Also applied to NBA/WNBA (#290).

**Files changed:** `odds_api.py`, `server.py`, `dashboard.html`

---

## 2026-05-27 — Backfill NRL 2022 + 2023 seasons (#100)

- **2023 season**: 213 games added from NRL.com API → `nrl_cache/2023/` (29 MB)
- **2022 season**: 201 games added from NRL.com API → `nrl_cache/2022/` (27 MB)
- **Total**: 932 games across 5 seasons (2022: 201, 2023: 213, 2024: 213, 2025: 213, 2026: 92)
- No odds or PW calls backfilled (deferred)

---

## 2026-05-27 — Odds API chart: Daily/Weekly/Monthly intervals (#99)

Added Daily, Weekly, Monthly to the Usage Over Time chart interval dropdown (alongside existing 1/5/15/30/60 min). Bucketing: daily by YYYY-MM-DD, weekly by ISO week (YYYY-Wnn), monthly by YYYY-MM. X-axis labels adapt per interval type. Ported to NBA/WNBA in #287.

**Files changed:** `dashboard.html`

---

## 2026-05-27 — Fix Odds API tab: BK stats from log + paginated fallback table (#97, #98)

### BK stats from log (#97)
Bookmaker Market Coverage and Fallback Audit Trail tables were always empty because `_bk_stats` was in-memory only (in `monitor.py` process, lost on exit). Fix: `_track_bk_usage()` now writes `type="bk_usage"` entries to `odds_api_log.jsonl`. Server computes BK stats from log entries (both `bk_usage` and `fallback` types).

### Paginated fallback table (#98)
Fallback Audit Trail (3000+ rows) now paginated: page size selector (10/25/50/100, default 25), First/Prev/Next/Last navigation, record count, sort toggle on Time column (newest/oldest first).

**Files changed:** `odds_api.py`, `server.py`, `dashboard.html`

---

## 2026-05-27 — Fix Odds API fallback log: primary bookmaker mismatch (#96)

`primary_name` in `extract_odds()` was using `BOOKMAKER_PRIORITY[0]` key (`"sportsbet"`) but comparing against `bk.get("title")` (`"Sportsbet"`) — case mismatch caused false fallback logging even when primary BK served the data. Also showed "primary bet365" from old priority list before #89.

Fix: resolve `primary_name` by looking up first priority BK in the API response and using its **title**. Falls back to first available BK's title if no priority BK is in the response.

**Files changed:** `odds_api.py`

---

## 2026-05-27 — Monitor Status Overview: NBA parity (#95)

8 enhancements to the Monitor Status Overview tab:

1. **Games Today** — new row showing game count, matchups with kickoff times (local TZ), live indicators
2. **Teams Monitored** — shows team count: "All 17 teams" or "3 of 17 teams: Storm, Panthers, ..."
3. **Alert Channel** — shows both Slack channel name AND ID: "#nrl-alerts (C0B4HFXE4PK)"
4. **ML Model** — adds last trained date/time in local timezone with UTC in parentheses
5. **Scheduler** — shows local TZ window with UTC in parens: "AEST 16:00–22:00 (UTC 06:00–12:00)"
6. **Last Backup** — parses backup timestamp, shows local + UTC + backup label
7. **Disk Space** — adds % disk free alongside GB
8. **Monitor last polled + Current Time** — renamed from "Last run", shows local timezone label (AEST)

Server: added `games_today`, `total_teams`, `slack_channel`, `disk_pct_free` to `/api/monitor-status`.

Fix: scheduler local time conversion used Jan 1 (AEDT/UTC+11) instead of today's date — gave wrong offset during AEST (UTC+10). Now uses current date for DST-aware conversion.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-26 — PW Trends help modal updated (#94)

Comprehensive rewrite of PW Trends (?) help modal: Timeframe Controls (Source, Timeframe, Date pickers, Interval incl. 1-60min game-minute, Cumulative/Per Interval toggle), NRL-specific filters (Half H1/H2/ET, Quarter Q1-Q4/ET), BK Spread ROI, server-side `compute_trend_series` behavior. Port from NBA #284.

**Files changed:** `dashboard.html`

---

## 2026-05-26 — Dashboard: Slack settings in Configuration (#93)

Slack channel ID, channel name, and bot token now editable from dashboard Configuration > Settings > Alerting. Previously hand-edited in config.json only. Token field masked and only saves if non-empty.

**Files changed:** `dashboard.html`

---

## 2026-05-26 — PW Underdog Alert + 3-way alert_mode (port from NBA #282) (#92)

### 3-way alert_mode
Replaces boolean `alert` field on conditions with `alert_mode`:
- `"none"` — no alert at all
- `"history"` — alert history only (no Slack)
- `"slack"` — Slack + alert history

Backward compatible via `_get_alert_mode()`. Dashboard Conditions tab shows dropdown.

### pw_underdog_alert condition
Alert-only condition for underdog PW calls in configurable quarters:
- `min_confidence`, `quarters`, `max_alerts_per_quarter_per_team`
- NRL synthetic quarters: Q1 (0-20'), Q2 (20-40'), Q3 (40-60'), Q4 (60-80'), ET (80'+) — derived from `game_minute` via `_quarter_from_minute()`
- Dashboard: quarter checkboxes (Q1-Q4 + ET), min confidence input, max alerts input, help text
- Default: Q3+Q4 (last 40 min of regulation)
- Added to `CONDITION_TYPES` whitelist in `server.py` (also added `first_team_scores` and `predicted_winner_threshold` which were missing)

**Files changed:** `monitor.py`, `server.py`, `dashboard.html`

---

## 2026-05-26 — Live Games: NBA-style card layout (#91)

Restructured live game cards to match NBA Monitor layout:

- **Card structure**: `gc-header` (team matchup + venue left, period/clock right), `gc-score` (separate team blocks with large score, leading team green), `gc-body` (collapsible wrapper)
- **Border**: 3px conditional — red for live, green for monitored (was 4px always green)
- **Collapse button**: custom JS with `_liveCollapsed` state tracking per game (replaces `<details>` elements)
- **Score blocks**: separate team blocks with 1.6rem score, winner highlighted, dash separator
- **Links**: centered NRL.com link row

All NRL-specific content preserved: 12 rugby stats grid, scoring timeline with player names/icons, PW badges, bookmaker sources.

**Files changed:** `dashboard.html`

---

## 2026-05-25 — Switch odds_api_log to JSONL format (#84)

- **Root cause:** `_append_log()` did read-modify-write (`json.load` → append → `json.dump`) without cross-process locking. `monitor.py` and `server.py` both wrote, causing corruption (concatenated JSON arrays).
- **Fix:** Switched to JSONL (line-delimited JSON). `_append_log()` now does `open('a')` + `write(line)` — append-only, no race condition.
- File renamed from `odds_api_log.json` to `odds_api_log.jsonl`.
- Auto-migration from legacy JSON array on first load (handles corruption). Original renamed to `.json.migrated`.
- `load_log()` reads JSONL line-by-line. `truncate_log(keep=N)` for periodic cleanup.
- Updated `RUNTIME_FILES` and `.gitignore`.

**Files changed:** `odds_api.py`, `runtime_backup.py`, `.gitignore`

---

## 2026-05-25 — Odds Monitor: configurable activate minute (#90)

- Changed `ACTIVATE_MINUTE` from 70 to 65 (default) to capture totals data before bookmakers pull the market (~minute 65-72).
- New config key `odds_monitor_activate_minute` — configurable via dashboard Configuration → Settings → Odds Monitor section.
- Read from config at each poll cycle (no restart needed).

**Files changed:** `odds_monitor.py`, `dashboard.html`, `config.example.json`

---

## 2026-05-25 — Change primary bookmaker from Bet365 to SportsBet (#89)

- Bet365 is never available for NRL on The Odds API (AU region). SportsBet has 100% coverage for ML + spreads and 91% of historical log entries.
- Updated `BOOKMAKER_PRIORITY` to use actual AU bookmaker API keys: `sportsbet`, `tab`, `pointsbetau`, `ladbrokes_au`, `unibet`, `neds`, `tabtouch`, `betright`, `betr_au`, `playup`, `betfair_ex_au`.
- Eliminates noisy "primary bet365 unavailable" fallback log entries.

**Files changed:** `odds_api.py`

---

## 2026-05-25 — Odds Monitor history: expandable rows + view persistence (#88)

- **Expandable history rows:** Each game shows collapsible summary + full snapshot detail.
- **Summary row (collapsed):** Date, teams with icons, final score, final total, OVER/UNDER/PUSH result, total line with move, snapshot count.
- **Expanded stats bar:** Monitoring window (e.g. 70'-83'), ML (home/away with move), spread (with move), spread covered YES/NO, tries in window, best EV edge, bookmaker names.
- **Expanded snapshot table (14 columns):** Time, Min, Score, Total, Total Line, Over $, Under $, Spread, H Spr $, A Spr $, Home ML, Away ML, Tries, EV Edge.
- **Data saved per game:** match_id, teams, snapshot_count, first/last timestamps, final scores, over/under result. Per snapshot (every 60s): game_minute, period, scores, total_points, tries_in_window, try_events_in_window, full odds (total line/prices, ML, spread/prices, bookmaker sources), EV analysis (implied probs, historical try probs, EV edges).
- **View persistence:** Live/History dropdown saved to localStorage (`om_view`). Restored synchronously before async fetch so history loads immediately when tab opens.
- **Sub-tab persistence:** Active sub-tab per panel saved to localStorage (`active_sub_{panel}`). Restored when switching panels so Odds Monitor stays active across navigation.
- **Auto-refresh disabled in history view** — only polls when monitor is running AND view is live.

**Files changed:** `dashboard.html`

---

## 2026-05-24 — Fix backup file count mismatch between Config and Recovery tabs

- Config tab used metadata `copied_files` list length (excluded `metadata.json`). Now uses actual directory file count from disk via `file_count` field, matching the Recovery tab.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-24 — Backup retention settings in dashboard config

- New config key `backup_retention` with `keep_scheduled` (default 10), `keep_preconfig` (default 10), `prune_days` (default 60).
- Settings in Configuration → Backups sub-tab with save/load support.
- Pre-config-save prune now uses config value instead of hardcoded 10.
- CLI `runtime_backup.py` falls back to config.json when `--keep-last` not specified.
- Recovery tab "Prune Now" default populated from config.

**Files changed:** `dashboard.html`, `server.py`, `runtime_backup.py`, `config.example.json`

---

## 2026-05-24 — Recovery tab improvements (#87)

- **Multi-select delete:** Checkboxes per row, Select All, "Delete Selected" button with batch API support.
- **File count column:** Shows files per backup directory (helps identify incomplete backups).
- **Records tooltip:** Column header explains "Number of games in game_history.json at backup time".
- **Backup Now button:** Creates manual backup from Recovery tab via `/api/snapshots/create`.
- **Refresh button:** Reloads backup list.

**Files changed:** `outcomes.py`, `server.py`, `dashboard.html`

---

## 2026-05-24 — Add 5 missing runtime files to scheduled backups (#86)

- Added `game_history.wal` (crash recovery replay), `config.json` (user config), `ml_training_history.json`, `model_eval_history_nrl.json`, `odds_monitor_history.json` to `RUNTIME_FILES` in `runtime_backup.py`.
- Total backed up files: 15 (was 10).

**Files changed:** `runtime_backup.py`

---

## 2026-05-24 — Fix corrupted odds_api_log.json (#83)

- **Bug:** Odds API stats tab showed no data — `odds_api_log.json` was corrupted (two concatenated JSON arrays).
- **Root cause:** Race condition between `monitor.py` and `server.py` both calling `_append_log()` with read-modify-write without cross-process locking (#84).
- **Data fix:** Recovered 2356 entries by parsing and merging both arrays (304 calls, 287 OK, 17 rate-limited). No code change.

---

## 2026-05-23 — Fix odds monitor history not persisting (#82)

- **Root cause:** `persist_game()` was called from `monitor.py` (separate process) but odds monitor snapshots only exist in `server.py`'s in-memory state. Fresh import had empty state → no data to persist.
- **Fix:** Persistence moved into odds monitor's own poll loop (same process). Detects games transitioning from active to ended, persists snapshots + final scores. Also persists remaining on auto-stop.
- Removed broken `persist_game()` call from `monitor.py`.

**Files changed:** `odds_monitor.py`, `monitor.py`

---

## 2026-05-23 — Custom Queries: restructure Late 2H Tries (#78)

- Replaced "1+/2+/3+ tries in window" tables with "1st/2nd/3rd try scored within window" tables.
- Each table shows when the Nth try occurred relative to 10/7/5/2 min windows, with eligible game count and % of all games.
- Key findings: 1st try within 5min in 34.1% of games; 2nd try within 5min in 87.3% of eligible; 3rd try within 2min in 71.9% of eligible.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-23 — Fix odds monitor running after game ends (#81)

- **is_live after FullTime:** NRL API keeps `matchMode: "Live"` briefly after `matchState: "FullTime"`. Added `and state not in COMPLETED_STATES` to `is_live` check in `nrl_api.py`.
- **Odds monitor auto-stop:** Thread now tracks consecutive idle polls and auto-stops after 5 polls (~5 min at 60s interval) with no active games. `_poll_once` returns `True/False` for tracking.

**Files changed:** `nrl_api.py`, `odds_monitor.py`

---

## 2026-05-23 — Fix PW tab: live game visibility + auto-refresh (#80)

- **Live game with 0 calls:** Live games now appear in PW tab even before first PW call. Shows "No PW calls yet" message. Server injects live games from `live_stats` into PW calls response.
- **Auto-refresh fix:** Timer used wrong selector (`document.querySelector('.sub-tab.active')` matched other panels). Fixed to `#panel-games .sub-tab[data-sub="games-pw"]`. Extracted `_pwStartLiveTimer()` helper that restarts on cached tab re-entry.
- **Live-only refresh:** Auto-refresh now uses `_pwRefreshLiveOnly()` which fetches fresh data but only updates live game entries in `_pwGames`. Historical games are untouched — no full page re-render. Matches NBA PW tab approach.
- **Live header fix:** Stopped showing "Winner: X by N" for live games (derived from current score). Now shows current score + pregame BK odds (H2H, Spread, Total, bookmaker).

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-23 — PW tab: live game header + pregame odds + totals investigation (#79)

- **Live game header fix:** Stopped showing "Winner: ..." line for live games (winner was derived from current score). Now shows current score + pregame BK odds (H2H, Spread, Total O/U) with bookmaker name.
- **Pregame odds on live PW games:** Server passes `pregame_home_ml`, `pregame_away_ml`, `pregame_home_spread`, `pregame_total`, `pregame_bookmaker` from `live_stats` pregame odds to PW calls response.
- **Totals market investigation (#79):** Bookmakers (TABtouch) pull totals market near end of game (~min 72). Not a code bug — working as designed. ML and spreads remain available.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-23 — PW tab: auto-refresh live games + status line

- **Auto-refresh:** PW tab refreshes every 60s when live games exist. Timer clears when user navigates away or no live games remain.
- **Status line:** Shows "Live — auto-refresh 60s · last: HH:MM:SS" next to Refresh button when live games active, "Updated HH:MM:SS" on manual refresh.

**Files changed:** `dashboard.html`

---

## 2026-05-23 — PW tab: hide History link for live games (#77)

- Live games showed 📋 History link but no game history exists until game ends. Added `source !== 'live'` check.

**Files changed:** `dashboard.html`

---

## 2026-05-23 — Fix misleading fallback log: primary bookmaker label (#76)

- Fallback audit trail incorrectly said "primary TAB unavailable" when TAB was actually the bookmaker being used. The `primary_name` was set to the first *available* bookmaker, not the *intended* primary from `BOOKMAKER_PRIORITY`.
- Now correctly references `bet365` as the intended primary. Bet365 is not available for NRL on The Odds API (AU region) — sportsbet/tab are the actual best available.

**Files changed:** `odds_api.py`

---

## 2026-05-23 — Fix PW tab: live game not listed (#75)

- **Bug:** Live games never appeared in the PW tab despite having PW calls in `.alert_state.json`.
- **Root cause:** `_skip_live_season` (undefined variable) instead of `_skip_live` in `build_pw_calls_payload()`. The `NameError` was silently caught, breaking the entire live merge block.
- **Fix:** Changed to `_skip_live`.

**Files changed:** `server.py`

---

## 2026-05-23 — Live Games: upcoming list + NRL.com/History links across tabs (#74)

- **Upcoming games in Live Games tab:** Single-line-per-game upcoming list shown at top of Live Games tab with day, kickoff time AEST, teams, venue, countdown, and NRL.com link. Fixed duplication on refresh.
- **NRL.com links (🏉):** Added to Live Games (upcoming line), Upcoming Games, Alert History (Full Time), Post Game Summary. Shared `_nrlLink()` helper.
- **History links (📋):** Added to Alert History (Full Time alerts) and Post Game Summary. Navigates to Game History detail view. Matches PW tab game header style (#4fc3f7, dotted underline).

**Files changed:** `dashboard.html`

---

## 2026-05-23 — Quarter-scaled thresholds for cumulative stat conditions (#69)

- Added `quarter_scale: true` config flag for cumulative stat conditions. When enabled, threshold scales by `min(game_minute / 80, 1.0)` — prevents conditions like "Poor run metres (<1400)" from firing misleadingly early (e.g., at minute 20 threshold becomes <350m).
- Applied to 13 conditions: error_rate, penalty_count, missed_tackles, run_metres, post_contact_metres, tackle_breaks, line_breaks, offloads, intercepts, ineffective_tackles, kick_return_metres.
- Not applied to percentage/rate conditions (completion_rate, possession, effective_tackle_pct, kick_defusal, play_the_ball_speed) — already normalized.
- `game_minute` added to condition evaluation context in `monitor.py`.
- Backward compatible — conditions without the flag behave as before.

**Files changed:** `conditions.py`, `monitor.py`, `config.example.json`

---

## 2026-05-23 — Odds API tab: error text, time filters, local timezone (#73)

- **Error code text** in Result column — `429 (Too Many Requests)`, `401 (Unauthorized)`, etc. via `_httpStatusText` lookup
- **Global time range filter** — dropdown: Last 1/6/12/24 hours, Last 1/10/30/60/90/120 days. Client-side filtering of all log entries + server-side for context stats. Shows "N of M entries" info.
- **Chart local date/time** — bucket keys and x-axis labels use local timezone. Shows date+time on first label and when date changes.
- **Local timezone everywhere** — `_oddsLocalTs()` and `_oddsLocalDate()` helpers applied to Log Entries, Daily table, Fallback Audit Trail timestamps.
- **Date From/To pickers** — custom date range filtering with mutual exclusion vs dropdown (selecting dates clears dropdown and vice versa).
- **Timezone label** — auto-detected abbreviation (e.g. "AEST") shown on all Date/Time column headings: Log Entries, Daily table, Fallback Audit Trail.
- **Reset button** — clears dropdown, date pickers, and reloads all data.
- **Filters apply to ALL tables** — date range passed to `/api/odds-api-stats` via `date_from`/`date_to` params. Call Results by Context, Fallback Audit Trail filtered server-side. Daily table filtered client-side.

Ported to NBA/WNBA in #278.

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-05-23 — Custom Queries: late try probability table (#67)

- Restructured late tries query from "does the scoring team win?" to "what is the probability a try is scored?"
- Unified table with: games with try, try %, total tries, avg/game, leading/trailing/tied split, win% as secondary
- Key findings: 74.6% of games have a try in last 10 min, 51.9% in last 5 min, 26.0% in last 2 min. Leading team scores ~57% of late tries.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-23 — Odds Monitor improvements (#71)

- **Poll interval:** Increased from 30s to 60s to reduce API credit usage
- **Game-end persistence:** `odds_monitor.persist_game()` now called at game-end in `monitor.py`, saving snapshots to `odds_monitor_history.json` with final scores and over/under result
- **Spread prices:** `extract_odds()` now captures `home_spread_price` and `away_spread_price` from Odds API. Added to odds monitor snapshots and displayed as "H Spr $" / "A Spr $" columns in the snapshot table
- **Totals investigation:** Documented in issue — Bet365 may not offer NRL totals; Sportsbet typically does. Fallback audit trail in logs confirms source per market.

**Files changed:** `odds_monitor.py`, `odds_api.py`, `monitor.py`, `dashboard.html`

---

## 2026-05-23 — Fix PW tab: live/recent games bypass season filter (#62)

- Live/recent PW games from `.alert_state.json` were injected into PW tab results regardless of the season filter — e.g. a 2026 game appeared when filtering for 2024
- Root cause: the live merge block in `build_pw_calls_payload()` only checked `if gid in games` (already in filtered set), not the season filter itself
- Fix: skip live/recent game injection when the season filter doesn't match the current year
- Same bug fixed in NBA/WNBA dashboard (`server.py`) using `_game_season_year()` for cross-year season format

**Files changed:** `server.py` (NRL + NBA/WNBA)

---

## 2026-05-23 — Bookmaker source tracking + Odds API stats (#72)

### Per-market bookmaker source
- `extract_odds()` returns `ml_source`, `spread_source`, `totals_source` — which bookmaker provided each market
- `fallback_log[]` audit trail when primary BK lacks a market (e.g. "Spread from TABtouch (primary TAB unavailable: no spreads market)")
- PW calls store `bk_ml_source`, `bk_spread_source`, `bk_totals_source`
- Pregame odds and Odds Monitor snapshots inherit per-market sources
- Fallback events logged to monitor.log

### Dashboard display
- **PW Tab:** BK Odds/Spread columns show source name as small muted text
- **Live Games:** odds bar shows ML source, spread source when different
- **Game History:** pregame info H2H/Spread rows show bookmaker name
- **Odds Monitor:** snapshot table shows source under Total Line, Spread, Home ML

### Odds API stats (Monitor Status > Odds API)
- **Call Results by Context** table — calls, OK, 429, errors, credits, avg latency per context (live_monitor, odds_monitor, upcoming, backfill_historical, etc.)
- **Bookmaker Market Coverage** table — per-BK count of ML/Spread/Totals provided, primary vs fallback usage (session-based). Always visible with empty-state message.
- **Fallback Audit Trail** — recent fallback events (last 200). Always visible with empty-state message.
- New `/api/odds-api-stats` endpoint with `?days=` time range filter

### Enhanced Log Entries table
- **Result column** — OK (green) / 429 (red) / CACHE (muted) / FALLBACK (orange) / ERR (red)
- **Context/Message column** — shows fallback message for fallback records
- **Row drilldown** — click any row to expand full record details (all fields)
- **Fallback persistence** — fallback events written to `odds_api_log.json` as `type="fallback"` with per-market source fields
- Context filter includes "fallback" type automatically

**Files changed:** `odds_api.py`, `monitor.py`, `odds_monitor.py`, `server.py`, `dashboard.html`

---

## 2026-05-23 — Enhanced live game cards (#70)

- **Conditions persist:** Accumulated condition hits stored in alert state per game (`cond_hits_{match_id}`). Conditions no longer disappear between poll cycles.
- **Scoring timeline:** Try, conversion, penalty goal, and field goal events extracted from NRL timeline API during live polling. Displayed in collapsible "Scoring Timeline" section with minute, player name, team, and event type.
- **Enhanced live game card:** Collapsible sections (Stats, Live Conditions, Scoring Timeline) using `<details>` elements. Expanded stats grid (12 stats: Poss%, Comp%, Errors, Penalties, Missed Tackles, Run Metres, Post Contact, Line Breaks, Tackle Breaks, Offloads, Eff Tackle%, PTB Speed). PW display with consensus, blended, polarity, and edge badges. Lead summary text. NRL.com game link. Condition pills show period and tooltip with direction/score/time.

**Files changed:** `monitor.py`, `dashboard.html`

---

## 2026-05-23 — Custom Queries: 3 new analyses (#67)

Three new query sections added below existing Custom Queries:

1. **First Scorer Wins 1H → Wins Game?** — summary stat: 80.8% of games where the first scorer also won 1H, they won the game (290/359 games)
2. **First Team to Score in 2H → Wins Game?** — summary (69.0%, 354/513 games) + sortable per-team breakdown table. Uses `score_progression` to detect first scoring event after halftime.
3. **Leading/Trailing Team Scores Late Try → Wins?** — separate tables by window (10/7/5/2 min). Uses `score_progression` to determine who was leading/trailing at each try's `game_seconds`. Leading: 99.7-100% win. Trailing: 0-4.7% win.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-23 — ROI Combos: lower default min_calls (#66)

- Lowered default `min_calls` from 10 to 1 (dashboard + server). NRL's PW dataset is heavily skewed (98% leading, 96% H2, 23% missing spread_role) — most combos had 0 calls and the old threshold filtered out nearly everything.

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-05-23 — PW tab: sort order toggle + BK odds cleanup (#56)

- **Sort order toggle:** Added "Newest First / Oldest First" button to PW tab. Sorts game cards by date, quarter groups (Q4→Q1 or Q1→Q4), and calls within groups by game_minute. State persisted to localStorage.
- **BK fields cleanup (#56 item 4):** Stopped enriching `bk_moneyline` and `bk_spread` from pregame odds at serve-time. BK columns now only show genuine live bookmaker data captured at PW fire time. Pregame odds still populate the Odds/Handicap columns. No game_history changes — enrichment is serve-time only.
- **Odds backfill from cache:** Ran `backfill_odds.py --use-cache` — 379 games now have pregame odds from existing odds_cache. PW calls Live Spread column populated via serve-time enrichment.

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-05-22 — PW Trends: add Season and Segment filters (#65)

- Added Season and Segment dropdown filters to the PW Trends tab filter bar
- Backend: `/api/pw-trend` accepts `season` and `segment` query params, filters `game_history` before computing trend series
- Dashboard: Season dropdown populated from `/api/history-facets`, Segment dropdown (All/Regular/Finals)
- Filter state persisted to localStorage (`pw_trend_state`)
- Clear button resets season/segment along with other filters

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-22 — Fix kickoff_utc and date on live game-end records

- **Root cause:** NRL API `startTime` field can be empty for completed games, so `game.get("start_time")` returned empty at game-end processing time. Live records got `kickoff_utc=None`, `date=None`.
- **Fix:** Capture `kickoff_utc` in alert state when game first goes live (same pattern as pregame odds). Use saved value as fallback at game-end. Also derive `date` field from kickoff time.
- **Data fix:** Patched existing R12 Raiders v Dolphins record with correct `kickoff_utc` and `date`.

**Files changed:** `monitor.py`

---

## 2026-05-22 — PW Trends date fix + PW tab season filter fix (#62)

- **PW Trends:** Live game records have `date=None` and `kickoff_utc=None` — added `rec.get("ts")` fallback in all 5 date extraction points in `pw_trend.py`. Without this, live games returned 0 data points in PW Trends.
- **PW tab season filter (#62):** Live/recent games from `.alert_state.json` bypassed season filter. Now skips injection when a non-current season is selected.

**Files changed:** `pw_trend.py`, `server.py`

---

## 2026-05-22 — Game-end record enrichment + cache writing (#63, #64)

**Issue:** #63, #64

### Game-end record (#63)
Completed game records now include all available data: `source:"live"`, 30 stats (home_stats/away_stats), PW calls from state with correct field, postgame tags (CLOSE/BLOWOUT/COMEBACK), pregame odds, try events with player names from NRL API timeline, 1H/2H scores, venue, kickoff_utc.

### Cache writing (#64)
Monitor now writes raw NRL API match data to `nrl_cache/{season}/match_{slug}_r{round}.json` at game end. Same format as backfill Phase 1 — enables future replays without re-fetching.

**Files changed:** `monitor.py`

---

## 2026-05-22 — Backfill PW: apply duplicate, HT, and ET suppression (#61)

**Issue:** #61

Ported live monitor suppression rules to backfill.py. Removed minute 40 (halftime) from eval points. Added duplicate suppression (same team+score as last call). ET calls validated as legitimate (games genuinely went to golden point).

Impact: 574 → 568 calls, 0 HT calls, accuracy unchanged at 93.0%.

**Files changed:** `backfill.py`

---

## 2026-05-22 — Fix live PW: duplicate suppression, HT suppression, invalid ET (#60)

**Issue:** #60

Three fixes for live PW call quality from first live game analysis (Raiders 22-30 Dolphins):

- **Duplicate suppression:** Skip recording when same team + same score as previous call (~65% noise reduction)
- **Halftime suppression:** Skip PW eval when period=="HT" or matchState=="HalfTime"
- **Invalid ET suppression:** Skip PW eval at minute 80+ when game not in extra time

Impact: 20 calls → 7 meaningful. Raw 90% → honest 71.4% accuracy.

**Files changed:** `monitor.py`

---

## 2026-05-22 — PW Trends: timeframe filters + interval display (#58)

New timeframe controls on the PW Performance Trends chart:

- **Timeframe dropdown**: Days (1/5/10/30/60/90/120), Months (1-12), Years (1-5), Games (10-500)
- **Date from/to pickers** for custom date ranges
- **Interval dropdown**: Daily (per game date), Weekly (ISO week), Monthly (calendar month), 1/5/15/30/60 min (game-minute buckets)
- **Cumulative / Per Interval toggle**: Cumulative (default) shows growing totals; Per Interval shows only that period's games independently
- Server-side `pw_trend.compute_trend_series()` generates per-game-date data points filtered by game date (not snapshot date), with `cumulative` flag
- **Source filter**: Live/Backfill/Other/All — server-side filtering by `source` field before computing trend series
- `/api/pw-trend` accepts `date_from`, `date_to`, `last_n_games`, `interval`, `cumulative`, `source` params
- Weekly labels show "2026-W21", monthly shows "2026-05"
- Mutual exclusion: timeframe clears date range and vice versa
- All persisted to localStorage, Clear button resets all
- **Minute intervals** (1/5/15/30/60 min): groups PW calls by game_minute bucket, showing accuracy/ROI by when during the game the call was made (e.g. "40-45'" = first 5 min of H2)
- **Removed** redundant Window dropdown (Cumulative/Last 30/60/90) — superseded by Timeframe games filter + Cumulative toggle

**Files changed:** `pw_trend.py`, `server.py`, `dashboard.html`

---

## 2026-05-21 — Fix Odds Monitor credit waste (#57)

Three fixes to prevent credit waste when no games are live:
- Skip outside NRL game window (UTC 06:00-12:00)
- Cache current round for 5 min instead of scanning every 30s
- Fix early-return: `if not active_games: return`

**Files changed:** `odds_monitor.py`

---

## 2026-05-21 — Odds Monitor: live odds + late-tries +EV analysis (#51)

**Issue:** #51

New **Analysis > Odds Monitor** sub-tab for monitoring live odds during the last 10 minutes of NRL games + extra time.

### New module: `odds_monitor.py`
- Background polling thread (30s interval) activates at game minute 70+ and ET
- Each snapshot captures: odds (h2h + spreads + totals), NRL score, try events, +EV analysis
- +EV calculation: compares bookmaker implied over/under probability against historical late-tries probability from game_history.json (515 games, 15 remaining-minute windows)
- In-memory snapshots per game, persisted to `odds_monitor_history.json` at game end (indefinite retention)

### `odds_api.py` changes
- New `fetch_odds_monitor_data()` with separate 25s cache (vs 2-min for monitor.py) and `h2h,spreads,totals` markets (3 credits/call)
- Existing caches unchanged — no impact on monitor.py polling

### Server endpoints
- `GET /api/odds-monitor` — live status, per-game snapshots, late-tries probability reference
- `GET /api/odds-monitor-history` — persisted historical data
- `POST /api/odds-monitor/start` — start background thread
- `POST /api/odds-monitor/stop` — stop background thread
- Auto-starts on server boot when `odds_api_enabled` + `odds_api_key` configured

### Dashboard (Analysis > Odds Monitor)
- **Game selector cards** with live score, period, total line, snapshot count
- **Chart.js line chart** — total line, total points, spread, moneyline over time with try annotations
- **EV summary cards** — remaining minutes, total line, implied over prob, historical 1+/2+/3+ tries prob, EV edge
- **Snapshot table** — 12-column reverse-chronological table of all captured data points
- **Historical view** — past games with first/last line, line movement, over/under hit
- **Late tries probability reference** — collapsible table (1-15 min windows)
- Start/Stop toggle button, auto-polls dashboard every 30s when running

**Files changed:** `odds_monitor.py` (new), `odds_api.py`, `server.py`, `dashboard.html`

---

## 2026-05-21 — Fix Custom Queries tab not loading (#50)

**Bug:** The Custom Queries sub-tab under Analysis showed "Loading..." forever. `loadCustomQueries()` called `buildAnalysisQuery('cq')` but this function was never defined, causing a JS `ReferenceError`.

**Fix:** Added `buildAnalysisQuery(prefix)` — a generic helper that reads `{prefix}-season`, `{prefix}-segment`, `{prefix}-tag`, `{prefix}-date-from`, `{prefix}-date-to`, `{prefix}-role` filter elements and builds the URL query string for `_filter_records()` on the server.

---

## 2026-05-21 — Live game cards: full stats, odds, conditions, PW badge (#54)

**Issue:** #54

Enhanced live game cards matching NBA Monitor: PW badge from state, pregame/live odds bar, 6-stat comparison grid (Poss%, Comp%, Errors, Missed Tackles, Run Metres, Line Breaks) with green highlighting, condition pills, 1H scores, period+clock display.

**Files changed:** `dashboard.html`

---

## 2026-05-21 — Fix PW tab: completed games showing as "live" + game header display

PW tab `source` was set to "live" based only on `live_stats.json` freshness (10min). Now also checks `is_live` on actual game in live_stats. Completed games show "recent" not "live".

**Files changed:** `server.py`

---

## 2026-05-21 — Fix BK spread: merge odds from multiple bookmakers

`extract_odds()` previously returned first bookmaker with any data, even if it lacked spreads. Now merges across bookmakers: moneyline from highest-priority BK (e.g. TAB), spread from next BK that has it (e.g. TABtouch). Ensures PW calls get both `bk_moneyline` and `bk_spread` when different bookmakers provide different markets.

**Files changed:** `odds_api.py`

---

## 2026-05-21 — BK Odds fallback + PW threshold config fix

- **BK Odds on PW calls:** Stored `last_live_odds_{match_id}` in state as fallback when current cycle doesn't poll odds (96fc928)
- **PW threshold conditions:** Restored 3 missing `predicted_winner_threshold` conditions (60%/75%/90%) to config.json — were dropped during dashboard save

---

## 2026-05-21 — Fix live game detection, stats, and dashboard display (#52)

**Issue:** #52

Three critical fixes for live game functionality:

### NRL API matchState changes
- `LIVE_STATES` expanded: added `FirstHalf`, `SecondHalf`, `ExtraTime` (API changed from `InProgress`)
- `matchMode=="Live"` fallback check added
- `_get_period()` maps new states directly: FirstHalf→1H, SecondHalf→2H, ExtraTime→ET (fixes "Pre" badge on live games)

### Stats extraction (new API format)
- NRL API changed stats structure from flat `team.stats` to nested `stats.groups[].stats[]` with `homeValue`/`awayValue` objects
- Added `_STAT_TITLE_MAP` (27 stat titles → camelCase keys) and `_stat_title_to_key()` helper
- Stats went from 4 fields (goals, tries) to 29 fields (possession, errors, tackles, etc.)
- Enables condition evaluation and PW predictions for live games

### Live stats dashboard data (#52)
- `is_live: True` added to live_stat dict (was missing)
- `conditions_fired`: accumulated hits from all_hits per game
- `pregame_odds`: always from state (not just when odds poll fires)
- `pw_calls`: always from state (not just when pw_analysis fires this cycle)

**Files changed:** `nrl_api.py`, `monitor.py`

---

## 2026-05-21 — Game History: filter persistence, source filter, detail enhancements (#47)

**Issue:** #47

- **Filter persistence:** 15 filter values saved to localStorage, restored on tab open
- **Source filter:** Live/Backfill/All dropdown with backend filtering
- **Detail enhancements:** Condition sort toggle (Time/A-Z), color-coded margins, data source label

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-21 — Monitor Status Overview: add missing status rows (#46)

**Issue:** #46

3 new status rows + 1 enhanced: ML model (Trained + STALE/CURRENT), Scheduler (UTC window + interval), Last backup (most recent snapshot). Game history now shows file size.

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-21 — Help modals across all dashboard tabs (#45)

**Issue:** #45

6 new contextual help modals with (?) buttons: ML Model Status, Condition Combinations, Game History Browser, Alert History, Upcoming Games, Post Game Summary. Total: 14 help modals across the dashboard (8 existing + 6 new).

**Files changed:** `dashboard.html`

---

## 2026-05-21 — NBA Parity Status Summary

### Completed features (this session)

| Feature | Issue | Status |
|---------|-------|--------|
| PW Trends (Chart.js, changelog, version cohorts) | #26 | Done |
| PW ROI Matrix (5-dim heatmap, pin controls, drill-down) | #29 | Done |
| PW ROI Combos (36 filter combos, 14-col table) | #30 | Done |
| Synthetic quarters (Q1-Q4/ET for analysis) | #31 | Done |
| Analysis/Outcomes (11-col conditions, compounds, combos, PW accuracy, pixel parity) | #32 | Done |
| Model Evaluation (Brier/LogLoss/ECE, 5 targets, retrain, snapshots) | #33 | Done |
| Configuration UI (add/edit/delete conditions + compounds) | #34 | Done |
| Config sub-tabs (Settings, Post-Game, PW Model, restructure) | #35-38 | Done |
| PW tab visibility fixes + NBA-style header result line | #39 | Done |
| Backfill PW accuracy safeguards (ML disabled, lookahead-safe, cumulative snapshots) | #40 | Done |
| Serve-time odds derivation from game record | #42 | Done |
| ML Training sub-tab (Train Now, staleness, history, log viewer) | #44 | Done |
| Logs tab (structured events, filter chips, raw mode, file selector, live tail) | — | Done |
| Outcomes pixel parity (pctBar, avgDiffCell, hover, arrows, localStorage filters) | — | Done |
| Config formatting (settings-grid, field-group, settings-note, save-bar, edge weights) | — | Done |
| Help modals across all tabs (14 total) | #45 | Done |
| ML Training retention fix + next scheduled + local/UTC timestamps | #49 | Done |

### Remaining items (lower priority polish)

| Feature | Issue | Priority |
|---------|-------|----------|
| Help modals across all tabs | #45 | Done |
| Monitor Status Overview extra rows | #46 | Done |
| Game History filter/detail enhancements | #47 | Done |
| Backfill 2024/2025 odds | #41 | Parked (credits) |
| Backfill PW Phase 2 (standalone CLI) | #43 | Low |

---

## 2026-05-21 — Monitor Status: ML Training sub-tab (#44)

**Issue:** #44

New Monitor Status sub-tab matching NBA Monitor's ML Training panel.

- **Backend:** `/api/ml-training-status` (staleness via SHA256, config, history), `/api/ml-train` POST (background `ml_model.py` subprocess, 300s timeout, log capture), `/api/ml-training-log/{filename}`
- **Dashboard:** Train Now/Refresh buttons, status row (last trained, STALE/CURRENT, auto ON/OFF), history table (7 cols: Date/Trigger/Result/Samples/**CV Accuracy**/Duration/Log), log viewer modal, 3s auto-polling during training
- **Infrastructure:** `ml_training_history.json` with retention pruning, training log files in `logs/`, SHA256 hash update on success

### Follow-up fixes (multiple commits)
- **POST endpoints fix:** `/api/ml-train` and `/api/model-eval-retrain` moved from `do_GET` to `do_POST` — BaseHTTPRequestHandler routes POST to `do_POST`, not `do_GET`
- **.venv setup:** Created `.venv` with `requests` + `scikit-learn` (matching NBA). All 3 systemd services updated to use `.venv/bin/python3` instead of `/usr/bin/python3` which lacks sklearn.
- **CV Accuracy column:** Added to history table. Parsed from ml_model.py output (both stdout and stderr since `_log()` writes to stderr).
- **Result detection fix:** `ml_model.py` exits 0 even on failure — result now checks exit code AND output content (CV accuracy present + no ERROR in output).

**Files changed:** `server.py`, `dashboard.html`, `.gitignore`, `systemd/*.service`

---

## 2026-05-21 — Fix Live Line ROI: fall back to pregame spread (#42)

`_did_cover_live_line` in `pw_matrix.py` and `pw_trend.py` only checked `live_spread` (live-polled odds), which is empty on backfilled calls. Now falls back to `spread` (pregame) for the synthetic live line calculation (`pregame_spread - margin_at_fire`). Also ensures `spread` field set on enriched calls in pw_matrix.

**Files changed:** `pw_matrix.py`, `pw_trend.py`

---

## 2026-05-21 — Fix ROI Matrix and Combos odds derivation (#42)

`pw_matrix.py` and `build_pw_roi_combos_payload` read moneyline/spread/BK fields directly from PW calls, which were empty after backfill regeneration. Now derive from parent game record for ML ROI, Spread ROI, BK ROI calculations and cover checks. Live Line ROI correctly remains None (requires live-polled odds during game).

**Files changed:** `server.py`, `pw_matrix.py`

---

## 2026-05-21 — Derive missing PW call odds fields from game record at serve-time (#42)

**Issue:** #42

After backfill regeneration (#40), PW calls had empty `spread_role`, `moneyline`, `spread`, `bk_moneyline`, `bk_spread`, `bk_name` because odds are attached to game records by `backfill_odds.py`, not embedded in PW calls at generation time.

All serve-time consumers now derive missing fields from the parent game record:
- `server.py` — `_enrich_pw_call()` (spread_role, moneyline, spread, bk_moneyline, bk_spread, bk_name) and `build_pw_roi_combos_payload()` (role, moneyline, spread)
- `pw_matrix.py` — `compute_pw_matrix()` (spread_role)
- `pw_trend.py` — `_collect_calls()` (spread_role, moneyline)

BK fields use pregame odds as fallback for backfilled calls (no live bookmaker data during backfill).

**Files changed:** `server.py`, `pw_matrix.py`, `pw_trend.py`

---

## 2026-05-21 — Cumulative condition snapshots for backfill PW calls (#40)

**Issue:** #40 (Phase 1 item 3)

At each eval minute, PBP conditions (score_diff, momentum_shift, points_run, halftime_turnaround) are now filtered by `game_seconds_at_fire <= eval_seconds`. Stats-based conditions (no fire time) are always included since they're cumulative by nature.

Previously all conditions were visible at every eval point — e.g. `halftime_turnaround` at minute 5.

### Impact

| Metric | Before | After |
|--------|--------|-------|
| Total calls | 1,029 | 574 |
| Games with calls | 228 | 192 |
| Accuracy | 98.1% | 93.0% |
| H1 calls | 113 | 17 |

Accuracy dropped 5.1pp — the most significant correction in the #40 series. H1 calls dropped dramatically because early eval points now have few PBP conditions available.

**Files changed:** `backfill.py`, `CLAUDE.md`

---

## 2026-05-21 — Lookahead-safe backfill PW calls (#40)

**Issue:** #40

Backfill PW call generation now prevents future data leakage:

1. **ML model disabled** — `ml_model = None` during backfill (matches NBA approach). Model trained on all 514 games would leak future outcomes into past predictions.
2. **Lookahead-safe prior** — historical condition win% computed from `prior_records` only (games with dates strictly before current game). Previously used `compute_outcomes()` which loaded all history.
3. **Edge store from priors** — `ConditionEdgeStore` already used `prior_records` (no change needed).

### Impact

| Metric | Before | After |
|--------|--------|-------|
| Total calls | 1,028 | 1,029 |
| Games with calls | 232 | 228 |
| Accuracy | 97.5% | 98.1% |
| 2024 R1-5 (early) | — | 95.1% |
| 2024 R20+ (late) | — | 98.0% |

Accuracy remains high because the remaining inflation comes from full-game conditions at every eval point (issue #40 Phase 1 item 3 — cumulative condition snapshots). With honest priors but full-game conditions, predictions are still trivially accurate.

**Files changed:** `backfill.py`, `CLAUDE.md`

---

## 2026-05-21 — Fix ML model feature mismatch (36 vs 35 features)

`first_team_scores` was added to `CONDITION_TYPES` after the model was last trained, causing `predict_winner()` to fail silently with `StandardScaler expecting 35 features`. All backfill PW calls were falling back to `basis: "historical"` with ML effectively disabled.

- Retrained `model_nrl.pkl` with current 36-feature schema (27 conditions + 9 context)
- 98.9% CV accuracy, 1,024 samples from 514 games
- `model_nrl.pkl` and `model_meta_nrl.json` are git-ignored runtime artifacts

**Files changed:** `model_nrl.pkl`, `model_meta_nrl.json` (runtime, git-ignored), `CHANGES.md`

---

## 2026-05-21 — PW: synthetic quarters for analysis (Q1/Q2/Q3/Q4/ET) (#31)

**Issue:** #31

Split 40-min halves into 20-min synthetic quarters for PW analysis. Doesn't affect PW eval cadence.

- **Quarter mapping:** Q1 (0-20'), Q2 (20-40'), Q3 (40-60'), Q4 (60-80'), ET (80'+)
- **`quarter` field** derived from `game_minute` at serve-time (`_enrich_pw_call`); persisted on new live PW calls
- **PW tab:** Quarter filter dropdown + "First Per Quarter" call selection + game cards grouped by quarter
- **PW Trends:** Quarter filter + `first_per_quarter` call selection + cross-dimensional filter breakdowns with `q:Q1`-style keys
- **ROI Matrix:** Quarter as new dimension — by_quarter breakdown + 3 new matrix pairs (threshold×quarter, consensus×quarter, quarter×spread_role)
- No backfill needed — quarter derived from existing `game_minute` on every API request
- **ROI Matrix:** Quarter in pin filter controls for slicing matrix by quarter
- **PW ROI Combos:** Quarter-based combos (Q1-Q4, ET) alongside half-based combos, `first_per_quarter` selection, `Half/Qtr` column in table

**Files changed:** `server.py`, `monitor.py`, `dashboard.html`, `pw_trend.py`, `pw_matrix.py`, `CLAUDE.md`

---

## 2026-05-21 — PW tab: fix visibility, NBA-style header result line (#39)

**Issue:** #39

- **Link visibility:** All `var(--accent)` (#0f3460, invisible on dark bg) replaced with `#4fc3f7` (bright blue) — game header links (Live/History/NRL.com), BK Odds cell, half section headers, condition expand links
- **PW header:** "PW:" in `var(--muted)` + team name in `var(--green)` bold (was all invisible accent)
- **Second header line:** Full NBA-style result summary visible when card collapsed — Winner by margin, PW Won YES/NO, Final score, Odds, Spread → Live → BK (bright blue), covered/missed by X, FAV/DOG
- **Duplicate footer removed:** Result div below summary was duplicating the header line

**Files changed:** `dashboard.html`

---

## 2026-05-20 — Config tab: 5 sub-tabs with Settings, Post-Game, PW Model (#35 #36 #37 #38)

Restructured Configuration from a flat panel into 5 sub-tabs matching the NBA Monitor layout with pixel-level formatting parity.

### NBA-style formatting (multiple commits)
- **CSS classes:** `.settings-title` (uppercase, 0.72rem, muted, letter-spacing), `.settings-group-label` (green sub-headers), `.settings-grid` (CSS grid auto-fit minmax 180px), `.field-group` (flex column, uppercase labels 0.7rem), `.settings-note` (0.75rem help text), `.add-cond-btn` (full-width dashed border), `.save-bar` with `.btn-primary` + `.btn-reset` per tab
- **Input styling:** border-radius 6px, focus border-color changes to green
- **Help text:** `.settings-note` after every section group with contextual explanations

### Conditions & Compounds tabs
- Existing editable editors from #34, restyled with full-width dashed Add buttons and per-tab Save/Reset bars

### Settings tab (NEW)
- **General:** Teams (sub-group), Dashboard bg colour, Alerting (cooldown, eog, condition probability), Model Eval snapshots, ML Training (auto-train, interval, schedule, retention), Upcoming Games (lookahead)
- **Scheduler:** Start/Stop hours (UTC), Interval — with NRL game window note
- **Log Rotation:** Enabled, Period, Retention, Rotation hour, Log files textarea — with path format note
- **Runtime Backups:** Take Snapshot button, snapshot list

### Post-Game tab (NEW)
- Summary/alert history windows in `.settings-grid` layout
- Dynamic tag manager with full-width dashed Add button, Up/Down reorder, built-in tag protection, help notes

### PW Model tab (NEW)
- **Alert settings, Adaptive thresholds, Confidence modifiers, Half weights, Suppression gates, Eval cadence, ML blend, Bayesian priors** — all in `.settings-group` sections with `.settings-group-label` headers and `.settings-note` help text
- **Condition Edge Weights table (NEW):** Sortable 5-column table (Condition, Fires, Won, Win%, Edge) with click-to-sort headers, green/red edge coloring. Auto-loads when PW Model tab opens. Backend `/api/condition-edges` endpoint.
- ML blend auto-computes historical weight on input

### Save integration
- Per-tab `.save-bar` with Save + Reset buttons (replacing single bottom button)
- `saveConfig()` collects 50+ fields; `loadConfig()` populates all including nested objects

**Files changed:** `dashboard.html`, `server.py`

---

## 2026-05-20 — Logs tab: NBA parity — structured events, filters, raw mode (multiple commits)

Rewrote the Monitor Status Logs sub-tab to match the NBA Monitor's log viewer.

### Backend
- **Structured event parsing:** Log lines parsed into `{ts, tag, message}` with tag classification (ERROR/WARN/ALERT/PW/ODDS/HISTORY/BACKUP/INFO). Fetch limit increased to 2000 (default 500), reads last 128KB.
- **`/api/log-tail` endpoint (NEW):** Returns raw file content with `file_size`, `byte_count`, `total_lines`, `truncated` metadata. Supports `monitor.log` and `dashboard.log` via file selector.

### Dashboard: Formatted view
- **Card-based entries** with timestamp + colored tag pill (8 tag types with distinct colors matching NBA)
- **Tag filter chips:** 8 toggleable chips (Alert, PW, Odds, History, Backup, Warn, Error, Info) with green active state, client-side filtering
- **Sort order:** Newest/Oldest first
- **Date range filters:** From/To date inputs
- **Per-page selector:** 25/50/100/200
- **Pagination:** First/Prev/Page X of Y/Next/Last
- **Reset button:** Restores all filters to defaults

### Dashboard: Raw mode
- **File selector:** Switch between `monitor.log` and `dashboard.log`
- **Tail size selector:** 8KB / 16KB / 32KB / 64KB / 128KB / 256KB
- **Search:** Filters lines client-side with match count display
- **Live tail:** 3-second auto-refresh toggle with ⚡ badge
- **Reload button:** Force refresh from server
- **Paginated raw output** with line/byte count and truncation info

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-20 — Outcomes tab: NBA pixel-parity polish (multiple commits)

Series of fixes to match the NBA Monitor Analysis tab layout pixel-for-pixel:

- **Tag Analytics:** Replaced flex bars with NBA-style 4-column table (Tag, Games, Eligible Games, Rate) with thin 8px accent-colored progress bar
- **PW Accuracy summary card:** NBA-style large-font metric cards (1.4-1.6rem) — Overall Accuracy, Calls correct, Games tracked, Games won, Games won %, Flat ROI with P&L, ML ROI with P&L, Spread ROI with cover count, BK Spread ROI
- **`pctBar()` helper:** 120px-wide horizontal bars (6px tall) with colored fills in all condition/combo/compound table cells — replaces plain text percentages
- **`avgDiffCell()` helper:** Color-coded +/- margin values (green positive, red negative)
- **Row hover highlight:** Background toggle on mouseenter/mouseleave for all condition rows
- **Drilldown arrow ↗:** Bright blue (#4fc3f7) dotted underline on condition names with arrow icon
- **Compound badge:** `rgba(59,130,246,0.15)` background matching NBA exactly
- **Combo pills:** `rgba(34,197,94,0.15)` green rounded pills matching NBA
- **Column headers:** "Team Wins", "+Δ%", "−Δ%", "Avg Δ", "Covered%" matching NBA naming
- **Role filter:** Added to Outcomes filter bar (Favorites/Underdogs)
- **Filter persistence:** All filter values saved/restored to localStorage across sessions
- **Model Evaluation auto-load:** Loads automatically when Outcomes tab opens
- **Horizontal scroll:** All table containers have `overflow-x:auto` to prevent window overflow
- **Upset% column:** Added to conditions, compounds, and combinations tables (computed from odds)
- **Backend `_build_pw_accuracy_summary()`:** Aggregates top-level ROI from threshold bands for the summary card

---

## 2026-05-20 — Configuration UI: add/edit/delete conditions and compound conditions (#34)

**Issue:** #34

Replace read-only condition display with fully editable forms in the Configuration tab.

- **Conditions editor:** Inline inputs for name, type (28 types), color, alert, alert_once, threshold, type-specific fields (score_diff period/status/deficit, PW threshold min_half), description. Add/Remove buttons per condition.
- **Compounds editor:** Editable name, operator (AND/OR), description, checkbox condition_refs selector from base conditions. Add/Remove buttons.
- **saveConfig():** Collects all fields from DOM via `_collectConditionsFromDOM()` / `_collectCompoundsFromDOM()`, includes in POST payload. Server validates unique names, valid types, referential integrity.

**Files changed:** `dashboard.html`

---

## 2026-05-20 — Model Evaluation: Brier/LogLoss/ECE metrics, retrain, snapshot history (#33)

**Issue:** #33

New `evaluate_model.py` module with chronological model evaluation matching the NBA Monitor.

### Backend
- **4 targets:** team_wins, game_close, game_blowout, game_comeback
- **Rolling-origin evaluation:** Train before day D, predict day D, step_days=7
- **Frequency vs Bayesian** predictors with NRL-calibrated Beta priors
- **3 metrics:** Brier score, Log Loss, ECE per predictor per target
- **Snapshot history:** `model_eval_history_nrl.json` with compact metrics (max 100)
- **CLI:** `python3 evaluate_model.py --target team_wins --step-days 7`

### API endpoints
- `GET /api/model-eval` — compute evaluation (optional `?persist=1&force=1`)
- `POST /api/model-eval-retrain` — force recompute + save snapshot
- `GET /api/model-eval-history` — snapshot history

### Dashboard
- Model Evaluation section in Outcomes tab with 9-column table (Target, Eval n, Mode, Freq/Bayes Brier/LogLoss/ECE)
- Refresh / Save snapshot / Retrain buttons with status display
- Snapshot history table (last 8 entries)
- Help modal explaining metrics, evaluation methods, targets

**Files changed:** `evaluate_model.py` (new), `server.py`, `dashboard.html`, `.gitignore`

---

## 2026-05-20 — Analysis/Outcomes: NBA parity — conditions, compounds, combos, PW accuracy (#32)

**Issue:** #32

Rewrote the Analysis/Outcomes sub-tab to match the NBA Monitor's Analysis tab layout.

### Backend: expanded `/api/analysis`
- **Enhanced condition stats (11 metrics):** team_wins%, game_upset% (from odds), game_close%, game_blowout%, game_comeback%, positive/negative margin%, avg margin, spread_covered%, tag_rates
- **Compound Outcomes:** Same schema for AND/OR compound conditions
- **Condition Combinations (1,771):** 2-5 way co-fires per team per game with win/tag rates
- **PW Accuracy breakdown:** by confidence band (50-90%) and by consensus, each with full ROI metrics (flat, ML, spread covered, spread ROI)
- New params: `min_sample`, `max_combo_size`, `margin_fired`, `search`

### Dashboard: rewritten Outcomes sub-tab
- **11-column Condition Outcomes table** (incl. Upset%) with drill-down to Game History
- **Compound Outcomes section** (same table format)
- **Condition Combinations section** (top 100, condition pills)
- **PW Accuracy section:** Summary card + by-band table (9 cols) + by-consensus table (9 cols)
- **Enhanced filter bar:** Min sample, Max combo (2-5), 7 sort options, Margin when fired, Search
- **Help modals** for conditions and PW accuracy
- Client-side sort/search from cached data

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-20 — PW ROI Combos: NBA parity with filter combos and full ROI metrics (#30)

**Issue:** #30

Rewrote PW ROI Combos to match the NBA Monitor's filter-combination approach and moved it into the ROI Matrix panel.

### Backend rewrite
- Replaced condition-name grouping with **filter-combination approach**: role (2) x margin (2) x call_selection (3) x half (3) = **36 combos**
- Full ROI metrics per combo: accuracy, games_won, flat_roi, ml_roi, spread_covered, spread_roi, live_line_roi, bk_line_roi
- Returns `total_computed` and `elapsed_ms` for UI feedback

### Dashboard changes
- **Removed** standalone PW ROI Combos sub-tab from Analysis
- **Moved** into ROI Matrix panel (below heatmap), matching NBA layout
- **14-column table** with all ROI metrics (up from 6 columns)
- **7 sort options** with client-side caching for instant re-sort
- **Load + Top ROI** buttons with elapsed timer, Show top 5/10/20
- **Row click drill-down** to Game History with matching filters
- Inherits season/segment from ROI Matrix controls

**Files changed:** `server.py`, `dashboard.html`

---

## 2026-05-20 — PW ROI Matrix sub-tab with cross-dimensional heatmap (#29)

**Issue:** #29

New Analysis sub-tab with a 4-dimension ROI matrix matching the NBA Monitor layout.

### New backend: `pw_matrix.py`
- **4 dimensions:** Confidence Band (50-90%), Consensus Type, Half (H1/H2/ET), Spread Role (Favorite/Underdog)
- **6 cross-dimensional pairs** pre-computed (threshold x consensus, threshold x half, etc.)
- Per-cell metrics: Games Won%, Flat ROI, ML ROI, Spread ROI, live line, BK line, calls/games
- Decimal odds ROI calculations (NRL format)
- Pin filter support for 4-way cross-tabs

### New API: `/api/pw-matrix`
- Query params: `season`, `segment`, `matrix_pin_{threshold,consensus,half,spread_role}`
- Dedicated endpoint (not piggybacked on `/api/outcomes` like NBA)

### Dashboard: ROI Matrix sub-tab
- Heatmap table with 5-line cells and green/red background tinting based on Flat ROI
- Row/column dimension dropdowns (any 2 of 4)
- Pin controls for remaining 2 dimensions (default: spread_role=underdog)
- Season and segment filters (independent from Analysis tab)
- Drill-down: click any cell to navigate to Game History with matching PW filters
- Help modal with full documentation
- 300ms debounce on filter changes

**Files changed:** `pw_matrix.py` (new), `dashboard.html`, `server.py`

---

## 2026-05-20 — Fix PW condition edge and implement edge gate suppression (#28)

**Issue:** #28

- `_enrich_pw_call()` computed `condition_edge` as a global win% without considering which team the condition fired for. Opponent conditions now have their edge sign flipped, and `avg_edge` is computed from the corrected per-condition edges at serve-time (works for both live and backfilled calls).
- Implemented edge gate suppression matching NBA Monitor: `pw_edge_gate` config key (default 0) suppresses PW calls where avg condition edge < threshold. Applied in both live monitor (`_check_pw_suppression`) and backfill (`_generate_pw_calls_for_record`).

**Files changed:** `server.py`, `monitor.py`, `backfill.py`, `CLAUDE.md`

---

## 2026-05-20 — Remove spurious minute-0 PW calls from backfill (#27)

**Issue:** #27

Backfill included minute 0 as a PW evaluation anchor point, producing 26 bogus "H1 0'" calls with 0-0 scores but full-game conditions — visible when filtering H1 but buried in All Halves view.

- Removed `0` from `eval_minutes` in `backfill.py` — first eval is now the first actual scoring event or halftime (minute 40)
- Skip PW eval when score is still 0-0 — truncated minute from `gameSeconds // 60` fires before the scoring event within that minute
- Purged 42 spurious PW calls from `game_history.json` (1,070 → 1,028 total; H1: 142 → 100)

**Files changed:** `backfill.py`, `game_history.json`

---

## 2026-05-20 — PW Trends sub-tab with Chart.js charts, changelog, and version cohorts (#26)

**Issue:** #26

Replaced the simple PW Metrics table under Analysis with a full PW Trends tab matching the NBA Monitor layout.

### New backend: `pw_trend.py`
- Computes snapshots with cumulative + rolling window (30/60/90) metrics
- Cross-dimensional filter breakdowns (role x half x call_sel x margin) pre-computed per snapshot
- Per-version cohort metrics from changelog-based version resolution
- Decimal odds ROI calculations (NRL uses decimal format, not American moneyline)
- 5 NRL changelog entries seeded: v1-pw-launch through v5-scoring-moments
- CLI: `python3 pw_trend.py snapshot|bootstrap|show`
- Snapshots auto-appended during scheduled runtime backups

### Dashboard: PW Trends sub-tab
- **Chart.js v4** added via CDN for canvas-based trend charts
- **4 chart modes:** Performance (Accuracy + ROI), Unit P&L, Rolling Accuracy (5-snap window), Bankroll ($10k sim)
- **6 filters:** Role, Half (H1/H2/ET), Calls (First/Last/1st/Half), Margin, Chart Type, Window (Cumulative/30/60/90)
- **Summary metrics line:** Calls, Correct, Accuracy, Flat ROI, ML ROI, Live Spread ROI, BK Spread ROI, Avg Edge
- **PW Logic Changelog table:** 13 columns with GitHub issue links and metrics at each changelog entry
- **Version Cohort Comparison table:** 12 columns grouping calls by algorithm version
- **Help modal** with full documentation of all features
- Filter state + legend visibility persisted to localStorage
- Changelog vertical line annotations on chart (when Chart.js annotation plugin available)

### Integration changes
- `server.py`: `/api/pw-trend` delegates to `pw_trend.load_trend_data()`
- `runtime_backup.py`: Appends PW trend snapshot + backs up `pw_trend_nrl.json`
- `monitor.py`, `backfill.py`, `outcomes.py`: Version resolution delegates to `pw_trend.resolve_pw_version()` (changelog-based)

### Files changed
`pw_trend.py` (new), `dashboard.html`, `server.py`, `runtime_backup.py`, `monitor.py`, `backfill.py`, `outcomes.py`, `.gitignore`

---

## 2026-05-20 — PW backfill: evaluate at actual scoring moments from PBP timeline (#25)

**Issue:** #25

Backfill PW eval points now derived from `score_progression` timestamps instead of fixed 10-minute intervals.

- Eval points built from PBP scoring events (try, penalty goal, field goal) + fixed anchors (minute 0, 40)
- Falls back to 10-minute intervals when no `score_progression` data
- 5-minute cooldown between evals still applies
- Minute 80 only included if game went to ET (scores tied)

### Results

| Metric | Before (fixed) | After (scoring events) |
|--------|---------------|----------------------|
| Games with PW calls | 223 | 232 |
| Total PW calls | 795 | 1,070 |
| Unique eval minutes | 8 | 79 |
| ET calls | 0 | 10 (legit) |
| H1 / H2 | 130 / 665 | 142 / 918 |

---

## 2026-05-20 — PW eval: trigger immediate evaluation on scoring events (#24)

**Issue:** #24

When a try or penalty goal changes the score between monitor polls, PW evaluation now fires immediately — bypassing the normal cadence cooldown (H1: 10min, H2: 5min, late: 2min).

- Tracks `last_score_{game_id}` in `.alert_state.json` to detect score changes between polls
- `force_pw` flag bypasses `_should_evaluate_pw()` cooldown; suppression gates and threshold checks still apply
- Config: `pw_eval_on_score_change` (default `true`)
- Logged as `"PW eval forced by score change"` for visibility

---

## 2026-05-20 — PW tab: fix Score column wrapping and NRL.com game link (#23)

**Issue:** #23

- **Score column** — added `white-space:nowrap` to prevent score text wrapping to a second line
- **NRL.com link** — uses `game_id` path directly (e.g. `/draw/nrl-premiership/2026/round-11/panthers-v-dragons/`) instead of constructing a broken URL from the game date

---

## 2026-05-20 — Odds API status line in Status Overview (#22)

**Issue:** #22

Added an Odds API credit status line to the Monitor Status → Overview panel, matching the NBA Monitor format.

- **`dashboard.html`** — New "Odds API" row fetches from `/api/odds-api-log` client-side. Displays: OK/Low/Critical label (≤20 Critical, ≤50 Low), credits remaining, today's credits used and call count (excludes cache hits). Grey "Disabled" when odds API is off.
- **`CLAUDE.md`** — Updated dashboard structure and API surface to reference Odds API credits

---

## 2026-05-20 — PW tab: match NBA Monitor stats parity (#19)

**Issue:** #19

### Changes

**Accuracy stats block** — 7 new ROI/spread pills added to match NBA layout:
- Flat ROI, ML ROI, Spread Covered (count + %), Spread ROI, Live Spread ROI, BK Odds ROI, BK Spread ROI
- Pills hidden when underlying data is null; color-coded green/red for ROI, green/yellow/red for spread %

**Game cards** — NBA-style header and footer:
- NRL.com match centre link, 🏉 Live nav link (→ Live Games tab), 📋 History nav link (→ Game History detail)
- Right-aligned PW header showing latest predicted team + win%
- Live game status (period + clock) in accent color; live game cards auto-open
- Result footer: odds, spread covered/missed by N, FAV/DOG badge

**Call rows** — 6 new columns (15 total, matching NBA):
- Time (HH:MM wall clock), Odds (pregame moneyline), Handicap (pregame spread), Live Spread (synthetic: pregame spread − margin), BK Odds (tooltip: bookmaker name), BK Spread (tooltip: bookmaker name)
- Active conditions in expandable details now color-coded by edge (green ≥10, light green ≥5, orange >-5, red)

**Backend — pregame odds capture** (`monitor.py`):
- First odds poll for each game saved as `pregame_odds_{game_id}` in state
- PW calls store: `moneyline`, `spread` (pregame), `bk_moneyline`, `bk_spread`, `bk_name` (bookmaker at fire time)

**Backend — ROI metrics + live calls** (`server.py`):
- `/api/pw-calls` computes 7 ROI metrics in `predicted_winner_accuracy`
- Calls enriched with `live_line`, `bk_live_line`, `condition_edge` per active condition
- Live PW calls merged from `.alert_state.json` with `source: "live"/"recent"`

**Backfill** (`backfill.py`):
- PW call generator now populates `moneyline`, `spread`, `bk_moneyline`, `bk_spread`, `bk_name`, `spread_role` from `backfill_odds.py` enrichment data
- 3-step backfill procedure (zero API credits): rebuild from cache → attach odds → regenerate PW calls in-place

### Backfill results

| Metric | Value |
|--------|-------|
| Total games | 514 |
| Games with odds (from `odds_cache/`) | 379 (73.7%) |
| Games with PW calls | 299 |
| Total PW calls | 1,283 |
| PW calls with moneyline/spread | 966 (75.3%) |

ROI metrics after backfill: Flat ROI +86.3%, ML ROI +101.3%, Spread Covered 92.3%, Spread ROI +76.3%, Live Spread ROI +24.5%, BK Odds ROI +105.8%, BK Spread ROI +76.3%. High values expected for synthetic backfilled calls (generated with hindsight).

135 games (mostly 2024) lack cached odds — run `backfill_odds.py` without `--use-cache` for 2024 dates to improve coverage (uses API credits).

### Server restart required

Dashboard server must be restarted after code changes to serve updated `server.py` and `dashboard.html`. After `systemctl --user restart nrl-dashboard.service`, all 2026 calls show Live Spread and BK Spread data. 2024/2025 calls show `—` due to missing `odds_cache/` data (see #20).

### Known limitation (#20)

Backfilled PW calls set `bk_moneyline`/`bk_spread` to the same pregame values as `moneyline`/`spread` (no live bookmaker data during backfill). BK ROI metrics are identical to Spread ROI for backfilled data. Live-captured calls via `monitor.py` will have real bookmaker data at fire time.

### Files modified

- `monitor.py` — pregame odds capture, 5 new fields in `_record_pw_call()`
- `server.py` — ROI helpers, call enrichment, live PW call merging, `build_pw_calls_payload()` rewrite
- `dashboard.html` — accuracy block, game cards, call table (15 columns)
- `backfill.py` — PW call generator with odds fields
- `CLAUDE.md` — updated PW docs

---

## 2026-05-20 — Fix PW backfill: minute 80 incorrectly classified as ET (#21)

**Issue:** #21

### Bug

Every backfilled game (299/299) had a spurious PW call with `half: "ET"` at `game_minute: 80`. Games with margins of 12, 18, 25 points were showing ET calls — clearly not extra time.

### Root cause

`_generate_pw_calls_for_record()` used `minute < 80` for H2 classification, so minute 80 fell into the else/ET branch. Minute 80 is end of regular time (fulltime), not extra time.

### Fix

- Changed half boundaries: `<= 40` for H1, `<= 80` for H2 (was `< 40`, `< 80`)
- Skip minute 80 evaluation entirely unless game actually went to extra time (scores tied at FT)

### Results

| Metric | Before | After |
|--------|--------|-------|
| Games with PW calls | 299 | 223 |
| Total PW calls | 1,283 | 795 |
| ET calls | 299 (spurious) | 0 |
| H1 calls | — | 130 |
| H2 calls | — | 665 |

---

## 2026-05-20 — Fix backfill condition margins to use fire-time scores (#18)

**Issue:** #18
**Commits:** cf416da, 57c7593

### Problem

All conditions in Game Detail showed the same end-of-game margin. Every condition for the winning team had +N and every condition for the losing team had -N, regardless of when during the game each condition would have fired.

### Fix

`evaluate_conditions_for_game()` now estimates the game_seconds when each condition first fired and looks up the actual score at that moment from `score_progression`.

**PBP conditions with fire time estimation:**

| Condition Type | Fire Time Source |
|----------------|-----------------|
| `score_diff` (Blowout 20+, Trailing 18+) | Scans score_progression for when margin threshold first crossed |
| `momentum_shift`, `points_run` | Last try event timestamp for the team |
| `try_scoring_run` | Last try in the consecutive run |
| `first_team_scores` | First scoring event in the game |
| `halftime_turnaround` | End of game (2H condition) |

**Stats-based conditions** (errors, completion rate, missed tackles, run metres, etc.) — continue showing final margin since these are cumulative stats with no single fire point.

### Results

- 1,900 / 8,299 conditions (23%) now have `game_seconds_at_fire` with time-specific margins
- All 514 games have varied condition margins (was: all identical per team)

### Example — Panthers 28-6 Dragons (final margin 22)

| Condition | Before | After |
|-----------|--------|-------|
| Blowout margin 20+ | +22 | **+20 @ 79min** |
| Trailing by 18+ in 2H | -22 | **-18 @ 48min** |
| 12+ unanswered points | +22 | **+20 @ 79min** |
| High error count (8+) | +22 | +22 (cumulative — correct) |

---

## 2026-05-20 — Fix PW backfill scores to use exact PBP data (#15, #17)

**Issues:** #15, #17
**Commits:** 3d06d5f, c0f1b1c

### Problem

PW backfill scores-at-fire were estimated via linear interpolation (`home_score * minute/80`), producing impossible NRL scores (e.g., 14-3 instead of actual 12-0 at halftime).

### Fix

- `score_progression` from `timeline_features.extract_features()` now stored in `game_history.json` during backfill — exact home/away scores at every scoring event from NRL PBP timeline
- `_generate_pw_calls_for_record()` uses `score_progression` for exact scores at any game minute
- Fallback chain: score_progression → halftime-anchored interpolation → linear estimate
- Final minute (80') always uses actual final scores

### Example — Panthers 28-6 Dragons

| Minute | Before (wrong) | After (from PBP) |
|--------|---------------|-------------------|
| 40' (HT) | 14-3 | **12-0** |
| 50' | 18-4 | **18-0** |
| 60' | 21-4 | **18-4** |
| 70' | 24-5 | **22-6** |
| 80' | 28-6 | **28-6** |

Also deduplicated 88 duplicate records from re-backfill.

---

## 2026-05-20 — Redesign Predicted Winners tab with NBA Monitor layout (#17)

**Issue:** #17
**Commit:** 6e5bce8

### API restructure

`/api/pw-calls` now returns games-grouped response instead of flat call list:
- `games[]` — each game with `game_id`, `game_name`, `game_start`, teams, scores, `winner`, `final_margin`, `pw_correct`, `pw_role`, `pw_spread`, `pw_covered`, `pw_cover_margin`, and `calls[]` array
- `predicted_winner_accuracy` — stats block with `accuracy_pct`, `correct/total_calls`, `total_games`, `games_correct`, `games_won_pct`, leading/trailing splits with per-split accuracy
- `available_seasons` — dynamic season list for filter dropdown

### Dashboard — NBA Monitor-style game cards

**Accuracy stats block:**
- Bordered card at top with colour-coded metrics: Accuracy%, Calls (correct/total), Games, Won (count + %), Leading accuracy, Trailing accuracy
- Green ≥65%, Yellow ≥50%, Red <50%

**Collapsible game cards (`<details class="pw-game-card">`):**
- Summary header: date/time (AEST), team icons + game name, call count badge, LIVE badge for active games
- Result footer (completed games): Winner by margin, PW Won (YES/NO), Final score, Spread + coverage (covered/missed), FAV/DOG badge
- Live game cards open by default; historical collapsed (toggleable)

**Per-half call tables (H1/H2/ET):**
- Collapsible `<details>` per half with call count
- Table columns: Game Time, Predicted Team (with icon), Win%, Score, Margin (colour-coded), Role (FAV/DOG), Polarity (net with +/− breakdown), Edge (colour-coded), Details (consensus/blended/basis badges + expandable conditions list)
- Suppressed rows at 45% opacity with red reason badge

**Pagination:**
- First/Prev/Next/Last buttons above and below game cards
- Page size selector: 5/10/20/50 per page (persisted to localStorage)

**Filter bar:**
- Filters: Segment (Regular Season/Finals), Season (dynamic), Role (Favourite/Underdog), Margin (Trailing/Leading), Half (H1/H2/ET), Call Selection (All/First/First Per Half/Last)
- "Set as default" checkbox persists all filter values to localStorage
- Show/Hide Suppressed toggle button
- Collapse All / Expand All toggle button
- Refresh button

### Files changed

| File | Lines | Summary |
|------|-------|---------|
| `server.py` | +120/−36 | Games-grouped `/api/pw-calls` with accuracy stats, spread coverage, season list |
| `dashboard.html` | +333/−48 | Full NBA-style PW tab: game cards, per-half tables, pagination, filter persistence |

---

## 2026-05-19 — Per-phase PW evaluation cadence (#15)

**Issue:** #15

PW evaluation interval is now configurable per game phase instead of a single `pw_cooldown_minutes`:

| Game Phase | Default Interval | Config Key |
|------------|-----------------|------------|
| H1 (0'–40') | Every 10 min | `pw_eval_interval_h1` |
| H2 early (40'–60') | Every 5 min | `pw_eval_interval_h2` |
| H2 late (60'+) & ET | Every 2 min | `pw_eval_interval_late` |

Late threshold configurable via `pw_eval_late_threshold_min` (default 60).

Replaces the single `pw_cooldown_minutes` approach. More frequent evaluation in critical late-game phases where predictions are most valuable.

---

## 2026-05-19 — Bayesian blending, PW Game History filters, version tracking — Phases 2-6 (#15)

**Issue:** #15
**Phases:** 2-6

### Phase 2: Bayesian blending engine (`outcomes.py`)

- `live_analysis_for_game()` — full prediction pipeline with frequency lookup + Bayesian posterior
- Beta distribution priors: `team_wins` (13/7), `game_upset` (7/13), `game_close` (8/12), `game_comeback` (3/7)
- Sample-size-adaptive blending: 70% Bayes / 30% frequency when <15 samples, scaling to 20%/80% at 50+ samples
- `ConditionEdgeStore` — tracks per-condition team-win rate (edge = win% - 50%)
- Polarity classification: 12 positive patterns (try_scoring_run, momentum_shift, etc.), 8 negative (error_rate, sin_bin, etc.)
- `monitor.py` upgraded to use Bayesian predictions when available, with fallback to simple win% average

### Game History PW filters (Step 11)

- 4 new filter dropdowns: PW Band (50-90%), PW Consensus (Strong/Conflicted/ML Only/Hist Only), PW Half (H1/H2/ET), PW Spread Role (Favorite/Underdog)
- Server-side filtering in `/api/history` — matches if ANY pw call in the game matches all active PW filters
- PW column in history table showing call count + accuracy %

### Game Detail PW section (Step 12)

- "Predicted Winner Calls" section with per-call table: Minute, Half, Predicted Team, Confidence%, Consensus, Margin, Role, Conditions count, Result
- Summary line: X calls, Y correct, Z% accuracy

### PW version tracking (Step 16)

- `PW_VERSIONS` list in `outcomes.py` mapping date ranges to version strings
- `resolve_pw_version()` used by monitor.py (live) and backfill.py (historical)
- Enables `/api/pw-trend` grouping by algorithm version

### PW Slack alert formatting (Step 17)

Enhanced PW alert messages:
```
🎯 NRL Predicted Winner: Melbourne Storm (72.5%)
📊 Consensus: Strong | Basis: combo (3 conditions)
🏉 Storm 18 - 12 Roosters (H2 55')
📈 Conditions: Momentum Shift 10+, Completion Rate >80%, Try Scoring Run 2+
⚡ Spread: Storm -5.5 (Favourite)
```

### Backup/recovery review (Step 18)

- Snapshot `metadata.json` enriched with `pw_call_count`, `pw_enabled`, `pw_version`
- Existing RUNTIME_FILES set already covers all PW state (`.alert_state.json`, `game_history.json`)
- WAL captures PW fields naturally (full game record including `predicted_winner_calls[]`)

---

## 2026-05-19 — Predicted Winner foundation + first_team_scores condition (#15)

**Issue:** #15
**Phase:** 1 — Foundation (Steps 1-14)

### New condition: `first_team_scores` (27th condition type)

- Fires once per game for the team that scores first (try, penalty goal, field goal)
- Detects from PBP score progression, try_events, or live score fallback
- Uses game state to ensure single-fire per game
- Added to `CONDITION_EVALUATORS` dispatch table and `config.example.json`
- Added to ML model `CONDITION_TYPES` for future training inclusion

### New condition: `predicted_winner_threshold` (28th condition type)

- Fires when blended PW confidence crosses a configurable threshold (60%, 75%, 90%)
- Uses `min_half` (1 or 2) instead of NBA's `min_quarter` — NRL 2x40-min halves
- Reads from `context["live_analysis"]` populated by PW evaluation pass
- Three example conditions in `config.example.json`: 60% (H1+), 75% (H2+), 90% (H2+)

### Predicted Winner evaluation in `monitor.py`

- **Two-pass condition evaluation:** base conditions first, then PW threshold conditions
- **ML + historical blending:** `predict_winner()` from `ml_model.py` blended with per-condition win% from `outcomes.py` (default 70/30 ML/historical)
- **Half weights:** H1 x0.90, H2 x1.05, ET x1.10
- **Margin boost:** leading team gets configurable boost (1.05 in H2 60'+)
- **Suppression gates:** margin gate -12 general / -8 late (H2 60'+), polarity gate, edge gate
- **PW call recording:** stored in `state["pw_calls_{game_id}"]` with full metadata (team, pct, consensus, half, minute, margin, spread role, conditions)
- **PW alerts:** formatted Slack messages with confidence %, consensus, half, score

### Adaptive live odds polling

- H1 (0'-40'): every 5 minutes (configurable `odds_poll_interval_h1`)
- H2 early (40'-70'): every 5 minutes
- H2 last 10 min (70'+): every 2 minutes (configurable `odds_poll_interval_late`)
- Extra Time: every 2 minutes
- Per-game `last_odds_poll_ts` tracking in state
- Live odds attached to `live_stats.json` output

### Per-team "First Team to Score" table (Custom Queries)

- Expanded existing first scorer summary to per-team breakdown
- Columns: Team, Games, 1H Win%, Game Win%, Avg Margin, Win as Fav%, Win as Dog%, Total Game Probability
- Sortable columns, colour-coded percentages
- Server returns `first_scorer_by_team` array from `/api/custom-queries`

### New API endpoints

| Endpoint | Purpose |
|----------|---------|
| `/api/pw-calls` | Filtered PW calls with season, half, band, consensus, spread_role, margin, call_selection params |
| `/api/pw-trend` | PW accuracy grouped by `pw_version` with calls_by_half and calls_by_band |
| `/api/pw-roi-combos` | Best condition combos by accuracy with min_calls and limit params |

### Dashboard additions

- **Predicted Winners tab** (under Games): filter bar (season, half, band, consensus, role, margin, selection), summary stats, calls table with confidence colour bands
- **PW Metrics tab** (under Analysis): version-grouped accuracy table with progress bars
- **PW ROI Combos tab** (under Analysis): condition combo accuracy table with min calls / limit filters
- **PW badges on live game cards:** predicted team + confidence % + consensus indicator
- **Per-team First Scorer table** in Custom Queries

### Backfill PW calls (`--pw-calls` flag)

- Opt-in flag for `backfill.py` (default: no PW calls generated)
- Evaluates at 10-minute intervals (0', 10', 20', ... 80')
- Uses ML + historical blend with half weights and margin gates
- Additional flags: `--pw-threshold`, `--pw-cooldown-minutes`, `--pw-force`
- Records stored as `predicted_winner_calls[]` in game_history.json with `synthetic: true`

### Configuration keys added

```json
{
  "predicted_winner_enabled": false,
  "predicted_winner_threshold": 65,
  "predicted_winner_alert": { "enabled": true, "threshold_pct": 65, "threshold_pct_favorite": 70, "threshold_pct_underdog": 60 },
  "pw_half_weights": { "H1": 0.90, "H2": 1.05, "ET": 1.10 },
  "pw_margin_gate": -12, "pw_margin_gate_late": -8,
  "pw_polarity_gate": 0, "pw_edge_gate": 0,
  "pw_cooldown_minutes": 5,
  "pw_blend_weights": { "ml": 0.70, "historical": 0.30 },
  "pw_margin_boost_leading": 1.0, "pw_margin_boost_leading_late": 1.05,
  "pw_consensus_boost_strong": 1.0, "pw_consensus_penalty_conflicted": 1.0,
  "pw_version": "v1.0",
  "odds_poll_interval_h1": 5, "odds_poll_interval_late": 2, "odds_poll_late_threshold_min": 70
}
```

### Files changed

| File | Lines | Summary |
|------|-------|---------|
| `conditions.py` | +147 | `first_team_scores` + `predicted_winner_threshold` evaluators |
| `config.example.json` | +67 | PW config keys, odds polling, 4 new conditions |
| `ml_model.py` | +1 | `first_team_scores` in CONDITION_TYPES |
| `monitor.py` | +520 | PW evaluation pipeline, adaptive odds polling, PW alerts |
| `server.py` | +295 | 3 PW endpoints, per-team first scorer |
| `dashboard.html` | +183 | PW tabs, badges, First Scorer table |
| `backfill.py` | +209 | `--pw-calls` with synthetic PW generation |

---

## 2026-05-19 — Update CLAUDE.md with backup architecture (#16)

**Issue:** #16
**Commit:** 1f32ac7

- Added `runtime_backup.py` and `recovery.py` to Core Files table
- Data flow updated with WAL append, backup timer (16:00 UTC), pre-config-save snapshots
- Dashboard structure updated with Recovery sub-tab and Runtime Backups section
- 6 new backup/recovery API endpoints in API Surface table
- New **Backup & Recovery** architecture section: three-layer protection, snapshot labels/retention, CLI reference, deploy instructions
- `game_history.wal` and `runtime_backups/` added to Persistent State Files
- Backup timer added to automated scheduling instructions
- WAL/fsync noted in Key Technical Constraints

---

## 2026-05-19 — Dashboard backup UI and API endpoints — Phase 4 (#16)

**Issue:** #16
**Phase:** 4 of 5 — Dashboard UI + server API endpoints

### Dashboard — Recovery sub-tab (Monitor Status)

- WAL status panel (entries, size, date range)
- Backup list table: Name, Records, Size, Date, WAL coverage indicator (YES/GAP/—)
- Restore button per backup (confirmation dialog with WAL replay option)
- Delete button per backup
- Retention controls: days input + Prune Now button (default 60 days)
- Help modal (?) with three-layer architecture explanation, CLI reference

### Dashboard — Runtime Backups section (Configuration)

- "Take Snapshot Now" button with status indicator
- Scrollable snapshot list: label (color-coded), local/UTC time, file count toggle, directory name
- Help modal (?) with snapshot types, retention policies, restore instructions
- Dynamic file list populated from API response

### Server API endpoints (6 new)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/snapshots` | GET | List all runtime snapshots with metadata |
| `/api/snapshots/create` | POST | Create manual snapshot |
| `/api/recovery/backups` | GET | List game_history backups + WAL status |
| `/api/recovery/restore` | POST | Restore from backup with optional WAL replay |
| `/api/recovery/delete-backup` | POST | Delete a single backup |
| `/api/recovery/prune-backups` | POST | Delete backups older than N days |

### Files modified

- `dashboard.html` — Recovery sub-tab, Runtime Backups section, 2 help modals, 8 JS functions
- `server.py` — 6 new API endpoints (2 GET, 4 POST)

---

## 2026-05-19 — Pre-backfill and pre-config-save snapshots — Phase 3 (#16)

**Issue:** #16
**Phase:** 3 of 5 — Automatic snapshots before destructive writes

### Pre-backfill snapshots

- `backfill.py` — snapshot with label `pre-backfill` before writing `game_history.json` in `replay_season()`
- `backfill_odds.py` — snapshot before writing odds data to `game_history.json`
- `backfill_pregame.py` — snapshot before writing pregame data to `game_history.json`
- Pre-backfill snapshots are retained indefinitely (no auto-prune)

### Pre-config-save snapshots

- `server.py` `POST /api/config` — snapshot with label `pre-config-save` before writing `config.json`
- Auto-prunes to keep last 10 `pre-config-save` snapshots
- Snapshot directory name returned in response: `{"status": "saved", "snapshot": "..."}`

### Files modified

- `backfill.py` — added pre-backfill snapshot hook
- `backfill_odds.py` — added pre-backfill snapshot hook
- `backfill_pregame.py` — added pre-backfill snapshot hook
- `server.py` — added pre-config-save snapshot + auto-prune in config save handler

---

## 2026-05-19 — WAL and auto-recovery — Phase 2 (#16)

**Issue:** #16
**Phase:** 2 of 5 — Write-Ahead Log + automatic corruption recovery

### Write-Ahead Log (WAL)

- `game_history.wal` — append-only JSONL file, one game record per line
- `monitor.py` `append_game_record()` writes to WAL **before** `game_history.json` — survives crashes between steps
- `outcomes.append_to_wal()` — fsync'd append for durability
- WAL auto-truncated after each backup (retains entries for last 5 backups)

### Auto-recovery

- `outcomes.load_history()` detects corrupted `game_history.json` (JSONDecodeError)
- Automatically restores from newest valid backup in `runtime_backups/`
- Replays WAL to recover records added since the backup
- Preserves corrupted file as `game_history.json.corrupted`
- Logs `[CRITICAL]` messages to stderr with timestamps

### outcomes.py changes

- `append_to_wal(record)` — fsync'd JSONL append
- `save_history()` — added fsync before os.replace for write durability
- `load_history()` — auto-recovery on corruption (backup + WAL replay)
- `_attempt_recovery_from_backup()` — searches backups newest-first, replays WAL

### Tested

- WAL append + status (2 entries, correct date range)
- Auto-recovery: corrupted file -> recovered 514 records from backup + 2 from WAL = 516 total
- Corrupted file preserved as `.corrupted`

---

## 2026-05-19 — Runtime backup and recovery — Phase 1 (#16)

**Issue:** #16
**Phase:** 1 of 5 — Snapshot infrastructure, scheduled backups, recovery CLI

### New files

- `runtime_backup.py` — Create, restore, and prune timestamped runtime snapshots
- `recovery.py` — CLI for backup listing, restore (with WAL replay), WAL status/truncate
- `systemd/nrl-backup.service` — Systemd oneshot for daily scheduled snapshot
- `systemd/nrl-backup.timer` — Daily at 16:00 UTC (outside NRL game window 06:00–12:00 UTC)

### Runtime files backed up (9 total)

`game_history.json`, `alerts.json`, `live_stats.json`, `game_ticker.json`, `.alert_state.json`, `odds_api_log.json`, `model_nrl.pkl`, `model_meta_nrl.json`, `backfill_state.json`

### Snapshot features

- Timestamped snapshot directories: `runtime_backups/<ISO8601>-<label>/`
- Labels: `scheduled`, `manual`, `pre-config-save`, `pre-backfill`
- `metadata.json` per snapshot (created_at, config fingerprint, file sizes/mtimes, history row count)
- Orphaned `.tmp.<pid>` cleanup during snapshot creation
- Retention pruning: `--keep-last N` with optional `--prune-label` filter

### Recovery CLI

```
python3 recovery.py list                           # List backups (name, records, size, date)
python3 recovery.py restore <name>                 # Restore game_history.json + WAL replay
python3 recovery.py restore <name> --no-wal        # Restore without WAL
python3 recovery.py wal-status                     # Show WAL file status
python3 recovery.py wal-truncate [--keep N]        # Truncate old WAL entries
```

### outcomes.py additions

- `save_history()` — atomic write with PID-based temp files
- `list_backups()` — enumerate backup dirs with record counts and sizes
- `restore_from_backup()` — restore game_history.json with optional WAL replay
- `replay_wal()`, `wal_status()`, `truncate_wal()` — WAL management helpers

### Deploy

```bash
cp systemd/nrl-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nrl-backup.timer
```

---

## 2026-05-19 — Total game probability in Custom Queries tables

**Issue:** #10 (follow-up)
**Commit:** 0bade0c

- Late Tries tables (1+/2+/3+): column renamed to "% All Games", subtitle shows total games count
- Trailing 20+ table: added "% All Games" columns for both 1+ and 2+ tries — probability relative to all games (not just trailing), alongside existing "Prob %" relative to trailing games
- Example: Last 10min 1+ tries — 29.7% of trailing games, 10.1% of all 514 games

---

## 2026-05-19 — First scorer analysis, late tries 2+/3+, column sorting

**Issue:** #10
**Commit:** 433d1c7

### First Scorer Analysis (new section)
- Team that scores first try: wins 1H 69.6%, wins game 67.1%
- Count, percentage, and bar chart for both metrics

### Late 2H Tries — expanded to three sections
- **1+ tries** (existing): 74.7% of games in last 10min
- **2+ tries** (new): 32.1% in last 10min, 17.1% last 7min
- **3+ tries** (new): 6.2% in last 10min, 2.1% last 7min

### Trailing 20+ — Prob % column
- Renamed existing % column to "Prob %" for clarity

### Column Sorting (all analysis tables)
- Clickable ⇅ column headers on all tables (Outcomes + Custom Queries)
- Toggle ascending/descending on click
- Numeric-aware sorting (parses numbers from cell text)

---

## 2026-05-19 — Odds API log pagination and context filter

**Issue:** #14
**Commit:** 54c9697

- Log entries increased from 20 to 2000
- Client-side pagination: First/Prev/Next/Last with page indicator
- Page size selector: 10/25/50/100 (default 25)
- Context filter dropdown: dynamically populated from log (test_live, upcoming, backfill_historical, etc.)
- Filter applies before pagination, filtered count shown

---

## 2026-05-19 — Reduce Odds API markets from 3 to 2 (33% credit savings)

**Issue:** #13
**Commit:** 2e9f6ed

- Default markets changed from `h2h,spreads,totals` to `h2h,spreads`
- Live calls: 2 credits (was 3), Historical: 20 credits (was 30)
- Dropped `totals` (O/U) market — not used in conditions, ML, or predictions
- Config: `odds_api_markets` option to override if totals needed
- `_fetch_all_events()` and `fetch_upcoming_odds()` accept `markets` parameter
- `backfill_odds.py` uses `odds_api.MARKETS` constant instead of hardcoded string

---

## 2026-05-18 — Odds API chart axis labels and mouseover tooltips

**Commit:** 88dc6ad

- Left Y-axis label: "API Calls / Credits Used" (blue, vertical)
- Right Y-axis label: "Credits Remaining" (green, vertical)
- Mouseover tooltips on chart: hover any time bucket to see timestamp, API Calls, Credits Used, Credits Remaining, Cache Hits in a styled tooltip card
- Tooltip follows mouse position, hides on mouse leave

---

## 2026-05-18 — Odds API table columns and 24h gate

**Commits:** 9fce4da, 0e4aa37

- Daily Consumption table: added Credits Remaining column (end-of-day balance)
- Recent Log Entries table: replaced single Details column with separate API Calls, Credits Used, Credits Remaining, Latency, and Context columns
- Reduced odds fetch gate from 48h to 24h before kickoff

---

## 2026-05-18 — Optimise Odds API calls from Upcoming Games tab

**Issue:** #12
**Commit:** f3588d4

Three-layer caching to reduce credit consumption by ~90%:

| Layer | TTL | Purpose |
|-------|-----|---------|
| Odds API upcoming cache (`odds_api.py`) | 15 min (configurable) | Prevents repeated API calls for pre-game odds |
| Server upcoming cache (`server.py`) | 2 min | Caches full payload including NRL fixture + ladder data |
| Dashboard JS cache (`dashboard.html`) | 5 min | Prevents re-fetch on tab switch |

- New `fetch_upcoming_odds()` with separate cache from live odds
- 48h gate: skips Odds API call entirely when no games within 48 hours
- Config: `odds_api_upcoming_cache_ttl` (default 900s = 15 min)
- Extracted `_renderUpcoming()` for reuse from JS cache path

---

## 2026-05-18 — Enhanced Odds API usage dashboard

**Issue:** #11
**Commit:** 7b7448b

Replaced basic Odds API sub-tab with NBA Monitor-style full usage dashboard:
- **Summary cards**: Credits Remaining (colour-coded), Total API Calls + credits + cache hits, By Context breakdown
- **Daily Credit Consumption table**: date, API calls, cache hits, credits used, avg latency (newest first)
- **Recent Log Entries** (last 20): time, type (colour-coded), credits remaining, details
- **Multi-dataset canvas chart**: blue bars (API Calls) + yellow bars (Credits Used) + green line (Credits Remaining) with 1/5/15/30/60 min interval selector and dual Y-axes

---

## 2026-05-18 — Fix odds backfill: 370/514 games now have odds (72%)

**Issue:** #9 (investigation)
**Commit:** 6c30326

Root cause: The Odds API historical endpoint returns odds for UPCOMING games from the snapshot date, not games played on that date. Fetching `date=2026-03-01` returns pre-game odds for games on 2026-03-05 to 2026-03-08.

Fix: Pool all cached events across all dates (466 events from 44 cache files) and search the full pool for each game. Also try fetching 1-3 days before kickoff when not in cache.

Coverage now:
- **2024:** 142/213 (67%)
- **2025:** 142/213 (67%)
- **2026:** 86/88 (98%)
- **Total:** 370/514 (72%), up from 8 (1.6%)

Remaining 144 missing games are early-season (2024 R1-R10, 2025 R1-R10) where no cached odds snapshots exist from dates before those games.

---

## 2026-05-18 — Role filter and trailing 20+ table enhancement

**Issue:** #9 (follow-up)
**Commit:** f60d724

- **Role filter** (Favorite/Underdog) added to both Outcomes and Custom Queries sub-tabs — filters games where the favourite or underdog won, based on ML odds, NRL.com H2H, or spread-derived odds
- **Trailing 20+ table** enhanced with: Games trailing 20+ column, % of trailing games columns for 1+ and 2+ tries, inline bar chart for 1+ tries percentage
- Key finding: 175 games (34% of 514) had a team trailing by 20+; 29.7% of those scored 1+ tries in the last 10 minutes

---

## 2026-05-18 — Filters for Analysis Outcomes and Custom Queries

**Issue:** #9 (follow-up)
**Commit:** bbfc512

Added 6 filters to both Analysis sub-tabs:
- **Season** — dynamic dropdown from game history (2024/2025/2026)
- **Segment** — All / Regular Season / Finals
- **Tag** — All / Win / Close / Comeback / Blowout
- **Date range** — from/to date pickers
- **Sort** (Outcomes only) — Games / Win%
- **Sort order** (Outcomes only) — Highest / Lowest first

Server-side `_filter_records()` shared between `/api/analysis` and `/api/custom-queries`. Both endpoints return `filters` applied and `seasons` list. Dropdowns auto-populate. Filters apply immediately on change.

---

## 2026-05-18 — Custom Queries sub-tab under Analysis

**Issue:** #9
**Commit:** d3eb5bc

New Analysis → Custom Queries sub-tab with season filter (All/2024/2025/2026):

1. **1H Totals O/U** — 16 lines (10.5–40.5) with under/over counts, percentages, and inline bar charts. Avg 1H total: 23.2, 22.5 line is ~50/50 split
2. **Late 2H Tries** — games with tries in last 10/7/5/2 minutes with total tries and per-game average. 74.7% of games have a try in the last 10 minutes
3. **Trailing 20+ Late Tries** — teams losing by 20+ that score 1+ or 2+ tries in final minutes. 52 instances (1+ tries in last 10min), 11 with 2+ tries

New `/api/custom-queries` endpoint with `?season=` filter parameter.

---

## 2026-05-18 — Historical pregame ladder backfill for all 514 games

**Issue:** #8 (follow-up)
**Commit:** 43ee9e8

- New `backfill_pregame.py` — fetches per-round ladder snapshots from NRL.com ladder API (supports historical: `?season=2024&round=15`)
- 93 ladder snapshots cached across 2024/2025/2026 seasons
- All 514 games now have `pregame` field with: W-L record, ladder position, home/away records, streak, form, points for/against/difference
- Uses ladder from round N-1 for round N games (shows state BEFORE the game)
- Game Detail Pregame Info section now shows: record with position (better highlighted green), home/away records, streak, points difference, plus HT score/2H swing/odds

---

## 2026-05-18 — Fix pregame info visibility in game history detail

**Issue:** #8 (follow-up)
**Commit:** 347542e

- Pregame Info section now renders for ALL 514 games (was hidden because it only checked for odds data)
- Shows HT Score and 2H Swing for every game (uses `halftime_margin` which all records have)
- Shows H2H, Spread, Total when Odds API data is available (8 games from historical backfill)
- Fixed `backfill_odds.py` fuzzy team matching: handles short→full name mapping ("Raiders" → "Canberra Raiders"), multi-word names, orientation swaps
- Added "Cronulla Sutherland Sharks" (no hyphen) to `odds_api.py` team map
- Ran historical odds backfill from cache: 8 games matched with spread/ML data

---

## 2026-05-18 — Pregame info: ladder records, odds, spreads in upcoming and game detail

**Issue:** #8
**Commit:** f8d6a3e

### Ladder data (`nrl_api.py`)
- `get_ladder(season)` with 1-hour cache — fetches all 17 teams from NRL.com ladder API
- Per-team data: W-L record, position (ordinal), home/away records, streak, form (last 5), points for/against/difference, avg winning/losing margin, close games, golden point wins

### Server
- New `/api/ladder` endpoint — all teams with ladder data
- `/api/upcoming` enhanced with per-game: team records, positions, streaks, Odds API spread/ML/totals/bookmaker
- Automatic Odds API fetch when `odds_api_enabled` is true

### Dashboard — Upcoming Games cards
- Team W-L records with ladder positions (e.g. "5-5 (8th)")
- Better record highlighted in green
- Streak display for both teams
- Odds API spread line (e.g. "Dolphins -0.5"), moneyline, total O/U, bookmaker name
- Layered with existing NRL.com H2H odds

### Dashboard — Game History Detail
- New Pregame Odds section (between tags and stats comparison)
- Side-by-side H2H odds with favourite highlighted in green
- Spread points for both teams, total O/U line
- Bookmaker name and formatted spread detail string

---

## 2026-05-18 — Odds API activated, Bet365 priority, docs updated

**Commit:** 1e3b8dd

- Odds API activated in config with API key (4,234 credits remaining)
- Bookmaker priority changed to Bet365 first (falls through to SportsBet for NRL as Bet365 lacks NRL markets on this API)
- Live test confirmed: all 5 Round 12 games returned with spreads + moneylines from SportsBet
- CLAUDE.md updated with: odds_api.py, backfill_odds.py, /api/odds-api-log, The Odds API section, 12 new stat conditions, odds config keys, counts updated (26 conditions, 15 APIs, 35-feature ML model)
- README.md updated with: same additions plus The Odds API usage section with CLI examples

---

## 2026-05-18 — The Odds API integration for live odds/spreads/moneylines

**Issue:** #7
**Commit:** d678fb2

### Core module (`odds_api.py`)
- `fetch_live_game_odds()` for NRL games (sport key: `rugbyleague_nrl`)
- 22-entry NRL team name mapping with aliases for The Odds API matching
- Australian bookmaker priority: Sportsbet > TAB > Ladbrokes > Bet365 > PointsBet > Unibet > Neds
- Module-level 120s cache, credit tracking, low-credit warnings
- Persistent usage logging (`odds_api_log.json`): startup/per-call/daily summary records (capped 5,000)
- `extract_odds()` for h2h (moneyline), spreads (handicap), totals (over/under)
- `format_spread_detail()` for human-readable spread strings (e.g. "Storm -4.5")

### Historical backfill (`backfill_odds.py`)
- Backfill historical odds from `/v4/historical/` endpoint
- Per-date snapshots cached in `odds_cache/`
- CLI: `--api-key`, `--dry-run`, `--start-date`, `--end-date`, `--use-cache`, `--test-live`
- Populates: `home_spread`, `away_spread`, `home_ml`, `away_ml`, `total_line`, `odds_bookmaker`, `spread_detail`

### Dashboard — Odds API sub-tab (Monitor Status)
- Credit balance and status summary (remaining, calls, credits used, last call)
- API call log table with: time, credits used, remaining, HTTP status, latency, context
- Credits-over-time line chart (canvas-based, NRL green theme)

### Server & config
- `/api/odds-api-log` endpoint with `?last=N` parameter
- Monitor status enhanced with `odds_api_enabled` flag
- Config: `odds_api_enabled`, `odds_api_key` keys

---

## 2026-05-18 — New conditions covering all NRL team stats

**Issue:** #6
**Commit:** d97dc42

Added 12 new condition types covering every available NRL team stat, bringing the total to 26 condition types and 34 configured conditions.

### New condition types
| Type | Signal | Default threshold |
|------|--------|-------------------|
| `run_metres` | Forward momentum | >2000 (good) / <1400 (poor) |
| `post_contact_metres` | Physical dominance in contact | >700 |
| `tackle_breaks` | Ability to beat defenders | >40 |
| `line_breaks` | Attacking penetration (raw count) | >7 |
| `offloads` | Creative attack / ball-playing | >14 |
| `intercepts` | Defensive reads / turnovers forced | >2 |
| `ineffective_tackles` | Weak tackling technique | >18 |
| `effective_tackle_pct` | Overall tackling efficiency | <85% (poor) / >92% (dominant) |
| `kick_defusal` | High ball handling | <60% (vulnerable) |
| `kick_return_metres` | Territory gained from kick returns | >250m |
| `play_the_ball_speed` | Fatigue / tempo indicator | >4.0s (slow) |
| `red_card` | Player sent off permanently | >=1 |

### Results
- 26 condition types total (8 original + 6 PBP + 12 new stats)
- 34 conditions in config (with positive/negative variants for key stats)
- 8,299 condition fires across 514 games (16.1 avg/game, up from 6,197)
- ML model retrained: 35 features, 98.9% CV accuracy
- 25 of 26 types firing in backfill (`try_scoring_run` requires live event data)

---

## 2026-05-18 — NRL PBP analysis and predicted winner model

**Issue:** #5
**Commit:** dd8def4

### Timeline feature extraction (`timeline_features.py`)
- Processes NRL match timeline (56,488 events across 514 games, 22 event types)
- Extracts 21 PBP features per game: scoring runs, error streaks, penalty windows (10-min sliding), sin bin events, line break surges, 40/20 kicks, halftime margin, 2H momentum, margin trajectory, score progression

### PBP-based conditions (6 new types)
- `momentum_shift` — team scored X+ unanswered points
- `error_streak` — team committed X consecutive errors
- `penalty_pressure` — X penalties in a 10-minute window
- `sin_bin` — player sin-binned (team down to 12)
- `line_break_surge` — X line breaks in 10-minute window
- `halftime_turnaround` — trailing by 6+ at HT but now leading
- Total condition types: 14 (8 original + 6 PBP)
- Backfill re-run: 6,197 condition fires across 514 games (up from 3,629)

### ML predicted winner model (`ml_model.py`)
- Logistic regression with 23 features (14 condition binary + 9 context)
- Context features: score_margin, is_home, halftime_margin, h2_momentum, max_unanswered, max_error_streak, max_penalty_window, sin_bins, line_break_surge
- Trained on 1,024 samples from 514 games
- Training accuracy: 99.6%, CV accuracy: 99.2% (+/- 0.9%)
- Top features by importance: score_margin, halftime_margin, h2_momentum, halftime_turnaround condition
- Model artifacts: `model_nrl.pkl` + `model_meta_nrl.json`

### Dashboard integration
- `/api/model-status` endpoint — model availability, schema, accuracy metrics
- Analysis tab: ML Model Status section showing training details
- `config.example.json` updated with 6 new PBP condition definitions

---

## 2026-05-18 — Dashboard restructure: Control Panel with nested tabs

**Issue:** #4
**Commit:** cab6317

Restructured the dashboard from flat tabs into an NBA Monitor-style Control Panel with nested panel tabs and sub-tabs.

### Panel structure
```
🧭 Control Panel
├── Monitor Status (Overview | Logs)
├── 🏉 Games (Live | Alerts | Upcoming | Post Game)
├── Configuration (General | Conditions | Compounds)
├── 📊 Analysis (Condition Outcomes | Tag Analytics)
└── 📚 Game History (full browser, preserved)
```

### New features
- **Monitor Status → Overview**: polling status, NRL API, round, live games, season, teams, cooldown, channel, conditions count, game history count, disk space, last run, current time
- **Monitor Status → Logs**: recent monitor.log entries with error/warning highlighting
- **Games → Post Game Summary**: recently completed games with scores, tags, venue
- **Analysis → Condition Outcomes**: per-condition stats table (games, win%, CLOSE/BLOWOUT/COMEBACK counts), sorted by sample size
- **Analysis → Tag Analytics**: bar chart visualisation of tag rates across all games
- **Configuration → Compound Conditions**: displays compound rules with operator and refs

### New server APIs
- `/api/monitor-status` — polling status, config summary, disk space, game counts
- `/api/log-events` — recent monitor.log lines (last 64KB)
- `/api/postgame` — recently completed games (configurable window)
- `/api/analysis` — condition outcome stats and tag frequencies

---

## 2026-05-18 — Team colour icons across dashboard

**Issue:** #3 (follow-up)
**Commit:** 66882c3

- Added circular badge icons with official NRL team colours and 3-letter abbreviations
- Icons appear in: Live Games, Upcoming Games, Alert History, Game History table, Game Detail (header, score breakdown, stats comparison, conditions fired, try timeline)
- 17 teams with official primary/accent colours (e.g. Storm purple/gold, Panthers navy/red, Broncos maroon/gold)
- Pure CSS rendering — no external images or CDN dependencies

---

## 2026-05-18 — Game history browser and detail views

**Issue:** #3
**Commit:** 96c754c

### Server APIs
- `/api/history` — server-side filtering with 10 filter params (team, condition, tag, season, segment, date range, free-text, sort, pagination); returns slim records for list view
- `/api/history-game` — full game detail with stats, conditions_fired, try_events
- `/api/history-facets` — facet counts for filter dropdowns (17 teams, 12 conditions, 4 tags, 3 seasons)

### Dashboard Game History tab
- Filter bar with text inputs, dropdowns, date range, free-text search, page size, sort
- Quick chips for tags (CLOSE/BLOWOUT/COMEBACK) and top teams from facets API
- Game list table with: round, date (AEST), teams, score, 1H score, margin, winner, postgame tag pills, conditions count, detail button
- Tag pills colour-coded: UPSET=red, CLOSE=yellow, BLOWOUT=orange, COMEBACK=blue

### Game Detail panel (click any game)
- Score breakdown table: 1H/2H/Total with tries and conversions per team
- Result with winner and margin
- 13-stat team comparison with better-value highlighting: Possession, Completion Rate, Errors, Penalties, Missed Tackles, Tackles Made, Effective Tackle %, Run Metres, Line Breaks, Tackle Breaks, Offloads, Kicks, Kicking Metres
- Conditions fired cards with type, team, direction, score margin
- Try timeline with minute marks and team attribution
- NRL.com link, venue, attendance

---

## 2026-05-18 — Backfill game history for 2024–2026 seasons

**Issue:** #2
**Commit:** 6f2c074

- Built `backfill.py` — 2-phase backfill script for historical game data
- **Phase 1** (`--fetch-cache`): Downloads draw + match centre data into `nrl_cache/<season>/`; idempotent, respects request delay
- **Phase 2** (`--use-cache --write-canonical`): Replays cached data, evaluates conditions from config, computes postgame tags, writes `game_history.json`
- CLI flags: `--dry-run`, `--skip-existing`, `--resume`, `--season`, `--delay`
- Covers regular season (R1–R27) and finals (R28–R31)
- Postgame tags: CLOSE (margin ≤6), BLOWOUT (margin ≥20), COMEBACK (trailing at HT wins), UPSET (underdog wins by odds)
- Scoring run analysis from try event timeline

### Backfill results
- **514 games** across 3 seasons (2024: 213, 2025: 213, 2026: 88)
- 100% stats coverage (avg 32 keys per game)
- 4,220 try events with timeline data
- 3,629 conditions fired across all games
- Tags: 155 CLOSE, 104 COMEBACK, 175 BLOWOUT
- Note: Pre-game odds not available for historical seasons (NRL API only serves odds for current fixtures)

---

## 2026-05-18 — Upcoming Games tab

**Commit:** 6ca4baf

- Added `/api/upcoming` endpoint — fetches current round fixtures from NRL.com with kickoff times (`clock.kickOffTimeLong`), odds, and venues; cached for 120s
- Dashboard Upcoming Games tab with games grouped by date (Today/Tomorrow/date headers)
- Each card shows: team matchup with abbreviations, kickoff time (AEST), countdown timer, odds with favourite highlighted in green, venue
- Default upcoming window: 168 hours (1 week)
- Default dashboard port changed to 8898 (NBA Monitor uses 8899)
- `nrl_api.py` updated to extract kickoff time, odds, venue_city from fixture data

---

## 2026-05-18 — Initial NRL Monitor project

**Issue:** #1
**Commit:** 023db9f

Built from scratch, adapting the NBA Monitor architecture for the NRL Telstra Premiership.

### Core files
- `nrl_api.py` — NRL.com API client (draw fixtures, match centre data, 17-team mappings with abbreviations and timezones)
- `monitor.py` — Polling engine (fetches live games, evaluates conditions, sends Slack alerts via direct API or OpenClaw gateway, persists state, records game history)
- `conditions.py` — 8 NRL-specific condition evaluators: score_diff, error_rate, completion_rate, penalty_count, possession, try_scoring_run, points_run, missed_tackles
- `outcomes.py` — Historical outcome analysis with exponential recency weighting
- `server.py` — Dashboard HTTP server with JSON APIs
- `dashboard.html` — Single-page dashboard with Live Games, Alert History, Configuration, Game History tabs

### Configuration & infrastructure
- `config.example.json` — Template with 14 NRL-specific conditions + 1 compound condition
- `setup.sh` — One-command bootstrap (deps, NRL API test, systemd/cron setup)
- `systemd/` — Timer + service units for AEST game windows (06:00–12:00 UTC)

### Key NRL adaptations from NBA Monitor
- NRL.com APIs instead of ESPN (draw API + match centre)
- 2 halves (1H/2H) instead of 4 quarters
- NRL stats: errors, set completion %, penalties, possession, missed tackles
- AEST game windows (Thu–Mon evenings) instead of US evening times
- 17 NRL teams with abbreviations
- GitHub repo: https://github.com/bobcheong/NRLMonitor
