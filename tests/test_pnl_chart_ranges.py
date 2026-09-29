from pathlib import Path


def test_pnl_history_api_supports_requested_ranges():
    source = Path("app/slack_dashboard_v2.py").read_text(encoding="utf-8")

    assert '"/api/dashboard/pnl-history"' in source
    assert '{"1d", "1w", "1m", "1y", "ytd", "all"}' in source
    assert 'timedelta(days=1)' in source
    assert 'timedelta(days=7)' in source
    assert 'timedelta(days=30)' in source
    assert 'timedelta(days=365)' in source
    assert 'datetime(now.year, 1, 1, tzinfo=timezone.utc)' in source


def test_pnl_chart_has_functional_range_buttons_and_value_in_body():
    source = Path("app/slack_dashboard_v2.py").read_text(encoding="utf-8")

    for label in ("1D", "1W", "1M", "1Y", "YTD", "ALL"):
        assert f">{label}</button>" in source

    assert 'class="pnl-chart-head"><div class="label">Portfolio P/L · LIVE</div>' in source
    assert 'class="pnl-chart-body"' in source
    assert '<div class="label">Live P/L</div><div class="pnl-chart-value" id="pnlChartValue">' in source
    assert "refreshPnlChartRange" in source
    assert "localStorage.setItem('pnlChartRangeKey'" in source


def test_long_term_pnl_archive_is_compact_and_live_point_is_not_lost():
    source = Path("app/slack_dashboard_v2.py").read_text(encoding="utf-8")

    assert "PNL_SAMPLE_SECONDS = 300" in source
    assert "PNL_HISTORY_MAX_ENTRIES = 210240" in source
    assert "_LATEST_PNL_POINT" in source
    assert "PNL_RANGE_MAX_POINTS = 1200" in source
    assert "_downsample_points" in source


def test_filtered_pnl_range_is_mode_aware_and_does_not_overwrite_selected_range():
    source = Path("app/dashboard_pnl_filters_v6.py").read_text(encoding="utf-8")
    chart = Path("app/slack_dashboard_v2.py").read_text(encoding="utf-8")

    assert '"/api/dashboard/pnl-history-mode"' in source
    assert 'MODE_PNL_SAMPLE_SECONDS = 300' in source
    assert 'MODE_PNL_HISTORY_MAX_ENTRIES = 210240' in source
    assert "refreshPnlChartRange" in source
    assert "renderPnlChart(p.history||[])" not in source
    assert "/api/dashboard/pnl-history-mode?mode=" in chart
    assert "localStorage.getItem('dashboardStatsFilter')||'live'" in chart
