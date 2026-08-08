import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { evaluateFirstTradeGate, assertFirstTradeCandidate } from '../src/firstTradeGate.js';
import { orderLinkId } from '../src/bybitClient.js';

function config(overrides = {}) {
  return {
    baseUrl: 'https://api-demo.bybit.com',
    enabled: true,
    maxActiveTrades: 3,
    gradeRiskPct: { 'A+': 1.0, A: 1.0 },
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

function activeCommand(overrides = {}) {
  return {
    candidateKey: 'BTCUSDT:recovery:1',
    state: 'MANAGING',
    payload: { symbol: 'BTCUSDT', side: 'Buy' },
    ...overrides,
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
  assert.deepEqual(result.gradeRiskPct, { 'A+': 1.0, A: 1.0 });
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
  assert.match(result.reasons.join(' '), /A\+=1\.00%, A=1\.00%, B\+=reject/);
});

test('legitimate adopted command and matching exchange position are restart-recoverable', async () => {
  const command = activeCommand();
  const result = await evaluateFirstTradeGate({
    bybit: bybit({ positions: [{ symbol: 'BTCUSDT', side: 'Buy', size: '0.01' }] }),
    repository: repository(),
    config: config(),
    adoptedCommands: [command],
  });
  assert.equal(result.ok, true);
  assert.equal(result.recoveryMode, true);
  assert.equal(result.activeCommandCount, 1);
  assert.equal(result.managedPositionCount, 1);
  assert.equal(result.orphanPositionCount, 0);
});

test('legitimate adopted command and matching linked order are restart-recoverable', async () => {
  const command = activeCommand({ state: 'ORDER_PENDING' });
  const result = await evaluateFirstTradeGate({
    bybit: bybit({ orders: [{ symbol: 'BTCUSDT', side: 'Buy', orderStatus: 'New', orderLinkId: orderLinkId(command.candidateKey) }] }),
    repository: repository(),
    config: config(),
    adoptedCommands: [command],
  });
  assert.equal(result.ok, true);
  assert.equal(result.managedOrderCount, 1);
  assert.equal(result.orphanOrderCount, 0);
});

test('orphan exchange position still blocks restart recovery', async () => {
  const result = await evaluateFirstTradeGate({
    bybit: bybit({ positions: [{ symbol: 'ETHUSDT', side: 'Sell', size: '0.5' }] }),
    repository: repository(),
    config: config(),
    adoptedCommands: [activeCommand()],
  });
  assert.equal(result.ok, false);
  assert.equal(result.orphanPositionCount, 1);
  assert.match(result.reasons.join(' '), /orphan open position/);
});

test('orphan exchange order still blocks restart recovery', async () => {
  const result = await evaluateFirstTradeGate({
    bybit: bybit({ orders: [{ symbol: 'ETHUSDT', side: 'Buy', orderStatus: 'New', orderLinkId: 'manual-order' }] }),
    repository: repository(),
    config: config(),
    adoptedCommands: [activeCommand()],
  });
  assert.equal(result.ok, false);
  assert.equal(result.orphanOrderCount, 1);
  assert.match(result.reasons.join(' '), /orphan open order/);
});

test('more than three recovered active commands blocks activation', async () => {
  const commands = [1, 2, 3, 4].map((index) => activeCommand({ candidateKey: `BTCUSDT:recovery:${index}` }));
  const result = await evaluateFirstTradeGate({
    bybit: bybit(),
    repository: repository(),
    config: config(),
    adoptedCommands: commands,
  });
  assert.equal(result.ok, false);
  assert.match(result.reasons.join(' '), /maximum is 3/);
});

test('coordinator adopts recovery commands before evaluating startup preflight', () => {
  const source = fs.readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');
  const adoptIndex = source.indexOf('repository.adoptActiveCommands(config.ownerId)');
  const gateIndex = source.indexOf('evaluateFirstTradeGate({ bybit, repository, config, adoptedCommands: adopted })');
  assert.notEqual(adoptIndex, -1);
  assert.notEqual(gateIndex, -1);
  assert.ok(adoptIndex < gateIndex, 'active-command adoption must happen before startup preflight');
});

test('A+ candidate accepts up to 1.00 percent effective risk', () => {
  const approved = { payload: { grade: 'A+', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 1.0, effectiveRiskPerTradePct: 1.0 } };
  assert.equal(assertFirstTradeCandidate(approved, config()), true);
  assert.equal(assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 0.5 } }, config()), true);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 1.01 } }, config()), /no greater than 1\.00%/);
});

test('A candidate accepts up to 1.00 percent effective risk', () => {
  const approved = { payload: { grade: 'A', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 1.0, effectiveRiskPerTradePct: 1.0 } };
  assert.equal(assertFirstTradeCandidate(approved, config()), true);
  assert.equal(assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 0.5 } }, config()), true);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, gradeRiskPct: 0.75 } }, config()), /requires A at 1\.00%/);
  assert.throws(() => assertFirstTradeCandidate({ payload: { ...approved.payload, effectiveRiskPerTradePct: 1.01 } }, config()), /no greater than 1\.00%/);
});

test('B+ candidate is rejected even when execution-qualified', () => {
  assert.throws(() => assertFirstTradeCandidate({ payload: { grade: 'B+', executionStatus: 'AWAITING_NODE_EXECUTION', gradeRiskPct: 0.5, effectiveRiskPerTradePct: 0.5 } }, config()), /B\+ is rejected/);
});
