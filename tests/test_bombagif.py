"""Focused behavior tests for Bombagif's image and Zipline paths."""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
import discord
from aiohttp import web
from PIL import Image

from main import (
    InvalidImage,
    Settings,
    UploadFailure,
    UserFacingError,
    _extract_zipline_file_url,
    _read_settings,
    _rasterize_svg,
    create_bot,
    convert_image,
    load_presence_config,
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

    def test_validates_suffix_mime_and_size(self) -> None:
        """Accept a valid image and reject mismatches and oversized inputs."""
        self.assertEqual(validate_image_metadata("input.PNG", "image/png", 20), ".png")
        # Discord's content_type can be absent or incorrectly generic for uploads.
        self.assertEqual(validate_image_metadata("input.png", None, 20), ".png")
        self.assertEqual(validate_image_metadata("input.png", "application/octet-stream", 20), ".png")
        with self.assertRaises(InvalidImage):
            validate_image_metadata("input.jpg", "image/jpeg", 20)
        with self.assertRaisesRegex(UserFacingError, "too large"):
            validate_image_metadata("large.png", "image/png", 15 * 1024 * 1024 + 1)

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

    def test_loads_playing_presence(self) -> None:
        """Read the checked-in default activity and online status."""
        from main import PRESENCE_CONFIG_PATH

        status, activity = load_presence_config(PRESENCE_CONFIG_PATH)
        self.assertEqual(status, discord.Status.online)
        self.assertIsInstance(activity, discord.Game)
        self.assertEqual(activity.name, "/gif | your images to GIFs")

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

    def test_rejects_invalid_presence_values(self) -> None:
        """Fail fast on invalid status and unsupported activity values."""
        invalid_values = (
            "[status]\nstatus = bogus\n",
            "[status]\nactivity_type = unknown\n",
            "[status]\nactivity_type = streaming\nactivity_text = live\n",
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
                await bot.on_ready()
            change_presence.assert_awaited_once()
            kwargs = change_presence.await_args.kwargs
            expected_status, expected_activity = load_presence_config()
            self.assertEqual(kwargs["status"], expected_status)
            self.assertEqual(kwargs["activity"].name, expected_activity.name)
        finally:
            await bot.close()

    async def test_gif_is_user_installed_and_global_context_enabled(self) -> None:
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
