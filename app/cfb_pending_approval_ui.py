from __future__ import annotations

import os
from typing import Any

import uvicorn
from fastapi import Depends, HTTPException

from app import cfb_total_click_launcher as base

composite = base.composite
cfb = base.cfb
app = base.app


# Surface active approval requests directly in the CFB status payload.  This is
# UI-only state enrichment; it never approves, rejects, cancels, or submits an
# order.
_ORIGINAL_STATUS_PICK_ITEM = cfb._status_pick_item


def _status_pick_item_with_pending_approvals(record: dict[str, Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
    item = _ORIGINAL_STATUS_PICK_ITEM(record, *args, **kwargs)
    signal_id = str(item.get("signal_id") or record.get("id") or "")
    pending: list[dict[str, Any]] = []
    try:
        queue = cfb.live_control.remote._queue_load()
    except Exception:
        queue = {}

    for request_id, queued in queue.items():
        if not isinstance(queued, dict) or queued.get("action") != "BUY":
            continue
        if str(queued.get("status") or "").upper() != "WAITING_APPROVAL":
            continue
        payload = queued.get("payload") or {}
        if str(payload.get("strategy_sport") or "").upper() != "CFB":
            continue
        if str(payload.get("strategy_pick_id") or "") != signal_id:
            continue
        pending.append(
            {
                "request_id": str(request_id),
                "line": payload.get("strategy_alternate_line") or payload.get("strategy_execution_selection"),
                "approval_reason": payload.get("approval_reason"),
                "signal_decimal_odds": payload.get("signal_decimal_odds"),
                "minimum_decimal_odds": payload.get("minimum_decimal_odds"),
                "max_price": payload.get("max_price"),
                "trade_id": payload.get("trade_id"),
            }
        )

    if pending:
        item["pending_approvals"] = pending
        item["approval_required"] = True
        item["status"] = "WAITING_APPROVAL"
        item["manual_buy_status"] = "WAITING_APPROVAL"
        item["manual_buy_request_id"] = pending[-1]["request_id"]
        item["request_id"] = pending[-1]["request_id"]
        item["approval_reason"] = pending[-1].get("approval_reason")
        item["signal_decimal_odds"] = pending[-1].get("signal_decimal_odds")
        item["minimum_decimal_odds"] = pending[-1].get("minimum_decimal_odds")
        item["manual_buy_alternate_line"] = pending[-1].get("line")
    else:
        item["pending_approvals"] = []
        # Queue state is authoritative.  Do not let a stale persisted manual
        # WAITING_APPROVAL flag keep the five live alternatives hidden after a
        # user cancels the approval request(s).
        manual_request_id = str(record.get("manual_buy_request_id") or "")
        queued = queue.get(manual_request_id) if manual_request_id else None
        queued_status = str((queued or {}).get("status") or "").upper()
        if str(item.get("manual_buy_status") or "").upper() == "WAITING_APPROVAL":
            item["manual_buy_status"] = queued_status or "CANCELLED"
        if queued_status != "WAITING_APPROVAL":
            item["approval_required"] = False
            item["approval_reason"] = None
            if str(item.get("status") or "").upper() == "WAITING_APPROVAL":
                item["status"] = str(record.get("status") or "MATCHED_LIVE_ALTERNATE")
    return item


cfb._status_pick_item = _status_pick_item_with_pending_approvals


@app.post(
    "/api/cfb-cappers/cancel-pending-approvals/{signal_id}",
    dependencies=[Depends(composite.dashboard._auth)],
)
def cancel_cfb_pending_approvals(signal_id: str) -> dict[str, Any]:
    """Cancel every still-waiting manual approval for one CFB signal."""
    try:
        queue = cfb.live_control.remote._queue_load()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Could not read executor queue: {exc}") from exc

    request_ids: list[str] = []
    for request_id, queued in queue.items():
        if not isinstance(queued, dict) or queued.get("action") != "BUY":
            continue
        if str(queued.get("status") or "").upper() != "WAITING_APPROVAL":
            continue
        payload = queued.get("payload") or {}
        if str(payload.get("strategy_sport") or "").upper() != "CFB":
            continue
        if str(payload.get("strategy_pick_id") or "") != signal_id:
            continue
        request_ids.append(str(request_id))

    if not request_ids:
        raise HTTPException(status_code=404, detail="No CFB BUY approvals are currently pending for this signal")

    cancelled: list[str] = []
    for request_id in request_ids:
        try:
            cfb.live_control._reject_waiting_buy(request_id)
            cancelled.append(request_id)
        except HTTPException as exc:
            # A simultaneous executor/UI refresh may have already decided one
            # request.  Ignore only that benign race and continue cancelling
            # any other duplicates for the same signal.
            if exc.status_code != 404:
                raise

    signal_file = composite.core.DATA_DIR / "cfb_capper_preview_signals.json"
    signals = composite.core._load(signal_file)
    if isinstance(signals, dict):
        record = signals.get(signal_id)
        if isinstance(record, dict):
            record["manual_buy_status"] = "CANCELLED"
            record["manual_buy_request_id"] = None
            record["approval_required"] = False
            record["approval_reason"] = None
            record["manual_buy_error"] = None
            record["manual_buy_alternate_line"] = None
            record["manual_buy_alternative_id"] = None
            record["updated_at"] = cfb._now_iso()
            signals[signal_id] = record
            composite.core._save(signal_file, signals)

    return {
        "ok": True,
        "signal_id": signal_id,
        "cancelled": cancelled,
        "cancelled_count": len(cancelled),
        "status": "CANCELLED",
    }


# Make the CFB card treat a manual WAITING_APPROVAL exactly like an automatic
# approval hold, and show every currently pending approval inline on the signal.
html = composite.dashboard.DASHBOARD_HTML
html = html.replace(
    "const waitingApproval=String(item.status||'').toUpperCase()==='WAITING_APPROVAL';",
    "const waitingApproval=String(item.status||'').toUpperCase()==='WAITING_APPROVAL'||state==='WAITING_APPROVAL'||item.approval_required===true;",
    1,
)

_old_approve = """  const approveAction=(waitingApproval&&item.request_id)?'<button type=\"button\" style=\"margin-top:6px;margin-right:6px\" data-request-id=\"'+cfbEsc(item.request_id)+'\" onclick=\"cfbApproveBuy(this.dataset.requestId,this)\">'+approvalText+'</button>':'';"""
_new_approve = """  const approvalRequestId=item.request_id||item.manual_buy_request_id;
  const pendingApprovals=Array.isArray(item.pending_approvals)?item.pending_approvals:[];
  const approveAction=pendingApprovals.length?pendingApprovals.map(p=>{
   const pOdds=p.signal_decimal_odds?Number(p.signal_decimal_odds).toFixed(2):'—';
   const pLine=p.line?cfbEsc(p.line):'CFB BUY';
   return '<button type=\"button\" style=\"margin-top:6px;margin-right:6px\" data-request-id=\"'+cfbEsc(p.request_id)+'\" onclick=\"cfbApproveBuy(this.dataset.requestId,this)\">APPROVE '+pLine+' @ '+pOdds+'</button>';
  }).join(''):((waitingApproval&&approvalRequestId)?'<button type=\"button\" style=\"margin-top:6px;margin-right:6px\" data-request-id=\"'+cfbEsc(approvalRequestId)+'\" onclick=\"cfbApproveBuy(this.dataset.requestId,this)\">'+approvalText+'</button>':'');
  const cancelApprovalAction=pendingApprovals.length?'<button type=\"button\" style=\"margin-top:6px;margin-right:6px\" data-signal-id=\"'+cfbEsc(item.signal_id)+'\" onclick=\"cfbCancelPendingApprovals(this.dataset.signalId,this)\">CANCEL</button>':'';"""
if _old_approve in html:
    html = html.replace(_old_approve, _new_approve, 1)

_old_action = """  const action=approveAction+buyAction+altActions+sellAction+marketAction;"""
_new_action = """  const approvalBanner=pendingApprovals.length?('<div class=\"monitor-action\" style=\"margin-top:7px\"><b>PENDING APPROVAL'+(pendingApprovals.length>1?'S ('+pendingApprovals.length+')':'')+':</b> '+pendingApprovals.map(p=>cfbEsc(p.line||'CFB BUY')+' @ '+(p.signal_decimal_odds?Number(p.signal_decimal_odds).toFixed(2):'—')).join(' · ')+'</div>'):'';
  const action=approveAction+cancelApprovalAction+buyAction+altActions+sellAction+marketAction;"""
if _old_action in html:
    html = html.replace(_old_action, _new_action, 1)

_old_return = """  return '<div class=\"monitor-signal\"><b>'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<div class=\"monitor-meta\">'+meta.join(' · ')+'</div>':'')+pnlLine+(action?'<div class=\"monitor-signal-actions\">'+action+'</div>':'')+'</div>';"""
_new_return = """  return '<div class=\"monitor-signal\"><b>'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<div class=\"monitor-meta\">'+meta.join(' · ')+'</div>':'')+approvalBanner+pnlLine+(action?'<div class=\"monitor-signal-actions\">'+action+'</div>':'')+'</div>';"""
if _old_return in html:
    html = html.replace(_old_return, _new_return, 1)

_cancel_js = r"""
async function cfbCancelPendingApprovals(signalId,btn){
 const original=btn.textContent;btn.disabled=true;btn.textContent='CANCELLING…';
 try{
  const r=await fetch('/api/cfb-cappers/cancel-pending-approvals/'+encodeURIComponent(signalId),{method:'POST'}),d=await r.json();
  if(!r.ok)throw new Error(d.detail||'Cancel failed');
  btn.textContent='CANCELLED';
  await loadCfbCapperStats();
 }catch(e){
  btn.disabled=false;btn.textContent=original;
  alert(e.message||String(e));
 }
}
"""
if "async function cfbCancelPendingApprovals(" not in html:
    html = html.replace("async function cfbApproveBuy(requestId,btn){", _cancel_js + "\nasync function cfbApproveBuy(requestId,btn){", 1)

composite.dashboard.DASHBOARD_HTML = html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
