"""Step 1 strategy upgrade: session-aware ORB and 1H/15M/5M confluence.

The upgrade is installed at runtime and keeps automatic execution enabled. It
replaces the legacy UTC-midnight ORB vote and filters actionable strategy votes
that conflict with the active 1H/15M/5M structure.
"""

from __future__ import annotations

import os
from datetime import datetime, time as clock_time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .engines import bot_engine
    from .engines.indicators import avg_volume, candle_direction, ema, trend_direction
except ImportError:  # pragma: no cover
    from engines import bot_engine
    from engines.indicators import avg_volume, candle_direction, ema, trend_direction

_INSTALLED_ATTR = "_strategy_step1_upgrade_installed"


def _parse_clock(value: str, default: str) -> clock_time:
    raw = str(value or default).strip()
    try:
        hour, minute = raw.split(":", 1)
        return clock_time(int(hour), int(minute))
    except (TypeError, ValueError):
        hour, minute = default.split(":", 1)
        return clock_time(int(hour), int(minute))


def settings() -> dict[str, Any]:
    return {
        "timezone": os.environ.get("ORB_TIMEZONE", "Asia/Dhaka"),
        "rangeMinutes": max(15, min(120, int(os.environ.get("ORB_RANGE_MINUTES", "60")))),
        "minimumVolumeRatio": max(1.0, min(3.0, float(os.environ.get("ORB_MIN_VOLUME_RATIO", "1.10")))),
        "sessions": {
            "LONDON": {
                "start": os.environ.get("ORB_LONDON_START", "14:00"),
                "end": os.environ.get("ORB_LONDON_END", "17:00"),
            },
            "NEW_YORK": {
                "start": os.environ.get("ORB_NEW_YORK_START", "20:00"),
                "end": os.environ.get("ORB_NEW_YORK_END", "23:30"),
            },
        },
    }


def _candle_dt(candle: dict[str, Any], tz: ZoneInfo) -> datetime:
    return datetime.fromtimestamp(int(candle.get("time") or 0) / 1000, tz=ZoneInfo("UTC")).astimezone(tz)


def _active_session(reference: datetime, cfg: dict[str, Any]) -> tuple[str, datetime, datetime, datetime] | None:
    for name, session in cfg["sessions"].items():
        start_clock = _parse_clock(session.get("start"), "14:00")
        end_clock = _parse_clock(session.get("end"), "17:00")
        start = reference.replace(hour=start_clock.hour, minute=start_clock.minute, second=0, microsecond=0)
        end = reference.replace(hour=end_clock.hour, minute=end_clock.minute, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)
        range_end = start + timedelta(minutes=int(cfg["rangeMinutes"]))
        if start <= reference <= end:
            return name, start, range_end, end
    return None


def _body_ratio(candle: dict[str, Any]) -> float:
    span = max(float(candle["high"]) - float(candle["low"]), 1e-12)
    return abs(float(candle["close"]) - float(candle["open"])) / span


def session_orb_engine(tf1h: list[dict[str, Any]], tf15m: list[dict[str, Any]], tf5m: list[dict[str, Any]]) -> dict[str, Any]:
    if len(tf15m) < 20 or len(tf5m) < 21:
        return {"engine": "ORB", "signal": "WAIT", "reason": "Not enough closed candles for session ORB", "strength": 0}

    cfg = settings()
    try:
        tz = ZoneInfo(str(cfg["timezone"]))
    except Exception:
        tz = ZoneInfo("Asia/Dhaka")

    reference = _candle_dt(tf5m[-1], tz)
    session = _active_session(reference, cfg)
    if session is None:
        return {"engine": "ORB", "signal": "WAIT", "reason": "Outside configured London/New York ORB sessions", "strength": 0}

    name, start, range_end, end = session
    opening = [row for row in tf15m if start <= _candle_dt(row, tz) < range_end]
    if len(opening) < max(1, int(cfg["rangeMinutes"]) // 15):
        return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} opening range is not complete", "strength": 0}
    if reference < range_end or reference > end:
        return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} ORB setup is outside its valid breakout window", "strength": 0}

    high = max(float(row["high"]) for row in opening)
    low = min(float(row["low"]) for row in opening)
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    previous5 = tf5m[-2]
    baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5.get("volume") or 0) / baseline if baseline > 0 else 0
    body = _body_ratio(last5)
    trend = trend_direction(tf1h)

    buy_break = float(last15["close"]) > high and float(last5["close"]) > high
    sell_break = float(last15["close"]) < low and float(last5["close"]) < low
    buy_follow = float(last5["close"]) > float(previous5["close"])
    sell_follow = float(last5["close"]) < float(previous5["close"])

    if buy_break:
        if trend == "Sell":
            return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} bullish ORB blocked by 1H bearish trend", "strength": 0}
        if volume_ratio < float(cfg["minimumVolumeRatio"]) or body < 0.40 or not buy_follow:
            return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} bullish ORB needs volume, body, and 5M follow-through", "strength": 0}
        distance_pct = ((float(last5["close"]) - high) / high) * 100 if high else 0
        strength = min(5.0, 2.5 + min(distance_pct, 1.0) + min(volume_ratio - 1.0, 1.0) + body)
        return {"engine": "ORB", "signal": "Buy", "reason": f"{name} session range high broken with 15M close and 5M confirmation", "strength": round(strength, 2), "session": name, "rangeHigh": high, "rangeLow": low}

    if sell_break:
        if trend == "Buy":
            return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} bearish ORB blocked by 1H bullish trend", "strength": 0}
        if volume_ratio < float(cfg["minimumVolumeRatio"]) or body < 0.40 or not sell_follow:
            return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} bearish ORB needs volume, body, and 5M follow-through", "strength": 0}
        distance_pct = ((low - float(last5["close"])) / low) * 100 if low else 0
        strength = min(5.0, 2.5 + min(distance_pct, 1.0) + min(volume_ratio - 1.0, 1.0) + body)
        return {"engine": "ORB", "signal": "Sell", "reason": f"{name} session range low broken with 15M close and 5M confirmation", "strength": round(strength, 2), "session": name, "rangeHigh": high, "rangeLow": low}

    return {"engine": "ORB", "signal": "WAIT", "reason": f"{name} opening range has not produced a confirmed breakout", "strength": 0, "session": name, "rangeHigh": high, "rangeLow": low}


def confluence(tf1h: list[dict[str, Any]], tf15m: list[dict[str, Any]], tf5m: list[dict[str, Any]]) -> dict[str, Any]:
    if len(tf1h) < 55 or len(tf15m) < 55 or len(tf5m) < 3:
        return {"ready": False, "direction": "WAIT", "reason": "Insufficient 1H/15M/5M history"}

    direction1h = trend_direction(tf1h)
    closes15 = [float(row["close"]) for row in tf15m]
    ema20 = ema(closes15, 20)
    ema50 = ema(closes15, 50)
    if not ema20 or not ema50:
        return {"ready": False, "direction": "WAIT", "reason": "15M EMA confluence unavailable"}

    last15 = closes15[-1]
    if ema20[-1] > ema50[-1] and last15 >= ema20[-1]:
        direction15 = "Buy"
    elif ema20[-1] < ema50[-1] and last15 <= ema20[-1]:
        direction15 = "Sell"
    else:
        direction15 = "WAIT"

    last5 = tf5m[-1]
    previous5 = tf5m[-2]
    direction5 = candle_direction(last5)
    follow = (
        direction5 == "Buy" and float(last5["close"]) > float(previous5["close"])
    ) or (
        direction5 == "Sell" and float(last5["close"]) < float(previous5["close"])
    )
    if not follow:
        direction5 = "WAIT"

    aligned = direction1h if direction1h == direction15 == direction5 else "WAIT"
    return {
        "ready": True,
        "direction": aligned,
        "direction1H": direction1h,
        "direction15M": direction15,
        "direction5M": direction5,
        "reason": "1H, 15M, and 5M aligned" if aligned != "WAIT" else "1H/15M/5M directions are not fully aligned",
    }


def _filter_votes(votes: list[dict[str, Any]], mtf: dict[str, Any]) -> list[dict[str, Any]]:
    allowed = mtf.get("direction")
    filtered: list[dict[str, Any]] = []
    for item in votes:
        vote = dict(item)
        signal = vote.get("signal")
        if signal in {"Buy", "Sell"} and signal != allowed:
            vote["signal"] = "WAIT"
            vote["strength"] = 0
            vote["reason"] = f"{vote.get('engine')} blocked: {mtf.get('reason')}"
            vote["mtfConfluence"] = mtf
        elif signal in {"Buy", "Sell"}:
            vote["mtfConfluence"] = mtf
        filtered.append(vote)
    return filtered


def install(core: Any) -> None:
    if getattr(core, _INSTALLED_ATTR, False):
        return

    original_strategies = bot_engine.BotEngineV2.strategies

    def upgraded_strategies(self: Any, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        votes = [dict(row) for row in original_strategies(self, snapshot)]
        tf = snapshot["timeframes"]
        orb = session_orb_engine(tf["1H"], tf["15M"], tf["5M"])
        votes = [orb if row.get("engine") == "ORB" else row for row in votes]
        return _filter_votes(votes, confluence(tf["1H"], tf["15M"], tf["5M"]))

    bot_engine.BotEngineV2.strategies = upgraded_strategies
    setattr(core, _INSTALLED_ATTR, True)


def status(core: Any) -> dict[str, Any]:
    return {
        "installed": bool(getattr(core, _INSTALLED_ATTR, False)),
        "features": ["LONDON_NEW_YORK_SESSION_ORB", "MTF_1H_15M_5M_CONFLUENCE"],
        "settings": settings(),
    }
