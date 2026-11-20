"""Eligibility rules and the local sensitive-content filter (the judge can never override these)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scout_safety import check_text, check_window, is_eligible_text, load_blocklist


class EligibilityTests(unittest.TestCase):
    def test_length_bounds(self) -> None:
        self.assertFalse(is_eligible_text("abc"))
        self.assertTrue(is_eligible_text("abcd"))
        self.assertTrue(is_eligible_text("x" * 400))
        self.assertFalse(is_eligible_text("x" * 401))
        self.assertFalse(is_eligible_text("   ab   "))  # stripped before measuring
        self.assertTrue(is_eligible_text("  abcd  "))

    def test_urls_make_text_ineligible(self) -> None:
        for text in ("look https://example.com now", "http://x.y is cool", "go to www.example.com please"):
            with self.subTest(text=text):
                self.assertFalse(is_eligible_text(text))

    def test_spoilers_ineligible(self) -> None:
        self.assertFalse(is_eligible_text("the butler did ||it|| tho"))

    def test_command_like_text_ineligible(self) -> None:
        for text in ("!scout on", "/gif cats", ".help me", "!stop now"):
            with self.subTest(text=text):
                self.assertFalse(is_eligible_text(text))

    def test_ellipsis_and_punctuation_are_not_commands(self) -> None:
        for text in ("...wait what", "!!! no way", "?? huh", "-- okay then"):
            with self.subTest(text=text):
                self.assertTrue(is_eligible_text(text))

    def test_plain_text_is_eligible(self) -> None:
        self.assertTrue(is_eligible_text("the better one was me"))


class SensitiveFilterTests(unittest.TestCase):
    def assertBlocked(self, text: str, reason: str | None = None) -> None:
        result = check_text(text)
        self.assertFalse(result.ok, f"expected {text!r} to be blocked")
        if reason:
            self.assertIn(reason, result.reasons)

    def assertAllowed(self, text: str) -> None:
        result = check_text(text)
        self.assertTrue(result.ok, f"expected {text!r} to be allowed, got {result.reasons}")

    def test_email(self) -> None:
        self.assertBlocked("mail me at someone@example.com ok", "email")
        self.assertAllowed("meet me @ noon")

    def test_phone_numbers(self) -> None:
        for text in ("+1 (415) 555-0132", "0412 345 678", "call 415-555-0132 now", "4155550132"):
            with self.subTest(text=text):
                self.assertBlocked(text, "phone")
        for text in ("call me at 5", "room 12 floor 3", "i have 1234 problems", "it was 2024-05-01"):
            with self.subTest(text=text):
                self.assertAllowed(text)

    def test_ip_addresses(self) -> None:
        self.assertBlocked("server is 192.168.1.20 lol", "ip")
        self.assertBlocked("fe80::1ff:fe23:4567:890a", "ip")
        self.assertAllowed("version 1.2 is out")
        self.assertAllowed("score was 3:2:1")

    def test_long_non_card_numbers_like_discord_ids_are_allowed(self) -> None:
        self.assertAllowed("user 1234567890123456789 said hi")  # 19 digits, fails Luhn

    def test_card_numbers_need_to_pass_luhn(self) -> None:
        self.assertBlocked("4111 1111 1111 1111", "card")
        self.assertBlocked("4111-1111-1111-1111", "card")
        self.assertBlocked("4111111111111111", "card")
        self.assertAllowed("4111 1111 1111 1112")

    def test_long_random_strings(self) -> None:
        self.assertBlocked("a3f9c2d8e1b74a6f9c2d8e1b74a6f9c2d8e1", "token")  # 36 hex chars
        self.assertBlocked("QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo0MTIzNDU2", "token")
        self.assertAllowed("supercalifragilisticexpialidocious is a word")

    def test_key_shaped_tokens(self) -> None:
        for text in (
            "sk-abcdefghijklmnop12345678",
            "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "xoxb-123456-abcdef",
            "AIzaSyD-abcdefghijklmnopqrstuv",
            "Authorization: Bearer abcdef123456",
        ):
            with self.subTest(text=text):
                self.assertBlocked(text, "token")
        self.assertAllowed("the sky is blue")

    def test_discord_invites(self) -> None:
        self.assertBlocked("join discord.gg/abcdef", "invite")
        self.assertBlocked("https://discord.com/invite/xyz", "invite")
        self.assertBlocked("discordapp.com/invite/xyz", "invite")
        self.assertAllowed("discord is down again")

    def test_street_addresses(self) -> None:
        self.assertBlocked("i live at 221 Baker Street", "address")
        self.assertBlocked("it is 1600 Pennsylvania Avenue", "address")
        self.assertAllowed("i have 5 streets to cover")
        self.assertAllowed("3 way trade")
        self.assertAllowed("i have 2 dr appointments")

    def test_mass_mentions(self) -> None:
        self.assertBlocked("hey @everyone wake up", "mention")
        self.assertBlocked("@here look", "mention")
        self.assertAllowed("everyone here is cool")

    def test_blocklist_uses_word_boundaries_and_ignores_case(self) -> None:
        blocklist = frozenset({"ass", "badword"})
        self.assertFalse(check_text("you BADWORD", blocklist).ok)
        self.assertIn("blocklist", check_text("such an ass", blocklist).reasons)
        self.assertTrue(check_text("my class is boring", blocklist).ok)
        self.assertTrue(check_text("passing grade", blocklist).ok)

    def test_check_window_merges_reasons_and_requires_every_text_ok(self) -> None:
        clean = check_window(["hello there", "nice weather"])
        self.assertTrue(clean.ok)
        self.assertEqual(clean.reasons, ())
        dirty = check_window(["hello there", "mail a@b.co", "call 415-555-0132"])
        self.assertFalse(dirty.ok)
        self.assertEqual(set(dirty.reasons), {"email", "phone"})

    def test_a_secret_split_across_messages_is_still_caught(self) -> None:
        self.assertFalse(check_window(["my number is 415 555", "0132 call me"]).ok)

    def test_empty_window_is_ok(self) -> None:
        self.assertTrue(check_window([]).ok)


class BlocklistFileTests(unittest.TestCase):
    def test_load_blocklist_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blocklist.txt"
            path.write_text("# comment\n\nBadWord\n  spaced term  \nanother # trailing\n", encoding="utf-8")
            self.assertEqual(load_blocklist(path), frozenset({"badword", "spaced term", "another"}))

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(load_blocklist(Path("/nonexistent/blocklist.txt")), frozenset())


if __name__ == "__main__":
    unittest.main()
