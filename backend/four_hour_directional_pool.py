"""Persistent four-hour directional pool derived from the daily Top-100 universe.

This module is intentionally additive. It consumes the canonical daily universe,
reuses the existing worker trend classifier, evaluates only fully closed 4H
candles, and exposes up to 50 BULLISH/BEARISH symbols to the existing worker.

There is no forced bullish/bearish quota. The strongest qualifying directions
win the ranking, and an empty/unavailable directional snapshot falls back to the
previous worker source without deleting the last usable upstream logic.
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


_PERSIST_KEY = "four_hour_directional_pool_v1"
_INTERVAL = "240"
_INTERVAL_SECONDS = 4 * 60 * 60
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_BASE_FETCH: Callable[[Any], tuple[list[str], dict[str, dict[str, Any]]]] | None = None
_TREND_CLASSIFIER: Callable[[list[dict[str, Any]]], tuple[str | None, float, str]] | None = None
_STORE: Any | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "source": "daily_top100_closed_4h_directional",
    "fourHourCandleTime": None,
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
        "poolSize": _integer("FOUR_HOUR_DIRECTIONAL_POOL_SIZE", 50, 10, 100),
        "minimumClosedCandles": _integer(
            "FOUR_HOUR_DIRECTIONAL_MIN_CLOSED_CANDLES", 60, 60, 240
        ),
    }


def _target_candle_open_seconds(timestamp: int) -> int:
    """Return the opening time of the latest fully closed UTC-aligned 4H bar."""
    return ((int(timestamp) // _INTERVAL_SECONDS) - 1) * _INTERVAL_SECONDS


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "source": str(_STATE.get("source") or "daily_top100_closed_4h_directional"),
        "fourHourCandleTime": _STATE.get("fourHourCandleTime"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "symbols": list(_STATE.get("symbols") or []),
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
    symbols = [str(value or "").upper() for value in raw_symbols if str(value or "").strip()]
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    if bool(symbols) != bool(rows):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or ("ready" if symbols else "empty")),
                "version": int(saved.get("version") or 1),
                "source": str(saved.get("source") or "daily_top100_closed_4h_directional"),
                "fourHourCandleTime": saved.get("fourHourCandleTime"),
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
        "fourHourCandleTime": payload["fourHourCandleTime"],
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
        and confirmed.get("fourHourCandleTime") == body["fourHourCandleTime"]
        and list(confirmed.get("symbols") or []) == body["symbols"]
    )


def _daily_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "daily_master_universe", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "daily_master_universe_status", None)
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
    candles, _ = core.fetch_candles(symbol, _INTERVAL, limit=max(80, minimum + 5))
    closed = filter_closed_candles(candles or [], _INTERVAL, now_ms=now_ms)
    return closed if len(closed) >= minimum else []


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, float, str]:
    return (
        float(row.get("fourHourTrendScore") or 0),
        float(row.get("dailyTrendScore") or 0),
        float(row.get("turnover24h") or 0),
        -float(row.get("spreadPct") or 0),
        str(row.get("symbol") or ""),
    )


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    """Build and persist the Top-50 directional pool for the latest closed 4H bar."""
    timestamp = int(now or time.time())
    target_open_seconds = _target_candle_open_seconds(timestamp)
    target_open_ms = target_open_seconds * 1000
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        if _TREND_CLASSIFIER is None:
            raise RuntimeError("4H directional pool is not installed with a trend classifier")

        daily = _daily_snapshot(core)
        daily_symbols = [str(value or "").upper() for value in daily.get("symbols") or []]
        daily_rows = {
            str(row.get("symbol") or "").upper(): dict(row)
            for row in daily.get("rows") or []
            if isinstance(row, dict) and row.get("symbol")
        }
        if not daily_symbols:
            raise RuntimeError("Daily Top-100 universe is unavailable")

        minimum = int(settings()["minimumClosedCandles"])
        now_ms = timestamp * 1000
        directional: list[dict[str, Any]] = []
        rejected = {
            "missing4hHistory": 0,
            "stale4hCandle": 0,
            "neutralOrUnclear": 0,
        }

        for symbol in daily_symbols:
            history = _closed_history(core, symbol, minimum, now_ms)
            if not history:
                rejected["missing4hHistory"] += 1
                continue
            latest_candle_time = int(history[-1].get("time") or 0)
            if latest_candle_time != target_open_ms:
                rejected["stale4hCandle"] += 1
                continue

            direction, score, reason = _TREND_CLASSIFIER(history)
            if direction not in {"BULLISH", "BEARISH"}:
                rejected["neutralOrUnclear"] += 1
                continue

            upstream = daily_rows.get(symbol, {})
            directional.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "trend": direction,
                    "fourHourTrend": direction,
                    "fourHourTrendScore": round(float(score), 4),
                    "fourHourReason": str(reason),
                    "fourHourCandleTime": latest_candle_time,
                    "dailyTrend": upstream.get("dailyTrend") or upstream.get("trend"),
                    "dailyTrendScore": round(float(upstream.get("dailyTrendScore") or 0), 4),
                    "directionChangedFromDaily": bool(
                        upstream.get("dailyTrend")
                        and str(upstream.get("dailyTrend")) != direction
                    ),
                    "lastPrice": float(upstream.get("lastPrice") or 0),
                    "turnover24h": float(upstream.get("turnover24h") or 0),
                    "spreadPct": float(upstream.get("spreadPct") or 0),
                    "selectedAt": timestamp,
                }
            )

        ranked = sorted(directional, key=_rank_key, reverse=True)
        selected = ranked[: int(settings()["poolSize"])]
        metrics = {
            "dailyUniverseInput": len(daily_symbols),
            "directionalQualified": len(directional),
            "selected": len(selected),
            "bullish": sum(1 for row in selected if row.get("direction") == "BULLISH"),
            "bearish": sum(1 for row in selected if row.get("direction") == "BEARISH"),
            "forcedDirectionQuota": False,
            "rejected": rejected,
        }
        payload = {
            "status": "ready" if selected else "empty",
            "version": 1,
            "source": "daily_top100_closed_4h_directional",
            "fourHourCandleTime": target_open_ms,
            "updatedAt": timestamp,
            "symbols": [row["symbol"] for row in selected],
            "rows": selected,
            "metrics": metrics,
            "lastError": None if selected else "No BULLISH/BEARISH symbol qualified for the closed 4H bar",
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
        return int(_STATE.get("fourHourCandleTime") or 0) != target_ms


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    return build(core, now=now) if due(now) else snapshot()


def _worker_source(core: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    current = ensure_current(core)
    rows = [dict(row) for row in current.get("rows") or []]
    if rows:
        tickers = {
            str(row["symbol"]): {
                "lastPrice": float(row.get("lastPrice") or 0),
                "turnover24h": float(row.get("turnover24h") or 0),
                "spreadPct": float(row.get("spreadPct") or 0),
            }
            for row in rows
        }
        return [str(row["symbol"]) for row in rows], tickers
    if _BASE_FETCH is None:
        return [], {}
    return _BASE_FETCH(core)


def install(core: Any, worker_module: Any) -> dict[str, Any]:
    """Layer the 4H Top-50 source over the existing daily-universe worker source."""
    global _BASE_FETCH, _TREND_CLASSIFIER, _STORE
    if getattr(worker_module, "_four_hour_directional_pool_v1_installed", False):
        return status(worker_module)

    base_fetch = getattr(worker_module, "_fetch_active_usdt_symbols", None)
    classifier = getattr(worker_module, "classify_trend", None)
    if not callable(base_fetch) or not callable(classifier):
        raise RuntimeError("Existing worker source or trend classifier is unavailable")

    _BASE_FETCH = base_fetch
    _TREND_CLASSIFIER = classifier
    _STORE = _persistent_store(core)
    _load_persisted()
    worker_module._fetch_active_usdt_symbols = _worker_source
    worker_module._four_hour_directional_pool_v1_installed = True
    core.four_hour_directional_pool = lambda force=False: build(core) if force else ensure_current(core)
    core.four_hour_directional_pool_status = snapshot
    return status(worker_module)


def status(worker_module: Any | None = None) -> dict[str, Any]:
    installed = bool(
        worker_module is not None
        and getattr(worker_module, "_four_hour_directional_pool_v1_installed", False)
    )
    return {
        "installed": installed,
        "policy": "EVERY_CLOSED_4H_TOP_50_BULLISH_BEARISH",
        "forcedDirectionQuota": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _BASE_FETCH, _TREND_CLASSIFIER, _STORE
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 1,
                "source": "daily_top100_closed_4h_directional",
                "fourHourCandleTime": None,
                "updatedAt": 0,
                "symbols": [],
                "rows": [],
                "metrics": {},
                "lastError": None,
                "persisted": False,
            }
        )
    _BASE_FETCH = None
    _TREND_CLASSIFIER = None
    _STORE = None
