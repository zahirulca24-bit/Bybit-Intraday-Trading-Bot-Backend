"""Execution-time enforcement for the Serial 6 scanner policy."""

from __future__ import annotations

import threading
import time
from typing import Any

try:
    from . import guarded_server as guarded
    from . import intraday_scanner
except ImportError:
    import guarded_server as guarded
    import intraday_scanner


_SNAPSHOT_CONTEXT = threading.local()


def _atr_volume_with_closed_history(core: Any, symbol: str):
    """Use enough history for the canonical 60-closed-candle fetch gate."""
    candles, _ = core.fetch_candles(symbol, "15", limit=80)
    if not candles or len(candles) < 60:
        return None, None
    closes = [float(row["close"]) for row in candles]
    highs = [float(row["high"]) for row in candles]
    lows = [float(row["low"]) for row in candles]
    volumes = [float(row.get("volume") or 0) for row in candles]
    atr = core.simple_atr(highs, lows, closes, 14)
    atr_pct = (atr / closes[-1]) * 100 if closes[-1] > 0 and atr > 0 else None
    baseline_rows = volumes[-21:-1]
    baseline = sum(baseline_rows) / len(baseline_rows) if baseline_rows and sum(baseline_rows) > 0 else 0
    volume_ratio = volumes[-1] / baseline if baseline > 0 else None
    return atr_pct, volume_ratio


def closed_market_snapshot(core: Any, engine: Any, symbol: str, interval: str) -> dict:
    """Build the modular engine snapshot exclusively from fully closed candles."""
    entry_interval = guarded.normalize_interval(interval)
    engine.set_status("marketData", "running")
    tf1h, message1h = core.fetch_candles(symbol, "60")
    tf15m, message15m = core.fetch_candles(symbol, "15")
    entry_tf, message_entry = core.fetch_candles(symbol, entry_interval)
    ok = bool(tf1h and tf15m and entry_tf)
    engine.set_status("marketData", "ok" if ok else "error")
    candle_time = int(entry_tf[-1]["time"]) if entry_tf else None
    _SNAPSHOT_CONTEXT.candle_time = candle_time
    # Guarded execution reads this thread-local value to build the unique signal key.
    guarded._SCAN_CONTEXT.candle_time = candle_time
    return {
        "ok": ok,
        "timeframes": {"1H": tf1h, "15M": tf15m, "5M": entry_tf},
        "message": "; ".join(x for x in [message1h, message15m, message_entry] if x),
        "entryInterval": entry_interval,
        "signalCandleTime": candle_time,
    }


def install(core: Any) -> None:
    if getattr(core, "_scanner_execution_gate_installed", False):
        return
    intraday_scanner._atr_volume = _atr_volume_with_closed_history
    original_universe = core.top_gainer_universe
    last_forced_at = [0.0]

    def throttled_universe(force=False, limit=10):
        now = time.monotonic()
        effective_force = bool(force) and now - last_forced_at[0] >= 60.0
        if effective_force:
            last_forced_at[0] = now
        return original_universe(force=effective_force, limit=limit)

    core.top_gainer_universe = throttled_universe
    engine = core.get_bot_engine()
    original_evaluate_signal = core.evaluate_signal
    original_risk_check = engine.risk_check

    # BotEngineV2.evaluate calls engine.market_data.snapshot directly. The older
    # guard patched engine.market_snapshot, which is unused by the modular engine.
    # Wire the real snapshot method so strategies and signal identity use the same
    # fully closed entry candle.
    def modular_closed_snapshot(symbol):
        interval = getattr(guarded._SCAN_CONTEXT, "interval", "5")
        return closed_market_snapshot(core, engine, symbol, interval)

    engine.market_data.snapshot = modular_closed_snapshot

    def current_vote_evaluate(symbol, interval, mode="balanced"):
        _SNAPSHOT_CONTEXT.candle_time = None
        result = original_evaluate_signal(symbol, interval, mode)
        signal, reason, votes, router, indicators, status = result
        indicators = dict(indicators or {})
        candle_time = indicators.get("signalCandleTime") or getattr(_SNAPSHOT_CONTEXT, "candle_time", None)
        indicators["entryInterval"] = guarded.normalize_interval(interval)
        indicators["signalCandleTime"] = int(candle_time) if candle_time is not None else None
        core._current_scanner_signal = {
            "symbol": str(symbol or "").upper(),
            "signal": signal,
            "votes": list(votes or []),
            "signalCandleTime": indicators["signalCandleTime"],
        }
        return signal, reason, votes, router, indicators, status

    def eligible_universe_risk_check(state, signal):
        universe = intraday_scanner.build_universe(core, force=False)
        symbol = str(state.get("symbol") or "").upper()
        eligible = set(universe.get("symbols") or [])
        if symbol not in eligible:
            # If the cached universe didn't include the signalled symbol, attempt
            # a targeted refresh and re-check. This avoids forcing a rebuild on
            # every order attempt while allowing freshly-signalled symbols to
            # pass the execution-time gate.
            try:
                fresh = intraday_scanner.build_universe(core, force=True)
                eligible = set(fresh.get("symbols") or [])
            except Exception:
                # Preserve original blocking behavior when refresh fails.
                pass
        if symbol not in eligible:
            engine.set_status("risk", "blocked")
            return False, "Symbol is not in the current liquid intraday eligible universe"
        market = next((row for row in universe.get("rows") or [] if row.get("symbol") == symbol), {})
        tier = market.get("costTier")
        context = getattr(core, "_current_scanner_signal", {}) or {}
        current_votes = context.get("votes") if context.get("symbol") == symbol and context.get("signal") == signal else []
        matching_votes = sum(1 for vote in current_votes if vote.get("signal") == signal)
        if tier == "strong_only" and matching_votes < 2:
            engine.set_status("risk", "blocked")
            return False, "Wide-spread tier requires at least two matching current strategy votes"
        return original_risk_check(state, signal)

    core.evaluate_signal = current_vote_evaluate
    engine.risk_check = eligible_universe_risk_check
    core._scanner_execution_gate_installed = True
