"""Render a clip as a static Discord-style quote card (PNG). Pure and offline: avatars and emoji are passed in.

Discord-free. The adapter turns buffered messages into a ``ClipCard``, prefetches avatar and emoji images, and
calls ``render_clip``; nothing in here touches the network.
"""

from __future__ import annotations

import datetime
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
GROUP_GAP_S = 7 * 60.0  # Discord groups an author's messages sent within 7 minutes of each other


@dataclass(frozen=True)
class ClipAuthor:
    """Who wrote a message, as shown on the card."""

    id: int
    name: str
    color: tuple[int, int, int] | None
    tag: str | None
    avatar: bytes | None


@dataclass(frozen=True)
class ClipMessage:
    """One message on the card (``text`` is the original version; ``at`` is its epoch time)."""

    author: ClipAuthor
    text: str
    at: float


@dataclass(frozen=True)
class ClipCard:
    """Everything needed to draw a clip. ``mentions`` maps user ids to display names for ``<@id>`` mentions."""

    messages: tuple[ClipMessage, ...]
    mentions: Mapping[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Theme:
    """Card colours (RGB)."""

    bg: tuple[int, int, int]
    text: tuple[int, int, int]
    name: tuple[int, int, int]
    time: tuple[int, int, int]
    tag_bg: tuple[int, int, int]
    tag_text: tuple[int, int, int]
    mention_bg: tuple[int, int, int]
    mention_text: tuple[int, int, int]
    code_bg: tuple[int, int, int]


THEMES: dict[str, Theme] = {
    # Sampled from the operator's reference screenshot (a near-black Discord theme).
    "onyx": Theme(
        bg=(4, 4, 4),
        text=(219, 222, 225),
        name=(255, 255, 255),
        time=(148, 155, 164),
        tag_bg=(32, 34, 35),
        tag_text=(219, 222, 225),
        mention_bg=(29, 33, 75),
        mention_text=(201, 205, 251),
        code_bg=(20, 20, 22),
    ),
    "dark": Theme(
        bg=(49, 51, 56),
        text=(219, 222, 225),
        name=(255, 255, 255),
        time=(148, 155, 164),
        tag_bg=(30, 31, 34),
        tag_text=(219, 222, 225),
        mention_bg=(61, 66, 112),
        mention_text=(201, 205, 251),
        code_bg=(43, 45, 49),
    ),
}


@dataclass(frozen=True)
class Fonts:
    """The fonts for one render scale."""

    body: ImageFont.FreeTypeFont
    bold: ImageFont.FreeTypeFont
    italic: ImageFont.FreeTypeFont
    mono: ImageFont.FreeTypeFont
    name: ImageFont.FreeTypeFont
    time: ImageFont.FreeTypeFont
    tag: ImageFont.FreeTypeFont


@lru_cache(maxsize=4)
def load_fonts(scale: int) -> Fonts:
    """Load the bundled Noto fonts at the given scale (logical sizes: body 16, name 16, time 12, tag 12, mono 14)."""

    def font(file: str, size: int) -> ImageFont.FreeTypeFont:
        path = FONT_DIR / file
        if not path.is_file():
            raise FileNotFoundError(f"bundled font missing: {path}")
        return ImageFont.truetype(str(path), size * scale)

    return Fonts(
        body=font("NotoSans-Regular.ttf", 16),
        bold=font("NotoSans-Bold.ttf", 16),
        italic=font("NotoSans-Italic.ttf", 16),
        mono=font("NotoSansMono-Regular.ttf", 14),
        name=font("NotoSans-Bold.ttf", 16),
        time=font("NotoSans-Regular.ttf", 12),
        tag=font("NotoSans-Medium.ttf", 12),
    )


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> list[str]:
    """Wrap text to ``max_width`` pixels: words stay whole where possible, long words break, newlines are kept."""
    lines: list[str] = []
    for paragraph in text.split("\n"):
        current = ""
        for word in paragraph.split(" "):
            candidate = word if not current else f"{current} {word}"
            if font.getlength(candidate) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            # the word alone is too wide (or the first word of the line): break it by characters
            while word and font.getlength(word) > max_width:
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut]) > max_width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def group_messages(messages: Sequence[ClipMessage]) -> list[list[ClipMessage]]:
    """Group consecutive messages like Discord: same author, each within 7 minutes of the previous one."""
    groups: list[list[ClipMessage]] = []
    for message in messages:
        last = groups[-1][-1] if groups else None
        if last is not None and last.author.id == message.author.id and message.at - last.at <= GROUP_GAP_S:
            groups[-1].append(message)
        else:
            groups.append([message])
    return groups


# layout, in logical pixels (multiplied by the render scale) ---------------------------------------------------

WIDTH = 520
PAD = 16
AVATAR = 40
TEXT_X = 72  # where names and message text start
LINE_H = 22
HEADER_H = 22
GROUP_GAP = 17
MAX_HEIGHT = 900
BASELINE = 16  # text baseline offset inside a 22 px row
MAX_NAME_CHARS = 28
_DEFAULT_AVATARS = [(88, 101, 242), (116, 127, 141), (59, 165, 93), (250, 166, 26), (237, 66, 69), (235, 69, 158)]


def format_time(epoch: float, tz: datetime.tzinfo | None = None) -> str:
    """Discord-style clock time such as ``7:09 PM`` (host-local time unless ``tz`` is given)."""
    moment = datetime.datetime.fromtimestamp(epoch, tz)
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _circle_mask(size: int) -> Image.Image:
    """An anti-aliased filled circle mask (drawn at 4x, then reduced)."""
    big = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(big).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def _avatar_image(author: ClipAuthor, size: int, fonts: Fonts) -> Image.Image:
    """The author's avatar as a circle; a coloured circle with their initial if there is none or it can't be read."""
    tile: Image.Image | None = None
    if author.avatar:
        try:
            with Image.open(io.BytesIO(author.avatar)) as source:
                tile = ImageOps.fit(source.convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
        except (OSError, ValueError, Image.DecompressionBombError):
            tile = None
    if tile is None:
        tile = Image.new("RGBA", (size, size), _DEFAULT_AVATARS[author.id % len(_DEFAULT_AVATARS)] + (255,))
        initial = (author.name.strip()[:1] or "?").upper()
        ImageDraw.Draw(tile).text((size / 2, size / 2), initial, font=fonts.bold, fill=(255, 255, 255), anchor="mm")
    tile.putalpha(_circle_mask(size))
    return tile


def _short_name(name: str) -> str:
    return name if len(name) <= MAX_NAME_CHARS else name[: MAX_NAME_CHARS - 1] + "…"


def render_clip(
    card: ClipCard,
    *,
    scale: int = 2,
    theme: str = "onyx",
    emoji: Mapping[str, bytes] | None = None,
) -> bytes:
    """Render the card to PNG bytes. Unknown themes fall back to ``onyx``; the image is at most 900 logical px tall."""
    colours = THEMES.get(theme, THEMES["onyx"])
    fonts = load_fonts(scale)
    text_width = (WIDTH - TEXT_X - PAD) * scale
    groups = group_messages(card.messages)

    laid_out: list[tuple[list[ClipMessage], list[list[str]]]] = []
    total = PAD
    for index, group in enumerate(groups):
        wrapped = [wrap_text(message.text, fonts.body, text_width) for message in group]
        laid_out.append((group, wrapped))
        total += HEADER_H + sum(len(lines) for lines in wrapped) * LINE_H
        total += GROUP_GAP if index < len(groups) - 1 else 0
    total += PAD
    height = min(total, MAX_HEIGHT) * scale

    image = Image.new("RGB", (WIDTH * scale, height), colours.bg)
    draw = ImageDraw.Draw(image)
    y = PAD
    for group, wrapped in laid_out:
        first = group[0]
        avatar = _avatar_image(first.author, AVATAR * scale, fonts)
        image.paste(avatar, (PAD * scale, y * scale), avatar)

        header_baseline = (y + BASELINE) * scale
        x = TEXT_X * scale
        name_colour = first.author.color or colours.name
        name = _short_name(first.author.name)
        draw.text((x, header_baseline), name, font=fonts.name, fill=name_colour, anchor="ls")
        x += int(fonts.name.getlength(name)) + 6 * scale
        if first.author.tag:
            tag_width = int(fonts.tag.getlength(first.author.tag)) + 8 * scale
            chip = (x, (y + 3) * scale, x + tag_width, (y + 19) * scale)
            draw.rounded_rectangle(chip, radius=3 * scale, fill=colours.tag_bg)
            draw.text((x + 4 * scale, header_baseline), first.author.tag, font=fonts.tag, fill=colours.tag_text, anchor="ls")
            x += tag_width + 6 * scale
        draw.text((x, header_baseline), format_time(first.at), font=fonts.time, fill=colours.time, anchor="ls")

        line_y = y + HEADER_H
        for lines in wrapped:
            for line in lines:
                draw.text((TEXT_X * scale, (line_y + BASELINE) * scale), line, font=fonts.body, fill=colours.text, anchor="ls")
                line_y += LINE_H
        y = line_y + GROUP_GAP

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
