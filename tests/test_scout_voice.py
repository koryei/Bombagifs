"""The voice-clip core: the rolling buffer, the request parser, and the Ogg encoder."""

from __future__ import annotations

import shutil
import struct
import subprocess
import unittest
from unittest.mock import patch

from scout_voice import (
    DEFAULT_CLIP_S,
    MAX_CLIP_S,
    SAMPLE_RATE,
    VoiceBuffer,
    VoiceRequest,
    encode_ogg,
    format_voice_notice,
    parse_voice_request,
    resolve_target,
)

MEMBERS = [(7, "Pranit"), (8, "Koryei"), (9, "Pranit Two")]


def pcm(seconds: float, value: int = 1000, *, channels: int = 2) -> bytes:
    """Constant-value s16le PCM, so a clip can be checked sample by sample."""
    return struct.pack("<h", value) * int(seconds * SAMPLE_RATE) * channels


def samples(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 2}h", data))


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class VoiceBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.buffer = VoiceBuffer(keep_s=45.0, clock=self.clock)

    def test_a_clip_is_exactly_the_requested_length(self) -> None:
        self.buffer.add(7, pcm(1.0))
        self.clock.now += 1.0
        clip = self.buffer.clip(seconds=1.0, user_id=7)
        self.assertEqual(len(clip), SAMPLE_RATE * 2)
        self.assertEqual(set(samples(clip)), {1000})

    def test_a_short_buffer_is_padded_with_silence_at_the_front(self) -> None:
        self.buffer.add(7, pcm(0.5, 2000))
        self.clock.now += 0.5
        clip = self.buffer.clip(seconds=1.0, user_id=7)
        self.assertEqual(len(clip), SAMPLE_RATE * 2)
        self.assertEqual(samples(clip)[: SAMPLE_RATE // 2], [0] * (SAMPLE_RATE // 2))  # the missing half
        self.assertEqual(set(samples(clip)[SAMPLE_RATE // 2 :]), {2000})

    def test_a_long_buffer_is_trimmed_to_the_end(self) -> None:
        self.buffer.add(7, pcm(0.5, 100))
        self.clock.now += 0.5
        self.buffer.add(7, pcm(0.5, 200))
        self.clock.now += 0.5
        clip = self.buffer.clip(seconds=0.5, user_id=7)
        self.assertEqual(set(samples(clip)), {200})  # only the newest half second

    def test_stereo_is_downmixed_to_mono(self) -> None:
        stereo = struct.pack("<hh", 100, 300) * SAMPLE_RATE
        self.buffer.add(7, stereo)
        self.clock.now += 1.0
        self.assertEqual(set(samples(self.buffer.clip(seconds=1.0, user_id=7))), {200})

    def test_mono_input_passes_through(self) -> None:
        self.buffer.add(7, pcm(1.0, 500, channels=1), channels=1)
        self.clock.now += 1.0
        self.assertEqual(set(samples(self.buffer.clip(seconds=1.0, user_id=7))), {500})

    def test_frames_older_than_keep_s_are_dropped(self) -> None:
        self.buffer.add(7, pcm(0.1, 100))
        self.clock.now += 46.0
        self.buffer.add(7, pcm(0.1, 200))
        self.assertEqual(self.buffer.speakers(), [7])
        clip = samples(self.buffer.clip(seconds=1.0, user_id=7))
        self.assertEqual(set(clip[: -int(0.1 * SAMPLE_RATE)]), {0})  # the evicted audio is gone, not replayed
        self.assertEqual(set(clip[-int(0.1 * SAMPLE_RATE) :]), {200})

    def test_last_speaker_is_whoever_talked_most_recently(self) -> None:
        self.buffer.add(7, pcm(0.1))
        self.clock.now += 1.0
        self.buffer.add(8, pcm(0.1))
        self.assertEqual(self.buffer.last_speaker(), 8)
        self.assertEqual(self.buffer.speakers(), [8, 7])

    def test_last_speaker_is_none_when_nobody_spoke_recently(self) -> None:
        self.buffer.add(7, pcm(0.1))
        self.clock.now += 20.0
        self.assertIsNone(self.buffer.last_speaker(within_s=15.0))

    def test_an_empty_buffer_clips_nothing(self) -> None:
        self.assertEqual(self.buffer.clip(seconds=5.0), b"")
        self.assertEqual(self.buffer.clip(seconds=5.0, user_id=7), b"")
        self.assertIsNone(self.buffer.last_speaker())

    def test_everyone_is_mixed_when_no_speaker_is_chosen(self) -> None:
        self.buffer.add(7, pcm(0.5, 1000))
        self.buffer.add(8, pcm(0.5, 2000))
        self.clock.now += 0.5
        clip = self.buffer.clip(seconds=0.5)
        self.assertEqual(len(clip), int(0.5 * SAMPLE_RATE) * 2)
        self.assertEqual(set(samples(clip)), {3000})

    def test_mixing_clips_instead_of_wrapping_around(self) -> None:
        self.buffer.add(7, pcm(0.5, 30000))
        self.buffer.add(8, pcm(0.5, 30000))
        self.clock.now += 0.5
        self.assertEqual(set(samples(self.buffer.clip(seconds=0.5))), {32767})

    def test_the_clip_length_is_capped(self) -> None:
        self.buffer.add(7, pcm(1.0))
        self.clock.now += 1.0
        self.assertEqual(len(self.buffer.clip(seconds=10_000, user_id=7)), int(MAX_CLIP_S * SAMPLE_RATE) * 2)

    def test_clear_forgets_everyone(self) -> None:
        self.buffer.add(7, pcm(0.1))
        self.buffer.clear()
        self.assertEqual(self.buffer.speakers(), [])

    def test_empty_frames_are_ignored(self) -> None:
        self.buffer.add(7, b"")
        self.assertEqual(self.buffer.speakers(), [])


class ParseVoiceRequestTests(unittest.TestCase):
    def test_a_bare_clip_request_means_the_last_speaker(self) -> None:
        self.assertEqual(parse_voice_request("clip that"), VoiceRequest())
        self.assertEqual(parse_voice_request("yo clip that"), VoiceRequest())

    def test_what_he_just_said_means_the_last_speaker(self) -> None:
        for text in ("clip what he just said", "clip what she said", "clip what they just said"):
            with self.subTest(text=text):
                self.assertEqual(parse_voice_request(text), VoiceRequest(last_speaker=True))

    def test_a_bare_pronoun_means_the_last_speaker(self) -> None:
        for text in ("clip him", "clip her", "clip them"):
            with self.subTest(text=text):
                self.assertEqual(parse_voice_request(text), VoiceRequest(last_speaker=True))

    def test_a_mention_picks_that_person(self) -> None:
        self.assertEqual(parse_voice_request("clip <@7>", MEMBERS), VoiceRequest(target_id=7))
        self.assertEqual(parse_voice_request("clip <@!7> that", MEMBERS), VoiceRequest(target_id=7))

    def test_a_mention_of_someone_not_in_the_call_is_still_a_target(self) -> None:
        # resolve_target turns this into "I don't see that person in the call."
        self.assertEqual(parse_voice_request("clip <@999>", MEMBERS), VoiceRequest(target_id=999))

    def test_a_name_picks_that_person(self) -> None:
        self.assertEqual(parse_voice_request("clip pranit that", MEMBERS), VoiceRequest(target_id=7))
        self.assertEqual(parse_voice_request("clip koryei", MEMBERS), VoiceRequest(target_id=8))

    def test_an_exact_name_beats_a_longer_one_that_starts_the_same(self) -> None:
        self.assertEqual(parse_voice_request("clip pranit that", MEMBERS), VoiceRequest(target_id=7))

    def test_an_ambiguous_prefix_is_kept_for_the_adapter_to_ask_about(self) -> None:
        self.assertEqual(parse_voice_request("clip pran that", MEMBERS), VoiceRequest(target_name="pran"))

    def test_an_unknown_name_is_kept_so_the_adapter_can_say_so(self) -> None:
        self.assertEqual(parse_voice_request("clip zendaya that", MEMBERS), VoiceRequest(target_name="zendaya"))

    def test_a_duration_is_read(self) -> None:
        self.assertEqual(parse_voice_request("clip that last 20 seconds"), VoiceRequest(seconds=20.0))
        self.assertEqual(parse_voice_request("clip 15s"), VoiceRequest(seconds=15.0))
        self.assertEqual(parse_voice_request("clip pranit that last 5 seconds", MEMBERS), VoiceRequest(seconds=5.0, target_id=7))

    def test_an_absurd_duration_is_not_a_duration(self) -> None:
        self.assertEqual(parse_voice_request("clip that 500 seconds"), VoiceRequest())

    def test_everything_else_is_not_a_request(self) -> None:
        for text in ("", "hello there", "gif that", "make that a gif", "clip", "what he said", "lol"):
            with self.subTest(text=text):
                self.assertIsNone(parse_voice_request(text, MEMBERS))

    def test_a_bare_clip_with_a_name_still_counts(self) -> None:
        self.assertEqual(parse_voice_request("clip pranit", MEMBERS), VoiceRequest(target_id=7))


class ResolveTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.buffer = VoiceBuffer(clock=self.clock)

    def test_a_known_id_resolves(self) -> None:
        self.assertEqual(resolve_target(VoiceRequest(target_id=7), self.buffer, MEMBERS), (7, None))

    def test_an_id_outside_the_call_is_refused(self) -> None:
        user_id, problem = resolve_target(VoiceRequest(target_id=99), self.buffer, MEMBERS)
        self.assertIsNone(user_id)
        self.assertIn("don't see", problem)

    def test_a_name_resolves_to_one_member(self) -> None:
        self.assertEqual(resolve_target(VoiceRequest(target_name="koryei"), self.buffer, MEMBERS), (8, None))

    def test_an_unknown_name_is_refused(self) -> None:
        user_id, problem = resolve_target(VoiceRequest(target_name="zendaya"), self.buffer, MEMBERS)
        self.assertIsNone(user_id)
        self.assertIn("zendaya", problem)

    def test_an_ambiguous_name_asks_which(self) -> None:
        user_id, problem = resolve_target(VoiceRequest(target_name="pran"), self.buffer, MEMBERS)
        self.assertIsNone(user_id)
        self.assertIn("Which", problem)

    def test_no_target_means_the_last_speaker(self) -> None:
        self.buffer.add(8, pcm(0.1))
        self.clock.now += 0.5
        self.assertEqual(resolve_target(VoiceRequest(), self.buffer, MEMBERS), (8, None))

    def test_no_target_and_no_speaker_means_everyone(self) -> None:
        self.assertEqual(resolve_target(VoiceRequest(), self.buffer, MEMBERS), (None, None))


class EncodeTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is not installed")
    def test_pcm_becomes_an_ogg_opus_file(self) -> None:
        ogg = encode_ogg(pcm(0.5, 4000, channels=1))
        self.assertEqual(ogg[:4], b"OggS")
        self.assertGreater(len(ogg), 100)

    def test_a_missing_ffmpeg_is_reported(self) -> None:
        with patch("scout_voice.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "FFmpeg"):
                encode_ogg(b"\x00\x00" * 100)

    def test_a_failed_encode_is_reported(self) -> None:
        failed = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"boom")
        with patch("scout_voice.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "could not be encoded"):
                encode_ogg(b"\x00\x00" * 100)

    def test_a_slow_encode_is_reported(self) -> None:
        with patch("scout_voice.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)):
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                encode_ogg(b"\x00\x00" * 100)

    def test_the_notice_names_the_speaker_and_the_length(self) -> None:
        self.assertEqual(format_voice_notice("Pranit", 30), "🎙️ **Pranit**, last 30s")
        self.assertEqual(format_voice_notice("everyone", 12.7), "🎙️ **everyone**, last 12s")

    def test_the_default_clip_length_is_sane(self) -> None:
        self.assertGreater(DEFAULT_CLIP_S, 0)
        self.assertLessEqual(DEFAULT_CLIP_S, MAX_CLIP_S)


if __name__ == "__main__":
    unittest.main()
