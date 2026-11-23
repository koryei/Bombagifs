"""Small persistent stores for the clip scout: opt-outs, enabled chats, and the clip ledger (discord-free)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from collections import OrderedDict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from private_files import append_private_line, write_private_json
from selfbot_logic import AllowlistStore

logger = logging.getLogger("bombagif.selfbot")


class OptOutStore(AllowlistStore):
    """Users who asked not to be clipped (same atomic, corrupt-file-safe storage as the allowlist)."""


class ScoutChatStore:
    """Chats where the owner turned the scout on. ``seed`` chats (from the environment) are always on."""

    def __init__(self, path: str | os.PathLike[str], seed: Iterable[int] = ()) -> None:
        self.path = Path(path)
        self._seed = frozenset(seed)
        self._chats: dict[int, dict[str, object]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not read scout chat list %s (%s); starting empty", self.path, exc)
            return
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("scout chat list must be a JSON object")
            self._chats = {int(key): dict(value) for key, value in data.items()}
        except (ValueError, TypeError) as exc:
            aside = self.path.with_name(self.path.name + ".corrupt")
            logger.warning("Scout chat list %s is corrupt (%s); moved to %s and starting empty", self.path, exc, aside)
            self._chats = {}
            try:
                os.replace(self.path, aside)
            except OSError:
                logger.warning("Could not move the corrupt scout chat list aside", exc_info=True)

    def _save(self) -> None:
        write_private_json(self.path, {str(chat_id): info for chat_id, info in sorted(self._chats.items())})

    def is_seeded(self, chat_id: int) -> bool:
        return chat_id in self._seed

    def enabled(self, chat_id: int) -> bool:
        return chat_id in self._seed or chat_id in self._chats

    def enable(self, chat_id: int, name: str = "") -> bool:
        """Turn a chat on; False if it already was (including seeded chats)."""
        if self.enabled(chat_id):
            return False
        self._chats[chat_id] = {"name": name, "at": time.time()}
        self._save()
        return True

    def disable(self, chat_id: int) -> bool:
        """Turn a chat off; False if it was not on, or is seeded from the environment."""
        if chat_id in self._seed or chat_id not in self._chats:
            return False
        del self._chats[chat_id]
        self._save()
        return True

    def list(self) -> list[tuple[int, str]]:
        names = {chat_id: str(info.get("name", "")) for chat_id, info in self._chats.items()}
        for chat_id in self._seed:
            names.setdefault(chat_id, "")
        return sorted(names.items())


@dataclass(frozen=True)
class ClipRecord:
    """One ledger line: what the scout did with a candidate that the judge liked."""

    at: float
    chat_id: int
    message_ids: tuple[int, ...]
    author_ids: tuple[int, ...]
    mode: Literal["posted", "dry-run", "owner", "rejected"]
    funny: float
    decide: Mapping[str, float]
    deleted: bool
    provider: str
    reason: str = ""
    posted_message_id: int | None = None
    text: tuple[str, ...] | None = None
    speakers: tuple[str, ...] | None = None  # anonymous speaker label per text line (A, B, C), for teaching examples


@dataclass(frozen=True)
class ReviewItem:
    """A candidate the owner may rate. ``lines`` are anonymous (speaker label, original text); ``names`` are for the DM."""

    id: int
    chat_id: int
    names: tuple[str, ...]
    lines: tuple[tuple[str, str], ...]
    funny: float
    decide: Mapping[str, float]
    reason: str
    would_clip: bool


class ClipLedger:
    """Append-only JSON-lines record of clips (mode 600). Text is stored only for posted/owner clips."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def append(self, record: ClipRecord) -> None:
        line = json.dumps(dataclasses.asdict(record), ensure_ascii=False, separators=(",", ":"))
        append_private_line(self.path, line)

    def recent_posted_ids(self, limit: int = 500) -> set[int]:
        """Message ids of the clips we posted in the last ``limit`` ledger lines (junk lines are skipped)."""
        try:
            with self.path.open(encoding="utf-8") as handle:
                lines = list(deque((line for line in handle if line.strip()), maxlen=limit))
        except OSError:
            return set()
        found: set[int] = set()
        for line in lines:
            try:
                value = json.loads(line).get("posted_message_id")
            except (ValueError, AttributeError):
                continue
            if isinstance(value, int) and not isinstance(value, bool):
                found.add(value)
        return found


    def find_posted(self, message_id: int, limit: int = 500) -> ClipRecord | None:
        """The ledger record of the clip we posted as ``message_id`` (searching the last ``limit`` lines), if any."""
        try:
            with self.path.open(encoding="utf-8") as handle:
                lines = list(deque((line for line in handle if line.strip()), maxlen=limit))
        except OSError:
            return None
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("posted_message_id") != message_id:
                continue
            try:
                text, speakers = row.get("text"), row.get("speakers")
                return ClipRecord(
                    at=float(row["at"]),
                    chat_id=int(row["chat_id"]),
                    message_ids=tuple(int(i) for i in row["message_ids"]),
                    author_ids=tuple(int(i) for i in row["author_ids"]),
                    mode=row["mode"],
                    funny=float(row["funny"]),
                    decide={str(k): float(v) for k, v in dict(row["decide"]).items()},
                    deleted=bool(row["deleted"]),
                    provider=str(row["provider"]),
                    reason=str(row.get("reason", "")),
                    posted_message_id=message_id,
                    text=tuple(str(x) for x in text) if isinstance(text, list) else None,
                    speakers=tuple(str(x) for x in speakers) if isinstance(speakers, list) else None,
                )
            except (KeyError, TypeError, ValueError):
                continue
        return None


def _review_item(row: object) -> ReviewItem:
    """Parse one stored candidate, raising ValueError/TypeError/KeyError when it is malformed."""
    if not isinstance(row, dict):
        raise ValueError("a pending review must be an object")
    return ReviewItem(
        id=int(row["id"]),
        chat_id=int(row["chat_id"]),
        names=tuple(str(name) for name in row["names"]),
        lines=tuple((str(speaker), str(text)) for speaker, text in row["lines"]),
        funny=float(row["funny"]),
        decide={str(key): float(value) for key, value in dict(row["decide"]).items()},
        reason=str(row.get("reason", "")),
        would_clip=bool(row["would_clip"]),
    )


class ReviewStore:
    """``data/reviews.json``: candidates still waiting for the owner's rating, so a restart does not lose them."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def load(self) -> OrderedDict[int, ReviewItem]:
        """Pending candidates by DM message id, oldest first. A missing file means none; a corrupt one is moved aside."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return OrderedDict()
        except OSError as exc:
            logger.warning("Could not read pending reviews %s (%s); starting empty", self.path, exc)
            return OrderedDict()
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("pending reviews must be a JSON object")
            return OrderedDict((int(key), _review_item(row)) for key, row in data.items())
        except (ValueError, TypeError, KeyError) as exc:
            aside = self.path.with_name(self.path.name + ".corrupt")
            logger.warning("Pending reviews %s are corrupt (%s); moved to %s and starting empty", self.path, exc, aside)
            try:
                os.replace(self.path, aside)
            except OSError:
                logger.warning("Could not move the corrupt pending reviews aside", exc_info=True)
            return OrderedDict()

    def save(self, reviews: Mapping[int, ReviewItem]) -> None:
        """Replace the file with ``reviews`` (atomic, mode 600)."""
        write_private_json(self.path, {str(key): dataclasses.asdict(item) for key, item in reviews.items()})
