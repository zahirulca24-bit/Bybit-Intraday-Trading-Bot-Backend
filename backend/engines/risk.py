import time


def _number(value, default=0.0):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else default


def _vote_strength(vote_item):
    return abs(_number((vote_item or {}).get("strength"), 0.0))


def _matching_votes(state, signal):
    votes = state.get("engineVotes") or state.get("strategyVotes") or []
    if not isinstance(votes, list):
        return []
    return [vote for vote in votes if isinstance(vote, dict) and vote.get("signal") == signal]


def _losing_streak(state):
    daily = state.get("dailyRisk") if isinstance(state.get("dailyRisk"), dict) else {}
    return max(
        0,
        int(_number(
            state.get("consecutiveLosses", state.get("losingStreak", daily.get("consecutiveLosses", 0))),
            0,
        )),
    )


def _is_authoritative_entry_safety_candidate(state):
    """Identify the canonical closed-5M -> Step-7 automatic entry contract."""
    return bool(
        state.get("riskStatus") == "PENDING_RISK"
        and state.get("positionSizingStatus") == "NOT_EVALUATED_STEP8"
        and state.get("orderSubmitted") is False
    )


def signal_risk_policy(state, signal):
    """Return risk metadata without touching exchange state.

    Canonical automatic candidates have already passed strategy selection,
    15M classification, grade eligibility, and closed-5M confirmation. For
    those candidates this helper is intentionally a no-op for strategy quality:
    it cannot reject on vote count/strength, cannot reduce size, and cannot
    apply losing-streak sizing. Legacy/manual callers keep the historical policy
    until those paths are retired separately.
    """
    if signal not in ("Buy", "Sell"):
        return {
            "ok": False,
            "reason": "No executable signal",
            "sizeFactor": 0.0,
            "riskFlags": ["no_executable_signal"],
        }

    if _is_authoritative_entry_safety_candidate(state):
        return {
            "ok": True,
            "reason": "Entry safety candidate; strategy quality already confirmed upstream",
            "sizeFactor": 1.0,
            "riskFlags": [],
            "entrySafetyOnly": True,
        }

    router = state.get("router") if isinstance(state.get("router"), dict) else {}
    matching = _matching_votes(state, signal)
    strengths = [_vote_strength(vote) for vote in matching]
    strongest = max(strengths) if strengths else _number(state.get("strategyStrength"), 0.0)
    matching_count = len(matching) if matching else int(_number(router.get("confidence"), 0))
    flags = []
    size_factor = 1.0

    if matching_count <= 0 and strongest <= 0:
        flags.append("signal_context_unavailable")
    elif matching_count <= 1:
        if strongest < 2.0:
            return {
                "ok": False,
                "reason": f"Signal risk blocked: single-vote strength {strongest:.2f} is below 2.00",
                "sizeFactor": 0.0,
                "riskFlags": ["weak_single_vote"],
                "signalStrength": round(strongest, 4),
                "matchingVotes": matching_count,
            }
        if strongest < 3.0:
            size_factor = min(size_factor, 0.5)
            flags.append("single_vote_reduced_size")
        elif strongest < 4.0:
            size_factor = min(size_factor, 0.75)
            flags.append("moderate_single_vote")
    elif strongest < 2.5:
        size_factor = min(size_factor, 0.75)
        flags.append("multi_vote_low_strength")

    streak = _losing_streak(state)
    if streak >= 4:
        return {
            "ok": False,
            "reason": f"Losing streak risk block: {streak} consecutive losses",
            "sizeFactor": 0.0,
            "riskFlags": [*flags, "losing_streak_block"],
            "signalStrength": round(strongest, 4),
            "matchingVotes": matching_count,
            "consecutiveLosses": streak,
        }
    if streak == 3:
        size_factor = min(size_factor, 0.25)
        flags.append("losing_streak_critical_reduce")
    elif streak == 2:
        size_factor = min(size_factor, 0.5)
        flags.append("losing_streak_reduce")

    reason = "Signal risk approved"
    if size_factor < 1:
        reason = f"Signal risk approved with {size_factor:.2f}x size factor"
    return {
        "ok": True,
        "reason": reason,
        "sizeFactor": round(size_factor, 4),
        "riskFlags": flags,
        "signalStrength": round(strongest, 4),
        "matchingVotes": matching_count,
        "consecutiveLosses": streak,
    }


class RiskEngine:
    def __init__(self, position_size_fn, open_positions_count_fn=None):
        self.position_size_fn = position_size_fn
        self.open_positions_count_fn = open_positions_count_fn

    def evaluate(self, state, signal):
        now = time.time()
        if signal not in ("Buy", "Sell"):
            return {"ok": False, "reason": "No executable signal", "riskFlags": ["no_executable_signal"]}
        policy = signal_risk_policy(state, signal)
        if not policy.get("ok"):
            return {**policy, "ok": False, "reason": policy.get("reason", "Signal risk blocked")}
        state["riskPolicy"] = policy
        state["riskSizeFactor"] = policy.get("sizeFactor", 1.0)
        if now - float(state.get("lastTradeAt") or 0) < int(state["cooldownSeconds"]):
            return {**policy, "ok": False, "reason": "Cooldown active", "riskFlags": [*policy.get("riskFlags", []), "cooldown_active"]}
        position_size, position_msg = self.position_size_fn(state["symbol"])
        if position_size is None:
            return {**policy, "ok": False, "reason": position_msg, "riskFlags": [*policy.get("riskFlags", []), "position_check_unavailable"]}
        if position_size > 0:
            return {**policy, "ok": False, "reason": "Position already open", "riskFlags": [*policy.get("riskFlags", []), "position_already_open"]}
        if self.open_positions_count_fn:
            open_count, open_msg = self.open_positions_count_fn()
            if open_count is None:
                return {**policy, "ok": False, "reason": open_msg, "riskFlags": [*policy.get("riskFlags", []), "open_position_count_unavailable"]}
            max_open = max(1, int(state.get("maxOpenPositions") or 1))
            if open_count >= max_open:
                return {**policy, "ok": False, "reason": f"Max open positions reached ({open_count}/{max_open})", "riskFlags": [*policy.get("riskFlags", []), "max_open_positions_reached"]}
        return {**policy, "ok": True, "reason": policy.get("reason", "Risk approved")}

    def check(self, state, signal):
        decision = self.evaluate(state, signal)
        state["riskDecision"] = decision
        return bool(decision.get("ok")), decision.get("reason", "Risk blocked")
