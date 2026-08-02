# Bybit Intraday Trading Bot — Backend

Production-oriented backend for a **Bybit Demo-only** intraday trading system. Python owns market scanning, setup confirmation, risk and sizing. A separate Node.js service owns order submission, fill verification and trade management. PostgreSQL is the shared durable source of truth.

> Real-money trading is unsupported. Both Python and Node runtimes are locked to `https://api-demo.bybit.com`. Production Bybit endpoints are prohibited.

## Current project status

**Verified implementation progress: 11 / 12 steps complete.**

| Step | Scope | Status |
|---:|---|---|
| 1 | Existing backend audit and architecture lock | Complete |
| 2 | Daily full universe → Top 100 | Complete |
| 3 | 4H directional shortlist → Top 50 | Complete |
| 4 | 1H watchlist → Top 20 | Complete |
| 5 | Closed 15M setup classification | Complete |
| 6 | Closed 5M entry confirmation | Complete |
| 7 | Authoritative risk approval | Complete |
| 8 | Position sizing and margin validation | Complete |
| 9 | PostgreSQL Python → Node execution command contract | Complete |
| 10 | Bybit Demo Node execution service | Complete |
| 11 | Restart-safe Node trade management workers | Complete |
| 12 | End-to-end integration, frontend reconciliation and controlled Demo verification | Pending |

Latest merged milestones:

- Step 10 merge commit: `af157520cf4360042bac50b3b96642fd588ce41f`
- Step 11 merge commit: `810f74f0aea9108cecbd7c2a1b9d77056f33daba`

## Safety and operating policy

| Area | Locked policy |
|---|---|
| Exchange | Bybit Demo only |
| Product | USDT linear derivatives |
| Margin mode | Isolated |
| Leverage | 5x |
| Maximum active trades | 3 persistent slots |
| A+ risk | 1.00% |
| A risk | 0.75% |
| B+ | Rejected for automatic execution |
| Per-trade margin cap | 25% of equity |
| Combined margin cap | 60% of equity |
| Minimum free reserve | 40% of equity |
| Minimum gross risk-reward | 1:2 |
| Python order submission | Disabled |
| Node execution default | Disabled / fail-closed |
| Real-money use | Prohibited |

## Canonical end-to-end pipeline

```text
Daily full Bybit universe
        ↓
Daily Top 100
        ↓
4H directional Top 50
        ↓
1H watchlist Top 20
        ↓
Closed 15M setup classification
        ↓
Closed 5M confirmation
        ↓
Authoritative risk approval
        ↓
Position sizing + margin validation
        ↓
PostgreSQL execution_commands outbox
        ↓
Node slot claim: AVAILABLE → RESERVED
        ↓
Final wallet/instrument/price/risk revalidation
        ↓
Bybit Demo order submission
        ↓
ORDER_PENDING → PARTIALLY_FILLED / MANAGING / FAILED
        ↓
Node trade management
        ↓
CLOSING → CLOSED
```

The old router summary is not the authoritative execution pipeline.

## Python runtime

Cloud Run and production-like Python API execution must use:

```bash
python -m backend.cloud_run_server
```

Container definition:

```text
Dockerfile.cloudrun
```

Python responsibilities:

- scanner hierarchy;
- closed-candle strategy evaluation;
- authoritative risk checks;
- sizing and margin approval;
- immutable execution-command publication;
- API, diagnostics, journal and runtime status.

Python must not submit, modify or close exchange orders after a candidate enters the Node execution contract.

## Node execution runtime

Node service location:

```text
node_execution/
```

Container definition:

```text
node_execution/Dockerfile
```

Run locally:

```bash
cd node_execution
npm install
npm run check
npm test
npm start
```

Node responsibilities:

- exactly three persistent execution slots;
- stable ownership using `NODE_EXECUTION_OWNER_ID`;
- PostgreSQL advisory leadership;
- restart recovery of `RESERVED`, `ORDER_PENDING`, `PARTIALLY_FILLED`, `MANAGING` and `CLOSING` commands;
- final wallet, instrument, mark-price, quantity, risk and margin revalidation;
- Isolated 5x verification;
- deterministic `orderLinkId`;
- Bybit Demo order submission;
- order/fill/protection evidence persistence;
- partial exits, break-even and runner trailing;
- manual or exchange-side close reconciliation.

## Execution command state machine

```text
AVAILABLE
  → RESERVED
  → ORDER_PENDING
  → PARTIALLY_FILLED
  → MANAGING
  → CLOSING
  → CLOSED
```

Failure transitions are allowed only from the approved contract graph. `CLOSED` and `FAILED` are terminal states. Unknown submission outcomes remain `ORDER_PENDING`; the system must not blindly resubmit.

## Approved trade-management policy

```text
TP1: 1.5R → close 40%
After TP1: move stop to break-even
TP2: 2.0R → close 30%
Runner: remaining 30%
Runner trailing distance: 0.5R
```

Management rules:

- reduce-only partial-close orders;
- deterministic management order identities;
- exchange evidence checked before submission;
- trailing stop may tighten only;
- no averaging down;
- restart must resume the existing management stage;
- fills and management actions must persist in PostgreSQL.

## PostgreSQL durability

PostgreSQL is mandatory for automatic execution. The current central migration set includes the `execution_commands` contract and existing order/fill/runtime ledgers.

Required durability conditions:

- migration version is current;
- `execution_commands` exists;
- runtime state is not degraded;
- claims and transitions are atomic;
- order and fill evidence survives restart;
- only one Node execution leader is active.

The application must remain fail-closed when PostgreSQL is unavailable or degraded.

## Health endpoints

### Python API service

| Endpoint | Authentication | Purpose |
|---|---:|---|
| `GET /healthz` | Public | Process liveness |
| `GET /readyz` | Public | Environment, PostgreSQL and runtime readiness |
| `GET /api/health` | Public | Compatibility liveness endpoint |
| `GET /api/runtime/leadership` | Bearer token | Leader and worker diagnostics |
| `GET /api/durable-state/status` | Bearer token | Persistent-state diagnostics |
| `GET /api/workers/status` | Bearer token | Worker and execution status |

### Node execution service

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Container liveness |
| `GET /readyz` | Fail-closed execution readiness |
| `GET /` | Runtime summary without secrets |

## Required environment

Python API service:

```text
ADMIN_TOKEN
BYBIT_API_KEY
BYBIT_API_SECRET
DATABASE_URL
CORS_ALLOWED_ORIGINS
HOST=0.0.0.0
BYBIT_BASE_URL=https://api-demo.bybit.com
BOT_EXECUTION_ENABLED=false
TIMEZONE=Asia/Dhaka
```

Node execution service:

```text
DATABASE_URL
BYBIT_API_KEY
BYBIT_API_SECRET
NODE_EXECUTION_OWNER_ID
BYBIT_BASE_URL=https://api-demo.bybit.com
NODE_EXECUTION_ENABLED=false
PORT=8080
```

Production secrets must come from Google Secret Manager or an equivalent secret store. Never commit `.env` files, API keys, administrator tokens or database credentials.

## Build and test

Python backend:

```bash
docker build -f Dockerfile.cloudrun -t bybit-backend:local .
python -m pip install -r requirements.txt pytest==9.1.1
python -m compileall -q backend tests
python -m pytest -q --tb=short
```

Node execution service:

```bash
cd node_execution
npm install --ignore-scripts --no-audit --no-fund
npm run check
npm test
cd ..
docker build -f node_execution/Dockerfile -t bybit-node-execution:local node_execution
```

Current verified Node evidence after Step 11:

```text
Node tests: 13 / 13 passed
Syntax checks: passed
Demo-only and fail-closed boundary check: passed
Cloud Run Docker build: passed
Running container /healthz smoke test: passed
```

## Cloud Run topology

The final deployment requires two services sharing the same PostgreSQL database:

```text
Vercel frontend
      ↓
Python Cloud Run API
      ↓
PostgreSQL execution outbox and ledgers
      ↑
Node Cloud Run execution/management service
      ↓
Bybit Demo API
```

Recommended Node service deployment constraints:

- one maximum instance;
- stable `NODE_EXECUTION_OWNER_ID`;
- always-allocated CPU for persistent workers;
- Secret Manager-backed environment variables;
- `NODE_EXECUTION_ENABLED=false` during initial deployment;
- public health only; no secret-bearing responses.

## Frontend reconciliation blockers found before Step 12

Screenshots from the current Vercel frontend show that backend connectivity and wallet data are live, but the UI still represents parts of the legacy execution model.

The following must be corrected in Step 12:

1. **Durable storage is displayed as `DEGRADED`.** PostgreSQL readiness and persistence truth must be reconciled before acceptance.
2. **Lifecycle cards display `LONG 10x`.** The approved backend policy is Isolated 5x.
3. **Invalid zero-value events display `PASS`.** Entries with signal `0%`, notional `$0`, SL `0`, TP `0` or leverage `0x` must never be rendered as successful execution.
4. **Legacy router labels remain visible.** Labels such as `balanced router` and `liquid_intraday_top_movers` must not be presented as the authoritative execution path.
5. **The UI omits Steps 8–11.** It must display sizing approval, PostgreSQL outbox state, Node slot state, fill state and management state.
6. **Manual-control guidance is outdated.** The frontend must reflect the merged backend-managed lifecycle and expose controls only where verified endpoints exist.
7. **Frontend execution truth must come from canonical backend state.** Scanner summaries or legacy journal events must not be promoted to order-success truth.

Until these items are resolved, frontend `PASS` badges must not be treated as evidence of a real successful trade lifecycle.

## Step 12 acceptance plan

Step 12 is the final integration and verification phase. It must complete all of the following:

1. Reconcile frontend API contracts with the merged Python and Node runtimes.
2. Verify PostgreSQL migrations, readiness and restart persistence.
3. Deploy the Node execution container to Cloud Run with execution disabled.
4. Verify Python and Node `/healthz` and `/readyz` responses.
5. Verify exactly one Node execution leader and three persistent slots.
6. Confirm the frontend displays Isolated 5x and canonical pipeline states.
7. Block zero-value or legacy events from showing execution `PASS`.
8. Enable execution only for a controlled Bybit Demo test.
9. Run one complete lifecycle:

```text
scan → setup → confirmation → risk → sizing → outbox → claim
→ order → fill → protection → TP/BE/trailing or controlled close → CLOSED
```

10. Restart the Node service during a controlled lifecycle and confirm recovery without duplicate submission.
11. Verify order, fill, protection and management evidence in PostgreSQL.
12. Disable execution again and publish the final readiness report.

## Deployment acceptance

A deployment is not accepted until all conditions hold:

1. Python and Node CI are green.
2. Both containers build successfully.
3. Python and Node `/healthz` return HTTP `200`.
4. Python and Node `/readyz` return HTTP `200` when enabled prerequisites are healthy.
5. Durable state is healthy and not degraded.
6. PostgreSQL migration and execution-command contract are current.
7. Exactly one Node leader owns all execution slots.
8. The frontend matches canonical backend truth.
9. No zero-value lifecycle event is labelled `PASS`.
10. Isolated 5x is verified before order submission.
11. One controlled Bybit Demo lifecycle completes without duplicate order or close actions.
12. Execution is returned to disabled state after acceptance testing.

## Security model

- Privileged Python API operations require `Authorization: Bearer <ADMIN_TOKEN>`.
- CORS is same-origin or explicit allowlist only.
- Demo endpoint policy blocks non-Demo Bybit hosts.
- PostgreSQL is the only persistent automatic-execution store.
- Python and Node execution are disabled by default.
- Deterministic order identities prevent blind duplicate submission.
- Unknown exchange outcomes remain pending for reconciliation.
- Leader loss or PostgreSQL degradation disables execution.
- SIGTERM stops workers and releases leadership safely.
- Health/readiness responses contain no secret values.

## Remaining work

Only **Step 12** remains. The project is not yet approved for unattended operation. Final approval requires frontend reconciliation, deployment validation and one controlled end-to-end Bybit Demo lifecycle with restart-recovery evidence.

## Repository and deployed services

Backend repository:

```text
zahirulca24-bit/Bybit-Intraday-Trading-Bot-Backend
```

Frontend repository:

```text
zahirulca24-bit/Bybit-Intraday-Trading-Bot-Frontend
```

Current services:

```text
Frontend: https://bybit-intraday-trading-bot-frontend-liard.vercel.app
Python backend: https://bybit-intraday-backend-608992045433.asia-south1.run.app
```
