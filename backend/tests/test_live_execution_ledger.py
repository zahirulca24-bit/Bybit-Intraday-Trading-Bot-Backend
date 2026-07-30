from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from backend import live_execution_ledger as ledger


def execution(exec_id, time_ms, symbol, side, qty, price, closed="0", fee="0.1", **extra):
    row = {
        "execId": exec_id,
        "execTime": str(time_ms),
        "symbol": symbol,
        "orderId": f"order-{exec_id}",
        "orderLinkId": f"link-{exec_id}",
        "side": side,
        "execQty": str(qty),
        "execPrice": str(price),
        "execFee": str(fee),
        "closedSize": str(closed),
        "execType": "Trade",
        **extra,
    }
    return ledger.normalize_execution(row, "2026-07-29")


def test_ena_partial_close_reconstructs_true_position_path():
    rows = [
        execution("ena-open", 1_753_803_001_000, "ENAUSDT", "Sell", "11134", "0.08096"),
        execution("ena-close", 1_753_810_926_000, "ENAUSDT", "Buy", "4453", "0.07975", "4453"),
    ]
    result = ledger.reconcile_executions(rows, {"ENAUSDT": Decimal("-6681")})
    assert [row["action"] for row in result] == ["ENTRY", "PARTIAL_EXIT"]
    assert result[0]["positionBefore"] == Decimal("0")
    assert result[0]["positionAfter"] == Decimal("-11134")
    assert result[1]["positionBefore"] == Decimal("-11134")
    assert result[1]["positionAfter"] == Decimal("-6681")
    assert result[1]["closedSize"] == Decimal("4453")


def test_daily_counters_separate_fill_rows_from_completed_trades():
    rows = [
        execution("a", 1000, "ENAUSDT", "Sell", "11134", "0.08096"),
        execution("b", 2000, "ENAUSDT", "Buy", "4453", "0.07975", "4453"),
        execution("c", 3000, "SHIB1000USDT", "Sell", "53670", "0.004658"),
        execution("d", 4000, "SHIB1000USDT", "Buy", "53670", "0.004669", "53670"),
    ]
    positions = {"ENAUSDT": Decimal("-6681"), "SHIB1000USDT": Decimal("0")}
    reconciled = ledger.reconcile_executions(rows, positions)
    summary = ledger.summarize_rows(reconciled, trading_date="2026-07-29", current_positions=positions)
    assert summary["totalExecutions"] == 4
    assert summary["entryExecutions"] == 2
    assert summary["exitExecutions"] == 2
    assert summary["partialCloseExecutions"] == 1
    assert summary["completedTrades"] == 1
    assert summary["openPositions"] == 1
    assert summary["feesPaid"] == "0.4"


def test_reversal_counts_as_exit_and_new_entry():
    rows = [
        execution("open", 1000, "BTCUSDT", "Buy", "2", "100"),
        execution("reverse", 2000, "BTCUSDT", "Sell", "3", "99", "2"),
    ]
    reconciled = ledger.reconcile_executions(rows, {"BTCUSDT": Decimal("-1")})
    assert [row["action"] for row in reconciled] == ["ENTRY", "REVERSAL"]
    summary = ledger.summarize_rows(reconciled, trading_date="2026-07-29", current_positions={"BTCUSDT": Decimal("-1")})
    assert summary["entryExecutions"] == 2
    assert summary["exitExecutions"] == 1
    assert summary["completedTrades"] == 1


def test_funding_and_non_usdt_rows_do_not_enter_trade_counters():
    assert ledger.normalize_execution(
        {"execId": "fund", "execTime": "1000", "symbol": "ENAUSDT", "side": "Sell", "execQty": "1", "execType": "Funding"},
        "2026-07-29",
    ) is None
    assert execution("usdc", 1000, "BTCPERP", "Buy", "1", "100") is None


def test_boolean_and_fee_rebate_semantics_are_truthful():
    maker = execution("maker", 1000, "BTCUSDT", "Buy", "1", "100", fee="-0.02", isMaker="false")
    taker = execution("taker", 2000, "BTCUSDT", "Sell", "1", "101", closed="1", fee="0.06", isMaker=True)
    rows = ledger.reconcile_executions([maker, taker], {"BTCUSDT": Decimal("0")})
    summary = ledger.summarize_rows(rows, trading_date="2026-07-29", current_positions={})
    assert maker["isMaker"] is False
    assert taker["isMaker"] is True
    assert summary["feesPaid"] == "0.06"
    assert summary["feeRebates"] == "0.02"
    assert summary["netTradingFees"] == "0.04"


def test_hedge_mode_fails_closed():
    with pytest.raises(ledger.LiveExecutionValidationError):
        ledger.current_signed_positions([{"symbol": "BTCUSDT", "side": "Buy", "size": "1", "positionIdx": 1}])


class FakeCore:
    def __init__(self):
        self.calls = []
        self.pages = 0

    def bybit_request(self, method, path, params):
        self.calls.append((method, path, dict(params)))
        assert path == ledger.EXECUTION_LIST_PATH
        self.pages += 1
        if self.pages == 1:
            return {"retCode": 0, "result": {"list": [{"execId": "one", "execTime": "1000", "symbol": "BTCUSDT", "side": "Buy", "execQty": "1", "execPrice": "100", "execFee": "0.1", "execType": "Trade"}], "nextPageCursor": "next"}}
        return {"retCode": 0, "result": {"list": [{"execId": "one", "execTime": "1000", "symbol": "BTCUSDT", "side": "Buy", "execQty": "1", "execPrice": "100", "execFee": "0.1", "execType": "Trade"}, {"execId": "two", "execTime": "2000", "symbol": "BTCUSDT", "side": "Sell", "execQty": "1", "closedSize": "1", "execPrice": "101", "execFee": "0.1", "execType": "Trade"}], "nextPageCursor": ""}}


def test_execution_pagination_is_deduplicated_and_read_only():
    core = FakeCore()
    service = ledger.LiveExecutionLedgerService(core, SimpleNamespace())
    rows = service._fetch_execution_pages(0, 3000, "2026-07-29")
    assert {row["execId"] for row in rows} == {"one", "two"}
    assert all(method == "GET" for method, _, _ in core.calls)


def test_cursor_round_trip_and_validation():
    cursor = ledger._encode_cursor(1234, "exec-x")
    assert ledger._decode_cursor(cursor) == (1234, "exec-x")


def test_flat_hedge_mode_slot_fails_closed_before_size_filter():
    with pytest.raises(ledger.LiveExecutionValidationError):
        ledger.current_signed_positions(
            [{"symbol": "ETHUSDT", "side": "", "size": "0", "positionIdx": 2}]
        )


def test_position_snapshot_follows_all_pages_and_uses_second_page_anchor():
    class PositionCore:
        def __init__(self):
            self.calls = []

        def bybit_request(self, method, path, params):
            self.calls.append((method, path, dict(params)))
            assert method == "GET"
            assert path == ledger.POSITION_LIST_PATH
            if not params.get("cursor"):
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"symbol": "BTCUSDT", "side": "", "size": "0", "positionIdx": 0}],
                        "nextPageCursor": "page-2",
                    },
                }
            return {
                "retCode": 0,
                "result": {
                    "list": [{"symbol": "ENAUSDT", "side": "Sell", "size": "6681", "positionIdx": 0}],
                    "nextPageCursor": "",
                },
            }

    core = PositionCore()
    service = ledger.LiveExecutionLedgerService(core, SimpleNamespace())
    assert service._fetch_positions() == {"ENAUSDT": Decimal("-6681")}
    assert len(core.calls) == 2
    assert core.calls[1][2]["cursor"] == "page-2"


def test_previous_day_backfill_anchors_to_current_day_opening_position():
    class Store:
        def __init__(self):
            import threading

            self.lock = threading.RLock()
            self.values = {}

        def put(self, key, value):
            self.values[key] = value

        def get(self, key, default=None):
            return self.values.get(key, default)

    class Core:
        BOT_LOCK = None
        BOT_STATE = {}

        @staticmethod
        def get_current_trading_date_key():
            return "2026-07-29"

        @staticmethod
        def get_configured_timezone():
            return "Asia/Dhaka"

        @staticmethod
        def get_trading_day_start_epoch(date_key):
            from datetime import datetime
            from zoneinfo import ZoneInfo

            return int(datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Dhaka")).timestamp())

    class Service(ledger.LiveExecutionLedgerService):
        def ensure_schema(self):
            return None

        def _consistent_current_snapshot(self, trading_date):
            # A carried ENA short has no current-day execution, so the current
            # position is also the prior day's closing anchor.
            return [], {"ENAUSDT": Decimal("-6681")}, 1_785_342_600_000, 1, 2

        def _fetch_execution_pages(self, start_ms, end_ms, trading_date):
            assert trading_date == "2026-07-28"
            return [
                execution("prev-open", 1_785_246_201_000, "ENAUSDT", "Sell", "11134", "0.08096"),
                execution("prev-close", 1_785_252_926_000, "ENAUSDT", "Buy", "4453", "0.07975", "4453"),
            ]

        def _persist(self, rows, synced_at_ms):
            self.persisted = list(rows)

        def _rows_for_date(self, trading_date):
            return list(self.persisted)

    service = Service(Core(), Store(), now_ms=lambda: 1_785_342_600_000)
    result = service.sync("2026-07-28")
    assert result["backfill"] is True
    assert result["anchorMethod"] == "current_day_opening_position_reconstruction"
    assert result["entryExecutions"] == 1
    assert result["partialCloseExecutions"] == 1
    assert service.persisted[-1]["positionAfter"] == Decimal("-6681")
