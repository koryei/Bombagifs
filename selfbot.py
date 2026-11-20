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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv

from bombagif_core import DOWNLOAD_TIMEOUT, Settings, configure_logging, read_settings
from selfbot_logic import AllowlistStore, RateLimiter


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
