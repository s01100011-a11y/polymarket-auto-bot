from pathlib import Path


def test_wallet_contains_summary_metrics_and_live_graph_moves_right():
    source = Path("app/wallet_dashboard.py").read_text(encoding="utf-8")

    assert 'class="wallet-live-layout"' in source
    assert 'class="wallet-dashboard-row wallet-row-address"' in source
    assert 'class="wallet-dashboard-row wallet-row-balance"' in source
    assert 'class="wallet-dashboard-row wallet-row-activity"' in source
    assert 'id="walletAddress"' in source
    assert 'id="mode"' in source
    assert 'id="walletBalance"' in source
    assert 'id="walletPortfolio"' in source
    assert 'id="pnl"' in source
    assert 'id="budget"' in source
    assert 'id="watches"' in source
    assert 'id="liveTrades"' in source
    assert 'id="walletLiveGraphSlot"' in source
    assert 'html = html.replace(cards_html, "", 1)' in source
    assert 'cards_html.replace("Live trades", "Current trades")' in source
    assert "function moveLivePnlGraphIntoWallet()" in source
    assert "document.querySelector('.pnl-chart-card')" in source
    assert "slot.appendChild(chart)" in source
    assert "grid-template-columns:minmax(430px,1fr) minmax(0,1.35fr)" in source
    assert "@media(max-width:1050px){.wallet-live-layout{grid-template-columns:1fr}" in source
