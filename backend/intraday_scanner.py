"""Liquid intraday universe, bounded scanning, and cost-aware sizing.

The defaults in this module are the approved Serial 6 production baseline.
They remain environment configurable only within explicit safety bounds.
"""

from __future__ import annotations

import math
import os
import threading
import time
import urllib.parse
from decimal import Decimal
from typing import Any, Iterable

try:
    from .scanner_safety import bounded_symbols, normalize_interval
except ImportError:
    from scanner_safety import bounded_symbols, normalize_interval

ALLOWED_ENTRY_INTERVALS = frozenset({"5", "15", "30", "60"})
_UNIVERSE_LOCK = threading.Lock()
_SCAN_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {
    "symbols": [],
    "rows": [],
    "updatedAt": 0,
    "nextRefreshAt": 0,
    "source": "liquid_intraday_top_movers",
    "metrics": {},
}


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def settings() -> dict[str, float | int]:
    return {
        "shortlistSize": _integer("UNIVERSE_SHORTLIST_SIZE", 20, 10, 40),
        "deepScanSize": _integer("MAX_SCAN_SYMBOLS", 10, 1, 10),
        "normalSpreadPct": _number("NORMAL_SPREAD_PCT", 0.08, 0.01, 0.20),
        "reducedSpreadPct": _number("REDUCED_SIZE_SPREAD_PCT", 0.15, 0.02, 0.20),
        "maxSpreadPct": _number("MAX_SPREAD_PCT", 0.20, 0.03, 0.30),
        "minimumTurnover": _number("MIN_TURNOVER_24H", 10_000_000, 1_000_000, 1_000_000_000),
        "minimumPrice": _number("MIN_LAST_PRICE", 0.01, 0.000001, 100),
        "maximumAbsoluteChange": _number("MAX_ABS_CHANGE_PCT", 15.0, 5.0, 50.0),
        "minimumAtrPct": _number("MIN_ATR_15M_PCT", 0.25, 0.01, 5.0),
        "maximumAtrPct": _number("MAX_ATR_15M_PCT", 3.0, 0.10, 10.0),
        "minimumVolumeRatio": _number("MIN_RECENT_VOLUME_RATIO", 1.20, 0.50, 5.0),
        "minimumGrossRr": _number("MIN_GROSS_RR", 2.0, 1.0, 5.0),
        "minimumNetRr": _number("MIN_NET_RR", 1.70, 1.0, 5.0),
        "preferredNetRr": _number("PREFERRED_NET_RR", 2.0, 1.0, 6.0),
        "normalCostRiskPct": _number("NORMAL_ENTRY_COST_RISK_PCT", 15.0, 1.0, 50.0),
        "maximumCostRiskPct": _number("MAX_ENTRY_COST_RISK_PCT", 35.0, 5.0, 75.0),
        "refreshSeconds": _integer("UNIVERSE_REFRESH_SECONDS", 600, 60, 3600),
        "deadlineSeconds": _number("SCAN_DEADLINE_SECONDS", 20.0, 2.0, 60.0),
        "takerFeePct": _number("ESTIMATED_TAKER_FEE_PCT", 0.055, 0.0, 0.20),
        "slippageMultiplier": _number("SLIPPAGE_SPREAD_MULTIPLIER", 0.50, 0.0, 2.0),
    }


def normalize_scanner_interval(value: object) -> str:
    interval = normalize_interval(value, "5")
    if interval not in ALLOWED_ENTRY_INTERVALS:
        raise ValueError("Scanner interval must be one of 5, 15, 30, or 60")
    return interval


def normalize_symbols(values: Iterable[object], maximum: int) -> tuple[list[str], list[str]]:
    requested = [str(value or "").strip().upper() for value in values or []]
    valid: list[str] = []
    rejected: list[str] = []
    for symbol in requested:
        if not symbol or len(symbol) > 24 or not symbol.endswith("USDT") or not symbol.isalnum():
            if symbol:
                rejected.append(symbol)
            continue
        valid.append(symbol)
    bounded = bounded_symbols(valid, maximum)
    rejected.extend(symbol for symbol in valid if symbol not in bounded)
    return bounded, list(dict.fromkeys(rejected))


def _spread_pct(item: dict) -> float | None:
    try:
        bid = float(item.get("bid1Price") or 0)
        ask = float(item.get("ask1Price") or 0)
        last = float(item.get("lastPrice") or 0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0 or last <= 0 or ask < bid:
        return None
    return ((ask - bid) / last) * 100


def _atr_volume(core: Any, symbol: str) -> tuple[float | None, float | None]:
    candles, _ = core.fetch_candles(symbol, "15", limit=40)
    if not candles or len(candles) < 22:
        return None, None
    closes = [float(row["close"]) for row in candles]
    highs = [float(row["high"]) for row in candles]
    lows = [float(row["low"]) for row in candles]
    volumes = [float(row.get("volume") or 0) for row in candles]
    atr = core.simple_atr(highs, lows, closes, 14)
    atr_pct = (atr / closes[-1]) * 100 if closes[-1] > 0 and atr > 0 else None
    baseline = sum(volumes[-21:-1]) / 20 if sum(volumes[-21:-1]) > 0 else 0
    volume_ratio = volumes[-1] / baseline if baseline > 0 else None
    return atr_pct, volume_ratio


def _score(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows
    max_turnover = max(float(row["turnover24h"]) for row in rows) or 1
    max_movement = max(abs(float(row["changePct"])) for row in rows) or 1
    max_volume_ratio = max(float(row.get("volumeRatio") or 0) for row in rows) or 1
    for row in rows:
        liquidity = math.log1p(float(row["turnover24h"])) / math.log1p(max_turnover)
        volume = min(1.0, float(row.get("volumeRatio") or 0) / max_volume_ratio)
        trend = min(1.0, abs(float(row["changePct"])) / max_movement)
        atr = float(row.get("atr15mPct") or 0)
        volatility = 1.0 - min(1.0, abs(atr - 1.25) / 1.25)
        spread_quality = 1.0 - min(1.0, float(row["spreadPct"]) / 0.20)
        row["rankScore"] = round(
            (liquidity * 25) + (volume * 25) + (trend * 20) + (volatility * 20) + (spread_quality * 10),
            4,
        )
    return sorted(rows, key=lambda row: row["rankScore"], reverse=True)


def build_universe(core: Any, force: bool = False, limit: int | None = None) -> dict:
    cfg = settings()
    now = int(time.time())
    if not force and _CACHE["symbols"] and int(_CACHE["nextRefreshAt"]) > now:
        return dict(_CACHE)
    if not _UNIVERSE_LOCK.acquire(blocking=False):
        return dict(_CACHE)
    started = time.monotonic()
    try:
        payload = core.public_bybit_get("/v5/market/tickers", {"category": "linear"})
        ticker_rows = (payload.get("result") or {}).get("list") or [] if payload.get("retCode") == 0 else []
        total = len(ticker_rows)
        liquid: list[dict] = []
        counts = {
            "totalContracts": total,
            "validUsdt": 0,
            "spreadPassed": 0,
            "liquidityPassed": 0,
            "enriched": 0,
            "enrichmentRejected": 0,
            "deadlineExceeded": False,
        }
        for item in ticker_rows:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol.endswith("USDT") or not symbol.isalnum():
                continue
            counts["validUsdt"] += 1
            spread = _spread_pct(item)
            try:
                last = float(item.get("lastPrice") or 0)
                turnover = float(item.get("turnover24h") or 0)
                change_pct = float(item.get("price24hPcnt") or 0) * 100
            except (TypeError, ValueError):
                continue
            if spread is None or spread > float(cfg["maxSpreadPct"]):
                continue
            counts["spreadPassed"] += 1
            if last < float(cfg["minimumPrice"]) or turnover < float(cfg["minimumTurnover"]):
                continue
            if abs(change_pct) > float(cfg["maximumAbsoluteChange"]):
                continue
            counts["liquidityPassed"] += 1
            liquid.append({"symbol": symbol, "lastPrice": last, "turnover24h": turnover, "changePct": change_pct, "spreadPct": round(spread, 5)})

        preliminary = sorted(liquid, key=lambda row: (row["turnover24h"], abs(row["changePct"])), reverse=True)[: int(cfg["shortlistSize"])]
        enriched: list[dict] = []
        rejected: list[dict] = []
        for row in preliminary:
            if time.monotonic() - started >= float(cfg["deadlineSeconds"]):
                counts["deadlineExceeded"] = True
                rejected.extend(
                    {"symbol": pending["symbol"], "reason": "scan_deadline_exceeded"}
                    for pending in preliminary[len(enriched) + len(rejected):]
                )
                break
            try:
                atr_pct, volume_ratio = _atr_volume(core, row["symbol"])
            except Exception:
                rejected.append({"symbol": row["symbol"], "reason": "market_history_error"})
                continue
            row["atr15mPct"] = round(atr_pct, 5) if atr_pct is not None else None
            row["volumeRatio"] = round(volume_ratio, 4) if volume_ratio is not None else None
            if atr_pct is None:
                rejected.append({"symbol": row["symbol"], "reason": "insufficient_closed_history"})
                continue
            if atr_pct < float(cfg["minimumAtrPct"]):
                rejected.append({"symbol": row["symbol"], "reason": "atr_below_minimum"})
                continue
            if atr_pct > float(cfg["maximumAtrPct"]):
                rejected.append({"symbol": row["symbol"], "reason": "atr_above_maximum"})
                continue
            row["volumeConfirmed"] = bool(
                volume_ratio is not None and volume_ratio >= float(cfg["minimumVolumeRatio"])
            )
            row["costTier"] = spread_tier(row["spreadPct"], cfg)
            enriched.append(row)

        counts["enriched"] = len(enriched)
        counts["enrichmentRejected"] = len(rejected)
        ranked = _score(enriched)
        selected_limit = min(int(limit or cfg["deepScanSize"]), int(cfg["deepScanSize"]))
        selected = ranked[:selected_limit]
        metrics = {
            **counts,
            "shortlisted": len(preliminary),
            "deepScan": len(selected),
            "rejected": len(rejected),
        }
        if selected:
            _CACHE.update({
                "symbols": [row["symbol"] for row in selected],
                "rows": selected,
                "shortlist": ranked[: int(cfg["shortlistSize"])],
                "rejections": rejected,
                "updatedAt": now,
                "nextRefreshAt": now + int(cfg["refreshSeconds"]),
                "source": "liquid_intraday_top_movers",
                "metrics": metrics,
                "policy": cfg,
            })
        else:
            _CACHE.update({
                "symbols": [],
                "rows": [],
                "shortlist": [],
                "rejections": rejected,
                "updatedAt": now,
                "nextRefreshAt": now + 60,
                "source": "liquid_intraday_top_movers_empty",
                "metrics": metrics,
                "policy": cfg,
            })
        return dict(_CACHE)
    finally:
        _UNIVERSE_LOCK.release()


def spread_tier(spread_pct: float, cfg: dict | None = None) -> str:
    cfg = cfg or settings()
    if spread_pct <= float(cfg["normalSpreadPct"]):
        return "normal"
    if spread_pct <= float(cfg["reducedSpreadPct"]):
        return "reduced"
    if spread_pct <= float(cfg["maxSpreadPct"]):
        return "strong_only"
    return "blocked"


def estimate_trade_cost(core: Any, symbol: str, notional: float, risk_amount: float, stop_pct: float, take_pct: float) -> dict:
    cfg = settings()
    payload = core.public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    item = ((payload.get("result") or {}).get("list") or [{}])[0] if payload.get("retCode") == 0 else {}
    spread = _spread_pct(item)
    if spread is None:
        return {"ok": False, "reason": "Spread unavailable; cost gate failed closed"}
    tier = spread_tier(spread, cfg)
    slippage_pct = spread * float(cfg["slippageMultiplier"])
    round_trip_fee_pct = 2 * float(cfg["takerFeePct"])
    total_cost_pct = spread + slippage_pct + round_trip_fee_pct
    estimated_cost = notional * (total_cost_pct / 100)
    cost_risk_pct = (estimated_cost / risk_amount) * 100 if risk_amount > 0 else math.inf
    gross_rr = take_pct / stop_pct if stop_pct > 0 else 0
    net_reward_pct = take_pct - total_cost_pct
    net_risk_pct = stop_pct + total_cost_pct
    net_rr = net_reward_pct / net_risk_pct if net_risk_pct > 0 else 0
    ok = tier != "blocked" and gross_rr >= float(cfg["minimumGrossRr"]) and net_rr >= float(cfg["minimumNetRr"]) and cost_risk_pct <= float(cfg["maximumCostRiskPct"])
    size_factor = 1.0
    if tier in {"reduced", "strong_only"} or cost_risk_pct > float(cfg["normalCostRiskPct"]):
        size_factor = 0.5
    return {
        "ok": ok,
        "reason": "Cost and net RR approved" if ok else "Trade blocked by spread, cost-to-risk, or net RR policy",
        "spreadPct": round(spread, 5),
        "spreadTier": tier,
        "slippagePct": round(slippage_pct, 5),
        "estimatedRoundTripFeePct": round(round_trip_fee_pct, 5),
        "estimatedTotalCostPct": round(total_cost_pct, 5),
        "estimatedCostUsdt": round(estimated_cost, 4),
        "costRiskPct": round(cost_risk_pct, 4),
        "grossRr": round(gross_rr, 4),
        "netRr": round(net_rr, 4),
        "preferredNetRrMet": net_rr >= float(cfg["preferredNetRr"]),
        "sizeFactor": size_factor,
    }


def install(core: Any) -> None:
    if getattr(core, "_intraday_scanner_installed", False):
        return
    original_sizing = core.calculate_position_sizing

    def liquid_universe(force=False, limit=10):
        return build_universe(core, force=force, limit=limit)

    def cost_aware_sizing(symbol, state):
        sizing = original_sizing(symbol, state)
        if not sizing.get("ok"):
            return sizing
        notional = float(sizing.get("notional") or sizing.get("estimatedNotional") or 0)
        risk_amount = float(sizing.get("riskAmount") or 0)
        stop_pct = float(state.get("stopLossPct") or 0)
        take_pct = float(state.get("takeProfitPct") or 0)
        cost = estimate_trade_cost(core, symbol, notional, risk_amount, stop_pct, take_pct)
        sizing["costGate"] = cost
        if not cost.get("ok"):
            sizing.update({"ok": False, "reason": cost.get("reason"), "qty": "0", "roundedQty": "0", "rounded_qty": "0"})
            return sizing
        factor = Decimal(str(cost.get("sizeFactor") or 1))
        if factor < 1:
            rules = core.get_instrument_rules(symbol)
            reduced = core.floor_to_step(Decimal(str(sizing["qty"])) * factor, rules["qtyStep"])
            mark = Decimal(str(sizing["markPrice"]))
            if reduced < rules["minOrderQty"] or reduced * mark < rules["minNotionalValue"]:
                sizing.update({"ok": False, "reason": "Reduced cost-aware size is below Bybit minimums", "qty": "0"})
                return sizing
            sizing["qty"] = core.format_qty(reduced)
            sizing["roundedQty"] = sizing["qty"]
            sizing["rounded_qty"] = sizing["qty"]
            sizing["estimatedNotional"] = core.format_qty(reduced * mark)
            sizing["estimated_notional"] = sizing["estimatedNotional"]
            sizing["notional"] = sizing["estimatedNotional"]
            sizing["reason"] = "Position size reduced by spread/cost policy"
        return sizing

    core.top_gainer_universe = liquid_universe
    core.calculate_position_sizing = cost_aware_sizing
    core._intraday_scanner_installed = True


def scan_lock() -> threading.Lock:
    return _SCAN_LOCK