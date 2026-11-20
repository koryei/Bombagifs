"""Asset URL rules and card building for clips (discord-free)."""

from __future__ import annotations

import unittest

from clip_assets import asset_url_allowed, build_card, custom_emoji_url, mentioned_ids, rgb, twemoji_url
from scout_buffer import ScoutMessage, Version


def scout_msg(id: int, text: str, *, author: int = 10, name: str | None = None, color: int | None = None, tag: str | None = None, at: float = 100.0) -> ScoutMessage:
    return ScoutMessage(
        id=id, chat_id=1, author_id=author, author_name=name or f"user{author}", avatar_url="https://cdn.discordapp.com/a.png",
        color=color, tag=tag, created_at=at, versions=[Version(text, at), Version("edited " + text, at + 5)],
    )


class UrlTests(unittest.TestCase):
    def test_twemoji_url_is_pinned_and_validated(self) -> None:
        self.assertEqual(
            twemoji_url("1f602"), "https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/1f602.png"
        )
        self.assertTrue(twemoji_url("1f468-200d-1f469").endswith("/1f468-200d-1f469.png"))
        for bad in ("", "../x", "1F602", "1f602.png", "zz", "1f602/../a", "1f602?x=1"):
            with self.subTest(key=bad):
                with self.assertRaises(ValueError):
                    twemoji_url(bad)

    def test_custom_emoji_url(self) -> None:
        self.assertEqual(custom_emoji_url("123456"), "https://cdn.discordapp.com/emojis/123456.png?size=64")
        for bad in ("", "12a", "../1", "1/2"):
            with self.subTest(id=bad):
                with self.assertRaises(ValueError):
                    custom_emoji_url(bad)

    def test_only_discord_and_the_pinned_emoji_host_are_allowed(self) -> None:
        for url in (
            "https://cdn.discordapp.com/avatars/1/h.png?size=128",
            "https://media.discordapp.net/x.png",
            twemoji_url("1f602"),
            custom_emoji_url("123"),
        ):
            with self.subTest(url=url):
                self.assertTrue(asset_url_allowed(url))
        for url in (
            "https://evil.example/a.png",
            "http://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/1f602.png",
            "https://cdn.jsdelivr.net/gh/someone/else/file.png",
            "https://cdn.jsdelivr.net.evil.example/gh/jdecked/twemoji@15.1.0/a.png",
            "https://user:pw@cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/a.png",
            "https://cdn.jsdelivr.net:8443/gh/jdecked/twemoji@15.1.0/a.png",
            "https://127.0.0.1/a.png",
            "",
        ):
            with self.subTest(url=url):
                self.assertFalse(asset_url_allowed(url))


class HelperTests(unittest.TestCase):
    def test_rgb(self) -> None:
        self.assertEqual(rgb(0xFF9632), (255, 150, 50))
        self.assertEqual(rgb(0x000001), (0, 0, 1))
        self.assertIsNone(rgb(0))
        self.assertIsNone(rgb(None))

    def test_mentioned_ids(self) -> None:
        self.assertEqual(mentioned_ids(["hi <@1> and <@!2>", "<#3> <@&4> <@5>"]), {1, 2, 5})
        self.assertEqual(mentioned_ids(["no mentions"]), set())

    def test_build_card_uses_original_text_and_shares_one_author_object(self) -> None:
        messages = [scout_msg(1, "first", author=10, name="Maple", color=0xFF9632, tag="FOX"), scout_msg(2, "second", author=10, at=105.0), scout_msg(3, "reply", author=20, name="Quill", at=110.0)]
        card = build_card(messages, {10: b"avatar-bytes"}, {7: "Someone"})
        self.assertEqual([m.text for m in card.messages], ["first", "second", "reply"])
        self.assertEqual([m.at for m in card.messages], [100.0, 105.0, 110.0])
        first, second, third = card.messages
        self.assertIs(first.author, second.author)
        self.assertEqual((first.author.name, first.author.color, first.author.tag, first.author.avatar), ("Maple", (255, 150, 50), "FOX", b"avatar-bytes"))
        self.assertEqual((third.author.name, third.author.color, third.author.tag, third.author.avatar), ("Quill", None, None, None))
        self.assertEqual(dict(card.mentions), {7: "Someone"})


if __name__ == "__main__":
    unittest.main()
