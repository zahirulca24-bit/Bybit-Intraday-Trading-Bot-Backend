"""Pure scanner-safety helpers for the guarded Bybit Demo runtime."""

from __future__ import annotations

import time
from typing import Callable, Iterable

SUPPORTED_INTERVALS = {
    "1": 60_000,
    "3": 180_000,
    "5": 300_000,
    "15": 900_000,
    "30": 1_800_000,
    "60": 3_600_000,
    "120": 7_200_000,
    "240": 14_400_000,
    "360": 21_600_000,
    "720": 43_200_000,
    "D": 86_400_000,
    "W": 604_800_000,
}


def normalize_interval(value: object, default: str = "5") -> str:
    interval = str(value or default).upper()
    if interval not in SUPPORTED_INTERVALS:
        raise ValueError(f"Unsupported Bybit interval: {value}")
    return interval


def filter_closed_candles(candles: Iterable[dict], interval: object, now_ms: int | None = None) -> list[dict]:
    """Return only candles whose full interval has elapsed."""
    normalized = normalize_interval(interval)
    cutoff = int(time.time() * 1000) if now_ms is None else int(now_ms)
    duration = SUPPORTED_INTERVALS[normalized]
    rows = []
    for candle in candles or []:
        try:
            opened_at = int(candle.get("time"))
        except (TypeError, ValueError, AttributeError):
            continue
        if opened_at + duration <= cutoff:
            rows.append(candle)
    return rows


def bounded_symbols(symbols: Iterable[object], maximum: int = 10) -> list[str]:
    """Normalize, de-duplicate and cap the scanner universe."""
    cap = max(1, int(maximum))
    result: list[str] = []
    seen: set[str] = set()
    for raw in symbols or []:
        symbol = str(raw or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
        if len(result) >= cap:
            break
    return result


def signal_identity(symbol: object, interval: object, candle_time: object, side: object) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_interval = normalize_interval(interval)
    normalized_side = str(side or "").strip().title()
    if not normalized_symbol or normalized_side not in {"Buy", "Sell"}:
        raise ValueError("Executable signal identity requires symbol and Buy/Sell side")
    try:
        timestamp = int(candle_time)
    except (TypeError, ValueError):
        raise ValueError("Executable signal identity requires a closed candle timestamp") from None
    return f"{normalized_symbol}:{normalized_interval}:{timestamp}:{normalized_side}"


def deadline_reached(started_at: float, timeout_seconds: float, clock: Callable[[], float] = time.monotonic) -> bool:
    return clock() - started_at >= max(0.1, float(timeout_seconds))
