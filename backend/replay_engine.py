"""Authenticated candle-by-candle Historical Replay engine.

Advances immutable PostgreSQL candles, evaluates historical strategy/risk, and
runs deterministic simulated execution. It never calls an exchange or private API.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager, nullcontext
from typing import Any

try:
    from . import replay_simulated_execution
    from . import replay_step_repository as repository
    from . import replay_strategy_risk
    from .replay_storage import _jsonb
except ImportError:
    import replay_simulated_execution
    import replay_step_repository as repository
    import replay_strategy_risk
    from replay_storage import _jsonb


STEP_PATH = "/api/replay/step"
MAX_STEP_CANDLES = repository.MAX_STEP_CANDLES
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}")


class ReplayEngineError(RuntimeError):
    pass


class ReplayEngineValidationError(ReplayEngineError):
    pass


class ReplayEngineNotFoundError(ReplayEngineError):
    pass


class ReplayEngineConflictError(ReplayEngineError):
    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class ReplayEngineDataIncompleteError(ReplayEngineConflictError):
    pass


class ReplayEngineStoreError(ReplayEngineError):
    pass


def _identifier(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _IDENTIFIER_PATTERN.fullmatch(text):
        raise ReplayEngineValidationError(
            f"{field} must contain 8-80 letters, numbers, underscores, or hyphens."
        )
    return text


def _steps(value: Any) -> int:
    candidate = 1 if value is None or value == "" else value
    try:
        result = int(candidate)
    except (TypeError, ValueError) as exc:
        raise ReplayEngineValidationError("steps must be an integer.") from exc
    if not 1 <= result <= MAX_STEP_CANDLES:
        raise ReplayEngineValidationError(
            f"steps must be between 1 and {MAX_STEP_CANDLES}."
        )
    return result


def _expected_cursor(payload: Mapping[str, Any]) -> int | None:
    if "expectedCursorTime" in payload:
        value = payload.get("expectedCursorTime")
    elif "expected_cursor_time" in payload:
        value = payload.get("expected_cursor_time")
    else:
        raise ReplayEngineValidationError(
            "expectedCursorTime is required; use null before the first candle."
        )
    if value is None or value == "":
        return None
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplayEngineValidationError(
            "expectedCursorTime must be an integer timestamp in milliseconds or null."
        ) from exc
    if result < 1_000_000_000_000:
        raise ReplayEngineValidationError(
            "expectedCursorTime must be a Unix timestamp in milliseconds or null."
        )
    return result


def normalize_step_request(payload: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ReplayEngineValidationError("Replay step payload must be an object.")
    return {
        "sessionId": _identifier(
            payload.get("sessionId", payload.get("session_id")), "sessionId"
        ),
        "requestId": _identifier(
            payload.get("requestId", payload.get("idempotencyKey")), "requestId"
        ),
        "steps": _steps(payload.get("steps", payload.get("count"))),
        "expectedCursorTime": _expected_cursor(payload),
    }


@contextmanager
def _database_pipeline_lock(store: Any, session_id: str) -> Iterator[None]:
    """Serialize all durable phases for one session across processes/instances."""

    connect = getattr(store, "connect", None)
    if not callable(connect):
        yield
        return
    with connect() as lock_db:
        with lock_db.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(hashtext(%s))", (session_id,))
        try:
            yield
        finally:
            with lock_db.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (session_id,))


def _validate_execution_preflight(store: Any, session_id: str) -> None:
    """Reject immutable fee/leverage errors before the replay cursor is advanced."""

    connect = getattr(store, "connect", None)
    if not callable(connect):
        return
    with store.lock, connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT config FROM replay_sessions WHERE session_id=%s",
                (session_id,),
            )
            row = cur.fetchone()
    if row is None:
        return
    config = row[0] if isinstance(row[0], dict) else {}
    try:
        replay_simulated_execution.execution_config({"config": config})
    except replay_simulated_execution.ReplaySimulationError as exc:
        raise ReplayEngineValidationError(
            f"Invalid replay execution configuration: {exc}"
        ) from exc


def _mark_recovery_required(
    store: Any,
    advanced: Mapping[str, Any] | None,
) -> None:
    """Block later requests until the same idempotency key finishes enrichment."""

    if not isinstance(advanced, Mapping):
        return
    session = advanced.get("session")
    session = dict(session) if isinstance(session, Mapping) else {}
    session_id = str(session.get("sessionId") or "")
    request_id = str(advanced.get("requestId") or "")
    candles = advanced.get("candles")
    if not session_id or not request_id or not isinstance(candles, list) or not candles:
        return
    connect = getattr(store, "connect", None)
    if not callable(connect):
        return
    target_status = "COMPLETED" if advanced.get("completed") else "PAUSED"
    with store.lock, connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT summary FROM replay_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return
            summary = row[0] if isinstance(row[0], dict) else {}
            summary = dict(summary)
            summary["pendingFinalStatus"] = target_status
            summary["pipelineRecoveryRequired"] = True
            summary["pendingReplayRequestId"] = request_id
            cur.execute(
                "UPDATE replay_sessions SET status='RUNNING',summary=%s,updated_at=%s "
                "WHERE session_id=%s",
                (_jsonb(summary), int(time.time()), session_id),
            )
        db.commit()


def _finalize_execution_response(
    store: Any,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist truthful activity flags and clear every recovery marker."""

    response = dict(result)
    if response.get("executionEnrichmentComplete") is not True:
        return response
    if response.get("executionActivityFinalized") is True:
        return response
    execution = response.get("execution")
    execution = dict(execution) if isinstance(execution, Mapping) else {}
    session = response.get("session")
    session = dict(session) if isinstance(session, Mapping) else {}
    session_id = str(session.get("sessionId") or "")
    request_id = str(response.get("requestId") or "")
    if not session_id or not request_id:
        return response

    active = bool(
        int(execution.get("opened") or 0)
        or int(execution.get("closed") or 0)
        or int(execution.get("openTrades") or 0)
    )
    summary = session.get("summary")
    summary = dict(summary) if isinstance(summary, Mapping) else {}
    for key in (
        "pendingFinalStatus",
        "pipelineRecoveryRequired",
        "pendingReplayRequestId",
    ):
        summary.pop(key, None)
    summary["executionSimulated"] = active
    session["summary"] = summary
    response["session"] = session
    response["executionSimulated"] = active
    response["executionActivityFinalized"] = True
    execution["simulatedActivity"] = active
    response["execution"] = execution

    connect = getattr(store, "connect", None)
    if not callable(connect):
        return response
    now = int(time.time())
    target_status = str(session.get("status") or "PAUSED").upper()
    with store.lock, connect() as db:
        with db.cursor() as cur:
            cur.execute(
                "SELECT session_id FROM replay_sessions WHERE session_id=%s FOR UPDATE",
                (session_id,),
            )
            if cur.fetchone() is None:
                raise ReplayEngineStoreError(
                    "Replay session disappeared while finalizing execution state."
                )
            cur.execute(
                "UPDATE replay_sessions SET status=%s,summary=%s,updated_at=%s "
                "WHERE session_id=%s",
                (target_status, _jsonb(summary), now, session_id),
            )
            session["updatedAt"] = now
            response["session"] = session
            cur.execute(
                "UPDATE replay_step_requests SET response_payload=%s "
                "WHERE session_id=%s AND request_id=%s",
                (_jsonb(response), session_id, request_id),
            )
            if cur.rowcount != 1:
                raise ReplayEngineStoreError(
                    "Final simulated execution response was not persisted."
                )
        db.commit()
    return response


class CandleReplayEngine:
    def __init__(self, store: Any):
        self.store = store

    def step(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request = normalize_step_request(payload)
        advanced: Mapping[str, Any] | None = None
        try:
            process_lock = getattr(self.store, "lock", None)
            process_context = process_lock if process_lock is not None else nullcontext()
            with process_context:
                with _database_pipeline_lock(self.store, request["sessionId"]):
                    _validate_execution_preflight(
                        self.store, request["sessionId"]
                    )
                    advanced = repository.advance_session(self.store, request)
                    try:
                        strategized = replay_strategy_risk.enrich_step(
                            self.store, advanced
                        )
                        executed = replay_simulated_execution.enrich_step(
                            self.store, strategized
                        )
                        if advanced.get("idempotent"):
                            executed = dict(executed)
                            executed["idempotent"] = True
                        return _finalize_execution_response(self.store, executed)
                    except Exception:
                        _mark_recovery_required(self.store, advanced)
                        raise
        except repository.ReplayStepNotFoundError as exc:
            raise ReplayEngineNotFoundError(str(exc)) from exc
        except repository.ReplayStepDataIncompleteError as exc:
            raise ReplayEngineDataIncompleteError(str(exc), exc.details) from exc
        except repository.ReplayStepConflictError as exc:
            raise ReplayEngineConflictError(str(exc), exc.details) from exc
        except repository.ReplayStepRepositoryError as exc:
            raise ReplayEngineStoreError(str(exc)) from exc
        except replay_simulated_execution.ReplaySimulationError as exc:
            raise ReplayEngineStoreError(
                f"Replay simulated execution failed: {exc}"
            ) from exc
        except ReplayEngineError:
            raise
        except Exception as exc:
            raise ReplayEngineStoreError(
                f"Replay strategy, risk, or simulated execution persistence failed: {exc}"
            ) from exc


def _mark_step_capability(result: Any) -> Any:
    if isinstance(result, dict):
        result["stepEngineImplemented"] = True
        result["strategyReplayImplemented"] = True
        result["riskReplayImplemented"] = True
        result["simulatedExecutionImplemented"] = True
    return result


def _decorate_installed_session_service(core: Any) -> None:
    service = getattr(core, "_replay_session_service", None)
    if service is None or getattr(service, "_step_capability_decorated", False):
        return
    for name in ("start", "get", "reset", "_existing_response"):
        original = getattr(service, name, None)
        if not callable(original):
            continue

        def wrapped(*args: Any, _original=original, **kwargs: Any) -> Any:
            return _mark_step_capability(_original(*args, **kwargs))

        setattr(service, name, wrapped)
    service._step_capability_decorated = True


def install(core: Any) -> CandleReplayEngine:
    _decorate_installed_session_service(core)
    existing = getattr(core, "_candle_replay_engine", None)
    if isinstance(existing, CandleReplayEngine):
        return existing
    engine = CandleReplayEngine(getattr(core, "_durable_state_store", None))
    core._candle_replay_engine = engine
    return engine


def _engine(core: Any) -> CandleReplayEngine:
    current = getattr(core, "_candle_replay_engine", None)
    return current if isinstance(current, CandleReplayEngine) else install(core)


def is_post_path(path: str) -> bool:
    return path == STEP_PATH


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    details = None
    if isinstance(exc, ReplayEngineValidationError):
        status, code = 400, "REPLAY_STEP_INVALID"
    elif isinstance(exc, ReplayEngineNotFoundError):
        status, code = 404, "REPLAY_SESSION_NOT_FOUND"
    elif isinstance(exc, ReplayEngineDataIncompleteError):
        status, code, details = 409, "REPLAY_STEP_DATA_INCOMPLETE", exc.details
    elif isinstance(exc, ReplayEngineConflictError):
        status, code, details = 409, "REPLAY_STEP_CONFLICT", exc.details
    elif isinstance(exc, ReplayEngineStoreError):
        status, code = 503, "REPLAY_STORAGE_UNAVAILABLE"
    else:
        status, code = 500, "REPLAY_STEP_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Historical replay candle step failed."
    response = {"ok": False, "code": code, "error": message}
    if details:
        response["details"] = details
    core.json_response(handler, status, response)


def handle_post(
    handler: Any,
    core: Any,
    path: str,
    payload: Mapping[str, Any],
) -> bool:
    if path != STEP_PATH:
        return False
    try:
        result = _engine(core).step(payload)
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True
