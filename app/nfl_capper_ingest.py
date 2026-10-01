from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_UP
from typing import Any

import httpx
from fastapi import Depends, HTTPException
from polymarket import PublicClient

from app import capper_control
from app import dashboard_live_control_v4 as live_control

TARGET_SOURCES = {
    "SLAM - All Access": "Slam - NFL",
    "The Syndicate": "Syndicate - NFL",
}
SOURCE_LABELS = ("Slam - NFL", "Syndicate - NFL")
SPORT_CONTROL_LABEL = "NFL Auto-Trading"
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


def _target_profit_for_pick(
    pick: dict[str, Any],
    unit_usdc: Decimal = Decimal("10"),
) -> Decimal:
    """Dollar profit target represented by the posted unit count."""
    amount = Decimal(str(unit_usdc))
    if amount <= 0:
        raise ValueError("unit size must be positive")
    return (_units_for_pick(pick) * amount).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def _stake_to_win_at_price(target_profit_usdc: Any, price: Any) -> Decimal:
    """Cash budget required at a binary-market price to win the target profit."""
    target = Decimal(str(target_profit_usdc))
    p = Decimal(str(price))
    if target <= 0:
        raise ValueError("target profit must be positive")
    if p <= 0 or p >= 1:
        raise ValueError(f"price must be between 0 and 1, got {p}")
    # shares = stake / p; winning profit = shares - stake.
    # stake = target * p / (1 - p). Round up so the target is never undersized.
    return (target * p / (Decimal("1") - p)).quantize(
        Decimal("0.01"),
        rounding=ROUND_UP,
    )


def _stake_for_pick(
    pick: dict[str, Any],
    unit_usdc: Decimal = Decimal("10"),
) -> Decimal:
    """Posted-odds stake required to win the requested unit profit target."""
    target = _target_profit_for_pick(pick, unit_usdc)
    decimal_odds = _decimal_odds_for_pick(pick)
    profit_multiple = decimal_odds - Decimal("1")
    if profit_multiple <= 0:
        raise ValueError(f"invalid decimal odds {decimal_odds}")
    return (target / profit_multiple).quantize(
        Decimal("0.01"),
        rounding=ROUND_UP,
    )


def _capper_unit_settings_path(core: Any):
    return core.DATA_DIR / "capper_unit_sizes.json"


def _load_capper_unit_settings(core: Any) -> dict[str, Any]:
    data = core._load(_capper_unit_settings_path(core))
    return data if isinstance(data, dict) else {}


def _portfolio_value_usdc() -> Decimal | None:
    """Current total wallet value = available USDC + marked Polymarket positions."""
    try:
        state = live_control.remote._state()
        cash_raw = state.get("usdc_balance")
        positions_raw = state.get("portfolio_value")
        # Percentage sizing needs the full wallet value. If either component is
        # unavailable, fail closed rather than silently undercounting the base.
        if cash_raw is None or cash_raw == "" or positions_raw is None or positions_raw == "":
            return None
        cash = Decimal(str(cash_raw))
        positions = Decimal(str(positions_raw))
        return (cash + positions).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception:
        return None


def _capper_unit_config(
    core: Any,
    label: str,
    default: Decimal,
) -> dict[str, Any]:
    """Return fixed/portfolio-percent unit configuration and the live effective 1u."""
    settings = _load_capper_unit_settings(core)
    try:
        fixed = Decimal(str(settings.get(label, default)))
    except Exception:
        fixed = Decimal(str(default))
    if fixed <= 0:
        fixed = Decimal(str(default))
    fixed = fixed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    mode = str(settings.get(f"{label}::mode", "fixed") or "fixed").strip().lower()
    if mode not in {"fixed", "portfolio_pct"}:
        mode = "fixed"

    try:
        portfolio_pct = Decimal(
            str(settings.get(f"{label}::portfolio_pct", "10"))
        )
    except Exception:
        portfolio_pct = Decimal("10")
    if portfolio_pct <= 0 or portfolio_pct > 100:
        portfolio_pct = Decimal("10")
    portfolio_pct = portfolio_pct.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )

    portfolio = _portfolio_value_usdc()
    effective = fixed
    error = None
    if mode == "portfolio_pct":
        if portfolio is None:
            error = "Portfolio value is unavailable; percentage unit sizing is waiting for a wallet heartbeat."
        elif portfolio <= 0:
            error = "Portfolio value must be greater than $0 for percentage unit sizing."
        else:
            effective = (
                portfolio * portfolio_pct / Decimal("100")
            ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if effective <= 0:
                error = "Calculated percentage unit size is not positive."

    return {
        "mode": mode,
        "unit_usdc": str(effective),
        "fixed_unit_usdc": str(fixed),
        "portfolio_pct": str(portfolio_pct),
        "portfolio_value_usdc": str(portfolio) if portfolio is not None else None,
        "error": error,
    }


def _capper_unit_usdc(core: Any, label: str, default: Decimal) -> Decimal:
    """Resolve the current 1u profit target; fail closed if dynamic sizing has no portfolio."""
    config = _capper_unit_config(core, label, default)
    if config["mode"] == "portfolio_pct" and config.get("error"):
        raise RuntimeError(str(config["error"]))
    return Decimal(str(config["unit_usdc"]))


def _set_capper_unit_usdc(core: Any, label: str, value: Any) -> Decimal:
    """Set a fixed dollar 1u profit target and disable portfolio-percentage mode."""
    amount = Decimal(str(value))
    if amount <= 0 or amount > Decimal("10000"):
        raise ValueError("unit size must be greater than 0 and no more than $10,000")
    amount = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    settings = _load_capper_unit_settings(core)
    settings[label] = str(amount)
    settings[f"{label}::mode"] = "fixed"
    settings.setdefault(f"{label}::portfolio_pct", "10.00")
    core._save(_capper_unit_settings_path(core), settings)
    return amount


def _set_capper_portfolio_pct(core: Any, label: str, value: Any) -> Decimal:
    """Enable live portfolio-percentage unit sizing for one capper."""
    pct = Decimal(str(value if value is not None else "10"))
    if pct <= 0 or pct > 100:
        raise ValueError("portfolio percentage must be greater than 0 and no more than 100")
    pct = pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    settings = _load_capper_unit_settings(core)
    settings[f"{label}::portfolio_pct"] = str(pct)
    settings[f"{label}::mode"] = "portfolio_pct"
    core._save(_capper_unit_settings_path(core), settings)
    return pct


def _ensure_signal_call_unit_snapshot(
    record: dict[str, Any],
    pick: dict[str, Any],
    *,
    unit_config: dict[str, Any],
    effective_unit_usdc: Decimal,
) -> bool:
    """Freeze the unit basis that applied when a Telegram call first entered the bot."""
    changed = False

    def _positive_decimal(value: Any) -> Decimal | None:
        try:
            amount = Decimal(str(value))
            return amount if amount > 0 else None
        except Exception:
            return None

    call_unit = _positive_decimal(record.get("unit_usdc_at_call"))
    if call_unit is None:
        # Legacy rows may already have the then-current unit persisted. Prefer it
        # over today's capper setting so a later unit change cannot rewrite history.
        call_unit = _positive_decimal(record.get("unit_usdc")) or Decimal(str(effective_unit_usdc))
        record["unit_usdc_at_call"] = str(
            call_unit.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        changed = True

    call_target = _positive_decimal(record.get("target_profit_usdc_at_call"))
    if call_target is None:
        legacy_target = _positive_decimal(record.get("target_profit_usdc"))
        call_target = legacy_target or _target_profit_for_pick(pick, call_unit)
        record["target_profit_usdc_at_call"] = str(
            call_target.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        )
        changed = True

    snapshot_fields = {
        "unit_mode_at_call": record.get("unit_mode") or unit_config.get("mode") or "fixed",
        "portfolio_pct_at_call": (
            record.get("portfolio_pct")
            if record.get("portfolio_pct") is not None
            else unit_config.get("portfolio_pct")
        ),
        "portfolio_value_usdc_at_call": (
            record.get("portfolio_value_usdc")
            if record.get("portfolio_value_usdc") is not None
            else unit_config.get("portfolio_value_usdc")
        ),
        "unit_snapshot_at": record.get("first_seen_at") or _now_iso(),
        "unit_snapshot_source": "signal_first_seen",
    }
    for key, value in snapshot_fields.items():
        if key not in record:
            record[key] = value
            changed = True
    return changed


def _decimal_odds_for_pick(pick: dict[str, Any]) -> Decimal:
    """Return posted decimal odds, falling back to the audit default of -115."""
    try:
        decimal_odds = Decimal(str(pick.get("decimal_odds")))
        if decimal_odds > 1:
            return decimal_odds
    except Exception:
        pass

    try:
        american = Decimal(str(pick.get("american_odds")))
        if american > 0:
            return Decimal("1") + (american / Decimal("100"))
        if american < 0:
            return Decimal("1") + (Decimal("100") / abs(american))
    except Exception:
        pass

    return Decimal("1") + (Decimal("100") / Decimal("115"))


def _missed_signal_stats(
    signals: dict[str, Any],
    executions: dict[str, Any],
    *,
    labels: tuple[str, ...] = SOURCE_LABELS,
    sport: str = "NFL",
    unit_usdc: Decimal = Decimal("10"),
    unit_usdc_by_label: dict[str, Decimal] | None = None,
) -> dict[str, dict[str, Any]]:
    """Hypothetical P/L for graded Telegram calls that never became real trades."""
    traded_signal_ids: set[str] = set()
    for rec in executions.values():
        if not isinstance(rec, dict) or rec.get("parent_trade_id") or bool(rec.get("paper")):
            continue
        record_sport = str(rec.get("strategy_sport") or "").upper()
        if sport and record_sport and record_sport != str(sport).upper():
            continue
        signal_id = str(rec.get("strategy_pick_id") or "")
        if signal_id:
            traded_signal_ids.add(signal_id)

    out: dict[str, dict[str, Any]] = {}
    for label in labels:
        graded = wins = losses = pushes = 0
        missed_pnl = Decimal("0")
        # Historical missed P/L must not move when today's unit setting changes.
        # unit_usdc is only a legacy fallback for rows that pre-date per-signal snapshots.
        historical_default_unit = Decimal(str(unit_usdc))
        for record in signals.values():
            if not isinstance(record, dict) or record.get("source") != label:
                continue
            signal_id = str(record.get("id") or "")
            if signal_id and signal_id in traded_signal_ids:
                continue

            result = str(record.get("pick_result") or "").upper()
            if result not in {"WIN", "LOSS", "PUSH"}:
                continue

            pick = record.get("pick") if isinstance(record.get("pick"), dict) else {}
            record_unit = None
            for key in ("unit_usdc_at_call", "unit_usdc"):
                try:
                    candidate = Decimal(str(record.get(key)))
                    if candidate > 0:
                        record_unit = candidate
                        break
                except Exception:
                    pass
            if record_unit is None:
                record_unit = historical_default_unit

            target_profit = None
            for key in ("target_profit_usdc_at_call", "target_profit_usdc"):
                try:
                    candidate = Decimal(str(record.get(key)))
                    if candidate > 0:
                        target_profit = candidate
                        break
                except Exception:
                    pass
            if target_profit is None:
                target_profit = _target_profit_for_pick(pick, record_unit)

            stake = None
            try:
                candidate = Decimal(str(record.get("stake_usdc_at_call")))
                if candidate > 0:
                    stake = candidate
            except Exception:
                pass
            if stake is None and str(record.get("sizing_mode") or "").upper() == "TO_WIN":
                try:
                    candidate = Decimal(str(record.get("stake_usdc")))
                    if candidate > 0:
                        stake = candidate
                except Exception:
                    pass
            if stake is None:
                stake = _stake_for_pick(pick, record_unit)

            graded += 1
            if result == "WIN":
                wins += 1
                missed_pnl += target_profit
            elif result == "LOSS":
                losses += 1
                missed_pnl -= stake
            else:
                pushes += 1

        out[label] = {
            "missed_graded": graded,
            "missed_wins": wins,
            "missed_losses": losses,
            "missed_pushes": pushes,
            "missed_pnl_usdc": str(missed_pnl.quantize(Decimal("0.01"))),
        }
    return out


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
    if "team_total" in types:
        primary.add("total")
    if len(primary) != 1:
        return None, "pick does not resolve to exactly one supported game market"

    kind = next(iter(primary))
    teams = [str(x).upper() for x in (pick.get("teams") or []) if str(x).upper() in NFL_TEAMS]
    explicit_matchup = bool(
        re.search(r"(?:/|\bvs\.?\b|\bv\.?\b|\s@\s)", selection, re.I)
    )
    if explicit_matchup and len(set(teams)) != 2:
        return None, "explicit matchup does not resolve to exactly two NFL teams"
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


def _espn_nfl_competitor_matches(abbr: str, competitor: dict[str, Any]) -> bool:
    team = competitor.get("team") or {}
    espn_abbr = str(team.get("abbreviation") or "").upper()
    if espn_abbr == str(abbr).upper():
        return True
    text = " ".join(
        str(team.get(key) or "")
        for key in ("displayName", "shortDisplayName", "name", "location")
    )
    return _contains_alias(text, abbr)


def _nfl_scoreboard_result_for_pick(
    pick: dict[str, Any],
    *,
    cache: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    """Grade one supported NFL game call from a completed ESPN final score."""
    kind, _ = _classify_pick(pick)
    posted = _parse_iso(pick.get("posted_at"))
    teams = [str(x).upper() for x in (pick.get("teams") or []) if str(x).upper() in NFL_TEAMS]
    if kind is None or posted is None or not teams:
        return None

    event_cache = cache if cache is not None else {}
    matched: dict[str, dict[str, Any]] = {}
    for offset in (-1, 0, 1, 2, 3, 4, 5, 6, 7):
        game_day = (posted + timedelta(days=offset)).strftime("%Y%m%d")
        events = event_cache.get(game_day)
        if events is None:
            try:
                response = httpx.get(
                    "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
                    params={"dates": game_day, "limit": 100},
                    timeout=12,
                )
                response.raise_for_status()
                events = list((response.json() or {}).get("events") or [])
            except Exception:
                events = []
            event_cache[game_day] = events

        for event in events:
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competition = competitions[0] or {}
            status = ((competition.get("status") or {}).get("type") or {})
            if not bool(status.get("completed")):
                continue
            competitors = competition.get("competitors") or []
            if len(competitors) != 2:
                continue
            if not all(any(_espn_nfl_competitor_matches(team, comp) for comp in competitors) for team in teams):
                continue
            event_id = str(event.get("id") or "")
            if event_id:
                matched[event_id] = event

    if len(matched) != 1:
        return None

    event = next(iter(matched.values()))
    competition = (event.get("competitions") or [{}])[0] or {}
    competitors = competition.get("competitors") or []
    try:
        scores = [Decimal(str(comp.get("score"))) for comp in competitors]
    except Exception:
        return None

    labels = []
    for comp in competitors:
        team = comp.get("team") or {}
        labels.append(str(team.get("shortDisplayName") or team.get("displayName") or team.get("name") or "").strip())
    final_score = f"{labels[0]} {scores[0]:f} - {labels[1]} {scores[1]:f}"

    result: str | None = None
    selected_score: Decimal | None = None
    opponent_score: Decimal | None = None
    if kind in {"moneyline", "spread"}:
        selected = teams[0]
        selected_index = next(
            (idx for idx, comp in enumerate(competitors) if _espn_nfl_competitor_matches(selected, comp)),
            None,
        )
        if selected_index is None:
            return None
        other_index = 1 - selected_index
        selected_score = scores[selected_index]
        opponent_score = scores[other_index]
        if kind == "moneyline":
            result = "WIN" if selected_score > opponent_score else "LOSS" if selected_score < opponent_score else "PUSH"
        else:
            try:
                line = Decimal(str((pick.get("spread_lines") or [])[0]))
            except Exception:
                return None
            adjusted = selected_score + line
            result = "WIN" if adjusted > opponent_score else "LOSS" if adjusted < opponent_score else "PUSH"
    elif kind == "total":
        try:
            line = Decimal(str(pick.get("total_line")))
        except Exception:
            return None
        total = scores[0] + scores[1]
        side = str(pick.get("total_side") or "").upper()
        if total == line:
            result = "PUSH"
        elif side == "OVER":
            result = "WIN" if total > line else "LOSS"
        elif side == "UNDER":
            result = "WIN" if total < line else "LOSS"

    if result is None:
        return None
    return {
        "pick_result": result,
        "result_source": "espn_final_score",
        "result_event_id": str(event.get("id") or ""),
        "result_event_title": str(event.get("name") or event.get("shortName") or ""),
        "final_score": final_score,
        "result_checked_at": _now_iso(),
        "selected_final_score": str(selected_score) if selected_score is not None else None,
        "opponent_final_score": str(opponent_score) if opponent_score is not None else None,
    }


def _refresh_nfl_signal_results(
    signals: dict[str, Any],
    *,
    now: datetime | None = None,
    min_age_seconds: int = 4 * 60 * 60,
    retry_seconds: int = 30 * 60,
) -> bool:
    current = now or datetime.now(timezone.utc)
    changed = False
    cache: dict[str, list[dict[str, Any]]] = {}
    for record in signals.values():
        if not isinstance(record, dict):
            continue
        if str(record.get("pick_result") or "").upper() in {"WIN", "LOSS", "PUSH"}:
            continue
        pick = record.get("pick")
        if not isinstance(pick, dict):
            continue
        kind, _ = _classify_pick(pick)
        if kind is None:
            continue
        posted = _parse_iso(pick.get("posted_at"))
        if posted is None or (current - posted).total_seconds() < min_age_seconds:
            continue
        last_lookup = _parse_iso(record.get("result_lookup_at"))
        if last_lookup is not None and (current - last_lookup).total_seconds() < retry_seconds:
            continue

        record["result_lookup_at"] = current.isoformat()
        changed = True
        resolved = _nfl_scoreboard_result_for_pick(pick, cache=cache)
        if not resolved:
            continue
        for key, value in resolved.items():
            if record.get(key) != value:
                record[key] = value
                changed = True
    return changed


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


def _full_game_market_type_matches(actual: str, kind: str) -> bool:
    normalized = _norm(actual)
    allowed = {
        "moneyline": {"moneyline"},
        "spread": {"spread", "spreads"},
        "total": {"total", "totals"},
    }
    return normalized in allowed.get(kind, set())


def _is_team_total_pick(pick: dict[str, Any]) -> bool:
    """Keep team totals separate from game totals, even when the bridge labels both as total."""
    types = {str(x).lower() for x in (pick.get("bet_types") or [])}
    if "team_total" in types:
        return True

    selection = str(pick.get("selection") or "")
    if re.search(r"\bteam\s+total\b", selection, re.I):
        return True

    teams = [
        str(x).upper()
        for x in (pick.get("teams") or [])
        if str(x).upper() in NFL_TEAMS
    ]
    if len(set(teams)) != 1:
        return False
    if re.search(r"(?:/|\bvs\.?\b|\bv\.?\b|\s@\s)", selection, re.I):
        return False

    # Compatibility guard for bridge posts such as "Steelers U21.5" that omit
    # an explicit team_total tag. NFL full-game totals do not normally live in
    # this range; the team name + one-team context prevents a bare U/O line
    # from being silently reclassified.
    if not _contains_alias(selection, teams[0]):
        return False
    try:
        line = Decimal(str(pick.get("total_line")))
    except Exception:
        return False
    return Decimal("0") < line <= Decimal("34.5")


def _total_market_scope_matches(market: Any, pick: dict[str, Any]) -> bool:
    """Require team-total signals to hit only full-game team-total markets."""
    actual = _market_type(market)
    text = _market_text(market)
    is_team_market = bool(re.search(r"\bteam\s+total\b", text, re.I))
    period_market = bool(
        re.search(
            r"\b(?:1h|2h|first half|second half|1st half|2nd half|quarter|[1-4](?:st|nd|rd|th)? q)\b",
            text,
            re.I,
        )
    )

    if _is_team_total_pick(pick):
        if not _type_matches(actual, "total") or not is_team_market or period_market:
            return False
        teams = [
            str(x).upper()
            for x in (pick.get("teams") or [])
            if str(x).upper() in NFL_TEAMS
        ]
        return bool(teams and _contains_alias(text, teams[0]))

    return _full_game_market_type_matches(actual, "total") and not is_team_market


def _event_date(event: Any) -> date | None:
    schedule = getattr(event, "schedule", None)
    for source in (schedule, event):
        if source is None:
            continue
        for attr in (
            "event_date", "eventDate",
            "start_time", "startTime",
            "start_date", "startDate",
            "end_date", "endDate",
        ):
            value = getattr(source, attr, None)
            if isinstance(value, date) and not isinstance(value, datetime):
                return value
            parsed = _parse_iso(value)
            if parsed is not None:
                return parsed.date()

    text = " ".join([
        str(getattr(event, "slug", "") or ""),
        str(getattr(event, "title", "") or ""),
    ])
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
    """Choose one nearby NFL game for a one-team pick without future-week guessing."""
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
        if -1 <= delta_days <= 4:
            candidates[key] = delta_days

    if not candidates:
        return []

    def priority(delta_days: int) -> tuple[int, int]:
        return (0 if delta_days >= 0 else 1, abs(delta_days))

    best_priority = min(priority(delta) for delta in candidates.values())
    best_keys = {
        key for key, delta in candidates.items()
        if priority(delta) == best_priority
    }
    if len(best_keys) != 1:
        return [row for row in ranked if _event_key(row[1]) in best_keys]

    best_key = next(iter(best_keys))
    return [row for row in ranked if _event_key(row[1]) == best_key]


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


def _spread_outcome_any_line(
    market: Any,
    pick: dict[str, Any],
) -> tuple[str, Any, Decimal] | None:
    """Resolve the selected NFL team and its effective structured spread line."""
    outcomes = [(label, obj) for label, obj in _outcomes(market) if obj is not None]
    if len(outcomes) != 2:
        return None
    teams = [str(x).upper() for x in (pick.get("teams") or []) if str(x).upper() in NFL_TEAMS]
    if not teams:
        return None
    selected_team = teams[0]

    selected_indexes = [
        i for i, (label, _) in enumerate(outcomes)
        if _contains_alias(label, selected_team)
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

    subject_indexes = []
    subject_norm = _norm(subject)
    for i, (label, _) in enumerate(outcomes):
        label_norm = _norm(label)
        if subject_norm and (
            subject_norm == label_norm
            or f" {subject_norm} " in f" {label_norm} "
            or f" {label_norm} " in f" {subject_norm} "
        ):
            subject_indexes.append(i)
    if len(subject_indexes) != 1:
        return None

    effective_line = base_line if selected_index == subject_indexes[0] else -base_line
    label, obj = outcomes[selected_index]
    return label, obj, effective_line


def _format_signed_spread_line(value: Decimal) -> str:
    number = value.normalize()
    body = format(abs(number), "f")
    if "." in body:
        body = body.rstrip("0").rstrip(".")
    return ("+" if number >= 0 else "-") + body


def _find_better_spread_fallback(
    pick: dict[str, Any],
    *,
    max_distance: Decimal,
) -> tuple[Any, Any, str, Any, Decimal]:
    """Resolve the nearest open same-game spread that is strictly better for the picked team."""
    teams = [str(x).upper() for x in (pick.get("teams") or []) if str(x).upper() in NFL_TEAMS]
    if not teams:
        raise ValueError("NFL spread fallback needs one recognized team")
    try:
        requested = Decimal(str(list(pick.get("spread_lines") or [None])[0]))
    except Exception as exc:
        raise ValueError("NFL spread fallback needs one requested spread line") from exc
    if max_distance <= 0:
        raise ValueError("NFL safer-spread fallback distance is disabled")

    search_queries: list[str] = []
    for candidate in (_team_name(teams[0]), *_team_aliases(teams[0])):
        text = str(candidate or "").strip()
        if text and text.casefold() not in {q.casefold() for q in search_queries}:
            search_queries.append(text)

    rows: list[tuple[int, Any, Any, str, Any, Decimal]] = []
    with PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in search_queries:
            result = client.list_events(
                title_search=query,
                closed=False,
                page_size=30,
            ).first_page()
            for event in result.items:
                key = _event_key(event)
                if key:
                    events_by_key[key] = event

        for event in events_by_key.values():
            event_slug = _norm(getattr(event, "slug", ""))
            if event_slug and not event_slug.startswith("nfl-"):
                continue
            etext = _norm(
                " ".join(
                    [
                        str(getattr(event, "title", "") or ""),
                        str(getattr(event, "slug", "") or ""),
                    ]
                )
            )
            if not all(_contains_alias(etext, team) for team in teams[:2]):
                continue
            for market in getattr(event, "markets", ()) or ():
                if not _full_game_market_type_matches(_market_type(market), "spread"):
                    continue
                if not getattr(getattr(market, "state", None), "accepting_orders", False):
                    continue
                selected = _spread_outcome_any_line(market, pick)
                if selected is None:
                    continue
                label, obj, line = selected
                if line == requested:
                    continue
                distance = abs(line - requested)
                rows.append((int(distance * 1000), event, market, label, obj, line))

    if len(teams) == 1:
        rows = _narrow_one_team_events_by_posted_date(rows, pick)  # type: ignore[arg-type]

    if not rows:
        raise ValueError("No open same-game NFL spread alternatives were found")

    event_keys = {_event_key(row[1]) for row in rows}
    event_keys.discard("")
    if len(event_keys) != 1:
        raise ValueError("NFL spread alternatives matched multiple nearby events; unattended trade blocked")

    available = sorted({row[5] for row in rows})
    safer = [
        row
        for row in rows
        if row[5] > requested and (row[5] - requested) <= max_distance
    ]
    if not safer:
        available_text = ", ".join(_format_signed_spread_line(line) for line in available)
        raise ValueError(
            "No safer same-game NFL spread is available within "
            + _format_signed_spread_line(max_distance).lstrip("+")
            + " points; open alternatives: "
            + available_text
        )

    safer.sort(key=lambda row: (row[0], row[5]))
    best = safer[0]
    best_line = best[5]
    same_line = [row for row in safer if row[5] == best_line]
    market_ids = {
        str(getattr(row[2], "id", "") or getattr(row[2], "slug", ""))
        for row in same_line
    }
    market_ids.discard("")
    if len(market_ids) > 1:
        raise ValueError("Multiple Polymarket markets expose the same safer NFL spread; unattended trade blocked")
    return best[1], best[2], best[3], best[4], best_line


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
                if kind == "total":
                    if not _total_market_scope_matches(market, pick):
                        continue
                elif not _full_game_market_type_matches(actual, kind):
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

    if len(teams) == 1:
        ranked = _narrow_one_team_events_by_posted_date(ranked, pick)
        if not ranked:
            raise ValueError(
                "No unique nearby NFL event matched the one-team pick; unattended trade blocked"
            )

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



def _find_better_total_fallback(
    pick: dict[str, Any],
    *,
    max_distance: Decimal = Decimal("2.0"),
) -> tuple[Any, Any, str, Any, Decimal]:
    """Use only a strictly better nearby total when the exact NFL total is unavailable."""
    teams = [str(x).upper() for x in (pick.get("teams") or [])]
    side = str(pick.get("total_side") or "").upper()
    try:
        requested = Decimal(str(pick.get("total_line")))
    except Exception as exc:
        raise ValueError("NFL total fallback requires a numeric requested line") from exc
    if side not in {"UNDER", "OVER"}:
        raise ValueError("NFL total fallback requires OVER or UNDER")

    ranked: list[tuple[int, Decimal, Any, Any, str, Any]] = []
    search_queries: list[str] = []
    for candidate in (_team_name(teams[0]), *_team_aliases(teams[0])):
        text = str(candidate or "").strip()
        if text and text.casefold() not in {q.casefold() for q in search_queries}:
            search_queries.append(text)

    with PublicClient() as client:
        events_by_key: dict[str, Any] = {}
        for query in search_queries:
            result = client.list_events(title_search=query, closed=False, page_size=30).first_page()
            for event in result.items:
                key = str(getattr(event, "id", "") or getattr(event, "slug", "") or getattr(event, "title", ""))
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
                if not _total_market_scope_matches(market, pick):
                    continue
                if not getattr(getattr(market, "state", None), "accepting_orders", False):
                    continue
                sports = getattr(market, "sports", None)
                try:
                    candidate_line = Decimal(str(getattr(sports, "line", None)))
                except Exception:
                    continue
                distance = abs(candidate_line - requested)
                if distance <= 0 or distance > max_distance:
                    continue
                # A higher total is better for UNDER; a lower total is better for OVER.
                if side == "UNDER" and candidate_line <= requested:
                    continue
                if side == "OVER" and candidate_line >= requested:
                    continue
                candidate_pick = dict(pick)
                candidate_pick["total_line"] = str(candidate_line)
                selected = _select_outcome(market, candidate_pick, "total")
                if selected is None:
                    continue
                label, obj = selected
                score = 20 - int(distance * 10)
                ranked.append((score, distance, event, market, label, obj))

    if not ranked:
        raise ValueError(
            f"No safer same-game NFL total within {max_distance} points of {side} {requested}"
        )

    if len(teams) == 1:
        narrowed = _narrow_one_team_events_by_posted_date(
            [(row[0], row[2], row[3], row[4], row[5]) for row in ranked],
            pick,
        )
        allowed = {_event_key(row[1]) for row in narrowed}
        ranked = [row for row in ranked if _event_key(row[2]) in allowed]
        if not ranked:
            raise ValueError("No unique nearby NFL event matched the one-team total fallback")

    event_keys = {_event_key(row[2]) for row in ranked}
    event_keys.discard("")
    if len(event_keys) != 1:
        raise ValueError("NFL total fallback did not resolve to exactly one event")

    ranked.sort(key=lambda row: (row[1], -row[0]))
    best = ranked[0]
    return best[2], best[3], best[4], best[5], Decimal(str(getattr(getattr(best[3], "sports", None), "line")))


def _matched_event_phase(event: Any, market: Any, now: datetime | None = None) -> str:
    """Fail closed when deciding whether an outage-recovered NFL pick is still pregame."""
    current = now or datetime.now(timezone.utc)
    state = getattr(event, "state", None)
    schedule = getattr(event, "schedule", None)
    event_sports = getattr(event, "sports", None)
    market_sports = getattr(market, "sports", None)
    market_state = getattr(market, "state", None)

    status = _norm(
        getattr(event_sports, "game_status", "")
        or getattr(market_sports, "game_status", "")
    )
    if bool(getattr(state, "closed", False)) or bool(getattr(state, "ended", False)):
        return "CLOSED"
    if getattr(market_state, "accepting_orders", None) is False:
        return "CLOSED"
    if status in {"final", "finished", "ended", "complete", "completed", "closed", "post", "postgame"} or status.startswith("final"):
        return "CLOSED"
    if bool(getattr(state, "live", False)) or status in {"live", "in progress", "inprogress", "halftime", "half time"}:
        return "LIVE"

    for source in (market_sports, schedule, event):
        if source is None:
            continue
        for attr in (
            "event_start_time", "eventStartTime",
            "game_start_time", "gameStartTime",
            "start_time", "startTime",
            "scheduled_at", "scheduledAt",
            "start_date", "startDate",
        ):
            value = getattr(source, attr, None)
            if value is None:
                continue
            text = str(value).strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                continue
            parsed = _parse_iso(value)
            if parsed is not None:
                return "LIVE" if current >= parsed else "PREGAME"

    # Missing kickoff metadata is not enough to authorize a delayed unattended BUY.
    return "UNKNOWN"


def _is_recovery_age(age_seconds: float, normal_max: int, recovery_max: int) -> bool:
    return age_seconds > normal_max and age_seconds <= recovery_max


def _queue_result_is_no_fill(queued: dict[str, Any]) -> bool:
    if str(queued.get("action") or "").upper() != "BUY":
        return False
    result = queued.get("result") or {}
    status = str(result.get("status") or "").upper()
    error = str(queued.get("error") or "")
    try:
        filled = Decimal(str(result.get("filled_shares") or "0"))
    except Exception:
        filled = Decimal("0")
    return bool(
        filled <= 0
        and (
            "BUY_UNFILLED_RETRYABLE" in error
            or "UNFILLED" in status
            or result.get("ok") is False
        )
    )


def _pending_auto_budget(remote: Any) -> Decimal:
    total = Decimal("0")
    try:
        queue = remote._queue_load()
    except Exception:
        return total
    for rec in queue.values():
        if rec.get("action") != "BUY" or rec.get("status") not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
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
    recovery: bool = False,
    better_spread_fallback_enabled: bool = False,
    max_alt_spread_points: Decimal = Decimal("1.0"),
    max_alt_total_points: Decimal = Decimal("2.0"),
) -> dict[str, Any]:
    kind, reason = _classify_pick(pick)
    if kind is None:
        return {"status": "IGNORED_UNSUPPORTED", "reason": reason}

    if not core.auto_trading_enabled():
        raise RuntimeError("AUTO_TRADING is disabled")

    units = _units_for_pick(pick)
    target_profit = _target_profit_for_pick(pick, unit_usdc)

    requested_spread_line: str | None = None
    executed_spread_line: str | None = None
    alternate_spread_fallback = False
    requested_total_line: str | None = None
    executed_total_line: str | None = None
    alternate_total_fallback = False
    try:
        event, market, outcome_label, outcome_obj = _find_market(pick, kind)
    except ValueError as exact_exc:
        if kind == "spread" and better_spread_fallback_enabled:
            try:
                event, market, outcome_label, outcome_obj, fallback_line = _find_better_spread_fallback(
                    pick,
                    max_distance=max_alt_spread_points,
                )
            except Exception as fallback_exc:
                raise ValueError(f"{exact_exc}; safer spread fallback failed: {fallback_exc}") from fallback_exc
            requested = Decimal(str(list(pick.get("spread_lines") or [None])[0]))
            requested_spread_line = _format_signed_spread_line(requested)
            executed_spread_line = _format_signed_spread_line(fallback_line)
            alternate_spread_fallback = True
        elif kind == "total":
            try:
                event, market, outcome_label, outcome_obj, fallback_line = _find_better_total_fallback(
                    pick,
                    max_distance=max_alt_total_points,
                )
            except Exception as fallback_exc:
                raise ValueError(f"{exact_exc}; safer total fallback failed: {fallback_exc}") from fallback_exc
            requested_total_line = str(Decimal(str(pick.get("total_line"))).normalize())
            executed_total_line = str(fallback_line.normalize())
            alternate_total_fallback = True
        else:
            raise

    if kind == "spread" and requested_spread_line is None:
        requested = Decimal(str(list(pick.get("spread_lines") or [None])[0]))
        requested_spread_line = _format_signed_spread_line(requested)
        executed_spread_line = requested_spread_line

    if kind == "total" and requested_total_line is None:
        requested = Decimal(str(pick.get("total_line")))
        requested_total_line = str(requested.normalize())
        executed_total_line = requested_total_line

    recovery_phase = _matched_event_phase(event, market) if recovery else None
    if recovery and recovery_phase != "PREGAME":
        return {
            "status": "IGNORED_STALE",
            "reason": f"outage recovery requires a confirmed pregame market; phase={recovery_phase}",
            "recovery_checked_at": _now_iso(),
            "recovery_phase": recovery_phase,
        }

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

    stake = _stake_to_win_at_price(target_profit, max_price)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            "Requested " + str(units) + "u targets $" + str(target_profit)
            + " profit and requires $" + str(stake) + " stake at "
            + str(max_price) + "; exceeds MAX_AUTO_TRADE_USDC=$"
            + str(core.MAX_AUTO_TRADE_USDC)
        )

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
        "strategy_target_profit_usdc": str(target_profit),
        "strategy_sizing_mode": "TO_WIN",
        "strategy_pick_id": fp,
        "strategy_posted_at": pick.get("posted_at"),
        "strategy_selection": pick.get("selection"),
        "strategy_telegram_source": pick.get("source"),
        "strategy_recovered_after_bridge_outage": bool(recovery),
        "strategy_requested_spread_line": requested_spread_line,
        "strategy_executed_spread_line": executed_spread_line,
        "strategy_alternate_spread_fallback": alternate_spread_fallback,
        "strategy_requested_total_line": requested_total_line,
        "strategy_executed_total_line": executed_total_line,
        "strategy_alternate_total_fallback": alternate_total_fallback,
        "strategy_total_scope": "team_total" if kind == "total" and _is_team_total_pick(pick) else ("game_total" if kind == "total" else None),
    }
    queued = remote._enqueue("BUY", payload)
    waiting_approval = str(queued.get("status") or "").upper() == "WAITING_APPROVAL"
    return {
        "status": "WAITING_APPROVAL" if waiting_approval else "QUEUED",
        "request_id": queued["id"],
        "approval_required": waiting_approval,
        "approval_reason": payload.get("approval_reason"),
        "signal_decimal_odds": payload.get("signal_decimal_odds"),
        "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
        "trade_id": trade_id,
        "strategy_source": source_label,
        "market_type": kind,
        "market": str(getattr(market, "question", "") or getattr(event, "title", "NFL market")),
        "market_url": market_url,
        "outcome": outcome_label,
        "asset_id": asset_id,
        "units": str(units),
        "unit_usdc": str(unit_usdc),
        "target_profit_usdc": str(target_profit),
        "stake_usdc": str(stake),
        "sizing_mode": "TO_WIN",
        "max_price": str(max_price),
        "spread": str(spread),
        "recovered_after_bridge_outage": bool(recovery),
        "recovery_phase": recovery_phase,
        "requested_spread_line": requested_spread_line,
        "executed_spread_line": executed_spread_line,
        "alternate_spread_fallback": alternate_spread_fallback,
        "requested_total_line": requested_total_line,
        "executed_total_line": executed_total_line,
        "alternate_total_fallback": alternate_total_fallback,
        "market_scope": "team_total" if kind == "total" and _is_team_total_pick(pick) else ("game_total" if kind == "total" else None),
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
    target_profit = _target_profit_for_pick(pick, unit_usdc)

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

    stake = _stake_to_win_at_price(target_profit, best_ask)
    if stake > core.MAX_AUTO_TRADE_USDC:
        raise RuntimeError(
            "Requested " + str(units) + "u targets $" + str(target_profit)
            + " profit and requires $" + str(stake) + " stake at "
            + str(best_ask) + "; exceeds MAX_AUTO_TRADE_USDC=$"
            + str(core.MAX_AUTO_TRADE_USDC)
        )

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
        "strategy_target_profit_usdc": str(target_profit),
        "strategy_sizing_mode": "TO_WIN",
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
        "unit_usdc": str(unit_usdc),
        "target_profit_usdc": str(target_profit),
        "stake_usdc": str(stake),
        "sizing_mode": "TO_WIN",
        "current_buy_price": str(buy_price),
        "best_ask": str(best_ask),
        "max_price": str(best_ask),
        "spread": str(spread),
    }


def _live_mark_map(dashboard: Any, executions: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the same executable SELL-side marks used by the main live dashboard."""
    records = [rec for rec in executions.values() if isinstance(rec, dict)]
    try:
        current, _ = dashboard._estimate_pnl(records)
    except Exception:
        return {}
    return {
        str(row.get("id")): row
        for row in current
        if isinstance(row, dict) and row.get("id") is not None
    }


def _stats_from_executions(
    executions: dict[str, Any],
    *,
    labels: tuple[str, ...] = SOURCE_LABELS,
    sport: str = "NFL",
    live_marks: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    marks = live_marks or {}
    out: dict[str, dict[str, Any]] = {}
    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    for label in labels:
        wins = losses = pushes = open_trades = total = live_marked = 0
        stake = Decimal("0")
        realized = Decimal("0")
        realized_7d = Decimal("0")
        realized_30d = Decimal("0")
        units = Decimal("0")
        live_pnl = Decimal("0")
        open_cost_basis = Decimal("0")
        open_value = Decimal("0")
        for execution_id, rec in executions.items():
            if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("strategy_source") != label:
                continue
            record_sport = str(rec.get("strategy_sport") or "").upper()
            if sport and record_sport and record_sport != str(sport).upper():
                continue
            total += 1
            try:
                units += Decimal(str(rec.get("strategy_units") or "0"))
            except Exception:
                pass
            status = str(rec.get("status") or "")
            if status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}:
                open_trades += 1
                trade_id = str(rec.get("id") or execution_id)
                mark = marks.get(trade_id) or {}
                try:
                    entry = Decimal(str(mark.get("entry_price") or "0"))
                    shares = Decimal(str(mark.get("shares") or "0"))
                    basis = entry * shares if entry > 0 and shares > 0 else Decimal(
                        str(rec.get("actual_cost_usdc") or rec.get("budget_usdc") or "0")
                    )
                    open_cost_basis += basis
                except Exception:
                    pass
                pnl_raw = mark.get("estimated_pnl")
                if pnl_raw is not None:
                    try:
                        live_pnl += Decimal(str(pnl_raw))
                        live_marked += 1
                    except Exception:
                        pass
                try:
                    current_value_raw = mark.get("current_value_usdc")
                    if current_value_raw is not None:
                        open_value += Decimal(str(current_value_raw))
                    else:
                        current_price = Decimal(str(mark.get("current_price") or mark.get("current_sell_price") or "0"))
                        shares = Decimal(str(mark.get("shares") or "0"))
                        if current_price > 0 and shares > 0:
                            open_value += current_price * shares
                except Exception:
                    pass
                continue
            pnl_raw = rec.get("realized_pnl")
            if pnl_raw is None:
                continue
            try:
                pnl = Decimal(str(pnl_raw))
            except Exception:
                continue
            realized += pnl
            realized_at = _parse_iso(
                rec.get("closed_at")
                or rec.get("updated_at")
                or rec.get("submitted_at")
                or rec.get("created_at")
            )
            if realized_at is not None:
                if realized_at >= cutoff_30d:
                    realized_30d += pnl
                if realized_at >= cutoff_7d:
                    realized_7d += pnl
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
        total_live_pnl = realized + live_pnl
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
            "realized_pnl_7d_usdc": str(realized_7d.quantize(Decimal("0.01"))),
            "realized_pnl_30d_usdc": str(realized_30d.quantize(Decimal("0.01"))),
            "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
            "live_marked": live_marked,
            "live_pnl_usdc": str(live_pnl.quantize(Decimal("0.01"))) if live_marked else None,
            "total_live_pnl_usdc": str(total_live_pnl.quantize(Decimal("0.01"))),
            "open_cost_basis_usdc": str(open_cost_basis.quantize(Decimal("0.01"))) if open_trades else "0.00",
            "open_value_usdc": str(open_value.quantize(Decimal("0.01"))) if live_marked else None,
        }
    return out


def _execution_for_nfl_signal(
    record: dict[str, Any],
    executions: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return the execution associated with one persisted NFL capper signal."""
    if not isinstance(executions, dict):
        return None
    signal_id = str(record.get("id") or "")
    trade_id = str(record.get("trade_id") or "")
    matches: list[dict[str, Any]] = []
    for execution_id, rec in executions.items():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        if trade_id and str(rec.get("id") or execution_id) == trade_id:
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


def _nfl_dashboard_event_phase(
    record: dict[str, Any],
    execution: dict[str, Any] | None = None,
) -> str | None:
    """Normalize persisted signal/trade state for the dashboard phase tabs."""
    settlement = (execution or {}).get("settlement") or {}
    result = str(
        settlement.get("result")
        or record.get("pick_result")
        or ""
    ).upper()
    if result in {"WIN", "LOSS", "PUSH"} or record.get("final_score"):
        return "CLOSED"

    phase = str(
        record.get("event_phase")
        or record.get("espn_phase")
        or ""
    ).upper()
    if phase in {"PREGAME", "LIVE", "CLOSED"}:
        return phase

    status = str(record.get("status") or "").upper()
    if status in {"EVENT_CLOSED", "SETTLED_WIN", "SETTLED_LOSS", "SETTLED_PUSH"}:
        return "CLOSED"
    if status in {"MATCHED_LIVE", "MATCHED_LIVE_ALTERNATE"}:
        return "LIVE"
    if status in {"MATCHED_PREGAME", "MATCHED_PREGAME_ALTERNATE"}:
        return "PREGAME"
    return None


def _nfl_signal_dashboard_item(
    record: dict[str, Any],
    executions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Shape one NFL capper signal for the same dashboard UI used by CFB."""
    execution = _execution_for_nfl_signal(record, executions)
    settlement = (execution or {}).get("settlement") or {}
    trade_result = str(settlement.get("result") or "").upper() or None
    posted = _parse_iso(record.get("posted_at"))
    age_seconds = None
    if posted is not None:
        age_seconds = max(0, int((datetime.now(timezone.utc) - posted).total_seconds()))

    market_url = str(record.get("market_url") or "")
    matched = bool(
        record.get("asset_id")
        and record.get("outcome")
        and market_url.startswith("https://polymarket.com/")
    )
    phase = _nfl_dashboard_event_phase(record, execution)
    execution_status = str((execution or {}).get("status") or "")
    realized_pnl = (execution or {}).get("realized_pnl")
    trade_stake = (execution or {}).get("actual_cost_usdc") or (execution or {}).get("budget_usdc")

    return {
        "status": record.get("status"),
        "selection": record.get("selection"),
        "posted_at": record.get("posted_at"),
        "updated_at": record.get("updated_at"),
        "signal_age_seconds": age_seconds,
        "units": record.get("units"),
        "unit_usdc": record.get("unit_usdc_at_call") or record.get("unit_usdc"),
        "target_profit_usdc": (
            record.get("target_profit_usdc_at_call")
            or record.get("target_profit_usdc")
        ),
        "stake_usdc": record.get("stake_usdc_at_call") or record.get("stake_usdc"),
        "sizing_mode": record.get("sizing_mode"),
        "unit_mode_at_call": record.get("unit_mode_at_call"),
        "portfolio_pct_at_call": record.get("portfolio_pct_at_call"),
        "portfolio_value_usdc_at_call": record.get("portfolio_value_usdc_at_call"),
        "match_status": "MATCHED" if matched else record.get("match_status"),
        "market_type": record.get("market_type"),
        "requested_total_line": record.get("requested_total_line"),
        "executed_total_line": record.get("executed_total_line"),
        "market": record.get("market"),
        "market_url": record.get("market_url"),
        "event_title": record.get("event_title") or record.get("result_event_title"),
        "event_start_at": record.get("event_start_at"),
        "event_finished_at": record.get("event_finished_at"),
        "event_completed_at": (
            record.get("event_finished_at")
            or record.get("event_completed_at")
            or record.get("result_checked_at")
            or (execution or {}).get("closed_at")
            or (record.get("updated_at") if phase == "CLOSED" else None)
        ),
        "result_checked_at": record.get("result_checked_at"),
        "event_phase": phase,
        "outcome": record.get("outcome"),
        "exact_position": (
            (
                str(record.get("outcome") or "")
                + (
                    " " + str(record.get("executed_total_line"))
                    if record.get("executed_total_line") is not None
                    else ""
                )
            ).strip()
            or record.get("selection")
        ),
        "matchup": record.get("event_title") or record.get("result_event_title"),
        "asset_id": record.get("asset_id"),
        "current_buy_price": record.get("current_buy_price"),
        "best_ask": record.get("best_ask") or record.get("max_price"),
        "spread": record.get("spread"),
        "live_odds_american": record.get("live_odds_american"),
        "reason": record.get("reason"),
        "last_error": record.get("last_error"),
        "request_id": record.get("request_id"),
        "approval_required": record.get("approval_required"),
        "approval_reason": record.get("approval_reason"),
        "signal_decimal_odds": record.get("signal_decimal_odds"),
        "minimum_decimal_odds": record.get("minimum_decimal_odds"),
        "signal_id": record.get("id"),
        "pick_result": record.get("pick_result"),
        "trade_executed": execution is not None,
        "trade_id": str((execution or {}).get("id") or "") or None,
        "trade_status": execution_status or None,
        "trade_result": trade_result,
        "trade_pnl_usdc": realized_pnl,
        "trade_stake_usdc": trade_stake,
        "sell_available": execution_status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"},
        "result_source": record.get("result_source"),
        "result_event_title": record.get("result_event_title"),
        "final_score": record.get("final_score"),
        "live_score": record.get("live_score"),
        "espn_score": record.get("espn_score"),
        "espn_status": record.get("espn_status"),
    }


def _position_items(
    executions: dict[str, Any],
    label: str,
    *,
    sport: str = "NFL",
    limit: int = 40,
    live_marks: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    marks = live_marks or {}
    items: list[dict[str, Any]] = []
    for execution_id, rec in executions.items():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        if rec.get("strategy_source") != label:
            continue
        record_sport = str(rec.get("strategy_sport") or "").upper()
        if sport and record_sport and record_sport != str(sport).upper():
            continue
        status = str(rec.get("status") or "")
        settlement = rec.get("settlement") or {}
        result = str(settlement.get("result") or "").upper() or None
        if result not in {"WIN", "LOSS", "PUSH"}:
            result = None
        quote = rec.get("quote") or {}
        realized = rec.get("realized_pnl")
        stake = rec.get("actual_cost_usdc") or rec.get("budget_usdc")
        trade_id = str(rec.get("id") or execution_id)
        mark = marks.get(trade_id) or {}
        live_pnl = mark.get("estimated_pnl") if status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"} else None
        entry_price = mark.get("entry_price")
        current_price = mark.get("current_price") or mark.get("current_sell_price")
        shares = mark.get("shares")
        current_value = mark.get("current_value_usdc")
        open_cost_basis = None
        live_pnl_pct = None
        try:
            entry_dec = Decimal(str(entry_price or "0"))
            shares_dec = Decimal(str(shares or "0"))
            if entry_dec > 0 and shares_dec > 0:
                basis = entry_dec * shares_dec
                open_cost_basis = str(basis.quantize(Decimal("0.01")))
                if current_value is None and current_price is not None:
                    current_value = str((Decimal(str(current_price)) * shares_dec).quantize(Decimal("0.01")))
                if live_pnl is not None and basis > 0:
                    live_pnl_pct = str((Decimal(str(live_pnl)) / basis * Decimal("100")).quantize(Decimal("0.1")))
        except Exception:
            pass
        items.append({
            "trade_id": trade_id,
            "selection": rec.get("strategy_selection") or quote.get("requested_outcome") or quote.get("market"),
            "status": status,
            "result": result,
            "stake_usdc": stake,
            "realized_pnl_usdc": realized,
            "live_pnl_usdc": live_pnl,
            "live_pnl_pct": live_pnl_pct,
            "entry_price": entry_price,
            "current_price": current_price,
            "shares": shares,
            "open_cost_basis_usdc": open_cost_basis,
            "current_value_usdc": current_value,
            "market": quote.get("market"),
            "market_url": quote.get("market_url"),
            "outcome": quote.get("resolved_outcome") or quote.get("requested_outcome"),
            "submitted_at": rec.get("submitted_at") or rec.get("created_at"),
            "closed_at": rec.get("closed_at"),
            "sell_available": status in {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"},
            "finished": bool(
                result
                or status in {
                    "SETTLED_WIN", "SETTLED_LOSS", "SETTLED_PUSH",
                    "CLOSED", "CLOSED_RECONCILED",
                }
            ),
        })
    items.sort(
        key=lambda item: (
            0 if item.get("sell_available") else 1,
            str(item.get("closed_at") or item.get("submitted_at") or ""),
        )
    )
    return items[:limit]
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
    recovery_max_age_seconds = max(
        max_age_seconds,
        int(os.getenv("NFL_CAPPER_RECOVERY_MAX_PICK_AGE_SECONDS", "86400")),
    )
    feed_window_minutes = max(
        180,
        min(4320, int(os.getenv("NFL_CAPPER_FEED_WINDOW_MINUTES", "1440"))),
    )
    unit_usdc = Decimal(os.getenv("NFL_CAPPER_UNIT_USDC", "10"))
    better_spread_fallback_enabled = os.getenv(
        "NFL_CAPPER_BETTER_SPREAD_FALLBACK_ENABLED",
        "false",
    ).strip().lower() in {"1", "true", "yes", "on"}
    max_alt_spread_points = max(
        Decimal("0"),
        Decimal(os.getenv("NFL_CAPPER_ALT_SPREAD_MAX_POINTS", "1.0")),
    )
    max_alt_total_points = max(
        Decimal("0"),
        Decimal(os.getenv("NFL_CAPPER_ALT_TOTAL_MAX_POINTS", "2.0")),
    )
    no_fill_retry_limit = max(0, int(os.getenv("NFL_CAPPER_NO_FILL_RETRIES", "2")))
    test_preview_raw = os.getenv("NFL_CAPPER_TEST_PREVIEW_JSON", "").strip()
    test_preview_marker = core.DATA_DIR / "nfl_capper_test_preview.json"
    manual_resend_raw = os.getenv("NFL_CAPPER_MANUAL_RESEND_JSON", "").strip()
    manual_resend_marker = core.DATA_DIR / "nfl_capper_manual_resend.json"

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
            current_status = str(rec.get("status") or "")
            if current_status not in {"QUEUED", "EXECUTOR_DONE", "EXECUTOR_FAILED"} or not rec.get("request_id"):
                continue
            queued = queue.get(str(rec.get("request_id")))
            if not queued:
                continue
            qstatus = str(queued.get("status") or "")
            no_fill = _queue_result_is_no_fill(queued)
            if no_fill and qstatus in {"DONE", "FAILED"}:
                attempts = int(rec.get("no_fill_retries") or 0)
                if attempts < no_fill_retry_limit:
                    rec["status"] = "RETRYING"
                    rec["no_fill_retries"] = attempts + 1
                    rec["last_error"] = str(
                        queued.get("error")
                        or "BUY_UNFILLED_RETRYABLE: previous limit order filled 0 shares"
                    )
                    rec["last_no_fill_at"] = queued.get("updated_at") or _now_iso()
                    rec.pop("request_id", None)
                    rec.pop("completed_at", None)
                else:
                    rec["status"] = "EXECUTOR_FAILED"
                    rec["last_error"] = (
                        str(queued.get("error") or "BUY unfilled")
                        + f"; no-fill retry limit {no_fill_retry_limit} reached"
                    )
                    rec["completed_at"] = queued.get("updated_at") or _now_iso()
                changed = True
            elif qstatus == "DONE":
                if current_status != "EXECUTOR_DONE":
                    rec["status"] = "EXECUTOR_DONE"
                    rec["completed_at"] = queued.get("updated_at") or _now_iso()
                    rec["result"] = queued.get("result")
                    changed = True
            elif qstatus == "FAILED":
                failure_text = str(queued.get("error") or "")
                # One-time recovery for orders that were rejected solely by the
                # retired Termux phone emergency ceiling. Reuse the exact original
                # BUY payload so stake, token, line, max price and strategy metadata
                # do not get recalculated at retry time.
                if (
                    "phone emergency hard ceiling" in failure_text
                    and not rec.get("legacy_phone_ceiling_retry_done")
                    and str((queued.get("payload") or {}).get("strategy_selection") or rec.get("selection") or "").strip().lower() == "bears +4"
                    and str((queued.get("payload") or {}).get("strategy_source") or rec.get("source") or "").strip() == "Syndicate - NFL"
                ):
                    retry_payload = dict(queued.get("payload") or {})
                    retried = remote._enqueue("BUY", retry_payload)
                    rec["status"] = "QUEUED"
                    rec["request_id"] = retried["id"]
                    rec["legacy_phone_ceiling_retry_done"] = True
                    rec["legacy_phone_ceiling_retry_at"] = _now_iso()
                    rec["legacy_phone_ceiling_original_request_id"] = queued.get("id")
                    rec["last_error"] = failure_text
                    rec.pop("completed_at", None)
                    changed = True
                    print(
                        "NFL_CAPPER_SIGNAL "
                        f"status=QUEUED source={rec.get('source')!r} "
                        f"selection={rec.get('selection')!r} "
                        f"request_id={retried['id']!r} "
                        f"recovery=legacy_phone_ceiling original_request_id={queued.get('id')!r}",
                        flush=True,
                    )
                else:
                    rec["status"] = "EXECUTOR_FAILED"
                    rec["last_error"] = queued.get("error")
                    rec["completed_at"] = queued.get("updated_at") or _now_iso()
                    rec["result"] = queued.get("result")
                    changed = True
        return changed

    async def _poll_once() -> None:
        nonlocal last_feed_signature
        _STATUS["last_poll_at"] = _now_iso()
        signals = _load_signals()
        changed = _sync_queue_status(signals)
        if await asyncio.to_thread(_refresh_nfl_signal_results, signals):
            changed = True
        if not enabled:
            _STATUS["last_error"] = "NFL capper automation is disabled"
            if changed:
                _save_signals(signals)
            return

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{bridge_url}/public/nfl",
                    params={
                        "minutes": feed_window_minutes,
                        "limit": 120,
                        "include_graded": "false",
                        "require_fresh": "true",
                    },
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
                            "total_side", "total_line", "units", "decimal_odds", "american_odds", "status",
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
            if current and not isinstance(current.get("pick"), dict):
                current["pick"] = pick
                changed = True
            if current and current.get("status") in {
                "QUEUED", "EXECUTOR_DONE", "EXECUTOR_FAILED",
                "IGNORED_UNSUPPORTED", "IGNORED_UNTRACKED_SOURCE",
            }:
                continue
            if (
                current
                and current.get("status") == "IGNORED_STALE"
                and current.get("recovery_checked_at")
            ):
                continue

            source_label = _source_label(pick)
            if source_label is not None:
                unit_config = _capper_unit_config(core, source_label, unit_usdc)
                effective_unit_usdc = Decimal(str(unit_config["unit_usdc"]))
            else:
                unit_config = {
                    "mode": "fixed",
                    "unit_usdc": str(unit_usdc),
                    "fixed_unit_usdc": str(unit_usdc),
                    "portfolio_pct": "10.00",
                    "portfolio_value_usdc": None,
                    "error": None,
                }
                effective_unit_usdc = unit_usdc
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
                "unit_usdc": str(effective_unit_usdc),
                "target_profit_usdc": str(_target_profit_for_pick(pick, effective_unit_usdc)),
                "sizing_mode": "TO_WIN",
                "unit_mode": unit_config["mode"],
                "portfolio_pct": unit_config["portfolio_pct"],
                "portfolio_value_usdc": unit_config["portfolio_value_usdc"],
                "pick": pick,
            }
            if _ensure_signal_call_unit_snapshot(
                base_record,
                pick,
                unit_config=unit_config,
                effective_unit_usdc=effective_unit_usdc,
            ):
                changed = True
            if current:
                base_record["unit_usdc"] = str(effective_unit_usdc)
                base_record["target_profit_usdc"] = str(
                    _target_profit_for_pick(pick, effective_unit_usdc)
                )
                base_record["sizing_mode"] = "TO_WIN"
                base_record["unit_mode"] = unit_config["mode"]
                base_record["portfolio_pct"] = unit_config["portfolio_pct"]
                base_record["portfolio_value_usdc"] = unit_config["portfolio_value_usdc"]

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

            if not capper_control.is_enabled(core, SPORT_CONTROL_LABEL):
                base_record["status"] = "PAUSED_SPORT"
                base_record["reason"] = "NFL auto-trading is offline; automatic trading is paused"
                base_record["updated_at"] = _now_iso()
                base_record.pop("last_error", None)
                signals[fp] = base_record
                changed = True
                continue

            if not capper_control.is_enabled(core, source_label):
                base_record["status"] = "PAUSED_CAPPER"
                base_record["reason"] = f"{source_label} is offline; automatic trading is paused"
                base_record["updated_at"] = _now_iso()
                base_record.pop("last_error", None)
                signals[fp] = base_record
                changed = True
                continue

            if unit_config.get("error"):
                base_record["status"] = "RETRYING"
                base_record["last_error"] = str(unit_config["error"])
                base_record["updated_at"] = _now_iso()
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status=RETRYING source={source_label!r} "
                    f"selection={pick.get('selection')!r} "
                    f"reason={base_record['last_error']!r}",
                    flush=True,
                )
                continue

            posted = _parse_iso(pick.get("posted_at"))
            age = (now - posted).total_seconds() if posted else float("inf")
            recovery = _is_recovery_age(age, max_age_seconds, recovery_max_age_seconds)
            if age > recovery_max_age_seconds:
                base_record["status"] = "IGNORED_STALE"
                base_record["reason"] = (
                    f"pick age exceeds {recovery_max_age_seconds}s outage-recovery limit"
                )
                base_record["recovery_checked_at"] = _now_iso()
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
                result = await asyncio.to_thread(
                    _prepare_pick,
                    pick,
                    core=core,
                    remote=remote,
                    unit_usdc=effective_unit_usdc,
                    recovery=recovery,
                    better_spread_fallback_enabled=better_spread_fallback_enabled,
                    max_alt_spread_points=max_alt_spread_points,
                    max_alt_total_points=max_alt_total_points,
                )
                base_record.update(result)
                if not base_record.get("stake_usdc_at_call") and result.get("stake_usdc"):
                    base_record["stake_usdc_at_call"] = result.get("stake_usdc")
                if recovery:
                    base_record["recovery_checked_at"] = _now_iso()
                base_record["updated_at"] = _now_iso()
                base_record.pop("last_error", None)
                signals[fp] = base_record
                changed = True
                print(
                    "NFL_CAPPER_SIGNAL "
                    f"status={base_record.get('status')} source={source_label!r} "
                    f"telegram_source={pick.get('source')!r} selection={pick.get('selection')!r} "
                    f"request_id={base_record.get('request_id')!r} recovery={recovery}",
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

    async def _run_manual_resend_once() -> None:
        """One-shot, environment-triggered resend through the normal NFL BUY path.

        This exists for repairing a specific already-audited signal after a parser/
        market-scope bug. A SHA marker on the persistent volume makes deploy/restart
        retries idempotent, and the normal auto-trading, price, spread, sizing,
        budget, approval and Termux-executor safeguards remain in force.
        """
        if not manual_resend_raw:
            return

        trigger_hash = hashlib.sha256(manual_resend_raw.encode("utf-8")).hexdigest()
        existing_marker = core._load(manual_resend_marker) if manual_resend_marker.exists() else {}
        if (
            isinstance(existing_marker, dict)
            and existing_marker.get("trigger_hash") == trigger_hash
            and existing_marker.get("status") in {"QUEUED", "WAITING_APPROVAL", "DONE", "SKIPPED_DUPLICATE"}
        ):
            return

        try:
            request = json.loads(manual_resend_raw)
            if not isinstance(request, dict):
                raise RuntimeError("NFL_CAPPER_MANUAL_RESEND_JSON must decode to an object")
            pick = dict(request.get("pick") or {})
            if not pick:
                raise RuntimeError("manual resend requires a pick object")
            pick["posted_at"] = _now_iso()
            if _source_label(pick) not in SOURCE_LABELS:
                raise RuntimeError("manual resend source must resolve to Slam or Syndicate")
            kind, reason = _classify_pick(pick)
            if kind is None:
                raise RuntimeError(reason or "manual resend pick is unsupported")
            if kind == "total" and bool(request.get("require_team_total", False)) and not _is_team_total_pick(pick):
                raise RuntimeError("manual resend requires team-total classification")

            # Block a second corrected position/request for the same selection.
            selection_norm = _norm(pick.get("selection"))
            executions = core._load(core.EXECUTIONS_FILE)
            if isinstance(executions, dict):
                for rec in executions.values():
                    if not isinstance(rec, dict) or rec.get("parent_trade_id"):
                        continue
                    if _norm(rec.get("strategy_selection")) != selection_norm:
                        continue
                    if str(rec.get("strategy_total_scope") or "") != "team_total":
                        continue
                    if str(rec.get("status") or "").upper() not in {
                        "FAILED", "CLOSED", "CLOSED_RECONCILED",
                        "SETTLED_WIN", "SETTLED_LOSS", "SETTLED_PUSH",
                    }:
                        marker = {
                            "trigger_hash": trigger_hash,
                            "created_at": _now_iso(),
                            "status": "SKIPPED_DUPLICATE",
                            "reason": "an open corrected team-total execution already exists",
                            "trade_id": rec.get("id"),
                        }
                        core._save(manual_resend_marker, marker)
                        print("NFL_CAPPER_MANUAL_RESEND " + json.dumps(marker, sort_keys=True, default=str), flush=True)
                        return

            try:
                resend_unit = Decimal(str(request["unit_usdc"]))
            except Exception as exc:
                raise RuntimeError("manual resend requires unit_usdc") from exc
            if resend_unit <= 0:
                raise RuntimeError("manual resend unit_usdc must be positive")

            result = await asyncio.to_thread(
                _prepare_pick,
                pick,
                core=core,
                remote=remote,
                unit_usdc=resend_unit,
                recovery=False,
                better_spread_fallback_enabled=better_spread_fallback_enabled,
                max_alt_spread_points=max_alt_spread_points,
                max_alt_total_points=max_alt_total_points,
            )
            if bool(request.get("require_team_total", False)) and result.get("market_scope") != "team_total":
                raise RuntimeError("resolved resend was not a team-total market")

            marker = {
                "trigger_hash": trigger_hash,
                "created_at": _now_iso(),
                "status": result.get("status"),
                "pick": pick,
                "unit_usdc": str(resend_unit),
                "result": result,
            }
            core._save(manual_resend_marker, marker)
            print("NFL_CAPPER_MANUAL_RESEND " + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str), flush=True)
        except Exception as exc:
            marker = {
                "trigger_hash": trigger_hash,
                "created_at": _now_iso(),
                "status": "FAILED",
                "error": f"{type(exc).__name__}: {exc}",
            }
            core._save(manual_resend_marker, marker)
            print("NFL_CAPPER_MANUAL_RESEND " + json.dumps(marker, sort_keys=True, separators=(",", ":"), default=str), flush=True)


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
            test_source = _source_label(pick)
            test_unit_usdc = (
                _capper_unit_usdc(core, test_source, unit_usdc)
                if test_source is not None
                else unit_usdc
            )
            result = await asyncio.to_thread(
                _prepare_test_preview,
                pick,
                core=core,
                remote=remote,
                unit_usdc=test_unit_usdc,
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
            manual_resend_task = asyncio.create_task(
                _run_manual_resend_once(),
                name="nfl-capper-manual-resend",
            )
            try:
                yield
            finally:
                for pending in (task, preview_task, manual_resend_task):
                    pending.cancel()
                for pending in (task, preview_task, manual_resend_task):
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _lifespan

    def _stats_payload() -> dict[str, Any]:
        executions = core._load(core.EXECUTIONS_FILE)
        if not isinstance(executions, dict):
            executions = {}
        signals = _load_signals()
        live_marks = _live_mark_map(dashboard, executions)
        stats = _stats_from_executions(executions, live_marks=live_marks)
        unit_config_by_label = {
            label: _capper_unit_config(core, label, unit_usdc)
            for label in SOURCE_LABELS
        }
        unit_by_label = {
            label: Decimal(str(config["unit_usdc"]))
            for label, config in unit_config_by_label.items()
        }
        missed = _missed_signal_stats(
            signals,
            executions,
            labels=SOURCE_LABELS,
            sport="NFL",
            unit_usdc=unit_usdc,
            unit_usdc_by_label=unit_by_label,
        )
        for label in SOURCE_LABELS:
            unit_config = unit_config_by_label[label]
            stats.setdefault(label, {}).update(missed.get(label, {}))
            stats[label]["unit_usdc"] = unit_config["unit_usdc"]
            stats[label]["fixed_unit_usdc"] = unit_config["fixed_unit_usdc"]
            stats[label]["unit_mode"] = unit_config["mode"]
            stats[label]["portfolio_pct"] = unit_config["portfolio_pct"]
            stats[label]["portfolio_value_usdc"] = unit_config["portfolio_value_usdc"]
            stats[label]["unit_error"] = unit_config["error"]
            stats[label]["sizing_mode"] = "TO_WIN"
            stats[label]["enabled"] = capper_control.is_enabled(core, label)
            stats[label]["positions"] = _position_items(
                executions,
                label,
                sport="NFL",
                live_marks=live_marks,
            )
            rows = [
                r for r in signals.values()
                if isinstance(r, dict) and r.get("source") == label
            ]
            items = [_nfl_signal_dashboard_item(r, executions) for r in rows]
            items.sort(
                key=lambda item: str(
                    item.get("updated_at")
                    or item.get("posted_at")
                    or ""
                ),
                reverse=True,
            )

            def status_items(*wanted: str) -> list[dict[str, Any]]:
                wanted_set = set(wanted)
                return [
                    item for item in items
                    if str(item.get("status") or "") in wanted_set
                ][:30]

            stats[label].update({
                "signals": len(rows),
                "queued": sum(1 for r in rows if r.get("status") in {"QUEUED", "WAITING_APPROVAL"}),
                "done": sum(1 for r in rows if r.get("status") == "EXECUTOR_DONE"),
                "failed": sum(1 for r in rows if r.get("status") == "EXECUTOR_FAILED"),
                "retrying": sum(1 for r in rows if r.get("status") == "RETRYING"),
                "pregame": sum(1 for item in items if item.get("event_phase") == "PREGAME"),
                "live": sum(1 for item in items if item.get("event_phase") == "LIVE"),
                "closed": sum(1 for item in items if item.get("event_phase") == "CLOSED"),
                "unsupported": sum(1 for r in rows if r.get("status") == "IGNORED_UNSUPPORTED"),
                "all_items": items[:60],
                "queued_items": status_items("QUEUED", "WAITING_APPROVAL"),
                "done_items": status_items("EXECUTOR_DONE"),
                "failed_items": status_items("EXECUTOR_FAILED"),
                "retrying_items": status_items("RETRYING"),
                "pregame_items": [item for item in items if item.get("event_phase") == "PREGAME"][:30],
                "live_items": [item for item in items if item.get("event_phase") == "LIVE"][:30],
                "closed_items": [item for item in items if item.get("event_phase") == "CLOSED"][:30],
                "unsupported_items": status_items("IGNORED_UNSUPPORTED"),
            })
        signal_summary = {
            "queued": sum(1 for x in signals.values() if (x or {}).get("status") in {"QUEUED", "EXECUTOR_DONE"}),
            "failed": sum(1 for x in signals.values() if (x or {}).get("status") == "EXECUTOR_FAILED"),
            "retrying": sum(1 for x in signals.values() if (x or {}).get("status") == "RETRYING"),
            "stale_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_STALE"),
            "unsupported_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_UNSUPPORTED"),
            "untracked_source_ignored": sum(1 for x in signals.values() if (x or {}).get("status") == "IGNORED_UNTRACKED_SOURCE"),
        }
        auto_trading = bool(core.auto_trading_enabled())
        live_trading = bool(core.live_trading_enabled())
        sport_enabled = capper_control.is_enabled(core, SPORT_CONTROL_LABEL)
        return {
            "enabled": enabled,
            "sport_enabled": sport_enabled,
            "unit_usdc": str(unit_usdc),
            "sizing_mode": "TO_WIN",
            "poll_seconds": poll_seconds,
            "max_pick_age_seconds": max_age_seconds,
            "feed_window_minutes": feed_window_minutes,
            "better_spread_fallback_enabled": better_spread_fallback_enabled,
            "max_alt_spread_points": str(max_alt_spread_points),
            "auto_trading": auto_trading,
            "live_trading": live_trading,
            "auto_live": bool(enabled and sport_enabled and auto_trading and live_trading),
            "status": dict(_STATUS),
            "signals": signal_summary,
            "cappers": stats,
        }

    @app.get("/api/nfl-cappers/stats", dependencies=[Depends(dashboard._auth)])
    def nfl_capper_stats():
        return _stats_payload()

    @app.put("/api/nfl-cappers/sport-enabled", dependencies=[Depends(dashboard._auth)])
    def nfl_sport_enabled(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        enabled_now = capper_control.set_enabled(core, SPORT_CONTROL_LABEL, payload["enabled"])
        return {
            "ok": True,
            "sport": "NFL",
            "enabled": enabled_now,
            "note": "Controls new NFL automatic trades; existing queued/open positions are unchanged.",
        }

    @app.put("/api/nfl-cappers/enabled/{capper_key}", dependencies=[Depends(dashboard._auth)])
    def nfl_capper_enabled(capper_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = {
            "slam": "Slam - NFL",
            "syndicate": "Syndicate - NFL",
        }.get(str(capper_key).strip().lower())
        if label is None:
            raise HTTPException(status_code=404, detail="Unknown NFL capper")
        if not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled must be true or false")
        enabled_now = capper_control.set_enabled(core, label, payload["enabled"])
        return {
            "ok": True,
            "capper": label,
            "enabled": enabled_now,
            "note": "Controls new automatic trades only; existing queued/open positions are unchanged.",
        }

    @app.put("/api/nfl-cappers/unit-size/{capper_key}", dependencies=[Depends(dashboard._auth)])
    def nfl_capper_unit_size(capper_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = {
            "slam": "Slam - NFL",
            "syndicate": "Syndicate - NFL",
        }.get(str(capper_key).strip().lower())
        if label is None:
            raise HTTPException(status_code=404, detail="Unknown NFL capper")
        try:
            _set_capper_unit_usdc(core, label, payload.get("unit_usdc"))
            config = _capper_unit_config(core, label, unit_usdc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "capper": label,
            **config,
            "sizing_mode": "TO_WIN",
            "note": "Fixed mode enabled. Applies to new/retried orders; existing queued/open positions are unchanged.",
        }

    @app.put("/api/nfl-cappers/unit-percent/{capper_key}", dependencies=[Depends(dashboard._auth)])
    def nfl_capper_unit_percent(capper_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        label = {
            "slam": "Slam - NFL",
            "syndicate": "Syndicate - NFL",
        }.get(str(capper_key).strip().lower())
        if label is None:
            raise HTTPException(status_code=404, detail="Unknown NFL capper")
        try:
            _set_capper_portfolio_pct(core, label, payload.get("portfolio_pct", 10))
            config = _capper_unit_config(core, label, unit_usdc)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "ok": True,
            "capper": label,
            **config,
            "sizing_mode": "TO_WIN",
            "note": "Portfolio-percentage mode enabled. 1u recalculates from the latest total wallet value for every new/retried order.",
        }

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
    <div class="nfl-capper-head">
      <div class="capper-panel-title">NFL AUTO-TRADING</div>
    </div>
    <div class="capper-panel-status-row">
      <div class="capper-status-field"><div class="capper-status-label">STATUS</div><div class="nfl-capper-state" id="nflCapperState">Loading…</div></div>
      <button type="button" class="badge capper-power-btn sport-power-btn" id="nflSportPower" data-enabled="1" aria-pressed="true" onclick="nflToggleSport(this)" title="Toggle all NFL automatic trading"><span class="dot"></span><span class="capper-power-text">Online</span></button>
    </div>
    <div class="nfl-capper-meta" id="nflCapperMeta"></div>
    <div class="capper-panel-actions"><button type="button" id="nflFinishedToggle" onclick="nflToggleFinished()">Hide finished</button><button type="button" data-capper-last24h-toggle onclick="capperToggleLast24h()">Last 24h only: OFF</button></div>
    <div class="nfl-capper-grid">
      <div class="nfl-capper-card"><div class="capper-card-head"><b>Slam - NFL</b><button type="button" class="badge capper-power-btn" id="nflCapperPower-slam" data-enabled="1" aria-pressed="true" onclick="nflToggleCapper('slam',this)" title="Toggle Slam - NFL automatic trading"><span class="dot"></span><span class="capper-power-text">Online</span></button></div><div class="nfl-capper-kpis" id="nflCapperSlam">—</div></div>
      <div class="nfl-capper-card"><div class="capper-card-head"><b>Syndicate - NFL</b><button type="button" class="badge capper-power-btn" id="nflCapperPower-syndicate" data-enabled="1" aria-pressed="true" onclick="nflToggleCapper('syndicate',this)" title="Toggle Syndicate - NFL automatic trading"><span class="dot"></span><span class="capper-power-text">Online</span></button></div><div class="nfl-capper-kpis" id="nflCapperSyndicate">—</div></div>
    </div>
  </div>
"""
        html = html.replace('  <div class="tabs">', panel + '  <div class="tabs">', 1)
        css = r"""
.nfl-capper-panel{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:14px;padding:15px;margin:14px 0}.nfl-capper-head{display:flex;justify-content:flex-start;align-items:center;text-align:left}.capper-panel-title{font-size:15px;font-weight:900;line-height:1.25;text-transform:uppercase}.capper-panel-status-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:7px 0 5px;text-align:left}.nfl-capper-state{font-size:15px;font-weight:850;margin:0}.nfl-capper-meta{font-size:12px;color:var(--muted);text-align:left;line-height:1.45}.capper-panel-actions{display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-start;margin-top:7px}.nfl-capper-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.nfl-capper-card{border:1px solid var(--border);border-radius:10px;background:#0d1522;padding:13px}.capper-card-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:5px;text-align:left}.capper-card-head>b{font-size:18px;line-height:1.3}.capper-power-btn{flex:0 0 auto;white-space:nowrap}.nfl-capper-kpis{font-size:14px;color:var(--muted);line-height:1.75;margin-top:7px}.nfl-capper-kpis .capper-pnl{font-size:17px;font-weight:900}.nfl-capper-kpis .capper-pnl.positive,.nfl-result-line.positive{color:#86efac}.nfl-capper-kpis .capper-pnl.negative,.nfl-result-line.negative{color:#fca5a5}.nfl-capper-kpis .capper-pnl.flat,.nfl-result-line.flat{color:var(--muted)}.nfl-position-row{margin-top:8px;padding:9px 10px;border-radius:8px;border:1px solid rgba(255,255,255,.08)}.nfl-position-row.win{background:rgba(34,197,94,.11);border-color:rgba(34,197,94,.40);border-left:4px solid #22c55e}.nfl-position-row.loss{background:rgba(239,68,68,.10);border-color:rgba(239,68,68,.40);border-left:4px solid #ef4444}.nfl-position-row.push{background:rgba(245,158,11,.10);border-color:rgba(245,158,11,.38);border-left:4px solid #f59e0b}.nfl-position-row.open{background:rgba(245,158,11,.12);border-color:rgba(245,158,11,.46);border-left:4px solid #f59e0b}.nfl-position-title{font-size:15px;font-weight:850}.nfl-position-pnl,.nfl-result-line{font-size:16px;font-weight:900}.nfl-position-pnl.positive{color:#86efac}.nfl-position-pnl.negative{color:#fca5a5}.nfl-result-line{margin-top:6px;line-height:1.35}@media(max-width:650px){.nfl-capper-grid{grid-template-columns:1fr}.nfl-capper-meta{text-align:left}.capper-card-head>b{font-size:19px}.nfl-capper-kpis{font-size:15px}.nfl-capper-kpis .capper-pnl{font-size:18px}.nfl-position-title{font-size:16px}.nfl-position-pnl,.nfl-result-line{font-size:17px}}
"""
        html = html.replace("</style>", css + "</style>", 1)
        js = r"""
function nflEsc(v){
 return String(v??'').replace(/[&<>\"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[ch]));
}
function nflPickTime(v){
 if(!v)return '';
 const d=new Date(v);
 return Number.isNaN(d.getTime())?nflEsc(v):nflEsc(d.toLocaleString());
}
function nflAge(seconds){
 if(seconds===null||seconds===undefined)return '';
 const s=Math.max(0,Number(seconds)||0);
 if(s<60)return Math.floor(s)+'s';
 if(s<3600)return Math.floor(s/60)+'m';
 return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
function nflEventDelta(ms){
 const s=Math.max(0,Math.floor(Number(ms||0)/1000));
 if(s<60)return s+'s';
 if(s<3600)return Math.floor(s/60)+'m';
 if(s<86400)return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
 return Math.floor(s/86400)+'d '+Math.floor((s%86400)/3600)+'h';
}
function nflEventBadgeLabel(item){
 const phase=String(item.event_phase||'').toUpperCase();
 const now=Date.now();
 if(phase==='LIVE')return 'LIVE';
 if(phase==='CLOSED'){
  const raw=item.event_completed_at||item.event_finished_at||item.result_checked_at||item.closed_at||item.updated_at;
  const done=raw?new Date(raw):null;
  if(done&&!Number.isNaN(done.getTime())&&now>=done.getTime())return 'COMPLETED · '+nflEventDelta(now-done.getTime())+' ago';
  return 'COMPLETED';
 }
 const start=item.event_start_at?new Date(item.event_start_at):null;
 if(start&&!Number.isNaN(start.getTime())){
  const remaining=start.getTime()-now;
  if(remaining>0)return 'T-'+nflEventDelta(remaining);
  if(phase==='PREGAME')return 'STARTING';
 }
 if(phase==='PREGAME')return 'UPCOMING';
 return '';
}
function nflPhaseVisual(item){
 const phase=String(item.event_phase||'').toUpperCase();
 const status=String(item.status||'').toUpperCase();
 const result=String(item.trade_result||item.pick_result||'').toUpperCase();
 const label=nflEventBadgeLabel(item);
 if(status==='WAITING_APPROVAL')return {label:'APPROVAL REQUIRED',row:'background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.42);border-left:4px solid #f59e0b;',badge:'color:#8a5b00;'};
 if(phase==='CLOSED'&&result==='WIN')return {label,row:'background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.42);border-left:4px solid #22c55e;',badge:'color:#008000;'};
 if(phase==='CLOSED'&&result==='LOSS')return {label,row:'background:rgba(239,68,68,.11);border:1px solid rgba(239,68,68,.42);border-left:4px solid #ef4444;',badge:'color:#b00000;'};
 if(phase==='CLOSED'&&result==='PUSH')return {label,row:'background:rgba(245,158,11,.10);border:1px solid rgba(245,158,11,.38);border-left:4px solid #f59e0b;',badge:'color:#8a5b00;'};
 if(phase==='LIVE')return {label:'LIVE',row:'background:rgba(34,197,94,.10);border:1px solid rgba(34,197,94,.38);border-left:4px solid #22c55e;',badge:'color:#008000;'};
 if(phase==='CLOSED')return {label,row:'background:rgba(148,163,184,.08);border:1px solid rgba(148,163,184,.24);border-left:4px solid #94a3b8;opacity:.82;',badge:'color:#404040;'};
 if(phase==='PREGAME')return {label,row:'background:rgba(59,130,246,.10);border:1px solid rgba(59,130,246,.35);border-left:4px solid #3b82f6;',badge:'color:#000080;'};
 const fallback=status==='RETRYING'?'RETRYING':status==='EXECUTOR_FAILED'?'FAILED':status==='EXECUTOR_DONE'?'DONE':status==='QUEUED'?'QUEUED':'';
 return {label:label||fallback,row:'border:1px solid rgba(255,255,255,.07);',badge:'color:#404040;'};
}
let nflHideFinished=localStorage.getItem('nflHideFinished')==='1';
const nflActiveTabs={slam:'signals',syndicate:'signals'};
let nflLastCappers={};
function nflUpdateFinishedToggle(){
 const btn=document.getElementById('nflFinishedToggle');
 if(btn){btn.textContent=nflHideFinished?'Show finished':'Hide finished';btn.classList.toggle('active',nflHideFinished);btn.setAttribute('aria-pressed',nflHideFinished?'true':'false')}
}
function nflSyncSportPower(enabled){
 const btn=document.getElementById('nflSportPower');
 if(!btn)return;
 const on=enabled!==false;
 btn.dataset.enabled=on?'1':'0';
 btn.classList.toggle('active',on);
 btn.classList.toggle('offline',!on);
 btn.setAttribute('aria-pressed',on?'true':'false');
 const text=btn.querySelector('.capper-power-text');
 if(text)text.textContent=on?'Online':'Offline';
}
async function nflToggleSport(btn){
 const online=btn.dataset.enabled!=='0';
 if(online&&!confirm('Turn NFL AUTO-TRADING OFFLINE? New NFL automatic trades from all cappers will pause.'))return;
 btn.disabled=true;
 try{
  const r=await fetch('/api/nfl-cappers/sport-enabled',{
   method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!online})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'NFL status update failed');
  await loadNflCapperStats();
 }catch(e){alert(String(e.message||e))}
 finally{btn.disabled=false}
}
function nflSyncPowerButton(sourceKey,enabled){
 const btn=document.getElementById('nflCapperPower-'+sourceKey);
 if(!btn)return;
 const on=enabled!==false;
 btn.dataset.enabled=on?'1':'0';
 btn.classList.toggle('active',on);
 btn.classList.toggle('offline',!on);
 btn.setAttribute('aria-pressed',on?'true':'false');
 const text=btn.querySelector('.capper-power-text');
 if(text)text.textContent=on?'Online':'Offline';
}
async function nflToggleCapper(sourceKey,btn){
 const online=btn.dataset.enabled!=='0';
 if(online&&!confirm('Turn this NFL capper OFFLINE? New automatic trades from it will pause.'))return;
 btn.disabled=true;
 try{
  const r=await fetch('/api/nfl-cappers/enabled/'+encodeURIComponent(sourceKey),{
   method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled:!online})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Capper status update failed');
  await loadNflCapperStats();
 }catch(e){alert(String(e.message||e))}
 finally{btn.disabled=false}
}
function nflRenderCappers(){
 const s=document.getElementById('nflCapperSlam'),y=document.getElementById('nflCapperSyndicate');
 if(s)s.innerHTML=nflCapperLine(nflLastCappers['Slam - NFL'],'slam');
 if(y)y.innerHTML=nflCapperLine(nflLastCappers['Syndicate - NFL'],'syndicate');
 nflSyncPowerButton('slam',(nflLastCappers['Slam - NFL']||{}).enabled);
 nflSyncPowerButton('syndicate',(nflLastCappers['Syndicate - NFL']||{}).enabled);
 capperSyncLast24hButtons();
}
window.addEventListener('capper-history-filter-change',nflRenderCappers);
function nflToggleFinished(){
 nflHideFinished=!nflHideFinished;
 localStorage.setItem('nflHideFinished',nflHideFinished?'1':'0');
 nflUpdateFinishedToggle();
 const s=document.getElementById('nflCapperSlam'),y=document.getElementById('nflCapperSyndicate');
 if(s)s.innerHTML=nflCapperLine(nflLastCappers['Slam - NFL'],'slam');
 if(y)y.innerHTML=nflCapperLine(nflLastCappers['Syndicate - NFL'],'syndicate');
}
function nflPositionList(x){
 const items=Array.isArray(x.positions)?x.positions:[];
 const open=items.filter(item=>item.sell_available);
 const settledAll=items.filter(item=>!item.sell_available&&item.finished);
 const settled=capperLast24hOnly
  ? settledAll.filter(item=>capperWithin24h(item.closed_at||item.submitted_at))
  : settledAll;
 const renderOpen=item=>{
  const raw=item.live_pnl_usdc===null||item.live_pnl_usdc===undefined?null:Number(item.live_pnl_usdc);
  const pnlClass=raw===null||raw===0?'':(raw>0?'positive':'negative');
  const pnlText=raw===null?'—':(raw>0?'+':'')+'$'+raw.toFixed(2)+(item.live_pnl_pct===null||item.live_pnl_pct===undefined?'':' ('+Number(item.live_pnl_pct).toFixed(1)+'%)');
  const entry=item.entry_price===null||item.entry_price===undefined?'—':(Number(item.entry_price)*100).toFixed(1)+'¢';
  const live=item.current_price===null||item.current_price===undefined?'—':(Number(item.current_price)*100).toFixed(1)+'¢';
  const shares=item.shares===null||item.shares===undefined?'—':Number(item.shares).toFixed(2);
  const cost=item.open_cost_basis_usdc||item.stake_usdc;
  const value=item.current_value_usdc===null||item.current_value_usdc===undefined?'—':'$'+Number(item.current_value_usdc).toFixed(2);
  const sell=item.trade_id?'<button type="button" style="margin-top:6px" data-trade-id="'+nflEsc(item.trade_id)+'" onclick="nflSellPosition(this.dataset.tradeId,this)">SELL POSITION</button>':'';
  return '<div class="nfl-position-row open"><div class="nfl-position-title">'+nflEsc(item.selection||item.market||'NFL position')+' · OPEN</div><div>'+nflEsc(item.outcome||'')+' · Stake $'+Number(cost||0).toFixed(2)+' · Shares '+shares+'</div><div>Entry '+entry+' · Live '+live+' · Value '+value+'</div><div class="nfl-position-pnl '+pnlClass+'">Live P/L '+pnlText+'</div>'+sell+'</div>';
 };
 const renderSettled=item=>{
  const result=String(item.result||'').toUpperCase();
  const awaiting=!result&&String(item.status||'').toUpperCase()==='CLOSED_RECONCILED';
  const cls=result==='WIN'?'win':result==='LOSS'?'loss':result==='PUSH'?'push':'';
  const raw=item.realized_pnl_usdc===null||item.realized_pnl_usdc===undefined?null:Number(item.realized_pnl_usdc);
  const pnl=raw===null?'':('<div class="nfl-position-pnl '+(raw>0?'positive':raw<0?'negative':'')+'">Realized P/L '+(raw>0?'+':'')+'$'+raw.toFixed(2)+'</div>');
  const stake=item.stake_usdc===null||item.stake_usdc===undefined?'':' · Stake $'+Number(item.stake_usdc).toFixed(2);
  const statusLabel=result?(' · '+nflEsc(result)):(awaiting?' · AWAITING SETTLEMENT':'');
  const manual=awaiting&&item.trade_id
   ? '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:7px"><button type="button" data-result="WIN" data-trade-id="'+nflEsc(item.trade_id)+'" onclick="nflManualSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE WIN</button><button type="button" data-result="LOSS" data-trade-id="'+nflEsc(item.trade_id)+'" onclick="nflManualSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE LOSS</button><button type="button" data-result="PUSH" data-trade-id="'+nflEsc(item.trade_id)+'" onclick="nflManualSettle(this.dataset.tradeId,this.dataset.result,this)">SETTLE PUSH</button></div>'
   : '';
  return '<div class="nfl-position-row '+cls+'"><div class="nfl-position-title">'+nflEsc(item.selection||item.market||'NFL position')+statusLabel+'</div><div>'+nflEsc(item.outcome||'')+stake+'</div>'+pnl+manual+'</div>';
 };
 let html='<div style="margin-top:9px"><span class="capper-section-badge">Open positions</span>'+(open.length?open.map(renderOpen).join(''):'<div style="margin-top:7px;opacity:.7">No open positions.</div>')+'</div>';
 if(!nflHideFinished&&(settled.length||capperLast24hOnly)){
  html+='<div style="margin-top:12px"><span class="capper-section-badge">Settled positions'+(capperLast24hOnly?' · last 24h':'')+'</span>'+(settled.length?settled.map(renderSettled).join(''):'<div style="margin-top:7px;opacity:.7">No settled positions in the last 24 hours.</div>')+'</div>';
 }
 return html;
}
async function nflSellPosition(tradeId,btn){
 if(!confirm('Sell the full tracked open position at the current executable market?'))return;
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SELLING…';
 try{
  const r=await fetch('/api/executor/request-sell/'+encodeURIComponent(tradeId),{method:'POST'});
  const q=await r.json();
  if(!r.ok)throw new Error(q.detail||'SELL request failed');
  const started=Date.now();
  while(Date.now()-started<90000){
   await new Promise(resolve=>setTimeout(resolve,1000));
   const sr=await fetch('/api/executor/request-status/'+encodeURIComponent(q.request_id),{cache:'no-store'});
   const sd=await sr.json();
   if(!sr.ok)throw new Error(sd.detail||'SELL status failed');
   if(sd.status==='DONE'){
    btn.textContent='SOLD';
    await loadNflCapperStats();
    return;
   }
   if(sd.status==='FAILED'){
    const err=String(sd.error||'SELL failed');
    if(err.includes('CLOB outcome-token balance became zero before SELL')){
     btn.textContent='SOLD';
     await loadNflCapperStats();
     alert('Position already has 0 shares on Polymarket. No second SELL was submitted; the dashboard will reconcile it as closed.');
     return;
    }
    throw new Error(err);
   }
  }
  throw new Error('SELL timed out waiting for Termux');
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function nflManualSettle(tradeId,result,btn){
 const choice=String(result||'').toUpperCase();
 if(!['WIN','LOSS','PUSH'].includes(choice))return;
 if(!confirm('Manually settle this position as '+choice+'? This writes the final result and realized P/L and cannot be undone from this button.'))return;
 const group=btn.parentElement;
 const buttons=group?Array.from(group.querySelectorAll('button')):[btn];
 buttons.forEach(b=>b.disabled=true);
 const original=btn.textContent;
 btn.textContent='SETTLING…';
 try{
  const r=await fetch('/api/dashboard/manual-settle/'+encodeURIComponent(tradeId)+'/'+encodeURIComponent(choice),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Manual settlement failed');
  btn.textContent='SETTLED '+choice;
  await loadNflCapperStats();
 }catch(e){
  buttons.forEach(b=>b.disabled=false);
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
function nflTabSpec(x){
 return [
  ['signals','Signals',Number(x.signals||0),x.all_items||[],'signals'],
  ['queued','Queued',Number(x.queued||0),x.queued_items||[],'queued'],
  ['done','Done',Number(x.done||0),x.done_items||[],'done'],
  ['failed','Failed',Number(x.failed||0),x.failed_items||[],'failed'],
  ['retrying','Retrying',Number(x.retrying||0),x.retrying_items||[],'retrying'],
  ['pregame','Pregame',Number(x.pregame||0),x.pregame_items||[],'pregame'],
  ['live','Live',Number(x.live||0),x.live_items||[],'live'],
  ['closed','Closed',Number(x.closed||0),x.closed_items||[],'closed'],
  ['unsupported','Unsupported',Number(x.unsupported||0),x.unsupported_items||[],'unsupported']
 ];
}
function nflSetTab(sourceKey,tab){
 nflActiveTabs[sourceKey]=tab;
 const x=nflLastCappers[sourceKey==='slam'?'Slam - NFL':'Syndicate - NFL'];
 const el=document.getElementById(sourceKey==='slam'?'nflCapperSlam':'nflCapperSyndicate');
 if(el&&x)el.innerHTML=nflCapperLine(x,sourceKey);
}
function nflPickList(title,items,kind){
 if(!Array.isArray(items)||!items.length)return '';
 const scoped=capperLast24hOnly&&kind==='signals'
  ? items.filter(item=>capperWithin24h(item.posted_at||item.updated_at))
  : items;
 const visible=nflHideFinished?scoped.filter(item=>String(item.event_phase||'').toUpperCase()!=='CLOSED'):scoped;
 if(!visible.length){
  if(capperLast24hOnly&&kind==='signals')return '<div style="margin-top:8px;opacity:.7">No signals in the last 24 hours.</div>';
  return '<div style="margin-top:8px;opacity:.7">'+(nflHideFinished?'Finished games hidden.':'No signals.')+'</div>';
 }
 const rows=visible.map(item=>{
  const meta=[];
  if(item.units!==null&&item.units!==undefined&&item.units!=='')meta.push(nflEsc(item.units)+'u');
  if(item.stake_usdc!==null&&item.stake_usdc!==undefined&&item.stake_usdc!=='')meta.push('risk $'+Number(item.stake_usdc).toFixed(2));
  if(item.target_profit_usdc!==null&&item.target_profit_usdc!==undefined&&item.target_profit_usdc!=='')meta.push('to win $'+Number(item.target_profit_usdc).toFixed(2));
  if(item.posted_at)meta.push('signal '+nflPickTime(item.posted_at)+(item.signal_age_seconds!==null&&item.signal_age_seconds!==undefined?' · age '+nflAge(item.signal_age_seconds):''));
  if(item.updated_at)meta.push('last update '+nflPickTime(item.updated_at));
  if(item.matchup)meta.push('Match: '+nflEsc(item.matchup));
  if(item.exact_position)meta.push('POSITION HELD: '+nflEsc(item.exact_position));
  if(item.market)meta.push('Market: '+nflEsc(item.market));
  if(item.result_event_title)meta.push('Game '+nflEsc(item.result_event_title));
  if(item.event_phase==='LIVE'){
   meta.push('GAME LIVE');
   if(item.espn_score)meta.push('ESPN '+nflEsc(item.espn_score)+(item.espn_status?' · '+nflEsc(item.espn_status):''));
  }else if(item.event_phase==='CLOSED')meta.push('GAME FINISHED');
  else if(item.event_start_at)meta.push('starts '+nflPickTime(item.event_start_at));
  if(item.final_score)meta.push('Final '+nflEsc(item.final_score));
  if(item.status)meta.push('status '+nflEsc(item.status));
  if(item.signal_decimal_odds)meta.push('odds '+Number(item.signal_decimal_odds).toFixed(2));
  if(item.approval_reason==='MIN_ODDS')meta.push('minimum odds '+Number(item.minimum_decimal_odds||1.70).toFixed(2));
  if(item.reason&&['pregame','live','closed','unsupported','failed','signals'].includes(kind))meta.push(nflEsc(item.reason));
  if(item.last_error&&['retrying','failed','signals'].includes(kind))meta.push(nflEsc(item.last_error));
  const approveAction=(String(item.status||'').toUpperCase()==='WAITING_APPROVAL'&&item.request_id)?'<button type="button" style="margin-top:6px" data-request-id="'+nflEsc(item.request_id)+'" onclick="nflApproveBuy(this.dataset.requestId,this)">APPROVE '+Number(item.signal_decimal_odds||0).toFixed(2)+'</button>':'';
  const sellAction=(item.trade_id&&item.sell_available)?'<button type="button" style="margin-top:6px" data-trade-id="'+nflEsc(item.trade_id)+'" onclick="nflSellPosition(this.dataset.tradeId,this)">SELL POSITION</button>':'';
  const marketAction=(String(item.event_phase||'').toUpperCase()!=='CLOSED'&&item.market_url&&String(item.market_url).startsWith('https://polymarket.com/'))?'<a style="display:inline-block;margin:6px 0 0 8px" target="_blank" rel="noopener noreferrer" href="'+nflEsc(item.market_url)+'">OPEN MARKET</a>':'';
  const visual=nflPhaseVisual(item);
  const badge=visual.label?'<span class="capper-event-badge" style="'+visual.badge+'">'+visual.label+'</span>':'';
  let pnlLine='';
  if(String(item.event_phase||'').toUpperCase()==='CLOSED'){
   if(item.trade_executed){
    const raw=item.trade_pnl_usdc===null||item.trade_pnl_usdc===undefined?null:Number(item.trade_pnl_usdc);
    const cls=raw===null||raw===0?'flat':(raw>0?'positive':'negative');
    const text=raw===null?'pending':(raw>0?'+':'')+'$'+raw.toFixed(2);
    pnlLine='<div class="monitor-action '+cls+'">Trade P/L '+text+'</div>';
   }else{
    pnlLine='<div class="monitor-action flat">Trade P/L — · NOT TRADED</div>';
   }
  }else if(!item.trade_executed&&!['QUEUED','WAITING_APPROVAL'].includes(String(item.status||'').toUpperCase())){
   pnlLine='<div class="monitor-action flat">NOT TRADED</div>';
  }
  const action=approveAction+sellAction+marketAction;
  return '<div class="monitor-signal"><b>'+nflEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<div class="monitor-meta">'+meta.join(' · ')+'</div>':'')+pnlLine+(action?'<div class="monitor-signal-actions">'+action+'</div>':'')+'</div>';
 }).join('');
 return '<div style="margin-top:8px"><span class="capper-section-badge">'+nflEsc(title)+'</span>'+rows+'</div>';
}
async function nflApproveBuy(requestId,btn){
 if(!confirm('Approve this below-minimum-odds BUY for execution?'))return;
 const original=btn.textContent;btn.disabled=true;btn.textContent='APPROVING…';
 try{
  const r=await fetch('/api/executor/approve-buy/'+encodeURIComponent(requestId),{method:'POST'});
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Approval failed');
  btn.textContent='APPROVED';
  await loadNflCapperStats();
 }catch(e){btn.disabled=false;btn.textContent=original;alert(String(e.message||e))}
}
async function nflSetUnitSize(sourceKey,btn){
 const input=document.getElementById('nflUnitSize-'+sourceKey);
 const value=Number(input&&input.value);
 if(!Number.isFinite(value)||value<=0){
  alert('Enter a unit size greater than 0.');
  return;
 }
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SAVING…';
 try{
  const r=await fetch('/api/nfl-cappers/unit-size/'+encodeURIComponent(sourceKey),{
   method:'PUT',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({unit_usdc:value})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Unit size update failed');
  btn.textContent='SAVED';
  await loadNflCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
async function nflSetPortfolioPct(sourceKey,btn){
 const input=document.getElementById('nflPortfolioPct-'+sourceKey);
 const value=Number(input&&input.value);
 if(!Number.isFinite(value)||value<=0||value>100){
  alert('Enter a portfolio percentage greater than 0 and no more than 100.');
  return;
 }
 const original=btn.textContent;
 btn.disabled=true;
 btn.textContent='SAVING…';
 try{
  const r=await fetch('/api/nfl-cappers/unit-percent/'+encodeURIComponent(sourceKey),{
   method:'PUT',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({portfolio_pct:value})
  });
  const d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Portfolio unit update failed');
  btn.textContent='AUTO ON';
  await loadNflCapperStats();
 }catch(e){
  btn.disabled=false;
  btn.textContent=original;
  alert(String(e.message||e));
 }
}
function nflCapperLine(x,sourceKey){
 if(!x)return 'No tracked signals yet';
 const roi=x.roi_pct===null||x.roi_pct===undefined?'—':Number(x.roi_pct).toFixed(1)+'%';
 const winPct=x.win_pct===null||x.win_pct===undefined?'—':Number(x.win_pct).toFixed(1)+'%';
 const rawPnl=x.realized_pnl_usdc===null||x.realized_pnl_usdc===undefined?null:Number(x.realized_pnl_usdc);
 const pnl=rawPnl===null?'—':(rawPnl>0?'+':'')+'$'+rawPnl.toFixed(2);
 const pnlClass=rawPnl===null||rawPnl===0?'flat':(rawPnl>0?'positive':'negative');
 const missedRaw=Number(x.missed_pnl_usdc||0);
 const missedPnl=(missedRaw>0?'+':'')+'$'+missedRaw.toFixed(2);
 const missedClass=missedRaw===0?'flat':(missedRaw>0?'positive':'negative');
 const liveRaw=Number(x.total_live_pnl_usdc||0);
 const livePnl=(liveRaw>0?'+':'')+'$'+liveRaw.toFixed(2);
 const liveClass=liveRaw===0?'flat':(liveRaw>0?'positive':'negative');
 const openValue=x.open_value_usdc===null||x.open_value_usdc===undefined?'—':'$'+Number(x.open_value_usdc).toFixed(2);
 const pnl7Raw=Number(x.realized_pnl_7d_usdc||0);
 const pnl30Raw=Number(x.realized_pnl_30d_usdc||0);
 const pnl7=(pnl7Raw>0?'+':'')+'$'+pnl7Raw.toFixed(2);
 const pnl30=(pnl30Raw>0?'+':'')+'$'+pnl30Raw.toFixed(2);
 const pnl7Class=pnl7Raw===0?'flat':(pnl7Raw>0?'positive':'negative');
 const pnl30Class=pnl30Raw===0?'flat':(pnl30Raw>0?'positive':'negative');
 const metrics='<div class="capper-metrics-grid">'
  +'<div class="capper-metric"><span>Bets</span><b>'+(x.bets||0)+'</b></div>'
  +'<div class="capper-metric"><span>Open</span><b>'+(x.open||0)+'</b></div>'
  +'<div class="capper-metric"><span>W-L-P</span><b>'+(x.wins||0)+'-'+(x.losses||0)+'-'+(x.pushes||0)+'</b></div>'
  +'<div class="capper-metric"><span>Win</span><b>'+winPct+'</b></div>'
  +'<div class="capper-metric"><span>Stake</span><b>$'+Number(x.graded_stake_usdc||0).toFixed(2)+'</b></div>'
  +'<div class="capper-metric"><span>ROI</span><b>'+roi+'</b></div>'
  +'<div class="capper-metric"><span>Realized P/L</span><b class="capper-pnl '+pnlClass+'">'+pnl+'</b></div>'
  +'<div class="capper-metric"><span>Live P/L</span><b class="capper-pnl '+liveClass+'">'+livePnl+'</b></div>'
  +'<div class="capper-metric"><span>7D P/L</span><b class="capper-pnl '+pnl7Class+'">'+pnl7+'</b></div>'
  +'<div class="capper-metric"><span>30D P/L</span><b class="capper-pnl '+pnl30Class+'">'+pnl30+'</b></div>'
  +'<div class="capper-metric"><span>Open value</span><b>'+openValue+'</b></div>'
  +'<div class="capper-metric"><span>Missed P/L</span><b class="capper-pnl '+missedClass+'">'+missedPnl+' <small>('+Number(x.missed_graded||0)+')</small></b></div>'
  +'</div>';
 const active=nflActiveTabs[sourceKey]||'signals';
 const specs=nflTabSpec(x);
 const tabs='<div class="capper-tabs">'+specs.map(s=>'<button type="button" class="'+(s[0]===active?'active':'')+'" aria-pressed="'+(s[0]===active?'true':'false')+'" onclick="nflSetTab(\''+sourceKey+'\',\''+s[0]+'\')"><span>'+nflEsc(s[1])+'</span><b>'+s[2]+'</b></button>').join('')+'</div>';
 const spec=specs.find(s=>s[0]===active)||specs[0];
 const body=(spec[3]||[]).length
  ? nflPickList(spec[1],spec[3],spec[4])
  : '<div style="margin-top:8px"><span class="capper-section-badge">'+nflEsc(spec[1])+'</span><div style="margin-top:7px;opacity:.7">No '+nflEsc(spec[1].toLowerCase())+' signals.</div></div>';
 const unitValue=Number(x.unit_usdc||10).toFixed(2);
 const fixedValue=Number(x.fixed_unit_usdc||x.unit_usdc||10).toFixed(2);
 const pctValue=Number(x.portfolio_pct||10).toFixed(2);
 const autoPct=String(x.unit_mode||'fixed')==='portfolio_pct';
 const portfolioValue=x.portfolio_value_usdc===null||x.portfolio_value_usdc===undefined?null:Number(x.portfolio_value_usdc);
 const modeText=autoPct
  ? (x.unit_error?('AUTO '+pctValue+'% · '+nflEsc(x.unit_error)):('AUTO '+pctValue+'% of $'+portfolioValue.toFixed(2)+' = 1u WIN $'+unitValue))
  : ('FIXED · 1u WIN $'+fixedValue);
 const fixedBtnStyle=autoPct?'opacity:.68':'font-weight:800;border-color:#86efac';
 const autoBtnStyle=autoPct?'font-weight:800;border-color:#86efac':'opacity:.68';
 const unitControl='<div class="capper-sizing">'
  +'<div class="capper-sizing-row"><span class="capper-sizing-label">Fixed 1u win $</span><div class="capper-sizing-controls"><button type="button" class="'+(!autoPct?'active':'')+'" aria-pressed="'+(!autoPct?'true':'false')+'" style="'+fixedBtnStyle+'" onclick="nflSetUnitSize(\''+sourceKey+'\',this)">SET 1U</button><input id="nflUnitSize-'+sourceKey+'" type="number" min="0.01" max="10000" step="0.01" value="'+fixedValue+'"></div></div>'
  +'<div class="capper-sizing-row"><span class="capper-sizing-label">Portfolio %</span><div class="capper-sizing-controls"><button type="button" class="'+(autoPct?'active':'')+'" aria-pressed="'+(autoPct?'true':'false')+'" style="'+autoBtnStyle+'" onclick="nflSetPortfolioPct(\''+sourceKey+'\',this)">AUTO %</button><input id="nflPortfolioPct-'+sourceKey+'" type="number" min="0.01" max="100" step="0.01" value="'+pctValue+'"></div></div>'
  +'<div class="capper-sizing-note">'+modeText+'<br><span>TO WIN sizing · risk varies by odds · auto recalculates before new/retried orders</span></div>'
  +'</div>';
 return unitControl+metrics+nflPositionList(x)+tabs+body;
}
async function loadNflCapperStats(){
 try{
  const r=await fetch('/api/nfl-cappers/stats',{cache:'no-store'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'NFL capper stats failed');
  const state=document.getElementById('nflCapperState'),meta=document.getElementById('nflCapperMeta');
  if(state)state.textContent=!d.enabled?'DISABLED':(!d.sport_enabled?'ENABLED · AUTO OFF':(d.auto_live?'ENABLED · AUTO LIVE':'ENABLED · AUTO OFF'));
  nflSyncSportPower(d.sport_enabled);
  if(meta)meta.textContent='Sizing: TO WIN posted units · default 1u win $'+Number(d.unit_usdc||10).toFixed(2)+' · fresh ≤ '+(d.max_pick_age_seconds||0)+'s · scan '+Math.round(Number(d.feed_window_minutes||0)/60)+'h · poll '+(d.poll_seconds||0)+'s';
  nflLastCappers=d.cappers||{};
  nflRenderCappers();
  nflUpdateFinishedToggle();
 }catch(e){
  const state=document.getElementById('nflCapperState');if(state)state.textContent='Stats unavailable: '+String(e);
 }
}
loadNflCapperStats();setInterval(loadNflCapperStats,10000);
"""
        html = html.replace("</script>", js + "\n</script>", 1)
        dashboard.DASHBOARD_HTML = html
