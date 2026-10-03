from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app import dashboard_metrics_v3 as metrics
from app import nfl_capper_ingest as nfl
from app import universal_position_identity as identity

SH01 = "SH01"
LINE_TOLERANCE_POINTS = Decimal("1")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _within_line_tolerance(requested: Decimal, executed: Decimal) -> bool:
    return abs(requested - executed) <= LINE_TOLERANCE_POINTS


def unit_value_usdc(rec: dict[str, Any]) -> Decimal:
    """Recover the value of 1u at bet time without depending on risk/stake size.

    Primary source is the unit snapshot persisted by the executor. Older records
    can recover it from target profit / called units, or from the binary entry
    economics for TO_WIN sizing. This keeps unit P/L comparable when the dollar
    stake or portfolio percentage changes over time.
    """
    direct = _d(rec.get("strategy_unit_usdc"))
    if direct > 0:
        return direct

    units = _d(rec.get("strategy_units"))
    target = _d(rec.get("strategy_target_profit_usdc"))
    if units > 0 and target > 0:
        return target / units

    mode = str(rec.get("strategy_sizing_mode") or rec.get("sizing_mode") or "").upper()
    quote = rec.get("quote") or {}
    entry = _d(quote.get("paper_entry_price") or quote.get("entry_price") or quote.get("limit_price"))
    stake = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
    if units > 0 and stake > 0:
        if mode in {"TO_WIN", "WIN", "TARGET_PROFIT"} and Decimal("0") < entry < Decimal("1"):
            potential_profit = stake * (Decimal("1") - entry) / entry
            if potential_profit > 0:
                return potential_profit / units
        # Last-resort historical fallback when only a flat-risk record survives.
        if mode in {"RISK", "FLAT", "STAKE", ""}:
            return stake / units
    return Decimal("0")


def attribution_for_execution(rec: dict[str, Any]) -> tuple[str, str]:
    source = str(rec.get("strategy_source") or "").strip()
    if source.upper() == SH01 or str(rec.get("source") or "") == "polymarket_account_reconcile":
        return SH01, "manual_or_account"

    link_id = str(rec.get("strategy_pick_id") or rec.get("slack_event_id") or "").strip()
    if not source or not link_id:
        return SH01, "no_linked_original_call"

    kind = identity.market_kind(rec)
    if kind == "spread":
        requested = identity.requested_spread(rec)
        executed = identity.effective_spread(rec)
        if requested is None or executed is None:
            return SH01, "spread_line_unverifiable"
        if not _within_line_tolerance(requested, executed):
            return SH01, f"spread_changed:{identity.fmt_signed(requested)}->{identity.fmt_signed(executed)}"
        if requested != executed:
            return source, f"spread_within_1pt:{identity.fmt_signed(requested)}->{identity.fmt_signed(executed)}"
        return source, "exact_original_call"

    if kind in {"total", "team_total"}:
        req_side, requested = identity.requested_total(rec)
        exe_side, executed = identity.total_identity(rec)
        if requested is None or executed is None:
            return SH01, "total_line_unverifiable"
        if req_side and exe_side and req_side != exe_side:
            return SH01, "total_side_changed_from_original_call"
        if not _within_line_tolerance(requested, executed):
            return SH01, "total_changed_from_original_call"
        if requested != executed:
            return source, f"total_within_1pt:{requested}->{executed}"
        return source, "exact_original_call"

    if kind == "moneyline":
        return (source, "exact_original_call") if identity.moneyline_matches(rec) else (SH01, "moneyline_outcome_unverifiable")

    return SH01, f"unsupported_market_type:{kind}"


def repair_records(core: Any) -> dict[str, int]:
    executions = core._load(core.EXECUTIONS_FILE)
    if not isinstance(executions, dict):
        return {"checked": 0, "changed": 0}
    checked = changed = 0
    for key, rec in executions.items():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        checked += 1
        exact = identity.canonical_exact_position(rec)
        if exact and rec.get("canonical_exact_position") != exact:
            rec["canonical_exact_position"] = exact
            changed += 1
        spread = identity.effective_spread(rec)
        if spread is not None:
            signed = identity.fmt_signed(spread)
            if str(rec.get("strategy_executed_spread_line") or "") != signed:
                rec["strategy_executed_spread_line"] = signed
                changed += 1
        source, reason = attribution_for_execution(rec)
        if rec.get("attribution_source") != source:
            rec["attribution_source"] = source
            changed += 1
        if rec.get("attribution_reason") != reason:
            rec["attribution_reason"] = reason
            changed += 1
        if source == SH01 and rec.get("original_strategy_source") is None and rec.get("strategy_source"):
            rec["original_strategy_source"] = rec.get("strategy_source")
            changed += 1
        unit_value = unit_value_usdc(rec)
        if unit_value > 0 and _d(rec.get("stats_unit_value_usdc")) != unit_value:
            rec["stats_unit_value_usdc"] = str(unit_value)
            changed += 1
        executions[key] = rec
    if changed:
        core._save(core.EXECUTIONS_FILE, executions)
    return {"checked": checked, "changed": changed}


def install_stats_attribution(core: Any) -> None:
    if getattr(nfl, "_sh01_stats_patch", False):
        return
    original = nfl._stats_from_executions

    def patched(executions: dict[str, Any], *, labels: tuple[str, ...] = nfl.SOURCE_LABELS, sport: str = "NFL", live_marks: dict[str, dict[str, Any]] | None = None):
        transformed: dict[str, Any] = {}
        for key, rec in executions.items():
            if not isinstance(rec, dict):
                transformed[key] = rec
                continue
            clone = dict(rec)
            clone["strategy_source"] = rec.get("attribution_source") or attribution_for_execution(rec)[0]
            transformed[key] = clone
        result = original(transformed, labels=labels, sport=sport, live_marks=live_marks)
        marks = live_marks or {}
        now = datetime.now(timezone.utc)
        for label in labels:
            realized_units = live_units = units_7d = units_30d = Decimal("0")
            coverage = 0
            for execution_id, rec in transformed.items():
                if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("strategy_source") != label:
                    continue
                record_sport = str(rec.get("strategy_sport") or "").upper()
                if sport and record_sport and record_sport != str(sport).upper():
                    continue
                unit_usdc = unit_value_usdc(rec)
                if unit_usdc <= 0:
                    continue
                coverage += 1
                status = str(rec.get("status") or "")
                if status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
                    mark = marks.get(str(rec.get("id") or execution_id)) or {}
                    live_units += _d(mark.get("estimated_pnl")) / unit_usdc
                    continue
                pnl = rec.get("realized_pnl")
                if pnl is None:
                    continue
                pnl_units = _d(pnl) / unit_usdc
                realized_units += pnl_units
                settled = metrics._metric_time(rec)
                if settled is not None:
                    age = (now - settled).total_seconds()
                    if age <= 30 * 86400:
                        units_30d += pnl_units
                    if age <= 7 * 86400:
                        units_7d += pnl_units
            row = result.setdefault(label, {"label": label})
            row["realized_units_pnl"] = str(realized_units.quantize(Decimal("0.01")))
            row["live_units_pnl"] = str(live_units.quantize(Decimal("0.01")))
            row["total_live_units_pnl"] = str((realized_units + live_units).quantize(Decimal("0.01")))
            row["realized_units_7d"] = str(units_7d.quantize(Decimal("0.01")))
            row["realized_units_30d"] = str(units_30d.quantize(Decimal("0.01")))
            row["unit_pnl_coverage_bets"] = coverage
        return result

    nfl._stats_from_executions = patched
    nfl._sh01_stats_patch = True

    original_identity = metrics._legacy_monitor_identity
    def attributed_identity(rec: dict[str, Any]) -> tuple[str, str]:
        source, sport = original_identity(rec)
        attr = str(rec.get("attribution_source") or "").strip()
        return (attr or attribution_for_execution(rec)[0], sport)
    metrics._legacy_monitor_identity = attributed_identity


def capper_name(rec: dict[str, Any]) -> tuple[str | None, str]:
    source, sport = metrics._legacy_monitor_identity(rec)
    return metrics._capper_name(source, sport), sport


def unit_summary(records: list[dict[str, Any]], marks: dict[str, dict[str, Any]], *, source_name: str | None = None, sport: str | None = None, bet_type: str | None = None) -> dict[str, Any]:
    units_called = realized_units = live_units = Decimal("0")
    coverage = 0
    for rec in records:
        if rec.get("parent_trade_id"):
            continue
        capper, rec_sport = capper_name(rec)
        if source_name and capper != source_name:
            continue
        if sport and rec_sport != sport:
            continue
        kind = identity.market_kind(rec)
        rec_type = "ML" if kind == "moneyline" else "Spread" if kind == "spread" else "Total" if kind in {"total", "team_total"} else None
        if bet_type and rec_type != bet_type:
            continue
        units_called += _d(rec.get("strategy_units"))
        unit_usdc = unit_value_usdc(rec)
        if unit_usdc <= 0:
            continue
        coverage += 1
        if str(rec.get("status") or "") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
            live_units += _d((marks.get(str(rec.get("id") or "")) or {}).get("estimated_pnl")) / unit_usdc
        elif rec.get("realized_pnl") is not None:
            realized_units += _d(rec.get("realized_pnl")) / unit_usdc
    return {
        "units_called": str(units_called.quantize(Decimal("0.01"))),
        "realized_units_pnl": str(realized_units.quantize(Decimal("0.01"))),
        "live_units_pnl": str(live_units.quantize(Decimal("0.01"))),
        "total_live_units_pnl": str((realized_units + live_units).quantize(Decimal("0.01"))),
        "unit_pnl_coverage_bets": coverage,
    }
