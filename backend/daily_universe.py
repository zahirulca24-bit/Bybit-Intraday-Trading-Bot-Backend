"""Persistent daily master universe built from existing eligibility filters.

This module reuses the current symbol worker's eligibility filter and trend
classifier. Once installed, the worker consumes a cached daily 1D+4H aligned
master universe (up to 100 symbols) instead of rebuilding the full eligible
contract list every five minutes.

The scheduled refresh is 00:05 UTC (06:05 Asia/Dhaka) by default. A missing
cache is bootstrapped immediately, and any build failure falls back to the
previous worker source without deleting the last good snapshot.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

try:
    from .scanner_safety import filter_closed_candles
except ImportError:  # pragma: no cover
    from scanner_safety import filter_closed_candles


_PERSIST_KEY = "daily_master_universe_v1"
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_BASE_FETCH: Callable[[Any], tuple[list[str], dict[str, dict[str, Any]]]] | None = None
_TREND_CLASSIFIER: Callable[[list[dict[str, Any]]], tuple[str | None, float, str]] | None = None
_STORE: Any | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "source": "daily_1d_4h_aligned_trend",
    "runDayUtc": None,
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
        "universeSize": _integer("DAILY_UNIVERSE_SIZE", 100, 10, 200),
        "runHourUtc": _integer("DAILY_UNIVERSE_RUN_HOUR_UTC", 0, 0, 23),
        "runMinuteUtc": _integer("DAILY_UNIVERSE_RUN_MINUTE_UTC", 5, 0, 59),
        "minimumClosedCandles": _integer("DAILY_UNIVERSE_MIN_CLOSED_CANDLES", 60, 60, 240),
    }


def _target_run_day(timestamp: int) -> str:
    cfg = settings()
    current = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    scheduled = current.replace(
        hour=int(cfg["runHourUtc"]),
        minute=int(cfg["runMinuteUtc"]),
        second=0,
        microsecond=0,
    )
    if current < scheduled:
        scheduled -= timedelta(days=1)
    return scheduled.strftime("%Y-%m-%d")


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    rows = [dict(row) for row in _STATE.get("rows") or []]
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "source": str(_STATE.get("source") or "daily_1d_4h_aligned_trend"),
        "runDayUtc": _STATE.get("runDayUtc"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "symbols": list(_STATE.get("symbols") or []),
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
    symbols = [str(value or "").upper() for value in raw_symbols if str(value or "").strip()]
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    if bool(symbols) != bool(rows):
        return
    status = "ready" if symbols else "empty"
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or status),
                "version": int(saved.get("version") or 1),
                "source": str(saved.get("source") or "daily_1d_4h_aligned_trend"),
                "runDayUtc": saved.get("runDayUtc"),
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
        "runDayUtc": payload["runDayUtc"],
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
        and confirmed.get("runDayUtc") == body["runDayUtc"]
        and list(confirmed.get("symbols") or []) == body["symbols"]
    )


def _closed_history(
    core: Any,
    symbol: str,
    interval: str,
    minimum: int,
    now_ms: int,
) -> list[dict[str, Any]]:
    candles, _ = core.fetch_candles(symbol, interval, limit=max(80, minimum + 5))
    closed = filter_closed_candles(candles or [], interval, now_ms=now_ms)
    return closed if len(closed) >= minimum else []


def _rank_key(row: dict[str, Any]) -> tuple[float, float, float, str]:
    # The weaker timeframe controls the primary score. Liquidity and spread are
    # only deterministic tie-breakers, preserving trend as the selection basis.
    trend_score = float(row.get("trendScore") or 0)
    turnover = float(row.get("turnover24h") or 0)
    spread = float(row.get("spreadPct") or 0)
    return trend_score, turnover, -spread, str(row.get("symbol") or "")


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    """Build and persist the current scheduled daily master universe."""
    timestamp = int(now or time.time())
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        if _BASE_FETCH is None or _TREND_CLASSIFIER is None:
            raise RuntimeError("Daily universe is not installed with worker dependencies")

        cfg = settings()
        symbols, tickers = _BASE_FETCH(core)
        minimum = int(cfg["minimumClosedCandles"])
        now_ms = timestamp * 1000
        aligned: list[dict[str, Any]] = []
        rejected = {
            "missingDailyHistory": 0,
            "missing4hHistory": 0,
            "unclearDailyTrend": 0,
            "unclear4hTrend": 0,
            "timeframeConflict": 0,
        }

        for symbol in symbols:
            daily = _closed_history(core, symbol, "D", minimum, now_ms)
            if not daily:
                rejected["missingDailyHistory"] += 1
                continue
            four_hour = _closed_history(core, symbol, "240", minimum, now_ms)
            if not four_hour:
                rejected["missing4hHistory"] += 1
                continue

            daily_trend, daily_score, daily_reason = _TREND_CLASSIFIER(daily)
            if daily_trend not in {"BULLISH", "BEARISH"}:
                rejected["unclearDailyTrend"] += 1
                continue
            four_hour_trend, four_hour_score, four_hour_reason = _TREND_CLASSIFIER(four_hour)
            if four_hour_trend not in {"BULLISH", "BEARISH"}:
                rejected["unclear4hTrend"] += 1
                continue
            if daily_trend != four_hour_trend:
                rejected["timeframeConflict"] += 1
                continue

            market = dict(tickers.get(symbol) or {})
            aligned.append(
                {
                    "symbol": symbol,
                    "trend": daily_trend,
                    "dailyTrend": daily_trend,
                    "fourHourTrend": four_hour_trend,
                    "dailyTrendScore": round(float(daily_score), 4),
                    "fourHourTrendScore": round(float(four_hour_score), 4),
                    "trendScore": round(min(float(daily_score), float(four_hour_score)), 4),
                    "dailyReason": str(daily_reason),
                    "fourHourReason": str(four_hour_reason),
                    "lastPrice": float(market.get("lastPrice") or 0),
                    "turnover24h": float(market.get("turnover24h") or 0),
                    "spreadPct": float(market.get("spreadPct") or 0),
                    "selectedAt": timestamp,
                }
            )

        ranked = sorted(aligned, key=_rank_key, reverse=True)
        selected = ranked[: int(cfg["universeSize"])]
        metrics = {
            "eligibleInput": len(symbols),
            "trendAligned": len(aligned),
            "selected": len(selected),
            "bullish": sum(1 for row in selected if row.get("trend") == "BULLISH"),
            "bearish": sum(1 for row in selected if row.get("trend") == "BEARISH"),
            "rejected": rejected,
        }
        payload = {
            "status": "ready" if selected else "empty",
            "version": 1,
            "source": "daily_1d_4h_aligned_trend",
            "runDayUtc": _target_run_day(timestamp),
            "updatedAt": timestamp,
            "symbols": [row["symbol"] for row in selected],
            "rows": selected,
            "metrics": metrics,
            "lastError": None if selected else "No 1D+4H trend-aligned symbols passed existing eligibility filters",
            "persisted": False,
        }
        persisted = _persist(payload)
        payload["persisted"] = persisted
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
    target = _target_run_day(timestamp)
    with _STATE_LOCK:
        return str(_STATE.get("runDayUtc") or "") != target


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
    """Install the daily source without deleting or rewriting worker logic."""
    global _BASE_FETCH, _TREND_CLASSIFIER, _STORE
    if getattr(worker_module, "_daily_universe_v1_installed", False):
        return status(worker_module)

    base_fetch = getattr(worker_module, "_fetch_active_usdt_symbols", None)
    classifier = getattr(worker_module, "classify_trend", None)
    if not callable(base_fetch) or not callable(classifier):
        raise RuntimeError("Worker eligibility source or trend classifier is unavailable")

    _BASE_FETCH = base_fetch
    _TREND_CLASSIFIER = classifier
    _STORE = _persistent_store(core)
    _load_persisted()
    worker_module._fetch_active_usdt_symbols = _worker_source
    worker_module._daily_universe_v1_installed = True
    core.daily_master_universe = lambda force=False: build(core) if force else ensure_current(core)
    core.daily_master_universe_status = snapshot
    return status(worker_module)


def status(worker_module: Any | None = None) -> dict[str, Any]:
    installed = bool(
        worker_module is not None
        and getattr(worker_module, "_daily_universe_v1_installed", False)
    )
    return {
        "installed": installed,
        "policy": "DAILY_TOP_100_1D_4H_ALIGNED",
        "schedule": "00:05 UTC / 06:05 Asia-Dhaka",
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    """Reset module state for isolated unit tests."""
    global _BASE_FETCH, _TREND_CLASSIFIER, _STORE
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 1,
                "source": "daily_1d_4h_aligned_trend",
                "runDayUtc": None,
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
