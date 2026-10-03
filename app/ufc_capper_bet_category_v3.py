from __future__ import annotations

import re
import unicodedata
from typing import Any

from app import ufc_current_capper_bets_v1 as capper


def _ascii_lower(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return text.encode("ascii", "ignore").decode("ascii").casefold().strip()


def _compact(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _ascii_lower(value))


def _selection_text(row: dict[str, Any]) -> str:
    """Use the wager selection itself; matchup/subject is only a last resort."""
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    for key in ("selection", "pick", "bet", "fighter", "outcome"):
        value = data.get(key)
        if str(value or "").strip():
            return str(value).strip()
    return str(row.get("subject") or "").strip()


def _fighters_in_text(text: str, fighters: tuple[str, str]) -> list[str]:
    hay = _compact(text)
    out: list[str] = []
    for fighter in fighters:
        aliases = capper._aliases(fighter)
        if any(alias and alias in hay for alias in aliases):
            out.append(fighter)
    return out


def _clean_fighter_ml(text: str, fighters: tuple[str, str]) -> bool:
    t = _ascii_lower(text)
    compact = _compact(t)
    matched = _fighters_in_text(text, fighters)
    if len(matched) != 1:
        return False
    fighter = matched[0]
    aliases = capper._aliases(fighter)
    # Remove normal outright-winner vocabulary and common odds decorations.
    stripped = re.sub(r"\b(moneyline|ml|to\s+win|winner|win|straight)\b", " ", t)
    stripped = re.sub(r"(?:^|\s)[+-]\d{2,4}(?:\s|$)", " ", stripped)
    stripped = re.sub(r"(?:^|\s)\d+(?:\.\d+)?(?:\s|$)", " ", stripped)
    stripped_compact = _compact(stripped)
    return stripped_compact in aliases or compact in aliases or any(
        compact in {a + "ml", a + "towin", a + "win", a + "winner"}
        for a in aliases
    )


def _category(row: dict[str, Any], fighters: tuple[str, str]) -> str:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    raw_type = _ascii_lower(data.get("bet_type") or row.get("market") or data.get("market_type") or "")
    type_compact = _compact(raw_type)
    selection = _selection_text(row)
    text = _ascii_lower(selection)
    words = set(re.findall(r"[a-z0-9]+", text))

    # Explicit structured type first.
    if any(x in type_compact for x in ("parlay", "combo", "multibet", "accumulator")):
        return "PARLAY"
    if any(x in type_compact for x in ("round", "roundbet")):
        return "ROUND"
    if any(x in type_compact for x in ("method", "decision", "ko", "tko", "submission", "inside", "distance")):
        return "METHOD"
    if any(x in type_compact for x in ("total", "overunder", "ou")):
        return "TOTAL"
    if any(x in type_compact for x in ("moneyline", "straightml", "fightwinner", "winner", "towin")) or type_compact == "ml":
        return "ML"
    if "prop" in type_compact:
        return "PROP"

    # Then infer from actual wager text, not the matchup title.
    if any(term in text for term in ("parlay", "combo", "accumulator", "multi bet", "same game")):
        return "PARLAY"
    if words.intersection({"round", "rounds"}) or re.search(r"\br\s*\d+\b", text):
        return "ROUND"
    if words.intersection({"ko", "tko", "submission", "sub", "decision", "method"}) or "inside the distance" in text or "goes the distance" in text:
        return "METHOD"
    if words.intersection({"over", "under"}) or "total" in words:
        return "TOTAL"
    if "prop" in words:
        return "PROP"
    if re.search(r"(?:^|\s)(?:ml|moneyline)(?:\s|$)", text) or _clean_fighter_ml(selection, fighters):
        return "ML"

    return "OTHER"


def _normalize_pick(row: dict[str, Any], fighters: tuple[str, str]) -> dict[str, Any]:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    value = row.get("value") if isinstance(row.get("value"), dict) else {}
    selection = _selection_text(row)
    category = _category(row, fighters)
    matched_selection = _fighters_in_text(selection, fighters)
    matched_fight = capper._matched_fighters(row, fighters)
    return {
        "id": row.get("id"),
        "capper": data.get("capper") or row.get("source") or "Unknown capper",
        "bet": selection or data.get("bet") or row.get("subject") or "UFC pick",
        "bet_type": category,
        "bet_type_raw": data.get("bet_type") or row.get("market") or data.get("market_type") or "",
        "bet_category": category,
        "book": data.get("book") or "",
        "units": value.get("units"),
        "odds_decimal": value.get("odds_decimal"),
        "odds_american": value.get("odds_american"),
        "wager_usd": value.get("wager_usd"),
        "to_win_usd": value.get("to_win_usd"),
        "status": value.get("result") or data.get("status") or "Pending",
        "bet_placed_date": data.get("bet_placed_date") or row.get("observed_at"),
        "matched_fighters": matched_selection or matched_fight,
        "is_straight_ml": category == "ML",
        "source": row.get("source") or "Audit DB",
        "retrieved_at": row.get("retrieved_at"),
    }


# The existing endpoint calls this module-global function at request time, so patching
# it here upgrades classification without adding another API or polling path.
capper._normalize_pick = _normalize_pick
capper._CACHE.clear()
