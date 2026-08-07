import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateFirstTradeGate, assertFirstTradeCandidate } from '../src/firstTradeGate.js';

function config(overrides = {}) {
  return {
    baseUrl: 'https://api-demo.bybit.com',
    enabled: true,
    maxActiveTrades: 3,
    gradeRiskPct: { 'A+': 1.0, A: 0.75 },
    ...overrides,
  };
}

function bybit({ positions = [], orders = [] } = {}) {
  return {
    positions: async () => ({ result: { list: positions } }),
    activeOrders: async () => ({ result: { list: orders } }),
  };
}

function repository(active = []) {
  return {
    pool: { query: async () => ({ rows: active }) },
  };
}

test('clean Demo account enables three controlled execution slots with grade-based risk policy', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config() });
  assert.equal(result.ok, true);
  assert.equal(result.armed, true);
  assert.equal(result.openPositionCount, 0);
  assert.equal(result.openOrderCount, 0);
  assert.equal(result.activeCommandCount, 0);
  assert.equal(result.maxActiveTrades, 3);
  assert.deepEqual(result.gradeRiskPct, { 'A+': 1.0, A: 0.75 });
  assert.equal(result.bPlusRejected, true);
});

test('NODE_EXECUTION_ENABLED remains the single operator execution control', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config({ enabled: false }) });
  assert.equal(result.ok, false);
  assert.equal(result.armed, false);
  assert.match(result.reasons.join(' '), /NODE_EXECUTION_ENABLED is false/);
});

test('gate rejects an invalid active-trade contract', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config({ maxActiveTrades: 1 }) });
  assert.equal(result.ok, false);
  assert.match(result.reasons.join(' '), /MAX_ACTIVE_TRADES must equal 3/);
});

test('gate rejects an invalid grade-risk configuration', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config({ gradeRiskPct: { 'A+': 0.25, A: 0.25 } }) });
  assert.equal(result.ok, false);
  assert.match(result.reasons.join(' '), /A\+=1\.00%, A=0\.75%, B\+=reject/);
});

test('open exchange truth or unresolved database command blocks activation', async () => {
  const result = await evaluateFirstTradeGate({
    bybit: bybit({ positions: [{ size: '0.01' }], orders: [{ orderStatus: 'New' }] }),
    repository: repository([{ candidate_key: 'existing', state: 'ORDER_PENDING' }]),
    config: config(),
  });
  assert.equal(result.ok, false);
  assert.equal(result.openPositionCount, 1);
  assert.equal(result.openOrderCount, 1);
  assert.equal(result.activeCommandCount, 1);
});

test('A+ candidate accepts up to 1.00 percent effective risk', () => {
  const approved = { payload: { grade: 'A+', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 1.0, effectiveRiskPerTradePct: 1.0 } };
  assert.equal(assertFirstTradeCandidate(approved, config()), true);
  assert.equal(assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 0.5 } }, config()), true);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 1.01 } }, config()), /no greater than 1\.00%/);
});

test('A candidate accepts up to 0.75 percent effective risk', () => {
  const approved = { payload: { grade: 'A', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 0.75, effectiveRiskPerTradePct: 0.75 } };
  assert.equal(assertFirstTradeCandidate(approved, config()), true);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, gradeRiskPct: 1.0 } }, config()), /requires A at 0\.75%/);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 0.8 } }, config()), /no greater than 0\.75%/);
});

test('B+ candidate is rejected even when execution-qualified', () => {
  assert.throws(() => assertFirstTradeCandidate({ payload: { grade: 'B+', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 0.5, effectiveRiskPerTradePct: 0.5 } }, config()), /B\+ is rejected/);
});
