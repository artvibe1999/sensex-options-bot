"""Regression tests for the second review: shadow/live consistency, realistic paper fills, restart safety,
partial-exit accounting and the forward-evidence exploration filter."""
import datetime as dt
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt
import recorder
import strategy_library as lib
from config import IST, BacktestParams, RiskParams, Schedule, StrategyParams
from costs import CostParams
from execution import OrderLeg, PaperBroker
from exit_engine import ExitEngine, ExitParams
from market_data import Snapshot
from risk_manager import ManagedPosition, OpenLeg, RiskManager
from shadow import ShadowBook
from strategy_engine import Regime, RegimeState

BP, SP = BacktestParams(), StrategyParams()
NORMAL = RegimeState(Regime.NORMAL, 15.0, 0.9, 13.0, False, 0)


def _t():
    return dt.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)


def _snap(t, trend_dir, trend_daily, rs=NORMAL):
    chain = bt.model_chain(80000.0, 0.14, t.date() + dt.timedelta(days=2), t, BP, SP.risk_free_rate)
    return Snapshot(t, 80000.0, rs.vix, rs, chain, trend_dir, True, None, None, trend_daily)


class ShadowConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = recorder.Recorder(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self):
        self.rec.close()
        self.tmp.cleanup()

    def test_with_trend_variants_use_the_daily_trend_like_the_live_bot(self):
        specs = [s for s in lib.build_library() if s.template == "bull_put_spread" and s.trend == "with" and "delta0.25" in s.id
                 and s.dte_max == 3 and "NORMAL" in s.regimes][:2]
        self.assertTrue(specs)
        t = _t()
        book = ShadowBook(specs, self.rec, RiskParams(), Schedule(), CostParams())
        book.step(_snap(t, trend_dir=-1, trend_daily=1), t)          # 5-minute trend down, daily trend up
        self.assertTrue(book.open, "a bullish 'with' variant must follow the DAILY trend")
        book2 = ShadowBook(specs, self.rec, RiskParams(), Schedule(), CostParams())
        book2.done_today.clear()
        book2.step(_snap(t, trend_dir=1, trend_daily=-1), t)
        self.assertFalse(book2.open)

    def test_shadow_exits_with_the_live_exit_engine(self):
        specs = [s for s in lib.build_library() if s.template == "iron_condor" and s.dte_max == 3
                 and "NORMAL" in s.regimes and s.trend == "none" and not s.needs_oi][:1]
        t = _t()
        eng = ExitEngine(ExitParams(), verbose=False)
        book = ShadowBook(specs, self.rec, RiskParams(), Schedule(), CostParams(), exit_engine=eng)
        snap = _snap(t, 0, 0)
        book.step(snap, t)
        self.assertEqual(len(book.open), 1)
        pos = next(iter(book.open.values()))
        stop = pos.xpos.exit_state["stop_rs"]
        self.assertAlmostEqual(stop, min(2000.0, specs[0].sl_frac * pos.max_loss))
        book._mark = lambda p, s: -stop + 1                           # inside the stop: hold
        book.step(snap, t + dt.timedelta(minutes=1))
        self.assertEqual(len(book.open), 1)
        book._mark = lambda p, s: -stop                               # at the stop: the engine exits
        book.step(snap, t + dt.timedelta(minutes=2))
        self.assertFalse(book.open)
        rows = self.rec._db.execute("SELECT reason FROM shadow_trades").fetchall()
        self.assertEqual(rows, [("HARD_STOP",)])

    def test_shadow_results_are_read_walk_forward(self):
        self.rec.shadow_trade("a", "2026-10-26T10:00:00+05:30", "x", "R", "N", 1, 1, -5.0, 100)
        self.rec.shadow_trade("a", "2026-10-27T10:00:00+05:30", "x", "R", "N", 1, 1, 7.0, 100)
        self.assertEqual(self.rec.shadow_results("2026-10-01", "2026-10-27"), [("a", -5.0)])


class PaperFillTests(unittest.TestCase):
    def _leg(self, side, strike=80000.0):
        from zerodha_client import OptionContract
        c = OptionContract(1, f"SX{int(strike)}CE", "BFO", strike, dt.date(2026, 11, 5), "CE", 20, 0.05)
        return OrderLeg(c, side, 20, 100.0)

    def test_fills_at_the_real_bid_and_ask(self):
        pb = PaperBroker(0.005, quote_fn=lambda c: (98.0, 102.0))
        fills = pb.execute([self._leg("BUY"), self._leg("SELL", 80100.0)], "MIS")
        self.assertEqual(fills["SX80000CE"], 102.0)
        self.assertEqual(fills["SX80100CE"], 98.0)

    def test_falls_back_to_slippage_without_a_two_sided_quote(self):
        for q in (None, (0.0, 102.0), (103.0, 102.0)):
            pb = PaperBroker(0.005, quote_fn=lambda c, q=q: q)
            self.assertEqual(pb.execute([self._leg("BUY")], "MIS")["SX80000CE"], 100.5)
        def boom(c):
            raise RuntimeError("feed")
        self.assertEqual(PaperBroker(0.005, quote_fn=boom).execute([self._leg("SELL")], "MIS")["SX80000CE"], 99.5)


def _bare_bot(tmpdir):
    import main
    b = object.__new__(main.Bot)
    b.live, b.mode, b.position = False, "PAPER", None
    b.cfg = types.SimpleNamespace(paths=types.SimpleNamespace(state=os.path.join(tmpdir, "state.json")))
    b.day, b.realized, b.trades_today, b.eod_done = dt.date(2026, 10, 29), -1047.0, 1, False
    b.risk = RiskManager(RiskParams(), Schedule())
    b.alerts = types.SimpleNamespace(send=lambda *a, **k: None)
    return b


class RestartTests(unittest.TestCase):
    def test_day_summary_is_not_repeated_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            b = _bare_bot(d)
            b.eod_done = True
            b._save_state()
            b2 = _bare_bot(d)
            b2._load_state()
            self.assertTrue(b2.eod_done)
            self.assertEqual(b2.realized, -1047.0)


class ExploreBlockTests(unittest.TestCase):
    def test_families_with_clearly_negative_forward_results_are_skipped(self):
        import main
        specs = lib.build_library()
        lose = next(s for s in specs if s.template == "long_put" and s.trend == "ivalid")
        win = next(s for s in specs if s.template == "bull_put_spread" and s.trend == "none")
        few = next(s for s in specs if s.template == "iron_fly")
        rows = [(lose.id, -500.0 + 10 * (i % 3)) for i in range(25)] + [(win.id, 300.0 + i) for i in range(25)] \
            + [(few.id, -900.0) for _ in range(5)]
        b = object.__new__(main.Bot)
        b.cfg = types.SimpleNamespace(explore_fwd_min_trades=20, explore_fwd_days=60, explore_fwd_max_t=-1.0)
        b.spec_by_id = {s.id: s for s in specs}
        seen = {}
        b.rec = types.SimpleNamespace(shadow_results=lambda since, before: (seen.update(since=since, before=before), rows)[1])
        b._refresh_explore_block(dt.datetime(2026, 10, 30, 9, 0, tzinfo=IST))
        self.assertEqual(b._explore_block, {("long_put", "ivalid")})
        self.assertEqual(seen, {"since": "2026-08-31", "before": "2026-10-30"})   # only days before today
        b.cfg.explore_fwd_min_trades = 0
        b._refresh_explore_block(dt.datetime(2026, 10, 30, 9, 0, tzinfo=IST))
        self.assertEqual(b._explore_block, set())


class PartialExitTests(unittest.TestCase):
    def test_final_trade_row_carries_the_whole_trade(self):
        import main
        import sim_harness as sh
        from option_strategies import STRATEGY_CLASSES
        plan = sh.plan_from_strategy(STRATEGY_CLASSES["bull_call_spread"](), sh.sample_chain()[0])
        legs = [OpenLeg(l.contract, l.side, 40, 100.0) for l in plan.legs]
        pos = ManagedPosition(plan, 2, legs, dt.datetime.now(IST), 4000.0, 4000.0)
        b = object.__new__(main.Bot)
        b.position, b.live, b.mode, b.realized, b._exit_failures = pos, False, "PAPER", 0.0, 0
        b.cfg = types.SimpleNamespace(costs=CostParams(),
                                      execution=types.SimpleNamespace(product="MIS", market_exit_after_failures=3))
        b._ltps = lambda p: {l.contract.token: 110.0 for l in p.legs}
        trades, sheet = [], []
        b.rec = types.SimpleNamespace(trade=lambda *a: trades.append(a))
        b.sheets = types.SimpleNamespace(append=lambda tab, h, rows: sheet.append((tab, rows)))
        b.risk = RiskManager(RiskParams(), Schedule())
        b.alerts = types.SimpleNamespace(send=lambda *a, **k: None)
        b._save_state = b._verify_positions = lambda *a, **k: None
        calls = iter([0.5, 1.0])                                     # first attempt closes half of every leg

        class Broker:
            def close(self, out, product, mult, market=False):
                frac = next(calls)
                return {o.contract.symbol: (110.0, int(o.qty * frac)) for o in out}
        b.broker = Broker()
        b._exit("TEST")
        self.assertIsNotNone(b.position)
        first_net = b.realized
        b._exit("TEST")
        self.assertIsNone(b.position)
        final = [t for t in trades if t[3] == "exit"][0]
        partial = [t for t in trades if t[3] == "exit_partial"][0]
        self.assertAlmostEqual(final[5], partial[5] * 2)              # whole-trade gross = both halves
        self.assertAlmostEqual(final[7], b.realized)                   # whole-trade net = what the day realised
        self.assertNotEqual(first_net, b.realized)
        trades_tab = [rows for tab, rows in sheet if tab == "Trades"][0][0]
        self.assertAlmostEqual(trades_tab[7], round(b.realized, 2))


if __name__ == "__main__":
    unittest.main()
