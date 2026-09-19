from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from polymarket import PublicClient

from app import dashboard
from app import main as core

app = dashboard.app

SLACK_ALERTS_FILE = core.DATA_DIR / "slack_alerts.json"
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID", "").strip()
SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "").strip()
SLACK_EVENTS_ENABLED = os.getenv("SLACK_EVENTS_ENABLED", "false").lower() == "true"
SLACK_AUTO_PAPER = os.getenv("SLACK_AUTO_PAPER", "true").lower() == "true"
SLACK_REQUIRE_WNBA = os.getenv("SLACK_REQUIRE_WNBA", "true").lower() == "true"
SLACK_PAPER_BUDGET_USDC = Decimal(os.getenv("SLACK_PAPER_BUDGET_USDC", "10"))
SLACK_MAX_ALERT_AGE_SECONDS = max(30, int(os.getenv("SLACK_MAX_ALERT_AGE_SECONDS", "180")))

# Intentionally hard-coded. Slack ingestion cannot place a real order.
SLACK_PAPER_ONLY = True

WNBA_ALIASES: dict[str, tuple[str, ...]] = {
    "Atlanta Dream": ("atlanta dream", "dream", "atl"),
    "Chicago Sky": ("chicago sky", "sky", "chi"),
    "Connecticut Sun": ("connecticut sun", "sun", "conn"),
    "Dallas Wings": ("dallas wings", "wings", "dal"),
    "Golden State Valkyries": ("golden state valkyries", "valkyries", "gsv", "golden state"),
    "Indiana Fever": ("indiana fever", "fever", "ind"),
    "Las Vegas Aces": ("las vegas aces", "aces", "lva", "las vegas"),
    "Los Angeles Sparks": ("los angeles sparks", "sparks", "las", "la sparks"),
    "Minnesota Lynx": ("minnesota lynx", "lynx", "min"),
    "New York Liberty": ("new york liberty", "liberty", "nyl", "new york"),
    "Phoenix Mercury": ("phoenix mercury", "mercury", "phx"),
    "Portland Fire": ("portland fire", "fire", "por", "portland"),
    "Seattle Storm": ("seattle storm", "storm", "sea"),
    "Toronto Tempo": ("toronto tempo", "tempo", "tor", "toronto"),
    "Washington Mystics": ("washington mystics", "mystics", "was", "washington"),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold()).strip()


def _team_mentions(text: str) -> list[str]:
    norm = _norm_text(text)
    found: list[str] = []
    for team, aliases in WNBA_ALIASES.items():
        if any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", norm) for alias in aliases):
            found.append(team)
    return found


def _looks_wnba(text: str) -> bool:
    norm = _norm_text(text)
    return "wnba" in norm or bool(_team_mentions(text))


def _parse_alert(text: str) -> dict[str, Any]:
    norm = _norm_text(text)
    teams = _team_mentions(text)

    market_kind = "moneyline"
    selection: str | None = None
    line: Decimal | None = None

    total_match = re.search(r"\b(over|under|o|u)\s*([0-9]{2,3}(?:\.5)?)\b", norm)
    if total_match:
        market_kind = "total"
        selection = "Over" if total_match.group(1) in {"over", "o"} else "Under"
        line = Decimal(total_match.group(2))
    else:
        spread_match = re.search(r"(?:^|\s)([+\-]\d{1,2}(?:\.5)?)(?:\s|$)", norm)
        if spread_match and teams:
            market_kind = "spread"
            selection = teams[0]
            line = Decimal(spread_match.group(1))
        elif teams:
            market_kind = "moneyline"
            selection = teams[0]

    units = None
    unit_match = re.search(r"\b(\d+(?:\.\d+)?)\s*u\b", norm)
    if unit_match:
        units = Decimal(unit_match.group(1))

    return {
        "raw_text": text,
        "teams": teams,
        "market_kind": market_kind,
        "selection": selection,
        "line": str(line) if line is not None else None,
        "units": str(units) if units is not None else None,
    }


def _verify_slack_signature(raw_body: bytes, timestamp: str | None, signature: str | None) -> None:
    if not SLACK_SIGNING_SECRET:
        raise HTTPException(status_code=503, detail="SLACK_SIGNING_SECRET is not configured")
    if not timestamp or not signature:
        raise HTTPException(status_code=401, detail="Missing Slack signature headers")
    try:
        ts = int(timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid Slack timestamp") from exc
    if abs(int(time.time()) - ts) > 60 * 5:
        raise HTTPException(status_code=401, detail="Stale Slack request")
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), base, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(status_code=401, detail="Invalid Slack signature")


def _market_type_text(market: Any) -> str:
    try:
        return _norm_text(core._market_type(market))
    except Exception:
        return ""


def _market_text(market: Any) -> str:
    bits = [
        str(getattr(market, "question", "") or ""),
        str(getattr(market, "slug", "") or ""),
        _market_type_text(market),
    ]
    return _norm_text(" ".join(bits))


def _event_text(event: Any) -> str:
    return _norm_text(" ".join([
        str(getattr(event, "title", "") or ""),
        str(getattr(event, "slug", "") or ""),
    ]))


def _outcome_candidates(market: Any) -> list[tuple[str, Any]]:
    yes = market.outcomes.yes
    no = market.outcomes.no
    return [
        (str(getattr(yes, "label", "Yes")), yes),
        (str(getattr(no, "label", "No")), no),
    ]


def _select_outcome(market: Any, parsed: dict[str, Any]) -> tuple[str, Any] | None:
    selection = parsed.get("selection")
    line_text = parsed.get("line")
    market_kind = parsed.get("market_kind")
    mtext = _market_text(market)

    if market_kind == "total":
        if line_text and line_text not in mtext:
            return None
        wanted = _norm_text(str(selection or ""))
        for label, outcome in _outcome_candidates(market):
            if wanted and wanted in _norm_text(label):
                return label, outcome
        # Binary total markets often use YES/NO while the question says Over/Under.
        if wanted == "over" and "over" in mtext:
            return _outcome_candidates(market)[0]
        if wanted == "under" and "under" in mtext:
            return _outcome_candidates(market)[0]
        return None

    if market_kind == "spread":
        team = _norm_text(str(selection or ""))
        if line_text and line_text not in mtext and line_text.replace("+", "") not in mtext:
            return None
        for label, outcome in _outcome_candidates(market):
            lnorm = _norm_text(label)
            if team and (team in lnorm or any(a in lnorm for a in WNBA_ALIASES.get(str(selection), ()) )):
                return label, outcome
        # Some spread questions encode the team in the question with YES/NO outcomes.
        if team and team in mtext:
            return _outcome_candidates(market)[0]
        return None

    team = _norm_text(str(selection or ""))
    for label, outcome in _outcome_candidates(market):
        lnorm = _norm_text(label)
        aliases = WNBA_ALIASES.get(str(selection), ())
        if team and (team in lnorm or any(a in lnorm for a in aliases)):
            return label, outcome
    if team and (team in mtext or any(a in mtext for a in WNBA_ALIASES.get(str(selection), ()))):
        return _outcome_candidates(market)[0]
    return None


def _find_market(parsed: dict[str, Any]) -> tuple[Any, Any, str, Any]:
    teams = parsed.get("teams") or []
    if not teams:
        raise ValueError("No WNBA team found in alert")

    query = teams[0]
    candidates: list[Any] = []
    with PublicClient() as client:
        paginator = client.list_events(title_search=query, closed=False, page_size=20)
        page = paginator.first_page()
        for event in page.items:
            etext = _event_text(event)
            if not any(alias in etext for team in teams for alias in WNBA_ALIASES.get(team, ())):
                continue
            candidates.append(event)

        if not candidates:
            raise ValueError(f"No open Polymarket event found for {query}")

        kind = parsed.get("market_kind")
        kind_words = {
            "moneyline": ("moneyline", "winner", "win"),
            "spread": ("spread", "handicap"),
            "total": ("total", "over", "under"),
        }.get(kind, (kind,))

        ranked: list[tuple[int, Any, Any, str, Any]] = []
        for event in candidates:
            for market in getattr(event, "markets", ()) or ():
                mt = _market_type_text(market)
                mtext = _market_text(market)
                score = 0
                if any(word and word in mt for word in kind_words):
                    score += 6
                if any(word and word in mtext for word in kind_words):
                    score += 3
                outcome = _select_outcome(market, parsed)
                if outcome is None:
                    continue
                label, outcome_obj = outcome
                score += 6
                if getattr(getattr(market, "state", None), "accepting_orders", False):
                    score += 2
                ranked.append((score, event, market, label, outcome_obj))

        if not ranked:
            raise ValueError(f"Found WNBA event but no matching {kind} market")
        ranked.sort(key=lambda item: item[0], reverse=True)
        _, event, market, label, outcome_obj = ranked[0]
        return event, market, label, outcome_obj


def _paper_trade_from_alert(parsed: dict[str, Any], slack_event_id: str, slack_event: dict[str, Any]) -> dict[str, Any]:
    event, market, outcome_label, outcome_obj = _find_market(parsed)
    asset_id = getattr(outcome_obj, "token_id", None) or getattr(outcome_obj, "position_id", None)
    if not asset_id:
        raise ValueError("Matched market has no tradable asset id")

    with PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=str(asset_id), side="BUY")))
        midpoint = Decimal(str(client.get_midpoint(asset_id=str(asset_id))))
        spread = Decimal(str(client.get_spread(asset_id=str(asset_id))))

    if buy_price <= 0 or buy_price >= 1:
        raise ValueError(f"Invalid current buy price {buy_price}")
    if buy_price > core.MAX_PRICE:
        raise ValueError(f"Current buy price {buy_price} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise ValueError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    budget = min(SLACK_PAPER_BUDGET_USDC, core.MAX_AUTO_TRADE_USDC)
    shares = (budget / buy_price).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    record_id = f"slack-{slack_event_id[:18]}"
    market_url = f"https://polymarket.com/event/{getattr(event, 'slug', '')}" if getattr(event, "slug", None) else None

    record = {
        "id": record_id,
        "status": "PAPER_OPEN",
        "created_at": _now_iso(),
        "submitted_at": _now_iso(),
        "budget_usdc": str(budget),
        "auto": True,
        "source": "slack",
        "paper": True,
        "slack_event_id": slack_event_id,
        "slack_channel": slack_event.get("channel"),
        "slack_ts": slack_event.get("ts"),
        "intent": {
            "market_url": market_url,
            "outcome": outcome_label,
            "market_type": parsed.get("market_kind"),
            "max_price": str(buy_price),
            "budget_usdc": str(budget),
            "note": parsed.get("raw_text"),
        },
        "quote": {
            "market": str(getattr(market, "question", None) or getattr(event, "title", "WNBA market")),
            "market_type": core._market_type(market),
            "market_url": market_url,
            "requested_outcome": parsed.get("selection"),
            "resolved_outcome": outcome_label,
            "asset_id": str(asset_id),
            "limit_price": str(buy_price),
            "paper_entry_price": str(buy_price),
            "budget_usdc": str(budget),
            "shares": str(shares),
            "current_buy_price": str(buy_price),
            "midpoint": str(midpoint),
            "spread": str(spread),
        },
        "execution": {
            "placed": False,
            "paper": True,
            "reason": "Slack paper-trading mode: no real order was sent.",
        },
    }

    executions = core._load(core.EXECUTIONS_FILE)
    if record_id in executions:
        return executions[record_id]
    executions[record_id] = record
    core._save(core.EXECUTIONS_FILE, executions)
    return record


def _save_alert(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    alerts = core._load(SLACK_ALERTS_FILE)
    if event_id in alerts:
        return alerts[event_id]
    alerts[event_id] = payload
    core._save(SLACK_ALERTS_FILE, alerts)
    return payload


def _live_fill_snapshot(client: PublicClient, wallet: str, rec: dict[str, Any], asset_id: str) -> dict[str, Any] | None:
    """Recover this tracked BUY from Polymarket's executed-trade feed.

    The remote executor cancels any unfilled remainder within seconds, so fills
    should cluster tightly around the recorded completion time. We match the
    same wallet + outcome token + BUY side in a bounded window and accumulate
    up to the executor-reported filled share count.
    """
    if not wallet or not asset_id:
        return None
    target = dashboard._safe_decimal(rec.get("filled_shares") or (rec.get("quote") or {}).get("shares"))
    if target <= 0:
        return None
    raw_time = rec.get("submitted_at") or rec.get("created_at")
    if not raw_time:
        return None
    try:
        anchor = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    try:
        rows = []
        for trade in client.list_trades(
            user=wallet,
            side="BUY",
            start=int(anchor.timestamp()) - 180,
            end=int(anchor.timestamp()) + 180,
            page_size=100,
        ).iter_items():
            if str(getattr(trade, "asset_id", "")) != asset_id:
                continue
            rows.append(trade)
    except Exception:
        return None

    if not rows:
        return None
    rows.sort(key=lambda t: abs((getattr(t, "timestamp") - anchor).total_seconds()))
    remaining = target
    matched: list[tuple[Decimal, Decimal, str]] = []
    for trade in rows:
        size = dashboard._safe_decimal(getattr(trade, "size", None))
        price = dashboard._safe_decimal(getattr(trade, "price", None))
        if size <= 0 or price <= 0:
            continue
        use = min(size, remaining)
        matched.append((use, price, str(getattr(trade, "transaction_hash", "") or "")))
        remaining -= use
        if remaining <= Decimal("0.0001"):
            break

    matched_size = sum((x[0] for x in matched), Decimal("0"))
    if matched_size <= 0 or matched_size < target * Decimal("0.98"):
        return None
    notional = sum((size * price for size, price, _ in matched), Decimal("0"))
    avg = notional / matched_size
    return {
        "shares": matched_size,
        "avg_price": avg,
        "cost_usdc": notional,
        "transaction_hashes": list(dict.fromkeys(tx for _, _, tx in matched if tx)),
        "matched_at": anchor.isoformat(),
    }


# Include paper positions in the dashboard's current-trade/P&L calculation.
# For real live positions, prefer Polymarket's authoritative wallet-position
# cost basis and P/L instead of treating an order limit as the fill price.
def _estimate_pnl_with_paper(records: list[dict]) -> tuple[list[dict], Decimal]:
    current: list[dict] = []
    total = Decimal("0")
    active = [r for r in records if r.get("status") in {"ORDER_SUBMITTED", "PAPER_OPEN"}]
    if not active:
        return current, total

    try:
        client = PublicClient()
    except Exception:
        client = None

    wallet = os.getenv("POLYMARKET_DEPOSIT_WALLET", "").strip()
    positions_by_asset: dict[str, Any] = {}
    if client and wallet:
        try:
            positions_by_asset = {
                str(getattr(p, "asset_id", "")): p
                for p in client.list_positions(user=wallet, page_size=100).iter_items()
                if getattr(p, "asset_id", None)
            }
        except Exception:
            positions_by_asset = {}

    executions_changed = False
    try:
        for rec in active:
            q = rec.get("quote") or {}
            asset_id = str(q.get("asset_id") or "")
            is_paper = bool(rec.get("paper")) or rec.get("status") == "PAPER_OPEN"
            tracked_shares = dashboard._safe_decimal(rec.get("filled_shares") or q.get("shares"))
            shares = tracked_shares
            entry = dashboard._safe_decimal(q.get("paper_entry_price") or q.get("entry_price") or q.get("limit_price"))
            current_price = None
            cost_basis = None
            current_value = None
            pnl = None
            pnl_percent = None
            fill_txs: list[str] = []
            entry_source = "paper_snapshot" if is_paper else "recorded_limit_fallback"

            position = positions_by_asset.get(asset_id) if asset_id else None

            # For live trades, executed Polymarket trades are the primary source
            # of the actual entry price and cost. The limit is only a ceiling.
            if not is_paper and client and wallet and asset_id:
                fills = _live_fill_snapshot(client, wallet, rec, asset_id)
                if fills:
                    shares = fills["shares"]
                    entry = fills["avg_price"]
                    cost_basis = fills["cost_usdc"]
                    fill_txs = fills["transaction_hashes"]
                    entry_source = "polymarket_executed_trades"

                    # Persist the reconciled fill so sell/realized-P&L accounting
                    # and history use the same authoritative entry.
                    if q.get("entry_price") != str(entry) or rec.get("actual_cost_usdc") != str(cost_basis):
                        q["entry_price"] = str(entry)
                        q["fill_transaction_hashes"] = fill_txs
                        rec["quote"] = q
                        rec["actual_cost_usdc"] = str(cost_basis)
                        rec["budget_usdc"] = str(cost_basis.quantize(Decimal("0.01")))
                        rec["network_reconciled_at"] = datetime.now(timezone.utc).isoformat()
                        executions_changed = True

            # Use the wallet position for the live mark. If this bot opened from
            # zero and fill-level data was unavailable, position avg/cost is a
            # safe authoritative fallback for this tracked trade.
            if position is not None:
                pos_size = dashboard._safe_decimal(getattr(position, "current_size", None))
                pos_price = dashboard._safe_decimal(getattr(position, "current_price", None))
                if pos_price > 0:
                    current_price = pos_price

                pre_size = dashboard._safe_decimal(rec.get("pre_position_size"))
                if not is_paper and entry_source == "recorded_limit_fallback" and pre_size <= Decimal("0.0001") and pos_size > 0:
                    shares = pos_size
                    entry = dashboard._safe_decimal(getattr(position, "avg_price", None))
                    cost_basis = dashboard._safe_decimal(getattr(position, "entry_cost_usdc", None))
                    entry_source = "polymarket_position"
                    if entry > 0:
                        q["entry_price"] = str(entry)
                        rec["quote"] = q
                    if cost_basis > 0:
                        rec["actual_cost_usdc"] = str(cost_basis)
                        rec["budget_usdc"] = str(cost_basis.quantize(Decimal("0.01")))
                    rec["network_reconciled_at"] = datetime.now(timezone.utc).isoformat()
                    executions_changed = True

            # Current value and P/L are based on the tracked fill, not the limit.
            if current_price is not None and shares > 0 and entry > 0:
                if cost_basis is None or cost_basis <= 0:
                    cost_basis = entry * shares
                current_value = current_price * shares
                pnl = current_value - cost_basis
                pnl_percent = (pnl / cost_basis * Decimal("100")) if cost_basis > 0 else None
                total += pnl
            elif is_paper and client and asset_id and shares > 0 and entry > 0:
                try:
                    current_price = dashboard._safe_decimal(client.get_midpoint(asset_id=asset_id))
                    cost_basis = entry * shares
                    current_value = current_price * shares
                    pnl = current_value - cost_basis
                    pnl_percent = (pnl / cost_basis * Decimal("100")) if cost_basis > 0 else None
                    total += pnl
                except Exception:
                    pass

            display_cost = cost_basis
            if display_cost is None or display_cost <= 0:
                display_cost = dashboard._safe_decimal(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))

            current.append({
                "id": rec.get("id"),
                "market": q.get("market") or (rec.get("intent") or {}).get("market_url"),
                "market_url": q.get("market_url") or (rec.get("intent") or {}).get("market_url"),
                "outcome": q.get("resolved_outcome") or q.get("requested_outcome"),
                "entry_price": str(entry) if entry else None,
                "limit_price": q.get("limit_price"),
                "current_midpoint": str(current_price) if current_price is not None else None,
                "shares": str(shares) if shares else None,
                "budget_usdc": str(display_cost.quantize(Decimal("0.01"))) if display_cost is not None else None,
                "requested_budget_usdc": (rec.get("intent") or {}).get("budget_usdc"),
                "current_value_usdc": str(current_value.quantize(Decimal("0.01"))) if current_value is not None else None,
                "estimated_pnl": str(pnl.quantize(Decimal("0.01"))) if pnl is not None else None,
                "pnl_percent": str(pnl_percent.quantize(Decimal("0.01"))) if pnl_percent is not None else None,
                "entry_source": entry_source,
                "fill_transaction_hashes": fill_txs or q.get("fill_transaction_hashes") or [],
                "submitted_at": rec.get("submitted_at") or rec.get("created_at"),
                "order_id": (rec.get("execution") or {}).get("order_id"),
                "status": rec.get("status"),
                "source": rec.get("source"),
            })
    finally:
        if executions_changed:
            execution_map = core._load(core.EXECUTIONS_FILE)
            by_id = {str(r.get("id")): r for r in active if r.get("id")}
            execution_map.update(by_id)
            core._save(core.EXECUTIONS_FILE, execution_map)
        if client:
            try:
                client.close()
            except Exception:
                pass
    return current, total

dashboard._estimate_pnl = _estimate_pnl_with_paper
dashboard.DASHBOARD_HTML = dashboard.DASHBOARD_HTML.replace("Submitted live trades", "Current trades").replace("Live trades", "Current trades")


@app.get("/api/slack/status", dependencies=[dashboard.Depends(dashboard._auth)])
def slack_status():
    alerts = core._load(SLACK_ALERTS_FILE)
    executions = core._load(core.EXECUTIONS_FILE)
    paper = [r for r in executions.values() if r.get("source") == "slack" and r.get("paper")]
    return {
        "events_enabled": SLACK_EVENTS_ENABLED,
        "paper_only": SLACK_PAPER_ONLY,
        "auto_paper": SLACK_AUTO_PAPER,
        "require_wnba": SLACK_REQUIRE_WNBA,
        "channel_filter_configured": bool(SLACK_CHANNEL_ID),
        "signing_secret_configured": bool(SLACK_SIGNING_SECRET),
        "paper_budget_usdc": str(SLACK_PAPER_BUDGET_USDC),
        "alerts_received": len(alerts),
        "paper_trades_created": len(paper),
    }


@app.get("/api/slack/alerts", dependencies=[dashboard.Depends(dashboard._auth)])
def slack_alerts():
    alerts = list(core._load(SLACK_ALERTS_FILE).values())
    alerts.sort(key=lambda r: r.get("received_at", ""), reverse=True)
    return alerts[:100]


@app.post("/slack/events")
async def slack_events(request: Request):
    raw = await request.body()
    if not SLACK_EVENTS_ENABLED:
        raise HTTPException(status_code=503, detail="Slack event ingestion is disabled")
    _verify_slack_signature(raw, request.headers.get("x-slack-request-timestamp"), request.headers.get("x-slack-signature"))
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Slack JSON") from exc

    if body.get("type") == "url_verification":
        return JSONResponse({"challenge": body.get("challenge")})
    if body.get("type") != "event_callback":
        return {"ok": True, "ignored": "unsupported payload type"}

    event = body.get("event") or {}
    if event.get("type") != "message":
        return {"ok": True, "ignored": "not a message"}
    if event.get("subtype") in {"message_changed", "message_deleted"}:
        return {"ok": True, "ignored": "message edit/delete"}
    if SLACK_CHANNEL_ID and event.get("channel") != SLACK_CHANNEL_ID:
        return {"ok": True, "ignored": "different channel"}

    text = str(event.get("text") or "").strip()
    if not text:
        return {"ok": True, "ignored": "empty message"}
    if SLACK_REQUIRE_WNBA and not _looks_wnba(text):
        return {"ok": True, "ignored": "not WNBA"}

    event_id = str(body.get("event_id") or event.get("client_msg_id") or event.get("ts") or uuid.uuid4().hex)
    alerts = core._load(SLACK_ALERTS_FILE)
    if event_id in alerts:
        return {"ok": True, "duplicate": True, "event_id": event_id}

    event_time = None
    try:
        event_time = int(str(body.get("event_time") or "0"))
    except ValueError:
        event_time = None
    if event_time and abs(int(time.time()) - event_time) > SLACK_MAX_ALERT_AGE_SECONDS:
        return {"ok": True, "ignored": "stale alert", "event_id": event_id}

    parsed = _parse_alert(text)
    rec: dict[str, Any] = {
        "event_id": event_id,
        "received_at": _now_iso(),
        "channel": event.get("channel"),
        "ts": event.get("ts"),
        "text": text,
        "parsed": parsed,
        "paper_only": True,
        "status": "RECEIVED",
    }

    if SLACK_AUTO_PAPER:
        try:
            trade = _paper_trade_from_alert(parsed, event_id, event)
            is_paper = bool(trade.get("paper", True))
            if is_paper:
                rec["status"] = "PAPER_TRADE_CREATED"
                rec["paper_trade_id"] = trade.get("id")
                rec["paper_trade"] = trade
            else:
                if trade.get("requires_approval"):
                    rec["status"] = "LIVE_TRADE_PREPARED"
                else:
                    rec["status"] = "LIVE_TRADE_QUEUED" if trade.get("queued") else "LIVE_TRADE_CREATED"
                rec["live_trade_id"] = trade.get("id") or trade.get("trade_id")
                rec["executor_request_id"] = trade.get("request_id")
                rec["live_trade"] = trade
        except Exception as exc:
            rec["status"] = "NO_TRADE"
            rec["error"] = str(exc)

    _save_alert(event_id, rec)
    return {"ok": True, "event_id": event_id, "status": rec["status"], "paper_trade_id": rec.get("paper_trade_id")}
