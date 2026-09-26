from pathlib import Path


def test_dashboard_stats_default_is_live_and_open_trades_remain_both():
    stats_source = Path("app/dashboard_filters_v5.py").read_text(encoding="utf-8")
    pnl_source = Path("app/dashboard_pnl_filters_v6.py").read_text(encoding="utf-8")

    assert "dashboardStatsDefaultVersion='live-v1'" in stats_source
    assert "localStorage.setItem('dashboardStatsFilter','live')" in stats_source
    assert "localStorage.getItem('dashboardStatsFilter')||'live'" in stats_source
    assert "localStorage.getItem('dashboardTradesFilter')||'both'" in stats_source
    assert "localStorage.getItem('dashboardStatsFilter')||'live'" in pnl_source
    assert "Portfolio P/L · LIVE" in pnl_source


def test_top_stats_include_portfolio_value_and_total_missed_pnl():
    metrics_source = Path("app/dashboard_metrics_v3.py").read_text(encoding="utf-8")
    stats_source = Path("app/dashboard_filters_v5.py").read_text(encoding="utf-8")

    assert 'id="performancePortfolio"' in metrics_source
    assert 'id="performanceMissedPnl"' in metrics_source
    assert 'id="performanceMissedCount"' in metrics_source
    assert "Portfolio value" in metrics_source
    assert "Missed P/L · total" in metrics_source

    assert "def _portfolio_value()" in stats_source
    assert 'state.get("usdc_balance")' in stats_source
    assert 'state.get("portfolio_value")' in stats_source
    assert "def _total_missed_pnl(" in stats_source
    assert '"nfl_capper_signals.json"' in stats_source
    assert '"cfb_capper_preview_signals.json"' in stats_source
    assert '"portfolio_value_usdc": _portfolio_value()' in stats_source
    assert '"missed_graded_total": total_missed["missed_graded"]' in stats_source
    assert '"missed_pnl_total_usdc": total_missed["missed_pnl_usdc"]' in stats_source
    assert "graded missed calls · NFL + CFB" in stats_source
