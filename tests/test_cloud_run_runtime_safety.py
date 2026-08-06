from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from backend import cloud_run_server, runtime_instance_guard


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=None):
        if "pg_try_advisory_lock" in query:
            self.row = (self.connection.granted,)
        elif "pg_advisory_unlock" in query:
            self.connection.unlocked = True
            self.row = (True,)
        else:
            if self.connection.fail_health:
                raise RuntimeError("connection lost")
            self.row = (1,)

    def fetchone(self):
        return self.row


class Connection:
    def __init__(self, granted=True):
        self.granted = granted
        self.fail_health = False
        self.unlocked = False
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return Cursor(self)

    def close(self):
        self.closed = True


class Store:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def core_for(connection, *, shutdown=False):
    return SimpleNamespace(
        BOT_LOCK=threading.RLock(),
        BOT_STATE={"enabled": True},
        _durable_state_store=Store(connection),
        runtime_lifecycle_status=lambda: {"shutdownStarted": shutdown},
    )


def _reset_cloud_run_state():
    cloud_run_server._WORKERS_STARTED = False
    cloud_run_server._PROMOTION_THREAD = None
    cloud_run_server._PROMOTION_CONTEXT = None
    cloud_run_server._RECOVERY_STATE.update({
        "status": "idle",
        "attempts": 0,
        "startedAt": 0,
        "lastAttemptAt": 0,
        "recoveredAt": 0,
        "lastError": None,
    })


def setup_function():
    runtime_instance_guard.release()
    _reset_cloud_run_state()


def teardown_function():
    runtime_instance_guard.release()
    _reset_cloud_run_state()


def test_postgres_leader_lock_is_retained_until_release():
    connection = Connection(granted=True)
    core = core_for(connection)
    status = runtime_instance_guard.install(core)
    assert status["leader"] is True
    assert connection.closed is False
    assert core.BOT_STATE["runtimeExecutionLeader"] is True
    released = runtime_instance_guard.release(core, "test complete")
    assert released["leader"] is False
    assert connection.unlocked is True
    assert connection.closed is True
    assert core.BOT_STATE["enabled"] is False


def test_follower_instance_is_fail_closed():
    connection = Connection(granted=False)
    core = core_for(connection)
    status = runtime_instance_guard.install(core)
    assert status["status"] == "standby"
    assert status["leader"] is False
    assert connection.closed is True
    assert core.BOT_STATE["enabled"] is False
    assert core.BOT_STATE["executionGuard"]["ok"] is False


def test_lost_leader_connection_disables_execution():
    connection = Connection(granted=True)
    core = core_for(connection)
    runtime_instance_guard.install(core)
    connection.fail_health = True
    status = runtime_instance_guard.snapshot()
    assert status["status"] == "lost"
    assert status["leader"] is False
    assert core.BOT_STATE["enabled"] is False
    assert core.BOT_STATE["executionGuard"]["ok"] is False


def test_standby_runtime_promotes_and_starts_orchestrator(monkeypatch):
    core = core_for(Connection(granted=False))
    states = iter((
        {"leader": False, "reason": "lock held"},
        {"leader": True, "reason": "lock acquired"},
    ))
    starts = []
    monkeypatch.setattr(cloud_run_server.runtime_instance_guard, "acquire", lambda _core: next(states))
    monkeypatch.setattr(
        cloud_run_server,
        "_ORIGINAL_ORCHESTRATOR_START",
        lambda *args: starts.append(args) or {"status": "running"},
    )
    assert cloud_run_server._promote_from_standby_once(core, object(), object()) is False
    assert starts == []
    assert cloud_run_server._promote_from_standby_once(core, object(), object()) is True
    assert len(starts) == 1


def test_concurrent_promotion_starts_workers_exactly_once(monkeypatch):
    core = core_for(Connection(granted=True))
    starts = []
    monkeypatch.setattr(
        cloud_run_server.runtime_instance_guard,
        "acquire",
        lambda _core: {"leader": True, "reason": "lock acquired"},
    )
    monkeypatch.setattr(
        cloud_run_server,
        "_ORIGINAL_ORCHESTRATOR_START",
        lambda *args: starts.append(args) or {"status": "running"},
    )
    threads = [
        threading.Thread(
            target=cloud_run_server._promote_from_standby_once,
            args=(core, object(), object()),
        )
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(starts) == 1
    assert cloud_run_server._WORKERS_STARTED is True


def test_lost_session_reacquires_even_when_workers_already_started(monkeypatch):
    core = core_for(Connection(granted=True))
    cloud_run_server._WORKERS_STARTED = True
    acquired = []
    starts = []
    monkeypatch.setattr(
        cloud_run_server.runtime_instance_guard,
        "acquire",
        lambda _core: acquired.append(True) or {"leader": True, "reason": "reacquired"},
    )
    monkeypatch.setattr(
        cloud_run_server,
        "_ORIGINAL_ORCHESTRATOR_START",
        lambda *args: starts.append(args),
    )

    assert cloud_run_server._promote_from_standby_once(core, object(), object()) is True
    assert acquired == [True]
    assert starts == []


def test_scheduler_loss_starts_recovery_and_remains_fail_closed(monkeypatch):
    core = core_for(Connection(granted=True))
    symbol_worker = object()
    setup_worker = object()
    handoff = object()
    recovery_calls = []
    monkeypatch.setattr(cloud_run_server.runtime_instance_guard, "is_leader", lambda: False)
    monkeypatch.setattr(
        cloud_run_server.runtime_instance_guard,
        "snapshot",
        lambda: {"leader": False, "status": "lost", "reason": "connection lost"},
    )
    monkeypatch.setattr(
        cloud_run_server,
        "_start_standby_promotion",
        lambda *args: recovery_calls.append(args),
    )

    cloud_run_server._install_orchestrator_guard()
    result = cloud_run_server.secure_server.runtime_orchestrator.run_due_once(
        core,
        symbol_worker,
        setup_worker,
        handoff,
    )

    assert result["status"] == "standby"
    assert core.BOT_STATE["enabled"] is False
    assert len(recovery_calls) == 1
    assert recovery_calls[0] == (core, symbol_worker, setup_worker, handoff)


def test_recovery_context_is_saved_for_mutation_gate(monkeypatch):
    core = core_for(Connection(granted=True))
    context = (core, object(), object(), object())
    cloud_run_server._remember_promotion_context(*context)
    calls = []
    monkeypatch.setattr(
        cloud_run_server,
        "_start_standby_promotion",
        lambda *args: calls.append(args),
    )

    cloud_run_server._start_recovery_from_saved_context()
    assert calls == [context]


def test_shutdown_blocks_promotion_before_lock_acquisition(monkeypatch):
    core = core_for(Connection(granted=True), shutdown=True)
    acquired = []
    monkeypatch.setattr(
        cloud_run_server.runtime_instance_guard,
        "acquire",
        lambda _core: acquired.append(True) or {"leader": True},
    )
    assert cloud_run_server._promote_from_standby_once(core, object(), object()) is False
    assert acquired == []
    assert core.BOT_STATE["enabled"] is False


def test_worker_start_failure_releases_leadership(monkeypatch):
    connection = Connection(granted=True)
    core = core_for(connection)
    runtime_instance_guard.install(core)
    monkeypatch.setattr(
        cloud_run_server.runtime_instance_guard,
        "acquire",
        lambda _core: {"leader": True, "reason": "lock acquired"},
    )
    monkeypatch.setattr(
        cloud_run_server,
        "_ORIGINAL_ORCHESTRATOR_START",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("start failed")),
    )
    assert cloud_run_server._promote_from_standby_once(core, object(), object()) is False
    status = runtime_instance_guard.snapshot()
    assert status["leader"] is False
    assert status["status"] == "released"
    assert connection.unlocked is True
    assert core.BOT_STATE["enabled"] is False


def test_cloud_run_entrypoint_contract_is_explicit():
    source = Path(cloud_run_server.__file__).read_text(encoding="utf-8")
    assert "runtime_instance_guard.install(runtime_core)" in source
    assert "runtime_instance_guard.is_leader()" in source
    assert "_start_standby_promotion" in source
    assert "_promote_from_standby_once" in source
    assert "_start_workers_once" in source
    assert "_start_recovery_from_saved_context" in source
    assert "RUNTIME_LEADER_RECOVERY_BASE_SECONDS" in source
    assert "RuntimeLifecycle" in source
    assert 'os.environ.get("PORT", "8080")' in source
    assert '"/api/bot/start"' in source
    assert '"/api/bybit/demo-order"' in source
