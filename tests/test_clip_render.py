"""Clip card rendering: fonts, wrapping, grouping, layout, markdown, mentions, and emoji (offline)."""

from __future__ import annotations

import unittest

from clip_render import ClipAuthor, ClipMessage, group_messages, load_fonts, wrap_text

T0 = 1_700_000_000.0


def author(id: int = 1, name: str = "Pranit", *, color=None, tag=None, avatar=None) -> ClipAuthor:
    return ClipAuthor(id=id, name=name, color=color, tag=tag, avatar=avatar)


def message(text: str, *, who: ClipAuthor | None = None, at: float = T0) -> ClipMessage:
    return ClipMessage(author=who or author(), text=text, at=at)


class FontTests(unittest.TestCase):
    def test_fonts_load_at_scale(self) -> None:
        for scale in (1, 2):
            fonts = load_fonts(scale)
            for name in ("body", "bold", "italic", "mono", "name", "time", "tag"):
                with self.subTest(scale=scale, font=name):
                    self.assertGreater(getattr(fonts, name).getlength("Hello"), 0)
        self.assertGreater(load_fonts(2).body.getlength("Hello"), load_fonts(1).body.getlength("Hello") * 1.9)


class WrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.font = load_fonts(1).body

    def test_wrap_text_breaks_at_width(self) -> None:
        lines = wrap_text("the better one called and they left", self.font, 120)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(self.font.getlength(line) <= 120 for line in lines))
        self.assertEqual(" ".join(lines), "the better one called and they left")

    def test_short_text_stays_on_one_line(self) -> None:
        self.assertEqual(wrap_text("hello", self.font, 400), ["hello"])

    def test_wrap_breaks_long_words(self) -> None:
        lines = wrap_text("x" * 200, self.font, 100)
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(self.font.getlength(line) <= 100 for line in lines))
        self.assertEqual("".join(lines), "x" * 200)

    def test_wrap_preserves_explicit_newlines(self) -> None:
        self.assertEqual(wrap_text("a\n\nb", self.font, 400), ["a", "", "b"])

    def test_wrap_empty_text(self) -> None:
        self.assertEqual(wrap_text("", self.font, 400), [""])


class GroupingTests(unittest.TestCase):
    def test_same_author_within_7_minutes_shares_a_header(self) -> None:
        a = author()
        groups = group_messages([message("one", who=a, at=T0), message("two", who=a, at=T0 + 60), message("three", who=a, at=T0 + 480)])
        self.assertEqual([[m.text for m in g] for g in groups], [["one", "two", "three"]])

    def test_a_gap_over_7_minutes_starts_a_new_header(self) -> None:
        a = author()
        groups = group_messages([message("one", who=a, at=T0), message("two", who=a, at=T0 + 421)])
        self.assertEqual([[m.text for m in g] for g in groups], [["one"], ["two"]])

    def test_a_different_author_starts_a_new_header(self) -> None:
        a, b = author(1), author(2, "Shan")
        groups = group_messages([message("one", who=a), message("two", who=b, at=T0 + 1), message("three", who=a, at=T0 + 2)])
        self.assertEqual([[m.text for m in g] for g in groups], [["one"], ["two"], ["three"]])

    def test_empty_input(self) -> None:
        self.assertEqual(group_messages([]), [])


if __name__ == "__main__":
    unittest.main()
