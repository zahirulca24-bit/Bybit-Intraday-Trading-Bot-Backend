"""Durable Python-to-Node execution-command publisher.

Consumes sizing-ready rows and persists their immutable JSON payload to
PostgreSQL as AVAILABLE commands for the Node execution worker. PostgreSQL is
support infrastructure, not a trade-eligibility gate: persistence problems are
reported as WAIT/RETRY/DEGRADED and never reclassify an already risk-approved
trade as rejected.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Mapping

POLICY_ID = "PYTHON_NODE_EXECUTION_CONTRACT_V2_NONBLOCKING_SUPPORT"
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 2,
    "policyId": POLICY_ID,
    "source": "sizing_ready_postgresql_support_outbox",
    "inputFingerprint": None,
    "updatedAt": 0,
    "rows": [],
    "metrics": {},
    "lastError": None,
}


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 2),
        "policyId": str(_STATE.get("policyId") or POLICY_ID),
        "source": str(_STATE.get("source") or "sizing_ready_postgresql_support_outbox"),
        "inputFingerprint": _STATE.get("inputFingerprint"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "orderSubmissions": 0,
        "claimsCreatedByPython": 0,
        "supportOnly": True,
        "tradeRejectionAuthority": False,
    }


def snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return _snapshot_unlocked()


def _sizing_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "position_sizing_margin_status", None)
    if callable(reader):
        payload = reader()
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "position_sizing_margin", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _fingerprint(upstream: Mapping[str, Any]) -> str:
    base = str(upstream.get("inputFingerprint") or "")
    keys = sorted(
        str(row.get("candidateKey") or "")
        for row in upstream.get("approvedSizingQueue") or []
        if isinstance(row, dict) and row.get("candidateKey")
    )
    return f"{base}:{'|'.join(keys)}"


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _validation_error(candidate: Mapping[str, Any]) -> str | None:
    if not candidate.get("candidateKey"):
        return "candidateKey is required"
    if not candidate.get("sizingApproved"):
        return "Sizing output is not ready"
    if candidate.get("positionSizingStatus") != "SIZING_APPROVED":
        return "positionSizingStatus must be SIZING_APPROVED"
    if candidate.get("executionStatus") != "AWAITING_NODE_EXECUTION":
        return "executionStatus must be AWAITING_NODE_EXECUTION"
    if candidate.get("orderSubmitted") is not False:
        return "orderSubmitted must be false"
    if not str(candidate.get("symbol") or "").upper():
        return "symbol is required"
    if str(candidate.get("side") or "") not in {"Buy", "Sell"}:
        return "side must be Buy or Sell"
    for field in (
        "entryReference",
        "technicalStopLoss",
        "takeProfitReference",
        "qty",
        "requiredInitialMarginUsdt",
    ):
        if not _positive(candidate.get(field)):
            return f"{field} must be positive"
    if str(candidate.get("marginMode") or "").upper() != "ISOLATED":
        return "marginMode must be ISOLATED"
    try:
        leverage = int(candidate.get("leverage") or 0)
    except (TypeError, ValueError):
        leverage = 0
    if leverage <= 0 or leverage > 10:
        return "leverage must be between 1 and 10"
    requirements = candidate.get("nodeExecutionRequirements")
    if not isinstance(requirements, dict):
        return "nodeExecutionRequirements are required"
    if str(requirements.get("marginMode") or "").upper() != "ISOLATED":
        return "Node margin-mode requirement must be ISOLATED"
    try:
        required_leverage = int(requirements.get("leverage") or 0)
    except (TypeError, ValueError):
        required_leverage = 0
    if required_leverage <= 0 or required_leverage > 10:
        return "Node leverage requirement must be between 1 and 10"
    if requirements.get("revalidateWalletAndInstrumentRules") is not True:
        return "Node wallet/instrument revalidation is required"
    if requirements.get("submitOnlyAfterRevalidation") is not True:
        return "Node submission must require revalidation"
    return None


def _store(core: Any) -> tuple[Any | None, str | None]:
    store = getattr(core, "_durable_state_store", None)
    required = (
        "status",
        "publish_execution_command",
        "get_execution_command",
        "list_execution_commands",
        "claim_execution_command",
        "transition_execution_command",
    )
    if store is None or any(not callable(getattr(store, name, None)) for name in required):
        return None, "PostgreSQL execution-command store is not installed"
    try:
        status = dict(store.status() or {})
    except Exception as exc:
        return None, f"Execution-command store health check failed: {exc}"
    if (
        not status.get("ok")
        or status.get("degraded")
        or not status.get("restartSafe")
        or str(status.get("backend") or "").lower() != "postgresql"
    ):
        return None, "Healthy restart-safe PostgreSQL is temporarily unavailable"
    return store, None


def _support_wait(candidate: Mapping[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {
        "candidateKey": candidate.get("candidateKey"),
        "symbol": candidate.get("symbol"),
        "state": "WAIT_RETRY",
        "published": False,
        "code": code,
        "reason": reason,
        "orderSubmitted": False,
        "tradeRejected": False,
        "supportOnly": True,
    }


def build(
    core: Any,
    now: int | None = None,
    *,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist current sizing outputs without claiming or executing them."""
    timestamp = int(now or time.time())
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        source = dict(upstream or _sizing_snapshot(core))
        source_status = str(source.get("status") or "")
        if source_status not in {"ready", "empty", "degraded"}:
            raise RuntimeError("Position sizing snapshot is not ready")
        candidates = [
            dict(row)
            for row in source.get("approvedSizingQueue") or []
            if isinstance(row, dict)
        ]
        store, store_error = _store(core)

        rows: list[dict[str, Any]] = []
        published = 0
        duplicates = 0
        waiting = 0
        conflicts = 0

        if store is None:
            rows = [
                _support_wait(
                    candidate,
                    "OUTBOX_SUPPORT_UNAVAILABLE",
                    store_error or "PostgreSQL support outbox unavailable; retry without changing trade eligibility",
                )
                for candidate in candidates
            ]
            waiting = len(rows)
        else:
            for candidate in candidates:
                validation_error = _validation_error(candidate)
                if validation_error:
                    rows.append(
                        _support_wait(candidate, "EXECUTION_PAYLOAD_NOT_READY", validation_error)
                    )
                    waiting += 1
                    continue

                candidate_key = str(candidate["candidateKey"])
                try:
                    created = bool(
                        store.publish_execution_command(
                            candidate_key,
                            dict(candidate),
                            created_at=timestamp,
                        )
                    )
                    stored = store.get_execution_command(candidate_key)
                except Exception as exc:
                    rows.append(
                        _support_wait(
                            candidate,
                            "OUTBOX_PERSISTENCE_RETRY",
                            f"PostgreSQL support write/read failed and will retry: {exc}",
                        )
                    )
                    waiting += 1
                    continue

                if not isinstance(stored, dict):
                    rows.append(
                        _support_wait(
                            candidate,
                            "OUTBOX_PERSISTENCE_RETRY",
                            "Published command could not be reloaded; retry without changing trade eligibility",
                        )
                    )
                    waiting += 1
                    continue
                if _canonical(stored.get("payload") or {}) != _canonical(candidate):
                    rows.append(
                        _support_wait(
                            candidate,
                            "IMMUTABLE_PAYLOAD_CONFLICT",
                            "Candidate key already exists with a different immutable payload; operator reconciliation required",
                        )
                    )
                    waiting += 1
                    conflicts += 1
                    continue

                state = str(stored.get("state") or "")
                rows.append(
                    {
                        "candidateKey": candidate_key,
                        "symbol": candidate.get("symbol"),
                        "state": state,
                        "slotId": stored.get("slotId"),
                        "ownerId": stored.get("ownerId"),
                        "published": created,
                        "code": "COMMAND_PUBLISHED" if created else "COMMAND_ALREADY_EXISTS",
                        "reason": (
                            "Immutable execution command persisted as AVAILABLE"
                            if created
                            else "Existing immutable command retained without overwrite"
                        ),
                        "orderSubmitted": False,
                        "tradeRejected": False,
                        "supportOnly": True,
                    }
                )
                if created:
                    published += 1
                else:
                    duplicates += 1

        fingerprint = _fingerprint(source)
        metrics = {
            "sizingApprovedInput": len(candidates),
            "published": published,
            "idempotentDuplicates": duplicates,
            "waitingRetry": waiting,
            "blocked": 0,
            "immutableConflicts": conflicts,
            "claimOperations": 0,
            "orderSubmissions": 0,
            "maximumNodeSlots": 3,
            "automaticClaimExpiry": False,
            "policy": "POSTGRESQL_SUPPORT_ONLY_NON_REJECTION_OUTBOX",
            "tradeRejectionAuthority": False,
        }
        degraded = waiting > 0
        payload = {
            "status": "degraded" if degraded else ("ready" if rows else "empty"),
            "version": 2,
            "policyId": POLICY_ID,
            "source": "sizing_ready_postgresql_support_outbox",
            "inputFingerprint": fingerprint,
            "updatedAt": timestamp,
            "rows": rows,
            "metrics": metrics,
            "lastError": (
                "PostgreSQL support persistence requires retry/reconciliation; trade eligibility is unchanged"
                if degraded
                else None
            ),
        }
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            _STATE.update(
                {
                    "status": "degraded",
                    "updatedAt": timestamp,
                    "lastError": f"Support outbox unavailable: {exc}",
                }
            )
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(core: Any) -> bool:
    upstream = _sizing_snapshot(core)
    fingerprint = _fingerprint(upstream)
    with _STATE_LOCK:
        return bool(
            fingerprint != str(_STATE.get("inputFingerprint") or "")
            or str(_STATE.get("status") or "") in {"error", "degraded"}
        )


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    if not due(core):
        return snapshot()
    upstream = _sizing_snapshot(core)
    return build(core, now=now, upstream=upstream)


def install(core: Any) -> dict[str, Any]:
    if getattr(core, "_execution_command_outbox_v1_installed", False):
        return status(core)
    core.execution_command_outbox = (
        lambda force=False: build(core) if force else ensure_current(core)
    )
    core.execution_command_outbox_status = snapshot
    setattr(core, "_execution_command_outbox_v1_installed", True)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    return {
        "installed": bool(
            core is not None
            and getattr(core, "_execution_command_outbox_v1_installed", False)
        ),
        "policyId": POLICY_ID,
        "immutablePayload": True,
        "maximumNodeSlots": 3,
        "maximumLeverage": 10,
        "usesPostgresSkipLocked": True,
        "automaticClaimExpiry": False,
        "claimsCommands": False,
        "submitsOrder": False,
        "supportOnly": True,
        "tradeRejectionAuthority": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 2,
                "policyId": POLICY_ID,
                "source": "sizing_ready_postgresql_support_outbox",
                "inputFingerprint": None,
                "updatedAt": 0,
                "rows": [],
                "metrics": {},
                "lastError": None,
            }
        )
