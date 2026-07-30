"""Fail-closed safety boundary for Historical Replay.

Historical Replay is an internal simulation mode. It may read explicitly
allowlisted public market data, but it must never submit, amend, cancel, close,
or otherwise manage an exchange order or position.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REPLAY_RUNTIME_MODE = "historical_replay"
REPLAY_EXECUTION_MODE = "simulated_only"
ALLOWED_PUBLIC_MARKET_PATHS = frozenset({"/v5/market/kline"})
HISTORICAL_PUBLIC_MARKET_ORIGIN = "https://api.bybit.com"

_FORBIDDEN_TRUTHY_FIELDS = frozenset(
    {
        "confirmdemoorder",
        "execute",
        "executeorder",
        "placeorder",
        "sendorder",
        "submitorder",
        "autotrade",
        "livetrading",
        "demotrading",
        "exchangeexecution",
    }
)
_FORBIDDEN_NONEMPTY_FIELDS = frozenset(
    {"apikey", "apisecret", "exchangeendpoint", "privateendpoint", "orderendpoint"}
)
_ALLOWED_EXECUTION_MODES = frozenset(
    {"simulated", "simulation", "simulated_only", "replay", "historical_replay", "backtest"}
)


class ReplaySafetyViolation(ValueError):
    """Raised when a replay request attempts to cross the exchange boundary."""


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return value is not None


def _walk_items(value: Any) -> Iterable[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key).strip().lower(), item
            yield from _walk_items(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_items(item)


def validate_replay_request(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    data = dict(payload or {})
    violations: list[str] = []
    for key, value in _walk_items(data):
        if key in _FORBIDDEN_TRUTHY_FIELDS and _is_truthy(value):
            violations.append(f"{key}=true")
        if key in _FORBIDDEN_NONEMPTY_FIELDS and str(value or "").strip():
            violations.append(f"{key}=provided")
        if key == "executionmode":
            normalized = str(value or "").strip().lower()
            if normalized and normalized not in _ALLOWED_EXECUTION_MODES:
                violations.append(f"executionmode={normalized}")
    if violations:
        joined = ", ".join(sorted(set(violations)))
        raise ReplaySafetyViolation(
            "Historical Replay is simulation-only; exchange execution intent was rejected: "
            f"{joined}"
        )
    data["runtimeMode"] = REPLAY_RUNTIME_MODE
    data["executionMode"] = REPLAY_EXECUTION_MODE
    data["externalExecutionAllowed"] = False
    return data


def assert_public_market_request(method: str, path: str) -> tuple[str, str]:
    normalized_method = str(method or "").strip().upper()
    normalized_path = str(path or "").split("?", 1)[0].strip()
    if normalized_method != "GET":
        raise ReplaySafetyViolation(
            "Historical Replay market data is read-only; only GET is allowed."
        )
    if normalized_path not in ALLOWED_PUBLIC_MARKET_PATHS:
        raise ReplaySafetyViolation(
            f"Historical Replay market path is not allowlisted: {normalized_path or '<empty>'}"
        )
    return normalized_method, normalized_path


def block_external_exchange_action(action: str = "exchange action") -> None:
    raise ReplaySafetyViolation(
        f"Historical Replay cannot perform {str(action or 'exchange action')}."
    )


def policy_status() -> dict[str, Any]:
    return {
        "ok": True,
        "module": "historical_replay",
        "runtimeMode": REPLAY_RUNTIME_MODE,
        "executionMode": REPLAY_EXECUTION_MODE,
        "externalExecutionAllowed": False,
        "exchangeOrderRoutesAllowed": False,
        "privateExchangeApiAllowed": False,
        "publicMarketDataReadOnly": True,
        "historicalDataCollectorImplemented": True,
        "historicalDataOrigin": HISTORICAL_PUBLIC_MARKET_ORIGIN,
        "allowedPublicMarketPaths": sorted(ALLOWED_PUBLIC_MARKET_PATHS),
        "sessionApiImplemented": True,
        "sessionApiCapabilities": ["create", "list", "get", "reset"],
        "stepEngineImplemented": True,
        "stepApiCapabilities": [
            "deterministic_cursor",
            "bounded_batch",
            "idempotent_request",
            "optimistic_cursor",
            "atomic_events",
            "historical_strategy",
            "candidate_risk",
            "simulated_fills",
            "fees",
            "stop_loss",
            "take_profit",
            "realized_pnl",
            "equity_marks",
        ],
        "strategyReplayImplemented": True,
        "strategyReplayCapabilities": [
            "ema20",
            "ema50",
            "rsi14",
            "atr14",
            "macd",
            "grading",
        ],
        "riskReplayImplemented": True,
        "riskReplayCapabilities": [
            "grade_risk_pct",
            "atr_stop",
            "two_r_target",
            "candidate_sizing",
            "leverage_cap",
        ],
        "simulatedExecutionImplemented": True,
        "simulatedExecutionCapabilities": [
            "candle_close_entry",
            "conservative_stop_first",
            "gap_aware_stop",
            "take_profit",
            "fees",
            "realized_pnl",
            "equity_marks",
            "session_end_close",
            "durable_idempotency",
        ],
        "performanceSummaryImplemented": True,
        "performanceSummaryCapabilities": [
            "win_rate",
            "gross_profit_loss",
            "profit_factor",
            "expectancy",
            "r_multiples",
            "fees",
            "net_and_equity_pnl",
            "max_drawdown",
            "equity_curve",
            "trade_streaks",
        ],
        "replayJournalImplemented": True,
        "replayJournalCapabilities": [
            "event_timeline",
            "stable_sequence_pagination",
            "event_type_filter",
            "category_filter",
            "trade_snapshot",
            "trade_status_filter",
            "optional_payloads",
        ],
    }


def unavailable_payload(capability: str) -> dict[str, Any]:
    status = policy_status()
    return {
        "ok": False,
        "error": "Historical Replay capability is not implemented yet.",
        "code": "REPLAY_NOT_IMPLEMENTED",
        "capability": str(capability or "unknown"),
        "safety": status,
    }
