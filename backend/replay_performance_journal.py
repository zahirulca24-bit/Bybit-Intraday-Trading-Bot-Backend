"""Read-only performance analytics and journal APIs for Historical Replay.

All values come from PostgreSQL replay sessions, trades, and events. This module
has no exchange client, credentials, private route, or order-submission ability.
"""
from __future__ import annotations

import math
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

try:
    from .replay_storage import _normalized_identifier, _session_row
except ImportError:
    from replay_storage import _normalized_identifier, _session_row

MONEY_QUANTUM = Decimal("0.00000001")
PERCENT_QUANTUM = Decimal("0.0001")
R_QUANTUM = Decimal("0.0001")
DEFAULT_JOURNAL_LIMIT = 50
MAX_JOURNAL_LIMIT = 200
DEFAULT_TRADE_LIMIT = 50
MAX_TRADE_LIMIT = 200
DEFAULT_CURVE_LIMIT = 200
MAX_CURVE_LIMIT = 1000

_IDENTIFIER = r"[A-Za-z0-9_-]{8,80}"
_PERFORMANCE_PATH = re.compile(rf"^/api/replay/sessions/({_IDENTIFIER})/performance$")
_JOURNAL_PATH = re.compile(rf"^/api/replay/sessions/({_IDENTIFIER})/journal$")
_EVENT_TYPE_PATTERN = re.compile(r"[a-z0-9_.-]{2,80}")
_JOURNAL_CATEGORIES = frozenset(
    {"all", "session", "step", "candle", "strategy", "risk", "execution", "trade", "pnl"}
)
_TRADE_STATUSES = frozenset({"OPEN", "CLOSED", "CANCELLED"})


class ReplayAnalyticsError(RuntimeError):
    pass


class ReplayAnalyticsValidationError(ReplayAnalyticsError):
    pass


class ReplayAnalyticsNotFoundError(ReplayAnalyticsError):
    pass


class ReplayAnalyticsStoreError(ReplayAnalyticsError):
    pass


def _decimal(value: Any, field: str, *, default: Any | None = None) -> Decimal:
    candidate = default if value in (None, "") else value
    try:
        result = Decimal(str(candidate))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplayAnalyticsStoreError(f"Stored {field} is not numeric.") from exc
    if not result.is_finite():
        raise ReplayAnalyticsStoreError(f"Stored {field} is not finite.")
    return result


def _q(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)


def _money(value: Decimal) -> str:
    return format(_q(value), ".8f")


def _pct(value: Decimal) -> str:
    return format(value.quantize(PERCENT_QUANTUM, rounding=ROUND_DOWN), ".4f")


def _r(value: Decimal) -> str:
    return format(value.quantize(R_QUANTUM, rounding=ROUND_DOWN), ".4f")


def _bounded_integer(
    value: Any,
    field: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    candidate = default if value in (None, "") else value
    try:
        result = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ReplayAnalyticsValidationError(f"{field} must be an integer.") from exc
    if not minimum <= result <= maximum:
        raise ReplayAnalyticsValidationError(
            f"{field} must be between {minimum} and {maximum}."
        )
    return result


def _optional_sequence(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplayAnalyticsValidationError("cursorSequence must be an integer.") from exc
    if result < 0:
        raise ReplayAnalyticsValidationError("cursorSequence cannot be negative.")
    return result


def _boolean(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ReplayAnalyticsValidationError("Boolean analytics option is invalid.")


def _direction(value: Any) -> str:
    result = str(value or "desc").strip().lower()
    if result not in {"asc", "desc"}:
        raise ReplayAnalyticsValidationError("direction must be asc or desc.")
    return result


def _event_type(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    result = str(value).strip().lower()
    if not _EVENT_TYPE_PATTERN.fullmatch(result):
        raise ReplayAnalyticsValidationError("eventType is invalid.")
    return result


def _category(value: Any) -> str:
    result = str(value or "all").strip().lower()
    if result not in _JOURNAL_CATEGORIES:
        raise ReplayAnalyticsValidationError(
            "category must be one of: all, session, step, candle, strategy, risk, execution, trade, pnl."
        )
    return result


def _trade_status(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    result = str(value).strip().upper()
    if result not in _TRADE_STATUSES:
        raise ReplayAnalyticsValidationError("tradeStatus must be OPEN, CLOSED, or CANCELLED.")
    return result


def normalize_performance_query(query: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(query or {})
    return {
        "includeEquityCurve": _boolean(data.get("includeEquityCurve"), default=True),
        "curveLimit": _bounded_integer(
            data.get("curveLimit"),
            "curveLimit",
            default=DEFAULT_CURVE_LIMIT,
            minimum=2,
            maximum=MAX_CURVE_LIMIT,
        ),
    }


def normalize_journal_query(query: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(query or {})
    return {
        "limit": _bounded_integer(
            data.get("limit"),
            "limit",
            default=DEFAULT_JOURNAL_LIMIT,
            minimum=1,
            maximum=MAX_JOURNAL_LIMIT,
        ),
        "direction": _direction(data.get("direction")),
        "cursorSequence": _optional_sequence(data.get("cursorSequence")),
        "eventType": _event_type(data.get("eventType")),
        "category": _category(data.get("category")),
        "includePayload": _boolean(data.get("includePayload"), default=True),
        "includeTrades": _boolean(data.get("includeTrades"), default=True),
        "tradeStatus": _trade_status(data.get("tradeStatus")),
        "tradeLimit": _bounded_integer(
            data.get("tradeLimit"),
            "tradeLimit",
            default=DEFAULT_TRADE_LIMIT,
            minimum=1,
            maximum=MAX_TRADE_LIMIT,
        ),
    }


def require_store(store: Any) -> Any:
    if store is None or not hasattr(store, "lock") or not callable(getattr(store, "connect", None)):
        raise ReplayAnalyticsStoreError("Persistent PostgreSQL replay storage is unavailable.")
    status = getattr(store, "status", None)
    if callable(status):
        snapshot = dict(status() or {})
        if not snapshot.get("ok") or snapshot.get("degraded"):
            raise ReplayAnalyticsStoreError(
                snapshot.get("error") or "Persistent PostgreSQL replay storage is degraded."
            )
    return store


def equity_sample_indexes(mark_count: int, curve_limit: int) -> set[int]:
    """Return deterministic, first/last-inclusive indexes bounded by curve_limit."""
    count = max(0, int(mark_count))
    limit = max(2, int(curve_limit))
    if count <= limit:
        return set(range(count))
    return {(index * (count - 1)) // (limit - 1) for index in range(limit)}


def _trade_row(row: Sequence[Any]) -> dict[str, Any]:
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
        "createdAt": int(row[12]),
        "updatedAt": int(row[13]),
    }


def calculate_trade_metrics(
    trades: Sequence[Mapping[str, Any]], session: Mapping[str, Any]
) -> dict[str, Any]:
    ordered = sorted(
        (dict(trade) for trade in trades),
        key=lambda trade: (
            int(trade.get("exitTime") or 9_999_999_999_999),
            int(trade.get("entryTime") or 0),
            str(trade.get("tradeId") or ""),
        ),
    )
    closed = [trade for trade in ordered if str(trade.get("status")).upper() == "CLOSED"]
    opened = [trade for trade in ordered if str(trade.get("status")).upper() == "OPEN"]
    cancelled = [trade for trade in ordered if str(trade.get("status")).upper() == "CANCELLED"]
    pnls = [_decimal(row.get("realizedPnl"), "trade realizedPnl", default=0) for row in closed]
    fees = sum(
        (_decimal(row.get("fees"), "trade fees", default=0) for row in ordered),
        Decimal("0"),
    )
    gross_profit = sum((pnl for pnl in pnls if pnl > 0), Decimal("0"))
    gross_loss = -sum((pnl for pnl in pnls if pnl < 0), Decimal("0"))
    net_realized = sum(pnls, Decimal("0"))
    wins = sum(pnl > 0 for pnl in pnls)
    losses = sum(pnl < 0 for pnl in pnls)
    breakeven = sum(pnl == 0 for pnl in pnls)
    closed_count = len(closed)
    win_rate = Decimal(wins) * 100 / Decimal(closed_count) if closed_count else Decimal("0")
    expectancy = net_realized / Decimal(closed_count) if closed_count else Decimal("0")
    average_win = gross_profit / Decimal(wins) if wins else Decimal("0")
    average_loss = gross_loss / Decimal(losses) if losses else Decimal("0")

    if gross_loss > 0:
        profit_factor: str | None = _r(gross_profit / gross_loss)
        profit_factor_status = "finite"
    elif gross_profit > 0:
        profit_factor = None
        profit_factor_status = "no_losses"
    else:
        profit_factor = None
        profit_factor_status = "no_closed_profit_or_loss"

    r_values: list[Decimal] = []
    durations: list[int] = []
    for row, pnl in zip(closed, pnls):
        payload = dict(row.get("payload") or {}) if isinstance(row.get("payload"), Mapping) else {}
        if payload.get("riskAmount") not in (None, ""):
            risk_amount = _decimal(payload["riskAmount"], "trade riskAmount")
            if risk_amount > 0:
                r_values.append(pnl / risk_amount)
        entry_time = int(row.get("entryTime") or 0)
        exit_time = int(row.get("exitTime") or entry_time)
        if exit_time >= entry_time:
            durations.append(exit_time - entry_time)

    total_r = sum(r_values, Decimal("0"))
    average_r = total_r / Decimal(len(r_values)) if r_values else Decimal("0")
    win_run = loss_run = max_wins = max_losses = 0
    for pnl in pnls:
        if pnl > 0:
            win_run += 1
            loss_run = 0
            max_wins = max(max_wins, win_run)
        elif pnl < 0:
            loss_run += 1
            win_run = 0
            max_losses = max(max_losses, loss_run)
        else:
            win_run = loss_run = 0

    best = max(closed, key=lambda row: _decimal(row.get("realizedPnl"), "trade realizedPnl", default=0)) if closed else None
    worst = min(closed, key=lambda row: _decimal(row.get("realizedPnl"), "trade realizedPnl", default=0)) if closed else None
    initial = _decimal(session.get("initialBalance"), "session initialBalance", default=0)
    balance = _decimal(session.get("balance"), "session balance", default=initial)
    equity = _decimal(session.get("equity"), "session equity", default=balance)

    def compact_trade(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "tradeId": row.get("tradeId"),
            "realizedPnl": str(row.get("realizedPnl")),
            "exitTime": row.get("exitTime"),
        }

    return {
        "totalTrades": len(ordered),
        "closedTrades": closed_count,
        "openTrades": len(opened),
        "cancelledTrades": len(cancelled),
        "winningTrades": wins,
        "losingTrades": losses,
        "breakevenTrades": breakeven,
        "longTrades": sum(str(row.get("side")) == "Buy" for row in ordered),
        "shortTrades": sum(str(row.get("side")) == "Sell" for row in ordered),
        "winRatePct": _pct(win_rate),
        "grossProfit": _money(gross_profit),
        "grossLoss": _money(gross_loss),
        "netRealizedPnl": _money(net_realized),
        "feesPaid": _money(fees),
        "expectancy": _money(expectancy),
        "averageWin": _money(average_win),
        "averageLoss": _money(average_loss),
        "profitFactor": profit_factor,
        "profitFactorStatus": profit_factor_status,
        "totalR": _r(total_r),
        "averageR": _r(average_r),
        "rSampleTrades": len(r_values),
        "averageTradeDurationMs": int(sum(durations) / len(durations)) if durations else 0,
        "maxConsecutiveWins": max_wins,
        "maxConsecutiveLosses": max_losses,
        "bestTrade": compact_trade(best),
        "worstTrade": compact_trade(worst),
        "initialBalance": _money(initial),
        "balance": _money(balance),
        "equity": _money(equity),
        "netPnl": _money(balance - initial),
        "equityPnl": _money(equity - initial),
    }


def _new_drawdown(initial_equity: Any, initial_time: int | None) -> dict[str, Any]:
    initial = _decimal(initial_equity, "initial equity", default=0)
    return {
        "peak": initial,
        "current": initial,
        "peakTime": initial_time,
        "maxAmount": Decimal("0"),
        "maxPct": Decimal("0"),
        "maxPeakTime": initial_time,
        "maxTroughTime": initial_time,
    }


def _advance_drawdown(state: dict[str, Any], equity: Decimal, point_time: int | None) -> None:
    state["current"] = equity
    if equity > state["peak"]:
        state["peak"] = equity
        state["peakTime"] = point_time
    amount = state["peak"] - equity
    percent = amount * 100 / state["peak"] if state["peak"] > 0 else Decimal("0")
    if amount > state["maxAmount"] or (amount == state["maxAmount"] and percent > state["maxPct"]):
        state["maxAmount"] = amount
        state["maxPct"] = percent
        state["maxPeakTime"] = state["peakTime"]
        state["maxTroughTime"] = point_time


def _drawdown_result(state: Mapping[str, Any]) -> dict[str, Any]:
    current_amount = state["peak"] - state["current"]
    current_pct = current_amount * 100 / state["peak"] if state["peak"] > 0 else Decimal("0")
    return {
        "maxDrawdown": _money(state["maxAmount"]),
        "maxDrawdownPct": _pct(state["maxPct"]),
        "maxDrawdownPeakTime": state["maxPeakTime"],
        "maxDrawdownTroughTime": state["maxTroughTime"],
        "currentDrawdown": _money(current_amount),
        "currentDrawdownPct": _pct(current_pct),
        "highWaterEquity": _money(state["peak"]),
    }


def calculate_drawdown(
    initial_equity: Any,
    points: Sequence[Mapping[str, Any]],
    *,
    initial_time: int | None = None,
) -> dict[str, Any]:
    state = _new_drawdown(initial_equity, initial_time)
    for point in points:
        point_time = point.get("candleOpenTime")
        _advance_drawdown(
            state,
            _decimal(point.get("equity"), "equity mark"),
            int(point_time) if point_time is not None else None,
        )
    return _drawdown_result(state)


class ReplayPerformanceJournalService:
    def __init__(self, store: Any):
        self.store = store

    def _store(self) -> Any:
        return require_store(self.store)

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
            raise ReplayAnalyticsNotFoundError("Replay session was not found.")
        return session

    def performance(
        self,
        session_id: str,
        *,
        include_equity_curve: bool = True,
        curve_limit: int = DEFAULT_CURVE_LIMIT,
    ) -> dict[str, Any]:
        limit = _bounded_integer(
            curve_limit,
            "curveLimit",
            default=DEFAULT_CURVE_LIMIT,
            minimum=2,
            maximum=MAX_CURVE_LIMIT,
        )
        store = self._store()
        with store.lock, store.connect() as db:
            with db.cursor() as cur:
                session = self._session(cur, session_id)
                cur.execute(
                    "SELECT trade_id,symbol,side,status,entry_time,exit_time,entry_price,"
                    "exit_price,quantity,realized_pnl,fees,payload,created_at,updated_at "
                    "FROM replay_trades WHERE session_id=%s ORDER BY entry_time ASC,trade_id ASC",
                    (session["sessionId"],),
                )
                trades = [_trade_row(row) for row in cur.fetchall()]
                metrics = calculate_trade_metrics(trades, session)
                cur.execute(
                    "SELECT COUNT(*) FROM replay_events WHERE session_id=%s AND event_type='pnl.marked'",
                    (session["sessionId"],),
                )
                mark_count = int(cur.fetchone()[0] or 0)
                sample_indexes = equity_sample_indexes(mark_count, limit) if include_equity_curve else set()
                stride = (
                    max(1, math.ceil((mark_count - 1) / (limit - 1)))
                    if include_equity_curve and mark_count > limit
                    else 1 if include_equity_curve else None
                )
                cur.execute(
                    "SELECT sequence_no,candle_open_time,payload,created_at FROM replay_events "
                    "WHERE session_id=%s AND event_type='pnl.marked' ORDER BY sequence_no ASC",
                    (session["sessionId"],),
                )
                drawdown_state = _new_drawdown(session["initialBalance"], session["startTime"])
                sampled: list[dict[str, Any]] = []
                ignored_marks = 0
                last_valid: dict[str, Any] | None = None
                for index, row in enumerate(cur):
                    payload = row[2] if isinstance(row[2], dict) else {}
                    try:
                        equity = _decimal(payload.get("equity"), "equity mark")
                    except ReplayAnalyticsStoreError:
                        ignored_marks += 1
                        continue
                    candle_time = int(row[1]) if row[1] is not None else None
                    point = {
                        "sequenceNo": int(row[0]),
                        "candleOpenTime": candle_time,
                        "balance": str(payload.get("balance")) if payload.get("balance") is not None else None,
                        "equity": _money(equity),
                        "unrealizedPnl": str(payload.get("unrealizedPnl")) if payload.get("unrealizedPnl") is not None else None,
                        "createdAt": int(row[3]),
                    }
                    _advance_drawdown(drawdown_state, equity, candle_time)
                    last_valid = point
                    if include_equity_curve and index in sample_indexes:
                        sampled.append(point)
                if include_equity_curve and last_valid is not None:
                    if not sampled:
                        sampled.append(last_valid)
                    elif sampled[-1]["sequenceNo"] != last_valid["sequenceNo"]:
                        if len(sampled) >= limit:
                            sampled[-1] = last_valid
                        else:
                            sampled.append(last_valid)

        drawdown = _drawdown_result(drawdown_state)
        max_drawdown = _decimal(drawdown["maxDrawdown"], "max drawdown", default=0)
        net_realized = _decimal(metrics["netRealizedPnl"], "net realized PnL", default=0)
        is_final = session["status"] == "COMPLETED" and metrics["openTrades"] == 0
        return {
            "ok": True,
            "sessionId": session["sessionId"],
            "symbol": session["symbol"],
            "timeframe": session["timeframe"],
            "sessionStatus": session["status"],
            "asOfCursorTime": session.get("cursorTime"),
            "isFinal": is_final,
            "metrics": {
                **metrics,
                **drawdown,
                "recoveryFactor": _r(net_realized / max_drawdown) if max_drawdown > 0 else None,
            },
            "metricBasis": {
                "realizedPnl": "persisted replay trade realized_pnl net of trade fees",
                "drawdown": "persisted pnl.marked equity peak-to-trough",
                "rMultiple": "realized_pnl divided by planned riskAmount",
                "currency": "USDT",
            },
            "equityCurve": sampled if include_equity_curve else [],
            "equityCurveMeta": {
                "included": bool(include_equity_curve),
                "totalMarks": mark_count,
                "returnedPoints": len(sampled) if include_equity_curve else 0,
                "ignoredMalformedMarks": ignored_marks,
                "samplingStride": stride,
                "limit": limit,
            },
            "performanceSummaryImplemented": True,
            "replayJournalImplemented": True,
            "externalExecutionAllowed": False,
        }

    def journal(self, session_id: str, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
        options = normalize_journal_query(query)
        store = self._store()
        with store.lock, store.connect() as db:
            with db.cursor() as cur:
                session = self._session(cur, session_id)
                clauses = ["session_id=%s"]
                params: list[Any] = [session["sessionId"]]
                cursor_sequence = options["cursorSequence"]
                direction = options["direction"]
                if cursor_sequence is not None:
                    clauses.append("sequence_no>%s" if direction == "asc" else "sequence_no<%s")
                    params.append(cursor_sequence)
                if options["eventType"] is not None:
                    clauses.append("event_type=%s")
                    params.append(options["eventType"])
                elif options["category"] != "all":
                    clauses.append("event_type LIKE %s")
                    params.append(f"{options['category']}.%")
                params.append(options["limit"] + 1)
                cur.execute(
                    "SELECT sequence_no,event_type,candle_open_time,payload,created_at "
                    f"FROM replay_events WHERE {' AND '.join(clauses)} "
                    f"ORDER BY sequence_no {'ASC' if direction == 'asc' else 'DESC'} LIMIT %s",
                    tuple(params),
                )
                rows = cur.fetchall()
                has_more = len(rows) > options["limit"]
                entries = []
                for row in rows[: options["limit"]]:
                    entry = {
                        "sequenceNo": int(row[0]),
                        "eventType": row[1],
                        "candleOpenTime": int(row[2]) if row[2] is not None else None,
                        "createdAt": int(row[4]),
                    }
                    if options["includePayload"]:
                        entry["payload"] = row[3] if isinstance(row[3], dict) else {}
                    entries.append(entry)
                cur.execute(
                    "SELECT COUNT(*),MIN(sequence_no),MAX(sequence_no) FROM replay_events WHERE session_id=%s",
                    (session["sessionId"],),
                )
                event_stats = cur.fetchone()

                trades: list[dict[str, Any]] = []
                if options["includeTrades"]:
                    trade_clauses = ["session_id=%s"]
                    trade_params: list[Any] = [session["sessionId"]]
                    if options["tradeStatus"] is not None:
                        trade_clauses.append("status=%s")
                        trade_params.append(options["tradeStatus"])
                    trade_params.append(options["tradeLimit"])
                    cur.execute(
                        "SELECT trade_id,symbol,side,status,entry_time,exit_time,entry_price,"
                        "exit_price,quantity,realized_pnl,fees,payload,created_at,updated_at "
                        f"FROM replay_trades WHERE {' AND '.join(trade_clauses)} "
                        "ORDER BY entry_time DESC,trade_id DESC LIMIT %s",
                        tuple(trade_params),
                    )
                    trades = [_trade_row(row) for row in cur.fetchall()]
                cur.execute(
                    "SELECT COUNT(*),COUNT(*) FILTER (WHERE status='OPEN'),"
                    "COUNT(*) FILTER (WHERE status='CLOSED'),"
                    "COUNT(*) FILTER (WHERE status='CANCELLED') "
                    "FROM replay_trades WHERE session_id=%s",
                    (session["sessionId"],),
                )
                trade_stats = cur.fetchone()

        return {
            "ok": True,
            "session": session,
            "entries": entries,
            "trades": trades,
            "pagination": {
                "direction": direction,
                "cursorSequence": cursor_sequence,
                "nextCursorSequence": entries[-1]["sequenceNo"] if has_more and entries else None,
                "hasMore": has_more,
                "limit": options["limit"],
            },
            "filters": {
                "eventType": options["eventType"],
                "category": options["category"],
                "includePayload": options["includePayload"],
                "includeTrades": options["includeTrades"],
                "tradeStatus": options["tradeStatus"],
                "tradeLimit": options["tradeLimit"],
            },
            "journalSummary": {
                "totalEvents": int(event_stats[0] or 0),
                "firstSequence": int(event_stats[1]) if event_stats[1] is not None else None,
                "lastSequence": int(event_stats[2]) if event_stats[2] is not None else None,
                "totalTrades": int(trade_stats[0] or 0),
                "openTrades": int(trade_stats[1] or 0),
                "closedTrades": int(trade_stats[2] or 0),
                "cancelledTrades": int(trade_stats[3] or 0),
            },
            "performanceSummaryImplemented": True,
            "replayJournalImplemented": True,
            "externalExecutionAllowed": False,
        }


def _mark_capabilities(result: Any) -> Any:
    if isinstance(result, dict):
        result["performanceSummaryImplemented"] = True
        result["replayJournalImplemented"] = True
    return result


def _decorate_session_service(core: Any) -> None:
    service = getattr(core, "_replay_session_service", None)
    if service is None or getattr(service, "_performance_journal_decorated", False):
        return
    for name in ("start", "get", "reset", "list", "_existing_response"):
        original = getattr(service, name, None)
        if not callable(original):
            continue

        def wrapped(*args: Any, _original=original, **kwargs: Any) -> Any:
            return _mark_capabilities(_original(*args, **kwargs))

        setattr(service, name, wrapped)
    service._performance_journal_decorated = True


def install(core: Any) -> ReplayPerformanceJournalService:
    _decorate_session_service(core)
    existing = getattr(core, "_replay_performance_journal_service", None)
    if isinstance(existing, ReplayPerformanceJournalService):
        return existing
    service = ReplayPerformanceJournalService(getattr(core, "_durable_state_store", None))
    core._replay_performance_journal_service = service
    return service


def _service(core: Any) -> ReplayPerformanceJournalService:
    current = getattr(core, "_replay_performance_journal_service", None)
    return current if isinstance(current, ReplayPerformanceJournalService) else install(core)


def is_get_path(path: str) -> bool:
    return bool(_PERFORMANCE_PATH.fullmatch(path) or _JOURNAL_PATH.fullmatch(path))


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    if isinstance(exc, ReplayAnalyticsValidationError):
        status, code = 400, "REPLAY_ANALYTICS_INVALID"
    elif isinstance(exc, ReplayAnalyticsNotFoundError):
        status, code = 404, "REPLAY_SESSION_NOT_FOUND"
    elif isinstance(exc, ReplayAnalyticsStoreError):
        status, code = 503, "REPLAY_STORAGE_UNAVAILABLE"
    else:
        status, code = 500, "REPLAY_ANALYTICS_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Historical replay analytics operation failed."
    core.json_response(handler, status, {"ok": False, "code": code, "error": message})


def handle_get(handler: Any, core: Any, path: str) -> bool:
    performance_match = _PERFORMANCE_PATH.fullmatch(path)
    journal_match = _JOURNAL_PATH.fullmatch(path)
    if performance_match is None and journal_match is None:
        return False
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    try:
        if performance_match is not None:
            options = normalize_performance_query(query)
            result = _service(core).performance(
                performance_match.group(1),
                include_equity_curve=options["includeEquityCurve"],
                curve_limit=options["curveLimit"],
            )
        else:
            result = _service(core).journal(journal_match.group(1), query)
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
