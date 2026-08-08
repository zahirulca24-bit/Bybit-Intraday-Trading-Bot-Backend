import threading

import pytest

from backend import authoritative_entry_risk as risk


class MemoryStore:
    def __init__(self):
        self.values = {}

    def status(self):
        return {"ok": True, "degraded": False}

    def get(self, key, default=None):
        value = self.values.get(key, default)
        if isinstance(value, dict):
            return dict(value)
        return value

    def put(self, key, value):
        self.values[key] = dict(value)
        return True


class CoreStub:
    def __init__(self):
        self.BOT_LOCK = threading.Lock()
        self.BOT_STATE = {
            "lastTradeAt": 0,
            "cooldownSeconds": 180,
            "maxOpenPositions": 3,
            "consecutiveLosses": 0,
        }
        self._durable_state_store = MemoryStore()
        self.entry_snapshot = entry_snapshot()
        self.daily = {
            "ok": True,
            "blocked": False,
            "newEntriesAllowed": True,
            "reason": "Daily risk OK",
            "policyId": "DAILY_NET_LOSS_V1",
        }
        self.position = {
            "ok": True,
            "reason": "No existing position conflict",
            "openPositions": 1,
            "maxOpenPositions": 3,
        }
        self.daily_calls = 0
        self.position_calls = 0
        self.sizing_calls = 0
        self.order_calls = 0

    def five_minute_entry_confirmation_status(self):
        return dict(self.entry_snapshot)

    def daily_risk_report(self, state):
        self.daily_calls += 1
        return dict(self.daily)

    def existing_position_guard(self, symbol, side, state):
        self.position_calls += 1
        return dict(self.position)

    def calculate_position_sizing(self, symbol, state):
        self.sizing_calls += 1
        raise AssertionError("Step 7 must not calculate position size")

    def place_demo_order(self, *args, **kwargs):
        self.order_calls += 1
        raise AssertionError("Step 7 must not submit an order")


def candidate(**updates):
    row = {
        "candidateKey": "BTCUSDT:2700000:3600000:Buy:Trend Follow",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "strategy": "Trend Follow",
        "setupFifteenMinuteCandleTime": 2_700_000,
        "entryFiveMinuteCandleTime": 3_600_000,
        "entryReference": 100.0,
        "strategyStrength": 4.8,
        "grade": "A+",
        "gradeScore": 96.0,
        "createdAt": 3900,
        "riskStatus": "PENDING_RISK",
        "positionSizingStatus": "NOT_EVALUATED_STEP8",
        "executionStatus": "AWAITING_NODE_EXECUTION",
        "orderSubmitted": False,
    }
    row.update(updates)
    return row


def entry_snapshot(*rows):
    queue = list(rows) if rows else [candidate()]
    return {
        "status": "ready",
        "fiveMinuteCandleTime": 3_600_000,
        "confirmedEntryQueue": queue,
        "confirmedEntryQueueSize": len(queue),
    }


@pytest.fixture(autouse=True)
def reset_risk(monkeypatch):
    risk._reset_for_tests()
    monkeypatch.setattr(
        risk.agreement_execution_guard,
        "is_restricted_symbol",
        lambda symbol: False,
    )
    yield
    risk._reset_for_tests()


def _approved(core, now=4000):
    risk.install(core)
    result = risk.build(core, now=now)
    assert result["approvedRiskQueueSize"] == 1
    return result, result["approvedRiskQueue"][0]


def test_valid_confirmed_a_plus_candidate_is_approved():
    core = CoreStub()

    result, approved = _approved(core)

    assert result["status"] == "ready"
    assert approved["grade"] == "A+"
    assert approved["riskStatus"] == "APPROVED_RISK"
    assert approved["riskApproved"] is True
    assert approved["riskPolicyId"] == risk.POLICY_ID
    assert approved["riskDecision"]["code"] == "RISK_APPROVED"


def test_valid_confirmed_a_candidate_is_approved():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(grade="A", gradeScore=90.0))

    _, approved = _approved(core)

    assert approved["grade"] == "A"
    assert approved["riskApproved"] is True


def test_approved_candidate_always_has_full_risk_size_factor():
    core = CoreStub()

    _, approved = _approved(core)

    assert approved["riskSizeFactor"] == 1.0
    assert approved["riskFlags"] == []
    assert approved["riskDecision"]["checks"]["signalRisk"]["entrySafetyOnly"] is True


def test_one_matching_strategy_vote_does_not_reject_entry_safety():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(
        candidate(
            strategyStrength=1.0,
            engineVotes=[{"engine": "Trend Follow", "signal": "Buy", "strength": 1.0}],
        )
    )

    _, approved = _approved(core)

    assert approved["riskSizeFactor"] == 1.0
    assert approved["riskFlags"] == []


def test_low_historical_strategy_strength_does_not_block_confirmed_candidate():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(strategyStrength=0.1))

    _, approved = _approved(core)

    assert approved["riskApproved"] is True
    assert approved["riskSizeFactor"] == 1.0


def test_one_engine_vote_in_old_half_size_band_stays_full_size():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(
        candidate(
            strategyStrength=2.5,
            engineVotes=[{"engine": "Trend Follow", "signal": "Buy", "strength": 2.5}],
        )
    )

    _, approved = _approved(core)

    assert approved["riskSizeFactor"] == 1.0


def test_one_engine_vote_in_old_three_quarter_band_stays_full_size():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(
        candidate(
            strategyStrength=3.5,
            engineVotes=[{"engine": "Trend Follow", "signal": "Buy", "strength": 3.5}],
        )
    )

    _, approved = _approved(core)

    assert approved["riskSizeFactor"] == 1.0


def test_multiple_matching_votes_do_not_change_risk_size_factor():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(
        candidate(
            strategyStrength=1.5,
            engineVotes=[
                {"engine": "Trend Follow", "signal": "Buy", "strength": 1.5},
                {"engine": "Breakout", "signal": "Buy", "strength": 1.4},
            ],
        )
    )

    _, approved = _approved(core)

    assert approved["riskSizeFactor"] == 1.0
    assert approved["riskFlags"] == []


def test_losing_streak_does_not_block_or_reduce_confirmed_candidate():
    core = CoreStub()
    core.BOT_STATE["consecutiveLosses"] = 4
    core.entry_snapshot = entry_snapshot(candidate(strategyStrength=0.5))

    _, approved = _approved(core)

    assert approved["riskApproved"] is True
    assert approved["riskSizeFactor"] == 1.0
    assert approved["riskFlags"] == []


def test_authoritative_daily_loss_risk_blocks_candidate():
    core = CoreStub()
    core.daily = {
        "ok": True,
        "blocked": True,
        "newEntriesAllowed": False,
        "lockType": "DAILY_NET_LOSS",
        "reason": "Daily net-loss limit reached",
    }
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskStatus"] == "BLOCKED_RISK"
    assert row["riskDecision"]["code"] == "DAILY_NET_LOSS"
    assert result["approvedRiskQueueSize"] == 0
    assert core.position_calls == 0


def test_authoritative_max_trades_per_day_blocks_candidate():
    core = CoreStub()
    core.daily = {
        "ok": True,
        "blocked": True,
        "newEntriesAllowed": False,
        "lockType": "MAX_TRADES_PER_DAY",
        "reason": "Maximum trades for the trading day reached",
    }
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "MAX_TRADES_PER_DAY"
    assert result["approvedRiskQueueSize"] == 0


def test_same_symbol_open_is_blocked_by_protected_position_guard():
    core = CoreStub()
    core.position = {
        "ok": False,
        "reason": "Position already open for BTCUSDT",
        "openPositions": 1,
        "maxOpenPositions": 3,
    }
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "POSITION_GUARD_BLOCKED"
    assert "already open" in row["riskDecision"]["reason"].lower()


def test_max_three_open_positions_is_blocked_by_protected_position_guard():
    core = CoreStub()
    core.position = {
        "ok": False,
        "reason": "Max open positions reached (3/3)",
        "openPositions": 3,
        "maxOpenPositions": 3,
    }
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "POSITION_GUARD_BLOCKED"
    assert "3/3" in row["riskDecision"]["reason"]
    assert result["approvedRiskQueueSize"] == 0


def test_existing_cooldown_rule_blocks_candidate():
    core = CoreStub()
    core.BOT_STATE["lastTradeAt"] = 3950
    core.BOT_STATE["cooldownSeconds"] = 180
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "COOLDOWN_ACTIVE"
    cooldown = row["riskDecision"]["checks"]["cooldown"]
    assert cooldown["elapsedSeconds"] == 50
    assert cooldown["cooldownSeconds"] == 180


def test_existing_agreement_policy_blocks_restricted_symbol(monkeypatch):
    core = CoreStub()
    monkeypatch.setattr(
        risk.agreement_execution_guard,
        "is_restricted_symbol",
        lambda symbol: True,
    )
    monkeypatch.setattr(
        risk.agreement_execution_guard,
        "rejection",
        lambda symbol, boundary: {
            "code": "AGREEMENT_REQUIRED_SYMBOL_BLOCKED",
            "reason": f"{symbol} blocked at {boundary}",
        },
    )
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "AGREEMENT_REQUIRED_SYMBOL_BLOCKED"
    assert "authoritative_entry_risk" in row["riskDecision"]["reason"]
    assert core.daily_calls == 0


def test_existing_candidate_age_limit_blocks_stale_entry(monkeypatch):
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(createdAt=1000))
    monkeypatch.setattr(
        risk.execution_handoff,
        "settings",
        lambda: {"maxCandidateAgeSeconds": 1200},
    )
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskDecision"]["code"] == "CANDIDATE_STALE"
    freshness = row["riskDecision"]["checks"]["freshness"]
    assert freshness["ageSeconds"] == 3000
    assert freshness["maximumAgeSeconds"] == 1200


def test_already_submitted_order_is_blocked():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(orderSubmitted=True))
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskStatus"] == "BLOCKED_RISK"
    assert row["riskDecision"]["code"] == "INVALID_CONFIRMED_ENTRY"
    assert result["approvedRiskQueueSize"] == 0


def test_invalid_candidate_identity_is_blocked():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(candidateKey=""))
    risk.install(core)

    result = risk.build(core, now=4000)

    row = result["rows"][0]
    assert row["riskStatus"] == "BLOCKED_RISK"
    assert row["riskDecision"]["code"] == "INVALID_CONFIRMED_ENTRY"
    assert result["approvedRiskQueueSize"] == 0


def test_non_execution_grade_is_blocked():
    core = CoreStub()
    core.entry_snapshot = entry_snapshot(candidate(grade="B+"))
    risk.install(core)

    result = risk.build(core, now=4000)

    assert result["rows"][0]["riskDecision"]["code"] == "INVALID_CONFIRMED_ENTRY"
    assert result["approvedRiskQueueSize"] == 0


def test_entry_safety_hands_off_to_step8_without_quantity_or_order():
    core = CoreStub()

    result, approved = _approved(core)

    assert "qty" not in approved
    assert approved["positionSizingStatus"] == "NOT_EVALUATED_STEP8"
    assert approved["executionStatus"] == "AWAITING_POSITION_SIZING"
    assert approved["orderSubmitted"] is False
    assert result["positionSizingCalls"] == 0
    assert result["orderSubmissions"] == 0
    assert core.sizing_calls == 0
    assert core.order_calls == 0


def test_same_input_fingerprint_is_not_rechecked():
    core = CoreStub()
    risk.install(core)

    first = risk.ensure_current(core, now=4000)
    second = risk.ensure_current(core, now=4050)

    assert first["approvedRiskQueueSize"] == 1
    assert second["approvedRiskQueueSize"] == 1
    assert core.daily_calls == 1
    assert core.position_calls == 1


def test_risk_snapshot_is_persisted_and_reloaded():
    core = CoreStub()
    store = core._durable_state_store
    risk.install(core)
    result = risk.build(core, now=4000)
    assert result["persisted"] is True

    risk._reset_for_tests()
    restarted = CoreStub()
    restarted._durable_state_store = store
    risk.install(restarted)
    restored = risk.snapshot()

    assert restored["inputFingerprint"] == result["inputFingerprint"]
    assert restored["approvedRiskQueueSize"] == 1
    assert restored["approvedRiskQueue"][0]["candidateKey"] == result[
        "approvedRiskQueue"
    ][0]["candidateKey"]
