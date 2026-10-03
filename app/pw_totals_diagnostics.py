from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app import pw_export_ingest as pwexp

_STARTED = False
_THREAD: threading.Thread | None = None


def _num(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def _decimal_price(value: Any) -> float | None:
    n = _num(value)
    if n is None or n == 0:
        return None
    if 1.01 <= n <= 10.0:
        return n
    if n > 0:
        return 1.0 + n / 100.0
    return 1.0 + 100.0 / abs(n)


def start() -> None:
    global _STARTED, _THREAD
    if _STARTED:
        return
    _STARTED = True

    export_url = os.getenv("PW_WNBA_EXPORT_URL", "").strip()
    proxy = os.getenv("PW_EXPORT_SOCKS_PROXY", "socks5://127.0.0.1:1055").strip()
    peer = os.getenv("PW_TAILNET_PEER", "").strip()
    if not export_url:
        return
    parsed = urlsplit(export_url)
    calls_url = urlunsplit((parsed.scheme, parsed.netloc, "/api/research/totals/calls", "", ""))

    def run() -> None:
        time.sleep(14.0)
        try:
            rows: list[dict[str, Any]] = []
            route = ""
            for page in range(1, 100):
                payload, route = pwexp._tailnet_https_json(
                    calls_url,
                    {"league": "wnba", "page": page, "per_page": 500},
                    proxy,
                    peer,
                    timeout=45.0,
                    user_agent="railway-wnba-totals-diagnostic/1",
                )
                page_rows = payload.get("calls") if isinstance(payload, dict) else None
                if not isinstance(page_rows, list):
                    raise RuntimeError("calls payload missing calls[]")
                rows.extend(r for r in page_rows if isinstance(r, dict))
                pag = payload.get("pagination") or {}
                pages = int(pag.get("pages") or 0)
                if not page_rows or (pages and page >= pages) or (not pages and len(page_rows) < 500):
                    break

            prices = [r.get("total_over_price") for r in rows if r.get("total_over_price") is not None]
            decimals = [_decimal_price(x) for x in prices]
            valid_results = [r for r in rows if str(r.get("over_result") or "").upper() in {"W", "L", "P"}]
            edge8 = [r for r in rows if (_num(r.get("pace_edge_vs_live_total")) or -999) >= 8]
            stage = []
            for r in edge8:
                if str(r.get("over_result") or "").upper() not in {"W", "L", "P"}:
                    continue
                d = _decimal_price(r.get("total_over_price"))
                if d is None or d < 1.70:
                    continue
                stage.append(r)
            game_ids = [str(r.get("game_id") or "") for r in rows if r.get("game_id") not in (None, "")]
            diag = {
                "route": route,
                "rows": len(rows),
                "edge_present": sum(_num(r.get("pace_edge_vs_live_total")) is not None for r in rows),
                "edge8": len(edge8),
                "valid_results": len(valid_results),
                "price_present": len(prices),
                "price_decimal_like": sum(1.01 <= (x or 0) <= 10 for x in (_num(v) for v in prices)),
                "price_american_like": sum(abs(x or 0) > 10 for x in (_num(v) for v in prices)),
                "decimal_gte_170": sum(d is not None and d >= 1.70 for d in decimals),
                "edge8_result_price_floor": len(stage),
                "game_id_present": len(game_ids),
                "unique_game_ids": len(set(game_ids)),
                "result_values": Counter(str(r.get("over_result")) for r in rows).most_common(8),
                "price_examples": [str(x) for x in prices[:12]],
                "edge_examples": [str(r.get("pace_edge_vs_live_total")) for r in rows[:12]],
            }
            print("PW_TOTALS_REMOTE_DIAG " + json.dumps(diag, separators=(",", ":")), flush=True)
        except Exception as exc:
            print(f"PW_TOTALS_REMOTE_DIAG_ERROR {type(exc).__name__}:{exc}", flush=True)

    _THREAD = threading.Thread(target=run, name="wnba-totals-diagnostic", daemon=True)
    _THREAD.start()
