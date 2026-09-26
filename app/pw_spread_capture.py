from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends

from app import cfb_capper_preview as cfb

_INSTALLED = False


def _num(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _level_stats(levels: Any, side: str) -> tuple[float | None, float | None, float]:
    rows: list[tuple[float, float | None]] = []
    total = 0.0
    for level in levels or []:
        price = _num(getattr(level, "price", None) if not isinstance(level, dict) else level.get("price"))
        size = _num(getattr(level, "size", None) if not isinstance(level, dict) else level.get("size"))
        if price is None:
            continue
        rows.append((price, size))
        if size is not None and size > 0:
            total += size
    if not rows:
        return None, None, total
    best = max(rows, key=lambda item: item[0]) if side == "bid" else min(rows, key=lambda item: item[0])
    return best[0], best[1], total


def _spread_candidates(event: Any, pick_name: str, aliases: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Return every open full-game spread token for the selected WNBA team.

    This reuses the fail-closed sports spread decoder already used by CFB:
    Polymarket's question subject identifies the base side and the complementary
    outcome receives the inverse signed line.
    """
    hints = [str(pick_name or "").strip(), *(str(x).strip() for x in aliases)]
    hints = [hint for hint in dict.fromkeys(hints) if hint]
    if not hints:
        return []

    out: dict[str, dict[str, Any]] = {}
    for market in getattr(event, "markets", ()) or ():
        sports_type = str(getattr(getattr(market, "sports", None), "sports_market_type", "") or "").lower()
        if "spread" not in sports_type:
            continue
        state = getattr(market, "state", None)
        accepting = getattr(state, "accepting_orders", None)
        if accepting is False:
            continue

        resolved = None
        for hint in hints:
            candidate = cfb._spread_outcome_any_line(market, {"team_hint": hint})
            if candidate is not None:
                resolved = candidate
                break
        if resolved is None:
            continue

        label, outcome, line = resolved
        asset_id = str(
            getattr(outcome, "token_id", None)
            or getattr(outcome, "position_id", None)
            or ""
        )
        if not asset_id:
            continue

        out[asset_id] = {
            "asset_id": asset_id,
            "outcome_label": str(label),
            "poly_line": float(line),
            "market_id": str(getattr(market, "id", "") or ""),
            "condition_id": str(
                getattr(market, "condition_id", "")
                or getattr(market, "conditionId", "")
                or ""
            ),
            "question": str(getattr(market, "question", "") or ""),
        }
    return sorted(out.values(), key=lambda row: (row["poly_line"], row["asset_id"]))


def install(*, app: Any, history: Any, ingest: Any, dashboard: Any, strategy: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_SPREAD_CAPTURE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    sample_seconds = max(1.0, float(os.getenv("PW_SPREAD_CAPTURE_SAMPLE_SECONDS", "1")))
    window_seconds = max(60, int(os.getenv("PW_SPREAD_CAPTURE_WINDOW_SECONDS", "300")))
    stats: dict[str, Any] = {
        "alerts_seen": 0,
        "watches_created": 0,
        "market_match_errors": 0,
        "sample_errors": 0,
    }

    def init_schema() -> None:
        with history._db() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS pw_spread_watches (
                    watch_key TEXT PRIMARY KEY,
                    alert_event_id TEXT NOT NULL,
                    game_id TEXT,
                    pick TEXT,
                    pick_abbr TEXT,
                    quarter TEXT,
                    score_at_alert TEXT,
                    event_ts TEXT,
                    bk_ml REAL,
                    bk_spread REAL,
                    same_side_call_no INTEGER NOT NULL DEFAULT 1,
                    event_slug TEXT,
                    market_id TEXT,
                    condition_id TEXT,
                    question TEXT,
                    outcome_label TEXT,
                    poly_line REAL NOT NULL,
                    asset_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    last_error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_pw_spread_watches_active
                    ON pw_spread_watches(status, expires_at);
                CREATE INDEX IF NOT EXISTS idx_pw_spread_watches_game_side
                    ON pw_spread_watches(game_id, pick_abbr, same_side_call_no);

                CREATE TABLE IF NOT EXISTS pw_spread_ticks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    watch_key TEXT NOT NULL,
                    game_id TEXT,
                    sampled_at TEXT NOT NULL,
                    sampled_epoch REAL NOT NULL,
                    asset_id TEXT NOT NULL,
                    buy_price REAL,
                    sell_price REAL,
                    midpoint REAL,
                    clob_spread REAL,
                    best_bid REAL,
                    best_bid_size REAL,
                    best_ask REAL,
                    best_ask_size REAL,
                    bid_depth REAL,
                    ask_depth REAL,
                    quarter TEXT,
                    clock TEXT,
                    away_score INTEGER,
                    home_score INTEGER,
                    source TEXT NOT NULL DEFAULT 'natural_live_clob',
                    UNIQUE(watch_key, sampled_at)
                );
                CREATE INDEX IF NOT EXISTS idx_pw_spread_ticks_watch_time
                    ON pw_spread_ticks(watch_key, sampled_epoch);
                CREATE INDEX IF NOT EXISTS idx_pw_spread_ticks_asset_time
                    ON pw_spread_ticks(asset_id, sampled_epoch);
                """
            )

    def latest_game_state(game_id: str) -> dict[str, Any]:
        if not game_id:
            return {}
        with history._db() as con:
            row = con.execute(
                """
                SELECT quarter,clock,away_score,home_score
                FROM play_by_play
                WHERE game_id=?
                ORDER BY sequence_no DESC LIMIT 1
                """,
                (game_id,),
            ).fetchone()
        return dict(row) if row else {}

    def capture_alert(event_id: str, text: str) -> None:
        if not enabled or not text:
            return
        stats["alerts_seen"] = int(stats.get("alerts_seen") or 0) + 1
        try:
            row = strategy._pw_row(text, event_id)
            if not row:
                return

            parsed = ingest._parse_alert(text)
            event, _, _, _ = ingest._find_market(parsed)
            pick = str(row.get("predicted_winner") or parsed.get("selection") or "")
            pick_abbr = history.TEAM_ABBR.get(pick)
            aliases = tuple(ingest.WNBA_ALIASES.get(pick, ()))
            candidates = _spread_candidates(event, pick, aliases)
            if not candidates:
                stats["market_match_errors"] = int(stats.get("market_match_errors") or 0) + 1
                print(
                    f"PW_SPREAD_WATCH_NONE event={event_id} game={row.get('game_id')} pick={pick_abbr or pick}",
                    flush=True,
                )
                return

            game_id = str(row.get("game_id") or "")
            with history._db() as con:
                same_side = con.execute(
                    """
                    SELECT COUNT(*) c FROM alerts
                    WHERE game_id=? AND predicted_winner_abbr=?
                    """,
                    (game_id, pick_abbr),
                ).fetchone()["c"]
                same_side = max(1, int(same_side or 0))
                created_at = datetime.now(timezone.utc).isoformat()
                expires_at = time.time() + window_seconds
                event_slug = str(getattr(event, "slug", "") or "")

                for candidate in candidates:
                    watch_key = f"{event_id}:{candidate['asset_id']}"
                    before = con.total_changes
                    con.execute(
                        """
                        INSERT OR IGNORE INTO pw_spread_watches(
                            watch_key,alert_event_id,game_id,pick,pick_abbr,quarter,score_at_alert,event_ts,
                            bk_ml,bk_spread,same_side_call_no,event_slug,market_id,condition_id,question,
                            outcome_label,poly_line,asset_id,created_at,expires_at,status
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'ACTIVE')
                        """,
                        (
                            watch_key, str(event_id), game_id, pick, pick_abbr, row.get("quarter"),
                            row.get("score_at_alert"), row.get("event_ts"), row.get("bk_ml"),
                            row.get("bk_spread"), same_side, event_slug, candidate["market_id"],
                            candidate["condition_id"], candidate["question"], candidate["outcome_label"],
                            candidate["poly_line"], candidate["asset_id"], created_at, expires_at,
                        ),
                    )
                    if con.total_changes > before:
                        stats["watches_created"] = int(stats.get("watches_created") or 0) + 1

            print(
                "PW_SPREAD_WATCH "
                f"event={event_id} game={game_id} pick={pick_abbr or pick} "
                f"markets={len(candidates)} call_no={same_side} window_s={window_seconds}",
                flush=True,
            )
        except Exception as exc:
            stats["market_match_errors"] = int(stats.get("market_match_errors") or 0) + 1
            print(
                f"PW_SPREAD_WATCH_ERROR event={event_id} error={type(exc).__name__}:{exc}",
                flush=True,
            )

    def sample_loop() -> None:
        while True:
            started = time.time()
            try:
                init_schema()
                with history._db() as con:
                    con.execute(
                        "UPDATE pw_spread_watches SET status='EXPIRED' WHERE status='ACTIVE' AND expires_at<=?",
                        (time.time(),),
                    )
                    watches = [
                        dict(row)
                        for row in con.execute(
                            "SELECT * FROM pw_spread_watches WHERE status='ACTIVE' AND expires_at>?",
                            (time.time(),),
                        ).fetchall()
                    ]

                if watches:
                    with ingest.PublicClient() as client:
                        for watch in watches:
                            asset_id = str(watch["asset_id"])
                            try:
                                book = client.get_order_book(asset_id=asset_id)
                                buy_price = _num(client.get_price(asset_id=asset_id, side="BUY"))
                                sell_price = _num(client.get_price(asset_id=asset_id, side="SELL"))
                                midpoint = _num(client.get_midpoint(asset_id=asset_id))
                                clob_spread = _num(client.get_spread(asset_id=asset_id))
                                best_bid, best_bid_size, bid_depth = _level_stats(
                                    getattr(book, "bids", None), "bid"
                                )
                                best_ask, best_ask_size, ask_depth = _level_stats(
                                    getattr(book, "asks", None), "ask"
                                )
                                game_state = latest_game_state(str(watch.get("game_id") or ""))
                                now = datetime.now(timezone.utc)
                                sampled_at = now.isoformat(timespec="milliseconds")
                                with history._db() as con:
                                    con.execute(
                                        """
                                        INSERT OR IGNORE INTO pw_spread_ticks(
                                            watch_key,game_id,sampled_at,sampled_epoch,asset_id,
                                            buy_price,sell_price,midpoint,clob_spread,
                                            best_bid,best_bid_size,best_ask,best_ask_size,
                                            bid_depth,ask_depth,quarter,clock,away_score,home_score
                                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                        """,
                                        (
                                            watch["watch_key"], watch.get("game_id"), sampled_at, now.timestamp(),
                                            asset_id, buy_price, sell_price, midpoint, clob_spread,
                                            best_bid, best_bid_size, best_ask, best_ask_size,
                                            bid_depth, ask_depth,
                                            game_state.get("quarter") or watch.get("quarter"),
                                            game_state.get("clock"), game_state.get("away_score"),
                                            game_state.get("home_score"),
                                        ),
                                    )
                            except Exception as exc:
                                stats["sample_errors"] = int(stats.get("sample_errors") or 0) + 1
                                with history._db() as con:
                                    con.execute(
                                        "UPDATE pw_spread_watches SET last_error=? WHERE watch_key=?",
                                        (f"{type(exc).__name__}:{exc}", watch["watch_key"]),
                                    )
            except Exception as exc:
                stats["sample_errors"] = int(stats.get("sample_errors") or 0) + 1
                print(f"PW_SPREAD_SAMPLE_ERROR {type(exc).__name__}:{exc}", flush=True)

            time.sleep(max(0.05, sample_seconds - (time.time() - started)))

    def forward_rows() -> list[dict[str, Any]]:
        init_schema()
        with history._db() as con:
            rows = [
                dict(row)
                for row in con.execute(
                    """
                    SELECT
                        w.*,
                        g.team_a,g.team_b,g.score_a,g.score_b,
                        t.sampled_at AS entry_sampled_at,
                        t.best_ask AS entry_best_ask,
                        t.best_bid AS entry_best_bid,
                        t.best_ask_size AS entry_best_ask_size,
                        t.best_bid_size AS entry_best_bid_size,
                        t.ask_depth AS entry_ask_depth,
                        t.bid_depth AS entry_bid_depth
                    FROM pw_spread_watches w
                    LEFT JOIN games g ON g.game_id=w.game_id
                    LEFT JOIN pw_spread_ticks t ON t.id=(
                        SELECT t2.id FROM pw_spread_ticks t2
                        WHERE t2.watch_key=w.watch_key
                          AND t2.best_ask IS NOT NULL
                          AND t2.best_ask>0 AND t2.best_ask<1
                        ORDER BY t2.sampled_epoch ASC
                        LIMIT 1
                    )
                    ORDER BY w.created_at,w.game_id,w.pick_abbr,w.poly_line
                    """
                ).fetchall()
            ]

        for row in rows:
            bk_spread = _num(row.get("bk_spread"))
            poly_line = _num(row.get("poly_line"))
            row["line_gap_vs_bk"] = (
                round(poly_line - bk_spread, 3)
                if poly_line is not None and bk_spread is not None
                else None
            )

            score_a = _num(row.get("score_a"))
            score_b = _num(row.get("score_b"))
            pick_abbr = str(row.get("pick_abbr") or "")
            result = None
            if score_a is not None and score_b is not None and poly_line is not None:
                if pick_abbr == str(row.get("team_a") or ""):
                    adjusted = score_a + poly_line - score_b
                elif pick_abbr == str(row.get("team_b") or ""):
                    adjusted = score_b + poly_line - score_a
                else:
                    adjusted = None
                if adjusted is not None:
                    result = "W" if adjusted > 0 else "L" if adjusted < 0 else "P"
            row["spread_result"] = result

            ask = _num(row.get("entry_best_ask"))
            pnl = None
            if ask is not None and 0 < ask < 1 and result:
                if result == "W":
                    pnl = (1.0 / ask) - 1.0
                elif result == "L":
                    pnl = -1.0
                else:
                    pnl = 0.0
            row["paper_pnl_per_1_risked"] = round(pnl, 4) if pnl is not None else None
        return rows

    def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
        entered = [row for row in rows if _num(row.get("entry_best_ask")) is not None]
        settled = [row for row in entered if row.get("spread_result") in {"W", "L", "P"}]
        wins = sum(1 for row in settled if row.get("spread_result") == "W")
        losses = sum(1 for row in settled if row.get("spread_result") == "L")
        pushes = sum(1 for row in settled if row.get("spread_result") == "P")
        pnl = sum(float(row.get("paper_pnl_per_1_risked") or 0) for row in settled)
        risked = wins + losses
        return {
            "market_observations": len(rows),
            "entered": len(entered),
            "settled": len(settled),
            "wins": wins,
            "losses": losses,
            "pushes": pushes,
            "paper_pnl_per_1_risked": round(pnl, 4),
            "paper_roi_pct": round(100.0 * pnl / risked, 2) if risked else None,
        }

    def one_position_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[(str(row.get("game_id") or ""), str(row.get("pick_abbr") or ""))].append(row)

        chosen: list[dict[str, Any]] = []
        for group in grouped.values():
            first_call = [row for row in group if int(row.get("same_side_call_no") or 1) == 1]
            pool = first_call or group
            with_entry = [row for row in pool if _num(row.get("entry_best_ask")) is not None]
            if not with_entry:
                continue

            def rank(row: dict[str, Any]) -> tuple[float, float, str]:
                gap = row.get("line_gap_vs_bk")
                gap_rank = abs(float(gap)) if gap is not None else 999.0
                ask = _num(row.get("entry_best_ask")) or 1.0
                return gap_rank, ask, str(row.get("watch_key") or "")

            chosen.append(min(with_entry, key=rank))
        return chosen

    _original_save_alert = ingest._save_alert

    def save_alert_with_spread_capture(event_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        saved = _original_save_alert(event_id, payload)
        text = str((payload or {}).get("text") or "")
        if enabled and text:
            threading.Thread(
                target=capture_alert,
                args=(str(event_id), text),
                name=f"pw-spread-watch-{str(event_id)[:12]}",
                daemon=True,
            ).start()
        return saved

    ingest._save_alert = save_alert_with_spread_capture

    @app.get("/api/pw-spread-capture/status", dependencies=[Depends(dashboard._auth)])
    def spread_capture_status():
        init_schema()
        with history._db() as con:
            active = con.execute(
                "SELECT COUNT(*) c FROM pw_spread_watches WHERE status='ACTIVE' AND expires_at>?",
                (time.time(),),
            ).fetchone()["c"]
            watches = con.execute("SELECT COUNT(*) c FROM pw_spread_watches").fetchone()["c"]
            ticks = con.execute("SELECT COUNT(*) c FROM pw_spread_ticks").fetchone()["c"]
            games = con.execute(
                "SELECT COUNT(DISTINCT game_id) c FROM pw_spread_watches WHERE game_id IS NOT NULL AND game_id<>''"
            ).fetchone()["c"]
            first_calls = con.execute(
                "SELECT COUNT(*) c FROM pw_spread_watches WHERE same_side_call_no=1"
            ).fetchone()["c"]
            repeats = con.execute(
                "SELECT COUNT(*) c FROM pw_spread_watches WHERE same_side_call_no>1"
            ).fetchone()["c"]
        return {
            "enabled": enabled,
            "research_only": True,
            "natural_live_only": True,
            "sample_seconds": sample_seconds,
            "window_seconds": window_seconds,
            "active_watches": int(active or 0),
            "watches": int(watches or 0),
            "ticks": int(ticks or 0),
            "games": int(games or 0),
            "first_call_market_watches": int(first_calls or 0),
            "repeat_call_market_watches": int(repeats or 0),
            "stats": dict(stats),
        }

    @app.get("/api/pw-spread-capture/forward", dependencies=[Depends(dashboard._auth)])
    def spread_capture_forward():
        rows = forward_rows()
        first = [row for row in rows if int(row.get("same_side_call_no") or 1) == 1]
        repeated = [row for row in rows if int(row.get("same_side_call_no") or 1) > 1]
        one_position = one_position_rows(rows)
        return {
            "research_only": True,
            "entry_definition": "first observed executable best ask after the genuine PW call",
            "one_position_selection": "first PW call per game/team; closest captured Polymarket line to stored BK spread, then lower ask",
            "all_market_observations": aggregate(rows),
            "first_call_market_observations": aggregate(first),
            "repeat_call_market_observations": aggregate(repeated),
            "one_position_per_game_team": aggregate(one_position),
            "one_position_rows": one_position[-100:],
            "recent_market_rows": rows[-200:],
        }

    init_schema()
    if enabled:
        threading.Thread(
            target=sample_loop,
            name="pw-spread-capture-sampler",
            daemon=True,
        ).start()

    print(
        "PW_SPREAD_CAPTURE_READY "
        f"enabled={enabled} sample_seconds={sample_seconds} window_seconds={window_seconds} "
        "research_only=true natural_live_only=true",
        flush=True,
    )
