"""Offline tests: python -m unittest discover -s tests -v"""
import datetime as dt
import os
import sqlite3
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:  # allow the suite to run without kiteconnect installed
    import kiteconnect  # noqa: F401
except ImportError:  # pragma: no cover
    m = types.ModuleType("kiteconnect")
    m.KiteConnect = m.KiteTicker = object
    e = types.ModuleType("kiteconnect.exceptions")
    for n in ("NetworkException", "GeneralException", "TokenException"):
        setattr(e, n, type(n, (Exception,), {}))
    m.exceptions = e
    sys.modules.update({"kiteconnect": m, "kiteconnect.exceptions": e})

import backtest as bt
import recorder
import strategy_library as lib
import token_store
from config import IST, BacktestParams, RiskParams, Schedule, StrategyParams
from costs import CostParams, trade_cost
from main import _position_from_dict, _position_to_dict
from market_data import Snapshot
from risk_manager import ManagedPosition, OpenLeg, RiskManager
from shadow import ShadowBook
from strategy_engine import Regime, RegimeState, TradePlan, bs_price, implied_vol

BP, SP = BacktestParams(), StrategyParams()
LOW = "LOW_VOL"
TODAY = dt.date(2026, 10, 21)
NOW = dt.datetime(2026, 10, 21, 10, 0, tzinfo=IST)
IV = {"LOW_VOL": 0.11, "NORMAL": 0.15, "HIGH_VOL": 0.22}


def plan_for(spec, spot=80000.0):
    dte = 2 if spec.dte_max <= 3 else 5
    regime = spec.regimes[0]
    chain = bt.model_chain(spot, IV[regime], TODAY + dt.timedelta(days=dte), NOW, BP, SP.risk_free_rate)
    rs = RegimeState(Regime(regime), 15.0, 1.0, 12.0, False, 0)
    tdir = lib.TEMPLATE_DIRECTION.get(spec.template, 0)
    return lib.build_plan(spec, chain, rs, tdir, TODAY, NOW), chain, rs


class LibraryTests(unittest.TestCase):
    def test_at_least_100_unique_variants(self):
        specs = lib.build_library()
        self.assertGreaterEqual(len(specs), 100)
        self.assertEqual(len({s.id for s in specs}), len(specs))
        self.assertGreaterEqual(len({s.template for s in specs}), 8)

    def test_every_template_can_produce_a_trade_and_is_defined_risk(self):
        by_template, total, built = {}, 0, 0
        for spec in (s for s in lib.build_library() if not s.needs_oi):   # OI variants need live OI data
            plan, _, _ = plan_for(spec)
            total += 1
            if plan:
                built += 1
                by_template[spec.template] = by_template.get(spec.template, 0) + 1
                self.assertGreater(plan.max_loss_unit, 0)
                self.assertGreater(plan.max_profit_unit, 0)
        self.assertEqual(set(by_template), {s.template for s in lib.build_library()},
                         f"templates that never built a plan: built={by_template}")
        print(f"\n  library coverage on a sample chain: {built}/{total} variants produced a plan")
        self.assertGreater(built / total, 0.5)

    def test_oi_variants_have_plain_twins_and_are_not_backtested(self):
        specs = lib.build_library()
        ids = {s.id for s in specs}
        oi = [s for s in specs if s.needs_oi]
        self.assertGreaterEqual(len(specs), 250)
        self.assertGreater(len(oi), 50)
        for s in oi:
            self.assertIn(s.base_id, ids, f"no plain twin for {s.id}")      # needed by promote.py's benchmark
            self.assertTrue(s.id.endswith("|oi=" + s.oi_rule))
        res = bt.run_backtest(bt.synthetic_bars(200), oi[:6], SP, BP, CostParams())
        self.assertEqual(sum(len(v) for v in res.values()), 0)                # skipped: no historical OI

    def test_chain_features_rules_and_fail_closed(self):
        import dataclasses
        from chain_features import FeatureTracker, compute
        plain = next(s for s in lib.build_library() if s.template == "iron_condor" and s.regimes == (LOW,)
                     and dict(s.params) == {"delta": 0.15, "wing": 500} and s.dte_max == 3)
        walls = next(s for s in lib.build_library() if s.id == plain.id + "|oi=walls")
        pcr_calm = next(s for s in lib.build_library() if s.id == plain.id + "|oi=pcr_calm")
        gex_pos = next(s for s in lib.build_library() if s.id == plain.id + "|oi=gex_pos")
        base_plan, chain, rs = plan_for(plain)
        sc = min(l.contract.strike for l in base_plan.legs if l.side == "SELL" and l.contract.kind == "CE")
        sp = max(l.contract.strike for l in base_plan.legs if l.side == "SELL" and l.contract.kind == "PE")

        def with_oi(call_peak, put_peak, call_base=1000.0, put_base=1000.0):
            c2 = dataclasses.replace(chain, quotes={})
            for (k, kind), q in chain.quotes.items():
                peak = call_peak if kind == "CE" else put_peak
                base = call_base if kind == "CE" else put_base
                c2.quotes[(k, kind)] = dataclasses.replace(q, oi=base * (50 if k == peak else 1))
            return c2

        inside = compute(with_oi(sc - 200, sp + 200))               # walls between spot and the shorts
        self.assertEqual(inside.call_wall, sc - 200)
        self.assertEqual(inside.put_wall, sp + 200)
        self.assertIsNotNone(lib.build_plan(walls, chain, rs, 0, TODAY, NOW, inside))
        beyond = compute(with_oi(sc + 300, sp - 300))               # walls beyond the shorts: no protection
        self.assertIsNone(lib.build_plan(walls, chain, rs, 0, TODAY, NOW, beyond))
        self.assertIsNone(lib.build_plan(walls, chain, rs, 0, TODAY, NOW, None))      # no features: fail closed
        self.assertIsNone(lib.build_plan(walls, chain, rs, 0, TODAY, NOW))
        balanced = compute(with_oi(sc - 200, sp + 200, 1000.0, 1000.0))
        self.assertTrue(0.8 <= balanced.pcr <= 1.3)
        self.assertIsNotNone(lib.build_plan(pcr_calm, chain, rs, 0, TODAY, NOW, balanced))
        put_heavy = compute(with_oi(sc - 200, sp + 200, 1000.0, 5000.0))
        self.assertGreater(put_heavy.pcr, 1.3)
        self.assertIsNone(lib.build_plan(pcr_calm, chain, rs, 0, TODAY, NOW, put_heavy))
        self.assertEqual(compute(with_oi(sc - 200, sp + 200, 5000.0, 1000.0)).net_gex > 0,
                         lib.build_plan(gex_pos, chain, rs, 0, TODAY, NOW,
                                        compute(with_oi(sc - 200, sp + 200, 5000.0, 1000.0))) is not None)
        self.assertLess(compute(with_oi(sc - 200, sp + 200, 1000.0, 5000.0)).net_gex, 0)
        self.assertIsNone(lib.build_plan(gex_pos, chain, rs, 0, TODAY, NOW,
                                         compute(with_oi(sc - 200, sp + 200, 1000.0, 5000.0))))
        # OI change vs a reference taken >= 5 minutes earlier
        tr = FeatureTracker()
        tr.update(0.0, with_oi(sc - 200, sp + 200, 1000.0, 1000.0))
        f = tr.update(400.0, with_oi(sc - 200, sp + 200, 2000.0, 1000.0))
        self.assertGreater(f.call_oi_chg, 0)
        self.assertAlmostEqual(f.put_oi_chg, 0.0)
        self.assertIsNone(FeatureTracker().update(0.0, chain).call_oi_chg)      # no reference yet

    def test_regime_and_spike_gates(self):
        spec = next(s for s in lib.build_library() if s.template == "iron_condor" and s.regimes == ("LOW_VOL",))
        plan, chain, _ = plan_for(spec)
        self.assertIsNotNone(plan)
        high = RegimeState(Regime.HIGH_VOL, 25, 2, 20, False, 3)
        self.assertIsNone(lib.build_plan(spec, chain, high, 0, TODAY, NOW))
        spike = RegimeState(Regime.LOW_VOL, 12, 0.5, 9, True, -2)
        self.assertIsNone(lib.build_plan(spec, chain, spike, 0, TODAY, NOW))

    def test_load_approved_fails_closed(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.json")
            self.assertEqual(lib.load_approved(p, TODAY), {})
            open(p, "w").write('{"valid_until": "2026-10-01", "variants": [{"id": "x", "score": 1}]}')
            self.assertEqual(lib.load_approved(p, TODAY), {})
            open(p, "w").write('{"valid_until": "2026-12-01", "variants": [{"id": "x", "score": 1}]}')
            self.assertEqual(lib.load_approved(p, TODAY), {"x": 1.0})


class MathAndCostTests(unittest.TestCase):
    def test_iv_roundtrip(self):
        p = bs_price(80000, 80500, 5 / 365, 0.065, 0.17, "CE")
        self.assertAlmostEqual(implied_vol(p, 80000, 80500, 5 / 365, 0.065, "CE"), 0.17, places=3)

    def test_costs(self):
        c = trade_cost([("SELL", 100.0, 20), ("BUY", 100.0, 20)], CostParams())
        self.assertGreater(c, 40.0)                       # two orders of brokerage at minimum
        sell_only = trade_cost([("SELL", 100.0, 20)], CostParams())
        buy_only = trade_cost([("BUY", 100.0, 20)], CostParams())
        self.assertGreater(sell_only, buy_only)           # STT applies on the sell side

    def test_position_sizing_respects_risk_budget(self):
        spec = next(s for s in lib.build_library() if s.template == "iron_condor")
        plan, _, _ = plan_for(spec)
        rm = RiskManager(RiskParams(), Schedule())
        lots = rm.size(plan, 500_000.0)
        self.assertLessEqual(lots * plan.max_loss_per_lot, 500_000 * RiskParams().max_risk_per_trade_pct + 1e-6)
        self.assertEqual(rm.size(plan, 1_000.0), 0)       # too small an account: no trade


class BacktestTests(unittest.TestCase):
    def test_simulation_stop_loss_on_crash_day(self):
        spec = next(s for s in lib.build_library() if s.template == "iron_condor" and s.regimes == ("LOW_VOL",))
        plan, _, _ = plan_for(spec)
        crash = {"date": TODAY, "open": 80000.0, "high": 80100.0, "low": 74000.0, "close": 74500.0}
        t = bt.simulate_trade(plan, spec, crash, 0.11, 0.2, BP, CostParams(), SP.risk_free_rate, NOW)
        self.assertEqual(t.reason, "SL")
        self.assertLess(t.net, -0.5 * plan.max_loss_per_lot)

    def test_no_phantom_edge_for_option_buyers(self):
        """Regression: a calendar-time decay clock made long straddles/strangles look
        hugely profitable on daily bars. Buyers must not show a large positive average
        on synthetic data where implied vol >= realised vol."""
        import statistics
        bars = bt.synthetic_bars(500)
        specs = [s for s in lib.build_library() if s.template in ("long_straddle", "long_strangle")][::3]
        results = bt.run_backtest(bars, specs, SP, BP, CostParams())
        nets = [t.net for v in results.values() for t in v]
        self.assertGreater(len(nets), 50)
        self.assertLess(statistics.mean(nets), 500.0)

    def test_trading_time_clock(self):
        # Friday 10:00 -> next Monday close must count 1 trading day, not 3 calendar days
        fri = dt.datetime(2026, 10, 23, 10, 0, tzinfo=IST)
        y = bt._years(dt.date(2026, 10, 26), fri)
        self.assertLess(y * 252, 2.0)
        self.assertGreater(y * 252, 1.0)

    def test_pipeline_runs_end_to_end_on_synthetic_data(self):
        bars = bt.synthetic_bars(420)
        specs = lib.build_library()[::12]
        results = bt.run_backtest(bars, specs, SP, BP, CostParams())
        self.assertEqual(set(results), {s.id for s in specs})
        self.assertGreater(sum(len(v) for v in results.values()), 0)
        rows, per = bt.walk_forward(results, [b["date"] for b in bars[30:]], bt.Criteria(train_days=100, test_days=40))
        self.assertGreater(len(rows), 0)
        self.assertEqual(set(per), set(results))
        for v in per.values():
            self.assertIn("approved", v)


class InfraTests(unittest.TestCase):
    def test_token_freshness(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.json")
            rec = token_store.write_token(path, "abc", "U1")
            self.assertEqual(token_store.read_token(path)["access_token"], "abc")
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
            self.assertTrue(token_store.is_fresh(rec))
            stale = {"access_token": "x", "issued_at": dt.datetime(2026, 10, 20, 10, 0, tzinfo=IST).isoformat()}
            self.assertFalse(token_store.is_fresh(stale, dt.datetime(2026, 10, 21, 9, 0, tzinfo=IST)))
            self.assertTrue(token_store.is_fresh(stale, dt.datetime(2026, 10, 20, 20, 0, tzinfo=IST)))

    def test_login_state_is_stateless_signed_and_expires(self):
        import token_service
        from config import Config, Credentials, Paths
        class FakeKite:
            def __init__(self, **kw): pass
            def login_url(self): return "https://kite.example/login?v=3"
        orig = token_service.KiteConnect
        token_service.KiteConnect = FakeKite
        try:
            with tempfile.TemporaryDirectory() as d:
                mk = lambda secret: token_service.TokenService(Config(
                    credentials=Credentials("k", secret, None, None),
                    paths=Paths(data_dir=d, token_file=os.path.join(d, "t.json"))))
                maker, server, other = mk("s3cret"), mk("s3cret"), mk("different")
                url = maker.login_url()
                state = url.split("state%3D")[1]
                self.assertTrue(server.check_state(state))                 # link from another process
                self.assertFalse(other.check_state(state))                 # wrong secret = forged
                self.assertFalse(server.check_state(state[:-1] + ("0" if state[-1] != "0" else "1")))  # tampered
                self.assertFalse(server.check_state(None))
                self.assertFalse(server.check_state("forged"))
                import time as _t
                self.assertFalse(server.check_state(state, now=_t.time() + 1900))  # expired
                self.assertTrue(set(state) <= set("0123456789abcdefx"))            # URL/Kite safe
        finally:
            token_service.KiteConnect = orig

    def test_sheets_sink_posts_rows_and_is_noop_when_disabled(self):
        import json, threading, time as _t
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import sheets_sink
        got = []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sink = sheets_sink.SheetsSink(f"http://127.0.0.1:{srv.server_port}/x", "sec")
        sink.append("Orders", sheets_sink.ORDERS_HEADER, [["2026-10-22T09:31:00+05:30", "paper", "v1", "ENTRY", "SENSEX26OCT80000CE", "SELL", 20, 100.0, 99.5, "r"]])
        for _ in range(50):
            if got: break
            _t.sleep(0.1)
        srv.shutdown()
        self.assertEqual(got[0]["secret"], "sec")
        self.assertEqual(got[0]["tab"], "Orders")
        self.assertEqual(len(got[0]["rows"][0]), len(sheets_sink.ORDERS_HEADER))
        off = sheets_sink.SheetsSink(None, None)
        off.append("Orders", [], [[1]])                      # must not raise or start a thread
        self.assertFalse(off.enabled)

    def test_sheets_sink_does_not_retry_unexpected_200_reply(self):
        import json, threading, time as _t
        from http.server import BaseHTTPRequestHandler, HTTPServer
        import sheets_sink
        got = []
        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.end_headers(); self.wfile.write(b"done")   # not exactly "ok"
            def log_message(self, *a): pass
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        sink = sheets_sink.SheetsSink(f"http://127.0.0.1:{srv.server_port}/x", "sec")
        sink.append("Health", ["a"], [["row"]])
        _t.sleep(2.5)
        srv.shutdown()
        self.assertEqual(len(got), 1)                       # exactly one delivery, no retry storm
        self.assertTrue(got[0]["id"])

    def test_approvals_load_on_same_day_restart_and_reload_on_change(self):
        import json as _j
        import main as m
        from config import Config, Credentials, Paths
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(credentials=Credentials("k", None, None, None),
                         paths=Paths(data_dir=d, token_file=os.path.join(d, "t.json")))
            bot = object.__new__(m.Bot)
            bot.cfg, bot.live, bot.approved, bot._approved_stamp = cfg, False, {}, None
            bot.alerts = types.SimpleNamespace(send=lambda *a, **k: None)
            now = dt.datetime(2026, 10, 22, 11, 0, tzinfo=IST)
            bot._refresh_approvals(now)                          # file missing: fails closed
            self.assertEqual(bot.approved, {})
            with open(cfg.paths.approved_backtest, "w") as fh:   # backtest finishes while bot is running
                _j.dump({"valid_until": "2026-12-31", "variants": [{"id": "v1", "score": 2.0}]}, fh)
            bot._refresh_approvals(now)                          # no new-day step needed
            self.assertEqual(bot.approved, {"v1": 2.0})
            self.assertEqual(bot._approved_stamp[-1], now.date())

    def test_chain_is_built_from_stream_without_rest_and_rest_is_rate_limited(self):
        import time as _t
        from strategy_engine import StrategyEngine
        from zerodha_client import OptionContract
        expiry = TODAY + dt.timedelta(days=3)
        cons = [OptionContract(1000 + i * 2 + j, f"S{i}{k}", "BFO", 73000.0 + i * 100, expiry, k, 20, 0.05)
                for i in range(0, 61) for j, k in enumerate(("CE", "PE"))]

        class FakeClient:
            def __init__(self):
                self.flow, self.rest_calls, self.full = {}, 0, set()
            def contracts_for(self, e): return cons
            def subscribe_full(self, m): self.full |= set(m)
            def flow_snapshot(self, max_age_s=15.0):
                now = _t.monotonic()
                return {k: v for k, v in self.flow.items() if now - v["mono"] <= max_age_s}
            def quote(self, keys):
                self.rest_calls += 1
                return {k: {"last_price": 50.0, "oi": 10, "volume": 5, "depth": {}} for k in keys}

        cl = FakeClient()
        eng = StrategyEngine(cl, SP)
        spot = 76000.0
        now = dt.datetime(2026, 10, 21, 10, 0, tzinfo=IST)
        c1 = eng.build_chain(expiry, spot, now)              # nothing ticked yet: REST warm-up
        self.assertIsNotNone(c1)
        self.assertEqual((eng.last_source, cl.rest_calls), ("rest", 1))
        self.assertTrue(cl.full)                             # whole window now subscribed in FULL mode
        self.assertIsNone(eng.build_chain(expiry, spot, now))  # still no ticks: 2nd REST call < 2 s later is refused
        self.assertEqual(cl.rest_calls, 1)
        for c in cons:                                       # ticks arrive
            intrinsic = max(0.0, (spot - c.strike) if c.kind == "CE" else (c.strike - spot))
            cl.flow[c.token] = {"mono": _t.monotonic(), "ltp": intrinsic + 40.0, "oi": 1000, "volume": 7,
                                "bid": intrinsic + 39.0, "ask": intrinsic + 41.0}
        t0 = _t.perf_counter()
        for _ in range(20):
            ch = eng.build_chain(expiry, spot, now)
        per_call = (_t.perf_counter() - t0) / 20
        self.assertEqual((eng.last_source, cl.rest_calls), ("stream", 1))   # no further REST calls
        q = ch.get(ch.atm, "CE")
        self.assertEqual((q.ltp - q.contract.strike * 0 > 0, q.bid, q.ask), (True, q.ltp - 1.0, q.ltp + 1.0))
        self.assertLess(per_call, 0.25, "chain build must fit comfortably inside a 1 s cycle")
        for k in list(cl.flow):                              # feed dies: stale ATM ticks -> no stream chain
            cl.flow[k]["mono"] -= 4000.0
        eng._last_rest = -1e9
        eng.build_chain(expiry, spot, now)
        self.assertEqual(eng.last_source, "rest")

    def test_recorder_full_chain_throttle_and_flow_prune(self):
        with tempfile.TemporaryDirectory() as d:
            rec = recorder.Recorder(os.path.join(d, "t.db"))
            rec.flow([("2026-09-01T10:00:00", "S", 1.0, "CE", 1, 1, 1, 1, 1),
                      ("2026-10-20T10:00:00", "S", 1.0, "CE", 1, 1, 1, 1, 1)])
            self.assertEqual(rec.prune_flow(30, "2026-10-21"), 1)
            self.assertEqual(rec.prune_flow(0, "2026-10-21"), 0)
            rec.close()

    def test_recorder_stores_features_and_flow(self):
        from chain_features import compute
        with tempfile.TemporaryDirectory() as d:
            rec = recorder.Recorder(os.path.join(d, "t.db"))
            rs = RegimeState(Regime.LOW_VOL, 12.0, 0.5, 9.0, False, 0)
            chain = bt.model_chain(80000.0, 0.11, TODAY + dt.timedelta(days=2), NOW, BP, SP.risk_free_rate)
            snap = Snapshot(NOW, 80000.0, 12.0, rs, chain, 0, True, compute(chain))
            rec.record_snapshot(snap)
            rec.flow([(NOW.isoformat(), "SX1", 80000.0, "CE", 100.0, 5000.0, 123.0, 99.5, 100.5)])
            con = sqlite3.connect(os.path.join(d, "t.db"))
            self.assertEqual(con.execute("SELECT COUNT(*) FROM features").fetchone()[0], 1)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM flow").fetchone()[0], 1)
            con.close(); rec.close()

    def test_health_collect_and_sheet_row(self):
        import health_report as hr
        from config import Config, Credentials, Paths
        with tempfile.TemporaryDirectory() as d:
            cfg = Config(credentials=Credentials("k", None, None, None),
                         paths=Paths(data_dir=d, token_file=os.path.join(d, "t.json")))
            now = dt.datetime(2026, 10, 22, 11, 0, tzinfo=IST)
            res = hr.collect(cfg, now)                              # no DB yet: must not raise
            self.assertTrue(res["flags"])
            rec = recorder.Recorder(cfg.paths.db)
            rs = RegimeState(Regime.LOW_VOL, 12.0, 0.5, 9.0, False, 0)
            chain = bt.model_chain(80000.0, 0.11, now.date() + dt.timedelta(days=2), now, BP, SP.risk_free_rate)
            rec.record_snapshot(Snapshot(now - dt.timedelta(seconds=5), 80000.0, 12.0, rs, chain, 0, True))
            rec.close()
            res = hr.collect(cfg, now)
            i = res["info"]
            self.assertEqual(i["market_rows"], 1)
            self.assertGreater(i["chain_rows"], 50)
            self.assertAlmostEqual(i["age_s"], 5, delta=1)
            row = hr.sheet_row(now, "paper", res)
            self.assertEqual(len(row), len(hr.HEALTH_HEADER))
            self.assertIn("Not logged in", row[-1])                 # no token file in the temp dir

    def test_recorder_and_shadow_book(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            rec = recorder.Recorder(db)
            specs = [s for s in lib.build_library()
                     if s.template == "iron_condor" and s.regimes == ("LOW_VOL",) and s.dte_max == 3][:4]
            book = ShadowBook(specs, rec, RiskParams(), Schedule(), CostParams())
            expiry = TODAY + dt.timedelta(days=2)
            rs = RegimeState(Regime.LOW_VOL, 12.0, 0.5, 9.0, False, -2)
            t = dt.datetime(2026, 10, 21, 9, 35, tzinfo=IST)
            first = None
            while t.time() <= dt.time(15, 25):
                spot = 80000.0 + (t.hour - 9) * 15
                chain = bt.model_chain(spot, 0.11, expiry, t, BP, SP.risk_free_rate)
                snap = Snapshot(t, spot, 12.0, rs, chain, 0, True)
                first = first or snap
                rec.record_snapshot(snap)
                book.step(snap, t)
                t += dt.timedelta(minutes=10)
            con = sqlite3.connect(db)
            self.assertGreater(con.execute("SELECT COUNT(*) FROM chain").fetchone()[0], 100)
            self.assertGreater(con.execute("SELECT COUNT(*) FROM market").fetchone()[0], 10)
            n = con.execute("SELECT COUNT(*) FROM shadow_trades").fetchone()[0]
            self.assertGreaterEqual(n, 1)
            self.assertEqual(len(book.open), 0)            # everything squared off by 15:15
            out = os.path.join(d, "chain.csv")
            self.assertGreater(recorder._export(db, "chain", None, out), 100)
            con.close()
            rec.close()

    def test_position_state_roundtrip(self):
        spec = next(s for s in lib.build_library() if s.template == "iron_condor")
        plan, _, _ = plan_for(spec)
        legs = [OpenLeg(l.contract, l.side, 20, l.ref_price) for l in plan.legs]
        pos = ManagedPosition(plan, 1, legs, NOW, plan.max_loss_per_lot, plan.max_profit_per_lot, 123.0)
        back = _position_from_dict(_position_to_dict(pos))
        self.assertEqual(back.plan.variant_id, plan.variant_id)
        self.assertEqual([(l.contract.symbol, l.side, l.qty) for l in back.legs],
                         [(l.contract.symbol, l.side, l.qty) for l in legs])
        self.assertEqual(back.peak_pnl, 123.0)
        self.assertEqual(back.plan.expiry, plan.expiry)


if __name__ == "__main__":
    unittest.main()
