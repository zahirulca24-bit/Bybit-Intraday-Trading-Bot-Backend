"""Canonical read-only Bybit execution ledger and truthful daily counters.

The service synchronizes authenticated Bybit Demo execution history into
PostgreSQL. It never creates, amends, cancels, or closes an order. Entry fills,
exit fills, partial closes, completed position cycles, and reversals remain
separate so a fill count is never presented as a completed-trade count.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

try:
    from .live_execution_storage import LIVE_EXECUTION_MIGRATION
except ImportError:  # pragma: no cover - direct script import
    from live_execution_storage import LIVE_EXECUTION_MIGRATION

SCHEMA_VERSION = int(LIVE_EXECUTION_MIGRATION[0])
EXECUTION_LIST_PATH = "/v5/execution/list"
POSITION_LIST_PATH = "/v5/position/list"
SUMMARY_PATH = "/api/live-executions/summary"
LIST_PATH = "/api/live-executions"
SYNC_PATH = "/api/live-executions/sync"
BYBIT_PAGE_LIMIT = 100
POSITION_PAGE_LIMIT = 200
MAX_SYNC_PAGES = 20
MAX_POSITION_PAGES = 10
MAX_SNAPSHOT_ATTEMPTS = 3
DEFAULT_SYNC_SECONDS = 60
DEFAULT_BACKFILL_SECONDS = 300
_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
_USDT_PERPETUAL_PATTERN = re.compile(r"[A-Z0-9]+USDT")
_ACTIONS = frozenset({"ENTRY", "ADD", "PARTIAL_EXIT", "FULL_EXIT", "REVERSAL"})
_POSITION_EXEC_TYPES = frozenset(
    {"Trade", "AdlTrade", "BustTrade", "Delivery", "Settle", "BlockTrade", "MovePosition"}
)


class LiveExecutionLedgerError(RuntimeError):
    """Base error for the live execution truth service."""


class LiveExecutionValidationError(LiveExecutionLedgerError):
    """Raised when an operator request or account shape is unsupported."""


class LiveExecutionBusyError(LiveExecutionLedgerError):
    """Raised when another execution sync already owns the process lock."""


class LiveExecutionTransportError(LiveExecutionLedgerError):
    """Raised when Bybit Demo account truth cannot be read."""


class LiveExecutionStoreError(LiveExecutionLedgerError):
    """Raised when PostgreSQL execution truth cannot be persisted or read."""


def _decimal(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


def _decimal_text(value: Any) -> str:
    return format(_decimal(value), "f")


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    return None


def _sign(value: Decimal) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def normalize_date(value: Any, *, default: str) -> str:
    text = str(value or default).strip()
    if not _DATE_PATTERN.fullmatch(text):
        raise LiveExecutionValidationError("date must use YYYY-MM-DD.")
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise LiveExecutionValidationError("date is not a valid calendar date.") from exc
    return text


def _previous_date_key(date_key: str) -> str:
    current = datetime.strptime(date_key, "%Y-%m-%d")
    return (current - timedelta(days=1)).strftime("%Y-%m-%d")


def _date_window_ms(core: Any, date_key: str, now_ms: int) -> tuple[int, int]:
    start_ms = int(core.get_trading_day_start_epoch(date_key)) * 1000
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(str(core.get_configured_timezone() or "UTC"))
        current = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=zone)
        next_start_ms = int((current + timedelta(days=1)).timestamp() * 1000)
    except Exception:
        next_start_ms = start_ms + 86_400_000
    if start_ms > now_ms:
        raise LiveExecutionValidationError("date cannot be in the future.")
    return start_ms, min(now_ms, next_start_ms - 1)


def normalize_execution(row: Mapping[str, Any], trading_date: str) -> dict[str, Any] | None:
    exec_id = str(row.get("execId") or "").strip()
    symbol = str(row.get("symbol") or "").strip().upper()
    side = str(row.get("side") or "").strip().title()
    exec_type = str(row.get("execType") or "Trade").strip()
    qty = abs(_decimal(row.get("execQty")))
    exec_time = _integer(row.get("execTime"))
    if exec_type not in _POSITION_EXEC_TYPES:
        return None
    if not _USDT_PERPETUAL_PATTERN.fullmatch(symbol):
        return None
    if not exec_id or side not in {"Buy", "Sell"} or qty <= 0 or exec_time <= 0:
        return None
    return {
        "execId": exec_id,
        "tradingDate": trading_date,
        "execTime": exec_time,
        "sequenceNo": _integer(row.get("seq"), 0) or None,
        "symbol": symbol,
        "orderId": str(row.get("orderId") or "") or None,
        "orderLinkId": str(row.get("orderLinkId") or "") or None,
        "side": side,
        "execType": exec_type,
        "execQty": qty,
        "execPrice": _decimal(row.get("execPrice")),
        "execFee": _decimal(row.get("execFee")),
        "feeCurrency": str(row.get("feeCurrency") or row.get("feeCoin") or "USDT"),
        "leavesQty": abs(_decimal(row.get("leavesQty"))),
        "apiClosedSize": abs(_decimal(row.get("closedSize"))),
        "isMaker": _optional_bool(row.get("isMaker")),
        "raw": dict(row),
    }


def current_signed_positions(rows: Sequence[Mapping[str, Any]]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    seen_sides: dict[str, set[str]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").strip().upper()
        if not _USDT_PERPETUAL_PATTERN.fullmatch(symbol):
            continue
        position_idx = _integer(row.get("positionIdx"), 0)
        if position_idx in {1, 2}:
            raise LiveExecutionValidationError(
                f"Hedge-mode position slot detected for {symbol}; one-way exposure is required for deterministic reconciliation."
            )
        side = str(row.get("side") or "").strip().title()
        size = abs(_decimal(row.get("size")))
        if side not in {"Buy", "Sell"} or size <= 0:
            continue
        seen_sides.setdefault(symbol, set()).add(side)
        if len(seen_sides[symbol]) > 1:
            raise LiveExecutionValidationError(
                f"Opposing live positions detected for {symbol}; deterministic netting is blocked."
            )
        result[symbol] = result.get(symbol, Decimal("0")) + (size if side == "Buy" else -size)
    return result


def _execution_sort_key(item: Mapping[str, Any]) -> tuple[Any, ...]:
    # Bybit notes that equal execTime rows can be out of order. Sequence, order,
    # descending leaves quantity, and execId provide a deterministic tie-break.
    return (
        _integer(item.get("execTime")),
        _integer(item.get("sequenceNo")),
        str(item.get("orderId") or ""),
        -_decimal(item.get("leavesQty")),
        str(item.get("execId") or ""),
    )


def reconcile_executions(
    executions: Sequence[Mapping[str, Any]],
    positions_now: Mapping[str, Decimal],
) -> list[dict[str, Any]]:
    """Reconstruct before/after exposure by walking backward from Bybit positions."""

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in executions:
        row = dict(item)
        grouped.setdefault(str(row["symbol"]), []).append(row)

    reconciled: list[dict[str, Any]] = []
    for symbol, rows in grouped.items():
        rows.sort(key=_execution_sort_key)
        after = _decimal(positions_now.get(symbol, Decimal("0")))
        reverse_rows: list[dict[str, Any]] = []
        for item in reversed(rows):
            qty = abs(_decimal(item["execQty"]))
            delta = qty if item["side"] == "Buy" else -qty
            before = after - delta
            inferred_closed = Decimal("0")
            if _sign(before) and _sign(delta) and _sign(before) != _sign(delta):
                inferred_closed = min(abs(before), qty)
            api_closed = min(qty, abs(_decimal(item.get("apiClosedSize"))))
            closed_size = max(inferred_closed, api_closed)
            entry_size = max(Decimal("0"), qty - closed_size)

            if closed_size > 0 and entry_size > 0:
                action = "REVERSAL"
            elif closed_size > 0 and after == 0:
                action = "FULL_EXIT"
            elif closed_size > 0:
                action = "PARTIAL_EXIT"
            elif before == 0:
                action = "ENTRY"
            else:
                action = "ADD"

            item.update(
                positionBefore=before,
                positionAfter=after,
                closedSize=closed_size,
                entrySize=entry_size,
                action=action,
            )
            reverse_rows.append(item)
            after = before
        reconciled.extend(reversed(reverse_rows))

    reconciled.sort(key=_execution_sort_key)
    return reconciled


def opening_position_snapshot(
    executions: Sequence[Mapping[str, Any]],
    positions_now: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    """Return the position snapshot immediately before the first supplied fill."""

    opening = {symbol: _decimal(value) for symbol, value in positions_now.items()}
    first_seen: set[str] = set()
    for row in reconcile_executions(executions, positions_now):
        symbol = str(row["symbol"])
        if symbol not in first_seen:
            opening[symbol] = _decimal(row["positionBefore"])
            first_seen.add(symbol)
    return {symbol: value for symbol, value in opening.items() if value != 0}


def summarize_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    trading_date: str,
    current_positions: Mapping[str, Decimal] | None = None,
) -> dict[str, Any]:
    actions = [str(row.get("action") or "") for row in rows]
    fees = [_decimal(row.get("execFee")) for row in rows]
    fee_currencies = sorted({str(row.get("feeCurrency") or "") for row in rows if row.get("feeCurrency")})
    positions = current_positions or {}
    open_symbols = sorted(symbol for symbol, value in positions.items() if _decimal(value) != 0)
    times = [int(row["execTime"]) for row in rows if _integer(row.get("execTime")) > 0]
    symbols = sorted({str(row.get("symbol")) for row in rows if row.get("symbol")})
    return {
        "available": True,
        "source": "BYBIT_DEMO_EXECUTION_LIST",
        "tradingDate": trading_date,
        "totalExecutions": len(rows),
        "entryExecutions": sum(action in {"ENTRY", "ADD", "REVERSAL"} for action in actions),
        "exitExecutions": sum(action in {"PARTIAL_EXIT", "FULL_EXIT", "REVERSAL"} for action in actions),
        "partialCloseExecutions": actions.count("PARTIAL_EXIT"),
        "completedTrades": actions.count("FULL_EXIT") + actions.count("REVERSAL"),
        "reversalExecutions": actions.count("REVERSAL"),
        "symbolsExecuted": len(symbols),
        "executedSymbols": symbols,
        "openPositions": len(open_symbols),
        "openPositionSymbols": open_symbols,
        "feesPaid": format(sum((max(fee, Decimal("0")) for fee in fees), Decimal("0")), "f"),
        "feeRebates": format(sum((abs(min(fee, Decimal("0"))) for fee in fees), Decimal("0")), "f"),
        "netTradingFees": format(sum(fees, Decimal("0")), "f"),
        "feeCurrencies": fee_currencies,
        "firstExecutionTime": min(times) if times else None,
        "lastExecutionTime": max(times) if times else None,
        "countSemantics": {
            "entryExecutions": "fill rows that opened or increased exposure",
            "exitExecutions": "fill rows that reduced or closed exposure",
            "partialCloseExecutions": "exit fill rows that left exposure open",
            "completedTrades": "position cycles closed to zero or reversed",
        },
    }


def _jsonb(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, default=str)


def _encode_cursor(exec_time: int, exec_id: str) -> str:
    raw = json.dumps([int(exec_time), str(exec_id)], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: Any) -> tuple[int, str] | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        padded = text + "=" * (-len(text) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError
        return int(decoded[0]), str(decoded[1])
    except Exception as exc:
        raise LiveExecutionValidationError("cursor is invalid.") from exc


class LiveExecutionLedgerService:
    def __init__(self, core: Any, store: Any, *, now_ms: Callable[[], int] | None = None):
        self.core = core
        self.store = store
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        self._sync_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_snapshot: dict[str, Any] = {
            "available": False,
            "source": "BYBIT_DEMO_EXECUTION_LIST",
            "reason": "Execution ledger has not completed its first sync.",
            "stale": True,
        }

    def _connect(self):
        connect = getattr(self.store, "connect", None)
        if not callable(connect):
            raise LiveExecutionStoreError("Persistent PostgreSQL execution ledger is unavailable.")
        return connect

    def ensure_schema(self) -> None:
        migrate = getattr(self.store, "migrate", None)
        if callable(migrate):
            try:
                migrate()
            except Exception as exc:
                raise LiveExecutionStoreError(f"Execution ledger migration failed: {exc}") from exc
        connect = self._connect()
        try:
            with self.store.lock, connect() as db:
                with db.cursor() as cur:
                    cur.execute("SELECT to_regclass('public.live_execution_ledger')")
                    row = cur.fetchone()
                    if row is None or row[0] is None:
                        raise LiveExecutionStoreError("Execution ledger migration version 4 is not applied.")
        except LiveExecutionStoreError:
            raise
        except Exception as exc:
            raise LiveExecutionStoreError(f"Execution ledger schema check failed: {exc}") from exc

    def _fetch_execution_pages(self, start_ms: int, end_ms: int, trading_date: str) -> list[dict[str, Any]]:
        cursor = ""
        seen_cursors: set[str] = set()
        by_id: dict[str, dict[str, Any]] = {}
        for _ in range(MAX_SYNC_PAGES):
            params: dict[str, str] = {
                "category": "linear",
                "startTime": str(start_ms),
                "endTime": str(end_ms),
                "limit": str(BYBIT_PAGE_LIMIT),
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.core.bybit_request("GET", EXECUTION_LIST_PATH, params)
            if not isinstance(payload, Mapping) or payload.get("retCode") != 0:
                message = payload.get("retMsg") if isinstance(payload, Mapping) else None
                raise LiveExecutionTransportError(message or "Bybit execution history is unavailable.")
            result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
            for raw in result.get("list") or []:
                if isinstance(raw, Mapping):
                    normalized = normalize_execution(raw, trading_date)
                    if normalized is not None:
                        by_id[normalized["execId"]] = normalized
            next_cursor = str(result.get("nextPageCursor") or "").strip()
            if not next_cursor:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise LiveExecutionTransportError("Bybit execution pagination returned a repeated cursor.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise LiveExecutionTransportError("Bybit execution pagination exceeded the safety page limit.")
        return list(by_id.values())

    def _fetch_positions(self) -> dict[str, Decimal]:
        cursor = ""
        seen_cursors: set[str] = set()
        rows: list[Mapping[str, Any]] = []
        for _ in range(MAX_POSITION_PAGES):
            params = {
                "category": "linear",
                "settleCoin": "USDT",
                "limit": str(POSITION_PAGE_LIMIT),
            }
            if cursor:
                params["cursor"] = cursor
            payload = self.core.bybit_request("GET", POSITION_LIST_PATH, params)
            if not isinstance(payload, Mapping) or payload.get("retCode") != 0:
                message = payload.get("retMsg") if isinstance(payload, Mapping) else None
                raise LiveExecutionTransportError(message or "Bybit open positions are unavailable.")
            result = payload.get("result") if isinstance(payload.get("result"), Mapping) else {}
            rows.extend(row for row in result.get("list") or [] if isinstance(row, Mapping))
            next_cursor = str(result.get("nextPageCursor") or "").strip()
            if not next_cursor:
                break
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise LiveExecutionTransportError("Bybit position pagination returned a repeated cursor.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise LiveExecutionTransportError("Bybit position pagination exceeded the safety page limit.")
        return current_signed_positions(rows)

    def _consistent_current_snapshot(
        self, trading_date: str
    ) -> tuple[list[dict[str, Any]], dict[str, Decimal], int, int, int]:
        """Read executions between two equal position snapshots to avoid race classification."""

        for _ in range(MAX_SNAPSHOT_ATTEMPTS):
            positions_before = self._fetch_positions()
            cutoff_ms = self._now_ms()
            start_ms, end_ms = _date_window_ms(self.core, trading_date, cutoff_ms)
            executions = self._fetch_execution_pages(start_ms, end_ms, trading_date)
            positions_after = self._fetch_positions()
            if positions_before == positions_after:
                return executions, positions_after, cutoff_ms, start_ms, end_ms
        raise LiveExecutionTransportError(
            "Bybit positions changed during execution synchronization; retrying is required."
        )

    def _persist(self, rows: Sequence[Mapping[str, Any]], synced_at_ms: int) -> None:
        sql = (
            "INSERT INTO live_execution_ledger("
            "exec_id,trading_date,exec_time,sequence_no,symbol,order_id,order_link_id,side,exec_type,"
            "exec_qty,exec_price,exec_fee,fee_currency,leaves_qty,api_closed_size,closed_size,entry_size,"
            "position_before,position_after,action,is_maker,raw_payload,synced_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s) "
            "ON CONFLICT(exec_id) DO UPDATE SET "
            "trading_date=EXCLUDED.trading_date,exec_time=EXCLUDED.exec_time,sequence_no=EXCLUDED.sequence_no,"
            "symbol=EXCLUDED.symbol,order_id=EXCLUDED.order_id,order_link_id=EXCLUDED.order_link_id,"
            "side=EXCLUDED.side,exec_type=EXCLUDED.exec_type,exec_qty=EXCLUDED.exec_qty,"
            "exec_price=EXCLUDED.exec_price,exec_fee=EXCLUDED.exec_fee,fee_currency=EXCLUDED.fee_currency,"
            "leaves_qty=EXCLUDED.leaves_qty,api_closed_size=EXCLUDED.api_closed_size,"
            "closed_size=EXCLUDED.closed_size,entry_size=EXCLUDED.entry_size,"
            "position_before=EXCLUDED.position_before,position_after=EXCLUDED.position_after,"
            "action=EXCLUDED.action,is_maker=EXCLUDED.is_maker,raw_payload=EXCLUDED.raw_payload,"
            "synced_at=EXCLUDED.synced_at"
        )
        connect = self._connect()
        try:
            with self.store.lock, connect() as db:
                with db.cursor() as cur:
                    for row in rows:
                        action = str(row.get("action") or "")
                        if action not in _ACTIONS:
                            raise LiveExecutionStoreError(f"Unsupported execution action: {action}")
                        cur.execute(
                            sql,
                            (
                                row["execId"], row["tradingDate"], int(row["execTime"]), row.get("sequenceNo"),
                                row["symbol"], row.get("orderId"), row.get("orderLinkId"), row["side"],
                                row["execType"], row["execQty"], row["execPrice"], row["execFee"],
                                row["feeCurrency"], row["leavesQty"], row["apiClosedSize"], row["closedSize"],
                                row["entrySize"], row["positionBefore"], row["positionAfter"], action,
                                row.get("isMaker"), _jsonb(row.get("raw") or {}), synced_at_ms,
                            ),
                        )
                db.commit()
        except LiveExecutionStoreError:
            raise
        except Exception as exc:
            raise LiveExecutionStoreError(f"Execution ledger persistence failed: {exc}") from exc

    def _rows_for_date(self, trading_date: str) -> list[dict[str, Any]]:
        connect = self._connect()
        try:
            with self.store.lock, connect() as db:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT exec_id,trading_date,exec_time,sequence_no,symbol,order_id,order_link_id,side,"
                        "exec_type,exec_qty,exec_price,exec_fee,fee_currency,leaves_qty,api_closed_size,"
                        "closed_size,entry_size,position_before,position_after,action,is_maker,synced_at "
                        "FROM live_execution_ledger WHERE trading_date=%s "
                        "ORDER BY exec_time ASC,sequence_no ASC NULLS FIRST,order_id ASC NULLS FIRST,"
                        "leaves_qty DESC,exec_id ASC",
                        (trading_date,),
                    )
                    db_rows = cur.fetchall()
        except Exception as exc:
            raise LiveExecutionStoreError(f"Execution ledger query failed: {exc}") from exc
        keys = (
            "execId", "tradingDate", "execTime", "sequenceNo", "symbol", "orderId", "orderLinkId",
            "side", "execType", "execQty", "execPrice", "execFee", "feeCurrency", "leavesQty",
            "apiClosedSize", "closedSize", "entrySize", "positionBefore", "positionAfter", "action",
            "isMaker", "syncedAt",
        )
        return [dict(zip(keys, row)) for row in db_rows]

    def sync(self, date_value: Any = None) -> dict[str, Any]:
        current_date = self.core.get_current_trading_date_key()
        previous_date = _previous_date_key(current_date)
        trading_date = normalize_date(date_value, default=current_date)
        if trading_date not in {current_date, previous_date}:
            raise LiveExecutionValidationError(
                "Execution synchronization is limited to the current and immediately previous trading date."
            )
        if not self._sync_lock.acquire(blocking=False):
            raise LiveExecutionBusyError("Execution ledger synchronization is already running.")
        try:
            self.ensure_schema()
            current_executions, current_positions, cutoff_ms, current_start, current_end = (
                self._consistent_current_snapshot(current_date)
            )
            if trading_date == current_date:
                normalized = current_executions
                positions = current_positions
                start_ms, end_ms = current_start, current_end
            else:
                positions = opening_position_snapshot(current_executions, current_positions)
                start_ms, end_ms = _date_window_ms(self.core, previous_date, cutoff_ms)
                normalized = self._fetch_execution_pages(start_ms, end_ms, previous_date)

            reconciled = reconcile_executions(normalized, positions)
            self._persist(reconciled, cutoff_ms)
            stored_rows = self._rows_for_date(trading_date)
            summary = summarize_rows(stored_rows, trading_date=trading_date, current_positions=positions)
            summary.update(
                ok=True,
                stale=False,
                lastSyncedAt=cutoff_ms,
                windowStart=start_ms,
                windowEnd=end_ms,
                rowsFetched=len(normalized),
                rowsPersisted=len(reconciled),
                backfill=trading_date == previous_date,
                anchorMethod=(
                    "live_position_double_read"
                    if trading_date == current_date
                    else "current_day_opening_position_reconstruction"
                ),
            )
            if callable(getattr(self.store, "put", None)):
                self.store.put(f"live_execution_truth:{trading_date}", summary)
                if trading_date == current_date:
                    self.store.put("live_execution_truth", summary)
            if trading_date == current_date:
                self._last_snapshot = dict(summary)
                lock = getattr(self.core, "BOT_LOCK", None)
                state = getattr(self.core, "BOT_STATE", None)
                if lock is not None and isinstance(state, dict):
                    with lock:
                        state["executionTruth"] = dict(summary)
            return summary
        except Exception as exc:
            if trading_date == current_date:
                previous = dict(self._last_snapshot)
                previous.update(ok=False, stale=True, syncError=str(exc), lastSyncAttemptAt=self._now_ms())
                self._last_snapshot = previous
            raise
        finally:
            self._sync_lock.release()

    def cached_summary(self, date_value: Any = None) -> dict[str, Any]:
        trading_date = normalize_date(date_value, default=self.core.get_current_trading_date_key())
        stored = None
        if callable(getattr(self.store, "get", None)):
            stored = self.store.get(f"live_execution_truth:{trading_date}")
            if stored is None and trading_date == self.core.get_current_trading_date_key():
                stored = self.store.get("live_execution_truth")
        if isinstance(stored, Mapping) and stored.get("tradingDate") == trading_date:
            snapshot = dict(stored)
            age_ms = max(0, self._now_ms() - _integer(snapshot.get("lastSyncedAt")))
            snapshot["stale"] = age_ms > self.sync_interval_seconds * 2000
            self._last_snapshot = snapshot
            return snapshot
        if self._last_snapshot.get("tradingDate") == trading_date:
            return dict(self._last_snapshot)
        return {
            "available": False,
            "source": "BYBIT_DEMO_EXECUTION_LIST",
            "tradingDate": trading_date,
            "stale": True,
            "reason": "No successful execution-ledger sync exists for this trading date.",
            "totalExecutions": None,
            "entryExecutions": None,
            "exitExecutions": None,
            "partialCloseExecutions": None,
            "completedTrades": None,
            "openPositions": None,
        }

    @property
    def sync_interval_seconds(self) -> int:
        try:
            return max(30, min(900, int(os.environ.get("EXECUTION_LEDGER_SYNC_SECONDS", DEFAULT_SYNC_SECONDS))))
        except ValueError:
            return DEFAULT_SYNC_SECONDS

    @property
    def backfill_interval_seconds(self) -> int:
        try:
            return max(60, min(3600, int(os.environ.get("EXECUTION_LEDGER_BACKFILL_SECONDS", DEFAULT_BACKFILL_SECONDS))))
        except ValueError:
            return DEFAULT_BACKFILL_SECONDS

    def list(
        self,
        *,
        date_value: Any = None,
        limit: Any = 100,
        cursor: Any = None,
        symbol: Any = None,
    ) -> dict[str, Any]:
        trading_date = normalize_date(date_value, default=self.core.get_current_trading_date_key())
        try:
            bounded_limit = max(1, min(500, int(limit or 100)))
        except (TypeError, ValueError) as exc:
            raise LiveExecutionValidationError("limit must be an integer.") from exc
        decoded = _decode_cursor(cursor)
        normalized_symbol = str(symbol or "").strip().upper() or None
        if normalized_symbol and not _USDT_PERPETUAL_PATTERN.fullmatch(normalized_symbol):
            raise LiveExecutionValidationError("symbol must be a USDT perpetual contract.")
        clauses = ["trading_date=%s"]
        params: list[Any] = [trading_date]
        if normalized_symbol:
            clauses.append("symbol=%s")
            params.append(normalized_symbol)
        if decoded:
            clauses.append("(exec_time,exec_id)<(%s,%s)")
            params.extend(decoded)
        params.append(bounded_limit + 1)
        connect = self._connect()
        try:
            with self.store.lock, connect() as db:
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT exec_id,exec_time,sequence_no,symbol,order_id,order_link_id,side,exec_type,"
                        "exec_qty,exec_price,exec_fee,fee_currency,leaves_qty,closed_size,entry_size,"
                        "position_before,position_after,action,is_maker,synced_at "
                        "FROM live_execution_ledger WHERE "
                        + " AND ".join(clauses)
                        + " ORDER BY exec_time DESC,exec_id DESC LIMIT %s",
                        tuple(params),
                    )
                    db_rows = cur.fetchall()
        except Exception as exc:
            raise LiveExecutionStoreError(f"Execution ledger listing failed: {exc}") from exc
        has_more = len(db_rows) > bounded_limit
        page = db_rows[:bounded_limit]
        entries = [
            {
                "execId": row[0], "execTime": int(row[1]), "sequenceNo": row[2], "symbol": row[3],
                "orderId": row[4], "orderLinkId": row[5], "side": row[6], "execType": row[7],
                "execQty": _decimal_text(row[8]), "execPrice": _decimal_text(row[9]),
                "execFee": _decimal_text(row[10]), "feeCurrency": row[11], "leavesQty": _decimal_text(row[12]),
                "closedSize": _decimal_text(row[13]), "entrySize": _decimal_text(row[14]),
                "positionBefore": _decimal_text(row[15]), "positionAfter": _decimal_text(row[16]),
                "action": row[17], "isMaker": row[18], "syncedAt": int(row[19]),
            }
            for row in page
        ]
        next_cursor = _encode_cursor(page[-1][1], page[-1][0]) if has_more and page else None
        return {
            "ok": True,
            "source": "BYBIT_DEMO_EXECUTION_LIST",
            "tradingDate": trading_date,
            "entries": entries,
            "pagination": {"limit": bounded_limit, "hasMore": has_more, "nextCursor": next_cursor},
            "filters": {"symbol": normalized_symbol},
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return

        def run() -> None:
            last_backfill_at = 0.0
            while not self._stop.is_set():
                now = time.monotonic()
                if now - last_backfill_at >= self.backfill_interval_seconds:
                    try:
                        self.sync(_previous_date_key(self.core.get_current_trading_date_key()))
                    except Exception as exc:
                        print(f"Live execution previous-day backfill failed: {exc}", flush=True)
                    last_backfill_at = now
                try:
                    self.sync()
                except Exception as exc:
                    print(f"Live execution ledger sync failed: {exc}", flush=True)
                self._stop.wait(self.sync_interval_seconds)

        self._thread = threading.Thread(target=run, name="live-execution-ledger", daemon=True)
        self._thread.start()


def install(core: Any, *, start_worker: bool = True) -> LiveExecutionLedgerService:
    existing = getattr(core, "_live_execution_ledger_service", None)
    if isinstance(existing, LiveExecutionLedgerService):
        return existing
    service = LiveExecutionLedgerService(core, getattr(core, "_durable_state_store", None))
    schema_ready = True
    try:
        service.ensure_schema()
    except LiveExecutionLedgerError as exc:
        schema_ready = False
        print(f"Live execution ledger unavailable: {exc}", flush=True)
    core._live_execution_ledger_service = service

    if not getattr(core, "_live_execution_daily_report_decorated", False):
        original_daily_risk_report = core.daily_risk_report

        def daily_risk_with_execution_truth(state: Mapping[str, Any]) -> dict[str, Any]:
            report = dict(original_daily_risk_report(state) or {})
            truth = service.cached_summary(report.get("tradingDateKey"))
            report["executionTruth"] = truth
            report["tradeCounters"] = {
                "source": truth.get("source"),
                "available": truth.get("available", False),
                "entryExecutions": truth.get("entryExecutions"),
                "exitExecutions": truth.get("exitExecutions"),
                "partialCloseExecutions": truth.get("partialCloseExecutions"),
                "completedTrades": truth.get("completedTrades"),
                "openPositions": truth.get("openPositions"),
            }
            report["legacyAcceptedOrderCount"] = report.get("tradesToday")
            report["legacyTradeGateActive"] = True
            return report

        core.daily_risk_report = daily_risk_with_execution_truth
        core._live_execution_daily_report_decorated = True

    if start_worker and schema_ready:
        service.start()
    return service


def _service(core: Any) -> LiveExecutionLedgerService:
    current = getattr(core, "_live_execution_ledger_service", None)
    return current if isinstance(current, LiveExecutionLedgerService) else install(core)


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    if isinstance(exc, LiveExecutionValidationError):
        status, code = 400, "LIVE_EXECUTION_INVALID"
    elif isinstance(exc, LiveExecutionBusyError):
        status, code = 409, "LIVE_EXECUTION_SYNC_BUSY"
    elif isinstance(exc, LiveExecutionTransportError):
        status, code = 502, "BYBIT_EXECUTION_TRUTH_UNAVAILABLE"
    elif isinstance(exc, LiveExecutionStoreError):
        status, code = 503, "LIVE_EXECUTION_STORE_UNAVAILABLE"
    else:
        status, code = 500, "LIVE_EXECUTION_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Live execution truth operation failed."
    core.json_response(handler, status, {"ok": False, "code": code, "error": message})


def handle_get(handler: Any, core: Any, path: str) -> bool:
    if path not in {SUMMARY_PATH, LIST_PATH}:
        return False
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    try:
        result = (
            {"ok": True, **_service(core).cached_summary(query.get("date"))}
            if path == SUMMARY_PATH
            else _service(core).list(
                date_value=query.get("date"),
                limit=query.get("limit", 100),
                cursor=query.get("cursor"),
                symbol=query.get("symbol"),
            )
        )
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True


def handle_post(handler: Any, core: Any, path: str, payload: Mapping[str, Any]) -> bool:
    if path != SYNC_PATH:
        return False
    try:
        result = _service(core).sync(payload.get("date") if isinstance(payload, Mapping) else None)
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
