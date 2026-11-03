"""Market data -> candles -> indicators -> strategy -> ensemble -> selector -> risk -> paper order -> adaptive exit."""
import datetime as dt
import unittest

from tests.enginekit import TODAY, Kit
from tests.helpers import SETTINGS, cd, day, one_minute, t

from sensexpro.features import Frame
from sensexpro.strategies.ensemble import Ensemble


class TestCandlesToEnsemble(unittest.TestCase):
    def test_one_minute_feed_to_signal(self):
        """Real 1-minute candles aggregated to 5 / 15 minutes feed the real strategies (ORB fires)."""
        warm = []
        px = 50000.0
        for n in range(3):
            closes = []
            for k in range(375):
                px += 0.8
                closes.append(px + (15 if k % 2 else -15))
            warm += one_minute(day(n), closes, vol=100.0)
        d = day(3)
        L = warm[-1].close
        today = [L + (k % 5) for k in range(15)]                    # opening range ~L..L+4 plus wicks
        today += [L + 3, L + 2, L + 4, L + 3, L + 2]               # 09:30-09:35 inside
        today += [L + 10, L + 20, L + 30, L + 40, L + 50]          # 09:35-09:40 breakout
        bars1 = warm + one_minute(d, today, vol=100.0)
        bars1 = bars1[:-5] + [cd.Bar(b.start, 1, b.open, b.high, b.low, b.close, 400.0) for b in bars1[-5:]]
        b5, b15 = cd.aggregate(bars1, 5), cd.aggregate(bars1, 15)
        f = Frame.build("SENSEX", b5, b15, SETTINGS.strategy.indicators)
        ens = Ensemble(SETTINGS.strategy)
        cands = ens.candidates(f, f.last)
        self.assertIn(("orb", "CE"), [(c.strategy, c.direction) for c in cands])


class TestEngineRoundTrip(unittest.TestCase):
    def test_signal_to_paper_entry_to_adaptive_exit(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        pos = k.engine.position
        self.assertIsNotNone(pos, k.notifier.alerts)
        self.assertEqual((pos.qty, pos.status, pos.direction), (20, "OPEN", "CE"))
        self.assertEqual(pos.contract.kind, "CE")
        self.assertAlmostEqual(pos.entry_price, 201.55, places=2)  # ask 201 + 0.25% slippage, rounded up to tick
        self.assertEqual(k.engine.book.entries_today, 1)
        self.assertEqual(k.notifier.orders[0][3], "ENTRY")
        self.assertEqual(k.notifier.journal[0][2], "entry")       # report page journal row
        # the thesis breaks: a completed close below the invalidation level
        k.bar([pos.invalidation - 40])
        k.set_px(150.0, 151.0)
        k.step()
        self.assertIsNone(k.engine.position)
        rows = k.store.closed_positions("PAPER", "2000")
        self.assertEqual(len(rows), 1)
        self.assertIn("STRUCTURE", rows[0]["exit_reason"])
        self.assertLess(rows[0]["net"], 0)
        self.assertEqual(k.notifier.trades[0][2], "sp_orb_CE")
        self.assertAlmostEqual(k.engine.book.day_realized, rows[0]["net"], places=2)

    def test_square_off_time_closes_without_latching(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        self.assertIsNotNone(k.engine.position)
        k.engine.step(dt.datetime.combine(TODAY, dt.time(15, 15, 1), tzinfo=t(TODAY, 9, 15).tzinfo))
        self.assertIsNone(k.engine.position)
        self.assertFalse(k.engine.book.kill_latched)

    def test_low_score_signal_not_traded(self):
        k = Kit()
        k.bar(push_candidate=k.candidate("PE", key="weak", strength=0.0))
        k.step()
        self.assertIsNone(k.engine.position)
        self.assertIn("weak", k.store.consumed(TODAY))

    def test_wide_spread_contract_rejected(self):
        k = Kit()
        k.set_px(190.0, 210.0)                                     # 10% spread everywhere
        k.bar(push_candidate=k.candidate())
        k.step()
        self.assertIsNone(k.engine.position)
        self.assertIn("setup-1", k.store.consumed(TODAY))

    def test_reduce_whole_lots_after_profit_milestone(self):
        k = Kit(lots=3)
        k.set_px(100.0, 100.5)
        k.bar(push_candidate=k.candidate())
        k.step()
        pos = k.engine.position
        self.assertEqual(pos.qty, 60)
        k.set_px(140.0, 140.5)                                     # +40% on premium: milestone
        k.step()
        self.assertTrue(pos.milestone_hit)
        k.set_px(139.0, 142.0, avg=150.0)                          # below its day VWAP, spread widened
        k.market.frame = None                                      # bar inputs missing -> score in the 50-69 band
        k.market.new_bar = True
        k.step()
        self.assertEqual(pos.qty, 40)                              # one whole lot sold
        self.assertEqual(k.notifier.orders[-1][3], "EXIT-PARTIAL")


if __name__ == "__main__":
    unittest.main()
