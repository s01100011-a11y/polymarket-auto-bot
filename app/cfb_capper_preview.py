from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import Depends, HTTPException
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


def _resolved_signal_outcome(asset_id: str) -> dict[str, Any] | None:
    """Return authoritative Polymarket resolution for a matched CFB outcome token."""
    if not asset_id:
        return None
    with PublicClient() as client:
        markets = list(
            client.list_markets(
                clob_token_ids=[asset_id],
                closed=True,
                page_size=5,
            ).iter_items()
        )

    for market in markets:
        state = getattr(market, "state", None)
        resolution = getattr(market, "resolution", None)
        status = str(getattr(resolution, "uma_resolution_status", "") or "").lower()
        if not bool(getattr(state, "closed", False)) or status not in {"resolved", "settled"}:
            continue
        outcomes = getattr(market, "outcomes", None)
        for outcome in (
            getattr(outcomes, "yes", None),
            getattr(outcomes, "no", None),
        ):
            if outcome is None or str(getattr(outcome, "token_id", "")) != str(asset_id):
                continue
            try:
                price = Decimal(str(getattr(outcome, "price", "")))
            except Exception:
                return None
            if price >= Decimal("0.9999"):
                result, terminal = "WIN", Decimal("1")
            elif price <= Decimal("0.0001"):
                result, terminal = "LOSS", Decimal("0")
            elif abs(price - Decimal("0.5")) <= Decimal("0.0001"):
                result, terminal = "PUSH", Decimal("0.5")
            else:
                return None
            return {
                "pick_result": result,
                "settlement_terminal_price": str(terminal),
                "settlement_market_id": str(getattr(market, "id", "") or ""),
                "settlement_market_slug": str(getattr(market, "slug", "") or ""),
                "settlement_checked_at": _now_iso(),
            }
    return None


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
    explicit = [str(x).strip() for x in (pick.get("event_hints") or []) if str(x).strip()]
    if len(explicit) == 2:
        return explicit
    if kind == "total":
        return explicit
    hint = str(pick.get("team_hint") or "").strip()
    return [hint] if hint else []


def _event_date(event: Any) -> date | None:
    """Best-effort event date from Gamma schedule metadata or CFB slug/title."""
    schedule = getattr(event, "schedule", None)
    for source in (schedule, event):
        if source is None:
            continue
        for attr in (
            "event_date",
            "eventDate",
            "start_time",
            "startTime",
            "start_date",
            "startDate",
            "end_date",
            "endDate",
        ):
            value = getattr(source, attr, None)
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            parsed = _parse_iso(value)
            if parsed is not None:
                return parsed.date()

    text = " ".join(
        [
            str(getattr(event, "slug", "") or ""),
            str(getattr(event, "title", "") or ""),
        ]
    )
    match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", text)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _event_key(event: Any) -> str:
    return str(
        getattr(event, "id", "")
        or getattr(event, "slug", "")
        or getattr(event, "title", "")
    )


def _narrow_one_team_events_by_posted_date(
    ranked: list[tuple[int, Any, Any, str, Any]],
    pick: dict[str, Any],
) -> list[tuple[int, Any, Any, str, Any]]:
    """Narrow one-team CFB picks to one nearby scheduled event without guessing."""
    posted = _parse_iso(pick.get("posted_at"))
    if posted is None:
        return ranked

    candidates: dict[str, int] = {}
    posted_day = posted.date()
    for row in ranked:
        key = _event_key(row[1])
        event_day = _event_date(row[1])
        if not key or event_day is None:
            continue
        delta_days = (event_day - posted_day).days
        # Allow one day behind for UTC/local-date boundaries and at most four
        # days ahead. A week-later game must never inherit an old stale pick.
        if -1 <= delta_days <= 4:
            candidates[key] = delta_days

    if not candidates:
        return []

    def priority(delta_days: int) -> tuple[int, int]:
        return (0 if delta_days >= 0 else 1, abs(delta_days))

    best_priority = min(priority(delta) for delta in candidates.values())
    best_keys = {
        key
        for key, delta in candidates.items()
        if priority(delta) == best_priority
    }
    if len(best_keys) != 1:
        return [row for row in ranked if _event_key(row[1]) in best_keys]

    best_key = next(iter(best_keys))
    return [row for row in ranked if _event_key(row[1]) == best_key]


def _full_game_market_type_matches(actual: str, kind: str) -> bool:
    normalized = nfl._norm(actual)
    allowed = {
        "moneyline": {"moneyline"},
        "spread": {"spread", "spreads"},
        "total": {"total", "totals"},
    }
    return normalized in allowed.get(kind, set())


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
        requested = list(pick.get("spread_lines") or [None])[0]
        try:
            requested_line = Decimal(str(requested))
        except Exception:
            return None
        selected = _spread_outcome_any_line(market, pick)
        if selected is None:
            return None
        label, obj, effective_line = selected
        if effective_line != requested_line:
            return None
        return label, obj

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
                if not _full_game_market_type_matches(actual, kind):
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

    if len(hints) == 1:
        ranked = _narrow_one_team_events_by_posted_date(ranked, pick)
        if not ranked:
            raise ValueError(
                "No nearby dated Polymarket CFB event matched the one-team pick"
            )

    event_keys = {_event_key(row[1]) for row in ranked}
    event_keys.discard("")
    if len(event_keys) != 1:
        raise ValueError(
            "CFB pick matched multiple open events and no unique nearby game could be resolved"
        )

    ranked.sort(key=lambda row: row[0], reverse=True)
    best = ranked[0]
    if len(ranked) > 1 and ranked[1][0] == best[0]:
        first_id = str(getattr(best[2], "id", "") or getattr(best[2], "slug", ""))
        second_id = str(getattr(ranked[1][2], "id", "") or getattr(ranked[1][2], "slug", ""))
        if first_id != second_id:
            raise ValueError("Multiple equally strong CFB markets matched; preview blocked")
    return best[1], best[2], best[3], best[4]


def _format_signed_line(value: Decimal) -> str:
    number = value.normalize()
    body = format(abs(number), "f")
    if "." in body:
        body = body.rstrip("0").rstrip(".")
    sign = "+" if number >= 0 else "-"
    return f"{sign}{body}"


def _spread_outcome_any_line(
    market: Any,
    pick: dict[str, Any],
) -> tuple[str, Any, Decimal] | None:
    """Return selected team/outcome plus its effective full-game spread line.

    Gamma encodes a market such as Indiana -20.5 as:
      question = "Spread: Indiana (-20.5)"
      outcomes = ["Indiana", "Northwestern"]
      sports.line = -20.5

    The second outcome is therefore the complementary Northwestern +20.5 side.
    We only invert when the question subject maps unambiguously to the other
    outcome; otherwise fail closed.
    """
    outcomes = [(label, obj) for label, obj in nfl._outcomes(market) if obj is not None]
    if len(outcomes) != 2:
        return None

    team_hint = str(pick.get("team_hint") or "").strip()
    if not team_hint:
        return None

    selected_indexes = [
        i for i, (label, _) in enumerate(outcomes)
        if _contains_hint(label, team_hint)
    ]
    if len(selected_indexes) != 1:
        return None
    selected_index = selected_indexes[0]

    question = str(getattr(market, "question", "") or "")
    subject_match = re.search(
        r"(?:^|\b)Spread:\s*(.+?)\s*\(\s*([+-]\d{1,2}(?:\.\d+)?)\s*\)\s*$",
        question,
        re.I,
    )
    if not subject_match:
        return None

    subject = subject_match.group(1).strip()
    try:
        question_line = Decimal(subject_match.group(2))
    except Exception:
        return None

    structured_raw = getattr(getattr(market, "sports", None), "line", None)
    if structured_raw is not None:
        try:
            structured_line = Decimal(str(structured_raw))
        except Exception:
            return None
        if structured_line != question_line:
            return None
        base_line = structured_line
    else:
        base_line = question_line

    subject_indexes = [
        i for i, (label, _) in enumerate(outcomes)
        if _contains_hint(label, subject)
    ]
    if len(subject_indexes) != 1:
        return None
    subject_index = subject_indexes[0]

    effective_line = base_line if selected_index == subject_index else -base_line
    label, obj = outcomes[selected_index]
    return label, obj, effective_line


def _find_spread_alternatives(
    pick: dict[str, Any],
    *,
    limit: int = 4,
) -> list[dict[str, Any]]:
    """Find explicit nearby spread lines for the same picked team/event."""
    hints = _event_hints_for_pick(pick, "spread")
    if not hints:
        return []

    try:
        original = Decimal(str(list(pick.get("spread_lines") or [None])[0]))
    except Exception:
        return []

    rows: list[tuple[int, Any, Any, str, Any, Decimal]] = []
    with PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in hints:
            result = client.list_events(title_search=query, closed=False, page_size=30).first_page()
            for event in result.items:
                key = _event_key(event)
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
                if not _full_game_market_type_matches(actual, "spread"):
                    continue
                if not getattr(getattr(market, "state", None), "accepting_orders", False):
                    continue
                selected = _spread_outcome_any_line(market, pick)
                if selected is None:
                    continue
                label, obj, line = selected
                if line == original:
                    continue
                distance = abs(line - original)
                rows.append((int(distance * 100), event, market, label, obj, line))

    if not rows:
        return []

    if len(hints) == 1:
        rows = _narrow_one_team_events_by_posted_date(rows, pick)
        if not rows:
            return []

    event_keys = {_event_key(row[1]) for row in rows}
    event_keys.discard("")
    if len(event_keys) != 1:
        return []

    # For equal distance, prefer the more favorable spread for the selected team
    # (numerically larger: +21.5 over +20.5, -6 over -6.5).
    rows.sort(key=lambda row: (row[0], 0 if row[5] >= original else 1, -row[5]))

    alternatives: list[dict[str, Any]] = []
    seen_lines: set[str] = set()
    for _, event, market, label, obj, line in rows:
        line_text = _format_signed_line(line)
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
            quote = _read_live_buy_quote(asset_id)
        except Exception:
            continue

        event_slug = str(getattr(event, "slug", "") or "")
        if not event_slug.startswith("cfb-"):
            continue
        lifecycle = _event_lifecycle_metadata(event, market)
        alt_id = hashlib.sha256(f"{asset_id}|{line_text}".encode("utf-8")).hexdigest()[:16]
        relative = "BETTER" if line > original else "WORSE"
        if line == original:
            relative = "SAME"
        match = {
            "alternative_id": alt_id,
            "match_status": "ALTERNATE",
            "market_type": "spread",
            "event_slug": event_slug,
            "event_title": str(getattr(event, "title", "") or ""),
            **lifecycle,
            "market": str(
                getattr(market, "question", "")
                or getattr(event, "title", "CFB spread")
            ),
            "market_url": f"https://polymarket.com/sports/cfb/{event_slug}",
            "outcome": label,
            "asset_id": asset_id,
            "spread_line": line_text,
            "original_spread_line": _format_signed_line(original),
            "relative_to_original": relative,
            **quote,
        }
        match["event_phase"] = _event_phase(match)
        alternatives.append(match)
        if len(alternatives) >= max(1, limit):
            break

    return alternatives


def _refresh_spread_alternatives(record: dict[str, Any]) -> bool:
    pick = record.get("pick")
    if not isinstance(pick, dict):
        return False

    existing = record.get("live_alternatives") or []
    if isinstance(existing, list) and existing:
        first = existing[0] if isinstance(existing[0], dict) else None
        if first:
            try:
                lifecycle = _refresh_saved_event_state(first)
                phase = _event_phase(lifecycle)
                if phase == "CLOSED":
                    changed = False
                    for key, value in lifecycle.items():
                        if record.get(key) != value:
                            record[key] = value
                            changed = True
                    if record.get("event_phase") != "CLOSED":
                        record["event_phase"] = "CLOSED"
                        changed = True
                    if record.get("status") != "EVENT_CLOSED":
                        record["status"] = "EVENT_CLOSED"
                        record["reason"] = "matched Polymarket event/market is closed"
                        changed = True
                    return changed
            except Exception:
                pass

    try:
        alternatives = _find_spread_alternatives(pick)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if record.get("alternate_error") != error:
            record["alternate_error"] = error
            return True
        return False

    changed = record.get("live_alternatives") != alternatives
    record["live_alternatives"] = alternatives
    record["alternate_error"] = None
    if not alternatives:
        if record.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
            record["status"] = "RETRYING"
            record["reason"] = "exact spread unavailable and no current explicit alternate spread is open"
            changed = True
        return changed

    phase = str(alternatives[0].get("event_phase") or "PREGAME")
    status = "MATCHED_LIVE_ALTERNATE" if phase == "LIVE" else "MATCHED_PREGAME_ALTERNATE"
    reason = (
        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
    )
    for key in ("event_title", "event_start_at", "market_url"):
        value = alternatives[0].get(key)
        if record.get(key) != value:
            record[key] = value
            changed = True
    if record.get("event_phase") != phase:
        record["event_phase"] = phase
        changed = True
    if record.get("status") != status:
        record["status"] = status
        changed = True
    if record.get("match_status") != "ALTERNATE_AVAILABLE":
        record["match_status"] = "ALTERNATE_AVAILABLE"
        changed = True
    if record.get("reason") != reason:
        record["reason"] = reason
        changed = True
    return changed


def _precise_event_start(event: Any, market: Any | None = None) -> datetime | None:
    """Return an actual kickoff timestamp; reject date-only metadata."""
    market_sports = getattr(market, "sports", None) if market is not None else None
    schedule = getattr(event, "schedule", None)
    sources = (market_sports, schedule, event)
    # Sports-specific kickoff fields are more reliable than generic event dates.
    for attrs in (
        (
            "event_start_time", "eventStartTime",
            "game_start_time", "gameStartTime",
            "start_time", "startTime",
            "scheduled_at", "scheduledAt",
        ),
        ("start_date", "startDate"),
    ):
        for source in sources:
            if source is None:
                continue
            for attr in attrs:
                value = getattr(source, attr, None)
                if value is None:
                    continue
                text = str(value).strip()
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                    continue
                parsed = _parse_iso(value)
                if parsed is not None:
                    return parsed
    return None


def _event_lifecycle_metadata(event: Any, market: Any | None = None) -> dict[str, Any]:
    """Read Gamma's nested event state/schedule/sports lifecycle fields."""
    state = getattr(event, "state", None)
    schedule = getattr(event, "schedule", None)
    sports = getattr(event, "sports", None)
    start_at = _precise_event_start(event, market)

    finished_at = None
    for source in (schedule, event):
        if source is None:
            continue
        for attr in ("finished_at", "finishedAt", "closed_time", "closedTime"):
            finished_at = _parse_iso(getattr(source, attr, None))
            if finished_at is not None:
                break
        if finished_at is not None:
            break

    def flag(name: str, fallback: str | None = None) -> bool | None:
        value = getattr(state, name, None) if state is not None else None
        if value is None and fallback:
            value = getattr(event, fallback, None)
        return bool(value) if value is not None else None

    accepting = getattr(getattr(market, "state", None), "accepting_orders", None)
    game_status = str(getattr(sports, "game_status", "") or "").strip() or None
    return {
        "event_start_at": start_at.isoformat() if start_at is not None else None,
        "event_start_checked_at": _now_iso(),
        "event_finished_at": finished_at.isoformat() if finished_at is not None else None,
        "event_closed": flag("closed", "closed"),
        "event_ended": flag("ended"),
        "event_live": flag("live"),
        "game_status": game_status,
        "market_accepting_orders": bool(accepting) if accepting is not None else None,
    }


def _event_phase(record: dict[str, Any], now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    status = nfl._norm(record.get("game_status") or "")
    final_statuses = {
        "final", "finished", "ended", "complete", "completed",
        "closed", "post", "postgame",
    }
    if bool(record.get("event_closed")) or bool(record.get("event_ended")):
        return "CLOSED"
    finished = _parse_iso(record.get("event_finished_at"))
    if finished is not None and current >= finished:
        return "CLOSED"
    if status in final_statuses or status.startswith("final"):
        return "CLOSED"
    if record.get("market_accepting_orders") is False:
        return "CLOSED"
    if bool(record.get("event_live")):
        return "LIVE"
    if status in {"live", "in progress", "inprogress", "halftime", "half time"}:
        return "LIVE"
    start = _parse_iso(record.get("event_start_at"))
    if start is not None and current >= start:
        return "LIVE"
    return "PREGAME"


def _american_odds_from_price(value: Any) -> str | None:
    try:
        price = Decimal(str(value))
    except Exception:
        return None
    if price <= 0 or price >= 1:
        return None
    if price >= Decimal("0.5"):
        raw = -(Decimal("100") * price / (Decimal("1") - price))
    else:
        raw = Decimal("100") * (Decimal("1") - price) / price
    rounded = int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return f"+{rounded}" if rounded > 0 else str(rounded)


def _resolve_market_match(pick: dict[str, Any], kind: str) -> dict[str, Any]:
    """Resolve and persist the exact Polymarket event/market before any BUY action."""
    event, market, outcome_label, outcome_obj = _find_market(pick, kind)
    asset_id = str(
        getattr(outcome_obj, "token_id", None)
        or getattr(outcome_obj, "position_id", None)
        or ""
    )
    if not asset_id:
        raise RuntimeError("Matched CFB market has no tradable outcome token")

    event_slug = str(getattr(event, "slug", "") or "")
    if not event_slug.startswith("cfb-"):
        raise RuntimeError("Matched event is not a CFB Polymarket event")

    lifecycle = _event_lifecycle_metadata(event, market)
    return {
        "match_status": "MATCHED",
        "matched_at": _now_iso(),
        "market_type": kind,
        "event_slug": event_slug,
        "event_title": str(getattr(event, "title", "") or ""),
        **lifecycle,
        "market": str(
            getattr(market, "question", "")
            or getattr(event, "title", "CFB market")
        ),
        "market_url": f"https://polymarket.com/sports/cfb/{event_slug}",
        "outcome": outcome_label,
        "asset_id": asset_id,
    }


def _saved_market_match(record: dict[str, Any] | None, kind: str | None = None) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    market_type = str(record.get("market_type") or "")
    if kind and market_type != kind:
        return None
    asset_id = str(record.get("asset_id") or "")
    market_url = str(record.get("market_url") or "")
    outcome = str(record.get("outcome") or "")
    if not asset_id or not outcome or not market_url.startswith("https://polymarket.com/sports/cfb/"):
        return None
    event_slug = str(record.get("event_slug") or market_url.rstrip("/").rsplit("/", 1)[-1])
    if not event_slug.startswith("cfb-"):
        return None
    return {
        "match_status": "MATCHED",
        "matched_at": record.get("matched_at"),
        "market_type": market_type,
        "event_slug": event_slug,
        "event_title": record.get("event_title"),
        "event_start_at": record.get("event_start_at"),
        "event_start_checked_at": record.get("event_start_checked_at"),
        "event_closed": record.get("event_closed"),
        "event_ended": record.get("event_ended"),
        "event_live": record.get("event_live"),
        "event_finished_at": record.get("event_finished_at"),
        "game_status": record.get("game_status"),
        "market_accepting_orders": record.get("market_accepting_orders"),
        "market": record.get("market"),
        "market_url": market_url,
        "outcome": outcome,
        "asset_id": asset_id,
    }


def _stored_match_within_pick_window(record: dict[str, Any]) -> bool:
    """Reject legacy one-team matches that point at a later unrelated game."""
    pick = record.get("pick")
    if not isinstance(pick, dict):
        return True
    kind, _ = _classify_pick(pick)
    if kind is None:
        return True
    hints = _event_hints_for_pick(pick, kind)
    if len(hints) != 1:
        return True

    posted = _parse_iso(pick.get("posted_at"))
    if posted is None:
        return True

    event_day = None
    start = _parse_iso(record.get("event_start_at"))
    if start is not None:
        event_day = start.date()
    if event_day is None:
        slug = str(record.get("event_slug") or "")
        match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", slug)
        if match:
            try:
                event_day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                event_day = None
    if event_day is None:
        return True

    delta_days = (event_day - posted.date()).days
    return -1 <= delta_days <= 4


def _repair_stored_future_match(record: dict[str, Any]) -> bool:
    """Migrate a legacy wrong-game match or close it if the original event is gone."""
    pick = record.get("pick")
    if not isinstance(pick, dict):
        return False
    kind, reason = _classify_pick(pick)
    if kind is None:
        record["status"] = "IGNORED_UNSUPPORTED"
        record["reason"] = reason or "unsupported CFB pick"
        return True

    try:
        corrected = _resolve_market_match(pick, kind)
    except Exception as exact_exc:
        if kind == "spread":
            try:
                alternatives = _find_spread_alternatives(pick)
            except Exception:
                alternatives = []
            if alternatives:
                first = alternatives[0]
                for key in (
                    "asset_id", "outcome", "market", "matched_at",
                    "event_closed", "event_ended", "event_live",
                    "event_finished_at", "game_status", "market_accepting_orders",
                ):
                    record.pop(key, None)
                record["live_alternatives"] = alternatives
                record["match_status"] = "ALTERNATE_AVAILABLE"
                record["event_slug"] = first.get("event_slug")
                record["event_title"] = first.get("event_title")
                record["event_start_at"] = first.get("event_start_at")
                record["event_start_checked_at"] = first.get("event_start_checked_at")
                record["event_phase"] = first.get("event_phase")
                record["market_url"] = first.get("market_url")
                record["status"] = (
                    "MATCHED_LIVE_ALTERNATE"
                    if first.get("event_phase") == "LIVE"
                    else "MATCHED_PREGAME_ALTERNATE"
                )
                record["reason"] = (
                    "legacy future-game match rejected; showing explicit nearby live alternatives"
                )
                record["match_error"] = f"{type(exact_exc).__name__}: {exact_exc}"
                return True

        # The previously stored event is known to be outside the allowed pick
        # window, and no valid nearby open replacement exists. Do not let the
        # stale signal become buyable against a later game.
        record["event_closed"] = True
        record["event_ended"] = True
        record["event_live"] = False
        record["market_accepting_orders"] = False
        record["event_phase"] = "CLOSED"
        record["status"] = "EVENT_CLOSED"
        record["match_status"] = "INVALID_FUTURE_MATCH"
        record["reason"] = (
            "legacy future-game match rejected; original nearby event is no longer open"
        )
        record["match_error"] = f"{type(exact_exc).__name__}: {exact_exc}"
        return True

    record.update(corrected)
    record["event_phase"] = _event_phase(record)
    record["reason"] = "legacy future-game match corrected to the nearby original event"
    record["match_error"] = None
    return True


def _refresh_saved_event_state(record: dict[str, Any]) -> dict[str, Any]:
    """Refresh the exact stored Gamma event/market by slug; never roll to another game."""
    event_slug = str(record.get("event_slug") or "")
    asset_id = str(record.get("asset_id") or "")
    if not event_slug.startswith("cfb-") or not asset_id:
        raise RuntimeError("Stored CFB match is missing event slug or asset id")

    with PublicClient() as client:
        event = client.get_event(slug=event_slug)

    matched_market = None
    for market in getattr(event, "markets", ()) or ():
        for _, outcome in nfl._outcomes(market):
            if outcome is None:
                continue
            token = str(
                getattr(outcome, "token_id", None)
                or getattr(outcome, "position_id", None)
                or ""
            )
            if token == asset_id:
                matched_market = market
                break
        if matched_market is not None:
            break

    lifecycle = _event_lifecycle_metadata(event, matched_market)
    if matched_market is None and not (
        lifecycle.get("event_closed")
        or lifecycle.get("event_ended")
        or _event_phase(lifecycle) == "CLOSED"
    ):
        raise RuntimeError("Stored CFB asset is no longer present on its matched event")

    return {
        "event_title": str(getattr(event, "title", "") or record.get("event_title") or ""),
        **lifecycle,
    }


def _read_live_buy_quote(asset_id: str) -> dict[str, str]:
    """Read current executable BUY data without applying trade-entry limits."""
    with PublicClient() as client:
        buy_price = Decimal(str(client.get_price(asset_id=asset_id, side="BUY")))
        spread = Decimal(str(client.get_spread(asset_id=asset_id)))
        book = client.get_order_book(asset_id=asset_id)

    asks = getattr(book, "asks", None) or []
    best_ask = min((Decimal(str(level.price)) for level in asks), default=None)
    if buy_price <= 0 or buy_price >= 1:
        raise RuntimeError(f"Invalid current BUY price {buy_price}")
    if best_ask is None or best_ask <= 0 or best_ask >= 1:
        raise RuntimeError("No valid current ask is available")
    return {
        "current_buy_price": str(buy_price),
        "best_ask": str(best_ask),
        "max_price": str(best_ask),
        "spread": str(spread),
        "live_odds_american": _american_odds_from_price(best_ask),
        "quote_updated_at": _now_iso(),
    }


def _refresh_live_buy_quote(asset_id: str, core: Any) -> dict[str, str]:
    """Refresh executable BUY data and enforce live trading limits."""
    quote = _read_live_buy_quote(asset_id)
    best_ask = Decimal(quote["best_ask"])
    spread = Decimal(quote["spread"])
    if best_ask > core.MAX_PRICE:
        raise RuntimeError(f"Current best ask {best_ask} exceeds MAX_PRICE={core.MAX_PRICE}")
    if spread > core.MAX_SPREAD:
        raise RuntimeError(f"Current spread {spread} exceeds MAX_SPREAD={core.MAX_SPREAD}")
    return quote


def _refresh_record_runtime(record: dict[str, Any]) -> bool:
    """Refresh pregame lifecycle and dashboard odds for one matched signal."""
    match = _saved_market_match(record)
    if match is None:
        return False

    changed = False
    if not _stored_match_within_pick_window(record):
        return _repair_stored_future_match(record)

    try:
        refreshed_state = _refresh_saved_event_state(record)
        for key, value in refreshed_state.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
        if record.get("event_metadata_error") is not None:
            record["event_metadata_error"] = None
            changed = True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if record.get("event_metadata_error") != error:
            record["event_metadata_error"] = error
            changed = True

    match = _saved_market_match(record) or match
    phase = _event_phase(record)

    if record.get("event_phase") != phase:
        record["event_phase"] = phase
        changed = True

    if phase == "CLOSED":
        if record.get("status") != "EVENT_CLOSED":
            record["status"] = "EVENT_CLOSED"
            record["reason"] = "matched Polymarket event/market is closed"
            changed = True
        if str(record.get("pick_result") or "").upper() not in {"WIN", "LOSS", "PUSH"}:
            try:
                resolved = _resolved_signal_outcome(str(match["asset_id"]))
                if resolved:
                    for key, value in resolved.items():
                        if record.get(key) != value:
                            record[key] = value
                            changed = True
                    if record.get("settlement_error") is not None:
                        record["settlement_error"] = None
                        changed = True
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                if record.get("settlement_error") != error:
                    record["settlement_error"] = error
                    changed = True
        return changed

    if phase == "LIVE":
        if record.get("status") != "MATCHED_LIVE":
            record["status"] = "MATCHED_LIVE"
            record["reason"] = "game is live; matched Polymarket market remains open"
            changed = True
    elif record.get("status") == "IGNORED_STALE":
        record["status"] = "MATCHED_PREGAME"
        record["reason"] = "outside auto-trade freshness window; manual BUY remains available while market is open"
        changed = True

    try:
        quote = _read_live_buy_quote(str(match["asset_id"]))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if record.get("live_quote_error") != error:
            record["live_quote_error"] = error
            changed = True
        return changed

    for key, value in quote.items():
        if record.get(key) != value:
            record[key] = value
            changed = True
    if record.get("live_quote_error") is not None:
        record["live_quote_error"] = None
        changed = True
    return changed


def _prepare_preview(
    pick: dict[str, Any],
    *,
    core: Any,
    remote: Any,
    unit_usdc: Decimal,
    matched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kind, reason = _classify_pick(pick)
    if kind is None:
        raise RuntimeError(reason or "unsupported CFB pick")

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

    match = _saved_market_match(matched, kind) or _resolve_market_match(pick, kind)
    asset_id = str(match["asset_id"])
    quote = _refresh_live_buy_quote(asset_id, core)
    best_ask = Decimal(quote["best_ask"])

    source_label = _source_label(pick)
    fp = _fingerprint(pick)
    trade_id = f"cfb-capper-{fp[:18]}"
    payload = {
        "market_url": match["market_url"],
        "outcome": match["outcome"],
        "market_type": kind,
        "asset_id": asset_id,
        "max_price": str(best_ask),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
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
        "trade_id": trade_id,
        "strategy_source": source_label,
        **match,
        "units": str(units),
        "stake_usdc": str(stake),
        **quote,
    }


def _existing_manual_buy(core: Any, remote: Any, fingerprint: str) -> dict[str, Any] | None:
    try:
        remote._expire_stale_buys_persisted()
    except Exception:
        pass

    try:
        queue = remote._queue_load()
    except Exception:
        queue = {}

    for queued in queue.values():
        if queued.get("action") != "BUY" or queued.get("status") not in {"PENDING", "LEASED"}:
            continue
        payload = queued.get("payload") or {}
        if str(payload.get("strategy_pick_id") or "") == fingerprint:
            return queued

    executions_file = getattr(core, "EXECUTIONS_FILE", None)
    if executions_file is not None:
        try:
            executions = core._load(executions_file)
        except Exception:
            executions = {}
        for trade in executions.values():
            if trade.get("status") not in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
                continue
            if str(trade.get("strategy_pick_id") or "") == fingerprint:
                return trade
    return None


def _prepare_manual_buy(
    pick: dict[str, Any],
    *,
    core: Any,
    remote: Any,
    unit_usdc: Decimal,
    matched: dict[str, Any] | None = None,
    strategy_pick_id: str | None = None,
    strategy_selection: str | None = None,
    strategy_alternate_line: str | None = None,
) -> dict[str, Any]:
    """Queue one user-authorized CFB BUY after refreshing the live market."""
    kind, reason = _classify_pick(pick)
    if kind is None:
        raise RuntimeError(reason or "unsupported CFB pick")
    if matched is not None and _event_phase(matched) == "CLOSED":
        raise RuntimeError("Matched CFB market is closed")

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

    fp = str(strategy_pick_id or _fingerprint(pick))
    if _existing_manual_buy(core, remote, fp) is not None:
        raise RuntimeError("A live CFB BUY for this signal is already queued or open")

    used = core._daily_budget_used()
    pending = nfl._pending_auto_budget(remote)
    if used + pending + stake > core.MAX_DAILY_BUDGET_USDC:
        raise RuntimeError(
            "Daily auto budget guard would be exceeded: "
            f"used=${used}, pending=${pending}, requested=${stake}, "
            f"limit=${core.MAX_DAILY_BUDGET_USDC}"
        )

    match = _saved_market_match(matched, kind) or _resolve_market_match(pick, kind)
    asset_id = str(match["asset_id"])
    quote = _refresh_live_buy_quote(asset_id, core)
    best_ask = Decimal(quote["best_ask"])

    source_label = _source_label(pick)
    trade_id = f"cfb-manual-{fp[:12]}-{uuid.uuid4().hex[:6]}"
    payload = {
        "market_url": match["market_url"],
        "outcome": match["outcome"],
        "market_type": kind,
        "asset_id": asset_id,
        "max_price": str(best_ask),
        "budget_usdc": str(stake),
        "trade_id": trade_id,
        "max_spread": str(core.MAX_SPREAD),
        "max_price_global": str(core.MAX_PRICE),
        "source": "termux_executor",
        "auto": False,
        "manual": True,
        "strategy_execution_mode": "manual",
        "strategy_event_phase": _event_phase(match),
        "strategy_source": source_label,
        "strategy_sport": "CFB",
        "strategy_units": str(units),
        "strategy_unit_usdc": str(unit_usdc),
        "strategy_pick_id": fp,
        "strategy_posted_at": pick.get("posted_at"),
        "strategy_selection": strategy_selection or pick.get("selection"),
        "strategy_execution_selection": pick.get("selection"),
        "strategy_alternate_line": strategy_alternate_line,
        "strategy_telegram_source": pick.get("source"),
    }
    queued = remote._enqueue("BUY", payload)
    return {
        "status": "MANUAL_BUY_QUEUED",
        "request_id": queued["id"],
        "trade_id": trade_id,
        "strategy_source": source_label,
        **match,
        "units": str(units),
        "stake_usdc": str(stake),
        **quote,
    }


def _execution_for_signal(
    record: dict[str, Any],
    executions: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(executions, dict):
        return None
    signal_id = str(record.get("id") or "")
    manual_trade_id = str(record.get("manual_buy_trade_id") or "")
    matches: list[dict[str, Any]] = []
    for rec in executions.values():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        if manual_trade_id and str(rec.get("id") or "") == manual_trade_id:
            matches.append(rec)
            continue
        if signal_id and str(rec.get("strategy_pick_id") or "") == signal_id:
            matches.append(rec)
    if not matches:
        return None
    matches.sort(
        key=lambda rec: (
            rec.get("realized_pnl") is not None,
            str(rec.get("closed_at") or rec.get("submitted_at") or rec.get("created_at") or ""),
        ),
        reverse=True,
    )
    return matches[0]


def _status_pick_item(
    record: dict[str, Any],
    executions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    saved_match = _saved_market_match(record)
    phase = _event_phase(record) if saved_match else record.get("event_phase")
    posted = _parse_iso(record.get("posted_at"))
    age_seconds = None
    if posted is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - posted).total_seconds()))
    execution = _execution_for_signal(record, executions)
    settlement = (execution or {}).get("settlement") or {}
    trade_result = str(settlement.get("result") or "").upper() or None
    realized_pnl = (execution or {}).get("realized_pnl")
    trade_stake = (execution or {}).get("actual_cost_usdc") or (execution or {}).get("budget_usdc")
    return {
        "status": record.get("status"),
        "selection": record.get("selection"),
        "posted_at": record.get("posted_at"),
        "updated_at": record.get("updated_at"),
        "signal_age_seconds": age_seconds,
        "units": record.get("units"),
        "stake_usdc": record.get("stake_usdc"),
        "match_status": "MATCHED" if saved_match else record.get("match_status"),
        "market_type": record.get("market_type"),
        "market": record.get("market"),
        "market_url": record.get("market_url"),
        "event_title": record.get("event_title"),
        "event_start_at": record.get("event_start_at"),
        "event_phase": phase,
        "outcome": record.get("outcome"),
        "asset_id": record.get("asset_id"),
        "current_buy_price": record.get("current_buy_price"),
        "best_ask": record.get("best_ask"),
        "spread": record.get("spread"),
        "live_odds_american": record.get("live_odds_american"),
        "quote_updated_at": record.get("quote_updated_at"),
        "live_quote_error": record.get("live_quote_error"),
        "live_alternatives": record.get("live_alternatives") or [],
        "alternate_error": record.get("alternate_error"),
        "reason": record.get("reason"),
        "last_error": record.get("last_error"),
        "request_id": record.get("request_id"),
        "signal_id": record.get("id"),
        "buy_available": bool(
            saved_match
            and phase in {"PREGAME", "LIVE"}
            and record.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
            }
        ),
        "manual_buy_request_id": record.get("manual_buy_request_id"),
        "manual_buy_status": record.get("manual_buy_status"),
        "manual_buy_error": record.get("manual_buy_error"),
        "pick_result": record.get("pick_result"),
        "settlement_terminal_price": record.get("settlement_terminal_price"),
        "settlement_error": record.get("settlement_error"),
        "trade_executed": execution is not None,
        "trade_status": (execution or {}).get("status"),
        "trade_result": trade_result,
        "trade_pnl_usdc": realized_pnl,
        "trade_stake_usdc": trade_stake,
    }


def _recent_status_items(
    rows: list[dict[str, Any]],
    status: str,
    *,
    limit: int = 30,
    executions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    matching = [row for row in rows if row.get("status") == status]
    matching.sort(
        key=lambda row: str(
            row.get("updated_at")
            or row.get("posted_at")
            or row.get("first_seen_at")
            or ""
        ),
        reverse=True,
    )
    return [_status_pick_item(row, executions) for row in matching[:limit]]


def _recent_all_items(
    rows: list[dict[str, Any]],
    *,
    limit: int = 60,
    executions: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    ordered = list(rows)
    ordered.sort(
        key=lambda row: str(
            row.get("updated_at")
            or row.get("posted_at")
            or row.get("first_seen_at")
            or ""
        ),
        reverse=True,
    )
    return [_status_pick_item(row, executions) for row in ordered[:limit]]


def _inject_dashboard_panel(html: str) -> str:
    if 'id="cfbCapperStats"' in html:
        return html

    panel = r"""
  <div class="nfl-capper-panel" id="cfbCapperStats">
    <div class="nfl-capper-head"><div><div class="label">CFB capper status</div><div class="nfl-capper-state" id="cfbCapperState">Loading…</div></div><div class="nfl-capper-meta" id="cfbCapperMeta"></div></div>
    <div class="nfl-capper-grid">
      <div class="nfl-capper-card"><b>Slam - CFB</b><div class="nfl-capper-kpis" id="cfbCapperSlam">—</div></div>
      <div class="nfl-capper-card"><b>Syndicate - CFB</b><div class="nfl-capper-kpis" id="cfbCapperSyndicate">—</div></div>
    </div>
  </div>
"""
    html = html.replace('  <div class="tabs">', panel + '  <div class="tabs">', 1)
    css = r"""
.cfb-capper-performance{font-size:15px;line-height:1.75;margin:3px 0 9px;color:var(--muted)}.cfb-capper-performance .capper-pnl{font-size:18px;font-weight:900}.cfb-capper-performance .capper-pnl.positive,.cfb-result-line.positive{color:#86efac}.cfb-capper-performance .capper-pnl.negative,.cfb-result-line.negative{color:#fca5a5}.cfb-capper-performance .capper-pnl.flat,.cfb-result-line.flat{color:var(--muted)}.cfb-result-line{font-size:16px;font-weight:900;margin-top:6px;line-height:1.35}@media(max-width:650px){.cfb-capper-performance{font-size:16px}.cfb-capper-performance .capper-pnl{font-size:19px}.cfb-result-line{font-size:17px}}
"""
    html = html.replace("</style>", css + "</style>", 1)

    js = r"""
function cfbEsc(v){
 return String(v??'').replace(/[&<>\"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
}
function cfbPickTime(v){
 if(!v)return '';
 const d=new Date(v);
 return Number.isNaN(d.getTime())?cfbEsc(v):cfbEsc(d.toLocaleString());
}
function cfbAge(seconds){
 if(seconds===null||seconds===undefined)return '';
 const s=Math.max(0,Number(seconds)||0);
 if(s<60)return Math.floor(s)+'s';
 if(s<3600)return Math.floor(s/60)+'m';
 return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
function cfbPhaseVisual(item){
 const phase=String(item.event_phase||'').toUpperCase();
 const status=String(item.status||'').toUpperCase();
 const result=String(item.trade_result||item.pick_result||'').toUpperCase();
 if(phase==='CLOSED'&&result==='WIN'){
  return {
   label:'WIN',
   row:'background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.42);border-left:4px solid #22c55e;',
   badge:'background:rgba(34,197,94,.20);border:1px solid rgba(34,197,94,.50);color:#86efac;'
  };
 }
 if(phase==='CLOSED'&&result==='LOSS'){
  return {
   label:'LOSS',
   row:'background:rgba(239,68,68,.11);border:1px solid rgba(239,68,68,.42);border-left:4px solid #ef4444;',
   badge:'background:rgba(239,68,68,.18);border:1px solid rgba(239,68,68,.50);color:#fca5a5;'
  };
 }
 if(phase==='CLOSED'&&result==='PUSH'){
  return {
   label:'PUSH',
   row:'background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.38);border-left:4px solid #f59e0b;',
   badge:'background:rgba(245,158,11,.16);border:1px solid rgba(245,158,11,.45);color:#fcd34d;'
  };
 }
 if(phase==='LIVE'||status==='MATCHED_LIVE'||status==='MATCHED_LIVE_ALTERNATE'){
  return {
   label:'LIVE',
   row:'background:rgba(34,197,94,.10);border:1px solid rgba(34,197,94,.38);border-left:4px solid #22c55e;',
   badge:'background:rgba(34,197,94,.18);border:1px solid rgba(34,197,94,.45);color:#86efac;'
  };
 }
 if(phase==='CLOSED'||status==='EVENT_CLOSED'){
  return {
   label:'FINISHED',
   row:'background:rgba(148,163,184,.08);border:1px solid rgba(148,163,184,.24);border-left:4px solid #94a3b8;opacity:.82;',
   badge:'background:rgba(148,163,184,.15);border:1px solid rgba(148,163,184,.32);color:#cbd5e1;'
  };
 }
 if(phase==='PREGAME'||status==='MATCHED_PREGAME'||status==='MATCHED_PREGAME_ALTERNATE'){
  return {
   label:'PREGAME',
   row:'background:rgba(59,130,246,.10);border:1px solid rgba(59,130,246,.35);border-left:4px solid #3b82f6;',
   badge:'background:rgba(59,130,246,.17);border:1px solid rgba(59,130,246,.42);color:#93c5fd;'
  };
 }
 return {
  label:'',
  row:'border:1px solid rgba(255,255,255,.07);',
  badge:''
 };
}
function cfbPickList(title,items,kind){
 if(!Array.isArray(items)||!items.length)return '';
 const rows=items.map(item=>{
  const meta=[];
  if(item.units!==null&&item.units!==undefined&&item.units!=='')meta.push(cfbEsc(item.units)+'u');
  if(item.stake_usdc!==null&&item.stake_usdc!==undefined&&item.stake_usdc!=='')meta.push('\u0024'+Number(item.stake_usdc).toFixed(2));
  if(item.posted_at)meta.push('posted '+cfbPickTime(item.posted_at)+(item.signal_age_seconds!==null&&item.signal_age_seconds!==undefined?' · age '+cfbAge(item.signal_age_seconds):''));
  if(item.market)meta.push('Matched: '+cfbEsc(item.market)+(item.outcome?' → '+cfbEsc(item.outcome):''));
  if(item.event_phase==='LIVE')meta.push('GAME LIVE');
  else if(item.event_phase==='CLOSED')meta.push('GAME FINISHED');
  else if(item.event_start_at)meta.push('starts '+cfbPickTime(item.event_start_at));
  if(item.best_ask!==null&&item.best_ask!==undefined&&item.best_ask!==''){
   const cents=(Number(item.best_ask)*100).toFixed(1).replace(/\.0$/,'');
   meta.push('Live BUY '+cents+'¢'+(item.live_odds_american?' ('+cfbEsc(item.live_odds_american)+')':''));
  }
  if(item.live_quote_error)meta.push('live odds unavailable');
  if(item.status)meta.push('status '+cfbEsc(item.status));
  if(item.reason&&['pregame','live','closed','unsupported','failed','signals'].includes(kind))meta.push(cfbEsc(item.reason));
  if(item.last_error&&['retrying','failed','signals'].includes(kind))meta.push(cfbEsc(item.last_error));
  if(!item.buy_available&&!['MATCHED','ALTERNATE_AVAILABLE'].includes(String(item.match_status||'')))meta.push('Polymarket match pending');
  if(item.manual_buy_status)meta.push('manual BUY '+cfbEsc(item.manual_buy_status));
  if(item.manual_buy_error)meta.push(cfbEsc(item.manual_buy_error));
  const state=String(item.manual_buy_status||'').toUpperCase();
  const locked=['PENDING','LEASED','DONE'].includes(state);
  const label=state==='DONE'?'BOUGHT':(state==='PENDING'||state==='LEASED'?'BUY '+state:'BUY LIVE');
  const buyAction=(item.signal_id&&item.buy_available)?'<button type="button" style="margin-top:6px" data-signal-id="'+cfbEsc(item.signal_id)+'" onclick="cfbManualBuy(this.dataset.signalId,this)"'+(locked?' disabled':'')+'>'+label+'</button>':'';
  const alternatives=Array.isArray(item.live_alternatives)?item.live_alternatives:[];
  const altActions=alternatives.map(alt=>{
   const cents=(Number(alt.best_ask)*100).toFixed(1).replace(/\.0$/,'');
   const relative=alt.relative_to_original==='BETTER'?' BETTER LINE':(alt.relative_to_original==='WORSE'?' WORSE LINE':'');
   const odds=alt.live_odds_american?' ('+cfbEsc(alt.live_odds_american)+')':'';
   return '<button type="button" style="margin-top:6px;margin-right:6px" data-signal-id="'+cfbEsc(item.signal_id)+'" data-alt-id="'+cfbEsc(alt.alternative_id)+'" onclick="cfbManualAltBuy(this.dataset.signalId,this.dataset.altId,this)"'+(locked?' disabled':'')+'>BUY '+cfbEsc(alt.spread_line)+' @ '+cents+'¢'+odds+relative+'</button>';
  }).join('');
  const marketAction=(item.market_url&&String(item.market_url).startsWith('https://polymarket.com/'))?'<a style="display:inline-block;margin:6px 0 0 8px" target="_blank" rel="noopener noreferrer" href="'+cfbEsc(item.market_url)+'">OPEN MARKET</a>':'';
  const action=buyAction+altActions+marketAction;
  const visual=cfbPhaseVisual(item);
  const badge=visual.label?'<span style="display:inline-block;margin-left:7px;padding:2px 6px;border-radius:999px;font-size:12px;font-weight:850;letter-spacing:.03em;vertical-align:1px;'+visual.badge+'">'+visual.label+'</span>':'';
  let pnlLine='';
  if(String(item.event_phase||'').toUpperCase()==='CLOSED'){
   if(item.trade_executed){
    const raw=item.trade_pnl_usdc===null||item.trade_pnl_usdc===undefined?null:Number(item.trade_pnl_usdc);
    const cls=raw===null||raw===0?'flat':(raw>0?'positive':'negative');
    const text=raw===null?'pending':(raw>0?'+':'')+' }).join('');
 return '<div style="margin-top:8px"><b>'+cfbEsc(title)+'</b>'+rows+'</div>';
}
async function cfbManualBuy(signalId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SUBMITTING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy/'+encodeURIComponent(signalId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function cfbManualAltBuy(signalId,altId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='RE-CHECKING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy-alternate/'+encodeURIComponent(signalId)+'/'+encodeURIComponent(altId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
const cfbActiveTabs={slam:'signals',syndicate:'signals'};
let cfbLastSources={};
function cfbTabSpec(x){
 return [
  ['signals','Signals',Number(x.signals||0),x.all_items||[],'signals'],
  ['queued','Queued',Number(x.preview_queued||0),x.queued_items||[],'queued'],
  ['done','Done',Number(x.preview_done||0),x.done_items||[],'done'],
  ['failed','Failed',Number(x.preview_failed||0),x.failed_items||[],'failed'],
  ['retrying','Retrying',Number(x.retrying||0),x.retrying_items||[],'retrying'],
  ['pregame','Pregame',Number(x.pregame||0),x.pregame_items||[],'pregame'],
  ['live','Live',Number(x.live||0),x.live_items||[],'live'],
  ['closed','Closed',Number(x.closed||0),x.closed_items||[],'closed'],
  ['unsupported','Unsupported',Number(x.unsupported||0),x.unsupported_items||[],'unsupported']
 ];
}
function cfbSetTab(sourceKey,tab){
 cfbActiveTabs[sourceKey]=tab;
 const x=cfbLastSources[sourceKey==='slam'?'Slam - CFB':'Syndicate - CFB'];
 const el=document.getElementById(sourceKey==='slam'?'cfbCapperSlam':'cfbCapperSyndicate');
 if(el&&x)el.innerHTML=cfbCapperLine(x,sourceKey);
}
function cfbCapperLine(x,sourceKey){
 if(!x)return 'No tracked signals yet';
 const p=x.performance||{};
 const roi=p.roi_pct===null||p.roi_pct===undefined?'—':Number(p.roi_pct).toFixed(1)+'%';
 const winPct=p.win_pct===null||p.win_pct===undefined?'—':Number(p.win_pct).toFixed(1)+'%';
 const rawPnl=p.realized_pnl_usdc===null||p.realized_pnl_usdc===undefined?null:Number(p.realized_pnl_usdc);
 const pnl=rawPnl===null?'—':(rawPnl>0?'+':'')+'  const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return performance+tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            existing_status = str(existing.get("status") or "")
            if existing_status in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE"}:
                continue
            if (
                existing_status == "EVENT_CLOSED"
                and str(existing.get("pick_result") or "").upper() in {"WIN", "LOSS", "PUSH"}
            ):
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        executions = core._load(core.EXECUTIONS_FILE)
        if not isinstance(executions, dict):
            executions = {}
        performance = nfl._stats_from_executions(
            executions,
            labels=SOURCE_LABELS,
            sport="CFB",
        )
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "performance": performance.get(label, {}),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows, executions=executions),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED", executions=executions),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE", executions=executions),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE", executions=executions),
                "retrying_items": _recent_status_items(rows, "RETRYING", executions=executions),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED", executions=executions),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30, executions=executions),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30, executions=executions),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED", executions=executions),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED", executions=executions),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
+raw.toFixed(2);
    pnlLine='<div class="cfb-result-line '+cls+'">Trade P/L '+text+'</div>';
   }else{
    pnlLine='<div class="cfb-result-line flat">Trade P/L — · NOT TRADED</div>';
   }
  }
  return '<div style="margin-top:7px;padding:9px 10px;border-radius:8px;'+visual.row+'"><b style="font-size:15px">'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<br><span>'+meta.join(' · ')+'</span>':'')+pnlLine+(action?'<br>'+action:'')+'</div>';
 }).join('');
 return '<div style="margin-top:8px"><b>'+cfbEsc(title)+'</b>'+rows+'</div>';
}
async function cfbManualBuy(signalId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SUBMITTING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy/'+encodeURIComponent(signalId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function cfbManualAltBuy(signalId,altId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='RE-CHECKING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy-alternate/'+encodeURIComponent(signalId)+'/'+encodeURIComponent(altId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
const cfbActiveTabs={slam:'signals',syndicate:'signals'};
let cfbLastSources={};
function cfbTabSpec(x){
 return [
  ['signals','Signals',Number(x.signals||0),x.all_items||[],'signals'],
  ['queued','Queued',Number(x.preview_queued||0),x.queued_items||[],'queued'],
  ['done','Done',Number(x.preview_done||0),x.done_items||[],'done'],
  ['failed','Failed',Number(x.preview_failed||0),x.failed_items||[],'failed'],
  ['retrying','Retrying',Number(x.retrying||0),x.retrying_items||[],'retrying'],
  ['pregame','Pregame',Number(x.pregame||0),x.pregame_items||[],'pregame'],
  ['live','Live',Number(x.live||0),x.live_items||[],'live'],
  ['closed','Closed',Number(x.closed||0),x.closed_items||[],'closed'],
  ['unsupported','Unsupported',Number(x.unsupported||0),x.unsupported_items||[],'unsupported']
 ];
}
function cfbSetTab(sourceKey,tab){
 cfbActiveTabs[sourceKey]=tab;
 const x=cfbLastSources[sourceKey==='slam'?'Slam - CFB':'Syndicate - CFB'];
 const el=document.getElementById(sourceKey==='slam'?'cfbCapperSlam':'cfbCapperSyndicate');
 if(el&&x)el.innerHTML=cfbCapperLine(x,sourceKey);
}
function cfbCapperLine(x,sourceKey){
 if(!x)return 'No tracked signals yet';
 const active=cfbActiveTabs[sourceKey]||'signals';
 const specs=cfbTabSpec(x);
 const tabs='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">'+specs.map(s=>{
  const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            if existing.get("status") in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED"),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "retrying_items": _recent_status_items(rows, "RETRYING"),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED"),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED"),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
+rawPnl.toFixed(2);
 const pnlClass=rawPnl===null||rawPnl===0?'flat':(rawPnl>0?'positive':'negative');
 const performance='<div class="cfb-capper-performance"><div>Bets '+(p.bets||0)+' · Open '+(p.open||0)+' · W-L-P '+(p.wins||0)+'-'+(p.losses||0)+'-'+(p.pushes||0)+' · Win '+winPct+'</div><div>Stake   const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            if existing.get("status") in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED"),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "retrying_items": _recent_status_items(rows, "RETRYING"),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED"),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED"),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
+raw.toFixed(2);
    pnlLine='<div class="cfb-result-line '+cls+'">Trade P/L '+text+'</div>';
   }else{
    pnlLine='<div class="cfb-result-line flat">Trade P/L — · NOT TRADED</div>';
   }
  }
  return '<div style="margin-top:7px;padding:9px 10px;border-radius:8px;'+visual.row+'"><b style="font-size:15px">'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<br><span>'+meta.join(' · ')+'</span>':'')+pnlLine+(action?'<br>'+action:'')+'</div>';
 }).join('');
 return '<div style="margin-top:8px"><b>'+cfbEsc(title)+'</b>'+rows+'</div>';
}
async function cfbManualBuy(signalId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SUBMITTING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy/'+encodeURIComponent(signalId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function cfbManualAltBuy(signalId,altId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='RE-CHECKING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy-alternate/'+encodeURIComponent(signalId)+'/'+encodeURIComponent(altId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
const cfbActiveTabs={slam:'signals',syndicate:'signals'};
let cfbLastSources={};
function cfbTabSpec(x){
 return [
  ['signals','Signals',Number(x.signals||0),x.all_items||[],'signals'],
  ['queued','Queued',Number(x.preview_queued||0),x.queued_items||[],'queued'],
  ['done','Done',Number(x.preview_done||0),x.done_items||[],'done'],
  ['failed','Failed',Number(x.preview_failed||0),x.failed_items||[],'failed'],
  ['retrying','Retrying',Number(x.retrying||0),x.retrying_items||[],'retrying'],
  ['pregame','Pregame',Number(x.pregame||0),x.pregame_items||[],'pregame'],
  ['live','Live',Number(x.live||0),x.live_items||[],'live'],
  ['closed','Closed',Number(x.closed||0),x.closed_items||[],'closed'],
  ['unsupported','Unsupported',Number(x.unsupported||0),x.unsupported_items||[],'unsupported']
 ];
}
function cfbSetTab(sourceKey,tab){
 cfbActiveTabs[sourceKey]=tab;
 const x=cfbLastSources[sourceKey==='slam'?'Slam - CFB':'Syndicate - CFB'];
 const el=document.getElementById(sourceKey==='slam'?'cfbCapperSlam':'cfbCapperSyndicate');
 if(el&&x)el.innerHTML=cfbCapperLine(x,sourceKey);
}
function cfbCapperLine(x,sourceKey){
 if(!x)return 'No tracked signals yet';
 const active=cfbActiveTabs[sourceKey]||'signals';
 const specs=cfbTabSpec(x);
 const tabs='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">'+specs.map(s=>{
  const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            if existing.get("status") in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED"),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "retrying_items": _recent_status_items(rows, "RETRYING"),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED"),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED"),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
+Number(p.graded_stake_usdc||0).toFixed(2)+' · <span class="capper-pnl '+pnlClass+'">P/L '+pnl+'</span> · ROI '+roi+'</div></div>';
 const active=cfbActiveTabs[sourceKey]||'signals';
 const specs=cfbTabSpec(x);
 const tabs='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">'+specs.map(s=>{
  const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            if existing.get("status") in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED"),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "retrying_items": _recent_status_items(rows, "RETRYING"),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED"),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED"),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
+raw.toFixed(2);
    pnlLine='<div class="cfb-result-line '+cls+'">Trade P/L '+text+'</div>';
   }else{
    pnlLine='<div class="cfb-result-line flat">Trade P/L — · NOT TRADED</div>';
   }
  }
  return '<div style="margin-top:7px;padding:9px 10px;border-radius:8px;'+visual.row+'"><b style="font-size:15px">'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<br><span>'+meta.join(' · ')+'</span>':'')+pnlLine+(action?'<br>'+action:'')+'</div>';
 }).join('');
 return '<div style="margin-top:8px"><b>'+cfbEsc(title)+'</b>'+rows+'</div>';
}
async function cfbManualBuy(signalId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SUBMITTING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy/'+encodeURIComponent(signalId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function cfbManualAltBuy(signalId,altId,btn){
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='RE-CHECKING…';
 try{
  const r=await fetch('/api/cfb-cappers/manual-buy-alternate/'+encodeURIComponent(signalId)+'/'+encodeURIComponent(altId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual alternate CFB BUY failed');
  btn.textContent='QUEUED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
const cfbActiveTabs={slam:'signals',syndicate:'signals'};
let cfbLastSources={};
function cfbTabSpec(x){
 return [
  ['signals','Signals',Number(x.signals||0),x.all_items||[],'signals'],
  ['queued','Queued',Number(x.preview_queued||0),x.queued_items||[],'queued'],
  ['done','Done',Number(x.preview_done||0),x.done_items||[],'done'],
  ['failed','Failed',Number(x.preview_failed||0),x.failed_items||[],'failed'],
  ['retrying','Retrying',Number(x.retrying||0),x.retrying_items||[],'retrying'],
  ['pregame','Pregame',Number(x.pregame||0),x.pregame_items||[],'pregame'],
  ['live','Live',Number(x.live||0),x.live_items||[],'live'],
  ['closed','Closed',Number(x.closed||0),x.closed_items||[],'closed'],
  ['unsupported','Unsupported',Number(x.unsupported||0),x.unsupported_items||[],'unsupported']
 ];
}
function cfbSetTab(sourceKey,tab){
 cfbActiveTabs[sourceKey]=tab;
 const x=cfbLastSources[sourceKey==='slam'?'Slam - CFB':'Syndicate - CFB'];
 const el=document.getElementById(sourceKey==='slam'?'cfbCapperSlam':'cfbCapperSyndicate');
 if(el&&x)el.innerHTML=cfbCapperLine(x,sourceKey);
}
function cfbCapperLine(x,sourceKey){
 if(!x)return 'No tracked signals yet';
 const active=cfbActiveTabs[sourceKey]||'signals';
 const specs=cfbTabSpec(x);
 const tabs='<div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:7px">'+specs.map(s=>{
  const selected=s[0]===active;
  return '<button type="button" style="padding:5px 8px;min-height:34px;'+(selected?'font-weight:700;opacity:1':'opacity:.72')+'" onclick="cfbSetTab(\''+sourceKey+'\',\''+s[0]+'\')">'+cfbEsc(s[1])+' '+s[2]+'</button>';
 }).join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const items=spec[3]||[];
 const body=items.length?cfbPickList(spec[1]+' signals',items,spec[4]):'<div style="margin-top:8px;opacity:.7">No '+cfbEsc(spec[1].toLowerCase())+' signals.</div>';
 return tabs+body;
}
async function loadCfbCapperStats(){
 try{
  const r=await fetch('/api/cfb-cappers/status',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'CFB capper status failed');
  const state=document.getElementById('cfbCapperState'),meta=document.getElementById('cfbCapperMeta');
  let mode=' · '+String(d.mode||'').replaceAll('_',' ');
  if(state)state.textContent=(d.enabled?'ENABLED':'DISABLED')+(d.enabled?mode:'');
  if(meta)meta.textContent='1u = \u0024'+Number(d.unit_usdc||10).toFixed(2)+' · auto fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · manual pregame/live while market open · odds refresh '+(d.poll_seconds||0)+'s';
  cfbLastSources=d.sources||{};
  const s=document.getElementById('cfbCapperSlam'),y=document.getElementById('cfbCapperSyndicate');
  if(s)s.innerHTML=cfbCapperLine(cfbLastSources['Slam - CFB'],'slam');
  if(y)y.innerHTML=cfbCapperLine(cfbLastSources['Syndicate - CFB'],'syndicate');
 }catch(e){
  const state=document.getElementById('cfbCapperState');if(state)state.textContent='Status unavailable: '+String(e);
 }
}
loadCfbCapperStats();setInterval(loadCfbCapperStats,10000);
"""
    return html.replace("</script>", js + "\n</script>", 1)


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
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("CFB_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
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
            manual_request_id = str(rec.get("manual_buy_request_id") or "")
            if manual_request_id:
                manual = queue.get(manual_request_id)
                if manual:
                    manual_status = str(manual.get("status") or "")
                    manual_error = manual.get("error")
                    manual_result = manual.get("result")
                    if rec.get("manual_buy_status") != manual_status:
                        rec["manual_buy_status"] = manual_status
                        changed = True
                    if rec.get("manual_buy_error") != manual_error:
                        rec["manual_buy_error"] = manual_error
                        changed = True
                    if manual_result is not None and rec.get("manual_buy_result") != manual_result:
                        rec["manual_buy_result"] = manual_result
                        changed = True
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
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 300,
                        "include_graded": "false",
                    },
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

        # Refresh current odds/lifecycle for every already matched active signal,
        # including older signals that may no longer be in the bridge feed.
        for existing in signals.values():
            if existing.get("status") in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                continue
            if existing.get("status") in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                if await asyncio.to_thread(_refresh_spread_alternatives, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True
                continue
            if _saved_market_match(existing) is not None:
                if await asyncio.to_thread(_refresh_record_runtime, existing):
                    existing["updated_at"] = _now_iso()
                    changed = True

        for pick in picks:
            fp = _fingerprint(pick)
            current = signals.get(fp)
            if current and not current.get("pick"):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
                "MATCHED_PREGAME", "MATCHED_LIVE",
                "MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE", "EVENT_CLOSED",
                "IGNORED_STALE", "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                terminal = str(current.get("status") or "")
                if terminal in {"IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE", "EVENT_CLOSED"}:
                    continue
                if terminal in {"MATCHED_PREGAME_ALTERNATE", "MATCHED_LIVE_ALTERNATE"}:
                    continue
                if (
                    terminal != "IGNORED_STALE"
                    and _saved_market_match(current) is not None
                    and current.get("event_start_checked_at")
                ):
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
                "pick": pick,
            }

            if source_label is None:
                record["status"] = "IGNORED_UNTRACKED_SOURCE"
                record["reason"] = "CFB feed source is not Slam or Syndicate"
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
                saved_match = _saved_market_match(record, kind)
                if saved_match is None or not record.get("event_start_checked_at"):
                    saved_match = await asyncio.to_thread(_resolve_market_match, pick, kind)
                    record.update(saved_match)
                else:
                    record["match_status"] = "MATCHED"
                record["match_error"] = None
            except Exception as exc:
                record["match_error"] = f"{type(exc).__name__}: {exc}"
                record["last_error"] = record["match_error"]
                alternatives = []
                if kind == "spread":
                    try:
                        alternatives = await asyncio.to_thread(_find_spread_alternatives, pick)
                    except Exception as alt_exc:
                        record["alternate_error"] = f"{type(alt_exc).__name__}: {alt_exc}"
                if alternatives:
                    record["live_alternatives"] = alternatives
                    record["alternate_error"] = None
                    record["match_status"] = "ALTERNATE_AVAILABLE"
                    record["event_title"] = alternatives[0].get("event_title")
                    record["event_start_at"] = alternatives[0].get("event_start_at")
                    record["event_phase"] = alternatives[0].get("event_phase")
                    record["market_url"] = alternatives[0].get("market_url")
                    record["status"] = (
                        "MATCHED_LIVE_ALTERNATE"
                        if alternatives[0].get("event_phase") == "LIVE"
                        else "MATCHED_PREGAME_ALTERNATE"
                    )
                    record["reason"] = (
                        "exact original spread is unavailable; explicit current Polymarket alternatives are shown"
                    )
                    print(
                        "CFB_CAPPER_ALTERNATES "
                        + json.dumps(
                            {
                                "selection": record.get("selection"),
                                "source": record.get("source"),
                                "status": record.get("status"),
                                "event_title": record.get("event_title"),
                                "event_phase": record.get("event_phase"),
                                "alternatives": [
                                    {
                                        "line": alt.get("spread_line"),
                                        "best_ask": alt.get("best_ask"),
                                        "odds": alt.get("live_odds_american"),
                                        "relative": alt.get("relative_to_original"),
                                    }
                                    for alt in alternatives
                                ],
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                            default=str,
                        ),
                        flush=True,
                    )
                else:
                    record["status"] = "RETRYING"
                    record["match_status"] = "UNRESOLVED"
                record["updated_at"] = _now_iso()
                signals[fp] = record
                changed = True
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            if age > max_age_seconds:
                record["event_phase"] = _event_phase(record, now)
                if record["event_phase"] == "LIVE":
                    record["status"] = "MATCHED_LIVE"
                    record["reason"] = "game is live; matched Polymarket market remains open"
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
                elif record["event_phase"] == "CLOSED":
                    record["status"] = "EVENT_CLOSED"
                    record["reason"] = "matched Polymarket event/market is closed"
                else:
                    record["status"] = "MATCHED_PREGAME"
                    record["reason"] = (
                        f"outside {max_age_seconds}s auto-trade freshness window; "
                        "manual BUY remains available while market is open"
                    )
                    try:
                        record.update(await asyncio.to_thread(
                            _read_live_buy_quote,
                            str(record["asset_id"]),
                        ))
                        record["live_quote_error"] = None
                    except Exception as exc:
                        record["live_quote_error"] = f"{type(exc).__name__}: {exc}"
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
                    matched=record,
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
        counts: dict[str, dict[str, Any]] = {}
        for label in SOURCE_LABELS:
            rows = [r for r in signals.values() if r.get("source") == label]
            counts[label] = {
                "signals": len(rows),
                "preview_done": sum(1 for r in rows if r.get("status") == "PREVIEW_DONE"),
                "preview_failed": sum(1 for r in rows if r.get("status") == "PREVIEW_FAILED"),
                "preview_queued": sum(1 for r in rows if r.get("status") == "PREVIEW_QUEUED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ),
                "live": sum(
                    1 for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ),
                "closed": sum(1 for r in rows if r.get("status") == "EVENT_CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": _recent_all_items(rows),
                "queued_items": _recent_status_items(rows, "PREVIEW_QUEUED"),
                "done_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "previewed_items": _recent_status_items(rows, "PREVIEW_DONE"),
                "retrying_items": _recent_status_items(rows, "RETRYING"),
                "failed_items": _recent_status_items(rows, "PREVIEW_FAILED"),
                "pregame_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}
                ], limit=30),
                "live_items": _recent_all_items([
                    r for r in rows
                    if r.get("status") in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}
                ], limit=30),
                "closed_items": _recent_status_items(rows, "EVENT_CLOSED"),
                "unsupported_items": _recent_status_items(rows, "IGNORED_UNSUPPORTED"),
            }
        return {
            "enabled": enabled,
            "mode": "PREVIEW_ONLY",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "unit_usdc": str(unit_usdc),
            "sources": counts,
            "status": dict(_STATUS),
        }


    @app.post(
        "/api/cfb-cappers/manual-buy-alternate/{signal_id}/{alternative_id}",
        dependencies=[Depends(dashboard._auth)],
    )
    def cfb_capper_manual_buy_alternate(signal_id: str, alternative_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            raise HTTPException(status_code=409, detail="Original CFB pick details are unavailable")
        kind, _ = _classify_pick(pick)
        if kind != "spread":
            raise HTTPException(status_code=409, detail="Alternate live-line BUY is only for spread signals")

        try:
            alternatives = _find_spread_alternatives(pick, limit=8)
        except Exception as exc:
            raise HTTPException(status_code=409, detail=f"Could not refresh live spread alternatives: {exc}") from exc
        selected = next(
            (alt for alt in alternatives if str(alt.get("alternative_id") or "") == alternative_id),
            None,
        )
        if selected is None:
            raise HTTPException(
                status_code=409,
                detail="That live spread is no longer available; refresh the dashboard and choose a current line",
            )
        if _event_phase(selected) == "CLOSED":
            raise HTTPException(status_code=409, detail="Selected Polymarket spread market is closed")

        execution_pick = dict(pick)
        execution_pick["spread_lines"] = [selected["spread_line"]]
        execution_pick["selection"] = (
            f"{pick.get('team_hint') or pick.get('selection')} {selected['spread_line']}"
        )
        try:
            result = _prepare_manual_buy(
                execution_pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=selected,
                strategy_pick_id=signal_id,
                strategy_selection=str(record.get("selection") or pick.get("selection") or ""),
                strategy_alternate_line=str(selected["spread_line"]),
            )
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["manual_buy_alternate_line"] = selected["spread_line"]
        record["manual_buy_alternative_id"] = alternative_id
        record["live_alternatives"] = alternatives
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "signal_id": signal_id,
            "original_selection": record.get("selection"),
            "selected_live_line": selected["spread_line"],
            "request_id": result["request_id"],
            "trade_id": result["trade_id"],
            "best_ask": result.get("best_ask"),
            "live_odds_american": result.get("live_odds_american"),
        }


    @app.post("/api/cfb-cappers/manual-buy/{signal_id}", dependencies=[Depends(dashboard._auth)])
    def cfb_capper_manual_buy(signal_id: str) -> dict[str, Any]:
        signals = _load_signals()
        record = signals.get(signal_id)
        if not record:
            raise HTTPException(status_code=404, detail="Unknown CFB signal")
        if record.get("source") not in SOURCE_LABELS:
            raise HTTPException(status_code=400, detail="Only Slam/Syndicate CFB signals can be bought")
        if record.get("status") not in {
            "PREVIEW_QUEUED", "PREVIEW_DONE", "PREVIEW_FAILED",
            "MATCHED_PREGAME", "MATCHED_LIVE", "RETRYING"
        }:
            raise HTTPException(
                status_code=409,
                detail=f"CFB signal is {record.get('status')}; BUY LIVE requires a matched actionable signal",
            )
        saved_match = _saved_market_match(record)
        if saved_match is None:
            raise HTTPException(
                status_code=409,
                detail="CFB signal has not been safely matched to a Polymarket event yet",
            )
        phase = _event_phase(record)
        if phase == "CLOSED":
            raise HTTPException(
                status_code=409,
                detail="CFB market is closed; BUY LIVE is no longer available",
            )

        pick = record.get("pick") if isinstance(record.get("pick"), dict) else None
        if pick is None:
            feed = core._load(last_feed_file)
            for candidate in (feed.get("picks") or []) if isinstance(feed, dict) else []:
                if isinstance(candidate, dict) and _fingerprint(candidate) == signal_id:
                    pick = candidate
                    break
        if pick is None:
            raise HTTPException(
                status_code=409,
                detail="Original CFB pick details are no longer available to submit the matched market",
            )

        try:
            result = _prepare_manual_buy(
                pick,
                core=core,
                remote=remote,
                unit_usdc=unit_usdc,
                matched=saved_match,
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        record["manual_buy_request_id"] = result["request_id"]
        record["manual_buy_trade_id"] = result["trade_id"]
        record["manual_buy_status"] = "PENDING"
        record["manual_buy_error"] = None
        record["manual_buy_at"] = _now_iso()
        record["updated_at"] = _now_iso()
        signals[signal_id] = record
        _save_signals(signals)
        return {
            "ok": True,
            "manual": True,
            "auto": False,
            **result,
        }

    dashboard.DASHBOARD_HTML = _inject_dashboard_panel(dashboard.DASHBOARD_HTML)
