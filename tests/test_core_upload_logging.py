"""Zipline rejections must leave a diagnosable (and secret-free) log line."""

from __future__ import annotations

import unittest

import aiohttp
from aiohttp import web

from bombagif_core import Settings, UploadFailure, upload_to_zipline


class UploadLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def _serve(self, handler) -> Settings:
        app = web.Application()
        app.router.add_post("/api/upload", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        self.addAsyncCleanup(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        return Settings(
            discord_token="unused",
            zipline_token="zipline-secret-token",
            zipline_url=f"http://127.0.0.1:{port}",
            log_level="INFO",
            allowed_guilds=frozenset(),
            background=(255, 255, 255),
        )

    async def _upload(self, settings: Settings) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            await upload_to_zipline(session, settings, b"GIF89a", "a.gif", trace_id="trace-1")

    async def test_client_error_logs_status_and_body_snippet(self) -> None:
        async def handler(_: web.Request) -> web.Response:
            return web.json_response({"error": "Invalid token"}, status=403)

        settings = await self._serve(handler)
        with self.assertLogs("bombagif.zipline", "WARNING") as logs:
            with self.assertRaisesRegex(UploadFailure, "rejected the upload"):
                await self._upload(settings)
        output = "\n".join(logs.output)
        self.assertIn("status=403", output)
        self.assertIn("Invalid token", output)
        self.assertNotIn(settings.zipline_token, output)

    async def test_exhausted_retries_log_final_status(self) -> None:
        async def handler(_: web.Request) -> web.Response:
            return web.Response(status=503, headers={"Retry-After": "0"})

        settings = await self._serve(handler)
        with self.assertLogs("bombagif.zipline", "WARNING") as logs:
            with self.assertRaisesRegex(UploadFailure, "temporarily unavailable"):
                await self._upload(settings)
        self.assertIn("status=503", "\n".join(logs.output))


if __name__ == "__main__":
    unittest.main()
