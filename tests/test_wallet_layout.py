from pathlib import Path


def test_wallet_metrics_stack_under_wallet_and_live_graph_moves_right():
    source = Path("app/wallet_dashboard.py").read_text(encoding="utf-8")

    assert 'class="wallet-live-layout"' in source
    assert 'class="wallet-metrics-stack"' in source
    assert 'id="walletBalance"' in source
    assert 'id="walletPortfolio"' in source
    assert 'id="walletLiveGraphSlot"' in source
    assert "function moveLivePnlGraphIntoWallet()" in source
    assert "document.querySelector('.pnl-chart-card')" in source
    assert "slot.appendChild(chart)" in source
    assert "grid-template-columns:minmax(300px,.85fr) minmax(0,1.65fr)" in source
    assert "@media(max-width:900px){.wallet-live-layout{grid-template-columns:1fr}" in source
