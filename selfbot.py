"""Bombagif self-bot: a user-account front door that answers pings with GIF links.

Runs on ``discord.py-self`` from its own virtualenv (see requirements-selfbot.txt); it shares the
conversion pipeline with the slash-command app through ``bombagif_core``.

WARNING: automating a user account violates Discord's Terms of Service. Use an alt account.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import platform
import re
import signal
import string
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import discord
from dotenv import load_dotenv

from bombagif_core import (
    DOWNLOAD_TIMEOUT,
    MAX_GIF_BATCH,
    MediaSource,
    Settings,
    UserFacingError,
    configure_logging,
    convert_and_upload,
    download_url,
    read_settings,
)
from clip_assets import asset_url_allowed, build_card, custom_emoji_url, mentioned_ids, twemoji_url
from clip_render import collect_asset_keys, render_clip
from scout_buffer import ScoutMessage
from scout import (
    ANNOUNCE_OFF,
    ANNOUNCE_ON,
    HELP_OWNER,
    HELP_USER,
    ClipPlan,
    ReviewItem,
    Scout,
    ScoutRuntime,
    build_scout_runtime,
    format_clip_notice,
    parse_help_command,
    parse_scout_command,
)
from scout_config import read_scout_config
from scout_judge import Verdict, build_judge_chain
from scout_manual import ManualGate
from scout_stores import ClipRecord
from scout_taste import NOTES_MAX_CHARS, TasteBook, tune_report
from scout_discord import chat_is_scoutable, message_is_clippable, to_scout_message
from selfbot_logic import (
    CLIP_OPT_OUT_CONFIRMATION,
    NO_MEDIA_TEXT,
    START_CONFIRMATION,
    STOP_CONFIRMATION,
    AllowlistStore,
    BotStats,
    CaptchaGuard,
    ChannelKind,
    Cooldown,
    Incoming,
    Kind,
    RateLimiter,
    collect_media,
    decide,
    env_flag,
    format_results,
    has_media,
    help_text,
    resolve_ui_mode,
    welcome_text,
)


_MARKDOWN_SPECIALS = re.compile(r"([\\*_~|>`])")


def _escape_markdown(text: str) -> str:
    """Backslash-escape Discord markdown characters (so a quoted line shows exactly as written)."""
    return _MARKDOWN_SPECIALS.sub(r"\\\1", text)


class SelfBot(discord.Client):
    """Discord user client that converts media for people who have DMed it first."""

    def __init__(
        self,
        settings: Settings,
        allowlist: AllowlistStore,
        rate_limiter: RateLimiter | None = None,
        captcha_guard: CaptchaGuard | None = None,
        auto_accept_friends: bool = False,
        ui: Any | None = None,
        scout: ScoutRuntime | None = None,
    ) -> None:
        super().__init__()
        self.config = settings
        self.allowlist = allowlist
        self.captcha_guard = captcha_guard or CaptchaGuard(allowlist.path.with_name("captcha-blocked.json"))
        self.rate_limiter = rate_limiter or RateLimiter()
        self.stats = BotStats()
        self.ui = ui  # console_ui.TerminalUi in pretty mode, else None
        self.scout = scout  # None unless the clip scout is configured
        self._scout_task: asyncio.Task[None] | None = None
        self._manual_gate = ManualGate(scout.cfg) if scout is not None else None
        if scout is not None and scout.cfg.owner_id is not None:
            self.allowlist.add(scout.cfg.owner_id)  # the operator never has to DM their own account first
        self._help_user_cooldown = Cooldown(3600.0)  # a stranger gets the help text at most once an hour...
        self._help_chat_cooldown = Cooldown(600.0)  # ...and a chat at most once every 10 minutes
        self._clip_message_ids: set[int] = set()  # clips we posted, so a reply of `stop` to one can be recognised
        self._booted = False
        self.auto_accept_friends = auto_accept_friends
        self.friend_accept_delay = 3.0  # seconds between accepts, so a burst never looks automated
        self.friend_sweep_limit = 10  # pending requests accepted per (re)connect
        self._friend_lock = asyncio.Lock()
        self.http_session: aiohttp.ClientSession | None = None
        self.max_parallel_jobs = max(1, min(2, os.cpu_count() or 1))
        self.executor = ThreadPoolExecutor(max_workers=self.max_parallel_jobs)
        self.semaphore = asyncio.Semaphore(self.max_parallel_jobs)
        self._shutdown = False
        self.logger = logging.getLogger("bombagif.selfbot")

    async def setup_hook(self) -> None:
        """Create the shared HTTP session and make SIGINT/SIGTERM disconnect cleanly."""
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        self._start_scout()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda: asyncio.create_task(self.close()))
            except (NotImplementedError, RuntimeError):
                pass

    def ui_status(self) -> str:
        """The connection state shown on the live panel."""
        if self.captcha_guard.blocked:
            return "paused"
        return "online" if self.is_ready() else "connecting"

    def scout_view(self) -> tuple[str, str, bool] | None:
        """``(mode, judge status, can post)`` for the live panel, or ``None`` when the scout is not running."""
        runtime = self._scouting()
        if runtime is None:
            return None
        if runtime.judge is None:
            return (runtime.cfg.mode, "starting", False)
        return (runtime.cfg.mode, runtime.judge.status, runtime.judge.can_post)

    def _scout_info(self) -> dict[str, str]:
        runtime = self._scouting()
        if runtime is None:
            return {"scout": "off"}
        info = {"scout": runtime.cfg.mode}
        judge = runtime.judge
        info["judge"] = judge.status if judge is not None else "starting"
        info["scout chats"] = str(len(runtime.chats.list()))
        active = judge.active if judge is not None else None
        if active is not None and active.name == "inception":
            info["reminder"] = "Inception: turn OFF 'Improve the model for everyone' in its API settings"
        return info

    def _boot_info(self) -> dict[str, str]:
        """Facts for the boot banner. Never includes tokens."""
        return {**self._boot_basics(), **self._scout_info()}

    def _boot_basics(self) -> dict[str, str]:
        guard = self.captcha_guard
        return {
            "account": str(self.user),
            "zipline": urlparse(self.config.zipline_url).netloc,
            "allowlist": f"{len(self.allowlist)} users ({self.allowlist.path})",
            "auto-accept friends": "on" if self.auto_accept_friends else "off",
            "captcha guard": f"paused ({guard.reason})" if guard.blocked else "armed",
            "rate limit": f"{self.rate_limiter.limit} triggers / {int(self.rate_limiter.window)}s per user",
            "batch": f"up to {MAX_GIF_BATCH} files per message",
            "workers": str(self.max_parallel_jobs),
            "python": platform.python_version(),
            "discord.py-self": discord.__version__,
        }

    async def on_ready(self) -> None:
        self.logger.info("Self-bot connected", extra={"user_id": self.user.id if self.user else None})
        if self.ui is not None and not self._booted:
            self._booted = True  # reconnects fire on_ready again; the banner is printed once
            self.ui.show_boot(self._boot_info())
        if self.auto_accept_friends:
            pending = [r for r in self.relationships if r.type is discord.RelationshipType.incoming_request]
            for relationship in pending[: self.friend_sweep_limit]:
                if self.captcha_guard.blocked:
                    break
                await self._accept_friend(relationship)

    async def on_relationship_add(self, relationship: discord.Relationship) -> None:
        """Accept a new incoming friend request when the operator opted in."""
        if self.auto_accept_friends:
            await self._accept_friend(relationship)

    async def _accept_friend(self, relationship: discord.Relationship) -> None:
        """Accept one incoming request, paced and CAPTCHA-aware. Accepting never registers anyone."""
        if relationship.type is not discord.RelationshipType.incoming_request:
            return
        async with self._friend_lock:
            if self.captcha_guard.blocked:
                return
            try:
                await relationship.accept()
                self.stats.friends_accepted += 1
                self.stats.note("info", f"Accepted a friend request from {self._who(relationship.user)}")
                self.logger.info("Accepted a friend request", extra={"user_id": relationship.user.id})
            except discord.CaptchaRequired:
                self._trip_captcha("accepting a friend request")
                return
            except discord.HTTPException:
                self.logger.exception("Could not accept a friend request", extra={"user_id": relationship.user.id})
            await asyncio.sleep(self.friend_accept_delay)

    def _incoming(self, message: discord.Message) -> Incoming:
        """Summarize a Discord message into the facts the decision rules use."""
        if isinstance(message.channel, discord.DMChannel):
            channel = ChannelKind.DM
        elif isinstance(message.channel, discord.GroupChannel):
            channel = ChannelKind.GROUP
        else:
            channel = ChannelKind.GUILD
        me = self.user
        # Only a direct user mention counts; @everyone/@here and role pings never trigger.
        mentions_me = me is not None and any(user.id == me.id for user in message.mentions)
        return Incoming(
            author_id=message.author.id,
            author_is_bot=bool(message.author.bot),
            is_own=me is not None and message.author.id == me.id,
            is_system=message.is_system(),
            channel=channel,
            mentions_me=mentions_me,
            content=message.content or "",
            has_payload=has_media(message),
            is_reply=message.reference is not None,
            is_clip_reply=message.reference is not None
            and getattr(message.reference, "message_id", None) in self._clip_message_ids,
        )

    @staticmethod
    def _who(user: object) -> str:
        """A short display name for the terminal feed (never used in replies)."""
        name = getattr(user, "display_name", None) or getattr(user, "name", None)
        return str(name or getattr(user, "id", "someone"))

    def _trip_captcha(self, action: str) -> None:
        """Pause all account actions and show the pause in the activity feed."""
        if self.captcha_guard.trip(action):
            self.stats.note("warn", f"CAPTCHA requested while {action}. Paused; see the log to resume.")

    async def _reply(self, message: discord.Message, text: str) -> None:
        """Reply once to a message without pinging anyone."""
        if self.captcha_guard.blocked:
            return
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.CaptchaRequired:
            self._trip_captcha("sending a reply")
        except discord.HTTPException:
            self.logger.exception(
                "Could not send self-bot reply",
                extra={"user_id": message.author.id, "guild_id": message.guild.id if message.guild else None},
            )

    def _start_scout(self) -> None:
        """Build the judge chain and start the scout loop (needs the HTTP session)."""
        runtime = self._scouting()
        if runtime is None or self.http_session is None:
            return
        taste = (
            TasteBook(runtime.taste_notes, runtime.taste_store)
            if runtime.taste_notes is not None and runtime.taste_store is not None
            else None
        )
        runtime.judge = build_judge_chain(runtime.cfg, self.http_session, taste=taste)
        runtime.scout = Scout(
            runtime.cfg,
            runtime.buffer,
            runtime.judge,
            runtime.optout,
            runtime.ledger,
            self._post_clip,
            stats=self.stats,
            paused=lambda: self.captcha_guard.blocked,
            blocklist=runtime.blocklist,
            on_review=self._send_review,
            taste=runtime.taste_store,
        )
        self._clip_message_ids |= runtime.ledger.recent_posted_ids()
        self._scout_task = asyncio.create_task(runtime.scout.run(), name="clip-scout")

    # clip scout: gateway events feed the in-memory buffer ---------------------------------------------------------

    def _scouting(self) -> ScoutRuntime | None:
        runtime = self.scout
        return runtime if runtime is not None and runtime.cfg.mode != "off" else None

    def _buffer_message(self, message: discord.Message) -> None:
        """Remember a message for the scout (enabled guild/group chats only; never 1:1 DMs, NSFW, bots, or opt-outs)."""
        runtime = self._scouting()
        me = self.user
        if runtime is None or message.author.bot or (me is not None and message.author.id == me.id):
            return
        channel = message.channel
        if not chat_is_scoutable(channel) or not runtime.chats.enabled(channel.id):
            return
        runtime.buffer.add(to_scout_message(message, eligible=message_is_clippable(message)))

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        runtime = self._scouting()
        content = payload.data.get("content") if runtime is not None else None
        if isinstance(content, str):
            runtime.buffer.edit(payload.channel_id, payload.message_id, content)  # type: ignore[union-attr]

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        runtime = self._scouting()
        if runtime is not None:
            runtime.buffer.delete(payload.channel_id, payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        runtime = self._scouting()
        if runtime is not None:
            runtime.buffer.delete_many(payload.channel_id, list(payload.message_ids))

    def _count_reaction(self, payload: discord.RawReactionActionEvent, added: bool) -> None:
        runtime = self._scouting()
        me = self.user
        if runtime is None or (me is not None and payload.user_id == me.id):
            return
        runtime.buffer.react(payload.channel_id, payload.message_id, str(payload.emoji), added)

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        self._count_reaction(payload, True)
        try:
            await self._handle_rating(payload)
        except Exception:
            self.logger.exception("Could not handle a rating reaction")

    # clip scout: teaching (review DMs, ratings) ---------------------------------------------------------------

    @staticmethod
    def _labelled_lines(chosen: Sequence[ScoutMessage]) -> list[tuple[str, str]]:
        """(speaker label, text) pairs with anonymous labels A, B, C by first appearance; non-text messages are skipped."""
        labels: dict[int, str] = {}
        lines: list[tuple[str, str]] = []
        for message in chosen:
            if not message.eligible or not message.original:
                continue
            label = labels.setdefault(message.author_id, string.ascii_uppercase[len(labels) % 26])
            lines.append((label, message.original))
        return lines

    def _review_text(self, item: ReviewItem) -> str:
        speakers = {speaker for speaker, _ in item.lines}
        shown = [
            f"> ({speaker}) {_escape_markdown(text)}" if len(speakers) > 1 else f"> {_escape_markdown(text)}"
            for speaker, text in item.lines
        ]
        scores = f"funny {item.funny:.1f}"
        parts = [f"{key} {item.decide[key]:.2f}" for key in ("funny", "standalone", "sensitive") if key in item.decide]
        if parts:
            scores += " · decide " + " / ".join(parts)
        names = ", ".join(_escape_markdown(name) for name in item.names) or "someone"
        text = "\n".join(
            [
                f"🔎 **Review candidate** ({'would clip' if item.would_clip else 'near miss'})",
                f"From: {names}",
                *shown,
                scores,
                f"Judge: {item.reason}" if item.reason else "",
                "React 👍 clip-worthy · 👎 never · 🙈 skip",
            ]
        )
        return "\n".join(line for line in text.split("\n") if line)[:1900]

    async def _send_review(self, item: ReviewItem) -> int | None:
        """DM a candidate to the owner for rating. Returns the DM's message id, or ``None`` if it could not be sent."""
        runtime = self._scouting()
        if runtime is None or runtime.cfg.owner_id is None:
            return None
        try:
            owner = self.get_user(runtime.cfg.owner_id) or await self.fetch_user(runtime.cfg.owner_id)
            sent = await owner.send(self._review_text(item), allowed_mentions=discord.AllowedMentions.none())
        except discord.CaptchaRequired:
            self._trip_captcha("sending a review")
            return None
        except discord.HTTPException:
            self.logger.warning("Could not DM a review candidate to the owner", exc_info=True)
            return None
        for emoji in ("👍", "👎", "🙈"):
            try:
                await sent.add_reaction(emoji)
            except discord.CaptchaRequired:
                self._trip_captcha("adding review reactions")
                break
            except discord.HTTPException:
                self.logger.warning("Could not add the review reactions", exc_info=True)
                break
        return int(sent.id)

    async def _handle_rating(self, payload: discord.RawReactionActionEvent) -> None:
        """The owner reacting 👍/👎/🙈 on a review DM, or 👍/👎 on a clip we posted, teaches the judge."""
        runtime = self.scout
        if runtime is None or runtime.cfg.owner_id is None or payload.user_id != runtime.cfg.owner_id:
            return
        label = {"👍": "good", "👎": "bad", "🙈": "skip"}.get(str(payload.emoji)[:1])
        if label is None:
            return
        scout = runtime.scout
        if scout is not None and payload.message_id in scout.reviews:
            saved = await scout.rate(payload.message_id, label)
            self.stats.note("info", f"you rated a candidate {label}" + (" (saved)" if saved and label != "skip" else ""))
            return
        if label in ("good", "bad") and payload.message_id in self._clip_message_ids and runtime.taste_store is not None:
            record = runtime.ledger.find_posted(payload.message_id)
            if record is None or not record.text:
                return
            speakers = record.speakers or ()
            lines = [(speakers[i] if i < len(speakers) else "A", text) for i, text in enumerate(record.text)]
            if runtime.taste_store.add(label, lines, funny=record.funny, decide=record.decide, source="clip"):
                self.stats.note("info", f"you rated a posted clip {label} (saved)")

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        self._count_reaction(payload, False)

    # clip scout: rendering and posting ----------------------------------------------------------------------------

    async def _fetch_asset(self, url: str) -> bytes | None:
        """Download an avatar or emoji image from an allowed host; any failure just means "no image"."""
        if self.http_session is None or not asset_url_allowed(url):
            return None
        try:
            return await download_url(self.http_session, url)
        except Exception:
            self.logger.debug("asset fetch failed", exc_info=True)
            return None

    async def _fetch_emoji(self, unicode_keys: set[str], custom_ids: set[str]) -> dict[str, bytes]:
        """Fetch emoji images: custom ones from Discord, unicode ones from the pinned Twemoji host (cached on disk)."""
        runtime = self.scout
        cache = runtime.emoji_cache if runtime is not None else None
        found: dict[str, bytes] = {}

        async def unicode(key: str) -> None:
            cached = cache / f"{key}.png" if cache is not None else None
            if cached is not None:
                with contextlib.suppress(OSError):
                    found[key] = cached.read_bytes()
                    return
            data = await self._fetch_asset(twemoji_url(key))
            if data is None:
                return
            found[key] = data
            if cached is not None:
                with contextlib.suppress(OSError):
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    cached.write_bytes(data)

        async def custom(emoji_id: str) -> None:
            data = await self._fetch_asset(custom_emoji_url(emoji_id))
            if data is not None:
                found[f"custom:{emoji_id}"] = data

        await asyncio.gather(
            *(unicode(key) for key in unicode_keys), *(custom(i) for i in custom_ids), return_exceptions=True
        )
        return found

    def _mention_names(self, plan: ClipPlan) -> dict[int, str]:
        names = {m.author_id: m.author_name for m in plan.messages}
        resolved: dict[int, str] = {}
        for user_id in mentioned_ids(m.original for m in plan.messages):
            if user_id in names:
                resolved[user_id] = names[user_id]
                continue
            user = self.get_user(user_id)
            shown = getattr(user, "display_name", None) or getattr(user, "name", None)
            if shown:
                resolved[user_id] = str(shown)
        return resolved

    async def _send_clip(self, plan: ClipPlan, channel: Any, png: bytes, notice: str, runtime: ScoutRuntime) -> Any:
        none = discord.AllowedMentions.none()

        def attachment() -> discord.File:
            return discord.File(io.BytesIO(png), filename="clip.png")

        if plan.to_owner:
            owner_id = runtime.cfg.owner_id
            if owner_id is None:
                self.logger.warning("Deleted clip not sent: SELFBOT_OWNER_ID is not set")
                return None
            owner = self.get_user(owner_id) or await self.fetch_user(owner_id)
            link = channel.get_partial_message(plan.messages[0].id).jump_url
            return await owner.send(content=f"{notice}\n{link}", file=attachment(), allowed_mentions=none)
        live = [m for m in plan.messages if not m.deleted]
        if live:
            try:
                return await channel.get_partial_message(live[-1].id).reply(
                    content=notice, file=attachment(), mention_author=False, allowed_mentions=none
                )
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                if isinstance(exc, discord.CaptchaRequired) or exc.code not in (10008, 50035):
                    raise
        return await channel.send(content=notice, file=attachment(), allowed_mentions=none)

    async def _post_clip(self, plan: ClipPlan) -> int | None:
        """Render a planned clip and post it (or DM it to the owner). Returns the sent message id, or ``None``."""
        runtime = self._scouting()
        if runtime is None or self.http_session is None:
            return None
        try:
            channel = self.get_channel(plan.chat_id) or await self.fetch_channel(plan.chat_id)
            authors = {m.author_id: m.avatar_url for m in plan.messages}
            fetched = await asyncio.gather(*(self._fetch_asset(url) for url in authors.values()), return_exceptions=True)
            avatars = {uid: data for uid, data in zip(authors, fetched) if isinstance(data, bytes)}
            card = build_card(plan.messages, avatars, self._mention_names(plan))
            emoji = await self._fetch_emoji(*collect_asset_keys(card))
            loop = asyncio.get_running_loop()
            png = await loop.run_in_executor(
                self.executor, partial(render_clip, card, scale=2, theme=runtime.cfg.theme, emoji=emoji)
            )
            notice = format_clip_notice([m.author_name for m in plan.messages], plan.deleted, plan.requested_by)
            sent = await self._send_clip(plan, channel, png, notice, runtime)
        except discord.CaptchaRequired:
            self._trip_captcha("posting a clip")
            return None
        except Exception:
            self.logger.exception("Could not post a clip")
            return None
        if sent is None:
            return None
        if not plan.to_owner:
            self._clip_message_ids.add(sent.id)
        return int(sent.id)

    # clip scout: owner commands -----------------------------------------------------------------------------------

    async def _owner_say(self, message: discord.Message, text: str) -> None:
        """Answer the owner privately (a DM), so commands never add noise to a public chat."""
        try:
            await message.author.send(text)
        except discord.CaptchaRequired:
            self._trip_captcha("answering a !scout command")
        except discord.HTTPException:
            self.logger.warning("Could not DM the owner a !scout answer", exc_info=True)

    async def _announce(self, channel: Any, text: str) -> None:
        try:
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
        except discord.CaptchaRequired:
            self._trip_captcha("posting a scout announcement")
        except discord.HTTPException:
            self.logger.exception("Could not post a scout announcement")

    @staticmethod
    def _example_line(lines: Sequence[tuple[str, str]], multi: bool) -> str:
        text = " | ".join(f"{speaker}: {line}" if multi else line for speaker, line in lines)
        return f"• {text[:140]}{'…' if len(text) > 140 else ''}"

    def _teaching_reply(self, command: Any, runtime: ScoutRuntime) -> str:
        """The owner's answer for ``!scout taste|examples|tune|help`` (and their ``add``/``clear`` forms)."""
        action = command.action
        if action == "help":
            return HELP_OWNER
        notes, store = runtime.taste_notes, runtime.taste_store
        if notes is None or store is None:
            return "Teaching isn't set up on this account."
        if action == "taste":
            return f"{notes.show()}\n({len(notes.text)}/{NOTES_MAX_CHARS} characters)"
        if action == "taste_add":
            if notes.add(command.arg or ""):
                return f"Added. ({len(notes.text)}/{NOTES_MAX_CHARS} characters)"
            if not notes.clean(command.arg or ""):
                return "Nothing to add."
            return (
                f"That would go over the {NOTES_MAX_CHARS}-character limit ({len(notes.text)} used). "
                "Shorten it, or `!scout taste clear` to start over."
            )
        if action == "taste_clear":
            notes.clear()
            return "Cleared the taste notes."
        if action == "examples_clear":
            store.clear()
            return "Cleared all rated examples."
        if action == "tune":
            return tune_report(store.all(), runtime.cfg.judge_funny_min)
        counts = store.counts()  # examples
        if not any(counts.values()):
            return "No rated examples yet. React 👍/👎 on review DMs or posted clips to teach me."
        found = store.examples()
        parts = [f"Rated examples: good {counts['good']}, bad {counts['bad']} (the judge sees the newest {store.per_label} of each)."]
        for label, title in (("good", "👍 newest"), ("bad", "👎 newest")):
            if found[label]:
                parts.append(f"{title}:")
                parts.extend(self._example_line(e.lines, len({s for s, _ in e.lines}) > 1) for e in found[label][:3])
        return "\n".join(parts)[:1900]

    async def _handle_help(self, message: discord.Message) -> bool:
        """``!help``: the owner gets the full guide by DM; anyone else only in a 1:1 DM, with the short one."""
        if not parse_help_command(message.content or ""):
            return False
        me = self.user
        if message.author.bot or (me is not None and message.author.id == me.id):
            return False
        runtime = self.scout
        is_owner = runtime is not None and runtime.cfg.owner_id == message.author.id
        in_dm = isinstance(message.channel, discord.DMChannel)
        if not (is_owner or in_dm):
            return False
        if self.captcha_guard.blocked:
            return True
        if is_owner:
            await self._owner_say(message, HELP_OWNER)
        else:
            await self._reply(message, HELP_USER)
        return True

    async def _handle_scout_command(self, message: discord.Message) -> bool:
        """Run a ``!scout`` command if the owner sent one. Returns True when the message was a command (consumed)."""
        runtime = self.scout
        command = parse_scout_command(message.content or "")
        if runtime is None or command is None or runtime.cfg.owner_id is None or message.author.id != runtime.cfg.owner_id:
            return False
        if self.captcha_guard.blocked:
            return True
        cfg = runtime.cfg
        if command.action in ("taste", "taste_add", "taste_clear", "examples", "examples_clear", "tune", "help"):
            await self._owner_say(message, self._teaching_reply(command, runtime))
            return True
        if cfg.mode == "off":
            await self._owner_say(message, "Scouting is off. Set SELFBOT_SCOUT_MODE=dry-run or on and restart me.")
            return True
        in_dm = isinstance(message.channel, discord.DMChannel)
        if command.action in ("status", "list"):
            await self._owner_say(message, self._scout_report(command.action, message.channel.id, runtime))
            return True

        # on / off: this chat, or (from the owner's DM only) a chat id
        if command.chat_id is not None:
            if not in_dm:
                await self._owner_say(message, "Use the chat id form in a DM with me, or run the command inside the chat.")
                return True
            channel = self.get_channel(command.chat_id)
            if channel is None:
                await self._owner_say(message, "I can't see that chat.")
                return True
        elif in_dm:
            await self._owner_say(message, "Run this inside the chat you want, or send `!scout on <chat id>` here.")
            return True
        else:
            channel = message.channel
        chat_id = int(channel.id)
        if command.action == "on":
            if isinstance(channel, discord.DMChannel):
                await self._owner_say(message, "I never scout 1:1 DMs.")
            elif not chat_is_scoutable(channel):
                await self._owner_say(message, "I never scout NSFW channels.")
            elif not runtime.chats.enable(chat_id, str(getattr(channel, "name", None) or chat_id)):
                await self._owner_say(message, "Scouting is already on in that chat.")
            elif cfg.mode == "dry-run":
                await self._owner_say(message, "Scouting is on in that chat (dry-run: nothing will be posted, so no announcement).")
            else:
                await self._announce(channel, ANNOUNCE_ON)
            return True
        # off
        if runtime.chats.is_seeded(chat_id):
            await self._owner_say(message, "That chat is enabled by SELFBOT_SCOUT_CHATS; remove it from there and restart.")
        elif not runtime.chats.disable(chat_id):
            await self._owner_say(message, "Scouting wasn't on in that chat.")
        else:
            runtime.buffer.drop_chat(chat_id)
            if cfg.mode == "dry-run":
                await self._owner_say(message, "Scouting is off in that chat.")
            else:
                await self._announce(channel, ANNOUNCE_OFF)
        return True

    def _scout_report(self, action: str, chat_id: int, runtime: ScoutRuntime) -> str:
        chats = runtime.chats.list()
        if action == "list":
            if not chats:
                return "No chats are enabled for scouting."
            return "Scouting is on in:\n" + "\n".join(f"• {name or '(unnamed)'} ({cid})" for cid, name in chats)
        judge = runtime.judge.status if runtime.judge is not None else "not started"
        here = "on" if runtime.chats.enabled(chat_id) else "off"
        return (
            f"Scout mode: {runtime.cfg.mode}. Judge: {judge}. Chats enabled: {len(chats)}. This chat: {here}. "
            f"Clips posted: {self.stats.clips_posted}, dry-run hits: {self.stats.dry_run_hits}."
        )

    async def on_message(self, message: discord.Message) -> None:
        """Apply the consent and trigger rules to every message the account can see."""
        try:
            if await self._handle_help(message) or await self._handle_scout_command(message):
                return
        except Exception:
            self.logger.exception("Unhandled !scout command error")
            return
        try:
            self._buffer_message(message)
        except Exception:
            self.logger.exception("Could not buffer a message for the scout", extra={"user_id": message.author.id})
        if self.captcha_guard.blocked:
            return
        try:
            await self._handle_message(message)
        except discord.CaptchaRequired:
            self._trip_captcha("handling a message")
        except Exception:
            self.logger.exception("Unhandled self-bot message error", extra={"user_id": message.author.id})

    async def _handle_message(self, message: discord.Message) -> None:
        decision = decide(self._incoming(message), self.allowlist)
        author = message.author.id
        runtime = self._scouting()
        clipping_live = runtime is not None and runtime.cfg.mode == "on"
        if decision.kind is Kind.REGISTER:
            self.allowlist.add(author)
            self.stats.registered += 1
            self.stats.note("info", f"{self._who(message.author)} said hi: registered")
            opted_out = clipping_live and author in runtime.optout  # type: ignore[union-attr]
            await self._reply(message, welcome_text(clipping_live, opted_out=opted_out))
            if decision.convert:
                await self._convert_and_reply(message)
        elif decision.kind is Kind.UNREGISTER:
            self.allowlist.remove(author)
            if runtime is not None:
                runtime.optout.add(author)
            self.stats.unregistered += 1
            self.stats.note("info", f"{self._who(message.author)} opted out")
            await self._reply(message, STOP_CONFIRMATION)
        elif decision.kind is Kind.START:
            if runtime is not None:
                runtime.optout.remove(author)
            self.allowlist.add(author)
            self.stats.note("info", f"{self._who(message.author)} opted back in")
            await self._reply(message, START_CONFIRMATION)
        elif decision.kind is Kind.OPT_OUT_CLIPS:
            if runtime is not None:
                runtime.optout.add(author)
            self.stats.note("info", f"{self._who(message.author)} opted out of clips")
            await self._reply(message, CLIP_OPT_OUT_CONFIRMATION)
        elif decision.kind is Kind.CONVERT:
            await self._convert_and_reply(message)
        elif decision.kind is Kind.HELP:
            chat_id = message.channel.id
            opted_out = runtime is not None and author in runtime.optout
            if not opted_out and self._help_user_cooldown.is_ready(author) and self._help_chat_cooldown.is_ready(chat_id):
                self._help_user_cooldown.mark(author)
                self._help_chat_cooldown.mark(chat_id)
                self.stats.note("info", f"{self._who(message.author)} pinged without DMing first: sent help")
                await self._reply(message, help_text(clipping_live))

    async def _resolve_reference(self, message: discord.Message) -> discord.Message | None:
        """Return the message this one replies to, or None if deleted or unreachable."""
        reference = message.reference
        if reference is None:
            return None
        for candidate in (reference.resolved, reference.cached_message):
            if candidate is not None and hasattr(candidate, "attachments"):
                return candidate
        if reference.resolved is not None or reference.message_id is None:
            return None  # Discord reported the original as deleted.
        if getattr(reference, "channel_id", None) not in (None, message.channel.id):
            return None
        try:
            return await message.channel.fetch_message(reference.message_id)
        except discord.CaptchaRequired:
            self._trip_captcha("fetching a message")
            return None
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def _convert_one(
        self, source: MediaSource, user_id: int, guild_id: int | None
    ) -> tuple[str, str, bool]:
        """Convert one item, returning (filename, link-or-error, ok) and never raising."""
        trace_id = uuid.uuid4().hex
        context = {"user_id": user_id, "guild_id": guild_id, "trace_id": trace_id}
        if self.http_session is None:
            return source.filename, "I'm still starting up. Please try again shortly.", False
        try:
            async with self.semaphore:
                link = await convert_and_upload(
                    self.http_session,
                    self.config,
                    self.executor,
                    source,
                    trace_id=trace_id,
                    log_context={"user_id": user_id, "guild_id": guild_id},
                )
            return source.filename, link, True
        except UserFacingError as exc:
            return source.filename, str(exc), False
        except Exception:
            self.logger.exception("Unexpected self-bot processing error", extra=context)
            return source.filename, "Could not process this file.", False

    async def _convert_and_reply(self, message: discord.Message) -> None:
        """Convert the message's media and answer with a single reply."""
        author_id = message.author.id
        if not self.rate_limiter.allow(author_id):
            self.stats.rate_limited += 1
            self.stats.note("warn", f"{self._who(message.author)} rate limited")
            self.logger.info("Rate limited trigger ignored", extra={"user_id": author_id})
            return
        self.stats.triggers += 1
        guild_id = message.guild.id if message.guild is not None else None
        async with message.channel.typing():
            referenced = None
            collected = collect_media(message)
            if not collected.sources and message.reference is not None:
                referenced = await self._resolve_reference(message)
                collected = collect_media(message, referenced)
            if not collected.sources:
                if referenced is not None and await self._try_manual_clip(message, referenced):
                    return
                # Only complain when the person clearly asked: they pinged us or attached something.
                # A plain DM reply (e.g. "thanks!" to one of our messages) is just conversation.
                if message.attachments or self._incoming(message).mentions_me:
                    await self._reply(message, NO_MEDIA_TEXT)
                return
            who = self._who(message.author)
            results = []
            for source in collected.sources:
                started = time.monotonic()
                result = await self._convert_one(source, author_id, guild_id)
                results.append(result)
                seconds = time.monotonic() - started
                if result[2]:
                    self.stats.conversions_ok += 1
                    self.stats.note("ok", f"{who}: {source.filename} converted ({seconds:.1f}s)")
                else:
                    self.stats.conversions_failed += 1
                    self.stats.note("fail", f"{who}: {source.filename}: {result[1]}")
            await self._reply(message, format_results(results, collected.skipped))

    def _teach(self, label: str, chosen: Sequence[ScoutMessage], source: str) -> None:
        """Save a ruling as a taste example (anonymised; no scores for rulings that did not come from the judge)."""
        runtime = self.scout
        store = runtime.taste_store if runtime is not None else None
        if store is None:
            return
        lines = self._labelled_lines(chosen)
        if lines and store.add(label, lines, source=source):
            self.stats.note("info", f"taught: saved a {label} example ({source})")

    def _manual_chosen(self, referenced: discord.Message, runtime: ScoutRuntime) -> list[ScoutMessage]:
        """The messages a "clip that" request covers: the replied-to message's buffered run, else just that message."""
        chat_id = referenced.channel.id
        buffered = runtime.buffer.get(chat_id, referenced.id)
        if buffered is not None:
            for run in runtime.buffer.runs(chat_id):
                if any(m.id == referenced.id for m in run):
                    return list(run)
            return [buffered]
        return [to_scout_message(referenced, eligible=message_is_clippable(referenced))]

    async def _try_manual_clip(self, message: discord.Message, referenced: discord.Message) -> bool:
        """Handle "@account" as a reply to a text message: clip it. Returns True if the request was handled."""
        runtime, gate = self.scout, self._manual_gate
        if runtime is None or gate is None or runtime.cfg.mode == "off" or not runtime.cfg.manual:
            return False
        channel = message.channel
        if not chat_is_scoutable(channel):
            return False
        requester = message.author.id
        is_owner = runtime.cfg.owner_id == requester
        chosen = self._manual_chosen(referenced, runtime)
        me = self.user
        reason = gate.check(
            chosen,
            requester_id=requester,
            is_owner=is_owner,
            chat_enabled=runtime.chats.enabled(channel.id),
            optout=runtime.optout,
            blocklist=runtime.blocklist,
            self_id=me.id if me is not None else None,
            target_is_bot=bool(referenced.author.bot),
        )
        if reason is not None:
            await self._reply(message, reason)
            return True
        who = self._who(message.author)
        deleted = any(m.deleted for m in chosen)
        plan = ClipPlan(
            chat_id=channel.id,
            messages=tuple(chosen),
            verdict=Verdict(True, 0, len(chosen) - 1, 0.0, True, (), "manual", {}, "manual"),
            deleted=deleted,
            to_owner=False,
            requested_by=who,
        )
        sent_id = await self._post_clip(plan)
        if sent_id is None:
            return True  # _post_clip already logged why; the requester is not told twice
        gate.commit(chosen, requester, is_owner)
        self.stats.clips_posted += 1
        names = ", ".join(dict.fromkeys(m.author_name for m in chosen))
        self.stats.note("ok", f"manual clip: {who} clipped {names}")
        try:
            runtime.ledger.append(
                ClipRecord(
                    at=time.time(), chat_id=channel.id, message_ids=tuple(m.id for m in chosen),
                    author_ids=tuple(dict.fromkeys(m.author_id for m in chosen)), mode="posted", funny=0.0, decide={},
                    deleted=deleted, provider="manual", reason=f"requested by {requester}", posted_message_id=sent_id,
                    text=tuple(m.original for m in chosen),
                )
            )
        except OSError:
            self.logger.exception("could not write the clip ledger")
        if is_owner:
            self._teach("good", chosen, "manual")
        return True

    async def close(self) -> None:
        """Close HTTP resources and drain image work before disconnecting."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            if self._scout_task is not None:
                self._scout_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._scout_task
            if self.http_session is not None and not self.http_session.closed:
                await self.http_session.close()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self.executor.shutdown(wait=True, cancel_futures=True))
        finally:
            if self.ui is not None:
                self.ui.stop(self.stats)
            await super().close()


def create_bot(settings: Settings, data_dir: Path, environ: Mapping[str, str]) -> SelfBot:
    """Build the self-bot (allowlist, CAPTCHA guard, optional auto-accept, optional scout) from the environment."""
    return SelfBot(
        settings,
        AllowlistStore(data_dir / "allowlist.json"),
        captcha_guard=CaptchaGuard(data_dir / "captcha-blocked.json"),
        auto_accept_friends=env_flag(environ.get("SELFBOT_AUTO_ACCEPT_FRIENDS")),
        scout=build_scout_runtime(read_scout_config(environ), data_dir),
    )


def main() -> None:
    """Load configuration and run the self-bot until interrupted."""
    load_dotenv()
    settings = read_settings("SELFBOT_TOKEN")
    mode = resolve_ui_mode(os.getenv("SELFBOT_UI"), is_tty=sys.stdout.isatty())
    bot = create_bot(settings, Path(os.getenv("SELFBOT_DATA_DIR", "data")), os.environ)
    if mode == "pretty":
        import console_ui  # rich is only needed for the pretty terminal

        console = console_ui.create_console()
        console_ui.configure_pretty_logging(console, settings.log_level)
        dashboard = console_ui.Dashboard(bot.stats, bot.ui_status, lambda: len(bot.allowlist), get_scout=bot.scout_view)
        bot.ui = console_ui.TerminalUi(console, dashboard)
    else:
        configure_logging(settings.log_level)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
