from __future__ import annotations

import time
from typing import Any, Callable

Position = dict[str, Any]
Result = dict[str, Any]


def _summarize_positions(rows: list[Position]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for row in rows:
        try:
            size = abs(float(row.get("size") or 0))
        except (TypeError, ValueError):
            size = 0.0
        if size <= 0:
            continue
        summaries.append({
            "symbol": row.get("symbol"),
            "side": row.get("side"),
            "size": size,
        })
    return summaries


def execute_verified_kill_switch(
    *,
    get_open_positions: Callable[[], tuple[list[Position] | None, str]],
    cancel_all: Callable[[str], Result],
    close_symbol_positions: Callable[[str], Result],
    journal_add: Callable[[str, dict[str, Any]], None],
    max_verify_attempts: int = 3,
    verify_delay_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> Result:
    positions, message = get_open_positions()
    if positions is None:
        return {
            "retCode": -1,
            "retMsg": f"Failed to fetch positions: {message}",
            "verifiedFlat": False,
            "closedSymbols": [],
            "remainingPositions": [],
            "closeAttempts": 0,
            "openPositionsBefore": 0,
            "results": [],
        }

    active_positions = _summarize_positions(positions)
    if not active_positions:
        return {
            "retCode": 0,
            "retMsg": "Kill switch verified: account already flat.",
            "verifiedFlat": True,
            "closedSymbols": [],
            "remainingPositions": [],
            "closeAttempts": 0,
            "openPositionsBefore": 0,
            "results": [],
        }

    symbols = sorted({str(row["symbol"]) for row in active_positions if row.get("symbol")})
    results: list[dict[str, Any]] = []
    accepted_orders = 0

    for symbol in symbols:
        cancel_result = cancel_all(symbol)
        close_result = close_symbol_positions(symbol)
        orders = close_result.get("orders") or []
        order_results = []
        for order in orders:
            accepted = isinstance(order, dict) and order.get("retCode") == 0
            order_results.append({"accepted": accepted, "result": order})
            if accepted:
                accepted_orders += 1

        symbol_result = {
            "symbol": symbol,
            "cancelResult": cancel_result,
            "closeResult": close_result,
            "orderResults": order_results,
        }
        results.append(symbol_result)
        journal_add("kill_switch", symbol_result)

    remaining: list[dict[str, Any]] = []
    verify_error = ""
    attempts = max(1, int(max_verify_attempts))
    for attempt in range(attempts):
        verify_positions, verify_message = get_open_positions()
        if verify_positions is None:
            verify_error = verify_message or "Position verification failed"
        else:
            remaining = _summarize_positions(verify_positions)
            verify_error = ""
            if not remaining:
                break
        if attempt < attempts - 1:
            sleep(max(0.0, verify_delay_seconds))

    close_failures = [
        item for item in results
        if not item["closeResult"].get("ok")
        or not item["orderResults"]
        or any(not order["accepted"] for order in item["orderResults"])
    ]
    verified_flat = not verify_error and not remaining
    success = verified_flat and not close_failures

    if success:
        message = "Kill switch verified: all positions are flat."
    elif verify_error:
        message = f"Kill switch verification failed: {verify_error}"
    elif remaining:
        symbols_left = ", ".join(str(item.get("symbol")) for item in remaining if item.get("symbol"))
        message = f"Kill switch incomplete: positions remain open ({symbols_left or 'unknown symbols'})."
    else:
        message = "Kill switch incomplete: one or more close orders were rejected or missing."

    return {
        "retCode": 0 if success else -1,
        "retMsg": message,
        "verifiedFlat": verified_flat,
        "closedSymbols": symbols if success else [],
        "remainingPositions": remaining,
        "closeAttempts": accepted_orders,
        "openPositionsBefore": len(active_positions),
        "results": results,
    }
