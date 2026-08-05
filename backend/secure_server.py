"""Canonical authenticated runtime for the Bybit Demo bot."""

from __future__ import annotations

import os
import time
import urllib.parse
from http.server import ThreadingHTTPServer

try:
    from . import agreement_contract_filter, agreement_execution_guard, analytics_runtime
    from . import authoritative_daily_risk
    from . import bybit_endpoint_policy, cost_policy_fix, execution_handoff, intraday_scanner
    from . import execution_handoff_safety_hotfix, execution_idempotency
    from . import execution_idempotency_race_fix, execution_idempotency_review_fix
    from . import live_execution_ledger
    from . import position_synced_server as verified
    from . import replay_collector, replay_engine, replay_performance_journal
    from . import replay_safety, replay_sessions, replay_visualization
    from . import runtime_orchestrator, setup_worker, worker
    from .durable_runtime import install as install_durable_runtime
    from .intraday_scanner import (
        install as install_intraday_scanner,
        normalize_scanner_interval,
        normalize_symbols,
        scan_lock,
        settings as scanner_settings,
    )
    from .replay_accuracy import install as install_replay_accuracy
    from .runtime_security import authorize_get, handle_options, install as install_runtime_security, reject_disallowed_origin
    from .scanner_execution_gate import install as install_scanner_execution_gate
    from .scanner_review_fixes import install as install_scanner_review_fixes
except ImportError:
    import agreement_contract_filter
    import agreement_execution_guard
    import analytics_runtime
    import authoritative_daily_risk
    import bybit_endpoint_policy
    import cost_policy_fix
    import execution_handoff
    import execution_handoff_safety_hotfix
    import execution_idempotency
    import execution_idempotency_race_fix
    import execution_idempotency_review_fix
    import intraday_scanner
    import live_execution_ledger
    import position_synced_server as verified
    import replay_collector
    import replay_engine
    import replay_performance_journal
    import replay_safety
    import replay_sessions
    import replay_visualization
    import runtime_orchestrator
    import setup_worker
    import worker
    from durable_runtime import install as install_durable_runtime
    from intraday_scanner import (
        install as install_intraday_scanner,
        normalize_scanner_interval,
        normalize_symbols,
        scan_lock,
        settings as scanner_settings,
    )
    from replay_accuracy import install as install_replay_accuracy
    from runtime_security import authorize_get, handle_options, install as install_runtime_security, reject_disallowed_origin
    from scanner_execution_gate import install as install_scanner_execution_gate
    from scanner_review_fixes import install as install_scanner_review_fixes

core = verified.guarded.core


def _scanner_response(handler) -> None:
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(handler.path).query))
    cfg = scanner_settings()
    try:
        interval = normalize_scanner_interval(query.get("interval", "5"))
        mode = core.normalize_mode(query.get("mode", "balanced"))
    except ValueError as exc:
        core.json_response(handler, 400, {"ok": False, "error": str(exc)})
        return

    universe = core.top_gainer_universe(
        force=query.get("forceUniverse", "0").lower() in {"1", "true", "yes"},
        limit=int(cfg["deepScanSize"]),
    )
    requested_raw = query.get("symbols", "")
    requested = requested_raw.split(",") if requested_raw else list(universe.get("symbols") or [])
    requested, agreement_rejected = agreement_contract_filter.filter_symbols(requested)
    symbols, rejected = normalize_symbols(requested, int(cfg["deepScanSize"]))
    rejected = list(dict.fromkeys([*agreement_rejected, *rejected]))
    if not symbols:
        core.json_response(handler, 400, {"ok": False, "error": "No eligible USDT symbols supplied", "rejectedSymbols": rejected})
        return

    lock = scan_lock()
    if not lock.acquire(blocking=False):
        core.json_response(handler, 429, {"ok": False, "error": "Scanner request already in progress"})
        return
    started = time.monotonic()
    rows = []
    timed_out = False
    market_rows = {row.get("symbol"): row for row in universe.get("rows") or []}
    try:
        for symbol in symbols:
            if rows and time.monotonic() - started >= float(cfg["deadlineSeconds"]):
                timed_out = True
                break
            signal, reason, votes, router, indicators, engine_status = core.evaluate_signal(symbol, interval, mode)
            market = market_rows.get(symbol, {})
            strong_votes = sum(1 for vote in votes or [] if vote.get("signal") == signal)
            cost_tier = market.get("costTier", "blocked")
            eligible = cost_tier != "blocked" and not (cost_tier == "strong_only" and strong_votes < 2)
            if signal in {"Buy", "Sell"} and not eligible:
                signal = "WAIT"
                reason = f"Scanner cost tier {cost_tier} is not eligible for this signal strength"
            row = {
                "symbol": symbol, "signal": signal, "reason": reason,
                "changePct": market.get("changePct"), "turnover24h": market.get("turnover24h"),
                "spreadPct": market.get("spreadPct"), "atr15mPct": market.get("atr15mPct"),
                "volumeRatio": market.get("volumeRatio"), "rankScore": market.get("rankScore"),
                "costTier": cost_tier, "engineVotes": votes, "router": router,
                "indicators": indicators, "engineStatus": engine_status,
            }
            row["score"] = core.signal_score(row)
            rows.append(row)
    finally:
        lock.release()

    rows.sort(key=lambda row: row["score"], reverse=True)
    core.json_response(handler, 200, {
        "ok": True, "interval": interval, "mode": mode, "rows": rows, "universe": universe,
        "scanMeta": {
            "requested": len(requested) + len(agreement_rejected), "accepted": len(symbols), "rejected": len(rejected),
            "rejectedSymbols": rejected, "completed": len(rows), "timedOut": timed_out,
            "deadlineSeconds": cfg["deadlineSeconds"], "shortlistSize": cfg["shortlistSize"],
            "deepScanSize": cfg["deepScanSize"],
        },
        "scanSeconds": 30,
        "topGainerRefreshSeconds": cfg["refreshSeconds"],
    })


class SecurePositionSyncedHandler(verified.PositionSyncedHandler):
    """Apply origin, authentication, scanner safety, durable state, and worker status."""

    def do_OPTIONS(self):
        handle_options(self)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if reject_disallowed_origin(self):
            return
        if not authorize_get(self, path):
            return
        if live_execution_ledger.handle_get(self, core, path):
            return
        if analytics_runtime.handle_get(self, core, path):
            return
        if replay_collector.handle_get(self, core, path):
            return
        if replay_performance_journal.handle_get(self, core, path):
            return
        if replay_visualization.handle_get(self, core, path):
            return
        if replay_sessions.handle_get(self, core, path):
            return
        if path == "/api/bot/scanner":
            _scanner_response(self)
            return
        if path == "/api/replay/status":
            core.json_response(self, 200, replay_safety.policy_status())
            return
        if path == "/api/durable-state/status":
            core.json_response(self, 200, core.durable_state_status())
            return
        if path == "/api/workers/status":
            core.json_response(self, 200, {
                "ok": True,
                "runtime": runtime_orchestrator.snapshot(),
                "symbolSelection": worker.snapshot(),
                "setupVerification": setup_worker.snapshot(),
                "executionHandoff": execution_handoff.snapshot(),
                "executionConnected": True,
                "demoEndpointPolicy": bybit_endpoint_policy.policy_status(),
                "agreementContractFilter": agreement_contract_filter.status(),
                "agreementExecutionGuard": agreement_execution_guard.status(core),
                "costPolicyFixInstalled": bool(getattr(core, "_cost_policy_fix_installed", False)),
                "executionHandoffSafetyHotfixInstalled": bool(
                    getattr(core, "_execution_handoff_p0_03_hotfix_installed", False)
                ),
                "executionIdempotency": execution_idempotency.status(core, execution_handoff),
                "executionIdempotencyRaceFix": execution_idempotency_race_fix.status(core, execution_handoff),
                "executionIdempotencyReviewFix": execution_idempotency_review_fix.status(
                    core, execution_handoff
                ),
            })
            return
        if path == "/api/workers/symbols":
            core.json_response(self, 200, {"ok": True, **worker.snapshot()})
            return
        if path == "/api/workers/setups":
            core.json_response(self, 200, {"ok": True, **setup_worker.snapshot()})
            return
        if path == "/api/workers/execution":
            core.json_response(self, 200, {"ok": True, **execution_handoff.snapshot()})
            return
        return super().do_GET()

    def _read_replay_payload(self):
        if not self.is_authorized():
            core.json_response(self, 401, {"ok": False, "error": "Unauthorized"})
            return None
        try:
            payload = core.read_json(self)
        except Exception as exc:
            core.json_response(self, 400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return None
        try:
            return replay_safety.validate_replay_request(payload)
        except replay_safety.ReplaySafetyViolation as exc:
            core.json_response(
                self,
                400,
                {
                    "ok": False,
                    "code": "REPLAY_SAFETY_VIOLATION",
                    "error": str(exc),
                    "safety": replay_safety.policy_status(),
                },
            )
            return None

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if reject_disallowed_origin(self):
            return
        if path == live_execution_ledger.SYNC_PATH:
            payload = self._read_authorized_payload()
            if payload is None:
                return
            live_execution_ledger.handle_post(self, core, path, payload)
            return
        if path == "/api/replay/data/sync":
            payload = self._read_replay_payload()
            if payload is None:
                return
            replay_collector.handle_post(self, core, path, payload)
            return
        if replay_sessions.is_post_path(path):
            payload = self._read_replay_payload()
            if payload is None:
                return
            replay_sessions.handle_post(self, core, path, payload)
            return
        if replay_engine.is_post_path(path):
            payload = self._read_replay_payload()
            if payload is None:
                return
            replay_engine.handle_post(self, core, path, payload)
            return
        return super().do_POST()


def install_secure_runtime() -> None:
    """Install verified controls and start automatic guarded execution."""
    bybit_endpoint_policy.install(core)
    install_runtime_security(core)
    verified.guarded.install_guards()
    install_intraday_scanner(core)
    agreement_contract_filter.install(core)
    cost_policy_fix.install(core, setup_worker, intraday_scanner)
    install_scanner_review_fixes(core)
    install_scanner_execution_gate(core)
    install_replay_accuracy(core)
    verified.install_position_management()
    verified.install_mandatory_entry_protection()
    core.existing_position_guard = verified._protected_existing_position_guard
    install_durable_runtime(core)
    live_execution_ledger.install(core)
    authoritative_daily_risk.install(core)
    replay_collector.install(core)
    replay_sessions.install(core)
    replay_engine.install(core)
    replay_performance_journal.install(core)
    replay_visualization.install(core)
    with core.BOT_LOCK:
        core.BOT_STATE["maxOpenPositions"] = 3
    execution_idempotency.install(core, execution_handoff)
    execution_idempotency_race_fix.install(core, execution_handoff)
    execution_idempotency_review_fix.install(core, execution_handoff)
    execution_handoff_safety_hotfix.install(core, execution_handoff)
    agreement_execution_guard.install(core, setup_worker, execution_handoff)
    runtime_orchestrator.start(core, worker, setup_worker, execution_handoff)


def run() -> None:
    install_secure_runtime()
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), SecurePositionSyncedHandler)
    print(f"Secure Bybit demo backend with automatic guarded execution running on http://{host}:{port}", flush=True)
    print(f"Bybit endpoint policy: {bybit_endpoint_policy.policy_status()}", flush=True)
    print(f"Agreement execution guard: {agreement_execution_guard.status(core)}", flush=True)
    print(f"Reading environment from {core.ENV_PATH}", flush=True)
    print(f"Durable state: {core.durable_state_status()}", flush=True)
    print(f"Worker runtime: {runtime_orchestrator.snapshot()}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    run()
