"""The dashboard screen: readable at a glance, and honest when the bot is not there."""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path

from rich.console import Console

import dashboard
from console_ui import THEME


def _snapshot(**overrides: object) -> dict[str, object]:
    snapshot: dict[str, object] = {
        "at": time.time() - 2,
        "state": "online",
        "account": "bombaclat",
        "zipline": "zip.example",
        "uptime_s": 183_420,
        "allowlist": 4,
        "scout": {"mode": "dry-run", "judge": "inception", "can_post": True, "chats": 2, "reviews_waiting": 0, "manual": True},
        "counters": {"converted": 12, "failed": 1, "triggers": 30, "rate_limited": 2, "new_users": 1, "friends": 0,
                     "judged": 41, "passed": 3, "posted": 0, "rejected": 3},
        "activity": [
            {"at": time.time() - 60, "level": "ok", "text": "clipped mika, jonas (funny 8.4)"},
            {"at": time.time() - 120, "level": "warn", "text": "clip rejected: author on cooldown (mika, jonas)"},
        ],
    }
    snapshot.update(overrides)
    return snapshot


def _render(snapshot: dict[str, object] | None, *, width: int = 120, age: float | None = 2.0, service: str = "active") -> str:
    console = Console(file=io.StringIO(), width=width, theme=THEME, highlight=False)
    console.print(dashboard.render_dashboard(snapshot, age=age, service=service, version="v3.2"))
    return console.file.getvalue()  # type: ignore[union-attr]


class ReadStatusTests(unittest.TestCase):
    def test_a_fresh_snapshot_reports_its_age(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(_snapshot(at=time.time() - 5)))
            snapshot, age = dashboard.read_status(path)
        self.assertIsNotNone(snapshot)
        self.assertGreaterEqual(age or 0, 4.0)

    def test_a_missing_or_broken_file_is_not_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dashboard.read_status(Path(tmp) / "nope.json"), (None, None))
            broken = Path(tmp) / "broken.json"
            broken.write_text("{not json")
            self.assertEqual(dashboard.read_status(broken), (None, None))
            broken.write_text("[1, 2]")
            self.assertEqual(dashboard.read_status(broken), (None, None))


class NeedsYouTests(unittest.TestCase):
    def test_a_healthy_bot_needs_nothing(self) -> None:
        self.assertEqual(dashboard.needs_you(_snapshot(), age=2.0, service="active"), [])

    def test_a_stopped_service_is_named_with_the_command_that_fixes_it(self) -> None:
        needs = dashboard.needs_you(_snapshot(), age=2.0, service="inactive")
        self.assertEqual(len(needs), 1)
        self.assertIn("selfbot-ctl.sh start", needs[0])

    def test_a_stale_snapshot_says_how_long_it_has_been_quiet(self) -> None:
        needs = dashboard.needs_you(_snapshot(), age=400.0, service="active")
        self.assertIn("no status for 6m 40s", needs[0])

    def test_a_captcha_pause_explains_how_to_resume(self) -> None:
        needs = dashboard.needs_you(_snapshot(state="paused"), age=2.0, service="active")
        self.assertIn("CAPTCHA", needs[0])
        self.assertIn("captcha-blocked.json", needs[0])

    def test_a_judge_that_cannot_post_names_the_keys(self) -> None:
        scout = {"mode": "on", "judge": "heuristic (no LLM)", "can_post": False, "chats": 1, "reviews_waiting": 0}
        needs = dashboard.needs_you(_snapshot(scout=scout), age=2.0, service="active")
        self.assertIn("INCEPTION_API_KEY", needs[0])

    def test_waiting_reviews_are_counted_in_plain_words(self) -> None:
        scout = {"mode": "dry-run", "judge": "inception", "can_post": True, "chats": 2, "reviews_waiting": 6}
        self.assertEqual(dashboard.needs_you(_snapshot(scout=scout), age=2.0, service="active"),
                         ["6 review DMs waiting for your rating"])

    def test_no_snapshot_says_what_is_missing(self) -> None:
        needs = dashboard.needs_you(None, age=None, service="active")
        self.assertEqual(len(needs), 1)
        self.assertIn("no status yet", needs[0])


class RenderTests(unittest.TestCase):
    def test_the_screen_names_the_bot_and_its_state(self) -> None:
        screen = _render(_snapshot())
        for expected in ("Bombagif", "online", "bombaclat", "zip.example", "4 allowlisted", "v3.2", "up 2d 02h"):
            self.assertIn(expected, screen)

    def test_the_screen_shows_the_scout_and_the_counters(self) -> None:
        screen = _render(_snapshot())
        for expected in ("Clip scout", "dry-run", "judge inception", "2 chats on", "judged 41", "posted 0", "converted 12"):
            self.assertIn(expected, screen)

    def test_activity_reads_as_sentences_not_log_lines(self) -> None:
        screen = _render(_snapshot())
        self.assertIn("clipped mika, jonas (funny 8.4)", screen)
        for jargon in ("clip=", "funny=0.0", "trace_id", "decide:", "logger"):
            self.assertNotIn(jargon, screen)

    def test_no_bare_discord_ids_reach_the_screen(self) -> None:
        screen = _render(_snapshot())
        self.assertNotRegex(screen, r"\b\d{17,20}\b")

    def test_it_fits_eighty_columns(self) -> None:
        screen = _render(_snapshot(), width=80)
        self.assertTrue(all(len(line) <= 80 for line in screen.splitlines()), "a line is wider than the terminal")
        self.assertIn("Clip scout", screen)

    def test_without_a_snapshot_it_says_so_instead_of_showing_zeros(self) -> None:
        screen = _render(None, age=None, service="inactive")
        self.assertIn("no status yet", screen)
        self.assertIn("selfbot-ctl.sh start", screen)
        self.assertNotIn("converted 0", screen)

    def test_a_stopped_service_is_not_reported_as_online(self) -> None:
        screen = _render(_snapshot(), service="inactive")  # the snapshot still says "online"
        self.assertIn("stopped", screen)
        self.assertNotIn("online", screen)

    def test_a_quiet_bot_is_flagged_on_screen(self) -> None:
        self.assertIn("no status for 6m 40s", _render(_snapshot(), age=400.0))


class ServiceStateTests(unittest.TestCase):
    def _fake_systemctl(self, tmp: str, output: str) -> str:
        path = Path(tmp) / "systemctl"
        path.write_text(f"#!/bin/sh\necho {output}\n")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return str(path)

    def test_it_reports_what_systemd_says(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dashboard.service_state("bombagif-selfbot", systemctl=self._fake_systemctl(tmp, "active")), "active")

    def test_a_missing_systemctl_is_unknown_not_a_crash(self) -> None:
        self.assertEqual(dashboard.service_state("bombagif-selfbot", systemctl="/nonexistent/systemctl"), "unknown")


class MainTests(unittest.TestCase):
    def test_a_pipe_gets_one_frame_instead_of_an_empty_screen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(_snapshot()))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = dashboard.main(["--status", str(path), "--systemctl", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("Clip scout", printed.getvalue())

    def test_once_prints_one_frame_and_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(_snapshot()))
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = dashboard.main(["--status", str(path), "--once", "--systemctl", "/nonexistent"])
        self.assertEqual(code, 0)
        self.assertIn("Bombagif", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
