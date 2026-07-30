# Bybit Demo Intraday Trading Bot

Demo-only intraday trading control center for Bybit Demo Trading.

> **Audit status — 29 July 2026:** the canonical Demo runtime is operational, the worker engine can scan and submit Demo orders, and `maxOpenPositions` is enforced at `3`. A live Demo order was observed as fully filled. The repository remains **NO-GO for real-money trading** until every P0 checklist item is completed and independently verified with code evidence plus Demo runtime evidence.

## Current readiness

| Area | Status |
|---|---|
| Bybit environment | **Demo only** |
| Backend connection | **Working** |
| Worker orchestrator | **Working** |
| Automatic scanner-to-trade execution | **Observed working in Demo** |
| Final order fill verification | **Working** |
| Maximum open positions | **3** |
| Agreement-required symbol exclusion | **PASS — scanner/setup/handoff/final-submit guarded** |
| Authoritative risk gate | **Incomplete** |
| Advanced position-management verification | **Incomplete** |
| Restart-safe persistence | **Deployment-dependent** |
| Real-money trading | **Strictly disabled / unsupported** |

## Canonical runtime

The only supported runtime command is:

```text
python backend/secure_server.py
```

Render uses this command through `render.yaml`.

`backend/position_synced_server.py` is an internal verified-trading module and must not be used as the deployment entrypoint. The legacy ASGI entrypoint `app/main.py` remains disabled/fail-closed.

## Runtime architecture

```text
Bybit Demo Market
    ↓
Symbol Selection Worker
    ↓
Top-30 Active Trend Pool
    ↓
Market Data + Indicator Engine
    ↓
Five Strategy Engines
    ↓
Router Engine
    ↓
Setup Verification Worker
    ↓
Agreement Execution Guard
    ↓
Risk and Execution Gates
    ↓
Execution Handoff
    ↓
Final Order Submit Guard
    ↓
Bybit Demo Order
    ↓
Final-Fill Verification + Initial SL/TP
    ↓
Trade Journal and Position Management
```

## Verified completed checklist

The following items are marked complete only where repository evidence exists. Runtime-sensitive items are not marked complete unless Demo evidence is also available.

### Runtime and deployment

- [x] Canonical runtime uses `python backend/secure_server.py`.
- [x] Render start command points to the canonical runtime.
- [x] Legacy ASGI entrypoint remains disabled/fail-closed.
- [x] Bybit base URL is locked to `https://api-demo.bybit.com` in deployment configuration.
- [x] Real-money trading is documented as unsupported.
- [x] Worker orchestrator starts the symbol, setup, and execution workers.
- [x] `maxOpenPositions` is configured and runtime-enforced at `3`.

### Security and API controls

- [x] Private API routes require bearer-token authentication.
- [x] CORS uses same-origin or an explicit allowlist.
- [x] Scanner requests use a non-blocking concurrency lock.
- [x] Scanner symbol normalization and rejection reporting are implemented.
- [x] Kill-switch capability exists in the runtime.
- [x] Demo endpoint policy is installed before worker execution.

### Scanner and execution safety

- [x] Closed-candle multi-timeframe scanning is implemented.
- [x] Same-symbol existing-position protection is implemented.
- [x] Maximum open-position protection is implemented.
- [x] Execution claim and idempotency modules are installed.
- [x] Race-condition and idempotency review fixes are installed.
- [x] Final-fill verification requires `Filled` status and positive executed quantity.
- [x] Pending/unresolved acknowledged entries are preserved instead of being treated as successful fills.
- [x] One automatic Demo order was observed reaching a final fill.
- [x] Agreement-required symbols are blocked at setup queue insertion.
- [x] Agreement-required cached setup candidates are pruned before execution handoff.
- [x] Agreement-required symbols are blocked again at final `place_demo_order` submission boundary.
- [x] Regression tests cover `CLUSDT`/`MUUSDT` setup queue, cached handoff, and final submit blocking.

### Trade lifecycle and analytics

- [x] Initial order placement is implemented.
- [x] Initial stop-loss and take-profit attachment is implemented.
- [x] Trade journal recording is implemented.
- [x] Replay data collection is installed.
- [x] Replay sessions and replay engine are installed.
- [x] Replay performance summary and journal API are wired.
- [x] Regression tests were added for replay performance and journal contracts.

## Open task checklist

### P0 — Execution blockers

These tasks must be completed before the bot can be considered safe for unattended Demo operation.

- [x] **P0-01 — Close agreement-required symbol leakage.**
  - Apply contract eligibility at scanner, shortlist, cached setup, queue, handoff, and final submission boundaries.
  - Reject `MUUSDT`, `CLUSDT`, and configured exclusions.
  - Add regression tests proving blocked symbols cannot reach the Bybit request layer.
  - Evidence: `backend/agreement_execution_guard.py`, `backend/secure_server.py`, `tests/test_agreement_execution_guard.py`.

- [ ] **P0-02 — Default startup state must be PAUSED.**
  - Process boot must not automatically authorize new order submission.
  - Scanner, signal generation, and execution permission must have explicit states.
  - A deploy or restart must not silently resume execution.

- [ ] **P0-03 — Build one authoritative pre-order risk gate.**
  - Consolidate every execution permission check into one fail-closed function.
  - No route, worker, cached setup, or handoff path may bypass this gate.
  - Return structured rejection codes for every blocked order.

- [ ] **P0-04 — Add equity-based risk sizing.**
  - Calculate position size from account equity, entry, stop distance, and configured risk percentage.
  - Reject missing, zero, negative, stale, or inconsistent account-equity values.
  - Enforce exchange quantity step and minimum-order rules.

- [ ] **P0-05 — Enforce account-level exposure limits.**
  - Cap total notional exposure.
  - Cap per-symbol exposure.
  - Cap same-direction exposure.
  - Block highly correlated concurrent positions.

- [ ] **P0-06 — Add daily loss and consecutive-loss locks.**
  - Enforce configured daily realized-loss limit.
  - Add consecutive-loss cooldown.
  - Persist lock state across restart and redeploy.
  - Require an audited reset path.

- [ ] **P0-07 — Verify leverage and margin mode before every entry.**
  - Confirm allowed leverage cap.
  - Confirm isolated/cross mode policy.
  - Fail closed when exchange state cannot be verified.
  - Record requested and confirmed exchange settings.

- [ ] **P0-08 — Make runtime state restart-safe.**
  - Configure persistent storage through `BOT_STATE_DB_PATH` or a durable database.
  - Persist execution identities, pending entries, cooldowns, locks, and management actions.
  - Test service restart with an open position and unresolved order.

- [ ] **P0-09 — Synchronize UI bot state with worker execution state.**
  - Page refresh, navigation, frontend reconnect, and backend deploy must show authoritative backend state.
  - The UI must never display stopped while execution workers are active.
  - The UI must never display running when execution is blocked.

- [ ] **P0-10 — Complete controlled Demo lifecycle verification.**
  - Verify TP1 partial close.
  - Verify breakeven stop update.
  - Verify trailing-stop update.
  - Verify TP2 or final close.
  - Verify duplicate-action protection.
  - Verify reconciliation after timeout or network failure.

### P1 — Strategy and signal quality

- [ ] **P1-01 — Add standardized signal grading.**
  - `A+` and `A` may be execution-eligible.
  - `B+` must be watch-only.
  - Lower grades must be rejected.
  - Grade calculation must be deterministic and tested.

- [ ] **P1-02 — Normalize strategy confidence.**
  - Every strategy must return strength on the same bounded scale.
  - Router decisions must not compare raw price distance with RSI distance.
  - Add engine-specific weights and regime-aware weighting.

- [ ] **P1-03 — Enforce minimum risk-reward.**
  - Calculate RR after fees, spread, slippage allowance, and stop distance.
  - Reject trades below the configured minimum RR.
  - Store planned and realized R-multiples in the journal.

- [ ] **P1-04 — Upgrade trend-follow logic.**
  - Add EMA200 regime context.
  - Add MACD confirmation.
  - Add ATR regime and stop quality.
  - Add rejection-candle structure.
  - Raise relative-volume confirmation from weak participation to an explicit threshold.

- [ ] **P1-05 — Require breakout quality and retest.**
  - Add zone-based support/resistance.
  - Require breakout distance relative to ATR.
  - Require close confirmation and configurable retest.
  - Reject fake breaks and late entries.

- [ ] **P1-06 — Replace fixed-lookback RSI divergence.**
  - Use confirmed swing highs and lows.
  - Support regular and hidden divergence.
  - Require minimum pivot separation and zone context.

- [ ] **P1-07 — Convert VWAP to session-anchored VWAP.**
  - Reset VWAP by configured London and New York sessions.
  - Add deviation bands and rejection quality.
  - Prevent old-session volume from contaminating the active session.

- [ ] **P1-08 — Convert ORB to London/NY session-aware logic.**
  - Configure session start/end in `Asia/Dhaka`.
  - Build opening ranges from the selected session window.
  - Add setup expiry, breakout quality, and optional retest confirmation.
  - Do not use the first UTC-day candle as the final ORB definition.

- [ ] **P1-09 — Strengthen market-data validation.**
  - Detect missing, duplicate, stale, out-of-order, and abnormal-spike candles.
  - Reject incomplete synchronized snapshots.
  - Record data-quality rejection reasons.

### P2 — Reliability, observability, and release gates

- [ ] **P2-01 — Add worker watchdog and backoff.**
- [ ] **P2-02 — Add full audit events.**
- [ ] **P2-03 — Add end-to-end integration tests.**
- [ ] **P2-04 — Add release CI gates.**
- [ ] **P2-05 — Add deployment health acceptance.**

## Engine inventory and current limitations

### Symbol Selection Worker

**File:** `backend/worker.py`

Implemented:

- Fetch active Bybit USDT linear perpetual contracts.
- Reject low-turnover and excessive-spread symbols.
- Scan symbols in round-robin batches.
- Use fully closed `1H` candles.
- Rank and maintain an active symbol pool.

Still missing or incomplete:

- EMA200 regime.
- ATR regime.
- Correlation controls.
- Stronger stale-candidate expiry.

### Indicator and strategy engines

**Files:** `backend/engines/indicators.py`, `backend/engines/strategies.py`, `backend/engines/router.py`

Current strategies:

- Trend Follow.
- Support/Resistance Breakout.
- RSI Divergence.
- VWAP Bounce.
- ORB.

Current limitations:

- Indicator pipeline does not fully integrate EMA200, MACD, ATR, and ADX.
- RSI divergence uses a fixed earlier lookback instead of confirmed pivots.
- VWAP is rolling instead of session-anchored.
- ORB uses the first `1H` candle of the UTC day instead of London/NY session ranges.
- Router strength values are not normalized across strategies.
- A+/A/B+ execution grading is not implemented.

### Risk Engine

**File:** `backend/engines/risk.py`

Current checks:

- Executable Buy/Sell signal.
- Cooldown.
- Existing same-symbol position.
- Maximum open positions.

The current risk engine is not yet the complete authoritative gate. Equity exposure, ATR stop validation, total notional exposure, minimum RR, daily loss, consecutive-loss cooldown, leverage, margin mode, and correlation controls remain incomplete or distributed.

### Execution and final-fill verification

**Files:**

- `backend/execution_handoff.py`
- `backend/execution_handoff_safety_hotfix.py`
- `backend/execution_idempotency.py`
- `backend/execution_idempotency_race_fix.py`
- `backend/execution_idempotency_review_fix.py`
- `backend/agreement_execution_guard.py`
- `backend/engines/order_fill.py`

Implemented:

- Candidate claim before submission.
- Deterministic execution identity where required.
- Duplicate and concurrent claim protection.
- Agreement-required candidate blocking before setup queue insertion.
- Agreement-required cached candidate pruning before handoff.
- Agreement-required final order submit blocking before the Bybit request layer.
- Final `Filled` status verification.
- Positive executed-quantity requirement.
- Distinction between filled, partial, rejected, cancelled, timeout, and unresolved states.

Open issue:

- Full unattended Demo readiness still requires authoritative risk gate, restart-safe state, leverage/margin verification, and controlled trade-management evidence.

### Position management

Implemented in code paths:

- Initial SL/TP.
- Breakeven handling.
- Partial TP handling.
- Trailing-stop handling.
- Duplicate-management-action protection.

Verification still required:

- Live Demo TP1 partial close.
- Live Demo breakeven mutation.
- Live Demo trailing-stop mutation.
- Final close and reconciliation.
- Restart recovery while a position is open.

## Environment

```env
BYBIT_API_KEY=your_demo_api_key
BYBIT_API_SECRET=your_demo_api_secret
BYBIT_BASE_URL=https://api-demo.bybit.com
BYBIT_DEMO=true
ADMIN_TOKEN=replace-with-a-strong-random-token
CORS_ALLOWED_ORIGINS=
PUBLIC_BASE_URL=
TIMEZONE=Asia/Dhaka
```

Optional durable-state configuration:

```env
BOT_STATE_DB_PATH=/mounted/path/bot_state.sqlite3
```

When `BOT_STATE_DB_PATH` points to persistent storage, execution identity and runtime state can survive service replacement. Without persistent storage, local state must not be treated as restart-safe evidence.

Do not commit secrets. Do not use real-money API credentials.

## Local run

```powershell
Copy-Item backend/.env.example backend/.env
python backend/secure_server.py
```

Open `http://127.0.0.1:8787/`, enter the admin token, and keep the exchange configuration in Bybit Demo mode.

## Important authenticated status routes

```text
/api/bot/status
/api/durable-state/status
/api/workers/status
/api/workers/symbols
/api/workers/setups
/api/workers/execution
```

## Acceptance verdict

| Capability | Verdict |
|---|---|
| Demo market-data access | **PASS** |
| Scanner and worker scheduling | **PASS** |
| Strategy vote generation | **PASS with quality limitations** |
| Router decision | **PASS with architecture limitations** |
| Automatic Demo order execution | **PASS — observed once** |
| Agreement-symbol full-path guard | **PASS — implemented with focused regression tests** |
| Final-fill verification | **PASS** |
| Maximum three simultaneous positions | **Configured and runtime-enforced** |
| Authoritative risk gate | **FAIL — incomplete** |
| Advanced position-management live proof | **INCOMPLETE** |
| Restart-safe persistence | **DEPLOYMENT-DEPENDENT** |
| Real-money readiness | **STRICT NO-GO** |

## Definition of done

A task may be changed from `[ ]` to `[x]` only when all applicable evidence exists:

1. Implementation is committed.
2. Focused automated tests pass or are added for CI execution.
3. Integration or regression tests pass where the environment is available.
4. Demo runtime evidence is captured for runtime-sensitive behavior.
5. README status and checklist are updated.
6. The change is independently reviewed.

A merged PR or passing static inspection alone is not proof that the complete Demo trading workflow is correct.
