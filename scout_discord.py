"""Maps discord.py-self objects to the scout's discord-free types. Adapter-only: this module imports ``discord``."""

from __future__ import annotations

import discord

from scout_buffer import ScoutMessage, Version
from scout_safety import is_eligible_text


def chat_is_scoutable(channel: object) -> bool:
    """Guild channels and group DMs may be scouted (if enabled); 1:1 DMs and NSFW channels never are."""
    if isinstance(channel, discord.DMChannel):
        return False
    if isinstance(channel, discord.GroupChannel):
        return True
    if getattr(channel, "guild", None) is None:
        return False
    is_nsfw = getattr(channel, "is_nsfw", None)
    return not (callable(is_nsfw) and is_nsfw())


def message_is_clippable(message: discord.Message) -> bool:
    """A normal text-only message: no attachments, stickers, links, spoilers, or command-like text."""
    return (
        not message.is_system()
        and not message.attachments
        and not getattr(message, "stickers", None)
        and is_eligible_text(message.content or "")
    )


def _avatar_url(author: object) -> str:
    avatar = getattr(author, "display_avatar", None)
    if avatar is None:
        return ""
    try:
        return str(avatar.with_size(128).url)
    except (AttributeError, ValueError, TypeError):
        return str(getattr(avatar, "url", avatar))


def to_scout_message(message: discord.Message, *, eligible: bool) -> ScoutMessage:
    """Snapshot a message for the buffer. Non-text messages keep no text (they become ``[not text]`` placeholders)."""
    author = message.author
    created = message.created_at.timestamp()
    colour = getattr(getattr(author, "color", None), "value", 0)
    guild_tag = getattr(author, "primary_guild", None)
    tag = getattr(guild_tag, "tag", None) if guild_tag is not None and getattr(guild_tag, "identity_enabled", True) else None
    return ScoutMessage(
        id=message.id,
        chat_id=message.channel.id,
        author_id=author.id,
        author_name=str(getattr(author, "display_name", None) or getattr(author, "name", None) or author.id),
        avatar_url=_avatar_url(author),
        color=colour or None,
        tag=tag or None,
        created_at=created,
        versions=[Version(message.content if eligible else "", created)],
        eligible=eligible,
    )
