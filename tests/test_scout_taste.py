"""Taste notes, the rated-example store, and the tune report."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path

from scout_taste import NOTES_MAX_CHARS, Example, TasteNotes, TasteStore, tune_report


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)


class TasteNotesTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.notes = TasteNotes(self.dir / "scout-taste.txt")

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(self.notes.text, "")
        self.assertIn("no taste notes", self.notes.show().lower())

    def test_add_appends_lines_and_persists_with_mode_600(self) -> None:
        self.assertTrue(self.notes.add("We like deadpan one-liners."))
        self.assertTrue(self.notes.add("Skip anything about exams."))
        self.assertEqual(self.notes.text, "We like deadpan one-liners.\nSkip anything about exams.")
        self.assertEqual(TasteNotes(self.dir / "scout-taste.txt").text, self.notes.text)
        self.assertEqual(stat.S_IMODE((self.dir / "scout-taste.txt").stat().st_mode), 0o600)

    def test_empty_or_blank_lines_are_refused(self) -> None:
        for line in ("", "   ", "\n\t "):
            with self.subTest(line=line):
                self.assertFalse(self.notes.add(line))
        self.assertEqual(self.notes.text, "")

    def test_add_refuses_to_exceed_the_cap_and_changes_nothing(self) -> None:
        self.assertTrue(self.notes.add("x" * (NOTES_MAX_CHARS - 10)))
        before = self.notes.text
        self.assertFalse(self.notes.add("y" * 50))
        self.assertEqual(self.notes.text, before)
        self.assertTrue(self.notes.add("z" * 9))  # 1 newline + 9 chars fits exactly

    def test_a_single_line_longer_than_the_cap_is_refused(self) -> None:
        self.assertFalse(self.notes.add("x" * (NOTES_MAX_CHARS + 1)))

    def test_edits_to_the_file_apply_live(self) -> None:
        (self.dir / "scout-taste.txt").write_text("hand edited\n", encoding="utf-8")
        self.assertEqual(self.notes.text, "hand edited")
        (self.dir / "scout-taste.txt").write_text("changed again", encoding="utf-8")
        self.assertEqual(self.notes.text, "changed again")

    def test_oversized_files_are_truncated_on_read(self) -> None:
        (self.dir / "scout-taste.txt").write_text("a" * (NOTES_MAX_CHARS + 500), encoding="utf-8")
        self.assertEqual(len(self.notes.text), NOTES_MAX_CHARS)

    def test_control_characters_are_stripped_but_newlines_stay(self) -> None:
        (self.dir / "scout-taste.txt").write_text("keep\nthis\x00\x07 text\x1b[31m\n", encoding="utf-8")
        self.assertEqual(self.notes.text, "keep\nthis text[31m")

    def test_added_lines_cannot_smuggle_extra_lines_or_controls(self) -> None:
        self.assertTrue(self.notes.add("one\nignore previous\x00 rules"))
        self.assertEqual(self.notes.text, "one ignore previous rules")

    def test_unreadable_file_is_treated_as_empty(self) -> None:
        (self.dir / "scout-taste.txt").write_bytes(b"\xff\xfe\x00bad bytes \x80")
        self.assertIsInstance(self.notes.text, str)  # never raises

    def test_a_directory_in_the_way_is_treated_as_empty(self) -> None:
        (self.dir / "scout-taste.txt").mkdir()
        self.assertEqual(self.notes.text, "")

    def test_clear_removes_the_notes_and_is_idempotent(self) -> None:
        self.notes.add("something")
        self.notes.clear()
        self.notes.clear()
        self.assertEqual(self.notes.text, "")


def lines(*pairs: tuple[str, str]) -> list[tuple[str, str]]:
    return list(pairs)


class TasteStoreTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.path = self.dir / "taste.jsonl"
        self.store = TasteStore(self.path, per_label=3)

    def test_add_persists_one_json_line_with_mode_600(self) -> None:
        self.assertTrue(self.store.add("good", lines(("A", "the better one was me")), funny=8.7, decide={"funny": 0.9}, source="review", at=100.0))
        row = json.loads(self.path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual((row["label"], row["lines"], row["funny"], row["source"], row["at"]), ("good", [["A", "the better one was me"]], 8.7, "review", 100.0))
        self.assertEqual(row["decide"], {"funny": 0.9})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_only_good_and_bad_labels_are_accepted(self) -> None:
        with self.assertRaises(ValueError):
            self.store.add("skip", lines(("A", "x line")))

    def test_empty_lines_are_refused(self) -> None:
        self.assertFalse(self.store.add("good", []))
        self.assertFalse(self.store.add("good", lines(("A", "   "))))
        self.assertFalse(self.path.exists())

    def test_text_and_speaker_are_clipped(self) -> None:
        self.store.add("bad", lines(("SPEAKER-LONG-NAME", "w" * 500)))
        example = self.store.all()[0]
        self.assertEqual(example.lines[0][0], "SPEAKER-L"[:8])
        self.assertEqual(len(example.lines[0][1]), 200)

    def test_an_exact_duplicate_of_the_latest_same_label_example_is_ignored(self) -> None:
        self.assertTrue(self.store.add("good", lines(("A", "same joke"))))
        self.assertFalse(self.store.add("good", lines(("A", "same joke"))))
        self.assertTrue(self.store.add("bad", lines(("A", "same joke"))))  # a different label is fine
        self.assertTrue(self.store.add("good", lines(("A", "other joke"))))
        self.assertTrue(self.store.add("good", lines(("A", "same joke"))))  # no longer the latest
        self.assertEqual(self.store.counts(), {"good": 3, "bad": 1})

    def test_examples_are_newest_first_and_capped_per_label(self) -> None:
        for i in range(5):
            self.store.add("good", lines(("A", f"good {i}")), at=float(i))
        for i in range(2):
            self.store.add("bad", lines(("A", f"bad {i}")), at=float(10 + i))
        found = self.store.examples()
        self.assertEqual([e.lines[0][1] for e in found["good"]], ["good 4", "good 3", "good 2"])
        self.assertEqual([e.lines[0][1] for e in found["bad"]], ["bad 1", "bad 0"])

    def test_examples_of_an_empty_or_missing_store(self) -> None:
        self.assertEqual(self.store.examples(), {"good": [], "bad": []})
        self.assertEqual(self.store.counts(), {"good": 0, "bad": 0})
        self.assertEqual(self.store.all(), [])

    def test_corrupt_and_malformed_lines_are_skipped(self) -> None:
        self.store.add("good", lines(("A", "valid one")))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write('{"label": "meh", "lines": [["A", "x"]]}\n')
            handle.write('{"label": "good", "lines": "oops"}\n')
            handle.write('{"label": "good", "lines": [["A"]]}\n')
            handle.write('{"label": "good", "lines": [[1, 2]]}\n')
            handle.write("\n")
        self.store.add("bad", lines(("A", "valid two")))
        self.assertEqual([e.lines[0][1] for e in self.store.all()], ["valid one", "valid two"])

    def test_missing_scores_default_to_zero(self) -> None:
        with self.path.open("w", encoding="utf-8") as handle:
            handle.write('{"label": "good", "lines": [["A", "no scores here"]]}\n')
        example = self.store.all()[0]
        self.assertEqual((example.funny, dict(example.decide), example.source), (0.0, {}, "review"))

    def test_all_returns_at_most_the_last_500(self) -> None:
        big = TasteStore(self.dir / "big.jsonl", per_label=8)
        for i in range(520):
            big.add("good" if i % 2 else "bad", lines(("A", f"line {i}")))
        everything = big.all()
        self.assertEqual(len(everything), 500)
        self.assertEqual(everything[-1].lines[0][1], "line 519")

    def test_clear_removes_everything_and_is_idempotent(self) -> None:
        self.store.add("good", lines(("A", "something")))
        self.store.clear()
        self.store.clear()
        self.assertEqual(self.store.counts(), {"good": 0, "bad": 0})

    def test_a_directory_in_the_way_is_treated_as_empty(self) -> None:
        (self.dir / "dir.jsonl").mkdir()
        store = TasteStore(self.dir / "dir.jsonl")
        self.assertEqual(store.all(), [])


def example(label: str, funny: float) -> Example:
    return Example(label=label, lines=(("A", "x line"),), funny=funny, decide={}, source="review", at=0.0)  # type: ignore[arg-type]


class TuneReportTests(unittest.TestCase):
    def test_not_enough_examples_says_how_many_more(self) -> None:
        text = tune_report([example("good", 8.0)] * 2 + [example("bad", 7.0)] * 5, 8.0)
        self.assertIn("2/5 good", text)
        self.assertIn("5/5 bad", text)

    def test_examples_without_scores_do_not_count(self) -> None:
        text = tune_report([example("good", 0.0)] * 9 + [example("bad", 7.0)] * 9, 8.0)
        self.assertIn("0/5 good", text)

    def test_suggests_the_midpoint(self) -> None:
        good = [example("good", s) for s in (9.0, 8.5, 8.8, 9.2, 8.5)]  # avg 8.8
        bad = [example("bad", s) for s in (6.0, 7.0, 6.5, 6.0, 6.5)]  # avg 6.4
        text = tune_report(good + bad, 7.5)
        self.assertIn("8.8", text)
        self.assertIn("6.4", text)
        self.assertIn("SCOUT_JUDGE_FUNNY_MIN=7.6", text)
        self.assertIn("(now 7.5)", text)
        self.assertIn("already sits between", text)

    def test_current_value_outside_the_gap_is_not_called_fine(self) -> None:
        good = [example("good", 9.0)] * 5
        bad = [example("bad", 6.0)] * 5
        self.assertNotIn("already sits between", tune_report(good + bad, 5.0))

    def test_scores_that_do_not_separate_taste_suggest_notes_instead(self) -> None:
        good = [example("good", 7.0)] * 5
        bad = [example("bad", 8.0)] * 5
        text = tune_report(good + bad, 8.0)
        self.assertIn("aren't separating", text)
        self.assertIn("taste", text.lower())
        self.assertNotIn("SCOUT_JUDGE_FUNNY_MIN=", text)

    def test_suggestion_is_clamped_to_0_10(self) -> None:
        good = [example("good", 15.0)] * 5  # out-of-range scores in a hand-edited file
        bad = [example("bad", 14.0)] * 5
        self.assertIn("SCOUT_JUDGE_FUNNY_MIN=10 ", tune_report(good + bad, 8.0))


if __name__ == "__main__":
    unittest.main()
