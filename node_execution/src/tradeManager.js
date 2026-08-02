import crypto from 'node:crypto';

const POLICY = Object.freeze({
  id: 'NODE_TRADE_MANAGEMENT_V1',
  tp1R: 1.5,
  tp1ClosePct: 40,
  tp2R: 2.0,
  tp2ClosePct: 30,
  runnerPct: 30,
  trailingDistanceR: 0.5,
});

function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

function positive(value, name) {
  const number = Number(value);
  if (!Number.isFinite(number) || number <= 0) throw new Error(`${name} must be positive`);
  return number;
}

function direction(side) { return side === 'Buy' ? 1 : -1; }
function opposite(side) { return side === 'Buy' ? 'Sell' : 'Buy'; }
function target(entry, risk, side, multiple) { return entry + direction(side) * risk * multiple; }
function reached(mark, price, side) { return side === 'Buy' ? mark >= price : mark <= price; }

function decimals(value) {
  const text = String(value);
  return text.includes('.') ? text.split('.')[1].length : 0;
}

function floorToStep(value, step) {
  const scale = 10 ** Math.max(decimals(step), 0);
  const stepUnits = Math.round(Number(step) * scale);
  const valueUnits = Math.floor((Number(value) * scale) + 1e-9);
  if (!stepUnits || valueUnits <= 0) return '0';
  return ((Math.floor(valueUnits / stepUnits) * stepUnits) / scale).toFixed(decimals(step));
}

function managementLinkId(candidateKey, action) {
  const digest = crypto.createHash('sha256').update(`${candidateKey}:${action}`).digest('hex').slice(0, 24);
  return `n11-${action}-${digest}`.slice(0, 36);
}

export class TradeManager {
  constructor(repository, bybit, config) {
    this.repository = repository;
    this.bybit = bybit;
    this.config = config;
  }

  async cycle(command) {
    if (!['MANAGING', 'PARTIALLY_FILLED'].includes(command.state)) {
      throw new Error(`Step 11 cannot manage command state ${command.state}`);
    }
    const payload = command.payload;
    const symbol = String(payload.symbol || '');
    const side = String(payload.side || '');
    if (!symbol || !['Buy', 'Sell'].includes(side)) throw new Error('Managed trade identity is invalid');

    const [positionResponse, tickerResponse, instrumentResponse] = await Promise.all([
      this.bybit.position(symbol),
      this.bybit.ticker(symbol),
      this.bybit.instrument(symbol),
    ]);
    const position = rows(positionResponse).find((row) => Number(row.size || 0) > 0 && String(row.side) === side);
    if (!position) return this.closeMissingPosition(command, 'EXCHANGE_OR_MANUAL_CLOSE_DETECTED');

    const ticker = rows(tickerResponse)[0];
    const instrument = rows(instrumentResponse)[0];
    if (!ticker || !instrument) throw new Error('Ticker or instrument truth unavailable for management');
    const mark = positive(ticker.markPrice || ticker.lastPrice, 'markPrice');
    const currentQty = positive(position.size, 'position.size');
    const entry = positive(position.avgPrice || payload.entryReference, 'position.avgPrice');
    const initialStop = positive(payload.technicalStopLoss, 'technicalStopLoss');
    const risk = Math.abs(entry - initialStop);
    if (!(risk > 0)) throw new Error('Initial technical risk distance is invalid');

    let state = await this.repository.getManagementState(command.candidateKey);
    if (!state) {
      state = {
        version: 1,
        policyId: POLICY.id,
        candidateKey: command.candidateKey,
        symbol,
        side,
        initialQty: currentQty,
        averageEntry: entry,
        initialStop,
        riskDistance: risk,
        tp1Price: target(entry, risk, side, POLICY.tp1R),
        tp2Price: target(entry, risk, side, POLICY.tp2R),
        tp1Done: false,
        breakEvenDone: false,
        tp2Done: false,
        trailingActive: false,
        trailingStop: null,
        lastObservedQty: currentQty,
        createdAt: Date.now(),
        updatedAt: Date.now(),
      };
      await this.repository.putManagementState(command.candidateKey, state);
      await this.repository.recordOrder(command, 'MANAGEMENT_INITIALIZED', { policy: POLICY, managementState: state });
    }

    state.lastObservedQty = currentQty;
    state.lastMarkPrice = mark;
    state.updatedAt = Date.now();

    if (!state.tp1Done && reached(mark, state.tp1Price, side)) {
      const result = await this.closeFraction(command, state, position, instrument, 'tp1', POLICY.tp1ClosePct);
      state.tp1Done = true;
      state.tp1ClosedQty = result.closedQty;
      await this.moveBreakEven(command, state, position);
      state.breakEvenDone = true;
    }

    const refreshedAfterTp1 = await this.currentPosition(symbol, side);
    if (!refreshedAfterTp1) {
      await this.repository.putManagementState(command.candidateKey, { ...state, closedAt: Date.now(), closeReason: 'POSITION_CLOSED_AFTER_TP1' });
      return this.finishClosed(command, 'POSITION_CLOSED_AFTER_TP1');
    }

    if (!state.tp2Done && reached(mark, state.tp2Price, side)) {
      const result = await this.closeFraction(command, state, refreshedAfterTp1, instrument, 'tp2', POLICY.tp2ClosePct);
      state.tp2Done = true;
      state.tp2ClosedQty = result.closedQty;
      state.trailingActive = true;
    }

    const refreshed = await this.currentPosition(symbol, side);
    if (!refreshed) {
      await this.repository.putManagementState(command.candidateKey, { ...state, closedAt: Date.now(), closeReason: 'POSITION_CLOSED_AFTER_TP2' });
      return this.finishClosed(command, 'POSITION_CLOSED_AFTER_TP2');
    }

    if (state.tp2Done) {
      const proposed = mark - direction(side) * state.riskDistance * POLICY.trailingDistanceR;
      const previous = Number(state.trailingStop || 0);
      const tighter = side === 'Buy' ? Math.max(previous, proposed) : (previous > 0 ? Math.min(previous, proposed) : proposed);
      const valid = side === 'Buy' ? tighter < mark : tighter > mark;
      if (valid && Math.abs(tighter - previous) > Math.max(mark * 1e-7, 1e-8)) {
        await this.bybit.setTradingStop({
          category: this.config.category,
          symbol,
          positionIdx: Number(refreshed.positionIdx || 0),
          tpslMode: 'Full',
          stopLoss: String(tighter),
          slTriggerBy: 'MarkPrice',
          slOrderType: 'Market',
        });
        state.trailingStop = tighter;
        state.trailingActive = true;
        await this.repository.recordOrder(command, 'RUNNER_TRAILING_UPDATED', { markPrice: mark, trailingStop: tighter, distanceR: POLICY.trailingDistanceR });
      }
    }

    await this.repository.putManagementState(command.candidateKey, state);
    await this.repository.recordOrder(command, 'MANAGEMENT_RECONCILED', {
      markPrice: mark,
      positionQty: Number(refreshed.size || 0),
      tp1Done: state.tp1Done,
      tp2Done: state.tp2Done,
      breakEvenDone: state.breakEvenDone,
      trailingActive: state.trailingActive,
      trailingStop: state.trailingStop,
    });
    if (command.state === 'PARTIALLY_FILLED') return this.repository.transition(command, 'MANAGING');
    return command;
  }

  async currentPosition(symbol, side) {
    return rows(await this.bybit.position(symbol)).find((row) => Number(row.size || 0) > 0 && String(row.side) === side) || null;
  }

  async closeFraction(command, state, position, instrument, action, closePct) {
    const lot = instrument.lotSizeFilter || {};
    const step = String(lot.qtyStep || '0');
    const minQty = Number(lot.minOrderQty || 0);
    const desired = Number(state.initialQty) * closePct / 100;
    const available = Number(position.size || 0);
    let quantity = Number(floorToStep(Math.min(desired, available), step));
    if (quantity < minQty) {
      if (available <= minQty + 1e-12) quantity = available;
      else throw new Error(`${action} close quantity is below current Bybit minimum`);
    }
    const linkId = managementLinkId(command.candidateKey, action);
    const prior = await this.bybit.findOrder(command.payload.symbol, linkId);
    const executions = rows(await this.bybit.executions(command.payload.symbol, linkId));
    const priorFilled = executions.reduce((sum, row) => sum + Number(row.execQty || 0), 0);
    if (!prior && priorFilled <= 0) {
      await this.repository.recordOrder(command, `${action.toUpperCase()}_INTENT`, { orderLinkId: linkId, closeQty: quantity, closePct });
      await this.bybit.createOrder({
        category: this.config.category,
        symbol: command.payload.symbol,
        side: opposite(command.payload.side),
        orderType: 'Market',
        qty: String(quantity),
        timeInForce: 'IOC',
        positionIdx: Number(position.positionIdx || 0),
        orderLinkId: linkId,
        reduceOnly: true,
        closeOnTrigger: false,
      });
    }
    const resolution = await this.bybit.waitForResolution(command.payload.symbol, linkId, quantity);
    await this.repository.recordFills(command, resolution.executions || []);
    if (!['FILLED', 'PARTIAL'].includes(resolution.state) || Number(resolution.cumulativeQty || 0) <= 0) {
      throw new Error(`${action} close is not fill-verified; management remains fail-closed`);
    }
    await this.repository.recordOrder(command, `${action.toUpperCase()}_FILLED`, {
      orderLinkId: linkId,
      closePct,
      requestedQty: quantity,
      closedQty: Number(resolution.cumulativeQty || 0),
    });
    return { closedQty: Number(resolution.cumulativeQty || 0) };
  }

  async moveBreakEven(command, state, position) {
    await this.bybit.setTradingStop({
      category: this.config.category,
      symbol: command.payload.symbol,
      positionIdx: Number(position.positionIdx || 0),
      tpslMode: 'Full',
      stopLoss: String(state.averageEntry),
      takeProfit: String(state.tp2Price),
      slTriggerBy: 'MarkPrice',
      tpTriggerBy: 'MarkPrice',
      slOrderType: 'Market',
      tpOrderType: 'Market',
    });
    await this.repository.recordOrder(command, 'BREAK_EVEN_SET', { stopLoss: state.averageEntry, takeProfit: state.tp2Price });
  }

  async closeMissingPosition(command, reason) {
    await this.repository.putManagementState(command.candidateKey, { ...(await this.repository.getManagementState(command.candidateKey) || {}), closedAt: Date.now(), closeReason: reason });
    return this.finishClosed(command, reason);
  }

  async finishClosed(command, reason) {
    let current = command;
    if (current.state === 'PARTIALLY_FILLED') current = await this.repository.transition(current, 'CLOSING');
    else if (current.state === 'MANAGING') current = await this.repository.transition(current, 'CLOSING');
    await this.repository.recordOrder(current, 'POSITION_CLOSED', { reason });
    return this.repository.transition(current, 'CLOSED');
  }
}

export { POLICY as TRADE_MANAGEMENT_POLICY, managementLinkId };
