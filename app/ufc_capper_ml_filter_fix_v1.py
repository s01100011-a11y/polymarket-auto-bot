from __future__ import annotations

import re
from typing import Any

from app import ufc_current_capper_bets_v1 as capper


def _selection_text(row: dict[str, Any]) -> str:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    # Use the actual side/selection first. The row subject commonly contains the
    # full matchup (both fighters), which is useful for attaching a pick to a
    # fight but must not be used to decide whether a pick is a single-fighter ML.
    for value in (
        data.get("selection"),
        data.get("bet"),
        data.get("pick"),
        data.get("fighter"),
        data.get("fighter_name"),
        row.get("selection"),
        row.get("outcome"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return str(row.get("subject") or "").strip()


def _fighters_in_text(text: str, fighters: tuple[str, str]) -> list[str]:
    hay = capper._compact(text)
    matched: list[str] = []
    for fighter in fighters:
        if any(alias in hay for alias in capper._aliases(fighter)):
            matched.append(fighter)
    return matched


def _is_straight_ml_selection_first(row: dict[str, Any], fighters: tuple[str, str]) -> bool:
    data = row.get("data") if isinstance(row.get("data"), dict) else {}
    bet_type_raw = str(data.get("bet_type") or row.get("market") or "").strip().casefold()
    bet_type = capper._compact(bet_type_raw)
    selection = _selection_text(row).casefold()
    selection_compact = capper._compact(selection)

    words = set(re.findall(r"[a-z0-9]+", selection))
    if any(term in selection for term in ("parlay", "combo", "double chance", "inside the distance")):
        return False
    if words.intersection({
        "prop", "over", "under", "round", "rounds", "method", "decision", "points",
        "ko", "tko", "submission", "sub", "distance", "finish", "finishes", "draw",
    }):
        return False
    if any(term in bet_type for term in (
        "parlay", "combo", "prop", "total", "round", "method", "decision", "ko", "tko",
        "submission", "spread",
    )):
        return False

    # Critical fix: determine the chosen fighter from the selection only, not the
    # matchup/subject. A subject such as "Johnny Walker vs Mick Parkin" contains
    # both fighters and caused every clean ML pick on that fight to be rejected.
    matched = _fighters_in_text(selection, fighters)
    if len(matched) != 1:
        return False

    if bet_type in {"ml", "moneyline", "straightml", "straightmoneyline", "winner", "fightwinner", "towin", "straight"}:
        return True
    if "moneyline" in bet_type or bet_type.endswith("ml"):
        return True

    fighter = matched[0]
    aliases = capper._aliases(fighter)
    stripped = re.sub(r"\b(moneyline|ml|to win|winner|win|straight)\b", " ", selection)
    stripped_compact = capper._compact(stripped)
    if stripped_compact in aliases or selection_compact in aliases:
        return True
    if any(
        alias and selection_compact in {
            alias + "ml", alias + "moneyline", alias + "towin", alias + "win", alias + "winner"
        }
        for alias in aliases
    ):
        return True

    # Explicit ML text on a single-fighter selection is enough even when the
    # upstream sheet left bet_type blank.
    if re.search(r"(?:^|\s)(?:ml|moneyline)(?:\s|$)", selection):
        return True
    return False


capper._is_straight_ml = _is_straight_ml_selection_first
