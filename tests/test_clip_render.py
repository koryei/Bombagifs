"""Clip card rendering: fonts, wrapping, grouping, layout, markdown, mentions, and emoji (offline)."""

from __future__ import annotations

import datetime
import io
import unittest
from pathlib import Path

from PIL import Image

from clip_render import (
    BASELINE,
    HEADER_H,
    PAD,
    THEMES,
    TEXT_X,
    ClipAuthor,
    ClipCard,
    ClipMessage,
    Span,
    collect_asset_keys,
    demo_card,
    format_time,
    host_zone,
    group_messages,
    load_fonts,
    render_clip,
    tokenize_markdown,
    wrap_text,
)

T0 = 1_700_000_000.0


def author(id: int = 1, name: str = "Pranit", *, color=None, tag=None, avatar=None) -> ClipAuthor:
    return ClipAuthor(id=id, name=name, color=color, tag=tag, avatar=avatar)


def message(text: str, *, who: ClipAuthor | None = None, at: float = T0) -> ClipMessage:
    return ClipMessage(author=who or author(), text=text, at=at)


class FontTests(unittest.TestCase):
    def test_fonts_load_at_scale(self) -> None:
        for scale in (1, 2):
            fonts = load_fonts(scale)
            for name in ("body", "bold", "italic", "bold_italic", "mono", "name", "time", "tag"):
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
    def test_host_zone_reads_the_machine_zone_fresh_each_time(self) -> None:
        import os
        import shutil
        import tempfile
        import zoneinfo

        source = next((Path(root) / "America/New_York" for root in zoneinfo.TZPATH if (Path(root) / "America/New_York").is_file()), None)
        if source is None:
            self.skipTest("no system zoneinfo database")
        epoch = 1_800_000_000  # 08:00 UTC
        with tempfile.TemporaryDirectory() as work:
            localtime = Path(work) / "localtime"
            shutil.copy(source, localtime)
            old = os.environ.pop("TZ", None)
            try:
                self.assertEqual(format_time(epoch, host_zone(str(localtime))), "3:00 AM")
            finally:
                if old is not None:
                    os.environ["TZ"] = old

    def test_host_zone_prefers_tz_and_survives_a_missing_file(self) -> None:
        import os

        old = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Tokyo"
        try:
            self.assertEqual(format_time(1_800_000_000, host_zone("/nonexistent/localtime")), "5:00 PM")
        finally:
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
        os.environ.pop("TZ", None)
        try:
            self.assertIsNone(host_zone("/nonexistent/localtime"))  # falls back to the C library's local time
        finally:
            if old is not None:
                os.environ["TZ"] = old

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


def flags(span: Span) -> str:
    return ("b" if span.bold else "") + ("i" if span.italic else "") + ("u" if span.underline else "") + ("s" if span.strike else "")


def shape(text: str, mentions: dict[int, str] | None = None) -> list[tuple[str, str, str]]:
    return [(s.kind, s.text, flags(s)) for s in tokenize_markdown(text, mentions)]


class MarkdownTokenizerTests(unittest.TestCase):
    def test_plain_text(self) -> None:
        self.assertEqual(shape("just words"), [("text", "just words", "")])
        self.assertEqual(shape(""), [])

    def test_basic_styles(self) -> None:
        self.assertEqual(shape("**bold**"), [("text", "bold", "b")])
        self.assertEqual(shape("*it*"), [("text", "it", "i")])
        self.assertEqual(shape("_it_"), [("text", "it", "i")])
        self.assertEqual(shape("__under__"), [("text", "under", "u")])
        self.assertEqual(shape("~~gone~~"), [("text", "gone", "s")])

    def test_styles_inside_text(self) -> None:
        self.assertEqual(shape("a **b** c"), [("text", "a ", ""), ("text", "b", "b"), ("text", " c", "")])

    def test_nested_styles(self) -> None:
        self.assertEqual(shape("***both***"), [("text", "both", "bi")])
        self.assertEqual(shape("**bold *italic* bold**"), [("text", "bold ", "b"), ("text", "italic", "bi"), ("text", " bold", "b")])
        self.assertEqual(shape("__under **bold**__"), [("text", "under ", "u"), ("text", "bold", "bu")])

    def test_inline_code_is_not_parsed_further(self) -> None:
        self.assertEqual(shape("run `**x**` now"), [("text", "run ", ""), ("code", "**x**", ""), ("text", " now", "")])
        self.assertEqual(shape("``a ` b``"), [("code", "a ` b", "")])

    def test_fenced_code_blocks(self) -> None:
        self.assertEqual(shape("```\nline1\nline2\n```"), [("codeblock", "line1\nline2", "")])
        self.assertEqual(shape("```py\nprint(1)\n```"), [("codeblock", "print(1)", "")])
        self.assertEqual(shape("before ```x = 1``` after"), [("text", "before ", ""), ("codeblock", "x = 1", ""), ("text", " after", "")])

    def test_unmatched_markers_stay_literal(self) -> None:
        for text in ("**oops", "a * b", "2 * 3 * 4", "~~nope", "`open", "snake_case_name", "lone _ underscore", "** **"):
            with self.subTest(text=text):
                self.assertEqual(shape(text), [("text", text, "")])

    def test_escaped_markers_are_literal(self) -> None:
        self.assertEqual(shape("\\*not italic\\*"), [("text", "*not italic*", "")])
        self.assertEqual(shape("a \\\\ b"), [("text", "a \\ b", "")])  # an escaped backslash
        self.assertEqual(shape("trailing \\"), [("text", "trailing \\", "")])

    def test_mentions_resolve_from_the_mapping_else_unknown(self) -> None:
        names = {1: "Koryei"}
        self.assertEqual(shape("hi <@1>", names), [("text", "hi ", ""), ("mention", "@Koryei", "")])
        self.assertEqual(shape("<@!1>", names), [("mention", "@Koryei", "")])
        self.assertEqual(shape("<@2>", names), [("mention", "@unknown", "")])
        self.assertEqual(shape("<#9>", names), [("mention", "#channel", "")])
        self.assertEqual(shape("<@&7>", names), [("mention", "@role", "")])

    def test_timestamps_become_plain_text(self) -> None:
        spans = tokenize_markdown("at <t:1700000000:R> ok")
        self.assertTrue(all(s.kind == "text" for s in spans))
        self.assertNotIn("<t:", "".join(s.text for s in spans))

    def test_custom_emoji_spans(self) -> None:
        spans = tokenize_markdown("a <:pog:123> b <a:wave:456>")
        custom = [(s.kind, s.text, s.ref) for s in spans if s.kind == "custom_emoji"]
        self.assertEqual(custom, [("custom_emoji", "pog", "123"), ("custom_emoji", "wave", "456")])

    def test_unclosed_angle_brackets_stay_literal(self) -> None:
        self.assertEqual(shape("a < b <@ c"), [("text", "a < b <@ c", "")])


class MarkdownRenderTests(unittest.TestCase):
    BODY_Y = (PAD + HEADER_H + BASELINE) * 2  # baseline of the first body line at scale 2

    def text_row_max(self, image: Image.Image, y0: int, y1: int, x1: int) -> int:
        best = 0
        for y in range(y0, y1):
            row = image.crop((TEXT_X * 2, y, x1, y + 1))
            best = max(best, count_close(row, THEMES["onyx"].text))
        return best

    def test_code_block_draws_a_panel_in_the_code_colour(self) -> None:
        plain = render(card_of(message("x = 1")), scale=2)
        block = render(card_of(message("```\nx = 1\ny = 2\n```")), scale=2)
        self.assertEqual(count_close(plain, THEMES["onyx"].code_bg, tolerance=0), 0)
        self.assertGreater(count_close(block, THEMES["onyx"].code_bg, tolerance=0), 2000)

    def test_inline_code_draws_a_background(self) -> None:
        image = render(card_of(message("run `ls -la` now")), scale=2)
        self.assertGreater(count_close(image, THEMES["onyx"].code_bg, tolerance=0), 200)

    def test_mention_draws_a_pill_with_mention_colours(self) -> None:
        image = render(ClipCard(messages=(message("hey <@1>"),), mentions={1: "Koryei"}), scale=2)
        self.assertGreater(count_close(image, THEMES["onyx"].mention_bg, tolerance=0), 200)
        self.assertGreater(count_close(image, THEMES["onyx"].mention_text), 40)

    def test_underline_draws_a_line_under_the_text(self) -> None:
        font = load_fonts(2).body
        width = int(font.getlength("word"))
        x1 = TEXT_X * 2 + width
        plain = render(card_of(message("word")), scale=2)
        under = render(card_of(message("__word__")), scale=2)
        y0, y1 = self.BODY_Y + 1, self.BODY_Y + 6
        self.assertGreaterEqual(self.text_row_max(under, y0, y1, x1), width * 0.9)
        self.assertLess(self.text_row_max(plain, y0, y1, x1), width * 0.3)

    def test_strikethrough_draws_a_line_through_the_text(self) -> None:
        font = load_fonts(2).body
        width = int(font.getlength("word"))
        x1 = TEXT_X * 2 + width
        plain = render(card_of(message("word")), scale=2)
        struck = render(card_of(message("~~word~~")), scale=2)
        y0, y1 = self.BODY_Y - 14, self.BODY_Y - 4
        self.assertGreaterEqual(self.text_row_max(struck, y0, y1, x1), width * 0.9)
        self.assertLess(self.text_row_max(plain, y0, y1, x1), width * 0.8)

    def test_bold_changes_the_rendering(self) -> None:
        self.assertNotEqual(render_clip(card_of(message("word")), scale=2), render_clip(card_of(message("**word**")), scale=2))

    def test_markdown_markers_are_not_drawn(self) -> None:
        asterisks = render(card_of(message("**word**")), scale=2)
        bare = render(card_of(message("word")), scale=2)
        # bold glyphs are wider than regular, but markers must not add width equivalent to four asterisks
        font = load_fonts(2)
        self.assertLess(font.bold.getlength("word") + 4, font.bold.getlength("**word**"))
        self.assertEqual(asterisks.size[1], bare.size[1])

    def test_styled_text_wraps_inside_the_card(self) -> None:
        text = " ".join(["**bold**", "_italic_", "plain", "`code`", "~~strike~~"] * 8)
        image = render(card_of(message(text)), scale=2)
        right_padding = image.crop(((520 - PAD + 2) * 2, 0, 520 * 2, image.size[1]))
        self.assertEqual(count_close(right_padding, THEMES["onyx"].bg, tolerance=0), right_padding.size[0] * right_padding.size[1])
        self.assertGreater(image.size[1], render(card_of(message("short")), scale=2).size[1] + 40)

    def test_very_long_unbroken_words_do_not_overflow(self) -> None:
        image = render(card_of(message("**" + "w" * 200 + "**")), scale=2)
        right_padding = image.crop(((520 - PAD + 2) * 2, 0, 520 * 2, image.size[1]))
        self.assertEqual(count_close(right_padding, THEMES["onyx"].bg, tolerance=0), right_padding.size[0] * right_padding.size[1])

    def test_explicit_newlines_inside_styles_make_new_lines(self) -> None:
        one = render(card_of(message("**a b**")), scale=1).size[1]
        two = render(card_of(message("**a\nb**")), scale=1).size[1]
        self.assertEqual(two - one, 22)

    def test_empty_message_still_takes_a_line(self) -> None:
        self.assertEqual(render(card_of(message("")), scale=1).size[1], render(card_of(message("x")), scale=1).size[1])


class EmojiTokenTests(unittest.TestCase):
    def emoji_spans(self, text: str) -> list[tuple[str, str | None]]:
        return [(s.text, s.ref) for s in tokenize_markdown(text) if s.kind == "emoji"]

    def test_unicode_emoji_are_tokenised_including_zwj_skin_tone_and_flags(self) -> None:
        text = "a 👍🏽 b 👨‍👩‍👧 c 🇨🇦 d ❤️ e 😂 f 1️⃣"
        self.assertEqual(
            self.emoji_spans(text),
            [
                ("👍🏽", "1f44d-1f3fd"),
                ("👨‍👩‍👧", "1f468-200d-1f469-200d-1f467"),
                ("🇨🇦", "1f1e8-1f1e6"),
                ("❤️", "2764"),
                ("😂", "1f602"),
                ("1️⃣", "31-20e3"),
            ],
        )

    def test_text_between_emoji_is_kept_with_its_style(self) -> None:
        spans = [(s.kind, s.text, s.bold) for s in tokenize_markdown("**hi 😂 there**")]
        self.assertEqual(spans, [("text", "hi ", True), ("emoji", "😂", False), ("text", " there", True)])

    def test_plain_text_and_digits_are_not_emoji(self) -> None:
        self.assertEqual(self.emoji_spans("hello 123 #tag * ok"), [])

    def test_emoji_inside_code_is_left_alone(self) -> None:
        self.assertEqual(self.emoji_spans("`😂` and ```😂```"), [])

    def test_collect_asset_keys(self) -> None:
        card = card_of(message("hi 😂 <:pog:123> <a:wave:456> 😂"), message("🇨🇦", who=author(2), at=T0 + 1))
        self.assertEqual(collect_asset_keys(card), ({"1f602", "1f1e8-1f1e6"}, {"123", "456"}))

    def test_collect_asset_keys_ignores_code_and_plain_text(self) -> None:
        self.assertEqual(collect_asset_keys(card_of(message("plain `😂` text"))), (set(), set()))


class EmojiRenderTests(unittest.TestCase):
    RED, GREEN = (255, 0, 0), (0, 255, 0)

    def test_render_uses_supplied_emoji_images_inline(self) -> None:
        card = card_of(message("hi 😂 there"))
        image = render(card, scale=2, emoji={"1f602": solid_png(self.RED, 72)})
        self.assertGreater(count_close(image, self.RED, tolerance=5), 1000)  # about 20x20 logical px at 2x
        self.assertEqual(count_close(render(card, scale=2), self.RED, tolerance=5), 0)

    def test_custom_emoji_images_are_used(self) -> None:
        card = card_of(message("yo <:pog:123>"))
        image = render(card, scale=2, emoji={"custom:123": solid_png(self.GREEN, 64)})
        self.assertGreater(count_close(image, self.GREEN, tolerance=5), 1000)

    def test_missing_custom_emoji_falls_back_to_its_name(self) -> None:
        image = render(card_of(message("yo <:pog:123>")), scale=2)
        self.assertGreater(count_close(image, THEMES["onyx"].text, box=(100 * 2, 38 * 2, 300 * 2, 62 * 2)), 30)

    def test_missing_emoji_asset_falls_back_to_glyph_without_error(self) -> None:
        image = render(card_of(message("hi 😂 there 🇨🇦")), scale=2, emoji={})
        self.assertGreater(image.size[1], 0)

    def test_undecodable_emoji_asset_is_ignored(self) -> None:
        image = render(card_of(message("hi 😂")), scale=2, emoji={"1f602": b"not a png"})
        self.assertGreater(image.size[1], 0)

    def test_emoji_only_message_is_rendered_larger(self) -> None:
        assets = {"1f602": solid_png(self.RED, 72)}
        plain = render(card_of(message("hi 😂")), scale=1, emoji=assets)
        jumbo = render(card_of(message("😂😂")), scale=1, emoji=assets)
        self.assertEqual(jumbo.size[1] - plain.size[1], 30)  # a 52 px row instead of a 22 px row
        self.assertGreater(count_close(jumbo, self.RED, tolerance=5), 2 * 40 * 40)

    def test_more_than_five_emoji_are_not_jumbo(self) -> None:
        assets = {"1f602": solid_png(self.RED, 72)}
        five = render(card_of(message("😂" * 5)), scale=1, emoji=assets).size[1]
        six = render(card_of(message("😂" * 6)), scale=1, emoji=assets).size[1]
        normal = render(card_of(message("hi 😂")), scale=1, emoji=assets).size[1]
        self.assertGreater(five, normal)
        self.assertEqual(six, normal)

    def test_emoji_with_text_wraps_and_stays_inside_the_card(self) -> None:
        assets = {"1f602": solid_png(self.RED, 72)}
        image = render(card_of(message("word 😂 " * 30)), scale=2, emoji=assets)
        right_padding = image.crop(((520 - 16 + 2) * 2, 0, 520 * 2, image.size[1]))
        self.assertEqual(count_close(right_padding, THEMES["onyx"].bg, tolerance=0), right_padding.size[0] * right_padding.size[1])


class DemoTests(unittest.TestCase):
    def test_demo_card_is_made_up_data_that_renders(self) -> None:
        card = demo_card()
        self.assertGreaterEqual(len(card.messages), 3)
        names = {m.author.name for m in card.messages}
        self.assertTrue(names.isdisjoint({"Pranit", "Koryei", "Shan"}))  # no real people in the repo
        image = render(card, scale=2)
        self.assertGreater(image.size[1], 200)


if __name__ == "__main__":
    unittest.main()
