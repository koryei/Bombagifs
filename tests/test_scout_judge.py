"""Scout judge: anonymised windows, verdict validation, provider clients, and the fallback chain."""

from __future__ import annotations

import asyncio
import json
import math
import unittest

import aiohttp
from aiohttp import web

from scout_buffer import ScoutMessage, Version
from scout_judge import (
    DECIDE_QUESTIONS,
    SENSITIVE_FLAGS,
    DecideApi,
    HeuristicJudge,
    JudgeWindow,
    ProviderError,
    ProviderUnavailable,
    build_judge_window,
    decide_stage,
    validate_verdict,
    window_payload,
)

NAMES = {10: "Pranit", 20: "Koryei", 30: "Shan", 40: "Lochan"}


def msg(id: int, text: str, *, author: int = 10, eligible: bool = True, deleted: bool = False, edits: tuple[str, ...] = ()) -> ScoutMessage:
    versions = [Version(text, 1000.0 + id)] + [Version(e, 1100.0 + id) for e in edits]
    return ScoutMessage(
        id=id,
        chat_id=1,
        author_id=author,
        author_name=NAMES[author],
        avatar_url=f"https://cdn.discordapp.com/avatars/{author}/hash{author}.png",
        color=None,
        tag=None,
        created_at=1000.0 + id,
        versions=versions,
        eligible=eligible,
        deleted_at=1200.0 if deleted else None,
    )


def window_of(messages: list[ScoutMessage], run: list[ScoutMessage] | None = None) -> JudgeWindow:
    return build_judge_window(messages, run or messages[:1])


def good_raw(**overrides: object) -> dict[str, object]:
    raw: dict[str, object] = {
        "clip": True,
        "start": 0,
        "end": 1,
        "funny": 8.5,
        "standalone": True,
        "sensitive": [],
        "reason": "setup and punchline",
    }
    raw.update(overrides)
    return raw


class JudgeWindowTests(unittest.TestCase):
    def test_speakers_are_labelled_by_first_appearance(self) -> None:
        messages = [msg(1, "hi", author=20), msg(2, "yo", author=10), msg(3, "again", author=20), msg(4, "hey", author=30)]
        window = window_of(messages)
        self.assertEqual([line.speaker for line in window.lines], ["A", "B", "A", "C"])

    def test_payload_has_no_names_ids_or_avatars(self) -> None:
        messages = [msg(1, "hello world", author=10), msg(2, "second", author=20)]
        payload = json.dumps(window_payload(window_of(messages)))
        for needle in ("Pranit", "Koryei", "avatars", "cdn.discordapp", "hash10", "hash20", '"id"'):
            self.assertNotIn(needle, payload)
        self.assertEqual(
            window_payload(window_of(messages)),
            [
                {"i": 0, "speaker": "A", "text": "hello world", "deleted": False},
                {"i": 1, "speaker": "B", "text": "second", "deleted": False},
            ],
        )

    def test_payload_uses_original_text_not_edits(self) -> None:
        window = window_of([msg(1, "the real joke", edits=("nothing to see",))])
        self.assertEqual(window.lines[0].text, "the real joke")
        self.assertNotIn("nothing to see", json.dumps(window_payload(window)))

    def test_ineligible_lines_become_not_text_placeholders(self) -> None:
        messages = [msg(1, "", author=20, eligible=False), msg(2, "text here", author=10)]
        window = window_of(messages, messages[1:])
        self.assertEqual((window.lines[0].speaker, window.lines[0].text), ("-", "[not text]"))
        self.assertEqual(window.lines[1].speaker, "A")  # placeholders do not consume a speaker label

    def test_deleted_flag_is_carried(self) -> None:
        window = window_of([msg(1, "oops", deleted=True), msg(2, "fine")])
        self.assertEqual([line.deleted for line in window.lines], [True, False])

    def test_run_indexes_and_messages_are_kept(self) -> None:
        messages = [msg(1, "before", author=20), msg(2, "run a", author=10), msg(3, "run b", author=10), msg(4, "after", author=20)]
        window = build_judge_window(messages, messages[1:3])
        self.assertEqual((window.run_start, window.run_end), (1, 2))
        self.assertEqual([m.id for m in window.messages], [1, 2, 3, 4])


class ValidateVerdictTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window = window_of(
            [msg(1, "setup", author=10), msg(2, "punchline", author=20), msg(3, "more", author=30), msg(4, "x", author=10)]
        )

    def validate(self, raw: object) -> object:
        return validate_verdict(raw, self.window, provider="test")

    def test_accepts_a_good_verdict(self) -> None:
        verdict = self.validate(good_raw())
        self.assertIsNotNone(verdict)
        self.assertEqual((verdict.clip, verdict.start, verdict.end, verdict.funny, verdict.standalone), (True, 0, 1, 8.5, True))
        self.assertEqual((verdict.sensitive, verdict.reason, verdict.provider), ((), "setup and punchline", "test"))

    def test_integer_funny_and_known_flags_are_fine(self) -> None:
        verdict = self.validate(good_raw(funny=9, clip=False, sensitive=["health"]))
        self.assertEqual((verdict.funny, verdict.sensitive), (9.0, ("health",)))

    def test_rejects_malformed_verdicts(self) -> None:
        bad = {
            "not a dict": "clip it",
            "list": [True],
            "missing clip": {k: v for k, v in good_raw().items() if k != "clip"},
            "missing reason": {k: v for k, v in good_raw().items() if k != "reason"},
            "clip as int": good_raw(clip=1),
            "standalone as string": good_raw(standalone="yes"),
            "start as float": good_raw(start=0.0),
            "start as bool": good_raw(start=True),
            "end before start": good_raw(start=2, end=1),
            "end past the window": good_raw(start=0, end=4),
            "negative start": good_raw(start=-1, end=1),
            "funny too high": good_raw(funny=11),
            "funny negative": good_raw(funny=-1),
            "funny nan": good_raw(funny=math.nan),
            "funny inf": good_raw(funny=math.inf),
            "funny as string": good_raw(funny="8"),
            "unknown flag": good_raw(sensitive=["spicy"]),
            "flags not a list": good_raw(sensitive="slur"),
            "flag not a string": good_raw(sensitive=[1]),
            "reason not a string": good_raw(reason=7),
        }
        for name, raw in bad.items():
            with self.subTest(name=name):
                self.assertIsNone(self.validate(raw))

    def test_clip_false_does_not_need_a_valid_range(self) -> None:
        verdict = self.validate(good_raw(clip=False, start=-5, end=99))
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.clip)

    def test_nine_messages_is_too_many(self) -> None:
        messages = [msg(i, f"line {i}", author=10) for i in range(1, 11)]
        window = window_of(messages)
        self.assertIsNone(validate_verdict(good_raw(start=0, end=8), window, provider="t"))  # 9 lines
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=7), window, provider="t"))  # 8 lines

    def test_more_than_600_characters_is_too_long(self) -> None:
        window = window_of([msg(1, "x" * 300, author=10), msg(2, "y" * 301, author=10)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=1), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=0), window, provider="t"))

    def test_four_authors_is_too_many(self) -> None:
        window = window_of([msg(1, "a", author=10), msg(2, "b", author=20), msg(3, "c", author=30), msg(4, "d", author=40)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=3), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=0, end=2), window, provider="t"))

    def test_a_not_text_line_inside_the_range_is_rejected(self) -> None:
        window = window_of([msg(1, "a", author=10), msg(2, "", author=20, eligible=False), msg(3, "c", author=10)])
        self.assertIsNone(validate_verdict(good_raw(start=0, end=2), window, provider="t"))
        self.assertIsNotNone(validate_verdict(good_raw(start=2, end=2), window, provider="t"))

    def test_reason_is_truncated_to_160_chars(self) -> None:
        verdict = self.validate(good_raw(reason="r" * 500))
        self.assertEqual(len(verdict.reason), 160)

    def test_decide_scores_are_attached(self) -> None:
        verdict = validate_verdict(good_raw(), self.window, provider="p", decide={"funny": 0.9})
        self.assertEqual(dict(verdict.decide), {"funny": 0.9})

    def test_sensitive_flags_constant_is_complete(self) -> None:
        self.assertEqual(
            set(SENSITIVE_FLAGS),
            {"personal_info", "credentials", "self_harm", "sexual", "slur", "minor", "health", "tragedy", "other"},
        )


class HeuristicJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_heuristic_judge_never_clips_and_cannot_post(self) -> None:
        judge = HeuristicJudge()
        verdict = await judge.judge(window_of([msg(1, "anything at all")]))
        self.assertFalse(verdict.clip)
        self.assertFalse(judge.can_post)
        self.assertEqual((judge.name, verdict.provider, verdict.reason), ("heuristic", "heuristic", "no LLM judge"))


API_KEY = "inception-secret-key-123"


class MockServer:
    """A loopback aiohttp server whose handler a test supplies; records every request."""

    def __init__(self, handler) -> None:
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def start(self, path: str) -> "MockServer":
        async def wrapped(request: web.Request) -> web.StreamResponse:
            body = await request.read()
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
            self.requests.append({"path": request.path, "headers": dict(request.headers), "json": parsed})
            return await self.handler(request, parsed, len(self.requests))

        app = web.Application()
        app.router.add_post(path, wrapped)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"  # type: ignore[union-attr]
        return self

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()


def decide_answers(**overrides: object) -> dict[str, object]:
    answers: dict[str, object] = {
        "funny": {"type": "noul", "noul": 0.93},
        "standalone": {"type": "noul", "noul": 0.88},
        "sensitive": {"type": "noul", "noul": 0.02},
        "quality": {"type": "score", "score": 3.4, "legend": {}, "probabilities": {}, "confidence": 0.7},
    }
    answers.update(overrides)
    return answers


class DecideApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.session = aiohttp.ClientSession()
        self.addAsyncCleanup(self.session.close)
        self.window = window_of(
            [msg(1, "setup line", author=10), msg(2, "the punchline", author=20, deleted=True)],
            [msg(2, "the punchline", author=20)],
        )

    async def serve(self, handler) -> MockServer:
        server = await MockServer(handler).start("/v1/decisions")
        self.addAsyncCleanup(server.stop)
        return server

    def api(self, server: MockServer, **kwargs: object) -> DecideApi:
        options = {"backoff": 0.0, "timeout": 2.0, "retries": 2}
        options.update(kwargs)
        return DecideApi(self.session, API_KEY, base_url=server.url, **options)  # type: ignore[arg-type]

    async def ok(self, request: web.Request, body: object, n: int) -> web.Response:
        return web.json_response({"model": "mercury-decide", "answers": decide_answers(), "usage": {"input_tokens": 100, "output_tokens": 3}})

    async def test_request_shape(self) -> None:
        server = await self.serve(self.ok)
        await decide_stage(self.api(server), self.window)
        request = server.requests[0]
        self.assertEqual(request["path"], "/v1/decisions")
        self.assertEqual(request["headers"]["Authorization"], f"Bearer {API_KEY}")
        body = request["json"]
        self.assertEqual(body["model"], "mercury-decide")
        self.assertEqual(set(body["questions"]), {"funny", "standalone", "sensitive", "quality"})
        self.assertEqual([body["questions"][k]["type"] for k in ("funny", "standalone", "sensitive")], ["noul"] * 3)
        self.assertEqual(body["questions"]["quality"]["type"], "score")
        self.assertEqual(len(body["questions"]["quality"]["criteria"]), 5)
        self.assertEqual(body["state"]["window"], window_payload(self.window))
        self.assertEqual(body["state"]["candidate"], {"start": self.window.run_start, "end": self.window.run_end})

    async def test_parses_answers(self) -> None:
        server = await self.serve(self.ok)
        scores = await decide_stage(self.api(server), self.window)
        self.assertEqual(scores, {"funny": 0.93, "standalone": 0.88, "sensitive": 0.02, "quality": 3.4})

    async def test_state_contains_no_names_or_ids(self) -> None:
        server = await self.serve(self.ok)
        await decide_stage(self.api(server), self.window)
        sent = json.dumps(server.requests[0]["json"])
        for needle in ("Pranit", "Koryei", "avatars", "hash10", "hash20", API_KEY):
            self.assertNotIn(needle, sent)

    async def test_questions_treat_text_as_data(self) -> None:
        for name, question in DECIDE_QUESTIONS.items():
            with self.subTest(name=name):
                self.assertIn("data", str(question["instructions"]).lower())

    async def test_gone_or_refused_statuses_raise_provider_unavailable(self) -> None:
        for status in (401, 402, 404, 410):
            async def handler(request: web.Request, body: object, n: int, status: int = status) -> web.Response:
                return web.json_response({"error": "nope"}, status=status)

            server = await self.serve(handler)
            with self.subTest(status=status):
                with self.assertRaises(ProviderUnavailable):
                    await decide_stage(self.api(server), self.window)
                self.assertEqual(len(server.requests), 1)  # no retries for these

    async def test_503_then_ok_retries(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            return web.Response(status=503) if n == 1 else await self.ok(request, body, n)

        server = await self.serve(handler)
        scores = await decide_stage(self.api(server), self.window)
        self.assertEqual(scores["funny"], 0.93)
        self.assertEqual(len(server.requests), 2)

    async def test_429_honours_retry_after_zero(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            return web.Response(status=429, headers={"Retry-After": "0"}) if n == 1 else await self.ok(request, body, n)

        server = await self.serve(handler)
        await decide_stage(self.api(server, backoff=5.0), self.window)  # would take 5 s if Retry-After were ignored
        self.assertEqual(len(server.requests), 2)

    async def test_exhausted_retries_raise_provider_error_and_log_without_the_key(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            return web.Response(status=500)

        server = await self.serve(handler)
        with self.assertLogs("bombagif.scout", "WARNING") as logs:
            with self.assertRaises(ProviderError):
                await decide_stage(self.api(server), self.window)
        self.assertEqual(len(server.requests), 3)  # 1 + 2 retries
        self.assertNotIn(API_KEY, "\n".join(logs.output))

    async def test_other_client_errors_are_not_retried(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            return web.json_response({"error": "bad"}, status=422)

        server = await self.serve(handler)
        with self.assertRaises(ProviderError):
            await decide_stage(self.api(server), self.window)
        self.assertEqual(len(server.requests), 1)

    async def test_timeout_raises_provider_error(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            await asyncio.sleep(0.5)
            return web.Response(status=200)

        server = await self.serve(handler)
        with self.assertRaises(ProviderError):
            await decide_stage(self.api(server, timeout=0.05, retries=0), self.window)

    async def test_missing_or_ill_typed_answers_raise_provider_error(self) -> None:
        cases = {
            "missing question": {k: v for k, v in decide_answers().items() if k != "funny"},
            "noul out of range": decide_answers(funny={"type": "noul", "noul": 1.5}),
            "noul wrong type": decide_answers(funny={"type": "noul", "noul": "high"}),
            "score missing": decide_answers(quality={"type": "score"}),
            "answers not an object": ["x"],
        }
        for name, answers in cases.items():
            async def handler(request: web.Request, body: object, n: int, answers: object = answers) -> web.Response:
                return web.json_response({"answers": answers})

            server = await self.serve(handler)
            with self.subTest(name=name):
                with self.assertRaises(ProviderError):
                    await decide_stage(self.api(server), self.window)

    async def test_non_json_body_raises_provider_error(self) -> None:
        async def handler(request: web.Request, body: object, n: int) -> web.Response:
            return web.Response(text="<html>gateway</html>", content_type="text/html")

        server = await self.serve(handler)
        with self.assertRaises(ProviderError):
            await decide_stage(self.api(server), self.window)


if __name__ == "__main__":
    unittest.main()
