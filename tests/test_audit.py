"""Regression tests for the backtest / shadow / promotion audit."""
import csv
import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backtest as bt
import promote
import recorder
import strategy_library as lib
from config import IST, BacktestParams, RiskParams, Schedule, StrategyParams
from costs import CostParams
from market_data import Snapshot
from shadow import ShadowBook
from strategy_engine import Regime, RegimeState, bs_price, implied_vol

BP, SP = BacktestParams(), StrategyParams()


class BacktestAuditTests(unittest.TestCase):
    def test_expiry_weekday_follows_the_real_history(self):
        self.assertEqual(bt.expiry_weekday_on(dt.date(2024, 6, 3), BP), 4)     # Friday era
        self.assertEqual(bt.expiry_weekday_on(dt.date(2025, 3, 3), BP), 1)     # Tuesday era
        self.assertEqual(bt.expiry_weekday_on(dt.date(2025, 10, 1), BP), 3)    # Thursday era
        forced = BacktestParams(expiry_weekday=2)
        self.assertEqual(bt.expiry_weekday_on(dt.date(2024, 6, 3), forced), 2)

    def test_listed_expiries_override_the_schedule(self):
        known = [dt.date(2026, 10, 21), dt.date(2026, 10, 28)]          # a Wednesday series: not in the schedule
        first = dt.date(2026, 10, 15)
        self.assertEqual(bt.expiry_for(dt.date(2026, 10, 22), BP, known, first), dt.date(2026, 10, 28))
        self.assertEqual(bt.expiry_for(dt.date(2026, 10, 20), BP, known, first), dt.date(2026, 10, 21))
        self.assertEqual(bt.expiry_for(dt.date(2026, 10, 21), BP, known, first), dt.date(2026, 10, 28))   # no 0-DTE
        # before the first recorded day the dated schedule applies (Thursday era)
        self.assertEqual(bt.expiry_for(dt.date(2025, 10, 1), BP, known, first), dt.date(2025, 10, 2))
        self.assertEqual(bt.expiry_for(dt.date(2025, 10, 1), BP), dt.date(2025, 10, 2))

    def test_recorded_expiries_are_read_from_the_database(self):
        with tempfile.TemporaryDirectory() as d:
            rec = recorder.Recorder(os.path.join(d, "x.db"))
            rec._write("INSERT INTO chain VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       [("2026-10-20T10:00:00+05:30", "2026-10-21", 80000.0, "CE", 100.0, 0, 0.1, 0, 0, 0, 0)])
            rec.close()
            known, first = bt.exchange_expiries(os.path.join(d, "x.db"), [dt.date(2026, 11, 4)])
            self.assertEqual(known, [dt.date(2026, 10, 21), dt.date(2026, 11, 4)])
            self.assertEqual(first, dt.date(2026, 10, 20))

    def test_bad_input_bars_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            day, rows = dt.date(2023, 1, 2), []
            while len(rows) < 320:
                day += dt.timedelta(days=1)
                if day.weekday() < 5:
                    rows.append((day, 70000.0, 70500.0, 69500.0, 70100.0))
            rows[100] = (rows[100][0], 70000.0, 70500.0, 69500.0, 0.0)           # zero close
            rows[101] = (rows[101][0], 70000.0, 69000.0, 69500.0, 70100.0)       # high < low
            for name, vix in (("i.csv", None), ("v.csv", 14.0)):
                with open(os.path.join(d, name), "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["date", "open", "high", "low", "close"])
                    for r in rows:
                        w.writerow([r[0]] + (list(r[1:]) if vix is None else [vix] * 4))
            bars = bt.load_csv(os.path.join(d, "i.csv"), os.path.join(d, "v.csv"))
            self.assertEqual(len(bars), 318)
            self.assertTrue(all(b["close"] > 0 and b["high"] >= b["low"] for b in bars))

    def test_trading_clock_iv_roundtrip(self):
        """calibrate.py solves IV on the backtest's own clock: pricing then inverting must return the input."""
        now = dt.datetime(2026, 10, 22, 10, 0, tzinfo=IST)
        exp = dt.date(2026, 10, 27)
        T = bt._years(exp, now)
        px = bs_price(80000.0, 80000.0, T, SP.risk_free_rate, 0.14, "CE")
        self.assertAlmostEqual(implied_vol(px, 80000.0, 80000.0, T, SP.risk_free_rate, "CE"), 0.14, places=3)


class PromotionAuditTests(unittest.TestCase):
    def test_tstat(self):
        self.assertEqual(promote._tstat([5.0]), 0.0)
        self.assertGreater(promote._tstat([10, 12, 9, 11, 10, 13]), 5)
        self.assertLess(promote._tstat([100, -90, 120, -110, 95, -80, 90, -95]), 1.0)   # noisy winners are not edge
        self.assertEqual(promote._tstat([3.0, 3.0, 3.0]), 99.0)


class ShadowAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = recorder.Recorder(os.path.join(self.tmp.name, "t.db"))
        self.specs = [s for s in lib.build_library()
                      if s.template == "iron_condor" and s.regimes == ("LOW_VOL",) and s.dte_max == 3][:3]
        self.rs = RegimeState(Regime.LOW_VOL, 12.0, 0.5, 9.0, False, -2)

    def tearDown(self):
        self.rec.close()
        self.tmp.cleanup()

    def snap(self, t, spread_pct=None):
        chain = bt.model_chain(80000.0, 0.11, t.date() + dt.timedelta(days=2), t, BP, SP.risk_free_rate)
        if spread_pct is not None:                                       # attach two-sided depth around the model price
            from dataclasses import replace
            for k, q in list(chain.quotes.items()):
                h = q.ltp * spread_pct / 200
                chain.quotes[k] = replace(q, bid=q.ltp - h, ask=q.ltp + h)
        return Snapshot(t, 80000.0, 12.0, self.rs, chain, 0, True)

    def book(self):
        return ShadowBook(self.specs, self.rec, RiskParams(), Schedule(), CostParams())

    def test_entries_use_executable_prices_not_ltp(self):
        t = dt.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
        book = self.book()
        book.step(self.snap(t, spread_pct=4.0), t)
        self.assertTrue(book.open)
        pos = next(iter(book.open.values()))
        for sl in pos.legs:
            if sl.side == "BUY":
                self.assertGreater(sl.entry, sl.last)                    # paid the ask
            else:
                self.assertLess(sl.entry, sl.last)                       # sold at the bid

    def test_illiquid_structures_are_skipped_like_live(self):
        t = dt.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
        book = self.book()
        book.step(self.snap(t, spread_pct=40.0), t)                      # 40% spreads: live would refuse
        self.assertEqual(len(book.open), 0)

    def test_restart_does_not_reenter_the_same_variant_today(self):
        t = dt.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
        first = self.book()
        first.step(self.snap(t), t)
        entered = set(first.open)
        self.assertTrue(entered)
        second = self.book()                                             # bot restarted: in-memory state is gone
        self.assertTrue(entered <= second.done_today)
        second.step(self.snap(t), t)
        self.assertFalse(entered & set(second.open))

    def test_stale_feed_still_squares_off(self):
        t = dt.datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
        book = self.book()
        book.step(self.snap(t), t)
        self.assertTrue(book.open)
        book.flatten_if_due(t.replace(hour=14, minute=0))
        self.assertTrue(book.open)                                       # not yet square-off time
        book.flatten_if_due(t.replace(hour=15, minute=16))
        self.assertFalse(book.open)


class PriceGuardTests(unittest.TestCase):
    def test_zero_price_never_reaches_the_bot(self):
        import time
        from zerodha_client import ZerodhaClient
        c = object.__new__(ZerodhaClient)
        c._ticks, c._keys = {1: (0.0, time.monotonic())}, {1: "BSE:SENSEX"}
        c.quote = lambda keys: {keys[0]: {"last_price": 0}}
        with self.assertRaises(ValueError):
            c.get_ltp(1)
        c.quote = lambda keys: {keys[0]: {"last_price": 80123.5}}
        self.assertEqual(c.get_ltp(1), 80123.5)             # a bad stream tick falls back to REST


class ReviewFixTests(unittest.TestCase):
    def test_sheets_worker_survives_an_unserialisable_row(self):
        import time as _t
        import sheets_sink
        calls = []
        orig = sheets_sink.requests.post
        class R:                                              # pretend Google accepted the good batch
            status_code, text = 200, "ok"
        sheets_sink.requests.post = lambda *a, **k: (calls.append(1), R())[1]
        try:
            s = sheets_sink.SheetsSink("http://x.invalid/exec", "s")
            s.append("T", ["h"], [[object()]])               # would raise TypeError inside requests in real life
            s.append("T", ["h"], [["ok"]])
            for _ in range(50):
                if len(calls) >= 2:
                    break
                _t.sleep(0.05)
            self.assertGreaterEqual(len(calls), 2)
        finally:
            sheets_sink.requests.post = orig

    def test_library_builds_without_asserts(self):
        ids = [s.id for s in lib.build_library()]
        self.assertEqual(len(ids), len(set(ids)))


class IndependentReviewTests(unittest.TestCase):
    def test_mismatch_halt_does_not_trigger_a_blind_flatten(self):
        from risk_manager import RiskManager
        rm = RiskManager(RiskParams(), Schedule())
        rm.halt("POSITION_MISMATCH")
        self.assertIsNone(rm.update_portfolio(100000.0, 0.0))
        rm.halt("DAILY_LOSS_LIMIT")
        self.assertEqual(rm.update_portfolio(100000.0, 0.0), "DAILY_LOSS_LIMIT")

    def _bot(self, live=True):
        import types
        import main
        from execution import OrderLeg
        b = object.__new__(main.Bot)
        b.live, b._last_px = live, {}
        return b, OrderLeg, types

    def test_exit_quantity_is_clamped_to_what_the_broker_holds(self):
        import sim_harness as sh
        from option_strategies import STRATEGY_CLASSES
        import types
        b, OrderLeg, _ = self._bot()
        legs = sh.order_legs(sh.plan_from_strategy(STRATEGY_CLASSES["bull_call_spread"](), CHAIN_FOR_REVIEW), 1)
        closing = [OrderLeg(l.contract, "SELL" if l.side == "BUY" else "BUY", l.qty, l.ref_price) for l in legs]
        long_sym = next(l.contract.symbol for l in legs if l.side == "BUY")
        short_sym = next(l.contract.symbol for l in legs if l.side == "SELL")
        qty = legs[0].qty
        b.client = types.SimpleNamespace(net_positions=lambda: {long_sym: qty // 2, short_sym: -qty})
        out = {l.contract.symbol: (l.side, l.qty) for l in b._clamp_to_broker(closing)}
        self.assertEqual(out[long_sym], ("SELL", qty // 2))              # broker holds only half the long
        self.assertEqual(out[short_sym], ("BUY", qty))
        b.client = types.SimpleNamespace(net_positions=lambda: {})        # flat at the broker: send nothing
        self.assertEqual(b._clamp_to_broker(closing), [])

    def test_dead_quote_does_not_block_stops(self):
        import types
        import main
        from risk_manager import ManagedPosition, OpenLeg
        import sim_harness as sh
        from option_strategies import STRATEGY_CLASSES
        b, _, _ = self._bot()
        plan = sh.plan_from_strategy(STRATEGY_CLASSES["bull_call_spread"](), CHAIN_FOR_REVIEW)
        pos = ManagedPosition(plan, 1, [OpenLeg(l.contract, l.side, 20, l.ref_price) for l in plan.legs],
                              dt.datetime.now(IST), 1000.0, 1000.0)
        def ltp(tok):
            raise ValueError("no valid price")
        b.client = types.SimpleNamespace(get_ltp=ltp)
        got = b._ltps(pos)
        self.assertEqual(set(got), {l.contract.token for l in plan.legs})
        self.assertTrue(all(v > 0 for v in got.values()))

    def test_login_link_is_single_use_and_account_checked(self):
        import types
        import token_service as ts
        svc = object.__new__(ts.TokenService)
        import threading
        svc._lock, svc._used_nonces, svc._last_rt, svc._last_user = threading.Lock(), set(), None, ""
        svc.cfg = types.SimpleNamespace(credentials=types.SimpleNamespace(api_secret="s3cret"),
                                        paths=types.SimpleNamespace(token_file=os.path.join(tempfile.mkdtemp(), "t.json")))
        svc.alerts = types.SimpleNamespace(send=lambda *a, **k: None)
        svc.kite = types.SimpleNamespace(generate_session=lambda rt, api_secret: {"access_token": "AT", "user_id": "AB1234"})
        state = svc.make_state()
        os.environ["KITE_USER_ID"] = "ZZ9999"
        try:
            with self.assertRaises(PermissionError):
                svc.exchange("abcdefghij12", state)                       # wrong Zerodha account
            os.environ["KITE_USER_ID"] = "AB1234"
            self.assertEqual(svc.exchange("abcdefghij12", state), "AB1234")
            with self.assertRaises(PermissionError):
                svc.exchange("klmnopqrst34", state)                       # same link, different request token
        finally:
            os.environ.pop("KITE_USER_ID", None)

    def test_alert_log_redacts_login_state(self):
        import logging
        from alerts import Alerter
        with self.assertLogs("alerts", level="INFO") as cm:
            Alerter(None, None).send("Tap: https://kite.zerodha.com/connect/login?v=3&redirect_params=state%3D123xabcxdeadbeef")
        self.assertNotIn("deadbeef", "\n".join(cm.output))


import sim_harness as _sh
CHAIN_FOR_REVIEW = _sh.sample_chain()[0]


if __name__ == "__main__":
    unittest.main()
