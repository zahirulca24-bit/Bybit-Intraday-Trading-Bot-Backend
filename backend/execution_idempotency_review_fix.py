"""Close the cross-process and legacy-claim gaps found in PR #58 review.

This installer runs after :mod:`execution_idempotency` and before workers start.
It binds the exchange client order ID to the exact claim created by the current
handoff thread, verifies both the active claim and permanent ledger immediately
before order construction, and migrates any pre-existing active claim during
startup (including unresolved states that never transition automatically).
"""

from __future__ import annotations

import threading
import time
from typing import Any

try:
    from . import execution_idempotency as idem
except ImportError:
    import execution_idempotency as idem


_BINDING = threading.local()
_ORIGINAL_CREATE_CLAIM = None
_ORIGINAL_TRANSITION_CLAIM = None
_ORIGINAL_GENERATE_ORDER_LINK_ID = None


def _clear_binding() -> None:
    if hasattr(_BINDING, "claim"):
        delattr(_BINDING, "claim")


def _claim_binding(claim: Any) -> dict[str, str]:
    if not isinstance(claim, dict):
        raise RuntimeError("Submitting execution claim is invalid.")

    claim_id = str(claim.get("claimId") or "").strip()
    candidate_key = str(claim.get("candidateKey") or "").strip()
    fingerprint = str(claim.get("candidateFingerprint") or "").strip()
    ledger_key = str(claim.get("idempotencyLedgerKey") or "").strip()
    order_link_id = str(claim.get("orderLinkId") or "").strip()

    if not claim_id or not candidate_key:
        raise RuntimeError("Submitting claim lacks durable identity.")

    expected_fingerprint = idem._fingerprint(candidate_key)
    expected_ledger_key = idem._ledger_key(candidate_key)
    expected_order_link_id = idem._deterministic_order_link_id(candidate_key)

    if fingerprint != expected_fingerprint:
        raise RuntimeError("Submitting claim fingerprint failed identity validation.")
    if ledger_key != expected_ledger_key:
        raise RuntimeError("Submitting claim ledger key failed identity validation.")
    if order_link_id != expected_order_link_id:
        raise RuntimeError("Submitting claim orderLinkId failed identity validation.")

    return {
        "claimId": claim_id,
        "candidateKey": candidate_key,
        "fingerprint": fingerprint,
        "ledgerKey": ledger_key,
        "orderLinkId": order_link_id,
    }


def _verify_active_ownership(
    store: Any,
    handoff: Any,
    binding: dict[str, str],
) -> dict[str, Any]:
    active = idem._read_record(store, str(handoff._ACTIVE_CLAIM_KEY))
    if active is None:
        raise RuntimeError(
            "Setup-worker order id requested without the submitting active claim."
        )

    active_binding = _claim_binding(active)
    for key in ("claimId", "candidateKey", "fingerprint", "ledgerKey", "orderLinkId"):
        if active_binding[key] != binding[key]:
            raise RuntimeError(
                "Submitting claim no longer owns the durable active-claim slot."
            )

    if str(active.get("state") or "").upper() != "CLAIMED":
        raise RuntimeError(
            "Submitting claim is not in the pre-submission CLAIMED state."
        )
    return active


def _verify_ledger_ownership(
    store: Any,
    binding: dict[str, str],
) -> dict[str, Any]:
    record = idem._read_record(store, binding["ledgerKey"])
    if record is None:
        raise RuntimeError(
            "Permanent idempotency ledger is missing before order submission."
        )

    idem._verify_identity(
        record,
        candidate_key=binding["candidateKey"],
        fingerprint=binding["fingerprint"],
        order_link_id=binding["orderLinkId"],
    )
    if str(record.get("claimId") or "") != binding["claimId"]:
        raise RuntimeError(
            "Permanent idempotency ledger is owned by a different claim."
        )
    if str(record.get("state") or "").upper() != "CLAIMED":
        raise RuntimeError(
            "Permanent idempotency ledger is not in the CLAIMED state."
        )
    return record


def _bootstrap_existing_active_claim(
    store: Any,
    handoff: Any,
    timestamp: int,
) -> dict[str, Any] | None:
    active = idem._read_record(store, str(handoff._ACTIVE_CLAIM_KEY))
    if active is None:
        return None

    enriched, ledger_key, record = idem._ensure_claim_metadata(
        handoff,
        store,
        active,
        timestamp,
    )
    idem._sync_ledger(
        store,
        ledger_key,
        record,
        enriched,
        timestamp,
    )
    return enriched


def status(core: Any, handoff: Any) -> dict[str, Any]:
    return {
        "installed": bool(
            getattr(handoff, "_idempotency_review_fix_installed", False)
        ),
        "claimBoundOrderLinkId": True,
        "preSubmitLedgerVerification": True,
        "legacyActiveClaimBootstrap": True,
        "durableStoreInstalled": bool(
            getattr(core, "_durable_state_store", None) is not None
        ),
    }


def install(core: Any, handoff: Any) -> dict[str, Any]:
    """Install the reviewed fail-closed corrections exactly once."""
    global _ORIGINAL_CREATE_CLAIM
    global _ORIGINAL_TRANSITION_CLAIM
    global _ORIGINAL_GENERATE_ORDER_LINK_ID

    if getattr(handoff, "_idempotency_review_fix_installed", False):
        return status(core, handoff)
    if not getattr(handoff, "_restart_safe_idempotency_installed", False):
        raise RuntimeError(
            "Idempotency review fix requires restart-safe idempotency first."
        )

    store = getattr(core, "_durable_state_store", None)
    if store is None:
        raise RuntimeError("Idempotency review fix requires durable storage.")

    _ORIGINAL_CREATE_CLAIM = handoff._create_claim
    _ORIGINAL_TRANSITION_CLAIM = handoff._transition_claim
    _ORIGINAL_GENERATE_ORDER_LINK_ID = core.generate_order_link_id

    # This runs before workers start. It covers CLAIMED, SUBMITTED, UNRESOLVED,
    # SUBMISSION_UNKNOWN and resolved legacy claims that may never transition.
    _bootstrap_existing_active_claim(store, handoff, int(time.time()))

    def create_claim(
        runtime_store: Any,
        candidate: dict[str, Any],
        timestamp: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        _clear_binding()
        claim, conflict = _ORIGINAL_CREATE_CLAIM(
            runtime_store,
            candidate,
            timestamp,
        )
        if claim is not None:
            _BINDING.claim = _claim_binding(claim)
        return claim, conflict

    def transition_claim(
        runtime_store: Any,
        claim: dict[str, Any],
        state: str,
        timestamp: int,
        **changes: Any,
    ) -> dict[str, Any]:
        try:
            return _ORIGINAL_TRANSITION_CLAIM(
                runtime_store,
                claim,
                state,
                timestamp,
                **changes,
            )
        finally:
            binding = getattr(_BINDING, "claim", None)
            if isinstance(binding, dict) and (
                str(binding.get("claimId") or "")
                == str(claim.get("claimId") or "")
                and str(state or "").upper() != "CLAIMED"
            ):
                _clear_binding()

    def generate_order_link_id(source: Any) -> str:
        if not idem._setup_source(source):
            return _ORIGINAL_GENERATE_ORDER_LINK_ID(source)

        binding = getattr(_BINDING, "claim", None)
        try:
            if not isinstance(binding, dict):
                raise RuntimeError(
                    "Setup-worker order id requested without a bound submitting claim."
                )

            # Verify active-slot ownership and the permanent duplicate barrier.
            _verify_active_ownership(store, handoff, binding)
            _verify_ledger_ownership(store, binding)

            # Re-read the active claim after the ledger check. Another process
            # replacing the global active row cannot make this request use its ID.
            _verify_active_ownership(store, handoff, binding)
            return str(binding["orderLinkId"])
        finally:
            # One claim may construct only one setup entry order automatically.
            _clear_binding()

    handoff._create_claim = create_claim
    handoff._transition_claim = transition_claim
    core.generate_order_link_id = generate_order_link_id
    handoff._idempotency_review_fix_installed = True
    core._execution_idempotency_review_fix_installed = True
    core.execution_idempotency_review_fix_status = lambda: status(core, handoff)
    return status(core, handoff)
