import crypto from 'node:crypto';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function stableQuery(params) {
  return new URLSearchParams(
    Object.entries(params)
      .filter(([, value]) => value !== undefined && value !== null && value !== '')
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => [key, String(value)]),
  ).toString();
}

function apiError(json, status) {
  const error = new Error(String(json?.retMsg || `Bybit HTTP ${status}`));
  error.retCode = Number(json?.retCode ?? status);
  error.response = json;
  return error;
}

function orderRows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

export class BybitClient {
  constructor(config, fetchImpl = fetch) {
    this.config = config;
    this.fetch = fetchImpl;
    this.clockOffsetMs = 0;
  }

  async fetchJson(url, init) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.config.requestTimeoutMs);
    try {
      const response = await this.fetch(url, { ...init, signal: controller.signal });
      const text = await response.text();
      let json;
      try { json = JSON.parse(text); } catch { throw new Error(`Bybit returned invalid JSON (${response.status})`); }
      if (!response.ok || Number(json.retCode) !== 0) throw apiError(json, response.status);
      return json;
    } finally {
      clearTimeout(timer);
    }
  }

  async publicRequest(path, params = {}) {
    const query = stableQuery(params);
    return this.fetchJson(`${this.config.baseUrl}${path}${query ? `?${query}` : ''}`, {
      method: 'GET',
      headers: { 'Content-Type': 'application/json' },
    });
  }

  async syncClock() {
    const response = await this.publicRequest('/v5/market/time');
    const serverMs = Number(response.time || Number(response?.result?.timeSecond || 0) * 1000);
    if (Number.isFinite(serverMs) && serverMs > 0) this.clockOffsetMs = serverMs - Date.now();
    return this.clockOffsetMs;
  }

  async request(method, path, params = {}, retryClock = true) {
    const timestamp = String(Date.now() + this.clockOffsetMs);
    const body = method === 'GET' ? '' : JSON.stringify(params);
    const query = method === 'GET' ? stableQuery(params) : '';
    const signaturePayload = timestamp + this.config.apiKey + this.config.recvWindow + (method === 'GET' ? query : body);
    const signature = crypto.createHmac('sha256', this.config.apiSecret).update(signaturePayload).digest('hex');
    try {
      return await this.fetchJson(`${this.config.baseUrl}${path}${query ? `?${query}` : ''}`, {
        method,
        headers: {
          'Content-Type': 'application/json',
          'X-BAPI-API-KEY': this.config.apiKey,
          'X-BAPI-TIMESTAMP': timestamp,
          'X-BAPI-RECV-WINDOW': String(this.config.recvWindow),
          'X-BAPI-SIGN': signature,
        },
        body: method === 'GET' ? undefined : body,
      });
    } catch (error) {
      if (retryClock && Number(error.retCode) === 10002) {
        await this.syncClock();
        return this.request(method, path, params, false);
      }
      throw error;
    }
  }

  accountInfo() { return this.request('GET', '/v5/account/info'); }
  wallet() { return this.request('GET', '/v5/account/wallet-balance', { accountType: 'UNIFIED', coin: 'USDT' }); }
  instrument(symbol) { return this.publicRequest('/v5/market/instruments-info', { category: 'linear', symbol }); }
  ticker(symbol) { return this.publicRequest('/v5/market/tickers', { category: 'linear', symbol }); }
  position(symbol) { return this.request('GET', '/v5/position/list', { category: 'linear', symbol }); }
  positions() { return this.request('GET', '/v5/position/list', { category: 'linear', settleCoin: 'USDT', limit: 200 }); }
  activeOrders(symbol) {
    return this.request('GET', '/v5/order/realtime', {
      category: 'linear',
      ...(symbol ? { symbol } : { settleCoin: 'USDT' }),
      openOnly: 0,
      limit: 50,
    });
  }
  realtimeOrder(symbol, orderLinkId) { return this.request('GET', '/v5/order/realtime', { category: 'linear', symbol, orderLinkId }); }
  orderHistory(symbol, orderLinkId) { return this.request('GET', '/v5/order/history', { category: 'linear', symbol, orderLinkId, limit: 20 }); }
  executions(symbol, orderLinkId) { return this.request('GET', '/v5/execution/list', { category: 'linear', symbol, orderLinkId, limit: 100 }); }
  setMarginMode() { return this.request('POST', '/v5/account/set-margin-mode', { setMarginMode: 'ISOLATED_MARGIN' }); }
  async setLeverage(symbol, leverage = 5) {
    try {
      return await this.request('POST', '/v5/position/set-leverage', {
        category: 'linear', symbol, buyLeverage: String(leverage), sellLeverage: String(leverage),
      });
    } catch (error) {
      if (Number(error.retCode) === 110043) return { retCode: 0, retMsg: 'Leverage already set', result: {}, alreadySet: true };
      throw error;
    }
  }
  createOrder(payload) { return this.request('POST', '/v5/order/create', payload); }
  setTradingStop(payload) { return this.request('POST', '/v5/position/trading-stop', payload); }

  async findOrder(symbol, orderLinkId) {
    const realtime = await this.realtimeOrder(symbol, orderLinkId);
    const current = orderRows(realtime)[0];
    if (current) return current;
    const history = await this.orderHistory(symbol, orderLinkId);
    return orderRows(history)[0] || null;
  }

  async waitForResolution(symbol, orderLinkId, expectedQty = 0) {
    const deadline = Date.now() + this.config.fillTimeoutMs;
    let lastOrder = null;
    let lastExecutions = [];
    while (Date.now() < deadline) {
      [lastOrder, lastExecutions] = await Promise.all([
        this.findOrder(symbol, orderLinkId),
        this.executions(symbol, orderLinkId).then(orderRows),
      ]);
      const requestedQty = Number(lastOrder?.qty || expectedQty || 0);
      const cumulativeQty = Math.max(
        Number(lastOrder?.cumExecQty || 0),
        lastExecutions.reduce((sum, row) => sum + Number(row.execQty || 0), 0),
      );
      const status = String(lastOrder?.orderStatus || '');
      if (cumulativeQty > 0 && (status === 'Filled' || (requestedQty > 0 && cumulativeQty + 1e-12 >= requestedQty))) {
        return { state: 'FILLED', order: lastOrder, executions: lastExecutions, cumulativeQty };
      }
      if (cumulativeQty > 0 && (status === 'PartiallyFilled' || (requestedQty > 0 && cumulativeQty < requestedQty))) {
        return { state: 'PARTIAL', order: lastOrder, executions: lastExecutions, cumulativeQty };
      }
      if (['Cancelled', 'Rejected', 'Deactivated', 'Expired'].includes(status)) {
        return { state: cumulativeQty > 0 ? 'PARTIAL' : 'FAILED', order: lastOrder, executions: lastExecutions, cumulativeQty };
      }
      await sleep(this.config.fillPollMs);
    }
    const cumulativeQty = Math.max(
      Number(lastOrder?.cumExecQty || 0),
      lastExecutions.reduce((sum, row) => sum + Number(row.execQty || 0), 0),
    );
    return { state: cumulativeQty > 0 ? 'PARTIAL' : 'UNKNOWN', order: lastOrder, executions: lastExecutions, cumulativeQty };
  }

  async ensureProtection(payload) {
    const read = async () => orderRows(await this.position(payload.symbol)).find((row) => Number(row.size || 0) > 0);
    let position = await read();
    if (!position) throw new Error('Filled execution has no matching open position truth');
    const expectedStop = Number(payload.technicalStopLoss);
    const expectedTake = Number(payload.takeProfitReference);
    const protectedNow = Number(position.stopLoss || 0) > 0 && Number(position.takeProfit || 0) > 0;
    if (!protectedNow) {
      await this.setTradingStop({
        category: 'linear',
        symbol: payload.symbol,
        positionIdx: Number(position.positionIdx || 0),
        tpslMode: 'Full',
        stopLoss: String(payload.technicalStopLoss),
        takeProfit: String(payload.takeProfitReference),
        slTriggerBy: 'MarkPrice',
        tpTriggerBy: 'MarkPrice',
        slOrderType: 'Market',
        tpOrderType: 'Market',
      });
      position = await read();
    }
    if (!position || Number(position.stopLoss || 0) <= 0 || Number(position.takeProfit || 0) <= 0) {
      throw new Error('Mandatory stop-loss/take-profit protection is not verified');
    }
    const stopTolerance = Math.max(Math.abs(expectedStop) * 1e-6, 1e-10);
    const takeTolerance = Math.max(Math.abs(expectedTake) * 1e-6, 1e-10);
    if (Math.abs(Number(position.stopLoss) - expectedStop) > stopTolerance || Math.abs(Number(position.takeProfit) - expectedTake) > takeTolerance) {
      throw new Error('Exchange protection prices differ from the approved technical plan');
    }
    return position;
  }
}

export function orderLinkId(candidateKey) {
  return `n10-${crypto.createHash('sha256').update(String(candidateKey)).digest('hex').slice(0, 28)}`;
}
