"""Step 3 strategy upgrade: market-regime filtering and trade-quality analytics."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

try:
    from .engines import bot_engine
    from .engines.indicators import ema
except ImportError:  # pragma: no cover
    from engines import bot_engine
    from engines.indicators import ema

_INSTALLED_ATTR = "_strategy_step3_upgrade_installed"
_TREND_ENGINES = {"Trend Follow", "S/R Breakout", "ORB", "VWAP Bounce"}
_REVERSAL_ENGINES = {"RSI Divergence", "Liquidity Sweep"}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _atr(candles: list[dict[str, Any]], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    values: list[float] = []
    for index in range(1, len(candles)):
        row = candles[index]
        previous = candles[index - 1]
        high = _number(row.get("high"))
        low = _number(row.get("low"))
        previous_close = _number(previous.get("close"))
        values.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(values[-period:]) / period if len(values) >= period else 0.0


def detect_regime(tf1h: list[dict[str, Any]]) -> dict[str, Any]:
    if len(tf1h) < 55:
        return {"regime": "UNKNOWN", "direction": "WAIT", "confidence": 0, "reason": "Insufficient 1H history"}

    closes = [_number(row.get("close")) for row in tf1h]
    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)
    if not ema20 or not ema50 or closes[-1] <= 0:
        return {"regime": "UNKNOWN", "direction": "WAIT", "confidence": 0, "reason": "EMA regime unavailable"}

    separation_pct = abs(ema20[-1] - ema50[-1]) / closes[-1] * 100
    slope_pct = ((ema20[-1] - ema20[-4]) / ema20[-4]) * 100 if len(ema20) >= 4 and ema20[-4] else 0.0
    atr_value = _atr(tf1h, 14)
    atr_pct = atr_value / closes[-1] * 100 if closes[-1] else 0.0

    if separation_pct >= 0.18 and abs(slope_pct) >= 0.08:
        direction = "Buy" if ema20[-1] > ema50[-1] else "Sell"
        confidence = min(100, int(55 + separation_pct * 80 + abs(slope_pct) * 100))
        return {
            "regime": "TRENDING",
            "direction": direction,
            "confidence": confidence,
            "emaSeparationPct": round(separation_pct, 4),
            "ema20SlopePct": round(slope_pct, 4),
            "atrPct": round(atr_pct, 4),
            "reason": f"Strong {direction} trend with EMA separation and slope",
        }

    if atr_pct >= 2.5:
        return {
            "regime": "HIGH_VOLATILITY",
            "direction": "WAIT",
            "confidence": min(100, int(50 + atr_pct * 10)),
            "emaSeparationPct": round(separation_pct, 4),
            "ema20SlopePct": round(slope_pct, 4),
            "atrPct": round(atr_pct, 4),
            "reason": "1H ATR volatility is elevated",
        }

    return {
        "regime": "RANGING",
        "direction": "WAIT",
        "confidence": max(40, min(90, int(80 - separation_pct * 100))),
        "emaSeparationPct": round(separation_pct, 4),
        "ema20SlopePct": round(slope_pct, 4),
        "atrPct": round(atr_pct, 4),
        "reason": "EMA separation and slope do not confirm a trend",
    }


def filter_votes(votes: list[dict[str, Any]], regime: dict[str, Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    state = regime.get("regime")
    direction = regime.get("direction")
    for raw in votes:
        vote = dict(raw)
        signal = vote.get("signal")
        engine = str(vote.get("engine") or "")
        blocked_reason = None
        if signal in {"Buy", "Sell"}:
            if state == "TRENDING" and signal != direction:
                blocked_reason = f"{engine} blocked by opposing {direction} trending regime"
            elif state == "RANGING" and engine in _TREND_ENGINES:
                blocked_reason = f"{engine} blocked because the 1H market is ranging"
            elif state == "HIGH_VOLATILITY" and engine in _REVERSAL_ENGINES:
                blocked_reason = f"{engine} blocked during high-volatility regime"
        if blocked_reason:
            vote["signal"] = "WAIT"
            vote["strength"] = 0
            vote["reason"] = blocked_reason
        vote["marketRegime"] = regime
        output.append(vote)
    return output


def _group_quality(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "UNATTRIBUTED")].append(row)
    result: list[dict[str, Any]] = []
    for label, items in groups.items():
        pnls = [_number(item.get("closedPnl")) for item in items]
        r_values = [
            _number(item.get("realizedR"))
            for item in items
            if item.get("realizedR") is not None and math.isfinite(_number(item.get("realizedR")))
        ]
        wins = sum(1 for pnl in pnls if pnl > 0)
        gross_profit = sum(pnl for pnl in pnls if pnl > 0)
        gross_loss = abs(sum(pnl for pnl in pnls if pnl < 0))
        result.append({
            "label": label,
            "totalTrades": len(items),
            "wins": wins,
            "losses": sum(1 for pnl in pnls if pnl < 0),
            "winRatePct": round((wins / len(items)) * 100, 2) if items else 0.0,
            "netPnl": round(sum(pnls), 6),
            "expectancyPnl": round(sum(pnls) / len(items), 6) if items else 0.0,
            "averageR": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "profitFactor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else None,
        })
    result.sort(key=lambda row: (row["netPnl"], row["totalTrades"]), reverse=True)
    return result


def trade_quality_snapshot(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attributed = [row for row in rows if any(row.get(key) for key in ("strategy", "grade", "session", "marketRegime"))]
    return {
        "sampleSize": len(rows),
        "attributedTrades": len(attributed),
        "unattributedTrades": len(rows) - len(attributed),
        "byStrategy": _group_quality(attributed, "strategy"),
        "byGrade": _group_quality(attributed, "grade"),
        "bySession": _group_quality(attributed, "session"),
        "byRegime": _group_quality(attributed, "marketRegime"),
        "truthfulEmptyState": len(attributed) == 0,
        "note": "Legacy Bybit closed-PnL rows remain unattributed until order metadata is persisted and reconciled.",
    }


def install(core: Any, analytics_runtime: Any) -> None:
    if getattr(core, _INSTALLED_ATTR, False):
        return

    original_strategies = bot_engine.BotEngineV2.strategies
    original_builder = analytics_runtime.build_analytics_snapshot

    def upgraded_strategies(self: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        votes = [dict(row) for row in original_strategies(self, snapshot)]
        regime = detect_regime(snapshot["timeframes"]["1H"])
        return filter_votes(votes, regime)

    def upgraded_builder(rows: list[dict[str, Any]], max_rows: int = 200) -> dict[str, Any]:
        payload = dict(original_builder(rows, max_rows=max_rows))
        payload["tradeQuality"] = trade_quality_snapshot(rows)
        return payload

    bot_engine.BotEngineV2.strategies = upgraded_strategies
    analytics_runtime.build_analytics_snapshot = upgraded_builder
    setattr(core, _INSTALLED_ATTR, True)


def status(core: Any) -> dict[str, Any]:
    return {
        "installed": bool(getattr(core, _INSTALLED_ATTR, False)),
        "features": ["MARKET_REGIME_FILTER", "TRADE_QUALITY_ANALYTICS"],
        "regimes": ["TRENDING", "RANGING", "HIGH_VOLATILITY", "UNKNOWN"],
    }
