"""The scout's Discord adapter: which messages are clippable, and which pictures a message carries."""

from __future__ import annotations

import unittest
from importlib import metadata
from types import SimpleNamespace

try:
    metadata.version("discord.py-self")
    HAS_SELFBOT_LIB = True
except metadata.PackageNotFoundError:
    HAS_SELFBOT_LIB = False

if HAS_SELFBOT_LIB:
    from scout_discord import IMAGE_EDGE, MAX_IMAGES, image_urls


def attachment(url: str, content_type: str = "image/png") -> SimpleNamespace:
    return SimpleNamespace(url=url, content_type=content_type)


def message(**kwargs: object) -> SimpleNamespace:
    fields: dict[str, object] = {"attachments": (), "embeds": (), "stickers": ()}
    fields.update(kwargs)
    return SimpleNamespace(**fields)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class ImageUrlTests(unittest.TestCase):
    def test_an_image_attachment_is_picked_up(self) -> None:
        found = image_urls(message(attachments=[attachment("https://cdn.discordapp.com/a.png")]))
        self.assertEqual(len(found), 1)
        self.assertIn("cdn.discordapp.com/a.png", found[0])

    def test_a_non_image_attachment_is_skipped(self) -> None:
        found = image_urls(message(attachments=[attachment("https://cdn.discordapp.com/a.zip", "application/zip")]))
        self.assertEqual(found, [])

    def test_an_attachment_without_a_content_type_is_skipped(self) -> None:
        found = image_urls(message(attachments=[attachment("https://cdn.discordapp.com/a.png", "")]))
        self.assertEqual(found, [])

    def test_cdn_urls_are_resized(self) -> None:
        found = image_urls(message(attachments=[attachment("https://cdn.discordapp.com/a.png")]))
        self.assertIn(f"width={IMAGE_EDGE}", found[0])
        self.assertIn(f"height={IMAGE_EDGE}", found[0])

    def test_a_url_that_already_has_a_query_gets_an_ampersand(self) -> None:
        found = image_urls(message(attachments=[attachment("https://cdn.discordapp.com/a.png?ex=1")]))
        self.assertIn("?ex=1&width=", found[0])

    def test_a_foreign_url_is_left_alone(self) -> None:
        found = image_urls(message(attachments=[attachment("https://example.com/a.png")]))
        self.assertEqual(found, ["https://example.com/a.png"])

    def test_embed_images_and_thumbnails_are_picked_up(self) -> None:
        embed = SimpleNamespace(
            image=SimpleNamespace(url="https://cdn.discordapp.com/i.png"),
            thumbnail=SimpleNamespace(url="https://cdn.discordapp.com/t.png"),
        )
        self.assertEqual(len(image_urls(message(embeds=[embed]))), 2)

    def test_an_embed_without_pictures_adds_nothing(self) -> None:
        embed = SimpleNamespace(image=None, thumbnail=None)
        self.assertEqual(image_urls(message(embeds=[embed])), [])

    def test_a_sticker_is_picked_up(self) -> None:
        sticker = SimpleNamespace(url="https://cdn.discordapp.com/s.png")
        self.assertEqual(len(image_urls(message(stickers=[sticker]))), 1)

    def test_pictures_are_capped(self) -> None:
        many = [attachment(f"https://cdn.discordapp.com/{i}.png") for i in range(6)]
        self.assertEqual(len(image_urls(message(attachments=many))), MAX_IMAGES)

    def test_a_message_with_no_pictures_has_none(self) -> None:
        self.assertEqual(image_urls(message()), [])


if __name__ == "__main__":
    unittest.main()
