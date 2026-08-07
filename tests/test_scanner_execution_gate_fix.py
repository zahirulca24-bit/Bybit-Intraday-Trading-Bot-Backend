from __future__ import annotations

import types

from backend import intraday_scanner, scanner_execution_gate


class DummyEngine:
    def __init__(self):
        self.status = {}
        self.risk_calls = []
        self.market_data = types.SimpleNamespace(snapshot=lambda symbol: {})

    def set_status(self, *args):
        pass

    def risk_check(self, state, signal):
        self.risk_calls.append((state, signal))
        return True, "ok"


class DummyCore:
    def __init__(self):
        self.engine = DummyEngine()
        self._current_scanner_signal = {"symbol": "BTCUSDT", "signal": "Buy", "votes": []}

    def get_bot_engine(self):
        return self.engine

    def top_gainer_universe(self, *args, **kwargs):
        return {"symbols": [], "rows": []}

    def evaluate_signal(self, symbol, interval, mode="balanced"):
        return "WAIT", "fixture", [], {}, {}, {}


def test_execution_gate_refreshes_universe_on_miss(monkeypatch):
    """A stale universe must refresh before blocking a freshly signalled symbol."""

    core = DummyCore()
    engine = core.get_bot_engine()
    calls = []

    def build_universe_stub(core_arg, force=False, limit=None):
        calls.append(bool(force))
        if not force:
            return {"symbols": [], "rows": []}
        return {"symbols": ["BTCUSDT"], "rows": [{"symbol": "BTCUSDT", "costTier": "normal"}]}

    monkeypatch.setattr(intraday_scanner, "build_universe", build_universe_stub)
    scanner_execution_gate.install(core)

    state = {"symbol": "BTCUSDT"}
    allowed, reason = engine.risk_check(state, "Buy")

    assert calls, "build_universe was not called"
    assert calls[0] is False
    assert True in calls, "expected a forced refresh when the symbol was missing"
    assert allowed is True, f"Expected allowed after refresh; blocked with reason: {reason}"


def test_strong_only_tier_accepts_one_matching_strategy_vote(monkeypatch):
    """Locked policy permits one valid matching strategy; no hidden two-vote gate."""

    core = DummyCore()
    engine = core.get_bot_engine()
    core._current_scanner_signal = {
        "symbol": "BTCUSDT",
        "signal": "Buy",
        "votes": [{"engine": "trend", "signal": "Buy", "strength": 4.0}],
    }

    monkeypatch.setattr(
        intraday_scanner,
        "build_universe",
        lambda core_arg, force=False, limit=None: {
            "symbols": ["BTCUSDT"],
            "rows": [{"symbol": "BTCUSDT", "costTier": "strong_only"}],
        },
    )

    scanner_execution_gate.install(core)
    allowed, reason = engine.risk_check({"symbol": "BTCUSDT"}, "Buy")

    assert allowed is True, f"Single matching strategy must be allowed; blocked with reason: {reason}"
