"""PostgreSQL schema and storage primitives for Historical Replay.

This module stores only historical market data and simulated replay state. It
contains no exchange client, API key handling, order submission, or position
management capability.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from psycopg.types.json import Jsonb
except ImportError:  # pragma: no cover - the parent store reports this cleanly
    Jsonb = None


REPLAY_SCHEMA_VERSION = 2
REPLAY_INTERVALS = frozenset({"5", "15", "60"})
REPLAY_SESSION_STATUSES = frozenset(
    {"READY", "RUNNING", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"}
)
REPLAY_TRADE_STATUSES = frozenset({"OPEN", "CLOSED", "CANCELLED"})
REPLAY_TRADE_SIDES = frozenset({"Buy", "Sell"})

REPLAY_MIGRATION: tuple[int, tuple[str, ...]] = (
    REPLAY_SCHEMA_VERSION,
    (
        "CREATE TABLE IF NOT EXISTS replay_candles ("
        "symbol TEXT NOT NULL, timeframe TEXT NOT NULL, open_time BIGINT NOT NULL, "
        "open_price NUMERIC(38,18) NOT NULL, high_price NUMERIC(38,18) NOT NULL, "
        "low_price NUMERIC(38,18) NOT NULL, close_price NUMERIC(38,18) NOT NULL, "
        "volume NUMERIC(38,18) NOT NULL, turnover NUMERIC(38,18), source TEXT NOT NULL, "
        "ingested_at BIGINT NOT NULL, PRIMARY KEY(symbol,timeframe,open_time))",
        "CREATE INDEX IF NOT EXISTS ix_replay_candles_range "
        "ON replay_candles(symbol,timeframe,open_time)",
        "CREATE TABLE IF NOT EXISTS replay_sessions ("
        "session_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, timeframe TEXT NOT NULL, "
        "status TEXT NOT NULL, start_time BIGINT NOT NULL, end_time BIGINT NOT NULL, "
        "cursor_time BIGINT, initial_balance NUMERIC(38,18) NOT NULL, "
        "balance NUMERIC(38,18) NOT NULL, equity NUMERIC(38,18) NOT NULL, "
        "strategy_mode TEXT NOT NULL, config JSONB NOT NULL, summary JSONB NOT NULL, "
        "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS ix_replay_sessions_updated "
        "ON replay_sessions(updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS ix_replay_sessions_status "
        "ON replay_sessions(status,updated_at DESC)",
        "CREATE TABLE IF NOT EXISTS replay_events ("
        "id BIGSERIAL PRIMARY KEY, session_id TEXT NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE, "
        "sequence_no BIGINT NOT NULL, event_type TEXT NOT NULL, candle_open_time BIGINT, "
        "payload JSONB NOT NULL, created_at BIGINT NOT NULL, "
        "UNIQUE(session_id,sequence_no))",
        "CREATE INDEX IF NOT EXISTS ix_replay_events_session "
        "ON replay_events(session_id,sequence_no)",
        "CREATE TABLE IF NOT EXISTS replay_trades ("
        "trade_id TEXT NOT NULL, session_id TEXT NOT NULL REFERENCES replay_sessions(session_id) ON DELETE CASCADE, "
        "symbol TEXT NOT NULL, side TEXT NOT NULL, status TEXT NOT NULL, "
        "entry_time BIGINT NOT NULL, exit_time BIGINT, "
        "entry_price NUMERIC(38,18) NOT NULL, exit_price NUMERIC(38,18), "
        "quantity NUMERIC(38,18) NOT NULL, realized_pnl NUMERIC(38,18) NOT NULL, "
        "fees NUMERIC(38,18) NOT NULL, payload JSONB NOT NULL, "
        "created_at BIGINT NOT NULL, updated_at BIGINT NOT NULL, "
        "PRIMARY KEY(session_id,trade_id))",
        "CREATE INDEX IF NOT EXISTS ix_replay_trades_session "
        "ON replay_trades(session_id,entry_time)",
        "CREATE INDEX IF NOT EXISTS ix_replay_trades_status "
        "ON replay_trades(session_id,status,updated_at DESC)",
    ),
)


class ReplayStorageValidationError(ValueError):
    """Raised when replay persistence input violates the locked data contract."""


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _normalized_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,24}USDT", symbol):
        raise ReplayStorageValidationError("Replay symbol must be a USDT contract code.")
    return symbol


def _normalized_interval(value: Any) -> str:
    interval = str(value or "").strip()
    if interval not in REPLAY_INTERVALS:
        raise ReplayStorageValidationError("Replay timeframe must be one of: 5, 15, 60.")
    return interval


def _normalized_identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", text):
        raise ReplayStorageValidationError(
            f"{field} must contain 8-80 letters, numbers, underscores, or hyphens."
        )
    return text


def _integer(value: Any, field: str, *, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplayStorageValidationError(f"{field} must be an integer.") from exc
    if result < minimum:
        raise ReplayStorageValidationError(f"{field} must be at least {minimum}.")
    return result


def _decimal(
    value: Any,
    field: str,
    *,
    positive: bool = False,
    nonnegative: bool = False,
    default: Any | None = None,
) -> Decimal:
    candidate = default if value is None else value
    try:
        result = Decimal(str(candidate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplayStorageValidationError(f"{field} must be numeric.") from exc
    if not result.is_finite():
        raise ReplayStorageValidationError(f"{field} must be finite.")
    if positive and result <= 0:
        raise ReplayStorageValidationError(f"{field} must be greater than zero.")
    if nonnegative and result < 0:
        raise ReplayStorageValidationError(f"{field} cannot be negative.")
    return result


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReplayStorageValidationError(f"{field} must be an object.")
    return dict(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _jsonb(value: Any):
    if Jsonb is None:
        raise RuntimeError("psycopg JSONB support is unavailable.")
    return Jsonb(_json_safe(value))


def normalize_replay_candle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate one closed OHLCV candle before persistence."""

    if not isinstance(payload, Mapping):
        raise ReplayStorageValidationError("Replay candle must be an object.")
    symbol = _normalized_symbol(payload.get("symbol"))
    timeframe = _normalized_interval(payload.get("timeframe") or payload.get("interval"))
    open_time = _integer(
        payload.get("open_time", payload.get("openTime", payload.get("time"))),
        "open_time",
    )
    open_price = _decimal(payload.get("open_price", payload.get("open")), "open", positive=True)
    high_price = _decimal(payload.get("high_price", payload.get("high")), "high", positive=True)
    low_price = _decimal(payload.get("low_price", payload.get("low")), "low", positive=True)
    close_price = _decimal(payload.get("close_price", payload.get("close")), "close", positive=True)
    volume = _decimal(payload.get("volume"), "volume", nonnegative=True)
    turnover_raw = payload.get("turnover")
    turnover = (
        None
        if _is_missing(turnover_raw)
        else _decimal(turnover_raw, "turnover", nonnegative=True)
    )

    if high_price < max(open_price, close_price, low_price):
        raise ReplayStorageValidationError("Candle high is below another OHLC value.")
    if low_price > min(open_price, close_price, high_price):
        raise ReplayStorageValidationError("Candle low is above another OHLC value.")

    source = str(payload.get("source") or "bybit_public_kline").strip()
    if not source:
        raise ReplayStorageValidationError("Candle source is required.")
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": open_time,
        "open_price": open_price,
        "high_price": high_price,
        "low_price": low_price,
        "close_price": close_price,
        "volume": volume,
        "turnover": turnover,
        "source": source[:80],
    }


def normalize_replay_session(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize the immutable identity and initial state of a replay session."""

    if not isinstance(payload, Mapping):
        raise ReplayStorageValidationError("Replay session must be an object.")
    session_id = _normalized_identifier(
        payload.get("session_id", payload.get("sessionId")), "session_id"
    )
    symbol = _normalized_symbol(payload.get("symbol"))
    timeframe = _normalized_interval(payload.get("timeframe") or payload.get("interval"))
    status = str(payload.get("status") or "READY").strip().upper()
    if status not in REPLAY_SESSION_STATUSES:
        raise ReplayStorageValidationError("Unsupported replay session status.")
    start_time = _integer(payload.get("start_time", payload.get("startTime")), "start_time")
    end_time = _integer(payload.get("end_time", payload.get("endTime")), "end_time")
    if end_time <= start_time:
        raise ReplayStorageValidationError("Replay end_time must be after start_time.")
    cursor_raw = payload.get("cursor_time", payload.get("cursorTime"))
    cursor_time = None if _is_missing(cursor_raw) else _integer(cursor_raw, "cursor_time")
    if cursor_time is not None and not start_time <= cursor_time <= end_time:
        raise ReplayStorageValidationError("Replay cursor_time must be inside the session range.")

    initial_balance = _decimal(
        payload.get("initial_balance", payload.get("initialBalance")),
        "initial_balance",
        positive=True,
    )
    balance = _decimal(payload.get("balance"), "balance", nonnegative=True, default=initial_balance)
    equity = _decimal(payload.get("equity"), "equity", nonnegative=True, default=balance)
    strategy_mode = str(
        payload.get("strategy_mode", payload.get("strategyMode")) or "conservative"
    ).strip().lower()
    if strategy_mode not in {"conservative", "balanced", "aggressive"}:
        raise ReplayStorageValidationError("Unsupported replay strategy_mode.")
    return {
        "session_id": session_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "start_time": start_time,
        "end_time": end_time,
        "cursor_time": cursor_time,
        "initial_balance": initial_balance,
        "balance": balance,
        "equity": equity,
        "strategy_mode": strategy_mode,
        "config": _mapping(payload.get("config"), "config"),
        "summary": _mapping(payload.get("summary"), "summary"),
    }


def normalize_replay_trade(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one simulated trade. Exchange identifiers are intentionally absent."""

    if not isinstance(payload, Mapping):
        raise ReplayStorageValidationError("Replay trade must be an object.")
    trade_id = _normalized_identifier(payload.get("trade_id", payload.get("tradeId")), "trade_id")
    session_id = _normalized_identifier(
        payload.get("session_id", payload.get("sessionId")), "session_id"
    )
    side = str(payload.get("side") or "").strip().title()
    if side not in REPLAY_TRADE_SIDES:
        raise ReplayStorageValidationError("Replay trade side must be Buy or Sell.")
    status = str(payload.get("status") or "OPEN").strip().upper()
    if status not in REPLAY_TRADE_STATUSES:
        raise ReplayStorageValidationError("Unsupported replay trade status.")
    entry_time = _integer(payload.get("entry_time", payload.get("entryTime")), "entry_time")
    exit_raw = payload.get("exit_time", payload.get("exitTime"))
    exit_time = None if _is_missing(exit_raw) else _integer(exit_raw, "exit_time")
    if exit_time is not None and exit_time < entry_time:
        raise ReplayStorageValidationError("Replay trade exit_time cannot precede entry_time.")
    entry_price = _decimal(
        payload.get("entry_price", payload.get("entryPrice")), "entry_price", positive=True
    )
    exit_price_raw = payload.get("exit_price", payload.get("exitPrice"))
    exit_price = (
        None
        if _is_missing(exit_price_raw)
        else _decimal(exit_price_raw, "exit_price", positive=True)
    )
    if status == "CLOSED" and (exit_time is None or exit_price is None):
        raise ReplayStorageValidationError("Closed replay trades require exit_time and exit_price.")
    return {
        "trade_id": trade_id,
        "session_id": session_id,
        "symbol": _normalized_symbol(payload.get("symbol")),
        "side": side,
        "status": status,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": _decimal(payload.get("quantity", payload.get("qty")), "quantity", positive=True),
        "realized_pnl": _decimal(
            payload.get("realized_pnl", payload.get("realizedPnl")),
            "realized_pnl",
            default=0,
        ),
        "fees": _decimal(payload.get("fees"), "fees", nonnegative=True, default=0),
        "payload": _mapping(payload.get("payload"), "payload"),
    }


def _session_row(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "sessionId": row[0],
        "symbol": row[1],
        "timeframe": row[2],
        "status": row[3],
        "startTime": int(row[4]),
        "endTime": int(row[5]),
        "cursorTime": int(row[6]) if row[6] is not None else None,
        "initialBalance": str(row[7]),
        "balance": str(row[8]),
        "equity": str(row[9]),
        "strategyMode": row[10],
        "config": row[11] if isinstance(row[11], dict) else {},
        "summary": row[12] if isinstance(row[12], dict) else {},
        "createdAt": int(row[13]),
        "updatedAt": int(row[14]),
    }


class ReplayStorageMixin:
    """Parameterized PostgreSQL operations mixed into the durable state store."""

    def upsert_replay_candles(self, candles: Iterable[Mapping[str, Any]]) -> int:
        rows = [normalize_replay_candle(candle) for candle in candles]
        if not rows:
            return 0
        now = int(time.time())
        values = [
            (
                row["symbol"],
                row["timeframe"],
                row["open_time"],
                row["open_price"],
                row["high_price"],
                row["low_price"],
                row["close_price"],
                row["volume"],
                row["turnover"],
                row["source"],
                now,
            )
            for row in rows
        ]
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.executemany(
                    "INSERT INTO replay_candles("
                    "symbol,timeframe,open_time,open_price,high_price,low_price,close_price,volume,turnover,source,ingested_at"
                    ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(symbol,timeframe,open_time) DO UPDATE SET "
                    "open_price=EXCLUDED.open_price,high_price=EXCLUDED.high_price,"
                    "low_price=EXCLUDED.low_price,close_price=EXCLUDED.close_price,"
                    "volume=EXCLUDED.volume,turnover=EXCLUDED.turnover,"
                    "source=EXCLUDED.source,ingested_at=EXCLUDED.ingested_at",
                    values,
                )
            db.commit()
        return len(rows)

    def replay_candle_coverage(self, symbol: str, timeframe: str) -> dict[str, Any]:
        normalized_symbol = _normalized_symbol(symbol)
        normalized_interval = _normalized_interval(timeframe)
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*),MIN(open_time),MAX(open_time) FROM replay_candles "
                    "WHERE symbol=%s AND timeframe=%s",
                    (normalized_symbol, normalized_interval),
                )
                count, first_time, last_time = cur.fetchone()
        return {
            "symbol": normalized_symbol,
            "timeframe": normalized_interval,
            "count": int(count or 0),
            "firstOpenTime": int(first_time) if first_time is not None else None,
            "lastOpenTime": int(last_time) if last_time is not None else None,
        }

    def get_replay_candles(
        self,
        symbol: str,
        timeframe: str,
        start_time: int,
        end_time: int,
        limit: int = 5000,
    ) -> list[dict[str, Any]]:
        normalized_symbol = _normalized_symbol(symbol)
        normalized_interval = _normalized_interval(timeframe)
        start = _integer(start_time, "start_time")
        end = _integer(end_time, "end_time")
        if end < start:
            raise ReplayStorageValidationError("end_time cannot precede start_time.")
        bounded_limit = max(1, min(10000, int(limit)))
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT open_time,open_price,high_price,low_price,close_price,volume,turnover,source "
                    "FROM replay_candles WHERE symbol=%s AND timeframe=%s "
                    "AND open_time BETWEEN %s AND %s ORDER BY open_time ASC LIMIT %s",
                    (normalized_symbol, normalized_interval, start, end, bounded_limit),
                )
                rows = cur.fetchall()
        return [
            {
                "symbol": normalized_symbol,
                "timeframe": normalized_interval,
                "openTime": int(row[0]),
                "open": str(row[1]),
                "high": str(row[2]),
                "low": str(row[3]),
                "close": str(row[4]),
                "volume": str(row[5]),
                "turnover": str(row[6]) if row[6] is not None else None,
                "source": row[7],
            }
            for row in rows
        ]

    def create_replay_session(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        session = normalize_replay_session(payload)
        now = int(time.time())
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO replay_sessions("
                    "session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                    "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at"
                    ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(session_id) DO NOTHING",
                    (
                        session["session_id"],
                        session["symbol"],
                        session["timeframe"],
                        session["status"],
                        session["start_time"],
                        session["end_time"],
                        session["cursor_time"],
                        session["initial_balance"],
                        session["balance"],
                        session["equity"],
                        session["strategy_mode"],
                        _jsonb(session["config"]),
                        _jsonb(session["summary"]),
                        now,
                        now,
                    ),
                )
                created = cur.rowcount == 1
            db.commit()
        return {"created": created, **_json_safe(session), "created_at": now, "updated_at": now}

    def get_replay_session(self, session_id: str) -> dict[str, Any] | None:
        normalized_id = _normalized_identifier(session_id, "session_id")
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                    "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
                    "FROM replay_sessions WHERE session_id=%s",
                    (normalized_id,),
                )
                row = cur.fetchone()
        return _session_row(row)

    def list_replay_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(500, int(limit)))
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                    "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
                    "FROM replay_sessions ORDER BY updated_at DESC LIMIT %s",
                    (bounded_limit,),
                )
                rows = cur.fetchall()
        return [session for row in rows if (session := _session_row(row)) is not None]

    def update_replay_session_state(
        self,
        session_id: str,
        *,
        status: str,
        cursor_time: int | None,
        balance: Any,
        equity: Any,
        summary: Mapping[str, Any] | None = None,
    ) -> bool:
        normalized_id = _normalized_identifier(session_id, "session_id")
        normalized_status = str(status or "").strip().upper()
        if normalized_status not in REPLAY_SESSION_STATUSES:
            raise ReplayStorageValidationError("Unsupported replay session status.")
        normalized_cursor = None if cursor_time is None else _integer(cursor_time, "cursor_time")
        normalized_balance = _decimal(balance, "balance", nonnegative=True)
        normalized_equity = _decimal(equity, "equity", nonnegative=True)

        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT start_time,end_time,summary FROM replay_sessions "
                    "WHERE session_id=%s FOR UPDATE",
                    (normalized_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return False
                start_time, end_time, stored_summary = row
                if normalized_cursor is not None and not (
                    int(start_time) <= normalized_cursor <= int(end_time)
                ):
                    raise ReplayStorageValidationError(
                        "Replay cursor_time must be inside the session range."
                    )
                normalized_summary = (
                    stored_summary if summary is None and isinstance(stored_summary, dict) else {}
                )
                if summary is not None:
                    normalized_summary = _mapping(summary, "summary")
                cur.execute(
                    "UPDATE replay_sessions SET status=%s,cursor_time=%s,balance=%s,equity=%s,"
                    "summary=%s,updated_at=%s WHERE session_id=%s",
                    (
                        normalized_status,
                        normalized_cursor,
                        normalized_balance,
                        normalized_equity,
                        _jsonb(normalized_summary),
                        int(time.time()),
                        normalized_id,
                    ),
                )
                changed = cur.rowcount == 1
            db.commit()
        return changed

    def append_replay_event(
        self,
        session_id: str,
        sequence_no: int,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        candle_open_time: int | None = None,
    ) -> bool:
        normalized_id = _normalized_identifier(session_id, "session_id")
        normalized_sequence = _integer(sequence_no, "sequence_no", minimum=0)
        normalized_event = str(event_type or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9_.-]{2,80}", normalized_event):
            raise ReplayStorageValidationError("Invalid replay event_type.")
        normalized_candle_time = (
            None
            if candle_open_time is None
            else _integer(candle_open_time, "candle_open_time")
        )
        body = _mapping(payload, "payload")
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO replay_events("
                    "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
                    ") VALUES(%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(session_id,sequence_no) DO NOTHING",
                    (
                        normalized_id,
                        normalized_sequence,
                        normalized_event,
                        normalized_candle_time,
                        _jsonb(body),
                        int(time.time()),
                    ),
                )
                created = cur.rowcount == 1
            db.commit()
        return created

    def list_replay_events(self, session_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        normalized_id = _normalized_identifier(session_id, "session_id")
        bounded_limit = max(1, min(5000, int(limit)))
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT sequence_no,event_type,candle_open_time,payload,created_at "
                    "FROM replay_events WHERE session_id=%s ORDER BY sequence_no ASC LIMIT %s",
                    (normalized_id, bounded_limit),
                )
                rows = cur.fetchall()
        return [
            {
                "sequenceNo": int(row[0]),
                "eventType": row[1],
                "candleOpenTime": int(row[2]) if row[2] is not None else None,
                "payload": row[3] if isinstance(row[3], dict) else {},
                "createdAt": int(row[4]),
            }
            for row in rows
        ]

    def upsert_replay_trade(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        trade = normalize_replay_trade(payload)
        now = int(time.time())
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "INSERT INTO replay_trades("
                    "trade_id,session_id,symbol,side,status,entry_time,exit_time,entry_price,"
                    "exit_price,quantity,realized_pnl,fees,payload,created_at,updated_at"
                    ") VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT(session_id,trade_id) DO UPDATE SET "
                    "status=EXCLUDED.status,exit_time=EXCLUDED.exit_time,"
                    "exit_price=EXCLUDED.exit_price,quantity=EXCLUDED.quantity,"
                    "realized_pnl=EXCLUDED.realized_pnl,fees=EXCLUDED.fees,"
                    "payload=EXCLUDED.payload,updated_at=EXCLUDED.updated_at",
                    (
                        trade["trade_id"],
                        trade["session_id"],
                        trade["symbol"],
                        trade["side"],
                        trade["status"],
                        trade["entry_time"],
                        trade["exit_time"],
                        trade["entry_price"],
                        trade["exit_price"],
                        trade["quantity"],
                        trade["realized_pnl"],
                        trade["fees"],
                        _jsonb(trade["payload"]),
                        now,
                        now,
                    ),
                )
            db.commit()
        return _json_safe(trade)

    def list_replay_trades(self, session_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        normalized_id = _normalized_identifier(session_id, "session_id")
        bounded_limit = max(1, min(5000, int(limit)))
        with self.lock, self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    "SELECT trade_id,symbol,side,status,entry_time,exit_time,entry_price,"
                    "exit_price,quantity,realized_pnl,fees,payload,created_at,updated_at "
                    "FROM replay_trades WHERE session_id=%s ORDER BY entry_time ASC LIMIT %s",
                    (normalized_id, bounded_limit),
                )
                rows = cur.fetchall()
        return [
            {
                "tradeId": row[0],
                "symbol": row[1],
                "side": row[2],
                "status": row[3],
                "entryTime": int(row[4]),
                "exitTime": int(row[5]) if row[5] is not None else None,
                "entryPrice": str(row[6]),
                "exitPrice": str(row[7]) if row[7] is not None else None,
                "quantity": str(row[8]),
                "realizedPnl": str(row[9]),
                "fees": str(row[10]),
                "payload": row[11] if isinstance(row[11], dict) else {},
                "createdAt": int(row[12]),
                "updatedAt": int(row[13]),
            }
            for row in rows
        ]
