import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from costs import CostParams  # noqa: E402
from recorder import Recorder  # noqa: E402
from report_site import (ReportSite, live_trades, load_live, paper_trades, render, save_live_day)  # noqa: E402
from telegram_control import TelegramControl  # noqa: E402

DAY = dt.date(2026, 11, 2)


def fill(oid, side, qty, px, t, sym="SENSEX26N0580000CE", exch="BFO"):
    return {"trade_id": f"t{oid}{t}", "order_id": oid, "exchange": exch, "tradingsymbol": sym, "product": "MIS",
            "average_price": px, "quantity": qty, "transaction_type": side,
            "fill_timestamp": dt.datetime.combine(DAY, dt.time(*t)), "placed_by": "AB1234"}


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_saves_only_option_fills_without_account_ids(self):
        orders = [{"order_id": "1", "tag": "SXALGO-1", "exchange": "BFO", "tradingsymbol": "X", "placed_by": "AB1234"},
                  {"order_id": "9", "tag": "", "exchange": "NSE", "tradingsymbol": "INFY"}]
        trades = [fill("1", "BUY", 40, 245.0, (10, 46)), fill("9", "BUY", 1, 1500.0, (11, 0), "INFY", "NSE")]
        rec = save_live_day(orders, trades, DAY, self.dir)
        self.assertEqual(len(rec["fills"]), 1)
        self.assertEqual(len(rec["orders"]), 1)
        raw = open(os.path.join(self.dir, "2026-11-02.json"), encoding="utf-8").read()
        self.assertNotIn("AB1234", raw)                        # no user ids stored
        self.assertEqual(load_live(self.dir, "2026-11-01")[0]["date"], "2026-11-02")
        self.assertEqual(load_live(self.dir, "2026-11-03"), [])

    def test_realised_pnl_charges_and_bot_tag(self):
        orders = [{"order_id": "1", "tag": "SXALGO-a"}, {"order_id": "2", "tag": "SXALGO-b"}, {"order_id": "3", "tag": ""},
                  {"order_id": "4", "tag": ""}]
        fills = [fill("1", "BUY", 20, 245.0, (10, 46)), fill("1", "BUY", 20, 246.0, (10, 46)),   # one order, 2 fills
                 fill("2", "SELL", 40, 300.0, (15, 15)),
                 fill("3", "BUY", 20, 100.0, (11, 0), "SENSEX26N0579000PE"), fill("4", "SELL", 20, 90.0, (11, 30), "SENSEX26N0579000PE")]
        rows, days = live_trades([{"date": "2026-11-02", "fills": fills, "orders": orders}], CostParams())
        self.assertEqual(days, ["2026-11-02"])
        ce = next(r for r in rows if r["symbol"].endswith("CE"))
        self.assertEqual((ce["who"], ce["bought"], ce["sold"], ce["open_qty"]), ("Bot", 40, 40, 0))
        self.assertAlmostEqual(ce["gross"], (300.0 - 245.5) * 40)
        self.assertTrue(0 < ce["cost"] < 150)                   # 2 orders: brokerage counted per ORDER, not per fill
        self.assertAlmostEqual(ce["net"], ce["gross"] - ce["cost"], places=2)
        pe = next(r for r in rows if r["symbol"].endswith("PE"))
        self.assertEqual(pe["who"], "Manual")
        self.assertLess(pe["net"], 0)

    def test_open_quantity_is_not_counted_as_profit(self):
        rows, _ = live_trades([{"date": "2026-11-02", "orders": [],
                                "fills": [fill("1", "BUY", 40, 245.0, (10, 46)), fill("2", "SELL", 20, 300.0, (14, 0))]}],
                              CostParams())
        self.assertEqual(rows[0]["open_qty"], 20)
        self.assertAlmostEqual(rows[0]["gross"], (300.0 - 245.0) * 20)


class PaperAndPageTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.rec = Recorder(os.path.join(self.dir, "t.db"))

    def _trade(self, ts_in, ts_out, vid, net, reason="SQUARE_OFF_TIME"):
        self.rec.trade(ts_in, "PAPER", vid, "entry", "", 0, 0, 0, {"lots": 2, "rationale": "[PLAYBOOK] x", "spot": 80000})
        self.rec.trade(ts_out, "PAPER", vid, "exit", reason, net + 50, 50, net, {"lots": 2})

    def test_pairs_entries_with_exits_and_ignores_older_and_live(self):
        self.rec.trade("2026-10-30T10:00:00", "PAPER", "old", "entry", "", 0, 0, 0, {})
        self.rec.trade("2026-10-30T11:00:00", "PAPER", "old", "exit", "X", 1, 0, 1, {})
        self._trade("2026-11-02T10:46:00", "2026-11-02T15:15:00", "pb_single_UP", 2100.0)
        self._trade("2026-11-02T10:00:00", "2026-11-02T10:40:00", "pb_condor", -400.0, "LOSS_CAP_1500")
        self.rec.trade("2026-11-02T11:00:00", "LIVE", "pb_condor", "exit", "X", 9, 0, 9, {})
        rows = paper_trades(self.rec.trades_since("2026-11-02", "PAPER"))
        self.assertEqual(sorted(r["setup"] for r in rows), ["Iron condor", "Single leg Up"])
        single = next(r for r in rows if r["setup"] == "Single leg Up")
        self.assertEqual((single["entry"], single["exit"], single["lots"], single["net"]), ("10:46", "15:15", 2, 2100.0))

    def test_build_writes_page_and_detects_changes(self):
        site = ReportSite(self.dir, "https://x/r/tok/", "2026-11-02", CostParams())
        changed, _ = site.build(self.rec, ["2026-11-02"])
        self.assertTrue(changed)                               # first build
        changed, _ = site.build(self.rec, ["2026-11-02"])
        self.assertFalse(changed)                              # nothing new: no Telegram notice
        self._trade("2026-11-02T10:46:00", "2026-11-02T15:15:00", "pb_single_UP", 2100.0)
        changed, summary = site.build(self.rec, ["2026-11-02"])
        self.assertTrue(changed)
        html = open(os.path.join(self.dir, "site", "index.html"), encoding="utf-8").read()
        self.assertIn("pb_single_UP", html)
        self.assertNotIn("/*__DATA__*/null", html)
        text = site.summary_text("2026-11-02", summary)
        self.assertIn("Paper: net +2,100 over 1 trade", text)
        self.assertIn("https://x/r/tok/", text)

    def test_data_cannot_break_out_of_the_script(self):
        html = render({"since": "x", "paper": {"trades": [{"why": "</script><script>alert(1)</script>"}], "days": []},
                       "live": {"trades": [], "days": []}, "backtest_url": ""}, "now")
        self.assertNotIn("</script><script>alert(1)", html)
        self.assertEqual(html.count("</script>"), 1)


class TelegramReportTests(unittest.TestCase):
    def test_report_command(self):
        d = tempfile.mkdtemp()
        tg = TelegramControl("t", "1", os.path.join(d, "s.json"), reply=lambda t: None, report_url="https://x/r/tok/")
        self.assertIn("https://x/r/tok/", tg.handle("1", "/report"))
        self.assertIsNone(tg.handle("2", "/report"))           # other chats get nothing
        tg2 = TelegramControl("t", "1", os.path.join(d, "s.json"), reply=lambda t: None)
        self.assertIn("not configured", tg2.handle("1", "/report"))


if __name__ == "__main__":
    unittest.main()
