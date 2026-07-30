"""Exclude contracts that require separate Bybit trading agreements.

The Bybit ticker feed does not expose account-level agreement eligibility. The
canonical scanner therefore applies a fail-closed denylist for contracts that
have produced agreement-required rejections in Bybit Demo. Operators may add
more symbols through ``AGREEMENT_REQUIRED_SYMBOLS``; the built-in exclusions
cannot be removed through environment configuration.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

_BUILT_IN_EXCLUSIONS = frozenset({"MUUSDT", "CLUSDT"})


def excluded_symbols() -> frozenset[str]:
    configured = {
        value.strip().upper()
        for value in os.environ.get("AGREEMENT_REQUIRED_SYMBOLS", "").split(",")
        if value.strip()
    }
    return frozenset(_BUILT_IN_EXCLUSIONS | configured)


def filter_symbols(values: Iterable[object]) -> tuple[list[str], list[str]]:
    blocked = excluded_symbols()
    accepted: list[str] = []
    rejected: list[str] = []
    for value in values or []:
        symbol = str(value or "").strip().upper()
        if symbol in blocked:
            rejected.append(symbol)
        else:
            accepted.append(symbol)
    return accepted, list(dict.fromkeys(rejected))


def filter_universe(universe: Any) -> dict:
    payload = dict(universe) if isinstance(universe, dict) else {}
    blocked = excluded_symbols()

    def allowed(row: Any) -> bool:
        return isinstance(row, dict) and str(row.get("symbol") or "").upper() not in blocked

    symbols = [
        str(symbol).upper()
        for symbol in payload.get("symbols") or []
        if str(symbol or "").upper() not in blocked
    ]
    rows = [dict(row) for row in payload.get("rows") or [] if allowed(row)]
    shortlist = [dict(row) for row in payload.get("shortlist") or [] if allowed(row)]

    payload["symbols"] = symbols
    payload["rows"] = rows
    if "shortlist" in payload:
        payload["shortlist"] = shortlist
    metrics = dict(payload.get("metrics") or {})
    metrics["agreementRequiredExcluded"] = len(blocked)
    payload["metrics"] = metrics
    payload["agreementRequiredExcludedSymbols"] = sorted(blocked)
    return payload


def install(core: Any) -> None:
    if getattr(core, "_agreement_contract_filter_installed", False):
        return
    original = core.top_gainer_universe

    def eligible_universe(*args: Any, **kwargs: Any) -> dict:
        return filter_universe(original(*args, **kwargs))

    core.top_gainer_universe = eligible_universe
    core._agreement_contract_filter_installed = True


def status() -> dict:
    return {
        "installed": True,
        "excludedSymbols": sorted(excluded_symbols()),
        "policy": "fail_closed_denylist",
    }
