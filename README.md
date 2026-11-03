# SENSEX PRO (Zerodha Kite Connect)

Selective **intraday long-option** engine (buy CE / buy PE) for six index-option markets: SENSEX, NIFTY, BANKNIFTY,
FINNIFTY, MIDCPNIFTY, NIFTYNXT50. Built to the "SENSEX PRO Product & Engineering Requirements" brief: five entry
strategies, a regime filter, a scored ensemble, contract selection on real bid/ask, a risk manager, adaptive
thesis-based exits and independent emergency capital protection.

**Paper mode by default.** No strategy, win rate or profit is guaranteed, and the research below found **no edge
after option costs** (see "Research results"). Do not trade it live on the strength of passing tests or a paper run.

## How it trades (one loop per second)
1. **Data** (`sensexpro/market.py`): instrument master of the day (lot size, tick size, expiries, strikes, never
   hardcoded), 1-minute index candles from Kite with the index future's volume, aggregated to 5 and 15 minutes from
   CLOSED candles only; an option window (ATM ± 6 strikes) streamed in FULL mode with bid/ask, depth, volume, the
   exchange timestamp and the local receive time. Stale (> 5 s), crossed, zero or one-sided quotes are never used.
2. **Regime** (`regime.py`): TREND_UP / TREND_DOWN / RANGE / HIGH_VOLATILITY / LOW_LIQUIDITY_OR_UNCERTAIN /
   DATA_UNSAFE from 15-minute EMA structure, ADX, VWAP slope, VWAP crosses, ATR ratio, candle gaps and option quotes.
3. **Strategies** (`strategies/`): Opening Range Breakout, VWAP continuation, EMA pullback, failed breakout /
   liquidity sweep, Bollinger squeeze. Each emits candidates on completed 5-minute candles; none can place orders.
4. **Ensemble** (`strategies/ensemble.py`): brief 4.6 permission table, dedupe, CE/PE conflict on the same bar =
   rejected, a setup is never traded twice, 100-point score (trend 20, VWAP 20, price action 20, volume 15, momentum
   15, option liquidity 10). 80+ eligible (85+ in HIGH_VOLATILITY), 70-79 watchlist, below 70 rejected.
5. **Contract** (`options.py`): nearest expiry (next one on expiry day), ATM / 1 ITM / 1 OTM only, spread ≤ 1.5 %,
   volume and depth checks, sized on the ASK in whole lots, reduced to fit the premium cap, skipped if one lot does
   not fit. Kite has no Greeks: delta is reported as unavailable (never fabricated).
6. **Risk** (`risk.py`): window 09:30-14:30, one position, max 3 entries a day across all indices, 10-minute
   cool-down after an exit, 2 losses in a row = no more entries that day, daily / weekly / drawdown breakers, kill
   switch, fresh data, reconciled broker state, no unresolved orders.
7. **Execution** (`execution.py`): order intent journaled before sending, unique client id = Kite tag, state machine
   (CREATED ... FILLED / CANCELLED / REJECTED / UNKNOWN), requested vs filled vs remaining tracked. Paper fills at the
   ask + 0.25 % (buy) / bid - 0.25 % (sell) with simulated rejections, no-fills and partial fills.
8. **Exits** (`exits.py`, `emergency.py`): emergency protection is checked first on every loop and overrides
   everything (per-trade loss budget, daily / weekly / drawdown, stale quote, wide spread, broker mismatch,
   unreconciled orders, 15:15 square-off). Then the adaptive score: structure 30, VWAP 20, momentum 15, option health
   15, IV/theta 10 (unavailable from Kite: always scored as missing, never as favourable), time 10. A completed close
   beyond the invalidation level exits at once; 70+ exits; 50-69 twice in a row exits; the invalidation level trails
   confirmed swings. After a profit milestone (25 % of premium) the exit threshold drops to 50 and multi-lot
   positions sell one lot at a time. No fixed premium-percentage stop.

## Risk limits (config/risk.yaml)
Written for the brief's Rs 40,000 and scaled by `PAPER_CAPITAL / 40,000` (live: Kite cash). At the VM's
`PAPER_CAPITAL=200000` (x5):

| Limit | Brief (Rs 40k) | At Rs 2,00,000 |
|---|---:|---:|
| Max premium per entry (ask x qty + charges) | 2,000 | 10,000 |
| Emergency loss per trade | 600 | 3,000 |
| Daily loss breaker (entries off, open position closed) | 1,200 | 6,000 |
| Weekly loss breaker (kill switch) | 3,000 | 15,000 |
| Drawdown breaker (kill switch) | 4,000 | 20,000 |
| Entries per day / open positions / losses in a row | 3 / 1 / 2 | same |

A Rs 10,000 cap fits about one near-the-money SENSEX or NIFTY weekly lot; most monthly-index lots (BANKNIFTY,
MIDCPNIFTY, NIFTYNXT50) cost more and are skipped. The emergency threshold is not a guaranteed maximum loss: gaps,
illiquidity, circuit limits, rejected orders or outages can lose more, up to the whole premium.

## Paper and live
- `docker-compose.yml` runs `main.py --auto`: PAPER unless `data/mode.json` says LIVE **and** `.env` has
  `LIVE_TRADING_CONFIRM=YES` (both required; a LIVE request without the master switch starts PAPER and says why).
- Telegram `/mode live` then `/confirm <code>` (owner only) writes `data/mode.json`; the bot restarts in the new mode
  once no position is open. `/mode paper` switches back. In PAPER the live adapter is never constructed.

## Telegram
`/lots`, `/lots N` (SENSEX), `/lots INDEX N`, `/indices`, `/signals`, `/risk`, `/status`, `/report`, `/mode`,
`/mode live|paper` + `/confirm CODE`, `/kill [reason]`, `/resetkill` + `/confirm CODE`, `/help`. Lots are an upper
bound (the premium cap may reduce them). Alerts: start / stop, session ready, feed stalls, signals not traded and why,
entries, partial fills, exits with give-back from the peak, emergency triggers, kill switch, day summary.

## Kept from the earlier bot
Kite login (`token_service.py`: daily Telegram login link, `data/token.json`), Caddy for the redirect URL, the Google
Sheet feed (same tabs and columns: Orders, Trades, Status, Health, Log), the report page (ONE Cloud Storage object
`gs://$REPORT_GCS_BUCKET/r/$REPORT_TOKEN/index.html`, rebuilt at 16:00), `data/state.json` for the VM's autostop
guard, the same `.env` variables (`MAX_RISK_PER_TRADE_PCT`, `MAX_DAILY_LOSS_PCT`, `CHAIN_INTERVAL_S` are no longer
read).

## Journal, logs, dashboard
- SQLite `data/sxbot.db` (schema migrations, `sp_*` tables): candles, SENSEX option quote snapshots (1/s), every scored
  signal with components and reasons, contract selections with alternatives and rejections, order intents / state
  changes / fills, positions, every exit assessment, risk events, risk book. Daily consistent backup
  `data/sxbot-backup.db` after the close.
- Logs: `logs/bot.log` (text) and `logs/sensexpro.jsonl` (JSON), rotated; `docker compose logs -f bot`.
- Dashboard (read-only) on the VM's loopback: `gcloud compute ssh sxbot --zone asia-south1-a -- -L 8090:127.0.0.1:8090`
  then open http://localhost:8090 (`/status.json` for JSON).
- Health: `bash scripts/health_check.sh`, `docker compose exec bot python -m sensexpro.cli health`.

## Restart safety
On every start and reconnect: in-flight orders are resolved (paper: cancelled; live: looked up by tag, a working order
is cancelled), the open position's quantity is rebuilt from journaled fills, realised P&L missed by a crash is booked
exactly once, LIVE compares broker positions with the journal (mismatch = kill switch), and no entry is allowed until
this is done. A shutdown never flattens: the position is recovered and managed after the restart.

## Research results (underlying-signal research, NOT an options backtest)
`python -m sensexpro.research.backtest` replays the live strategy / regime / ensemble / exit code on 5-minute index
history 2018-2026. Kite serves no expired-option or intraday futures history, so outcomes are in index points, the
volume filter is off (no historical volume) and option P&L is a labelled model (delta 0.5, VIX-based time decay,
spread, Zerodha charges). Full tables: [docs/RESEARCH.md](docs/RESEARCH.md).

| Index | Trades | Win % | Points / trade | Modelled Rs / lot, 2018-26 | Years positive |
|---|---:|---:|---:|---:|---:|
| SENSEX | 1,208 | 36.8 | +4.4 | -4,23,820 | 0 / 9 |
| NIFTY | 1,228 | 37.1 | +1.3 | -4,25,085 | 0 / 9 |
| BANKNIFTY | 1,173 | 33.8 | +0.2 | -3,76,460 | 2 / 9 |
| FINNIFTY | 1,191 | 37.1 | +2.0 | -2,90,937 | 2 / 9 |
| MIDCPNIFTY | 701 | 41.5 | +2.7 | -1,11,416 | 1 / 5 |
| NIFTYNXT50 | 1,048 | 41.0 | +31.4 | +67,190 (illiquid options) | 2 / 9 |

30 minutes after a signal the index had moved the trade's way about 50 % of the time with a mean move of about 0
points, for every strategy: the signals do not predict direction, and buying options pays time decay, spread and
charges on every trade. **Verdict: no evidence of an edge. Not recommended for live money.**

## Tests
`python -m unittest discover -s tests -t .` (122 tests: unit, integration, recovery, safety incl. no-look-ahead and
a secret scan). Windows needs `pip install tzdata`. On the VM the deploy script runs them inside the image.

## Deploy / rollback
See [deploy/DEPLOY.md](deploy/DEPLOY.md). Short form, on the VM: `bash scripts/deploy.sh <commit>`; rollback:
`bash scripts/deploy.sh "$(cat data/deploy.prev)"`.
