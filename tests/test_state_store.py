from __future__ import annotations

from backend.state_store import StateStore


def test_put_if_absent_is_atomic_for_existing_key(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BOT_STATE_DB_PATH",
        str(tmp_path / "state.sqlite3"),
    )
    store = StateStore()

    assert store.put_if_absent("claim", {"state": "CLAIMED"}) is True
    assert store.put_if_absent("claim", {"state": "OTHER"}) is False
    assert store.get("claim") == {"state": "CLAIMED"}


def test_status_requires_explicit_persistent_path(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_STATE_DB_PATH", raising=False)
    store = StateStore(path=tmp_path / "state.sqlite3")

    status = store.status()

    assert status["ok"] is True
    assert status["persistentPathConfigured"] is False
    assert status["degraded"] is True
