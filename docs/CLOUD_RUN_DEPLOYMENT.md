# Cloud Run Deployment

This backend supports **Bybit Demo only**. A deployment must start paused and must never use the Bybit live API endpoint.

## Runtime contract

- Container command: `python -m backend.cloud_run_server`
- Container port: Cloud Run-provided `PORT` (default `8080`)
- Bind host: `0.0.0.0`
- Startup execution state: `BOT_EXECUTION_ENABLED=false`
- Persistent state: PostgreSQL only
- Automatic worker ownership: one PostgreSQL advisory-lock leader
- Liveness: `GET /healthz`
- Readiness: `GET /readyz`
- Legacy-compatible health: `GET /api/health`

## Required secrets

Create these values in Google Secret Manager and pin a specific secret version during deployment:

- `ADMIN_TOKEN` — random value of at least 32 characters
- `BYBIT_API_KEY` — Bybit Demo API key
- `BYBIT_API_SECRET` — Bybit Demo API secret
- `DATABASE_URL` — PostgreSQL connection URL

Never commit the values to GitHub, `.env`, Docker build arguments, Cloud Build substitutions, or frontend variables.

## Build and push

Set the deployment variables in Cloud Shell:

```bash
export PROJECT_ID="your-project-id"
export REGION="asia-south1"
export SERVICE="bybit-intraday-bot-backend"
export REPOSITORY="bybit-bot"
export IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/backend:$(git rev-parse --short HEAD)"

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com secretmanager.googleapis.com

gcloud artifacts repositories describe "$REPOSITORY" --location "$REGION" >/dev/null 2>&1 || \
  gcloud artifacts repositories create "$REPOSITORY" \
    --repository-format=docker \
    --location="$REGION"

gcloud builds submit \
  --config cloudbuild.yaml \
  --substitutions="_IMAGE=${IMAGE}" \
  .
```

## Deploy a paused revision

Replace the frontend origin and secret names before running:

```bash
export FRONTEND_ORIGIN="https://your-vercel-app.vercel.app"

gcloud run deploy "$SERVICE" \
  --image="$IMAGE" \
  --region="$REGION" \
  --platform=managed \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=1Gi \
  --no-cpu-throttling \
  --min-instances=1 \
  --max-instances=2 \
  --concurrency=20 \
  --timeout=300 \
  --set-env-vars="HOST=0.0.0.0,BYBIT_BASE_URL=https://api-demo.bybit.com,BOT_EXECUTION_ENABLED=false,TIMEZONE=Asia/Dhaka,CORS_ALLOWED_ORIGINS=${FRONTEND_ORIGIN}" \
  --update-secrets="ADMIN_TOKEN=bybit-admin-token:1,BYBIT_API_KEY=bybit-demo-api-key:1,BYBIT_API_SECRET=bybit-demo-api-secret:1,DATABASE_URL=bybit-database-url:1" \
  --startup-probe="httpGet.path=/healthz,initialDelaySeconds=0,timeoutSeconds=3,periodSeconds=5,failureThreshold=24" \
  --liveness-probe="httpGet.path=/healthz,initialDelaySeconds=30,timeoutSeconds=3,periodSeconds=30,failureThreshold=3"
```

The service may have two API instances, but PostgreSQL advisory locking permits only one automatic-execution leader. A standby instance remains able to serve API reads while rejecting privileged execution mutations.

## Set the public backend URL

```bash
export SERVICE_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"

gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --update-env-vars="PUBLIC_BASE_URL=${SERVICE_URL}"
```

## Deployment acceptance

Do not start the bot until all checks pass:

```bash
curl --fail --silent --show-error "${SERVICE_URL}/healthz"
curl --fail --silent --show-error "${SERVICE_URL}/readyz"
```

Expected readiness properties:

- `ok: true`
- `demoOnly: true`
- `durableState.ok: true`
- `durableState.degraded: false`
- `runtimeLeadership.status` is `leader` or `standby`
- `environment.config.bybitBaseUrl` is `https://api-demo.bybit.com`
- `environment.config.startupExecutionEnabled` is `false`

Then verify an authenticated read without exposing the token in shell history:

```bash
read -s ADMIN_TOKEN_VALUE
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${ADMIN_TOKEN_VALUE}" \
  "${SERVICE_URL}/api/workers/status"
unset ADMIN_TOKEN_VALUE
```

## Rollback

List revisions and move all traffic back to the last verified revision:

```bash
gcloud run revisions list --service="$SERVICE" --region="$REGION"
gcloud run services update-traffic "$SERVICE" --region="$REGION" --to-revisions="VERIFIED_REVISION=100"
```

A rollback does not authorize execution. Confirm `/readyz`, authenticated worker status, durable-state reconciliation, and paused bot state after every revision change.
