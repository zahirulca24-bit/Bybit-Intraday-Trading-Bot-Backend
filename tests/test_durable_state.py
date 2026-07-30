from pathlib import Path

from backend.state_store import StateStore


def test_journal_survives_new_store_instance(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = StateStore(path)
    first.append("signal_executed", {"signalKey": "BTCUSDT:5:1:Buy"}, ts=123)

    second = StateStore(path)
    assert second.recent() == [
        {
            "time": 123,
            "event": "signal_executed",
            "payload": {"signalKey": "BTCUSDT:5:1:Buy"},
        }
    ]


def test_pending_entry_round_trip_and_clear(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    pending = {"symbol": "BTCUSDT", "orderResult": {"result": {"orderId": "abc"}}}
    store.put("pending_entry", pending)
    assert store.get("pending_entry") == pending
    store.delete("pending_entry")
    assert store.get("pending_entry") is None


def test_risk_state_round_trip(tmp_path):
    store = StateStore(tmp_path / "state.sqlite3")
    state = {"tradingDateKey": "2026-07-26", "lastTradeAt": 99, "lastSignal": "Buy"}
    store.put("risk_state", state)
    assert store.get("risk_state") == state


def test_status_marks_unconfigured_local_path_degraded(tmp_path, monkeypatch):
    monkeypatch.delenv("BOT_STATE_DB_PATH", raising=False)
    store = StateStore(tmp_path / "state.sqlite3")
    status = store.status()
    assert status["ok"] is True
    assert status["persistentPathConfigured"] is False
    assert status["degraded"] is True


def test_sqlite_side_files_are_not_required_for_readback(tmp_path):
    path = Path(tmp_path) / "state.sqlite3"
    StateStore(path).append("event", {"ok": True})
    assert StateStore(path).recent(1)[0]["payload"] == {"ok": True}
