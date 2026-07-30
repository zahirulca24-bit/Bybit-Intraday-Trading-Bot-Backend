from __future__ import annotations

from types import SimpleNamespace

from backend.batch1_safety import fail_closed_daily_risk, validate_start_payload


def _defaults():
    return {
        "symbol": "BTCUSDT",
        "interval": "5",
        "qty": "0.001",
        "maxAllocationUsdt": 250,
        "riskPerTradePct": 2.0,
        "maxOpenPositions": 3,
        "dailyLossCapUsdt": 47.90,
        "maxTradesPerDay": 6,
        "stopLossPct": 0.8,
        "takeProfitPct": 1.6,
        "breakevenTriggerPct": 0.6,
        "partialTpTriggerPct": 1.4,
        "partialTpClosePct": 40,
        "trailingStopTriggerPct": 1.8,
        "trailingStopDistancePct": 0.45,
        "cooldownSeconds": 180,
        "mode": "conservative",
    }


def test_start_clears_legacy_six_trade_limit_and_keeps_open_limit_three():
    config = validate_start_payload({}, _defaults())
    assert config["maxOpenPositions"] == 3
    assert config["maxTradesPerDay"] is None


def test_client_cannot_reenable_daily_trade_count_limit():
    config = validate_start_payload({"maxTradesPerDay": 1}, _defaults())
    assert config["maxTradesPerDay"] is None


def test_six_or_more_accepted_orders_do_not_block_when_loss_cap_not_hit():
    core = SimpleNamespace(
        get_current_trading_date_key=lambda: "2026-07-29",
        get_daily_closed_pnl=lambda _: (-25.0, "OK"),
        count_today_accepted_orders=lambda *args: (_ for _ in ()).throw(
            AssertionError("trade count must not be consulted")
        ),
        get_bot_engine=lambda: object(),
    )
    blocked, reason = fail_closed_daily_risk(
        core,
        {"dailyLossCapUsdt": 47.90, "maxTradesPerDay": 6},
    )
    assert blocked is False
    assert "trade count unlimited" in reason


def test_monetary_daily_loss_cap_still_blocks_new_entries():
    core = SimpleNamespace(
        get_current_trading_date_key=lambda: "2026-07-29",
        get_daily_closed_pnl=lambda _: (-47.90, "OK"),
    )
    blocked, reason = fail_closed_daily_risk(core, {"dailyLossCapUsdt": 47.90})
    assert blocked is True
    assert "Daily loss cap reached" in reason
