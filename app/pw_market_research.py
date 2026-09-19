from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends

_INSTALLED = False

TEAM_SLUGS = {
    "ATL": ["atl"], "CHI": ["chi"], "CON": ["con"], "DAL": ["dal"],
    "GS": ["gsv", "gs"], "IND": ["ind"], "LV": ["lva", "lv"],
    "LA": ["las", "la"], "MIN": ["min"], "NY": ["nyl", "ny"],
    "PHX": ["phx"], "POR": ["por"], "SEA": ["sea"], "TOR": ["tor"],
    "WSH": ["was", "wsh"],
}


def install(*, app: Any, history: Any, ingest: Any, dashboard: Any, strategy: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_MARKET_RESEARCH_ENABLED", "true").strip().lower() in {"1","true","yes","on"}
    sample_seconds = max(1.0, float(os.getenv("PW_MARKET_SAMPLE_SECONDS", "1")))
    window_seconds = max(60, int(os.getenv("PW_MARKET_WINDOW_SECONDS", "300")))
    history_enabled = os.getenv("PW_MARKET_HISTORY_BACKTEST_ENABLED", "true").strip().lower() in {"1","true","yes","on"}
    history_sleep = max(0.02, float(os.getenv("PW_MARKET_HISTORY_REQUEST_SLEEP", "0.08")))
    lock = threading.Lock()
    live_stats: dict[str, Any] = {"sample_errors": 0, "market_match_errors": 0}
    backtest_cache: dict[str, Any] = {}

    def init_schema() -> None:
        with history._db() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS pw_market_watches (
                    alert_key TEXT PRIMARY KEY,
                    game_id TEXT,
                    pw_event_id TEXT,
                    pick TEXT,
                    pick_abbr TEXT,
                    venue TEXT,
                    quarter TEXT,
                    score_at_alert TEXT,
                    bk_ml INTEGER,
                    win_probability REAL,
                    strategy_ids TEXT,
                    same_side_call_no INTEGER,
                    event_slug TEXT,
                    market_id TEXT,
                    condition_id TEXT,
                    asset_id TEXT NOT NULL,
                    outcome_label TEXT,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pw_market_watch_expiry
                    ON pw_market_watches(status, expires_at);

                CREATE TABLE IF NOT EXISTS pw_market_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_key TEXT NOT NULL,
                    game_id TEXT,
                    sampled_at TEXT NOT NULL,
                    sampled_epoch REAL NOT NULL,
                    asset_id TEXT NOT NULL,
                    buy_price REAL,
                    sell_price REAL,
                    midpoint REAL,
                    spread REAL,
                    best_bid REAL,
                    best_bid_size REAL,
                    best_ask REAL,
                    best_ask_size REAL,
                    quarter TEXT,
                    clock TEXT,
                    away_score INTEGER,
                    home_score INTEGER,
                    source TEXT NOT NULL DEFAULT 'clob',
                    UNIQUE(alert_key, sampled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_pw_market_ticks_alert_time
                    ON pw_market_ticks(alert_key, sampled_epoch);
                CREATE INDEX IF NOT EXISTS idx_pw_market_ticks_asset_time
                    ON pw_market_ticks(asset_id, sampled_epoch);

                CREATE TABLE IF NOT EXISTS pw_market_history_map (
                    game_id TEXT NOT NULL,
                    pick_abbr TEXT NOT NULL,
                    event_slug TEXT,
                    market_id TEXT,
                    condition_id TEXT,
                    asset_id TEXT,
                    opposite_asset_id TEXT,
                    outcome_label TEXT,
                    opposite_outcome_label TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    mapped_at TEXT NOT NULL,
                    PRIMARY KEY(game_id, pick_abbr)
                );

                CREATE TABLE IF NOT EXISTS pw_market_history_points (
                    asset_id TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    price REAL NOT NULL,
                    PRIMARY KEY(asset_id, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_pw_market_history_points
                    ON pw_market_history_points(asset_id, ts);
                """
            )

    def num(v: Any) -> float | None:
        try:
            if v in (None, ""):
                return None
            x = float(v)
            return x if math.isfinite(x) else None
        except Exception:
            return None

    def level_best(levels: Any, side: str) -> tuple[float | None, float | None]:
        vals = []
        for level in levels or []:
            price = num(getattr(level, "price", None) if not isinstance(level, dict) else level.get("price"))
            size = num(getattr(level, "size", None) if not isinstance(level, dict) else level.get("size"))
            if price is not None:
                vals.append((price, size))
        if not vals:
            return None, None
        return (max(vals, key=lambda x: x[0]) if side == "bid" else min(vals, key=lambda x: x[0]))

    def latest_game_state(game_id: str) -> dict[str, Any]:
        with history._db() as con:
            r = con.execute(
                """
                SELECT quarter,clock,away_score,home_score
                FROM play_by_play
                WHERE game_id=?
                ORDER BY sequence_no DESC LIMIT 1
                """,
                (game_id,),
            ).fetchone()
        return dict(r) if r else {}

    def resolve_live_watch(event_id: str, text: str) -> None:
        if not enabled or not text:
            return
        try:
            row = strategy._pw_row(text, event_id)
            if not row:
                return
            parsed = ingest._parse_alert(text)
            event, market, outcome_label, outcome_obj = ingest._find_market(parsed)
            asset_id = getattr(outcome_obj, "token_id", None) or getattr(outcome_obj, "position_id", None)
            if not asset_id:
                raise RuntimeError("moneyline outcome has no asset id")
            game_id = str(row.get("game_id") or "")
            pick = str(row.get("predicted_winner") or "")
            abbr = history.TEAM_ABBR.get(pick)
            venue = strategy._resolve_venue(game_id, pick)
            decision = strategy._decision(text, event_id)
            matches = list(decision.get("matched_strategies") or [])
            with history._db() as con:
                prior = con.execute(
                    """
                    SELECT COUNT(*) c FROM pw_market_watches
                    WHERE game_id=? AND pick_abbr=? AND created_at < ?
                    """,
                    (game_id, abbr, datetime.now(timezone.utc).isoformat()),
                ).fetchone()["c"]
                alert_key = f"{event_id}:{asset_id}"
                con.execute(
                    """
                    INSERT OR IGNORE INTO pw_market_watches(
                        alert_key,game_id,pw_event_id,pick,pick_abbr,venue,quarter,score_at_alert,
                        bk_ml,win_probability,strategy_ids,same_side_call_no,event_slug,market_id,
                        condition_id,asset_id,outcome_label,created_at,expires_at,status
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')
                    """,
                    (
                        alert_key, game_id, event_id, pick, abbr, venue, row.get("quarter"),
                        row.get("score_at_alert"), row.get("bk_ml"), row.get("win_probability"),
                        json.dumps(matches), int(prior or 0) + 1,
                        str(getattr(event, "slug", "") or "") or None,
                        str(getattr(market, "id", "") or "") or None,
                        str(getattr(market, "condition_id", "") or getattr(market, "conditionId", "") or "") or None,
                        str(asset_id), outcome_label,
                        datetime.now(timezone.utc).isoformat(), time.time() + window_seconds,
                    ),
                )
            print(
                "PW_MARKET_WATCH "
                f"game={game_id} pick={abbr} asset={str(asset_id)[:12]} "
                f"quarter={row.get('quarter')} strategies={','.join(matches) or 'none'} "
                f"window_s={window_seconds}",
                flush=True,
            )
        except Exception as exc:
            live_stats["market_match_errors"] = int(live_stats.get("market_match_errors") or 0) + 1
            print(f"PW_MARKET_WATCH_ERROR event={event_id} error={type(exc).__name__}:{exc}", flush=True)

    def sample_loop() -> None:
        while True:
            started = time.time()
            try:
                init_schema()
                with history._db() as con:
                    con.execute(
                        "UPDATE pw_market_watches SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at<=?",
                        (time.time(),),
                    )
                    watches = [
                        dict(r) for r in con.execute(
                            "SELECT * FROM pw_market_watches WHERE status='ACTIVE' AND expires_at>?",
                            (time.time(),),
                        ).fetchall()
                    ]
                if watches:
                    with ingest.PublicClient() as client:
                        for w in watches:
                            asset = str(w["asset_id"])
                            try:
                                book = client.get_order_book(asset_id=asset)
                                buy = num(client.get_price(asset_id=asset, side="BUY"))
                                sell = num(client.get_price(asset_id=asset, side="SELL"))
                                mid = num(client.get_midpoint(asset_id=asset))
                                spread = num(client.get_spread(asset_id=asset))
                                bid, bid_size = level_best(getattr(book, "bids", None), "bid")
                                ask, ask_size = level_best(getattr(book, "asks", None), "ask")
                                state = latest_game_state(str(w.get("game_id") or ""))
                                now = datetime.now(timezone.utc)
                                sampled_at = now.isoformat(timespec="milliseconds")
                                with history._db() as con:
                                    con.execute(
                                        """
                                        INSERT OR IGNORE INTO pw_market_ticks(
                                            alert_key,game_id,sampled_at,sampled_epoch,asset_id,
                                            buy_price,sell_price,midpoint,spread,best_bid,best_bid_size,
                                            best_ask,best_ask_size,quarter,clock,away_score,home_score
                                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                        """,
                                        (
                                            w["alert_key"], w.get("game_id"), sampled_at, now.timestamp(), asset,
                                            buy,sell,mid,spread,bid,bid_size,ask,ask_size,
                                            state.get("quarter") or w.get("quarter"), state.get("clock"),
                                            state.get("away_score"),state.get("home_score"),
                                        ),
                                    )
                            except Exception as exc:
                                live_stats["sample_errors"] = int(live_stats.get("sample_errors") or 0) + 1
                                with history._db() as con:
                                    con.execute(
                                        "UPDATE pw_market_watches SET last_error=? WHERE alert_key=?",
                                        (f"{type(exc).__name__}:{exc}", w["alert_key"]),
                                    )
            except Exception as exc:
                live_stats["sample_errors"] = int(live_stats.get("sample_errors") or 0) + 1
                print(f"PW_MARKET_SAMPLE_ERROR {type(exc).__name__}:{exc}", flush=True)
            delay = max(0.05, sample_seconds - (time.time() - started))
            time.sleep(delay)

    def parse_event_ts(value: Any) -> datetime | None:
        if not value:
            return None
        s = str(value).strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
        try:
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S %z")
        except Exception:
            return None

    def json_list(v: Any) -> list[Any]:
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                x = json.loads(v)
                return x if isinstance(x, list) else []
            except Exception:
                return []
        return []

    def gamma_event(slug: str) -> dict[str, Any] | None:
        r = httpx.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=12.0,
        )
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                return data["data"][0] if data["data"] else None
            return data if data.get("markets") else None
        return None

    def event_slug_candidates(team_a: str, team_b: str, dt: datetime) -> list[str]:
        et = dt.astimezone(ZoneInfo("America/New_York"))
        dates = [(et + timedelta(days=d)).date().isoformat() for d in (0,-1,1)]
        out: list[str] = []
        for a in TEAM_SLUGS.get(team_a, [team_a.lower()]):
            for b in TEAM_SLUGS.get(team_b, [team_b.lower()]):
                for date in dates:
                    out.append(f"wnba-{a}-{b}-{date}")
                    out.append(f"wnba-{b}-{a}-{date}")
        return list(dict.fromkeys(out))

    def norm(s: Any) -> str:
        return " ".join(str(s or "").lower().replace("-", " ").split())

    def match_moneyline(event: dict[str, Any], pick_name: str) -> dict[str, Any] | None:
        markets = event.get("markets") or []
        if not isinstance(markets, list):
            return None
        scored: list[tuple[int, dict[str, Any]]] = []
        pick_norm = norm(pick_name)
        aliases = [norm(x) for x in ingest.WNBA_ALIASES.get(pick_name, ())] + [pick_norm]
        for m in markets:
            if not isinstance(m, dict):
                continue
            q = norm(m.get("question"))
            mt = norm(m.get("sportsMarketType") or m.get("marketType") or m.get("groupItemTitle"))
            outcomes = [str(x) for x in json_list(m.get("outcomes"))]
            tokens = [str(x) for x in json_list(m.get("clobTokenIds"))]
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            score = 0
            if "moneyline" in mt or mt in {"money line","winner"}:
                score += 20
            if any(x in q for x in ("spread","over ","under ","points","assists","rebounds","margin")):
                score -= 20
            if any(any(alias and alias in norm(o) for alias in aliases) for o in outcomes):
                score += 10
            if score > 0:
                scored.append((score, m))
        if not scored:
            return None
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]

    def map_history_game(game_id: str, pick_abbr: str, pick_name: str, team_a: str, team_b: str, dt: datetime) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        last_error = "event not found"
        for slug in event_slug_candidates(team_a, team_b, dt):
            try:
                event = gamma_event(slug)
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                time.sleep(history_sleep)
                continue
            if not event:
                continue
            market = match_moneyline(event, pick_name)
            if not market:
                last_error = f"no moneyline market in {slug}"
                continue
            outcomes = [str(x) for x in json_list(market.get("outcomes"))]
            tokens = [str(x) for x in json_list(market.get("clobTokenIds"))]
            aliases = [norm(x) for x in ingest.WNBA_ALIASES.get(pick_name, ())] + [norm(pick_name)]
            idx = next((i for i,o in enumerate(outcomes) if any(a and a in norm(o) for a in aliases)), None)
            if idx is None:
                last_error = f"pick outcome not found in {slug}"
                continue
            other = 1 - idx
            result = {
                "game_id":game_id,"pick_abbr":pick_abbr,"event_slug":slug,
                "market_id":str(market.get("id") or ""),
                "condition_id":str(market.get("conditionId") or market.get("condition_id") or ""),
                "asset_id":tokens[idx],"opposite_asset_id":tokens[other],
                "outcome_label":outcomes[idx],"opposite_outcome_label":outcomes[other],
                "status":"OK","error":None,"mapped_at":now,
            }
            with history._db() as con:
                con.execute(
                    """
                    INSERT OR REPLACE INTO pw_market_history_map(
                        game_id,pick_abbr,event_slug,market_id,condition_id,asset_id,
                        opposite_asset_id,outcome_label,opposite_outcome_label,status,error,mapped_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    tuple(result[k] for k in (
                        "game_id","pick_abbr","event_slug","market_id","condition_id","asset_id",
                        "opposite_asset_id","outcome_label","opposite_outcome_label","status","error","mapped_at"
                    )),
                )
            return result
        result = {
            "game_id":game_id,"pick_abbr":pick_abbr,"event_slug":None,"market_id":None,
            "condition_id":None,"asset_id":None,"opposite_asset_id":None,"outcome_label":None,
            "opposite_outcome_label":None,"status":"ERROR","error":last_error,"mapped_at":now,
        }
        with history._db() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO pw_market_history_map(
                    game_id,pick_abbr,event_slug,market_id,condition_id,asset_id,
                    opposite_asset_id,outcome_label,opposite_outcome_label,status,error,mapped_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                tuple(result[k] for k in (
                    "game_id","pick_abbr","event_slug","market_id","condition_id","asset_id",
                    "opposite_asset_id","outcome_label","opposite_outcome_label","status","error","mapped_at"
                )),
            )
        return result

    def fetch_history(asset_id: str, start_ts: int, end_ts: int) -> list[tuple[int,float]]:
        with history._db() as con:
            cached = [
                (int(r["ts"]), float(r["price"]))
                for r in con.execute(
                    "SELECT ts,price FROM pw_market_history_points WHERE asset_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                    (asset_id,start_ts,end_ts),
                ).fetchall()
            ]
        if cached and cached[0][0] <= start_ts + 90 and cached[-1][0] >= end_ts - 90:
            return cached
        r = httpx.get(
            "https://clob.polymarket.com/prices-history",
            params={"market":asset_id,"startTs":start_ts,"endTs":end_ts,"fidelity":1},
            timeout=20.0,
        )
        r.raise_for_status()
        payload = r.json()
        pts = payload.get("history") if isinstance(payload, dict) else payload
        out: list[tuple[int,float]] = []
        for p in pts or []:
            try:
                ts = int(p.get("t") or p.get("timestamp"))
                price = float(p.get("p") or p.get("price"))
                if 0 < price < 1:
                    out.append((ts,price))
            except Exception:
                continue
        with history._db() as con:
            con.executemany(
                "INSERT OR IGNORE INTO pw_market_history_points(asset_id,ts,price) VALUES(?,?,?)",
                [(asset_id,t,p) for t,p in out],
            )
        return sorted(out)

    def nearest_at_or_after(points: list[tuple[int,float]], ts: int, max_delay: int = 90) -> tuple[int,float] | None:
        for t,p in points:
            if t >= ts and t - ts <= max_delay:
                return t,p
        return None

    def simulate(points: list[tuple[int,float]], call_ts: int, entry_mode: str, tp: float, sl: float, horizon: int) -> dict[str, Any] | None:
        start = nearest_at_or_after(points, call_ts)
        if not start:
            return None
        entry_t, base = start
        entry = base
        if entry_mode.startswith("dip"):
            dip = float(entry_mode.replace("dip","")) / 100.0
            found = None
            for t,p in points:
                if t < entry_t:
                    continue
                if t > call_ts + 180:
                    break
                if p <= base - dip:
                    found = (t,p)
                    break
            if not found:
                return None
            entry_t,entry = found
        end_t = entry_t + horizon
        window = [(t,p) for t,p in points if entry_t <= t <= end_t]
        if not window:
            return None
        mfe = max(p-entry for _,p in window)
        mae = min(p-entry for _,p in window)
        exit_t, exit_p, reason = window[-1][0], window[-1][1], "TIME"
        for t,p in window[1:]:
            if p - entry >= tp:
                exit_t,exit_p,reason=t,p,"TP"
                break
            if p - entry <= -sl:
                exit_t,exit_p,reason=t,p,"SL"
                break
        stake=100.0
        pnl=stake*((exit_p/entry)-1.0)
        return {
            "entry_t":entry_t,"entry_price":entry,"exit_t":exit_t,"exit_price":exit_p,
            "hold_s":exit_t-entry_t,"reason":reason,"pnl":pnl,"roi_pct":pnl,
            "mfe_cents":mfe*100.0,"mae_cents":mae*100.0,
        }

    def summarize_trades(trades: list[dict[str, Any]]) -> dict[str, Any]:
        if not trades:
            return {"trades":0}
        pnl=[float(x["pnl"]) for x in trades]
        equity=0.0; peak=0.0; max_dd=0.0
        for x in trades:
            equity += float(x["pnl"])
            peak=max(peak,equity)
            max_dd=max(max_dd,peak-equity)
        return {
            "trades":len(trades),
            "wins":sum(1 for x in trades if x["pnl"]>0),
            "losses":sum(1 for x in trades if x["pnl"]<0),
            "pnl_usdc":round(sum(pnl),2),
            "roi_pct":round(sum(pnl)/(100.0*len(trades))*100.0,2),
            "avg_trade_pct":round(mean([x["roi_pct"] for x in trades]),2),
            "avg_hold_s":round(mean([x["hold_s"] for x in trades]),1),
            "avg_mfe_cents":round(mean([x["mfe_cents"] for x in trades]),2),
            "avg_mae_cents":round(mean([x["mae_cents"] for x in trades]),2),
            "max_drawdown_usdc":round(max_dd,2),
            "tp_exits":sum(1 for x in trades if x["reason"]=="TP"),
            "sl_exits":sum(1 for x in trades if x["reason"]=="SL"),
            "time_exits":sum(1 for x in trades if x["reason"]=="TIME"),
        }

    def run_historical_backtest() -> dict[str, Any]:
        if not history_enabled:
            return {"enabled":False}
        with lock:
            with history._db() as con:
                alerts = [
                    dict(r) for r in con.execute(
                        """
                        SELECT a.id,a.event_ts,a.game_id,a.predicted_winner,a.predicted_winner_abbr,
                               a.quarter,a.bk_ml,a.result,g.team_a,g.team_b
                        FROM alerts a JOIN games g ON g.game_id=a.game_id
                        WHERE a.backtest_eligible=1
                        ORDER BY a.event_ts,a.id
                        """
                    ).fetchall()
                ]
                maps = {
                    (str(r["game_id"]),str(r["pick_abbr"])):dict(r)
                    for r in con.execute("SELECT * FROM pw_market_history_map").fetchall()
                }

            side_count: dict[tuple[str,str],int]=defaultdict(int)
            prepared=[]
            unique={}
            for a in alerts:
                dt=parse_event_ts(a.get("event_ts"))
                if not dt:
                    continue
                key=(str(a["game_id"]),str(a["predicted_winner_abbr"]))
                side_count[key]+=1
                a["same_side_call_no"]=side_count[key]
                unique[key]=(a,dt)

            mapped=0; map_errors=0
            items=list(unique.items())
            for idx,(key,(a,dt)) in enumerate(items, start=1):
                existing=maps.get(key)
                if existing and existing.get("status")=="OK" and existing.get("asset_id"):
                    if idx % 25 == 0:
                        print(f"PW_MARKET_MAP_PROGRESS done={idx}/{len(items)} ok={sum(1 for x in maps.values() if x.get('status')=='OK')} errors={sum(1 for x in maps.values() if x.get('status')!='OK')}", flush=True)
                    continue

                # If the other side of this same game is already mapped, derive
                # this side by swapping the two binary moneyline outcome tokens.
                sibling = next(
                    (
                        x for (gid,_),x in maps.items()
                        if gid == str(a["game_id"])
                        and x.get("status") == "OK"
                        and x.get("asset_id")
                        and x.get("opposite_asset_id")
                    ),
                    None,
                )
                if sibling:
                    m={
                        "game_id":str(a["game_id"]),
                        "pick_abbr":str(a["predicted_winner_abbr"]),
                        "event_slug":sibling.get("event_slug"),
                        "market_id":sibling.get("market_id"),
                        "condition_id":sibling.get("condition_id"),
                        "asset_id":sibling.get("opposite_asset_id"),
                        "opposite_asset_id":sibling.get("asset_id"),
                        "outcome_label":sibling.get("opposite_outcome_label"),
                        "opposite_outcome_label":sibling.get("outcome_label"),
                        "status":"OK","error":None,"mapped_at":datetime.now(timezone.utc).isoformat(),
                    }
                    with history._db() as con:
                        con.execute(
                            """
                            INSERT OR REPLACE INTO pw_market_history_map(
                                game_id,pick_abbr,event_slug,market_id,condition_id,asset_id,
                                opposite_asset_id,outcome_label,opposite_outcome_label,status,error,mapped_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            tuple(m[k] for k in (
                                "game_id","pick_abbr","event_slug","market_id","condition_id","asset_id",
                                "opposite_asset_id","outcome_label","opposite_outcome_label","status","error","mapped_at"
                            )),
                        )
                else:
                    m=map_history_game(
                        str(a["game_id"]),str(a["predicted_winner_abbr"]),str(a["predicted_winner"]),
                        str(a["team_a"]),str(a["team_b"]),dt
                    )
                    time.sleep(history_sleep)
                maps[key]=m
                if m.get("status")=="OK": mapped+=1
                else: map_errors+=1
                if idx % 25 == 0:
                    print(f"PW_MARKET_MAP_PROGRESS done={idx}/{len(items)} ok={sum(1 for x in maps.values() if x.get('status')=='OK')} errors={sum(1 for x in maps.values() if x.get('status')!='OK')}", flush=True)

            series_cache: dict[tuple[str,int,int],list[tuple[int,float]]]={}
            price_errors=0
            for a in alerts:
                dt=parse_event_ts(a.get("event_ts"))
                if not dt: continue
                key=(str(a["game_id"]),str(a["predicted_winner_abbr"]))
                m=maps.get(key)
                if not m or m.get("status")!="OK": continue
                call_ts=int(dt.timestamp())
                start=call_ts-120; end=call_ts+600
                try:
                    pk=(str(m["asset_id"]),start,end)
                    points=series_cache.get(pk)
                    if points is None:
                        points=fetch_history(str(m["asset_id"]),start,end); series_cache[pk]=points
                        time.sleep(history_sleep)
                    oppk=(str(m["opposite_asset_id"]),start,end)
                    opp=series_cache.get(oppk)
                    if opp is None:
                        opp=fetch_history(str(m["opposite_asset_id"]),start,end); series_cache[oppk]=opp
                        time.sleep(history_sleep)
                except Exception:
                    price_errors+=1
                    continue
                a["_dt"]=dt; a["_call_ts"]=call_ts; a["_map"]=m; a["_points"]=points; a["_opp_points"]=opp
                prepared.append(a)

            def is_away(a: dict[str,Any]) -> bool:
                return str(a.get("predicted_winner_abbr"))==str(a.get("team_a"))
            def early_q4(a: dict[str,Any]) -> bool:
                return str(a.get("quarter"))=="Q4"
            strategies={
                "immediate_pw": lambda a:(a["_points"],"immediate"),
                "early_q4_fade": lambda a:(a["_opp_points"],"immediate") if early_q4(a) else None,
                "away_underdog_fade": lambda a:(a["_opp_points"],"immediate") if is_away(a) and float(a.get("bk_ml") or 0)>0 else None,
                "repeat_pw_dip3": lambda a:(a["_points"],"dip3") if int(a.get("same_side_call_no") or 0)>=2 else None,
                "repeat_pw_dip5": lambda a:(a["_points"],"dip5") if int(a.get("same_side_call_no") or 0)>=2 else None,
            }
            results={}
            for name,selector in strategies.items():
                configs=[]
                for tp_c in (3,5,8):
                    for sl_c in (3,5,8):
                        for horizon in (60,120,180,300):
                            trades=[]
                            for a in prepared:
                                selected=selector(a)
                                if not selected: continue
                                points,mode=selected
                                sim=simulate(points,int(a["_call_ts"]),mode,tp_c/100.0,sl_c/100.0,horizon)
                                if sim:
                                    sim["call_ts"]=a["_call_ts"]; trades.append(sim)
                            stats=summarize_trades(trades)
                            configs.append({
                                "tp_cents":tp_c,"sl_cents":sl_c,"horizon_s":horizon,
                                **stats,
                            })
                configs.sort(key=lambda x:(float(x.get("roi_pct") or -999),int(x.get("trades") or 0)), reverse=True)
                results[name]={"best":configs[:5],"all_configs":configs}

            output={
                "generated_at":datetime.now(timezone.utc).isoformat(),
                "method":"historical Polymarket midpoint proxy; 1-minute fidelity where available",
                "alerts":len(alerts),
                "unique_game_sides":len(unique),
                "prepared_calls":len(prepared),
                "market_maps_ok":sum(1 for x in maps.values() if x.get("status")=="OK"),
                "market_maps_error":sum(1 for x in maps.values() if x.get("status")!="OK"),
                "price_errors":price_errors,
                "strategies":results,
            }
            backtest_cache.clear(); backtest_cache.update(output)
            print(
                "PW_MARKET_BACKTEST "
                f"alerts={len(alerts)} prepared={len(prepared)} maps_ok={output['market_maps_ok']} "
                f"maps_error={output['market_maps_error']} price_errors={price_errors}",
                flush=True,
            )
            for name,v in results.items():
                b=(v.get("best") or [{}])[0]
                print(
                    "PW_MARKET_BACKTEST_BEST "
                    f"strategy={name} trades={b.get('trades',0)} roi={b.get('roi_pct')} "
                    f"pnl={b.get('pnl_usdc')} tp={b.get('tp_cents')} sl={b.get('sl_cents')} "
                    f"horizon={b.get('horizon_s')} dd={b.get('max_drawdown_usdc')}",
                    flush=True,
                )
            return output

    _original_save_alert = ingest._save_alert
    def save_alert_with_market_watch(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        saved = _original_save_alert(event_id, payload)
        text = str((payload or {}).get("text") or "")
        if text and enabled:
            threading.Thread(
                target=resolve_live_watch,args=(str(event_id),text),
                name=f"pw-market-watch-{str(event_id)[:12]}",daemon=True
            ).start()
        return saved
    ingest._save_alert = save_alert_with_market_watch

    @app.get("/api/pw-market-research/status", dependencies=[Depends(dashboard._auth)])
    def market_research_status():
        init_schema()
        with history._db() as con:
            active=con.execute("SELECT COUNT(*) c FROM pw_market_watches WHERE status='ACTIVE' AND expires_at>?",(time.time(),)).fetchone()["c"]
            watches=con.execute("SELECT COUNT(*) c FROM pw_market_watches").fetchone()["c"]
            ticks=con.execute("SELECT COUNT(*) c FROM pw_market_ticks").fetchone()["c"]
            maps_ok=con.execute("SELECT COUNT(*) c FROM pw_market_history_map WHERE status='OK'").fetchone()["c"]
            hist_pts=con.execute("SELECT COUNT(*) c FROM pw_market_history_points").fetchone()["c"]
        return {
            "enabled":enabled,"sample_seconds":sample_seconds,"window_seconds":window_seconds,
            "history_backtest_enabled":history_enabled,"active_watches":int(active or 0),
            "watches":int(watches or 0),"ticks":int(ticks or 0),"history_maps_ok":int(maps_ok or 0),
            "history_points":int(hist_pts or 0),"live_stats":dict(live_stats),
            "backtest_summary":{
                "generated_at":backtest_cache.get("generated_at"),
                "prepared_calls":backtest_cache.get("prepared_calls"),
                "market_maps_ok":backtest_cache.get("market_maps_ok"),
                "market_maps_error":backtest_cache.get("market_maps_error"),
            } if backtest_cache else None,
        }

    @app.get("/api/pw-market-research/backtest", dependencies=[Depends(dashboard._auth)])
    def market_backtest_result():
        return backtest_cache or {"status":"not_ready"}

    @app.post("/api/pw-market-research/backtest/recompute", dependencies=[Depends(dashboard._auth)])
    def market_backtest_recompute():
        threading.Thread(target=run_historical_backtest,name="pw-market-backtest-manual",daemon=True).start()
        return {"started":True}

    init_schema()
    if enabled:
        threading.Thread(target=sample_loop,name="pw-market-sampler",daemon=True).start()
    if history_enabled:
        def boot_backtest() -> None:
            time.sleep(20.0)
            try:
                run_historical_backtest()
            except Exception as exc:
                print(f"PW_MARKET_BACKTEST_ERROR {type(exc).__name__}:{exc}", flush=True)
        threading.Thread(target=boot_backtest,name="pw-market-backtest",daemon=True).start()
    print(
        "PW_MARKET_RESEARCH_READY "
        f"enabled={enabled} sample_seconds={sample_seconds} window_seconds={window_seconds} "
        f"history_backtest={history_enabled} research_only=true",
        flush=True,
    )
