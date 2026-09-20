# Design: Odds API Request/Response Logging (#192)

## Summary

Add optional logging of all Odds API HTTP requests and full responses (not just fallback events), controlled via a dashboard checkbox, viewable in the Odds API tab and reusing the existing fallback data modal.

Cross-repo: NBA #355

## Storage

- **File:** `odds_api_request_log.jsonl` (JSONL, append-only, git-ignored)
- **Config keys:**
  - `odds_api_request_log_enabled` (bool, default `false`)
  - `odds_api_request_log_retention_days` (int, default `1`)
- **Auto-prune** on server startup, same pattern as fallback data pruning

## Entry Schema

Each JSONL line:

```json
{
  "ts": "2026-06-23T05:00:00Z",
  "context": "pregame|odds_monitor|startup|api_test_events|api_test_odds",
  "request_url": "https://api.the-odds-api.com/v4/sports/rugbyleague_nrl/odds/",
  "request_params": {"regions": "au", "markets": "h2h,spreads", "oddsFormat": "decimal"},
  "response_status": 200,
  "response_size": 12400,
  "response_body": {},
  "latency_ms": 143.2,
  "credits_used": 2,
  "credits_remaining": 485,
  "event_count": 4,
  "home_team": null,
  "away_team": null
}
```

- `apiKey` is stripped from `request_params` before saving.
- `event_count` is the length of the response body array (when applicable).
- `home_team`/`away_team` are null for multi-event endpoints, populated for single-event test calls.

## Call Sites to Instrument (5)

| # | Function | File | Context | Notes |
|---|----------|------|---------|-------|
| 1 | `log_startup()` | odds_api.py:130 | `"startup"` | Free `/v4/sports/` call, no response body to save |
| 2 | `fetch_live_odds()` | odds_api.py:644 | Passed from caller (e.g. `"pregame"`, `"pw_fire"`, `"live_poll"`) | Main credit-costing call |
| 3 | `fetch_odds_monitor_data()` | odds_api.py:823 | `"odds_monitor"` | Credit-costing, 60s polling |
| 4 | `/api/odds-api-test-events` | server.py | `"api_test_events"` | Free `/v4/sports/.../events/` call |
| 5 | `/api/odds-api-test` | server.py | `"api_test_odds"` | Credit-costing test call |

**Excluded:** `query_live_credits()` (odds_api.py:435) — fires every 60s from dashboard polling, would be too noisy.

## Functions in odds_api.py

### configure_request_log(enabled, retention_days=1)

Mirrors `configure_fallback_save()`. Sets module-level `_request_log_enabled` and `_request_log_retention_days`.

### _save_request_log(ts, context, url, params, resp_status, resp_body, latency_ms, credits_used, credits_remaining)

- Guard: return immediately if `_request_log_enabled` is False
- Strip `apiKey` from params copy before writing
- Compute `response_size` from `len(json.dumps(resp_body))` when resp_body is not None
- Compute `event_count` from `len(resp_body)` when resp_body is a list
- Append JSON line to `REQUEST_LOG_FILE`

### load_request_log(last_n=200, context_filter=None)

- Read JSONL, return last N entries (without `response_body` field to keep payloads small)
- Optional `context_filter` string to filter by context value

### get_request_log_entry(ts)

- Return single full entry (including `response_body`) matching timestamp
- Used by detail modal

### prune_request_log()

- Delete entries older than `_request_log_retention_days`
- Called on server startup alongside fallback data pruning
- Returns count of pruned entries

## API Endpoints (server.py)

### GET /api/odds-api-request-log

Query params:
- `last` (int, default 200, max 500) — number of entries
- `context` (string, optional) — filter by context

Returns: `{"entries": [...], "count": N}` — entries without `response_body`.

### GET /api/odds-api-request-log-data?ts=...

Returns: full entry including `response_body` for a specific timestamp.

Returns 404 if not found.

## Dashboard Changes

### Odds API Tab — Usage Sub-tab

- **Enable checkbox** below the existing fallback save checkbox: "Log API Requests" toggle
  - Reads `odds_api_request_log_enabled` from config on load
  - Writes via `POST /api/config` on toggle (same pattern as fallback save toggle)
- **Retention input** next to checkbox: days input (same pattern as fallback retention)
- **Request log entries** appear in the existing "Recent Log Entries" table
  - New rows with `type: "api_request"` distinguished from existing call/fallback/bk_usage rows
  - Columns: timestamp, context, status, latency, response size, credits used
  - "View" button on each row opens the detail modal
- **Detail modal** reuses the existing `_viewFallbackData()` modal, generalized to show:
  - Request URL + params
  - Response status + full JSON body

### Logs Tab

No new structured log tag. Odds API HTTP calls already produce `[ODDS_API]` lines in monitor.log which classify under the existing `ODDS` tag in `_classifyLogEvent()`. The JSONL request log is a separate data store for the Odds API tab detail viewer — it does not feed into the structured logs.

## Server Startup Wiring

In `server.py` startup (alongside existing `configure_fallback_save` and `prune_fallback_data` calls):

```python
odds_api.configure_request_log(
    config.get("odds_api_request_log_enabled", False),
    config.get("odds_api_request_log_retention_days", 1))
pruned = odds_api.prune_request_log()
```

Also in `POST /api/config` handler: call `configure_request_log()` when config is saved.

## Config Save Wiring

In the config POST handler, after saving config, call `configure_request_log()` with the new values (same pattern as the existing `configure_fallback_save()` call).

## Files Changed

| File | Changes |
|------|---------|
| `odds_api.py` | Add `REQUEST_LOG_FILE`, `_request_log_enabled`, `configure_request_log()`, `_save_request_log()`, `load_request_log()`, `get_request_log_entry()`, `prune_request_log()`. Add `_save_request_log()` calls in `log_startup()`, `fetch_live_odds()`, `fetch_odds_monitor_data()`. |
| `server.py` | Add 2 API endpoints. Add startup wiring. Add config save wiring. Add `_save_request_log()` calls in test endpoints. |
| `dashboard.html` | Add enable checkbox + retention input. Extend log entries table to show request log rows. Generalize detail modal. |
| `config.example.json` | Add `odds_api_request_log_enabled` and `odds_api_request_log_retention_days` keys. |
