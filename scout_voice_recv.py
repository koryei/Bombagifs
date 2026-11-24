"""The DAVE bridge for ``discord-ext-voice-recv``: decrypt the payload a second time before Opus sees it.

Discord now encrypts call audio end to end with the DAVE protocol. ``discord.py-self`` speaks it (it needs the
``davey`` package, or the voice socket closes with 4017), but ``discord-ext-voice-recv`` predates it: the reader
decrypts the transport layer with nacl and hands the payload straight to Opus. Once the call's DAVE group is
ready, that payload is MLS ciphertext, so every packet fails to decode with ``OpusError: corrupted stream`` and
the router stops listening for good.

``install_dave_decrypt`` wraps the reader's RTP decryptor so the payload is decrypted once more on the way
through. While the group is still forming Discord sends plaintext (passthrough mode), so a session that is not
ready yet is left alone.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # optional, like voice receive itself: without davey the bot cannot join a call at all
    import davey
except ImportError:  # pragma: no cover - exercised by the main venv, which has no davey
    davey = None  # type: ignore[assignment]

logger = logging.getLogger("bombagif.selfbot")


def install_dave_decrypt(client: Any) -> bool:
    """Decrypt DAVE payloads on ``client``'s reader. Returns False when there is nothing to wrap."""
    if davey is None:
        return False
    reader = getattr(client, "_reader", None)
    decryptor = getattr(reader, "decryptor", None)
    if decryptor is None:
        return False
    original = decryptor.decrypt_rtp

    def decrypt_rtp(packet: Any) -> bytes:
        data = original(packet)
        session = getattr(getattr(client, "_connection", None), "dave_session", None)
        if session is None or not session.ready:
            return data  # passthrough: the group is not formed yet, so the payload is still plaintext
        user_id = client._get_id_from_ssrc(packet.ssrc)
        if user_id is None:
            return data
        try:
            return session.decrypt(user_id, davey.MediaType.audio, data)
        except Exception:
            # Handing ciphertext to Opus raises, and the router treats that as fatal and stops listening, so
            # drop the packet as silence instead.
            logger.debug("Could not decrypt a DAVE packet from %s", user_id, exc_info=True)
            return b""

    decryptor.decrypt_rtp = decrypt_rtp
    return True
