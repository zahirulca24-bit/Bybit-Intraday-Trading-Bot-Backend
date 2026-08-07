"""Focused live execution truth recovery helpers.

This module fixes three operational gaps without changing strategy or risk policy:
1. Derive a conservative usable margin when Bybit Demo/UTA reports zero or blank
   totalAvailableBalance even though equity exists.
2. Reuse the same Bybit closed-PnL source as analytics when the legacy daily-PnL
   helper cannot produce today's realized PnL.
3. Keep the recovery paths fail-closed: no synthetic PnL and no margin larger
   than equity minus current initial margin.
"""

from __future__ import annotations

from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else default


def _install_wallet_margin_fallback() -> None:
    try:
        from . import position_sizing_margin
    except ImportError:  # pragma: no cover
        import position_sizing_margin

    if getattr(position_sizing_margin, "_execution_truth_wallet_fallback_installed", False):
        return

    original = position_sizing_margin._wallet_snapshot

    def wallet_snapshot(core: Any) -> dict[str, Any]:
        payload = dict(original(core) or {})
        if not payload.get("ok"):
            return payload

        equity = _number(payload.get("equity"), 0.0)
        available = _number(payload.get("availableMargin"), -1.0)
        current_initial = max(0.0, _number(payload.get("currentInitialMargin"), 0.0))

        # Bybit Demo/UTA can return zero/blank account-level available balance
        # while the account is funded. Never manufacture more capacity than the
        # conservative equity-minus-current-initial-margin amount.
        conservative_available = max(0.0, equity - current_initial)
        if equity > 0 and available <= 0 and conservative_available > 0:
            payload["availableMargin"] = conservative_available
            payload["availableMarginSource"] = "EQUITY_MINUS_CURRENT_INITIAL_MARGIN"
            payload["availableMarginFallbackApplied"] = True
        else:
            payload["availableMarginSource"] = "BYBIT_TOTAL_AVAILABLE_BALANCE"
            payload["availableMarginFallbackApplied"] = False
        return payload

    position_sizing_margin._wallet_snapshot = wallet_snapshot
    position_sizing_margin._execution_truth_wallet_fallback_installed = True


def _install_daily_closed_pnl_fallback(core: Any, analytics_runtime: Any) -> None:
    if getattr(core, "_execution_truth_daily_pnl_fallback_installed", False):
        return

    original = core.get_daily_closed_pnl

    def get_daily_closed_pnl(trading_date: str):
        value, message = original(trading_date)
        if value is not None:
            return value, message

        try:
            start_epoch = int(core.get_trading_day_start_epoch(trading_date))
            start_ms = start_epoch * 1000 if start_epoch < 10_000_000_000 else start_epoch
            end_ms = start_ms + 86_400_000
            rows = analytics_runtime._fetch_closed_trades(core, 500)
            today = [
                row for row in rows
                if start_ms <= int(row.get("closedAt") or 0) < end_ms
            ]
            realized = sum(_number(row.get("closedPnl"), 0.0) for row in today)
            return realized, (
                "OK via canonical Bybit closed-PnL fallback"
                if today
                else "OK; no closed-PnL rows for trading day"
            )
        except Exception as exc:
            return None, f"{message}; canonical closed-PnL fallback failed: {exc}"

    core.get_daily_closed_pnl = get_daily_closed_pnl
    core._execution_truth_daily_pnl_fallback_installed = True


def install(core: Any, analytics_runtime: Any) -> dict[str, Any]:
    _install_wallet_margin_fallback()
    _install_daily_closed_pnl_fallback(core, analytics_runtime)
    return status(core)


def status(core: Any | None = None) -> dict[str, Any]:
    try:
        from . import position_sizing_margin
    except ImportError:  # pragma: no cover
        import position_sizing_margin
    return {
        "installed": bool(
            core is not None
            and getattr(core, "_execution_truth_daily_pnl_fallback_installed", False)
            and getattr(position_sizing_margin, "_execution_truth_wallet_fallback_installed", False)
        ),
        "walletFallback": "equity_minus_current_initial_margin",
        "dailyPnlFallback": "canonical_bybit_closed_pnl",
        "syntheticPnlAllowed": False,
    }
