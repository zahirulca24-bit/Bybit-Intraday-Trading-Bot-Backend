"""Round-robin symbol-selection worker for the Bybit Demo bot.

Continuously scans the active Bybit USDT linear-perpetual universe in batches,
classifies each scanned symbol as BULLISH or BEARISH from fully closed 1H
candles, and maintains a rolling ranked pool of the best 30 trend-qualified
symbols.

This module does not evaluate setups, perform risk checks, or submit orders.
"""

from __future__ import annotations

import math
import os
import threading
import time
from typing import Any

_LOCK = threading.Lock()
_STATE: dict[str, Any] = {
    "status": "idle",
    "allSymbols": [],
    "currentIndex": 0,
    "batchSize": 100,
    "cycleNumber": 0,
    "scannedInCycle": 0,
    "candidates": {},
    "activeSymbols": [],
    "rows": [],
    "updatedAt": 0,
    "lastBatchAt": 0,
    "lastFullCycleAt": 0,
    "lastError": None,
}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, int | float]:
    return {
        "batchSize": _integer("SYMBOL_WORKER_BATCH_SIZE", 100, 100, 500),
        "activePoolSize": _integer("SYMBOL_WORKER_ACTIVE_POOL_SIZE", 30, 1, 100),
        "minimumClosedCandles": _integer("SYMBOL_WORKER_MIN_1H_CANDLES", 60, 60, 240),
        "minimumTurnover": float(os.environ.get("SYMBOL_WORKER_MIN_TURNOVER_24H", "10000000")),
        "maximumSpreadPct": float(os.environ.get("SYMBOL_WORKER_MAX_SPREAD_PCT", "0.20")),
    }


def _spread_pct(item: dict[str, Any]) -> float | None:
    try:
        bid = float(item.get("bid1Price") or 0)
        ask = float(item.get("ask1Price") or 0)
        last = float(item.get("lastPrice") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
        return None
    return ((ask - bid) / last) * 100


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    multiplier = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    result = [current]
    for value in values[period:]:
        current = ((value - current) * multiplier) + current
        result.append(current)
    return result


def classify_trend(candles: list[dict[str, Any]]) -> tuple[str | None, float, str]:
    """Return BULLISH/BEARISH only; unclear trend is rejected with None."""
    closes = [float(row["close"]) for row in candles]
    ema20 = _ema(closes, 20)
    ema50 = _ema(closes, 50)
    if len(ema20) < 4 or len(ema50) < 4:
        return None, 0.0, "Not enough EMA history"

    price = closes[-1]
    fast = ema20[-1]
    slow = ema50[-1]
    fast_slope = fast - ema20[-4]
    separation_pct = abs(fast - slow) / price * 100 if price > 0 else 0.0
    slope_pct = abs(fast_slope) / price * 100 if price > 0 else 0.0

    if fast > slow and price > fast and fast_slope > 0:
        trend = "BULLISH"
    elif fast < slow and price < fast and fast_slope < 0:
        trend = "BEARISH"
    else:
        return None, 0.0, "Trend is not cleanly bullish or bearish"

    score = min(100.0, (separation_pct * 45.0) + (slope_pct * 250.0))
    return trend, round(score, 4), "EMA20/EMA50 alignment, price location, and EMA20 slope confirmed"


def _fetch_active_usdt_symbols(core: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    payload = core.public_bybit_get("/v5/market/tickers", {"category": "linear"})
    rows = (payload.get("result") or {}).get("list") or [] if payload.get("retCode") == 0 else []
    cfg = settings()
    symbols: list[str] = []
    tickers: dict[str, dict[str, Any]] = {}
    for item in rows:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or not symbol.isalnum():
            continue
        try:
            turnover = float(item.get("turnover24h") or 0)
            last = float(item.get("lastPrice") or 0)
        except (TypeError, ValueError):
            continue
        spread = _spread_pct(item)
        if last <= 0 or turnover < float(cfg["minimumTurnover"]):
            continue
        if spread is None or spread > float(cfg["maximumSpreadPct"]):
            continue
        symbols.append(symbol)
        tickers[symbol] = {
            "lastPrice": last,
            "turnover24h": turnover,
            "spreadPct": round(spread, 5),
        }
    return sorted(set(symbols)), tickers


def _rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    max_turnover = max(float(row["turnover24h"]) for row in rows) or 1.0
    for row in rows:
        liquidity = math.log1p(float(row["turnover24h"])) / math.log1p(max_turnover)
        spread_quality = 1.0 - min(1.0, float(row["spreadPct"]) / 0.20)
        row["rankScore"] = round(
            (float(row["trendScore"]) * 0.65) + (liquidity * 25.0) + (spread_quality * 10.0),
            4,
        )
    return sorted(rows, key=lambda row: (row["rankScore"], row["turnover24h"]), reverse=True)


def run_batch(core: Any, now: int | None = None) -> dict[str, Any]:
    """Scan the next non-overlapping batch and update the rolling best-30 pool."""
    timestamp = int(now or time.time())
    cfg = settings()
    if not _LOCK.acquire(blocking=False):
        return snapshot_unlocked("busy")

    try:
        symbols, tickers = _fetch_active_usdt_symbols(core)
        if not symbols:
            _STATE.update({"status": "error", "lastError": "No eligible USDT perpetual symbols", "updatedAt": timestamp})
            return snapshot_unlocked()

        previous_symbols = list(_STATE.get("allSymbols") or [])
        if previous_symbols != symbols:
            current_symbol = previous_symbols[_STATE["currentIndex"]] if previous_symbols and _STATE["currentIndex"] < len(previous_symbols) else None
            _STATE["allSymbols"] = symbols
            _STATE["currentIndex"] = symbols.index(current_symbol) if current_symbol in symbols else 0

        total = len(symbols)
        start = int(_STATE["currentIndex"]) % total
        configured_batch_size = min(int(cfg["batchSize"]), total)
        remaining_in_cycle = total - start
        batch_size = min(configured_batch_size, remaining_in_cycle)
        indexes = list(range(start, start + batch_size))
        batch = [symbols[index] for index in indexes]
        wrapped = start + batch_size >= total

        candidates: dict[str, dict[str, Any]] = dict(_STATE.get("candidates") or {})
        qualified = 0
        rejected = 0
        for symbol in batch:
            candles, _ = core.fetch_candles(symbol, "60", limit=max(80, int(cfg["minimumClosedCandles"]) + 5))
            if not candles or len(candles) < int(cfg["minimumClosedCandles"]):
                candidates.pop(symbol, None)
                rejected += 1
                continue
            trend, trend_score, reason = classify_trend(candles)
            if trend not in {"BULLISH", "BEARISH"}:
                candidates.pop(symbol, None)
                rejected += 1
                continue
            market = tickers[symbol]
            candidates[symbol] = {
                "symbol": symbol,
                "trend": trend,
                "trendScore": trend_score,
                "turnover24h": market["turnover24h"],
                "spreadPct": market["spreadPct"],
                "lastPrice": market["lastPrice"],
                "reason": reason,
                "lastScannedAt": timestamp,
            }
            qualified += 1

        active_universe = set(symbols)
        candidates = {symbol: row for symbol, row in candidates.items() if symbol in active_universe}
        ranked = _rank_rows(list(candidates.values()))
        selected = ranked[: int(cfg["activePoolSize"])]

        next_index = 0 if wrapped else start + batch_size
        cycle_number = int(_STATE["cycleNumber"]) + (1 if wrapped else 0)
        scanned_in_cycle = 0 if wrapped else min(total, int(_STATE["scannedInCycle"]) + batch_size)
        _STATE.update({
            "status": "ok",
            "allSymbols": symbols,
            "currentIndex": next_index,
            "batchSize": batch_size,
            "cycleNumber": cycle_number,
            "scannedInCycle": scanned_in_cycle,
            "candidates": candidates,
            "activeSymbols": [row["symbol"] for row in selected],
            "rows": selected,
            "updatedAt": timestamp,
            "lastBatchAt": timestamp,
            "lastFullCycleAt": timestamp if wrapped else int(_STATE.get("lastFullCycleAt") or 0),
            "lastError": None,
            "lastBatch": {
                "startIndex": start,
                "endIndex": indexes[-1],
                "requested": len(batch),
                "qualified": qualified,
                "rejected": rejected,
                "wrapped": wrapped,
            },
        })
        return snapshot_unlocked()
    except Exception as exc:
        _STATE.update({"status": "error", "lastError": str(exc), "updatedAt": timestamp})
        return snapshot_unlocked()
    finally:
        _LOCK.release()


def snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    rows = [dict(row) for row in _STATE.get("rows") or []]
    return {
        "status": status_override or _STATE.get("status", "idle"),
        "totalUniverse": len(_STATE.get("allSymbols") or []),
        "currentIndex": int(_STATE.get("currentIndex") or 0),
        "batchSize": int(_STATE.get("batchSize") or 100),
        "cycleNumber": int(_STATE.get("cycleNumber") or 0),
        "scannedInCycle": int(_STATE.get("scannedInCycle") or 0),
        "activeSymbols": list(_STATE.get("activeSymbols") or []),
        "bullishCount": sum(1 for row in rows if row.get("trend") == "BULLISH"),
        "bearishCount": sum(1 for row in rows if row.get("trend") == "BEARISH"),
        "rows": rows,
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "lastBatchAt": int(_STATE.get("lastBatchAt") or 0),
        "lastFullCycleAt": int(_STATE.get("lastFullCycleAt") or 0),
        "lastBatch": dict(_STATE.get("lastBatch") or {}),
        "lastError": _STATE.get("lastError"),
    }


def snapshot() -> dict[str, Any]:
    with _LOCK:
        return snapshot_unlocked()
