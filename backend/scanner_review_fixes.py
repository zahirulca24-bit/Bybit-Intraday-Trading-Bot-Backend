"""Review fixes for Serial 6 scanner safety."""

from __future__ import annotations

from typing import Any

try:
    from . import intraday_scanner
except ImportError:
    import intraday_scanner


def install(core: Any) -> None:
    if getattr(core, "_scanner_review_fixes_installed", False):
        return

    original_build_universe = intraday_scanner.build_universe

    def fail_closed_universe(core_arg: Any, force: bool = False, limit: int | None = None) -> dict:
        result = original_build_universe(core_arg, force=force, limit=limit)
        metrics = result.get("metrics") or {}
        if int(metrics.get("enriched") or 0) == 0:
            intraday_scanner._CACHE.update(
                {
                    "symbols": [],
                    "rows": [],
                    "shortlist": [],
                    "source": "liquid_intraday_top_movers_empty",
                }
            )
            result = dict(intraday_scanner._CACHE)
        return result

    intraday_scanner.build_universe = fail_closed_universe

    with core.BOT_LOCK:
        if float(core.BOT_STATE.get("takeProfitPct") or 0) <= 1.6:
            core.BOT_STATE["takeProfitPct"] = 2.0

    core._scanner_review_fixes_installed = True
