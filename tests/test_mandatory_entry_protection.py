from decimal import Decimal

import pytest

from backend.engines.entry_protection import place_mandatory_protected_order
from backend.engines.trade_management import TradeManagementEngine
from backend import position_synced_server as runtime


RULES = {
    "ok": True,
    "reason": "OK",
    "qtyStep": Decimal("0.001"),
    "minOrderQty": Decimal("0.001"),
    "maxOrderQty": Decimal("100"),
    "minNotionalValue": Decimal("5"),
    "tickSize": Decimal("0.1"),
}


def build_order(stop_loss_pct=1, take_profit_pct=2, *, side="Buy", mark=100, rules=None):
    submitted = []

    def submit(method, path, payload):
        submitted.append((method, path, payload))
        return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-1"}}

    result = place_mandatory_protected_order(
        "BTCUSDT",
        side,
        "0.1",
        "test",
        stop_loss_pct,
        take_profit_pct,
        get_mark_price=lambda symbol: mark,
        get_instrument_rules=lambda symbol: dict(rules or RULES),
        generate_order_link_id=lambda source: "protected-entry-1",
        submit_order=submit,
    )
    return result, submitted


@pytest.mark.parametrize(
    ("stop_loss_pct", "take_profit_pct"),
    [
        (None, 2),
        (1, None),
        (0, 2),
        (1, 0),
        (-1, 2),
        (1, -2),
        ("nan", 2),
        (1, "Infinity"),
    ],
)
def test_missing_or_invalid_protection_blocks_without_submission(stop_loss_pct, take_profit_pct):
    result, submitted = build_order(stop_loss_pct, take_profit_pct)

    assert result["retCode"] == -1006
    assert result["protectionRequired"] is True
    assert "blocked locally" in result["retMsg"]
    assert submitted == []


def test_tick_rounding_cannot_collapse_protection_to_entry_price():
    rules = {**RULES, "tickSize": Decimal("1")}
    result, submitted = build_order(0.1, 0.1, mark=100, rules=rules)

    assert result["retCode"] == -1006
    assert "opposite sides" in result["retMsg"]
    assert submitted == []


def test_valid_buy_entry_always_contains_full_market_sl_and_tp():
    result, submitted = build_order(1, 2, side="Buy")

    assert result["retCode"] == 0
    assert len(submitted) == 1
    method, path, order = submitted[0]
    assert method == "POST"
    assert path == "/v5/order/create"
    assert order["orderType"] == "Market"
    assert order["stopLoss"] == "99"
    assert order["takeProfit"] == "102"
    assert order["tpslMode"] == "Full"
    assert order["tpOrderType"] == "Market"
    assert order["slOrderType"] == "Market"
    assert Decimal(order["stopLoss"]) < Decimal("100") < Decimal(order["takeProfit"])


def test_valid_sell_entry_keeps_directional_protection():
    result, submitted = build_order(1, 2, side="Sell")

    assert result["retCode"] == 0
    order = submitted[0][2]
    assert order["stopLoss"] == "101"
    assert order["takeProfit"] == "98"
    assert Decimal(order["takeProfit"]) < Decimal("100") < Decimal(order["stopLoss"])


def test_quantity_failure_still_never_submits_an_order():
    rules = {**RULES, "minOrderQty": Decimal("1")}
    result, submitted = build_order(1, 2, rules=rules)

    assert result["retCode"] == -1001
    assert submitted == []


class FakeMarketData:
    def public_get(self, path, params):
        if path == "/v5/market/tickers":
            return {
                "retCode": 0,
                "result": {"list": [{"markPrice": "100"}]},
            }
        return {
            "retCode": 0,
            "result": {
                "list": [
                    {
                        "lotSizeFilter": {
                            "qtyStep": "0.001",
                            "minOrderQty": "0.001",
                            "maxOrderQty": "100",
                            "minNotionalValue": "5",
                        },
                        "priceFilter": {"tickSize": "0.1"},
                    }
                ]
            },
        }


def test_modular_auto_engine_uses_the_same_mandatory_gate():
    submitted = []

    def request(method, path, payload):
        submitted.append(payload)
        return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "auto-1"}}

    engine = TradeManagementEngine(request, FakeMarketData())

    blocked = engine.place_order("BTCUSDT", "Buy", "0.1", "auto", None, 2)
    assert blocked["retCode"] == -1006
    assert submitted == []

    accepted = engine.place_order("BTCUSDT", "Buy", "0.1", "auto", 1, 2)
    assert accepted["retCode"] == 0
    assert submitted[0]["stopLoss"] == "99"
    assert submitted[0]["takeProfit"] == "102"


def test_canonical_manual_entry_function_uses_mandatory_gate(monkeypatch):
    submitted = []

    monkeypatch.setattr(runtime.guarded.core, "get_mark_price", lambda symbol: 100)
    monkeypatch.setattr(runtime.guarded.core, "get_instrument_rules", lambda symbol: dict(RULES))
    monkeypatch.setattr(runtime.guarded.core, "generate_order_link_id", lambda source: "manual-1")

    def request(method, path, payload):
        if method == "POST":
            submitted.append(payload)
            return {"retCode": 0, "retMsg": "OK", "result": {"orderId": "manual-1"}}
        return {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "list": [
                    {
                        "orderId": "manual-1",
                        "orderStatus": "Filled",
                        "cumExecQty": "0.1",
                        "avgPrice": "100",
                    }
                ]
            },
        }

    monkeypatch.setattr(runtime.guarded.core, "bybit_request", request)

    blocked = runtime._mandatory_place_demo_order("BTCUSDT", "Buy", "0.1", "manual", 0, 2)
    assert blocked["retCode"] == -1006
    assert submitted == []

    accepted = runtime._mandatory_place_demo_order("BTCUSDT", "Buy", "0.1", "manual", 1, 2)
    assert accepted["retCode"] == 0
    assert accepted["finalFilled"] is True
    assert submitted[0]["stopLoss"] == "99"
    assert submitted[0]["takeProfit"] == "102"
