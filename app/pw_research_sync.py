from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import Depends

from app import pw_export_ingest as pwexp

_INSTALLED = False
_THREAD: threading.Thread | None = None


def install(*, app: Any, history: Any, core: Any, dashboard: Any) -> None:
    global _INSTALLED, _THREAD
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_RESEARCH_SYNC_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    pbp_enabled = os.getenv("PW_RESEARCH_PBP_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    export_source = os.getenv("PW_RESEARCH_EXPORT_SOURCE", "all").strip() or "all"
    since = os.getenv("PW_RESEARCH_SINCE", "2026-05-01").strip() or "2026-05-01"
    export_url = os.getenv(
        "PW_WNBA_EXPORT_URL",
        "https://bob-mbp-ubuntu.taila35415.ts.net:8445/api/pw-export",
    ).strip()
    proxy = os.getenv("PW_EXPORT_SOCKS_PROXY", "socks5://127.0.0.1:1055").strip()
    tailnet_peer = os.getenv("PW_TAILNET_PEER", "100.81.244.65").strip()
    state_file = core.DATA_DIR / "pw_research_sync_state.json"
    lock = threading.Lock()

    def load_state() -> dict[str, Any]:
        try:
            state = core._load(state_file)
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def save_state(state: dict[str, Any]) -> None:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        core._save(state_file, state)

    def init_schema() -> None:
        with history._db() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS play_by_play (
                    game_id TEXT NOT NULL,
                    play_id TEXT NOT NULL,
                    sequence_no INTEGER,
                    event_ts TEXT,
                    period INTEGER,
                    quarter TEXT,
                    clock TEXT,
                    away_score INTEGER,
                    home_score INTEGER,
                    score_diff_home INTEGER,
                    event_type TEXT,
                    team_id TEXT,
                    team_abbr TEXT,
                    text TEXT,
                    scoring_play INTEGER NOT NULL DEFAULT 0,
                    score_value INTEGER,
                    source TEXT NOT NULL DEFAULT 'espn',
                    raw_json TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(game_id, play_id)
                );
                CREATE INDEX IF NOT EXISTS idx_pbp_game_seq ON play_by_play(game_id, sequence_no);
                CREATE INDEX IF NOT EXISTS idx_pbp_game_period_clock ON play_by_play(game_id, period, clock);
                """
            )
            cols = {r["name"] for r in con.execute("PRAGMA table_info(alerts)").fetchall()}
            if "research_source" not in cols:
                con.execute("ALTER TABLE alerts ADD COLUMN research_source TEXT")
            if "research_raw_json" not in cols:
                con.execute("ALTER TABLE alerts ADD COLUMN research_raw_json TEXT")

            state = load_state()
            if int(state.get("research_import_version") or 0) < 2:
                removed = con.execute(
                    "DELETE FROM alerts WHERE research_source='pw-server'"
                ).rowcount
                state["research_import_version"] = 2
                state["research_import_cleanup_removed"] = int(removed or 0)
                state["research_import_cleanup_at"] = datetime.now(timezone.utc).isoformat()
                save_state(state)
                print(
                    f"PW_RESEARCH_IMPORT_MIGRATION version=2 removed={int(removed or 0)}",
                    flush=True,
                )

    def fetch_export_rows() -> list[dict[str, Any]]:
        params = {
            "source": export_source,
            "since": since,
            "include_suppressed": "0",
        }
        target = urlsplit(export_url)
        if target.scheme.lower() != "https" or not target.hostname:
            raise RuntimeError("PW_WNBA_EXPORT_URL must be a valid https URL")

        proxy_url = urlsplit(proxy)
        proxy_host = proxy_url.hostname or "127.0.0.1"
        proxy_port = proxy_url.port or 1055
        target_port = target.port or 443
        connect_host = tailnet_peer or target.hostname
        raw = socket.create_connection((proxy_host, proxy_port), timeout=8.0)
        raw.settimeout(45.0)
        try:
            authority = f"{connect_host}:{target_port}"
            raw.sendall(
                (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    "Proxy-Connection: keep-alive\r\n\r\n"
                ).encode("ascii")
            )
            header = bytearray()
            while b"\r\n\r\n" not in header and len(header) < 16384:
                chunk = raw.recv(4096)
                if not chunk:
                    break
                header.extend(chunk)
            status_line = bytes(header).split(b"\r\n", 1)[0].decode("ascii", "replace")
            if " 200 " not in f" {status_line} " and not status_line.endswith(" 200"):
                raise RuntimeError(f"Tailscale CONNECT failed: {status_line}")

            context = ssl.create_default_context()
            tls = context.wrap_socket(raw, server_hostname=target.hostname)
            tls.settimeout(45.0)
            query = urlencode(params)
            path = target.path or "/"
            request_path = f"{path}?{query}" if query else path
            host_header = target.hostname if target_port == 443 else f"{target.hostname}:{target_port}"
            tls.sendall(
                (
                    f"GET {request_path} HTTP/1.1\r\n"
                    f"Host: {host_header}\r\n"
                    "Accept: application/json\r\n"
                    "Connection: close\r\n"
                    "User-Agent: railway-pw-research-sync/1\r\n\r\n"
                ).encode("ascii")
            )
            response = http.client.HTTPResponse(tls)
            response.begin()
            body = response.read()
            if response.status >= 400:
                raise RuntimeError(f"PW export HTTP {response.status}: {body[:200].decode('utf-8','replace')}")
            payload = json.loads(body.decode("utf-8"))
            return pwexp._records(payload)
        finally:
            try:
                raw.close()
            except Exception:
                pass

    def natural_duplicate(con: Any, row: dict[str, Any]) -> bool:
        game_id = row.get("game_id")
        event_ts = row.get("event_ts")
        pick = row.get("predicted_winner")
        quarter = row.get("quarter")
        bk_ml = row.get("bk_ml")
        if not game_id or not event_ts or not pick:
            return False
        hit = con.execute(
            """
            SELECT 1 FROM alerts
            WHERE game_id=? AND event_ts=? AND predicted_winner=?
              AND COALESCE(quarter,'')=COALESCE(?,'')
              AND (
                    (bk_ml IS NULL AND ? IS NULL)
                    OR bk_ml=?
                  )
            LIMIT 1
            """,
            (str(game_id), str(event_ts), str(pick), quarter, bk_ml, bk_ml),
        ).fetchone()
        return bool(hit)

    def normalize_export_row(raw: dict[str, Any]) -> dict[str, Any] | None:
        text, meta = pwexp._synth_text(raw, history)
        if not text:
            return None
        parsed = history._parse_current_message(text)
        alert = next((x for x in parsed if x.get("record_type") == "alert"), None)
        if not alert:
            return None

        def first(*keys: str) -> Any:
            return pwexp._first(raw, *keys)

        alert["source_channel"] = "pw-export-historical"
        row_dt = pwexp._row_dt(raw)
        if row_dt is not None:
            kl = row_dt.astimezone(timezone(timedelta(hours=8)))
            alert["event_ts"] = kl.strftime("%Y-%m-%d %H:%M:%S +08")
        else:
            alert["event_ts"] = meta.get("event_ts") or alert.get("event_ts")
        alert["game_id"] = meta.get("game_id") or alert.get("game_id")
        alert["predicted_winner"] = meta.get("predicted_winner") or alert.get("predicted_winner")
        alert["quarter"] = meta.get("quarter") or alert.get("quarter")
        alert["win_probability"] = meta.get("win_probability") if meta.get("win_probability") is not None else alert.get("win_probability")
        alert["bk_ml"] = meta.get("bk_ml") if meta.get("bk_ml") is not None else alert.get("bk_ml")
        alert["score_at_alert"] = meta.get("score_at_alert") or alert.get("score_at_alert")
        alert["consensus"] = first("consensus")
        alert["margin"] = pwexp._num(first("margin"))
        alert["pregame_odds"] = pwexp._int_odds(first("pregame_odds", "pregameOdds"))
        alert["handicap"] = pwexp._num(first("handicap", "spread"))
        alert["live_spread"] = pwexp._num(first("live_spread", "liveSpread"))
        alert["bk_spread"] = pwexp._num(first("bk_spread", "bkSpread"))
        alert["edge"] = pwexp._num(first("edge"))
        alert["is_test"] = bool(first("is_test", "isTest"))
        alert["alert_format"] = str(first("alert_format", "alertFormat") or "pw-export")
        alert["event_id"] = f"research:{pwexp._identity(raw)}"
        return alert

    def import_missing_pw() -> dict[str, int]:
        rows = fetch_export_rows()
        inserted = 0
        duplicates = 0
        invalid = 0
        with history._db() as con:
            for raw in rows:
                if bool(pwexp._first(raw, "_suppressed", "suppressed", "is_suppressed", "isSuppressed")):
                    continue
                row = normalize_export_row(raw)
                if not row:
                    invalid += 1
                    continue
                if natural_duplicate(con, row):
                    duplicates += 1
                    continue
                before = con.total_changes
                history._insert_alert(con, row)
                if con.total_changes > before:
                    inserted += 1
                    con.execute(
                        """
                        UPDATE alerts
                        SET research_source='pw-server', research_raw_json=?
                        WHERE source_key=?
                        """,
                        (json.dumps(raw, separators=(",", ":"), default=str), f"event:{row['event_id']}"),
                    )
            history._grade_all(con)
        return {"server_rows": len(rows), "inserted": inserted, "duplicates": duplicates, "invalid": invalid}

    def parse_int(value: Any) -> int | None:
        try:
            if value in (None, ""):
                return None
            return int(value)
        except Exception:
            try:
                return int(float(str(value)))
            except Exception:
                return None

    def play_id(play: dict[str, Any], index: int) -> str:
        raw = play.get("id") or play.get("sequenceNumber") or play.get("sequence") or play.get("uid")
        return str(raw) if raw not in (None, "") else f"idx-{index}"

    def fetch_game_pbp(game_id: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        r = httpx.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary",
            params={"event": game_id},
            timeout=12.0,
        )
        r.raise_for_status()
        data = r.json()
        plays = data.get("plays") or []
        if not isinstance(plays, list):
            plays = []
        return [p for p in plays if isinstance(p, dict)], data

    def upsert_pbp_game(game_id: str, plays: list[dict[str, Any]]) -> int:
        inserted = 0
        now = datetime.now(timezone.utc).isoformat()
        with history._db() as con:
            for idx, play in enumerate(plays):
                period_obj = play.get("period") or {}
                clock_obj = play.get("clock") or {}
                team_obj = play.get("team") or {}
                type_obj = play.get("type") or {}
                period = parse_int(period_obj.get("number") if isinstance(period_obj, dict) else period_obj)
                away_score = parse_int(play.get("awayScore"))
                home_score = parse_int(play.get("homeScore"))
                diff = None
                if home_score is not None and away_score is not None:
                    diff = home_score - away_score
                pid = play_id(play, idx)
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO play_by_play(
                        game_id,play_id,sequence_no,event_ts,period,quarter,clock,
                        away_score,home_score,score_diff_home,event_type,team_id,team_abbr,
                        text,scoring_play,score_value,source,raw_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        game_id,
                        pid,
                        parse_int(play.get("sequenceNumber") or play.get("sequence")) or idx,
                        str(play.get("wallclock") or play.get("modified") or "") or None,
                        period,
                        f"Q{period}" if period and 1 <= period <= 4 else (f"OT{period-4}" if period and period > 4 else None),
                        str(clock_obj.get("displayValue") if isinstance(clock_obj, dict) else clock_obj or "") or None,
                        away_score,
                        home_score,
                        diff,
                        str(type_obj.get("text") if isinstance(type_obj, dict) else type_obj or "") or None,
                        str(team_obj.get("id") if isinstance(team_obj, dict) else "") or None,
                        str(team_obj.get("abbreviation") if isinstance(team_obj, dict) else "") or None,
                        str(play.get("text") or play.get("shortText") or "") or None,
                        1 if play.get("scoringPlay") else 0,
                        parse_int(play.get("scoreValue")),
                        "espn",
                        json.dumps(play, separators=(",", ":"), default=str),
                        now,
                    ),
                )
                if cur.rowcount:
                    inserted += 1
        return inserted

    def import_play_by_play() -> dict[str, int]:
        with history._db() as con:
            game_ids = [
                str(r["game_id"])
                for r in con.execute(
                    """
                    SELECT DISTINCT a.game_id
                    FROM alerts a
                    LEFT JOIN (
                        SELECT DISTINCT game_id FROM play_by_play
                    ) p ON p.game_id=a.game_id
                    WHERE a.game_id IS NOT NULL AND a.game_id<>'' AND p.game_id IS NULL
                    ORDER BY a.game_id
                    """
                ).fetchall()
            ]
        games_ok = 0
        games_error = 0
        plays_seen = 0
        plays_inserted = 0
        for i, game_id in enumerate(game_ids, start=1):
            try:
                plays, _ = fetch_game_pbp(game_id)
                plays_seen += len(plays)
                plays_inserted += upsert_pbp_game(game_id, plays)
                games_ok += 1
            except Exception as exc:
                games_error += 1
                print(f"PW_RESEARCH_PBP_ERROR game_id={game_id} error={type(exc).__name__}:{exc}", flush=True)
            if i % 25 == 0:
                print(
                    f"PW_RESEARCH_PBP_PROGRESS games={i}/{len(game_ids)} plays_inserted={plays_inserted}",
                    flush=True,
                )
            time.sleep(0.03)
        return {
            "games_total": len(game_ids),
            "games_ok": games_ok,
            "games_error": games_error,
            "plays_seen": plays_seen,
            "plays_inserted": plays_inserted,
        }

    def summary() -> dict[str, Any]:
        with history._db() as con:
            alerts = con.execute("SELECT COUNT(*) c FROM alerts").fetchone()["c"]
            games = con.execute("SELECT COUNT(DISTINCT game_id) c FROM alerts WHERE game_id IS NOT NULL").fetchone()["c"]
            plays = con.execute("SELECT COUNT(*) c FROM play_by_play").fetchone()["c"]
            pbp_games = con.execute("SELECT COUNT(DISTINCT game_id) c FROM play_by_play").fetchone()["c"]
            research_alerts = con.execute("SELECT COUNT(*) c FROM alerts WHERE research_source='pw-server'").fetchone()["c"]
        return {
            "alerts": int(alerts or 0),
            "games_with_pw": int(games or 0),
            "play_by_play_rows": int(plays or 0),
            "games_with_play_by_play": int(pbp_games or 0),
            "research_imported_pw": int(research_alerts or 0),
        }

    def sync_all() -> dict[str, Any]:
        if not lock.acquire(blocking=False):
            state = load_state()
            return {"running": True, **state}
        try:
            init_schema()
            state = load_state()
            state.update({"running": True, "started_at": datetime.now(timezone.utc).isoformat(), "last_error": None})
            save_state(state)
            pw_result: dict[str, Any] = {}
            pbp_result: dict[str, Any] = {}
            try:
                pw_result = import_missing_pw()
                print(
                    "PW_RESEARCH_PW_SYNC "
                    f"rows={pw_result['server_rows']} inserted={pw_result['inserted']} "
                    f"duplicates={pw_result['duplicates']} invalid={pw_result['invalid']}",
                    flush=True,
                )
            except Exception as exc:
                pw_result = {"error": f"{type(exc).__name__}: {exc}"}
                print(f"PW_RESEARCH_PW_ERROR {pw_result['error']}", flush=True)

            if pbp_enabled:
                try:
                    pbp_result = import_play_by_play()
                    print(
                        "PW_RESEARCH_PBP_DONE "
                        f"games={pbp_result['games_ok']}/{pbp_result['games_total']} "
                        f"plays_seen={pbp_result['plays_seen']} inserted={pbp_result['plays_inserted']}",
                        flush=True,
                    )
                except Exception as exc:
                    pbp_result = {"error": f"{type(exc).__name__}: {exc}"}
                    print(f"PW_RESEARCH_PBP_FATAL {pbp_result['error']}", flush=True)

            state.update(
                {
                    "running": False,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "pw": pw_result,
                    "play_by_play": pbp_result,
                    "summary": summary(),
                }
            )
            save_state(state)
            return state
        finally:
            lock.release()

    @app.get("/api/pw-research/status", dependencies=[Depends(dashboard._auth)])
    def research_status():
        init_schema()
        return {
            "enabled": enabled,
            "play_by_play_enabled": pbp_enabled,
            "export_source": export_source,
            "since": since,
            "summary": summary(),
            **load_state(),
        }

    @app.get("/api/pw-research/game/{game_id}", dependencies=[Depends(dashboard._auth)])
    def research_game(game_id: str):
        init_schema()
        with history._db() as con:
            alerts = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT * FROM alerts WHERE game_id=?
                    ORDER BY event_ts ASC,id ASC
                    """,
                    (game_id,),
                ).fetchall()
            ]
            plays = [
                dict(r)
                for r in con.execute(
                    """
                    SELECT game_id,play_id,sequence_no,event_ts,period,quarter,clock,
                           away_score,home_score,score_diff_home,event_type,team_id,team_abbr,
                           text,scoring_play,score_value,source
                    FROM play_by_play WHERE game_id=?
                    ORDER BY sequence_no ASC
                    """,
                    (game_id,),
                ).fetchall()
            ]
        return {"game_id": game_id, "pw_calls": alerts, "play_by_play": plays}

    @app.post("/api/pw-research/sync", dependencies=[Depends(dashboard._auth)])
    def research_sync():
        return sync_all()

    init_schema()
    if enabled:
        def boot_sync() -> None:
            time.sleep(4.0)
            state = sync_all()
            # Play-by-play is durable after the first pass. If the private PW
            # server is temporarily offline, retry only the research-only PW
            # backfill so historical calls are never routed through trading.
            while isinstance((state.get("pw") or {}), dict) and (state.get("pw") or {}).get("error"):
                time.sleep(20.0)
                try:
                    pw_result = import_missing_pw()
                    state = load_state()
                    state["pw"] = pw_result
                    state["summary"] = summary()
                    state["pw_retry_completed_at"] = datetime.now(timezone.utc).isoformat()
                    save_state(state)
                    print(
                        "PW_RESEARCH_PW_RETRY_OK "
                        f"rows={pw_result['server_rows']} inserted={pw_result['inserted']} "
                        f"duplicates={pw_result['duplicates']} invalid={pw_result['invalid']}",
                        flush=True,
                    )
                    break
                except Exception as exc:
                    state = load_state()
                    state["pw"] = {"error": f"{type(exc).__name__}: {exc}"}
                    state["pw_retry_at"] = datetime.now(timezone.utc).isoformat()
                    save_state(state)
                    print(f"PW_RESEARCH_PW_RETRY_ERROR {type(exc).__name__}:{exc}", flush=True)
        _THREAD = threading.Thread(target=boot_sync, name="pw-research-sync", daemon=True)
        _THREAD.start()
        print(
            f"PW_RESEARCH_SYNC_READY enabled=true source={export_source} since={since} pbp={pbp_enabled}",
            flush=True,
        )
    else:
        print("PW_RESEARCH_SYNC_READY enabled=false", flush=True)
