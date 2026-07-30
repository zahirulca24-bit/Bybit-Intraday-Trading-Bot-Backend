from __future__ import annotations

import os
import threading

from backend.live_execution_ledger import LiveExecutionLedgerService
from backend.postgres_state_store import PostgresStateStore


class Core:
    def __init__(self):
        self.BOT_LOCK = threading.RLock()
        self.BOT_STATE = {}
        self.calls = []

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

    def bybit_request(self, method, path, params):
        self.calls.append((method, path))
        if path == "/v5/execution/list":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "execId": "ena-open",
                            "orderId": "order-open",
                            "execTime": "1785331801000",
                            "symbol": "ENAUSDT",
                            "side": "Sell",
                            "execQty": "11134",
                            "execPrice": "0.08096",
                            "execFee": "0.4958",
                            "closedSize": "0",
                            "execType": "Trade",
                        },
                        {
                            "execId": "ena-close",
                            "orderId": "order-close",
                            "execTime": "1785338526000",
                            "symbol": "ENAUSDT",
                            "side": "Buy",
                            "execQty": "4453",
                            "execPrice": "0.07975",
                            "execFee": "0.1953",
                            "closedSize": "4453",
                            "execType": "Trade",
                        },
                        {
                            "execId": "ena-funding",
                            "execTime": "1785340800000",
                            "symbol": "ENAUSDT",
                            "side": "Sell",
                            "execQty": "6681",
                            "execPrice": "0.07970",
                            "execFee": "-0.06115993",
                            "closedSize": "0",
                            "execType": "Funding",
                        },
                    ],
                    "nextPageCursor": "",
                },
            }
        if path == "/v5/position/list":
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": "ENAUSDT", "side": "Sell", "size": "6681", "positionIdx": 0}
                    ]
                },
            }
        raise AssertionError(path)


def main():
    assert os.environ.get("DATABASE_URL")
    store = PostgresStateStore()
    core = Core()
    service = LiveExecutionLedgerService(core, store, now_ms=lambda: 1_785_342_600_000)

    first = service.sync("2026-07-29")
    second = service.sync("2026-07-29")

    assert first["totalExecutions"] == 2
    assert first["entryExecutions"] == 1
    assert first["exitExecutions"] == 1
    assert first["partialCloseExecutions"] == 1
    assert first["completedTrades"] == 0
    assert first["openPositions"] == 1
    assert first["feesPaid"] == "0.6911"
    assert second["totalExecutions"] == 2, second
    assert second["rowsPersisted"] == 2

    page = service.list(date_value="2026-07-29", limit=10)
    assert len(page["entries"]) == 2
    assert [entry["action"] for entry in reversed(page["entries"])] == ["ENTRY", "PARTIAL_EXIT"]
    assert page["entries"][0]["positionAfter"] == "-6681"

    assert all(method == "GET" for method, _ in core.calls)
    assert {path for _, path in core.calls} == {"/v5/execution/list", "/v5/position/list"}

    with store.connect() as db:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM live_execution_ledger")
            assert cur.fetchone()[0] == 2
            cur.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=4")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT action,position_before,position_after FROM live_execution_ledger ORDER BY exec_time")
            rows = cur.fetchall()
            assert rows[0][0] == "ENTRY"
            assert str(rows[0][1]) == "0"
            assert str(rows[0][2]) == "-11134"
            assert rows[1][0] == "PARTIAL_EXIT"
            assert str(rows[1][2]) == "-6681"

    print("live execution ledger postgres verification passed")


if __name__ == "__main__":
    main()
