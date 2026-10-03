from __future__ import annotations

import os
from typing import Any

import uvicorn

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
    return item


cfb._status_pick_item = _status_pick_item_with_pending_approvals


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
  }).join(''):((waitingApproval&&approvalRequestId)?'<button type=\"button\" style=\"margin-top:6px;margin-right:6px\" data-request-id=\"'+cfbEsc(approvalRequestId)+'\" onclick=\"cfbApproveBuy(this.dataset.requestId,this)\">'+approvalText+'</button>':'');"""
if _old_approve in html:
    html = html.replace(_old_approve, _new_approve, 1)

_old_action = """  const action=approveAction+buyAction+altActions+sellAction+marketAction;"""
_new_action = """  const approvalBanner=pendingApprovals.length?('<div class=\"monitor-action\" style=\"margin-top:7px\"><b>PENDING APPROVAL'+(pendingApprovals.length>1?'S ('+pendingApprovals.length+')':'')+':</b> '+pendingApprovals.map(p=>cfbEsc(p.line||'CFB BUY')+' @ '+(p.signal_decimal_odds?Number(p.signal_decimal_odds).toFixed(2):'—')).join(' · ')+'</div>'):'';
  const action=approveAction+buyAction+altActions+sellAction+marketAction;"""
if _old_action in html:
    html = html.replace(_old_action, _new_action, 1)

_old_return = """  return '<div class=\"monitor-signal\"><b>'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<div class=\"monitor-meta\">'+meta.join(' · ')+'</div>':'')+pnlLine+(action?'<div class=\"monitor-signal-actions\">'+action+'</div>':'')+'</div>';"""
_new_return = """  return '<div class=\"monitor-signal\"><b>'+cfbEsc(item.selection||'Unknown selection')+'</b>'+badge+(meta.length?'<div class=\"monitor-meta\">'+meta.join(' · ')+'</div>':'')+approvalBanner+pnlLine+(action?'<div class=\"monitor-signal-actions\">'+action+'</div>':'')+'</div>';"""
if _old_return in html:
    html = html.replace(_old_return, _new_return, 1)

composite.dashboard.DASHBOARD_HTML = html


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
