#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from polymarket import PublicClient
import termux_executor as base

# A SELL uses immediate FAK market execution, but never below this bounded floor.
# Defaults allow at most 3 cents adverse movement from the best bid observed when
# the exit starts. Each retry can cross one extra cent of the visible book.
SELL_MAX_SLIPPAGE = max(Decimal("0"), min(Decimal("0.10"), Decimal(os.getenv("EXECUTOR_SELL_MAX_SLIPPAGE", "0.03"))))
SELL_STEP = max(Decimal("0.001"), min(Decimal("0.03"), Decimal(os.getenv("EXECUTOR_SELL_STEP", "0.01"))))
SELL_MIN_PRICE = max(Decimal("0.001"), min(Decimal("0.50"), Decimal(os.getenv("EXECUTOR_SELL_MIN_PRICE", "0.01"))))
SELL_RETRIES = max(1, min(5, int(os.getenv("EXECUTOR_SELL_RETRIES", "3"))))
SELL_POSITION_WAIT_SECONDS = max(1, min(8, int(os.getenv("EXECUTOR_SELL_POSITION_WAIT_SECONDS", "4"))))


def _best_bid(asset_id: str) -> Decimal:
    with PublicClient() as client:
        book = client.get_order_book(asset_id=asset_id)
    bids = getattr(book, "bids", None) or []
    best = max((Decimal(str(level.price)) for level in bids), default=Decimal("0"))
    if best <= 0 or best >= 1:
        raise RuntimeError("No valid bid is currently available for this position")
    return best


def _response_price(response: Any, fallback: Decimal) -> Decimal:
    """Best-effort average execution price from an accepted SELL response."""
    try:
        making = Decimal(str(getattr(response, "making_amount", "0") or "0"))
        taking = Decimal(str(getattr(response, "taking_amount", "0") or "0"))
        if making > 0 and taking > 0:
            price = taking / making
            if Decimal("0") < price < Decimal("1"):
                return price
    except Exception:
        pass
    return fallback


def _sell(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    base._geo()
    asset_id = str(payload.get("asset_id") or "")
    target = Decimal(str(payload.get("shares") or "0"))
    entry = Decimal(str(payload.get("entry_price") or "0"))
    if not asset_id or target <= 0:
        raise RuntimeError("SELL request has no tracked asset/shares")

    position_before = base._position_size(wallet, asset_id)
    target = min(position_before, target)
    if target <= 0:
        raise RuntimeError("Wallet no longer holds the tracked test shares")

    initial_best_bid = _best_bid(asset_id)
    hard_floor = max(SELL_MIN_PRICE, initial_best_bid - SELL_MAX_SLIPPAGE)
    hard_floor = hard_floor.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
    if hard_floor <= 0:
        hard_floor = SELL_MIN_PRICE

    current_position = position_before
    total_sold = Decimal("0")
    total_notional = Decimal("0")
    order_ids: list[str] = []
    attempts: list[dict[str, str]] = []

    for attempt in range(1, SELL_RETRIES + 1):
        remaining = max(Decimal("0"), target - total_sold)
        if remaining <= Decimal("0.0001"):
            break

        best_bid = _best_bid(asset_id)
        min_price = max(hard_floor, best_bid - SELL_STEP)
        min_price = min_price.quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if min_price <= 0:
            min_price = hard_floor

        response = None
        response_error = None
        with base._secure(private_key, wallet) as client:
            response = client.place_market_order(
                token_id=asset_id,
                side="SELL",
                shares=str(remaining),
                min_price=str(min_price),
                order_type="FAK",
            )

        accepted = bool(getattr(response, "ok", False))
        order_id = str(getattr(response, "order_id", "") or "")
        if order_id:
            order_ids.append(order_id)
        if not accepted:
            response_error = str(getattr(response, "message", None) or getattr(response, "code", None) or "FAK rejected")

        after = current_position
        if accepted:
            deadline = time.time() + SELL_POSITION_WAIT_SECONDS
            while time.time() < deadline:
                time.sleep(0.5)
                after = base._position_size(wallet, asset_id)
                if after < current_position:
                    break

        sold_now = max(Decimal("0"), current_position - after)
        exec_price = _response_price(response, best_bid) if accepted else best_bid
        if sold_now > 0:
            total_sold += sold_now
            total_notional += exec_price * sold_now
            current_position = after

        attempts.append({
            "attempt": str(attempt),
            "best_bid": str(best_bid),
            "min_price": str(min_price),
            "sold_shares": str(sold_now),
            "order_id": order_id,
            "response": "accepted" if accepted else response_error or "rejected",
        })

        if total_sold >= target - Decimal("0.0001"):
            break
        time.sleep(0.35)

    if total_sold <= 0:
        raise RuntimeError(
            f"SELL FAK did not fill after {SELL_RETRIES} attempts; no shares were sold. "
            f"Initial best bid {initial_best_bid}, protected floor {hard_floor}."
        )

    remaining = max(Decimal("0"), target - total_sold)
    average_sell_price = total_notional / total_sold if total_notional > 0 else initial_best_bid
    realized = ((average_sell_price - entry) * total_sold).quantize(Decimal("0.01"))
    return {
        "ok": True,
        "status": "CLOSED" if remaining <= Decimal("0.0001") else "PARTIALLY_CLOSED",
        "trade_id": payload.get("trade_id"),
        "order_id": order_ids[-1] if order_ids else None,
        "order_ids": order_ids,
        "sold_shares": str(total_sold),
        "remaining_shares": str(remaining),
        "sell_price": str(average_sell_price),
        "realized_pnl": str(realized),
        "initial_best_bid": str(initial_best_bid),
        "min_sell_price": str(hard_floor),
        "attempts": attempts,
        "executor": base.WORKER_NAME,
        "execution_type": "FAK_MARKET_SELL",
    }


base._sell = _sell


if __name__ == "__main__":
    base.main()
