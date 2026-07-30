"""Cost-aware target policy plus quality-based risk position sizing.

This runtime patch preserves the existing spread/net-RR controls and replaces the
legacy fixed-allocation sizing path for automatic confirmed setups.

Execution policy:
- A+ setup: 1.00% account-equity risk
- A setup: 0.75% account-equity risk
- B+ or lower: rejected before execution
- technical stop distance determines notional and quantity
- no arbitrary fixed 250 USDT allocation cap
- Bybit quantity step/minimum/maximum rules are enforced fail-closed
- rounded quantity is revalidated so actual stop risk never exceeds the grade cap
"""

from __future__ import annotations

import threading
from decimal import Decimal
from typing import Any


_CONTEXT = threading.local()


def _float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _spread_pct(item: dict[str, Any]) -> float | None:
    bid = _float(item.get("bid1Price"))
    ask = _float(item.get("ask1Price"))
    last = _float(item.get("lastPrice"))
    if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
        return None
    return ((ask - bid) / last) * 100


def _market_cost(core: Any, symbol: str, scanner_module: Any) -> dict[str, Any]:
    cfg = scanner_module.settings()
    payload = core.public_bybit_get(
        "/v5/market/tickers", {"category": "linear", "symbol": symbol}
    )
    item = (
        ((payload.get("result") or {}).get("list") or [{}])[0]
        if payload.get("retCode") == 0
        else {}
    )
    spread = _spread_pct(item)
    if spread is None:
        return {
            "ok": False,
            "blockCode": "BLOCKED_SPREAD_UNAVAILABLE",
            "reason": "Spread unavailable; cost policy failed closed",
        }
    tier = scanner_module.spread_tier(spread, cfg)
    slippage_pct = spread * _float(cfg.get("slippageMultiplier"))
    fee_pct = 2 * _float(cfg.get("takerFeePct"))
    return {
        "ok": True,
        "spreadPct": spread,
        "spreadTier": tier,
        "slippagePct": slippage_pct,
        "estimatedRoundTripFeePct": fee_pct,
        "estimatedTotalCostPct": spread + slippage_pct + fee_pct,
    }


def evaluate_cost_policy(
    *,
    stop_pct: float,
    take_pct: float,
    market_cost: dict[str, Any],
    scanner_module: Any,
    notional: float = 0.0,
    risk_amount: float = 0.0,
) -> dict[str, Any]:
    cfg = scanner_module.settings()
    minimum_gross_rr = _float(cfg.get("minimumGrossRr"), 2.0)
    minimum_net_rr = _float(cfg.get("minimumNetRr"), 1.7)
    preferred_net_rr = _float(cfg.get("preferredNetRr"), 2.0)
    maximum_cost_risk_pct = _float(cfg.get("maximumCostRiskPct"), 35.0)
    normal_cost_risk_pct = _float(cfg.get("normalCostRiskPct"), 15.0)

    if not market_cost.get("ok"):
        return dict(market_cost)
    spread = _float(market_cost.get("spreadPct"))
    tier = str(market_cost.get("spreadTier") or "blocked")
    total_cost_pct = _float(market_cost.get("estimatedTotalCostPct"))
    if tier == "blocked":
        return {
            **market_cost,
            "ok": False,
            "blockCode": "BLOCKED_WIDE_SPREAD",
            "reason": f"Spread {spread:.5f}% exceeds the maximum spread policy",
        }
    if stop_pct <= 0:
        return {
            **market_cost,
            "ok": False,
            "blockCode": "BLOCKED_INVALID_STOP",
            "reason": "Stop distance is zero or invalid",
        }

    required_take_for_net = minimum_net_rr * (stop_pct + total_cost_pct) + total_cost_pct
    required_take_for_gross = minimum_gross_rr * stop_pct
    required_take_pct = max(required_take_for_gross, required_take_for_net)
    adjusted_take_pct = max(take_pct, required_take_pct)
    estimated_cost = notional * (total_cost_pct / 100) if notional > 0 else 0.0
    cost_risk_pct = (
        (estimated_cost / risk_amount) * 100
        if risk_amount > 0
        else (total_cost_pct / stop_pct) * 100
    )
    gross_rr = adjusted_take_pct / stop_pct
    net_reward_pct = adjusted_take_pct - total_cost_pct
    net_risk_pct = stop_pct + total_cost_pct
    net_rr = net_reward_pct / net_risk_pct if net_risk_pct > 0 else 0.0

    block_code = None
    reason = "Cost and net RR approved"
    if cost_risk_pct > maximum_cost_risk_pct:
        block_code = "BLOCKED_COST_TO_RISK"
        reason = (
            f"Estimated cost is {cost_risk_pct:.2f}% of trade risk; "
            f"maximum allowed is {maximum_cost_risk_pct:.2f}%"
        )
    elif gross_rr + 1e-9 < minimum_gross_rr:
        block_code = "BLOCKED_GROSS_RR"
        reason = f"Gross RR {gross_rr:.2f} is below minimum {minimum_gross_rr:.2f}"
    elif net_rr + 1e-9 < minimum_net_rr:
        block_code = "BLOCKED_NET_RR"
        reason = f"Net RR {net_rr:.2f} is below minimum {minimum_net_rr:.2f}"

    size_factor = 1.0
    if tier in {"reduced", "strong_only"} or cost_risk_pct > normal_cost_risk_pct:
        size_factor = 0.5
    return {
        **market_cost,
        "ok": block_code is None,
        "blockCode": block_code,
        "reason": reason,
        "estimatedCostUsdt": round(estimated_cost, 4),
        "costRiskPct": round(cost_risk_pct, 4),
        "originalTakeProfitPct": round(take_pct, 6),
        "requiredTakeProfitPct": round(required_take_pct, 6),
        "adjustedTakeProfitPct": round(adjusted_take_pct, 6),
        "requiredGrossRr": round(required_take_pct / stop_pct, 4),
        "grossRr": round(gross_rr, 4),
        "netRr": round(net_rr, 4),
        "preferredNetRrMet": net_rr >= preferred_net_rr,
        "targetAdjusted": adjusted_take_pct > take_pct + 1e-9,
        "sizeFactor": size_factor,
    }


def classify_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    side = str(candidate.get("side") or candidate.get("expectedSide") or "")
    votes = list(candidate.get("strategyVotes") or [])
    aligned = [row for row in votes if str(row.get("signal") or "") == side]
    count = len(aligned)
    if count >= 3:
        return {"grade": "A+", "riskPct": 1.0, "alignedVotes": count, "eligible": True}
    if count == 2:
        return {"grade": "A", "riskPct": 0.75, "alignedVotes": count, "eligible": True}
    return {"grade": "B+", "riskPct": 0.0, "alignedVotes": count, "eligible": False}


def _quality_sizing(core: Any, symbol: str, state: dict[str, Any]) -> dict[str, Any]:
    candidate = getattr(_CONTEXT, "candidate", None)
    grade = str(state.get("qualityGrade") or (candidate or {}).get("qualityGrade") or "")
    risk_pct = _float(state.get("qualityRiskPct") or (candidate or {}).get("qualityRiskPct"))
    if grade not in {"A+", "A"} or not (0.5 <= risk_pct <= 1.0):
        return {
            "ok": False,
            "code": "QUALITY_GRADE_BLOCKED",
            "reason": "Automatic position sizing requires an eligible A or A+ setup; B+ is rejected.",
            "qty": "0",
            "qualityGrade": grade or "UNAVAILABLE",
        }

    mark = core.get_mark_price(symbol)
    if not mark or mark <= 0:
        return {"ok": False, "reason": "Mark price unavailable", "qty": "0"}
    equity, equity_message = core.get_wallet_equity()
    if equity is None or equity <= 0:
        return {"ok": False, "reason": equity_message, "qty": "0"}
    rules = core.get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"ok": False, "reason": rules.get("reason", "Instrument rules unavailable"), "qty": "0"}

    stop_pct = _float(state.get("stopLossPct"))
    if stop_pct <= 0:
        return {"ok": False, "reason": "Technical stop distance is invalid", "qty": "0"}
    risk_budget = equity * (risk_pct / 100)
    stop_distance = mark * (stop_pct / 100)
    raw_qty = Decimal(str(risk_budget / stop_distance))
    qty = core.floor_to_step(raw_qty, rules["qtyStep"])
    max_qty = rules.get("maxOrderQty") or Decimal("0")
    if max_qty > 0:
        qty = min(qty, max_qty)
    notional = qty * Decimal(str(mark))
    actual_stop_risk = float(qty) * stop_distance

    invalid = (
        qty <= 0
        or qty < rules["minOrderQty"]
        or (max_qty > 0 and qty > max_qty)
        or (rules["minNotionalValue"] > 0 and notional < rules["minNotionalValue"])
        or actual_stop_risk > risk_budget + 1e-8
    )
    if invalid:
        return {
            "ok": False,
            "code": "RISK_SIZING_BLOCKED",
            "reason": "Risk-based quantity does not meet Bybit limits or exceeds the grade risk cap.",
            "qty": "0",
            "qualityGrade": grade,
            "riskPerTradePct": risk_pct,
            "riskBudgetUsdt": round(risk_budget, 6),
            "actualStopRiskUsdt": round(actual_stop_risk, 6),
        }

    return {
        "ok": True,
        "reason": "Quality-based technical-stop position size approved",
        "qty": core.format_qty(qty),
        "rawQty": core.format_qty(raw_qty),
        "roundedQty": core.format_qty(qty),
        "notional": core.format_qty(notional),
        "estimatedNotional": core.format_qty(notional),
        "equity": round(equity, 4),
        "markPrice": mark,
        "stopLossPct": round(stop_pct, 6),
        "qualityGrade": grade,
        "alignedVotes": int((candidate or {}).get("alignedVotes") or 0),
        "riskPerTradePct": risk_pct,
        "riskBudgetUsdt": round(risk_budget, 6),
        "actualStopRiskUsdt": round(actual_stop_risk, 6),
        "actualRiskPct": round((actual_stop_risk / equity) * 100, 6),
        "allocationCapApplied": False,
        "qtyStep": core.format_qty(rules["qtyStep"]),
        "minQty": core.format_qty(rules["minOrderQty"]),
        "minNotionalValue": core.format_qty(rules["minNotionalValue"]),
    }


def install(core: Any, setup_worker: Any, scanner_module: Any) -> None:
    if getattr(core, "_cost_policy_fix_installed", False):
        return

    try:
        from . import execution_handoff
    except ImportError:
        import execution_handoff

    def explicit_estimate_trade_cost(core_arg, symbol, notional, risk_amount, stop_pct, take_pct):
        return evaluate_cost_policy(
            stop_pct=stop_pct,
            take_pct=take_pct,
            market_cost=_market_cost(core_arg, symbol, scanner_module),
            scanner_module=scanner_module,
            notional=notional,
            risk_amount=risk_amount,
        )

    scanner_module.estimate_trade_cost = explicit_estimate_trade_cost
    core.calculate_position_sizing = lambda symbol, state: _quality_sizing(core, symbol, state)

    original_price_plan = execution_handoff._price_plan

    def contextual_price_plan(core_arg: Any, candidate: dict[str, Any]):
        _CONTEXT.candidate = dict(candidate)
        return original_price_plan(core_arg, candidate)

    execution_handoff._price_plan = contextual_price_plan
    original_setup_run_batch = setup_worker.run_batch

    def quality_and_cost_adjusted_batch(core_arg: Any, symbol_worker: Any, now: int | None = None):
        original_setup_run_batch(core_arg, symbol_worker, now=now)
        with setup_worker._LOCK:
            queue = []
            queue_by_key = {}
            for raw in list(setup_worker._STATE.get("confirmedQueue") or []):
                candidate = dict(raw)
                row_match = next(
                    (
                        row for row in setup_worker._STATE.get("rows") or []
                        if row.get("candidateKey") == candidate.get("candidateKey")
                    ),
                    {},
                )
                candidate.setdefault("strategyVotes", list(row_match.get("strategyVotes") or []))
                quality = classify_quality(candidate)
                candidate.update({
                    "qualityGrade": quality["grade"],
                    "qualityRiskPct": quality["riskPct"],
                    "alignedVotes": quality["alignedVotes"],
                })
                if not quality["eligible"]:
                    continue
                entry = _float(candidate.get("entryReference"))
                stop = _float(candidate.get("stopLoss"))
                take = _float(candidate.get("takeProfitReference"))
                if entry <= 0 or stop <= 0 or take <= 0:
                    continue
                stop_pct = abs(entry - stop) / entry * 100
                take_pct = abs(take - entry) / entry * 100
                policy = evaluate_cost_policy(
                    stop_pct=stop_pct,
                    take_pct=take_pct,
                    market_cost=_market_cost(core_arg, str(candidate.get("symbol") or ""), scanner_module),
                    scanner_module=scanner_module,
                )
                candidate["costGate"] = policy
                if not policy.get("ok"):
                    continue
                adjusted_take_pct = _float(policy.get("adjustedTakeProfitPct"))
                distance = entry * adjusted_take_pct / 100
                candidate["takeProfitReference"] = round(
                    entry + distance if candidate.get("side") == "Buy" else entry - distance,
                    12,
                )
                candidate["riskReward"] = round(adjusted_take_pct / stop_pct, 4)
                queue.append(candidate)
                queue_by_key[str(candidate.get("candidateKey"))] = candidate

            setup_worker._STATE["confirmedQueue"] = queue
            rows = []
            for original in list(setup_worker._STATE.get("rows") or []):
                row = dict(original)
                key = str(row.get("candidateKey") or "")
                if row.get("status") == "CONFIRMED" and key:
                    approved = queue_by_key.get(key)
                    if approved:
                        row.update(approved)
                        row["reason"] = f"{approved['qualityGrade']} setup approved for risk-based sizing"
                    else:
                        quality = classify_quality(row)
                        row.update({
                            "status": "REJECTED",
                            "queued": False,
                            "qualityGrade": quality["grade"],
                            "qualityRiskPct": 0.0,
                            "alignedVotes": quality["alignedVotes"],
                            "reason": "B+ setup rejected: at least two aligned strategy votes are required",
                        })
                rows.append(row)
            setup_worker._STATE["rows"] = rows
        return setup_worker.snapshot()

    setup_worker.run_batch = quality_and_cost_adjusted_batch
    core._quality_position_sizing_installed = True
    core._cost_policy_fix_installed = True
