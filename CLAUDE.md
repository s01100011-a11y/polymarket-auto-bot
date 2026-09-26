# CLAUDE.md

This file provides guidance to Claude Code when working with this repository.

## What This Project Does

Polymarket Auto Bot is a FastAPI service that automates sports prediction-market trading on Polymarket. It ingests live game signals from upstream monitors (NBAMonitor) and Telegram cappers, matches them to Polymarket markets, applies risk controls, and executes limit orders via the Polymarket CLOB.

**GitHub:** https://github.com/s01100011-a11y/polymarket-auto-bot

## Supported Sports

| Sport | Signal Source | Module | Status |
|-------|-------------|--------|--------|
| WNBA | NBAMonitor PW calls (Slack + Tailnet) | `slack_ingest.py`, `pw_export_ingest.py` | Live auto-trading |
| NFL | Telegram cappers (SLAM, Syndicate) | `nfl_capper_ingest.py` | Live auto-trading |
| CFB | Telegram cappers (SLAM, Syndicate) | `cfb_capper_preview.py` | Live auto-trading (via `CFB_CAPPER_LIVE_ENABLED`) |
| NBA | NBAMonitor PW calls (Tailnet) | `pw_export_ingest.py` | Live auto-trading (via `PW_NBA_EXPORT_URL`) |

## Architecture

### Module Chain Pattern

The app assembles via linear module imports — each module does `app = base.app`:

```
main.py → dashboard.py → live_trading.py → wnba_pw_strategy_test_v12.py → wnba_pw_research_v13.py
```

Research/ingest modules use `.install()` to add routes and background threads.

### Signal Paths

- **WNBA/NBA**: Slack Events push (~1-3s) + PW export Tailnet poll (~4-8s) with content-hash dedup
- **NFL/CFB**: Telegram bridge poll → parse picks → match Polymarket markets → enqueue BUY/PREVIEW

### Triple-Gate Safety

All auto-trading requires three gates enabled: `LIVE_TRADING=true` + `AUTO_TRADING=true` + sport-specific enable flag.

### Execution Flow

Railway bot enqueues orders → Termux executor (Android) polls queue, signs with local wallet, submits to Polymarket CLOB.

## Running the Project

```bash
pip install -r requirements.txt
uvicorn app.wnba_pw_research_v13:app --host 0.0.0.0 --port 8000
```

Entry point: `start.sh` (confirm for production)

## Key Files

| File | Role |
|------|------|
| `app/main.py` | Core trading engine — risk checks, budget tracking, market matching |
| `app/dashboard.py` | Web dashboard — dark-themed UI, P&L tracking, mode controls |
| `app/live_trading.py` | Polymarket SDK integration — order placement, position management |
| `app/slack_ingest.py` | Slack Events webhook — WNBA/NBA signal ingestion |
| `app/pw_export_ingest.py` | PW export Tailnet poller — WNBA + NBA feeds |
| `app/nfl_capper_ingest.py` | NFL Telegram capper automation |
| `app/cfb_capper_preview.py` | CFB Telegram capper — preview or live mode |
| `app/wnba_pw_strategy_test_v12.py` | Strategy engine — watch-until-price queue, approval flow |
| `app/wnba_pw_research_v13.py` | Research module orchestrator |
| `app/dashboard_live_control_v4.py` | Live trading controls — executor health, mode toggles |
| `app/wallet_dashboard.py` | Wallet balance display |
| `.env.example` | All configuration variables with descriptions |

## Key Environment Variables

- `LIVE_TRADING` / `AUTO_TRADING` — master trading gates
- `CFB_CAPPER_LIVE_ENABLED` — promote CFB from preview to live
- `PW_NBA_EXPORT_URL` — enable NBA feed polling
- `PW_WNBA_EXPORT_URL` — WNBA Tailnet feed URL
- `NFL_CAPPER_ENABLED` / `CFB_CAPPER_ENABLED` — sport-specific toggles
- `MAX_AUTO_TRADE_USDC` / `MAX_DAILY_BUDGET_USDC` — risk limits

## Behavioral Guidelines

### 1. Think Before Coding
Don't assume. Surface tradeoffs. If uncertain, ask.

### 2. Simplicity First
Minimum code that solves the problem. No speculative features or abstractions for single-use code.

### 3. Surgical Changes
Touch only what you must. Don't "improve" adjacent code. Match existing style.

### 4. Change Documentation
Every change: update the GitHub issue, update `CHANGES.md`, commit and push.

### 5. Safety First
Never weaken trading safety gates. All new trading paths must respect the triple-gate pattern. Default new features to disabled/preview.
