"""SQLite storage for runtime evidence and recovery state."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, path=None):
        default = Path(__file__).resolve().parent / "data" / "bot_state.sqlite3"
        self.path = Path(path or os.environ.get("BOT_STATE_DB_PATH") or default)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS journal("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "ts INTEGER NOT NULL, "
                "event TEXT NOT NULL, "
                "payload TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS state("
                "key TEXT PRIMARY KEY, "
                "value TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def append(self, event: str, payload=None, ts=None):
        entry = {
            "time": int(ts or time.time()),
            "event": str(event),
            "payload": payload or {},
        }
        body = json.dumps(entry["payload"], separators=(",", ":"), default=str)
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO journal(ts,event,payload) VALUES(?,?,?)",
                (entry["time"], entry["event"], body),
            )
        return entry

    def recent(self, limit=1000):
        with self.lock, self.connect() as db:
            rows = db.execute(
                "SELECT ts,event,payload FROM journal ORDER BY id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        result = []
        for ts, event, body in reversed(rows):
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {"corruptPayload": True}
            result.append({"time": int(ts), "event": event, "payload": payload})
        return result

    def put(self, key: str, value: Any):
        body = json.dumps(value, separators=(",", ":"), default=str)
        with self.lock, self.connect() as db:
            db.execute(
                "INSERT INTO state(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, body, int(time.time())),
            )

    def put_if_absent(self, key: str, value: Any) -> bool:
        """Atomically create one state record only when the key is absent."""
        body = json.dumps(value, separators=(",", ":"), default=str)
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO state(key,value,updated_at) VALUES(?,?,?)",
                (key, body, int(time.time())),
            )
            return cursor.rowcount == 1

    def compare_and_swap(self, key: str, expected: Any, replacement: Any) -> bool:
        """Atomically replace a state row only when its full JSON value still matches."""
        expected_body = json.dumps(expected, separators=(",", ":"), default=str)
        replacement_body = json.dumps(replacement, separators=(",", ":"), default=str)
        with self.lock, self.connect() as db:
            cursor = db.execute(
                "UPDATE state SET value=?, updated_at=? WHERE key=? AND value=?",
                (replacement_body, int(time.time()), key, expected_body),
            )
            return cursor.rowcount == 1

    def get(self, key: str, default=None):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def delete(self, key: str):
        with self.lock, self.connect() as db:
            db.execute("DELETE FROM state WHERE key=?", (key,))

    def status(self):
        configured = bool(os.environ.get("BOT_STATE_DB_PATH"))
        return {
            "ok": self.path.exists(),
            "path": str(self.path),
            "persistentPathConfigured": configured,
            "degraded": not configured,
        }
