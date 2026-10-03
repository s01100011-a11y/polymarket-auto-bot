from __future__ import annotations

from app import attribution_core as attribution
from app import dashboard_sh01_capper_v3 as base
from app import dashboard_metrics_v3 as metrics


def _audit() -> None:
    try:
        attribution.repair_records(base.core)
        raw = base.core._load(base.core.EXECUTIONS_FILE)
        executions = raw if isinstance(raw, dict) else {}
        rows = []
        for rec in executions.values():
            if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("paper") or rec.get("stats_excluded"):
                continue
            capper, sport = attribution.capper_name(rec)
            if str(capper or "").strip().upper() != attribution.SH01:
                continue
            quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
            realized = metrics.base.pnl_base._explicit_realized_pnl(rec)
            rows.append({
                "id": rec.get("id"),
                "sport": sport,
                "status": rec.get("status"),
                "source": rec.get("source"),
                "strategy_source": rec.get("strategy_source"),
                "original_strategy_source": rec.get("original_strategy_source"),
                "attribution_source": rec.get("attribution_source"),
                "reason": rec.get("attribution_reason"),
                "selection": rec.get("strategy_selection"),
                "executed": rec.get("canonical_exact_position") or rec.get("strategy_execution_selection"),
                "market": quote.get("market"),
                "outcome": quote.get("resolved_outcome") or quote.get("requested_outcome"),
                "stake": rec.get("actual_cost_usdc") or rec.get("budget_usdc"),
                "realized_pnl": str(realized) if realized is not None else None,
                "created_at": rec.get("created_at"),
                "closed_at": rec.get("closed_at"),
            })
        total = sum((metrics._d(r.get("realized_pnl")) for r in rows if r.get("realized_pnl") is not None), metrics.Decimal("0"))
        print(f"SH01_AUDIT_ONCE count={len(rows)} realized_total={total}", flush=True)
        for row in rows:
            print(f"SH01_AUDIT_ROW {row}", flush=True)
    except Exception as exc:
        print(f"SH01_AUDIT_ONCE_ERROR {type(exc).__name__}: {exc}", flush=True)


_audit()
