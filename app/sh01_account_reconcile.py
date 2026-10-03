from __future__ import annotations

import hashlib
import threading
import time
from decimal import Decimal
from typing import Any

from app import attribution_core as attribution
from app import dashboard_metrics_v3 as metrics
from app import universal_position_identity as identity

_EPS = Decimal("0.0001")
_LOCK = threading.Lock()
_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}
_SETTLED = {"SETTLED_WIN", "SETTLED_LOSS", "SETTLED_PUSH"}


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _text(pos: Any, *names: str) -> str:
    for name in names:
        value = getattr(pos, name, None)
        if value not in {None, ""}:
            return str(value)
    return ""


def _infer_sport(pos: Any) -> str | None:
    text = " ".join(_text(pos, name) for name in ("event_slug", "slug", "market_slug", "title", "market_title", "question")).casefold()
    for needle, sport in (
        ("cfb", "CFB"), ("ncaaf", "CFB"), ("nfl", "NFL"),
        ("wnba", "WNBA"), ("nba", "NBA"), ("mlb", "MLB"),
        ("nhl", "NHL"), ("nrl", "NRL"), ("ufc", "UFC"),
    ):
        if needle in text:
            return sport
    return None


def _infer_market_type(pos: Any, title: str) -> str:
    direct = _text(pos, "market_type", "sports_market_type")
    if direct:
        return direct
    low = title.casefold()
    if "spread" in low:
        return "spread"
    if "over" in low or "under" in low or "total" in low:
        return "total"
    return "moneyline"


def _current_wallet_positions(wallet: str) -> dict[str, Any]:
    """Return current wallet holdings only, not the full historical position archive."""
    positions: dict[str, Any] = {}
    with metrics.ingest.PublicClient() as client:
        paginator = client.list_positions(user=wallet, page_size=100)
        for pos in paginator.iter_items():
            asset_id = str(getattr(pos, "asset_id", None) or getattr(pos, "token_id", None) or "")
            if not asset_id:
                continue
            if _d(getattr(pos, "current_size", "0")) <= _EPS:
                continue
            positions[asset_id] = pos
    return positions


def reconcile(core: Any) -> dict[str, Any]:
    with _LOCK:
        wallet = metrics._wallet_for_reconciliation()
        if not wallet:
            return {"ok": False, "reason": "wallet unavailable"}
        try:
            positions = _current_wallet_positions(wallet)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

        attribution.repair_records(core)
        executions = core._load(core.EXECUTIONS_FILE)
        if not isinstance(executions, dict):
            executions = {}

        known_by_asset: dict[str, Decimal] = {}
        for rec in executions.values():
            if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("paper"):
                continue
            if str(rec.get("source") or "") == "polymarket_account_reconcile":
                continue
            if str(rec.get("status") or "") not in _ACTIVE:
                continue
            quote = rec.get("quote") or {}
            asset_id = str(quote.get("asset_id") or "")
            if not asset_id:
                continue
            shares = _d(rec.get("remaining_shares") if rec.get("remaining_shares") is not None else rec.get("filled_shares") or quote.get("shares"))
            known_by_asset[asset_id] = known_by_asset.get(asset_id, Decimal("0")) + max(shares, Decimal("0"))

        stamp = metrics._now_iso()
        changed = imported = preserved_settled = 0
        seen_synthetic: set[str] = set()
        for asset_id, pos in positions.items():
            wallet_size = _d(getattr(pos, "current_size", "0"))
            known = known_by_asset.get(str(asset_id), Decimal("0"))
            unmatched = max(Decimal("0"), wallet_size - known)
            rec_id = "sh01-account-" + hashlib.sha256(str(asset_id).encode("utf-8")).hexdigest()[:18]
            seen_synthetic.add(rec_id)
            existing = executions.get(rec_id) if isinstance(executions.get(rec_id), dict) else None

            if unmatched <= _EPS:
                if existing and existing.get("status") in _ACTIVE:
                    existing["status"] = "CLOSED_RECONCILED"
                    existing["remaining_shares"] = "0"
                    existing["closed_at"] = existing.get("closed_at") or stamp
                    existing["wallet_reconciled_at"] = stamp
                    existing["reconciliation_note"] = "Previously unmatched Polymarket holding is now fully explained by tracked executions."
                    executions[rec_id] = existing
                    changed += 1
                continue

            # Resolved positions can remain visible in the wallet until redemption.
            # Once the normal settlement engine has graded a synthetic SH01 record,
            # never reopen it merely because those terminal CTF shares still exist.
            if existing and str(existing.get("status") or "") in _SETTLED:
                existing["wallet_reconciled_at"] = stamp
                existing["wallet_position_size"] = str(wallet_size)
                existing["account_reconcile_note"] = "Resolved unmatched account position remains attributed to SH01; terminal settlement is preserved until redemption."
                executions[rec_id] = existing
                preserved_settled += 1
                continue

            avg_price = _d(getattr(pos, "avg_price", "0"))
            entry_cost = _d(getattr(pos, "entry_cost_usdc", "0"))
            if entry_cost <= 0 and avg_price > 0:
                entry_cost = unmatched * avg_price
            title = _text(pos, "title", "market_title", "question") or f"Polymarket asset {str(asset_id)[:12]}…"
            outcome = _text(pos, "outcome", "outcome_name", "name")
            event_slug = _text(pos, "event_slug", "slug", "market_slug")
            market_url = f"https://polymarket.com/event/{event_slug}" if event_slug else None
            sport = _infer_sport(pos)
            market_type = _infer_market_type(pos, title)

            rec = dict(existing or {})
            rec.update({
                "id": rec_id,
                "status": "ORDER_SUBMITTED",
                "created_at": rec.get("created_at") or stamp,
                "submitted_at": rec.get("submitted_at") or stamp,
                "updated_at": stamp,
                "source": "polymarket_account_reconcile",
                "paper": False,
                "auto": False,
                "strategy_source": attribution.SH01,
                "attribution_source": attribution.SH01,
                "attribution_reason": "unmatched_polymarket_account_position",
                "strategy_sport": sport,
                "remaining_shares": str(unmatched),
                "filled_shares": str(unmatched),
                "actual_cost_usdc": str(entry_cost) if entry_cost > 0 else None,
                "budget_usdc": str(entry_cost) if entry_cost > 0 else None,
                "wallet_reconciled_at": stamp,
                "wallet_position_size": str(wallet_size),
                "account_reconcile_note": "Holding exists in the Polymarket account but is not attributable to an exact linked capper/monitor execution, so it is tracked under SH01.",
                "quote": {
                    "market": title,
                    "market_url": market_url,
                    "market_type": market_type,
                    "requested_outcome": outcome or None,
                    "resolved_outcome": outcome or None,
                    "asset_id": str(asset_id),
                    "entry_price": str(avg_price) if avg_price > 0 else None,
                    "limit_price": str(avg_price) if avg_price > 0 else None,
                    "shares": str(unmatched),
                },
            })
            rec["canonical_exact_position"] = identity.canonical_exact_position(rec)
            executions[rec_id] = rec
            changed += 1
            imported += 1

        # Current-only reconciliation explicitly closes synthetic live records whose
        # asset disappears from the wallet response. Already-settled records stay graded.
        for rec_id, rec in list(executions.items()):
            if not str(rec_id).startswith("sh01-account-") or not isinstance(rec, dict):
                continue
            if rec_id in seen_synthetic or rec.get("status") not in _ACTIVE:
                continue
            rec["status"] = "CLOSED_RECONCILED"
            rec["remaining_shares"] = "0"
            rec["closed_at"] = rec.get("closed_at") or stamp
            rec["wallet_reconciled_at"] = stamp
            rec["reconciliation_note"] = "Unmatched Polymarket holding is no longer present. Exit price and realized P/L remain unknown unless recorded by the bot."
            executions[rec_id] = rec
            changed += 1

        if changed or preserved_settled:
            core._save(core.EXECUTIONS_FILE, executions)
        return {
            "ok": True,
            "positions_checked": len(positions),
            "imported_or_updated_sh01": imported,
            "preserved_settled_sh01": preserved_settled,
            "changed": changed,
        }


def start_background(core: Any, interval_seconds: int = 30) -> None:
    def loop() -> None:
        print("SH01_ACCOUNT_RECONCILE_THREAD_STARTED", flush=True)
        while True:
            try:
                print(f"SH01_ACCOUNT_RECONCILE {reconcile(core)}", flush=True)
            except Exception as exc:
                print(f"SH01_ACCOUNT_RECONCILE_ERROR {type(exc).__name__}:{exc}", flush=True)
            time.sleep(max(15, int(interval_seconds)))
    threading.Thread(target=loop, name="sh01-account-reconcile", daemon=True).start()
