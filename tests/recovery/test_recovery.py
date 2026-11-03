"""Restart safety: open positions, in-flight orders, risk counters, partial exits, broker reconciliation."""
import datetime as dt
import json
import os
import unittest

from tests.enginekit import TODAY, Kit

from sensexpro.execution import CANCELLED


class TestRecovery(unittest.TestCase):
    def test_restart_keeps_open_position_and_counters(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        pos = k.engine.position
        k.engine.book.latch("test latch", k.now())
        k.engine._save()
        k2 = Kit(store=k.store, tmpdir=k.dir)                      # a fresh process on the same journal
        p2 = k2.engine.position
        self.assertIsNotNone(p2)
        self.assertEqual((p2.id, p2.qty, p2.entry_price), (pos.id, pos.qty, pos.entry_price))
        self.assertEqual(k2.engine.book.entries_today, 1)
        self.assertTrue(k2.engine.book.kill_latched)
        with open(os.path.join(k.dir, "state.json"), encoding="utf-8") as f:
            self.assertIsNotNone(json.load(f)["position"])          # the VM autostop guard sees it

    def test_in_flight_order_and_pending_position_after_crash(self):
        k = Kit()
        from sensexpro.models import Position
        c = k.contracts[0]
        pos = Position("crash-1", "SENSEX", c, 1, 0, 200.0, k.now(), "orb", "CE", "k", 85.0, 1.0, 1.0, 0.5, 0.0,
                       status="PENDING")
        k.store.save_position("PAPER", pos)
        k.store.order_intent({"client_id": "SP261029E9999", "mode": "PAPER", "position_id": "crash-1",
                              "index": "SENSEX", "symbol": c.symbol, "token": c.token, "exchange": "BFO",
                              "side": "BUY", "kind": "entry", "requested_qty": 20})
        k.store.order_update("SP261029E9999", "SUBMITTED")
        k2 = Kit(store=k.store, tmpdir=k.dir)
        self.assertIsNone(k2.engine.position)                      # never filled: voided, not "assumed"
        self.assertEqual(k.store.order("SP261029E9999")["state"], CANCELLED)
        self.assertFalse(k2.engine.book.kill_latched)
        self.assertEqual(k.store.open_orders("PAPER"), [])

    def test_partial_exit_fills_booked_exactly_once(self):
        k = Kit(lots=2)
        k.bar(push_candidate=k.candidate())
        k.step()
        pos = k.engine.position
        # an exit fill journaled, then the process died before the risk book was saved
        k.store.order_intent({"client_id": "SP261029X9999", "mode": "PAPER", "position_id": pos.id,
                              "index": "SENSEX", "symbol": pos.contract.symbol, "token": pos.contract.token,
                              "exchange": "BFO", "side": "SELL", "kind": "exit", "requested_qty": 20})
        k.store.fill("SP261029X9999", 20, 180.0)
        k.store.order_update("SP261029X9999", "FILLED", filled_qty=20, avg_price=180.0)
        day0 = k.engine.book.day_realized
        k2 = Kit(store=k.store, tmpdir=k.dir, lots=2)
        p2 = k2.engine.position
        self.assertEqual(p2.qty, 20)
        booked = k2.engine.book.day_realized - day0
        self.assertLess(booked, 0)
        k3 = Kit(store=k.store, tmpdir=k.dir, lots=2)               # restart again: no double count
        self.assertAlmostEqual(k3.engine.book.day_realized, k2.engine.book.day_realized, places=2)

    def test_reconnect_reconciliation_keeps_position(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        before = k.engine.position.qty
        k.engine.recover(TODAY)                                    # WebSocket reconnect path
        self.assertEqual(k.engine.position.qty, before)
        self.assertTrue(k.engine.reconciled)

    def test_live_broker_mismatch_latches_kill_switch(self):
        k = Kit(mode="LIVE", positions_fn=lambda: {"SENSEXFOO": 20})
        self.assertTrue(k.engine.book.kill_latched)
        self.assertTrue(any(lvl == "CRITICAL" for lvl, _ in k.notifier.alerts))

    def test_stale_position_from_previous_day_is_closed_in_journal(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        nxt = Kit(store=k.store, tmpdir=k.dir)
        nxt.engine.start_session(200_000.0, TODAY + dt.timedelta(days=1))
        self.assertIsNone(nxt.engine.position)
        rows = k.store.closed_positions("PAPER", "2000")
        self.assertTrue(rows and rows[-1]["exit_reason"].startswith("STALE_FROM_"))

    def test_daily_reset_and_weekly_persistence(self):
        k = Kit()
        b = k.engine.book
        b.day_realized, b.week_realized, b.entries_today = -1000.0, -1000.0, 2
        k.engine._save()
        k.engine.new_day(TODAY + dt.timedelta(days=1))
        self.assertEqual((b.day_realized, b.entries_today), (0.0, 0))
        self.assertEqual(b.week_realized, -1000.0)


if __name__ == "__main__":
    unittest.main()
