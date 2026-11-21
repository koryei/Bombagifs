"""The gate for "clip that" requests (discord-free): who may be clipped, where, and how often.

A person asked for the clip, so the judge is skipped, but every protection that does not depend on humour still
applies: opt-outs, plain text only, the local sensitive filter, the enabled-chat rule, and rate limits.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Container, Sequence

from scout_buffer import ScoutMessage
from scout_config import ScoutConfig
from scout_judge import MAX_CLIP_CHARS
from scout_safety import check_window

REFUSAL_OPTED_OUT = "That person opted out of clips."
REFUSAL_NOT_TEXT = "I can only clip plain text messages."
REFUSAL_CANT = "I can't clip that one."
REFUSAL_NOT_ENABLED = "Clipping isn't on in this chat."
REFUSAL_ALREADY = "That one was already clipped."
REFUSAL_SLOW = "Slow down, try again in a minute."

REMEMBERED_CLIPS = 1000
WINDOW_S = 600.0


class ManualGate:
    """Checks a manual clip request and remembers what was clipped. Refusals never quote the message."""

    def __init__(self, cfg: ScoutConfig, clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self._clock = clock
        self._requests: dict[int, deque[float]] = {}
        self._author_last: dict[int, float] = {}
        self._order: deque[tuple[int, int]] = deque()
        self._ids: set[tuple[int, int]] = set()

    def check(
        self,
        chosen: Sequence[ScoutMessage],
        *,
        requester_id: int,
        is_owner: bool,
        chat_enabled: bool,
        optout: Container[int],
        blocklist: frozenset[str],
        self_id: int | None,
        target_is_bot: bool = False,
    ) -> str | None:
        """The refusal to send the requester, or ``None`` if the clip may go ahead."""
        if not chosen or target_is_bot or any(m.author_id == self_id for m in chosen):
            return REFUSAL_CANT
        if any(m.author_id in optout for m in chosen):
            return REFUSAL_OPTED_OUT
        texts = [m.original for m in chosen]
        if not all(m.eligible for m in chosen) or sum(len(text) for text in texts) > MAX_CLIP_CHARS:
            return REFUSAL_NOT_TEXT
        if not check_window(texts, blocklist).ok:
            return REFUSAL_CANT
        if not (is_owner or chat_enabled):
            return REFUSAL_NOT_ENABLED
        if any(m.clipped or (m.chat_id, m.id) in self._ids for m in chosen):
            return REFUSAL_ALREADY
        if not is_owner:
            now = self._clock()
            recent = self._requests.get(requester_id, deque())
            while recent and now - recent[0] >= WINDOW_S:
                recent.popleft()
            if len(recent) >= self.cfg.manual_limit_per_10_min:
                return REFUSAL_SLOW
            if any(now - self._author_last.get(m.author_id, float("-inf")) < self.cfg.manual_author_cooldown_s for m in chosen):
                return REFUSAL_SLOW
        return None

    def commit(self, chosen: Sequence[ScoutMessage], requester_id: int, is_owner: bool) -> None:
        """Remember a clip that went out: flag its messages and start the cooldowns."""
        now = self._clock()
        for message in chosen:
            message.clipped = True
            self._author_last[message.author_id] = now
            key = (message.chat_id, message.id)
            if key not in self._ids:
                if len(self._order) >= REMEMBERED_CLIPS:
                    self._ids.discard(self._order.popleft())
                self._order.append(key)
                self._ids.add(key)
        if not is_owner:
            self._requests.setdefault(requester_id, deque()).append(now)
