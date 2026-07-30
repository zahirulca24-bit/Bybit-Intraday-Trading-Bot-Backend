from __future__ import annotations

from decimal import Decimal

import pytest

from backend import replay_simulated_execution as simulation


def sample_trade(side="Buy", entry="100", stop="95", target="110", opened=1_800_000_000_000):
    return {
        "tradeId": "sim_trade_0001",
        "symbol": "BTCUSDT",
        "side": side,
        "status": "OPEN",
        "entryTime": opened,
        "entryPrice": entry,
        "quantity": "2",
        "realizedPnl": "0",
        "fees": "0.12",
        "payload": {"stopLoss": stop, "takeProfit": target},
    }


def sample_candle(open_price="100", high="111", low="94", close="103", offset=300_000):
    return {
        "openTime": 1_800_000_000_000 + offset,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": "100",
    }


def test_execution_config_defaults_and_bounds():
    defaults = simulation.execution_config({"config": {}})
    assert defaults == {"feeBps": Decimal("6"), "maxLeverage": Decimal("3")}
    configured = simulation.execution_config(
        {"config": {"replayFeeBps": "8.5", "maxLeverage": "2"}}
    )
    assert configured["feeBps"] == Decimal("8.5")
    assert configured["maxLeverage"] == Decimal("2")
    with pytest.raises(simulation.ReplaySimulationError, match="between"):
        simulation.execution_config({"config": {"replayFeeBps": 101}})
    with pytest.raises(simulation.ReplaySimulationError, match="between"):
        simulation.execution_config({"config": {"maxLeverage": 11}})


def test_same_candle_conflict_resolves_stop_first():
    decision = simulation._exit_decision(sample_trade(), sample_candle())
    assert decision["reason"] == "stop_loss"
    assert decision["price"] == Decimal("95")
    assert decision["sameCandleConflict"] is True


def test_gap_through_stop_uses_worse_open_price():
    buy = simulation._exit_decision(
        sample_trade(), sample_candle(open_price="90", high="96", low="89", close="93")
    )
    assert buy["price"] == Decimal("90")
    sell = simulation._exit_decision(
        sample_trade(side="Sell", stop="105", target="90"),
        sample_candle(open_price="110", high="112", low="106", close="108"),
    )
    assert sell["price"] == Decimal("110")


def test_limited_liability_caps_gap_loss_and_exposes_adjustment():
    applied, adjustment, limited = simulation._limited_liability_delta(
        Decimal("-2970.60000000"), Decimal("999.40000000")
    )
    assert applied == Decimal("-999.40000000")
    assert adjustment == Decimal("1971.20000000")
    assert limited is True
    assert Decimal("999.40000000") + applied == Decimal("0E-8")

    normal, normal_adjustment, normal_limited = simulation._limited_liability_delta(
        Decimal("-50"), Decimal("999.4")
    )
    assert normal == Decimal("-50.00000000")
    assert normal_adjustment == Decimal("0E-8")
    assert normal_limited is False


def test_take_profit_and_pnl_are_side_correct():
    buy = simulation._exit_decision(
        sample_trade(), sample_candle(high="112", low="99")
    )
    assert buy["reason"] == "take_profit"
    assert buy["price"] == Decimal("110")
    assert simulation._gross_pnl(
        "Buy", Decimal("100"), Decimal("110"), Decimal("2")
    ) == Decimal("20.00000000")
    assert simulation._gross_pnl(
        "Sell", Decimal("100"), Decimal("90"), Decimal("2")
    ) == Decimal("20.00000000")


def test_entry_candle_cannot_trigger_its_own_protection():
    trade = sample_trade(opened=1_800_000_300_000)
    assert simulation._exit_decision(trade, sample_candle(offset=300_000)) is None


def test_trade_identity_and_fee_are_deterministic():
    first = simulation._trade_id("replay_test_0001", 1_800_000_000_000)
    second = simulation._trade_id("replay_test_0001", 1_800_000_000_000)
    assert first == second
    assert first.startswith("sim_")
    assert 8 <= len(first) <= 80
    assert simulation._fee(Decimal("100"), Decimal("2"), Decimal("6")) == Decimal(
        "0.12000000"
    )
