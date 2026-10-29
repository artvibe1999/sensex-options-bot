"""Intraday price-action family: levels, sideways filter, breakouts, traps, ranges, plans, spot exits, gating,
the intraday backtester and promotion's drawdown-vs-capital rule."""
import dataclasses
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt
import intraday as I
import intraday_backtest as ib
import strategy_library as lib
from config import IST, BacktestParams, StrategyParams
from costs import CostParams
from exit_engine import ExitEngine, ExitParams
from risk_manager import ManagedPosition, OpenLeg
from strategy_engine import Regime, RegimeState

DAY = dt.date(2026, 10, 27)
NORMAL = RegimeState(Regime.NORMAL, 15.0, 0.9, 13.0, False, 0)


def bars_from(ohlc, day=DAY, start=dt.time(9, 15)):
    t0 = dt.datetime.combine(day, start, tzinfo=IST)
    return [I.Bar(t0 + dt.timedelta(minutes=5 * i), *x) for i, x in enumerate(ohlc)]


PREV = [bars_from([(74000, 74400, 73300, 74000)], day=DAY - dt.timedelta(days=1))]
OR = [(74000, 74050, 73950, 74000)] * 6
BREAKDOWN = OR + [(74000, 74010, 73955, 73960),           # closes just above the OR low
                  (73958, 73962, 73790, 73800),           # strong red bar through 73950 / 73900 / 73850
                  (73800, 73805, 73770, 73780)]           # confirmation: still below, red
TRAP = OR + [(74000, 74010, 73990, 74000),
             (74000, 74120, 73990, 74030),                # pokes above the OR high (74050)
             (74030, 74060, 74000, 74010),
             (74010, 74015, 73960, 73970)]                # closes back below 74050 with a red bar


class LevelTests(unittest.TestCase):
    def test_cpr_and_opening_range(self):
        today = bars_from(OR)
        lv = I.levels_from(PREV, today, 6)
        self.assertAlmostEqual(lv.pivot, (74400 + 73300 + 74000) / 3)
        self.assertAlmostEqual(lv.bc, min((74400 + 73300) / 2, 2 * lv.pivot - (74400 + 73300) / 2))
        self.assertEqual((lv.orh, lv.orl, lv.adr), (74050, 73950, 1100))
        daily = I.levels_from_daily([{"high": 74400, "low": 73300, "close": 74000}], today, 6)
        self.assertAlmostEqual(daily.pivot, lv.pivot)
        self.assertIsNone(I.levels_from(PREV, today[:3], 6).orh)          # opening range not complete yet

    def test_swing_levels(self):
        b = bars_from([(1, 2, 0.5, 1), (1, 3, 0.6, 1), (1, 5, 0.7, 1), (1, 3, 0.4, 1), (1, 2, 0.8, 1)])
        hs, ls = I.swing_levels(b)
        self.assertEqual((hs, ls), ([5], []))


class SidewaysTests(unittest.TestCase):
    def test_dead_day_conditions(self):
        lv = I.levels_from(PREV, bars_from(OR), 6)
        trend = bars_from([(74000 - 20 * i, 74005 - 20 * i, 73975 - 20 * i, 73980 - 20 * i) for i in range(14)])
        chop = bars_from([(74000, 74030, 73970, 74000 + (10 if i % 2 else -10)) for i in range(14)])
        p = I.SignalParams(er_min=0.3)
        self.assertFalse(I.sideways(trend, lv, p)[0])
        self.assertTrue(I.sideways(chop, lv, p)[0])
        wide = dataclasses.replace(lv, cpr_pct=0.4)
        self.assertIn("CPR", I.sideways(trend, wide, I.SignalParams(er_min=0, cpr_wide_pct=0.25))[1])
        self.assertIn("narrow", I.sideways(trend[:3], lv, I.SignalParams(er_min=0, min_range_adr=0.5))[1])


class SignalTests(unittest.TestCase):
    P = I.SignalParams(er_min=0.0)

    def _detect(self, rows, kind, **kw):
        b = bars_from(rows)
        lv = I.levels_from(PREV, b, 6)
        return I.detect(b, lv, dataclasses.replace(self.P, **kw), kind)

    def test_confirmed_breakdown(self):
        s = self._detect(BREAKDOWN, I.BREAKOUT)
        self.assertEqual(s.direction, -1)
        self.assertEqual(s.entry, 73780)
        self.assertGreater(s.stop, s.level)                               # stop back above the broken level
        self.assertAlmostEqual(s.target, s.entry - 2.0 * s.risk)
        failed = BREAKDOWN[:-1] + [(73800, 73900, 73795, 73890)]         # follow-up bar rallied: likely a trap
        self.assertIsNone(self._detect(failed, I.BREAKOUT))

    def test_no_breakout_without_room_or_on_a_sideways_day(self):
        self.assertIsNone(self._detect(BREAKDOWN, I.BREAKOUT, room_r=10.0))
        self.assertIsNone(self._detect(BREAKDOWN, I.BREAKOUT, min_range_adr=5.0))       # day has not expanded
        self.assertIsNone(self._detect(BREAKDOWN, I.BREAKOUT, cpr_wide_pct=0.0001))     # wide-CPR day
        self.assertIsNone(self._detect(BREAKDOWN, I.BREAKOUT, entry_end=dt.time(9, 50)))

    def test_cached_levels_give_the_same_signal(self):
        b = bars_from(BREAKDOWN)
        lv = I.levels_from(PREV, b, 6)
        kl = [I.key_levels(lv, b[:i]) for i in range(len(b) + 1)]
        self.assertEqual(I.detect(b, lv, self.P, I.BREAKOUT), I.detect(b, lv, self.P, I.BREAKOUT, kl))

    def test_bull_trap(self):
        s = self._detect(TRAP, I.TRAP)
        self.assertEqual((s.direction, s.level), (-1, 74050))
        self.assertGreater(s.stop, 74120)                                 # beyond the poke high
        self.assertIsNone(self._detect(TRAP[:-1] + [(74010, 74080, 74005, 74070)], I.TRAP))   # held above: no trap

    def test_pullback_after_a_confirmed_breakdown(self):
        rows = BREAKDOWN + [(73780, 73835, 73775, 73830),       # pulls back up towards the broken 73850
                            (73830, 73845, 73800, 73810),
                            (73810, 73812, 73720, 73725)]       # strong red bar below the previous low: resume
        s = self._detect(rows, I.PULLBACK, room_r=1.0)
        self.assertEqual((s.kind, s.direction, s.level), (I.PULLBACK, -1, 73850))
        self.assertGreater(s.stop, 73845)                                 # above the pullback high
        reclaimed = BREAKDOWN + [(73780, 73990, 73775, 73980), (73980, 73985, 73900, 73905)]
        self.assertIsNone(self._detect(reclaimed, I.PULLBACK, room_r=1.0))   # closed back above the stop: failed

    def test_range_day(self):
        quiet = OR + [(74000, 74020, 73980, 74000 + (5 if i % 2 else -5)) for i in range(20)]
        s = self._detect(quiet, I.RANGE)
        self.assertEqual(s.direction, 0)
        self.assertLessEqual(s.lo, 73950)
        self.assertIsNone(self._detect(quiet[:12], I.RANGE))             # before 11:00


def _ictx(rows):
    b = bars_from(rows)
    return lib.IntradayContext(b, I.levels_from(PREV, b, 6))


def _chain(spot, t):
    return bt.model_chain(spot, 0.15, DAY + dt.timedelta(days=2), t, BacktestParams(), StrategyParams().risk_free_rate)


def _spec(template, **p):
    return next(s for s in lib.build_library() if s.template == template
                and all(s.p.get(k) == v for k, v in p.items()))


class PlanTests(unittest.TestCase):
    T = dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST)

    def test_single_leg_put_on_a_breakdown(self):
        spec = _spec("ib_long", sig=1, dir=-1, flt=0, conf=1, tr=2.5, otm=0, be=1)
        plan = lib.build_plan(spec, _chain(73780, self.T), NORMAL, None, DAY, self.T, None, None, _ictx(BREAKDOWN))
        self.assertEqual(len(plan.legs), 1)
        self.assertEqual((plan.legs[0].side, plan.legs[0].contract.kind), ("BUY", "PE"))
        self.assertTrue(plan.spot_rules)
        self.assertGreater(plan.spot_hi, plan.entry_spot)                 # stop above (bearish trade)
        self.assertLess(plan.spot_lo, plan.entry_spot)                    # target below
        self.assertLess(plan.spot_be, plan.entry_spot)
        self.assertFalse(spec.multi_leg)

    def test_direction_filter_and_no_signal(self):
        bull_only = _spec("ib_long", sig=1, dir=1, flt=0, conf=1)
        self.assertIsNone(lib.build_plan(bull_only, _chain(73780, self.T), NORMAL, None, DAY, self.T, None, None,
                                         _ictx(BREAKDOWN)))
        spec = _spec("ib_long", sig=1, dir=-1, flt=0, conf=1)
        self.assertIsNone(lib.build_plan(spec, _chain(74000, self.T), NORMAL, None, DAY, self.T, None, None, _ictx(OR)))
        self.assertIsNone(lib.build_plan(spec, _chain(73780, self.T), NORMAL, None, DAY, self.T))   # no context

    def test_credit_spread_sold_beyond_the_trap(self):
        spec = _spec("ib_credit", sig=2, width=200)
        t = self.T + dt.timedelta(minutes=5)
        plan = lib.build_plan(spec, _chain(73970, t), NORMAL, None, DAY, t, None, None, _ictx(TRAP))
        short = next(l for l in plan.legs if l.side == "SELL")
        self.assertEqual(short.contract.kind, "CE")
        self.assertGreaterEqual(short.contract.strike, 74120)            # above the trap high where stops sit
        self.assertTrue(spec.multi_leg)
        self.assertIsNone(plan.spot_lo)
        self.assertGreater(plan.spot_hi, 74120)


class SpotExitTests(unittest.TestCase):
    def _pos(self, lo, hi, be, entry):
        plan = types.SimpleNamespace(variant_id="ib", name="IB_LONG", expiry=DAY, sl_frac=0.9, spot_rules=True,
                                     spot_lo=lo, spot_hi=hi, spot_be=be, entry_spot=entry)
        return types.SimpleNamespace(plan=plan, legs=[], max_loss=8000.0, max_profit=8000.0, peak_pnl=0.0,
                                     exit_state={})

    def test_bounds_and_breakeven(self):
        eng = ExitEngine(ExitParams(), verbose=False)
        now = dt.datetime.combine(DAY, dt.time(11, 0), tzinfo=IST)
        pos = self._pos(lo=73400, hi=74000, be=73650, entry=73800)       # bearish: stop 74000, target 73400
        self.assertIsNone(eng.evaluate(pos, 0, now, None, 73900).reason)
        self.assertIsNone(eng.evaluate(pos, 2500, now, None, 73640).reason)   # +1R: stop moves to entry
        self.assertEqual(pos.exit_state["spot_hi"], 73800)
        self.assertEqual(eng.evaluate(pos, 100, now, None, 73801).reason, "SPOT_HI")
        pos2 = self._pos(lo=73400, hi=74000, be=None, entry=73800)
        self.assertEqual(eng.evaluate(pos2, 3000, now, None, 73399).reason, "SPOT_LO")
        self.assertEqual(eng.evaluate(self._pos(73400, 74000, None, 73800), -2000, now, None, 73800).reason,
                         "HARD_STOP")                                      # rupee backstop still applies
        self.assertIsNone(eng.evaluate(self._pos(73400, 74000, None, 73800), 1500, now, None, None).reason)

    def test_state_round_trip_keeps_spot_rules(self):
        import main
        spec = _spec("ib_long", sig=1, dir=-1, flt=0, conf=1, tr=2.5, otm=0, be=1)
        t = dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST)
        plan = lib.build_plan(spec, _chain(73780, t), NORMAL, None, DAY, t, None, None, _ictx(BREAKDOWN))
        pos = ManagedPosition(plan, 1, [OpenLeg(l.contract, l.side, 20, l.ref_price) for l in plan.legs], t,
                              plan.max_loss_per_lot, plan.max_profit_per_lot)
        back = main._position_from_dict(json.loads(json.dumps(main._position_to_dict(pos))))
        self.assertEqual((back.plan.spot_lo, back.plan.spot_hi, back.plan.spot_be, back.plan.entry_spot),
                         (plan.spot_lo, plan.spot_hi, plan.spot_be, plan.entry_spot))
        self.assertTrue(back.plan.spot_rules)


class CapitalGateTests(unittest.TestCase):
    def test_small_accounts_trade_single_leg_only(self):
        import main
        b = object.__new__(main.Bot)
        b.cfg = types.SimpleNamespace(multi_leg_min_capital=100_000.0)
        single, multi = _spec("ib_long", sig=1), _spec("ib_credit", sig=1)
        b.capital = 20_000.0
        self.assertTrue(b._capital_allows(single))
        self.assertFalse(b._capital_allows(multi))
        self.assertFalse(b._capital_allows(_spec("iron_condor")))
        b.capital = 150_000.0
        self.assertTrue(b._capital_allows(multi))


class ExploreGatingTests(unittest.TestCase):
    def _bot(self, approved, capital, explore_with_approved=False):
        import main
        from config import RiskParams, Schedule
        from risk_manager import RiskManager
        b = object.__new__(main.Bot)
        b.cfg = types.SimpleNamespace(multi_leg_min_capital=100_000.0, explore_with_approved=explore_with_approved)
        b.capital, b.trades_today, b._entry_block_until = capital, 0, 0.0
        b.spec_by_id = {s.id: s for s in lib.build_library()}
        b.approved = {v: 1.0 for v in approved}
        b.risk = RiskManager(RiskParams(), Schedule())
        b.explored = 0
        b._try_explore = lambda now, snap: setattr(b, "explored", b.explored + 1)
        return b

    def test_explore_only_when_no_validated_strategy_fits_the_account(self):
        now = dt.datetime.combine(DAY, dt.time(10, 0), tzinfo=IST)
        snap = types.SimpleNamespace(fresh=True, chain=_chain(74000, now), rs=NORMAL, trend_daily=0, features=None,
                                     tinfo=None, ictx=_ictx(OR))                       # no signal on a quiet morning
        single, multi = _spec("ib_long", sig=1, dir=-1, flt=1).id, _spec("ib_credit", sig=1).id
        b = self._bot([single], 20_000.0)
        b._try_entry(now, snap)
        self.assertEqual(b.explored, 0)                                   # validated strategy exists: wait for it
        b = self._bot([multi], 20_000.0)                                  # only multi-leg approved: unusable at 20k
        b._try_entry(now, snap)
        self.assertEqual(b.explored, 1)
        b = self._bot([single], 20_000.0, explore_with_approved=True)
        b._try_entry(now, snap)
        self.assertEqual(b.explored, 1)


class IntradayBacktestTests(unittest.TestCase):
    def test_simulated_stop_and_target(self):
        spec = _spec("ib_long", sig=1, dir=-1, flt=0, conf=1, tr=2.5, otm=0, be=0)
        b = bars_from(BREAKDOWN)
        t = b[-1].ts + dt.timedelta(minutes=5)
        plan = lib.build_plan(spec, _chain(73780, t), NORMAL, None, DAY, t, None, None,
                              lib.IntradayContext(b, I.levels_from(PREV, b, 6)))
        sp = ib.SimParams()
        down = b + bars_from([(73780, 73790, 73300, 73320)], start=dt.time(10, 0))     # hits the target
        tr, _ = ib._simulate(plan, down, len(b) - 1, [15.0] * len(down), sp, CostParams(), 2000.0)
        self.assertEqual(tr.reason, "SPOT_LO")
        self.assertGreater(tr.net, 0)
        up = b + bars_from([(73780, 74100, 73770, 74090)], start=dt.time(10, 0))       # through the stop
        tr, _ = ib._simulate(plan, up, len(b) - 1, [15.0] * len(up), sp, CostParams(), 2000.0)
        self.assertIn(tr.reason, ("SPOT_HI", "HARD_STOP"))
        self.assertLess(tr.net, 0)
        flat = b + bars_from([(73780, 73790, 73770, 73780)] * 70, start=dt.time(10, 0))
        tr, _ = ib._simulate(plan, flat, len(b) - 1, [15.0] * len(flat), sp, CostParams(), 2000.0)
        self.assertEqual(tr.reason, "SQUARE_OFF_TIME")
        self.assertLess(tr.net, 0)                                        # time decay + costs on a flat day

    def test_run_days_end_to_end(self):
        days = {}
        for k in range(12):
            d = DAY - dt.timedelta(days=20 - k)
            rows = BREAKDOWN + [(73780, 73790, 73770, 73780)] * 60 if k == 11 else [(74000, 74400, 73300, 74000)] * 75
            days[d.isoformat()] = bars_from(rows, day=d)
        vix = {b.ts: 15.0 for bars in days.values() for b in bars}
        spec = _spec("ib_long", sig=1, dir=-1, flt=0, conf=1, tr=2.5, otm=0, be=0)
        out = ib.run(days, vix, [spec], ib.SimParams(), 1)
        self.assertEqual(len(out[spec.id]), 1)
        self.assertEqual(ib.family(spec.id), "ib_long/breakout")

    def test_daily_backtest_skips_intraday_variants(self):
        bars = bt.synthetic_bars(80)
        res = bt.run_backtest(bars, [_spec("ib_long", sig=1)], StrategyParams(), BacktestParams(), CostParams())
        self.assertEqual(list(res.values()), [[]])


class PromoteDrawdownTests(unittest.TestCase):
    def test_tier_needs_capital_for_the_backtest_drawdown(self):
        import subprocess
        spec = _spec("ib_long", sig=1, dir=-1, flt=1)
        with tempfile.TemporaryDirectory() as d:
            full = {"n": 100, "net": 60000, "pf": 1.7, "max_dd": 19000}
            json.dump({"valid_until": "2099-01-01", "variants": []}, open(os.path.join(d, "approved_backtest.json"), "w"))
            json.dump({"valid_until": "2099-01-01", "variants": [{"id": spec.id, "score": 3, "full": full,
                      "tiers": ["small", "medium", "large"]}]}, open(os.path.join(d, "approved_intraday.json"), "w"))
            con = sqlite3.connect(os.path.join(d, "sxbot.db"))
            con.execute("CREATE TABLE shadow_trades(variant, entry_ts, exit_ts, reason, regime, gross, cost, net, max_loss)")
            con.execute("CREATE TABLE trades(ts, mode, variant, event, reason, gross, cost, net, payload)")
            today = dt.date.today()
            for i in range(30):
                day = (today - dt.timedelta(days=30 - i)).isoformat()
                con.execute("INSERT INTO shadow_trades VALUES (?,?,?,?,?,?,?,?,?)",
                            (spec.id, day + "T10:00:00+05:30", day, "SPOT_LO", "N", 1, 1, 900.0 if i % 3 else -300.0, 8000.0))
            con.commit()
            con.close()
            env = {**os.environ, "KITE_API_KEY": "x"}
            root = os.path.join(os.path.dirname(__file__), "..")
            subprocess.run([sys.executable, os.path.join(root, "promote.py"), "--data-dir", d], check=True,
                           capture_output=True, env=env, cwd=root)
            live = json.load(open(os.path.join(d, "approved_live.json")))["variants"]
            self.assertEqual(len(live), 1)
            self.assertNotIn("small", live[0]["tiers"])                    # Rs 15k < 1.5 x Rs 19k drawdown
            self.assertIn("medium", live[0]["tiers"])


if __name__ == "__main__":
    unittest.main()
