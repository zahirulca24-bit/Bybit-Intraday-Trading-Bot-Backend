"""Persistent hourly Top-20 watchlist derived from the closed-4H pool.

The existing symbol worker already owns the approved 1H EMA trend classifier
and ranking formula. This additive runtime layer reuses those functions, limits
input to the canonical closed-4H Top-50 pool, evaluates only fully closed 1H
candles, persists the resulting Top-20 watchlist, and exposes that snapshot to
the existing setup worker.

No new ranking weights, strategy rules, risk thresholds, or execution behavior
are introduced here. A 1H direction change from the upstream 4H direction is
recorded but not silently blocked, preserving the existing worker behavior.
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


_PERSIST_KEY = "hourly_watchlist_top20_v1"
_INTERVAL = "60"
_INTERVAL_SECONDS = 60 * 60
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_TREND_CLASSIFIER: Callable[[list[dict[str, Any]]], tuple[str | None, float, str]] | None = None
_RANK_ROWS: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None
_STORE: Any | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "source": "four_hour_top50_closed_1h_watchlist",
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


def settings() -> dict[str, int]:
    return {
        "watchlistSize": _integer("HOURLY_WATCHLIST_SIZE", 20, 1, 50),
        "minimumClosedCandles": _integer(
            "HOURLY_WATCHLIST_MIN_CLOSED_CANDLES", 60, 60, 240
        ),
    }


def _target_candle_open_seconds(timestamp: int) -> int:
    """Return the opening time of the latest fully closed UTC-aligned 1H bar."""
    return ((int(timestamp) // _INTERVAL_SECONDS) - 1) * _INTERVAL_SECONDS


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "source": str(_STATE.get("source") or "four_hour_top50_closed_1h_watchlist"),
        "oneHourCandleTime": _STATE.get("oneHourCandleTime"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "activeSymbols": list(_STATE.get("symbols") or []),
        "symbols": list(_STATE.get("symbols") or []),
        "bullishCount": sum(
            1 for row in _STATE.get("rows") or [] if row.get("trend") == "BULLISH"
        ),
        "bearishCount": sum(
            1 for row in _STATE.get("rows") or [] if row.get("trend") == "BEARISH"
        ),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
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
    symbols = [
        str(value or "").upper()
        for value in raw_symbols
        if str(value or "").strip()
    ]
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    if bool(symbols) != bool(rows):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or ("ready" if symbols else "empty")),
                "version": int(saved.get("version") or 1),
                "source": str(
                    saved.get("source") or "four_hour_top50_closed_1h_watchlist"
                ),
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


def _four_hour_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "four_hour_directional_pool", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "four_hour_directional_pool_status", None)
    if callable(reader):
        payload = reader()
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _closed_history(
    core: Any,
    symbol: str,
    minimum: int,
    now_ms: int,
) -> list[dict[str, Any]]:
    candles, _ = core.fetch_candles(
        symbol,
        _INTERVAL,
        limit=max(80, minimum + 5),
    )
    closed = filter_closed_candles(candles or [], _INTERVAL, now_ms=now_ms)
    return closed if len(closed) >= minimum else []


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    """Build and persist the Top-20 list for the latest fully closed 1H bar."""
    timestamp = int(now or time.time())
    target_open_seconds = _target_candle_open_seconds(timestamp)
    target_open_ms = target_open_seconds * 1000
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        if _TREND_CLASSIFIER is None or _RANK_ROWS is None:
            raise RuntimeError("Hourly watchlist is not installed with worker dependencies")

        upstream = _four_hour_snapshot(core)
        upstream_symbols = [
            str(value or "").upper() for value in upstream.get("symbols") or []
        ]
        upstream_rows = {
            str(row.get("symbol") or "").upper(): dict(row)
            for row in upstream.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        if not upstream_symbols:
            raise RuntimeError("Closed-4H Top-50 directional pool is unavailable")

        minimum = int(settings()["minimumClosedCandles"])
        now_ms = timestamp * 1000
        qualified: list[dict[str, Any]] = []
        rejected = {
            "missing1hHistory": 0,
            "stale1hCandle": 0,
            "neutralOrUnclear": 0,
        }

        for symbol in upstream_symbols:
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

            parent = upstream_rows.get(symbol, {})
            four_hour_trend = parent.get("fourHourTrend") or parent.get("direction") or parent.get("trend")
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
                    "fourHourTrend": four_hour_trend,
                    "fourHourTrendScore": round(
                        float(parent.get("fourHourTrendScore") or 0), 4
                    ),
                    "directionChangedFrom4h": bool(
                        four_hour_trend and str(four_hour_trend) != trend
                    ),
                    "turnover24h": float(parent.get("turnover24h") or 0),
                    "spreadPct": float(parent.get("spreadPct") or 0),
                    "lastPrice": float(parent.get("lastPrice") or 0),
                    "reason": str(reason),
                    "lastScannedAt": timestamp,
                    "selectedAt": timestamp,
                }
            )

        ranked = _RANK_ROWS(qualified)
        selected = ranked[: int(settings()["watchlistSize"])]
        metrics = {
            "fourHourPoolInput": len(upstream_symbols),
            "oneHourQualified": len(qualified),
            "selected": len(selected),
            "bullish": sum(1 for row in selected if row.get("trend") == "BULLISH"),
            "bearish": sum(1 for row in selected if row.get("trend") == "BEARISH"),
            "directionChangedFrom4h": sum(
                1 for row in selected if row.get("directionChangedFrom4h")
            ),
            "alignmentRequired": False,
            "rankingPolicy": "existing_worker_rank_rows",
            "rejected": rejected,
        }
        payload = {
            "status": "ready" if selected else "empty",
            "version": 1,
            "source": "four_hour_top50_closed_1h_watchlist",
            "oneHourCandleTime": target_open_ms,
            "updatedAt": timestamp,
            "symbols": [str(row["symbol"]) for row in selected],
            "rows": selected,
            "metrics": metrics,
            "lastError": None
            if selected
            else "No BULLISH/BEARISH symbol qualified on the closed 1H bar",
            "persisted": False,
        }
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("symbols"))
            _STATE.update(
                {
                    "status": "stale" if has_cache else "error",
                    "lastError": str(exc),
                }
            )
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
    """Runtime-compatible replacement for the existing symbol-worker batch call."""
    return ensure_current(core, now=now)


def install(core: Any, worker_module: Any) -> dict[str, Any]:
    """Expose the persistent Top-20 snapshot through the existing worker API."""
    global _TREND_CLASSIFIER, _RANK_ROWS, _STORE
    if getattr(worker_module, "_hourly_watchlist_v1_installed", False):
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
    worker_module._hourly_watchlist_v1_installed = True

    core.hourly_watchlist = lambda force=False: build(core) if force else ensure_current(core)
    core.hourly_watchlist_status = snapshot
    return status(worker_module)


def status(worker_module: Any | None = None) -> dict[str, Any]:
    installed = bool(
        worker_module is not None
        and getattr(worker_module, "_hourly_watchlist_v1_installed", False)
    )
    return {
        "installed": installed,
        "policy": "EVERY_CLOSED_1H_TOP_20_EXISTING_WORKER_RANKING",
        "alignmentRequired": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _TREND_CLASSIFIER, _RANK_ROWS, _STORE
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 1,
                "source": "four_hour_top50_closed_1h_watchlist",
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
