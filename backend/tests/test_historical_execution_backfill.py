from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from backend import historical_execution_backfill as backfill


class FakeStore:
    def __init__(self):
        self.values = {}

    def put(self, key, value):
        self.values[key] = value


class FakeService:
    def __init__(self):
        self.store = FakeStore()
        self._now_ms = lambda: int(datetime(2026, 8, 6, 10, tzinfo=timezone.utc).timestamp() * 1000)
        self.persisted = []
        self.rows = {}

    def ensure_schema(self):
        return None

    def _fetch_execution_pages(self, start_ms, end_ms, trading_date):
        if trading_date == "2026-08-02":
            return [{
                "execId": "open-1", "tradingDate": trading_date, "execTime": start_ms + 1,
                "sequenceNo": 1, "symbol": "SHIB1000USDT", "orderId": "order-open",
                "orderLinkId": None, "side": "Buy", "execType": "Trade",
                "execQty": Decimal("136700"), "execPrice": Decimal("0.004979"),
                "execFee": Decimal("0.37434612"), "feeCurrency": "USDT",
                "leavesQty": Decimal("0"), "apiClosedSize": Decimal("0"),
                "isMaker": False, "raw": {},
            }]
        if trading_date == "2026-08-03":
            return [{
                "execId": "close-1", "tradingDate": trading_date, "execTime": start_ms + 1,
                "sequenceNo": 2, "symbol": "SHIB1000USDT", "orderId": "order-close",
                "orderLinkId": None, "side": "Sell", "execType": "Trade",
                "execQty": Decimal("136700"), "execPrice": Decimal("0.004928"),
                "execFee": Decimal("0.37051168"), "feeCurrency": "USDT",
                "leavesQty": Decimal("0"), "apiClosedSize": Decimal("136700"),
                "isMaker": False, "raw": {},
            }]
        return []

    def _fetch_positions(self):
        return {}

    def _persist(self, rows, synced_at_ms):
        self.persisted = list(rows)
        self.rows = {}
        for row in rows:
            self.rows.setdefault(row["tradingDate"], []).append(dict(row))

    def _rows_for_date(self, trading_date):
        return list(self.rows.get(trading_date, []))


class FakeCore:
    def __init__(self, service):
        self._live_execution_ledger_service = service

    def get_current_trading_date_key(self):
        return "2026-08-06"

    def get_trading_day_start_epoch(self, date_key):
        return int(datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())

    def get_configured_timezone(self):
        return "UTC"


def test_date_keys_are_bounded_and_oldest_first():
    assert backfill._date_keys("2026-08-06", 5) == [
        "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"
    ]
    assert backfill._bounded_days(30) == 30


def test_cross_day_open_and_close_are_reconciled(monkeypatch):
    service = FakeService()
    core = FakeCore(service)
    monkeypatch.setattr(backfill.live_execution_ledger, "_service", lambda supplied_core: service)

    result = backfill.run(core, days=5)

    assert result["ok"] is True
    assert result["dateFrom"] == "2026-08-02"
    assert result["dateTo"] == "2026-08-06"
    assert result["rowsFetched"] == 2
    assert result["rowsPersisted"] == 2
    assert [row["action"] for row in service.persisted] == ["ENTRY", "FULL_EXIT"]
    assert service.store.values["live_execution_truth:2026-08-02"]["entryExecutions"] == 1
    assert service.store.values["live_execution_truth:2026-08-03"]["completedTrades"] == 1


def test_backfill_rejects_more_than_thirty_days():
    try:
        backfill._bounded_days(31)
    except backfill.live_execution_ledger.LiveExecutionValidationError as exc:
        assert "between 1 and 30" in str(exc)
    else:
        raise AssertionError("31-day backfill must be rejected")
