from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from app import attribution_core as attribution
from app import dashboard_metrics_v3 as metrics
from app import nfl_capper_ingest as nfl
from app import sh01_account_reconcile as reconcile_mod
from app import universal_position_identity as identity

_EPS = Decimal("0.0001")
_OU_RE = re.compile(r"\bO\s*/\s*U\s*([0-9]+(?:\.[0-9]+)?)\b", re.I)
_TOTAL_RE = re.compile(r"\b(OVER|UNDER)\s*([0-9]+(?:\.[0-9]+)?)\b", re.I)
_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED"}
_SETTLED = {"SETTLED_WIN", "SETTLED_LOSS", "SETTLED_PUSH"}


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _asset_id(rec: dict[str, Any]) -> str:
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    return str(quote.get("asset_id") or "")


def _is_synthetic(rec: dict[str, Any]) -> bool:
    return str(rec.get("source") or "") == "polymarket_account_reconcile"


def _is_executor_test(rec: dict[str, Any]) -> bool:
    rid = str(rec.get("id") or "").casefold()
    if rid.startswith("live-test-remote-"):
        return True
    if rec.get("test") is True or rec.get("is_test") is True:
        return True
    text = " ".join(
        str(x or "")
        for x in (
            rec.get("note"),
            (rec.get("intent") or {}).get("note") if isinstance(rec.get("intent"), dict) else None,
        )
    ).casefold()
    return "this is a test alert" in text


def _sport_from_record(rec: dict[str, Any]) -> str | None:
    direct = str(rec.get("strategy_sport") or "").strip().upper()
    if direct:
        return direct
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    text = " ".join(
        str(x or "")
        for x in (quote.get("market_url"), quote.get("market"), rec.get("market_url"))
    ).casefold()
    for needle, sport in (
        ("/wnba/", "WNBA"), ("wnba-", "WNBA"),
        ("/nba/", "NBA"), ("nba-", "NBA"),
        ("/cfb/", "CFB"), ("cfb-", "CFB"),
        ("/nfl/", "NFL"), ("nfl-", "NFL"),
        ("/mlb/", "MLB"), ("mlb-", "MLB"),
    ):
        if needle in text:
            return sport
    return None


def _preprocess_legacy_records(executions: dict[str, Any]) -> int:
    """Tag executor tests and restore old live-monitor trades that predate strategy metadata."""
    changed = 0
    for key, rec in executions.items():
        if not isinstance(rec, dict) or rec.get("parent_trade_id"):
            continue
        if _is_executor_test(rec):
            if rec.get("stats_excluded") is not True:
                rec["stats_excluded"] = True
                changed += 1
            if rec.get("stats_excluded_reason") != "executor_test_trade":
                rec["stats_excluded_reason"] = "executor_test_trade"
                changed += 1
            executions[key] = rec
            continue

        # Historical live Slack trades from the basketball monitor were created
        # before strategy_* attribution fields were persisted. Restore those
        # records to the monitor rather than treating them as manual SH01 bets.
        if str(rec.get("source") or "") == "slack_live" and rec.get("slack_event_id"):
            sport = _sport_from_record(rec)
            if sport in {"WNBA", "NBA"}:
                label = f"{sport} Monitor - {sport}"
                if str(rec.get("strategy_source") or "") != label:
                    rec["strategy_source"] = label
                    changed += 1
                if str(rec.get("original_strategy_source") or "") != label:
                    rec["original_strategy_source"] = label
                    changed += 1
                if rec.get("strategy_sport") != sport:
                    rec["strategy_sport"] = sport
                    changed += 1
                if not rec.get("strategy_pick_id"):
                    rec["strategy_pick_id"] = str(rec.get("slack_event_id"))
                    changed += 1
                quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
                if not rec.get("strategy_selection"):
                    rec["strategy_selection"] = quote.get("resolved_outcome") or quote.get("requested_outcome")
                    changed += 1
                if not rec.get("strategy_units"):
                    rec["strategy_units"] = "1"
                    changed += 1
                if not rec.get("strategy_unit_usdc"):
                    basis = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
                    if basis > 0:
                        rec["strategy_unit_usdc"] = str(basis)
                        changed += 1
                if rec.get("stats_excluded"):
                    rec.pop("stats_excluded", None)
                    rec.pop("stats_excluded_reason", None)
                    changed += 1
                executions[key] = rec
    return changed


def _tracked_asset_shares(executions: dict[str, Any]) -> dict[str, Decimal]:
    """Shares already explained by real executions, including resolved/unredeemed positions."""
    known: dict[str, Decimal] = {}
    for rec in executions.values():
        if not isinstance(rec, dict) or rec.get("parent_trade_id") or rec.get("paper") or _is_synthetic(rec):
            continue
        asset = _asset_id(rec)
        if not asset:
            continue
        status = str(rec.get("status") or "").upper()
        shares = Decimal("0")
        if status in _ACTIVE:
            shares = _d(rec.get("remaining_shares") if rec.get("remaining_shares") is not None else rec.get("filled_shares") or (rec.get("quote") or {}).get("shares"))
        elif status in _SETTLED:
            # A market-resolution settlement sets remaining_shares to zero even
            # though terminal CTF shares can remain visible until redemption.
            settlement = rec.get("settlement") if isinstance(rec.get("settlement"), dict) else {}
            if settlement or rec.get("exit_price") in {"0", "1", 0, 1}:
                shares = _d(rec.get("filled_shares") or (rec.get("quote") or {}).get("shares"))
        if shares > _EPS:
            known[asset] = known.get(asset, Decimal("0")) + shares
    return known


def _reprice_synthetic(rec: dict[str, Any], unmatched: Decimal) -> None:
    quote = rec.get("quote") if isinstance(rec.get("quote"), dict) else {}
    avg = _d(quote.get("entry_price") or quote.get("limit_price"))
    if avg > 0:
        cost = unmatched * avg
    else:
        old_shares = _d(rec.get("filled_shares"))
        old_cost = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
        cost = old_cost * unmatched / old_shares if old_shares > 0 else Decimal("0")
    rec["filled_shares"] = str(unmatched)
    if str(rec.get("status") or "").upper() in _ACTIVE:
        rec["remaining_shares"] = str(unmatched)
    rec["actual_cost_usdc"] = str(cost) if cost > 0 else None
    rec["budget_usdc"] = str(cost) if cost > 0 else None
    quote["shares"] = str(unmatched)
    rec["quote"] = quote
    status = str(rec.get("status") or "").upper()
    if status in _SETTLED:
        result = str((rec.get("settlement") or {}).get("result") or "").upper()
        if result == "WIN":
            rec["realized_pnl"] = str(unmatched - cost)
        elif result == "LOSS":
            rec["realized_pnl"] = str(-cost)
        elif result == "PUSH":
            rec["realized_pnl"] = "0"


def _mark_duplicate(rec: dict[str, Any], stamp: str, reason: str) -> None:
    if not rec.get("reconciled_previous_status"):
        rec["reconciled_previous_status"] = rec.get("status")
    if rec.get("realized_pnl") is not None and rec.get("reconciled_previous_realized_pnl") is None:
        rec["reconciled_previous_realized_pnl"] = rec.get("realized_pnl")
    rec["status"] = "RECONCILED_DUPLICATE"
    rec["remaining_shares"] = "0"
    rec["stats_excluded"] = True
    rec["stats_excluded_reason"] = reason
    rec["wallet_reconciled_at"] = stamp
    rec["reconciliation_note"] = "Synthetic SH01 wallet row duplicates shares already explained by a tracked execution; excluded from SH01 stats/history."


def _cleanup_historical_synthetics(executions: dict[str, Any], known: dict[str, Decimal], stamp: str) -> int:
    changed = 0
    for key, rec in executions.items():
        if not isinstance(rec, dict) or not _is_synthetic(rec):
            continue
        asset = _asset_id(rec)
        snapshot = _d(rec.get("wallet_position_size"))
        tracked = known.get(asset, Decimal("0"))
        if not asset or snapshot <= _EPS or tracked <= _EPS:
            continue
        expected = max(Decimal("0"), snapshot - tracked)
        current = _d(rec.get("filled_shares"))
        if expected <= _EPS:
            if rec.get("stats_excluded_reason") != "duplicate_of_tracked_execution":
                _mark_duplicate(rec, stamp, "duplicate_of_tracked_execution")
                changed += 1
        elif current > expected + _EPS:
            _reprice_synthetic(rec, expected)
            rec.pop("stats_excluded", None)
            rec.pop("stats_excluded_reason", None)
            rec["reconciliation_note"] = "SH01 shares reduced to the residual wallet exposure after subtracting tracked executions."
            changed += 1
        executions[key] = rec
    return changed


# Polymarket full-game totals are commonly titled "Team A vs Team B: O/U 42.5".
# The older parser only understood literal "Under 42.5"/"Over 42.5" text.
def _total_identity_v4(rec: dict[str, Any], quote: dict[str, Any] | None = None) -> tuple[str | None, Decimal | None]:
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
    texts = (
        str(rec.get("strategy_alternate_line") or ""),
        str(rec.get("strategy_execution_selection") or ""),
        str(q.get("market") or ""),
    )
    for text in texts:
        match = _TOTAL_RE.search(text)
        if match:
            try:
                return side or match.group(1).upper(), Decimal(match.group(2))
            except Exception:
                pass
        ou = _OU_RE.search(text)
        if ou:
            try:
                return side or None, Decimal(ou.group(1))
            except Exception:
                pass
    return side or None, None


identity.total_identity = _total_identity_v4


def _requested_total_scope(rec: dict[str, Any]) -> str:
    direct = str(rec.get("strategy_total_scope") or "").casefold()
    if "team" in direct:
        return "team_total"
    if direct:
        return "game_total"
    selection = str(rec.get("strategy_selection") or "")
    low = selection.casefold()
    if "team total" in low:
        return "team_total"
    if re.search(r"\b(?:vs?\.?|versus)\b|/", low):
        return "game_total"
    match = _TOTAL_RE.search(selection)
    if match:
        prefix = selection[: match.start()].strip(" :-")
        suffix = selection[match.end() :]
        if prefix and re.search(r"\bpoints?\b", suffix, re.I):
            return "team_total"
    return "game_total"


def _executed_total_scope(rec: dict[str, Any]) -> str:
    kind = identity.market_kind(rec)
    if kind == "team_total":
        return "team_total"
    market = str((rec.get("quote") or {}).get("market") or "").casefold()
    if "team total" in market or "team points" in market:
        return "team_total"
    return "game_total"


def _attribution_v4(rec: dict[str, Any]) -> tuple[str, str]:
    if _is_synthetic(rec):
        return attribution.SH01, "manual_or_account"
    current_source = str(rec.get("strategy_source") or "").strip()
    source = str(rec.get("original_strategy_source") or "").strip() or current_source
    if current_source.upper() == attribution.SH01 and not str(rec.get("original_strategy_source") or "").strip():
        return attribution.SH01, "manual_or_account"
    link_id = str(rec.get("strategy_pick_id") or rec.get("slack_event_id") or "").strip()
    if not source or not link_id:
        return attribution.SH01, "no_linked_original_call"

    kind = identity.market_kind(rec)
    if kind == "spread":
        requested = identity.requested_spread(rec)
        executed = identity.effective_spread(rec)
        if requested is None or executed is None:
            return attribution.SH01, "spread_line_unverifiable"
        if abs(requested - executed) > attribution.LINE_TOLERANCE_POINTS:
            return attribution.SH01, f"spread_changed:{identity.fmt_signed(requested)}->{identity.fmt_signed(executed)}"
        if requested != executed:
            return source, f"spread_within_1pt:{identity.fmt_signed(requested)}->{identity.fmt_signed(executed)}"
        return source, "exact_original_call"

    if kind in {"total", "team_total"}:
        requested_scope = _requested_total_scope(rec)
        executed_scope = _executed_total_scope(rec)
        if requested_scope != executed_scope:
            return attribution.SH01, f"total_scope_changed:{requested_scope}->{executed_scope}"
        req_side, requested = identity.requested_total(rec)
        exe_side, executed = identity.total_identity(rec)
        if requested is None or executed is None:
            return attribution.SH01, "total_line_unverifiable"
        if req_side and exe_side and req_side != exe_side:
            return attribution.SH01, "total_side_changed_from_original_call"
        if abs(requested - executed) > attribution.LINE_TOLERANCE_POINTS:
            return attribution.SH01, f"total_changed:{requested}->{executed}"
        if requested != executed:
            return source, f"total_within_1pt:{requested}->{executed}"
        return source, "exact_original_call"

    if kind == "moneyline":
        return (source, "exact_original_call") if identity.moneyline_matches(rec) else (attribution.SH01, "moneyline_outcome_unverifiable")
    return attribution.SH01, f"unsupported_market_type:{kind}"


attribution.attribution_for_execution = _attribution_v4

_ORIGINAL_REPAIR = attribution.repair_records


def _repair_records_v4(core: Any) -> dict[str, int]:
    executions = core._load(core.EXECUTIONS_FILE)
    pre_changed = 0
    if isinstance(executions, dict):
        pre_changed += _preprocess_legacy_records(executions)
        stamp = metrics._now_iso()
        pre_changed += _cleanup_historical_synthetics(executions, _tracked_asset_shares(executions), stamp)
        if pre_changed:
            core._save(core.EXECUTIONS_FILE, executions)
    result = _ORIGINAL_REPAIR(core)
    if isinstance(result, dict):
        result["changed"] = int(result.get("changed") or 0) + pre_changed
    return result


attribution.repair_records = _repair_records_v4

# Excluded audit/test/duplicate rows stay in executions.json for traceability but
# are invisible to capper cards, positions, More Stats, and unit P/L.
_ORIGINAL_NFL_STATS = nfl._stats_from_executions
_ORIGINAL_NFL_POSITIONS = nfl._position_items
_ORIGINAL_UNIT_SUMMARY = attribution.unit_summary
_ORIGINAL_LEGACY_IDENTITY = metrics._legacy_monitor_identity


def _filtered_execs(executions: dict[str, Any]) -> dict[str, Any]:
    return {
        key: rec
        for key, rec in executions.items()
        if not (isinstance(rec, dict) and rec.get("stats_excluded"))
    }


def _stats_filtered(executions: dict[str, Any], *args: Any, **kwargs: Any):
    return _ORIGINAL_NFL_STATS(_filtered_execs(executions), *args, **kwargs)


def _positions_filtered(executions: dict[str, Any], *args: Any, **kwargs: Any):
    return _ORIGINAL_NFL_POSITIONS(_filtered_execs(executions), *args, **kwargs)


def _unit_summary_filtered(records: list[dict[str, Any]], marks: dict[str, dict[str, Any]], **kwargs: Any):
    clean = [rec for rec in records if not (isinstance(rec, dict) and rec.get("stats_excluded"))]
    return _ORIGINAL_UNIT_SUMMARY(clean, marks, **kwargs)


def _legacy_identity_filtered(rec: dict[str, Any]):
    if isinstance(rec, dict) and rec.get("stats_excluded"):
        return None, None
    return _ORIGINAL_LEGACY_IDENTITY(rec)


nfl._stats_from_executions = _stats_filtered
nfl._position_items = _positions_filtered
attribution.unit_summary = _unit_summary_filtered
metrics._legacy_monitor_identity = _legacy_identity_filtered


def _reconcile_v4(core: Any) -> dict[str, Any]:
    with reconcile_mod._LOCK:
        wallet = metrics._wallet_for_reconciliation()
        if not wallet:
            return {"ok": False, "reason": "wallet unavailable"}
        try:
            positions = reconcile_mod._current_wallet_positions(wallet)
        except Exception as exc:
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}

        attribution.repair_records(core)
        executions = core._load(core.EXECUTIONS_FILE)
        if not isinstance(executions, dict):
            executions = {}
        _preprocess_legacy_records(executions)
        known = _tracked_asset_shares(executions)
        stamp = metrics._now_iso()
        changed = _cleanup_historical_synthetics(executions, known, stamp)
        imported = preserved_settled = duplicates_removed = 0
        seen: set[str] = set()

        for asset_id, pos in positions.items():
            wallet_size = _d(getattr(pos, "current_size", "0"))
            tracked = known.get(str(asset_id), Decimal("0"))
            unmatched = max(Decimal("0"), wallet_size - tracked)
            rec_id = "sh01-account-" + __import__("hashlib").sha256(str(asset_id).encode("utf-8")).hexdigest()[:18]
            seen.add(rec_id)
            existing = executions.get(rec_id) if isinstance(executions.get(rec_id), dict) else None

            if unmatched <= _EPS:
                if existing and existing.get("stats_excluded_reason") != "duplicate_of_tracked_execution":
                    _mark_duplicate(existing, stamp, "duplicate_of_tracked_execution")
                    executions[rec_id] = existing
                    changed += 1
                    duplicates_removed += 1
                continue

            avg_price = _d(getattr(pos, "avg_price", "0"))
            full_entry_cost = _d(getattr(pos, "entry_cost_usdc", "0"))
            if avg_price > 0:
                entry_cost = unmatched * avg_price
            elif wallet_size > 0 and full_entry_cost > 0:
                entry_cost = full_entry_cost * unmatched / wallet_size
            else:
                entry_cost = Decimal("0")
            title = reconcile_mod._text(pos, "title", "market_title", "question") or f"Polymarket asset {str(asset_id)[:12]}…"
            outcome = reconcile_mod._text(pos, "outcome", "outcome_name", "name")
            event_slug = reconcile_mod._text(pos, "event_slug", "slug", "market_slug")
            market_url = f"https://polymarket.com/event/{event_slug}" if event_slug else None
            sport = reconcile_mod._infer_sport(pos)
            market_type = reconcile_mod._infer_market_type(pos, title)

            rec = dict(existing or {})
            was_settled = str(rec.get("status") or "").upper() in _SETTLED
            rec.update({
                "id": rec_id,
                "created_at": rec.get("created_at") or stamp,
                "submitted_at": rec.get("submitted_at") or stamp,
                "updated_at": stamp,
                "source": "polymarket_account_reconcile",
                "paper": False,
                "auto": False,
                "strategy_source": attribution.SH01,
                "attribution_source": attribution.SH01,
                "attribution_reason": "unmatched_polymarket_account_position",
                "strategy_sport": sport,
                "wallet_reconciled_at": stamp,
                "wallet_position_size": str(wallet_size),
                "account_reconcile_note": "Only the residual wallet shares not explained by tracked executions are attributed to SH01.",
                "quote": {
                    "market": title,
                    "market_url": market_url,
                    "market_type": market_type,
                    "requested_outcome": outcome or None,
                    "resolved_outcome": outcome or None,
                    "asset_id": str(asset_id),
                    "entry_price": str(avg_price) if avg_price > 0 else None,
                    "limit_price": str(avg_price) if avg_price > 0 else None,
                    "shares": str(unmatched),
                },
            })
            rec.pop("stats_excluded", None)
            rec.pop("stats_excluded_reason", None)
            if not was_settled:
                rec["status"] = "ORDER_SUBMITTED"
                rec["remaining_shares"] = str(unmatched)
            rec["filled_shares"] = str(unmatched)
            rec["actual_cost_usdc"] = str(entry_cost) if entry_cost > 0 else None
            rec["budget_usdc"] = str(entry_cost) if entry_cost > 0 else None
            rec["canonical_exact_position"] = identity.canonical_exact_position(rec)
            if was_settled:
                _reprice_synthetic(rec, unmatched)
                preserved_settled += 1
            executions[rec_id] = rec
            changed += 1
            imported += 1

        for rec_id, rec in list(executions.items()):
            if not isinstance(rec, dict) or not str(rec_id).startswith("sh01-account-"):
                continue
            if rec_id in seen or rec.get("stats_excluded") or str(rec.get("status") or "").upper() not in _ACTIVE:
                continue
            rec["status"] = "CLOSED_RECONCILED"
            rec["remaining_shares"] = "0"
            rec["closed_at"] = rec.get("closed_at") or stamp
            rec["wallet_reconciled_at"] = stamp
            rec["reconciliation_note"] = "Unmatched Polymarket holding is no longer present. Exit price and realized P/L remain unknown unless recorded by the bot."
            executions[rec_id] = rec
            changed += 1

        if changed:
            core._save(core.EXECUTIONS_FILE, executions)
        return {
            "ok": True,
            "positions_checked": len(positions),
            "imported_or_updated_sh01": imported,
            "preserved_settled_sh01": preserved_settled,
            "duplicate_synthetics_removed": duplicates_removed,
            "changed": changed,
        }


reconcile_mod.reconcile = _reconcile_v4
