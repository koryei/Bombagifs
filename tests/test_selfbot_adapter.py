"""Self-bot adapter tests. They need discord.py-self, so they only run in .venv-selfbot."""

from __future__ import annotations

import asyncio
import datetime
import json
import struct
import tempfile
import unittest
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, PropertyMock, patch

import aiohttp

from bombagif_core import Settings, UserFacingError
from scout import ANNOUNCE_OFF, ANNOUNCE_ON, HELP_OWNER, HELP_USER, ClipPlan, ReviewItem, ScoutRuntime, format_clip_notice
from scout_buffer import ScoutMessage, Version
from scout_judge import Verdict
from scout_buffer import ScoutBuffer
from scout_config import read_scout_config
from scout_manual import REFUSAL_CANT, REFUSAL_NOT_ENABLED, REFUSAL_NOT_TEXT, REFUSAL_OPTED_OUT, REFUSAL_SLOW
from scout_stores import ClipLedger, ClipRecord, OptOutStore, ReviewStore, ScoutChatStore
from selfbot_logic import (
    CLIP_OPT_OUT_CONFIRMATION,
    NO_MEDIA_TEXT,
    START_CONFIRMATION,
    STOP_CONFIRMATION,
    WELCOME_TEXT,
    AllowlistStore,
    RateLimiter,
    help_text,
    welcome_text,
)

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
    channel_id: int = 777,
    nsfw: bool = False,
    stickers: list | None = None,
    message_id: int = 555,
    created: float = 1_700_000_000.0,
    author_extra: dict | None = None,
) -> SimpleNamespace:
    channel = Mock(spec=channel_cls or discord.DMChannel)
    channel.typing = Mock(return_value=_Typing())
    channel.id = channel_id
    channel.is_nsfw = Mock(return_value=nsfw)
    author = SimpleNamespace(
        id=author_id,
        bot=False,
        name=f"user{author_id}",
        display_avatar=SimpleNamespace(
            with_size=lambda size: SimpleNamespace(url=f"https://cdn.discordapp.com/avatars/{author_id}/h.png?size={size}")
        ),
    )
    for key, value in (author_extra or {}).items():
        setattr(author, key, value)
    message = SimpleNamespace(
        id=message_id,
        author=author,
        channel=channel,
        guild=None,
        content=content,
        attachments=attachments or [],
        embeds=embeds or [],
        stickers=stickers or [],
        mentions=mentions or [],
        mention_everyone=mention_everyone,
        reference=reference,
        created_at=datetime.datetime.fromtimestamp(created, datetime.timezone.utc),
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

    async def test_stop_unregisters_and_confirms(self) -> None:  # also covers the v2 flow without a scout
        self.allowlist.add(99)
        message = make_message(content="stop")
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], STOP_CONFIRMATION)
        self.assertNotIn(99, self.allowlist)

    async def test_stranger_ping_in_guild_gets_help_and_is_not_registered(self) -> None:
        message = make_message(channel_cls=discord.TextChannel, mentions=[self.me])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], help_text(False))
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
        self.assertEqual(message.reply.await_args.args[0], NO_MEDIA_TEXT + " (I can't read `.txt` files.)")
        self.convert.assert_not_awaited()

    async def test_dm_text_reply_without_media_is_silent(self) -> None:
        # e.g. a member replying "thanks!" to one of the bot's own messages. Discord counts the reply as a
        # mention of the replied-to author, which is not an ask.
        reference = SimpleNamespace(message_id=42, resolved=SimpleNamespace(attachments=[], embeds=[]), cached_message=None)
        message = make_message(content="thanks!", reference=reference, mentions=[self.me])
        await self.bot.on_message(message)
        message.reply.assert_not_awaited()
        self.convert.assert_not_awaited()

    async def test_dm_reply_to_a_media_message_still_converts(self) -> None:
        reference = SimpleNamespace(message_id=42, resolved=SimpleNamespace(attachments=[attachment("b.png")], embeds=[]), cached_message=None)
        message = make_message(content="please", reference=reference, mentions=[self.me])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], LINK)
        self.convert.assert_awaited_once()

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
class SelfBotUpdateNoticeTests(_SelfBotCase):
    """An unattended update leaves a notice on disk; the bot DMs it to everyone on the allowlist."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.bot.update_notice_delay = 0  # no pacing between DMs in tests

    def write_notice(self, **overrides: object) -> Path:
        notice = {
            "from": "f9ee931",
            "to": "6160b39",
            "outcome": "updated",
            "at": 1_763_866_490,
            "commits": [{"sha": "6160b39", "subject": "Let a training phase review every judged run"}],
        }
        notice.update(overrides)
        path = self.allowlist.path.with_name("update-notice.json")
        path.write_text(json.dumps(notice), encoding="utf-8")
        return path

    def fake_user(self, user_id: int) -> SimpleNamespace:
        return SimpleNamespace(id=user_id, send=AsyncMock())

    def patch_users(self, users: dict[int, SimpleNamespace]) -> None:
        from selfbot import SelfBot

        patcher = patch.object(SelfBot, "get_user", side_effect=lambda user_id: users.get(user_id))
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_every_allowlisted_user_gets_the_update(self) -> None:
        self.allowlist.add(11)
        self.allowlist.add(22)
        path = self.write_notice()
        users = {11: self.fake_user(11), 22: self.fake_user(22)}
        self.patch_users(users)
        await self.bot.on_ready()
        for user in users.values():
            user.send.assert_awaited_once()
            self.assertIn("Bombagif updated itself", user.send.await_args.args[0])
        self.assertFalse(path.exists(), "the notice is consumed once it has been delivered")

    async def test_no_notice_means_no_dms(self) -> None:
        self.allowlist.add(11)
        user = self.fake_user(11)
        self.patch_users({11: user})
        await self.bot.on_ready()
        user.send.assert_not_awaited()

    async def test_one_failed_dm_does_not_stop_the_others(self) -> None:
        self.allowlist.add(11)
        self.allowlist.add(22)
        path = self.write_notice()
        broken = self.fake_user(11)
        broken.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        working = self.fake_user(22)
        self.patch_users({11: broken, 22: working})
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_ready()
        working.send.assert_awaited_once()
        self.assertFalse(path.exists(), "one delivery is enough to consume the notice")

    async def test_a_notice_nobody_received_is_kept_for_the_next_boot(self) -> None:
        self.allowlist.add(11)
        path = self.write_notice()
        broken = self.fake_user(11)
        broken.send.side_effect = discord.HTTPException(SimpleNamespace(status=500, reason="boom"), "boom")
        self.patch_users({11: broken})
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_ready()
        self.assertTrue(path.exists(), "an undelivered notice is retried on the next boot")

    async def test_a_malformed_notice_is_dropped_without_a_dm(self) -> None:
        self.allowlist.add(11)
        path = self.allowlist.path.with_name("update-notice.json")
        path.write_text("{not json", encoding="utf-8")
        user = self.fake_user(11)
        self.patch_users({11: user})
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_ready()
        user.send.assert_not_awaited()
        self.assertFalse(path.exists(), "a notice that can never be read is removed")

    async def test_the_update_is_announced_once_per_process(self) -> None:
        self.allowlist.add(11)
        self.write_notice()
        user = self.fake_user(11)
        self.patch_users({11: user})
        await self.bot.on_ready()
        await self.bot.on_ready()  # a reconnect fires on_ready again
        user.send.assert_awaited_once()

    async def test_a_captcha_while_announcing_pauses_the_account(self) -> None:
        self.allowlist.add(11)
        self.write_notice()
        user = self.fake_user(11)
        user.send.side_effect = make_captcha()
        self.patch_users({11: user})
        await self.bot.on_ready()
        self.assertTrue(self.bot.captcha_guard.blocked)

    async def test_announcements_can_be_turned_off(self) -> None:
        self.allowlist.add(11)
        path = self.write_notice()
        self.bot.announce_updates = False
        user = self.fake_user(11)
        self.patch_users({11: user})
        await self.bot.on_ready()
        user.send.assert_not_awaited()
        self.assertFalse(path.exists(), "an opted-out notice is dropped, not retried forever")

    async def test_create_bot_reads_the_opt_out_from_the_environment(self) -> None:
        from selfbot import create_bot

        quiet = create_bot(SETTINGS, Path(self._tmp.name), {"SELFBOT_UPDATE_NOTICES": "false"})
        self.addCleanup(quiet.executor.shutdown, False)
        self.assertFalse(quiet.announce_updates)
        loud = create_bot(SETTINGS, Path(self._tmp.name), {})
        self.addCleanup(loud.executor.shutdown, False)
        self.assertTrue(loud.announce_updates)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotBusyTests(_SelfBotCase):
    """In-flight work is mirrored to data/busy.json so an update can wait for it."""

    def marker(self) -> dict:
        return json.loads(self.allowlist.path.with_name("busy.json").read_text(encoding="utf-8"))

    async def test_a_conversion_marks_the_bot_busy(self) -> None:
        self.allowlist.add(99)
        seen: list[int] = []

        async def convert(*args: object, **kwargs: object) -> str:
            seen.append(self.marker()["jobs"])
            return LINK

        with patch("selfbot.convert_and_upload", side_effect=convert):
            await self.bot.on_message(make_message(attachments=[attachment("cat.png")]))
        self.assertEqual(seen, [1], "the conversion runs while the marker says one job")
        self.assertEqual(self.marker()["jobs"], 0, "the marker is cleared afterwards")

    async def test_a_message_with_nothing_to_do_leaves_the_marker_clear(self) -> None:
        self.allowlist.add(99)
        await self.bot.on_message(make_message(content="hello"))
        self.assertEqual(self.marker()["jobs"], 0)


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


CHAT = 777
NOW = 1_700_000_000.0


def make_runtime(root: Path, *, mode: str = "dry-run", enabled: tuple[int, ...] = (CHAT,), **env: str) -> ScoutRuntime:
    cfg = read_scout_config({"SELFBOT_SCOUT_MODE": mode, **env})
    optout = OptOutStore(root / "optout.json")
    chats = ScoutChatStore(root / "scout-chats.json", seed=cfg.seed_chats)
    for chat_id in enabled:
        chats.enable(chat_id, "test chat")
    buffer = ScoutBuffer(
        max_age_s=10**9, max_per_chat=100, is_opted_out=lambda user_id: user_id in optout, clock=lambda: NOW + 10
    )
    from scout_taste import TasteNotes, TasteStore

    return ScoutRuntime(
        cfg=cfg, buffer=buffer, chats=chats, optout=optout, ledger=ClipLedger(root / "clips.jsonl"),
        taste_notes=TasteNotes(root / "scout-taste.txt"), taste_store=TasteStore(root / "taste.jsonl", cfg.examples_per_label),
        review_store=ReviewStore(root / "reviews.json"),
    )


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotScoutBufferTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name))
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.addAsyncCleanup(self.bot.close)

    def buffered(self, message_id: int = 555, chat: int = CHAT):
        return self.runtime.buffer.get(chat, message_id)

    async def test_enabled_guild_message_is_buffered_with_display_fields(self) -> None:
        color = SimpleNamespace(value=0xFF9632)
        tag = SimpleNamespace(tag="DNA", identity_enabled=True)
        message = make_message(
            channel_cls=discord.TextChannel,
            content="the better one was me",
            author_extra={"display_name": "Pranit", "color": color, "primary_guild": tag},
        )
        await self.bot.on_message(message)
        found = self.buffered()
        self.assertIsNotNone(found)
        self.assertEqual((found.author_id, found.author_name, found.original, found.eligible), (99, "Pranit", "the better one was me", True))
        self.assertEqual((found.color, found.tag, found.chat_id, found.created_at), (0xFF9632, "DNA", CHAT, NOW))
        self.assertEqual(found.avatar_url, "https://cdn.discordapp.com/avatars/99/h.png?size=128")

    async def test_missing_display_fields_fall_back_safely(self) -> None:
        message = make_message(channel_cls=discord.TextChannel, content="hello there")
        await self.bot.on_message(message)
        found = self.buffered()
        self.assertEqual((found.author_name, found.color, found.tag), ("user99", None, None))

    async def test_zero_colour_and_disabled_tag_mean_none(self) -> None:
        message = make_message(
            channel_cls=discord.TextChannel,
            content="hello there",
            author_extra={"color": SimpleNamespace(value=0), "primary_guild": SimpleNamespace(tag="X", identity_enabled=False)},
        )
        await self.bot.on_message(message)
        found = self.buffered()
        self.assertEqual((found.color, found.tag), (None, None))

    async def test_disabled_chat_is_not_buffered(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="hello there", channel_id=123))
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_one_to_one_dm_is_never_buffered(self) -> None:
        self.runtime.chats.enable(CHAT)
        await self.bot.on_message(make_message(channel_cls=discord.DMChannel, content="hello there"))
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_nsfw_channel_is_never_buffered(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="hello there", nsfw=True))
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_group_dm_is_buffered_when_enabled(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.GroupChannel, content="hello there"))
        self.assertIsNotNone(self.buffered())

    async def test_attachment_url_sticker_and_command_messages_are_ineligible_placeholders(self) -> None:
        cases = {
            1: make_message(channel_cls=discord.TextChannel, content="look at this", attachments=[attachment()], message_id=1),
            2: make_message(channel_cls=discord.TextChannel, content="see https://example.com", message_id=2),
            3: make_message(channel_cls=discord.TextChannel, content="nice sticker", stickers=[object()], message_id=3),
            4: make_message(channel_cls=discord.TextChannel, content="!on", message_id=4),
            5: make_message(channel_cls=discord.TextChannel, content="ok", message_id=5),
        }
        for message in cases.values():
            await self.bot.on_message(message)
        self.assertEqual([self.buffered(i).eligible for i in cases], [False, False, False, False, False])
        self.assertEqual(self.buffered(1).original, "")  # no text is kept for non-text messages

    async def test_opted_out_author_is_not_buffered(self) -> None:
        self.runtime.optout.add(99)
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="hello there"))
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_bot_and_own_messages_are_not_buffered(self) -> None:
        bot_message = make_message(channel_cls=discord.TextChannel, content="beep boop here", message_id=1)
        bot_message.author.bot = True
        own = make_message(author_id=SELF_ID, channel_cls=discord.TextChannel, content="my own line", message_id=2)
        await self.bot.on_message(bot_message)
        await self.bot.on_message(own)
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_buffering_continues_while_the_captcha_guard_is_tripped(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("test")
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="hello there"))
        self.assertIsNotNone(self.buffered())

    async def test_raw_edit_appends_version_from_payload_data_content(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="the real joke"))
        await self.bot.on_raw_message_edit(SimpleNamespace(channel_id=CHAT, message_id=555, data={"content": "nothing here"}))
        self.assertEqual([v.content for v in self.buffered().versions], ["the real joke", "nothing here"])
        self.assertEqual(self.buffered().original, "the real joke")

    async def test_raw_edit_without_content_is_ignored(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="the real joke"))
        await self.bot.on_raw_message_edit(SimpleNamespace(channel_id=CHAT, message_id=555, data={"embeds": []}))
        self.assertEqual(len(self.buffered().versions), 1)

    async def test_raw_delete_marks_deleted_even_without_cached_message(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="oops wrong chat"))
        await self.bot.on_raw_message_delete(SimpleNamespace(channel_id=CHAT, message_id=555, cached_message=None))
        self.assertTrue(self.buffered().deleted)
        self.assertEqual(self.buffered().original, "oops wrong chat")

    async def test_bulk_delete_marks_all(self) -> None:
        for i in (1, 2, 3):
            await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content=f"line number {i}", message_id=i))
        await self.bot.on_raw_bulk_message_delete(SimpleNamespace(channel_id=CHAT, message_ids={1, 3}, cached_messages=[]))
        self.assertEqual([self.buffered(i).deleted for i in (1, 2, 3)], [True, False, True])

    async def test_raw_reaction_laugh_emoji_counts(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="a funny line"))
        payload = lambda emoji, user=5: SimpleNamespace(channel_id=CHAT, message_id=555, user_id=user, emoji=emoji)  # noqa: E731
        await self.bot.on_raw_reaction_add(payload("😂"))
        await self.bot.on_raw_reaction_add(payload("💀", user=6))
        await self.bot.on_raw_reaction_add(payload("❤️"))
        self.assertEqual(self.buffered().laugh_reactions, 2)
        await self.bot.on_raw_reaction_remove(payload("😂"))
        self.assertEqual(self.buffered().laugh_reactions, 1)

    async def test_own_reactions_are_ignored(self) -> None:
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="a funny line"))
        await self.bot.on_raw_reaction_add(SimpleNamespace(channel_id=CHAT, message_id=555, user_id=SELF_ID, emoji="😂"))
        self.assertEqual(self.buffered().laugh_reactions, 0)

    async def test_events_for_unbuffered_messages_are_ignored_without_error(self) -> None:
        await self.bot.on_raw_message_edit(SimpleNamespace(channel_id=CHAT, message_id=404, data={"content": "x"}))
        await self.bot.on_raw_message_delete(SimpleNamespace(channel_id=CHAT, message_id=404, cached_message=None))
        await self.bot.on_raw_bulk_message_delete(SimpleNamespace(channel_id=9, message_ids={1}, cached_messages=[]))
        await self.bot.on_raw_reaction_add(SimpleNamespace(channel_id=CHAT, message_id=404, user_id=5, emoji="😂"))
        await self.bot.on_raw_reaction_remove(SimpleNamespace(channel_id=CHAT, message_id=404, user_id=5, emoji="😂"))
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_scout_off_means_no_buffering_and_handlers_do_nothing(self) -> None:
        from selfbot import SelfBot

        for scout in (None, make_runtime(Path(self._tmp.name), mode="off")):
            bot = SelfBot(SETTINGS, self.allowlist, scout=scout)
            self.addAsyncCleanup(bot.close)
            await bot.on_message(make_message(channel_cls=discord.TextChannel, content="hello there"))
            await bot.on_raw_message_edit(SimpleNamespace(channel_id=CHAT, message_id=555, data={"content": "x"}))
            await bot.on_raw_message_delete(SimpleNamespace(channel_id=CHAT, message_id=555, cached_message=None))
            await bot.on_raw_reaction_add(SimpleNamespace(channel_id=CHAT, message_id=555, user_id=5, emoji="😂"))
            if scout is not None:
                self.assertEqual(scout.buffer.chats(), [])

    async def test_a_buffering_failure_never_blocks_normal_handling(self) -> None:
        self.allowlist.add(99)
        message = make_message(channel_cls=discord.TextChannel, attachments=[attachment()], mentions=[self.me])
        with patch.object(self.runtime.buffer, "add", side_effect=RuntimeError("boom")):
            with patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK):
                with self.assertLogs("bombagif.selfbot", "ERROR"):
                    await self.bot.on_message(message)
        message.reply.assert_awaited_once()  # the v2 conversion flow still ran


def smsg(id: int, text: str, *, author: int = 10, name: str | None = None, deleted: bool = False, color: int | None = None) -> ScoutMessage:
    return ScoutMessage(
        id=id, chat_id=CHAT, author_id=author, author_name=name or f"user{author}",
        avatar_url=f"https://cdn.discordapp.com/avatars/{author}/h.png?size=128", color=color, tag=None,
        created_at=NOW + id, versions=[Version(text, NOW + id)], deleted_at=NOW + 99 if deleted else None,
    )


def plan_of(*messages: ScoutMessage, to_owner: bool = False) -> ClipPlan:
    verdict = Verdict(True, 0, len(messages) - 1, 9.0, True, (), "funny", {"funny": 0.9}, "fake")
    return ClipPlan(CHAT, tuple(messages), verdict, any(m.deleted for m in messages), to_owner)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotPostTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name), mode="on", SELFBOT_OWNER_ID="4242")
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot.close)
        self.partial = Mock()
        self.partial.reply = AsyncMock(return_value=SimpleNamespace(id=900))
        self.partial.jump_url = "https://discord.com/channels/1/777/10"
        self.channel = Mock()
        self.channel.get_partial_message = Mock(return_value=self.partial)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=901))
        self.bot.get_channel = Mock(return_value=self.channel)  # type: ignore[method-assign]
        self.render = patch("selfbot.render_clip", return_value=b"\x89PNG fake").start()
        self.addCleanup(patch.stopall)
        self.fetch = patch.object(self.bot, "_fetch_asset", AsyncMock(return_value=None)).start()

    async def test_post_replies_to_last_non_deleted_message_with_png_and_notice(self) -> None:
        plan = plan_of(smsg(10, "first line"), smsg(11, "second line", deleted=True))
        self.assertEqual(await self.bot._post_clip(plan), 900)
        self.channel.get_partial_message.assert_called_once_with(10)
        kwargs = self.partial.reply.await_args.kwargs
        self.assertEqual(kwargs["content"], format_clip_notice(["user10", "user10"], True))
        self.assertIs(kwargs["mention_author"], False)
        self.assertEqual(kwargs["file"].filename, "clip.png")
        self.channel.send.assert_not_awaited()

    async def test_clip_is_rendered_at_scale_2_with_the_configured_theme(self) -> None:
        await self.bot._post_clip(plan_of(smsg(10, "a line")))
        self.assertEqual(self.render.call_args.kwargs["scale"], 2)
        self.assertEqual(self.render.call_args.kwargs["theme"], self.runtime.cfg.theme)

    def group_channel(self) -> Mock:
        """A real GroupChannel has no get_partial_message (the spec'd mock raises AttributeError for it too)."""
        group = Mock(spec=discord.GroupChannel)
        group.id = CHAT
        group.send = AsyncMock(return_value=SimpleNamespace(id=902))
        self.bot.get_channel = Mock(return_value=group)  # type: ignore[method-assign]
        return group

    async def test_group_chats_reply_with_a_message_reference(self) -> None:
        group = self.group_channel()
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "a line"), smsg(11, "second", deleted=True))), 902)
        group.send.assert_awaited_once()
        kwargs = group.send.await_args.kwargs
        self.assertEqual((kwargs["reference"].message_id, kwargs["reference"].channel_id), (10, CHAT))
        self.assertIs(kwargs["mention_author"], False)
        self.assertEqual(kwargs["file"].filename, "clip.png")
        self.assertIn("**user10**", kwargs["content"])
        allowed = kwargs["allowed_mentions"]
        self.assertFalse(any((allowed.everyone, allowed.users, allowed.roles, allowed.replied_user)))
        self.assertIn(902, self.bot._clip_message_ids)

    async def test_a_group_reply_to_a_vanished_message_falls_back_to_a_plain_send(self) -> None:
        group = self.group_channel()
        group.send.side_effect = [discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message"), SimpleNamespace(id=903)]
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "a line"))), 903)
        self.assertEqual(group.send.await_count, 2)
        self.assertNotIn("reference", group.send.await_args.kwargs)

    async def test_a_group_clip_of_only_deleted_messages_is_a_plain_send(self) -> None:
        group = self.group_channel()
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "gone", deleted=True))), 902)
        self.assertNotIn("reference", group.send.await_args.kwargs)

    async def test_a_group_captcha_trips_the_guard(self) -> None:
        group = self.group_channel()
        group.send.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.assertIsNone(await self.bot._post_clip(plan_of(smsg(10, "a line"))))
        self.assertEqual(self.bot.captcha_guard.reason, "posting a clip")

    async def test_owner_route_builds_a_jump_link_for_group_chats(self) -> None:
        self.group_channel()
        owner = Mock()
        owner.send = AsyncMock(return_value=SimpleNamespace(id=777000))
        self.bot.get_user = Mock(return_value=owner)  # type: ignore[method-assign]
        await self.bot._post_clip(plan_of(smsg(10, "regretted", deleted=True), to_owner=True))
        self.assertIn(f"https://discord.com/channels/@me/{CHAT}/10", owner.send.await_args.kwargs["content"])

    async def test_all_deleted_uses_channel_send(self) -> None:
        plan = plan_of(smsg(10, "gone", deleted=True), smsg(11, "also gone", deleted=True))
        self.assertEqual(await self.bot._post_clip(plan), 901)
        self.partial.reply.assert_not_awaited()
        self.assertEqual(self.channel.send.await_args.kwargs["file"].filename, "clip.png")

    async def test_unknown_message_error_falls_back_to_send(self) -> None:
        self.partial.reply.side_effect = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "a line"))), 901)
        self.channel.send.assert_awaited_once()

    async def test_invalid_form_body_reply_error_also_falls_back_to_send(self) -> None:
        error = discord.HTTPException(SimpleNamespace(status=400, reason="Bad Request"), {"code": 50035, "message": "Invalid Form Body"})
        self.partial.reply.side_effect = error
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "a line"))), 901)

    async def test_other_http_errors_return_none_and_are_logged(self) -> None:
        self.partial.reply.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.assertIsNone(await self.bot._post_clip(plan_of(smsg(10, "a line"))))
        self.channel.send.assert_not_awaited()

    async def test_notice_names_all_authors_without_mentions(self) -> None:
        plan = plan_of(smsg(10, "setup", author=10, name="Pranit"), smsg(11, "punch", author=20, name="<@5>"))
        await self.bot._post_clip(plan)
        kwargs = self.partial.reply.await_args.kwargs
        self.assertIn("**Pranit**", kwargs["content"])
        self.assertNotIn("<@5>", kwargs["content"])
        allowed = kwargs["allowed_mentions"]
        self.assertFalse(any((allowed.everyone, allowed.users, allowed.roles, allowed.replied_user)))

    async def test_to_owner_dms_the_owner_only(self) -> None:
        owner = Mock()
        owner.send = AsyncMock(return_value=SimpleNamespace(id=777000))
        self.bot.get_user = Mock(return_value=owner)  # type: ignore[method-assign]
        plan = plan_of(smsg(10, "regretted", deleted=True), to_owner=True)
        self.assertEqual(await self.bot._post_clip(plan), 777000)
        self.bot.get_user.assert_called_once_with(4242)
        kwargs = owner.send.await_args.kwargs
        self.assertIn("https://discord.com/channels/1/777/10", kwargs["content"])
        self.assertIn("**user10**", kwargs["content"])
        self.partial.reply.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertNotIn(777000, self.bot._clip_message_ids)  # owner DMs are not public clips

    async def test_to_owner_without_an_owner_id_returns_none(self) -> None:
        self.runtime.cfg = read_scout_config({"SELFBOT_SCOUT_MODE": "on"})
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            self.assertIsNone(await self.bot._post_clip(plan_of(smsg(10, "x", deleted=True), to_owner=True)))

    async def test_captcha_while_posting_trips_the_guard_and_returns_none(self) -> None:
        self.partial.reply.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.assertIsNone(await self.bot._post_clip(plan_of(smsg(10, "a line"))))
        self.assertEqual(self.bot.captcha_guard.reason, "posting a clip")

    async def test_asset_fetch_failure_still_posts(self) -> None:
        self.fetch.side_effect = RuntimeError("cdn down")
        self.assertEqual(await self.bot._post_clip(plan_of(smsg(10, "a line 😂 <:pog:123>"))), 900)

    async def test_avatars_and_emoji_are_prefetched_and_passed_to_the_renderer(self) -> None:
        async def fake_fetch(url: str) -> bytes | None:
            return b"emoji-bytes" if "emojis" in url or "twemoji" in url else b"avatar-bytes"

        self.fetch.side_effect = fake_fetch
        await self.bot._post_clip(plan_of(smsg(10, "hi 😂 <:pog:123>")))
        card = self.render.call_args.args[0]
        self.assertEqual(card.messages[0].author.avatar, b"avatar-bytes")
        emoji = self.render.call_args.kwargs["emoji"]
        self.assertEqual(emoji, {"1f602": b"emoji-bytes", "custom:123": b"emoji-bytes"})

    async def test_mentions_are_resolved_from_authors_and_the_user_cache(self) -> None:
        self.bot.get_user = Mock(side_effect=lambda uid: SimpleNamespace(display_name="Cached") if uid == 3 else None)  # type: ignore[method-assign]
        await self.bot._post_clip(plan_of(smsg(10, "yo <@20> and <@3> and <@9>", author=10), smsg(11, "hey", author=20, name="Quill")))
        self.assertEqual(dict(self.render.call_args.args[0].mentions), {20: "Quill", 3: "Cached"})

    async def test_successful_posts_remember_the_message_id(self) -> None:
        await self.bot._post_clip(plan_of(smsg(10, "a line")))
        self.assertIn(900, self.bot._clip_message_ids)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotAssetFetchTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name), mode="on")
        self.runtime.emoji_cache = Path(self._tmp.name) / "emoji-cache"
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot.close)
        self.download = patch("selfbot.download_url", new_callable=AsyncMock, return_value=b"image-bytes").start()
        self.addCleanup(patch.stopall)

    async def test_asset_hosts_are_restricted(self) -> None:
        self.assertIsNone(await self.bot._fetch_asset("https://evil.example/a.png"))
        self.assertIsNone(await self.bot._fetch_asset("http://127.0.0.1/a.png"))
        self.download.assert_not_awaited()
        self.assertEqual(await self.bot._fetch_asset("https://cdn.discordapp.com/avatars/1/h.png"), b"image-bytes")
        self.download.assert_awaited_once()

    async def test_download_errors_become_none(self) -> None:
        self.download.side_effect = RuntimeError("boom")
        self.assertIsNone(await self.bot._fetch_asset("https://cdn.discordapp.com/avatars/1/h.png"))

    async def test_unicode_emoji_are_cached_on_disk(self) -> None:
        emoji = await self.bot._fetch_emoji({"1f602"}, {"123"})
        self.assertEqual(emoji, {"1f602": b"image-bytes", "custom:123": b"image-bytes"})
        self.assertEqual((self.runtime.emoji_cache / "1f602.png").read_bytes(), b"image-bytes")
        self.download.reset_mock()
        again = await self.bot._fetch_emoji({"1f602"}, set())
        self.assertEqual(again, {"1f602": b"image-bytes"})
        self.download.assert_not_awaited()  # served from the cache

    async def test_cache_failures_do_not_lose_the_image(self) -> None:
        self.runtime.emoji_cache = Path("/nonexistent-root/emoji-cache")
        self.assertEqual(await self.bot._fetch_emoji({"1f602"}, set()), {"1f602": b"image-bytes"})


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotScoutLifecycleTests(_SelfBotCase):
    async def test_scout_starts_with_the_client_and_stops_on_close(self) -> None:
        from selfbot import SelfBot

        runtime = make_runtime(Path(self._tmp.name), mode="dry-run")
        runtime.ledger.append(__import__("scout_stores").ClipRecord(1.0, 1, (1,), (2,), "posted", 9.0, {}, False, "x", posted_message_id=4242))
        bot = SelfBot(SETTINGS, self.allowlist, scout=runtime)
        await bot.setup_hook()
        self.assertIsNotNone(runtime.scout)
        self.assertEqual(runtime.judge.status, "heuristic (no LLM)")
        self.assertIn(4242, bot._clip_message_ids)
        task = bot._scout_task
        self.assertFalse(task.done())
        await bot.close()
        self.assertTrue(task.cancelled() or task.done())

    async def test_no_scout_task_when_scouting_is_off(self) -> None:
        from selfbot import SelfBot

        for scout in (None, make_runtime(Path(self._tmp.name), mode="off")):
            bot = SelfBot(SETTINGS, self.allowlist, scout=scout)
            await bot.setup_hook()
            self.assertIsNone(bot._scout_task)
            await bot.close()


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotOptOutTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name), mode="on")
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.addAsyncCleanup(self.bot.close)

    async def test_welcome_does_not_mention_clipping_in_dry_run(self) -> None:
        from selfbot import SelfBot

        dry = SelfBot(SETTINGS, self.allowlist, scout=make_runtime(Path(self._tmp.name) / "dry", mode="dry-run"))
        self.addAsyncCleanup(dry.close)
        message = make_message(author_id=445, content="hi")
        await dry.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], WELCOME_TEXT)  # nothing is posted in dry-run

    async def test_stop_in_dm_adds_optout_and_removes_allowlist(self) -> None:
        self.allowlist.add(99)
        message = make_message(content="stop")
        await self.bot.on_message(message)
        self.assertIn(99, self.runtime.optout)
        self.assertNotIn(99, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], STOP_CONFIRMATION)
        self.assertEqual(self.bot.stats.unregistered, 1)

    async def test_stranger_stop_in_dm_is_confirmed_and_recorded(self) -> None:
        message = make_message(author_id=321, content="stop")
        await self.bot.on_message(message)
        self.assertIn(321, self.runtime.optout)
        self.assertNotIn(321, self.allowlist)  # not registered by saying stop
        self.assertEqual(message.reply.await_args.args[0], STOP_CONFIRMATION)

    async def test_start_removes_optout_and_registers(self) -> None:
        self.runtime.optout.add(99)
        message = make_message(content="start")
        await self.bot.on_message(message)
        self.assertNotIn(99, self.runtime.optout)
        self.assertIn(99, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], START_CONFIRMATION)

    async def test_reply_stop_to_a_clip_records_optout_and_confirms(self) -> None:
        self.bot._clip_message_ids.add(900)
        reference = SimpleNamespace(message_id=900, resolved=None, cached_message=None)
        message = make_message(author_id=55, channel_cls=discord.TextChannel, content="stop", reference=reference)
        await self.bot.on_message(message)
        self.assertIn(55, self.runtime.optout)
        self.assertNotIn(55, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], CLIP_OPT_OUT_CONFIRMATION)

    async def test_reply_stop_to_a_clip_only_opts_out_the_replier(self) -> None:
        self.bot._clip_message_ids.add(900)
        self.runtime.optout.add(77)
        reference = SimpleNamespace(message_id=900, resolved=None, cached_message=None)
        await self.bot.on_message(make_message(author_id=55, channel_cls=discord.TextChannel, content="stop", reference=reference))
        self.assertEqual({55, 77}, {uid for uid in (55, 77, 99) if uid in self.runtime.optout})

    async def test_reply_stop_to_another_message_is_ignored(self) -> None:
        reference = SimpleNamespace(message_id=12345, resolved=None, cached_message=None)
        message = make_message(author_id=55, channel_cls=discord.TextChannel, content="stop", reference=reference)
        await self.bot.on_message(message)
        self.assertNotIn(55, self.runtime.optout)
        message.reply.assert_not_awaited()

    async def test_other_text_in_a_reply_to_a_clip_does_nothing(self) -> None:
        self.bot._clip_message_ids.add(900)
        reference = SimpleNamespace(message_id=900, resolved=None, cached_message=None)
        message = make_message(author_id=55, channel_cls=discord.TextChannel, content="haha that was me", reference=reference)
        await self.bot.on_message(message)
        self.assertNotIn(55, self.runtime.optout)
        message.reply.assert_not_awaited()

    async def test_welcome_mentions_clipping_when_the_scout_is_on(self) -> None:
        message = make_message(author_id=444, content="hi")
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], welcome_text(True))

    async def test_an_opted_out_user_who_dms_again_is_told_clipping_stays_off(self) -> None:
        self.runtime.optout.add(444)
        message = make_message(author_id=444, content="hi")
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], welcome_text(True, opted_out=True))
        self.assertIn(444, self.allowlist)  # registered for conversions
        self.assertIn(444, self.runtime.optout)  # but still opted out of clips

    async def test_opted_out_author_messages_leave_the_buffer_window(self) -> None:
        self.runtime.chats.enable(CHAT)
        for message_id, author in ((1, 55), (2, 66)):
            await self.bot.on_message(make_message(author_id=author, channel_cls=discord.TextChannel, content="a funny line", message_id=message_id))
        self.bot._clip_message_ids.add(900)
        reference = SimpleNamespace(message_id=900, resolved=None, cached_message=None)
        await self.bot.on_message(make_message(author_id=55, channel_cls=discord.TextChannel, content="stop", reference=reference, message_id=3))
        run = [self.runtime.buffer.get(CHAT, 2)]
        self.assertEqual([m.id for m in self.runtime.buffer.window(CHAT, run)], [2])

    async def test_stop_without_a_scout_still_works_like_v2(self) -> None:
        from selfbot import SelfBot

        bot = SelfBot(SETTINGS, self.allowlist)
        self.addAsyncCleanup(bot.close)
        self.allowlist.add(99)
        message = make_message(content="stop")
        await bot.on_message(message)
        self.assertNotIn(99, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], STOP_CONFIRMATION)

    async def test_start_without_a_scout_registers(self) -> None:
        from selfbot import SelfBot

        bot = SelfBot(SETTINGS, self.allowlist)
        self.addAsyncCleanup(bot.close)
        message = make_message(author_id=222, content="start")
        await bot.on_message(message)
        self.assertIn(222, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], START_CONFIRMATION)


OWNER = 4242
OTHER_CHAT = 888


class _CommandCase(_SelfBotCase):
    """Fixtures for owner-command tests: a bot with a scout runtime and helpers to build owner/other messages."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from selfbot import SelfBot

        self.make_bot = lambda runtime: SelfBot(SETTINGS, self.allowlist, scout=runtime)
        self.runtime = make_runtime(Path(self._tmp.name), mode="on", enabled=(), SELFBOT_OWNER_ID=str(OWNER))
        self.bot = self.make_bot(self.runtime)
        self.addAsyncCleanup(self.bot.close)

    def command(self, text: str, *, author: int = OWNER, channel_cls: type | None = None, channel_id: int = CHAT, **kwargs):
        message = make_message(author_id=author, channel_cls=channel_cls or discord.TextChannel, content=text, channel_id=channel_id, **kwargs)
        message.author.send = AsyncMock()
        message.channel.send = AsyncMock()
        message.channel.name = "friends"
        return message

    @staticmethod
    def sent(mock: AsyncMock) -> str:
        call = mock.await_args
        return call.args[0] if call.args else call.kwargs["content"]

    def replies(self, message) -> list[str]:
        """What the bot said back, in the channel the command was typed in."""
        return [call.args[0] if call.args else call.kwargs.get("content", "") for call in message.channel.send.await_args_list]

    def update_run(self, *, cat_ok: bool = True, start_ok: bool = True):
        """Fake subprocess.run for !update: the systemctl cat probe, then the sudo start."""
        calls: list[list[str]] = []

        def run(argv, **kwargs):
            calls.append(argv)
            ok = cat_ok if argv[1] == "cat" else start_ok
            return SimpleNamespace(returncode=0 if ok else 1, stdout=b"", stderr=b"")

        return run, calls


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotScoutCommandTests(_CommandCase):
    async def test_only_the_owner_can_use_scout_commands(self) -> None:
        message = self.command("!on", author=99)
        await self.bot.on_message(message)
        self.assertFalse(self.runtime.chats.enabled(CHAT))
        message.channel.send.assert_not_awaited()
        message.author.send.assert_not_awaited()
        message.reply.assert_not_awaited()

    async def test_scout_on_enables_the_chat_and_posts_the_announcement(self) -> None:
        message = self.command("!on")
        await self.bot.on_message(message)
        self.assertTrue(self.runtime.chats.enabled(CHAT))
        self.assertEqual(self.runtime.chats.list(), [(CHAT, "friends")])
        message.channel.send.assert_awaited_once()
        self.assertEqual(self.sent(message.channel.send), ANNOUNCE_ON)
        allowed = message.channel.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(any((allowed.everyone, allowed.users, allowed.roles, allowed.replied_user)))

    async def test_scout_on_in_dry_run_confirms_in_the_channel_without_announcing(self) -> None:
        runtime = make_runtime(Path(self._tmp.name) / "dry", mode="dry-run", enabled=(), SELFBOT_OWNER_ID=str(OWNER))
        bot = self.make_bot(runtime)
        self.addAsyncCleanup(bot.close)
        message = self.command("!on")
        await bot.on_message(message)
        self.assertTrue(runtime.chats.enabled(CHAT))
        self.assertIn("dry-run", self.replies(message)[0].lower())

    async def test_scout_off_disables_drops_the_buffer_and_announces(self) -> None:
        self.runtime.chats.enable(CHAT, "friends")
        self.runtime.buffer.add(smsg(1, "a line to forget"))
        message = self.command("!off")
        await self.bot.on_message(message)
        self.assertFalse(self.runtime.chats.enabled(CHAT))
        self.assertEqual(self.runtime.buffer.chats(), [])
        self.assertEqual(self.sent(message.channel.send), ANNOUNCE_OFF)

    async def test_scout_off_for_a_chat_that_was_not_on_says_so(self) -> None:
        message = self.command("!off")
        await self.bot.on_message(message)
        self.assertIn("wasn't on", self.replies(message)[0])

    async def test_scout_on_twice_does_not_announce_twice(self) -> None:
        await self.bot.on_message(self.command("!on"))
        again = self.command("!on")
        await self.bot.on_message(again)
        self.assertIn("already on", self.replies(again)[0])

    async def test_seeded_chat_cannot_be_disabled(self) -> None:
        runtime = make_runtime(Path(self._tmp.name) / "seeded", mode="on", enabled=(), SELFBOT_OWNER_ID=str(OWNER), SELFBOT_SCOUT_CHATS=str(CHAT))
        bot = self.make_bot(runtime)
        self.addAsyncCleanup(bot.close)
        message = self.command("!off")
        await bot.on_message(message)
        self.assertTrue(runtime.chats.enabled(CHAT))
        self.assertIn("SELFBOT_SCOUT_CHATS", self.replies(message)[0])

    async def test_scout_on_is_refused_in_nsfw_and_in_dm_without_an_id(self) -> None:
        nsfw = self.command("!on", nsfw=True)
        await self.bot.on_message(nsfw)
        self.assertFalse(self.runtime.chats.enabled(CHAT))
        self.assertIn("NSFW", self.replies(nsfw)[0])
        dm = self.command("!on", channel_cls=discord.DMChannel)
        await self.bot.on_message(dm)
        self.assertIn("chat id", self.replies(dm)[0].lower())
        self.assertEqual(self.runtime.chats.list(), [])

    async def test_scout_on_with_a_chat_id_from_the_owner_dm(self) -> None:
        target = Mock(spec=discord.TextChannel)
        target.id = OTHER_CHAT
        target.name = "other"
        target.is_nsfw = Mock(return_value=False)
        target.send = AsyncMock()
        self.bot.get_channel = Mock(return_value=target)  # type: ignore[method-assign]
        message = self.command(f"!on {OTHER_CHAT}", channel_cls=discord.DMChannel, channel_id=5)
        await self.bot.on_message(message)
        self.assertTrue(self.runtime.chats.enabled(OTHER_CHAT))
        target.send.assert_awaited_once()
        self.assertEqual(self.sent(target.send), ANNOUNCE_ON)
        self.assertEqual(self.runtime.chats.list(), [(OTHER_CHAT, "other")])

    async def test_chat_id_form_is_refused_outside_the_owner_dm_and_for_unknown_or_unscoutable_chats(self) -> None:
        public = self.command(f"!on {OTHER_CHAT}")
        await self.bot.on_message(public)
        self.assertFalse(self.runtime.chats.enabled(OTHER_CHAT))
        self.assertIn("DM", self.replies(public)[0])
        self.bot.get_channel = Mock(return_value=None)  # type: ignore[method-assign]
        unknown = self.command(f"!on {OTHER_CHAT}", channel_cls=discord.DMChannel, channel_id=5)
        await self.bot.on_message(unknown)
        self.assertIn("can't see", self.replies(unknown)[0])
        one_to_one = Mock(spec=discord.DMChannel)
        one_to_one.id = OTHER_CHAT
        self.bot.get_channel = Mock(return_value=one_to_one)  # type: ignore[method-assign]
        refused = self.command(f"!on {OTHER_CHAT}", channel_cls=discord.DMChannel, channel_id=5)
        await self.bot.on_message(refused)
        self.assertFalse(self.runtime.chats.enabled(OTHER_CHAT))
        self.assertIn("1:1", self.replies(refused)[0])

    async def test_scout_status_and_list_reply_to_the_owner_only(self) -> None:
        from scout_judge import build_judge_chain

        self.runtime.judge = build_judge_chain(self.runtime.cfg, None)  # type: ignore[arg-type]
        self.runtime.chats.enable(CHAT, "friends")
        status = self.command("!status")
        await self.bot.on_message(status)
        text = self.replies(status)[0]
        for needle in ("on", "heuristic", "1", "this chat"):
            self.assertIn(needle, text.lower())
        listing = self.command("!list")
        await self.bot.on_message(listing)
        self.assertIn("friends", self.replies(listing)[0])
        self.assertIn(str(CHAT), self.replies(listing)[0])
        for message in (status, listing):
            message.reply.assert_not_awaited()

    async def test_scout_list_when_nothing_is_enabled(self) -> None:
        message = self.command("!list")
        await self.bot.on_message(message)
        self.assertIn("no chats", self.replies(message)[0].lower())

    async def test_update_starts_the_update_unit(self) -> None:
        import selfbot as selfbot_module

        run, calls = self.update_run()
        message = self.command("!update")
        with patch.object(selfbot_module.subprocess, "run", run):
            await self.bot.on_message(message)
        self.assertEqual(calls[0], ["systemctl", "cat", "bombagif-selfbot-update.service"])
        self.assertEqual(calls[1], ["sudo", "-n", "systemctl", "start", "bombagif-selfbot-update.service"])
        self.assertIn("Updating now", self.replies(message)[0])

    async def test_update_says_how_to_enable_it_when_the_unit_is_missing(self) -> None:
        import selfbot as selfbot_module

        run, _ = self.update_run(cat_ok=False)
        message = self.command("!update")
        with patch.object(selfbot_module.subprocess, "run", run):
            await self.bot.on_message(message)
        self.assertIn("update-service", self.replies(message)[0])

    async def test_update_reports_a_refused_sudo(self) -> None:
        import selfbot as selfbot_module

        run, _ = self.update_run(start_ok=False)
        message = self.command("!update")
        with patch.object(selfbot_module.subprocess, "run", run):
            await self.bot.on_message(message)
        self.assertIn("sudo refused", self.replies(message)[0])

    async def test_scout_command_when_mode_is_off_is_refused(self) -> None:
        runtime = make_runtime(Path(self._tmp.name) / "off", mode="off", enabled=(), SELFBOT_OWNER_ID=str(OWNER))
        bot = self.make_bot(runtime)
        self.addAsyncCleanup(bot.close)
        message = self.command("!on")
        await bot.on_message(message)
        self.assertFalse(runtime.chats.enabled(CHAT))
        self.assertIn("SELFBOT_SCOUT_MODE", self.replies(message)[0])

    async def test_owner_without_a_configured_scout_is_ignored(self) -> None:
        bot = self.make_bot(None)
        self.addAsyncCleanup(bot.close)
        message = self.command("!on")
        await bot.on_message(message)
        message.author.send.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_command_messages_are_not_buffered_or_converted(self) -> None:
        self.runtime.chats.enable(CHAT)
        self.allowlist.add(OWNER)
        message = self.command("!status", attachments=[attachment()], mentions=[self.me])
        with patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK) as convert:
            await self.bot.on_message(message)
        convert.assert_not_awaited()
        self.assertEqual(self.runtime.buffer.chats(), [])

    async def test_a_captcha_while_announcing_trips_the_guard(self) -> None:
        message = self.command("!on")
        message.channel.send.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(message)
        self.assertTrue(self.bot.captcha_guard.blocked)

    async def test_a_channel_that_refuses_the_answer_falls_back_to_a_dm(self) -> None:
        message = self.command("!status")
        message.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"
        )
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_message(message)
        self.assertEqual(len(message.author.send.await_args_list), 1)

    async def test_closed_owner_dms_do_not_break_the_command(self) -> None:
        message = self.command("!status")
        message.channel.send.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Missing Access"
        )
        message.author.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_message(message)


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotHelpTests(_SelfBotCase):
    def ping(self, *, author: int = 99, chat: int = CHAT, **kwargs):
        return make_message(author_id=author, channel_cls=discord.TextChannel, mentions=[self.me], channel_id=chat, **kwargs)

    async def test_stranger_ping_gets_help_once_per_hour_per_person(self) -> None:
        first, second = self.ping(chat=1), self.ping(chat=2)
        await self.bot.on_message(first)
        await self.bot.on_message(second)
        first.reply.assert_awaited_once()
        second.reply.assert_not_awaited()  # same person, different chat, still within the hour

    async def test_help_is_available_again_after_an_hour(self) -> None:
        clock = [1000.0]
        from selfbot_logic import Cooldown

        self.bot._help_user_cooldown = Cooldown(3600, clock=lambda: clock[0])
        first, second = self.ping(chat=1), self.ping(chat=2)
        await self.bot.on_message(first)
        clock[0] += 3600
        await self.bot.on_message(second)
        first.reply.assert_awaited_once()
        second.reply.assert_awaited_once()

    async def test_help_is_throttled_per_chat(self) -> None:
        first, second, other_chat = self.ping(author=1), self.ping(author=2), self.ping(author=3, chat=999)
        for message in (first, second, other_chat):
            await self.bot.on_message(message)
        first.reply.assert_awaited_once()
        second.reply.assert_not_awaited()  # a different stranger, but the chat just got help
        other_chat.reply.assert_awaited_once()

    async def test_a_throttled_chat_does_not_use_up_the_persons_hour(self) -> None:
        await self.bot.on_message(self.ping(author=1))  # chat now cooling
        blocked = self.ping(author=2)
        await self.bot.on_message(blocked)
        blocked.reply.assert_not_awaited()
        elsewhere = self.ping(author=2, chat=999)
        await self.bot.on_message(elsewhere)
        elsewhere.reply.assert_awaited_once()  # author 2's own cooldown was never consumed

    async def test_help_never_registers_or_converts(self) -> None:
        message = self.ping(attachments=[attachment()])
        with patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK) as convert:
            await self.bot.on_message(message)
        convert.assert_not_awaited()
        self.assertNotIn(99, self.allowlist)
        self.assertEqual(message.reply.await_args.args[0], help_text(False))

    async def test_help_silent_when_captcha_paused(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("test")
        message = self.ping()
        await self.bot.on_message(message)
        message.reply.assert_not_awaited()

    async def test_help_silent_for_an_opted_out_author(self) -> None:
        from selfbot import SelfBot

        runtime = make_runtime(Path(self._tmp.name), mode="on")
        runtime.optout.add(99)
        bot = SelfBot(SETTINGS, self.allowlist, scout=runtime)
        self.addAsyncCleanup(bot.close)
        message = self.ping()
        await bot.on_message(message)
        message.reply.assert_not_awaited()

    async def test_help_mentions_clipping_only_when_clips_are_live(self) -> None:
        from selfbot import SelfBot

        live = SelfBot(SETTINGS, self.allowlist, scout=make_runtime(Path(self._tmp.name) / "live", mode="on"))
        self.addAsyncCleanup(live.close)
        message = self.ping()
        await live.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], help_text(True))

    async def test_help_is_counted_in_the_feed(self) -> None:
        await self.bot.on_message(self.ping())
        self.assertTrue(any("help" in entry.text for entry in self.bot.stats.activity))

    async def test_member_ping_without_media_still_gets_the_no_media_text(self) -> None:
        self.allowlist.add(99)
        message = self.ping()
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], NO_MEDIA_TEXT)

    async def test_a_ping_in_a_one_to_one_dm_is_not_a_stranger_ping(self) -> None:
        message = make_message(channel_cls=discord.DMChannel, content="hi", mentions=[self.me])
        await self.bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], WELCOME_TEXT)  # registered, not helped


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotScoutUiTests(_SelfBotCase):
    def bot_with(self, mode: str, **env: str):
        from scout_judge import build_judge_chain
        from selfbot import SelfBot

        runtime = make_runtime(Path(self._tmp.name) / f"ui-{mode}-{len(env)}", mode=mode, **env)
        runtime.judge = build_judge_chain(runtime.cfg, None, clock=lambda: NOW)  # type: ignore[arg-type]
        bot = SelfBot(SETTINGS, self.allowlist, scout=runtime)
        self.addAsyncCleanup(bot.close)
        return bot

    async def test_boot_info_without_a_scout_says_off(self) -> None:
        info = self.bot._boot_info()
        self.assertEqual(info["scout"], "off")
        self.assertNotIn("judge", info)

    async def test_boot_info_scout_rows(self) -> None:
        info = self.bot_with("dry-run")._boot_info()
        self.assertEqual(info["scout"], "dry-run")
        self.assertEqual(info["judge"], "heuristic (no LLM)")
        self.assertEqual(info["scout chats"], "1")
        self.assertNotIn("reminder", info)

    async def test_inception_reminder_shows_while_inception_is_active(self) -> None:
        bot = self.bot_with("on", INCEPTION_API_KEY="inception-secret-key")
        info = bot._boot_info()
        self.assertEqual(info["judge"], "inception")
        self.assertIn("Improve the model for everyone", info["reminder"])
        self.assertNotIn("inception-secret-key", " ".join(str(v) for v in info.values()))

    async def test_boot_info_teaching_rows(self) -> None:
        bot = self.bot_with("dry-run")
        info = bot._boot_info()
        self.assertEqual((info["manual clips"], info["review"]), ("on", "dry-run"))
        self.assertEqual(info["taste"], "0 chars of notes, 0 good / 0 bad examples")
        bot.scout.taste_notes.add("we like deadpan")
        bot.scout.taste_store.add("good", [("A", "a line")])
        bot.scout.taste_store.add("bad", [("A", "another line")])
        bot.scout.taste_store.add("bad", [("A", "third line")])
        self.assertEqual(bot._boot_info()["taste"], "15 chars of notes, 1 good / 2 bad examples")

    async def test_boot_info_reflects_manual_and_review_settings(self) -> None:
        info = self.bot_with("on", SCOUT_MANUAL="off", SCOUT_REVIEW="always")._boot_info()
        self.assertEqual((info["manual clips"], info["review"]), ("off", "always"))

    async def test_boot_info_never_leaks_the_notes_themselves(self) -> None:
        bot = self.bot_with("dry-run")
        bot.scout.taste_notes.add("secret taste instruction")
        self.assertNotIn("secret taste instruction", " ".join(bot._boot_info().values()))

    async def test_scout_view_is_none_when_off_and_a_tuple_otherwise(self) -> None:
        self.assertIsNone(self.bot.scout_view())
        self.assertIsNone(self.bot_with("off").scout_view())
        self.assertEqual(self.bot_with("dry-run").scout_view(), ("dry-run", "heuristic (no LLM)", False))
        self.assertEqual(self.bot_with("on", INCEPTION_API_KEY="k").scout_view(), ("on", "inception", True))

    async def test_scout_view_before_the_judge_exists(self) -> None:
        from selfbot import SelfBot

        bot = SelfBot(SETTINGS, self.allowlist, scout=make_runtime(Path(self._tmp.name) / "nojudge", mode="on"))
        self.addAsyncCleanup(bot.close)
        self.assertEqual(bot.scout_view(), ("on", "starting", False))


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class CreateBotTests(unittest.TestCase):
    ENV = {"ZIPLINE_TOKEN": "z", "ZIPLINE_URL": "https://z.example", "SELFBOT_TOKEN": "t"}

    def create(self, **extra: str):
        from bombagif_core import read_settings
        from selfbot import create_bot

        env = {**self.ENV, **extra}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return create_bot(read_settings("SELFBOT_TOKEN", env), Path(tmp.name), env)

    def test_without_scout_settings_the_bot_is_plain_v2(self) -> None:
        bot = self.create()
        self.assertIsNone(bot.scout)
        self.assertFalse(bot.auto_accept_friends)
        self.assertEqual(bot.captcha_guard.path.name, "captcha-blocked.json")

    def test_teaching_settings_reach_the_runtime(self) -> None:
        bot = self.create(SELFBOT_SCOUT_MODE="dry-run", SCOUT_EXAMPLES="5", SCOUT_REVIEW="off", SCOUT_MANUAL="off")
        self.assertEqual(bot.scout.taste_store.per_label, 5)
        self.assertEqual((bot.scout.cfg.review, bot.scout.cfg.manual), ("off", False))
        self.assertIsNotNone(bot._manual_gate)

    def test_the_owner_is_registered_at_startup(self) -> None:
        bot = self.create(SELFBOT_SCOUT_MODE="dry-run", SELFBOT_OWNER_ID="42")
        self.assertIn(42, bot.allowlist)

    def test_scout_settings_build_a_runtime(self) -> None:
        bot = self.create(SELFBOT_SCOUT_MODE="dry-run", SELFBOT_OWNER_ID="42", INCEPTION_API_KEY="k")
        self.assertEqual((bot.scout.cfg.mode, bot.scout.cfg.owner_id), ("dry-run", 42))
        self.assertEqual(bot.scout.cfg.inception_api_key, "k")

    def test_friend_auto_accept_flag_is_read(self) -> None:
        self.assertTrue(self.create(SELFBOT_AUTO_ACCEPT_FRIENDS="true").auto_accept_friends)

    def test_invalid_scout_settings_fail_fast_with_the_variable_name(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SELFBOT_SCOUT_MODE"):
            self.create(SELFBOT_SCOUT_MODE="sometimes")


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotManualClipTests(_SelfBotCase):
    TARGET_TEXT = "If I ever inherit a successful billboard business, I won't tell anybody, but there will be signs."

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        from selfbot import SelfBot

        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root, mode="on", SELFBOT_OWNER_ID=str(OWNER))
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot.close)
        self.allowlist.add(99)
        self.partial = Mock()
        self.partial.reply = AsyncMock(return_value=SimpleNamespace(id=900))
        self.partial.jump_url = "https://discord.com/channels/1/777/700"
        self.channel = Mock()
        self.channel.get_partial_message = Mock(return_value=self.partial)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=901))
        self.bot.get_channel = Mock(return_value=self.channel)  # type: ignore[method-assign]
        self.render = patch("selfbot.render_clip", return_value=b"\x89PNG fake").start()
        self.addCleanup(patch.stopall)
        patch.object(self.bot, "_fetch_asset", AsyncMock(return_value=None)).start()
        self.convert = patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK).start()

    def target(self, text: str | None = None, *, author: int = 10, message_id: int = 700, **kwargs):
        return make_message(
            author_id=author, channel_cls=discord.TextChannel, content=self.TARGET_TEXT if text is None else text,
            message_id=message_id, channel_id=kwargs.pop("channel_id", CHAT), **kwargs,
        )

    def ping(self, target, *, author: int = 99, chat: int = CHAT, **kwargs):
        reference = SimpleNamespace(message_id=target.id, resolved=target, cached_message=None)
        message = make_message(
            author_id=author, channel_cls=discord.TextChannel, mentions=[self.me], reference=reference,
            channel_id=chat, message_id=kwargs.pop("message_id", 800), **kwargs,
        )
        return message

    def posted_notice(self) -> str:
        return self.partial.reply.await_args.kwargs["content"]

    def rows(self) -> list[dict]:
        path = self.root / "clips.jsonl"
        import json as _json

        return [_json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    async def test_a_member_reply_pinging_a_text_message_clips_it(self) -> None:
        target = self.target(author_extra={"display_name": "Koryei"})
        request = self.ping(target, author_extra={"display_name": "Pranit"})
        await self.bot.on_message(request)
        self.partial.reply.assert_awaited_once()
        self.assertEqual(self.posted_notice(), "🎬 Clipped **Koryei** at **Pranit**'s request — DM me `stop` to opt out of clips.")
        self.channel.get_partial_message.assert_called_with(700)
        card = self.render.call_args.args[0]
        self.assertEqual([m.text for m in card.messages], [self.TARGET_TEXT])
        request.reply.assert_not_awaited()  # the clip is the answer, not "no media"
        self.convert.assert_not_awaited()

    async def test_manual_clips_are_ledgered_counted_and_remembered(self) -> None:
        await self.bot.on_message(self.ping(self.target()))
        row = self.rows()[0]
        self.assertEqual((row["mode"], row["provider"], row["chat_id"], row["message_ids"], row["posted_message_id"]), ("posted", "manual", CHAT, [700], 900))
        self.assertEqual(row["text"], [self.TARGET_TEXT])
        self.assertEqual(self.bot.stats.clips_posted, 1)
        self.assertIn(900, self.bot._clip_message_ids)
        self.assertTrue(any("manual" in e.text for e in self.bot.stats.activity))

    async def test_the_buffered_run_of_the_replied_to_message_is_used(self) -> None:
        for i, line in enumerate(("first line of the story", "second line of the story", "third line of the story")):
            self.runtime.buffer.add(smsg(700 + i, line, author=10))
        target = self.target("first line of the story")
        await self.bot.on_message(self.ping(target))
        card = self.render.call_args.args[0]
        self.assertEqual([m.text for m in card.messages], ["first line of the story", "second line of the story", "third line of the story"])

    async def test_an_unbuffered_message_is_clipped_alone(self) -> None:
        await self.bot.on_message(self.ping(self.target()))
        self.assertEqual(len(self.render.call_args.args[0].messages), 1)

    async def refused(self, request, expected: str) -> None:
        posted_before = self.partial.reply.await_count
        await self.bot.on_message(request)
        self.assertEqual(request.reply.await_args.args[0], expected)
        self.assertEqual(self.partial.reply.await_count, posted_before)  # nothing was posted
        self.channel.send.assert_not_awaited()

    async def test_opted_out_authors_are_refused(self) -> None:
        self.runtime.optout.add(10)
        await self.refused(self.ping(self.target()), REFUSAL_OPTED_OUT)

    async def test_messages_with_links_or_attachments_are_refused(self) -> None:
        await self.refused(self.ping(self.target("look at https://example.com please")), REFUSAL_NOT_TEXT)
        await self.refused(self.ping(self.target("see this one ||spoiler|| text", message_id=701)), REFUSAL_NOT_TEXT)
        await self.refused(self.ping(self.target("!on", message_id=702)), REFUSAL_NOT_TEXT)

    async def test_sensitive_text_is_refused_without_quoting_it(self) -> None:
        request = self.ping(self.target("call me on 415-555-0132 right now"))
        await self.bot.on_message(request)
        self.assertEqual(request.reply.await_args.args[0], REFUSAL_CANT)
        self.assertNotIn("415", request.reply.await_args.args[0])

    async def test_bots_cannot_be_clipped(self) -> None:
        target = self.target()
        target.author.bot = True
        await self.refused(self.ping(target), REFUSAL_CANT)

    async def test_members_need_an_enabled_chat_but_the_owner_does_not(self) -> None:
        self.runtime.chats.disable(CHAT)
        await self.refused(self.ping(self.target(), chat=CHAT), REFUSAL_NOT_ENABLED)
        self.allowlist.add(OWNER)
        await self.bot.on_message(self.ping(self.target(message_id=702), author=OWNER, message_id=801))
        self.partial.reply.assert_awaited_once()
        self.assertIn("at **user4242**'s request", self.posted_notice())

    async def test_requests_are_rate_limited_for_members(self) -> None:
        for i in range(3):
            await self.bot.on_message(self.ping(self.target(message_id=710 + i, author=20 + i), message_id=810 + i))
        self.assertEqual(self.partial.reply.await_count, 3)
        await self.refused(self.ping(self.target(message_id=720, author=30), message_id=820), REFUSAL_SLOW)

    async def test_a_message_cannot_be_clipped_twice(self) -> None:
        await self.bot.on_message(self.ping(self.target()))
        again = self.ping(self.target(), message_id=801)
        await self.bot.on_message(again)
        self.assertEqual(self.partial.reply.await_count, 1)
        self.assertIn("already clipped", again.reply.await_args.args[0])

    async def test_a_failed_post_is_silent_and_starts_no_cooldown(self) -> None:
        self.partial.reply.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Permissions")
        request = self.ping(self.target())
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            await self.bot.on_message(request)
        request.reply.assert_not_awaited()
        self.assertEqual(self.bot.stats.clips_posted, 0)
        self.partial.reply.side_effect = None
        await self.bot.on_message(self.ping(self.target(), message_id=801))
        self.assertEqual(self.bot.stats.clips_posted, 1)  # the same message could still be clipped

    async def test_nsfw_and_dm_chats_fall_back_to_the_no_media_text(self) -> None:
        nsfw = self.ping(self.target(), nsfw=True)
        await self.bot.on_message(nsfw)
        self.assertEqual(nsfw.reply.await_args.args[0], NO_MEDIA_TEXT)
        self.partial.reply.assert_not_awaited()

    async def test_without_a_reply_or_a_runtime_or_with_manual_off_it_is_the_old_behaviour(self) -> None:
        from selfbot import SelfBot

        bare = make_message(author_id=99, channel_cls=discord.TextChannel, mentions=[self.me])
        await self.bot.on_message(bare)
        self.assertEqual(bare.reply.await_args.args[0], NO_MEDIA_TEXT)
        for scout in (None, make_runtime(self.root / "off", mode="off", SELFBOT_OWNER_ID=str(OWNER)), make_runtime(self.root / "man", mode="on", SCOUT_MANUAL="off")):
            bot = SelfBot(SETTINGS, self.allowlist, scout=scout)
            self.addAsyncCleanup(bot.close)
            request = self.ping(self.target(message_id=730))
            await bot.on_message(request)
            self.assertEqual(request.reply.await_args.args[0], NO_MEDIA_TEXT)
        self.partial.reply.assert_not_awaited()

    async def test_media_in_the_message_or_the_target_still_wins(self) -> None:
        await self.bot.on_message(self.ping(self.target("a text message"), attachments=[attachment()]))
        self.convert.assert_awaited_once()
        self.partial.reply.assert_not_awaited()
        self.convert.reset_mock()
        with_media = self.target("caption text", attachments=[attachment("theirs.png")], message_id=740)
        await self.bot.on_message(self.ping(with_media, message_id=840))
        self.convert.assert_awaited_once()

    async def test_a_deleted_target_is_the_no_media_text(self) -> None:
        reference = SimpleNamespace(message_id=700, resolved=SimpleNamespace(id=700), cached_message=None)  # the deleted marker has no attachments
        request = make_message(author_id=99, channel_cls=discord.TextChannel, mentions=[self.me], reference=reference)
        await self.bot.on_message(request)
        self.assertEqual(request.reply.await_args.args[0], NO_MEDIA_TEXT)

    async def test_clip_that_works_in_a_group_chat(self) -> None:
        group = Mock(spec=discord.GroupChannel)
        group.id = CHAT
        group.send = AsyncMock(return_value=SimpleNamespace(id=902))
        self.bot.get_channel = Mock(return_value=group)  # type: ignore[method-assign]
        target = make_message(author_id=10, channel_cls=discord.GroupChannel, content=self.TARGET_TEXT, message_id=700, channel_id=CHAT)
        reference = SimpleNamespace(message_id=700, resolved=target, cached_message=None)
        request = make_message(author_id=99, channel_cls=discord.GroupChannel, mentions=[self.me], reference=reference, channel_id=CHAT, message_id=800)
        await self.bot.on_message(request)
        group.send.assert_awaited_once()
        self.assertEqual(group.send.await_args.kwargs["reference"].message_id, 700)
        request.reply.assert_not_awaited()
        self.assertEqual(self.bot.stats.clips_posted, 1)

    async def test_dry_run_still_posts_manual_clips(self) -> None:
        from selfbot import SelfBot

        bot = SelfBot(SETTINGS, self.allowlist, scout=make_runtime(self.root / "dry", mode="dry-run"))
        bot.http_session = self.bot.http_session
        bot.get_channel = self.bot.get_channel  # type: ignore[method-assign]
        patch.object(bot, "_fetch_asset", AsyncMock(return_value=None)).start()
        await bot.on_message(self.ping(self.target()))
        self.partial.reply.assert_awaited_once()

    async def test_the_owner_is_always_a_registered_member(self) -> None:
        self.assertIn(OWNER, self.allowlist)

    async def test_owner_requests_teach_a_good_example_but_members_do_not(self) -> None:
        taught = []
        self.bot._teach = lambda label, chosen, source: taught.append((label, [m.id for m in chosen], source))  # type: ignore[method-assign]
        await self.bot.on_message(self.ping(self.target(message_id=750), author=OWNER, message_id=850))
        await self.bot.on_message(self.ping(self.target(message_id=751, author=20), author=99, message_id=851))
        self.assertEqual(taught, [("good", [750], "manual")])


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotTeachingCommandTests(_CommandCase):

    async def test_taste_notes_can_be_shown_added_and_cleared(self) -> None:
        shown = self.command("!taste")
        await self.bot.on_message(shown)
        self.assertIn("no taste notes", self.replies(shown)[0].lower())
        self.assertIn("/1500", self.replies(shown)[0])
        added = self.command("!taste add We like deadpan one-liners")
        await self.bot.on_message(added)
        self.assertEqual(self.runtime.taste_notes.text, "We like deadpan one-liners")
        self.assertIn("Added", self.replies(added)[0])
        self.assertIn("26/1500", self.replies(added)[0])
        again = self.command("!taste")
        await self.bot.on_message(again)
        self.assertIn("We like deadpan one-liners", self.replies(again)[0])
        cleared = self.command("!taste clear")
        await self.bot.on_message(cleared)
        self.assertEqual(self.runtime.taste_notes.text, "")
        self.assertIn("Cleared", self.replies(cleared)[0])

    async def test_taste_add_refuses_overlong_notes_and_changes_nothing(self) -> None:
        self.runtime.taste_notes.add("x" * 1490)
        message = self.command("!taste add " + "y" * 50)
        await self.bot.on_message(message)
        self.assertEqual(len(self.runtime.taste_notes.text), 1490)
        self.assertIn("limit", self.replies(message)[0].lower())

    async def test_taste_add_of_only_control_characters_says_nothing_to_add(self) -> None:
        message = self.command("!taste add \x00\x07\x00")
        await self.bot.on_message(message)
        self.assertIn("Nothing to add", self.replies(message)[0])

    async def test_non_owners_cannot_teach(self) -> None:
        message = self.command("!taste add ignore the owner", author=99)
        await self.bot.on_message(message)
        self.assertEqual(self.runtime.taste_notes.text, "")
        message.author.send.assert_not_awaited()
        message.channel.send.assert_not_awaited()

    async def test_examples_report_and_clear(self) -> None:
        empty = self.command("!examples")
        await self.bot.on_message(empty)
        self.assertIn("no rated examples", self.replies(empty)[0].lower())
        store = self.runtime.taste_store
        store.add("good", [("A", "the better one was me")])
        store.add("good", [("A", "setup"), ("B", "punchline")])
        store.add("bad", [("A", "a boring line")])
        message = self.command("!examples")
        await self.bot.on_message(message)
        text = self.replies(message)[0]
        for needle in ("good 2", "bad 1", "the better one was me", "A: setup | B: punchline", "a boring line"):
            self.assertIn(needle, text)
        cleared = self.command("!examples clear")
        await self.bot.on_message(cleared)
        self.assertEqual(store.counts(), {"good": 0, "bad": 0})
        self.assertIn("Cleared", self.replies(cleared)[0])

    async def test_tune_reports_progress_then_a_suggestion(self) -> None:
        message = self.command("!tune")
        await self.bot.on_message(message)
        self.assertIn("Not enough", self.replies(message)[0])
        store = self.runtime.taste_store
        for i in range(5):
            store.add("good", [("A", f"good line {i}")], funny=9.0)
            store.add("bad", [("A", f"bad line {i}")], funny=6.0)
        again = self.command("!tune")
        await self.bot.on_message(again)
        self.assertIn("SCOUT_JUDGE_FUNNY_MIN=7.5", self.replies(again)[0])

    async def test_scout_help_answers_with_the_owner_guide(self) -> None:
        message = self.command("!help")
        await self.bot.on_message(message)
        self.assertEqual(self.replies(message), [HELP_OWNER])

    async def test_help_from_the_owner_answers_wherever_it_was_typed(self) -> None:
        for channel_cls, channel_id in ((discord.TextChannel, CHAT), (discord.DMChannel, 5)):
            message = self.command("!help", channel_cls=channel_cls, channel_id=channel_id)
            await self.bot.on_message(message)
            self.assertEqual(self.replies(message), [HELP_OWNER])
            message.reply.assert_not_awaited()

    async def test_help_from_others_only_answers_in_a_one_to_one_dm(self) -> None:
        dm = self.command("!help", author=99, channel_cls=discord.DMChannel, channel_id=5)
        await self.bot.on_message(dm)
        self.assertEqual(dm.reply.await_args.args[0], HELP_USER)
        public = self.command("!help", author=99)
        await self.bot.on_message(public)
        public.reply.assert_not_awaited()
        public.author.send.assert_not_awaited()
        public.channel.send.assert_not_awaited()

    async def test_help_works_without_a_scout_runtime(self) -> None:
        bot = self.make_bot(None)
        self.addAsyncCleanup(bot.close)
        message = self.command("!help", author=OWNER, channel_cls=discord.DMChannel, channel_id=5)
        await bot.on_message(message)
        self.assertEqual(message.reply.await_args.args[0], HELP_USER)  # no runtime, so nobody is "the owner"

    async def test_help_is_silent_while_captcha_paused_and_ignores_bots(self) -> None:
        bot_dm = self.command("!help", author=99, channel_cls=discord.DMChannel, channel_id=5)
        bot_dm.author.bot = True
        await self.bot.on_message(bot_dm)
        bot_dm.reply.assert_not_awaited()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.bot.captcha_guard.trip("test")
        dm = self.command("!help", author=99, channel_cls=discord.DMChannel, channel_id=5)
        await self.bot.on_message(dm)
        dm.reply.assert_not_awaited()
        owner = self.command("!help")
        await self.bot.on_message(owner)
        owner.author.send.assert_not_awaited()

    async def test_teaching_commands_work_when_scouting_is_off(self) -> None:
        runtime = make_runtime(Path(self._tmp.name) / "offmode", mode="off", enabled=(), SELFBOT_OWNER_ID=str(OWNER))
        bot = self.make_bot(runtime)
        self.addAsyncCleanup(bot.close)
        message = self.command("!taste add keep it dry")
        await bot.on_message(message)
        self.assertEqual(runtime.taste_notes.text, "keep it dry")
        on = self.command("!on")
        await bot.on_message(on)
        self.assertIn("SELFBOT_SCOUT_MODE", self.replies(on)[0])  # the scouting commands are still refused

    async def test_teaching_without_stores_says_so(self) -> None:
        self.runtime.taste_notes = None
        self.runtime.taste_store = None
        for text in ("!taste", "!taste add x here", "!examples", "!tune"):
            message = self.command(text)
            await self.bot.on_message(message)
            self.assertIn("isn't set up", self.replies(message)[0])

    async def test_help_and_teaching_messages_are_never_buffered_or_converted(self) -> None:
        self.runtime.chats.enable(CHAT)
        self.allowlist.add(OWNER)
        for text in ("!help", "!taste add something good", "!tune"):
            message = self.command(text, attachments=[attachment()], mentions=[self.me])
            with patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK) as convert:
                await self.bot.on_message(message)
            convert.assert_not_awaited()
        self.assertEqual(self.runtime.buffer.chats(), [])


def review_item(*, id: int = 1, would_clip: bool = True, names=("Pranit",), lines=(("A", "the better one was me"),), funny: float = 8.7) -> ReviewItem:
    return ReviewItem(id, CHAT, tuple(names), tuple(lines), funny, {"funny": 0.93, "standalone": 0.88, "sensitive": 0.02}, "deadpan and standalone", would_clip)


class FakeVoiceClient:
    """Stands in for VoiceRecvClient: records the sink and whether it was disconnected."""

    def __init__(self, channel) -> None:
        self.channel = channel
        self.sink = None
        self.connected = True

    def listen(self, sink) -> None:
        self.sink = sink

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False


class FakeVoiceChannel:
    """Stands in for a discord VoiceChannel: records how the bot connected."""

    def __init__(self, name: str = "General", members=()) -> None:
        self.name = name
        self.members = list(members)
        self.client: FakeVoiceClient | None = None
        self.connect_kwargs: dict | None = None

    async def connect(self, **kwargs):
        self.connect_kwargs = kwargs
        self.client = FakeVoiceClient(self)
        return self.client


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotVoiceTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        import selfbot as selfbot_module
        from selfbot import SelfBot

        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root, mode="dry-run", SELFBOT_OWNER_ID=str(OWNER), SCOUT_VOICE="on")
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot.close)
        self.bot._start_scout()
        self.scout = self.runtime.scout
        self.voice_channel = FakeVoiceChannel(
            members=[SimpleNamespace(id=OWNER, display_name="Koryei"), SimpleNamespace(id=7, display_name="Pranit")]
        )
        self.text_channel = Mock(spec=discord.TextChannel)
        self.text_channel.id = CHAT
        self.text_channel.send = AsyncMock()
        # The test venv has no voice_recv or davey, so the adapter's optional imports are stubbed for these tests.
        for name, value in (("HAS_VOICE_RECV", True), ("HAS_DAVE", True), ("VoiceRecvClient", FakeVoiceClient)):
            patcher = patch.object(selfbot_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.owner_dm = AsyncMock()
        self.owner_voice = SimpleNamespace(channel=self.voice_channel)

    def owner_message(self, content: str, *, in_voice: bool = True, author: int = OWNER):
        extra = {"send": self.owner_dm}
        if in_voice:
            extra["voice"] = self.owner_voice
        message = make_message(
            author_id=author, channel_cls=discord.TextChannel, channel_id=CHAT, content=content, author_extra=extra
        )
        message.channel = self.text_channel  # so a posted clip lands somewhere the test can see it
        return message

    async def join(self) -> None:
        await self.bot.on_message(self.owner_message("!join"))
        self.assertIsNotNone(self.bot._voice_client)
        self.join_reply = self.said()
        self.text_channel.send.reset_mock()  # so a clip assertion sees only the clip

    def said(self, channel=None) -> str:
        """The last thing the bot said, in the channel the command was typed in."""
        return (channel or self.text_channel).send.await_args.args[0]

    def feed(self, user_id: int = 7, seconds: float = 1.0, value: int = 1000) -> None:
        self.bot._voice.add(user_id, struct.pack("<h", value) * int(seconds * 48000) * 2)

    async def test_join_connects_undeafened_and_starts_listening(self) -> None:
        await self.join()
        self.assertIs(self.voice_channel.connect_kwargs["cls"], FakeVoiceClient)
        self.assertFalse(self.voice_channel.connect_kwargs["self_deaf"])  # a deafened client is sent no audio
        self.assertTrue(self.voice_channel.connect_kwargs["self_mute"])
        self.assertIsNotNone(self.voice_channel.client.sink)
        self.assertIn("Listening", self.join_reply)

    async def test_join_refuses_when_voice_is_off(self) -> None:
        self.runtime.cfg = read_scout_config({"SELFBOT_SCOUT_MODE": "dry-run", "SELFBOT_OWNER_ID": str(OWNER)})
        await self.bot.on_message(self.owner_message("!join"))
        self.assertIsNone(self.bot._voice_client)
        self.assertIn("SCOUT_VOICE", self.said())

    async def test_join_refuses_when_the_owner_is_not_in_a_call(self) -> None:
        await self.bot.on_message(self.owner_message("!join", in_voice=False))
        self.assertIsNone(self.bot._voice_client)
        self.assertIn("Join a voice channel", self.said())

    def group_call(self) -> Mock:
        """A group DM in a call: recipients instead of members, and a connect() that takes no self_mute."""
        group = Mock(spec=discord.GroupChannel)
        group.name = None
        group.guild = None
        group.me = SimpleNamespace(id=OWNER, display_name="Koryei")
        group.recipients = [SimpleNamespace(id=7, display_name="Pranit")]
        group.send = AsyncMock()
        group.connect = AsyncMock(return_value=FakeVoiceClient(group))
        return group

    async def test_join_works_in_a_group_dm(self) -> None:
        group = self.group_call()
        self.owner_voice = SimpleNamespace(channel=group)
        message = self.owner_message("!join")
        message.channel = group
        await self.bot.on_message(message)
        self.assertIs(self.bot._voice_client.channel, group)
        self.assertFalse(group.connect.await_args.kwargs.get("ring", True))  # never ring the whole group
        self.assertIn("Listening", self.said(group))

    async def test_clip_that_works_in_a_group_dm(self) -> None:
        group = self.group_call()
        self.owner_voice = SimpleNamespace(channel=group)
        message = self.owner_message("!join")
        message.channel = group
        await self.bot.on_message(message)
        group.send.reset_mock()  # so the clip assertion sees only the clip
        self.feed(7)
        clip = self.owner_message("clip that")
        clip.channel = group
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake"):
            await self.bot.on_message(clip)
        group.send.assert_awaited_once()
        self.assertEqual(group.send.await_args.args[0], "🎙️ **Pranit**, last 30s")

    def dm_call(self) -> Mock:
        """A 1:1 DM in a call: one recipient, no guild, and a connect() that takes no self_mute."""
        dm = Mock(spec=discord.DMChannel)
        dm.guild = None
        dm.me = SimpleNamespace(id=OWNER, display_name="Koryei")
        dm.recipient = SimpleNamespace(id=7, display_name="aarav")
        dm.send = AsyncMock()
        dm.connect = AsyncMock(return_value=FakeVoiceClient(dm))
        return dm

    async def test_join_works_in_a_one_to_one_dm(self) -> None:
        dm = self.dm_call()
        self.owner_voice = SimpleNamespace(channel=dm)
        message = self.owner_message("!join")
        message.channel = dm
        await self.bot.on_message(message)
        self.assertIs(self.bot._voice_client.channel, dm)
        self.assertFalse(dm.connect.await_args.kwargs.get("ring", True))  # never ring the other person
        self.assertIn("Listening", self.said(dm))

    async def test_clip_aarav_works_in_a_one_to_one_dm(self) -> None:
        # A 1:1 DM has a single recipient, not a recipients list, so the name has to resolve from that.
        dm = self.dm_call()
        self.owner_voice = SimpleNamespace(channel=dm)
        message = self.owner_message("!join")
        message.channel = dm
        await self.bot.on_message(message)
        dm.send.reset_mock()  # so the clip assertion sees only the clip
        self.feed(7)
        clip = self.owner_message("clip aarav")
        clip.channel = dm
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake"):
            await self.bot.on_message(clip)
        dm.send.assert_awaited_once()
        self.assertEqual(dm.send.await_args.args[0], "🎙️ **aarav**, last 30s")

    async def test_join_explains_a_missing_libopus(self) -> None:
        self.voice_channel.connect = AsyncMock(side_effect=discord.opus.OpusNotLoaded)
        await self.bot.on_message(self.owner_message("!join"))
        self.assertIsNone(self.bot._voice_client)
        self.assertIn("libopus", self.said())

    async def test_join_refuses_without_dave(self) -> None:
        # Discord closes the voice socket with 4017 when the client cannot do the DAVE protocol.
        with patch("selfbot.HAS_DAVE", False):
            await self.bot.on_message(self.owner_message("!join"))
        self.assertIsNone(self.bot._voice_client)
        self.assertIn("davey", self.said())

    async def test_a_failed_join_drops_the_half_open_client(self) -> None:
        # py-self registers the voice client before the handshake, so a failure would make the next !join say
        # "Already connected to a voice channel" until the bot restarted.
        half_open = SimpleNamespace(disconnect=AsyncMock())
        self.voice_channel.guild = SimpleNamespace(voice_client=half_open)
        self.voice_channel.connect = AsyncMock(side_effect=RuntimeError("handshake failed"))
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            await self.bot.on_message(self.owner_message("!join"))
        half_open.disconnect.assert_awaited_once_with(force=True)
        self.assertIn("couldn't join", self.said())

    async def test_leave_disconnects_and_forgets_the_buffer(self) -> None:
        await self.join()
        client = self.bot._voice_client
        await self.bot.on_message(self.owner_message("!leave"))
        self.assertFalse(client.connected)
        self.assertIsNone(self.bot._voice)
        self.assertIsNone(self.bot._voice_client)
        self.assertIn("Left", self.said())

    async def test_voice_reports_what_it_is_listening_to(self) -> None:
        await self.bot.on_message(self.owner_message("!voice"))
        self.assertIn("not in a call", self.said())
        await self.join()
        self.feed()
        await self.bot.on_message(self.owner_message("!voice"))
        self.assertIn("1 voice(s) buffered", self.said())

    async def test_clip_that_posts_the_last_speaker_as_an_ogg(self) -> None:
        await self.join()
        self.feed(7)
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip that"))
        encode.assert_called_once()
        self.text_channel.send.assert_awaited_once()
        self.assertEqual(self.text_channel.send.await_args.args[0], "🎙️ **Pranit**, last 30s")
        self.assertEqual(self.text_channel.send.await_args.kwargs["file"].filename, "clip.ogg")

    async def test_clip_a_named_person_uses_that_persons_audio(self) -> None:
        await self.join()
        self.feed(7, value=1000)
        self.feed(OWNER, value=2000)
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip pranit that"))
        self.assertIn("Pranit", self.text_channel.send.await_args.args[0])
        pcm = struct.unpack(f"<{len(encode.call_args.args[0]) // 2}h", encode.call_args.args[0])
        self.assertEqual({sample for sample in pcm if sample}, {1000})  # only Pranit's audio, silence-padded

    async def test_clip_what_he_just_said_uses_the_last_speaker(self) -> None:
        await self.join()
        self.feed(7, value=1000)
        self.feed(OWNER, value=2000)
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake"):
            await self.bot.on_message(self.owner_message("clip what he just said"))
        self.assertIn("Koryei", self.text_channel.send.await_args.args[0])

    async def test_an_unknown_name_is_refused_without_a_clip(self) -> None:
        await self.join()
        self.feed(7)
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip zendaya that"))
        encode.assert_not_called()
        self.assertIn("zendaya", self.text_channel.send.await_args.args[0])

    async def test_an_empty_buffer_says_so(self) -> None:
        await self.join()
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip that"))
        encode.assert_not_called()
        self.assertIn("Nothing to clip", self.text_channel.send.await_args.args[0])

    async def test_a_clip_request_from_someone_else_is_ignored(self) -> None:
        await self.join()
        self.feed(7)
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip that", author=99))
        encode.assert_not_called()
        self.text_channel.send.assert_not_awaited()

    async def test_a_clip_request_without_being_in_a_call_is_ignored(self) -> None:
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake") as encode:
            await self.bot.on_message(self.owner_message("clip that"))
        encode.assert_not_called()

    async def test_the_wake_word_posts_a_clip(self) -> None:
        await self.join()
        self.feed(7)
        self.bot.get_channel = Mock(return_value=self.text_channel)  # type: ignore[method-assign]
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake"):
            await self.bot._clip_from_wake()
        self.text_channel.send.assert_awaited_once()
        self.assertIn("Pranit", self.text_channel.send.await_args.args[0])

    async def test_the_wake_word_is_rate_limited(self) -> None:
        await self.join()
        self.feed(7)
        self.bot.get_channel = Mock(return_value=self.text_channel)  # type: ignore[method-assign]
        with patch("selfbot.encode_ogg", return_value=b"OggS-fake"):
            await self.bot._clip_from_wake()
            await self.bot._clip_from_wake()
        self.text_channel.send.assert_awaited_once()  # the second one is inside the cooldown

    async def test_the_sink_feeds_the_buffer_and_the_wake_word(self) -> None:
        await self.join()
        sink = self.voice_channel.client.sink
        heard: list[int] = []
        sink.on_wake = lambda: heard.append(1)
        sink.wake = SimpleNamespace(feed=lambda pcm: True)
        sink.write(SimpleNamespace(id=7), SimpleNamespace(pcm=struct.pack("<h", 5) * 960))
        self.assertEqual(self.bot._voice.speakers(), [7])
        self.assertEqual(heard, [1])

    async def test_a_broken_wake_word_is_turned_off_not_raised(self) -> None:
        await self.join()
        sink = self.voice_channel.client.sink
        sink.wake = SimpleNamespace(feed=lambda pcm: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            sink.write(SimpleNamespace(id=7), SimpleNamespace(pcm=struct.pack("<h", 5) * 960))
        self.assertIsNone(sink.wake)
        self.assertEqual(self.bot._voice.speakers(), [7])  # the audio still reached the buffer

    async def test_the_sink_ignores_unknown_users_and_empty_frames(self) -> None:
        await self.join()
        sink = self.voice_channel.client.sink
        sink.write(None, SimpleNamespace(pcm=b"\x00\x00"))
        sink.write(SimpleNamespace(id=7), SimpleNamespace(pcm=b""))
        self.assertEqual(self.bot._voice.speakers(), [])


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotReviewTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        from selfbot import SelfBot

        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root, mode="dry-run", SELFBOT_OWNER_ID=str(OWNER))
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.bot.close)
        self.bot._start_scout()
        self.scout = self.runtime.scout
        self.sent = SimpleNamespace(id=6001, add_reaction=AsyncMock())
        self.owner = Mock()
        self.owner.send = AsyncMock(return_value=self.sent)
        self.bot.get_user = Mock(return_value=self.owner)  # type: ignore[method-assign]

    def reaction(self, emoji: str, *, user: int = OWNER, message_id: int = 6001):
        return SimpleNamespace(channel_id=5, message_id=message_id, user_id=user, emoji=emoji)

    async def test_start_scout_wires_review_and_taste(self) -> None:
        self.assertEqual(self.scout.on_review, self.bot._send_review)
        self.assertIs(self.scout.taste, self.runtime.taste_store)

    async def test_review_dm_shows_the_candidate_to_the_owner_only(self) -> None:
        self.assertEqual(await self.bot._send_review((review_item(),)), [(6001, (review_item(),))])
        self.bot.get_user.assert_called_once_with(OWNER)
        content = self.owner.send.await_args.args[0] if self.owner.send.await_args.args else self.owner.send.await_args.kwargs["content"]
        for needle in ("would clip", "Pranit", "the better one was me", "8.7", "deadpan and standalone", "Reply to this message", "`good`", "`bad`", "`skip`"):
            self.assertIn(needle, content)
        self.assertNotIn("React", content)
        allowed = self.owner.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(any((allowed.everyone, allowed.users, allowed.roles, allowed.replied_user)))
        self.sent.add_reaction.assert_not_awaited()  # reactions are rate limited, so the account never adds any

    async def test_near_miss_dms_say_so(self) -> None:
        await self.bot._send_review((review_item(would_clip=False),))
        content = self.owner.send.await_args.args[0] if self.owner.send.await_args.args else self.owner.send.await_args.kwargs["content"]
        self.assertIn("near miss", content)
        self.assertNotIn("would clip", content)

    async def test_multi_speaker_reviews_label_each_line(self) -> None:
        await self.bot._send_review((review_item(names=("A name", "B name"), lines=(("A", "setup"), ("B", "punch"))),))
        content = self.owner.send.await_args.args[0] if self.owner.send.await_args.args else self.owner.send.await_args.kwargs["content"]
        self.assertIn("(A)", content)
        self.assertIn("(B)", content)

    async def test_a_batch_dm_numbers_each_candidate(self) -> None:
        items = (review_item(), review_item(id=2, would_clip=False, lines=(("A", "second line"),)))
        self.assertEqual(await self.bot._send_review(items), [(6001, items)])
        self.owner.send.assert_awaited_once()
        content = self.owner.send.await_args.args[0]
        for needle in ("**Review candidates** (2)", "**1.** would clip", "**2.** near miss", "the better one was me", "second line",
                       "`1 good`", "`2 bad`", "`1g 2b`"):
            self.assertIn(needle, content)
        self.assertNotIn("Reply to this message", content)  # a batch is answered with numbers, not a bare word

    async def test_a_long_batch_is_split_across_dms(self) -> None:
        items = tuple(review_item(id=i, lines=(("A", "x" * 400),)) for i in range(1, 13))
        sent = await self.bot._send_review(items)
        self.assertGreater(len(sent), 1)
        self.assertEqual([item for _, chunk in sent for item in chunk], list(items))  # every candidate, in order
        for call in self.owner.send.await_args_list:
            self.assertLessEqual(len(call.args[0]), 2000)

    async def test_replying_numbers_rates_a_batch(self) -> None:
        self.scout.reviews[6001] = (review_item(), review_item(id=2, lines=(("A", "second line"),)))
        message = self.owner_reply("1 good 2 bad")
        await self.bot.on_message(message)
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 1})
        self.assertEqual([e.lines for e in self.runtime.taste_store.all()], [(("A", "the better one was me"),), (("A", "second line"),)])
        self.assertNotIn(6001, self.scout.reviews)
        message.reply.assert_not_awaited()

    async def test_a_partly_rated_batch_keeps_the_rest_pending(self) -> None:
        self.scout.reviews[6001] = (review_item(), review_item(id=2, lines=(("A", "second line"),)))
        await self.bot.on_message(self.owner_reply("2 skip"))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertEqual([item.id for item in self.scout.reviews[6001]], [1])

    async def test_a_bare_rating_on_a_batch_asks_for_numbers(self) -> None:
        self.scout.reviews[6001] = (review_item(), review_item(id=2, lines=(("A", "second line"),)))
        message = self.owner_reply("good")
        await self.bot.on_message(message)
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertIn(6001, self.scout.reviews)  # nothing was rated: it is ambiguous which candidate was meant
        self.assertTrue(any("1 good 2 bad" in e.text for e in self.bot.stats.activity))
        message.reply.assert_not_awaited()

    async def test_reacting_to_a_batch_asks_for_a_reply(self) -> None:
        self.scout.reviews[6001] = (review_item(), review_item(id=2, lines=(("A", "second line"),)))
        await self.bot.on_raw_reaction_add(self.reaction("👍"))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertIn(6001, self.scout.reviews)
        self.assertTrue(any("1 good 2 bad" in e.text for e in self.bot.stats.activity))

    async def test_no_owner_or_closed_dms_return_none(self) -> None:
        self.runtime.cfg = read_scout_config({"SELFBOT_SCOUT_MODE": "dry-run"})
        self.assertEqual(await self.bot._send_review((review_item(),)), [])
        self.runtime.cfg = read_scout_config({"SELFBOT_SCOUT_MODE": "dry-run", "SELFBOT_OWNER_ID": str(OWNER)})
        self.owner.send.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Cannot send messages to this user")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            self.assertEqual(await self.bot._send_review((review_item(),)), [])

    async def test_a_captcha_while_sending_a_review_trips_the_guard(self) -> None:
        self.owner.send.side_effect = make_captcha()
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.assertEqual(await self.bot._send_review((review_item(),)), [])
        self.assertEqual(self.bot.captcha_guard.reason, "sending a review")

    async def test_the_account_never_adds_reactions_anywhere(self) -> None:
        await self.bot._send_review((review_item(),))
        await self.bot._send_review((review_item(would_clip=False),))
        self.sent.add_reaction.assert_not_awaited()

    def owner_reply(self, text: str, *, to: int = 6001, author: int = OWNER, channel_cls=None, **kwargs):
        reference = SimpleNamespace(message_id=to, resolved=None, cached_message=None)
        return make_message(author_id=author, channel_cls=channel_cls or discord.DMChannel, content=text, reference=reference, message_id=9100, **kwargs)

    async def rate_by_reply(self, text: str, **kwargs):
        self.scout.reviews[6001] = (review_item(),)
        message = self.owner_reply(text, **kwargs)
        await self.bot.on_message(message)
        return message

    async def test_replying_good_or_bad_to_a_review_dm_rates_it(self) -> None:
        message = await self.rate_by_reply("good")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 0})
        self.assertEqual(self.runtime.taste_store.all()[0].lines, (("A", "the better one was me"),))
        self.assertNotIn(6001, self.scout.reviews)
        message.reply.assert_not_awaited()  # rating is silent: no "no media" reply, no confirmation noise
        await self.rate_by_reply("bad")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 1})

    async def test_reply_aliases_and_skip(self) -> None:
        for text, expected in (("!good", {"good": 1, "bad": 0}), ("yes", {"good": 1, "bad": 0}), ("👍🏽", {"good": 1, "bad": 0}), ("no", {"good": 0, "bad": 1}), ("👎", {"good": 0, "bad": 1}), ("skip", {"good": 0, "bad": 0}), ("🙈", {"good": 0, "bad": 0})):
            with self.subTest(text=text):
                self.runtime.taste_store.clear()
                await self.rate_by_reply(text)
                self.assertEqual(self.runtime.taste_store.counts(), expected)
                self.assertNotIn(6001, self.scout.reviews)

    async def test_replies_from_other_people_or_with_other_text_do_not_rate(self) -> None:
        await self.rate_by_reply("good", author=99)
        await self.rate_by_reply("thanks, that one was great")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertIn(6001, self.scout.reviews)  # still waiting for the owner

    async def test_a_rating_reply_to_a_forgotten_review_is_consumed_silently(self) -> None:
        # A restart (or the 200-candidate cap) can forget a review DM. The owner's "good" is still a rating,
        # not a conversion request, so it must never be answered with "no media".
        from selfbot import SelfBot

        self.scout.reviews[6001] = (review_item(),)
        with patch.object(SelfBot, "_convert_and_reply", new_callable=AsyncMock) as convert:
            message = self.owner_reply("good", to=424242, mentions=[self.me])
            await self.bot.on_message(message)
        convert.assert_not_awaited()
        message.reply.assert_not_awaited()
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertIn(6001, self.scout.reviews)  # the pending candidate is untouched

    async def test_a_rating_still_lands_after_a_restart(self) -> None:
        import aiohttp
        from selfbot import SelfBot

        self.scout.reviews[6001] = (review_item(),)
        self.scout._save_reviews()
        restarted_runtime = make_runtime(self.root, mode="dry-run", SELFBOT_OWNER_ID=str(OWNER))
        restarted = SelfBot(SETTINGS, self.allowlist, scout=restarted_runtime)
        restarted.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(restarted.close)
        restarted._start_scout()
        message = self.owner_reply("good")
        await restarted.on_message(message)
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 0})
        message.reply.assert_not_awaited()

    async def test_a_second_reply_to_the_same_review_does_nothing(self) -> None:
        await self.rate_by_reply("good")
        await self.bot.on_message(self.owner_reply("bad"))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 0})

    async def test_rating_by_reply_shows_in_the_feed(self) -> None:
        await self.rate_by_reply("bad")
        self.assertTrue(any("bad" in e.text and "rated" in e.text for e in self.bot.stats.activity))

    async def test_replying_good_or_bad_to_a_posted_clip_teaches_from_the_ledger(self) -> None:
        self.runtime.ledger.append(ClipRecord(1.0, CHAT, (1, 2), (10, 20), "posted", 8.9, {"funny": 0.9}, False, "inception", posted_message_id=900, text=("setup", "punchline"), speakers=("A", "B")))
        self.bot._clip_message_ids.add(900)
        message = self.owner_reply("bad", to=900, channel_cls=discord.TextChannel, channel_id=CHAT)
        await self.bot.on_message(message)
        example = self.runtime.taste_store.all()[0]
        self.assertEqual((example.label, example.source, example.funny, example.lines), ("bad", "clip", 8.9, (("A", "setup"), ("B", "punchline"))))
        message.reply.assert_not_awaited()

    async def test_clip_replies_ignore_skip_non_owners_non_clips_and_old_rows(self) -> None:
        self.runtime.ledger.append(ClipRecord(1.0, CHAT, (1,), (10,), "posted", 8.0, {}, False, "manual", posted_message_id=900, text=("old row",)))
        self.bot._clip_message_ids.add(900)
        await self.bot.on_message(self.owner_reply("skip", to=900, channel_cls=discord.TextChannel, channel_id=CHAT))
        await self.bot.on_message(self.owner_reply("good", to=900, author=99, channel_cls=discord.TextChannel, channel_id=CHAT))
        await self.bot.on_message(self.owner_reply("good", to=555, channel_cls=discord.TextChannel, channel_id=CHAT))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        await self.bot.on_message(self.owner_reply("good", to=900, channel_cls=discord.TextChannel, channel_id=CHAT))
        self.assertEqual(self.runtime.taste_store.all()[0].lines, (("A", "old row"),))  # a row without speakers uses one speaker

    async def test_rating_replies_work_without_a_scout_task_or_judge(self) -> None:
        self.scout.reviews[6001] = (review_item(),)
        self.runtime.scout = None  # e.g. a bot that has not finished starting
        await self.bot.on_message(self.owner_reply("good"))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})  # nothing to rate against; no crash

    async def test_owner_reactions_still_count_for_free(self) -> None:
        self.scout.reviews[6001] = (review_item(),)
        await self.bot.on_raw_reaction_add(self.reaction("👍"))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 0})

    async def rate(self, emoji: str, *, user: int = OWNER) -> None:
        self.scout.reviews[6001] = (review_item(),)
        await self.bot.on_raw_reaction_add(self.reaction(emoji, user=user))

    async def test_owner_reactions_rate_the_candidate(self) -> None:
        await self.rate("👍")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 0})
        self.assertEqual(self.runtime.taste_store.all()[0].lines, (("A", "the better one was me"),))
        self.assertNotIn(6001, self.scout.reviews)
        await self.rate("👎")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 1, "bad": 1})

    async def test_skin_tone_thumbs_count(self) -> None:
        await self.rate("👍🏽")
        self.assertEqual(self.runtime.taste_store.counts()["good"], 1)

    async def test_skip_drops_the_candidate_without_saving(self) -> None:
        await self.rate("🙈")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertNotIn(6001, self.scout.reviews)

    async def test_other_people_and_other_emoji_are_ignored(self) -> None:
        await self.rate("👍", user=99)
        await self.rate("❤️")
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})
        self.assertIn(6001, self.scout.reviews)  # still waiting for the owner

    async def test_unknown_messages_are_ignored(self) -> None:
        await self.bot.on_raw_reaction_add(self.reaction("👍", message_id=424242))
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})

    async def test_rating_shows_in_the_feed(self) -> None:
        await self.rate("👍")
        self.assertTrue(any("good" in e.text for e in self.bot.stats.activity))

    async def test_reacting_to_a_posted_clip_teaches_from_the_ledger(self) -> None:
        self.runtime.ledger.append(ClipRecord(1.0, CHAT, (1, 2), (10, 20), "posted", 8.9, {"funny": 0.9}, False, "inception", posted_message_id=900, text=("setup", "punchline"), speakers=("A", "B")))
        self.bot._clip_message_ids.add(900)
        await self.bot.on_raw_reaction_add(self.reaction("👎", message_id=900))
        example = self.runtime.taste_store.all()[0]
        self.assertEqual((example.label, example.source, example.funny), ("bad", "clip", 8.9))
        self.assertEqual(example.lines, (("A", "setup"), ("B", "punchline")))

    async def test_clip_reactions_without_speakers_use_one_speaker(self) -> None:
        self.runtime.ledger.append(ClipRecord(1.0, CHAT, (1,), (10,), "posted", 8.0, {}, False, "manual", posted_message_id=901, text=("old style row",)))
        self.bot._clip_message_ids.add(901)
        await self.bot.on_raw_reaction_add(self.reaction("👍", message_id=901))
        self.assertEqual(self.runtime.taste_store.all()[0].lines, (("A", "old style row"),))

    async def test_clip_reactions_ignore_skip_non_owners_and_unknown_clips(self) -> None:
        self.runtime.ledger.append(ClipRecord(1.0, CHAT, (1,), (10,), "posted", 8.0, {}, False, "x", posted_message_id=900, text=("a line",)))
        self.bot._clip_message_ids.add(900)
        await self.bot.on_raw_reaction_add(self.reaction("🙈", message_id=900))
        await self.bot.on_raw_reaction_add(self.reaction("👍", user=99, message_id=900))
        await self.bot.on_raw_reaction_add(self.reaction("👍", message_id=555))  # not one of our clips
        self.assertEqual(self.runtime.taste_store.counts(), {"good": 0, "bad": 0})

    async def test_laugh_counting_still_works_alongside_rating(self) -> None:
        self.runtime.chats.enable(CHAT)
        await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="a funny line", message_id=1))
        await self.bot.on_raw_reaction_add(SimpleNamespace(channel_id=CHAT, message_id=1, user_id=5, emoji="😂"))
        self.assertEqual(self.runtime.buffer.get(CHAT, 1).laugh_reactions, 1)

    async def test_teach_saves_labelled_examples_from_messages(self) -> None:
        chosen = [smsg(1, "setup line", author=10), smsg(2, "the punchline", author=20), smsg(3, "tag on", author=10)]
        self.bot._teach("good", chosen, "manual")
        example = self.runtime.taste_store.all()[0]
        self.assertEqual((example.label, example.source, example.funny), ("good", "manual", 0.0))
        self.assertEqual(example.lines, (("A", "setup line"), ("B", "the punchline"), ("A", "tag on")))

    async def test_teach_without_a_store_is_a_no_op(self) -> None:
        self.runtime.taste_store = None
        self.bot._teach("good", [smsg(1, "x line")], "manual")  # must not raise

    async def test_the_judge_chain_reads_the_live_taste_notes(self) -> None:
        self.runtime.taste_notes.add("we like deadpan")
        self.assertEqual(self.runtime.judge.active.name, "heuristic")  # no keys in this test: the chain is the fallback
        from selfbot import SelfBot

        runtime = make_runtime(self.root / "keyed", mode="dry-run", INCEPTION_API_KEY="k")
        runtime.taste_notes.add("we like deadpan")
        bot = SelfBot(SETTINGS, self.allowlist, scout=runtime)
        bot.http_session = self.bot.http_session
        bot._start_scout()
        self.addAsyncCleanup(bot.close)
        self.assertEqual(runtime.judge.active.taste.notes(), "we like deadpan")


class FakeIntent:
    """Stands in for scout_intent.IntentClassifier."""

    def __init__(self, intent=None) -> None:
        self.intent = intent
        self.calls: list[tuple[str, bool]] = []

    async def classify(self, text: str, *, is_reply: bool, guess: object):
        self.calls.append((text, is_reply))
        return self.intent


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotTriggerTests(_SelfBotCase):
    """Spoken triggers: "clip that" / "gif that" and their typos, with no ping."""

    TEXT = "If I ever inherit a successful billboard business, I won't tell anybody, but there will be signs."

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        import aiohttp
        from scout_intent import Intent

        self.Intent = Intent
        self.root = Path(self._tmp.name)
        self.runtime = make_runtime(self.root, mode="on", SELFBOT_OWNER_ID=str(OWNER))
        self.bot = self.make_bot(self.runtime)
        self.allowlist.add(99)
        self.partial = Mock()
        self.partial.reply = AsyncMock(return_value=SimpleNamespace(id=900))
        self.channel = Mock()
        self.channel.get_partial_message = Mock(return_value=self.partial)
        self.channel.send = AsyncMock(return_value=SimpleNamespace(id=901))
        self.bot.get_channel = Mock(return_value=self.channel)  # type: ignore[method-assign]
        self.render = patch("selfbot.render_clip", return_value=b"\x89PNG fake").start()
        self.addCleanup(patch.stopall)
        patch.object(self.bot, "_fetch_asset", AsyncMock(return_value=None)).start()
        self.convert = patch("selfbot.convert_and_upload", new_callable=AsyncMock, return_value=LINK).start()
        self.intent = FakeIntent()
        self.bot.intent = self.intent

    def make_bot(self, runtime):
        import aiohttp
        from selfbot import SelfBot

        bot = SelfBot(SETTINGS, self.allowlist, scout=runtime)
        bot.http_session = aiohttp.ClientSession()
        self.addAsyncCleanup(bot.close)
        return bot

    def target(self, text: str | None = None, *, author: int = 10, message_id: int = 700, **kwargs):
        return make_message(
            author_id=author, channel_cls=discord.TextChannel, content=self.TEXT if text is None else text,
            message_id=message_id, channel_id=CHAT, **kwargs,
        )

    def say(self, text: str, *, author: int = 99, message_id: int = 800, to=None, chat: int = CHAT, channel_cls=None, **kwargs):
        reference = SimpleNamespace(message_id=to.id, resolved=to, cached_message=None) if to is not None else None
        return make_message(
            author_id=author, channel_cls=channel_cls or discord.TextChannel, content=text, reference=reference,
            channel_id=chat, message_id=message_id, created=NOW + message_id, **kwargs,
        )

    def card_texts(self) -> list[str]:
        return [m.text for m in self.render.call_args.args[0].messages]

    def buffer(self, *messages: ScoutMessage) -> None:
        for message in messages:
            self.runtime.buffer.add(message)

    async def assert_nothing_happened(self, request) -> None:
        self.partial.reply.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        request.reply.assert_not_awaited()
        self.convert.assert_not_awaited()
        self.render.assert_not_called()

    # replies ---------------------------------------------------------------------------------------------------

    async def test_replying_clip_that_clips_the_message_without_a_ping(self) -> None:
        request = self.say("clip that", to=self.target(author_extra={"display_name": "Koryei"}), author_extra={"display_name": "Pranit"})
        await self.bot.on_message(request)
        self.partial.reply.assert_awaited_once()
        self.assertEqual(self.partial.reply.await_args.kwargs["content"], "🎬 Clipped **Koryei** at **Pranit**'s request — DM me `stop` to opt out of clips.")
        self.assertEqual(self.card_texts(), [self.TEXT])
        request.reply.assert_not_awaited()
        self.assertEqual(self.bot.stats.clips_posted, 1)
        self.assertEqual(self.intent.calls, [])  # a known phrase never costs an AI call

    async def test_the_request_message_is_not_buffered_as_chat(self) -> None:
        await self.bot.on_message(self.say("clip that lmao", to=self.target()))
        self.assertIsNone(self.runtime.buffer.get(CHAT, 800))

    async def test_a_reply_uses_the_buffered_run_of_the_target(self) -> None:
        self.buffer(smsg(700, "first line of the story"), smsg(701, "second line of the story"))
        await self.bot.on_message(self.say("clip that", to=self.target("first line of the story")))
        self.assertEqual(self.card_texts(), ["first line of the story", "second line of the story"])

    async def test_filler_laughter_and_emoji_around_the_phrase_are_fine(self) -> None:
        for i, text in enumerate(("bro clip that LMFAOOO 😭", "yo can someone clip this fr", "CLIP THAT")):
            request = self.say(text, to=self.target(f"unique line number {i} for the card", author=20 + i, message_id=710 + i), message_id=810 + i)
            await self.bot.on_message(request)
        self.assertEqual(self.partial.reply.await_count, 3)

    # no reply: the latest message -----------------------------------------------------------------------------------

    async def test_without_a_reply_the_latest_message_is_clipped(self) -> None:
        self.buffer(smsg(700, "an earlier message", author=10), smsg(701, self.TEXT, author=20))
        request = self.say("clip that")
        await self.bot.on_message(request)
        self.assertEqual(self.card_texts(), [self.TEXT])
        self.channel.get_partial_message.assert_called_with(701)
        request.reply.assert_not_awaited()

    async def test_without_a_reply_the_latest_run_of_one_person_is_clipped(self) -> None:
        self.buffer(smsg(700, "setup of the joke", author=10), smsg(701, "and the punchline", author=10))
        await self.bot.on_message(self.say("clip that"))
        self.assertEqual(self.card_texts(), ["setup of the joke", "and the punchline"])

    async def test_a_stale_or_missing_message_is_not_clipped(self) -> None:
        request = self.say("clip that")
        await self.bot.on_message(request)  # nothing buffered
        await self.assert_nothing_happened(request)
        self.buffer(smsg(600, "said a long while ago"))  # 200 seconds before the request, the limit is 180
        again = self.say("clip that", message_id=801)
        await self.bot.on_message(again)
        await self.assert_nothing_happened(again)

    async def test_a_photo_in_between_is_not_reached_past(self) -> None:
        from dataclasses import replace

        self.buffer(smsg(700, "a text message before the photo"), replace(smsg(701, ""), eligible=False))
        request = self.say("clip that")
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)

    # typos: the AI decides -----------------------------------------------------------------------------------------

    async def test_a_typo_is_confirmed_by_the_ai(self) -> None:
        self.intent.intent = self.Intent("clip")
        request = self.say("clkip that LMFAII", to=self.target())
        await self.bot.on_message(request)
        self.assertEqual(self.intent.calls, [("clkip that LMFAII", True)])
        self.assertEqual(self.card_texts(), [self.TEXT])
        request.reply.assert_not_awaited()

    async def test_when_the_ai_says_no_it_is_just_chat(self) -> None:
        self.intent.intent = self.Intent("none")
        request = self.say("clip that video", to=self.target())
        await self.bot.on_message(request)
        self.assertEqual(len(self.intent.calls), 1)
        await self.assert_nothing_happened(request)
        self.assertIsNotNone(self.runtime.buffer.get(CHAT, 800))  # and it is buffered like any message

    async def test_without_an_ai_a_typo_is_ignored(self) -> None:
        self.bot.intent = None
        request = self.say("clkip that", to=self.target())
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)

    async def test_the_ai_is_never_asked_about_ordinary_chat(self) -> None:
        for text in ("lol that was so funny", "i am heading out", "what are we doing tomorrow"):
            await self.bot.on_message(self.say(text, message_id=802))
        self.assertEqual(self.intent.calls, [])

    async def test_phrases_mode_never_calls_the_ai_but_still_hears_known_phrases(self) -> None:
        bot = self.make_bot(make_runtime(self.root / "ph", mode="on", SCOUT_TRIGGERS="phrases"))
        self.wire(bot)
        bot.intent = self.intent
        self.intent.intent = self.Intent("clip")
        await bot.on_message(self.say("clkip that", to=self.target()))
        self.partial.reply.assert_not_awaited()
        await bot.on_message(self.say("clip that", to=self.target(), message_id=801))
        self.partial.reply.assert_awaited_once()
        self.assertEqual(self.intent.calls, [])

    def wire(self, bot) -> None:
        bot.get_channel = Mock(return_value=self.channel)  # type: ignore[method-assign]
        patch.object(bot, "_fetch_asset", AsyncMock(return_value=None)).start()

    # who and where ---------------------------------------------------------------------------------------------------

    async def test_strangers_are_ignored_unless_anyone_is_allowed(self) -> None:
        request = self.say("clip that", author=55, to=self.target())
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)
        bot = self.make_bot(make_runtime(self.root / "any", mode="on", SCOUT_TRIGGER_ANYONE="on"))
        self.wire(bot)
        await bot.on_message(self.say("clip that", author=55, to=self.target(message_id=710), message_id=810))
        self.partial.reply.assert_awaited_once()

    async def test_chats_without_scouting_are_not_listened_to(self) -> None:
        self.runtime.chats.disable(CHAT)
        self.allowlist.add(OWNER)
        for author in (99, OWNER):
            request = self.say("clip that", author=author, to=self.target())
            await self.bot.on_message(request)
            await self.assert_nothing_happened(request)

    async def test_the_triggers_setting_off_turns_it_all_off(self) -> None:
        bot = self.make_bot(make_runtime(self.root / "off", mode="on", SCOUT_TRIGGERS="off"))
        self.wire(bot)
        request = self.say("clip that", to=self.target())
        await bot.on_message(request)
        await self.assert_nothing_happened(request)

    async def test_manual_off_or_scouting_off_hears_nothing(self) -> None:
        for name, runtime in (("man", make_runtime(self.root / "man", mode="on", SCOUT_MANUAL="off")), ("off", make_runtime(self.root / "off2", mode="off"))):
            bot = self.make_bot(runtime)
            self.wire(bot)
            request = self.say("clip that", to=self.target())
            await bot.on_message(request)
            await self.assert_nothing_happened(request)

    async def test_dms_are_not_listened_to(self) -> None:
        request = self.say("clip that", to=self.target(), channel_cls=discord.DMChannel)
        await self.bot.on_message(request)
        self.partial.reply.assert_not_awaited()
        self.render.assert_not_called()

    async def test_bots_and_the_account_itself_cannot_trigger(self) -> None:
        bot_author = self.say("clip that", to=self.target(), author_extra={"bot": True})
        own = self.say("clip that", author=SELF_ID, to=self.target(message_id=710), message_id=801)
        for request in (bot_author, own):
            await self.bot.on_message(request)
        self.partial.reply.assert_not_awaited()
        self.render.assert_not_called()

    async def test_a_ping_still_goes_through_the_ping_path_once(self) -> None:
        request = self.say("clip that", to=self.target(), mentions=[self.me])
        await self.bot.on_message(request)
        self.partial.reply.assert_awaited_once()
        self.assertEqual(self.bot.stats.clips_posted, 1)

    async def test_a_message_aimed_at_someone_else_is_not_ours(self) -> None:
        request = self.say("<@123456789012345678> clip that", to=self.target())
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)

    async def test_the_captcha_pause_stops_triggers(self) -> None:
        self.bot.captcha_guard.trip("test")
        request = self.say("clip that", to=self.target())
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)

    # refusals are silent -----------------------------------------------------------------------------------------------

    async def test_refusals_are_silent_but_show_in_the_feed(self) -> None:
        self.runtime.optout.add(10)
        request = self.say("clip that", to=self.target())
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)
        self.assertTrue(any("ignored" in e.text for e in self.bot.stats.activity))

    async def test_sensitive_text_is_silently_refused(self) -> None:
        request = self.say("clip that", to=self.target("call me on 415-555-0132 right now"))
        await self.bot.on_message(request)
        await self.assert_nothing_happened(request)

    async def test_the_member_limit_is_silent_too(self) -> None:
        for i in range(3):
            await self.bot.on_message(self.say("clip that", to=self.target(f"distinct clip number {i} here", author=20 + i, message_id=710 + i), message_id=810 + i))
        self.assertEqual(self.partial.reply.await_count, 3)
        fourth = self.say("clip that", to=self.target("a fourth distinct message", author=30, message_id=720), message_id=820)
        await self.bot.on_message(fourth)
        self.assertEqual(self.partial.reply.await_count, 3)
        fourth.reply.assert_not_awaited()

    async def test_a_message_cannot_be_clipped_twice(self) -> None:
        for message_id in (800, 801):
            await self.bot.on_message(self.say("clip that", to=self.target(), message_id=message_id))
        self.assertEqual(self.partial.reply.await_count, 1)

    # gif that ------------------------------------------------------------------------------------------------------------

    async def test_gif_that_converts_the_replied_to_media(self) -> None:
        request = self.say("gif that", to=self.target("look", attachments=[attachment()]))
        await self.bot.on_message(request)
        self.convert.assert_awaited_once()
        self.assertIn(LINK, request.reply.await_args.args[0])
        self.render.assert_not_called()

    async def test_gif_that_without_media_or_a_reply_is_silent_and_never_becomes_a_clip(self) -> None:
        no_media = self.say("gif that", to=self.target("just words in this one"))
        await self.bot.on_message(no_media)
        await self.assert_nothing_happened(no_media)
        no_reply = self.say("gif that", message_id=801)
        await self.bot.on_message(no_reply)
        await self.assert_nothing_happened(no_reply)

    async def test_gif_needs_a_registered_member_even_when_anyone_may_clip(self) -> None:
        bot = self.make_bot(make_runtime(self.root / "any2", mode="on", SCOUT_TRIGGER_ANYONE="on"))
        self.wire(bot)
        request = self.say("gif that", author=55, to=self.target("look", attachments=[attachment()]))
        await bot.on_message(request)
        self.convert.assert_not_awaited()
        request.reply.assert_not_awaited()

    async def test_the_ai_can_turn_an_unsure_message_into_a_gif_request(self) -> None:
        self.intent.intent = self.Intent("gif")
        request = self.say("clkip that", to=self.target("look", attachments=[attachment()]))
        await self.bot.on_message(request)
        self.convert.assert_awaited_once()


class FakeImageResponse:
    """Just enough of an aiohttp response for the picture download."""

    def __init__(self, *, status: int = 200, body: bytes = b"\x89PNG", content_type: str = "image/png") -> None:
        self.status = status
        self.headers = {"Content-Type": content_type}
        self.content = SimpleNamespace(read=AsyncMock(return_value=body))

    async def __aenter__(self) -> "FakeImageResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class FakeImageSession:
    """Records the URLs asked for and hands back one canned response (or raises)."""

    def __init__(self, response: object | None = None, *, error: Exception | None = None) -> None:
        self.response = response if response is not None else FakeImageResponse()
        self.error = error
        self.urls: list[str] = []
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    def get(self, url: str, **kwargs: object) -> object:
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self.response


@unittest.skipUnless(HAS_SELFBOT_LIB, "needs discord.py-self (run in .venv-selfbot)")
class SelfBotScoutImageTests(_SelfBotCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name))
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.addAsyncCleanup(self.bot.close)

    def image_message(self, url: str = "https://cdn.discordapp.com/a.png") -> SimpleNamespace:
        attachment = SimpleNamespace(url=url, content_type="image/png")
        return make_message(channel_cls=discord.TextChannel, content="look at this", attachments=[attachment])

    def buffered_image_message(self) -> SimpleNamespace:
        """A message already in the buffer, as it would be by the time its pictures finish downloading."""
        message = self.image_message()
        self.runtime.buffer.add(smsg(message.id, "look at this"))
        return message

    async def test_an_image_message_starts_a_picture_download(self) -> None:
        message = self.image_message()
        with patch.object(self.bot, "_attach_images", AsyncMock()) as attach:
            await self.bot.on_message(message)
            await asyncio.gather(*list(self.bot._image_tasks))
        attach.assert_awaited_once()
        self.assertEqual(attach.await_args.args[0], message)
        self.assertEqual(len(attach.await_args.args[2]), 1)

    async def test_a_text_only_message_starts_no_download(self) -> None:
        with patch.object(self.bot, "_attach_images", AsyncMock()) as attach:
            await self.bot.on_message(make_message(channel_cls=discord.TextChannel, content="just words"))
            await asyncio.gather(*list(self.bot._image_tasks))
        attach.assert_not_awaited()

    async def test_pictures_are_not_downloaded_when_turned_off(self) -> None:
        from selfbot import SelfBot

        self.runtime = make_runtime(Path(self._tmp.name), SCOUT_IMAGES="off")
        self.bot = SelfBot(SETTINGS, self.allowlist, scout=self.runtime)
        self.addAsyncCleanup(self.bot.close)
        with patch.object(self.bot, "_attach_images", AsyncMock()) as attach:
            await self.bot.on_message(self.image_message())
            await asyncio.gather(*list(self.bot._image_tasks))
        attach.assert_not_awaited()

    async def test_a_downloaded_picture_is_attached_to_the_buffered_message(self) -> None:
        message = self.buffered_image_message()
        self.bot.http_session = FakeImageSession(FakeImageResponse(body=b"\x89PNG"))
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        found = self.runtime.buffer.get(CHAT, message.id)
        self.assertEqual(len(found.images), 1)
        self.assertTrue(found.images[0].startswith("data:image/png;base64,"))

    async def test_nothing_is_fetched_before_the_http_session_exists(self) -> None:
        message = self.buffered_image_message()
        self.assertIsNone(self.bot.http_session)
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        self.assertEqual(self.runtime.buffer.get(CHAT, message.id).images, ())

    async def test_a_failed_download_leaves_the_message_without_pictures(self) -> None:
        message = self.buffered_image_message()
        self.bot.http_session = FakeImageSession(FakeImageResponse(status=404))
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        self.assertEqual(self.runtime.buffer.get(CHAT, message.id).images, ())

    async def test_a_connection_error_leaves_the_message_without_pictures(self) -> None:
        message = self.buffered_image_message()
        self.bot.http_session = FakeImageSession(error=aiohttp.ClientError("boom"))
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        self.assertEqual(self.runtime.buffer.get(CHAT, message.id).images, ())

    async def test_a_non_image_response_is_ignored(self) -> None:
        message = self.buffered_image_message()
        self.bot.http_session = FakeImageSession(FakeImageResponse(content_type="text/html"))
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        self.assertEqual(self.runtime.buffer.get(CHAT, message.id).images, ())

    async def test_an_oversized_picture_is_ignored(self) -> None:
        from selfbot import MAX_IMAGE_BYTES

        message = self.buffered_image_message()
        self.bot.http_session = FakeImageSession(FakeImageResponse(body=b"x" * (MAX_IMAGE_BYTES + 1)))
        await self.bot._attach_images(message, self.runtime, ["https://cdn.discordapp.com/a.png"])
        self.assertEqual(self.runtime.buffer.get(CHAT, message.id).images, ())


if __name__ == "__main__":
    unittest.main()
