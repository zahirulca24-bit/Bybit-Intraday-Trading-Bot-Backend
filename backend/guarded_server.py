"""Guarded runtime for the canonical Bybit Demo backend.

This module wraps ``backend/server.py`` without changing its public API. It
serializes bot ticks, invalidates in-flight execution on runtime transitions,
verifies Kill Switch closes, enforces Batch 1 execution-safety gates, and
installs bounded closed-candle scanner controls.
"""

from __future__ import annotations

import os
import threading
import time
from http.server import ThreadingHTTPServer

try:
    from . import server as core
    from .batch1_safety import fail_closed_daily_risk, normalize_symbol, validate_start_payload
    from .kill_switch_verification import execute_verified_kill_switch
    from .scanner_safety import bounded_symbols, deadline_reached, filter_closed_candles, normalize_interval, signal_identity
except ImportError:
    import server as core
    from batch1_safety import fail_closed_daily_risk, normalize_symbol, validate_start_payload
    from kill_switch_verification import execute_verified_kill_switch
    from scanner_safety import bounded_symbols, deadline_reached, filter_closed_candles, normalize_interval, signal_identity

_EXECUTION_LOCK = threading.Lock()
_GENERATION_LOCK = threading.Lock()
_SIGNAL_LOCK = threading.Lock()
_TICK_CONTEXT = threading.local()
_SCAN_CONTEXT = threading.local()
_RUNTIME_GENERATION = 0
_ORIGINAL_BOT_TICK = core.bot_tick
_ORIGINAL_FETCH_CANDLES = core.fetch_candles
_ORIGINAL_EVALUATE_SIGNAL = core.evaluate_signal
_ORIGINAL_SELECT_BEST_SIGNAL = core.select_best_signal


def current_generation() -> int:
    with _GENERATION_LOCK:
        return _RUNTIME_GENERATION


def advance_generation() -> int:
    global _RUNTIME_GENERATION
    with _GENERATION_LOCK:
        _RUNTIME_GENERATION += 1
        return _RUNTIME_GENERATION


def execution_is_current() -> bool:
    tick_generation = getattr(_TICK_CONTEXT, "generation", None)
    if tick_generation is None:
        return False
    with core.BOT_LOCK:
        enabled = bool(core.BOT_STATE.get("enabled"))
    return enabled and tick_generation == current_generation()


def _scan_limit() -> int:
    try:
        return max(1, min(25, int(os.environ.get("MAX_SCAN_SYMBOLS", "10"))))
    except ValueError:
        return 10


def _scan_deadline_seconds() -> float:
    try:
        return max(1.0, min(60.0, float(os.environ.get("SCAN_DEADLINE_SECONDS", "20"))))
    except ValueError:
        return 20.0


def _closed_fetch_candles(symbol, interval, limit=120):
    normalized = normalize_interval(interval)
    candles, message = _ORIGINAL_FETCH_CANDLES(symbol, normalized, limit=limit)
    if not candles:
        return candles, message
    closed = filter_closed_candles(candles, normalized)
    if len(closed) < 60:
        return None, f"Not enough closed candles for interval {normalized}"
    return closed, "OK"


def _market_snapshot(symbol):
    engine = core.get_bot_engine()
    engine.set_status("marketData", "running")
    entry_interval = normalize_interval(getattr(_SCAN_CONTEXT, "interval", "5"))
    tf1h, message1h = core.fetch_candles(symbol, "60")
    tf15m, message15m = core.fetch_candles(symbol, "15")
    entry_tf, message_entry = core.fetch_candles(symbol, entry_interval)
    ok = bool(tf1h and tf15m and entry_tf)
    engine.set_status("marketData", "ok" if ok else "error")
    candle_time = int(entry_tf[-1]["time"]) if entry_tf else None
    _SCAN_CONTEXT.candle_time = candle_time
    return {
        "ok": ok,
        "timeframes": {"1H": tf1h, "15M": tf15m, "5M": entry_tf},
        "message": "; ".join(x for x in [message1h, message15m, message_entry] if x),
        "entryInterval": entry_interval,
        "signalCandleTime": candle_time,
    }


def _evaluate_signal(symbol, interval, mode="balanced"):
    normalized = normalize_interval(interval)
    _SCAN_CONTEXT.interval = normalized
    _SCAN_CONTEXT.candle_time = None
    signal, reason, votes, router, indicators, status = _ORIGINAL_EVALUATE_SIGNAL(symbol, normalized, mode)
    indicators = dict(indicators or {})
    indicators["entryInterval"] = normalized
    indicators["signalCandleTime"] = getattr(_SCAN_CONTEXT, "candle_time", None)
    if signal in ("Buy", "Sell") and indicators["signalCandleTime"] is not None:
        _TICK_CONTEXT.signal_key = signal_identity(symbol, normalized, indicators["signalCandleTime"], signal)
    return signal, reason, votes, router, indicators, status


def _select_best_signal(symbols, interval, mode):
    normalized = normalize_interval(interval)
    bounded = bounded_symbols(symbols, _scan_limit())
    started_at = time.monotonic()
    rows = []
    timed_out = False
    for symbol in bounded:
        if rows and deadline_reached(started_at, _scan_deadline_seconds()):
            timed_out = True
            break
        signal, reason, votes, router, indicators, engine_status = core.evaluate_signal(symbol, normalized, mode)
        row = {
            "symbol": symbol,
            "signal": signal,
            "reason": reason,
            "engineVotes": votes,
            "router": router,
            "indicators": indicators,
            "engineStatus": engine_status,
        }
        row["score"] = core.signal_score(row)
        rows.append(row)
    executable = [row for row in rows if row["signal"] in ("Buy", "Sell")]
    best = max(executable, key=lambda row: row["score"]) if executable else (rows[0] if rows else None)
    if best and best.get("signal") in ("Buy", "Sell"):
        candle_time = (best.get("indicators") or {}).get("signalCandleTime")
        _TICK_CONTEXT.signal_key = signal_identity(best["symbol"], normalized, candle_time, best["signal"])
    _SCAN_CONTEXT.scan_meta = {
        "requested": len(list(symbols or [])),
        "bounded": len(bounded),
        "completed": len(rows),
        "timedOut": timed_out,
        "deadlineSeconds": _scan_deadline_seconds(),
        "interval": normalized,
    }
    return best, rows


def _signal_key_seen(engine, key: str) -> bool:
    entries = getattr(engine.journal, "entries", [])
    return any(
        entry.get("event") == "signal_executed"
        and (entry.get("payload") or {}).get("signalKey") == key
        for entry in entries
    )


def guarded_bot_tick():
    if not _EXECUTION_LOCK.acquire(blocking=False):
        with core.BOT_LOCK:
            state = dict(core.BOT_STATE)
            state["lastReason"] = "Execution cycle already in progress; duplicate tick skipped."
        return state
    _TICK_CONTEXT.generation = current_generation()
    _TICK_CONTEXT.signal_key = None
    _SCAN_CONTEXT.scan_meta = None
    try:
        result = _ORIGINAL_BOT_TICK()
        scan_meta = getattr(_SCAN_CONTEXT, "scan_meta", None)
        if scan_meta:
            with core.BOT_LOCK:
                core.BOT_STATE["scanMeta"] = scan_meta
                result = dict(core.BOT_STATE)
        return result
    finally:
        _TICK_CONTEXT.generation = None
        _TICK_CONTEXT.signal_key = None
        _EXECUTION_LOCK.release()


def _install_execute_guard() -> None:
    engine = core.get_bot_engine()
    if getattr(engine, "_execution_generation_guard_installed", False):
        return
    original_execute = engine.execute

    def guarded_execute(state, signal):
        if not execution_is_current():
            result = {"retCode": -1002, "retMsg": "Order blocked locally: runtime stopped or execution generation changed.", "result": {}}
            engine.set_status("tradeManagement", "blocked")
            engine.journal.add("execution_cancelled", {"symbol": state.get("symbol"), "signal": signal, "result": result})
            return result
        key = getattr(_TICK_CONTEXT, "signal_key", None)
        if not key:
            result = {"retCode": -1004, "retMsg": "Order blocked locally: closed-candle signal identity unavailable.", "result": {}}
            engine.set_status("tradeManagement", "blocked")
            engine.journal.add("signal_identity_blocked", {"symbol": state.get("symbol"), "signal": signal, "result": result})
            return result
        with _SIGNAL_LOCK:
            if _signal_key_seen(engine, key):
                result = {"retCode": -1005, "retMsg": "Order blocked locally: this closed-candle signal was already executed.", "result": {}}
                engine.set_status("tradeManagement", "blocked")
                engine.journal.add("duplicate_signal_blocked", {"symbol": state.get("symbol"), "signal": signal, "signalKey": key, "result": result})
                return result
            result = original_execute(state, signal)
            if result.get("retCode") == 0:
                engine.journal.add("signal_executed", {"symbol": state.get("symbol"), "signal": signal, "signalKey": key, "result": result})
            return result

    engine.execute = guarded_execute
    engine._execution_generation_guard_installed = True


def _install_scanner_guards() -> None:
    engine = core.get_bot_engine()
    core.fetch_candles = _closed_fetch_candles
    engine.market_snapshot = _market_snapshot
    core.evaluate_signal = _evaluate_signal
    core.select_best_signal = _select_best_signal


def verified_kill_switch_result():
    with core.BOT_LOCK:
        core.check_and_reset_daily_state(core.BOT_STATE)
        core.BOT_STATE.update({"enabled": False, "lastReason": "Auto trader stopped by kill switch."})
    engine = core.get_bot_engine()
    result = execute_verified_kill_switch(
        get_open_positions=core.get_open_positions,
        cancel_all=lambda symbol: core.bybit_request("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": symbol}),
        close_symbol_positions=core.close_symbol_positions,
        journal_add=engine.journal.add,
    )
    engine.set_status("journal", "ok")
    engine.set_status("tradeManagement", "flat" if result.get("verifiedFlat") else "blocked")
    with core.BOT_LOCK:
        core.BOT_STATE["lastReason"] = result.get("retMsg", "Kill switch completed")
    return result


def _journal_block(event: str, symbol: str, side: str, reason: str) -> dict:
    result = {"retCode": -1003, "retMsg": reason, "result": {}, "ok": False}
    core.get_bot_engine().journal.add(event, {"requestedSymbol": symbol, "symbol": symbol, "side": side, "result": result})
    core.get_bot_engine().set_status("journal", "ok")
    return result


class GuardedHandler(core.Handler):
    def _read_authorized_payload(self):
        if not self.is_authorized():
            core.json_response(self, 401, {"ok": False, "error": "Unauthorized"})
            return None
        try:
            return core.read_json(self)
        except Exception as exc:
            core.json_response(self, 400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return None

    def _manual_connection_test(self, payload):
        if payload.get("confirmDemoOrder") is not True:
            core.json_response(self, 400, {"ok": False, "error": "confirmDemoOrder must be true"})
            return
        side = "Sell" if payload.get("side") == "Sell" else "Buy"
        try:
            symbol = normalize_symbol(payload.get("symbol", "BTCUSDT"))
            stop_loss_pct = float(payload.get("stopLossPct", 0.8))
            take_profit_pct = float(payload.get("takeProfitPct", 1.6))
        except (TypeError, ValueError) as exc:
            core.json_response(self, 400, _journal_block("manual_connection_test_blocked", str(payload.get("symbol") or ""), side, str(exc)))
            return
        with core.BOT_LOCK:
            core.check_and_reset_daily_state(core.BOT_STATE)
            state = dict(core.BOT_STATE)
        reached, reason = fail_closed_daily_risk(core, state)
        if reached:
            core.json_response(self, 200, _journal_block("manual_connection_test_blocked", symbol, side, reason))
            return
        sizing = core.calculate_position_sizing(symbol, state)
        if not sizing.get("ok"):
            core.json_response(self, 200, _journal_block("manual_connection_test_blocked", symbol, side, sizing.get("reason", "Sizing unavailable")))
            return
        guard = core.existing_position_guard(symbol, side, state)
        if not guard.get("ok"):
            core.json_response(self, 200, _journal_block("manual_connection_test_blocked", symbol, side, guard.get("reason", "Position guard blocked")))
            return
        result = core.place_demo_order(symbol, side, sizing["qty"], "manual", stop_loss_pct, take_profit_pct)
        core.get_bot_engine().journal.add("manual_connection_test", {"requestedSymbol": symbol, "executedSymbol": symbol, "symbol": symbol, "side": side, "result": result})
        core.get_bot_engine().set_status("journal", "ok")
        core.json_response(self, 200, result)

    def _start_bot(self, payload):
        with core.BOT_LOCK:
            core.check_and_reset_daily_state(core.BOT_STATE)
            current = dict(core.BOT_STATE)
        try:
            config = validate_start_payload(payload, current)
            config["interval"] = normalize_interval(config.get("interval", current.get("interval", "5")))
        except ValueError as exc:
            core.json_response(self, 400, {"ok": False, "enabled": False, "reason": str(exc)})
            return
        reached, reason = fail_closed_daily_risk(core, {**current, **config})
        if reached:
            core.json_response(self, 200, {"ok": False, "enabled": False, "reason": reason})
            return
        universe = core.top_gainer_universe(force=True, limit=_scan_limit())
        advance_generation()
        with core.BOT_LOCK:
            core.BOT_STATE.update({**config, "enabled": True, "autoPick": True, "scanSymbols": bounded_symbols(universe["symbols"], _scan_limit()), "symbolSource": universe["source"], "selectedSignalSymbol": config["symbol"], "universe": universe, "lastReason": f"Auto trader started in {config['mode']} mode with server-bounded scanner and risk settings."})
        core.ensure_bot_thread()
        core.json_response(self, 200, {"ok": True, "bot": core.bot_tick()})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/bybit/kill-switch":
            payload = self._read_authorized_payload()
            if payload is None:
                return
            advance_generation()
            core.json_response(self, 200, verified_kill_switch_result())
            return
        if path == "/api/bybit/demo-order":
            payload = self._read_authorized_payload()
            if payload is not None:
                self._manual_connection_test(payload)
            return
        if path == "/api/bot/start":
            payload = self._read_authorized_payload()
            if payload is not None:
                self._start_bot(payload)
            return
        if path == "/api/bot/stop":
            advance_generation()
        return super().do_POST()


def install_guards() -> None:
    _install_scanner_guards()
    core.bot_tick = guarded_bot_tick
    core.daily_loss_cap_reached = lambda state: fail_closed_daily_risk(core, state)
    _install_execute_guard()


def run() -> None:
    install_guards()
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), GuardedHandler)
    print(f"Guarded Bybit demo backend running on http://{host}:{port}", flush=True)
    print(f"Reading environment from {core.ENV_PATH}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
