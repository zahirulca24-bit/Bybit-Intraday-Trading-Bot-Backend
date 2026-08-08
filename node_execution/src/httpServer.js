import http from 'node:http';
import crypto from 'node:crypto';

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
  const recoveryRequired = Boolean(runtime.recoveryRequired);
  const serviceReady = slots.validSlotCount && !slots.duplicateCandidateDetected && !recoveryRequired;
  const executionReady = serviceReady && Boolean(runtime.enabled) && Boolean(runtime.ready) && Boolean(runtime.leader);
  return { serviceReady, executionReady, recoveryRequired, slots };
}

function bearerToken(request) {
  const authorization = String(request.headers.authorization || '');
  if (authorization.toLowerCase().startsWith('bearer ')) return authorization.slice(7).trim();
  return String(request.headers['x-node-handoff-token'] || '').trim();
}

function tokenMatches(provided, expected) {
  const left = Buffer.from(String(provided || ''));
  const right = Buffer.from(String(expected || ''));
  return left.length > 0 && left.length === right.length && crypto.timingSafeEqual(left, right);
}

async function readJson(request, maximumBytes = 64 * 1024) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > maximumBytes) throw new Error('Candidate payload is too large');
    chunks.push(chunk);
  }
  if (!chunks.length) throw new Error('JSON body is required');
  const parsed = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('Candidate payload must be a JSON object');
  return parsed;
}

function publicSnapshot(runtime) {
  const readiness = readinessSnapshot(runtime);
  return {
    service: 'bybit-demo-node-execution',
    version: 'direct-node-sizing-top50-v1',
    demoOnly: true,
    enabled: runtime.enabled,
    ready: runtime.ready,
    leader: runtime.leader,
    databaseReady: runtime.databaseReady,
    migrationVersion: runtime.migrationVersion,
    serviceReady: readiness.serviceReady,
    executionReady: readiness.executionReady,
    recoveryRequired: readiness.recoveryRequired,
    recoveryStatus: runtime.recoveryStatus || (readiness.recoveryRequired ? 'DEGRADED_RECOVERY' : 'READY'),
    startedAt: runtime.startedAt,
    slots: runtime.slots,
    slotSafety: readiness.slots,
    nodeHandoff: runtime.nodeHandoff || { status: 'WAIT', delivered: 0, retrying: 0, rejectedInvalid: 0 },
    nodeLiveSizing: runtime.nodeLiveSizing || { status: 'WAITING_FOR_CANDIDATE', code: 'WAITING_FOR_CANDIDATE' },
    nodeExecution: runtime.nodeExecution || { status: 'WAITING_FOR_CANDIDATE' },
    postgresSupport: runtime.postgresSupport || {
      status: runtime.databaseReady ? 'PASS' : 'DEGRADED',
      supportOnly: true,
      tradeRejectionAuthority: false,
    },
    lastError: runtime.lastError,
    now: new Date().toISOString(),
  };
}

export function createHealthServer(runtime, port, options = {}) {
  const server = http.createServer(async (request, response) => {
    const path = new URL(request.url || '/', 'http://localhost').pathname;
    const snapshot = publicSnapshot(runtime);

    if (path === '/internal/execution-candidate' && String(request.method || '').toUpperCase() === 'POST') {
      if (typeof options.intakeCandidate !== 'function') return json(response, 404, { ok: false, error: 'Not found' });
      const expected = String(options.handoffToken || '').trim();
      if (!expected) return json(response, 503, { ok: false, code: 'NODE_HANDOFF_NOT_CONFIGURED', error: 'Direct candidate handoff is not configured' });
      if (!tokenMatches(bearerToken(request), expected)) return json(response, 401, { ok: false, error: 'Unauthorized' });
      if (runtime.recoveryRequired) {
        return json(response, 409, {
          ok: false,
          code: 'RECONCILIATION_REQUIRED',
          reason: 'Node recovery requires ownership reconciliation before accepting new entries',
        });
      }
      try {
        const payload = await readJson(request);
        const result = await options.intakeCandidate(payload);
        return json(response, 202, {
          ok: true,
          code: result?.duplicate ? 'NODE_HANDOFF_DUPLICATE' : 'NODE_HANDOFF_ACCEPTED',
          reason: result?.duplicate ? 'Candidate already exists with identical immutable facts' : 'Candidate accepted by Node execution controller',
          duplicate: Boolean(result?.duplicate),
          candidateKey: result?.command?.candidateKey || payload.candidateKey || null,
          state: result?.command?.state || 'AVAILABLE',
        });
      } catch (error) {
        return json(response, 400, {
          ok: false,
          code: 'NODE_HANDOFF_INVALID',
          error: String(error?.message || error || 'Invalid candidate'),
        });
      }
    }

    if (path === '/healthz') return json(response, 200, { ok: true, ...snapshot });
    if (path === '/readyz') return json(response, snapshot.serviceReady ? 200 : 503, { ok: snapshot.serviceReady, ...snapshot });
    if (path === '/') return json(response, 200, snapshot);
    return json(response, 404, { ok: false, error: 'Not found' });
  });
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '0.0.0.0', () => resolve(server));
  });
}
