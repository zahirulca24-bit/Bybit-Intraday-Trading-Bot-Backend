export function loadConfig(env = process.env) {
  const required = (name) => {
    const value = String(env[name] || '').trim();
    if (!value) throw new Error(`${name} is required`);
    return value;
  };
  const baseUrl = String(env.BYBIT_BASE_URL || 'https://api-demo.bybit.com').replace(/\/$/, '');
  if (baseUrl !== 'https://api-demo.bybit.com') {
    throw new Error('Step 10 is locked to Bybit Demo: https://api-demo.bybit.com');
  }
  return {
    databaseUrl: required('DATABASE_URL'),
    apiKey: required('BYBIT_API_KEY'),
    apiSecret: required('BYBIT_API_SECRET'),
    baseUrl,
    enabled: String(env.NODE_EXECUTION_ENABLED || 'false').toLowerCase() === 'true',
    ownerPrefix: String(env.NODE_EXECUTION_OWNER_PREFIX || 'node-exec'),
    pollMs: Math.max(1000, Number(env.NODE_EXECUTION_POLL_MS || 3000)),
    fillPollMs: Math.max(500, Number(env.NODE_FILL_POLL_MS || 1000)),
    fillTimeoutMs: Math.max(5000, Number(env.NODE_FILL_TIMEOUT_MS || 30000)),
    recvWindow: Math.max(1000, Number(env.BYBIT_RECV_WINDOW || 5000)),
  };
}
