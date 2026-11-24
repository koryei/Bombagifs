"""Tests for the DAVE bridge: without it voice_recv hands Opus ciphertext and the reader dies on the first packet."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import scout_voice_recv
from scout_voice_recv import install_dave_decrypt

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


if __name__ == "__main__":
    unittest.main()
