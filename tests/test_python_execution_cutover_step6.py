import threading

from backend import python_execution_cutover as cutover


class JournalStub:
    def __init__(self):
        self.entries = []

    def add(self, event, payload):
        self.entries.append({"event": event, "payload": payload})


class EngineStub:
    def __init__(self):
        self.journal = JournalStub()
        self.status = {}
        self.original_execute_calls = []

        def original_execute(state, signal):
            self.original_execute_calls.append((state, signal))
            return {"retCode": 0}

        self.execute = original_execute

    def set_status(self, engine, state):
        self.status[engine] = state


class CoreStub:
    def __init__(self):
        self.BOT_LOCK = threading.Lock()
        self.BOT_STATE = {}
        self.engine = EngineStub()
        self.place_calls = []

        def place_demo_order(
            symbol,
            side,
            qty,
            source,
            stop_loss_pct=None,
            take_profit_pct=None,
        ):
            self.place_calls.append(
                (
                    symbol,
                    side,
                    qty,
                    source,
                    stop_loss_pct,
                    take_profit_pct,
                )
            )
            return {"retCode": 0, "retMsg": "manual accepted"}

        self.place_demo_order = place_demo_order

    def get_bot_engine(self):
        return self.engine


def test_auto_and_setup_worker_orders_fail_closed():
    core = CoreStub()
    cutover.install(core)

    auto = core.place_demo_order(
        "BTCUSDT", "Buy", "0.01", "auto", 1.0, 2.0
    )
    setup = core.place_demo_order(
        "ETHUSDT", "Sell", "0.02", "setup-worker", 1.0, 2.0
    )

    assert auto["retCode"] == -2606
    assert setup["retCode"] == -2606
    assert auto["code"] == "PYTHON_AUTO_EXECUTION_DISABLED"
    assert auto["orderSubmitted"] is False
    assert core.place_calls == []


def test_manual_demo_order_path_is_preserved():
    core = CoreStub()
    cutover.install(core)

    result = core.place_demo_order(
        "BTCUSDT", "Buy", "0.01", "manual", 1.0, 2.0
    )

    assert result["retCode"] == 0
    assert len(core.place_calls) == 1
    assert core.place_calls[0][3] == "manual"


def test_engine_auto_execute_is_blocked_without_calling_original():
    core = CoreStub()
    cutover.install(core)

    result = core.engine.execute({"symbol": "BTCUSDT"}, "Buy")

    assert result["retCode"] == -2606
    assert result["executionAuthority"] == "NODE_JS_PENDING"
    assert core.engine.original_execute_calls == []
    assert core.engine.status["tradeManagement"] == "blocked"
    assert core.engine.journal.entries[-1]["event"] == (
        "python_auto_execution_blocked"
    )


def test_cutover_status_and_bot_state_are_explicit():
    core = CoreStub()
    status = cutover.install(core)

    assert status["installed"] is True
    assert status["manualDemoOrderPreserved"] is True
    assert status["openPositionManagementPreserved"] is True
    assert core.BOT_STATE["executionAuthority"] == "NODE_JS_PENDING"
    assert core.BOT_STATE["legacyPythonAutoExecutionDisabled"] is True
