# Bybit Intraday Trading Bot — Backend

Backend for a **Bybit Demo-only** intraday trading system. Python owns scanning, signal preparation, risk and sizing. Node.js owns execution and the complete trade lifecycle. PostgreSQL and journal services remain enabled as support infrastructure.

> Real-money trading is unsupported. Both runtimes are locked to `https://api-demo.bybit.com`.

## Locked trading plan

**Status:** LOCKED  
**Date:** 08 August 2026  
**Day:** Saturday  
**Time:** 02:09 AM Bangladesh Time (UTC+6)

This plan must not be changed silently. Any later change requires verified evidence, a blocker, or explicit owner approval.

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
5M candle must close in trade direction / valid confirmation
        ↓
Signal Ready
        ↓
Authoritative Risk
        ↓
Sizing
        ↓
PostgreSQL Outbox / Journal support
        ↓
Node execution slot
        ↓
Bybit Demo order
        ↓
Full worker-managed trade lifecycle
        ↓
CLOSED
```

The scanner entry path is strictly **1H → 15M → 5M closed candle**. No 1D or 4H scanner dependency is part of the locked flow.

## Trading policy

| Area | Locked policy |
|---|---|
| Exchange | Bybit Demo only |
| Product | USDT linear derivatives |
| Margin mode | Isolated |
| Maximum leverage | 10x |
| Maximum active trades | 3 |
| Execution workers / slots | 3 |
| A+ risk | 1.00% |
| A risk | 1.00% |
| B+ | Rejected for automatic execution |
| Entry confirmation | Closed 5M candle must validate the trade direction/setup |
| Position size | Calculated from approved risk, stop distance and exchange rules |
| Fixed per-trade 25% margin gate | Not part of the locked trade-eligibility rule |
| Fixed combined 60% margin gate | Not part of the locked trade-eligibility rule |
| Fixed 40% free-margin gate | Not part of the locked trade-eligibility rule |
| Python order submission | Disabled |
| Node order execution | Enabled/controlled by operator runtime |
| Real-money use | Prohibited |

**10x is a maximum leverage capability, not a requirement to use full 10x exposure on every trade.** The execution path uses the approved 1% risk and stop distance to determine required notional and margin.

## Risk and sizing authority

Risk and Sizing are the trade-eligibility authorities.

- A+ and A both use **1% risk**.
- Sizing uses the approved risk, entry price, structural stop distance and Bybit quantity/instrument rules.
- Leverage may be used up to **10x Isolated** to reduce required margin for the approved notional.
- A trade must not be rejected only because journal or support persistence is temporarily degraded.

## PostgreSQL Outbox and Journal

PostgreSQL Outbox and Journal remain **ON**.

They are support infrastructure, not independent strategy/risk rejection gates.

If support persistence or journal sync has a temporary problem, the candidate must not be marked as a permanent strategy/risk rejection solely for that reason. Use operational states such as `WAIT`, `RETRY` or `DEGRADED` and preserve reconciliation data where available.

Journal data may be reconciled from Bybit Demo order, fill, position and closed-PnL truth.

## Node execution workers

There are **3 execution slots/workers**, matching the maximum of **3 active trades**.

Each worker owns one trade from entry through final close:

```text
ENTRY DECISION
  → ORDER SUBMIT
  → FILL CONFIRMATION
  → INITIAL PROTECTION
  → ACTIVE MANAGEMENT
  → PARTIAL TP / BREAK-EVEN / TRAILING / PROTECTION
  → FULL CLOSE
  → FINAL RECONCILIATION
  → SLOT AVAILABLE
```

The worker manages the **full trade lifecycle**, not only the close.

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

Unknown submission outcomes must be reconciled and must not be blindly resubmitted.

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
- position sizing;
- API, diagnostics and runtime status.

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
- stable slot ownership;
- final exchange/account/instrument validation;
- Isolated leverage up to 10x;
- deterministic order identity;
- Bybit Demo order submission;
- fill verification;
- protection placement;
- complete trade management from entry through close;
- reconciliation after restart or exchange-side state changes.

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

## Acceptance target

A deployment is accepted only when:

1. The scanner follows **1H → 15M → closed 5M** only.
2. Valid A+/A signals use **1% risk**.
3. Sizing produces a valid exchange quantity from approved risk and structural stop distance.
4. Up to three Node workers can independently own and manage three active trades.
5. A worker owns its trade from entry through final close.
6. Journal/Outbox support faults do not become false strategy/risk rejections.
7. One complete Bybit Demo lifecycle reaches `CLOSED` without duplicate submission.

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
