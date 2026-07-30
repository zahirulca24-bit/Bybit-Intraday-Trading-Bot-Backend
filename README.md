# Bybit Intraday Trading Bot — Backend

Production-oriented Python backend for a **Bybit Demo-only** intraday trading bot. The runtime provides authenticated bot controls, market scanning, execution safety gates, PostgreSQL-backed durable state, worker orchestration, replay analytics, and Cloud Run lifecycle protection.

> Real-money trading is unsupported. `BYBIT_BASE_URL` is validated against `https://api-demo.bybit.com`, and startup is rejected if a live endpoint is configured.

## Current status

| Area | Status |
|---|---|
| Exchange environment | Bybit Demo only |
| Cloud Run container | Implemented |
| PostgreSQL durable state | Required |
| Single execution leader | PostgreSQL advisory lock |
| Graceful shutdown | SIGTERM/SIGINT supported |
| Startup execution state | Paused / fail-closed |
| Full backend CI | PostgreSQL-backed full suite |
| Real-money use | Prohibited |

## Canonical runtime

Cloud Run and production-like execution must use:

```bash
python -m backend.cloud_run_server
```

Local legacy entrypoints are not the Cloud Run deployment contract. The container definition is `Dockerfile.cloudrun`.

## Runtime architecture

```text
Cloud Run HTTP API instances
        │
        ├── Public liveness/readiness
        ├── Authenticated control and diagnostics
        └── PostgreSQL advisory-lock election
                         │
                 One execution leader
                         │
                Symbol selection worker
                         │
                Setup verification worker
                         │
               Authoritative safety gates
                         │
                  Execution handoff
                         │
                  Bybit Demo API
                         │
             Fill/protection verification
                         │
               PostgreSQL journal/state
```

Multiple API instances may serve requests, but only the advisory-lock leader may run automatic execution. Standby instances reject execution mutations.

## Health endpoints

| Endpoint | Authentication | Purpose |
|---|---:|---|
| `GET /healthz` | Public | Process liveness |
| `GET /readyz` | Public | Environment, PostgreSQL and runtime readiness |
| `GET /api/health` | Public | Compatibility liveness endpoint |
| `GET /api/runtime/leadership` | Bearer token | Leader and worker diagnostics |
| `GET /api/durable-state/status` | Bearer token | Persistent-state diagnostics |
| `GET /api/workers/status` | Bearer token | Worker and execution status |

Health responses contain no secret values.

## Required environment

Use `.env.example` only as a key-name reference. Production secrets must come from Google Secret Manager or an equivalent secret store.

Required:

```text
ADMIN_TOKEN
BYBIT_API_KEY
BYBIT_API_SECRET
DATABASE_URL
CORS_ALLOWED_ORIGINS
```

Locked or expected values:

```text
HOST=0.0.0.0
BYBIT_BASE_URL=https://api-demo.bybit.com
BOT_EXECUTION_ENABLED=false
TIMEZONE=Asia/Dhaka
```

Startup validation rejects:

- missing or placeholder secrets;
- short administrator tokens;
- non-PostgreSQL durable-state URLs;
- the Bybit live endpoint;
- wildcard or malformed CORS origins;
- non-HTTPS remote frontend origins;
- startup execution enablement;
- invalid `HOST` or `PORT` values.

## Build locally

```bash
docker build -f Dockerfile.cloudrun -t bybit-backend:local .
```

The image runs as a non-root user and excludes local `.env`, database, log, archive and test artifacts from the build context.

## Run locally with production checks

A reachable PostgreSQL database and non-placeholder Demo credentials are required:

```bash
docker run --rm -p 8080:8080 \
  -e ADMIN_TOKEN="replace-with-32-plus-random-characters" \
  -e BYBIT_API_KEY="your-demo-key" \
  -e BYBIT_API_SECRET="your-demo-secret" \
  -e DATABASE_URL="postgresql://user:password@host:5432/database" \
  -e BYBIT_BASE_URL="https://api-demo.bybit.com" \
  -e BOT_EXECUTION_ENABLED="false" \
  -e CORS_ALLOWED_ORIGINS="http://localhost:3000" \
  bybit-backend:local
```

Verify:

```bash
curl --fail http://localhost:8080/healthz
curl --fail http://localhost:8080/readyz
```

## Tests

```bash
python -m pip install -r requirements.txt pytest==9.1.1
python -m compileall -q backend tests
python -m pytest -q --tb=short
```

GitHub Actions runs the complete suite with PostgreSQL 16. Cloud Run-specific CI additionally compiles the entrypoint, runs focused runtime tests, validates the container contract, builds the image, starts it as a non-root user, and probes health/readiness.

## Cloud Run deployment

Use the controlled procedure in:

[`docs/CLOUD_RUN_DEPLOYMENT.md`](docs/CLOUD_RUN_DEPLOYMENT.md)

Build configuration:

- `Dockerfile.cloudrun`
- `cloudbuild.yaml`
- `.dockerignore`
- `.env.example`

Do not deploy from `render.yaml` and do not override the container command with a legacy server entrypoint.

## Security model

- Privileged API operations require `Authorization: Bearer <ADMIN_TOKEN>`.
- CORS is same-origin or explicit allowlist only.
- Demo endpoint policy blocks non-Demo Bybit hosts.
- Persistent execution claims and state use PostgreSQL only.
- Automatic execution is disabled at process boot.
- Order-capable follower instances return `503`.
- Lost leader ownership disables execution.
- SIGTERM stops workers and releases the advisory lock.
- Secrets are not returned by health/readiness responses.

## Deployment acceptance

A revision is not accepted until all conditions hold:

1. Container build and full backend CI are green.
2. `/healthz` returns HTTP `200`.
3. `/readyz` returns HTTP `200` with `ok: true`.
4. Durable state is healthy and not degraded.
5. Runtime leadership is `leader` or `standby`.
6. The bot remains paused after deployment.
7. Authenticated worker status matches the UI state.
8. No live Bybit endpoint or real credentials are configured.

## Remaining production work

Cloud Run readiness does not mean unattended trading approval. Remaining work includes independent Demo lifecycle verification, authoritative risk-policy review, exposure/correlation controls, full frontend-backend authorization acceptance, observability and controlled rollback testing.

## Repository

`zahirulca24-bit/Bybit-Intraday-Trading-Bot-Backend`
