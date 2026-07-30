import time

from .hardened_strategies import (
    liquidity_sweep_engine,
    orb_engine,
    rsi_divergence_engine,
    sr_breakout_engine,
    trend_following_engine,
    vwap_bounce_engine,
)
from .indicators import avg_volume, ema, rsi, trend_direction
from .journal import JournalEngine
from .market_data import MarketDataEngine
from .order_fill import finalize_entry_order
from .risk import RiskEngine
from .router import route_votes
from .trade_management import TradeManagementEngine


class BotEngineV2:
    def __init__(self, base_url, bybit_request_fn, position_size_fn, open_positions_count_fn=None):
        self.version = "2.0.0"
        self.started_at = time.time()
        self.market_data = MarketDataEngine(base_url)
        self.risk = RiskEngine(position_size_fn, open_positions_count_fn)
        self.trade_management = TradeManagementEngine(bybit_request_fn, self.market_data)
        self.journal = JournalEngine()
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

    def evaluate(self, symbol, mode="balanced"):
        self.set_status("marketData", "running")
        snapshot = self.market_data.snapshot(symbol)
        self.set_status("marketData", "ok" if snapshot["ok"] else "error")
        if not snapshot["ok"]:
            router = route_votes([], mode)
            return "WAIT", snapshot["message"], [], router, {}, dict(self.status)
        indicators = self.indicators(snapshot)
        votes = self.strategies(snapshot)
        self.set_status("router", "running")
        router = route_votes(votes, mode)
        self.set_status("router", "ok")
        return router["decision"], router["reason"], votes, router, indicators, dict(self.status)

    def risk_check(self, state, signal):
        self.set_status("risk", "running")
        approved, reason = self.risk.check(state, signal)
        self.set_status("risk", "ok" if approved else "blocked")
        return approved, reason

    def execute(self, state, signal):
        self.set_status("tradeManagement", "running")
        create_result = self.trade_management.place_order(
            state["symbol"],
            signal,
            state["qty"],
            "auto",
            state["stopLossPct"],
            state["takeProfitPct"],
        )
        result = finalize_entry_order(
            state["symbol"],
            create_result,
            self.trade_management.bybit_request,
        )
        self.set_status("tradeManagement", "ok" if result.get("finalFilled") else "error")
        self.journal.add("auto_order", {"symbol": state["symbol"], "signal": signal, "result": result})
        self.set_status("journal", "ok")
        return result

    def overview(self):
        return {
            "version": self.version,
            "runtime": "modular",
            "uptimeSeconds": int(time.time() - self.started_at),
            "status": dict(self.status),
            "journal": self.journal.recent(),
            "journalPath": str(self.journal.path),
        }
