function required(env, name) {
  const value = String(env[name] ?? '').trim();
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function boundedNumber(env, name, fallback, min, max) {
  const parsed = Number(env[name] ?? fallback);
  const value = Number.isFinite(parsed) ? parsed : fallback;
  return Math.max(min, Math.min(max, value));
}

export function loadConfig(env = process.env) {
  const baseUrl = String(env.BYBIT_BASE_URL || 'https://api-demo.bybit.com').replace(/\/$/, '');
  if (baseUrl !== 'https://api-demo.bybit.com') {
    throw new Error('Step 10 is locked to Bybit Demo: https://api-demo.bybit.com');
  }
  const ownerId = required(env, 'NODE_EXECUTION_OWNER_ID');
  if (!/^[A-Za-z0-9._:-]{3,80}$/.test(ownerId)) {
    throw new Error('NODE_EXECUTION_OWNER_ID must be a stable 3-80 character identifier');
  }
  const enabled = String(env.NODE_EXECUTION_ENABLED || 'false').toLowerCase() === 'true';
  const firstDemoTradeArmed = String(env.FIRST_DEMO_TRADE_ARMED || 'false').toLowerCase() === 'true';
  const maxActiveTrades = Math.trunc(boundedNumber(env, 'FIRST_DEMO_MAX_ACTIVE_TRADES', 1, 1, 1));
  const riskPerTradePct = boundedNumber(env, 'FIRST_DEMO_RISK_PER_TRADE_PCT', 0.25, 0.25, 0.25);
  return Object.freeze({
    databaseUrl: required(env, 'DATABASE_URL'),
    apiKey: required(env, 'BYBIT_API_KEY'),
    apiSecret: required(env, 'BYBIT_API_SECRET'),
    baseUrl,
    ownerId,
    enabled,
    firstDemoTradeArmed,
    maxActiveTrades,
    riskPerTradePct,
    port: Math.trunc(boundedNumber(env, 'PORT', 8080, 1, 65535)),
    pollMs: Math.trunc(boundedNumber(env, 'NODE_EXECUTION_POLL_MS', 3000, 500, 60000)),
    fillPollMs: Math.trunc(boundedNumber(env, 'NODE_FILL_POLL_MS', 1000, 250, 10000)),
    fillTimeoutMs: Math.trunc(boundedNumber(env, 'NODE_FILL_TIMEOUT_MS', 30000, 5000, 120000)),
    requestTimeoutMs: Math.trunc(boundedNumber(env, 'BYBIT_REQUEST_TIMEOUT_MS', 15000, 3000, 60000)),
    recvWindow: Math.trunc(boundedNumber(env, 'BYBIT_RECV_WINDOW', 20000, 1000, 60000)),
    maxCandidateAgeSeconds: boundedNumber(env, 'EXECUTION_CANDIDATE_MAX_AGE_SECONDS', 1200, 60, 3600),
    maxEntryDriftPct: boundedNumber(env, 'EXECUTION_MAX_ENTRY_DRIFT_PCT', 0.50, 0.05, 3.0),
    minimumGrossRr: 2.0,
    leverage: 5,
    marginMode: 'ISOLATED_MARGIN',
    category: 'linear',
    settleCoin: 'USDT',
    slotCount: 3,
  });
}
