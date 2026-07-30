def ema(values, period):
    if len(values) < period:
        return []
    alpha = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return 50
    gains = []
    losses = []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def avg_volume(candles, period=20):
    window = candles[-period:]
    if not window:
        return 0
    return sum(item["volume"] for item in window) / len(window)


def trend_direction(candles):
    closes = [item["close"] for item in candles]
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    if len(fast) < 2 or len(slow) < 2:
        return "WAIT"
    if fast[-1] > slow[-1] and closes[-1] > fast[-1]:
        return "Buy"
    if fast[-1] < slow[-1] and closes[-1] < fast[-1]:
        return "Sell"
    return "WAIT"


def candle_direction(candle):
    if candle["close"] > candle["open"]:
        return "Buy"
    if candle["close"] < candle["open"]:
        return "Sell"
    return "WAIT"


def near_value(price, target, pct):
    if not target:
        return False
    return abs((price - target) / target) * 100 <= pct


def swing_zone(candles, period=20):
    window = candles[-period:]
    return min(item["low"] for item in window), max(item["high"] for item in window)
