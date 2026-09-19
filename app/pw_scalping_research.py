from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from fastapi import Depends

_INSTALLED = False


def install(*, app: Any, history: Any, dashboard: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    cache: dict[str, Any] = {}
    lock = threading.Lock()

    def parse_dt(value: Any) -> datetime | None:
        if not value:
            return None
        s = str(value).strip()
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    def clock_seconds(value: Any) -> int | None:
        if value is None:
            return None
        s = str(value).strip()
        try:
            if ":" in s:
                mm, ss = s.split(":", 1)
                return int(mm) * 60 + int(float(ss))
            return int(float(s))
        except Exception:
            return None

    def elapsed_seconds(period: Any, clock: Any) -> int | None:
        try:
            p = int(period)
        except Exception:
            return None
        if p < 1 or p > 4:
            return None
        c = clock_seconds(clock)
        if c is None:
            return None
        return (p - 1) * 600 + (600 - c)

    def score_pair(value: Any) -> tuple[int, int] | None:
        if not value:
            return None
        try:
            a, b = str(value).split("-", 1)
            return int(a), int(b)
        except Exception:
            return None

    def pick_margin(row: dict[str, Any], venue: str) -> int | None:
        away = row.get("away_score")
        home = row.get("home_score")
        if away is None or home is None:
            return None
        try:
            away_i = int(away)
            home_i = int(home)
        except Exception:
            return None
        return away_i - home_i if venue == "away" else home_i - away_i

    def avg(values: list[float]) -> float | None:
        return round(mean(values), 3) if values else None

    def pct(n: int, d: int) -> float | None:
        return round(n / d * 100.0, 1) if d else None

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {"calls": len(rows)}
        if not rows:
            return out
        out["final_win_pct"] = pct(sum(1 for r in rows if r.get("final_win")), len(rows))
        for seconds in (30, 60, 120, 180):
            key = str(seconds)
            deltas = [float(r[f"delta_{seconds}"]) for r in rows if r.get(f"delta_{seconds}") is not None]
            mfes = [float(r[f"mfe_{seconds}"]) for r in rows if r.get(f"mfe_{seconds}") is not None]
            maes = [float(r[f"mae_{seconds}"]) for r in rows if r.get(f"mae_{seconds}") is not None]
            out[f"h{key}"] = {
                "n": len(deltas),
                "avg_score_delta": avg(deltas),
                "positive_move_pct": pct(sum(1 for x in deltas if x > 0), len(deltas)),
                "negative_move_pct": pct(sum(1 for x in deltas if x < 0), len(deltas)),
                "avg_mfe": avg(mfes),
                "avg_mae": avg(maes),
            }
        for points in (2, 4, 6):
            vals = [r.get(f"first_{points}") for r in rows if r.get(f"first_{points}") is not None]
            out[f"threshold_{points}"] = {
                "n": len(vals),
                "favorable_first_pct": pct(sum(1 for x in vals if x == 1), len(vals)),
                "adverse_first_pct": pct(sum(1 for x in vals if x == -1), len(vals)),
                "neither_pct": pct(sum(1 for x in vals if x == 0), len(vals)),
            }
        dip = [r for r in rows if r.get("dip2_seen")]
        out["dip2"] = {
            "seen": len(dip),
            "seen_pct": pct(len(dip), len(rows)),
            "recover_to_start_pct": pct(sum(1 for r in dip if r.get("dip2_recover0")), len(dip)),
            "recover_to_plus2_pct": pct(sum(1 for r in dip if r.get("dip2_recover2")), len(dip)),
        }
        return out

    def analyze() -> dict[str, Any]:
        with history._db() as con:
            games = {
                str(r["game_id"]): dict(r)
                for r in con.execute("SELECT game_id,team_a,team_b FROM games").fetchall()
            }
            alerts = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT id,event_ts,game_id,predicted_winner,predicted_winner_abbr,
                           quarter,bk_ml,win_probability,score_at_alert,result
                    FROM alerts
                    WHERE game_id IS NOT NULL AND game_id<>'' AND backtest_eligible=1
                    ORDER BY event_ts ASC,id ASC
                    """
                ).fetchall()
            ]
            pbp_rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT game_id,play_id,sequence_no,event_ts,period,quarter,clock,
                           away_score,home_score,scoring_play,score_value
                    FROM play_by_play
                    WHERE period BETWEEN 1 AND 4
                    ORDER BY game_id,sequence_no ASC
                    """
                ).fetchall()
            ]

        plays_by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for p in pbp_rows:
            p["_elapsed"] = elapsed_seconds(p.get("period"), p.get("clock"))
            p["_dt"] = parse_dt(p.get("event_ts"))
            plays_by_game[str(p.get("game_id") or "")].append(p)

        aligned: list[dict[str, Any]] = []
        alignment_modes: dict[str, int] = defaultdict(int)

        for alert in alerts:
            game_id = str(alert.get("game_id") or "")
            game = games.get(game_id)
            plays = plays_by_game.get(game_id) or []
            if not game or not plays:
                continue
            abbr = str(alert.get("predicted_winner_abbr") or "")
            venue = "away" if abbr == str(game.get("team_a") or "") else ("home" if abbr == str(game.get("team_b") or "") else "")
            if not venue:
                continue
            try:
                period = int(str(alert.get("quarter") or "").upper().replace("Q", ""))
            except Exception:
                continue
            if period < 1 or period > 4:
                continue

            alert_dt = parse_dt(alert.get("event_ts"))
            score = score_pair(alert.get("score_at_alert"))
            candidates = [p for p in plays if int(p.get("period") or 0) == period]
            exact = []
            if score:
                exact = [
                    p for p in candidates
                    if p.get("away_score") is not None
                    and p.get("home_score") is not None
                    and (int(p["away_score"]), int(p["home_score"])) == score
                ]

            mode = "score_time"
            pool = exact
            if not pool:
                pool = candidates
                mode = "time_only"
            if not pool:
                continue

            def distance(p: dict[str, Any]) -> float:
                pdt = p.get("_dt")
                if alert_dt is not None and pdt is not None:
                    try:
                        return abs((pdt - alert_dt).total_seconds())
                    except Exception:
                        pass
                return float(p.get("sequence_no") or 0) * -1.0

            anchor = min(pool, key=distance) if alert_dt is not None else pool[-1]
            anchor_elapsed = anchor.get("_elapsed")
            start_margin = pick_margin(anchor, venue)
            if anchor_elapsed is None or start_margin is None:
                continue
            anchor_seq = int(anchor.get("sequence_no") or 0)

            future = [
                p for p in plays
                if p.get("_elapsed") is not None
                and int(p.get("sequence_no") or 0) >= anchor_seq
                and int(p["_elapsed"]) >= int(anchor_elapsed)
            ]
            if not future:
                continue

            rec: dict[str, Any] = {
                "alert_id": alert.get("id"),
                "event_ts": alert.get("event_ts"),
                "game_id": game_id,
                "pick": alert.get("predicted_winner"),
                "pick_abbr": abbr,
                "venue": venue,
                "quarter": str(alert.get("quarter") or "").upper(),
                "clock": anchor.get("clock"),
                "clock_seconds": clock_seconds(anchor.get("clock")),
                "bk_ml": alert.get("bk_ml"),
                "win_probability": alert.get("win_probability"),
                "final_win": str(alert.get("result") or "") == "W",
                "start_margin": start_margin,
                "alignment": mode,
            }
            alignment_modes[mode] += 1

            for seconds in (30, 60, 120, 180):
                window = [p for p in future if int(p["_elapsed"]) <= int(anchor_elapsed) + seconds]
                margins = [pick_margin(p, venue) for p in window]
                margins = [m for m in margins if m is not None]
                if not margins:
                    rec[f"delta_{seconds}"] = None
                    rec[f"mfe_{seconds}"] = None
                    rec[f"mae_{seconds}"] = None
                    continue
                deltas = [m - start_margin for m in margins]
                rec[f"delta_{seconds}"] = deltas[-1]
                rec[f"mfe_{seconds}"] = max(deltas)
                rec[f"mae_{seconds}"] = min(deltas)

            window180 = [p for p in future if int(p["_elapsed"]) <= int(anchor_elapsed) + 180]
            deltas180: list[int] = []
            for p in window180:
                m = pick_margin(p, venue)
                if m is not None:
                    deltas180.append(m - start_margin)

            for points in (2, 4, 6):
                hit = 0
                for d in deltas180:
                    if d >= points:
                        hit = 1
                        break
                    if d <= -points:
                        hit = -1
                        break
                rec[f"first_{points}"] = hit

            dip_index = next((i for i, d in enumerate(deltas180) if d <= -2), None)
            rec["dip2_seen"] = dip_index is not None
            if dip_index is not None:
                after = deltas180[dip_index:]
                rec["dip2_recover0"] = any(d >= 0 for d in after)
                rec["dip2_recover2"] = any(d >= 2 for d in after)
            else:
                rec["dip2_recover0"] = False
                rec["dip2_recover2"] = False
            aligned.append(rec)

        seen_side: dict[tuple[str, str], int] = defaultdict(int)
        for rec in sorted(aligned, key=lambda r: (str(r.get("event_ts") or ""), str(r.get("alert_id") or ""))):
            key = (str(rec.get("game_id") or ""), str(rec.get("pick_abbr") or ""))
            seen_side[key] += 1
            rec["same_side_call_no"] = seen_side[key]

        late_q3 = [
            r for r in aligned
            if r.get("quarter") == "Q3"
            and r.get("clock_seconds") is not None
            and int(r["clock_seconds"]) <= 180
        ]
        early_q4 = [
            r for r in aligned
            if r.get("quarter") == "Q4"
            and r.get("clock_seconds") is not None
            and int(r["clock_seconds"]) >= 420
        ]
        first_calls = [r for r in aligned if int(r.get("same_side_call_no") or 0) == 1]
        repeat_calls = [r for r in aligned if int(r.get("same_side_call_no") or 0) >= 2]

        filters = {
            "away_underdog": lambda r: r.get("venue") == "away" and float(r.get("bk_ml") or 0) > 0,
            "plus_money": lambda r: float(r.get("bk_ml") or 0) > 0,
            "q3_away": lambda r: r.get("quarter") == "Q3" and r.get("venue") == "away",
            "home_underdog": lambda r: r.get("venue") == "home" and float(r.get("bk_ml") or 0) > 0,
        }

        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source": {
                "alerts_eligible": len(alerts),
                "play_by_play_rows": len(pbp_rows),
                "games_with_play_by_play": len(plays_by_game),
                "aligned_calls": len(aligned),
                "alignment_rate_pct": pct(len(aligned), len(alerts)),
                "alignment_modes": dict(alignment_modes),
            },
            "all_calls": aggregate(aligned),
            "late_q3_last_3m": aggregate(late_q3),
            "early_q4_first_3m": aggregate(early_q4),
            "first_same_side_call": aggregate(first_calls),
            "repeat_same_side_calls": aggregate(repeat_calls),
            "filters": {
                name: aggregate([r for r in aligned if pred(r)])
                for name, pred in filters.items()
            },
        }
        return result

    def run_analysis() -> dict[str, Any]:
        with lock:
            result = analyze()
            cache.clear()
            cache.update(result)

        source = result.get("source") or {}
        print(
            "PW_SCALP_SUMMARY "
            f"pbp_rows={source.get('play_by_play_rows')} "
            f"games={source.get('games_with_play_by_play')} "
            f"eligible_calls={source.get('alerts_eligible')} "
            f"aligned={source.get('aligned_calls')} "
            f"align_pct={source.get('alignment_rate_pct')}",
            flush=True,
        )
        for label in ("all_calls", "late_q3_last_3m", "early_q4_first_3m", "first_same_side_call", "repeat_same_side_calls"):
            s = result.get(label) or {}
            h120 = s.get("h120") or {}
            t4 = s.get("threshold_4") or {}
            dip = s.get("dip2") or {}
            print(
                "PW_SCALP_WINDOW "
                f"name={label} calls={s.get('calls')} "
                f"delta120={h120.get('avg_score_delta')} "
                f"mfe120={h120.get('avg_mfe')} mae120={h120.get('avg_mae')} "
                f"plus4_first_pct={t4.get('favorable_first_pct')} "
                f"minus4_first_pct={t4.get('adverse_first_pct')} "
                f"dip2_pct={dip.get('seen_pct')} "
                f"dip2_recover_plus2_pct={dip.get('recover_to_plus2_pct')} "
                f"final_win_pct={s.get('final_win_pct')}",
                flush=True,
            )
        for name, s in (result.get("filters") or {}).items():
            h120 = s.get("h120") or {}
            t4 = s.get("threshold_4") or {}
            print(
                "PW_SCALP_FILTER "
                f"name={name} calls={s.get('calls')} "
                f"delta120={h120.get('avg_score_delta')} "
                f"plus4_first_pct={t4.get('favorable_first_pct')} "
                f"minus4_first_pct={t4.get('adverse_first_pct')} "
                f"final_win_pct={s.get('final_win_pct')}",
                flush=True,
            )
        return result

    @app.get("/api/pw-scalping/summary", dependencies=[Depends(dashboard._auth)])
    def scalp_summary():
        if not cache:
            return run_analysis()
        return cache

    @app.post("/api/pw-scalping/recompute", dependencies=[Depends(dashboard._auth)])
    def scalp_recompute():
        return run_analysis()

    def boot() -> None:
        time.sleep(12.0)
        try:
            run_analysis()
        except Exception as exc:
            print(f"PW_SCALP_ERROR {type(exc).__name__}:{exc}", flush=True)

    threading.Thread(target=boot, name="pw-scalping-research", daemon=True).start()
    print("PW_SCALP_RESEARCH_READY enabled=true research_only=true", flush=True)
