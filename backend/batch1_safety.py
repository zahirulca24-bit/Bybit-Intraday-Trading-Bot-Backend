"""Batch 1 execution-safety helpers for the canonical guarded runtime."""

from __future__ import annotations

import re
from typing import Any

# Base assets such as BTC and ETH are valid, so allow 2-16 alphanumeric
# characters before the USDT quote suffix while rejecting an empty base.
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,16}USDT$")
RISK_PER_TRADE_PCT = 2.0

LIMITS = {
    "maxAllocationUsdt": (1.0, 1000.0),
    "riskPerTradePct": (RISK_PER_TRADE_PCT, RISK_PER_TRADE_PCT),
    "maxOpenPositions": (1, 5),
    "dailyLossCapUsdt": (0.0, 500.0),
    "stopLossPct": (0.1, 10.0),
    "takeProfitPct": (0.1, 20.0),
    "breakevenTriggerPct": (0.1, 5.0),
    "partialTpTriggerPct": (0.1, 10.0),
    "partialTpClosePct": (1.0, 100.0),
    "trailingStopTriggerPct": (0.1, 10.0),
    "trailingStopDistancePct": (0.05, 5.0),
    "cooldownSeconds": (60, 86400),
}


def _number(payload: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(payload.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be numeric") from exc


def _integer(payload: dict[str, Any], key: str, default: int) -> int:
    value = _number(payload, key, float(default))
    if not value.is_integer():
        raise ValueError(f"{key} must be an integer")
    return int(value)


def _bounded(name: str, value: float | int) -> float | int:
    minimum, maximum = LIMITS[name]
    if value < minimum or value > maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must be a valid USDT perpetual symbol")
    return symbol


def validate_start_payload(payload: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized server-bounded configuration or raise ValueError."""
    symbol = normalize_symbol(payload.get("symbol") or defaults.get("symbol") or "BTCUSDT")
    interval = str(payload.get("interval", defaults.get("interval", "5")))
    if interval not in {"1", "3", "5", "15", "30", "60", "120", "240"}:
        raise ValueError("interval is not supported")

    requested_risk = _number(payload, "riskPerTradePct", RISK_PER_TRADE_PCT)
    if requested_risk != RISK_PER_TRADE_PCT:
        raise ValueError("riskPerTradePct is locked at 2%")

    config = {
        "symbol": symbol,
        "interval": interval,
        "qty": str(payload.get("qty", defaults.get("qty", "0.001"))),
        "maxAllocationUsdt": _bounded("maxAllocationUsdt", _number(payload, "maxAllocationUsdt", float(defaults.get("maxAllocationUsdt", 250)))),
        "riskPerTradePct": RISK_PER_TRADE_PCT,
        "maxOpenPositions": _bounded("maxOpenPositions", _integer(payload, "maxOpenPositions", int(defaults.get("maxOpenPositions", 1)))),
        "dailyLossCapUsdt": _bounded("dailyLossCapUsdt", _number(payload, "dailyLossCapUsdt", float(defaults.get("dailyLossCapUsdt", 25)))),
        # Daily trade count is intentionally unlimited. Persist None so any
        # legacy value such as 6 cannot survive a new start configuration.
        "maxTradesPerDay": None,
        "stopLossPct": _bounded("stopLossPct", _number(payload, "stopLossPct", float(defaults.get("stopLossPct", 0.8)))),
        "takeProfitPct": _bounded("takeProfitPct", _number(payload, "takeProfitPct", float(defaults.get("takeProfitPct", 1.6)))),
        "breakevenTriggerPct": _bounded("breakevenTriggerPct", _number(payload, "breakevenTriggerPct", float(defaults.get("breakevenTriggerPct", 0.6)))),
        "partialTpTriggerPct": _bounded("partialTpTriggerPct", _number(payload, "partialTpTriggerPct", float(defaults.get("partialTpTriggerPct", 1.4)))),
        "partialTpClosePct": _bounded("partialTpClosePct", _number(payload, "partialTpClosePct", float(defaults.get("partialTpClosePct", 40)))),
        "trailingStopTriggerPct": _bounded("trailingStopTriggerPct", _number(payload, "trailingStopTriggerPct", float(defaults.get("trailingStopTriggerPct", 1.8)))),
        "trailingStopDistancePct": _bounded("trailingStopDistancePct", _number(payload, "trailingStopDistancePct", float(defaults.get("trailingStopDistancePct", 0.45)))),
        "cooldownSeconds": _bounded("cooldownSeconds", _integer(payload, "cooldownSeconds", int(defaults.get("cooldownSeconds", 180)))),
        "breakevenEnabled": payload.get("breakevenEnabled", defaults.get("breakevenEnabled", True)) is not False,
        "partialTpEnabled": payload.get("partialTpEnabled", defaults.get("partialTpEnabled", True)) is not False,
        "trailingStopEnabled": payload.get("trailingStopEnabled", defaults.get("trailingStopEnabled", False)) is not False,
        "mode": str(payload.get("mode", defaults.get("mode", "conservative"))).lower(),
    }
    if config["mode"] not in {"conservative", "balanced", "aggressive"}:
        raise ValueError("mode is not supported")
    return config


def fail_closed_daily_risk(core: Any, state: dict[str, Any]) -> tuple[bool, str]:
    """Evaluate monetary daily risk only; trade count never stops the bot."""
    date_key = core.get_current_trading_date_key()
    closed_pnl, message = core.get_daily_closed_pnl(date_key)
    if closed_pnl is None:
        return True, f"Daily risk unavailable; execution blocked: {message}"

    cap = float(state.get("dailyLossCapUsdt") or 0)
    loss_used = abs(min(0.0, float(closed_pnl)))
    if cap > 0 and loss_used >= cap:
        return True, f"Daily loss cap reached (${loss_used:.2f}/${cap:.2f})"

    return False, "Daily risk OK; trade count unlimited"
