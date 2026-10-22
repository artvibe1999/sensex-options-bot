# SENSEX options bot (Zerodha Kite Connect)

Autonomous, defined-risk SENSEX options bot. **Paper mode by default.** Live trading needs
`--live` AND `LIVE_TRADING_CONFIRM=YES`.

## Honest status
- **164 strategy candidates** (8 templates x parameters x regime x DTE x trend filter x stop/target). They are
  *candidates*, not "proven". Nothing trades live until it passes: walk-forward backtest -> forward shadow
  book -> `promote.py` -> `data/approved_live.json` (valid 30 days; missing/expired = no trades).
- The backtest prices options with Black-Scholes (Kite has no expired-option history). It is an approximation.
  Real evidence = the data the bot records (`data/sxbot.db`): full chain every 10 s, ATM+/-5 strikes every second.
- On synthetic data every template is slightly negative after costs. That is expected: costs are real and
  the model gives no free edge. Do not expect the pipeline to approve much until real data says so.
- NOT verified (flagged VERIFY in `config.py`): SENSEX weekly expiry weekday (Thursday assumed), strike step
  (100 assumed), lot size (20 assumed; the live bot reads lot size/expiries/tokens from the instruments dump).
- `token_service.py`'s `redirect_params` state nonce is untested against live Kite.

## Layout
`config.py` `costs.py` `zerodha_client.py` `strategy_engine.py` `strategy_library.py` `risk_manager.py`
`market_data.py` `shadow.py` `recorder.py` `backtest.py` `promote.py` `token_service.py` `token_store.py`
`alerts.py` `main.py`, `tests/`, `Dockerfile`, `docker-compose.yml`, `deploy/`.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env        # fill KITE_API_KEY / KITE_API_SECRET (+ Telegram optional)
python -m unittest discover -s tests -v
```

## Pipeline
```bash
# 1. Backtest on real index + VIX history (writes data/approved_backtest.json)
python backtest.py --fetch-kite 700
# 2. Run paper mode daily: records all market/chain data and shadow-trades all 164 variants
python main.py --paper
# 3. After 4+ weeks, promote variants that pass backtest AND forward stats
python promote.py
# 4. Only then, live (small size first)
LIVE_TRADING_CONFIRM=YES python main.py --live
```
Export recorded data: `python recorder.py export --table chain --since 2026-10-01 --out chain.csv`

## 1-second data cadence
The bot decides, shadow-tests and records every second (`LOOP_INTERVAL_S=1`, `CHAIN_INTERVAL_S=1`). Kite's REST
quote endpoint allows only ~1 request/s, so the chain is built from WebSocket FULL-mode ticks (all ~160 contracts
in the +/-40 strike window; LTP, OI, volume, bid, ask) and Black-Scholes IV/greeks are recomputed each second.
REST is used only for warm-up or if the feed stalls, and never more than once per 2 s. Stored per second: `market`,
`features`, and ATM+/-5 strikes in `flow`; the full 160-strike `chain` table stays at 10 s (`CHAIN_RECORD_EVERY_S`)
because every second would be ~3.6M rows/day. `flow` older than 30 days is deleted (`FLOW_RETENTION_DAYS`, 0 = keep).
Note: Kite sends a tick only when a contract trades or its book changes, so illiquid strikes carry their last value.

## OI / gamma features (research layer)
Every fresh chain (1 s) the bot also computes and stores (table `features`): PCR, call/put OI walls, max pain,
net gamma exposure (a disputed proxy), 25-delta skew, 5-minute OI change, ATM spread and volume. ATM+/-5 strikes
are stored every second in table `flow` (OI, volume, bid, ask); set `STREAM_FULL=0` to switch that off
(`STREAM_CHAIN=0` goes back to REST polling; then set `CHAIN_INTERVAL_S=3` or more). 104 extra variants (ids ending `|oi=<rule>`) add one of these as a filter to
a plain strategy: `walls` (OI walls sit between spot and the short strikes), `pcr_calm`, `gex_pos`, `gex_neg`,
`flow`. They cannot be backtested (no historical OI), so they are shadow-tested live and `promote.py` only
promotes one if it beats its plain twin (avg net per trade and profit factor) over 40+ forward trades.
Nothing here is a proven edge; the filters are hypotheses the shadow book will confirm or reject.

## Small vs large accounts
A SENSEX lot cannot be split. At 73,000 spot one lot of a defined-risk spread loses Rs 1,800-17,000 in the worst
case, more than 2% of a small account, so the standard limits would place NO trades below about Rs 3.75 lakh.
`CAPITAL_PROFILE=auto` therefore lets accounts below Rs 3.75 lakh trade ONE lot whose worst-case loss is at most
min(15% of capital, Rs 7,500), with the daily loss limit widened to match (accounts above keep 2% / 3%).
`python capital_check.py` shows which variants fit which capital. Rs 15,000 can only use the narrow debit spreads
(about 20 variants); Rs 50,000 about 90. Fixed brokerage makes a small lot proportionally dearer (about Rs 145
round trip on a Rs 2,000-risk spread = 7%). Nothing here promises profit: test each size in paper mode first
(`PAPER_CAPITAL=15000`), and re-run `backtest.py` so the new 200/300-point spreads get approved or rejected.

### 200 extra small-account variants
The library now has 420 plain variants (+104 OI-rule ones): 200 were added for small accounts (narrow 200/300-point
credit spreads, condors and debit spreads, several stop/target settings; worst case about Rs 1.8k-5k per lot).
They get no special treatment: each must pass the same walk-forward backtest, then forward shadow trading, before it
trades. Win rate is recorded (`full.win_rate` in `data/approved_backtest.json`) but is NOT the approval criterion:
credit spreads win often and still lose money when the rare loss is 5-10x the typical win. Approval needs profit
factor and positive net after costs. Trying more variants also raises the chance that some pass by luck, which is why
the forward shadow stage matters.

## Is it working?
`docker compose exec bot python health_report.py` prints login status, approvals, latest spot/VIX/regime,
recorded rows, shadow-trade results and a list of problems. With no approved variants the bot records data and
shadow-trades but places NO paper orders; run the backtest first (step 1 above), then restart the bot.

## Daily token
See `deploy/DEPLOY.md`.
