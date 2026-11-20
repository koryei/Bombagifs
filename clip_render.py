"""Render a clip as a static Discord-style quote card (PNG). Pure and offline: avatars and emoji are passed in.

Discord-free. The adapter turns buffered messages into a ``ClipCard``, prefetches avatar and emoji images, and
calls ``render_clip``; nothing in here touches the network.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import ImageFont

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
