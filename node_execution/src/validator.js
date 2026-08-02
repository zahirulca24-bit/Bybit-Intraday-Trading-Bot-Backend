const number = (v) => Number(v || 0);

export function validateContract(payload) {
  if (!payload || payload.positionSizingStatus !== 'SIZING_APPROVED' || payload.sizingApproved !== true) throw new Error('Command is not Step-8 sizing approved');
  if (payload.executionStatus !== 'AWAITING_NODE_EXECUTION' || payload.orderSubmitted !== false) throw new Error('Command execution state is invalid');
  if (payload.marginMode !== 'ISOLATED' || Number(payload.leverage) !== 5) throw new Error('Only Isolated 5x is approved');
  if (!payload.candidateKey || !payload.symbol || !['Buy','Sell'].includes(payload.side)) throw new Error('Candidate identity is invalid');
  if (number(payload.qty) <= 0 || number(payload.entryReference) <= 0 || number(payload.technicalStopLoss) <= 0 || number(payload.takeProfitReference) <= 0) throw new Error('Quantity or technical price plan is invalid');
  const buy = payload.side === 'Buy';
  if (buy && !(number(payload.technicalStopLoss) < number(payload.entryReference) && number(payload.takeProfitReference) > number(payload.entryReference))) throw new Error('Buy technical price plan is invalid');
  if (!buy && !(number(payload.takeProfitReference) < number(payload.entryReference) && number(payload.technicalStopLoss) > number(payload.entryReference))) throw new Error('Sell technical price plan is invalid');
  return payload;
}

export function revalidateLive(payload, walletResponse, instrumentResponse, positionResponse) {
  validateContract(payload);
  const wallet = walletResponse?.result?.list?.[0];
  const instrument = instrumentResponse?.result?.list?.[0];
  if (!wallet || !instrument) throw new Error('Wallet or instrument truth unavailable');
  const equity = number(wallet.totalEquity);
  const available = number(wallet.totalAvailableBalance);
  const required = number(payload.requiredInitialMarginUsdt);
  if (equity <= 0 || available <= 0 || required <= 0 || required > available) throw new Error('Available margin is insufficient');
  if (required > equity * 0.25 + 1e-8) throw new Error('Per-trade 25% margin cap violated');
  const projected = number(payload.projectedTotalInitialMarginUsdt);
  if (projected > equity * 0.60 + 1e-8 || equity - projected < equity * 0.40 - 1e-8) throw new Error('Combined margin or free-reserve cap violated');
  const lot = instrument.lotSizeFilter || {};
  const qty = number(payload.qty);
  if (qty < number(lot.minOrderQty) || (number(lot.maxMarketOrderQty) > 0 && qty > number(lot.maxMarketOrderQty))) throw new Error('Bybit quantity limits changed');
  const positions = positionResponse?.result?.list || [];
  if (positions.some((p) => number(p.size) > 0 && String(p.symbol) === payload.symbol)) throw new Error('Existing symbol position blocks duplicate execution');
  return { equity, available, instrument };
}
