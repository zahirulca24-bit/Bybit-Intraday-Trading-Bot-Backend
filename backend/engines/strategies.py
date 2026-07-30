from .indicators import avg_volume, candle_direction, ema, near_value, rsi, swing_zone, trend_direction


def vote(engine, signal, reason, strength=0):
    return {
        "engine": engine,
        "signal": signal,
        "reason": reason,
        "strength": round(float(strength), 2),
    }


def _avg_range(candles):
    if not candles:
        return 0
    return sum(max(item["high"] - item["low"], 0) for item in candles) / len(candles)


def _body_ratio(candle):
    candle_range = max(candle["high"] - candle["low"], 0.00000001)
    return abs(candle["close"] - candle["open"]) / candle_range


def _slope_pct(values, lookback=3):
    if len(values) <= lookback or not values[-lookback - 1]:
        return 0
    return ((values[-1] - values[-lookback - 1]) / values[-lookback - 1]) * 100


def _structure_aligned(candles, direction, lookback=5):
    window = candles[-lookback:]
    if len(window) < lookback:
        return False
    first = window[: max(2, lookback // 2)]
    second = window[-max(2, lookback // 2):]
    if direction == "Buy":
        return min(item["low"] for item in second) >= min(item["low"] for item in first)
    if direction == "Sell":
        return max(item["high"] for item in second) <= max(item["high"] for item in first)
    return False


def _trend_follow_strength(separation_pct, slope_pct, body_ratio, volume_ratio, pullback_reclaim):
    score = 2.0
    if separation_pct >= 0.12:
        score += 0.5
    if abs(slope_pct) >= 0.08:
        score += 0.5
    if body_ratio >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if pullback_reclaim:
        score += 0.75
    return min(5.0, score)


def _vwap(candles):
    total_volume = sum(item["volume"] for item in candles)
    if total_volume <= 0:
        return None
    return sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in candles) / total_volume


def _vwap_strength(distance_pct, body_ratio, volume_ratio, reclaim, structure_ok):
    score = 2.0
    if distance_pct <= 0.18:
        score += 0.5
    if body_ratio >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if reclaim:
        score += 0.75
    if structure_ok:
        score += 0.5
    return min(5.0, score)


def _vwap_structure_aligned(candles, direction, lookback=6):
    window = candles[-lookback:]
    if len(window) < lookback:
        return False
    first_close = window[0]["close"]
    last_close = window[-1]["close"]
    if direction == "Buy":
        return last_close >= first_close
    if direction == "Sell":
        return last_close <= first_close
    return False


def _pivot_points(candles, field, kind, left=2, right=2):
    pivots = []
    for index in range(left, len(candles) - right):
        value = candles[index][field]
        prior = [candles[i][field] for i in range(index - left, index)]
        future = [candles[i][field] for i in range(index + 1, index + right + 1)]
        if kind == "high" and value > max(prior) and value >= max(future):
            pivots.append((index, value))
        if kind == "low" and value < min(prior) and value <= min(future):
            pivots.append((index, value))
    return pivots


def _rsi_at(closes, index, period=14):
    if index < period:
        return 50
    return rsi(closes[: index + 1], period)


def _divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_through):
    score = 2.0
    if rsi_delta >= 6:
        score += 0.75
    if body >= 0.35:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if price_move_pct >= 0.25:
        score += 0.5
    if follow_through:
        score += 0.5
    return min(5.0, score)


def _wick_ratio(candle, side):
    candle_range = max(candle["high"] - candle["low"], 0.00000001)
    if side == "Buy":
        wick = min(candle["open"], candle["close"]) - candle["low"]
    else:
        wick = candle["high"] - max(candle["open"], candle["close"])
    return max(wick, 0) / candle_range


def _liquidity_sweep_strength(sweep_pct, wick_ratio, volume_ratio, body, follow_through):
    score = 2.0
    if sweep_pct >= 0.08:
        score += 0.5
    if wick_ratio >= 0.45:
        score += 0.75
    if volume_ratio >= 1.25:
        score += 0.75
    if body >= 0.3:
        score += 0.5
    if follow_through:
        score += 0.5
    return min(5.0, score)


def trend_following_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 55 or len(tf15m) < 25 or len(tf5m) < 21:
        return vote("Trend Follow", "WAIT", "Not enough closed candles for trend-follow confirmation")

    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("Trend Follow", "WAIT", "1H EMA20/50 trend not clean")

    closes1h = [item["close"] for item in tf1h]
    ema20_1h = ema(closes1h, 20)
    ema50_1h = ema(closes1h, 50)
    separation_pct = abs((ema20_1h[-1] - ema50_1h[-1]) / closes1h[-1]) * 100 if closes1h[-1] else 0
    slope = _slope_pct(ema20_1h, 3)
    slope_ok = (direction == "Buy" and slope > 0.04) or (direction == "Sell" and slope < -0.04)
    if separation_pct < 0.06 or not slope_ok:
        return vote("Trend Follow", "WAIT", "1H trend lacks EMA separation or slope strength")

    closes15 = [item["close"] for item in tf15m]
    ema20_15 = ema(closes15, 20)
    if not ema20_15:
        return vote("Trend Follow", "WAIT", "15M EMA setup unavailable")
    last15 = tf15m[-1]
    structure_ok = _structure_aligned(tf15m, direction, 6)
    if not structure_ok:
        return vote("Trend Follow", "WAIT", f"1H {direction}; 15M structure is not aligned")

    ema_ref = ema20_15[-1]
    if direction == "Buy":
        pullback = last15["low"] <= ema_ref <= last15["close"] or near_value(last15["close"], ema_ref, 0.28)
        overextended = ((last15["close"] - ema_ref) / ema_ref) * 100 > 0.9 if ema_ref else True
    else:
        pullback = last15["high"] >= ema_ref >= last15["close"] or near_value(last15["close"], ema_ref, 0.28)
        overextended = ((ema_ref - last15["close"]) / ema_ref) * 100 > 0.9 if ema_ref else True
    if not pullback:
        return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 15M EMA pullback/reclaim")
    if overextended:
        return vote("Trend Follow", "WAIT", f"1H {direction}; 15M close is overextended from EMA20")

    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    entry = candle_direction(last5)
    follow_through = (
        last5["close"] > prev5["close"]
        if direction == "Buy"
        else last5["close"] < prev5["close"]
    )
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    body = _body_ratio(last5)
    if entry != direction or not follow_through or body < 0.35 or volume_ratio < 0.95:
        return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 5M body, volume, and follow-through")

    strength = _trend_follow_strength(separation_pct, slope, body, volume_ratio, pullback)
    return vote("Trend Follow", direction, f"1H {direction} trend, 15M EMA reclaim, 5M follow-through confirmed", strength)


def _breakout_strength(last5, previous5, level, side, volume_ratio, body_ratio, confirmed15, retested):
    close = last5["close"]
    distance_pct = abs((close - level) / level) * 100 if level else 0
    score = 2.0
    if distance_pct >= 0.08:
        score += 0.75
    if body_ratio >= 0.55:
        score += 0.75
    if volume_ratio >= 1.35:
        score += 0.75
    if confirmed15:
        score += 0.5
    if retested:
        score += 0.75
    if side == "Buy" and previous5 and previous5["low"] <= level <= last5["close"]:
        score += 0.25
    if side == "Sell" and previous5 and last5["close"] <= level <= previous5["high"]:
        score += 0.25
    return min(5.0, score)


def sr_breakout_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 31 or len(tf15m) < 30 or len(tf5m) < 21:
        return vote("S/R Breakout", "WAIT", "Not enough closed structure for breakout confirmation")

    support, resistance = swing_zone(tf1h[-31:-1], 30)
    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    last15 = tf15m[-1]
    range15 = max(item["high"] for item in tf15m[-8:]) - min(item["low"] for item in tf15m[-8:])
    avg_range15 = _avg_range(tf15m[-30:])
    avg_range5 = _avg_range(tf5m[-21:-1])
    consolidating = range15 <= avg_range15 * 4
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    body = _body_ratio(last5)
    oversized = avg_range5 > 0 and (last5["high"] - last5["low"]) > avg_range5 * 3.5

    if last5["high"] > resistance and last5["close"] <= resistance:
        return vote("S/R Breakout", "WAIT", "False breakout: wick pierced resistance but 5M closed back inside")
    if last5["low"] < support and last5["close"] >= support:
        return vote("S/R Breakout", "WAIT", "False breakdown: wick pierced support but 5M closed back inside")

    confirmed_buy_15 = last15["close"] > resistance
    confirmed_sell_15 = last15["close"] < support
    retested_buy = prev5["low"] <= resistance <= last5["close"]
    retested_sell = last5["close"] <= support <= prev5["high"]
    volume_ok = volume_ratio >= 1.2
    body_ok = body >= 0.45

    if last5["close"] > resistance:
        if not consolidating:
            return vote("S/R Breakout", "WAIT", "Breakout ignored: 15M structure is too expanded")
        if oversized:
            return vote("S/R Breakout", "WAIT", "Breakout ignored: 5M candle is abnormally extended")
        if not confirmed_buy_15 or not body_ok or not volume_ok:
            return vote("S/R Breakout", "WAIT", "Resistance break needs 15M close, strong body, and volume confirmation")
        strength = _breakout_strength(last5, prev5, resistance, "Buy", volume_ratio, body, confirmed_buy_15, retested_buy)
        return vote("S/R Breakout", "Buy", "Confirmed resistance breakout with 15M close, 5M body, and volume", strength)

    if last5["close"] < support:
        if not consolidating:
            return vote("S/R Breakout", "WAIT", "Breakdown ignored: 15M structure is too expanded")
        if oversized:
            return vote("S/R Breakout", "WAIT", "Breakdown ignored: 5M candle is abnormally extended")
        if not confirmed_sell_15 or not body_ok or not volume_ok:
            return vote("S/R Breakout", "WAIT", "Support break needs 15M close, strong body, and volume confirmation")
        strength = _breakout_strength(last5, prev5, support, "Sell", volume_ratio, body, confirmed_sell_15, retested_sell)
        return vote("S/R Breakout", "Sell", "Confirmed support breakdown with 15M close, 5M body, and volume", strength)

    return vote("S/R Breakout", "WAIT", "No confirmed 1H support/resistance breakout")


def rsi_divergence_engine(tf1h, tf15m, tf5m):
    if len(tf15m) < 35 or len(tf5m) < 21:
        return vote("RSI Divergence", "WAIT", "Not enough closed candles for pivot RSI divergence")

    direction = trend_direction(tf1h)
    closes15 = [item["close"] for item in tf15m]
    recent = tf15m[-34:]
    offset = len(tf15m) - len(recent)
    lows = _pivot_points(recent, "low", "low")
    highs = _pivot_points(recent, "high", "high")
    rsi_now = rsi(closes15, 14)
    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    entry = candle_direction(last5)
    body = _body_ratio(last5)
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    follow_buy = last5["close"] > prev5["close"]
    follow_sell = last5["close"] < prev5["close"]

    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        first_idx = offset + first[0]
        second_idx = offset + second[0]
        rsi_first = _rsi_at(closes15, first_idx)
        rsi_second = _rsi_at(closes15, second_idx)
        price_move_pct = abs((second[1] - first[1]) / first[1]) * 100 if first[1] else 0
        rsi_delta = rsi_second - rsi_first
        bullish = (
            second[1] < first[1]
            and rsi_delta >= 4
            and rsi_now <= 52
            and entry == "Buy"
            and follow_buy
            and body >= 0.3
            and volume_ratio >= 1.0
            and direction != "Sell"
        )
        if bullish:
            strength = _divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_buy)
            return vote("RSI Divergence", "Buy", f"15M bullish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", strength)

    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        first_idx = offset + first[0]
        second_idx = offset + second[0]
        rsi_first = _rsi_at(closes15, first_idx)
        rsi_second = _rsi_at(closes15, second_idx)
        price_move_pct = abs((second[1] - first[1]) / first[1]) * 100 if first[1] else 0
        rsi_delta = rsi_first - rsi_second
        bearish = (
            second[1] > first[1]
            and rsi_delta >= 4
            and rsi_now >= 48
            and entry == "Sell"
            and follow_sell
            and body >= 0.3
            and volume_ratio >= 1.0
            and direction != "Buy"
        )
        if bearish:
            strength = _divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_sell)
            return vote("RSI Divergence", "Sell", f"15M bearish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", strength)

    return vote("RSI Divergence", "WAIT", f"No confirmed pivot divergence, RSI {rsi_now:.1f}")


def vwap_bounce_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 55 or len(tf15m) < 40 or len(tf5m) < 21:
        return vote("VWAP Bounce", "WAIT", "Not enough closed candles for VWAP confirmation")

    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("VWAP Bounce", "WAIT", "1H trend not clean for VWAP bounce")

    vwap = _vwap(tf15m[-40:])
    if vwap is None:
        return vote("VWAP Bounce", "WAIT", "15M VWAP volume unavailable")

    last15 = tf15m[-1]
    prev15 = tf15m[-2]
    distance_pct = abs((last15["close"] - vwap) / vwap) * 100 if vwap else 999
    near_vwap = distance_pct <= 0.35
    structure_ok = _vwap_structure_aligned(tf15m, direction, 6)
    if not near_vwap:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; 15M close is too far from VWAP")
    if not structure_ok:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; 15M VWAP structure is not aligned")

    if direction == "Buy":
        reclaim = last15["low"] <= vwap <= last15["close"] or (prev15["close"] < vwap <= last15["close"])
        overextended = ((last15["close"] - vwap) / vwap) * 100 > 0.45 if vwap else True
    else:
        reclaim = last15["high"] >= vwap >= last15["close"] or (prev15["close"] > vwap >= last15["close"])
        overextended = ((vwap - last15["close"]) / vwap) * 100 > 0.45 if vwap else True
    if not reclaim:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for 15M VWAP reclaim/rejection")
    if overextended:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; VWAP bounce is already overextended")

    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    entry = candle_direction(last5)
    follow_through = (
        last5["close"] > prev5["close"]
        if direction == "Buy"
        else last5["close"] < prev5["close"]
    )
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    body = _body_ratio(last5)
    if entry != direction or not follow_through or body < 0.35 or volume_ratio < 0.95:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for 5M VWAP bounce body, volume, and follow-through")

    strength = _vwap_strength(distance_pct, body, volume_ratio, reclaim, structure_ok)
    return vote("VWAP Bounce", direction, f"1H {direction}, 15M VWAP reclaim, 5M follow-through confirmed", strength)


def liquidity_sweep_engine(tf1h, tf15m, tf5m):
    if len(tf15m) < 25 or len(tf5m) < 21:
        return vote("Liquidity Sweep", "WAIT", "Not enough closed candles for liquidity sweep confirmation")

    direction = trend_direction(tf1h)
    recent = tf15m[-21:-1]
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    prior_high = max(item["high"] for item in recent)
    prior_low = min(item["low"] for item in recent)
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    body = _body_ratio(last5)

    swept_low = last15["low"] < prior_low and last15["close"] > prior_low
    swept_high = last15["high"] > prior_high and last15["close"] < prior_high

    if swept_low:
        wick = _wick_ratio(last15, "Buy")
        sweep_pct = ((prior_low - last15["low"]) / prior_low) * 100 if prior_low else 0
        follow = last5["close"] > prev5["close"] and candle_direction(last5) == "Buy"
        if direction == "Sell":
            return vote("Liquidity Sweep", "WAIT", "Bullish liquidity sweep blocked by strong 1H downtrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bullish sweep needs wick rejection, volume, and 5M follow-through")
        strength = _liquidity_sweep_strength(sweep_pct, wick, volume_ratio, body, follow)
        return vote("Liquidity Sweep", "Buy", "Bullish stop-hunt sweep below 15M range with reclaim confirmed", strength)

    if swept_high:
        wick = _wick_ratio(last15, "Sell")
        sweep_pct = ((last15["high"] - prior_high) / prior_high) * 100 if prior_high else 0
        follow = last5["close"] < prev5["close"] and candle_direction(last5) == "Sell"
        if direction == "Buy":
            return vote("Liquidity Sweep", "WAIT", "Bearish liquidity sweep blocked by strong 1H uptrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bearish sweep needs wick rejection, volume, and 5M follow-through")
        strength = _liquidity_sweep_strength(sweep_pct, wick, volume_ratio, body, follow)
        return vote("Liquidity Sweep", "Sell", "Bearish stop-hunt sweep above 15M range with reclaim confirmed", strength)

    return vote("Liquidity Sweep", "WAIT", "No confirmed stop-hunt liquidity sweep")


from datetime import datetime, timezone

def orb_engine(tf1h, tf15m, tf5m):
    now = datetime.now(timezone.utc)
    today_utc_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_utc_midnight_ms = int(today_utc_midnight.timestamp() * 1000)

    opening = None
    for candle in tf1h:
        if candle["time"] >= today_utc_midnight_ms:
            opening = candle
            break

    if not opening:
        return vote("ORB", "WAIT", "No 1H candle found for current UTC day")

    high = opening["high"]
    low = opening["low"]
    last15 = tf15m[-1]
    last5 = tf5m[-1]
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.1
    if last15["close"] > high and last5["close"] > high and volume_ok:
        return vote("ORB", "Buy", "1H opening range high broken, 15M/5M confirmed", last5["close"] - high)
    if last15["close"] < low and last5["close"] < low and volume_ok:
        return vote("ORB", "Sell", "1H opening range low broken, 15M/5M confirmed", low - last5["close"])
    return vote("ORB", "WAIT", "Opening range not confirmed on 15M and 5M")
