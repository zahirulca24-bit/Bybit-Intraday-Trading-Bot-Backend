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
            self.market_data = SimpleNamespace(snapshot=lambda symbol: {})

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


def test_snapshot_keeps_real_five_minute_confirmation_when_entry_is_15m():
    calls = []

    def fetch_candles(symbol, interval, **kwargs):
        calls.append(interval)
        return ([{"time": {"60": 60, "15": 15, "5": 5}[interval]}], "")

    engine = SimpleNamespace(set_status=lambda *args: None)
    core = SimpleNamespace(fetch_candles=fetch_candles)

    result = scanner_execution_gate.closed_market_snapshot(core, engine, "BTCUSDT", "15")

    assert calls == ["60", "15", "5"]
    assert result["timeframes"]["15M"][-1]["time"] == 15
    assert result["timeframes"]["5M"][-1]["time"] == 5
    assert result["signalCandleTime"] == 15


def test_low_volume_symbol_reaches_deep_scan_and_is_marked_unconfirmed(monkeypatch):
    cfg = {
        "shortlistSize": 20,
        "deepScanSize": 10,
        "normalSpreadPct": 0.08,
        "reducedSpreadPct": 0.15,
        "maxSpreadPct": 0.20,
        "minimumTurnover": 1_000_000,
        "minimumPrice": 0.01,
        "maximumAbsoluteChange": 15.0,
        "minimumAtrPct": 0.25,
        "maximumAtrPct": 3.0,
        "minimumVolumeRatio": 1.20,
        "minimumGrossRr": 2.0,
        "minimumNetRr": 1.70,
        "preferredNetRr": 2.0,
        "normalCostRiskPct": 15.0,
        "maximumCostRiskPct": 35.0,
        "refreshSeconds": 600,
        "deadlineSeconds": 20.0,
        "takerFeePct": 0.055,
        "slippageMultiplier": 0.50,
    }
    tickers = [
        {
            "symbol": f"COIN{i}USDT",
            "bid1Price": "10.00",
            "ask1Price": "10.01",
            "lastPrice": "10.005",
            "turnover24h": str(50_000_000 - i),
            "price24hPcnt": "0.01",
        }
        for i in range(10)
    ]
    core = SimpleNamespace(
        public_bybit_get=lambda *args, **kwargs: {"retCode": 0, "result": {"list": tickers}},
    )
    monkeypatch.setattr(intraday_scanner, "settings", lambda: cfg)
    monkeypatch.setattr(intraday_scanner, "_atr_volume", lambda core, symbol: (0.8, 0.7))
    intraday_scanner._CACHE.update({"symbols": [], "rows": [], "updatedAt": 0, "nextRefreshAt": 0, "metrics": {}})

    result = intraday_scanner.build_universe(core, force=True)

    assert len(result["symbols"]) == 10
    assert result["metrics"]["deepScan"] == 10
    assert result["metrics"]["rejected"] == 0
    assert all(row["volumeConfirmed"] is False for row in result["rows"])


def test_enrichment_rejections_are_visible(monkeypatch):
    cfg = {
        "shortlistSize": 10,
        "deepScanSize": 10,
        "normalSpreadPct": 0.08,
        "reducedSpreadPct": 0.15,
        "maxSpreadPct": 0.20,
        "minimumTurnover": 1_000_000,
        "minimumPrice": 0.01,
        "maximumAbsoluteChange": 15.0,
        "minimumAtrPct": 0.25,
        "maximumAtrPct": 3.0,
        "minimumVolumeRatio": 1.20,
        "minimumGrossRr": 2.0,
        "minimumNetRr": 1.70,
        "preferredNetRr": 2.0,
        "normalCostRiskPct": 15.0,
        "maximumCostRiskPct": 35.0,
        "refreshSeconds": 600,
        "deadlineSeconds": 20.0,
        "takerFeePct": 0.055,
        "slippageMultiplier": 0.50,
    }
    tickers = [
        {
            "symbol": f"TEST{i}USDT",
            "bid1Price": "10.00",
            "ask1Price": "10.01",
            "lastPrice": "10.005",
            "turnover24h": str(50_000_000 - i),
            "price24hPcnt": "0.01",
        }
        for i in range(10)
    ]
    core = SimpleNamespace(
        public_bybit_get=lambda *args, **kwargs: {"retCode": 0, "result": {"list": tickers}},
    )
    monkeypatch.setattr(intraday_scanner, "settings", lambda: cfg)
    monkeypatch.setattr(intraday_scanner, "_atr_volume", lambda core, symbol: (None, None))
    intraday_scanner._CACHE.update({"symbols": [], "rows": [], "updatedAt": 0, "nextRefreshAt": 0, "metrics": {}})

    result = intraday_scanner.build_universe(core, force=True)

    assert result["symbols"] == []
    assert result["metrics"]["rejected"] == 10
    assert result["metrics"]["enrichmentRejected"] == 10
    assert {item["reason"] for item in result["rejections"]} == {"insufficient_closed_history"}
