"""Terminal UI rendering (needs rich, so it runs in .venv-selfbot)."""

from __future__ import annotations

import unittest
from importlib.util import find_spec

HAS_RICH = find_spec("rich") is not None

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

    def test_shutdown_panel_summarizes_the_session(self) -> None:
        stats = self.stats()
        stats.conversions_ok, stats.conversions_failed = 12, 1
        text = squash(render(console_ui.render_shutdown_panel(stats, now=3725.0)))
        self.assertIn("signed off", text)
        self.assertIn("12 converted", text)
        self.assertIn("1 failed", text)
        self.assertIn("1h 02m 05s", text)


if __name__ == "__main__":
    unittest.main()
