"""The clip scout orchestrator (discord-free): picks candidate runs, judges them, applies the gates, and posts.

The Discord adapter feeds the buffer and supplies ``on_clip``; everything that decides *whether* to clip lives here
so it can be tested with a fake judge and a fake clock.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from scout_buffer import ScoutBuffer, ScoutMessage
from scout_config import ScoutConfig
from scout_judge import JudgeChain, Verdict, build_judge_window
from scout_stores import ClipLedger, ClipRecord, OptOutStore
from selfbot_logic import BotStats

logger = logging.getLogger("bombagif.scout")

RETRY_AFTER_NO_VERDICT_S = 30.0
HOUR_S = 3600.0


@dataclass(frozen=True)
class ClipPlan:
    """A clip the scout decided to make: which messages, the verdict, and where it goes."""

    chat_id: int
    messages: tuple[ScoutMessage, ...]
    verdict: Verdict
    deleted: bool
    to_owner: bool


def _has_signal(run: list[ScoutMessage]) -> bool:
    return any(m.laugh_reactions or m.laugh_replies for m in run)


class Scout:
    """Schedules screening of buffered runs and turns approved verdicts into clip plans."""

    def __init__(
        self,
        cfg: ScoutConfig,
        buffer: ScoutBuffer,
        judge: JudgeChain,
        optout: OptOutStore,
        ledger: ClipLedger,
        on_clip: Callable[[ClipPlan], Awaitable[int | None]],
        *,
        stats: BotStats | None = None,
        paused: Callable[[], bool] = lambda: False,
        blocklist: frozenset[str] = frozenset(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.buffer = buffer
        self.judge = judge
        self.optout = optout
        self.ledger = ledger
        self.on_clip = on_clip
        self.stats = stats if stats is not None else BotStats()
        self.paused = paused
        self.blocklist = blocklist
        self.clock = clock
        self._retry_at: dict[tuple[int, int], float] = {}
        self._chat_last: dict[int, float] = {}
        self._author_last: dict[int, float] = {}
        self._posts: deque[float] = deque()

    # scheduling -----------------------------------------------------------------------------------------------

    def _ready(self, run: list[ScoutMessage], now: float) -> bool:
        """A run is ready when something in it is new, nothing was already clipped, and it has settled."""
        if any(m.clipped for m in run) or all(m.screened for m in run):
            return False
        if any(m.deleted for m in run):
            return True  # deletion fast-path: do not wait out the reaction window
        return now - max(m.created_at for m in run) >= self.cfg.reaction_window_s

    async def tick(self) -> None:
        """One scheduling pass over every buffered chat."""
        if self.cfg.mode == "off" or self.paused():
            return
        try:
            now = self.clock()
            for chat_id in self.buffer.chats():
                for run in self.buffer.runs(chat_id):
                    key = (chat_id, run[0].id)
                    if not self._ready(run, now) or self._retry_at.get(key, 0.0) > now:
                        continue
                    deleted = any(m.deleted for m in run)
                    if self.cfg.screen == "signals" and not deleted and not _has_signal(run):
                        continue
                    await self._screen(chat_id, run, key, now)
                    if self.paused():
                        return
        finally:
            for event in self.judge.drain_events():
                self.stats.note("warn", event)

    async def run(self) -> None:
        """Tick forever; a failing tick is logged and never stops the scout."""
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scout tick failed")
            await asyncio.sleep(self.cfg.tick_s)

    # screening ------------------------------------------------------------------------------------------------

    def _mark_screened(self, run: list[ScoutMessage]) -> None:
        for message in run:
            message.screened = True

    async def _screen(self, chat_id: int, run: list[ScoutMessage], key: tuple[int, int], now: float) -> None:
        if not self.judge.can_post:
            # No LLM judge: surface laughed-at runs for the operator, never post.
            self._mark_screened(run)
            if _has_signal(run) or any(m.deleted for m in run):
                self._record_dry_run(chat_id, tuple(run), None, reason="no LLM judge")
            return
        window = build_judge_window(self.buffer.window(chat_id, run), run)
        verdict = await self.judge.judge(window)
        if verdict is None:
            self._retry_at[key] = now + RETRY_AFTER_NO_VERDICT_S
            return
        self._retry_at.pop(key, None)
        self._mark_screened(run)
        self.stats.scout_screened += 1
        if not verdict.clip:
            return
        self.stats.scout_passed += 1
        await self._act(chat_id, window.messages[verdict.start : verdict.end + 1], verdict)

    # decisions ------------------------------------------------------------------------------------------------

    async def _act(self, chat_id: int, chosen: tuple[ScoutMessage, ...], verdict: Verdict) -> None:
        """Apply the gates to a clip the judge approved, then post it (or record it in dry-run)."""

    def _record_dry_run(
        self, chat_id: int, chosen: tuple[ScoutMessage, ...], verdict: Verdict | None, *, reason: str = ""
    ) -> None:
        self._write(
            ClipRecord(
                at=self.clock(),
                chat_id=chat_id,
                message_ids=tuple(m.id for m in chosen),
                author_ids=tuple(dict.fromkeys(m.author_id for m in chosen)),
                mode="dry-run",
                funny=verdict.funny if verdict else 0.0,
                decide=dict(verdict.decide) if verdict else {},
                deleted=any(m.deleted for m in chosen),
                provider=verdict.provider if verdict else "heuristic",
                reason=reason,
            )
        )
        self.stats.dry_run_hits += 1
        names = ", ".join(dict.fromkeys(m.author_name for m in chosen))
        self.stats.note("info", f"dry-run: candidate from {names} ({reason or 'judged'})")

    def _write(self, record: ClipRecord) -> None:
        try:
            self.ledger.append(record)
        except OSError:
            logger.exception("could not write the clip ledger")
