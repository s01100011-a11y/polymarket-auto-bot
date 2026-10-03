from __future__ import annotations

from pathlib import Path


APPROVAL_TTL_DEFAULT_SECONDS = 3600


def _patch_executor_approval_expiry() -> None:
    path = Path(__file__).with_name("termux_executor_dashboard.py")
    source = path.read_text(encoding="utf-8")

    ttl_marker = "EXECUTOR_APPROVAL_TTL_SECONDS"
    if ttl_marker not in source:
        anchor = ")\nMAX_QUEUE_ITEMS = 500\nMIN_DECIMAL_ODDS = Decimal(os.getenv(\"MIN_DECIMAL_ODDS\", \"1.70\"))\n"
        replacement = (
            ")\n"
            "EXECUTOR_APPROVAL_TTL_SECONDS = max(\n"
            "    EXECUTOR_BUY_TTL_SECONDS,\n"
            "    int(os.getenv(\"EXECUTOR_APPROVAL_TTL_SECONDS\", \"3600\")),\n"
            ")\n"
            "MAX_QUEUE_ITEMS = 500\n"
            "MIN_DECIMAL_ODDS = Decimal(os.getenv(\"MIN_DECIMAL_ODDS\", \"1.70\"))\n"
        )
        if anchor not in source:
            raise RuntimeError("Executor approval TTL constant anchor not found")
        source = source.replace(anchor, replacement, 1)

    old_status = (
        '        status = str(rec.get("status") or "")\n'
        '        if status not in {"PENDING", "LEASED"}:\n'
        '            continue\n\n'
        '        created = float(rec.get("created_unix") or 0)\n'
        '        if not created or current - created <= EXECUTOR_BUY_TTL_SECONDS:\n'
        '            continue\n'
    )
    new_status = (
        '        status = str(rec.get("status") or "")\n'
        '        if status not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:\n'
        '            continue\n\n'
        '        created = float(rec.get("created_unix") or 0)\n'
        '        ttl_seconds = (\n'
        '            EXECUTOR_APPROVAL_TTL_SECONDS\n'
        '            if status == "WAITING_APPROVAL"\n'
        '            else EXECUTOR_BUY_TTL_SECONDS\n'
        '        )\n'
        '        if not created or current - created <= ttl_seconds:\n'
        '            continue\n'
    )
    if old_status in source:
        source = source.replace(old_status, new_status, 1)
    elif 'if status not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:' not in source:
        raise RuntimeError("Executor stale BUY status anchor not found")

    old_error = (
        '        if status == "PENDING":\n'
        '            rec["error"] = (\n'
        '                f"BUY expired after {EXECUTOR_BUY_TTL_SECONDS}s before executor pickup; "\n'
        '                "no order was submitted"\n'
        '            )\n'
        '        else:\n'
        '            rec["error"] = (\n'
        '                f"BUY executor lease expired after {EXECUTOR_BUY_TTL_SECONDS}s without a result; "\n'
        '                "automatic late retry was blocked. Reconcile the wallet before retrying"\n'
        '            )\n'
    )
    new_error = (
        '        if status == "WAITING_APPROVAL":\n'
        '            rec["error"] = (\n'
        '                f"BUY approval expired after {EXECUTOR_APPROVAL_TTL_SECONDS}s; "\n'
        '                "no order was submitted and its reserved daily budget was released"\n'
        '            )\n'
        '        elif status == "PENDING":\n'
        '            rec["error"] = (\n'
        '                f"BUY expired after {EXECUTOR_BUY_TTL_SECONDS}s before executor pickup; "\n'
        '                "no order was submitted"\n'
        '            )\n'
        '        else:\n'
        '            rec["error"] = (\n'
        '                f"BUY executor lease expired after {EXECUTOR_BUY_TTL_SECONDS}s without a result; "\n'
        '                "automatic late retry was blocked. Reconcile the wallet before retrying"\n'
        '            )\n'
    )
    if old_error in source:
        source = source.replace(old_error, new_error, 1)
    elif 'BUY approval expired after {EXECUTOR_APPROVAL_TTL_SECONDS}s' not in source:
        raise RuntimeError("Executor stale BUY error anchor not found")

    path.write_text(source, encoding="utf-8")
    print(
        "STALE_APPROVAL_EXECUTOR_PATCH ready "
        f"default_ttl={APPROVAL_TTL_DEFAULT_SECONDS}s",
        flush=True,
    )


def _patch_pending_budget_cleanup() -> None:
    path = Path(__file__).with_name("nfl_capper_ingest.py")
    source = path.read_text(encoding="utf-8")
    marker = "remote._expire_stale_buys_persisted()\n    except Exception:\n        pass\n    try:\n        queue = remote._queue_load()"
    if marker in source:
        print("STALE_APPROVAL_BUDGET_PATCH already_present", flush=True)
        return

    anchor = (
        'def _pending_auto_budget(remote: Any) -> Decimal:\n'
        '    total = Decimal("0")\n'
        '    try:\n'
        '        queue = remote._queue_load()\n'
    )
    replacement = (
        'def _pending_auto_budget(remote: Any) -> Decimal:\n'
        '    total = Decimal("0")\n'
        '    # Release stale approval reservations before computing the daily guard.\n'
        '    try:\n'
        '        remote._expire_stale_buys_persisted()\n'
        '    except Exception:\n'
        '        pass\n'
        '    try:\n'
        '        queue = remote._queue_load()\n'
    )
    if anchor not in source:
        raise RuntimeError("Pending auto budget cleanup anchor not found")
    path.write_text(source.replace(anchor, replacement, 1), encoding="utf-8")
    print("STALE_APPROVAL_BUDGET_PATCH applied", flush=True)


def main() -> None:
    _patch_executor_approval_expiry()
    _patch_pending_budget_cleanup()


if __name__ == "__main__":
    main()
