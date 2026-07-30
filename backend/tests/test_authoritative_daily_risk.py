from __future__ import annotations

import threading
from types import SimpleNamespace

from backend import authoritative_daily_risk as policy
from backend.batch1_safety import validate_start_payload


class FakeLedger:
    def __init__(self, summary=None):
        self.summary = summary or {
            "available": True,
            "stale": False,
            "source": "BYBIT_DEMO_EXECUTION_LIST",
            "tradingDate": "2026-07-29",
            "totalExecutions": 16,
            "entryExecutions": 7,
            "exitExecutions": 9,
            "partialCloseExecutions": 2,
            "completedTrades": 6,
            "reversalExecutions": 0,
            "openPositions": 1,
        }

    def cached_summary(self, trading_date):
        return {**self.summary, "tradingDate": trading_date}


class FakeStore:
    def __init__(self):
        self.values = {
            "risk_state": {
                "maxTradesPerDay": 1,
                "lastReason": "Max trades/day reached (1/1)",
            }
        }

    def get(self, key, default=None):
        return self.values.get(key, default)

    def put(self, key, value):
        self.values[key] = value


class Core:
    def __init__(self, closed_pnl=5.0, ledger=True):
        self.BOT_LOCK = threading.RLock()
        self.BOT_STATE = {
            "enabled": False,
            "dailyLossCapUsdt": 48.0,
            "maxTradesPerDay": 1,
            "lastReason": "Max trades/day reached (1/1)",
            "executionGuard": {"ok": False, "reason": "Max trades/day reached (1/1)"},
            "orderLifecycle": {
                "signal": "WAIT",
                "guard": "blocked",
                "order": "skipped",
                "protection": "skipped",
                "status": "blocked",
                "reason": "Max trades/day reached (1/1)",
            },
        }
        self.closed_pnl = closed_pnl
        self._durable_state_store = FakeStore()
        if ledger:
            self._live_execution_ledger_service = FakeLedger()

    @staticmethod
    def get_current_trading_date_key():
        return "2026-07-29"

    @staticmethod
    def get_trading_day_start_epoch(date_key):
        assert date_key == "2026-07-29"
        return 1_753_722_000

    @staticmethod
    def get_configured_timezone():
        return "Asia/Dhaka"

    def get_daily_closed_pnl(self, date_key):
        assert date_key == "2026-07-29"
        if self.closed_pnl is None:
            return None, "Bybit closed-PnL unavailable"
        return self.closed_pnl, "OK"


def test_trade_count_is_informational_and_never_blocks_new_entries():
    core = Core(closed_pnl=5.0)

    report = policy.evaluate(core, core.BOT_STATE)

    assert report["blocked"] is False
    assert report["newEntriesAllowed"] is True
    assert report["tradeCountLimitEnabled"] is False
    assert report["maxTradesPerDay"] is None
    assert report["tradesPerDay"] == "UNLIMITED"
    assert report["legacyTradeGateActive"] is False
    assert report["tradesToday"] == 6
    assert report["tradeCounters"]["totalExecutions"] == 16
    assert "max trades/day" not in report["reason"].lower()


def test_realized_net_loss_is_the_only_configured_daily_lock():
    core = Core(closed_pnl=-48.0)

    report = policy.evaluate(core, core.BOT_STATE)

    assert report["blocked"] is True
    assert report["newEntriesAllowed"] is False
    assert report["lockType"] == "DAILY_NET_LOSS"
    assert report["lossUsed"] == 48.0
    assert report["remainingLossCapacity"] == 0.0
    assert report["existingPositionProtectionAllowed"] is True
    assert "daily net-loss limit reached" in report["reason"].lower()


def test_missing_pnl_truth_fails_closed_without_fabricated_zeroes():
    core = Core(closed_pnl=None, ledger=False)

    report = policy.evaluate(core, core.BOT_STATE)

    assert report["ok"] is False
    assert report["blocked"] is True
    assert report["lockType"] == "PNL_TRUTH_UNAVAILABLE"
    assert report["closedPnl"] is None
    assert report["lossUsed"] is None
    assert report["tradesToday"] is None
    assert report["tradeCounters"]["completedTrades"] is None


def test_install_replaces_all_readers_and_clears_persisted_legacy_gate():
    core = Core(closed_pnl=3.5)

    status = policy.install(core)

    assert status["installed"] is True
    assert core.BOT_STATE["enabled"] is False  # installation never auto-starts the bot
    assert core.BOT_STATE["maxTradesPerDay"] is None
    assert core.BOT_STATE["dailyRisk"]["blocked"] is False
    assert core.BOT_STATE["executionGuard"]["ok"] is True
    assert "max trades/day" not in core.BOT_STATE["lastReason"].lower()
    assert core.BOT_STATE["orderLifecycle"]["status"] == "idle"

    report = core.daily_risk_report(core.BOT_STATE)
    reached, reason = core.daily_loss_cap_reached(core.BOT_STATE)
    debug = core.get_debug_risk_info(core.BOT_STATE)

    assert reached is report["blocked"] is False
    assert reason == report["reason"]
    assert debug["newEntriesAllowed"] is True
    assert debug["tradesToday"]["limitEnabled"] is False
    assert core._durable_state_store.values["risk_state"]["maxTradesPerDay"] is None
    assert core._durable_state_store.values["risk_state"]["dailyRisk"]["policyId"] == policy.POLICY_ID


def test_start_contract_discards_client_or_persisted_trade_limit():
    defaults = {
        "symbol": "BTCUSDT",
        "interval": "5",
        "maxTradesPerDay": 1,
        "dailyLossCapUsdt": 48,
    }

    config = validate_start_payload(
        {
            "symbol": "BTCUSDT",
            "riskPerTradePct": 2,
            "maxTradesPerDay": 1,
        },
        defaults,
    )

    assert config["maxTradesPerDay"] is None
