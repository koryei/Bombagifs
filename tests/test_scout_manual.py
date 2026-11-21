"""The gate for "clip that" requests: who may be clipped, where, and how often."""

from __future__ import annotations

import unittest

from scout_buffer import ScoutMessage, Version
from scout_config import read_scout_config
from scout_manual import (
    REFUSAL_ALREADY,
    REFUSAL_CANT,
    REFUSAL_NOT_ENABLED,
    REFUSAL_NOT_TEXT,
    REFUSAL_OPTED_OUT,
    REFUSAL_SLOW,
    ManualGate,
)

SELF_ID = 1000


class FakeClock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


def msg(id: int, text: str = "the better one was me", *, author: int = 10, eligible: bool = True, chat: int = 1) -> ScoutMessage:
    return ScoutMessage(
        id=id, chat_id=chat, author_id=author, author_name=f"user{author}", avatar_url="", color=None, tag=None,
        created_at=1.0, versions=[Version(text if eligible else "", 1.0)], eligible=eligible,
    )


class GateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.gate = ManualGate(read_scout_config({}), clock=self.clock)
        self.optout: set[int] = set()

    def check(self, chosen=None, *, requester: int = 50, owner: bool = False, enabled: bool = True, blocklist=frozenset(), bot: bool = False):
        return self.gate.check(
            chosen if chosen is not None else [msg(1)],
            requester_id=requester, is_owner=owner, chat_enabled=enabled, optout=self.optout,
            blocklist=blocklist, self_id=SELF_ID, target_is_bot=bot,
        )


class RefusalTests(GateCase):
    def test_a_normal_request_is_allowed(self) -> None:
        self.assertIsNone(self.check())

    def test_nothing_to_clip(self) -> None:
        self.assertEqual(self.check([]), REFUSAL_CANT)

    def test_bots_and_the_account_itself_cannot_be_clipped(self) -> None:
        self.assertEqual(self.check(bot=True), REFUSAL_CANT)
        self.assertEqual(self.check([msg(1, author=SELF_ID)]), REFUSAL_CANT)

    def test_opted_out_authors_are_refused(self) -> None:
        self.optout.add(10)
        self.assertEqual(self.check(), REFUSAL_OPTED_OUT)
        self.assertEqual(self.check([msg(1, author=20), msg(2, author=10)]), REFUSAL_OPTED_OUT)  # anyone in the range

    def test_only_plain_text_can_be_clipped(self) -> None:
        self.assertEqual(self.check([msg(1, eligible=False)]), REFUSAL_NOT_TEXT)
        self.assertEqual(self.check([msg(1), msg(2, eligible=False)]), REFUSAL_NOT_TEXT)

    def test_total_length_is_capped_at_600_characters(self) -> None:
        self.assertIsNone(self.check([msg(i, "x" * 200) for i in (1, 2, 3)]))
        self.assertEqual(self.check([msg(i, "x" * 200) for i in (1, 2, 3, 4)]), REFUSAL_NOT_TEXT)

    def test_sensitive_text_is_refused_generically(self) -> None:
        for text in ("call me on 415-555-0132 lol", "mail me at a@b.co please", "key sk-abcdefghijklmnop12345678 haha"):
            with self.subTest(text=text):
                self.assertEqual(self.check([msg(1, text)]), REFUSAL_CANT)

    def test_blocklisted_terms_are_refused(self) -> None:
        self.assertEqual(self.check([msg(1, "what a badword moment")], blocklist=frozenset({"badword"})), REFUSAL_CANT)

    def test_refusals_never_quote_the_message(self) -> None:
        secret = "call me on 415-555-0132 lol"
        self.assertNotIn("415", self.check([msg(1, secret)]) or "")
        self.assertNotIn("user10", self.check([msg(1, secret)]) or "")

    def test_members_need_an_enabled_chat_but_the_owner_does_not(self) -> None:
        self.assertEqual(self.check(enabled=False), REFUSAL_NOT_ENABLED)
        self.assertIsNone(self.check(enabled=False, owner=True))

    def test_the_order_of_checks(self) -> None:
        self.optout.add(10)
        # opted out beats everything below it
        self.assertEqual(self.check([msg(1, "call 415-555-0132 now", eligible=True)], enabled=False), REFUSAL_OPTED_OUT)
        self.optout.clear()
        # not-text beats safety; safety beats not-enabled
        self.assertEqual(self.check([msg(1, eligible=False)], enabled=False), REFUSAL_NOT_TEXT)
        self.assertEqual(self.check([msg(1, "call 415-555-0132 now")], enabled=False), REFUSAL_CANT)
        # bot beats opt-out
        self.optout.add(10)
        self.assertEqual(self.check(bot=True), REFUSAL_CANT)


class LimitTests(GateCase):
    def test_already_clipped_messages_are_refused(self) -> None:
        chosen = [msg(1)]
        self.assertIsNone(self.check(chosen))
        self.gate.commit(chosen, requester_id=50, is_owner=False)
        self.assertEqual(self.check([msg(1)]), REFUSAL_ALREADY)  # same chat + id, even as a fresh object

    def test_a_buffered_message_flagged_clipped_is_refused(self) -> None:
        message = msg(1)
        message.clipped = True
        self.assertEqual(self.check([message]), REFUSAL_ALREADY)

    def test_commit_flags_the_messages(self) -> None:
        chosen = [msg(1), msg(2)]
        self.gate.commit(chosen, requester_id=50, is_owner=False)
        self.assertTrue(all(m.clipped for m in chosen))

    def test_a_requester_gets_three_clips_per_ten_minutes(self) -> None:
        for i in range(3):
            chosen = [msg(i + 1, author=10 + i)]
            self.assertIsNone(self.check(chosen))
            self.gate.commit(chosen, requester_id=50, is_owner=False)
        self.assertEqual(self.check([msg(9, author=99)]), REFUSAL_SLOW)
        self.assertIsNone(self.check([msg(9, author=99)], requester=51))  # another requester is unaffected
        self.clock.now += 601
        self.assertIsNone(self.check([msg(9, author=99)]))

    def test_one_clip_per_author_per_two_minutes(self) -> None:
        chosen = [msg(1, author=10)]
        self.gate.commit(chosen, requester_id=50, is_owner=False)
        self.assertEqual(self.check([msg(2, author=10)], requester=51), REFUSAL_SLOW)
        self.assertIsNone(self.check([msg(2, author=20)], requester=51))  # a different author is fine
        self.clock.now += 121
        self.assertIsNone(self.check([msg(2, author=10)], requester=51))

    def test_the_owner_is_exempt_from_both_limits(self) -> None:
        for i in range(10):
            chosen = [msg(i + 1, author=10)]
            self.assertIsNone(self.check(chosen, requester=7, owner=True))
            self.gate.commit(chosen, requester_id=7, is_owner=True)

    def test_owner_clips_still_start_the_author_cooldown_for_members(self) -> None:
        self.gate.commit([msg(1, author=10)], requester_id=7, is_owner=True)
        self.assertEqual(self.check([msg(2, author=10)]), REFUSAL_SLOW)

    def test_limits_follow_the_configuration(self) -> None:
        gate = ManualGate(read_scout_config({"SCOUT_MANUAL_LIMIT_PER_10_MIN": "1", "SCOUT_MANUAL_AUTHOR_COOLDOWN_SECONDS": "10"}), clock=self.clock)
        chosen = [msg(1, author=10)]
        gate.commit(chosen, requester_id=50, is_owner=False)
        kwargs = dict(requester_id=50, is_owner=False, chat_enabled=True, optout=self.optout, blocklist=frozenset(), self_id=SELF_ID)
        self.assertEqual(gate.check([msg(2, author=20)], **kwargs), REFUSAL_SLOW)
        self.clock.now += 601
        self.assertIsNone(gate.check([msg(2, author=10)], **kwargs))

    def test_the_clipped_id_memory_is_bounded(self) -> None:
        for i in range(1500):
            self.gate.commit([msg(i, author=i)], requester_id=7, is_owner=True)
        self.assertEqual(self.check([msg(1499, author=5000)], owner=True), REFUSAL_ALREADY)  # recent ids are remembered
        self.assertIsNone(self.check([msg(0, author=5001)], owner=True))  # the oldest were forgotten


if __name__ == "__main__":
    unittest.main()
