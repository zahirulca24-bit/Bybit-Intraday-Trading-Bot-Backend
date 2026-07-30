from __future__ import annotations

import json
from decimal import Decimal

import pytest

from backend.replay_collector import (
    INTERVAL_MS,
    KLINE_PATH,
    MAX_REPLAY_CANDLES,
    PUBLIC_MARKET_ORIGIN,
    HistoricalKlineCollector,
    PublicBybitKlineClient,
    ReplayCollectorTransportError,
    ReplayCollectorValidationError,
    cached_range_status,
    normalize_sync_request,
    parse_kline_page,
)


BASE = 1_800_000_000_000


def _raw_candle(open_time: int, close: str = "101") -> list[str]:
    close_value = Decimal(close)
    high = max(Decimal("102"), close_value + Decimal("1"))
    low = min(Decimal("99"), close_value - Decimal("1"))
    return [
        str(open_time),
        "100",
        str(high),
        str(low),
        str(close_value),
        "25",
        "2510",
    ]


class FakeTransport:
    def __init__(self, rows: list[list[str]], ret_code: int = 0):
        self.rows = rows
        self.ret_code = ret_code
        self.calls: list[dict] = []

    def get_kline(self, params):
        params = dict(params)
        self.calls.append(params)
        if self.ret_code != 0:
            return {
                "retCode": self.ret_code,
                "retMsg": "failure",
                "result": {"list": []},
            }
        selected = [
            row
            for row in self.rows
            if int(params["start"]) <= int(row[0]) <= int(params["end"])
        ]
        selected.sort(key=lambda row: int(row[0]), reverse=True)
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {"list": selected[: int(params["limit"])]},
        }


class FakeStore:
    def __init__(self):
        self.rows: dict[tuple[str, str, int], dict] = {}
        self.upsert_calls = 0

    def status(self):
        return {"ok": True, "degraded": False, "backend": "fake-postgresql"}

    def upsert_replay_candles(self, candles):
        candles = list(candles)
        self.upsert_calls += 1
        for row in candles:
            key = (row["symbol"], row["timeframe"], int(row["open_time"]))
            self.rows[key] = dict(row)
        return len(candles)

    def replay_candle_coverage(self, symbol, timeframe):
        times = sorted(
            key[2]
            for key in self.rows
            if key[0] == symbol and key[1] == timeframe
        )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "count": len(times),
            "firstOpenTime": times[0] if times else None,
            "lastOpenTime": times[-1] if times else None,
        }

    def get_replay_candles(
        self, symbol, timeframe, start_time, end_time, limit=5000
    ):
        rows = [
            row
            for (row_symbol, row_timeframe, open_time), row in self.rows.items()
            if row_symbol == symbol
            and row_timeframe == timeframe
            and int(start_time) <= open_time <= int(end_time)
        ]
        rows.sort(key=lambda row: int(row["open_time"]))
        return [
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "openTime": int(row["open_time"]),
                "open": str(row["open_price"]),
                "high": str(row["high_price"]),
                "low": str(row["low_price"]),
                "close": str(row["close_price"]),
                "volume": str(row["volume"]),
                "turnover": (
                    str(row["turnover"])
                    if row["turnover"] is not None
                    else None
                ),
                "source": row["source"],
            }
            for row in rows[: int(limit)]
        ]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_sync_request_aligns_range_and_excludes_current_open_candle():
    interval = INTERVAL_MS["5"]
    now_ms = BASE + (10 * interval) + 12_345
    request = normalize_sync_request(
        {
            "symbol": "btcusdt",
            "timeframe": "5",
            "startTime": BASE + 1,
            "endTime": now_ms + interval,
        },
        now_ms=now_ms,
    )

    assert request["symbol"] == "BTCUSDT"
    assert request["startTime"] == BASE + interval
    assert request["endTime"] == BASE + (9 * interval)
    assert request["lastClosedOpenTime"] == BASE + (9 * interval)
    assert request["expectedCandles"] == 9


def test_sync_request_rejects_seconds_invalid_interval_and_oversized_range():
    with pytest.raises(ReplayCollectorValidationError, match="milliseconds"):
        normalize_sync_request(
            {
                "symbol": "BTCUSDT",
                "timeframe": "5",
                "startTime": 1_800_000_000,
                "endTime": BASE,
            },
            now_ms=BASE + INTERVAL_MS["5"],
        )

    with pytest.raises(ReplayCollectorValidationError, match="timeframe"):
        normalize_sync_request(
            {
                "symbol": "BTCUSDT",
                "timeframe": "1",
                "startTime": BASE,
                "endTime": BASE,
            },
            now_ms=BASE + INTERVAL_MS["5"],
        )

    interval = INTERVAL_MS["5"]
    end_time = BASE + (MAX_REPLAY_CANDLES * interval)
    with pytest.raises(ReplayCollectorValidationError, match="safety limit"):
        normalize_sync_request(
            {
                "symbol": "BTCUSDT",
                "timeframe": "5",
                "startTime": BASE,
                "endTime": end_time,
            },
            now_ms=end_time + interval,
        )


def test_public_client_uses_immutable_origin_allowlisted_path_and_no_auth_headers():
    captured = {}

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = {
            key.lower(): value for key, value in request.header_items()
        }
        captured["timeout"] = timeout
        return FakeResponse(
            {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
        )

    client = PublicBybitKlineClient(opener=opener, sleeper=lambda _: None)
    result = client.get_kline(
        {
            "category": "linear",
            "symbol": "BTCUSDT",
            "interval": "5",
            "start": BASE,
            "end": BASE + INTERVAL_MS["5"],
            "limit": 1000,
        }
    )

    assert result["retCode"] == 0
    assert captured["url"].startswith(
        f"{PUBLIC_MARKET_ORIGIN}{KLINE_PATH}?"
    )
    assert "x-bapi-api-key" not in captured["headers"]
    assert "authorization" not in captured["headers"]
    assert captured["timeout"] == 15.0


def test_kline_page_is_sorted_validated_and_deduplicated():
    interval = INTERVAL_MS["5"]
    request = normalize_sync_request(
        {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "startTime": BASE,
            "endTime": BASE + (2 * interval),
        },
        now_ms=BASE + (4 * interval),
    )
    payload = {
        "retCode": 0,
        "result": {
            "list": [
                _raw_candle(BASE + (2 * interval), "103"),
                _raw_candle(BASE + interval, "102"),
                _raw_candle(BASE + interval, "102"),
                _raw_candle(BASE, "101"),
            ]
        },
    }

    rows, raw_count = parse_kline_page(payload, request)

    assert raw_count == 4
    assert [row["open_time"] for row in rows] == [
        BASE,
        BASE + interval,
        BASE + (2 * interval),
    ]
    assert rows[-1]["close_price"] == Decimal("103")
    assert all(
        row["source"] == "bybit_main_public_kline" for row in rows
    )


def test_kline_page_rejects_malformed_or_misaligned_rows():
    interval = INTERVAL_MS["5"]
    request = normalize_sync_request(
        {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "startTime": BASE,
            "endTime": BASE + interval,
        },
        now_ms=BASE + (3 * interval),
    )

    with pytest.raises(ReplayCollectorTransportError, match="malformed"):
        parse_kline_page(
            {"retCode": 0, "result": {"list": [[str(BASE), "1"]]}},
            request,
        )

    with pytest.raises(ReplayCollectorTransportError, match="misaligned"):
        parse_kline_page(
            {"retCode": 0, "result": {"list": [_raw_candle(BASE + 1)]}},
            request,
        )


def test_collector_paginates_backward_and_returns_ascending_unique_candles():
    interval = INTERVAL_MS["5"]
    rows = [
        _raw_candle(BASE + (index * interval), str(100 + index))
        for index in range(7)
    ]
    transport = FakeTransport(rows)
    collector = HistoricalKlineCollector(
        None,
        transport=transport,
        page_limit=3,
        min_request_interval_seconds=0,
        now_ms=lambda: BASE + (10 * interval),
    )

    result = collector.collect(
        {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "startTime": BASE,
            "endTime": BASE + (6 * interval),
        }
    )

    assert result["pages"] == 3
    assert result["fetchedCandles"] == 7
    assert [row["open_time"] for row in result["candles"]] == [
        BASE + (index * interval) for index in range(7)
    ]
    assert [call["end"] for call in transport.calls] == [
        BASE + (6 * interval),
        BASE + (3 * interval),
        BASE,
    ]


def test_sync_populates_postgres_cache_then_skips_complete_range():
    interval = INTERVAL_MS["5"]
    rows = [_raw_candle(BASE + (index * interval)) for index in range(5)]
    transport = FakeTransport(rows)
    store = FakeStore()
    collector = HistoricalKlineCollector(
        store,
        transport=transport,
        page_limit=2,
        min_request_interval_seconds=0,
        now_ms=lambda: BASE + (8 * interval),
    )
    payload = {
        "symbol": "BTCUSDT",
        "timeframe": "5",
        "startTime": BASE,
        "endTime": BASE + (4 * interval),
    }

    first = collector.sync(payload)
    calls_after_first = len(transport.calls)
    second = collector.sync(payload)

    assert first["source"] == "bybit_main_public_kline"
    assert first["range"]["complete"] is True
    assert first["storedCandles"] == 5
    assert second["source"] == "postgresql_cache"
    assert second["pages"] == 0
    assert len(transport.calls) == calls_after_first
    assert store.replay_candle_coverage("BTCUSDT", "5")["count"] == 5


def test_cached_range_status_detects_an_internal_gap():
    interval = INTERVAL_MS["5"]
    store = FakeStore()
    store.upsert_replay_candles(
        [
            {
                "symbol": "BTCUSDT",
                "timeframe": "5",
                "open_time": BASE + (index * interval),
                "open_price": Decimal("100"),
                "high_price": Decimal("102"),
                "low_price": Decimal("99"),
                "close_price": Decimal("101"),
                "volume": Decimal("25"),
                "turnover": Decimal("2510"),
                "source": "test",
            }
            for index in (0, 1, 3, 4)
        ]
    )
    request = normalize_sync_request(
        {
            "symbol": "BTCUSDT",
            "timeframe": "5",
            "startTime": BASE,
            "endTime": BASE + (4 * interval),
        },
        now_ms=BASE + (8 * interval),
    )

    status = cached_range_status(store, request)

    assert status["complete"] is False
    assert status["cachedCandles"] == 4
    assert status["missingCandles"] == 1


def test_collector_fails_closed_on_bybit_error_response():
    interval = INTERVAL_MS["5"]
    collector = HistoricalKlineCollector(
        None,
        transport=FakeTransport([], ret_code=10001),
        min_request_interval_seconds=0,
        now_ms=lambda: BASE + (3 * interval),
    )

    with pytest.raises(ReplayCollectorTransportError, match="retCode"):
        collector.collect(
            {
                "symbol": "BTCUSDT",
                "timeframe": "5",
                "startTime": BASE,
                "endTime": BASE + interval,
            }
        )
