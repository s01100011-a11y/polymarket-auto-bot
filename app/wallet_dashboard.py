from __future__ import annotations

from decimal import Decimal

from fastapi import Depends

from app import live_trading as base

app = base.app
dashboard = base.dashboard
core = base.core

USDC_BASE = Decimal("1000000")


def _wallet_snapshot() -> dict:
    private_key, wallet = base._credentials()
    with base.SecureClient.create(private_key=private_key, wallet=wallet) as client:
        balance_allowance = client.get_balance_allowance(asset_type="COLLATERAL")
        raw_balance = Decimal(str(balance_allowance.balance))
        balance = raw_balance / USDC_BASE
        try:
            portfolio = client.get_portfolio_value()
            portfolio_value = Decimal(str(getattr(portfolio, "value", "0") or "0"))
        except Exception:
            portfolio_value = None
        return {
            "ok": True,
            "connected": True,
            "wallet": base._mask_wallet(str(client.wallet)),
            "signer": base._mask_wallet(str(client.signer)),
            "wallet_type": str(client.wallet_type),
            "usdc_balance": str(balance.quantize(Decimal("0.01"))),
            "raw_balance_base_units": str(int(raw_balance)),
            "portfolio_value": str(portfolio_value.quantize(Decimal("0.01"))) if portfolio_value is not None else None,
            "live_trading": core.live_trading_enabled(),
            "auto_trading": core.auto_trading_enabled(),
            "slack_paper_only": base.ingest.SLACK_PAPER_ONLY,
        }


@app.get("/api/live/wallet", dependencies=[Depends(dashboard._auth)])
def live_wallet():
    return _wallet_snapshot()


def _install_wallet_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="walletBalance"' in html:
        return

    cards_html = '''  <div class="cards">
    <div class="card"><div class="label">Mode</div><div class="value small" id="mode">—</div></div>
    <div class="card"><div class="label">Active watches</div><div class="value" id="watches">—</div></div>
    <div class="card"><div class="label">Live trades</div><div class="value" id="liveTrades">—</div></div>
    <div class="card"><div class="label">Est. P/L</div><div class="value" id="pnl">—</div></div>
    <div class="card"><div class="label">Daily budget used</div><div class="value small" id="budget">—</div></div>
  </div>

'''
    # Some earlier dashboard layers rename "Live trades" to "Current trades".
    # Remove either spelling before recreating the same live IDs inside Wallet.
    html = html.replace(cards_html, "", 1)
    html = html.replace(cards_html.replace("Live trades", "Current trades"), "", 1)

    wallet_html = '''
  <div class="wallet-live-layout">
    <div class="wallet-strip">
      <div class="wallet-main">
        <div class="label">Wallet</div>

        <div class="wallet-dashboard-row wallet-row-address">
          <div class="wallet-data-box wallet-address-box">
            <div class="label">Wallet address</div>
            <div class="wallet-address" id="walletAddress">Checking connection…</div>
            <div class="wallet-state" id="walletState">Read-only balance check</div>
          </div>
        </div>

        <div class="wallet-dashboard-row wallet-row-balance">
          <div class="wallet-data-box wallet-metric">
            <div class="label">Available USDC</div>
            <div class="wallet-balance" id="walletBalance">—</div>
          </div>
          <div class="wallet-data-box wallet-metric">
            <div class="label">Positions value</div>
            <div class="wallet-secondary" id="walletPortfolio">—</div>
          </div>
          <div class="wallet-data-box wallet-summary-box">
            <div class="label">Est. P/L</div>
            <div class="value" id="pnl">—</div>
          </div>
        </div>

        <div class="wallet-dashboard-row wallet-row-activity">
          <div class="wallet-data-box wallet-summary-box">
            <div class="label">Daily budget used</div>
            <div class="value small" id="budget">—</div>
          </div>
          <div class="wallet-data-box wallet-summary-box">
            <div class="label">Active watches</div>
            <div class="value" id="watches">—</div>
          </div>
          <div class="wallet-data-box wallet-summary-box">
            <div class="label">Current trades</div>
            <div class="value" id="liveTrades">—</div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <div class="wallet-graph-slot" id="walletLiveGraphSlot"></div>
'''
    html = html.replace('  <div class="tabs">', wallet_html + '  <div class="tabs">', 1)

    css = '''
.wallet-live-layout{display:grid;grid-template-columns:1fr;gap:14px;align-items:stretch;margin:18px 0}.wallet-strip{background:rgba(17,24,39,.88);border:1px solid var(--border);border-radius:16px;padding:16px;margin:0;height:100%;box-sizing:border-box}.wallet-main{height:100%}.wallet-dashboard-row{display:grid;gap:8px;margin-top:8px}.wallet-row-address{grid-template-columns:1fr}.wallet-row-balance{grid-template-columns:repeat(3,minmax(0,1fr))}.wallet-row-activity{grid-template-columns:repeat(3,minmax(0,1fr))}.wallet-data-box{min-width:0;padding:10px}.wallet-address{font-size:15px;font-weight:800;margin-top:6px;word-break:break-all}.wallet-state{font-size:11px;color:var(--muted);margin-top:4px}.wallet-metric{border:0}.wallet-balance{font-size:26px;font-weight:900;color:var(--accent);margin-top:5px}.wallet-secondary{font-size:20px;font-weight:850;margin-top:5px}.wallet-error{color:var(--bad)}.wallet-graph-slot{min-width:0;margin:14px 0}.wallet-graph-slot .pnl-chart-card{height:auto;box-sizing:border-box;margin:0}.wallet-graph-slot .pnl-chart{height:225px}@media(max-width:1050px){.wallet-live-layout{grid-template-columns:1fr}}@media(max-width:700px){.wallet-row-address,.wallet-row-balance,.wallet-row-activity{grid-template-columns:1fr}.wallet-dashboard-row{gap:6px}.wallet-data-box{padding:9px}}
'''
    html = html.replace('</style>', css + '</style>', 1)

    wallet_js = r'''
function moveLivePnlGraphToStandaloneWindow(){
 const slot=document.getElementById('walletLiveGraphSlot');
 const chart=document.querySelector('.pnl-chart-card');
 if(slot&&chart&&chart.parentElement!==slot)slot.appendChild(chart);
}
async function loadWallet(){
 try{
  const r=await fetch('/api/live/wallet',{cache:'no-store'});
  const a=document.getElementById('walletAddress'),s=document.getElementById('walletState'),b=document.getElementById('walletBalance'),p=document.getElementById('walletPortfolio');
  if(!r.ok){let msg='HTTP '+r.status;try{const e=await r.json();msg=e.detail||msg}catch(_e){};throw new Error(msg)}
  const d=await r.json();
  a.textContent=d.wallet||'Connected wallet';a.className='wallet-address';
  s.textContent=`Connected · ${d.wallet_type||'wallet'} · Live trading ${d.live_trading?'ON':'OFF'} · Slack ${d.slack_paper_only?'PAPER':'LIVE'}`;s.className='wallet-state';
  b.textContent='$'+Number(d.usdc_balance||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
  p.textContent=d.portfolio_value===null||d.portfolio_value===undefined?'—':'$'+Number(d.portfolio_value).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
 }catch(e){
  const a=document.getElementById('walletAddress'),s=document.getElementById('walletState'),b=document.getElementById('walletBalance'),p=document.getElementById('walletPortfolio');
  if(a){a.textContent='Wallet connection failed';a.className='wallet-address wallet-error'}if(s){s.textContent=String(e);s.className='wallet-state wallet-error'}if(b)b.textContent='—';if(p)p.textContent='—';
 }
}
'''
    html = html.replace('</script>', wallet_js + '\nmoveLivePnlGraphToStandaloneWindow();loadWallet();setInterval(loadWallet,5000);\n</script>', 1)
    dashboard.DASHBOARD_HTML = html


_install_wallet_ui()
