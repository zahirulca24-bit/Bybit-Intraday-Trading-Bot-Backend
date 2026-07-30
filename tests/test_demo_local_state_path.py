from __future__ import annotations

import os
from pathlib import Path

from backend import durable_runtime


def test_demo_runtime_configures_writable_local_state_path_when_unset(monkeypatch):
    monkeypatch.delenv("BOT_STATE_DB_PATH", raising=False)

    path = durable_runtime._configure_demo_state_path()

    expected = Path(durable_runtime.__file__).resolve().parent / "data" / "bot_state.sqlite3"
    assert path == str(expected)
    assert os.environ["BOT_STATE_DB_PATH"] == str(expected)


def test_explicit_state_path_is_preserved(monkeypatch, tmp_path):
    explicit = tmp_path / "bot.sqlite3"
    monkeypatch.setenv("BOT_STATE_DB_PATH", str(explicit))

    path = durable_runtime._configure_demo_state_path()

    assert path == str(explicit)
    assert os.environ["BOT_STATE_DB_PATH"] == str(explicit)
