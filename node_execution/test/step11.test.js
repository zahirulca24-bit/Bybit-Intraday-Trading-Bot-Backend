import test from 'node:test';
import assert from 'node:assert/strict';
import { TradeManager, TRADE_MANAGEMENT_POLICY, managementLinkId } from '../src/tradeManager.js';

const payload = {
  candidateKey: 'cand-11', symbol: 'BTCUSDT', side: 'Buy',
  technicalStopLoss: 90, entryReference: 100,
};

function command(state = 'MANAGING') {
  return { candidateKey: 'cand-11', state, ownerId: 'owner:slot1', slotId: 1, payload };
}

function repository(initial = null) {
  let state = initial;
  const transitions = [];
  const orders = [];
  const fills = [];
  return {
    transitions, orders, fills,
    getManagementState: async () => state,
    putManagementState: async (_key, value) => { state = structuredClone(value); },
    recordOrder: async (_command, status, evidence) => orders.push({ status, evidence }),
    recordFills: async (_command, rows) => fills.push(...rows),
    transition: async (current, next) => { transitions.push(next); return { ...current, state: next }; },
    snapshot: () => state,
  };
}

function bybit(markSequence, positionSequence) {
  let markIndex = 0;
  let positionIndex = 0;
  let closes = 0;
  const stops = [];
  return {
    get closes() { return closes; },
    stops,
    position: async () => ({ result: { list: [positionSequence[Math.min(positionIndex++, positionSequence.length - 1)]].filter(Boolean) } }),
    ticker: async () => ({ result: { list: [{ markPrice: String(markSequence[Math.min(markIndex++, markSequence.length - 1)]) }] } }),
    instrument: async () => ({ result: { list: [{ lotSizeFilter: { qtyStep: '0.001', minOrderQty: '0.001' } }] } }),
    findOrder: async () => null,
    executions: async () => ({ result: { list: [] } }),
    createOrder: async (order) => { closes += 1; return { retCode: 0, result: { orderId: order.orderLinkId } }; },
    waitForResolution: async (_symbol, link, qty) => ({ state: 'FILLED', cumulativeQty: Number(qty), executions: [{ execId: link, execQty: String(qty), execPrice: '115' }] }),
    setTradingStop: async (body) => { stops.push(body); return { retCode: 0 }; },
  };
}

const config = { category: 'linear' };

test('approved management policy is locked', () => {
  assert.deepEqual(TRADE_MANAGEMENT_POLICY, {
    id: 'NODE_TRADE_MANAGEMENT_V1', tp1R: 1.5, tp1ClosePct: 40,
    tp2R: 2, tp2ClosePct: 30, runnerPct: 30, trailingDistanceR: 0.5,
  });
  assert.equal(managementLinkId('abc', 'tp1'), managementLinkId('abc', 'tp1'));
});

test('TP1 closes 40 percent and moves stop to break-even', async () => {
  const repo = repository();
  const exchange = bybit([115], [
    { symbol: 'BTCUSDT', side: 'Buy', size: '1', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.6', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.6', avgPrice: '100', positionIdx: 0 },
  ]);
  const result = await new TradeManager(repo, exchange, config).cycle(command());
  assert.equal(result.state, 'MANAGING');
  assert.equal(exchange.closes, 1);
  assert.equal(repo.snapshot().tp1Done, true);
  assert.equal(repo.snapshot().breakEvenDone, true);
  assert.equal(Number(exchange.stops[0].stopLoss), 100);
});

test('TP2 closes 30 percent of initial qty and enables 0.5R trailing runner', async () => {
  const initial = {
    version: 1, policyId: 'NODE_TRADE_MANAGEMENT_V1', candidateKey: 'cand-11', symbol: 'BTCUSDT', side: 'Buy',
    initialQty: 1, averageEntry: 100, initialStop: 90, riskDistance: 10,
    tp1Price: 115, tp2Price: 120, tp1Done: true, breakEvenDone: true,
    tp2Done: false, trailingActive: false, trailingStop: null,
  };
  const repo = repository(initial);
  const exchange = bybit([121], [
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.6', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.3', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.3', avgPrice: '100', positionIdx: 0 },
  ]);
  await new TradeManager(repo, exchange, config).cycle(command());
  assert.equal(exchange.closes, 1);
  assert.equal(repo.snapshot().tp2Done, true);
  assert.equal(repo.snapshot().trailingActive, true);
  assert.equal(Number(repo.snapshot().trailingStop), 116);
});

test('missing exchange position closes command without another order', async () => {
  const repo = repository({ policyId: 'NODE_TRADE_MANAGEMENT_V1' });
  const exchange = bybit([100], [null]);
  const result = await new TradeManager(repo, exchange, config).cycle(command());
  assert.equal(exchange.closes, 0);
  assert.deepEqual(repo.transitions, ['CLOSING', 'CLOSED']);
  assert.equal(result.state, 'CLOSED');
});

test('PARTIALLY_FILLED reconciles into MANAGING when position remains', async () => {
  const repo = repository();
  const exchange = bybit([105], [
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.5', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.5', avgPrice: '100', positionIdx: 0 },
    { symbol: 'BTCUSDT', side: 'Buy', size: '0.5', avgPrice: '100', positionIdx: 0 },
  ]);
  const result = await new TradeManager(repo, exchange, config).cycle(command('PARTIALLY_FILLED'));
  assert.deepEqual(repo.transitions, ['MANAGING']);
  assert.equal(result.state, 'MANAGING');
});
