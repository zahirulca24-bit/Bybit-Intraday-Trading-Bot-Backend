"""Step 2 strategy upgrade: ATR-based SL/TP and deterministic signal grading."""

from __future__ import annotations

import os
from typing import Any, Callable

_INSTALLED_ATTR = "_strategy_step2_upgrade_installed"


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, float]:
    return {
        "atrPeriod": _number("STEP2_ATR_PERIOD", 14, 5, 50),
        "atrStopMultiplier": _number("STEP2_ATR_STOP_MULTIPLIER", 1.5, 0.5, 5.0),
        "minimumRiskReward": _number("STEP2_MIN_RR", 2.0, 1.5, 5.0),
        "maximumStopPct": _number("STEP2_MAX_STOP_PCT", 2.5, 0.25, 8.0),
        "aPlusStrength": _number("STEP2_GRADE_A_PLUS_STRENGTH", 4.5, 0.0, 5.0),
        "aStrength": _number("STEP2_GRADE_A_STRENGTH", 3.8, 0.0, 5.0),
        "bPlusStrength": _number("STEP2_GRADE_B_PLUS_STRENGTH", 3.2, 0.0, 5.0),
    }


def true_range(current: dict[str, Any], previous_close: float) -> float:
    high = float(current["high"])
    low = float(current["low"])
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def atr(candles: list[dict[str, Any]], period: int = 14) -> float | None:
    if len(candles) < period + 1:
        return None
    values = [
        true_range(candles[index], float(candles[index - 1]["close"]))
        for index in range(len(candles) - period, len(candles))
    ]
    return sum(values) / len(values) if values else None


def dynamic_price_plan(
    candles: list[dict[str, Any]],
    side: str,
    minimum_rr: float,
    lookback: int,
) -> tuple[dict[str, float] | None, str]:
    cfg = settings()
    period = int(cfg["atrPeriod"])
    current_atr = atr(candles, period)
    if current_atr is None or current_atr <= 0:
        return None, "ATR is unavailable or invalid"

    entry = float(candles[-1]["close"])
    if entry <= 0:
        return None, "Invalid entry reference"

    stop_distance = current_atr * float(cfg["atrStopMultiplier"])
    stop_pct = stop_distance / entry * 100
    if stop_pct <= 0 or stop_pct > float(cfg["maximumStopPct"]):
        return None, f"ATR stop distance {stop_pct:.4f}% is outside configured limits"

    rr = max(float(minimum_rr), float(cfg["minimumRiskReward"]))
    reward_distance = stop_distance * rr
    if side == "Buy":
        stop = entry - stop_distance
        take = entry + reward_distance
    elif side == "Sell":
        stop = entry + stop_distance
        take = entry - reward_distance
    else:
        return None, "Candidate side must be Buy or Sell"

    if stop <= 0 or take <= 0:
        return None, "ATR stop or target is invalid"

    return {
        "entryReference": round(entry, 12),
        "stopLoss": round(stop, 12),
        "takeProfitReference": round(take, 12),
        "riskReward": round(rr, 4),
        "atr15m": round(current_atr, 12),
        "atrStopMultiplier": round(float(cfg["atrStopMultiplier"]), 4),
        "stopDistancePct": round(stop_pct, 6),
        "pricePlanSource": "ATR_15M",
    }, "ATR-based stop and target confirmed"


def grade_for_strength(strength: Any) -> dict[str, Any]:
    cfg = settings()
    try:
        value = max(0.0, min(5.0, abs(float(strength or 0))))
    except (TypeError, ValueError):
        value = 0.0

    score = round(value / 5.0 * 100, 2)
    if value >= cfg["aPlusStrength"]:
        grade, eligible = "A+", True
    elif value >= cfg["aStrength"]:
        grade, eligible = "A", True
    elif value >= cfg["bPlusStrength"]:
        grade, eligible = "B+", False
    else:
        grade, eligible = "REJECT", False

    return {
        "grade": grade,
        "gradeScore": score,
        "executionEligible": eligible,
        "watchOnly": grade == "B+",
        "reason": (
            "Execution eligible"
            if eligible
            else "Watch-only grade" if grade == "B+" else "Below minimum grade"
        ),
    }


def install(core: Any, setup_worker: Any) -> None:
    if getattr(core, _INSTALLED_ATTR, False):
        return

    original_queue: Callable[..., Any] = setup_worker._queue_candidate
    original_evaluate: Callable[..., Any] = setup_worker._evaluate_symbol

    setup_worker._price_plan = dynamic_price_plan

    def graded_queue(candidate: dict[str, Any], queue_limit: int) -> bool:
        grading = grade_for_strength(candidate.get("strategyStrength"))
        candidate.update(grading)
        if not grading["executionEligible"]:
            return False
        return bool(original_queue(candidate, queue_limit))

    def graded_evaluate(core_obj: Any, active_row: dict[str, Any], now_ms: int, cfg: dict[str, Any]) -> dict[str, Any]:
        result = dict(original_evaluate(core_obj, active_row, now_ms, cfg) or {})
        if result.get("strategyStrength") is None:
            return result
        grading = grade_for_strength(result.get("strategyStrength"))
        result.update(grading)
        if grading["grade"] == "B+":
            result["status"] = "NEAR_SETUP"
            result["queued"] = False
            result["reason"] = "B+ signal is watch-only and cannot enter the execution queue"
        elif grading["grade"] == "REJECT":
            result["status"] = "NO_SETUP"
            result["queued"] = False
            result["reason"] = "Signal grade is below B+"
        return result

    setup_worker._queue_candidate = graded_queue
    setup_worker._evaluate_symbol = graded_evaluate
    setattr(core, _INSTALLED_ATTR, True)


def status(core: Any) -> dict[str, Any]:
    return {
        "installed": bool(getattr(core, _INSTALLED_ATTR, False)),
        "features": ["ATR_15M_DYNAMIC_SL_TP", "A_PLUS_A_B_PLUS_GRADING"],
        "policy": {"A+": "EXECUTE", "A": "EXECUTE", "B+": "WATCH_ONLY", "REJECT": "IGNORE"},
        "settings": settings(),
    }
