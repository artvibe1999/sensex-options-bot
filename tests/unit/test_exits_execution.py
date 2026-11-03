import dataclasses
import datetime as dt
import random
import unittest

from tests.helpers import Bar, SETTINGS, contract, day, frame, quote, session, t, tmp_store, trend_days

from costs import CostParams, trade_cost
from sensexpro.emergency import EmergencyProtection
from sensexpro.execution import (ACKNOWLEDGED, CANCELLED, FILLED, PARTIALLY_FILLED, REJECTED, SUBMITTED,
                                 ExecutionEngine, InvalidTransition, PaperBroker, check_transition, round_tick)
from sensexpro.exits import EXIT, HOLD, PREPARE, REDUCE, AdaptiveExitEngine, OptionTracker, update_structure
from sensexpro.models import Position
from sensexpro.risk import RiskBook

XP = SETTINGS.strategy.exits
LIM = SETTINGS.risk.scaled(200_000)
D3 = day(3)


def position(direction="CE", invalidation=None, qty=20, entry_time=None, entry_price=200.0, entry_spot=None, f=None):
    c = contract(80500, direction)
    last = f.bars[f.last] if f else None
    return Position("p1", "SENSEX", c, qty // 20, qty, entry_price, entry_time or t(D3, 9, 50), "orb", direction, "k",
                    85.0, invalidation if invalidation is not None else (last.low - 200 if last else 0),
                    entry_spot or (last.close if last else 0), 0.5, 40.0)


def up_frame(extra=None):
    bars = trend_days(3, per_bar=10, wiggle=5)
    closes = [bars[-1].close + 10 * k for k in range(1, 9)] + (extra or [])
    return frame(bars + session(D3, closes, start_open=bars[-1].close, rng=15))


class TestAdaptiveExit(unittest.TestCase):
    def setUp(self):
        self.eng = AdaptiveExitEngine(XP, SETTINGS.strategy.regime)

    def test_healthy_trend_holds_and_missing_inputs_never_zero(self):
        f = up_frame()
        pos = position(f=f, entry_time=t(D3, 9, 50))
        opt = OptionTracker()
        opt.update(quote(pos.contract, 210, 211, avg=205), t(D3, 9, 55))
        a = self.eng.assess(pos, f, f.last, opt, f.bars[-1].close, t(D3, 9, 56), True)
        self.assertEqual(a.action, HOLD, a)
        self.assertIn("iv_theta", a.missing)
        self.assertEqual(a.components["iv_theta"], XP.weights.iv_theta * XP.missing_input_share)
        none = self.eng.assess(pos, None, None, OptionTracker(), None, t(D3, 9, 56), False)
        self.assertGreaterEqual(none.total, 100 * XP.missing_input_share - 0.01)   # all missing: half weight each

    def test_structural_break_exits_immediately(self):
        bars = up_frame().bars
        last = bars[-1]
        drop = dataclasses.replace(last, start=last.start + dt.timedelta(minutes=5), open=last.close,
                                   high=last.close + 5, low=last.close - 30, close=last.close - 25)
        f2 = frame(bars + [drop])
        pos = position(f=f2, invalidation=last.close - 20, entry_time=t(D3, 9, 50))
        a = self.eng.assess(pos, f2, f2.last, OptionTracker(), drop.close, t(D3, 10, 0), True)
        self.assertTrue(a.structural)
        self.assertEqual(a.action, EXIT)
        self.assertLess(a.total, 70)                               # exits although the total is below 70

    def test_prepare_twice_then_exit(self):
        f = up_frame()
        pos = position(f=f)
        eng = AdaptiveExitEngine(dataclasses.replace(XP) if False else XP, SETTINGS.strategy.regime)
        a1 = eng.assess(pos, None, None, OptionTracker(), None, t(D3, 10, 30), True)    # 50 = all missing
        self.assertEqual(a1.action, PREPARE)
        a2 = eng.assess(pos, None, None, OptionTracker(), None, t(D3, 10, 35), True)
        self.assertEqual(a2.action, EXIT)

    def test_reduce_one_lot_after_milestone(self):
        pos = position(qty=60)
        pos.milestone_hit = True
        a = self.eng.assess(pos, None, None, OptionTracker(), None, t(D3, 10, 30), True)
        self.assertEqual(a.action, REDUCE)
        pos2 = position(qty=20)
        pos2.milestone_hit = True
        self.assertEqual(self.eng.assess(pos2, None, None, OptionTracker(), None, t(D3, 10, 30), True).action, EXIT)

    def test_structure_trails_to_confirmed_higher_low(self):
        base = trend_days(3, per_bar=10, wiggle=5)
        L = base[-1].close
        lows = [L + 40, L + 30, L + 20, L + 60, L + 100, L + 140]             # bar 2 = confirmed swing low
        mk = [Bar(t(D3, 9, 15) + dt.timedelta(minutes=5 * k), 5, lo + 10, lo + 30, lo, lo + 20, 1000.0)
              for k, lo in enumerate(lows)]
        f = frame(base + mk)
        pos = position(f=f, invalidation=L - 100, entry_time=t(D3, 9, 15))
        lvl = update_structure(pos, f, f.last, 2)
        self.assertEqual(lvl, L + 20)


class TestEmergency(unittest.TestCase):
    def test_overrides_and_latches(self):
        em = EmergencyProtection(LIM, SETTINGS.session)
        pos = position()
        b = RiskBook()
        b.ensure_base(200_000)
        now = t(D3, 11, 0)
        q = quote(pos.contract, 100, 101)
        tr = em.check(pos, q, -3000.0, b, now, 100.0)
        self.assertEqual((tr.kind, tr.latch), ("trade_loss", True))
        self.assertIsNone(em.check(pos, q, -100.0, b, now, 100.0))
        b.day_realized = -5000
        self.assertEqual(em.check(pos, q, -1000.0, b, now, 100.0).kind, "daily_loss")
        b.day_realized = 0
        self.assertEqual(em.check(pos, q, 0.0, b, now, 100.0, broker_ok=False).kind, "broker_mismatch")
        self.assertIsNone(em.check(pos, None, None, b, now, 100.0, quote_problem="stale"))
        self.assertEqual(em.check(pos, None, None, b, now, 131.0, quote_problem="stale").kind, "stale_quote")
        wide = quote(pos.contract, 100, 120)
        em.reset()
        self.assertIsNone(em.check(pos, wide, 0.0, b, now, 200.0))
        self.assertEqual(em.check(pos, wide, 0.0, b, now, 216.0).kind, "wide_spread")
        sq = em.check(pos, q, 0.0, b, t(D3, 15, 15), 300.0)
        self.assertEqual((sq.kind, sq.latch), ("session_close", False))


class TestExecution(unittest.TestCase):
    def test_state_machine(self):
        check_transition(SUBMITTED, ACKNOWLEDGED)
        check_transition(ACKNOWLEDGED, PARTIALLY_FILLED)
        check_transition(PARTIALLY_FILLED, FILLED)
        for old, new in ((FILLED, CANCELLED), (REJECTED, FILLED), ("CREATED", FILLED)):
            with self.assertRaises(InvalidTransition):
                check_transition(old, new)

    def test_tick_rounding_against_us(self):
        self.assertEqual(round_tick(100.01, 0.05, True), 100.05)
        self.assertEqual(round_tick(100.04, 0.05, False), 100.0)

    def _engine(self, sim, q):
        store, _ = tmp_store()
        c = contract(80500)
        broker = PaperBroker(sim, lambda tok: q, 5.0, rng=random.Random(1), now_mono=lambda: 100.0)
        return ExecutionEngine(store, broker, "MIS", "SP"), store, c

    def test_paper_fill_at_ask_and_bid_with_slippage_and_journal(self):
        c = contract(80500)
        q = quote(c, 200.0, 201.0, mono=100.0)
        sim = dataclasses.replace(SETTINGS.execution.paper, reject_prob=0, no_fill_prob=0, partial_fill_prob=0)
        exe, store, _ = self._engine(sim, q)
        ex = exe.execute(kind="entry", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                         symbol=c.symbol, side="BUY", qty=40, lot_size=20, tick_size=0.05, reason="t",
                         today=dt.date(2026, 11, 3))
        self.assertEqual((ex.state, ex.filled), (FILLED, 40))
        self.assertEqual(ex.avg_price, round_tick(201.0 * 1.0025, 0.05, True))
        o = store.order(ex.client_id)
        self.assertEqual((o["state"], o["filled_qty"], o["requested_qty"]), (FILLED, 40, 40))
        self.assertTrue(ex.client_id.startswith("SP261103E"))
        sx = exe.execute(kind="exit", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                         symbol=c.symbol, side="SELL", qty=40, lot_size=20, tick_size=0.05, reason="t",
                         today=dt.date(2026, 11, 3))
        self.assertEqual(sx.avg_price, round_tick(200.0 * 0.9975, 0.05, False))
        self.assertNotEqual(sx.client_id, ex.client_id)

    def test_whole_lot_partial_fill_reject_and_no_fill(self):
        c = contract(80500)
        q = quote(c, 200.0, 201.0, mono=100.0)
        part = dataclasses.replace(SETTINGS.execution.paper, reject_prob=0, no_fill_prob=0, partial_fill_prob=0.999)
        exe, _s, _ = self._engine(part, q)
        ex = exe.execute(kind="entry", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                         symbol=c.symbol, side="BUY", qty=60, lot_size=20, tick_size=0.05, reason="t")
        self.assertEqual(ex.state, CANCELLED)
        self.assertEqual(ex.filled % 20, 0)
        self.assertTrue(0 < ex.filled < 60)
        rej = dataclasses.replace(SETTINGS.execution.paper, reject_prob=0.999)
        exe2, _s, _ = self._engine(rej, q)
        self.assertEqual(exe2.execute(kind="entry", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                                      symbol=c.symbol, side="BUY", qty=20, lot_size=20, tick_size=0.05,
                                      reason="t").state, REJECTED)
        with self.assertRaises(ValueError):
            exe2.execute(kind="entry", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                         symbol=c.symbol, side="BUY", qty=25, lot_size=20, tick_size=0.05, reason="t")

    def test_stale_quote_rejects_paper_order(self):
        c = contract(80500)
        q = quote(c, 200.0, 201.0, mono=80.0)                      # 20 s old
        exe, _s, _ = self._engine(SETTINGS.execution.paper, q)
        ex = exe.execute(kind="entry", position_id="p", index="SENSEX", token=c.token, exchange="BFO",
                         symbol=c.symbol, side="BUY", qty=20, lot_size=20, tick_size=0.05, reason="t")
        self.assertEqual((ex.state, ex.filled), (REJECTED, 0))

    def test_pnl_includes_charges(self):
        pos = position(qty=20, entry_price=200.0)
        exit_cost = trade_cost([("SELL", 210.0, 20)], CostParams())
        self.assertAlmostEqual(pos.unrealized(210.0, exit_cost), 10 * 20 - exit_cost - 40.0)


if __name__ == "__main__":
    unittest.main()
