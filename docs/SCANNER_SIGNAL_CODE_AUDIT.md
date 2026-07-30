# Scanner and Signal Code Audit

Date: 25 July 2026
Status: Code audit complete; no fixes implemented
Canonical runtime: `backend/guarded_server.py` wrapping `backend/server.py`

## Audited flow

```text
Top-gainer universe → multi-timeframe candle fetch → five strategies → router → best-signal selection → risk handoff
```

## Confirmed blockers

### SIGNAL-01 — Scanner timeframe input is not used by canonical evaluation

- **Location:** `evaluate_signal(symbol, interval, mode)` and `BotEngineV2.evaluate(symbol, mode)`
- **Expected:** The scanner/API timeframe selection must control the evaluated entry timeframe or be rejected as unsupported.
- **Actual:** `evaluate_signal(...)` receives `interval` but calls `get_bot_engine().evaluate(symbol, mode)` without passing it. The engine always fetches fixed `60`, `15`, and `5` minute candles.
- **Impact:** UI/API may display a selected timeframe that did not control signal generation.
- **Severity:** High

### SIGNAL-02 — Current forming candles are used as confirmed strategy candles

- **Location:** `fetch_candles(...)` and all five strategy engines
- **Expected:** Trading decisions use only candles whose close time has passed.
- **Actual:** Bybit kline rows are sorted and returned without removing the current open candle. Strategies use `[-1]` as confirmation.
- **Impact:** Signals can appear/disappear intrabar and automatic execution can act on an unconfirmed candle.
- **Severity:** Critical for automatic execution

### SIGNAL-03 — Router has no minimum signal-strength threshold

- **Location:** `route_votes(...)`
- **Expected:** A router described as requiring a strong vote must enforce a quantitative minimum strength/quality gate.
- **Actual:** Balanced mode approves one actionable vote regardless of its `strength`; aggressive mode also approves one side when its simple vote score is greater. `strength` is used only for ranking/tie influence, not eligibility.
- **Impact:** A weak single strategy vote can become an executable signal.
- **Severity:** High

### SIGNAL-04 — Signal selection has no explicit grade or trade-quality gate

- **Location:** `select_best_signal(...)` and `signal_score(...)`
- **Expected:** Only strategy setups meeting defined execution quality, confidence, and risk prerequisites become executable candidates.
- **Actual:** Every `Buy` or `Sell` router decision is executable. The selector picks the maximum `(confidence * 1000) + vote strength` without an A+/A grade, minimum score, RR, spread-at-entry, or setup-validity gate.
- **Impact:** The system can select the best of several weak candidates rather than a genuinely valid setup.
- **Severity:** Critical for strategy acceptance

### SCAN-01 — One automatic scan cycle can generate a large sequential API workload

- **Location:** `top_gainer_universe(...)`, `select_best_signal(...)`, `BotEngineV2.market_snapshot(...)`
- **Actual:** Up to 10 symbols are evaluated sequentially, and each evaluation makes three kline requests. A cycle can therefore require at least 30 market-data calls, plus universe, sizing, wallet, rules, position, and risk calls. Individual HTTP requests allow up to 15 seconds.
- **Impact:** A nominal 30-second bot cycle can overrun its interval, become stale, or encounter exchange/request limits. Browser scanner refreshes can add another independent scan workload.
- **Severity:** High reliability blocker

### SCAN-02 — Market-universe failure silently falls back instead of exposing degraded scan state

- **Location:** `top_gainer_universe(...)`
- **Expected:** A ticker/API/filter failure should be distinguishable from a valid market universe.
- **Actual:** When no selected rows exist, the function returns a fixed default symbol list with `source: fallback`, including cases where the upstream request failed.
- **Impact:** The app can continue showing/scanning symbols while the top-gainer source is unavailable; a degraded scan may look operational.
- **Severity:** High truthfulness gap

### SIGNAL-05 — Multi-timeframe freshness and alignment are not validated

- **Location:** `BotEngineV2.market_snapshot(...)`
- **Expected:** 1H, 15M, and 5M datasets must be fresh and aligned to the same decision timestamp.
- **Actual:** The engine checks only that all three lists are non-empty. It does not verify last-candle age, clock alignment, duplicate timestamps, gaps, or that the 15M/1H candle was closed before the 5M decision candle.
- **Impact:** A router decision can combine stale or temporally inconsistent timeframes.
- **Severity:** Critical for deterministic signals

## Existing related findings

- `MARKET-01`: forming candle included in strategy evaluation
- `MARKET-02`: S/R breakout includes the tested current 1H candle in its reference zone
- `REPLAY-01`: higher-timeframe replay look-ahead leakage
- `REPLAY-02`: ORB historical replay uses wall-clock date

## Verdict

The code can fetch symbols and produce `Buy`, `Sell`, or `WAIT`; there is no single syntax-level blocker preventing signal generation. However, the current signal path is **not acceptable for automatic execution** because candle closure, timeframe truthfulness, data alignment, minimum signal strength, setup quality, and scan workload are not safely enforced.

## Required next implementation batch

**Scanner, Closed-Candle and Signal Accuracy**

The batch must remain bounded to:

1. closed-candle filtering and timestamp alignment;
2. scanner timeframe contract;
3. stale/gap detection;
4. explicit signal eligibility/strength gate;
5. bounded scan workload and degraded-state reporting;
6. focused automated tests;
7. deployed scanner and signal runtime proof.

No order/fill/protection changes belong in this batch.
