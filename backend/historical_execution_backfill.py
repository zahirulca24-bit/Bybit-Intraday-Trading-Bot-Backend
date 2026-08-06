"""Operator-safe historical Bybit Demo execution backfill.

Reads authenticated Bybit Demo fills, reconstructs cross-day position cycles,
and persists them idempotently through the canonical live execution ledger.
This module is read-only with respect to the exchange.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

try:
    from . import live_execution_ledger
except ImportError:  # pragma: no cover
    import live_execution_ledger

BACKFILL_PATH = "/api/live-executions/backfill"
DEFAULT_DAYS = 30
MAX_DAYS = 30
_LOCK = threading.Lock()
_LAST_RESULT: dict[str, Any] = {
    "ok": False,
    "running": False,
    "reason": "Historical execution backfill has not run.",
}


def _date_keys(current_date: str, days: int) -> list[str]:
    current = datetime.strptime(current_date, "%Y-%m-%d")
    return [
        (current - timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days - 1, -1, -1)
    ]


def _bounded_days(value: Any) -> int:
    try:
        days = int(value or DEFAULT_DAYS)
    except (TypeError, ValueError) as exc:
        raise live_execution_ledger.LiveExecutionValidationError("days must be an integer.") from exc
    if days < 1 or days > MAX_DAYS:
        raise live_execution_ledger.LiveExecutionValidationError(
            f"days must be between 1 and {MAX_DAYS}."
        )
    return days


def run(core: Any, *, days: Any = DEFAULT_DAYS) -> dict[str, Any]:
    """Backfill a bounded range and reconcile fills across day boundaries."""
    global _LAST_RESULT
    bounded_days = _bounded_days(days)
    if not _LOCK.acquire(blocking=False):
        raise live_execution_ledger.LiveExecutionBusyError(
            "Historical execution backfill is already running."
        )
    try:
        service = live_execution_ledger._service(core)
        service.ensure_schema()
        current_date = core.get_current_trading_date_key()
        now_ms = service._now_ms()
        dates = _date_keys(current_date, bounded_days)

        combined: list[dict[str, Any]] = []
        windows: dict[str, tuple[int, int]] = {}
        for date_key in dates:
            start_ms, end_ms = live_execution_ledger._date_window_ms(core, date_key, now_ms)
            windows[date_key] = (start_ms, end_ms)
            combined.extend(service._fetch_execution_pages(start_ms, end_ms, date_key))

        # Reconcile the complete ordered stream against current Bybit exposure.
        # This correctly joins entries opened on one day and closed on another.
        positions = service._fetch_positions()
        reconciled = live_execution_ledger.reconcile_executions(combined, positions)
        service._persist(reconciled, now_ms)

        per_date: list[dict[str, Any]] = []
        for date_key in dates:
            rows = service._rows_for_date(date_key)
            summary = live_execution_ledger.summarize_rows(
                rows,
                trading_date=date_key,
                current_positions=positions if date_key == current_date else {},
            )
            start_ms, end_ms = windows[date_key]
            summary.update(
                ok=True,
                stale=False,
                historicalBackfill=True,
                lastSyncedAt=now_ms,
                windowStart=start_ms,
                windowEnd=end_ms,
                rowsFetched=sum(1 for row in combined if row.get("tradingDate") == date_key),
                rowsPersisted=len(rows),
                anchorMethod="cross_day_current_position_reconstruction",
            )
            if callable(getattr(service.store, "put", None)):
                service.store.put(f"live_execution_truth:{date_key}", summary)
                if date_key == current_date:
                    service.store.put("live_execution_truth", summary)
            per_date.append(summary)

        _LAST_RESULT = {
            "ok": True,
            "running": False,
            "source": "BYBIT_DEMO_EXECUTION_LIST",
            "daysRequested": bounded_days,
            "dateFrom": dates[0],
            "dateTo": dates[-1],
            "rowsFetched": len(combined),
            "rowsPersisted": len(reconciled),
            "dates": per_date,
            "completedAt": now_ms,
            "idempotency": "execId",
        }
        return dict(_LAST_RESULT)
    except Exception as exc:
        _LAST_RESULT = {"ok": False, "running": False, "error": str(exc)}
        raise
    finally:
        _LOCK.release()


def start_once(core: Any, *, days: Any = DEFAULT_DAYS) -> None:
    """Run once in a daemon thread without delaying Cloud Run startup."""
    global _LAST_RESULT
    bounded_days = _bounded_days(days)
    if _LAST_RESULT.get("running") or _LAST_RESULT.get("ok"):
        return
    _LAST_RESULT = {"ok": False, "running": True, "daysRequested": bounded_days}

    def worker() -> None:
        try:
            run(core, days=bounded_days)
        except Exception as exc:  # surfaced through status and Cloud Run logs
            print(f"Historical execution backfill failed: {exc}", flush=True)

    threading.Thread(
        target=worker,
        name="historical-execution-backfill",
        daemon=True,
    ).start()


def status() -> dict[str, Any]:
    return dict(_LAST_RESULT)


def handle_post(handler: Any, core: Any, path: str, payload: Mapping[str, Any]) -> bool:
    if path != BACKFILL_PATH:
        return False
    try:
        result = run(core, days=payload.get("days", DEFAULT_DAYS))
    except Exception as exc:
        live_execution_ledger._error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
