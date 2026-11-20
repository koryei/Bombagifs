"""Bombagif self-bot: a user-account front door that answers pings with GIF links.

Runs on ``discord.py-self`` from its own virtualenv (see requirements-selfbot.txt); it shares the
conversion pipeline with the slash-command app through ``bombagif_core``.

WARNING: automating a user account violates Discord's Terms of Service. Use an alt account.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import signal
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
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
    read_settings,
)
from selfbot_logic import (
    NO_MEDIA_TEXT,
    STOP_CONFIRMATION,
    WELCOME_TEXT,
    AllowlistStore,
    BotStats,
    CaptchaGuard,
    ChannelKind,
    Incoming,
    Kind,
    RateLimiter,
    collect_media,
    decide,
    env_flag,
    format_results,
    has_media,
    resolve_ui_mode,
)


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
    ) -> None:
        super().__init__()
        self.config = settings
        self.allowlist = allowlist
        self.captcha_guard = captcha_guard or CaptchaGuard(allowlist.path.with_name("captcha-blocked.json"))
        self.rate_limiter = rate_limiter or RateLimiter()
        self.stats = BotStats()
        self.ui = ui  # console_ui.TerminalUi in pretty mode, else None
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

    def _boot_info(self) -> dict[str, str]:
        """Facts for the boot banner. Never includes tokens."""
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

    async def on_message(self, message: discord.Message) -> None:
        """Apply the consent and trigger rules to every message the account can see."""
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
        if decision.kind is Kind.REGISTER:
            self.allowlist.add(message.author.id)
            self.stats.registered += 1
            self.stats.note("info", f"{self._who(message.author)} said hi: registered")
            await self._reply(message, WELCOME_TEXT)
            if decision.convert:
                await self._convert_and_reply(message)
        elif decision.kind is Kind.UNREGISTER:
            self.allowlist.remove(message.author.id)
            self.stats.unregistered += 1
            self.stats.note("info", f"{self._who(message.author)} opted out")
            await self._reply(message, STOP_CONFIRMATION)
        elif decision.kind is Kind.CONVERT:
            await self._convert_and_reply(message)

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
            collected = collect_media(message)
            if not collected.sources and message.reference is not None:
                collected = collect_media(message, await self._resolve_reference(message))
            if not collected.sources:
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

    async def close(self) -> None:
        """Close HTTP resources and drain image work before disconnecting."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            if self.http_session is not None and not self.http_session.closed:
                await self.http_session.close()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: self.executor.shutdown(wait=True, cancel_futures=True))
        finally:
            if self.ui is not None:
                self.ui.stop(self.stats)
            await super().close()


def main() -> None:
    """Load configuration and run the self-bot until interrupted."""
    load_dotenv()
    settings = read_settings("SELFBOT_TOKEN")
    mode = resolve_ui_mode(os.getenv("SELFBOT_UI"), is_tty=sys.stdout.isatty())
    data_dir = Path(os.getenv("SELFBOT_DATA_DIR", "data"))
    bot = SelfBot(
        settings,
        AllowlistStore(data_dir / "allowlist.json"),
        captcha_guard=CaptchaGuard(data_dir / "captcha-blocked.json"),
        auto_accept_friends=env_flag(os.getenv("SELFBOT_AUTO_ACCEPT_FRIENDS")),
    )
    if mode == "pretty":
        import console_ui  # rich is only needed for the pretty terminal

        console = console_ui.create_console()
        console_ui.configure_pretty_logging(console, settings.log_level)
        dashboard = console_ui.Dashboard(bot.stats, bot.ui_status, lambda: len(bot.allowlist))
        bot.ui = console_ui.TerminalUi(console, dashboard)
    else:
        configure_logging(settings.log_level)
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
