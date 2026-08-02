import { orderLinkId } from './bybitClient.js';
import { validateContract, revalidateLive } from './validator.js';

function rows(response) {
  return Array.isArray(response?.result?.list) ? response.result.list : [];
}

export class CommandExecutor {
  constructor(repository, bybit, config) {
    this.repository = repository;
    this.bybit = bybit;
    this.config = config;
  }

  async execute(command) {
    const payload = validateContract(command.payload, command.candidateKey);
    const linkId = orderLinkId(command.candidateKey);

    if (command.state === 'ORDER_PENDING') {
      return this.resolvePending(command, payload, linkId);
    }
    if (command.state !== 'RESERVED') {
      throw new Error(`Step 10 cannot execute command state ${command.state}`);
    }

    try {
      if (await this.hasExchangeEvidence(payload.symbol, linkId)) {
        command = await this.repository.transition(command, 'ORDER_PENDING');
        await this.repository.recordOrder(command, 'RECOVERED_EXCHANGE_EVIDENCE', { orderLinkId: linkId });
        return this.resolvePending(command, payload, linkId);
      }

      await this.finalRevalidation(command, payload, linkId);
      await this.ensureAccountSettings(payload.symbol);
      const finalTruth = await this.finalRevalidation(command, payload, linkId);

      await this.repository.recordOrder(command, 'SUBMISSION_INTENT', {
        orderLinkId: linkId,
        finalRevalidation: finalTruth,
        submissionAttemptedAt: Date.now(),
      });
      command = await this.repository.transition(command, 'ORDER_PENDING');

      let acknowledgement;
      try {
        acknowledgement = await this.bybit.createOrder({
          category: this.config.category,
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
          slOrderType: 'Market',
          tpOrderType: 'Market',
          tpslMode: 'Full',
        });
      } catch (error) {
        await this.repository.recordOrder(command, error.retCode !== undefined ? 'ORDER_REJECTED' : 'SUBMISSION_UNKNOWN', {
          orderLinkId: linkId,
          retCode: error.retCode ?? null,
          reason: error.message,
          response: error.response ?? null,
        }).catch(() => undefined);
        if (error.retCode !== undefined && error.retCode !== null) {
          return this.repository.transition(command, 'FAILED');
        }
        throw error;
      }

      const orderId = String(acknowledgement?.result?.orderId || '').trim();
      if (!orderId) {
        await this.repository.recordOrder(command, 'ACKNOWLEDGEMENT_INCOMPLETE', {
          orderLinkId: linkId,
          acknowledgement,
        });
        throw new Error('Bybit order acknowledgement has no orderId; command remains ORDER_PENDING');
      }
      await this.repository.recordOrder(command, 'ORDER_ACKNOWLEDGED', {
        orderLinkId: linkId,
        orderId,
        acknowledgement,
      });
      return this.resolvePending(command, payload, linkId);
    } catch (error) {
      if (command.state === 'RESERVED') {
        await this.repository.recordOrder(command, 'PRE_SUBMISSION_BLOCKED', {
          orderLinkId: linkId,
          reason: error.message,
        }).catch(() => undefined);
        await this.repository.transition(command, 'FAILED').catch(() => undefined);
      }
      throw error;
    }
  }

  async ensureAccountSettings(symbol) {
    const before = await this.bybit.accountInfo();
    const current = String(before?.result?.marginMode || '');
    if (current !== this.config.marginMode) {
      await this.bybit.setMarginMode();
      const after = await this.bybit.accountInfo();
      if (String(after?.result?.marginMode || '') !== this.config.marginMode) {
        throw new Error('Bybit account margin mode is not verified as ISOLATED_MARGIN');
      }
    }
    await this.bybit.setLeverage(symbol, this.config.leverage);
  }

  async hasExchangeEvidence(symbol, linkId) {
    const [order, executionResponse] = await Promise.all([
      this.bybit.findOrder(symbol, linkId),
      this.bybit.executions(symbol, linkId),
    ]);
    return Boolean(order) || rows(executionResponse).some((row) => Number(row.execQty || 0) > 0);
  }

  async finalRevalidation(command, payload, linkId) {
    const [wallet, instrument, ticker, positions, activeOrders, pendingReservedMargin] = await Promise.all([
      this.bybit.wallet(),
      this.bybit.instrument(payload.symbol),
      this.bybit.ticker(payload.symbol),
      this.bybit.positions(),
      this.bybit.activeOrders(payload.symbol),
      this.repository.pendingReservedMargin(command.candidateKey),
    ]);
    return revalidateLive(payload, {
      wallet,
      instrument,
      ticker,
      positions,
      activeOrders,
      pendingReservedMargin,
      orderLinkId: linkId,
    }, this.config);
  }

  async resolvePending(command, payload, linkId) {
    const resolution = await this.bybit.waitForResolution(payload.symbol, linkId, Number(payload.qty));
    await this.repository.recordFills(command, resolution.executions || []);
    await this.repository.recordOrder(command, `RESOLUTION_${resolution.state}`, {
      orderLinkId: linkId,
      order: resolution.order || null,
      cumulativeQty: Number(resolution.cumulativeQty || 0),
      executionCount: (resolution.executions || []).length,
    });

    if (resolution.state === 'FAILED') {
      return this.repository.transition(command, 'FAILED');
    }
    if (resolution.state === 'UNKNOWN') {
      return command;
    }

    const protectedPosition = await this.bybit.ensureProtection(payload);
    await this.repository.recordOrder(command, 'PROTECTION_VERIFIED', {
      orderLinkId: linkId,
      position: protectedPosition,
    });

    if (resolution.state === 'FILLED') {
      return this.repository.transition(command, 'MANAGING');
    }
    if (resolution.state === 'PARTIAL') {
      return this.repository.transition(command, 'PARTIALLY_FILLED');
    }
    return command;
  }
}
