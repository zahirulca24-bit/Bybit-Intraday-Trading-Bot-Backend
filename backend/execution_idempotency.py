"""Restart-safe candidate and exchange-order idempotency for the execution handoff.

The canonical handoff already keeps one active durable claim. This installer adds a
permanent candidate ledger and a deterministic Bybit ``orderLinkId`` so the same
closed-candle candidate cannot be submitted again after process or host restart.

Safety properties:

* reserve the candidate fingerprint durably before creating the active claim;
* keep every terminal and unresolved ledger record instead of reusing the key;
* derive the exchange client order id only from the candidate key;
* fail closed when a reservation, ledger transition, or identity check is unsafe.
"""

from __future__ import annotations

import hashlib
from typing import Any

_LEDGER_PREFIX = "execution_idempotency_v1:"
_ORDER_LINK_PREFIX = "cdx-idem-"
_MAX_CANDIDATE_KEY_LENGTH = 512

_ORIGINAL_CREATE_CLAIM = None
_ORIGINAL_TRANSITION_CLAIM = None
_ORIGINAL_GENERATE_ORDER_LINK_ID = None


def _candidate_key(candidate: Any) -> str:
    if not isinstance(candidate, dict):
        raise RuntimeError("Execution candidate is not a mapping.")
    key = str(candidate.get("candidateKey") or "").strip()
    if not key:
        raise RuntimeError("Execution candidate has no candidateKey.")
    if len(key) > _MAX_CANDIDATE_KEY_LENGTH:
        raise RuntimeError("Execution candidateKey exceeds the safety limit.")
    return key


def _fingerprint(candidate_key: str) -> str:
    return hashlib.sha256(candidate_key.encode("utf-8")).hexdigest()


def _ledger_key(candidate_key: str) -> str:
    return f"{_LEDGER_PREFIX}{_fingerprint(candidate_key)}"


def _deterministic_order_link_id(candidate_key: str) -> str:
    # 9-character prefix + 27 hexadecimal characters = Bybit's 36-char limit.
    return f"{_ORDER_LINK_PREFIX}{_fingerprint(candidate_key)[:27]}"


def _read_record(store: Any, key: str) -> dict[str, Any] | None:
    value = store.get(key)
    return dict(value) if isinstance(value, dict) else None


def _verify_identity(
    record: dict[str, Any],
    *,
    candidate_key: str,
    fingerprint: str,
    order_link_id: str,
) -> None:
    if str(record.get("candidateKey") or "") != candidate_key:
        raise RuntimeError("Idempotency ledger candidate identity mismatch.")
    if str(record.get("fingerprint") or "") != fingerprint:
        raise RuntimeError("Idempotency ledger fingerprint mismatch.")
    if str(record.get("orderLinkId") or "") != order_link_id:
        raise RuntimeError("Idempotency ledger orderLinkId mismatch.")


def _write_record(
    store: Any,
    ledger_key: str,
    record: dict[str, Any],
) -> dict[str, Any]:
    store.put(ledger_key, record)
    saved = _read_record(store, ledger_key)
    if saved is None:
        raise RuntimeError("Idempotency ledger record could not be reloaded.")
    _verify_identity(
        saved,
        candidate_key=str(record["candidateKey"]),
        fingerprint=str(record["fingerprint"]),
        order_link_id=str(record["orderLinkId"]),
    )
    if str(saved.get("state") or "") != str(record.get("state") or ""):
        raise RuntimeError("Idempotency ledger state was not committed.")
    return saved


def _reservation(
    candidate: dict[str, Any],
    timestamp: int,
) -> tuple[str, dict[str, Any]]:
    candidate_key = _candidate_key(candidate)
    fingerprint = _fingerprint(candidate_key)
    order_link_id = _deterministic_order_link_id(candidate_key)
    ledger_key = f"{_LEDGER_PREFIX}{fingerprint}"
    record = {
        "version": 1,
        "candidateKey": candidate_key,
        "fingerprint": fingerprint,
        "orderLinkId": order_link_id,
        "state": "RESERVED",
        "createdAt": int(timestamp),
        "updatedAt": int(timestamp),
        "candidate": dict(candidate),
        "requiresOperatorReview": False,
    }
    return ledger_key, record


def _conflict_claim(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
    timestamp: int,
) -> dict[str, Any]:
    candidate_key = _candidate_key(candidate)
    fingerprint = _fingerprint(candidate_key)
    order_link_id = _deterministic_order_link_id(candidate_key)
    record = dict(existing or {})
    if record:
        _verify_identity(
            record,
            candidate_key=candidate_key,
            fingerprint=fingerprint,
            order_link_id=order_link_id,
        )
    ledger_state = str(record.get("state") or "UNKNOWN").upper()
    return {
        "version": 1,
        "claimId": str(record.get("claimId") or f"idem-{fingerprint[:16]}"),
        "candidateKey": candidate_key,
        "candidate": dict(candidate),
        "state": f"IDEMPOTENCY_{ledger_state}",
        "claimedAt": int(record.get("createdAt") or timestamp),
        "updatedAt": int(record.get("updatedAt") or timestamp),
        "queueRemovalPending": False,
        "requiresOperatorReview": True,
        "idempotencyLedgerKey": f"{_LEDGER_PREFIX}{fingerprint}",
        "orderLinkId": order_link_id,
    }


def _ensure_claim_metadata(
    handoff: Any,
    store: Any,
    claim: dict[str, Any],
    timestamp: int,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    candidate = dict(claim.get("candidate") or {})
    candidate_key = str(claim.get("candidateKey") or "").strip()
    if not candidate_key:
        candidate_key = _candidate_key(candidate)
    fingerprint = _fingerprint(candidate_key)
    order_link_id = _deterministic_order_link_id(candidate_key)
    ledger_key = f"{_LEDGER_PREFIX}{fingerprint}"

    record = _read_record(store, ledger_key)
    if record is None:
        bootstrap = {
            "version": 1,
            "candidateKey": candidate_key,
            "fingerprint": fingerprint,
            "orderLinkId": order_link_id,
            "state": "RECOVERED_ACTIVE_CLAIM",
            "createdAt": int(
                claim.get("claimedAt")
                or claim.get("updatedAt")
                or timestamp
            ),
            "updatedAt": int(timestamp),
            "candidate": candidate,
            "claimId": claim.get("claimId"),
            "requiresOperatorReview": True,
            "recoveredLegacyClaim": True,
        }
        if not bool(store.put_if_absent(ledger_key, bootstrap)):
            record = _read_record(store, ledger_key)
        else:
            record = _read_record(store, ledger_key)
    if record is None:
        raise RuntimeError("Idempotency ledger could not be initialized.")
    _verify_identity(
        record,
        candidate_key=candidate_key,
        fingerprint=fingerprint,
        order_link_id=order_link_id,
    )

    enriched = {
        **dict(claim),
        "candidateKey": candidate_key,
        "candidateFingerprint": fingerprint,
        "idempotencyLedgerKey": ledger_key,
        "orderLinkId": order_link_id,
    }
    if enriched != claim:
        enriched = handoff._persist_claim(store, enriched)
    return enriched, ledger_key, record


def _terminal_outcome(claim: dict[str, Any]) -> str | None:
    fill = claim.get("fillDecision")
    if not isinstance(fill, dict):
        return None
    if fill.get("accepted") is True:
        return "FILLED"
    code = str(fill.get("code") or "").upper()
    if code in {"ORDER_CREATE_REJECTED", "ORDER_CANCELLED", "ORDER_REJECTED"}:
        return "NO_FILL"
    return None


def _sync_ledger(
    store: Any,
    ledger_key: str,
    current: dict[str, Any],
    claim: dict[str, Any],
    timestamp: int,
) -> dict[str, Any]:
    candidate_key = str(claim.get("candidateKey") or "")
    fingerprint = str(claim.get("candidateFingerprint") or _fingerprint(candidate_key))
    order_link_id = str(
        claim.get("orderLinkId")
        or _deterministic_order_link_id(candidate_key)
    )
    _verify_identity(
        current,
        candidate_key=candidate_key,
        fingerprint=fingerprint,
        order_link_id=order_link_id,
    )

    state = str(claim.get("state") or "UNKNOWN").upper()
    updated = {
        **current,
        "state": state,
        "updatedAt": int(timestamp),
        "claimId": claim.get("claimId"),
        "claimedAt": claim.get("claimedAt"),
        "submittedAt": claim.get("submittedAt"),
        "resolvedAt": claim.get("resolvedAt"),
        "completedAt": claim.get("completedAt"),
        "requiresOperatorReview": bool(claim.get("requiresOperatorReview")),
        "queueRemovalPending": bool(claim.get("queueRemovalPending")),
    }
    outcome = _terminal_outcome(claim)
    if outcome:
        updated["terminalOutcome"] = outcome

    order_response = claim.get("orderResponse")
    if isinstance(order_response, dict):
        result = order_response.get("result")
        if isinstance(result, dict):
            updated["exchangeOrderId"] = result.get("orderId")
            updated["exchangeOrderLinkId"] = (
                result.get("orderLinkId") or order_link_id
            )
        updated["exchangeRetCode"] = order_response.get("exchangeRetCode")
        updated["responseRetCode"] = order_response.get("retCode")

    return _write_record(store, ledger_key, updated)


def _setup_source(source: Any) -> bool:
    normalized = "".join(ch.lower() for ch in str(source or "") if ch.isalnum())
    return normalized == "setupworker"


def status(core: Any, handoff: Any) -> dict[str, Any]:
    return {
        "installed": bool(
            getattr(handoff, "_restart_safe_idempotency_installed", False)
        ),
        "permanentCandidateLedger": True,
        "deterministicOrderLinkId": True,
        "ledgerPrefix": _LEDGER_PREFIX,
        "durableStoreInstalled": bool(
            getattr(core, "_durable_state_store", None) is not None
        ),
    }


def install(core: Any, handoff: Any) -> dict[str, Any]:
    """Install permanent candidate and deterministic order idempotency once."""
    global _ORIGINAL_CREATE_CLAIM
    global _ORIGINAL_TRANSITION_CLAIM
    global _ORIGINAL_GENERATE_ORDER_LINK_ID

    if getattr(handoff, "_restart_safe_idempotency_installed", False):
        return status(core, handoff)

    store = getattr(core, "_durable_state_store", None)
    if store is None:
        raise RuntimeError(
            "Restart-safe idempotency requires the durable state runtime."
        )
    for name in ("get", "put", "put_if_absent", "status"):
        if not callable(getattr(store, name, None)):
            raise RuntimeError(
                "Restart-safe idempotency requires a complete durable state store."
            )

    _ORIGINAL_CREATE_CLAIM = handoff._create_claim
    _ORIGINAL_TRANSITION_CLAIM = handoff._transition_claim
    _ORIGINAL_GENERATE_ORDER_LINK_ID = core.generate_order_link_id

    def create_claim(
        runtime_store: Any,
        candidate: dict[str, Any],
        timestamp: int,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        ledger_key, reservation = _reservation(candidate, timestamp)
        created = bool(
            runtime_store.put_if_absent(ledger_key, reservation)
        )
        if not created:
            return (
                None,
                _conflict_claim(
                    _read_record(runtime_store, ledger_key),
                    candidate,
                    timestamp,
                ),
            )

        saved_reservation = _read_record(runtime_store, ledger_key)
        if saved_reservation is None:
            raise RuntimeError(
                "Candidate idempotency reservation was not committed."
            )
        _verify_identity(
            saved_reservation,
            candidate_key=str(reservation["candidateKey"]),
            fingerprint=str(reservation["fingerprint"]),
            order_link_id=str(reservation["orderLinkId"]),
        )

        try:
            claim, conflict = _ORIGINAL_CREATE_CLAIM(
                runtime_store, candidate, timestamp
            )
        except Exception:
            failed = {
                **saved_reservation,
                "state": "CLAIM_CREATION_ERROR",
                "updatedAt": int(timestamp),
                "requiresOperatorReview": True,
            }
            _write_record(runtime_store, ledger_key, failed)
            raise

        if claim is None:
            blocked = {
                **saved_reservation,
                "state": "ACTIVE_CLAIM_CONFLICT",
                "updatedAt": int(timestamp),
                "requiresOperatorReview": True,
            }
            _write_record(runtime_store, ledger_key, blocked)
            return None, conflict or _conflict_claim(
                blocked, candidate, timestamp
            )

        enriched = {
            **dict(claim),
            "candidateFingerprint": reservation["fingerprint"],
            "idempotencyLedgerKey": ledger_key,
            "orderLinkId": reservation["orderLinkId"],
        }
        enriched = handoff._persist_claim(runtime_store, enriched)
        claimed_record = {
            **saved_reservation,
            "state": "CLAIMED",
            "updatedAt": int(timestamp),
            "claimId": enriched.get("claimId"),
            "claimedAt": enriched.get("claimedAt"),
        }
        _write_record(runtime_store, ledger_key, claimed_record)
        return enriched, None

    def transition_claim(
        runtime_store: Any,
        claim: dict[str, Any],
        state: str,
        timestamp: int,
        **changes: Any,
    ) -> dict[str, Any]:
        updated = _ORIGINAL_TRANSITION_CLAIM(
            runtime_store,
            claim,
            state,
            timestamp,
            **changes,
        )
        updated, ledger_key, record = _ensure_claim_metadata(
            handoff,
            runtime_store,
            updated,
            timestamp,
        )
        _sync_ledger(
            runtime_store,
            ledger_key,
            record,
            updated,
            timestamp,
        )
        return updated

    def generate_order_link_id(source: Any) -> str:
        if _setup_source(source):
            active = _read_record(
                store,
                str(handoff._ACTIVE_CLAIM_KEY),
            )
            if active is None:
                raise RuntimeError(
                    "Setup-worker order id requested without a durable active claim."
                )
            candidate_key = str(active.get("candidateKey") or "").strip()
            order_link_id = str(active.get("orderLinkId") or "")
            if not candidate_key or not order_link_id:
                raise RuntimeError(
                    "Durable active claim lacks idempotent order identity."
                )
            expected = _deterministic_order_link_id(candidate_key)
            if order_link_id != expected:
                raise RuntimeError(
                    "Durable active claim orderLinkId failed identity validation."
                )
            return order_link_id
        return _ORIGINAL_GENERATE_ORDER_LINK_ID(source)

    handoff._create_claim = create_claim
    handoff._transition_claim = transition_claim
    core.generate_order_link_id = generate_order_link_id
    handoff._restart_safe_idempotency_installed = True
    core._execution_idempotency_installed = True
    core.execution_idempotency_status = lambda: status(core, handoff)
    return status(core, handoff)
