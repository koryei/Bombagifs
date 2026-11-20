"""Self-bot adapter tests. They need discord.py-self, so they only run in .venv-selfbot."""

from __future__ import annotations

import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

from bombagif_core import Settings, UserFacingError
from selfbot_logic import NO_MEDIA_TEXT, STOP_CONFIRMATION, WELCOME_TEXT, AllowlistStore, CaptchaGuard, RateLimiter

try:
    metadata.version("discord.py-self")
    HAS_SELFBOT_LIB = True
    import discord
except metadata.PackageNotFoundError:
    HAS_SELFBOT_LIB = False

SETTINGS = Settings(
    discord_token="user-token-secret",
    zipline_token="zipline-secret-token",
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


def make_captcha() -> Exception:
    """A CaptchaRequired without going through its HTTP-response constructor."""
    return discord.CaptchaRequired.__new__(discord.CaptchaRequired)


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
        author=SimpleNamespace(id=author_id, bot=False, name=f"user{author_id}"),
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

    async def test_dm_text_reply_without_media_is_silent(self) -> None:
        # e.g. a member replying "thanks!" to one of the bot's own messages.
        reference = SimpleNamespace(message_id=42, resolved=SimpleNamespace(attachments=[], embeds=[]), cached_message=None)
        message = make_message(content="thanks!", reference=reference)
        await self.bot.on_message(message)
        message.reply.assert_not_awaited()
        self.convert.assert_not_awaited()

    async def test_batch_is_capped_and_noted(self) -> None:
        message = make_message(attachments=[attachment(f"{i}.png") for i in range(7)])
        await self.bot.on_message(message)
        self.assertEqual(self.convert.await_count, 5)
        self.assertTrue(message.reply.await_args.args[0].endswith("(+2 skipped, max 5 per message)"))

    async def test_link_preview_embed_is_not_a_trigger(self) -> None:
        link_preview = SimpleNamespace(type="link", image=None, video=None, thumbnail=None, url="https://example.com")
        message = make_message(content="look https://example.com", embeds=[link_preview])
        await self.bot.on_message(message)
        message.reply.assert_not_awaited()

    async def test_gif_embed_in_member_dm_converts(self) -> None:
        video = SimpleNamespace(url="https://x", proxy_url="https://images-ext-1.discordapp.net/external/h/https/media.tenor.com/a/tenor")
        gifv = SimpleNamespace(type="gifv", image=None, video=video, thumbnail=None, url="https://tenor.com/view/x")
        message = make_message(embeds=[gifv])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], LINK)
        self.assertTrue(self.convert.await_args.args[3].filename.endswith(".mp4"))

    async def test_reply_failure_does_not_raise(self) -> None:
        response = SimpleNamespace(status=403, reason="Forbidden")
        message = make_message(attachments=[attachment()])
        message.reply.side_effect = discord.Forbidden(response, "no")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)

    async def test_logs_do_not_contain_token(self) -> None:
        self.convert.side_effect = RuntimeError("boom")
        message = make_message(attachments=[attachment()])
        with self.assertLogs(level="DEBUG") as logs:
            await self.bot.on_message(message)
        self.assertNotIn(SETTINGS.discord_token, "\n".join(logs.output))

    async def test_unexpected_handler_bug_does_not_raise(self) -> None:
        message = make_message(content="hi")
        with patch.object(type(self.bot), "_incoming", side_effect=ValueError("bug")):
            with self.assertLogs("bombagif.selfbot", "ERROR"):
                await self.bot.on_message(message)

    async def test_reply_target_media_is_converted(self) -> None:
        target = SimpleNamespace(attachments=[attachment("theirs.png")], embeds=[])
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], reference=SimpleNamespace(message_id=42, resolved=target, cached_message=None))
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], LINK)
        self.assertEqual(self.convert.await_args.args[3].filename, "theirs.png")

    async def test_uncached_reply_target_is_fetched(self) -> None:
        target = SimpleNamespace(attachments=[attachment("fetched.png")], embeds=[])
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], reference=SimpleNamespace(message_id=42, resolved=None, cached_message=None))
        message.channel.fetch_message = AsyncMock(return_value=target)
        await self.bot.on_message(message)
        message.channel.fetch_message.assert_awaited_once_with(42)
        self.assertEqual(self.convert.await_args.args[3].filename, "fetched.png")

    async def test_deleted_reference_falls_back_to_own_media(self) -> None:
        response = SimpleNamespace(status=404, reason="Not Found")
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], attachments=[attachment("mine.png")], reference=SimpleNamespace(message_id=42, resolved=None, cached_message=None))
        message.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(response, "gone"))
        await self.bot.on_message(message)
        self.assertEqual(self.convert.await_args.args[3].filename, "mine.png")

    async def test_deleted_reference_without_media_says_none_found(self) -> None:
        response = SimpleNamespace(status=404, reason="Not Found")
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], reference=SimpleNamespace(message_id=42, resolved=None, cached_message=None))
        message.channel.fetch_message = AsyncMock(side_effect=discord.NotFound(response, "gone"))
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], NO_MEDIA_TEXT)

    async def test_deleted_marker_resolved_value_is_not_media(self) -> None:
        deleted_marker = SimpleNamespace(id=42)  # discord.DeletedReferencedMessage has no attachments
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], attachments=[attachment("mine.png")], reference=SimpleNamespace(message_id=42, resolved=deleted_marker, cached_message=None))
        await self.bot.on_message(message)
        self.assertEqual(self.convert.await_args.args[3].filename, "mine.png")


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotCaptchaGuardTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.allowlist.add(99)
        convert = patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK)
        self.convert = convert.start()
        self.addCleanup(convert.stop)

    async def test_captcha_on_reply_trips_guard(self) -> None:
        message = make_message(attachments=[attachment()])
        message.reply.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertTrue(self.bot.captcha_guard.blocked)
        self.assertEqual(self.bot.captcha_guard.reason, "sending a reply")

    async def test_tripped_guard_pauses_everything(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("test")
        member = make_message(attachments=[attachment()])
        stranger = make_message(author_id=123, content="hi")
        await self.bot.on_message(member)
        await self.bot.on_message(stranger)
        member.reply.assert_not_awaited()
        stranger.reply.assert_not_awaited()
        self.convert.assert_not_awaited()
        self.assertNotIn(123, self.allowlist)

    async def test_captcha_while_fetching_reply_target_trips_guard(self) -> None:
        reference = SimpleNamespace(message_id=42, resolved=None, cached_message=None)
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me], reference=reference)
        message.channel.fetch_message = AsyncMock(side_effect=make_captcha())
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertEqual(self.bot.captcha_guard.reason, "fetching a message")
        message.reply.assert_not_awaited()

    async def test_captcha_from_typing_trips_guard(self) -> None:
        class _CaptchaTyping:
            async def __aenter__(self) -> None:
                raise make_captcha()

            async def __aexit__(self, *exc_info: object) -> None:
                return None

        message = make_message(attachments=[attachment()])
        message.channel.typing = Mock(return_value=_CaptchaTyping())
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertTrue(self.bot.captcha_guard.blocked)

    async def test_persisted_guard_is_loaded_at_startup(self) -> None:
        from selfbot import SelfBot

        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("test")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            fresh = SelfBot(SETTINGS, self.allowlist)
        self.addAsyncCleanup(fresh.close)
        self.assertTrue(fresh.captcha_guard.blocked)

    async def test_other_http_errors_do_not_trip_guard(self) -> None:
        message = make_message(attachments=[attachment()])
        message.reply.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertFalse(self.bot.captcha_guard.blocked)


def make_relationship(kind: object, user_id: int = 5) -> SimpleNamespace:
    return SimpleNamespace(type=kind, user=SimpleNamespace(id=user_id), accept=AsyncMock())


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotFriendRequestTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.auto_accept_friends = True
        self.bot.friend_accept_delay = 0
        self.incoming = discord.RelationshipType.incoming_request

    async def test_incoming_request_is_accepted_when_enabled(self) -> None:
        relationship = make_relationship(self.incoming)
        await self.bot.on_relationship_add(relationship)
        relationship.accept.assert_awaited_once()

    async def test_disabled_by_default(self) -> None:
        self.bot.auto_accept_friends = False
        relationship = make_relationship(self.incoming)
        await self.bot.on_relationship_add(relationship)
        relationship.accept.assert_not_awaited()

    async def test_only_incoming_requests_are_accepted(self) -> None:
        for kind in (discord.RelationshipType.friend, discord.RelationshipType.outgoing_request, discord.RelationshipType.blocked):
            with self.subTest(kind=kind):
                relationship = make_relationship(kind)
                await self.bot.on_relationship_add(relationship)
                relationship.accept.assert_not_awaited()

    async def test_accepting_does_not_register_the_user(self) -> None:
        await self.bot.on_relationship_add(make_relationship(self.incoming, user_id=5))
        self.assertNotIn(5, self.allowlist)

    async def test_captcha_on_accept_trips_guard_and_stops(self) -> None:
        relationship = make_relationship(self.incoming)
        relationship.accept.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_relationship_add(relationship)
        self.assertEqual(self.bot.captcha_guard.reason, "accepting a friend request")
        later = make_relationship(self.incoming, user_id=6)
        await self.bot.on_relationship_add(later)
        later.accept.assert_not_awaited()

    async def test_other_errors_are_logged_without_tripping(self) -> None:
        relationship = make_relationship(self.incoming)
        relationship.accept.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_relationship_add(relationship)
        self.assertFalse(self.bot.captcha_guard.blocked)

    async def test_ready_sweeps_pending_requests_up_to_the_cap(self) -> None:
        from selfbot import SelfBot

        pending = [make_relationship(self.incoming, user_id=i) for i in range(15)]
        friend = make_relationship(discord.RelationshipType.friend, user_id=99)
        with patch.object(SelfBot, "relationships", new_callable=PropertyMock, return_value=[friend, *pending]):
            await self.bot.on_ready()
        accepted = [r for r in pending if r.accept.await_count == 1]
        self.assertEqual(len(accepted), self.bot.friend_sweep_limit)
        friend.accept.assert_not_awaited()


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotStatsTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.allowlist.add(99)
        convert = patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK)
        self.convert = convert.start()
        self.addCleanup(convert.stop)

    async def test_successful_conversion_is_counted_and_listed(self) -> None:
        await self.bot.on_message(make_message(attachments=[attachment("cat.png")]))
        stats = self.bot.stats
        self.assertEqual((stats.triggers, stats.conversions_ok, stats.conversions_failed), (1, 1, 0))
        last = stats.activity[-1]
        self.assertEqual(last.level, "ok")
        self.assertIn("cat.png", last.text)
        self.assertIn("user99", last.text)

    async def test_failed_conversion_is_counted(self) -> None:
        self.convert.side_effect = UserFacingError("too big")
        await self.bot.on_message(make_message(attachments=[attachment("big.png")]))
        self.assertEqual((self.bot.stats.conversions_ok, self.bot.stats.conversions_failed), (0, 1))
        self.assertEqual(self.bot.stats.activity[-1].level, "fail")
        self.assertIn("too big", self.bot.stats.activity[-1].text)

    async def test_rate_limited_trigger_is_counted_separately(self) -> None:
        self.bot.rate_limiter = RateLimiter(limit=1)
        for _ in range(2):
            await self.bot.on_message(make_message(attachments=[attachment()]))
        self.assertEqual((self.bot.stats.triggers, self.bot.stats.rate_limited), (1, 1))

    async def test_registration_and_stop_are_counted(self) -> None:
        await self.bot.on_message(make_message(author_id=123, content="hi"))
        await self.bot.on_message(make_message(author_id=123, content="stop"))
        self.assertEqual((self.bot.stats.registered, self.bot.stats.unregistered), (1, 1))
        self.assertEqual([e.level for e in list(self.bot.stats.activity)[-2:]], ["info", "info"])

    async def test_accepted_friend_request_is_counted(self) -> None:
        self.bot.auto_accept_friends = True
        self.bot.friend_accept_delay = 0
        relationship = make_relationship(discord.RelationshipType.incoming_request)
        await self.bot.on_relationship_add(relationship)
        self.assertEqual(self.bot.stats.friends_accepted, 1)

    async def test_captcha_trip_shows_a_warning_in_the_feed(self) -> None:
        message = make_message(attachments=[attachment()])
        message.reply.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertEqual(self.bot.stats.activity[-1].level, "warn")
        self.assertIn("CAPTCHA", self.bot.stats.activity[-1].text)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotTerminalUiTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ui = Mock()
        self.bot.ui = self.ui

    async def test_on_ready_shows_the_boot_banner_once(self) -> None:
        await self.bot.on_ready()
        await self.bot.on_ready()  # reconnects must not reprint it
        self.ui.show_boot.assert_called_once()

    async def test_boot_info_has_useful_rows_and_no_secrets(self) -> None:
        await self.bot.on_ready()
        info = self.ui.show_boot.call_args.args[0]
        for label in ("account", "zipline", "allowlist", "auto-accept friends", "captcha guard", "rate limit"):
            self.assertIn(label, info)
        blob = " ".join(str(v) for v in info.values())
        self.assertNotIn(SETTINGS.discord_token, blob)
        self.assertNotIn(SETTINGS.zipline_token, blob)
        self.assertIn("z.example", info["zipline"])
        self.assertNotIn("https://", info["zipline"])

    async def test_boot_info_reflects_a_tripped_guard(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("sending a reply")
        await self.bot.on_ready()
        self.assertIn("paused", self.ui.show_boot.call_args.args[0]["captcha guard"])

    async def test_ui_status_reflects_guard_and_connection(self) -> None:
        self.assertEqual(self.bot.ui_status(), "connecting")  # not ready: never logged in
        with patch.object(type(self.bot), "is_ready", return_value=True):
            self.assertEqual(self.bot.ui_status(), "online")
            with self.assertLogs("bombagif.selfbot", "ERROR"):
                self.bot.captcha_guard.trip("x")
            self.assertEqual(self.bot.ui_status(), "paused")

    async def test_close_stops_the_ui(self) -> None:
        await self.bot.close()
        self.ui.stop.assert_called_once_with(self.bot.stats)


if __name__ == "__main__":
    unittest.main()
