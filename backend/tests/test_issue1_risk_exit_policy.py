from __future__ import annotations

import threading
from types import SimpleNamespace

from backend import issue1_risk_exit_policy as policy


class Store:
    def __init__(self):
        self.data = {}

    def status(self):
        return {"ok": True, "degraded": False, "persistentPathConfigured": True}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value


class Journal:
    def __init__(self):
        self.entries = []

    def add(self, event, payload):
        self.entries.append({"event": event, "payload": payload})


class Engine:
    def __init__(self):
        self.journal = Journal()
        self.status = {}

    def set_status(self, name, value):
        self.status[name] = value


def make_core(net_pnl=-25.0, equity=958.0):
    engine = Engine()
    core = SimpleNamespace()
    core._durable_state_store = Store()
    core.BOT_LOCK = threading.Lock()
    core.BOT_STATE = {}
    core.get_current_trading_date_key = lambda: "2026-07-29"
    core.get_wallet_equity = lambda: (equity, "OK")
    core.get_daily_closed_pnl = lambda _: (net_pnl, "OK")
    core.get_bot_engine = lambda: engine
    return core, engine


def test_daily_gate_uses_five_percent_starting_equity_and_ignores_trade_count():
    core, _ = make_core(net_pnl=-25.0)
    blocked, reason = policy.daily_net_loss_gate(core, {})
    assert blocked is False
    assert "trade count unlimited" in reason
    assert core.BOT_STATE["dailyRisk"]["limitUsdt"] == 47.9
    assert core.BOT_STATE["maxTradesPerDay"] is None


def test_daily_gate_blocks_once_loss_exceeds_five_percent_currency_threshold():
    core, _ = make_core(net_pnl=-47.91)
    blocked, reason = policy.daily_net_loss_gate(core, {})
    assert blocked is True
    assert "Daily net-loss lock reached" in reason


def test_r_multiple_uses_original_stop_distance():
    position = {"side": "Buy", "markPrice": "103", "symbol": "BTCUSDT"}
    plan = {"entryPrice": "100", "riskDistance": "2", "side": "Buy"}
    assert policy._r_multiple(position, plan) == 1.5


def test_tp1_then_tp2_and_runner_policy(monkeypatch):
    core, engine = make_core()
    position = {
        "symbol": "BTCUSDT",
        "side": "Buy",
        "positionIdx": 0,
        "openTime": "1",
        "avgPrice": "100",
        "markPrice": "103",
        "stopLoss": "98",
        "size": "10",
    }
    core.get_open_positions = lambda: ([position], "OK")
    monkeypatch.setattr(policy.pm, "_partial", lambda *args, **kwargs: {"verified": True, "positionKey": policy.pm.position_key(position), "result": {"retCode": 0}})
    monkeypatch.setattr(policy.pm, "_breakeven", lambda *args, **kwargs: {"verified": True, "result": {"retCode": 0}})
    monkeypatch.setattr(policy.pm, "_trailing", lambda *args, **kwargs: {"verified": True, "result": {"retCode": 0}})
    monkeypatch.setattr(policy.pm, "pending_partial_close", lambda: None)

    first = policy.manage_positions(core, {})
    assert any(row.get("stage") == "TP1" for row in first["actions"])
    assert any(row.get("stage") == "TP1_BREAKEVEN" for row in first["actions"])
    assert any(row["event"] == "tp1_1_5r" for row in engine.journal.entries)

    position["markPrice"] = "104"
    second = policy.manage_positions(core, {})
    assert any(row.get("stage") == "TP2" for row in second["actions"])
    assert any(row.get("stage") == "RUNNER" for row in second["actions"])
    assert any(row["event"] == "tp2_2r" for row in engine.journal.entries)
    assert any(row["event"] == "runner_trailing_0_5r" for row in engine.journal.entries)
