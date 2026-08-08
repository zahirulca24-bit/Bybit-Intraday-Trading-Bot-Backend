from __future__ import annotations

import inspect

from backend import execution_command_outbox as outbox
from backend import fifteen_minute_strategy_classifier as classifier
from backend.engines.bot_engine import BotEngineV2


def risk_candidate(**updates):
    row = {
        "candidateKey": "BTCUSDT:15m:5m:Buy:Trend Follow",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "strategy": "Trend Follow",
        "grade": "A+",
        "gradeScore": 99.0,
        "entryReference": 100.0,
        "entryFiveMinuteCandleTime": 1_000_000,
        "setupFifteenMinuteCandleTime": 900_000,
        "createdAt": 4000,
        "riskApproved": True,
        "riskStatus": "APPROVED_RISK",
        "riskSizeFactor": 1.0,
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "AWAITING_POSITION_SIZING",
        "orderSubmitted": False,
    }
    row.update(updates)
    return row


class DirectHandoffCore:
    def __init__(self, sizing_rows=None, store=None):
        self._risk = {
            "status": "ready",
            "inputFingerprint": "risk:fingerprint",
            "approvedRiskQueue": [risk_candidate()],
        }
        self._sizing = {
            "status": "degraded",
            "inputFingerprint": "sizing:fingerprint",
            "rows": list(sizing_rows or []),
            "approvedSizingQueue": [],
        }
        self._durable_state_store = store
        self.order_calls = 0

    def authoritative_entry_risk_status(self):
        return dict(self._risk)

    def position_sizing_margin_status(self):
        return dict(self._sizing)

    def place_demo_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("Canonical Python runtime must never submit a Bybit order")


def test_risk_approved_candidate_delivers_directly_when_python_sizing_waits(monkeypatch):
    outbox._reset_for_tests()
    monkeypatch.setenv("NODE_EXECUTION_URL", "https://node.internal")
    monkeypatch.setenv("NODE_HANDOFF_TOKEN", "test-token")
    delivered = []

    def fake_post(payload, url, token, timeout):
        delivered.append((dict(payload), url, token, timeout))
        return {
            "ok": True,
            "statusCode": 202,
            "body": {"code": "NODE_HANDOFF_ACCEPTED", "state": "AVAILABLE"},
        }

    monkeypatch.setattr(outbox, "_post_candidate", fake_post)
    core = DirectHandoffCore(
        sizing_rows=[
            {
                **risk_candidate(),
                "positionSizingStatus": "SIZING_WAIT",
                "sizingApproved": False,
                "sizingDecision": {"code": "WALLET_DATA_WAIT", "tradeRejected": False},
            }
        ],
        store=None,
    )
    before = dict(core._risk["approvedRiskQueue"][0])

    result = outbox.build(core, now=5000)

    assert result["nodeHandoff"]["delivered"] == 1
    assert result["nodeHandoff"]["retrying"] == 0
    assert result["postgresSupport"]["status"] in {"WAIT", "WAIT_RETRY"}
    assert result["metrics"]["riskApprovedInput"] == 1
    assert result["metrics"]["sizingDiagnosticApprovedInput"] == 0
    assert result["metrics"]["orderSubmissions"] == 0
    assert core.order_calls == 0
    assert core._risk["approvedRiskQueue"][0] == before
    payload = delivered[0][0]
    assert payload["riskApproved"] is True
    assert payload["riskStatus"] == "APPROVED_RISK"
    assert payload["riskPerTradePct"] == 1.0
    assert payload["executionStatus"] == "AWAITING_NODE_EXECUTION"
    assert "qty" not in payload
    assert "requiredInitialMarginUsdt" not in payload


def test_postgresql_unavailable_is_support_degraded_not_direct_delivery_block(monkeypatch):
    outbox._reset_for_tests()
    monkeypatch.setenv("NODE_EXECUTION_URL", "https://node.internal")
    monkeypatch.setenv("NODE_HANDOFF_TOKEN", "test-token")
    monkeypatch.setattr(
        outbox,
        "_post_candidate",
        lambda payload, url, token, timeout: {
            "ok": True,
            "statusCode": 202,
            "body": {"code": "NODE_HANDOFF_ACCEPTED", "state": "AVAILABLE"},
        },
    )
    core = DirectHandoffCore(store=None)

    result = outbox.build(core, now=5000)

    assert result["nodeHandoff"]["delivered"] == 1
    assert result["postgresSupport"]["tradeRejectionAuthority"] is False
    assert result["metrics"]["canonicalTransport"] == "AUTHENTICATED_DIRECT_HTTP"
    assert result["metrics"]["postgresRole"] == "SUPPORT_RECONCILIATION_ONLY"


def test_direct_handoff_failure_retries_without_reclassifying_risk(monkeypatch):
    outbox._reset_for_tests()
    monkeypatch.setenv("NODE_EXECUTION_URL", "https://node.internal")
    monkeypatch.setenv("NODE_HANDOFF_TOKEN", "test-token")
    monkeypatch.setattr(
        outbox,
        "_post_candidate",
        lambda payload, url, token, timeout: {
            "ok": False,
            "statusCode": 503,
            "reason": "temporary unavailable",
            "body": {},
        },
    )
    core = DirectHandoffCore(store=None)

    result = outbox.build(core, now=5000)

    assert result["nodeHandoff"]["retrying"] == 1
    assert result["nodeHandoff"]["rows"][0]["state"] == "NODE_HANDOFF_RETRY"
    assert result["nodeHandoff"]["rows"][0]["tradeRejected"] is False
    assert core._risk["approvedRiskQueue"][0]["riskApproved"] is True
    assert core._risk["approvedRiskQueue"][0]["riskStatus"] == "APPROVED_RISK"


def test_closed_15m_classifier_processes_all_50_current_watchlist_rows(monkeypatch):
    classifier._reset_for_tests()
    timestamp = 10_000
    one_hour_time = 9_000_000
    rows = [
        {
            "symbol": f"COIN{index:02d}USDT",
            "trend": "BULLISH",
            "oneHourTrend": "BULLISH",
            "oneHourCandleTime": one_hour_time,
        }
        for index in range(50)
    ]

    class Core:
        def hourly_watchlist(self, force=False):
            return {
                "status": "ready",
                "oneHourCandleTime": one_hour_time,
                "symbols": [row["symbol"] for row in rows],
                "rows": rows,
            }

    def fake_classify(core, row, target_open_ms, now_ms):
        return {
            "symbol": row["symbol"],
            "status": "WATCHING",
            "atr15mPct": 1.0,
            "volumeRatio": 1.0,
        }, None

    monkeypatch.setattr(classifier, "_classify_symbol", fake_classify)
    result = classifier.build(Core(), now=timestamp)

    assert result["metrics"]["hourlyWatchlistInput"] == 50
    assert result["metrics"]["processed"] == 50
    assert len(result["rows"]) == 50
    assert result["metrics"]["maximumWatchlistRows"] == 50


def test_six_strategy_engines_remain_registered_in_bot_engine():
    source = inspect.getsource(BotEngineV2.strategies)
    expected = [
        "trend_following_engine",
        "sr_breakout_engine",
        "rsi_divergence_engine",
        "vwap_bounce_engine",
        "liquidity_sweep_engine",
        "orb_engine",
    ]
    assert all(name in source for name in expected)
    assert source.count("_engine(") == 6
