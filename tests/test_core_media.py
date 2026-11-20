"""URL safety and format sniffing for media that does not come from an attachment."""

from __future__ import annotations

import unittest

from bombagif_core import is_discord_media_url, sniff_suffix


class DiscordMediaUrlTests(unittest.TestCase):
    def test_accepts_discord_hosts(self) -> None:
        for url in (
            "https://media.discordapp.net/attachments/1/2/a.png?ex=1&hm=2",
            "https://cdn.discordapp.com/attachments/1/2/a.png",
            "https://images-ext-1.discordapp.net/external/h/https/media.tenor.com/x/t.mp4",
            "https://media.discordapp.net:443/a.png",
        ):
            with self.subTest(url=url):
                self.assertTrue(is_discord_media_url(url))

    def test_rejects_everything_else(self) -> None:
        for url in (
            "http://media.discordapp.net/a.png",
            "https://evil.example/a.png",
            "https://discordapp.net.evil.example/a.png",
            "https://evil-discordapp.net/a.png",
            "https://user:pw@media.discordapp.net/a.png",
            "https://media.discordapp.net:8443/a.png",
            "https://127.0.0.1/a.png",
            "https://localhost/a.png",
            "ftp://media.discordapp.net/a.png",
            "https://media.discordapp.net:notaport/a.png",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(is_discord_media_url(url))


class SniffSuffixTests(unittest.TestCase):
    def test_recognizes_supported_formats(self) -> None:
        samples = {
            ".png": b"\x89PNG\r\n\x1a\n" + b"\0" * 16,
            ".jpg": b"\xff\xd8\xff\xe0" + b"\0" * 16,
            ".webp": b"RIFF\x24\0\0\0WEBPVP8 " + b"\0" * 16,
            ".avif": b"\0\0\0\x1cftypavif\0\0\0\0avifmif1" + b"\0" * 16,
            ".mp4": b"\0\0\0\x20ftypisom\0\0\x02\0isomiso2" + b"\0" * 16,
            ".webm": b"\x1a\x45\xdf\xa3" + b"\0" * 16,
        }
        for suffix, data in samples.items():
            with self.subTest(suffix=suffix):
                self.assertEqual(sniff_suffix(data), suffix)

    def test_unknown_or_unsupported_is_none(self) -> None:
        for data in (b"", b"GIF89a" + b"\0" * 16, b"not an image at all", b"\0" * 32):
            with self.subTest(data=data[:8]):
                self.assertIsNone(sniff_suffix(data))


if __name__ == "__main__":
    unittest.main()
