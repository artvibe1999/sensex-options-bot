# SENSEX options bot (Zerodha Kite Connect)

Autonomous, defined-risk SENSEX options bot. **Paper mode by default.** Live trading needs
`--live` AND `LIVE_TRADING_CONFIRM=YES`.

## Honest status
- **164 strategy candidates** (8 templates x parameters x regime x DTE x trend filter x stop/target). They are
  *candidates*, not "proven". Nothing trades live until it passes: walk-forward backtest -> forward shadow
  book -> `promote.py` -> `data/approved_live.json` (valid 30 days; missing/expired = no trades).
- The backtest prices options with Black-Scholes (Kite has no expired-option history). It is an approximation.
  Real evidence = the data the bot records every 10 s (`data/sxbot.db`, table `chain`).
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

## Is it working?
`docker compose exec bot python health_report.py` prints login status, approvals, latest spot/VIX/regime,
recorded rows, shadow-trade results and a list of problems. With no approved variants the bot records data and
shadow-trades but places NO paper orders; run the backtest first (step 1 above), then restart the bot.

## Daily token
See `deploy/DEPLOY.md`.
