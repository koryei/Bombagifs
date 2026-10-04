"""Focused behavior tests for Bombagif's image and Zipline paths."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import aiohttp
import discord
from aiohttp import web
from PIL import Image, features

from main import (
    InvalidImage,
    PublicGifView,
    Settings,
    UploadFailure,
    UserFacingError,
    MAX_INPUT_BYTES,
    MAX_OUTPUT_BYTES,
    MAX_GIF_BATCH,
    GIF_TARGET_BYTES,
    PRESENCE_REASSERT_TICKS,
    _CandidateTooLarge,
    _convert_image_to_target,
    _extract_zipline_file_url,
    _presence_signature,
    _read_settings,
    _rasterize_svg,
    create_bot,
    convert_image,
    convert_video,
    load_presence_config,
    load_presence_or_default,
    upload_to_zipline,
    validate_image_metadata,
)


class ConfigurationTests(unittest.TestCase):
    """Check self-hosting requires the operator's own Zipline configuration."""

    def test_requires_own_zipline_url_and_secret(self) -> None:
        """Fail closed rather than falling back to a maintainer endpoint."""
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "operator-discord-secret",
                "ZIPLINE_TOKEN": "operator-zipline-secret",
                "ZIPLINE_URL": "",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "your own Zipline instance URL"):
                _read_settings()

    def test_reads_operator_supplied_https_zipline_url(self) -> None:
        """Use only the Zipline URL configured by the self-hoster."""
        env = {
            "DISCORD_TOKEN": "operator-discord-secret",
            "ZIPLINE_TOKEN": "operator-zipline-secret",
            "ZIPLINE_URL": "https://zipline.operator.example",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = _read_settings()
        self.assertEqual(settings.zipline_url, "https://zipline.operator.example")
        self.assertEqual(settings.zipline_token, "operator-zipline-secret")

    def test_rejects_insecure_zipline_url(self) -> None:
        """Require HTTPS before sending the operator token to Zipline."""
        env = {
            "DISCORD_TOKEN": "operator-discord-secret",
            "ZIPLINE_TOKEN": "operator-zipline-secret",
            "ZIPLINE_URL": "http://zipline.operator.example",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "HTTPS base URL"):
                _read_settings()

    def test_rejects_credentials_embedded_in_zipline_url(self) -> None:
        """Keep credentials in environment secret fields, not URL/log strings."""
        env = {
            "DISCORD_TOKEN": "operator-discord-secret",
            "ZIPLINE_TOKEN": "operator-zipline-secret",
            "ZIPLINE_URL": "https://user:secret@zipline.operator.example",
        }
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "without embedded credentials"):
                _read_settings()


class ImageTests(unittest.TestCase):
    """Exercise offline validation and Pillow conversion behavior."""

    def test_rejects_extension_content_mismatch(self) -> None:
        """Reject image bytes that do not match the validated extension."""
        source = Image.new("RGB", (1, 1), (0, 0, 0))
        encoded = io.BytesIO()
        source.save(encoded, format="PNG")
        with self.assertRaisesRegex(InvalidImage, "contents do not match"):
            convert_image(encoded.getvalue(), ".webp", (255, 255, 255))

    def test_converts_jpg_jpeg_and_avif_images(self) -> None:
        """Decode new raster formats and emit valid GIFs."""
        jpeg = io.BytesIO()
        Image.new("RGB", (40, 25), (90, 140, 210)).save(jpeg, format="JPEG")
        for suffix in (".jpg", ".jpeg"):
            with self.subTest(suffix=suffix):
                converted = convert_image(jpeg.getvalue(), suffix, (255, 255, 255))
                with Image.open(io.BytesIO(converted)) as gif:
                    self.assertEqual(gif.format, "GIF")
                    self.assertEqual(gif.size, (40, 25))

    def test_converts_avif_when_supported(self) -> None:
        """Decode a real AVIF fixture if Pillow and the host encoder support it."""
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            self.skipTest("FFmpeg is required to generate the AVIF test fixture")
        with tempfile.TemporaryDirectory(prefix="bombagif-avif-test-") as work_dir:
            input_path = Path(work_dir) / "fixture.avif"
            generated = subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=green:s=40x24:d=0.1",
                    "-frames:v",
                    "1",
                    "-c:v",
                    "libsvtav1",
                    "-preset",
                    "12",
                    "-crf",
                    "45",
                    "-y",
                    str(input_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            if generated.returncode != 0:
                self.skipTest("FFmpeg lacks an AVIF-capable encoder")
            converted = convert_image(input_path.read_bytes(), ".avif", (255, 255, 255))
        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertLessEqual(gif.width, 40)
            self.assertLessEqual(gif.height, 40)

    def test_rejects_png_content_named_as_jpeg_or_avif(self) -> None:
        """Keep extension/content validation strict for the newly allowed suffixes."""
        png = io.BytesIO()
        Image.new("RGB", (2, 2), (0, 0, 0)).save(png, format="PNG")
        for suffix in (".jpg", ".jpeg"):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(InvalidImage, "contents do not match"):
                convert_image(png.getvalue(), suffix, (255, 255, 255))
        if features.check("avif"):
            with self.assertRaises(InvalidImage):
                convert_image(png.getvalue(), ".avif", (255, 255, 255))

    def test_validates_suffix_mime_and_size(self) -> None:
        """Accept a valid image and reject mismatches and oversized inputs."""
        self.assertEqual(validate_image_metadata("input.PNG", "image/png", 20), ".png")
        self.assertEqual(validate_image_metadata("clip.MP4", "video/mp4", 20), ".mp4")
        self.assertEqual(validate_image_metadata("clip.webm", "video/webm", 20), ".webm")
        # Discord's content_type can be absent or incorrectly generic for uploads.
        self.assertEqual(validate_image_metadata("input.png", None, 20), ".png")
        self.assertEqual(validate_image_metadata("input.png", "application/octet-stream", 20), ".png")
        self.assertEqual(validate_image_metadata("input.jpg", "image/jpeg", 20), ".jpg")
        self.assertEqual(validate_image_metadata("input.jpeg", "image/jpeg", 20), ".jpeg")
        self.assertEqual(validate_image_metadata("input.avif", "image/avif", 20), ".avif")
        with self.assertRaisesRegex(UserFacingError, "too large"):
            validate_image_metadata("large.png", "image/png", 15 * 1024 * 1024 + 1)
        with self.assertRaisesRegex(UserFacingError, "too large"):
            validate_image_metadata("large.mp4", "video/mp4", 15 * 1024 * 1024 + 1)

    def test_still_image_output_uses_target_or_smallest_fallback(self) -> None:
        """Produce valid GIFs and prefer a candidate under the 1 MB soft target."""
        source = Image.new("RGB", (64, 48), (120, 80, 200))
        png = io.BytesIO()
        source.save(png, format="PNG")
        converted = convert_image(png.getvalue(), ".png", (255, 255, 255))
        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
        self.assertLessEqual(len(converted), MAX_OUTPUT_BYTES)
        self.assertLessEqual(len(converted), GIF_TARGET_BYTES)

    def test_image_attempts_fall_back_to_smallest_successful_candidate(self) -> None:
        """Continue after a candidate exceeds the hard cap and keep the smallest."""
        with patch(
            "main._convert_image_once",
            side_effect=[b"a" * (GIF_TARGET_BYTES + 1), _CandidateTooLarge("too large"), b"b" * 900_000],
        ) as convert_once:
            candidate = _convert_image_to_target(b"source", ".png", (255, 255, 255))
        self.assertEqual(candidate, b"b" * 900_000)
        self.assertEqual(convert_once.call_count, 3)

    def test_image_uses_smallest_candidate_when_target_is_unreachable(self) -> None:
        """Return the smallest bounded candidate when all successful outputs exceed 1 MB."""
        candidates = [
            b"a" * 1_400_000,
            b"b" * 1_200_000,
            b"c" * 1_300_000,
            b"d" * 1_500_000,
            b"e" * 1_600_000,
        ]
        with patch("main._convert_image_once", side_effect=candidates):
            self.assertEqual(
                _convert_image_to_target(b"source", ".png", (255, 255, 255)), candidates[1]
            )

    def test_video_uses_smallest_candidate_when_target_is_unreachable(self) -> None:
        """Try each bounded video profile and return the smallest valid GIF candidate."""
        candidates = [
            b"a" * 1_400_000,
            b"b" * 1_200_000,
            b"c" * 1_300_000,
            b"d" * 1_500_000,
            b"e" * 1_600_000,
        ]
        with patch("main._convert_video_once", side_effect=candidates) as convert_once:
            result = convert_video(b"source", ".mp4")
        self.assertEqual(result, candidates[1])
        self.assertEqual(convert_once.call_count, 5)

    def test_video_conversion_uses_bounded_ffmpeg_pipeline_and_cleans_tempfiles(self) -> None:
        """Trim and optimize video with a shell-free FFmpeg command and clean temp files."""
        workdirs: list[Path] = []
        commands: list[list[str]] = []

        def fake_ffmpeg(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            input_path = Path(command[command.index("-i") + 1])
            output_path = Path(command[-1])
            workdirs.append(input_path.parent)
            commands.append(command)
            self.assertEqual(input_path.read_bytes(), b"video bytes")
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIsNot(kwargs["stderr"], subprocess.DEVNULL)
            self.assertFalse(kwargs["shell"])
            output_path.write_bytes(b"GIF89a" + b"frame" + b";")
            return subprocess.CompletedProcess(command, 0)

        for suffix in (".mp4", ".webm"):
            with self.subTest(suffix=suffix):
                with patch("main.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                    "main.subprocess.run", side_effect=fake_ffmpeg
                ) as run:
                    converted = convert_video(b"video bytes", suffix)
                self.assertEqual(converted, b"GIF89aframe;")
                run.assert_called_once()
                self.assertTrue(run.call_args.kwargs["stdin"] is subprocess.DEVNULL)
                self.assertFalse(run.call_args.kwargs["shell"])
                self.assertGreater(run.call_args.kwargs["timeout"], 0)
                self.assertLessEqual(run.call_args.kwargs["timeout"], 45)
                self.assertFalse(workdirs[-1].exists())

        for command in commands:
            self.assertNotIn("shell", command)
            self.assertEqual(command[command.index("-t") + 1], "10")
            self.assertEqual(command[command.index("-max_pixels") + 1], str(20_000_000))
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")
            self.assertNotIn("-f", command)  # let FFmpeg detect MP4/MOV and Matroska/WebM variants
            self.assertIn("fps=", command[command.index("-filter_complex") + 1])
            self.assertIn("palettegen", command[command.index("-filter_complex") + 1])
            self.assertIn("scale=", command[command.index("-filter_complex") + 1])
            self.assertIn("-an", command)
            self.assertIn("-autorotate", command)
            self.assertIn("-frames:v", command)
            self.assertEqual(command[command.index("-loop") + 1], "0")
            self.assertEqual(command[command.index("-fs") + 1], str(MAX_OUTPUT_BYTES))

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_converts_mp4_with_default_moov_atom_at_end(self) -> None:
        """Convert a generated MP4 in the common non-faststart layout end-to-end."""
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        with tempfile.TemporaryDirectory(prefix="bombagif-mp4-test-") as work_dir:
            input_path = Path(work_dir) / "fixture.mp4"
            generated = subprocess.run(
                [
                    str(ffmpeg),
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=red:s=160x120:r=15:d=1.2",
                    "-an",
                    "-c:v",
                    "mpeg4",
                    "-pix_fmt",
                    "yuv420p",
                    str(input_path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode(errors="replace"))
            converted = convert_video(input_path.read_bytes(), ".mp4")

        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertLessEqual(gif.width, 480)
            self.assertLessEqual(gif.height, 480)
            self.assertGreater(gif.n_frames, 1)
            self.assertLessEqual(gif.n_frames, 150)
            self.assertLessEqual(len(converted), MAX_OUTPUT_BYTES)

    def test_video_conversion_reports_missing_ffmpeg_and_invalid_outputs(self) -> None:
        """Report missing FFmpeg, timeout, failed decodes, and oversized output clearly."""
        with patch("main.shutil.which", return_value=None):
            with self.assertRaisesRegex(InvalidImage, "requires FFmpeg"):
                convert_video(b"video bytes", ".mp4")

        with self.assertRaisesRegex(InvalidImage, "MP4 or WebM"):
            convert_video(b"video bytes", ".mov")
        with self.assertRaisesRegex(InvalidImage, "maximum upload size is 15 MB"):
            convert_video(b"v" * (MAX_INPUT_BYTES + 1), ".mp4")

        timed_out_workdirs: list[Path] = []

        def time_out(command: list[str], **kwargs: object) -> object:
            timed_out_workdirs.append(Path(str(kwargs["cwd"])))
            raise subprocess.TimeoutExpired(command, 45)

        with patch("main.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "main.subprocess.run", side_effect=time_out
        ):
            with self.assertRaisesRegex(InvalidImage, "took too long"):
                convert_video(b"video bytes", ".mp4")
        self.assertFalse(timed_out_workdirs[-1].exists())

        failed_workdirs: list[Path] = []

        def failed_ffmpeg(command: list[str], **kwargs: object) -> object:
            failed_workdirs.append(Path(str(kwargs["cwd"])))
            stderr_file = kwargs["stderr"]
            self.assertNotEqual(stderr_file, subprocess.DEVNULL)
            stderr_file.write(b"[mov,mp4] moov atom not found")
            return subprocess.CompletedProcess(command, 1)

        with self.assertLogs("bombagif.media", level="WARNING") as logs:
            with patch("main.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                "main.subprocess.run", side_effect=failed_ffmpeg
            ):
                with self.assertRaisesRegex(InvalidImage, "service journal"):
                    convert_video(b"video bytes", ".mp4", "test-trace")
        self.assertIn("moov atom not found", "\n".join(logs.output))
        self.assertEqual(logs.records[0].trace_id, "test-trace")
        self.assertFalse(failed_workdirs[-1].exists())

        oversized_workdirs: list[Path] = []
        oversized_attempts = 0

        def oversized_output(
            command: list[str], **kwargs: object
        ) -> subprocess.CompletedProcess[bytes]:
            nonlocal oversized_attempts
            output_path = Path(command[-1])
            oversized_workdirs.append(output_path.parent)
            if oversized_attempts == 0:
                with output_path.open("wb") as output:
                    output.truncate(MAX_OUTPUT_BYTES)
                oversized_attempts += 1
                return subprocess.CompletedProcess(command, 1)
            output_path.write_bytes(b"GIF89aframe;")
            oversized_attempts += 1
            return subprocess.CompletedProcess(command, 0)

        with patch("main.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
            "main.subprocess.run", side_effect=oversized_output
        ):
            converted = convert_video(b"video bytes", ".mp4")
        self.assertEqual(converted, b"GIF89aframe;")
        self.assertEqual(oversized_attempts, 2)
        self.assertFalse(any(workdir.exists() for workdir in oversized_workdirs))

    def test_converts_png_and_flattens_transparency(self) -> None:
        """Produce a readable GIF with configured background for transparent pixels."""
        source = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
        source.putpixel((0, 0), (255, 0, 0, 255))
        encoded = io.BytesIO()
        source.save(encoded, format="PNG")

        converted = convert_image(encoded.getvalue(), ".png", (255, 255, 255))
        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertEqual(gif.size, (2, 1))
            gif_rgb = gif.convert("RGB")
            self.assertEqual(gif_rgb.getpixel((0, 0)), (255, 0, 0))
            self.assertEqual(gif_rgb.getpixel((1, 0)), (255, 255, 255))

    def test_preserves_webp_animation(self) -> None:
        """Preserve a two-frame WEBP animation in GIF output."""
        first = Image.new("RGBA", (3, 2), (255, 0, 0, 255))
        second = Image.new("RGBA", (3, 2), (0, 0, 255, 255))
        encoded = io.BytesIO()
        first.save(
            encoded,
            format="WEBP",
            save_all=True,
            append_images=[second],
            duration=[100, 150],
            loop=0,
        )
        converted = convert_image(encoded.getvalue(), ".webp", (255, 255, 255))
        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertEqual(gif.n_frames, 2)

    def test_missing_cairo_reports_macos_install_hint(self) -> None:
        """Explain the native Cairo prerequisite without blocking bot startup."""
        with patch("main.importlib.import_module", side_effect=OSError("libcairo missing")):
            with self.assertRaisesRegex(InvalidImage, "brew install cairo"):
                _rasterize_svg(b"<svg/>", 1, 1)

    def test_rasterizes_safe_svg_and_rejects_external_reference(self) -> None:
        """Rasterize a self-contained SVG and reject resource-bearing SVG input."""
        safe = b'<svg xmlns="http://www.w3.org/2000/svg" width="4" height="3"><rect width="4" height="3" fill="red"/></svg>'
        converted = convert_image(safe, ".svg", (255, 255, 255))
        with Image.open(io.BytesIO(converted)) as gif:
            self.assertEqual(gif.format, "GIF")
            self.assertEqual(gif.size, (4, 3))

        unsafe_svgs = (
            b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x.png"/></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import url(https://example.com/x.css);</style></svg>',
            b'<svg xmlns="http://www.w3.org/2000/svg"><text>url(https://evil.test)</text></svg>',
        )
        for unsafe in unsafe_svgs:
            with self.subTest(svg=unsafe):
                with self.assertRaises(InvalidImage):
                    convert_image(unsafe, ".svg", (255, 255, 255))


class PresenceConfigTests(unittest.TestCase):
    """Validate editable Discord status and activity settings."""

    def test_loads_checked_in_status_card(self) -> None:
        """Read the checked-in card: title, both lines, and its button."""
        from main import PRESENCE_CONFIG_PATH

        status, activity = load_presence_config(PRESENCE_CONFIG_PATH)
        self.assertEqual(status, discord.Status.online)
        self.assertIsInstance(activity, discord.Activity)
        assert isinstance(activity, discord.Activity)
        self.assertEqual(activity.name, "Convert video & images to optimized GIFs")
        self.assertEqual(activity.type, discord.ActivityType.playing)
        self.assertIsNone(activity.details)
        self.assertEqual(activity.state, "Run /gif in DMs, groups, or servers")
        self.assertEqual(activity.assets, {})
        self.assertEqual(activity.buttons, [])

    def test_resolves_user_install_button_url(self) -> None:
        """Point the card's button at this app's own User-Install URL."""
        config = (
            "[status]\nactivity_name = Bombagif\nstate = Install once\n"
            "button_label = Try It Out\nbutton_url = user-install\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "status.config"
            config_path.write_text(config, encoding="utf-8")
            _, resolved = load_presence_config(config_path, 123456789)
            _, pending = load_presence_config(config_path)
        assert isinstance(resolved, discord.Activity)
        assert isinstance(pending, discord.Activity)
        self.assertEqual(
            resolved.state_url,
            "https://discord.com/oauth2/authorize?client_id=123456789"
            "&scope=applications.commands&integration_type=1",
        )
        self.assertIn("integration_type=1", resolved.state_url)
        self.assertEqual(resolved.buttons, ["Try It Out"])
        # Without a known application id the button waits for the next READY.
        self.assertIsNone(pending.state_url)

    def test_builds_card_images_from_config(self) -> None:
        """Expose the Rich Presence assets shown on the card."""
        config = (
            "[status]\nactivity_name = Bombagif\n"
            "large_image = bombagif_logo\nlarge_text = Bombagif\n"
            "small_image = bombagif_badge\nsmall_text = Bombagif\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "status.config"
            config_path.write_text(config, encoding="utf-8")
            _, activity = load_presence_config(config_path)
        assert isinstance(activity, discord.Activity)
        self.assertEqual(
            activity.assets,
            {
                "large_image": "bombagif_logo",
                "large_text": "Bombagif",
                "small_image": "bombagif_badge",
                "small_text": "Bombagif",
            },
        )
        self.assertEqual(activity.to_dict()["assets"], activity.assets)

    def test_plain_activity_text_stays_a_single_line(self) -> None:
        """Keep the minimal one-line presence free of rich activity fields."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "status.config"
            config_path.write_text(
                "[status]\nstatus = idle\nactivity_type = watching\nactivity_text = GIFs\n",
                encoding="utf-8",
            )
            status, activity = load_presence_config(config_path)
        self.assertEqual(status, discord.Status.idle)
        assert activity is not None
        self.assertEqual(activity.name, "GIFs")
        self.assertEqual(activity.type, discord.ActivityType.watching)
        payload = activity.to_dict()
        self.assertNotIn("details", payload)
        self.assertNotIn("state", payload)
        self.assertEqual(payload["buttons"], [])

    def test_supports_custom_activity_modes(self) -> None:
        """Create listening, watching, streaming, and custom presences from config."""
        cases = (
            ("listening", "music", "", discord.ActivityType.listening),
            ("watching", "GIFs", "", discord.ActivityType.watching),
            ("competing", "a GIF challenge", "", discord.ActivityType.competing),
            ("streaming", "a live GIF build", "streaming_url = https://twitch.tv/example", None),
            ("custom", "ready for GIFs", "activity_emoji = ✨", None),
        )
        for activity_type, text, extra, expected_type in cases:
            with self.subTest(activity_type=activity_type):
                config = f"[status]\nstatus = dnd\nactivity_type = {activity_type}\nactivity_text = {text}\n{extra}"
                with tempfile.TemporaryDirectory() as directory:
                    config_path = Path(directory) / "status.config"
                    config_path.write_text(config, encoding="utf-8")
                    status, activity = load_presence_config(config_path)
                self.assertEqual(status, discord.Status.dnd)
                self.assertIsNotNone(activity)
                assert activity is not None
                if expected_type is not None:
                    self.assertEqual(activity.type, expected_type)
                self.assertEqual(activity.name, text)
                if activity_type == "custom":
                    self.assertEqual(activity.emoji.name, "✨")

    def test_blank_activity_disables_activity(self) -> None:
        """Allow operators to leave only a selected online/offline status."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "status.config"
            config_path.write_text("[status]\nstatus = idle\nactivity_text = \n", encoding="utf-8")
            status, activity = load_presence_config(config_path)
        self.assertEqual(status, discord.Status.idle)
        self.assertIsNone(activity)

    def test_broken_config_keeps_bot_online(self) -> None:
        """Never let a half-edited status.config hide the bot's presence."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "status.config"
            config_path.write_text("[status]\nstatus = bogus\n", encoding="utf-8")
            with patch("main.PRESENCE_CONFIG_PATH", config_path):
                status, activity = load_presence_or_default()
        self.assertEqual(status, discord.Status.online)
        self.assertIsNone(activity)

    def test_rejects_invalid_presence_values(self) -> None:
        """Fail fast on invalid status and unsupported activity values."""
        invalid_values = (
            "[status]\nstatus = bogus\n",
            "[status]\nactivity_type = unknown\n",
            "[status]\nactivity_type = streaming\nactivity_text = live\n",
            f"[status]\ndetails = {'x' * 129}\n",
            f"[status]\nactivity_name = Bombagif\nbutton_label = {'y' * 33}\n",
            f"[status]\nactivity_name = Bombagif\nlarge_image = {'z' * 33}\n",
            "[status]\nactivity_type = custom\nactivity_name = Bombagif\nlarge_image = logo\n",
            "[status]\nactivity_name = Bombagif\nbutton_label = Try It Out\nbutton_url = http://insecure.example\n",
            "[status]\nactivity_type = custom\nactivity_name = Bombagif\ndetails = one line\n",
        )
        for config in invalid_values:
            with self.subTest(config=config):
                with tempfile.TemporaryDirectory() as directory:
                    config_path = Path(directory) / "status.config"
                    config_path.write_text(config, encoding="utf-8")
                    with self.assertRaises(RuntimeError):
                        load_presence_config(config_path)


class DiscordCommandTests(unittest.IsolatedAsyncioTestCase):
    """Verify the command is exposed as a personal user-installed app."""

    async def test_bot_announces_presence_in_initial_connection(self) -> None:
        """Send the configured presence with the first IDENTIFY payload."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            expected_status, expected_activity = load_presence_or_default()
            self.assertEqual(bot.presence_status, expected_status)
            self.assertEqual(
                bot.presence_activity.name if bot.presence_activity else None,
                expected_activity.name if expected_activity else None,
            )
        finally:
            await bot.close()

    async def test_bot_reapplies_presence_after_gateway_resume(self) -> None:
        """Re-push the presence when Discord resumes a session without on_ready."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            with patch.object(bot, "change_presence", new_callable=unittest.mock.AsyncMock) as change_presence:
                await bot.on_resumed()
            change_presence.assert_awaited_once()
            self.assertEqual(change_presence.await_args.kwargs["status"], discord.Status.online)
        finally:
            await bot.close()

    async def test_bot_sets_online_presence_when_connected(self) -> None:
        """Show an online status and a useful activity after connecting."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            with patch.object(bot, "change_presence", new_callable=unittest.mock.AsyncMock) as change_presence:
                with patch.object(bot._connection, "application_id", 123456789):
                    await bot.on_ready()
            change_presence.assert_awaited_once()
            kwargs = change_presence.await_args.kwargs
            expected_status, expected_activity = load_presence_config(application_id=123456789)
            self.assertEqual(kwargs["status"], expected_status)
            self.assertEqual(kwargs["activity"].name, expected_activity.name)
            self.assertEqual(kwargs["activity"].to_dict(), expected_activity.to_dict())
        finally:
            await bot.close()

    async def test_bot_replies_to_direct_messages_with_user_install_link(self) -> None:
        """Send helpful GIF guidance and the account-install link in DMs."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        message = Mock()
        message.author.bot = False
        message.guild = None
        message.channel.send = unittest.mock.AsyncMock()
        try:
            with patch.object(bot._connection, "application_id", 123456789):
                await bot.on_message(message)
            message.channel.send.assert_awaited_once()
            response = message.channel.send.await_args.args[0]
            self.assertIn("/gif", response)
            self.assertIn("JPG/JPEG", response)
            self.assertIn("up to 5 files", response)
            self.assertIn(
                "https://discord.com/oauth2/authorize?client_id=123456789"
                "&scope=applications.commands&integration_type=1",
                response,
            )
            allowed_mentions = message.channel.send.await_args.kwargs["allowed_mentions"]
            self.assertFalse(allowed_mentions.everyone)
            self.assertFalse(allowed_mentions.users)
            self.assertFalse(allowed_mentions.roles)
            self.assertFalse(allowed_mentions.replied_user)
        finally:
            await bot.close()

    async def test_bot_skips_guild_and_bot_authored_messages(self) -> None:
        """Never auto-reply to ordinary server messages or other bots."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            for guild, is_bot in ((Mock(), False), (None, True)):
                with self.subTest(guild=guild is not None, is_bot=is_bot):
                    message = Mock()
                    message.guild = guild
                    message.author.bot = is_bot
                    message.channel.send = unittest.mock.AsyncMock()
                    await bot.on_message(message)
                    message.channel.send.assert_not_awaited()
        finally:
            await bot.close()

    async def test_bot_dm_reply_without_application_id_omits_broken_link(self) -> None:
        """Keep startup-safe help useful without constructing an invalid OAuth URL."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        message = Mock()
        message.author.bot = False
        message.guild = None
        message.channel.send = unittest.mock.AsyncMock()
        try:
            with patch.object(bot._connection, "application_id", None):
                await bot.on_message(message)
            response = message.channel.send.await_args.args[0]
            self.assertIn("/gif", response)
            self.assertNotIn("oauth2/authorize", response)
        finally:
            await bot.close()

    async def test_gif_result_offers_share_and_copy_controls_in_every_context(self) -> None:
        """Attach one share/copy view to guild, bot-DM, and private-channel results."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        attachment = Mock()
        attachment.filename = "reaction.webp"
        attachment.content_type = "image/webp"
        attachment.size = 128
        link = "https://gifs.example.test/u/reaction.gif"

        async def fake_process(_attachment: object, _user_id: int, _guild_id: int | None) -> str:
            return link

        try:
            with patch.object(bot, "_process_one", side_effect=fake_process):
                for guild_id, context in (
                    (123, discord.app_commands.AppCommandContext(guild=True)),
                    (None, discord.app_commands.AppCommandContext(dm_channel=True)),
                    (None, discord.app_commands.AppCommandContext(private_channel=True)),
                ):
                    with self.subTest(guild_id=guild_id, context=context):
                        interaction = Mock()
                        interaction.guild_id = guild_id
                        interaction.context = context
                        interaction.user.id = 456
                        interaction.response.defer = unittest.mock.AsyncMock()
                        interaction.followup.send = unittest.mock.AsyncMock()
                        interaction.response.send_message = unittest.mock.AsyncMock()
                        await bot.interaction_upload(interaction, attachment)

                        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
                        result = interaction.followup.send.await_args.kwargs
                        self.assertTrue(result["ephemeral"])
                        self.assertIsInstance(result["embed"], discord.Embed)
                        self.assertEqual(result["embed"].image.url, link)
                        view = result["view"]
                        self.assertIsInstance(view, PublicGifView)
                        self.assertEqual(view.link, link)
                        self.assertEqual(
                            [child.label for child in view.children],
                            ["Share in this chat", "Copy Link"],
                        )
        finally:
            await bot.close()

    async def test_batch_command_processes_each_file_and_keeps_share_copy_controls(self) -> None:
        """Batch results should be sequential per-file outputs with each file's controls."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        attachment_a = Mock(filename="a.jpg")
        attachment_b = Mock(filename="b.avif")
        interaction = Mock()
        interaction.guild_id = None
        interaction.user.id = 321
        interaction.response.defer = unittest.mock.AsyncMock()
        interaction.response.send_message = unittest.mock.AsyncMock()
        interaction.followup.send = unittest.mock.AsyncMock()
        interaction.edit_original_response = unittest.mock.AsyncMock()

        async def process(attachment: object, _user_id: int, _guild_id: int | None) -> str:
            return f"https://gifs.example.test/{attachment.filename}.gif"

        try:
            with patch.object(bot, "_process_one", side_effect=process):
                await bot.interaction_upload_batch(interaction, [attachment_a, attachment_b])
            interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
            interaction.edit_original_response.assert_awaited_once()
            edit_kwargs = interaction.edit_original_response.await_args.kwargs
            self.assertEqual(edit_kwargs["content"], "Finished processing 2 files.")
            self.assertFalse(edit_kwargs["allowed_mentions"].everyone)
            self.assertEqual(interaction.followup.send.await_count, 2)
            for result in interaction.followup.send.await_args_list:
                self.assertTrue(result.kwargs["ephemeral"])
                self.assertIsInstance(result.kwargs["view"], PublicGifView)
                self.assertEqual(
                    [child.label for child in result.kwargs["view"].children],
                    ["Share in this chat", "Copy Link"],
                )
        finally:
            await bot.close()

    async def test_batch_rejects_more_than_five_files(self) -> None:
        """Reject oversized internal batches rather than silently dropping files."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        interaction = Mock()
        interaction.response.send_message = unittest.mock.AsyncMock()
        try:
            attachments = [Mock(filename=f"{i}.jpg") for i in range(MAX_GIF_BATCH + 1)]
            await bot.interaction_upload_batch(interaction, attachments)
            interaction.response.send_message.assert_awaited_once()
            self.assertIn(str(MAX_GIF_BATCH), interaction.response.send_message.await_args.args[0])
            interaction.response.defer.assert_not_called()
        finally:
            await bot.close()

    async def test_batch_continues_when_one_file_fails(self) -> None:
        """Return the successful file and a separate error for the rejected file."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        bad, good = Mock(filename="bad.exe"), Mock(filename="good.jpg")
        interaction = Mock()
        interaction.guild_id = None
        interaction.user.id = 321
        interaction.response.defer = unittest.mock.AsyncMock()
        interaction.response.send_message = unittest.mock.AsyncMock()
        interaction.followup.send = unittest.mock.AsyncMock()
        interaction.edit_original_response = unittest.mock.AsyncMock()

        async def process(attachment: object, _user_id: int, _guild_id: int | None) -> str:
            if attachment is bad:
                raise InvalidImage("unsupported file")
            return "https://gifs.example.test/good.gif"

        try:
            with patch.object(bot, "_process_one", side_effect=process):
                await bot.interaction_upload_batch(interaction, [bad, good])
            self.assertEqual(interaction.followup.send.await_count, 2)
            self.assertIn("bad.exe", interaction.followup.send.await_args_list[0].args[0])
            self.assertIsInstance(interaction.followup.send.await_args_list[1].kwargs["embed"], discord.Embed)
            interaction.edit_original_response.assert_awaited_once()
        finally:
            await bot.close()

    async def test_share_button_posts_visibly_in_server_and_private_channels(self) -> None:
        """Share as a public follow-up in either a server or a user-installed DM."""
        embed = discord.Embed(title="Your GIF is ready")
        embed.set_image(url="https://gifs.example.test/u/reaction.gif")

        contexts = (
            (123, discord.app_commands.AppCommandContext(guild=True)),
            (None, discord.app_commands.AppCommandContext(dm_channel=True)),
            (None, discord.app_commands.AppCommandContext(private_channel=True)),
        )
        for guild_id, context in contexts:
            with self.subTest(guild_id=guild_id, context=context):
                view = PublicGifView(123, embed, "https://gifs.example.test/u/reaction.gif")
                share_button = next(child for child in view.children if child.label == "Share in this chat")
                interaction = Mock()
                interaction.user.id = 123
                interaction.guild_id = guild_id
                interaction.context = context
                interaction.guild = Mock() if guild_id is not None else None
                interaction.channel = Mock() if guild_id is not None else None
                interaction.app_permissions.send_messages = True
                interaction.app_permissions.embed_links = True
                interaction.response.send_message = unittest.mock.AsyncMock()
                interaction.response.defer = unittest.mock.AsyncMock()
                interaction.followup.send = unittest.mock.AsyncMock()
                interaction.edit_original_response = unittest.mock.AsyncMock()

                await view.share_in_chat.callback(interaction)

                interaction.response.defer.assert_awaited_once_with()
                self.assertEqual(interaction.followup.send.await_count, 1)
                shared_kwargs = interaction.followup.send.await_args.kwargs
                self.assertIs(shared_kwargs["embed"], embed)
                self.assertFalse(shared_kwargs["ephemeral"])
                self.assertFalse(shared_kwargs["allowed_mentions"].everyone)
                self.assertEqual(interaction.edit_original_response.await_count, 1)
                self.assertIs(interaction.edit_original_response.await_args.kwargs["view"], view)
                self.assertTrue(view.posted)
                self.assertTrue(share_button.disabled)
                self.assertEqual(share_button.label, "Shared")
                copy_button = next(child for child in view.children if child.label == "Copy Link")
                self.assertFalse(copy_button.disabled)

    async def test_share_button_is_owner_only_and_checks_guild_permissions(self) -> None:
        """Keep sharing requester-only and enforce server posting permissions."""
        embed = discord.Embed(title="Your GIF is ready")
        view = PublicGifView(123, embed, "https://gifs.example.test/u/reaction.gif")
        share_button = next(child for child in view.children if child.label == "Share in this chat")

        unauthorized = Mock()
        unauthorized.user.id = 456
        unauthorized.response.send_message = unittest.mock.AsyncMock()
        await view.share_in_chat.callback(unauthorized)
        unauthorized.response.send_message.assert_awaited_once_with(
            "Only the person who requested this GIF can share it.", ephemeral=True
        )

        denied = Mock()
        denied.user.id = 123
        denied.guild_id = 123
        denied.guild = Mock()
        denied.channel = Mock()
        denied.app_permissions.send_messages = False
        denied.app_permissions.embed_links = True
        denied.response.send_message = unittest.mock.AsyncMock()
        denied.response.defer = unittest.mock.AsyncMock()
        await view.share_in_chat.callback(denied)
        denied.response.send_message.assert_awaited_once()
        self.assertIn("Send Messages and Embed Links", denied.response.send_message.await_args.args[0])
        denied.response.defer.assert_not_awaited()
        self.assertFalse(view.posted)

    async def test_copy_button_reveals_direct_link_ephemerally_to_requester(self) -> None:
        """Return the direct URL privately; Discord has no native clipboard component."""
        link = "https://gifs.example.test/u/reaction.gif"
        view = PublicGifView(123, discord.Embed(title="GIF"), link)
        copy_button = next(child for child in view.children if child.label == "Copy Link")

        unauthorized = Mock()
        unauthorized.user.id = 456
        unauthorized.response.send_message = unittest.mock.AsyncMock()
        await view.copy_link.callback(unauthorized)
        unauthorized.response.send_message.assert_awaited_once_with(
            "Only the person who requested this GIF can copy its link.", ephemeral=True
        )

        interaction = Mock()
        interaction.user.id = 123
        interaction.response.send_message = unittest.mock.AsyncMock()
        await view.copy_link.callback(interaction)
        copy_args = interaction.response.send_message.await_args
        self.assertEqual(copy_args.args, (link,))
        self.assertTrue(copy_args.kwargs["ephemeral"])
        self.assertTrue(copy_args.kwargs["suppress_embeds"])
        mentions = copy_args.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)

    async def test_public_gif_button_is_owner_only_single_use_and_posts_embed(self) -> None:
        """Post the polished GIF embed once to the clicked server channel."""
        embed = discord.Embed(title="Your GIF is ready")
        embed.set_image(url="https://gifs.example.test/u/reaction.gif")
        view = PublicGifView(123, embed, "https://gifs.example.test/u/reaction.gif")
        button = next(item for item in view.children if isinstance(item, discord.ui.Button) and item.label == "Share in this chat")
        channel = Mock()
        channel.send = unittest.mock.AsyncMock()
        unauthorized = Mock()
        unauthorized.user.id = 456
        unauthorized.response.send_message = unittest.mock.AsyncMock()
        await view.share_in_chat.callback(unauthorized)
        unauthorized.response.send_message.assert_awaited_once()

        interaction = Mock()
        interaction.user.id = 123
        interaction.guild = Mock()
        interaction.channel = channel
        interaction.guild_id = 123
        interaction.app_permissions.send_messages = True
        interaction.app_permissions.embed_links = True
        interaction.response.send_message = unittest.mock.AsyncMock()
        interaction.response.defer = unittest.mock.AsyncMock()
        interaction.followup.send = unittest.mock.AsyncMock()
        interaction.edit_original_response = unittest.mock.AsyncMock()
        await view.share_in_chat.callback(interaction)
        interaction.response.defer.assert_awaited_once_with()
        interaction.followup.send.assert_awaited_once()
        self.assertEqual(interaction.edit_original_response.await_count, 1)
        self.assertIs(interaction.edit_original_response.await_args.kwargs["view"], view)
        self.assertTrue(view.posted)
        self.assertTrue(button.disabled)

        repeat = Mock()
        repeat.user.id = 123
        repeat.response.send_message = unittest.mock.AsyncMock()
        await view.share_in_chat.callback(repeat)
        repeat.response.send_message.assert_awaited_once_with("This GIF was already shared.", ephemeral=True)
        interaction.followup.send.assert_awaited_once()

    async def test_public_gif_button_checks_channel_permissions(self) -> None:
        """Explain missing message/embed permissions instead of failing the click."""
        view = PublicGifView(123, discord.Embed(title="GIF"), "https://gifs.example.test/u/reaction.gif")
        button = next(item for item in view.children if isinstance(item, discord.ui.Button))
        interaction = Mock()
        interaction.user.id = 123
        interaction.guild = Mock()
        interaction.channel = Mock()
        interaction.app_permissions.send_messages = False
        interaction.app_permissions.embed_links = False
        interaction.response.send_message = unittest.mock.AsyncMock()
        interaction.response.defer = unittest.mock.AsyncMock()

        await view.share_in_chat.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        self.assertIn("Send Messages and Embed Links", interaction.response.send_message.await_args.args[0])
        interaction.response.defer.assert_not_awaited()

    async def test_bot_applies_status_config_edits_without_restart(self) -> None:
        """Follow status.config while connected so presence edits apply live."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "status.config"
                config_path.write_text("[status]\nstatus = idle\nactivity_name = First\n", encoding="utf-8")
                with patch("main.PRESENCE_CONFIG_PATH", config_path):
                    signature = _presence_signature()
                    with patch.object(
                        bot, "change_presence", new_callable=unittest.mock.AsyncMock
                    ) as change_presence:
                        config_path.write_text(
                            "[status]\nstatus = dnd\nactivity_name = Second\n", encoding="utf-8"
                        )
                        signature, tick = await bot._presence_watch_step(signature, 3)
                    self.assertEqual(signature, _presence_signature())
            change_presence.assert_awaited_once()
            self.assertEqual(change_presence.await_args.kwargs["status"], discord.Status.dnd)
            self.assertEqual(change_presence.await_args.kwargs["activity"].name, "Second")
            self.assertEqual(tick, 0)
        finally:
            await bot.close()

    async def test_presence_watch_reasserts_unchanged_config_periodically(self) -> None:
        """Keep a quiet app's presence fresh without any config edit."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            with tempfile.TemporaryDirectory() as directory:
                config_path = Path(directory) / "status.config"
                config_path.write_text("[status]\nstatus = online\nactivity_name = Bombagif\n", encoding="utf-8")
                with patch("main.PRESENCE_CONFIG_PATH", config_path):
                    signature = _presence_signature()
                    with patch.object(
                        bot, "change_presence", new_callable=unittest.mock.AsyncMock
                    ) as change_presence:
                        bot.ws = object()
                        await bot._presence_watch_step(signature, PRESENCE_REASSERT_TICKS - 2)
                        change_presence.assert_not_awaited()
                        await bot._presence_watch_step(signature, PRESENCE_REASSERT_TICKS - 1)
            change_presence.assert_awaited_once()
            self.assertEqual(change_presence.await_args.kwargs["status"], discord.Status.online)
        finally:
            bot.ws = None
            await bot.close()
        """Enable user installation without requiring a server bot install."""
        settings = Settings(
            discord_token="unused",
            zipline_token="unused",
            zipline_url="https://self-hosted.example.test",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        bot = create_bot(settings)
        try:
            command = bot.tree.get_command("gif")
            self.assertIsNotNone(command)
            assert command is not None
            self.assertTrue(command.allowed_installs.user)
            self.assertFalse(command.allowed_installs.guild)
            self.assertTrue(command.allowed_contexts.guild)
            self.assertTrue(command.allowed_contexts.dm_channel)
            self.assertTrue(command.allowed_contexts.private_channel)
            self.assertEqual(command.parameters[0].name, "media")
            self.assertEqual(
                [parameter.name for parameter in command.parameters],
                ["media", "media_2", "media_3", "media_4", "media_5"],
            )
            self.assertTrue(command.parameters[0].required)
            self.assertTrue(all(not parameter.required for parameter in command.parameters[1:]))
        finally:
            await bot.close()


class ZiplineResponseParsingTests(unittest.TestCase):
    """Parse HTTPS links returned by Zipline across proxy and API versions."""

    def test_upgrades_same_host_http_url_behind_https_proxy(self) -> None:
        """Use the configured public host and HTTPS scheme for proxy-generated links."""
        result = _extract_zipline_file_url(
            {"files": ["http://zip.example.test:3000/u/abc123.gif"]},
            "https://zip.example.test",
        )
        self.assertEqual(result, "https://zip.example.test/u/abc123.gif")

    def test_preserves_https_response_url(self) -> None:
        """Keep the valid HTTPS URL returned by Zipline unchanged."""
        url = "https://zip.example.test/u/abc123.gif"
        self.assertEqual(_extract_zipline_file_url({"files": [url]}, "https://zip.example.test"), url)

    def test_rejects_cross_host_or_plain_http_urls(self) -> None:
        """Do not upgrade arbitrary HTTP links from a successful API payload."""
        self.assertIsNone(
            _extract_zipline_file_url(
                {"files": ["http://elsewhere.example.test/u/abc123.gif"]},
                "https://zip.example.test",
            )
        )
        self.assertIsNone(
            _extract_zipline_file_url(
                {"files": ["http://zip.example.test/u/abc123.gif"]},
                "http://zip.example.test",
            )
        )


class ZiplineTests(unittest.IsolatedAsyncioTestCase):
    """Exercise Zipline multipart upload and retry behavior on loopback HTTP."""

    async def asyncSetUp(self) -> None:
        self.requests = 0

        async def upload(request: web.Request) -> web.Response:
            self.requests += 1
            self.assertEqual(request.headers.get("Authorization"), "test-token")
            self.assertIn("multipart/form-data", request.headers.get("Content-Type", ""))
            body = await request.read()
            self.assertIn(b'name="file"; filename="converted.gif"', body)
            self.assertIn(b"GIF89a", body)
            if self.requests == 1:
                return web.Response(status=503)
            if self.requests == 2:
                return web.Response(status=429, headers={"Retry-After": "0"})
            return web.json_response({"files": ["https://self-hosted.example.test/a1b2.gif"]})

        self.app = web.Application()
        self.app.router.add_post("/api/upload", upload)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        self.port = self.site._server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def test_retries_and_returns_zipline_file_url(self) -> None:
        """Retry errors, then parse Zipline v3 files as URL strings."""
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{self.port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            link = await upload_to_zipline(
                session,
                settings,
                b"GIF89a-test",
                "converted.gif",
                trace_id="test-trace",
            )
        self.assertEqual(link, "https://self-hosted.example.test/a1b2.gif")
        self.assertEqual(self.requests, 3)

    async def test_accepts_zipline_legacy_object_response(self) -> None:
        """Accept Zipline instances that wrap file URLs in objects."""

        async def upload(_: web.Request) -> web.Response:
            return web.json_response({"files": [{"url": "https://self-hosted.example.test/legacy.gif"}]})

        app = web.Application()
        app.router.add_post("/api/upload", upload)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                result = await upload_to_zipline(session, settings, b"gif", "legacy.gif", trace_id="legacy")
            self.assertEqual(result, "https://self-hosted.example.test/legacy.gif")
        finally:
            await runner.cleanup()

    async def test_accepts_zipline_plain_text_url_response(self) -> None:
        """Accept Zipline's documented No-JSON URL response format."""

        async def upload(_: web.Request) -> web.Response:
            return web.Response(text="https://self-hosted.example.test/plain.gif")

        app = web.Application()
        app.router.add_post("/api/upload", upload)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                result = await upload_to_zipline(session, settings, b"gif", "plain.gif", trace_id="plain")
            self.assertEqual(result, "https://self-hosted.example.test/plain.gif")
        finally:
            await runner.cleanup()

    async def test_accepts_zipline_root_list_response(self) -> None:
        """Accept JSON responses that directly contain a list of file URLs."""

        async def upload(_: web.Request) -> web.Response:
            return web.json_response(["https://self-hosted.example.test/list.gif"])

        app = web.Application()
        app.router.add_post("/api/upload", upload)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                result = await upload_to_zipline(session, settings, b"gif", "list.gif", trace_id="list")
            self.assertEqual(result, "https://self-hosted.example.test/list.gif")
        finally:
            await runner.cleanup()

    async def test_accepts_zipline_v4_string_response(self) -> None:
        """Accept a JSON string URL response used by current Zipline docs."""

        async def upload(_: web.Request) -> web.Response:
            return web.json_response("https://self-hosted.example.test/v4.gif")

        app = web.Application()
        app.router.add_post("/api/upload", upload)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        port = site._server.sockets[0].getsockname()[1]
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                result = await upload_to_zipline(session, settings, b"gif", "v4.gif", trace_id="v4")
            self.assertEqual(result, "https://self-hosted.example.test/v4.gif")
        finally:
            await runner.cleanup()

    async def test_rejects_invalid_zipline_payload(self) -> None:
        """Treat malformed success responses as an explicit upload failure."""

        async def bad_upload(_: web.Request) -> web.Response:
            return web.json_response({"files": []})

        bad_app = web.Application()
        bad_app.router.add_post("/api/upload", bad_upload)
        bad_runner = web.AppRunner(bad_app)
        await bad_runner.setup()
        bad_site = web.TCPSite(bad_runner, "127.0.0.1", 0)
        await bad_site.start()
        assert bad_site._server is not None
        bad_port = bad_site._server.sockets[0].getsockname()[1]
        settings = Settings(
            discord_token="unused",
            zipline_token="test-token",
            zipline_url=f"http://127.0.0.1:{bad_port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )
        try:
            with self.assertRaises(UploadFailure):
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=3)) as session:
                    await upload_to_zipline(session, settings, b"gif", "bad.gif", trace_id="invalid")
        finally:
            await bad_runner.cleanup()


if __name__ == "__main__":
    unittest.main()
