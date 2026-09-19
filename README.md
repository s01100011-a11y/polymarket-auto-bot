# Polymarket Auto Bot (v0.2)

Termux-friendly Polymarket trading service using the current official `polymarket-client` Python SDK.

## Engineering history

Full implementation notes, deployment history, safeguards, incidents/fixes, and the complete commit index are maintained in [DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md).

Future code changes should update that file with the objective, files changed, behavior/data/configuration impact, verification performed, deployment result, and any outstanding work.

## What changed in v0.2

- Keeps the v0.1 approval flow.
- Adds authenticated **automatic signal execution**.
- Adds a **watch-until-price** queue: submit a signal once and the bot rechecks the live market every 30 seconds until the price reaches your limit or the signal expires.
- Rechecks Polymarket geographic eligibility and live order-book conditions before every execution.
- Enforces a separate automatic per-trade cap, daily budget cap, maximum price and maximum spread.
- Deduplicates by `signal_id` so the same signal cannot intentionally execute twice.
- Defaults to dry-run: no real order is possible unless **both** `LIVE_TRADING=true` and `AUTO_TRADING=true`.
- Automatic political execution is blocked by default. Start with sports/non-political signals.

## Termux install

Do **not** run the project from Android shared storage (`~/storage/downloads`). Copy/unzip it into the Termux home directory first.

```bash
pkg update
pkg install python clang rust make pkg-config libffi openssl unzip -y
cd ~
unzip polymarket-auto-bot-v0.2.zip
cd polymarket-auto-bot
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Do not run `pip install --upgrade pip` in Termux; Termux warns that replacing its packaged pip can break the Python package.

## Configure dry-run

Generate a local signal secret:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Put the output into `.env` as `SIGNAL_SECRET=...`.

Leave these as:

```text
LIVE_TRADING=false
AUTO_TRADING=false
```

## Start

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Open:

```text
http://127.0.0.1:8787
http://127.0.0.1:8787/docs
```

## Automatic signal format

`POST /auto/watch` stores a signal and keeps checking it until `expires_at`.

Example:

```json
{
  "signal_id": "nfl-bills-lions-over-001",
  "market_url": "https://polymarket.com/event/example",
  "outcome": "YES",
  "max_price": 0.55,
  "budget_usdc": 10,
  "category": "sports",
  "expires_at": "2026-09-18T00:00:00Z",
  "note": "automated sports signal"
}
```

Send the signal secret in the `X-Signal-Secret` HTTP header.

`POST /auto/execute` is the one-shot version: it executes only if the best ask is already at or below `max_price`; otherwise it returns `WAITING_FOR_PRICE`.

## Dry-run curl test

Replace `YOUR_SECRET` and the market URL:

```bash
curl -X POST http://127.0.0.1:8787/auto/watch \
  -H 'Content-Type: application/json' \
  -H 'X-Signal-Secret: YOUR_SECRET' \
  -d '{
    "signal_id":"test-001",
    "market_url":"https://polymarket.com/event/REPLACE_ME",
    "outcome":"YES",
    "max_price":0.50,
    "budget_usdc":5,
    "category":"sports",
    "expires_at":"2026-12-31T00:00:00Z",
    "note":"dry run"
  }'
```

Check the watch queue:

```bash
curl http://127.0.0.1:8787/auto/watch -H 'X-Signal-Secret: YOUR_SECRET'
```

## Guardrails

`.env` defaults:

```text
MAX_AUTO_TRADE_USDC=25
MAX_DAILY_BUDGET_USDC=100
MAX_PRICE=0.95
MAX_SPREAD=0.08
AUTO_POLL_SECONDS=30
BLOCK_POLITICAL_AUTO=true
```

The bot uses limit orders only. It will not intentionally pay above the signal's `max_price`.

## Real trading

Do not enable live mode until the dry-run flow has been tested with real market URLs and the logs look correct.

When ready, live execution requires local wallet credentials supported by the official Polymarket SDK. Never paste private keys into ChatGPT, screenshots, Telegram, or GitHub. Use a dedicated low-balance trading wallet rather than a primary wallet.

Before every quote/execution, the bot checks Polymarket's geoblock endpoint. Do not use a VPN/proxy or another server to bypass geographic restrictions.

## Connecting this chat later

This service now has the receiving side we need. The missing piece is a secure bridge from ChatGPT to your phone/server. A custom ChatGPT plugin can call `/auto/watch` with a structured sports signal. Because a phone on `127.0.0.1` is not reachable from the public internet, that bridge will need a secure HTTPS endpoint or a small cloud relay. Do not expose port 8787 directly to the internet without TLS/authentication.
