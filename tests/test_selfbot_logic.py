"""Discord-free self-bot logic: allowlist, rate limiting, decisions, media collection, copy."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from selfbot_logic import (
    CLIP_OPT_OUT_CONFIRMATION,
    NO_MEDIA_TEXT,
    START_CONFIRMATION,
    STOP_CONFIRMATION,
    WELCOME_TEXT,
    AllowlistStore,
    apply_timezone,
    BotStats,
    CaptchaGuard,
    ChannelKind,
    Cooldown,
    Decision,
    Incoming,
    Kind,
    RateLimiter,
    collect_media,
    decide,
    env_flag,
    env_flag_off,
    format_results,
    help_text,
    resolve_ui_mode,
    update_notice_text,
    welcome_text,
)


class AllowlistStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "nested" / "allowlist.json"

    def test_missing_file_is_empty(self) -> None:
        store = AllowlistStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertNotIn(42, store)

    def test_add_persists_across_instances(self) -> None:
        AllowlistStore(self.path).add(42)
        self.assertIn(42, AllowlistStore(self.path))

    def test_add_twice_returns_false(self) -> None:
        store = AllowlistStore(self.path)
        self.assertTrue(store.add(42))
        self.assertFalse(store.add(42))

    def test_remove_persists(self) -> None:
        store = AllowlistStore(self.path)
        store.add(42)
        self.assertTrue(store.remove(42))
        self.assertFalse(store.remove(42))
        self.assertNotIn(42, AllowlistStore(self.path))

    def test_file_mode_is_600(self) -> None:
        AllowlistStore(self.path).add(42)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_corrupt_file_is_moved_aside(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertLogs("bombagif.selfbot", "WARNING"):
            store = AllowlistStore(self.path)
        self.assertEqual(len(store), 0)
        self.assertTrue(self.path.with_name("allowlist.json.corrupt").exists())
        self.assertFalse(self.path.exists())


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.limiter = RateLimiter(limit=5, window=60.0, clock=self.clock)

    def test_allows_up_to_limit_then_blocks(self) -> None:
        self.assertEqual([self.limiter.allow(1) for _ in range(5)], [True] * 5)
        self.assertFalse(self.limiter.allow(1))

    def test_window_slides(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.clock.now += 61
        self.assertTrue(self.limiter.allow(1))

    def test_users_are_independent(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.assertTrue(self.limiter.allow(2))

    def test_blocked_attempts_do_not_extend_window(self) -> None:
        for _ in range(5):
            self.limiter.allow(1)
        self.clock.now += 30
        self.assertFalse(self.limiter.allow(1))
        self.clock.now += 31  # 61 s after the first five hits; the blocked try must not count
        self.assertTrue(self.limiter.allow(1))


def msg(**overrides: object) -> Incoming:
    fields: dict[str, object] = dict(
        author_id=7, author_is_bot=False, is_own=False, is_system=False,
        channel=ChannelKind.GUILD, mentions_me=False, content="", has_payload=False, is_reply=False,
    )
    fields.update(overrides)
    return Incoming(**fields)  # type: ignore[arg-type]


class DecideTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.allowlist = AllowlistStore(Path(self._tmp.name) / "a.json")
        self.allowlist.add(7)

    def stranger(self, **overrides: object) -> Incoming:
        return msg(author_id=99, **overrides)

    def test_ignores_own_bot_and_system(self) -> None:
        for flag in ("is_own", "author_is_bot", "is_system"):
            with self.subTest(flag=flag):
                decision = decide(msg(channel=ChannelKind.DM, has_payload=True, **{flag: True}), self.allowlist)
                self.assertEqual(decision.kind, Kind.IGNORE)

    def test_dm_stranger_registers(self) -> None:
        self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, content="hi"), self.allowlist), Decision(Kind.REGISTER, False))

    def test_dm_stranger_with_media_registers_and_converts(self) -> None:
        self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, has_payload=True), self.allowlist), Decision(Kind.REGISTER, True))

    def test_dm_stranger_stop_opts_out(self) -> None:
        # v2 ignored this; now a stranger who DMs `stop` is recorded as opted out of clipping
        self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, content="stop"), self.allowlist).kind, Kind.UNREGISTER)

    def test_stop_with_other_words_is_not_an_opt_out(self) -> None:
        self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, content="please stop that"), self.allowlist).kind, Kind.REGISTER)

    def test_dm_start_returns_start_for_members_and_strangers(self) -> None:
        for content in ("start", " START ", "Start\n"):
            with self.subTest(content=content):
                self.assertEqual(decide(msg(channel=ChannelKind.DM, content=content), self.allowlist).kind, Kind.START)
                self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, content=content), self.allowlist).kind, Kind.START)

    def test_start_outside_a_dm_does_nothing(self) -> None:
        self.assertEqual(decide(msg(content="start", mentions_me=True), self.allowlist).kind, Kind.CONVERT)  # a normal member ping
        self.assertEqual(decide(self.stranger(content="start"), self.allowlist).kind, Kind.IGNORE)

    def test_reply_stop_to_a_clip_opts_out_the_replier_only(self) -> None:
        for author_in, label in ((msg, "member"), (self.stranger, "stranger")):
            for channel in (ChannelKind.GUILD, ChannelKind.GROUP):
                with self.subTest(author=label, channel=channel):
                    decision = decide(author_in(channel=channel, content=" Stop ", is_clip_reply=True), self.allowlist)
                    self.assertEqual(decision.kind, Kind.OPT_OUT_CLIPS)

    def test_stop_reply_to_a_non_clip_message_is_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(content="stop", is_reply=True), self.allowlist).kind, Kind.IGNORE)
        self.assertEqual(decide(msg(content="stop", is_reply=True), self.allowlist).kind, Kind.IGNORE)

    def test_other_text_in_a_reply_to_a_clip_is_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(content="lol that was me", is_clip_reply=True), self.allowlist).kind, Kind.IGNORE)

    def test_clip_reply_from_a_bot_is_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(content="stop", is_clip_reply=True, author_is_bot=True), self.allowlist).kind, Kind.IGNORE)

    def test_stranger_mention_returns_help(self) -> None:
        for channel in (ChannelKind.GUILD, ChannelKind.GROUP):
            with self.subTest(channel=channel):
                self.assertEqual(decide(self.stranger(channel=channel, mentions_me=True, has_payload=True), self.allowlist).kind, Kind.HELP)

    def test_stranger_without_a_mention_is_still_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(has_payload=True), self.allowlist).kind, Kind.IGNORE)

    def test_incoming_without_is_clip_reply_defaults_to_false(self) -> None:
        self.assertFalse(msg().is_clip_reply)

    def test_dm_member_stop_unregisters(self) -> None:
        for content in ("stop", " STOP ", "Stop\n"):
            with self.subTest(content=content):
                self.assertEqual(decide(msg(channel=ChannelKind.DM, content=content), self.allowlist).kind, Kind.UNREGISTER)

    def test_dm_member_media_converts(self) -> None:
        self.assertEqual(decide(msg(channel=ChannelKind.DM, has_payload=True), self.allowlist).kind, Kind.CONVERT)

    def test_dm_member_reply_converts(self) -> None:
        self.assertEqual(decide(msg(channel=ChannelKind.DM, is_reply=True), self.allowlist).kind, Kind.CONVERT)

    def test_dm_member_plain_text_ignored(self) -> None:
        self.assertEqual(decide(msg(channel=ChannelKind.DM, content="hello"), self.allowlist).kind, Kind.IGNORE)

    def test_guild_mention_from_member_converts(self) -> None:
        self.assertEqual(decide(msg(mentions_me=True), self.allowlist).kind, Kind.CONVERT)

    def test_guild_mention_from_stranger_gets_help_not_a_conversion(self) -> None:
        self.assertEqual(decide(self.stranger(mentions_me=True, has_payload=True), self.allowlist).kind, Kind.HELP)

    def test_guild_media_without_mention_ignored(self) -> None:
        self.assertEqual(decide(msg(has_payload=True), self.allowlist).kind, Kind.IGNORE)

    def test_group_mention_from_member_converts(self) -> None:
        self.assertEqual(decide(msg(channel=ChannelKind.GROUP, mentions_me=True), self.allowlist).kind, Kind.CONVERT)


def att(name: str) -> SimpleNamespace:
    return SimpleNamespace(filename=name, url=f"https://cdn.discordapp.com/{name}", size=10, content_type=None)


def fake_message(*names: str) -> SimpleNamespace:
    return SimpleNamespace(attachments=[att(n) for n in names], embeds=[])


class CollectMediaTests(unittest.TestCase):
    def test_own_attachments_win_over_referenced(self) -> None:
        collected = collect_media(fake_message("mine.png"), fake_message("theirs.png"))
        self.assertEqual([s.filename for s in collected.sources], ["mine.png"])

    def test_falls_back_to_referenced(self) -> None:
        collected = collect_media(fake_message(), fake_message("theirs.png"))
        self.assertEqual([s.filename for s in collected.sources], ["theirs.png"])

    def test_unsupported_files_dropped_silently(self) -> None:
        collected = collect_media(fake_message("notes.txt", "a.png"))
        self.assertEqual([s.filename for s in collected.sources], ["a.png"])
        self.assertEqual(collected.skipped, 0)

    def test_unsupported_own_files_fall_back_to_referenced(self) -> None:
        collected = collect_media(fake_message("notes.txt"), fake_message("theirs.webp"))
        self.assertEqual([s.filename for s in collected.sources], ["theirs.webp"])

    def test_caps_at_limit_and_counts_skipped(self) -> None:
        collected = collect_media(fake_message(*[f"{i}.png" for i in range(7)]))
        self.assertEqual(len(collected.sources), 5)
        self.assertEqual(collected.skipped, 2)

    def test_no_media_returns_empty(self) -> None:
        collected = collect_media(fake_message())
        self.assertEqual((collected.sources, collected.skipped), ([], 0))

    def test_referenced_none_is_fine(self) -> None:
        self.assertEqual(collect_media(fake_message("a.mp4"), None).sources[0].filename, "a.mp4")

    def test_sources_carry_attachment_fields(self) -> None:
        source = collect_media(fake_message("a.png")).sources[0]
        self.assertEqual((source.url, source.size), ("https://cdn.discordapp.com/a.png", 10))


class FormatResultsTests(unittest.TestCase):
    def test_single_ok_is_bare_link(self) -> None:
        self.assertEqual(format_results([("a.png", "https://z.example/u/a.gif", True)]), "https://z.example/u/a.gif")

    def test_multiple_lines_and_errors(self) -> None:
        text = format_results([("a.png", "https://z.example/a.gif", True), ("b.png", "Too big.", False)])
        self.assertEqual(text, "**a.png** → https://z.example/a.gif\n**b.png**: Too big.")

    def test_single_error_is_a_labelled_line(self) -> None:
        self.assertEqual(format_results([("b.png", "Too big.", False)]), "**b.png**: Too big.")

    def test_filename_markdown_escaped(self) -> None:
        text = format_results([("a_b*.png", "x", False), ("c.png", "y", False)])
        self.assertIn("**a\\_b\\*.png**", text)

    def test_skipped_note(self) -> None:
        text = format_results([("a.png", "https://z.example/a.gif", True)], skipped=2)
        self.assertEqual(text, "https://z.example/a.gif\n(+2 skipped, max 5 per message)")

    def test_truncates_to_2000_at_line_boundary(self) -> None:
        results = [(f"file{i}.png", "https://z.example/" + "x" * 150 + ".gif", True) for i in range(30)]
        text = format_results(results)
        self.assertLessEqual(len(text), 2000)
        self.assertTrue(text.startswith("**file0.png** → https://z.example/"))
        self.assertTrue(text.splitlines()[-1].endswith(".gif"))

    def test_welcome_text_variants_fit_limits(self) -> None:
        self.assertEqual(welcome_text(False), WELCOME_TEXT)
        scout = welcome_text(True)
        self.assertLessEqual(len(scout), 500)
        self.assertIn("clip", scout.lower())
        self.assertIn("stop", scout.lower())
        self.assertTrue(scout.startswith(WELCOME_TEXT))
        opted_out = welcome_text(True, opted_out=True)
        self.assertIn("`start`", opted_out)
        self.assertLessEqual(len(opted_out), 500)
        self.assertNotIn("clip", welcome_text(False).lower())

    def test_confirmation_copy(self) -> None:
        self.assertIn("clip", STOP_CONFIRMATION.lower())
        self.assertIn("convert", STOP_CONFIRMATION.lower())
        self.assertIn("`start`", STOP_CONFIRMATION)
        self.assertIn("clip", START_CONFIRMATION.lower())
        self.assertIn("`stop`", START_CONFIRMATION)
        self.assertIn("`start`", CLIP_OPT_OUT_CONFIRMATION)
        for text in (STOP_CONFIRMATION, START_CONFIRMATION, CLIP_OPT_OUT_CONFIRMATION):
            self.assertLessEqual(len(text), 300)

    def test_copy_constants(self) -> None:
        self.assertIn("stop", WELCOME_TEXT.lower())
        self.assertLessEqual(len(WELCOME_TEXT), 400)
        self.assertTrue(STOP_CONFIRMATION)
        self.assertTrue(NO_MEDIA_TEXT)


def embed(kind: str, *, image: str | None = None, video: str | None = None, thumb: str | None = None, url: str | None = None) -> SimpleNamespace:
    def media(proxy: str | None) -> SimpleNamespace | None:
        return SimpleNamespace(url=proxy, proxy_url=proxy) if proxy else None

    return SimpleNamespace(type=kind, image=media(image), video=media(video), thumbnail=media(thumb), url=url)


def message_with_embeds(*embeds: SimpleNamespace, attachments: tuple[str, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(attachments=[att(n) for n in attachments], embeds=list(embeds))


class CollectEmbedTests(unittest.TestCase):
    def test_collect_embed_image(self) -> None:
        msg = message_with_embeds(embed("image", image="https://media.discordapp.net/external/h/https/i.example/cat.png?w=1"))
        sources = collect_media(msg).sources
        self.assertEqual([(s.filename, s.url) for s in sources], [("cat.png", "https://media.discordapp.net/external/h/https/i.example/cat.png?w=1")])

    def test_collect_gifv_uses_video_proxy_and_mp4_suffix(self) -> None:
        msg = message_with_embeds(embed("gifv", video="https://images-ext-1.discordapp.net/external/h/https/media.tenor.com/abc/tenor", url="https://tenor.com/view/x"))
        sources = collect_media(msg).sources
        self.assertEqual(len(sources), 1)
        self.assertTrue(sources[0].filename.endswith(".mp4"))
        self.assertTrue(sources[0].url.startswith("https://images-ext-1.discordapp.net/"))

    def test_collect_video_embed_with_mp4_name_is_kept(self) -> None:
        msg = message_with_embeds(embed("video", video="https://media.discordapp.net/external/h/clip.webm"))
        self.assertEqual(collect_media(msg).sources[0].filename, "clip.webm")

    def test_image_embed_falls_back_to_thumbnail(self) -> None:
        msg = message_with_embeds(embed("image", thumb="https://media.discordapp.net/external/h/t.jpg"))
        self.assertEqual(collect_media(msg).sources[0].filename, "t.jpg")

    def test_collect_drops_non_discord_embed_host(self) -> None:
        msg = message_with_embeds(
            embed("image", image="https://evil.example/a.png"),
            embed("gifv", video="http://127.0.0.1/a.mp4"),
        )
        self.assertEqual(collect_media(msg).sources, [])

    def test_unsupported_embed_types_ignored(self) -> None:
        msg = message_with_embeds(embed("rich", image="https://media.discordapp.net/a.png"), embed("link"))
        self.assertEqual(collect_media(msg).sources, [])

    def test_attachments_still_win_over_embeds(self) -> None:
        msg = message_with_embeds(embed("image", image="https://media.discordapp.net/e.png"), attachments=("mine.png",))
        self.assertEqual([s.filename for s in collect_media(msg).sources], ["mine.png"])

    def test_own_embeds_win_over_referenced_attachments(self) -> None:
        mine = message_with_embeds(embed("image", image="https://media.discordapp.net/e.png"))
        theirs = message_with_embeds(attachments=("theirs.png",))
        self.assertEqual([s.filename for s in collect_media(mine, theirs).sources], ["e.png"])

    def test_referenced_embeds_are_used_last(self) -> None:
        theirs = message_with_embeds(embed("image", image="https://media.discordapp.net/r.png"))
        self.assertEqual([s.filename for s in collect_media(message_with_embeds(), theirs).sources], ["r.png"])

    def test_embeds_count_against_batch_cap(self) -> None:
        msg = message_with_embeds(*[embed("image", image=f"https://media.discordapp.net/{i}.png") for i in range(6)])
        collected = collect_media(msg)
        self.assertEqual((len(collected.sources), collected.skipped), (5, 1))


class CaptchaGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "data" / "captcha-blocked.json"

    def test_starts_unblocked_without_state_file(self) -> None:
        guard = CaptchaGuard(self.path)
        self.assertFalse(guard.blocked)

    def test_trip_blocks_and_persists_across_instances(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            self.assertTrue(CaptchaGuard(self.path).trip("friend request acceptance"))
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            reloaded = CaptchaGuard(self.path)
        self.assertTrue(reloaded.blocked)
        self.assertEqual(reloaded.reason, "friend request acceptance")

    def test_second_trip_is_a_no_op_and_keeps_first_reason(self) -> None:
        guard = CaptchaGuard(self.path)
        with self.assertLogs("bombagif.selfbot", "ERROR") as logs:
            self.assertTrue(guard.trip("reply"))
            self.assertFalse(guard.trip("typing"))
        self.assertEqual(len(logs.records), 1)
        self.assertEqual(guard.reason, "reply")

    def test_state_file_mode_is_600(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            CaptchaGuard(self.path).trip("reply")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_unreadable_state_file_still_blocks(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{garbage", encoding="utf-8")
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            guard = CaptchaGuard(self.path)
        self.assertTrue(guard.blocked)

    def test_deleting_the_file_clears_the_block(self) -> None:
        with self.assertLogs("bombagif.selfbot", "ERROR"):
            CaptchaGuard(self.path).trip("reply")
        self.path.unlink()
        self.assertFalse(CaptchaGuard(self.path).blocked)


class EnvFlagTests(unittest.TestCase):
    def test_truthy_values(self) -> None:
        for value in ("1", "true", "TRUE", " yes ", "on"):
            with self.subTest(value=value):
                self.assertTrue(env_flag(value))

    def test_everything_else_is_false(self) -> None:
        for value in (None, "", "0", "false", "no", "off", "maybe"):
            with self.subTest(value=value):
                self.assertFalse(env_flag(value))


class BotStatsTests(unittest.TestCase):
    def test_counters_start_at_zero(self) -> None:
        stats = BotStats(started_at=100.0)
        self.assertEqual(
            (stats.triggers, stats.conversions_ok, stats.conversions_failed, stats.rate_limited, stats.registered, stats.unregistered, stats.friends_accepted),
            (0, 0, 0, 0, 0, 0, 0),
        )
        self.assertEqual(len(stats.activity), 0)

    def test_scout_counters_default_to_zero(self) -> None:
        stats = BotStats(started_at=0.0)
        self.assertEqual(
            (stats.scout_screened, stats.scout_passed, stats.clips_posted, stats.deleted_rescued, stats.dry_run_hits, stats.scout_rejected),
            (0, 0, 0, 0, 0, 0),
        )

    def test_note_keeps_newest_entries_in_order(self) -> None:
        stats = BotStats(started_at=0.0, activity_size=3)
        for index in range(5):
            stats.note("info", f"event {index}", now=float(index))
        self.assertEqual([entry.text for entry in stats.activity], ["event 2", "event 3", "event 4"])
        self.assertEqual(stats.activity[-1].level, "info")
        self.assertEqual(stats.activity[-1].at, 4.0)

    def test_uptime_formatting(self) -> None:
        stats = BotStats(started_at=1000.0)
        cases = {0: "0s", 59: "59s", 65: "1m 05s", 3725: "1h 02m 05s", 90061: "1d 01h 01m"}
        for seconds, expected in cases.items():
            with self.subTest(seconds=seconds):
                self.assertEqual(stats.uptime(now=1000.0 + seconds), expected)

    def test_uptime_never_negative(self) -> None:
        self.assertEqual(BotStats(started_at=50.0).uptime(now=10.0), "0s")


class ResolveUiModeTests(unittest.TestCase):
    def test_auto_follows_the_terminal(self) -> None:
        for setting in (None, "", "auto", " AUTO "):
            with self.subTest(setting=setting):
                self.assertEqual(resolve_ui_mode(setting, is_tty=True), "pretty")
                self.assertEqual(resolve_ui_mode(setting, is_tty=False), "json")

    def test_explicit_modes_win_over_the_terminal(self) -> None:
        self.assertEqual(resolve_ui_mode("pretty", is_tty=False), "pretty")
        self.assertEqual(resolve_ui_mode("JSON", is_tty=True), "json")

    def test_unknown_mode_names_the_variable(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "SELFBOT_UI"):
            resolve_ui_mode("fancy", is_tty=True)


class CooldownTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.cooldown = Cooldown(60.0, clock=self.clock)

    def test_cooldown_blocks_until_elapsed_per_key(self) -> None:
        self.assertTrue(self.cooldown.ready("a"))
        self.assertFalse(self.cooldown.ready("a"))
        self.assertTrue(self.cooldown.ready("b"))  # keys are independent
        self.clock.now += 59
        self.assertFalse(self.cooldown.ready("a"))
        self.clock.now += 1
        self.assertTrue(self.cooldown.ready("a"))

    def test_is_ready_does_not_record_and_mark_does(self) -> None:
        self.assertTrue(self.cooldown.is_ready("a"))
        self.assertTrue(self.cooldown.is_ready("a"))
        self.cooldown.mark("a")
        self.assertFalse(self.cooldown.is_ready("a"))
        self.clock.now += 60
        self.assertTrue(self.cooldown.is_ready("a"))

    def test_blocked_attempts_do_not_extend_the_cooldown(self) -> None:
        self.cooldown.ready("a")
        self.clock.now += 30
        self.assertFalse(self.cooldown.ready("a"))
        self.clock.now += 30
        self.assertTrue(self.cooldown.ready("a"))


class HelpTextTests(unittest.TestCase):
    def test_help_text_explains_the_dm_first_rule(self) -> None:
        for scout_on in (False, True):
            text = help_text(scout_on)
            with self.subTest(scout_on=scout_on):
                self.assertIn("DM me", text)
                self.assertIn("`hi`", text)
                self.assertLessEqual(len(text), 600)

    def test_clipping_is_mentioned_only_when_the_scout_is_on(self) -> None:
        self.assertNotIn("clip", help_text(False).lower())
        self.assertIn("clip", help_text(True).lower())
        self.assertIn("`stop`", help_text(True))


class UpdateNoticeTests(unittest.TestCase):
    """The DM that tells the allowlist what an unattended update changed."""

    def notice(self, **overrides: object) -> dict:
        base = {
            "from": "f9ee931",
            "to": "6160b39",
            "outcome": "updated",
            "at": 1_763_866_490,
            "commits": [
                {"sha": "6160b39", "subject": "Let a training phase review every judged run"},
                {"sha": "1410a30", "subject": "Fix video conversion: drop bare -autorotate"},
            ],
        }
        base.update(overrides)
        return base

    def test_names_the_new_version_and_every_change(self) -> None:
        text = update_notice_text(self.notice())
        self.assertTrue(text.startswith("# Bombagif updated itself"))
        self.assertIn("`6160b39`", text)
        self.assertIn("<t:1763866490:R>", text)
        self.assertIn("2 changes", text)
        self.assertIn("- **Let** a training phase review every judged run", text)
        self.assertIn("- **Fix** video conversion: drop bare -autorotate", text)

    def test_one_change_is_singular(self) -> None:
        text = update_notice_text(self.notice(commits=[{"sha": "a", "subject": "Add a thing"}]))
        self.assertIn("1 change", text)
        self.assertNotIn("1 changes", text)

    def test_rolled_back_says_so_and_names_both_versions(self) -> None:
        text = update_notice_text(self.notice(outcome="rolled-back"))
        self.assertTrue(text.startswith("# Update rolled back"))
        self.assertIn("`6160b39`", text)
        self.assertIn("`f9ee931`", text)
        self.assertIn("## What it was going to bring", text)
        self.assertNotIn("What's new", text)

    def test_subject_markdown_is_escaped(self) -> None:
        text = update_notice_text(self.notice(commits=[{"sha": "a", "subject": "Fix *bold* and `code`"}]))
        self.assertIn(r"\*bold\*", text)
        self.assertIn(r"\`code\`", text)

    def test_a_long_update_is_capped(self) -> None:
        commits = [{"sha": f"{index:07x}", "subject": f"Change number {index}"} for index in range(30)]
        text = update_notice_text(self.notice(commits=commits))
        self.assertIn("and 18 more", text)
        self.assertLessEqual(len(text), 1900)

    def test_a_notice_without_commits_still_renders(self) -> None:
        text = update_notice_text(self.notice(commits=[]))
        self.assertIn("`6160b39`", text)
        self.assertNotIn("## What's new", text)

    def test_the_opt_out_flag_is_parsed(self) -> None:
        for value in ("0", "false", "NO", " off "):
            with self.subTest(value=value):
                self.assertTrue(env_flag_off(value))
        for value in (None, "", "1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertFalse(env_flag_off(value))


class TimezoneTests(unittest.TestCase):
    EPOCH = 1_800_000_000  # 2027-01-15 08:00:00 UTC

    def setUp(self) -> None:
        import os
        import time

        self._old = os.environ.get("TZ")
        self.addCleanup(self._restore)
        self.time, self.os = time, os

    def _restore(self) -> None:
        if self._old is None:
            self.os.environ.pop("TZ", None)
        else:
            self.os.environ["TZ"] = self._old
        self.time.tzset()

    def _hour(self) -> int:
        return self.time.localtime(self.EPOCH).tm_hour

    def test_tz_changes_the_local_clock(self) -> None:
        self.assertEqual(apply_timezone({"TZ": "UTC"}), "UTC")
        self.assertEqual(self._hour(), 8)
        self.assertEqual(apply_timezone({"TZ": "America/New_York"}), "America/New_York")
        self.assertEqual(self._hour(), 3)  # EST, UTC-5, in January
        apply_timezone({"TZ": "Asia/Kolkata"})
        self.assertEqual(self.time.localtime(self.EPOCH).tm_min, 30)

    def test_clip_card_times_follow_the_timezone(self) -> None:
        from clip_render import format_time

        apply_timezone({"TZ": "America/Los_Angeles"})
        self.assertEqual(format_time(self.EPOCH), "12:00 AM")

    def test_unset_or_blank_leaves_the_host_zone_alone(self) -> None:
        self.assertIsNone(apply_timezone({}))
        self.assertIsNone(apply_timezone({"TZ": "  "}))

    def test_an_unknown_zone_is_a_clear_error(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "TZ"):
            apply_timezone({"TZ": "Mars/Olympus"})
        with self.assertRaisesRegex(RuntimeError, "TZ"):
            apply_timezone({"TZ": "../etc/passwd"})


if __name__ == "__main__":
    unittest.main()
