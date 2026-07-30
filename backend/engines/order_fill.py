"""Verify that an acknowledged Bybit entry order reached a final full fill."""

from __future__ import annotations

import threading
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


Requester = Callable[[str, str, dict[str, Any]], dict[str, Any]]
Sleeper = Callable[[float], None]

FINAL_FILL_BLOCK_CODE = -1007

_FILLED = {"filled"}
_CANCELLED = {"cancelled", "canceled", "deactivated", "expired"}
_REJECTED = {"rejected"}
_PARTIAL_TERMINAL = {"partiallyfilledcanceled", "partiallyfilledcancelled"}
_PARTIAL_PENDING = {"partiallyfilled"}
_PENDING = {"new", "untriggered", "triggered", "active", "created", "pending"}

_PENDING_LOCK = threading.Lock()
_PENDING_ENTRY: dict[str, Any] | None = None


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive(value: Any) -> bool:
    parsed = _decimal(value)
    return parsed is not None and parsed > 0


def _normalized_status(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _identifiers(order_result: Any) -> tuple[str, str]:
    result = order_result.get("result") if isinstance(order_result, dict) else None
    if not isinstance(result, dict):
        return "", ""
    return str(result.get("orderId") or ""), str(result.get("orderLinkId") or "")


def _status_row(payload: Any, source: str) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return None, f"{source} returned an invalid response"
    if payload.get("retCode") != 0:
        return None, str(payload.get("retMsg") or f"{source} query failed")
    result = payload.get("result")
    if not isinstance(result, dict):
        return None, f"{source} result payload is invalid"
    rows = result.get("list")
    if not isinstance(rows, list):
        return None, f"{source} order list is invalid"
    if not rows:
        return None, f"{source} has no matching order"
    row = rows[0]
    if not isinstance(row, dict):
        return None, f"{source} order row is invalid"
    return {
        "source": source,
        "orderId": str(row.get("orderId") or ""),
        "orderLinkId": str(row.get("orderLinkId") or ""),
        "orderStatus": str(row.get("orderStatus") or "Unknown"),
        "cumExecQty": row.get("cumExecQty"),
        "avgPrice": row.get("avgPrice"),
    }, "OK"


def _classified(record: dict[str, Any], attempt: int) -> dict[str, Any]:
    status = _normalized_status(record.get("orderStatus"))
    executed = _positive(record.get("cumExecQty"))
    base = {**record, "attempts": attempt, "accepted": False, "finalFilled": False}

    if status in _FILLED and executed:
        return {
            **base,
            "ok": True,
            "accepted": True,
            "finalFilled": True,
            "state": "filled",
            "terminal": True,
            "unresolved": False,
            "reason": "Order is Filled with positive executed quantity.",
        }
    if status in _FILLED:
        return {
            **base,
            "ok": False,
            "state": "invalid_fill",
            "terminal": True,
            "unresolved": True,
            "reason": "Bybit reported Filled without a positive executed quantity.",
        }
    if status in _PARTIAL_TERMINAL or (
        executed and status in (_CANCELLED | _REJECTED)
    ):
        return {
            **base,
            "ok": False,
            "state": "partial",
            "terminal": True,
            "unresolved": True,
            "reason": "Order ended with executed quantity below a verified full fill; operator review is required.",
        }
    if status in _CANCELLED:
        return {
            **base,
            "ok": False,
            "state": "cancelled",
            "terminal": True,
            "unresolved": False,
            "reason": f"Order reached terminal status {record.get('orderStatus') or 'Cancelled'}.",
        }
    if status in _REJECTED:
        return {
            **base,
            "ok": False,
            "state": "rejected",
            "terminal": True,
            "unresolved": False,
            "reason": "Order was rejected by Bybit.",
        }
    if status in _PARTIAL_PENDING or executed:
        return {
            **base,
            "ok": False,
            "state": "partial",
            "terminal": False,
            "unresolved": True,
            "reason": "Order is only partially filled.",
        }
    return {
        **base,
        "ok": False,
        "state": "pending" if status in _PENDING else "unknown",
        "terminal": False,
        "unresolved": True,
        "reason": f"Order has not reached a verified full fill ({record.get('orderStatus') or 'Unknown'}).",
    }


def verify_final_fill(
    symbol: str,
    order_result: Any,
    requester: Requester,
    *,
    attempts: int = 6,
    delay_seconds: float = 0.25,
    sleeper: Sleeper = time.sleep,
) -> dict[str, Any]:
    """Poll realtime and history until the order is fully filled or terminal."""
    if not isinstance(order_result, dict):
        return {
            "ok": False,
            "accepted": False,
            "finalFilled": False,
            "state": "invalid_create_response",
            "terminal": True,
            "unresolved": True,
            "attempts": 0,
            "reason": "Order-create response is invalid.",
        }
    if order_result.get("retCode") != 0:
        return {
            "ok": False,
            "accepted": False,
            "finalFilled": False,
            "state": "create_rejected",
            "terminal": True,
            "unresolved": False,
            "attempts": 0,
            "reason": str(order_result.get("retMsg") or "Order create was rejected."),
        }

    order_id, order_link_id = _identifiers(order_result)
    if not order_id and not order_link_id:
        return {
            "ok": False,
            "accepted": False,
            "finalFilled": False,
            "state": "verification_error",
            "terminal": False,
            "unresolved": True,
            "attempts": 0,
            "reason": "Order acknowledgement did not include an order identifier.",
        }

    params: dict[str, Any] = {"category": "linear", "symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    else:
        params["orderLinkId"] = order_link_id

    last: dict[str, Any] | None = None
    errors: list[str] = []
    total_attempts = max(1, int(attempts))

    for attempt in range(1, total_attempts + 1):
        candidates: list[dict[str, Any]] = []
        for path, source in (
            ("/v5/order/realtime", "realtime"),
            ("/v5/order/history", "history"),
        ):
            try:
                payload = requester("GET", path, dict(params))
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}")
                continue
            row, message = _status_row(payload, source)
            if row is None:
                errors.append(message)
                continue
            candidates.append(_classified(row, attempt))

        for candidate in candidates:
            if candidate.get("accepted"):
                return candidate
        for candidate in candidates:
            if candidate.get("terminal"):
                return candidate
        if candidates:
            partials = [item for item in candidates if item.get("state") == "partial"]
            last = partials[0] if partials else candidates[0]

        if attempt < total_attempts and delay_seconds > 0:
            sleeper(delay_seconds)

    if last is not None:
        state = "partial" if last.get("state") == "partial" else "timeout"
        return {
            **last,
            "state": state,
            "terminal": False,
            "unresolved": True,
            "reason": "Final full fill was not confirmed before the verification deadline.",
        }
    return {
        "ok": False,
        "accepted": False,
        "finalFilled": False,
        "state": "verification_error",
        "terminal": False,
        "unresolved": True,
        "attempts": total_attempts,
        "orderId": order_id,
        "orderLinkId": order_link_id,
        "reason": "; ".join(errors[-4:]) or "Order status verification failed.",
    }


def _same_pending(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_id, left_link = _identifiers(left)
    right_id, right_link = _identifiers(right)
    return bool((left_id and left_id == right_id) or (left_link and left_link == right_link))


def clear_pending_entry() -> None:
    global _PENDING_ENTRY
    with _PENDING_LOCK:
        _PENDING_ENTRY = None


def get_pending_entry() -> dict[str, Any] | None:
    with _PENDING_LOCK:
        return dict(_PENDING_ENTRY) if _PENDING_ENTRY else None


def _register_pending(symbol: str, order_result: dict[str, Any], verification: dict[str, Any]) -> None:
    global _PENDING_ENTRY
    with _PENDING_LOCK:
        _PENDING_ENTRY = {
            "symbol": symbol,
            "orderResult": dict(order_result),
            "verification": dict(verification),
        }


def _clear_if_matching(order_result: dict[str, Any]) -> None:
    global _PENDING_ENTRY
    with _PENDING_LOCK:
        if _PENDING_ENTRY and _same_pending(_PENDING_ENTRY.get("orderResult") or {}, order_result):
            _PENDING_ENTRY = None


def finalize_entry_order(
    symbol: str,
    order_result: Any,
    requester: Requester,
    *,
    attempts: int = 6,
    delay_seconds: float = 0.25,
    sleeper: Sleeper = time.sleep,
) -> dict[str, Any]:
    """Return retCode 0 only when Bybit confirms a complete positive fill."""
    source = dict(order_result) if isinstance(order_result, dict) else {}
    verification = verify_final_fill(
        symbol,
        order_result,
        requester,
        attempts=attempts,
        delay_seconds=delay_seconds,
        sleeper=sleeper,
    )
    final = {
        **source,
        "exchangeRetCode": source.get("retCode"),
        "exchangeRetMsg": source.get("retMsg"),
        "fillVerification": verification,
        "finalFilled": bool(verification.get("accepted")),
        "accepted": bool(verification.get("accepted")),
    }

    if verification.get("accepted"):
        _clear_if_matching(source)
        final["retCode"] = 0
        final["retMsg"] = "OK: order fully filled"
        return final

    if source.get("retCode") == 0:
        final["retCode"] = FINAL_FILL_BLOCK_CODE
        final["retMsg"] = f"Order not accepted as filled: {verification.get('reason', 'Final fill unavailable')}"
    elif "retCode" not in final:
        final["retCode"] = FINAL_FILL_BLOCK_CODE
        final["retMsg"] = str(verification.get("reason") or "Order create response invalid")

    final["requiresOperatorReview"] = bool(verification.get("unresolved"))
    if verification.get("unresolved") and source.get("retCode") == 0:
        _register_pending(symbol, source, verification)
    return final


def pending_entry_gate(requester: Requester) -> tuple[bool, str, dict[str, Any] | None]:
    """Block all new entries while a prior acknowledged order remains unresolved."""
    pending = get_pending_entry()
    if not pending:
        return True, "No unresolved entry order.", None

    order_result = pending.get("orderResult") or {}
    symbol = str(pending.get("symbol") or "")
    verification = verify_final_fill(
        symbol,
        order_result,
        requester,
        attempts=1,
        delay_seconds=0,
    )
    if verification.get("accepted"):
        _clear_if_matching(order_result)
        return (
            False,
            "Previous entry is now filled; this cycle is blocked until position synchronization completes.",
            verification,
        )
    if verification.get("terminal") and not verification.get("unresolved"):
        _clear_if_matching(order_result)
        return True, "Previous entry resolved without a fill.", verification

    _register_pending(symbol, order_result, verification)
    return (
        False,
        f"Previous entry fill remains unresolved: {verification.get('reason', 'status unavailable')}",
        verification,
    )
