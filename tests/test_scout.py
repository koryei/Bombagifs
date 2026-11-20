"""The scout orchestrator: scheduling, gates, cooldowns, modes, ledger, and helper text."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from scout import Scout
from scout_buffer import ScoutBuffer, ScoutMessage, Version
from scout_config import read_scout_config
from scout_judge import HeuristicJudge, JudgeChain, JudgeWindow, Verdict
from scout_stores import ClipLedger, OptOutStore
from selfbot_logic import BotStats

T0 = 1_000_000.0


class FakeClock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def good_verdict(**overrides: object) -> Verdict:
    fields: dict[str, object] = dict(
        clip=True,
        start=0,
        end=0,
        funny=9.0,
        standalone=True,
        sensitive=(),
        reason="funny",
        decide={"funny": 0.95, "standalone": 0.9, "sensitive": 0.01},
        provider="fake",
    )
    fields.update(overrides)
    return Verdict(**fields)  # type: ignore[arg-type]


def no_verdict() -> Verdict:
    return good_verdict(clip=False)


class FakeJudge:
    """Scripted judge: ``responder(window)`` returns the verdict (or None) for each call."""

    name = "fake"
    can_post = True

    def __init__(self, responder) -> None:
        self.responder = responder
        self.windows: list[JudgeWindow] = []

    @property
    def calls(self) -> int:
        return len(self.windows)

    async def judge(self, window: JudgeWindow) -> Verdict | None:
        self.windows.append(window)
        return self.responder(window)


def msg(id: int, text: str = "some funny words", *, chat: int = 1, author: int = 10, at: float = T0, eligible: bool = True) -> ScoutMessage:
    return ScoutMessage(
        id=id,
        chat_id=chat,
        author_id=author,
        author_name=f"user{author}",
        avatar_url="https://cdn.discordapp.com/avatars/1/a.png",
        color=None,
        tag=None,
        created_at=at,
        versions=[Version(text, at)],
        eligible=eligible,
    )


class ScoutCase(unittest.IsolatedAsyncioTestCase):
    """Builds a Scout with a fake clock, a real buffer, real stores in a temp dir, and a recording on_clip."""

    env: dict[str, str] = {}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()
        self.cfg = read_scout_config({"SELFBOT_SCOUT_MODE": "on", **self.env})
        self.optout = OptOutStore(self.dir / "optout.json")
        self.buffer = ScoutBuffer(max_age_s=3600, max_per_chat=200, is_opted_out=lambda uid: uid in self.optout, clock=self.clock)
        self.ledger = ClipLedger(self.dir / "clips.jsonl")
        self.stats = BotStats(started_at=T0)
        self.posted: list = []
        self.post_result: int | None = 9001
        self.paused = False

    async def on_clip(self, plan) -> int | None:
        self.posted.append(plan)
        return self.post_result

    def scout(self, judge, *, cfg=None, blocklist=frozenset()) -> Scout:
        chain = judge if isinstance(judge, JudgeChain) else JudgeChain([judge, HeuristicJudge()])
        return Scout(
            cfg or self.cfg,
            self.buffer,
            chain,
            self.optout,
            self.ledger,
            self.on_clip,
            stats=self.stats,
            paused=lambda: self.paused,
            blocklist=blocklist,
            clock=self.clock,
        )

    def add(self, id: int, text: str = "some funny words", **kwargs) -> ScoutMessage:
        kwargs.setdefault("at", self.clock.now)
        message = msg(id, text, **kwargs)
        self.assertTrue(self.buffer.add(message))
        return message

    def ledger_rows(self) -> list[dict]:
        path = self.dir / "clips.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class SelectionTests(ScoutCase):
    async def test_run_waits_for_the_reaction_window(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1)
        self.clock.now = T0 + 44
        await scout.tick()
        self.assertEqual(judge.calls, 0)
        self.clock.now = T0 + 45
        await scout.tick()
        self.assertEqual(judge.calls, 1)

    async def test_deleted_run_is_screened_immediately(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1)
        self.buffer.delete(1, 1)
        self.clock.now = T0 + 1
        await scout.tick()
        self.assertEqual(judge.calls, 1)
        self.assertTrue(judge.windows[0].lines[0].deleted)

    async def test_screen_signals_skips_runs_without_laughter(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge, cfg=replace(self.cfg, screen="signals"))
        quiet = self.add(1)
        self.add(2, author=20, chat=2)
        self.buffer.react(2, 2, "😂", True)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 1)  # only the chat-2 run, which has a laugh reaction
        self.assertFalse(quiet.screened)

    async def test_laugh_replies_count_as_a_signal(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge, cfg=replace(self.cfg, screen="signals"))
        self.add(1, "the joke", author=10)
        self.add(2, "lmao", author=20, at=T0 + 5)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 1)

    async def test_screened_runs_are_not_screened_twice_but_a_grown_run_is(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        first = self.add(1)
        self.clock.now = T0 + 60
        await scout.tick()
        await scout.tick()
        self.assertEqual(judge.calls, 1)
        self.assertTrue(first.screened)
        self.add(2, "and then another line", at=self.clock.now)  # joins the same run
        await scout.tick()
        self.assertEqual(judge.calls, 1)  # the grown run must settle first
        self.clock.now += 46
        await scout.tick()
        self.assertEqual(judge.calls, 2)
        self.assertEqual(len(judge.windows[1].lines), 2)

    async def test_none_verdict_retries_after_30_seconds_and_does_not_mark_screened(self) -> None:
        answers = [None, no_verdict()]
        judge = FakeJudge(lambda w: answers.pop(0))
        scout = self.scout(judge)
        message = self.add(1)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 1)
        self.assertFalse(message.screened)
        self.clock.now += 29
        await scout.tick()
        self.assertEqual(judge.calls, 1)
        self.clock.now += 2
        await scout.tick()
        self.assertEqual(judge.calls, 2)
        self.assertTrue(message.screened)

    async def test_ineligible_messages_are_never_screened(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1, "", eligible=False)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 0)

    async def test_judge_window_has_context_around_the_run(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1, "before", author=20)
        self.add(2, "the run", author=10, at=T0 + 1)
        self.add(3, "after", author=30, at=T0 + 2)
        self.clock.now = T0 + 60
        await scout.tick()
        # three single-message runs, each screened with its neighbours as context
        self.assertEqual(judge.calls, 3)
        middle = next(w for w in judge.windows if w.lines[w.run_start].text == "the run")
        self.assertEqual([line.text for line in middle.lines], ["before", "the run", "after"])

    async def test_runs_with_an_already_clipped_message_are_skipped(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        message = self.add(1)
        message.clipped = True
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 0)

    async def test_paused_scout_does_nothing(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1)
        self.clock.now = T0 + 60
        self.paused = True
        await scout.tick()
        self.assertEqual(judge.calls, 0)

    async def test_mode_off_does_nothing(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge, cfg=replace(self.cfg, mode="off"))
        self.add(1)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(judge.calls, 0)

    async def test_screening_counts_are_recorded(self) -> None:
        judge = FakeJudge(lambda w: no_verdict())
        scout = self.scout(judge)
        self.add(1)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual((self.stats.scout_screened, self.stats.scout_passed), (1, 0))

    async def test_provider_events_reach_the_feed(self) -> None:
        from scout_judge import ProviderUnavailable

        class Gone:
            name = "inception"
            can_post = True

            async def judge(self, window):
                raise ProviderUnavailable("shut down")

        chain = JudgeChain([Gone(), HeuristicJudge()])  # type: ignore[list-item]
        scout = self.scout(chain)
        self.add(1)
        self.clock.now = T0 + 60
        with self.assertLogs("bombagif.scout", "WARNING"):
            await scout.tick()
        self.assertTrue(any("inception judge disabled" in e.text for e in self.stats.activity))

    async def test_heuristic_chain_never_calls_on_clip_and_records_signal_runs(self) -> None:
        scout = self.scout(JudgeChain([HeuristicJudge()]))
        self.add(1, "the joke", author=10)
        self.add(2, "lmao", author=20, at=T0 + 5)
        self.add(3, "quiet line", author=30, chat=2)
        self.clock.now = T0 + 60
        await scout.tick()
        self.assertEqual(self.posted, [])
        rows = self.ledger_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["mode"], rows[0]["reason"], rows[0]["chat_id"], rows[0]["message_ids"]), ("dry-run", "no LLM judge", 1, [1]))
        self.assertIsNone(rows[0]["text"])
        self.assertEqual(self.stats.dry_run_hits, 1)


if __name__ == "__main__":
    unittest.main()
