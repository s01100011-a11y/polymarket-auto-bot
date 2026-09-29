from pathlib import Path


def test_reconciled_closed_positions_remain_settlement_candidates():
    source = Path("app/slack_ingest.py").read_text(encoding="utf-8")

    assert 'r.get("status") == "CLOSED_RECONCILED"' in source
    assert 'r.get("realized_pnl") is None' in source
    assert 'not (r.get("settlement") or {}).get("result")' in source


def test_nfl_settled_card_shows_pending_settlement_status():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "AWAITING SETTLEMENT" in source


def test_manual_settlement_endpoint_is_limited_to_unresolved_reconciled_live_trades():
    source = Path("app/slack_ingest.py").read_text(encoding="utf-8")

    assert '/api/dashboard/manual-settle/{trade_id}/{result}' in source
    assert 'str(rec.get("status") or "") != "CLOSED_RECONCILED"' in source
    assert 'result not in {"WIN", "LOSS", "PUSH"}' in source
    assert '"source": "dashboard_manual"' in source
    assert 'rec["realized_pnl"]' in source


def test_nfl_unresolved_settlement_card_has_manual_controls():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "SETTLE WIN" in source
    assert "SETTLE LOSS" in source
    assert "SETTLE PUSH" in source
    assert "nflManualSettle" in source
    assert "/api/dashboard/manual-settle/" in source


def test_shared_last_24h_toggle_filters_nfl_and_future_capper_panels():
    metrics = Path("app/dashboard_metrics_v3.py").read_text(encoding="utf-8")
    nfl = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "capperLast24hOnly" in metrics
    assert "capperWithin24h" in metrics
    assert "capperToggleLast24h" in metrics
    assert "capper-history-filter-change" in metrics
    assert "data-capper-last24h-toggle" in nfl
    assert "capperWithin24h(item.closed_at||item.submitted_at)" in nfl
    assert "capperLast24hOnly&&kind==='signals'" in nfl
    assert "No settled positions in the last 24 hours." in nfl
    assert "No signals in the last 24 hours." in nfl
