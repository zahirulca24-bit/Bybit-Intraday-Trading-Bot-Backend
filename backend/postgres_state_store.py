"""Persistent PostgreSQL storage for restart-safe trading state.

The store intentionally exposes the same atomic interface used by the execution
handoff while adding versioned migrations and dedicated order/fill ledgers.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

try:
    import psycopg
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - reported by status()
    psycopg = None
    Jsonb = None

try:
    from .execution_command_storage import (
        EXECUTION_COMMAND_MIGRATION,
        ExecutionCommandStorageMixin,
    )
    from .live_execution_storage import LIVE_EXECUTION_MIGRATION
    from .replay_step_repository import REPLAY_STEP_MIGRATION
    from .replay_storage import REPLAY_MIGRATION, ReplayStorageMixin
except ImportError:
    from execution_command_storage import (
        EXECUTION_COMMAND_MIGRATION,
        ExecutionCommandStorageMixin,
    )
    from live_execution_storage import LIVE_EXECUTION_MIGRATION
    from replay_step_repository import REPLAY_STEP_MIGRATION
    from replay_storage import REPLAY_MIGRATION, ReplayStorageMixin


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS journal (id BIGSERIAL PRIMARY KEY, ts BIGINT NOT NULL, event TEXT NOT NULL, payload JSONB NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_journal_ts ON journal(ts DESC)",
            "CREATE TABLE IF NOT EXISTS runtime_state (key TEXT PRIMARY KEY, value JSONB NOT NULL, updated_at BIGINT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS orders (order_key TEXT PRIMARY KEY, symbol TEXT, side TEXT, status TEXT, payload JSONB NOT NULL, updated_at BIGINT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS fills (fill_key TEXT PRIMARY KEY, order_key TEXT, symbol TEXT, qty NUMERIC, price NUMERIC, payload JSONB NOT NULL, created_at BIGINT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS risk_snapshots (id BIGSERIAL PRIMARY KEY, trading_date TEXT, payload JSONB NOT NULL, created_at BIGINT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_risk_snapshots_created ON risk_snapshots(created_at DESC)",
            "CREATE TABLE IF NOT EXISTS reconciliation_runs (id BIGSERIAL PRIMARY KEY, status TEXT NOT NULL, payload JSONB NOT NULL, created_at BIGINT NOT NULL)",
        ),
    ),
    REPLAY_MIGRATION,
    REPLAY_STEP_MIGRATION,
    LIVE_EXECUTION_MIGRATION,
    EXECUTION_COMMAND_MIGRATION,
)


class PostgresStateStore(ExecutionCommandStorageMixin, ReplayStorageMixin):
    def __init__(self, database_url: str | None = None):
        self.database_url = database_url or os.environ.get("DATABASE_URL", "")
        self.lock = threading.RLock()
        self._last_error: str | None = None
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is required for persistent automatic execution.")
        if psycopg is None:
            raise RuntimeError("psycopg is not installed.")
        self.migrate()

    def connect(self):
        return psycopg.connect(self.database_url, connect_timeout=10)

    def migrate(self) -> None:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at BIGINT NOT NULL)"
                )
                cur.execute("SELECT version FROM schema_migrations")
                applied = {int(row[0]) for row in cur.fetchall()}
                for version, statements in MIGRATIONS:
                    if version in applied:
                        continue
                    for statement in statements:
                        cur.execute(statement)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (%s, %s)",
                        (version, int(time.time())),
                    )
            db.commit()

    def append(self, event: str, payload=None, ts=None):
        entry = {"time": int(ts or time.time()), "event": str(event), "payload": payload or {}}
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO journal(ts,event,payload) VALUES(%s,%s,%s)",
                    (entry["time"], entry["event"], Jsonb(entry["payload"])),
                )
            db.commit()
        return entry

    def recent(self, limit=1000):
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT ts,event,payload FROM journal ORDER BY id DESC LIMIT %s",
                    (max(1, int(limit)),),
                )
                rows = cur.fetchall()
        return [
            {"time": int(ts), "event": event, "payload": payload if isinstance(payload, dict) else json.loads(payload)}
            for ts, event, payload in reversed(rows)
        ]

    def put(self, key: str, value: Any):
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO runtime_state(key,value,updated_at) VALUES(%s,%s,%s) "
                    "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                    (key, Jsonb(value), int(time.time())),
                )
            db.commit()

    def put_if_absent(self, key: str, value: Any) -> bool:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO runtime_state(key,value,updated_at) VALUES(%s,%s,%s) ON CONFLICT(key) DO NOTHING",
                    (key, Jsonb(value), int(time.time())),
                )
                created = cur.rowcount == 1
            db.commit()
        return created

    def compare_and_swap(self, key: str, expected: Any, replacement: Any) -> bool:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "UPDATE runtime_state SET value=%s, updated_at=%s WHERE key=%s AND value=%s::jsonb",
                    (Jsonb(replacement), int(time.time()), key, json.dumps(expected, separators=(",", ":"), default=str)),
                )
                changed = cur.rowcount == 1
            db.commit()
        return changed

    def get(self, key: str, default=None):
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute("SELECT value FROM runtime_state WHERE key=%s", (key,))
                row = cur.fetchone()
        return row[0] if row else default

    def delete(self, key: str):
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute("DELETE FROM runtime_state WHERE key=%s", (key,))
            db.commit()

    def record_order(self, order_key: str, payload: dict[str, Any]) -> None:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO orders(order_key,symbol,side,status,payload,updated_at) VALUES(%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(order_key) DO UPDATE SET status=EXCLUDED.status,payload=EXCLUDED.payload,updated_at=EXCLUDED.updated_at",
                    (
                        order_key,
                        payload.get("symbol"),
                        payload.get("side") or payload.get("signal"),
                        payload.get("status") or payload.get("state"),
                        Jsonb(payload),
                        int(time.time()),
                    ),
                )
            db.commit()

    def record_fill(self, fill_key: str, order_key: str | None, payload: dict[str, Any]) -> None:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO fills(fill_key,order_key,symbol,qty,price,payload,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(fill_key) DO NOTHING",
                    (
                        fill_key,
                        order_key,
                        payload.get("symbol"),
                        payload.get("cumExecQty") or payload.get("qty"),
                        payload.get("avgPrice") or payload.get("price"),
                        Jsonb(payload),
                        int(time.time()),
                    ),
                )
            db.commit()

    def record_risk_snapshot(self, trading_date: str | None, payload: dict[str, Any]) -> None:
        now = int(time.time())
        retention_days = max(1, int(os.environ.get("RISK_SNAPSHOT_RETENTION_DAYS", "30")))
        max_rows = max(100, int(os.environ.get("RISK_SNAPSHOT_MAX_ROWS", "10000")))
        cutoff = now - (retention_days * 86400)
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO risk_snapshots(trading_date,payload,created_at) VALUES(%s,%s,%s)",
                    (trading_date, Jsonb(payload), now),
                )
                cur.execute("DELETE FROM risk_snapshots WHERE created_at < %s", (cutoff,))
                cur.execute(
                    "DELETE FROM risk_snapshots WHERE id IN ("
                    "SELECT id FROM risk_snapshots ORDER BY id DESC OFFSET %s)",
                    (max_rows,),
                )
            db.commit()

    def record_reconciliation(self, status: str, payload: dict[str, Any]) -> None:
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO reconciliation_runs(status,payload,created_at) VALUES(%s,%s,%s)",
                    (status, Jsonb(payload), int(time.time())),
                )
            db.commit()

    def status(self) -> dict[str, Any]:
        try:
            with self.connect() as db:
                with db.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    cur.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations")
                    version = int(cur.fetchone()[0])
            self._last_error = None
            return {
                "ok": True,
                "backend": "postgresql",
                "persistentPathConfigured": True,
                "degraded": False,
                "migrationVersion": version,
                "restartSafe": True,
            }
        except Exception as exc:
            self._last_error = str(exc)
            return {
                "ok": False,
                "backend": "postgresql",
                "persistentPathConfigured": bool(self.database_url),
                "degraded": True,
                "restartSafe": False,
                "error": self._last_error,
            }
