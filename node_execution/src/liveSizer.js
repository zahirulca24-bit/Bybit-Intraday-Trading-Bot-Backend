function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

function positive(value, label) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new ExecutionWaitError('WALLET_DATA_WAIT', `${label} must be positive`);
  return number;
}

function decimalPlaces(value) {
  const text = String(value ?? '');
  if (/e-/i.test(text)) return Number(text.split(/e-/i)[1] || 0);
  return text.includes('.') ? text.split('.')[1].length : 0;
}

function floorToStep(value, step) {
  const numericStep = Number(step);
  if (!(numericStep > 0) || !(value > 0)) return 0;
  const scale = 10 ** Math.min(12, Math.max(decimalPlaces(step), 0));
  const stepUnits = Math.max(1, Math.round(numericStep * scale));
  const valueUnits = Math.floor(value * scale + 1e-9);
  return Math.floor(valueUnits / stepUnits) * stepUnits / scale;
}

function floorPrice(value, tick) {
  const numericTick = Number(tick);
  if (!(numericTick > 0)) return value;
  return floorToStep(value, numericTick);
}

function ceilPrice(value, tick) {
  const numericTick = Number(tick);
  if (!(numericTick > 0)) return value;
  const floored = floorPrice(value, numericTick);
  return floored + 1e-12 >= value ? floored : floored + numericTick;
}

function candidateCreatedSeconds(payload) {
  const raw = Number(payload?.createdAt || payload?.riskDecisionAt || 0);
  if (!Number.isFinite(raw) || raw <= 0) return 0;
  return raw > 1e12 ? Math.floor(raw / 1000) : Math.floor(raw);
}

export class ExecutionWaitError extends Error {
  constructor(code, message, retryable = true) {
    super(message);
    this.name = 'ExecutionWaitError';
    this.code = code;
    this.retryable = retryable;
  }
}

export function normalizeClosedFifteenMinuteCandles(response, nowMs = Date.now()) {
  const intervalMs = 15 * 60 * 1000;
  return rows(response)
    .map((row) => Array.isArray(row) ? {
      time: Number(row[0]),
      open: Number(row[1]),
      high: Number(row[2]),
      low: Number(row[3]),
      close: Number(row[4]),
    } : {
      time: Number(row?.time ?? row?.startTime),
      open: Number(row?.open),
      high: Number(row?.high),
      low: Number(row?.low),
      close: Number(row?.close),
    })
    .filter((row) => Number.isFinite(row.time) && row.time > 0
      && Number.isFinite(row.high) && Number.isFinite(row.low)
      && row.high > 0 && row.low > 0
      && row.time + intervalMs <= nowMs)
    .sort((left, right) => left.time - right.time);
}

function technicalPlan(payload, truth, config, mark, nowMs) {
  const candles = normalizeClosedFifteenMinuteCandles(truth.kline15m, nowMs);
  const lookback = Math.max(1, Number(config.structureLookback || 12));
  if (candles.length < lookback) {
    throw new ExecutionWaitError('TECHNICAL_PLAN_WAIT', `Need at least ${lookback} fully closed 15M candles`);
  }
  const structural = candles.slice(-lookback);
  const tick = Number(rows(truth.instrument)[0]?.priceFilter?.tickSize || 0);
  let stop;
  let take;
  if (payload.side === 'Buy') {
    stop = Math.min(...structural.map((row) => row.low));
    stop = floorPrice(stop, tick);
    if (!(stop > 0 && stop < mark)) {
      throw new ExecutionWaitError('TECHNICAL_PLAN_WAIT', 'Closed-15M Buy structure does not provide a valid stop below current mark');
    }
    const riskDistance = mark - stop;
    take = ceilPrice(mark + riskDistance * config.minimumGrossRr, tick);
  } else {
    stop = Math.max(...structural.map((row) => row.high));
    stop = ceilPrice(stop, tick);
    if (!(stop > mark)) {
      throw new ExecutionWaitError('TECHNICAL_PLAN_WAIT', 'Closed-15M Sell structure does not provide a valid stop above current mark');
    }
    const riskDistance = stop - mark;
    take = floorPrice(mark - riskDistance * config.minimumGrossRr, tick);
    if (!(take > 0)) throw new ExecutionWaitError('TECHNICAL_PLAN_WAIT', 'Closed-15M Sell structure produces an invalid target');
  }
  const stopDistance = Math.abs(mark - stop);
  const takeDistance = Math.abs(take - mark);
  const grossRr = takeDistance / stopDistance;
  if (!(stopDistance > 0) || grossRr + 1e-9 < config.minimumGrossRr) {
    throw new ExecutionWaitError('TECHNICAL_PLAN_WAIT', 'Current structural plan does not provide minimum 2R');
  }
  return {
    technicalStopLoss: stop,
    takeProfitReference: take,
    stopDistance,
    grossRr,
    structureLookback: lookback,
    latestClosed15mCandleTime: structural[structural.length - 1].time,
  };
}

function openPositions(truth) {
  return rows(truth.positions).filter((row) => Number(row.size || 0) > 0);
}

function activeEntryOrders(truth, orderLinkId) {
  return rows(truth.activeOrders).filter((row) => {
    const status = String(row.orderStatus || '');
    const reduceOnly = row.reduceOnly === true || String(row.reduceOnly || '').toLowerCase() === 'true';
    return ['New', 'PartiallyFilled', 'Untriggered', 'Created'].includes(status)
      && !reduceOnly
      && String(row.orderLinkId || '') !== String(orderLinkId || '');
  });
}

export function buildLiveExecutionPlan(payload, truth, config, nowSeconds = Math.floor(Date.now() / 1000)) {
  const wallet = rows(truth.wallet)[0];
  const instrument = rows(truth.instrument)[0];
  const ticker = rows(truth.ticker)[0];
  if (!wallet) throw new ExecutionWaitError('WALLET_DATA_WAIT', 'Current Bybit wallet truth is unavailable');
  if (!instrument) throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'Current Bybit instrument rules are unavailable');
  if (!ticker) throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'Current Bybit ticker truth is unavailable');
  if (String(instrument.status || '') !== 'Trading') {
    throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'Bybit instrument is not currently Trading');
  }

  const createdAt = candidateCreatedSeconds(payload);
  if (!createdAt || nowSeconds - createdAt > config.maxCandidateAgeSeconds || createdAt - nowSeconds > 30) {
    throw new ExecutionWaitError('CANDIDATE_STALE', 'Risk-approved candidate is stale or future-dated', false);
  }

  const equity = positive(wallet.totalEquity, 'wallet.totalEquity');
  const available = Number(wallet.totalAvailableBalance);
  if (!Number.isFinite(available) || available < 0) {
    throw new ExecutionWaitError('WALLET_DATA_WAIT', 'Current available margin is unavailable');
  }
  const mark = positive(ticker.markPrice || ticker.lastPrice, 'markPrice');
  const entryReference = positive(payload.entryReference, 'entryReference');
  const driftPct = Math.abs(mark - entryReference) / entryReference * 100;
  if (driftPct > config.maxEntryDriftPct + 1e-12) {
    throw new ExecutionWaitError('CANDIDATE_STALE', `Candidate price drift exceeds ${config.maxEntryDriftPct}%`, false);
  }

  const positions = openPositions(truth);
  if (positions.some((row) => String(row.symbol) === String(payload.symbol))) {
    throw new ExecutionWaitError('DUPLICATE_SYMBOL', 'Existing same-symbol position blocks duplicate execution');
  }
  if (positions.length >= config.maxActiveTrades) {
    throw new ExecutionWaitError('MAX_ACTIVE_TRADES', 'Maximum three active positions already reached');
  }
  if (activeEntryOrders(truth, truth.orderLinkId).length) {
    throw new ExecutionWaitError('DUPLICATE_SYMBOL', 'Another active same-symbol entry order exists');
  }

  const plan = technicalPlan(payload, truth, config, mark, nowSeconds * 1000);
  const riskBudgetUsdt = equity * 0.01;
  const rawRiskQty = riskBudgetUsdt / plan.stopDistance;
  if (!(rawRiskQty > 0)) throw new ExecutionWaitError('NODE_SIZING_WAIT', 'Risk-derived quantity is unavailable');

  const lot = instrument.lotSizeFilter || {};
  const qtyStep = Number(lot.qtyStep || 0);
  const minOrderQty = Number(lot.minOrderQty || 0);
  const maxOrderQty = Number(lot.maxMktOrderQty || lot.maxMarketOrderQty || lot.maxOrderQty || Number.POSITIVE_INFINITY);
  const minNotionalValue = Number(lot.minNotionalValue || 0);
  if (!(qtyStep > 0) || !(minOrderQty > 0) || !(maxOrderQty > 0)) {
    throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'Bybit quantity rules are incomplete');
  }

  const leverageFilter = instrument.leverageFilter || {};
  const supportedMax = Number(leverageFilter.maxLeverage || config.leverage);
  if (!(supportedMax > 0)) throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'Instrument leverage limit is unavailable');
  const leverage = Math.min(config.leverage, supportedMax, 10);
  if (!(leverage > 0)) throw new ExecutionWaitError('INSTRUMENT_RULE_WAIT', 'No safe isolated leverage is available');

  const pendingReservedMargin = Number(truth.pendingReservedMargin || 0);
  if (!Number.isFinite(pendingReservedMargin) || pendingReservedMargin < 0) {
    throw new ExecutionWaitError('WALLET_DATA_WAIT', 'Pending reserved execution exposure is unavailable');
  }
  const remainingAvailable = Math.max(0, available - pendingReservedMargin);
  const marginQtyCap = remainingAvailable * leverage / mark;
  const unrounded = Math.min(rawRiskQty, maxOrderQty, marginQtyCap);
  const qty = floorToStep(unrounded, qtyStep);
  if (!(qty > 0) || qty + 1e-12 < minOrderQty) {
    throw new ExecutionWaitError('INSUFFICIENT_MARGIN', 'Risk-derived quantity cannot satisfy current minimum order quantity');
  }
  const notional = qty * mark;
  if (minNotionalValue > 0 && notional + 1e-9 < minNotionalValue) {
    throw new ExecutionWaitError('INSUFFICIENT_MARGIN', 'Risk-derived quantity cannot satisfy current minNotionalValue without increasing risk');
  }
  const plannedStopRiskUsdt = qty * plan.stopDistance;
  if (plannedStopRiskUsdt > riskBudgetUsdt + Math.max(1e-8, riskBudgetUsdt * 1e-9)) {
    throw new ExecutionWaitError('NODE_SIZING_WAIT', 'Rounded quantity exceeds the fixed 1% stop-risk budget');
  }
  const requiredInitialMarginUsdt = notional / leverage;
  if (requiredInitialMarginUsdt > remainingAvailable + 1e-8) {
    throw new ExecutionWaitError('INSUFFICIENT_MARGIN', 'Available margin cannot support the risk-derived quantity');
  }

  return {
    ...payload,
    entryReference: mark,
    executableMarkPrice: mark,
    technicalStopLoss: plan.technicalStopLoss,
    takeProfitReference: plan.takeProfitReference,
    qty: String(qty),
    requiredInitialMarginUsdt,
    riskBudgetUsdt,
    plannedStopRiskUsdt,
    effectiveRiskPerTradePct: plannedStopRiskUsdt / equity * 100,
    riskPerTradePct: 1.0,
    marginMode: 'ISOLATED',
    leverage,
    sizingAuthority: 'NODE_LIVE_BYBIT_TRUTH',
    nodeSizingStatus: 'NODE_SIZING_READY',
    sizingApproved: true,
    positionSizingStatus: 'NODE_SIZING_READY',
    executionStatus: 'AWAITING_NODE_EXECUTION',
    nodeSizingDecisionAt: nowSeconds,
    nodeSizingEvidence: {
      equity,
      availableMargin: available,
      pendingReservedMargin,
      remainingAvailableMargin: remainingAvailable,
      entryReferenceBeforeLiveSizing: entryReference,
      entryDriftPct: driftPct,
      stopDistance: plan.stopDistance,
      grossRr: plan.grossRr,
      structureLookback: plan.structureLookback,
      latestClosed15mCandleTime: plan.latestClosed15mCandleTime,
      qtyStep,
      minOrderQty,
      maxOrderQty,
      minNotionalValue,
      rawRiskQty,
      marginQtyCap,
    },
  };
}

export { floorToStep };
