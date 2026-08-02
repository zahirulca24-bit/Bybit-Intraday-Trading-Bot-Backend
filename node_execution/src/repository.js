const ACTIVE = ['RESERVED', 'ORDER_PENDING', 'PARTIALLY_FILLED', 'MANAGING', 'CLOSING'];
const TRANSITIONS = new Map([
  ['RESERVED', new Set(['ORDER_PENDING', 'FAILED'])],
  ['ORDER_PENDING', new Set(['PARTIALLY_FILLED', 'MANAGING', 'FAILED'])],
  ['PARTIALLY_FILLED', new Set(['MANAGING', 'CLOSING', 'FAILED'])],
  ['MANAGING', new Set(['CLOSING', 'CLOSED', 'FAILED'])],
  ['CLOSING', new Set(['CLOSED', 'FAILED'])],
]);

function nowSeconds() { return Math.floor(Date.now() / 1000); }
function managementKey(candidateKey) { return `node_trade_management:${candidateKey}`; }

export class ExecutionRepository {
  constructor(pool) { this.pool = pool; this.leaderClient = null; }

  async acquireLeadership() {
    if (this.leaderClient) return true;
    const client = await this.pool.connect();
    try {
      const result = await client.query('SELECT pg_try_advisory_lock($1) AS leader', [21010]);
      if (result.rows[0]?.leader !== true) { client.release(); return false; }
      this.leaderClient = client;
      return true;
    } catch (error) { client.release(); throw error; }
  }

  async releaseLeadership() {
    const client = this.leaderClient;
    this.leaderClient = null;
    if (!client) return;
    try { await client.query('SELECT pg_advisory_unlock($1)', [21010]); } finally { client.release(); }
  }

  async ping() {
    const result = await this.pool.query("SELECT COALESCE(MAX(version),0)::int AS version, to_regclass('public.execution_commands') AS table_name FROM schema_migrations");
    const row = result.rows[0] || {};
    return { ok: Number(row.version) >= 5 && row.table_name === 'execution_commands', migrationVersion: Number(row.version || 0) };
  }

  async claim(ownerId, slotId) {
    if (![1,2,3].includes(slotId)) throw new Error('slotId must be 1, 2, or 3');
    const client = await this.pool.connect();
    try {
      await client.query('BEGIN');
      const occupied = await client.query('SELECT 1 FROM execution_commands WHERE slot_id=$1 AND state = ANY($2::text[]) LIMIT 1', [slotId, ACTIVE]);
      if (occupied.rowCount) { await client.query('COMMIT'); return null; }
      const active = await client.query('SELECT COUNT(*)::int AS count FROM execution_commands WHERE state = ANY($1::text[])', [ACTIVE]);
      if (Number(active.rows[0].count) >= 3) { await client.query('COMMIT'); return null; }
      const selected = await client.query("SELECT candidate_key FROM execution_commands WHERE state='AVAILABLE' ORDER BY created_at,candidate_key FOR UPDATE SKIP LOCKED LIMIT 1");
      if (!selected.rowCount) { await client.query('COMMIT'); return null; }
      const result = await client.query("UPDATE execution_commands SET state='RESERVED',slot_id=$1,owner_id=$2,updated_at=$3 WHERE candidate_key=$4 AND state='AVAILABLE' RETURNING *", [slotId, ownerId, nowSeconds(), selected.rows[0].candidate_key]);
      await client.query('COMMIT');
      return normalize(result.rows[0]);
    } catch (error) {
      await client.query('ROLLBACK').catch(() => undefined);
      throw error;
    } finally { client.release(); }
  }

  async ownedActiveCommand(ownerId, slotId) {
    const result = await this.pool.query('SELECT * FROM execution_commands WHERE owner_id=$1 AND slot_id=$2 AND state = ANY($3::text[]) ORDER BY updated_at LIMIT 1', [ownerId, slotId, ACTIVE]);
    return result.rowCount ? normalize(result.rows[0]) : null;
  }

  async ownedExecutionCommand(ownerId, slotId) { return this.ownedActiveCommand(ownerId, slotId); }

  async transition(command, nextState) {
    const expected = String(command.state);
    if (!TRANSITIONS.get(expected)?.has(nextState)) throw new Error(`Invalid transition ${expected}->${nextState}`);
    const result = await this.pool.query('UPDATE execution_commands SET state=$1,updated_at=$2 WHERE candidate_key=$3 AND owner_id=$4 AND state=$5 RETURNING *', [nextState, nowSeconds(), command.candidateKey, command.ownerId, expected]);
    if (!result.rowCount) throw new Error('Execution command transition lost ownership or state changed');
    return normalize(result.rows[0]);
  }

  async get(candidateKey) {
    const result = await this.pool.query('SELECT * FROM execution_commands WHERE candidate_key=$1', [candidateKey]);
    return result.rowCount ? normalize(result.rows[0]) : null;
  }

  async pendingReservedMargin(candidateKey) {
    const result = await this.pool.query(`SELECT COALESCE(SUM(CASE WHEN (payload->>'requiredInitialMarginUsdt') ~ '^[0-9]+(\\.[0-9]+)?$' THEN (payload->>'requiredInitialMarginUsdt')::numeric ELSE 0 END),0)::text AS amount FROM execution_commands WHERE candidate_key<>$1 AND state = ANY($2::text[])`, [candidateKey, ['RESERVED','ORDER_PENDING']]);
    return Number(result.rows[0]?.amount || 0);
  }

  async getManagementState(candidateKey) {
    const result = await this.pool.query('SELECT value FROM runtime_state WHERE key=$1', [managementKey(candidateKey)]);
    return result.rowCount ? result.rows[0].value : null;
  }

  async putManagementState(candidateKey, state) {
    await this.pool.query(`INSERT INTO runtime_state(key,value,updated_at) VALUES($1,$2::jsonb,$3) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_at=EXCLUDED.updated_at`, [managementKey(candidateKey), JSON.stringify(state), nowSeconds()]);
  }

  async recordOrder(command, status, evidence = {}) {
    const payload = { candidateKey: command.candidateKey, slotId: command.slotId, ownerId: command.ownerId, commandState: command.state, status, ...evidence };
    await this.pool.query(`INSERT INTO orders(order_key,symbol,side,status,payload,updated_at) VALUES($1,$2,$3,$4,$5::jsonb,$6) ON CONFLICT(order_key) DO UPDATE SET symbol=EXCLUDED.symbol,side=EXCLUDED.side,status=EXCLUDED.status,payload=EXCLUDED.payload,updated_at=EXCLUDED.updated_at`, [command.candidateKey, command.payload?.symbol, command.payload?.side, status, JSON.stringify(payload), nowSeconds()]);
  }

  async recordFills(command, executions = []) {
    for (const execution of executions) {
      const fillKey = String(execution.execId || '').trim();
      if (!fillKey || Number(execution.execQty || 0) <= 0) continue;
      await this.pool.query(`INSERT INTO fills(fill_key,order_key,symbol,qty,price,payload,created_at) VALUES($1,$2,$3,$4,$5,$6::jsonb,$7) ON CONFLICT(fill_key) DO NOTHING`, [fillKey, command.candidateKey, command.payload?.symbol, execution.execQty, execution.execPrice, JSON.stringify({ ...execution, candidateKey: command.candidateKey }), Math.floor(Number(execution.execTime || Date.now()) / 1000)]);
    }
  }
}

function normalize(row) {
  if (!row) return null;
  return { candidateKey: row.candidate_key, slotId: row.slot_id == null ? null : Number(row.slot_id), state: row.state, payload: row.payload, ownerId: row.owner_id, createdAt: Number(row.created_at), updatedAt: Number(row.updated_at) };
}
