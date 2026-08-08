from backend import hourly_watchlist


class FakeCore:
    def __init__(self, price_24h_pcnt: str):
        self.price_24h_pcnt = price_24h_pcnt

    def public_bybit_get(self, path, params):
        assert path == "/v5/market/tickers"
        assert params == {"category": "linear"}
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "lastPrice": "100.0",
                        "turnover24h": "25000000",
                        "bid1Price": "99.99",
                        "ask1Price": "100.01",
                        "price24hPcnt": self.price_24h_pcnt,
                    }
                ]
            },
        }


def test_eligible_market_converts_bybit_price24h_fraction_to_percent():
    symbols, market, rejected = hourly_watchlist._eligible_market(FakeCore("0.0125"))

    assert symbols == ["BTCUSDT"]
    assert market["BTCUSDT"]["change24hPct"] == 1.25
    assert rejected == {"invalidSymbol": 0, "invalidPrice": 0, "turnover": 0, "spread": 0}


def test_eligible_market_preserves_negative_24h_change():
    _, market, _ = hourly_watchlist._eligible_market(FakeCore("-0.032"))

    assert market["BTCUSDT"]["change24hPct"] == -3.2
