from types import SimpleNamespace

from backend import intraday_scanner, scanner_execution_gate, scanner_review_fixes


class DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_empty_authoritative_refresh_clears_eligible_cache(monkeypatch):
    intraday_scanner._CACHE.update({
        "symbols": ["BTCUSDT"],
        "rows": [{"symbol": "BTCUSDT"}],
        "shortlist": [{"symbol": "BTCUSDT"}],
        "metrics": {"enriched": 1},
    })
    monkeypatch.setattr(
        intraday_scanner,
        "build_universe",
        lambda core, force=False, limit=None: {
            "symbols": ["BTCUSDT"],
            "rows": [{"symbol": "BTCUSDT"}],
            "metrics": {"enriched": 0},
        },
    )
    core = SimpleNamespace(BOT_LOCK=DummyLock(), BOT_STATE={"takeProfitPct": 1.6})
    scanner_review_fixes.install(core)

    result = intraday_scanner.build_universe(core, force=True)

    assert result["symbols"] == []
    assert result["rows"] == []
    assert core.BOT_STATE["takeProfitPct"] == 2.0


def test_strong_only_gate_uses_current_evaluation_votes(monkeypatch):
    class Engine:
        def __init__(self):
            self.status = None

        def set_status(self, name, value):
            self.status = (name, value)

        def risk_check(self, state, signal):
            return True, "approved"

    engine = Engine()
    core = SimpleNamespace()
    core.top_gainer_universe = lambda force=False, limit=10: {}
    core.get_bot_engine = lambda: engine
    core.evaluate_signal = lambda symbol, interval, mode="balanced": (
        "Buy",
        "ok",
        [{"signal": "Buy"}, {"signal": "Buy"}],
        {},
        {},
        {},
    )
    core.fetch_candles = lambda *args, **kwargs: ([], "")
    core.simple_atr = lambda *args: 0
    monkeypatch.setattr(
        intraday_scanner,
        "build_universe",
        lambda core, force=False: {
            "symbols": ["BTCUSDT"],
            "rows": [{"symbol": "BTCUSDT", "costTier": "strong_only"}],
        },
    )

    scanner_execution_gate.install(core)
    core.evaluate_signal("BTCUSDT", "5")
    ok, reason = engine.risk_check({"symbol": "BTCUSDT", "engineVotes": []}, "Buy")

    assert ok is True
    assert reason == "approved"


def test_default_target_can_pass_net_rr_at_low_normal_spread(monkeypatch):
    monkeypatch.setattr(
        intraday_scanner,
        "settings",
        lambda: {
            "normalSpreadPct": 0.08,
            "reducedSpreadPct": 0.15,
            "maxSpreadPct": 0.20,
            "slippageMultiplier": 0.50,
            "takerFeePct": 0.055,
            "minimumGrossRr": 2.0,
            "minimumNetRr": 1.70,
            "preferredNetRr": 2.0,
            "normalCostRiskPct": 15.0,
            "maximumCostRiskPct": 35.0,
        },
    )
    core = SimpleNamespace(
        public_bybit_get=lambda *args, **kwargs: {
            "retCode": 0,
            "result": {"list": [{"bid1Price": "100", "ask1Price": "100.02", "lastPrice": "100.01"}]},
        }
    )

    result = intraday_scanner.estimate_trade_cost(core, "BTCUSDT", 1000, 20, 0.8, 2.0)

    assert result["grossRr"] >= 2.0
    assert result["netRr"] >= 1.70
    assert result["ok"] is True
