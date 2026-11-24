"""The optional spoken trigger: resampling, the null detector, and the guarded Porcupine build."""

from __future__ import annotations

import struct
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scout_wake import (
    CALL_RATE,
    RESAMPLE,
    WAKE_RATE,
    NullWakeWord,
    PorcupineWakeWord,
    build_wake_word,
    to_wake_rate,
)


def pcm(seconds: float, value: int = 1000) -> bytes:
    return struct.pack("<h", value) * int(seconds * CALL_RATE)


def samples(data: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(data) // 2}h", data))


class ResampleTests(unittest.TestCase):
    def test_48k_becomes_16k(self) -> None:
        self.assertEqual(RESAMPLE, 3)
        self.assertEqual(len(to_wake_rate(pcm(1.0))), int(1.0 * WAKE_RATE) * 2)

    def test_each_output_sample_averages_three_input_samples(self) -> None:
        data = struct.pack("<hhh", 300, 600, 900) * 10
        self.assertEqual(set(samples(to_wake_rate(data))), {600})

    def test_a_partial_group_is_dropped(self) -> None:
        self.assertEqual(to_wake_rate(struct.pack("<hh", 1, 2)), b"")

    def test_empty_input_is_empty_output(self) -> None:
        self.assertEqual(to_wake_rate(b""), b"")


class NullWakeWordTests(unittest.TestCase):
    def test_it_never_fires(self) -> None:
        detector = NullWakeWord()
        self.assertFalse(detector.feed(pcm(1.0)))
        detector.close()  # closing is a no-op, not an error


class FakePorcupine:
    """Stands in for pvporcupine: fires when a frame is all ones."""

    def __init__(self, frame_length: int = 512) -> None:
        self.frame_length = frame_length
        self.frames: list[bytes] = []
        self.deleted = False

    def process(self, frame: bytes) -> int:
        self.frames.append(frame)
        return 0 if frame == b"\x01\x00" * self.frame_length else -1

    def delete(self) -> None:
        self.deleted = True


class PorcupineWakeWordTests(unittest.TestCase):
    def build(self, handle: FakePorcupine) -> PorcupineWakeWord:
        module = SimpleNamespace(create=lambda **kwargs: handle)
        with patch.dict(sys.modules, {"pvporcupine": module}):
            return PorcupineWakeWord("key", keyword="computer")

    def test_it_feeds_whole_frames_and_reports_a_detection(self) -> None:
        handle = FakePorcupine(frame_length=4)
        detector = self.build(handle)
        self.assertFalse(detector.feed(b"\x00\x00"))  # not enough for one frame yet
        self.assertEqual(handle.frames, [])
        heard = detector.feed(struct.pack("<h", 1) * (4 * RESAMPLE))
        self.assertTrue(heard)
        self.assertEqual(len(handle.frames), 1)
        self.assertEqual(len(handle.frames[0]), 4 * 2)

    def test_a_quiet_frame_is_not_a_detection(self) -> None:
        handle = FakePorcupine(frame_length=4)
        detector = self.build(handle)
        self.assertFalse(detector.feed(struct.pack("<h", 0) * (4 * RESAMPLE)))

    def test_close_deletes_the_handle(self) -> None:
        handle = FakePorcupine()
        detector = self.build(handle)
        detector.close()
        self.assertTrue(handle.deleted)

    def test_close_survives_a_broken_handle(self) -> None:
        handle = FakePorcupine()
        handle.delete = lambda: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        detector = self.build(handle)
        detector.close()  # must not raise


class BuildWakeWordTests(unittest.TestCase):
    def test_off_means_no_detector(self) -> None:
        self.assertIsNone(build_wake_word(enabled=False, access_key="key", keyword="computer", keyword_path=""))

    def test_no_key_means_no_detector(self) -> None:
        with self.assertLogs("bombagif.scout", "WARNING"):
            self.assertIsNone(build_wake_word(enabled=True, access_key="", keyword="computer", keyword_path=""))

    def test_a_missing_package_means_no_detector(self) -> None:
        with patch.dict(sys.modules, {"pvporcupine": None}):
            with self.assertLogs("bombagif.scout", "WARNING"):
                self.assertIsNone(build_wake_word(enabled=True, access_key="key", keyword="computer", keyword_path=""))

    def test_a_broken_detector_means_no_detector(self) -> None:
        module = SimpleNamespace(create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("bad key")))
        with patch.dict(sys.modules, {"pvporcupine": module}):
            with self.assertLogs("bombagif.scout", "WARNING"):
                self.assertIsNone(build_wake_word(enabled=True, access_key="key", keyword="computer", keyword_path=""))

    def test_a_custom_keyword_file_is_used_when_given(self) -> None:
        seen: dict[str, object] = {}

        def create(**kwargs):
            seen.update(kwargs)
            return FakePorcupine()

        module = SimpleNamespace(create=create)
        with patch.dict(sys.modules, {"pvporcupine": module}):
            detector = build_wake_word(enabled=True, access_key="key", keyword="computer", keyword_path="/tmp/bomba.ppn")
        self.assertIsNotNone(detector)
        self.assertEqual(seen["keyword_paths"], ["/tmp/bomba.ppn"])
        self.assertNotIn("keywords", seen)

    def test_a_built_in_keyword_is_used_without_a_file(self) -> None:
        seen: dict[str, object] = {}

        def create(**kwargs):
            seen.update(kwargs)
            return FakePorcupine()

        module = SimpleNamespace(create=create)
        with patch.dict(sys.modules, {"pvporcupine": module}):
            build_wake_word(enabled=True, access_key="key", keyword="jarvis", keyword_path="")
        self.assertEqual(seen["keywords"], ["jarvis"])


if __name__ == "__main__":
    unittest.main()
