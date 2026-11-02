# PLAN: multi-index scanner, 1-lot trades, "stop after the first winning trade" (2026-11-02)

Status: **implemented 2026-11-03** (paper by default). Decisions taken by the user: all six indices scanned; lots
SENSEX 2, NIFTY 1, FINNIFTY 1, MIDCPNIFTY 1, BANKNIFTY 2, NIFTYNXT50 2; paper / live switch from Telegram; no spread
recording outside SENSEX; stop after the day's first profitable trade.

Phase 0 result (lab6, bot playbook code on Kite 5-minute history 2018-2026, real margins, Zerodha charges): the SENSEX
settings copied unchanged LOSE on every monthly-only index (intraday decay of a monthly option is too small for the
costs). Trading the monthly indices only in their expiry week fixed BANKNIFTY (credit spread) and MIDCPNIFTY (wider
condor); NIFTY passes with the single leg and a wider condor. FINNIFTY and NIFTYNXT50: no setup passed both periods
(NIFTYNXT50 options barely trade), so they are scanned but take no trades. The four-index portfolio under the daily rule
is about as profitable as SENSEX alone (+11.6L vs +11.9L 2018-26) and trades ~46 more days; ~37% of days still lose.

Differences from the plan below: no 1-lot hard cap (the user chose lots per index); the evidence gate disables
FINNIFTY / NIFTYNXT50 instead of keeping them off the scan.

## 1. What exists today (discovery)

| Area | Module | Today |
|---|---|---|
| Main loop | `main.py` (1,052 lines) | One thread, 1 s cycle: snapshot, playbook decision, entry/exit, report |
| Broker | `zerodha_client.py` | Kite REST + KiteTicker WebSocket; **SENSEX only** (`load_instruments` reads BFO `SENSEX`); one `_RateLimiter` (fixed gap) plus retry with exponential backoff (1-20 s) on network errors; `TokenException` is fatal |
| Market data | `market_data.py`, `market_context.py` | One underlying (SENSEX) + India VIX; 5-minute history for the trend state |
| Decisions | `playbook.py`, `pb_indicators.py` | Single leg / iron condor / credit spread rules, tuned on SENSEX weekly options |
| Orders | `execution.py` | Limit orders with re-pricing, idempotent order tags (`SXALGO…`), paper broker |
| Risk | `risk_manager.py`, `config.py` | Daily loss halt, margin limit (80% of capital), max 2 trades/day, per-trade Rs 1,500 loss cap |
| State | `data/state.json` (atomic write), `recorder.py` (SQLite) | Restart-safe position + day state |
| Tests | `tests/` (199 tests, unittest style; pytest runs them as is) | No lint configuration |
| VM | e2-small (2 shared vCPU, 2 GB) | Docker compose: bot, token service, Caddy |

## 2. Facts from Kite (2 Nov 2026) that shape the design

| Index | Exchange | Lot | Expiries | 1-lot exposure margin per sold option (2% of notional) |
|---|---|---|---|---|
| SENSEX | BFO | 20 (25 from the Jan 2027 monthly) | **weekly** | ~Rs 30,300 |
| NIFTY 50 | NFO | 65 | **weekly** | ~Rs 30,500 |
| BANKNIFTY | NFO | 30 | **monthly only** | ~Rs 33,000 |
| FINNIFTY | NFO | 60 | **monthly only** | ~Rs 29,900 |
| MIDCPNIFTY | NFO | 120 | **monthly only** | ~Rs 32,100 |
| NIFTYNXT50 | NFO | 25 | **monthly only** | ~Rs 34,500 |

Consequences:
1. **Only NIFTY and SENSEX have weekly options.** The playbook's strikes and exits are built around a weekly expiry
   (strike chosen by trading days left, theta). On the four monthly-only indices the same rules face very different
   time decay; they must be re-validated, not copied.
2. **No index is cheaper to trade.** Every one needs ~Rs 30-35k exposure margin per sold option, so spreads and
   condors cost about the same everywhere; single legs need only the premium.
3. **The playbook has been backtested on SENSEX only.** A cross-index scorer can only rank setups whose edge has been
   measured; without that, "highest-probability" is a guess. Phase 0 below produces that evidence.
4. Kite limits (published): quotes 1 request/s, historical 3/s, orders 10/s (and per-minute/day caps), other calls
   10/s; WebSocket 3,000 instruments per connection. Six chains at ATM +/- 10 strikes is ~250 tokens: one
   connection is enough, no polling of option chains is needed.

## 3. Design

### 3.1 Phase 0: evidence gate (research, before any live-path code)
- Replay the bot's playbook code per index on Kite 5-minute index history (2018-2026 where Kite has it), option prices
  from the lab model with an IV proxy per index calibrated on recorded live quotes (India VIX fits NIFTY/SENSEX; the
  others need their own scale, measured from the chain during market hours).
- Measure real bid-ask spreads and depth per index during a market session (record ATM +/- 10 strikes for a day).
- Real Kite margins (basket margin, the calibrated formula) and Zerodha's NSE charges (verify NFO exchange/stamp rates
  against the virtual contract note, as was done for BFO).
- Same gate as the SENSEX work: choose on 2018-22, keep an index/setup only if it is also positive on 2023-26, also at
  double slippage. **Only indices that pass are enabled** in the scanner (config flag per index).
- Also backtest the "stop after the first winning trade" rule (3.4) on SENSEX: it changes results and must be
  measured before it goes in.

### 3.2 New modules
| Module | Purpose |
|---|---|
| `indices.py` | Frozen registry `IndexSpec(name, exchange, option_name, spot_key, strike_step, expiry_kind, iv_scale, enabled)`; the only place index constants live |
| `models.py` | Pydantic v2 models for data crossing the broker boundary: instrument rows, ticks (only the fields used), quotes/depth, order responses, contract-note rows. Invalid payloads are dropped and logged, never trusted |
| `multi_chain.py` | Per-index chain built from WebSocket ticks for the nearest tradable expiry: normalised quote rows (mid, spread %, depth, IV by Black-Scholes inversion, delta), re-centred when spot moves a strike; stale-tick detection |
| `scorer.py` | Pure functions, no I/O: for each enabled index, the playbook decision for that index (generalised playbook params) -> candidate trades -> score = backtested expected value per trade for that index/setup minus the measured cost now (spread + charges), divided by worst-case loss; liquidity floor (max spread %, min depth); deterministic tie-break. Returns the single best candidate or none |
| `day_guard.py` | Day state machine, persisted in `state.json`: `ACTIVE -> LOCKED_WIN` (first closed trade with net > 0), `LOCKED_LOSS` (daily loss limit), `LOCKED_MAX` (max trades). A lock blocks entries only; an open position is still managed to its exit. Restart-safe |
| `rate_limit.py` | Thread-safe token buckets per Kite endpoint class (quote 1/s, historical 3/s, orders 10/s + per-minute cap, other 10/s) with an injectable clock; a circuit breaker (closed / open / half-open) with exponential backoff and jitter; replaces `_RateLimiter` |

### 3.3 Changes to existing code
- `zerodha_client.py`: load NFO + BFO instruments for the enabled indices; per-index contracts, expiries and spot
  tokens; every REST call through `rate_limit.py`; the breaker wraps network calls; WebSocket subscribes the union of
  all enabled chains; tokens/secrets never logged (already true: keep and test).
- `playbook.py`: rules parameterised by `IndexSpec` (strike step, expiry kind, VIX proxy); SENSEX defaults unchanged.
- `main.py`: with `MULTI_INDEX=1` the cycle calls the scanner instead of the SENSEX-only playbook path; one position at
  a time across all indices; the day guard is checked in the same thread, immediately before an order is sent.
  `MULTI_INDEX=0` (default until Phase 0 passes) keeps today's behaviour exactly.
- `config.py` / Telegram: **1 lot hard cap** (`MAX_LOTS_PER_TRADE=1` enforced in the order path, not only in
  settings; `/lots` above 1 is refused while it is set); per-index enable flags.
- `costs.py`: per-exchange charge rates (NSE options differ from BSE), verified against Zerodha.
- `report_site.py`: index column on paper and live trades.

### 3.4 The daily rule, precisely
- Entries stop for the day as soon as **one trade closes with a net profit** (after charges), or the daily loss limit
  is hit, or max trades are reached.
- A losing trade does not lock: the bot may take another (up to max trades / the loss limit).
- This rule cannot make a day profitable; it only stops trading after a win. Its effect on returns is measured in
  Phase 0 before it is switched on.

### 3.5 Concurrency, latency, CPU
- Keep the order path **single-threaded** (the main loop): no two orders can race, and the day guard and position
  check happen in the same thread right before placement. Market data already arrives on the KiteTicker thread; shared
  tick state stays behind its existing lock. An asyncio rewrite would add risk without lowering latency here (Kite's
  limits, not Python, bound the order rate).
- Scoring is pure and cheap (6 chains x ~40 rows per second). Bounded memory: fixed-size per-token tick buffers,
  caches with size caps (as `_margin_cache` already has), no per-tick logging.
- WebSocket: reconnect with backoff (KiteTicker's, plus a stall watchdog that already exists); after a reconnect,
  chains are re-subscribed and marked stale until fresh ticks arrive.

### 3.6 Security
- `.env` permissions 600 checked at start-up (warn and refuse live mode if wider); secrets only from the environment;
  no secrets in logs, Telegram or the report (tests assert it).
- Validate every broker payload (models.py); orders only for instruments in the loaded registry; quantity always
  `lot_size x 1`, asserted again in the order path.
- Idempotent order tags already exist: keep and extend them to every index.

### 3.7 Tests and tooling
- Add `pytest` and `ruff` as dev dependencies (`requirements-dev.txt`); existing unittest tests run unchanged and are
  never edited to pass.
- New tests: index registry, payload validation (bad/missing fields), chain normalisation and re-centring, scorer
  ranking and liquidity floors, token bucket and breaker on a fake clock, day guard transitions and restart
  persistence, 1-lot cap in the order path, an integration test running the main loop on the paper broker with
  three fake indices.
- Run everything on the VM in the bot image (`docker run ... python -m pytest`), plus `ruff check`.

## 4. Phases and commits
| # | Work | Commit(s) |
|---|---|---|
| 0 | Evidence: per-index replay, live spread recording (1 market day), margins, NSE charges, day-rule backtest -> report to you; you decide which indices to enable | lab only; one commit for the spread recorder |
| 1 | `rate_limit.py` + breaker, wired into `zerodha_client.py` | 1-2 |
| 2 | `models.py` + validation at the broker boundary | 1 |
| 3 | `indices.py`, multi-exchange instruments, `multi_chain.py` | 2 |
| 4 | `day_guard.py` + 1-lot hard cap | 1-2 |
| 5 | `scorer.py`, playbook parameterised by index, `main.py` wiring behind `MULTI_INDEX` | 2-3 |
| 6 | costs per exchange, report index column | 1 |
| 7 | Verification: full tests + ruff on the VM, self-review (concurrency, memory, security), paper deploy with `MULTI_INDEX` only for indices that passed | 1 |

## 5. Decisions needed from you
1. **Evidence gate:** enable an index only if the playbook passes the backtest on it (recommended), or enable all six
   regardless.
2. **The daily rule:** confirm "stop after the first trade that closes with a profit; a losing trade does not stop
   trading (until the loss limit or max trades)".
3. **1 lot:** confirm 1 lot replaces today's 2-lot default (it roughly halves backtest P&L at Rs 2,00,000 and halves
   the risk per trade).
4. **Phase 0 needs one live market session** to record spreads on the other indices (the bot can record tomorrow
   without trading on them).
