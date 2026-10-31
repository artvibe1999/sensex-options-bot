# SENSEX options bot (Zerodha Kite Connect)

Autonomous SENSEX options bot. **Paper mode by default.** Live trading needs `--live` AND `LIVE_TRADING_CONFIRM=YES`.

## Current status (31 Oct 2026)
- **Active strategy: the playbook** (`playbook.py`, on by default, `PLAYBOOK=0` switches it off). Four setups, one
  position at a time, **no stop-loss and no trailing stop**; every trade is closed by 15:15 at the latest.
- Deployed on the GCP VM `sxbot` in **paper mode** with `PAPER_CAPITAL=40000`. Default 2 lots per trade, changeable
  from Telegram. The bot only goes live when you switch the compose command to `--live` and set
  `LIVE_TRADING_CONFIRM=YES`.
- Results so far are a **backtest** (see "How the playbook was chosen"). Option prices in it are modelled from the index
  and India VIX, because Kite serves no history for expired option contracts. Forward paper results will show whether
  the edge holds; follow them on the trading report page.
- The older approvals pipeline (164+ variants, walk-forward backtest, shadow book, `promote.py`) is still in the code
  and still records data, but it does not place entries while the playbook is on. Its documentation is kept below.

## The playbook (`playbook.py`, `pb_indicators.py`)
Indicators are computed on 15-minute bars built from Kite's 5-minute history, using **only candles that had closed
when the history was fetched** (`MarketData.completed_5m`).

| Setup | When | What | Exit |
|---|---|---|---|
| **Single leg** (top priority) | 15-min Supertrend(10,2) flips and Supertrend(10,3) already agrees, signal bar ending 10:30-13:00, India VIX >= 14. Max 2 per day. | Buy a CE (up) or PE (down). Strike against time decay: ATM with 3+ trading days to expiry, 200 pts ITM with 2 days, 400 pts ITM with 1 day. | Trend reversal (Supertrend(10,2) turns against the trade), Rs 1,500 loss cap, else 15:15. |
| **Iron condor** | No trend: first moment 10:00-12:00 with 15-min ADX(14) < 25, no single-leg trade yet today. Any VIX. Once per day. | Sell CE/PE 0.8% out of the money when VIX >= 14, 0.6% out when VIX < 14; buy wings 1.7% further out. | 15:15, Rs 1,500 loss cap, or a single-leg signal (condor closed, trend trade taken). |
| **Trend credit spread** | Strong trend without a fresh flip: ADX >= 30, both Supertrends and +DI/-DI agree, 10:30-13:00, no other trade today. | Sell the option 1% OTM on the side away from the trend (PE in an uptrend, CE in a downtrend), buy one 1.2% further out. | Trend reversal, Rs 1,500 loss cap, a single-leg signal, or 15:15. |

- **Expiry day:** the bot never trades the expiring contract on expiry day; the chain uses the next weekly expiry.
  Using next week's contract on the day *before* expiry was tested and made results worse, so it is not used.
- **Lots:** the Telegram setting (default 2), or fewer (down to 1) when that many lots need more margin than the capital
  allows (Kite's basket margin, max 80% of capital on small accounts). A trade that does not fit even at 1 lot is skipped
  with a Telegram warning.
- **Pre-emption:** an open condor or credit spread is closed for a trend signal only if the trend trade can actually be
  placed (quote and margin), checked at most every 10 s.
- **Quotes:** a trend signal whose option is not quoted yet is retried for 60 s; a margin refusal is final.
- The playbook bypasses the old exit engine, daily-loss stop and portfolio trailing stop. Only broker-safety halts
  (position mismatch, unconfirmed unwind, exits not completing, shutdown) can close a playbook position early.

## Telegram commands (`telegram_control.py`)
Only messages from `TELEGRAM_CHAT_ID` are obeyed (in a group chat every member can use them; a private chat is safer).

| Command | Effect |
|---|---|
| `/lots` | Show the lot settings |
| `/lots N` | Single-leg lots, 1-20, paper and live, from the next entry |
| `/mlots N` | Condor and credit-spread lots, 1-20, from the next entry |
| `/status` | Mode, open position, today's P&L, lot settings |
| `/report` | Link to the trading report page |

Settings persist in `data/settings.json` (created on the first change). An open position is never resized.

## Trading report page (`report_site.py`)
One self-contained HTML page with **Paper** and **Live** tabs: net P&L since `REPORT_SINCE`, trades, win rate, days
won / lost / without a trade, best and worst day, largest drop from a peak, daily P&L chart, by month, by setup
(paper) or bot vs manual (live), and every day with its trades.
- **Paper:** the bot's own trade log (`trades` table, mode PAPER).
- **Live:** after each close (~15:30, before the 15:40 auto-stop) the bot reads the account's real option orders and
  fills from Kite (`orders()`, `trades()`; read-only) and saves them in `data/live/YYYY-MM-DD.json` (no account ids).
  Kite keeps only the current day, so a day the VM is off at the close cannot be fetched later. Realised P&L per
  contract per day; charges estimated per order from the Zerodha schedule; **Bot** = orders tagged `SXALGO`.
- **Hosting:** the page is **one Cloud Storage object**, `gs://REPORT_GCS_BUCKET/r/<REPORT_TOKEN>/index.html`,
  overwritten in place on every rebuild (start-up and each close), so its link never changes and works while the VM is
  off. Uploaded with the VM's own service-account token from the metadata server (no key file), `cacheControl:
  no-cache`. The page carries `noindex` and a Content-Security-Policy meta tag. The VM no longer serves the page.
- **Telegram:** the link with a one-line summary is sent only when the page's data changed (fingerprint in
  `data/report.hash`); a failed upload is retried at the next build.
- **Cloud setup** (done for `sxbot`): a dedicated bucket with uniform access; `allUsers` =
  `roles/storage.legacyObjectReader` (read an object, no listing, so the token path cannot be discovered); a service
  account `sxbot-report` with `roles/storage.objectUser` on that bucket only; the VM runs as `sxbot-report` with scopes
  `storage-rw,logging-write` (instead of the default compute account with project Editor).

## Configuration added for the playbook and report
| Variable | Default | Meaning |
|---|---|---|
| `PLAYBOOK` | `1` | `0` = old approvals + explorer entry path |
| `PB_LOSS_CAP_RS` | `1500` | Loss cap per playbook trade (Rs); `0` = off |
| `PB_SINGLE_MIN_VIX` | `14` | Minimum India VIX for single-leg entries |
| `PB_CREDIT_SPREAD` | `1` | `0` switches off the trend credit spread (thinnest edge) |
| `SINGLE_LOTS` / `MULTI_LOTS` | `2` / `2` | Default lots until changed from Telegram |
| `PAPER_CAPITAL` | `500000` (VM: `40000`) | Paper account size used for margin checks |
| `REPORT_TOKEN` | (none) | Long random string, the secret part of the report path |
| `REPORT_GCS_BUCKET` | (none) | Bucket holding the report page; unset = no page |
| `REPORT_SINCE` | `2026-11-02` | First date shown on the report |
| `REPORT_BACKTEST_URL` | backtest page | Link shown in the report footer |

## How the playbook was chosen (research, Oct 2026)
Data: SENSEX and India VIX 1-minute candles from Kite, 14 Mar 2018 - 30 Oct 2026 (2,118 trading days), built into
5-minute bars. Option prices: Black-Scholes on India VIX (calls x0.91, puts x1.02), calibrated on 8,319 real recorded
quotes (median error under 2%); real intraday straddle decay was checked against the model. Costs: 0.5% slippage per
fill (real median half-spread is 0.12% at the money) plus Zerodha brokerage, STT 0.15% on sells, exchange, SEBI, GST
and stamp duty. Rules were chosen on 2018-2022 and kept only if they also worked on 2023-2026.
1. **Indicator grid with stops and trails** (167,105 combinations: 16 entry families, 972 filters, 3 exits, 5 option
   structures): nothing passed; the best in-sample configurations lost out of sample. Re-tuning each year on the
   previous 3 years lost in 5 of 6 years.
2. **No stop-loss / no trail, exit on trend reversal** (~99k single-leg and ~23k multi-leg combinations): only the
   confirmed 15-minute Supertrend flip worked for single legs. A Rs 1,500 loss cap beat reversal-only exits by far.
   A far-wing iron condor on no-trend days was the strongest multi-leg result.
3. **Loss reduction:** 33 variants (cap sizes, condor filters on gap / range / opening range, single-leg VIX and DI
   filters, keeping the condor instead of closing it) - none improved both periods, so none was adopted.
4. **Filling no-trade days:** the condor at VIX below 14 (232 of 240 variants helped on both periods) and the trend
   credit spread (a narrower group helped on both) were added; a 1-lot fall-back fixed margin skips at Rs 40,000.

**Final replay of the bot's own code** (commit `54a33e3`, Rs 40,000 capital, 2 lots or 1 when 2 do not fit):

| Setup | Trades | Net (Rs) | Win rate | Profit factor |
|---|---|---|---|---|
| Single leg | 111 | +2,39,012 | 25% | 2.74 |
| Iron condor, VIX >= 14 | 711 | +3,91,955 | 69% | 2.78 |
| Iron condor, VIX < 14 | 450 | +2,77,124 | 80% | 3.75 |
| Trend credit spread | 824 | +72,441 | 47% | 1.16 |
| **Total** | **2,096** | **+9,80,531** | 60% | 2.07 |

Every year 2018-2026 positive; trades on 2,001 of 2,118 days (1,248 won, 753 lost); 88% of months positive, worst
month -Rs 7,127. At 1% slippage per fill: +Rs 6,21,380, still every year positive, but the credit spread alone turns
slightly negative. Caveats: SENSEX weekly options were illiquid before mid-2023, so the early years are a model of what
the rules would have done; not every day or month is profitable.

## Code review fixes (31 Oct 2026)
- Trend state used Kite's still-forming last 5-minute candle for a few seconds after each close, which could fake a
  flip or reversal: now only candles closed at fetch time are used.
- A trend signal was dropped when its option was not quoted yet: now retried for 60 s.
- A condor or credit spread was closed for a trend signal even when that trade could not be placed: now only when it can.
- Quiet far-OTM legs triggered a REST quote per leg per second (Kite allows ~1/s): playbook legs now accept streamed
  ticks up to 60 s old.
- No more misleading "No approved strategies" alerts while the playbook trades; the session alert shows playbook and lots.

## Layout
`config.py` `costs.py` `zerodha_client.py` `strategy_engine.py` `strategy_library.py` `risk_manager.py`
`market_data.py` `shadow.py` `recorder.py` `backtest.py` `promote.py` `token_service.py` `token_store.py`
`alerts.py` `main.py`, `tests/`, `Dockerfile`, `docker-compose.yml`, `deploy/`.
Playbook and report: `playbook.py` (setups, entries, exits), `pb_indicators.py` (pure-Python EMA / ATR / RSI / ADX /
Supertrend / MACD, 15-minute bars), `telegram_control.py` (Telegram commands), `report_site.py` (report page and
Cloud Storage upload); tests in `tests/test_playbook.py` and `tests/test_report_site.py`.

## Setup
```bash
pip install -r requirements.txt
cp .env.example .env        # fill KITE_API_KEY / KITE_API_SECRET (+ Telegram optional)
python -m unittest discover -s tests -v
```

## Legacy pipeline (approvals; entries only with `PLAYBOOK=0`)
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

## The ten structures, execution and the mocked-market harness

`option_strategies.py` holds an `OptionStrategy` base class and ten concrete strategies: bull call spread, bear put
spread, bull put spread, bear call spread, bear put ladder (4-leg, protective wing), iron condor, iron butterfly, bull
call ratio spread (long 1, short 2, long 1 far-OTM wing) and the two pure-debit ITM spreads. Each picks its strikes
from the chain using offsets in index points and works out its own max risk, max profit and breakevens from the exact
expiry payoff (a structure with unbounded loss is rejected). They are registered in the library as templates `os_*`
(about 190 variants), so they are backtested, shadow-tested and approved per account size like everything else, with
widths from 200 points for small accounts up to 1000 for larger ones.

`execution.py` places a structure in two verified phases: all BUY legs first, wait until each is confirmed filled,
only then the SELL legs. Any rejection, timeout, partial fill or network error cancels what is open and unwinds what
filled. Exits clone the structure with sides flipped and buy back shorts first; they start as marketable limit orders
with a widening buffer and switch to MARKET orders after `ExecutionParams.market_exit_after_failures` (3) failed
attempts. In live mode the exact structure is checked with `basket_order_margins` against cash before any order is sent.
Stops, profit targets (50% of max profit for debits, 80% for credits), the trailing stop and the square-off time are
enforced by the existing risk manager.

Before sizing, every planned structure is re-validated against the current chain (`validate_plan_quotes`): each leg must be a
listed, quoted contract of the right expiry, with a two-sided and tight bid/ask where depth exists, and the structure must
still be defined-risk at executable prices (BUY at the ask, SELL at the bid). Live entries then pass a margin check, run in
two verified phases, and are confirmed against the broker's positions. Exits never unwind: whatever closed stays closed,
the remainder is retried (wider limit, then MARKET), and the longs are never sold while a short buy-back is unconfirmed.

`python sim_harness.py` runs every strategy through mocked 1-minute ticks and prints how each exited. The tests in
`tests/test_execution.py` script a mock Kite client (fill, partial, reject, hang, network error) and assert the order
sequence.

### Live-readiness hardening (audit)
- Every order carries a unique tag; if a placement raises (timeout) the order book is searched by tag and a found order is adopted, never re-sent.
- After a cancel the order is polled to a final status so a late partial fill is not missed.
- Unwinds are verified (marketable limit, then MARKET); anything unconfirmed is reported, trading halts and a CRITICAL alert fires.
- Failed entries cool down (2 min x failures), halt the day after 3, and the broker position book must be flat afterwards.
- Persistently incomplete exits halt new entries; partial exits verify the remaining legs.
- An open position is still managed at square-off time if the tick feed looks stalled (REST prices).
- A malformed tick can no longer kill the WebSocket callback; `market_protection` falls back for older kiteconnect.
- Not testable without a live broker: real Kite status strings/latency, market-protection semantics, margin API shape. Stay in paper until forward evidence.

### Testing / promotion audit (backtest, shadow book, calibrate, promote)
- The backtest's trend filter used EMA on **daily** closes while the live bot used 5-minute closes, so `with`/`flat` approvals were validated on a different signal than the one traded. Live plan-building now uses `Snapshot.trend_daily` (same input as the backtest); the 5-minute trend is still recorded.
- Weekly-expiry weekday follows the real history (Friday -> Tuesday from 1 Jan 2025 -> Thursday from 1 Sep 2025) instead of a fixed Thursday; set `expiry_weekday` to force one. Verify against your instruments dump.
- The backtest uses the exchange's own expiry dates wherever the bot has seen them (every expiry in the recorded `chain` table plus today's instruments listing, from the first recorded day on). Kite serves no expired contracts, so before that it falls back to the dated weekday schedule. If a listed expiry disagrees with the schedule, `backtest.py` warns that the weekday changed.
- Lot size and strike step come from the live instruments dump when you run `backtest.py --fetch-kite` (written into `approved_backtest.json` as `contract_spec`); the live bot, shadow book and risk sizing already read them from the contracts. `BT_LOT_SIZE` / `BT_STRIKE_STEP` override for offline runs.
- Stops in the backtest fill 10% worse than the trigger (capped at max loss); CSV bars with zero/NaN/inconsistent prices are dropped.
- `calibrate.py` now solves ATM IV on the backtest's own trading-time clock. The stored calendar-time IV differs by a DTE/weekend-dependent factor, so earlier suggestions (e.g. 1.00) were not comparable with `BT_IV_SCALE`. Re-run it before changing the scale.
- Shadow book fills at the ask (buys) / bid (sells), skips structures the live bot would refuse (`validate_plan_quotes`), squares off at 15:15 even if the feed is stale, and no longer re-enters a variant after a restart. Earlier shadow results (e.g. day 1) were optimistic: treat them as discarded.
- `promote.py` also requires the mean net per trade to be >= 1.0 standard error above zero (`--min-t`).
- Daily-history cache older than 7 days is ignored (fail closed).
- Backtest approvals must also survive DOUBLE slippage (stress gate); `BT_SLIPPAGE_PCT` is configurable and `calibrate.py` now compares it with the real recorded half-spread.
- `get_ltp` rejects zero/NaN prices instead of passing them to sizing, stops or the chain.

### Independent review fixes
- Position management no longer depends on data/recording/shadow code: a failing snapshot, recorder or shadow step is logged and the open position is still stop/target/square-off managed (also when the feed looks stalled, using REST prices).
- A leg with no valid price uses its last good (or entry) price, so one dead quote cannot block a stop.
- Exit exceptions escalate (wider limit, then MARKET); exit quantities are clamped to what the broker actually holds; a POSITION_MISMATCH halt no longer triggers a blind full-size exit (only loss-control halts flatten).
- Live state is kept in its own file (`state.live.json`), so a paper restart cannot overwrite a live position's state. `start_stream` replaces any earlier socket; session start retries with back-off.
- Login link is single-use; set `KITE_USER_ID` in `.env` so a token for another Zerodha account is refused; callbacks are serialised; the `state` is redacted from logs.
- Telegram alerts are sent from a background thread, so an outage never stalls the trading loop.
- `promote.py`: a variant whose plain twin exists in the library can no longer pass the twin test without twin evidence.

### Paper exploration (so paper mode always produces trades to learn from)
In `--paper` mode, when no approved variant fires (or none is approved for your account size), `explorer.py` takes the scanner's best setups (score >= `EXPLORE_MIN_SCORE`, default 60), picks the affordable, liquid, defined-risk variant with the best (target - costs) / stop margin and paper-trades it, up to `EXPLORE_MAX_TRADES` (default 4) a day. Trades are labelled `[EXPLORE <setup> <score>]`. Set `PAPER_EXPLORE=0` to disable. It never runs in `--live` mode, and it is an experiment generator, not evidence of an edge: judge it by the recorded results after costs.

`promote.py` counts these trades as forward evidence (per one lot, after costs), but never twice: if the shadow book already traded the same variant that day, only the shadow trade counts. A variant with no backtest approval can be promoted from exploration only under a stricter bar: `--min-explore` (default 10) scanner-selected trades that are net positive, plus the forward-only bar (>= 40 trades, profit factor >= 1.3, t-stat >= `--min-t`, per account tier). `promote.py` also prints results per scanner setup, which shows which setups (if any) are worth keeping.

## Trends, traps and account-size tiers

**Valid trend / trap detection** (`trend_regime.py`). A *valid* trend is a clean one-directional move (high
efficiency ratio, fast EMA on the right side of the slow one). A *trap* is a failed breakout: price pushes past a recent
high/low then closes back inside it. A failed upside breakout (bull trap) favours bearish trades; a failed downside
breakout (bear trap) favours bullish trades. Four variant families use it (160 variants, all defined-risk spreads):

| mode | data | validated by |
|---|---|---|
| `dvalid`, `dtrap` | daily bars | backtest + forward shadow |
| `ivalid`, `itrap` | 5-minute bars | forward shadow only (stricter gates, must beat the no-filter twin when it has data) |

The classification is recorded every 10 s in the `trend` table so you can review what it saw.

**Account-size tiers.** Every backtest trade is re-sized and re-costed for a small (Rs 15,000), medium (Rs 1,00,000) and
large (Rs 5,00,000) account using the real sizing rules, then walk-forward tested per tier. `approved_backtest.json`
lists the tiers each variant passed; the bot loads only variants approved for its own account size
(below Rs 30k small, below Rs 3L medium, otherwise large). `promote.py` applies the same split to forward evidence.
`python health_report.py` shows approvals per tier.

**Low-cost directional family (small accounts).** 192 more variants: single-leg `long_call`/`long_put` and 200/300-point
debit spreads, entered only on a valid trend or trap, and skipped when round-trip costs would exceed 20% of the target
profit (`|c20` in the id). On a sample chain about 32 fit Rs 15k (debit spreads), more fit Rs 20k+. Fitting an account is not the
same as being profitable: they still have to pass the backtest (daily modes) or forward shadow gates (intraday modes).

**Opportunity scanner** (`scanner.py`). Each second-level snapshot is scored for six setups: `TREND_UP/DOWN`,
`TRAP_REVERSAL_UP/DOWN`, `RANGE_PIN` (sell premium, defined risk) and `VOL_EXPANSION` (buy premium). A setup's score is the
share of weighted checks that hold (trend/trap state, regime, OI walls, PCR, gamma, max pain, liquidity), and the passed
and missing checks are stored in the `scan` table every 10 s. It never trades by itself; it is a ranked, explained
shortlist, and the forward shadow results show whether high scores really win before anything relies on them.

Limits: nothing here guarantees a trade in every situation. A tier with no approved variant simply does not trade.

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
recorded rows, shadow-trade results and a list of problems. With the playbook on (default) the bot trades without
approvals; follow its results on the trading report page and with `/status` in Telegram. With `PLAYBOOK=0` and no
approved variants the bot records data and shadow-trades but places NO paper orders; run the backtest first (step 1 of
the legacy pipeline), then restart the bot.

## Daily token
See `deploy/DEPLOY.md`.

### Rupee exit rules on the combined position
Exits use the combined P&L of all legs. `TRAIL_MIN_PROFIT_RS` (default 2000): the trailing profit stop only arms once combined profit reaches it and never exits below it (floor = max(peak x (1 - giveback), this)); the day-level trailing stop obeys it too. `POSITION_STOP_RS` (default 2000): combined stop-loss ceiling, a variant's tighter stop still applies. Set either to 0 to disable. These are live/paper overlays; backtest and shadow results are computed without them, so they do not reflect these rules.

### Exit engine and entry filter (`exit_engine.py`, `market_context.py`, `entry_filter.py`)
**Exits** work on the combined P&L of every leg (quantities include the 20-unit lot):
- Combined hard stop at -`POSITION_STOP_RS` (2000). Between -2000 and +2000 nothing exits the trade: not variant stop/take-profit fractions, not the day-level trailing stop (disabled while the engine is on).
- Once the peak reaches +`TRAIL_MIN_PROFIT_RS` (2000) the trail arms. The floor is a ratchet (never lowered): `max(2000, peak x (1 - giveback), milestone - lag x step)`, so it steps up at 3000, 4000, ... A print strictly below the floor exits with `TRAILING_STOP`.
- `giveback` starts at 40% and is tightened or widened (15%-60%) from live signals: reversal retrace off the favourable extreme, 5-min momentum exhaustion, PCR momentum, max-pain drift, OI build-up and option-writing side, VIX change versus the structure's vega, and the theta window (from 13:30 on expiry day, from 14:15 otherwise). Widening never lowers a floor already locked.
- Not "noise", so they still exit: 15:15 square-off, daily-loss kill-switch, shutdown, broker position mismatch / exit-not-completing halts.
- Trail state (`peak`, `floor`) is saved with the position and survives restarts.

**Entry filter** (all must pass; missing data fails closed unless `ENTRY_FILTER_STRICT=0`): the structure's net-delta bias must be confirmed by the daily/intraday trend or a trap reversal with nothing opposing (neutral structures need no valid trend); IV skew (25-delta put IV - call IV) must not contradict it; price-vs-OI build-up (long/short build-up, short covering, long unwinding) or option-writing side must agree (neutral structures need quiet OI); and, with `REQUIRE_REACHABLE_ARM=1`, the plan's maximum profit must exceed the 2000 arm level. Every pass/reject is logged with the reason (`ENTRY-FILTER ...`).

The backtest and shadow book do **not** apply these exit or entry rules, so approvals still reflect the older rules.

### Margin in paper mode
Paper trades now go through the same Kite basket-margin check as live (read-only API call, no orders): lots are sized to fit `margin_utilisation` x capital, and the final entry is blocked if the whole structure needs more. A failed margin query blocks the trade. The Telegram ENTRY alert shows the margin used. Order rows in the Orders sheet are listed in execution order (BUY hedges first, then SELLs).

### Log tab (remote visibility without shell access)
With the Sheets feed enabled, `sheet_log.py` mirrors WARNING+ lines and key events (entries, exits, entry-filter passes, exit-engine arm/floor/stop/adaptation lines) to a **Log** tab every minute, with secrets redacted. Entry-filter rejections are summarised hourly by failed check. The first row after each start says which build is running (git commit, or the code files' date inside Docker). The Apps Script creates the tab automatically; no script change is needed.

### Tuning after the first paper week (Oct 26-29: 4 explore trades, all losers, net -Rs 1,991)
What the trades showed, and what changed:
- **RANGE_PIN fired at 09:30 every day.** The intraday trend needs ~14 five-minute bars; before that it reported "no
  trend, no trap", which the scanner and entry filter read as a calm range (score 100). `TrendState.ready` now marks an
  unformed read; neutral setups and neutral structures are not taken until it forms (~10:25).
- **Exits were fixed in rupees, not scaled to the structure.** A 1-lot 500-wing iron fly loses at most ~Rs 1.9k, so the
  Rs 2,000 hard stop could never fire, and the Rs 2,000 arm level was out of reach intraday: 15:15 was the only exit.
  The same Rs 2,000 arm made every narrow debit spread (max profit ~Rs 1.8k) fail the `reach` gate, so only flies were
  traded. Now stop = min(`POSITION_STOP_RS`, sl_frac x max loss) and arm = min(`TRAIL_MIN_PROFIT_RS`,
  `TRAIL_ARM_FRAC` x max profit).
- **Costs beat the edge.** Charges were Rs 157-298 per trade on gross moves of Rs 10-750. New entry checks: `cost` (arm
  level >= `MIN_ARM_COST_MULT` x round-trip charges) and `theta` (MIS premium selling must expect to earn
  `MIN_THETA_COST_MULT` x charges from intraday theta). Replayed on the three flies, all three are rejected.
- Exploration now ranks candidates by the exit engine's real arm/stop levels, not a 60% take-profit no intraday trade reaches.
None of this is a proven edge: it removes trades that could not pay for themselves. Keep paper mode until `promote.py`
approves variants on forward evidence.

### Second review (full code + data review, Oct 2026)
**Evidence used:** 6 recorded days (full chain every 10 s, depth near ATM), 898 shadow trades, 476 days of SENSEX/VIX.
- `calibrate.py` on the recorded chain: ATM IV / India VIX = 0.951 (backtest assumes 0.95) and the median half-spread
  is 0.13% (backtest charges 1%): the model is not flattering itself.
- Walk-forward backtest: the average out-of-sample result of the variants picked in each training window is
  **negative (-Rs 177 per variant per window)**, and no variant passes for the small (Rs 15k) or medium (Rs 1L) tier.
  Selecting from ~1,000 variants does not find a durable edge on daily data; only paper/shadow evidence can.
- A minute-by-minute replay of the recorded days through the real `Bot` and `ShadowBook` reproduces the live paper
  trades (same structure, same entry time). Tuning exits on it moved the shadow total by only +-10% for every setting
  tried: losses come from entries (mostly direction: the daily trend pointed down in a rising week), not exits.

**Bugs fixed**
- Shadow book built plans with the 5-minute trend (`trend_dir`) while the live bot and backtest use the daily trend:
  forward evidence for every `with`/`flat` variant measured a different signal than the one traded.
- Shadow book exited with the old percentage rules while the bot uses the exit engine: it now uses the same engine
  (quiet logging), so `promote.py` approves on the rules that will actually trade.
- Paper fills were LTP +/- 0.5%; they now pay the real bid/ask from the chain (as the shadow book does), so paper
  results are as pessimistic as live execution.
- A restart after the close repeated the day summary (Status tab / Telegram): `eod_done` is now persisted.
- After a partial exit, the Trades row and the `exit` record (used by `promote.py`) held only the last fill: they now
  carry the whole trade.
- Removed `StrategyEngine.select()` and helpers: dead code referencing config fields that do not exist.

**New**
- Exploration skips strategy families with clearly negative forward evidence (walk-forward, see `EXPLORE_FWD_*`).
- `promote.py --since YYYY-MM-DD` ignores evidence recorded before a rules change. Shadow trades before this version
  used the old trend/exit rules: run `python promote.py --since 2026-10-30` once enough new data exists.

### Intraday price-action strategies (intraday.py, intraday_backtest.py)
**Data:** 5-minute SENSEX and India VIX bars, Jan 2023 - Oct 2026 (~945 sessions), from Kite. Options priced with
Black-Scholes at VIX x 0.951 (measured on recorded chains), 0.5% slippage per fill (2x stress), real charges.

**What the data says about SENSEX intraday (so the strategy design follows evidence, not folklore)**
- Intraday realised volatility is only **0.58 x India VIX**: options are priced for bigger moves than the session
  delivers. Holding bought options intraday fights that premium; option WRITERS (institutions) collect it.
- Only 5% of sessions trend cleanly (efficiency ratio > 0.3); prior-day-high tests fail (trap) more often than they
  break out (238 vs 204); 30-minute opening-range breakouts follow through barely more than they fail (251 vs 209).
- Walk-forward over 11 windows, every common rule LOSES before filters: breakout option buying, trap fading
  (worst: fading "traps" mostly means betting against real breakouts), range-day condors (gross positive, but
  ~Rs 220 of charges per 4-leg lot eats it).

**What survived** (filters that helped in BOTH 2023-24 and 2025-26):
- Buy a put on a confirmed downside breakdown of a key level (prior-day H/L, CPR, opening range, swing levels), on a
  NON-sideways day: CPR width < 0.25%, the day's range >= 0.5 x ADR, session efficiency >= 0.2, room to the next
  support >= 1.5 risk units, target 2.5R, stop back above the broken level. Calls on upside breakouts lose: rallies
  crush implied volatility; selloffs inflate it.
- Multi-leg (medium/large accounts): sell a 500-wide credit spread beyond the breakout/breakdown stop - writing the
  option where trapped traders' stops sit.

**Honest limits:** ~100 trades per variant in 3.8 years (about 2 a month). The best put-buying variant lost in 2023
and 2025 and made money in 2024 and 2026; max drawdown about Rs 19-20k per lot - the whole of a Rs 20k account.
The direction rule and the filters were chosen after looking at the data (with a discovery/validation split), so
treat backtest numbers as optimistic. Hence:
- paper mode trades the approved variants for every tier, but `promote.py` promotes a variant to LIVE for an account
  tier only if that tier's capital >= 1.5 x the backtest max drawdown per lot (`--dd-capital-mult`);
- accounts below `MULTI_LEG_MIN_CAPITAL` (Rs 1 lakh) never trade multi-leg structures;
- signal trades are sized on their stop (rupee hard stop + 25%), not the whole premium, and exit on the underlying
  (stop / target / breakeven after +1R) exactly as backtested; the entry filter and rupee trail do not apply to them;
- with validated strategies available, the paper bot no longer explores unvalidated scanner setups
  (`EXPLORE_WITH_APPROVED=0`).

**Run / refresh** (weekly; approvals expire after 60 days):
```bash
docker compose exec bot python intraday_backtest.py --fetch-kite 1400     # or --index data/sensex_5m.csv --vix data/vix_5m.csv
docker compose restart bot
```

### Calls vs puts, ITM strikes, pullbacks (revisited, Oct 2026)
Protocol: rules designed on 2023-2024 only, each direction's chosen rule tested once on 2025-2026, then the full
walk-forward decides approvals.
- **Calls need the sideways-day filter** (+31/trade with it vs -218 without, 2023-24), confirmation, a 2.5R target and
  in-the-money strikes (200 ITM: -111/trade vs 100 OTM: -170 across all call variants) - ITM premium is mostly
  intrinsic, so the IV crush that comes with rallies hurts less.
- Even so, the best call rule was only breakeven on 2025-26 (PF 1.08) and LOST in the full walk-forward (out-of-sample
  PF 0.94, max drawdown ~Rs 25k per lot). No call-buying variant is approved. They stay in the library, so the shadow
  book keeps forward-testing them and `promote.py` will pick one up if live evidence turns positive.
- The pullback-retest entry looked good in 2023-24 calls but lost Rs 45k on 2025-26 (PF 0.50): rejected.
- Calls and puts are separate variants now (`dir` +1 / -1). A "both directions" variant could pass on its put trades
  and switch call buying on by itself; that can no longer happen.
