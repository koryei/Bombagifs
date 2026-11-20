"""Discord-free Bombagif core: safe image/video-to-GIF conversion and upload helpers.

This module must never import ``discord``: the slash-command app (discord.py) and the
self-bot (discord.py-self) both import it, and those libraries cannot share an environment.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final, Protocol
from urllib.parse import urlparse

import aiohttp
from defusedxml import ElementTree as SafeET
from PIL import Image, ImageOps, UnidentifiedImageError, features

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
MAX_VIDEO_DURATION: Final[int] = 10
MAX_VIDEO_FRAMES: Final[int] = 150
VIDEO_CONVERSION_TIMEOUT: Final[int] = 45
VIDEO_MAX_DIMENSION: Final[int] = 480

SVG_MAX_NODES: Final[int] = 20_000
SVG_MAX_TEXT_BYTES: Final[int] = 2_000_000
ALLOWED_TYPES: Final[dict[str, set[str]]] = {
    ".webp": {"image/webp"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg", "image/jpg"},
    ".jpeg": {"image/jpeg", "image/jpg"},
    ".avif": {"image/avif"},
    ".svg": {"image/svg+xml", "text/xml", "application/xml"},
    ".mp4": {"video/mp4"},
    ".webm": {"video/webm"},
}
GIF_TARGET_BYTES: Final[int] = 1_000_000
MAX_GIF_BATCH: Final[int] = 5


class UserFacingError(Exception):
    """An expected error safe to report to the requester."""


class InvalidImage(UserFacingError):
    """The input image is invalid or exceeds image processing safety limits."""


class _CandidateTooLarge(InvalidImage):
    """A valid conversion may fit after reducing dimensions or frame count."""


class UploadFailure(UserFacingError):
    """The Zipline service could not accept or report the uploaded file."""


def validate_attachment_metadata(filename: str, content_type: str | None, size: int | None) -> str:
    """Validate a supported image or video extension and known declared size."""
    suffix = os.path.splitext(os.path.basename(filename).lower())[1]
    if suffix not in ALLOWED_TYPES:
        raise InvalidImage("Use a WEBP, PNG, JPG, JPEG, AVIF, SVG, MP4, or WebM file.")
    # Discord can omit or misreport attachment MIME metadata. Validate the
    # extension here and verify the actual decoded format in the converter.
    if size is not None and size > MAX_INPUT_BYTES:
        raise UserFacingError("That file is too large. The maximum upload size is 15 MB.")
    return suffix


def validate_image_metadata(filename: str, content_type: str | None, size: int | None) -> str:
    """Backward-compatible alias for attachment metadata validation."""
    return validate_attachment_metadata(filename, content_type, size)


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
            raise _CandidateTooLarge("The converted GIF is too large to upload.")
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


def convert_video(data: bytes, suffix: str, trace_id: str | None = None) -> bytes:
    """Convert video using bounded size-reduction attempts toward a 1 MB GIF."""
    best_candidate: bytes | None = None
    deadline = time.monotonic() + VIDEO_CONVERSION_TIMEOUT
    for max_dimension, fps, max_frames in (
        (VIDEO_MAX_DIMENSION, 15, MAX_VIDEO_FRAMES),
        (384, 12, 120),
        (320, 10, 100),
        (240, 8, 80),
        (160, 6, 60),
    ):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            candidate = _convert_video_once(
                data, suffix, trace_id,
                max_dimension=max_dimension,
                fps=fps,
                max_frames=max_frames,
                timeout=remaining,
            )
        except _CandidateTooLarge:
            continue
        except InvalidImage:
            if best_candidate is not None:
                break
            raise
        if best_candidate is None or len(candidate) < len(best_candidate):
            best_candidate = candidate
        if len(candidate) <= GIF_TARGET_BYTES:
            return candidate
    if best_candidate is not None:
        return best_candidate
    if time.monotonic() >= deadline:
        raise InvalidImage("Video conversion took too long. Try a shorter or smaller video.")
    raise InvalidImage("The converted GIF is too large to upload (maximum 25 MB).")


def _convert_video_once(
    data: bytes,
    suffix: str,
    trace_id: str | None = None,
    *,
    max_dimension: int = VIDEO_MAX_DIMENSION,
    fps: int = 15,
    max_frames: int = MAX_VIDEO_FRAMES,
    timeout: float = VIDEO_CONVERSION_TIMEOUT,
) -> bytes:
    """Convert one MP4/WebM candidate into a bounded GIF."""
    if suffix not in {".mp4", ".webm"}:
        raise InvalidImage("Use an MP4 or WebM video.")
    if len(data) > MAX_INPUT_BYTES:
        raise InvalidImage("That video is too large. The maximum upload size is 15 MB.")
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise InvalidImage("Video conversion requires FFmpeg. Ask the bot host to install it and restart Bombagif.")

    filter_graph = (
        f"[0:V:0]fps={fps},scale={max_dimension}:{max_dimension}:"
        "force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
        "split[s0][s1];[s0]palettegen=stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=bayer[out]"
    )
    try:
        with tempfile.TemporaryDirectory(prefix="bombagif-video-") as work_dir:
            input_path = Path(work_dir) / f"input{suffix}"
            output_path = Path(work_dir) / "output.gif"
            input_path.write_bytes(data)
            stderr_path = Path(work_dir) / "ffmpeg.stderr"
            command = [
                ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-threads",
                "1",
                "-filter_complex_threads",
                "1",
                "-autorotate",
                "-max_pixels",
                str(MAX_PIXELS),
                "-protocol_whitelist",
                "file",
                "-t",
                str(MAX_VIDEO_DURATION),
                "-i",
                str(input_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[out]",
                "-an",
                "-sn",
                "-dn",
                "-frames:v",
                str(max_frames),
                "-map_metadata",
                "-1",
                "-loop",
                "0",
                "-fs",
                str(MAX_OUTPUT_BYTES),
                str(output_path),
            ]
            try:
                with stderr_path.open("wb") as stderr_file:
                    result = subprocess.run(
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_file,
                        check=False,
                        shell=False,
                        timeout=timeout,
                        cwd=work_dir,
                    )
            except subprocess.TimeoutExpired as exc:
                raise InvalidImage("Video conversion took too long. Try a shorter or smaller video.") from exc
            except OSError as exc:
                raise InvalidImage("FFmpeg could not start video conversion on this host.") from exc
            output_size = output_path.stat().st_size if output_path.is_file() else 0
            if output_size >= MAX_OUTPUT_BYTES and result.returncode != 0:
                raise _CandidateTooLarge("The GIF candidate reached the 25 MB output limit.")
            if output_size > MAX_OUTPUT_BYTES:
                raise _CandidateTooLarge("The converted GIF is too large to upload (maximum 25 MB).")
            if result.returncode != 0:
                with stderr_path.open("rb") as stderr_file:
                    stderr_size = stderr_file.seek(0, os.SEEK_END)
                    stderr_file.seek(max(0, stderr_size - 4096))
                    diagnostic = stderr_file.read(4096).decode("utf-8", errors="replace")
                diagnostic = " ".join(
                    "".join(char if char.isprintable() else " " for char in diagnostic).split()
                )[:1000]
                logging.getLogger("bombagif.media").warning(
                    "FFmpeg video conversion failed (returncode=%s)%s",
                    result.returncode,
                    f": {diagnostic}" if diagnostic else "",
                    extra={"trace_id": trace_id} if trace_id else {},
                )
                raise InvalidImage(
                    "That MP4/WebM could not be converted. Please try another video. "
                    "The bot host can inspect the service journal for the FFmpeg error."
                )
            if output_size == 0:
                raise InvalidImage("That video does not contain a usable video stream.")
            with output_path.open("rb") as output_file:
                signature = output_file.read(6)
                output_file.seek(-1, os.SEEK_END)
                trailer = output_file.read(1)
                if signature not in {b"GIF87a", b"GIF89a"} or trailer != b";":
                    if output_size >= MAX_OUTPUT_BYTES:
                        raise _CandidateTooLarge("The GIF candidate reached the 25 MB output limit.")
                    raise InvalidImage("FFmpeg did not produce a complete GIF for that video.")
                output_file.seek(0)
                return output_file.read(MAX_OUTPUT_BYTES)
    except InvalidImage:
        raise
    except OSError as exc:
        raise InvalidImage("Video conversion could not access temporary storage on this host.") from exc


def _is_avif_container(data: bytes) -> bool:
    """Check the ISO-BMFF ftyp box brands before handing a file to AVIF fallback."""
    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    brands = [data[8:12]]
    brands.extend(data[offset : offset + 4] for offset in range(16, min(len(data), 64), 4))
    return any(brand in {b"avif", b"avis"} for brand in brands)


def _decode_avif_with_ffmpeg(data: bytes) -> bytes | None:
    """Decode an AVIF to a bounded first-frame PNG when Pillow lacks its AV1 codec."""
    if not _is_avif_container(data):
        return None
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="bombagif-avif-") as work_dir:
            input_path = Path(work_dir) / "input.avif"
            output_path = Path(work_dir) / "frame.png"
            input_path.write_bytes(data)
            command = [
                ffmpeg,
                "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-threads", "1", "-max_pixels", str(MAX_PIXELS),
                "-protocol_whitelist", "file", "-i", str(input_path),
                "-frames:v", "1", "-map_metadata", "-1",
                "-f", "image2", "-fs", str(MAX_INPUT_BYTES), str(output_path),
            ]
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                shell=False,
                timeout=15,
                cwd=work_dir,
            )
            if result.returncode != 0 or not output_path.is_file():
                return None
            if output_path.stat().st_size > MAX_INPUT_BYTES:
                return None
            png = output_path.read_bytes()
            if not png.startswith(b"\x89PNG\r\n\x1a\n"):
                return None
            return png
    except (OSError, subprocess.TimeoutExpired):
        return None


def _convert_image_to_target(
    data: bytes, suffix: str, background: tuple[int, int, int]
) -> bytes:
    """Try bounded downscale/frame-reduction steps for a 1 MB GIF target."""
    if len(data) > MAX_INPUT_BYTES:
        raise InvalidImage("That image is too large. The maximum upload size is 15 MB.")
    if suffix == ".avif" and not features.check("avif"):
        decoded = _decode_avif_with_ffmpeg(data)
        if decoded is not None:
            data, suffix = decoded, ".png"
    best_candidate: bytes | None = None
    attempts = (
        (MAX_DIMENSION, MAX_FRAMES, 20),
        (768, 75, 40),
        (512, 50, 67),
        (384, 30, 100),
        (256, 15, 150),
    )
    for max_dimension, max_frames, duration_floor_ms in attempts:
        try:
            candidate = _convert_image_once(
                data,
                suffix,
                background,
                max_dimension=max_dimension,
                max_frames=max_frames,
                duration_floor_ms=duration_floor_ms,
            )
        except _CandidateTooLarge:
            continue
        except InvalidImage:
            if best_candidate is not None:
                break
            raise
        if best_candidate is None or len(candidate) < len(best_candidate):
            best_candidate = candidate
        if len(candidate) <= GIF_TARGET_BYTES:
            return candidate
    if best_candidate is None:
        raise InvalidImage("The converted GIF is too large to upload (maximum 25 MB).")
    return best_candidate


def convert_image(data: bytes, suffix: str, background: tuple[int, int, int]) -> bytes:
    """Convert an image toward the 1 MB target, preserving a bounded fallback."""
    return _convert_image_to_target(data, suffix, background)


def _convert_image_once(
    data: bytes,
    suffix: str,
    background: tuple[int, int, int],
    *,
    max_dimension: int,
    max_frames: int,
    duration_floor_ms: int,
) -> bytes:
    """Encode one image candidate under progressively smaller quality settings."""
    if len(data) > MAX_INPUT_BYTES:
        raise InvalidImage("That image is too large. The maximum upload size is 15 MB.")
    if suffix == ".svg":
        width, height = _check_svg(data)
        data = _rasterize_svg(data, width, height)

    try:
        with Image.open(io.BytesIO(data)) as image:
            expected_format = {
                ".png": "PNG", ".webp": "WEBP", ".svg": "PNG",
                ".jpg": "JPEG", ".jpeg": "JPEG", ".avif": "AVIF",
            }[suffix]
            if image.format != expected_format:
                raise InvalidImage("The image contents do not match the file extension.")
            if image.width <= 0 or image.height <= 0 or image.width * image.height > MAX_PIXELS:
                raise InvalidImage("That image has dimensions that are too large to process safely.")
            avif_fallback: Image.Image | None = None
            if suffix == ".avif" and features.check("avif"):
                try:
                    image.load()
                except (RuntimeError, OSError):
                    png_data = _decode_avif_with_ffmpeg(data)
                    if png_data is None:
                        raise InvalidImage("AVIF decoder unavailable; enable AVIF codecs in Pillow or install FFmpeg.")
                    with Image.open(io.BytesIO(png_data)) as decoded_image:
                        avif_fallback = decoded_image.copy()
            if avif_fallback is not None and (
                avif_fallback.width <= 0
                or avif_fallback.height <= 0
                or avif_fallback.width * avif_fallback.height > MAX_PIXELS
            ):
                raise InvalidImage("That AVIF has dimensions that are too large to process safely.")
            frames: list[Image.Image] = []
            durations: list[int] = []
            source_width, source_height = (
                avif_fallback.size if avif_fallback is not None else image.size
            )
            pixel_count = source_width * source_height
            output_pixels = min(source_width, max_dimension) * min(source_height, max_dimension)
            frame_count = min(
                getattr(image, "n_frames", 1),
                max_frames,
                max(1, MAX_TOTAL_FRAME_PIXELS // pixel_count),
                max(1, MAX_TOTAL_FRAME_PIXELS // output_pixels),
            )
            for frame_index in range(frame_count):
                if avif_fallback is None:
                    image.seek(frame_index)
                    frame = ImageOps.exif_transpose(image.copy())
                else:
                    frame = avif_fallback.copy()
                frame.thumbnail((max_dimension, max_dimension), Image.Resampling.LANCZOS)
                rgba = frame.convert("RGBA")
                flattened = Image.new("RGB", rgba.size, background)
                flattened.paste(rgba, mask=rgba.getchannel("A"))
                frames.append(flattened.quantize(colors=256, method=Image.Quantize.MEDIANCUT))
                frame_duration = 100
                if avif_fallback is None:
                    frame_duration = int(image.info.get("duration", 100))
                durations.append(min(60_000, max(duration_floor_ms, frame_duration)))
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
    except (InvalidImage, UnidentifiedImageError, OSError, ValueError, EOFError, SyntaxError, RuntimeError, Image.DecompressionBombError) as exc:
        if isinstance(exc, InvalidImage):
            raise
        raise InvalidImage("That image could not be decoded. Please try another file.") from exc


class MediaLike(Protocol):
    """Anything with attachment-like fields (discord.Attachment, MediaSource, test fakes)."""

    filename: str
    url: str
    size: int | None
    content_type: str | None


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated runtime configuration loaded from the environment."""

    discord_token: str
    zipline_token: str
    zipline_url: str
    log_level: str
    allowed_guilds: frozenset[int]
    background: tuple[int, int, int]


async def read_bounded(response: aiohttp.ClientResponse, limit: int) -> bytes:
    """Read an HTTP response body with an enforced streaming byte limit."""
    chunks = bytearray()
    async for chunk in response.content.iter_chunked(64 * 1024):
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise UserFacingError("That file is too large. The maximum upload size is 15 MB.")
    return bytes(chunks)


async def download_attachment(session: aiohttp.ClientSession, attachment: MediaLike) -> bytes:
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


def read_settings(token_env: str = "DISCORD_TOKEN") -> Settings:
    """Read required credentials and validate the optional runtime settings.

    ``token_env`` names the Discord login token variable: ``DISCORD_TOKEN`` for the app,
    ``SELFBOT_TOKEN`` for the self-bot. Neither program requires the other's token.
    """
    discord_token = os.getenv(token_env, "").strip()
    zipline_token = os.getenv("ZIPLINE_TOKEN", "").strip()
    if not discord_token or not zipline_token:
        raise RuntimeError(f"{token_env} and ZIPLINE_TOKEN must be configured")

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


@dataclass(frozen=True, slots=True)
class MediaSource:
    """A downloadable media item that is not necessarily a Discord attachment."""

    filename: str
    url: str
    size: int | None = None
    content_type: str | None = None


async def convert_and_upload(
    session: aiohttp.ClientSession,
    settings: Settings,
    executor: ThreadPoolExecutor,
    source: MediaLike,
    *,
    trace_id: str,
    log_context: dict[str, Any] | None = None,
) -> str:
    """Validate, download, convert and upload one media item, returning its GIF link."""
    logger = logging.getLogger("bombagif")
    context = {"trace_id": trace_id, **(log_context or {})}
    suffix = validate_attachment_metadata(source.filename, source.content_type, source.size)
    logger.info("Processing attachment", extra={**context, "attachment_name": source.filename})
    data = await download_attachment(session, source)
    try:
        loop = asyncio.get_running_loop()
        if suffix in {".mp4", ".webm"}:
            gif_data = await loop.run_in_executor(executor, convert_video, data, suffix, trace_id)
        else:
            gif_data = await loop.run_in_executor(
                executor, _convert_image_to_target, data, suffix, settings.background
            )
    except InvalidImage:
        raise
    except Exception as exc:
        logger.exception("Media conversion failed", extra=context)
        raise InvalidImage("That file could not be converted. Please try another image or video.") from exc
    safe_stem = re.sub(
        r"[^A-Za-z0-9_-]+",
        "-",
        os.path.splitext(os.path.basename(source.filename))[0],
    ).strip("-_")[:64] or "media"
    return await upload_to_zipline(session, settings, gif_data, f"{safe_stem}.gif", trace_id=trace_id)


_DISCORD_MEDIA_DOMAINS: Final[tuple[str, ...]] = ("discordapp.net", "discordapp.com")


def is_discord_media_url(url: str) -> bool:
    """True only for https URLs on Discord's own CDN/proxy hosts, with no credentials or odd port.

    Embed media is fetched through Discord's proxy URLs, never the embed's original host, so a
    message can't make the bot request arbitrary (or internal) addresses.
    """
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None:
        return False
    if port not in (None, 443):
        return False
    host = host.lower()
    return any(host == domain or host.endswith("." + domain) for domain in _DISCORD_MEDIA_DOMAINS)


def sniff_suffix(data: bytes) -> str | None:
    """Guess a supported file extension from magic bytes, or None if unrecognized."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if _is_avif_container(data):
        return ".avif"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        return ".mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    return None

