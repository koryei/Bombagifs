"""Shared download -> convert -> upload pipeline used by both front doors."""

from __future__ import annotations

import io
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

from PIL import Image

from bombagif_core import MAX_INPUT_BYTES, InvalidImage, MediaSource, Settings, UserFacingError, convert_and_upload

SETTINGS = Settings(
    discord_token="t",
    zipline_token="z",
    zipline_url="https://z.example",
    log_level="INFO",
    allowed_guilds=frozenset(),
    background=(255, 255, 255),
)


def _png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


class ConvertAndUploadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.addCleanup(self.executor.shutdown)

    async def test_convert_and_upload_returns_link(self) -> None:
        download = AsyncMock(return_value=_png())
        upload = AsyncMock(return_value="https://z.example/u/a.gif")
        with patch("bombagif_core.download_attachment", download), patch("bombagif_core.upload_to_zipline", upload):
            link = await convert_and_upload(
                object(), SETTINGS, self.executor,
                MediaSource("My Pic!.png", "https://cdn.discordapp.com/x"), trace_id="t1",
            )
        self.assertEqual(link, "https://z.example/u/a.gif")
        args = upload.await_args
        self.assertEqual(args.args[3], "My-Pic.gif")
        self.assertTrue(args.args[2].startswith(b"GIF8"))

    async def test_unsupported_extension_with_unrecognized_content_is_rejected(self) -> None:
        download = AsyncMock(return_value=b"just some text, not media")
        with patch("bombagif_core.download_url", download):
            with self.assertRaises(InvalidImage):
                await convert_and_upload(
                    object(), SETTINGS, self.executor,
                    MediaSource("a.txt", "https://cdn.discordapp.com/x"), trace_id="t2",
                )

    async def test_extensionless_png_is_sniffed_and_converted(self) -> None:
        upload = AsyncMock(return_value="https://z.example/u/embed.gif")
        with patch("bombagif_core.download_url", AsyncMock(return_value=_png())), patch(
            "bombagif_core.upload_to_zipline", upload
        ):
            link = await convert_and_upload(
                object(), SETTINGS, self.executor,
                MediaSource("embed", "https://media.discordapp.net/x"), trace_id="t3",
            )
        self.assertEqual(link, "https://z.example/u/embed.gif")
        self.assertEqual(upload.await_args.args[3], "embed.gif")

    async def test_oversized_declared_size_is_rejected_before_download(self) -> None:
        download = AsyncMock(return_value=_png())
        with patch("bombagif_core.download_url", download):
            with self.assertRaises(UserFacingError):
                await convert_and_upload(
                    object(), SETTINGS, self.executor,
                    MediaSource("embed", "https://media.discordapp.net/x", size=MAX_INPUT_BYTES + 1), trace_id="t4",
                )
        download.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
