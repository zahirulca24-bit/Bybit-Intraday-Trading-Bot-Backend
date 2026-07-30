# Final Release Verification Report

**Project:** Bybit Demo Intraday Trading Bot  
**Date:** 26 July 2026  
**Scope:** Serials 1–10  
**Canonical runtime:** `python backend/secure_server.py`

## Executive verdict

**Overall verdict: INCONCLUSIVE for controlled unattended Demo release; NO-GO for real-money trading.**

The implementation work for Serials 1–9 is merged. Controlled manual Bybit Demo evidence confirms one fully filled entry with exchange-visible SL and TP. Final acceptance is not complete because the final merged tree has not been fully executed locally, Render deployment evidence is incomplete, durable-disk recovery is unverified, automatic scanner-to-trade execution is unproven, and position-management triggers have not been demonstrated.

## Verification principles

- Code inspection is implementation evidence, not runtime evidence.
- A gate is `PASS` only when it ran successfully and its evidence is available.
- A gate is `FAIL` when it ran and failed or a mandatory requirement is known to be violated.
- A gate is `INCONCLUSIVE` when it did not run or evidence is unavailable.
- No GitHub Actions workflow exists in this repository; no CI success is claimed.

## Merged implementation evidence

| Serial | PR | Merge commit | Result |
|---:|---:|---|---|
| 1 | #30 | `a9fb6ed0706b6c48b90663d14830716917cdd68d` | Protection fail-closed implementation merged |
| 2 | #31 | `7d98c1f988b539143ad9f9f680da2778de967153` | Mandatory SL/TP implementation merged |
| 3 | #32 | `4e9d4b6f21c5f2d226032f42f927ffaddf31a635` | Final-fill verification merged |
| 4 | #33 | `074c4a0b5bd4ad1dff572445b2cb3a7df0bf5e15` | Position-management reliability merged |
| 5 | #35 | `8a8ecc1dd37efa9b5f112ded6a20d06948b6354e` | Secure runtime and routes merged |
| 6 | #36 | `2df65de0b05174fda2f7b983909b394ac342ce8a` | Scanner safety and cost gates merged |
| 7 | #37 | `57a8a4c2c7ee1670b7db9bf73b0fd8ddf02cd699` | Durable journal and risk state merged |
| 8 | #38 | `96b5b00488cbc5b06b5a92c57291f09509aa4f9b` | Replay accuracy merged |
| 9 | #39 | `ba7e3284d3583c5caebf6060ee926378adc4dbbd` | Confirmed UI fixes merged |

## Confirmed live evidence

### Controlled manual Demo entry — PASS

Recorded evidence:

- Event: `manual_connection_test`
- Result: `OK: order fully filled`
- Symbol: `BTCUSDT`
- Side: `Buy`
- App size: `0.003 BTC`
- Bybit Demo size: `0.003 BTC`
- Exchange-visible TP: `65,259.20`
- Exchange-visible SL: `63,717.60`

This supports the manual entry, final-fill, and mandatory-protection gates only. It does not establish automatic scanner-to-trade behavior.

## Final gate matrix

| Gate | Verdict | Evidence / blocker |
|---|---|---|
| Canonical runtime selected in `render.yaml` | PASS | `python backend/secure_server.py` configured |
| Alternate ASGI trading entrypoint disabled | PASS | `app/main.py` fail-closed implementation merged |
| Serials 1–9 code merged | PASS | PR and merge-commit evidence above |
| Manual final fill | PASS | Controlled Bybit Demo evidence recorded |
| Exchange-visible SL and TP | PASS | Controlled Bybit Demo evidence recorded |
| Final Python syntax compilation | INCONCLUSIVE | Not executed against final merged tree |
| Focused tests | INCONCLUSIVE | No final execution output available |
| Full pytest suite | INCONCLUSIVE | No final execution output available |
| Dependency audit | INCONCLUSIVE | Not executed; no evidence available |
| Render deployment | INCONCLUSIVE | Final deployment logs unavailable |
| Public/private route policy | INCONCLUSIVE | Implementation merged; live smoke unavailable |
| Same-origin and foreign-Origin behavior | INCONCLUSIVE | Live browser/HTTP evidence unavailable |
| Persistent SQLite disk | INCONCLUSIVE | Render disk path and restart test unavailable |
| Automatic scanner-to-trade | INCONCLUSIVE | No controlled automatic Demo entry evidence |
| Cost-to-risk and net-RR live behavior | INCONCLUSIVE | No controlled live cost-gate evidence |
| Replay endpoint parity | INCONCLUSIVE | No before/after live replay comparison |
| Partial TP trigger | INCONCLUSIVE | No controlled trigger proof |
| Breakeven trigger | INCONCLUSIVE | No controlled trigger proof |
| Trailing-stop trigger | INCONCLUSIVE | No controlled trigger proof |
| Leverage and margin-mode policy | FAIL / OPEN BLOCKER | Prior screenshot showed Cross `10.00x`; approved cap not verified |
| Unattended Demo operation | NO-GO | Mandatory live gates incomplete |
| Real-money trading | NO-GO | Project is Demo-only and acceptance is incomplete |

## Exact local verification commands

Run from the repository root using a clean environment:

```powershell
python -m compileall backend app scripts tests
python -m pytest -q
python backend/secure_server.py
```

The runtime-start command should be stopped after confirming startup output and local health behavior.

Suggested local HTTP checks:

```powershell
curl.exe -i http://127.0.0.1:8787/api/health
curl.exe -i http://127.0.0.1:8787/api/bybit/ticker?symbol=BTCUSDT
curl.exe -i http://127.0.0.1:8787/api/bot/status
curl.exe -i -H "Authorization: Bearer <ADMIN_TOKEN>" http://127.0.0.1:8787/api/bot/status
curl.exe -i -H "Authorization: Bearer <ADMIN_TOKEN>" http://127.0.0.1:8787/api/durable-state/status
```

Expected behavior:

- Public health/market endpoints return normal responses without a token.
- Private endpoints return `401` without a token.
- Private endpoints return normal responses with the valid token.
- Durable-state status is not degraded only when a persistent path is configured.

## Exact Render verification checklist

1. Deploy current `main`.
2. Confirm startup command is `python backend/secure_server.py`.
3. Confirm required secret environment variables are set.
4. Mount a persistent disk.
5. Set:

```env
BOT_STATE_DB_PATH=/var/data/bot_state.sqlite3
```

6. Confirm `/api/durable-state/status` reports configured persistence.
7. Restart/redeploy and confirm journal, daily-risk state, and unresolved pending entry survive.
8. Verify unauthenticated private routes return `401`.
9. Verify a foreign `Origin` returns `403` with no allow-origin header.
10. Verify same-origin UI polling succeeds.

## Controlled Bybit Demo acceptance sequence

Use the smallest exchange-valid quantity and keep real-money credentials unavailable.

1. Keep the bot stopped and confirm account, positions, journal, and durable-state status.
2. Run the scanner and confirm bounded output: shortlist 20, deep scan 10.
3. Confirm candidates use closed candles and include Buy/Sell directions.
4. Record spread tier, estimated cost-to-risk, gross RR, and net RR.
5. Allow one controlled automatic Demo entry only when all gates pass.
6. Confirm final fill, positive executed quantity, matching position, SL, and TP.
7. Restart the service and confirm durable state recovery.
8. Use controlled market conditions or a tiny managed position to prove partial TP, breakeven, and trailing-stop behavior.
9. Verify leverage and margin mode explicitly against the approved policy.
10. Record screenshots/logs for every gate.

## Release blockers that must close before acceptance

- Final syntax and pytest execution against the final merged tree.
- Render deployment and route/CORS smoke evidence.
- Persistent disk and restart-recovery evidence.
- One automatic scanner-to-trade Demo proof.
- Live cost-gate evidence.
- Partial TP, breakeven, and trailing-stop proof.
- Explicit leverage and margin-mode policy verification.

## Final decision

The codebase may proceed to **controlled Bybit Demo verification only**. It is not accepted for unattended operation, public use, client funds, advisory service, or real-money trading.
