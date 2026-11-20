"""Discord-free self-bot logic: allowlist, rate limiting, decisions, media collection, copy."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path

from selfbot_logic import AllowlistStore, RateLimiter


class AllowlistStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "allowlist.json"

    def test_missing_file_is_empty(self) -> None:
        store = AllowlistStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertNotIn(42, store)

    def test_add_persists_across_instances(self) -> None:
        AllowlistStore(self.path).add(42)
        self.assertIn(42, AllowlistStore(self.path))

    def test_add_twice_returns_false(self) -> None:
        store = AllowlistStore(self.path)
        self.assertTrue(store.add(42))
        self.assertFalse(store.add(42))

    def test_remove_persists(self) -> None:
        store = AllowlistStore(self.path)
        store.add(42)
        self.assertTrue(store.remove(42))
        self.assertFalse(store.remove(42))
        self.assertNotIn(42, AllowlistStore(self.path))

    def test_file_mode_is_600(self) -> None:
        AllowlistStore(self.path).add(42)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corrupt_file_is_moved_aside(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            store = AllowlistStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertTrue(self.path.with_name("allowlist.json.corrupt").exists())
        self.assertFalse(self.path.exists())


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = RateLimiter(limit=5, window=60.0, clock=self.clock)

    def test_allows_up_to_limit_then_blocks(self) -> None:
        self.assertEqual([self.limiter.allow(1) for _ in range(5)], [True] * 5)
        self.assertFalse(self.limiter.allow(1))

    def test_window_slides(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.clock.now += 61
        self.assertTrue(self.limiter.allow(1))

    def test_users_are_independent(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.assertTrue(self.limiter.allow(2))

    def test_blocked_attempts_do_not_extend_window(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.clock.now += 30
        self.assertFalse(self.limiter.allow(1))
        self.clock.now += 31  # 61 s after the first five hits; the blocked try must not count
        self.assertTrue(self.limiter.allow(1))


if __name__ == "__main__":
    unittest.main()
