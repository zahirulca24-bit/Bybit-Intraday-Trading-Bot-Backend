from backend import execution_truth_recovery, position_sizing_margin


class Core:
    def __init__(self):
        self._execution_truth_daily_pnl_fallback_installed = False
        self._responses = []

    def get_daily_closed_pnl(self, _trading_date):
        return None, "legacy source unavailable"

    def get_trading_day_start_epoch(self, _trading_date):
        return 1_000


class Analytics:
    @staticmethod
    def _fetch_closed_trades(_core, _limit):
        return [
            {"closedAt": 1_000_000, "closedPnl": -2.5},
            {"closedAt": 1_100_000, "closedPnl": 1.0},
            {"closedAt": 90_000_000, "closedPnl": 99.0},
        ]


def test_wallet_zero_available_balance_falls_back_conservatively(monkeypatch):
    original = position_sizing_margin._wallet_snapshot
    monkeypatch.setattr(
        position_sizing_margin,
        "_wallet_snapshot",
        lambda _core: {
            "ok": True,
            "equity": 1000.0,
            "availableMargin": 0.0,
            "currentInitialMargin": 100.0,
        },
    )
    monkeypatch.setattr(
        position_sizing_margin,
        "_execution_truth_wallet_fallback_installed",
        False,
        raising=False,
    )
    try:
        execution_truth_recovery._install_wallet_margin_fallback()
        result = position_sizing_margin._wallet_snapshot(object())
        assert result["availableMargin"] == 900.0
        assert result["availableMarginFallbackApplied"] is True
        assert result["availableMarginSource"] == "EQUITY_MINUS_CURRENT_INITIAL_MARGIN"
    finally:
        monkeypatch.setattr(position_sizing_margin, "_wallet_snapshot", original)
        monkeypatch.setattr(
            position_sizing_margin,
            "_execution_truth_wallet_fallback_installed",
            False,
            raising=False,
        )


def test_wallet_positive_available_balance_remains_authoritative(monkeypatch):
    original = position_sizing_margin._wallet_snapshot
    monkeypatch.setattr(
        position_sizing_margin,
        "_wallet_snapshot",
        lambda _core: {
            "ok": True,
            "equity": 1000.0,
            "availableMargin": 700.0,
            "currentInitialMargin": 100.0,
        },
    )
    monkeypatch.setattr(
        position_sizing_margin,
        "_execution_truth_wallet_fallback_installed",
        False,
        raising=False,
    )
    try:
        execution_truth_recovery._install_wallet_margin_fallback()
        result = position_sizing_margin._wallet_snapshot(object())
        assert result["availableMargin"] == 700.0
        assert result["availableMarginFallbackApplied"] is False
        assert result["availableMarginSource"] == "BYBIT_TOTAL_AVAILABLE_BALANCE"
    finally:
        monkeypatch.setattr(position_sizing_margin, "_wallet_snapshot", original)
        monkeypatch.setattr(
            position_sizing_margin,
            "_execution_truth_wallet_fallback_installed",
            False,
            raising=False,
        )


def test_daily_pnl_fallback_uses_only_current_trading_day_rows():
    core = Core()
    execution_truth_recovery._install_daily_closed_pnl_fallback(core, Analytics)

    value, message = core.get_daily_closed_pnl("2026-08-07")

    assert value == -1.5
    assert "canonical Bybit closed-PnL fallback" in message


def test_daily_pnl_primary_source_is_preserved():
    core = Core()
    core.get_daily_closed_pnl = lambda _trading_date: (3.25, "primary")
    execution_truth_recovery._install_daily_closed_pnl_fallback(core, Analytics)

    value, message = core.get_daily_closed_pnl("2026-08-07")

    assert value == 3.25
    assert message == "primary"
