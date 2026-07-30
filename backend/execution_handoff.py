"""Crash-safe handoff from confirmed setup candidates to Bybit Demo execution.

The handoff consumes at most one candidate per run. A candidate is never removed
from the setup queue before the exchange outcome has been durably recorded.
Unknown or unresolved submission outcomes remain claimed and fail closed so the
same candidate cannot be submitted again automatically after a crash/restart.
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from typing import Any

try:
    from . import setup_queue_transaction
except ImportError:
    import setup_queue_transaction


_LOCK = threading.Lock()
_ACTIVE_CLAIM_KEY = "execution_handoff_active_claim"
_LAST_CLAIM_KEY = "execution_handoff_last_claim"
_ACTIVE_CLAIM_STATES = {"CLAIMED", "SUBMITTED", "UNRESOLVED", "SUBMISSION_UNKNOWN"}
_RESOLVED_CLAIM_STATES = {"RESOLVED_FILLED", "RESOLVED_NO_FILL"}

_STATE: dict[str, Any] = {
    "status": "idle",
    "lastRunAt": 0,
    "lastCandidateKey": None,
    "lastResult": None,
    "attempts": 0,
    "executed": 0,
    "blocked": 0,
    "failed": 0,
    "lastError": None,
    "activeClaim": None,
    "claimStore": None,
}


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, float]:
    return {
        "maxCandidateAgeSeconds": _number(
            "EXECUTION_CANDIDATE_MAX_AGE_SECONDS", 1200, 60, 3600
        ),
        "maxEntryDriftPct": _number(
            "EXECUTION_MAX_ENTRY_DRIFT_PCT", 0.50, 0.05, 3.0
        ),
    }


def _result(
    status: str,
    code: str,
    reason: str,
    candidate: dict | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "status": status,
        "code": code,
        "reason": reason,
        "candidateKey": (candidate or {}).get("candidateKey"),
        "symbol": (candidate or {}).get("symbol"),
        **extra,
    }


def _record(core: Any, result: dict[str, Any], timestamp: int) -> dict[str, Any]:
    """Record while ``run_once`` owns the non-reentrant handoff lock."""
    _STATE["status"] = result["status"]
    _STATE["lastRunAt"] = timestamp
    _STATE["lastCandidateKey"] = result.get("candidateKey")
    _STATE["lastResult"] = dict(result)
    _STATE["lastError"] = (
        result.get("reason") if result["status"] == "error" else None
    )
    if result["status"] == "executed":
        _STATE["executed"] += 1
    elif result["status"] == "blocked":
        _STATE["blocked"] += 1
    elif result["status"] == "error":
        _STATE["failed"] += 1
    try:
        core.get_bot_engine().journal.add("setup_execution_handoff", result)
        core.get_bot_engine().set_status("journal", "ok")
    except Exception:
        # AUD-P1-07 remains a separately locked finding.
        pass
    return snapshot_unlocked()


def _price_plan(
    core: Any, candidate: dict[str, Any]
) -> tuple[dict[str, float] | None, str]:
    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "")
    try:
        entry_reference = float(candidate.get("entryReference") or 0)
        stop = float(candidate.get("stopLoss") or 0)
        take = float(candidate.get("takeProfitReference") or 0)
    except (TypeError, ValueError):
        return None, "Candidate prices are invalid"

    mark = float(core.get_mark_price(symbol) or 0)
    if mark <= 0 or entry_reference <= 0:
        return None, "Current mark price or entry reference is unavailable"

    drift_pct = abs(mark - entry_reference) / entry_reference * 100
    if drift_pct > settings()["maxEntryDriftPct"]:
        return None, f"Candidate is stale by price drift ({drift_pct:.4f}%)"

    if side == "Buy":
        if not (stop < mark < take):
            return None, "Buy candidate stop/target is invalid at current mark"
        stop_pct = (mark - stop) / mark * 100
        take_pct = (take - mark) / mark * 100
    elif side == "Sell":
        if not (take < mark < stop):
            return None, "Sell candidate stop/target is invalid at current mark"
        stop_pct = (stop - mark) / mark * 100
        take_pct = (mark - take) / mark * 100
    else:
        return None, "Candidate side must be Buy or Sell"

    if stop_pct <= 0 or take_pct <= 0 or take_pct / stop_pct < 2.0:
        return None, "Candidate no longer provides minimum 1:2 gross RR"

    return {
        "markPrice": mark,
        "entryDriftPct": drift_pct,
        "stopLossPct": stop_pct,
        "takeProfitPct": take_pct,
        "grossRr": take_pct / stop_pct,
    }, "Candidate price plan revalidated"


def _normalized_fill_state(value: Any) -> str:
    return "".join(ch.lower() for ch in str(value or "") if ch.isalnum())


def _positive_number(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _mandatory_fill_decision(order: Any) -> dict[str, Any]:
    """Accept only complete, terminal, positive-quantity fill evidence."""
    if not isinstance(order, dict):
        return {
            "accepted": False,
            "code": "ORDER_RESPONSE_INVALID",
            "reason": "Order execution returned an invalid response.",
            "requiresOperatorReview": True,
            "verification": None,
        }

    try:
        ret_code = int(order.get("retCode", -1))
    except (TypeError, ValueError):
        ret_code = -1

    if ret_code != 0:
        return {
            "accepted": False,
            "code": "ORDER_CREATE_REJECTED",
            "reason": str(
                order.get("retMsg") or "Bybit rejected the order request."
            ),
            "requiresOperatorReview": False,
            "verification": order.get("fillVerification"),
        }

    verification = order.get("fillVerification")
    if verification is None:
        return {
            "accepted": False,
            "code": "FILL_VERIFICATION_MISSING",
            "reason": "Mandatory fillVerification evidence is missing.",
            "requiresOperatorReview": True,
            "verification": None,
        }
    if not isinstance(verification, dict):
        return {
            "accepted": False,
            "code": "FILL_VERIFICATION_INVALID",
            "reason": "Mandatory fillVerification evidence is invalid.",
            "requiresOperatorReview": True,
            "verification": None,
        }

    state = _normalized_fill_state(verification.get("state"))
    reason = str(
        verification.get("reason")
        or order.get("retMsg")
        or "Final full fill was not independently verified."
    )
    evidence = {
        "orderAccepted": order.get("accepted") is True,
        "orderFinalFilled": order.get("finalFilled") is True,
        "exchangeAccepted": order.get("exchangeRetCode") == 0,
        "verificationAccepted": verification.get("accepted") is True,
        "verificationFinalFilled": verification.get("finalFilled") is True,
        "terminal": verification.get("terminal") is True,
        "resolved": verification.get("unresolved") is False,
        "stateFilled": state == "filled",
        "positiveExecutedQty": _positive_number(
            verification.get("cumExecQty")
        ),
    }

    if all(evidence.values()):
        return {
            "accepted": True,
            "code": "ORDER_FILLED",
            "reason": reason,
            "requiresOperatorReview": False,
            "verification": dict(verification),
            "evidence": evidence,
        }

    if state == "partial":
        code = "ORDER_PARTIAL"
    elif state in {"cancelled", "canceled", "deactivated", "expired"}:
        code = "ORDER_CANCELLED"
    elif state in {"rejected", "createrejected"}:
        code = "ORDER_REJECTED"
    elif state in {"pending", "timeout", "unknown", "new", "active", "created"}:
        code = "ORDER_FILL_UNRESOLVED"
    elif state in {
        "invalidfill",
        "verificationerror",
        "invalidcreateresponse",
    }:
        code = "FILL_VERIFICATION_INVALID"
    else:
        code = "FILL_VERIFICATION_INCOMPLETE"

    requires_review = bool(verification.get("unresolved")) or code in {
        "ORDER_PARTIAL",
        "ORDER_FILL_UNRESOLVED",
        "FILL_VERIFICATION_INVALID",
        "FILL_VERIFICATION_INCOMPLETE",
    }
    return {
        "accepted": False,
        "code": code,
        "reason": reason,
        "requiresOperatorReview": requires_review,
        "verification": dict(verification),
        "evidence": evidence,
    }


def _claim_summary(claim: Any) -> dict[str, Any] | None:
    if not isinstance(claim, dict):
        return None
    return {
        "claimId": claim.get("claimId"),
        "candidateKey": claim.get("candidateKey"),
        "state": claim.get("state"),
        "claimedAt": claim.get("claimedAt"),
        "submittedAt": claim.get("submittedAt"),
        "resolvedAt": claim.get("resolvedAt"),
        "completedAt": claim.get("completedAt"),
        "queueRemovalPending": bool(claim.get("queueRemovalPending")),
        "requiresOperatorReview": bool(claim.get("requiresOperatorReview")),
    }


def _claim_store(core: Any) -> tuple[Any | None, dict[str, Any], str | None]:
    store = getattr(core, "_durable_state_store", None)
    if store is None:
        return None, {"ok": False, "degraded": True}, (
            "Durable execution claim store is not installed."
        )

    required = ("get", "put", "delete", "put_if_absent", "status")
    if any(not callable(getattr(store, name, None)) for name in required):
        return None, {"ok": False, "degraded": True}, (
            "Durable execution claim store lacks required atomic operations."
        )

    try:
        status = dict(store.status() or {})
    except Exception as exc:
        return None, {"ok": False, "degraded": True}, (
            f"Durable execution claim store status failed: {exc}"
        )

    if not status.get("ok"):
        return None, status, "Durable execution claim store is unavailable."
    if status.get("degraded") or not status.get("persistentPathConfigured"):
        return None, status, (
            "Durable execution claim path is not explicitly configured; "
            "automatic execution is blocked."
        )
    return store, status, None


def _load_active_claim(store: Any) -> dict[str, Any] | None:
    claim = store.get(_ACTIVE_CLAIM_KEY)
    return dict(claim) if isinstance(claim, dict) else None


def _persist_claim(store: Any, claim: dict[str, Any]) -> dict[str, Any]:
    store.put(_ACTIVE_CLAIM_KEY, claim)
    saved = store.get(_ACTIVE_CLAIM_KEY)
    if not isinstance(saved, dict):
        raise RuntimeError("Durable execution claim could not be reloaded.")
    if saved.get("claimId") != claim.get("claimId"):
        raise RuntimeError("Durable execution claim identity changed.")
    if saved.get("state") != claim.get("state"):
        raise RuntimeError("Durable execution claim state was not committed.")
    return dict(saved)


def _create_claim(
    store: Any, candidate: dict[str, Any], timestamp: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    claim = {
        "version": 1,
        "claimId": secrets.token_hex(16),
        "candidateKey": str(candidate.get("candidateKey") or ""),
        "candidate": dict(candidate),
        "state": "CLAIMED",
        "claimedAt": timestamp,
        "updatedAt": timestamp,
        "queueRemovalPending": False,
        "requiresOperatorReview": False,
    }
    created = bool(store.put_if_absent(_ACTIVE_CLAIM_KEY, claim))
    if not created:
        return None, _load_active_claim(store)
    saved = _load_active_claim(store)
    if (
        not isinstance(saved, dict)
        or saved.get("claimId") != claim["claimId"]
        or saved.get("state") != "CLAIMED"
    ):
        raise RuntimeError("Durable candidate claim was not atomically committed.")
    return saved, None


def _transition_claim(
    store: Any,
    claim: dict[str, Any],
    state: str,
    timestamp: int,
    **changes: Any,
) -> dict[str, Any]:
    updated = {
        **dict(claim),
        **changes,
        "state": state,
        "updatedAt": timestamp,
    }
    return _persist_claim(store, updated)


def _archive_and_clear_claim(store: Any, claim: dict[str, Any]) -> None:
    store.put(_LAST_CLAIM_KEY, claim)
    archived = store.get(_LAST_CLAIM_KEY)
    if not isinstance(archived, dict) or archived.get("claimId") != claim.get(
        "claimId"
    ):
        raise RuntimeError("Resolved execution claim could not be archived.")
    store.delete(_ACTIVE_CLAIM_KEY)


def _disable_execution(core: Any, reason: str) -> None:
    with core.BOT_LOCK:
        core.BOT_STATE.update(
            {
                "enabled": False,
                "lastReason": reason,
                "executionGuard": {"ok": False, "reason": reason},
            }
        )


def _complete_resolved_claim(
    core: Any,
    setup_worker: Any,
    store: Any,
    claim: dict[str, Any],
    timestamp: int,
    *,
    recovery: bool,
) -> dict[str, Any]:
    candidate = dict(claim.get("candidate") or {})
    candidate_key = str(claim.get("candidateKey") or "")
    removed, removal_reason = setup_queue_transaction.remove_exact_candidate(
        setup_worker, candidate_key
    )
    if not removed:
        reason = (
            "Durable execution result exists, but the matching candidate could "
            f"not be removed safely: {removal_reason}"
        )
        claim = _transition_claim(
            store,
            claim,
            str(claim.get("state") or "UNRESOLVED"),
            timestamp,
            queueRemovalPending=True,
            queueRemovalError=reason,
            requiresOperatorReview=True,
        )
        _STATE["activeClaim"] = _claim_summary(claim)
        _disable_execution(core, reason)
        return _record(
            core,
            _result(
                "error",
                "QUEUE_RESOLUTION_FAILED",
                reason,
                candidate,
                durableClaim=_claim_summary(claim),
            ),
            timestamp,
        )

    completed = _transition_claim(
        store,
        claim,
        "COMPLETED",
        timestamp,
        queueRemovalPending=False,
        queueRemovalCompletedAt=timestamp,
        queueRemovalResult=removal_reason,
        completedAt=timestamp,
    )
    _archive_and_clear_claim(store, completed)
    _STATE["activeClaim"] = None

    if recovery:
        return _record(
            core,
            _result(
                "blocked",
                "CLAIM_RECOVERY_COMPLETED",
                (
                    "A previously resolved durable execution claim was finalized "
                    "without resubmitting an order."
                ),
                candidate,
                durableClaim=_claim_summary(completed),
                queueRemovalResult=removal_reason,
            ),
            timestamp,
        )
    return completed


def _recover_or_block_existing_claim(
    core: Any,
    setup_worker: Any,
    store: Any,
    claim: dict[str, Any],
    timestamp: int,
) -> dict[str, Any]:
    state = str(claim.get("state") or "UNKNOWN").upper()
    _STATE["activeClaim"] = _claim_summary(claim)

    if state in _RESOLVED_CLAIM_STATES or state == "COMPLETED":
        return _complete_resolved_claim(
            core,
            setup_worker,
            store,
            claim,
            timestamp,
            recovery=True,
        )

    reason = (
        "A durable execution claim has an unresolved exchange-submission "
        f"outcome ({state}); automatic resubmission is blocked."
    )
    _disable_execution(core, reason)
    return _record(
        core,
        _result(
            "blocked",
            "EXECUTION_CLAIM_UNRESOLVED",
            reason,
            dict(claim.get("candidate") or {}),
            durableClaim=_claim_summary(claim),
        ),
        timestamp,
    )


def _remove_pre_execution_candidate(
    core: Any,
    setup_worker: Any,
    candidate: dict[str, Any],
    timestamp: int,
    code: str,
    reason: str,
    **extra: Any,
) -> dict[str, Any]:
    removed, removal_reason = setup_queue_transaction.remove_exact_candidate(
        setup_worker, str(candidate.get("candidateKey") or "")
    )
    if not removed:
        return _record(
            core,
            _result(
                "error",
                "QUEUE_REMOVE_FAILED",
                f"{reason}; candidate removal failed: {removal_reason}",
                candidate,
                **extra,
            ),
            timestamp,
        )
    return _record(
        core,
        _result(
            "blocked",
            code,
            reason,
            candidate,
            queueRemovalResult=removal_reason,
            **extra,
        ),
        timestamp,
    )


def run_once(
    core: Any, setup_worker: Any, now: int | None = None
) -> dict[str, Any]:
    """Attempt one candidate using a durable claim-before-submit transaction."""
    timestamp = int(now or time.time())
    if not _LOCK.acquire(blocking=False):
        return snapshot_with_result(
            _result(
                "busy",
                "HANDOFF_BUSY",
                "Execution handoff already running",
            )
        )

    store = None
    claim: dict[str, Any] | None = None
    submission_started = False
    try:
        _STATE["attempts"] += 1

        store, store_status, store_error = _claim_store(core)
        _STATE["claimStore"] = store_status
        if store is None:
            reason = str(store_error or "Durable claim store unavailable.")
            _disable_execution(core, reason)
            return _record(
                core,
                _result(
                    "blocked",
                    "DURABLE_CLAIM_UNAVAILABLE",
                    reason,
                    durableClaimStore=store_status,
                ),
                timestamp,
            )

        existing_claim = _load_active_claim(store)
        if existing_claim:
            return _recover_or_block_existing_claim(
                core,
                setup_worker,
                store,
                existing_claim,
                timestamp,
            )

        queue = list(
            (setup_worker.snapshot() or {}).get("confirmedQueue") or []
        )
        if not queue:
            return snapshot_with_result(
                _result(
                    "idle",
                    "NO_CANDIDATE",
                    "No confirmed setup candidate is pending",
                )
            )

        candidate = dict(queue[0])
        candidate_key = str(candidate.get("candidateKey") or "")

        with core.BOT_LOCK:
            state = dict(core.BOT_STATE)
        if not state.get("enabled"):
            return snapshot_with_result(
                _result(
                    "waiting",
                    "BOT_STOPPED",
                    "Bot is stopped; candidate remains queued",
                    candidate,
                )
            )

        created_at = int(candidate.get("createdAt") or 0)
        age = timestamp - created_at if created_at else 10**9
        if age > settings()["maxCandidateAgeSeconds"]:
            return _remove_pre_execution_candidate(
                core,
                setup_worker,
                candidate,
                timestamp,
                "CANDIDATE_STALE",
                f"Candidate age {age}s exceeds limit",
            )

        symbol = str(candidate.get("symbol") or "").upper()
        side = str(candidate.get("side") or "")
        if not candidate_key or not symbol or side not in {"Buy", "Sell"}:
            return _remove_pre_execution_candidate(
                core,
                setup_worker,
                candidate,
                timestamp,
                "INVALID_CANDIDATE",
                "Candidate key, symbol, or side is invalid",
            )

        daily = core.daily_risk_report(state)
        if not daily.get("ok") or daily.get("blocked"):
            return _record(
                core,
                _result(
                    "blocked",
                    "DAILY_RISK_BLOCKED",
                    daily.get("reason", "Daily risk blocked"),
                    candidate,
                    dailyRisk=daily,
                ),
                timestamp,
            )

        position_guard = core.existing_position_guard(symbol, side, state)
        if not position_guard.get("ok"):
            return _record(
                core,
                _result(
                    "blocked",
                    "POSITION_GUARD_BLOCKED",
                    position_guard.get("reason", "Position guard blocked"),
                    candidate,
                    positionGuard=position_guard,
                ),
                timestamp,
            )

        plan, plan_reason = _price_plan(core, candidate)
        if plan is None:
            return _remove_pre_execution_candidate(
                core,
                setup_worker,
                candidate,
                timestamp,
                "PRICE_PLAN_BLOCKED",
                plan_reason,
            )

        execution_state = {
            **state,
            "symbol": symbol,
            "signal": side,
            "strategyStrength": candidate.get("strategyStrength"),
            "stopLossPct": plan["stopLossPct"],
            "takeProfitPct": plan["takeProfitPct"],
        }
        sizing = core.calculate_position_sizing(symbol, execution_state)
        if not sizing.get("ok"):
            return _remove_pre_execution_candidate(
                core,
                setup_worker,
                candidate,
                timestamp,
                "SIZING_BLOCKED",
                sizing.get("reason", "Position sizing blocked"),
                positionSizing=sizing,
                pricePlan=plan,
            )

        claim, conflicting_claim = _create_claim(
            store, candidate, timestamp
        )
        if claim is None:
            reason = (
                "Another durable execution claim already exists; automatic "
                "submission is blocked."
            )
            _disable_execution(core, reason)
            return _record(
                core,
                _result(
                    "blocked",
                    "EXECUTION_CLAIM_CONFLICT",
                    reason,
                    candidate,
                    durableClaim=_claim_summary(conflicting_claim),
                ),
                timestamp,
            )
        _STATE["activeClaim"] = _claim_summary(claim)

        submission_started = True
        raw_order = core.place_demo_order(
            symbol,
            side,
            sizing.get("qty"),
            "setup-worker",
            plan["stopLossPct"],
            plan["takeProfitPct"],
        )
        order = (
            raw_order
            if isinstance(raw_order, dict)
            else {
                "retCode": -1,
                "retMsg": "Order execution returned an invalid response.",
                "result": {},
            }
        )

        claim = _transition_claim(
            store,
            claim,
            "SUBMITTED",
            timestamp,
            submittedAt=timestamp,
            orderResponse=order,
            queueRemovalPending=False,
        )
        _STATE["activeClaim"] = _claim_summary(claim)

        fill_decision = _mandatory_fill_decision(raw_order)
        accepted = bool(fill_decision["accepted"])
        failure_reason = str(fill_decision["reason"])
        terminal_no_fill = str(fill_decision["code"]) in {
            "ORDER_CREATE_REJECTED",
            "ORDER_CANCELLED",
            "ORDER_REJECTED",
        } and not fill_decision.get("requiresOperatorReview")

        if accepted:
            resolution_state = "RESOLVED_FILLED"
            queue_removal_pending = True
        elif terminal_no_fill:
            resolution_state = "RESOLVED_NO_FILL"
            queue_removal_pending = True
        else:
            resolution_state = "UNRESOLVED"
            queue_removal_pending = False

        claim = _transition_claim(
            store,
            claim,
            resolution_state,
            timestamp,
            resolvedAt=timestamp,
            fillDecision=fill_decision,
            queueRemovalPending=queue_removal_pending,
            requiresOperatorReview=bool(
                fill_decision.get("requiresOperatorReview")
            ),
        )
        _STATE["activeClaim"] = _claim_summary(claim)

        payload = {
            "candidate": candidate,
            "symbol": symbol,
            "signal": side,
            "result": order,
            "fillDecision": fill_decision,
            "durableClaim": _claim_summary(claim),
            "positionSizing": sizing,
            "pricePlan": plan,
        }
        core.get_bot_engine().journal.add("auto_order", payload)
        core.get_bot_engine().set_status("journal", "ok")

        updates = {
            "symbol": symbol,
            "selectedSignalSymbol": symbol,
            "lastSignal": side,
            "lastReason": (
                "Confirmed setup executed and final full fill verified"
                if accepted
                else failure_reason
            ),
            "lastOrder": order,
            "lastTradeAt": (
                timestamp
                if accepted
                else core.BOT_STATE.get("lastTradeAt", 0)
            ),
            "positionSizing": sizing,
            "executionGuard": {
                "ok": accepted,
                "reason": (
                    "Setup handoff and final full fill verified"
                    if accepted
                    else failure_reason
                ),
            },
        }
        if fill_decision.get("requiresOperatorReview"):
            updates["enabled"] = False

        with core.BOT_LOCK:
            core.BOT_STATE.update(updates)

        if resolution_state in _RESOLVED_CLAIM_STATES:
            completed = _complete_resolved_claim(
                core,
                setup_worker,
                store,
                claim,
                timestamp,
                recovery=False,
            )
            if isinstance(completed, dict) and completed.get("lastResult"):
                return completed
            claim = completed

        if accepted:
            return _record(
                core,
                _result(
                    "executed",
                    "ORDER_FILLED",
                    (
                        "Confirmed setup executed with mandatory final-fill "
                        "evidence after durable resolution."
                    ),
                    candidate,
                    order=order,
                    fillDecision=fill_decision,
                    durableClaim=_claim_summary(claim),
                    positionSizing=sizing,
                    pricePlan=plan,
                ),
                timestamp,
            )

        if terminal_no_fill:
            return _record(
                core,
                _result(
                    "error",
                    str(fill_decision["code"]),
                    failure_reason,
                    candidate,
                    order=order,
                    fillDecision=fill_decision,
                    durableClaim=_claim_summary(claim),
                    positionSizing=sizing,
                    pricePlan=plan,
                ),
                timestamp,
            )

        return _record(
            core,
            _result(
                "error",
                str(fill_decision["code"]),
                failure_reason,
                candidate,
                order=order,
                fillDecision=fill_decision,
                durableClaim=_claim_summary(claim),
                positionSizing=sizing,
                pricePlan=plan,
            ),
            timestamp,
        )
    except Exception as exc:
        reason = (
            "Exchange submission outcome is unknown after an execution handoff "
            f"exception: {exc}"
            if submission_started
            else f"Execution handoff failed before exchange submission: {exc}"
        )
        if store is not None and claim is not None:
            try:
                claim = _transition_claim(
                    store,
                    claim,
                    (
                        "SUBMISSION_UNKNOWN"
                        if submission_started
                        else "UNRESOLVED"
                    ),
                    timestamp,
                    requiresOperatorReview=True,
                    queueRemovalPending=False,
                    error=reason,
                )
                _STATE["activeClaim"] = _claim_summary(claim)
            except Exception:
                # The earlier durable CLAIMED record is deliberately left in
                # place. A later run treats it as unresolved and cannot resubmit.
                pass
        if submission_started:
            _disable_execution(core, reason)
        return _record(
            core,
            _result(
                "error",
                (
                    "SUBMISSION_OUTCOME_UNKNOWN"
                    if submission_started
                    else "HANDOFF_EXCEPTION"
                ),
                reason,
                dict((claim or {}).get("candidate") or {}),
                durableClaim=_claim_summary(claim),
            ),
            timestamp,
        )
    finally:
        _LOCK.release()


def snapshot_with_result(result: dict[str, Any]) -> dict[str, Any]:
    data = snapshot_unlocked()
    data["currentResult"] = result
    return data


def snapshot_unlocked() -> dict[str, Any]:
    return {**dict(_STATE), "settings": settings()}


def snapshot() -> dict[str, Any]:
    if _LOCK.locked():
        return snapshot_unlocked()
    with _LOCK:
        return snapshot_unlocked()
