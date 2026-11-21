"""Opt-out list, enabled-chat store, and the clip ledger."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from scout_stores import ClipLedger, ClipRecord, OptOutStore, ScoutChatStore


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class OptOutStoreTests(TempDirCase):
    def test_optout_store_persists_and_mode_600(self) -> None:
        path = self.dir / "optout.json"
        store = OptOutStore(path)
        self.assertTrue(store.add(42))
        self.assertFalse(store.add(42))
        self.assertIn(42, OptOutStore(path))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(store.remove(42))
        self.assertNotIn(42, OptOutStore(path))

    def test_optout_corrupt_file_moved_aside(self) -> None:
        path = self.dir / "optout.json"
        path.write_text("{nope", encoding="utf-8")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            store = OptOutStore(path)
        self.assertEqual(len(store), 0)
        self.assertTrue(path.with_name("optout.json.corrupt").exists())


class ScoutChatStoreTests(TempDirCase):
    def test_chat_store_enable_disable_persist(self) -> None:
        path = self.dir / "scout-chats.json"
        store = ScoutChatStore(path)
        self.assertFalse(store.enabled(5))
        self.assertTrue(store.enable(5, "friends gc"))
        self.assertFalse(store.enable(5))
        self.assertTrue(ScoutChatStore(path).enabled(5))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(store.disable(5))
        self.assertFalse(store.disable(5))
        self.assertFalse(ScoutChatStore(path).enabled(5))

    def test_seeded_chats_are_enabled_and_cannot_be_disabled(self) -> None:
        store = ScoutChatStore(self.dir / "scout-chats.json", seed=[7, 8])
        self.assertTrue(store.enabled(7))
        self.assertTrue(store.is_seeded(7))
        self.assertFalse(store.enable(7))  # already enabled
        self.assertFalse(store.disable(7))
        self.assertTrue(store.enabled(7))
        self.assertFalse(store.is_seeded(5))

    def test_chat_store_list_includes_names_and_seeds(self) -> None:
        store = ScoutChatStore(self.dir / "scout-chats.json", seed=[7])
        store.enable(5, "friends gc")
        self.assertEqual(store.list(), [(5, "friends gc"), (7, "")])

    def test_corrupt_chat_file_is_moved_aside_and_starts_empty(self) -> None:
        path = self.dir / "scout-chats.json"
        path.write_text("[1, 2", encoding="utf-8")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            store = ScoutChatStore(path)
        self.assertEqual(store.list(), [])
        self.assertTrue(path.with_name("scout-chats.json.corrupt").exists())


def record(**overrides: object) -> ClipRecord:
    fields: dict[str, object] = dict(
        at=1000.0,
        chat_id=1,
        message_ids=(11, 12),
        author_ids=(5,),
        mode="posted",
        funny=8.5,
        decide={"funny": 0.9, "standalone": 0.85, "sensitive": 0.01},
        deleted=False,
        provider="inception",
    )
    fields.update(overrides)
    return ClipRecord(**fields)  # type: ignore[arg-type]


class ClipLedgerTests(TempDirCase):
    def test_ledger_appends_json_lines_mode_600(self) -> None:
        path = self.dir / "clips.jsonl"
        ledger = ClipLedger(path)
        ledger.append(record(posted_message_id=900, text=("a line",)))
        ledger.append(record(mode="dry-run", message_ids=(13,)))
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual((first["mode"], first["chat_id"], first["message_ids"], first["posted_message_id"]), ("posted", 1, [11, 12], 900))
        self.assertEqual(first["decide"]["funny"], 0.9)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ledger_text_only_when_given(self) -> None:
        path = self.dir / "clips.jsonl"
        ClipLedger(path).append(record(mode="dry-run"))
        self.assertIsNone(json.loads(path.read_text(encoding="utf-8"))["text"])

    def test_recent_posted_ids_reads_only_the_last_n_lines(self) -> None:
        ledger = ClipLedger(self.dir / "clips.jsonl")
        for i in range(5):
            ledger.append(record(posted_message_id=100 + i))
        self.assertEqual(ledger.recent_posted_ids(limit=3), {102, 103, 104})
        self.assertEqual(ledger.recent_posted_ids(limit=500), {100, 101, 102, 103, 104})

    def test_recent_posted_ids_skips_unposted_and_corrupt_lines(self) -> None:
        path = self.dir / "clips.jsonl"
        ledger = ClipLedger(path)
        ledger.append(record(posted_message_id=100))
        ledger.append(record(mode="dry-run"))  # no posted id
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json\n\n{\"posted_message_id\": \"7\"}\n")  # junk and a wrong type
        self.assertEqual(ledger.recent_posted_ids(), {100})

    def test_find_posted_returns_the_record_with_text_and_speakers(self) -> None:
        ledger = ClipLedger(self.dir / "clips.jsonl")
        ledger.append(record(posted_message_id=100, text=("first line",), speakers=("A",)))
        ledger.append(record(posted_message_id=101, text=("one", "two"), speakers=("A", "B"), funny=9.1))
        found = ledger.find_posted(101)
        self.assertEqual((found.posted_message_id, found.text, found.speakers, found.funny), (101, ("one", "two"), ("A", "B"), 9.1))
        self.assertEqual(found.decide["funny"], 0.9)
        self.assertEqual(ledger.find_posted(100).text, ("first line",))

    def test_find_posted_handles_old_rows_missing_junk_and_unknown_ids(self) -> None:
        path = self.dir / "clips.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            handle.write('{"at": 1, "chat_id": 1, "message_ids": [1], "author_ids": [2], "mode": "posted", "funny": 8.0, "decide": {}, "deleted": false, "provider": "x", "posted_message_id": 55, "text": ["old row"]}\n')
            handle.write("not json\n\n")
            handle.write('{"posted_message_id": 56}\n')
        ledger = ClipLedger(path)
        old = ledger.find_posted(55)
        self.assertEqual((old.text, old.speakers), (("old row",), None))
        self.assertIsNone(ledger.find_posted(56))  # incomplete row
        self.assertIsNone(ledger.find_posted(999))
        self.assertIsNone(ClipLedger(self.dir / "missing.jsonl").find_posted(1))

    def test_find_posted_only_looks_at_recent_lines(self) -> None:
        ledger = ClipLedger(self.dir / "clips.jsonl")
        for i in range(30):
            ledger.append(record(posted_message_id=1000 + i))
        self.assertIsNotNone(ledger.find_posted(1000, limit=30))
        self.assertIsNone(ledger.find_posted(1000, limit=10))

    def test_recent_posted_ids_of_a_missing_file_is_empty(self) -> None:
        self.assertEqual(ClipLedger(self.dir / "none.jsonl").recent_posted_ids(), set())


if __name__ == "__main__":
    unittest.main()
