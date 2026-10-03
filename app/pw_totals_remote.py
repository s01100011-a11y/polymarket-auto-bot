from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from fastapi import Depends, Query

from app import pw_export_ingest as pwexp

_INSTALLED = False
_BOOT_THREAD: threading.Thread | None = None
DEFAULT_THRESHOLDS = (4.0, 6.0, 8.0, 10.0, 12.0)
DEFAULT_MIN_DECIMAL_ODDS = 1.70
MIN_ENRICHMENT_COVERAGE = 0.80


def _finite_float(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _american_to_decimal(value: Any) -> float | None:
    try:
        n = float(str(value).replace("+", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(n) or n == 0:
        return None
    if 1.01 <= n <= 10.0:
        return n
    if n > 0:
        return 1.0 + n / 100.0
    return 1.0 + 100.0 / abs(n)


def _parse_ts(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.max.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _same_monitor_url(export_url: str, path: str) -> str:
    parsed = urlsplit(export_url)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise RuntimeError("PW_WNBA_EXPORT_URL must be a valid https URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/" + path.lstrip("/"), "", ""))


def _enrichment_status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    enriched = [
        row
        for row in rows
        if _finite_float(row.get("pace_edge_vs_live_total")) is not None
        and str(row.get("over_result") or "").upper() in {"W", "L", "P"}
        and _american_to_decimal(row.get("total_over_price")) is not None
    ]
    coverage = len(enriched) / len(rows) if rows else 0.0
    return {
        "rows": len(rows),
        "enriched_rows": len(enriched),
        "coverage_pct": round(coverage * 100.0, 2),
        "sufficient": coverage >= MIN_ENRICHMENT_COVERAGE,
    }


def _candidate_bets(rows: list[dict[str, Any]], threshold: float, min_decimal_odds: float | None, first_per_game: bool = True) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for row in rows:
        edge = _finite_float(row.get("pace_edge_vs_live_total"))
        if edge is None or edge < threshold:
            continue
        result = str(row.get("over_result") or "").upper()
        if result not in {"W", "L", "P"}:
            continue
        decimal_odds = _american_to_decimal(row.get("total_over_price"))
        if decimal_odds is None or decimal_odds <= 1.0:
            continue
        if min_decimal_odds is not None and decimal_odds < min_decimal_odds:
            continue
        candidates.append({
            "game_id": str(row.get("game_id") or ""),
            "call_ts": row.get("call_ts"),
            "decimal_odds": decimal_odds,
            "result": result,
        })
    candidates.sort(key=lambda bet: _parse_ts(bet.get("call_ts")))
    if not first_per_game:
        return candidates
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for bet in candidates:
        gid = bet.get("game_id") or ""
        if not gid or gid in seen:
            continue
        seen.add(gid)
        out.append(bet)
    return out


def _summary(bets: list[dict[str, Any]]) -> dict[str, Any]:
    wins = sum(b["result"] == "W" for b in bets)
    losses = sum(b["result"] == "L" for b in bets)
    pushes = sum(b["result"] == "P" for b in bets)
    units_risked = 0.0
    units_won = 0.0
    prices: list[float] = []
    for bet in bets:
        decimal_odds = float(bet["decimal_odds"])
        stake = 1.0 / (decimal_odds - 1.0)
        prices.append(decimal_odds)
        units_risked += stake
        if bet["result"] == "W":
            units_won += 1.0
        elif bet["result"] == "L":
            units_won -= stake
    decisions = wins + losses
    return {
        "bets": len(bets),
        "wins": wins,
        "losses": losses,
        "pushes": pushes,
        "win_pct_ex_push": round(wins / decisions * 100.0, 2) if decisions else None,
        "units_risked": round(units_risked, 4),
        "units_won": round(units_won, 4),
        "roi_pct": round(units_won / units_risked * 100.0, 2) if units_risked else None,
        "avg_decimal_odds": round(sum(prices) / len(prices), 4) if prices else None,
    }


def _holdout(bets: list[dict[str, Any]], train_fraction: float) -> dict[str, Any]:
    ordered = sorted(bets, key=lambda bet: _parse_ts(bet.get("call_ts")))
    if len(ordered) < 2:
        return {"train": _summary(ordered), "test": _summary([])}
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * train_fraction)))
    return {
        "train": _summary(ordered[:cut]),
        "test": _summary(ordered[cut:]),
        "train_end": ordered[cut - 1].get("call_ts"),
        "test_start": ordered[cut].get("call_ts"),
    }


def analyze_to_win_one(rows: list[dict[str, Any]], thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS, min_decimal_odds: float | None = DEFAULT_MIN_DECIMAL_ODDS, first_per_game: bool = True) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for threshold in thresholds:
        bets = _candidate_bets(rows, threshold, min_decimal_odds, first_per_game)
        results[f"{threshold:g}+"] = {
            "summary": _summary(bets),
            "holdout_70_30": _holdout(bets, 0.70),
            "holdout_80_20": _holdout(bets, 0.80),
        }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "WNBA OVER pace-edge",
        "staking": "to_win_1u",
        "first_per_game": first_per_game,
        "min_decimal_odds": min_decimal_odds,
        "source_rows": len(rows),
        "thresholds": results,
    }


def install(*, app: Any, dashboard: Any, core: Any) -> None:
    global _INSTALLED, _BOOT_THREAD
    if _INSTALLED:
        return
    _INSTALLED = True

    export_url = os.getenv("PW_WNBA_EXPORT_URL", "").strip()
    proxy = os.getenv("PW_EXPORT_SOCKS_PROXY", "socks5://127.0.0.1:1055").strip()
    tailnet_peer = os.getenv("PW_TAILNET_PEER", "").strip()
    if not export_url:
        print("PW_TOTALS_REMOTE_READY enabled=false reason=PW_WNBA_EXPORT_URL_missing", flush=True)
        return

    report_url = _same_monitor_url(export_url, "/api/research/totals")
    calls_url = _same_monitor_url(export_url, "/api/research/totals/calls")
    result_file = core.DATA_DIR / "wnba_totals_remote_test.json"

    def fetch_json(url: str, params: dict[str, Any]) -> tuple[Any, str]:
        return pwexp._tailnet_https_json(url, params, proxy, tailnet_peer, timeout=45.0, user_agent="railway-wnba-totals-research/1")

    def fetch_all_calls(start_date: str | None = None, end_date: str | None = None) -> tuple[list[dict[str, Any]], str]:
        rows: list[dict[str, Any]] = []
        route = ""
        for page in range(1, 101):
            params: dict[str, Any] = {"league": "wnba", "page": page, "per_page": 500}
            if start_date:
                params["start_date"] = start_date
            if end_date:
                params["end_date"] = end_date
            payload, route = fetch_json(calls_url, params)
            if not isinstance(payload, dict) or not isinstance(payload.get("calls"), list):
                raise RuntimeError("WNBA totals calls payload is invalid")
            page_rows = [r for r in payload["calls"] if isinstance(r, dict)]
            rows.extend(page_rows)
            pagination = payload.get("pagination") or {}
            pages = int(pagination.get("pages") or 0)
            if not page_rows or (pages and page >= pages) or (not pages and len(page_rows) < 500):
                return rows, route
        raise RuntimeError("WNBA totals calls exceeded pagination safety limit")

    def run_test(thresholds: tuple[float, ...], min_decimal_odds: float, first_per_game: bool, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
        rows, route = fetch_all_calls(start_date, end_date)
        enrichment = _enrichment_status(rows)
        if not enrichment["sufficient"]:
            result = {
                "status": "insufficient_enrichment",
                "message": "Call-level totals cache is incomplete; exact staking results are intentionally blocked.",
                "enrichment": enrichment,
                "route": route,
                "source_endpoint": "/api/research/totals/calls?league=wnba",
            }
            core._save(result_file, result)
            return result
        result = analyze_to_win_one(rows, thresholds, min_decimal_odds, first_per_game)
        result["status"] = "ok"
        result["enrichment"] = enrichment
        result["route"] = route
        result["source_endpoint"] = "/api/research/totals/calls?league=wnba"
        core._save(result_file, result)
        return result

    @app.get("/api/pw-research/wnba-totals/report", dependencies=[Depends(dashboard._auth)])
    def wnba_totals_report():
        payload, route = fetch_json(report_url, {"league": "wnba"})
        return {"route": route, "upstream": payload}

    @app.get("/api/pw-research/wnba-totals/calls", dependencies=[Depends(dashboard._auth)])
    def wnba_totals_calls(page: int = Query(default=1, ge=1), per_page: int = Query(default=100, ge=1, le=1000), start_date: str | None = None, end_date: str | None = None):
        params: dict[str, Any] = {"league": "wnba", "page": page, "per_page": per_page}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        payload, route = fetch_json(calls_url, params)
        return {"route": route, "upstream": payload}

    @app.get("/api/pw-research/wnba-totals/test", dependencies=[Depends(dashboard._auth)])
    def wnba_totals_test(thresholds: str = "4,6,8,10,12", min_decimal_odds: float = Query(default=1.70, ge=1.01, le=10.0), first_per_game: bool = True, start_date: str | None = None, end_date: str | None = None):
        parsed = tuple(sorted({float(part.strip()) for part in thresholds.split(",") if part.strip()}))
        if not parsed or len(parsed) > 20 or any(x < 0 or x > 100 for x in parsed):
            raise ValueError("thresholds must contain 1-20 values between 0 and 100")
        return run_test(parsed, min_decimal_odds, first_per_game, start_date, end_date)

    def boot_test() -> None:
        time.sleep(10.0)
        try:
            result = run_test(DEFAULT_THRESHOLDS, DEFAULT_MIN_DECIMAL_ODDS, True)
            if result.get("status") != "ok":
                print("PW_TOTALS_REMOTE_TEST_BLOCKED " + json.dumps(result, separators=(",", ":")), flush=True)
                return
            compact = {key: value.get("summary", {}) for key, value in (result.get("thresholds") or {}).items()}
            print("PW_TOTALS_REMOTE_TEST_OK " + json.dumps({"source_rows": result.get("source_rows"), "min_decimal_odds": result.get("min_decimal_odds"), "thresholds": compact}, separators=(",", ":")), flush=True)
        except Exception as exc:
            print(f"PW_TOTALS_REMOTE_TEST_ERROR {type(exc).__name__}:{exc}", flush=True)

    _BOOT_THREAD = threading.Thread(target=boot_test, name="wnba-totals-remote-test", daemon=True)
    _BOOT_THREAD.start()
    print("PW_TOTALS_REMOTE_READY endpoints=/api/pw-research/wnba-totals/{report,calls,test}", flush=True)
