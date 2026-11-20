"""Asset URLs and card building for clips (discord-free).

Only Discord's own hosts and one pinned emoji host may be fetched, so a chat message can never make the bot request an
arbitrary address.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from urllib.parse import urlparse

from bombagif_core import is_discord_media_url
from clip_render import ClipAuthor, ClipCard, ClipMessage
from scout_buffer import ScoutMessage

EMOJI_HOST = "cdn.jsdelivr.net"
TWEMOJI_VERSION = "15.1.0"
_TWEMOJI_PATH = f"/gh/jdecked/twemoji@{TWEMOJI_VERSION}/assets/72x72/"
_KEY_RE = re.compile(r"^[0-9a-f]+(?:-[0-9a-f]+)*$")
_DIGITS_RE = re.compile(r"^\d+$")
_MENTION_ID_RE = re.compile(r"<@!?(\d+)>")


def twemoji_url(key: str) -> str:
    """The pinned Twemoji 72x72 PNG for an emoji key like ``1f602`` or ``1f468-200d-1f469``."""
    if not _KEY_RE.match(key):
        raise ValueError(f"not a valid emoji key: {key!r}")
    return f"https://{EMOJI_HOST}{_TWEMOJI_PATH}{key}.png"


def custom_emoji_url(emoji_id: str) -> str:
    """Discord's CDN image for a custom emoji id."""
    if not _DIGITS_RE.match(emoji_id):
        raise ValueError(f"not a valid emoji id: {emoji_id!r}")
    return f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=64"


def asset_url_allowed(url: str) -> bool:
    """True for Discord media URLs and the pinned Twemoji path on the emoji host; everything else is refused."""
    if is_discord_media_url(url):
        return True
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == EMOJI_HOST
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and parsed.path.startswith(_TWEMOJI_PATH)
    )


def rgb(value: int | None) -> tuple[int, int, int] | None:
    """Split a Discord colour integer into RGB; ``0``/``None`` (the default colour) means no custom colour."""
    if not value:
        return None
    return ((value >> 16) & 255, (value >> 8) & 255, value & 255)


def mentioned_ids(texts: Iterable[str]) -> set[int]:
    """User ids mentioned as ``<@id>`` / ``<@!id>`` in the texts."""
    return {int(found) for text in texts for found in _MENTION_ID_RE.findall(text)}


def build_card(
    messages: Sequence[ScoutMessage],
    avatars: Mapping[int, bytes],
    mentions: Mapping[int, str],
) -> ClipCard:
    """Turn buffered messages into a card: original text, one shared author object per person."""
    authors: dict[int, ClipAuthor] = {}
    cards: list[ClipMessage] = []
    for message in messages:
        author = authors.get(message.author_id)
        if author is None:
            author = authors[message.author_id] = ClipAuthor(
                id=message.author_id,
                name=message.author_name,
                color=rgb(message.color),
                tag=message.tag,
                avatar=avatars.get(message.author_id),
            )
        cards.append(ClipMessage(author=author, text=message.original, at=message.created_at))
    return ClipCard(messages=tuple(cards), mentions=dict(mentions))
