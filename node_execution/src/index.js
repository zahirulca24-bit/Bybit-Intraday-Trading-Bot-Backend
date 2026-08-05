import pg from 'pg';
import { loadConfig } from './config.js';
import { BybitClient } from './bybitClient.js';
import { ExecutionRepository } from './repository.js';
import { CommandExecutor } from './executor.js';
import { TradeManager } from './tradeManager.js';
import { createHealthServer } from './httpServer.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const config = loadConfig();
const runtime = {
  enabled: config.enabled,
  ready: false,
  leader: false,
  databaseReady: false,
  migrationVersion: 0,
  startedAt: new Date().toISOString(),
  lastError: null,
  slots: Object.fromEntries([1, 2, 3].map((slotId) => [slotId, { state: 'IDLE', candidateKey: null, updatedAt: null }])),
};

const server = await createHealthServer(runtime, config.port);
console.log(JSON.stringify({ level: 'info', event: 'http_listening', port: config.port }));

const pool = new pg.Pool({
  connectionString: config.databaseUrl,
  max: 8,
  connectionTimeoutMillis: 10000,
  idleTimeoutMillis: 30000,
  application_name: 'bybit-node-execution-step11',
});
const repository = new ExecutionRepository(pool);
const bybit = new BybitClient(config);
const executor = new CommandExecutor(repository, bybit, config);
const manager = new TradeManager(repository, bybit, config);
let stopping = false;
let workersStarted = false;

function setSlot(slotId, state, candidateKey = null) {
  runtime.slots[slotId] = { state, candidateKey, updatedAt: new Date().toISOString() };
}

async function processCommand(command) {
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
        runtime.lastError = 'Node execution leader database session was lost; all slots stopped.';
        setSlot(slotId, 'LEADERSHIP_LOST');
        break;
      }
      command = await repository.ownedActiveCommand(ownerId, slotId);
      if (!command) command = await repository.claim(ownerId, slotId);
      if (!command) {
        setSlot(slotId, 'WAITING');
        await sleep(config.pollMs);
        continue;
      }
      setSlot(slotId, command.state, command.candidateKey);
      const result = await processCommand(command);
      setSlot(slotId, result?.state || 'UNKNOWN', command.candidateKey);
      await sleep(config.pollMs);
    } catch (error) {
      runtime.lastError = error.message;
      setSlot(slotId, command?.state || 'ERROR', command?.candidateKey || null);
      console.error(JSON.stringify({ level: 'error', event: 'slot_error', slotId, candidateKey: command?.candidateKey || null, message: error.message, time: new Date().toISOString() }));
      await sleep(config.pollMs);
    }
  }
}

async function coordinator() {
  if (!config.enabled) {
    runtime.lastError = 'NODE_EXECUTION_ENABLED is false; execution and management remain fail-closed.';
    console.log(JSON.stringify({ level: 'warn', event: 'execution_disabled' }));
    return;
  }
  while (!stopping) {
    try {
      const database = await repository.ping();
      runtime.databaseReady = database.ok;
      runtime.migrationVersion = database.migrationVersion;
      if (!database.ok) throw new Error('PostgreSQL migration v5 execution contract is unavailable');
      runtime.leader = await repository.acquireLeadership();
      if (!runtime.leader) {
        runtime.ready = false;
        runtime.lastError = 'Another Node execution instance owns the PostgreSQL leader lock.';
        await sleep(config.pollMs);
        continue;
      }

      const adopted = await repository.adoptActiveCommands(config.ownerId);
      runtime.ready = true;
      runtime.lastError = null;
      workersStarted = true;
      console.log(JSON.stringify({ level: 'info', event: 'execution_management_leader_ready', ownerId: config.ownerId, adoptedCommands: adopted.length }));
      await Promise.all([1, 2, 3].map(slotLoop));

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
      await repository.releaseLeadership().catch(() => undefined);
      console.error(JSON.stringify({ level: 'error', event: 'coordinator_error', message: error.message }));
      if (!stopping) await sleep(config.pollMs);
    }
  }
}

async function shutdown(signal) {
  if (stopping) return;
  stopping = true;
  workersStarted = false;
  runtime.ready = false;
  runtime.leader = false;
  for (const slotId of [1, 2, 3]) setSlot(slotId, 'STOPPING', runtime.slots[slotId]?.candidateKey || null);
  console.log(JSON.stringify({ level: 'info', event: 'shutdown', signal }));
  await repository.releaseLeadership().catch(() => undefined);
  await pool.end().catch(() => undefined);
  await new Promise((resolve) => server.close(resolve));
}

for (const signal of ['SIGTERM', 'SIGINT']) process.on(signal, () => shutdown(signal).finally(() => process.exit(0)));
process.on('unhandledRejection', (error) => {
  runtime.ready = false;
  runtime.leader = false;
  runtime.lastError = error?.message || String(error);
  console.error(JSON.stringify({ level: 'fatal', event: 'unhandled_rejection', message: runtime.lastError }));
});

void coordinator();
