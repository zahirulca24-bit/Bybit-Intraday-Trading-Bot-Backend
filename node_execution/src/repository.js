const ACTIVE = ['RESERVED','ORDER_PENDING','PARTIALLY_FILLED','MANAGING','CLOSING'];
const TRANSITIONS = new Map([
  ['RESERVED', new Set(['ORDER_PENDING','FAILED'])],
  ['ORDER_PENDING', new Set(['PARTIALLY_FILLED','MANAGING','FAILED'])],
  ['PARTIALLY_FILLED', new Set(['MANAGING','CLOSING','FAILED'])],
  ['MANAGING', new Set(['CLOSING','CLOSED','FAILED'])],
  ['CLOSING', new Set(['CLOSED','FAILED'])],
]);

export class ExecutionRepository {
  constructor(pool) { this.pool = pool; }

  async claim(ownerId, slotId) {
    if (![1,2,3].includes(slotId)) throw new Error('slotId must be 1, 2, or 3');
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const occupied = await client.query('SELECT 1 FROM execution_commands WHERE slot_id=$1 AND state = ANY($2) LIMIT 1', [slotId, ACTIVE]);
      if (occupied.rowCount) { await client.query('COMMIT'); return null; }
      const active = await client.query('SELECT COUNT(*)::int AS count FROM execution_commands WHERE state = ANY($1)', [ACTIVE]);
      if (Number(active.rows[0].count) >= 3) { await client.query('COMMIT'); return null; }
      const selected = await client.query("SELECT candidate_key FROM execution_commands WHERE state='AVAILABLE' ORDER BY created_at,candidate_key FOR UPDATE SKIP LOCKED LIMIT 1");
      if (!selected.rowCount) { await client.query('COMMIT'); return null; }
      const result = await client.query("UPDATE execution_commands SET state='RESERVED',slot_id=$1,owner_id=$2,updated_at=$3 WHERE candidate_key=$4 AND state='AVAILABLE' RETURNING *", [slotId, ownerId, Math.floor(Date.now()/1000), selected.rows[0].candidate_key]);
      await client.query('COMMIT');
      return normalize(result.rows[0]);
    } catch (error) {
      await client.query('ROLLBACK');
      throw error;
    } finally { client.release(); }
  }

  async transition(command, nextState) {
    const expected = String(command.state);
    if (!TRANSITIONS.get(expected)?.has(nextState)) throw new Error(`Invalid transition ${expected}->${nextState}`);
    const result = await this.pool.query('UPDATE execution_commands SET state=$1,updated_at=$2 WHERE candidate_key=$3 AND owner_id=$4 AND state=$5 RETURNING *', [nextState, Math.floor(Date.now()/1000), command.candidateKey, command.ownerId, expected]);
    if (!result.rowCount) throw new Error('Execution command transition lost ownership or state changed');
    return normalize(result.rows[0]);
  }

  async get(candidateKey) {
    const result = await this.pool.query('SELECT * FROM execution_commands WHERE candidate_key=$1', [candidateKey]);
    return result.rowCount ? normalize(result.rows[0]) : null;
  }
}

function normalize(row) {
  if (!row) return null;
  return {
    candidateKey: row.candidate_key,
    slotId: Number(row.slot_id),
    state: row.state,
    payload: row.payload,
    ownerId: row.owner_id,
    createdAt: Number(row.created_at),
    updatedAt: Number(row.updated_at),
  };
}
