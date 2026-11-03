import datetime as dt
import os
import tempfile
import unittest

from tests.helpers import IST, SETTINGS, contract, quote

from costs import CostParams
from sensexpro.options import choose_expiry, select_contract
from sensexpro.risk import EntryContext, RiskBook, approve_entry, breaker_breach
from sensexpro.settings import ConfigError, load_settings

LIM = SETTINGS.risk.scaled(200_000)            # x5: cap 10k, emergency 3k, daily 6k, weekly 15k, dd 20k
CP = SETTINGS.strategy.contracts
TODAY = dt.date(2026, 11, 3)
NOW = dt.datetime(2026, 11, 3, 10, 30, tzinfo=IST)
EXP = dt.date(2026, 11, 5)


def chain(prices=None, expiry=EXP, lot=20):
    """SENSEX-like CE/PE strikes 80000..81000; prices: strike -> (bid, ask)."""
    cs, qs = [], {}
    for k in range(80000, 81100, 100):
        for kind in ("CE", "PE"):
            c = contract(k, kind, expiry, lot)
            cs.append(c)
            bid, ask = (prices or {}).get((k, kind), (200.0, 201.0))
            qs[c.token] = quote(c, bid, ask, mono=100.0)
    return cs, qs


def sel(direction="CE", spot=80510.0, wanted=2, prices=None, limits=LIM, mono=100.5, contracts=None, quotes=None,
        today=TODAY):
    cs, qs = chain(prices)
    return select_contract(direction, spot, 100.0, contracts or cs, quotes or qs, wanted, 10, limits, CostParams(),
                           CP, today, mono, NOW, 5.0)


class TestContractSelection(unittest.TestCase):
    def test_atm_whole_lots_within_cap(self):
        r = sel()
        s = r.selection
        self.assertIsNotNone(s)
        self.assertEqual(s.contract.strike, 80500)
        self.assertEqual(s.lots, 2)                                 # 2 x 20 x 201 = 8,040 + charges < 10,000
        self.assertEqual(s.qty, 40)
        self.assertLessEqual(s.outlay, LIM.max_premium_per_entry)
        self.assertEqual(s.ask, 201.0)                              # sized on the ask, never LTP
        self.assertIsNone(s.delta)                                  # no fabricated Greeks

    def test_lots_reduced_to_fit_then_skipped(self):
        r = sel(prices={(k, "CE"): (300.0, 301.0) for k in range(80000, 81100, 100)})
        self.assertEqual(r.selection.lots, 1)                       # 2 lots = 12,040 > cap; 1 lot fits
        r2 = sel(prices={(k, "CE"): (600.0, 601.0) for k in range(80000, 81100, 100)})
        self.assertIsNone(r2.selection)                             # one lot 12,020 > cap: no trade
        self.assertTrue(any("one lot costs" in why for _s, why in r2.rejections))

    def test_spec_cap_at_40k_blocks_typical_lot(self):
        r = sel(limits=SETTINGS.risk.scaled(40_000))
        self.assertIsNone(r.selection)

    def test_stale_crossed_zero_and_wide_quotes_rejected(self):
        cs, qs = chain()
        atm = [c for c in cs if c.strike == 80500 and c.kind == "CE"][0]
        qs[atm.token] = quote(atm, 200.0, 201.0, mono=90.0)          # 10.5 s old
        r = sel(contracts=cs, quotes=qs)
        self.assertNotEqual(r.selection.contract.strike, 80500)
        self.assertTrue(any("stale" in w for _s, w in r.rejections))
        for bad in ((202.0, 201.0), (0.0, 201.0), (150.0, 250.0)):
            qs[atm.token] = quote(atm, *bad, mono=100.0)
            r = sel(contracts=cs, quotes=qs)
            self.assertNotEqual(r.selection.contract.strike, 80500, bad)

    def test_never_beyond_one_strike_from_atm(self):
        cs, qs = chain()
        near = {80400, 80500, 80600}
        for c in cs:
            if c.strike in near:
                qs[c.token] = quote(c, 0.0, 0.0)
        r = sel(contracts=cs, quotes=qs)
        self.assertIsNone(r.selection)                              # cheaper far strikes are never chosen

    def test_pe_moneyness(self):
        s = sel("PE", spot=80510.0).selection
        self.assertEqual((s.contract.kind, s.contract.strike), ("PE", 80500))

    def test_expiry_day_uses_next_expiry(self):
        cs = [contract(80500, "CE", dt.date(2026, 11, 3)), contract(80500, "CE", dt.date(2026, 11, 10), token=7)]
        self.assertEqual(choose_expiry(cs, dt.date(2026, 11, 3), True), dt.date(2026, 11, 10))
        self.assertEqual(choose_expiry(cs, dt.date(2026, 11, 3), False), dt.date(2026, 11, 3))
        self.assertIsNone(choose_expiry(cs, dt.date(2026, 11, 11), True))

    def test_depth_must_cover_a_lot(self):
        cs, qs = chain()
        for c in cs:
            qs[c.token] = quote(c, 200.0, 201.0, ask_qty=10)        # less than one lot (20) at the best ask
        self.assertIsNone(sel(contracts=cs, quotes=qs).selection)


def ctx(**kw):
    base = dict(now=NOW, has_position=False, pending_orders=0, spot_fresh=True, quote_problem=None,
                instruments_day=TODAY, lots=1, lot_size=20, qty=20, outlay=4000.0, spread_ok=True, feasible=True,
                reconciled=True, feed_ok=True)
    base.update(kw)
    return EntryContext(**base)


class TestRisk(unittest.TestCase):
    def book(self):
        b = RiskBook()
        b.ensure_base(200_000)
        b.roll(TODAY)
        return b

    def test_scaling(self):
        self.assertEqual((LIM.max_premium_per_entry, LIM.emergency_loss_per_trade, LIM.daily_loss_limit,
                          LIM.weekly_loss_limit, LIM.max_drawdown), (10000, 3000, 6000, 15000, 20000))
        self.assertEqual(LIM.scaled(200_000).max_premium_per_entry, 10000)        # idempotent
        self.assertEqual(SETTINGS.risk.scaled(40_000).max_premium_per_entry, 2000)

    def test_clean_entry_approved(self):
        ok, why = approve_entry(ctx(), self.book(), LIM, SETTINGS.session)
        self.assertTrue(ok, why)

    def test_each_check_blocks(self):
        b = self.book()
        cases = {
            "window": ctx(now=NOW.replace(hour=9, minute=20)),
            "late": ctx(now=NOW.replace(hour=14, minute=45)),
            "position": ctx(has_position=True),
            "pending": ctx(pending_orders=1),
            "spot": ctx(spot_fresh=False),
            "quote": ctx(quote_problem="stale (9s)"),
            "instruments": ctx(instruments_day=TODAY - dt.timedelta(days=1)),
            "fraction": ctx(lots=1, qty=30),
            "outlay": ctx(outlay=10_001.0),
            "spread": ctx(spread_ok=False),
            "feasible": ctx(feasible=False),
            "reconciled": ctx(reconciled=False),
            "feed": ctx(feed_ok=False),
            "weekend": ctx(now=dt.datetime(2026, 11, 7, 10, 30, tzinfo=IST)),
        }
        for name, c in cases.items():
            ok, why = approve_entry(c, b, LIM, SETTINGS.session)
            self.assertFalse(ok, name)
            self.assertTrue(why, name)

    def test_entry_count_cooldown_kill_and_loss_streak(self):
        b = self.book()
        b.entries_today = 3
        self.assertFalse(approve_entry(ctx(), b, LIM, SETTINGS.session)[0])
        b = self.book()
        b.last_exit_at = (NOW - dt.timedelta(minutes=5)).isoformat()
        self.assertFalse(approve_entry(ctx(), b, LIM, SETTINGS.session)[0])
        b.last_exit_at = (NOW - dt.timedelta(minutes=11)).isoformat()
        self.assertTrue(approve_entry(ctx(), b, LIM, SETTINGS.session)[0])
        b.latch("test", NOW)
        self.assertFalse(approve_entry(ctx(), b, LIM, SETTINGS.session)[0])
        b = self.book()
        for _ in range(2):
            b.add_realized(-100)
            b.on_close(-100, NOW - dt.timedelta(hours=1), LIM)
        self.assertIn("consecutive losses", b.day_block)
        self.assertFalse(approve_entry(ctx(), b, LIM, SETTINGS.session)[0])

    def test_breakers(self):
        b = self.book()
        self.assertIsNone(breaker_breach(b, LIM))
        self.assertEqual(breaker_breach(b, LIM, unrealized=-6000).kind, "daily_loss")
        b.week_realized = -12_000
        self.assertEqual(breaker_breach(b, LIM, unrealized=-3000).kind, "weekly_loss")
        b = self.book()
        b.cum_realized, b.peak_equity = -19_000, 201_000            # equity 181k, 20k below peak
        b.day_realized = b.week_realized = 0
        self.assertEqual(breaker_breach(b, LIM).kind, "drawdown")

    def test_roll_resets_day_not_kill_or_week(self):
        b = self.book()
        b.day_realized, b.entries_today, b.week_realized = -500, 2, -500
        b.latch("x", NOW)
        b.roll(TODAY + dt.timedelta(days=1))                         # same ISO week
        self.assertEqual((b.day_realized, b.entries_today), (0.0, 0))
        self.assertEqual(b.week_realized, -500)
        self.assertTrue(b.kill_latched)
        b.roll(TODAY + dt.timedelta(days=7))
        self.assertEqual(b.week_realized, 0.0)
        r = RiskBook.restore(b.export())
        self.assertTrue(r.kill_latched)

    def test_invalid_config_fails_closed(self):
        src = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")
        d = tempfile.mkdtemp()
        for n in ("app.yaml", "risk.yaml", "execution.yaml", "strategy.yaml"):
            with open(os.path.join(src, n), encoding="utf-8") as f, open(os.path.join(d, n), "w", encoding="utf-8") as g:
                g.write(f.read())
        load_settings(d, env={})
        with open(os.path.join(d, "risk.yaml"), "a", encoding="utf-8") as g:
            g.write("\nmax_open_positions: 2\n")
        with self.assertRaises(ConfigError):
            load_settings(d, env={})
        with self.assertRaises(ConfigError):
            load_settings(d + "-missing", env={})


if __name__ == "__main__":
    unittest.main()
