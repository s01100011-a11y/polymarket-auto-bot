from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

_SPREAD_RE = re.compile(r"(?:^|\bspread\s*:\s*)(?P<team>.+?)\s*\(?(?P<line>[+-]\d+(?:\.\d+)?)\)?(?:\s|$)", re.I)
_SIGNED_RE = re.compile(r"(?<!\d)([+-]\d+(?:\.\d+)?)(?!\d)")
_TOTAL_RE = re.compile(r"\b(OVER|UNDER)\s*([0-9]+(?:\.[0-9]+)?)\b", re.I)


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _same_team(a: Any, b: Any) -> bool:
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    aliases = {"pitt": "pittsburgh", "pittsburghpanthers": "pittsburgh", "pittsburgh": "pittsburgh"}
    return aliases.get(na, na) == aliases.get(nb, nb)


def _fmt(value: Decimal) -> str:
    raw = format(value.normalize(), "f")
    return raw.rstrip("0").rstrip(".") if "." in raw else raw


def fmt_signed(value: Decimal) -> str:
    return ("+" if value >= 0 else "-") + _fmt(abs(value))


def market_kind(rec: dict[str, Any], quote: dict[str, Any] | None = None) -> str:
    q = quote if isinstance(quote, dict) else (rec.get("quote") or {})
    raw = str(rec.get("market_type") or rec.get("strategy_market_type") or q.get("market_type") or "").casefold()
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    if "teamtotal" in compact:
        return "team_total"
    if "spread" in compact or compact in {"handicap", "line"}:
        return "spread"
    if "total" in compact or compact in {"overunder", "ou"}:
        return "total"
    if "moneyline" in compact or compact in {"ml", "h2h", "headtohead", "winner", "matchwinner"}:
        return "moneyline"
    return compact or "unknown"


def effective_spread(rec: dict[str, Any], quote: dict[str, Any] | None = None) -> Decimal | None:
    q = quote if isinstance(quote, dict) else (rec.get("quote") or {})
    if market_kind(rec, q) != "spread":
        return None
    market = str(q.get("market") or "").strip()
    outcome = str(q.get("resolved_outcome") or q.get("requested_outcome") or rec.get("outcome") or "").strip()
    if market and outcome:
        match = _SPREAD_RE.search(market)
        if match:
            try:
                line = Decimal(str(match.group("line")))
            except Exception:
                line = None
            if line is not None:
                named_team = str(match.group("team") or "").strip(" :-()")
                if _same_team(named_team, outcome):
                    return line
                if _norm(outcome) not in {"yes", "no"}:
                    return -line
    stored = rec.get("strategy_executed_spread_line")
    if stored not in {None, ""}:
        try:
            return Decimal(str(stored))
        except Exception:
            pass
    match = _SIGNED_RE.search(str(rec.get("strategy_alternate_line") or ""))
    if match:
        try:
            return Decimal(match.group(1))
        except Exception:
            pass
    return None


def total_identity(rec: dict[str, Any], quote: dict[str, Any] | None = None) -> tuple[str | None, Decimal | None]:
    q = quote if isinstance(quote, dict) else (rec.get("quote") or {})
    side = str(q.get("resolved_outcome") or q.get("requested_outcome") or "").upper().strip()
    if side not in {"OVER", "UNDER"}:
        side = ""
    direct = rec.get("strategy_executed_total_line")
    if direct not in {None, ""}:
        try:
            return side or None, Decimal(str(direct))
        except Exception:
            pass
    for text in (str(rec.get("strategy_alternate_line") or ""), str(rec.get("strategy_execution_selection") or ""), str(q.get("market") or "")):
        match = _TOTAL_RE.search(text)
        if not match:
            continue
        try:
            return side or match.group(1).upper(), Decimal(match.group(2))
        except Exception:
            continue
    return side or None, None


def canonical_exact_position(rec: dict[str, Any], quote: dict[str, Any] | None = None) -> str | None:
    q = quote if isinstance(quote, dict) else (rec.get("quote") or {})
    kind = market_kind(rec, q)
    outcome = str(q.get("resolved_outcome") or q.get("requested_outcome") or rec.get("outcome") or "").strip()
    if kind == "spread":
        line = effective_spread(rec, q)
        if outcome and line is not None:
            return f"{outcome} {fmt_signed(line)}"
    if kind in {"total", "team_total"}:
        side, line = total_identity(rec, q)
        if line is not None:
            return f"{(side or outcome or 'Total').title()} {_fmt(line)}".strip()
    if kind == "moneyline" and outcome:
        return outcome
    return outcome or str(q.get("market") or "").strip() or None


def requested_spread(rec: dict[str, Any]) -> Decimal | None:
    direct = rec.get("strategy_requested_spread_line")
    if direct not in {None, ""}:
        try:
            return Decimal(str(direct))
        except Exception:
            pass
    match = _SIGNED_RE.search(str(rec.get("strategy_selection") or ""))
    if match:
        try:
            return Decimal(match.group(1))
        except Exception:
            pass
    return None


def requested_total(rec: dict[str, Any]) -> tuple[str | None, Decimal | None]:
    selection = str(rec.get("strategy_selection") or "")
    match = _TOTAL_RE.search(selection)
    side = match.group(1).upper() if match else None
    direct = rec.get("strategy_requested_total_line")
    if direct not in {None, ""}:
        try:
            return side, Decimal(str(direct))
        except Exception:
            pass
    if match:
        try:
            return side, Decimal(match.group(2))
        except Exception:
            pass
    return side, None


def moneyline_matches(rec: dict[str, Any]) -> bool:
    q = rec.get("quote") or {}
    selection = str(rec.get("strategy_selection") or "")
    cleaned = re.sub(r"\b(?:ML|MONEY\s*LINE|MONEYLINE|TO\s+WIN)\b", "", selection, flags=re.I).strip(" -")
    requested = str(q.get("requested_outcome") or "")
    resolved = str(q.get("resolved_outcome") or "")
    return bool((requested and (_same_team(cleaned, requested) or _same_team(selection, requested))) or (resolved and (_same_team(cleaned, resolved) or _same_team(selection, resolved))) or (requested and resolved and _same_team(requested, resolved)))
