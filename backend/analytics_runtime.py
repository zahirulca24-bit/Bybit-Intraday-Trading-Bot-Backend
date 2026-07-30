"""Read-only performance analytics for the canonical Bybit Demo runtime.

All metrics are derived from Bybit V5 closed-PnL records. No mock trades,
synthetic outcomes, or browser-provided values are accepted.
"""

from __future__ import annotations

import math
import statistics
import threading
import time
import urllib.parse
from collections import defaultdict
from typing import Any

_CACHE_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {"expiresAt": 0.0, "maxRows": 0, "snapshot": None}
_DEFAULT_MAX_ROWS = 200
_DEFAULT_LOOKBACK_DAYS = 7
_CACHE_SECONDS = 15


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _timestamp_ms(value: Any) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return 0
    return parsed * 1000 if 0 < parsed < 10_000_000_000 else max(0, parsed)


def _round(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)


def normalize_closed_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize one exchange closed-PnL row into the public analytics contract.

    Bybit's ``side`` is the closing order side. A Sell close therefore closes a
    long position, while a Buy close closes a short position. Both truths are
    retained so the API never labels the closing action as the position side.
    """
    pnl = _number(row.get("closedPnl"))
    side_raw = str(row.get("side") or "").strip().lower()
    closing_side = "BUY" if side_raw == "buy" else "SELL" if side_raw == "sell" else "UNKNOWN"
    position_side = "SHORT" if closing_side == "BUY" else "LONG" if closing_side == "SELL" else "UNKNOWN"
    closed_at = _timestamp_ms(row.get("updatedTime") or row.get("createdTime"))
    order_id = str(row.get("orderId") or row.get("orderLinkId") or "").strip()
    symbol = str(row.get("symbol") or "UNKNOWN").strip().upper()
    identity = order_id or f"{symbol}:{position_side}:{closed_at}:{pnl:.12f}"
    return {
        "id": identity,
        "orderId": order_id or None,
        "symbol": symbol,
        "side": position_side,
        "positionSide": position_side,
        "closingSide": closing_side,
        "closedPnl": pnl,
        "closedSize": abs(_number(row.get("closedSize") or row.get("qty"))),
        "avgEntryPrice": _number(row.get("avgEntryPrice") or row.get("orderPrice")),
        "avgExitPrice": _number(row.get("avgExitPrice")),
        "leverage": _number(row.get("leverage")),
        "closedAt": closed_at,
        "strategy": None,
        "strategyAttribution": "UNATTRIBUTED",
    }


def _fetch_closed_trades(core: Any, max_rows: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor = ""

    while len(rows) < max_rows:
        request_limit = min(100, max_rows - len(rows))
        params: dict[str, Any] = {"category": "linear", "limit": request_limit}
        if cursor:
            params["cursor"] = cursor
        payload = core.bybit_request("GET", "/v5/position/closed-pnl", params)
        if not isinstance(payload, dict) or int(payload.get("retCode", -1)) != 0:
            message = payload.get("retMsg") if isinstance(payload, dict) else "Invalid exchange response"
            raise RuntimeError(f"Bybit closed-PnL request failed: {message}")

        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        batch = result.get("list") if isinstance(result.get("list"), list) else []
        for raw in batch:
            if not isinstance(raw, dict):
                continue
            trade = normalize_closed_trade(raw)
            if trade["id"] in seen:
                continue
            seen.add(trade["id"])
            rows.append(trade)
            if len(rows) >= max_rows:
                break

        next_cursor = str(result.get("nextPageCursor") or "").strip()
        if not batch or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor

    rows.sort(key=lambda item: (int(item.get("closedAt") or 0), str(item.get("id") or "")))
    return rows


def _group_metrics(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "UNKNOWN")].append(row)

    output: list[dict[str, Any]] = []
    for label, items in groups.items():
        pnl_values = [_number(item.get("closedPnl")) for item in items]
        wins = sum(1 for pnl in pnl_values if pnl > 0)
        losses = sum(1 for pnl in pnl_values if pnl < 0)
        breakeven = len(items) - wins - losses
        gross_profit = sum(pnl for pnl in pnl_values if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0))
        output.append({
            "label": label,
            "totalTrades": len(items),
            "wins": wins,
            "losses": losses,
            "breakeven": breakeven,
            "winRatePct": _round((wins / len(items)) * 100 if items else 0.0, 2),
            "netPnl": _round(sum(pnl_values)),
            "averagePnl": _round(sum(pnl_values) / len(items) if items else 0.0),
            "grossProfit": _round(gross_profit),
            "grossLoss": _round(gross_loss),
            "profitFactor": _round(gross_profit / gross_loss) if gross_loss > 0 else None,
        })

    output.sort(key=lambda item: (float(item.get("netPnl") or 0), int(item.get("totalTrades") or 0)), reverse=True)
    return output


def _streaks(pnl_values: list[float]) -> tuple[int, int]:
    best_win = best_loss = current_win = current_loss = 0
    for pnl in pnl_values:
        if pnl > 0:
            current_win += 1
            current_loss = 0
            best_win = max(best_win, current_win)
        elif pnl < 0:
            current_loss += 1
            current_win = 0
            best_loss = max(best_loss, current_loss)
        else:
            current_win = current_loss = 0
    return best_win, best_loss


def build_analytics_snapshot(rows: list[dict[str, Any]], max_rows: int = _DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Build summary, breakdown, and drawdown payloads from normalized trades."""
    ordered = sorted(rows, key=lambda item: (int(item.get("closedAt") or 0), str(item.get("id") or "")))
    pnl_values = [_number(item.get("closedPnl")) for item in ordered]
    total = len(ordered)
    wins = sum(1 for pnl in pnl_values if pnl > 0)
    losses = sum(1 for pnl in pnl_values if pnl < 0)
    breakeven = total - wins - losses
    gross_profit = sum(pnl for pnl in pnl_values if pnl > 0)
    gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0))
    net_pnl = sum(pnl_values)
    avg_win = gross_profit / wins if wins else 0.0
    avg_loss = gross_loss / losses if losses else 0.0
    max_win_streak, max_loss_streak = _streaks(pnl_values)

    pnl_sharpe: float | None = None
    if total >= 2:
        deviation = statistics.stdev(pnl_values)
        if deviation > 0:
            pnl_sharpe = (statistics.mean(pnl_values) / deviation) * math.sqrt(total)

    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    curve: list[dict[str, Any]] = []
    for index, trade in enumerate(ordered, start=1):
        pnl = _number(trade.get("closedPnl"))
        cumulative += pnl
        peak = max(peak, cumulative)
        drawdown = max(0.0, peak - cumulative)
        max_drawdown = max(max_drawdown, drawdown)
        drawdown_pct = (drawdown / abs(peak)) * 100 if peak > 0 else None
        curve.append({
            "index": index,
            "time": int(trade.get("closedAt") or 0),
            "symbol": trade.get("symbol"),
            "side": trade.get("side"),
            "pnl": _round(pnl),
            "cumulativePnl": _round(cumulative),
            "peakPnl": _round(peak),
            "drawdown": _round(drawdown),
            "drawdownPct": _round(drawdown_pct, 2),
        })

    summary = {
        "totalTrades": total,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "winRatePct": _round((wins / total) * 100 if total else 0.0, 2),
        "netPnl": _round(net_pnl),
        "grossProfit": _round(gross_profit),
        "grossLoss": _round(gross_loss),
        "profitFactor": _round(gross_profit / gross_loss) if gross_loss > 0 else None,
        "averagePnl": _round(net_pnl / total if total else 0.0),
        "averageWin": _round(avg_win),
        "averageLoss": _round(avg_loss),
        "payoffRatio": _round(avg_win / avg_loss) if avg_loss > 0 else None,
        "expectancy": _round(net_pnl / total if total else 0.0),
        "pnlSharpe": _round(pnl_sharpe),
        "maxDrawdown": _round(max_drawdown),
        "currentDrawdown": _round(curve[-1]["drawdown"] if curve else 0.0),
        "bestTrade": _round(max(pnl_values) if pnl_values else 0.0),
        "worstTrade": _round(min(pnl_values) if pnl_values else 0.0),
        "maxConsecutiveWins": max_win_streak,
        "maxConsecutiveLosses": max_loss_streak,
        "periodStart": int(ordered[0].get("closedAt") or 0) if ordered else None,
        "periodEnd": int(ordered[-1].get("closedAt") or 0) if ordered else None,
    }

    generated_at = int(time.time() * 1000)
    window_ms = _DEFAULT_LOOKBACK_DAYS * 86_400_000
    metadata = {
        "source": "BYBIT_DEMO_CLOSED_PNL",
        "generatedAt": generated_at,
        "sampleSize": total,
        "sampleLimit": max_rows,
        "sampleLimited": total >= max_rows,
        "currency": "USDT",
        "lookbackDays": _DEFAULT_LOOKBACK_DAYS,
        "windowStart": generated_at - window_ms,
        "windowEnd": generated_at,
        "windowSource": "BYBIT_DEFAULT_7_DAY_WINDOW",
        "pnlSharpeMethod": "trade-level closed-PnL, zero benchmark, non-annualized",
        "strategyAttribution": "UNAVAILABLE_FOR_LEGACY_EXCHANGE_ROWS",
        "truthfulEmptyState": total == 0,
    }
    return {
        "summary": summary,
        "breakdown": {
            "bySymbol": _group_metrics(ordered, "symbol"),
            "bySide": _group_metrics(ordered, "side"),
            "unattributedTrades": total,
        },
        "drawdown": {"curve": curve, "maxDrawdown": _round(max_drawdown)},
        "recentTrades": list(reversed(ordered[-20:])),
        "metadata": metadata,
    }


def analytics_snapshot(core: Any, max_rows: int = _DEFAULT_MAX_ROWS, force: bool = False) -> dict[str, Any]:
    max_rows = max(1, min(500, int(max_rows)))
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get("snapshot")
        if not force and cached is not None and float(_CACHE.get("expiresAt") or 0) > now and int(_CACHE.get("maxRows") or 0) >= max_rows:
            return cached

    rows = _fetch_closed_trades(core, max_rows)
    snapshot = build_analytics_snapshot(rows, max_rows=max_rows)
    with _CACHE_LOCK:
        _CACHE.update({"expiresAt": now + _CACHE_SECONDS, "maxRows": max_rows, "snapshot": snapshot})
    return snapshot


def handle_get(handler: Any, core: Any, path: str) -> bool:
    """Serve one authenticated analytics route. Returns True when handled."""
    clean_path = str(path or "").split("?", 1)[0]
    if clean_path not in {
        "/api/analytics/summary",
        "/api/analytics/winrate-breakdown",
        "/api/analytics/drawdown-curve",
    }:
        return False

    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    try:
        max_rows = max(1, min(500, int(query.get("limit", _DEFAULT_MAX_ROWS))))
        force = str(query.get("force", "0")).lower() in {"1", "true", "yes"}
        snapshot = analytics_snapshot(core, max_rows=max_rows, force=force)
    except (TypeError, ValueError):
        core.json_response(handler, 400, {"ok": False, "error": "limit must be an integer between 1 and 500"})
        return True
    except Exception as exc:
        core.json_response(handler, 502, {"ok": False, "error": str(exc), "source": "BYBIT_DEMO_CLOSED_PNL"})
        return True

    if clean_path.endswith("/summary"):
        payload = {
            "ok": True,
            "summary": snapshot["summary"],
            "recentTrades": snapshot["recentTrades"],
            "metadata": snapshot["metadata"],
        }
    elif clean_path.endswith("/winrate-breakdown"):
        payload = {"ok": True, **snapshot["breakdown"], "metadata": snapshot["metadata"]}
    else:
        payload = {"ok": True, **snapshot["drawdown"], "metadata": snapshot["metadata"]}
    core.json_response(handler, 200, payload)
    return True
