from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from fastapi import Depends, HTTPException, Query

_INSTALLED = False
_MODE = "performance"
_POLICY = "__performance_sizing_policy_2026_10_03__"
_KNOWN = (
    "Slam - NFL", "Syndicate - NFL", "Slam - CFB", "Syndicate - CFB",
    "WNBA Monitor - WNBA", "NBA Monitor - NBA",
)


def _clean(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _norm(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _clean(v).casefold())


def _excluded(label: str) -> bool:
    return _norm(label).startswith("sh01")


def _d(v: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        return Decimal(str(v))
    except Exception:
        return default


def _q(v: Decimal) -> Decimal:
    return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _dt(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        return None


def _matches(label: str, value: Any) -> bool:
    a, b = _norm(label), _norm(value)
    if not b:
        return False
    if a == b:
        return True
    parts = [x for x in _clean(label).split(" - ") if x]
    return bool(parts and b in {_norm(parts[0]), _norm(" - ".join(parts[:-1]))})


def _sport(label: str) -> str:
    tail = _clean(label).split(" - ")[-1].upper()
    return tail if tail in {"NFL", "CFB", "WNBA", "NBA", "MLB", "UFC", "SOCCER", "NRL", "WNRL"} else ""


def _pct(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if row.get(key) in (None, ""):
            continue
        try:
            v = Decimal(str(row[key]))
        except Exception:
            continue
        if not key.casefold().endswith("_pct") and abs(v) <= 1:
            v *= 100
        return v
    return None


def _coerce(label: str, row: dict[str, Any], source: str) -> dict[str, Any] | None:
    merged = dict(row)
    if isinstance(row.get("data"), dict):
        merged.update(row["data"])
    n = None
    for key in ("bets", "n", "sample_size", "graded_bets", "count"):
        try:
            if merged.get(key) not in (None, ""):
                n = max(0, int(float(merged[key])))
                break
        except Exception:
            pass
    season = _pct(merged, "season_roi_pct", "roi_pct", "season_roi", "roi")
    r30 = _pct(merged, "last30_roi_pct", "roi_30d_pct", "last30_roi", "roi_30d")
    r10 = _pct(merged, "last10_roi_pct", "roi_last10_pct", "last10_roi", "roi_last10")
    if n is None or season is None:
        return None
    dd = max(Decimal("0"), _d(merged.get("drawdown_units") or merged.get("current_drawdown_units")))
    return {
        "label": label, "bets": n, "season_roi_pct": season,
        "last30_roi_pct": r30 if r30 is not None else season,
        "last10_roi_pct": r10 if r10 is not None else (r30 if r30 is not None else season),
        "drawdown_units": dd, "source": source,
        "observed_at": row.get("observed_at") or row.get("updated_at") or row.get("retrieved_at"),
    }


def _audit_file(core: Any, label: str) -> dict[str, Any] | None:
    names = [x for x in (os.getenv("CAPPER_PERFORMANCE_FILE", "").strip(), "audit_capper_performance.json", "capper_performance.json") if x]
    for name in names:
        try:
            path = core.DATA_DIR / name if "/" not in name else type(core.DATA_DIR)(name)
            raw = core._load(path)
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        pools: list[Any] = [raw] + [raw.get(k) for k in ("cappers", "performance", "rows", "records")]
        for pool in pools:
            items = pool.items() if isinstance(pool, dict) else enumerate(pool) if isinstance(pool, list) else []
            for key, row in items:
                if not isinstance(row, dict):
                    continue
                ident = row.get("label") or row.get("capper") or row.get("source") or key
                if _matches(label, ident):
                    out = _coerce(label, row, f"audit_file:{name}")
                    if out:
                        return out
    return None


def _audit_research(core: Any, label: str) -> dict[str, Any] | None:
    try:
        raw = core._load(core.DATA_DIR / "audit_research_store.json")
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    rows = []
    if isinstance(raw.get("history"), list):
        rows += [x for x in raw["history"] if isinstance(x, dict)]
    if isinstance(raw.get("latest"), dict):
        rows += [x for x in raw["latest"].values() if isinstance(x, dict)]
    wanted = {"capper_performance", "capper performance", "capper_stats", "capper stats", "capper_audit", "capper audit", "performance_snapshot"}
    sport = _sport(label)
    rows.sort(key=lambda x: str(x.get("observed_at") or x.get("retrieved_at") or x.get("ingested_at") or ""), reverse=True)
    for row in rows:
        if _clean(row.get("data_type")).casefold() not in wanted:
            continue
        if sport and _clean(row.get("sport")).upper() not in {"", sport}:
            continue
        ident = row.get("subject") or row.get("source") or (row.get("data") or {}).get("capper")
        if _matches(label, ident):
            out = _coerce(label, row, "audit_research_store")
            if out:
                return out
    return None


def _execution_metrics(core: Any, label: str) -> dict[str, Any]:
    raw = core._load(core.EXECUTIONS_FILE)
    now = datetime.now(timezone.utc)
    rows = []
    for rec in (raw.values() if isinstance(raw, dict) else []):
        if not isinstance(rec, dict) or rec.get("paper") or rec.get("parent_trade_id"):
            continue
        ident = rec.get("strategy_source") or rec.get("strategy_telegram_source") or rec.get("capper") or rec.get("source_label")
        if not _matches(label, ident):
            continue
        settlement = rec.get("settlement") if isinstance(rec.get("settlement"), dict) else {}
        if _clean(settlement.get("result") or rec.get("result")).upper() not in {"WIN", "LOSS", "PUSH"}:
            continue
        when = _dt(rec.get("closed_at") or rec.get("settled_at") or rec.get("submitted_at") or rec.get("created_at"))
        if when is not None and when.year != now.year:
            continue
        stake = _d(rec.get("actual_cost_usdc") or rec.get("budget_usdc") or rec.get("stake_usdc"))
        pnl_raw = rec.get("realized_pnl") if rec.get("realized_pnl") not in (None, "") else rec.get("realized_pnl_usdc")
        if pnl_raw in (None, ""):
            pnl_raw = settlement.get("pnl_usdc")
        pnl, unit = _d(pnl_raw), _d(rec.get("unit_usdc_at_call") or rec.get("unit_usdc"))
        if stake > 0:
            rows.append({"dt": when, "stake": stake, "pnl": pnl, "unit": unit})
    rows.sort(key=lambda x: x["dt"] or datetime.min.replace(tzinfo=timezone.utc))

    def roi(xs: list[dict[str, Any]]) -> Decimal:
        st = sum((x["stake"] for x in xs), Decimal("0"))
        return sum((x["pnl"] for x in xs), Decimal("0")) / st * 100 if st > 0 else Decimal("0")

    r30 = [x for x in rows if x["dt"] is not None and x["dt"] >= now - timedelta(days=30)]
    cum = peak = Decimal("0")
    for x in rows:
        cum += x["pnl"] / x["unit"] if x["unit"] > 0 else Decimal("0")
        peak = max(peak, cum)
    season = roi(rows)
    return {
        "label": label, "bets": len(rows), "season_roi_pct": season,
        "last30_roi_pct": roi(r30) if r30 else season,
        "last10_roi_pct": roi(rows[-10:]) if rows else season,
        "drawdown_units": max(Decimal("0"), peak - cum),
        "source": "bot_settled_fallback", "observed_at": now.isoformat(),
    }


def _recommend(core: Any, label: str) -> dict[str, Any]:
    m = _audit_file(core, label) or _audit_research(core, label) or _execution_metrics(core, label)
    n = max(0, int(m.get("bets") or 0))
    season, r30, r10 = _d(m.get("season_roi_pct")), _d(m.get("last30_roi_pct")), _d(m.get("last10_roi_pct"))
    dd = max(Decimal("0"), _d(m.get("drawdown_units")))
    adjusted = season * Decimal(n) / (Decimal(n) + 50) if n else Decimal("0")
    score = adjusted * Decimal("0.60") + r30 * Decimal("0.25") + r10 * Decimal("0.15")
    if n < 20 or score <= 0:
        base, tier = Decimal("0.50"), "CONSERVATIVE"
    elif score < 4:
        base, tier = Decimal("0.75"), "DEVELOPING"
    elif score < 8:
        base, tier = Decimal("1.00"), "PROVEN"
    elif score < 12:
        base, tier = Decimal("1.25"), "STRONG"
    elif n >= 150:
        base, tier = Decimal("1.50"), "EXCEPTIONAL"
    else:
        base, tier = Decimal("1.25"), "STRONG"
    mod = Decimal("1") if dd <= 3 else Decimal("0.85") if dd <= 5 else Decimal("0.65") if dd <= 8 else Decimal("0.40") if dd <= 12 else Decimal("0.25")
    pct = min(Decimal("1.50"), max(Decimal("0.25"), base * mod))
    return {**m, "adjusted_season_roi_pct": _q(adjusted), "performance_score": _q(score), "drawdown_modifier": _q(mod), "recommended_pct": _q(pct), "tier": tier}


def _enable(core: Any, nfl: Any, label: str) -> None:
    if _excluded(label):
        raise ValueError("SH01 is excluded from performance staking")
    settings = nfl._load_capper_unit_settings(core)
    settings[f"{label}::mode"] = _MODE
    core._save(nfl._capper_unit_settings_path(core), settings)


def _migrate(core: Any, nfl: Any) -> None:
    settings = nfl._load_capper_unit_settings(core)
    if settings.get(_POLICY):
        return
    labels = set(_KNOWN)
    for key in settings:
        if isinstance(key, str) and "::" not in key and not key.startswith("__"):
            labels.add(key)
    try:
        raw = core._load(core.EXECUTIONS_FILE)
        for rec in (raw.values() if isinstance(raw, dict) else []):
            if isinstance(rec, dict):
                label = _clean(rec.get("strategy_source") or rec.get("strategy_telegram_source"))
                if label:
                    labels.add(label)
    except Exception:
        pass
    for label in labels:
        if label and not _excluded(label):
            settings[f"{label}::mode"] = _MODE
    settings[_POLICY] = datetime.now(timezone.utc).isoformat()
    core._save(nfl._capper_unit_settings_path(core), settings)


def _install_ui(dashboard: Any) -> None:
    html = dashboard.DASHBOARD_HTML
    if "performance-sizing-v1" in html:
        return
    css = """
/* performance-sizing-v1 */
.performance-sizing-row .capper-sizing-value{font-size:11px;line-height:1.25;min-width:175px}.performance-sizing-row button.active{font-weight:800;border-color:#008000}.perf-muted{opacity:.78}
"""
    js = r"""
function perfTarget(box){const i=box.querySelector('input[id^="nflUnitSize-"],input[id^="cfbUnitSize-"],input[id^="monitorUnitSize-"]');if(!i)return null;let k;if(i.id.startsWith('nflUnitSize-')){k=i.id.slice(12).toLowerCase();return {label:(k==='slam'?'Slam':'Syndicate')+' - NFL'}}if(i.id.startsWith('cfbUnitSize-')){k=i.id.slice(12).toLowerCase();return {label:(k==='slam'?'Slam':'Syndicate')+' - CFB'}}k=i.id.slice(16).toUpperCase();return {label:k+' Monitor - '+k}}
async function perfEnable(label,b){b.disabled=true;try{const r=await fetch('/api/performance-sizing',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})}),j=await r.json();if(!r.ok)throw Error(j.detail||r.status);await perfDecorate()}catch(e){alert(String(e.message||e))}finally{b.disabled=false}}
async function perfDecorate(){for(const box of document.querySelectorAll('.capper-sizing')){const t=perfTarget(box);if(!t||/^sh01/i.test(t.label))continue;let row=box.querySelector('.performance-sizing-row');if(!row){row=document.createElement('div');row.className='capper-sizing-row performance-sizing-row';row.innerHTML='<span class="capper-sizing-label">Performance</span><div class="capper-sizing-controls"><button type="button">AUTO PERF</button><span class="capper-sizing-value">Loading…</span></div>';const note=box.querySelector('.capper-sizing-note');note?box.insertBefore(row,note):box.appendChild(row);row.querySelector('button').onclick=()=>perfEnable(t.label,row.querySelector('button'))}const v=row.querySelector('.capper-sizing-value'),b=row.querySelector('button');try{const r=await fetch('/api/performance-sizing?label='+encodeURIComponent(t.label)),j=await r.json();if(!r.ok)throw Error(j.detail||r.status);const active=j.mode==='performance',pct=Number(j.performance_pct||0),unit=Number(j.unit_usdc||0),score=Number(j.performance_score||0),dd=Number(j.drawdown_units||0),n=Number(j.bets||0);b.classList.toggle('active',active);b.setAttribute('aria-pressed',active?'true':'false');if(active){box.querySelectorAll('.capper-sizing-row:not(.performance-sizing-row) button.active').forEach(x=>x.classList.remove('active'));box.querySelectorAll('.capper-sizing-row:not(.performance-sizing-row) button[aria-pressed="true"]').forEach(x=>x.setAttribute('aria-pressed','false'));const note=box.querySelector('.capper-sizing-note');if(note)note.innerHTML='PERFORMANCE · 1u = '+pct.toFixed(2)+'% portfolio ($'+unit.toFixed(2)+')<br><span>TO WIN sizing · Audit DB first · sample shrinkage · drawdown protection</span>'}v.innerHTML='<b>'+pct.toFixed(2)+'%</b> · 1u $'+unit.toFixed(2)+'<br><span class="perf-muted">'+String(j.tier||'')+' · '+n+' bets · score '+score.toFixed(2)+' · DD '+dd.toFixed(2)+'u</span>'}catch(e){v.textContent='Unavailable'}}}
for(const n of ['nflRenderCappers','cfbRenderCappers','renderBasketballMonitors']){if(typeof window[n]==='function'){const f=window[n];window[n]=function(){const r=f.apply(this,arguments);setTimeout(perfDecorate,0);return r}}}setTimeout(perfDecorate,600);setInterval(perfDecorate,30000);
"""
    if "</style>" in html:
        html = html.replace("</style>", css + "</style>", 1)
    dashboard.DASHBOARD_HTML = html.replace("</body>", "<script>" + js + "</script></body>", 1) if "</body>" in html else html + "<script>" + js + "</script>"


def install(*, app: Any, dashboard: Any, core: Any, nfl: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    original_config, original_unit = nfl._capper_unit_config, nfl._capper_unit_usdc
    original_snapshot = getattr(nfl, "_ensure_signal_call_unit_snapshot", None)

    def config(core_obj: Any, label: str, default: Decimal) -> dict[str, Any]:
        base = original_config(core_obj, label, default)
        settings = nfl._load_capper_unit_settings(core_obj)
        mode = _clean(settings.get(f"{label}::mode")).casefold() or (_MODE if not _excluded(label) else str(base.get("mode") or "fixed"))
        if _excluded(label) or mode != _MODE:
            return base
        rec, portfolio = _recommend(core_obj, label), nfl._portfolio_value_usdc()
        error = None
        effective = _d(base.get("fixed_unit_usdc"), Decimal(str(default)))
        if portfolio is None:
            error = "Portfolio value is unavailable; performance unit sizing is waiting for a wallet heartbeat."
        elif portfolio <= 0:
            error = "Portfolio value must be greater than $0 for performance unit sizing."
        else:
            effective = _q(portfolio * _d(rec.get("recommended_pct")) / 100)
        return {**base, "mode": _MODE, "unit_usdc": str(effective), "portfolio_value_usdc": str(portfolio) if portfolio is not None else None, "error": error,
                "performance_pct": str(rec["recommended_pct"]), "performance_score": str(rec["performance_score"]), "adjusted_season_roi_pct": str(rec["adjusted_season_roi_pct"]),
                "season_roi_pct": str(_q(_d(rec.get("season_roi_pct")))), "last30_roi_pct": str(_q(_d(rec.get("last30_roi_pct")))), "last10_roi_pct": str(_q(_d(rec.get("last10_roi_pct")))),
                "drawdown_units": str(_q(_d(rec.get("drawdown_units")))), "drawdown_modifier": str(rec["drawdown_modifier"]), "bets": int(rec.get("bets") or 0), "tier": rec.get("tier"),
                "performance_source": rec.get("source"), "performance_observed_at": rec.get("observed_at")}

    def unit(core_obj: Any, label: str, default: Decimal) -> Decimal:
        c = config(core_obj, label, default)
        if c.get("mode") == _MODE and c.get("error"):
            raise RuntimeError(str(c["error"]))
        return Decimal(str(c["unit_usdc"])) if c.get("mode") == _MODE else original_unit(core_obj, label, default)

    nfl._capper_unit_config, nfl._capper_unit_usdc = config, unit
    nfl._set_capper_performance_mode = lambda core_obj, label: _enable(core_obj, nfl, label)

    if callable(original_snapshot):
        def snapshot(record: dict[str, Any], pick: dict[str, Any], *, unit_config: dict[str, Any], effective_unit_usdc: Decimal) -> bool:
            changed = bool(original_snapshot(record, pick, unit_config=unit_config, effective_unit_usdc=effective_unit_usdc))
            if str(unit_config.get("mode") or "").casefold() == _MODE:
                fields = {"performance_pct_at_call": unit_config.get("performance_pct"), "performance_score_at_call": unit_config.get("performance_score"),
                          "performance_bets_at_call": unit_config.get("bets"), "performance_drawdown_units_at_call": unit_config.get("drawdown_units"),
                          "performance_tier_at_call": unit_config.get("tier"), "performance_source_at_call": unit_config.get("performance_source")}
                for k, v in fields.items():
                    if k not in record:
                        record[k], changed = v, True
            return changed
        nfl._ensure_signal_call_unit_snapshot = snapshot

    _migrate(core, nfl)

    @app.get("/api/performance-sizing", dependencies=[Depends(dashboard._auth)])
    def status(label: str = Query(min_length=1, max_length=120)) -> dict[str, Any]:
        if _excluded(label):
            raise HTTPException(status_code=400, detail="SH01 is excluded from performance staking")
        return {"label": label, **config(core, label, Decimal("10"))}

    @app.put("/api/performance-sizing", dependencies=[Depends(dashboard._auth)])
    def enable(payload: dict[str, Any]) -> dict[str, Any]:
        label = _clean(payload.get("label"))
        if not label:
            raise HTTPException(status_code=400, detail="label is required")
        try:
            _enable(core, nfl, label)
            return {"ok": True, "label": label, **config(core, label, Decimal("10"))}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    _install_ui(dashboard)
