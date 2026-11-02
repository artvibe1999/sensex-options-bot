"""Multi-index engine: rate limits, circuit breaker, day lock, mode switching, payload validation, index feeds,
candidate selection and the bot's multi-index entry path."""
import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import IST, RiskParams, Schedule  # noqa: E402
from day_guard import DayGuard  # noqa: E402
from indices import EXPIRY_WEEK_DAYS, INDICES  # noqa: E402
from mode_control import read_mode, startup_mode, write_mode  # noqa: E402
from models import Candle, OptionRow, parse_rows  # noqa: E402
from multi_index import IndexFeed, MultiIndex, two_sided  # noqa: E402
from playbook import PlaybookParams, TrendState  # noqa: E402
from rate_limit import CircuitBreaker, CircuitOpen, TokenBucket, backoff_s, endpoint_for  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from telegram_control import TelegramControl  # noqa: E402
from zerodha_client import OptionContract  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


# ------------------------------------------------------------------ rate limits / breaker
class RateLimitTests(unittest.TestCase):
    def test_bucket_paces_to_its_rate(self):
        c = FakeClock()
        b = TokenBucket(1.0, 1.0, clock=c, sleep=c.sleep)
        self.assertEqual(b.acquire(), 0.0)                  # the stored token
        self.assertAlmostEqual(b.acquire(), 1.0)            # then 1 per second
        c.t += 5.0
        self.assertEqual(b.acquire(), 0.0)                  # refilled while idle, capped at the burst
        self.assertAlmostEqual(b.acquire(), 1.0)

    def test_burst_allows_back_to_back_calls(self):
        c = FakeClock()
        b = TokenBucket(3.0, 3.0, clock=c, sleep=c.sleep)
        self.assertEqual([b.acquire() for _ in range(3)], [0.0, 0.0, 0.0])
        self.assertAlmostEqual(b.acquire(), 1 / 3)

    def test_endpoint_classes(self):
        def quote(): pass
        def historical_data(): pass
        def place_order(): pass
        def margins(): pass
        self.assertEqual([endpoint_for(f) for f in (quote, historical_data, place_order, margins)],
                         ["quote", "historical", "order", "default"])

    def test_breaker_opens_then_half_opens_and_closes(self):
        c = FakeClock()
        br = CircuitBreaker(threshold=3, cooldown_s=30, clock=c, jitter=lambda: 0.0)
        for _ in range(3):
            br.before()
            br.failure()
        self.assertEqual(br.state, br.OPEN)
        with self.assertRaises(CircuitOpen):
            br.before()
        self.assertIsInstance(CircuitOpen("x"), ConnectionError)
        c.t += 31
        br.before()                                         # the probe goes through
        self.assertEqual(br.state, br.HALF_OPEN)
        with self.assertRaises(CircuitOpen):
            br.before()                                     # only one probe at a time
        br.success()
        self.assertEqual(br.state, br.CLOSED)
        br.before()

    def test_failed_probe_reopens_for_longer(self):
        c = FakeClock()
        br = CircuitBreaker(threshold=1, cooldown_s=10, clock=c, jitter=lambda: 0.0)
        br.before()
        br.failure()
        c.t += 11
        br.before()
        br.failure()                                        # probe failed: 20 s now
        c.t += 15
        with self.assertRaises(CircuitOpen):
            br.before()
        c.t += 6
        br.before()

    def test_backoff_grows_and_is_capped(self):
        vals = [backoff_s(a, jitter=lambda: 0.0) for a in range(8)]
        self.assertEqual(vals[:5], [1, 2, 4, 8, 16])
        self.assertEqual(max(vals), 20)
        self.assertLessEqual(backoff_s(3, jitter=lambda: 1.0), 8 * 1.25)


# ------------------------------------------------------------------ day guard
class DayGuardTests(unittest.TestCase):
    D = dt.date(2026, 11, 3)

    def test_locks_after_first_win_not_after_a_loss(self):
        g = DayGuard()
        self.assertTrue(g.allows_entry(self.D))
        self.assertFalse(g.on_trade_closed(self.D, -900))
        self.assertTrue(g.allows_entry(self.D))
        self.assertTrue(g.on_trade_closed(self.D, 450))
        self.assertFalse(g.allows_entry(self.D))
        self.assertFalse(g.on_trade_closed(self.D, 300))    # already locked: only one "locked" event
        self.assertEqual((g.closed, g.wins), (3, 2))

    def test_a_new_day_unlocks(self):
        g = DayGuard()
        g.on_trade_closed(self.D, 10)
        self.assertTrue(g.allows_entry(self.D + dt.timedelta(days=1)))

    def test_breakeven_is_not_a_win(self):
        g = DayGuard()
        g.on_trade_closed(self.D, 0.0)
        self.assertTrue(g.allows_entry(self.D))

    def test_restart_keeps_the_lock(self):
        g = DayGuard()
        g.on_trade_closed(self.D, 10)
        g2 = DayGuard.restore(json.loads(json.dumps(g.export())))
        self.assertFalse(g2.allows_entry(self.D))
        self.assertTrue(DayGuard.restore({"day": "garbage"}).allows_entry(self.D))

    def test_disabled_never_locks(self):
        g = DayGuard(enabled=False)
        g.on_trade_closed(self.D, 10)
        self.assertTrue(g.allows_entry(self.D))


# ------------------------------------------------------------------ mode switching
class ModeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "mode.json")

    def test_default_and_round_trip(self):
        self.assertEqual(read_mode(self.path), "PAPER")
        write_mode(self.path, "LIVE")
        self.assertEqual(read_mode(self.path), "LIVE")
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(read_mode(self.path), "PAPER")       # unreadable -> PAPER
        with self.assertRaises(ValueError):
            write_mode(self.path, "YOLO")

    def test_live_needs_the_master_switch(self):
        write_mode(self.path, "LIVE")
        mode, note = startup_mode(self.path, {})
        self.assertEqual(mode, "PAPER")
        self.assertIn("LIVE_TRADING_CONFIRM", note)
        self.assertEqual(startup_mode(self.path, {"LIVE_TRADING_CONFIRM": "YES"}), ("LIVE", ""))
        self.assertEqual(startup_mode(self.path, {"LIVE_TRADING_CONFIRM": "yes"})[0], "PAPER")   # exact YES only


class TelegramMultiTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.switched = []
        self.mode = "PAPER"
        self.tg = TelegramControl("tok", "123", os.path.join(self.dir, "s.json"), reply=lambda t: None,
                                  index_defaults={"NIFTY": 1, "BANKNIFTY": 2}, mode_now=lambda: self.mode,
                                  mode_fn=lambda m: self.switched.append(m) or f"switched {m}", admin_ids={"777"})

    def test_index_lots(self):
        self.assertIn("NIFTY 1", self.tg.handle("123", "/lots"))
        self.assertIn("NIFTY 3", self.tg.handle("123", "/lots nifty 3"))
        self.assertEqual(self.tg.lots_for("NIFTY", "condor"), 3)
        self.assertEqual(self.tg.lots_for("BANKNIFTY", "credit"), 2)
        self.assertIn("Unknown index", self.tg.handle("123", "/lots FOO 1"))
        self.tg.handle("123", "/lots SENSEX 4")
        self.assertEqual((self.tg.lots_for("SENSEX", "single"), self.tg.lots_for("SENSEX", "condor")), (4, 4))
        again = TelegramControl("tok", "123", os.path.join(self.dir, "s.json"), reply=lambda t: None,
                                index_defaults={"NIFTY": 1, "BANKNIFTY": 2})
        self.assertEqual(again.lots_for("NIFTY", "single"), 3)                # persisted

    def test_mode_switch_needs_owner_and_the_code(self):
        self.assertIn("Only the account owner", self.tg.handle("123", "/mode live", from_id="555"))   # group member
        self.assertIn("Only the account owner", self.tg.handle("123", "/mode live"))                   # no sender
        msg = self.tg.handle("123", "/mode live", from_id="123")
        self.assertIn("REAL orders", msg)
        code = msg.split("/confirm ")[1].split()[0]
        self.assertIn("No matching", self.tg.handle("123", "/confirm 000000" if code != "000000" else "/confirm 1",
                                                    from_id="123"))
        self.assertEqual(self.switched, [])                 # a wrong code also cancels the request
        msg = self.tg.handle("123", "/mode live", from_id="777")             # a listed admin
        code = msg.split("/confirm ")[1].split()[0]
        self.assertEqual(self.tg.handle("123", f"/confirm {code}", from_id="777"), "switched LIVE")
        self.assertEqual(self.switched, ["LIVE"])
        self.assertIn("No matching", self.tg.handle("123", f"/confirm {code}", from_id="777"))   # one use only

    def test_expired_code_and_same_mode(self):
        import telegram_control
        self.assertIn("Already in PAPER", self.tg.handle("123", "/mode paper", from_id="123"))
        msg = self.tg.handle("123", "/mode live", from_id="123")
        code = msg.split("/confirm ")[1].split()[0]
        old = telegram_control.CONFIRM_TTL_S
        try:
            self.tg._pending = (self.tg._pending[0], self.tg._pending[1], 0.0)    # already expired
            self.assertIn("No matching", self.tg.handle("123", f"/confirm {code}", from_id="123"))
        finally:
            telegram_control.CONFIRM_TTL_S = old
        self.assertIsNone(self.tg.handle("999", "/mode live", from_id="999"))   # other chats: ignored entirely


# ------------------------------------------------------------------ payload validation
class ModelTests(unittest.TestCase):
    ROW = {"instrument_token": 11, "tradingsymbol": "NIFTY26N0923400CE", "name": "NIFTY", "exchange": "NFO",
           "instrument_type": "CE", "strike": 23400.0, "expiry": dt.date(2026, 11, 9), "lot_size": 65,
           "tick_size": 0.05, "segment": "NFO-OPT"}

    def test_valid_and_invalid_option_rows(self):
        bad = [dict(self.ROW, lot_size=0), dict(self.ROW, instrument_type="FUT"), dict(self.ROW, strike=-1),
               dict(self.ROW, tradingsymbol="nifty; rm -rf"), {k: v for k, v in self.ROW.items() if k != "expiry"}]
        rows = parse_rows(OptionRow, [self.ROW] + bad, "test")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].lot_size, 65)

    def test_candle_high_low_must_bracket(self):
        t = dt.datetime(2026, 11, 3, 10, 0, tzinfo=IST)
        ok = {"date": t, "open": 10, "high": 12, "low": 9, "close": 11}
        rows = parse_rows(Candle, [ok, dict(ok, high=10.5), dict(ok, close=0)], "test")
        self.assertEqual(len(rows), 1)


# ------------------------------------------------------------------ registry
class RegistryTests(unittest.TestCase):
    def test_six_indices_with_backtested_setups(self):
        self.assertEqual(set(INDICES), {"SENSEX", "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"})
        self.assertEqual(INDICES["SENSEX"].setups, {"single", "condor", "credit"})
        self.assertEqual(INDICES["NIFTY"].setups, {"single", "condor"})
        self.assertEqual(INDICES["BANKNIFTY"].setups, {"credit"})
        self.assertEqual(INDICES["MIDCPNIFTY"].setups, {"condor"})
        self.assertEqual(INDICES["FINNIFTY"].setups, frozenset())
        self.assertEqual(INDICES["NIFTYNXT50"].setups, frozenset())
        self.assertEqual({n: s.default_lots for n, s in INDICES.items()},
                         {"SENSEX": 2, "NIFTY": 1, "FINNIFTY": 1, "MIDCPNIFTY": 1, "BANKNIFTY": 2, "NIFTYNXT50": 2})
        self.assertTrue(all(s.monthly_only for n, s in INDICES.items() if n not in ("SENSEX", "NIFTY")))


# ------------------------------------------------------------------ feeds and candidates
NOW = dt.datetime(2026, 11, 3, 11, 0, 5, tzinfo=IST)


class FakeClient:
    def __init__(self, specs):
        self.contracts, self.flow, self.spots, self.full, self.tok = {}, {}, {}, {}, 10_000
        for name, (exp, spot, step, lot) in specs.items():
            cs = []
            for k in range(-15, 16):
                for kind in ("CE", "PE"):
                    self.tok += 1
                    strike = round(spot / step) * step + k * step
                    sym = f"{name}X{int(strike)}{kind}"
                    cs.append(OptionContract(self.tok, sym, "NFO", strike, exp, kind, lot, 0.05))
            self.contracts[name] = cs
            self.spots[name] = spot

    def index_contracts(self, name, exchange):
        return self.contracts.get(name, [])

    def index_token(self, exchange, symbol):
        for name, spec in INDICES.items():
            if spec.spot_symbol == symbol:
                return hash(name) % 1000 + 1
        raise RuntimeError(symbol)

    def subscribe_tokens(self, m):
        pass

    def subscribe_full(self, m):
        self.full.update(m)

    def tick_ltp(self, token, max_age_s=5.0):
        for name in INDICES:
            if hash(name) % 1000 + 1 == token:
                return self.spots.get(name)
        return None

    def flow_snapshot(self, max_age_s=15.0):
        return self.flow

    def historical(self, *a):
        return []

    def quote_all(self, name, bid_ask=lambda px: (px - 0.5, px + 0.5)):
        for c in self.contracts[name]:
            px = max(1.0, 200 - abs(c.strike - self.spots[name]) / 5)
            b, a = bid_ask(px)
            self.flow[c.token] = {"ltp": px, "bid": b, "ask": a, "oi": 100, "volume": 1000, "mono": 0}


def up_trend():
    return TrendState(NOW - dt.timedelta(minutes=1), st2=1, st2_prev=-1, st3=1, adx=32.0, pdi=30.0, mdi=10.0)


class FeedTests(unittest.TestCase):
    def setUp(self):
        weekly = dt.date(2026, 11, 9)
        monthly = dt.date(2026, 11, 24)
        self.client = FakeClient({"NIFTY": (weekly, 23440.0, 50.0, 65), "BANKNIFTY": (monthly, 55000.0, 100.0, 30),
                                  "SENSEX": (dt.date(2026, 11, 5), 75690.0, 100.0, 20)})
        specs = {k: INDICES[k] for k in ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY")}
        self.mi = MultiIndex(self.client, PlaybookParams(), specs)
        self.mi.load()

    def test_monthly_index_trades_only_in_its_expiry_week(self):
        f = self.mi.feed("BANKNIFTY")
        self.assertIsNone(f.expiry(dt.date(2026, 11, 3)))                     # 15 trading days left
        self.assertEqual(f.expiry(dt.date(2026, 11, 18)), dt.date(2026, 11, 24))   # 4 left
        self.assertEqual(EXPIRY_WEEK_DAYS, 4)
        n = self.mi.feed("NIFTY")
        self.assertEqual(n.expiry(dt.date(2026, 11, 3)), dt.date(2026, 11, 9))    # weekly: always
        self.assertEqual(n.expiry(dt.date(2026, 11, 9)), None)                    # expiry day: no later contract here

    def test_window_is_streamed_in_full_mode(self):
        self.mi.refresh(NOW)
        nifty = [c for c in self.client.contracts["NIFTY"] if c.token in self.client.full]
        self.assertEqual(len(nifty), 2 * (2 * IndexFeed.WINDOW + 1))
        bank = [c for c in self.client.contracts["BANKNIFTY"] if c.token in self.client.full]
        self.assertFalse(bank)                                                    # not its expiry week

    def test_single_signal_becomes_a_ranked_candidate(self):
        for name in ("NIFTY", "SENSEX"):
            self.client.quote_all(name)
            self.mi.feed(name).trend = lambda now: up_trend()
        cands = self.mi.candidates(NOW, 16.0)
        self.assertEqual([(c.index, c.setup) for c in cands], [("SENSEX", "single"), ("NIFTY", "single")])  # edge
        c = cands[1]
        self.assertEqual(c.plan.underlying, "NIFTY")
        self.assertIn("[PLAYBOOK NIFTY]", c.plan.rationale)
        self.assertEqual(c.plan.legs[0].contract.kind, "CE")
        self.assertEqual(c.plan.legs[0].contract.lot_size, 65)
        self.assertEqual(self.mi.why_not["FINNIFTY"], "no backtested setup" if self.mi.feed("FINNIFTY").ready
                         else "not loaded")

    def test_one_sided_or_wide_quotes_are_refused(self):
        self.client.quote_all("NIFTY", bid_ask=lambda px: (None, px + 0.5))
        self.mi.feed("NIFTY").trend = lambda now: up_trend()
        self.assertEqual(self.mi.candidates(NOW, 16.0), [])
        self.assertIn("two-sided", self.mi.why_not["NIFTY"])
        self.client.quote_all("NIFTY", bid_ask=lambda px: (px * 0.9, px * 1.1))      # ~20% wide
        self.assertEqual(self.mi.candidates(NOW, 16.0), [])
        self.assertIn("spread", self.mi.why_not["NIFTY"])

    def test_low_vix_or_no_trend_gives_no_single(self):
        self.client.quote_all("NIFTY")
        self.mi.feed("NIFTY").trend = lambda now: up_trend()
        self.assertEqual([c for c in self.mi.candidates(NOW, 12.0) if c.setup == "single"], [])   # VIX < 14

    def test_bid_ask_maps_by_token_not_prefix(self):
        self.client.quote_all("NIFTY")
        c = self.client.contracts["NIFTY"][0]
        self.assertIsNotNone(self.mi.bid_ask(c))
        other = OptionContract(999999, "NIFTYNXT50X1CE", "NFO", 1.0, dt.date(2026, 11, 24), "CE", 25, 0.05)
        self.assertIsNone(self.mi.bid_ask(other))

    def test_two_sided_helper(self):
        self.client.quote_all("NIFTY")
        f = self.mi.feed("NIFTY")
        ch = f.chain(NOW)
        from playbook import build_single
        plan = build_single(ch, 1, NOW, "t", itm_pct=(0.0027, 0.0053))
        self.assertIsNone(two_sided(plan, ch, 3.0))


# ------------------------------------------------------------------ bot entry path
def bare_bot(tmpdir, cands):
    import main
    b = object.__new__(main.Bot)
    b.live, b.mode, b.position = False, "PAPER", None
    b.cfg = types.SimpleNamespace(schedule=Schedule(),
                                  paths=types.SimpleNamespace(state=os.path.join(tmpdir, "state.json")))
    b.risk = RiskManager(RiskParams(), Schedule())
    b.guard = DayGuard()
    b.trades_today, b._entry_block_until = 0, 0.0
    b.entered = []
    feeds = {}

    class PB:
        def __init__(self):
            self.calls = []
        def took_single(self, d): self.calls.append("took_single")
        def took_range(self): self.calls.append("took_range")
        def skipped_signal(self, d): self.calls.append("skipped")
    for c in cands:
        feeds.setdefault(c.index, types.SimpleNamespace(pb=PB(), chain=lambda now: None))
    b.mi = types.SimpleNamespace(candidates=lambda now, vix: list(cands), feed=lambda n: feeds[n], feeds=feeds,
                                 why_not={})
    b.tg = types.SimpleNamespace(lots_for=lambda i, s: 1)
    b._pb_lots = lambda plan, wanted: wanted
    b.alerts = types.SimpleNamespace(send=lambda *a, **k: None)
    b._save_state = lambda: None

    def enter(now, snap, plan, lots, score, n, chain=None, spot=None):
        b.entered.append((plan.underlying, lots))
        b.position = object()
        b.trades_today += 1
    b._enter = enter
    return b, feeds


def cand(index, setup, edge):
    from multi_index import Candidate
    plan = types.SimpleNamespace(underlying=index, name=f"{index} {setup}")
    return Candidate(index, setup, plan, types.SimpleNamespace(why="w"), edge, 100.0)


class BotMultiEntryTests(unittest.TestCase):
    T = dt.datetime(2026, 11, 3, 11, 0, tzinfo=IST)
    SNAP = types.SimpleNamespace(vix=16.0)

    def test_best_candidate_only_and_losers_not_chased(self):
        with tempfile.TemporaryDirectory() as d:
            b, feeds = bare_bot(d, [cand("SENSEX", "single", 2162), cand("NIFTY", "single", 1166)])
            b._try_multi(self.T, self.SNAP)
            self.assertEqual(b.entered, [("SENSEX", 1)])
            self.assertEqual(feeds["SENSEX"].pb.calls, ["took_single"])
            self.assertEqual(feeds["NIFTY"].pb.calls, ["skipped"])

    def test_locked_day_and_max_trades_block_entries(self):
        with tempfile.TemporaryDirectory() as d:
            b, _ = bare_bot(d, [cand("NIFTY", "condor", 266)])
            b.guard.on_trade_closed(self.T.date(), 500)
            b._try_multi(self.T, self.SNAP)
            self.assertEqual(b.entered, [])
            b, _ = bare_bot(d, [cand("NIFTY", "condor", 266)])
            b.trades_today = RiskParams().max_trades_per_day
            b._try_multi(self.T, self.SNAP)
            self.assertEqual(b.entered, [])

    def test_no_entry_after_square_off_or_before_the_window(self):
        with tempfile.TemporaryDirectory() as d:
            b, _ = bare_bot(d, [cand("NIFTY", "condor", 266)])
            b._try_multi(dt.datetime(2026, 11, 3, 15, 16, tzinfo=IST), self.SNAP)
            b._try_multi(dt.datetime(2026, 11, 3, 9, 20, tzinfo=IST), self.SNAP)
            self.assertEqual(b.entered, [])

    def test_unaffordable_candidate_falls_through_to_the_next(self):
        with tempfile.TemporaryDirectory() as d:
            b, feeds = bare_bot(d, [cand("BANKNIFTY", "credit", 900), cand("NIFTY", "condor", 266)])
            b._pb_lots = lambda plan, wanted: 0 if plan.underlying == "BANKNIFTY" else wanted
            b._try_multi(self.T, self.SNAP)
            self.assertEqual(b.entered, [("NIFTY", 1)])
            self.assertEqual(feeds["BANKNIFTY"].pb.calls, ["took_range"])     # margin refusal: one attempt per day


class ReportIndexTests(unittest.TestCase):
    def test_paper_trades_name_the_index(self):
        from report_site import paper_trades
        rows = [("2026-11-03T11:00:00", "pb_condor", "entry", "", 0, 0, 0, json.dumps({"lots": 1, "index": "NIFTY"})),
                ("2026-11-03T15:15:00", "pb_condor", "exit", "SQUARE_OFF_TIME", 500, 90, 410, "{}"),
                ("2026-11-04T10:00:00", "pb_condor", "entry", "", 0, 0, 0, json.dumps({"lots": 2})),
                ("2026-11-04T15:15:00", "pb_condor", "exit", "SQUARE_OFF_TIME", 300, 90, 210, "{}")]
        out = paper_trades(rows)
        self.assertTrue(out[0]["setup"].startswith("NIFTY "))
        self.assertEqual(out[0]["index"], "NIFTY")
        self.assertFalse(out[1]["setup"].startswith("SENSEX"))          # SENSEX keeps its existing names
        self.assertEqual(out[1]["index"], "SENSEX")


class StateTests(unittest.TestCase):
    def test_position_underlying_survives_a_restart(self):
        import main
        from strategy_engine import Leg, TradePlan
        from risk_manager import ManagedPosition, OpenLeg
        c = OptionContract(5, "NIFTYX23400CE", "NFO", 23400.0, dt.date(2026, 11, 9), "CE", 65, 0.05)
        plan = TradePlan("Long CE", "TREND", c.expiry, (Leg(c, "BUY", 100.0),), 65, 100.0, 1000.0, "r",
                         variant_id="pb_single_UP", underlying="NIFTY")
        pos = ManagedPosition(plan, 1, [OpenLeg(c, "BUY", 65, 100.0)], dt.datetime(2026, 11, 3, 11, 0, tzinfo=IST),
                              6500.0, 65000.0)
        self.assertEqual(main._position_from_dict(main._position_to_dict(pos)).plan.underlying, "NIFTY")
        old = main._position_to_dict(pos)
        old["plan"].pop("underlying")
        self.assertEqual(main._position_from_dict(old).plan.underlying, "SENSEX")   # pre-multi state files


if __name__ == "__main__":
    unittest.main()
