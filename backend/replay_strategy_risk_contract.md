# Historical Replay Step 6 Contract

- Evaluates only immutable `replay_session_candles`.
- Indicators: EMA20, EMA50, RSI14, ATR14, MACD(12,26) with deterministic signal approximation.
- Grades: A+ (4 aligned votes), A (3), B+ (2), REJECT otherwise.
- Only A+/A Buy/Sell candidates receive risk plans.
- Risk: A+ 1.00%, A 0.75% of replay equity; 1.5 ATR stop; 2R target.
- Results are candidate decisions only.
- No orders, fills, fees, positions, SL/TP execution, PnL, private Bybit API, Demo execution, Testnet, paper trading, or live-money execution.
- Step 7 owns simulated execution.
