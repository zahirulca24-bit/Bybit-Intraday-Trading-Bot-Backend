from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import guarded_server as guarded


def setup_function():
    guarded._TICK_CONTEXT.generation = None
    with guarded.core.BOT_LOCK:
        guarded.core.BOT_STATE["enabled"] = True


def test_duplicate_tick_is_skipped(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow_tick():
        calls.append("tick")
        entered.set()
        assert release.wait(timeout=2)
        return {"enabled": True}

    monkeypatch.setattr(guarded, "_ORIGINAL_BOT_TICK", slow_tick)
    first_result = {}

    def run_first():
        first_result.update(guarded.guarded_bot_tick())

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=2)

    duplicate = guarded.guarded_bot_tick()
    assert "duplicate tick skipped" in duplicate["lastReason"].lower()
    assert calls == ["tick"]

    release.set()
    thread.join(timeout=2)
    assert first_result["enabled"] is True


def test_generation_change_invalidates_inflight_execution():
    guarded._TICK_CONTEXT.generation = guarded.current_generation()
    assert guarded.execution_is_current() is True

    guarded.advance_generation()
    assert guarded.execution_is_current() is False


def test_stopped_runtime_blocks_execution():
    guarded._TICK_CONTEXT.generation = guarded.current_generation()
    with guarded.core.BOT_LOCK:
        guarded.core.BOT_STATE["enabled"] = False
    assert guarded.execution_is_current() is False


def test_install_guards_is_idempotent():
    guarded.install_guards()
    engine = guarded.core.get_bot_engine()
    first_execute = engine.execute
    guarded.install_guards()
    assert engine.execute is first_execute
