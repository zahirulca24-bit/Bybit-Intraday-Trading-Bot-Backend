from backend import fifteen_minute_strategy_classifier as classifier


class Core:
    @staticmethod
    def simple_atr(highs, lows, closes, period):
        assert period == 14
        return 2.0


def test_closed_15m_market_metrics_publish_real_values():
    history = []
    for index in range(21):
        history.append({
            "time": 1_700_000_000_000 + index * 900_000,
            "open": 99.0,
            "high": 102.0,
            "low": 98.0,
            "close": 100.0,
            "volume": 100.0 if index < 20 else 150.0,
        })

    result = classifier._closed_15m_market_metrics(Core(), history)

    assert result["atr15mPct"] == 2.0
    assert result["volumeRatio"] == 1.5
    assert result["marketMetricsCandleTime"] == history[-1]["time"]


def test_closed_15m_market_metrics_never_invent_zero_when_unavailable():
    result = classifier._closed_15m_market_metrics(Core(), [])
    assert result == {
        "atr15mPct": None,
        "volumeRatio": None,
        "marketMetricsCandleTime": None,
    }
