import http from 'node:http';

function json(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  });
  response.end(body);
}

function slotSafety(slots = {}) {
  const values = Object.entries(slots);
  const active = values.filter(([, slot]) => slot?.candidateKey);
  const candidateKeys = active.map(([, slot]) => String(slot.candidateKey));
  return {
    configuredSlots: values.length,
    activeSlots: active.length,
    uniqueActiveCandidates: new Set(candidateKeys).size,
    duplicateCandidateDetected: new Set(candidateKeys).size !== candidateKeys.length,
    validSlotCount: values.length === 3,
  };
}

export function readinessSnapshot(runtime) {
  const slots = slotSafety(runtime.slots);
  const serviceReady = Boolean(runtime.databaseReady) && Number(runtime.migrationVersion || 0) >= 5 && slots.validSlotCount && !slots.duplicateCandidateDetected;
  const executionReady = serviceReady && Boolean(runtime.enabled) && Boolean(runtime.ready) && Boolean(runtime.leader);
  return { serviceReady, executionReady, slots };
}

export function createHealthServer(runtime, port) {
  const server = http.createServer((request, response) => {
    const path = new URL(request.url || '/', 'http://localhost').pathname;
    const readiness = readinessSnapshot(runtime);
    const snapshot = {
      service: 'bybit-demo-node-execution',
      version: 'step11-phase6',
      demoOnly: true,
      enabled: runtime.enabled,
      ready: runtime.ready,
      leader: runtime.leader,
      databaseReady: runtime.databaseReady,
      migrationVersion: runtime.migrationVersion,
      serviceReady: readiness.serviceReady,
      executionReady: readiness.executionReady,
      startedAt: runtime.startedAt,
      slots: runtime.slots,
      slotSafety: readiness.slots,
      lastError: runtime.lastError,
      now: new Date().toISOString(),
    };
    if (path === '/healthz') return json(response, 200, { ok: true, ...snapshot });
    if (path === '/readyz') return json(response, readiness.serviceReady ? 200 : 503, { ok: readiness.serviceReady, ...snapshot });
    if (path === '/') return json(response, 200, snapshot);
    return json(response, 404, { ok: false, error: 'Not found' });
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '0.0.0.0', () => resolve(server));
  });
}
