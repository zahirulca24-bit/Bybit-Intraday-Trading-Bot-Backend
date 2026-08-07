function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

const ACTIVE = ['RESERVED', 'ORDER_PENDING', 'PARTIALLY_FILLED', 'MANAGING', 'CLOSING'];

export async function evaluateFirstTradeGate({ bybit, repository, config }) {
  const reasons = [];
  if (config.baseUrl !== 'https://api-demo.bybit.com') reasons.push('Bybit endpoint is not Demo.');
  if (!config.enabled) reasons.push('NODE_EXECUTION_ENABLED is false.');
  if (config.maxActiveTrades !== 3) reasons.push('MAX_ACTIVE_TRADES must equal 3.');
  if (Number(config.gradeRiskPct?.['A+']) !== 1.0 || Number(config.gradeRiskPct?.A) !== 0.75) {
    reasons.push('Node grade-risk contract must be A+=1.00%, A=0.75%, B+=reject.');
  }

  const [positionsResponse, ordersResponse, activeResult] = await Promise.all([
    bybit.positions(),
    bybit.activeOrders(),
    repository.pool.query('SELECT candidate_key,state FROM execution_commands WHERE state = ANY($1::text[]) ORDER BY created_at', [ACTIVE]),
  ]);

  const openPositions = rows(positionsResponse).filter((row) => Number(row.size || 0) > 0);
  const openOrders = rows(ordersResponse).filter((row) => !['Filled', 'Cancelled', 'Rejected', 'Deactivated', 'Expired'].includes(String(row.orderStatus || '')));
  const activeCommands = activeResult.rows || [];
  if (openPositions.length) reasons.push(`Exchange has ${openPositions.length} open position(s).`);
  if (openOrders.length) reasons.push(`Exchange has ${openOrders.length} open order(s).`);
  if (activeCommands.length) reasons.push(`PostgreSQL has ${activeCommands.length} unresolved active command(s).`);

  return {
    ok: reasons.length === 0,
    armed: config.enabled,
    demoOnly: true,
    maxActiveTrades: config.maxActiveTrades,
    gradeRiskPct: { ...config.gradeRiskPct },
    bPlusRejected: true,
    openPositionCount: openPositions.length,
    openOrderCount: openOrders.length,
    activeCommandCount: activeCommands.length,
    reasons,
  };
}

export function assertFirstTradeCandidate(command, config) {
  const payload = command?.payload || {};
  const grade = String(payload.grade || payload.qualityGrade || payload.signalGrade || '').toUpperCase();
  const qualified = payload.qualified === true || String(payload.executionStatus || '').toUpperCase() === 'AWAITING_NODE_EXECUTION';
  const expectedGradeRiskPct = Number(config.gradeRiskPct?.[grade]);
  const payloadGradeRiskPct = Number(payload.gradeRiskPct);
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
