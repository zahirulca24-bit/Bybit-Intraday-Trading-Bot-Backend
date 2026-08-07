import test from 'node:test';
import assert from 'node:assert/strict';
import { evaluateFirstTradeGate, assertFirstTradeCandidate } from '../src/firstTradeGate.js';

function config(overrides = {}) {
  return {
    baseUrl: 'https://api-demo.bybit.com',
    enabled: true,
    firstDemoTradeArmed: true,
    maxActiveTrades: 3,
    riskPerTradePct: 0.25,
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
    pool: {
      query: async () => ({ rows: active }),
    },
  };
}

test('clean Demo account arms three controlled execution slots', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config() });
  assert.equal(result.ok, true);
  assert.equal(result.openPositionCount, 0);
  assert.equal(result.openOrderCount, 0);
  assert.equal(result.activeCommandCount, 0);
  assert.equal(result.maxActiveTrades, 3);
  assert.equal(result.riskPerTradePct, 0.25);
});

test('gate remains fail-closed unless separately armed', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config({ firstDemoTradeArmed: false }) });
  assert.equal(result.ok, false);
  assert.match(result.reasons.join(' '), /FIRST_DEMO_TRADE_ARMED/);
});

test('gate rejects an invalid active-trade contract', async () => {
  const result = await evaluateFirstTradeGate({ bybit: bybit(), repository: repository(), config: config({ maxActiveTrades: 1 }) });
  assert.equal(result.ok, false);
  assert.match(result.reasons.join(' '), /MAX_ACTIVE_TRADES must equal 3/);
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

test('first candidate must be A or A+, execution-qualified, and exactly 0.25 percent risk', () => {
  const approved = { payload: { grade: 'A+', executionStatus: 'AWAITING_NODE_EXECUTION', riskPerTradePct: 0.25 } };
  assert.equal(assertFirstTradeCandidate(approved, config()), true);
  assert.throws(() => assertFirstTradeCandidate({ payload: { grade: 'B+', executionStatus: 'AWAITING_NODE_EXECUTION', riskPerTradePct: 0.25 } }, config()), /A\+ or A/);
  assert.throws(() => assertFirstTradeCandidate({ payload: { grade: 'A', executionStatus: 'AWAITING_NODE_EXECUTION', riskPerTradePct: 0.5 } }, config()), /0.25/);
});
