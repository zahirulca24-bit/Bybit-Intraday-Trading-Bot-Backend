import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { orderLinkId } from '../src/bybitClient.js';
import { validateContract, revalidateLive } from '../src/validator.js';
import { CommandExecutor } from '../src/executor.js';
import { createHealthServer } from '../src/httpServer.js';

const now = Math.floor(Date.now() / 1000);
const payload = {
  candidateKey: 'cand-1',
  symbol: 'BTCUSDT',
  side: 'Buy',
  positionSizingStatus: 'SIZING_APPROVED',
  sizingApproved: true,
  sizingDecisionAt: now,
  executionStatus: 'AWAITING_NODE_EXECUTION',
  orderSubmitted: false,
  marginMode: 'ISOLATED',
  leverage: 5,
  qty: '0.100',
  entryReference: 100,
  technicalStopLoss: 99,
  takeProfitReference: 102,
  requiredInitialMarginUsdt: 2,
  projectedTotalInitialMarginUsdt: 102,
  nodeExecutionRequirements: {
    marginMode: 'ISOLATED',
    leverage: 5,
    revalidateWalletAndInstrumentRules: true,
    submitOnlyAfterRevalidation: true,
  },
};

const config = {
  maxCandidateAgeSeconds: 1200,
  maxEntryDriftPct: 0.5,
  minimumGrossRr: 2,
  leverage: 5,
  marginMode: 'ISOLATED_MARGIN',
  category: 'linear',
};

function liveTruth(overrides = {}) {
  return {
    wallet: { result: { list: [{ totalEquity: '1000', totalAvailableBalance: '800', totalInitialMargin: '100' }] } },
    instrument: { result: { list: [{
      status: 'Trading',
      lotSizeFilter: { minOrderQty: '0.001', maxMktOrderQty: '10', qtyStep: '0.001', minNotionalValue: '5' },
      leverageFilter: { maxLeverage: '100' },
    }] } },
    ticker: { result: { list: [{ markPrice: '100' }] } },
    positions: { result: { list: [] } },
    activeOrders: { result: { list: [] } },
    pendingReservedMargin: 0,
    orderLinkId: orderLinkId(payload.candidateKey),
    ...overrides,
  };
}

function repositoryMock() {
  const transitions = [];
  const orders = [];
  const fills = [];
  return {
    transitions,
    orders,
    fills,
    transition: async (command, nextState) => {
      transitions.push(nextState);
      return { ...command, state: nextState };
    },
    pendingReservedMargin: async () => 0,
    recordOrder: async (_command, status, evidence) => { orders.push({ status, evidence }); },
    recordFills: async (_command, evidence) => { fills.push(...evidence); },
  };
}

function bybitMock(resolution = { state: 'FILLED', executions: [{ execId: 'e1', execQty: '0.100', execPrice: '100' }], cumulativeQty: 0.1 }) {
  let created = 0;
  let marginChanges = 0;
  return {
    get created() { return created; },
    get marginChanges() { return marginChanges; },
    findOrder: async () => null,
    executions: async () => ({ result: { list: [] } }),
    wallet: async () => liveTruth().wallet,
    instrument: async () => liveTruth().instrument,
    ticker: async () => liveTruth().ticker,
    positions: async () => liveTruth().positions,
    activeOrders: async () => liveTruth().activeOrders,
    accountInfo: async () => ({ result: { marginMode: 'ISOLATED_MARGIN' } }),
    setMarginMode: async () => { marginChanges += 1; return { retCode: 0 }; },
    setLeverage: async () => ({ retCode: 0 }),
    createOrder: async () => { created += 1; return { retCode: 0, result: { orderId: 'order-1' } }; },
    waitForResolution: async () => resolution,
    ensureProtection: async () => ({ symbol: 'BTCUSDT', size: '0.100', stopLoss: '99', takeProfit: '102' }),
  };
}

test('configuration is demo-only, requires stable owner identity, and is disabled by default', () => {
  const env = {
    DATABASE_URL: 'postgres://x',
    BYBIT_API_KEY: 'k',
    BYBIT_API_SECRET: 's',
    NODE_EXECUTION_OWNER_ID: 'bybit-executor-prod',
  };
  const cfg = loadConfig(env);
  assert.equal(cfg.baseUrl, 'https://api-demo.bybit.com');
  assert.equal(cfg.enabled, false);
  assert.equal(cfg.port, 8080);
  assert.throws(() => loadConfig({ ...env, BYBIT_BASE_URL: 'https://api.bybit.com' }), /locked to Bybit Demo/);
  assert.throws(() => loadConfig({ ...env, NODE_EXECUTION_OWNER_ID: '' }), /required/);
});

test('order identity is deterministic and bounded', () => {
  assert.equal(orderLinkId('abc'), orderLinkId('abc'));
  assert.notEqual(orderLinkId('abc'), orderLinkId('def'));
  assert.ok(orderLinkId('abc').length <= 36);
});

test('contract and final live truth are revalidated', () => {
  assert.equal(validateContract(payload, 'cand-1'), payload);
  const result = revalidateLive(payload, liveTruth(), config, now);
  assert.equal(result.markPrice, 100);
  assert.equal(result.grossRr, 2);
  assert.throws(() => validateContract({ ...payload, leverage: 10 }, 'cand-1'), /Isolated 5x/);
  assert.throws(() => revalidateLive(payload, liveTruth({ ticker: { result: { list: [{ markPrice: '101' }] } } }), config, now), /drift/);
  assert.throws(() => revalidateLive({ ...payload, qty: '0.1005' }, liveTruth(), config, now), /qtyStep/);
  assert.throws(() => revalidateLive({ ...payload, sizingDecisionAt: now - 5000 }, liveTruth(), config, now), /stale/);
});

test('executor submits once and moves to managing only after fill and protection evidence', async () => {
  const repository = repositoryMock();
  const bybit = bybitMock();
  const executor = new CommandExecutor(repository, bybit, config);
  const result = await executor.execute({ candidateKey: 'cand-1', state: 'RESERVED', ownerId: 'owner:slot1', slotId: 1, payload });
  assert.deepEqual(repository.transitions, ['ORDER_PENDING', 'MANAGING']);
  assert.equal(bybit.created, 1);
  assert.equal(bybit.marginChanges, 0);
  assert.equal(result.state, 'MANAGING');
  assert.ok(repository.orders.some((row) => row.status === 'PROTECTION_VERIFIED'));
  assert.equal(repository.fills.length, 1);
});

test('unknown submission resolution remains ORDER_PENDING and is not blindly failed', async () => {
  const repository = repositoryMock();
  const bybit = bybitMock({ state: 'UNKNOWN', executions: [], cumulativeQty: 0 });
  const executor = new CommandExecutor(repository, bybit, config);
  const result = await executor.execute({ candidateKey: 'cand-1', state: 'RESERVED', ownerId: 'owner:slot1', slotId: 1, payload });
  assert.deepEqual(repository.transitions, ['ORDER_PENDING']);
  assert.equal(result.state, 'ORDER_PENDING');
});

test('restart recovery resumes ORDER_PENDING without another order submission', async () => {
  const repository = repositoryMock();
  const bybit = bybitMock();
  const executor = new CommandExecutor(repository, bybit, config);
  const result = await executor.execute({ candidateKey: 'cand-1', state: 'ORDER_PENDING', ownerId: 'owner:slot1', slotId: 1, payload });
  assert.deepEqual(repository.transitions, ['MANAGING']);
  assert.equal(bybit.created, 0);
  assert.equal(result.state, 'MANAGING');
});

test('explicit Bybit rejection becomes FAILED while transport uncertainty stays pending', async () => {
  const repository = repositoryMock();
  const bybit = bybitMock();
  bybit.createOrder = async () => {
    const error = new Error('rejected');
    error.retCode = 110001;
    throw error;
  };
  const executor = new CommandExecutor(repository, bybit, config);
  const result = await executor.execute({ candidateKey: 'cand-1', state: 'RESERVED', ownerId: 'owner:slot1', slotId: 1, payload });
  assert.deepEqual(repository.transitions, ['ORDER_PENDING', 'FAILED']);
  assert.equal(result.state, 'FAILED');
});

test('Cloud Run health server binds a port and readiness is fail-closed', async () => {
  const runtime = {
    enabled: false,
    ready: false,
    leader: false,
    databaseReady: false,
    migrationVersion: 0,
    startedAt: new Date().toISOString(),
    slots: {},
    lastError: 'disabled',
  };
  const server = await createHealthServer(runtime, 0);
  try {
    const port = server.address().port;
    const health = await fetch(`http://127.0.0.1:${port}/healthz`);
    const readiness = await fetch(`http://127.0.0.1:${port}/readyz`);
    assert.equal(health.status, 200);
    assert.equal(readiness.status, 503);
    runtime.ready = true;
    const ready = await fetch(`http://127.0.0.1:${port}/readyz`);
    assert.equal(ready.status, 200);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});
