"""Self-bot adapter tests. They need discord.py-self, so they only run in .venv-selfbot."""

from __future__ import annotations

import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from bombagif_core import Settings, UserFacingError
from selfbot_logic import NO_MEDIA_TEXT, STOP_CONFIRMATION, WELCOME_TEXT, AllowlistStore, RateLimiter

try:
    metadata.version("discord.py-self")
    HAS_SELFBOT_LIB = True
    import discord
except metadata.PackageNotFoundError:
    HAS_SELFBOT_LIB = False

SETTINGS = Settings(
    discord_token="user-token-secret",
    zipline_token="z",
    zipline_url="https://z.example",
    log_level="INFO",
    allowed_guilds=frozenset(),
    background=(255, 255, 255),
)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_selfbot_constructs_and_closes(self) -> None:
        from selfbot import SelfBot

        with tempfile.TemporaryDirectory() as tmp:
            bot = SelfBot(SETTINGS, AllowlistStore(Path(tmp) / "a.json"))
            await bot.setup_hook()
            self.assertIsNotNone(bot.http_session)
            self.assertFalse(bot.http_session.closed)
            await bot.close()
            self.assertTrue(bot.http_session.closed)


SELF_ID = 1000
LINK = "https://z.example/u/a.gif"


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> None:
        return None


def attachment(name: str = "a.png") -> SimpleNamespace:
    return SimpleNamespace(filename=name, url=f"https://cdn.discordapp.com/{name}", size=10, content_type=None)


def make_message(
    *,
    author_id: int = 99,
    channel_cls: type | None = None,
    content: str = "",
    attachments: list | None = None,
    embeds: list | None = None,
    mentions: list | None = None,
    reference: object | None = None,
    mention_everyone: bool = False,
) -> SimpleNamespace:
    channel = Mock(spec=channel_cls or discord.DMChannel)
    channel.typing = Mock(return_value=_Typing())
    message = SimpleNamespace(
        id=555,
        author=SimpleNamespace(id=author_id, bot=False),
        channel=channel,
        guild=None,
        content=content,
        attachments=attachments or [],
        embeds=embeds or [],
        mentions=mentions or [],
        mention_everyone=mention_everyone,
        reference=reference,
        is_system=lambda: False,
        reply=AsyncMock(),
    )
    return message


class _SelfBotCase(unittest.IsolatedAsyncioTestCase):
    """Builds a SelfBot with a fake identity and a temporary allowlist."""

    async def asyncSetUp(self) -> None:
        from selfbot import SelfBot

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.allowlist = AllowlistStore(Path(self._tmp.name) / "a.json")
        self.bot = SelfBot(SETTINGS, self.allowlist)
        self.me = SimpleNamespace(id=SELF_ID)
        user_patch = patch.object(SelfBot, "user", new_callable=PropertyMock, return_value=self.me)
        user_patch.start()
        self.addCleanup(user_patch.stop)
        await self.bot.setup_hook()
        self.addAsyncCleanup(self.bot.close)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotMessageTests(_SelfBotCase):
    async def test_stranger_dm_gets_welcome_and_is_registered(self) -> None:
        message = make_message(content="hi")
        await self.bot.on_message(message)
        message.reply.assert_awaited_once()
        self.assertEqual(message.reply.await_args.args[0], WELCOME_TEXT)
        self.assertIn(99, self.allowlist)

    async def test_stop_unregisters_and_confirms(self) -> None:
        self.allowlist.add(99)
        message = make_message(content="stop")
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], STOP_CONFIRMATION)
        self.assertNotIn(99, self.allowlist)

    async def test_stranger_ping_in_guild_is_silent(self) -> None:
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me])
        await self.bot.on_message(message)
        message.reply.assert_not_awaited()
        self.assertNotIn(99, self.allowlist)

    async def test_everyone_and_role_mentions_do_not_trigger(self) -> None:
        self.allowlist.add(99)
        message = make_message(channel_cls=discord.TextChannel, mention_everyone=True)
        self.assertFalse(self.bot._incoming(message).mentions_me)

    async def test_direct_mention_and_group_channel_are_mapped(self) -> None:
        from selfbot_logic import ChannelKind

        group = self.bot._incoming(make_message(channel_cls=discord.GroupChannel, mentions=[self.me]))
        self.assertEqual((group.channel, group.mentions_me), (ChannelKind.GROUP, True))
        guild = self.bot._incoming(make_message(channel_cls=discord.TextChannel))
        self.assertEqual(guild.channel, ChannelKind.GUILD)

    async def test_reply_uses_no_mentions(self) -> None:
        message = make_message(content="hi")
        await self.bot.on_message(message)
        kwargs = message.reply.await_args.kwargs
        self.assertIs(kwargs["mention_author"], False)
        allowed = kwargs["allowed_mentions"]
        self.assertFalse(any((allowed.everyone, allowed.users, allowed.roles, allowed.replied_user)))


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotConvertTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.allowlist.add(99)
        convert = patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK)
        self.convert = convert.start()
        self.addCleanup(convert.stop)

    async def test_member_dm_media_replies_with_link(self) -> None:
        message = make_message(attachments=[attachment()])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], LINK)
        self.convert.assert_awaited_once()

    async def test_stranger_dm_with_media_registers_and_converts(self) -> None:
        self.allowlist.remove(99)
        message = make_message(attachments=[attachment()])
        await self.bot.on_message(message)
        self.assertEqual([c.args[0] for c in message.reply.await_args_list], [WELCOME_TEXT, LINK])
        self.assertIn(99, self.allowlist)

    async def test_member_mention_in_guild_converts(self) -> None:
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], attachments=[attachment()])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], LINK)

    async def test_rate_limited_trigger_is_silent(self) -> None:
        self.bot.rate_limiter = RateLimiter(limit=1)
        first, second = make_message(attachments=[attachment()]), make_message(attachments=[attachment()])
        await self.bot.on_message(first)
        await self.bot.on_message(second)
        first.reply.assert_awaited_once()
        second.reply.assert_not_awaited()

    async def test_user_facing_error_is_reported(self) -> None:
        self.convert.side_effect = UserFacingError("That file is too large.")
        message = make_message(attachments=[attachment("big.png")])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], "**big.png**: That file is too large.")

    async def test_unexpected_error_gets_generic_message_and_is_logged(self) -> None:
        self.convert.side_effect = RuntimeError("boom")
        message = make_message(attachments=[attachment("a.png")])
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], "**a.png**: Could not process this file.")

    async def test_no_supported_media_gets_no_media_text(self) -> None:
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], attachments=[attachment("n.txt")])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], NO_MEDIA_TEXT)
        self.convert.assert_not_awaited()

    async def test_batch_is_capped_and_noted(self) -> None:
        message = make_message(attachments=[attachment(f"{i}.png") for i in range(7)])
        await self.bot.on_message(message)
        self.assertEqual(self.convert.await_count, 5)
        self.assertTrue(message.reply.await_args.args[0].endswith("(+2 skipped, max 5 per message)"))


if __name__ == "__main__":
    unittest.main()
