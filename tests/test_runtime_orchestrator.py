from backend import runtime_orchestrator as orchestrator


class StubSymbolWorker:
    def __init__(self):
        self.calls = []

    def run_batch(self, core, now=None):
        self.calls.append(now)
        return {"status": "ok", "rows": []}


class StubSetupWorker:
    def __init__(self):
        self.calls = []

    def run_batch(self, core, symbol_worker, now=None):
        self.calls.append(now)
        return {"status": "ok", "rows": []}


class StubExecutionHandoff:
    def __init__(self):
        self.calls = []

    def run_once(self, core, setup_worker, now=None):
        self.calls.append(now)
        return {"status": "executed"}


def reset_state():
    orchestrator.stop(timeout=0)
    with orchestrator._LOCK:
        orchestrator._STATE.update(
            {
                "status": "stopped",
                "startedAt": 0,
                "stoppedAt": 0,
                "lastLoopAt": 0,
                "lastSymbolRunAt": 0,
                "lastSetupRunAt": 0,
                "lastEntryRunAt": 0,
                "lastRiskRunAt": 0,
                "lastSizingRunAt": 0,
                "lastCommandPublishAt": 0,
                "lastExecutionRunAt": 0,
                "nextSymbolRunAt": 0,
                "nextSetupRunAt": 0,
                "nextExecutionRunAt": 0,
                "symbolRuns": 0,
                "setupRuns": 0,
                "entryRuns": 0,
                "riskRuns": 0,
                "sizingRuns": 0,
                "commandPublishRuns": 0,
                "executionRuns": 0,
                "legacySetupWorkerDisabled": True,
                "legacyPythonExecutionDisabled": True,
                "lastErrorCode": None,
                "lastErrorMessage": None,
                "backoffActive": False,
                "consecutiveFailureCount": 0,
                "currentRetryDelaySeconds": 0,
                "nextRetryAt": 0,
                "lastFailureAt": 0,
                "lastFailureCategory": None,
            }
        )


def install_stage_spies(monkeypatch):
    calls = {
        "classification": [],
        "entry": [],
        "risk": [],
        "sizing": [],
        "outbox": [],
    }

    def classification(core, now):
        calls["classification"].append(now)
        return {"status": "ready"}

    def entry(core, now):
        calls["entry"].append(now)
        return {"status": "ready"}

    def risk(core, now):
        calls["risk"].append(now)
        return {"status": "ready"}

    def sizing(core, now):
        calls["sizing"].append(now)
        return {"status": "ready"}

    def outbox(core, now):
        calls["outbox"].append(now)
        return {"status": "ready"}

    monkeypatch.setattr(
        orchestrator,
        "_run_fifteen_minute_strategy_classifier",
        classification,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_five_minute_entry_confirmation",
        entry,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_authoritative_entry_risk",
        risk,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_position_sizing_margin",
        sizing,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_execution_command_outbox",
        outbox,
    )
    return calls


def test_due_stages_run_immediately_without_legacy_workers(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    calls = install_stage_spies(monkeypatch)
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()
    execution = StubExecutionHandoff()

    state = orchestrator.run_due_once(
        object(), symbol, setup, execution, now=1000
    )

    assert symbol.calls == [1000]
    assert calls["classification"] == [1000]
    assert calls["entry"] == [1000]
    assert calls["risk"] == [1000]
    assert calls["sizing"] == [1000]
    assert calls["outbox"] == [1000]
    assert setup.calls == []
    assert execution.calls == []
    assert state["nextSymbolRunAt"] == 1300
    assert state["nextSetupRunAt"] == 1300
    assert state["nextExecutionRunAt"] == 0
    assert state["symbolRuns"] == 1
    assert state["setupRuns"] == 1
    assert state["entryRuns"] == 1
    assert state["riskRuns"] == 1
    assert state["sizingRuns"] == 1
    assert state["commandPublishRuns"] == 1
    assert state["lastCommandPublishAt"] == 1000
    assert state["executionRuns"] == 0
    assert state["legacySetupWorkerDisabled"] is True
    assert state["legacyPythonExecutionDisabled"] is True


def test_stages_do_not_repeat_before_interval(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    calls = install_stage_spies(monkeypatch)
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    orchestrator.run_due_once(object(), symbol, setup, now=1200)

    assert symbol.calls == [1000]
    assert calls["classification"] == [1000]
    assert calls["entry"] == [1000]
    assert calls["risk"] == [1000]
    assert calls["sizing"] == [1000]
    assert calls["outbox"] == [1000]
    assert setup.calls == []


def test_stages_run_again_when_due(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    calls = install_stage_spies(monkeypatch)
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    state = orchestrator.run_due_once(object(), symbol, setup, now=1300)

    assert symbol.calls == [1000, 1300]
    assert calls["classification"] == [1000, 1300]
    assert calls["entry"] == [1000, 1300]
    assert calls["risk"] == [1000, 1300]
    assert calls["sizing"] == [1000, 1300]
    assert calls["outbox"] == [1000, 1300]
    assert setup.calls == []
    assert state["symbolRuns"] == 2
    assert state["setupRuns"] == 2
    assert state["entryRuns"] == 2
    assert state["riskRuns"] == 2
    assert state["sizingRuns"] == 2
    assert state["commandPublishRuns"] == 2


def test_orchestrator_never_runs_legacy_execution_handoff(monkeypatch):
    reset_state()
    install_stage_spies(monkeypatch)
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()
    execution = StubExecutionHandoff()

    state = orchestrator.run_due_once(
        object(), symbol, setup, execution, now=2000
    )

    assert execution.calls == []
    assert state["executionRuns"] == 0
    assert state["settings"]["legacyPythonExecutionEnabled"] is False


def test_first_failure_schedules_five_second_retry(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    def fail(*_args, **_kwargs):
        raise RuntimeError("database connection refused")

    monkeypatch.setattr(orchestrator, "_run_fifteen_minute_strategy_classifier", fail)
    state = orchestrator.run_due_once(object(), symbol, setup, now=1000)

    assert state["status"] == "backoff"
    assert state["backoffActive"] is True
    assert state["consecutiveFailureCount"] == 1
    assert state["currentRetryDelaySeconds"] == 5
    assert state["nextRetryAt"] == 1005
    assert state["lastFailureAt"] == 1000
    assert state["lastFailureCategory"] == "database"
    assert state["lastErrorCode"] == "WORKER_DATABASE_FAILURE"
    assert state["lastErrorMessage"] == "Database operation failed."


def test_repeated_failures_follow_exact_bounded_sequence(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()
    times = [1000, 1005, 1015, 1035, 1075, 1155, 1315, 1615]
    expected_delays = [5, 10, 20, 40, 80, 160, 300, 300]

    def fail(*_args, **_kwargs):
        raise RuntimeError("Bybit API timeout")

    monkeypatch.setattr(orchestrator, "_run_fifteen_minute_strategy_classifier", fail)

    for index, timestamp in enumerate(times):
        state = orchestrator.run_due_once(object(), symbol, setup, now=timestamp)
        assert state["consecutiveFailureCount"] == index + 1
        assert state["currentRetryDelaySeconds"] == expected_delays[index]
        assert state["nextRetryAt"] == timestamp + expected_delays[index]
        assert state["lastFailureCategory"] == "exchange/API"
        assert state["lastErrorCode"] == "WORKER_EXCHANGE_API_FAILURE"
        assert (
            state["lastErrorMessage"]
            == "Exchange or external API operation failed."
        )


def test_retry_delay_never_exceeds_three_hundred_seconds(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    def fail(*_args, **_kwargs):
        raise RuntimeError("market data candles unavailable")

    monkeypatch.setattr(orchestrator, "_run_fifteen_minute_strategy_classifier", fail)

    timestamp = 1000
    state = {}
    for _ in range(10):
        state = orchestrator.run_due_once(object(), symbol, setup, now=timestamp)
        timestamp = state["nextRetryAt"]

    assert state["currentRetryDelaySeconds"] == 300
    assert state["nextRetryAt"] == timestamp
    assert state["lastFailureCategory"] == "market data"


def test_pipeline_does_not_rerun_before_next_retry_at(monkeypatch):
    reset_state()
    calls = install_stage_spies(monkeypatch)
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    def fail_once(core, now):
        calls["classification"].append(now)
        raise ValueError("validation failed for setup payload")

    monkeypatch.setattr(
        orchestrator,
        "_run_fifteen_minute_strategy_classifier",
        fail_once,
    )

    failed = orchestrator.run_due_once(object(), symbol, setup, now=1000)
    paused = orchestrator.run_due_once(object(), symbol, setup, now=1004)

    assert symbol.calls == [1000]
    assert calls["classification"] == [1000]
    assert paused["status"] == "backoff"
    assert paused["nextRetryAt"] == 1005
    assert paused["consecutiveFailureCount"] == 1
    assert failed["lastFailureCategory"] == "validation"
    assert failed["lastErrorCode"] == "WORKER_VALIDATION_FAILURE"
    assert failed["lastErrorMessage"] == "Runtime validation failed."


def test_successful_run_resets_failure_state(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()
    attempts = {"count": 0}

    def fail_then_succeed(core, now):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("database unavailable")
        return {"status": "ready"}

    monkeypatch.setattr(
        orchestrator,
        "_run_fifteen_minute_strategy_classifier",
        fail_then_succeed,
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_five_minute_entry_confirmation",
        lambda core, now: {"status": "ready"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_authoritative_entry_risk",
        lambda core, now: {"status": "ready"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_position_sizing_margin",
        lambda core, now: {"status": "ready"},
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_execution_command_outbox",
        lambda core, now: {"status": "ready"},
    )

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    state = orchestrator.run_due_once(object(), symbol, setup, now=1005)

    assert state["status"] == "running"
    assert state["backoffActive"] is False
    assert state["consecutiveFailureCount"] == 0
    assert state["currentRetryDelaySeconds"] == 0
    assert state["nextRetryAt"] == 0
    assert state["lastFailureAt"] == 0
    assert state["lastFailureCategory"] is None
    assert state["lastErrorCode"] is None
    assert state["lastErrorMessage"] is None


def test_runtime_snapshot_reports_backoff_state(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    def fail(*_args, **_kwargs):
        raise RuntimeError("database transaction aborted")

    monkeypatch.setattr(orchestrator, "_run_fifteen_minute_strategy_classifier", fail)
    monkeypatch.setattr(orchestrator.time, "time", lambda: 1002)

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    snapshot = orchestrator.snapshot()

    assert snapshot["status"] == "backoff"
    assert snapshot["backoffActive"] is True
    assert snapshot["currentRetryDelaySeconds"] == 5
    assert snapshot["nextRetryAt"] == 1005
    assert snapshot["lastFailureCategory"] == "database"
    assert snapshot["lastErrorCode"] == "WORKER_DATABASE_FAILURE"
    assert snapshot["lastErrorMessage"] == "Database operation failed."
    assert snapshot["legacySetupWorkerDisabled"] is True
    assert snapshot["legacyPythonExecutionDisabled"] is True


def test_runtime_snapshot_never_exposes_secret_bearing_exception(monkeypatch):
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()
    raw_secret = (
        "Authorization: Bearer secret-token password=my-password "
        "postgresql://user:pass@host/database api_key=secret-key "
        "token=secret-token Cookie: session=secret-value"
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(raw_secret)

    monkeypatch.setattr(orchestrator, "_run_fifteen_minute_strategy_classifier", fail)
    snapshot = orchestrator.run_due_once(object(), symbol, setup, now=1000)

    serialized = repr(snapshot)
    assert "secret-token" not in serialized
    assert "my-password" not in serialized
    assert "user:pass@host" not in serialized
    assert "secret-key" not in serialized
    assert "session=secret-value" not in serialized
    assert "Authorization: Bearer" not in serialized
    assert snapshot["lastErrorCode"] == "WORKER_DATABASE_FAILURE"
    assert snapshot["lastErrorMessage"] == "Database operation failed."
    assert snapshot["lastFailureCategory"] == "database"
    assert "lastError" not in snapshot


def test_exception_summary_redacts_sensitive_values():
    message = (
        "Authorization: Bearer secret-token "
        "password=my-password "
        "postgresql://user:pass@host/database "
        "api_key=secret-key "
        "token=secret-token "
        "Cookie: session=secret-value"
    )

    sanitized = orchestrator._sanitize_exception_summary(RuntimeError(message))

    assert "secret-token" not in sanitized
    assert "my-password" not in sanitized
    assert "user:pass@host" not in sanitized
    assert "secret-key" not in sanitized
    assert "session=secret-value" not in sanitized
    assert "Bearer [REDACTED]" in sanitized
    assert "password=[REDACTED]" in sanitized
    assert "postgresql://[REDACTED]:[REDACTED]@host/database" in sanitized
    assert "api_key=[REDACTED]" in sanitized
    assert "token=[REDACTED]" in sanitized
    assert "Cookie: [REDACTED]" in sanitized
