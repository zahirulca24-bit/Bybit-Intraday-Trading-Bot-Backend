"""Historical Bybit kline collection for replay-only market data.

This module is isolated from the authenticated Demo execution client. It can
issue anonymous GET requests only to the immutable Bybit main public market
origin and the allowlisted V5 kline path, then persist validated closed candles
through the replay PostgreSQL storage interface.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

try:
    from . import replay_safety
    from .replay_storage import ReplayStorageValidationError, normalize_replay_candle
except ImportError:
    import replay_safety
    from replay_storage import ReplayStorageValidationError, normalize_replay_candle


PUBLIC_MARKET_ORIGIN = "https://api.bybit.com"
KLINE_PATH = "/v5/market/kline"
KLINE_CATEGORY = "linear"
KLINE_PAGE_LIMIT = 1000
MAX_REPLAY_CANDLES = 200_000
CACHE_READ_CHUNK = 10_000
INTERVAL_MS = {"5": 300_000, "15": 900_000, "60": 3_600_000}
_RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
_RETRYABLE_RETCODES = frozenset({10000, 10006})


class ReplayCollectorError(RuntimeError):
    """Base failure raised by the historical data collector."""


class ReplayCollectorValidationError(ReplayCollectorError):
    """Raised when a historical data request violates the locked contract."""


class ReplayCollectorTransportError(ReplayCollectorError):
    """Raised when Bybit public market data cannot be fetched safely."""


class ReplayCollectorStoreError(ReplayCollectorError):
    """Raised when the PostgreSQL replay cache is unavailable."""


class ReplayCollectorBusyError(ReplayCollectorError):
    """Raised when another collector sync is already active in this process."""


def _integer_ms(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplayCollectorValidationError(
            f"{field} must be an integer timestamp in milliseconds."
        ) from exc
    if result < 1_000_000_000_000:
        raise ReplayCollectorValidationError(
            f"{field} must be a Unix timestamp in milliseconds."
        )
    return result


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,24}USDT", symbol):
        raise ReplayCollectorValidationError(
            "Replay symbol must be an uppercase-compatible USDT contract code."
        )
    return symbol


def normalize_timeframe(value: Any) -> str:
    timeframe = str(value or "").strip()
    if timeframe not in INTERVAL_MS:
        raise ReplayCollectorValidationError(
            "Replay timeframe must be one of: 5, 15, 60."
        )
    return timeframe


def _boolean(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ReplayCollectorValidationError("force must be a boolean.")


def normalize_sync_request(
    payload: Mapping[str, Any], now_ms: int | None = None
) -> dict[str, Any]:
    """Validate and align a replay data range to fully closed candle boundaries."""

    if not isinstance(payload, Mapping):
        raise ReplayCollectorValidationError("Replay data request must be an object.")
    symbol = normalize_symbol(payload.get("symbol"))
    timeframe = normalize_timeframe(payload.get("timeframe") or payload.get("interval"))
    interval_ms = INTERVAL_MS[timeframe]
    requested_start = _integer_ms(
        payload.get("start_time", payload.get("startTime")), "startTime"
    )
    requested_end = _integer_ms(
        payload.get("end_time", payload.get("endTime")), "endTime"
    )
    if requested_end < requested_start:
        raise ReplayCollectorValidationError("endTime cannot precede startTime.")

    current_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    last_closed_open = (current_ms // interval_ms) * interval_ms - interval_ms
    aligned_start = ((requested_start + interval_ms - 1) // interval_ms) * interval_ms
    aligned_requested_end = (requested_end // interval_ms) * interval_ms
    aligned_end = min(aligned_requested_end, last_closed_open)
    if aligned_end < aligned_start:
        raise ReplayCollectorValidationError(
            "The selected range does not contain a fully closed replay candle."
        )

    expected_candles = ((aligned_end - aligned_start) // interval_ms) + 1
    if expected_candles > MAX_REPLAY_CANDLES:
        raise ReplayCollectorValidationError(
            f"Replay data request exceeds the {MAX_REPLAY_CANDLES} candle safety limit."
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "intervalMs": interval_ms,
        "requestedStartTime": requested_start,
        "requestedEndTime": requested_end,
        "startTime": aligned_start,
        "endTime": aligned_end,
        "lastClosedOpenTime": last_closed_open,
        "expectedCandles": expected_candles,
        "force": _boolean(payload.get("force"), False),
    }


class PublicBybitKlineClient:
    """Anonymous immutable-origin transport for the single allowlisted kline path."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 15.0,
        max_retries: int = 3,
        retry_base_seconds: float = 0.4,
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.timeout_seconds = max(1.0, min(60.0, float(timeout_seconds)))
        self.max_retries = max(0, min(6, int(max_retries)))
        self.retry_base_seconds = max(0.0, min(5.0, float(retry_base_seconds)))
        self._opener = opener or urllib.request.urlopen
        self._sleep = sleeper

    def _delay(self, attempt: int) -> None:
        delay = self.retry_base_seconds * (2**attempt)
        if delay > 0:
            self._sleep(min(delay, 8.0))

    def get_kline(self, params: Mapping[str, Any]) -> dict[str, Any]:
        replay_safety.assert_public_market_request("GET", KLINE_PATH)
        allowed_keys = {"category", "symbol", "interval", "start", "end", "limit"}
        unknown = set(params) - allowed_keys
        if unknown:
            raise ReplayCollectorValidationError(
                f"Unsupported Bybit kline query field(s): {', '.join(sorted(unknown))}"
            )
        query = urllib.parse.urlencode({key: params[key] for key in sorted(params)})
        request = urllib.request.Request(
            f"{PUBLIC_MARKET_ORIGIN}{KLINE_PATH}?{query}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Bybit-Intraday-Replay-Collector/1.0",
            },
            method="GET",
        )

        for attempt in range(self.max_retries + 1):
            try:
                with self._opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in _RETRYABLE_HTTP_STATUS and attempt < self.max_retries:
                    self._delay(attempt)
                    continue
                raise ReplayCollectorTransportError(
                    f"Bybit public kline HTTP request failed with status {exc.code}."
                ) from exc
            except (
                urllib.error.URLError,
                TimeoutError,
                OSError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
                if attempt < self.max_retries:
                    self._delay(attempt)
                    continue
                raise ReplayCollectorTransportError(
                    "Bybit public kline response was unavailable or invalid."
                ) from exc

            if not isinstance(payload, dict):
                raise ReplayCollectorTransportError(
                    "Bybit public kline response was not an object."
                )
            try:
                ret_code = int(payload.get("retCode", -1))
            except (TypeError, ValueError):
                ret_code = -1
            if ret_code == 0:
                return payload
            if ret_code in _RETRYABLE_RETCODES and attempt < self.max_retries:
                self._delay(attempt)
                continue
            message = str(payload.get("retMsg") or "Unknown Bybit public API error")
            raise ReplayCollectorTransportError(
                f"Bybit public kline request failed: retCode={ret_code}, {message[:160]}"
            )

        raise ReplayCollectorTransportError(
            "Bybit public kline retry policy was exhausted."
        )


def _ret_code(payload: Mapping[str, Any]) -> int:
    try:
        return int(payload.get("retCode", -1))
    except (TypeError, ValueError):
        return -1


def parse_kline_page(
    payload: Mapping[str, Any], request: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """Parse one reverse-ordered Bybit page into validated ascending closed candles."""

    if not isinstance(payload, Mapping):
        raise ReplayCollectorTransportError("Bybit public kline response was invalid.")
    ret_code = _ret_code(payload)
    if ret_code != 0:
        message = str(payload.get("retMsg") or "Unknown Bybit public API error")
        raise ReplayCollectorTransportError(
            f"Bybit public kline request failed: retCode={ret_code}, {message[:160]}"
        )
    result = payload.get("result")
    raw_rows = result.get("list") if isinstance(result, Mapping) else None
    if raw_rows is None:
        raise ReplayCollectorTransportError(
            "Bybit public kline result list is missing."
        )
    if not isinstance(raw_rows, list):
        raise ReplayCollectorTransportError(
            "Bybit public kline result list is invalid."
        )

    start_time = int(request["startTime"])
    end_time = int(request["endTime"])
    interval_ms = int(request["intervalMs"])
    last_closed_open = int(request["lastClosedOpenTime"])
    symbol = str(request["symbol"])
    timeframe = str(request["timeframe"])
    rows_by_time: dict[int, dict[str, Any]] = {}

    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 7:
            raise ReplayCollectorTransportError(
                "Bybit returned a malformed kline row."
            )
        try:
            open_time = int(raw[0])
        except (TypeError, ValueError) as exc:
            raise ReplayCollectorTransportError(
                "Bybit returned an invalid kline timestamp."
            ) from exc
        if open_time < start_time or open_time > end_time or open_time > last_closed_open:
            continue
        if open_time % interval_ms != 0:
            raise ReplayCollectorTransportError(
                "Bybit returned a misaligned kline timestamp."
            )
        try:
            candle = normalize_replay_candle(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "open_time": open_time,
                    "open": raw[1],
                    "high": raw[2],
                    "low": raw[3],
                    "close": raw[4],
                    "volume": raw[5],
                    "turnover": raw[6],
                    "source": "bybit_main_public_kline",
                }
            )
        except ReplayStorageValidationError as exc:
            raise ReplayCollectorTransportError(
                "Bybit returned invalid OHLCV candle data."
            ) from exc
        rows_by_time[open_time] = candle

    return [rows_by_time[key] for key in sorted(rows_by_time)], len(raw_rows)


def _require_store(store: Any) -> Any:
    required = (
        "upsert_replay_candles",
        "replay_candle_coverage",
        "get_replay_candles",
    )
    if any(not callable(getattr(store, name, None)) for name in required):
        raise ReplayCollectorStoreError(
            "Persistent PostgreSQL replay storage is unavailable."
        )
    status = getattr(store, "status", None)
    if callable(status):
        snapshot = dict(status() or {})
        if not snapshot.get("ok") or snapshot.get("degraded"):
            raise ReplayCollectorStoreError(
                snapshot.get("error")
                or "Persistent PostgreSQL replay storage is degraded."
            )
    return store


def cached_range_status(store: Any, request: Mapping[str, Any]) -> dict[str, Any]:
    """Verify exact candle continuity in bounded database reads."""

    store = _require_store(store)
    symbol = str(request["symbol"])
    timeframe = str(request["timeframe"])
    interval_ms = int(request["intervalMs"])
    start_time = int(request["startTime"])
    end_time = int(request["endTime"])
    expected_total = int(request["expectedCandles"])
    cached_total = 0
    missing_total = 0
    cursor = start_time

    while cursor <= end_time:
        chunk_end = min(
            end_time, cursor + ((CACHE_READ_CHUNK - 1) * interval_ms)
        )
        expected_times = set(range(cursor, chunk_end + 1, interval_ms))
        rows = store.get_replay_candles(
            symbol,
            timeframe,
            cursor,
            chunk_end,
            limit=CACHE_READ_CHUNK,
        )
        actual_times = {
            int(row.get("openTime"))
            for row in rows
            if isinstance(row, Mapping) and row.get("openTime") is not None
        }
        cached_total += len(actual_times & expected_times)
        missing_total += len(expected_times - actual_times)
        cursor = chunk_end + interval_ms

    return {
        "complete": missing_total == 0 and cached_total == expected_total,
        "expectedCandles": expected_total,
        "cachedCandles": cached_total,
        "missingCandles": missing_total,
        "startTime": start_time,
        "endTime": end_time,
    }


class HistoricalKlineCollector:
    """Paginate, validate, and cache closed Bybit linear USDT klines."""

    def __init__(
        self,
        store: Any,
        *,
        transport: Any | None = None,
        page_limit: int = KLINE_PAGE_LIMIT,
        max_pages: int = 500,
        min_request_interval_seconds: float = 0.10,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        now_ms: Callable[[], int] | None = None,
    ):
        self.store = store
        self.transport = transport or PublicBybitKlineClient()
        self.page_limit = max(1, min(KLINE_PAGE_LIMIT, int(page_limit)))
        self.max_pages = max(1, min(1000, int(max_pages)))
        self.min_request_interval_seconds = max(
            0.0, min(2.0, float(min_request_interval_seconds))
        )
        self._sleep = sleeper
        self._monotonic = monotonic
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sync_lock = threading.Lock()

    def _throttle(self, last_request_at: float | None) -> float:
        if last_request_at is not None and self.min_request_interval_seconds > 0:
            elapsed = self._monotonic() - last_request_at
            remaining = self.min_request_interval_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
        return self._monotonic()

    def _run_pages(
        self,
        request: Mapping[str, Any],
        on_page: Callable[[list[dict[str, Any]]], None],
    ) -> dict[str, Any]:
        cursor_end = int(request["endTime"])
        start_time = int(request["startTime"])
        interval_ms = int(request["intervalMs"])
        seen: set[int] = set()
        pages = fetched = duplicates = raw_rows_total = 0
        last_request_at: float | None = None

        while cursor_end >= start_time:
            if pages >= self.max_pages:
                raise ReplayCollectorTransportError(
                    "Historical kline pagination exceeded the configured page safety limit."
                )
            last_request_at = self._throttle(last_request_at)
            payload = self.transport.get_kline(
                {
                    "category": KLINE_CATEGORY,
                    "symbol": request["symbol"],
                    "interval": request["timeframe"],
                    "start": start_time,
                    "end": cursor_end,
                    "limit": self.page_limit,
                }
            )
            rows, raw_count = parse_kline_page(
                payload, {**request, "endTime": cursor_end}
            )
            pages += 1
            raw_rows_total += raw_count
            if raw_count == 0:
                break

            fresh_rows = []
            for row in rows:
                open_time = int(row["open_time"])
                if open_time in seen:
                    duplicates += 1
                    continue
                seen.add(open_time)
                fresh_rows.append(row)
            if fresh_rows:
                on_page(fresh_rows)
                fetched += len(fresh_rows)

            raw_list = ((payload.get("result") or {}).get("list") or [])
            try:
                raw_times = [
                    int(item[0])
                    for item in raw_list
                    if isinstance(item, (list, tuple)) and item
                ]
            except (TypeError, ValueError) as exc:
                raise ReplayCollectorTransportError(
                    "Bybit returned an invalid pagination timestamp."
                ) from exc
            if not raw_times:
                break
            oldest = min(raw_times)
            if oldest <= start_time or raw_count < self.page_limit:
                break
            next_end = oldest - interval_ms
            if next_end >= cursor_end:
                raise ReplayCollectorTransportError(
                    "Historical kline pagination made no backward progress."
                )
            cursor_end = next_end

        return {
            "pages": pages,
            "fetchedCandles": fetched,
            "duplicatesDropped": duplicates,
            "rawRows": raw_rows_total,
        }

    def collect(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Download a bounded range without writing it; intended for verification."""

        request = normalize_sync_request(payload, now_ms=self._now_ms())
        candles: list[dict[str, Any]] = []
        stats = self._run_pages(request, candles.extend)
        candles.sort(key=lambda row: int(row["open_time"]))
        return {"ok": True, "request": request, **stats, "candles": candles}

    def coverage(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = normalize_sync_request(payload, now_ms=self._now_ms())
        store = _require_store(self.store)
        return {
            "ok": True,
            "request": request,
            "coverage": store.replay_candle_coverage(
                request["symbol"], request["timeframe"]
            ),
            "range": cached_range_status(store, request),
        }

    def sync(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Synchronously fetch and idempotently cache one validated candle range."""

        request = normalize_sync_request(payload, now_ms=self._now_ms())
        store = _require_store(self.store)
        if not self._sync_lock.acquire(blocking=False):
            raise ReplayCollectorBusyError(
                "Another historical replay data sync is already running."
            )
        try:
            before = cached_range_status(store, request)
            if before["complete"] and not request["force"]:
                return {
                    "ok": True,
                    "source": "postgresql_cache",
                    "request": request,
                    "pages": 0,
                    "fetchedCandles": 0,
                    "storedCandles": 0,
                    "duplicatesDropped": 0,
                    "range": before,
                    "coverage": store.replay_candle_coverage(
                        request["symbol"], request["timeframe"]
                    ),
                }

            stored = 0

            def persist(rows: list[dict[str, Any]]) -> None:
                nonlocal stored
                stored += int(store.upsert_replay_candles(rows))

            stats = self._run_pages(request, persist)
            after = cached_range_status(store, request)
            return {
                "ok": True,
                "source": "bybit_main_public_kline",
                "request": request,
                **stats,
                "storedCandles": stored,
                "range": after,
                "coverage": store.replay_candle_coverage(
                    request["symbol"], request["timeframe"]
                ),
            }
        finally:
            self._sync_lock.release()


def install(core: Any, transport: Any | None = None) -> HistoricalKlineCollector:
    existing = getattr(core, "_replay_historical_collector", None)
    if isinstance(existing, HistoricalKlineCollector):
        return existing
    collector = HistoricalKlineCollector(
        getattr(core, "_durable_state_store", None), transport=transport
    )
    core._replay_historical_collector = collector
    return collector


def _collector(core: Any) -> HistoricalKlineCollector:
    current = getattr(core, "_replay_historical_collector", None)
    if not isinstance(current, HistoricalKlineCollector):
        current = install(core)
    return current


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    if isinstance(exc, ReplayCollectorValidationError):
        status, code = 400, "REPLAY_DATA_INVALID"
    elif isinstance(exc, ReplayCollectorBusyError):
        status, code = 409, "REPLAY_DATA_SYNC_BUSY"
    elif isinstance(exc, ReplayCollectorStoreError):
        status, code = 503, "REPLAY_STORAGE_UNAVAILABLE"
    elif isinstance(exc, ReplayCollectorTransportError):
        status, code = 502, "BYBIT_PUBLIC_DATA_UNAVAILABLE"
    else:
        status, code = 500, "REPLAY_DATA_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Historical replay data operation failed."
    core.json_response(
        handler,
        status,
        {"ok": False, "code": code, "error": message},
    )


def handle_get(handler: Any, core: Any, path: str) -> bool:
    if path != "/api/replay/data/coverage":
        return False
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    try:
        result = _collector(core).coverage(query)
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True


def handle_post(
    handler: Any, core: Any, path: str, payload: Mapping[str, Any]
) -> bool:
    if path != "/api/replay/data/sync":
        return False
    try:
        result = _collector(core).sync(payload)
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
