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

TARGET_SOURCES = {
    "SLAM - All Access": "Slam - NFL",
    "The Syndicate": "Syndicate - NFL",
}
SOURCE_LABELS = ("Slam - NFL", "Syndicate - NFL")
SOURCE_ID_ENV = {
    "Slam - NFL": "NFL_CAPPER_SLAM_SOURCE_IDS",
    "Syndicate - NFL": "NFL_CAPPER_SYNDICATE_SOURCE_IDS",
}

NFL_TEAMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ARI": ("Arizona Cardinals", ("arizona cardinals", "cardinals")),
    "ATL": ("Atlanta Falcons", ("atlanta falcons", "falcons")),
    "BAL": ("Baltimore Ravens", ("baltimore ravens", "ravens")),
    "BUF": ("Buffalo Bills", ("buffalo bills", "bills")),
    "CAR": ("Carolina Panthers", ("carolina panthers", "panthers")),
    "CHI": ("Chicago Bears", ("chicago bears", "bears")),
    "CIN": ("Cincinnati Bengals", ("cincinnati bengals", "bengals")),
    "CLE": ("Cleveland Browns", ("cleveland browns", "browns")),
    "DAL": ("Dallas Cowboys", ("dallas cowboys", "cowboys")),
    "DEN": ("Denver Broncos", ("denver broncos", "broncos")),
    "DET": ("Detroit Lions", ("detroit lions", "lions")),
    "GB": ("Green Bay Packers", ("green bay packers", "packers")),
    "HOU": ("Houston Texans", ("houston texans", "texans")),
    "IND": ("Indianapolis Colts", ("indianapolis colts", "colts")),
    "JAX": ("Jacksonville Jaguars", ("jacksonville jaguars", "jaguars", "jags")),
    "KC": ("Kansas City Chiefs", ("kansas city chiefs", "chiefs")),
    "LV": ("Las Vegas Raiders", ("las vegas raiders", "raiders")),
    "LAC": ("Los Angeles Chargers", ("los angeles chargers", "la chargers", "chargers")),
    "LAR": ("Los Angeles Rams", ("los angeles rams", "la rams", "rams")),
    "MIA": ("Miami Dolphins", ("miami dolphins", "dolphins")),
    "MIN": ("Minnesota Vikings", ("minnesota vikings", "vikings")),
    "NE": ("New England Patriots", ("new england patriots", "patriots", "pats")),
    "NO": ("New Orleans Saints", ("new orleans saints", "saints")),
    "NYG": ("New York Giants", ("new york giants", "ny giants", "giants")),
    "NYJ": ("New York Jets", ("new york jets", "ny jets", "jets")),
    "PHI": ("Philadelphia Eagles", ("philadelphia eagles", "eagles")),
    "PIT": ("Pittsburgh Steelers", ("pittsburgh steelers", "steelers")),
    "SEA": ("Seattle Seahawks", ("seattle seahawks", "seahawks")),
    "SF": ("San Francisco 49ers", ("san francisco 49ers", "49ers", "niners")),
    "TB": ("Tampa Bay Buccaneers", ("tampa bay buccaneers", "buccaneers", "bucs")),
    "TEN": ("Tennessee Titans", ("tennessee titans", "titans")),
    "WAS": ("Washington Commanders", ("washington commanders", "commanders")),
}

_INSTALLED = False
_STATUS: dict[str, Any] = {
    "last_poll_at": None,
    "last_success_at": None,
    "last_error": None,
    "cycles": 0,
}


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _feed_content_signature(feed: dict[str, Any]) -> str:
    """Hash only feed content, excluding timestamps that change every poll."""
    payload = {
        "scanned_posts": feed.get("scanned_posts"),
        "detected_posts": feed.get("detected_posts"),
        "detected_picks": feed.get("detected_picks"),
        "listener_connected": feed.get("listener_connected"),
        "listener_ready": feed.get("listener_ready"),
        "freshness": {
            "connected": (feed.get("freshness") or {}).get("connected"),
            "ready": (feed.get("freshness") or {}).get("ready"),
            "resolved_channels": (feed.get("freshness") or {}).get("resolved_channels") or [],
            "error": (feed.get("freshness") or {}).get("error"),
        },
        "unparsed_recent": feed.get("unparsed_recent") or [],
        "picks": feed.get("picks") or [],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


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


def _compact_source(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _configured_source_ids(label: str) -> set[str]:
    env_name = SOURCE_ID_ENV[label]
    return {
        item.strip()
        for item in os.getenv(env_name, "").split(",")
        if item.strip()
    }


def _source_label(pick: dict[str, Any]) -> str | None:
    """Resolve mutable Telegram labels/usernames to a stable capper identity."""
    source = str(pick.get("source") or "").strip()
    exact = TARGET_SOURCES.get(source)
    if exact is not None:
        return exact

    source_key = _compact_source(pick.get("source_key"))
    if source_key in {"slam", "slamnfl"}:
        return "Slam - NFL"
    if source_key in {"syndicate", "thesyndicate", "syndicatenfl"}:
        return "Syndicate - NFL"

    compact = _compact_source(source)
    # The bridge historically stored Telegram username-or-title. Match the
    # analyst identity rather than requiring one mutable display string.
    if compact.startswith("slam") or "slamthebookie" in compact:
        return "Slam - NFL"
    if "syndicate" in compact:
        return "Syndicate - NFL"

    source_id = str(pick.get("source_id") or "").strip()
    if source_id:
        for label in SOURCE_LABELS:
            if source_id in _configured_source_ids(label):
                return label
    return None


def _units_for_pick(pick: dict[str, Any]) -> Decimal:
    try:
        units = Decimal(str(pick.get("units") if pick.get("units") is not None else "1"))
    except Exception:
        units = Decimal("1")
    return Decimal("1") if units <= 1 else units


def _stake_for_pick(pick: dict[str, Any], unit_usdc: Decimal = Decimal("10")) -> Decimal:
    return (_units_for_pick(pick) * unit_usdc).quantize(Decimal("0.01"))


def _fingerprint(pick: dict[str, Any]) -> str:
    canonical = {
        "source": str(pick.get("source") or ""),
        "posted_at": str(pick.get("posted_at") or ""),
        "selection": str(pick.get("selection") or ""),
        "teams": list(pick.get("teams") or []),
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
    if "prop" in types:
        return None, "props are not auto-traded"
    if pick.get("period") or "period" in types:
        return None, "period/half/quarter markets are not auto-traded"

    selection = str(pick.get("selection") or "").strip()
    if len(selection) > 90:
        return None, "selection is too verbose/ambiguous for unattended execution"

    primary = types & {"moneyline", "spread", "total"}
    if len(primary) != 1:
        return None, "pick does not resolve to exactly one supported game market"

    kind = next(iter(primary))
    teams = [str(x).upper() for x in (pick.get("teams") or []) if str(x).upper() in NFL_TEAMS]
    if kind == "moneyline" and len(teams) < 1:
        return None, "moneyline pick has no recognized NFL team"
    if kind == "spread":
        lines = list(pick.get("spread_lines") or [])
        if len(teams) < 1 or len(lines) != 1:
            return None, "spread pick needs one recognized team and one spread line"
    if kind == "total":
        if len(teams) < 1 or pick.get("total_line") is None or str(pick.get("total_side") or "").upper() not in {"OVER", "UNDER"}:
            return None, "total pick needs at least one recognized NFL team, a side, and a total line"
    return kind, None


def _team_aliases(abbr: str) -> tuple[str, ...]:
    row = NFL_TEAMS.get(str(abbr).upper())
    return row[1] if row else ()


def _team_name(abbr: str) -> str:
    row = NFL_TEAMS.get(str(abbr).upper())
    return row[0] if row else str(abbr)


def _contains_alias(text: str, abbr: str) -> bool:
    value = _norm(text)
    return any(re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", value) for alias in _team_aliases(abbr))


def _market_text(market: Any) -> str:
    return _norm(" ".join([
        str(getattr(market, "question", "") or ""),
        str(getattr(market, "slug", "") or ""),
        str(getattr(getattr(market, "sports", None), "sports_market_type", "") or ""),
    ]))


def _market_type(market: Any) -> str:
    return _norm(getattr(getattr(market, "sports", None), "sports_market_type", ""))


def _type_matches(actual: str, wanted: str) -> bool:
    actual = _norm(actual)
    if actual == wanted:
        return True
    aliases = {
        "moneyline": ("moneyline", "money line", "winner", "match winner"),
        "spread": ("spread", "handicap"),
        "total": ("total", "over/under", "over under"),
    }
    return any(word in actual for word in aliases[wanted])


def _outcomes(market: Any) -> list[tuple[str, Any]]:
    outcomes = getattr(market, "outcomes", None)
    return [
        (str(getattr(getattr(outcomes, "yes", None), "label", "Yes") or "Yes"), getattr(outcomes, "yes", None)),
        (str(getattr(getattr(outcomes, "no", None), "label", "No") or "No"), getattr(outcomes, "no", None)),
    ]


def _line_matches(text: str, value: Any, *, signed: bool) -> bool:
    try:
        number = Decimal(str(value))
    except Exception:
        return False
    target = format(abs(number).normalize(), "f")
    if "." not in target:
        target = str(int(abs(number)))
    body = str(text or "")
    signed_hits = re.findall(rf"(?<!\d)([+-])\s*{re.escape(target)}(?!\d)", body)
    if signed:
        wanted_sign = "+" if number >= 0 else "-"
        if signed_hits:
            return wanted_sign in signed_hits
        return bool(re.search(rf"(?<!\d){re.escape(target)}(?!\d)", body))
    return bool(re.search(rf"(?<!\d){re.escape(target)}(?!\d)", body))


def _structured_line_matches(market: Any, value: Any) -> bool | None:
    """Compare against Polymarket's structured sports line when available."""
    raw = getattr(getattr(market, "sports", None), "line", None)
    if raw is None:
        return None
    try:
        return Decimal(str(raw)) == Decimal(str(value))
    except Exception:
        return False


def _select_outcome(market: Any, pick: dict[str, Any], kind: str) -> tuple[str, Any] | None:
    mtext = _market_text(market)
    teams = [str(x).upper() for x in (pick.get("teams") or [])]
    outcomes = [(label, obj) for label, obj in _outcomes(market) if obj is not None]
    if len(outcomes) != 2:
        return None

    if kind == "moneyline":
        team = teams[0]
        for label, obj in outcomes:
            if _contains_alias(label, team):
                return label, obj
        if _contains_alias(mtext, team) and _norm(outcomes[0][0]) == "yes":
            return outcomes[0]
        return None

    if kind == "spread":
        team = teams[0]
        line = list(pick.get("spread_lines") or [None])[0]
        if not _line_matches(mtext, line, signed=True):
            return None
        for label, obj in outcomes:
            if _contains_alias(label, team) and _line_matches(label + " " + mtext, line, signed=True):
                return label, obj
        if _contains_alias(mtext, team) and _norm(outcomes[0][0]) == "yes":
            return outcomes[0]
        return None

    if kind == "total":
        side = str(pick.get("total_side") or "").upper()
        line = pick.get("total_line")
        structured_match = _structured_line_matches(market, line)
        if structured_match is False:
            return None
        if structured_match is None and not _line_matches(mtext, line, signed=False):
            return None
        for label, obj in outcomes:
            if side.lower() in _norm(label):
                return label, obj
        yes, no = outcomes
        if _norm(yes[0]) == "yes" and _norm(no[0]) == "no":
            has_over = bool(re.search(r"(?<![a-z])over(?![a-z])", mtext))
            has_under = bool(re.search(r"(?<![a-z])under(?![a-z])", mtext))
            if has_over and not has_under:
                return yes if side == "OVER" else no
            if has_under and not has_over:
                return yes if side == "UNDER" else no
        return None
    return None


def _diagnose_market_candidates(pick: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    teams = [str(x).upper() for x in (pick.get("teams") or [])]
    query = _team_name(teams[0]) if teams else ""
    rows: list[dict[str, Any]] = []
    if not query:
        return rows

    with PublicClient() as client:
        events = list(
            client.list_events(
                title_search=query,
                closed=False,
                page_size=30,
            ).first_page().items
        )
        for event in events:
            for market in getattr(event, "markets", ()) or ():
                sports = getattr(market, "sports", None)
                outcomes = getattr(market, "outcomes", None)
                rows.append({
                    "event_title": str(getattr(event, "title", "") or ""),
                    "event_slug": str(getattr(event, "slug", "") or ""),
                    "market_id": str(getattr(market, "id", "") or ""),
                    "market_question": str(getattr(market, "question", "") or ""),
                    "market_slug": str(getattr(market, "slug", "") or ""),
                    "sports_market_type": str(getattr(sports, "sports_market_type", "") or ""),
                    "sports_line": getattr(sports, "line", None),
                    "yes_label": str(
                        getattr(getattr(outcomes, "yes", None), "label", "") or ""
                    ),
                    "no_label": str(
                        getattr(getattr(outcomes, "no", None), "label", "") or ""
                    ),
                    "accepting_orders": bool(
                        getattr(getattr(market, "state", None), "accepting_orders", False)
                    ),
                })
    return rows


def _find_market(pick: dict[str, Any], kind: str) -> tuple[Any, Any, str, Any]:
    teams = [str(x).upper() for x in (pick.get("teams") or [])]
    ranked: list[tuple[int, Any, Any, str, Any]] = []

    search_queries: list[str] = []
    for candidate in (_team_name(teams[0]), *_team_aliases(teams[0])):
        text = str(candidate or "").strip()
        if text and text.casefold() not in {q.casefold() for q in search_queries}:
            search_queries.append(text)

    with PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in search_queries:
            result = client.list_events(
                title_search=query,
                closed=False,
                page_size=30,
            ).first_page()
            for event in result.items:
                key = str(
                    getattr(event, "id", "")
                    or getattr(event, "slug", "")
                    or getattr(event, "title", "")
                )
                if key:
                    events_by_key[key] = event

        for event in events_by_key.values():
            event_slug = _norm(getattr(event, "slug", ""))
            if event_slug and not event_slug.startswith("nfl-"):
                continue
            etext = _norm(" ".join([str(getattr(event, "title", "") or ""), str(getattr(event, "slug", "") or "")]))
            if not all(_contains_alias(etext, team) for team in teams[:2]):
                continue
            for market in getattr(event, "markets", ()) or ():
                actual = _market_type(market)
                if not _type_matches(actual, kind):
                    continue
                outcome = _select_outcome(market, pick, kind)
                if outcome is None:
                    continue
                label, obj = outcome
                score = 10
                if actual == kind:
                    score += 4
                if getattr(getattr(market, "state", None), "accepting_orders", False):
                    score += 4
                if _contains_alias(_market_text(market) + " " + label, teams[0]):
                    score += 2
                ranked.append((score, event, market, label, obj))

    if not ranked:
        raise ValueError(f"No exact open Polymarket NFL {kind} market matched '{pick.get('selection')}'")

    # When Telegram gives us only one team for a game total, the exact line
    # must still identify exactly one NFL event. Never use ranking heuristics
    # to choose between two different games involving the same team.
    if kind == "total" and len(teams) == 1:
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
            raise ValueError(
                "One-team NFL total did not resolve to exactly one event; unattended trade blocked"
            )

    ranked.sort(key=lambda row: row[0], reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best[0]:
        first_id = str(getattr(best[2], "id", "") or getattr(best[2], "slug", ""))
        second_id = str(getattr(ranked[1][2], "id", "") or getattr(ranked[1][2], "slug", ""))
        if first_id != second_id:
            raise ValueError("Multiple equally strong Polymarket market matches; unattended trade blocked")
    return best[1], best[2], best[3], best[4]


def _pending_auto_budget(remote: Any) -> Decimal:
    total = Decimal("0")
    try:
        queue = remote._queue_load()
    except Exception:
        return total
    for rec in queue.values():
        if rec.get("action") != "BUY" or rec.get("status") not in {"PENDING", "LEASED"}:
            continue
        try:
            total += Decimal(str((rec.get("payload") or {}).get("budget_usdc") or "0"))
        except Exception:
            continue
    return total


def _prepare_pick(
    pick: dict[str, Any],
    *,
    core: Any,
    remote: Any,
    unit_usdc: Decimal,
) -> dict[str, Any]:
    kind, reason = _classify_pick(pick)
    if kind is None:
        return {"status": "IGNORED_UNSUPPORTED", "reason": reason}

    if not core.auto_trading_enabled():
        raise RuntimeError("AUTO_TRADING is disabled")

    ready, executor_state = live_control._executor_ready()
    if not ready:
        if executor_state.get("geo_blocked"):
            raise RuntimeError("Termux executor is geoblocked")
        raise RuntimeError("Termux executor is offline")

    units = _units_for_pick(pick)
    stake = _stake_for_pick(pick, unit_usdc)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            "Requested " + str(units) + "u = $" + str(stake)
            + " exceeds MAX_AUTO_TRADE_USDC=$" + str(core.MAX_AUTO_TRADE_USDC)
        )

    event, market, outcome_label, outcome_obj = _find_market(pick, kind)
    asset_id = str(getattr(outcome_obj, "token_id", None) or getattr(outcome_obj, "position_id", None) or "")
    if not asset_id:
        raise RuntimeError("Matched Polymarket market has no tradable outcome token")

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
    max_price = best_ask
    if max_price > core.MAX_PRICE:
        raise RuntimeError(f"Current best ask {max_price} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise RuntimeError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")

    used = core._daily_budget_used()
    pending = _pending_auto_budget(remote)
    if used + pending + stake > core.MAX_DAILY_BUDGET_USDC:
        raise RuntimeError(
            "Daily auto budget would exceed MAX_DAILY_BUDGET_USDC=$" + str(core.MAX_DAILY_BUDGET_USDC)
            + "; used=$" + str(used) + ", pending=$" + str(pending) + ", requested=$" + str(stake)
        )

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug:
        raise RuntimeError("Matched Polymarket event has no slug")
    market_url = f"https://polymarket.com/sports/nfl/{event_slug}"

    fp = _fingerprint(pick)
    source_label = _source_label(pick)
    trade_id = f"nfl-capper-{fp[:18]}"
    payload = {
        "market_url": market_url,
        "outcome": outcome_label,
        "market_type": kind,
        "asset_id": asset_id,
        "max_price": str(max_price),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "termux_executor",
        "auto": True,
        "strategy_source": source_label,
        "strategy_sport": "NFL",
        "strategy_units": str(units),
        "strategy_unit_usdc": str(unit_usdc),
        "strategy_pick_id": fp,
        "strategy_posted_at": pick.get("posted_at"),
        "strategy_selection": pick.get("selection"),
        "strategy_telegram_source": pick.get("source"),
    }
    queued = remote._enqueue("BUY", payload)
    return {
        "status": "QUEUED",
        "request_id": queued["id"],
        "trade_id": trade_id,
        "strategy_source": source_label,
        "market_type": kind,
        "market": str(getattr(market, "question", "") or getattr(event, "title", "NFL market")),
        "market_url": market_url,
        "outcome": outcome_label,
        "asset_id": asset_id,
        "units": str(units),
        "stake_usdc": str(stake),
        "max_price": str(max_price),
        "spread": str(spread),
    }



def _prepare_test_preview(
    pick: dict[str, Any],
    *,
    core: Any,
    remote: Any,
    unit_usdc: Decimal,
) -> dict[str, Any]:
    """Exercise the production Telegram-capper checks through Termux PREVIEW only."""
    kind, reason = _classify_pick(pick)
    if kind is None:
        raise RuntimeError(reason or "unsupported test pick")

    if not core.auto_trading_enabled():
        raise RuntimeError("AUTO_TRADING is disabled")

    ready, executor_state = live_control._executor_ready()
    if not ready:
        if executor_state.get("geo_blocked"):
            raise RuntimeError("Termux executor is geoblocked")
        raise RuntimeError("Termux executor is offline")

    units = _units_for_pick(pick)
    stake = _stake_for_pick(pick, unit_usdc)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            "Requested " + str(units) + "u = $" + str(stake)
            + " exceeds MAX_AUTO_TRADE_USDC=$" + str(core.MAX_AUTO_TRADE_USDC)
        )

    try:
        event, market, outcome_label, outcome_obj = _find_market(pick, kind)
    except Exception:
        diagnostics = _diagnose_market_candidates(pick, kind)
        print(
            "NFL_CAPPER_TEST_MARKET_DIAGNOSTICS "
            + json.dumps(
                diagnostics[:200],
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
            flush=True,
        )
        raise
    asset_id = str(
        getattr(outcome_obj, "token_id", None)
        or getattr(outcome_obj, "position_id", None)
        or ""
    )
    if not asset_id:
        raise RuntimeError("Matched Polymarket market has no tradable outcome token")

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

    used = core._daily_budget_used()
    pending = _pending_auto_budget(remote)
    if used + pending + stake > core.MAX_DAILY_BUDGET_USDC:
        raise RuntimeError(
            "Daily auto budget would exceed MAX_DAILY_BUDGET_USDC=$"
            + str(core.MAX_DAILY_BUDGET_USDC)
            + "; used=$" + str(used)
            + ", pending=$" + str(pending)
            + ", requested=$" + str(stake)
        )

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug:
        raise RuntimeError("Matched Polymarket event has no slug")

    payload = {
        "market_url": f"https://polymarket.com/sports/nfl/{event_slug}",
        "outcome": outcome_label,
        "market_type": kind,
        "asset_id": asset_id,
        "max_price": str(best_ask),
        "budget_usdc": str(stake),
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "nfl_capper_test_preview",
        "auto": False,
        "strategy_source": _source_label(pick),
        "strategy_sport": "NFL",
        "strategy_units": str(units),
        "strategy_unit_usdc": str(unit_usdc),
        "strategy_selection": pick.get("selection"),
        "strategy_telegram_source": pick.get("source"),
    }
    queued = remote._enqueue("PREVIEW", payload)
    return {
        "status": "PREVIEW_QUEUED",
        "request_id": queued["id"],
        "market": str(
            getattr(market, "question", "")
            or getattr(event, "title", "NFL market")
        ),
        "market_url": payload["market_url"],
        "market_type": kind,
        "outcome": outcome_label,
        "asset_id": asset_id,
        "units": str(units),
        "stake_usdc": str(stake),
        "current_buy_price": str(buy_price),
        "best_ask": str(best_ask),
        "max_price": str(best_ask),
        "spread": str(spread),
    }


def _stats_from_executions(executions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for label in SOURCE_LABELS:
        wins = losses = pushes = open_trades = total = 0
        stake = Decimal("0")
        realized = Decimal("0")
        units = Decimal("0")
        for rec in executions.values():
            if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("strategy_source") != label:
                continue
            total += 1
            try:
                units += Decimal(str(rec.get("strategy_units") or "0"))
            except Exception:
                pass
            status = str(rec.get("status") or "")
            if status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
                open_trades += 1
                continue
            pnl_raw = rec.get("realized_pnl")
            if pnl_raw is None:
                continue
            try:
                pnl = Decimal(str(pnl_raw))
            except Exception:
                continue
            realized += pnl
            try:
                stake += Decimal(str(rec.get("actual_cost_usdc") or rec.get("budget_usdc") or "0"))
            except Exception:
                pass
            settlement_result = str((rec.get("settlement") or {}).get("result") or "").upper()
            if settlement_result == "PUSH":
                pushes += 1
            elif settlement_result == "WIN" or (not settlement_result and pnl > 0):
                wins += 1
            elif settlement_result == "LOSS" or (not settlement_result and pnl < 0):
                losses += 1
            else:
                pushes += 1

        graded = wins + losses + pushes
        decided = wins + losses
        win_pct = (Decimal(wins) / Decimal(decided) * Decimal("100")) if decided else None
        roi = (realized / stake * Decimal("100")) if stake > 0 else None
        out[label] = {
            "label": label,
            "bets": total,
            "open": open_trades,
            "graded": graded,
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "win_pct": str(win_pct.quantize(Decimal("0.1"))) if win_pct is not None else None,
            "units_staked": str(units.quantize(Decimal("0.01"))),
            "graded_stake_usdc": str(stake.quantize(Decimal("0.01"))),
            "realized_pnl_usdc": str(realized.quantize(Decimal("0.01"))),
            "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
        }
    return out


def install(*, app: Any, dashboard: Any, core: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    remote = live_control.remote
    signal_file = core.DATA_DIR / "nfl_capper_signals.json"
    last_feed_file = core.DATA_DIR / "nfl_capper_last_feed.json"
    bridge_url = os.getenv(
        "NFL_CAPPER_BRIDGE_URL",
        "https://telegram-chatgpt-bridge-production-286a.up.railway.app",
    ).rstrip("/")
    enabled = os.getenv("NFL_CAPPER_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    poll_seconds = max(5, int(os.getenv("NFL_CAPPER_POLL_SECONDS", "15")))
    max_age_seconds = max(30, int(os.getenv("NFL_CAPPER_MAX_PICK_AGE_SECONDS", "180")))
    unit_usdc = Decimal(os.getenv("NFL_CAPPER_UNIT_USDC", "10"))
    test_preview_raw = os.getenv("NFL_CAPPER_TEST_PREVIEW_JSON", "").strip()
    test_preview_marker = core.DATA_DIR / "nfl_capper_test_preview.json"

    def _load_signals() -> dict[str, Any]:
        data = core._load(signal_file)
        return data if isinstance(data, dict) else {}

    def _save_signals(data: dict[str, Any]) -> None:
        if len(data) > 2500:
            ordered = sorted(data.items(), key=lambda item: str((item[1] or {}).get("first_seen_at") or ""))
            data = dict(ordered[-2000:])
        core._save(signal_file, data)

    last_feed_signature: str | None = None

    def _sync_queue_status(signals: dict[str, Any]) -> bool:
        changed = False
        queue = remote._queue_load()
        for rec in signals.values():
            if rec.get("status") != "QUEUED" or not rec.get("request_id"):
                continue
            queued = queue.get(str(rec.get("request_id")))
            if not queued:
                continue
            qstatus = str(queued.get("status") or "")
            if qstatus == "DONE":
                rec["status"] = "EXECUTOR_DONE"
                rec["completed_at"] = queued.get("updated_at") or _now_iso()
                changed = True
            elif qstatus == "FAILED":
                rec["status"] = "EXECUTOR_FAILED"
                rec["last_error"] = queued.get("error")
                rec["completed_at"] = queued.get("updated_at") or _now_iso()
                changed = True
        return changed

    async def _poll_once() -> None:
        nonlocal last_feed_signature
        _STATUS["last_poll_at"] = _now_iso()
        signals = _load_signals()
        changed = _sync_queue_status(signals)
        if not enabled:
            _STATUS["last_error"] = "NFL capper automation is disabled"
            if changed:
                _save_signals(signals)
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{bridge_url}/public/nfl",
                    params={"minutes": 180, "limit": 120, "include_graded": "false"},
                )
                response.raise_for_status()
                feed = response.json()

            # Persist only the sanitized public-feed fields needed for diagnosis.
            # The bridge endpoint does not expose raw Telegram message text.
            core._save(last_feed_file, {
                "saved_at": _now_iso(),
                "sport": feed.get("sport"),
                "generated_at": feed.get("generated_at"),
                "window_minutes": feed.get("window_minutes"),
                "freshness": feed.get("freshness"),
                "scanned_posts": feed.get("scanned_posts"),
                "detected_posts": feed.get("detected_posts"),
                "detected_picks": feed.get("detected_picks"),
                "listener_connected": feed.get("listener_connected"),
                "listener_ready": feed.get("listener_ready"),
                "unparsed_recent": feed.get("unparsed_recent") or [],
                "picks": feed.get("picks") or [],
            })

            feed_signature = _feed_content_signature(feed)
            if feed_signature != last_feed_signature:
                picks_for_log = [p for p in (feed.get("picks") or []) if isinstance(p, dict)]
                unparsed_for_log = [p for p in (feed.get("unparsed_recent") or []) if isinstance(p, dict)]
                print(
                    "NFL_CAPPER_FEED "
                    f"scanned_posts={feed.get('scanned_posts')} "
                    f"detected_posts={feed.get('detected_posts')} "
                    f"detected_picks={feed.get('detected_picks')} "
                    f"unparsed_recent={len(unparsed_for_log)} "
                    f"listener_connected={feed.get('listener_connected')} "
                    f"listener_ready={feed.get('listener_ready')} "
                    f"freshness_error={(feed.get('freshness') or {}).get('error')!r} "
                    f"resolved_channels={(feed.get('freshness') or {}).get('resolved_channels') or []!r}",
                    flush=True,
                )
                for row in picks_for_log[:20]:
                    safe = {
                        key: row.get(key)
                        for key in (
                            "source", "source_id", "source_key", "posted_at", "selection",
                            "teams", "bet_types", "period", "spread_lines",
                            "total_side", "total_line", "units", "status",
                        )
                    }
                    print(
                        "NFL_CAPPER_FEED_PICK "
                        + json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str),
                        flush=True,
                    )
                for row in unparsed_for_log[:20]:
                    safe = {
                        key: row.get(key)
                        for key in (
                            "source", "source_id", "source_key", "posted_at", "has_media",
                            "text_present", "text_length", "detected_teams",
                            "candidate_line_count",
                        )
                    }
                    print(
                        "NFL_CAPPER_UNPARSED "
                        + json.dumps(safe, sort_keys=True, separators=(",", ":"), default=str),
                        flush=True,
                    )
                last_feed_signature = feed_signature
        except Exception as exc:
            _STATUS["last_error"] = f"{type(exc).__name__}: {exc}"
            if changed:
                _save_signals(signals)
            return

        picks = [p for p in (feed.get("picks") or []) if isinstance(p, dict)]
        picks.sort(key=lambda p: str(p.get("posted_at") or ""))
        now = datetime.now(timezone.utc)

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and current.get("status") in {
                "QUEUED", "EXECUTOR_DONE", "EXECUTOR_FAILED", "IGNORED_STALE",
                "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                continue

            source_label = _source_label(pick)
            base_record = current or {
                "id": fp,
                "first_seen_at": _now_iso(),
                "source": source_label,
                "telegram_source": pick.get("source"),
                "telegram_source_id": pick.get("source_id"),
                "telegram_source_key": pick.get("source_key"),
                "posted_at": pick.get("posted_at"),
                "selection": pick.get("selection"),
                "units": str(_units_for_pick(pick)),
                "stake_usdc": str(_stake_for_pick(pick, unit_usdc)),
            }

            if source_label is None:
                base_record["status"] = "IGNORED_UNTRACKED_SOURCE"
                base_record["reason"] = "NFL feed source is not Slam or Syndicate"
                base_record["updated_at"] = _now_iso()
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SOURCE_IGNORED "
                    f"source={pick.get('source')!r} source_id={pick.get('source_id')!r} "
                    f"source_key={pick.get('source_key')!r} selection={pick.get('selection')!r}",
                    flush=True,
                )
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                base_record["status"] = "IGNORED_STALE"
                base_record["reason"] = f"pick age exceeds {max_age_seconds}s freshness limit"
                base_record["updated_at"] = _now_iso()
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status=IGNORED_STALE source={source_label!r} "
                    f"telegram_source={pick.get('source')!r} selection={pick.get('selection')!r} "
                    f"age_seconds={int(age) if age != float('inf') else 'unknown'} "
                    f"reason={base_record['reason']!r}",
                    flush=True,
                )
                continue

            kind, unsupported = _classify_pick(pick)
            if kind is None:
                base_record["status"] = "IGNORED_UNSUPPORTED"
                base_record["reason"] = unsupported
                base_record["updated_at"] = _now_iso()
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status=IGNORED_UNSUPPORTED source={source_label!r} "
                    f"telegram_source={pick.get('source')!r} selection={pick.get('selection')!r} "
                    f"reason={unsupported!r}",
                    flush=True,
                )
                continue

            try:
                result = await asyncio.to_thread(_prepare_pick, pick, core=core, remote=remote, unit_usdc=unit_usdc)
                base_record.update(result)
                base_record["updated_at"] = _now_iso()
                base_record.pop("last_error", None)
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status={base_record.get('status')} source={source_label!r} "
                    f"telegram_source={pick.get('source')!r} selection={pick.get('selection')!r} "
                    f"request_id={base_record.get('request_id')!r}",
                    flush=True,
                )
            except Exception as exc:
                base_record["status"] = "RETRYING"
                base_record["last_error"] = f"{type(exc).__name__}: {exc}"
                base_record["updated_at"] = _now_iso()
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status=RETRYING source={source_label!r} telegram_source={pick.get('source')!r} "
                    f"selection={pick.get('selection')!r} error={base_record['last_error']}",
                    flush=True,
                )

        if changed:
            _save_signals(signals)
        _STATUS["last_success_at"] = _now_iso()
        _STATUS["last_error"] = None
        _STATUS["cycles"] = int(_STATUS.get("cycles") or 0) + 1

    async def _run_test_preview_once() -> None:
        if not test_preview_raw:
            return

        trigger_hash = hashlib.sha256(test_preview_raw.encode("utf-8")).hexdigest()
        existing_marker = core._load(test_preview_marker) if test_preview_marker.exists() else {}
        if (
            isinstance(existing_marker, dict)
            and existing_marker.get("trigger_hash") == trigger_hash
            and existing_marker.get("status") in {"QUEUED", "DONE"}
        ):
            return

        try:
            pick = json.loads(test_preview_raw)
            if not isinstance(pick, dict):
                raise RuntimeError("NFL_CAPPER_TEST_PREVIEW_JSON must decode to an object")

            # Railway deploy cutovers briefly interrupt the phone poller. Wait for
            # the existing paired Termux worker to reconnect instead of failing
            # the one-shot immediately during that normal handoff window.
            executor_state: dict[str, Any] = {}
            while True:
                ready, executor_state = live_control._executor_ready()
                if ready:
                    break
                if executor_state.get("geo_blocked"):
                    raise RuntimeError("Termux executor is geoblocked")
                await asyncio.sleep(2)

            pick["posted_at"] = _now_iso()
            result = await asyncio.to_thread(
                _prepare_test_preview,
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
                "NFL_CAPPER_TEST_PREVIEW "
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
                    "NFL_CAPPER_TEST_PREVIEW_RESULT "
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
                "NFL_CAPPER_TEST_PREVIEW_RESULT "
                + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str),
                flush=True,
            )

    async def _loop() -> None:
        await asyncio.sleep(2)
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
            task = asyncio.create_task(_loop(), name="nfl-capper-poller")
            preview_task = asyncio.create_task(
                _run_test_preview_once(),
                name="nfl-capper-test-preview",
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

    def _stats_payload() -> dict[str, Any]:
        stats = _stats_from_executions(core._load(core.EXECUTIONS_FILE))
        signals = _load_signals()
        signal_summary = {
            "queued": sum(1 for x in signals.values() if (x or {}).get("status") in {"QUEUED", "EXECUTOR_DONE"}),
            "failed": sum(1 for x in signals.values() if (x or {}).get("status") == "EXECUTOR_FAILED"),
            "retrying": sum(1 for x in signals.values() if (x or {}).get("status") == "RETRYING"),
            "stale_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_STALE"),
            "unsupported_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_UNSUPPORTED"),
            "untracked_source_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_UNTRACKED_SOURCE"),
        }
        return {
            "enabled": enabled,
            "unit_usdc": str(unit_usdc),
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "auto_trading": bool(core.auto_trading_enabled()),
            "status": dict(_STATUS),
            "signals": signal_summary,
            "cappers": stats,
        }

    @app.get("/api/nfl-cappers/stats", dependencies=[Depends(dashboard._auth)])
    def nfl_capper_stats():
        return _stats_payload()

    base_snapshot = dashboard._dashboard_snapshot

    def _dashboard_snapshot_with_nfl_cappers() -> dict[str, Any]:
        data = base_snapshot()
        data["nfl_cappers"] = _stats_payload()
        return data

    dashboard._dashboard_snapshot = _dashboard_snapshot_with_nfl_cappers

    html = dashboard.DASHBOARD_HTML
    if 'id="nflCapperStats"' not in html:
        panel = r"""
  <div class="nfl-capper-panel" id="nflCapperStats">
    <div class="nfl-capper-head"><div><div class="label">NFL capper auto-trading</div><div class="nfl-capper-state" id="nflCapperState">Loading…</div></div><div class="nfl-capper-meta" id="nflCapperMeta"></div></div>
    <div class="nfl-capper-grid">
      <div class="nfl-capper-card"><b>Slam - NFL</b><div class="nfl-capper-kpis" id="nflCapperSlam">—</div></div>
      <div class="nfl-capper-card"><b>Syndicate - NFL</b><div class="nfl-capper-kpis" id="nflCapperSyndicate">—</div></div>
    </div>
  </div>
"""
        html = html.replace('  <div class="tabs">', panel + '  <div class="tabs">', 1)
        css = r"""
.nfl-capper-panel{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:14px;padding:15px;margin:14px 0}.nfl-capper-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}.nfl-capper-state{font-size:13px;font-weight:850;margin-top:5px}.nfl-capper-meta{font-size:10px;color:var(--muted);text-align:right}.nfl-capper-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.nfl-capper-card{border:1px solid var(--border);border-radius:10px;background:#0d1522;padding:12px}.nfl-capper-kpis{font-size:11px;color:var(--muted);line-height:1.7;margin-top:5px}@media(max-width:650px){.nfl-capper-grid{grid-template-columns:1fr}.nfl-capper-head{flex-direction:column}.nfl-capper-meta{text-align:left}}
"""
        html = html.replace("</style>", css + "</style>", 1)
        js = r"""
function nflCapperLine(x){
 if(!x)return 'No tracked trades yet';
 const roi=x.roi_pct===null||x.roi_pct===undefined?'—':Number(x.roi_pct).toFixed(1)+'%';
 const pnl=x.realized_pnl_usdc===null||x.realized_pnl_usdc===undefined?'—':'$'+Number(x.realized_pnl_usdc).toFixed(2);
 return 'Bets '+(x.bets||0)+' · Open '+(x.open||0)+' · W-L-P '+(x.wins||0)+'-'+(x.losses||0)+'-'+(x.pushes||0)+'<br>Stake $'+Number(x.graded_stake_usdc||0).toFixed(2)+' · P/L '+pnl+' · ROI '+roi;
}
async function loadNflCapperStats(){
 try{
  const r=await fetch('/api/nfl-cappers/stats',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'NFL capper stats failed');
  const state=document.getElementById('nflCapperState'),meta=document.getElementById('nflCapperMeta');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.auto_trading?' · AUTO LIVE':' · AUTO TRADING OFF');
  if(meta)meta.textContent='1u = $'+Number(d.unit_usdc||10).toFixed(2)+' · fresh ≤ '+(d.max_pick_age_seconds||0)+'s · poll '+(d.poll_seconds||0)+'s';
  const s=document.getElementById('nflCapperSlam'),y=document.getElementById('nflCapperSyndicate');
  if(s)s.innerHTML=nflCapperLine((d.cappers||{})['Slam - NFL']);
  if(y)y.innerHTML=nflCapperLine((d.cappers||{})['Syndicate - NFL']);
 }catch(e){
  const state=document.getElementById('nflCapperState');if(state)state.textContent='Stats unavailable: '+String(e);
 }
}
loadNflCapperStats();setInterval(loadNflCapperStats,10000);
"""
        html = html.replace("</script>", js + "\n</script>", 1)
        dashboard.DASHBOARD_HTML = html
