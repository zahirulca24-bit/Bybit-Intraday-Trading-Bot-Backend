import test from 'node:test';
import assert from 'node:assert/strict';
import { ExecutionRepository } from '../src/repository.js';

function row(candidateKey, slotId, ownerId, state = 'MANAGING') {
  return {
    candidate_key: candidateKey,
    slot_id: slotId,
    owner_id: ownerId,
    state,
    payload: { symbol: 'BTCUSDT', side: 'Buy' },
    created_at: 1,
    updated_at: 1,
  };
}

class AdoptionClient {
  constructor(rows) {
    this.rows = rows;
    this.released = false;
    this.queries = [];
  }
  async query(sql, params = []) {
    this.queries.push({ sql, params });
    if (sql === 'BEGIN' || sql === 'COMMIT' || sql === 'ROLLBACK') return { rowCount: 0, rows: [] };
    if (sql.includes('pg_advisory_xact_lock')) return { rowCount: 1, rows: [{ ok: true }] };
    if (sql.startsWith('SELECT * FROM execution_commands')) return { rowCount: this.rows.length, rows: this.rows };
    if (sql.startsWith('UPDATE execution_commands')) {
      const [slotId, ownerId, updatedAt, candidateKey] = params;
      const source = this.rows.find((item) => item.candidate_key === candidateKey);
      return { rowCount: 1, rows: [{ ...source, slot_id: slotId, owner_id: ownerId, updated_at: updatedAt }] };
    }
    throw new Error(`Unexpected SQL: ${sql}`);
  }
  release() { this.released = true; }
}

class Pool {
  constructor(client) { this.client = client; }
  async connect() { return this.client; }
}

test('restart adoption assigns exactly one active command to each unique slot', async () => {
  const client = new AdoptionClient([
    row('a', 1, 'old:slot1'),
    row('b', 1, 'old:slot1'),
    row('c', null, 'old'),
  ]);
  const repository = new ExecutionRepository(new Pool(client));

  const adopted = await repository.adoptActiveCommands('new-owner');

  assert.deepEqual(adopted.map((item) => item.slotId), [1, 2, 3]);
  assert.deepEqual(adopted.map((item) => item.ownerId), [
    'new-owner:slot1',
    'new-owner:slot2',
    'new-owner:slot3',
  ]);
  assert.equal(new Set(adopted.map((item) => item.slotId)).size, 3);
  assert.equal(client.released, true);
});

test('ownership recovery fails closed when more than three active commands exist', async () => {
  const client = new AdoptionClient([
    row('a', 1, 'old'),
    row('b', 2, 'old'),
    row('c', 3, 'old'),
    row('d', null, 'old'),
  ]);
  const repository = new ExecutionRepository(new Pool(client));

  await assert.rejects(
    () => repository.adoptActiveCommands('new-owner'),
    /More than three active execution commands/,
  );
  assert.ok(client.queries.some((entry) => entry.sql === 'ROLLBACK'));
});

test('lost leader session is detected before slots continue processing', async () => {
  let destroyed = false;
  const leaderClient = {
    async query(sql) {
      if (sql.includes('SELECT 1')) throw new Error('connection lost');
      return { rows: [] };
    },
    release(force) { destroyed = force === true; },
  };
  const repository = new ExecutionRepository({});
  repository.leaderClient = leaderClient;

  const leader = await repository.verifyLeadership();

  assert.equal(leader, false);
  assert.equal(repository.leaderClient, null);
  assert.equal(destroyed, true);
});

test('transition is fenced by candidate, owner, slot, and expected state', async () => {
  let captured = null;
  const repository = new ExecutionRepository({
    async query(sql, params) {
      captured = { sql, params };
      return { rowCount: 0, rows: [] };
    },
  });

  await assert.rejects(
    () => repository.transition({ candidateKey: 'a', ownerId: 'owner:slot2', slotId: 2, state: 'MANAGING' }, 'CLOSING'),
    /lost ownership, slot, or state changed/,
  );
  assert.ok(captured.sql.includes('slot_id=$5'));
  assert.deepEqual(captured.params.slice(2, 5), ['a', 'owner:slot2', 2]);
});
