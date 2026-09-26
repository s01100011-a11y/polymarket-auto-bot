# Polymarket Auto Bot

Automated sports prediction-market trading service for [Polymarket](https://polymarket.com), built on the official `polymarket-client` Python SDK.

The bot ingests live game signals from upstream monitors and Telegram cappers, matches them to Polymarket prediction markets, applies configurable risk controls, and executes limit orders — either automatically or through a manual approval queue. It runs on Railway (cloud) with a Termux executor (Android phone) for on-device wallet signing.

---

## Table of Contents

- [Overview](#overview)
- [System Architecture](#system-architecture)
  - [High-Level Architecture](#high-level-architecture)
  - [Module Chain](#module-chain)
  - [Signal Ingestion Paths](#signal-ingestion-paths)
  - [Order Execution Flow](#order-execution-flow)
  - [Deployment Topology](#deployment-topology)
- [Modules](#modules)
  - [Core Trading Engine](#core-trading-engine)
  - [Dashboard Layer](#dashboard-layer)
  - [Signal Ingestion](#signal-ingestion)
  - [Research Modules](#research-modules)
  - [Termux Executor](#termux-executor)
  - [Infrastructure](#infrastructure)
- [Related Systems](#related-systems)
  - [NBAMonitor (upstream)](#nbamonitor-upstream)
  - [NRLMonitor (research only)](#nrlmonitor-research-only)
  - [Telegram-ChatGPT Bridge](#telegram-chatgpt-bridge)
- [Processing Flow Diagrams](#processing-flow-diagrams)
  - [WNBA Predicted Winner Flow](#wnba-predicted-winner-flow)
  - [NFL/CFB Capper Flow](#nflcfb-capper-flow)
  - [Termux Executor Flow](#termux-executor-flow)
  - [Watch Loop Flow](#watch-loop-flow)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Safety Gates](#safety-gates)
- [Installation](#installation)
  - [Termux (Android)](#termux-android)
  - [Railway (Cloud)](#railway-cloud)
- [API Endpoints](#api-endpoints)
- [Glossary](#glossary)
- [Index](#index)

---

## Overview

Polymarket Auto Bot is a FastAPI service that automates sports prediction-market trading on Polymarket. It supports multiple signal sources — live in-game analytics from the NBAMonitor system and professional sports picks from Telegram cappers — and converts them into executable limit orders on Polymarket's CLOB (Central Limit Order Book).

### Key capabilities

- **Multi-source signal ingestion**: WNBA Predicted Winner calls via Slack webhooks and direct Tailnet feed, NFL/CFB capper picks via Telegram bridge
- **Dual execution mode**: Paper trading (simulated) and live trading (real USDC orders) with triple-gate safety
- **Watch-until-price queue**: Signals are monitored until the market price reaches the configured limit or the signal expires
- **Risk management**: Per-trade caps, daily budget limits, maximum price/spread thresholds, political market blocking
- **Real-time dashboard**: Dark-themed web UI with P&L tracking, position management, live/paper mode controls, and SELL buttons
- **Termux executor**: Android-based order executor for wallet-local signing via the Polymarket SDK
- **Research tooling**: Spread capture, scalping analysis, game reconstruction, and backtesting modules

### Supported sports

| Sport | Signal Source | Status |
|-------|-------------|--------|
| WNBA | NBAMonitor Predicted Winner calls | Active — live auto-trading |
| NFL | Telegram cappers (SLAM, Syndicate) | Active — live auto-trading |
| CFB | Telegram cappers (SLAM, Syndicate) | Active — live auto-trading (via `CFB_CAPPER_LIVE_ENABLED`) |
| NBA | NBAMonitor PW calls (Tailnet feed) | Active — live auto-trading (via `PW_NBA_EXPORT_URL`) |

---

## System Architecture

### High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIGNAL SOURCES                               │
│                                                                     │
│  ┌──────────────┐   ┌──────────────────┐   ┌────────────────────┐  │
│  │  NBAMonitor   │   │  Telegram Bridge  │   │  Manual /auto/*    │  │
│  │  (WNBA/NBA)  │   │  (NFL/CFB Cappers)│   │  API Signals       │  │
│  └──────┬───────┘   └────────┬─────────┘   └────────┬───────────┘  │
│         │                    │                       │              │
│    ┌────┴────┐               │                       │              │
│    │  Slack  │  Tailnet      │                       │              │
│    │  Event  │  Direct       │                       │              │
│    │  Push   │  Poll         │                       │              │
│    └────┬────┘────┬──────────┘                       │              │
└─────────┼─────────┼──────────────────────────────────┼──────────────┘
          │         │                                  │
┌─────────┼─────────┼──────────────────────────────────┼──────────────┐
│         ▼         ▼          RAILWAY BOT             ▼              │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                    FastAPI Application                       │    │
│  │                                                             │    │
│  │  ┌─────────────┐  ┌───────────────┐  ┌──────────────────┐  │    │
│  │  │ Slack Ingest │  │ PW Export     │  │ NFL/CFB Capper   │  │    │
│  │  │ + WNBA Parse │  │ Ingest (Pull) │  │ Ingest (Poll)    │  │    │
│  │  └──────┬───────┘  └───────┬───────┘  └────────┬─────────┘  │    │
│  │         │                  │                    │            │    │
│  │         ▼                  ▼                    ▼            │    │
│  │  ┌──────────────────────────────────────────────────────┐   │    │
│  │  │            Signal Deduplication & Validation          │   │    │
│  │  └──────────────────────┬───────────────────────────────┘   │    │
│  │                         ▼                                   │    │
│  │  ┌──────────────────────────────────────────────────────┐   │    │
│  │  │     Strategy Filters (quarter, edge, venue, EV)      │   │    │
│  │  └──────────────────────┬───────────────────────────────┘   │    │
│  │                         ▼                                   │    │
│  │  ┌──────────────────────────────────────────────────────┐   │    │
│  │  │          Core Trading Engine (app/main.py)           │   │    │
│  │  │  ┌─────────┐ ┌──────────┐ ┌───────────┐ ┌────────┐  │   │    │
│  │  │  │ Risk    │ │ Market   │ │ Quote     │ │ Order  │  │   │    │
│  │  │  │ Checks  │ │ Matching │ │ & Spread  │ │ Submit │  │   │    │
│  │  │  └─────────┘ └──────────┘ └───────────┘ └────┬───┘  │   │    │
│  │  └───────────────────────────────────────────────┼──────┘   │    │
│  │                                                  │          │    │
│  │  ┌──────────────────────────────────────────┐    │          │    │
│  │  │         Dashboard (Web UI)                │    │          │    │
│  │  │  P&L · Positions · Settings · Controls    │    │          │    │
│  │  └──────────────────────────────────────────┘    │          │    │
│  └──────────────────────────────────────────────────┼──────────┘    │
│                                                     │               │
│                              Executor Queue          │               │
│                              (JSON file)             │               │
└──────────────────────────────────────────────────────┼───────────────┘
                                                       │
                                          ┌────────────┼────────────┐
                                          │  TERMUX    │  EXECUTOR  │
                                          │  (Android  ▼  Phone)    │
                                          │  ┌───────────────────┐  │
                                          │  │ Wallet Signing    │  │
                                          │  │ Order Submission  │  │
                                          │  │ Fill Confirmation │  │
                                          │  └───────────────────┘  │
                                          └─────────────────────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │   Polymarket    │
                                              │   CLOB / API    │
                                              └─────────────────┘
```

### Module Chain

The FastAPI application is assembled through a linear module chain. Each module imports the previous one's `app` object and adds its own routes and functionality:

```
app/main.py                        Core trading engine, risk checks, watch loop
  └─ app/dashboard.py              Web dashboard, auth, settings
       └─ app/slack_ingest.py      Slack webhook ingestion, WNBA team matching
            └─ app/slack_wnba.py   WNBA-specific PW message parsing
                 └─ app/slack_dashboard_v2.py    P&L tracking, history
                      └─ app/dashboard_filters_v5.py  Trade filtering
                           └─ app/dashboard_pnl_filters_v6.py  P&L by mode
                                └─ app/wnba_pw_history_v7.py   PW alert DB
                                     └─ app/wnba_pw_history_v8.py  DB summary
                                          └─ app/wnba_pw_history_v9.py  DB init
                                               └─ app/wnba_pw_history_v10.py  Backfill
                                                    └─ app/wnba_pw_filters_v11.py  PW filters
                                                         └─ app/wnba_pw_strategy_test_v12.py  Strategy
                                                              └─ app/wnba_pw_research_v13.py  [ENTRYPOINT]
```

The entrypoint module (`wnba_pw_research_v13.py`) also installs plugin-style modules:

```
wnba_pw_research_v13.py
  ├── pw_research_sync.install()        Research data sync via Tailnet
  ├── pw_scalping_research.install()    Scalping opportunity analysis
  ├── pw_market_research.install()      Market depth research
  ├── pw_spread_capture.install()       Forward spread capture
  ├── pw_game_reconstruction.install()  Game timeline reconstruction
  ├── pw_spread_backtest.install()      Spread strategy backtesting
  ├── nfl_capper_ingest.install()       NFL Telegram capper polling
  └── cfb_capper_preview.install()      CFB capper preview/validation
```

Additionally, these modules are wired into the chain between `dashboard.py` and `slack_ingest.py`:

```
app/live_trading.py                Live BUY/SELL test endpoints
  └─ app/wallet_dashboard.py      Wallet balance/portfolio display
       └─ app/live_test_dashboard.py   Sports market validation
            └─ app/termux_executor_dashboard.py   Executor queue & pairing
                 └─ app/termux_executor_dashboard_v2.py  P&L estimation
                      └─ app/dashboard_metrics_v3.py     Reconciliation
                           └─ app/dashboard_live_control_v4.py  Slack mode controls
```

### Signal Ingestion Paths

The bot receives trading signals through four independent paths:

```
                    ┌────────────────────────────────────┐
                    │          NBAMonitor                 │
                    │   (ESPN polling, PW generation)     │
                    └─────────┬──────────────┬───────────┘
                              │              │
                    Slack Event API     /api/pw-export
                    (push, ~1-3s)      (Tailnet poll, ~4-8s)
                              │              │
                              ▼              ▼
           ┌──────────────────────────────────────────────┐
           │               Railway Bot                     │
           │                                              │
Path 1 ──▶ │  slack_ingest.py ──▶ slack_wnba.py          │
           │       (Slack Events webhook)                  │
           │                                              │
Path 2 ──▶ │  pw_export_ingest.py                         │
           │       (Tailnet HTTPS poll every 8s)           │
           │                                              │
Path 3 ──▶ │  nfl_capper_ingest.py                        │
           │       (Telegram bridge poll every 15s)        │
           │                                              │
Path 4 ──▶ │  cfb_capper_preview.py                       │
           │       (Telegram bridge poll every 15s)        │
           │                                              │
           │  All paths ──▶ dedup ──▶ core._execute_limit()│
           └──────────────────────────────────────────────┘
```

**Path 1 — Slack Events (WNBA, push):** NBAMonitor sends a Slack alert when a Predicted Winner call is generated. Slack's Events API pushes the message to the bot's `/slack/events` webhook. Latency: ~1-3 seconds from call generation.

**Path 2 — PW Export Direct Feed (WNBA, pull):** The bot polls the NBAMonitor's `/api/pw-export` endpoint every 8 seconds over the Tailscale VPN. Uses HTTP CONNECT through Tailscale's outbound proxy with TLS. Serves as a redundant path with its own deduplication. Latency: ~4-8 seconds average.

**Path 3 — NFL Capper Ingest (NFL, pull):** Polls a Telegram-to-HTTP bridge service every 15 seconds for new NFL picks from professional cappers (SLAM, Syndicate). Parses team names, odds, and unit sizing from structured pick messages.

**Path 4 — CFB Capper Preview (CFB, pull):** Same architecture as NFL but in preview/validation mode — picks are parsed and validated but never queued for real execution.

**Deduplication:** Paths 1 and 2 can receive the same WNBA signal. The bot deduplicates using `_existing_pw_signal()` with content-hash identity matching (`_pw_signal_key()` / `_identity()`), so a signal arriving on both paths is executed at most once.

### Order Execution Flow

```
Signal Received
      │
      ▼
┌─────────────┐     ┌──────────────────┐
│ Parse &     │────▶│ Dedup Check      │──── duplicate ──▶ SKIP
│ Validate    │     │ (_pw_signal_key)  │
└─────────────┘     └────────┬─────────┘
                             │ new
                             ▼
                    ┌──────────────────┐
                    │ Strategy Filter  │──── filtered ──▶ LOG_SKIP
                    │ (quarter/edge/EV)│
                    └────────┬─────────┘
                             │ pass
                             ▼
                    ┌──────────────────┐
                    │ Risk Checks      │──── exceeded ──▶ REJECT
                    │ • budget cap     │
                    │ • max price      │
                    │ • max spread     │
                    │ • political block│
                    └────────┬─────────┘
                             │ pass
                             ▼
                    ┌──────────────────┐
                    │ Market Matching  │──── no match ──▶ LOG_ERROR
                    │ (Polymarket API) │
                    └────────┬─────────┘
                             │ matched
                             ▼
                    ┌──────────────────┐
                    │ Quote & Spread   │──── spread too wide ──▶ WAITING
                    │ (order book)     │
                    └────────┬─────────┘
                             │ ok
                             ▼
                    ┌──────────────────┐
                    │ Geoblock Check   │──── blocked ──▶ REJECT
                    └────────┬─────────┘
                             │ ok
                             ▼
              ┌──────────────┴──────────────┐
              │                             │
        LIVE_TRADING=true             LIVE_TRADING=false
        AUTO_TRADING=true
              │                             │
              ▼                             ▼
    ┌──────────────────┐          ┌──────────────────┐
    │ Executor Queue   │          │ Paper Trade       │
    │ (Termux pickup)  │          │ (simulated)       │
    │ or Direct Submit │          └──────────────────┘
    └────────┬─────────┘
             │
             ▼
    ┌──────────────────┐
    │ Limit Order      │
    │ (Polymarket CLOB)│
    └──────────────────┘
```

### Deployment Topology

```
┌──────────────────────────────────────────────────────────────────┐
│                         TAILSCALE VPN (Tailnet)                  │
│                                                                  │
│  ┌─────────────────────┐           ┌──────────────────────────┐  │
│  │   Home Server        │           │   Railway (Cloud)         │  │
│  │                     │  Tailnet   │                          │  │
│  │  NBAMonitor         │◀─────────▶│  Polymarket Auto Bot     │  │
│  │  • ESPN poll (30s)  │  HTTPS    │  • FastAPI + Uvicorn     │  │
│  │  • PW generation    │           │  • Tailscale userspace   │  │
│  │  • /api/pw-export   │           │  • SOCKS5 proxy          │  │
│  │  • Slack alerts     │           │  • Signal ingestion      │  │
│  │  :8445              │           │  • Dashboard             │  │
│  └─────────────────────┘           │  :$PORT                  │  │
│                                    └────────────┬─────────────┘  │
│                                                 │                │
│  ┌─────────────────────┐                        │                │
│  │   Android Phone      │    Executor Queue      │                │
│  │   (Termux)           │◀───────────────────────┘                │
│  │                     │    HTTPS polling                        │
│  │  termux_executor.py │                                        │
│  │  • Wallet keys      │──────────────────────▶ Polymarket CLOB │
│  │  • Order signing    │    Limit orders                        │
│  │  • Fill monitoring  │                                        │
│  └─────────────────────┘                                        │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

External Services:
  • Slack Events API ──▶ Railway /slack/events
  • Telegram Bridge  ──▶ Railway (polled by NFL/CFB ingest)
  • Polymarket API   ◀── Railway (quotes) + Termux (orders)
```

> **Note:** The Railway deployment uses a Docker container (`Dockerfile`) with `start.sh` as the entrypoint, which boots Tailscale userspace networking before starting Uvicorn. The `railway.json` specifies a Nixpacks builder with `app.main:app`, but the actual production entrypoint should be confirmed against the deployed configuration — `start.sh` launches `app.wnba_pw_research_v13:app`.

---

## Modules

### Core Trading Engine

#### `app/main.py` — Trading Core
The foundation of the entire application. Provides:
- **FastAPI app** instance and lifespan management
- **`live_trading_enabled()`** / **`auto_trading_enabled()`** — runtime environment variable checks (not cached at import time)
- **`_quote()`** — fetches order book from Polymarket's CLOB and calculates best ask/bid and spread
- **`_risk_checks()`** — enforces per-trade cap, daily budget, max price, max spread, political blocking
- **`_check_daily_budget()`** — aggregates daily spend in the configured trading timezone
- **`_execute_limit()`** — places a limit order on Polymarket via the SDK
- **`_watch_loop()`** — background async loop that polls the watch queue every `AUTO_POLL_SECONDS`
- **JSON file storage** with `threading.Lock` and atomic tmp-then-rename writes (`_write_json()`)
- **Data files**: `pending.json`, `watchlist.json`, `executions.json`

#### `app/structured_logging.py` — JSON Structured Logging
Configurable JSON log formatter with UTC timestamps, severity levels, logger names, and structured trading context fields (event, trade_id, request_id, stage, reason, status). Controlled by `LOG_LEVEL` environment variable.

### Dashboard Layer

#### `app/dashboard.py` — Web Dashboard (v0.5.3)
Dark-themed HTML dashboard with HTTP Basic authentication. Features:
- Trading parameter display and live editing (max trade, daily budget, price/spread limits)
- Auto-trading state toggle (persistent across restarts)
- Active watches, pending approvals, and execution history
- SELL buttons for open positions
- Executor status and version display
- `_dashboard_snapshot()` aggregation function

#### `app/dashboard_metrics_v3.py` — Reconciliation Engine
Periodic reconciliation of local trade records against on-chain Polymarket state. Detects fills, partial closes, and settlement.

#### `app/dashboard_live_control_v4.py` — Slack Trading Mode
Controls whether Slack-sourced signals trigger paper trades or live order preparation. Exposes `/api/slack-mode` endpoints. Railway `AUTO_TRADING` env var is authoritative and overrides dashboard state.

#### `app/dashboard_filters_v5.py` — Trade Filtering
Portfolio value calculation, trade bucketing (paper vs. live), and filtering logic for the dashboard views.

#### `app/dashboard_pnl_filters_v6.py` — P&L by Mode
Splits P&L history into paper and live buckets with periodic sampling (every 10 seconds, up to 5760 entries).

#### `app/wallet_dashboard.py` — Wallet Display
Connects to the Polymarket SDK to display real-time wallet balance, allowance, and portfolio value (masked for display).

#### `app/live_trading.py` — Live Test Endpoints
Protected BUY/SELL endpoints for manual live trading tests. Requires dashboard authentication. Validates against supported sports market types (moneyline, spread, total).

#### `app/live_test_dashboard.py` — Sports Market Validation
Additional validation layer restricting live test BUY to supported sports markets only.

### Signal Ingestion

#### `app/slack_ingest.py` — Slack Events Ingestion
Receives Slack Events API webhooks at `/slack/events`. Features:
- HMAC signature verification using `SLACK_SIGNING_SECRET`
- Channel filtering via `SLACK_CHANNEL_ID`
- WNBA team name matching with aliases (15 teams, multiple aliases each)
- Alert age checking (rejects events older than `SLACK_MAX_ALERT_AGE_SECONDS`)
- Paper trade generation from parsed alerts

#### `app/slack_wnba.py` — WNBA PW Message Parser
Specialized parser for NBAMonitor's "Predicted Winner" Slack messages. Extracts:
- Predicted winner team name
- Win probability percentage
- Consensus direction
- Current quarter and game state
- Edge value and implied probability
- Moneyline odds

Patches the base `slack_ingest` module's `_parse_alert` and `_paper_trade_from_alert` at import time.

#### `app/pw_export_ingest.py` — Direct PW Feed Poller
Polls the NBAMonitor's `/api/pw-export` endpoint via the Tailscale VPN. Architecture:
- **`_tailnet_https_json()`** — HTTP CONNECT through Tailscale's outbound proxy with TLS. Prefers MagicDNS hostname resolution with peer-IP fallback.
- **`install()`** — sets up a background polling thread
- **Bootstrap safety**: first poll sets the cursor only, never executes historical calls
- **Deduplication**: content-hash identity via `_identity()`
- Polls every `PW_EXPORT_POLL_SECONDS` (default 8s)

#### `app/nfl_capper_ingest.py` — NFL Capper Polling
Polls the Telegram-ChatGPT bridge for NFL picks from professional cappers:
- Sources: SLAM - All Access, The Syndicate
- Parses pick messages for team, odds, unit sizing
- 32-team NFL alias dictionary for fuzzy matching
- Converts American odds to implied probability
- Polls every `NFL_CAPPER_POLL_SECONDS` (default 15s)

#### `app/cfb_capper_preview.py` — CFB Capper Preview
Mirrors the NFL capper architecture for college football, but in **preview/validation mode only** — picks are parsed, validated, and logged but never queued for real execution. Useful for validating the pipeline before enabling live CFB trading.

### Research Modules

These modules are installed as plugins via `.install()` and add research API endpoints.

#### `app/pw_research_sync.py` — Research Data Sync
Synchronizes PW call data from the NBAMonitor via Tailnet for local research analysis. Shares the same `_tailnet_https_json()` transport as `pw_export_ingest.py`.

#### `app/pw_market_research.py` — Market Depth Research
Analyzes Polymarket order book depth, liquidity, and market structure for WNBA PW markets.

#### `app/pw_scalping_research.py` — Scalping Analysis
Identifies scalping opportunities based on price movement patterns and spread behavior.

#### `app/pw_spread_capture.py` — Forward Spread Capture
Real-time forward-looking spread sampling. Captures bid/ask spread snapshots every `PW_SPREAD_CAPTURE_SAMPLE_SECONDS` (default 1s) over a configurable window to analyze execution quality.

#### `app/pw_game_reconstruction.py` — Game Timeline Reconstruction
Reconstructs complete game timelines from PW call data for post-game analysis.

#### `app/pw_spread_backtest.py` — Spread Strategy Backtesting
Historical backtesting of spread-based entry strategies against recorded PW call and market data.

#### `app/wnba_pw_history_v7.py` through `v10` — PW Alert Database
SQLite-backed storage of all WNBA Predicted Winner alerts with grading (win/loss), P&L calculation, and historical summary statistics. Includes backfill from seed data and JSONL imports.

#### `app/wnba_pw_filters_v11.py` — PW Strategy Filters
Configurable filters for PW call selection:
- Quarter filter (e.g., Q4 only)
- Venue filter (home/away)
- Moneyline range (e.g., +100 to +399)
- Minimum edge (percentage points)
- Minimum expected value
- One-per-game-per-side dedup

#### `app/wnba_pw_strategy_test_v12.py` — Strategy Testing
Live strategy testing with configurable filter combinations (Q3 away, away dog, plus money, home dog). Makes execution decisions based on the current filter configuration.

### Termux Executor

#### `scripts/termux_executor.py` / `termux_executor_v2.py` — On-Device Executor
Runs on an Android phone in Termux. Responsibilities:
- Polls the Railway bot's executor queue for pending orders
- Signs transactions locally using the Polymarket wallet private key
- Submits limit orders to the Polymarket CLOB via `SecureClient`
- Monitors for fills and reports results back to the Railway bot
- Emergency max USDC backstop (`EXECUTOR_EMERGENCY_MAX_USDC`)
- Persistent journal for audit trail

#### `app/termux_executor_dashboard.py` / `v2` — Executor Dashboard
Server-side executor queue management:
- Executor pairing via shared secret (TOTP-style)
- Queue item lifecycle (pending → claimed → executed → confirmed)
- TTL-based expiry for stale queue items
- Executor heartbeat and status monitoring

#### `scripts/start_termux_executor.sh` — Executor Startup Script
Shell script for launching the Termux executor with environment setup.

### Infrastructure

#### `app/structured_logging.py` — JSON Logging
Production-grade structured logging with JSON output, configurable log levels, and trading-context fields.

#### `start.sh` — Container Entrypoint
Boots the Docker container:
1. Starts `tailscaled` in userspace networking mode with SOCKS5/HTTP proxy at `127.0.0.1:1055`
2. Authenticates to the Tailnet using `TS_AUTHKEY`
3. Waits for the Tailscale socket to become ready (up to 20s)
4. Launches Uvicorn with the full module chain entrypoint

#### `Dockerfile` — Container Image
Multi-stage build:
- Stage 1: Copies Tailscale binaries from `tailscale/tailscale:stable`
- Stage 2: Python 3.12 slim with the app, dependencies, and Tailscale binaries

#### `railway.json` — Railway Deployment Config
Configures the Railway platform:
- Health check at `/health` with 30s timeout
- Restart on failure (up to 10 retries)
- Nixpacks builder

---

## Related Systems

### NBAMonitor (upstream)

A separate project that provides the WNBA/NBA Predicted Winner signal pipeline. **Not part of this repository** — a read-only vendored snapshot is kept in `research/upstream_nbamonitor/` for reference (pinned to commit `7fc3285`).

**What NBAMonitor does:**
- Polls ESPN APIs every 30 seconds for live game data (scores, quarter, clock)
- Evaluates configurable conditions (score differential, momentum, quarter thresholds)
- Generates Predicted Winner (PW) calls when conditions trigger
- Sends Slack alerts with structured PW call details
- Serves `/api/pw-export` — a JSON API that returns all PW calls since a given timestamp
- Runs on a home server accessible via the Tailscale VPN

**How this bot connects to NBAMonitor:**
1. **Slack push path**: NBAMonitor → Slack → Slack Events API → bot's `/slack/events`
2. **Direct pull path**: Bot → Tailscale VPN → NBAMonitor's `/api/pw-export` (HTTPS)

### NRLMonitor (research only)

A vendored snapshot of an NRL (Australian rugby league) monitor in `research/upstream_nrlmonitor/`. Currently research-only — no active NRL signal ingestion exists in the bot. Provides reference architecture for potential future league expansion.

### Telegram-ChatGPT Bridge

An external service that sanitizes and serves Telegram messages over HTTP. The NFL and CFB capper ingest modules poll this bridge for new picks. The bridge is hosted separately on Railway.

---

## Processing Flow Diagrams

### WNBA Predicted Winner Flow

```
NBAMonitor detects PW condition (ESPN live data)
         │
         ├──────────────────────────────────────┐
         │                                      │
         ▼                                      ▼
  Slack alert sent                     /api/pw-export updated
         │                                      │
         ▼                                      ▼
  Slack Events API push              pw_export_ingest.py polls
  (~1-3s latency)                    every 8s (~4-8s latency)
         │                                      │
         ▼                                      ▼
  slack_ingest.py                    _tailnet_https_json()
  • HMAC verify                      • HTTP CONNECT via proxy
  • Channel filter                   • TLS to NBAMonitor
  • Age check                        • MagicDNS preferred
         │                                      │
         ▼                                      ▼
  slack_wnba.py                      Content-hash dedup
  • Parse PW fields                  (_identity())
  • Extract odds/edge                       │
         │                                  │
         ▼                                  │
  _existing_pw_signal() ◀───────────────────┘
  (cross-path dedup)
         │
         ▼ (first arrival wins)
  Strategy filter (v11/v12)
  • Quarter: Q3/Q4
  • Venue: home/away
  • ML range: +100 to +399
  • Edge: ≥8pp
  • EV: ≥8%
         │
         ▼ (pass)
  core._execute_limit()
  or paper trade
```

### NFL/CFB Capper Flow

```
Telegram capper posts pick
  (SLAM / Syndicate channel)
         │
         ▼
  Telegram-ChatGPT Bridge
  (sanitizes, serves over HTTP)
         │
         ▼
  nfl_capper_ingest.py polls every 15s
  • Source filtering (SLAM, Syndicate)
  • Pick age check (≤180s)
  • Team name parsing (32-team alias dict)
  • Odds extraction (American format)
  • Unit sizing (1u = $10 default)
         │
         ▼
  Polymarket market search
  • Match team to prediction market
  • Verify market is open
         │
         ▼
  Risk checks → Executor queue
  (or paper trade if LIVE_TRADING=false)
```

### Termux Executor Flow

```
Railway bot queues order
  (termux_executor_queue.json)
         │
         ▼
  Termux executor polls queue
  via HTTPS (heartbeat + claim)
         │
         ▼
  Validate order params
  • Emergency max USDC check
  • Market still open check
         │
         ▼
  SecureClient.create()
  • Private key (local)
  • Deposit wallet
         │
         ▼
  Place limit order on CLOB
         │
         ▼
  Wait for fill (6-8s)
         │
         ▼
  Report result to Railway
  • Fill price, shares, fees
  • Or timeout/error
         │
         ▼
  Railway updates execution record
  Dashboard reflects new position
```

### Watch Loop Flow

```
Signal submitted to /auto/watch
  or /auto/execute
         │
         ▼
  Stored in watchlist.json
  with max_price + expires_at
         │
         ▼
  ┌─────────────────────────┐
  │  _watch_loop() runs     │ ◀──── every AUTO_POLL_SECONDS (30s)
  │  every poll interval    │
  └────────┬────────────────┘
           │
           ▼ (for each watch item)
  ┌──────────────────┐
  │ Expired?         │──── yes ──▶ Remove from queue
  └────────┬─────────┘
           │ no
           ▼
  ┌──────────────────┐
  │ Already executed?│──── yes ──▶ Skip (dedup by signal_id)
  └────────┬─────────┘
           │ no
           ▼
  ┌──────────────────┐
  │ Quote market     │──── spread too wide ──▶ Wait for next cycle
  │ (best ask price) │
  └────────┬─────────┘
           │
           ▼
  ┌──────────────────┐
  │ ask ≤ max_price? │──── no ──▶ Wait for next cycle
  └────────┬─────────┘
           │ yes
           ▼
  Risk checks → Execute
```

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and configure:

#### Safety Gates

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVE_TRADING` | `false` | Master switch for real orders. Must be `true` for any real USDC to move. |
| `AUTO_TRADING` | `false` | Enables automatic execution without manual approval. Requires `LIVE_TRADING=true`. |
| `BLOCK_POLITICAL_AUTO` | `true` | Prevents automatic execution on political markets. |

**Triple-gate safety**: A real automatic order requires all three: `LIVE_TRADING=true` + `AUTO_TRADING=true` + the signal passes risk checks. Dry-run is the default.

#### Risk Limits

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_TRADE_USDC` | `100` | Maximum USDC per manual trade |
| `MAX_AUTO_TRADE_USDC` | `25` | Maximum USDC per automatic trade |
| `MAX_DAILY_BUDGET_USDC` | `100` | Maximum total USDC spent per calendar day (in `TRADING_TIMEZONE`) |
| `MAX_PRICE` | `0.95` | Maximum price (probability) the bot will pay |
| `MAX_SPREAD` | `0.08` | Maximum bid-ask spread allowed for execution |
| `AUTO_POLL_SECONDS` | `30` | Watch loop polling interval (minimum 10s) |
| `TRADING_TIMEZONE` | `Asia/Kuala_Lumpur` | Timezone for daily budget reset |

#### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `SIGNAL_SECRET` | *(required)* | Long random secret for `/auto/*` API endpoints (in `X-Signal-Secret` header) |
| `DASHBOARD_USER` | `admin` | Dashboard HTTP Basic username |
| `DASHBOARD_PASSWORD` | *(required)* | Dashboard HTTP Basic password |
| `POLYMARKET_PRIVATE_KEY` | *(empty)* | Wallet private key for live orders. **Never commit.** |
| `POLYMARKET_DEPOSIT_WALLET` | *(empty)* | Polymarket deposit wallet address |

#### Slack Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_EVENTS_ENABLED` | `false` | Enable Slack Events webhook ingestion |
| `SLACK_SIGNING_SECRET` | *(empty)* | Slack app signing secret for HMAC verification |
| `SLACK_CHANNEL_ID` | *(empty)* | Restrict ingestion to a specific Slack channel |
| `SLACK_AUTO_PAPER` | `true` | Auto-generate paper trades from Slack alerts |
| `SLACK_MAX_ALERT_AGE_SECONDS` | `180` | Reject Slack events older than this |

#### PW Export (Direct Feed)

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_EXPORT_INGEST_ENABLED` | `true` | Enable direct PW feed polling |
| `PW_WNBA_EXPORT_URL` | *(Tailnet URL)* | NBAMonitor's `/api/pw-export` endpoint |
| `PW_EXPORT_POLL_SECONDS` | `8` | Polling interval for direct feed |
| `PW_TAILNET_PEER` | *(peer IP)* | Fallback Tailscale peer IP address |

#### NFL/CFB Capper

| Variable | Default | Description |
|----------|---------|-------------|
| `NFL_CAPPER_ENABLED` | `true` | Enable NFL capper polling |
| `NFL_CAPPER_UNIT_USDC` | `10` | USDC value of 1 unit |
| `NFL_CAPPER_POLL_SECONDS` | `15` | Polling interval |
| `NFL_CAPPER_MAX_PICK_AGE_SECONDS` | `180` | Max age of a valid pick |
| `NFL_CAPPER_BRIDGE_URL` | *(bridge URL)* | Telegram-ChatGPT bridge endpoint |
| `CFB_CAPPER_ENABLED` | `true` | Enable CFB capper preview |

#### Tailscale

| Variable | Default | Description |
|----------|---------|-------------|
| `TS_AUTHKEY` | *(required)* | Tailscale authentication key |
| `TS_HOSTNAME` | `railway-polymarket-bot` | Tailscale node hostname |
| `TS_PROXY_ADDR` | `127.0.0.1:1055` | SOCKS5/HTTP proxy listen address |

#### Termux Executor

| Variable | Default | Description |
|----------|---------|-------------|
| `EXECUTOR_BUY_TTL_SECONDS` | `180` | Max time a BUY can wait for Termux executor |
| `EXECUTOR_EMERGENCY_MAX_USDC` | `500` | Hard emergency backstop per order |
| `REMOTE_EXECUTION_ENABLED` | `true` | Enable remote executor queue |

#### Research

| Variable | Default | Description |
|----------|---------|-------------|
| `PW_SPREAD_CAPTURE_ENABLED` | `true` | Enable forward spread capture |
| `PW_SPREAD_CAPTURE_SAMPLE_SECONDS` | `1` | Spread sampling interval |
| `PW_SPREAD_CAPTURE_WINDOW_SECONDS` | `300` | Capture window duration |
| `PW_STRATEGY_TEST_ENABLED` | `true` | Enable strategy testing |
| `PW_FILTERS_ENABLED` | `true` | Enable PW call filters |

#### Logging

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Structured logging level: DEBUG, INFO, WARNING, ERROR, CRITICAL |

### Safety Gates

The bot implements defense-in-depth with multiple independent safety layers:

```
Layer 1: LIVE_TRADING=false (default)
  └─ No real USDC can move. All orders are paper/simulated.

Layer 2: AUTO_TRADING=false (default)
  └─ Even if live, automatic signals require manual approval.

Layer 3: BLOCK_POLITICAL_AUTO=true (default)
  └─ Political markets are excluded from automation.

Layer 4: Per-trade caps
  └─ MAX_AUTO_TRADE_USDC (25), MAX_TRADE_USDC (100)

Layer 5: Daily budget
  └─ MAX_DAILY_BUDGET_USDC (100) resets at midnight in TRADING_TIMEZONE

Layer 6: Market quality
  └─ MAX_PRICE (0.95), MAX_SPREAD (0.08)

Layer 7: Geoblock
  └─ Checks Polymarket eligibility before every execution

Layer 8: Executor emergency cap
  └─ EXECUTOR_EMERGENCY_MAX_USDC (500) — hard limit on Termux
```

---

## Installation

### Termux (Android)

Do **not** run from Android shared storage (`~/storage/downloads`). Copy into the Termux home directory.

```bash
pkg update
pkg install python clang rust make pkg-config libffi openssl unzip -y
cd ~
unzip polymarket-auto-bot.zip
cd polymarket-auto-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your configuration
```

> Do not run `pip install --upgrade pip` in Termux; Termux warns that replacing its packaged pip can break the Python package.

Start the server:

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Start the executor:

```bash
python scripts/termux_executor.py
```

### Railway (Cloud)

The Railway deployment uses Docker with the provided `Dockerfile` and `start.sh`:

1. Set all required environment variables in the Railway dashboard
2. Ensure `TS_AUTHKEY` is set for Tailscale VPN connectivity
3. The container will:
   - Boot Tailscale in userspace mode
   - Authenticate to the Tailnet
   - Start Uvicorn with the full module chain

Health check: `GET /health` (30s timeout, restart on failure, max 10 retries).

---

## API Endpoints

### Signal Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/auto/watch` | Signal Secret | Submit a signal to the watch-until-price queue |
| `GET` | `/auto/watch` | Signal Secret | List current watch queue |
| `POST` | `/auto/execute` | Signal Secret | One-shot execution (immediate or WAITING_FOR_PRICE) |
| `POST` | `/slack/events` | Slack HMAC | Slack Events API webhook receiver |

### Trading Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/prepare` | Dashboard | Prepare a manual trade for approval |
| `POST` | `/approve/{trade_id}` | Dashboard | Approve a prepared trade |
| `POST` | `/api/live/buy` | Dashboard | Live test BUY |
| `POST` | `/api/live/sell/{trade_id}` | Dashboard | Live test SELL |

### Dashboard & Status

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | Dashboard | Main dashboard UI |
| `GET` | `/health` | None | Health check (returns 503 if watch loop stale) |
| `GET` | `/api/snapshot` | Dashboard | Dashboard data snapshot |
| `GET/PUT` | `/api/settings` | Dashboard | Trading parameter management |
| `GET/PUT` | `/api/slack-mode` | Dashboard | Slack trading mode (paper/live) |

### Executor Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/executor/queue` | Executor Token | Poll for pending orders |
| `POST` | `/api/executor/claim/{id}` | Executor Token | Claim a queue item |
| `POST` | `/api/executor/result/{id}` | Executor Token | Report execution result |
| `POST` | `/api/executor/heartbeat` | Executor Token | Executor status heartbeat |

### Research Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/research/sync` | Dashboard | PW data sync status |
| `GET` | `/api/research/scalping` | Dashboard | Scalping opportunity analysis |
| `GET` | `/api/research/market` | Dashboard | Market depth analysis |
| `GET` | `/api/research/spread-capture` | Dashboard | Forward spread samples |
| `GET` | `/api/research/game-reconstruction` | Dashboard | Game timeline data |

---

## Glossary

| Term | Definition |
|------|-----------|
| **CLOB** | Central Limit Order Book — Polymarket's order matching system where limit orders are placed |
| **PW / Predicted Winner** | A signal generated by NBAMonitor when in-game conditions predict a likely winner |
| **Paper Trade** | A simulated trade recorded for tracking but not submitted to Polymarket |
| **Live Trade** | A real trade submitted to Polymarket's CLOB using USDC |
| **Watch Queue** | A list of signals being monitored until their target price is reached or they expire |
| **Signal** | A trading recommendation from any source (PW call, capper pick, manual) |
| **Executor** | The Termux-based component that holds wallet keys and submits signed orders |
| **Tailnet** | The Tailscale VPN network connecting the Railway bot to the NBAMonitor |
| **MagicDNS** | Tailscale's DNS system that resolves node hostnames within the Tailnet |
| **Edge** | The difference between model-estimated probability and market-implied probability (in percentage points) |
| **EV** | Expected Value — the expected profit/loss of a bet given the edge and odds |
| **Moneyline (ML)** | American odds format (e.g., +150 means $100 bet wins $150; -200 means $200 bet wins $100) |
| **Spread** | The difference between the best bid and best ask price in the order book |
| **Geoblock** | Polymarket's geographic eligibility check — the bot verifies compliance before every order |
| **Triple Gate** | The three independent switches that must all be enabled for automatic live trading |
| **Capper** | A professional sports handicapper who publishes picks (e.g., SLAM, The Syndicate) |
| **Bridge** | The Telegram-to-HTTP service that sanitizes and serves capper messages |
| **Dedup** | Deduplication — preventing the same signal from executing twice across multiple ingestion paths |
| **USDC** | USD Coin — the stablecoin used for Polymarket trading |
| **Reconciliation** | Comparing local trade records against on-chain Polymarket state to detect fills and settlements |
| **Backfill** | Importing historical PW call data into the SQLite database for research |
| **Quarter Filter** | Strategy filter that only trades PW calls made during specific game quarters |

---

## Index

### By File

| File | Section |
|------|---------|
| `app/main.py` | [Core Trading Engine](#core-trading-engine) |
| `app/dashboard.py` | [Dashboard Layer](#dashboard-layer) |
| `app/slack_ingest.py` | [Signal Ingestion — Slack](#appslack_ingestpy--slack-events-ingestion) |
| `app/slack_wnba.py` | [Signal Ingestion — WNBA](#appslack_wnbapy--wnba-pw-message-parser) |
| `app/pw_export_ingest.py` | [Signal Ingestion — Direct Feed](#apppw_export_ingestpy--direct-pw-feed-poller) |
| `app/nfl_capper_ingest.py` | [Signal Ingestion — NFL](#appnfl_capper_ingestpy--nfl-capper-polling) |
| `app/cfb_capper_preview.py` | [Signal Ingestion — CFB](#appcfb_capper_previewpy--cfb-capper-preview) |
| `app/live_trading.py` | [Live Trading](#applive_tradingpy--live-test-endpoints) |
| `app/wallet_dashboard.py` | [Wallet Display](#appwallet_dashboardpy--wallet-display) |
| `app/termux_executor_dashboard.py` | [Executor Dashboard](#apptermux_executor_dashboardpy--v2--executor-dashboard) |
| `app/structured_logging.py` | [Logging](#appstructured_loggingpy--json-structured-logging) |
| `app/pw_research_sync.py` | [Research — Sync](#apppw_research_syncpy--research-data-sync) |
| `app/pw_market_research.py` | [Research — Market](#apppw_market_researchpy--market-depth-research) |
| `app/pw_scalping_research.py` | [Research — Scalping](#apppw_scalping_researchpy--scalping-analysis) |
| `app/pw_spread_capture.py` | [Research — Spread](#apppw_spread_capturepy--forward-spread-capture) |
| `app/pw_game_reconstruction.py` | [Research — Games](#apppw_game_reconstructionpy--game-timeline-reconstruction) |
| `app/pw_spread_backtest.py` | [Research — Backtest](#apppw_spread_backtestpy--spread-strategy-backtesting) |
| `app/wnba_pw_history_v7-v10.py` | [Research — PW DB](#appwnba_pw_history_v7py-through-v10--pw-alert-database) |
| `app/wnba_pw_filters_v11.py` | [Research — Filters](#appwnba_pw_filters_v11py--pw-strategy-filters) |
| `app/wnba_pw_strategy_test_v12.py` | [Research — Strategy](#appwnba_pw_strategy_test_v12py--strategy-testing) |
| `scripts/termux_executor.py` | [Termux Executor](#termux-executor) |
| `start.sh` | [Container Entrypoint](#startsh--container-entrypoint) |
| `Dockerfile` | [Container Image](#dockerfile--container-image) |
| `railway.json` | [Railway Config](#railwayjson--railway-deployment-config) |
| `research/upstream_nbamonitor/` | [NBAMonitor](#nbamonitor-upstream) |
| `research/upstream_nrlmonitor/` | [NRLMonitor](#nrlmonitor-research-only) |

### By Concept

| Concept | Relevant Sections |
|---------|------------------|
| Order execution | [Order Execution Flow](#order-execution-flow), [Core Trading Engine](#core-trading-engine) |
| Risk management | [Safety Gates](#safety-gates), [Configuration](#configuration) |
| Signal ingestion | [Signal Ingestion Paths](#signal-ingestion-paths), [Signal Ingestion](#signal-ingestion) |
| WNBA trading | [WNBA PW Flow](#wnba-predicted-winner-flow), [PW Filters](#appwnba_pw_filters_v11py--pw-strategy-filters) |
| NFL/CFB trading | [NFL/CFB Capper Flow](#nflcfb-capper-flow) |
| Deployment | [Deployment Topology](#deployment-topology), [Installation](#installation) |
| Tailscale VPN | [Deployment Topology](#deployment-topology), [Direct Feed](#apppw_export_ingestpy--direct-pw-feed-poller) |
| Dashboard | [Dashboard Layer](#dashboard-layer) |
| Research | [Research Modules](#research-modules) |
| Termux | [Termux Executor](#termux-executor), [Termux Executor Flow](#termux-executor-flow) |

---

## Engineering History

Full implementation notes, deployment history, safeguards, incidents/fixes, and the complete commit index are maintained in [DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md).

Future code changes should update that file with the objective, files changed, behavior/data/configuration impact, verification performed, deployment result, and any outstanding work.
