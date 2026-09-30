from __future__ import annotations

import os
import secrets
import uuid
from decimal import Decimal
from typing import Any

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field, HttpUrl
from polymarket import PublicClient


COMBO_MAX_LEGS = max(2, min(12, int(os.getenv("COMBO_MAX_LEGS", "8"))))


class ComboLeg(BaseModel):
    market_url: HttpUrl
    outcome: str = Field(min_length=1, max_length=160)
    market_type: str = Field(default="moneyline", min_length=1, max_length=120)


class ComboRequest(BaseModel):
    legs: list[ComboLeg] = Field(min_length=2, max_length=12)
    budget_usdc: Decimal = Field(gt=0)
    max_price: Decimal = Field(default=Decimal("0.95"), gt=0, lt=1)
    label: str = Field(default="", max_length=240)
    note: str = Field(default="", max_length=500)


def combo_trading_enabled() -> bool:
    return os.getenv("COMBO_TRADING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _require_smoke_token(token: str) -> None:
    expected = os.getenv("COMBO_SMOKE_TOKEN", "").strip()
    if not expected or not secrets.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=404, detail="Not found")
    if combo_trading_enabled():
        raise HTTPException(status_code=409, detail="Combo smoke preview is disabled while live Combo trading is enabled.")


def _resolve_position_id(core, market: Any, outcome: str) -> tuple[str, str]:
    target = core._norm(outcome)
    yes = market.outcomes.yes
    no = market.outcomes.no
    yes_label = str(getattr(yes, "label", "Yes"))
    no_label = str(getattr(no, "label", "No"))

    if target in {"yes", core._norm(yes_label)}:
        selected, label = yes, yes_label
    elif target in {"no", core._norm(no_label)}:
        selected, label = no, no_label
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Outcome '{outcome}' does not match this market. Valid outcomes: '{yes_label}' or '{no_label}'.",
        )

    position_id = getattr(selected, "position_id", None)
    if not position_id:
        raise HTTPException(
            status_code=400,
            detail=f"Polymarket did not expose a Combo position_id for outcome '{label}'.",
        )
    return str(position_id), label


def resolve_combo_legs(core, legs: list[ComboLeg]) -> list[dict[str, str]]:
    if len(legs) < 2:
        raise HTTPException(status_code=400, detail="A Combo requires at least two legs.")
    if len(legs) > COMBO_MAX_LEGS:
        raise HTTPException(status_code=400, detail=f"Combo exceeds COMBO_MAX_LEGS={COMBO_MAX_LEGS}.")

    resolved: list[dict[str, str]] = []
    with PublicClient() as client:
        for index, leg in enumerate(legs, start=1):
            market_url = str(leg.market_url)
            if core._sports_event_slug(market_url) is None:
                raise HTTPException(status_code=400, detail=f"Combo leg {index} must use a Polymarket /sports/ event URL.")

            requested_type = core._norm(leg.market_type)
            if requested_type not in {"moneyline", "spread", "total"}:
                raise HTTPException(
                    status_code=400,
                    detail=f"Combo leg {index} market type '{leg.market_type}' is not supported.",
                )

            intent = core.TradeIntent(
                market_url=market_url,
                outcome=leg.outcome,
                market_type=requested_type,
                max_price=Decimal("0.99"),
                budget_usdc=Decimal("1"),
                note="Combo leg resolution",
            )
            market = core._select_market(client, intent)
            actual_type = core._norm(core._market_type(market))
            if actual_type != requested_type:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Combo leg {index} resolved to market type '{core._market_type(market)}', "
                        f"not requested '{requested_type}'."
                    ),
                )
            position_id, outcome_label = _resolve_position_id(core, market, leg.outcome)
            resolved.append(
                {
                    "market_url": market_url,
                    "market": str(getattr(market, "question", None) or getattr(market, "slug", "sports market")),
                    "market_type": actual_type,
                    "requested_outcome": leg.outcome,
                    "resolved_outcome": outcome_label,
                    "position_id": position_id,
                }
            )

    ids = [leg["position_id"] for leg in resolved]
    if len(set(ids)) != len(ids):
        raise HTTPException(status_code=400, detail="Combo contains duplicate Polymarket position IDs.")
    return resolved


def _validate_budget(core, budget: Decimal) -> None:
    if budget > core.MAX_AUTO_TRADE_USDC:
        raise HTTPException(
            status_code=400,
            detail=f"Combo amount exceeds dashboard Auto trade cap ${core.MAX_AUTO_TRADE_USDC}.",
        )
    used = core._daily_budget_used()
    if used + budget > core.MAX_DAILY_BUDGET_USDC:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Combo would exceed MAX_DAILY_BUDGET_USDC=${core.MAX_DAILY_BUDGET_USDC}; "
                f"used=${used}."
            ),
        )


def _payload(req: ComboRequest, resolved: list[dict[str, str]], trade_id: str | None = None) -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "budget_usdc": str(req.budget_usdc),
        "max_price": str(req.max_price),
        "label": req.label.strip() or " + ".join(leg["resolved_outcome"] for leg in resolved),
        "note": req.note,
        "legs": resolved,
        "leg_position_ids": [leg["position_id"] for leg in resolved],
        "source": "combo_api",
        "auto": True,
    }


def install(*, app, dashboard, core) -> None:
    from app import termux_executor_dashboard as executor

    @app.get("/api/combo/status", dependencies=[Depends(dashboard._auth)])
    def combo_status():
        return {
            "enabled": combo_trading_enabled(),
            "max_legs": COMBO_MAX_LEGS,
            "live_trading": core.live_trading_enabled(),
            "auto_trading": core.auto_trading_enabled(),
            "max_auto_trade_usdc": str(core.MAX_AUTO_TRADE_USDC),
            "daily_budget_usdc": str(core.MAX_DAILY_BUDGET_USDC),
        }

    @app.get("/api/combo/smoke/preview")
    def combo_smoke_preview(
        token: str,
        market_url: HttpUrl,
        outcome1: str,
        market_type1: str,
        outcome2: str,
        market_type2: str,
        max_price: Decimal = Decimal("0.95"),
    ):
        _require_smoke_token(token)
        req = ComboRequest(
            legs=[
                ComboLeg(market_url=market_url, outcome=outcome1, market_type=market_type1),
                ComboLeg(market_url=market_url, outcome=outcome2, market_type=market_type2),
            ],
            budget_usdc=Decimal("1"),
            max_price=max_price,
            label=f"Combo smoke test — {outcome1} + {outcome2}",
            note="QUOTE ONLY — diagnostic smoke test",
        )
        resolved = resolve_combo_legs(core, req.legs)
        _validate_budget(core, req.budget_usdc)
        if req.max_price > core.MAX_PRICE:
            raise HTTPException(status_code=400, detail=f"Combo maximum price exceeds MAX_PRICE={core.MAX_PRICE}.")
        rec = executor._enqueue("COMBO_PREVIEW", _payload(req, resolved))
        return {
            "ok": True,
            "queued": True,
            "request_id": rec["id"],
            "legs": resolved,
            "budget_usdc": str(req.budget_usdc),
        }

    @app.get("/api/combo/smoke/result/{request_id}")
    def combo_smoke_result(request_id: str, token: str):
        _require_smoke_token(token)
        rec = executor._queue_load().get(request_id)
        if not rec or rec.get("action") != "COMBO_PREVIEW":
            raise HTTPException(status_code=404, detail="Combo preview request not found")
        return {
            "id": rec.get("id"),
            "action": rec.get("action"),
            "status": rec.get("status"),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "result": rec.get("result"),
            "error": rec.get("error"),
        }

    @app.post("/api/combo/preview", dependencies=[Depends(dashboard._auth)])
    def combo_preview(req: ComboRequest):
        resolved = resolve_combo_legs(core, req.legs)
        _validate_budget(core, req.budget_usdc)
        if req.max_price > core.MAX_PRICE:
            raise HTTPException(status_code=400, detail=f"Combo maximum price exceeds MAX_PRICE={core.MAX_PRICE}.")
        rec = executor._enqueue("COMBO_PREVIEW", _payload(req, resolved))
        return {
            "ok": True,
            "queued": True,
            "request_id": rec["id"],
            "legs": resolved,
            "budget_usdc": str(req.budget_usdc),
        }

    @app.post("/api/combo/buy", dependencies=[Depends(dashboard._auth)])
    def combo_buy(req: ComboRequest):
        if not core.bot_enabled():
            raise HTTPException(status_code=409, detail="Dashboard master switch is OFF.")
        if not combo_trading_enabled():
            raise HTTPException(status_code=409, detail="Combo live trading is disabled (COMBO_TRADING_ENABLED=false).")
        if not core.live_trading_enabled() or not core.auto_trading_enabled():
            raise HTTPException(status_code=409, detail="Combo BUY requires LIVE_TRADING=true and AUTO_TRADING=true.")

        _validate_budget(core, req.budget_usdc)
        if req.max_price > core.MAX_PRICE:
            raise HTTPException(status_code=400, detail=f"Combo maximum price exceeds MAX_PRICE={core.MAX_PRICE}.")
        resolved = resolve_combo_legs(core, req.legs)
        trade_id = f"combo-{uuid.uuid4().hex[:12]}"
        rec = executor._enqueue("COMBO_BUY", _payload(req, resolved, trade_id=trade_id))
        return {
            "ok": True,
            "queued": True,
            "request_id": rec["id"],
            "trade_id": trade_id,
            "legs": resolved,
            "budget_usdc": str(req.budget_usdc),
        }

    print(
        "POLYMARKET_COMBOS installed "
        f"enabled={combo_trading_enabled()} max_legs={COMBO_MAX_LEGS}",
        flush=True,
    )
