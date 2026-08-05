#!/usr/bin/env python3
"""Non-trading end-to-end deployment readiness verification.

This verifier performs GET-only checks. It never calls an order, start, stop,
position-management, or mutation endpoint.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _base(name: str) -> str:
    value = os.environ.get(name, "").strip().rstrip("/")
    if not value.startswith("https://"):
        raise RuntimeError(f"{name} must be a configured HTTPS URL")
    return value


def _get_json(url: str, token: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Accept": "application/json", "User-Agent": "deployment-readiness-gate/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"body": body[:500]}
        return error.code, payload


def verify() -> list[Check]:
    frontend = _base("FRONTEND_PUBLIC_URL")
    backend = _base("BACKEND_PUBLIC_URL")
    node = _base("NODE_EXECUTION_PUBLIC_URL")
    admin_token = os.environ.get("ADMIN_TOKEN", "").strip()
    if len(admin_token) < 32:
        raise RuntimeError("ADMIN_TOKEN must be configured and at least 32 characters")

    checks: list[Check] = []

    frontend_status, _ = _get_json(f"{frontend}/api/auth/status")
    checks.append(Check("frontend_reachable", frontend_status in {200, 401}, f"HTTP {frontend_status}"))

    status, payload = _get_json(f"{backend}/healthz")
    checks.append(Check("backend_liveness", status == 200 and payload.get("ok") is True and payload.get("demoOnly") is True, f"HTTP {status}"))

    status, payload = _get_json(f"{backend}/readyz")
    durable = payload.get("durableState") or {}
    leadership = payload.get("runtimeLeadership") or {}
    checks.append(Check("backend_readiness", status == 200 and payload.get("ok") is True, f"HTTP {status}"))
    checks.append(Check("postgresql_durable", durable.get("ok") is True and durable.get("degraded") is False and durable.get("restartSafe") is True, json.dumps(durable, sort_keys=True)))
    checks.append(Check("postgresql_migration", int(durable.get("migrationVersion") or 0) >= 5, f"migration={durable.get('migrationVersion')}"))
    checks.append(Check("python_leadership_state", leadership.get("status") in {"leader", "standby"}, json.dumps(leadership, sort_keys=True)))

    status, payload = _get_json(f"{backend}/api/runtime/leadership", admin_token)
    checks.append(Check("protected_runtime_truth", status == 200 and payload.get("ok") is True, f"HTTP {status}"))

    status, payload = _get_json(f"{node}/healthz")
    checks.append(Check("node_liveness", status == 200 and payload.get("ok") is True and payload.get("demoOnly") is True, f"HTTP {status}"))

    status, payload = _get_json(f"{node}/readyz")
    slot_safety = payload.get("slotSafety") or {}
    checks.append(Check("node_service_readiness", status == 200 and payload.get("serviceReady") is True, f"HTTP {status}"))
    checks.append(Check("node_execution_disabled", payload.get("enabled") is False and payload.get("executionReady") is False, f"enabled={payload.get('enabled')} executionReady={payload.get('executionReady')}"))
    checks.append(Check("three_slot_contract", slot_safety.get("validSlotCount") is True and slot_safety.get("configuredSlots") == 3, json.dumps(slot_safety, sort_keys=True)))
    checks.append(Check("no_duplicate_slot_candidate", slot_safety.get("duplicateCandidateDetected") is False, json.dumps(slot_safety, sort_keys=True)))

    return checks


def main() -> int:
    try:
        checks = verify()
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}, indent=2))
        return 2

    result = {
        "ok": all(check.ok for check in checks),
        "nonTradingVerification": True,
        "checks": [check.__dict__ for check in checks],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
