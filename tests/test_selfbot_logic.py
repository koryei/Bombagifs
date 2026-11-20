"""Discord-free self-bot logic: allowlist, rate limiting, decisions, media collection, copy."""

from __future__ import annotations

import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from selfbot_logic import (
    NO_MEDIA_TEXT,
    STOP_CONFIRMATION,
    WELCOME_TEXT,
    AllowlistStore,
    ChannelKind,
    Decision,
    Incoming,
    Kind,
    RateLimiter,
    collect_media,
    decide,
    format_results,
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

    def test_dm_stranger_stop_is_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(channel=ChannelKind.DM, content="stop"), self.allowlist).kind, Kind.IGNORE)

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

    def test_guild_mention_from_stranger_ignored(self) -> None:
        self.assertEqual(decide(self.stranger(mentions_me=True, has_payload=True), self.allowlist).kind, Kind.IGNORE)

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


if __name__ == "__main__":
    unittest.main()
