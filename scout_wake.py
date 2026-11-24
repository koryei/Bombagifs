"""The spoken trigger (optional): hear the wake word in a call and clip without typing anything.

Porcupine does the listening, so nothing is transcribed and no audio leaves the machine. It wants 16 kHz mono
frames of a fixed length; the call delivers 48 kHz mono, so ``feed`` resamples and re-chunks on the way in.

The package and the access key are both optional: without them ``build_wake_word`` returns ``None`` and the bot
simply has no spoken trigger. Nothing here raises into the voice loop.
"""

from __future__ import annotations

import logging
from array import array
from typing import Protocol

logger = logging.getLogger("bombagif.scout")

CALL_RATE = 48000
WAKE_RATE = 16000
RESAMPLE = CALL_RATE // WAKE_RATE  # exactly 3, so no fractional resampling is needed


class WakeWord(Protocol):
    """A detector fed mono 48 kHz s16le; ``feed`` returns True when the word was heard."""

    def feed(self, pcm: bytes) -> bool: ...

    def close(self) -> None: ...


class NullWakeWord:
    """The spoken trigger turned off: never fires, costs nothing."""

    def feed(self, pcm: bytes) -> bool:
        return False

    def close(self) -> None:
        return None


def to_wake_rate(pcm: bytes) -> bytes:
    """48 kHz mono s16le to 16 kHz mono s16le, averaging each group of three samples (a crude anti-alias)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - len(pcm) % (2 * RESAMPLE)])
    out = array("h", bytes(len(samples) // RESAMPLE * 2))
    for index in range(len(out)):
        base = index * RESAMPLE
        out[index] = (samples[base] + samples[base + 1] + samples[base + 2]) // RESAMPLE
    return out.tobytes()


class PorcupineWakeWord:
    """Porcupine, fed from the call. ``keyword`` is a built-in word; ``keyword_path`` is a custom ``.ppn`` file."""

    def __init__(self, access_key: str, *, keyword: str = "computer", keyword_path: str | None = None) -> None:
        import pvporcupine  # imported here so the package stays optional

        if keyword_path:
            self._handle = pvporcupine.create(access_key=access_key, keyword_paths=[keyword_path])
        else:
            self._handle = pvporcupine.create(access_key=access_key, keywords=[keyword])
        self._frame_bytes = self._handle.frame_length * 2
        self._pending = bytearray()

    def feed(self, pcm: bytes) -> bool:
        self._pending.extend(to_wake_rate(pcm))
        heard = False
        while len(self._pending) >= self._frame_bytes:
            frame = bytes(self._pending[: self._frame_bytes])
            del self._pending[: self._frame_bytes]
            if self._handle.process(frame) >= 0:
                heard = True
        return heard

    def close(self) -> None:
        try:
            self._handle.delete()
        except Exception:  # a closed handle must never break shutdown
            logger.debug("Could not close the wake-word detector", exc_info=True)


def build_wake_word(*, enabled: bool, access_key: str, keyword: str, keyword_path: str) -> WakeWord | None:
    """Build the detector, or ``None`` when it is off or cannot be built. Never raises."""
    if not enabled:
        return None
    if not access_key:
        logger.warning("SCOUT_VOICE_WAKE is on but PICOVOICE_ACCESS_KEY is empty; the spoken trigger stays off")
        return None
    try:
        return PorcupineWakeWord(access_key, keyword=keyword or "computer", keyword_path=keyword_path or None)
    except ImportError:
        logger.warning("SCOUT_VOICE_WAKE is on but pvporcupine is not installed; the spoken trigger stays off")
    except Exception:
        logger.warning("Could not start the wake-word detector; the spoken trigger stays off", exc_info=True)
    return None
