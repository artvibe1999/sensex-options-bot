import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from costs import CostParams, trade_cost  # noqa: E402


def zerodha(exch_pct, turnover, stamp, stt):
    brk, ex, sebi = 20.0, turnover * exch_pct, turnover * 0.000001
    return brk + ex + sebi + (brk + ex + sebi) * 0.18 + stamp + stt


class ZerodhaChargesTests(unittest.TestCase):
    """Pinned to Zerodha's own figures (Kite virtual contract note)."""

    def test_real_buy_order_matches_zerodha(self):
        # 2 Nov 2026: BUY 40 SENSEX26N0575200CE @ 536.45, Zerodha total 32.854463 (stamp Rs 1)
        self.assertAlmostEqual(trade_cost([("BUY", 536.45, 40)], CostParams()), 32.854463, places=4)

    def test_sell_order_pays_stt_and_no_stamp(self):
        c = trade_cost([("SELL", 510.0, 40)], CostParams())
        self.assertAlmostEqual(c, zerodha(0.000325, 20400, 0, 30.6), places=4)

    def test_stamp_duty_is_rounded_to_the_nearest_rupee(self):
        # Zerodha contract note, 3 Nov 2026 (BFO, qty 20): turnover 10,000 -> stamp 0 (0.30); 20,000 -> 1 (0.60);
        # 50,000 -> 2 (1.50)
        for px, stamp in ((500.0, 0), (1000.0, 1), (2500.0, 2)):
            c = trade_cost([("BUY", px, 20)], CostParams())
            self.assertAlmostEqual(c, zerodha(0.000325, px * 20, stamp, 0), places=4, msg=px)

    def test_nse_exchange_rate(self):
        # Zerodha contract note, 3 Nov 2026 (NFO, qty 65): BUY @ 500 -> exchange 11.547, stamp 1; SELL -> STT 48.75
        nse = CostParams().for_exchange("NFO")
        self.assertAlmostEqual(trade_cost([("BUY", 500.0, 65)], nse), zerodha(0.0003553, 32500, 1, 0), places=3)
        self.assertAlmostEqual(trade_cost([("SELL", 500.0, 65)], nse), zerodha(0.0003553, 32500, 0, 48.75), places=3)
        self.assertEqual(CostParams().for_exchange("BFO").exch_txn_pct, CostParams().exch_txn_pct)


if __name__ == "__main__":
    unittest.main()
