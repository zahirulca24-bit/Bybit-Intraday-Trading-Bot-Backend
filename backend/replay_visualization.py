"""Read-only visualization contract for Historical Replay.

The endpoint exposes only PostgreSQL replay candles and simulated replay trades.
It has no exchange client, credentials, private order route, or external execution
capability. Active sessions are look-ahead safe by default: candles after the
persisted replay cursor are not returned.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

try:
    from .replay_storage import _normalized_identifier, _session_row
except ImportError:
    from replay_storage import _normalized_identifier, _session_row

_IDENTIFIER = r"[A-Za-z0-9_-]{8,80}"
_VISUALIZATION_PATH = re.compile(rf"^/api/replay/sessions/({_IDENTIFIER})/visualization$")
DEFAULT_CANDLE_LIMIT = 500
MAX_CANDLE_LIMIT = 1000
MONEY_QUANTUM = Decimal("0.00000001")
R_QUANTUM = Decimal("0.0001")


class ReplayVisualizationError(RuntimeError):
    pass


class ReplayVisualizationValidationError(ReplayVisualizationError):
    pass


class ReplayVisualizationNotFoundError(ReplayVisualizationError):
    pass


class ReplayVisualizationStoreError(ReplayVisualizationError):
    pass


def _decimal(value: Any, field: str, *, default: Any | None = None) -> Decimal:
    candidate = default if value in (None, "") else value
    try:
        result = Decimal(str(candidate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplayVisualizationStoreError(f"Stored {field} is not numeric.") from exc
    if not result.is_finite():
        raise ReplayVisualizationStoreError(f"Stored {field} is not finite.")
    return result


def _money(value: Any) -> str:
    return format(_decimal(value, "money", default=0).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN), ".8f")


def _r(value: Decimal) -> str:
    return format(value.quantize(R_QUANTUM, rounding=ROUND_DOWN), ".4f")


def _bounded_limit(value: Any) -> int:
    candidate = DEFAULT_CANDLE_LIMIT if value in (None, "") else value
    try:
        result = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ReplayVisualizationValidationError("limit must be an integer.") from exc
    if not 1 <= result <= MAX_CANDLE_LIMIT:
        raise ReplayVisualizationValidationError(
            f"limit must be between 1 and {MAX_CANDLE_LIMIT}."
        )
    return result


def _boolean(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ReplayVisualizationValidationError("includeFuture must be boolean.")


def normalize_query(query: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(query or {})
    return {
        "limit": _bounded_limit(data.get("limit")),
        "includeFuture": _boolean(data.get("includeFuture"), default=False),
    }


def require_store(store: Any) -> Any:
    if store is None or not hasattr(store, "lock") or not callable(getattr(store, "connect", None)):
        raise ReplayVisualizationStoreError("Persistent PostgreSQL replay storage is unavailable.")
    status = getattr(store, "status", None)
    if callable(status):
        snapshot = dict(status() or {})
        if not snapshot.get("ok") or snapshot.get("degraded"):
            raise ReplayVisualizationStoreError(
                snapshot.get("error") or "Persistent PostgreSQL replay storage is degraded."
            )
    return store


def _trade_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
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
    }


def trade_visualization(trade: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(trade.get("payload") or {}) if isinstance(trade.get("payload"), Mapping) else {}
    risk_amount = _decimal(payload.get("riskAmount"), "riskAmount", default=0)
    net_pnl = _decimal(payload.get("netPnl", trade.get("realizedPnl")), "netPnl", default=0)
    entry_time = int(trade.get("entryTime") or 0)
    exit_time_raw = trade.get("exitTime")
    exit_time = int(exit_time_raw) if exit_time_raw is not None else None
    holding_ms = max(0, exit_time - entry_time) if exit_time is not None else None

    protection = {
        "stopLoss": str(payload.get("stopLoss")) if payload.get("stopLoss") is not None else None,
        "takeProfit": str(payload.get("takeProfit")) if payload.get("takeProfit") is not None else None,
    }
    markers: list[dict[str, Any]] = [
        {
            "type": "entry",
            "time": entry_time,
            "price": str(trade.get("entryPrice")),
            "side": trade.get("side"),
            "tradeId": trade.get("tradeId"),
        }
    ]
    if protection["stopLoss"] is not None:
        markers.append(
            {
                "type": "stop_loss",
                "time": entry_time,
                "price": protection["stopLoss"],
                "tradeId": trade.get("tradeId"),
            }
        )
    if protection["takeProfit"] is not None:
        markers.append(
            {
                "type": "take_profit",
                "time": entry_time,
                "price": protection["takeProfit"],
                "tradeId": trade.get("tradeId"),
            }
        )
    if exit_time is not None and trade.get("exitPrice") is not None:
        markers.append(
            {
                "type": "exit",
                "time": exit_time,
                "price": str(trade.get("exitPrice")),
                "reason": payload.get("exitReason"),
                "tradeId": trade.get("tradeId"),
            }
        )

    return {
        "tradeId": trade.get("tradeId"),
        "symbol": trade.get("symbol"),
        "side": trade.get("side"),
        "status": trade.get("status"),
        "quantity": str(trade.get("quantity")),
        "entryTime": entry_time,
        "entryPrice": str(trade.get("entryPrice")),
        "exitTime": exit_time,
        "exitPrice": str(trade.get("exitPrice")) if trade.get("exitPrice") is not None else None,
        "exitReason": payload.get("exitReason"),
        "protection": protection,
        "grossPnl": _money(payload.get("grossPnl", 0)),
        "fees": _money(trade.get("fees", 0)),
        "netPnl": _money(net_pnl),
        "riskAmount": _money(risk_amount),
        "rMultiple": _r(net_pnl / risk_amount) if risk_amount > 0 else None,
        "holdingDurationMs": holding_ms,
        "sameCandleConflict": bool(payload.get("sameCandleConflict", False)),
        "sameCandlePolicy": payload.get("sameCandlePolicy") or "stop_first",
        "limitedLiabilityApplied": bool(payload.get("limitedLiabilityApplied", False)),
        "markers": markers,
    }


class ReplayVisualizationService:
    def __init__(self, store: Any):
        self.store = store

    def _session(self, cursor: Any, session_id: str) -> dict[str, Any]:
        normalized = _normalized_identifier(session_id, "session_id")
        cursor.execute(
            "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
            "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
            "FROM replay_sessions WHERE session_id=%s",
            (normalized,),
        )
        session = _session_row(cursor.fetchone())
        if session is None:
            raise ReplayVisualizationNotFoundError("Replay session was not found.")
        return session

    def visualization(
        self,
        session_id: str,
        *,
        limit: int = DEFAULT_CANDLE_LIMIT,
        include_future: bool = False,
    ) -> dict[str, Any]:
        store = require_store(self.store)
        bounded_limit = _bounded_limit(limit)
        with store.lock, store.connect() as db:
            with db.cursor() as cur:
                session = self._session(cur, session_id)
                completed = session["status"] == "COMPLETED"
                reveal_future = bool(include_future and completed)
                visible_end = session["endTime"] if reveal_future or completed else session.get("cursorTime")

                candles: list[dict[str, Any]] = []
                if visible_end is not None:
                    cur.execute(
                        "SELECT open_time,open_price,high_price,low_price,close_price,volume,"
                        "turnover,source FROM replay_candles "
                        "WHERE symbol=%s AND timeframe=%s AND open_time>=%s AND open_time<=%s "
                        "ORDER BY open_time DESC LIMIT %s",
                        (
                            session["symbol"],
                            session["timeframe"],
                            session["startTime"],
                            int(visible_end),
                            bounded_limit,
                        ),
                    )
                    rows = list(reversed(cur.fetchall()))
                    candles = [
                        {
                            "openTime": int(row[0]),
                            "open": str(row[1]),
                            "high": str(row[2]),
                            "low": str(row[3]),
                            "close": str(row[4]),
                            "volume": str(row[5]),
                            "turnover": str(row[6]) if row[6] is not None else None,
                            "source": row[7],
                            "closed": True,
                        }
                        for row in rows
                    ]

                cur.execute(
                    "SELECT trade_id,symbol,side,status,entry_time,exit_time,entry_price,"
                    "exit_price,quantity,realized_pnl,fees,payload "
                    "FROM replay_trades WHERE session_id=%s ORDER BY entry_time ASC,trade_id ASC",
                    (session["sessionId"],),
                )
                trades = [trade_visualization(_trade_row(row)) for row in cur.fetchall()]

        markers = [marker for trade in trades for marker in trade["markers"]]
        markers.sort(key=lambda marker: (int(marker.get("time") or 0), str(marker.get("tradeId") or ""), marker["type"]))
        return {
            "ok": True,
            "session": {
                "sessionId": session["sessionId"],
                "symbol": session["symbol"],
                "timeframe": session["timeframe"],
                "status": session["status"],
                "startTime": session["startTime"],
                "endTime": session["endTime"],
                "cursorTime": session.get("cursorTime"),
                "updatedAt": session["updatedAt"],
            },
            "candles": candles,
            "trades": trades,
            "markers": markers,
            "meta": {
                "candleLimit": bounded_limit,
                "returnedCandles": len(candles),
                "returnedTrades": len(trades),
                "returnedMarkers": len(markers),
                "lookaheadBlocked": not completed,
                "includeFutureRequested": bool(include_future),
                "includeFutureApplied": reveal_future,
                "candleSource": "POSTGRESQL_REPLAY_CANDLES",
                "tradeSource": "POSTGRESQL_REPLAY_TRADES",
                "sameCandlePolicy": "stop_first",
            },
            "safety": {
                "simulationOnly": True,
                "externalExecutionAllowed": False,
                "exchangeCredentialsUsed": False,
                "closedCandlesOnly": True,
                "activeSessionLookaheadBlocked": True,
            },
            "visualizationContractVersion": 1,
        }


def install(core: Any) -> ReplayVisualizationService:
    existing = getattr(core, "_replay_visualization_service", None)
    if isinstance(existing, ReplayVisualizationService):
        return existing
    service = ReplayVisualizationService(getattr(core, "_durable_state_store", None))
    core._replay_visualization_service = service
    return service


def _service(core: Any) -> ReplayVisualizationService:
    current = getattr(core, "_replay_visualization_service", None)
    return current if isinstance(current, ReplayVisualizationService) else install(core)


def is_get_path(path: str) -> bool:
    return bool(_VISUALIZATION_PATH.fullmatch(path))


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    if isinstance(exc, ReplayVisualizationValidationError):
        status, code = 400, "REPLAY_VISUALIZATION_INVALID"
    elif isinstance(exc, ReplayVisualizationNotFoundError):
        status, code = 404, "REPLAY_SESSION_NOT_FOUND"
    elif isinstance(exc, ReplayVisualizationStoreError):
        status, code = 503, "REPLAY_STORAGE_UNAVAILABLE"
    else:
        status, code = 500, "REPLAY_VISUALIZATION_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Historical replay visualization operation failed."
    core.json_response(handler, status, {"ok": False, "code": code, "error": message})


def handle_get(handler: Any, core: Any, path: str) -> bool:
    match = _VISUALIZATION_PATH.fullmatch(path)
    if match is None:
        return False
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    try:
        options = normalize_query(query)
        result = _service(core).visualization(
            match.group(1),
            limit=options["limit"],
            include_future=options["includeFuture"],
        )
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
