"""Small persistent stores for the clip scout: opt-outs, enabled chats, and the clip ledger (discord-free)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from collections import deque
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
