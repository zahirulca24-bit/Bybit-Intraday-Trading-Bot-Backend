# Scanner and Signal Code-Side Blocker Audit

Date: 25 July 2026
Scope: canonical runtime only (`backend/guarded_server.py` wrapping `backend/server.py`)
Status: audit complete; no defect implementation started

## Audited path

```text
Top-gainer universe → candle fetch → five strategy votes → router decision → best-symbol selection → risk handoff
```

## Verdict

The scanner is not hard-crashed or permanently disabled. It can fetch a universe, evaluate symbols, produce `Buy`, `Sell`, or `WAIT`, and hand an executable decision to the risk layer.

However, signal output is not yet trustworthy enough for automated execution because four code-side blockers remain.

## Confirmed blockers

### SCAN-01 — Requested scanner interval is ignored

- **Location:** `evaluate_signal(symbol, interval, mode)` and `BotEngineV2.evaluate(...)`
- **Expected:** The selected scanner interval must materially control the data and strategy evaluation.
- **Actual:** `evaluate_signal(...)` accepts `interval` but calls `get_bot_engine().evaluate(symbol, mode)` without passing it. The engine always fetches hard-coded `60`, `15`, and `5` minute candles.
- **Impact:** Changing the UI/scanner timeframe does not change the underlying strategy timeframes, while the API response still reports the requested interval.
- **Severity:** Critical signal-truthfulness blocker
- **Automated proof:** Pending
- **Runtime proof:** Pending comparison of identical symbols across different requested intervals

### SIGNAL-01 — Forming candles can generate actionable votes

- **Location:** `fetch_candles(...)` and all strategy engines using the last candle
- **Expected:** Automated signals must use confirmed closed candles only.
- **Actual:** Bybit kline rows are sorted and returned without removing the currently forming candle. Strategies use `tf1h[-1]`, `tf15m[-1]`, and `tf5m[-1]` directly.
- **Impact:** Votes and router decisions can appear, disappear, or reverse before candle close.
- **Severity:** Critical execution-readiness blocker
- **Existing register:** `MARKET-01`
- **Automated proof:** Pending candle-boundary test
- **Runtime proof:** Pending live pre-close/post-close comparison

### SIGNAL-02 — No candle-level execution idempotency

- **Location:** `bot_loop()`, `bot_tick()`, `select_best_signal(...)`, and execution guards
- **Expected:** A specific symbol, side, strategy decision, and closed-candle timestamp must be executable at most once.
- **Actual:** The loop rescans every 30 seconds, but no signal key or candle timestamp is stored and checked before execution. The current controls rely on open-position checks and a time-based cooldown.
- **Impact:** If a position closes while the same candle-level signal remains active, the same signal can be executed again after cooldown without a new confirmed candle.
- **Severity:** Critical duplicate-signal blocker
- **Automated proof:** Pending repeated-tick test using the same candle identity
- **Runtime proof:** Pending controlled same-candle replay

### SCAN-02 — Scan completion has no bounded deadline or partial-result contract

- **Location:** `select_best_signal(...)` and `/api/bot/scanner`
- **Expected:** Each scan must have a total deadline, per-symbol timeout handling, and explicit partial/degraded status.
- **Actual:** Symbols are evaluated sequentially. Each symbol requires three timeframe fetches, and each network call can wait up to 15 seconds. One slow symbol delays all later symbols; there is no overall scan deadline or partial-result status.
- **Impact:** A nominal 30-second loop can overrun substantially, delay signals, and repeatedly skip overlapping guarded ticks.
- **Severity:** High availability blocker
- **Automated proof:** Pending slow-provider timing test
- **Runtime proof:** Pending latency/degraded-scan observation

### SCAN-03 — Canonical scanner request accepts an uncapped symbol list

- **Location:** canonical `GET /api/bot/scanner`
- **Expected:** Strict server-side symbol count and request-rate limits.
- **Actual:** The optional comma-separated `symbols` query is iterated without a maximum count. Each symbol triggers multi-timeframe strategy evaluation.
- **Impact:** A caller can cause excessive public Bybit requests and long-running scanner work even on the canonical runtime.
- **Severity:** High resource-exhaustion blocker
- **Related register:** `RATE-01` previously documented only for the alternative runtime
- **Automated proof:** Pending capped-list contract test
- **Runtime proof:** Pending safe rejection test

## Already-confirmed strategy accuracy dependencies

These findings were already in the main audit and remain prerequisites for trustworthy signals:

- `MARKET-02`: S/R breakout includes the tested 1H candle in its own support/resistance reference zone.
- `REPLAY-01`: higher-timeframe replay includes candles by start time and can leak future OHLC values.
- `REPLAY-02`: ORB replay uses current wall-clock date.

## Not blockers / intended behavior

The following were reviewed and are not classified as defects:

- Conservative mode requires two matching votes.
- Conflicting Buy and Sell votes return `WAIT`.
- A failed/empty top-gainer selection falls back to the default symbol universe and exposes `source: fallback`.
- Market-data failure returns `WAIT` rather than creating an executable signal.
- `select_best_signal(...)` ranks executable decisions by router confidence and matching vote strength.

## Required order before order-lifecycle work

1. Make scanner interval behavior truthful or explicitly lock the product to fixed `1H + 15M + 5M` timeframes.
2. Enforce closed-candle-only evaluation.
3. Add candle-level signal identity and one-execution-per-signal protection.
4. Add bounded scan deadlines, partial/degraded results, and canonical symbol caps.
5. Prove scanner and router behavior on the deployed app.
6. Only then proceed to final order, fill, and protection verification.

## Audit control statement

This document records code-confirmed findings only. No scanner, strategy, router, risk, or execution code was changed in this audit branch.
