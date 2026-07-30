"""Round-robin setup-verification worker.

Consumes the current active-30 output from ``backend.worker`` and verifies ten
symbols per run. It evaluates only fully closed 15-minute candles, accepts one
trend-aligned actionable strategy vote, creates a minimum-2R candidate, and
stores confirmed candidates in an in-memory handoff queue.

This module never submits orders and never calls the execution engine.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "currentIndex": 0,
    "batchSize": 10,
    "cycleNumber": 0,
    "lastRunAt": 0,
    "lastError": None,
    "lastEvaluatedCandle": {},
    "rows": [],
    "confirmedQueue": [],
}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, int | float]:
    return {
        "batchSize": _integer("SETUP_WORKER_BATCH_SIZE", 10, 1, 30),
        "minimumClosedCandles": _integer("SETUP_WORKER_MIN_15M_CANDLES", 60, 30, 240),
        "minimumRiskReward": _number("SETUP_WORKER_MIN_RR", 2.0, 2.0, 10.0),
        "structureLookback": _integer("SETUP_WORKER_STRUCTURE_LOOKBACK", 12, 5, 40),
        "queueLimit": _integer("SETUP_WORKER_QUEUE_LIMIT", 100, 10, 1000),
    }


def _closed_15m(candles: list[dict[str, Any]], now_ms: int) -> list[dict[str, Any]]:
    interval_ms = 15 * 60 * 1000
    return [row for row in candles if int(row.get("time") or 0) + interval_ms <= now_ms]


def _expected_side(trend: str) -> str | None:
    if trend == "BULLISH":
        return "Buy"
    if trend == "BEARISH":
        return "Sell"
    return None


def _actionable_vote(votes: list[dict[str, Any]], expected_side: str) -> dict[str, Any] | None:
    aligned = [vote for vote in votes if vote.get("signal") == expected_side]
    if not aligned:
        return None
    return max(aligned, key=lambda vote: abs(float(vote.get("strength") or 0)))


def _price_plan(
    candles: list[dict[str, Any]],
    side: str,
    minimum_rr: float,
    lookback: int,
) -> tuple[dict[str, float] | None, str]:
    window = candles[-lookback:]
    if len(window) < lookback:
        return None, "Not enough closed 15M structure history"

    entry = float(candles[-1]["close"])
    if entry <= 0:
        return None, "Invalid entry reference"

    if side == "Buy":
        stop = min(float(row["low"]) for row in window)
        risk = entry - stop
        take = entry + (risk * minimum_rr)
    else:
        stop = max(float(row["high"]) for row in window)
        risk = stop - entry
        take = entry - (risk * minimum_rr)

    if risk <= 0 or take <= 0:
        return None, "Invalid structural stop or target"

    reward = abs(take - entry)
    rr = reward / risk
    if rr + 1e-9 < minimum_rr:
        return None, "Risk/reward is below minimum"

    return {
        "entryReference": round(entry, 12),
        "stopLoss": round(stop, 12),
        "takeProfitReference": round(take, 12),
        "riskReward": round(rr, 4),
    }, "Minimum structural risk/reward confirmed"


def _queue_candidate(candidate: dict[str, Any], queue_limit: int) -> bool:
    key = candidate["candidateKey"]
    queue = list(_STATE.get("confirmedQueue") or [])
    if any(row.get("candidateKey") == key for row in queue):
        return False
    queue.append(candidate)
    _STATE["confirmedQueue"] = queue[-queue_limit:]
    return True


def _evaluate_symbol(core: Any, active_row: dict[str, Any], now_ms: int, cfg: dict[str, Any]) -> dict[str, Any]:
    symbol = str(active_row.get("symbol") or "").upper()
    trend = str(active_row.get("trend") or "").upper()
    expected_side = _expected_side(trend)
    base = {"symbol": symbol, "trend": trend, "status": "NO_SETUP"}
    if not symbol or expected_side is None:
        return {**base, "reason": "Missing symbol or unsupported trend"}

    candles, candle_message = core.fetch_candles(
        symbol,
        "15",
        limit=max(80, int(cfg["minimumClosedCandles"]) + 5),
    )
    closed = _closed_15m(candles or [], now_ms)
    if len(closed) < int(cfg["minimumClosedCandles"]):
        return {**base, "reason": candle_message or "Not enough closed 15M candles"}

    candle_time = int(closed[-1]["time"])
    previous = int((_STATE.get("lastEvaluatedCandle") or {}).get(symbol) or 0)
    if candle_time <= previous:
        return {**base, "status": "SKIPPED", "signalCandleTime": candle_time, "reason": "15M candle already evaluated"}

    _STATE.setdefault("lastEvaluatedCandle", {})[symbol] = candle_time
    signal, reason, votes, router, indicators, engine_status = core.evaluate_signal(symbol, "15", "aggressive")
    votes = list(votes or [])
    aligned_vote = _actionable_vote(votes, expected_side)

    common = {
        **base,
        "signalCandleTime": candle_time,
        "expectedSide": expected_side,
        "engineSignal": signal,
        "engineReason": reason,
        "router": router or {},
        "engineStatus": engine_status,
        "strategyVotes": votes,
        "indicators": indicators or {},
    }

    if aligned_vote is None:
        has_waiting_strategy = any(vote.get("signal") == "WAIT" for vote in votes)
        return {
            **common,
            "status": "NEAR_SETUP" if has_waiting_strategy else "NO_SETUP",
            "reason": "No trend-aligned actionable strategy vote on this closed candle",
        }

    plan, plan_reason = _price_plan(
        closed,
        expected_side,
        float(cfg["minimumRiskReward"]),
        int(cfg["structureLookback"]),
    )
    if plan is None:
        return {**common, "status": "NO_SETUP", "strategy": aligned_vote.get("engine"), "reason": plan_reason}

    candidate = {
        "candidateKey": f"{symbol}:{candle_time}:{expected_side}",
        "symbol": symbol,
        "side": expected_side,
        "trend": trend,
        "strategy": aligned_vote.get("engine"),
        "strategyReason": aligned_vote.get("reason"),
        "strategyStrength": aligned_vote.get("strength", 0),
        "signalCandleTime": candle_time,
        **plan,
        "createdAt": int(now_ms / 1000),
        "executionStatus": "PENDING_HANDOFF",
    }
    queued = _queue_candidate(candidate, int(cfg["queueLimit"]))
    return {
        **common,
        **candidate,
        "status": "CONFIRMED",
        "queued": queued,
        "reason": plan_reason if queued else "Candidate already queued",
    }


def run_batch(core: Any, symbol_worker: Any, now: int | None = None) -> dict[str, Any]:
    """Verify the next ten symbols from the latest active-30 snapshot."""
    timestamp = int(now or time.time())
    now_ms = timestamp * 1000
    cfg = settings()
    if not _LOCK.acquire(blocking=False):
        return snapshot_unlocked("busy")

    try:
        active_snapshot = symbol_worker.snapshot()
        active_rows = [dict(row) for row in active_snapshot.get("rows") or []]
        if not active_rows:
            _STATE.update({"status": "waiting", "lastRunAt": timestamp, "lastError": "Active symbol pool is empty", "rows": []})
            return snapshot_unlocked()

        total = len(active_rows)
        start = int(_STATE.get("currentIndex") or 0) % total
        batch_size = min(int(cfg["batchSize"]), total)
        indexes = [(start + offset) % total for offset in range(batch_size)]
        batch = [active_rows[index] for index in indexes]
        wrapped = start + batch_size >= total

        rows = [_evaluate_symbol(core, row, now_ms, cfg) for row in batch]
        next_index = (start + batch_size) % total
        _STATE.update({
            "status": "ok",
            "currentIndex": next_index,
            "batchSize": batch_size,
            "cycleNumber": int(_STATE.get("cycleNumber") or 0) + (1 if wrapped else 0),
            "lastRunAt": timestamp,
            "lastError": None,
            "rows": rows,
            "lastBatch": {
                "startIndex": start,
                "endIndex": indexes[-1],
                "requested": len(batch),
                "confirmed": sum(1 for row in rows if row.get("status") == "CONFIRMED"),
                "nearSetup": sum(1 for row in rows if row.get("status") == "NEAR_SETUP"),
                "noSetup": sum(1 for row in rows if row.get("status") == "NO_SETUP"),
                "skipped": sum(1 for row in rows if row.get("status") == "SKIPPED"),
                "wrapped": wrapped,
                "activePoolVersion": active_snapshot.get("updatedAt", 0),
            },
        })
        return snapshot_unlocked()
    except Exception as exc:
        _STATE.update({"status": "error", "lastRunAt": timestamp, "lastError": str(exc)})
        return snapshot_unlocked()
    finally:
        _LOCK.release()


def pop_confirmed() -> dict[str, Any] | None:
    """Return one confirmed candidate for a future execution handoff."""
    with _LOCK:
        queue = list(_STATE.get("confirmedQueue") or [])
        if not queue:
            return None
        candidate = queue.pop(0)
        _STATE["confirmedQueue"] = queue
        return dict(candidate)


def snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    queue = [dict(row) for row in _STATE.get("confirmedQueue") or []]
    return {
        "status": status_override or _STATE.get("status", "idle"),
        "currentIndex": int(_STATE.get("currentIndex") or 0),
        "batchSize": int(_STATE.get("batchSize") or 10),
        "cycleNumber": int(_STATE.get("cycleNumber") or 0),
        "lastRunAt": int(_STATE.get("lastRunAt") or 0),
        "lastBatch": dict(_STATE.get("lastBatch") or {}),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "confirmedQueue": queue,
        "confirmedQueueSize": len(queue),
        "lastError": _STATE.get("lastError"),
    }


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return snapshot_unlocked()
