import test from 'node:test';
import assert from 'node:assert/strict';
import { orderLinkId } from '../src/bybitClient.js';
import { HybridExecutionRepository } from '../src/hybridRepository.js';
import { buildLiveExecutionPlan, ExecutionWaitError } from '../src/liveSizer.js';
import { validateCandidateFacts } from '../src/validator.js';
import { evaluateFirstTradeGate } from '../src/firstTradeGate.js';
import { createHealthServer } from '../src/httpServer.js';

const now = Math.floor(Date.now() / 1000);
const config = {
  maxCandidateAgeSeconds: 1200,
  maxEntryDriftPct: 0.5,
  minimumGrossRr: 2,
  structureLookback: 12,
  leverage: 10,
  maxActiveTrades: 3,
  marginMode: 'ISOLATED_MARGIN',
  category: 'linear',
  baseUrl: 'https://api-demo.bybit.com',
  enabled: true,
  gradeRiskPct: { 'A+': 1, A: 1 },
};

function candidate(key = 'direct-1', overrides = {}) {
  return {
    candidateKey: key,
    symbol: 'BTCUSDT',
    side: 'Buy',
    strategy: 'Trend Follow',
    grade: 'A+',
    gradeScore: 99,
    entryReference: 100,
    entryFiveMinuteCandleTime: now * 1000 - 300_000,
    setupFifteenMinuteCandleTime: now * 1000 - 900_000,
    createdAt: now,
    riskApproved: true,
    riskStatus: 'APPROVED_RISK',
    riskPerTradePct: 1,
    gradeRiskPct: 1,
    effectiveRiskPerTradePct: 1,
    qualified: true,
    executionStatus: 'AWAITING_NODE_EXECUTION',
    orderSubmitted: false,
    ...overrides,
  };
}

function klineRows({ low = 99, high = 101 } = {}) {
  const interval = 15 * 60 * 1000;
  const latestStart = (now * 1000) - interval - 1000;
  const list = [];
  for (let index = 0; index < 20; index += 1) {
    const start = latestStart - ((19 - index) * interval);
    list.push([String(start), '100', String(high), String(low), '100', '1000', '100000']);
  }
  return { result: { list: list.reverse() } };
}

function truth(overrides = {}) {
  return {
    wallet: { result: { list: [{ totalEquity: '1000', totalAvailableBalance: '800', totalInitialMargin: '100' }] } },
    instrument: { result: { list: [{
      status: 'Trading',
      lotSizeFilter: { minOrderQty: '0.001', maxMktOrderQty: '100', qtyStep: '0.001', minNotionalValue: '5' },
      leverageFilter: { maxLeverage: '100' },
      priceFilter: { tickSize: '0.01' },
    }] } },
    ticker: { result: { list: [{ markPrice: '100' }] } },
    kline15m: klineRows(),
    positions: { result: { list: [] } },
    activeOrders: { result: { list: [] } },
    pendingReservedMargin: 0,
    orderLinkId: orderLinkId('direct-1'),
    ...overrides,
  };
}

function assertWait(fn, code) {
  assert.throws(fn, (error) => error instanceof ExecutionWaitError && error.code === code);
}

test('direct Entry-Safety candidate is valid without Python qty or margin', () => {
  const input = candidate();
  assert.equal(input.qty, undefined);
  assert.equal(input.requiredInitialMarginUsdt, undefined);
  assert.equal(validateCandidateFacts(input, input.candidateKey), input);
});

test('Node live sizing fixes planned stop risk at at most one percent equity', () => {
  const plan = buildLiveExecutionPlan(candidate(), truth(), config, now);
  assert.equal(plan.riskBudgetUsdt, 10);
  assert.equal(plan.qty, '10');
  assert.equal(plan.plannedStopRiskUsdt, 10);
  assert.equal(plan.effectiveRiskPerTradePct, 1);
  assert.equal(plan.sizingAuthority, 'NODE_LIVE_BYBIT_TRUTH');
  assert.equal(plan.leverage, 10);
});

test('10x leverage caps margin capacity and never multiplies the risk budget', () => {
  const plan = buildLiveExecutionPlan(candidate(), truth({
    wallet: { result: { list: [{ totalEquity: '1000', totalAvailableBalance: '50', totalInitialMargin: '0' }] } },
  }), config, now);
  assert.equal(plan.riskBudgetUsdt, 10);
  assert.equal(plan.qty, '5');
  assert.equal(plan.plannedStopRiskUsdt, 5);
  assert.equal(plan.requiredInitialMarginUsdt, 50);
});

test('Node floors risk quantity to current qtyStep and respects market maximum', () => {
  const plan = buildLiveExecutionPlan(candidate(), truth({
    instrument: { result: { list: [{
      status: 'Trading',
      lotSizeFilter: { minOrderQty: '0.003', maxMktOrderQty: '2.004', qtyStep: '0.003', minNotionalValue: '5' },
      leverageFilter: { maxLeverage: '100' },
      priceFilter: { tickSize: '0.01' },
    }] } },
  }), config, now);
  assert.equal(plan.qty, '2.004');
  assert.ok(Number(plan.qty) <= 2.004);
  assert.ok(plan.plannedStopRiskUsdt <= plan.riskBudgetUsdt);
});

test('Node never increases quantity merely to satisfy minNotional or minimum quantity', () => {
  assertWait(() => buildLiveExecutionPlan(candidate(), truth({
    instrument: { result: { list: [{
      status: 'Trading',
      lotSizeFilter: { minOrderQty: '20', maxMktOrderQty: '100', qtyStep: '1', minNotionalValue: '5' },
      leverageFilter: { maxLeverage: '100' },
      priceFilter: { tickSize: '0.01' },
    }] } },
  }), config, now), 'INSUFFICIENT_MARGIN');

  assertWait(() => buildLiveExecutionPlan(candidate(), truth({
    instrument: { result: { list: [{
      status: 'Trading',
      lotSizeFilter: { minOrderQty: '0.001', maxMktOrderQty: '100', qtyStep: '0.001', minNotionalValue: '2000' },
      leverageFilter: { maxLeverage: '100' },
      priceFilter: { tickSize: '0.01' },
    }] } },
  }), config, now), 'INSUFFICIENT_MARGIN');
});

test('invalid closed-15M structural plan affects the candidate with a wait code', () => {
  assertWait(() => buildLiveExecutionPlan(candidate(), truth({
    kline15m: klineRows({ low: 100.5, high: 101 }),
  }), config, now), 'TECHNICAL_PLAN_WAIT');
});

test('same-symbol and maximum-three protections remain enforced', () => {
  assertWait(() => buildLiveExecutionPlan(candidate(), truth({
    positions: { result: { list: [{ symbol: 'BTCUSDT', side: 'Buy', size: '1' }] } },
  }), config, now), 'DUPLICATE_SYMBOL');

  assertWait(() => buildLiveExecutionPlan(candidate(), truth({
    positions: { result: { list: [
      { symbol: 'ETHUSDT', side: 'Buy', size: '1' },
      { symbol: 'SOLUSDT', side: 'Buy', size: '1' },
      { symbol: 'XRPUSDT', side: 'Sell', size: '1' },
    ] } },
  }), config, now), 'MAX_ACTIVE_TRADES');
});

test('hybrid controller deduplicates candidateKey and enforces only three active slots', async () => {
  const repository = new HybridExecutionRepository(null);
  assert.equal(repository.acceptDirectCandidate(candidate('a')).duplicate, false);
  assert.equal(repository.acceptDirectCandidate(candidate('a')).duplicate, true);
  assert.throws(() => repository.acceptDirectCandidate(candidate('a', { symbol: 'ETHUSDT' })), /different immutable/);
  repository.acceptDirectCandidate(candidate('b', { symbol: 'ETHUSDT' }));
  repository.acceptDirectCandidate(candidate('c', { symbol: 'SOLUSDT' }));
  repository.acceptDirectCandidate(candidate('d', { symbol: 'XRPUSDT' }));
  assert.ok(await repository.claim('owner:slot1', 1));
  assert.ok(await repository.claim('owner:slot2', 2));
  assert.ok(await repository.claim('owner:slot3', 3));
  assert.equal(await repository.claim('owner:slot1', 1), null);
});

test('database unavailable with no exchange orphans permits direct safe intake', async () => {
  const repository = new HybridExecutionRepository(null);
  const bybit = {
    positions: async () => ({ result: { list: [] } }),
    activeOrders: async () => ({ result: { list: [] } }),
  };
  const gate = await evaluateFirstTradeGate({ bybit, repository, config });
  assert.equal(gate.ok, true);
  assert.equal(gate.postgresSupportStatus, 'DEGRADED');
  assert.equal(gate.postgresRequiredForNewCandidate, false);
  assert.equal(gate.recoveryCode, null);
});

test('cold start with DB unavailable and orphan exchange position requires reconciliation', async () => {
  const repository = new HybridExecutionRepository(null);
  const bybit = {
    positions: async () => ({ result: { list: [{ symbol: 'BTCUSDT', side: 'Buy', size: '1' }] } }),
    activeOrders: async () => ({ result: { list: [] } }),
  };
  const gate = await evaluateFirstTradeGate({ bybit, repository, config });
  assert.equal(gate.ok, false);
  assert.equal(gate.recoveryStatus, 'DEGRADED_RECOVERY');
  assert.equal(gate.recoveryCode, 'RECONCILIATION_REQUIRED');
});

test('authenticated direct endpoint is idempotent and rejects unauthenticated delivery', async () => {
  const repository = new HybridExecutionRepository(null);
  const runtime = {
    enabled: true, ready: true, leader: true, databaseReady: false, migrationVersion: 0,
    recoveryRequired: false, slots: { 1: { state: 'WAITING' }, 2: { state: 'WAITING' }, 3: { state: 'WAITING' } },
  };
  const server = await createHealthServer(runtime, 0, {
    handoffToken: 'test-token',
    intakeCandidate: async (payload) => repository.acceptDirectCandidate(payload),
  });
  try {
    const port = server.address().port;
    const url = `http://127.0.0.1:${port}/internal/execution-candidate`;
    const unauthorized = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(candidate()) });
    assert.equal(unauthorized.status, 401);
    const first = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer test-token' }, body: JSON.stringify(candidate()) });
    assert.equal(first.status, 202);
    assert.equal((await first.json()).duplicate, false);
    const duplicate = await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json', authorization: 'Bearer test-token' }, body: JSON.stringify(candidate()) });
    assert.equal(duplicate.status, 202);
    assert.equal((await duplicate.json()).duplicate, true);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
