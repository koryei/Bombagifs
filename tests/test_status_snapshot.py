"""The snapshot the bot hands the dashboard: complete, atomic, and free of secrets."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from selfbot_logic import ActivityEntry, BotStats, StatusSnapshot, counters_from, write_status


def _snapshot(**overrides: object) -> StatusSnapshot:
    fields: dict[str, object] = {
        "at": 1795236737.5,
        "state": "online",
        "account": "bombaclat",
        "zipline": "zip.example",
        "uptime_s": 18342,
        "allowlist": 4,
        "scout": {"mode": "dry-run", "judge": "inception", "can_post": True, "chats": 2, "reviews_waiting": 6, "manual": True},
        "counters": {"judged": 41, "posted": 0},
        "activity": (ActivityEntry(1795236737.0, "warn", "clip rejected: author on cooldown"),),
    }
    fields.update(overrides)
    return StatusSnapshot(**fields)  # type: ignore[arg-type]


class CountersTests(unittest.TestCase):
    def test_counters_are_named_the_way_the_screen_labels_them(self) -> None:
        stats = BotStats()
        stats.conversions_ok = 12
        stats.scout_screened = 41
        stats.clips_posted = 3
        counters = counters_from(stats)
        self.assertEqual((counters["converted"], counters["judged"], counters["posted"]), (12, 41, 3))
        self.assertEqual(set(counters), {
            "converted", "failed", "triggers", "rate_limited", "new_users", "friends",
            "judged", "passed", "posted", "rejected", "dry_run_hits", "deleted_rescued",
        })


class SnapshotTests(unittest.TestCase):
    def test_to_dict_is_json_ready(self) -> None:
        payload = _snapshot().to_dict()
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(payload["scout"]["mode"], "dry-run")
        self.assertEqual(payload["activity"][0]["level"], "warn")

    def test_the_snapshot_carries_no_secrets(self) -> None:
        text = json.dumps(_snapshot().to_dict())
        for forbidden in ("token", "secret", "password", "zipline_token"):
            self.assertNotIn(forbidden, text.lower())

    def test_write_status_is_atomic_and_owner_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            write_status(path, _snapshot())
            self.assertEqual(json.loads(path.read_text())["state"], "online")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["status.json"])  # no temp file left behind


if __name__ == "__main__":
    unittest.main()
