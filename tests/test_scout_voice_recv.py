"""Tests for the DAVE bridge: without it voice_recv hands Opus ciphertext and the reader dies on the first packet."""

from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import scout_voice_recv
from scout_voice_recv import (
    _patch_lookup_member,
    _private_hook,
    _private_lookup_member,
    install_dave_decrypt,
    install_private_call_hook,
)

FAKE_DAVEY = SimpleNamespace(MediaType=SimpleNamespace(audio="audio"))


class FakeSession:
    """Stands in for davey.DaveSession: records what it was asked to decrypt."""

    def __init__(self, *, ready: bool = True, fail: bool = False) -> None:
        self.ready = ready
        self.fail = fail
        self.calls: list[tuple] = []

    def decrypt(self, user_id, media_type, packet):
        self.calls.append((user_id, media_type, packet))
        if self.fail:
            raise ValueError("not in the group")
        return b"plain-" + packet


def make_client(*, session=None, ssrc_to_id=None):
    """A voice client with just the pieces the bridge touches."""
    decryptor = SimpleNamespace(decrypt_rtp=lambda packet: b"cipher")
    client = SimpleNamespace(
        _reader=SimpleNamespace(decryptor=decryptor),
        _connection=SimpleNamespace(dave_session=session),
        _get_id_from_ssrc=lambda ssrc: (ssrc_to_id or {}).get(ssrc),
    )
    return client, decryptor


class InstallDaveDecryptTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = patch.object(scout_voice_recv, "davey", FAKE_DAVEY)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_returns_false_without_a_reader(self) -> None:
        self.assertFalse(install_dave_decrypt(SimpleNamespace()))

    def test_returns_false_without_davey(self) -> None:
        client, _ = make_client()
        with patch.object(scout_voice_recv, "davey", None):
            self.assertFalse(install_dave_decrypt(client))

    def test_decrypts_the_payload_for_the_speaker(self) -> None:
        session = FakeSession()
        client, decryptor = make_client(session=session, ssrc_to_id={42: 7})
        self.assertTrue(install_dave_decrypt(client))
        self.assertEqual(decryptor.decrypt_rtp(SimpleNamespace(ssrc=42)), b"plain-cipher")
        self.assertEqual(session.calls, [(7, "audio", b"cipher")])

    def test_passes_through_while_the_group_is_forming(self) -> None:
        # Discord sends plaintext until the call's DAVE group is ready, so decrypting then would corrupt it.
        session = FakeSession(ready=False)
        client, decryptor = make_client(session=session, ssrc_to_id={42: 7})
        install_dave_decrypt(client)
        self.assertEqual(decryptor.decrypt_rtp(SimpleNamespace(ssrc=42)), b"cipher")
        self.assertEqual(session.calls, [])

    def test_passes_through_without_a_session(self) -> None:
        client, decryptor = make_client(session=None, ssrc_to_id={42: 7})
        install_dave_decrypt(client)
        self.assertEqual(decryptor.decrypt_rtp(SimpleNamespace(ssrc=42)), b"cipher")

    def test_passes_through_for_an_unknown_ssrc(self) -> None:
        session = FakeSession()
        client, decryptor = make_client(session=session, ssrc_to_id={})
        install_dave_decrypt(client)
        self.assertEqual(decryptor.decrypt_rtp(SimpleNamespace(ssrc=99)), b"cipher")
        self.assertEqual(session.calls, [])

    def test_a_failed_decrypt_becomes_silence_not_a_dead_reader(self) -> None:
        # Handing ciphertext to Opus raises, and the router treats that as fatal and stops listening for good.
        session = FakeSession(fail=True)
        client, decryptor = make_client(session=session, ssrc_to_id={42: 7})
        install_dave_decrypt(client)
        self.assertEqual(decryptor.decrypt_rtp(SimpleNamespace(ssrc=42)), b"")


FAKE_GATEWAY = SimpleNamespace(
    READY=2, SESSION_DESCRIPTION=4, SPEAKING=5, CLIENT_CONNECT=11, CLIENT_DISCONNECT=13
)


class FakeRecvClient:
    """Stands in for VoiceRecvClient on a private call: no guild, recipients instead of members."""

    def __init__(self, recipients=(), me=None) -> None:
        self.channel = SimpleNamespace(recipients=list(recipients), me=me)
        self.user = SimpleNamespace(id=999)
        self.guild = None
        self._reader = SimpleNamespace(
            update_secret_key=Mock(), packet_router=SimpleNamespace(destroy_decoder=Mock())
        )
        self.ssrcs: dict[int, int] = {}
        self.events: list[tuple] = []

    def _add_ssrc(self, user_id, ssrc) -> None:
        self.ssrcs[ssrc] = user_id

    def _get_ssrc_from_id(self, user_id):
        return next((ssrc for ssrc, uid in self.ssrcs.items() if uid == user_id), None)

    def _remove_ssrc(self, *, user_id) -> None:
        for ssrc, uid in list(self.ssrcs.items()):
            if uid == user_id:
                del self.ssrcs[ssrc]

    def dispatch(self, event, *args) -> None:
        self.events.append((event, args))


class PrivateCallHookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for name, value in (("vr_gateway", FAKE_GATEWAY), ("try_enum", lambda enum, value: value)):
            patcher = patch.object(scout_voice_recv, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_registers_our_own_ssrc(self) -> None:
        client = FakeRecvClient()
        await _private_hook(SimpleNamespace(secret_key=b""), {"op": 2, "d": {"ssrc": 55}}, client)
        self.assertEqual(client.ssrcs, {55: 999})

    async def test_registers_a_speaker_and_names_them(self) -> None:
        pranit = SimpleNamespace(id=7, name="Pranit")
        client = FakeRecvClient(recipients=[pranit])
        await _private_hook(
            SimpleNamespace(secret_key=b""), {"op": 5, "d": {"user_id": "7", "ssrc": 42, "speaking": 1}}, client
        )
        self.assertEqual(client.ssrcs, {42: 7})
        self.assertEqual(client.events, [("voice_member_speaking_state", (pranit, 42, 1))])

    async def test_updates_the_secret_key(self) -> None:
        client = FakeRecvClient()
        await _private_hook(SimpleNamespace(secret_key=[1, 2, 3]), {"op": 4, "d": {}}, client)
        client._reader.update_secret_key.assert_called_once_with(b"\x01\x02\x03")

    async def test_drops_a_disconnected_speaker(self) -> None:
        client = FakeRecvClient(recipients=[SimpleNamespace(id=7, name="Pranit")])
        client.ssrcs = {42: 7}
        await _private_hook(SimpleNamespace(secret_key=b""), {"op": 13, "d": {"user_id": "7"}}, client)
        client._reader.packet_router.destroy_decoder.assert_called_once_with(42)
        self.assertEqual(client.ssrcs, {})

    def test_lookup_member_names_a_group_dm_speaker(self) -> None:
        pranit = SimpleNamespace(id=7, name="Pranit")
        reader = SimpleNamespace(
            voice_client=SimpleNamespace(
                _get_id_from_ssrc=lambda ssrc: {42: 7}.get(ssrc),
                channel=SimpleNamespace(recipients=[pranit], recipient=None, me=None),
            )
        )
        self.assertIs(_private_lookup_member(reader, 42), pranit)

    def test_lookup_member_names_a_one_to_one_dm_speaker(self) -> None:
        aarav = SimpleNamespace(id=7, name="aarav")
        reader = SimpleNamespace(
            voice_client=SimpleNamespace(
                _get_id_from_ssrc=lambda ssrc: 7,
                channel=SimpleNamespace(recipients=[], recipient=aarav, me=None),
            )
        )
        self.assertIs(_private_lookup_member(reader, 42), aarav)

    def test_lookup_member_returns_none_for_an_unknown_ssrc(self) -> None:
        reader = SimpleNamespace(
            voice_client=SimpleNamespace(
                _get_id_from_ssrc=lambda ssrc: None,
                channel=SimpleNamespace(recipients=[], recipient=None, me=None),
            )
        )
        self.assertIsNone(_private_lookup_member(reader, 42))

    def test_patch_lookup_member_targets_the_speaking_timer(self) -> None:
        # _lookup_member lives on SpeakingTimer, not AudioReader: patching the wrong class broke every join.
        class SpeakingTimer:
            def _lookup_member(self, ssrc):
                return "from-the-guild"

        fake = types.ModuleType("discord.ext.voice_recv.reader")
        fake.SpeakingTimer = SpeakingTimer
        with patch.dict(sys.modules, {"discord.ext.voice_recv.reader": fake}):
            _patch_lookup_member()
            _patch_lookup_member()  # idempotent: never wrap our own wrapper
        self.assertTrue(getattr(SpeakingTimer._lookup_member, "_bombagif_private", False))

    def test_the_patched_lookup_leaves_guild_calls_alone(self) -> None:
        class SpeakingTimer:
            def _lookup_member(self, ssrc):
                return "from-the-guild"

        fake = types.ModuleType("discord.ext.voice_recv.reader")
        fake.SpeakingTimer = SpeakingTimer
        with patch.dict(sys.modules, {"discord.ext.voice_recv.reader": fake}):
            _patch_lookup_member()
        timer = SpeakingTimer()
        timer.voice_client = SimpleNamespace(guild=object())
        self.assertEqual(timer._lookup_member(42), "from-the-guild")

    def test_the_patched_lookup_names_a_private_call_speaker(self) -> None:
        class SpeakingTimer:
            def _lookup_member(self, ssrc):
                return "from-the-guild"

        fake = types.ModuleType("discord.ext.voice_recv.reader")
        fake.SpeakingTimer = SpeakingTimer
        with patch.dict(sys.modules, {"discord.ext.voice_recv.reader": fake}):
            _patch_lookup_member()
        aarav = SimpleNamespace(id=7, name="aarav")
        timer = SpeakingTimer()
        timer.voice_client = SimpleNamespace(
            guild=None,
            _get_id_from_ssrc=lambda ssrc: 7,
            channel=SimpleNamespace(recipients=[], recipient=aarav, me=None),
        )
        self.assertIs(timer._lookup_member(42), aarav)

    def test_install_returns_false_without_voice_recv(self) -> None:
        with patch.object(scout_voice_recv, "vr_client", None):
            self.assertFalse(install_private_call_hook())

    def test_install_wraps_the_hook_once(self) -> None:
        async def original(ws, msg):
            return None

        fake = SimpleNamespace(hook=original)
        with patch.object(scout_voice_recv, "vr_client", fake):
            self.assertTrue(install_private_call_hook())
            self.assertTrue(install_private_call_hook())  # idempotent: never wrap our own wrapper
        self.assertIsNot(fake.hook, original)
        self.assertTrue(getattr(fake.hook, "_bombagif_private", False))

    async def test_the_wrapped_hook_leaves_guild_calls_alone(self) -> None:
        seen = []

        async def original(ws, msg):
            seen.append(msg)

        fake = SimpleNamespace(hook=original)
        with patch.object(scout_voice_recv, "vr_client", fake):
            install_private_call_hook()
        ws = SimpleNamespace(_connection=SimpleNamespace(voice_client=SimpleNamespace(guild=object())))
        await fake.hook(ws, {"op": 2, "d": {"ssrc": 1}})
        self.assertEqual(seen, [{"op": 2, "d": {"ssrc": 1}}])


if __name__ == "__main__":
    unittest.main()