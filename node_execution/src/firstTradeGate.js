import { orderLinkId } from './bybitClient.js';

function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

const ACTIVE = ['RESERVED', 'ORDER_PENDING', 'PARTIALLY_FILLED', 'MANAGING', 'CLOSING'];

function normalizeCommand(row) {
  if (!row) return null;
  return {
    candidateKey: row.candidateKey ?? row.candidate_key ?? null,
    state: row.state ?? null,
    payload: row.payload ?? {},
  };
}

function sameText(left, right) {
  return String(left || '').trim().toUpperCase() === String(right || '').trim().toUpperCase();
}

function positionIsManaged(position, commands) {
  return commands.some((command) => {
    const payload = command.payload || {};
    return sameText(position.symbol, payload.symbol) && sameText(position.side, payload.side);
  });
}

function orderIsManaged(order, commands) {
  return commands.some((command) => {
    const payload = command.payload || {};
    const expectedLinkId = command.candidateKey ? orderLinkId(command.candidateKey) : '';
    if (expectedLinkId && String(order.orderLinkId || '') === expectedLinkId) return true;
    const reduceOnly = order.reduceOnly === true || String(order.reduceOnly || '').toLowerCase() === 'true';
    return reduceOnly && sameText(order.symbol, payload.symbol);
  });
}

async function activeCommands(repository, adoptedCommands) {
  if (adoptedCommands !== null) return adoptedCommands;
  if (typeof repository.activeCommands === 'function') return repository.activeCommands();
  if (repository?.pool?.query) {
    const result = await repository.pool.query(
      'SELECT candidate_key,state,payload FROM execution_commands WHERE state = ANY($1::text[]) ORDER BY created_at',
      [ACTIVE],
    );
    return result.rows || [];
  }
  return [];
}

export async function evaluateFirstTradeGate({ bybit, repository, config, adoptedCommands = null }) {
  const reasons = [];
  if (config.baseUrl !== 'https://api-demo.bybit.com') reasons.push('Bybit endpoint is not Demo.');
  if (!config.enabled) reasons.push('NODE_EXECUTION_ENABLED is false.');
  if (config.maxActiveTrades !== 3) reasons.push('MAX_ACTIVE_TRADES must equal 3.');
  if (Number(config.gradeRiskPct?.['A+']) !== 1.0 || Number(config.gradeRiskPct?.A) !== 1.0) {
    reasons.push('Node grade-risk contract must be A+=1.00%, A=1.00%, B+=reject.');
  }

  const [positionsResponse, ordersResponse, activeRows] = await Promise.all([
    bybit.positions(),
    bybit.activeOrders(),
    activeCommands(repository, adoptedCommands),
  ]);

  const openPositions = rows(positionsResponse).filter((row) => Number(row.size || 0) > 0);
  const openOrders = rows(ordersResponse).filter((row) => !['Filled', 'Cancelled', 'Rejected', 'Deactivated', 'Expired'].includes(String(row.orderStatus || '')));
  const active = (activeRows || []).map(normalizeCommand).filter(Boolean);

  const orphanPositions = openPositions.filter((position) => !positionIsManaged(position, active));
  const orphanOrders = openOrders.filter((order) => !orderIsManaged(order, active));

  if (active.length > config.maxActiveTrades) {
    reasons.push(`Execution controller has ${active.length} active candidates; maximum is ${config.maxActiveTrades}.`);
  }
  if (orphanPositions.length) reasons.push(`Exchange has ${orphanPositions.length} orphan open position(s).`);
  if (orphanOrders.length) reasons.push(`Exchange has ${orphanOrders.length} orphan open order(s).`);

  const support = typeof repository.supportSnapshot === 'function'
    ? repository.supportSnapshot()
    : { status: 'PASS', databaseReady: true };
  const reconciliationRequired = !support.databaseReady && (orphanPositions.length > 0 || orphanOrders.length > 0);

  return {
    ok: reasons.length === 0,
    armed: config.enabled,
    demoOnly: true,
    maxActiveTrades: config.maxActiveTrades,
    gradeRiskPct: { ...config.gradeRiskPct },
    bPlusRejected: true,
    recoveryMode: active.length > 0,
    recoveryStatus: reconciliationRequired ? 'DEGRADED_RECOVERY' : 'READY',
    recoveryCode: reconciliationRequired ? 'RECONCILIATION_REQUIRED' : null,
    postgresSupportStatus: support.status || 'DEGRADED',
    postgresRequiredForNewCandidate: false,
    openPositionCount: openPositions.length,
    openOrderCount: openOrders.length,
    activeCommandCount: active.length,
    managedPositionCount: openPositions.length - orphanPositions.length,
    managedOrderCount: openOrders.length - orphanOrders.length,
    orphanPositionCount: orphanPositions.length,
    orphanOrderCount: orphanOrders.length,
    reasons,
  };
}

export function assertFirstTradeCandidate(command, config) {
  const payload = command?.payload || {};
  const grade = String(payload.grade || payload.qualityGrade || payload.signalGrade || '').toUpperCase();
  const qualified = payload.qualified === true || String(payload.executionStatus || '').toUpperCase() === 'AWAITING_NODE_EXECUTION';
  const expectedGradeRiskPct = Number(config.gradeRiskPct?.[grade]);
  const payloadGradeRiskPct = Number(payload.gradeRiskPct ?? payload.riskPerTradePct);
  const effectiveRiskPct = Number(payload.effectiveRiskPerTradePct ?? payload.riskPerTradePct ?? payload.riskPct);

  if (!['A+', 'A'].includes(grade) || !Number.isFinite(expectedGradeRiskPct)) {
    throw new Error('Node execution requires an A+ or A signal grade; B+ is rejected.');
  }
  if (!qualified) throw new Error('Node execution candidate is not execution-qualified.');
  if (!Number.isFinite(payloadGradeRiskPct) || Math.abs(payloadGradeRiskPct - expectedGradeRiskPct) > 1e-9) {
    throw new Error(`Node grade-risk contract requires ${grade} at ${expectedGradeRiskPct.toFixed(2)}%.`);
  }
  if (!Number.isFinite(effectiveRiskPct) || effectiveRiskPct <= 0 || effectiveRiskPct - expectedGradeRiskPct > 1e-9) {
    throw new Error(`Node effective risk for ${grade} must be positive and no greater than ${expectedGradeRiskPct.toFixed(2)}%.`);
  }
  return true;
}
