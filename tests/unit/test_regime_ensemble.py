import dataclasses
import unittest

from tests.helpers import SETTINGS, day, frame, session, trend_days

from sensexpro.models import Candidate
from sensexpro.regime import DATA_UNSAFE, HIGH_VOL, LOW_LIQ, RANGE, TREND_UP, RegimeState, detect
from sensexpro.strategies import scorer
from sensexpro.strategies.ensemble import ELIGIBLE, REJECT, WATCHLIST, Ensemble, permitted

RP = SETTINGS.strategy.regime


class TestRegime(unittest.TestCase):
    def test_warmup_is_data_unsafe(self):
        f = frame(session(day(0), [50000 + k for k in range(40)]))
        self.assertEqual(detect(f, f.last, RP).regime, DATA_UNSAFE)

    def test_feed_problem_is_data_unsafe(self):
        f = frame(trend_days(3))
        st = detect(f, f.last, RP, data_ok=False, why_unsafe="stale")
        self.assertEqual((st.regime, st.evidence["why"]), (DATA_UNSAFE, "stale"))

    def test_gap_is_data_unsafe(self):
        bars = trend_days(3)
        bars[-2] = dataclasses.replace(bars[-2], gap=True)
        f = frame(bars)
        self.assertEqual(detect(f, f.last, RP).regime, DATA_UNSAFE)

    def test_illiquid_options(self):
        f = frame(trend_days(3))
        self.assertEqual(detect(f, f.last, RP, liquidity_ok=False).regime, LOW_LIQ)

    def test_trend_up(self):
        f = frame(trend_days(3, per_bar=10, wiggle=5))
        self.assertEqual(detect(f, f.last, RP).regime, TREND_UP)

    def test_choppy_is_range(self):
        bars = trend_days(2, per_bar=0.0)
        bars += session(day(2), [50000 + (60 if k % 2 else -60) for k in range(40)])
        f = frame(bars)
        self.assertEqual(detect(f, f.last, RP).regime, RANGE)

    def test_volatility_spike(self):
        bars = trend_days(3, per_bar=0.0, wiggle=10, rng=10)
        bars += session(day(3), [50000 + (400 if k % 2 else -400) for k in range(30)], rng=50)
        f = frame(bars)
        self.assertEqual(detect(f, f.last, RP).regime, HIGH_VOL)


def cand(direction="CE", strategy="orb", strong=False, pa=1.0, key="k1", ref=None, bars=None):
    f = frame(bars or trend_days(3, per_bar=10, wiggle=5))
    i = f.last
    c = Candidate("SENSEX", strategy, direction, f.bars[i].end, key, pa, ref or f.bars[i].close, f.bars[i].low,
                  ("test",), {}, strong)
    return c, f, i


class FixedStrategy:
    def __init__(self, sid, cands):
        self.id, self.cands = sid, cands

    def evaluate(self, f, i):
        return list(self.cands)


class TestScoringEnsemble(unittest.TestCase):
    def test_score_components_and_bounds(self):
        c, f, i = cand()
        total, comp, _ = scorer.score(c, f, i, RP, liquidity=10)
        self.assertEqual(set(comp), {"trend15", "vwap", "price_action", "volume", "momentum", "option_liquidity"})
        self.assertEqual(comp["trend15"], 20.0)
        self.assertLessEqual(total, 100)
        self.assertEqual(total, round(sum(comp.values()), 1))

    def test_missing_volume_scores_zero(self):
        bars = [dataclasses.replace(b, volume=None) for b in trend_days(3, per_bar=10, wiggle=5)]
        c, f, i = cand(bars=bars)
        _t, comp, why = scorer.score(c, f, i, RP)
        self.assertEqual(comp["volume"], 0.0)
        self.assertIn("volume unavailable", why)

    def test_against_trend_scores_low(self):
        c, f, i = cand("PE")
        total, comp, _ = scorer.score(c, f, i, RP)
        self.assertEqual(comp["trend15"], 0.0)
        self.assertLess(total, 60)

    def test_liquidity_points(self):
        self.assertEqual(scorer.option_liquidity_points(0.4), 10.0)
        self.assertEqual(scorer.option_liquidity_points(1.2), 4.0)
        self.assertEqual(scorer.option_liquidity_points(3.0), 0.0)
        self.assertEqual(scorer.option_liquidity_points(None), 0.0)

    def test_permission_table(self):
        c, _f, _i = cand("PE", "orb")
        self.assertFalse(permitted(c, TREND_UP)[0])
        self.assertTrue(permitted(dataclasses.replace(c, direction="CE"), TREND_UP)[0])
        fb = dataclasses.replace(c, strategy="failed_breakout", direction="CE")
        self.assertFalse(permitted(fb, TREND_UP)[0])                      # needs strong evidence
        self.assertTrue(permitted(dataclasses.replace(fb, strong=True), TREND_UP)[0])
        self.assertFalse(permitted(dataclasses.replace(c, strategy="ema_pullback"), RANGE)[0])
        self.assertFalse(permitted(dataclasses.replace(c, strategy="bollinger_squeeze"), RANGE)[0])
        self.assertTrue(permitted(dataclasses.replace(c, strategy="bollinger_squeeze", strong=True), RANGE)[0])
        for s in ("orb", "vwap_continuation", "ema_pullback", "failed_breakout", "bollinger_squeeze"):
            self.assertFalse(permitted(dataclasses.replace(c, strategy=s), LOW_LIQ)[0])
            self.assertFalse(permitted(dataclasses.replace(c, strategy=s), DATA_UNSAFE)[0])

    def test_conflicting_directions_rejected(self):
        a, f, i = cand("CE", "orb", key="a")
        b = dataclasses.replace(a, strategy="failed_breakout", direction="PE", setup_key="b")
        ens = Ensemble(SETTINGS.strategy, [FixedStrategy("orb", [a]), FixedStrategy("failed_breakout", [b])])
        res = ens.evaluate(f, i, RegimeState(TREND_UP, {}))
        self.assertIsNone(res.best)
        self.assertEqual(len(res.rejected), 2)

    def test_consumed_setup_is_not_repeated_and_duplicates_deduped(self):
        a, f, i = cand("CE", "orb", key="same")
        ens = Ensemble(SETTINGS.strategy, [FixedStrategy("orb", [a, a])])
        res = ens.evaluate(f, i, RegimeState(TREND_UP, {}))
        self.assertEqual(len(res.signals), 1)
        res2 = ens.evaluate(f, i, RegimeState(TREND_UP, {}), consumed={"same"})
        self.assertEqual(res2.signals, [])

    def test_thresholds_and_final_decision(self):
        a, f, i = cand("CE", "orb")
        ens = Ensemble(SETTINGS.strategy, [FixedStrategy("orb", [a])])
        res = ens.evaluate(f, i, RegimeState(TREND_UP, {}))
        sig = res.signals[0]
        self.assertEqual(ens.final_decision(dataclasses.replace(sig, score=75.0), 5.0), (ELIGIBLE, 80.0))
        self.assertEqual(ens.final_decision(dataclasses.replace(sig, score=70.0), 5.0)[0], WATCHLIST)
        self.assertEqual(ens.final_decision(dataclasses.replace(sig, score=50.0), 0.0)[0], REJECT)
        hv = dataclasses.replace(sig, score=78.0, regime=HIGH_VOL)
        self.assertEqual(ens.final_decision(hv, 4.0)[0], WATCHLIST)        # selective: needs 85

    def test_strategy_exception_does_not_stop_others(self):
        class Boom:
            id = "orb"

            def evaluate(self, f, i):
                raise RuntimeError("boom")
        a, f, i = cand("CE", "vwap_continuation")
        ens = Ensemble(SETTINGS.strategy, [Boom(), FixedStrategy("vwap_continuation", [a])])
        self.assertEqual(len(ens.candidates(f, i)), 1)


if __name__ == "__main__":
    unittest.main()
