# Direct Node sizing and Top50 canonical pipeline

The canonical automatic path is:

`Eligible Bybit USDT linear contracts -> closed 1H Top50 -> closed 15M classification -> later fully closed 5M confirmation -> Entry Safety -> authenticated Node handoff -> Node live technical plan and sizing -> Node execution -> Node trade management`

## Locked execution policy

- All six strategy engines remain active: Trend Follow, S/R Breakout, RSI Divergence, VWAP Bounce, Liquidity Sweep, and ORB.
- A+ and A are execution eligible; B+ is watch-only.
- Maximum planned stop risk is 1.00% of current equity per trade.
- Maximum three active trades.
- Bybit Demo USDT-linear only.
- Isolated leverage is capped at 10x and is a margin-capacity constraint only. It never multiplies the 1% risk budget.
- Node derives/validates the structural stop from fully closed 15M history immediately before sizing and requires at least 2R gross reward/risk.
- Existing Node trade management remains TP1 40% at 1.5R then break-even, TP2 30% at 2R, and a final 30% runner with a 0.5R trail.
- Python never submits a Bybit order.

## Support systems

Python `position_sizing_margin.py` remains available for diagnostics/audit. A diagnostic WAIT or DEGRADED state does not change Entry Safety approval and does not block direct Node delivery.

PostgreSQL remains available for execution-command compatibility, orders, fills, journal/state, reconciliation, and restart recovery. It is not the mandatory Python-to-Node transport. Healthy in-process direct candidates and managed trades can continue when PostgreSQL support is degraded. A cold Node restart with PostgreSQL unavailable and exchange positions/orders whose ownership cannot be reconstructed enters `DEGRADED_RECOVERY / RECONCILIATION_REQUIRED` and does not open additional trades until reconciliation is safe.

## Environment

Python/backend direct-delivery settings:

- `NODE_EXECUTION_URL`: internal URL of the Node execution service.
- `NODE_HANDOFF_TOKEN`: shared internal bearer secret. Store it in Secret Manager; do not commit it.
- `NODE_HANDOFF_TIMEOUT_SECONDS`: optional HTTP timeout, default 5 seconds.
- `HOURLY_WATCHLIST_SIZE`: defaults to 50.

Node settings:

- `NODE_HANDOFF_TOKEN`: same shared handoff secret.
- `NODE_STRUCTURE_LOOKBACK_15M`: closed-15M structure lookback, default 12.
- `DATABASE_URL`: support/reconciliation database connection. Direct intake does not require it to be healthy, but cold-start orphan safety remains fail-closed.

No database migration is required for this architecture change; the existing execution-command/order/fill/runtime-state schema is retained.
