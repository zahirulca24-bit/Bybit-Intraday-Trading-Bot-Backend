import pg from 'pg';
import { loadConfig } from './config.js';
import { BybitClient } from './bybitClient.js';
import { ExecutionRepository } from './repository.js';
import { HybridExecutionRepository } from './hybridRepository.js';
import { CommandExecutor } from './executor.js';
import { TradeManager } from './tradeManager.js';
import { createHealthServer } from './httpServer.js';
import { evaluateFirstTradeGate, assertFirstTradeCandidate } from './firstTradeGate.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const config = loadConfig();
const runtime = {
  enabled: config.enabled,
  ready: false,
  leader: false,
  databaseReady: false,
  migrationVersion: 0,
  recoveryRequired: false,
  recoveryStatus: 'STARTING',
  startedAt: new Date().toISOString(),
  lastError: null,
  firstTradeGate: { ok: false, armed: config.enabled, reasons: ['Preflight has not run.'] },
  nodeHandoff: { status: 'WAIT', delivered: 0, retrying: 0, rejectedInvalid: 0, lastCandidateKey: null, lastCode: 'WAITING_FOR_CANDIDATE' },
  nodeLiveSizing: { status: 'WAITING_FOR_CANDIDATE', code: 'WAITING_FOR_CANDIDATE', candidateKey: null, symbol: null, reason: 'No candidate is currently being live-sized.' },
  nodeExecution: { status: 'WAITING_FOR_CANDIDATE', candidateKey: null, symbol: null, side: null },
  postgresSupport: { status: config.databaseUrl ? 'WAIT_RETRY' : 'DEGRADED', supportOnly: true, tradeRejectionAuthority: false },
  slots: Object.fromEntries([1, 2, 3].map((slotId) => [slotId, { state: 'IDLE', candidateKey: null, symbol: null, side: null, updatedAt: null }])),
};

const pool = config.databaseUrl ? new pg.Pool({
  connectionString: config.databaseUrl,
  max: 8,
  connectionTimeoutMillis: 10000,
  idleTimeoutMillis: 30000,
  application_name: 'bybit-node-execution-direct-controlled-demo',
}) : null;
const databaseRepository = pool ? new ExecutionRepository(pool) : null;
const repository = new HybridExecutionRepository(databaseRepository);
const bybit = new BybitClient(config);
const executor = new CommandExecutor(repository, bybit, config);
const manager = new TradeManager(repository, bybit, config);
let stopping = false;
let workersStarted = false;

function setSlot(slotId, state, command = null) {
  runtime.slots[slotId] = {
    state,
    candidateKey: command?.candidateKey || null,
    symbol: command?.payload?.symbol || null,
    side: command?.payload?.side || null,
    updatedAt: new Date().toISOString(),
  };
}

function updateSupportStatus() {
  runtime.postgresSupport = repository.supportSnapshot();
  runtime.databaseReady = Boolean(runtime.postgresSupport.databaseReady);
  runtime.migrationVersion = Number(runtime.postgresSupport.migrationVersion || 0);
}

function recordSizingStatus(command, result) {
  const wait = result?.nodeExecutionWait;
  if (wait) {
    runtime.nodeLiveSizing = {
      status: 'WAIT',
      code: wait.code || 'NODE_EXECUTION_WAIT',
      candidateKey: command.candidateKey,
      symbol: command.payload?.symbol || null,
      reason: wait.reason || 'Node live sizing/execution preparation is waiting.',
      retryable: wait.retryable !== false,
    };
    return;
  }
  const payload = result?.payload || command?.payload || {};
  if (payload.nodeSizingStatus === 'NODE_SIZING_READY') {
    runtime.nodeLiveSizing = {
      status: 'READY',
      code: 'NODE_SIZING_READY',
      candidateKey: command.candidateKey,
      symbol: payload.symbol || null,
      reason: 'Node live Bybit sizing and technical plan are ready.',
      riskBudgetUsdt: payload.riskBudgetUsdt ?? null,
      plannedStopRiskUsdt: payload.plannedStopRiskUsdt ?? null,
      qty: payload.qty ?? null,
    };
  }
}

async function processCommand(command) {
  if (command.state === 'RESERVED') assertFirstTradeCandidate(command, config);
  if (['RESERVED', 'ORDER_PENDING'].includes(command.state)) return executor.execute(command);
  if (['PARTIALLY_FILLED', 'MANAGING'].includes(command.state)) return manager.cycle(command);
  if (command.state === 'CLOSING') {
    const position = await bybit.position(command.payload.symbol);
    const stillOpen = (position?.result?.list || []).some((row) => Number(row.size || 0) > 0 && String(row.side) === String(command.payload.side));
    if (stillOpen) return command;
    await repository.recordOrder(command, 'CLOSING_RECONCILED', { reason: 'No open exchange position remains' });
    return repository.transition(command, 'CLOSED');
  }
  return command;
}

async function slotLoop(slotId) {
  const ownerId = `${config.ownerId}:slot${slotId}`;
  setSlot(slotId, 'WAITING');
  while (!stopping && runtime.leader) {
    let command = null;
    try {
      if (!await repository.verifyLeadership()) {
        runtime.leader = false;
        runtime.ready = false;
        runtime.lastError = 'Node execution ownership was lost; slot loops stopped without resubmitting orders.';
        setSlot(slotId, 'LEADERSHIP_LOST');
        break;
      }
      updateSupportStatus();
      command = await repository.ownedActiveCommand(ownerId, slotId);
      if (!command && !runtime.recoveryRequired) command = await repository.claim(ownerId, slotId);
      if (!command) {
        setSlot(slotId, runtime.recoveryRequired ? 'RECONCILIATION_REQUIRED' : 'WAITING');
        await sleep(config.pollMs);
        continue;
      }
      setSlot(slotId, command.state, command);
      runtime.nodeExecution = {
        status: command.state,
        candidateKey: command.candidateKey,
        symbol: command.payload?.symbol || null,
        side: command.payload?.side || null,
        slotId,
      };
      const result = await processCommand(command);
      recordSizingStatus(command, result);
      setSlot(slotId, result?.state || 'UNKNOWN', result || command);
      runtime.nodeExecution = {
        status: result?.state || 'UNKNOWN',
        candidateKey: command.candidateKey,
        symbol: (result?.payload || command.payload)?.symbol || null,
        side: (result?.payload || command.payload)?.side || null,
        slotId,
      };
      await sleep(config.pollMs);
    } catch (error) {
      runtime.lastError = error.message;
      setSlot(slotId, command?.state || 'ERROR', command);
      console.error(JSON.stringify({ level: 'error', event: 'slot_error', slotId, candidateKey: command?.candidateKey || null, message: error.message, time: new Date().toISOString() }));
      await sleep(config.pollMs);
    }
  }
}

async function coordinator() {
  if (!config.enabled) {
    runtime.lastError = 'NODE_EXECUTION_ENABLED is false; execution and management remain fail-closed.';
    runtime.recoveryStatus = 'DISABLED';
    console.log(JSON.stringify({ level: 'warn', event: 'execution_disabled' }));
    return;
  }
  while (!stopping) {
    try {
      const database = await repository.ping();
      runtime.databaseReady = Boolean(database.ok);
      runtime.migrationVersion = Number(database.migrationVersion || 0);
      updateSupportStatus();

      runtime.leader = await repository.acquireLeadership();
      if (!runtime.leader) {
        runtime.ready = false;
        runtime.lastError = 'Another Node execution instance owns the PostgreSQL execution leader lock.';
        runtime.recoveryStatus = 'LEADER_STANDBY';
        await sleep(config.pollMs);
        continue;
      }

      // Keep restart adoption before preflight. The `adopted` set below is the
      // unified active set after PostgreSQL adoption, so direct and support
      // inputs are reconciled by the same startup gate and slot controller.
      const postgresAdopted = await repository.adoptActiveCommands(config.ownerId);
      const adopted = await repository.activeCommands();
      if (adopted.length > config.maxActiveTrades) throw new Error(`Execution permits at most ${config.maxActiveTrades} active candidates.`);

      runtime.firstTradeGate = await evaluateFirstTradeGate({ bybit, repository, config, adoptedCommands: adopted });
      runtime.recoveryRequired = runtime.firstTradeGate.recoveryCode === 'RECONCILIATION_REQUIRED';
      runtime.recoveryStatus = runtime.firstTradeGate.recoveryStatus;
      if (!runtime.firstTradeGate.ok) {
        runtime.ready = false;
        runtime.lastError = `Execution recovery/preflight blocked: ${runtime.firstTradeGate.reasons.join(' ')}`;
        await repository.releaseLeadership().catch(() => undefined);
        runtime.leader = false;
        await sleep(config.pollMs);
        continue;
      }

      runtime.ready = true;
      runtime.recoveryRequired = false;
      runtime.recoveryStatus = 'READY';
      runtime.lastError = null;
      workersStarted = true;
      console.log(JSON.stringify({
        level: 'info',
        event: 'direct_demo_execution_ready',
        ownerId: config.ownerId,
        gradeRiskPct: config.gradeRiskPct,
        maxActiveTrades: config.maxActiveTrades,
        databaseSupport: runtime.postgresSupport.status,
        recoveredActiveCandidates: adopted.length,
        recoveredPostgresCommands: postgresAdopted.length,
      }));
      await Promise.all([1, 2, 3].map((slotId) => slotLoop(slotId)));

      workersStarted = false;
      runtime.ready = false;
      runtime.leader = false;
      if (!stopping) {
        await repository.releaseLeadership().catch(() => undefined);
        await sleep(config.pollMs);
      }
    } catch (error) {
      workersStarted = false;
      runtime.ready = false;
      runtime.leader = false;
      runtime.lastError = error.message;
      updateSupportStatus();
      await repository.releaseLeadership().catch(() => undefined);
      console.error(JSON.stringify({ level: 'error', event: 'coordinator_error', message: error.message }));
      if (!stopping) await sleep(config.pollMs);
    }
  }
}

const server = await createHealthServer(runtime, config.port, {
  handoffToken: config.handoffToken,
  intakeCandidate: async (payload) => {
    if (!runtime.enabled) throw new Error('Node execution is disabled');
    if (runtime.recoveryRequired) throw new Error('RECONCILIATION_REQUIRED');
    try {
      const accepted = repository.acceptDirectCandidate(payload);
      runtime.nodeHandoff = {
        ...runtime.nodeHandoff,
        status: 'PASS',
        delivered: Number(runtime.nodeHandoff.delivered || 0) + (accepted.duplicate ? 0 : 1),
        lastCandidateKey: accepted.command.candidateKey,
        lastCode: accepted.duplicate ? 'NODE_HANDOFF_DUPLICATE' : 'NODE_HANDOFF_ACCEPTED',
      };
      return accepted;
    } catch (error) {
      runtime.nodeHandoff = {
        ...runtime.nodeHandoff,
        status: 'DEGRADED',
        rejectedInvalid: Number(runtime.nodeHandoff.rejectedInvalid || 0) + 1,
        lastCode: 'NODE_HANDOFF_INVALID',
      };
      throw error;
    }
  },
});
console.log(JSON.stringify({ level: 'info', event: 'http_listening', port: config.port }));

async function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  workersStarted = false;
  runtime.ready = false;
  runtime.leader = false;
  for (const slotId of [1, 2, 3]) setSlot(slotId, 'STOPPING', runtime.slots[slotId]);
  console.log(JSON.stringify({ level: 'info', event: 'shutdown', signal }));
  await repository.releaseLeadership().catch(() => undefined);
  if (pool) await pool.end().catch(() => undefined);
  await new Promise((resolve) => server.close(resolve));
}

for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => shutdown(signal).finally(() => process.exit(0)));
process.on('unhandledRejection', (error) => {
  runtime.ready = false;
  runtime.lastError = error?.message || String(error);
  console.error(JSON.stringify({ level: 'fatal', event: 'unhandled_rejection', message: runtime.lastError }));
});

void coordinator();
