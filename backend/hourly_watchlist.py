"""Persistent closed-1H Top-20 watchlist from directly eligible USDT contracts.

This module is the canonical scan entry point for the strategy pipeline. Eligible
Bybit USDT linear contracts are filtered by price, turnover and spread, then
classified from the latest fully closed 1H candle set using the existing worker
trend classifier and ranking formula. The resulting Top-20 feeds the existing
closed-15M setup stage.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable

try:
    from .scanner_safety import filter_closed_candles
except ImportError:  # pragma: no cover
    from scanner_safety import filter_closed_candles


_PERSIST_KEY = "hourly_watchlist_top20_v2"
_INTERVAL = "60"
_INTERVAL_SECONDS = 60 * 60
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_TREND_CLASSIFIER: Callable[[list[dict[str, Any]]], tuple[str | None, float, str]] | None = None
_RANK_ROWS: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
_STORE: Any | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 2,
    "source": "eligible_usdt_closed_1h_top20",
    "oneHourCandleTime": None,
    "updatedAt": 0,
    "symbols": [],
    "rows": [],
    "metrics": {},
    "lastError": None,
    "persisted": False,
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
        "watchlistSize": _integer("HOURLY_WATCHLIST_SIZE", 20, 1, 50),
        "minimumClosedCandles": _integer(
            "HOURLY_WATCHLIST_MIN_CLOSED_CANDLES", 60, 60, 240
        ),
        "minimumTurnover": _number(
            "SYMBOL_WORKER_MIN_TURNOVER_24H", 10_000_000, 0, 1_000_000_000
        ),
        "maximumSpreadPct": _number(
            "SYMBOL_WORKER_MAX_SPREAD_PCT", 0.20, 0.01, 3.0
        ),
    }


def _target_candle_open_seconds(timestamp: int) -> int:
    return ((int(timestamp) // _INTERVAL_SECONDS) - 1) * _INTERVAL_SECONDS


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    rows = [dict(row) for row in _STATE.get("rows") or []]
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 2),
        "source": str(_STATE.get("source") or "eligible_usdt_closed_1h_top20"),
        "oneHourCandleTime": _STATE.get("oneHourCandleTime"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "activeSymbols": list(_STATE.get("symbols") or []),
        "symbols": list(_STATE.get("symbols") or []),
        "bullishCount": sum(1 for row in rows if row.get("trend") == "BULLISH"),
        "bearishCount": sum(1 for row in rows if row.get("trend") == "BEARISH"),
        "rows": rows,
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "persisted": bool(_STATE.get("persisted")),
        "settings": settings(),
    }


def snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return _snapshot_unlocked()


def _persistent_store(core: Any) -> Any | None:
    store = getattr(core, "_durable_state_store", None)
    if store is None:
        return None
    for name in ("get", "put", "status"):
        if not callable(getattr(store, name, None)):
            return None
    try:
        status = dict(store.status() or {})
    except Exception:
        return None
    if not status.get("ok") or status.get("degraded"):
        return None
    return store


def _load_persisted() -> None:
    if _STORE is None:
        return
    try:
        saved = _STORE.get(_PERSIST_KEY)
    except Exception:
        return
    if not isinstance(saved, dict):
        return
    raw_symbols = saved.get("symbols")
    raw_rows = saved.get("rows")
    if not isinstance(raw_symbols, list) or not isinstance(raw_rows, list):
        return
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    symbols = [str(value or "").upper() for value in raw_symbols if str(value or "").strip()]
    if bool(rows) != bool(symbols):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or ("ready" if symbols else "empty")),
                "version": int(saved.get("version") or 2),
                "source": str(saved.get("source") or "eligible_usdt_closed_1h_top20"),
                "oneHourCandleTime": saved.get("oneHourCandleTime"),
                "updatedAt": int(saved.get("updatedAt") or 0),
                "symbols": symbols,
                "rows": rows,
                "metrics": dict(saved.get("metrics") or {}),
                "lastError": saved.get("lastError"),
                "persisted": True,
            }
        )


def _persist(payload: dict[str, Any]) -> bool:
    if _STORE is None:
        return False
    body = {
        "status": payload["status"],
        "version": payload["version"],
        "source": payload["source"],
        "oneHourCandleTime": payload["oneHourCandleTime"],
        "updatedAt": payload["updatedAt"],
        "symbols": payload["symbols"],
        "rows": payload["rows"],
        "metrics": payload["metrics"],
        "lastError": payload.get("lastError"),
    }
    try:
        _STORE.put(_PERSIST_KEY, body)
        confirmed = _STORE.get(_PERSIST_KEY)
    except Exception:
        return False
    return bool(
        isinstance(confirmed, dict)
        and confirmed.get("oneHourCandleTime") == body["oneHourCandleTime"]
        and list(confirmed.get("symbols") or []) == body["symbols"]
    )


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


def _eligible_market(core: Any) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, int]]:
    payload = core.public_bybit_get("/v5/market/tickers", {"category": "linear"})
    rows = (payload.get("result") or {}).get("list") or [] if payload.get("retCode") == 0 else []
    cfg = settings()
    symbols: list[str] = []
    market: dict[str, dict[str, Any]] = {}
    rejected = {"invalidSymbol": 0, "invalidPrice": 0, "turnover": 0, "spread": 0}

    for item in rows:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or not symbol.isalnum():
            rejected["invalidSymbol"] += 1
            continue
        try:
            last = float(item.get("lastPrice") or 0)
            turnover = float(item.get("turnover24h") or 0)
            price_24h_pct = float(item.get("price24hPcnt") or 0) * 100
        except (TypeError, ValueError):
            rejected["invalidPrice"] += 1
            continue
        if last <= 0:
            rejected["invalidPrice"] += 1
            continue
        if turnover < float(cfg["minimumTurnover"]):
            rejected["turnover"] += 1
            continue
        spread = _spread_pct(item)
        if spread is None or spread > float(cfg["maximumSpreadPct"]):
            rejected["spread"] += 1
            continue
        symbols.append(symbol)
        market[symbol] = {
            "lastPrice": last,
            "turnover24h": turnover,
            "spreadPct": round(spread, 5),
            "change24hPct": round(price_24h_pct, 5),
        }
    return sorted(set(symbols)), market, rejected


def _closed_history(core: Any, symbol: str, minimum: int, now_ms: int) -> list[dict[str, Any]]:
    candles, _ = core.fetch_candles(symbol, _INTERVAL, limit=max(80, minimum + 5))
    closed = filter_closed_candles(candles or [], _INTERVAL, now_ms=now_ms)
    return closed if len(closed) >= minimum else []


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    timestamp = int(now or time.time())
    target_open_ms = _target_candle_open_seconds(timestamp) * 1000
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        if _TREND_CLASSIFIER is None or _RANK_ROWS is None:
            raise RuntimeError("Hourly watchlist is not installed with worker dependencies")

        symbols, market, market_rejected = _eligible_market(core)
        if not symbols:
            raise RuntimeError("No eligible USDT perpetual symbols for closed-1H scan")

        cfg = settings()
        minimum = int(cfg["minimumClosedCandles"])
        now_ms = timestamp * 1000
        qualified: list[dict[str, Any]] = []
        rejected = {
            "missing1hHistory": 0,
            "stale1hCandle": 0,
            "neutralOrUnclear": 0,
        }

        for symbol in symbols:
            history = _closed_history(core, symbol, minimum, now_ms)
            if not history:
                rejected["missing1hHistory"] += 1
                continue
            latest_candle_time = int(history[-1].get("time") or 0)
            if latest_candle_time != target_open_ms:
                rejected["stale1hCandle"] += 1
                continue
            trend, trend_score, reason = _TREND_CLASSIFIER(history)
            if trend not in {"BULLISH", "BEARISH"}:
                rejected["neutralOrUnclear"] += 1
                continue
            ticker = market[symbol]
            qualified.append(
                {
                    "symbol": symbol,
                    "trend": trend,
                    "direction": trend,
                    "trendScore": round(float(trend_score), 4),
                    "oneHourTrend": trend,
                    "oneHourTrendScore": round(float(trend_score), 4),
                    "oneHourReason": str(reason),
                    "oneHourCandleTime": latest_candle_time,
                    "turnover24h": float(ticker["turnover24h"]),
                    "spreadPct": float(ticker["spreadPct"]),
                    "lastPrice": float(ticker["lastPrice"]),
                    "change24hPct": float(ticker["change24hPct"]),
                    "reason": str(reason),
                    "lastScannedAt": timestamp,
                    "selectedAt": timestamp,
                }
            )

        ranked = _RANK_ROWS(qualified)
        selected = ranked[: int(cfg["watchlistSize"])]
        metrics = {
            "eligibleMarketInput": len(symbols),
            "oneHourQualified": len(qualified),
            "selected": len(selected),
            "bullish": sum(1 for row in selected if row.get("trend") == "BULLISH"),
            "bearish": sum(1 for row in selected if row.get("trend") == "BEARISH"),
            "upstreamTimeframes": ["1H"],
            "rankingPolicy": "existing_worker_rank_rows",
            "marketRejected": market_rejected,
            "rejected": rejected,
        }
        payload = {
            "status": "ready" if selected else "empty",
            "version": 2,
            "source": "eligible_usdt_closed_1h_top20",
            "oneHourCandleTime": target_open_ms,
            "updatedAt": timestamp,
            "symbols": [str(row["symbol"]) for row in selected],
            "rows": selected,
            "metrics": metrics,
            "lastError": None if selected else "No BULLISH/BEARISH symbol qualified on the closed 1H bar",
            "persisted": False,
        }
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("symbols"))
            _STATE.update({"status": "stale" if has_cache else "error", "lastError": str(exc)})
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(now: int | None = None) -> bool:
    timestamp = int(now or time.time())
    target_ms = _target_candle_open_seconds(timestamp) * 1000
    with _STATE_LOCK:
        return int(_STATE.get("oneHourCandleTime") or 0) != target_ms


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    return build(core, now=now) if due(now) else snapshot()


def run_batch(core: Any, now: int | None = None) -> dict[str, Any]:
    return ensure_current(core, now=now)


def install(core: Any, worker_module: Any) -> dict[str, Any]:
    global _TREND_CLASSIFIER, _RANK_ROWS, _STORE
    if getattr(worker_module, "_hourly_watchlist_v2_installed", False):
        return status(worker_module)

    classifier = getattr(worker_module, "classify_trend", None)
    ranker = getattr(worker_module, "_rank_rows", None)
    if not callable(classifier) or not callable(ranker):
        raise RuntimeError("Existing worker trend classifier or ranker is unavailable")

    _TREND_CLASSIFIER = classifier
    _RANK_ROWS = ranker
    _STORE = _persistent_store(core)
    _load_persisted()

    worker_module._hourly_watchlist_base_run_batch = worker_module.run_batch
    worker_module._hourly_watchlist_base_snapshot = worker_module.snapshot
    worker_module.run_batch = run_batch
    worker_module.snapshot = snapshot
    worker_module._hourly_watchlist_v2_installed = True

    core.hourly_watchlist = lambda force=False: build(core) if force else ensure_current(core)
    core.hourly_watchlist_status = snapshot
    return status(worker_module)


def status(worker_module: Any | None = None) -> dict[str, Any]:
    installed = bool(worker_module is not None and getattr(worker_module, "_hourly_watchlist_v2_installed", False))
    return {
        "installed": installed,
        "policy": "ELIGIBLE_USDT_TO_CLOSED_1H_TOP20",
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _TREND_CLASSIFIER, _RANK_ROWS, _STORE
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 2,
                "source": "eligible_usdt_closed_1h_top20",
                "oneHourCandleTime": None,
                "updatedAt": 0,
                "symbols": [],
                "rows": [],
                "metrics": {},
                "lastError": None,
                "persisted": False,
            }
        )
    _TREND_CLASSIFIER = None
    _RANK_ROWS = None
    _STORE = None