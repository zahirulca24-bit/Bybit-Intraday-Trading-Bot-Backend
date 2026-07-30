"""Normalize all non-zero Bybit USDT perpetual positions across cursor pages."""

from __future__ import annotations

from typing import Any, Callable

Requester = Callable[[str, str, dict[str, Any]], dict[str, Any]]


def _numeric_size(value: Any) -> float:
    try:
        return abs(float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _numeric_pnl(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _failure(message: str, ret_code: Any = -1) -> dict[str, Any]:
    return {
        "retCode": ret_code,
        "retMsg": message,
        "result": {"list": [], "count": 0},
    }


def collect_open_positions(requester: Requester, max_pages: int = 10) -> dict[str, Any]:
    """Fetch every cursor page and return a stable frontend-compatible payload.

    Any request exception, malformed response, exchange error, or incomplete
    pagination returns an error payload so callers can fail closed.
    """
    cursor = ""
    positions: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    completed = False

    for _ in range(max(1, max_pages)):
        params: dict[str, Any] = {
            "category": "linear",
            "settleCoin": "USDT",
            "limit": "200",
        }
        if cursor:
            params["cursor"] = cursor

        try:
            payload = requester("GET", "/v5/position/list", params)
        except Exception as exc:  # requester can wrap network/timeout errors
            return _failure(f"Open position synchronization failed: {type(exc).__name__}")

        if not isinstance(payload, dict):
            return _failure("Open position synchronization failed: invalid exchange response")

        if payload.get("retCode") != 0:
            return _failure(
                str(payload.get("retMsg") or "Open position synchronization failed"),
                payload.get("retCode", -1),
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            return _failure("Open position synchronization failed: invalid result payload")

        raw_rows = result.get("list")
        if not isinstance(raw_rows, list):
            return _failure("Open position synchronization failed: invalid position list")

        for source in raw_rows:
            if not isinstance(source, dict):
                return _failure("Open position synchronization failed: invalid position row")
            if _numeric_size(source.get("size")) <= 0:
                continue
            row = dict(source)
            key = (
                str(row.get("symbol") or ""),
                str(row.get("positionIdx") or "0"),
                str(row.get("side") or ""),
            )
            if key in seen:
                continue
            seen.add(key)
            positions.append(row)

        next_cursor = str(result.get("nextPageCursor") or "")
        if not next_cursor:
            completed = True
            break
        if next_cursor == cursor:
            return _failure("Open position synchronization failed: repeated pagination cursor")
        cursor = next_cursor

    if not completed:
        return _failure("Open position synchronization failed: pagination limit reached before completion")

    positions.sort(key=lambda row: (str(row.get("symbol") or ""), str(row.get("positionIdx") or "0")))
    total_pnl = sum(_numeric_pnl(row.get("unrealisedPnl")) for row in positions)
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {
            "list": positions,
            "count": len(positions),
            "totalUnrealisedPnl": total_pnl,
            "nextPageCursor": "",
        },
    }
