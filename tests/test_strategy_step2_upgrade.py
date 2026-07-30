from __future__ import annotations

from types import SimpleNamespace

from backend import strategy_step2_upgrade


def _candles(count=30, start=100.0, step=0.2):
    rows = []
    price = start
    for index in range(count):
        close = price + step
        rows.append(
            {
                "time": index * 900_000,
                "open": price,
                "high": close + 0.4,
                "low": price - 0.4,
                "close": close,
                "volume": 1000 + index,
            }
        )
        price = close
    return rows


def test_dynamic_atr_plan_builds_valid_buy_rr():
    plan, reason = strategy_step2_upgrade.dynamic_price_plan(_candles(), "Buy", 2.0, 12)
    assert plan is not None, reason
    assert plan["pricePlanSource"] == "ATR_15M"
    assert plan["stopLoss"] < plan["entryReference"] < plan["takeProfitReference"]
    assert plan["riskReward"] >= 2.0
    assert plan["atr15m"] > 0


def test_dynamic_atr_plan_builds_valid_sell_rr():
    plan, reason = strategy_step2_upgrade.dynamic_price_plan(_candles(), "Sell", 2.0, 12)
    assert plan is not None, reason
    assert plan["takeProfitReference"] < plan["entryReference"] < plan["stopLoss"]
    assert plan["riskReward"] >= 2.0


def test_signal_grading_policy():
    assert strategy_step2_upgrade.grade_for_strength(4.8)["grade"] == "A+"
    assert strategy_step2_upgrade.grade_for_strength(4.0)["grade"] == "A"
    b_plus = strategy_step2_upgrade.grade_for_strength(3.4)
    assert b_plus["grade"] == "B+"
    assert b_plus["watchOnly"] is True
    assert b_plus["executionEligible"] is False
    assert strategy_step2_upgrade.grade_for_strength(2.0)["grade"] == "REJECT"


def test_install_blocks_b_plus_from_execution_queue():
    queued = []

    def original_queue(candidate, queue_limit):
        queued.append(dict(candidate))
        return True

    def original_evaluate(core, active_row, now_ms, cfg):
        candidate = {
            "symbol": "BTCUSDT",
            "strategyStrength": 3.4,
            "status": "CONFIRMED",
            "queued": True,
        }
        setup_worker._queue_candidate(candidate, 10)
        return candidate

    setup_worker = SimpleNamespace(
        _queue_candidate=original_queue,
        _evaluate_symbol=original_evaluate,
        _price_plan=lambda *args: ({}, "old"),
    )
    core = SimpleNamespace()

    strategy_step2_upgrade.install(core, setup_worker)
    result = setup_worker._evaluate_symbol(core, {}, 0, {})

    assert result["grade"] == "B+"
    assert result["status"] == "NEAR_SETUP"
    assert result["queued"] is False
    assert queued == []
