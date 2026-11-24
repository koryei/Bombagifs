"""The clip scout orchestrator (discord-free): picks candidate runs, judges them, applies the gates, and posts.

The Discord adapter feeds the buffer and supplies ``on_clip``; everything that decides *whether* to clip lives here
so it can be tested with a fake judge and a fake clock.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from scout_buffer import ScoutBuffer, ScoutMessage
from scout_config import ScoutConfig
from scout_judge import JudgeChain, JudgeWindow, Verdict, build_judge_window
from scout_safety import check_window, load_blocklist
from scout_stores import ClipLedger, ClipRecord, OptOutStore, ReviewItem, ReviewStore, ScoutChatStore
from scout_taste import TasteNotes, TasteStore
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
    requested_by: str | None = None  # set for manual ("clip that") clips


REMEMBERED_REVIEWS = 200


@dataclass
class ScoutRuntime:
    """Everything the Discord adapter needs to feed and drive the scout. ``judge``/``scout`` are built once the
    adapter has an HTTP session (see ``selfbot.SelfBot.setup_hook``)."""

    cfg: ScoutConfig
    buffer: ScoutBuffer
    chats: ScoutChatStore
    optout: OptOutStore
    ledger: ClipLedger
    blocklist: frozenset[str] = frozenset()
    emoji_cache: Path | None = None
    taste_notes: TasteNotes | None = None
    taste_store: TasteStore | None = None
    review_store: ReviewStore | None = None
    judge: JudgeChain | None = None
    scout: "Scout | None" = field(default=None, repr=False)


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
        on_review: Callable[[Sequence[ReviewItem]], Awaitable[Sequence[tuple[int, Sequence[ReviewItem]]] | None]] | None = None,
        taste: TasteStore | None = None,
        review_store: ReviewStore | None = None,
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
        self.on_review = on_review
        self.taste = taste
        self._review_store = review_store
        # Candidates the owner can still rate, by DM message id (one DM can hold a batch). Loaded from disk so a
        # restart does not lose them.
        self.reviews: OrderedDict[int, tuple[ReviewItem, ...]] = (
            review_store.load() if review_store is not None else OrderedDict()
        )
        self._pending: list[ReviewItem] = []  # candidates waiting for the next batch DM
        self._pending_since: float | None = None
        self._review_times: deque[float] = deque()
        self._review_seq = max((item.id for items in self.reviews.values() for item in items), default=0)

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
            await self._flush_reviews(now)
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
        # Dry-run is for calibration, so show every verdict (scores only, never the message text); live mode is quiet.
        logger.log(
            logging.INFO if self.cfg.mode == "dry-run" else logging.DEBUG,
            "judged a run in chat %s: clip=%s funny=%.1f via %s (%s)",
            chat_id, verdict.clip, verdict.funny, verdict.provider, verdict.reason,
        )
        if not verdict.clip:
            await self._maybe_review(window, verdict, window.run_start, window.run_end, would_clip=False)
            return
        self.stats.scout_passed += 1
        await self._act(chat_id, window.messages[verdict.start : verdict.end + 1], verdict, window)

    # teaching: candidates the owner can rate ------------------------------------------------------------------

    def _review_enabled(self) -> bool:
        if self.on_review is None:
            return False
        return self.cfg.review == "always" or (self.cfg.review == "dry-run" and self.cfg.mode == "dry-run")

    async def _maybe_review(self, window: JudgeWindow, verdict: Verdict, start: int, end: int, *, would_clip: bool) -> None:
        """Queue a candidate for the owner to rate: would-be clips and near-misses, a few per hour, never raising."""
        if not self._review_enabled():
            return
        near_miss = (
            verdict.decide.get("funny", 0.0) >= self.cfg.near_miss_decide_funny
            or verdict.funny >= self.cfg.near_miss_final_funny
        )
        if not (would_clip or near_miss):
            return
        now = self.clock()
        while self._review_times and now - self._review_times[0] >= HOUR_S:
            self._review_times.popleft()
        if len(self._review_times) >= self.cfg.review_per_hour:
            return
        lines = tuple((line.speaker, line.text) for line in window.lines[start : end + 1] if line.speaker != "-")
        if not lines:
            return
        names = tuple(dict.fromkeys(m.author_name for m in window.messages[start : end + 1] if m.eligible))
        self._review_seq += 1
        item = ReviewItem(self._review_seq, window.messages[start].chat_id, names, lines, verdict.funny, dict(verdict.decide), verdict.reason, would_clip)
        self._review_times.append(now)
        self._pending.append(item)
        if self._pending_since is None:
            self._pending_since = now
        await self._flush_reviews(now)

    async def _flush_reviews(self, now: float) -> None:
        """Send the queued candidates as one DM once the batch is full or has waited long enough.

        Batching keeps a busy chat from turning into a stream of one-candidate DMs. A failed send drops the batch:
        the candidates are still in the ledger, and retrying a stale DM is worse than losing one review.
        """
        if not self._pending or self.on_review is None:
            return
        waited = now - self._pending_since if self._pending_since is not None else 0.0
        if len(self._pending) < self.cfg.review_batch and waited < self.cfg.review_batch_minutes * 60:
            return
        batch = tuple(self._pending)
        self._pending.clear()
        self._pending_since = None
        try:
            sent = await self.on_review(batch)  # type: ignore[misc]
        except Exception:
            logger.exception("could not send a review batch")
            return
        if not sent:
            return
        for dm_id, items in sent:
            self.reviews[int(dm_id)] = tuple(items)
        while len(self.reviews) > REMEMBERED_REVIEWS:
            self.reviews.popitem(last=False)
        self._save_reviews()

    def _save_reviews(self) -> None:
        """Write the pending candidates to disk; a full disk must not stop the scout from clipping."""
        if self._review_store is None:
            return
        try:
            self._review_store.save(self.reviews)
        except OSError:
            logger.warning("Could not save the pending reviews", exc_info=True)

    async def rate(self, message_id: int, label: str, *, source: str = "review") -> bool:
        """Apply the owner's rating (``good``/``bad``/``skip``) to a reviewed candidate. True if it was saved or skipped."""
        return await self.rate_many(message_id, {0: label}, source=source)

    async def rate_many(self, message_id: int, labels: Mapping[int, str], *, source: str = "review") -> bool:
        """Rate several candidates from one batch DM at once, by 0-based position within the batch.

        Candidates left out of ``labels`` stay pending, so the owner can rate a batch in pieces. True if anything
        was saved or skipped.
        """
        batch = self.reviews.get(message_id)
        if batch is None:
            return False
        rated = {index: label for index, label in labels.items() if 0 <= index < len(batch)}
        if not rated:
            return False
        kept = tuple(item for index, item in enumerate(batch) if index not in rated)
        if kept:
            self.reviews[message_id] = kept
        else:
            del self.reviews[message_id]
        self._save_reviews()
        saved = False
        for index, label in rated.items():
            if label == "skip":
                saved = True
                continue
            if self.taste is None:
                continue
            item = batch[index]
            saved = self.taste.add(label, item.lines, funny=item.funny, decide=item.decide, source=source) or saved
        return saved

    # decisions ------------------------------------------------------------------------------------------------

    def _reject(self, chat_id: int, chosen: tuple[ScoutMessage, ...], verdict: Verdict, reason: str) -> None:
        names = ", ".join(dict.fromkeys(m.author_name for m in chosen))
        self.stats.scout_rejected += 1
        self.stats.note("warn", f"clip rejected: {reason} ({names})")
        logger.info("clip rejected: %s", reason)
        self._write(
            ClipRecord(
                at=self.clock(),
                chat_id=chat_id,
                message_ids=tuple(m.id for m in chosen),
                author_ids=tuple(dict.fromkeys(m.author_id for m in chosen)),
                mode="rejected",
                funny=verdict.funny,
                decide=dict(verdict.decide),
                deleted=any(m.deleted for m in chosen),
                provider=verdict.provider,
                reason=reason,
            )
        )

    def _gate(self, chat_id: int, chosen: tuple[ScoutMessage, ...], verdict: Verdict, now: float) -> str | None:
        """Return why a clip must not be posted, or ``None`` if every gate passes. Order matches the spec."""
        cfg = self.cfg
        if any(m.deleted for m in chosen):
            if cfg.clip_deleted == "off":
                return "deleted clips are off"
            if verdict.funny < cfg.deleted_funny_min or verdict.decide.get("sensitive", 1.0) > cfg.deleted_sensitive_max:
                return "deleted bar"
        safety = check_window([m.original for m in chosen], self.blocklist)
        if not safety.ok:
            return "safety: " + ",".join(safety.reasons)
        if any(m.author_id in self.optout for m in chosen):
            return "opted out"
        if any(m.clipped for m in chosen):
            return "already clipped"
        if now - self._chat_last.get(chat_id, float("-inf")) < cfg.chat_cooldown_s:
            return "chat cooldown"
        if any(now - self._author_last.get(m.author_id, float("-inf")) < cfg.author_cooldown_s for m in chosen):
            return "author cooldown"
        while self._posts and now - self._posts[0] >= HOUR_S:
            self._posts.popleft()
        if len(self._posts) >= cfg.max_per_hour:
            return "hourly cap"
        if self.paused():
            return "paused"
        return None

    def _commit(self, chat_id: int, chosen: tuple[ScoutMessage, ...], now: float) -> None:
        """Remember a clip that went out: flag its messages and start the cooldowns."""
        for message in chosen:
            message.clipped = True
            self._author_last[message.author_id] = now
        self._chat_last[chat_id] = now
        self._posts.append(now)

    async def _act(self, chat_id: int, chosen: tuple[ScoutMessage, ...], verdict: Verdict, window: JudgeWindow) -> None:
        """Apply the gates to a clip the judge approved, then post it."""
        now = self.clock()
        reason = self._gate(chat_id, chosen, verdict, now)
        if reason is not None:
            self._reject(chat_id, chosen, verdict, reason)
            return
        deleted = any(m.deleted for m in chosen)
        if self.cfg.mode == "dry-run":
            self._commit(chat_id, chosen, now)  # mimic cooldowns so dry-run counts match what "on" would do
            self._record_dry_run(chat_id, chosen, verdict)
            await self._maybe_review(window, verdict, verdict.start, verdict.end, would_clip=True)
            return
        plan = ClipPlan(
            chat_id=chat_id,
            messages=chosen,
            verdict=verdict,
            deleted=deleted,
            to_owner=deleted and self.cfg.clip_deleted == "owner",
        )
        posted_id = await self.on_clip(plan)
        if posted_id is None:
            self._reject(chat_id, chosen, verdict, "post failed")
            return
        self._commit(chat_id, chosen, now)
        names = ", ".join(dict.fromkeys(m.author_name for m in chosen))
        if plan.to_owner:
            self.stats.deleted_rescued += 1
        else:
            self.stats.clips_posted += 1
            if deleted:
                self.stats.deleted_rescued += 1
        self.stats.note("ok", f"clipped {names} (funny {verdict.funny:.1f}{', deleted' if deleted else ''})")
        self._write(
            ClipRecord(
                at=now,
                chat_id=chat_id,
                message_ids=tuple(m.id for m in chosen),
                author_ids=tuple(dict.fromkeys(m.author_id for m in chosen)),
                mode="owner" if plan.to_owner else "posted",
                funny=verdict.funny,
                decide=dict(verdict.decide),
                deleted=deleted,
                provider=verdict.provider,
                reason=verdict.reason,
                posted_message_id=None if plan.to_owner else posted_id,
                text=tuple(m.original for m in chosen),
                speakers=tuple(line.speaker for line in window.lines[verdict.start : verdict.end + 1]),
            )
        )

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
        if verdict is None:
            self.stats.note("info", f"dry-run: candidate from {names} ({reason})")
        else:
            deleted = " [deleted]" if any(m.deleted for m in chosen) else ""
            self.stats.note("info", f"dry-run: would clip {names} (funny {verdict.funny:.1f}){deleted}")

    def _write(self, record: ClipRecord) -> None:
        try:
            self.ledger.append(record)
        except OSError:
            logger.exception("could not write the clip ledger")


def build_scout_runtime(cfg: ScoutConfig, data_dir: Path) -> ScoutRuntime | None:
    """Create the scout's stores and buffer under ``data_dir``.

    Returns ``None`` when the scout is off and no owner is configured. With mode ``off`` but an owner set it still
    builds a runtime (without scouting) so the owner's commands can explain why it is refused. The judge
    chain and the scout loop are created later, once the adapter has an HTTP session.
    """
    if cfg.mode == "off" and cfg.owner_id is None:
        return None
    optout = OptOutStore(data_dir / "optout.json")
    return ScoutRuntime(
        cfg=cfg,
        buffer=ScoutBuffer(
            max_age_s=cfg.buffer_minutes * 60.0,
            max_per_chat=cfg.buffer_max_per_chat,
            is_opted_out=lambda user_id: user_id in optout,
        ),
        chats=ScoutChatStore(data_dir / "scout-chats.json", seed=cfg.seed_chats),
        optout=optout,
        ledger=ClipLedger(data_dir / "clips.jsonl"),
        blocklist=load_blocklist(data_dir / "scout-blocklist.txt"),
        emoji_cache=data_dir / "emoji-cache",
        taste_notes=TasteNotes(data_dir / "scout-taste.txt"),
        taste_store=TasteStore(data_dir / "taste.jsonl", per_label=cfg.examples_per_label),
        review_store=ReviewStore(data_dir / "reviews.json"),
    )


# notice text and owner commands --------------------------------------------------------------------------------

ANNOUNCE_ON = (
    "🎬 Heads up: I'm clipping messages that are funny out of context in this chat, and I'll name whoever is in "
    "them. DM me `stop` if you'd rather I didn't clip you."
)
ANNOUNCE_OFF = "🎬 Stopped clipping in this chat."
DELETED_SUFFIX = " (deleted, but not fast enough)"
MAX_NAME_CHARS = 32
_MD_SPECIALS = re.compile(r"([\\*_~|>`])")


def _notice_name(name: str) -> str:
    """A display name made safe for the notice: truncated, markdown-escaped, and never a mention."""
    if len(name) > MAX_NAME_CHARS:
        name = name[: MAX_NAME_CHARS - 1] + "…"
    name = _MD_SPECIALS.sub(r"\\\1", name)
    return name.replace("<", "<\u200b").replace("@", "@\u200b")


def format_clip_notice(names: Sequence[str], deleted: bool, requested_by: str | None = None) -> str:
    """The text posted with a clip: who is in it, who asked (for manual clips), how to opt out, and a deleted snark."""
    unique = [_notice_name(name) for name in dict.fromkeys(names)]
    bold = [f"**{name}**" for name in unique]
    if len(bold) <= 1:
        who = "".join(bold)
    else:
        who = ", ".join(bold[:-1]) + " and " + bold[-1]
    asked = f" at **{_notice_name(requested_by)}**'s request" if requested_by and requested_by.strip() else ""
    text = f"🎬 Clipped {who}{asked} — DM me `stop` to opt out of clips."
    return text + DELETED_SUFFIX if deleted else text


@dataclass(frozen=True)
class ScoutCommand:
    """A parsed owner command (``!on|off [chat id]``, ``status``, ``list``, ``taste``, ``examples``, ``tune``, ``help``)."""

    action: Literal[
        "on", "off", "status", "list", "taste", "taste_add", "taste_clear", "examples", "examples_clear", "tune", "help",
        "join", "leave", "voice",
    ]
    chat_id: int | None
    arg: str | None = None


_COMMAND_RE = re.compile(r"^!(?:scout\s+)?(on|off|status|list)(?:\s+(\d+))?$", re.IGNORECASE)
_SIMPLE_RE = re.compile(r"^!(?:scout\s+)?(taste|examples|tune|help|join|leave|voice)(?:\s+(clear))?$", re.IGNORECASE)
_TASTE_ADD_RE = re.compile(r"^!(?:scout\s+)?taste\s+add\s+(.+)$", re.IGNORECASE | re.DOTALL)
_SIMPLE_ACTIONS = {"taste": "taste", "examples": "examples", "tune": "tune", "help": "help", "join": "join", "leave": "leave", "voice": "voice"}


def parse_scout_command(text: str) -> ScoutCommand | None:
    """Parse an owner command like ``!on`` (the ``scout`` word is optional: ``!scout on`` works too); anything that is not exactly a command returns ``None``."""
    text = text.strip()
    added = _TASTE_ADD_RE.match(text)
    if added is not None:
        body = added.group(1).strip()
        return ScoutCommand("taste_add", None, body) if body else None
    simple = _SIMPLE_RE.match(text)
    if simple is not None:
        action, clear = simple.group(1).lower(), simple.group(2)
        if clear:
            return ScoutCommand(f"{action}_clear", None) if action in ("taste", "examples") else None  # type: ignore[arg-type]
        return ScoutCommand(_SIMPLE_ACTIONS[action], None)  # type: ignore[arg-type]
    match = _COMMAND_RE.match(text)
    if match is None:
        return None
    action = match.group(1).lower()
    raw_id = match.group(2)
    if raw_id is not None:
        if action not in ("on", "off") or int(raw_id) <= 0:
            return None
        return ScoutCommand(action, int(raw_id))  # type: ignore[arg-type]
    return ScoutCommand(action, None)  # type: ignore[arg-type]


_HELP_RE = re.compile(r"^!help$", re.IGNORECASE)


def parse_help_command(text: str) -> bool:
    """True for exactly ``!help`` (any case, surrounding whitespace ignored)."""
    return _HELP_RE.match(text.strip()) is not None


_RATING_WORDS = {"good": "good", "yes": "good", "y": "good", "👍": "good", "bad": "bad", "no": "bad", "n": "bad", "👎": "bad", "skip": "skip", "🙈": "skip"}
_EMOJI_MODIFIERS = re.compile("[\U0001F3FB-\U0001F3FF\uFE0F]")


def parse_rating(text: str) -> Literal["good", "bad", "skip"] | None:
    """Read an owner's reply as a rating: ``good``/``bad``/``skip`` (optionally ``!``-prefixed), ``yes``/``no``, or one 👍/👎/🙈.

    Anything else (including a sentence that merely contains one of those words) is not a rating.
    """
    word = _EMOJI_MODIFIERS.sub("", text.strip().lower())
    if word.startswith("!"):
        word = word[1:]
    return _RATING_WORDS.get(word)  # type: ignore[return-value]


_BATCH_RATING_RE = re.compile(r"(\d+)\s*[:.)\-]?\s*(good|bad|skip|yes|no|[gbs])(?![a-z])")
_BATCH_LETTERS = {"g": "good", "b": "bad", "s": "skip"}


def parse_ratings(text: str, count: int) -> dict[int, str]:
    """Read a reply to a batch DM as ``{0-based index: rating}``, e.g. ``1 good 2 bad`` or ``1g, 3s``.

    Numbers are 1-based, as shown in the DM. Anything out of range for ``count`` is dropped, so a typo cannot rate
    the wrong candidate. An empty result means the reply was not a batch rating.
    """
    ratings: dict[int, str] = {}
    for match in _BATCH_RATING_RE.finditer(text.lower()):
        index = int(match.group(1)) - 1
        if 0 <= index < count:
            word = match.group(2)
            ratings[index] = _BATCH_LETTERS.get(word) or _RATING_WORDS[word]
    return ratings


HELP_OWNER = (
    "**Bombagif: owner guide** (I answer commands by DM)\n"
    "**Clip scout**\n"
    "`!on` / `!off`: turn clipping on or off in the chat you type it in "
    "(DM me `!on <chat id>` for another chat)\n"
    "`!status` and `!list`: how the scout is doing, and where it is on\n"
    "**Teaching it**\n"
    "`!taste` shows my taste notes, `!taste add <text>` adds a line (e.g. \"we like deadpan one-liners, skip "
    "anything about exams\"), `!taste clear` wipes them\n"
    "`!examples` shows what you've rated, `!examples clear` forgets it\n"
    "`!tune` compares your ratings with my scores and suggests thresholds\n"
    "In dry-run I DM you candidates and near-misses, a few to a message. Reply `good`, `bad` or `skip`, numbered when "
    "there are several (`1 good 2 bad 3 skip`). You can also reply `good`/`bad` to any clip I posted. Your ratings "
    "become examples the judge sees.\n"
    "**Voice**\n"
    "`!join` makes me listen in your call, server or group DM (I join undeafened, so everyone sees it); `!leave` "
    "stops. Then say `clip that` in a text channel, or `clip Pranit that`, or `clip what he just said`, and I post "
    "the last 30s as audio. `!voice` says whether I'm listening.\n"
    "**Clip that**\n"
    "In chats where scouting is on, say or reply `clip that` (no ping needed; typos and \"lmfao\" are fine) and I clip "
    "that message, or the latest one if you don't reply. Replying `gif that` to an image or "
    "video makes a GIF. Members can do this in scouting chats; you can also reply and @ me anywhere. Opted-out people "
    "and sensitive text are always refused, silently when nobody pinged me.\n"
    "**Everyone**\n"
    "`stop` in a DM opts out of clips and conversions, `start` brings you back. "
    "`!help` in a DM with me shows the short guide. Settings live in `.env` (see `.env.example`)."
)

HELP_USER = (
    "**Hi, I'm Bombagif.** I turn images and videos into GIF links.\n"
    "• DM me `hi` first, then @ me on an image or video, or reply to one and @ me.\n"
    "• In chats where clipping is on, reply `clip that` (no ping needed) and I'll clip that message as a quote card.\n"
    "• In those chats I also clip messages that are funny out of context, and I say who's in them.\n"
    "Send `stop` in a DM to opt out of clips and conversions, and `start` to come back."
)
