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


def reset_state():
    orchestrator.stop(timeout=0)
    with orchestrator._LOCK:
        orchestrator._STATE.update({
            "status": "stopped",
            "startedAt": 0,
            "stoppedAt": 0,
            "lastLoopAt": 0,
            "lastSymbolRunAt": 0,
            "lastSetupRunAt": 0,
            "nextSymbolRunAt": 0,
            "nextSetupRunAt": 0,
            "symbolRuns": 0,
            "setupRuns": 0,
            "lastError": None,
        })


def test_due_workers_run_immediately(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    state = orchestrator.run_due_once(object(), symbol, setup, now=1000)

    assert symbol.calls == [1000]
    assert setup.calls == [1000]
    assert state["nextSymbolRunAt"] == 1300
    assert state["nextSetupRunAt"] == 1300
    assert state["symbolRuns"] == 1
    assert state["setupRuns"] == 1


def test_workers_do_not_repeat_before_interval(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    orchestrator.run_due_once(object(), symbol, setup, now=1200)

    assert symbol.calls == [1000]
    assert setup.calls == [1000]


def test_workers_run_again_when_due(monkeypatch):
    reset_state()
    monkeypatch.setenv("SYMBOL_WORKER_INTERVAL_SECONDS", "300")
    monkeypatch.setenv("SETUP_WORKER_INTERVAL_SECONDS", "300")
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    orchestrator.run_due_once(object(), symbol, setup, now=1000)
    state = orchestrator.run_due_once(object(), symbol, setup, now=1300)

    assert symbol.calls == [1000, 1300]
    assert setup.calls == [1000, 1300]
    assert state["symbolRuns"] == 2
    assert state["setupRuns"] == 2


def test_orchestrator_never_pops_or_executes_confirmed_candidates():
    reset_state()
    symbol = StubSymbolWorker()
    setup = StubSetupWorker()

    orchestrator.run_due_once(object(), symbol, setup, now=2000)

    assert not hasattr(orchestrator, "execute")
    assert not hasattr(orchestrator, "pop_confirmed")
