from __future__ import annotations

from fastapi import Depends, HTTPException

from app import wallet_dashboard as base
from app import live_trading

app = base.app
dashboard = base.dashboard
core = base.core


# Hard-lock the protected live-test BUY path to sports moneyline markets.
_ORIGINAL_VALIDATE_TEST_MARKET = live_trading._validate_test_market


def _validate_moneyline_only(req: live_trading.LiveTestBuy):
    if core._norm(req.market_type) != "moneyline":
        raise HTTPException(status_code=400, detail="Live test BUY is locked to moneyline only")

    result = _ORIGINAL_VALIDATE_TEST_MARKET(req)
    market = result[1]
    actual_type = core._norm(core._market_type(market))
    if actual_type != "moneyline":
        raise HTTPException(
            status_code=400,
            detail=f"Resolved market type is '{core._market_type(market)}'; live test BUY requires moneyline",
        )
    return result


live_trading._validate_test_market = _validate_moneyline_only


@app.post("/api/live/test-preview", dependencies=[Depends(dashboard._auth)])
def live_test_preview(req: live_trading.LiveTestBuy):
    _, market, asset_id, outcome_label, buy_price, spread, shares = _validate_moneyline_only(req)
    return {
        "ok": True,
        "market": str(getattr(market, "question", None) or getattr(market, "slug", "sports market")),
        "market_type": core._market_type(market),
        "outcome": outcome_label,
        "asset_id": str(asset_id),
        "current_buy_price": str(buy_price),
        "spread": str(spread),
        "requested_shares": str(shares),
        "budget_usdc": str(req.budget_usdc),
        "max_price": str(req.max_price),
        "live_trading": core.live_trading_enabled(),
        "test_max_usdc": str(live_trading.LIVE_TEST_MAX_USDC),
    }


def _install_live_test_ui() -> None:
    html = dashboard.DASHBOARD_HTML
    if 'id="liveTestPanel"' in html:
        return

    html = html.replace(
        '<button class="tab" data-tab="settings">Configuration</button>',
        '<button class="tab" data-tab="livet">Live test</button>\n    <button class="tab" data-tab="settings">Configuration</button>',
        1,
    )

    panel = '''  <section class="panel" id="livet">
    <div id="liveTestPanel">
      <h2>Live Moneyline Test</h2>
      <p class="note">Protected manual test only. Sports moneyline BUYs are hard-locked to a maximum of $5. Automatic trading is separate and remains controlled by AUTO_TRADING.</p>
      <div class="live-test-warning">REAL MONEY · A BUY button press can submit a live Polymarket limit order.</div>
      <div class="grid2 live-test-grid">
        <div class="setting"><label>Polymarket sports URL</label><input id="ltMarket" type="url" placeholder="https://polymarket.com/sports/..." autocomplete="off"></div>
        <div class="setting"><label>Moneyline outcome / team</label><input id="ltOutcome" type="text" placeholder="Team name" autocomplete="off"></div>
        <div class="setting"><label>Maximum price</label><input id="ltMaxPrice" type="number" min="0.01" max="0.99" step="0.01" value="0.95"></div>
        <div class="setting"><label>Test amount (USDC, max $5)</label><input id="ltBudget" type="number" min="0.01" max="5" step="0.01" value="5.00"></div>
      </div>
      <div class="live-test-lock">Market type: <b>MONEYLINE ONLY</b> · Any spread/total request is rejected server-side.</div>
      <div class="actions live-test-actions">
        <button class="btn secondary" id="ltPreview" type="button">Check market</button>
        <button class="btn danger" id="ltBuy" type="button">BUY test position</button>
        <button class="btn" id="ltSell" type="button" disabled>SELL test position</button>
        <span class="save-msg" id="ltMsg">No live order submitted.</span>
      </div>
      <div class="live-test-result" id="ltResult">Use “Check market” first to verify the resolved moneyline, current BUY price and spread.</div>
    </div>
  </section>
'''
    html = html.replace('  <section class="panel" id="settings">', panel + '  <section class="panel" id="settings">', 1)

    css = '''
.live-test-warning{border:1px solid var(--bad);background:rgba(251,113,133,.08);color:var(--bad);font-size:12px;font-weight:850;letter-spacing:.05em;padding:10px 12px;border-radius:10px;margin:12px 0}.live-test-grid{margin-top:12px}.live-test-lock{margin-top:12px;padding:10px 12px;border:1px solid var(--border);background:#0d1522;border-radius:10px;color:var(--muted);font-size:12px}.live-test-actions{flex-wrap:wrap}.btn.secondary{background:#dbeafe;color:#0c2340}.btn.danger{background:#fecdd3;color:#3b0812}.btn:disabled{opacity:.45;cursor:not-allowed}.live-test-result{margin-top:12px;border:1px solid var(--border);background:#080e18;border-radius:10px;padding:12px;white-space:pre-wrap;word-break:break-word;color:var(--muted);font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
'''
    html = html.replace('</style>', css + '</style>', 1)

    js = r'''
let liveTestTradeId=null;
function ltEl(id){return document.getElementById(id)}
function ltPayload(){
 const budget=Number(ltEl('ltBudget').value), maxPrice=Number(ltEl('ltMaxPrice').value);
 if(!ltEl('ltMarket').value.trim())throw new Error('Enter a Polymarket sports URL');
 if(!ltEl('ltOutcome').value.trim())throw new Error('Enter the moneyline team/outcome');
 if(!(budget>0&&budget<=5))throw new Error('Test amount must be between $0.01 and $5.00');
 if(!(maxPrice>0&&maxPrice<1))throw new Error('Maximum price must be between 0 and 1');
 return {market_url:ltEl('ltMarket').value.trim(),outcome:ltEl('ltOutcome').value.trim(),market_type:'moneyline',max_price:maxPrice,budget_usdc:budget};
}
function ltShow(value){ltEl('ltResult').textContent=typeof value==='string'?value:JSON.stringify(value,null,2)}
async function ltJson(r){let d=null;try{d=await r.json()}catch(_e){}if(!r.ok)throw new Error((d&&d.detail)||('HTTP '+r.status));return d}
async function previewLiveTest(){
 try{ltEl('ltMsg').textContent='Checking moneyline…';const r=await fetch('/api/live/test-preview',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ltPayload())});const d=await ltJson(r);ltShow(d);ltEl('ltMsg').textContent='Market check passed. No order placed.'}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='Market check failed.'}
}
async function buyLiveTest(){
 try{
  const p=ltPayload();
  if(!confirm(`Submit a REAL $${Number(p.budget_usdc).toFixed(2)} moneyline limit BUY for ${p.outcome}?`))return;
  ltEl('ltBuy').disabled=true;ltEl('ltMsg').textContent='Submitting live BUY…';
  const r=await fetch('/api/live/test-buy',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(p)});const d=await ltJson(r);ltShow(d);
  if(d.ok&&d.trade_id){liveTestTradeId=d.trade_id;ltEl('ltSell').disabled=false;ltEl('ltMsg').textContent='BUY filled. SELL is ready.'}else{ltEl('ltMsg').textContent='BUY did not fill; remainder canceled.'}
 }catch(e){ltShow(String(e));ltEl('ltMsg').textContent='BUY failed.'}
 finally{ltEl('ltBuy').disabled=false}
}
async function sellLiveTest(){
 if(!liveTestTradeId){ltShow('No filled test trade is available to sell.');return}
 if(!confirm('Submit a REAL SELL for the tracked test shares at the current executable SELL price?'))return;
 try{ltEl('ltSell').disabled=true;ltEl('ltMsg').textContent='Submitting live SELL…';const r=await fetch('/api/live/test-sell/'+encodeURIComponent(liveTestTradeId),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});const d=await ltJson(r);ltShow(d);ltEl('ltMsg').textContent='SELL completed. Round-trip test finished.';liveTestTradeId=null}
 catch(e){ltShow(String(e));ltEl('ltMsg').textContent='SELL failed; check the position before retrying.';ltEl('ltSell').disabled=false}
}
if(ltEl('ltPreview'))ltEl('ltPreview').addEventListener('click',previewLiveTest);
if(ltEl('ltBuy'))ltEl('ltBuy').addEventListener('click',buyLiveTest);
if(ltEl('ltSell'))ltEl('ltSell').addEventListener('click',sellLiveTest);
'''
    html = html.replace('</script>', js + '\n</script>', 1)
    dashboard.DASHBOARD_HTML = html


_install_live_test_ui()
print("LIVE_TEST_MONEYLINE_LOCK enabled max_usdc=" + str(live_trading.LIVE_TEST_MAX_USDC), flush=True)
