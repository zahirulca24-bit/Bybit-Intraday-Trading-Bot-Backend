"""Deterministic replay engine with closed-candle alignment and no look-ahead entry."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:
    from .engines.hardened_strategies import install as install_hardened_strategies
except ImportError:
    from engines.hardened_strategies import install as install_hardened_strategies

_INTERVAL_MS = {"5": 300_000, "15": 900_000, "60": 3_600_000}


def closed_at(candles: list[dict], decision_time: int, interval: str, limit: int = 120) -> list[dict]:
    """Return candles fully closed by decision_time, ordered and bounded."""
    duration = _INTERVAL_MS[interval]
    rows = [row for row in candles if int(row.get("time") or 0) + duration <= decision_time]
    rows.sort(key=lambda row: int(row.get("time") or 0))
    return rows[-limit:]


def deterministic_orb(core: Any, tf1h: list[dict], tf15m: list[dict], tf5m: list[dict], decision_time: int) -> dict:
    """Evaluate ORB using the replay decision day, never wall-clock time."""
    day = datetime.fromtimestamp(decision_time / 1000, tz=timezone.utc)
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    start_ms = int(midnight.timestamp() * 1000)
    opening = next((row for row in tf1h if int(row["time"]) >= start_ms), None)
    if not opening:
        return core.vote("ORB", "WAIT", "No closed 1H candle found for replay UTC day")
    high, low = opening["high"], opening["low"]
    last15, last5 = tf15m[-1], tf5m[-1]
    volume_ok = last5["volume"] >= core.avg_volume(tf5m, 20) * 1.08
    if last15["close"] > high and last5["close"] > high and volume_ok:
        return core.vote("ORB", "Buy", "Replay UTC opening range high broken, 15M/5M confirmed", last5["close"] - high)
    if last15["close"] < low and last5["close"] < low and volume_ok:
        return core.vote("ORB", "Sell", "Replay UTC opening range low broken, 15M/5M confirmed", low - last5["close"])
    return core.vote("ORB", "WAIT", "Replay opening range not confirmed on closed 15M/5M candles")


def replay(core: Any, symbol: str, horizon: str = "24h", mode: str = "balanced", stop_loss_pct: float = 0.8, take_profit_pct: float = 2.0) -> dict:
    horizon = str(horizon or "24h").lower()
    interval = "15" if horizon == "7d" else "5"
    limit = 700 if horizon == "7d" else 320
    lookahead = 16 if interval == "15" else 24
    step = 2 if interval == "15" else 3
    entry_tf, msg_entry = core.fetch_candles(symbol, interval, limit=limit)
    tf15, msg15 = core.fetch_candles(symbol, "15", limit=700)
    tf1h, msg1h = core.fetch_candles(symbol, "60", limit=220)
    if not entry_tf or not tf15 or not tf1h:
        return {"ok": False, "message": msg_entry if not entry_tf else msg15 if not tf15 else msg1h, "trades": []}

    entry_tf = sorted(entry_tf, key=lambda row: int(row["time"]))
    votes_by_engine: dict[str, dict[str, int]] = {}
    signal_counts = {"Buy": 0, "Sell": 0, "WAIT": 0}
    trades: list[dict] = []
    blocked_until = 0

    for index in range(80, max(80, len(entry_tf) - lookahead - 1), step):
        if index <= blocked_until:
            continue
        signal_candle = entry_tf[index]
        decision_time = int(signal_candle["time"]) + _INTERVAL_MS[interval]
        replay_entry = closed_at(entry_tf, decision_time, interval, 120)
        replay15 = closed_at(tf15, decision_time, "15", 120)
        replay1h = closed_at(tf1h, decision_time, "60", 120)
        if min(len(replay_entry), len(replay15), len(replay1h)) < 60:
            continue

        votes = [
            core.trend_following_engine(replay1h, replay15, replay_entry),
            core.sr_breakout_engine(replay1h, replay15, replay_entry),
            core.rsi_divergence_engine(replay1h, replay15, replay_entry),
            core.vwap_bounce_engine(replay1h, replay15, replay_entry),
            core.liquidity_sweep_engine(replay1h, replay15, replay_entry),
            deterministic_orb(core, replay1h, replay15, replay_entry, decision_time),
        ]
        for strategy_vote in votes:
            bucket = votes_by_engine.setdefault(strategy_vote["engine"], {"Buy": 0, "Sell": 0, "WAIT": 0})
            bucket[strategy_vote["signal"]] = bucket.get(strategy_vote["signal"], 0) + 1
        router = core.route_votes(votes, mode)
        signal = router["decision"]
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
        if signal not in {"Buy", "Sell"}:
            continue

        entry_index = index + 1
        entry = float(entry_tf[entry_index]["open"])
        future = entry_tf[entry_index:entry_index + lookahead]
        outcome, bars_held, exit_price = core.estimate_trade_outcome(signal, entry, future, stop_loss_pct, take_profit_pct)
        pnl_pct = ((exit_price - entry) / entry) * 100 if signal == "Buy" else ((entry - exit_price) / entry) * 100
        trades.append({
            "signalTime": int(signal_candle["time"]),
            "decisionTime": decision_time,
            "entryTime": int(entry_tf[entry_index]["time"]),
            "symbol": symbol,
            "side": signal,
            "entry": round(entry, 8),
            "exit": round(exit_price, 8),
            "outcome": outcome,
            "pnlPct": round(pnl_pct, 4),
            "barsHeld": bars_held,
            "router": router,
            "votes": votes,
        })
        blocked_until = entry_index + max(1, bars_held) - 1

    wins = sum(1 for row in trades if row["outcome"] == "win")
    losses = sum(1 for row in trades if row["outcome"] == "loss")
    return {
        "ok": True,
        "symbol": symbol,
        "horizon": horizon,
        "interval": interval,
        "mode": core.normalize_mode(mode),
        "candles": len(entry_tf),
        "stopLossPct": stop_loss_pct,
        "takeProfitPct": take_profit_pct,
        "methodology": {
            "closedCandlesOnly": True,
            "higherTimeframeAligned": True,
            "entry": "next_candle_open",
            "sameBarStopAndTarget": "stop_first_conservative",
            "orbClock": "replay_utc_timestamp",
            "strategyParity": True,
        },
        "summary": {
            "trades": len(trades), "wins": wins, "losses": losses,
            "flats": len(trades) - wins - losses,
            "winRate": round((wins / len(trades)) * 100, 2) if trades else 0,
            "estimatedPnlPct": round(sum(row["pnlPct"] for row in trades), 4),
        },
        "signalCounts": signal_counts,
        "votesByEngine": votes_by_engine,
        "trades": trades[-100:],
    }


def install(core: Any) -> None:
    if getattr(core, "_accurate_replay_installed", False):
        return
    install_hardened_strategies(core)
    core.replay_strategy_quality = lambda symbol, horizon="24h", mode="balanced", stop_loss_pct=0.8, take_profit_pct=2.0: replay(
        core, symbol, horizon, mode, stop_loss_pct, take_profit_pct
    )
    core._accurate_replay_installed = True
