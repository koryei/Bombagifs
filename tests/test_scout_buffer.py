"""The scout's in-memory message buffer: versions, deletions, expiry, signals, runs, windows."""

from __future__ import annotations

import unittest

from scout_buffer import ScoutBuffer, ScoutMessage, Version, has_laughter


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def msg(
    id: int,
    text: str = "hello there",
    *,
    chat: int = 1,
    author: int = 10,
    at: float = 1_000_000.0,
    eligible: bool = True,
    name: str | None = None,
) -> ScoutMessage:
    return ScoutMessage(
        id=id,
        chat_id=chat,
        author_id=author,
        author_name=name or f"user{author}",
        avatar_url=f"https://cdn.discordapp.com/avatars/{author}/a.png",
        color=None,
        tag=None,
        created_at=at,
        versions=[Version(text, at)],
        eligible=eligible,
    )


class BufferCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.opted_out: set[int] = set()
        self.buffer = ScoutBuffer(
            max_age_s=900, max_per_chat=5, is_opted_out=lambda user_id: user_id in self.opted_out, clock=self.clock
        )

    def test_add_and_get(self) -> None:
        self.assertTrue(self.buffer.add(msg(1)))
        found = self.buffer.get(1, 1)
        self.assertIsNotNone(found)
        self.assertEqual(found.original, "hello there")
        self.assertFalse(found.deleted)
        self.assertEqual(self.buffer.chats(), [1])

    def test_duplicate_id_is_not_added(self) -> None:
        self.assertTrue(self.buffer.add(msg(1, "first")))
        self.assertFalse(self.buffer.add(msg(1, "second")))
        self.assertEqual(self.buffer.get(1, 1).original, "first")

    def test_opted_out_author_is_never_stored(self) -> None:
        self.opted_out.add(10)
        self.assertFalse(self.buffer.add(msg(1, author=10)))
        self.assertIsNone(self.buffer.get(1, 1))
        self.assertEqual(self.buffer.chats(), [])

    def test_edit_appends_a_version_and_keeps_original(self) -> None:
        self.buffer.add(msg(1, "the joke"))
        self.clock.now += 5
        self.buffer.edit(1, 1, "nothing to see")
        found = self.buffer.get(1, 1)
        self.assertEqual(found.original, "the joke")
        self.assertEqual([v.content for v in found.versions], ["the joke", "nothing to see"])
        self.assertEqual(found.versions[-1].at, self.clock.now)

    def test_delete_keeps_content_and_sets_deleted_at(self) -> None:
        self.buffer.add(msg(1, "oops"))
        self.clock.now += 3
        self.buffer.delete(1, 1)
        found = self.buffer.get(1, 1)
        self.assertTrue(found.deleted)
        self.assertEqual(found.deleted_at, self.clock.now)
        self.assertEqual(found.original, "oops")

    def test_delete_many(self) -> None:
        for i in (1, 2, 3):
            self.buffer.add(msg(i))
        self.buffer.delete_many(1, [1, 3, 99])
        self.assertEqual([self.buffer.get(1, i).deleted for i in (1, 2, 3)], [True, False, True])

    def test_events_for_unknown_messages_are_ignored(self) -> None:
        self.buffer.edit(1, 404, "x")
        self.buffer.delete(1, 404)
        self.buffer.delete_many(7, [1, 2])
        self.assertEqual(self.buffer.chats(), [])
        self.assertIsNone(self.buffer.get(1, 404))

    def test_messages_expire_after_max_age(self) -> None:
        self.buffer.add(msg(1))
        self.clock.now += 899
        self.assertIsNotNone(self.buffer.get(1, 1))
        self.clock.now += 2
        self.assertIsNone(self.buffer.get(1, 1))
        self.assertEqual(self.buffer.chats(), [])

    def test_per_chat_cap_drops_oldest(self) -> None:
        for i in range(1, 8):
            self.buffer.add(msg(i))
        self.assertIsNone(self.buffer.get(1, 1))
        self.assertIsNone(self.buffer.get(1, 2))
        self.assertIsNotNone(self.buffer.get(1, 3))
        self.assertIsNotNone(self.buffer.get(1, 7))

    def test_chats_are_independent(self) -> None:
        self.buffer.add(msg(1, chat=1))
        self.buffer.add(msg(1, chat=2))
        self.assertEqual(sorted(self.buffer.chats()), [1, 2])


class LaughSignalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.buffer = ScoutBuffer(max_age_s=900, max_per_chat=50, is_opted_out=lambda _: False, clock=self.clock)

    def test_react_counts_only_laugh_emoji_and_can_decrement(self) -> None:
        self.buffer.add(msg(1))
        self.buffer.react(1, 1, "😂", True)
        self.buffer.react(1, 1, "💀", True)
        self.buffer.react(1, 1, "❤️", True)
        self.assertEqual(self.buffer.get(1, 1).laugh_reactions, 2)
        self.buffer.react(1, 1, "😂", False)
        self.buffer.react(1, 1, "😂", False)
        self.buffer.react(1, 1, "💀", False)
        self.assertEqual(self.buffer.get(1, 1).laugh_reactions, 0)  # never below zero

    def test_react_on_unknown_message_is_ignored(self) -> None:
        self.buffer.react(1, 404, "😂", True)
        self.assertEqual(self.buffer.chats(), [])

    def test_has_laughter(self) -> None:
        for text in ("LMAO", "im dead", "💀", "bro 😭", "lmfao what", "😂😂"):
            with self.subTest(text=text):
                self.assertTrue(has_laughter(text))
        for text in ("hello there", "that is a deadline", "", "lol maybe"):
            with self.subTest(text=text):
                self.assertFalse(has_laughter(text))

    def test_laugh_reply_credits_recent_messages_by_other_authors(self) -> None:
        now = self.clock.now
        self.buffer.add(msg(1, "first thing", author=10, at=now - 20))
        self.buffer.add(msg(2, "second thing", author=10, at=now - 10))
        self.buffer.add(msg(3, "my own line", author=20, at=now - 5))
        self.buffer.add(msg(4, "lmao", author=20, at=now))
        self.assertEqual(self.buffer.get(1, 1).laugh_replies, 1)
        self.assertEqual(self.buffer.get(1, 2).laugh_replies, 1)
        self.assertEqual(self.buffer.get(1, 3).laugh_replies, 0)  # the laugher's own message
        self.assertEqual(self.buffer.get(1, 4).laugh_replies, 0)

    def test_old_messages_are_not_credited(self) -> None:
        now = self.clock.now
        self.buffer.add(msg(1, "old", author=10, at=now - 120))
        self.buffer.add(msg(2, "lmao", author=20, at=now))
        self.assertEqual(self.buffer.get(1, 1).laugh_replies, 0)

    def test_only_the_three_previous_messages_are_credited(self) -> None:
        now = self.clock.now
        for i in range(1, 6):
            self.buffer.add(msg(i, f"line {i}", author=10, at=now - 30 + i))
        self.buffer.add(msg(6, "dead", author=20, at=now))
        self.assertEqual([self.buffer.get(1, i).laugh_replies for i in range(1, 6)], [0, 0, 1, 1, 1])

    def test_non_laughing_reply_credits_nothing(self) -> None:
        now = self.clock.now
        self.buffer.add(msg(1, "joke", author=10, at=now - 5))
        self.buffer.add(msg(2, "interesting", author=20, at=now))
        self.assertEqual(self.buffer.get(1, 1).laugh_replies, 0)


if __name__ == "__main__":
    unittest.main()
