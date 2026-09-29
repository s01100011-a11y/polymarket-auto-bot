from pathlib import Path


def test_dashboard_is_renamed_and_win95_theme_is_final_layer():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "S01-807 · POLYMARKET SPORTS DESK" not in source
    assert "<title>s01807.exe</title>" in source
    assert "s01807-win95-theme" in source
    assert 'background:#008080' in source
    assert 'background:linear-gradient(90deg,#000080' in source
    assert '"MS Sans Serif",Tahoma,Arial,sans-serif' in source
    assert "border-top:2px solid #fff" in source
    assert "border-right:2px solid #404040" in source
    assert "_install_s01807_win95_theme()" in source


def test_theme_keeps_dark_data_areas_and_mobile_layout():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert ".pnl-chart-card{" in source
    assert "background:#000!important" in source
    assert ".nfl-capper-card,.more-stats-row{" in source
    assert "@media(max-width:900px)" in source


def test_win95_app_chrome_has_one_line_title_and_fake_window_controls():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "<title>s01807.exe</title>" in source
    assert "win95-titlebar" in source
    assert 'id="win95Title">S01807 v—</span>' in source
    assert "win95-logo-red" in source
    assert "win95-logo-green" in source
    assert "win95-logo-blue" in source
    assert "win95-logo-yellow" in source
    assert "win95-minimize" in source
    assert "win95-maximize" in source
    assert "win95-close" in source
    assert "win95-info-strip" in source
    assert 'id="versionLabel"' in source
    assert 'id="versionDateLabel"' in source
    assert 'id="updated"' in source
    assert 'id="serviceState"' in source


def test_major_dashboard_windows_show_teal_desktop_between_sections():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert ".wrap{" in source
    assert "background:transparent" in source
    assert ".wallet-live-layout{gap:10px" in source
    assert "win95-stats-window" in source
    assert '>WALLET</span>' in source
    assert ">STATS</span>" in source
    assert "s01807InstallSectionWindows" in source
    assert "s01807MiniControls" in source


def test_dopewars_inspired_data_typography_and_palette():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "Dope Wars-inspired retro data typography + palette" in source
    assert '"Lucida Console","Courier New",monospace' in source
    assert "--retro-green:#00ff2a" in source
    assert "--retro-red:#ff1616" in source
    assert "--retro-yellow:#ffff2e" in source
    assert "font-variant-numeric:tabular-nums" in source
    assert ".nfl-position-row.win .nfl-position-title" in source
    assert ".nfl-position-row.loss .nfl-position-title" in source
    assert ".monitor-signal{" in source


def test_top_title_power_control_and_dark_data_surfaces():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")
    dashboard_source = Path("app/dashboard.py").read_text(encoding="utf-8")

    assert 'id="win95Title">S01807 v—</span>' in source
    assert 'id="botPowerBtn"' in source
    assert '<span class="versionchip" id="versionLabel">' not in source.split("new_top = '''", 1)[1].split("'''", 1)[0]
    assert "background:#000!important" in source
    assert 'button.active,button[aria-pressed="true"]' in source
    assert "--retro-gray:#ffffff" in source
    assert '"/api/dashboard/bot-enabled"' in dashboard_source
    assert "win95Title.textContent='S01807 v'+(s.version||'—')" in dashboard_source


def test_windows_use_grey_bodies_with_separated_black_data_boxes():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "Win95 grey window bodies with inset black data boxes" in source
    assert ".wallet-strip,.panel,.nfl-capper-panel,.more-stats-panel,.pnl-chart-card{" in source
    assert "background:#c0c0c0!important" in source
    assert ".wallet-data-box,.performance-card,.nfl-capper-card,.more-stats-row" in source
    assert "background:#000!important" in source
    assert ".wallet-dashboard-row{gap:7px!important" in source


def test_capper_cards_use_clean_responsive_layout():
    theme = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")
    nfl = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")
    cfb = Path("app/cfb_capper_preview.py").read_text(encoding="utf-8")
    basketball = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert ".capper-sizing{" in theme
    assert ".capper-metrics-grid{" in theme
    assert ".capper-tabs{" in theme
    assert "grid-template-columns:repeat(2,minmax(0,1fr))" in theme
    assert "grid-template-columns:repeat(3,minmax(0,1fr))" in theme
    assert 'class="capper-sizing"' in nfl
    assert 'class="capper-metrics-grid"' in nfl
    assert 'class="capper-tabs"' in nfl
    assert 'class="capper-sizing"' in cfb
    assert 'class="capper-metrics-grid"' in cfb
    assert 'class="capper-tabs"' in cfb
    assert 'class="capper-sizing"' in basketball
    assert 'class="capper-metrics-grid"' in basketball


def test_pw_and_legacy_panels_follow_main_win95_theme():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "Normalize legacy PW/research/control panels to the Win95 dashboard system" in source
    assert ".slack-mode-box{" in source
    assert ".pw-strategy-card{" in source
    assert ".pw-strategy-metric{" in source
    assert ".pw-filter-table-wrap{" in source
    assert ".pw-filter-final{" in source
    assert ".live-test-warning,.live-test-lock,.live-test-result{" in source
    assert "background:#c0c0c0!important" in source
    assert "background:#000!important" in source
    assert "color:#fff!important" in source
    assert "var(--retro-green)" in source
    assert "var(--retro-red)" in source


def test_sport_title_bars_and_status_rows_use_independent_power_controls():
    theme = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")
    nfl = Path("app/nfl_capper_ingest.py").read_text(encoding="utf-8")
    cfb = Path("app/cfb_capper_preview.py").read_text(encoding="utf-8")
    basketball = Path("app/basketball_monitor_capper.py").read_text(encoding="utf-8")

    assert ".capper-panel-status-row" in theme
    assert ".sport-power-btn" in theme
    assert ".capper-power-btn.offline .dot" in theme

    assert '<div class="capper-panel-title">NFL AUTO-TRADING</div>' in nfl
    assert 'id="nflCapperState"' in nfl
    assert 'id="nflSportPower"' in nfl
    assert 'id="nflCapperPower-slam"' in nfl
    assert 'id="nflCapperPower-syndicate"' in nfl

    assert '<div class="capper-panel-title">CFB AUTO-TRADING</div>' in cfb
    assert 'id="cfbCapperState"' in cfb
    assert 'id="cfbSportPower"' in cfb
    assert 'id="cfbCapperPower-slam"' in cfb
    assert 'id="cfbCapperPower-syndicate"' in cfb

    assert '<div class="capper-panel-title">WNBA AUTO-TRADING</div>' in basketball
    assert '<div class="capper-panel-title">NBA AUTO-TRADING</div>' in basketball
    assert 'id="wnbaMonitorState"' in basketball
    assert 'id="nbaMonitorState"' in basketball
    assert 'id="monitorCapperPower-wnba"' in basketball
    assert 'id="monitorCapperPower-nba"' in basketball
