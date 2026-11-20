"""Terminal UI rendering (needs rich, so it runs in .venv-selfbot)."""

from __future__ import annotations

import unittest
from importlib.util import find_spec

HAS_RICH = find_spec("rich") is not None

import logging
import re
import time

from selfbot_logic import BotStats

if HAS_RICH:
    import console_ui


def render(renderable: object, width: int = 100) -> str:
    console = console_ui.create_console(record=True, width=width, force_terminal=False, color_system=None)
    console.print(renderable)
    return console.export_text()


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class BootBannerTests(unittest.TestCase):
    INFO = {"account": "Bombaclat#0001", "zipline": "gifs.example.com", "auto-accept friends": "off"}

    def test_banner_shows_logo_title_and_info_rows(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO))
        self.assertIn("/ __ )/ __ \\/  |/  / __ )/   | / ____/  _/ ____/", text)  # the logo
        self.assertIn("Bombagif", text)
        for label, value in self.INFO.items():
            self.assertIn(label, text)
            self.assertIn(value, text)

    LOGO_LINE = "/ __ )/ __ \\/  |/  / __ )/   | / ____/  _/ ____/"

    def test_wide_terminal_puts_logo_and_facts_side_by_side(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=100)
        row = next(line for line in text.splitlines() if self.LOGO_LINE in line)
        self.assertIn("zipline", row)

    def test_medium_terminal_stacks_facts_under_the_logo(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=70)
        self.assertIn(self.LOGO_LINE, text)
        self.assertIn("Bombaclat#0001", text)
        self.assertIn("gifs.example.com", text)
        self.assertTrue(all("zipline" not in line for line in text.splitlines() if self.LOGO_LINE in line))

    def test_narrow_terminal_drops_the_logo_and_keeps_full_values(self) -> None:
        text = render(console_ui.render_boot_banner(self.INFO), width=50)
        self.assertNotIn("/ __ )", text)
        self.assertIn("Bombaclat#0001", text)
        self.assertIn("gifs.example.com", text)

    def test_create_console_uses_the_theme(self) -> None:
        console = console_ui.create_console()
        self.assertTrue(console.get_style("brand").bold)
        self.assertTrue(console.get_style("bad").bold)


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class DashboardTests(unittest.TestCase):
    def stats(self) -> BotStats:
        return BotStats(started_at=0.0)

    def test_online_status_uptime_and_allowlist_size(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.stats(), status="online", allowlist_size=3, now=3725.0)))
        self.assertIn("online", text)
        self.assertIn("uptime 1h 02m 05s", text)
        self.assertIn("3 allowlisted", text)

    def test_paused_status_is_loud(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.stats(), status="paused", allowlist_size=0, now=1.0)))
        self.assertIn("CAPTCHA", text)
        self.assertNotIn("online", text)

    def test_connecting_status(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.stats(), status="connecting", allowlist_size=0, now=1.0)))
        self.assertIn("connecting", text)

    def test_counters_are_shown(self) -> None:
        stats = self.stats()
        stats.conversions_ok, stats.conversions_failed, stats.triggers = 12, 1, 13
        stats.rate_limited, stats.registered, stats.friends_accepted = 2, 3, 4
        text = squash(render(console_ui.render_dashboard(stats, status="online", allowlist_size=3, now=1.0)))
        for expected in ("converted 12", "failed 1", "triggers 13", "rate limited 2", "new users 3", "friends 4"):
            self.assertIn(expected, text)

    def test_empty_feed_has_a_hint(self) -> None:
        text = render(console_ui.render_dashboard(self.stats(), status="online", allowlist_size=0, now=1.0))
        self.assertIn("Waiting for the first DM or ping", text)

    def test_feed_lists_entries_oldest_first_with_icons(self) -> None:
        stats = self.stats()
        stats.note("info", "koryei said hi: registered", now=100.0)
        stats.note("ok", "koryei: cat.png converted (1.2s)", now=101.0)
        stats.note("fail", "pranit: big.png: too large", now=102.0)
        stats.note("warn", "pranit rate limited", now=103.0)
        text = render(console_ui.render_dashboard(stats, status="online", allowlist_size=1, now=104.0))
        positions = [text.index(s) for s in ("registered", "cat.png", "big.png", "pranit rate limited")]
        self.assertEqual(positions, sorted(positions))
        for icon in ("\u2714", "\u2716", "!", "\u2022"):
            self.assertIn(icon, text)
        self.assertIn(time.strftime("%H:%M:%S", time.localtime(101.0)), text)

    def test_long_feed_text_is_truncated_not_wrapped(self) -> None:
        stats = self.stats()
        stats.note("ok", "x" * 300, now=1.0)
        text = render(console_ui.render_dashboard(stats, status="online", allowlist_size=0, now=2.0), width=60)
        self.assertTrue(all(len(line) <= 60 for line in text.splitlines()))

    def test_narrow_terminal_still_renders_counters(self) -> None:
        stats = self.stats()
        stats.conversions_ok = 5
        text = squash(render(console_ui.render_dashboard(stats, status="online", allowlist_size=0, now=1.0), width=40))
        self.assertIn("converted 5", text)

    def test_live_dashboard_object_renders_current_state(self) -> None:
        stats = self.stats()
        state = {"status": "connecting"}
        dashboard = console_ui.Dashboard(stats, lambda: state["status"], lambda: 7, clock=lambda: 61.0)
        self.assertIn("connecting", squash(render(dashboard)))
        state["status"] = "online"
        stats.conversions_ok = 2
        text = squash(render(dashboard))
        self.assertIn("converted 2", text)
        self.assertIn("7 allowlisted", text)
        self.assertIn("uptime 1m 01s", text)

    def test_live_dashboard_leaves_room_for_a_echoed_ctrl_c(self) -> None:
        # A full-width panel makes the terminal's "^C" echo wrap below it, which breaks Rich's cursor maths.
        stats = self.stats()
        stats.note("ok", "y" * 300, now=1.0)
        for width in (60, 100, 150):
            with self.subTest(width=width):
                dashboard = console_ui.Dashboard(stats, lambda: "online", lambda: 1, clock=lambda: 2.0)
                text = render(dashboard, width=width)
                self.assertLessEqual(max(len(line) for line in text.splitlines()), width - 3)

    def scout_stats(self) -> BotStats:
        stats = self.stats()
        stats.scout_screened, stats.scout_passed, stats.clips_posted = 40, 6, 3
        stats.deleted_rescued, stats.dry_run_hits, stats.scout_rejected = 1, 5, 2
        return stats

    def test_dashboard_shows_the_scout_row_only_when_enabled(self) -> None:
        plain = squash(render(console_ui.render_dashboard(self.scout_stats(), status="online", allowlist_size=1, now=1.0)))
        self.assertNotIn("screened", plain)
        self.assertNotIn("judge", plain)
        scouting = squash(render(console_ui.render_dashboard(self.scout_stats(), status="online", allowlist_size=1, now=1.0, scout=("dry-run", "inception", True))))
        self.assertIn("screened", scouting)

    def test_scout_counters_render(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.scout_stats(), status="online", allowlist_size=1, now=1.0, scout=("on", "inception", True))))
        for expected in ("screened 40", "passed 6", "clips 3", "deleted rescued 1", "dry-run hits 5", "rejected 2"):
            self.assertIn(expected, text)

    def test_judge_status_and_mode_in_the_header(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.stats(), status="online", allowlist_size=1, now=1.0, scout=("on", "inception", True))))
        self.assertIn("scout on", text)
        self.assertIn("judge inception", text)
        self.assertNotIn("paused", text)

    def test_a_judge_that_cannot_post_is_shown_as_paused(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.stats(), status="online", allowlist_size=1, now=1.0, scout=("dry-run", "heuristic (no LLM)", False))))
        self.assertIn("judge paused", text)
        self.assertIn("heuristic (no LLM)", text)

    def test_scout_rows_survive_a_narrow_terminal(self) -> None:
        text = squash(render(console_ui.render_dashboard(self.scout_stats(), status="online", allowlist_size=1, now=1.0, scout=("on", "inception", True)), width=50))
        self.assertIn("screened 40", text)
        self.assertIn("clips 3", text)

    def test_live_dashboard_reads_the_scout_view_each_time(self) -> None:
        stats = self.stats()
        view = {"value": None}
        dashboard = console_ui.Dashboard(stats, lambda: "online", lambda: 1, clock=lambda: 5.0, get_scout=lambda: view["value"])
        self.assertNotIn("screened", squash(render(dashboard)))
        view["value"] = ("on", "openai", True)
        stats.scout_screened = 9
        text = squash(render(dashboard))
        self.assertIn("screened 9", text)
        self.assertIn("judge openai", text)

    def test_shutdown_panel_summarizes_the_session(self) -> None:
        stats = self.stats()
        stats.conversions_ok, stats.conversions_failed = 12, 1
        text = squash(render(console_ui.render_shutdown_panel(stats, now=3725.0)))
        self.assertIn("signed off", text)
        self.assertIn("12 converted", text)
        self.assertIn("1 failed", text)
        self.assertIn("1h 02m 05s", text)


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class PrettyLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = console_ui.create_console(record=True, width=110, force_terminal=False, color_system=None)
        self.handler = console_ui.PrettyLogHandler(self.console)
        self.logger = logging.getLogger("bombagif.test_pretty")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)
        self.addCleanup(lambda: setattr(self.logger, "handlers", []))

    def output(self) -> str:
        return self.console.export_text()

    def test_line_has_time_level_short_logger_and_message(self) -> None:
        self.logger.info("Self-bot connected")
        line = self.output().strip()
        self.assertRegex(line, r"^\d\d:\d\d:\d\d\s+INFO\s+test_pretty\s+Self-bot connected$")

    def test_levels_are_labelled(self) -> None:
        self.logger.warning("w")
        self.logger.error("e")
        out = self.output()
        self.assertIn("WARN", out)
        self.assertIn("ERROR", out)

    def test_known_extras_are_appended_and_trace_ids_shortened(self) -> None:
        self.logger.info("Processing", extra={"user_id": 5, "trace_id": "abcdef1234567890", "attachment_name": "a.png"})
        out = self.output()
        self.assertIn("user_id=5", out)
        self.assertIn("trace=abcdef12", out)
        self.assertNotIn("abcdef1234567890", out)
        self.assertIn("file=a.png", out)

    def test_square_brackets_are_not_markup(self) -> None:
        self.logger.info("weird [red]name[/red].png")
        self.assertIn("weird [red]name[/red].png", self.output())

    def test_exceptions_show_their_type_and_message(self) -> None:
        try:
            raise ValueError("boom")
        except ValueError:
            self.logger.exception("failed")
        out = self.output()
        self.assertIn("failed", out)
        self.assertIn("ValueError", out)
        self.assertIn("boom", out)

    def test_discord_library_chatter_is_hidden_but_warnings_show(self) -> None:
        root = logging.getLogger()
        discord_logger = logging.getLogger("discord")
        saved, level, discord_level = root.handlers[:], root.level, discord_logger.level
        self.addCleanup(lambda: (root.handlers.__setitem__(slice(None), saved), root.setLevel(level), discord_logger.setLevel(discord_level)))
        console_ui.configure_pretty_logging(self.console, "INFO")
        logging.getLogger("discord.http").info("Found user agent Mozilla")
        logging.getLogger("discord.gateway").warning("Gateway hiccup")
        out = self.output()
        self.assertNotIn("Found user agent", out)
        self.assertIn("Gateway hiccup", out)

    def test_debug_level_keeps_discord_logs(self) -> None:
        root = logging.getLogger()
        discord_logger = logging.getLogger("discord")
        saved, level, discord_level = root.handlers[:], root.level, discord_logger.level
        self.addCleanup(lambda: (root.handlers.__setitem__(slice(None), saved), root.setLevel(level), discord_logger.setLevel(discord_level)))
        console_ui.configure_pretty_logging(self.console, "DEBUG")
        logging.getLogger("discord.http").info("Found user agent Mozilla")
        self.assertIn("Found user agent", self.output())

    def test_configure_pretty_logging_replaces_root_handlers(self) -> None:
        root = logging.getLogger()
        saved, level = root.handlers[:], root.level
        self.addCleanup(lambda: (root.handlers.__setitem__(slice(None), saved), root.setLevel(level)))
        console_ui.configure_pretty_logging(self.console, "INFO")
        self.assertEqual([type(h) for h in root.handlers], [console_ui.PrettyLogHandler])
        logging.getLogger("bombagif.selfbot").info("hello pretty")
        self.assertIn("hello pretty", self.output())


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class TerminalUiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.console = console_ui.create_console(record=True, width=100, force_terminal=True, color_system=None)
        self.stats = BotStats(started_at=0.0)
        dashboard = console_ui.Dashboard(self.stats, lambda: "online", lambda: 2)
        self.ui = console_ui.TerminalUi(self.console, dashboard)
        self.addCleanup(self.ui.stop, self.stats)

    def test_show_boot_prints_the_banner_once(self) -> None:
        info = {"account": "Bombaclat#0001"}
        self.ui.show_boot(info)
        self.ui.show_boot(info)
        text = self.console.export_text()
        self.assertEqual(text.count("Bombagif self-bot is online"), 1)
        self.assertIn("Bombaclat#0001", text)

    def test_the_last_live_frame_stays_on_screen_after_stop(self) -> None:
        self.stats.conversions_ok = 4
        self.ui.show_boot({"account": "x"})
        self.ui.stop(self.stats)
        text = self.console.export_text()
        self.assertIn("recent activity", text)  # the final dashboard frame was not erased
        self.assertLess(text.index("recent activity"), text.index("signed off"))

    def test_stop_prints_the_shutdown_summary_and_is_idempotent(self) -> None:
        self.stats.conversions_ok = 3
        self.ui.show_boot({"account": "x"})
        self.ui.stop(self.stats)
        self.ui.stop(self.stats)
        text = self.console.export_text()
        self.assertEqual(text.count("signed off"), 1)
        self.assertIn("3 converted", text)

    def test_stop_without_boot_prints_nothing(self) -> None:
        self.ui.stop(self.stats)
        self.assertNotIn("signed off", self.console.export_text())


@unittest.skipUnless(HAS_RICH, "needs rich (run in .venv-selfbot)")
class DemoTests(unittest.TestCase):
    def test_demo_runs_and_shows_every_piece(self) -> None:
        console = console_ui.create_console(record=True, width=100, force_terminal=True, color_system=None)
        console_ui.demo(console, pause=0.0)
        text = console.export_text()
        for expected in ("Bombagif self-bot is online", "converted", "signed off", "CAPTCHA"):
            self.assertIn(expected, text)


if __name__ == "__main__":
    unittest.main()
