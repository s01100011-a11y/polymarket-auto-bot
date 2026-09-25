from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx
from fastapi import Depends
from polymarket import PublicClient

from app import dashboard_live_control_v4 as live_control
from app import nfl_capper_ingest as nfl


TARGET_SOURCES = {
    "SLAM - All Access": "Slam - CFB",
    "The Syndicate": "Syndicate - CFB",
}
SOURCE_LABELS = ("Slam - CFB", "Syndicate - CFB")
SOURCE_ID_ENV = {
    "Slam - CFB": "CFB_CAPPER_SLAM_SOURCE_IDS",
    "Syndicate - CFB": "CFB_CAPPER_SYNDICATE_SOURCE_IDS",
}

_INSTALLED = False
_STATUS: dict[str, Any] = {
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "cycles": 0,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _configured_source_ids(label: str) -> set[str]:
    return {
        item.strip()
        for item in os.getenv(SOURCE_ID_ENV[label], "").split(",")
        if item.strip()
    }


def _source_label(pick: dict[str, Any]) -> str | None:
    source = str(pick.get("source") or "").strip()
    exact = TARGET_SOURCES.get(source)
    if exact is not None:
        return exact

    source_key = _compact(pick.get("source_key"))
    if source_key in {"slam", "slamcfb", "slamncaaf"}:
        return "Slam - CFB"
    if source_key in {"syndicate", "thesyndicate", "syndicatecfb", "syndicatencaaf"}:
        return "Syndicate - CFB"

    compact = _compact(source)
    if compact.startswith("slam") or "slamthebookie" in compact:
        return "Slam - CFB"
    if "syndicate" in compact:
        return "Syndicate - CFB"

    source_id = str(pick.get("source_id") or "").strip()
    if source_id:
        for label in SOURCE_LABELS:
            if source_id in _configured_source_ids(label):
                return label
    return None


def _units_for_pick(pick: dict[str, Any]) -> Decimal:
    return nfl._units_for_pick(pick)


def _stake_for_pick(pick: dict[str, Any], unit_usdc: Decimal = Decimal("10")) -> Decimal:
    return nfl._stake_for_pick(pick, unit_usdc)


def _fingerprint(pick: dict[str, Any]) -> str:
    canonical = {
        "source": str(pick.get("source") or ""),
        "posted_at": str(pick.get("posted_at") or ""),
        "selection": str(pick.get("selection") or ""),
        "team_hint": str(pick.get("team_hint") or ""),
        "event_hints": list(pick.get("event_hints") or []),
        "bet_types": sorted(str(x) for x in (pick.get("bet_types") or [])),
        "period": pick.get("period"),
        "spread_lines": list(pick.get("spread_lines") or []),
        "total_side": pick.get("total_side"),
        "total_line": pick.get("total_line"),
        "units": pick.get("units"),
    }
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _classify_pick(pick: dict[str, Any]) -> tuple[str | None, str | None]:
    if _source_label(pick) is None:
        return None, "untracked source"
    if str(pick.get("status") or "active").lower() != "active":
        return None, "pick is not active"

    types = {str(x).lower() for x in (pick.get("bet_types") or [])}
    if "team_total" in types:
        return None, "team totals are not supported"
    if pick.get("period") or "period" in types:
        return None, "period/half/quarter markets are not supported"
    if "prop" in types:
        return None, "props are not supported"

    selection = str(pick.get("selection") or "").strip()
    if len(selection) > 90:
        return None, "selection is too verbose/ambiguous"

    primary = types & {"moneyline", "spread", "total"}
    if len(primary) != 1:
        return None, "pick does not resolve to exactly one supported full-game market"

    kind = next(iter(primary))
    team_hint = str(pick.get("team_hint") or "").strip()
    event_hints = [str(x).strip() for x in (pick.get("event_hints") or []) if str(x).strip()]

    if kind == "moneyline" and not team_hint:
        return None, "moneyline pick has no selected college team"
    if kind == "spread":
        lines = list(pick.get("spread_lines") or [])
        if not team_hint or len(lines) != 1:
            return None, "spread pick needs one selected team and one spread line"
    if kind == "total":
        if len(event_hints) != 2:
            return None, "full-game total needs one explicit two-team matchup"
        if pick.get("total_line") is None or str(pick.get("total_side") or "").upper() not in {"OVER", "UNDER"}:
            return None, "total pick needs a side and total line"
    return kind, None


def _norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _contains_hint(text: Any, hint: Any) -> bool:
    haystack = " " + _norm_text(text) + " "
    needle = _norm_text(hint)
    return bool(needle and (" " + needle + " ") in haystack)


def _event_hints_for_pick(pick: dict[str, Any], kind: str) -> list[str]:
    if kind == "total":
        return [str(x).strip() for x in (pick.get("event_hints") or []) if str(x).strip()]
    hint = str(pick.get("team_hint") or "").strip()
    return [hint] if hint else []


def _select_outcome(market: Any, pick: dict[str, Any], kind: str) -> tuple[str, Any] | None:
    outcomes = [(label, obj) for label, obj in nfl._outcomes(market) if obj is not None]
    if len(outcomes) != 2:
        return None

    mtext = nfl._market_text(market)
    team_hint = str(pick.get("team_hint") or "").strip()

    if kind == "moneyline":
        for label, obj in outcomes:
            if _contains_hint(label, team_hint):
                return label, obj
        if _contains_hint(mtext, team_hint) and nfl._norm(outcomes[0][0]) == "yes":
            return outcomes[0]
        return None

    if kind == "spread":
        line = list(pick.get("spread_lines") or [None])[0]
        if not nfl._line_matches(mtext, line, signed=True):
            return None
        for label, obj in outcomes:
            if _contains_hint(label, team_hint) and nfl._line_matches(label + " " + mtext, line, signed=True):
                return label, obj
        if _contains_hint(mtext, team_hint) and nfl._norm(outcomes[0][0]) == "yes":
            return outcomes[0]
        return None

    if kind == "total":
        side = str(pick.get("total_side") or "").upper()
        line = pick.get("total_line")
        structured = nfl._structured_line_matches(market, line)
        if structured is False:
            return None
        if structured is None and not nfl._line_matches(mtext, line, signed=False):
            return None
        for label, obj in outcomes:
            if side.lower() in nfl._norm(label):
                return label, obj
        yes, no = outcomes
        if nfl._norm(yes[0]) == "yes" and nfl._norm(no[0]) == "no":
            has_over = bool(re.search(r"(?<![a-z])over(?![a-z])", mtext))
            has_under = bool(re.search(r"(?<![a-z])under(?![a-z])", mtext))
            if has_over and not has_under:
                return yes if side == "OVER" else no
            if has_under and not has_over:
                return yes if side == "UNDER" else no
        return None

    return None


def _find_market(pick: dict[str, Any], kind: str) -> tuple[Any, Any, str, Any]:
    hints = _event_hints_for_pick(pick, kind)
    if not hints:
        raise ValueError("No college-football event hints are available")

    ranked: list[tuple[int, Any, Any, str, Any]] = []
    with PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in hints:
            result = client.list_events(title_search=query, closed=False, page_size=30).first_page()
            for event in result.items:
                key = str(
                    getattr(event, "id", "")
                    or getattr(event, "slug", "")
                    or getattr(event, "title", "")
                )
                if key:
                    events_by_key[key] = event

        for event in events_by_key.values():
            slug = nfl._norm(getattr(event, "slug", ""))
            if not slug.startswith("cfb-"):
                continue
            etext = " ".join([
                str(getattr(event, "title", "") or ""),
                str(getattr(event, "slug", "") or ""),
            ])
            if not all(_contains_hint(etext, hint) for hint in hints):
                continue

            for market in getattr(event, "markets", ()) or ():
                actual = nfl._market_type(market)
                if not nfl._type_matches(actual, kind):
                    continue
                outcome = _select_outcome(market, pick, kind)
                if outcome is None:
                    continue
                label, obj = outcome
                score = 10
                if nfl._norm(actual) == kind:
                    score += 4
                if getattr(getattr(market, "state", None), "accepting_orders", False):
                    score += 4
                if kind != "total" and _contains_hint(nfl._market_text(market) + " " + label, hints[0]):
                    score += 2
                ranked.append((score, event, market, label, obj))

    if not ranked:
        raise ValueError(
            f"No exact open Polymarket CFB {kind} market matched '{pick.get('selection')}'"
        )

    event_keys = {
        str(
            getattr(row[1], "id", "")
            or getattr(row[1], "slug", "")
            or getattr(row[1], "title", "")
        )
        for row in ranked
    }
    event_keys.discard("")
    if len(event_keys) != 1:
        raise ValueError("CFB pick matched multiple open events; preview blocked")

    ranked.sort(key=lambda row: row[0], reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best[0]:
        first_id = str(getattr(best[2], "id", "") or getattr(best[2], "slug", ""))
        second_id = str(getattr(ranked[1][2], "id", "") or getattr(ranked[1][2], "slug", ""))
        if first_id != second_id:
            raise ValueError("Multiple equally strong CFB markets matched; preview blocked")
    return best[1], best[2], best[3], best[4]


def _prepare_preview(
    pick: dict[str, Any],
    *,
    core: Any,
    remote: Any,
    unit_usdc: Decimal,
) -> dict[str, Any]:
    kind, reason = _classify_pick(pick)
    if kind is None:
        raise RuntimeError(reason or "unsupported CFB pick")

    ready, executor_state = live_control._executor_ready()
    if not ready:
        if executor_state.get("geo_blocked"):
            raise RuntimeError("Termux executor is geoblocked")
        raise RuntimeError("Termux executor is offline")

    units = _units_for_pick(pick)
    stake = _stake_for_pick(pick, unit_usdc)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            f"Requested {units}u = ${stake} exceeds MAX_AUTO_TRADE_USDC=${core.MAX_AUTO_TRADE_USDC}"
        )

    used = core._daily_budget_used()
    pending = nfl._pending_auto_budget(remote)
    if used + pending + stake > core.MAX_DAILY_BUDGET_USDC:
        raise RuntimeError(
            "Daily auto budget guard would be exceeded: "
            f"used=${used}, pending=${pending}, requested=${stake}, "
            f"limit=${core.MAX_DAILY_BUDGET_USDC}"
        )

    event, market, outcome_label, outcome_obj = _find_market(pick, kind)
    asset_id = str(
        getattr(outcome_obj, "token_id", None)
        or getattr(outcome_obj, "position_id", None)
        or ""
    )
    if not asset_id:
        raise RuntimeError("Matched CFB market has no tradable outcome token")

    with PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        book = client.get_order_book(asset_id=asset_id)

    asks = getattr(book, "asks", None) or []
    best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    if buy_price <= 0 or buy_price >= 1:
        raise RuntimeError(f"Invalid current BUY price {buy_price}")
    if best_ask is None:
        raise RuntimeError("No current ask is available")
    if best_ask > core.MAX_PRICE:
        raise RuntimeError(f"Current best ask {best_ask} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise RuntimeError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug.startswith("cfb-"):
        raise RuntimeError("Matched event is not a CFB Polymarket event")

    source_label = _source_label(pick)
    fp = _fingerprint(pick)
    payload = {
        "market_url": f"https://polymarket.com/sports/cfb/{event_slug}",
        "outcome": outcome_label,
        "market_type": kind,
        "asset_id": asset_id,
        "max_price": str(best_ask),
        "budget_usdc": str(stake),
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "cfb_capper_preview",
        "auto": False,
        "strategy_source": source_label,
        "strategy_sport": "CFB",
        "strategy_units": str(units),
        "strategy_unit_usdc": str(unit_usdc),
        "strategy_pick_id": fp,
        "strategy_posted_at": pick.get("posted_at"),
        "strategy_selection": pick.get("selection"),
        "strategy_telegram_source": pick.get("source"),
    }
    queued = remote._enqueue("PREVIEW", payload)
    return {
        "status": "PREVIEW_QUEUED",
        "request_id": queued["id"],
        "strategy_source": source_label,
        "market_type": kind,
        "market": str(
            getattr(market, "question", "")
            or getattr(event, "title", "CFB market")
        ),
        "market_url": payload["market_url"],
        "outcome": outcome_label,
        "asset_id": asset_id,
        "units": str(units),
        "stake_usdc": str(stake),
        "current_buy_price": str(buy_price),
        "best_ask": str(best_ask),
        "max_price": str(best_ask),
        "spread": str(spread),
    }


def install(*, app: Any, dashboard: Any, core: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    remote = live_control.remote
    signal_file = core.DATA_DIR / "cfb_capper_preview_signals.json"
    last_feed_file = core.DATA_DIR / "cfb_capper_last_feed.json"
    bridge_url = os.getenv(
        "CFB_CAPPER_BRIDGE_URL",
        "https://telegram-chatgpt-bridge-production-286a.up.railway.app",
    ).rstrip("/")
    enabled = os.getenv("CFB_CAPPER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    poll_seconds = max(5, int(os.getenv("CFB_CAPPER_POLL_SECONDS", "15")))
    max_age_seconds = max(30, int(os.getenv("CFB_CAPPER_MAX_PICK_AGE_SECONDS", "180")))
    unit_usdc = Decimal(os.getenv("CFB_CAPPER_UNIT_USDC", "10"))
    test_preview_raw = os.getenv("CFB_CAPPER_TEST_PREVIEW_JSON", "").strip()
    test_preview_marker = core.DATA_DIR / "cfb_capper_test_preview.json"

    def _load_signals() -> dict[str, Any]:
        data = core._load(signal_file)
        return data if isinstance(data, dict) else {}

    def _save_signals(data: dict[str, Any]) -> None:
        if len(data) > 2500:
            ordered = sorted(
                data.items(),
                key=lambda item: str((item[1] or {}).get("first_seen_at") or ""),
            )
            data = dict(ordered[-2000:])
        core._save(signal_file, data)

    def _sync_queue_status(signals: dict[str, Any]) -> bool:
        changed = False
        queue = remote._queue_load()
        for rec in signals.values():
            if rec.get("status") != "PREVIEW_QUEUED" or not rec.get("request_id"):
                continue
            queued = queue.get(str(rec.get("request_id")))
            if not queued:
                continue
            qstatus = str(queued.get("status") or "")
            if qstatus == "DONE":
                rec["status"] = "PREVIEW_DONE"
                rec["completed_at"] = queued.get("updated_at") or _now_iso()
                rec["result"] = queued.get("result")
                changed = True
            elif qstatus == "FAILED":
                rec["status"] = "PREVIEW_FAILED"
                rec["last_error"] = queued.get("error")
                rec["completed_at"] = queued.get("updated_at") or _now_iso()
                changed = True
        return changed

    async def _poll_once() -> None:
        _STATUS["last_poll_at"] = _now_iso()
        signals = _load_signals()
        changed = _sync_queue_status(signals)

        if not enabled:
            _STATUS["last_error"] = "CFB capper preview is disabled"
            if changed:
                _save_signals(signals)
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{bridge_url}/public/ncaaf",
                    params={"minutes": 180, "limit": 120, "include_graded": "false"},
                )
                response.raise_for_status()
                feed = response.json()
        except Exception as exc:
            _STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            if changed:
                _save_signals(signals)
            return

        core._save(last_feed_file, {
            "saved_at": _now_iso(),
            "sport": feed.get("sport"),
            "generated_at": feed.get("generated_at"),
            "freshness": feed.get("freshness"),
            "scanned_posts": feed.get("scanned_posts"),
            "detected_posts": feed.get("detected_posts"),
            "detected_picks": feed.get("detected_picks"),
            "listener_connected": feed.get("listener_connected"),
            "listener_ready": feed.get("listener_ready"),
            "picks": feed.get("picks") or [],
        })

        picks = [p for p in (feed.get("picks") or []) if isinstance(p, dict)]
        picks.sort(key=lambda p: str(p.get("posted_at") or ""))
        now = datetime.now(timezone.utc)

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                continue

            source_label = _source_label(pick)
            record = current or {
                "id": fp,
                "first_seen_at": _now_iso(),
                "source": source_label,
                "telegram_source": pick.get("source"),
                "posted_at": pick.get("posted_at"),
                "selection": pick.get("selection"),
                "units": str(_units_for_pick(pick)),
                "stake_usdc": str(_stake_for_pick(pick, unit_usdc)),
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["status"] = "IGNORED_STALE"
                record["reason"] = f"pick age exceeds {max_age_seconds}s freshness limit"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            kind, reason = _classify_pick(pick)
            if kind is None:
                record["status"] = "IGNORED_UNSUPPORTED"
                record["reason"] = reason
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            try:
                prepared = await asyncio.to_thread(
                    _prepare_preview,
                    pick,
                    core=core,
                    remote=remote,
                    unit_usdc=unit_usdc,
                )
                record.update(prepared)
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                print(
                    "CFB_CAPPER_PREVIEW "
                    + json.dumps(record, sort_keys=True, separators=(",", ":"), default=str),
                    flush=True,
                )
            except Exception as exc:
                record["status"] = "RETRYING"
                record["last_error"] = f"{type(exc).__name__}: {exc}"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True

        if changed:
            _save_signals(signals)

        _STATUS["last_success_at"] = _now_iso()
        _STATUS["last_error"] = None
        _STATUS["cycles"] = int(_STATUS.get("cycles") or 0) + 1

    async def _run_test_preview_once() -> None:
        if not test_preview_raw:
            return

        trigger_hash = hashlib.sha256(test_preview_raw.encode("utf-8")).hexdigest()
        existing = core._load(test_preview_marker) if test_preview_marker.exists() else {}
        if (
            isinstance(existing, dict)
            and existing.get("trigger_hash") == trigger_hash
            and existing.get("status") in {"QUEUED", "DONE"}
        ):
            return

        try:
            pick = json.loads(test_preview_raw)
            if not isinstance(pick, dict):
                raise RuntimeError("CFB_CAPPER_TEST_PREVIEW_JSON must decode to an object")
            pick["posted_at"] = _now_iso()

            while True:
                ready, state = live_control._executor_ready()
                if ready:
                    break
                if state.get("geo_blocked"):
                    raise RuntimeError("Termux executor is geoblocked")
                await asyncio.sleep(2)

            result = await asyncio.to_thread(
                _prepare_preview,
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
            )
            marker = {
                "trigger_hash": trigger_hash,
                "created_at": _now_iso(),
                "pick": pick,
                "preview": result,
                "status": "QUEUED",
            }
            core._save(test_preview_marker, marker)
            print(
                "CFB_CAPPER_TEST_PREVIEW "
                + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str),
                flush=True,
            )

            request_id = str(result.get("request_id") or "")
            for _ in range(90):
                await asyncio.sleep(1)
                queued = remote._queue_load().get(request_id) if request_id else None
                if not queued:
                    continue
                status = str(queued.get("status") or "")
                if status not in {"DONE", "FAILED"}:
                    continue
                marker["status"] = status
                marker["completed_at"] = queued.get("updated_at") or _now_iso()
                marker["result"] = queued.get("result")
                marker["error"] = queued.get("error")
                core._save(test_preview_marker, marker)
                print(
                    "CFB_CAPPER_TEST_PREVIEW_RESULT "
                    + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str),
                    flush=True,
                )
                return
        except Exception as exc:
            marker = {
                "trigger_hash": trigger_hash,
                "created_at": _now_iso(),
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
            core._save(test_preview_marker, marker)
            print(
                "CFB_CAPPER_TEST_PREVIEW_RESULT "
                + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str),
                flush=True,
            )

    async def _loop() -> None:
        await asyncio.sleep(4)
        while True:
            try:
                await _poll_once()
            except Exception as exc:
                _STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(poll_seconds)

    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _lifespan(application: Any):
        async with original_lifespan(application):
            task = asyncio.create_task(_loop(), name="cfb-capper-preview-poller")
            preview_task = asyncio.create_task(
                _run_test_preview_once(),
                name="cfb-capper-test-preview",
            )
            try:
                yield
            finally:
                for pending in (task, preview_task):
                    pending.cancel()
                for pending in (task, preview_task):
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _lifespan

    @app.get("/api/cfb-cappers/status", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_status() -> dict[str, Any]:
        signals = _load_signals()
        counts: dict[str, dict[str, int]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "stale": sum(1 for r in rows if r.get("status") == "IGNORED_STALE"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }
