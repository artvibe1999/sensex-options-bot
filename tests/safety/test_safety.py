"""Safety: paper never reaches the order endpoints, stale data blocks entries, no duplicate orders, emergency
protection overrides adaptive exits and blocks entries, no secrets in tracked files."""
import os
import re
import subprocess
import tempfile
import unittest

from tests.enginekit import Kit
from tests.helpers import ROOT

from mode_control import startup_mode, write_mode


class ExplodingKite:
    """Any order endpoint call fails the test."""

    def __getattr__(self, name):
        if name in ("place_order", "modify_order", "cancel_order", "order_by_tag", "order_history"):
            raise AssertionError(f"PAPER mode called the live endpoint {name}")
        raise AttributeError(name)


class TestPaperIsolation(unittest.TestCase):
    def test_paper_round_trip_never_touches_order_endpoints(self):
        k = Kit()
        k.engine.exe.broker.client = ExplodingKite()               # PaperBroker has no client; make sure
        k.bar(push_candidate=k.candidate())
        k.step()
        self.assertIsNotNone(k.engine.position)
        k.bar([k.engine.position.invalidation - 40])
        k.step()
        self.assertIsNone(k.engine.position)
        self.assertEqual(k.engine.exe.mode, "PAPER")

    def test_live_requires_master_switch(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "mode.json")
        write_mode(p, "LIVE")
        self.assertEqual(startup_mode(p, {})[0], "PAPER")
        self.assertEqual(startup_mode(p, {"LIVE_TRADING_CONFIRM": "yes"})[0], "PAPER")
        self.assertEqual(startup_mode(p, {"LIVE_TRADING_CONFIRM": "YES"})[0], "LIVE")
        self.assertEqual(startup_mode(os.path.join(d, "missing.json"), {"LIVE_TRADING_CONFIRM": "YES"})[0], "PAPER")

    def test_main_builds_paper_broker_unless_live(self):
        import main
        from sensexpro.execution import LiveBroker, PaperBroker
        src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
        self.assertIn("LiveBroker(self.client, s.execution.live, quote_fn) if live else", src)
        self.assertTrue(issubclass(PaperBroker, object) and LiveBroker.mode == "LIVE" and PaperBroker.mode == "PAPER")
        self.assertTrue(hasattr(main, "Bot"))


class TestEntryGuards(unittest.TestCase):
    def test_stale_quote_blocks_entry(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.clock.advance(30)                                        # quotes are now 30 s old
        k.engine.step(k.now())
        self.assertIsNone(k.engine.position)
        self.assertEqual(len(k.engine.pending), 1)                 # kept until fresh quotes or TTL
        k.engine.step(k.now(200))                                  # TTL passed
        self.assertEqual(k.engine.pending, [])
        self.assertIsNone(k.engine.position)

    def test_stale_underlying_blocks_entry(self):
        k = Kit()
        k.market.spot_px = None
        k.bar(push_candidate=k.candidate())
        k.market.spot_px = None
        k.step()
        self.assertIsNone(k.engine.position)

    def test_duplicate_signal_creates_one_order(self):
        k = Kit()
        c = k.candidate(key="dup")
        k.bar(push_candidate=c)
        k.step()
        k.bar([k.engine.position.invalidation - 40])
        k.step()                                                   # exited
        k.engine.book.last_exit_at = None                          # no cool-down
        k.bar([k.bars[-1].close + 5], push_candidate=c)            # the same setup key again
        k.step()
        self.assertIsNone(k.engine.position)
        entries = [o for o in k.store.orders_on("PAPER", "2000") if o["kind"] == "entry"]
        self.assertEqual(len(entries), 1)

    def test_circuit_breaker_blocks_new_entries(self):
        k = Kit()
        k.engine.book.day_realized = -6000.0                       # daily limit at Rs 2L capital
        k.bar(push_candidate=k.candidate())
        k.step()
        self.assertIsNone(k.engine.position)

    def test_kill_switch_blocks_and_reset_restores(self):
        k = Kit()
        k.engine.kill("test")
        k.bar(push_candidate=k.candidate(key="a"))
        k.step()
        self.assertIsNone(k.engine.position)
        k.engine.reset_kill()
        k.bar([k.bars[-1].close + 5], push_candidate=k.candidate(key="b"))
        k.step()
        self.assertIsNotNone(k.engine.position)


class TestEmergencyOverride(unittest.TestCase):
    def test_emergency_exit_overrides_adaptive_hold_and_latches(self):
        k = Kit(lots=2)
        k.bar(push_candidate=k.candidate())
        k.step()
        pos = k.engine.position
        self.assertIsNotNone(pos)
        k.set_px(120.0, 121.0)                                     # -81 x 40 = -3,240 < -3,000 emergency budget
        k.step()
        self.assertIsNone(k.engine.position)
        self.assertTrue(k.engine.book.kill_latched)
        self.assertIn("trade_loss", k.engine.book.kill_reason)
        rows = k.store.closed_positions("PAPER", "2000")
        self.assertEqual(rows[-1]["exit_reason"], "TRADE_LOSS")
        k.bar([k.bars[-1].close + 5], push_candidate=k.candidate(key="after"))
        k.step()
        self.assertIsNone(k.engine.position)                      # latched: no new entries

    def test_stale_quote_with_open_position_triggers_emergency_after_grace(self):
        k = Kit()
        k.bar(push_candidate=k.candidate())
        k.step()
        k.market._quotes.clear()                                   # feed dies
        for _ in range(3):
            k.clock.advance(15)
            k.engine.step(k.now())
        self.assertGreater(k.market.rest_calls, 0)                 # REST refresh tried first
        self.assertTrue(k.engine.book.kill_latched)
        self.assertIn("stale_quote", k.engine.book.kill_reason)


class TestSecrets(unittest.TestCase):
    PATTERNS = [
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        re.compile(r"\"private_key\"\s*:"),
        re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),                       # Telegram bot token
        re.compile(r"(KITE_API_SECRET|KITE_ACCESS_TOKEN|SHEETS_SECRET|TELEGRAM_BOT_TOKEN)\s*=\s*[A-Za-z0-9]{12,}"),
        re.compile(r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,}"),
    ]

    def test_no_credentials_in_tracked_files(self):
        try:
            files = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("not a git checkout")
        hits = []
        for rel in files.split():
            p = os.path.join(ROOT, rel)
            if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
                continue
            try:
                text = open(p, encoding="utf-8").read()
            except (UnicodeDecodeError, OSError):
                continue
            for pat in self.PATTERNS:
                if pat.search(text):
                    hits.append(f"{rel}: {pat.pattern}")
        self.assertEqual(hits, [])

    def test_secrets_files_are_ignored(self):
        path = os.path.join(ROOT, ".gitignore")
        if not os.path.exists(path):
            self.skipTest("not a git checkout (e.g. inside the Docker image)")
        ig = open(path, encoding="utf-8").read().split()
        for needed in (".env", "data/", "logs/", "*.db", "token.json"):
            self.assertIn(needed, ig)


if __name__ == "__main__":
    unittest.main()
