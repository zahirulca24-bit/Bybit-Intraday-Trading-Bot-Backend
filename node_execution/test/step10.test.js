import test from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';
import { orderLinkId } from '../src/bybitClient.js';
import { validateContract, revalidateLive } from '../src/validator.js';
import { CommandExecutor } from '../src/executor.js';

const payload = {
  candidateKey:'cand-1', symbol:'BTCUSDT', side:'Buy', positionSizingStatus:'SIZING_APPROVED', sizingApproved:true,
  executionStatus:'AWAITING_NODE_EXECUTION', orderSubmitted:false, marginMode:'ISOLATED', leverage:5,
  qty:'0.01', entryReference:100, technicalStopLoss:99, takeProfitReference:102,
  requiredInitialMarginUsdt:20, projectedTotalInitialMarginUsdt:40,
};

test('configuration is demo-only and disabled by default', () => {
  const cfg = loadConfig({DATABASE_URL:'postgres://x',BYBIT_API_KEY:'k',BYBIT_API_SECRET:'s'});
  assert.equal(cfg.baseUrl, 'https://api-demo.bybit.com');
  assert.equal(cfg.enabled, false);
  assert.throws(() => loadConfig({DATABASE_URL:'x',BYBIT_API_KEY:'k',BYBIT_API_SECRET:'s',BYBIT_BASE_URL:'https://api.bybit.com'}), /locked to Bybit Demo/);
});

test('order link identity is deterministic and bounded', () => {
  assert.equal(orderLinkId('abc'), orderLinkId('abc'));
  assert.notEqual(orderLinkId('abc'), orderLinkId('def'));
  assert.ok(orderLinkId('abc').length <= 36);
});

test('contract and live margin are revalidated', () => {
  assert.equal(validateContract(payload), payload);
  const result = revalidateLive(payload,
    {result:{list:[{totalEquity:'1000',totalAvailableBalance:'800'}]}},
    {result:{list:[{lotSizeFilter:{minOrderQty:'0.001',maxMarketOrderQty:'10'}}]}},
    {result:{list:[]}});
  assert.equal(result.equity, 1000);
  assert.throws(() => validateContract({...payload, leverage:10}), /Isolated 5x/);
  assert.throws(() => revalidateLive({...payload,requiredInitialMarginUsdt:300}, {result:{list:[{totalEquity:'1000',totalAvailableBalance:'800'}]}}, {result:{list:[{lotSizeFilter:{minOrderQty:'0.001',maxMarketOrderQty:'10'}}]}}, {result:{list:[]}}), /25%/);
});

test('executor transitions to managing only after full fill', async () => {
  const transitions=[];
  const repo={transition:async(c,next)=>{transitions.push(next);return {...c,state:next};}};
  const bybit={
    openOrder:async()=>({result:{list:[]}}), wallet:async()=>({result:{list:[{totalEquity:'1000',totalAvailableBalance:'800'}]}}),
    instrument:async()=>({result:{list:[{lotSizeFilter:{minOrderQty:'0.001',maxMarketOrderQty:'10'}}]}}), position:async()=>({result:{list:[]}}),
    setMarginMode:async()=>({}), setLeverage:async()=>({}), createOrder:async()=>({}), waitForFill:async()=>({state:'FILLED'})
  };
  const result=await new CommandExecutor(repo,bybit).execute({candidateKey:'cand-1',state:'RESERVED',ownerId:'o',payload});
  assert.deepEqual(transitions,['ORDER_PENDING','MANAGING']);
  assert.equal(result.state,'MANAGING');
});

test('unknown fill remains order pending fail-closed', async () => {
  const transitions=[];
  const repo={transition:async(c,next)=>{transitions.push(next);return {...c,state:next};}};
  const bybit={openOrder:async()=>({result:{list:[]}}),wallet:async()=>({result:{list:[{totalEquity:'1000',totalAvailableBalance:'800'}]}}),instrument:async()=>({result:{list:[{lotSizeFilter:{minOrderQty:'0.001',maxMarketOrderQty:'10'}}]}}),position:async()=>({result:{list:[]}}),setMarginMode:async()=>({}),setLeverage:async()=>({}),createOrder:async()=>({}),waitForFill:async()=>({state:'UNKNOWN'})};
  const result=await new CommandExecutor(repo,bybit).execute({candidateKey:'cand-1',state:'RESERVED',ownerId:'o',payload});
  assert.deepEqual(transitions,['ORDER_PENDING']);
  assert.equal(result.state,'ORDER_PENDING');
});
