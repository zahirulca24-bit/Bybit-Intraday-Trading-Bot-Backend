"""Post-merge safety corrections for the durable execution handoff.

This bounded installer fixes two crash-safety defects found during the independent
review of PR #56:

* transport failures returned as local sentinel responses must remain unresolved;
* a recovered RESOLVED_FILLED claim must restore journal and BOT_STATE accounting
  before the claim is archived and removed.
"""

from __future__ import annotations

from typing import Any


_TRANSPORT_UNKNOWN_CODES = {-2}


def _integer(value: Any, default: int = -999999) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_transport_unknown(order: Any) -> bool:
    if not isinstance(order, dict):
        return False
    if _integer(order.get("retCode")) in _TRANSPORT_UNKNOWN_CODES:
        return True
    return order.get("transportError") is True or str(
        order.get("errorType") or ""
    ).upper() in {"TRANSPORT", "TIMEOUT", "CONNECTION"}


def _journal_entries(engine: Any) -> list[dict[str, Any]]:
    journal = getattr(engine, "journal", None)
    if journal is None:
        return []
    entries = getattr(journal, "entries", None)
    if isinstance(entries, list):
        return entries
    if isinstance(journal, list):
        return journal
    return []


def _journal_has_claim(engine: Any, claim_id: str) -> bool:
    for entry in _journal_entries(engine):
        if entry.get("event") != "auto_order":
            continue
        payload = entry.get("payload") or {}
        durable = payload.get("durableClaim") or {}
        if str(durable.get("claimId") or "") == claim_id:
            return True
        if str(payload.get("recoveryClaimId") or "") == claim_id:
            return True
    return False


def _replay_filled_accounting(
    core: Any,
    handoff: Any,
    claim: dict[str, Any],
    timestamp: int,
) -> bool:
    claim_id = str(claim.get("claimId") or "")
    candidate = dict(claim.get("candidate") or {})
    order = claim.get("orderResponse")
    fill_decision = claim.get("fillDecision")

    if not claim_id:
        raise RuntimeError("Recovered filled claim has no claimId.")
    if not isinstance(order, dict) or _integer(order.get("retCode")) != 0:
        raise RuntimeError("Recovered filled claim has no accepted order response.")
    if not isinstance(fill_decision, dict) or fill_decision.get("accepted") is not True:
        raise RuntimeError("Recovered filled claim has no accepted fill decision.")

    symbol = str(candidate.get("symbol") or "").upper()
    side = str(candidate.get("side") or "")
    if not symbol or side not in {"Buy", "Sell"}:
        raise RuntimeError("Recovered filled claim has invalid candidate identity.")

    engine = core.get_bot_engine()
    added = False
    if not _journal_has_claim(engine, claim_id):
        payload = {
            "candidate": candidate,
            "symbol": symbol,
            "signal": side,
            "result": order,
            "fillDecision": fill_decision,
            "durableClaim": handoff._claim_summary(claim),
            "recovery": True,
            "recoveryClaimId": claim_id,
        }
        engine.journal.add("auto_order", payload)
        added = True
    engine.set_status("journal", "ok")

    trade_at = int(
        claim.get("resolvedAt")
        or claim.get("submittedAt")
        or claim.get("claimedAt")
        or timestamp
    )
    with core.BOT_LOCK:
        core.BOT_STATE.update(
            {
                "symbol": symbol,
                "selectedSignalSymbol": symbol,
                "lastSignal": side,
                "lastReason": (
                    "Recovered durable filled-order accounting before claim completion"
                ),
                "lastOrder": order,
                "lastTradeAt": trade_at,
                "executionGuard": {
                    "ok": True,
                    "reason": (
                        "Recovered final-fill evidence was restored to durable "
                        "journal and runtime accounting"
                    ),
                },
            }
        )
    return added


def install(core: Any, handoff: Any) -> None:
    """Install the bounded post-merge corrections exactly once."""
    if getattr(handoff, "_post_merge_p0_03_hotfix_installed", False):
        return

    original_fill_decision = handoff._mandatory_fill_decision
    original_complete_resolved_claim = handoff._complete_resolved_claim

    def mandatory_fill_decision(order: Any) -> dict[str, Any]:
        if _is_transport_unknown(order):
            return {
                "accepted": False,
                "code": "ORDER_SUBMISSION_TRANSPORT_UNKNOWN",
                "reason": str(
                    (order or {}).get("retMsg")
                    or "Order transport failed after submission began; exchange outcome is unknown."
                ),
                "requiresOperatorReview": True,
                "verification": (order or {}).get("fillVerification"),
                "transportUnknown": True,
            }
        return original_fill_decision(order)

    def complete_resolved_claim(
        runtime_core: Any,
        setup_worker: Any,
        store: Any,
        claim: dict[str, Any],
        timestamp: int,
        *,
        recovery: bool,
    ) -> dict[str, Any]:
        state = str(claim.get("state") or "").upper()
        if recovery and state == "RESOLVED_FILLED":
            try:
                journal_added = _replay_filled_accounting(
                    runtime_core, handoff, claim, timestamp
                )
                claim = handoff._transition_claim(
                    store,
                    claim,
                    "RESOLVED_FILLED",
                    timestamp,
                    recoveryAccountingAppliedAt=timestamp,
                    recoveryAccountingJournalAdded=journal_added,
                    requiresOperatorReview=False,
                )
            except Exception as exc:
                reason = (
                    "Recovered filled claim accounting could not be restored; "
                    f"claim remains active and execution is blocked: {exc}"
                )
                handoff._disable_execution(runtime_core, reason)
                return handoff._record(
                    runtime_core,
                    handoff._result(
                        "error",
                        "RECOVERY_ACCOUNTING_FAILED",
                        reason,
                        dict(claim.get("candidate") or {}),
                        durableClaim=handoff._claim_summary(claim),
                    ),
                    timestamp,
                )

        return original_complete_resolved_claim(
            runtime_core,
            setup_worker,
            store,
            claim,
            timestamp,
            recovery=recovery,
        )

    handoff._mandatory_fill_decision = mandatory_fill_decision
    handoff._complete_resolved_claim = complete_resolved_claim
    handoff._post_merge_p0_03_hotfix_installed = True
    core._execution_handoff_p0_03_hotfix_installed = True
