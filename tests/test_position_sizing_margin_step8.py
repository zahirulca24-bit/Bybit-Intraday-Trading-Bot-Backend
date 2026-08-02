import threading
from decimal import Decimal, ROUND_DOWN

import pytest

from backend import position_sizing_margin as sizing
from backend import setup_worker


SETUP_TIME = 60 * 15 * 60 * 1000


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {"ok": True, "degraded": False}

    def get(self, key, default=None):
        value = self.values.get(key, default)
        if isinstance(value, dict):
            return dict(value)
        return value

    def put(self, key, value):
        self.values[key] = dict(value)
        return True


def candles(low=99.0, high=101.0, close=100.0):
    rows = []
    for index in range(60):
        rows.append(
            {
                "time": SETUP_TIME - ((59 - index) * 15 * 60 * 1000),
                "open": close,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000.0,
            }
        )
    return rows


def candidate(key="btc-1", **updates):
    row = {
        "candidateKey": key,
        "symbol": "BTCUSDT",
        "side": "Buy",
        "strategy": "Trend Follow",
        "setupFifteenMinuteCandleTime": SETUP_TIME,
        "entryFiveMinuteCandleTime": SETUP_TIME + (15 * 60 * 1000),
        "entryReference": 100.0,
        "strategyStrength": 4.8,
        "grade": "A+",
        "riskStatus": "APPROVED_RISK",
        "riskApproved": True,
        "riskSizeFactor": 1.0,
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "AWAITING_POSITION_SIZING",
        "orderSubmitted": False,
    }
    row.update(updates)
    return row


def risk_snapshot(*rows):
    queue = list(rows) if rows else [candidate()]
    return {
        "status": "ready",
        "fiveMinuteCandleTime": SETUP_TIME + (15 * 60 * 1000),
        "inputFingerprint": "risk-input-v1",
        "approvedRiskQueue": queue,
        "approvedRiskQueueSize": len(queue),
    }


class CoreStub:
    def __init__(self, *rows, low=99.0, high=101.0, close=100.0):
        self.BOT_LOCK = threading.Lock()
        self.BOT_STATE = {}
        self._durable_state_store = MemoryStore()
        self.risk_snapshot = risk_snapshot(*rows)
        self.candle_rows = candles(low=low, high=high, close=close)
        self.wallet = {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "totalEquity": "1000",
                        "totalAvailableBalance": "1000",
                        "totalInitialMargin": "0",
                    }
                ]
            },
        }
        self.rules = {
            "ok": True,
            "qtyStep": Decimal("0.1"),
            "minOrderQty": Decimal("0.1"),
            "maxOrderQty": Decimal("1000"),
            "minNotionalValue": Decimal("5"),
        }
        self.order_calls = 0
        self.manual_sizing_calls = 0
        self.calculate_position_sizing = self._manual_sizing

    def _manual_sizing(self, symbol, state):
        self.manual_sizing_calls += 1
        return {"ok": True, "riskPerTradePct": 2.0}

    def authoritative_entry_risk_status(self):
        return dict(self.risk_snapshot)

    def fetch_candles(self, symbol, interval, limit=120):
        assert interval == "15"
        return list(self.candle_rows), "OK"

    def bybit_request(self, method, path, params=None):
        if path == "/v5/account/wallet-balance":
            return dict(self.wallet)
        self.order_calls += 1
        raise AssertionError("Step 8 must not submit or mutate an exchange order")

    def public_bybit_get(self, path, params=None):
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": (params or {}).get("symbol", "BTCUSDT"),
                        "bid1Price": "99.99",
                        "ask1Price": "100.01",
                        "lastPrice": "100",
                    }
                ]
            },
        }

    def get_instrument_rules(self, symbol):
        return dict(self.rules)

    def floor_to_step(self, value, step):
        value = Decimal(str(value))
        step = Decimal(str(step))
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    def format_qty(self, value):
        return format(Decimal(str(value)).normalize(), "f")

    def get_open_positions(self):
        return [], "OK"


@pytest.fixture(autouse=True)
def reset_sizing(monkeypatch):
    sizing._reset_for_tests()
    monkeypatch.setattr(
        sizing.cost_policy_fix,
        "_market_cost",
        lambda core, symbol, scanner: {
            "ok": True,
            "spreadPct": 0.02,
            "spreadTier": "normal",
            "slippagePct": 0.01,
            "estimatedRoundTripFeePct": 0.11,
            "estimatedTotalCostPct": 0.14,
        },
    )
    monkeypatch.setattr(
        sizing.cost_policy_fix,
        "evaluate_cost_policy",
        lambda **kwargs: {
            **dict(kwargs["market_cost"]),
            "ok": True,
            "blockCode": None,
            "reason": "Cost and net RR approved",
            "adjustedTakeProfitPct": max(2.25, float(kwargs["take_pct"])),
            "grossRr": max(2.25, float(kwargs["take_pct"])) / float(kwargs["stop_pct"]),
            "netRr": 2.0,
            "sizeFactor": 1.0,
        },
    )
    yield
    sizing._reset_for_tests()


def test_a_plus_uses_one_percent_risk_and_isolated_5x():
    core = CoreStub()
    original_manual = core.calculate_position_sizing
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    assert result["approvedSizingQueueSize"] == 1
    approved = result["approvedSizingQueue"][0]
    assert approved["positionSizingStatus"] == "SIZING_APPROVED"
    assert approved["gradeRiskPct"] == 1.0
    assert approved["effectiveRiskPerTradePct"] == 1.0
    assert approved["riskBudgetUsdt"] == 10.0
    assert approved["qty"] == "10"
    assert approved["notional"] == "1000"
    assert approved["requiredInitialMarginUsdt"] == 200.0
    assert approved["marginMode"] == "ISOLATED"
    assert approved["leverage"] == 5
    assert approved["technicalStopLoss"] == 99.0
    assert approved["takeProfitReference"] == 102.25
    assert approved["orderSubmitted"] is False
    assert core.calculate_position_sizing == original_manual
    assert core.manual_sizing_calls == 0
    assert core.order_calls == 0


def test_a_grade_uses_point_seven_five_percent_risk():
    core = CoreStub(candidate(grade="A"))
    sizing.install(core, setup_worker)

    approved = sizing.build(core, now=5000)["approvedSizingQueue"][0]

    assert approved["gradeRiskPct"] == 0.75
    assert approved["riskBudgetUsdt"] == 7.5
    assert approved["qty"] == "7.5"
    assert approved["requiredInitialMarginUsdt"] == 150.0


def test_b_plus_is_rejected_even_if_upstream_is_malformed_as_approved():
    core = CoreStub(candidate(grade="B+"))
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    assert result["approvedSizingQueueSize"] == 0
    assert result["rows"][0]["sizingDecision"]["code"] == "GRADE_RISK_BLOCKED"


def test_invalid_structural_stop_is_blocked_without_fixed_percent_fallback():
    core = CoreStub(candidate(entryReference=98.0))
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    row = result["rows"][0]
    assert row["sizingDecision"]["code"] == "INVALID_TECHNICAL_STOP"
    assert result["approvedSizingQueueSize"] == 0
    assert "fixed" not in row["sizingDecision"]["reason"].lower()


def test_per_trade_margin_cap_reduces_quantity_without_widening_stop():
    core = CoreStub(low=99.5, high=100.5)
    sizing.install(core, setup_worker)

    approved = sizing.build(core, now=5000)["approvedSizingQueue"][0]

    assert approved["technicalStopLoss"] == 99.5
    assert approved["marginReducedQuantity"] is True
    assert approved["qty"] == "12.5"
    assert approved["requiredInitialMarginUsdt"] == 250.0
    assert approved["actualStopRiskUsdt"] == 6.25
    assert approved["riskBudgetUsdt"] == 10.0


def test_three_candidates_respect_combined_sixty_percent_and_free_forty_percent():
    rows = [candidate(f"btc-{index}") for index in range(1, 4)]
    core = CoreStub(*rows)
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    assert result["approvedSizingQueueSize"] == 3
    assert result["metrics"]["reservedInitialMarginUsdt"] == 600.0
    third = result["approvedSizingQueue"][2]
    assert third["projectedTotalInitialMarginUsdt"] == 600.0
    assert third["projectedFreeMarginUsdt"] == 400.0
    assert third["marginCaps"] == {
        "perTradePct": 25.0,
        "combinedPct": 60.0,
        "minimumFreeReservePct": 40.0,
    }


def test_available_margin_reduces_quantity_before_exchange_submission():
    core = CoreStub()
    core.wallet["result"]["list"][0]["totalAvailableBalance"] = "100"
    sizing.install(core, setup_worker)

    approved = sizing.build(core, now=5000)["approvedSizingQueue"][0]

    assert approved["qty"] == "5"
    assert approved["requiredInitialMarginUsdt"] == 100.0
    assert approved["marginReducedQuantity"] is True
    assert core.order_calls == 0


def test_bybit_min_notional_blocks_too_small_margin_capped_quantity():
    core = CoreStub()
    core.wallet["result"]["list"][0]["totalAvailableBalance"] = "1"
    core.rules["minNotionalValue"] = Decimal("100")
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    assert result["approvedSizingQueueSize"] == 0
    assert result["rows"][0]["sizingDecision"]["code"] == "BYBIT_QUANTITY_RULE_BLOCKED"


def test_existing_cost_policy_can_block_sizing(monkeypatch):
    core = CoreStub()
    monkeypatch.setattr(
        sizing.cost_policy_fix,
        "evaluate_cost_policy",
        lambda **kwargs: {
            **dict(kwargs["market_cost"]),
            "ok": False,
            "blockCode": "BLOCKED_NET_RR",
            "reason": "Net RR below existing minimum",
        },
    )
    sizing.install(core, setup_worker)

    result = sizing.build(core, now=5000)

    assert result["approvedSizingQueueSize"] == 0
    assert result["rows"][0]["sizingDecision"]["code"] == "BLOCKED_NET_RR"


def test_same_input_is_idempotent_and_persisted_across_restart():
    core = CoreStub()
    store = core._durable_state_store
    sizing.install(core, setup_worker)

    first = sizing.ensure_current(core, now=5000)
    second = sizing.ensure_current(core, now=5100)

    assert first["persisted"] is True
    assert second["approvedSizingQueueSize"] == 1
    assert first["inputFingerprint"] == second["inputFingerprint"]

    sizing._reset_for_tests()
    restarted = CoreStub()
    restarted._durable_state_store = store
    sizing.install(restarted, setup_worker)
    restored = sizing.snapshot()

    assert restored["inputFingerprint"] == first["inputFingerprint"]
    assert restored["approvedSizingQueueSize"] == 1
    assert restored["approvedSizingQueue"][0]["candidateKey"] == "btc-1"
