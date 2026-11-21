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


class BufferRecentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.opted_out: set[int] = set()
        self.buffer = ScoutBuffer(
            max_age_s=900, max_per_chat=50, is_opted_out=lambda user_id: user_id in self.opted_out, clock=self.clock
        )
        for i in range(1, 6):
            self.buffer.add(msg(i, f"line {i}", author=10 + i % 2, at=self.clock.now - 10 + i))
        self.buffer.add(msg(9, "other chat", chat=2))

    def test_the_messages_just_before_an_id_oldest_first(self) -> None:
        self.assertEqual([m.id for m in self.buffer.recent(1, before_id=6, limit=3)], [3, 4, 5])
        self.assertEqual([m.id for m in self.buffer.recent(1, before_id=4, limit=8)], [1, 2, 3])
        self.assertEqual([m.id for m in self.buffer.recent(1, before_id=6, limit=1)], [5])

    def test_the_trigger_itself_and_newer_messages_are_excluded(self) -> None:
        self.assertEqual([m.id for m in self.buffer.recent(1, before_id=5, limit=8)], [1, 2, 3, 4])

    def test_other_chats_unknown_chats_and_empty_limits(self) -> None:
        self.assertEqual([m.id for m in self.buffer.recent(2, before_id=100, limit=5)], [9])
        self.assertEqual(self.buffer.recent(77, before_id=100, limit=5), [])
        self.assertEqual(self.buffer.recent(1, before_id=100, limit=0), [])

    def test_ineligible_messages_stay_as_placeholders_but_opted_out_people_vanish(self) -> None:
        self.buffer.add(msg(6, "[photo]", eligible=False, at=self.clock.now))
        self.opted_out.add(11)
        ids = [m.id for m in self.buffer.recent(1, before_id=7, limit=8)]
        self.assertEqual(ids, [2, 4, 6])  # odd ids are author 11 (opted out)


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

    def test_drop_chat_forgets_everything_in_that_chat_only(self) -> None:
        self.buffer.add(msg(1, chat=1))
        self.buffer.add(msg(2, chat=2))
        self.buffer.drop_chat(1)
        self.buffer.drop_chat(404)  # unknown chats are ignored
        self.assertEqual(self.buffer.chats(), [2])
        self.assertIsNone(self.buffer.get(1, 1))

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


class RunAndWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.opted_out: set[int] = set()
        self.buffer = ScoutBuffer(
            max_age_s=3600, max_per_chat=100, is_opted_out=lambda user_id: user_id in self.opted_out, clock=self.clock
        )
        self.t0 = self.clock.now - 1000

    def add(self, id: int, text: str = "some words", *, author: int = 10, dt: float = 0, **kwargs) -> ScoutMessage:
        message = msg(id, text, author=author, at=self.t0 + dt, **kwargs)
        self.assertTrue(self.buffer.add(message))
        return message

    def ids(self, run: list[ScoutMessage]) -> list[int]:
        return [m.id for m in run]

    def test_run_groups_consecutive_messages_by_one_author_within_7_minutes(self) -> None:
        for i, dt in enumerate((0, 60, 120), start=1):
            self.add(i, dt=dt)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[1, 2, 3]])

    def test_other_author_between_splits_runs(self) -> None:
        self.add(1, author=10, dt=0)
        self.add(2, author=20, dt=10)
        self.add(3, author=10, dt=20)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[1], [2], [3]])

    def test_gap_over_7_minutes_splits_runs(self) -> None:
        self.add(1, dt=0)
        self.add(2, dt=420)  # exactly 7 minutes still groups
        self.add(3, dt=841)  # 421 s after the previous message splits
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[1, 2], [3]])

    def test_ineligible_messages_break_runs_and_are_excluded(self) -> None:
        self.add(1, dt=0)
        self.add(2, "", dt=5, eligible=False)
        self.add(3, dt=10)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[1], [3]])

    def test_long_runs_are_split_at_8_messages(self) -> None:
        for i in range(1, 11):
            self.add(i, dt=i)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [list(range(1, 9)), [9, 10]])

    def test_long_runs_are_split_at_600_characters(self) -> None:
        for i in range(1, 5):
            self.add(i, "x" * 200, dt=i)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[1, 2, 3], [4]])

    def test_deleted_messages_stay_in_runs(self) -> None:
        self.add(1, dt=0)
        self.add(2, dt=5)
        self.buffer.delete(1, 2)
        runs = self.buffer.runs(1)
        self.assertEqual([self.ids(r) for r in runs], [[1, 2]])
        self.assertTrue(runs[0][1].deleted)

    def test_runs_skip_authors_who_opted_out_after_buffering(self) -> None:
        self.add(1, author=10, dt=0)
        self.add(2, author=20, dt=5)
        self.opted_out.add(10)
        self.assertEqual([self.ids(r) for r in self.buffer.runs(1)], [[2]])

    def test_runs_of_an_unknown_chat_are_empty(self) -> None:
        self.assertEqual(self.buffer.runs(99), [])

    def test_window_has_up_to_5_before_and_3_after_contiguous(self) -> None:
        for i in range(1, 13):
            self.add(i, author=10 + (i % 2), dt=i)  # alternating authors: every run is one message
        run = self.buffer.runs(1)[5]
        self.assertEqual(self.ids(run), [6])
        self.assertEqual(self.ids(self.buffer.window(1, run)), [1, 2, 3, 4, 5, 6, 7, 8, 9])

    def test_window_is_shorter_at_the_edges(self) -> None:
        for i in range(1, 4):
            self.add(i, author=10 + (i % 2), dt=i)
        run = self.buffer.runs(1)[0]
        self.assertEqual(self.ids(self.buffer.window(1, run)), [1, 2, 3])

    def test_window_covers_a_multi_message_run(self) -> None:
        self.add(1, author=20, dt=0)
        self.add(2, author=10, dt=5)
        self.add(3, author=10, dt=6)
        self.add(4, author=20, dt=10)
        run = self.buffer.runs(1)[1]
        self.assertEqual(self.ids(run), [2, 3])
        self.assertEqual(self.ids(self.buffer.window(1, run)), [1, 2, 3, 4])

    def test_window_drops_authors_who_opted_out_after_buffering(self) -> None:
        self.add(1, author=20, dt=0)
        self.add(2, author=10, dt=5)
        self.add(3, author=30, dt=10)
        self.opted_out.add(20)
        run = [self.buffer.get(1, 2)]
        self.assertEqual(self.ids(self.buffer.window(1, run)), [2, 3])

    def test_window_keeps_ineligible_messages_as_placeholders(self) -> None:
        self.add(1, "", author=20, dt=0, eligible=False)
        self.add(2, author=10, dt=5)
        run = [self.buffer.get(1, 2)]
        window = self.buffer.window(1, run)
        self.assertEqual(self.ids(window), [1, 2])
        self.assertFalse(window[0].eligible)


if __name__ == "__main__":
    unittest.main()
