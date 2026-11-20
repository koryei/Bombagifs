"""Scout judge: anonymised windows, verdict validation, provider clients, and the fallback chain."""

from __future__ import annotations

import json
import math
import unittest

from scout_buffer import ScoutMessage, Version
from scout_judge import (
    SENSITIVE_FLAGS,
    HeuristicJudge,
    JudgeWindow,
    build_judge_window,
    validate_verdict,
    window_payload,
)

NAMES = {10: "Pranit", 20: "Koryei", 30: "Shan", 40: "Lochan"}


def msg(id: int, text: str, *, author: int = 10, eligible: bool = True, deleted: bool = False, edits: tuple[str, ...] = ()) -> ScoutMessage:
    versions = [Version(text, 1000.0 + id)] + [Version(e, 1100.0 + id) for e in edits]
    return ScoutMessage(
        id=id,
        chat_id=1,
        author_id=author,
        author_name=NAMES[author],
        avatar_url=f"https://cdn.discordapp.com/avatars/{author}/hash{author}.png",
        color=None,
        tag=None,
        created_at=1000.0 + id,
        versions=versions,
        eligible=eligible,
        deleted_at=1200.0 if deleted else None,
    )


def window_of(messages: list[ScoutMessage], run: list[ScoutMessage] | None = None) -> JudgeWindow:
    return build_judge_window(messages, run or messages[:1])


def good_raw(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "clip": True,
        "start": 0,
        "end": 1,
        "funny": 8.5,
        "standalone": True,
        "sensitive": [],
        "reason": "setup and punchline",
    }
    raw.update(overrides)
    return raw


class JudgeWindowTests(unittest.TestCase):
    def test_speakers_are_labelled_by_first_appearance(self) -> None:
        messages = [msg(1, "hi", author=20), msg(2, "yo", author=10), msg(3, "again", author=20), msg(4, "hey", author=30)]
        window = window_of(messages)
        self.assertEqual([line.speaker for line in window.lines], ["A", "B", "A", "C"])

    def test_payload_has_no_names_ids_or_avatars(self) -> None:
        messages = [msg(1, "hello world", author=10), msg(2, "second", author=20)]
        payload = json.dumps(window_payload(window_of(messages)))
        for needle in ("Pranit", "Koryei", "avatars", "cdn.discordapp", "hash10", "hash20", '"id"'):
            self.assertNotIn(needle, payload)
        self.assertEqual(
            window_payload(window_of(messages)),
            [
                {"i": 0, "speaker": "A", "text": "hello world", "deleted": False},
                {"i": 1, "speaker": "B", "text": "second", "deleted": False},
            ],
        )

    def test_payload_uses_original_text_not_edits(self) -> None:
        window = window_of([msg(1, "the real joke", edits=("nothing to see",))])
        self.assertEqual(window.lines[0].text, "the real joke")
        self.assertNotIn("nothing to see", json.dumps(window_payload(window)))

    def test_ineligible_lines_become_not_text_placeholders(self) -> None:
        messages = [msg(1, "", author=20, eligible=False), msg(2, "text here", author=10)]
        window = window_of(messages, messages[1:])
        self.assertEqual((window.lines[0].speaker, window.lines[0].text), ("-", "[not text]"))
        self.assertEqual(window.lines[1].speaker, "A")  # placeholders do not consume a speaker label

    def test_deleted_flag_is_carried(self) -> None:
        window = window_of([msg(1, "oops", deleted=True), msg(2, "fine")])
        self.assertEqual([line.deleted for line in window.lines], [True, False])

    def test_run_indexes_and_messages_are_kept(self) -> None:
        messages = [msg(1, "before", author=20), msg(2, "run a", author=10), msg(3, "run b", author=10), msg(4, "after", author=20)]
        window = build_judge_window(messages, messages[1:3])
        self.assertEqual((window.run_start, window.run_end), (1, 2))
        self.assertEqual([m.id for m in window.messages], [1, 2, 3, 4])


class ValidateVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = window_of(
            [msg(1, "setup", author=10), msg(2, "punchline", author=20), msg(3, "more", author=30), msg(4, "x", author=10)]
        )

    def validate(self, raw: object) -> object:
        return validate_verdict(raw, self.window, provider="test")

    def test_accepts_a_good_verdict(self) -> None:
        verdict = self.validate(good_raw())
        self.assertIsNotNone(verdict)
        self.assertEqual((verdict.clip, verdict.start, verdict.end, verdict.funny, verdict.standalone), (True, 0, 1, 8.5, True))
        self.assertEqual((verdict.sensitive, verdict.reason, verdict.provider), ((), "setup and punchline", "test"))

    def test_integer_funny_and_known_flags_are_fine(self) -> None:
        verdict = self.validate(good_raw(funny=9, clip=False, sensitive=["health"]))
        self.assertEqual((verdict.funny, verdict.sensitive), (9.0, ("health",)))

    def test_rejects_malformed_verdicts(self) -> None:
        bad = {
            "not a dict": "clip it",
            "list": [True],
            "missing clip": {k: v for k, v in good_raw().items() if k != "clip"},
            "missing reason": {k: v for k, v in good_raw().items() if k != "reason"},
            "clip as int": good_raw(clip=1),
            "standalone as string": good_raw(standalone="yes"),
            "start as float": good_raw(start=0.0),
            "start as bool": good_raw(start=True),
            "end before start": good_raw(start=2, end=1),
            "end past the window": good_raw(start=0, end=4),
            "negative start": good_raw(start=-1, end=1),
            "funny too high": good_raw(funny=11),
            "funny negative": good_raw(funny=-1),
            "funny nan": good_raw(funny=math.nan),
            "funny inf": good_raw(funny=math.inf),
            "funny as string": good_raw(funny="8"),
            "unknown flag": good_raw(sensitive=["spicy"]),
            "flags not a list": good_raw(sensitive="slur"),
            "flag not a string": good_raw(sensitive=[1]),
            "reason not a string": good_raw(reason=7),
        }
        for name, raw in bad.items():
            with self.subTest(name=name):
                self.assertIsNone(self.validate(raw))

    def test_clip_false_does_not_need_a_valid_range(self) -> None:
        verdict = self.validate(good_raw(clip=False, start=-5, end=99))
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.clip)

    def test_nine_messages_is_too_many(self) -> None:
        messages = [msg(i, f"line {i}", author=10) for i in range(1, 11)]
        window = window_of(messages)
        self.assertIsNone(validate_verdict(good_raw(start=0, end=8), window, provider="t"))  # 9 lines
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=7), window, provider="t"))  # 8 lines

    def test_more_than_600_characters_is_too_long(self) -> None:
        window = window_of([msg(1, "x" * 300, author=10), msg(2, "y" * 301, author=10)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=1), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=0), window, provider="t"))

    def test_four_authors_is_too_many(self) -> None:
        window = window_of([msg(1, "a", author=10), msg(2, "b", author=20), msg(3, "c", author=30), msg(4, "d", author=40)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=3), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=2), window, provider="t"))

    def test_a_not_text_line_inside_the_range_is_rejected(self) -> None:
        window = window_of([msg(1, "a", author=10), msg(2, "", author=20, eligible=False), msg(3, "c", author=10)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=2), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=2, end=2), window, provider="t"))

    def test_reason_is_truncated_to_160_chars(self) -> None:
        verdict = self.validate(good_raw(reason="r" * 500))
        self.assertEqual(len(verdict.reason), 160)

    def test_decide_scores_are_attached(self) -> None:
        verdict = validate_verdict(good_raw(), self.window, provider="p", decide={"funny": 0.9})
        self.assertEqual(dict(verdict.decide), {"funny": 0.9})

    def test_sensitive_flags_constant_is_complete(self) -> None:
        self.assertEqual(
            set(SENSITIVE_FLAGS),
            {"personal_info", "credentials", "self_harm", "sexual", "slur", "minor", "health", "tragedy", "other"},
        )


class HeuristicJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_heuristic_judge_never_clips_and_cannot_post(self) -> None:
        judge = HeuristicJudge()
        verdict = await judge.judge(window_of([msg(1, "anything at all")]))
        self.assertFalse(verdict.clip)
        self.assertFalse(judge.can_post)
        self.assertEqual((judge.name, verdict.provider, verdict.reason), ("heuristic", "heuristic", "no LLM judge"))


if __name__ == "__main__":
    unittest.main()
