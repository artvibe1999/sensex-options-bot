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


class ValidationAndExitTests(unittest.TestCase):
    def setUp(self):
        import copy
        self.chain = copy.copy(CHAIN)
        self.chain.quotes = dict(CHAIN.quotes)
        self.plan = sh.plan_from_strategy(STRATEGY_CLASSES["iron_condor"](), self.chain)

    def _depth(self, bid_f=0.99, ask_f=1.01, only=None):
        import dataclasses
        for l in self.plan.legs:
            k = (l.contract.strike, l.contract.kind)
            q = self.chain.quotes[k]
            if only is None or l.contract.symbol == only:
                self.chain.quotes[k] = dataclasses.replace(q, bid=q.ltp * bid_f, ask=q.ltp * ask_f)

    def test_valid_structure_passes_with_and_without_depth(self):
        from execution import validate_plan_quotes
        self.assertTrue(validate_plan_quotes(self.plan, self.chain).ok)
        self._depth()
        self.assertTrue(validate_plan_quotes(self.plan, self.chain).ok)

    def test_missing_wide_or_one_sided_quotes_are_rejected(self):
        from execution import validate_plan_quotes
        sell = next(l for l in self.plan.legs if l.side == "SELL")
        del self.chain.quotes[(sell.contract.strike, sell.contract.kind)]
        self.assertFalse(validate_plan_quotes(self.plan, self.chain).ok)
        self.setUp()
        self._depth(0.80, 1.20)                                            # 40% wide spread
        qc = validate_plan_quotes(self.plan, self.chain)
        self.assertFalse(qc.ok)
        self.assertIn("spread", qc.reason)
        self.setUp()
        self._depth(0.0, 1.01, only=sell.contract.symbol)                  # SELL leg with no bid cannot be exited
        self.assertFalse(validate_plan_quotes(self.plan, self.chain).ok)

    def test_executable_prices_that_break_the_risk_budget_are_rejected(self):
        from execution import validate_plan_quotes
        self._depth(0.90, 1.10, only=None)                                 # 20% spread but within 8%? no: rejected
        self.assertFalse(validate_plan_quotes(self.plan, self.chain, max_spread_pct=50.0, max_loss_slack=1.0).ok)

    def test_exit_never_sells_longs_while_a_buyback_is_unconfirmed(self):
        kite = sh.MockKite()
        legs = reverse_legs(sh.order_legs(sh.plan_from_strategy(STRATEGY_CLASSES["iron_condor"](), CHAIN)))
        buy = next(l for l in legs if l.side == "BUY")
        kite.behaviour[buy.contract.symbol] = "hang"
        res = LiveBroker(kite, sh.fast_cfg(), sleep=lambda _s: None).close(legs, "MIS")
        placed = [(s, sym) for k, s, sym, *_ in kite.log if k == "place"]
        self.assertFalse(any(s == "SELL" for s, _ in placed))              # longs untouched: hedge stays on
        self.assertNotIn(buy.contract.symbol, res)

    def test_partial_exit_keeps_closed_legs_closed_and_reports_the_rest(self):
        from execution import apply_exit_fills
        from risk_manager import OpenLeg
        kite = sh.MockKite()
        plan = sh.plan_from_strategy(STRATEGY_CLASSES["iron_condor"](), CHAIN)
        entry = sh.order_legs(plan)
        open_legs = [OpenLeg(l.contract, l.side, l.qty, l.ref_price) for l in entry]
        closing = reverse_legs(entry)
        stuck = next(l for l in closing if l.side == "SELL")
        kite.behaviour[(stuck.contract.symbol, "SELL")] = "hang"
        res = LiveBroker(kite, sh.fast_cfg(), sleep=lambda _s: None).close(closing, "MIS")
        gross, cash = apply_exit_fills(open_legs, res)
        left = [l for l in open_legs if l.qty > 0]
        self.assertEqual([l.contract.symbol for l in left], [stuck.contract.symbol])
        self.assertEqual(left[0].side, "BUY")                              # only a long (limited-risk) leg remains
        self.assertEqual(len(cash), 2 * (len(entry) - 1))

    def test_complete_with_fewer_lots_than_ordered_is_not_trusted(self):
        kite = sh.MockKite()
        legs = sh.order_legs(sh.plan_from_strategy(STRATEGY_CLASSES["bull_call_spread"](), CHAIN))
        orig = kite.order_history
        def short(oid):
            h = orig(oid)
            return [{**h[0], "filled_quantity": h[0]["filled_quantity"] - 1}] if h[0]["status"] == "COMPLETE" else h
        kite.order_history = short
        self.assertIsNone(LiveBroker(kite, sh.fast_cfg(), sleep=lambda _s: None).execute(legs, "MIS"))


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
