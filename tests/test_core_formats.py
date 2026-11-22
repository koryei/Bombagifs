"""More input formats: phone/desktop video containers, BMP and TIFF."""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from bombagif_core import (
    ALLOWED_TYPES,
    VIDEO_SUFFIXES,
    InvalidImage,
    convert_image,
    convert_video,
    validate_attachment_metadata,
)
from selfbot_logic import NO_MEDIA_TEXT, collect_media, no_media_reply

NEW_VIDEO = (".mov", ".m4v", ".mkv", ".avi", ".3gp", ".ogv", ".wmv")
NEW_IMAGE = (".bmp", ".tif", ".tiff")


def fake_message(*names: str) -> SimpleNamespace:
    files = [SimpleNamespace(filename=n, url=f"https://cdn.discordapp.com/{n}", size=10, content_type=None) for n in names]
    return SimpleNamespace(attachments=files, embeds=[])


class SuffixTests(unittest.TestCase):
    def test_every_new_format_is_allowed(self) -> None:
        for suffix in NEW_VIDEO + NEW_IMAGE:
            with self.subTest(suffix=suffix):
                self.assertIn(suffix, ALLOWED_TYPES)
                self.assertEqual(validate_attachment_metadata(f"clip{suffix.upper()}", None, 20), suffix)

    def test_videos_are_one_set(self) -> None:
        self.assertEqual(VIDEO_SUFFIXES, {".mp4", ".webm", *NEW_VIDEO})
        self.assertTrue(VIDEO_SUFFIXES <= set(ALLOWED_TYPES))

    def test_the_old_formats_still_work_and_junk_is_still_refused(self) -> None:
        for name in ("a.png", "a.jpg", "a.mp4", "a.webm", "a.svg", "a.avif", "a.webp"):
            validate_attachment_metadata(name, None, 20)
        for name in ("a.txt", "a.zip", "a.exe", "a", "a.mov.exe"):
            with self.subTest(name=name), self.assertRaises(InvalidImage):
                validate_attachment_metadata(name, None, 20)

    def test_the_adapter_picks_up_a_mov_attachment(self) -> None:
        sources = collect_media(fake_message("tiktok.MOV")).sources
        self.assertEqual([s.filename for s in sources], ["tiktok.MOV"])


class NoMediaReplyTests(unittest.TestCase):
    def test_plain_text_when_nothing_was_attached(self) -> None:
        self.assertEqual(no_media_reply(fake_message()), NO_MEDIA_TEXT)

    def test_it_names_the_type_it_could_not_read(self) -> None:
        reply = no_media_reply(fake_message("notes.xyz"))
        self.assertTrue(reply.startswith(NO_MEDIA_TEXT))
        self.assertIn("`.xyz`", reply)

    def test_it_lists_each_unsupported_type_once(self) -> None:
        reply = no_media_reply(fake_message("a.heic", "b.HEIC", "c.flac"))
        self.assertEqual(reply.count(".heic"), 1)
        self.assertIn(".flac", reply)

    def test_odd_names_cannot_inject_markdown_or_pings(self) -> None:
        reply = no_media_reply(fake_message("x.`@everyone`", "y.a b", "z." + "q" * 50))
        self.assertNotIn("@everyone", reply)
        self.assertLessEqual(len(reply), 300)


class ImageTests(unittest.TestCase):
    def _gif_of(self, fmt: str, suffix: str) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 10, 10)).save(buffer, fmt)
        out = convert_image(buffer.getvalue(), suffix, (255, 255, 255))
        self.assertTrue(out.startswith(b"GIF8"))

    def test_bmp(self) -> None:
        self._gif_of("BMP", ".bmp")

    def test_tiff(self) -> None:
        self._gif_of("TIFF", ".tif")
        self._gif_of("TIFF", ".tiff")

    def test_a_bmp_named_png_is_still_refused(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4)).save(buffer, "BMP")
        with self.assertRaises(InvalidImage):
            convert_image(buffer.getvalue(), ".png", (255, 255, 255))


class VideoTests(unittest.TestCase):
    def test_every_video_suffix_reaches_ffmpeg(self) -> None:
        for suffix in sorted(VIDEO_SUFFIXES):
            with self.subTest(suffix=suffix):
                def fake(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                    self.assertTrue(command[command.index("-i") + 1].endswith(f"input{suffix}"))
                    Path(command[-1]).write_bytes(b"GIF89aframe;")
                    return subprocess.CompletedProcess(command, 0)

                with patch("bombagif_core.shutil.which", return_value="/usr/bin/ffmpeg"), patch(
                    "bombagif_core.subprocess.run", side_effect=fake
                ):
                    self.assertEqual(convert_video(b"video", suffix), b"GIF89aframe;")

    def test_other_suffixes_are_refused(self) -> None:
        with self.assertRaises(InvalidImage):
            convert_video(b"video", ".txt")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_a_real_mov_becomes_a_gif(self) -> None:
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "in.mov"
            made = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
                 "-pix_fmt", "yuv420p", str(path)],
                capture_output=True,
            )
            if made.returncode != 0:
                self.skipTest("this ffmpeg cannot make a test .mov")
            out = convert_video(path.read_bytes(), ".mov")
        self.assertTrue(out.startswith(b"GIF8"))


if __name__ == "__main__":
    unittest.main()
