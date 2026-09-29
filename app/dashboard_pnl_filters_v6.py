from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import Depends

from app import dashboard_filters_v5 as base

app = base.app
dashboard = base.dashboard
core = base.core

MODE_PNL_HISTORY_FILE = core.DATA_DIR / "pnl_history_modes.json"
MODE_PNL_HISTORY_MAX_ENTRIES = 210240
MODE_PNL_SAMPLE_SECONDS = 300
MODE_PNL_RANGE_MAX_POINTS = 1200
_MODE_PNL_LOCK = threading.Lock()
_LATEST_MODE_PNL_POINT: dict[str, Any] | None = None

_BASE_SNAPSHOT = dashboard._dashboard_snapshot


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _mode_totals(data: dict[str, Any]) -> dict[str, dict[str, Decimal]]:
    realized = {"paper": Decimal("0"), "live": Decimal("0")}
    open_pnl = {"paper": Decimal("0"), "live": Decimal("0")}

    executions = core._load(core.EXECUTIONS_FILE)
    for rec in executions.values():
        bucket = base._trade_bucket(rec)
        if bucket not in {"paper", "live"}:
            continue
        if rec.get("status") in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}:
            continue
        pnl = base.metrics_base.base.pnl_base._explicit_realized_pnl(rec)
        if pnl is not None:
            realized[bucket] += pnl

    execution_by_id = {
        str(rec.get("id") or key): rec
        for key, rec in executions.items()
        if isinstance(rec, dict)
    }
    for row in data.get("live_trades", []):
        rec = execution_by_id.get(str(row.get("id") or "")) or row
        bucket = base._trade_bucket(rec)
        if bucket not in {"paper", "live"}:
            continue
        if row.get("estimated_pnl") is not None:
            open_pnl[bucket] += _d(row.get("estimated_pnl"))

    result: dict[str, dict[str, Decimal]] = {}
    for mode in ("paper", "live"):
        result[mode] = {
            "realized": realized[mode],
            "open": open_pnl[mode],
            "total": realized[mode] + open_pnl[mode],
        }
    result["both"] = {
        "realized": realized["paper"] + realized["live"],
        "open": open_pnl["paper"] + open_pnl["live"],
        "total": realized["paper"] + realized["live"] + open_pnl["paper"] + open_pnl["live"],
    }
    return result


def _mode_history_points() -> list[dict[str, Any]]:
    history = core._load(MODE_PNL_HISTORY_FILE)
    if not isinstance(history, dict):
        return []
    points = [v for v in history.values() if isinstance(v, dict)]
    points.sort(key=lambda x: float(x.get("epoch") or 0))
    return points


def _append_latest_mode_point(points: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = _LATEST_MODE_PNL_POINT
    if not isinstance(latest, dict):
        return points
    if not points or float(latest.get("epoch") or 0) > float(points[-1].get("epoch") or 0):
        return [*points, dict(latest)]
    return points


def _downsample_mode_points(points: list[dict[str, Any]], limit: int = MODE_PNL_RANGE_MAX_POINTS) -> list[dict[str, Any]]:
    if len(points) <= limit:
        return points
    last_index = len(points) - 1
    indexes = {0, last_index}
    for i in range(1, max(1, limit - 1)):
        indexes.add(round(i * last_index / max(1, limit - 1)))
    return [points[i] for i in sorted(indexes)]


def _mode_range_cutoff(range_key: str, now: datetime) -> float | None:
    key = str(range_key or "1d").strip().lower()
    if key == "1d":
        return (now - timedelta(days=1)).timestamp()
    if key == "1w":
        return (now - timedelta(days=7)).timestamp()
    if key == "1m":
        return (now - timedelta(days=30)).timestamp()
    if key == "1y":
        return (now - timedelta(days=365)).timestamp()
    if key == "ytd":
        return datetime(now.year, 1, 1, tzinfo=timezone.utc).timestamp()
    if key == "all":
        return None
    return (now - timedelta(days=1)).timestamp()


def _mode_history_for_range(mode: str, range_key: str) -> dict[str, Any]:
    mode_key = str(mode or "live").strip().lower()
    if mode_key not in {"live", "paper", "both"}:
        mode_key = "live"
    range_name = str(range_key or "1d").strip().lower()
    if range_name not in {"1d", "1w", "1m", "1y", "ytd", "all"}:
        range_name = "1d"
    field = {"live": "live_pnl", "paper": "paper_pnl", "both": "both_pnl"}[mode_key]
    with _MODE_PNL_LOCK:
        raw_points = _append_latest_mode_point(_mode_history_points())
    cutoff = _mode_range_cutoff(range_name, datetime.now(timezone.utc))
    if cutoff is not None:
        raw_points = [p for p in raw_points if float(p.get("epoch") or 0) >= cutoff]
    points = [
        {"epoch": p.get("epoch"), "at": p.get("at"), "pnl": p.get(field)}
        for p in raw_points
        if p.get(field) is not None
    ]
    sampled = _downsample_mode_points(points)
    return {
        "mode": mode_key,
        "range": range_name,
        "points": sampled,
        "sample_count": len(points),
        "returned_count": len(sampled),
        "first_at": points[0].get("at") if points else None,
        "last_at": points[-1].get("at") if points else None,
        "archive_sample_seconds": MODE_PNL_SAMPLE_SECONDS,
    }


@app.get("/api/dashboard/pnl-history-mode", dependencies=[Depends(dashboard._auth)])
def dashboard_mode_pnl_history(mode: str = "live", range: str = "1d"):
    return _mode_history_for_range(mode, range)


def _record_mode_history(totals: dict[str, dict[str, Decimal]]) -> dict[str, list[dict[str, Any]]]:
    global _LATEST_MODE_PNL_POINT
    now = time.time()
    current = {
        "epoch": now,
        "at": base.base.ingest._now_iso(),
        "paper_pnl": str(totals["paper"]["total"].quantize(Decimal("0.01"))),
        "live_pnl": str(totals["live"]["total"].quantize(Decimal("0.01"))),
        "both_pnl": str(totals["both"]["total"].quantize(Decimal("0.01"))),
    }
    with _MODE_PNL_LOCK:
        _LATEST_MODE_PNL_POINT = current
        history = core._load(MODE_PNL_HISTORY_FILE)
        if not isinstance(history, dict):
            history = {}
        latest_epoch = 0.0
        if history:
            try:
                latest_epoch = max(float(v.get("epoch") or 0) for v in history.values() if isinstance(v, dict))
            except Exception:
                latest_epoch = 0.0

        if not history or now - latest_epoch >= MODE_PNL_SAMPLE_SECONDS:
            key = str(int(now * 1000))
            history[key] = current
            if len(history) > MODE_PNL_HISTORY_MAX_ENTRIES:
                ordered = sorted(history.items(), key=lambda kv: float((kv[1] or {}).get("epoch") or 0))
                history = dict(ordered[-MODE_PNL_HISTORY_MAX_ENTRIES:])
            core._save(MODE_PNL_HISTORY_FILE, history)

        points = [v for v in history.values() if isinstance(v, dict)]
        points.sort(key=lambda x: float(x.get("epoch") or 0))
        points = _append_latest_mode_point(points)
        cutoff = now - 86400
        points = [p for p in points if float(p.get("epoch") or 0) >= cutoff][-360:]

    out: dict[str, list[dict[str, Any]]] = {"paper": [], "live": [], "both": []}
    for point in points:
        base_point = {"epoch": point.get("epoch"), "at": point.get("at")}
        for mode, field in (("paper", "paper_pnl"), ("live", "live_pnl"), ("both", "both_pnl")):
            if point.get(field) is not None:
                out[mode].append({**base_point, "pnl": point.get(field)})
    return out


def _dashboard_snapshot_v6() -> dict[str, Any]:
    data = _BASE_SNAPSHOT()
    totals = _mode_totals(data)
    histories = _record_mode_history(totals)
    data["filtered_pnl"] = {
        mode: {
            "total_pnl": str(values["total"].quantize(Decimal("0.01"))),
            "open_unrealized_pnl": str(values["open"].quantize(Decimal("0.01"))),
            "realized_pnl": str(values["realized"].quantize(Decimal("0.01"))),
            "history": histories[mode],
        }
        for mode, values in totals.items()
    }
    return data


dashboard._dashboard_snapshot = _dashboard_snapshot_v6


def _install_filtered_pnl_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if "dashboardFilteredPnl" in html:
        return

    # The regular dashboard refresh previously forced the all-trades P/L and
    # aggregate graph. The Stats filter is now the sole owner of both displays.
    html = html.replace(
        "setPnl(document.getElementById('pnl'),s.estimated_total_pnl);",
        ""
    )
    html = html.replace("renderPnlChart(d.pnl_history||[]);", "")

    html = html.replace(
        '<div class="label">Live portfolio P/L</div>',
        '<div class="label" id="pnlChartLabel">Portfolio P/L · LIVE</div>',
        1,
    )

    load_anchor = "const d=await r.json(),s=d.status;"
    load_replacement = (
        "const d=await r.json(),s=d.status;"
        "window.dashboardFilteredPnl=d.filtered_pnl||window.dashboardFilteredPnl||{};"
        "renderFilteredPnl();"
    )
    html = html.replace(load_anchor, load_replacement, 1)

    js = r"""
window.dashboardFilteredPnl=window.dashboardFilteredPnl||{};

function renderFilteredPnl(){
 const mode=(localStorage.getItem('dashboardStatsFilter')||'live').toLowerCase();
 const p=(window.dashboardFilteredPnl||{})[mode];
 if(!p)return;
 const card=document.getElementById('pnl');
 if(card)setPnl(card,p.total_pnl);
 const label=document.getElementById('pnlChartLabel');
 if(label)label.textContent='Portfolio P/L · '+mode.toUpperCase();
 if(typeof refreshPnlChartRange==='function')refreshPnlChartRange();
}

document.addEventListener('click',function(ev){
 if(ev.target.closest('[data-stats-filter]')){
  setTimeout(renderFilteredPnl,0);
 }
});
"""
    html = html.replace("</script>", js + "\n</script>", 1)
    dashboard.DASHBOARD_HTML = html


_install_filtered_pnl_ui()
