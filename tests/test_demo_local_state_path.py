from __future__ import annotations

import os

from backend import durable_runtime
from backend.postgres_state_store import PostgresStateStore


def test_demo_runtime_does_not_create_local_state_fallback_when_unset(monkeypatch):
    monkeypatch.delenv("BOT_STATE_DB_PATH", raising=False)

    store = durable_runtime._build_store()

    assert "BOT_STATE_DB_PATH" not in os.environ
    assert isinstance(store, (PostgresStateStore, durable_runtime.UnavailablePersistentStore))
    assert store.status()["backend"] == "postgresql"


def test_legacy_local_state_path_is_ignored_but_not_mutated(monkeypatch, tmp_path):
    explicit = tmp_path / "bot.sqlite3"
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(explicit))

    store = durable_runtime._build_store()

    assert os.environ["BOT_STATE_DB_PATH"] == str(explicit)
    assert isinstance(store, (PostgresStateStore, durable_runtime.UnavailablePersistentStore))
    assert store.status()["backend"] == "postgresql"
