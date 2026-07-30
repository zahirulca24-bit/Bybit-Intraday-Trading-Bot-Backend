"""Authenticated Historical Replay session API.

This module creates and reads simulation-only replay sessions backed by the
PostgreSQL replay repository. It never calls the authenticated Bybit client and
contains no exchange order, position, or private API capability.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.parse
from collections.abc import Callable, Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

try:
    from . import replay_collector, replay_session_repository as repository
    from .replay_storage import REPLAY_SESSION_STATUSES, ReplayStorageValidationError
except ImportError:
    import replay_collector
    import replay_session_repository as repository
    from replay_storage import REPLAY_SESSION_STATUSES, ReplayStorageValidationError


SESSION_LIST_DEFAULT = 50
SESSION_LIST_MAX = 100
SESSION_CONFIG_MAX_BYTES = 16_384
MIN_SESSION_CANDLES = 2
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,80}")
_SESSION_DETAIL_PATH = re.compile(r"^/api/replay/sessions/([A-Za-z0-9_-]{8,80})$")
_SESSION_RESET_PATH = re.compile(
    r"^/api/replay/sessions/([A-Za-z0-9_-]{8,80})/reset$"
)


class ReplaySessionError(RuntimeError):
    """Base failure raised by the replay session API."""


class ReplaySessionValidationError(ReplaySessionError):
    """Raised when a replay session request violates the API contract."""


class ReplaySessionNotFoundError(ReplaySessionError):
    """Raised when a requested session does not exist."""


class ReplaySessionConflictError(ReplaySessionError):
    """Raised for idempotency conflicts or unsafe state transitions."""


class ReplaySessionDataIncompleteError(ReplaySessionConflictError):
    """Raised when the exact replay candle range is not available."""

    def __init__(self, message: str, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.details = dict(details or {})


class ReplaySessionStoreError(ReplaySessionError):
    """Raised when durable PostgreSQL replay state is unavailable."""


def _boolean(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    raise ReplaySessionValidationError("Boolean replay option is invalid.")


def _timestamp_ms(value: Any, field: str) -> int:
    try:
        timestamp = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplaySessionValidationError(
            f"{field} must be an integer timestamp in milliseconds."
        ) from exc
    if timestamp < 1_000_000_000_000:
        raise ReplaySessionValidationError(
            f"{field} must be a Unix timestamp in milliseconds."
        )
    return timestamp


def _positive_decimal(value: Any, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ReplaySessionValidationError(f"{field} must be numeric.") from exc
    if not result.is_finite() or result <= 0:
        raise ReplaySessionValidationError(f"{field} must be greater than zero.")
    if result > Decimal("1000000000000"):
        raise ReplaySessionValidationError(f"{field} exceeds the replay safety limit.")
    return result


def _strategy_mode(value: Any) -> str:
    mode = str(value or "conservative").strip().lower()
    if mode not in {"conservative", "balanced", "aggressive"}:
        raise ReplaySessionValidationError("Unsupported replay strategyMode.")
    return mode


def _optional_session_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    session_id = str(value).strip()
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ReplaySessionValidationError(
            "sessionId must contain 8-80 letters, numbers, underscores, or hyphens."
        )
    return session_id


def _bounded_config(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReplaySessionValidationError("config must be an object.")
    config = dict(value)
    try:
        encoded = json.dumps(
            config,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReplaySessionValidationError(
            "config must contain JSON-compatible finite values."
        ) from exc
    if len(encoded) > SESSION_CONFIG_MAX_BYTES:
        raise ReplaySessionValidationError(
            f"config exceeds the {SESSION_CONFIG_MAX_BYTES}-byte safety limit."
        )
    return config


def _bounded_limit(value: Any, *, default: int = SESSION_LIST_DEFAULT) -> int:
    if value is None or value == "":
        return default
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplaySessionValidationError("limit must be an integer.") from exc
    if not 1 <= limit <= SESSION_LIST_MAX:
        raise ReplaySessionValidationError(
            f"limit must be between 1 and {SESSION_LIST_MAX}."
        )
    return limit


def _normalized_status(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    status = str(value).strip().upper()
    if status not in REPLAY_SESSION_STATUSES:
        raise ReplaySessionValidationError("Unsupported replay session status.")
    return status


def _immutable_config(value: Any) -> dict[str, Any]:
    config = dict(value or {}) if isinstance(value, Mapping) else {}
    config.pop("autoSync", None)
    return config


def _request_signature(session: Mapping[str, Any]) -> tuple[Any, ...]:
    config = dict(session.get("config") or {})
    try:
        initial_balance = Decimal(str(session.get("initialBalance")))
    except (InvalidOperation, TypeError, ValueError):
        initial_balance = Decimal("-1")
    try:
        requested_start = int(
            config.get("requestedStartTime", session.get("startTime") or 0)
        )
        requested_end = int(
            config.get("requestedEndTime", session.get("endTime") or 0)
        )
    except (TypeError, ValueError):
        requested_start = requested_end = 0
    return (
        str(session.get("symbol") or ""),
        str(session.get("timeframe") or ""),
        requested_start,
        requested_end,
        initial_balance,
        str(session.get("strategyMode") or ""),
        _immutable_config(config),
    )


def _new_session_id(now_seconds: int, token_factory: Callable[[int], str]) -> str:
    return f"replay_{int(now_seconds)}_{token_factory(8)}"


def _range_complete(result: Mapping[str, Any]) -> bool:
    selected_range = result.get("range")
    return bool(isinstance(selected_range, Mapping) and selected_range.get("complete"))


class ReplaySessionService:
    """Create, list, inspect, and reset persistent simulation-only sessions."""

    def __init__(
        self,
        store: Any,
        collector: Any,
        *,
        now_seconds: Callable[[], int] | None = None,
        token_factory: Callable[[int], str] = secrets.token_hex,
    ):
        self.store = store
        self.collector = collector
        self._now_seconds = now_seconds or (lambda: int(time.time()))
        self._token_factory = token_factory

    def _store(self) -> Any:
        try:
            return repository.require_store(self.store)
        except repository.ReplaySessionRepositoryError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc

    def _coverage(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            return self.collector.coverage(payload)
        except replay_collector.ReplayCollectorValidationError as exc:
            raise ReplaySessionValidationError(str(exc)) from exc
        except replay_collector.ReplayCollectorStoreError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc
        except replay_collector.ReplayCollectorTransportError:
            raise
        except replay_collector.ReplayCollectorError as exc:
            raise ReplaySessionError(str(exc)) from exc

    def _existing_response(
        self, session: Mapping[str, Any], *, auto_sync: bool
    ) -> dict[str, Any]:
        data = self._coverage(
            {
                "symbol": session["symbol"],
                "timeframe": session["timeframe"],
                "startTime": session["startTime"],
                "endTime": session["endTime"],
            }
        )
        return {
            "ok": True,
            "created": False,
            "session": dict(session),
            "data": {
                "autoSync": auto_sync,
                "syncPerformed": False,
                "source": "postgresql_cache",
                "range": data.get("range"),
                "coverage": data.get("coverage"),
            },
            "stepEngineImplemented": False,
        }

    def start(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise ReplaySessionValidationError("Replay start payload must be an object.")

        auto_sync = _boolean(payload.get("autoSync"), default=True)
        force_sync = _boolean(
            payload.get("forceDataSync", payload.get("force")), default=False
        )
        if force_sync and not auto_sync:
            raise ReplaySessionValidationError(
                "forceDataSync requires autoSync to be enabled."
            )
        initial_balance = _positive_decimal(
            payload.get("initialBalance", payload.get("initial_balance", "1000")),
            "initialBalance",
        )
        strategy_mode = _strategy_mode(
            payload.get("strategyMode", payload.get("strategy_mode"))
        )
        requested_id = _optional_session_id(
            payload.get("sessionId", payload.get("session_id"))
        )
        user_config = _bounded_config(payload.get("config"))
        try:
            symbol = replay_collector.normalize_symbol(payload.get("symbol"))
            timeframe = replay_collector.normalize_timeframe(
                payload.get("timeframe", payload.get("interval"))
            )
        except replay_collector.ReplayCollectorValidationError as exc:
            raise ReplaySessionValidationError(str(exc)) from exc
        requested_start = _timestamp_ms(
            payload.get("startTime", payload.get("start_time")), "startTime"
        )
        requested_end = _timestamp_ms(
            payload.get("endTime", payload.get("end_time")), "endTime"
        )
        if requested_end < requested_start:
            raise ReplaySessionValidationError("endTime cannot precede startTime.")

        config = dict(user_config)
        config.update(
            {
                "runtimeMode": "historical_replay",
                "executionMode": "simulated_only",
                "externalExecutionAllowed": False,
                "dataSource": "bybit_main_public_kline",
                "intervalMs": int(replay_collector.INTERVAL_MS[timeframe]),
                "requestedStartTime": requested_start,
                "requestedEndTime": requested_end,
                "autoSync": auto_sync,
            }
        )
        config = _bounded_config(config)
        expected_request = {
            "symbol": symbol,
            "timeframe": timeframe,
            "startTime": requested_start,
            "endTime": requested_end,
            "initialBalance": str(initial_balance),
            "strategyMode": strategy_mode,
            "config": config,
        }

        store = self._store()
        if requested_id is not None:
            try:
                existing = repository.get_session(store, requested_id)
            except ReplayStorageValidationError as exc:
                raise ReplaySessionValidationError(str(exc)) from exc
            except repository.ReplaySessionRepositoryError as exc:
                raise ReplaySessionStoreError(str(exc)) from exc
            if existing is not None:
                if _request_signature(existing) != _request_signature(expected_request):
                    raise ReplaySessionConflictError(
                        "sessionId already exists with different immutable replay settings."
                    )
                return self._existing_response(existing, auto_sync=auto_sync)

        data_payload = {
            "symbol": symbol,
            "timeframe": timeframe,
            "startTime": requested_start,
            "endTime": requested_end,
            "force": force_sync,
        }
        coverage = self._coverage(data_payload)
        request = coverage.get("request")
        if not isinstance(request, Mapping):
            raise ReplaySessionError("Replay data operation returned no normalized range.")
        if int(request.get("expectedCandles") or 0) < MIN_SESSION_CANDLES:
            raise ReplaySessionValidationError(
                f"Replay session requires at least {MIN_SESSION_CANDLES} fully closed candles."
            )

        sync_result: dict[str, Any] | None = None
        data_result = coverage
        needs_sync = force_sync or not _range_complete(coverage)
        if needs_sync:
            if not auto_sync:
                raise ReplaySessionDataIncompleteError(
                    "Replay candle range is incomplete; sync data before starting.",
                    coverage,
                )
            try:
                sync_result = self.collector.sync(data_payload)
            except replay_collector.ReplayCollectorValidationError as exc:
                raise ReplaySessionValidationError(str(exc)) from exc
            except replay_collector.ReplayCollectorStoreError as exc:
                raise ReplaySessionStoreError(str(exc)) from exc
            except replay_collector.ReplayCollectorBusyError as exc:
                raise ReplaySessionConflictError(str(exc)) from exc
            except replay_collector.ReplayCollectorTransportError:
                raise
            except replay_collector.ReplayCollectorError as exc:
                raise ReplaySessionError(str(exc)) from exc
            data_result = sync_result
            if not _range_complete(sync_result):
                raise ReplaySessionDataIncompleteError(
                    "Replay candle sync completed without a continuous selected range.",
                    sync_result,
                )
            request = sync_result.get("request")
            if not isinstance(request, Mapping):
                raise ReplaySessionError(
                    "Replay data synchronization returned no normalized range."
                )

        session_id = requested_id or _new_session_id(
            self._now_seconds(), self._token_factory
        )
        session_payload = {
            "session_id": session_id,
            "symbol": request["symbol"],
            "timeframe": request["timeframe"],
            "status": "READY",
            "start_time": int(request["startTime"]),
            "end_time": int(request["endTime"]),
            "cursor_time": None,
            "initial_balance": initial_balance,
            "balance": initial_balance,
            "equity": initial_balance,
            "strategy_mode": strategy_mode,
            "config": config,
            "summary": {},
        }
        created_event = {
            "symbol": session_payload["symbol"],
            "timeframe": session_payload["timeframe"],
            "startTime": session_payload["start_time"],
            "endTime": session_payload["end_time"],
            "requestedStartTime": requested_start,
            "requestedEndTime": requested_end,
            "initialBalance": str(initial_balance),
            "strategyMode": strategy_mode,
            "executionMode": "simulated_only",
        }

        try:
            persisted = repository.create_session(
                store, session_payload, created_event
            )
        except ReplayStorageValidationError as exc:
            raise ReplaySessionValidationError(str(exc)) from exc
        except repository.ReplaySessionRepositoryError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc

        session = persisted["session"]
        created = bool(persisted["created"])
        if not created:
            if _request_signature(session) != _request_signature(expected_request):
                raise ReplaySessionConflictError(
                    "sessionId already exists with different immutable replay settings."
                )
            return self._existing_response(session, auto_sync=auto_sync)

        return {
            "ok": True,
            "created": True,
            "session": session,
            "data": {
                "autoSync": auto_sync,
                "syncPerformed": sync_result is not None,
                "source": data_result.get("source", "postgresql_cache"),
                "range": data_result.get("range"),
                "coverage": data_result.get("coverage"),
            },
            "stepEngineImplemented": False,
        }

    def list(self, *, limit: Any = None, status: Any = None) -> dict[str, Any]:
        bounded_limit = _bounded_limit(limit)
        normalized_status = _normalized_status(status)
        try:
            sessions = repository.list_sessions(
                self._store(), limit=bounded_limit, status=normalized_status
            )
        except ReplayStorageValidationError as exc:
            raise ReplaySessionValidationError(str(exc)) from exc
        except repository.ReplaySessionRepositoryError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc
        return {
            "ok": True,
            "sessions": sessions,
            "count": len(sessions),
            "limit": bounded_limit,
            "status": normalized_status,
        }

    def get(self, session_id: str) -> dict[str, Any]:
        try:
            session = repository.get_session(self._store(), session_id)
        except ReplayStorageValidationError as exc:
            raise ReplaySessionValidationError(str(exc)) from exc
        except repository.ReplaySessionRepositoryError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc
        if session is None:
            raise ReplaySessionNotFoundError("Replay session was not found.")

        data = self._coverage(
            {
                "symbol": session["symbol"],
                "timeframe": session["timeframe"],
                "startTime": session["startTime"],
                "endTime": session["endTime"],
            }
        )
        return {
            "ok": True,
            "session": session,
            "data": {
                "range": data.get("range"),
                "coverage": data.get("coverage"),
            },
            "stepEngineImplemented": False,
        }

    def reset(self, session_id: str) -> dict[str, Any]:
        store = self._store()
        try:
            session = repository.reset_session(store, session_id)
        except ReplayStorageValidationError as exc:
            if "running replay session" in str(exc).lower():
                raise ReplaySessionConflictError(str(exc)) from exc
            raise ReplaySessionValidationError(str(exc)) from exc
        except repository.ReplaySessionRepositoryError as exc:
            raise ReplaySessionStoreError(str(exc)) from exc
        if session is None:
            raise ReplaySessionNotFoundError("Replay session was not found.")
        return {
            "ok": True,
            "reset": True,
            "session": session,
            "stepEngineImplemented": False,
        }


def install(core: Any, collector: Any | None = None) -> ReplaySessionService:
    existing = getattr(core, "_replay_session_service", None)
    if isinstance(existing, ReplaySessionService):
        return existing
    service = ReplaySessionService(
        getattr(core, "_durable_state_store", None),
        collector or replay_collector.install(core),
    )
    core._replay_session_service = service
    return service


def _service(core: Any) -> ReplaySessionService:
    current = getattr(core, "_replay_session_service", None)
    if not isinstance(current, ReplaySessionService):
        current = install(core)
    return current


def is_post_path(path: str) -> bool:
    return path == "/api/replay/start" or bool(_SESSION_RESET_PATH.fullmatch(path))


def _error_response(handler: Any, core: Any, exc: Exception) -> None:
    details: dict[str, Any] | None = None
    if isinstance(exc, ReplaySessionValidationError):
        status, code = 400, "REPLAY_SESSION_INVALID"
    elif isinstance(exc, ReplaySessionNotFoundError):
        status, code = 404, "REPLAY_SESSION_NOT_FOUND"
    elif isinstance(exc, ReplaySessionDataIncompleteError):
        status, code = 409, "REPLAY_DATA_INCOMPLETE"
        details = exc.details
    elif isinstance(exc, ReplaySessionConflictError):
        status, code = 409, "REPLAY_SESSION_CONFLICT"
    elif isinstance(exc, ReplaySessionStoreError):
        status, code = 503, "REPLAY_STORAGE_UNAVAILABLE"
    elif isinstance(exc, replay_collector.ReplayCollectorTransportError):
        status, code = 502, "BYBIT_PUBLIC_DATA_UNAVAILABLE"
    else:
        status, code = 500, "REPLAY_SESSION_INTERNAL_ERROR"
    message = str(exc) if status != 500 else "Historical replay session operation failed."
    response: dict[str, Any] = {"ok": False, "code": code, "error": message}
    if details is not None:
        response["details"] = details
    core.json_response(handler, status, response)


def handle_get(handler: Any, core: Any, path: str) -> bool:
    if path == "/api/replay/sessions":
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
        try:
            result = _service(core).list(
                limit=query.get("limit"), status=query.get("status")
            )
        except Exception as exc:
            _error_response(handler, core, exc)
        else:
            core.json_response(handler, 200, result)
        return True

    match = _SESSION_DETAIL_PATH.fullmatch(path)
    if not match:
        return False
    try:
        result = _service(core).get(match.group(1))
    except Exception as exc:
        _error_response(handler, core, exc)
    else:
        core.json_response(handler, 200, result)
    return True


def handle_post(
    handler: Any,
    core: Any,
    path: str,
    payload: Mapping[str, Any],
) -> bool:
    try:
        if path == "/api/replay/start":
            result = _service(core).start(payload)
            core.json_response(handler, 201 if result.get("created") else 200, result)
            return True

        match = _SESSION_RESET_PATH.fullmatch(path)
        if match:
            result = _service(core).reset(match.group(1))
            core.json_response(handler, 200, result)
            return True
        return False
    except Exception as exc:
        _error_response(handler, core, exc)
        return True
