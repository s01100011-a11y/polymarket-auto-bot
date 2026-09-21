from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
import socket
import subprocess
import threading
import time
from urllib.parse import urlencode, urlsplit
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends

_INSTALLED = False
_THREAD: threading.Thread | None = None


def _tailnet_https_json(
    url: str,
    params: dict[str, Any],
    proxy: str,
    tailnet_peer: str = "",
    *,
    timeout: float = 8.0,
    user_agent: str = "railway-pw-export/3",
) -> tuple[Any, str]:
    """GET JSON through tailscaled's outbound HTTP proxy.

    Prefer the MagicDNS hostname so Tailscale resolves/routes the current node.
    Fall back to the configured peer IP for compatibility. Use a fresh tunnel
    per request so a stale TLS socket cannot poison later polls (#23).
    """
    target = urlsplit(url)
    if target.scheme.lower() != "https" or not target.hostname:
        raise RuntimeError("PW export URL must be a valid https URL")

    proxy_url = urlsplit(proxy)
    proxy_host = proxy_url.hostname or "127.0.0.1"
    proxy_port = proxy_url.port or 1055
    target_port = target.port or 443
    path = target.path or "/"
    query = urlencode(params)
    request_path = f"{path}?{query}" if query else path
    host_header = target.hostname if target_port == 443 else f"{target.hostname}:{target_port}"

    routes: list[str] = []
    for candidate in (target.hostname, tailnet_peer):
        if candidate and candidate not in routes:
            routes.append(candidate)

    last_exc: Exception | None = None
    for connect_host in routes:
        raw = None
        tls = None
        try:
            raw = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
            raw.settimeout(timeout)
            authority = f"{connect_host}:{target_port}"
            raw.sendall(
                (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    "Proxy-Connection: close\r\n"
                    "\r\n"
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
                raise RuntimeError(f"Tailscale CONNECT failed via {connect_host}: {status_line}")

            context = ssl.create_default_context()
            tls = context.wrap_socket(raw, server_hostname=target.hostname)
            raw = None  # tls owns the underlying socket now
            tls.settimeout(timeout)
            tls.sendall(
                (
                    f"GET {request_path} HTTP/1.1\r\n"
                    f"Host: {host_header}\r\n"
                    "Accept: application/json\r\n"
                    "Connection: close\r\n"
                    f"User-Agent: {user_agent}\r\n"
                    "\r\n"
                ).encode("ascii")
            )
            response = http.client.HTTPResponse(tls)
            response.begin()
            body = response.read()
            if response.status >= 400:
                preview = body[:240].decode("utf-8", "replace").replace("\n", " ")
                raise RuntimeError(f"PW export HTTP {response.status}: {preview}")
            return json.loads(body.decode("utf-8")), connect_host
        except Exception as exc:
            last_exc = exc
        finally:
            if tls is not None:
                try:
                    tls.close()
                except Exception:
                    pass
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass

    raise RuntimeError(f"PW export fetch failed on routes {routes}: {last_exc}")


def _first(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row.get(key) is not None:
            return row.get(key)
    return None


def _records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("calls", "records", "items", "alerts", "results", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
        if isinstance(value, dict):
            nested = _records(value)
            if nested:
                return nested
    # Some flat APIs return a single record object.
    if any(k in payload for k in ("predicted_winner", "game_id", "gameId", "call_id")):
        return [payload]
    return []


def _identity(row: dict[str, Any]) -> str:
    raw = _first(
        row,
        "source_key",
        "call_id",
        "alert_id",
        "id",
        "event_id",
        "eventId",
        "uuid",
        "key",
    )
    if raw not in (None, ""):
        return str(raw)

    # /api/pw-export rows do not currently expose a dedicated call id.
    # Build the identity only from fields that belong to the call itself.
    # Do not hash grading/final-score fields because those can change later.
    stable_parts = [
        _first(row, "game_id", "gameId"),
        _first(row, "ts", "event_ts", "timestamp"),
        _first(row, "predicted_team", "predicted_winner", "predictedWinner", "pick"),
        _first(row, "quarter", "period", "q"),
        _first(row, "basis"),
        _first(row, "pw_version"),
        _first(row, "model_schema_version"),
    ]
    if any(x not in (None, "") for x in stable_parts):
        stable = "|".join("" if x is None else str(x) for x in stable_parts)
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    stable = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()
def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        n = float(value)
        if n > 10_000_000_000:
            n /= 1000.0
        try:
            return datetime.fromtimestamp(n, tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        try:
            return _parse_dt(float(text))
        except Exception:
            pass
    candidates = [
        text.replace("Z", "+00:00"),
        text.replace(" +08", "+08:00") if text.endswith(" +08") else text,
    ]
    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            pass
    return None


def _row_dt(row: dict[str, Any]) -> datetime | None:
    return _parse_dt(
        _first(
            row,
            "event_ts",
            "timestamp",
            "created_at",
            "createdAt",
            "alert_ts",
            "alert_time",
            "time",
            "ts",
            "published_at",
        )
    )


def _num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    try:
        return float(text)
    except Exception:
        return None


def _int_odds(value: Any) -> int | None:
    n = _num(value)
    if n is None:
        return None
    return int(round(n))


def _team_name(value: Any, history: Any) -> str | None:
    if value in (None, ""):
        return None
    raw = str(value).strip()
    if raw in history.TEAM_ABBR:
        return raw
    upper = raw.upper().replace(".", "")
    reverse = {str(v).upper(): k for k, v in history.TEAM_ABBR.items()}
    if upper in reverse:
        return reverse[upper]

    aliases = {
        "ATLANTA": "Atlanta Dream",
        "DREAM": "Atlanta Dream",
        "CHICAGO": "Chicago Sky",
        "SKY": "Chicago Sky",
        "CONNECTICUT": "Connecticut Sun",
        "SUN": "Connecticut Sun",
        "DALLAS": "Dallas Wings",
        "WINGS": "Dallas Wings",
        "GOLDEN STATE": "Golden State Valkyries",
        "VALKYRIES": "Golden State Valkyries",
        "INDIANA": "Indiana Fever",
        "FEVER": "Indiana Fever",
        "LAS VEGAS": "Las Vegas Aces",
        "ACES": "Las Vegas Aces",
        "LOS ANGELES": "Los Angeles Sparks",
        "SPARKS": "Los Angeles Sparks",
        "MINNESOTA": "Minnesota Lynx",
        "LYNX": "Minnesota Lynx",
        "NEW YORK": "New York Liberty",
        "LIBERTY": "New York Liberty",
        "PHOENIX": "Phoenix Mercury",
        "MERCURY": "Phoenix Mercury",
        "PORTLAND": "Portland Fire",
        "FIRE": "Portland Fire",
        "SEATTLE": "Seattle Storm",
        "STORM": "Seattle Storm",
        "TORONTO": "Toronto Tempo",
        "TEMPO": "Toronto Tempo",
        "WASHINGTON": "Washington Mystics",
        "MYSTICS": "Washington Mystics",
    }
    return aliases.get(upper)


def _normalize_quarter(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().upper()
    m = re.search(r"Q?([1-4])", text)
    return f"Q{m.group(1)}" if m else None


def _synth_text(row: dict[str, Any], history: Any) -> tuple[str | None, dict[str, Any]]:
    existing = _first(row, "text", "message", "raw_text", "alert_text")
    game_id = _first(row, "game_id", "gameId", "espn_game_id", "espnGameId")
    pick = _team_name(
        _first(
            row,
            "predicted_winner",
            "predictedWinner",
            "predicted_team",
            "predictedTeam",
            "pick",
            "selection",
            "team",
            "winner_pick",
        ),
        history,
    )
    quarter = _normalize_quarter(_first(row, "quarter", "period", "q"))
    probability = _num(
        _first(
            row,
            "win_probability",
            "winProbability",
            "win_prob",
            "probability",
            "pct",
            "confidence",
        )
    )
    if probability is not None and 0 <= probability <= 1:
        probability *= 100.0
    bk_ml = _int_odds(
        _first(
            row,
            "bk_ml",
            "bkML",
            "bk_moneyline",
            "bkMoneyline",
            "bk_odds",
            "bkOdds",
            "moneyline",
            "ml",
            "odds",
        )
    )
    score = _first(row, "score_at_alert", "scoreAtAlert", "score")
    if score in (None, ""):
        away_score = _first(row, "away_score_at_fire", "away_score")
        home_score = _first(row, "home_score_at_fire", "home_score")
        if away_score not in (None, "") and home_score not in (None, ""):
            score = f"{away_score}-{home_score}"
    event_ts = _first(
        row,
        "event_ts",
        "timestamp",
        "created_at",
        "createdAt",
        "alert_ts",
        "alert_time",
        "time",
        "ts",
    )

    meta = {
        "game_id": str(game_id) if game_id not in (None, "") else None,
        "predicted_winner": pick,
        "quarter": quarter,
        "win_probability": probability,
        "bk_ml": bk_ml,
        "score_at_alert": str(score) if score not in (None, "") else None,
        "event_ts": str(event_ts) if event_ts not in (None, "") else None,
    }

    if existing:
        text = str(existing)
        # Preserve a rich original message when it already matches the parser.
        if "Predicted Winner" in text and re.search(r"gameId/\d+", text, re.I):
            return text, meta

    if not game_id or not pick:
        return None, meta

    lines = ["WNBA PW Alert"]
    if quarter:
        lines.append(quarter)
    if probability is not None:
        lines.append(f"Predicted Winner: *{pick}* ({probability:g}% win probability)")
    else:
        lines.append(f"Predicted Winner: *{pick}*")
    if bk_ml is not None:
        lines.append(f"BK ML: {bk_ml:+d}")
    if score:
        lines.append(f"Score: {score}")
    lines.append(f"https://site.api.espn.com/gameId/{game_id}")

    dt = _row_dt(row)
    if dt:
        kl = dt.astimezone(timezone(timedelta(hours=8)))
        lines.append(kl.strftime("%Y-%m-%d %H:%M:%S +08"))

    return "\n".join(lines), meta


def install(*, app: Any, ingest: Any, core: Any, history: Any, dashboard: Any) -> None:
    global _INSTALLED, _THREAD
    if _INSTALLED:
        return
    _INSTALLED = True

    enabled = os.getenv("PW_EXPORT_INGEST_ENABLED", "true").lower() == "true"
    poll_seconds = max(3.0, float(os.getenv("PW_EXPORT_POLL_SECONDS", "8")))
    max_age_seconds = max(30, int(os.getenv("PW_EXPORT_MAX_AGE_SECONDS", "180")))
    lookback_days = max(0, int(os.getenv("PW_EXPORT_POLL_LOOKBACK_DAYS", "1")))
    source = os.getenv("PW_EXPORT_SOURCE", "live").strip() or "live"
    include_suppressed = os.getenv("PW_EXPORT_INCLUDE_SUPPRESSED", "0").strip() or "0"
    url = os.getenv(
        "PW_WNBA_EXPORT_URL",
        "https://bob-mbp-ubuntu.taila35415.ts.net:8445/api/pw-export",
    ).strip()
    proxy = os.getenv("PW_EXPORT_SOCKS_PROXY", "socks5://127.0.0.1:1055").strip()
    tailnet_peer = os.getenv("PW_TAILNET_PEER", "100.81.244.65").strip()
    ts_socket = os.getenv("TS_SOCKET", "/tmp/tailscale/tailscaled.sock").strip()
    state_file = core.DATA_DIR / "pw_export_ingest_state.json"
    stop = threading.Event()

    def load_state() -> dict[str, Any]:
        try:
            state = core._load(state_file)
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def save_state(state: dict[str, Any]) -> None:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        core._save(state_file, state)

    def query_since() -> str:
        return (datetime.now(timezone.utc) - timedelta(days=lookback_days)).date().isoformat()

    def ensure_tailnet_ready() -> None:
        if not tailnet_peer:
            return
        try:
            proc = subprocess.run(
                [
                    "tailscale",
                    f"--socket={ts_socket}",
                    "ping",
                    "--timeout=2s",
                    "--c=1",
                    tailnet_peer,
                ],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:180]
                print(
                    f"PW_EXPORT_TAILNET_WAIT peer={tailnet_peer} rc={proc.returncode} detail={detail}",
                    flush=True,
                )
        except Exception as exc:
            print(
                f"PW_EXPORT_TAILNET_WAIT peer={tailnet_peer} error={type(exc).__name__}:{exc}",
                flush=True,
            )

    def fetch_rows() -> tuple[list[dict[str, Any]], Any, str]:
        params = {
            "source": source,
            "since": query_since(),
            "include_suppressed": include_suppressed,
        }
        ensure_tailnet_ready()
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                payload, route = _tailnet_https_json(
                    url,
                    params,
                    proxy,
                    tailnet_peer,
                    timeout=8.0,
                    user_agent="railway-pw-export-poller/3",
                )
                return _records(payload), payload, route
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(0.75 * (attempt + 1))
                    continue
                raise
        raise RuntimeError(f"PW export fetch failed: {last_exc}")

    def mark_seen(state: dict[str, Any], ids: list[str]) -> None:
        existing = [str(x) for x in (state.get("seen") or [])]
        merged = list(dict.fromkeys([*existing, *ids]))
        state["seen"] = merged[-10000:]

    def process(row: dict[str, Any], rec_id: str) -> tuple[str, str | None]:
        text, meta = _synth_text(row, history)
        if not text:
            return "INVALID", "missing game_id or recognized predicted_winner"

        event_id = f"pwexport-{rec_id}"
        event = {
            "channel": "pw-export",
            "ts": str(_first(row, "ts", "timestamp", "event_ts") or ""),
            "source": "pw-export",
        }
        parsed = ingest._parse_alert(text)
        existing_signal = None
        try:
            existing_signal = ingest._existing_pw_signal(
                core._load(ingest.SLACK_ALERTS_FILE),
                parsed,
                event_id,
            )
        except Exception:
            existing_signal = None

        alert: dict[str, Any] = {
            "event_id": event_id,
            "received_at": ingest._now_iso(),
            "channel": "pw-export",
            "ts": event.get("ts"),
            "text": text,
            "parsed": parsed,
            "pw_export_meta": meta,
            "pw_export_record": row,
            "paper_only": not bool(core.auto_trading_enabled()),
            "status": "RECEIVED",
        }

        if existing_signal:
            alert["status"] = "DUPLICATE_SIGNAL"
            alert["duplicate_of"] = existing_signal
            ingest._save_alert(event_id, alert)
            return "DUPLICATE_SIGNAL", None

        try:
            trade = ingest._paper_trade_from_alert(parsed, event_id, event)
            is_paper = bool(trade.get("paper", True))
            if is_paper:
                alert["status"] = "PAPER_TRADE_CREATED"
                alert["paper_trade_id"] = trade.get("id")
                alert["paper_trade"] = trade
            else:
                if trade.get("requires_approval"):
                    alert["status"] = "LIVE_TRADE_PREPARED"
                else:
                    alert["status"] = "LIVE_TRADE_QUEUED" if trade.get("queued") else "LIVE_TRADE_CREATED"
                alert["live_trade_id"] = trade.get("id") or trade.get("trade_id")
                alert["executor_request_id"] = trade.get("request_id")
                alert["live_trade"] = trade
        except Exception as exc:
            alert["status"] = "NO_TRADE"
            alert["error"] = str(exc)

        ingest._save_alert(event_id, alert)
        return str(alert["status"]), str(alert.get("error") or "") or None

    def loop() -> None:
        state = load_state()
        identity_version = 2
        if int(state.get("identity_version") or 0) != identity_version:
            # Identity logic changed after inspecting the live export schema.
            # Re-bootstrap safely so existing calls can never become "new".
            state["bootstrapped"] = False
            state["seen"] = []
            state["identity_version"] = identity_version
            state["identity_migrated_at"] = datetime.now(timezone.utc).isoformat()

        state.setdefault("processed", 0)
        state.setdefault("successful_polls", 0)
        state.setdefault("trade_actions", 0)
        state.setdefault("no_trade", 0)
        state.setdefault("invalid", 0)
        state.setdefault("errors", 0)

        # Give tailscaled/control-plane state a brief settling window after
        # container startup; Railway /health is already available independently.
        stop.wait(3.0)

        while not stop.is_set():
            started = time.time()
            try:
                rows, payload, route = fetch_rows()
                ids = [_identity(r) for r in rows]
                state["last_poll_at"] = datetime.now(timezone.utc).isoformat()
                state["last_poll_records"] = len(rows)
                state["last_http_status"] = 200
                state["last_error"] = None
                state["successful_polls"] = int(state.get("successful_polls") or 0) + 1
                state["consecutive_errors"] = 0
                state["last_success_at"] = datetime.now(timezone.utc).isoformat()
                state["last_route"] = route

                if not state.get("schema_logged") and rows:
                    print(
                        "PW_EXPORT_SCHEMA "
                        f"top={type(payload).__name__} "
                        f"record_keys={','.join(sorted(rows[0].keys()))}",
                        flush=True,
                    )
                    state["schema_logged"] = True

                if not state.get("bootstrapped"):
                    # Critical safety invariant: the first successful sync establishes
                    # the cursor only. Existing/historical records are NEVER executed.
                    mark_seen(state, ids)
                    state["bootstrapped"] = True
                    state["bootstrapped_at"] = datetime.now(timezone.utc).isoformat()
                    state["bootstrap_records"] = len(rows)
                    parseable = sum(1 for r in rows if _synth_text(r, history)[0])
                    state["bootstrap_parseable"] = parseable
                    save_state(state)
                    print(
                        f"PW_EXPORT_BOOTSTRAP records={len(rows)} parseable={parseable} action=cursor_only",
                        flush=True,
                    )
                else:
                    seen = set(str(x) for x in (state.get("seen") or []))
                    fresh = [(r, i) for r, i in zip(rows, ids) if i not in seen]
                    fresh.sort(key=lambda pair: _row_dt(pair[0]) or datetime.now(timezone.utc))

                    for row, rec_id in fresh:
                        # Respect an explicit suppression marker even if the API was
                        # configured to include suppressed rows for diagnostics.
                        if bool(_first(row, "_suppressed", "suppressed", "is_suppressed", "isSuppressed")):
                            mark_seen(state, [rec_id])
                            continue

                        row_dt = _row_dt(row)
                        if row_dt is not None:
                            age = (datetime.now(timezone.utc) - row_dt).total_seconds()
                            if age > max_age_seconds:
                                mark_seen(state, [rec_id])
                                print(
                                    f"PW_EXPORT_SKIP id={rec_id} reason=stale age_seconds={int(age)}",
                                    flush=True,
                                )
                                continue

                        status, error = process(row, rec_id)
                        mark_seen(state, [rec_id])
                        state["processed"] = int(state.get("processed") or 0) + 1
                        if status in {"LIVE_TRADE_QUEUED", "LIVE_TRADE_PREPARED", "LIVE_TRADE_CREATED", "PAPER_TRADE_CREATED"}:
                            state["trade_actions"] = int(state.get("trade_actions") or 0) + 1
                        elif status == "INVALID":
                            state["invalid"] = int(state.get("invalid") or 0) + 1
                        else:
                            state["no_trade"] = int(state.get("no_trade") or 0) + 1
                        state["last_record_id"] = rec_id
                        state["last_record_status"] = status
                        state["last_record_at"] = datetime.now(timezone.utc).isoformat()
                        state["last_record_error"] = error
                        save_state(state)
                        print(
                            f"PW_EXPORT_RECORD id={rec_id} status={status}"
                            + (f" error={error}" if error else ""),
                            flush=True,
                        )

                    if not fresh:
                        save_state(state)
                        print(
                            f"PW_EXPORT_POLL_OK records={len(rows)} fresh=0 poll={state.get('successful_polls')} route={route}",
                            flush=True,
                        )

            except Exception as exc:
                state["errors"] = int(state.get("errors") or 0) + 1
                state["consecutive_errors"] = int(state.get("consecutive_errors") or 0) + 1
                state["last_error"] = f"{type(exc).__name__}: {exc}"
                state["last_error_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    save_state(state)
                except Exception:
                    pass
                print(
                    f"PW_EXPORT_POLL_ERROR error={type(exc).__name__}:{exc}",
                    flush=True,
                )

            elapsed = time.time() - started
            stop.wait(max(0.5, poll_seconds - elapsed))

    @app.get("/api/pw-export/status", dependencies=[Depends(dashboard._auth)])
    def pw_export_status():
        state = load_state()
        return {
            "enabled": enabled,
            "url_configured": bool(url),
            "source": source,
            "poll_seconds": poll_seconds,
            "max_age_seconds": max_age_seconds,
            "lookback_days": lookback_days,
            "proxy": "socks5://127.0.0.1:1055" if proxy else None,
            "auto_trading": bool(core.auto_trading_enabled()),
            **state,
        }

    if enabled:
        _THREAD = threading.Thread(target=loop, name="pw-export-poller", daemon=True)
        _THREAD.start()
        print(
            f"PW_EXPORT_INGEST_READY enabled=true poll_seconds={poll_seconds:g} "
            f"source={source} bootstrap_only_first_sync=true",
            flush=True,
        )
    else:
        print("PW_EXPORT_INGEST_READY enabled=false", flush=True)
