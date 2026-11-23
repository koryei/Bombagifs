"""Bombagif self-bot: a user-account front door that answers pings with GIF links.

Runs on ``discord.py-self`` from its own virtualenv (see requirements-selfbot.txt); it shares the
conversion pipeline with the slash-command app through ``bombagif_core``.

WARNING: automating a user account violates Discord's Terms of Service. Use an alt account.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import datetime
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
from clip_render import collect_asset_keys, host_zone, render_clip
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
    parse_rating,
    parse_scout_command,
)
from scout_config import read_scout_config
from scout_intent import IntentClassifier, build_intent_classifier
from scout_judge import Verdict, build_judge_chain
from scout_manual import ManualGate
from scout_stores import ClipRecord
from scout_taste import NOTES_MAX_CHARS, TasteBook, tune_report
from scout_discord import chat_is_scoutable, message_is_clippable, to_scout_message
from scout_trigger import Trigger, parse_trigger
from selfbot_logic import (
    apply_timezone,
    CLIP_OPT_OUT_CONFIRMATION,
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
    env_flag_off,
    format_results,
    has_media,
    help_text,
    no_media_reply,
    resolve_ui_mode,
    update_notice_text,
    welcome_text,
)


_MARKDOWN_SPECIALS = re.compile(r"([\\*_~|>`])")
UPDATE_NOTICE_NAME = "update-notice.json"  # written by selfbot-ctl.sh update, read once on boot


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
        announce_updates: bool = True,
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
        self.intent: IntentClassifier | None = None  # confirms typo'd "clip that" requests (set when the scout starts)
        if scout is not None and scout.cfg.owner_id is not None:
            self.allowlist.add(scout.cfg.owner_id)  # the operator never has to DM their own account first
        self._help_user_cooldown = Cooldown(3600.0)  # a stranger gets the help text at most once an hour...
        self._help_chat_cooldown = Cooldown(600.0)  # ...and a chat at most once every 10 minutes
        self._clip_message_ids: set[int] = set()  # clips we posted, so a reply of `stop` to one can be recognised
        self._booted = False
        self._update_announced = False  # the update notice is delivered once per process, not per reconnect
        self.announce_updates = announce_updates  # SELFBOT_UPDATE_NOTICES=false keeps the bot quiet about updates
        self.update_notice_delay = 1.0  # seconds between update DMs, so a burst never looks automated
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
        info["manual clips"] = "on" if runtime.cfg.manual else "off"
        info["review"] = runtime.cfg.review
        notes = runtime.taste_notes.text if runtime.taste_notes is not None else ""
        counts = runtime.taste_store.counts() if runtime.taste_store is not None else {"good": 0, "bad": 0}
        info["taste"] = f"{len(notes)} chars of notes, {counts['good']} good / {counts['bad']} bad examples"
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
        await self._announce_update()
        if self.auto_accept_friends:
            pending = [r for r in self.relationships if r.type is discord.RelationshipType.incoming_request]
            for relationship in pending[: self.friend_sweep_limit]:
                if self.captcha_guard.blocked:
                    break
                await self._accept_friend(relationship)

    async def _announce_update(self) -> None:
        """DM everyone on the allowlist what an unattended update changed, then consume the notice."""
        if self._update_announced:
            return
        self._update_announced = True
        path = self.allowlist.path.with_name(UPDATE_NOTICE_NAME)
        if not self.announce_updates:
            path.unlink(missing_ok=True)  # opted out: drop the notice instead of retrying it forever
            return
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError:
            self.logger.warning("Could not read the update notice at %s", path, exc_info=True)
            return
        try:
            notice = json.loads(raw)
            if not isinstance(notice, dict):
                raise ValueError("the update notice must be a JSON object")
            text = update_notice_text(notice)
        except (ValueError, TypeError):
            self.logger.warning("Ignoring a malformed update notice at %s", path)
            path.unlink(missing_ok=True)
            return
        recipients = self.allowlist.ids()
        delivered = 0
        for user_id in recipients:
            if self.captcha_guard.blocked:
                break
            try:
                user = self.get_user(user_id) or await self.fetch_user(user_id)
                await user.send(text, allowed_mentions=discord.AllowedMentions.none())
                delivered += 1
            except discord.CaptchaRequired:
                self._trip_captcha("announcing an update")
                break
            except discord.HTTPException:
                self.logger.warning("Could not DM the update notice", extra={"user_id": user_id}, exc_info=True)
            await asyncio.sleep(self.update_notice_delay)
        if delivered or not recipients:
            path.unlink(missing_ok=True)  # an undelivered notice stays for the next boot
        self.logger.info(
            "Announced update %s to %d of %d people", notice.get("to"), delivered, len(recipients)
        )

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
        self.intent = build_intent_classifier(runtime.cfg, self.http_session)
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
            review_store=runtime.review_store,
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
                "Reply to this message with `good` (clip-worthy) · `bad` (never) · `skip`",
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
        return int(sent.id)  # no reactions are added: they are rate limited, so you rate by replying

    async def _handle_rating(self, payload: discord.RawReactionActionEvent) -> None:
        """The owner reacting 👍/👎/🙈 on a review DM or 👍/👎 on a clip we posted also teaches (reacting is free for them)."""
        runtime = self.scout
        if runtime is None or runtime.cfg.owner_id is None or payload.user_id != runtime.cfg.owner_id:
            return
        label = {"👍": "good", "👎": "bad", "🙈": "skip"}.get(str(payload.emoji)[:1])
        if label is not None:
            await self._apply_rating(runtime, payload.message_id, label)

    async def _apply_rating(self, runtime: ScoutRuntime, message_id: int, label: str) -> bool:
        """Rate a review DM or a posted clip by its message id. True when ``message_id`` was one of ours."""
        scout = runtime.scout
        if scout is not None and message_id in scout.reviews:
            saved = await scout.rate(message_id, label)
            self.stats.note("info", f"you rated a candidate {label}" + (" (saved)" if saved and label != "skip" else ""))
            return True
        if message_id in self._clip_message_ids and runtime.taste_store is not None:
            if label in ("good", "bad"):
                record = runtime.ledger.find_posted(message_id)
                if record is not None and record.text:
                    speakers = record.speakers or ()
                    lines = [(speakers[i] if i < len(speakers) else "A", text) for i, text in enumerate(record.text)]
                    if runtime.taste_store.add(label, lines, funny=record.funny, decide=record.decide, source="clip"):
                        self.stats.note("info", f"you rated a posted clip {label} (saved)")
            return True
        return False

    async def _handle_rating_reply(self, message: discord.Message) -> bool:
        """The owner replying ``good``/``bad``/``skip`` to a review DM or a posted clip. True when the reply was consumed."""
        runtime = self.scout
        if runtime is None or runtime.cfg.owner_id is None or message.author.id != runtime.cfg.owner_id:
            return False
        target = getattr(getattr(message, "reference", None), "message_id", None)
        label = parse_rating(message.content or "")
        if target is None or label is None:
            return False
        if await self._apply_rating(runtime, int(target), label):
            return True
        if isinstance(message.channel, discord.DMChannel):
            # In a 1:1 DM the only messages the owner can reply to are ours, so a rating word is a rating even
            # when the candidate is gone (evicted, or sent before a restart). Never let it reach the converter.
            self.stats.note("info", f"you rated a candidate {label} (no longer pending)")
            return True
        return False

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

    @staticmethod
    def _jump_url(channel: Any, message_id: int) -> str:
        """A link to a message. Group DMs have no partial messages, so the link is built by hand."""
        getter = getattr(channel, "get_partial_message", None)
        if getter is not None:
            return str(getter(message_id).jump_url)
        private = isinstance(channel, (discord.GroupChannel, discord.DMChannel))
        guild = None if private else getattr(channel, "guild", None)
        return f"https://discord.com/channels/{guild.id if guild is not None else '@me'}/{channel.id}/{message_id}"

    @staticmethod
    async def _reply_in(channel: Any, message_id: int, **kwargs: Any) -> Any:
        """Reply to a message in any channel. Group DMs (which have no ``get_partial_message``) use a message reference."""
        getter = getattr(channel, "get_partial_message", None)
        if getter is not None:
            return await getter(message_id).reply(**kwargs)
        reference = discord.MessageReference(message_id=message_id, channel_id=channel.id)
        return await channel.send(reference=reference, **kwargs)

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
            link = self._jump_url(channel, plan.messages[0].id)
            return await owner.send(content=f"{notice}\n{link}", file=attachment(), allowed_mentions=none)
        live = [m for m in plan.messages if not m.deleted]
        if live:
            try:
                return await self._reply_in(
                    channel, live[-1].id, content=notice, file=attachment(), mention_author=False, allowed_mentions=none
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
            self._trip_captcha("answering a owner command")
        except discord.HTTPException:
            self.logger.warning("Could not DM the owner an owner-command answer", exc_info=True)

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
        """The owner's answer for ``!taste|examples|tune|help`` (and their ``add``/``clear`` forms)."""
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
                "Shorten it, or `!taste clear` to start over."
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
            return "No rated examples yet. Reply `good` or `bad` to a review DM or a posted clip to teach me."
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
        """Run an owner command (``!on``, ``!taste`` ...) if the owner sent one. Returns True when the message was a command (consumed)."""
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
            await self._owner_say(message, "Run this inside the chat you want, or send `!on <chat id>` here.")
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

    # clip scout: spoken triggers ("clip that", "gif that"), no ping needed -------------------------------------------------

    async def _handle_trigger(self, message: discord.Message) -> bool:
        """Hear "clip that" / "clip last 3" / "gif that" (and typos) in a chat the owner turned scouting on in.

        Known phrases are matched locally; typo'd or oddly worded ones are confirmed by the AI check. Returns True when
        the message was a request (it is then not treated as chat), False for everything else.
        """
        runtime = self._scouting()
        if runtime is None or self._manual_gate is None or not runtime.cfg.manual or runtime.cfg.triggers == "off":
            return False
        me = self.user
        author = message.author
        if author.bot or (me is not None and author.id == me.id) or message.is_system():
            return False
        channel = message.channel
        if not chat_is_scoutable(channel) or not runtime.chats.enabled(channel.id) or self._incoming(message).mentions_me:
            return False  # a ping takes the ping path
        member = runtime.cfg.owner_id == author.id or author.id in self.allowlist
        if not (member or runtime.cfg.trigger_anyone):
            return False
        is_reply = getattr(message.reference, "message_id", None) is not None
        text = message.content or ""
        trigger = parse_trigger(text, is_reply=is_reply)
        if trigger is None or (trigger.kind == "gif" and not member):
            return False
        if not trigger.sure:
            if runtime.cfg.triggers != "ai" or self.intent is None:
                return False
            intent = await self.intent.classify(text, is_reply=is_reply, guess=trigger)
            if intent is None or intent.kind == "none" or (intent.kind == "gif" and not member):
                return False
            trigger = Trigger(intent.kind)
        if self.captcha_guard.blocked:
            return True
        if not self.rate_limiter.allow(author.id):
            self.stats.rate_limited += 1
            return True
        if trigger.kind == "gif":
            if is_reply:
                await self._convert_and_reply(message, gif_only=True)
            return True
        await self._clip_on_request(message, trigger, runtime, is_reply)
        return True

    async def _clip_on_request(self, message: discord.Message, trigger: Trigger, runtime: ScoutRuntime, is_reply: bool) -> None:
        """Pick what a spoken "clip that" points at (the replied-to message, else the latest chat) and clip it silently."""
        channel = message.channel
        if is_reply:
            referenced = await self._resolve_reference(message)
            if referenced is None:
                return
            await self._manual_clip(
                message, self._manual_chosen(referenced, runtime), target_is_bot=bool(referenced.author.bot), silent=True
            )
            return
        recent = runtime.buffer.recent(channel.id, message.id, 1)
        if not recent:
            return
        age = message.created_at.timestamp() - recent[-1].created_at
        if age > runtime.cfg.trigger_max_age_s:
            self.stats.note("info", f"{self._who(message.author)}: clip request ignored (nothing recent to clip)")
            return
        newest = recent[-1]
        chosen = next((run for run in runtime.buffer.runs(channel.id) if any(m.id == newest.id for m in run)), [newest])
        await self._manual_clip(message, list(chosen), target_is_bot=False, silent=True)

    async def on_message(self, message: discord.Message) -> None:
        """Apply the consent and trigger rules to every message the account can see."""
        try:
            if (
                await self._handle_rating_reply(message)
                or await self._handle_help(message)
                or await self._handle_scout_command(message)
            ):
                return
        except Exception:
            self.logger.exception("Unhandled owner command error")
            return
        try:
            if await self._handle_trigger(message):
                return
        except discord.CaptchaRequired:
            self._trip_captcha("handling a clip request")
            return
        except Exception:
            self.logger.exception("Unhandled clip trigger error", extra={"user_id": message.author.id})
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

    async def _convert_and_reply(self, message: discord.Message, *, gif_only: bool = False) -> None:
        """Convert the message's media and answer with a single reply. ``gif_only`` (a spoken "gif that") never falls back to a clip or a complaint."""
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
                if gif_only:
                    return
                if referenced is not None and await self._try_manual_clip(message, referenced):
                    return
                # Only complain when the person clearly asked: they attached something, or pinged us outside a
                # 1:1 DM. A DM reply carries an implicit mention of the message it answers, so it is conversation.
                if message.attachments or (
                    self._incoming(message).mentions_me and not isinstance(message.channel, discord.DMChannel)
                ):
                    await self._reply(message, no_media_reply(message))
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
        runtime = self.scout
        if runtime is None or runtime.cfg.mode == "off" or not runtime.cfg.manual:
            return False
        return await self._manual_clip(
            message, self._manual_chosen(referenced, runtime), target_is_bot=bool(referenced.author.bot), silent=False
        )

    async def _manual_clip(
        self, message: discord.Message, chosen: list[ScoutMessage], *, target_is_bot: bool, silent: bool
    ) -> bool:
        """Clip ``chosen`` because ``message`` asked. ``silent`` requests (spoken triggers) are never answered with a refusal."""
        runtime, gate = self.scout, self._manual_gate
        if runtime is None or gate is None or runtime.cfg.mode == "off" or not runtime.cfg.manual:
            return False
        channel = message.channel
        if not chat_is_scoutable(channel):
            return False
        requester = message.author.id
        is_owner = runtime.cfg.owner_id == requester
        me = self.user
        reason = gate.check(
            chosen,
            requester_id=requester,
            is_owner=is_owner,
            chat_enabled=runtime.chats.enabled(channel.id),
            optout=runtime.optout,
            blocklist=runtime.blocklist,
            self_id=me.id if me is not None else None,
            target_is_bot=target_is_bot,
        )
        if reason is not None:
            if silent:
                self.stats.note("info", f"{self._who(message.author)}: clip request ignored ({reason.rstrip('.')})")
            else:
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
        announce_updates=not env_flag_off(environ.get("SELFBOT_UPDATE_NOTICES")),
    )


def main() -> None:
    """Load configuration and run the self-bot until interrupted."""
    load_dotenv()
    apply_timezone(os.environ)
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
    zone = host_zone()
    now = datetime.datetime.now(zone)
    logging.getLogger("bombagif").info("Clip cards use timezone %s (now %s)", now.tzname(), now.strftime("%H:%M UTC%z"))
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
