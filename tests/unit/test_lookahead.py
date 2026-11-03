"""No look-ahead: strategy candidates, regime and score at bar i are identical whether or not later bars exist."""
import random
import unittest

from tests.helpers import SETTINGS, day, frame, session

from sensexpro.regime import detect
from sensexpro.strategies.ensemble import Ensemble


def random_walk(n_days: int, seed: int = 3):
    rnd = random.Random(seed)
    px, out = 50000.0, []
    for d in range(n_days):
        closes = []
        for _k in range(75):
            px += rnd.gauss(0, 35)
            closes.append(px)
        vols = [rnd.uniform(500, 3000) for _ in range(75)]
        out += session(day(d), closes, rng=20, vols=vols)
    return out


class TestNoLookAhead(unittest.TestCase):
    def test_outputs_at_i_do_not_depend_on_future_bars(self):
        bars = random_walk(6)
        full = frame(bars)
        ens = Ensemble(SETTINGS.strategy)
        checked = fired = 0
        for i in range(5 * 75 + 3, 6 * 75 - 1, 2):
            cut = frame(bars[:i + 1])
            a = sorted((c.strategy, c.direction, c.setup_key, round(c.invalidation, 6)) for c in ens.candidates(full, i))
            b = sorted((c.strategy, c.direction, c.setup_key, round(c.invalidation, 6))
                       for c in ens.candidates(cut, cut.last))
            self.assertEqual(a, b, f"bar {i}")
            self.assertEqual(detect(full, i, SETTINGS.strategy.regime), detect(cut, cut.last, SETTINGS.strategy.regime))
            checked += 1
            fired += bool(a)
        self.assertGreater(checked, 30)


if __name__ == "__main__":
    unittest.main()
