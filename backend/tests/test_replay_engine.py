from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend import replay_engine
from backend import replay_step_repository as repository
from backend.replay_safety import policy_status, validate_replay_request

SESSION_ID = "replay_test_0001"
REQUEST_ID = "step_request_0001"
CURSOR = 1_800_000_000_000


def _payload(**patch):
    result = {
        "sessionId": SESSION_ID,
        "requestId": REQUEST_ID,
        "steps": 1,
        "expectedCursorTime": None,
    }
    result.update(patch)
    return result


def test_normalize_step_request_requires_idempotency_and_cursor_contract():
    request = replay_engine.normalize_step_request(_payload(steps="2"))
    assert request == {
        "sessionId": SESSION_ID,
        "requestId": REQUEST_ID,
        "steps": 2,
        "expectedCursorTime": None,
    }
    alias = replay_engine.normalize_step_request(
        {
            "session_id": SESSION_ID,
            "idempotencyKey": REQUEST_ID,
            "count": 3,
            "expected_cursor_time": CURSOR,
        }
    )
    assert alias["steps"] == 3
    assert alias["expectedCursorTime"] == CURSOR


@pytest.mark.parametrize(
    "payload,match",
    [
        ({"requestId": REQUEST_ID, "expectedCursorTime": None}, "sessionId"),
        ({"sessionId": SESSION_ID, "expectedCursorTime": None}, "requestId"),
        ({"sessionId": SESSION_ID, "requestId": REQUEST_ID}, "expectedCursorTime"),
        (_payload(steps=0), "between 1"),
        (_payload(steps=101), "between 1"),
        (_payload(expectedCursorTime=123), "Unix timestamp"),
    ],
)
def test_invalid_step_requests_fail_closed(payload, match):
    with pytest.raises(replay_engine.ReplayEngineValidationError, match=match):
        replay_engine.normalize_step_request(payload)


def test_replay_safety_normalization_cannot_enable_external_execution():
    payload = validate_replay_request({**_payload(), "executionMode": "simulation"})
    request = replay_engine.normalize_step_request(payload)
    assert request["sessionId"] == SESSION_ID
    assert payload["executionMode"] == "simulated_only"
    assert payload["externalExecutionAllowed"] is False


def test_engine_delegates_only_normalized_step_request(monkeypatch):
    captured = {}

    def advance(store, request):
        captured["store"] = store
        captured["request"] = dict(request)
        return {"ok": True, "cursorTime": CURSOR}

    monkeypatch.setattr(repository, "advance_session", advance)
    store = object()
    result = replay_engine.CandleReplayEngine(store).step(_payload(steps=2))
    assert result["ok"] is True
    assert captured["store"] is store
    assert captured["request"]["steps"] == 2
    assert set(captured["request"]) == {
        "sessionId",
        "requestId",
        "steps",
        "expectedCursorTime",
    }


def test_repository_errors_are_mapped_to_api_domain(monkeypatch):
    engine = replay_engine.CandleReplayEngine(object())
    monkeypatch.setattr(
        repository,
        "advance_session",
        lambda *args: (_ for _ in ()).throw(repository.ReplayStepNotFoundError("missing")),
    )
    with pytest.raises(replay_engine.ReplayEngineNotFoundError, match="missing"):
        engine.step(_payload())

    monkeypatch.setattr(
        repository,
        "advance_session",
        lambda *args: (_ for _ in ()).throw(
            repository.ReplayStepConflictError("stale", {"actualCursorTime": CURSOR})
        ),
    )
    with pytest.raises(replay_engine.ReplayEngineConflictError) as conflict:
        engine.step(_payload())
    assert conflict.value.details["actualCursorTime"] == CURSOR

    monkeypatch.setattr(
        repository,
        "advance_session",
        lambda *args: (_ for _ in ()).throw(
            repository.ReplayStepDataIncompleteError("gap", {"missingOpenTimes": [CURSOR]})
        ),
    )
    with pytest.raises(replay_engine.ReplayEngineDataIncompleteError) as gap:
        engine.step(_payload())
    assert gap.value.details["missingOpenTimes"] == [CURSOR]


def test_install_decorates_runtime_session_responses_truthfully():
    class SessionService:
        def start(self, payload):
            return {"ok": True, "stepEngineImplemented": False}

        def get(self, session_id):
            return {"ok": True, "stepEngineImplemented": False}

        def reset(self, session_id):
            return {"ok": True, "stepEngineImplemented": False}

        def _existing_response(self, session, *, auto_sync):
            return {"ok": True, "stepEngineImplemented": False}

    service = SessionService()
    core = SimpleNamespace(_durable_state_store=object(), _replay_session_service=service)
    engine = replay_engine.install(core)
    assert isinstance(engine, replay_engine.CandleReplayEngine)
    assert service.start({})["stepEngineImplemented"] is True
    assert service.get(SESSION_ID)["strategyReplayImplemented"] is True
    assert service.get(SESSION_ID)["riskReplayImplemented"] is True
    assert service.reset(SESSION_ID)["simulatedExecutionImplemented"] is True
    assert replay_engine.install(core) is engine


def test_step_route_and_policy_contract_exposes_simulated_execution():
    assert replay_engine.is_post_path("/api/replay/step") is True
    assert replay_engine.is_post_path("/api/replay/start") is False
    status = policy_status()
    assert status["stepEngineImplemented"] is True
    assert "idempotent_request" in status["stepApiCapabilities"]
    assert "historical_strategy" in status["stepApiCapabilities"]
    assert "simulated_fills" in status["stepApiCapabilities"]
    assert status["strategyReplayImplemented"] is True
    assert status["riskReplayImplemented"] is True
    assert status["simulatedExecutionImplemented"] is True
    assert "durable_idempotency" in status["simulatedExecutionCapabilities"]
    assert status["externalExecutionAllowed"] is False
