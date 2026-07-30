"""Verified, idempotent management of open Bybit positions."""

from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .order_fill import verify_final_fill


_LOCK = threading.Lock()
_INFLIGHT: set[str] = set()
_FAILED_AT: dict[str, float] = {}
_PENDING_PARTIAL: dict[str, Any] | None = None


def _dec(value: Any) -> Decimal | None:
    try:
        value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _positive(value: Any) -> Decimal | None:
    value = _dec(value)
    return value if value is not None and value > 0 else None


def _number(value: Any, default: float, low: float, high: float | None = None) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if value != value or value in (float("inf"), float("-inf")):
        value = default
    value = max(low, value)
    return min(value, high) if high is not None else value


def position_key(position: dict[str, Any]) -> str:
    symbol = str(position.get("symbol") or "")
    side = str(position.get("side") or "")
    idx = str(
        position.get("positionIdx")
        if position.get("positionIdx") is not None
        else "0"
    )
    opened = str(position.get("openTime") or position.get("createdTime") or "")
    return f"{symbol}:{side}:{idx}:{opened or position.get('avgPrice') or 'unknown'}"


def reset_management_state() -> None:
    global _PENDING_PARTIAL
    with _LOCK:
        _INFLIGHT.clear()
        _FAILED_AT.clear()
        _PENDING_PARTIAL = None


def pending_partial_close() -> dict[str, Any] | None:
    with _LOCK:
        return dict(_PENDING_PARTIAL) if _PENDING_PARTIAL else None


def _set_pending(payload: dict[str, Any] | None) -> None:
    global _PENDING_PARTIAL
    with _LOCK:
        _PENDING_PARTIAL = dict(payload) if payload else None


def _claim(key: str, cooldown: float) -> tuple[bool, str]:
    now = time.monotonic()
    with _LOCK:
        if key in _INFLIGHT:
            return False, "Action already in progress."
        failed_at = _FAILED_AT.get(key)
        if failed_at is not None and now - failed_at < cooldown:
            return False, "Previous failure is inside the retry cooldown."
        _INFLIGHT.add(key)
    return True, "claimed"


def _release(key: str, failed: bool) -> None:
    with _LOCK:
        _INFLIGHT.discard(key)
        if failed:
            _FAILED_AT[key] = time.monotonic()
        else:
            _FAILED_AT.pop(key, None)


def _entries(engine: Any) -> list[dict[str, Any]]:
    entries = getattr(getattr(engine, "journal", None), "entries", [])
    return entries if isinstance(entries, list) else []


def _completed(
    engine: Any,
    event: str,
    key: str,
    legacy_key: str,
) -> tuple[bool, bool]:
    for entry in _entries(engine):
        if entry.get("event") != event:
            continue
        payload = entry.get("payload") or {}
        if payload.get("positionKey") not in {key, legacy_key}:
            continue
        if payload.get("verified") is True:
            return True, True
        result = payload.get("result") or {}
        try:
            if int(result.get("retCode")) == 0:
                return True, False
        except (TypeError, ValueError):
            pass
    return False, False


def _matching(
    rows: list[dict[str, Any]],
    position: dict[str, Any],
) -> dict[str, Any] | None:
    for row in rows:
        if (
            isinstance(row, dict)
            and str(row.get("symbol") or "") == str(position.get("symbol") or "")
            and str(row.get("side") or "") == str(position.get("side") or "")
        ):
            expected_idx = position.get("positionIdx")
            actual_idx = row.get("positionIdx")
            if expected_idx is None or actual_idx is None:
                return row
            try:
                if int(expected_idx) == int(actual_idx):
                    return row
            except (TypeError, ValueError):
                pass
    return None


def _positions(core: Any, symbol: str) -> list[dict[str, Any]] | None:
    try:
        rows, _ = core.get_symbol_open_positions(symbol)
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    return rows if all(isinstance(row, dict) for row in rows) else None


def _poll(
    core: Any,
    position: dict[str, Any],
    predicate: Callable[[dict[str, Any] | None], bool],
    *,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> tuple[bool, dict[str, Any] | None, int]:
    symbol = str(position.get("symbol") or "")
    current = None
    total = max(1, attempts)
    for attempt in range(1, total + 1):
        rows = _positions(core, symbol)
        if rows is not None:
            current = _matching(rows, position)
            if predicate(current):
                return True, current, attempt
        if attempt < total and delay > 0:
            sleeper(delay)
    return False, current, total


def _journal(engine: Any, event: str, action: dict[str, Any]) -> None:
    journal = getattr(engine, "journal", None)
    success = bool(action.get("verified"))
    if journal is not None and hasattr(journal, "add"):
        journal.add(event if success else f"{event}_failed", action)
    if hasattr(engine, "set_status"):
        engine.set_status("journal", "ok")


def _base_action(
    event: str,
    position: dict[str, Any],
    pnl: float,
    result: dict[str, Any],
    verification: dict[str, Any],
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": event,
        "positionKey": position_key(position),
        "symbol": position.get("symbol"),
        "side": position.get("side"),
        "pnlPct": round(pnl, 4),
        "verified": bool(verification.get("ok")),
        "status": "verified" if verification.get("ok") else "failed",
        "result": result,
        "verification": verification,
        **extra,
    }


def _partial(
    core: Any,
    engine: Any,
    position: dict[str, Any],
    pnl: float,
    close_pct: float,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    result = core.close_partial_position(position, close_pct)
    result = result if isinstance(result, dict) else {}
    if result.get("retCode") != 0:
        verification = {
            "ok": False,
            "state": "create_rejected",
            "reason": (
                result.get("retMsg")
                or result.get("reason")
                or "Partial close rejected."
            ),
        }
    else:
        fill = verify_final_fill(
            str(position.get("symbol") or ""),
            result,
            core.bybit_request,
            attempts=attempts,
            delay_seconds=delay,
            sleeper=sleeper,
        )
        initial = _positive(position.get("size"))
        executed = _positive(fill.get("cumExecQty"))
        if fill.get("accepted") and initial is not None and executed is not None:
            expected = max(Decimal("0"), initial - executed)
            tolerance = max(
                Decimal("0.00000001"),
                initial * Decimal("0.000001"),
            )
            ok, current, used = _poll(
                core,
                position,
                lambda row: (
                    (_positive(row.get("size")) if row else Decimal("0"))
                    or Decimal("0")
                )
                <= expected + tolerance,
                attempts=attempts,
                delay=delay,
                sleeper=sleeper,
            )
            verification = {
                "ok": ok,
                "state": "position_reduced" if ok else "position_sync_timeout",
                "reason": (
                    "Filled reduce-only quantity is reflected in the position."
                    if ok
                    else "Partial close filled but position reduction was not synchronized."
                ),
                "attempts": used,
                "fill": fill,
                "currentSize": current.get("size") if current else "0",
            }
            if not ok:
                _set_pending(
                    {"position": dict(position), "orderResult": dict(result)}
                )
        else:
            verification = {
                "ok": False,
                "state": fill.get("state"),
                "reason": fill.get("reason"),
                "fill": fill,
            }
            if fill.get("unresolved"):
                _set_pending(
                    {"position": dict(position), "orderResult": dict(result)}
                )
    action = _base_action(
        "partial_take_profit",
        position,
        pnl,
        result,
        verification,
        closePct=close_pct,
    )
    _journal(engine, "partial_take_profit", action)
    return action


def _breakeven(
    core: Any,
    engine: Any,
    position: dict[str, Any],
    pnl: float,
    target: Decimal,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    stop = core.format_price(position.get("symbol"), float(target))
    body = {
        "category": "linear",
        "symbol": position.get("symbol"),
        "tpslMode": "Full",
        "stopLoss": stop,
    }
    if position.get("positionIdx") is not None:
        body["positionIdx"] = int(position.get("positionIdx") or 0)
    result = core.bybit_request("POST", "/v5/position/trading-stop", body)
    result = result if isinstance(result, dict) else {}
    if result.get("retCode") == 0:
        side = str(position.get("side") or "")
        target_value = _positive(stop)
        ok, current, used = _poll(
            core,
            position,
            lambda row: (
                row is None
                or (
                    target_value is not None
                    and _positive(row.get("stopLoss")) is not None
                    and (
                        (
                            side == "Buy"
                            and _positive(row.get("stopLoss")) >= target_value
                        )
                        or (
                            side == "Sell"
                            and _positive(row.get("stopLoss")) <= target_value
                        )
                    )
                )
            ),
            attempts=attempts,
            delay=delay,
            sleeper=sleeper,
        )
        verification = {
            "ok": ok,
            "state": "stop_verified" if ok else "stop_sync_timeout",
            "reason": (
                "Exchange position confirms breakeven protection."
                if ok
                else "Exchange position did not confirm the breakeven stop."
            ),
            "attempts": used,
            "reported": current.get("stopLoss") if current else None,
        }
    else:
        verification = {
            "ok": False,
            "state": "request_rejected",
            "reason": result.get("retMsg") or "Breakeven update rejected.",
        }
    action = _base_action(
        "breakeven_stop",
        position,
        pnl,
        result,
        verification,
        stopLoss=stop,
    )
    _journal(engine, "breakeven_stop", action)
    return action


def _trailing(
    core: Any,
    engine: Any,
    position: dict[str, Any],
    pnl: float,
    distance_pct: float,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> dict[str, Any]:
    mark = _positive(position.get("markPrice"))
    expected = (
        _positive(
            core.format_price(
                position.get("symbol"),
                float(mark) * distance_pct / 100,
            )
        )
        if mark is not None
        else None
    )
    result = core.set_trailing_stop(position, distance_pct)
    result = result if isinstance(result, dict) else {}
    if result.get("retCode") == 0 and expected is not None:
        tolerance = max(
            Decimal("0.00000001"),
            expected * Decimal("0.001"),
        )
        ok, current, used = _poll(
            core,
            position,
            lambda row: (
                row is None
                or (
                    _positive(row.get("trailingStop")) is not None
                    and abs(_positive(row.get("trailingStop")) - expected)
                    <= tolerance
                )
            ),
            attempts=attempts,
            delay=delay,
            sleeper=sleeper,
        )
        verification = {
            "ok": ok,
            "state": "trailing_verified" if ok else "trailing_sync_timeout",
            "reason": (
                "Exchange position confirms the trailing stop."
                if ok
                else "Exchange position did not confirm the trailing stop."
            ),
            "attempts": used,
            "reported": current.get("trailingStop") if current else None,
        }
    else:
        verification = {
            "ok": False,
            "state": "request_rejected",
            "reason": result.get("retMsg") or "Trailing-stop update rejected.",
        }
    action = _base_action(
        "trailing_stop_enabled",
        position,
        pnl,
        result,
        verification,
        distancePct=distance_pct,
        expectedDistance=str(expected) if expected is not None else None,
    )
    _journal(engine, "trailing_stop_enabled", action)
    return action


def manage_positions(
    core: Any,
    state: dict[str, Any],
    *,
    attempts: int = 4,
    delay_seconds: float = 0.2,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    try:
        positions, message = core.get_open_positions()
    except Exception as exc:
        return {
            "ok": False,
            "actions": [],
            "failures": 1,
            "reason": f"Open-position fetch failed: {type(exc).__name__}",
        }
    if positions is None or not isinstance(positions, list):
        return {
            "ok": False,
            "actions": [],
            "failures": 1,
            "reason": str(message),
        }

    engine = core.get_bot_engine()
    cooldown = _number(
        state.get("positionManagementRetrySeconds"),
        60,
        1,
        3600,
    )
    partial_trigger = _number(state.get("partialTpTriggerPct"), 1.0, 0.1)
    partial_close = _number(state.get("partialTpClosePct"), 50, 1, 100)
    breakeven_trigger = _number(
        state.get("breakevenTriggerPct"),
        0.6,
        0.1,
    )
    trailing_trigger = _number(
        state.get("trailingStopTriggerPct"),
        0.8,
        0.1,
    )
    trailing_distance = _number(
        state.get("trailingStopDistancePct"),
        0.35,
        0.05,
    )

    actions: list[dict[str, Any]] = []
    failures = 0
    skipped = 0
    for position in positions:
        if not isinstance(position, dict):
            failures += 1
            continue
        avg = _positive(position.get("avgPrice"))
        mark = _positive(position.get("markPrice"))
        side = str(position.get("side") or "")
        symbol = str(position.get("symbol") or "")
        if avg is None or mark is None or side not in {"Buy", "Sell"} or not symbol:
            failures += 1
            continue

        pnl = (
            float(((mark - avg) / avg) * 100)
            if side == "Buy"
            else float(((avg - mark) / avg) * 100)
        )
        key = position_key(position)
        try:
            legacy = str(core.position_key(position))
        except Exception:
            legacy = key
        current_stop = _positive(position.get("stopLoss"))
        already_safe = current_stop is not None and (
            (side == "Buy" and current_stop >= avg)
            or (side == "Sell" and current_stop <= avg)
        )
        target = avg * (
            Decimal("1.0002") if side == "Buy" else Decimal("0.9998")
        )

        candidates = [
            (
                "partial_take_profit",
                state.get("partialTpEnabled", True) is not False
                and pnl >= partial_trigger,
                lambda: _partial(
                    core,
                    engine,
                    position,
                    pnl,
                    partial_close,
                    attempts,
                    delay_seconds,
                    sleeper,
                ),
            ),
            (
                "breakeven_stop",
                state.get("breakevenEnabled", True) is not False
                and pnl >= breakeven_trigger
                and not already_safe,
                lambda: _breakeven(
                    core,
                    engine,
                    position,
                    pnl,
                    target,
                    attempts,
                    delay_seconds,
                    sleeper,
                ),
            ),
            (
                "trailing_stop_enabled",
                state.get("trailingStopEnabled", True) is not False
                and pnl >= trailing_trigger,
                lambda: _trailing(
                    core,
                    engine,
                    position,
                    pnl,
                    trailing_distance,
                    attempts,
                    delay_seconds,
                    sleeper,
                ),
            ),
        ]
        for event, eligible, execute in candidates:
            if not eligible:
                continue
            done, verified = _completed(engine, event, key, legacy)
            if done:
                skipped += 1
                actions.append(
                    {
                        "type": event,
                        "positionKey": key,
                        "symbol": symbol,
                        "side": side,
                        "verified": verified,
                        "status": "skipped",
                        "reason": (
                            "Verified action already completed."
                            if verified
                            else "Legacy accepted action will not be repeated automatically."
                        ),
                    }
                )
                continue
            action_key = f"{event}:{key}"
            claimed, reason = _claim(action_key, cooldown)
            if not claimed:
                skipped += 1
                actions.append(
                    {
                        "type": event,
                        "positionKey": key,
                        "symbol": symbol,
                        "side": side,
                        "verified": False,
                        "status": "skipped",
                        "reason": reason,
                    }
                )
                continue
            try:
                action = execute()
            except Exception as exc:
                action = {
                    "type": event,
                    "positionKey": key,
                    "symbol": symbol,
                    "side": side,
                    "verified": False,
                    "status": "failed",
                    "result": {},
                    "verification": {
                        "ok": False,
                        "state": "exception",
                        "reason": (
                            f"Management action failed: {type(exc).__name__}"
                        ),
                    },
                }
                _journal(engine, event, action)
            failed = not bool(action.get("verified"))
            _release(action_key, failed)
            actions.append(action)
            failures += int(failed)

    if hasattr(engine, "set_status"):
        engine.set_status("tradeManagement", "error" if failures else "ok")
    return {
        "ok": failures == 0,
        "actions": actions,
        "failures": failures,
        "skipped": skipped,
        "reason": f"Position management: {failures} failed, {skipped} skipped.",
        "pendingPartialClose": pending_partial_close(),
    }


def entry_gate(core: Any) -> tuple[bool, str, dict[str, Any] | None]:
    pending = pending_partial_close()
    if not pending:
        return True, "No unresolved partial close.", None
    position = pending.get("position") or {}
    result = pending.get("orderResult") or {}
    fill = verify_final_fill(
        str(position.get("symbol") or ""),
        result,
        core.bybit_request,
        attempts=1,
        delay_seconds=0,
    )
    if fill.get("accepted"):
        initial = _positive(position.get("size"))
        executed = _positive(fill.get("cumExecQty"))
        if initial is not None and executed is not None:
            expected = max(Decimal("0"), initial - executed)
            rows = _positions(core, str(position.get("symbol") or ""))
            current = _matching(rows, position) if rows is not None else None
            size = _positive(current.get("size")) if current else Decimal("0")
            tolerance = max(
                Decimal("0.00000001"),
                initial * Decimal("0.000001"),
            )
            if size is not None and size <= expected + tolerance:
                _set_pending(None)
                return (
                    False,
                    "Partial close is now synchronized; this entry cycle remains blocked.",
                    {"fill": fill},
                )
        return (
            False,
            "Partial close filled but position synchronization is pending.",
            {"fill": fill},
        )
    if fill.get("terminal") and not fill.get("unresolved"):
        _set_pending(None)
        return True, "Partial close resolved without execution.", {"fill": fill}
    return (
        False,
        f"Partial close remains unresolved: {fill.get('reason', 'status unavailable')}",
        {"fill": fill},
    )
