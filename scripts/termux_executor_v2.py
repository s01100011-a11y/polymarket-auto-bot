#!/usr/bin/env python3
from __future__ import annotations

import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

from polymarket import PublicClient
from polymarket._internal.actions import account as _account_actions
from polymarket._internal.actions.orders.allowance import resolve_order_balance_allowance_target
from polymarket._internal.wallet import signature_type_for
import termux_executor as base

# A SELL uses immediate FAK market execution, but never below this bounded floor.
# Defaults allow at most 3 cents adverse movement from the best bid observed when
# the exit starts. Each retry can cross one extra cent of the visible book.
SELL_MAX_SLIPPAGE = max(Decimal("0"), min(Decimal("0.10"), Decimal(os.getenv("EXECUTOR_SELL_MAX_SLIPPAGE", "0.03"))))
SELL_STEP = max(Decimal("0.001"), min(Decimal("0.03"), Decimal(os.getenv("EXECUTOR_SELL_STEP", "0.01"))))
SELL_MIN_PRICE = max(Decimal("0.001"), min(Decimal("0.50"), Decimal(os.getenv("EXECUTOR_SELL_MIN_PRICE", "0.01"))))
SELL_RETRIES = max(1, min(5, int(os.getenv("EXECUTOR_SELL_RETRIES", "3"))))
SELL_POSITION_WAIT_SECONDS = max(1, min(8, int(os.getenv("EXECUTOR_SELL_POSITION_WAIT_SECONDS", "4"))))
BASE_UNITS = Decimal("1000000")


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


def _refresh_sell_balance(client: Any, asset_id: str) -> tuple[str, Decimal, dict[str, int]]:
    """Refresh Polymarket's CLOB cache for this outcome token before a SELL."""
    asset_type, resolved_asset_id = resolve_order_balance_allowance_target(
        side="SELL", asset_id=asset_id
    )
    signature_type = signature_type_for(client.wallet_type)
    update_path, update_params = _account_actions.build_update_balance_allowance_request(
        asset_type=asset_type,
        asset_id=str(resolved_asset_id) if resolved_asset_id is not None else None,
        signature_type=signature_type,
    )
    # The SDK currently only auto-recovers one specific allowance rejection string.
    # Explicitly refreshing here also handles the "balance is not enough" stale-cache case.
    client._ctx.secure_clob.get_bytes(update_path, params=update_params)  # pyright: ignore[reportPrivateUsage]
    time.sleep(0.15)
    balance_allowance = client.get_balance_allowance(
        asset_type=asset_type,
        asset_id=str(resolved_asset_id) if resolved_asset_id is not None else None,
    )
    shares = Decimal(balance_allowance.balance) / BASE_UNITS
    return str(asset_type), shares, dict(balance_allowance.allowances)


def _sell(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    base._geo()
    asset_id = str(payload.get("asset_id") or "")
    requested_target = Decimal(str(payload.get("shares") or "0"))
    entry = Decimal(str(payload.get("entry_price") or "0"))
    if not asset_id or requested_target <= 0:
        raise RuntimeError("SELL request has no tracked asset/shares")

    position_before = base._position_size(wallet, asset_id)
    target = min(position_before, requested_target)
    if target <= 0:
        raise RuntimeError("Wallet no longer holds the tracked test shares")

    # Public position data can update before the CLOB balance cache. Refresh it explicitly.
    with base._secure(private_key, wallet) as client:
        asset_type, clob_sellable, allowances = _refresh_sell_balance(client, asset_id)
    if clob_sellable <= 0:
        raise RuntimeError(
            f"CLOB still reports 0 sellable outcome shares after balance refresh "
            f"({asset_type}); public position shows {position_before}. Wait for settlement and retry."
        )
    target = min(target, clob_sellable)
    print(
        f"SELL balance refreshed: public={position_before} CLOB={clob_sellable} "
        f"target={target} asset_type={asset_type} allowances={len(allowances)}"
    )

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
            _, clob_available_now, _ = _refresh_sell_balance(client, asset_id)
            order_size = min(remaining, clob_available_now)
            if order_size <= Decimal("0.0001"):
                raise RuntimeError(
                    "CLOB outcome-token balance became zero before SELL. "
                    "No order was submitted; refresh/settlement is still pending."
                )
            response = client.place_market_order(
                token_id=asset_id,
                side="SELL",
                shares=str(order_size),
                min_price=str(min_price),
                order_type="FAK",
            )

        accepted = bool(getattr(response, "ok", False))
        order_id = str(getattr(response, "order_id", "") or "")
        if order_id:
            order_ids.append(order_id)
        if not accepted:
            response_error = str(
                getattr(response, "message", None)
                or getattr(response, "code", None)
                or "FAK rejected"
            )

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
            "clob_available": str(clob_available_now),
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
        "clob_sellable_before": str(clob_sellable),
        "attempts": attempts,
        "executor": base.WORKER_NAME,
        "execution_type": "FAK_MARKET_SELL",
    }


# Also warm the CLOB balance cache after a successful BUY so future exits are ready.
_original_buy = base._buy


def _buy(payload: dict[str, Any], private_key: str, wallet: str) -> dict[str, Any]:
    result = _original_buy(payload, private_key, wallet)
    asset_id = str(result.get("asset_id") or "")
    if result.get("ok") and asset_id:
        try:
            with base._secure(private_key, wallet) as client:
                asset_type, clob_shares, _ = _refresh_sell_balance(client, asset_id)
            result["clob_balance_after_buy"] = str(clob_shares)
            result["clob_asset_type"] = asset_type
        except Exception as exc:
            result["clob_balance_refresh_warning"] = f"{type(exc).__name__}: {exc}"
    return result


base._buy = _buy
base._sell = _sell


if __name__ == "__main__":
    base.main()
