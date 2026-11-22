"""Render a clip as a static Discord-style quote card (PNG). Pure and offline: avatars and emoji are passed in.

Discord-free. The adapter turns buffered messages into a ``ClipCard``, prefetches avatar and emoji images, and
calls ``render_clip``; nothing in here touches the network.
"""

from __future__ import annotations

import datetime
import io
import re
import string
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:  # the `emoji` package segments sequences exactly; without it a regex covers the common cases
    import emoji as _emoji_lib
except ImportError:  # pragma: no cover - exercised when the package is absent
    _emoji_lib = None

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
    bold_italic: ImageFont.FreeTypeFont
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
        bold_italic=font("NotoSans-BoldItalic.ttf", 16),
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


def host_zone(localtime_path: str = "/etc/localtime") -> datetime.tzinfo | None:
    """The zone to print card times in: ``TZ`` if set, else the machine's zone read fresh from ``/etc/localtime``.

    The C library caches the zone when a process first asks for local time, so a bot that keeps running after the VM's
    timezone is changed would go on printing the old one. Reading the file again keeps cards on the machine's real clock.
    ``None`` means "let the C library decide" (no zoneinfo data, or an unreadable file).
    """
    import os
    import zoneinfo

    name = (os.environ.get("TZ") or "").strip()
    try:
        if name:
            return zoneinfo.ZoneInfo(name)
        with open(localtime_path, "rb") as handle:
            return zoneinfo.ZoneInfo.from_file(handle)
    except (OSError, ValueError, zoneinfo.ZoneInfoNotFoundError):
        return None


def format_time(epoch: float, tz: datetime.tzinfo | None = None) -> str:
    """Discord-style clock time such as ``7:09 PM`` in ``tz`` (the machine's own zone when ``tz`` is None)."""
    moment = datetime.datetime.fromtimestamp(epoch, tz or host_zone())
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


# markdown -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A run of message content: ``kind`` is text, code, codeblock, mention, emoji, or custom_emoji."""

    kind: str
    text: str
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    ref: str | None = None  # emoji key or custom emoji id


_Style = tuple[bool, bool, bool, bool]  # bold, italic, underline, strike
_TOKEN_RE = re.compile(r"<(?:@&(?P<role>\d+)|@!?(?P<user>\d+)|#(?P<chan>\d+)|t:(?P<ts>-?\d+)(?::[A-Za-z])?)>")
_CUSTOM_EMOJI_RE = re.compile(r"<(?P<anim>a?):(?P<name>\w{2,32}):(?P<id>\d+)>")
_LANG_LINE_RE = re.compile(r"^[A-Za-z0-9_+#-]+\n")


def _with_marker(style: _Style, marker: str) -> _Style:
    bold, italic, underline, strike = style
    if marker == "***":
        return (True, True, underline, strike)
    if marker == "**":
        return (True, italic, underline, strike)
    if marker in ("*", "_"):
        return (bold, True, underline, strike)
    if marker == "__":
        return (bold, italic, True, strike)
    return (bold, italic, underline, True)  # ~~


def _find_closer(text: str, start: int, marker: str) -> int | None:
    """Index of the marker that closes one opened just before ``start``, or ``None``."""
    char = marker[0]
    j = start
    while True:
        k = text.find(marker, j)
        if k == -1:
            return None
        j = k + 1
        if k == start or text[k - 1] == "\\" or text[k - 1].isspace():
            continue
        if len(marker) == 1 and (text[k - 1] == char or text[k + 1 : k + 2] == char):
            continue  # part of a longer run of the same character
        if char == "_" and text[k + len(marker) : k + len(marker) + 1].isalnum():
            continue  # no emphasis inside words (snake_case)
        return k


def _try_emphasis(text: str, i: int, style: _Style, mentions: Mapping[int, str]) -> tuple[list[Span], int] | None:
    candidates = {"*": ("***", "**", "*"), "_": ("__", "_"), "~": ("~~",)}.get(text[i], ())
    for marker in candidates:
        if not text.startswith(marker, i):
            continue
        start = i + len(marker)
        if start >= len(text) or text[start].isspace():
            continue
        if text[i] == "_" and i > 0 and text[i - 1].isalnum():
            continue
        closer = _find_closer(text, start, marker)
        if closer is None:
            continue
        return _parse(text[start:closer], _with_marker(style, marker), mentions), closer + len(marker)
    return None


def _merge(spans: list[Span]) -> list[Span]:
    merged: list[Span] = []
    for span in spans:
        if merged and span.kind == "text" and merged[-1].kind == "text" and _style_of(merged[-1]) == _style_of(span):
            merged[-1] = Span("text", merged[-1].text + span.text, *_style_of(span))
        else:
            merged.append(span)
    return merged


def _style_of(span: Span) -> _Style:
    return (span.bold, span.italic, span.underline, span.strike)


def _parse(text: str, style: _Style, mentions: Mapping[int, str]) -> list[Span]:
    out: list[Span] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            out.append(Span("text", "".join(buffer), *style))
            buffer.clear()

    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "\\" and i + 1 < n and text[i + 1] in string.punctuation:
            buffer.append(text[i + 1])
            i += 2
            continue
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end != -1:
                flush()
                body = text[i + 3 : end]
                language = _LANG_LINE_RE.match(body)
                if language:
                    body = body[language.end() :]
                out.append(Span("codeblock", body.strip("\n")))
                i = end + 3
                continue
        if char == "`":
            run = len(text[i:]) - len(text[i:].lstrip("`"))
            end = text.find("`" * run, i + run)
            if end > i + run:
                flush()
                out.append(Span("code", text[i + run : end].strip(" ")))
                i = end + run
                continue
        if char == "<":
            custom = _CUSTOM_EMOJI_RE.match(text, i)
            if custom:
                flush()
                out.append(Span("custom_emoji", custom.group("name"), ref=custom.group("id")))
                i = custom.end()
                continue
            token = _TOKEN_RE.match(text, i)
            if token:
                flush()
                if token.group("user"):
                    out.append(Span("mention", "@" + mentions.get(int(token.group("user")), "unknown")))
                elif token.group("chan"):
                    out.append(Span("mention", "#channel"))
                elif token.group("role"):
                    out.append(Span("mention", "@role"))
                else:
                    try:
                        shown = format_time(int(token.group("ts")))
                    except (OverflowError, OSError, ValueError):
                        shown = "some time"
                    out.append(Span("text", shown, *style))
                i = token.end()
                continue
        if char in "*_~":
            emphasised = _try_emphasis(text, i, style, mentions)
            if emphasised is not None:
                flush()
                spans, i = emphasised
                out.extend(spans)
                continue
        buffer.append(char)
        i += 1
    flush()
    return _merge(out)


_EMOJI_CHAR = (
    "[\u00a9\u00ae\u203c\u2049\u2122\u2139\u2194-\u21aa\u231a-\u23ff\u24c2\u25aa-\u25fe\u2600-\u27bf"
    "\u2934\u2935\u2b05-\u2b55\u3030\u303d\u3297\u3299\U0001f000-\U0001faff]"
)
_EMOJI_MOD = "[\U0001f3fb-\U0001f3ff]"
_FALLBACK_EMOJI_RE = re.compile(
    "[\U0001f1e6-\U0001f1ff]{2}"  # flags
    "|[0-9#*]\ufe0f?\u20e3"  # keycaps
    f"|{_EMOJI_CHAR}[\ufe0e\ufe0f]?{_EMOJI_MOD}?(?:\u200d{_EMOJI_CHAR}\ufe0f?{_EMOJI_MOD}?)*"
)


def emoji_key(sequence: str) -> str:
    """The Twemoji file stem for an emoji sequence: hex codepoints joined by ``-`` (``fe0f`` dropped unless in a ZWJ)."""
    if "\u200d" not in sequence:
        sequence = sequence.replace("\ufe0f", "")
    return "-".join(f"{ord(char):x}" for char in sequence)


def _emoji_ranges(text: str) -> list[tuple[int, int]]:
    if _emoji_lib is not None:
        return [(found["match_start"], found["match_end"]) for found in _emoji_lib.emoji_list(text)]
    return [(match.start(), match.end()) for match in _FALLBACK_EMOJI_RE.finditer(text)]


def _split_emoji(spans: list[Span]) -> list[Span]:
    """Split unicode emoji out of plain text spans (code and other kinds are left alone)."""
    out: list[Span] = []
    for span in spans:
        if span.kind != "text":
            out.append(span)
            continue
        position = 0
        for start, end in _emoji_ranges(span.text):
            if start > position:
                out.append(Span("text", span.text[position:start], *_style_of(span)))
            sequence = span.text[start:end]
            out.append(Span("emoji", sequence, ref=emoji_key(sequence)))
            position = end
        if position < len(span.text):
            out.append(Span("text", span.text[position:], *_style_of(span)))
    return out


def tokenize_markdown(text: str, mentions: Mapping[int, str] | None = None) -> list[Span]:
    """Parse the Discord markdown subset (bold, italic, underline, strike, code, mentions, emoji)."""
    return _split_emoji(_parse(text, (False, False, False, False), mentions or {}))


def collect_asset_keys(card: ClipCard) -> tuple[set[str], set[str]]:
    """The images a card needs: ``(unicode emoji keys, custom emoji ids)``, so the caller can prefetch them."""
    unicode_keys: set[str] = set()
    custom_ids: set[str] = set()
    for message in card.messages:
        for span in tokenize_markdown(message.text, card.mentions):
            if span.kind == "emoji" and span.ref:
                unicode_keys.add(span.ref)
            elif span.kind == "custom_emoji" and span.ref:
                custom_ids.add(span.ref)
    return unicode_keys, custom_ids


# layout and drawing -------------------------------------------------------------------------------------------

PILL_PAD = 3  # logical px of padding inside code/mention backgrounds
BLOCK_LINE_H = 18
BLOCK_PAD = 8
BLOCK_MARGIN = 4


@dataclass
class _Item:
    kind: str  # text, code, mention, emoji, custom, newline
    text: str
    font: ImageFont.FreeTypeFont
    fill: tuple[int, int, int]
    width: int
    underline: bool = False
    strike: bool = False
    bg: tuple[int, int, int] | None = None
    ref: str | None = None
    space: bool = False
    height: int = LINE_H  # logical px of the row this item needs
    size: int = 0  # logical px of an emoji image


@dataclass
class _Row:
    height: int  # logical px
    items: list[tuple[int, _Item]]  # (x offset in scaled px, item)
    block: list[str] | None = None  # code block lines


def _font_for(span: Span, fonts: Fonts) -> ImageFont.FreeTypeFont:
    if span.bold and span.italic:
        return fonts.bold_italic
    if span.bold:
        return fonts.bold
    if span.italic:
        return fonts.italic
    return fonts.body


EMOJI_SIZE = 20
JUMBO_SIZE = 48
JUMBO_ROW = 52
JUMBO_MAX = 5


def _is_jumbo(spans: Sequence[Span]) -> bool:
    """Discord shows a message of only 1-5 emoji at a larger size."""
    emoji_count = sum(1 for s in spans if s.kind in ("emoji", "custom_emoji"))
    others = [s for s in spans if s.kind not in ("emoji", "custom_emoji") and not (s.kind == "text" and not s.text.strip())]
    return 0 < emoji_count <= JUMBO_MAX and not others


def _items_for(span: Span, fonts: Fonts, colours: Theme, scale: int, jumbo: bool = False) -> list[_Item]:
    pad = PILL_PAD * scale
    if span.kind == "text":
        font = _font_for(span, fonts)
        items: list[_Item] = []
        for token in re.findall(r"\n|[ \t]+|[^\s]+", span.text):
            if token == "\n":
                items.append(_Item("newline", "", font, colours.text, 0))
                continue
            items.append(
                _Item(
                    "text", token, font, colours.text, int(font.getlength(token)),
                    underline=span.underline, strike=span.strike, space=token.isspace(), ref=None,
                )
            )
        return items
    if span.kind == "code":
        font = fonts.mono
        return [_Item("code", span.text, font, colours.text, int(font.getlength(span.text)) + 2 * pad, bg=colours.code_bg)]
    if span.kind == "mention":
        font = fonts.body
        return [_Item("mention", span.text, font, colours.mention_text, int(font.getlength(span.text)) + 2 * pad, bg=colours.mention_bg)]
    size = JUMBO_SIZE if jumbo else EMOJI_SIZE
    height = JUMBO_ROW if jumbo else LINE_H
    shown = f":{span.text}:" if span.kind == "custom_emoji" else span.text  # what we draw if the image is missing
    kind = "custom" if span.kind == "custom_emoji" else "emoji"
    return [_Item(kind, shown, fonts.body, colours.text, (size + (4 if jumbo else 2)) * scale, ref=span.ref, height=height, size=size)]


def _split_item(item: _Item, max_width: int) -> list[_Item]:
    """Break an over-wide item into pieces that each fit on a line (by characters)."""
    pad = PILL_PAD * 2 if item.bg is not None else 0
    pieces: list[_Item] = []
    rest = item.text
    while rest:
        cut = len(rest)
        while cut > 1 and int(item.font.getlength(rest[:cut])) + pad > max_width:
            cut -= 1
        chunk, rest = rest[:cut], rest[cut:]
        pieces.append(
            _Item(item.kind, chunk, item.font, item.fill, int(item.font.getlength(chunk)) + pad,
                  underline=item.underline, strike=item.strike, bg=item.bg, ref=item.ref)
        )
    return pieces


def _layout(spans: Sequence[Span], fonts: Fonts, colours: Theme, scale: int, max_width: int, jumbo: bool = False) -> list[_Row]:
    """Lay spans out into rows: words wrap at ``max_width`` (scaled px), code blocks get their own rows."""
    rows: list[_Row] = []
    line: list[tuple[int, _Item]] = []
    x = 0

    def end_line() -> None:
        nonlocal line, x
        rows.append(_Row(max([LINE_H] + [item.height for _, item in line]), line))
        line, x = [], 0

    for span in spans:
        if span.kind == "codeblock":
            if line:
                end_line()
            inner = max_width - 2 * BLOCK_PAD * scale
            lines = [piece for source in span.text.split("\n") for piece in wrap_text(source, fonts.mono, inner)]
            rows.append(_Row(len(lines) * BLOCK_LINE_H + 2 * BLOCK_PAD + 2 * BLOCK_MARGIN, [], block=lines))
            continue
        for item in _items_for(span, fonts, colours, scale, jumbo):
            if item.kind == "newline":
                end_line()
                continue
            if item.space:
                if not line:
                    continue
                if x + item.width > max_width:
                    end_line()
                    continue
                line.append((x, item))
                x += item.width
                continue
            if x + item.width > max_width and line:
                end_line()
            pieces = _split_item(item, max_width) if item.width > max_width else [item]
            for index, piece in enumerate(pieces):
                if index and line:
                    end_line()
                line.append((x, piece))
                x += piece.width
    if line or not rows:
        end_line()
    return rows


def _draw_row(
    image: Image.Image,
    draw: ImageDraw.ImageDraw,
    row: _Row,
    top: int,
    scale: int,
    colours: Theme,
    fonts: Fonts,
    assets: Mapping[str, Image.Image],
) -> None:
    """Draw one laid-out row whose top edge is at ``top`` logical px."""
    left = TEXT_X * scale
    if row.block is not None:
        box_top = (top + BLOCK_MARGIN) * scale
        box_height = (len(row.block) * BLOCK_LINE_H + 2 * BLOCK_PAD) * scale
        draw.rounded_rectangle((left, box_top, (WIDTH - PAD) * scale, box_top + box_height), radius=4 * scale, fill=colours.code_bg)
        for index, text in enumerate(row.block):
            y = box_top + (BLOCK_PAD + index * BLOCK_LINE_H + 13) * scale
            draw.text((left + BLOCK_PAD * scale, y), text, font=fonts.mono, fill=colours.text, anchor="ls")
        return
    baseline = (top + BASELINE) * scale
    if row.height > LINE_H:  # a jumbo row: centre the text baseline of any fallback glyph
        baseline = (top + row.height // 2 + 6) * scale
    for offset, item in row.items:
        x = left + offset
        if item.kind in ("emoji", "custom"):
            asset = assets.get(item.ref or "" if item.kind == "emoji" else f"custom:{item.ref}")
            if asset is not None:
                side = item.size * scale
                tile = asset.resize((side, side), Image.Resampling.LANCZOS)
                y = top * scale + (row.height * scale - side) // 2
                image.paste(tile, (x + scale, y), tile)
                continue
        text_x = x
        if item.bg is not None:
            draw.rounded_rectangle((x, (top + 2) * scale, x + item.width, (top + 21) * scale), radius=3 * scale, fill=item.bg)
            text_x = x + PILL_PAD * scale
        if item.text:
            draw.text((text_x, baseline), item.text, font=item.font, fill=item.fill, anchor="ls")
        if item.underline:
            draw.rectangle((x, baseline + 2 * scale, x + item.width - 1, baseline + 3 * scale - 1), fill=item.fill)
        if item.strike:
            draw.rectangle((x, baseline - 5 * scale, x + item.width - 1, baseline - 4 * scale - 1), fill=item.fill)


def _decode_assets(emoji: Mapping[str, bytes] | None) -> dict[str, Image.Image]:
    """Decode supplied emoji images, silently dropping any that cannot be read."""
    decoded: dict[str, Image.Image] = {}
    for key, data in (emoji or {}).items():
        try:
            with Image.open(io.BytesIO(data)) as source:
                decoded[key] = source.convert("RGBA")
        except (OSError, ValueError, Image.DecompressionBombError):
            continue
    return decoded


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
    max_width = (WIDTH - TEXT_X - PAD) * scale
    groups = group_messages(card.messages)
    assets = _decode_assets(emoji)

    laid_out: list[tuple[list[ClipMessage], list[list[_Row]]]] = []
    total = PAD
    for index, group in enumerate(groups):
        message_rows = []
        for message in group:
            spans = tokenize_markdown(message.text, card.mentions)
            message_rows.append(_layout(spans, fonts, colours, scale, max_width, _is_jumbo(spans)))
        laid_out.append((group, message_rows))
        total += HEADER_H + sum(row.height for rows in message_rows for row in rows)
        total += GROUP_GAP if index < len(groups) - 1 else 0
    total += PAD
    height = min(total, MAX_HEIGHT) * scale

    image = Image.new("RGB", (WIDTH * scale, height), colours.bg)
    draw = ImageDraw.Draw(image)
    y = PAD
    for group, message_rows in laid_out:
        first = group[0]
        avatar = _avatar_image(first.author, AVATAR * scale, fonts)
        image.paste(avatar, (PAD * scale, y * scale), avatar)

        header_baseline = (y + BASELINE) * scale
        x = TEXT_X * scale
        name = _short_name(first.author.name)
        draw.text((x, header_baseline), name, font=fonts.name, fill=first.author.color or colours.name, anchor="ls")
        x += int(fonts.name.getlength(name)) + 6 * scale
        if first.author.tag:
            tag_width = int(fonts.tag.getlength(first.author.tag)) + 8 * scale
            draw.rounded_rectangle((x, (y + 3) * scale, x + tag_width, (y + 19) * scale), radius=3 * scale, fill=colours.tag_bg)
            draw.text((x + 4 * scale, header_baseline), first.author.tag, font=fonts.tag, fill=colours.tag_text, anchor="ls")
            x += tag_width + 6 * scale
        draw.text((x, header_baseline), format_time(first.at), font=fonts.time, fill=colours.time, anchor="ls")

        row_top = y + HEADER_H
        for rows in message_rows:
            for row in rows:
                _draw_row(image, draw, row, row_top, scale, colours, fonts, assets)
                row_top += row.height
        y = row_top + GROUP_GAP

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def demo_card() -> ClipCard:
    """A made-up card (no real people or messages) for previewing the look: ``python clip_render.py demo``."""
    pilot = ClipAuthor(1, "Maple", (255, 150, 50), "FOX", None)
    friend = ClipAuthor(2, "Quill", None, None, None)
    start = 1_700_000_000.0
    return ClipCard(
        messages=(
            ClipMessage(pilot, "so i finally **tried** the new place", start),
            ClipMessage(pilot, "it was `fine` honestly", start + 8),
            ClipMessage(friend, "<@1> the soup was ~~good~~ a crime", start + 30),
        ),
        mentions={1: "Maple"},
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) >= 2 and sys.argv[1] == "demo":
        target = Path(sys.argv[2] if len(sys.argv) > 2 else "clip-demo.png")
        target.write_bytes(render_clip(demo_card()))
        print(f"wrote {target}")
    else:
        print("usage: python clip_render.py demo [out.png]")
