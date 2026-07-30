ROUTER_MODES = {"conservative", "balanced", "aggressive"}


def normalize_mode(mode):
    mode = str(mode or "balanced").lower()
    return mode if mode in ROUTER_MODES else "balanced"


def vote_strength(vote_item):
    try:
        return abs(float(vote_item.get("strength") or 0))
    except (TypeError, ValueError, AttributeError):
        return 0.0


def single_vote_min_strength(mode):
    if mode == "aggressive":
        return 2.0
    if mode == "balanced":
        return 3.0
    return 0.0


def _weak_single_vote(votes, mode):
    if len(votes) != 1:
        return None
    leader = votes[0]
    minimum = single_vote_min_strength(mode)
    strength = vote_strength(leader)
    return (leader, strength, minimum) if strength < minimum else None


def _wait_for_weak_vote(side, votes, required, mode):
    weak = _weak_single_vote(votes, mode)
    if not weak:
        return None
    _, strength, minimum = weak
    return {
        "decision": "WAIT",
        "confidence": len(votes),
        "requiredVotes": required,
        "mode": mode,
        "reason": f"Router waiting: single {side} vote strength {strength:.2f} is below {minimum:.2f}",
    }


def route_votes(votes, mode="balanced"):
    mode = normalize_mode(mode)
    buy_votes = [item for item in votes if item.get("signal") == "Buy"]
    sell_votes = [item for item in votes if item.get("signal") == "Sell"]
    required = 2 if mode == "conservative" else 1

    if mode == "aggressive":
        buy_score = len(buy_votes) + sum(vote_strength(item) for item in buy_votes) / 100
        sell_score = len(sell_votes) + sum(vote_strength(item) for item in sell_votes) / 100
        if buy_votes and buy_score > sell_score:
            blocked = _wait_for_weak_vote("Buy", buy_votes, required, mode)
            if blocked:
                return blocked
            leader = max(buy_votes, key=vote_strength)
            return {
                "decision": "Buy",
                "confidence": len(buy_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Buy from {leader['engine']}",
            }
        if sell_votes and sell_score > buy_score:
            blocked = _wait_for_weak_vote("Sell", sell_votes, required, mode)
            if blocked:
                return blocked
            leader = max(sell_votes, key=vote_strength)
            return {
                "decision": "Sell",
                "confidence": len(sell_votes),
                "requiredVotes": required,
                "mode": mode,
                "reason": f"Aggressive demo approved Sell from {leader['engine']}",
            }

    if len(buy_votes) >= required and not sell_votes:
        blocked = _wait_for_weak_vote("Buy", buy_votes, required, mode)
        if blocked:
            return blocked
        leader = max(buy_votes, key=vote_strength)
        return {
            "decision": "Buy",
            "confidence": len(buy_votes),
            "requiredVotes": required,
            "mode": mode,
            "reason": f"{mode.title()} router approved Buy from {leader['engine']}",
        }

    if len(sell_votes) >= required and not buy_votes:
        blocked = _wait_for_weak_vote("Sell", sell_votes, required, mode)
        if blocked:
            return blocked
        leader = max(sell_votes, key=vote_strength)
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
