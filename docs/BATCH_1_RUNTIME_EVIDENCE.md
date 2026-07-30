# Batch 1 Runtime Evidence

Date: 25 July 2026
Environment: Render production service using the merged PR #22 commit `f8648582f15b5f0bb5225dfcd9b792c2d1758ec9`

## Verified gates

- [x] Focused Batch 1 regression tests passed
- [x] Existing Kill Switch verification passed
- [x] PR #22 merged to `main`
- [x] Render auto-deployment completed and service reported `Live`
- [x] Valid `BTCUSDT` manual connection test returned `manual_order BTCUSDT Buy: OK`
- [x] Invalid `INVALIDUSDT` connection test was blocked locally
- [x] Invalid-symbol request remained recorded as `INVALIDUSDT`; no evidence of fallback to BTCUSDT, ETHUSDT, or SOLUSDT
- [x] Extreme risk values were constrained/rejected by the server-side Batch 1 limits

## Pending acceptance gate

- [ ] Daily-risk fail-closed deployed-runtime proof

The code regression test proves that an unavailable closed-PnL result blocks execution. A controlled deployed-runtime observation is intentionally deferred until the end-of-day risk check. Batch 1 must not be marked fully complete until this evidence is recorded.

## Current conclusion

Batch 1 implementation, CI, merge, deployment, exact-symbol connection testing, invalid-symbol blocking, and server-side risk-limit behavior are verified. Final Batch 1 acceptance remains pending only for the deployed daily-risk fail-closed check and the corresponding README status update.
