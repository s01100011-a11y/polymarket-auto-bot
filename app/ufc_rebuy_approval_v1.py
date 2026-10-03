from __future__ import annotations

from app import termux_executor_dashboard as remote
from app import ufc_live_page_v1 as live
from app import ufc_live_sell_controls_v1 as _sell_controls  # noqa: F401
from app import ufc_sh01_dashboard as ufc


# Once a UFC buy is filled/open, allow another intentional buy on the same
# fighter. Only an identical request that is still waiting/pending/leased blocks
# another tap, which protects against accidental rapid double-submission.
def _pending_same_buy(strategy_pick_id: str) -> bool:
    try:
        remote._expire_stale_buys_persisted()
    except Exception:
        pass
    try:
        queue = remote._queue_load()
    except Exception:
        queue = {}
    for rec in queue.values():
        if not isinstance(rec, dict) or str(rec.get("action") or "").upper() != "BUY":
            continue
        if str(rec.get("status") or "").upper() not in {"WAITING_APPROVAL", "PENDING", "LEASED"}:
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        if str(payload.get("strategy_pick_id") or "") == str(strategy_pick_id):
            return True
    return False


ufc._existing_buy = _pending_same_buy


# WAITING_APPROVAL rows on the isolated UFC page should expose the same guarded
# approval action used everywhere else by the executor.
html = live.UFC_LIVE_HTML
if "ufc-live-approval-v1" not in html:
    css = r'''
/* ufc-live-approval-v1 */
.pending-actions{margin-top:6px;display:flex;gap:6px;flex-wrap:wrap}
.approve-buy{background:#008000!important;color:#fff!important;border:2px outset #fff!important;padding:5px 9px!important;font-weight:900!important;min-height:30px}
.approve-buy:disabled{opacity:.55}
@media(max-width:560px){.pending-actions .approve-buy{width:100%}}
'''
    html = html.replace("</style>", css + "</style>", 1)

    old = """   for(const x of pending){
    out+='<div class=\"position pending\"><b>'+esc(x.status)+' · '+esc(x.selection||'UFC BUY')+'</b><br>Stake '+money(x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.price)+' · Shares PENDING · P/L PENDING</div>';
   }"""
    new = """   for(const x of pending){
    const approve=String(x.status||'').toUpperCase()==='WAITING_APPROVAL'&&x.request_id?('<div class=\"pending-actions\"><button type=\"button\" class=\"approve-buy\" data-request-id=\"'+esc(x.request_id)+'\" onclick=\"ufcApprovePendingBuy(this.dataset.requestId,this)\">APPROVE BUY</button></div>'):'';
    out+='<div class=\"position pending\"><b>'+esc(x.status)+' · '+esc(x.selection||'UFC BUY')+'</b><br>Stake '+money(x.stake_usdc)+' · '+Number(x.units||1).toFixed(2)+'U · Odds '+odds(x.price)+' · Shares PENDING · P/L PENDING'+approve+'</div>';
   }"""
    if old in html:
        html = html.replace(old, new, 1)

    js = r'''
<script id="ufc-live-approval-v1">
async function ufcApprovePendingBuy(requestId,btn){
 const old=btn.textContent;btn.disabled=true;btn.textContent='APPROVING…';
 try{
  const r=await fetch('/api/executor/approve-buy/'+encodeURIComponent(requestId),{method:'POST'}),d=await r.json();
  if(!r.ok)throw Error(d.detail||d.error||'Approval failed');
  btn.textContent='APPROVED · QUEUED';
  setTimeout(()=>window.refreshPositions&&window.refreshPositions(),250);
 }catch(e){
  btn.disabled=false;btn.textContent=old;
  alert(String(e.message||e));
 }
}
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    live.UFC_LIVE_HTML = html
