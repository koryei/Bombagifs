import unittest

from scout_buffer import ScoutMessage, Version
from scout_trigger import Trigger, edit_distance, parse_trigger, pick_targets

CLIP = Trigger("clip")
GIF = Trigger("gif")


class ParseSureTests(unittest.TestCase):
    def test_plain_phrases_are_certain(self) -> None:
        for text in (
            "clip that", "Clip This", "clip it", "clip dat", "clip this one", "clip that one", "clip that msg", "clip that shi",
            "clip that shit", "clip that message", "CLIP THAT", "  clip   that  ", "clip that.", "clip that!!!", "clip that?",
            "clip thattt", "clippp that", "clip thaaat", "cl1p th4t",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_trigger(text), CLIP)

    def test_filler_laughter_and_emoji_are_ignored(self) -> None:
        for text in (
            "clip that lmao", "clip that LMFAOOO", "clip that lmfaii", "clip that LMFAII", "bro clip that",
            "yo bro clip that fr", "clip that rn", "clip that pls", "please clip that", "clip that 😭", "clip that 💀💀💀",
            "😭 clip that", "someone clip that", "can someone clip that", "can u clip that", "could you clip this please",
            "lol clip that", "clip that fr fr", "clip that haha", "clip that lolol", "bruh clip that", "omg clip that",
            "clip that <:kek:123456789012345678>", "clip that <a:wow:123456789012345678> lmao", "clip that rn rn rn", "ayo clip that",
            "bombaclip clip that", "clip that 😭😭 lmaooo", "clip this one rn pls",
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_trigger(text), CLIP)

    def test_object_first_forms(self) -> None:
        for text in ("that needs to be clipped", "that should be clipped", "this gotta be clipped lmao", "get that clipped", "that clipped"):
            with self.subTest(text=text):
                self.assertEqual(parse_trigger(text), CLIP)

    def test_counts(self) -> None:
        self.assertEqual(parse_trigger("clip last 3"), Trigger("clip", 3))
        self.assertEqual(parse_trigger("clip the last 3 messages"), Trigger("clip", 3))
        self.assertEqual(parse_trigger("clip last three"), Trigger("clip", 3))
        self.assertEqual(parse_trigger("clip the last two msgs lmao"), Trigger("clip", 2))
        self.assertEqual(parse_trigger("clip last few"), Trigger("clip", 3))
        self.assertEqual(parse_trigger("clip that convo"), Trigger("clip", 4))
        self.assertEqual(parse_trigger("clip this conversation"), Trigger("clip", 4))
        self.assertEqual(parse_trigger("clip last 1"), Trigger("clip", 1))
        self.assertEqual(parse_trigger("clip last 99"), Trigger("clip", 5))  # capped
        self.assertEqual(parse_trigger("clip last 0"), Trigger("clip", 1))

    def test_a_bare_verb_needs_a_reply(self) -> None:
        self.assertEqual(parse_trigger("clip", is_reply=True), CLIP)
        self.assertEqual(parse_trigger("clip lmao", is_reply=True), CLIP)
        self.assertEqual(parse_trigger("clipped", is_reply=True), CLIP)
        bare = parse_trigger("clip")
        self.assertEqual(bare, Trigger("clip", 1, sure=False))  # unsure without a reply: the AI decides

    def test_gif_phrases(self) -> None:
        for text in ("gif that", "gif this", "gif it", "GIF THAT lmao", "make that a gif", "make this a gif pls", "turn it into a gif",
                     "can u gif that", "gif dat"):
            with self.subTest(text=text):
                self.assertEqual(parse_trigger(text), GIF)
        self.assertEqual(parse_trigger("gif", is_reply=True), GIF)
        self.assertIsNone(parse_trigger("gif"))


class ParseNotATriggerTests(unittest.TestCase):
    def test_ordinary_chat_is_never_a_trigger(self) -> None:
        for text in (
            "", "   ", "hello", "lmao", "that was so funny", "i love this", "that", "it", "what time is it",
            "I clipped my nails yesterday and it hurt a lot honestly", "nice gif", "send the gif", "that gif is so good lol",
            "does anyone know how to gif a video in discord, i need it for tomorrow",
            "😭😭😭", "https://example.com/clip that", "x" * 500,
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_trigger(text))

    def test_directed_at_someone_else_is_not_ours(self) -> None:
        for text in ("<@123456789012345678> clip that", "<@!123456789012345678> clip that", "@everyone clip that", "@here clip that"):
            with self.subTest(text=text):
                self.assertIsNone(parse_trigger(text))

    def test_a_noun_use_of_clip_is_unsure_not_certain(self) -> None:
        for text in ("this clip", "that clip is fire", "clip that video", "send that clip", "i clip it later", "clip that for the gc"):
            with self.subTest(text=text):
                result = parse_trigger(text)
                self.assertTrue(result is None or not result.sure, result)


class ParseTyposTests(unittest.TestCase):
    def test_typos_are_unsure_so_the_ai_decides(self) -> None:
        for text in ("clkip that LMFAII", "cilp that", "clpi that", "clip taht", "clp that", "clipp that", "clkip thattt", "clip thta lmao"):
            with self.subTest(text=text):
                self.assertEqual(parse_trigger(text), Trigger("clip", 1, sure=False))

    def test_look_alike_words_may_be_sent_to_the_ai_but_never_post_alone(self) -> None:
        for text in ("flip that", "chip it", "slip that", "clap that"):
            with self.subTest(text=text):
                result = parse_trigger(text)
                self.assertTrue(result is None or not result.sure)

    def test_long_messages_are_not_considered(self) -> None:
        self.assertIsNone(parse_trigger("clip that " + "word " * 30))


class EditDistanceTests(unittest.TestCase):
    def test_distance(self) -> None:
        self.assertEqual(edit_distance("clip", "clip"), 0)
        self.assertEqual(edit_distance("clkip", "clip"), 1)
        self.assertEqual(edit_distance("cilp", "clip"), 1)  # a transposition counts once
        self.assertEqual(edit_distance("", "clip"), 4)
        self.assertEqual(edit_distance("flip", "clip"), 1)
        self.assertEqual(edit_distance("clap", "clip"), 1)
        self.assertEqual(edit_distance("clock", "clip"), 3)


def msg(i: int, author: int = 1, at: float = 1000.0, eligible: bool = True, text: str = "hi") -> ScoutMessage:
    return ScoutMessage(i, 50, author, f"u{author}", "", None, None, at, [Version(text, at)], eligible=eligible)


class PickTargetsTests(unittest.TestCase):
    def test_picks_the_last_n(self) -> None:
        chat = [msg(1), msg(2, 2), msg(3, 1), msg(4, 2)]
        self.assertEqual([m.id for m in pick_targets(chat, 1)], [4])
        self.assertEqual([m.id for m in pick_targets(chat, 3)], [2, 3, 4])
        self.assertEqual([m.id for m in pick_targets(chat, 99)], [1, 2, 3, 4])

    def test_no_more_than_three_authors(self) -> None:
        chat = [msg(1, 1), msg(2, 2), msg(3, 3), msg(4, 4), msg(5, 5)]
        self.assertEqual([m.id for m in pick_targets(chat, 5)], [3, 4, 5])

    def test_empty(self) -> None:
        self.assertEqual(pick_targets([], 3), [])
        self.assertEqual(pick_targets([msg(1)], 0), [])


if __name__ == "__main__":
    unittest.main()
