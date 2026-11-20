"""In-memory message buffer for the clip scout (discord-free, never persisted).

Keeps a short rolling copy of recent messages in enabled chats, including every edit and deletion, so
a deleted or edited message can still be judged. Nothing here is written to disk.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class Version:
    """One version of a message's text and when it was seen."""

    content: str
    at: float


@dataclass
class ScoutMessage:
    """A buffered chat message with everything the scout needs to judge, render, and credit it."""

    id: int
    chat_id: int
    author_id: int
    author_name: str
    avatar_url: str
    color: int | None
    tag: str | None
    created_at: float
    versions: list[Version]
    eligible: bool = True
    deleted_at: float | None = None
    laugh_reactions: int = 0
    laugh_replies: int = 0
    screened: bool = False
    clipped: bool = False

    @property
    def original(self) -> str:
        """The text as first posted (the version that gets judged and rendered)."""
        return self.versions[0].content

    @property
    def deleted(self) -> bool:
        return self.deleted_at is not None


class ScoutBuffer:
    """Per-chat rolling buffer with a maximum age and size; opted-out authors are never stored."""

    def __init__(
        self,
        *,
        max_age_s: float,
        max_per_chat: int,
        is_opted_out: Callable[[int], bool],
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.max_age_s = max_age_s
        self.max_per_chat = max_per_chat
        self.is_opted_out = is_opted_out
        self.clock = clock
        self._chats: dict[int, OrderedDict[int, ScoutMessage]] = {}

    def prune(self) -> None:
        """Drop messages older than the maximum age, and chats left empty."""
        cutoff = self.clock() - self.max_age_s
        for chat_id in list(self._chats):
            messages = self._chats[chat_id]
            for message_id in [mid for mid, m in messages.items() if m.created_at < cutoff]:
                del messages[message_id]
            if not messages:
                del self._chats[chat_id]

    def chats(self) -> list[int]:
        self.prune()
        return list(self._chats)

    def get(self, chat_id: int, message_id: int) -> ScoutMessage | None:
        self.prune()
        return self._chats.get(chat_id, {}).get(message_id)

    def add(self, message: ScoutMessage) -> bool:
        """Store a message; False if its author opted out or the id is already buffered."""
        self.prune()
        if self.is_opted_out(message.author_id):
            return False
        messages = self._chats.setdefault(message.chat_id, OrderedDict())
        if message.id in messages:
            return False
        messages[message.id] = message
        while len(messages) > self.max_per_chat:
            messages.popitem(last=False)
        return True

    def edit(self, chat_id: int, message_id: int, content: str, at: float | None = None) -> None:
        """Record a new version of a buffered message (unknown messages are ignored)."""
        found = self.get(chat_id, message_id)
        if found is not None:
            found.versions.append(Version(content, self.clock() if at is None else at))

    def delete(self, chat_id: int, message_id: int, at: float | None = None) -> None:
        """Mark a buffered message deleted, keeping its content (unknown messages are ignored)."""
        found = self.get(chat_id, message_id)
        if found is not None and found.deleted_at is None:
            found.deleted_at = self.clock() if at is None else at

    def delete_many(self, chat_id: int, message_ids: list[int], at: float | None = None) -> None:
        for message_id in message_ids:
            self.delete(chat_id, message_id, at)
