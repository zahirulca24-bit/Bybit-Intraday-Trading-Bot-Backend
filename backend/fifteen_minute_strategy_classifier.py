"""Persistent closed-15M strategy classification for the hourly Top-20 watchlist."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

try:
    from .scanner_safety import filter_closed_candles
    from .strategy_step2_upgrade import grade_for_strength
except ImportError:  # pragma: no cover
    from scanner_safety import filter_closed_candles
    from strategy_step2_upgrade import grade_for_strength

_PERSIST_KEY = "fifteen_minute_strategy_classification_v1"
_INTERVAL = "15"
_ENTRY_INTERVAL = "5"
_INTERVAL_SECONDS = 15 * 60
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_STORE: Any | None = None
_SETUP_SETTINGS: Callable[[], dict[str, Any]] | None = None
_EXPECTED_SIDE: Callable[[str], str | None] | None = None
_ACTIONABLE_VOTE: Callable[[list[dict[str, Any]], str], dict[str, Any] | None] | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "source": "hourly_top20_closed_15m_strategy_classification",
    "fifteenMinuteCandleTime": None,
    "updatedAt": 0,
    "symbols": [],
    "rows": [],
    "metrics": {},
    "lastError": None,
    "persisted": False,
}


def _target_candle_open_seconds(timestamp: int) -> int:
    return ((int(timestamp) // _INTERVAL_SECONDS) - 1) * _INTERVAL_SECONDS


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "source": str(_STATE.get("source") or "hourly_top20_closed_15m_strategy_classification"),
        "fifteenMinuteCandleTime": _STATE.get("fifteenMinuteCandleTime"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "symbols": list(_STATE.get("symbols") or []),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "persisted": bool(_STATE.get("persisted")),
        "entryQueueWrites": 0,
        "orderSubmissions": 0,
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
    raw_symbols, raw_rows = saved.get("symbols"), saved.get("rows")
    if not isinstance(raw_symbols, list) or not isinstance(raw_rows, list):
        return
    symbols = [str(v or "").upper() for v in raw_symbols if str(v or "").strip()]
    rows = [dict(row) for row in raw_rows if isinstance(row, dict)]
    if bool(symbols) != bool(rows):
        return
    with _STATE_LOCK:
        _STATE.update({
            "status": str(saved.get("status") or ("ready" if rows else "empty")),
            "version": int(saved.get("version") or 1),
            "source": str(saved.get("source") or "hourly_top20_closed_15m_strategy_classification"),
            "fifteenMinuteCandleTime": saved.get("fifteenMinuteCandleTime"),
            "updatedAt": int(saved.get("updatedAt") or 0),
            "symbols": symbols,
            "rows": rows,
            "metrics": dict(saved.get("metrics") or {}),
            "lastError": saved.get("lastError"),
            "persisted": True,
        })


def _persist(payload: dict[str, Any]) -> bool:
    if _STORE is None:
        return False
    body = {k: payload[k] for k in ("status", "version", "source", "fifteenMinuteCandleTime", "updatedAt", "symbols", "rows", "metrics")}
    body["lastError"] = payload.get("lastError")
    try:
        _STORE.put(_PERSIST_KEY, body)
        confirmed = _STORE.get(_PERSIST_KEY)
    except Exception:
        return False
    return bool(isinstance(confirmed, dict) and confirmed.get("fifteenMinuteCandleTime") == body["fifteenMinuteCandleTime"] and list(confirmed.get("symbols") or []) == body["symbols"])


def _hourly_snapshot(core: Any) -> dict[str, Any]:
    for name, args in (("hourly_watchlist", (False,)), ("hourly_watchlist_status", ())):
        reader = getattr(core, name, None)
        if callable(reader):
            payload = reader(*args)
            if isinstance(payload, dict):
                return dict(payload)
    return {}


def _minimum_closed_candles() -> int:
    if _SETUP_SETTINGS is None:
        return 60
    try:
        return int((_SETUP_SETTINGS() or {}).get("minimumClosedCandles") or 60)
    except Exception:
        return 60


def _closed_15m_history(core: Any, symbol: str, now_ms: int) -> list[dict[str, Any]]:
    minimum = _minimum_closed_candles()
    candles, _ = core.fetch_candles(symbol, _INTERVAL, limit=max(80, minimum + 5))
    closed = filter_closed_candles(candles or [], _INTERVAL, now_ms=now_ms)
    return closed if len(closed) >= minimum else []


def _closed_15m_market_metrics(core: Any, history: list[dict[str, Any]]) -> dict[str, float | int | None]:
    if len(history) < 21:
        return {"atr15mPct": None, "volumeRatio": None, "marketMetricsCandleTime": None}
    closes = [float(row.get("close") or 0) for row in history]
    highs = [float(row.get("high") or 0) for row in history]
    lows = [float(row.get("low") or 0) for row in history]
    volumes = [float(row.get("volume") or 0) for row in history]
    atr = float(core.simple_atr(highs, lows, closes, 14) or 0)
    atr_pct = (atr / closes[-1]) * 100 if closes[-1] > 0 and atr > 0 else None
    baseline_rows = volumes[-21:-1]
    baseline = sum(baseline_rows) / len(baseline_rows) if baseline_rows and sum(baseline_rows) > 0 else 0
    volume_ratio = volumes[-1] / baseline if baseline > 0 else None
    return {
        "atr15mPct": round(atr_pct, 5) if atr_pct is not None else None,
        "volumeRatio": round(volume_ratio, 4) if volume_ratio is not None else None,
        "marketMetricsCandleTime": int(history[-1].get("time") or 0) or None,
    }


def _vote_payload(vote: dict[str, Any], expected_side: str) -> dict[str, Any]:
    item = dict(vote or {})
    grading = grade_for_strength(item.get("strength"))
    return {**item, "alignedWithWatchlist": item.get("signal") == expected_side, "grade": grading["grade"], "gradeScore": grading["gradeScore"], "gradeExecutionEligible": grading["executionEligible"], "watchOnly": grading["watchOnly"]}


def _classify_symbol(core: Any, watchlist_row: dict[str, Any], target_open_ms: int, now_ms: int) -> tuple[dict[str, Any], str | None]:
    symbol = str(watchlist_row.get("symbol") or "").upper()
    trend = str(watchlist_row.get("oneHourTrend") or watchlist_row.get("trend") or "").upper()
    base = {
        "symbol": symbol,
        "watchlistTrend": trend,
        "oneHourCandleTime": watchlist_row.get("oneHourCandleTime"),
        "fifteenMinuteCandleTime": target_open_ms,
        "atr15mPct": None,
        "volumeRatio": None,
        "marketMetricsCandleTime": None,
        "entryEligible": False,
        "queued": False,
        "executionStatus": "NOT_EVALUATED_STEP5",
    }
    if not symbol or _EXPECTED_SIDE is None or _ACTIONABLE_VOTE is None:
        return {**base, "status": "ERROR", "reason": "Strategy classifier dependencies are unavailable"}, "dependencyError"
    expected_side = _EXPECTED_SIDE(trend)
    if expected_side is None:
        return {**base, "status": "NO_SETUP", "reason": "Hourly watchlist direction is unsupported"}, "unsupportedDirection"

    history = _closed_15m_history(core, symbol, now_ms)
    if not history:
        return {**base, "expectedSide": expected_side, "status": "NO_SETUP", "reason": "Not enough fully closed 15M candle history"}, "missing15mHistory"
    market_metrics = _closed_15m_market_metrics(core, history)
    latest_15m = int(history[-1].get("time") or 0)
    base = {**base, **market_metrics}
    if latest_15m != target_open_ms:
        return {**base, "expectedSide": expected_side, "observedFifteenMinuteCandleTime": latest_15m, "status": "NO_SETUP", "reason": "Latest fully closed 15M candle is stale"}, "stale15mCandle"

    try:
        signal, reason, votes, router, indicators, engine_status = core.evaluate_signal(symbol, _ENTRY_INTERVAL, "aggressive")
    except Exception as exc:
        return {**base, "expectedSide": expected_side, "status": "ERROR", "reason": f"Strategy engine failed: {exc}"}, "engineError"

    indicators = dict(indicators or {})
    entry_interval = str(indicators.get("entryInterval") or "")
    five_minute_candle_time = indicators.get("signalCandleTime")
    if entry_interval != _ENTRY_INTERVAL or five_minute_candle_time is None:
        return {**base, "expectedSide": expected_side, "status": "ERROR", "engineSignal": signal, "engineReason": reason, "entryInterval": entry_interval or None, "latestFiveMinuteCandleTime": five_minute_candle_time, "reason": "Real closed 5M strategy context is unavailable"}, "invalid5mContext"

    raw_votes = [dict(vote) for vote in votes or [] if isinstance(vote, dict)]
    classified_votes = [_vote_payload(vote, expected_side) for vote in raw_votes]
    aligned_vote = _ACTIONABLE_VOTE(raw_votes, expected_side)
    aligned_engines = [str(vote.get("engine") or "") for vote in raw_votes if vote.get("signal") == expected_side]
    opposing_side = "Sell" if expected_side == "Buy" else "Buy"
    opposing_engines = [str(vote.get("engine") or "") for vote in raw_votes if vote.get("signal") == opposing_side]
    waiting_engines = [str(vote.get("engine") or "") for vote in raw_votes if vote.get("signal") == "WAIT"]
    common = {
        **base,
        "expectedSide": expected_side,
        "engineSignal": signal,
        "engineReason": reason,
        "router": dict(router or {}),
        "engineStatus": dict(engine_status or {}),
        "indicators": indicators,
        "entryInterval": entry_interval,
        "latestFiveMinuteCandleTime": five_minute_candle_time,
        "usesRealFiveMinuteContext": True,
        "strategyVotes": classified_votes,
        "matchedStrategies": aligned_engines,
        "opposingStrategies": opposing_engines,
        "waitingStrategies": waiting_engines,
    }
    if aligned_vote is not None:
        grading = grade_for_strength(aligned_vote.get("strength"))
        return {**common, "status": "SETUP_CLASSIFIED", "strategy": aligned_vote.get("engine"), "strategyReason": aligned_vote.get("reason"), "strategyStrength": aligned_vote.get("strength", 0), "grade": grading["grade"], "gradeScore": grading["gradeScore"], "gradeExecutionEligible": grading["executionEligible"], "watchOnly": grading["watchOnly"], "reason": "Trend-aligned strategy setup classified; awaiting Step 6 closed-5M entry confirmation"}, None
    if waiting_engines:
        return {**common, "status": "WATCHING", "strategy": None, "reason": "No trend-aligned actionable vote; existing strategies remain in WAIT state"}, None
    return {**common, "status": "NO_SETUP", "strategy": None, "reason": "No trend-aligned strategy setup on the closed 15M candle"}, None


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    timestamp = int(now or time.time())
    target_open_ms = _target_candle_open_seconds(timestamp) * 1000
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")
    try:
        upstream = _hourly_snapshot(core)
        upstream_rows = [dict(row) for row in upstream.get("rows") or [] if isinstance(row, dict) and row.get("symbol")]
        if not upstream_rows:
            raise RuntimeError("Hourly Top-20 watchlist is unavailable")
        now_ms = timestamp * 1000
        rows: list[dict[str, Any]] = []
        rejected = {"missing15mHistory": 0, "stale15mCandle": 0, "unsupportedDirection": 0, "invalid5mContext": 0, "engineError": 0, "dependencyError": 0}
        for watchlist_row in upstream_rows[:20]:
            row, rejection = _classify_symbol(core, watchlist_row, target_open_ms, now_ms)
            rows.append(row)
            if rejection in rejected:
                rejected[rejection] += 1
        metrics = {
            "hourlyWatchlistInput": len(upstream_rows),
            "processed": len(rows),
            "setupClassified": sum(1 for row in rows if row.get("status") == "SETUP_CLASSIFIED"),
            "watching": sum(1 for row in rows if row.get("status") == "WATCHING"),
            "noSetup": sum(1 for row in rows if row.get("status") == "NO_SETUP"),
            "errors": sum(1 for row in rows if row.get("status") == "ERROR"),
            "marketMetricsPublished": sum(1 for row in rows if row.get("atr15mPct") is not None and row.get("volumeRatio") is not None),
            "entryQueueWrites": 0,
            "orderSubmissions": 0,
            "strategyPolicy": "existing_strategy_votes_and_grading",
            "entryConfirmationPolicy": "STEP6_CLOSED_5M_REQUIRED",
            "rejected": rejected,
        }
        payload = {"status": "ready" if rows else "empty", "version": 1, "source": "hourly_top20_closed_15m_strategy_classification", "fifteenMinuteCandleTime": target_open_ms, "updatedAt": timestamp, "symbols": [str(row.get("symbol") or "") for row in rows], "rows": rows, "metrics": metrics, "lastError": None if rows else "No watchlist symbol was classified", "persisted": False}
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("rows"))
            _STATE.update({"status": "stale" if has_cache else "error", "lastError": str(exc)})
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(now: int | None = None) -> bool:
    timestamp = int(now or time.time())
    target_ms = _target_candle_open_seconds(timestamp) * 1000
    with _STATE_LOCK:
        return int(_STATE.get("fifteenMinuteCandleTime") or 0) != target_ms


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    return build(core, now=now) if due(now) else snapshot()


def install(core: Any, setup_worker: Any) -> dict[str, Any]:
    global _STORE, _SETUP_SETTINGS, _EXPECTED_SIDE, _ACTIONABLE_VOTE
    if getattr(core, "_fifteen_minute_strategy_classifier_v1_installed", False):
        return status(core)
    setup_settings = getattr(setup_worker, "settings", None)
    expected_side = getattr(setup_worker, "_expected_side", None)
    actionable_vote = getattr(setup_worker, "_actionable_vote", None)
    if not all(callable(value) for value in (setup_settings, expected_side, actionable_vote)):
        raise RuntimeError("Existing setup-worker classification helpers are unavailable")
    _STORE = _persistent_store(core)
    _SETUP_SETTINGS = setup_settings
    _EXPECTED_SIDE = expected_side
    _ACTIONABLE_VOTE = actionable_vote
    _load_persisted()
    core.fifteen_minute_strategy_classification = lambda force=False: build(core) if force else ensure_current(core)
    core.fifteen_minute_strategy_classification_status = snapshot
    setattr(core, "_fifteen_minute_strategy_classifier_v1_installed", True)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    return {"installed": bool(core is not None and getattr(core, "_fifteen_minute_strategy_classifier_v1_installed", False)), "policy": "EVERY_CLOSED_15M_CLASSIFY_EXISTING_STRATEGIES", "usesRealFiveMinuteContext": True, "publishesClosed15mMarketMetrics": True, "createsEntryCandidate": False, "writesConfirmedQueue": False, "submitsOrder": False, "snapshot": snapshot()}


def _reset_for_tests() -> None:
    global _STORE, _SETUP_SETTINGS, _EXPECTED_SIDE, _ACTIONABLE_VOTE
    with _STATE_LOCK:
        _STATE.update({"status": "idle", "version": 1, "source": "hourly_top20_closed_15m_strategy_classification", "fifteenMinuteCandleTime": None, "updatedAt": 0, "symbols": [], "rows": [], "metrics": {}, "lastError": None, "persisted": False})
    _STORE = None
    _SETUP_SETTINGS = None
    _EXPECTED_SIDE = None
    _ACTIONABLE_VOTE = None
