"""Bombagif: safely convert Discord images to GIFs and upload them to Zipline."""

from __future__ import annotations

import asyncio
import configparser
import json
import logging
import os
import re
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
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
    MAX_INPUT_BYTES,
    MAX_UPLOAD_ATTEMPTS,
    UPLOAD_TIMEOUT,
    InvalidImage,
    UploadFailure,
    UserFacingError,
    _convert_image_to_target,
    convert_image,
    convert_video,
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


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime configuration loaded from the environment."""

    discord_token: str
    zipline_token: str
    zipline_url: str
    log_level: str
    allowed_guilds: frozenset[int]
    background: tuple[int, int, int]


def _read_settings() -> Settings:
    """Read required credentials and validate the optional runtime settings."""
    discord_token = os.getenv("DISCORD_TOKEN", "").strip()
    zipline_token = os.getenv("ZIPLINE_TOKEN", "").strip()
    if not discord_token or not zipline_token:
        raise RuntimeError("DISCORD_TOKEN and ZIPLINE_TOKEN must be configured")

    zipline_url = os.getenv("ZIPLINE_URL", "").strip().rstrip("/")
    if not zipline_url:
        raise RuntimeError("ZIPLINE_URL must be set to your own Zipline instance URL")
    parsed_url = urlparse(zipline_url)
    if (
        parsed_url.scheme != "https"
        or not parsed_url.hostname
        or parsed_url.username is not None
        or parsed_url.password is not None
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RuntimeError("ZIPLINE_URL must be an HTTPS base URL without embedded credentials or query parameters")

    guilds: set[int] = set()
    for guild_value in os.getenv("ALLOWED_GUILDS", "").split(","):
        guild_value = guild_value.strip()
        if guild_value:
            try:
                guild_id = int(guild_value)
                if guild_id <= 0:
                    raise ValueError
                guilds.add(guild_id)
            except ValueError as exc:
                raise RuntimeError("ALLOWED_GUILDS must contain comma-separated numeric IDs") from exc

    background_text = os.getenv("GIF_BACKGROUND", "#FFFFFF").strip()
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", background_text):
        raise RuntimeError("GIF_BACKGROUND must be a six-digit hex color such as #FFFFFF")
    background = tuple(int(background_text[index : index + 2], 16) for index in (1, 3, 5))

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise RuntimeError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    return Settings(
        discord_token=discord_token,
        zipline_token=zipline_token,
        zipline_url=zipline_url,
        log_level=log_level,
        allowed_guilds=frozenset(guilds),
        background=(background[0], background[1], background[2]),
    )


class JsonFormatter(logging.Formatter):
    """Format standard logging records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        """Serialize stable fields and contextual extras, without raw secrets."""
        payload: dict[str, Any] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("trace_id", "user_id", "guild_id", "attachment_name", "attempt"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    """Configure concise structured logs on stderr."""
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))


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


async def read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read an HTTP response body with an enforced streaming byte limit."""
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise UserFacingError("That file is too large. The maximum upload size is 15 MB.")
    return bytes(chunks)


async def download_attachment(session: aiohttp.ClientSession, attachment: discord.Attachment) -> bytes:
    """Download a validated image or video attachment within the hard input cap."""
    validate_attachment_metadata(attachment.filename, attachment.content_type or "", attachment.size)
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=10, sock_read=20)
    try:
        async with session.get(attachment.url, timeout=timeout) as response:
            if response.status != 200:
                raise UserFacingError("Discord could not provide that attachment. Please try again.")
            if response.content_length is not None and response.content_length > MAX_INPUT_BYTES:
                raise UserFacingError("That file is too large. The maximum upload size is 15 MB.")
            return await read_bounded(response, MAX_INPUT_BYTES)
    except asyncio.TimeoutError as exc:
        raise UserFacingError("Downloading that file timed out. Please try again.") from exc
    except aiohttp.ClientError as exc:
        raise UserFacingError("Discord could not provide that attachment. Please try again.") from exc


def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
    """Return a bounded Retry-After or exponential-backoff delay."""
    retry_after = response.headers.get("Retry-After")
    if retry_after:
        try:
            return min(8.0, max(0.0, float(retry_after)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return min(8.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return min(8.0, 0.5 * (2**attempt))


def _extract_zipline_file_url(payload: Any, zipline_base_url: str) -> str | None:
    """Read a Zipline file URL and upgrade same-host HTTP links behind TLS proxies."""
    configured_url = urlparse(zipline_base_url)
    candidate: Any = payload
    if isinstance(payload, list):
        candidate = payload[0] if payload else None
    elif isinstance(payload, dict):
        files = payload.get("files")
        if isinstance(files, list):
            candidate = files[0] if files else None
        elif isinstance(files, (str, dict)):
            candidate = files
        else:
            candidate = payload.get("url")
    if isinstance(candidate, dict):
        candidate = candidate.get("url")
    if not isinstance(candidate, str):
        return None

    file_url = candidate.strip()
    try:
        parsed_url = urlparse(file_url)
        configured_host = configured_url.hostname
        returned_host = parsed_url.hostname
        if (
            not configured_host
            or not returned_host
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            return None
        # Accessing .port also validates malformed authority suffixes.
        _ = parsed_url.port
        if parsed_url.scheme.lower() == "http":
            # Upgrade only when the bot's configured public URL is HTTPS and
            # Zipline's returned hostname matches it (ignoring an internal
            # port, which often differs behind a TLS-terminating proxy).
            if (
                configured_url.scheme.lower() != "https"
                or returned_host.casefold() != configured_host.casefold()
            ):
                return None
            file_url = parsed_url._replace(scheme="https", netloc=configured_url.netloc).geturl()
        elif parsed_url.scheme.lower() != "https":
            return None
    except ValueError:
        return None
    return file_url


async def upload_to_zipline(
    session: aiohttp.ClientSession,
    settings: Settings,
    content: bytes,
    filename: str,
    *,
    trace_id: str,
) -> str:
    """Upload a GIF to Zipline with bounded retries and parse its returned URL."""
    url = f"{settings.zipline_url}/api/upload"
    headers = {"Authorization": settings.zipline_token}
    timeout = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT, connect=10, sock_read=30)
    last_error: Exception | None = None

    for attempt in range(MAX_UPLOAD_ATTEMPTS):
        form = aiohttp.FormData()
        form.add_field("file", content, filename=filename, content_type="image/gif")
        try:
            async with session.post(url, headers=headers, data=form, timeout=timeout) as response:
                if response.status == 429 or response.status >= 500:
                    if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                        delay = _retry_delay(response, attempt)
                        logging.getLogger("bombagif.zipline").warning(
                            "Zipline requested retry",
                            extra={"trace_id": trace_id, "attempt": attempt + 1},
                        )
                        response.release()
                        await asyncio.sleep(delay)
                        continue
                    raise UploadFailure("The file host is temporarily unavailable. Please try again later.")
                if response.status < 200 or response.status >= 300:
                    raise UploadFailure("The file host rejected the upload. Please try again later.")
                try:
                    if response.content_length is not None and response.content_length > 1_000_000:
                        raise ValueError("Zipline response exceeded its size limit")
                    response_body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        response_body.extend(chunk)
                        if len(response_body) > 1_000_000:
                            raise ValueError("Zipline response exceeded its size limit")
                except (aiohttp.ClientError, ValueError) as exc:
                    raise UploadFailure("The file host returned an invalid response. Please try again later.") from exc

                try:
                    payload = json.loads(response_body)
                except (UnicodeDecodeError, ValueError, RecursionError):
                    # Some Zipline deployments (and reverse proxies) return the
                    # file URL as plain text instead of JSON.
                    payload = bytes(response_body).decode("utf-8", errors="replace").strip()
                returned_url = _extract_zipline_file_url(payload, settings.zipline_url)
                if returned_url is None:
                    logging.getLogger("bombagif.zipline").warning(
                        "Zipline returned success without a readable file URL "
                        "(content_type=%s, payload_type=%s)",
                        response.content_type,
                        type(payload).__name__,
                        extra={"trace_id": trace_id},
                    )
                    raise UploadFailure(
                        "Zipline accepted the upload, but Bombagif couldn't read a safe file URL. "
                        "For Zipline behind HTTPS, enable its Return HTTPS URLs setting "
                        "(CORE_RETURN_HTTPS_URLS=true) and confirm the returned link uses your configured Zipline host. "
                        "Check Zipline before retrying to avoid a duplicate upload."
                    )
                return returned_url
        except asyncio.TimeoutError as exc:
            last_error = exc
            if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                continue
        except (aiohttp.ClientError, OSError) as exc:
            last_error = exc
            if attempt + 1 < MAX_UPLOAD_ATTEMPTS:
                await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                continue
        except UploadFailure:
            raise

    error_info = (
        (type(last_error), last_error, last_error.__traceback__)
        if last_error is not None
        else None
    )
    logging.getLogger("bombagif.zipline").error(
        "Zipline upload failed after retries",
        extra={"trace_id": trace_id},
        exc_info=error_info,
    )
    if isinstance(last_error, asyncio.TimeoutError):
        raise UserFacingError("Uploading to the file host timed out. Please try again.") from last_error
    raise UploadFailure("The file host is unavailable. Please try again later.") from last_error


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
        context = self._log_context(user_id, guild_id, trace_id)
        suffix = validate_attachment_metadata(attachment.filename, attachment.content_type, attachment.size)
        if self.http_session is None:
            raise UploadFailure("The bot is still starting up. Please try again shortly.")
        self.logger.info("Processing attachment", extra={**context, "attachment_name": attachment.filename})
        data = await download_attachment(self.http_session, attachment)
        try:
            loop = asyncio.get_running_loop()
            converter = convert_video if suffix in {".mp4", ".webm"} else convert_image
            if converter is convert_video:
                conversion_args = (data, suffix, trace_id)
                gif_data = await loop.run_in_executor(self.image_executor, converter, *conversion_args)
            else:
                gif_data = await loop.run_in_executor(
                    self.image_executor,
                    _convert_image_to_target,
                    data,
                    suffix,
                    self.settings.background,
                )
        except InvalidImage:
            raise
        except Exception as exc:
            self.logger.exception("Media conversion failed", extra=context)
            raise InvalidImage("That file could not be converted. Please try another image or video.") from exc
        safe_stem = re.sub(
            r"[^A-Za-z0-9_-]+",
            "-",
            os.path.splitext(os.path.basename(attachment.filename))[0],
        ).strip("-_")[:64] or "media"
        output_name = f"{safe_stem}.gif"
        return await upload_to_zipline(self.http_session, self.settings, gif_data, output_name, trace_id=trace_id)

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
    configure_logging(settings.log_level)
    bot = create_bot(settings)
    _install_signal_handlers(bot)
    try:
        await bot.start(settings.discord_token)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())
