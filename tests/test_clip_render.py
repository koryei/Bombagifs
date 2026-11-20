"""Clip card rendering: fonts, wrapping, grouping, layout, markdown, mentions, and emoji (offline)."""

from __future__ import annotations

import datetime
import io
import unittest

from PIL import Image

from clip_render import THEMES, ClipAuthor, ClipCard, ClipMessage, format_time, group_messages, load_fonts, render_clip, wrap_text

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


def solid_png(color: tuple[int, int, int], size: int = 64) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buffer, format="PNG")
    return buffer.getvalue()


def render(card: ClipCard, **kwargs: object) -> Image.Image:
    return Image.open(io.BytesIO(render_clip(card, **kwargs))).convert("RGB")  # type: ignore[arg-type]


def card_of(*messages: ClipMessage) -> ClipCard:
    return ClipCard(messages=messages)


def count_close(image: Image.Image, color: tuple[int, int, int], tolerance: int = 12, box: tuple[int, int, int, int] | None = None) -> int:
    region = image.crop(box) if box else image
    pixels = region.get_flattened_data() if hasattr(region, "get_flattened_data") else region.getdata()
    return sum(1 for p in pixels if all(abs(p[i] - color[i]) <= tolerance for i in range(3)))


class FormatTimeTests(unittest.TestCase):
    def test_format_time(self) -> None:
        utc = datetime.timezone.utc

        def at(hour: int, minute: int) -> float:
            return datetime.datetime(2026, 11, 20, hour, minute, tzinfo=utc).timestamp()

        self.assertEqual(format_time(at(19, 9), utc), "7:09 PM")
        self.assertEqual(format_time(at(0, 5), utc), "12:05 AM")
        self.assertEqual(format_time(at(12, 30), utc), "12:30 PM")
        self.assertEqual(format_time(at(1, 0), utc), "1:00 AM")
        self.assertEqual(format_time(at(23, 59), utc), "11:59 PM")

    def test_format_time_defaults_to_the_host_time_zone(self) -> None:
        stamp = datetime.datetime(2026, 11, 20, 15, 4).timestamp()  # naive = host-local
        self.assertEqual(format_time(stamp), "3:04 PM")


class RenderTests(unittest.TestCase):
    def test_render_returns_a_decodable_png_of_expected_width(self) -> None:
        data = render_clip(card_of(message("hello world")), scale=2)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(Image.open(io.BytesIO(data)).size[0], 1040)
        self.assertEqual(Image.open(io.BytesIO(render_clip(card_of(message("hello world")), scale=1))).size[0], 520)

    def test_background_follows_the_theme(self) -> None:
        card = card_of(message("hello world"))
        self.assertEqual(render(card, theme="onyx").getpixel((0, 0)), THEMES["onyx"].bg)
        self.assertEqual(render(card, theme="dark").getpixel((0, 0)), (49, 51, 56))

    def test_unknown_theme_falls_back_to_onyx(self) -> None:
        self.assertEqual(render(card_of(message("hello")), theme="nope").getpixel((0, 0)), THEMES["onyx"].bg)

    def test_body_text_uses_the_text_colour(self) -> None:
        image = render(card_of(message("the better one was me")), scale=2)
        body = (72 * 2, 38 * 2, 500 * 2, 62 * 2)
        self.assertGreater(count_close(image, THEMES["onyx"].text, box=body), 50)

    def test_name_uses_the_member_colour_or_the_default(self) -> None:
        header = (72 * 2, 14 * 2, 200 * 2, 36 * 2)
        red = render(card_of(message("hi there", who=author(color=(255, 0, 0)))), scale=2)
        self.assertGreater(count_close(red, (255, 0, 0), box=header), 30)
        plain = render(card_of(message("hi there", who=author(color=None))), scale=2)
        self.assertEqual(count_close(plain, (255, 0, 0), box=header), 0)
        self.assertGreater(count_close(plain, THEMES["onyx"].name, box=header), 30)

    def test_timestamp_is_drawn_in_the_time_colour(self) -> None:
        image = render(card_of(message("hello there")), scale=2)
        self.assertGreater(count_close(image, THEMES["onyx"].time, box=(72 * 2, 14 * 2, 400 * 2, 36 * 2)), 10)

    def test_avatar_is_circular(self) -> None:
        image = render(card_of(message("hello", who=author(avatar=solid_png((0, 200, 0))))), scale=2)
        self.assertEqual(image.getpixel((16 * 2 + 20 * 2, 16 * 2 + 20 * 2)), (0, 200, 0))  # centre of the circle
        self.assertEqual(image.getpixel((16 * 2 + 1, 16 * 2 + 1)), THEMES["onyx"].bg)  # corner of the avatar box

    def test_missing_avatar_draws_a_filled_initial_circle(self) -> None:
        image = render(card_of(message("hello", who=author(7, "Zed", avatar=None))), scale=2)
        near_top = image.getpixel((16 * 2 + 20 * 2, 16 * 2 + 4 * 2))
        self.assertNotEqual(near_top, THEMES["onyx"].bg)
        self.assertEqual(image.getpixel((16 * 2 + 1, 16 * 2 + 1)), THEMES["onyx"].bg)

    def test_undecodable_avatar_falls_back_to_the_initial_circle(self) -> None:
        image = render(card_of(message("hello", who=author(avatar=b"not an image"))), scale=2)
        self.assertNotEqual(image.getpixel((16 * 2 + 20 * 2, 16 * 2 + 4 * 2)), THEMES["onyx"].bg)

    def test_height_grows_with_lines_and_groups_and_is_capped(self) -> None:
        one = render(card_of(message("short")), scale=1).size[1]
        many = render(card_of(message("\n".join(["line"] * 6))), scale=1).size[1]
        self.assertGreater(many, one + 5 * 20)
        two_groups = render(card_of(message("a", who=author(1)), message("b", who=author(2, "Shan"), at=T0 + 1)), scale=1).size[1]
        self.assertGreater(two_groups, one + 30)
        capped = render(card_of(message("\n".join(["x"] * 500))), scale=1).size[1]
        self.assertEqual(capped, 900)

    def test_scale_2_is_twice_scale_1(self) -> None:
        card = card_of(message("short line one\nshort line two\nand a third"))  # no wrapping, so metrics agree
        h1, h2 = render(card, scale=1).size[1], render(card, scale=2).size[1]
        self.assertLessEqual(abs(h2 - 2 * h1), 2)

    def test_tag_chip_is_drawn_only_when_a_tag_is_present(self) -> None:
        header = (72 * 2, 14 * 2, 400 * 2, 36 * 2)
        tagged = render(card_of(message("hello", who=author(tag="DNA"))), scale=2)
        untagged = render(card_of(message("hello", who=author(tag=None))), scale=2)
        self.assertGreater(count_close(tagged, THEMES["onyx"].tag_bg, tolerance=0, box=header), 100)
        self.assertLess(count_close(untagged, THEMES["onyx"].tag_bg, tolerance=0, box=header), 20)  # only anti-aliasing noise

    def test_messages_in_one_group_share_a_single_header(self) -> None:
        a = author()
        grouped = render(card_of(message("one", who=a), message("two", who=a, at=T0 + 5)), scale=1).size[1]
        split = render(card_of(message("one", who=a), message("two", who=a, at=T0 + 900)), scale=1).size[1]
        self.assertLess(grouped, split)

    def test_empty_card_still_renders_a_valid_image(self) -> None:
        self.assertGreater(render(card_of(), scale=1).size[1], 0)


if __name__ == "__main__":
    unittest.main()
