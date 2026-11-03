# Research results: underlying-signal research (NOT an options P&L backtest)

Run on 3 Nov 2026 with `python -m sensexpro.research.backtest --no-volume-filter` (code in `sensexpro/research/`).

* Data: Kite 5-minute index candles 2018-01 .. 2026-11 (MIDCPNIFTY from 2022), India VIX 5-minute. Kite serves no history for expired option contracts and no intraday history for futures (continuous data is daily only, checked 3 Nov 2026), so there is **no historical volume**: the volume thresholds were switched off (every bar volume 1, volume score 6/15) and VWAP is the equal-weighted session average.
* Trades: the live strategy, regime, ensemble and adaptive-exit code, one position at a time per index, max 3 entries a day, 10-minute cool-down, stop after 2 losses in a row, entry at the next 5-minute bar's open, exit at the bar close that triggers (structure / score) or 15:15. Option liquidity assumed 7/10 points; option-health inputs missing (scored at half weight, as the live engine does).
* `points` = index points in the trade's direction. `model Rs/lot` = a MODEL of one ATM option lot: 0.5 x points x lot, minus ATM time decay over the holding time (premium 0.4 x S x VIX x 0.95 x sqrt(T), 3 trading days to expiry for SENSEX / NIFTY weeklies, 10 for monthlies), minus a 0.5 % spread and Zerodha charges. Lots: SENSEX 20, NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120, NIFTYNXT50 25.
* Walk-forward: nothing was tuned. The brief's default parameters are evaluated on three fixed chronological periods.

## Summary

| Index | Trades | Win % | Points/trade | PF (points) | Max DD (points) | Worst losing streak | Model Rs/lot | 2018-21 | 2022-23 | 2024-26 | Months + | Years + |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SENSEX | 1,208 | 36.8 | 4.44 | 1.07 | 5,108 | 14 | -423,820 | -122,044 | -119,235 | -182,542 | 18.1 % | 0/9 |
| NIFTY | 1,228 | 37.1 | 1.34 | 1.08 | 1,398 | 15 | -425,085 | -160,588 | -101,640 | -162,857 | 17.8 % | 0/9 |
| BANKNIFTY | 1,173 | 33.8 | 0.2 | 1.0 | 6,961 | 17 | -376,460 | -69,713 | -119,812 | -186,935 | 25.5 % | 2/9 |
| FINNIFTY | 1,191 | 37.1 | 2.02 | 1.08 | 2,749 | 14 | -290,937 | -17,960 | -120,996 | -151,981 | 31.1 % | 2/9 |
| MIDCPNIFTY | 701 | 41.5 | 2.65 | 1.19 | 893 | 13 | -111,416 | - | -43,626 | -67,790 | 34.5 % | 1/5 |
| NIFTYNXT50 | 1,048 | 41.0 | 31.36 | 1.52 | 3,795 | 11 | 67,190 | 106,609 | -60,080 | 20,661 | 38.9 % | 2/9 |

NIFTYNXT50's modelled profit is not credible as tradable: its options trade very little (about 33 lots near the money on 2 Nov 2026), so the 0.5 % spread assumption is far too kind, and it lost in 2022-23.

## Do the signals predict direction? (index points 30 minutes after every scored signal)

| Index | Strategy | Signals | Mean points | Hit % |
|---|---|---:|---:|---:|
| SENSEX | bollinger_squeeze | 151 | 0.91 | 49.7 |
| SENSEX | ema_pullback | 328 | 0.2 | 48.2 |
| SENSEX | failed_breakout | 7,448 | 0.01 | 50.3 |
| SENSEX | orb | 633 | -6.77 | 47.7 |
| SENSEX | vwap_continuation | 1,886 | -0.4 | 49.2 |
| NIFTY | bollinger_squeeze | 163 | 0.81 | 46.0 |
| NIFTY | ema_pullback | 351 | -2.18 | 48.4 |
| NIFTY | failed_breakout | 7,596 | 0.47 | 50.1 |
| NIFTY | orb | 652 | -1.45 | 46.6 |
| NIFTY | vwap_continuation | 2,001 | -0.19 | 50.8 |
| BANKNIFTY | bollinger_squeeze | 125 | 3.68 | 52.0 |
| BANKNIFTY | ema_pullback | 315 | 2.84 | 49.5 |
| BANKNIFTY | failed_breakout | 8,284 | -0.04 | 49.9 |
| BANKNIFTY | orb | 581 | -9.0 | 46.0 |
| BANKNIFTY | vwap_continuation | 1,763 | 1.87 | 49.7 |
| FINNIFTY | bollinger_squeeze | 146 | 3.29 | 61.0 |
| FINNIFTY | ema_pullback | 326 | -3.61 | 45.1 |
| FINNIFTY | failed_breakout | 7,913 | -0.84 | 49.5 |
| FINNIFTY | orb | 638 | 0.97 | 50.0 |
| FINNIFTY | vwap_continuation | 1,876 | 2.0 | 48.9 |
| MIDCPNIFTY | bollinger_squeeze | 76 | -0.32 | 48.7 |
| MIDCPNIFTY | ema_pullback | 187 | -5.59 | 44.4 |
| MIDCPNIFTY | failed_breakout | 3,615 | -0.72 | 48.4 |
| MIDCPNIFTY | orb | 336 | 1.61 | 51.5 |
| MIDCPNIFTY | vwap_continuation | 1,142 | 1.82 | 51.8 |
| NIFTYNXT50 | bollinger_squeeze | 137 | 6.24 | 50.4 |
| NIFTYNXT50 | ema_pullback | 312 | -1.5 | 51.6 |
| NIFTYNXT50 | failed_breakout | 5,461 | 0.71 | 50.0 |
| NIFTYNXT50 | orb | 581 | 2.1 | 46.0 |
| NIFTYNXT50 | vwap_continuation | 1,768 | 8.89 | 51.3 |

## By strategy (trades taken)

| Index | Strategy | Trades | Win % | Points/trade | Model Rs/lot |
|---|---|---:|---:|---:|---:|
| SENSEX | bollinger_squeeze | 19 | 26.3 | -54.25 | -17,458 |
| SENSEX | ema_pullback | 20 | 30.0 | 3.81 | -5,895 |
| SENSEX | failed_breakout | 108 | 38.9 | 4.75 | -31,217 |
| SENSEX | orb | 380 | 36.3 | 0.16 | -149,587 |
| SENSEX | vwap_continuation | 681 | 37.2 | 8.44 | -219,663 |
| NIFTY | bollinger_squeeze | 23 | 39.1 | -11.04 | -16,588 |
| NIFTY | ema_pullback | 20 | 25.0 | -17.13 | -17,466 |
| NIFTY | failed_breakout | 107 | 30.8 | 0.12 | -33,967 |
| NIFTY | orb | 388 | 34.8 | 0.65 | -143,082 |
| NIFTY | vwap_continuation | 690 | 39.7 | 2.86 | -213,982 |
| BANKNIFTY | bollinger_squeeze | 20 | 45.0 | 53.83 | 9,113 |
| BANKNIFTY | ema_pullback | 20 | 35.0 | 22.49 | -35 |
| BANKNIFTY | failed_breakout | 95 | 35.8 | -7.51 | -38,175 |
| BANKNIFTY | orb | 336 | 30.1 | -8.6 | -147,498 |
| BANKNIFTY | vwap_continuation | 702 | 35.0 | 3.3 | -199,864 |
| FINNIFTY | bollinger_squeeze | 19 | 57.9 | 24.05 | 7,105 |
| FINNIFTY | ema_pullback | 18 | 27.8 | -4.87 | -6,857 |
| FINNIFTY | failed_breakout | 74 | 33.8 | -5.05 | -29,269 |
| FINNIFTY | orb | 381 | 35.2 | 0.74 | -103,572 |
| FINNIFTY | vwap_continuation | 699 | 38.2 | 3.05 | -158,345 |
| MIDCPNIFTY | bollinger_squeeze | 13 | 15.4 | -12.03 | -12,736 |
| MIDCPNIFTY | ema_pullback | 10 | 10.0 | -19.43 | -13,916 |
| MIDCPNIFTY | failed_breakout | 39 | 43.6 | 8.36 | 7,393 |
| MIDCPNIFTY | orb | 217 | 39.6 | 1.39 | -49,732 |
| MIDCPNIFTY | vwap_continuation | 422 | 43.8 | 3.74 | -42,425 |
| NIFTYNXT50 | bollinger_squeeze | 16 | 62.5 | 142.67 | 22,258 |
| NIFTYNXT50 | ema_pullback | 14 | 50.0 | 43.21 | 2,652 |
| NIFTYNXT50 | failed_breakout | 51 | 39.2 | 234.31 | 133,696 |
| NIFTYNXT50 | orb | 349 | 35.5 | 9.52 | -65,308 |
| NIFTYNXT50 | vwap_continuation | 618 | 43.5 | 23.8 | -26,108 |

## By regime, direction, time of day, VIX (SENSEX and NIFTY)

**SENSEX by regime:** HIGH_VOLATILITY 67 trades, 44.13 pts, Rs -4,026; RANGE 107 trades, 3.39 pts, Rs -31,855; TREND_DOWN 489 trades, -0.3 pts, Rs -195,533; TREND_UP 545 trades, 4.03 pts, Rs -192,406

**SENSEX by direction:** CE 623 trades, 5.99 pts, Rs -206,352; PE 585 trades, 2.79 pts, Rs -217,468

**SENSEX by time:** 09:30-10:30 626 trades, 1.81 pts, Rs -253,090; 10:30-12:00 250 trades, 9.35 pts, Rs -78,530; 12:00-13:30 190 trades, 7.15 pts, Rs -53,082; 13:30+ 142 trades, 3.77 pts, Rs -39,120

**SENSEX by vix:** 13-18 554 trades, -3.75 pts, Rs -226,014; <13 307 trades, 3.07 pts, Rs -99,154; >=18 344 trades, 18.64 pts, Rs -98,708; unknown 3 trades, 30.31 pts, Rs 55

**SENSEX by exit:** SCORE 22 trades, -105.96 pts, Rs -29,477; SQUARE 132 trades, 255.37 pts, Rs 260,211; STRUCTURE 1054 trades, -24.68 pts, Rs -654,554

**SENSEX by year:** 2018 114 trades, -8.7 pts, Rs -37,259; 2019 132 trades, 6.22 pts, Rs -28,230; 2020 142 trades, 19.34 pts, Rs -30,240; 2021 134 trades, 21.21 pts, Rs -26,315; 2022 145 trades, -9.16 pts, Rs -74,193; 2023 146 trades, 1.39 pts, Rs -45,042; 2024 133 trades, 16.28 pts, Rs -49,558; 2025 149 trades, 6.82 pts, Rs -57,424; 2026 113 trades, -18.65 pts, Rs -75,559

**NIFTY by regime:** HIGH_VOLATILITY 73 trades, 10.13 pts, Rs -9,208; RANGE 107 trades, 0.12 pts, Rs -33,967; TREND_DOWN 495 trades, -0.9 pts, Rs -211,865; TREND_UP 553 trades, 2.42 pts, Rs -170,045

**NIFTY by direction:** CE 645 trades, 2.18 pts, Rs -201,848; PE 583 trades, 0.41 pts, Rs -223,237

**NIFTY by time:** 09:30-10:30 660 trades, 1.53 pts, Rs -242,496; 10:30-12:00 248 trades, 0.22 pts, Rs -97,575; 12:00-13:30 164 trades, 3.0 pts, Rs -42,813; 13:30+ 156 trades, 0.56 pts, Rs -42,201

**NIFTY by vix:** 13-18 578 trades, 2.68 pts, Rs -171,882; <13 319 trades, -2.23 pts, Rs -132,137; >=18 330 trades, 2.39 pts, Rs -121,083; unknown 1 trades, 18.95 pts, Rs 18

**NIFTY by exit:** SCORE 25 trades, -31.4 pts, Rs -32,135; SQUARE 137 trades, 68.66 pts, Rs 227,551; STRUCTURE 1066 trades, -6.54 pts, Rs -620,500

**NIFTY by year:** 2018 140 trades, -2.44 pts, Rs -44,971; 2019 134 trades, -0.95 pts, Rs -40,647; 2020 133 trades, 3.81 pts, Rs -34,727; 2021 130 trades, 2.5 pts, Rs -40,242; 2022 143 trades, -1.21 pts, Rs -65,373; 2023 149 trades, 2.49 pts, Rs -36,268; 2024 134 trades, 9.16 pts, Rs -31,030; 2025 155 trades, -0.97 pts, Rs -77,076; 2026 110 trades, 0.07 pts, Rs -54,751

## Conclusion

The five strategies, as specified, do not predict the next 30 minutes of the index (hit rate about 50 %, mean move about 0 points for every strategy on every index). The adaptive exit lets winners run (average win about 1.8 x average loss on SENSEX), which keeps the points total slightly positive, but buying an option pays time decay, the spread and charges on every trade, and that turns every index except the illiquid NIFTYNXT50 negative in all three periods. This matches the earlier 167,000-configuration intraday study (no robust intraday option-buying edge). Paper trading can confirm or refute this with real option prices, but there is no evidence here to expect a profit.
