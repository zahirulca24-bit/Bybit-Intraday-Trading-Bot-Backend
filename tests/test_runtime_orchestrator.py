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
                "lastError": None,
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
