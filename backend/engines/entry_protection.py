"""Create entry orders only when valid exchange-side stop loss and take profit exist."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable


PROTECTION_BLOCK_CODE = -1006
QUANTITY_BLOCK_CODE = -1001


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _format_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _round_to_tick(value: Decimal, tick: Decimal) -> Decimal:
    return (value / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick


def _blocked(reason: str, *, protection: bool) -> dict[str, Any]:
    prefix = "mandatory stop loss/take profit unavailable" if protection else "quantity/notional does not meet Bybit instrument limits"
    return {
        "retCode": PROTECTION_BLOCK_CODE if protection else QUANTITY_BLOCK_CODE,
        "retMsg": f"Order blocked locally: {prefix}. {reason}",
        "result": {},
        "protectionRequired": protection,
    }


def place_mandatory_protected_order(
    symbol: str,
    side: str,
    qty: Any,
    source: str,
    stop_loss_pct: Any,
    take_profit_pct: Any,
    *,
    get_mark_price: Callable[[str], Any],
    get_instrument_rules: Callable[[str], dict[str, Any]],
    generate_order_link_id: Callable[[str], str],
    submit_order: Callable[[str, str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Submit a non-reduce-only market entry only with valid directional SL and TP.

    This is the final local gate immediately before ``/v5/order/create``. Missing,
    non-finite, non-positive, rounded-to-entry, or directionally invalid protection
    blocks the order without calling ``submit_order``.
    """

    if side not in {"Buy", "Sell"}:
        return _blocked("Entry side must be Buy or Sell.", protection=True)

    stop_pct = _positive_decimal(stop_loss_pct)
    take_pct = _positive_decimal(take_profit_pct)
    if stop_pct is None:
        return _blocked("Stop loss percentage must be finite and greater than zero.", protection=True)
    if take_pct is None:
        return _blocked("Take profit percentage must be finite and greater than zero.", protection=True)

    try:
        mark = _positive_decimal(get_mark_price(symbol))
    except Exception as exc:
        return _blocked(f"Mark price lookup failed: {type(exc).__name__}.", protection=True)
    if mark is None:
        return _blocked("A valid positive mark price is required to calculate protection.", protection=True)

    try:
        rules = get_instrument_rules(symbol)
    except Exception as exc:
        return _blocked(f"Instrument rule lookup failed: {type(exc).__name__}.", protection=True)
    if not isinstance(rules, dict) or not rules.get("ok"):
        reason = rules.get("reason") if isinstance(rules, dict) else "Invalid instrument rules"
        return _blocked(str(reason or "Instrument rules unavailable."), protection=True)

    qty_step = _positive_decimal(rules.get("qtyStep"))
    min_order_qty = _positive_decimal(rules.get("minOrderQty"))
    max_order_qty = _non_negative_decimal(rules.get("maxOrderQty"))
    min_notional = _non_negative_decimal(rules.get("minNotionalValue"))
    tick_size = _positive_decimal(rules.get("tickSize"))
    if None in {qty_step, min_order_qty, max_order_qty, min_notional, tick_size}:
        return _blocked("Instrument quantity or price filters are invalid.", protection=True)

    requested_qty = _positive_decimal(qty)
    if requested_qty is None:
        return _blocked("Quantity must be finite and greater than zero.", protection=False)

    rounded_qty = _floor_to_step(requested_qty, qty_step)
    notional = rounded_qty * mark
    if rounded_qty < min_order_qty:
        return _blocked("Rounded quantity is below minOrderQty.", protection=False)
    if max_order_qty > 0 and rounded_qty > max_order_qty:
        return _blocked("Rounded quantity exceeds maxOrderQty.", protection=False)
    if min_notional > 0 and notional < min_notional:
        return _blocked("Estimated notional is below minNotionalValue.", protection=False)

    hundred = Decimal("100")
    if side == "Buy":
        raw_stop = mark * (Decimal("1") - (stop_pct / hundred))
        raw_take = mark * (Decimal("1") + (take_pct / hundred))
    else:
        raw_stop = mark * (Decimal("1") + (stop_pct / hundred))
        raw_take = mark * (Decimal("1") - (take_pct / hundred))

    if raw_stop <= 0 or raw_take <= 0:
        return _blocked("Calculated protection price is not positive.", protection=True)

    stop_loss = _round_to_tick(raw_stop, tick_size)
    take_profit = _round_to_tick(raw_take, tick_size)
    if stop_loss <= 0 or take_profit <= 0:
        return _blocked("Rounded protection price is not positive.", protection=True)

    directional = (
        stop_loss < mark < take_profit
        if side == "Buy"
        else take_profit < mark < stop_loss
    )
    if not directional:
        return _blocked(
            "Rounded prices must remain on opposite sides of the current mark price.",
            protection=True,
        )

    order = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": _format_decimal(rounded_qty),
        "timeInForce": "IOC",
        "orderLinkId": generate_order_link_id(source),
        "stopLoss": _format_decimal(stop_loss),
        "takeProfit": _format_decimal(take_profit),
        "tpslMode": "Full",
        "tpOrderType": "Market",
        "slOrderType": "Market",
    }
    return submit_order("POST", "/v5/order/create", order)
