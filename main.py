"""Bombagif: safely convert Discord images to GIFs and upload them to Zipline."""

from __future__ import annotations

import asyncio
import configparser
import logging
import os
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from bombagif_core import (
    DOWNLOAD_TIMEOUT,
    MAX_GIF_BATCH,
    Settings,
    UploadFailure,
    UserFacingError,
    configure_logging,
    convert_and_upload,
    read_settings,
    validate_attachment_metadata,
)

load_dotenv()

PRESENCE_CONFIG_PATH: Final[Path] = Path(__file__).with_name("status.config")
PRESENCE_WATCH_SECONDS: Final[float] = 15.0
PRESENCE_REASSERT_TICKS: Final[int] = 20
MAX_PRESENCE_TEXT: Final[int] = 128
MAX_PRESENCE_BUTTON_LABEL: Final[int] = 32
MAX_PRESENCE_ASSET_KEY: Final[int] = 32
USER_INSTALL_BUTTON_URL: Final[str] = "user-install"


class PublicGifView(discord.ui.View):
    """Single-use share control and private direct-link copy reply."""

    def __init__(self, requester_id: int, embed: discord.Embed, link: str) -> None:
        super().__init__(timeout=15 * 60)
        self.requester_id = requester_id
        self.embed = embed
        self.link = link
        self.posted = False
        self._post_lock = asyncio.Lock()

    @discord.ui.button(
        label="Share in this chat",
        style=discord.ButtonStyle.primary,
        emoji="📣",
    )
    async def share_in_chat(self, interaction: discord.Interaction, button: discord.ui.Button["PublicGifView"]) -> None:
        """Post the GIF into the same server, DM, or group DM once."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who requested this GIF can share it.", ephemeral=True
            )
            return

        async with self._post_lock:
            if self.posted:
                await interaction.response.send_message("This GIF was already shared.", ephemeral=True)
                return

            if interaction.guild_id is not None:
                if interaction.guild is None or interaction.channel is None:
                    await interaction.response.send_message(
                        "I couldn't access this server channel to share the GIF.", ephemeral=True
                    )
                    return
                if not interaction.app_permissions.send_messages or not interaction.app_permissions.embed_links:
                    await interaction.response.send_message(
                        "I need Send Messages and Embed Links permission in this channel to share the GIF.",
                        ephemeral=True,
                    )
                    return

            # A component defer-update keeps the original (possibly ephemeral)
            # result intact; its follow-up is a new, visible message in this chat.
            await interaction.response.defer()
            await interaction.followup.send(
                embed=self.embed,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self.posted = True
            button.disabled = True
            button.label = "Shared"
            await interaction.edit_original_response(
                view=self,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @discord.ui.button(
        label="Copy Link",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
    )
    async def copy_link(self, interaction: discord.Interaction, button: discord.ui.Button["PublicGifView"]) -> None:
        """Show the direct GIF URL privately so the requester can copy it."""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the person who requested this GIF can copy its link.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            self.link,
            ephemeral=True,
            suppress_embeds=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _presence_text(
    section: configparser.SectionProxy,
    key: str,
    limit: int = MAX_PRESENCE_TEXT,
) -> str:
    """Read one single-line status.config value within Discord's length limit."""
    value = section.get(key, "").strip()
    if len(value) > limit or "\n" in value or "\r" in value:
        raise RuntimeError(f"status.config {key} must be a single line of at most {limit} characters")
    return value


def _https_url(value: str, key: str) -> str:
    """Validate a status.config URL that Discord requires over HTTPS."""
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError
        _ = parsed.port
    except ValueError as exc:
        raise RuntimeError(f"status.config {key} must be a valid HTTPS URL") from exc
    return value


def _user_install_url(application_id: int | None) -> str | None:
    """Build this app's account-level command-install URL once Discord supplies its ID."""
    if application_id is None:
        return None
    return (
        f"https://discord.com/oauth2/authorize?client_id={application_id}"
        "&scope=applications.commands&integration_type=1"
    )


def _presence_button_url(section: configparser.SectionProxy, application_id: int | None) -> str:
    """Resolve button_url, including the `user-install` shortcut for this app."""
    raw = section.get("button_url", "").strip()
    if not raw:
        return ""
    if raw.lower() == USER_INSTALL_BUTTON_URL:
        # The application id exists after Discord sends READY; presence is
        # re-applied then, so the link is not lost during initial startup.
        return _user_install_url(application_id) or ""
    return _https_url(raw, "button_url")


def load_presence_config(
    config_path: str | os.PathLike[str] = PRESENCE_CONFIG_PATH,
    application_id: int | None = None,
) -> tuple[discord.Status, discord.BaseActivity | None]:
    """Load and validate the public Discord presence card from status.config.

    The card's title (`activity_name`), line 1 (`details`), line 2 (`state`),
    and optional button are all configurable. `application_id` is only used to
    resolve the `button_url = user-install` shortcut into an install link.
    """
    path = Path(config_path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as config_file:
            parser.read_file(config_file)
    except (OSError, configparser.Error) as exc:
        raise RuntimeError(f"Could not read Discord presence config at {path}: {exc}") from exc

    if not parser.has_section("status"):
        raise RuntimeError(f"Presence config {path} must contain a [status] section")
    section = parser["status"]
    status_name = section.get("status", "online").strip().lower()
    if status_name not in {"online", "idle", "dnd", "invisible"}:
        raise RuntimeError("status.config status must be online, idle, dnd, or invisible")
    status = discord.Status(status_name)

    activity_type = section.get("activity_type", "playing").strip().lower()
    activity_kinds = {
        "playing": discord.ActivityType.playing,
        "listening": discord.ActivityType.listening,
        "watching": discord.ActivityType.watching,
        "competing": discord.ActivityType.competing,
        "streaming": discord.ActivityType.streaming,
    }
    if activity_type not in {*activity_kinds, "custom"}:
        raise RuntimeError(f"status.config activity_type must be one of: {', '.join(sorted({*activity_kinds, 'custom'}))}")

    activity_text = _presence_text(section, "activity_text")
    activity_name = _presence_text(section, "activity_name")
    details = _presence_text(section, "details")
    state = _presence_text(section, "state")
    button_label = _presence_text(section, "button_label", MAX_PRESENCE_BUTTON_LABEL)
    assets = {
        "large_image": _presence_text(section, "large_image", MAX_PRESENCE_ASSET_KEY),
        "large_text": _presence_text(section, "large_text"),
        "small_image": _presence_text(section, "small_image", MAX_PRESENCE_ASSET_KEY),
        "small_text": _presence_text(section, "small_text"),
    }
    assets = {key: value for key, value in assets.items() if value}
    if not (activity_text or activity_name or details or state):
        return status, None

    # Discord needs a title, and the config's own text is the best fallback.
    name = activity_name or activity_text or details or state

    if activity_type == "custom":
        if details or state or button_label or assets:
            raise RuntimeError(
                "status.config activity_type = custom keeps a single line; use another "
                "activity_type for details, state, button_label, or images"
            )
        activity_emoji = section.get("activity_emoji", "").strip() or None
        return status, discord.CustomActivity(name=name, emoji=activity_emoji)

    streaming_url = ""
    if activity_type == "streaming":
        streaming_url = _https_url(section.get("streaming_url", "").strip(), "streaming_url")

    # Discord only renders details/state/buttons/assets for rich activities, so
    # a plain one-line config keeps using the slim library activities.
    if not (details or state or button_label or assets):
        if activity_type == "playing":
            return status, discord.Game(name=name)
        if activity_type == "streaming":
            return status, discord.Streaming(name=name, url=streaming_url)
        return status, discord.Activity(type=activity_kinds[activity_type], name=name)

    activity_fields: dict[str, Any] = {"type": activity_kinds[activity_type], "name": name}
    if streaming_url:
        activity_fields["url"] = streaming_url
    if details:
        activity_fields["details"] = details
    if state:
        activity_fields["state"] = state
    if assets:
        activity_fields["assets"] = assets
    button_url = _presence_button_url(section, application_id)
    if button_label:
        activity_fields["buttons"] = [button_label]
        if button_url:
            # Discord resolves button links from the app's own settings, so the
            # same URL is attached to the state line as a guaranteed link.
            activity_fields["state_url"] = button_url
    return status, discord.Activity(**activity_fields)


def load_presence_or_default(
    application_id: int | None = None,
) -> tuple[discord.Status, discord.BaseActivity | None]:
    """Load status.config, falling back to a visible online presence on error.

    A broken or half-edited status.config must never leave the app appearing
    offline or invisible, so an unreadable file keeps the bot online.
    """
    try:
        return load_presence_config(PRESENCE_CONFIG_PATH, application_id)
    except RuntimeError as exc:
        logging.getLogger("bombagif").error("Invalid status.config (%s); keeping the online presence", exc)
        return discord.Status.online, None


def _presence_signature() -> tuple[int, int] | None:
    """Fingerprint status.config so the watcher only reacts to real edits."""
    try:
        stat = PRESENCE_CONFIG_PATH.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _read_settings() -> Settings:
    """Read the slash-command app's settings (requires DISCORD_TOKEN)."""
    return read_settings("DISCORD_TOKEN")


class BombagifBot(commands.Bot):
    """Discord bot handling image attachments and the `/gif` command."""

    def __init__(self, settings: Settings) -> None:
        # The core user-installed slash command does not need privileged intents.
        intents = discord.Intents.default()
        # Send the configured presence with the initial IDENTIFY payload so the
        # app is never briefly offline, and re-apply it on every reconnect.
        self.presence_status, self.presence_activity = load_presence_or_default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            status=self.presence_status,
            activity=self.presence_activity,
        )
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.max_parallel_jobs = max(1, min(2, os.cpu_count() or 1))
        self.image_executor = ThreadPoolExecutor(max_workers=self.max_parallel_jobs)
        self.processing_semaphore = asyncio.Semaphore(self.max_parallel_jobs)
        self._shutdown = False
        self._presence_watch_task: asyncio.Task[None] | None = None
        self.logger = logging.getLogger("bombagif")

    async def setup_hook(self) -> None:
        """Initialize the shared HTTP session and synchronize application commands."""
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        # Global synchronization is required for user-installed commands.
        await self.tree.sync()
        self.logger.info("Global user-installed command synced")
        # Watch status.config so presence edits apply without a restart.
        self._presence_watch_task = asyncio.create_task(self._watch_presence())

    async def close(self) -> None:
        """Close HTTP resources and drain image work before disconnecting."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            if self._presence_watch_task is not None:
                self._presence_watch_task.cancel()
                try:
                    await self._presence_watch_task
                except asyncio.CancelledError:
                    pass
                self._presence_watch_task = None
            if self.http_session and not self.http_session.closed:
                await self.http_session.close()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self.image_executor.shutdown(wait=True, cancel_futures=True)
            )
        finally:
            await super().close()

    async def _apply_presence(self, reason: str) -> None:
        """Reload status.config and push the presence, never failing the connection."""
        status, activity = load_presence_or_default(self.application_id)
        self.presence_status, self.presence_activity = status, activity
        try:
            await self.change_presence(status=status, activity=activity)
        except Exception:  # pragma: no cover - defensive; presence must not break startup
            self.logger.exception("Could not apply Discord presence", extra={"reason": reason})
            return
        self.logger.info(
            "Discord presence applied",
            extra={"reason": reason, "status": status.value, "activity": activity.name if activity else None},
        )

    async def on_ready(self) -> None:
        """Apply configured presence after every successful Discord connection."""
        # Keep status.config's large_image key identical to the Developer Portal asset.
        # The user-install button resolves with Discord's READY Client ID and integration_type=1.
        await self._apply_presence("ready")
        self.logger.info("Bot connected", extra={"user_id": self.user.id if self.user else None})

    async def on_resumed(self) -> None:
        """Re-apply presence after a gateway resume, which skips on_ready."""
        await self._apply_presence("resumed")

    async def on_message(self, message: discord.Message) -> None:
        """Help human users who DM the bot; never answer in guild channels."""
        if message.author.bot or message.guild is not None:
            return

        install_url = _user_install_url(self.application_id)
        response = (
            "Yo! I'm Bombagif. Use `/gif` and attach JPG/JPEG, AVIF, WEBP, PNG, SVG, or a video to get an optimized [GIF link](https://gifs.bombaclat.wtf/u/T1XJjE.gif). You can add up to 5 files at once."
        )
        if install_url:
            response += f"\n[**Click this hyper-link to add Bombagif to your apps.**](<{install_url}>)"
        await message.channel.send(
            response,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _presence_watch_step(
        self,
        signature: tuple[int, int] | None,
        tick: int,
    ) -> tuple[tuple[int, int] | None, int]:
        """Apply edited presence config, or re-assert it periodically.

        Returns the refreshed fingerprint and tick count so the loop stays
        stateless and this step is directly testable.
        """
        current = _presence_signature()
        if current != signature:
            await self._apply_presence("status.config changed")
            return current, 0
        tick += 1
        if self.ws is not None and tick >= PRESENCE_REASSERT_TICKS:
            # Discord can quietly drop a presence nothing refreshes, and an
            # app with no users is easy to forget, so keep it authoritative.
            await self._apply_presence("periodic reassert")
            return current, 0
        return current, tick

    async def _watch_presence(self) -> None:
        """Reload status.config while running so edits need no restart."""
        signature, tick = _presence_signature(), 0
        try:
            while True:
                await asyncio.sleep(PRESENCE_WATCH_SECONDS)
                signature, tick = await self._presence_watch_step(signature, tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - the watcher must never stop the bot
            self.logger.exception("Presence watcher stopped")

    def _log_context(self, user_id: int, guild_id: int | None, trace_id: str) -> dict[str, Any]:
        """Build per-request logging context."""
        return {"user_id": user_id, "guild_id": guild_id, "trace_id": trace_id}

    async def _process_one(
        self,
        attachment: discord.Attachment,
        user_id: int,
        guild_id: int | None,
    ) -> str:
        """Validate, download, convert and upload one attachment."""
        async with self.processing_semaphore:
            return await self._process_one_limited(attachment, user_id, guild_id)

    async def _process_one_limited(
        self,
        attachment: discord.Attachment,
        user_id: int,
        guild_id: int | None,
    ) -> str:
        """Run one pipeline while holding the global media-work limit."""
        trace_id = uuid.uuid4().hex
        # Fail fast (before any download) on unsupported types or declared sizes.
        validate_attachment_metadata(attachment.filename, attachment.content_type, attachment.size)
        if self.http_session is None:
            raise UploadFailure("The bot is still starting up. Please try again shortly.")
        return await convert_and_upload(
            self.http_session,
            self.settings,
            self.image_executor,
            attachment,
            trace_id=trace_id,
            log_context={"user_id": user_id, "guild_id": guild_id},
        )

    async def _process_many(
        self,
        attachments: list[discord.Attachment],
        destination: discord.abc.Messageable,
        user_id: int,
        guild_id: int | None,
    ) -> None:
        """Process each selected image/video and report a concise per-file result."""
        for attachment in attachments:
            try:
                link = await self._process_one(attachment, user_id, guild_id)
                await destination.send(
                    f"**{discord.utils.escape_markdown(attachment.filename)}** → {link}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except UserFacingError as exc:
                await destination.send(
                    f"**{discord.utils.escape_markdown(attachment.filename)}**: {exc}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                self.logger.exception(
                    "Unexpected media processing error",
                    extra=self._log_context(user_id, guild_id, uuid.uuid4().hex),
                )
                await destination.send(
                    f"**{discord.utils.escape_markdown(attachment.filename)}**: Sorry, the media could not be processed.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )

    async def interaction_upload_batch(
        self, interaction: discord.Interaction, attachments: list[discord.Attachment]
    ) -> None:
        """Convert selected attachments sequentially and return per-file results."""
        if len(attachments) > MAX_GIF_BATCH:
            await interaction.response.send_message(
                f"You can convert up to {MAX_GIF_BATCH} files per `/gif` command.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild_id = interaction.guild_id
        if self.settings.allowed_guilds and guild_id is not None and guild_id not in self.settings.allowed_guilds:
            await interaction.response.send_message("This bot is not enabled in this server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        for attachment in attachments:
            try:
                link = await self._process_one(attachment, interaction.user.id, guild_id)
            except UserFacingError as exc:
                await interaction.followup.send(
                    f"{discord.utils.escape_markdown(attachment.filename)}: {exc}",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                continue
            except Exception:
                self.logger.exception(
                    "Unexpected batch processing error",
                    extra=self._log_context(interaction.user.id, guild_id, uuid.uuid4().hex),
                )
                await interaction.followup.send(
                    f"{discord.utils.escape_markdown(attachment.filename)}: Could not process this file.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                continue

            embed = discord.Embed(
                title=discord.utils.escape_markdown(attachment.filename)[:256],
                description="Your optimized GIF is ready to view or share.",
                color=discord.Color.blurple(),
            )
            embed.set_image(url=link)
            embed.add_field(name="Open GIF", value=f"[View or copy the direct link]({link})")
            embed.set_footer(text="Converted by Bombagif")
            await interaction.followup.send(
                embed=embed,
                view=PublicGifView(interaction.user.id, embed, link),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        await interaction.edit_original_response(
            content=f"Finished processing {len(attachments)} files.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def interaction_upload(
        self, interaction: discord.Interaction, attachment: discord.Attachment
    ) -> None:
        """Validate slash command access, defer promptly, then process its attachment."""
        guild_id = interaction.guild_id
        if (
            self.settings.allowed_guilds
            and guild_id is not None
            and guild_id not in self.settings.allowed_guilds
        ):
            await interaction.response.send_message("This bot is not enabled in this server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            link = await self._process_one(attachment, interaction.user.id, guild_id)
            embed = discord.Embed(
                title="Your GIF is ready",
                description="Your optimized GIF is ready to view or share.",
                color=discord.Color.blurple(),
            )
            embed.set_image(url=link)
            embed.add_field(name="Open GIF", value=f"[View or copy the direct link]({link})")
            embed.set_footer(text="Converted by Bombagif")
            await interaction.followup.send(
                embed=embed,
                view=PublicGifView(interaction.user.id, embed, link),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except UserFacingError as exc:
            await interaction.followup.send(
                str(exc), ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
            )
        except Exception:
            self.logger.exception(
                "Unexpected slash command processing error",
                extra=self._log_context(interaction.user.id, guild_id, uuid.uuid4().hex),
            )
            await interaction.followup.send(
                "Sorry, something went wrong while processing that media.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def create_bot(settings: Settings) -> BombagifBot:
    """Create the bot and register the globally available `/gif` command."""
    bot = BombagifBot(settings)

    @bot.tree.command(name="gif", description="Convert 1–5 images or videos to GIFs under 1 MB when possible")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(
        media="The first image (PNG/JPG/JPEG/AVIF/WEBP/SVG) or MP4/WebM video",
        media_2="Optional second file",
        media_3="Optional third file",
        media_4="Optional fourth file",
        media_5="Optional fifth file",
    )
    async def gif_command(
        interaction: discord.Interaction,
        media: discord.Attachment,
        media_2: discord.Attachment | None = None,
        media_3: discord.Attachment | None = None,
        media_4: discord.Attachment | None = None,
        media_5: discord.Attachment | None = None,
    ) -> None:
        """Convert one to five uploads to optimized GIFs."""
        attachments = [item for item in (media, media_2, media_3, media_4, media_5) if item is not None]
        if len(attachments) == 1:
            await bot.interaction_upload(interaction, attachments[0])
        else:
            await bot.interaction_upload_batch(interaction, attachments)

    return bot


def _install_signal_handlers(bot: BombagifBot) -> None:
    """Make shutdown signals request a normal Discord and HTTP disconnect."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except (NotImplementedError, RuntimeError):
            pass


async def main() -> None:
    """Load configuration, connect to Discord and release resources on exit."""
    settings = _read_settings()
    configure_logging(settings.log_level, settings.log_format)
    bot = create_bot(settings)
    _install_signal_handlers(bot)
    try:
        await bot.start(settings.discord_token)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
