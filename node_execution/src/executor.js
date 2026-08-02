import { orderLinkId } from './bybitClient.js';
import { validateContract, revalidateLive } from './validator.js';

export class CommandExecutor {
  constructor(repository, bybit) {
    this.repository = repository;
    this.bybit = bybit;
  }

  async execute(command) {
    const payload = validateContract(command.payload);
    const linkId = orderLinkId(command.candidateKey);
    try {
      const existing = await this.bybit.openOrder(payload.symbol, linkId);
      const existingOrder = existing?.result?.list?.[0];
      if (existingOrder) {
        command = await this.repository.transition(command, 'ORDER_PENDING');
        return this.resolveFill(command, payload, linkId);
      }
      const executionHistory = await this.bybit.executions(payload.symbol, linkId);
      const priorExecutions = executionHistory?.result?.list || [];
      if (priorExecutions.some((row) => Number(row.execQty || 0) > 0)) {
        command = await this.repository.transition(command, 'ORDER_PENDING');
        return this.repository.transition(command, 'MANAGING');
      }

      const [wallet, instrument, position] = await Promise.all([
        this.bybit.wallet(),
        this.bybit.instrument(payload.symbol),
        this.bybit.position(payload.symbol),
      ]);
      revalidateLive(payload, wallet, instrument, position);
      await this.bybit.setMarginMode();
      await this.bybit.setLeverage(payload.symbol, 5);

      command = await this.repository.transition(command, 'ORDER_PENDING');
      await this.bybit.createOrder({
        category: 'linear',
        symbol: payload.symbol,
        side: payload.side,
        orderType: 'Market',
        qty: String(payload.qty),
        timeInForce: 'IOC',
        positionIdx: 0,
        orderLinkId: linkId,
        reduceOnly: false,
        closeOnTrigger: false,
        stopLoss: String(payload.technicalStopLoss),
        takeProfit: String(payload.takeProfitReference),
        slTriggerBy: 'MarkPrice',
        tpTriggerBy: 'MarkPrice',
        tpslMode: 'Full',
      });
      return this.resolveFill(command, payload, linkId);
    } catch (error) {
      if (command.state === 'RESERVED') {
        await this.repository.transition(command, 'FAILED');
      }
      // ORDER_PENDING is deliberately retained for unknown submission/fill outcomes.
      throw error;
    }
  }

  async resolveFill(command, payload, linkId) {
    const fill = await this.bybit.waitForFill(payload.symbol, linkId);
    if (fill.state === 'FILLED') return this.repository.transition(command, 'MANAGING');
    if (fill.state === 'PARTIAL') return this.repository.transition(command, 'PARTIALLY_FILLED');
    if (fill.state === 'FAILED') return this.repository.transition(command, 'FAILED');
    return command;
  }
}
