"""Deterministic historical strategy and candidate-risk evaluation.

Pure replay calculation only. This module has no exchange client, private API,
order submission, fill simulation, position mutation, fees, or PnL capability.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping, Sequence

MIN_HISTORY = 50
GRADE_RISK_PCT = {"A+": Decimal("1.00"), "A": Decimal("0.75")}


def _d(value: Any) -> Decimal:
    return Decimal(str(value))


def _ema(values: Sequence[Decimal], period: int) -> Decimal:
    seed = sum(values[:period]) / Decimal(period)
    alpha = Decimal(2) / Decimal(period + 1)
    result = seed
    for value in values[period:]:
        result = (value * alpha) + (result * (Decimal(1) - alpha))
    return result


def _rsi(values: Sequence[Decimal], period: int = 14) -> Decimal:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    sample = changes[-period:]
    gains = sum((max(change, Decimal(0)) for change in sample), Decimal(0)) / Decimal(period)
    losses = sum((max(-change, Decimal(0)) for change in sample), Decimal(0)) / Decimal(period)
    if losses == 0:
        return Decimal(100)
    rs = gains / losses
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def _atr(candles: Sequence[Mapping[str, Any]], period: int = 14) -> Decimal:
    rows = candles[-(period + 1):]
    values: list[Decimal] = []
    for index in range(1, len(rows)):
        high = _d(rows[index]["high"])
        low = _d(rows[index]["low"])
        previous_close = _d(rows[index - 1]["close"])
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(values, Decimal(0)) / Decimal(period)


def _macd(values: Sequence[Decimal]) -> tuple[Decimal, Decimal, Decimal]:
    line = _ema(values, 12) - _ema(values, 26)
    rolling: list[Decimal] = []
    for end in range(len(values) - 8, len(values) + 1):
        subset = values[:end]
        if len(subset) >= 26:
            rolling.append(_ema(subset, 12) - _ema(subset, 26))
    signal = sum(rolling, Decimal(0)) / Decimal(len(rolling))
    return line, signal, line - signal


def evaluate(candles: Sequence[Mapping[str, Any]], session: Mapping[str, Any]) -> dict[str, Any]:
    if len(candles) < MIN_HISTORY:
        return {
            "evaluated": False,
            "signal": "WAIT",
            "grade": "REJECT",
            "eligible": False,
            "reason": f"Insufficient history: {len(candles)}/{MIN_HISTORY} candles",
            "risk": None,
            "indicators": {"historyCandles": len(candles)},
            "executionSimulated": False,
            "externalExecutionAllowed": False,
        }

    closes = [_d(row["close"]) for row in candles]
    close = closes[-1]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    rsi14, atr14 = _rsi(closes), _atr(candles)
    macd, macd_signal, macd_hist = _macd(closes)
    long_score = sum(
        [close > ema20, ema20 > ema50, macd_hist > 0, Decimal(48) <= rsi14 <= Decimal(70)]
    )
    short_score = sum(
        [close < ema20, ema20 < ema50, macd_hist < 0, Decimal(30) <= rsi14 <= Decimal(52)]
    )
    if max(long_score, short_score) < 3 or long_score == short_score:
        signal, score = "WAIT", max(long_score, short_score)
    elif long_score > short_score:
        signal, score = "Buy", long_score
    else:
        signal, score = "Sell", short_score

    grade = "A+" if score == 4 else "A" if score == 3 else "B+" if score == 2 else "REJECT"
    eligible = signal in {"Buy", "Sell"} and grade in GRADE_RISK_PCT and atr14 > 0
    risk = None
    if eligible:
        stop_distance = atr14 * Decimal("1.5")
        stop = close - stop_distance if signal == "Buy" else close + stop_distance
        target = close + stop_distance * 2 if signal == "Buy" else close - stop_distance * 2
        equity = _d(session.get("equity") or session.get("initialBalance") or 0)
        risk_pct = GRADE_RISK_PCT[grade]
        risk_amount = (equity * risk_pct / 100).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        quantity = (risk_amount / stop_distance).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
        risk = {
            "riskPct": str(risk_pct),
            "riskAmount": str(risk_amount),
            "entryPrice": str(close),
            "stopLoss": str(stop),
            "takeProfit": str(target),
            "stopDistance": str(stop_distance),
            "rewardRisk": "2",
            "quantity": str(quantity),
            "sizingStatus": "candidate_only",
        }

    return {
        "evaluated": True,
        "signal": signal,
        "grade": grade,
        "eligible": eligible,
        "reason": f"{score}/4 aligned historical votes",
        "risk": risk,
        "indicators": {
            "historyCandles": len(candles),
            "close": str(close),
            "ema20": str(ema20),
            "ema50": str(ema50),
            "rsi14": str(rsi14),
            "atr14": str(atr14),
            "macd": str(macd),
            "macdSignal": str(macd_signal),
            "macdHistogram": str(macd_hist),
            "longVotes": long_score,
            "shortVotes": short_score,
        },
        "executionSimulated": False,
        "externalExecutionAllowed": False,
    }


def _enriched(payload: Any) -> bool:
    return isinstance(payload, Mapping) and payload.get("strategyEnrichmentComplete") is True and isinstance(
        payload.get("strategyDecisions"), list
    )


def enrich_step(store: Any, result: Mapping[str, Any]) -> dict[str, Any]:
    """Persist strategy/risk events and the enriched idempotent response atomically.

    Step 5 cursor advancement is durable before this method runs. If this transaction
    ever rolls back, a retry resumes enrichment from the stored base step response.
    """
    response = dict(result)
    session = dict(response.get("session") or {})
    candles = list(response.get("candles") or [])
    session_id = str(session.get("sessionId") or "")
    request_id = str(response.get("requestId") or "")

    if not session_id or not request_id or not candles:
        return response
    if _enriched(response):
        return response

    with store.lock, store.connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM replay_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
            if cur.fetchone() is None:
                raise RuntimeError("Replay session disappeared before strategy evaluation.")

            cur.execute(
                "SELECT response_payload FROM replay_step_requests "
                "WHERE session_id=%s AND request_id=%s FOR UPDATE",
                (session_id, request_id),
            )
            stored_row = cur.fetchone()
            if stored_row is None:
                raise RuntimeError("Replay step idempotency record is unavailable.")
            stored_response = stored_row[0]
            if _enriched(stored_response):
                recovered = dict(stored_response)
                recovered["idempotent"] = bool(response.get("idempotent"))
                db.commit()
                return recovered

            cur.execute(
                "SELECT COALESCE(MAX(sequence_no),-1) FROM replay_events WHERE session_id=%s",
                (session_id,),
            )
            sequence = int(cur.fetchone()[0]) + 1
            decisions: list[dict[str, Any]] = []
            now = int(time.time())

            for candle in candles:
                open_time = int(candle["openTime"])
                cur.execute(
                    "SELECT open_time,open_price,high_price,low_price,close_price,volume "
                    "FROM replay_session_candles WHERE session_id=%s AND open_time<=%s "
                    "ORDER BY open_time ASC",
                    (session_id, open_time),
                )
                history = [
                    {
                        "openTime": int(row[0]),
                        "open": str(row[1]),
                        "high": str(row[2]),
                        "low": str(row[3]),
                        "close": str(row[4]),
                        "volume": str(row[5]),
                    }
                    for row in cur.fetchall()
                ]
                decision = evaluate(history, session)
                decision["candleOpenTime"] = open_time
                decision["strategyMode"] = session.get("strategyMode")
                decision["requestId"] = request_id
                decisions.append(decision)
                cur.execute(
                    "INSERT INTO replay_events(session_id,sequence_no,event_type,candle_open_time,payload,created_at) "
                    "VALUES(%s,%s,'strategy.evaluated',%s,%s::jsonb,%s)",
                    (session_id, sequence, open_time, json.dumps(decision, separators=(",", ":")), now),
                )
                sequence += 1
                if decision.get("risk") is not None:
                    risk_event = {
                        "requestId": request_id,
                        "signal": decision["signal"],
                        "grade": decision["grade"],
                        "eligible": decision["eligible"],
                        "risk": decision["risk"],
                        "executionSimulated": False,
                        "externalExecutionAllowed": False,
                    }
                    cur.execute(
                        "INSERT INTO replay_events(session_id,sequence_no,event_type,candle_open_time,payload,created_at) "
                        "VALUES(%s,%s,'risk.candidate',%s,%s::jsonb,%s)",
                        (session_id, sequence, open_time, json.dumps(risk_event, separators=(",", ":")), now),
                    )
                    sequence += 1

            response["strategyEnrichmentComplete"] = True
            response["strategyEvaluated"] = any(bool(item.get("evaluated")) for item in decisions)
            response["riskEvaluated"] = any(item.get("risk") is not None for item in decisions)
            response["strategyDecisions"] = decisions
            response["eligibleCandidates"] = sum(1 for item in decisions if item.get("eligible"))
            response["executionSimulated"] = False
            response["externalExecutionAllowed"] = False
            cur.execute(
                "UPDATE replay_step_requests SET response_payload=%s::jsonb "
                "WHERE session_id=%s AND request_id=%s",
                (json.dumps(response, separators=(",", ":")), session_id, request_id),
            )
            if cur.rowcount != 1:
                raise RuntimeError("Enriched replay response was not persisted.")
        db.commit()
    return response
