import os from 'node:os';
import pg from 'pg';
import { loadConfig } from './config.js';
import { BybitClient } from './bybitClient.js';
import { ExecutionRepository } from './repository.js';
import { CommandExecutor } from './executor.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const config = loadConfig();
if (!config.enabled) {
  console.error('NODE_EXECUTION_ENABLED is false; fail-closed startup.');
  process.exit(2);
}
const pool = new pg.Pool({ connectionString: config.databaseUrl, max: 6 });
const repository = new ExecutionRepository(pool);
const executor = new CommandExecutor(repository, new BybitClient(config));
let stopping = false;

async function slotLoop(slotId) {
  const ownerId = `${config.ownerPrefix}-${os.hostname()}-${process.pid}-slot${slotId}`;
  while (!stopping) {
    try {
      const command = await repository.claim(ownerId, slotId);
      if (!command) { await sleep(config.pollMs); continue; }
      await executor.execute(command);
    } catch (error) {
      console.error(JSON.stringify({ level: 'error', slotId, message: error.message, time: new Date().toISOString() }));
      await sleep(config.pollMs);
    }
  }
}

for (const signal of ['SIGTERM','SIGINT']) {
  process.on(signal, async () => {
    stopping = true;
    await pool.end().catch(() => undefined);
    process.exit(0);
  });
}

await Promise.all([1,2,3].map(slotLoop));
