from pathlib import Path


def test_pw_export_tags_wnba_and_nba_monitor_sources():
    source = Path("app/pw_export_ingest.py").read_text(encoding="utf-8")

    assert '"pw_export_sport": sport_key' in source
    assert '"monitor_sport": sport_key' in source
    assert '"monitor_source": monitor_source' in source
    assert 'event_id = f"pwexport-{sport_key.lower()}-{rec_id}"' in source


def test_monitor_trades_are_tagged_for_execution_stats():
    paper = Path("app/slack_ingest.py").read_text(encoding="utf-8")
    live = Path("app/dashboard_live_control_v4.py").read_text(encoding="utf-8")

    assert '"strategy_source": f"{monitor_source} - {monitor_sport}" if monitor_sport else None' in paper
    assert '"strategy_sport": monitor_sport or None' in paper
    assert '"strategy_source": f"{monitor_source} - {monitor_sport}" if monitor_sport else None' in live
    assert '"strategy_sport": monitor_sport or None' in live


def test_dashboard_has_wnba_and_nba_monitor_panels_with_shared_24h_filter():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")
    entry = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert '"WNBA Monitor - WNBA"' in source
    assert '"NBA Monitor - NBA"' in source
    assert '"/api/basketball-monitor/status"' in source
    assert "data-capper-last24h-toggle" in source
    assert "capperLast24hOnly" in source
    assert "capperWithin24h" in source
    assert "Open positions" in source
    assert "Settled positions" in source
    assert "Signals" in source
    assert "basketball_monitor_capper.install(" in entry


def test_more_stats_infers_legacy_pw_export_monitor_trades():
    source = Path("app/dashboard_metrics_v3.py").read_text(encoding="utf-8")

    assert "def _legacy_monitor_identity" in source
    assert 'event_id.startswith("pwexport-")' in source
    assert 'f"{league} Monitor - {league}"' in source


def test_basketball_monitor_has_independent_to_win_unit_controls():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")
    live = Path("app/dashboard_live_control_v4.py").read_text(encoding="utf-8")

    assert '"/api/basketball-monitor/unit-size/{sport_key}"' in source
    assert '"/api/basketball-monitor/unit-percent/{sport_key}"' in source
    assert "FIXED 1u WIN $" in source
    assert "SET 1U" in source
    assert "AUTO %" in source
    assert "sizing is TO WIN" in source
    assert "_monitor_stake_to_win" in live
    assert '"strategy_units": "1" if monitor_sport else None' in live
    assert '"strategy_unit_usdc": str(unit_usdc) if unit_usdc is not None else None' in live
    assert '"sizing_mode": "TO_WIN" if monitor_sport else "STAKE"' in live


def test_basketball_monitors_have_persistent_online_controls_and_execution_gate():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")
    live = Path("app/dashboard_live_control_v4.py").read_text(encoding="utf-8")

    assert '"/api/basketball-monitor/enabled/{sport_key}"' in source
    assert "monitorToggleCapper" in source
    assert '<div class="capper-panel-title">WNBA AUTO-TRADING</div>' in source
    assert '<div class="capper-panel-title">NBA AUTO-TRADING</div>' in source
    assert 'id="wnbaMonitorState"' in source
    assert 'id="nbaMonitorState"' in source
    assert 'id="monitorCapperPower-wnba"' in source
    assert 'id="monitorCapperPower-nba"' in source
    assert "capper_control.is_enabled(core, monitor_label)" in live
    assert 'raise ValueError(f"{monitor_label} is OFFLINE")' in live


def test_basketball_sections_use_the_same_badge_format():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert '<span class="capper-section-badge">Open positions</span>' in source
    assert '<span class="capper-section-badge">Settled positions' in source
    assert '<span class="capper-section-badge">Signals' in source
    assert "Signals signals" not in source


def test_basketball_sizing_controls_match_nfl_cfb_layout():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert '<span class="capper-sizing-label">Fixed 1u win $</span>' in source
    assert '<span class="capper-sizing-label">Portfolio %</span>' in source
    fixed = source.index('onclick="monitorSetUnitSize')
    fixed_input = source.index('id="monitorUnitSize-')
    auto = source.index('onclick="monitorSetPortfolioPct')
    auto_input = source.index('id="monitorPortfolioPct-')
    assert fixed < fixed_input
    assert auto < auto_input
    assert '<span>$</span>' not in source
    assert '<span>%</span>' not in source


def test_basketball_dashboard_counts_distinct_submitted_pw_calls():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert "def _submitted_pw_call_counts" in source
    assert 'submission.get("order_id")' in source
    assert 'payload.get("strategy_pick_id") or payload.get("slack_event_id")' in source
    assert '"pw_calls": len(submitted_at_by_signal)' in source
    assert '"pw_calls_24h": last_24h' in source
    assert "<span>PW Calls</span>" in source
    assert "visiblePwCalls=capperLast24hOnly" in source


def test_legacy_slack_pw_execution_is_inferred_into_wnba_monitor():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert 'source != "slack_live" and not event_id.startswith("pwexport-")' in source
    assert 'quote.get("requested_outcome")' in source
    assert 'ingest._league_for_team' in source


def test_pw_retry_hydrates_monitor_metadata_before_requeue():
    source = Path("app/dashboard_live_control_v4.py").read_text(encoding="utf-8")

    assert "def _hydrate_monitor_retry_payload" in source
    assert 'hydrated["strategy_sport"] = sport' in source
    assert 'f"{sport} Monitor - {sport}"' in source
    assert 'hydrated["strategy_pick_id"]' in source
    assert 'hydrated["strategy_selection"]' in source
    assert "payload = _hydrate_monitor_retry_payload" in source


def test_basketball_open_positions_show_stake_and_shares():
    source = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert "const stake=item.stake_usdc" in source
    assert "const shares=item.shares" in source
    assert "' · Stake '+stake+' · Shares '+shares" in source
