from __future__ import annotations

import json
from pathlib import Path


def _patch_poller_scope() -> None:
    path = Path(__file__).with_name("cfb_capper_preview.py")
    source = path.read_text(encoding="utf-8")
    anchor = '    async def _poll_once() -> None:\n        _STATUS["last_poll_at"] = _now_iso()\n'
    replacement = '    async def _poll_once() -> None:\n        nonlocal manual_fallback_picks\n        _STATUS["last_poll_at"] = _now_iso()\n'
    if anchor in source:
        path.write_text(source.replace(anchor, replacement, 1), encoding="utf-8")
        print("CFB_RUNTIME_SCOPE_PATCH applied", flush=True)
    elif "        nonlocal manual_fallback_picks\n" in source:
        print("CFB_RUNTIME_SCOPE_PATCH already_present", flush=True)
    else:
        raise RuntimeError("CFB poller scope patch anchor not found")


def _patch_immediate_signal_persistence() -> None:
    """Persist each CFB signal update before later rows can abort the poll cycle.

    The CFB worker historically saved the full signal dictionary only after every
    bridge pick had been processed.  A later exception could therefore discard an
    earlier successfully matched signal even though its CFB_CAPPER_* log line had
    already been emitted.  Save immediately after each per-pick state mutation so
    the dashboard reflects what the worker has actually processed.
    """
    path = Path(__file__).with_name("cfb_capper_preview.py")
    source = path.read_text(encoding="utf-8")
    needle = '                signals[fp] = record\n                changed = True\n'
    replacement = (
        '                signals[fp] = record\n'
        '                changed = True\n'
        '                _save_signals(signals)\n'
    )
    already = '                changed = True\n                _save_signals(signals)\n'
    if already in source:
        print("CFB_RUNTIME_IMMEDIATE_SAVE_PATCH already_present", flush=True)
        return
    count = source.count(needle)
    if count < 1:
        raise RuntimeError("CFB immediate-save patch anchor not found")
    path.write_text(source.replace(needle, replacement), encoding="utf-8")
    print(f"CFB_RUNTIME_IMMEDIATE_SAVE_PATCH applied replacements={count}", flush=True)


def main() -> None:
    _patch_poller_scope()
    _patch_immediate_signal_persistence()

    # Import after the source repairs so the module compiles with the fixed poller.
    from app import cfb_live_options_bootstrap as boot

    cfb = boot.cfb
    core = boot.composite.core
    signal_path = core.DATA_DIR / "cfb_capper_preview_signals.json"
    signals = core._load(signal_path)
    if not isinstance(signals, dict):
        signals = {}

    print(
        "CFB_RUNTIME_SIGNAL_STORE "
        + json.dumps({"path": str(signal_path), "exists": signal_path.exists(), "count": len(signals)}),
        flush=True,
    )

    changed = False
    found = 0
    for signal_id, record in signals.items():
        if not isinstance(record, dict):
            continue
        pick = record.get("pick") if isinstance(record.get("pick"), dict) else {}
        selection = str(record.get("selection") or pick.get("selection") or "").strip()
        team_hint = str(pick.get("team_hint") or "").strip()
        source = str(record.get("source") or pick.get("source") or "").strip()

        is_pitt = selection.upper().startswith("PITT +3") or team_hint.upper() == "PITT"
        is_slam = "SLAM" in source.upper()
        if not (is_pitt and is_slam):
            continue

        found += 1
        print(
            "CFB_PITT_FOUND "
            + json.dumps(
                {
                    "id": signal_id,
                    "selection": selection,
                    "team_hint": team_hint,
                    "source": source,
                    "status": record.get("status"),
                }
            ),
            flush=True,
        )

        try:
            did_change = cfb._refresh_unmatched_record(record)
            if did_change:
                record["updated_at"] = cfb._now_iso()
                changed = True
            print(
                "CFB_PITT_FORCE_REFRESH "
                + json.dumps(
                    {
                        "id": signal_id,
                        "status": record.get("status"),
                        "match_status": record.get("match_status"),
                        "event_title": record.get("event_title"),
                        "event_phase": record.get("event_phase"),
                        "alternatives": [
                            {
                                "line": alt.get("spread_line"),
                                "price": alt.get("best_ask"),
                                "odds": alt.get("live_odds_american"),
                                "relative": alt.get("relative_to_original"),
                            }
                            for alt in (record.get("live_alternatives") or [])
                        ],
                        "error": record.get("alternate_error") or record.get("match_error"),
                    },
                    default=str,
                ),
                flush=True,
            )
        except Exception as exc:
            print(
                f"CFB_PITT_FORCE_REFRESH_ERROR {type(exc).__name__}: {exc}",
                flush=True,
            )

    if changed:
        core._save(signal_path, signals)
    print(f"CFB_RUNTIME_PITT_REFRESH_DONE found={found} changed={changed}", flush=True)


if __name__ == "__main__":
    main()
