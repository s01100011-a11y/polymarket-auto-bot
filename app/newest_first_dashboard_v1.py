from __future__ import annotations

from typing import Any, Iterable

from app import nfl_capper_ingest as nfl


def _time_key(item: dict[str, Any]) -> str:
    """Best available dashboard timestamp, newest-first friendly for ISO strings."""
    return str(
        item.get("closed_at")
        or item.get("completed_at")
        or item.get("updated_at")
        or item.get("submitted_at")
        or item.get("posted_at")
        or item.get("received_at")
        or item.get("created_at")
        or ""
    )


def newest_first(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [row for row in rows if isinstance(row, dict)],
        key=_time_key,
        reverse=True,
    )


# nfl._position_items is the shared position formatter used by NFL, CFB,
# basketball monitor cards and SH01 sport cards. Sorting here makes Open and
# Settled position sections consistently newest -> oldest everywhere.
_ORIGINAL_POSITION_ITEMS = nfl._position_items


def _position_items_newest_first(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
    return newest_first(_ORIGINAL_POSITION_ITEMS(*args, **kwargs))


nfl._position_items = _position_items_newest_first


# CFB signal/status helpers historically depend on input insertion order. Sort
# the source rows before they apply their limits so the newest rows cannot be
# pushed out by older records, and sort the rendered result again as a guard.
try:
    from app import cfb_capper_preview as cfb

    for _name in ("_recent_status_items", "_recent_all_items"):
        _original = getattr(cfb, _name, None)
        if not callable(_original):
            continue

        def _make_wrapper(original):
            def _wrapped(rows, *args, **kwargs):
                ordered = newest_first(rows if isinstance(rows, list) else list(rows or []))
                result = original(ordered, *args, **kwargs)
                return newest_first(result if isinstance(result, list) else [])

            return _wrapped

        setattr(cfb, _name, _make_wrapper(_original))
except Exception:
    # The dashboard can run without the optional CFB preview module. NFL/shared
    # position ordering remains active even if CFB is unavailable.
    pass
