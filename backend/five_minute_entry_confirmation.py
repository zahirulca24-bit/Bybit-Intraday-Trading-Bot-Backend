"""Persistent closed-5M candle confirmation for classified 15M setups.

This stage consumes the latest canonical closed-15M strategy classification. An
A+/A setup is confirmed only when a later fully closed 5-minute candle is
favorable to the classified side: bullish for Buy, bearish for Sell. The 5M
stage is confirmation only; it does not re-run strategy voting or re-grade the
setup.

Step 6 does not run risk checks, calculate position size, or submit an order.
Confirmed records remain PENDING_RISK for the later authoritative risk, sizing,
and Node.js execution stages.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

try:
    from .scanner_safety import filter_closed_candles
except ImportError:  # pragma: no cover
    from scanner_safety import filter_closed_candles


_PERSIST_KEY = "five_minute_entry_confirmation_v1"
_FIVE_MINUTE_INTERVAL = "5"
_FIFTEEN_MINUTE_SECONDS = 15 * 60
_FIVE_MINUTE_SECONDS = 5 * 60
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_STORE: Any | None = None
_SETUP_SETTINGS: Callable[[], dict[str, Any]] | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "source": "closed_15m_setup_favorable_closed_5m_confirmation",
    "fiveMinuteCandleTime": None,
    "setupFifteenMinuteCandleTime": None,
    "updatedAt": 0,
    "rows": [],
    "confirmedEntryQueue": [],
    "metrics": {},
    "lastError": None,
    "persisted": False,
}


def _target_open_seconds(timestamp: int, interval_seconds: int) -> int:
    """Return the opening time of the latest fully closed aligned candle."""
    return ((int(timestamp) // interval_seconds) - 1) * interval_seconds


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    queue = [dict(row) for row in _STATE.get("confirmedEntryQueue") or []]
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "source": str(
            _STATE.get("source")
            or "closed_15m_setup_favorable_closed_5m_confirmation"
        ),
        "fiveMinuteCandleTime": _STATE.get("fiveMinuteCandleTime"),
        "setupFifteenMinuteCandleTime": _STATE.get(
            "setupFifteenMinuteCandleTime"
        ),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "confirmedEntryQueue": queue,
        "confirmedEntryQueueSize": len(queue),
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "persisted": bool(_STATE.get("persisted")),
        "riskChecks": 0,
        "positionSizingCalls": 0,
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
    raw_rows = saved.get("rows")
    raw_queue = saved.get("confirmedEntryQueue")
    if not isinstance(raw_rows, list) or not isinstance(raw_queue, list):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or "idle"),
                "version": int(saved.get("version") or 1),
                "source": str(
                    saved.get("source")
                    or "closed_15m_setup_favorable_closed_5m_confirmation"
                ),
                "fiveMinuteCandleTime": saved.get("fiveMinuteCandleTime"),
                "setupFifteenMinuteCandleTime": saved.get(
                    "setupFifteenMinuteCandleTime"
                ),
                "updatedAt": int(saved.get("updatedAt") or 0),
                "rows": [dict(row) for row in raw_rows if isinstance(row, dict)],
                "confirmedEntryQueue": [
                    dict(row) for row in raw_queue if isinstance(row, dict)
                ],
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
        "fiveMinuteCandleTime": payload["fiveMinuteCandleTime"],
        "setupFifteenMinuteCandleTime": payload[
            "setupFifteenMinuteCandleTime"
        ],
        "updatedAt": payload["updatedAt"],
        "rows": payload["rows"],
        "confirmedEntryQueue": payload["confirmedEntryQueue"],
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
        and confirmed.get("fiveMinuteCandleTime")
        == body["fiveMinuteCandleTime"]
        and list(confirmed.get("confirmedEntryQueue") or [])
        == body["confirmedEntryQueue"]
    )


def _classification_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "fifteen_minute_strategy_classification_status", None)
    if callable(reader):
        payload = reader()
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "fifteen_minute_strategy_classification", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _queue_limit() -> int:
    if _SETUP_SETTINGS is None:
        return 100
    try:
        return max(1, int((_SETUP_SETTINGS() or {}).get("queueLimit") or 100))
    except Exception:
        return 100


def _closed_five_minute_history(
    core: Any,
    symbol: str,
    now_ms: int,
) -> list[dict[str, Any]]:
    candles, _ = core.fetch_candles(symbol, _FIVE_MINUTE_INTERVAL, limit=120)
    return filter_closed_candles(
        candles or [],
        _FIVE_MINUTE_INTERVAL,
        now_ms=now_ms,
    )


def _queue_candidate(
    queue: list[dict[str, Any]],
    candidate: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    key = str(candidate.get("candidateKey") or "")
    if any(str(row.get("candidateKey") or "") == key for row in queue):
        return queue, False
    updated = [*queue, dict(candidate)]
    return updated[-_queue_limit():], True


def _confirm_symbol(
    core: Any,
    setup: dict[str, Any],
    target_five_minute_ms: int,
    target_setup_ms: int,
    now_ms: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    symbol = str(setup.get("symbol") or "").upper()
    side = str(setup.get("expectedSide") or "")
    strategy = str(setup.get("strategy") or "")
    base = {
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "setupFifteenMinuteCandleTime": target_setup_ms,
        "entryFiveMinuteCandleTime": target_five_minute_ms,
        "setupGrade": setup.get("grade"),
        "setupGradeScore": setup.get("gradeScore"),
        "status": "ENTRY_WAIT",
        "confirmed": False,
    }

    if not symbol or side not in {"Buy", "Sell"} or not strategy:
        return {
            **base,
            "status": "SETUP_INVALIDATED",
            "reason": "Classified setup identity is incomplete",
        }, None, "invalidSetupIdentity"

    if not setup.get("gradeExecutionEligible") or str(setup.get("grade") or "") not in {"A+", "A"}:
        return {
            **base,
            "status": "BLOCKED_GRADE",
            "reason": "Classified 15M setup is not A+/A execution eligible",
        }, None, "setupGradeBlocked"

    history = _closed_five_minute_history(core, symbol, now_ms)
    if not history:
        return {
            **base,
            "status": "ENTRY_WAIT",
            "reason": "Fully closed 5M candle history is unavailable",
        }, None, "missing5mHistory"

    latest = history[-1]
    observed_five_minute_ms = int(latest.get("time") or 0)
    if observed_five_minute_ms != target_five_minute_ms:
        return {
            **base,
            "observedFiveMinuteCandleTime": observed_five_minute_ms,
            "status": "ENTRY_WAIT",
            "reason": "Latest fully closed 5M candle is stale",
        }, None, "stale5mCandle"

    try:
        candle_open = float(latest.get("open") or 0)
        candle_close = float(latest.get("close") or 0)
    except (TypeError, ValueError):
        candle_open = 0.0
        candle_close = 0.0
    if candle_open <= 0 or candle_close <= 0:
        return {
            **base,
            "status": "ERROR",
            "observedFiveMinuteCandleTime": observed_five_minute_ms,
            "reason": "Closed-5M candle open/close is invalid",
        }, None, "invalidEntryReference"

    favorable = (
        (side == "Buy" and candle_close > candle_open)
        or (side == "Sell" and candle_close < candle_open)
    )
    common = {
        **base,
        "observedFiveMinuteCandleTime": observed_five_minute_ms,
        "fiveMinuteOpen": round(candle_open, 12),
        "fiveMinuteClose": round(candle_close, 12),
        "fiveMinuteDirection": (
            "BULLISH" if candle_close > candle_open
            else "BEARISH" if candle_close < candle_open
            else "FLAT"
        ),
        "confirmationRule": "FAVORABLE_CLOSED_5M_CANDLE",
    }
    if not favorable:
        return {
            **common,
            "status": "ENTRY_WAIT",
            "reason": f"Closed 5M candle is not favorable for {side}",
        }, None, None

    entry_reference = candle_close
    candidate_key = (
        f"{symbol}:{target_setup_ms}:{target_five_minute_ms}:{side}:{strategy}"
    )
    candidate = {
        "candidateKey": candidate_key,
        "symbol": symbol,
        "side": side,
        "strategy": strategy,
        "setupFifteenMinuteCandleTime": target_setup_ms,
        "entryFiveMinuteCandleTime": target_five_minute_ms,
        "entryReference": round(entry_reference, 12),
        "entryReferenceSource": "CLOSED_5M_CLOSE",
        "setupStrategyReason": setup.get("strategyReason"),
        "entryStrategyReason": f"Favorable closed 5M candle confirmed {side} entry",
        "strategyStrength": setup.get("strategyStrength", 0),
        "grade": setup.get("grade"),
        "gradeScore": setup.get("gradeScore"),
        "setupGrade": setup.get("grade"),
        "setupGradeScore": setup.get("gradeScore"),
        "fiveMinuteOpen": round(candle_open, 12),
        "fiveMinuteClose": round(candle_close, 12),
        "confirmationRule": "FAVORABLE_CLOSED_5M_CANDLE",
        "createdAt": int(now_ms / 1000),
        "riskStatus": "PENDING_RISK",
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "AWAITING_NODE_EXECUTION",
        "orderSubmitted": False,
    }
    return {
        **common,
        "status": "ENTRY_CONFIRMED",
        "confirmed": True,
        "entryGrade": setup.get("grade"),
        "entryGradeScore": setup.get("gradeScore"),
        "entryReference": candidate["entryReference"],
        "candidateKey": candidate_key,
        "reason": "Favorable closed 5M candle confirmed the classified 15M setup",
    }, candidate, None


def build(core: Any, now: int | None = None) -> dict[str, Any]:
    """Confirm eligible 15M setups once for the latest fully closed 5M bar."""
    timestamp = int(now or time.time())
    target_five_minute_ms = (
        _target_open_seconds(timestamp, _FIVE_MINUTE_SECONDS) * 1000
    )
    target_setup_ms = (
        _target_open_seconds(timestamp, _FIFTEEN_MINUTE_SECONDS) * 1000
    )
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        classification = _classification_snapshot(core)
        if classification.get("status") != "ready":
            raise RuntimeError("Closed-15M strategy classification is not ready")
        observed_setup_ms = int(
            classification.get("fifteenMinuteCandleTime") or 0
        )
        if observed_setup_ms != target_setup_ms:
            raise RuntimeError("Closed-15M strategy classification is stale")

        # A setup candle must close before a later 5M candle can confirm entry.
        setup_close_ms = target_setup_ms + (_FIFTEEN_MINUTE_SECONDS * 1000)
        if target_five_minute_ms < setup_close_ms:
            payload = {
                "status": "waiting",
                "version": 1,
                "source": "closed_15m_setup_favorable_closed_5m_confirmation",
                "fiveMinuteCandleTime": target_five_minute_ms,
                "setupFifteenMinuteCandleTime": target_setup_ms,
                "updatedAt": timestamp,
                "rows": [],
                "confirmedEntryQueue": list(
                    _STATE.get("confirmedEntryQueue") or []
                ),
                "metrics": {
                    "classifiedInput": 0,
                    "processed": 0,
                    "confirmed": 0,
                    "reason": "Awaiting the first closed 5M candle after the 15M setup close",
                    "riskChecks": 0,
                    "positionSizingCalls": 0,
                    "orderSubmissions": 0,
                },
                "lastError": None,
                "persisted": False,
            }
            payload["persisted"] = _persist(payload)
            with _STATE_LOCK:
                _STATE.update(payload)
                return _snapshot_unlocked()

        setup_rows = [
            dict(row)
            for row in classification.get("rows") or []
            if isinstance(row, dict)
            and row.get("status") == "SETUP_CLASSIFIED"
        ]
        now_ms = timestamp * 1000
        rows: list[dict[str, Any]] = []
        queue = [dict(row) for row in _STATE.get("confirmedEntryQueue") or []]
        queued_now = 0
        rejected = {
            "invalidSetupIdentity": 0,
            "setupGradeBlocked": 0,
            "missing5mHistory": 0,
            "stale5mCandle": 0,
            "invalidEntryReference": 0,
        }

        for setup in setup_rows:
            row, candidate, rejection = _confirm_symbol(
                core,
                setup,
                target_five_minute_ms,
                target_setup_ms,
                now_ms,
            )
            if candidate is not None:
                queue, added = _queue_candidate(queue, candidate)
                row["queued"] = added
                queued_now += int(added)
                if not added:
                    row["reason"] = "Closed-5M entry was already confirmed and queued"
            else:
                row["queued"] = False
            rows.append(row)
            if rejection in rejected:
                rejected[rejection] += 1

        metrics = {
            "classifiedInput": len(setup_rows),
            "processed": len(rows),
            "confirmed": sum(
                1 for row in rows if row.get("status") == "ENTRY_CONFIRMED"
            ),
            "queuedNow": queued_now,
            "entryWait": sum(
                1 for row in rows if row.get("status") == "ENTRY_WAIT"
            ),
            "invalidated": sum(
                1 for row in rows if row.get("status") == "SETUP_INVALIDATED"
            ),
            "gradeBlocked": sum(
                1 for row in rows if row.get("status") == "BLOCKED_GRADE"
            ),
            "errors": sum(1 for row in rows if row.get("status") == "ERROR"),
            "riskChecks": 0,
            "positionSizingCalls": 0,
            "orderSubmissions": 0,
            "confirmationPolicy": "FAVORABLE_CLOSED_5M_CANDLE_ONLY",
            "strategyRevoteAt5m": False,
            "regradeAt5m": False,
            "rejected": rejected,
        }
        payload = {
            "status": "ready",
            "version": 1,
            "source": "closed_15m_setup_favorable_closed_5m_confirmation",
            "fiveMinuteCandleTime": target_five_minute_ms,
            "setupFifteenMinuteCandleTime": target_setup_ms,
            "updatedAt": timestamp,
            "rows": rows,
            "confirmedEntryQueue": queue,
            "metrics": metrics,
            "lastError": None,
            "persisted": False,
        }
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("rows") or _STATE.get("confirmedEntryQueue"))
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
    target_ms = _target_open_seconds(timestamp, _FIVE_MINUTE_SECONDS) * 1000
    with _STATE_LOCK:
        return int(_STATE.get("fiveMinuteCandleTime") or 0) != target_ms


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    return build(core, now=now) if due(now) else snapshot()


def install(core: Any, setup_worker: Any) -> dict[str, Any]:
    global _STORE, _SETUP_SETTINGS
    if getattr(core, "_five_minute_entry_confirmation_v1_installed", False):
        return status(core)

    setup_settings = getattr(setup_worker, "settings", None)
    if not callable(setup_settings):
        raise RuntimeError("Existing setup-worker queue policy is unavailable")

    _STORE = _persistent_store(core)
    _SETUP_SETTINGS = setup_settings
    _load_persisted()
    core.five_minute_entry_confirmation = (
        lambda force=False: build(core) if force else ensure_current(core)
    )
    core.five_minute_entry_confirmation_status = snapshot
    setattr(core, "_five_minute_entry_confirmation_v1_installed", True)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    return {
        "installed": bool(
            core is not None
            and getattr(core, "_five_minute_entry_confirmation_v1_installed", False)
        ),
        "policy": "CLOSED_15M_SETUP_THEN_FAVORABLE_CLOSED_5M_CANDLE",
        "strategyRevoteAt5m": False,
        "regradeAt5m": False,
        "riskChecks": False,
        "positionSizing": False,
        "submitsOrder": False,
        "executionTarget": "NODE_JS_STEP10",
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _STORE, _SETUP_SETTINGS
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 1,
                "source": "closed_15m_setup_favorable_closed_5m_confirmation",
                "fiveMinuteCandleTime": None,
                "setupFifteenMinuteCandleTime": None,
                "updatedAt": 0,
                "rows": [],
                "confirmedEntryQueue": [],
                "metrics": {},
                "lastError": None,
                "persisted": False,
            }
        )
    _STORE = None
    _SETUP_SETTINGS = None
