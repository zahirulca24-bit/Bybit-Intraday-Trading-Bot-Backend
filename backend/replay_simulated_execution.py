"""Deterministic simulated execution for Historical Replay.

The module reads only frozen replay candles and writes only PostgreSQL replay
state. It has no exchange client, API credentials, private route, or external
order capability.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

try:
    from .replay_storage import _jsonb, _session_row
except ImportError:
    from replay_storage import _jsonb, _session_row


MONEY_QUANTUM = Decimal("0.00000001")
BPS_DIVISOR = Decimal("10000")
DEFAULT_FEE_BPS = Decimal("6")
MAX_FEE_BPS = Decimal("100")
DEFAULT_MAX_LEVERAGE = Decimal("3")
MAX_LEVERAGE = Decimal("10")
SAME_CANDLE_POLICY = "stop_first"
LIMITED_LIABILITY_POLICY = "account_balance_floor_zero"


class ReplaySimulationError(RuntimeError):
    """Raised when replay execution cannot be calculated or persisted safely."""


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplaySimulationError(f"{field} must be numeric.") from exc
    if not result.is_finite():
        raise ReplaySimulationError(f"{field} must be finite.")
    if positive and result <= 0:
        raise ReplaySimulationError(f"{field} must be greater than zero.")
    return result


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)


def _config_decimal(
    config: Mapping[str, Any],
    keys: Sequence[str],
    default: Decimal,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    raw: Any = None
    for key in keys:
        if key in config and config.get(key) not in (None, ""):
            raw = config.get(key)
            break
    value = default if raw is None else _decimal(raw, keys[0])
    if value < minimum or value > maximum:
        raise ReplaySimulationError(
            f"{keys[0]} must be between {minimum} and {maximum}."
        )
    return value


def execution_config(session: Mapping[str, Any]) -> dict[str, Decimal]:
    """Validate bounded, immutable execution assumptions."""

    config = session.get("config")
    config = dict(config) if isinstance(config, Mapping) else {}
    fee_bps = _config_decimal(
        config,
        ("replayFeeBps", "takerFeeBps", "feeBps"),
        DEFAULT_FEE_BPS,
        minimum=Decimal("0"),
        maximum=MAX_FEE_BPS,
    )
    max_leverage = _config_decimal(
        config,
        ("maxLeverage", "replayMaxLeverage"),
        DEFAULT_MAX_LEVERAGE,
        minimum=Decimal("1"),
        maximum=MAX_LEVERAGE,
    )
    return {"feeBps": fee_bps, "maxLeverage": max_leverage}


def _fee(price: Decimal, quantity: Decimal, fee_bps: Decimal) -> Decimal:
    return _quantize(price * quantity * fee_bps / BPS_DIVISOR)


def _trade_id(session_id: str, candle_open_time: int) -> str:
    digest = hashlib.sha256(
        f"{session_id}:{int(candle_open_time)}".encode("utf-8")
    ).hexdigest()[:32]
    return f"sim_{digest}"


def _gross_pnl(
    side: str,
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: Decimal,
) -> Decimal:
    direction = Decimal("1") if side == "Buy" else Decimal("-1")
    return _quantize((exit_price - entry_price) * quantity * direction)


def _limited_liability_delta(
    raw_balance_delta: Decimal,
    available_balance: Decimal,
) -> tuple[Decimal, Decimal, bool]:
    """Cap account loss at available balance and expose the adjustment explicitly."""

    available = max(Decimal("0"), _quantize(available_balance))
    floor_delta = -available
    applied = max(_quantize(raw_balance_delta), floor_delta)
    adjustment = _quantize(applied - raw_balance_delta)
    return applied, adjustment, adjustment > 0


def _trade_from_row(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None
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


def _load_open_trade(cursor: Any, session_id: str) -> dict[str, Any] | None:
    cursor.execute(
        "SELECT trade_id,symbol,side,status,entry_time,exit_time,entry_price,"
        "exit_price,quantity,realized_pnl,fees,payload "
        "FROM replay_trades WHERE session_id=%s AND status='OPEN' "
        "ORDER BY entry_time ASC,trade_id ASC LIMIT 1 FOR UPDATE",
        (session_id,),
    )
    return _trade_from_row(cursor.fetchone())


def _exit_decision(
    trade: Mapping[str, Any],
    candle: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve SL/TP conservatively; entry-candle exits are impossible."""

    payload = trade.get("payload")
    payload = dict(payload) if isinstance(payload, Mapping) else {}
    stop = _decimal(payload.get("stopLoss"), "stopLoss", positive=True)
    target = _decimal(payload.get("takeProfit"), "takeProfit", positive=True)
    candle_time = int(candle["openTime"])
    if candle_time <= int(trade["entryTime"]):
        return None

    side = str(trade["side"])
    open_price = _decimal(candle["open"], "candle.open", positive=True)
    high = _decimal(candle["high"], "candle.high", positive=True)
    low = _decimal(candle["low"], "candle.low", positive=True)

    if side == "Buy":
        stop_hit, target_hit = low <= stop, high >= target
        if stop_hit:
            return {
                "reason": "stop_loss",
                "price": open_price if open_price < stop else stop,
                "sameCandleConflict": bool(target_hit),
            }
        if target_hit:
            return {
                "reason": "take_profit",
                "price": target,
                "sameCandleConflict": False,
            }
    else:
        stop_hit, target_hit = high >= stop, low <= target
        if stop_hit:
            return {
                "reason": "stop_loss",
                "price": open_price if open_price > stop else stop,
                "sameCandleConflict": bool(target_hit),
            }
        if target_hit:
            return {
                "reason": "take_profit",
                "price": target,
                "sameCandleConflict": False,
            }
    return None


def _event(
    cursor: Any,
    *,
    session_id: str,
    sequence: int,
    event_type: str,
    candle_open_time: int | None,
    payload: Mapping[str, Any],
    created_at: int,
) -> int:
    cursor.execute(
        "INSERT INTO replay_events("
        "session_id,sequence_no,event_type,candle_open_time,payload,created_at"
        ") VALUES(%s,%s,%s,%s,%s,%s)",
        (
            session_id,
            sequence,
            event_type,
            candle_open_time,
            _jsonb(dict(payload)),
            created_at,
        ),
    )
    return sequence + 1


def _close_trade(
    cursor: Any,
    *,
    session_id: str,
    trade: Mapping[str, Any],
    candle: Mapping[str, Any],
    exit_price: Decimal,
    reason: str,
    fee_bps: Decimal,
    available_balance: Decimal,
    same_candle_conflict: bool,
    now: int,
) -> tuple[dict[str, Any], Decimal, Decimal]:
    entry_price = _decimal(trade["entryPrice"], "entryPrice", positive=True)
    quantity = _decimal(trade["quantity"], "quantity", positive=True)
    entry_fees = _decimal(trade["fees"], "entry fees")
    raw_gross = _gross_pnl(str(trade["side"]), entry_price, exit_price, quantity)
    exit_fee = _fee(exit_price, quantity, fee_bps)
    raw_balance_delta = _quantize(raw_gross - exit_fee)
    balance_delta, liquidation_adjustment, limited = _limited_liability_delta(
        raw_balance_delta, available_balance
    )
    effective_gross = _quantize(raw_gross + liquidation_adjustment)
    total_fees = _quantize(entry_fees + exit_fee)
    net = _quantize(balance_delta - entry_fees)

    payload = dict(trade.get("payload") or {})
    payload.update(
        {
            "exitReason": reason,
            "exitCandleOpenTime": int(candle["openTime"]),
            "exitFee": str(exit_fee),
            "grossPnl": str(raw_gross),
            "effectiveGrossPnl": str(effective_gross),
            "rawBalanceDelta": str(raw_balance_delta),
            "appliedBalanceDelta": str(balance_delta),
            "availableBalanceBeforeExit": str(_quantize(available_balance)),
            "liquidationAdjustment": str(liquidation_adjustment),
            "limitedLiabilityApplied": limited,
            "limitedLiabilityPolicy": LIMITED_LIABILITY_POLICY,
            "netPnl": str(net),
            "sameCandleConflict": bool(same_candle_conflict),
            "sameCandlePolicy": SAME_CANDLE_POLICY,
        }
    )
    cursor.execute(
        "UPDATE replay_trades SET status='CLOSED',exit_time=%s,exit_price=%s,"
        "realized_pnl=%s,fees=%s,payload=%s,updated_at=%s "
        "WHERE session_id=%s AND trade_id=%s AND status='OPEN'",
        (
            int(candle["openTime"]),
            _quantize(exit_price),
            net,
            total_fees,
            _jsonb(payload),
            now,
            session_id,
            trade["tradeId"],
        ),
    )
    if cursor.rowcount != 1:
        raise ReplaySimulationError("Open replay trade could not be closed atomically.")

    closed = dict(trade)
    closed.update(
        {
            "status": "CLOSED",
            "exitTime": int(candle["openTime"]),
            "exitPrice": str(_quantize(exit_price)),
            "realizedPnl": str(net),
            "fees": str(total_fees),
            "payload": payload,
        }
    )
    return closed, balance_delta, exit_fee


def _open_trade(
    cursor: Any,
    *,
    session: Mapping[str, Any],
    decision: Mapping[str, Any],
    candle: Mapping[str, Any],
    balance: Decimal,
    equity: Decimal,
    fee_bps: Decimal,
    max_leverage: Decimal,
    request_id: str,
    now: int,
) -> tuple[dict[str, Any] | None, Decimal, str | None]:
    risk = decision.get("risk")
    if not decision.get("eligible") or not isinstance(risk, Mapping):
        return None, Decimal("0"), "not_eligible"
    side = str(decision.get("signal") or "")
    if side not in {"Buy", "Sell"}:
        return None, Decimal("0"), "invalid_side"

    entry = _decimal(candle["close"], "entryPrice", positive=True)
    stop = _decimal(risk.get("stopLoss"), "stopLoss", positive=True)
    target = _decimal(risk.get("takeProfit"), "takeProfit", positive=True)
    if side == "Buy" and not stop < entry < target:
        return None, Decimal("0"), "invalid_protection"
    if side == "Sell" and not target < entry < stop:
        return None, Decimal("0"), "invalid_protection"

    stop_distance = abs(entry - stop)
    risk_pct = _decimal(risk.get("riskPct"), "riskPct", positive=True)
    risk_amount = _quantize(equity * risk_pct / Decimal("100"))
    risk_quantity = _quantize(risk_amount / stop_distance)
    leverage_quantity = _quantize(equity * max_leverage / entry)
    quantity = min(risk_quantity, leverage_quantity)
    if quantity <= 0:
        return None, Decimal("0"), "quantity_zero"

    entry_fee = _fee(entry, quantity, fee_bps)
    if entry_fee >= balance:
        return None, Decimal("0"), "insufficient_balance_for_fee"

    session_id = str(session["sessionId"])
    trade_id = _trade_id(session_id, int(candle["openTime"]))
    payload = {
        "grade": decision.get("grade"),
        "strategyMode": decision.get("strategyMode"),
        "riskPct": str(risk_pct),
        "riskAmount": str(risk_amount),
        "stopLoss": str(_quantize(stop)),
        "takeProfit": str(_quantize(target)),
        "stopDistance": str(_quantize(stop_distance)),
        "rewardRisk": str(risk.get("rewardRisk") or "2"),
        "entryFee": str(entry_fee),
        "feeBps": str(fee_bps),
        "maxLeverage": str(max_leverage),
        "entryRequestId": request_id,
        "entryCandleOpenTime": int(candle["openTime"]),
        "fillModel": "candle_close",
        "sameCandlePolicy": SAME_CANDLE_POLICY,
        "limitedLiabilityPolicy": LIMITED_LIABILITY_POLICY,
        "externalExecutionAllowed": False,
    }
    cursor.execute(
        "INSERT INTO replay_trades("
        "trade_id,session_id,symbol,side,status,entry_time,exit_time,entry_price,"
        "exit_price,quantity,realized_pnl,fees,payload,created_at,updated_at"
        ") VALUES(%s,%s,%s,%s,'OPEN',%s,NULL,%s,NULL,%s,0,%s,%s,%s,%s)",
        (
            trade_id,
            session_id,
            session["symbol"],
            side,
            int(candle["openTime"]),
            _quantize(entry),
            quantity,
            entry_fee,
            _jsonb(payload),
            now,
            now,
        ),
    )
    return (
        {
            "tradeId": trade_id,
            "symbol": session["symbol"],
            "side": side,
            "status": "OPEN",
            "entryTime": int(candle["openTime"]),
            "exitTime": None,
            "entryPrice": str(_quantize(entry)),
            "exitPrice": None,
            "quantity": str(quantity),
            "realizedPnl": "0",
            "fees": str(entry_fee),
            "payload": payload,
        },
        entry_fee,
        None,
    )


def _marked_equity(
    balance: Decimal,
    trade: Mapping[str, Any] | None,
    close_price: Decimal,
) -> tuple[Decimal, Decimal]:
    if trade is None:
        return _quantize(balance), Decimal("0")
    entry = _decimal(trade["entryPrice"], "entryPrice", positive=True)
    quantity = _decimal(trade["quantity"], "quantity", positive=True)
    unrealized = _gross_pnl(str(trade["side"]), entry, close_price, quantity)
    return _quantize(max(Decimal("0"), balance + unrealized)), unrealized


def _execution_complete(payload: Any) -> bool:
    return (
        isinstance(payload, Mapping)
        and payload.get("executionEnrichmentComplete") is True
        and isinstance(payload.get("execution"), Mapping)
    )


def enrich_step(store: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Persist fills, exits, fees, PnL and the idempotent response atomically."""

    response = dict(result)
    session = dict(response.get("session") or {})
    candles = list(response.get("candles") or [])
    session_id = str(session.get("sessionId") or "")
    request_id = str(response.get("requestId") or "")
    if not session_id or not request_id or not candles:
        return response
    if _execution_complete(response):
        return response
    if response.get("strategyEnrichmentComplete") is not True:
        raise ReplaySimulationError(
            "Strategy/risk enrichment must complete before simulated execution."
        )

    now = int(time.time())
    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at "
                "FROM replay_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
            locked_session = _session_row(cur.fetchone())
            if locked_session is None:
                raise ReplaySimulationError(
                    "Replay session disappeared before simulated execution."
                )

            cur.execute(
                "SELECT response_payload FROM replay_step_requests "
                "WHERE session_id=%s AND request_id=%s FOR UPDATE",
                (session_id, request_id),
            )
            stored_row = cur.fetchone()
            if stored_row is None:
                raise ReplaySimulationError(
                    "Replay step idempotency record is unavailable."
                )
            stored_response = stored_row[0]
            if _execution_complete(stored_response):
                recovered = dict(stored_response)
                recovered["idempotent"] = bool(response.get("idempotent"))
                db.commit()
                return recovered
            if (
                not isinstance(stored_response, Mapping)
                or stored_response.get("strategyEnrichmentComplete") is not True
            ):
                raise ReplaySimulationError(
                    "Persisted strategy/risk response is incomplete."
                )

            response = dict(stored_response)
            candles = list(response.get("candles") or candles)
            decisions = list(response.get("strategyDecisions") or [])
            decision_by_time = {
                int(item["candleOpenTime"]): dict(item)
                for item in decisions
                if isinstance(item, Mapping)
                and item.get("candleOpenTime") is not None
            }

            cfg = execution_config(locked_session)
            fee_bps, max_leverage = cfg["feeBps"], cfg["maxLeverage"]
            balance = _decimal(locked_session["balance"], "balance")
            equity = _decimal(locked_session["equity"], "equity")
            initial_balance = _decimal(
                locked_session["initialBalance"], "initialBalance", positive=True
            )
            open_trade = _load_open_trade(cur, session_id)
            locked_summary = dict(locked_session.get("summary") or {})
            target_status = str(
                locked_summary.get("pendingFinalStatus")
                or ("COMPLETED" if response.get("completed") else "PAUSED")
            ).upper()

            cur.execute(
                "SELECT COALESCE(MAX(sequence_no),-1) FROM replay_events WHERE session_id=%s",
                (session_id,),
            )
            sequence = int(cur.fetchone()[0]) + 1
            started_sequence = sequence
            sequence = _event(
                cur,
                session_id=session_id,
                sequence=sequence,
                event_type="execution.started",
                candle_open_time=None,
                payload={
                    "requestId": request_id,
                    "feeBps": str(fee_bps),
                    "maxLeverage": str(max_leverage),
                    "sameCandlePolicy": SAME_CANDLE_POLICY,
                    "limitedLiabilityPolicy": LIMITED_LIABILITY_POLICY,
                    "externalExecutionAllowed": False,
                },
                created_at=now,
            )

            opened_count = closed_count = skipped_count = liquidation_count = 0
            request_fees = request_realized = Decimal("0")
            marks: list[dict[str, Any]] = []
            last_candle_time = int(candles[-1]["openTime"])

            for candle in candles:
                candle_time = int(candle["openTime"])
                close_price = _decimal(
                    candle["close"], "candle.close", positive=True
                )

                if open_trade is not None:
                    exit_decision = _exit_decision(open_trade, candle)
                    if exit_decision is not None:
                        closed, balance_delta, exit_fee = _close_trade(
                            cur,
                            session_id=session_id,
                            trade=open_trade,
                            candle=candle,
                            exit_price=exit_decision["price"],
                            reason=exit_decision["reason"],
                            fee_bps=fee_bps,
                            available_balance=balance,
                            same_candle_conflict=bool(
                                exit_decision["sameCandleConflict"]
                            ),
                            now=now,
                        )
                        balance = _quantize(balance + balance_delta)
                        if balance < 0:
                            raise ReplaySimulationError(
                                "Limited-liability close produced a negative balance."
                            )
                        equity = balance
                        request_fees += exit_fee
                        request_realized += _decimal(
                            closed["realizedPnl"], "realizedPnl"
                        )
                        if closed["payload"].get("limitedLiabilityApplied"):
                            liquidation_count += 1
                        sequence = _event(
                            cur,
                            session_id=session_id,
                            sequence=sequence,
                            event_type="trade.closed",
                            candle_open_time=candle_time,
                            payload={
                                "requestId": request_id,
                                "trade": closed,
                                "exitReason": exit_decision["reason"],
                                "sameCandleConflict": bool(
                                    exit_decision["sameCandleConflict"]
                                ),
                                "sameCandlePolicy": SAME_CANDLE_POLICY,
                            },
                            created_at=now,
                        )
                        open_trade = None
                        closed_count += 1

                decision = decision_by_time.get(candle_time)
                is_final_candle = (
                    target_status == "COMPLETED"
                    and candle_time == last_candle_time
                )
                if isinstance(decision, Mapping) and decision.get("eligible"):
                    skipped_reason: str | None
                    if open_trade is not None:
                        skipped_reason = "open_trade_exists"
                    elif is_final_candle:
                        skipped_reason = "session_final_candle"
                    elif equity <= 0 or balance <= 0:
                        skipped_reason = "account_depleted"
                    else:
                        opened, entry_fee, skipped_reason = _open_trade(
                            cur,
                            session=locked_session,
                            decision=decision,
                            candle=candle,
                            balance=balance,
                            equity=equity,
                            fee_bps=fee_bps,
                            max_leverage=max_leverage,
                            request_id=request_id,
                            now=now,
                        )
                        if opened is not None:
                            open_trade = opened
                            balance = _quantize(balance - entry_fee)
                            request_fees += entry_fee
                            opened_count += 1
                            sequence = _event(
                                cur,
                                session_id=session_id,
                                sequence=sequence,
                                event_type="trade.opened",
                                candle_open_time=candle_time,
                                payload={
                                    "requestId": request_id,
                                    "trade": opened,
                                    "fillModel": "candle_close",
                                    "externalExecutionAllowed": False,
                                },
                                created_at=now,
                            )
                            skipped_reason = None
                    if skipped_reason is not None:
                        skipped_count += 1
                        sequence = _event(
                            cur,
                            session_id=session_id,
                            sequence=sequence,
                            event_type="execution.skipped",
                            candle_open_time=candle_time,
                            payload={
                                "requestId": request_id,
                                "reason": skipped_reason,
                                "signal": decision.get("signal"),
                                "grade": decision.get("grade"),
                            },
                            created_at=now,
                        )

                if (
                    target_status == "COMPLETED"
                    and candle_time == last_candle_time
                    and open_trade is not None
                ):
                    closed, balance_delta, exit_fee = _close_trade(
                        cur,
                        session_id=session_id,
                        trade=open_trade,
                        candle=candle,
                        exit_price=close_price,
                        reason="session_end",
                        fee_bps=fee_bps,
                        available_balance=balance,
                        same_candle_conflict=False,
                        now=now,
                    )
                    balance = _quantize(balance + balance_delta)
                    if balance < 0:
                        raise ReplaySimulationError(
                            "Session-end close produced a negative balance."
                        )
                    equity = balance
                    request_fees += exit_fee
                    request_realized += _decimal(
                        closed["realizedPnl"], "realizedPnl"
                    )
                    if closed["payload"].get("limitedLiabilityApplied"):
                        liquidation_count += 1
                    sequence = _event(
                        cur,
                        session_id=session_id,
                        sequence=sequence,
                        event_type="trade.closed",
                        candle_open_time=candle_time,
                        payload={
                            "requestId": request_id,
                            "trade": closed,
                            "exitReason": "session_end",
                            "sameCandlePolicy": SAME_CANDLE_POLICY,
                        },
                        created_at=now,
                    )
                    open_trade = None
                    closed_count += 1

                equity, unrealized = _marked_equity(
                    balance, open_trade, close_price
                )
                mark = {
                    "candleOpenTime": candle_time,
                    "balance": str(balance),
                    "equity": str(equity),
                    "unrealizedPnl": str(unrealized),
                    "openTradeId": (
                        open_trade.get("tradeId")
                        if isinstance(open_trade, Mapping)
                        else None
                    ),
                }
                marks.append(mark)
                sequence = _event(
                    cur,
                    session_id=session_id,
                    sequence=sequence,
                    event_type="pnl.marked",
                    candle_open_time=candle_time,
                    payload={"requestId": request_id, **mark},
                    created_at=now,
                )

            cur.execute(
                "SELECT COUNT(*) FILTER (WHERE status='OPEN'),"
                "COUNT(*) FILTER (WHERE status='CLOSED'),"
                "COUNT(*) FILTER (WHERE status='CLOSED' AND realized_pnl>0),"
                "COUNT(*) FILTER (WHERE status='CLOSED' AND realized_pnl<=0),"
                "COALESCE(SUM(realized_pnl) FILTER (WHERE status='CLOSED'),0),"
                "COALESCE(SUM(fees),0) "
                "FROM replay_trades WHERE session_id=%s",
                (session_id,),
            )
            stats = cur.fetchone()
            open_count = int(stats[0] or 0)
            total_closed = int(stats[1] or 0)
            wins = int(stats[2] or 0)
            losses = int(stats[3] or 0)
            total_realized = _quantize(_decimal(stats[4], "totalRealizedPnl"))
            total_fees = _quantize(_decimal(stats[5], "totalFees"))
            net_pnl = _quantize(balance - initial_balance)
            equity_pnl = _quantize(equity - initial_balance)

            summary = dict(locked_summary)
            for key in (
                "pendingFinalStatus",
                "pipelineRecoveryRequired",
                "pendingReplayRequestId",
            ):
                summary.pop(key, None)
            summary.update(
                {
                    "strategyEvaluated": True,
                    "executionSimulated": True,
                    "simulatedExecutionVersion": 1,
                    "fillModel": "candle_close",
                    "sameCandlePolicy": SAME_CANDLE_POLICY,
                    "limitedLiabilityPolicy": LIMITED_LIABILITY_POLICY,
                    "feeBps": str(fee_bps),
                    "maxLeverage": str(max_leverage),
                    "openTrades": open_count,
                    "closedTrades": total_closed,
                    "winningTrades": wins,
                    "losingTrades": losses,
                    "liquidations": int(
                        summary.get("liquidations") or 0
                    )
                    + liquidation_count,
                    "realizedPnl": str(total_realized),
                    "netPnl": str(net_pnl),
                    "equityPnl": str(equity_pnl),
                    "feesPaid": str(total_fees),
                    "lastExecutionRequestId": request_id,
                }
            )
            cur.execute(
                "UPDATE replay_sessions SET status=%s,balance=%s,equity=%s,"
                "summary=%s,updated_at=%s WHERE session_id=%s RETURNING "
                "session_id,symbol,timeframe,status,start_time,end_time,cursor_time,"
                "initial_balance,balance,equity,strategy_mode,config,summary,created_at,updated_at",
                (
                    target_status,
                    balance,
                    equity,
                    _jsonb(summary),
                    now,
                    session_id,
                ),
            )
            updated_session = _session_row(cur.fetchone())
            if updated_session is None:
                raise ReplaySimulationError(
                    "Simulated execution did not return persistent session state."
                )

            completed_sequence = sequence
            execution_payload = {
                "engineVersion": 1,
                "feeBps": str(fee_bps),
                "maxLeverage": str(max_leverage),
                "sameCandlePolicy": SAME_CANDLE_POLICY,
                "limitedLiabilityPolicy": LIMITED_LIABILITY_POLICY,
                "opened": opened_count,
                "closed": closed_count,
                "skipped": skipped_count,
                "liquidations": liquidation_count,
                "requestFees": str(_quantize(request_fees)),
                "requestRealizedPnl": str(_quantize(request_realized)),
                "balance": str(balance),
                "equity": str(equity),
                "netPnl": str(net_pnl),
                "equityPnl": str(equity_pnl),
                "openTrades": open_count,
                "closedTrades": total_closed,
                "marks": marks,
                "externalExecutionAllowed": False,
            }
            sequence = _event(
                cur,
                session_id=session_id,
                sequence=sequence,
                event_type="execution.completed",
                candle_open_time=last_candle_time,
                payload={"requestId": request_id, **execution_payload},
                created_at=now,
            )

            response["session"] = updated_session
            response["executionEnrichmentComplete"] = True
            response["executionSimulated"] = True
            response["simulatedExecutionImplemented"] = True
            response["externalExecutionAllowed"] = False
            response["execution"] = execution_payload
            events = dict(response.get("events") or {})
            events.update(
                {
                    "executionStartedSequence": started_sequence,
                    "executionCompletedSequence": completed_sequence,
                }
            )
            response["events"] = events
            cur.execute(
                "UPDATE replay_step_requests SET response_payload=%s "
                "WHERE session_id=%s AND request_id=%s",
                (_jsonb(response), session_id, request_id),
            )
            if cur.rowcount != 1:
                raise ReplaySimulationError(
                    "Simulated execution response was not persisted."
                )
        db.commit()
    return response
