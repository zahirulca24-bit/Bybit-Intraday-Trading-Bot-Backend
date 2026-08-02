import crypto from 'node:crypto';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const stableQuery = (params) => new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null).sort(([a], [b]) => a.localeCompare(b))).toString();

export class BybitClient {
  constructor(config, fetchImpl = fetch) {
    this.config = config;
    this.fetch = fetchImpl;
  }

  async request(method, path, params = {}) {
    const timestamp = Date.now().toString();
    const body = method === 'GET' ? '' : JSON.stringify(params);
    const query = method === 'GET' ? stableQuery(params) : '';
    const payload = timestamp + this.config.apiKey + this.config.recvWindow + (method === 'GET' ? query : body);
    const signature = crypto.createHmac('sha256', this.config.apiSecret).update(payload).digest('hex');
    const response = await this.fetch(`${this.config.baseUrl}${path}${query ? `?${query}` : ''}`, {
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
    const json = await response.json();
    if (!response.ok || Number(json.retCode) !== 0) {
      const error = new Error(json.retMsg || `Bybit HTTP ${response.status}`);
      error.retCode = json.retCode;
      error.response = json;
      throw error;
    }
    return json;
  }

  wallet() { return this.request('GET', '/v5/account/wallet-balance', { accountType: 'UNIFIED', coin: 'USDT' }); }
  instrument(symbol) { return this.request('GET', '/v5/market/instruments-info', { category: 'linear', symbol }); }
  position(symbol) { return this.request('GET', '/v5/position/list', { category: 'linear', symbol }); }
  openOrder(symbol, orderLinkId) { return this.request('GET', '/v5/order/realtime', { category: 'linear', symbol, orderLinkId }); }
  executions(symbol, orderLinkId) { return this.request('GET', '/v5/execution/list', { category: 'linear', symbol, orderLinkId }); }
  setMarginMode() { return this.request('POST', '/v5/account/set-margin-mode', { setMarginMode: 'ISOLATED_MARGIN' }); }
  setLeverage(symbol, leverage = 5) { return this.request('POST', '/v5/position/set-leverage', { category: 'linear', symbol, buyLeverage: String(leverage), sellLeverage: String(leverage) }); }
  createOrder(payload) { return this.request('POST', '/v5/order/create', payload); }

  async waitForFill(symbol, orderLinkId) {
    const deadline = Date.now() + this.config.fillTimeoutMs;
    let last = null;
    while (Date.now() < deadline) {
      const order = await this.openOrder(symbol, orderLinkId);
      const row = order?.result?.list?.[0];
      if (row) {
        last = row;
        const status = String(row.orderStatus || '');
        const qty = Number(row.qty || 0);
        const filled = Number(row.cumExecQty || 0);
        if (status === 'Filled' && filled > 0 && filled >= qty) return { state: 'FILLED', order: row };
        if (filled > 0 && filled < qty) return { state: 'PARTIAL', order: row };
        if (['Cancelled', 'Rejected', 'Deactivated'].includes(status)) return { state: 'FAILED', order: row };
      }
      await sleep(this.config.fillPollMs);
    }
    return { state: 'UNKNOWN', order: last };
  }
}

export function orderLinkId(candidateKey) {
  return `n10-${crypto.createHash('sha256').update(String(candidateKey)).digest('hex').slice(0, 28)}`;
}
