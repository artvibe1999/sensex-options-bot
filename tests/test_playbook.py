import datetime as dt
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import IST  # noqa: E402
from pb_indicators import Candle, supertrend, to_15m  # noqa: E402
from playbook import (DayState, Playbook, PlaybookParams, TrendState, build_condor, build_credit,  # noqa: E402
                      build_single, single_strike, trading_days_left, trend_state)
from strategy_engine import Chain, OptionQuote, TradePlan  # noqa: E402
from telegram_control import TelegramControl, load_settings  # noqa: E402
from zerodha_client import OptionContract  # noqa: E402

D = dt.date(2026, 11, 2)          # Monday


def T(h, m, day=D):
    return dt.datetime.combine(day, dt.time(h, m), tzinfo=IST)


def ts(st2, st2_prev, st3, adx=30.0, end=T(10, 45)):
    return TrendState(end, st2, st2_prev, st3, adx)


def chain(spot=80000.0, expiry=dt.date(2026, 11, 5)):
    ch = Chain(expiry, spot, round(spot / 100) * 100, 100.0, 1.0)
    for k in range(int(spot) - 4000, int(spot) + 4001, 100):
        for kind in ("CE", "PE"):
            intrinsic = max(spot - k, 0) if kind == "CE" else max(k - spot, 0)
            px = intrinsic + max(400 - abs(k - spot) * 0.2, 5)
            c = OptionContract(k, f"SENSEX{k}{kind}", "BFO", float(k), expiry, kind, 20, 0.05)
            ch.quotes[(float(k), kind)] = OptionQuote(c, px, 0, None, None, 0, px - 0.5, px + 0.5)
    return ch


class TestSignals(unittest.TestCase):
    def setUp(self):
        self.pb = Playbook(PlaybookParams())

    def test_confirmed_flip_in_window_is_a_signal(self):
        d = self.pb.single_signal(ts(1, -1, 1), T(10, 46), 15.0)
        self.assertEqual((d.action, d.direction), ("single", 1))
        d = self.pb.single_signal(ts(-1, 1, -1), T(10, 46), 15.0)
        self.assertEqual((d.action, d.direction), ("single", -1))

    def test_flip_without_st3_agreement_is_ignored(self):
        self.assertEqual(self.pb.single_signal(ts(1, -1, -1), T(10, 46), 15.0).action, "none")

    def test_no_flip_is_ignored(self):
        self.assertEqual(self.pb.single_signal(ts(1, 1, 1), T(10, 46), 15.0).action, "none")

    def test_window_vix_and_staleness(self):
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1, end=T(10, 15)), T(10, 16), 15.0).action, "none")
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1, end=T(13, 15)), T(13, 16), 15.0).action, "none")
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1), T(10, 46), 13.0).action, "none")      # VIX < 14
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1), T(10, 55), 15.0).action, "none")      # stale bar

    def test_one_entry_per_bar_and_two_per_day(self):
        d = self.pb.single_signal(ts(1, -1, 1), T(10, 46), 15.0)
        self.pb.took_single(d)
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1), T(10, 47), 15.0).action, "none")
        d = self.pb.single_signal(ts(-1, 1, -1, end=T(11, 30)), T(11, 31), 15.0)
        self.pb.took_single(d)
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1, end=T(12, 0)), T(12, 1), 15.0).action, "none")
        # a new day resets the count
        self.assertEqual(self.pb.single_signal(ts(1, -1, 1, end=T(10, 45, D + dt.timedelta(days=1))),
                                               T(10, 46, D + dt.timedelta(days=1)), 15.0).action, "single")

    def test_condor_on_no_trend_days_at_any_vix(self):
        d = self.pb.range_signal(ts(1, 1, 1, adx=18), T(10, 5), 16.0)
        self.assertEqual((d.action, d.short_pct), ("condor", 0.008))                                  # VIX >= 14
        d = self.pb.range_signal(ts(1, 1, 1, adx=18), T(10, 5), 11.0)
        self.assertEqual((d.action, d.short_pct), ("condor", 0.006))                                  # low VIX: closer
        self.assertEqual(self.pb.range_signal(ts(1, 1, 1, adx=27), T(10, 5), 16.0).action, "none")    # ADX 25-30
        self.assertEqual(self.pb.range_signal(ts(1, 1, 1, adx=18), T(12, 30), 16.0).action, "none")   # late for condor
        self.pb.took_single(self.pb.single_signal(ts(1, -1, 1), T(10, 46), 15.0))
        self.assertEqual(self.pb.range_signal(ts(1, 1, 1, adx=18), T(11, 0), 16.0).action, "none")    # single taken

    def test_trend_credit_on_strong_trend_without_flip(self):
        up = TrendState(T(10, 45), 1, 1, 1, 34.0, 30.0, 12.0)
        d = self.pb.range_signal(up, T(10, 46), 12.0)
        self.assertEqual((d.action, d.direction), ("credit", 1))
        dn = TrendState(T(10, 45), -1, -1, -1, 34.0, 10.0, 28.0)
        self.assertEqual(self.pb.range_signal(dn, T(10, 46), 12.0).direction, -1)
        mixed = TrendState(T(10, 45), 1, 1, -1, 34.0, 30.0, 12.0)                                      # STs disagree
        self.assertEqual(self.pb.range_signal(mixed, T(10, 46), 12.0).action, "none")
        self.assertEqual(self.pb.range_signal(TrendState(T(10, 0), 1, 1, 1, 34.0, 30.0, 12.0), T(10, 15), 12.0).action,
                         "none")                                                                       # before 10:30
        self.assertEqual(self.pb.range_signal(TrendState(T(10, 45), 1, 1, 1, 28.0, 30.0, 12.0), T(10, 46), 12.0).action,
                         "none")                                                                       # ADX < 30

    def test_one_range_trade_per_day(self):
        self.pb.range_signal(ts(1, 1, 1, adx=18), T(10, 5), 16.0)
        self.pb.took_range()
        self.assertEqual(self.pb.range_signal(ts(1, 1, 1, adx=18), T(10, 6), 16.0).action, "none")
        self.assertEqual(self.pb.range_signal(TrendState(T(11, 0), 1, 1, 1, 34.0, 30.0, 12.0), T(11, 1), 16.0).action,
                         "none")


class TestExits(unittest.TestCase):
    def setUp(self):
        self.pb = Playbook(PlaybookParams())
        self.long = build_single(chain(), 1, T(10, 46), "test")
        self.condor = build_condor(chain(), 0.008, 0.017, "test")
        self.credit = build_credit(chain(), 1, 0.010, 0.012, "test")

    def test_no_stop_or_trail_while_trend_continues(self):
        for pnl in (-1499, -800, 0, 5000, 20000):
            self.assertIsNone(self.pb.exit_reason(self.long, pnl, ts(1, 1, 1), False))

    def test_reversal_and_loss_cap(self):
        self.assertEqual(self.pb.exit_reason(self.long, 2000, ts(-1, 1, 1), False), "TREND_REVERSAL")
        self.assertEqual(self.pb.exit_reason(self.long, -1500, ts(1, 1, 1), False), "LOSS_CAP_1500")
        self.assertEqual(self.pb.exit_reason(self.condor, -1600, None, False), "LOSS_CAP_1500")

    def test_reversal_bar_is_not_reused_as_entry(self):
        rev = ts(-1, 1, -1, end=T(11, 0))
        self.assertEqual(self.pb.exit_reason(self.long, -200, rev, False), "TREND_REVERSAL")
        self.assertEqual(self.pb.single_signal(rev, T(11, 1), 15.0).action, "none")

    def test_condor_yields_to_trend_signal(self):
        self.assertEqual(self.pb.exit_reason(self.condor, 300, ts(1, 1, 1), True), "TREND_SIGNAL_PRIORITY")
        self.assertIsNone(self.pb.exit_reason(self.condor, 300, ts(1, 1, 1), False))

    def test_credit_exits_on_reversal_and_yields_to_single(self):
        self.assertIsNone(self.pb.exit_reason(self.credit, -900, ts(1, 1, 1), False))
        rev = ts(-1, 1, -1, end=T(11, 30))
        self.assertEqual(self.pb.exit_reason(self.credit, 100, rev, False), "TREND_REVERSAL")
        # a credit-spread reversal must NOT block the single-leg entry on that same bar
        self.assertEqual(self.pb.single_signal(rev, T(11, 31), 15.0).action, "single")
        self.assertEqual(self.pb.exit_reason(self.credit, 100, ts(1, 1, 1), True), "TREND_SIGNAL_PRIORITY")
        self.assertEqual(self.pb.exit_reason(self.credit, -1500, ts(1, 1, 1), False), "LOSS_CAP_1500")

    def test_single_never_pre_empted(self):
        self.assertIsNone(self.pb.exit_reason(self.long, 100, ts(1, 1, 1), True))

    def test_cap_can_be_switched_off(self):
        pb = Playbook(PlaybookParams(loss_cap_rs=0))
        self.assertIsNone(pb.exit_reason(self.long, -9000, ts(1, 1, 1), False))


class TestPlans(unittest.TestCase):
    def test_strike_moves_itm_as_expiry_nears(self):
        self.assertEqual(single_strike(80020, 1, 3, dt.time(11), 100), 80000)
        self.assertEqual(single_strike(80020, 1, 2, dt.time(11), 100), 79800)
        self.assertEqual(single_strike(80020, 1, 1, dt.time(11), 100), 79600)
        self.assertEqual(single_strike(80020, -1, 1, dt.time(11), 100), 80400)
        self.assertEqual(trading_days_left(dt.date(2026, 10, 30), dt.date(2026, 11, 5)), 4)   # Fri -> Thu

    def test_single_plan(self):
        p = build_single(chain(), -1, T(10, 46), "why")
        self.assertEqual(len(p.legs), 1)
        self.assertEqual((p.legs[0].side, p.legs[0].contract.kind), ("BUY", "PE"))
        self.assertTrue(p.variant_id.endswith("_DOWN"))
        self.assertIsNone(p.sl_frac)

    def test_credit_plan_sells_away_from_trend(self):
        up = build_credit(chain(), 1, 0.010, 0.012, "why")
        legs = {l.side: (l.contract.kind, l.contract.strike) for l in up.legs}
        self.assertEqual(legs, {"SELL": ("PE", 79200.0), "BUY": ("PE", 78200.0)})      # 1% = 800, 1.2% = 1000 (rounded)
        self.assertTrue(up.variant_id.endswith("_UP"))
        dn = build_credit(chain(), -1, 0.010, 0.012, "why")
        legs = {l.side: (l.contract.kind, l.contract.strike) for l in dn.legs}
        self.assertEqual(legs, {"SELL": ("CE", 80800.0), "BUY": ("CE", 81800.0)})
        self.assertGreater(up.max_profit_unit, 0)

    def test_condor_plan_is_defined_risk(self):
        p = build_condor(chain(), 0.008, 0.017, "why")
        sides = sorted((l.side, l.contract.kind, l.contract.strike) for l in p.legs)
        self.assertEqual([s[0] for s in sides], ["BUY", "BUY", "SELL", "SELL"])
        shorts = {l.contract.kind: l.contract.strike for l in p.legs if l.side == "SELL"}
        longs = {l.contract.kind: l.contract.strike for l in p.legs if l.side == "BUY"}
        self.assertEqual((shorts["CE"], shorts["PE"]), (80600.0, 79400.0))           # 0.8% of 80000 = 600
        self.assertEqual((longs["CE"], longs["PE"]), (82000.0, 78000.0))             # wings 1.7% = 1400 further
        self.assertGreater(p.max_profit_unit, 0)


class TestTrendState(unittest.TestCase):
    def _bars(self, closes, day=D):
        out, t = [], T(9, 15, day)
        for i, c in enumerate(closes):
            out.append(Candle(t + dt.timedelta(minutes=5 * i), c - 1, c + 20, c - 20, c))
        return out

    def test_needs_warmup_and_only_completed_bars(self):
        self.assertIsNone(trend_state(self._bars([80000.0] * 30), T(12, 0)))
        bars = self._bars([80000 + 10 * i for i in range(150)])
        st = trend_state(bars, bars[-1].ts + dt.timedelta(minutes=5))
        self.assertIsNotNone(st)
        self.assertEqual(st.st2, 1)
        # the last (incomplete) 15m bucket must not be used: same state one bar earlier inside the bucket
        st_mid = trend_state(bars, bars[-1].ts + dt.timedelta(minutes=2))
        self.assertLessEqual(st_mid.bar_end, bars[-1].ts + dt.timedelta(minutes=2))

    def test_to_15m_alignment(self):
        b = to_15m(self._bars([1.0, 2.0, 3.0, 4.0]))
        self.assertEqual([x.ts.time() for x in b], [dt.time(9, 15), dt.time(9, 30)])
        self.assertEqual(b[0].close, 3.0)

    def test_supertrend_flips(self):
        up = [100 + i for i in range(40)]
        dn = [140 - 3 * i for i in range(40)]
        c = up + dn
        d, _ = supertrend([x + 1 for x in c], [x - 1 for x in c], c, 10, 2.0)
        self.assertEqual(d[39], 1)
        self.assertEqual(d[-1], -1)


class TestBotWiring(unittest.TestCase):
    """Bot-level playbook wiring (review fixes): stale part-bars, quote retries, pre-emption only when tradable."""

    def _bot(self, hist, fetched, lots_fit=2):
        import types

        import main
        from config import RiskParams, Schedule
        from risk_manager import RiskManager
        b = object.__new__(main.Bot)
        b.md = types.SimpleNamespace(completed_5m=lambda: (hist, fetched))
        b.pb = Playbook(PlaybookParams())
        b._pb_cache = (None, None)
        b._pb_ready_cache = (None, -1e18, False)
        b.tg = types.SimpleNamespace(single_lots=2, multi_lots=2)
        b._pb_lots = lambda plan, wanted: min(wanted, lots_fit)
        b.risk = RiskManager(RiskParams(), Schedule())
        b._entry_block_until = 0.0
        b.position = None
        b.entered = []
        b.alerts = types.SimpleNamespace(send=lambda *a, **k: b.entered.append(("alert", a[0])))
        b._save_state = lambda: None
        b.cfg = types.SimpleNamespace(schedule=Schedule())

        def enter(now, snap, plan, lots, score, n):
            b.entered.append(("enter", plan.variant_id, lots))
            b.position = types.SimpleNamespace(plan=plan)
        b._enter = enter
        return b

    def test_trend_uses_only_bars_closed_at_fetch_time(self):
        bars, k = [], 0
        for day in (D - dt.timedelta(days=3), D):     # Friday + Monday: two sessions of 5-minute candles
            for i in range(75 if day != D else 30):
                c = 80000.0 + 10 * k
                bars.append({"date": T(9, 15, day) + dt.timedelta(minutes=5 * i), "open": c, "high": c + 20,
                             "low": c - 20, "close": c})
                k += 1
        fetched = bars[-1]["date"] + dt.timedelta(minutes=2)          # last candle still forming at fetch time
        b = self._bot(bars, fetched)
        # a later clock must not turn the stale last candle into a "completed" bar
        later = bars[-1]["date"] + dt.timedelta(minutes=5, seconds=2)
        st_later = b._pb_trend(later)
        st_fetch = trend_state([Candle(x["date"], x["open"], x["high"], x["low"], x["close"]) for x in bars], fetched)
        self.assertEqual(st_later, st_fetch)
        self.assertLessEqual(st_later.bar_end, fetched)
        b2 = self._bot([], None)
        self.assertIsNone(b2._pb_trend(later))                        # no history: no signal (fail closed)

    def _snap(self):
        import types
        return types.SimpleNamespace(fresh=True, chain=chain(), vix=16.0)

    def test_single_signal_retried_while_quote_missing(self):
        b = self._bot([], None)
        sig = ts(1, -1, 1, end=T(10, 45))
        b._pb_trend = lambda now: sig
        snap = self._snap()
        ok_plan = build_single(snap.chain, 1, T(10, 45, D), "x")
        b._pb_single_ready = lambda now, s, d: (None, 0, "quote: strike not quoted")
        b._try_playbook(T(10, 45, D) + dt.timedelta(seconds=10), snap)
        self.assertEqual(b.entered, [])                               # no alert, signal still live
        b._pb_single_ready = lambda now, s, d: (ok_plan, 2, "")
        b._try_playbook(T(10, 45, D) + dt.timedelta(seconds=20), snap)
        self.assertEqual(b.entered, [("enter", "pb_single_UP", 2)])

    def test_single_signal_given_up_after_retry_window(self):
        b = self._bot([], None)
        sig = ts(1, -1, 1, end=T(10, 45))
        b._pb_trend = lambda now: sig
        b._pb_single_ready = lambda now, s, d: (None, 0, "quote: strike not quoted")
        b._try_playbook(T(10, 46, D) + dt.timedelta(seconds=30), self._snap())     # 90 s after the bar
        self.assertEqual([e[0] for e in b.entered], ["alert"])
        self.assertEqual(b.pb.day.last_signal_bar, sig.bar_end.isoformat())

    def test_margin_refusal_is_not_retried(self):
        b = self._bot([], None)
        sig = ts(1, -1, 1, end=T(10, 45))
        b._pb_trend = lambda now: sig
        b._pb_single_ready = lambda now, s, d: (None, 0, "not even 1 lot fits the margin")
        b._try_playbook(T(10, 45, D) + dt.timedelta(seconds=10), self._snap())
        self.assertEqual([e[0] for e in b.entered], ["alert"])

    def test_condor_not_closed_for_an_untradable_trend_signal(self):
        import types
        b = self._bot([], None)
        condor = build_condor(chain(), 0.008, 0.017, "x")
        pos = types.SimpleNamespace(plan=condor, peak_pnl=0.0, pnl=lambda ltps: 200.0)
        ages = []
        b._ltps = lambda p, max_age_s=5.0: ages.append(max_age_s) or {}
        b._last_snap = self._snap()
        b.capital, b.realized = 40000.0, 0.0
        b._last_exit_status = b._last_state_save = 1e18
        exits = []
        b._exit = exits.append
        b.cfg = types.SimpleNamespace(schedule=b.cfg.schedule, execution=types.SimpleNamespace(product="MIS"))
        sig = ts(1, -1, 1, end=T(10, 45))
        b._pb_trend = lambda now: sig
        now = T(10, 45, D) + dt.timedelta(seconds=10)
        calls = []

        def not_ready(n, s, d):
            calls.append(1)
            return None, 0, "not even 1 lot fits the margin"
        b._pb_single_ready = not_ready
        b._manage_playbook(now, pos)
        b._manage_playbook(now, pos)                       # within the re-check interval: cached, no new margin query
        self.assertEqual(exits, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(ages[0], b.PB_TICK_MAX_AGE_S)     # quiet wings are priced from their last tick, not REST
        b._pb_ready_cache = (None, -1e18, False)           # re-check due
        b._pb_single_ready = lambda n, s, d: (build_single(chain(), 1, now, "x"), 2, "")
        b._manage_playbook(now, pos)
        self.assertEqual(exits, ["TREND_SIGNAL_PRIORITY"])


class TestTelegram(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "settings.json")
        self.tg = TelegramControl("tok", "123", self.path, reply=lambda t: None)

    def test_default_two_lots(self):
        self.assertEqual(self.tg.single_lots, 2)

    def test_change_persists(self):
        self.assertIn("Single-leg lots: 3", self.tg.handle("123", "/lots 3"))
        self.assertEqual(self.tg.single_lots, 3)
        self.tg.handle("123", "/mlots 4")
        s = load_settings(self.path)
        self.assertEqual((s.single_lots, s.multi_lots), (3, 4))
        self.assertEqual(TelegramControl("tok", "123", self.path, reply=lambda t: None).single_lots, 3)

    def test_rejects_other_chats_and_bad_values(self):
        self.assertIsNone(self.tg.handle("999", "/lots 9"))
        self.assertEqual(self.tg.single_lots, 2)
        self.assertIn("between", self.tg.handle("123", "/lots 0"))
        self.assertIn("Usage", self.tg.handle("123", "/lots x"))
        self.assertIn("between", self.tg.handle("123", "/lots 50"))
        self.assertEqual(self.tg.single_lots, 2)
        self.assertIsNone(self.tg.handle("123", "hello"))

    def test_daystate_roundtrip(self):
        s = DayState(D, 1, True, "x")
        r = DayState.restore(s.export())
        self.assertEqual((r.day, r.singles, r.multi_done, r.last_signal_bar), (D, 1, True, "x"))


if __name__ == "__main__":
    unittest.main()
