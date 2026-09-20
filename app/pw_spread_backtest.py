from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

import httpx

_INSTALLED = False

TEAM_SLUGS = {
    "ATL": ["atl"], "CHI": ["chi"], "CON": ["conn", "con"], "DAL": ["dal"],
    "GS": ["gsv", "gs"], "IND": ["ind"], "LV": ["las", "lva", "lv"],
    "LA": ["la", "las"], "MIN": ["min"], "NY": ["nyl", "ny"],
    "PHX": ["phx"], "POR": ["por"], "SEA": ["sea"], "TOR": ["tor"],
    "WSH": ["was", "wsh"],
}


def install(*, history: Any, ingest: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_SPREAD_BACKTEST_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        print("PW_SPREAD_BACKTEST_READY enabled=false research_only=true", flush=True)
        return

    request_sleep = max(0.02, float(os.getenv("PW_SPREAD_BACKTEST_REQUEST_SLEEP", "0.05")))
    max_lag = max(30, int(os.getenv("PW_SPREAD_BACKTEST_MAX_PRICE_LAG_SECONDS", "120")))
    stake = 100.0
    name_by_abbr = {abbr: name for name, abbr in history.TEAM_ABBR.items()}

    def parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        text = str(value).strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None

    def json_list(value: Any) -> list[Any]:
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except Exception:
                return []
        return []

    def num(value: Any) -> float | None:
        try:
            if value in (None, ""):
                return None
            x = float(value)
            return x if math.isfinite(x) else None
        except Exception:
            return None

    def norm(value: Any) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())

    def aliases(abbr: str) -> list[str]:
        full = name_by_abbr.get(abbr, abbr)
        vals = [full, abbr]
        try:
            vals.extend(list(ingest.WNBA_ALIASES.get(full, ())))
        except Exception:
            pass
        bits = norm(full).split()
        if bits:
            vals.append(bits[-1])
        return list(dict.fromkeys(norm(v) for v in vals if norm(v)))

    def matches_team(label: Any, abbr: str) -> bool:
        text = norm(label)
        return bool(text) and any(a == text or (len(a) >= 4 and a in text) for a in aliases(abbr))

    def event_slug_candidates(team_a: str, team_b: str, dt: datetime) -> list[str]:
        et = dt.astimezone(ZoneInfo("America/New_York"))
        dates = [(et + timedelta(days=d)).date().isoformat() for d in (0, -1, 1)]
        out: list[str] = []
        for a in TEAM_SLUGS.get(team_a, [team_a.lower()]):
            for b in TEAM_SLUGS.get(team_b, [team_b.lower()]):
                for date in dates:
                    out.append(f"wnba-{a}-{b}-{date}")
                    out.append(f"wnba-{b}-{a}-{date}")
        return list(dict.fromkeys(out))

    def gamma_event(slug: str) -> dict[str, Any] | None:
        response = httpx.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, timeout=15.0)
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"][0] if data["data"] else None
        return data if isinstance(data, dict) and data.get("markets") else None

    def threshold(market: dict[str, Any]) -> float | None:
        direct = num(market.get("groupItemThreshold"))
        if direct is not None:
            return direct
        title = str(market.get("groupItemTitle") or "")
        match = re.search(r"[-+]?\d+(?:\.\d+)?", title)
        if match:
            return num(match.group(0))
        question = str(market.get("question") or "")
        match = re.search(r"[-+]\d+(?:\.\d+)?", question)
        return num(match.group(0)) if match else None

    def spread_markets(event: dict[str, Any], team_a: str, team_b: str) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        unmatched = 0
        for m in event.get("markets") or []:
            if not isinstance(m, dict):
                continue
            market_type = str(m.get("sportsMarketType") or "").strip().lower()
            title_norm = norm(m.get("groupItemTitle"))
            if market_type != "spreads" and "spread" not in title_norm:
                continue
            outcomes = [str(x) for x in json_list(m.get("outcomes"))]
            tokens = [str(x) for x in json_list(m.get("clobTokenIds"))]
            resolved_prices = [num(x) for x in json_list(m.get("outcomePrices"))]
            line0 = threshold(m)
            if len(outcomes) != 2 or len(tokens) != 2 or line0 is None:
                unmatched += 1
                continue

            idx_a = next((i for i, label in enumerate(outcomes) if matches_team(label, team_a)), None)
            idx_b = next((i for i, label in enumerate(outcomes) if matches_team(label, team_b)), None)

            # Some sports spread markets are Yes/No. In that case, infer the named
            # team from the question and assign Yes to that team, No to the opponent.
            if idx_a is None or idx_b is None:
                q = str(m.get("question") or "")
                yes_idx = next((i for i, x in enumerate(outcomes) if norm(x) == "yes"), None)
                no_idx = next((i for i, x in enumerate(outcomes) if norm(x) == "no"), None)
                if yes_idx is not None and no_idx is not None:
                    if matches_team(q, team_a):
                        idx_a, idx_b = yes_idx, no_idx
                    elif matches_team(q, team_b):
                        idx_b, idx_a = yes_idx, no_idx

            if idx_a is None or idx_b is None or idx_a == idx_b:
                unmatched += 1
                continue

            lines = [line0, -line0]
            out.append({
                "event_slug": event.get("slug"),
                "market_id": str(m.get("id") or ""),
                "question": str(m.get("question") or ""),
                "group_title": str(m.get("groupItemTitle") or ""),
                "a_token": tokens[idx_a],
                "b_token": tokens[idx_b],
                "a_line": float(lines[idx_a]),
                "b_line": float(lines[idx_b]),
                "a_resolved": resolved_prices[idx_a] if idx_a < len(resolved_prices) else None,
                "b_resolved": resolved_prices[idx_b] if idx_b < len(resolved_prices) else None,
            })
        return out, unmatched

    price_cache: dict[tuple[str, int, int], list[tuple[int, float]]] = {}

    def price_points(token: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
        key = (token, start_ts, end_ts)
        if key in price_cache:
            return price_cache[key]

        with history._db() as con:
            rows = con.execute(
                "SELECT ts,price FROM pw_market_history_points WHERE asset_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                (token, start_ts, end_ts),
            ).fetchall()
        cached = [(int(r["ts"]), float(r["price"])) for r in rows]
        if cached and cached[0][0] <= start_ts + max_lag and cached[-1][0] >= end_ts - max_lag:
            price_cache[key] = cached
            return cached

        try:
            response = httpx.get(
                "https://clob.polymarket.com/prices-history",
                params={"market": token, "startTs": start_ts, "endTs": end_ts, "fidelity": 1},
                timeout=20.0,
            )
            response.raise_for_status()
            payload = response.json()
            raw = payload.get("history") if isinstance(payload, dict) else payload
            fresh: list[tuple[int, float]] = []
            for point in raw or []:
                if not isinstance(point, dict):
                    continue
                ts = point.get("t", point.get("timestamp"))
                px = point.get("p", point.get("price"))
                try:
                    its = int(float(ts))
                    fpx = float(px)
                    if 0.0 <= fpx <= 1.0:
                        fresh.append((its, fpx))
                except Exception:
                    continue
            if fresh:
                with history._db() as con:
                    con.executemany(
                        "INSERT OR IGNORE INTO pw_market_history_points(asset_id,ts,price) VALUES(?,?,?)",
                        [(token, t, p) for t, p in fresh],
                    )
                cached = sorted(set(cached + fresh))
            time.sleep(request_sleep)
        except Exception as exc:
            print(f"PW_SPREAD_PRICE_ERROR token={token[:12]} error={type(exc).__name__}:{exc}", flush=True)

        price_cache[key] = cached
        return cached

    def at_price(points: list[tuple[int, float]], target: int) -> tuple[float | None, int | None]:
        before = [(t, p) for t, p in points if t <= target and target - t <= max_lag]
        if before:
            t, p = before[-1]
            return p, target - t
        after = [(t, p) for t, p in points if t > target and t - target <= max_lag]
        if after:
            t, p = after[0]
            return p, t - target
        return None, None

    def realized_win(value: Any) -> bool | None:
        p = num(value)
        if p is None:
            return None
        if p >= 0.99:
            return True
        if p <= 0.01:
            return False
        return None

    def pnl(price: float, won: bool) -> float:
        if won:
            return stake * (1.0 / price - 1.0)
        return -stake

    def aggregate(records: list[dict[str, Any]], side: str = "pw") -> dict[str, Any]:
        key_win = "pw_spread_win" if side == "pw" else "fade_spread_win"
        key_px = "pw_price" if side == "pw" else "fade_price"
        key_pnl = "pw_pnl" if side == "pw" else "fade_pnl"
        rows = [r for r in records if r.get(key_win) is not None and r.get(key_px) is not None]
        if not rows:
            return {"trades": 0}
        wins = sum(1 for r in rows if r[key_win])
        profit = sum(float(r[key_pnl]) for r in rows)
        return {
            "trades": len(rows),
            "wins": wins,
            "losses": len(rows) - wins,
            "win_pct": round(100.0 * wins / len(rows), 2),
            "pnl_100": round(profit, 2),
            "roi_pct": round(100.0 * profit / (stake * len(rows)), 2),
            "avg_entry": round(mean(float(r[key_px]) for r in rows), 4),
        }

    def run() -> None:
        started = time.time()
        print("PW_SPREAD_BACKTEST_START research_only=true", flush=True)
        with history._db() as con:
            alerts = [
                dict(r) for r in con.execute(
                    """
                    SELECT a.id,a.event_ts,a.game_id,a.predicted_winner,a.predicted_winner_abbr,
                           a.quarter,a.win_probability,a.bk_ml,a.live_spread,a.bk_spread,a.result,
                           g.team_a,g.team_b,g.score_a,g.score_b
                    FROM alerts a
                    JOIN games g ON g.game_id=a.game_id
                    WHERE a.backtest_eligible=1
                    ORDER BY a.event_ts,a.id
                    """
                ).fetchall()
            ]

        by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for a in alerts:
            by_game[str(a["game_id"])].append(a)

        calls_by_side: dict[tuple[str, str], int] = defaultdict(int)
        records: list[dict[str, Any]] = []
        games_event_found = 0
        games_with_spread = 0
        total_spread_markets = 0
        unmatched_spread_markets = 0
        market_count_hist: dict[int, int] = defaultdict(int)
        line_values: dict[float, int] = defaultdict(int)
        price_errors = 0

        for game_no, (game_id, game_alerts) in enumerate(by_game.items(), start=1):
            first_dt = next((parse_dt(a.get("event_ts")) for a in game_alerts if parse_dt(a.get("event_ts"))), None)
            if first_dt is None:
                continue
            team_a = str(game_alerts[0].get("team_a") or "")
            team_b = str(game_alerts[0].get("team_b") or "")
            event = None
            for slug in event_slug_candidates(team_a, team_b, first_dt):
                try:
                    event = gamma_event(slug)
                except Exception:
                    time.sleep(request_sleep)
                    continue
                if event:
                    break
            if not event:
                print(f"PW_SPREAD_GAME game={game_id} status=no_event", flush=True)
                continue

            games_event_found += 1
            markets, unmatched = spread_markets(event, team_a, team_b)
            unmatched_spread_markets += unmatched
            market_count_hist[len(markets)] += 1
            if not markets:
                print(f"PW_SPREAD_GAME game={game_id} status=no_spread event={event.get('slug')}", flush=True)
                continue

            games_with_spread += 1
            total_spread_markets += len(markets)
            for m in markets:
                line_values[abs(float(m["a_line"]))] += 1

            times = [int(parse_dt(a["event_ts"]).timestamp()) for a in game_alerts if parse_dt(a.get("event_ts"))]
            if not times:
                continue
            start_ts, end_ts = min(times) - max_lag, max(times) + max_lag

            token_points: dict[str, list[tuple[int, float]]] = {}
            for m in markets:
                for token in (m["a_token"], m["b_token"]):
                    if token not in token_points:
                        pts = price_points(token, start_ts, end_ts)
                        token_points[token] = pts
                        if not pts:
                            price_errors += 1

            for alert in game_alerts:
                dt = parse_dt(alert.get("event_ts"))
                if dt is None:
                    continue
                ts = int(dt.timestamp())
                pick = str(alert.get("predicted_winner_abbr") or "")
                if pick not in {team_a, team_b}:
                    continue
                calls_by_side[(game_id, pick)] += 1
                call_no = calls_by_side[(game_id, pick)]

                candidates: list[dict[str, Any]] = []
                for m in markets:
                    if pick == team_a:
                        own_token, opp_token = m["a_token"], m["b_token"]
                        own_line = float(m["a_line"])
                        own_resolved, opp_resolved = m["a_resolved"], m["b_resolved"]
                    else:
                        own_token, opp_token = m["b_token"], m["a_token"]
                        own_line = float(m["b_line"])
                        own_resolved, opp_resolved = m["b_resolved"], m["a_resolved"]
                    own_px, own_lag = at_price(token_points.get(own_token, []), ts)
                    opp_px, opp_lag = at_price(token_points.get(opp_token, []), ts)
                    if own_px is None or opp_px is None:
                        continue
                    own_win = realized_win(own_resolved)
                    opp_win = realized_win(opp_resolved)
                    if own_win is None or opp_win is None:
                        continue
                    candidates.append({
                        "line": own_line,
                        "own_px": own_px,
                        "opp_px": opp_px,
                        "own_lag": own_lag,
                        "opp_lag": opp_lag,
                        "own_win": own_win,
                        "opp_win": opp_win,
                        "market": m,
                    })

                if not candidates:
                    continue

                # Main/live spread = actually listed market closest to 50/50 at this exact PW signal.
                chosen = min(candidates, key=lambda c: (abs(float(c["own_px"]) - 0.5), abs(float(c["line"]))))
                live = num(alert.get("live_spread"))
                rec = {
                    "alert_id": alert.get("id"),
                    "game_id": game_id,
                    "pick": pick,
                    "quarter": str(alert.get("quarter") or "").upper(),
                    "bk_ml": num(alert.get("bk_ml")),
                    "pw_result": str(alert.get("result") or ""),
                    "same_side_call_no": call_no,
                    "poly_line": float(chosen["line"]),
                    "pw_price": float(chosen["own_px"]),
                    "fade_price": float(chosen["opp_px"]),
                    "pw_spread_win": bool(chosen["own_win"]),
                    "fade_spread_win": bool(chosen["opp_win"]),
                    "pw_pnl": pnl(float(chosen["own_px"]), bool(chosen["own_win"])),
                    "fade_pnl": pnl(float(chosen["opp_px"]), bool(chosen["opp_win"])),
                    "live_spread": live,
                    "line_diff_vs_pw_live": (float(chosen["line"]) - live) if live is not None else None,
                    "available_spread_markets": len(markets),
                    "event_slug": event.get("slug"),
                    "group_title": chosen["market"].get("group_title"),
                }
                records.append(rec)

            if game_no % 20 == 0:
                print(
                    f"PW_SPREAD_PROGRESS games={game_no}/{len(by_game)} events={games_event_found} "
                    f"games_with_spread={games_with_spread} records={len(records)}",
                    flush=True,
                )

        def filt(pred):
            return [r for r in records if pred(r)]

        strategies = {
            "all_pw": ("pw", records),
            "all_fade": ("fade", records),
            "first_call_pw": ("pw", filt(lambda r: r["same_side_call_no"] == 1)),
            "repeat_call_pw": ("pw", filt(lambda r: r["same_side_call_no"] >= 2)),
            "q1_pw": ("pw", filt(lambda r: r["quarter"] == "Q1")),
            "q2_pw": ("pw", filt(lambda r: r["quarter"] == "Q2")),
            "q3_pw": ("pw", filt(lambda r: r["quarter"] == "Q3")),
            "q4_pw": ("pw", filt(lambda r: r["quarter"] == "Q4")),
            "q3_fade": ("fade", filt(lambda r: r["quarter"] == "Q3")),
            "q4_fade": ("fade", filt(lambda r: r["quarter"] == "Q4")),
            "plus_money_pw": ("pw", filt(lambda r: r["bk_ml"] is not None and r["bk_ml"] > 0)),
            "favorite_pw": ("pw", filt(lambda r: r["bk_ml"] is not None and r["bk_ml"] < 0)),
            "poly_at_least_as_good_as_pw_live": (
                "pw",
                filt(lambda r: r["line_diff_vs_pw_live"] is not None and r["line_diff_vs_pw_live"] >= 0),
            ),
            "poly_2pts_better_than_pw_live": (
                "pw",
                filt(lambda r: r["line_diff_vs_pw_live"] is not None and r["line_diff_vs_pw_live"] >= 2),
            ),
            "exact_pw_live_line": (
                "pw",
                filt(lambda r: r["line_diff_vs_pw_live"] is not None and abs(r["line_diff_vs_pw_live"]) < 0.01),
            ),
            "within_1pt_pw_live": (
                "pw",
                filt(lambda r: r["line_diff_vs_pw_live"] is not None and abs(r["line_diff_vs_pw_live"]) <= 1.0),
            ),
        }

        pw_winner_rows = filt(lambda r: r["pw_result"] == "W")
        pw_loser_rows = filt(lambda r: r["pw_result"] == "L")
        diagnostic = {
            "pw_ml_winners_on_spread": aggregate(pw_winner_rows, "pw"),
            "pw_ml_losers_on_spread": aggregate(pw_loser_rows, "pw"),
        }

        live_rows = [r for r in records if r.get("live_spread") is not None]
        exact = sum(1 for r in live_rows if abs(float(r["line_diff_vs_pw_live"])) < 0.01)
        within1 = sum(1 for r in live_rows if abs(float(r["line_diff_vs_pw_live"])) <= 1.0)
        within2 = sum(1 for r in live_rows if abs(float(r["line_diff_vs_pw_live"])) <= 2.0)
        favorable = sum(1 for r in live_rows if float(r["line_diff_vs_pw_live"]) >= 0)

        availability = {
            "games_total": len(by_game),
            "events_found": games_event_found,
            "games_with_any_spread": games_with_spread,
            "games_without_spread": games_event_found - games_with_spread,
            "spread_markets_total": total_spread_markets,
            "avg_spread_markets_per_spread_game": round(total_spread_markets / games_with_spread, 2) if games_with_spread else 0,
            "market_count_hist": dict(sorted(market_count_hist.items())),
            "unmatched_spread_markets": unmatched_spread_markets,
            "alerts_total": len(alerts),
            "alerts_with_executable_spread_price": len(records),
            "live_spread_comparable_calls": len(live_rows),
            "exact_pw_live_line_calls": exact,
            "within_1pt_pw_live_calls": within1,
            "within_2pt_pw_live_calls": within2,
            "poly_line_at_least_as_favorable_calls": favorable,
            "price_tokens_without_history": price_errors,
            "line_abs_frequency": {str(k): v for k, v in sorted(line_values.items())},
        }

        print("PW_SPREAD_AVAILABILITY " + json.dumps(availability, sort_keys=True), flush=True)
        for name, (side, rows) in strategies.items():
            stats = aggregate(rows, side)
            print(f"PW_SPREAD_STRATEGY name={name} side={side} " + " ".join(f"{k}={v}" for k, v in stats.items()), flush=True)
        print("PW_SPREAD_DIAGNOSTIC " + json.dumps(diagnostic, sort_keys=True), flush=True)

        best = sorted(
            [
                (name, aggregate(rows, side))
                for name, (side, rows) in strategies.items()
                if aggregate(rows, side).get("trades", 0) >= 30
            ],
            key=lambda x: float(x[1].get("roi_pct", -999)),
            reverse=True,
        )
        print("PW_SPREAD_RANKED " + json.dumps(best, sort_keys=True), flush=True)
        print(
            f"PW_SPREAD_BACKTEST_DONE records={len(records)} games={len(by_game)} "
            f"elapsed_s={round(time.time()-started,1)} research_only=true",
            flush=True,
        )

    def boot() -> None:
        time.sleep(25.0)
        try:
            run()
        except Exception as exc:
            print(f"PW_SPREAD_BACKTEST_ERROR {type(exc).__name__}:{exc}", flush=True)

    threading.Thread(target=boot, name="pw-spread-backtest", daemon=True).start()
    print("PW_SPREAD_BACKTEST_READY enabled=true research_only=true", flush=True)
