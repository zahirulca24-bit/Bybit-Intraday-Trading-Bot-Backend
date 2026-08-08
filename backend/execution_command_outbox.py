"""Direct risk-approved Python-to-Node handoff plus PostgreSQL support mirroring.

The canonical automatic execution transport is an authenticated HTTP delivery of
Entry-Safety-approved candidates directly to the Node execution service. Python
quantity/margin calculations are diagnostic only and are never prerequisites for
that delivery. PostgreSQL execution commands remain best-effort support/history
for compatibility and reconciliation.

Python never submits a Bybit order from this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Mapping

POLICY_ID = "DIRECT_NODE_HANDOFF_V1_POSTGRES_SUPPORT"
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_DELIVERED: dict[str, str] = {}

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 3,
    "policyId": POLICY_ID,
    "source": "risk_approved_direct_node_handoff_with_postgresql_support",
    "inputFingerprint": None,
    "updatedAt": 0,
    "rows": [],
    "metrics": {},
    "nodeHandoff": {
        "status": "WAIT",
        "delivered": 0,
        "retrying": 0,
        "rejectedInvalid": 0,
        "rows": [],
        "lastError": None,
    },
    "postgresSupport": {
        "status": "WAIT_RETRY",
        "published": 0,
        "waitingRetry": 0,
        "rows": [],
        "lastError": None,
    },
    "lastError": None,
}


def _snapshot_unlocked(status_override: str | None = None) -> dict[str, Any]:
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 3),
        "policyId": str(_STATE.get("policyId") or POLICY_ID),
        "source": str(
            _STATE.get("source")
            or "risk_approved_direct_node_handoff_with_postgresql_support"
        ),
        "inputFingerprint": _STATE.get("inputFingerprint"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "metrics": dict(_STATE.get("metrics") or {}),
        "nodeHandoff": dict(_STATE.get("nodeHandoff") or {}),
        "postgresSupport": dict(_STATE.get("postgresSupport") or {}),
        "lastError": _STATE.get("lastError"),
        "orderSubmissions": 0,
        "claimsCreatedByPython": 0,
        "supportOnly": False,
        "postgresSupportOnly": True,
        "tradeRejectionAuthority": False,
    }


def snapshot() -> dict[str, Any]:
    with _STATE_LOCK:
        return _snapshot_unlocked()


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


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(risk: Mapping[str, Any], sizing: Mapping[str, Any]) -> str:
    risk_base = str(risk.get("inputFingerprint") or "")
    risk_keys = sorted(
        str(row.get("candidateKey") or "")
        for row in risk.get("approvedRiskQueue") or []
        if isinstance(row, dict) and row.get("candidateKey")
    )
    sizing_base = str(sizing.get("inputFingerprint") or "")
    sizing_keys = sorted(
        str(row.get("candidateKey") or "")
        for row in sizing.get("approvedSizingQueue") or []
        if isinstance(row, dict) and row.get("candidateKey")
    )
    return f"risk={risk_base}:{'|'.join(risk_keys)};sizing={sizing_base}:{'|'.join(sizing_keys)}"


def _positive(value: Any) -> bool:
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def _handoff_payload(candidate: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    grade = str(candidate.get("grade") or "")
    if candidate.get("riskApproved") is not True or candidate.get("riskStatus") != "APPROVED_RISK":
        return None, "Candidate is not Entry-Safety approved"
    if grade not in {"A+", "A"}:
        return None, "Only A+ and A candidates are executable"
    if candidate.get("orderSubmitted") is not False:
        return None, "Candidate order state is not unsubmitted"
    candidate_key = str(candidate.get("candidateKey") or "").strip()
    symbol = str(candidate.get("symbol") or "").strip().upper()
    side = str(candidate.get("side") or "")
    if not candidate_key or not symbol or side not in {"Buy", "Sell"}:
        return None, "Candidate identity is invalid"
    if not _positive(candidate.get("entryReference")):
        return None, "entryReference must be positive"
    payload = {
        "candidateKey": candidate_key,
        "symbol": symbol,
        "side": side,
        "strategy": candidate.get("strategy"),
        "grade": grade,
        "gradeScore": candidate.get("gradeScore"),
        "entryReference": candidate.get("entryReference"),
        "entryFiveMinuteCandleTime": candidate.get("entryFiveMinuteCandleTime"),
        "setupFifteenMinuteCandleTime": candidate.get("setupFifteenMinuteCandleTime"),
        "createdAt": candidate.get("createdAt"),
        "riskApproved": True,
        "riskStatus": "APPROVED_RISK",
        "riskPerTradePct": 1.0,
        "gradeRiskPct": 1.0,
        "effectiveRiskPerTradePct": 1.0,
        "executionStatus": "AWAITING_NODE_EXECUTION",
        "orderSubmitted": False,
        "qualified": True,
    }
    return payload, None


def _handoff_settings() -> tuple[str, str, float]:
    url = str(os.environ.get("NODE_EXECUTION_URL") or "").strip().rstrip("/")
    token = str(os.environ.get("NODE_HANDOFF_TOKEN") or "").strip()
    try:
        timeout = float(os.environ.get("NODE_HANDOFF_TIMEOUT_SECONDS", "5"))
    except (TypeError, ValueError):
        timeout = 5.0
    return url, token, max(1.0, min(20.0, timeout))


def _post_candidate(payload: Mapping[str, Any], url: str, token: str, timeout: float) -> dict[str, Any]:
    body = json.dumps(dict(payload), separators=(",", ":"), default=str).encode("utf-8")
    request = urllib.request.Request(
        f"{url}/internal/execution-candidate",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Node-Handoff-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            decoded = json.loads(raw) if raw else {}
            return {
                "ok": 200 <= int(response.status) < 300,
                "statusCode": int(response.status),
                "body": decoded if isinstance(decoded, dict) else {},
            }
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw) if raw else {}
        except Exception:
            decoded = {}
        return {
            "ok": False,
            "statusCode": int(exc.code),
            "body": decoded if isinstance(decoded, dict) else {},
            "reason": str((decoded or {}).get("error") or exc.reason or exc),
        }


def _deliver_direct(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    url, token, timeout = _handoff_settings()
    rows: list[dict[str, Any]] = []
    delivered = retrying = invalid = duplicates = 0
    last_error: str | None = None

    for candidate in candidates:
        payload, validation_error = _handoff_payload(candidate)
        candidate_key = str(candidate.get("candidateKey") or "")
        if payload is None:
            invalid += 1
            rows.append(
                {
                    "candidateKey": candidate_key or None,
                    "symbol": candidate.get("symbol"),
                    "state": "INVALID",
                    "code": "NODE_HANDOFF_INVALID",
                    "reason": validation_error,
                    "tradeRejected": False,
                }
            )
            continue

        canonical = _canonical(payload)
        if _DELIVERED.get(candidate_key) == canonical:
            delivered += 1
            duplicates += 1
            rows.append(
                {
                    "candidateKey": candidate_key,
                    "symbol": payload["symbol"],
                    "state": "DELIVERED",
                    "code": "NODE_HANDOFF_ALREADY_DELIVERED",
                    "reason": "Candidate was already delivered idempotently",
                    "tradeRejected": False,
                }
            )
            continue

        if not url or not token:
            retrying += 1
            last_error = "NODE_EXECUTION_URL or NODE_HANDOFF_TOKEN is not configured"
            rows.append(
                {
                    "candidateKey": candidate_key,
                    "symbol": payload["symbol"],
                    "state": "NODE_HANDOFF_RETRY",
                    "code": "NODE_HANDOFF_CONFIG_WAIT",
                    "reason": last_error,
                    "tradeRejected": False,
                }
            )
            continue

        try:
            result = _post_candidate(payload, url, token, timeout)
        except Exception as exc:
            result = {"ok": False, "statusCode": 0, "reason": str(exc), "body": {}}

        if result.get("ok"):
            _DELIVERED[candidate_key] = canonical
            delivered += 1
            body = dict(result.get("body") or {})
            rows.append(
                {
                    "candidateKey": candidate_key,
                    "symbol": payload["symbol"],
                    "state": "DELIVERED",
                    "code": str(body.get("code") or "NODE_HANDOFF_DELIVERED"),
                    "reason": str(body.get("reason") or "Risk-approved candidate delivered to Node"),
                    "nodeState": body.get("state"),
                    "duplicate": bool(body.get("duplicate")),
                    "tradeRejected": False,
                }
            )
        else:
            retrying += 1
            body = dict(result.get("body") or {})
            last_error = str(
                result.get("reason")
                or body.get("error")
                or body.get("reason")
                or f"Node handoff HTTP {result.get('statusCode')}"
            )
            rows.append(
                {
                    "candidateKey": candidate_key,
                    "symbol": payload["symbol"],
                    "state": "NODE_HANDOFF_RETRY",
                    "code": "NODE_HANDOFF_RETRY",
                    "reason": last_error,
                    "statusCode": result.get("statusCode"),
                    "tradeRejected": False,
                }
            )

    status = "PASS" if candidates and retrying == 0 and invalid == 0 else (
        "DEGRADED" if retrying or invalid else "WAIT"
    )
    return {
        "status": status,
        "delivered": delivered,
        "retrying": retrying,
        "rejectedInvalid": invalid,
        "idempotentDuplicates": duplicates,
        "rows": rows,
        "lastError": last_error,
        "tradeRejectionAuthority": False,
    }


def _validation_error(candidate: Mapping[str, Any]) -> str | None:
    if not candidate.get("candidateKey"):
        return "candidateKey is required"
    if not candidate.get("sizingApproved"):
        return "Sizing diagnostic output is not ready"
    if candidate.get("positionSizingStatus") != "SIZING_APPROVED":
        return "positionSizingStatus must be SIZING_APPROVED for PostgreSQL compatibility mirroring"
    if candidate.get("executionStatus") != "AWAITING_NODE_EXECUTION":
        return "executionStatus must be AWAITING_NODE_EXECUTION"
    if candidate.get("orderSubmitted") is not False:
        return "orderSubmitted must be false"
    if not str(candidate.get("symbol") or "").upper():
        return "symbol is required"
    if str(candidate.get("side") or "") not in {"Buy", "Sell"}:
        return "side must be Buy or Sell"
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


def _mirror_postgres(core: Any, candidates: list[dict[str, Any]], timestamp: int) -> dict[str, Any]:
    store, store_error = _store(core)
    rows: list[dict[str, Any]] = []
    published = duplicates = waiting = conflicts = 0

    if store is None:
        rows = [
            _support_wait(
                candidate,
                "OUTBOX_SUPPORT_UNAVAILABLE",
                store_error or "PostgreSQL support unavailable; direct Node execution eligibility is unchanged",
            )
            for candidate in candidates
        ]
        waiting = len(rows)
    else:
        for candidate in candidates:
            validation_error = _validation_error(candidate)
            if validation_error:
                rows.append(_support_wait(candidate, "EXECUTION_PAYLOAD_NOT_READY", validation_error))
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
                        "Published command could not be reloaded; direct execution eligibility is unchanged",
                    )
                )
                waiting += 1
                continue
            if _canonical(stored.get("payload") or {}) != _canonical(candidate):
                rows.append(
                    _support_wait(
                        candidate,
                        "IMMUTABLE_PAYLOAD_CONFLICT",
                        "Candidate key already exists with a different support payload; operator reconciliation required",
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
                        "Immutable support command persisted as AVAILABLE"
                        if created
                        else "Existing immutable support command retained without overwrite"
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

    status = "PASS" if candidates and waiting == 0 else ("WAIT_RETRY" if waiting else "WAIT")
    return {
        "status": status,
        "published": published,
        "idempotentDuplicates": duplicates,
        "waitingRetry": waiting,
        "immutableConflicts": conflicts,
        "rows": rows,
        "lastError": store_error if store is None else (
            "PostgreSQL support writes require retry/reconciliation" if waiting else None
        ),
        "tradeRejectionAuthority": False,
        "supportOnly": True,
    }


def build(
    core: Any,
    now: int | None = None,
    *,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deliver Risk-approved candidates directly, then mirror support data best-effort."""
    timestamp = int(now or time.time())
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        risk = _risk_snapshot(core)
        sizing = dict(upstream or _sizing_snapshot(core))
        risk_candidates = [
            dict(row)
            for row in risk.get("approvedRiskQueue") or []
            if isinstance(row, dict)
        ]
        sizing_candidates = [
            dict(row)
            for row in sizing.get("approvedSizingQueue") or []
            if isinstance(row, dict)
        ]

        node_handoff = _deliver_direct(risk_candidates)
        postgres = _mirror_postgres(core, sizing_candidates, timestamp)
        rows = [
            *[dict(row, channel="DIRECT_NODE") for row in node_handoff["rows"]],
            *[dict(row, channel="POSTGRES_SUPPORT") for row in postgres["rows"]],
        ]
        fingerprint = _fingerprint(risk, sizing)
        metrics = {
            "riskApprovedInput": len(risk_candidates),
            "directDelivered": int(node_handoff["delivered"]),
            "directRetrying": int(node_handoff["retrying"]),
            "directInvalid": int(node_handoff["rejectedInvalid"]),
            "sizingDiagnosticApprovedInput": len(sizing_candidates),
            "postgresPublished": int(postgres["published"]),
            "postgresWaitingRetry": int(postgres["waitingRetry"]),
            "claimOperations": 0,
            "orderSubmissions": 0,
            "maximumNodeSlots": 3,
            "automaticClaimExpiry": False,
            "canonicalTransport": "AUTHENTICATED_DIRECT_HTTP",
            "postgresRole": "SUPPORT_RECONCILIATION_ONLY",
            "pythonSizingRole": "DIAGNOSTIC_ONLY",
            "tradeRejectionAuthority": False,
        }
        canonical_degraded = node_handoff["status"] == "DEGRADED"
        payload = {
            "status": "degraded" if canonical_degraded else ("ready" if rows else "empty"),
            "version": 3,
            "policyId": POLICY_ID,
            "source": "risk_approved_direct_node_handoff_with_postgresql_support",
            "inputFingerprint": fingerprint,
            "updatedAt": timestamp,
            "rows": rows,
            "metrics": metrics,
            "nodeHandoff": node_handoff,
            "postgresSupport": postgres,
            "lastError": node_handoff.get("lastError") if canonical_degraded else None,
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
                    "lastError": f"Direct Node handoff/support publisher unavailable: {exc}",
                }
            )
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(core: Any) -> bool:
    risk = _risk_snapshot(core)
    sizing = _sizing_snapshot(core)
    fingerprint = _fingerprint(risk, sizing)
    with _STATE_LOCK:
        node = dict(_STATE.get("nodeHandoff") or {})
        postgres = dict(_STATE.get("postgresSupport") or {})
        return bool(
            fingerprint != str(_STATE.get("inputFingerprint") or "")
            or int(node.get("retrying") or 0) > 0
            or int(postgres.get("waitingRetry") or 0) > 0
        )


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    if not due(core):
        return snapshot()
    sizing = _sizing_snapshot(core)
    return build(core, now=now, upstream=sizing)


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
        "canonicalTransport": "AUTHENTICATED_DIRECT_HTTP",
        "requiresPythonSizing": False,
        "requiresPostgresForDelivery": False,
        "immutableCandidateKey": True,
        "maximumNodeSlots": 3,
        "maximumLeverage": 10,
        "claimsCommands": False,
        "submitsOrder": False,
        "tradeRejectionAuthority": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    _DELIVERED.clear()
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 3,
                "policyId": POLICY_ID,
                "source": "risk_approved_direct_node_handoff_with_postgresql_support",
                "inputFingerprint": None,
                "updatedAt": 0,
                "rows": [],
                "metrics": {},
                "nodeHandoff": {
                    "status": "WAIT",
                    "delivered": 0,
                    "retrying": 0,
                    "rejectedInvalid": 0,
                    "rows": [],
                    "lastError": None,
                },
                "postgresSupport": {
                    "status": "WAIT_RETRY",
                    "published": 0,
                    "waitingRetry": 0,
                    "rows": [],
                    "lastError": None,
                },
                "lastError": None,
            }
        )
