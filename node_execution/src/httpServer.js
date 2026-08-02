import http from 'node:http';

function json(response, statusCode, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
  });
  response.end(body);
}

export function createHealthServer(runtime, port) {
  const server = http.createServer((request, response) => {
    const path = new URL(request.url || '/', 'http://localhost').pathname;
    const snapshot = {
      service: 'bybit-demo-node-execution',
      version: 'step10',
      enabled: runtime.enabled,
      ready: runtime.ready,
      leader: runtime.leader,
      databaseReady: runtime.databaseReady,
      migrationVersion: runtime.migrationVersion,
      startedAt: runtime.startedAt,
      slots: runtime.slots,
      lastError: runtime.lastError,
      now: new Date().toISOString(),
    };
    if (path === '/healthz') return json(response, 200, { ok: true, ...snapshot });
    if (path === '/readyz') return json(response, runtime.ready ? 200 : 503, { ok: runtime.ready, ...snapshot });
    if (path === '/') return json(response, 200, snapshot);
    return json(response, 404, { ok: false, error: 'Not found' });
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '0.0.0.0', () => resolve(server));
  });
}
