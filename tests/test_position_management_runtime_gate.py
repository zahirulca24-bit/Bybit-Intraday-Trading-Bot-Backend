from backend import position_synced_server as runtime


class Engine:
    def __init__(self):
        self.status = {}

    def set_status(self, name, value):
        self.status[name] = value


def test_pending_partial_close_blocks_management_before_any_new_action(monkeypatch):
    engine = Engine()
    monkeypatch.setattr(
        runtime,
        "position_management_entry_gate",
        lambda core: (
            False,
            "Partial close remains unresolved",
            {"fill": {"state": "partial"}},
        ),
    )
    monkeypatch.setattr(runtime.guarded.core, "get_bot_engine", lambda: engine)

    def must_not_manage(*args, **kwargs):
        raise AssertionError("management must not retry an unresolved reduce order")

    monkeypatch.setattr(runtime, "manage_positions", must_not_manage)

    result = runtime._verified_manage_open_positions({})

    assert result["ok"] is False
    assert result["actions"] == []
    assert result["pendingPartialClose"]["fill"]["state"] == "partial"
    assert engine.status["tradeManagement"] == "blocked"
