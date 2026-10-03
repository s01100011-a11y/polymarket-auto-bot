from __future__ import annotations

import hashlib
import os
import re
from decimal import Decimal
from typing import Any

import uvicorn

from app import wnba_pw_research_v13 as composite
from app import cfb_capper_preview as cfb


# Telegram commonly uses PITT while Polymarket indexes the school as Pittsburgh.
cfb._CFB_CANONICAL_HINTS["pitt"] = "Pittsburgh"

_ORIGINAL_FIND_SPREAD_ALTERNATIVES = cfb._find_spread_alternatives
_ORIGINAL_REFRESH_UNMATCHED_RECORD = cfb._refresh_unmatched_record


def _find_five_better_live_spread_options(
    pick: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    alternatives = _ORIGINAL_FIND_SPREAD_ALTERNATIVES(pick, limit=50)
    if not alternatives:
        return []
    live_rows = [
        row for row in alternatives
        if str(row.get("event_phase") or "").upper() == "LIVE"
    ]
    if live_rows:
        return [
            row for row in live_rows
            if str(row.get("relative_to_original") or "").upper() == "BETTER"
        ][:5]
    return alternatives[: max(1, min(int(limit or 5), 5))]


cfb._find_spread_alternatives = _find_five_better_live_spread_options


def _plain_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _market_total_line(market: Any) -> Decimal | None:
    raw = getattr(getattr(market, "sports", None), "line", None)
    if raw is not None:
        try:
            return Decimal(str(raw))
        except Exception:
            return None
    question = str(getattr(market, "question", "") or "")
    match = re.search(r"\b(?:over|under|total)\s*[:(]?\s*(\d{1,3}(?:\.\d+)?)", question, re.I)
    if not match:
        return None
    try:
        return Decimal(match.group(1))
    except Exception:
        return None


def _find_five_better_live_total_options(
    pick: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Return up to five safer open full-game totals for the same CFB event."""
    hints = cfb._event_hints_for_pick(pick, "total")
    if len(hints) != 2:
        return []
    side = str(pick.get("total_side") or "").upper()
    if side not in {"OVER", "UNDER"}:
        return []
    try:
        original = Decimal(str(pick.get("total_line")))
    except Exception:
        return []

    rows: list[tuple[Decimal, Any, Any, str, Any, Decimal]] = []
    with cfb.PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in hints:
            result = client.list_events(
                title_search=cfb._canonical_cfb_hint(query),
                closed=False,
                page_size=30,
            ).first_page()
            for event in result.items:
                key = cfb._event_key(event)
                if key:
                    events_by_key[key] = event

        for event in events_by_key.values():
            slug = cfb.nfl._norm(getattr(event, "slug", ""))
            if not slug.startswith("cfb-"):
                continue
            etext = " ".join([
                str(getattr(event, "title", "") or ""),
                str(getattr(event, "slug", "") or ""),
            ])
            if not all(cfb._contains_hint(etext, hint) for hint in hints):
                continue

            for market in getattr(event, "markets", ()) or ():
                actual = cfb.nfl._market_type(market)
                if not cfb._full_game_market_type_matches(actual, "total"):
                    continue
                if not getattr(getattr(market, "state", None), "accepting_orders", False):
                    continue
                line = _market_total_line(market)
                if line is None or line == original:
                    continue
                better = line > original if side == "UNDER" else line < original
                if not better:
                    continue
                trial = dict(pick)
                trial["total_line"] = line
                selected = cfb._select_outcome(market, trial, "total")
                if selected is None:
                    continue
                label, obj = selected
                rows.append((abs(line - original), event, market, label, obj, line))

    if not rows:
        return []
    event_keys = {cfb._event_key(row[1]) for row in rows}
    event_keys.discard("")
    if len(event_keys) != 1:
        return []

    rows.sort(key=lambda row: row[0])
    alternatives: list[dict[str, Any]] = []
    seen_lines: set[str] = set()
    for _, event, market, label, obj, line in rows:
        line_text = _plain_decimal(line)
        if line_text in seen_lines:
            continue
        seen_lines.add(line_text)
        asset_id = str(
            getattr(obj, "token_id", None)
            or getattr(obj, "position_id", None)
            or ""
        )
        if not asset_id:
            continue
        try:
            quote = cfb._read_live_buy_quote(asset_id)
        except Exception:
            continue
        event_slug = str(getattr(event, "slug", "") or "")
        if not event_slug.startswith("cfb-"):
            continue
        lifecycle = cfb._event_lifecycle_metadata(event, market)
        display_line = f"{side} {line_text}"
        alt_id = "total-view-" + hashlib.sha256(
            f"{asset_id}|{side}|{line_text}".encode("utf-8")
        ).hexdigest()[:16]
        match = {
            "alternative_id": alt_id,
            "match_status": "ALTERNATE",
            "market_type": "total",
            "event_slug": event_slug,
            "event_title": str(getattr(event, "title", "") or ""),
            **lifecycle,
            "market": str(getattr(market, "question", "") or getattr(event, "title", "CFB total")),
            "market_url": f"https://polymarket.com/sports/cfb/{event_slug}",
            "outcome": label,
            "asset_id": asset_id,
            "spread_line": display_line,
            "total_side": side,
            "total_line": line_text,
            "original_total_line": _plain_decimal(original),
            "relative_to_original": "BETTER",
            **quote,
        }
        match["event_phase"] = cfb._event_phase(match)
        alternatives.append(match)
        if len(alternatives) >= max(1, min(int(limit or 5), 5)):
            break
    return alternatives


cfb._find_total_alternatives = _find_five_better_live_total_options


def _refresh_unmatched_with_live_totals(record: dict[str, Any]) -> bool:
    changed = _ORIGINAL_REFRESH_UNMATCHED_RECORD(record)
    pick = cfb._persisted_record_pick(record)
    if not isinstance(pick, dict):
        return changed
    kind, _ = cfb._classify_pick(pick)
    if kind != "total" or cfb._saved_market_match(record) is not None:
        return changed
    phase = str(record.get("espn_phase") or record.get("event_phase") or "").upper()
    if phase != "LIVE" or str(record.get("status") or "").upper() == "EVENT_CLOSED":
        return changed

    try:
        alternatives = _find_five_better_live_total_options(pick, limit=5)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if record.get("alternate_error") != error:
            record["alternate_error"] = error
            return True
        return changed

    if not alternatives:
        reason = "game is live on ESPN; exact total is unavailable and no safer live total is currently open"
        if record.get("reason") != reason:
            record["reason"] = reason
            changed = True
        return changed

    first = alternatives[0]
    updates = {
        "live_alternatives": alternatives,
        "alternate_error": None,
        "match_status": "ALTERNATE_AVAILABLE",
        "event_title": first.get("event_title"),
        "event_start_at": first.get("event_start_at"),
        "event_phase": "LIVE",
        "market_url": first.get("market_url"),
        "status": "MATCHED_LIVE_ALTERNATE",
        "reason": "exact original total is unavailable; safer current Polymarket live totals are shown",
    }
    for key, value in updates.items():
        if record.get(key) != value:
            record[key] = value
            changed = True
    return changed


cfb._refresh_unmatched_record = _refresh_unmatched_with_live_totals


# The existing CFB card renderer expects live_alternatives. For total rows, make
# the option open the matched Polymarket event rather than send it through the
# spread-only alternate endpoint.
_OLD_ALT_FN = "async function cfbManualAltBuy(signalId,altId,btn){\n const original=btn.textContent;"
_NEW_ALT_FN = "async function cfbManualAltBuy(signalId,altId,btn){\n if(String(altId||'').startsWith('total-view-')){const card=btn.closest('.capper-pick')||btn.parentElement;const link=card&&card.querySelector?card.querySelector('a[href^=\"https://polymarket.com/\"]'):null;if(link){window.open(link.href,'_blank','noopener');return;}}\n const original=btn.textContent;"
if _OLD_ALT_FN in composite.dashboard.DASHBOARD_HTML:
    composite.dashboard.DASHBOARD_HTML = composite.dashboard.DASHBOARD_HTML.replace(_OLD_ALT_FN, _NEW_ALT_FN, 1)
composite.dashboard.DASHBOARD_HTML = composite.dashboard.DASHBOARD_HTML.replace(
    ">BUY '+cfbEsc(alt.spread_line)+' @ '+odds+relative+'</button>'",
    ">'+(String(alt.alternative_id||'').startsWith('total-view-')?'OPEN ':'BUY ')+cfbEsc(alt.spread_line)+' @ '+odds+relative+'</button>'",
    1,
)


def _refresh_existing_live_total_rows() -> None:
    signal_file = composite.core.DATA_DIR / "cfb_capper_preview_signals.json"
    signals = composite.core._load(signal_file)
    if not isinstance(signals, dict):
        return
    changed = False
    for signal_id, record in signals.items():
        if not isinstance(record, dict):
            continue
        pick = cfb._persisted_record_pick(record)
        if not isinstance(pick, dict):
            continue
        kind, _ = cfb._classify_pick(pick)
        if kind != "total":
            continue
        phase = str(record.get("espn_phase") or record.get("event_phase") or "").upper()
        if phase != "LIVE":
            continue
        try:
            if _refresh_unmatched_with_live_totals(record):
                record["updated_at"] = cfb._now_iso()
                signals[signal_id] = record
                changed = True
                print(
                    "CFB_TOTAL_FORCE_REFRESH "
                    + str({
                        "id": signal_id,
                        "selection": record.get("selection"),
                        "status": record.get("status"),
                        "alternatives": [
                            {"line": alt.get("spread_line"), "price": alt.get("best_ask")}
                            for alt in (record.get("live_alternatives") or [])
                        ],
                    }),
                    flush=True,
                )
        except Exception as exc:
            print(f"CFB_TOTAL_FORCE_REFRESH_ERROR id={signal_id} error={type(exc).__name__}: {exc}", flush=True)
    if changed:
        composite.core._save(signal_file, signals)


_refresh_existing_live_total_rows()


if __name__ == "__main__":
    uvicorn.run(
        composite.app,
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
    )
