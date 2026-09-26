# CHANGES

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
