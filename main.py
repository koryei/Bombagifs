"""Bombagif: safely convert Discord images to GIFs and upload them to Zipline."""

from __future__ import annotations

import asyncio
import configparser
import importlib
import io
import json
import logging
import math
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
from defusedxml import ElementTree as SafeET
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from PIL import Image, ImageOps, UnidentifiedImageError

load_dotenv()

MAX_INPUT_BYTES: Final[int] = 15 * 1024 * 1024
MAX_OUTPUT_BYTES: Final[int] = 25 * 1024 * 1024
MAX_PIXELS: Final[int] = 20_000_000
MAX_FRAMES: Final[int] = 100
MAX_TOTAL_FRAME_PIXELS: Final[int] = 50_000_000
MAX_DIMENSION: Final[int] = 1024
MAX_SVG_DIMENSION: Final[int] = 2048
DOWNLOAD_TIMEOUT: Final[int] = 30
UPLOAD_TIMEOUT: Final[int] = 45
MAX_UPLOAD_ATTEMPTS: Final[int] = 4
PRESENCE_CONFIG_PATH: Final[Path] = Path(__file__).with_name("status.config")
SVG_MAX_NODES: Final[int] = 20_000
SVG_MAX_TEXT_BYTES: Final[int] = 2_000_000
ALLOWED_TYPES: Final[dict[str, set[str]]] = {
    ".webp": {"image/webp"},
    ".png": {"image/png"},
    ".svg": {"image/svg+xml", "text/xml", "application/xml"},
}


class UserFacingError(Exception):
    """An expected error safe to report to the requester."""


class InvalidImage(UserFacingError):
    """The input image is invalid or exceeds image processing safety limits."""


class UploadFailure(UserFacingError):
    """The Zipline service could not accept or report the uploaded file."""


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


def load_presence_config(
    config_path: str | os.PathLike[str] = PRESENCE_CONFIG_PATH,
) -> tuple[discord.Status, discord.BaseActivity | None]:
    """Load and validate the public Discord status/activity from status.config."""
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
    allowed_types = {"playing", "listening", "watching", "competing", "streaming", "custom"}
    if activity_type not in allowed_types:
        raise RuntimeError(f"status.config activity_type must be one of: {', '.join(sorted(allowed_types))}")
    activity_text = section.get("activity_text", "").strip()
    if len(activity_text) > 128 or "\n" in activity_text or "\r" in activity_text:
        raise RuntimeError("status.config activity_text must be a single line of at most 128 characters")
    if not activity_text:
        return status, None

    if activity_type == "playing":
        activity: discord.BaseActivity = discord.Game(name=activity_text)
    elif activity_type == "streaming":
        streaming_url = section.get("streaming_url", "").strip()
        try:
            parsed_stream = urlparse(streaming_url)
            if parsed_stream.scheme != "https" or not parsed_stream.hostname:
                raise ValueError
            _ = parsed_stream.port
        except ValueError as exc:
            raise RuntimeError("status.config streaming_url must be a valid HTTPS URL when activity_type is streaming") from exc
        activity = discord.Streaming(name=activity_text, url=streaming_url)
    elif activity_type == "custom":
        activity_emoji = section.get("activity_emoji", "").strip() or None
        activity = discord.CustomActivity(name=activity_text, emoji=activity_emoji)
    else:
        activity_kinds = {
            "listening": discord.ActivityType.listening,
            "watching": discord.ActivityType.watching,
            "competing": discord.ActivityType.competing,
        }
        activity = discord.Activity(type=activity_kinds[activity_type], name=activity_text)
    return status, activity


def validate_image_metadata(filename: str, content_type: str | None, size: int | None) -> str:
    """Validate the supported extension and known declared size."""
    suffix = os.path.splitext(os.path.basename(filename).lower())[1]
    if suffix not in ALLOWED_TYPES:
        raise InvalidImage("Please use a WEBP, PNG, or SVG image.")
    # Discord can omit or misreport attachment MIME metadata. Validate the
    # extension here and verify the actual decoded format in convert_image.
    if size is not None and size > MAX_INPUT_BYTES:
        raise UserFacingError("That image is too large. The maximum upload size is 15 MB.")
    return suffix


def _check_svg(svg: bytes) -> tuple[int, int]:
    """Reject unsafe XML constructs and external references, and bound SVG size."""
    if len(svg) > MAX_INPUT_BYTES:
        raise InvalidImage("That SVG is too large to process safely.")
    lowered = svg.lower()
    if any(marker in lowered for marker in (b"<!doctype", b"<!entity", b"<script", b"<foreignobject")):
        raise InvalidImage("This SVG contains unsupported or unsafe content.")
    try:
        root = SafeET.fromstring(svg)
    except Exception as exc:
        raise InvalidImage("That SVG file is not valid XML.") from exc
    if not isinstance(root.tag, str) or root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise InvalidImage("That file does not contain a valid SVG image.")
    if not root.tag.startswith("{http://www.w3.org/2000/svg}"):
        raise InvalidImage("That file does not use a supported SVG namespace.")
    if b"<?xml-stylesheet" in lowered:
        raise InvalidImage("SVG images must not reference external resources.")

    elements = list(root.iter())
    if len(elements) > SVG_MAX_NODES:
        raise InvalidImage("That SVG contains too much detail to process safely.")
    text_budget = 0
    for element in elements:
        tag = element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""
        if tag in {"script", "foreignobject", "iframe", "object", "audio", "video"}:
            raise InvalidImage("This SVG contains unsupported or unsafe content.")
        text_budget += len(element.text or "")
        if element.text:
            text = element.text.lower()
            if "@import" in text:
                raise InvalidImage("SVG images must not reference external resources.")
            if "url(" in text:
                targets = re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)", element.text, re.IGNORECASE)
                if not targets or any(not target.strip().startswith("#") for _, target in targets):
                    raise InvalidImage("SVG images must not reference external resources.")
                if text.count("url(") != len(targets):
                    raise InvalidImage("SVG images must not reference external resources.")
            elif re.search(r"(?:https?:|file:|ftp:|data:|//)", text, re.IGNORECASE):
                raise InvalidImage("SVG images must not reference external resources.")
            elif "url" in text:
                raise InvalidImage("SVG images contain malformed resource references.")
        for raw_name, raw_value in element.attrib.items():
            text_budget += len(raw_name) + len(raw_value)
            name = raw_name.rsplit("}", 1)[-1].lower()
            value = raw_value.strip()
            if name.startswith("on") or name == "base":
                raise InvalidImage("This SVG contains unsupported or unsafe content.")
            if name in {"href", "src"} and value and not value.startswith("#"):
                raise InvalidImage("SVG images must not reference external resources.")
            if "@import" in value.lower():
                raise InvalidImage("SVG images must not reference external resources.")
            if "url(" in value.lower():
                targets = re.findall(r"url\(\s*(['\"]?)(.*?)\1\s*\)", value, re.IGNORECASE)
                if not targets or any(not target.strip().startswith("#") for _, target in targets):
                    raise InvalidImage("SVG images must not reference external resources.")
                if value.lower().count("url(") != len(targets):
                    raise InvalidImage("SVG images must not reference external resources.")
            elif "url" in value.lower():
                raise InvalidImage("SVG images contain malformed resource references.")
        if text_budget > SVG_MAX_TEXT_BYTES:
            raise InvalidImage("That SVG contains too much detail to process safely.")

    width = _svg_length(root.attrib.get("width"), default=MAX_SVG_DIMENSION)
    height = _svg_length(root.attrib.get("height"), default=MAX_SVG_DIMENSION)
    view_box = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if (not root.attrib.get("width") or not root.attrib.get("height")) and view_box:
        try:
            _, _, view_width, view_height = (float(part) for part in re.split(r"[\s,]+", view_box.strip()))
            if not math.isfinite(view_width) or not math.isfinite(view_height):
                raise ValueError("SVG viewBox dimensions must be finite")
            width = min(MAX_SVG_DIMENSION, max(1, int(view_width)))
            height = min(MAX_SVG_DIMENSION, max(1, int(view_height)))
        except (ValueError, TypeError, OverflowError):
            pass
    width, height = min(width, MAX_SVG_DIMENSION), min(height, MAX_SVG_DIMENSION)
    if width * height > MAX_PIXELS:
        raise InvalidImage("That SVG has dimensions that are too large to process safely.")
    return width, height


def _svg_length(value: str | None, default: int) -> int:
    """Read a positive SVG pixel dimension, falling back for percentages/units."""
    if value is None:
        return default
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*", value, re.IGNORECASE)
    if not match:
        return default
    try:
        length = float(match.group(1))
        if not math.isfinite(length):
            return default
        return max(1, min(MAX_SVG_DIMENSION, int(length)))
    except (ValueError, OverflowError):
        return MAX_SVG_DIMENSION


class _BoundedBytesIO(io.BytesIO):
    """In-memory output stream that stops GIF encoding at a fixed size cap."""

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    def write(self, content: bytes) -> int:
        """Reject writes that would exceed the configured cap."""
        if self.tell() + len(content) > self.limit:
            raise InvalidImage("The converted GIF is too large to upload.")
        return super().write(content)


def _rasterize_svg(data: bytes, width: int, height: int) -> bytes:
    """Rasterize an SVG, reporting clearly when Cairo's native library is missing."""
    try:
        cairosvg = importlib.import_module("cairosvg")
    except (ImportError, OSError) as exc:
        raise InvalidImage(
            "SVG conversion needs Cairo. On macOS install it with `brew install cairo`, "
            "then restart Bombagif."
        ) from exc
    try:
        return cairosvg.svg2png(
            bytestring=data,
            output_width=width,
            output_height=height,
            unsafe=False,
        )
    except (ValueError, OSError, RuntimeError, MemoryError, OverflowError) as exc:
        raise InvalidImage("That SVG could not be safely converted to an image.") from exc


def convert_image(data: bytes, suffix: str, background: tuple[int, int, int]) -> bytes:
    """Decode/rasterize a supported image and encode an optimized bounded GIF."""
    if len(data) > MAX_INPUT_BYTES:
        raise InvalidImage("That image is too large. The maximum upload size is 15 MB.")
    if suffix == ".svg":
        width, height = _check_svg(data)
        data = _rasterize_svg(data, width, height)

    try:
        with Image.open(io.BytesIO(data)) as image:
            expected_format = {".png": "PNG", ".webp": "WEBP", ".svg": "PNG"}[suffix]
            if image.format != expected_format:
                raise InvalidImage("The image contents do not match the file extension.")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS:
                raise InvalidImage("That image has dimensions that are too large to process safely.")
            frames: list[Image.Image] = []
            durations: list[int] = []
            pixel_count = image.width * image.height
            output_pixels = min(image.width, MAX_DIMENSION) * min(image.height, MAX_DIMENSION)
            frame_count = min(
                getattr(image, "n_frames", 1),
                MAX_FRAMES,
                max(1, MAX_TOTAL_FRAME_PIXELS // pixel_count),
                max(1, MAX_TOTAL_FRAME_PIXELS // output_pixels),
            )
            for frame_index in range(frame_count):
                image.seek(frame_index)
                frame = ImageOps.exif_transpose(image.copy())
                frame.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.Resampling.LANCZOS)
                rgba = frame.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, background)
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                frames.append(flattened.quantize(colors=256, method=Image.Quantize.MEDIANCUT))
                durations.append(min(60_000, max(20, int(image.info.get("duration", 100)))))
            if not frames:
                raise InvalidImage("That image does not contain a usable frame.")
            output = _BoundedBytesIO(MAX_OUTPUT_BYTES)
            save_options: dict[str, Any] = {
                "format": "GIF",
                "save_all": len(frames) > 1,
                "optimize": True,
                "disposal": 2,
            }
            if len(frames) > 1:
                save_options.update(append_images=frames[1:], duration=durations, loop=0)
            frames[0].save(output, **save_options)
            result = output.getvalue()
            if len(result) > MAX_OUTPUT_BYTES:
                raise InvalidImage("The converted GIF is too large to upload.")
            return result
    except (InvalidImage, UnidentifiedImageError, OSError, ValueError, EOFError, SyntaxError, Image.DecompressionBombError) as exc:
        if isinstance(exc, InvalidImage):
            raise
        raise InvalidImage("That image could not be decoded. Please try another file.") from exc


async def read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read an HTTP response body with an enforced streaming byte limit."""
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise UserFacingError("That image is too large. The maximum upload size is 15 MB.")
    return bytes(chunks)


async def download_attachment(session: aiohttp.ClientSession, attachment: discord.Attachment) -> bytes:
    """Download a validated Discord attachment while enforcing the hard input cap."""
    validate_image_metadata(attachment.filename, attachment.content_type or "", attachment.size)
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, connect=10, sock_read=20)
    try:
        async with session.get(attachment.url, timeout=timeout) as response:
            if response.status != 200:
                raise UserFacingError("Discord could not provide that attachment. Please try again.")
            if response.content_length is not None and response.content_length > MAX_INPUT_BYTES:
                raise UserFacingError("That image is too large. The maximum upload size is 15 MB.")
            return await read_bounded(response, MAX_INPUT_BYTES)
    except asyncio.TimeoutError as exc:
        raise UserFacingError("Downloading that image timed out. Please try again.") from exc
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


def _extract_zipline_file_url(payload: Any) -> str | None:
    """Read the uploaded file URL from common Zipline API response versions."""
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
        if (
            parsed_url.scheme.lower() != "https"
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
        ):
            return None
        # Accessing .port also validates malformed authority suffixes.
        _ = parsed_url.port
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
                returned_url = _extract_zipline_file_url(payload)
                if returned_url is None:
                    logging.getLogger("bombagif.zipline").warning(
                        "Zipline returned success without a readable file URL "
                        "(content_type=%s, payload_type=%s)",
                        response.content_type,
                        type(payload).__name__,
                        extra={"trace_id": trace_id},
                    )
                    raise UploadFailure(
                        "Zipline returned a successful upload, but Bombagif couldn't read its file URL. "
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
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.http_session: aiohttp.ClientSession | None = None
        self.max_parallel_jobs = max(1, min(2, os.cpu_count() or 1))
        self.image_executor = ThreadPoolExecutor(max_workers=self.max_parallel_jobs)
        self.processing_semaphore = asyncio.Semaphore(self.max_parallel_jobs)
        self._shutdown = False
        self.logger = logging.getLogger("bombagif")

    async def setup_hook(self) -> None:
        """Initialize the shared HTTP session and synchronize application commands."""
        timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        connector = aiohttp.TCPConnector(limit=32, ttl_dns_cache=300)
        self.http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
        # Global synchronization is required for user-installed commands.
        await self.tree.sync()
        self.logger.info("Global user-installed command synced")

    async def close(self) -> None:
        """Close HTTP resources and drain image work before disconnecting."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            if self.http_session and not self.http_session.closed:
                await self.http_session.close()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None, lambda: self.image_executor.shutdown(wait=True, cancel_futures=True)
            )
        finally:
            await super().close()

    async def on_ready(self) -> None:
        """Apply configured presence after every successful Discord connection."""
        status, activity = load_presence_config()
        await self.change_presence(status=status, activity=activity)
        self.logger.info("Bot connected", extra={"user_id": self.user.id if self.user else None})

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
        """Run one pipeline while holding the global work/conversion limit."""
        trace_id = uuid.uuid4().hex
        context = self._log_context(user_id, guild_id, trace_id)
        suffix = validate_image_metadata(attachment.filename, attachment.content_type, attachment.size)
        if self.http_session is None:
            raise UploadFailure("The bot is still starting up. Please try again shortly.")
        self.logger.info("Processing attachment", extra={**context, "attachment_name": attachment.filename})
        data = await download_attachment(self.http_session, attachment)
        try:
            loop = asyncio.get_running_loop()
            gif_data = await loop.run_in_executor(
                self.image_executor,
                convert_image,
                data,
                suffix,
                self.settings.background,
            )
        except InvalidImage:
            raise
        except Exception as exc:
            self.logger.exception("Image conversion failed", extra=context)
            raise InvalidImage("That image could not be converted. Please try another file.") from exc
        safe_stem = re.sub(
            r"[^A-Za-z0-9_-]+",
            "-",
            os.path.splitext(os.path.basename(attachment.filename))[0],
        ).strip("-_")[:64] or "image"
        output_name = f"{safe_stem}.gif"
        return await upload_to_zipline(self.http_session, self.settings, gif_data, output_name, trace_id=trace_id)

    async def _process_many(
        self,
        attachments: list[discord.Attachment],
        destination: discord.abc.Messageable,
        user_id: int,
        guild_id: int | None,
    ) -> None:
        """Process every selected image and report a concise per-file result."""
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
                    "Unexpected attachment processing error",
                    extra=self._log_context(user_id, guild_id, uuid.uuid4().hex),
                )
                await destination.send(
                    f"**{discord.utils.escape_markdown(attachment.filename)}**: Sorry, the image could not be processed.",
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
            await interaction.followup.send(
                f"Your GIF is ready: {link}",
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
                "Sorry, something went wrong while processing that image.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


def create_bot(settings: Settings) -> BombagifBot:
    """Create the bot and register the globally available `/gif` command."""
    bot = BombagifBot(settings)

    @bot.tree.command(name="gif", description="Convert an uploaded WEBP, PNG, or SVG image to a GIF")
    @app_commands.allowed_installs(guilds=False, users=True)
    @app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
    @app_commands.describe(image="The WEBP, PNG, or SVG image to convert")
    async def gif_command(interaction: discord.Interaction, image: discord.Attachment) -> None:
        """Convert a slash-command attachment to a GIF and short link."""
        await bot.interaction_upload(interaction, image)

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
