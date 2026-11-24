"""The DAVE and private-call bridges for ``discord-ext-voice-recv``.

Two things the extension predates:

* **DAVE.** Discord now encrypts call audio end to end. ``discord.py-self`` speaks it (it needs the ``davey``
  package, or the voice socket closes with 4017), but the reader decrypts the transport layer with nacl and hands
  the payload straight to Opus. Once the call's DAVE group is ready that payload is MLS ciphertext, so every
  packet fails to decode with ``OpusError: corrupted stream`` and the router stops listening for good.
  ``install_dave_decrypt`` wraps the reader's RTP decryptor to decrypt it once more.

* **Private calls.** The gateway hook reads ``vc.guild.me`` and ``vc.guild.get_member``, and a group DM's client
  has no guild at all, so the handshake dies with ``AttributeError: 'NoneType' object has no attribute 'me'``.
  ``install_private_call_hook`` swaps in a hook that resolves members from the channel's own recipients. Guild
  calls keep the upstream hook untouched.
"""

from __future__ import annotations

import logging
from typing import Any

try:  # optional, like voice receive itself: without davey the bot cannot join a call at all
    import davey
except ImportError:  # pragma: no cover - exercised by the main venv, which has no davey
    davey = None  # type: ignore[assignment]

try:  # optional: the main venv has no voice receive, and then neither bridge has anything to wrap
    from discord.enums import SpeakingState, try_enum
    from discord.ext.voice_recv import gateway as vr_gateway
    from discord.ext.voice_recv import voice_client as vr_client
except ImportError:  # pragma: no cover - exercised by the main venv
    SpeakingState = None  # type: ignore[assignment]
    try_enum = None  # type: ignore[assignment]
    vr_gateway = None  # type: ignore[assignment]
    vr_client = None  # type: ignore[assignment]

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


def _private_member(client: Any, user_id: int) -> Any:
    """The user behind an id in a private call: its recipients plus ourselves, since there is no guild to ask."""
    channel = getattr(client, "channel", None)
    for user in (*getattr(channel, "recipients", ()), getattr(channel, "recipient", None), getattr(channel, "me", None)):
        if user is not None and getattr(user, "id", None) == user_id:
            return user
    return None


def _private_lookup_member(reader: Any, ssrc: int) -> Any:
    """The member behind an ssrc in a private call, where the reader's own lookup has no guild to ask."""
    user_id = reader.voice_client._get_id_from_ssrc(ssrc)
    if not user_id:
        return None
    return _private_member(reader.voice_client, user_id)


def _patch_lookup_member() -> None:
    """The reader asks the guild for a speaker's member on every packet, which kills the audio thread."""
    try:
        from discord.ext.voice_recv.reader import AudioReader
    except ImportError:  # pragma: no cover - exercised by the main venv, which has no voice receive
        return

    if getattr(AudioReader._lookup_member, "_bombagif_private", False):
        return
    original = AudioReader._lookup_member

    def _lookup_member(self: Any, ssrc: int) -> Any:
        if self.voice_client.guild is not None:
            return original(self, ssrc)
        return _private_lookup_member(self, ssrc)

    _lookup_member._bombagif_private = True  # type: ignore[attr-defined]
    AudioReader._lookup_member = _lookup_member


async def _private_hook(ws: Any, msg: dict, client: Any) -> None:
    """The upstream gateway hook, minus the guild: a private call resolves members from its own recipients."""
    op = msg["op"]
    data = msg.get("d", {})
    if op == vr_gateway.READY:
        client._add_ssrc(client.user.id, data["ssrc"])
    elif op == vr_gateway.SESSION_DESCRIPTION:
        if client._reader:
            client._reader.update_secret_key(bytes(ws.secret_key))
    elif op == vr_gateway.SPEAKING:
        user_id = int(data["user_id"])
        client._add_ssrc(user_id, data["ssrc"])
        state = try_enum(SpeakingState, data["speaking"])
        client.dispatch("voice_member_speaking_state", _private_member(client, user_id), data["ssrc"], state)
    elif op == vr_gateway.CLIENT_CONNECT:
        for user_id in (int(uid) for uid in data["user_ids"]):
            client.dispatch("voice_member_connect", _private_member(client, user_id))
    elif op == vr_gateway.CLIENT_DISCONNECT:
        user_id = int(data["user_id"])
        ssrc = client._get_ssrc_from_id(user_id)
        if client._reader and ssrc is not None:
            client._reader.packet_router.destroy_decoder(ssrc)
        client._remove_ssrc(user_id=user_id)
        client.dispatch("voice_member_disconnect", _private_member(client, user_id), ssrc)


def install_private_call_hook() -> bool:
    """Let voice_recv handle a private call. Returns False when voice receive is not installed."""
    if vr_client is None:
        return False
    _patch_lookup_member()
    if getattr(vr_client.hook, "_bombagif_private", False):
        return True
    original = vr_client.hook

    async def hook(ws: Any, msg: dict) -> None:
        client = ws._connection.voice_client
        if getattr(client, "guild", None) is not None:
            return await original(ws, msg)
        return await _private_hook(ws, msg, client)

    hook._bombagif_private = True  # type: ignore[attr-defined]
    vr_client.hook = hook
    return True
