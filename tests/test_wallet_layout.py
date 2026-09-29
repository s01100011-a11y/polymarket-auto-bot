from pathlib import Path


def test_wallet_contains_summary_metrics_and_live_graph_is_standalone():
    source = Path("app/wallet_dashboard.py").read_text(encoding="utf-8")

    assert 'class="wallet-live-layout"' in source
    assert 'class="wallet-dashboard-row wallet-row-address"' in source
    assert 'class="wallet-dashboard-row wallet-row-balance"' in source
    assert 'class="wallet-dashboard-row wallet-row-activity"' in source
    assert 'id="walletAddress"' in source
    assert 'class="wallet-data-box wallet-mode-box"' not in source
    assert 'id="walletBalance"' in source
    assert 'id="walletPortfolio"' in source
    assert 'id="pnl"' in source
    assert 'id="budget"' in source
    assert 'id="watches"' in source
    assert 'id="liveTrades"' in source
    assert 'id="walletLiveGraphSlot"' in source
    assert 'html = html.replace(cards_html, "", 1)' in source
    assert 'cards_html.replace("Live trades", "Current trades")' in source
    assert "function moveLivePnlGraphToStandaloneWindow()" in source
    assert "document.querySelector('.pnl-chart-card')" in source
    assert "slot.appendChild(chart)" in source
    assert ".wallet-live-layout{display:grid;grid-template-columns:1fr" in source
    assert '<div class="wallet-graph-slot" id="walletLiveGraphSlot"></div>' in source


def test_mode_is_rendered_in_top_app_status_row_not_wallet():
    wallet = Path("app/wallet_dashboard.py").read_text(encoding="utf-8")
    theme = Path("app/wnba_pw_research_v13.py").read_text(encoding="utf-8")

    assert 'class="wallet-data-box wallet-mode-box"' not in wallet
    assert 'class="top-mode-status-row capper-panel-status-row"' in theme
    assert '<div class="capper-status-label">MODE</div>' in theme
    assert 'class="nfl-capper-state top-mode-state" id="mode"' in theme
    assert 'class="badge capper-power-btn sport-power-btn" id="botPowerBtn"' in theme


def test_live_graph_slot_is_outside_wallet_layout():
    source = Path("app/wallet_dashboard.py").read_text(encoding="utf-8")
    wallet = source.split("wallet_html = '''", 1)[1].split("'''", 1)[0]

    layout_start = wallet.index('<div class="wallet-live-layout">')
    graph_slot = wallet.index('<div class="wallet-graph-slot" id="walletLiveGraphSlot"></div>')
    layout_close = wallet.rfind('</div>', layout_start, graph_slot)
    assert layout_close < graph_slot
    assert "moveLivePnlGraphToStandaloneWindow()" in source
