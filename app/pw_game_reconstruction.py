from __future__ import annotations

import bisect
import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, Query

_INSTALLED = False


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


def quarter_period(value: Any) -> int | None:
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    if text.startswith("Q"):
        text = text[1:]
    try:
        period = int(text)
        return period if 1 <= period <= 4 else None
    except Exception:
        return None


def score_pair(value: Any) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        away, home = str(value).split("-", 1)
        return int(away), int(home)
    except Exception:
        return None


def derive_game_assets(
    team_a: str,
    team_b: str,
    mapping_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Derive away/home token IDs from any verified side mapping for a game."""
    away_asset = None
    home_asset = None
    away_label = None
    home_label = None
    event_slug = None
    market_id = None
    condition_id = None

    for row in mapping_rows:
        if str(row.get("status") or "") != "OK":
            continue
        pick = str(row.get("pick_abbr") or "")
        asset = str(row.get("asset_id") or "")
        opposite = str(row.get("opposite_asset_id") or "")
        if not asset or not opposite:
            continue
        event_slug = event_slug or row.get("event_slug")
        market_id = market_id or row.get("market_id")
        condition_id = condition_id or row.get("condition_id")
        if pick == str(team_a):
            away_asset = away_asset or asset
            home_asset = home_asset or opposite
            away_label = away_label or row.get("outcome_label")
            home_label = home_label or row.get("opposite_outcome_label")
        elif pick == str(team_b):
            home_asset = home_asset or asset
            away_asset = away_asset or opposite
            home_label = home_label or row.get("outcome_label")
            away_label = away_label or row.get("opposite_outcome_label")

    return {
        "away_asset_id": away_asset,
        "home_asset_id": home_asset,
        "away_outcome_label": away_label,
        "home_outcome_label": home_label,
        "event_slug": event_slug,
        "market_id": market_id,
        "condition_id": condition_id,
        "mapped": bool(away_asset and home_asset),
    }


def nearest_price(
    points: list[tuple[int, float]],
    event_ts: int,
    max_lag_seconds: int = 90,
) -> dict[str, Any] | None:
    """Use the latest known price at/before the event; only fall forward if needed."""
    if not points:
        return None
    timestamps = [x[0] for x in points]
    idx = bisect.bisect_right(timestamps, event_ts) - 1
    if idx >= 0:
        ts, price = points[idx]
        lag = event_ts - ts
        if lag <= max_lag_seconds:
            return {
                "ts": ts,
                "price": price,
                "lag_s": lag,
                "alignment": "at_or_before",
            }
    after = bisect.bisect_left(timestamps, event_ts)
    if after < len(points):
        ts, price = points[after]
        lag = ts - event_ts
        if lag <= max_lag_seconds:
            return {
                "ts": ts,
                "price": price,
                "lag_s": lag,
                "alignment": "after",
            }
    return None


def install(*, app: Any, history: Any, dashboard: Any, ingest: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_GAME_RECONSTRUCTION_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on"
    }
    max_price_lag = max(30, int(os.getenv("PW_GAME_RECONSTRUCTION_MAX_PRICE_LAG_SECONDS", "90")))
    request_sleep = max(0.02, float(os.getenv("PW_GAME_RECONSTRUCTION_REQUEST_SLEEP", "0.08")))
    boot_delay = max(20, int(os.getenv("PW_GAME_RECONSTRUCTION_BOOT_DELAY_SECONDS", "60")))
    lock = threading.Lock()
    run_state: dict[str, Any] = {
        "running": False,
        "last_started_at": None,
        "last_completed_at": None,
        "last_error": None,
    }

    abbr_to_name = {str(abbr): str(name) for name, abbr in history.TEAM_ABBR.items()}

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

    def norm(value: Any) -> str:
        return " ".join(str(value or "").lower().replace("-", " ").split())

    def gamma_event(slug: str) -> dict[str, Any] | None:
        response = httpx.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug},
            timeout=15.0,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data[0] if data else None
        if isinstance(data, dict):
            if isinstance(data.get("data"), list):
                return data["data"][0] if data["data"] else None
            return data if data.get("markets") else None
        return None

    def slug_candidates(team_a: str, team_b: str, game_dt: datetime) -> list[str]:
        et = game_dt.astimezone(ZoneInfo("America/New_York"))
        dates = [(et + timedelta(days=delta)).date().isoformat() for delta in (0, -1, 1)]
        out: list[str] = []
        for away_code in TEAM_SLUGS.get(team_a, [team_a.lower()]):
            for home_code in TEAM_SLUGS.get(team_b, [team_b.lower()]):
                for date in dates:
                    out.append(f"wnba-{away_code}-{home_code}-{date}")
                    out.append(f"wnba-{home_code}-{away_code}-{date}")
        return list(dict.fromkeys(out))

    def outcome_index(team_name: str, outcomes: list[str]) -> int | None:
        aliases = [norm(team_name)]
        aliases.extend(norm(x) for x in ingest.WNBA_ALIASES.get(team_name, ()))
        for index, outcome in enumerate(outcomes):
            outcome_norm = norm(outcome)
            if any(alias and alias in outcome_norm for alias in aliases):
                return index
        return None

    def moneyline_market(event: dict[str, Any], away_name: str, home_name: str) -> dict[str, Any] | None:
        candidates: list[tuple[int, dict[str, Any]]] = []
        for market in event.get("markets") or []:
            if not isinstance(market, dict):
                continue
            outcomes = [str(x) for x in json_list(market.get("outcomes"))]
            tokens = [str(x) for x in json_list(market.get("clobTokenIds"))]
            if len(outcomes) != 2 or len(tokens) != 2:
                continue
            away_idx = outcome_index(away_name, outcomes)
            home_idx = outcome_index(home_name, outcomes)
            if away_idx is None or home_idx is None or away_idx == home_idx:
                continue
            question = norm(market.get("question"))
            market_type = norm(
                market.get("sportsMarketType")
                or market.get("marketType")
                or market.get("groupItemTitle")
            )
            score = 20
            if "moneyline" in market_type or market_type in {"money line", "winner"}:
                score += 20
            if any(word in question for word in ("spread", "over ", "under ", "points", "assists", "rebounds", "margin")):
                score -= 30
            candidates.append((score, market))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def recover_game_mapping(
        game_id: str,
        team_a: str,
        team_b: str,
        first_pbp_ts: Any,
    ) -> list[dict[str, Any]]:
        game_dt = parse_dt(first_pbp_ts)
        away_name = abbr_to_name.get(team_a)
        home_name = abbr_to_name.get(team_b)
        if game_dt is None or not away_name or not home_name:
            return []

        last_error = "event not found"
        for slug in slug_candidates(team_a, team_b, game_dt):
            try:
                event = gamma_event(slug)
            except Exception as exc:
                last_error = f"{type(exc).__name__}:{exc}"
                time.sleep(request_sleep)
                continue
            if not event:
                continue

            market = moneyline_market(event, away_name, home_name)
            if not market:
                last_error = f"no moneyline market in {slug}"
                continue

            outcomes = [str(x) for x in json_list(market.get("outcomes"))]
            tokens = [str(x) for x in json_list(market.get("clobTokenIds"))]
            away_idx = outcome_index(away_name, outcomes)
            home_idx = outcome_index(home_name, outcomes)
            if away_idx is None or home_idx is None or away_idx == home_idx:
                last_error = f"team outcomes not found in {slug}"
                continue

            now = datetime.now(timezone.utc).isoformat()
            common = {
                "game_id": game_id,
                "event_slug": slug,
                "market_id": str(market.get("id") or ""),
                "condition_id": str(market.get("conditionId") or market.get("condition_id") or ""),
                "status": "OK",
                "error": None,
                "mapped_at": now,
            }
            away_row = {
                **common,
                "pick_abbr": team_a,
                "asset_id": tokens[away_idx],
                "opposite_asset_id": tokens[home_idx],
                "outcome_label": outcomes[away_idx],
                "opposite_outcome_label": outcomes[home_idx],
            }
            home_row = {
                **common,
                "pick_abbr": team_b,
                "asset_id": tokens[home_idx],
                "opposite_asset_id": tokens[away_idx],
                "outcome_label": outcomes[home_idx],
                "opposite_outcome_label": outcomes[away_idx],
            }
            with history._db() as con:
                for row in (away_row, home_row):
                    con.execute(
                        """
                        INSERT OR REPLACE INTO pw_market_history_map(
                            game_id,pick_abbr,event_slug,market_id,condition_id,asset_id,
                            opposite_asset_id,outcome_label,opposite_outcome_label,status,error,mapped_at
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            row["game_id"], row["pick_abbr"], row["event_slug"], row["market_id"],
                            row["condition_id"], row["asset_id"], row["opposite_asset_id"],
                            row["outcome_label"], row["opposite_outcome_label"], row["status"],
                            row["error"], row["mapped_at"],
                        ),
                    )
            print(
                "PW_GAME_RECON_MAP_RECOVERED "
                f"game={game_id} away={team_a} home={team_b} slug={slug}",
                flush=True,
            )
            return [away_row, home_row]

        print(
            "PW_GAME_RECON_MAP_RECOVERY_MISS "
            f"game={game_id} away={team_a} home={team_b} detail={last_error}",
            flush=True,
        )
        return []

    def init_schema() -> None:
        with history._db() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS pw_game_reconstruction_coverage (
                    game_id TEXT PRIMARY KEY,
                    team_a TEXT,
                    team_b TEXT,
                    pbp_rows INTEGER NOT NULL DEFAULT 0,
                    pw_calls INTEGER NOT NULL DEFAULT 0,
                    map_status TEXT NOT NULL,
                    event_slug TEXT,
                    market_id TEXT,
                    condition_id TEXT,
                    away_asset_id TEXT,
                    home_asset_id TEXT,
                    away_price_points INTEGER NOT NULL DEFAULT 0,
                    home_price_points INTEGER NOT NULL DEFAULT 0,
                    priced_pbp_rows INTEGER NOT NULL DEFAULT 0,
                    price_coverage_pct REAL,
                    reconstruction_status TEXT NOT NULL,
                    first_pbp_ts TEXT,
                    last_pbp_ts TEXT,
                    error TEXT,
                    generated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pw_game_reconstruction (
                    game_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    play_id TEXT,
                    event_ts TEXT,
                    event_epoch INTEGER,
                    period INTEGER,
                    quarter TEXT,
                    clock TEXT,
                    away_score INTEGER,
                    home_score INTEGER,
                    score_diff_home INTEGER,
                    event_type TEXT,
                    text TEXT,
                    scoring_play INTEGER,
                    score_value INTEGER,
                    away_price REAL,
                    home_price REAL,
                    away_price_ts INTEGER,
                    home_price_ts INTEGER,
                    away_price_lag_s INTEGER,
                    home_price_lag_s INTEGER,
                    away_price_alignment TEXT,
                    home_price_alignment TEXT,
                    pw_calls_json TEXT,
                    generated_at TEXT NOT NULL,
                    PRIMARY KEY(game_id, sequence_no)
                );
                CREATE INDEX IF NOT EXISTS idx_pw_game_recon_event
                    ON pw_game_reconstruction(game_id, event_epoch);
                CREATE INDEX IF NOT EXISTS idx_pw_game_recon_period
                    ON pw_game_reconstruction(game_id, period, sequence_no);
                """
            )

    def history_points(asset_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
        with history._db() as con:
            rows = con.execute(
                """
                SELECT ts,price
                FROM pw_market_history_points
                WHERE asset_id=? AND ts BETWEEN ? AND ?
                ORDER BY ts
                """,
                (asset_id, start_ts, end_ts),
            ).fetchall()
        return [(int(r["ts"]), float(r["price"])) for r in rows]

    def fetch_full_history(asset_id: str, start_ts: int, end_ts: int) -> list[tuple[int, float]]:
        cached = history_points(asset_id, start_ts, end_ts)
        if cached and cached[0][0] <= start_ts + max_price_lag and cached[-1][0] >= end_ts - max_price_lag:
            return cached

        response = httpx.get(
            "https://clob.polymarket.com/prices-history",
            params={
                "market": asset_id,
                "startTs": start_ts,
                "endTs": end_ts,
                "fidelity": 1,
            },
            timeout=25.0,
        )
        response.raise_for_status()
        payload = response.json()
        raw_points = payload.get("history") if isinstance(payload, dict) else payload
        points: list[tuple[int, float]] = []
        for item in raw_points or []:
            try:
                ts = int(item.get("t") or item.get("timestamp"))
                price = float(item.get("p") or item.get("price"))
                if math.isfinite(price) and 0 < price < 1:
                    points.append((ts, price))
            except Exception:
                continue
        points = sorted(set(points))
        if points:
            with history._db() as con:
                con.executemany(
                    """
                    INSERT OR REPLACE INTO pw_market_history_points(asset_id,ts,price)
                    VALUES(?,?,?)
                    """,
                    [(asset_id, ts, price) for ts, price in points],
                )
        return history_points(asset_id, start_ts, end_ts)

    def align_alerts_to_plays(
        plays: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
    ) -> dict[int, list[dict[str, Any]]]:
        by_sequence: dict[int, list[dict[str, Any]]] = defaultdict(list)
        by_period: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for play in plays:
            try:
                by_period[int(play.get("period") or 0)].append(play)
            except Exception:
                continue

        for alert in alerts:
            period = quarter_period(alert.get("quarter"))
            if period is None:
                continue
            candidates = by_period.get(period) or []
            if not candidates:
                continue
            alert_dt = parse_dt(alert.get("event_ts"))
            score = score_pair(alert.get("score_at_alert"))
            exact = []
            if score:
                exact = [
                    p for p in candidates
                    if p.get("away_score") is not None
                    and p.get("home_score") is not None
                    and (int(p["away_score"]), int(p["home_score"])) == score
                ]
            pool = exact or candidates

            def distance(play: dict[str, Any]) -> float:
                pdt = parse_dt(play.get("event_ts"))
                if alert_dt is not None and pdt is not None:
                    return abs((pdt - alert_dt).total_seconds())
                return float("inf")

            if alert_dt is not None:
                timed = [p for p in pool if parse_dt(p.get("event_ts")) is not None]
                anchor = min(timed, key=distance) if timed else pool[-1]
            else:
                anchor = pool[-1]
            seq = int(anchor.get("sequence_no") or 0)
            by_sequence[seq].append(
                {
                    "id": alert.get("id"),
                    "event_ts": alert.get("event_ts"),
                    "pick": alert.get("predicted_winner"),
                    "pick_abbr": alert.get("predicted_winner_abbr"),
                    "quarter": alert.get("quarter"),
                    "score_at_alert": alert.get("score_at_alert"),
                    "bk_ml": alert.get("bk_ml"),
                    "win_probability": alert.get("win_probability"),
                    "result": alert.get("result"),
                    "backtest_eligible": alert.get("backtest_eligible"),
                    "alignment": "score_time" if exact else "time_only",
                }
            )
        return by_sequence

    def save_coverage(row: dict[str, Any]) -> None:
        keys = (
            "game_id", "team_a", "team_b", "pbp_rows", "pw_calls", "map_status",
            "event_slug", "market_id", "condition_id", "away_asset_id", "home_asset_id",
            "away_price_points", "home_price_points", "priced_pbp_rows",
            "price_coverage_pct", "reconstruction_status", "first_pbp_ts",
            "last_pbp_ts", "error", "generated_at",
        )
        with history._db() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO pw_game_reconstruction_coverage(
                    game_id,team_a,team_b,pbp_rows,pw_calls,map_status,event_slug,
                    market_id,condition_id,away_asset_id,home_asset_id,
                    away_price_points,home_price_points,priced_pbp_rows,
                    price_coverage_pct,reconstruction_status,first_pbp_ts,
                    last_pbp_ts,error,generated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                tuple(row.get(k) for k in keys),
            )

    def reconstruct_all() -> dict[str, Any]:
        if not enabled:
            return {"enabled": False}

        with lock:
            run_state["running"] = True
            run_state["last_started_at"] = datetime.now(timezone.utc).isoformat()
            run_state["last_error"] = None
            try:
                init_schema()
                with history._db() as con:
                    games = [
                        dict(r)
                        for r in con.execute(
                            """
                            SELECT p.game_id,
                                   MAX(g.team_a) AS team_a,
                                   MAX(g.team_b) AS team_b,
                                   COUNT(p.sequence_no) AS pbp_rows,
                                   MIN(p.event_ts) AS first_pbp_ts,
                                   MAX(p.event_ts) AS last_pbp_ts
                            FROM play_by_play p
                            LEFT JOIN games g ON g.game_id=p.game_id
                            GROUP BY p.game_id
                            ORDER BY MIN(p.event_ts),p.game_id
                            """
                        ).fetchall()
                    ]
                    mappings_by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in con.execute(
                        "SELECT * FROM pw_market_history_map ORDER BY game_id,pick_abbr"
                    ).fetchall():
                        mappings_by_game[str(row["game_id"])].append(dict(row))
                    alerts_by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in con.execute(
                        """
                        SELECT id,event_ts,game_id,predicted_winner,predicted_winner_abbr,
                               quarter,score_at_alert,bk_ml,win_probability,result,
                               backtest_eligible
                        FROM alerts
                        WHERE game_id IS NOT NULL AND game_id<>''
                          AND COALESCE(is_test,0)=0
                        ORDER BY event_ts,id
                        """
                    ).fetchall():
                        alerts_by_game[str(row["game_id"])].append(dict(row))

                summary = {
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "games_with_pbp": len(games),
                    "fully_reconstructed": 0,
                    "partially_reconstructed": 0,
                    "no_game_metadata": 0,
                    "no_market_map": 0,
                    "no_price_history": 0,
                    "failed": 0,
                    "pbp_rows": 0,
                    "priced_pbp_rows": 0,
                    "pw_calls": 0,
                }

                for index, game in enumerate(games, start=1):
                    game_id = str(game["game_id"])
                    generated_at = datetime.now(timezone.utc).isoformat()
                    with history._db() as con:
                        plays = [
                            dict(r)
                            for r in con.execute(
                                """
                                SELECT game_id,play_id,sequence_no,event_ts,period,quarter,clock,
                                       away_score,home_score,score_diff_home,event_type,text,
                                       scoring_play,score_value
                                FROM play_by_play
                                WHERE game_id=?
                                ORDER BY sequence_no
                                """,
                                (game_id,),
                            ).fetchall()
                        ]
                    alerts = alerts_by_game.get(game_id) or []
                    summary["pbp_rows"] += len(plays)
                    summary["pw_calls"] += len(alerts)
                    team_a = str(game.get("team_a") or "")
                    team_b = str(game.get("team_b") or "")
                    assets = derive_game_assets(
                        team_a,
                        team_b,
                        mappings_by_game.get(game_id) or [],
                    )
                    if team_a and team_b and not assets["mapped"]:
                        recovered = recover_game_mapping(
                            game_id,
                            team_a,
                            team_b,
                            game.get("first_pbp_ts"),
                        )
                        if recovered:
                            mappings_by_game[game_id] = recovered
                            assets = derive_game_assets(team_a, team_b, recovered)
                    coverage = {
                        "game_id": game_id,
                        "team_a": game.get("team_a"),
                        "team_b": game.get("team_b"),
                        "pbp_rows": len(plays),
                        "pw_calls": len(alerts),
                        "map_status": "OK" if assets["mapped"] else "UNRESOLVED",
                        "event_slug": assets.get("event_slug"),
                        "market_id": assets.get("market_id"),
                        "condition_id": assets.get("condition_id"),
                        "away_asset_id": assets.get("away_asset_id"),
                        "home_asset_id": assets.get("home_asset_id"),
                        "away_price_points": 0,
                        "home_price_points": 0,
                        "priced_pbp_rows": 0,
                        "price_coverage_pct": 0.0,
                        "reconstruction_status": "NO_MARKET_MAP",
                        "first_pbp_ts": game.get("first_pbp_ts"),
                        "last_pbp_ts": game.get("last_pbp_ts"),
                        "error": None,
                        "generated_at": generated_at,
                    }

                    if not team_a or not team_b:
                        coverage["map_status"] = "MISSING_GAME_METADATA"
                        coverage["reconstruction_status"] = "NO_GAME_METADATA"
                        coverage["error"] = "play-by-play game_id is not present in canonical games metadata"
                        summary["no_game_metadata"] += 1
                        print(
                            "PW_GAME_RECON_UNRESOLVED "
                            f"game={game_id} away={team_a or 'UNKNOWN'} home={team_b or 'UNKNOWN'} "
                            f"first_ts={coverage.get('first_pbp_ts')} reason=no_game_metadata",
                            flush=True,
                        )
                        save_coverage(coverage)
                        with history._db() as con:
                            con.execute("DELETE FROM pw_game_reconstruction WHERE game_id=?", (game_id,))
                        continue

                    if not assets["mapped"]:
                        summary["no_market_map"] += 1
                        mapping_errors = [
                            str(x.get("error") or "")
                            for x in (mappings_by_game.get(game_id) or [])
                            if x.get("error")
                        ]
                        print(
                            "PW_GAME_RECON_UNRESOLVED "
                            f"game={game_id} away={team_a} home={team_b} "
                            f"first_ts={coverage.get('first_pbp_ts')} reason=no_market_map "
                            f"detail={mapping_errors[-1] if mapping_errors else 'no_mapping_row'}",
                            flush=True,
                        )
                        save_coverage(coverage)
                        with history._db() as con:
                            con.execute("DELETE FROM pw_game_reconstruction WHERE game_id=?", (game_id,))
                        continue

                    event_epochs = [
                        int(dt.timestamp())
                        for dt in (parse_dt(p.get("event_ts")) for p in plays)
                        if dt is not None
                    ]
                    if not event_epochs:
                        coverage["reconstruction_status"] = "FAILED"
                        coverage["error"] = "play-by-play has no parseable event timestamps"
                        summary["failed"] += 1
                        save_coverage(coverage)
                        continue

                    start_ts = min(event_epochs) - 300
                    end_ts = max(event_epochs) + 300
                    try:
                        away_points = fetch_full_history(str(assets["away_asset_id"]), start_ts, end_ts)
                        time.sleep(request_sleep)
                        home_points = fetch_full_history(str(assets["home_asset_id"]), start_ts, end_ts)
                        time.sleep(request_sleep)
                    except Exception as exc:
                        coverage["reconstruction_status"] = "FAILED"
                        coverage["error"] = f"{type(exc).__name__}:{exc}"
                        summary["failed"] += 1
                        save_coverage(coverage)
                        print(
                            f"PW_GAME_RECON_PRICE_ERROR game={game_id} "
                            f"error={type(exc).__name__}:{exc}",
                            flush=True,
                        )
                        continue

                    coverage["away_price_points"] = len(away_points)
                    coverage["home_price_points"] = len(home_points)
                    if not away_points or not home_points:
                        coverage["reconstruction_status"] = "NO_PRICE_HISTORY"
                        summary["no_price_history"] += 1
                        save_coverage(coverage)
                        with history._db() as con:
                            con.execute("DELETE FROM pw_game_reconstruction WHERE game_id=?", (game_id,))
                        continue

                    calls_by_sequence = align_alerts_to_plays(plays, alerts)
                    rows = []
                    priced = 0
                    for play in plays:
                        dt = parse_dt(play.get("event_ts"))
                        event_epoch = int(dt.timestamp()) if dt is not None else None
                        away_mark = nearest_price(away_points, event_epoch, max_price_lag) if event_epoch is not None else None
                        home_mark = nearest_price(home_points, event_epoch, max_price_lag) if event_epoch is not None else None
                        if away_mark is not None and home_mark is not None:
                            priced += 1
                        seq = int(play.get("sequence_no") or 0)
                        rows.append(
                            (
                                game_id,
                                seq,
                                play.get("play_id"),
                                play.get("event_ts"),
                                event_epoch,
                                play.get("period"),
                                play.get("quarter"),
                                play.get("clock"),
                                play.get("away_score"),
                                play.get("home_score"),
                                play.get("score_diff_home"),
                                play.get("event_type"),
                                play.get("text"),
                                1 if play.get("scoring_play") else 0,
                                play.get("score_value"),
                                away_mark.get("price") if away_mark else None,
                                home_mark.get("price") if home_mark else None,
                                away_mark.get("ts") if away_mark else None,
                                home_mark.get("ts") if home_mark else None,
                                away_mark.get("lag_s") if away_mark else None,
                                home_mark.get("lag_s") if home_mark else None,
                                away_mark.get("alignment") if away_mark else None,
                                home_mark.get("alignment") if home_mark else None,
                                json.dumps(calls_by_sequence.get(seq) or [], separators=(",", ":")),
                                generated_at,
                            )
                        )

                    with history._db() as con:
                        con.execute("DELETE FROM pw_game_reconstruction WHERE game_id=?", (game_id,))
                        con.executemany(
                            """
                            INSERT INTO pw_game_reconstruction(
                                game_id,sequence_no,play_id,event_ts,event_epoch,period,quarter,clock,
                                away_score,home_score,score_diff_home,event_type,text,scoring_play,
                                score_value,away_price,home_price,away_price_ts,home_price_ts,
                                away_price_lag_s,home_price_lag_s,away_price_alignment,
                                home_price_alignment,pw_calls_json,generated_at
                            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                            """,
                            rows,
                        )

                    coverage_pct = round(priced / len(plays) * 100.0, 2) if plays else 0.0
                    coverage["priced_pbp_rows"] = priced
                    coverage["price_coverage_pct"] = coverage_pct
                    if priced == len(plays) and plays:
                        coverage["reconstruction_status"] = "FULL"
                        summary["fully_reconstructed"] += 1
                    elif priced > 0:
                        coverage["reconstruction_status"] = "PARTIAL"
                        summary["partially_reconstructed"] += 1
                    else:
                        coverage["reconstruction_status"] = "NO_PRICE_HISTORY"
                        summary["no_price_history"] += 1
                    summary["priced_pbp_rows"] += priced
                    save_coverage(coverage)

                    if index % 20 == 0:
                        print(
                            "PW_GAME_RECON_PROGRESS "
                            f"done={index}/{len(games)} full={summary['fully_reconstructed']} "
                            f"partial={summary['partially_reconstructed']} "
                            f"no_game_meta={summary['no_game_metadata']} "
                            f"no_map={summary['no_market_map']} "
                            f"no_price={summary['no_price_history']} failed={summary['failed']}",
                            flush=True,
                        )

                run_state["last_completed_at"] = datetime.now(timezone.utc).isoformat()
                overall = round(
                    summary["priced_pbp_rows"] / summary["pbp_rows"] * 100.0, 2
                ) if summary["pbp_rows"] else 0.0
                summary["overall_price_coverage_pct"] = overall
                print(
                    "PW_GAME_RECON_SUMMARY "
                    f"games={summary['games_with_pbp']} "
                    f"full={summary['fully_reconstructed']} "
                    f"partial={summary['partially_reconstructed']} "
                    f"no_game_meta={summary['no_game_metadata']} "
                    f"no_map={summary['no_market_map']} "
                    f"no_price={summary['no_price_history']} failed={summary['failed']} "
                    f"pbp_rows={summary['pbp_rows']} priced_rows={summary['priced_pbp_rows']} "
                    f"coverage_pct={overall} pw_calls={summary['pw_calls']}",
                    flush=True,
                )
                return summary
            except Exception as exc:
                run_state["last_error"] = f"{type(exc).__name__}:{exc}"
                print(f"PW_GAME_RECON_ERROR {type(exc).__name__}:{exc}", flush=True)
                raise
            finally:
                run_state["running"] = False

    def coverage_summary() -> dict[str, Any]:
        init_schema()
        with history._db() as con:
            rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT *
                    FROM pw_game_reconstruction_coverage
                    ORDER BY first_pbp_ts,game_id
                    """
                ).fetchall()
            ]
        counts: dict[str, int] = defaultdict(int)
        total_pbp = 0
        total_priced = 0
        for row in rows:
            counts[str(row.get("reconstruction_status") or "UNKNOWN")] += 1
            total_pbp += int(row.get("pbp_rows") or 0)
            total_priced += int(row.get("priced_pbp_rows") or 0)
        return {
            "enabled": enabled,
            "run_state": dict(run_state),
            "games": len(rows),
            "status_counts": dict(counts),
            "pbp_rows": total_pbp,
            "priced_pbp_rows": total_priced,
            "overall_price_coverage_pct": round(total_priced / total_pbp * 100.0, 2)
            if total_pbp else 0.0,
            "rows": rows,
            "method": (
                "ESPN play-by-play joined to Polymarket prices-history at 1-minute fidelity; "
                "latest price at/before each play within the configured lag, otherwise the "
                "first price after the play within that lag. Historical prices are a proxy, "
                "not executable historical bid/ask."
            ),
        }

    @app.get("/api/pw-game-reconstruction/status", dependencies=[Depends(dashboard._auth)])
    def reconstruction_status():
        summary = coverage_summary()
        return {
            key: value
            for key, value in summary.items()
            if key != "rows"
        }

    @app.get("/api/pw-game-reconstruction/coverage", dependencies=[Depends(dashboard._auth)])
    def reconstruction_coverage():
        return coverage_summary()

    @app.get("/api/pw-game-reconstruction/game/{game_id}", dependencies=[Depends(dashboard._auth)])
    def reconstructed_game(
        game_id: str,
        limit: int = Query(2000, ge=1, le=10000),
        offset: int = Query(0, ge=0),
    ):
        init_schema()
        with history._db() as con:
            coverage = con.execute(
                "SELECT * FROM pw_game_reconstruction_coverage WHERE game_id=?",
                (game_id,),
            ).fetchone()
            rows = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT *
                    FROM pw_game_reconstruction
                    WHERE game_id=?
                    ORDER BY sequence_no
                    LIMIT ? OFFSET ?
                    """,
                    (game_id, limit, offset),
                ).fetchall()
            ]
            total = con.execute(
                "SELECT COUNT(*) c FROM pw_game_reconstruction WHERE game_id=?",
                (game_id,),
            ).fetchone()["c"]
        for row in rows:
            try:
                row["pw_calls"] = json.loads(row.pop("pw_calls_json") or "[]")
            except Exception:
                row["pw_calls"] = []
        return {
            "game_id": game_id,
            "coverage": dict(coverage) if coverage else None,
            "total_rows": int(total or 0),
            "offset": offset,
            "limit": limit,
            "rows": rows,
        }

    @app.post("/api/pw-game-reconstruction/recompute", dependencies=[Depends(dashboard._auth)])
    def reconstruction_recompute():
        if run_state.get("running"):
            return {"started": False, "reason": "already_running"}
        threading.Thread(
            target=reconstruct_all,
            name="pw-game-reconstruction-manual",
            daemon=True,
        ).start()
        return {"started": True}

    init_schema()
    if enabled:
        def boot() -> None:
            time.sleep(boot_delay)
            reconstruct_all()

        threading.Thread(
            target=boot,
            name="pw-game-reconstruction",
            daemon=True,
        ).start()

    print(
        "PW_GAME_RECON_READY "
        f"enabled={enabled} max_price_lag_s={max_price_lag} "
        f"boot_delay_s={boot_delay} research_only=true",
        flush=True,
    )