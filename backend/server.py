import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from engines.bot_engine import BotEngineV2 as ModularBotEngineV2
from engines.risk import signal_risk_policy


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
FRONTEND_INDEX = PROJECT_ROOT / "frontend" / "index.html"
ENV_PATH = ROOT / ".env"
RECV_WINDOW = "20000"
TOP_GAINER_REFRESH_SECONDS = 600
MIN_TURNOVER_24H = 1500000
MAX_SPREAD_PCT = 0.14
MAX_TOP_GAINER_CHANGE_PCT = 30
MIN_TOP_GAINER_CHANGE_PCT = 2
MIN_LAST_PRICE = 0.01
MIN_VOLUME_24H_UNITS = 200000
BOT_SCAN_SECONDS = 30
DEFAULT_SCAN_SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "DOGEUSDT",
    "ADAUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "LTCUSDT",
]
ROUTER_MODES = {"conservative", "balanced", "aggressive"}
MARKET_UNIVERSE = {
    "symbols": list(DEFAULT_SCAN_SYMBOLS),
    "rows": [],
    "updatedAt": 0,
    "nextRefreshAt": 0,
    "source": "fallback",
}

def get_configured_timezone():
    return os.environ.get("TIMEZONE") or os.environ.get("TZ") or "UTC"

def get_current_trading_date_key():
    import zoneinfo
    from datetime import datetime
    tz_str = get_configured_timezone()
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    return datetime.now(tz).strftime("%Y-%m-%d")

def get_trading_day_start_epoch(date_key):
    import zoneinfo
    from datetime import datetime
    tz_str = get_configured_timezone()
    try:
        tz = zoneinfo.ZoneInfo(tz_str)
    except Exception:
        tz = zoneinfo.ZoneInfo("UTC")
    dt = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=tz)
    return int(dt.timestamp())

BOT_STATE = {
    "enabled": False,
    "symbol": "BTCUSDT",
    "interval": "5",
    "qty": "0.001",
    "maxAllocationUsdt": 250,
    "riskPerTradePct": 0.25,
    "maxOpenPositions": 1,
    "dailyLossCapUsdt": 25,
    "maxTradesPerDay": 6,
    "breakevenEnabled": True,
    "breakevenTriggerPct": 0.6,
    "partialTpEnabled": True,
    "partialTpTriggerPct": 1.4,
    "partialTpClosePct": 40,
    "trailingStopEnabled": False,
    "trailingStopTriggerPct": 1.8,
    "trailingStopDistancePct": 0.45,
    "stopLossPct": 0.8,
    "takeProfitPct": 1.6,
    "cooldownSeconds": 180,
    "lastTradeAt": 0,
    "lastSignal": "WAIT",
    "lastReason": "Auto trader is stopped.",
    "engineVotes": [],
    "mode": "conservative",
    "autoPick": True,
    "scanSymbols": list(DEFAULT_SCAN_SYMBOLS),
    "symbolSource": "top_gainers",
    "selectedSignalSymbol": "BTCUSDT",
    "router": {
        "decision": "WAIT",
        "confidence": 0,
        "requiredVotes": 1,
        "mode": "balanced",
    },
    "lastOrder": None,
    "executionGuard": {"ok": True, "reason": "No execution attempted yet"},
    "orderLifecycle": {
        "signal": "WAIT",
        "guard": "idle",
        "order": "idle",
        "protection": "idle",
        "status": "idle",
        "reason": "No execution attempted yet",
    },
    "lastRunAt": None,
    "engineStatus": {},
    "scannerRows": [],
    "positionSizing": {},
    "tradeManagement": {},
    "tradingDateKey": get_current_trading_date_key(),
}
BOT_LOCK = threading.Lock()
BOT_THREAD = None


class BotEngineV2:
    def __init__(self):
        self.version = "2.0.0"
        self.started_at = time.time()
        self.journal = []
        self.status = {
            "marketData": "idle",
            "indicator": "idle",
            "strategy": "idle",
            "router": "idle",
            "risk": "idle",
            "tradeManagement": "idle",
            "journal": "idle",
        }

    def set_status(self, engine, state):
        self.status[engine] = state

    def add_journal(self, event, payload=None):
        entry = {
            "time": int(time.time()),
            "event": event,
            "payload": payload or {},
        }
        self.journal.append(entry)
        self.journal = self.journal[-200:]
        self.set_status("journal", "ok")
        return entry

    def market_snapshot(self, symbol):
        self.set_status("marketData", "running")
        tf1h, message1h = fetch_candles(symbol, "60")
        tf15m, message15m = fetch_candles(symbol, "15")
        tf5m, message5m = fetch_candles(symbol, "5")
        ok = bool(tf1h and tf15m and tf5m)
        self.set_status("marketData", "ok" if ok else "error")
        return {
            "ok": ok,
            "timeframes": {"1H": tf1h, "15M": tf15m, "5M": tf5m},
            "message": "; ".join(x for x in [message1h, message15m, message5m] if x),
        }

    def indicators(self, snapshot):
        self.set_status("indicator", "running")
        tf = snapshot["timeframes"]
        closes_1h = [item["close"] for item in tf["1H"]]
        closes_15m = [item["close"] for item in tf["15M"]]
        closes_5m = [item["close"] for item in tf["5M"]]
        values = {
            "trendDirection1H": trend_direction(tf["1H"]),
            "rsi15M": rsi(closes_15m, 14),
            "rsi5M": rsi(closes_5m, 14),
            "ema20_1H": (ema(closes_1h, 20) or [None])[-1],
            "ema50_1H": (ema(closes_1h, 50) or [None])[-1],
            "avgVolume5M": avg_volume(tf["5M"], 20),
        }
        self.set_status("indicator", "ok")
        return values

    def strategies(self, snapshot):
        self.set_status("strategy", "running")
        tf = snapshot["timeframes"]
        votes = [
            trend_following_engine(tf["1H"], tf["15M"], tf["5M"]),
            sr_breakout_engine(tf["1H"], tf["15M"], tf["5M"]),
            rsi_divergence_engine(tf["1H"], tf["15M"], tf["5M"]),
            vwap_bounce_engine(tf["1H"], tf["15M"], tf["5M"]),
            liquidity_sweep_engine(tf["1H"], tf["15M"], tf["5M"]),
            orb_engine(tf["1H"], tf["15M"], tf["5M"]),
        ]
        self.set_status("strategy", "ok")
        return votes

    def route(self, votes, mode="balanced"):
        self.set_status("router", "running")
        router = route_votes(votes, mode)
        self.set_status("router", "ok")
        return router

    def risk_check(self, state, signal):
        self.set_status("risk", "running")
        now = time.time()
        if signal not in ("Buy", "Sell"):
            self.set_status("risk", "wait")
            return False, "No executable signal"
        policy = signal_risk_policy(state, signal)
        state["riskPolicy"] = policy
        state["riskSizeFactor"] = policy.get("sizeFactor", 1.0)
        if not policy.get("ok"):
            self.set_status("risk", "blocked")
            state["riskDecision"] = policy
            return False, policy.get("reason", "Signal risk blocked")
        if now - float(state.get("lastTradeAt") or 0) < int(state["cooldownSeconds"]):
            self.set_status("risk", "blocked")
            state["riskDecision"] = {**policy, "ok": False, "reason": "Cooldown active"}
            return False, "Cooldown active"
        position_size, position_msg = get_position_size(state["symbol"])
        if position_size is None:
            self.set_status("risk", "error")
            state["riskDecision"] = {**policy, "ok": False, "reason": position_msg}
            return False, position_msg
        if position_size > 0:
            self.set_status("risk", "blocked")
            state["riskDecision"] = {**policy, "ok": False, "reason": "Position already open"}
            return False, "Position already open"
        self.set_status("risk", "ok")
        state["riskDecision"] = {**policy, "ok": True, "reason": policy.get("reason", "Risk approved")}
        return True, state["riskDecision"]["reason"]

    def execute(self, state, signal):
        self.set_status("tradeManagement", "running")
        result = place_demo_order(
            state["symbol"],
            signal,
            state["qty"],
            "auto",
            state["stopLossPct"],
            state["takeProfitPct"],
        )
        self.set_status("tradeManagement", "ok" if result.get("retCode") == 0 else "error")
        self.add_journal("auto_order", {"symbol": state["symbol"], "signal": signal, "result": result})
        return result

    def evaluate(self, symbol, mode="balanced"):
        snapshot = self.market_snapshot(symbol)
        if not snapshot["ok"]:
            router = route_votes([], mode)
            return "WAIT", snapshot["message"], [], router, {}, dict(self.status)
        indicators = self.indicators(snapshot)
        votes = self.strategies(snapshot)
        router = self.route(votes, mode)
        return router["decision"], router["reason"], votes, router, indicators, dict(self.status)

    def overview(self):
        return {
            "version": self.version,
            "uptimeSeconds": int(time.time() - self.started_at),
            "status": dict(self.status),
            "journal": list(self.journal[-50:]),
        }


BOT_ENGINE = None


def load_env():
    if not ENV_PATH.exists():
        return
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")


def config():
    return {
        "api_key": os.environ.get("BYBIT_API_KEY", ""),
        "api_secret": os.environ.get("BYBIT_API_SECRET", ""),
        "base_url": os.environ.get("BYBIT_BASE_URL", "https://api-demo.bybit.com").rstrip("/"),
    }


def json_response(handler, status, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
    handler.end_headers()
    handler.wfile.write(body)


def read_json(handler):
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length == 0:
        return {}
    body = handler.rfile.read(length).decode("utf-8")
    return json.loads(body)


def sign(secret, payload):
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def bybit_timestamp_ms():
    cfg = config()
    request = urllib.request.Request(
        cfg["base_url"] + "/v5/market/time",
        headers={"Content-Type": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return str(int(time.time() * 1000))

    if payload.get("time"):
        return str(int(payload["time"]))

    result = payload.get("result") or {}
    if result.get("timeSecond"):
        return str(int(result["timeSecond"]) * 1000)

    return str(int(time.time() * 1000))


def bybit_request(method, path, params=None):
    cfg = config()
    if not cfg["api_key"] or not cfg["api_secret"]:
        return {
            "retCode": -1,
            "retMsg": "Missing BYBIT_API_KEY or BYBIT_API_SECRET in .env",
            "result": {},
        }

    timestamp = bybit_timestamp_ms()
    params = params or {}
    headers = {
        "Content-Type": "application/json",
        "X-BAPI-API-KEY": cfg["api_key"],
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
    }

    if method == "GET":
        query = urllib.parse.urlencode(params)
        signature_payload = timestamp + cfg["api_key"] + RECV_WINDOW + query
        url = cfg["base_url"] + path + (f"?{query}" if query else "")
        data = None
    else:
        body = json.dumps(params, separators=(",", ":"))
        signature_payload = timestamp + cfg["api_key"] + RECV_WINDOW + body
        url = cfg["base_url"] + path
        data = body.encode("utf-8")

    headers["X-BAPI-SIGN"] = sign(cfg["api_secret"], signature_payload)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(error_body)
        except json.JSONDecodeError:
            return {"retCode": exc.code, "retMsg": error_body, "result": {}}
    except Exception as exc:
        return {"retCode": -2, "retMsg": str(exc), "result": {}}


def public_bybit_get(path, params=None):
    cfg = config()
    query = urllib.parse.urlencode(params or {})
    url = cfg["base_url"] + path + (f"?{query}" if query else "")
    request = urllib.request.Request(url, headers={"Content-Type": "application/json"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {"retCode": -2, "retMsg": str(exc), "result": {}}


def numeric(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def top_gainer_universe(force=False, limit=10):
    now = int(time.time())
    if (
        not force
        and MARKET_UNIVERSE["symbols"]
        and MARKET_UNIVERSE["nextRefreshAt"] > now
    ):
        return dict(MARKET_UNIVERSE)

    payload = public_bybit_get("/v5/market/tickers", {"category": "linear"})
    rows = []
    if payload.get("retCode") == 0:
        for item in (payload.get("result") or {}).get("list") or []:
            symbol = str(item.get("symbol") or "").upper()
            if not symbol.endswith("USDT"):
                continue
            last_price = numeric(item.get("lastPrice"))
            turnover = numeric(item.get("turnover24h"))
            volume_24h = numeric(item.get("volume24h"))
            change = numeric(item.get("price24hPcnt"))
            bid = numeric(item.get("bid1Price"))
            ask = numeric(item.get("ask1Price"))
            if last_price <= 0 or turnover <= 0:
                continue
            change_pct = change * 100
            spread_pct = ((ask - bid) / last_price) * 100 if ask > 0 and bid > 0 and ask >= bid else 0
            filters = []
            if last_price < MIN_LAST_PRICE:
                filters.append("too_cheap")
            if turnover < MIN_TURNOVER_24H:
                filters.append("low_turnover")
            if volume_24h < MIN_VOLUME_24H_UNITS:
                filters.append("low_units")
            if spread_pct > MAX_SPREAD_PCT:
                filters.append("wide_spread")
            if change_pct > MAX_TOP_GAINER_CHANGE_PCT:
                filters.append("overextended")
            if change_pct < MIN_TOP_GAINER_CHANGE_PCT:
                filters.append("weak_momentum")
            if filters:
                continue
            rows.append({
                "symbol": symbol,
                "changePct": change_pct,
                "turnover24h": turnover,
                "volume24h": volume_24h,
                "spreadPct": round(spread_pct, 4),
                "lastPrice": last_price,
            })

    rows.sort(key=lambda row: (row["changePct"], row["turnover24h"]), reverse=True)
    selected = rows[:limit]
    if selected:
        MARKET_UNIVERSE.update({
            "symbols": [row["symbol"] for row in selected],
            "rows": selected,
            "updatedAt": now,
            "nextRefreshAt": now + TOP_GAINER_REFRESH_SECONDS,
            "source": "top_gainers",
        })
    else:
        MARKET_UNIVERSE.update({
            "symbols": list(DEFAULT_SCAN_SYMBOLS),
            "rows": [],
            "updatedAt": now,
            "nextRefreshAt": now + 60,
            "source": "fallback",
        })
    return dict(MARKET_UNIVERSE)


def get_mark_price(symbol):
    payload = public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    value = row.get("markPrice") or row.get("lastPrice")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def get_tick_size(symbol):
    payload = public_bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return Decimal("0.01")
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    tick = ((row.get("priceFilter") or {}).get("tickSize")) or "0.01"
    try:
        return Decimal(str(tick))
    except Exception:
        return Decimal("0.01")


def get_instrument_rules(symbol):
    payload = public_bybit_get("/v5/market/instruments-info", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return {
            "ok": False,
            "reason": payload.get("retMsg", "Instrument rules unavailable"),
            "qtyStep": Decimal("0.001"),
            "minOrderQty": Decimal("0.001"),
            "maxOrderQty": Decimal("0"),
            "minNotionalValue": Decimal("5"),
            "tickSize": Decimal("0.01"),
        }
    row = ((payload.get("result") or {}).get("list") or [{}])[0]
    lot = row.get("lotSizeFilter") or {}
    price_filter = row.get("priceFilter") or {}
    return {
        "ok": bool(row),
        "reason": "OK" if row else "Instrument not found",
        "qtyStep": Decimal(str(lot.get("qtyStep") or "0.001")),
        "minOrderQty": Decimal(str(lot.get("minOrderQty") or "0.001")),
        "maxOrderQty": Decimal(str(lot.get("maxOrderQty") or lot.get("maxMktOrderQty") or "0")),
        "minNotionalValue": Decimal(str(lot.get("minNotionalValue") or "5")),
        "tickSize": Decimal(str(price_filter.get("tickSize") or "0.01")),
    }


def format_qty(value):
    qty = Decimal(str(value))
    return format(qty.normalize(), "f")


def floor_to_step(value, step):
    qty = Decimal(str(value))
    step = Decimal(str(step))
    if step <= 0:
        return qty
    return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step


def ceil_to_step(value, step):
    qty = Decimal(str(value))
    step = Decimal(str(step))
    if step <= 0:
        return qty
    floored = floor_to_step(qty, step)
    if floored >= qty:
        return floored
    return floored + step


def get_wallet_equity():
    payload = bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Wallet check failed")
    account = ((payload.get("result") or {}).get("list") or [{}])[0]
    try:
        return float(account.get("totalEquity") or 0), "OK"
    except (TypeError, ValueError):
        return None, "Wallet equity unavailable"


def calculate_position_sizing(symbol, state):
    mark = get_mark_price(symbol)
    if not mark:
        return {"ok": False, "reason": "Mark price unavailable", "qty": "0"}
    equity, equity_msg = get_wallet_equity()
    if equity is None or equity <= 0:
        return {"ok": False, "reason": equity_msg, "qty": "0"}

    rules = get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"ok": False, "reason": rules.get("reason", "Instrument rules unavailable"), "qty": "0"}
    risk_policy = state.get("riskPolicy") if isinstance(state.get("riskPolicy"), dict) else None
    signal = state.get("signal") or state.get("side")
    has_signal_context = (
        signal in {"Buy", "Sell"}
        and (
            state.get("engineVotes")
            or state.get("strategyVotes")
            or state.get("router")
            or state.get("strategyStrength") is not None
            or state.get("consecutiveLosses") is not None
            or state.get("losingStreak") is not None
        )
    )
    if risk_policy is None and has_signal_context:
        risk_policy = signal_risk_policy(state, signal)
    if risk_policy and not risk_policy.get("ok"):
        return {
            "ok": False,
            "reason": risk_policy.get("reason", "Signal risk blocked"),
            "qty": "0",
            "riskPolicy": risk_policy,
        }
    risk_size_factor = max(0.0, min(1.0, float((risk_policy or {}).get("sizeFactor", state.get("riskSizeFactor", 1.0)) or 1.0)))
    risk_pct = max(0.01, float(state.get("riskPerTradePct") or 0.5))
    stop_pct = max(0.1, float(state.get("stopLossPct") or 0.8))
    max_allocation = max(1.0, float(state.get("maxAllocationUsdt") or 250))
    adjusted_risk_pct = risk_pct * risk_size_factor
    adjusted_max_allocation = max_allocation * risk_size_factor
    risk_amount = equity * (adjusted_risk_pct / 100)
    stop_distance = mark * (stop_pct / 100)
    qty_by_risk = Decimal(str(risk_amount / stop_distance))
    qty_by_allocation = Decimal(str(adjusted_max_allocation / mark))
    raw_qty = min(qty_by_risk, qty_by_allocation)
    qty = floor_to_step(raw_qty, rules["qtyStep"])

    min_notional_qty = Decimal("0")
    if rules["minNotionalValue"] > 0:
        min_notional_qty = Decimal(str(rules["minNotionalValue"])) / Decimal(str(mark))
        min_notional_qty = ceil_to_step(min_notional_qty, rules["qtyStep"])

    min_qty = max(rules["minOrderQty"], min_notional_qty)
    max_qty = rules.get("maxOrderQty") or Decimal("0")
    if max_qty > 0:
        qty = min(qty, max_qty)

    notional = qty * Decimal(str(mark))

    # Reject locally if:
    # - qty < minOrderQty
    # - qty > maxOrderQty
    # - qty * markPrice < minNotionalValue
    rejected = False
    if qty < rules["minOrderQty"]:
        rejected = True
    elif max_qty > 0 and qty > max_qty:
        rejected = True
    elif rules["minNotionalValue"] > 0 and notional < rules["minNotionalValue"]:
        rejected = True

    if rejected:
        return {
            "ok": False,
            "reason": "Order blocked locally: quantity/notional does not meet Bybit instrument limits.",
            "qty": "0",
            "markPrice": mark,
            "mark_price": mark,
            "rawQty": format_qty(raw_qty),
            "raw_qty": format_qty(raw_qty),
            "roundedQty": "0",
            "rounded_qty": "0",
            "minQty": format_qty(rules["minOrderQty"]),
            "min_qty": format_qty(rules["minOrderQty"]),
            "maxQty": format_qty(rules["maxOrderQty"]) if rules["maxOrderQty"] > 0 else "unlimited",
            "max_qty": format_qty(rules["maxOrderQty"]) if rules["maxOrderQty"] > 0 else "unlimited",
            "minNotionalValue": format_qty(rules["minNotionalValue"]),
            "min_notional": format_qty(rules["minNotionalValue"]),
            "estimatedNotional": "0",
            "estimated_notional": "0",
            "qtyStep": format_qty(rules["qtyStep"]),
            "qty_step": format_qty(rules["qtyStep"]),
        }

    return {
        "ok": True,
        "reason": "Position size approved",
        "qty": format_qty(qty),
        "notional": format_qty(notional),
        "equity": round(equity, 4),
        "markPrice": mark,
        "mark_price": mark,
        "rawQty": format_qty(raw_qty),
        "raw_qty": format_qty(raw_qty),
        "roundedQty": format_qty(qty),
        "rounded_qty": format_qty(qty),
        "minQty": format_qty(rules["minOrderQty"]),
        "min_qty": format_qty(rules["minOrderQty"]),
        "maxQty": format_qty(rules["maxOrderQty"]) if rules["maxOrderQty"] > 0 else "unlimited",
        "max_qty": format_qty(rules["maxOrderQty"]) if rules["maxOrderQty"] > 0 else "unlimited",
        "minNotionalValue": format_qty(rules["minNotionalValue"]),
        "min_notional": format_qty(rules["minNotionalValue"]),
        "estimatedNotional": format_qty(notional),
        "estimated_notional": format_qty(notional),
        "riskAmount": round(risk_amount, 4),
        "riskPerTradePct": risk_pct,
        "effectiveRiskPerTradePct": round(adjusted_risk_pct, 6),
        "riskSizeFactor": round(risk_size_factor, 4),
        "riskPolicy": risk_policy or {
            "ok": True,
            "reason": "Signal risk context unavailable; base sizing used",
            "sizeFactor": 1.0,
            "riskFlags": ["signal_context_unavailable"],
        },
        "maxAllocationUsdt": max_allocation,
        "effectiveMaxAllocationUsdt": round(adjusted_max_allocation, 4),
        "qtyStep": format_qty(rules["qtyStep"]),
        "qty_step": format_qty(rules["qtyStep"]),
    }


def format_price(symbol, value):
    tick = get_tick_size(symbol)
    price = Decimal(str(value))
    rounded = (price / tick).to_integral_value(rounding=ROUND_HALF_UP) * tick
    return format(rounded.normalize(), "f")


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


def simple_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0
    ranges = []
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])
        ranges.append(max(high_low, high_close, low_close))
    return sum(ranges[-period:]) / period


def adx_proxy(highs, lows, closes, period=14):
    atr = simple_atr(highs, lows, closes, period)
    if not atr or not closes[-1]:
        return 0
    return min(60, (atr / closes[-1]) * 10000)


def fetch_candles(symbol, interval, limit=120):
    payload = public_bybit_get("/v5/market/kline", {
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


def vote(engine, signal, reason, strength=0):
    return {
        "engine": engine,
        "signal": signal,
        "reason": reason,
        "strength": round(float(strength), 2),
    }


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


def avg_volume(candles, period=20):
    window = candles[-period:]
    if not window:
        return 0
    return sum(item["volume"] for item in window) / len(window)


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


def avg_range(candles):
    if not candles:
        return 0
    return sum(max(item["high"] - item["low"], 0) for item in candles) / len(candles)


def body_ratio(candle):
    candle_range = max(candle["high"] - candle["low"], 0.00000001)
    return abs(candle["close"] - candle["open"]) / candle_range


def breakout_strength(last5, previous5, level, side, volume_ratio, body, confirmed15, retested):
    close = last5["close"]
    distance_pct = abs((close - level) / level) * 100 if level else 0
    score = 2.0
    if distance_pct >= 0.08:
        score += 0.75
    if body >= 0.55:
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


def slope_pct(values, lookback=3):
    if len(values) <= lookback or not values[-lookback - 1]:
        return 0
    return ((values[-1] - values[-lookback - 1]) / values[-lookback - 1]) * 100


def structure_aligned(candles, direction, lookback=5):
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


def trend_follow_strength(separation_pct, slope, body, volume_ratio, pullback_reclaim):
    score = 2.0
    if separation_pct >= 0.12:
        score += 0.5
    if abs(slope) >= 0.08:
        score += 0.5
    if body >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if pullback_reclaim:
        score += 0.75
    return min(5.0, score)


def vwap_value(candles):
    total_volume = sum(item["volume"] for item in candles)
    if total_volume <= 0:
        return None
    return sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in candles) / total_volume


def vwap_strength(distance_pct, body, volume_ratio, reclaim, structure_ok):
    score = 2.0
    if distance_pct <= 0.18:
        score += 0.5
    if body >= 0.45:
        score += 0.5
    if volume_ratio >= 1.1:
        score += 0.5
    if reclaim:
        score += 0.75
    if structure_ok:
        score += 0.5
    return min(5.0, score)


def vwap_structure_aligned(candles, direction, lookback=6):
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


def pivot_points(candles, field, kind, left=2, right=2):
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


def rsi_at(closes, index, period=14):
    if index < period:
        return 50
    return rsi(closes[: index + 1], period)


def divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_through):
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


def wick_ratio(candle, side):
    candle_range = max(candle["high"] - candle["low"], 0.00000001)
    if side == "Buy":
        wick = min(candle["open"], candle["close"]) - candle["low"]
    else:
        wick = candle["high"] - max(candle["open"], candle["close"])
    return max(wick, 0) / candle_range


def liquidity_sweep_strength(sweep_pct, wick, volume_ratio, body, follow_through):
    score = 2.0
    if sweep_pct >= 0.08:
        score += 0.5
    if wick >= 0.45:
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
    slope = slope_pct(ema20_1h, 3)
    slope_ok = (direction == "Buy" and slope > 0.04) or (direction == "Sell" and slope < -0.04)
    if separation_pct < 0.06 or not slope_ok:
        return vote("Trend Follow", "WAIT", "1H trend lacks EMA separation or slope strength")

    closes15 = [item["close"] for item in tf15m]
    ema20_15 = ema(closes15, 20)
    if not ema20_15:
        return vote("Trend Follow", "WAIT", "15M EMA setup unavailable")
    last15 = tf15m[-1]
    structure_ok = structure_aligned(tf15m, direction, 6)
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
    body = body_ratio(last5)
    if entry != direction or not follow_through or body < 0.35 or volume_ratio < 0.95:
        return vote("Trend Follow", "WAIT", f"1H {direction}; waiting for 5M body, volume, and follow-through")

    strength = trend_follow_strength(separation_pct, slope, body, volume_ratio, pullback)
    return vote("Trend Follow", direction, f"1H {direction} trend, 15M EMA reclaim, 5M follow-through confirmed", strength)


def sr_breakout_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 31 or len(tf15m) < 30 or len(tf5m) < 21:
        return vote("S/R Breakout", "WAIT", "Not enough closed structure for breakout confirmation")

    support, resistance = swing_zone(tf1h[-31:-1], 30)
    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    last15 = tf15m[-1]
    range15 = max(item["high"] for item in tf15m[-8:]) - min(item["low"] for item in tf15m[-8:])
    avg_range15 = avg_range(tf15m[-30:])
    avg_range5 = avg_range(tf5m[-21:-1])
    consolidating = range15 <= avg_range15 * 4
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    body = body_ratio(last5)
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
        strength = breakout_strength(last5, prev5, resistance, "Buy", volume_ratio, body, confirmed_buy_15, retested_buy)
        return vote("S/R Breakout", "Buy", "Confirmed resistance breakout with 15M close, 5M body, and volume", strength)

    if last5["close"] < support:
        if not consolidating:
            return vote("S/R Breakout", "WAIT", "Breakdown ignored: 15M structure is too expanded")
        if oversized:
            return vote("S/R Breakout", "WAIT", "Breakdown ignored: 5M candle is abnormally extended")
        if not confirmed_sell_15 or not body_ok or not volume_ok:
            return vote("S/R Breakout", "WAIT", "Support break needs 15M close, strong body, and volume confirmation")
        strength = breakout_strength(last5, prev5, support, "Sell", volume_ratio, body, confirmed_sell_15, retested_sell)
        return vote("S/R Breakout", "Sell", "Confirmed support breakdown with 15M close, 5M body, and volume", strength)

    return vote("S/R Breakout", "WAIT", "No confirmed 1H support/resistance breakout")


def rsi_divergence_engine(tf1h, tf15m, tf5m):
    if len(tf15m) < 35 or len(tf5m) < 21:
        return vote("RSI Divergence", "WAIT", "Not enough closed candles for pivot RSI divergence")

    direction = trend_direction(tf1h)
    closes15 = [item["close"] for item in tf15m]
    recent = tf15m[-34:]
    lows = pivot_points(recent, "low", "low")
    highs = pivot_points(recent, "high", "high")
    rsi_now = rsi(closes15, 14)
    last5 = tf5m[-1]
    prev5 = tf5m[-2]
    entry = candle_direction(last5)
    body = body_ratio(last5)
    volume_baseline = avg_volume(tf5m[-21:-1], 20)
    volume_ratio = last5["volume"] / volume_baseline if volume_baseline > 0 else 0
    follow_buy = last5["close"] > prev5["close"]
    follow_sell = last5["close"] < prev5["close"]

    if len(lows) >= 2:
        first, second = lows[-2], lows[-1]
        first_idx = len(tf15m) - len(recent) + first[0]
        second_idx = len(tf15m) - len(recent) + second[0]
        rsi_first = rsi_at(closes15, first_idx)
        rsi_second = rsi_at(closes15, second_idx)
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
            strength = divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_buy)
            return vote("RSI Divergence", "Buy", f"15M bullish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", strength)

    if len(highs) >= 2:
        first, second = highs[-2], highs[-1]
        first_idx = len(tf15m) - len(recent) + first[0]
        second_idx = len(tf15m) - len(recent) + second[0]
        rsi_first = rsi_at(closes15, first_idx)
        rsi_second = rsi_at(closes15, second_idx)
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
            strength = divergence_strength(rsi_delta, body, volume_ratio, price_move_pct, follow_sell)
            return vote("RSI Divergence", "Sell", f"15M bearish pivot divergence confirmed, RSI {rsi_now:.1f}, delta {rsi_delta:.1f}", strength)

    return vote("RSI Divergence", "WAIT", f"No confirmed pivot divergence, RSI {rsi_now:.1f}")


def vwap_bounce_engine(tf1h, tf15m, tf5m):
    if len(tf1h) < 55 or len(tf15m) < 40 or len(tf5m) < 21:
        return vote("VWAP Bounce", "WAIT", "Not enough closed candles for VWAP confirmation")

    direction = trend_direction(tf1h)
    if direction == "WAIT":
        return vote("VWAP Bounce", "WAIT", "1H trend not clean for VWAP bounce")

    vwap = vwap_value(tf15m[-40:])
    if vwap is None:
        return vote("VWAP Bounce", "WAIT", "15M VWAP volume unavailable")

    last15 = tf15m[-1]
    prev15 = tf15m[-2]
    distance_pct = abs((last15["close"] - vwap) / vwap) * 100 if vwap else 999
    near_vwap = distance_pct <= 0.35
    structure_ok = vwap_structure_aligned(tf15m, direction, 6)
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
    body = body_ratio(last5)
    if entry != direction or not follow_through or body < 0.35 or volume_ratio < 0.95:
        return vote("VWAP Bounce", "WAIT", f"1H {direction}; waiting for 5M VWAP bounce body, volume, and follow-through")

    strength = vwap_strength(distance_pct, body, volume_ratio, reclaim, structure_ok)
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
    body = body_ratio(last5)

    swept_low = last15["low"] < prior_low and last15["close"] > prior_low
    swept_high = last15["high"] > prior_high and last15["close"] < prior_high

    if swept_low:
        wick = wick_ratio(last15, "Buy")
        sweep_pct = ((prior_low - last15["low"]) / prior_low) * 100 if prior_low else 0
        follow = last5["close"] > prev5["close"] and candle_direction(last5) == "Buy"
        if direction == "Sell":
            return vote("Liquidity Sweep", "WAIT", "Bullish liquidity sweep blocked by strong 1H downtrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bullish sweep needs wick rejection, volume, and 5M follow-through")
        strength = liquidity_sweep_strength(sweep_pct, wick, volume_ratio, body, follow)
        return vote("Liquidity Sweep", "Buy", "Bullish stop-hunt sweep below 15M range with reclaim confirmed", strength)

    if swept_high:
        wick = wick_ratio(last15, "Sell")
        sweep_pct = ((last15["high"] - prior_high) / prior_high) * 100 if prior_high else 0
        follow = last5["close"] < prev5["close"] and candle_direction(last5) == "Sell"
        if direction == "Buy":
            return vote("Liquidity Sweep", "WAIT", "Bearish liquidity sweep blocked by strong 1H uptrend")
        if wick < 0.35 or volume_ratio < 1.05 or body < 0.25 or not follow:
            return vote("Liquidity Sweep", "WAIT", "Bearish sweep needs wick rejection, volume, and 5M follow-through")
        strength = liquidity_sweep_strength(sweep_pct, wick, volume_ratio, body, follow)
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
    volume_ok = last5["volume"] >= avg_volume(tf5m, 20) * 1.08

    if last15["close"] > high and last5["close"] > high and volume_ok:
        return vote("ORB", "Buy", "1H opening range high broken, 15M/5M confirmed", last5["close"] - high)
    if last15["close"] < low and last5["close"] < low and volume_ok:
        return vote("ORB", "Sell", "1H opening range low broken, 15M/5M confirmed", low - last5["close"])
    return vote("ORB", "WAIT", "Opening range not confirmed on 15M and 5M")


def legacy_trend_engine(closes, highs, lows):
    fast = ema(closes, 9)
    mid = ema(closes, 21)
    slow = ema(closes, 50)
    current_rsi = rsi(closes, 14)
    trend_strength = adx_proxy(highs, lows, closes)
    if len(fast) < 3 or len(mid) < 3 or len(slow) < 3:
        return vote("Trend", "WAIT", "Not enough EMA values")

    bullish_stack = fast[-1] > mid[-1] > slow[-1]
    bearish_stack = fast[-1] < mid[-1] < slow[-1]
    if bullish_stack and current_rsi < 68 and trend_strength >= 6:
        return vote("Trend", "Buy", f"EMA stack bullish, RSI {current_rsi:.1f}", trend_strength)
    if bearish_stack and current_rsi > 32 and trend_strength >= 6:
        return vote("Trend", "Sell", f"EMA stack bearish, RSI {current_rsi:.1f}", trend_strength)
    return vote("Trend", "WAIT", f"No clean trend, RSI {current_rsi:.1f}", trend_strength)


def legacy_vwap_engine(candles):
    window = candles[-40:]
    total_volume = sum(item["volume"] for item in window)
    if total_volume <= 0:
        return vote("VWAP", "WAIT", "No volume")
    vwap = sum(((item["high"] + item["low"] + item["close"]) / 3) * item["volume"] for item in window) / total_volume
    last = candles[-1]
    prev = candles[-2]
    closes = [item["close"] for item in candles]
    current_rsi = rsi(closes, 14)
    distance = ((last["close"] - vwap) / vwap) * 100
    near_vwap = abs(distance) <= 0.18
    volume_avg = sum(item["volume"] for item in candles[-21:-1]) / 20
    volume_ok = last["volume"] >= volume_avg * 1.05

    if near_vwap and last["close"] > prev["close"] and current_rsi < 66 and volume_ok:
        return vote("VWAP", "Buy", f"Pullback reclaimed VWAP, RSI {current_rsi:.1f}", abs(distance))
    if near_vwap and last["close"] < prev["close"] and current_rsi > 34 and volume_ok:
        return vote("VWAP", "Sell", f"VWAP rejection, RSI {current_rsi:.1f}", abs(distance))
    return vote("VWAP", "WAIT", f"VWAP distance {distance:.2f}%, RSI {current_rsi:.1f}", abs(distance))


def legacy_breakout_engine(candles):
    if len(candles) < 35:
        return vote("Breakout", "WAIT", "Not enough candles")
    lookback = candles[-31:-1]
    last = candles[-1]
    high_break = max(item["high"] for item in lookback)
    low_break = min(item["low"] for item in lookback)
    volume_avg = sum(item["volume"] for item in lookback) / len(lookback)
    volume_spike = last["volume"] >= volume_avg * 1.25
    breakout_margin = max(last["close"] - high_break, low_break - last["close"], 0)

    if last["close"] > high_break and volume_spike:
        return vote("Breakout", "Buy", "Range high breakout with volume", breakout_margin)
    if last["close"] < low_break and volume_spike:
        return vote("Breakout", "Sell", "Range low breakdown with volume", breakout_margin)
    return vote("Breakout", "WAIT", "No confirmed breakout", breakout_margin)


def normalize_mode(mode):
    mode = str(mode or "balanced").lower()
    return mode if mode in ROUTER_MODES else "balanced"


def vote_strength(vote_item):
    try:
        return abs(float(vote_item.get("strength") or 0))
    except (TypeError, ValueError):
        return 0.0


def single_vote_min_strength(mode):
    if mode == "aggressive":
        return 2.0
    if mode == "balanced":
        return 3.0
    return 0.0


def route_votes(votes, mode="balanced"):
    mode = normalize_mode(mode)
    buy_votes = [item for item in votes if item["signal"] == "Buy"]
    sell_votes = [item for item in votes if item["signal"] == "Sell"]
    required = 2 if mode == "conservative" else 1
    if mode == "aggressive":
        buy_score = len(buy_votes) + sum(vote_strength(item) for item in buy_votes) / 100
        sell_score = len(sell_votes) + sum(vote_strength(item) for item in sell_votes) / 100
        if buy_votes and buy_score > sell_score:
            leader = max(buy_votes, key=vote_strength)
            if len(buy_votes) == 1 and vote_strength(leader) < single_vote_min_strength(mode):
                return {
                    "decision": "WAIT",
                    "confidence": len(buy_votes),
                    "requiredVotes": required,
                    "mode": mode,
                    "reason": f"Router waiting: single Buy vote strength {vote_strength(leader):.2f} is below {single_vote_min_strength(mode):.2f}",
                }
            return {
                "decision": "Buy",
                "confidence": len(buy_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Buy from {leader['engine']}",
            }
        if sell_votes and sell_score > buy_score:
            leader = max(sell_votes, key=vote_strength)
            if len(sell_votes) == 1 and vote_strength(leader) < single_vote_min_strength(mode):
                return {
                    "decision": "WAIT",
                    "confidence": len(sell_votes),
                    "requiredVotes": required,
                    "mode": mode,
                    "reason": f"Router waiting: single Sell vote strength {vote_strength(leader):.2f} is below {single_vote_min_strength(mode):.2f}",
                }
            return {
                "decision": "Sell",
                "confidence": len(sell_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Sell from {leader['engine']}",
            }
    if len(buy_votes) >= required and not sell_votes:
        leader = max(buy_votes, key=vote_strength)
        if len(buy_votes) == 1 and vote_strength(leader) < single_vote_min_strength(mode):
            return {
                "decision": "WAIT",
                "confidence": len(buy_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Router waiting: single Buy vote strength {vote_strength(leader):.2f} is below {single_vote_min_strength(mode):.2f}",
            }
        return {
            "decision": "Buy",
            "confidence": len(buy_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Buy from {leader['engine']}",
        }
    if len(sell_votes) >= required and not buy_votes:
        leader = max(sell_votes, key=vote_strength)
        if len(sell_votes) == 1 and vote_strength(leader) < single_vote_min_strength(mode):
            return {
                "decision": "WAIT",
                "confidence": len(sell_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Router waiting: single Sell vote strength {vote_strength(leader):.2f} is below {single_vote_min_strength(mode):.2f}",
            }
        return {
            "decision": "Sell",
            "confidence": len(sell_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Sell from {leader['engine']}",
        }
    if buy_votes and sell_votes:
        reason = "Router waiting because Buy/Sell engines conflict"
    elif mode == "conservative":
        reason = "Router waiting for 2 matching engine votes"
    else:
        reason = "Router waiting for at least 1 actionable engine vote"
    return {
        "decision": "WAIT",
        "confidence": max(len(buy_votes), len(sell_votes)),
        "requiredVotes": required,
        "mode": mode,
        "reason": reason,
    }


def get_position_size(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Position check failed")
    positions = (payload.get("result") or {}).get("list") or []
    total_size = 0.0
    for position in positions:
        try:
            total_size += abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            pass
    return total_size, "OK"


def get_open_positions_count():
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Open position check failed")
    count = 0
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            if abs(float(position.get("size") or 0)) > 0:
                count += 1
        except (TypeError, ValueError):
            pass
    return count, "OK"


def local_day_start_epoch():
    return get_trading_day_start_epoch(get_current_trading_date_key())


def check_and_reset_daily_state(state):
    current_date = get_current_trading_date_key()
    last_date = state.get("tradingDateKey")
    if last_date != current_date:
        state["tradingDateKey"] = current_date

        if "dailyRisk" in state:
            state["dailyRisk"]["tradesToday"] = 0
            state["dailyRisk"]["lossUsed"] = 0.0
            state["dailyRisk"]["dailyLossUsed"] = 0.0
            state["dailyRisk"]["blocked"] = False
            state["dailyRisk"]["reason"] = "New trading day started."

        if state.get("lastReason") and ("Daily loss cap" in state["lastReason"] or "Max trades/day" in state["lastReason"]):
            state["lastReason"] = f"New trading day started ({current_date}). Auto trader is active."

        if state.get("executionGuard") and not state["executionGuard"].get("ok") and ("Daily loss cap" in state["executionGuard"].get("reason", "") or "Max trades/day" in state["executionGuard"].get("reason", "")):
            state["executionGuard"] = {"ok": True, "reason": "New trading day started."}

        if state.get("orderLifecycle"):
            if state["orderLifecycle"].get("status") == "blocked":
                state["orderLifecycle"] = order_lifecycle(
                    signal="WAIT",
                    guard="idle",
                    order="idle",
                    protection="idle",
                    status="idle",
                    reason="New trading day started."
                )


def count_today_accepted_orders(engine, date_key):
    start = get_trading_day_start_epoch(date_key)
    count = 0
    entries = []
    if hasattr(engine, "journal") and engine.journal is not None:
        if hasattr(engine.journal, "entries"):
            entries = engine.journal.entries
        elif isinstance(engine.journal, list):
            entries = engine.journal

    for entry in entries:
        if int(entry.get("time") or 0) < start:
            continue
        if entry.get("event") not in ("auto_order", "manual_order"):
            continue
        payload = entry.get("payload") or {}
        result = payload.get("result") or {}
        try:
            accepted = int(result.get("retCode")) == 0
        except (TypeError, ValueError):
            accepted = False
        if accepted:
            count += 1
    return count


def get_daily_closed_pnl(date_key):
    start_epoch = get_trading_day_start_epoch(date_key)
    start_ms = start_epoch * 1000

    # 1. Fetch today's accepted orders from the journal
    engine = get_bot_engine()
    accepted_orders = []
    entries = []
    if hasattr(engine, "journal") and engine.journal is not None:
        if hasattr(engine.journal, "entries"):
            entries = engine.journal.entries
        elif isinstance(engine.journal, list):
            entries = engine.journal

    for entry in entries:
        t = int(entry.get("time") or 0)
        if t < start_epoch:
            continue
        if entry.get("event") not in ("auto_order", "manual_order", "partial_take_profit", "kill_switch"):
            continue
        payload = entry.get("payload") or {}

        def extract_order_info(res, symbol, side, event):
            if not res or not isinstance(res, dict):
                return
            if int(res.get("retCode", -1)) == 0:
                res_data = res.get("result") or {}
                order_id = res_data.get("orderId")
                order_link_id = res_data.get("orderLinkId")
                if order_id or order_link_id:
                    accepted_orders.append({
                        "symbol": symbol,
                        "orderId": order_id,
                        "orderLinkId": order_link_id,
                        "time": t,
                        "event": event,
                        "side": side
                    })

        event = entry.get("event")
        if event in ("auto_order", "manual_order"):
            symbol = payload.get("symbol")
            side = payload.get("signal") or payload.get("side")
            result = payload.get("result") or {}
            extract_order_info(result, symbol, side, event)
        elif event == "partial_take_profit":
            symbol = payload.get("symbol")
            side = payload.get("side")
            result = payload.get("result") or {}
            extract_order_info(result, symbol, side, event)
        elif event == "kill_switch":
            close_result = payload.get("closeResult") or {}
            for res in close_result.get("orders") or []:
                symbol = payload.get("symbol")
                extract_order_info(res, symbol, None, event)

    # If there are no accepted orders from today, return 0.0 immediately
    if not accepted_orders:
        return 0.0, "OK"

    # 2. Fetch closed PnL from Bybit
    payload = bybit_request("GET", "/v5/position/closed-pnl", {
        "category": "linear",
        "startTime": str(start_ms),
        "limit": "100",
    })
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Closed PnL check failed")

    closed_pnl_list = (payload.get("result") or {}).get("list") or []
    if not closed_pnl_list:
        return 0.0, "OK"

    # 3. Fetch execution list from Bybit for today to link executions
    exec_payload = bybit_request("GET", "/v5/execution/list", {
        "category": "linear",
        "startTime": str(start_ms),
        "limit": "100",
    })

    executions = []
    if exec_payload.get("retCode") == 0:
        executions = (exec_payload.get("result") or {}).get("list") or []

    # Map our today's entry/opening orders by symbol
    today_entry_symbols = {order["symbol"] for order in accepted_orders if order["event"] in ("auto_order", "manual_order")}

    # Build entry execution times and closing execution times
    today_entry_order_ids = {order["orderId"] for order in accepted_orders if order["orderId"] and order["event"] in ("auto_order", "manual_order")}
    today_entry_order_link_ids = {order["orderLinkId"] for order in accepted_orders if order["orderLinkId"] and order["event"] in ("auto_order", "manual_order")}

    entry_exec_times_by_symbol = {}
    for ex in executions:
        ex_order_id = ex.get("orderId")
        ex_order_link_id = ex.get("orderLinkId")
        ex_symbol = ex.get("symbol")
        try:
            ex_time = int(ex.get("execTime") or 0)
        except (TypeError, ValueError):
            ex_time = 0

        if ex_order_id in today_entry_order_ids or ex_order_link_id in today_entry_order_link_ids:
            entry_exec_times_by_symbol.setdefault(ex_symbol, []).append(ex_time)

    pnl = 0.0
    for row in closed_pnl_list:
        try:
            row_time_str = row.get("updatedTime") or row.get("createdTime") or str(start_ms)
            row_time = int(row_time_str)
            if row_time < start_ms:
                continue

            row_symbol = row.get("symbol")
            row_order_id = row.get("orderId")
            row_closed_pnl = float(row.get("closedPnl") or 0)

            # Determine when this position was closed
            close_time = row_time
            for ex in executions:
                if ex.get("orderId") == row_order_id:
                    try:
                        close_time = int(ex.get("execTime") or row_time)
                    except (TypeError, ValueError):
                        close_time = row_time
                    break

            # Now, check if this closed PnL was opened today.
            is_today_trade = False
            if entry_exec_times_by_symbol.get(row_symbol):
                for entry_time in entry_exec_times_by_symbol[row_symbol]:
                    if entry_time <= close_time:
                        is_today_trade = True
                        break
            else:
                # Fallback: if no entry execution list is available, check if the symbol has an entry order today
                if row_symbol in today_entry_symbols:
                    is_today_trade = True

            if is_today_trade:
                pnl += row_closed_pnl
        except (TypeError, ValueError):
            pass

    return pnl, "OK"


def daily_loss_cap_reached(state):
    cap = max(0.0, float(state.get("dailyLossCapUsdt") or 0))
    if cap <= 0:
        return False, "Daily risk OK"
    date_key = get_current_trading_date_key()
    closed_pnl, pnl_msg = get_daily_closed_pnl(date_key)
    if closed_pnl is None:
        return False, f"Could not check closed PnL: {pnl_msg}"
    loss_used = abs(min(0.0, closed_pnl))
    if loss_used >= cap:
        return True, "Daily loss cap reached. Trading locked for today."
    return False, "Daily risk OK"


def daily_risk_report(state):
    cap = max(0.0, float(state.get("dailyLossCapUsdt") or 0))
    max_trades = max(1, int(state.get("maxTradesPerDay") or 1))
    date_key = get_current_trading_date_key()
    closed_pnl, pnl_msg = get_daily_closed_pnl(date_key)
    if closed_pnl is None:
        return {
            "ok": False,
            "blocked": True,
            "reason": pnl_msg,
            "dailyLossCapUsdt": cap,
            "maxTradesPerDay": max_trades,
            "tradingDateKey": date_key,
        }
    engine = get_bot_engine()
    trades_today = count_today_accepted_orders(engine, date_key)
    loss_used = abs(min(0.0, closed_pnl))
    blocked = (cap > 0 and loss_used >= cap) or trades_today >= max_trades
    if cap > 0 and loss_used >= cap:
        reason = f"Daily loss cap reached (${loss_used:.2f}/${cap:.2f})"
    elif trades_today >= max_trades:
        reason = f"Max trades/day reached ({trades_today}/{max_trades})"
    else:
        reason = "Daily risk OK"
    return {
        "ok": True,
        "blocked": blocked,
        "reason": reason,
        "closedPnl": round(closed_pnl, 4),
        "lossUsed": round(loss_used, 4),
        "dailyLossUsed": round(loss_used, 4),
        "dailyLossCapUsdt": cap,
        "tradesToday": trades_today,
        "maxTradesPerDay": max_trades,
        "dayStart": get_trading_day_start_epoch(date_key),
        "tradingDateKey": date_key,
    }


def get_debug_risk_info(state):
    date_key = get_current_trading_date_key()
    start_epoch = get_trading_day_start_epoch(date_key)

    closed_pnl, pnl_msg = get_daily_closed_pnl(date_key)
    loss_used = abs(min(0.0, closed_pnl)) if closed_pnl is not None else 0.0

    engine = get_bot_engine()
    trades_today = count_today_accepted_orders(engine, date_key)

    cap = max(0.0, float(state.get("dailyLossCapUsdt") or 0))
    max_trades = max(1, int(state.get("maxTradesPerDay") or 1))

    lock_reason = "Daily risk OK"
    if cap > 0 and loss_used >= cap:
        lock_reason = f"Daily loss cap reached (${loss_used:.2f}/${cap:.2f})"
    elif trades_today >= max_trades:
        lock_reason = f"Max trades/day reached ({trades_today}/{max_trades})"

    return {
        "tradingDateKey": date_key,
        "dayStartEpoch": start_epoch,
        "timezone": get_configured_timezone(),
        "tradesToday": {
            "source": "journal_accepted_orders",
            "count": trades_today,
            "max": max_trades,
        },
        "dailyLossUsed": {
            "source": "bybit_closed_pnl",
            "value": loss_used,
            "cap": cap,
        },
        "lockReason": lock_reason,
        "locked": (cap > 0 and loss_used >= cap) or trades_today >= max_trades,
    }


def get_open_positions():
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Open position check failed")
    positions = []
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            positions.append(position)
    return positions, "OK"


def get_symbol_open_positions(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return None, payload.get("retMsg", "Symbol position check failed")
    positions = []
    for position in (payload.get("result") or {}).get("list") or []:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            positions.append(position)
    return positions, "OK"


def summarize_position(position):
    try:
        size = abs(float(position.get("size") or 0))
    except (TypeError, ValueError):
        size = 0
    return {
        "symbol": position.get("symbol", ""),
        "side": position.get("side", ""),
        "size": size,
        "avgPrice": position.get("avgPrice"),
        "markPrice": position.get("markPrice"),
        "stopLoss": position.get("stopLoss"),
        "takeProfit": position.get("takeProfit"),
        "trailingStop": position.get("trailingStop"),
        "positionIdx": position.get("positionIdx"),
    }


def order_lifecycle(signal="WAIT", guard="idle", order="idle", protection="idle", status="idle", reason=""):
    return {
        "signal": signal,
        "guard": guard,
        "order": order,
        "protection": protection,
        "status": status,
        "reason": reason,
    }


def existing_position_guard(symbol, signal, state):
    positions, msg = get_symbol_open_positions(symbol)
    if positions is None:
        return {
            "ok": False,
            "reason": msg,
            "positions": [],
            "sameDirection": False,
            "oppositeDirection": False,
        }

    signal_side = "Buy" if signal == "Buy" else "Sell"
    summaries = [summarize_position(position) for position in positions]
    same = [position for position in summaries if position["side"] == signal_side]
    opposite = [position for position in summaries if position["side"] and position["side"] != signal_side]
    if same:
        return {
            "ok": False,
            "reason": f"Existing {symbol} {signal_side} position detected; duplicate entry blocked",
            "positions": summaries,
            "sameDirection": True,
            "oppositeDirection": False,
        }
    if opposite:
        return {
            "ok": False,
            "reason": f"Existing {symbol} opposite position detected; reverse trade blocked",
            "positions": summaries,
            "sameDirection": False,
            "oppositeDirection": True,
        }

    open_count, open_msg = get_open_positions_count()
    if open_count is None:
        return {
            "ok": False,
            "reason": open_msg,
            "positions": summaries,
            "sameDirection": False,
            "oppositeDirection": False,
        }
    max_open = max(1, int(state.get("maxOpenPositions") or 1))
    if open_count >= max_open:
        all_positions_payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "settleCoin": "USDT"})
        active_symbols = []
        if all_positions_payload.get("retCode") == 0:
            for row in (all_positions_payload.get("result") or {}).get("list") or []:
                try:
                    size = abs(float(row.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                if size > 0 and row.get("symbol"):
                    active_symbols.append(str(row.get("symbol")))
        symbol_text = ", ".join(active_symbols[:5]) if active_symbols else symbol
        return {
            "ok": False,
            "reason": f"Max open positions reached ({open_count}/{max_open}); active: {symbol_text}",
            "positions": summaries,
            "openPositions": open_count,
            "maxOpenPositions": max_open,
            "sameDirection": False,
            "oppositeDirection": False,
        }

    return {
        "ok": True,
        "reason": "No existing position conflict",
        "positions": summaries,
        "openPositions": open_count,
        "maxOpenPositions": max_open,
        "sameDirection": False,
        "oppositeDirection": False,
    }


def position_key(position):
    symbol = position.get("symbol", "")
    open_time = position.get("openTime") or position.get("createdTime") or position.get("updatedTime") or "0"
    side = position.get("side", "")
    return f"{symbol}:{side}:{open_time}"


def journal_has_position_event(event, key):
    for entry in getattr(get_bot_engine().journal, "entries", []):
        if entry.get("event") != event:
            continue
        payload = entry.get("payload") or {}
        result = payload.get("result") or {}
        try:
            accepted = int(result.get("retCode")) == 0
        except (TypeError, ValueError):
            accepted = False
        if payload.get("positionKey") == key and accepted:
            return True
    return False


def close_partial_position(position, close_pct):
    symbol = position.get("symbol")
    side = position.get("side")
    try:
        size = Decimal(str(abs(float(position.get("size") or 0))))
    except (TypeError, ValueError):
        return {"ok": False, "reason": "Position size unavailable"}
    if not symbol or not side or size <= 0:
        return {"ok": False, "reason": "No closeable position"}

    rules = get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"ok": False, "reason": rules.get("reason", "Instrument rules unavailable")}
    close_ratio = Decimal(str(max(1, min(100, float(close_pct))) / 100))
    close_qty = floor_to_step(size * close_ratio, rules["qtyStep"])
    if close_qty < rules["minOrderQty"] and size >= rules["minOrderQty"]:
        close_qty = min(size, rules["minOrderQty"])
    if close_qty <= 0 or close_qty > size:
        return {"ok": False, "reason": "Partial close qty below exchange minimum"}
    mark_price = Decimal(str(get_mark_price(symbol) or "0"))
    close_notional = close_qty * mark_price
    if mark_price <= 0:
        return {"ok": False, "reason": "Partial close mark price unavailable"}
    if close_notional < rules["minNotionalValue"]:
        return {
            "ok": False,
            "reason": "Partial close below exchange min notional",
            "qty": format_qty(close_qty),
            "notional": format_qty(close_notional),
            "minNotionalValue": format_qty(rules["minNotionalValue"]),
        }

    close_side = "Sell" if side == "Buy" else "Buy"
    order = {
        "category": "linear",
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": format_qty(close_qty),
        "reduceOnly": True,
        "timeInForce": "IOC",
        "orderLinkId": generate_order_link_id("partial"),
    }
    if position.get("positionIdx") is not None:
        order["positionIdx"] = int(position.get("positionIdx") or 0)
    return bybit_request("POST", "/v5/order/create", order)


def set_trailing_stop(position, distance_pct):
    symbol = position.get("symbol")
    side = position.get("side")
    try:
        mark_price = float(position.get("markPrice") or 0)
    except (TypeError, ValueError):
        mark_price = 0
    if not symbol or not side or mark_price <= 0:
        return {"retCode": -1, "retMsg": "Trailing stop mark price unavailable"}

    distance = mark_price * (max(0.05, float(distance_pct)) / 100)
    if side == "Buy":
        active_price = mark_price * 0.999
    else:
        active_price = mark_price * 1.001
    body = {
        "category": "linear",
        "symbol": symbol,
        "tpslMode": "Full",
        "trailingStop": format_price(symbol, distance),
        "activePrice": format_price(symbol, active_price),
    }
    if position.get("positionIdx") is not None:
        body["positionIdx"] = int(position.get("positionIdx") or 0)
    return bybit_request("POST", "/v5/position/trading-stop", body)


def manage_open_positions(state):
    positions, msg = get_open_positions()
    if positions is None:
        return {"ok": False, "actions": [], "reason": msg}

    actions = []
    breakeven_trigger_pct = max(0.1, float(state.get("breakevenTriggerPct") or 0.6))
    partial_trigger_pct = max(0.1, float(state.get("partialTpTriggerPct") or 1.0))
    partial_close_pct = max(1, min(100, float(state.get("partialTpClosePct") or 50)))
    trailing_trigger_pct = max(0.1, float(state.get("trailingStopTriggerPct") or 0.8))
    trailing_distance_pct = max(0.05, float(state.get("trailingStopDistancePct") or 0.35))
    for position in positions:
        symbol = position.get("symbol")
        side = position.get("side")
        try:
            avg_price = float(position.get("avgPrice") or 0)
            mark_price = float(position.get("markPrice") or 0)
        except (TypeError, ValueError):
            continue
        if not symbol or avg_price <= 0 or mark_price <= 0:
            continue
        key = position_key(position)
        if side == "Buy":
            pnl_pct = ((mark_price - avg_price) / avg_price) * 100
            breakeven_price = avg_price * 1.0002
            already_safe = float(position.get("stopLoss") or 0) >= avg_price
        else:
            pnl_pct = ((avg_price - mark_price) / avg_price) * 100
            breakeven_price = avg_price * 0.9998
            stop_loss = float(position.get("stopLoss") or 0)
            already_safe = stop_loss > 0 and stop_loss <= avg_price

        if (
            state.get("partialTpEnabled", True)
            and pnl_pct >= partial_trigger_pct
            and not journal_has_position_event("partial_take_profit", key)
        ):
            result = close_partial_position(position, partial_close_pct)
            action = {
                "type": "partial_take_profit",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "closePct": partial_close_pct,
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("partial_take_profit", action)
            get_bot_engine().set_status("journal", "ok")

        if state.get("breakevenEnabled", True) and pnl_pct >= breakeven_trigger_pct and not already_safe:
            body = {
                "category": "linear",
                "symbol": symbol,
                "tpslMode": "Full",
                "stopLoss": format_price(symbol, breakeven_price),
            }
            if position.get("positionIdx") is not None:
                body["positionIdx"] = int(position.get("positionIdx") or 0)
            result = bybit_request("POST", "/v5/position/trading-stop", body)
            action = {
                "type": "breakeven_stop",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "stopLoss": body["stopLoss"],
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("breakeven_stop", action)
            get_bot_engine().set_status("journal", "ok")

        if (
            state.get("trailingStopEnabled", True)
            and pnl_pct >= trailing_trigger_pct
            and not journal_has_position_event("trailing_stop_enabled", key)
        ):
            result = set_trailing_stop(position, trailing_distance_pct)
            action = {
                "type": "trailing_stop_enabled",
                "positionKey": key,
                "symbol": symbol,
                "side": side,
                "pnlPct": round(pnl_pct, 4),
                "distancePct": trailing_distance_pct,
                "result": result,
            }
            actions.append(action)
            get_bot_engine().journal.add("trailing_stop_enabled", action)
            get_bot_engine().set_status("journal", "ok")
    return {"ok": True, "actions": actions, "reason": "Managed open positions"}


def get_bot_engine():
    global BOT_ENGINE
    if BOT_ENGINE is None:
        BOT_ENGINE = ModularBotEngineV2(config()["base_url"], bybit_request, get_position_size, get_open_positions_count)
    return BOT_ENGINE


def evaluate_signal(symbol, interval, mode="balanced"):
    signal, reason, votes, router, indicators, status = get_bot_engine().evaluate(symbol, mode)
    return signal, reason, votes, router, indicators, status


def signal_score(row):
    router = row.get("router") or {}
    signal = row.get("signal")
    if signal not in ("Buy", "Sell"):
        return -1
    matching_votes = [item for item in row.get("engineVotes", []) if item.get("signal") == signal]
    return (int(router.get("confidence") or 0) * 1000) + sum(vote_strength(item) for item in matching_votes)


def select_best_signal(symbols, interval, mode):
    rows = []
    for symbol in symbols:
        signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
        rows.append({
            "symbol": symbol,
            "signal": signal,
            "reason": reason,
            "engineVotes": votes,
            "router": router,
            "indicators": indicators,
            "engineStatus": engine_status,
            "score": signal_score({
                "signal": signal,
                "engineVotes": votes,
                "router": router,
            }),
        })
    executable = [row for row in rows if row["signal"] in ("Buy", "Sell")]
    if executable:
        return max(executable, key=lambda row: row["score"]), rows
    return (rows[0] if rows else None), rows


def candles_until(candles, end_time, limit):
    rows = [item for item in candles if item["time"] <= end_time]
    return rows[-limit:]


def estimate_trade_outcome(side, entry_price, future, stop_loss_pct, take_profit_pct):
    if not future:
        return "open", 0, entry_price
    if side == "Buy":
        stop = entry_price * (1 - stop_loss_pct / 100)
        target = entry_price * (1 + take_profit_pct / 100)
        for index, candle in enumerate(future, start=1):
            hit_stop = candle["low"] <= stop
            hit_target = candle["high"] >= target
            if hit_stop and hit_target:
                return "loss", index, stop
            if hit_target:
                return "win", index, target
            if hit_stop:
                return "loss", index, stop
    else:
        stop = entry_price * (1 + stop_loss_pct / 100)
        target = entry_price * (1 - take_profit_pct / 100)
        for index, candle in enumerate(future, start=1):
            hit_stop = candle["high"] >= stop
            hit_target = candle["low"] <= target
            if hit_stop and hit_target:
                return "loss", index, stop
            if hit_target:
                return "win", index, target
            if hit_stop:
                return "loss", index, stop
    last_price = future[-1]["close"]
    pnl_pct = ((last_price - entry_price) / entry_price) * 100 if side == "Buy" else ((entry_price - last_price) / entry_price) * 100
    return ("win" if pnl_pct > 0 else "loss" if pnl_pct < 0 else "flat"), len(future), last_price


def replay_strategy_quality(symbol, horizon="24h", mode="balanced", stop_loss_pct=0.8, take_profit_pct=1.6):
    horizon = str(horizon or "24h").lower()
    replay_interval = "15" if horizon == "7d" else "5"
    limit = 700 if horizon == "7d" else 320
    lookahead = 16 if replay_interval == "15" else 24
    step = 2 if replay_interval == "15" else 3

    tf5, msg5 = fetch_candles(symbol, replay_interval, limit=limit)
    tf15, msg15 = fetch_candles(symbol, "15", limit=700)
    tf1h, msg1h = fetch_candles(symbol, "60", limit=220)
    if not tf5 or not tf15 or not tf1h:
        return {"ok": False, "message": msg5 if not tf5 else msg15 if not tf15 else msg1h, "trades": []}

    votes_by_engine = {}
    signal_counts = {"Buy": 0, "Sell": 0, "WAIT": 0}
    trades = []
    cursor_block_until = 0
    start_index = 80
    for index in range(start_index, max(start_index, len(tf5) - lookahead), step):
        if index <= cursor_block_until:
            continue
        end_time = tf5[index]["time"]
        replay5 = tf5[:index + 1]
        replay15 = candles_until(tf15, end_time, 120)
        replay1h = candles_until(tf1h, end_time, 120)
        if len(replay5) < 60 or len(replay15) < 60 or len(replay1h) < 60:
            continue

        votes = [
            trend_following_engine(replay1h, replay15, replay5),
            sr_breakout_engine(replay1h, replay15, replay5),
            rsi_divergence_engine(replay1h, replay15, replay5),
            vwap_bounce_engine(replay1h, replay15, replay5),
            liquidity_sweep_engine(replay1h, replay15, replay5),
            orb_engine(replay1h, replay15, replay5),
        ]
        for item in votes:
            engine = item["engine"]
            votes_by_engine.setdefault(engine, {"Buy": 0, "Sell": 0, "WAIT": 0})
            votes_by_engine[engine][item["signal"]] = votes_by_engine[engine].get(item["signal"], 0) + 1

        router = route_votes(votes, mode)
        signal = router["decision"]
        signal_counts[signal] = signal_counts.get(signal, 0) + 1
        if signal not in ("Buy", "Sell"):
            continue

        entry = replay5[-1]["close"]
        outcome, bars_held, exit_price = estimate_trade_outcome(signal, entry, tf5[index + 1:index + 1 + lookahead], stop_loss_pct, take_profit_pct)
        pnl_pct = ((exit_price - entry) / entry) * 100 if signal == "Buy" else ((entry - exit_price) / entry) * 100
        trades.append({
            "time": end_time,
            "symbol": symbol,
            "side": signal,
            "entry": round(entry, 8),
            "exit": round(exit_price, 8),
            "outcome": outcome,
            "pnlPct": round(pnl_pct, 4),
            "barsHeld": bars_held,
            "router": router,
            "votes": votes,
        })
        cursor_block_until = index + max(1, bars_held)

    wins = len([item for item in trades if item["outcome"] == "win"])
    losses = len([item for item in trades if item["outcome"] == "loss"])
    flats = len(trades) - wins - losses
    total_pnl = sum(item["pnlPct"] for item in trades)
    return {
        "ok": True,
        "symbol": symbol,
        "horizon": horizon,
        "interval": replay_interval,
        "mode": normalize_mode(mode),
        "candles": len(tf5),
        "stopLossPct": stop_loss_pct,
        "takeProfitPct": take_profit_pct,
        "summary": {
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "flats": flats,
            "winRate": round((wins / len(trades)) * 100, 2) if trades else 0,
            "estimatedPnlPct": round(total_pnl, 4),
        },
        "signalCounts": signal_counts,
        "votesByEngine": votes_by_engine,
        "trades": trades[-100:],
    }


def tpsl_prices(symbol, side, stop_loss_pct, take_profit_pct):
    mark = get_mark_price(symbol)
    if not mark:
        return None, None
    stop_loss_pct = max(0, float(stop_loss_pct or 0))
    take_profit_pct = max(0, float(take_profit_pct or 0))
    if side == "Buy":
        stop_loss = mark * (1 - (stop_loss_pct / 100))
        take_profit = mark * (1 + (take_profit_pct / 100))
    else:
        stop_loss = mark * (1 + (stop_loss_pct / 100))
        take_profit = mark * (1 - (take_profit_pct / 100))
    return format_price(symbol, stop_loss), format_price(symbol, take_profit)


def generate_order_link_id(source):
    prefix = "".join(ch.lower() for ch in str(source or "auto") if ch.isalnum())[:8] or "auto"
    nonce = secrets.token_hex(3)
    return f"cdx-{prefix}-{int(time.time() * 1000)}-{nonce}"[:36]


def place_demo_order(symbol, side, qty, source, stop_loss_pct=None, take_profit_pct=None):
    mark = get_mark_price(symbol)
    if not mark:
        return {"retCode": -1001, "retMsg": "Order blocked locally: quantity/notional does not meet Bybit instrument limits."}

    rules = get_instrument_rules(symbol)
    if not rules.get("ok"):
        return {"retCode": -1001, "retMsg": "Order blocked locally: quantity/notional does not meet Bybit instrument limits."}

    try:
        qty_dec = Decimal(str(qty))
    except Exception:
        qty_dec = Decimal("0")

    # Round quantity down to the valid qtyStep
    rounded_qty = floor_to_step(qty_dec, rules["qtyStep"])
    notional = rounded_qty * Decimal(str(mark))

    # Reject locally before sending to Bybit if:
    # - qty < minOrderQty
    # - qty > maxOrderQty
    # - qty * markPrice < minNotionalValue
    max_qty = rules.get("maxOrderQty") or Decimal("0")
    rejected = False
    if rounded_qty < rules["minOrderQty"]:
        rejected = True
    elif max_qty > 0 and rounded_qty > max_qty:
        rejected = True
    elif rules["minNotionalValue"] > 0 and notional < rules["minNotionalValue"]:
        rejected = True

    if rejected:
        return {
            "retCode": -1001,
            "retMsg": "Order blocked locally: quantity/notional does not meet Bybit instrument limits.",
            "result": {}
        }

    qty_str = format_qty(rounded_qty)

    order = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty_str,
        "timeInForce": "IOC",
        "orderLinkId": generate_order_link_id(source),
    }

    if stop_loss_pct is not None and take_profit_pct is not None:
        stop_loss, take_profit = tpsl_prices(symbol, side, stop_loss_pct, take_profit_pct)
        if stop_loss and take_profit:
            order.update({
                "stopLoss": stop_loss,
                "takeProfit": take_profit,
                "tpslMode": "Full",
                "tpOrderType": "Market",
                "slOrderType": "Market",
            })

    return bybit_request("POST", "/v5/order/create", order)


def fetch_order_status(symbol, order_result):
    result = order_result.get("result") or {}
    order_id = result.get("orderId")
    order_link_id = result.get("orderLinkId")
    if not order_id and not order_link_id:
        return {"ok": False, "reason": "Order id unavailable"}

    params = {"category": "linear", "symbol": symbol}
    if order_id:
        params["orderId"] = order_id
    elif order_link_id:
        params["orderLinkId"] = order_link_id

    realtime = bybit_request("GET", "/v5/order/realtime", params)
    rows = (realtime.get("result") or {}).get("list") or []
    if realtime.get("retCode") == 0 and rows:
        row = rows[0]
        return {
            "ok": True,
            "source": "realtime",
            "orderId": row.get("orderId") or order_id,
            "orderLinkId": row.get("orderLinkId") or order_link_id,
            "orderStatus": row.get("orderStatus") or "Unknown",
            "cumExecQty": row.get("cumExecQty"),
            "avgPrice": row.get("avgPrice"),
        }

    history = bybit_request("GET", "/v5/order/history", params)
    rows = (history.get("result") or {}).get("list") or []
    if history.get("retCode") == 0 and rows:
        row = rows[0]
        return {
            "ok": True,
            "source": "history",
            "orderId": row.get("orderId") or order_id,
            "orderLinkId": row.get("orderLinkId") or order_link_id,
            "orderStatus": row.get("orderStatus") or "Unknown",
            "cumExecQty": row.get("cumExecQty"),
            "avgPrice": row.get("avgPrice"),
        }

    return {
        "ok": False,
        "reason": realtime.get("retMsg") or history.get("retMsg") or "Order status unavailable",
        "orderId": order_id,
        "orderLinkId": order_link_id,
    }


def close_symbol_positions(symbol):
    payload = bybit_request("GET", "/v5/position/list", {"category": "linear", "symbol": symbol})
    if payload.get("retCode") != 0:
        return {"ok": False, "error": payload.get("retMsg", "Position check failed"), "orders": []}

    orders = []
    positions = (payload.get("result") or {}).get("list") or []
    for position in positions:
        try:
            size = abs(float(position.get("size") or 0))
        except (TypeError, ValueError):
            size = 0
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
            "orderLinkId": generate_order_link_id("close"),
        }
        if position.get("positionIdx") is not None:
            close_order["positionIdx"] = int(position.get("positionIdx") or 0)
        orders.append(bybit_request("POST", "/v5/order/create", close_order))
    return {"ok": True, "orders": orders}


def normalize_block_reason(reason_str):
    reason_lower = str(reason_str).lower()
    if "cooldown" in reason_lower:
        return "cooldown active"
    if "max open positions" in reason_lower:
        return "max open positions reached"
    if "position already open" in reason_lower or "existing" in reason_lower or "duplicate" in reason_lower or "reverse trade" in reason_lower:
        return "position already open"
    if "daily loss cap" in reason_lower:
        return "daily loss cap reached"
    if "no executable signal" in reason_lower:
        return "no executable signal"
    if "not confirmed" in reason_lower:
        return "position not confirmed after order"
    if "open order" in reason_lower or "margin hold" in reason_lower:
        return "open order or margin hold detected"
    return reason_str


def bot_tick():
    with BOT_LOCK:
        check_and_reset_daily_state(BOT_STATE)
        state = dict(BOT_STATE)

    symbol = state["symbol"]
    interval = state["interval"]
    mode = normalize_mode(state.get("mode"))
    auto_pick = bool(state.get("autoPick"))
    universe = top_gainer_universe()
    scan_symbols = list(universe["symbols"])
    now = time.time()
    management = manage_open_positions(state)
    daily_risk = daily_risk_report(state)

    if auto_pick:
        best, scan_rows = select_best_signal(scan_symbols, interval, mode)
        if best:
            symbol = best["symbol"]
            signal = best["signal"]
            reason = best["reason"]
            votes = best["engineVotes"]
            router = best["router"]
            indicators = best["indicators"]
            engine_status = best["engineStatus"]
        else:
            signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
            scan_rows = []
    else:
        signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
        scan_rows = []

    active_state = dict(state)
    active_state["symbol"] = symbol
    active_state["signal"] = signal
    active_state["engineVotes"] = votes
    active_state["router"] = router
    if daily_risk.get("consecutiveLosses") is not None:
        active_state["consecutiveLosses"] = daily_risk.get("consecutiveLosses")
    risk_policy = signal_risk_policy(active_state, signal) if signal in ("Buy", "Sell") else {}
    if risk_policy:
        active_state["riskPolicy"] = risk_policy
        active_state["riskSizeFactor"] = risk_policy.get("sizeFactor", 1.0)
    sizing = calculate_position_sizing(symbol, active_state) if signal in ("Buy", "Sell") else {}
    if sizing.get("ok"):
        active_state["qty"] = sizing["qty"]

    reached, lock_reason = daily_loss_cap_reached(state)
    if reached:
        daily_risk["blocked"] = True
        daily_risk["reason"] = lock_reason

    if reached:
        exact_reason = "daily loss cap reached"
        print(f"Guard blocked: {exact_reason}", flush=True)
    elif signal not in ("Buy", "Sell"):
        exact_reason = "no executable signal"
        print(f"Guard blocked: {exact_reason}", flush=True)

    update = {
        "lastRunAt": int(now),
        "lastSignal": signal,
        "lastReason": lock_reason if reached else reason,
        "engineVotes": votes,
        "router": router,
        "indicators": indicators,
        "engineStatus": engine_status,
        "selectedSignalSymbol": symbol,
        "scannerRows": scan_rows,
        "scanSymbols": scan_symbols,
        "symbolSource": universe["source"],
        "universe": universe,
        "mode": mode,
        "positionSizing": sizing,
        "riskPolicy": risk_policy,
        "tradeManagement": management,
        "dailyRisk": daily_risk,
        "executionGuard": {"ok": not reached, "reason": "daily loss cap reached" if reached else "no executable signal"},
        "orderLifecycle": order_lifecycle(
            signal=signal,
            guard="blocked" if reached else "idle",
            order="skipped" if reached else "idle",
            protection="skipped" if reached else "idle",
            status="blocked" if reached else "idle",
            reason="daily loss cap reached" if reached else "no executable signal"
        ),
    }

    if signal in ("Buy", "Sell"):
        if daily_risk.get("blocked"):
            raw_reason = daily_risk.get("reason", "Daily risk blocked")
            exact_reason = normalize_block_reason(raw_reason)
            print(f"Guard blocked: {exact_reason}", flush=True)
            update["lastReason"] = reason + f"; daily risk blocked: {raw_reason}"
            update["executionGuard"] = {"ok": False, "reason": exact_reason}
            update["orderLifecycle"] = order_lifecycle(
                signal=signal,
                guard="blocked",
                order="skipped",
                protection="skipped",
                status="blocked",
                reason=exact_reason
            )
        elif not sizing.get("ok"):
            raw_reason = sizing.get("reason", "Unknown sizing error")
            exact_reason = normalize_block_reason(raw_reason)
            print(f"Guard blocked: {exact_reason}", flush=True)
            update["lastReason"] = reason + f"; sizing blocked: {raw_reason}"
            update["executionGuard"] = {"ok": False, "reason": exact_reason}
            update["orderLifecycle"] = order_lifecycle(
                signal=signal,
                guard="blocked",
                order="skipped",
                protection="skipped",
                status="blocked",
                reason=exact_reason
            )
        else:
            engine = get_bot_engine()
            guard = existing_position_guard(symbol, signal, active_state)
            update["executionGuard"] = guard
            if not guard.get("ok"):
                engine.set_status("risk", "blocked")
                update["engineStatus"] = dict(engine.status)
                raw_reason = guard.get("reason", "Position guard blocked")
                exact_reason = normalize_block_reason(raw_reason)
                print(f"Guard blocked: {exact_reason}", flush=True)
                update["lastReason"] = reason + f"; execution guard blocked: {raw_reason}"
                update["executionGuard"] = {"ok": False, "reason": exact_reason}
                update["orderLifecycle"] = order_lifecycle(
                    signal=signal,
                    guard="blocked",
                    order="skipped",
                    protection="skipped",
                    status="blocked",
                    reason=exact_reason
                )
            else:
                approved, risk_reason = engine.risk_check(active_state, signal)
                update["engineStatus"] = dict(engine.status)
                update["riskDecision"] = active_state.get("riskDecision", active_state.get("riskPolicy", {}))
                if not approved:
                    exact_reason = normalize_block_reason(risk_reason)
                    print(f"Guard blocked: {exact_reason}", flush=True)
                    update["lastReason"] = reason + f"; {risk_reason}"
                    update["executionGuard"] = {**guard, "ok": False, "reason": exact_reason}
                    update["orderLifecycle"] = order_lifecycle(
                        signal=signal,
                        guard="blocked",
                        order="skipped",
                        protection="skipped",
                        status="blocked",
                        reason=exact_reason
                    )
                else:
                    result = engine.execute(active_state, signal)
                    update["engineStatus"] = dict(engine.status)
                    update["lastOrder"] = result
                    update["qty"] = active_state["qty"]
                    protection_status = "attached" if (
                        result.get("retCode") == 0
                        and active_state.get("stopLossPct") is not None
                        and active_state.get("takeProfitPct") is not None
                    ) else "skipped"
                    if result.get("retCode") == 0:
                        order_status = fetch_order_status(symbol, result)
                        result["lifecycleStatus"] = order_status
                        update["lastTradeAt"] = int(now)
                        update["lastReason"] = reason + f"; demo order accepted with qty {active_state['qty']}"
                        lifecycle_status = order_status.get("orderStatus") or "open-check-pending"
                        update["orderLifecycle"] = order_lifecycle(signal=signal, guard="passed", order=lifecycle_status, protection=protection_status, status="accepted", reason=update["lastReason"])
                    else:
                        update["lastReason"] = reason + f"; order rejected: {result.get('retMsg', 'Unknown error')}"
                        update["orderLifecycle"] = order_lifecycle(signal=signal, guard="passed", order="rejected", protection="skipped", status="rejected", reason=result.get("retMsg", "Unknown error"))

    with BOT_LOCK:
        BOT_STATE.update(update)
        return dict(BOT_STATE)


def bot_loop():
    while True:
        with BOT_LOCK:
            enabled = BOT_STATE["enabled"]
        if not enabled:
            break
        bot_tick()
        time.sleep(BOT_SCAN_SECONDS)


def ensure_bot_thread():
    global BOT_THREAD
    if BOT_THREAD and BOT_THREAD.is_alive():
        return
    BOT_THREAD = threading.Thread(target=bot_loop, daemon=True)
    BOT_THREAD.start()


class Handler(BaseHTTPRequestHandler):
    def is_authorized(self):
        expected = f"Bearer {ADMIN_TOKEN}"
        supplied = self.headers.get("Authorization", "")
        return bool(ADMIN_TOKEN) and secrets.compare_digest(supplied, expected)

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = dict(urllib.parse.parse_qsl(parsed.query))

        if parsed.path in ("/", "/index.html", "/app"):
            if FRONTEND_INDEX.exists():
                html = FRONTEND_INDEX.read_text(encoding="utf-8")
                html = html.replace('const apiParam = new URLSearchParams(window.location.search).get("api");', 'const apiParam = new URLSearchParams(window.location.search).get("api") || window.location.origin;')
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
            else:
                json_response(self, 404, {"ok": False, "error": "Frontend index not found"})
            return

        if parsed.path == "/api/health":
            cfg = config()
            json_response(self, 200, {
                "ok": True,
                "exchange": "bybit",
                "baseUrl": cfg["base_url"],
                "hasApiKey": bool(cfg["api_key"]),
                "hasApiSecret": bool(cfg["api_secret"]),
            })
            return

        if parsed.path == "/api/bybit/ticker":
            symbol = query.get("symbol", "BTCUSDT").upper()
            payload = public_bybit_get("/v5/market/tickers", {"category": "linear", "symbol": symbol})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bybit/kline":
            symbol = query.get("symbol", "BTCUSDT").upper()
            interval = query.get("interval", "5")
            candles, message = fetch_candles(symbol, interval)
            json_response(self, 200, {
                "ok": bool(candles),
                "symbol": symbol,
                "interval": interval,
                "candles": candles or [],
                "message": message,
            })
            return

        if parsed.path == "/api/bybit/wallet":
            payload = bybit_request("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bot/sizing":
            symbol = query.get("symbol", "BTCUSDT").upper()
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                state = dict(BOT_STATE)
            sizing = calculate_position_sizing(symbol, state)
            json_response(self, 200, {
                "ok": bool(sizing.get("ok")),
                "symbol": symbol,
                "sizing": sizing,
            })
            return

        if parsed.path == "/api/bybit/positions":
            symbol = query.get("symbol")
            params = {"category": "linear"}
            if symbol:
                params["symbol"] = symbol.upper()
            else:
                params["settleCoin"] = "USDT"
            payload = bybit_request("GET", "/v5/position/list", params)
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bybit/open-orders":
            symbol = query.get("symbol", "BTCUSDT").upper()
            payload = bybit_request("GET", "/v5/order/realtime", {"category": "linear", "symbol": symbol})
            json_response(self, 200, payload)
            return

        if parsed.path == "/api/bot/debug-risk":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                info = get_debug_risk_info(BOT_STATE)
            json_response(self, 200, info)
            return

        if parsed.path == "/api/bot/status":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                payload = dict(BOT_STATE)
                payload["engineOverview"] = get_bot_engine().overview()
                payload["universe"] = top_gainer_universe()
                payload["dailyRisk"] = daily_risk_report(payload)
                payload["debugRisk"] = get_debug_risk_info(payload)
                payload["scanSeconds"] = BOT_SCAN_SECONDS
                payload["topGainerRefreshSeconds"] = TOP_GAINER_REFRESH_SECONDS
            json_response(self, 200, {"ok": True, "bot": payload})
            return

        if parsed.path == "/api/bot/universe":
            force = query.get("force", "0") in ("1", "true", "yes")
            json_response(self, 200, {"ok": True, "universe": top_gainer_universe(force=force)})
            return

        if parsed.path == "/api/bot/engine":
            json_response(self, 200, {"ok": True, "engine": get_bot_engine().overview()})
            return

        if parsed.path == "/api/bot/journal":
            limit = max(1, min(500, int(query.get("limit", "100"))))
            engine = get_bot_engine()
            json_response(self, 200, {
                "ok": True,
                "journal": engine.journal.recent(limit),
                "journalPath": str(engine.journal.path),
            })
            return

        if parsed.path == "/api/bot/scanner":
            universe = top_gainer_universe(force=query.get("forceUniverse", "0") in ("1", "true", "yes"))
            symbols = query.get("symbols", ",".join(universe["symbols"]))
            interval = query.get("interval", "15")
            mode = normalize_mode(query.get("mode", "balanced"))
            market_rows = {row.get("symbol"): row for row in universe.get("rows", [])}
            rows = []
            for symbol in [item.strip().upper() for item in symbols.split(",") if item.strip()]:
                signal, reason, votes, router, indicators, engine_status = evaluate_signal(symbol, interval, mode)
                market = market_rows.get(symbol, {})
                rows.append({
                    "symbol": symbol,
                    "signal": signal,
                    "reason": reason,
                    "changePct": market.get("changePct"),
                    "turnover24h": market.get("turnover24h"),
                    "spreadPct": market.get("spreadPct"),
                    "engineVotes": votes,
                    "router": router,
                    "indicators": indicators,
                    "engineStatus": engine_status,
                    "score": signal_score({"signal": signal, "engineVotes": votes, "router": router}),
                })
            rows.sort(key=lambda row: row["score"], reverse=True)
            json_response(self, 200, {
                "ok": True,
                "interval": interval,
                "mode": mode,
                "rows": rows,
                "universe": universe,
                "scanSeconds": BOT_SCAN_SECONDS,
                "topGainerRefreshSeconds": TOP_GAINER_REFRESH_SECONDS,
            })
            return

        if parsed.path == "/api/bot/replay":
            symbol = query.get("symbol", "BTCUSDT").upper()
            horizon = query.get("horizon", "24h")
            mode = normalize_mode(query.get("mode", BOT_STATE.get("mode", "balanced")))
            stop_loss_pct = numeric(query.get("stopLossPct"), BOT_STATE.get("stopLossPct", 0.8))
            take_profit_pct = numeric(query.get("takeProfitPct"), BOT_STATE.get("takeProfitPct", 1.6))
            payload = replay_strategy_quality(symbol, horizon, mode, stop_loss_pct, take_profit_pct)
            json_response(self, 200, payload)
            return

        json_response(self, 404, {"ok": False, "error": "Route not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)

        if not self.is_authorized():
            json_response(self, 401, {"ok": False, "error": "Unauthorized"})
            return

        try:
            payload = read_json(self)
        except Exception as exc:
            json_response(self, 400, {"ok": False, "error": f"Invalid JSON: {exc}"})
            return

        if parsed.path == "/api/bybit/demo-order":
            if payload.get("confirmDemoOrder") is not True:
                json_response(self, 400, {"ok": False, "error": "confirmDemoOrder must be true"})
                return

            symbol = str(payload.get("symbol", "BTCUSDT")).upper()
            side = "Sell" if payload.get("side") == "Sell" else "Buy"
            stop_loss_pct = float(payload.get("stopLossPct", 0.8))
            take_profit_pct = float(payload.get("takeProfitPct", 1.6))

            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                state = dict(BOT_STATE)

            # Check daily loss cap
            reached, lock_reason = daily_loss_cap_reached(state)
            if reached:
                json_response(self, 200, {
                    "retCode": -1,
                    "retMsg": "Daily loss cap reached. Trading locked for today.",
                    "ok": False
                })
                return

            chosen_symbol = None
            sizing = None

            # 1. Try selected symbol
            p_sizing = calculate_position_sizing(symbol, state)
            if p_sizing.get("ok"):
                chosen_symbol = symbol
                sizing = p_sizing
            else:
                # 2. Try BTCUSDT, ETHUSDT, SOLUSDT in order
                for fb in ["BTCUSDT", "ETHUSDT", "SOLUSDT"]:
                    if fb != symbol:
                        fb_sizing = calculate_position_sizing(fb, state)
                        if fb_sizing.get("ok"):
                            chosen_symbol = fb
                            sizing = fb_sizing
                            break

            if not chosen_symbol or not sizing:
                result = {
                    "retCode": -1001,
                    "retMsg": "Order blocked locally: quantity/notional does not meet Bybit instrument limits.",
                    "result": {}
                }
                get_bot_engine().journal.add("manual_order", {"symbol": symbol, "side": side, "result": result})
                get_bot_engine().set_status("journal", "ok")
                json_response(self, 200, result)
                return

            qty = sizing["qty"]
            result = place_demo_order(chosen_symbol, side, qty, "manual", stop_loss_pct, take_profit_pct)
            get_bot_engine().journal.add("manual_order", {"symbol": chosen_symbol, "side": side, "result": result})
            get_bot_engine().set_status("journal", "ok")
            json_response(self, 200, result)
            return

        if parsed.path == "/api/bot/start":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                state = dict(BOT_STATE)
            check_state = {**state, "dailyLossCapUsdt": payload.get("dailyLossCapUsdt", state.get("dailyLossCapUsdt", 25.0))}
            reached, lock_reason = daily_loss_cap_reached(check_state)
            if reached:
                json_response(self, 200, {
                    "ok": False,
                    "enabled": False,
                    "reason": "Daily loss cap reached. Trading locked for today."
                })
                return

            universe = top_gainer_universe(force=True)
            daily_loss_cap = max(0.0, float(payload.get("dailyLossCapUsdt", 25.0)))
            symbol = str(payload.get("symbol") or universe["symbols"][0] or "BTCUSDT").upper()
            interval = str(payload.get("interval", "5"))
            qty = str(payload.get("qty", "0.001"))
            stop_loss_pct = float(payload.get("stopLossPct", 0.8))
            take_profit_pct = float(payload.get("takeProfitPct", 1.6))
            max_allocation = max(1, float(payload.get("maxAllocationUsdt", 250)))
            risk_per_trade = max(0.01, float(payload.get("riskPerTradePct", 0.5)))
            max_open_positions = max(1, int(payload.get("maxOpenPositions", 1)))
            max_trades_per_day = max(1, int(payload.get("maxTradesPerDay", 5)))
            breakeven_enabled = payload.get("breakevenEnabled", True) is not False
            breakeven_trigger = max(0.1, float(payload.get("breakevenTriggerPct", 0.6)))
            partial_tp_enabled = payload.get("partialTpEnabled", True) is not False
            partial_tp_trigger = max(0.1, float(payload.get("partialTpTriggerPct", 1.0)))
            partial_tp_close = max(1, min(100, float(payload.get("partialTpClosePct", 50))))
            trailing_stop_enabled = payload.get("trailingStopEnabled", True) is not False
            trailing_stop_trigger = max(0.1, float(payload.get("trailingStopTriggerPct", 0.8)))
            trailing_stop_distance = max(0.05, float(payload.get("trailingStopDistancePct", 0.35)))
            cooldown = max(60, int(payload.get("cooldownSeconds", 300)))
            mode = normalize_mode(payload.get("mode", "balanced"))
            auto_pick = True
            scan_symbols = list(universe["symbols"])
            with BOT_LOCK:
                BOT_STATE.update({
                    "enabled": True,
                    "symbol": symbol,
                    "interval": interval,
                    "qty": qty,
                    "maxAllocationUsdt": max_allocation,
                    "riskPerTradePct": risk_per_trade,
                    "maxOpenPositions": max_open_positions,
                    "dailyLossCapUsdt": daily_loss_cap,
                    "maxTradesPerDay": max_trades_per_day,
                    "breakevenEnabled": breakeven_enabled,
                    "breakevenTriggerPct": breakeven_trigger,
                    "partialTpEnabled": partial_tp_enabled,
                    "partialTpTriggerPct": partial_tp_trigger,
                    "partialTpClosePct": partial_tp_close,
                    "trailingStopEnabled": trailing_stop_enabled,
                    "trailingStopTriggerPct": trailing_stop_trigger,
                    "trailingStopDistancePct": trailing_stop_distance,
                    "stopLossPct": stop_loss_pct,
                    "takeProfitPct": take_profit_pct,
                    "cooldownSeconds": cooldown,
                    "mode": mode,
                    "autoPick": auto_pick,
                    "scanSymbols": scan_symbols,
                    "symbolSource": universe["source"],
                    "selectedSignalSymbol": symbol,
                    "universe": universe,
                    "lastReason": f"Auto trader started in {mode} mode with top-gainer scan.",
                })
            ensure_bot_thread()
            status = bot_tick()
            json_response(self, 200, {"ok": True, "bot": status})
            return

        if parsed.path == "/api/bot/stop":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                BOT_STATE.update({
                    "enabled": False,
                    "lastReason": "Auto trader stopped by user.",
                })
                status = dict(BOT_STATE)
            json_response(self, 200, {"ok": True, "bot": status})
            return

        if parsed.path == "/api/bot/manage-positions":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                state = dict(BOT_STATE)
            result = manage_open_positions(state)
            with BOT_LOCK:
                BOT_STATE.update({"tradeManagement": result})
                status = dict(BOT_STATE)
            json_response(self, 200, {"ok": result.get("ok", False), "result": result, "bot": status})
            return

        if parsed.path == "/api/bybit/kill-switch":
            with BOT_LOCK:
                check_and_reset_daily_state(BOT_STATE)
                BOT_STATE.update({
                    "enabled": False,
                    "lastReason": "Auto trader stopped by kill switch.",
                })

            positions, msg = get_open_positions()
            if positions is None:
                json_response(self, 200, {
                    "retCode": -1,
                    "retMsg": f"Failed to fetch positions: {msg}",
                    "closedSymbols": [],
                    "closeAttempts": 0,
                    "openPositionsBefore": 0,
                })
                return

            if not positions:
                print("No open positions to close", flush=True)
                json_response(self, 200, {
                    "retCode": -1,
                    "retMsg": "No open positions to close.",
                    "closedSymbols": [],
                    "closeAttempts": 0,
                    "openPositionsBefore": 0,
                })
                return

            open_positions_before = len(positions)
            closed_symbols = sorted(list(set(pos.get("symbol") for pos in positions if pos.get("symbol"))))

            cancel_results = []
            close_results = []
            orders_count = 0

            for position in positions:
                p_symbol = position.get("symbol")
                if not p_symbol:
                    continue

                print(f"Kill switch closing {p_symbol}", flush=True)

                cancel_res = bybit_request("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": p_symbol})
                cancel_results.append(cancel_res)

                close_res = close_symbol_positions(p_symbol)
                close_results.append(close_res)
                if close_res.get("ok"):
                    orders_count += len(close_res.get("orders") or [])

                print(f"Closed {p_symbol} position", flush=True)

                get_bot_engine().journal.add("kill_switch", {
                    "symbol": p_symbol,
                    "cancelResult": cancel_res,
                    "closeResult": close_res,
                })

            get_bot_engine().set_status("journal", "ok")

            cancel_result = cancel_results[-1] if cancel_results else {"retCode": 0}
            close_result = {"ok": True, "orders": [1] * orders_count}

            json_response(self, 200, {
                "retCode": 0 if cancel_result.get("retCode") == 0 and close_result.get("ok") else cancel_result.get("retCode", -1),
                "retMsg": "Kill switch sent: open orders cancelled and positions close attempted",
                "cancelResult": cancel_result,
                "closeResult": close_result,
                "closedSymbols": closed_symbols,
                "closeAttempts": orders_count,
                "openPositionsBefore": open_positions_before,
            })
            return

        json_response(self, 404, {"ok": False, "error": "Route not found"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"Bybit demo backend running on http://{host}:{port}", flush=True)
    print(f"Reading environment from {ENV_PATH}", flush=True)
    server.serve_forever()
