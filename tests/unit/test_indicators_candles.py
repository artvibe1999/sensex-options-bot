import datetime as dt
import unittest

from tests.helpers import IST, Bar, cd, day, one_minute, session, t

from sensexpro import indicators as ind


class TestIndicators(unittest.TestCase):
    def test_ema_seed_and_warmup(self):
        e = ind.ema([1, 2, 3, 4, 5, 6], 3)
        self.assertEqual(e[:2], [None, None])
        self.assertAlmostEqual(e[2], 2.0)                 # SMA seed
        self.assertAlmostEqual(e[3], 2.0 + 0.5 * (4 - 2))  # k = 2/(n+1) = 0.5

    def test_rsi_bounds_and_extremes(self):
        up = ind.rsi(list(range(1, 40)), 14)
        self.assertIsNone(up[13])
        self.assertEqual(up[20], 100.0)
        down = ind.rsi(list(range(40, 1, -1)), 14)
        self.assertEqual(down[20], 0.0)

    def test_macd_lengths_and_warmup(self):
        c = [100 + (i % 7) for i in range(80)]
        line, sig, hist = ind.macd(c)
        self.assertEqual(len(hist), 80)
        self.assertIsNone(hist[30])                       # needs 26 + 9 - 1 bars
        self.assertIsNotNone(hist[40])

    def test_atr_and_adx_warmup(self):
        bars = session(day(0), [50000 + 10 * k for k in range(60)])
        a = ind.atr(bars, 14)
        self.assertIsNone(a[12])
        self.assertGreater(a[20], 0)
        adx, pdi, mdi = ind.adx(bars, 14)
        self.assertIsNone(adx[26])
        self.assertIsNotNone(adx[27])
        self.assertGreater(pdi[40], mdi[40])              # rising series: +DI dominates

    def test_bollinger(self):
        mid, up, lo, bw = ind.bollinger([10.0] * 25, 20, 2)
        self.assertEqual(mid[19], 10.0)
        self.assertEqual(up[19], 10.0)
        self.assertEqual(bw[19], 0.0)
        self.assertIsNone(mid[18])

    def test_vwap_resets_each_session_and_falls_back_without_volume(self):
        a = session(day(0), [100.0, 110.0], rng=0)
        b = session(day(1), [200.0, 210.0], rng=0)
        v, fb = ind.session_vwap(a + b)
        self.assertFalse(fb)
        self.assertAlmostEqual(v[2], (200 + 200 + 200) / 3)   # day 2 restarts: first bar typical price
        nv = [Bar(x.start, 5, x.open, x.high, x.low, x.close, None) for x in a]
        v2, fb2 = ind.session_vwap(nv)
        self.assertTrue(fb2)
        self.assertIsNotNone(v2[1])

    def test_volume_ratio_missing_is_none(self):
        bars = session(day(0), [100.0 + k for k in range(25)], vols=[1000.0] * 24 + [3000.0])
        r = ind.volume_ratio(bars, 20)
        self.assertAlmostEqual(r[24], 3.0)
        nb = bars[:24] + [Bar(bars[24].start, 5, bars[24].open, bars[24].high, bars[24].low, bars[24].close, None)]
        self.assertIsNone(ind.volume_ratio(nb, 20)[24])

    def test_percentile_rank_uses_only_past(self):
        vals = [float(i) for i in range(30)]
        p = ind.percentile_rank(vals, 10)
        self.assertEqual(p[20], 100.0)                    # larger than every earlier value
        vals2 = vals[:20] + [-1.0] + vals[21:]
        self.assertEqual(ind.percentile_rank(vals2, 10)[20], 0.0)
        self.assertEqual(ind.percentile_rank(vals2, 10)[19], p[19])   # future change does not alter the past


class TestCandles(unittest.TestCase):
    def test_parse_rejects_duplicates_disorder_bad_ohlc_and_out_of_session(self):
        d = day(0)
        rows = [
            {"date": dt.datetime(d.year, d.month, d.day, 9, 14), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            {"date": dt.datetime(d.year, d.month, d.day, 9, 15), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 5},
            {"date": dt.datetime(d.year, d.month, d.day, 9, 15), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 5},
            {"date": dt.datetime(d.year, d.month, d.day, 9, 17), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 5},
            {"date": dt.datetime(d.year, d.month, d.day, 9, 16), "open": 10, "high": 11, "low": 9, "close": 10, "volume": 5},
            {"date": dt.datetime(d.year, d.month, d.day, 9, 18), "open": 10, "high": 9, "low": 9, "close": 10, "volume": 5},
        ]
        bars, problems = cd.parse_rows(rows)
        self.assertEqual([b.start.minute for b in bars], [15, 17])
        self.assertEqual(len(problems), 3)                # duplicate, out-of-order, bad OHLC
        self.assertTrue(all(b.start.tzinfo is not None for b in bars))

    def test_completed_drops_forming_bar(self):
        bars = one_minute(day(0), [100.0, 101.0, 102.0])
        self.assertEqual(len(cd.completed(bars, t(day(0), 9, 17) + dt.timedelta(seconds=30))), 2)

    def test_aggregate_aligned_complete_only_and_gap_flag(self):
        d = day(0)
        bars = one_minute(d, [100.0 + k for k in range(12)])          # 09:15 .. 09:26
        b5 = cd.aggregate(bars, 5)
        self.assertEqual([b.start.minute for b in b5], [15, 20])      # 09:25 bucket still forming
        self.assertEqual(b5[0].open, bars[0].open)
        self.assertEqual(b5[0].close, bars[4].close)
        self.assertEqual(b5[0].volume, 500.0)
        gappy = bars[:2] + bars[3:]
        g5 = cd.aggregate(gappy, 5)
        self.assertTrue(g5[0].gap)
        self.assertFalse(g5[1].gap)
        b15 = cd.aggregate(one_minute(d, [100.0] * 31), 15)
        self.assertEqual([(b.start.minute, b.minutes) for b in b15], [(15, 15), (30, 15)])

    def test_with_volume_and_missing_minutes(self):
        d = day(0)
        idx = one_minute(d, [100.0, 101.0, 102.0], vol=None)
        fut = one_minute(d, [50.0, 50.0], vol=7.0)
        merged = cd.with_volume(idx, fut)
        self.assertEqual([b.volume for b in merged], [7.0, 7.0, None])
        miss = cd.missing_minutes(idx[:1] + idx[2:], t(d, 9, 15), t(d, 9, 18))
        self.assertEqual(miss, [t(d, 9, 16)])

    def test_bar_rejects_naive_and_invalid(self):
        with self.assertRaises(ValueError):
            Bar(dt.datetime(2026, 1, 1, 9, 15), 1, 1, 1, 1, 1)
        with self.assertRaises(ValueError):
            Bar(dt.datetime(2026, 1, 1, 9, 15, tzinfo=IST), 1, 10, 9, 8, 10)


if __name__ == "__main__":
    unittest.main()
