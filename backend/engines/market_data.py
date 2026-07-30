import json
import urllib.parse
import urllib.request


class MarketDataEngine:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def public_get(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        url = self.base_url + path + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            return {"retCode": -2, "retMsg": str(exc), "result": {}}

    def candles(self, symbol, interval, limit=120):
        payload = self.public_get("/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        })
        if payload.get("retCode") != 0:
            return None, payload.get("retMsg", "Kline fetch failed")
        raw = (payload.get("result") or {}).get("list") or []
        raw = sorted(raw, key=lambda item: int(item[0]))
        candles = []
        for item in raw:
            if len(item) < 6:
                continue
            candles.append({
                "time": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
            })
        if len(candles) < 60:
            return None, "Not enough candles"
        return candles, "OK"

    def snapshot(self, symbol):
        tf1h, m1 = self.candles(symbol, "60")
        tf15m, m15 = self.candles(symbol, "15")
        tf5m, m5 = self.candles(symbol, "5")
        return {
            "ok": bool(tf1h and tf15m and tf5m),
            "timeframes": {"1H": tf1h, "15M": tf15m, "5M": tf5m},
            "message": "; ".join(x for x in [m1, m15, m5] if x),
        }
