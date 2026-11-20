"""Bombagif self-bot: a user-account front door that answers pings with GIF links.

Runs on ``discord.py-self`` from its own virtualenv (see requirements-selfbot.txt); it shares the
conversion pipeline with the slash-command app through ``bombagif_core``.

WARNING: automating a user account violates Discord's Terms of Service. Use an alt account.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

from bombagif_core import (
    DOWNLOAD_TIMEOUT,
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
    ChannelKind,
    Incoming,
    Kind,
    RateLimiter,
    collect_media,
    decide,
    format_results,
    has_media,
)


class SelfBot(discord.Client):
    """Discord user client that converts media for people who have DMed it first."""

    def __init__(
        self,
        settings: Settings,
        allowlist: AllowlistStore,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        super().__init__()
        self.config = settings
        self.allowlist = allowlist
        self.rate_limiter = rate_limiter or RateLimiter()
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

    async def on_ready(self) -> None:
        self.logger.info("Self-bot connected", extra={"user_id": self.user.id if self.user else None})

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

    async def _reply(self, message: discord.Message, text: str) -> None:
        """Reply once to a message without pinging anyone."""
        try:
            await message.reply(
                text,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            self.logger.exception(
                "Could not send self-bot reply",
                extra={"user_id": message.author.id, "guild_id": message.guild.id if message.guild else None},
            )

    async def on_message(self, message: discord.Message) -> None:
        """Apply the consent and trigger rules to every message the account can see."""
        try:
            await self._handle_message(message)
        except Exception:
            self.logger.exception("Unhandled self-bot message error", extra={"user_id": message.author.id})

    async def _handle_message(self, message: discord.Message) -> None:
        decision = decide(self._incoming(message), self.allowlist)
        if decision.kind is Kind.REGISTER:
            self.allowlist.add(message.author.id)
            await self._reply(message, WELCOME_TEXT)
            if decision.convert:
                await self._convert_and_reply(message)
        elif decision.kind is Kind.UNREGISTER:
            self.allowlist.remove(message.author.id)
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
            self.logger.info("Rate limited trigger ignored", extra={"user_id": author_id})
            return
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
            results = [await self._convert_one(source, author_id, guild_id) for source in collected.sources]
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
            await super().close()


def main() -> None:
    """Load configuration and run the self-bot until interrupted."""
    load_dotenv()
    settings = read_settings("SELFBOT_TOKEN")
    configure_logging(settings.log_level)
    data_dir = Path(os.getenv("SELFBOT_DATA_DIR", "data"))
    bot = SelfBot(settings, AllowlistStore(data_dir / "allowlist.json"))
    bot.run(settings.discord_token, log_handler=None)


if __name__ == "__main__":
    main()
