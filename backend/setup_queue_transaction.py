"""Exact, lock-protected mutations for the setup worker confirmed queue.

The setup worker intentionally exposes only snapshots and a legacy FIFO pop.
The execution handoff needs stronger semantics: remove one named candidate only
after its exchange outcome has been durably resolved. This adapter performs that
exact mutation while holding the setup worker's own lock.
"""

from __future__ import annotations

from typing import Any


def _queue_components(setup_worker: Any) -> tuple[Any, dict[str, Any]]:
    lock = getattr(setup_worker, "_LOCK", None)
    state = getattr(setup_worker, "_STATE", None)
    if lock is None or not hasattr(lock, "__enter__"):
        raise RuntimeError("Setup worker queue lock is unavailable.")
    if not isinstance(state, dict):
        raise RuntimeError("Setup worker queue state is unavailable.")
    return lock, state


def remove_exact_candidate(
    setup_worker: Any, candidate_key: str
) -> tuple[bool, str]:
    """Remove exactly ``candidate_key`` or prove it is already absent.

    Returning success for an already-absent key makes restart recovery
    idempotent after a crash between queue removal and claim completion.
    """

    key = str(candidate_key or "")
    if not key:
        return False, "Candidate key is missing."

    # Tests and future queue implementations may provide a public exact-removal
    # method. Prefer it when available.
    public_remove = getattr(setup_worker, "remove_confirmed", None)
    if callable(public_remove):
        removed = public_remove(key)
        if isinstance(removed, dict):
            if str(removed.get("candidateKey") or "") != key:
                return False, "Queue removed a different candidate."
            return True, "matching candidate removed"
        queue = list(
            (setup_worker.snapshot() or {}).get("confirmedQueue") or []
        )
        if not any(str(row.get("candidateKey") or "") == key for row in queue):
            return True, "matching candidate already absent"
        return False, "Matching candidate remains queued."

    lock, state = _queue_components(setup_worker)
    with lock:
        queue = [
            dict(row)
            for row in list(state.get("confirmedQueue") or [])
            if isinstance(row, dict)
        ]
        index = next(
            (
                position
                for position, row in enumerate(queue)
                if str(row.get("candidateKey") or "") == key
            ),
            None,
        )
        if index is None:
            return True, "matching candidate already absent"
        queue.pop(index)
        state["confirmedQueue"] = queue
        return True, "matching candidate removed"
