from pathlib import Path


def test_dashboard_is_renamed_and_win95_theme_is_final_layer():
    source = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert "S01-807 · POLYMARKET SPORTS DESK" in source
    assert "<title>S01-807 · Polymarket</title>" in source
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
