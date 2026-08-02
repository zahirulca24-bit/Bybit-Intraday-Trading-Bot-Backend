"""Persistent authoritative risk decisions for confirmed closed-5M entries.

This stage is deliberately compositional: it does not introduce a new risk
formula. It consumes Step-6 confirmed candidates and reuses the live runtime's
existing authoritative daily-risk report, protected position guard, agreement
contract guard, signal-risk policy, and cooldown rule.

Step 7 never calculates quantity and never submits an order. Approved records
remain ready for the separate Step-8 position-sizing stage and later Node.js
execution authority.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Mapping

try:
    from . import agreement_execution_guard, execution_handoff
    from .engines.risk import signal_risk_policy
except ImportError:  # pragma: no cover - direct script compatibility
    import agreement_execution_guard
    import execution_handoff
    from engines.risk import signal_risk_policy


POLICY_ID = "AUTHORITATIVE_ENTRY_RISK_V1"
_PERSIST_KEY = "authoritative_entry_risk_v1"
_STATE_LOCK = threading.RLock()
_BUILD_LOCK = threading.Lock()
_STORE: Any | None = None

_STATE: dict[str, Any] = {
    "status": "idle",
    "version": 1,
    "policyId": POLICY_ID,
    "source": "closed_5m_confirmed_entry_existing_risk_authorities",
    "fiveMinuteCandleTime": None,
    "inputFingerprint": None,
    "updatedAt": 0,
    "rows": [],
    "approvedRiskQueue": [],
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
    approved = [dict(row) for row in _STATE.get("approvedRiskQueue") or []]
    return {
        "status": status_override or str(_STATE.get("status") or "idle"),
        "version": int(_STATE.get("version") or 1),
        "policyId": str(_STATE.get("policyId") or POLICY_ID),
        "source": str(
            _STATE.get("source")
            or "closed_5m_confirmed_entry_existing_risk_authorities"
        ),
        "fiveMinuteCandleTime": _STATE.get("fiveMinuteCandleTime"),
        "inputFingerprint": _STATE.get("inputFingerprint"),
        "updatedAt": int(_STATE.get("updatedAt") or 0),
        "rows": [dict(row) for row in _STATE.get("rows") or []],
        "approvedRiskQueue": approved,
        "approvedRiskQueueSize": len(approved),
        "metrics": dict(_STATE.get("metrics") or {}),
        "lastError": _STATE.get("lastError"),
        "persisted": bool(_STATE.get("persisted")),
        "positionSizingCalls": 0,
        "orderSubmissions": 0,
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
    approved = saved.get("approvedRiskQueue")
    if not isinstance(rows, list) or not isinstance(approved, list):
        return
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": str(saved.get("status") or "idle"),
                "version": int(saved.get("version") or 1),
                "policyId": str(saved.get("policyId") or POLICY_ID),
                "source": str(
                    saved.get("source")
                    or "closed_5m_confirmed_entry_existing_risk_authorities"
                ),
                "fiveMinuteCandleTime": saved.get("fiveMinuteCandleTime"),
                "inputFingerprint": saved.get("inputFingerprint"),
                "updatedAt": int(saved.get("updatedAt") or 0),
                "rows": [dict(row) for row in rows if isinstance(row, dict)],
                "approvedRiskQueue": [
                    dict(row) for row in approved if isinstance(row, dict)
                ],
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
        "approvedRiskQueue": payload["approvedRiskQueue"],
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
        and list(confirmed.get("approvedRiskQueue") or [])
        == body["approvedRiskQueue"]
    )


def _entry_snapshot(core: Any) -> dict[str, Any]:
    reader = getattr(core, "five_minute_entry_confirmation_status", None)
    if callable(reader):
        payload = reader()
        if isinstance(payload, dict):
            return dict(payload)
    reader = getattr(core, "five_minute_entry_confirmation", None)
    if callable(reader):
        payload = reader(False)
        if isinstance(payload, dict):
            return dict(payload)
    return {}


def _fingerprint(upstream: Mapping[str, Any]) -> str:
    candle = int(upstream.get("fiveMinuteCandleTime") or 0)
    keys = sorted(
        str(row.get("candidateKey") or "")
        for row in upstream.get("confirmedEntryQueue") or []
        if isinstance(row, dict) and row.get("candidateKey")
    )
    return f"{candle}:{'|'.join(keys)}"


def _bot_state(core: Any) -> dict[str, Any]:
    lock = getattr(core, "BOT_LOCK", None)
    state = getattr(core, "BOT_STATE", {})
    if lock is not None:
        with lock:
            return dict(state or {})
    return dict(state or {})


def _max_candidate_age_seconds() -> float:
    try:
        return float(execution_handoff.settings()["maxCandidateAgeSeconds"])
    except Exception:
        # This fallback mirrors the existing handoff default; it does not create
        # a separate Step-7 policy.
        return 1200.0


def _blocked(
    candidate: Mapping[str, Any],
    *,
    code: str,
    reason: str,
    checks: Mapping[str, Any],
    timestamp: int,
) -> tuple[dict[str, Any], None]:
    row = {
        **dict(candidate),
        "riskStatus": "BLOCKED_RISK",
        "riskPolicyId": POLICY_ID,
        "riskDecisionAt": timestamp,
        "riskApproved": False,
        "riskDecision": {
            "ok": False,
            "code": code,
            "reason": reason,
            "checks": dict(checks),
        },
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "BLOCKED_BY_RISK",
        "orderSubmitted": False,
    }
    return row, None


def _evaluate_candidate(
    core: Any,
    candidate: Mapping[str, Any],
    state: Mapping[str, Any],
    timestamp: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    item = dict(candidate)
    candidate_key = str(item.get("candidateKey") or "")
    symbol = str(item.get("symbol") or "").upper()
    side = str(item.get("side") or "")
    grade = str(item.get("grade") or "")
    checks: dict[str, Any] = {}

    if (
        not candidate_key
        or not symbol
        or side not in {"Buy", "Sell"}
        or grade not in {"A+", "A"}
        or item.get("orderSubmitted") is not False
    ):
        return _blocked(
            item,
            code="INVALID_CONFIRMED_ENTRY",
            reason="Confirmed-entry identity, grade, or order state is invalid",
            checks=checks,
            timestamp=timestamp,
        )

    created_at = int(_number(item.get("createdAt"), 0))
    age_seconds = timestamp - created_at if created_at > 0 else 10**9
    max_age = _max_candidate_age_seconds()
    checks["freshness"] = {
        "ok": age_seconds <= max_age,
        "ageSeconds": age_seconds,
        "maximumAgeSeconds": max_age,
        "source": "execution_handoff.settings.maxCandidateAgeSeconds",
    }
    if age_seconds > max_age:
        return _blocked(
            item,
            code="CANDIDATE_STALE",
            reason=f"Confirmed entry age {age_seconds}s exceeds existing limit",
            checks=checks,
            timestamp=timestamp,
        )

    restricted = bool(agreement_execution_guard.is_restricted_symbol(symbol))
    checks["agreement"] = {
        "ok": not restricted,
        "restricted": restricted,
        "source": "agreement_execution_guard",
    }
    if restricted:
        rejection = agreement_execution_guard.rejection(
            symbol, "authoritative_entry_risk"
        )
        return _blocked(
            item,
            code=str(rejection.get("code") or "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"),
            reason=str(rejection.get("reason") or "Agreement policy blocked symbol"),
            checks=checks,
            timestamp=timestamp,
        )

    daily_reader = getattr(core, "daily_risk_report", None)
    if not callable(daily_reader):
        return _blocked(
            item,
            code="DAILY_RISK_AUTHORITY_UNAVAILABLE",
            reason="Authoritative daily-risk reader is unavailable",
            checks=checks,
            timestamp=timestamp,
        )
    try:
        daily = dict(daily_reader(dict(state)) or {})
    except Exception as exc:
        daily = {
            "ok": False,
            "blocked": True,
            "reason": f"Authoritative daily-risk evaluation failed: {exc}",
        }
    daily_allowed = bool(
        daily.get("ok")
        and not daily.get("blocked")
        and daily.get("newEntriesAllowed", True)
    )
    checks["dailyRisk"] = daily
    if not daily_allowed:
        return _blocked(
            item,
            code=str(daily.get("lockType") or "DAILY_RISK_BLOCKED"),
            reason=str(daily.get("reason") or "Authoritative daily risk blocked"),
            checks=checks,
            timestamp=timestamp,
        )

    position_reader = getattr(core, "existing_position_guard", None)
    if not callable(position_reader):
        return _blocked(
            item,
            code="POSITION_GUARD_UNAVAILABLE",
            reason="Protected position guard is unavailable",
            checks=checks,
            timestamp=timestamp,
        )
    try:
        position = dict(position_reader(symbol, side, dict(state)) or {})
    except Exception as exc:
        position = {
            "ok": False,
            "reason": f"Protected position guard failed: {exc}",
        }
    checks["positionGuard"] = position
    if not position.get("ok"):
        return _blocked(
            item,
            code="POSITION_GUARD_BLOCKED",
            reason=str(position.get("reason") or "Position guard blocked"),
            checks=checks,
            timestamp=timestamp,
        )

    risk_state = {
        **dict(state),
        **item,
        "symbol": symbol,
        "signal": side,
        "side": side,
        "strategyStrength": item.get("strategyStrength"),
    }
    signal_policy = dict(signal_risk_policy(risk_state, side) or {})
    checks["signalRisk"] = signal_policy
    if not signal_policy.get("ok"):
        return _blocked(
            item,
            code="SIGNAL_RISK_BLOCKED",
            reason=str(signal_policy.get("reason") or "Signal risk blocked"),
            checks=checks,
            timestamp=timestamp,
        )

    cooldown_seconds = max(0, int(_number(state.get("cooldownSeconds"), 0)))
    last_trade_at = _number(state.get("lastTradeAt"), 0.0)
    elapsed = timestamp - last_trade_at if last_trade_at > 0 else None
    cooldown_ok = elapsed is None or elapsed >= cooldown_seconds
    checks["cooldown"] = {
        "ok": cooldown_ok,
        "cooldownSeconds": cooldown_seconds,
        "lastTradeAt": last_trade_at,
        "elapsedSeconds": elapsed,
        "source": "existing_risk_cooldown_rule",
    }
    if not cooldown_ok:
        return _blocked(
            item,
            code="COOLDOWN_ACTIVE",
            reason="Cooldown active",
            checks=checks,
            timestamp=timestamp,
        )

    approved = {
        **item,
        "riskStatus": "APPROVED_RISK",
        "riskPolicyId": POLICY_ID,
        "riskDecisionAt": timestamp,
        "riskApproved": True,
        "riskSizeFactor": signal_policy.get("sizeFactor", 1.0),
        "riskFlags": list(signal_policy.get("riskFlags") or []),
        "riskDecision": {
            "ok": True,
            "code": "RISK_APPROVED",
            "reason": "Existing authoritative risk authorities approved candidate",
            "checks": checks,
        },
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "AWAITING_POSITION_SIZING",
        "orderSubmitted": False,
    }
    return dict(approved), dict(approved)


def build(
    core: Any,
    now: int | None = None,
    *,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate Step-6 candidates without sizing or order side effects."""
    timestamp = int(now or time.time())
    if not _BUILD_LOCK.acquire(blocking=False):
        with _STATE_LOCK:
            return _snapshot_unlocked("busy")

    try:
        source = dict(upstream or _entry_snapshot(core))
        source_status = str(source.get("status") or "")
        if source_status not in {"ready", "empty", "waiting"}:
            raise RuntimeError("Closed-5M entry confirmation is not ready")

        queue = [
            dict(row)
            for row in source.get("confirmedEntryQueue") or []
            if isinstance(row, dict)
        ]
        state = _bot_state(core)
        rows: list[dict[str, Any]] = []
        approved: list[dict[str, Any]] = []
        for candidate in queue:
            row, approved_candidate = _evaluate_candidate(
                core, candidate, state, timestamp
            )
            rows.append(row)
            if approved_candidate is not None:
                approved.append(approved_candidate)

        metrics = {
            "confirmedEntryInput": len(queue),
            "evaluated": len(rows),
            "approved": len(approved),
            "blocked": len(rows) - len(approved),
            "dailyRiskChecks": len(rows),
            "positionGuardChecks": sum(
                1
                for row in rows
                if "positionGuard" in (row.get("riskDecision") or {}).get("checks", {})
            ),
            "signalRiskChecks": sum(
                1
                for row in rows
                if "signalRisk" in (row.get("riskDecision") or {}).get("checks", {})
            ),
            "agreementChecks": sum(
                1
                for row in rows
                if "agreement" in (row.get("riskDecision") or {}).get("checks", {})
            ),
            "positionSizingCalls": 0,
            "orderSubmissions": 0,
            "policy": "REUSE_EXISTING_AUTHORITATIVE_RISK_AUTHORITIES",
        }
        fingerprint = _fingerprint(source)
        payload = {
            "status": "ready" if rows else "empty",
            "version": 1,
            "policyId": POLICY_ID,
            "source": "closed_5m_confirmed_entry_existing_risk_authorities",
            "fiveMinuteCandleTime": source.get("fiveMinuteCandleTime"),
            "inputFingerprint": fingerprint,
            "updatedAt": timestamp,
            "rows": rows,
            "approvedRiskQueue": approved,
            "metrics": metrics,
            "lastError": None,
            "persisted": False,
        }
        payload["persisted"] = _persist(payload)
        with _STATE_LOCK:
            _STATE.update(payload)
            return _snapshot_unlocked()
    except Exception as exc:
        with _STATE_LOCK:
            has_cache = bool(_STATE.get("rows") or _STATE.get("inputFingerprint"))
            _STATE.update(
                {
                    "status": "stale" if has_cache else "error",
                    "lastError": str(exc),
                }
            )
            return _snapshot_unlocked()
    finally:
        _BUILD_LOCK.release()


def due(core: Any) -> bool:
    upstream = _entry_snapshot(core)
    fingerprint = _fingerprint(upstream)
    with _STATE_LOCK:
        return bool(
            fingerprint != str(_STATE.get("inputFingerprint") or "")
            or str(_STATE.get("status") or "") in {"error", "stale"}
        )


def ensure_current(core: Any, now: int | None = None) -> dict[str, Any]:
    if not due(core):
        return snapshot()
    upstream = _entry_snapshot(core)
    return build(core, now=now, upstream=upstream)


def install(core: Any) -> dict[str, Any]:
    """Expose the Step-7 authority without replacing existing risk functions."""
    global _STORE
    if getattr(core, "_authoritative_entry_risk_v1_installed", False):
        return status(core)

    _STORE = _persistent_store(core)
    _load_persisted()
    core.authoritative_entry_risk = (
        lambda force=False: build(core) if force else ensure_current(core)
    )
    core.authoritative_entry_risk_status = snapshot
    setattr(core, "_authoritative_entry_risk_v1_installed", True)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    return {
        "installed": bool(
            core is not None
            and getattr(core, "_authoritative_entry_risk_v1_installed", False)
        ),
        "policyId": POLICY_ID,
        "reusesDailyRisk": True,
        "reusesPositionGuard": True,
        "reusesSignalRisk": True,
        "reusesCooldown": True,
        "reusesAgreementGuard": True,
        "calculatesPositionSize": False,
        "submitsOrder": False,
        "snapshot": snapshot(),
    }


def _reset_for_tests() -> None:
    global _STORE
    with _STATE_LOCK:
        _STATE.update(
            {
                "status": "idle",
                "version": 1,
                "policyId": POLICY_ID,
                "source": "closed_5m_confirmed_entry_existing_risk_authorities",
                "fiveMinuteCandleTime": None,
                "inputFingerprint": None,
                "updatedAt": 0,
                "rows": [],
                "approvedRiskQueue": [],
                "metrics": {},
                "lastError": None,
                "persisted": False,
            }
        )
    _STORE = None
