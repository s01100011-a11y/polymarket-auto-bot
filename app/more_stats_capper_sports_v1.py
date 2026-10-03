from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app import attribution_core as attribution
from app import dashboard_metrics_v3 as metrics
from app import dashboard_sh01_capper_v3 as base

core = base.core
dashboard = base.dashboard

_ORIGINAL_MORE_STATS = metrics._more_stats_payload
_ACTIVE = {"ORDER_SUBMITTED", "PARTIALLY_CLOSED", "PAPER_OPEN"}
_SPORT_ORDER = ("NFL", "CFB", "MLB", "WNBA", "NBA", "SOCCER", "UFC", "NRL", "WNRL")


def _d(value: Any, default: str = "0") -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _metric_time(rec: dict[str, Any]) -> datetime | None:
    for raw in (
        rec.get("closed_at"),
        rec.get("updated_at"),
        rec.get("submitted_at"),
        rec.get("created_at"),
    ):
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _blank(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "bets": 0,
        "open": 0,
        "wins": 0,
        "losses": 0,
        "pushes": 0,
        "graded": 0,
        "stake": Decimal("0"),
        "realized": Decimal("0"),
        "realized_7d": Decimal("0"),
        "realized_30d": Decimal("0"),
        "unrealized": Decimal("0"),
        "open_value": Decimal("0"),
    }


def _summarize(row: dict[str, Any]) -> dict[str, Any]:
    decided = int(row["wins"]) + int(row["losses"])
    win_pct = Decimal(int(row["wins"])) / Decimal(decided) * Decimal("100") if decided else None
    roi = row["realized"] / row["stake"] * Decimal("100") if row["stake"] > 0 else None
    total_live = row["realized"] + row["unrealized"]
    return {
        "name": row["name"],
        "bets": int(row["bets"]),
        "open": int(row["open"]),
        "wins": int(row["wins"]),
        "losses": int(row["losses"]),
        "pushes": int(row["pushes"]),
        "graded": int(row["graded"]),
        "win_pct": str(win_pct.quantize(Decimal("0.1"))) if win_pct is not None else None,
        "stake_usdc": str(row["stake"].quantize(Decimal("0.01"))),
        "realized_pnl_usdc": str(row["realized"].quantize(Decimal("0.01"))),
        "realized_pnl_7d_usdc": str(row["realized_7d"].quantize(Decimal("0.01"))),
        "realized_pnl_30d_usdc": str(row["realized_30d"].quantize(Decimal("0.01"))),
        "unrealized_pnl_usdc": str(row["unrealized"].quantize(Decimal("0.01"))),
        "total_live_pnl_usdc": str(total_live.quantize(Decimal("0.01"))),
        "open_value_usdc": str(row["open_value"].quantize(Decimal("0.01"))),
        "roi_pct": str(roi.quantize(Decimal("0.1"))) if roi is not None else None,
    }


def _sport_sort_key(name: str) -> tuple[int, str]:
    upper = str(name or "").upper()
    try:
        return _SPORT_ORDER.index(upper), upper
    except ValueError:
        return len(_SPORT_ORDER), upper


def _more_stats_with_capper_sports(mode: str = "live") -> dict[str, Any]:
    payload = _ORIGINAL_MORE_STATS(mode)
    resolved_mode = str(payload.get("mode") or mode or "live").strip().lower()

    raw = core._load(core.EXECUTIONS_FILE)
    executions = raw if isinstance(raw, dict) else {}
    records = [
        rec
        for rec in executions.values()
        if isinstance(rec, dict)
        and not rec.get("parent_trade_id")
        and not rec.get("stats_excluded")
        and (
            resolved_mode == "both"
            or (resolved_mode == "paper" and bool(rec.get("paper")))
            or (resolved_mode not in {"paper", "both"} and not bool(rec.get("paper")))
        )
    ]

    try:
        current, _ = dashboard._estimate_pnl(records)
    except Exception:
        current = []
    marks = {
        str(row.get("id")): row
        for row in current
        if isinstance(row, dict) and row.get("id")
    }

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for rec in records:
        capper, sport = attribution.capper_name(rec)
        capper = str(capper or "").strip()
        sport = str(sport or "").strip().upper()
        if not capper or not sport:
            continue

        key = (capper, sport)
        row = grouped.setdefault(key, _blank(sport))
        row["bets"] += 1

        status = str(rec.get("status") or "").upper()
        mark = marks.get(str(rec.get("id") or "")) or {}
        if status in _ACTIVE:
            row["open"] += 1
            row["unrealized"] += _d(mark.get("estimated_pnl"))
            row["open_value"] += _d(mark.get("current_value_usdc"))

        realized = metrics.base.pnl_base._explicit_realized_pnl(rec)
        if realized is None:
            continue

        row["graded"] += 1
        row["realized"] += realized
        stake = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc"))
        if stake > 0:
            row["stake"] += stake
        if realized > 0:
            row["wins"] += 1
        elif realized < 0:
            row["losses"] += 1
        else:
            row["pushes"] += 1

        settled_at = _metric_time(rec)
        if settled_at is not None:
            if settled_at >= cutoff_30d:
                row["realized_30d"] += realized
            if settled_at >= cutoff_7d:
                row["realized_7d"] += realized

    for capper_row in payload.get("by_capper") or []:
        capper_name = str(capper_row.get("name") or "").strip()
        sport_rows: list[dict[str, Any]] = []
        sports = sorted(
            (sport for capper, sport in grouped if capper == capper_name),
            key=_sport_sort_key,
        )
        for sport in sports:
            summary = _summarize(grouped[(capper_name, sport)])
            summary.update(
                attribution.unit_summary(
                    records,
                    marks,
                    source_name=capper_name,
                    sport=sport,
                )
            )
            sport_rows.append(summary)
        capper_row["sport_stats"] = sport_rows

    payload["capper_sport_breakdown"] = True
    return payload


metrics._more_stats_payload = _more_stats_with_capper_sports


html = dashboard.DASHBOARD_HTML
if "capperSportBreakdownV1" not in html:
    css = r'''
.capper-sport-breakdown{margin-top:10px;padding-top:8px;border-top:1px solid #666}.capper-sport-heading{font-weight:900;margin-bottom:5px}.capper-sport-row{margin-top:6px;padding:7px 8px;border:1px inset #fff;background:#111;color:#ddd;font-family:monospace;line-height:1.45}.capper-sport-row .sport-name{font-size:13px;font-weight:900;color:#fff}.capper-sport-row .positive{color:#22c55e}.capper-sport-row .negative{color:#ef4444}
'''
    html = html.replace("</style>", css + "</style>", 1)
    js = r'''
<script>
const capperSportBreakdownV1=true;
function capperSportMoney(v){const n=Number(v||0);return (n>0?'+':'')+'$'+n.toFixed(2)}
function capperSportUnit(v){const n=Number(v||0);return (n>0?'+':'')+n.toFixed(2)+'u'}
function capperSportClass(v){const n=Number(v||0);return n>0?'positive':n<0?'negative':''}
function capperSportHtml(rows){
 if(!Array.isArray(rows)||!rows.length)return '';
 return '<div class="capper-sport-breakdown"><div class="capper-sport-heading">SPORT BREAKDOWN</div>'
  +rows.map(s=>{
   const win=s.win_pct===null||s.win_pct===undefined?'—':Number(s.win_pct).toFixed(1)+'%';
   const roi=s.roi_pct===null||s.roi_pct===undefined?'—':Number(s.roi_pct).toFixed(1)+'%';
   return '<div class="capper-sport-row"><div class="sport-name">'+moreStatsEsc(s.name)+'</div>'
    +'<div>Bets '+Number(s.bets||0)+' · Open '+Number(s.open||0)+' · W-L-P '+Number(s.wins||0)+'-'+Number(s.losses||0)+'-'+Number(s.pushes||0)+' · Win '+win+'</div>'
    +'<div>Stake $'+Number(s.stake_usdc||0).toFixed(2)+' · ROI '+roi+'</div>'
    +'<div class="'+capperSportClass(s.realized_pnl_usdc)+'">Realized P/L '+capperSportMoney(s.realized_pnl_usdc)+' · 7D '+capperSportMoney(s.realized_pnl_7d_usdc)+' · 30D '+capperSportMoney(s.realized_pnl_30d_usdc)+'</div>'
    +'<div class="'+capperSportClass(s.total_live_pnl_usdc)+'">Live P/L '+capperSportMoney(s.total_live_pnl_usdc)+'</div>'
    +'<div>Units called '+Number(s.units_called||0).toFixed(2)+'u · Actual Unit P/L <span class="'+capperSportClass(s.total_live_units_pnl)+'">'+capperSportUnit(s.total_live_units_pnl)+'</span></div></div>';
  }).join('')+'</div>';
}
if(typeof renderMoreStats==='function'){
 const _renderMoreStatsCapperSports=renderMoreStats;
 renderMoreStats=function(){
  const result=_renderMoreStatsCapperSports.apply(this,arguments);
  try{
   if(moreStatsTab==='capper'&&moreStatsData){
    const rows=moreStatsData.by_capper||[];
    const cards=[...document.querySelectorAll('#moreStatsBody .more-stats-row')];
    cards.forEach((card,index)=>{
     const row=rows[index]||{};
     const old=card.querySelector('.capper-sport-breakdown');if(old)old.remove();
     const sportHtml=capperSportHtml(row.sport_stats||[]);
     if(sportHtml)card.insertAdjacentHTML('beforeend',sportHtml);
    });
   }
  }catch(e){}
  return result;
 };
}
</script>
'''
    html = html.replace("</body>", js + "</body>", 1)
    dashboard.DASHBOARD_HTML = html
