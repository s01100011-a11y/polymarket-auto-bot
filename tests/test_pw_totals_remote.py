from app import pw_totals_remote as mod


def test_american_to_decimal():
    assert mod._american_to_decimal(-110) == 1 + 100 / 110
    assert mod._american_to_decimal(+150) == 2.5
    assert mod._american_to_decimal(None) is None


def test_first_per_game_and_min_odds():
    rows = [
        {"game_id": "1", "call_ts": "2026-05-01T01:00:00Z", "pace_edge_vs_live_total": 8, "total_over_price": -110, "over_result": "W"},
        {"game_id": "1", "call_ts": "2026-05-01T01:05:00Z", "pace_edge_vs_live_total": 10, "total_over_price": -110, "over_result": "L"},
        {"game_id": "2", "call_ts": "2026-05-02T01:00:00Z", "pace_edge_vs_live_total": 9, "total_over_price": -200, "over_result": "W"},
    ]
    bets = mod._candidate_bets(rows, 8, 1.70, True)
    assert len(bets) == 1
    assert bets[0]["game_id"] == "1"


def test_to_win_one_exact_units():
    bets = [
        {"game_id": "1", "decimal_odds": 2.0, "result": "W"},
        {"game_id": "2", "decimal_odds": 1.5, "result": "L"},
    ]
    summary = mod._summary(bets)
    assert summary["units_risked"] == 3.0
    assert summary["units_won"] == -1.0
    assert summary["roi_pct"] == -33.33


def test_analysis_has_thresholds_and_holdouts():
    rows = []
    for i in range(10):
        rows.append({
            "game_id": str(i),
            "call_ts": f"2026-05-{i+1:02d}T01:00:00Z",
            "pace_edge_vs_live_total": 8 + i / 10,
            "total_over_price": -110,
            "over_result": "W" if i < 6 else "L",
        })
    result = mod.analyze_to_win_one(rows, thresholds=(8.0,), min_decimal_odds=1.70)
    block = result["thresholds"]["8+"]
    assert block["summary"]["bets"] == 10
    assert block["holdout_70_30"]["test"]["bets"] == 3
    assert block["holdout_80_20"]["test"]["bets"] == 2


def test_same_monitor_url_preserves_origin_only():
    url = mod._same_monitor_url("https://example.test:8445/api/pw-export", "/api/research/totals/calls")
    assert url == "https://example.test:8445/api/research/totals/calls"
