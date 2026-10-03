"""Focused behavior tests for Bombagif's image and Zipline paths."""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from PIL import Image

from main import (
    InvalidImage,
    Settings,
    UploadFailure,
    UserFacingError,
    _read_settings,
    _rasterize_svg,
    create_bot,
    convert_image,
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
        with self.assertRaises(InvalidImage):
            validate_image_metadata("input.png", "image/webp", 20)
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


class DiscordCommandTests(unittest.IsolatedAsyncioTestCase):
    """Verify the command is exposed as a personal user-installed app."""

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
            return web.json_response({"files": [{"url": "https://self-hosted.example.test/a1b2.gif"}]})

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
        """Retry a 5xx response and parse the returned files[0].url."""
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
