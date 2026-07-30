"""Fail-closed agreement-required contract guard for execution paths.

The scanner already filters known agreement-required Bybit Demo contracts, but
cached setup candidates and late execution handoff paths must also be protected.
This module installs a small runtime hotfix that blocks restricted symbols at
three boundaries:

1. setup queue insertion
2. setup queue read/prune before execution handoff
3. final ``place_demo_order`` call before the Bybit request layer
"""

from __future__ import annotations

from typing import Any, Callable

try:
    from . import agreement_contract_filter
except ImportError:  # pragma: no cover - direct script execution compatibility
    import agreement_contract_filter

_BLOCK_CODE = "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"
_INSTALLED_ATTR = "_agreement_execution_guard_installed"


def is_restricted_symbol(symbol: Any) -> bool:
    """Return True when ``symbol`` is blocked by the agreement policy."""
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return False
    _, rejected = agreement_contract_filter.filter_symbols([normalized])
    return normalized in set(rejected)


def rejection(symbol: Any, boundary: str) -> dict[str, Any]:
    normalized = str(symbol or "").strip().upper()
    return {
        "ok": False,
        "code": _BLOCK_CODE,
        "symbol": normalized,
        "boundary": boundary,
        "reason": (
            f"{normalized} is excluded by the Bybit Demo agreement-required "
            f"contract policy at {boundary}."
        ),
        "excludedSymbols": sorted(agreement_contract_filter.excluded_symbols()),
    }


def _candidate_symbol(candidate: Any) -> str:
    return str((candidate or {}).get("symbol") or "").strip().upper()


def _prune_setup_queue(setup_worker: Any) -> list[dict[str, Any]]:
    """Remove restricted candidates from the in-memory confirmed setup queue."""
    state = getattr(setup_worker, "_STATE", None)
    lock = getattr(setup_worker, "_LOCK", None)
    if not isinstance(state, dict):
        return []

    def mutate() -> list[dict[str, Any]]:
        queue = [dict(row) for row in state.get("confirmedQueue") or []]
        blocked = [row for row in queue if is_restricted_symbol(_candidate_symbol(row))]
        if blocked:
            state["confirmedQueue"] = [
                row for row in queue if not is_restricted_symbol(_candidate_symbol(row))
            ]
            state["lastAgreementExecutionGuard"] = {
                "code": _BLOCK_CODE,
                "blockedSymbols": sorted({_candidate_symbol(row) for row in blocked}),
                "blockedCount": len(blocked),
                "boundary": "setup_confirmed_queue",
            }
        return blocked

    if lock is not None and hasattr(lock, "acquire") and lock.acquire(blocking=False):
        try:
            return mutate()
        finally:
            lock.release()
    # If the setup worker already holds the lock, do not block. The final order
    # guard still protects exchange submission.
    return []


def _wrap_setup_queue(setup_worker: Any) -> None:
    original_queue_candidate: Callable[..., Any] | None = getattr(
        setup_worker, "_queue_candidate", None
    )
    if callable(original_queue_candidate) and not getattr(
        original_queue_candidate, "_agreement_guard_wrapped", False
    ):

        def guarded_queue_candidate(candidate: dict[str, Any], queue_limit: int) -> bool:
            symbol = _candidate_symbol(candidate)
            if is_restricted_symbol(symbol):
                state = getattr(setup_worker, "_STATE", None)
                if isinstance(state, dict):
                    state["lastAgreementExecutionGuard"] = rejection(
                        symbol, "setup_queue_insert"
                    )
                return False
            return bool(original_queue_candidate(candidate, queue_limit))

        guarded_queue_candidate._agreement_guard_wrapped = True  # type: ignore[attr-defined]
        setup_worker._queue_candidate = guarded_queue_candidate

    original_snapshot: Callable[..., Any] | None = getattr(setup_worker, "snapshot", None)
    if callable(original_snapshot) and not getattr(
        original_snapshot, "_agreement_guard_wrapped", False
    ):

        def guarded_snapshot() -> dict[str, Any]:
            _prune_setup_queue(setup_worker)
            return dict(original_snapshot() or {})

        guarded_snapshot._agreement_guard_wrapped = True  # type: ignore[attr-defined]
        setup_worker.snapshot = guarded_snapshot

    original_pop_confirmed: Callable[..., Any] | None = getattr(
        setup_worker, "pop_confirmed", None
    )
    if callable(original_pop_confirmed) and not getattr(
        original_pop_confirmed, "_agreement_guard_wrapped", False
    ):

        def guarded_pop_confirmed() -> dict[str, Any] | None:
            _prune_setup_queue(setup_worker)
            candidate = original_pop_confirmed()
            if candidate and is_restricted_symbol(_candidate_symbol(candidate)):
                return None
            return candidate

        guarded_pop_confirmed._agreement_guard_wrapped = True  # type: ignore[attr-defined]
        setup_worker.pop_confirmed = guarded_pop_confirmed


def _wrap_execution_handoff(execution_handoff: Any, setup_worker: Any) -> None:
    original_run_once: Callable[..., Any] | None = getattr(execution_handoff, "run_once", None)
    if callable(original_run_once) and not getattr(
        original_run_once, "_agreement_guard_wrapped", False
    ):

        def guarded_run_once(core: Any, worker: Any, now: int | None = None) -> dict[str, Any]:
            blocked = _prune_setup_queue(worker or setup_worker)
            if blocked:
                snapshot_with_result = getattr(execution_handoff, "snapshot_with_result", None)
                result = {
                    "status": "blocked",
                    "code": _BLOCK_CODE,
                    "reason": "Restricted agreement-required setup candidates were removed before execution handoff.",
                    "blockedSymbols": sorted({_candidate_symbol(row) for row in blocked}),
                    "boundary": "execution_handoff_preclaim",
                }
                if callable(snapshot_with_result):
                    return dict(snapshot_with_result(result) or {})
                return {"currentResult": result}
            return dict(original_run_once(core, worker, now) or {})

        guarded_run_once._agreement_guard_wrapped = True  # type: ignore[attr-defined]
        execution_handoff.run_once = guarded_run_once


def _wrap_final_order_submit(core: Any) -> None:
    original_place_demo_order: Callable[..., Any] | None = getattr(core, "place_demo_order", None)
    if callable(original_place_demo_order) and not getattr(
        original_place_demo_order, "_agreement_guard_wrapped", False
    ):

        def guarded_place_demo_order(symbol: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
            normalized = str(symbol or "").strip().upper()
            if is_restricted_symbol(normalized):
                blocked = rejection(normalized, "final_order_submit")
                return {
                    "retCode": -1,
                    "retMsg": blocked["reason"],
                    "result": {},
                    "accepted": False,
                    "finalFilled": False,
                    "agreementExecutionGuard": blocked,
                    "fillVerification": {
                        "accepted": False,
                        "finalFilled": False,
                        "terminal": True,
                        "unresolved": False,
                        "state": "Rejected",
                        "reason": blocked["reason"],
                        "cumExecQty": "0",
                    },
                }
            return original_place_demo_order(symbol, *args, **kwargs)

        guarded_place_demo_order._agreement_guard_wrapped = True  # type: ignore[attr-defined]
        core.place_demo_order = guarded_place_demo_order


def install(core: Any, setup_worker: Any, execution_handoff: Any) -> None:
    """Install fail-closed guards without changing public runtime contracts."""
    if getattr(core, _INSTALLED_ATTR, False):
        return
    _wrap_setup_queue(setup_worker)
    _wrap_execution_handoff(execution_handoff, setup_worker)
    _wrap_final_order_submit(core)
    setattr(core, _INSTALLED_ATTR, True)


def status(core: Any) -> dict[str, Any]:
    return {
        "installed": bool(getattr(core, _INSTALLED_ATTR, False)),
        "code": _BLOCK_CODE,
        "excludedSymbols": sorted(agreement_contract_filter.excluded_symbols()),
        "boundaries": [
            "setup_queue_insert",
            "setup_confirmed_queue",
            "execution_handoff_preclaim",
            "final_order_submit",
        ],
    }
