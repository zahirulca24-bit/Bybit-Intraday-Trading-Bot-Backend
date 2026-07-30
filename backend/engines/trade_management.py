import secrets
import time
from decimal import Decimal, ROUND_HALF_UP

from .entry_protection import place_mandatory_protected_order


class TradeManagementEngine:
    def __init__(self, bybit_request_fn, market_data):
        self.bybit_request = bybit_request_fn
        self.market_data = market_data

    def tick_size(self, symbol):
        payload = self.market_data.public_get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
        )
        row = ((payload.get("result") or {}).get("list") or [{}])[0]
        tick = ((row.get("priceFilter") or {}).get("tickSize")) or "0.01"
        try:
            return Decimal(str(tick))
        except Exception:
            return Decimal("0.01")

    def mark_price(self, symbol):
        payload = self.market_data.public_get(
            "/v5/market/tickers",
            {"category": "linear", "symbol": symbol},
        )
        row = ((payload.get("result") or {}).get("list") or [{}])[0]
        value = row.get("markPrice") or row.get("lastPrice")
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def instrument_rules(self, symbol):
        payload = self.market_data.public_get(
            "/v5/market/instruments-info",
            {"category": "linear", "symbol": symbol},
        )
        if payload.get("retCode") != 0:
            return {
                "ok": False,
                "reason": payload.get("retMsg", "Instrument rules unavailable"),
            }
        row = ((payload.get("result") or {}).get("list") or [{}])[0]
        if not row:
            return {"ok": False, "reason": "Instrument not found"}
        lot = row.get("lotSizeFilter") or {}
        price_filter = row.get("priceFilter") or {}
        try:
            return {
                "ok": True,
                "reason": "OK",
                "qtyStep": Decimal(str(lot.get("qtyStep") or "0.001")),
                "minOrderQty": Decimal(str(lot.get("minOrderQty") or "0.001")),
                "maxOrderQty": Decimal(
                    str(lot.get("maxOrderQty") or lot.get("maxMktOrderQty") or "0")
                ),
                "minNotionalValue": Decimal(
                    str(lot.get("minNotionalValue") or "5")
                ),
                "tickSize": Decimal(str(price_filter.get("tickSize") or "0.01")),
            }
        except Exception:
            return {"ok": False, "reason": "Instrument filters are invalid"}

    def format_price(self, symbol, value):
        tick = self.tick_size(symbol)
        price = Decimal(str(value))
        rounded = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
        return format(rounded.normalize(), "f")

    def tpsl_prices(self, symbol, side, stop_loss_pct, take_profit_pct):
        mark = self.mark_price(symbol)
        if not mark:
            return None, None
        if side == "Buy":
            stop_loss = mark * (1 - (float(stop_loss_pct) / 100))
            take_profit = mark * (1 + (float(take_profit_pct) / 100))
        else:
            stop_loss = mark * (1 + (float(stop_loss_pct) / 100))
            take_profit = mark * (1 - (float(take_profit_pct) / 100))
        return self.format_price(symbol, stop_loss), self.format_price(symbol, take_profit)

    def order_link_id(self, source):
        prefix = "".join(
            ch.lower() for ch in str(source or "auto") if ch.isalnum()
        )[:8] or "auto"
        nonce = secrets.token_hex(3)
        return f"cdx-{prefix}-{int(time.time() * 1000)}-{nonce}"[:36]

    def place_order(
        self,
        symbol,
        side,
        qty,
        source,
        stop_loss_pct=None,
        take_profit_pct=None,
    ):
        return place_mandatory_protected_order(
            symbol,
            side,
            qty,
            source,
            stop_loss_pct,
            take_profit_pct,
            get_mark_price=self.mark_price,
            get_instrument_rules=self.instrument_rules,
            generate_order_link_id=self.order_link_id,
            submit_order=self.bybit_request,
        )

    def close_positions(self, symbol):
        payload = self.bybit_request(
            "GET",
            "/v5/position/list",
            {"category": "linear", "symbol": symbol},
        )
        if payload.get("retCode") != 0:
            return {
                "ok": False,
                "error": payload.get("retMsg", "Position check failed"),
                "orders": [],
            }
        orders = []
        for position in (payload.get("result") or {}).get("list") or []:
            size = abs(float(position.get("size") or 0))
            if size <= 0:
                continue
            side = "Sell" if position.get("side") == "Buy" else "Buy"
            close_order = {
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Market",
                "qty": str(position.get("size")),
                "reduceOnly": True,
                "timeInForce": "IOC",
                "orderLinkId": self.order_link_id("close"),
            }
            if position.get("positionIdx") is not None:
                close_order["positionIdx"] = int(position.get("positionIdx") or 0)
            orders.append(
                self.bybit_request("POST", "/v5/order/create", close_order)
            )
        return {"ok": True, "orders": orders}
