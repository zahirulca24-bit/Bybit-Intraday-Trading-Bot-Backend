const ACTIVE = new Set(['RESERVED', 'ORDER_PENDING', 'PARTIALLY_FILLED', 'MANAGING', 'CLOSING']);
const TERMINAL = new Set(['CLOSED', 'FAILED']);

function nowSeconds() { return Math.floor(Date.now() / 1000); }

function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function clone(value) { return value == null ? value : JSON.parse(JSON.stringify(value)); }

export class HybridExecutionRepository {
  constructor(databaseRepository = null) {
    this.database = databaseRepository;
    this.direct = new Map();
    this.management = new Map();
    this.localLeader = false;
    this.databaseLeadership = false;
    this.support = {
      status: databaseRepository ? 'WAIT_RETRY' : 'DEGRADED',
      databaseReady: false,
      migrationVersion: 0,
      lastError: databaseRepository ? null : 'DATABASE_URL is not configured; PostgreSQL support is unavailable',
      supportOnly: true,
      tradeRejectionAuthority: false,
    };
  }

  supportSnapshot() { return { ...this.support }; }

  _degrade(error) {
    this.support = {
      ...this.support,
      status: 'DEGRADED',
      databaseReady: false,
      lastError: String(error?.message || error || 'PostgreSQL support unavailable'),
    };
  }

  _healthy(migrationVersion = this.support.migrationVersion) {
    this.support = {
      ...this.support,
      status: 'PASS',
      databaseReady: true,
      migrationVersion: Number(migrationVersion || 0),
      lastError: null,
    };
  }

  async ping() {
    if (!this.database) return { ok: false, migrationVersion: 0, degraded: true };
    try {
      const result = await this.database.ping();
      if (result?.ok) this._healthy(result.migrationVersion);
      else this._degrade('PostgreSQL execution-command migration is unavailable');
      return { ...result, degraded: !result?.ok };
    } catch (error) {
      this._degrade(error);
      return { ok: false, migrationVersion: 0, degraded: true, reason: this.support.lastError };
    }
  }

  async acquireLeadership() {
    if (!this.database) {
      this.localLeader = true;
      this.databaseLeadership = false;
      return true;
    }
    try {
      const acquired = await this.database.acquireLeadership();
      if (!acquired) return false;
      this.databaseLeadership = true;
      this.localLeader = false;
      return true;
    } catch (error) {
      this._degrade(error);
      this.databaseLeadership = false;
      this.localLeader = true;
      return true;
    }
  }

  async verifyLeadership() {
    if (this.databaseLeadership && this.database) {
      try {
        const ok = await this.database.verifyLeadership();
        if (ok) return true;
        this.databaseLeadership = false;
        this.localLeader = true;
        this._degrade('PostgreSQL leader session was lost; continuing only with in-process direct ownership');
        return true;
      } catch (error) {
        this.databaseLeadership = false;
        this.localLeader = true;
        this._degrade(error);
        return true;
      }
    }
    return this.localLeader;
  }

  async releaseLeadership() {
    this.localLeader = false;
    if (this.databaseLeadership && this.database) {
      try { await this.database.releaseLeadership(); } catch (error) { this._degrade(error); }
    }
    this.databaseLeadership = false;
  }

  acceptDirectCandidate(payload) {
    const candidateKey = String(payload?.candidateKey || '').trim();
    const symbol = String(payload?.symbol || '').trim().toUpperCase();
    const side = String(payload?.side || '');
    const grade = String(payload?.grade || '');
    if (!candidateKey || !symbol || !['Buy', 'Sell'].includes(side)) throw new Error('Direct candidate identity is invalid');
    if (!['A+', 'A'].includes(grade)) throw new Error('Direct candidate must be A+ or A');
    if (payload?.riskApproved !== true || String(payload?.riskStatus) !== 'APPROVED_RISK') {
      throw new Error('Direct candidate must be Entry-Safety approved');
    }
    if (Number(payload?.riskPerTradePct) !== 1) throw new Error('Direct candidate riskPerTradePct must equal 1.0');
    if (payload?.orderSubmitted !== false) throw new Error('Direct candidate must be unsubmitted');

    const existing = this.direct.get(candidateKey);
    if (existing) {
      if (canonical(existing.payload) !== canonical(payload)) {
        throw new Error('candidateKey already exists with different immutable signal/risk facts');
      }
      return { command: clone(existing), duplicate: true };
    }
    const timestamp = nowSeconds();
    const command = {
      candidateKey,
      slotId: null,
      state: 'AVAILABLE',
      payload: clone(payload),
      ownerId: null,
      createdAt: timestamp,
      updatedAt: timestamp,
      source: 'DIRECT',
    };
    this.direct.set(candidateKey, command);
    return { command: clone(command), duplicate: false };
  }

  _memoryCommands() {
    return [...this.direct.values()].map(clone);
  }

  async activeCommands() {
    const byKey = new Map(
      this._memoryCommands()
        .filter((command) => ACTIVE.has(command.state))
        .map((command) => [command.candidateKey, command]),
    );
    if (this.database && this.support.databaseReady) {
      try {
        const result = await this.database.pool.query(
          'SELECT * FROM execution_commands WHERE state = ANY($1::text[]) ORDER BY created_at,candidate_key',
          [[...ACTIVE]],
        );
        for (const row of result.rows || []) {
          if (!byKey.has(row.candidate_key)) {
            byKey.set(row.candidate_key, {
              candidateKey: row.candidate_key,
              slotId: row.slot_id == null ? null : Number(row.slot_id),
              state: row.state,
              payload: row.payload,
              ownerId: row.owner_id,
              createdAt: Number(row.created_at),
              updatedAt: Number(row.updated_at),
              source: 'POSTGRESQL',
            });
          }
        }
      } catch (error) { this._degrade(error); }
    }
    return [...byKey.values()];
  }

  async adoptActiveCommands(ownerBase) {
    if (!this.database || !this.support.databaseReady) return [];
    try {
      const adopted = await this.database.adoptActiveCommands(ownerBase);
      return adopted.map((command) => ({ ...command, source: 'POSTGRESQL' }));
    } catch (error) {
      this._degrade(error);
      return [];
    }
  }

  async claim(ownerId, slotId) {
    if (![1, 2, 3].includes(Number(slotId))) throw new Error('slotId must be 1, 2, or 3');
    const active = await this.activeCommands();
    if (active.length >= 3 || active.some((command) => Number(command.slotId) === Number(slotId))) return null;

    const direct = this._memoryCommands()
      .filter((command) => command.state === 'AVAILABLE')
      .sort((a, b) => a.createdAt - b.createdAt || a.candidateKey.localeCompare(b.candidateKey))[0];
    if (direct) {
      const stored = this.direct.get(direct.candidateKey);
      stored.state = 'RESERVED';
      stored.slotId = Number(slotId);
      stored.ownerId = ownerId;
      stored.updatedAt = nowSeconds();
      return clone(stored);
    }

    if (!this.database || !this.support.databaseReady) return null;
    try {
      const command = await this.database.claim(ownerId, Number(slotId));
      return command ? { ...command, source: 'POSTGRESQL' } : null;
    } catch (error) {
      this._degrade(error);
      return null;
    }
  }

  async ownedActiveCommand(ownerId, slotId) {
    const direct = this._memoryCommands().find(
      (command) => command.ownerId === ownerId
        && Number(command.slotId) === Number(slotId)
        && ACTIVE.has(command.state),
    );
    if (direct) return direct;
    if (!this.database || !this.support.databaseReady) return null;
    try {
      const command = await this.database.ownedActiveCommand(ownerId, Number(slotId));
      return command ? { ...command, source: 'POSTGRESQL' } : null;
    } catch (error) {
      this._degrade(error);
      return null;
    }
  }

  async ownedExecutionCommand(ownerId, slotId) { return this.ownedActiveCommand(ownerId, slotId); }

  async transition(command, nextState) {
    const stored = this.direct.get(command.candidateKey);
    if (stored) {
      stored.state = nextState;
      stored.slotId = command.slotId;
      stored.ownerId = command.ownerId;
      stored.payload = clone(command.payload || stored.payload);
      stored.updatedAt = nowSeconds();
      return clone(stored);
    }
    if (this.database && this.support.databaseReady) {
      try {
        const updated = await this.database.transition(command, nextState);
        return { ...updated, source: 'POSTGRESQL' };
      } catch (error) {
        this._degrade(error);
      }
    }
    const shadow = {
      ...clone(command),
      state: nextState,
      updatedAt: nowSeconds(),
      source: 'DB_SHADOW',
    };
    this.direct.set(command.candidateKey, shadow);
    return clone(shadow);
  }

  async get(candidateKey) {
    const memory = this.direct.get(String(candidateKey));
    if (memory) return clone(memory);
    if (!this.database || !this.support.databaseReady) return null;
    try {
      const command = await this.database.get(candidateKey);
      return command ? { ...command, source: 'POSTGRESQL' } : null;
    } catch (error) { this._degrade(error); return null; }
  }

  async pendingReservedMargin(candidateKey) {
    let amount = this._memoryCommands()
      .filter((command) => command.candidateKey !== candidateKey && ['RESERVED', 'ORDER_PENDING'].includes(command.state))
      .reduce((sum, command) => sum + Number(command.payload?.requiredInitialMarginUsdt || 0), 0);
    if (this.database && this.support.databaseReady) {
      try { amount += Number(await this.database.pendingReservedMargin(candidateKey) || 0); }
      catch (error) { this._degrade(error); }
    }
    return Math.max(0, amount);
  }

  async getManagementState(candidateKey) {
    if (this.management.has(candidateKey)) return clone(this.management.get(candidateKey));
    if (!this.database || !this.support.databaseReady) return null;
    try {
      const state = await this.database.getManagementState(candidateKey);
      if (state) this.management.set(candidateKey, clone(state));
      return state;
    } catch (error) { this._degrade(error); return null; }
  }

  async putManagementState(candidateKey, state) {
    this.management.set(candidateKey, clone(state));
    if (this.database && this.support.databaseReady) {
      try { await this.database.putManagementState(candidateKey, state); }
      catch (error) { this._degrade(error); }
    }
  }

  async recordOrder(command, status, evidence = {}) {
    const stored = this.direct.get(command.candidateKey);
    if (stored) {
      stored.lastOrderEvidence = { status, evidence: clone(evidence), at: nowSeconds() };
      stored.payload = clone(command.payload || stored.payload);
    }
    if (this.database && this.support.databaseReady) {
      try { await this.database.recordOrder(command, status, evidence); }
      catch (error) { this._degrade(error); }
    }
  }

  async recordFills(command, executions = []) {
    if (this.database && this.support.databaseReady) {
      try { await this.database.recordFills(command, executions); }
      catch (error) { this._degrade(error); }
    }
  }

  markExecutionPayload(command, payload) {
    const stored = this.direct.get(command.candidateKey);
    if (stored) {
      stored.payload = clone(payload);
      stored.updatedAt = nowSeconds();
      command.payload = clone(payload);
    }
    return command;
  }

  hasCandidate(candidateKey) { return this.direct.has(String(candidateKey)); }

  terminalCandidates() {
    return this._memoryCommands().filter((command) => TERMINAL.has(command.state));
  }
}
