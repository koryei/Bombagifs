"""Discord-free decision logic for the Bombagif self-bot.

Everything that decides *whether and what* to convert lives here so it can be unit-tested
with plain fake objects. This module must never import ``discord``.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("bombagif.selfbot")


class AllowlistStore:
    """Persisted set of users who have DMed the account first (user id -> first-DM time)."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self._users: dict[int, float] = {}
        self._load()

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._users

    def __len__(self) -> int:
        return len(self._users)

    def add(self, user_id: int, *, now: float | None = None) -> bool:
        """Allow a user; returns True only if they were newly added."""
        if user_id in self._users:
            return False
        self._users[user_id] = time.time() if now is None else now
        self._save()
        return True

    def remove(self, user_id: int) -> bool:
        """Revoke a user; returns True only if they were present."""
        if user_id not in self._users:
            return False
        del self._users[user_id]
        self._save()
        return True

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not read allowlist %s (%s); starting empty", self.path, exc)
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("allowlist must be a JSON object")
            self._users = {int(key): float(value) for key, value in data.items()}
        except (ValueError, TypeError, OverflowError) as exc:
            aside = self.path.with_name(self.path.name + ".corrupt")
            logger.warning("Allowlist %s is corrupt (%s); moved to %s and starting empty", self.path, exc, aside)
            self._users = {}
            try:
                os.replace(self.path, aside)
            except OSError:
                logger.warning("Could not move corrupt allowlist aside", exc_info=True)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({str(user_id): ts for user_id, ts in sorted(self._users.items())})
        fd, tmp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".allowlist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(payload)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


class RateLimiter:
    """Per-user sliding-window limiter: ``limit`` triggers per ``window`` seconds."""

    def __init__(
        self,
        limit: int = 5,
        window: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._hits: defaultdict[int, deque[float]] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        """Record and allow a trigger, or return False without recording when over the limit."""
        now = self._clock()
        hits = self._hits[user_id]
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


class ChannelKind(enum.Enum):
    """Where a message was sent."""

    DM = "dm"
    GROUP = "group"
    GUILD = "guild"


class Kind(enum.Enum):
    """What the self-bot should do with a message."""

    IGNORE = "ignore"
    REGISTER = "register"
    UNREGISTER = "unregister"
    CONVERT = "convert"


@dataclass(frozen=True)
class Incoming:
    """The facts about a message that trigger decisions depend on."""

    author_id: int
    author_is_bot: bool
    is_own: bool
    is_system: bool
    channel: ChannelKind
    mentions_me: bool
    content: str
    has_payload: bool  # attachments or embeds present
    is_reply: bool


@dataclass(frozen=True)
class Decision:
    """The chosen action; ``convert`` matters only for REGISTER (also convert this message)."""

    kind: Kind
    convert: bool = False


_IGNORE = Decision(Kind.IGNORE)


def decide(msg: Incoming, allowlist: AllowlistStore) -> Decision:
    """Apply the trigger rules: consent comes from DMing the account first."""
    if msg.is_own or msg.author_is_bot or msg.is_system:
        return _IGNORE
    is_member = msg.author_id in allowlist
    wants_stop = msg.content.strip().lower() == "stop"

    if msg.channel is ChannelKind.DM:
        if not is_member:
            return _IGNORE if wants_stop else Decision(Kind.REGISTER, msg.has_payload)
        if wants_stop:
            return Decision(Kind.UNREGISTER)
        if msg.has_payload or msg.is_reply:
            return Decision(Kind.CONVERT)
        return _IGNORE

    if msg.mentions_me and is_member:
        return Decision(Kind.CONVERT)
    return _IGNORE
