function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

export async function evaluateFirstTradeGate({ bybit, repository, config }) {
  const reasons = [];
  if (config.baseUrl !== 'https://api-demo.bybit.com') reasons.push('Bybit endpoint is not Demo.');
  if (!config.enabled) reasons.push('NODE_EXECUTION_ENABLED is false.');
  if (!config.firstDemoTradeArmed) reasons.push('FIRST_DEMO_TRADE_ARMED is false.');
  if (config.maxActiveTrades !== 1) reasons.push('FIRST_DEMO_MAX_ACTIVE_TRADES must equal 1.');
  if (config.riskPerTradePct !== 0.25) reasons.push('FIRST_DEMO_RISK_PER_TRADE_PCT must equal 0.25.');

  const [positionsResponse, ordersResponse, activeCommands] = await Promise.all([
    bybit.positions(),
    bybit.activeOrders(),
    repository.activeCommands(),
  ]);

  const openPositions = rows(positionsResponse).filter((row) => Number(row.size || 0) > 0);
  const openOrders = rows(ordersResponse).filter((row) => !['Filled', 'Cancelled', 'Rejected', 'Deactivated', 'Expired'].includes(String(row.orderStatus || '')));
  if (openPositions.length) reasons.push(`Exchange has ${openPositions.length} open position(s).`);
  if (openOrders.length) reasons.push(`Exchange has ${openOrders.length} open order(s).`);
  if (activeCommands.length) reasons.push(`PostgreSQL has ${activeCommands.length} unresolved active command(s).`);

  return {
    ok: reasons.length === 0,
    armed: config.firstDemoTradeArmed,
    demoOnly: true,
    maxActiveTrades: config.maxActiveTrades,
    riskPerTradePct: config.riskPerTradePct,
    openPositionCount: openPositions.length,
    openOrderCount: openOrders.length,
    activeCommandCount: activeCommands.length,
    reasons,
  };
}

export function assertFirstTradeCandidate(command, config) {
  const payload = command?.payload || {};
  const grade = String(payload.grade || payload.signalGrade || '').toUpperCase();
  const qualified = payload.qualified === true || String(payload.executionStatus || '').toUpperCase() === 'AWAITING_NODE_EXECUTION';
  const riskPct = Number(payload.riskPerTradePct ?? payload.riskPct ?? config.riskPerTradePct);
  if (!['A+', 'A'].includes(grade)) throw new Error('First Demo trade requires an A+ or A signal grade.');
  if (!qualified) throw new Error('First Demo trade candidate is not execution-qualified.');
  if (Math.abs(riskPct - config.riskPerTradePct) > 1e-9) throw new Error('First Demo trade risk must equal 0.25%.');
  return true;
}
