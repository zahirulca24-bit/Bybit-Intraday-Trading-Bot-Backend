"""Hardened strategy implementations shared by modular, canonical, and replay paths."""

from __future__ import annotations

from typing import Any

from .indicators import avg_volume, candle_direction, ema, near_value, rsi, swing_zone, trend_direction
from .strategies import orb_engine, vote

FIVE_MINUTES_MS = 5 * 60 * 1000
FIFTEEN_MINUTES_MS = 15 * 60 * 1000
MAX_CONFIRMATION_DELAY_MS = 20 * 60 * 1000
MAX_DIVERGENCE_AGE_BARS = 6
MAX_DIVERGENCE_PIVOT_DISTANCE_PCT = 2.5


def _avg_range(candles: list[dict[str, Any]]) -> float:
    if not candles:
        return 0.0
    return sum(max(float(row["high"]) - float(row["low"]), 0.0) for row in candles) / len(candles)


def _body_ratio(candle: dict[str, Any]) -> float:
    candle_range = max(float(candle["high"]) - float(candle["low"]), 0.00000001)
    return abs(float(candle["close"]) - float(candle["open"])) / candle_range


def _slope_pct(values: list[float], lookback: int = 3) -> float:
    if len(values) <= lookback or not values[-lookback - 1]:
        return 0.0
    return ((values[-1] - values[-lookback - 1]) / values[-lookback - 1]) * 100


def _structure_aligned(candles: list[dict[str, Any]], direction: str, lookback: int = 5) -> bool:
    window = candles[-lookback:]
    if len(window) < lookback:
        return False
    half = max(2, lookback // 2)
    first = window[:half]
    second = window[-half:]
    if direction == "Buy":
        return min(float(row["low"]) for row in second) >= min(float(row["low"]) for row in first)
    if direction == "Sell":
        return max(float(row["high"]) for row in second) <= max(float(row["high"]) for row in first)
    return False


def _vwap(candles: list[dict[str, Any]]) -> float | None:
    total_volume = sum(float(row["volume"]) for row in candles)
    if total_volume <= 0:
        return None
    weighted = sum(
        ((float(row["high"]) + float(row["low"]) + float(row["close"])) / 3)
        * float(row["volume"])
        for row in candles
    )
    return weighted / total_volume


def _vwap_structure_aligned(candles: list[dict[str, Any]], direction: str, lookback: int = 6) -> bool:
    window = candles[-lookback:]
    if len(window) < lookback:
        return False
    if direction == "Buy":
        return float(window[-1]["close"]) >= float(window[0]["close"])
    if direction == "Sell":
        return float(window[-1]["close"]) <= float(window[0]["close"])
    return False


def _pivot_points(
    candles: list[dict[str, Any]], field: str, kind: str, left: int = 2, right: int = 2
) -> list[tuple[int, float]]:
    pivots: list[tuple[int, float]] = []
    for index in range(left, len(candles) - right):
        value = float(candles[index][field])
        prior = [float(candles[i][field]) for i in range(index - left, index)]
        future = [float(candles[i][field]) for i in range(index + 1, index + right + 1)]
        if kind == "high" and value > max(prior) and value >= max(future):
            pivots.append((index, value))
        elif kind == "low" and value < min(prior) and value <= min(future):
            pivots.append((index, value))
    return pivots


def _rsi_at(closes: list[float], index: int, period: int = 14) -> float:
    return 50.0 if index < period else float(rsi(closes[: index + 1], period))


def _wick_ratio(candle: dict[str, Any], side: str) -> float:
    candle_range = max(float(candle["high"]) - float(candle["low"]), 0.00000001)
    if side == "Buy":
        wick = min(float(candle["open"]), float(candle["close"])) - float(candle["low"])
    else:
        wick = float(candle["high"]) - max(float(candle["open"]), float(candle["close"]))
    return max(wick, 0.0) / candle_range


def _time_ms(candle: dict[str, Any]) -> int:
    try:
        return int(candle.get("time") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _uses_exchange_timestamps(*candles: dict[str, Any]) -> bool:
    return all(_time_ms(row) >= 100_000_000_000 for row in candles)


def _ordered(candles: list[dict[str, Any]]) -> bool:
    times = [_time_ms(row) for row in candles]
    if not times or max(times) < 100_000_000_000:
        return True
    return all(current > previous for previous, current in zip(times, times[1:]))


def _five_minute_confirmation_after_15m(
    sweep_candle: dict[str, Any], confirmation_candle: dict[str, Any]
) -> bool:
    if not _uses_exchange_timestamps(sweep_candle, confirmation_candle):
        return True
    sweep_close = _time_ms(sweep_candle) + FIFTEEN_MINUTES_MS
    confirmation_open = _time_ms(confirmation_candle)
    return sweep_close <= confirmation_open <= sweep_close + MAX_CONFIRMATION_DELAY_MS


def _confirmed_retest(
    previous5: dict[str, Any], last5: dict[str, Any], level: float, side: str
) -> bool:
    if side == "Buy":
        return (
            float(previous5["close"]) > level
            and float(last5["low"]) <= level
            and float(last5["close"]) > level
        )
    return (
        float(previous5["close"]) < level
        and float(last5["high"]) >= level
        and float(last5["close"]) < level
    )


def _pivot_is_fresh(second_index: int, total: int, max_age: int = MAX_DIVERGENCE_AGE_BARS) -> bool:
    return 0 <= (total - 1 - second_index) <= max_age


def _pivot_separation_ok(first_index: int, second_index: int, minimum: int = 3, maximum: int = 18) -> bool:
    separation = second_index - first_index
    return minimum <= separation <= maximum


def _price_near_pivot(current_price: float, pivot_price: float) -> bool:
    if pivot_price <= 0:
        return False
    return abs((current_price - pivot_price) / pivot_price) * 100 <= MAX_DIVERGENCE_PIVOT_DISTANCE_PCT


def _trend_strength(
    separation_pct: float, slope_pct: float, body_ratio: float, volume_ratio: float
) -> float:
    score = 2.75
    if separation_pct >= 0.12:
        score += 0.5
    if abs(slope_pct) >= 0.08:
        score += 0.5
    if body_ratio >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    return min(5.0, score)


def _breakout_strength(
    close: float,
    level: float,
    volume_ratio: float,
    body_ratio: float,
    retested: bool,
) -> float:
    distance_pct = abs((close - level) / level) * 100 if level else 0.0
    score = 2.5
    if distance_pct >= 0.08:
        score += 0.75
    if body_ratio >= 0.55:
        score += 0.75
    if volume_ratio >= 1.35:
        score += 0.75
    if retested:
        score += 0.75
    return min(5.0, score)


def _divergence_strength(
    rsi_delta: float, body_ratio: float, volume_ratio: float, price_move_pct: float
) -> float:
    score = 2.5
    if rsi_delta >= 6:
        score += 0.75
    if body_ratio >= 0.35:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if price_move_pct >= 0.25:
        score += 0.5
    return min(5.0, score)


def _liquidity_strength(
    sweep_pct: float, wick_ratio: float, volume_ratio: float, body_ratio: float
) -> float:
    score = 2.5
    if sweep_pct >= 0.08:
        score += 0.5
    if wick_ratio >= 0.45:
        score += 0.75
    if volume_ratio >= 1.25:
        score += 0.75
    if body_ratio >= 0.3:
        score += 0.5
    return min(5.0, score)


def trend_following_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 55 or len(tf15m) < 25 or len(tf5m) < 21:
        return vote("Trend Follow", "WAIT", "Not enough closed candles for trend-follow confirmation")
    if not (_ordered(tf1h) and _ordered(tf15m) and _ordered(tf5m)):
        return vote("Trend Follow", "WAIT", "Candle timestamps are missing, duplicated, or out of order")

    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("Trend Follow", "WAIT", "1H EMA20/50 trend not clean")

    closes1h = [float(row["close"]) for row in tf1h]
    ema20_1h = ema(closes1h, 20)
    ema50_1h = ema(closes1h, 50)
    separation_pct = abs((ema20_1h[-1] - ema50_1h[-1]) / closes1h[-1]) * 100 if closes1h[-1] else 0
    slope = _slope_pct(ema20_1h, 3)
    slope_ok = (direction == "Buy" and slope > 0.04) or (direction == "Sell" and slope < -0.04)
    if separation_pct < 0.06 or not slope_ok:
        return vote("Trend Follow", "WAIT", "1H trend lacks EMA separation or slope strength")

    closes15 = [float(row["close"]) for row in tf15m]
    ema20_15 = ema(closes15, 20)
    if not ema20_15:
        return vote("Trend Follow", "WAIT", "15M EMA setup unavailable")
    last15 = tf15m[-1]
    if not _structure_aligned(tf15m, direction, 6):
        return vote("Trend Follow", "WAIT", f"1H {direction}; 15M structure is not aligned")

    ema_ref = ema20_15[-1]
    if direction == "Buy":
        pullback = float(last15["low"]) <= ema_ref <= float(last15["close"]) or near_value(float(last15["close"]), ema_ref, 0.28)
        overextended = ((float(last15["close"]) - ema_ref) / ema_ref) * 100 > 0.9 if ema_ref else True
    else:
        pullback = float(last15["high"]) >= ema_ref >= float(last15["close"]) or near_value(float(last15["close"]), ema_ref, 0.28)
        overextended = ((ema_ref - float(last15["close"])) / ema_ref) * 100 > 0.9 if ema_ref else True
    if not pullback:
        return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 15M EMA pullback/reclaim")
    if overextended:
        return vote("Trend Follow", "WAIT", f"1H {direction}; 15M close is overextended from EMA20")

    last5, prev5 = tf5m[-1], tf5m[-2]
    entry = candle_direction(last5)
    follow = float(last5["close"]) > float(prev5["close"]) if direction == "Buy" else float(last5["close"]) < float(prev5["close"])
    volume_base = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5["volume"]) / volume_base if volume_base > 0 else 0
    body = _body_ratio(last5)
    if entry != direction or not follow or body < 0.35 or volume_ratio < 0.95:
        return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 5M body, volume, and follow-through")

    return vote(
        "Trend Follow",
        direction,
        f"1H {direction} trend, 15M EMA reclaim, 5M follow-through confirmed",
        _trend_strength(separation_pct, slope, body, volume_ratio),
    )


def sr_breakout_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 31 or len(tf15m) < 30 or len(tf5m) < 21:
        return vote("S/R Breakout", "WAIT", "Not enough closed structure for breakout confirmation")
    if not (_ordered(tf1h) and _ordered(tf15m) and _ordered(tf5m)):
        return vote("S/R Breakout", "WAIT", "Candle timestamps are missing, duplicated, or out of order")

    support, resistance = swing_zone(tf1h[-31:-1], 30)
    last5, prev5, last15 = tf5m[-1], tf5m[-2], tf15m[-1]
    range15 = max(float(row["high"]) for row in tf15m[-8:]) - min(float(row["low"]) for row in tf15m[-8:])
    avg_range15 = _avg_range(tf15m[-30:])
    avg_range5 = _avg_range(tf5m[-21:-1])
    consolidating = avg_range15 > 0 and range15 <= avg_range15 * 4
    volume_base = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5["volume"]) / volume_base if volume_base > 0 else 0
    body = _body_ratio(last5)
    oversized = avg_range5 > 0 and (float(last5["high"]) - float(last5["low"])) > avg_range5 * 3.5

    if float(last5["high"]) > resistance and float(last5["close"]) <= resistance:
        return vote("S/R Breakout", "WAIT", "False breakout: wick pierced resistance but 5M closed back inside")
    if float(last5["low"]) < support and float(last5["close"]) >= support:
        return vote("S/R Breakout", "WAIT", "False breakdown: wick pierced support but 5M closed back inside")

    if float(last5["close"]) > resistance:
        if not consolidating or oversized:
            return vote("S/R Breakout", "WAIT", "Breakout rejected: expanded structure or abnormal 5M candle")
        if float(last15["close"]) <= resistance or body < 0.45 or volume_ratio < 1.2:
            return vote("S/R Breakout", "WAIT", "Resistance break needs 15M close, strong body, and volume confirmation")
        retested = _confirmed_retest(prev5, last5, resistance, "Buy")
        reason = "Confirmed resistance breakout and retest" if retested else "Confirmed resistance breakout"
        return vote("S/R Breakout", "Buy", reason, _breakout_strength(float(last5["close"]), resistance, volume_ratio, body, retested))

    if float(last5["close"]) < support:
        if not consolidating or oversized:
            return vote("S/R Breakout", "WAIT", "Breakdown rejected: expanded structure or abnormal 5M candle")
        if float(last15["close"]) >= support or body < 0.45 or volume_ratio < 1.2:
            return vote("S/R Breakout", "WAIT", "Support break needs 15M close, strong body, and volume confirmation")
        retested = _confirmed_retest(prev5, last5, support, "Sell")
        reason = "Confirmed support breakdown and retest" if retested else "Confirmed support breakdown"
        return vote("S/R Breakout", "Sell", reason, _breakout_strength(float(last5["close"]), support, volume_ratio, body, retested))

    return vote("S/R Breakout", "WAIT", "No confirmed 1H support/resistance breakout")


def rsi_divergence_engine(tf1h, tf15m, tf5m):
    if len(tf15m) < 35 or len(tf5m) < 21:
        return vote("RSI Divergence", "WAIT", "Not enough closed candles for pivot RSI divergence")
    if not (_ordered(tf15m) and _ordered(tf5m)):
        return vote("RSI Divergence", "WAIT", "Candle timestamps are missing, duplicated, or out of order")
    if not _five_minute_confirmation_after_15m(tf15m[-1], tf5m[-1]):
        return vote("RSI Divergence", "WAIT", "5M reversal is not chronologically aligned after the latest closed 15M candle")

    direction = trend_direction(tf1h)
    closes15 = [float(row["close"]) for row in tf15m]
    recent = tf15m[-34:]
    offset = len(tf15m) - len(recent)
    lows = _pivot_points(recent, "low", "low")
    highs = _pivot_points(recent, "high", "high")
    rsi_now = float(rsi(closes15, 14))
    last5, prev5 = tf5m[-1], tf5m[-2]
    entry = candle_direction(last5)
    body = _body_ratio(last5)
    volume_base = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5["volume"]) / volume_base if volume_base > 0 else 0
    follow_buy = float(last5["close"]) > float(prev5["close"])
    follow_sell = float(last5["close"]) < float(prev5["close"])
    current15_close = float(tf15m[-1]["close"])

    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        first_idx, second_idx = offset + first[0], offset + second[0]
        fresh = _pivot_is_fresh(second_idx, len(tf15m))
        separated = _pivot_separation_ok(first_idx, second_idx)
        near_pivot = _price_near_pivot(current15_close, second[1])
        rsi_first, rsi_second = _rsi_at(closes15, first_idx), _rsi_at(closes15, second_idx)
        rsi_delta = rsi_second - rsi_first
        price_move_pct = abs((second[1] - first[1]) / first[1]) * 100 if first[1] else 0
        if (
            fresh and separated and near_pivot and second[1] < first[1] and rsi_delta >= 4
            and rsi_now <= 52 and entry == "Buy" and follow_buy and body >= 0.3
            and volume_ratio >= 1.0 and direction != "Sell"
        ):
            result = vote(
                "RSI Divergence",
                "Buy",
                f"Fresh 15M bullish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}",
                _divergence_strength(rsi_delta, body, volume_ratio, price_move_pct),
            )
            result["setupKey"] = f"rsi-divergence:Buy:{_time_ms(tf15m[second_idx])}:{second[1]:.12g}"
            return result

    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        first_idx, second_idx = offset + first[0], offset + second[0]
        fresh = _pivot_is_fresh(second_idx, len(tf15m))
        separated = _pivot_separation_ok(first_idx, second_idx)
        near_pivot = _price_near_pivot(current15_close, second[1])
        rsi_first, rsi_second = _rsi_at(closes15, first_idx), _rsi_at(closes15, second_idx)
        rsi_delta = rsi_first - rsi_second
        price_move_pct = abs((second[1] - first[1]) / first[1]) * 100 if first[1] else 0
        if (
            fresh and separated and near_pivot and second[1] > first[1] and rsi_delta >= 4
            and rsi_now >= 48 and entry == "Sell" and follow_sell and body >= 0.3
            and volume_ratio >= 1.0 and direction != "Buy"
        ):
            result = vote(
                "RSI Divergence",
                "Sell",
                f"Fresh 15M bearish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}",
                _divergence_strength(rsi_delta, body, volume_ratio, price_move_pct),
            )
            result["setupKey"] = f"rsi-divergence:Sell:{_time_ms(tf15m[second_idx])}:{second[1]:.12g}"
            return result

    return vote("RSI Divergence", "WAIT", f"No fresh, nearby pivot divergence, RSI {rsi_now:.1f}")


def vwap_bounce_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 55 or len(tf15m) < 40 or len(tf5m) < 21:
        return vote("VWAP Bounce", "WAIT", "Not enough closed candles for VWAP confirmation")
    if not (_ordered(tf1h) and _ordered(tf15m) and _ordered(tf5m)):
        return vote("VWAP Bounce", "WAIT", "Candle timestamps are missing, duplicated, or out of order")

    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("VWAP Bounce", "WAIT", "1H trend not clean for VWAP bounce")
    vwap = _vwap(tf15m[-40:])
    if vwap is None:
        return vote("VWAP Bounce", "WAIT", "15M VWAP volume unavailable")

    last15, prev15 = tf15m[-1], tf15m[-2]
    distance_pct = abs((float(last15["close"]) - vwap) / vwap) * 100 if vwap else 999
    if distance_pct > 0.35:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; 15M close is too far from VWAP")
    if not _vwap_structure_aligned(tf15m, direction, 6):
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; 15M VWAP structure is not aligned")

    if direction == "Buy":
        reclaim = float(last15["low"]) <= vwap <= float(last15["close"]) or (float(prev15["close"]) < vwap <= float(last15["close"]))
        overextended = ((float(last15["close"]) - vwap) / vwap) * 100 > 0.45
    else:
        reclaim = float(last15["high"]) >= vwap >= float(last15["close"]) or (float(prev15["close"]) > vwap >= float(last15["close"]))
        overextended = ((vwap - float(last15["close"])) / vwap) * 100 > 0.45
    if not reclaim:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for 15M VWAP reclaim/rejection")
    if overextended:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; VWAP bounce is already overextended")

    last5, prev5 = tf5m[-1], tf5m[-2]
    follow = float(last5["close"]) > float(prev5["close"]) if direction == "Buy" else float(last5["close"]) < float(prev5["close"])
    volume_base = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5["volume"]) / volume_base if volume_base > 0 else 0
    body = _body_ratio(last5)
    if candle_direction(last5) != direction or not follow or body < 0.35 or volume_ratio < 0.95:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for 5M VWAP bounce body, volume, and follow-through")

    score = 2.75
    if distance_pct <= 0.18:
        score += 0.5
    if body >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    score += 0.5
    return vote("VWAP Bounce", direction, f"1H {direction}, 15M VWAP reclaim, 5M follow-through confirmed", min(5.0, score))


def liquidity_sweep_engine(tf1h, tf15m, tf5m):
    if len(tf15m) < 25 or len(tf5m) < 21:
        return vote("Liquidity Sweep", "WAIT", "Not enough closed candles for liquidity sweep confirmation")
    if not (_ordered(tf15m) and _ordered(tf5m)):
        return vote("Liquidity Sweep", "WAIT", "Candle timestamps are missing, duplicated, or out of order")

    direction = trend_direction(tf1h)
    recent, last15, last5, prev5 = tf15m[-21:-1], tf15m[-1], tf5m[-1], tf5m[-2]
    if not _five_minute_confirmation_after_15m(last15, last5):
        return vote("Liquidity Sweep", "WAIT", "5M follow-through is not chronologically aligned after the 15M sweep")

    prior_high = max(float(row["high"]) for row in recent)
    prior_low = min(float(row["low"]) for row in recent)
    volume_base = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = float(last5["volume"]) / volume_base if volume_base > 0 else 0
    body = _body_ratio(last5)
    swept_low = float(last15["low"]) < prior_low and float(last15["close"]) > prior_low
    swept_high = float(last15["high"]) > prior_high and float(last15["close"]) < prior_high

    if swept_low:
        wick = _wick_ratio(last15, "Buy")
        sweep_pct = ((prior_low - float(last15["low"])) / prior_low) * 100 if prior_low else 0
        follow = float(last5["close"]) > float(prev5["close"]) and candle_direction(last5) == "Buy"
        if direction == "Sell":
            return vote("Liquidity Sweep", "WAIT", "Bullish liquidity sweep blocked by strong 1H downtrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bullish sweep needs wick rejection, volume, and chronological 5M follow-through")
        result = vote("Liquidity Sweep", "Buy", "Bullish 15M stop-hunt reclaim with aligned 5M follow-through", _liquidity_strength(sweep_pct, wick, volume_ratio, body))
        result["setupKey"] = f"liquidity-sweep:Buy:{_time_ms(last15)}:{prior_low:.12g}"
        return result

    if swept_high:
        wick = _wick_ratio(last15, "Sell")
        sweep_pct = ((float(last15["high"]) - prior_high) / prior_high) * 100 if prior_high else 0
        follow = float(last5["close"]) < float(prev5["close"]) and candle_direction(last5) == "Sell"
        if direction == "Buy":
            return vote("Liquidity Sweep", "WAIT", "Bearish liquidity sweep blocked by strong 1H uptrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bearish sweep needs wick rejection, volume, and chronological 5M follow-through")
        result = vote("Liquidity Sweep", "Sell", "Bearish 15M stop-hunt reclaim with aligned 5M follow-through", _liquidity_strength(sweep_pct, wick, volume_ratio, body))
        result["setupKey"] = f"liquidity-sweep:Sell:{_time_ms(last15)}:{prior_high:.12g}"
        return result

    return vote("Liquidity Sweep", "WAIT", "No confirmed stop-hunt liquidity sweep")


def install(core: Any) -> None:
    """Install one authoritative strategy/router source into the canonical core."""
    from .router import route_votes

    core.trend_following_engine = trend_following_engine
    core.sr_breakout_engine = sr_breakout_engine
    core.rsi_divergence_engine = rsi_divergence_engine
    core.vwap_bounce_engine = vwap_bounce_engine
    core.liquidity_sweep_engine = liquidity_sweep_engine
    core.orb_engine = orb_engine
    core.route_votes = route_votes
    core._hardened_strategies_installed = True
