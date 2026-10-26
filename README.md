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
recorded rows, shadow-trade results and a list of problems. With no approved variants the bot records data and
shadow-trades but places NO paper orders; run the backtest first (step 1 above), then restart the bot.

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
