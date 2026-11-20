"""Discord-free decision logic for the Bombagif self-bot.

Everything that decides *whether and what* to convert lives here so it can be unit-tested
with plain fake objects. This module must never import ``discord``.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
import tempfile
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bombagif_core import ALLOWED_TYPES, MAX_GIF_BATCH, MediaSource, is_discord_media_url

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
        _write_private_json(self.path, {str(user_id): ts for user_id, ts in sorted(self._users.items())})


def _write_private_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON (temp file + rename) readable only by the owner (mode 600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(json.dumps(data))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class CaptchaGuard:
    """Persistent "stop touching the account" switch, tripped when Discord demands a CAPTCHA.

    A self-bot cannot solve CAPTCHAs, and retrying only invites a harder flag. Once tripped the
    guard stays blocked across restarts until the operator verifies the account by hand and
    deletes the state file.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.blocked = False
        self.reason = ""
        if self.path.exists():
            self.blocked = True
            try:
                self.reason = str(json.loads(self.path.read_text(encoding="utf-8")).get("reason", "unknown"))
            except (OSError, ValueError, AttributeError):
                self.reason = "unknown (state file unreadable)"
            logger.error(
                "CAPTCHA guard is active (%s): all account actions are paused. Log in to the account "
                "manually, then delete %s and restart.",
                self.reason,
                self.path,
            )

    def trip(self, reason: str) -> bool:
        """Block all account actions; returns True only the first time."""
        if self.blocked:
            return False
        self.blocked = True
        self.reason = reason
        try:
            _write_private_json(self.path, {"reason": reason, "at": time.time()})
        except OSError:
            logger.exception("Could not persist the CAPTCHA guard state to %s", self.path)
        logger.error(
            "Discord requested a CAPTCHA during %s: all account actions are paused. Log in to the "
            "account manually, then delete %s and restart.",
            reason,
            self.path,
        )
        return True


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


@dataclass(frozen=True)
class Collected:
    """Media to convert plus how many supported items were dropped by the batch cap."""

    sources: list[MediaSource]
    skipped: int


def _supported_attachments(message: Any) -> list[MediaSource]:
    found: list[MediaSource] = []
    for attachment in getattr(message, "attachments", None) or ():
        suffix = os.path.splitext(attachment.filename.lower())[1]
        if suffix in ALLOWED_TYPES:
            found.append(
                MediaSource(
                    filename=attachment.filename,
                    url=attachment.url,
                    size=attachment.size,
                    content_type=attachment.content_type,
                )
            )
    return found


def _embed_filename(url: str, kind: str) -> str:
    """Name an embed's media from its URL path; video embeds without a known extension get .mp4."""
    name = os.path.basename(urlparse(url).path) or "embed"
    if kind in ("gifv", "video") and os.path.splitext(name.lower())[1] not in ALLOWED_TYPES:
        name += ".mp4"
    return name


def _embed_sources(message: Any) -> list[MediaSource]:
    """Media from image/GIF/video embeds, using only Discord's own proxy URLs."""
    found: list[MediaSource] = []
    for embed in getattr(message, "embeds", None) or ():
        kind = getattr(embed, "type", None)
        if kind in ("gifv", "video"):
            candidates = [getattr(embed, "video", None)]
        elif kind == "image":
            # Discord puts a bare image link's media in `thumbnail`; some embeds use `image`.
            candidates = [getattr(embed, "image", None), getattr(embed, "thumbnail", None)]
        else:
            continue
        for media in candidates:
            url = getattr(media, "proxy_url", None)
            if url and is_discord_media_url(url):
                found.append(MediaSource(filename=_embed_filename(url, kind), url=url))
                break
    return found


def has_media(message: Any) -> bool:
    """True if the message carries attachments or convertible embed media (not plain link previews)."""
    return bool(getattr(message, "attachments", None)) or bool(_embed_sources(message))


def _message_sources(message: Any) -> list[MediaSource]:
    """A message's attachments; only when it has none, its embeds."""
    return _supported_attachments(message) or _embed_sources(message)


def collect_media(message: Any, referenced: Any | None = None, limit: int = MAX_GIF_BATCH) -> Collected:
    """Pick media from the triggering message, else from the message it replies to."""
    found = _message_sources(message)
    if not found and referenced is not None:
        found = _message_sources(referenced)
    return Collected(sources=found[:limit], skipped=max(0, len(found) - limit))


MAX_REPLY_CHARS = 2000

WELCOME_TEXT = (
    "Hey! I turn images and videos into GIF links.\n"
    "• DM me a file and I'll reply with a link.\n"
    "• Anywhere else: reply to a message with media and @ me, or @ me with a file attached.\n"
    "Send `stop` here any time and I'll leave you alone."
)
STOP_CONFIRMATION = "Okay, I won't convert your media anymore. DM me anything to switch me back on."
NO_MEDIA_TEXT = "I couldn't find a supported image or video to convert."

_MARKDOWN_CHARS = re.compile(r"([\\*_~|>`])")


def _escape(text: str) -> str:
    return _MARKDOWN_CHARS.sub(r"\\\1", text)


def format_results(results: list[tuple[str, str, bool]], skipped: int = 0) -> str:
    """Build one reply: a bare link for a lone success, else one labelled line per file."""
    if len(results) == 1 and results[0][2]:
        lines = [results[0][1]]
    else:
        lines = [
            f"**{_escape(name)}** → {value}" if ok else f"**{_escape(name)}**: {value}"
            for name, value, ok in results
        ]
    note = f"(+{skipped} skipped, max {MAX_GIF_BATCH} per message)" if skipped > 0 else None
    if note:
        lines.append(note)
    while len("\n".join(lines)) > MAX_REPLY_CHARS and len(lines) > 1:
        lines.pop()
    return "\n".join(lines)[:MAX_REPLY_CHARS]
