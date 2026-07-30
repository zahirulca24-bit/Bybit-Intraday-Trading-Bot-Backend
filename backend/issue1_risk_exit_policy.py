"""Issue 1 policy: staged R exits and a restart-safe daily net-loss lock.

Policy:
- TP1 at 1.5R closes 40% of the original position and moves the stop to breakeven.
- TP2 at 2R closes 30% of the original position (50% of the remaining 60%).
- The final 30% receives a 0.5R trailing distance after TP2.
- New entries are blocked when realized daily net PnL reaches -5% of the
  trading-day starting equity. Existing position management remains active.
- There is no maximum-trades-per-day gate.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, Callable

try:
    from .engines import position_management as pm
except ImportError:
    from engines import position_management as pm

DAILY_LIMIT_PCT = 5.0
TP1_R = 1.5
TP1_CLOSE_PCT = 40.0
TP2_R = 2.0
TP2_CLOSE_OF_REMAINDER_PCT = 50.0
RUNNER_TRAIL_R = 0.5
_BASELINE_PREFIX = "daily_net_loss_baseline_v1"
_POSITION_PREFIX = "position_r_plan_v1"


def _store(core: Any) -> Any | None:
    store = getattr(core, "_durable_state_store", None)
    required = ("get", "put", "status")
    if store is None or any(not callable(getattr(store, name, None)) for name in required):
        return None
    try:
        status = dict(store.status() or {})
    except Exception:
        return None
    if not status.get("ok") or status.get("degraded") or not status.get("persistentPathConfigured"):
        return None
    return store


def _baseline_key(date_key: str) -> str:
    return f"{_BASELINE_PREFIX}:{date_key}"


def _position_plan_key(position: dict[str, Any]) -> str:
    return f"{_POSITION_PREFIX}:{pm.position_key(position)}"


def _daily_baseline(core: Any, date_key: str) -> tuple[float | None, str]:
    store = _store(core)
    if store is None:
        return None, "Persistent daily-risk store is unavailable"
    key = _baseline_key(date_key)
    saved = store.get(key)
    if isinstance(saved, dict):
        try:
            equity = float(saved.get("startingEquity") or 0)
        except (TypeError, ValueError):
            equity = 0
        if equity > 0:
            return equity, "Persistent trading-day starting equity loaded"
    equity, message = core.get_wallet_equity()
    if equity is None or float(equity) <= 0:
        return None, f"Trading-day starting equity unavailable: {message}"
    payload = {"dateKey": date_key, "startingEquity": round(float(equity), 8), "createdAt": int(time.time())}
    store.put(key, payload)
    confirmed = store.get(key)
    if not isinstance(confirmed, dict):
        return None, "Trading-day starting equity was not durably committed"
    return float(confirmed.get("startingEquity") or 0), "Trading-day starting equity committed"


def daily_net_loss_gate(core: Any, state: dict[str, Any]) -> tuple[bool, str]:
    """Block only new entries at -5% realized daily net PnL; no trade-count cap."""
    date_key = core.get_current_trading_date_key()
    baseline, baseline_message = _daily_baseline(core, date_key)
    if baseline is None or baseline <= 0:
        return True, f"Daily net-loss lock unavailable; execution blocked: {baseline_message}"
    net_pnl, pnl_message = core.get_daily_closed_pnl(date_key)
    if net_pnl is None:
        return True, f"Daily net PnL unavailable; execution blocked: {pnl_message}"
    limit = baseline * (DAILY_LIMIT_PCT / 100.0)
    net_pnl = float(net_pnl)
    blocked = net_pnl <= -limit
    evidence = {
        "dateKey": date_key,
        "startingEquity": round(baseline, 4),
        "limitPct": DAILY_LIMIT_PCT,
        "limitUsdt": round(limit, 4),
        "realizedNetPnl": round(net_pnl, 4),
        "remainingLossCapacity": round(max(0.0, limit + net_pnl), 4),
        "blocked": blocked,
        "tradeCountLimited": False,
        "source": "BYBIT_DAILY_CLOSED_PNL",
    }
    with core.BOT_LOCK:
        core.BOT_STATE["dailyRisk"] = evidence
        core.BOT_STATE["dailyLossCapUsdt"] = round(limit, 4)
        core.BOT_STATE["maxTradesPerDay"] = None
    if blocked:
        return True, f"Daily net-loss lock reached: {net_pnl:.2f} USDT / -{limit:.2f} USDT ({DAILY_LIMIT_PCT:.2f}% of starting equity)"
    return False, f"Daily net-loss lock OK: {net_pnl:.2f} USDT; limit -{limit:.2f} USDT; trade count unlimited"


def _load_or_create_plan(core: Any, position: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    store = _store(core)
    if store is None:
        return None, "Persistent position R-plan store is unavailable"
    key = _position_plan_key(position)
    existing = store.get(key)
    if isinstance(existing, dict):
        return dict(existing), "Persistent position R-plan loaded"
    avg = pm._positive(position.get("avgPrice"))
    stop = pm._positive(position.get("stopLoss"))
    size = pm._positive(position.get("size"))
    side = str(position.get("side") or "")
    if avg is None or stop is None or size is None or side not in {"Buy", "Sell"}:
        return None, "Entry price, original stop, size, or side is unavailable"
    risk_distance = (avg - stop) if side == "Buy" else (stop - avg)
    if risk_distance <= 0:
        return None, "Original stop is not on the risk side of entry"
    plan = {
        "version": 1,
        "positionKey": pm.position_key(position),
        "symbol": position.get("symbol"),
        "side": side,
        "entryPrice": str(avg),
        "originalStop": str(stop),
        "originalSize": str(size),
        "riskDistance": str(risk_distance),
        "createdAt": int(time.time()),
    }
    store.put(key, plan)
    confirmed = store.get(key)
    if not isinstance(confirmed, dict):
        return None, "Position R-plan was not durably committed"
    return dict(confirmed), "Persistent position R-plan committed"


def _r_multiple(position: dict[str, Any], plan: dict[str, Any]) -> float:
    avg = Decimal(str(plan["entryPrice"]))
    risk = Decimal(str(plan["riskDistance"]))
    mark = pm._positive(position.get("markPrice"))
    side = str(plan.get("side") or "")
    if mark is None or risk <= 0:
        return 0.0
    favorable = (mark - avg) if side == "Buy" else (avg - mark)
    return float(favorable / risk)


def _event_done(engine: Any, event: str, position: dict[str, Any]) -> bool:
    key = pm.position_key(position)
    done, verified = pm._completed(engine, event, key, key)
    return bool(done and verified)


def _record_stage(engine: Any, event: str, action: dict[str, Any], plan: dict[str, Any], r_value: float) -> None:
    payload = {
        **dict(action),
        "positionKey": plan.get("positionKey"),
        "rMultiple": round(r_value, 4),
        "originalSize": plan.get("originalSize"),
        "riskDistance": plan.get("riskDistance"),
        "verified": bool(action.get("verified")),
    }
    engine.journal.add(event if payload["verified"] else f"{event}_failed", payload)
    engine.set_status("journal", "ok")


def manage_positions(core: Any, state: dict[str, Any], *, attempts: int = 4, delay_seconds: float = 0.2, sleeper: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Manage TP1, TP2, breakeven, and the 30% runner with verified actions."""
    try:
        positions, message = core.get_open_positions()
    except Exception as exc:
        return {"ok": False, "actions": [], "failures": 1, "reason": f"Open-position fetch failed: {type(exc).__name__}"}
    if positions is None or not isinstance(positions, list):
        return {"ok": False, "actions": [], "failures": 1, "reason": str(message)}
    engine = core.get_bot_engine()
    actions: list[dict[str, Any]] = []
    failures = 0
    skipped = 0
    for position in positions:
        if not isinstance(position, dict) or pm._positive(position.get("size")) is None:
            continue
        plan, plan_reason = _load_or_create_plan(core, position)
        if plan is None:
            failures += 1
            actions.append({"type": "r_plan", "symbol": position.get("symbol"), "verified": False, "status": "failed", "reason": plan_reason})
            continue
        r_value = _r_multiple(position, plan)
        risk_fraction = float(Decimal(str(plan["riskDistance"])) / Decimal(str(plan["entryPrice"])))
        pnl_pct = r_value * risk_fraction * 100.0
        tp1_done = _event_done(engine, "tp1_1_5r", position)
        tp2_done = _event_done(engine, "tp2_2r", position)
        trailing_done = _event_done(engine, "runner_trailing_0_5r", position)
        if r_value >= TP1_R and not tp1_done:
            action = pm._partial(core, engine, position, pnl_pct, TP1_CLOSE_PCT, attempts, delay_seconds, sleeper)
            _record_stage(engine, "tp1_1_5r", action, plan, r_value)
            actions.append({**action, "stage": "TP1", "targetR": TP1_R})
            failures += int(not action.get("verified"))
            if action.get("verified"):
                avg = Decimal(str(plan["entryPrice"]))
                side = str(plan.get("side"))
                be_target = avg * (Decimal("1.0002") if side == "Buy" else Decimal("0.9998"))
                be = pm._breakeven(core, engine, position, pnl_pct, be_target, attempts, delay_seconds, sleeper)
                actions.append({**be, "stage": "TP1_BREAKEVEN"})
                failures += int(not be.get("verified"))
            continue
        if r_value >= TP2_R and tp1_done and not tp2_done:
            action = pm._partial(core, engine, position, pnl_pct, TP2_CLOSE_OF_REMAINDER_PCT, attempts, delay_seconds, sleeper)
            _record_stage(engine, "tp2_2r", action, plan, r_value)
            actions.append({**action, "stage": "TP2", "targetR": TP2_R, "originalPositionClosePct": 30.0})
            failures += int(not action.get("verified"))
            if not action.get("verified"):
                continue
            tp2_done = True
        if tp2_done and not trailing_done:
            risk_pct = risk_fraction * 100.0
            distance_pct = risk_pct * RUNNER_TRAIL_R
            action = pm._trailing(core, engine, position, pnl_pct, distance_pct, attempts, delay_seconds, sleeper)
            _record_stage(engine, "runner_trailing_0_5r", action, plan, r_value)
            actions.append({**action, "stage": "RUNNER", "trailR": RUNNER_TRAIL_R, "runnerOriginalPct": 30.0})
            failures += int(not action.get("verified"))
        elif tp1_done and r_value < TP2_R:
            skipped += 1
    engine.set_status("tradeManagement", "error" if failures else "ok")
    return {
        "ok": failures == 0,
        "actions": actions,
        "failures": failures,
        "skipped": skipped,
        "reason": f"R-based position management: {failures} failed, {skipped} waiting.",
        "policy": {
            "tp1": {"r": TP1_R, "closeOriginalPct": TP1_CLOSE_PCT, "moveStopToBreakeven": True},
            "tp2": {"r": TP2_R, "closeOriginalPct": 30.0},
            "runner": {"originalPct": 30.0, "activateAfterR": TP2_R, "trailingDistanceR": RUNNER_TRAIL_R},
        },
        "pendingPartialClose": pm.pending_partial_close(),
    }


def install(core: Any, verified_module: Any) -> None:
    """Install the policy into the already-verified runtime."""
    if getattr(core, "_issue1_risk_exit_policy_installed", False):
        return
    verified_module.manage_positions = manage_positions
    verified_module.guarded.fail_closed_daily_risk = daily_net_loss_gate
    with core.BOT_LOCK:
        core.BOT_STATE.update({
            "partialTpEnabled": True,
            "partialTpTriggerR": TP1_R,
            "partialTpClosePct": TP1_CLOSE_PCT,
            "tp2TriggerR": TP2_R,
            "tp2CloseOriginalPct": 30.0,
            "trailingStopEnabled": True,
            "trailingStopTriggerR": TP2_R,
            "trailingStopDistanceR": RUNNER_TRAIL_R,
            "dailyNetLossLimitPct": DAILY_LIMIT_PCT,
            "maxTradesPerDay": None,
        })
    core._issue1_risk_exit_policy_installed = True
