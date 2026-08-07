# Bybit Intraday Trading Bot — Backend

Backend for a **Bybit Demo-only** intraday trading system. Python owns scanning, signal preparation, risk and sizing. Node.js owns order execution, fill verification and trade management. PostgreSQL is the shared durable source of truth.

> Real-money trading is unsupported. Both runtimes are locked to `https://api-demo.bybit.com`.

## Canonical pipeline

```text
Eligible Bybit USDT contracts
        ↓
Closed 1H watchlist → Top 20
        ↓
Closed 15M setup classification
        ↓
Closed 5M confirmation
        ↓
Signal Ready
        ↓
Authoritative risk approval
        ↓
Position sizing + margin validation
        ↓
PostgreSQL execution_commands outbox
        ↓
Node slot claim
        ↓
Final exchange/account validation
        ↓
Bybit Demo order
        ↓
Active trade management
        ↓
CLOSED
```

The scanner entry timeframe is **1H**. Setup confirmation uses **15M**, and entry confirmation uses **5M**.

## Trading policy

| Area | Policy |
|---|---|
| Exchange | Bybit Demo only |
| Product | USDT linear derivatives |
| Margin mode | Isolated |
| Leverage | 5x |
| Maximum active trades | 3 |
| A+ risk | 1.00% |
| A risk | 0.75% |
| B+ | Rejected for automatic execution |
| Per-trade margin cap | 25% of equity |
| Combined margin cap | 60% of equity |
| Minimum free reserve | 40% of equity |
| Minimum gross risk-reward | 1:2 |
| Python order submission | Disabled |
| Node order execution | Operator controlled |
| Real-money use | Prohibited |

## Python runtime

Run the Cloud Run API with:

```bash
python -m backend.cloud_run_server
```

Python responsibilities:

- eligible-market filtering;
- closed 1H watchlist generation;
- closed 15M setup classification;
- closed 5M confirmation;
- authoritative risk approval;
- position sizing and margin validation;
- immutable execution-command publication;
- API, diagnostics, journal and runtime status.

Python must not submit exchange orders.

## Node execution runtime

Location:

```text
node_execution/
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

- three persistent execution slots;
- stable ownership using `NODE_EXECUTION_OWNER_ID`;
- PostgreSQL advisory leadership;
- restart recovery;
- final wallet, instrument, mark-price, quantity, risk and margin validation;
- Isolated 5x verification;
- deterministic `orderLinkId`;
- Bybit Demo order submission;
- order/fill/protection evidence persistence;
- partial exits, break-even and runner trailing;
- manual or exchange-side close reconciliation.

## Execution state machine

```text
AVAILABLE
  → RESERVED
  → ORDER_PENDING
  → PARTIALLY_FILLED
  → MANAGING
  → CLOSING
  → CLOSED
```

`FAILED` is terminal. Unknown submission outcomes remain `ORDER_PENDING` for reconciliation and must not be blindly resubmitted.

## Trade management

```text
TP1: 1.5R → close 40%
After TP1: move stop to break-even
TP2: 2.0R → close 30%
Runner: remaining 30%
Runner trailing distance: 0.5R
```

## PostgreSQL durability

Automatic execution requires PostgreSQL. The execution command contract, ownership state, orders, fills and runtime state must survive restart.

Required conditions:

- current migrations;
- healthy durable state;
- atomic claims and transitions;
- one Node execution leader;
- persistent order/fill evidence.

## Health endpoints

### Python API

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Process liveness |
| `GET /readyz` | Runtime and dependency readiness |
| `GET /api/health` | Compatibility liveness |
| `GET /api/runtime/leadership` | Leader/worker diagnostics |
| `GET /api/durable-state/status` | Durable-state diagnostics |
| `GET /api/workers/status` | Worker/execution status |

### Node service

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Container liveness |
| `GET /readyz` | Execution readiness |
| `GET /` | Runtime summary |

## Environment

Python API:

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

Node execution:

```text
DATABASE_URL
BYBIT_API_KEY
BYBIT_API_SECRET
NODE_EXECUTION_OWNER_ID
BYBIT_BASE_URL=https://api-demo.bybit.com
NODE_EXECUTION_ENABLED=true
MAX_ACTIVE_TRADES=3
PORT=8080
```

Secrets must come from Google Secret Manager or an equivalent secret store. Never commit live API keys, administrator tokens, database credentials or `.env` secret files.

## Build and test

Python:

```bash
python -m compileall -q backend tests
python -m pytest -q --tb=short
```

Node:

```bash
cd node_execution
npm install --ignore-scripts --no-audit --no-fund
npm run check
npm test
```

## Deployment topology

```text
Frontend
   ↓
Python Cloud Run API
   ↓
PostgreSQL execution outbox and ledgers
   ↑
Node Cloud Run execution/management service
   ↓
Bybit Demo API
```

## Acceptance target

A deployment is accepted only when:

1. Python and Node CI are green.
2. Both services are healthy and ready.
3. PostgreSQL migrations and durable state are healthy.
4. Exactly one Node leader owns at most three execution slots.
5. The frontend displays canonical backend truth.
6. One complete Bybit Demo lifecycle reaches `CLOSED` without duplicate submission.

## Repository and services

```text
Backend: zahirulca24-bit/Bybit-Intraday-Trading-Bot-Backend
Frontend: zahirulca24-bit/Bybit-Intraday-Trading-Bot-Frontend
Frontend service: https://bybit-intraday-trading-bot-frontend-liard.vercel.app
Python backend: https://bybit-intraday-backend-608992045433.asia-south1.run.app
```

## Audit item — turnover authority

The active market-filter paths must use one configurable turnover authority. The intended configuration key is:

```text
MIN_TURNOVER_24H
```

Scanner, worker and server paths must not maintain conflicting independent thresholds.
