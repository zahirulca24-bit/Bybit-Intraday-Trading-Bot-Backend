import test from 'node:test';
import assert from 'node:assert/strict';
import { createHealthServer, readinessSnapshot } from '../src/httpServer.js';

function runtime(overrides = {}) {
  return {
    enabled: false,
    ready: false,
    leader: false,
    databaseReady: true,
    migrationVersion: 5,
    startedAt: new Date().toISOString(),
    lastError: 'Execution intentionally disabled',
    slots: {
      1: { state: 'IDLE', candidateKey: null, updatedAt: null },
      2: { state: 'IDLE', candidateKey: null, updatedAt: null },
      3: { state: 'IDLE', candidateKey: null, updatedAt: null },
    },
    ...overrides,
  };
}

test('healthy disabled service is deployment-ready but not execution-ready', () => {
  const result = readinessSnapshot(runtime());
  assert.equal(result.serviceReady, true);
  assert.equal(result.executionReady, false);
  assert.equal(result.slots.configuredSlots, 3);
});

test('execution readiness requires enabled leader runtime', () => {
  const result = readinessSnapshot(runtime({ enabled: true, ready: true, leader: true }));
  assert.equal(result.serviceReady, true);
  assert.equal(result.executionReady, true);
});

test('duplicate candidate across slots fails readiness', () => {
  const value = runtime();
  value.slots[1].candidateKey = 'candidate-1';
  value.slots[2].candidateKey = 'candidate-1';
  const result = readinessSnapshot(value);
  assert.equal(result.slots.duplicateCandidateDetected, true);
  assert.equal(result.serviceReady, false);
});

test('readyz returns 200 for safe disabled deployment', async () => {
  const server = await createHealthServer(runtime(), 0);
  try {
    const port = server.address().port;
    const response = await fetch(`http://127.0.0.1:${port}/readyz`);
    const payload = await response.json();
    assert.equal(response.status, 200);
    assert.equal(payload.serviceReady, true);
    assert.equal(payload.executionReady, false);
    assert.equal(payload.demoOnly, true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
