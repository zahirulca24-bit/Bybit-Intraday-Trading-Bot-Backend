"""Single authoritative daily-risk contract for the canonical live runtime.

Daily trade counts are informational only. New entries are blocked exclusively by
an unavailable daily-PnL truth source or the configured realized net-loss limit.
Existing-position protection remains active regardless of the new-entry lock.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

POLICY_ID = "DAILY_NET_LOSS_V1"
SOURCE = "BYBIT_DEMO_CLOSED_PNL"
_LEGACY_GATE_MARKERS = ("max trades/day", "maximum trades/day")


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result and result not in {float("inf"), float("-inf")} else default


def _legacy_trade_gate_reason(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return any(marker in text for marker in _LEGACY_GATE_MARKERS)


def _execution_truth(core: Any, trading_date: str) -> dict[str, Any]:
    service = getattr(core, "_live_execution_ledger_service", None)
    cached_summary = getattr(service, "cached_summary", None)
    if callable(cached_summary):
        try:
            truth = cached_summary(trading_date)
            if isinstance(truth, Mapping):
                return dict(truth)
        except Exception as exc:
            return {
                "available": False,
                "stale": True,
                "source": "BYBIT_DEMO_EXECUTION_LIST",
                "tradingDate": trading_date,
                "reason": f"Execution truth unavailable: {exc}",
            }
    return {
        "available": False,
        "stale": True,
        "source": "BYBIT_DEMO_EXECUTION_LIST",
        "tradingDate": trading_date,
        "reason": "Canonical execution ledger is not installed.",
    }


def _trade_counters(truth: Mapping[str, Any]) -> dict[str, Any]:
    available = bool(truth.get("available"))
    return {
        "source": truth.get("source") or "BYBIT_DEMO_EXECUTION_LIST",
        "available": available,
        "stale": bool(truth.get("stale", not available)),
        "totalExecutions": truth.get("totalExecutions") if available else None,
        "entryExecutions": truth.get("entryExecutions") if available else None,
        "exitExecutions": truth.get("exitExecutions") if available else None,
        "partialCloseExecutions": truth.get("partialCloseExecutions") if available else None,
        "completedTrades": truth.get("completedTrades") if available else None,
        "reversalExecutions": truth.get("reversalExecutions") if available else None,
        "openPositions": truth.get("openPositions") if available else None,
    }


def evaluate(core: Any, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the complete policy contract used by status, scanner and execution."""

    snapshot = dict(state or {})
    trading_date = str(core.get_current_trading_date_key())
    cap = max(0.0, _number(snapshot.get("dailyLossCapUsdt"), 0.0))
    truth = _execution_truth(core, trading_date)
    counters = _trade_counters(truth)

    try:
        closed_pnl, pnl_message = core.get_daily_closed_pnl(trading_date)
    except Exception as exc:
        closed_pnl, pnl_message = None, str(exc)

    if closed_pnl is None:
        realized_net = None
        loss_used = None
        remaining = None
        blocked = True
        reason = f"Daily PnL truth unavailable; new entries blocked: {pnl_message}"
        ok = False
    else:
        realized_net = _number(closed_pnl)
        loss_used = abs(min(0.0, realized_net))
        remaining = max(0.0, cap - loss_used) if cap > 0 else None
        blocked = bool(cap > 0 and loss_used >= cap)
        ok = True
        if blocked:
            reason = f"Daily net-loss limit reached ({loss_used:.4f}/{cap:.4f} USDT)"
        elif cap > 0:
            reason = f"Daily risk OK; {remaining:.4f} USDT loss capacity remains; trade count unlimited"
        else:
            reason = "Daily net-loss limit disabled; trade count unlimited"

    completed_trades = counters.get("completedTrades")
    return {
        "ok": ok,
        "authoritative": True,
        "policyId": POLICY_ID,
        "source": SOURCE,
        "tradingDateKey": trading_date,
        "blocked": blocked,
        "newEntriesAllowed": not blocked,
        "existingPositionProtectionAllowed": True,
        "reason": reason,
        "lockType": "DAILY_NET_LOSS" if blocked and closed_pnl is not None else (
            "PNL_TRUTH_UNAVAILABLE" if blocked else None
        ),
        "dailyLossLimitEnabled": cap > 0,
        "dailyLossCapUsdt": cap,
        "closedPnl": round(realized_net, 8) if realized_net is not None else None,
        "realizedNetPnl": round(realized_net, 8) if realized_net is not None else None,
        "lossUsed": round(loss_used, 8) if loss_used is not None else None,
        "dailyLossUsed": round(loss_used, 8) if loss_used is not None else None,
        "remainingLossCapacity": round(remaining, 8) if remaining is not None else None,
        "tradeCountLimitEnabled": False,
        "maxTradesPerDay": None,
        "tradesPerDay": "UNLIMITED",
        "tradeCountInformationalOnly": True,
        "legacyTradeGateActive": False,
        "tradesToday": completed_trades,
        "tradesTodaySemantics": "completed_position_cycles",
        "tradeCounters": counters,
        "executionTruth": truth,
        "pnlMessage": str(pnl_message or "OK"),
    }


def debug_report(core: Any, state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    policy = evaluate(core, state)
    counters = dict(policy.get("tradeCounters") or {})
    return {
        "policyId": POLICY_ID,
        "authoritative": True,
        "tradingDateKey": policy["tradingDateKey"],
        "dayStartEpoch": core.get_trading_day_start_epoch(policy["tradingDateKey"]),
        "timezone": core.get_configured_timezone(),
        "tradesToday": {
            "source": counters.get("source"),
            "count": counters.get("completedTrades"),
            "max": None,
            "limitEnabled": False,
            "semantics": "completed_position_cycles",
        },
        "executionCounters": counters,
        "dailyLossUsed": {
            "source": SOURCE,
            "value": policy.get("lossUsed"),
            "cap": policy.get("dailyLossCapUsdt"),
            "remaining": policy.get("remainingLossCapacity"),
        },
        "newEntriesAllowed": policy["newEntriesAllowed"],
        "existingPositionProtectionAllowed": True,
        "lockReason": policy["reason"],
    }


def _apply_policy_to_state(state: dict[str, Any], policy: Mapping[str, Any]) -> None:
    state["maxTradesPerDay"] = None
    state["dailyRisk"] = dict(policy)

    if _legacy_trade_gate_reason(state.get("lastReason")):
        state["lastReason"] = str(policy["reason"])

    guard = state.get("executionGuard")
    if isinstance(guard, Mapping) and _legacy_trade_gate_reason(guard.get("reason")):
        state["executionGuard"] = {
            "ok": bool(policy["newEntriesAllowed"]),
            "reason": str(policy["reason"]),
            "policyId": POLICY_ID,
        }

    lifecycle = state.get("orderLifecycle")
    if isinstance(lifecycle, Mapping) and _legacy_trade_gate_reason(lifecycle.get("reason")):
        if policy["blocked"]:
            state["orderLifecycle"] = {
                **dict(lifecycle),
                "guard": "blocked",
                "order": "skipped",
                "protection": "active_for_existing_positions",
                "status": "blocked",
                "reason": str(policy["reason"]),
            }
        else:
            state["orderLifecycle"] = {
                **dict(lifecycle),
                "signal": "WAIT",
                "guard": "idle",
                "order": "idle",
                "protection": "active_for_existing_positions",
                "status": "idle",
                "reason": str(policy["reason"]),
            }


def _persist_sanitized_state(core: Any) -> None:
    store = getattr(core, "_durable_state_store", None)
    get_value = getattr(store, "get", None)
    put_value = getattr(store, "put", None)
    if not callable(get_value) or not callable(put_value):
        return
    try:
        saved = get_value("risk_state", {})
        body = dict(saved) if isinstance(saved, Mapping) else {}
        for key in (
            "tradingDateKey",
            "lastTradeAt",
            "lastSignal",
            "lastReason",
            "lastOrder",
            "executionGuard",
            "orderLifecycle",
            "positionSizing",
            "tradeManagement",
            "dailyRisk",
            "maxOpenPositions",
            "maxTradesPerDay",
            "dailyLossCapUsdt",
        ):
            body[key] = core.BOT_STATE.get(key)
        put_value("risk_state", body)
    except Exception as exc:
        print(f"Authoritative daily-risk persistence warning: {exc}", flush=True)


def install(core: Any) -> dict[str, Any]:
    """Replace every live daily-risk reader with the same policy decision."""

    if getattr(core, "_authoritative_daily_risk_installed", False):
        return {"installed": True, "policyId": POLICY_ID}

    def daily_risk_report(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return evaluate(core, state)

    def daily_loss_cap_reached(state: Mapping[str, Any] | None = None) -> tuple[bool, str]:
        policy = evaluate(core, state)
        return bool(policy["blocked"]), str(policy["reason"])

    def get_debug_risk_info(state: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return debug_report(core, state)

    core.daily_risk_report = daily_risk_report
    core.daily_loss_cap_reached = daily_loss_cap_reached
    core.get_debug_risk_info = get_debug_risk_info
    core.authoritative_daily_risk_policy = daily_risk_report

    lock = getattr(core, "BOT_LOCK", None)
    if lock is not None:
        with lock:
            snapshot = dict(core.BOT_STATE)
        policy = evaluate(core, snapshot)
        with lock:
            _apply_policy_to_state(core.BOT_STATE, policy)
    else:
        policy = evaluate(core, getattr(core, "BOT_STATE", {}))
        _apply_policy_to_state(core.BOT_STATE, policy)

    core._authoritative_daily_risk_installed = True
    _persist_sanitized_state(core)
    return {
        "installed": True,
        "policyId": POLICY_ID,
        "tradeCountLimitEnabled": False,
        "newEntriesAllowed": policy["newEntriesAllowed"],
        "reason": policy["reason"],
    }
