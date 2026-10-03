from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends, HTTPException, Query

_INSTALLED = False
_LOCK = threading.Lock()
_MARKER = "AUDIT_RESEARCH_V1"
_DEFAULT_REPO = "s01100011-a11y/polymarket-auto-bot"
_DEFAULT_ISSUE = 227
_MAX_HISTORY = 10000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _slug(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _clean(value).casefold()).strip("-")


def _canonical_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("research payload must be an object")

    source = _clean(payload.get("source"))
    data_type = _clean(payload.get("data_type"))
    if not source:
        raise ValueError("source is required")
    if not data_type:
        raise ValueError("data_type is required")

    confidence = _clean(payload.get("confidence") or "researched").lower()
    if confidence not in {"verified", "researched", "derived"}:
        confidence = "researched"

    observed_at = _clean(payload.get("observed_at") or payload.get("retrieved_at") or _now_iso())
    retrieved_at = _clean(payload.get("retrieved_at") or _now_iso())

    out: dict[str, Any] = {
        "sport": _clean(payload.get("sport")).upper(),
        "league": _clean(payload.get("league")).upper(),
        "event": _clean(payload.get("event")),
        "event_id": _clean(payload.get("event_id")),
        "matchup": _clean(payload.get("matchup")),
        "subject": _clean(payload.get("subject")),
        "data_type": data_type,
        "market": _clean(payload.get("market")),
        "value": payload.get("value"),
        "data": payload.get("data") if isinstance(payload.get("data"), dict) else {},
        "source": source,
        "source_url": _clean(payload.get("source_url")),
        "observed_at": observed_at,
        "retrieved_at": retrieved_at,
        "confidence": confidence,
        "notes": _clean(payload.get("notes")),
        "tags": [str(x).strip() for x in (payload.get("tags") or []) if str(x).strip()],
    }
    freshness = payload.get("freshness_seconds")
    if freshness not in (None, ""):
        try:
            out["freshness_seconds"] = max(0, int(freshness))
        except (TypeError, ValueError):
            pass
    return out


def _entity_key(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("sport") or payload.get("league") or "global",
        payload.get("event_id") or payload.get("event") or payload.get("matchup") or "general",
        payload.get("data_type") or "research",
        payload.get("market") or "all",
        payload.get("subject") or "all",
    ]
    return ":".join(_slug(part) or "all" for part in parts)


def _record_id(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _store_path(core: Any):
    return core.DATA_DIR / "audit_research_store.json"


def _state_path(core: Any):
    return core.DATA_DIR / "audit_research_ingest_state.json"


def _load_store(core: Any) -> dict[str, Any]:
    raw = core._load(_store_path(core))
    if not isinstance(raw, dict):
        raw = {}
    latest = raw.get("latest") if isinstance(raw.get("latest"), dict) else {}
    history = raw.get("history") if isinstance(raw.get("history"), list) else []
    pending = raw.get("pending_remote") if isinstance(raw.get("pending_remote"), dict) else {}
    return {"version": 1, "latest": latest, "history": history, "pending_remote": pending}


def _save_store(core: Any, store: dict[str, Any]) -> None:
    history = store.get("history") or []
    if len(history) > _MAX_HISTORY:
        store["history"] = history[-_MAX_HISTORY:]
    core._save(_store_path(core), store)


def persist(core: Any, payload: dict[str, Any], *, origin: str = "api", origin_id: Any = None) -> dict[str, Any]:
    canonical = _canonical_payload(payload)
    entity_key = _entity_key(canonical)
    record_basis = dict(canonical)
    record_basis["origin"] = origin
    if origin_id is not None:
        record_basis["origin_id"] = str(origin_id)
    rid = _record_id(record_basis)

    with _LOCK:
        store = _load_store(core)
        for existing in reversed(store["history"][-500:]):
            if isinstance(existing, dict) and existing.get("id") == rid:
                return dict(existing)

        record = dict(canonical)
        record.update(
            {
                "id": rid,
                "entity_key": entity_key,
                "origin": origin,
                "origin_id": str(origin_id) if origin_id is not None else None,
                "ingested_at": _now_iso(),
                "remote_synced": False,
                "remote_error": None,
            }
        )
        store["history"].append(record)
        store["latest"][entity_key] = record
        store["pending_remote"][rid] = record
        _save_store(core, store)
        return dict(record)


def _parse_issue_comment(body: str) -> list[dict[str, Any]]:
    text = str(body or "")
    if _MARKER not in text:
        return []
    tail = text.split(_MARKER, 1)[1].strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", tail, flags=re.I | re.S)
    candidate = fenced.group(1).strip() if fenced else tail.strip()
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, dict):
        if isinstance(parsed.get("records"), list):
            return [x for x in parsed["records"] if isinstance(x, dict)]
        return [parsed]
    if isinstance(parsed, list):
        return [x for x in parsed if isinstance(x, dict)]
    return []


def _filtered_records(core: Any, *, sport: str = "", data_type: str = "", event: str = "", limit: int = 100, latest_only: bool = True) -> list[dict[str, Any]]:
    with _LOCK:
        store = _load_store(core)
    rows = list(store["latest"].values()) if latest_only else list(store["history"])
    sport_key = _clean(sport).upper()
    type_key = _clean(data_type).casefold()
    event_key = _clean(event).casefold()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        if sport_key and _clean(row.get("sport")).upper() != sport_key:
            continue
        if type_key and _clean(row.get("data_type")).casefold() != type_key:
            continue
        if event_key:
            hay = " ".join(_clean(row.get(k)) for k in ("event", "event_id", "matchup", "subject")).casefold()
            if event_key not in hay:
                continue
        out.append(dict(row))
        if len(out) >= limit:
            break
    return out


def install(*, app: Any, dashboard: Any, core: Any) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    base_url = os.getenv("AUDIT_CORE_URL", "").strip().rstrip("/")
    token = os.getenv("AUDIT_CORE_TOKEN", "").strip()
    remote_path = os.getenv("AUDIT_CORE_RESEARCH_PATH", "/api/core/research").strip() or "/api/core/research"
    poll_seconds = max(15, int(os.getenv("AUDIT_RESEARCH_GITHUB_POLL_SECONDS", "60")))
    sync_seconds = max(5, int(os.getenv("AUDIT_RESEARCH_SYNC_SECONDS", "15")))
    github_repo = os.getenv("AUDIT_RESEARCH_GITHUB_REPO", _DEFAULT_REPO).strip()
    try:
        github_issue = int(os.getenv("AUDIT_RESEARCH_GITHUB_ISSUE", str(_DEFAULT_ISSUE)))
    except ValueError:
        github_issue = _DEFAULT_ISSUE

    status: dict[str, Any] = {
        "enabled": True,
        "github_repo": github_repo,
        "github_issue": github_issue,
        "last_github_poll_at": None,
        "last_github_success_at": None,
        "last_github_error": None,
        "last_remote_success_at": None,
        "last_remote_error": None,
        "ingested_comments": 0,
        "ingested_records": 0,
        "remote_posted": 0,
    }

    async def _poll_github(client: httpx.AsyncClient) -> None:
        state = core._load(_state_path(core))
        if not isinstance(state, dict):
            state = {}
        last_comment_id = int(state.get("last_comment_id") or 0)
        url = f"https://api.github.com/repos/{github_repo}/issues/{github_issue}/comments"
        response = await client.get(
            url,
            params={"per_page": 100},
            headers={"Accept": "application/vnd.github+json", "User-Agent": "audit-db-research-ingest/1.0"},
            timeout=10.0,
        )
        response.raise_for_status()
        comments = response.json()
        if not isinstance(comments, list):
            return
        max_seen = last_comment_id
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            cid = int(comment.get("id") or 0)
            max_seen = max(max_seen, cid)
            if cid <= last_comment_id:
                continue
            records = _parse_issue_comment(str(comment.get("body") or ""))
            if not records:
                continue
            count = 0
            for payload in records:
                persist(core, payload, origin="github_issue", origin_id=cid)
                count += 1
            if count:
                status["ingested_comments"] = int(status["ingested_comments"]) + 1
                status["ingested_records"] = int(status["ingested_records"]) + count
        if max_seen > last_comment_id:
            state["last_comment_id"] = max_seen
            state["updated_at"] = _now_iso()
            core._save(_state_path(core), state)
        status["last_github_success_at"] = _now_iso()
        status["last_github_error"] = None

    async def _sync_remote(client: httpx.AsyncClient) -> None:
        if not base_url or not token:
            status["last_remote_error"] = "AUDIT_CORE_URL/AUDIT_CORE_TOKEN not configured"
            return
        with _LOCK:
            store = _load_store(core)
            pending = list(store["pending_remote"].items())[:100]
        if not pending:
            return
        changed = False
        for rid, record in pending:
            try:
                response = await client.post(
                    f"{base_url}{remote_path}",
                    headers={"X-Audit-Core-Token": token},
                    json=record,
                    timeout=8.0,
                )
                response.raise_for_status()
            except Exception as exc:
                status["last_remote_error"] = f"{type(exc).__name__}: {exc}"
                break
            with _LOCK:
                store = _load_store(core)
                stored = store["pending_remote"].pop(rid, None)
                if isinstance(stored, dict):
                    stored = dict(stored)
                    stored["remote_synced"] = True
                    stored["remote_error"] = None
                    latest_key = stored.get("entity_key")
                    if latest_key and isinstance(store["latest"].get(latest_key), dict) and store["latest"][latest_key].get("id") == rid:
                        store["latest"][latest_key] = stored
                    for idx in range(len(store["history"]) - 1, -1, -1):
                        row = store["history"][idx]
                        if isinstance(row, dict) and row.get("id") == rid:
                            store["history"][idx] = stored
                            break
                    _save_store(core, store)
                    changed = True
            status["remote_posted"] = int(status["remote_posted"]) + 1
            status["last_remote_success_at"] = _now_iso()
            status["last_remote_error"] = None
        if changed:
            return

    async def _loop() -> None:
        async with httpx.AsyncClient() as client:
            github_due = 0.0
            loop = asyncio.get_running_loop()
            while True:
                now = loop.time()
                if now >= github_due:
                    status["last_github_poll_at"] = _now_iso()
                    try:
                        await _poll_github(client)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        status["last_github_error"] = f"{type(exc).__name__}: {exc}"
                    github_due = now + poll_seconds
                try:
                    await _sync_remote(client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    status["last_remote_error"] = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(sync_seconds)

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def _research_lifespan(application):
        async with previous_lifespan(application):
            task = asyncio.create_task(_loop())
            try:
                yield
            finally:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    app.router.lifespan_context = _research_lifespan

    @app.get("/api/audit/research/status", dependencies=[Depends(dashboard._auth)])
    async def _research_status():
        with _LOCK:
            store = _load_store(core)
        out = dict(status)
        out.update(
            {
                "latest_count": len(store["latest"]),
                "history_count": len(store["history"]),
                "pending_remote": len(store["pending_remote"]),
            }
        )
        return out

    @app.get("/api/audit/research", dependencies=[Depends(dashboard._auth)])
    async def _research_get(
        sport: str = Query(default=""),
        data_type: str = Query(default=""),
        event: str = Query(default=""),
        limit: int = Query(default=100, ge=1, le=1000),
        latest_only: bool = Query(default=True),
    ):
        return {
            "records": _filtered_records(
                core,
                sport=sport,
                data_type=data_type,
                event=event,
                limit=limit,
                latest_only=latest_only,
            )
        }

    @app.post("/api/audit/research", dependencies=[Depends(dashboard._auth)])
    async def _research_post(payload: dict[str, Any]):
        try:
            record = persist(core, payload, origin="api")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "record": record}
