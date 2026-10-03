from __future__ import annotations

from typing import Any

from app import basketball_monitor_capper as monitor


_ORIGINAL_EXECUTION_SPORT = monitor._execution_sport
_MONITOR_LABELS = {
    str(spec.get("label") or "")
    for spec in monitor.SPORTS.values()
    if isinstance(spec, dict)
}


def _is_real_monitor_execution(rec: dict[str, Any]) -> bool:
    """Only PW/Slack monitor executions may appear on NBA/WNBA monitor cards.

    Manual dashboard orders, Polymarket account-reconcile rows, and executor test
    trades can still carry strategy_sport=WNBA/NBA.  That sport tag describes the
    market, not the source of the bet, so it must never be enough to attribute a
    position to the monitor.
    """
    if rec.get("stats_excluded"):
        return False

    source = str(rec.get("source") or "").strip().lower()
    event_id = str(rec.get("slack_event_id") or rec.get("strategy_pick_id") or "").strip()
    strategy_source = str(rec.get("strategy_source") or "").strip()
    attribution_source = str(rec.get("attribution_source") or "").strip()

    if source == "slack_live":
        return True
    if event_id.startswith("pwexport-"):
        return True
    if strategy_source in _MONITOR_LABELS or attribution_source in _MONITOR_LABELS:
        return True
    return False


def _execution_sport_strict(rec: dict[str, Any], ingest: Any) -> str | None:
    if not isinstance(rec, dict) or not _is_real_monitor_execution(rec):
        return None
    return _ORIGINAL_EXECUTION_SPORT(rec, ingest)


monitor._execution_sport = _execution_sport_strict
