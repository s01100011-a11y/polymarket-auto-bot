from pathlib import Path


def test_dashboard_stats_default_is_live_and_open_trades_remain_both():
    stats_source = Path("app/dashboard_filters_v5.py").read_text(encoding="utf-8")
    pnl_source = Path("app/dashboard_pnl_filters_v6.py").read_text(encoding="utf-8")

    assert "localStorage.getItem('dashboardStatsFilter')||'live'" in stats_source
    assert "localStorage.getItem('dashboardTradesFilter')||'both'" in stats_source
    assert "localStorage.getItem('dashboardStatsFilter')||'live'" in pnl_source
    assert "Portfolio P/L · LIVE" in pnl_source
