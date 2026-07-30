from __future__ import annotations

from copy import deepcopy

import pytest

from backend import replay_session_repository as repository
from backend import replay_sessions
from backend.replay_safety import policy_status
from backend.replay_storage import ReplayStorageValidationError

START = 1_800_000_000_000
END = START + 600_000


def _request(expected_candles=3, start=START, end=END):
    return {
        "symbol": "BTCUSDT",
        "timeframe": "5",
        "intervalMs": 300_000,
        "startTime": start,
        "endTime": end,
        "expectedCandles": expected_candles,
        "force": False,
    }


def _coverage(
    complete: bool,
    source: str = "postgresql_cache",
    *,
    expected_candles: int = 3,
    start: int = START,
    end: int = END,
):
    cached = expected_candles if complete else max(0, expected_candles - 2)
    return {
        "ok": True,
        "source": source,
        "request": _request(expected_candles, start, end),
        "range": {
            "complete": complete,
            "expectedCandles": expected_candles,
            "cachedCandles": cached,
            "missingCandles": 0 if complete else expected_candles - cached,
            "startTime": start,
            "endTime": end,
        },
        "coverage": {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "count": cached,
        },
    }


class FakeCollector:
    def __init__(self, *, complete: bool = True, expected_candles: int = 3):
        self.complete = complete
        self.expected_candles = expected_candles
        self.coverage_calls = []
        self.sync_calls = []

    def coverage(self, payload):
        self.coverage_calls.append(dict(payload))
        return _coverage(
            self.complete,
            expected_candles=self.expected_candles,
            start=int(payload.get("startTime") or START),
            end=int(payload.get("endTime") or END),
        )

    def sync(self, payload):
        self.sync_calls.append(dict(payload))
        self.complete = True
        return _coverage(
            True,
            source="bybit_main_public_kline",
            expected_candles=self.expected_candles,
            start=int(payload.get("startTime") or START),
            end=int(payload.get("endTime") or END),
        )


def _persisted_from(payload):
    return {
        "sessionId": payload["session_id"],
        "symbol": payload["symbol"],
        "timeframe": payload["timeframe"],
        "status": payload["status"],
        "startTime": payload["start_time"],
        "endTime": payload["end_time"],
        "cursorTime": payload["cursor_time"],
        "initialBalance": str(payload["initial_balance"]),
        "balance": str(payload["balance"]),
        "equity": str(payload["equity"]),
        "strategyMode": payload["strategy_mode"],
        "config": deepcopy(payload["config"]),
        "summary": {},
        "createdAt": 1,
        "updatedAt": 1,
    }


def _payload(**patch):
    result = {
        "sessionId": "replay_test_0001",
        "symbol": "BTCUSDT",
        "timeframe": "5",
        "startTime": START,
        "endTime": END,
        "initialBalance": "1000",
        "strategyMode": "balanced",
        "config": {"feeBps": 5},
    }
    result.update(patch)
    return result


def _service(monkeypatch, collector=None):
    store = object()
    monkeypatch.setattr(repository, "require_store", lambda candidate: candidate)
    monkeypatch.setattr(repository, "get_session", lambda store, session_id: None)
    return replay_sessions.ReplaySessionService(
        store,
        collector or FakeCollector(),
        now_seconds=lambda: 1_800_000_000,
        token_factory=lambda size: "a" * (size * 2),
    )


def test_start_creates_ready_simulation_only_session_from_complete_cache(monkeypatch):
    captured = {}

    def create(store, payload, event):
        captured["payload"] = deepcopy(payload)
        captured["event"] = deepcopy(event)
        return {"created": True, "session": _persisted_from(payload)}

    monkeypatch.setattr(repository, "create_session", create)
    result = _service(monkeypatch).start(_payload())

    assert result["created"] is True
    assert result["data"]["syncPerformed"] is False
    assert result["session"]["status"] == "READY"
    config = captured["payload"]["config"]
    assert config["runtimeMode"] == "historical_replay"
    assert config["executionMode"] == "simulated_only"
    assert config["externalExecutionAllowed"] is False
    assert config["requestedStartTime"] == START
    assert config["requestedEndTime"] == END
    assert captured["event"]["executionMode"] == "simulated_only"


def test_start_auto_syncs_incomplete_range_before_persisting(monkeypatch):
    collector = FakeCollector(complete=False)
    monkeypatch.setattr(
        repository,
        "create_session",
        lambda store, payload, event: {"created": True, "session": _persisted_from(payload)},
    )
    result = _service(monkeypatch, collector).start(_payload())
    assert len(collector.sync_calls) == 1
    assert result["data"]["syncPerformed"] is True
    assert result["data"]["source"] == "bybit_main_public_kline"


def test_start_blocks_incomplete_range_when_auto_sync_is_disabled(monkeypatch):
    with pytest.raises(replay_sessions.ReplaySessionDataIncompleteError, match="sync data"):
        _service(monkeypatch, FakeCollector(complete=False)).start(_payload(autoSync=False))


def test_force_data_sync_refreshes_complete_cache(monkeypatch):
    collector = FakeCollector()
    monkeypatch.setattr(
        repository,
        "create_session",
        lambda store, payload, event: {"created": True, "session": _persisted_from(payload)},
    )
    result = _service(monkeypatch, collector).start(_payload(forceDataSync=True))
    assert collector.sync_calls[0]["force"] is True
    assert result["data"]["syncPerformed"] is True


def test_force_sync_requires_auto_sync(monkeypatch):
    with pytest.raises(replay_sessions.ReplaySessionValidationError, match="requires autoSync"):
        _service(monkeypatch).start(_payload(forceDataSync=True, autoSync=False))


def test_single_candle_session_is_rejected_before_persistence(monkeypatch):
    called = {"create": False}

    def create(*args, **kwargs):
        called["create"] = True
        raise AssertionError("single candle session must not persist")

    monkeypatch.setattr(repository, "create_session", create)
    with pytest.raises(replay_sessions.ReplaySessionValidationError, match="at least 2"):
        _service(monkeypatch, FakeCollector(expected_candles=1)).start(_payload(endTime=START))
    assert called["create"] is False


def test_existing_id_is_resolved_before_time_relative_coverage(monkeypatch):
    requested_future_end = END + 900_000
    existing = _persisted_from(
        {
            "session_id": "replay_test_0001",
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "status": "READY",
            "start_time": START,
            "end_time": END,
            "cursor_time": None,
            "initial_balance": "1000",
            "balance": "1000",
            "equity": "1000",
            "strategy_mode": "balanced",
            "config": {
                "feeBps": 5,
                "runtimeMode": "historical_replay",
                "executionMode": "simulated_only",
                "externalExecutionAllowed": False,
                "dataSource": "bybit_main_public_kline",
                "intervalMs": 300_000,
                "requestedStartTime": START,
                "requestedEndTime": requested_future_end,
                "autoSync": True,
            },
        }
    )
    collector = FakeCollector()
    service = _service(monkeypatch, collector)
    monkeypatch.setattr(repository, "get_session", lambda store, sid: existing)
    result = service.start(_payload(endTime=requested_future_end))
    assert result["created"] is False
    assert collector.coverage_calls[0]["endTime"] == END


def test_existing_id_with_different_settings_is_rejected_before_data_work(monkeypatch):
    existing = _persisted_from(
        {
            "session_id": "replay_test_0001",
            "symbol": "ETHUSDT",
            "timeframe": "5",
            "status": "READY",
            "start_time": START,
            "end_time": END,
            "cursor_time": None,
            "initial_balance": "1000",
            "balance": "1000",
            "equity": "1000",
            "strategy_mode": "balanced",
            "config": {
                "runtimeMode": "historical_replay",
                "executionMode": "simulated_only",
                "externalExecutionAllowed": False,
                "dataSource": "bybit_main_public_kline",
                "intervalMs": 300_000,
                "requestedStartTime": START,
                "requestedEndTime": END,
            },
        }
    )
    collector = FakeCollector()
    service = _service(monkeypatch, collector)
    monkeypatch.setattr(repository, "get_session", lambda store, sid: existing)
    with pytest.raises(replay_sessions.ReplaySessionConflictError, match="different immutable"):
        service.start(_payload())
    assert collector.coverage_calls == []


def test_generated_session_id_and_final_config_limit(monkeypatch):
    monkeypatch.setattr(
        repository,
        "create_session",
        lambda store, payload, event: {"created": True, "session": _persisted_from(payload)},
    )
    payload = _payload()
    payload.pop("sessionId")
    result = _service(monkeypatch).start(payload)
    assert result["session"]["sessionId"] == "replay_1800000000_aaaaaaaaaaaaaaaa"
    with pytest.raises(replay_sessions.ReplaySessionValidationError, match="config exceeds"):
        _service(monkeypatch).start(_payload(config={"blob": "x" * 16_250}))


def test_list_validation_and_status_normalization(monkeypatch):
    captured = {}

    def list_sessions(store, *, limit, status):
        captured.update(limit=limit, status=status)
        return []

    monkeypatch.setattr(repository, "list_sessions", list_sessions)
    result = _service(monkeypatch).list(limit="25", status="ready")
    assert result["count"] == 0
    assert captured == {"limit": 25, "status": "READY"}
    for invalid in (0, 101, "bad"):
        with pytest.raises(replay_sessions.ReplaySessionValidationError):
            _service(monkeypatch).list(limit=invalid)


def test_get_not_found_and_reset_conflict(monkeypatch):
    service = _service(monkeypatch)
    with pytest.raises(replay_sessions.ReplaySessionNotFoundError):
        service.get("replay_test_0001")

    def reset(store, session_id):
        raise ReplayStorageValidationError("A running replay session must be paused before reset.")

    monkeypatch.setattr(repository, "reset_session", reset)
    with pytest.raises(replay_sessions.ReplaySessionConflictError):
        service.reset("replay_test_0001")


def test_get_returns_session_and_exact_cache_coverage(monkeypatch):
    persisted = _persisted_from(
        {
            "session_id": "replay_test_0001",
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "status": "READY",
            "start_time": START,
            "end_time": END,
            "cursor_time": None,
            "initial_balance": "1000",
            "balance": "1000",
            "equity": "1000",
            "strategy_mode": "balanced",
            "config": {},
        }
    )
    service = _service(monkeypatch)
    monkeypatch.setattr(repository, "get_session", lambda store, sid: persisted)
    result = service.get("replay_test_0001")
    assert result["session"]["sessionId"] == "replay_test_0001"
    assert result["data"]["range"]["complete"] is True


def test_route_contract_and_policy_expose_session_and_step_capabilities():
    assert replay_sessions.is_post_path("/api/replay/start") is True
    assert replay_sessions.is_post_path("/api/replay/sessions/replay_test_0001/reset") is True
    assert replay_sessions.is_post_path("/api/replay/step") is False
    status = policy_status()
    assert status["sessionApiCapabilities"] == ["create", "list", "get", "reset"]
    assert status["stepEngineImplemented"] is True
    assert status["strategyReplayImplemented"] is True
    assert status["riskReplayImplemented"] is True
    assert status["externalExecutionAllowed"] is False
