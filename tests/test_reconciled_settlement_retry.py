from pathlib import Path


def test_reconciled_closed_positions_remain_settlement_candidates():
    source = Path("app/slack_ingest.py").read_text(encoding="utf-8")

    assert 'r.get("status") == "CLOSED_RECONCILED"' in source
    assert 'r.get("realized_pnl") is None' in source
    assert 'not (r.get("settlement") or {}).get("result")' in source


def test_nfl_settled_card_shows_pending_settlement_status():
    source = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")

    assert "AWAITING SETTLEMENT" in source
