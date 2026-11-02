import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from costs import CostParams, trade_cost  # noqa: E402


class ZerodhaChargesTests(unittest.TestCase):
    """Pinned to Zerodha's own figures (Kite virtual contract note) for real orders of 2 Nov 2026."""

    def test_buy_order_matches_zerodha(self):
        # BUY 40 SENSEX26N0575200CE @ 536.45: Zerodha total 32.854463 (stamp duty Rs 1, not Rs 0.64)
        self.assertAlmostEqual(trade_cost([("BUY", 536.45, 40)], CostParams()), 32.854463, places=4)

    def test_sell_order_pays_stt_and_no_stamp(self):
        # SELL 40 @ 510: STT 30.60 (0.15%), no stamp duty
        c = trade_cost([("SELL", 510.0, 40)], CostParams())
        exch = 20400 * 0.000325
        self.assertAlmostEqual(c, 20 + exch + 0.0204 + (20 + exch + 0.0204) * 0.18 + 30.6, places=4)

    def test_stamp_duty_rounds_up_per_order(self):
        small = trade_cost([("BUY", 10.0, 20)], CostParams())      # turnover 200 -> stamp 0.006 -> Rs 1
        self.assertAlmostEqual(small - (20 + 200 * 0.000326) * 1.18, 1.0, places=2)
