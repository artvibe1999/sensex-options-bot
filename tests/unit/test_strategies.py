"""Each strategy: a positive case, negative cases and edge cases on deterministic candles."""
import unittest

from tests.helpers import SETTINGS, day, frame, session, trend_days

from sensexpro.strategies.ensemble import build_strategies

S = {s.id: s for s in build_strategies(SETTINGS.strategy)}
WARM = trend_days(2)
L = WARM[-1].close
D = day(2)
PSL = min(b.low for b in WARM[-75:])          # previous session low


def run(sid, closes, vols=None, rng=15.0, start=L, warm=WARM):
    f = frame(warm + session(D, closes, start_open=start, rng=rng, vols=vols))
    return S[sid].evaluate(f, f.last), f


class TestORB(unittest.TestCase):
    CLOSES = [L + 10, L + 30, L + 20, L + 40, L + 130]

    def test_breakout_with_volume_fires_ce(self):
        out, f = run("orb", self.CLOSES, [1000] * 4 + [3000])
        self.assertEqual([c.direction for c in out], ["CE"])
        c = out[0]
        self.assertEqual(c.evidence["or_high"], max(b.high for b in f.bars[-5:-2]))
        self.assertEqual(c.invalidation, f.bars[-1].low)              # breakout candle low
        self.assertTrue(c.setup_key.startswith("orb:"))

    def test_low_volume_break_rejected(self):
        out, _ = run("orb", self.CLOSES, [1000] * 5)
        self.assertEqual(out, [])

    def test_no_signal_before_range_complete(self):
        out, _ = run("orb", self.CLOSES[:3], [1000, 1000, 5000])
        self.assertEqual(out, [])

    def test_chasing_huge_candle_rejected(self):
        out, _ = run("orb", self.CLOSES[:4] + [L + 400], [1000] * 4 + [3000])
        self.assertEqual(out, [])

    def test_unknown_volume_never_counts_as_expansion(self):
        out, _ = run("orb", self.CLOSES, [None] * 5)
        self.assertEqual(out, [])

    def test_repeated_whipsaw_rejected(self):
        closes = [L + 10, L + 30, L + 20, L + 80, L + 20, L + 80, L + 20, L + 130]
        vols = [1000, 1000, 1000, 3000, 1000, 3000, 1000, 3000]
        out, _ = run("orb", closes, vols)
        self.assertEqual(out, [])                                    # third break of the same side


class TestVwapContinuation(unittest.TestCase):
    UP = [L + 25 * k for k in range(1, 13)]

    def test_pullback_and_confirmation_fires(self):
        out, f = run("vwap_continuation", self.UP + [self.UP[-1] - 40, self.UP[-1] - 70, self.UP[-1] + 10])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].direction, "CE")
        self.assertLess(out[0].invalidation, f.bars[-1].close)

    def test_touch_without_confirmation_candle_does_not_fire(self):
        out, _ = run("vwap_continuation", self.UP + [self.UP[-1] - 40, self.UP[-1] - 70, self.UP[-1] - 80])
        self.assertEqual(out, [])

    def test_no_pullback_no_signal(self):
        out, _ = run("vwap_continuation", self.UP + [self.UP[-1] + 25, self.UP[-1] + 50, self.UP[-1] + 75], rng=5)
        self.assertEqual(out, [])


class TestEmaPullback(unittest.TestCase):
    UP = [L + 20 * k for k in range(1, 16)]

    def test_pullback_into_ema_zone_with_trigger_fires(self):
        out, _ = run("ema_pullback", self.UP + [self.UP[-1] - 50, self.UP[-1] - 90, self.UP[-1] - 30])
        self.assertEqual([c.direction for c in out], ["CE"])

    def test_no_trigger_above_prior_high(self):
        out, _ = run("ema_pullback", self.UP + [self.UP[-1] - 50, self.UP[-1] - 90, self.UP[-1] - 40])
        self.assertEqual(out, [])

    def test_extended_entry_rejected(self):
        out, _ = run("ema_pullback", self.UP + [self.UP[-1] - 50, self.UP[-1] - 90, self.UP[-1] + 200])
        self.assertEqual(out, [])

    def test_flat_market_rejected(self):
        flat_warm = trend_days(2, per_bar=0.0)
        out, _ = run("ema_pullback", [L + (10 if k % 2 else -10) for k in range(18)], warm=flat_warm)
        self.assertEqual(out, [])


class TestFailedBreakout(unittest.TestCase):
    def test_sweep_of_previous_low_and_reclaim_fires_ce(self):
        out, f = run("failed_breakout", [PSL + 80, PSL + 60, PSL + 40, PSL + 30, PSL - 60, PSL + 50],
                     [1000] * 5 + [2000], start=PSL + 90)
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.direction, "CE")
        self.assertEqual(c.evidence["level"], "prev_session")
        self.assertEqual(c.invalidation, min(b.low for b in f.bars[-2:]))   # the sweep extreme

    def test_first_wick_without_reclaim_is_not_a_signal(self):
        out, _ = run("failed_breakout", [PSL + 80, PSL + 60, PSL + 40, PSL + 30, PSL - 60],
                     [1000] * 5, start=PSL + 90)
        self.assertEqual(out, [])

    def test_tiny_poke_is_not_a_sweep(self):
        out, _ = run("failed_breakout", [PSL + 80, PSL + 60, PSL + 40, PSL + 30, PSL + 20, PSL + 50],
                     [1000] * 6, start=PSL + 90, rng=12)
        self.assertEqual(out, [])


class TestBollingerSqueeze(unittest.TestCase):
    FLAT = [L + (5 if k % 2 else -5) for k in range(40)]

    def test_squeeze_release_with_volume_fires(self):
        out, _ = run("bollinger_squeeze", self.FLAT + [L + 55], [1000] * 40 + [3000], rng=8)
        self.assertEqual([c.direction for c in out], ["CE"])
        self.assertTrue(out[0].strong)

    def test_release_without_volume_rejected(self):
        out, _ = run("bollinger_squeeze", self.FLAT + [L + 55], [1000] * 41, rng=8)
        self.assertEqual(out, [])

    def test_overextended_breakout_rejected(self):
        out, _ = run("bollinger_squeeze", self.FLAT + [L + 160], [1000] * 40 + [3000], rng=8)
        self.assertEqual(out, [])

    def test_no_squeeze_no_signal(self):
        wide = [L + (60 if k % 2 else -60) for k in range(40)]
        out, _ = run("bollinger_squeeze", wide + [L + 200], [1000] * 40 + [3000], rng=8)
        self.assertEqual(out, [])


class TestNoOrdersFromStrategies(unittest.TestCase):
    def test_strategies_have_no_broker_access(self):
        for s in S.values():
            self.assertFalse(any(hasattr(s, a) for a in ("broker", "client", "execute", "place_order")))


if __name__ == "__main__":
    unittest.main()
