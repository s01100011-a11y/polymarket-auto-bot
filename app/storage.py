from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


class TrackedDict(dict):
    """Dictionary loaded from SQLite with its original snapshot attached.

    _save() can use this snapshot to apply only the caller's actual changes,
    which prevents an unrelated concurrent insert from being overwritten by a
    stale load-modify-save cycle.
    """

    def __init__(self, *args: Any, namespace: str, table: str, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._storage_namespace = namespace
        self._storage_table = table
        self._storage_original = dict(self)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class SQLiteStateStore:
    TABLES = ("executions", "watchlist", "pending", "settings", "pnl_history", "state")

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path, timeout=10.0, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                for table in self.TABLES:
                    con.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS {table} (
                            namespace TEXT NOT NULL,
                            key TEXT NOT NULL,
                            payload TEXT NOT NULL,
                            updated_at TEXT NOT NULL,
                            PRIMARY KEY(namespace, key)
                        )
                        """
                    )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS storage_migrations (
                        source_path TEXT PRIMARY KEY,
                        imported_at TEXT NOT NULL,
                        item_count INTEGER NOT NULL,
                        schema_version INTEGER NOT NULL
                    )
                    """
                )
                con.execute(
                    """
                    CREATE TABLE IF NOT EXISTS storage_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                con.execute(
                    "INSERT OR REPLACE INTO storage_meta(key,value) VALUES('schema_version',?)",
                    (str(SCHEMA_VERSION),),
                )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def namespace_for(path: Path) -> str:
        return Path(path).name

    @staticmethod
    def table_for(path: Path) -> str:
        name = Path(path).name.casefold()
        if name == "executions.json":
            return "executions"
        if name == "watchlist.json":
            return "watchlist"
        if name == "pending.json":
            return "pending"
        if "pnl_history" in name:
            return "pnl_history"
        if (
            "settings" in name
            or "auto_trading_state" in name
            or "trading_mode" in name
        ):
            return "settings"
        return "state"

    def load(self, path: Path) -> TrackedDict:
        path = Path(path)
        self.migrate_legacy_json(path)
        namespace = self.namespace_for(path)
        table = self.table_for(path)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT key,payload FROM {table} WHERE namespace=?",
                (namespace,),
            ).fetchall()
        data: dict[str, Any] = {}
        for row in rows:
            try:
                data[str(row["key"])] = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
        return TrackedDict(data, namespace=namespace, table=table)

    def save(self, path: Path, data: dict[str, Any]) -> None:
        path = Path(path)
        namespace = self.namespace_for(path)
        table = self.table_for(path)
        now = _utcnow()

        tracked = (
            isinstance(data, TrackedDict)
            and data._storage_namespace == namespace
            and data._storage_table == table
        )
        if tracked:
            original = data._storage_original
            deletes = sorted(set(original) - set(data))
            changes = {
                str(key): value
                for key, value in data.items()
                if key not in original or original.get(key) != value
            }
            if not deletes and not changes:
                return
            with self._connect() as con:
                con.execute("BEGIN IMMEDIATE")
                try:
                    for key in deletes:
                        con.execute(
                            f"DELETE FROM {table} WHERE namespace=? AND key=?",
                            (namespace, str(key)),
                        )
                    for key, value in changes.items():
                        con.execute(
                            f"""
                            INSERT INTO {table}(namespace,key,payload,updated_at)
                            VALUES(?,?,?,?)
                            ON CONFLICT(namespace,key) DO UPDATE SET
                                payload=excluded.payload,
                                updated_at=excluded.updated_at
                            """,
                            (
                                namespace,
                                key,
                                json.dumps(value, separators=(",", ":"), default=str),
                                now,
                            ),
                        )
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    raise
            data._storage_original = dict(data)
            return

        # Plain dictionaries represent an intentional full replacement.
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                con.execute(f"DELETE FROM {table} WHERE namespace=?", (namespace,))
                for key, value in data.items():
                    con.execute(
                        f"INSERT INTO {table}(namespace,key,payload,updated_at) VALUES(?,?,?,?)",
                        (
                            namespace,
                            str(key),
                            json.dumps(value, separators=(",", ":"), default=str),
                            now,
                        ),
                    )
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise

    def migrate_legacy_json(self, path: Path) -> int:
        path = Path(path)
        source = str(path.resolve())
        with self._connect() as con:
            done = con.execute(
                "SELECT 1 FROM storage_migrations WHERE source_path=?",
                (source,),
            ).fetchone()
        if done:
            return 0

        payload: dict[str, Any] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text())
                if isinstance(raw, dict):
                    payload = raw
            except (OSError, json.JSONDecodeError):
                payload = {}

        namespace = self.namespace_for(path)
        table = self.table_for(path)
        now = _utcnow()
        with self._connect() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                # Only seed an empty namespace; never overwrite SQLite data with
                # an old JSON snapshot after cutover.
                existing = con.execute(
                    f"SELECT 1 FROM {table} WHERE namespace=? LIMIT 1",
                    (namespace,),
                ).fetchone()
                imported = 0
                if not existing:
                    for key, value in payload.items():
                        con.execute(
                            f"INSERT INTO {table}(namespace,key,payload,updated_at) VALUES(?,?,?,?)",
                            (
                                namespace,
                                str(key),
                                json.dumps(value, separators=(",", ":"), default=str),
                                now,
                            ),
                        )
                        imported += 1
                con.execute(
                    """
                    INSERT OR REPLACE INTO storage_migrations(
                        source_path,imported_at,item_count,schema_version
                    ) VALUES(?,?,?,?)
                    """,
                    (source, now, imported, SCHEMA_VERSION),
                )
                con.execute("COMMIT")
                return imported
            except Exception:
                con.execute("ROLLBACK")
                raise

    def migrate_many(self, paths: Iterable[Path]) -> dict[str, int]:
        return {str(Path(path)): self.migrate_legacy_json(Path(path)) for path in paths}
