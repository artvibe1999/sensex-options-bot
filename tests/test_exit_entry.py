"""Tests: exit engine (neutral zone, hard stop, laddered ratchet, adaptation, persistence), market context,
entry filter."""
from __future__ import annotations

import datetime as dt
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import sim_harness as sh
from chain_features import ChainFeatures
from config import IST
from entry_filter import EntryParams, evaluate_entry, plan_bias
from exit_engine import ExitEngine, ExitParams
from market_context import (LONG_BUILDUP, LONG_UNWINDING, NEUTRAL, SHORT_BUILDUP, SHORT_COVERING, ContextSignals,
                            MarketContext, classify_oi_buildup, writing_bias)
from option_strategies import STRATEGY_CLASSES
from risk_manager import ManagedPosition, OpenLeg
from trend_regime import TrendInfo, TrendState

CH, NOW, BP, SP = sh.sample_chain()


def _pos(name="bull_call_spread", lots=1, max_loss=6000.0, max_profit=9000.0):
    plan = sh.plan_from_strategy(STRATEGY_CLASSES[name](), CH)
    legs = [OpenLeg(l.contract, l.side, plan.qty_for(l, lots), l.ref_price) for l in plan.legs]
    return ManagedPosition(plan, lots, legs, NOW, max_loss, max_profit)


def _engine(**kw):
    return ExitEngine(ExitParams(**kw))


def _run(eng, pos, series, now=NOW, sig=None, spot=None):
    out = []
    for pnl in series:
        out.append(eng.evaluate(pos, pnl, now, sig, spot).reason)
    return out


class LotSizeTests(unittest.TestCase):
    def test_pnl_includes_the_20_unit_lot(self):
        pos = _pos(lots=1)
        self.assertTrue(all(l.qty == 20 for l in pos.legs))
        ltps = {l.contract.token: l.entry_price for l in pos.legs}
        long_leg = next(l for l in pos.legs if l.side == "BUY")
        ltps[long_leg.contract.token] += 100.0                       # +100 index points on one 20-unit lot
        self.assertAlmostEqual(pos.pnl(ltps), 2000.0)
        pos3 = _pos(lots=3)
        self.assertTrue(all(l.qty == 60 for l in pos3.legs))


class NeutralZoneTests(unittest.TestCase):
    def test_nothing_exits_between_minus_and_plus_2000(self):
        eng, pos = _engine(), _pos()
        series = [-1999, -1500, -500, 0, 800, 1500, 1999, 1000, -300, -1999, 1999.5]
        self.assertEqual(_run(eng, pos, series), [None] * len(series))

    def test_hard_stop_at_minus_2000(self):
        eng, pos = _engine(), _pos()
        self.assertEqual(_run(eng, pos, [-1000, -2000]), [None, "HARD_STOP"])
        self.assertEqual(_run(eng, _pos(), [-3500]), ["HARD_STOP"])

    def test_variant_stop_fractions_do_not_exit_inside_the_zone(self):
        pos = _pos(max_loss=2300.0, max_profit=1700.0)
        pos.plan = SimpleNamespace(**{**pos.plan.__dict__, "sl_frac": 0.5, "tp_frac": 0.5}) \
            if hasattr(pos.plan, "__dict__") else pos.plan
        self.assertEqual(_run(_engine(), pos, [-1200, 900, 1700]), [None, None, None])

    def test_trailing_never_exits_below_arm_level(self):
        eng, pos = _engine(), _pos()
        r = _run(eng, pos, [2000, 2500, 2100, 2000, 1999])
        self.assertEqual(r[:4], [None] * 4)                         # crossing 2000 arms the trail, no instant exit
        self.assertEqual(r[4], "TRAILING_STOP")                     # first print below the 2000 floor exits

    def test_unarmed_dip_is_not_a_trailing_exit(self):
        eng, pos = _engine(), _pos()
        self.assertEqual(_run(eng, pos, [1900, 100, 1999, 50]), [None] * 4)


class LadderTests(unittest.TestCase):
    def test_floor_steps_up_at_milestones(self):
        eng, pos = _engine(), _pos()
        floors = []
        for peak in (2000, 3000, 4000, 5000, 6000, 10000):
            floors.append(eng.evaluate(pos, peak, NOW).floor)
        self.assertEqual(floors[0], 2000)
        self.assertEqual(floors[1], 2000)                           # 3000 peak: 60% = 1800 -> arm level wins
        self.assertEqual(floors[2], 2400)                           # 4000: 60% = 2400
        self.assertEqual(floors[3], 3000)                           # 5000: 60% = 3000 (ladder 3000 equal)
        self.assertEqual(floors[4], 4000)                           # 6000: ladder 4000 beats 60% = 3600
        self.assertEqual(floors[5], 8000)                           # 10000: ladder 8000 beats 6000
        self.assertEqual(sorted(floors), floors)                    # monotonic

    def test_floor_is_a_ratchet_even_when_signals_widen_the_band(self):
        eng, pos = _engine(), _pos()
        pos.exit_state = {"bias": 1, "vega": 10.0, "theta": -5.0, "entry_spot": 80000, "hi": 80000, "lo": 80000,
                          "floor": None, "armed": False, "notes": []}
        eng.evaluate(pos, 6000, NOW)                                # floor 4000
        wide = ContextSignals(ts=NOW, spot=80000, pcr_mom=0.2, buildup=LONG_BUILDUP, max_pain_drift=300.0)
        d = eng.evaluate(pos, 5500, NOW, wide, 80000)
        self.assertGreaterEqual(d.floor, 4000)
        self.assertIsNone(d.reason)
        self.assertEqual(eng.evaluate(pos, 3900, NOW, wide, 80000).reason, "TRAILING_STOP")


class AdaptationTests(unittest.TestCase):
    def _state(self, pos, bias=1, vega=10.0):
        pos.exit_state = {"bias": bias, "vega": vega, "theta": -5.0, "entry_spot": 80000.0, "hi": 80000.0,
                          "lo": 80000.0, "floor": None, "armed": False, "notes": []}

    def test_adverse_signals_tighten_support_widens(self):
        eng = _engine()
        base_pos = _pos()
        self._state(base_pos)
        base = eng.evaluate(base_pos, 4000, NOW).floor
        bad_pos = _pos()
        self._state(bad_pos)
        bad = ContextSignals(ts=NOW, spot=79800, pcr_mom=-0.12, buildup=SHORT_BUILDUP, max_pain_drift=-250.0,
                             spot_chg_pct=0.2, spot_chg_short_pct=-0.1, vix_chg_pct=None, write_bias=-1)
        tight = eng.evaluate(bad_pos, 4000, NOW, bad, 79800).floor      # 0.25% retrace from the high as well
        good_pos = _pos()
        self._state(good_pos)
        good = ContextSignals(ts=NOW, spot=80100, pcr_mom=0.1, buildup=LONG_BUILDUP, max_pain_drift=200.0)
        loose = eng.evaluate(good_pos, 4000, NOW, good, 80100).floor
        self.assertGreater(tight, base)
        self.assertLessEqual(loose, base)

    def test_vix_spike_hits_short_vega_harder_than_long_vega(self):
        eng = _engine()
        sig = ContextSignals(ts=NOW, spot=80000, vix_chg_pct=6.0)
        short_v, long_v = _pos("iron_condor"), _pos("bull_call_spread")
        self._state(short_v, bias=0, vega=-300.0)
        self._state(long_v, bias=0, vega=300.0)
        self.assertGreater(eng.evaluate(short_v, 4000, NOW, sig, 80000).floor,
                           eng.evaluate(long_v, 4000, NOW, sig, 80000).floor)

    def test_theta_window_on_expiry_day_tightens(self):
        eng = _engine()
        morning, late = _pos(), _pos()
        self._state(morning)
        self._state(late)
        exp = morning.plan.expiry
        m = eng.evaluate(morning, 4000, dt.datetime.combine(exp, dt.time(11, 0), tzinfo=IST)).floor
        l = eng.evaluate(late, 4000, dt.datetime.combine(exp, dt.time(14, 0), tzinfo=IST)).floor
        self.assertGreater(l, m)

    def test_giveback_is_clamped(self):
        eng = _engine(min_giveback=0.15, max_giveback=0.60)
        pos = _pos()
        self._state(pos)
        sig = ContextSignals(ts=NOW, spot=1, pcr_mom=-1.0, buildup=SHORT_BUILDUP, write_bias=-1, vix_chg_pct=9.0,
                             max_pain_drift=-900.0, spot_chg_pct=1.0, spot_chg_short_pct=-1.0)
        d = eng.evaluate(pos, 4000, dt.datetime.combine(pos.plan.expiry, dt.time(14, 30), tzinfo=IST), sig, 70000.0)
        self.assertGreaterEqual(d.giveback, 0.15)
        self.assertLessEqual(d.giveback, 0.60)


class PersistenceTests(unittest.TestCase):
    def test_exit_state_survives_restart(self):
        import json
        import main
        eng, pos = _engine(), _pos()
        eng.init_state(pos, CH, 80000.0)
        eng.evaluate(pos, 6000, NOW, None, 80100.0)
        blob = json.loads(json.dumps(main._position_to_dict(pos), default=str))
        back = main._position_from_dict(blob)
        self.assertEqual(back.exit_state["floor"], pos.exit_state["floor"])
        self.assertEqual(back.peak_pnl, 6000)
        self.assertEqual(eng.evaluate(back, 3900, NOW).reason, "TRAILING_STOP")

    def test_engine_error_still_enforces_the_hard_stop(self):
        eng, pos = _engine(), _pos()
        pos.exit_state = {"bias": "boom", "hi": object(), "lo": 1.0, "entry_spot": 1.0}
        self.assertEqual(eng.evaluate(pos, -2500, NOW, None, 80000.0).reason, "HARD_STOP")


class MarketContextTests(unittest.TestCase):
    def test_classify_buildup(self):
        self.assertEqual(classify_oi_buildup(0.3, 5000, 4000, 1_000_000), LONG_BUILDUP)
        self.assertEqual(classify_oi_buildup(-0.3, 5000, 4000, 1_000_000), SHORT_BUILDUP)
        self.assertEqual(classify_oi_buildup(0.3, -5000, -4000, 1_000_000), SHORT_COVERING)
        self.assertEqual(classify_oi_buildup(-0.3, -5000, -4000, 1_000_000), LONG_UNWINDING)
        self.assertEqual(classify_oi_buildup(0.01, 5000, 4000, 1_000_000), NEUTRAL)       # price noise
        self.assertEqual(classify_oi_buildup(0.3, 10, 10, 1_000_000), NEUTRAL)            # OI noise
        self.assertEqual(classify_oi_buildup(None, 5000, 4000, 1_000_000), NEUTRAL)

    def test_writing_bias(self):
        self.assertEqual(writing_bias(0.2, 1000, 5000), 1)       # puts written faster, price up: support
        self.assertEqual(writing_bias(-0.2, 5000, 1000), -1)     # calls written faster, price down: resistance
        self.assertEqual(writing_bias(0.2, 3000, 3000), 0)

    def test_context_tracks_spot_and_oi(self):
        ctx = MarketContext(sample_every_s=1.0)
        quotes = {(1.0, "CE"): SimpleNamespace(oi=100000.0), (1.0, "PE"): SimpleNamespace(oi=100000.0)}
        t0 = dt.datetime(2026, 10, 26, 10, 0, tzinfo=IST)

        def snap(spot, call_chg=None, put_chg=None, vix=15.0, pcr=1.0):
            f = SimpleNamespace(pcr=pcr, max_pain=80000.0, call_oi_chg=call_chg, put_oi_chg=put_chg)
            return SimpleNamespace(spot=spot, vix=vix, features=f, chain=SimpleNamespace(quotes=quotes))

        ctx.update(snap(80000.0), t0)
        ctx.update(snap(80160.0, 400.0, 600.0, vix=15.6, pcr=1.1), t0 + dt.timedelta(minutes=6))
        s = ctx.signals()
        self.assertAlmostEqual(s.spot_chg_pct, 0.2, places=2)
        self.assertEqual(s.buildup, LONG_BUILDUP)
        self.assertAlmostEqual(s.pcr_mom, 0.1, places=3)
        self.assertAlmostEqual(s.vix_chg_pct, 4.0, places=1)
        ctx.update(SimpleNamespace(spot=float("nan")), t0)           # garbage never raises
        ctx.update(None, t0)


def _snap(plan_chain=CH, skew=3.0, trend_daily=1, daily=None, intraday=None):
    f = ChainFeatures(pcr=1.0, call_wall=None, put_wall=None, max_pain=None, net_gex=0.0, atm_iv=0.15, skew=skew,
                      call_oi_chg=None, put_oi_chg=None, pcr_chg=None, atm_spread_pct=1.0, atm_volume=1000.0)
    ti = TrendInfo(daily=daily or TrendState(), intraday=intraday)
    return SimpleNamespace(chain=plan_chain, features=f, trend_daily=trend_daily, tinfo=ti)


def _sig(buildup=LONG_BUILDUP, write=0, vix=None):
    return ContextSignals(ts=NOW, spot=80000, spot_chg_pct=0.2, call_oi_chg=1000.0, put_oi_chg=2000.0,
                          total_oi=1e6, buildup=buildup, write_bias=write, vix_chg_pct=vix)


class EntryFilterTests(unittest.TestCase):
    P = EntryParams()

    def _plan(self, name):
        return sh.plan_from_strategy(STRATEGY_CLASSES[name](), CH)

    def test_bias_detection(self):
        self.assertEqual(plan_bias(self._plan("bull_call_spread"), CH)["bias"], 1)
        self.assertEqual(plan_bias(self._plan("bear_put_spread"), CH)["bias"], -1)
        self.assertEqual(plan_bias(self._plan("iron_condor"), CH)["bias"], 0)

    def test_bullish_entry_needs_trend_skew_and_oi_to_agree(self):
        plan = self._plan("bull_call_spread")
        ok = evaluate_entry(plan, _snap(), _sig(), self.P)
        self.assertTrue(ok.ok, ok.reason)
        self.assertFalse(evaluate_entry(plan, _snap(trend_daily=-1), _sig(), self.P).ok)             # trend opposes
        self.assertFalse(evaluate_entry(plan, _snap(skew=12.0), _sig(), self.P).ok)                  # heavy put skew
        self.assertFalse(evaluate_entry(plan, _snap(), _sig(buildup=SHORT_BUILDUP), self.P).ok)      # OI opposes
        self.assertFalse(evaluate_entry(plan, _snap(), _sig(buildup=NEUTRAL), self.P).ok)            # no confirmation
        self.assertTrue(evaluate_entry(plan, _snap(), _sig(buildup=NEUTRAL, write=1), self.P).ok)    # put writing confirms

    def test_missing_data_fails_closed(self):
        plan = self._plan("bull_call_spread")
        self.assertFalse(evaluate_entry(plan, _snap(skew=None), _sig(), self.P).ok)
        self.assertFalse(evaluate_entry(plan, _snap(), None, self.P).ok)
        self.assertFalse(evaluate_entry(plan, _snap(trend_daily=None), _sig(), self.P).ok)
        lax = EntryParams(strict=False)
        self.assertTrue(evaluate_entry(plan, _snap(skew=None), _sig(), lax).ok)

    def test_neutral_structure_rejected_in_a_valid_trend(self):
        plan = self._plan("iron_condor")
        calm = _snap(trend_daily=0)
        self.assertTrue(evaluate_entry(plan, calm, _sig(buildup=NEUTRAL), self.P).ok)
        trending = _snap(trend_daily=1, daily=TrendState(dir=1, valid=True, er=0.8))
        self.assertFalse(evaluate_entry(plan, trending, _sig(buildup=NEUTRAL), self.P).ok)
        self.assertFalse(evaluate_entry(plan, calm, _sig(buildup=LONG_BUILDUP), self.P).ok)          # directional OI flow

    def test_arm_level_must_be_reachable(self):
        plan = self._plan("bull_call_spread")
        small = evaluate_entry(plan, _snap(), _sig(), self.P, lots=1, arm_rs=plan.max_profit_per_lot + 1)
        self.assertFalse(small.ok)
        self.assertIn("reach", small.reason)
        self.assertTrue(evaluate_entry(plan, _snap(), _sig(), self.P, lots=1, arm_rs=2000.0).ok)

    def test_disabled_and_error_paths(self):
        plan = self._plan("bull_call_spread")
        self.assertTrue(evaluate_entry(plan, _snap(trend_daily=-1), None, EntryParams(enabled=False)).ok)
        self.assertFalse(evaluate_entry(plan, None, _sig(), self.P).ok)                              # internal error: reject


class BotManageTests(unittest.TestCase):
    """Bot._manage wiring: exits come from the engine, never inside the neutral zone, safety exits still work."""

    def _bot(self, pnl_holder, pos):
        import main
        from config import Schedule
        from risk_manager import RiskManager
        from config import RiskParams
        b = object.__new__(main.Bot)
        b.position, b.capital, b.realized = pos, 500000.0, 0.0
        b.xp = ExitParams()
        b.exit_engine = ExitEngine(b.xp)
        b.ctx, b._last_snap = MarketContext(), None
        b.risk = RiskManager(RiskParams(), Schedule())
        b.risk.apply_capital(b.capital)
        b.cfg = SimpleNamespace(schedule=Schedule(), execution=SimpleNamespace(product="MIS"))
        b._last_exit_status, b._last_state_save = 0.0, 1e18
        b.exited = []
        b._exit = lambda reason: b.exited.append(reason)
        b._save_state = lambda: None
        # one long leg carries all the P&L: pnl = (ltp - entry) x 20
        long_leg = next(l for l in pos.legs if l.side == "BUY")
        b._ltps = lambda p: {**{l.contract.token: l.entry_price for l in p.legs},
                             long_leg.contract.token: long_leg.entry_price + pnl_holder[0] / long_leg.qty}
        return b

    def test_manage_flow(self):
        pos = _pos(lots=1)
        pnl = [0.0]
        b = self._bot(pnl, pos)
        t = dt.datetime(2026, 10, 26, 11, 0, tzinfo=IST)
        for v in (-1999.0, 1500.0, 1999.0, 3000.0, 2500.0):
            pnl[0] = v
            b._manage(t)
        self.assertEqual(b.exited, [])
        pnl[0] = 1990.0
        b._manage(t)
        self.assertEqual(b.exited, ["TRAILING_STOP"])

    def test_hard_stop_and_square_off(self):
        pos = _pos()
        pnl = [-2000.0]
        b = self._bot(pnl, pos)
        b._manage(dt.datetime(2026, 10, 26, 11, 0, tzinfo=IST))
        self.assertEqual(b.exited, ["HARD_STOP"])
        pos2 = _pos()
        pnl2 = [500.0]
        b2 = self._bot(pnl2, pos2)
        b2._manage(dt.datetime(2026, 10, 26, 15, 16, tzinfo=IST))      # inside the zone, but the day is over
        self.assertEqual(b2.exited, ["SQUARE_OFF_TIME"])

    def test_daily_loss_kill_switch_still_overrides(self):
        pos = _pos()
        pnl = [-500.0]
        b = self._bot(pnl, pos)
        b.realized = -b.capital * 0.05                                   # day already past the 3% loss limit
        b._manage(dt.datetime(2026, 10, 26, 11, 0, tzinfo=IST))
        self.assertEqual(b.exited, ["DAILY_LOSS_LIMIT"])


if __name__ == "__main__":
    unittest.main()
