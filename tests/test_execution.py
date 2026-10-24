import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sim_harness as sh
from execution import LiveBroker, reverse_legs, validate_margin
from option_strategies import STRATEGY_CLASSES, EntryContext, analyse, expiry_payoff
import strategy_library as lib

CHAIN, NOW, BP, SP = sh.sample_chain()


def seq(kite, kinds=("place", "complete", "place_error", "cancel")):
    return [(k, s, sym) for k, s, sym, *_ in kite.log if k in kinds]


class StrategyMathTests(unittest.TestCase):
    def test_all_ten_build_with_defined_risk_and_consistent_breakevens(self):
        self.assertEqual(len(STRATEGY_CLASSES), 10)
        for key, cls in STRATEGY_CLASSES.items():
            with self.subTest(key):
                plan = sh.plan_from_strategy(cls(), CHAIN)
                self.assertIsNotNone(plan, key)
                priced = [(l.contract.kind, l.contract.strike, l.side, l.ratio, l.ref_price) for l in plan.legs]
                eco = analyse(priced)
                self.assertAlmostEqual(eco.max_loss, plan.max_loss_unit, places=1)
                self.assertAlmostEqual(eco.max_profit, plan.max_profit_unit, places=1)
                self.assertGreater(eco.max_loss, 0)
                self.assertGreater(eco.max_profit, 0)
                for be in eco.breakevens:                          # P&L is zero at every reported breakeven
                    self.assertAlmostEqual(expiry_payoff(priced, be), 0.0, delta=0.5)
                grid = [CHAIN.spot * f / 100 for f in range(50, 160)]      # nothing loses more than max loss
                self.assertGreaterEqual(min(expiry_payoff(priced, s) for s in grid), -eco.max_loss - 0.01)

    def test_ratio_leg_doubles_quantity_and_is_capped_above_the_wing(self):
        plan = sh.plan_from_strategy(STRATEGY_CLASSES["bull_call_ratio"](), CHAIN)
        ratios = sorted(l.ratio for l in plan.legs)
        self.assertEqual(ratios, [1, 1, 2])
        short = next(l for l in plan.legs if l.side == "SELL")
        self.assertEqual(plan.qty_for(short, 3), 3 * plan.lot_size * 2)
        priced = [(l.contract.kind, l.contract.strike, l.side, l.ratio, l.ref_price) for l in plan.legs]
        far = max(l.contract.strike for l in plan.legs)
        self.assertAlmostEqual(expiry_payoff(priced, far + 1000), expiry_payoff(priced, far + 5000), places=3)

    def test_ladder_has_protective_wing(self):
        plan = sh.plan_from_strategy(STRATEGY_CLASSES["bear_put_ladder"](), CHAIN)
        self.assertEqual([l.side for l in plan.legs], ["BUY", "SELL", "SELL", "BUY"])
        strikes = [l.contract.strike for l in plan.legs]
        self.assertEqual(strikes, sorted(strikes, reverse=True))

    def test_naked_short_call_structure_is_rejected(self):
        self.assertIsNone(analyse([("CE", 80000, "SELL", 1, 100.0)]))
        self.assertIsNone(analyse([("CE", 80000, "BUY", 1, 150.0), ("CE", 80300, "SELL", 2, 90.0)]))

    def test_entry_triggers(self):
        bull, ic = STRATEGY_CLASSES["bull_call_spread"](), STRATEGY_CLASSES["iron_condor"]()
        self.assertTrue(bull.entry_trigger(EntryContext("NORMAL", 1)))
        self.assertFalse(bull.entry_trigger(EntryContext("NORMAL", -1)))
        self.assertFalse(bull.entry_trigger(EntryContext("LOW_VOL", 1)))
        self.assertFalse(bull.entry_trigger(EntryContext("NORMAL", 1, spike=True)))
        self.assertTrue(ic.entry_trigger(EntryContext("LOW_VOL", 0)))
        self.assertFalse(ic.entry_trigger(EntryContext("HIGH_VOL", 0)))

    def test_library_wires_all_ten_templates_with_unique_ids(self):
        specs = lib.build_library()
        self.assertEqual(len({s.id for s in specs}), len(specs))
        have = {s.template for s in specs if s.template.startswith("os_")}
        self.assertEqual(have, {"os_" + k for k in STRATEGY_CLASSES})


class ExecutionTests(unittest.TestCase):
    def broker(self, kite):
        return LiveBroker(kite, sh.fast_cfg(), sleep=lambda _s: None)

    def legs(self, key="iron_condor", lots=1):
        return sh.order_legs(sh.plan_from_strategy(STRATEGY_CLASSES[key](), CHAIN), lots)

    def test_buys_fire_first_and_sells_wait_for_verified_fills_for_all_ten(self):
        for key in STRATEGY_CLASSES:
            with self.subTest(key):
                kite = sh.MockKite()
                legs = self.legs(key, lots=2)
                fills = self.broker(kite).execute(legs, "MIS")
                self.assertEqual(set(fills), {l.contract.symbol for l in legs})
                events = seq(kite)
                last_buy_complete = max(i for i, (k, s, _) in enumerate(events) if k == "complete" and s == "BUY")
                first_sell_place = min(i for i, (k, s, _) in enumerate(events) if k == "place" and s == "SELL")
                first_sell_place_idx = first_sell_place
                self.assertTrue(all(s == "BUY" for k, s, _ in events[:first_sell_place_idx] if k == "place"))
                self.assertLess(last_buy_complete, first_sell_place_idx)     # every BUY verified before any SELL

    def test_ratio_leg_quantity_is_sent_to_the_broker(self):
        kite = sh.MockKite()
        legs = self.legs("bull_call_ratio", lots=2)
        self.broker(kite).execute(legs, "MIS")
        qty = {(s, q) for k, s, _sym, q, _m in kite.log if k == "place"}
        self.assertIn(("SELL", 2 * 2 * legs[0].contract.lot_size), qty)

    def test_sell_leg_rejected_unwinds_longs_and_returns_none(self):
        kite = sh.MockKite()
        legs = self.legs("iron_condor")
        first_sell = next(l for l in legs if l.side == "SELL")
        kite.behaviour[(first_sell.contract.symbol, "SELL")] = "reject"
        self.assertIsNone(self.broker(kite).execute(legs, "MIS"))
        unwinds = [(s, sym) for k, s, sym, *_ in kite.log if k == "place"][len(legs):]
        self.assertEqual(sorted(sym for s, sym in unwinds if s == "SELL"),
                         sorted(l.contract.symbol for l in legs if l.side == "BUY"))

    def test_buy_leg_hang_cancels_and_fires_no_sell(self):
        kite = sh.MockKite()
        legs = self.legs("iron_condor")
        buy = next(l for l in legs if l.side == "BUY")
        kite.behaviour[buy.contract.symbol] = "hang"
        self.assertIsNone(self.broker(kite).execute(legs, "MIS"))
        placed = [(s, sym) for k, s, sym, *_ in kite.log if k == "place"]
        self.assertTrue(any(k == "cancel" for k, *_ in kite.log))
        self.assertFalse(any(s == "SELL" and sym in {l.contract.symbol for l in legs if l.side == "SELL"}
                             for s, sym in placed))                      # no short leg of the structure was fired

    def test_partial_fill_is_cancelled_and_only_the_filled_part_is_unwound(self):
        kite = sh.MockKite()
        legs = self.legs("bull_call_spread")
        buy = next(l for l in legs if l.side == "BUY")
        kite.behaviour[buy.contract.symbol] = "partial"
        self.assertIsNone(self.broker(kite).execute(legs, "MIS"))
        unwind = [(s, q) for k, s, sym, q, _m in kite.log if k == "place" and sym == buy.contract.symbol][-1]
        self.assertEqual(unwind, ("SELL", buy.qty // 2))

    def test_network_timeout_on_first_order_places_nothing_else(self):
        kite = sh.MockKite()
        legs = self.legs("bear_put_spread")
        buy = next(l for l in legs if l.side == "BUY")
        kite.behaviour[buy.contract.symbol] = "raise"
        self.assertIsNone(self.broker(kite).execute(legs, "MIS"))
        self.assertEqual([e for e in kite.log if e[0] == "place"], [])

    def test_exit_reverses_structure_and_buys_back_shorts_first(self):
        kite = sh.MockKite()
        legs = self.legs("iron_condor")
        closing = reverse_legs(legs)
        self.assertEqual([c.side for c in closing], ["SELL" if l.side == "BUY" else "BUY" for l in legs])
        self.assertEqual([c.qty for c in closing], [l.qty for l in legs])
        self.assertTrue(self.broker(kite).execute(closing, "MIS"))
        placed = [s for k, s, *_ in kite.log if k == "place"]
        self.assertEqual(placed, sorted(placed, key=lambda s: s != "BUY"))

    def test_market_escalation_sends_market_orders(self):
        kite = sh.MockKite()
        self.broker(kite).execute(reverse_legs(self.legs("bull_call_spread")), "MIS", market=True)
        self.assertTrue(all(m for k, _s, _sym, _q, m in kite.log if k == "place"))

    def test_margin_gate(self):
        legs = self.legs("iron_condor")
        self.assertTrue(validate_margin(sh.MockKite(), legs, "MIS", 1_000_000).ok)
        low = validate_margin(sh.MockKite(), legs, "MIS", 20_000)
        self.assertFalse(low.ok)
        self.assertIn("needs", low.reason)
        bad = validate_margin(sh.MockKite(margin_fails=True), legs, "MIS", 1_000_000)
        self.assertFalse(bad.ok)                                           # a failed margin query blocks the trade


class SessionTests(unittest.TestCase):
    def test_every_strategy_exits_with_a_rule_and_loss_stays_bounded(self):
        for key, cls in STRATEGY_CLASSES.items():
            plan = sh.plan_from_strategy(cls(), CHAIN)
            for seed, drift in ((1, 0.0), (2, 8.0), (3, -8.0), (4, 0.0)):
                with self.subTest(key=key, seed=seed):
                    r = sh.simulate_session(plan, CHAIN, NOW, BP, SP, seed=seed, drift=drift)
                    self.assertIn(r.reason, {"HARD_STOP", "TAKE_PROFIT", "TRAILING_STOP", "SQUARE_OFF_TIME"})
                    self.assertLessEqual(r.exit_time, dt.time(15, 16))     # never held past the square-off time
                    self.assertGreaterEqual(r.pnl, -1.25 * r.max_loss)     # stop slippage allowance only


if __name__ == "__main__":
    unittest.main()
