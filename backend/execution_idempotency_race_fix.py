"""Prevent stale startup recovery from overwriting a newer active claim."""

from __future__ import annotations

from typing import Any

try:
    from . import execution_idempotency as idem
except ImportError:
    import execution_idempotency as idem

_ORIGINAL_ENSURE = None


def _same_owner(current: Any, expected: dict[str, Any]) -> bool:
    return (
        isinstance(current, dict)
        and str(current.get("claimId") or "") == str(expected.get("claimId") or "")
        and str(current.get("state") or "").upper()
        == str(expected.get("state") or "").upper()
    )


def install(core: Any, handoff: Any) -> dict[str, Any]:
    global _ORIGINAL_ENSURE
    if getattr(handoff, "_idempotency_race_fix_installed", False):
        return status(core, handoff)

    store = getattr(core, "_durable_state_store", None)
    if store is None or not callable(getattr(store, "compare_and_swap", None)):
        raise RuntimeError(
            "Restart-safe legacy claim enrichment requires atomic compare-and-swap storage."
        )

    _ORIGINAL_ENSURE = idem._ensure_claim_metadata

    def ensure_claim_metadata(
        runtime_handoff: Any,
        runtime_store: Any,
        claim: dict[str, Any],
        timestamp: int,
    ):
        active_key = str(runtime_handoff._ACTIVE_CLAIM_KEY)
        current = idem._read_record(runtime_store, active_key)
        if not _same_owner(current, claim):
            raise RuntimeError(
                "Legacy active claim ownership changed before idempotency enrichment."
            )

        candidate = dict(current.get("candidate") or claim.get("candidate") or {})
        candidate_key = str(current.get("candidateKey") or claim.get("candidateKey") or "").strip()
        if not candidate_key:
            candidate_key = idem._candidate_key(candidate)
        fingerprint = idem._fingerprint(candidate_key)
        order_link_id = idem._deterministic_order_link_id(candidate_key)
        ledger_key = idem._ledger_key(candidate_key)

        record = idem._read_record(runtime_store, ledger_key)
        if record is None:
            bootstrap = {
                "version": 1,
                "candidateKey": candidate_key,
                "fingerprint": fingerprint,
                "orderLinkId": order_link_id,
                "state": "RECOVERED_ACTIVE_CLAIM",
                "createdAt": int(
                    current.get("claimedAt") or current.get("updatedAt") or timestamp
                ),
                "updatedAt": int(timestamp),
                "candidate": candidate,
                "claimId": current.get("claimId"),
                "requiresOperatorReview": True,
                "recoveredLegacyClaim": True,
            }
            runtime_store.put_if_absent(ledger_key, bootstrap)
            record = idem._read_record(runtime_store, ledger_key)
        if record is None:
            raise RuntimeError("Idempotency ledger could not be initialized.")
        idem._verify_identity(
            record,
            candidate_key=candidate_key,
            fingerprint=fingerprint,
            order_link_id=order_link_id,
        )

        enriched = {
            **dict(current),
            "candidateKey": candidate_key,
            "candidateFingerprint": fingerprint,
            "idempotencyLedgerKey": ledger_key,
            "orderLinkId": order_link_id,
        }
        if enriched != current:
            if not runtime_store.compare_and_swap(active_key, current, enriched):
                raise RuntimeError(
                    "Legacy active claim ownership changed during idempotency enrichment."
                )
            saved = idem._read_record(runtime_store, active_key)
            if not _same_owner(saved, enriched):
                raise RuntimeError(
                    "Legacy active claim conditional enrichment was not committed."
                )
            enriched = saved
        return enriched, ledger_key, record

    idem._ensure_claim_metadata = ensure_claim_metadata
    handoff._idempotency_race_fix_installed = True
    core._execution_idempotency_race_fix_installed = True
    return status(core, handoff)


def restore_for_tests() -> None:
    """Restore the process-wide wrapper between isolated test fixtures."""
    global _ORIGINAL_ENSURE
    if _ORIGINAL_ENSURE is not None:
        idem._ensure_claim_metadata = _ORIGINAL_ENSURE
        _ORIGINAL_ENSURE = None


def status(core: Any, handoff: Any) -> dict[str, Any]:
    store = getattr(core, "_durable_state_store", None)
    return {
        "installed": bool(getattr(handoff, "_idempotency_race_fix_installed", False)),
        "conditionalActiveClaimUpdate": True,
        "atomicCompareAndSwap": callable(getattr(store, "compare_and_swap", None)),
        "failClosedOnOwnershipChange": True,
        "preservesConcurrentClaimFields": True,
    }
