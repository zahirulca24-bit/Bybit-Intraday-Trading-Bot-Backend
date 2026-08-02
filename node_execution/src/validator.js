import { decimalGte, decimalLte, isStepAligned, positiveNumber } from './decimal.js';

function list(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

export function validateContract(payload, candidateKey = payload?.candidateKey) {
  if (!payload || payload.positionSizingStatus !== 'SIZING_APPROVED' || payload.sizingApproved !== true) {
    throw new Error('Command is not Step-8 sizing approved');
  }
  if (payload.executionStatus !== 'AWAITING_NODE_EXECUTION' || payload.orderSubmitted !== false) {
    throw new Error('Command execution state is invalid');
  }
  if (payload.marginMode !== 'ISOLATED' || Number(payload.leverage) !== 5) {
    throw new Error('Only Isolated 5x is approved');
  }
  const requirements = payload.nodeExecutionRequirements || {};
  if (
    requirements.marginMode !== 'ISOLATED'
    || Number(requirements.leverage) !== 5
    || requirements.revalidateWalletAndInstrumentRules !== true
    || requirements.submitOnlyAfterRevalidation !== true
  ) {
    throw new Error('Node execution revalidation contract is incomplete');
  }
  if (!payload.candidateKey || payload.candidateKey !== candidateKey || !payload.symbol || !['Buy', 'Sell'].includes(payload.side)) {
    throw new Error('Candidate identity is invalid');
  }
  positiveNumber(payload.qty, 'qty');
  positiveNumber(payload.entryReference, 'entryReference');
  positiveNumber(payload.technicalStopLoss, 'technicalStopLoss');
  positiveNumber(payload.takeProfitReference, 'takeProfitReference');
  positiveNumber(payload.requiredInitialMarginUsdt, 'requiredInitialMarginUsdt');
  const entry = Number(payload.entryReference);
  const stop = Number(payload.technicalStopLoss);
  const take = Number(payload.takeProfitReference);
  const buy = payload.side === 'Buy';
  if (buy && !(stop < entry && take > entry)) throw new Error('Buy technical price plan is invalid');
  if (!buy && !(take < entry && stop > entry)) throw new Error('Sell technical price plan is invalid');
  return payload;
}

export function revalidateLive(payload, truth, config, nowSeconds = Math.floor(Date.now() / 1000)) {
  validateContract(payload);
  const wallet = list(truth.wallet)[0];
  const instrument = list(truth.instrument)[0];
  const ticker = list(truth.ticker)[0];
  if (!wallet || !instrument || !ticker) throw new Error('Wallet, instrument, or ticker truth unavailable');

  const sizingAt = Number(payload.sizingDecisionAt || 0);
  if (!Number.isFinite(sizingAt) || sizingAt <= 0 || nowSeconds - sizingAt > config.maxCandidateAgeSeconds || sizingAt - nowSeconds > 30) {
    throw new Error('Sizing-approved candidate is stale or future-dated');
  }

  const equity = positiveNumber(wallet.totalEquity, 'wallet.totalEquity');
  const available = Number(wallet.totalAvailableBalance);
  const currentInitial = Number(wallet.totalInitialMargin);
  if (!Number.isFinite(available) || available < 0 || !Number.isFinite(currentInitial) || currentInitial < 0) {
    throw new Error('Authoritative wallet margin fields are unavailable');
  }

  if (String(instrument.status || '') !== 'Trading') throw new Error('Bybit instrument is not Trading');
  const lot = instrument.lotSizeFilter || {};
  const leverageFilter = instrument.leverageFilter || {};
  const qty = String(payload.qty);
  const qtyNumber = positiveNumber(qty, 'qty');
  const qtyStep = String(lot.qtyStep || '');
  if (!qtyStep || !isStepAligned(qty, qtyStep)) throw new Error('Quantity is not aligned to current Bybit qtyStep');
  if (lot.minOrderQty && !decimalGte(qty, lot.minOrderQty)) throw new Error('Quantity is below current Bybit minimum');
  const maxMarket = lot.maxMktOrderQty || lot.maxMarketOrderQty || lot.maxOrderQty;
  if (maxMarket && !decimalLte(qty, maxMarket)) throw new Error('Quantity exceeds current Bybit market maximum');
  if (Number(leverageFilter.maxLeverage || 0) < config.leverage) throw new Error('Instrument no longer supports approved 5x leverage');

  const mark = positiveNumber(ticker.markPrice || ticker.lastPrice, 'markPrice');
  const notional = qtyNumber * mark;
  if (Number(lot.minNotionalValue || 0) > 0 && notional + 1e-10 < Number(lot.minNotionalValue)) {
    throw new Error('Current notional is below Bybit minNotionalValue');
  }

  const entry = Number(payload.entryReference);
  const stop = Number(payload.technicalStopLoss);
  const take = Number(payload.takeProfitReference);
  const driftPct = Math.abs(mark - entry) / entry * 100;
  if (driftPct > config.maxEntryDriftPct + 1e-12) throw new Error(`Candidate price drift exceeds ${config.maxEntryDriftPct}%`);
  const stopDistance = payload.side === 'Buy' ? mark - stop : stop - mark;
  const takeDistance = payload.side === 'Buy' ? take - mark : mark - take;
  if (!(stopDistance > 0 && takeDistance > 0)) throw new Error('Technical SL/TP is invalid at current mark');
  const grossRr = takeDistance / stopDistance;
  if (grossRr + 1e-12 < config.minimumGrossRr) throw new Error('Current mark no longer provides minimum 1:2 gross RR');

  const positions = list(truth.positions).filter((row) => Number(row.size || 0) > 0);
  if (positions.some((row) => String(row.symbol) === payload.symbol)) throw new Error('Existing symbol position blocks duplicate execution');
  if (positions.length >= 3) throw new Error('Maximum three open positions already reached');
  const activeOrders = list(truth.activeOrders).filter((row) => {
    const status = String(row.orderStatus || '');
    return ['New', 'PartiallyFilled', 'Untriggered'].includes(status)
      && row.reduceOnly !== true && String(row.orderLinkId || '') !== String(truth.orderLinkId || '');
  });
  if (activeOrders.length) throw new Error('Another active entry order exists for this symbol');

  const pendingReserve = Number(truth.pendingReservedMargin || 0);
  if (!Number.isFinite(pendingReserve) || pendingReserve < 0) throw new Error('Pending reserved margin truth is invalid');
  const requiredMargin = notional / config.leverage;
  const perTradeCap = equity * 0.25;
  const combinedCap = equity * 0.60;
  const freeReserve = equity * 0.40;
  const projectedInitial = currentInitial + pendingReserve + requiredMargin;
  const projectedFree = equity - projectedInitial;
  if (requiredMargin > perTradeCap + 1e-8) throw new Error('Per-trade 25% margin cap violated');
  if (requiredMargin > Math.max(0, available - pendingReserve) + 1e-8) throw new Error('Available margin is insufficient after pending reservations');
  if (projectedInitial > combinedCap + 1e-8 || projectedFree + 1e-8 < freeReserve) {
    throw new Error('Combined 60% margin cap or 40% free reserve violated');
  }

  return {
    equity,
    available,
    currentInitialMargin: currentInitial,
    pendingReservedMargin: pendingReserve,
    requiredInitialMargin: requiredMargin,
    projectedInitialMargin: projectedInitial,
    projectedFreeMargin: projectedFree,
    markPrice: mark,
    entryDriftPct: driftPct,
    grossRr,
    notional,
    openPositionCount: positions.length,
  };
}
