"""Authoritative non-rejecting position sizing for the locked 08 Aug 2026 plan.

Risk approval is the trade-eligibility decision. This module is a calculator and
exchange adapter only: it derives a structural stop/target, calculates quantity
from the approved risk budget, applies real available margin and Bybit instrument
rules, and prepares the Node execution payload. It never turns an already
risk-approved candidate into a trade rejection. Missing sizing inputs are exposed
as SIZING_WAIT so the candidate can be retried without changing eligibility.
"""

from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any, Callable, Mapping

try:
    from . import cost_policy_fix, intraday_scanner
    from .scanner_safety import filter_closed_candles
except ImportError:  # pragma: no cover
    import cost_policy_fix
    import intraday_scanner
    from scanner_safety import filter_closed_candles


POLICY_ID = "POSITION_SIZING_NON_REJECTING_2026_08_08"
_PERSIST_KEY = "position_sizing_margin_v1"
LEVERAGE = 10.0
MARGIN_MODE = "ISOLATED"
GRADE_RISK_PCT = {"A+": 1.0, "A": 1.0}

_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_STORE: Any | None = None
_SETUP_SETTINGS: Callable[[], dict[str, Any]] | None = None
_PRICE_PLAN: Callable[..., tuple[dict[str, float] | None, str]] | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 3,
    "policyId": POLICY_ID,
    "source": "risk_approved_non_rejecting_sizing",
    "fiveMinuteCandleTime": None,
    "inputFingerprint": None,
    "updatedAt": 0,
    "rows": [],
    "approvedSizingQueue": [],
    "metrics": {},
    "lastError": None,
    "persisted": False,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in {float("inf"), float("-inf")}:
        return default
    return result


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    approved = [dict(row) for row in _STATE.get("approvedSizingQueue") or []]
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 3),
        "policyId": str(_STATE.get("policyId") or POLICY_ID),
        "source": str(_STATE.get("source") or "risk_approved_non_rejecting_sizing"),
        "fiveMinuteCandleTime": _STATE.get("fiveMinuteCandleTime"),
        "inputFingerprint": _STATE.get("inputFingerprint"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "approvedSizingQueue": approved,
        "approvedSizingQueueSize": len(approved),
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "persisted": bool(_STATE.get("persisted")),
        "orderSubmissions": 0,
        "tradeRejectionAuthority": False,
    }


def snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return _snapshot_unlocked()


def _persistent_store(core: Any) -> Any | None:
    store = getattr(core, "_durable_state_store", None)
    if store is None:
        return None
    for name in ("get", "put", "status"):
        if not callable(getattr(store, name, None)):
            return None
    try:
        status = dict(store.status() or {})
    except Exception:
        return None
    if not status.get("ok") or status.get("degraded"):
        return None
    return store


def _load_persisted() -> None:
    if _STORE is None:
        return
    try:
        saved = _STORE.get(_PERSIST_KEY)
    except Exception:
        return
    if not isinstance(saved, dict):
        return
    rows = saved.get("rows")
    approved = saved.get("approvedSizingQueue")
    if not isinstance(rows, list) or not isinstance(approved, list):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or "idle"),
                "version": int(saved.get("version") or 3),
                "policyId": POLICY_ID,
                "source": "risk_approved_non_rejecting_sizing",
                "fiveMinuteCandleTime": saved.get("fiveMinuteCandleTime"),
                "inputFingerprint": saved.get("inputFingerprint"),
                "updatedAt": int(saved.get("updatedAt") or 0),
                "rows": [dict(row) for row in rows if isinstance(row, dict)],
                "approvedSizingQueue": [dict(row) for row in approved if isinstance(row, dict)],
                "metrics": dict(saved.get("metrics") or {}),
                "lastError": saved.get("lastError"),
                "persisted": True,
            }
        )


def _persist(payload: Mapping[str, Any]) -> bool:
    if _STORE is None:
        return False
    body = {
        "status": payload["status"],
        "version": payload["version"],
        "policyId": payload["policyId"],
        "source": payload["source"],
        "fiveMinuteCandleTime": payload["fiveMinuteCandleTime"],
        "inputFingerprint": payload["inputFingerprint"],
        "updatedAt": payload["updatedAt"],
        "rows": payload["rows"],
        "approvedSizingQueue": payload["approvedSizingQueue"],
        "metrics": payload["metrics"],
        "lastError": payload.get("lastError"),
    }
    try:
        _STORE.put(_PERSIST_KEY, body)
        confirmed = _STORE.get(_PERSIST_KEY)
    except Exception:
        return False
    return bool(
        isinstance(confirmed, dict)
        and confirmed.get("inputFingerprint") == body["inputFingerprint"]
        and list(confirmed.get("approvedSizingQueue") or []) == body["approvedSizingQueue"]
    )


def _risk_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "authoritative_entry_risk_status", None)
    if callable(reader):
        payload = reader()
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "authoritative_entry_risk", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _fingerprint(upstream: Mapping[str, Any]) -> str:
    base = str(upstream.get("inputFingerprint") or "")
    keys = sorted(
        str(row.get("candidateKey") or "")
        for row in upstream.get("approvedRiskQueue") or []
        if isinstance(row, dict) and row.get("candidateKey")
    )
    return f"{base}:{'|'.join(keys)}"


def _setup_config() -> dict[str, Any]:
    fallback = {
        "minimumClosedCandles": 60,
        "minimumRiskReward": 2.0,
        "structureLookback": 12,
    }
    if _SETUP_SETTINGS is None:
        return fallback
    try:
        return dict(_SETUP_SETTINGS() or fallback)
    except Exception:
        return fallback


def _technical_plan(core: Any, candidate: Mapping[str, Any]) -> tuple[dict[str, float] | None, str]:
    # Reuse already-carried structural values first when upstream supplied them.
    carried_stop = _number(
        candidate.get("technicalStopLoss")
        or candidate.get("stopLoss")
        or candidate.get("stopLossPrice"),
        0.0,
    )
    carried_take = _number(
        candidate.get("takeProfitReference")
        or candidate.get("takeProfit")
        or candidate.get("takeProfitPrice"),
        0.0,
    )
    carried_entry = _number(candidate.get("entryReference"), 0.0)
    if carried_stop > 0 and carried_take > 0 and carried_entry > 0:
        return {
            "entryReference": carried_entry,
            "stopLoss": carried_stop,
            "takeProfitReference": carried_take,
        }, "Upstream structural plan reused"

    if _PRICE_PLAN is None:
        return None, "Existing setup-worker price-plan helper is unavailable"
    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "")
    setup_time = int(_number(candidate.get("setupFifteenMinuteCandleTime"), 0))
    if not symbol or side not in {"Buy", "Sell"} or setup_time <= 0:
        return None, "Technical setup identity is incomplete"

    cfg = _setup_config()
    minimum = max(1, int(_number(cfg.get("minimumClosedCandles"), 60)))
    lookback = max(1, int(_number(cfg.get("structureLookback"), 12)))
    minimum_rr = max(0.0, _number(cfg.get("minimumRiskReward"), 2.0))
    candles, message = core.fetch_candles(symbol, "15", limit=max(80, minimum + 5))
    closed = filter_closed_candles(
        candles or [],
        "15",
        now_ms=setup_time + (15 * 60 * 1000),
    )
    history = [row for row in closed if int(row.get("time") or 0) <= setup_time]
    if not history or int(history[-1].get("time") or 0) != setup_time:
        return None, message or "Exact closed-15M setup candle is unavailable"
    if len(history) < max(minimum, lookback):
        return None, "Not enough closed 15M history for the structural plan"
    return _PRICE_PLAN(history, side, minimum_rr, lookback)


def _wallet_snapshot(core: Any) -> dict[str, Any]:
    try:
        payload = core.bybit_request(
            "GET",
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED"},
        )
    except Exception as exc:
        return {"ok": False, "reason": f"Wallet margin request failed: {exc}"}
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        return {
            "ok": False,
            "reason": str((payload or {}).get("retMsg") or "Wallet margin data unavailable"),
        }
    account = ((payload.get("result") or {}).get("list") or [{}])[0]
    equity = _number(account.get("totalEquity"), 0.0)
    available = _number(account.get("totalAvailableBalance"), -1.0)
    initial = _number(account.get("totalInitialMargin"), -1.0)
    if equity <= 0:
        return {"ok": False, "reason": "Wallet equity is unavailable"}
    if initial < 0:
        positions_reader = getattr(core, "get_open_positions", None)
        if not callable(positions_reader):
            return {"ok": False, "reason": "Current initial margin is unavailable"}
        positions, reason = positions_reader()
        if positions is None:
            return {"ok": False, "reason": str(reason or "Open positions unavailable")}
        initial = 0.0
        for position in positions:
            if not isinstance(position, dict):
                continue
            position_im = _number(position.get("positionIM") or position.get("positionIMByMp"), -1.0)
            if position_im < 0:
                return {"ok": False, "reason": "An open position has no authoritative initial-margin value"}
            initial += position_im
    current_initial = max(0.0, initial)
    available_source = "BYBIT_TOTAL_AVAILABLE_BALANCE"
    fallback_applied = False
    if available < 0:
        # Bybit Demo Unified wallet can leave totalAvailableBalance blank while
        # the account is funded. Use a conservative live-equity remainder.
        available = max(0.0, equity - current_initial)
        available_source = "EQUITY_MINUS_CURRENT_INITIAL_MARGIN"
        fallback_applied = True
    if available < 0:
        return {"ok": False, "reason": "Wallet available balance is unavailable"}
    return {
        "ok": True,
        "source": "BYBIT_UNIFIED_WALLET",
        "equity": equity,
        "availableMargin": available,
        "availableMarginSource": available_source,
        "availableMarginFallbackApplied": fallback_applied,
        "currentInitialMargin": current_initial,
    }


def _wait(
    candidate: Mapping[str, Any],
    *,
    code: str,
    reason: str,
    checks: Mapping[str, Any],
    timestamp: int,
) -> tuple[dict[str, Any], None, float]:
    row = {
        **dict(candidate),
        "positionSizingStatus": "SIZING_WAIT",
        "sizingPolicyId": POLICY_ID,
        "sizingDecisionAt": timestamp,
        "sizingApproved": False,
        "sizingDecision": {
            "ok": False,
            "code": code,
            "reason": reason,
            "checks": dict(checks),
            "tradeRejected": False,
            "retryable": True,
        },
        "executionStatus": "AWAITING_SIZING_DATA",
        "orderSubmitted": False,
        "tradeRejected": False,
    }
    return row, None, 0.0


# Backward-compatible private name; semantics are intentionally non-blocking.
def _blocked(
    candidate: Mapping[str, Any],
    *,
    code: str,
    reason: str,
    checks: Mapping[str, Any],
    timestamp: int,
) -> tuple[dict[str, Any], None, float]:
    return _wait(
        candidate,
        code=code,
        reason=reason,
        checks=checks,
        timestamp=timestamp,
    )


def _evaluate_candidate(
    core: Any,
    candidate: Mapping[str, Any],
    wallet: Mapping[str, Any],
    reserved_margin: float,
    timestamp: int,
) -> tuple[dict[str, Any], dict[str, Any] | None, float]:
    item = dict(candidate)
    checks: dict[str, Any] = {}
    symbol = str(item.get("symbol") or "").upper()
    side = str(item.get("side") or "")
    grade = str(item.get("grade") or "")
    entry = _number(item.get("entryReference"), 0.0)

    if (
        not item.get("riskApproved")
        or item.get("riskStatus") != "APPROVED_RISK"
        or not item.get("candidateKey")
        or not symbol
        or side not in {"Buy", "Sell"}
        or entry <= 0
        or item.get("orderSubmitted") is not False
    ):
        return _wait(
            item,
            code="SIZING_INPUT_NOT_READY",
            reason="Risk-approved candidate identity or entry state is incomplete",
            checks=checks,
            timestamp=timestamp,
        )

    grade_risk_pct = GRADE_RISK_PCT.get(grade)
    if grade_risk_pct is None:
        return _wait(
            item,
            code="UPSTREAM_RISK_GRADE_MISMATCH",
            reason="Sizing received a grade that should have been resolved by Risk; eligibility is not changed here",
            checks=checks,
            timestamp=timestamp,
        )

    plan, plan_reason = _technical_plan(core, item)
    checks["technicalPlan"] = {
        "ok": plan is not None,
        "reason": plan_reason,
        "source": "upstream_or_setup_worker_structural_plan",
        "setupFifteenMinuteCandleTime": item.get("setupFifteenMinuteCandleTime"),
    }
    if plan is None:
        return _wait(
            item,
            code="TECHNICAL_PLAN_WAIT",
            reason=plan_reason,
            checks=checks,
            timestamp=timestamp,
        )

    stop = _number(plan.get("stopLoss"), 0.0)
    original_take = _number(plan.get("takeProfitReference"), 0.0)
    stop_valid = stop > 0 and ((side == "Buy" and stop < entry) or (side == "Sell" and stop > entry))
    take_valid = original_take > 0 and (
        (side == "Buy" and original_take > entry) or (side == "Sell" and original_take < entry)
    )
    checks["technicalPlan"].update(
        {
            "stopLoss": stop,
            "takeProfitReference": original_take,
            "setupEntryReference": plan.get("entryReference"),
            "stopValidForEntry": stop_valid,
            "takeValidForEntry": take_valid,
        }
    )
    if not stop_valid or not take_valid:
        return _wait(
            item,
            code="TECHNICAL_PLAN_WAIT",
            reason="Structural SL/TP is not executable for the current closed-5M entry",
            checks=checks,
            timestamp=timestamp,
        )

    if not wallet.get("ok"):
        return _wait(
            item,
            code="WALLET_DATA_WAIT",
            reason=str(wallet.get("reason") or "Wallet margin data unavailable"),
            checks=checks,
            timestamp=timestamp,
        )

    equity = _number(wallet.get("equity"), 0.0)
    available = _number(wallet.get("availableMargin"), 0.0)
    current_initial = _number(wallet.get("currentInitialMargin"), 0.0)
    risk_factor = max(0.0, min(1.0, _number(item.get("riskSizeFactor"), 1.0)))
    effective_risk_pct = grade_risk_pct * risk_factor
    if effective_risk_pct <= 0:
        return _wait(
            item,
            code="RISK_SIZE_FACTOR_WAIT",
            reason="Authoritative risk size factor is zero; wait for a non-zero risk allocation",
            checks=checks,
            timestamp=timestamp,
        )

    stop_distance = abs(entry - stop)
    if stop_distance <= 0:
        return _wait(
            item,
            code="TECHNICAL_PLAN_WAIT",
            reason="Structural stop distance is zero",
            checks=checks,
            timestamp=timestamp,
        )

    risk_budget = equity * (effective_risk_pct / 100.0)
    raw_risk_qty = risk_budget / stop_distance
    remaining_available = max(0.0, available - reserved_margin)
    max_available_qty = (remaining_available * LEVERAGE) / entry if remaining_available > 0 else 0.0
    raw_qty = min(raw_risk_qty, max_available_qty)
    margin_reduced = raw_qty + 1e-12 < raw_risk_qty

    checks["riskAndMargin"] = {
        "grade": grade,
        "gradeRiskPct": grade_risk_pct,
        "existingRiskSizeFactor": risk_factor,
        "effectiveRiskPct": effective_risk_pct,
        "riskBudgetUsdt": risk_budget,
        "riskQuantity": raw_risk_qty,
        "marginMode": MARGIN_MODE,
        "leverage": LEVERAGE,
        "riskMultipliedByLeverage": False,
        "equity": equity,
        "availableMargin": available,
        "availableMarginSource": wallet.get("availableMarginSource"),
        "availableMarginFallbackApplied": bool(wallet.get("availableMarginFallbackApplied")),
        "currentInitialMargin": current_initial,
        "reservedMarginThisCycle": reserved_margin,
        "remainingAvailableMarginUsdt": remaining_available,
        "availableMarginQuantityCap": max_available_qty,
        "quantityReducedByAvailableMargin": margin_reduced,
        "fixedPerTradeMarginCapEnabled": False,
        "fixedCombinedMarginCapEnabled": False,
        "fixedFreeReserveGateEnabled": False,
    }
    if raw_qty <= 0:
        return _wait(
            item,
            code="AVAILABLE_MARGIN_WAIT",
            reason="No real available margin currently remains for this risk-sized position",
            checks=checks,
            timestamp=timestamp,
        )

    rules = core.get_instrument_rules(symbol)
    if not isinstance(rules, dict) or not rules.get("ok"):
        return _wait(
            item,
            code="INSTRUMENT_RULES_WAIT",
            reason=str((rules or {}).get("reason") or "Bybit instrument rules unavailable"),
            checks=checks,
            timestamp=timestamp,
        )

    qty_step = Decimal(str(rules.get("qtyStep") or "0"))
    min_qty = Decimal(str(rules.get("minOrderQty") or "0"))
    max_qty = Decimal(str(rules.get("maxOrderQty") or "0"))
    min_notional = Decimal(str(rules.get("minNotionalValue") or "0"))
    if qty_step <= 0:
        return _wait(
            item,
            code="INSTRUMENT_RULES_WAIT",
            reason="Bybit quantity step is unavailable",
            checks=checks,
            timestamp=timestamp,
        )

    qty = core.floor_to_step(Decimal(str(raw_qty)), qty_step)
    if max_qty > 0:
        qty = min(qty, max_qty)
    notional = qty * Decimal(str(entry))
    actual_risk = float(qty) * stop_distance
    required_margin = float(notional) / LEVERAGE
    projected_total_margin = current_initial + reserved_margin + required_margin
    projected_free_margin = equity - projected_total_margin

    invalid_rules = (
        qty <= 0
        or qty < min_qty
        or (max_qty > 0 and qty > max_qty)
        or (min_notional > 0 and notional < min_notional)
    )
    risk_or_available_violation = (
        required_margin > remaining_available + 1e-8
        or actual_risk > risk_budget + 1e-8
    )

    checks["bybitRules"] = {
        "ok": not invalid_rules,
        "qtyStep": str(qty_step),
        "minOrderQty": str(min_qty),
        "maxOrderQty": str(max_qty),
        "minNotionalValue": str(min_notional),
        "roundedQty": str(qty),
        "notional": str(notional),
    }
    checks["marginProjection"] = {
        "ok": not risk_or_available_violation,
        "requiredInitialMarginUsdt": required_margin,
        "projectedTotalInitialMarginUsdt": projected_total_margin,
        "projectedFreeMarginUsdt": projected_free_margin,
        "remainingAvailableMarginUsdt": remaining_available,
        "fixedMarginCapsEnabled": False,
    }

    if invalid_rules:
        return _wait(
            item,
            code="BYBIT_ORDER_RULE_WAIT",
            reason="Calculated quantity cannot currently satisfy Bybit quantity/min-notional rules within approved risk",
            checks=checks,
            timestamp=timestamp,
        )
    if risk_or_available_violation:
        return _wait(
            item,
            code="RISK_OR_MARGIN_RECALC_WAIT",
            reason="Rounded quantity needs recalculation within approved risk and real available margin",
            checks=checks,
            timestamp=timestamp,
        )

    stop_pct = (stop_distance / entry) * 100.0
    take_pct = (abs(original_take - entry) / entry) * 100.0
    try:
        market_cost = cost_policy_fix._market_cost(core, symbol, intraday_scanner)
        cost_gate = cost_policy_fix.evaluate_cost_policy(
            stop_pct=stop_pct,
            take_pct=take_pct,
            market_cost=market_cost,
            scanner_module=intraday_scanner,
            notional=float(notional),
            risk_amount=actual_risk,
        )
    except Exception as exc:
        cost_gate = {"ok": False, "reason": f"Cost estimate unavailable: {exc}"}

    # Cost/net-RR is informational in sizing. Trade eligibility was already decided
    # by Risk; sizing must not create a second rejection gate.
    checks["costAndNetRr"] = {**dict(cost_gate), "sizingGate": False}
    adjusted_take = original_take
    adjusted_take_pct = _number(cost_gate.get("adjustedTakeProfitPct"), 0.0)
    if adjusted_take_pct > 0:
        adjusted_distance = entry * (adjusted_take_pct / 100.0)
        candidate_take = entry + adjusted_distance if side == "Buy" else entry - adjusted_distance
        if (side == "Buy" and candidate_take > entry) or (side == "Sell" and candidate_take < entry):
            adjusted_take = candidate_take
    risk_reward = abs(adjusted_take - entry) / stop_distance if stop_distance > 0 else 0.0

    approved = {
        **item,
        "positionSizingStatus": "SIZING_APPROVED",
        "sizingPolicyId": POLICY_ID,
        "sizingDecisionAt": timestamp,
        "sizingApproved": True,
        "sizingDecision": {
            "ok": True,
            "code": "SIZING_APPROVED",
            "reason": "Risk-approved trade sized for Node execution; sizing is not a rejection authority",
            "checks": checks,
            "tradeRejected": False,
        },
        "qualityGrade": grade,
        "gradeRiskPct": grade_risk_pct,
        "effectiveRiskPerTradePct": round(effective_risk_pct, 6),
        "riskBudgetUsdt": round(risk_budget, 8),
        "actualStopRiskUsdt": round(actual_risk, 8),
        "entryReference": round(entry, 12),
        "technicalStopLoss": round(stop, 12),
        "technicalStopSource": "CLOSED_15M_STRUCTURE_OR_UPSTREAM_PLAN",
        "originalTakeProfitReference": round(original_take, 12),
        "takeProfitReference": round(adjusted_take, 12),
        "riskReward": round(risk_reward, 4),
        "costGate": {**dict(cost_gate), "sizingGate": False},
        "qty": core.format_qty(qty),
        "rawRiskQty": core.format_qty(Decimal(str(raw_risk_qty))),
        "rawMarginCappedQty": core.format_qty(Decimal(str(raw_qty))),
        "notional": core.format_qty(notional),
        "marginMode": MARGIN_MODE,
        "leverage": int(LEVERAGE),
        "requiredInitialMarginUsdt": round(required_margin, 8),
        "projectedTotalInitialMarginUsdt": round(projected_total_margin, 8),
        "projectedFreeMarginUsdt": round(projected_free_margin, 8),
        "marginReducedQuantity": margin_reduced,
        "marginCaps": {
            "fixedPerTradeEnabled": False,
            "fixedCombinedEnabled": False,
            "fixedFreeReserveEnabled": False,
        },
        "nodeExecutionRequirements": {
            "marginMode": MARGIN_MODE,
            "leverage": int(LEVERAGE),
            "maximumLeverage": int(LEVERAGE),
            "revalidateWalletAndInstrumentRules": True,
            "submitOnlyAfterRevalidation": True,
        },
        "executionStatus": "AWAITING_NODE_EXECUTION",
        "orderSubmitted": False,
        "tradeRejected": False,
    }
    return dict(approved), dict(approved), required_margin


def build(
    core: Any,
    now: int | None = None,
    *,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    timestamp = int(now or time.time())
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")
    try:
        source = dict(upstream or _risk_snapshot(core))
        if str(source.get("status") or "") not in {"ready", "empty"}:
            raise RuntimeError("Authoritative entry-risk snapshot is not ready")
        queue = [dict(row) for row in source.get("approvedRiskQueue") or [] if isinstance(row, dict)]
        wallet = (
            _wallet_snapshot(core)
            if queue
            else {"ok": True, "equity": 0.0, "availableMargin": 0.0, "currentInitialMargin": 0.0}
        )
        rows: list[dict[str, Any]] = []
        approved: list[dict[str, Any]] = []
        reserved_margin = 0.0
        for candidate in queue:
            row, approved_candidate, margin = _evaluate_candidate(
                core,
                candidate,
                wallet,
                reserved_margin,
                timestamp,
            )
            rows.append(row)
            if approved_candidate is not None:
                approved.append(approved_candidate)
                reserved_margin += margin

        waiting = len(rows) - len(approved)
        metrics = {
            "approvedRiskInput": len(queue),
            "evaluated": len(rows),
            "approved": len(approved),
            "waiting": waiting,
            "blocked": 0,
            "tradeRejections": 0,
            "technicalPlanChecks": len(rows),
            "walletChecks": 1 if queue else 0,
            "instrumentRuleChecks": sum(
                1
                for row in rows
                if "bybitRules" in (row.get("sizingDecision") or {}).get("checks", {})
            ),
            "marginReduced": sum(1 for row in approved if row.get("marginReducedQuantity")),
            "reservedInitialMarginUsdt": round(reserved_margin, 8),
            "orderSubmissions": 0,
            "automaticRiskPolicy": "A_PLUS_1_PERCENT_A_1_PERCENT_B_PLUS_REJECT_AT_RISK",
            "manualDemoRiskPolicyChanged": False,
            "marginPolicy": "ISOLATED_MAX_10X_AVAILABLE_MARGIN_ONLY_NO_FIXED_25_60_40_GATES",
            "sizingPolicy": "CALCULATOR_ONLY_NON_REJECTION",
        }
        fingerprint = _fingerprint(source)
        payload = {
            "status": "ready" if rows else "empty",
            "version": 3,
            "policyId": POLICY_ID,
            "source": "risk_approved_non_rejecting_sizing",
            "fiveMinuteCandleTime": source.get("fiveMinuteCandleTime"),
            "inputFingerprint": fingerprint,
            "updatedAt": timestamp,
            "rows": rows,
            "approvedSizingQueue": approved,
            "metrics": metrics,
            "lastError": None,
            "persisted": False,
        }
        # Persistence is support infrastructure only; failure does not change the
        # sizing result or trade eligibility.
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("rows") or _STATE.get("inputFingerprint"))
            _STATE.update({"status": "stale" if has_cache else "error", "lastError": str(exc)})
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(core: Any) -> bool:
    source = _risk_snapshot(core)
    fingerprint = _fingerprint(source)
    with _STATE_LOCK:
        return bool(
            fingerprint != str(_STATE.get("inputFingerprint") or "")
            or str(_STATE.get("status") or "") in {"error", "stale"}
        )


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    if not due(core):
        return snapshot()
    return build(core, now=now, upstream=_risk_snapshot(core))


def install(core: Any, setup_worker: Any) -> dict[str, Any]:
    global _STORE, _SETUP_SETTINGS, _PRICE_PLAN
    if getattr(core, "_position_sizing_margin_v1_installed", False):
        return status(core)
    setup_settings = getattr(setup_worker, "settings", None)
    price_plan = getattr(setup_worker, "_price_plan", None)
    if not callable(setup_settings) or not callable(price_plan):
        raise RuntimeError("Existing setup-worker structural price-plan helpers are unavailable")
    _STORE = _persistent_store(core)
    _SETUP_SETTINGS = setup_settings
    _PRICE_PLAN = price_plan
    _load_persisted()
    core.position_sizing_margin = lambda force=False: build(core) if force else ensure_current(core)
    core.position_sizing_margin_status = snapshot
    setattr(core, "_position_sizing_margin_v1_installed", True)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    return {
        "installed": bool(core is not None and getattr(core, "_position_sizing_margin_v1_installed", False)),
        "policyId": POLICY_ID,
        "automaticGradeRiskPct": dict(GRADE_RISK_PCT),
        "bPlusRejectedBySizing": False,
        "riskOwnsGradeRejection": True,
        "manualDemoRiskChanged": False,
        "technicalStopSource": "upstream_or_setup_worker_structural_plan",
        "marginMode": MARGIN_MODE,
        "leverage": int(LEVERAGE),
        "maximumLeverage": int(LEVERAGE),
        "fixedPerTradeMarginCapEnabled": False,
        "fixedCombinedMarginCapEnabled": False,
        "fixedFreeMarginReserveEnabled": False,
        "perTradeMarginCapPct": None,
        "combinedMarginCapPct": None,
        "minimumFreeMarginReservePct": None,
        "tradeRejectionAuthority": False,
        "costNetRrIsSizingGate": False,
        "submitsOrder": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _STORE, _SETUP_SETTINGS, _PRICE_PLAN
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 3,
                "policyId": POLICY_ID,
                "source": "risk_approved_non_rejecting_sizing",
                "fiveMinuteCandleTime": None,
                "inputFingerprint": None,
                "updatedAt": 0,
                "rows": [],
                "approvedSizingQueue": [],
                "metrics": {},
                "lastError": None,
                "persisted": False,
            }
        )
    _STORE = None
    _SETUP_SETTINGS = None
    _PRICE_PLAN = None
